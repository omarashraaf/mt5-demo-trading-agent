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
    comment TEXT DEFAULT '',
    success INTEGER NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
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
    secret_ref TEXT DEFAULT '',
    secret_backend TEXT DEFAULT '',
    terminal_path TEXT DEFAULT '',
    label TEXT DEFAULT '',
    created_at REAL NOT NULL,
    last_used REAL
);

CREATE TABLE IF NOT EXISTS evaluation_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    evaluation_mode TEXT NOT NULL,
    requested_agent_name TEXT,
    primary_agent_name TEXT NOT NULL,
    final_agent_name TEXT NOT NULL,
    raw_technical_signal TEXT NOT NULL,
    executable_signal TEXT NOT NULL,
    executable_action TEXT NOT NULL,
    market_context_summary TEXT NOT NULL,
    gemini_assessment TEXT,
    trade_quality TEXT,
    portfolio_risk TEXT,
    anti_churn TEXT,
    risk_evaluation TEXT,
    execution_decision TEXT NOT NULL,
    position_management_plan TEXT,
    signal_id INTEGER,
    quality_score REAL DEFAULT 0.0,
    outcome_status TEXT DEFAULT 'pending',
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS position_management_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL UNIQUE,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    closed_at REAL NOT NULL,
    ticket INTEGER,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    profit REAL DEFAULT 0.0,
    exit_reason TEXT NOT NULL,
    holding_minutes REAL DEFAULT 0.0,
    symbol_category TEXT DEFAULT '',
    strategy TEXT DEFAULT '',
    planned_hold_minutes INTEGER DEFAULT 0,
    outcome_json TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS external_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    timestamp_utc REAL NOT NULL,
    event_type TEXT NOT NULL,
    category TEXT DEFAULT '',
    country TEXT DEFAULT '',
    importance TEXT DEFAULT 'medium',
    actual TEXT,
    forecast TEXT,
    previous TEXT,
    affected_assets_json TEXT NOT NULL DEFAULT '[]',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    fetched_at REAL NOT NULL,
    usable INTEGER NOT NULL DEFAULT 1,
    usability_reason TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event_fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    success INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT DEFAULT '',
    cursor TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_to_asset_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_event_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    baseline_bias TEXT NOT NULL DEFAULT 'neutral',
    needs_gemini_clarification INTEGER NOT NULL DEFAULT 1,
    tradable INTEGER NOT NULL DEFAULT 0,
    mapping_score REAL NOT NULL DEFAULT 0.0,
    reason TEXT DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(external_event_id, symbol),
    FOREIGN KEY (external_event_id) REFERENCES external_events(id)
);

CREATE TABLE IF NOT EXISTS gemini_event_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_event_id INTEGER NOT NULL,
    assessment_json TEXT NOT NULL,
    changed_mapping INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (external_event_id) REFERENCES external_events(id)
);

