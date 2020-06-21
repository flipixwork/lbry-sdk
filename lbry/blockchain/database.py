import os.path
import asyncio
import sqlite3
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor
from lbry.schema.url import normalize_name

from .bcd_data_stream import BCDataStream


FILES = [
    'block_index',
    'claims'
]


class FindShortestID:
    __slots__ = 'short_id', 'new_id'

    def __init__(self):
        self.short_id = ''
        self.new_id = None

    def step(self, other_id, new_id):
        other_id = other_id[::-1].hex()
        if self.new_id is None:
            self.new_id = new_id[::-1].hex()
        for i in range(len(self.new_id)):
            if other_id[i] != self.new_id[i]:
                if i > len(self.short_id)-1:
                    self.short_id = self.new_id[:i+1]
                break

    def finalize(self):
        return self.short_id


class BlockchainDB:

    def __init__(self, directory: str):
        self.directory = directory
        self.connection: Optional[sqlite3.Connection] = None
        self.executor: Optional[ThreadPoolExecutor] = None

    async def run_in_executor(self, *args):
        return await asyncio.get_event_loop().run_in_executor(self.executor, *args)

    def sync_open(self):
        self.connection = sqlite3.connect(
            os.path.join(self.directory, FILES[0]+'.sqlite'),
            timeout=60.0 * 5
        )
        for file in FILES[1:]:
            self.connection.execute(
                f"ATTACH DATABASE '{os.path.join(self.directory, file+'.sqlite')}' AS {file}"
            )
        self.connection.create_aggregate("find_shortest_id", 2, FindShortestID)
        #self.connection.execute(
        #    "CREATE INDEX IF NOT EXISTS claim_originalheight_claimid ON claim (originalheight, claimid);"
        #)
        self.connection.row_factory = sqlite3.Row

    async def open(self):
        assert self.executor is None, "Database is already open."
        self.executor = ThreadPoolExecutor(max_workers=1)
        return await self.run_in_executor(self.sync_open)

    def sync_close(self):
        self.connection.close()
        self.connection = None

    async def close(self):
        if self.executor is not None:
            if self.connection is not None:
                await self.run_in_executor(self.sync_close)
            self.executor.shutdown()
            self.executor = None

    async def commit(self):
        await self.run_in_executor(self.connection.commit)

    def sync_execute(self, sql: str, *args):
        return self.connection.execute(sql, *args)

    async def execute(self, sql: str, *args):
        return await self.run_in_executor(self.sync_execute, sql, *args)

    def sync_execute_fetchall(self, sql: str, *args) -> List[dict]:
        return [dict(r) for r in self.connection.execute(sql, *args).fetchall()]

    async def execute_fetchall(self, sql: str, *args) -> List[dict]:
        return await self.run_in_executor(self.sync_execute_fetchall, sql, *args)

    def sync_get_best_height(self) -> int:
        sql = "SELECT MAX(height) FROM block_info"
        return self.connection.execute(sql).fetchone()[0]

    async def get_best_height(self) -> int:
        return await self.run_in_executor(self.sync_get_best_height)

    def sync_get_block_files(self, file_number: int = None, start_height: int = None) -> List[dict]:
        sql = """
            SELECT
                file as file_number,
                COUNT(hash) as blocks,
                SUM(txcount) as txs,
                MAX(height) as best_height
            FROM block_info
            WHERE status&1 AND status&4
        """
        args = ()
        if file_number is not None and start_height is not None:
            sql += "AND file = ? AND height >= ?"
            args = (file_number, start_height)
        return self.sync_execute_fetchall(sql + " GROUP BY file ORDER BY file ASC;", args)

    async def get_block_files(self, file_number: int = None, start_height: int = None) -> List[dict]:
        return await self.run_in_executor(
            self.sync_get_block_files, file_number, start_height
        )

    def sync_get_blocks_in_file(self, block_file: int, start_height=0) -> List[dict]:
        return self.sync_execute_fetchall(
            """
            SELECT datapos as data_offset, height, hash as block_hash, txCount as txs
            FROM block_info
            WHERE file = ? AND height >= ? AND status&1 AND status&4
            ORDER BY datapos ASC;
            """, (block_file, start_height)
        )

    async def get_blocks_in_file(self, block_file: int, start_height=0) -> List[dict]:
        return await self.run_in_executor(self.sync_get_blocks_in_file, block_file, start_height)

    def sync_get_claim_support_txo_hashes(self, at_height: int) -> set:
        return {
            r['txID'] + BCDataStream.uint32.pack(r['txN'])
            for r in self.connection.execute(
                """
                SELECT txID, txN FROM claim WHERE updateHeight = ?
                UNION
                SELECT txID, txN FROM support WHERE blockHeight = ?
                """, (at_height, at_height)
            ).fetchall()
        }

    def sync_get_takeover_count(self, start_height: int, end_height: int) -> int:
        sql = "SELECT COUNT(*) FROM takeover WHERE height BETWEEN ? AND ?"
        return self.connection.execute(sql, (start_height, end_height)).fetchone()[0]

    async def get_takeover_count(self, start_height: int, end_height: int) -> int:
        return await self.run_in_executor(self.sync_get_takeover_count, start_height, end_height)

    def sync_get_takeovers(self, start_height: int, end_height: int) -> List[dict]:
        return self.sync_execute_fetchall(
            """
            SELECT
                takeover.name,
                takeover.claimID AS claim_hash,
                takeover.height
            FROM takeover
            WHERE height BETWEEN ? AND ?
            ORDER BY height, name
            """, (start_height, end_height)
        )

    async def get_takeovers(self, start_height: int, end_height: int) -> List[dict]:
        return await self.run_in_executor(self.sync_get_takeovers, start_height, end_height)

    def sync_get_claim_metadata_count(self, start_height: int, end_height: int) -> int:
        sql = "SELECT COUNT(*) FROM claim WHERE originalHeight BETWEEN ? AND ?"
        return self.connection.execute(sql, (start_height, end_height)).fetchone()[0]

    async def get_claim_metadata_count(self, start_height: int, end_height: int) -> int:
        return await self.run_in_executor(self.sync_get_claim_metadata_count, start_height, end_height)

    def sync_get_claim_metadata(self, start_height: int, end_height: int) -> List[dict]:
        return [{
            "name": r["name"],
            "claim_hash_": r["claimID"],
            "activation_height": r["activationHeight"],
            "expiration_height": r["expirationHeight"],
            "takeover_height": r["takeoverHeight"],
            "is_controlling": r["isControlling"],
            "short_url": f'{normalize_name(r["name"].decode())}#{r["shortestID"] or r["claimID"][::-1].hex()[0]}',
            "short_url_": f'{normalize_name(r["name"].decode())}#{r["shortestID"] or r["claimID"][::-1].hex()[0]}',
            } for r in self.sync_execute_fetchall(
            """
            SELECT
                name, claimID, activationHeight, expirationHeight,
                (SELECT
                    CASE WHEN takeover.claimID = claim.claimID THEN takeover.height END
                    FROM takeover WHERE takeover.name = claim.name
                    ORDER BY height DESC LIMIT 1
                ) AS takeoverHeight,
                (SELECT CASE WHEN takeover.claimID = claim.claimID THEN 1 ELSE 0 END
                 FROM takeover WHERE takeover.name = claim.name
                 ORDER BY height DESC LIMIT 1
                ) AS isControlling,
                (SELECT find_shortest_id(c.claimid, claim.claimid) FROM claim AS c
                 WHERE
                    c.nodename = claim.nodename AND
                    c.originalheight <= claim.originalheight AND
                    c.claimid != claim.claimid
                ) AS shortestID
            FROM claim
            WHERE originalHeight BETWEEN ? AND ?
            ORDER BY originalHeight, claimid
            """, (start_height, end_height)
        )]

    async def get_claim_metadata(self, start_height: int, end_height: int) -> List[dict]:
        return await self.run_in_executor(self.sync_get_claim_metadata, start_height, end_height)

    def sync_get_support_metadata_count(self, start_height: int, end_height: int) -> int:
        sql = "SELECT COUNT(*) FROM support WHERE blockHeight BETWEEN ? AND ?"
        return self.connection.execute(sql, (start_height, end_height)).fetchone()[0]

    async def get_support_metadata_count(self, start_height: int, end_height: int) -> int:
        return await self.run_in_executor(self.sync_get_support_metadata_count, start_height, end_height)

    def sync_get_support_metadata(self, start_height: int, end_height: int) -> List[dict]:
        return [{
            "name": r['name'],
            "txo_hash_pk": r['txID'] + BCDataStream.uint32.pack(r['txN']),
            "activation_height": r['activationHeight'],
            "expiration_height": r['expirationHeight'],
            } for r in self.sync_execute_fetchall(
            """
            SELECT name, txid, txn, activationHeight, expirationHeight
            FROM support WHERE blockHeight BETWEEN ? AND ?
            """, (start_height, end_height)
            )
        ]

    async def get_support_metadata(self, start_height: int, end_height: int) -> List[dict]:
        return await self.run_in_executor(self.sync_get_support_metadata, start_height, end_height)