import aiosqlite
import json
import time
import logging
import sqlite3
import inspect
from typing import Awaitable, Callable, Optional

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
    signal_id INTEGER,
    ticket INTEGER,
    detail TEXT NOT NULL,
    profit REAL DEFAULT 0.0,
    profit_pct REAL DEFAULT 0.0,
    decision_reason TEXT DEFAULT '',
    gemini_summary TEXT DEFAULT '',
    meta_model_summary TEXT DEFAULT ''
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

CREATE TABLE IF NOT EXISTS trade_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE,
    signal_id INTEGER,
    timestamp_utc REAL NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT DEFAULT '',
    strategy_mode TEXT DEFAULT '',
    session TEXT DEFAULT '',
    day_of_week INTEGER DEFAULT 0,
    technical_direction TEXT NOT NULL,
    smart_agent_summary TEXT DEFAULT '',
    gemini_summary TEXT DEFAULT '',
    quality_score REAL DEFAULT 0.0,
    confidence_score REAL DEFAULT 0.0,
    trend_h1 TEXT DEFAULT '',
    trend_h4 TEXT DEFAULT '',
    stop_loss REAL DEFAULT 0.0,
    take_profit REAL DEFAULT 0.0,
    reward_risk REAL DEFAULT 0.0,
    spread_at_eval REAL DEFAULT 0.0,
    atr_regime TEXT DEFAULT '',
    support_resistance_context TEXT DEFAULT '',
    event_id TEXT DEFAULT '',
    event_type TEXT DEFAULT '',
    event_importance TEXT DEFAULT '',
    contradiction_flag INTEGER DEFAULT 0,
    risk_decision TEXT DEFAULT '',
    rejection_reasons_json TEXT DEFAULT '[]',
    executed INTEGER NOT NULL DEFAULT 0,
    execution_ticket INTEGER,
    execution_fill_price REAL,
    execution_slippage_est REAL,
    margin_snapshot_json TEXT DEFAULT '{}',
    hold_duration_minutes REAL DEFAULT 0.0,
    exit_timestamp REAL,
    exit_reason TEXT DEFAULT '',
    gross_pnl REAL DEFAULT 0.0,
    net_pnl REAL DEFAULT 0.0,
    mfe REAL DEFAULT 0.0,
    mae REAL DEFAULT 0.0,
    unrealized_peak REAL DEFAULT 0.0,
    gemini_changed_decision INTEGER DEFAULT 0,
    meta_model_changed_decision INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    signal_id INTEGER,
    schema_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES trade_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE,
    algorithm TEXT NOT NULL,
    target_definition TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    training_date REAL NOT NULL,
    data_range_start REAL,
    data_range_end REAL,
    evaluation_metrics_json TEXT DEFAULT '{}',
    walk_forward_metrics_json TEXT DEFAULT '{}',
    approval_status TEXT NOT NULL DEFAULT 'candidate',
    notes TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    version_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'training',
    metrics_json TEXT DEFAULT '{}',
    params_json TEXT DEFAULT '{}',
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    model_version_id TEXT DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    config_json TEXT DEFAULT '{}',
    metrics_json TEXT DEFAULT '{}',
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS attribution_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL UNIQUE,
    report_type TEXT NOT NULL,
    generated_at REAL NOT NULL,
    data_range_start REAL,
    data_range_end REAL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_post_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(ticket, signal_id)
);

