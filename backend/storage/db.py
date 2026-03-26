import aiosqlite
import json
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "trading_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event TEXT NOT NULL,
    account INTEGER,
    server TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    agent_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    max_holding_minutes INTEGER,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    signal_id INTEGER,
    approved INTEGER NOT NULL,
    reason TEXT NOT NULL,
    adjusted_volume REAL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    volume REAL NOT NULL,
    price REAL,
    stop_loss REAL,
    take_profit REAL,
    ticket INTEGER,
    retcode INTEGER,
    retcode_desc TEXT,
    success INTEGER NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS position_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    ticket INTEGER NOT NULL,
    event TEXT NOT NULL,
    symbol TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS ai_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ticket INTEGER,
    detail TEXT NOT NULL,
    profit REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS saved_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account INTEGER NOT NULL UNIQUE,
    server TEXT NOT NULL,
    password TEXT NOT NULL,
    terminal_path TEXT DEFAULT '',
    label TEXT DEFAULT '',
    created_at REAL NOT NULL,
    last_used REAL
);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info(f"Database initialized at {self.path}")

    async def close(self):
        if self._db:
            await self._db.close()

    async def log_connection_event(
        self, event: str, account: int = 0, server: str = "", details: str = ""
    ):
        await self._db.execute(
            "INSERT INTO connection_events (timestamp, event, account, server, details) VALUES (?, ?, ?, ?, ?)",
            (time.time(), event, account, server, details),
        )
        await self._db.commit()

    async def log_signal(
        self,
        agent_name: str,
        symbol: str,
        timeframe: str,
        action: str,
        confidence: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        max_holding_minutes: Optional[int],
        reason: str,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO signals
            (timestamp, agent_name, symbol, timeframe, action, confidence, stop_loss, take_profit, max_holding_minutes, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                agent_name,
                symbol,
                timeframe,
                action,
                confidence,
                stop_loss,
                take_profit,
                max_holding_minutes,
                reason,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def log_risk_decision(
        self,
        signal_id: int,
        approved: bool,
        reason: str,
        adjusted_volume: float = 0.0,
    ):
        await self._db.execute(
            "INSERT INTO risk_decisions (timestamp, signal_id, approved, reason, adjusted_volume) VALUES (?, ?, ?, ?, ?)",
            (time.time(), signal_id, int(approved), reason, adjusted_volume),
        )
        await self._db.commit()

    async def log_order(
        self,
        signal_id: Optional[int],
        symbol: str,
        action: str,
        volume: float,
        price: Optional[float],
        stop_loss: Optional[float],
        take_profit: Optional[float],
        ticket: Optional[int],
        retcode: int,
        retcode_desc: str,
        success: bool,
    ):
        await self._db.execute(
            """INSERT INTO orders
            (timestamp, signal_id, symbol, action, volume, price, stop_loss, take_profit, ticket, retcode, retcode_desc, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                signal_id,
                symbol,
                action,
                volume,
                price,
                stop_loss,
                take_profit,
                ticket,
                retcode,
                retcode_desc,
                int(success),
            ),
        )
        await self._db.commit()

    async def log_position_change(
        self, ticket: int, event: str, symbol: str = "", details: str = ""
    ):
        await self._db.execute(
            "INSERT INTO position_changes (timestamp, ticket, event, symbol, details) VALUES (?, ?, ?, ?, ?)",
            (time.time(), ticket, event, symbol, details),
        )
        await self._db.commit()

    async def log_error(self, source: str, message: str, details: str = ""):
        await self._db.execute(
            "INSERT INTO errors (timestamp, source, message, details) VALUES (?, ?, ?, ?)",
            (time.time(), source, message, details),
        )
        await self._db.commit()

    async def get_logs(self, limit: int = 100, log_type: str = "all") -> list[dict]:
        results = []

        if log_type in ("all", "signals"):
            cursor = await self._db.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                entry = dict(zip(cols, row))
                entry["log_type"] = "signal"
                results.append(entry)

        if log_type in ("all", "risk"):
            cursor = await self._db.execute(
                "SELECT * FROM risk_decisions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                entry = dict(zip(cols, row))
                entry["log_type"] = "risk_decision"
                results.append(entry)

        if log_type in ("all", "orders"):
            cursor = await self._db.execute(
                "SELECT * FROM orders ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                entry = dict(zip(cols, row))
                entry["log_type"] = "order"
                results.append(entry)

        if log_type in ("all", "connections"):
            cursor = await self._db.execute(
                "SELECT * FROM connection_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                entry = dict(zip(cols, row))
                entry["log_type"] = "connection"
                results.append(entry)

        if log_type in ("all", "errors"):
            cursor = await self._db.execute(
                "SELECT * FROM errors ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                entry = dict(zip(cols, row))
                entry["log_type"] = "error"
                results.append(entry)

        results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return results[:limit]

    async def get_trade_history(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT o.*, s.agent_name, s.confidence, s.reason as signal_reason,
                      r.approved, r.reason as risk_reason
               FROM orders o
               LEFT JOIN signals s ON o.signal_id = s.id
               LEFT JOIN risk_decisions r ON r.signal_id = s.id
               ORDER BY o.timestamp DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def save_credentials(
        self, account: int, server: str, password: str,
        terminal_path: str = "", label: str = "",
    ):
        await self._db.execute(
            """INSERT OR REPLACE INTO saved_credentials
            (account, server, password, terminal_path, label, created_at, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account, server, password, terminal_path, label, time.time(), time.time()),
        )
        await self._db.commit()

    async def get_saved_credentials(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM saved_credentials ORDER BY last_used DESC"
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def delete_saved_credentials(self, account: int):
        await self._db.execute(
            "DELETE FROM saved_credentials WHERE account = ?", (account,)
        )
        await self._db.commit()

    async def update_credential_last_used(self, account: int):
        await self._db.execute(
            "UPDATE saved_credentials SET last_used = ? WHERE account = ?",
            (time.time(), account),
        )
        await self._db.commit()

    async def get_signal_by_id(self, signal_id: int) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def get_risk_decision_by_signal(self, signal_id: int) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM risk_decisions WHERE signal_id = ? ORDER BY id DESC LIMIT 1",
            (signal_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def log_ai_activity(
        self, action: str, symbol: str, ticket: int, detail: str, profit: float = 0.0
    ):
        await self._db.execute(
            "INSERT INTO ai_activity (timestamp, action, symbol, ticket, detail, profit) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), action, symbol, ticket, detail, profit),
        )
        await self._db.commit()

    async def get_ai_activity(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM ai_activity ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
