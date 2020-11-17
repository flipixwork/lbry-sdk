import logging
import asyncio
import json
from time import perf_counter
from operator import itemgetter
from contextlib import asynccontextmanager
from functools import partial
from typing import Dict, Optional, Tuple
import aiohttp

from lbry import __version__
from lbry.error import IncompatibleWalletServerError
from lbry.wallet.rpc import RPCSession as BaseClientSession, Connector, RPCError, ProtocolError
from lbry.wallet.stream import StreamController


log = logging.getLogger(__name__)


class ClientSession(BaseClientSession):
    def __init__(self, *args, network, server, timeout=30, on_connect_callback=None, **kwargs):
        self.network = network
        self.server = server
        super().__init__(*args, **kwargs)
        self._on_disconnect_controller = StreamController()
        self.on_disconnected = self._on_disconnect_controller.stream
        self.framer.max_size = self.max_errors = 1 << 32
        self.timeout = timeout
        self.max_seconds_idle = timeout * 2
        self.response_time: Optional[float] = None
        self.connection_latency: Optional[float] = None
        self._response_samples = 0
        self.pending_amount = 0
        self._on_connect_cb = on_connect_callback or (lambda: None)
        self.trigger_urgent_reconnect = asyncio.Event()
        self._connected = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._disconnected.set()

        self._on_status_controller = StreamController(merge_repeated_events=True)
        self.on_status = self._on_status_controller.stream
        self.on_status.listen(partial(self.network.ledger.process_status_update, self))
        self.subscription_controllers = {
            'blockchain.headers.subscribe': self.network._on_header_controller,
            'blockchain.address.subscribe': self._on_status_controller,
        }

    @property
    def available(self):
        return not self.is_closing() and self.response_time is not None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def server_address_and_port(self) -> Optional[Tuple[str, int]]:
        if not self.transport:
            return None
        return self.transport.get_extra_info('peername')

    async def send_timed_server_version_request(self, args=(), timeout=None):
        timeout = timeout or self.timeout
        log.debug("send version request to %s:%i", *self.server)
        start = perf_counter()
        result = await asyncio.wait_for(
            super().send_request('server.version', args), timeout=timeout
        )
        current_response_time = perf_counter() - start
        response_sum = (self.response_time or 0) * self._response_samples + current_response_time
        self.response_time = response_sum / (self._response_samples + 1)
        self._response_samples += 1
        return result

    async def send_request(self, method, args=()):
        self.pending_amount += 1
        log.debug("send %s%s to %s:%i", method, tuple(args), *self.server)
        try:
            if method == 'server.version':
                return await self.send_timed_server_version_request(args, self.timeout)
            request = asyncio.ensure_future(super().send_request(method, args))
            while not request.done():
                done, pending = await asyncio.wait([request], timeout=self.timeout)
                if pending:
                    log.debug("Time since last packet: %s", perf_counter() - self.last_packet_received)
                    if (perf_counter() - self.last_packet_received) < self.timeout:
                        continue
                    log.info("timeout sending %s to %s:%i", method, *self.server)
                    raise asyncio.TimeoutError
                if done:
                    try:
                        return request.result()
                    except ConnectionResetError:
                        log.error(
                            "wallet server (%s) reset connection upon our %s request, json of %i args is %i bytes",
                            self.server[0], method, len(args), len(json.dumps(args))
                        )
                        raise
        except (RPCError, ProtocolError) as e:
            log.warning("Wallet server (%s:%i) returned an error. Code: %s Message: %s",
                        *self.server, *e.args)
            raise e
        except ConnectionError:
            log.warning("connection to %s:%i lost", *self.server)
            self.synchronous_close()
            raise
        except asyncio.CancelledError:
            log.info("cancelled sending %s to %s:%i", method, *self.server)
            # self.synchronous_close()
            raise
        finally:
            self.pending_amount -= 1

    async def ensure_session(self):
        # Handles reconnecting and maintaining a session alive
        retry_delay = default_delay = 1.0
        while True:
            try:
                if self.is_closing():
                    await self.create_connection(self.timeout)
                    await self.ensure_server_version()
                    self._on_connect_cb()
                if (perf_counter() - self.last_send) > self.max_seconds_idle or self.response_time is None:
                    await self.ensure_server_version()
                retry_delay = default_delay
            except RPCError as e:
                await self.close()
                log.debug("Server error, ignoring for 1h: %s:%d -- %s", *self.server, e.message)
                retry_delay = 60 * 60
            except IncompatibleWalletServerError:
                await self.close()
                retry_delay = 60 * 60
                log.debug("Wallet server has an incompatible version, retrying in 1h: %s:%d", *self.server)
            except (asyncio.TimeoutError, OSError):
                await self.close()
                retry_delay = min(60, retry_delay * 2)
                log.debug("Wallet server timeout (retry in %s seconds): %s:%d", retry_delay, *self.server)
            try:
                await asyncio.wait_for(self.trigger_urgent_reconnect.wait(), timeout=retry_delay)
            except asyncio.TimeoutError:
                pass
            finally:
                self.trigger_urgent_reconnect.clear()

    async def ensure_server_version(self, required=None, timeout=3):
        required = required or self.network.PROTOCOL_VERSION
        response = await asyncio.wait_for(
            self.send_request('server.version', [__version__, required]), timeout=timeout
        )
        if tuple(int(piece) for piece in response[0].split(".")) < self.network.MINIMUM_REQUIRED:
            raise IncompatibleWalletServerError(*self.server)
        return response

    async def create_connection(self, timeout=6):
        connector = Connector(lambda: self, *self.server)
        start = perf_counter()
        await asyncio.wait_for(connector.create_connection(), timeout=timeout)
        self.connection_latency = perf_counter() - start

    async def handle_request(self, request):
        controller = self.subscription_controllers[request.method]
        controller.add(request.args)

    def connection_lost(self, exc):
        log.debug("Connection lost: %s:%d", *self.server)
        super().connection_lost(exc)
        self.response_time = None
        self.connection_latency = None
        self._response_samples = 0
        self._on_disconnect_controller.add(True)
        self._connected.clear()
        self._disconnected.set()

    def connection_made(self, transport):
        super(ClientSession, self).connection_made(transport)
        self._disconnected.clear()
        self._connected.set()