CREATE TABLE IF NOT EXISTS app_runtime_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    user_id TEXT DEFAULT '',
    user_email TEXT DEFAULT '',
    role TEXT DEFAULT 'user',
    action TEXT NOT NULL,
    path TEXT DEFAULT '',
    method TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    details_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS user_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at REAL NOT NULL,
    approved_at REAL,
    approved_by_user_id TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_timestamp
    ON signals(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp
    ON orders(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orders_signal_id
    ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_signal_ts
    ON risk_decisions(signal_id, timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_eval_journal_symbol_timestamp
    ON evaluation_journal(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_symbol_closed_at
    ON trade_outcomes(symbol, closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_activity_symbol_timestamp
    ON ai_activity(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trade_candidates_symbol_timestamp
    ON trade_candidates(symbol, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_feature_snapshots_candidate_created
    ON feature_snapshots(candidate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_activity_timestamp
    ON user_activity(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_user_access_status
    ON user_access(status, requested_at DESC);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._cloud_log_sink = None
        self._trade_outcome_callback: Optional[Callable[[dict], Awaitable[None] | None]] = None

    def set_cloud_log_sink(self, sink):
        self._cloud_log_sink = sink

    def set_trade_outcome_callback(self, callback: Optional[Callable[[dict], Awaitable[None] | None]]):
        self._trade_outcome_callback = callback

    async def _emit_cloud_log(self, event_type: str, payload: dict):
        if self._cloud_log_sink is None:
            return
        try:
            await self._cloud_log_sink(event_type, payload)
        except Exception:
            pass

    async def initialize(self):
        self._db = await aiosqlite.connect(self.path)
        # SQLite durability/concurrency settings to reduce lock contention in
        # concurrent scanner + auto-trader write bursts.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
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
        cursor = await self._db.execute("PRAGMA table_info(ai_activity)")
        rows = await cursor.fetchall()
        if rows:
            ai_columns = {row[1] for row in rows}
            if "signal_id" not in ai_columns:
                await self._db.execute(
                    "ALTER TABLE ai_activity ADD COLUMN signal_id INTEGER"
                )
            if "profit_pct" not in ai_columns:
                await self._db.execute(
                    "ALTER TABLE ai_activity ADD COLUMN profit_pct REAL DEFAULT 0.0"
                )
            if "decision_reason" not in ai_columns:
                await self._db.execute(
                    "ALTER TABLE ai_activity ADD COLUMN decision_reason TEXT DEFAULT ''"
                )
            if "gemini_summary" not in ai_columns:
                await self._db.execute(
                    "ALTER TABLE ai_activity ADD COLUMN gemini_summary TEXT DEFAULT ''"
                )
            if "meta_model_summary" not in ai_columns:
                await self._db.execute(
                    "ALTER TABLE ai_activity ADD COLUMN meta_model_summary TEXT DEFAULT ''"
                )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_post_analysis_ticket ON trade_post_analysis(ticket)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_post_analysis_signal ON trade_post_analysis(signal_id)"
        )

    async def log_connection_event(
        self, event: str, account: int = 0, server: str = "", details: str = ""
    ):
        ts = time.time()
        await self._db.execute(
            "INSERT INTO connection_events (timestamp, event, account, server, details) VALUES (?, ?, ?, ?, ?)",
            (ts, event, account, server, details),
        )
        await self._db.commit()
        await self._emit_cloud_log(
            "connection_event",
            {
                "timestamp": ts,
                "event": event,
                "account": account,
                "server": server,
                "details": details,
            },
        )

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
        ts = time.time()
        cursor = await self._db.execute(
            """INSERT INTO signals
            (timestamp, agent_name, symbol, timeframe, action, confidence, stop_loss, take_profit, max_holding_minutes, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts,
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
        await self._emit_cloud_log(
            "signal",
            {
                "timestamp": ts,
                "agent_name": agent_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "action": action,
                "confidence": confidence,
            },
        )
        return cursor.lastrowid

    async def log_risk_decision(
        self,
        signal_id: int,
        approved: bool,
        reason: str,
        adjusted_volume: float = 0.0,
    ):
        ts = time.time()
        await self._db.execute(
            "INSERT INTO risk_decisions (timestamp, signal_id, approved, reason, adjusted_volume) VALUES (?, ?, ?, ?, ?)",
            (ts, signal_id, int(approved), reason, adjusted_volume),
        )
        await self._db.commit()
        await self._emit_cloud_log(
            "risk_decision",
            {
                "timestamp": ts,
                "signal_id": signal_id,
                "approved": bool(approved),
                "reason": reason,
                "adjusted_volume": adjusted_volume,
            },
        )

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
        ts = time.time()
        payload = (
            ts,
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
        )
        try:
            await self._db.execute(
                """INSERT INTO orders
                (timestamp, signal_id, symbol, action, volume, price, stop_loss, take_profit, ticket, retcode, retcode_desc, comment, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                payload,
            )
        except sqlite3.IntegrityError as exc:
            # Keep trading flow alive if a stale/missing signal FK appears under
            # concurrent scanner activity; fall back to nullable signal_id.
            logger.warning(
                "log_order FK failed for signal_id=%s symbol=%s, retrying without signal link: %s",
                signal_id,
                symbol,
                exc,
            )
            await self._db.execute(
                """INSERT INTO orders
                (timestamp, signal_id, symbol, action, volume, price, stop_loss, take_profit, ticket, retcode, retcode_desc, comment, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    None,
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
        await self._emit_cloud_log(
            "order",
            {
                "timestamp": ts,
                "signal_id": signal_id,
                "symbol": symbol,
                "action": action,
                "volume": volume,
                "price": price,
                "ticket": ticket,
                "retcode": retcode,
                "retcode_desc": retcode_desc,
                "success": bool(success),
            },
        )

    async def log_position_change(
        self, ticket: int, event: str, symbol: str = "", details: str = ""
    ):
        ts = time.time()
        await self._db.execute(
            "INSERT INTO position_changes (timestamp, ticket, event, symbol, details) VALUES (?, ?, ?, ?, ?)",
            (ts, ticket, event, symbol, details),
        )
        await self._db.commit()
        await self._emit_cloud_log(
            "position_change",
            {
                "timestamp": ts,
                "ticket": ticket,
                "event": event,
                "symbol": symbol,
                "details": details,
            },
        )

    async def log_error(self, source: str, message: str, details: str = ""):
        ts = time.time()
        await self._db.execute(
            "INSERT INTO errors (timestamp, source, message, details) VALUES (?, ?, ?, ?)",
            (ts, source, message, details),
        )
        await self._db.commit()
        await self._emit_cloud_log(
            "error",
            {
                "timestamp": ts,
                "source": source,
                "message": message,
                "details": details,
            },
        )

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
               LEFT JOIN risk_decisions r
                    ON r.id = (
                        SELECT r2.id
                        FROM risk_decisions r2
                        WHERE r2.signal_id = s.id
                        ORDER BY r2.timestamp DESC, r2.id DESC
                        LIMIT 1
                    )
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
        self,
        action: str,
        symbol: str,
        ticket: int,
        detail: str,
        profit: float = 0.0,
        *,
        signal_id: Optional[int] = None,
        profit_pct: Optional[float] = None,
        decision_reason: str = "",
        gemini_summary: str = "",
        meta_model_summary: str = "",
    ):
        ts = time.time()
        await self._db.execute(
            """INSERT INTO ai_activity
               (timestamp, action, symbol, signal_id, ticket, detail, profit, profit_pct, decision_reason, gemini_summary, meta_model_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts,
                action,
                symbol,
                signal_id,
                ticket,
                detail,
                profit,
                float(profit_pct or 0.0),
                decision_reason or "",
                gemini_summary or "",
                meta_model_summary or "",
            ),
        )
        await self._db.commit()
        await self._emit_cloud_log(
            "ai_activity",
            {
                "timestamp": ts,
                "action": action,
                "symbol": symbol,
                "signal_id": signal_id,
                "ticket": ticket,
                "detail": detail,
                "profit": profit,
                "profit_pct": float(profit_pct or 0.0),
                "decision_reason": decision_reason or "",
                "gemini_summary": gemini_summary or "",
                "meta_model_summary": meta_model_summary or "",
            },
        )

    async def get_ai_activity(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM ai_activity ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def get_order_with_context_by_ticket(self, ticket: int) -> Optional[dict]:
        cursor = await self._db.execute(
            """SELECT o.*, s.agent_name, s.confidence, s.reason as signal_reason,
                      r.approved, r.reason as risk_reason
               FROM orders o
               LEFT JOIN signals s ON o.signal_id = s.id
               LEFT JOIN risk_decisions r
                    ON r.id = (
                        SELECT r2.id
                        FROM risk_decisions r2
                        WHERE r2.signal_id = s.id
                        ORDER BY r2.timestamp DESC, r2.id DESC
                        LIMIT 1
                    )
               WHERE o.ticket = ?
               ORDER BY o.timestamp DESC
               LIMIT 1""",
            (ticket,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def get_trade_candidates_by_signal_ids(self, signal_ids: list[int]) -> dict[int, dict]:
        ids = [int(sid) for sid in signal_ids if isinstance(sid, int)]
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        cursor = await self._db.execute(
            f"""SELECT *
                FROM trade_candidates
                WHERE signal_id IN ({placeholders})
                ORDER BY timestamp_utc DESC""",
            tuple(ids),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        mapped: dict[int, dict] = {}
        for row in rows:
            item = dict(zip(cols, row))
            sid = item.get("signal_id")
            if isinstance(sid, int) and sid not in mapped:
                mapped[sid] = item
        return mapped

    async def get_evaluation_journal_by_signal_ids(self, signal_ids: list[int]) -> dict[int, dict]:
        ids = [int(sid) for sid in signal_ids if isinstance(sid, int)]
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        cursor = await self._db.execute(
            f"""SELECT signal_id, timestamp, gemini_assessment, execution_decision, raw_technical_signal, executable_signal
                FROM evaluation_journal
                WHERE signal_id IN ({placeholders})
                ORDER BY timestamp DESC""",
            tuple(ids),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        mapped: dict[int, dict] = {}
        for row in rows:
            item = dict(zip(cols, row))
            sid = item.get("signal_id")
            if isinstance(sid, int) and sid not in mapped:
                mapped[sid] = item
        return mapped

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

    async def log_trade_candidate(
        self,
        *,
        candidate_id: str,
        signal_id: Optional[int],
        symbol: str,
        asset_class: str,
        strategy_mode: str,
        session: str,
        day_of_week: int,
        technical_direction: str,
        smart_agent_summary: str,
        gemini_summary: str,
        quality_score: float,
        confidence_score: float,
        trend_h1: str,
        trend_h4: str,
        stop_loss: float,
        take_profit: float,
        reward_risk: float,
        spread_at_eval: float,
        atr_regime: str,
        support_resistance_context: str,
        event_id: str,
        event_type: str,
        event_importance: str,
        contradiction_flag: bool,
        risk_decision: str,
        rejection_reasons: list[str],
        executed: bool,
        gemini_changed_decision: bool,
        meta_model_changed_decision: bool = False,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT OR REPLACE INTO trade_candidates
            (candidate_id, signal_id, timestamp_utc, symbol, asset_class, strategy_mode, session, day_of_week,
             technical_direction, smart_agent_summary, gemini_summary, quality_score, confidence_score,
             trend_h1, trend_h4, stop_loss, take_profit, reward_risk, spread_at_eval, atr_regime,
             support_resistance_context, event_id, event_type, event_importance, contradiction_flag,
             risk_decision, rejection_reasons_json, executed, gemini_changed_decision, meta_model_changed_decision, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate_id,
                signal_id,
                time.time(),
                symbol,
                asset_class,
                strategy_mode,
                session,
                int(day_of_week),
                technical_direction,
                smart_agent_summary,
                gemini_summary,
                float(quality_score or 0.0),
                float(confidence_score or 0.0),
                trend_h1,
                trend_h4,
                float(stop_loss or 0.0),
                float(take_profit or 0.0),
                float(reward_risk or 0.0),
                float(spread_at_eval or 0.0),
                atr_regime,
                support_resistance_context,
                event_id,
                event_type,
                event_importance,
                int(bool(contradiction_flag)),
                risk_decision,
                json.dumps(rejection_reasons or []),
                int(bool(executed)),
                int(bool(gemini_changed_decision)),
                int(bool(meta_model_changed_decision)),
                time.time(),
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def log_feature_snapshot(
        self,
        *,
        candidate_id: str,
        signal_id: Optional[int],
        schema_version: str,
        features: dict,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO feature_snapshots
            (candidate_id, signal_id, schema_version, features_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                candidate_id,
                signal_id,
                schema_version,
                json.dumps(features or {}),
                time.time(),
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def mark_trade_candidate_execution(
        self,
        *,
        signal_id: Optional[int],
        executed: bool,
        ticket: Optional[int] = None,
        fill_price: Optional[float] = None,
        slippage_estimate: Optional[float] = None,
        margin_snapshot: Optional[dict] = None,
    ):
        if signal_id is None:
            return
        await self._db.execute(
            """UPDATE trade_candidates
               SET executed = ?, execution_ticket = ?, execution_fill_price = ?, execution_slippage_est = ?,
                   margin_snapshot_json = COALESCE(?, margin_snapshot_json)
               WHERE signal_id = ?""",
            (
                int(bool(executed)),
                ticket,
                fill_price,
                slippage_estimate,
                json.dumps(margin_snapshot) if margin_snapshot is not None else None,
                signal_id,
            ),
        )
        await self._db.commit()

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
        await self._db.execute(
            """UPDATE trade_candidates
               SET hold_duration_minutes = ?,
                   exit_timestamp = ?,
                   exit_reason = ?,
                   gross_pnl = ?,
                   net_pnl = ?,
                   mfe = COALESCE(mfe, 0.0),
                   mae = COALESCE(mae, 0.0),
                   unrealized_peak = COALESCE(unrealized_peak, 0.0)
               WHERE (signal_id = ? AND ? IS NOT NULL)
                  OR (execution_ticket = ? AND ? IS NOT NULL)""",
            (
                float(holding_minutes or 0.0),
                time.time(),
                exit_reason,
                float(profit or 0.0),
                float(profit or 0.0),
                signal_id,
                signal_id,
                ticket,
                ticket,
            ),
        )
        if ticket is not None:
            await self.update_position_management_plan_status(ticket, "closed")
        await self._db.commit()
        await self._emit_cloud_log(
            "trade_outcome",
            {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
                "action": action,
                "confidence": float(confidence or 0.0),
                "profit": float(profit or 0.0),
                "exit_reason": exit_reason,
                "holding_minutes": float(holding_minutes or 0.0),
                "symbol_category": symbol_category,
                "strategy": strategy,
                "planned_hold_minutes": int(planned_hold_minutes or 0),
                "outcome_json": outcome_json or {},
            },
        )
        if self._trade_outcome_callback is not None:
            payload = {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
                "action": action,
                "profit": float(profit or 0.0),
                "exit_reason": exit_reason,
                "holding_minutes": float(holding_minutes or 0.0),
                "symbol_category": symbol_category,
                "strategy": strategy,
            }
            try:
                maybe = self._trade_outcome_callback(payload)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.exception("Trade outcome callback failed")

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

    async def get_trade_outcome_by_signal_id(self, signal_id: int) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM trade_outcomes WHERE signal_id = ? ORDER BY closed_at DESC LIMIT 1",
            (signal_id,),
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

    async def count_trade_outcomes(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM trade_outcomes")
        row = await cursor.fetchone()
        return int(row[0] or 0)

    async def save_trade_post_analysis(
        self,
        *,
        ticket: Optional[int],
        signal_id: Optional[int],
        symbol: str,
        analysis: dict,
    ) -> int:
        now = time.time()
        cursor = await self._db.execute(
            """INSERT OR REPLACE INTO trade_post_analysis
               (ticket, signal_id, symbol, analysis_json, created_at, updated_at)
               VALUES (
                 ?, ?, ?, ?,
                 COALESCE(
                   (SELECT created_at FROM trade_post_analysis WHERE ticket IS ? AND signal_id IS ?),
                   ?
                 ),
                 ?
               )""",
            (
                ticket,
                signal_id,
                symbol,
                json.dumps(analysis or {}),
                ticket,
                signal_id,
                now,
                now,
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid or 0)

    async def get_trade_post_analysis(
        self,
        *,
        ticket: Optional[int] = None,
        signal_id: Optional[int] = None,
    ) -> Optional[dict]:
        if ticket is None and signal_id is None:
            return None
        query = """SELECT *
                   FROM trade_post_analysis
                   WHERE 1=1"""
        params: list = []
        if ticket is not None:
            query += " AND ticket = ?"
            params.append(ticket)
        if signal_id is not None:
            query += " AND signal_id = ?"
            params.append(signal_id)
        query += " ORDER BY updated_at DESC LIMIT 1"
        cursor = await self._db.execute(query, tuple(params))
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        item = dict(zip(cols, row))
        raw = item.get("analysis_json")
        if isinstance(raw, str) and raw.strip():
            try:
                item["analysis_json"] = json.loads(raw)
            except Exception:
                item["analysis_json"] = {}
        else:
            item["analysis_json"] = {}
        return item

    async def get_trade_post_analysis_by_signal_ids(self, signal_ids: list[int]) -> dict[int, dict]:
        ids = [int(sid) for sid in signal_ids if isinstance(sid, int)]
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        cursor = await self._db.execute(
            f"""SELECT *
                FROM trade_post_analysis
                WHERE signal_id IN ({placeholders})
                ORDER BY updated_at DESC""",
            tuple(ids),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        mapped: dict[int, dict] = {}
        for row in rows:
            item = dict(zip(cols, row))
            sid = item.get("signal_id")
            if not isinstance(sid, int) or sid in mapped:
                continue
            raw = item.get("analysis_json")
            if isinstance(raw, str) and raw.strip():
                try:
                    item["analysis_json"] = json.loads(raw)
                except Exception:
                    item["analysis_json"] = {}
            else:
                item["analysis_json"] = {}
            mapped[sid] = item
        return mapped

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

    async def log_user_activity(
        self,
        *,
        user_id: str,
        user_email: str,
        role: str,
        action: str,
        path: str = "",
        method: str = "",
        status_code: int = 0,
        details: Optional[dict] = None,
    ):
        await self._db.execute(
            """INSERT INTO user_activity
            (timestamp, user_id, user_email, role, action, path, method, status_code, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                user_id or "",
                user_email or "",
                role or "user",
                action,
                path,
                method,
                int(status_code or 0),
                json.dumps(details or {}),
            ),
        )
        await self._db.commit()

    async def get_user_activity(self, limit: int = 200) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM user_activity ORDER BY timestamp DESC LIMIT ?",
            (max(1, min(limit, 2000)),),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items: list[dict] = []
        for row in rows:
            item = dict(zip(cols, row))
            try:
                item["details_json"] = json.loads(item.get("details_json") or "{}")
            except Exception:
                item["details_json"] = {}
            items.append(item)
        return items

    async def upsert_user_access(
        self,
        *,
        user_id: str,
        email: str,
        status: str = "pending",
        approved_by_user_id: str = "",
        notes: str = "",
    ):
        now = time.time()
        approved_at = now if status == "approved" else None
        await self._db.execute(
            """INSERT INTO user_access
            (user_id, email, status, requested_at, approved_at, approved_by_user_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                email=excluded.email,
                status=excluded.status,
                approved_at=excluded.approved_at,
                approved_by_user_id=excluded.approved_by_user_id,
                notes=excluded.notes""",
            (
                user_id,
                email,
                status,
                now,
                approved_at,
                approved_by_user_id,
                notes,
            ),
        )
        await self._db.commit()

    async def set_user_access_status(
        self,
        *,
        user_id: str,
        status: str,
        approved_by_user_id: str = "",
        notes: str = "",
    ):
        approved_at = time.time() if status == "approved" else None
        await self._db.execute(
            """UPDATE user_access
            SET status = ?, approved_at = ?, approved_by_user_id = ?, notes = ?
            WHERE user_id = ?""",
            (status, approved_at, approved_by_user_id, notes, user_id),
        )
        await self._db.commit()

    async def get_user_access(self, *, user_id: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM user_access WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def list_user_access_requests(self, *, status: Optional[str] = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM user_access"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY requested_at DESC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        cursor = await self._db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def log_model_run(
        self,
        *,
        run_id: str,
        version_id: Optional[str],
        status: str = "training",
        params: Optional[dict] = None,
        metrics: Optional[dict] = None,
        notes: str = "",
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO model_runs
            (run_id, version_id, started_at, finished_at, status, metrics_json, params_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                version_id,
                float(started_at or time.time()),
                finished_at,
                status,
                json.dumps(metrics or {}),
                json.dumps(params or {}),
                notes,
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def update_model_run(
        self,
        *,
        run_id: str,
        status: Optional[str] = None,
        metrics: Optional[dict] = None,
        notes: Optional[str] = None,
        finished_at: Optional[float] = None,
        version_id: Optional[str] = None,
    ):
        updates: list[str] = []
        params: list = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if metrics is not None:
            updates.append("metrics_json = ?")
            params.append(json.dumps(metrics))
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if finished_at is not None:
            updates.append("finished_at = ?")
            params.append(float(finished_at))
        if version_id is not None:
            updates.append("version_id = ?")
            params.append(version_id)
        if not updates:
            return
        params.append(run_id)
        await self._db.execute(
            f"UPDATE model_runs SET {', '.join(updates)} WHERE run_id = ?",
            tuple(params),
        )
        await self._db.commit()

    async def register_model_version(
        self,
        *,
        version_id: str,
        algorithm: str,
        target_definition: str,
        feature_schema_version: str,
        training_date: float,
        data_range_start: Optional[float] = None,
        data_range_end: Optional[float] = None,
        evaluation_metrics: Optional[dict] = None,
        walk_forward_metrics: Optional[dict] = None,
        approval_status: str = "candidate",
        notes: str = "",
    ) -> int:
        now = time.time()
        cursor = await self._db.execute(
            """INSERT INTO model_versions
            (version_id, algorithm, target_definition, feature_schema_version, training_date,
             data_range_start, data_range_end, evaluation_metrics_json, walk_forward_metrics_json,
             approval_status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                algorithm,
                target_definition,
                feature_schema_version,
                float(training_date),
                data_range_start,
                data_range_end,
                json.dumps(evaluation_metrics or {}),
                json.dumps(walk_forward_metrics or {}),
                approval_status,
                notes,
                now,
                now,
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def update_model_version(
        self,
        *,
        version_id: str,
        evaluation_metrics: Optional[dict] = None,
        walk_forward_metrics: Optional[dict] = None,
        approval_status: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        updates: list[str] = ["updated_at = ?"]
        params: list = [time.time()]
        if evaluation_metrics is not None:
            updates.append("evaluation_metrics_json = ?")
            params.append(json.dumps(evaluation_metrics))
        if walk_forward_metrics is not None:
            updates.append("walk_forward_metrics_json = ?")
            params.append(json.dumps(walk_forward_metrics))
        if approval_status is not None:
            updates.append("approval_status = ?")
            params.append(approval_status)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        params.append(version_id)
        await self._db.execute(
            f"UPDATE model_versions SET {', '.join(updates)} WHERE version_id = ?",
            tuple(params),
        )
        await self._db.commit()

    async def get_model_version(self, version_id: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM model_versions WHERE version_id = ? LIMIT 1",
            (version_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        item = dict(zip(cols, row))
        item["evaluation_metrics_json"] = json.loads(item.get("evaluation_metrics_json") or "{}")
        item["walk_forward_metrics_json"] = json.loads(item.get("walk_forward_metrics_json") or "{}")
        return item

    async def list_model_versions(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM model_versions ORDER BY training_date DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items = []
        for row in rows:
            item = dict(zip(cols, row))
            item["evaluation_metrics_json"] = json.loads(item.get("evaluation_metrics_json") or "{}")
            item["walk_forward_metrics_json"] = json.loads(item.get("walk_forward_metrics_json") or "{}")
            items.append(item)
        return items

    async def get_latest_approved_model_version(self) -> Optional[dict]:
        cursor = await self._db.execute(
            """SELECT * FROM model_versions
               WHERE approval_status = 'approved'
               ORDER BY training_date DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        item = dict(zip(cols, row))
        item["evaluation_metrics_json"] = json.loads(item.get("evaluation_metrics_json") or "{}")
        item["walk_forward_metrics_json"] = json.loads(item.get("walk_forward_metrics_json") or "{}")
        return item

    async def log_replay_run(
        self,
        *,
        run_id: str,
        model_version_id: str = "",
        config: Optional[dict] = None,
        status: str = "running",
        notes: str = "",
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
        metrics: Optional[dict] = None,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO replay_runs
            (run_id, model_version_id, started_at, finished_at, status, config_json, metrics_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                model_version_id,
                float(started_at or time.time()),
                finished_at,
                status,
                json.dumps(config or {}),
                json.dumps(metrics or {}),
                notes,
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def update_replay_run(
        self,
        *,
        run_id: str,
        status: Optional[str] = None,
        config: Optional[dict] = None,
        metrics: Optional[dict] = None,
        notes: Optional[str] = None,
        finished_at: Optional[float] = None,
        model_version_id: Optional[str] = None,
    ):
        updates: list[str] = []
        params: list = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if config is not None:
            updates.append("config_json = ?")
            params.append(json.dumps(config))
        if metrics is not None:
            updates.append("metrics_json = ?")
            params.append(json.dumps(metrics))
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if finished_at is not None:
            updates.append("finished_at = ?")
            params.append(float(finished_at))
        if model_version_id is not None:
            updates.append("model_version_id = ?")
            params.append(model_version_id)
        if not updates:
            return
        params.append(run_id)
        await self._db.execute(
            f"UPDATE replay_runs SET {', '.join(updates)} WHERE run_id = ?",
            tuple(params),
        )
        await self._db.commit()

    async def list_model_runs(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM model_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items = []
        for row in rows:
            item = dict(zip(cols, row))
            item["metrics_json"] = json.loads(item.get("metrics_json") or "{}")
            item["params_json"] = json.loads(item.get("params_json") or "{}")
            items.append(item)
        return items

    async def get_latest_model_run(self) -> Optional[dict]:
        runs = await self.list_model_runs(limit=1)
        return runs[0] if runs else None

    async def list_replay_runs(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items = []
        for row in rows:
            item = dict(zip(cols, row))
            item["config_json"] = json.loads(item.get("config_json") or "{}")
            item["metrics_json"] = json.loads(item.get("metrics_json") or "{}")
            items.append(item)
        return items

    async def get_latest_replay_run(self, *, notes_like: Optional[str] = None) -> Optional[dict]:
        if notes_like:
            cursor = await self._db.execute(
                "SELECT * FROM replay_runs WHERE notes LIKE ? ORDER BY started_at DESC LIMIT 1",
                (notes_like,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cursor.description]
            item = dict(zip(cols, row))
            item["config_json"] = json.loads(item.get("config_json") or "{}")
            item["metrics_json"] = json.loads(item.get("metrics_json") or "{}")
            return item
        runs = await self.list_replay_runs(limit=1)
        return runs[0] if runs else None

    async def save_attribution_report(
        self,
        *,
        report_id: str,
        report_type: str,
        data_range_start: Optional[float],
        data_range_end: Optional[float],
        report: dict,
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO attribution_reports
            (report_id, report_type, generated_at, data_range_start, data_range_end, report_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                report_id,
                report_type,
                time.time(),
                data_range_start,
                data_range_end,
                json.dumps(report or {}),
            ),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def list_attribution_reports(self, limit: int = 20) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM attribution_reports ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        items = []
        for row in rows:
            item = dict(zip(cols, row))
            item["report_json"] = json.loads(item.get("report_json") or "{}")
            items.append(item)
        return items

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