CREATE TABLE IF NOT EXISTS app_runtime_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._migrate_schema()
        await self._db.commit()
        logger.info(f"Database initialized at {self.path}")

    async def close(self):
        if self._db:
            await self._db.close()

    async def _migrate_schema(self):
        cursor = await self._db.execute("PRAGMA table_info(saved_credentials)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "secret_ref" not in columns:
            await self._db.execute(
                "ALTER TABLE saved_credentials ADD COLUMN secret_ref TEXT DEFAULT ''"
            )
        if "secret_backend" not in columns:
            await self._db.execute(
                "ALTER TABLE saved_credentials ADD COLUMN secret_backend TEXT DEFAULT ''"
            )
        cursor = await self._db.execute("PRAGMA table_info(evaluation_journal)")
        rows = await cursor.fetchall()
        if rows:
            eval_columns = {row[1] for row in rows}
            if "executable_action" not in eval_columns:
                await self._db.execute(
                    "ALTER TABLE evaluation_journal ADD COLUMN executable_action TEXT DEFAULT 'HOLD'"
                )
            if "quality_score" not in eval_columns:
                await self._db.execute(
                    "ALTER TABLE evaluation_journal ADD COLUMN quality_score REAL DEFAULT 0.0"
                )
            if "outcome_status" not in eval_columns:
                await self._db.execute(
                    "ALTER TABLE evaluation_journal ADD COLUMN outcome_status TEXT DEFAULT 'pending'"
                )
        cursor = await self._db.execute("PRAGMA table_info(trade_outcomes)")
        rows = await cursor.fetchall()
        if rows:
            outcome_columns = {row[1] for row in rows}
            if "holding_minutes" not in outcome_columns:
                await self._db.execute(
                    "ALTER TABLE trade_outcomes ADD COLUMN holding_minutes REAL DEFAULT 0.0"
                )
            if "symbol_category" not in outcome_columns:
                await self._db.execute(
                    "ALTER TABLE trade_outcomes ADD COLUMN symbol_category TEXT DEFAULT ''"
                )
            if "strategy" not in outcome_columns:
                await self._db.execute(
                    "ALTER TABLE trade_outcomes ADD COLUMN strategy TEXT DEFAULT ''"
                )
            if "planned_hold_minutes" not in outcome_columns:
                await self._db.execute(
                    "ALTER TABLE trade_outcomes ADD COLUMN planned_hold_minutes INTEGER DEFAULT 0"
                )
        cursor = await self._db.execute("PRAGMA table_info(orders)")
        rows = await cursor.fetchall()
        if rows:
            order_columns = {row[1] for row in rows}
            if "comment" not in order_columns:
                await self._db.execute(
                    "ALTER TABLE orders ADD COLUMN comment TEXT DEFAULT ''"
                )

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
        comment: str = "",
    ):
        await self._db.execute(
            """INSERT INTO orders
            (timestamp, signal_id, symbol, action, volume, price, stop_loss, take_profit, ticket, retcode, retcode_desc, comment, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                comment or "",
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
        self,
        account: int,
        server: str,
        terminal_path: str = "",
        label: str = "",
        secret_ref: str = "",
        secret_backend: str = "",
    ):
        await self._db.execute(
            """INSERT OR REPLACE INTO saved_credentials
            (account, server, password, secret_ref, secret_backend, terminal_path, label, created_at, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account,
                server,
                "",
                secret_ref,
                secret_backend,
                terminal_path,
                label,
                time.time(),
                time.time(),
            ),
        )
        await self._db.commit()

    async def get_saved_credentials(self) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT id, account, server, secret_ref, secret_backend, terminal_path,
                      label, created_at, last_used
               FROM saved_credentials
               ORDER BY last_used DESC"""
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def get_saved_credential(self, account: int) -> Optional[dict]:
        cursor = await self._db.execute(
            """SELECT * FROM saved_credentials
               WHERE account = ?
               ORDER BY last_used DESC
               LIMIT 1""",
            (account,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

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

    async def log_evaluation_journal(
        self,
        symbol: str,
        timeframe: str,
        evaluation_mode: str,
        requested_agent_name: str,
        primary_agent_name: str,
        final_agent_name: str,
        raw_technical_signal: dict,
        executable_signal: dict,
        market_context_summary: dict,
        gemini_assessment: Optional[dict],
        trade_quality: dict,
        portfolio_risk: dict,
        anti_churn: dict,
        risk_evaluation: dict,
        execution_decision: dict,
        position_management_plan: dict,
        signal_id: Optional[int] = None,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO evaluation_journal
            (timestamp, symbol, timeframe, evaluation_mode, requested_agent_name, primary_agent_name,
             final_agent_name, raw_technical_signal, executable_signal, executable_action, market_context_summary,
             gemini_assessment, trade_quality, portfolio_risk, anti_churn, risk_evaluation, execution_decision,
             position_management_plan, signal_id, quality_score, outcome_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                symbol,
                timeframe,
                evaluation_mode,
                requested_agent_name,
                primary_agent_name,
                final_agent_name,
                json.dumps(raw_technical_signal),
                json.dumps(executable_signal),
                executable_signal.get("action", "HOLD"),
                json.dumps(market_context_summary),
                json.dumps(gemini_assessment) if gemini_assessment is not None else None,
                json.dumps(trade_quality),
                json.dumps(portfolio_risk),
                json.dumps(anti_churn),
                json.dumps(risk_evaluation),
                json.dumps(execution_decision),
                json.dumps(position_management_plan),
                signal_id,
                trade_quality.get("final_trade_quality_score", 0.0),
                "pending",
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_recent_symbol_outcomes(
        self,
        symbol: str,
        limit: int = 5,
        within_minutes: Optional[int] = None,
    ) -> list[dict]:
        normalized_symbol = (symbol or "").upper()
        query = """
            SELECT * FROM trade_outcomes
            WHERE symbol = ?
        """
        params: list = [normalized_symbol]
        if within_minutes is not None:
            query += " AND closed_at >= ?"
            params.append(time.time() - within_minutes * 60)
        query += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        outcomes = [dict(zip(cols, row)) for row in rows]
        if outcomes:
            return outcomes

        # Backward-compatible fallback:
        # if structured trade_outcomes are sparse, derive minimal recent outcomes
        # from AI close activity so anti-churn/risk can still enforce cooldowns.
        fallback_query = """
            SELECT timestamp, action, symbol, profit, detail
            FROM ai_activity
            WHERE symbol = ?
              AND action LIKE 'close_%'
        """
        fallback_params: list = [normalized_symbol]
        if within_minutes is not None:
            fallback_query += " AND timestamp >= ?"
            fallback_params.append(time.time() - within_minutes * 60)
        fallback_query += " ORDER BY timestamp DESC LIMIT ?"
        fallback_params.append(limit)
        cursor = await self._db.execute(fallback_query, tuple(fallback_params))
        fb_rows = await cursor.fetchall()
        derived: list[dict] = []
        for ts, action, sym, profit, detail in fb_rows:
            direction = "BUY"
            text = f"{action or ''} {detail or ''}".upper()
            if "SELL" in text:
                direction = "SELL"
            derived.append(
                {
                    "closed_at": float(ts or time.time()),
                    "timestamp": float(ts or time.time()),
                    "action": direction,
                    "symbol": (sym or normalized_symbol).upper(),
                    "profit": float(profit or 0.0),
                    "exit_reason": str(action or "close"),
                }
            )
        return derived

    async def get_recent_symbol_evaluations(
        self,
        symbol: str,
        limit: int = 5,
        within_minutes: Optional[int] = None,
    ) -> list[dict]:
        query = """
            SELECT id, timestamp, symbol, executable_action, quality_score, execution_decision, outcome_status
            FROM evaluation_journal
            WHERE symbol = ?
        """
        params: list = [symbol]
        if within_minutes is not None:
            query += " AND timestamp >= ?"
            params.append(time.time() - within_minutes * 60)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def save_position_management_plan(
        self,
        ticket: int,
        signal_id: Optional[int],
        symbol: str,
        action: str,
        plan: dict,
    ):
        now = time.time()
        await self._db.execute(
            """INSERT OR REPLACE INTO position_management_plans
            (ticket, signal_id, symbol, action, plan_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT status FROM position_management_plans WHERE ticket = ?), 'open'),
                    COALESCE((SELECT created_at FROM position_management_plans WHERE ticket = ?), ?), ?)""",
            (
                ticket,
                signal_id,
                symbol,
                action,
                json.dumps(plan),
                ticket,
                ticket,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def get_position_management_plan(self, ticket: int) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM position_management_plans WHERE ticket = ? LIMIT 1",
            (ticket,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        result = dict(zip(cols, row))
        if result.get("plan_json"):
            result["plan_json"] = json.loads(result["plan_json"])
        return result

    async def update_position_management_plan_status(self, ticket: int, status: str):
        await self._db.execute(
            "UPDATE position_management_plans SET status = ?, updated_at = ? WHERE ticket = ?",
            (status, time.time(), ticket),
        )
        await self._db.commit()

    async def log_trade_outcome(
        self,
        ticket: Optional[int],
        signal_id: Optional[int],
        symbol: str,
        action: str,
        confidence: float,
        profit: float,
        exit_reason: str,
        holding_minutes: float = 0.0,
        symbol_category: str = "",
        strategy: str = "",
        planned_hold_minutes: Optional[int] = None,
        outcome_json: Optional[dict] = None,
    ):
        await self._db.execute(
            """INSERT INTO trade_outcomes
            (timestamp, closed_at, ticket, signal_id, symbol, action, confidence, profit, exit_reason,
             holding_minutes, symbol_category, strategy, planned_hold_minutes, outcome_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                time.time(),
                ticket,
                signal_id,
                symbol,
                action,
                confidence,
                profit,
                exit_reason,
                holding_minutes,
                symbol_category,
                strategy,
                int(planned_hold_minutes or 0),
                json.dumps(outcome_json) if outcome_json is not None else None,
            ),
        )
        if ticket is not None:
            await self.update_position_management_plan_status(ticket, "closed")
        await self._db.commit()

    async def get_trade_outcome_by_ticket(self, ticket: int) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM trade_outcomes WHERE ticket = ? ORDER BY closed_at DESC LIMIT 1",
            (ticket,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def mark_evaluation_outcome(self, signal_id: Optional[int], status: str):
        if signal_id is None:
            return
        await self._db.execute(
            "UPDATE evaluation_journal SET outcome_status = ? WHERE signal_id = ?",
            (status, signal_id),
        )
        await self._db.commit()

    async def get_trade_outcomes(self, limit: int = 200) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM trade_outcomes ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def log_event_fetch_run(
        self,
        *,
        provider: str,
        started_at: float,
        finished_at: float,
        success: bool,
        item_count: int,
        error: str = "",
        cursor: str = "",
    ) -> int:
        db_cursor = await self._db.execute(
            """INSERT INTO event_fetch_runs
            (provider, started_at, finished_at, success, item_count, error, cursor)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (provider, started_at, finished_at, int(success), item_count, error, cursor),
        )
        await self._db.commit()
        return db_cursor.lastrowid

    async def save_external_event(self, event: dict) -> int:
        payload = dict(event)
        now = time.time()
        await self._db.execute(
            """INSERT OR IGNORE INTO external_events
            (source, source_event_id, dedupe_key, title, summary, timestamp_utc, event_type, category,
             country, importance, actual, forecast, previous, affected_assets_json, raw_payload_json,
             fetched_at, usable, usability_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["source"],
                payload["source_event_id"],
                payload["dedupe_key"],
                payload["title"],
                payload.get("summary", ""),
                payload["timestamp_utc"],
                payload.get("event_type", "unknown"),
                payload.get("category", ""),
                payload.get("country", ""),
                payload.get("importance", "medium"),
                self._json_cell(payload.get("actual")),
                self._json_cell(payload.get("forecast")),
                self._json_cell(payload.get("previous")),
                json.dumps(payload.get("affected_assets", [])),
                json.dumps(payload.get("raw_payload", {})),
                float(payload.get("fetched_at", now)),
                int(bool(payload.get("usable", True))),
                payload.get("usability_reason", ""),
                now,
            ),
        )
        await self._db.commit()
        cursor = await self._db.execute(
            "SELECT id FROM external_events WHERE dedupe_key = ? LIMIT 1",
            (payload["dedupe_key"],),
        )
        row = await cursor.fetchone()
        return int(row[0])

    async def get_latest_external_events(
        self,
        limit: int = 50,
        usable_only: bool = True,
    ) -> list[dict]:
        query = "SELECT * FROM external_events"
        params: list = []
        if usable_only:
            query += " WHERE usable = 1"
        query += " ORDER BY timestamp_utc DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [self._decode_external_event(dict(zip(cols, row))) for row in rows]

    async def save_event_asset_mappings(self, external_event_id: int, mappings: list[dict]):
        for mapping in mappings:
            await self._db.execute(
                """INSERT OR REPLACE INTO event_to_asset_map
                (external_event_id, symbol, baseline_bias, needs_gemini_clarification,
                 tradable, mapping_score, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    external_event_id,
                    mapping["symbol"],
                    mapping.get("baseline_bias", "neutral"),
                    int(bool(mapping.get("needs_gemini_clarification", True))),
                    int(bool(mapping.get("tradable", False))),
                    float(mapping.get("mapping_score", 0.0)),
                    mapping.get("reason", ""),
                    time.time(),
                ),
            )
        await self._db.commit()

    async def get_event_asset_mappings(
        self,
        *,
        external_event_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query = "SELECT * FROM event_to_asset_map"
        params: list = []
        if external_event_id is not None:
            query += " WHERE external_event_id = ?"
            params.append(external_event_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def save_gemini_event_assessment(
        self,
        external_event_id: int,
        assessment: dict,
        *,
        changed_mapping: bool = False,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO gemini_event_assessments
            (external_event_id, assessment_json, changed_mapping, created_at)
            VALUES (?, ?, ?, ?)""",
            (
                external_event_id,
                json.dumps(assessment),
                int(changed_mapping),
                time.time(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_latest_gemini_event_assessments(
        self,
        *,
        external_event_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM gemini_event_assessments"
        params: list = []
        if external_event_id is not None:
            query += " WHERE external_event_id = ?"
            params.append(external_event_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items = [dict(zip(cols, row)) for row in rows]
        for item in items:
            item["assessment_json"] = json.loads(item["assessment_json"])
        return items

    async def save_runtime_state(self, key: str, value: dict):
        await self._db.execute(
            """INSERT INTO app_runtime_state (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, json.dumps(value), time.time()),
        )
        await self._db.commit()

    async def get_runtime_state(self, key: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT value_json FROM app_runtime_state WHERE key = ? LIMIT 1",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def _decode_external_event(self, row: dict) -> dict:
        row["affected_assets"] = json.loads(row.pop("affected_assets_json", "[]") or "[]")
        row["raw_payload"] = json.loads(row.pop("raw_payload_json", "{}") or "{}")
        row["actual"] = self._from_json_cell(row.get("actual"))
        row["forecast"] = self._from_json_cell(row.get("forecast"))
        row["previous"] = self._from_json_cell(row.get("previous"))
        row["usable"] = bool(row.get("usable", 0))
        return row

    def _json_cell(self, value):
        if value is None:
            return None
        return json.dumps(value)

    def _from_json_cell(self, value):
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except Exception:
            return value