class Network:

    PROTOCOL_VERSION = __version__
    MINIMUM_REQUIRED = (0, 65, 0)

    def __init__(self, ledger):
        self.ledger = ledger
        self.session_pool = SessionPool(network=self, timeout=self.config.get('connect_timeout', 6))
        self.client: Optional[ClientSession] = None
        self.clients: Dict[str, ClientSession] = {}
        self.server_features = None
        self._switch_task: Optional[asyncio.Task] = None
        self.running = False
        self.remote_height: int = 0
        self._concurrency = asyncio.Semaphore(16)

        self._on_connected_controller = StreamController()
        self.on_connected = self._on_connected_controller.stream

        self._on_header_controller = StreamController(merge_repeated_events=True)
        self.on_header = self._on_header_controller.stream

        self.subscription_controllers = {
            'blockchain.headers.subscribe': self._on_header_controller,
        }

        self.aiohttp_session: Optional[aiohttp.ClientSession] = None

    def get_wallet_session(self, wallet):
        return self.clients[wallet.id]

    async def close_wallet_session(self, wallet):
        session = self.clients.pop(wallet.id)
        ensure_connect_task = self.session_pool.wallet_session_tasks.pop(session)
        if ensure_connect_task and not ensure_connect_task.done():
            ensure_connect_task.cancel()
        await session.close()

    @property
    def config(self):
        return self.ledger.config

    async def switch_forever(self):
        while self.running:
            if self.is_connected():
                await self.client.on_disconnected.first
                self.server_features = None
                self.client = None
                continue

            self.client = await self.session_pool.wait_for_fastest_session()
            log.info("Switching to SPV wallet server: %s:%d", *self.client.server)
            try:
                self.server_features = await self.get_server_features()
                self._update_remote_height((await self.subscribe_headers(),))
                self._on_connected_controller.add(True)
                log.info("Subscribed to headers: %s:%d", *self.client.server)
            except (asyncio.TimeoutError, ConnectionError):
                log.info("Switching to %s:%d timed out, closing and retrying.", *self.client.server)
                self.client.synchronous_close()
                self.server_features = None
                self.client = None

    async def start(self):
        self.running = True
        self.aiohttp_session = aiohttp.ClientSession()
        self._switch_task = asyncio.ensure_future(self.switch_forever())
        # this may become unnecessary when there are no more bugs found,
        # but for now it helps understanding log reports
        self._switch_task.add_done_callback(lambda _: log.info("Wallet client switching task stopped."))
        self.session_pool.start(self.config['default_servers'])
        self.on_header.listen(self._update_remote_height)
        await self.ledger.subscribe_accounts()

    async def connect_wallets(self, *wallet_ids):
        if wallet_ids:
            await asyncio.wait([self.session_pool.connect_wallet(wallet_id) for wallet_id in wallet_ids])

    async def stop(self):
        if self.running:
            self.running = False
            await self.aiohttp_session.close()
            self._switch_task.cancel()
            self.session_pool.stop()

    def is_connected(self, wallet_id: str = None):
        if wallet_id is None:
            if not self.client:
                return False
            return self.client.is_connected
        if wallet_id not in self.clients:
            return False
        return self.clients[wallet_id].is_connected

    def connect_wallet_client(self, wallet_id: str, server: Tuple[str, int]):
        client = ClientSession(network=self, server=server)
        self.clients[wallet_id] = client
        return client

    def rpc(self, list_or_method, args, restricted=True, session=None):
        session = session or (self.client if restricted else self.session_pool.fastest_session)
        if session and not session.is_closing():
            return session.send_request(list_or_method, args)
        else:
            self.session_pool.trigger_nodelay_connect()
            raise ConnectionError("Attempting to send rpc request when connection is not available.")

    async def retriable_call(self, function, *args, **kwargs):
        async with self._concurrency:
            while self.running:
                if not self.is_connected:
                    log.warning("Wallet server unavailable, waiting for it to come back and retry.")
                    await self.on_connected.first
                await self.session_pool.wait_for_fastest_session()
                try:
                    return await function(*args, **kwargs)
                except asyncio.TimeoutError:
                    log.warning("Wallet server call timed out, retrying.")
                except ConnectionError:
                    pass
        raise asyncio.CancelledError()  # if we got here, we are shutting down

    @asynccontextmanager
    async def fastest_connection_context(self):

        if not self.is_connected:
            log.warning("Wallet server unavailable, waiting for it to come back and retry.")
            await self.on_connected.first
        await self.session_pool.wait_for_fastest_session()
        server = self.session_pool.fastest_session.server
        session = ClientSession(network=self, server=server)

        async def call_with_reconnect(function, *args, **kwargs):
            nonlocal session

            while self.running:
                if not session.is_connected:
                    try:
                        await session.create_connection()
                    except asyncio.TimeoutError:
                        if not session.is_connected:
                            log.warning("Wallet server unavailable, waiting for it to come back and retry.")
                            await self.on_connected.first
                        await self.session_pool.wait_for_fastest_session()
                        server = self.session_pool.fastest_session.server
                        session = ClientSession(network=self, server=server)
                try:
                    return await partial(function, *args, session_override=session, **kwargs)(*args, **kwargs)
                except asyncio.TimeoutError:
                    log.warning("'%s' failed, retrying", function.__name__)
        try:
            yield call_with_reconnect
        finally:
            await session.close()

    @asynccontextmanager
    async def single_call_context(self, function, *args, **kwargs):
        if not self.is_connected():
            log.warning("Wallet server unavailable, waiting for it to come back and retry.")
            await self.on_connected.first
        await self.session_pool.wait_for_fastest_session()
        server = self.session_pool.fastest_session.server
        session = ClientSession(network=self, server=server)

        async def call_with_reconnect(*a, **kw):
            while self.running:
                if not session.available:
                    await session.create_connection()
                try:
                    return await partial(function, *args, session_override=session, **kwargs)(*a, **kw)
                except asyncio.TimeoutError:
                    log.warning("'%s' failed, retrying", function.__name__)
        try:
            yield (call_with_reconnect, session)
        finally:
            await session.close()

    def _update_remote_height(self, header_args):
        self.remote_height = header_args[0]["height"]

    def get_transaction(self, tx_hash, known_height=None, session=None):
        # use any server if its old, otherwise restrict to who gave us the history
        restricted = known_height in (None, -1, 0) or 0 > known_height > self.remote_height - 10
        return self.rpc('blockchain.transaction.get', [tx_hash], restricted, session=session)

    def get_transaction_batch(self, txids, restricted=True, session=None):
        # use any server if its old, otherwise restrict to who gave us the history
        return self.rpc('blockchain.transaction.get_batch', txids, restricted, session=session)

    def get_transaction_and_merkle(self, tx_hash, known_height=None, session=None):
        # use any server if its old, otherwise restrict to who gave us the history
        restricted = known_height in (None, -1, 0) or 0 > known_height > self.remote_height - 10
        return self.rpc('blockchain.transaction.info', [tx_hash], restricted, session=session)

    def get_transaction_height(self, tx_hash, known_height=None, session=None):
        restricted = not known_height or 0 > known_height > self.remote_height - 10
        return self.rpc('blockchain.transaction.get_height', [tx_hash], restricted, session=session)

    def get_merkle(self, tx_hash, height, session=None):
        restricted = 0 > height > self.remote_height - 10
        return self.rpc('blockchain.transaction.get_merkle', [tx_hash, height], restricted, session=session)

    def get_headers(self, height, count=10000, b64=False):
        restricted = height >= self.remote_height - 100
        return self.rpc('blockchain.block.headers', [height, count, 0, b64], restricted)

    #  --- Subscribes, history and broadcasts are always aimed towards the master client directly
    def get_history(self, address, session=None):
        return self.rpc('blockchain.address.get_history', [address], True, session=session)

    def broadcast(self, raw_transaction, session=None):
        return self.rpc('blockchain.transaction.broadcast', [raw_transaction], True, session=session)

    def subscribe_headers(self):
        return self.rpc('blockchain.headers.subscribe', [True], True)

    async def subscribe_address(self, session, address, *addresses):
        addresses = list((address, ) + addresses)
        server_addr_and_port = session.server_address_and_port  # on disconnect client will be None
        try:
            return await self.rpc('blockchain.address.subscribe', addresses, True, session=session)
        except asyncio.TimeoutError:
            log.warning(
                "timed out subscribing to addresses from %s:%i",
                *server_addr_and_port
            )
            # abort and cancel, we can't lose a subscription, it will happen again on reconnect
            if session:
                session.abort()
            raise asyncio.CancelledError()

    def unsubscribe_address(self, address, session=None):
        return self.rpc('blockchain.address.unsubscribe', [address], True, session=session)

    def get_server_features(self, session=None):
        return self.rpc('server.features', (), restricted=True, session=session)

    def get_claims_by_ids(self, claim_ids):
        return self.rpc('blockchain.claimtrie.getclaimsbyids', claim_ids)

    def resolve(self, urls, session_override=None):
        return self.rpc('blockchain.claimtrie.resolve', urls, False, session=session_override)

    def claim_search(self, session_override=None, **kwargs):
        return self.rpc('blockchain.claimtrie.search', kwargs, False, session=session_override)

    async def new_resolve(self, server, urls):
        message = {"method": "resolve", "params": {"urls": urls, "protobuf": True}}
        async with self.aiohttp_session.post(server, json=message) as r:
            result = await r.json()
            return result['result']

    async def new_claim_search(self, server, **kwargs):
        kwargs['protobuf'] = True
        message = {"method": "claim_search", "params": kwargs}
        async with self.aiohttp_session.post(server, json=message) as r:
            result = await r.json()
            return result['result']

    async def sum_supports(self, server, **kwargs):
        message = {"method": "support_sum", "params": kwargs}
        async with self.aiohttp_session.post(server, json=message) as r:
            result = await r.json()
            return result['result']


