import asyncio
import struct
from time import perf_counter
import logging
from typing import Optional, Tuple, NamedTuple
from lbry.utils import LRUCache
# from prometheus_client import Counter


log = logging.getLogger(__name__)
_MAGIC = 1446058291  # genesis blocktime (which is actually wrong)
# ping_count_metric = Counter("ping_count", "Number of pings received", namespace='wallet_server_status')
_PAD_BYTES = b'\x00' * 64


class SPVPing(NamedTuple):
    magic: int
    protocol_version: int
    pad_bytes: bytes

    def encode(self):
        return struct.pack(b'!lB64s', *self)

    @staticmethod
    def make(protocol_version=1) -> bytes:
        return SPVPing(_MAGIC, protocol_version, _PAD_BYTES).encode()

    @classmethod
    def decode(cls, packet: bytes):
        decoded = cls(*struct.unpack(b'!lB64s', packet[:69]))
        if decoded.magic != _MAGIC:
            raise ValueError("invalid magic bytes")
        return decoded


class SPVPong(NamedTuple):
    protocol_version: int
    flags: int
    height: int
    tip: bytes
    source_address_raw: bytes

    def encode(self):
        return struct.pack(b'!BBl32s4s', *self)

    @staticmethod
    def make(height: int, tip: bytes, flags: int, protocol_version: int = 1) -> bytes:
        # note: drops the last 4 bytes so the result can be cached and have addresses added to it as needed
        return SPVPong(protocol_version, flags, height, tip, b'\x00\x00\x00\x00').encode()[:38]

    @classmethod
    def decode(cls, packet: bytes):
        return cls(*struct.unpack(b'!BBl32s4s', packet[:42]))

    @property
    def available(self) -> bool:
        return (self.flags & 0b00000001) > 0

    @property
    def ip_address(self) -> str:
        return ".".join(map(str, self.source_address_raw))

    def __repr__(self) -> str:
        return f"SPVPong(external_ip={self.ip_address}, version={self.protocol_version}, " \
               f"available={'True' if self.flags & 1 > 0 else 'False'}," \
               f" height={self.height}, tip={self.tip[::-1].hex()})"


class SPVServerStatusProtocol(asyncio.DatagramProtocol):
    PROTOCOL_VERSION = 1

    def __init__(self, height: int, tip: bytes, throttle_cache_size: int = 1024, throttle_rate: int = 10):
        super().__init__()
        self.transport: Optional[asyncio.transports.DatagramTransport] = None
        self._height = height
        self._tip = tip
        self._flags = 0
        self._cached_response = None
        self.update_cached_response()
        self._throttle = LRUCache(throttle_cache_size)
        self._time_now = 0.0
        self._last_ts = perf_counter()
        self._throttle_rate = throttle_rate

    def update_cached_response(self):
        self._cached_response = SPVPong.make(self._height, self._tip, self._flags, self.PROTOCOL_VERSION)

    def set_unavailable(self):
        self._flags &= 0b11111110
        self.update_cached_response()

    def set_available(self):
        self._flags |= 0b00000001
        self.update_cached_response()

    def set_height(self, height: int, tip: bytes):
        self._height, self._tip = height, tip
        self.update_cached_response()

    def should_throttle(self, host: str):
        key = int(self._time_now).to_bytes(4, byteorder='big') + host.encode()
        reqs = self._throttle.get(key, default=0) + 1
        self._throttle[key] = reqs
        if reqs >= self._throttle_rate:
            return True
        return False

    def make_pong(self, host):
        return self._cached_response + bytes(int(b) for b in host.split("."))

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        now, (host, port) = perf_counter(), addr
        self._time_now, self._last_ts = self._time_now + (now - self._last_ts), now
        if self.should_throttle(host):
            return
        try:
            SPVPing.decode(data)
        except (ValueError, struct.error, AttributeError, TypeError):
            log.exception("derp")
            return
        self.transport.sendto(self.make_pong(addr[0]), addr)
        # ping_count_metric.inc()

    def connection_made(self, transport) -> None:
        self.transport = transport

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self.transport = None

    def close(self):
        if self.transport:
            self.transport.close()


class StatusServer:
    def __init__(self):
        self._protocol: Optional[SPVServerStatusProtocol] = None

    async def start(self, height: int, tip: bytes, interface: str, port: int):
        if self._protocol:
            return
        loop = asyncio.get_event_loop()
        self._protocol = SPVServerStatusProtocol(height, tip)
        await loop.create_datagram_endpoint(lambda: self._protocol, (interface, port), reuse_port=True)
        log.info("started udp status server on %s:%i", interface, port)

    def stop(self):
        if self._protocol:
            self._protocol.close()
            self._protocol = None

    def set_unavailable(self):
        self._protocol.set_unavailable()

    def set_available(self):
        self._protocol.set_available()

    def set_height(self, height: int, tip: bytes):
        self._protocol.set_height(height, tip)