class SessionPool:

    def __init__(self, network: Network, timeout: float):
        self.network = network
        self.sessions: Dict[ClientSession, Optional[asyncio.Task]] = dict()
        self.wallet_session_tasks: Dict[ClientSession, Optional[asyncio.Task]] = dict()
        self.timeout = timeout
        self.new_connection_event = asyncio.Event()

    @property
    def online(self):
        return any(not session.is_closing() for session in self.sessions)

    @property
    def available_sessions(self):
        return (session for session in self.sessions if session.available)

    @property
    def fastest_session(self):
        if not self.online:
            return None
        return min(
            [((session.response_time + session.connection_latency) * (session.pending_amount + 1), session)
             for session in self.available_sessions] or [(0, None)],
            key=itemgetter(0)
        )[1]

    def _get_session_connect_callback(self, session: ClientSession):
        loop = asyncio.get_event_loop()

        def callback():
            duplicate_connections = [
                s for s in self.sessions
                if s is not session and s.server_address_and_port == session.server_address_and_port
            ]
            already_connected = None if not duplicate_connections else duplicate_connections[0]
            if already_connected:
                self.sessions.pop(session).cancel()
                session.synchronous_close()
                log.debug("wallet server %s resolves to the same server as %s, rechecking in an hour",
                          session.server[0], already_connected.server[0])
                loop.call_later(3600, self._connect_session, session.server)
                return
            self.new_connection_event.set()
            log.info("connected to %s:%i", *session.server)

        return callback

    def _connect_session(self, server: Tuple[str, int]):
        session = None
        for s in self.sessions:
            if s.server == server:
                session = s
                break
        if not session:
            session = ClientSession(
                network=self.network, server=server
            )
            session._on_connect_cb = self._get_session_connect_callback(session)
        task = self.sessions.get(session, None)
        if not task or task.done():
            task = asyncio.create_task(session.ensure_session())
            task.add_done_callback(lambda _: self.ensure_connections())
            self.sessions[session] = task

    async def connect_wallet(self, wallet_id: str):
        fastest = await self.wait_for_fastest_session()
        connected = asyncio.Event()
        if not self.network.is_connected(wallet_id):
            session = self.network.connect_wallet_client(wallet_id, fastest.server)
            session._on_connect_cb = connected.set
            # session._on_connect_cb = self._get_session_connect_callback(session)
        else:
            connected.set()
            session = self.network.clients[wallet_id]
        task = self.wallet_session_tasks.get(session, None)
        if not task or task.done():
            task = asyncio.create_task(session.ensure_session())
            # task.add_done_callback(lambda _: self.ensure_connections())
            self.wallet_session_tasks[session] = task
        await connected.wait()

    def start(self, default_servers):
        for server in default_servers:
            self._connect_session(server)

    def stop(self):
        for session, task in self.sessions.items():
            task.cancel()
            session.synchronous_close()
        self.sessions.clear()

    def ensure_connections(self):
        for session in self.sessions:
            self._connect_session(session.server)

    def trigger_nodelay_connect(self):
        # used when other parts of the system sees we might have internet back
        # bypasses the retry interval
        for session in self.sessions:
            session.trigger_urgent_reconnect.set()

    async def wait_for_fastest_session(self):
        while not self.fastest_session:
            self.trigger_nodelay_connect()
            self.new_connection_event.clear()
            await self.new_connection_event.wait()
        return self.fastest_session
