from fastapi import APIRouter, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, Literal, Any
import logging
import time
import asyncio
import json
import re
import math
import sqlite3
import os
import secrets
import webbrowser
import requests
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from adapters.benzinga_adapter import BenzingaAdapter
from mt5.connector import MT5Connector, ConnectionParams, TerminalSessionParams
from ibkr.connector import IBKRConnector, IBKRConnectionParams
from ibkr.adapters import IBKRConnectionAdapter, IBKRExecutionAdapter, IBKRMarketDataAdapter
from tradestation.connector import TradeStationConnector, TradeStationConnectionParams
from tradestation.market_data import TradeStationMarketDataService
from tradestation.execution import TradeStationExecutionEngine
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.mock_agent import MockAgent
from agent.sma_crossover_agent import SMACrossoverAgent
from agent.smart_agent import SmartAgent
from agent.interface import AgentInput
from agent.gemini_agent import GeminiAgent
from risk.rules import RiskEngine, UserPolicySettings
from storage.db import Database
from config import config
from llm.gemini_event_classifier import GeminiEventClassifier
from services.credential_store import CredentialVault
from services.asset_mapping_service import AssetMappingService
from services.cloud_secret_service import CloudSecretService
from services.secret_store import SecretStore, redact_secret
from services.supabase_admin_service import SupabaseAdminService
from services.event_ingestion_service import EventIngestionService
from services.event_normalization_service import EventNormalizationService
from services.execution_service import ExecutionService
from services.kill_switch_service import KillSwitchService
from services.broker_symbol_validator import BrokerSymbolValidator
from services.reconciliation_service import ReconciliationService
from services.gemini_news_analysis_service import GeminiNewsAnalysisService
from services.gemini_strategy_compiler_service import GeminiStrategyCompilerService
from services.gemini_adapter import GeminiAdapter
from services.gemini_model_fallback import (
    build_model_candidates,
    build_structured_output_model_candidates,
    cooldown_remaining_seconds,
    is_rate_limit_error,
    mark_model_cooldown,
    next_available_model,
)
from services.api_key_health_service import ApiKeyHealthConfig, ApiKeyHealthService
from services.news_ingestion_service import NewsIngestionService
from services.risk_service import RiskService
from services.signal_pipeline_service import SignalPipelineService
from services.symbol_universe_service import SymbolUniverseService
from services.task_orchestrator import TaskOrchestrator
from services.analytics_service import AnalyticsService
from services.replay_service import ReplayService
from services.research_cycle_service import ResearchCycleService
from services.trade_decision_service import TradeDecisionService
from domain.models import MarketContext, TechnicalSignal

logger = logging.getLogger(__name__)
_gemini_quota_cooldown_until = 0.0
_gemini_route_model_cooldowns: dict[str, float] = {}
_last_scan_gemini_enrich_at = 0.0
_last_auto_post_analysis_at = 0.0
_scan_confidence_cache: dict[str, tuple[float, float, str]] = {}
_SCAN_CONFIDENCE_TTL_SECONDS = 600.0
_available_symbols_cache: dict[tuple[str, bool, str, bool, bool], tuple[float, dict]] = {}
_connected_trade_history_cache: dict[tuple[str, int, int], tuple[float, dict]] = {}
_trade_history_realized_cache: dict[tuple[int, ...], tuple[float, dict[int, float]]] = {}
_trade_history_reconcile_cache: tuple[float, int] = (0.0, 0)
_positions_payload_cache: dict[tuple[str, str, bool], tuple[float, list[dict]]] = {}
_POSITIONS_CACHE_TTL_SECONDS = 30.0
_POSITIONS_FETCH_TIMEOUT_SECONDS = 2.0
_POSITIONS_DB_LOOKUP_TIMEOUT_SECONDS = 0.35
_market_tick_payload_cache: dict[str, tuple[float, dict]] = {}
_market_tick_symbol_resolution_cache: dict[str, str] = {}
_market_tick_refreshing_keys: set[str] = set()
_market_tick_refresh_last_started: dict[str, float] = {}
_MARKET_TICK_STALE_RETURN_SECONDS = 1.5
_MARKET_TICK_REFRESH_AFTER_SECONDS = 0.25
_MARKET_TICK_REFRESH_MIN_INTERVAL_SECONDS = 0.5


def _db_path() -> str:
    return str(getattr(db, "path", "") or config.DB_PATH)


def _read_trade_outcomes_direct(limit: int) -> list[dict]:
    """Small read-only SQLite fallback for UI history when the async DB queue is busy."""
    path = _db_path()
    if not path:
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """SELECT *
               FROM trade_outcomes
               ORDER BY closed_at DESC
               LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _read_trade_target_context_direct(tickets: list[int]) -> dict[int, dict]:
    ids = [int(ticket) for ticket in tickets if isinstance(ticket, int)]
    if not ids:
        return {}
    path = _db_path()
    if not path:
        return {}
    placeholders = ",".join("?" for _ in ids)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    mapped: dict[int, dict] = {
        ticket: {"order": None, "management_plan": None, "outcome": None, "post_analysis": None}
        for ticket in ids
    }
    try:
        conn.execute("PRAGMA query_only=ON")
        for row in conn.execute(
            f"""SELECT * FROM orders
                WHERE ticket IN ({placeholders}) AND success = 1
                ORDER BY timestamp DESC, id DESC""",
            tuple(ids),
        ).fetchall():
            item = dict(row)
            ticket = item.get("ticket")
            if not isinstance(ticket, int):
                continue
            if mapped.setdefault(ticket, {}).get("order") is None:
                mapped[ticket]["order"] = item
        for row in conn.execute(
            f"SELECT * FROM position_management_plans WHERE ticket IN ({placeholders})",
            tuple(ids),
        ).fetchall():
            item = dict(row)
            ticket = item.get("ticket")
            if not isinstance(ticket, int):
                continue
            item["plan_json"] = _as_dict_json(item.get("plan_json"))
            mapped.setdefault(ticket, {})["management_plan"] = item
        for row in conn.execute(
            f"""SELECT * FROM trade_outcomes
                WHERE ticket IN ({placeholders})
                ORDER BY closed_at DESC, id DESC""",
            tuple(ids),
        ).fetchall():
            item = dict(row)
            ticket = item.get("ticket")
            if not isinstance(ticket, int):
                continue
            item["outcome_json"] = _as_dict_json(item.get("outcome_json"))
            if mapped.setdefault(ticket, {}).get("outcome") is None:
                mapped[ticket]["outcome"] = item
        for row in conn.execute(
            f"""SELECT * FROM trade_post_analysis
                WHERE ticket IN ({placeholders})
                ORDER BY updated_at DESC, id DESC""",
            tuple(ids),
        ).fetchall():
            item = dict(row)
            ticket = item.get("ticket")
            if not isinstance(ticket, int):
                continue
            item["analysis_json"] = _as_dict_json(item.get("analysis_json"))
            if mapped.setdefault(ticket, {}).get("post_analysis") is None:
                mapped[ticket]["post_analysis"] = item
        return mapped
    finally:
        conn.close()


def _read_recent_order_context_direct(limit: int = 500) -> tuple[list[dict], dict[int, dict]]:
    path = _db_path()
    if not path:
        return [], {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """SELECT *
               FROM orders
               WHERE success = 1
               ORDER BY timestamp DESC, id DESC
               LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        orders = [dict(row) for row in rows]
    finally:
        conn.close()
    tickets = [int(row.get("ticket")) for row in orders if isinstance(row.get("ticket"), int)]
    return orders, _read_trade_target_context_direct(tickets)


def _read_recent_orders_direct(limit: int = 120) -> list[dict]:
    path = _db_path()
    if not path:
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """SELECT *
               FROM orders
               WHERE success = 1
               ORDER BY timestamp DESC, id DESC
               LIMIT ?""",
            (max(1, min(int(limit), 250)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _count_trade_outcomes_direct() -> int:
    path = _db_path()
    if not path:
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()
        return int((row or [0])[0] or 0)
    finally:
        conn.close()


def _as_dict_json(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_positive_float(value) -> Optional[float]:
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _target_pair_matches_side(action: str, entry_price: float, stop_loss: float, take_profit: float) -> bool:
    if entry_price <= 0:
        return True
    side = str(action or "").upper()
    if side == "BUY":
        return stop_loss < entry_price < take_profit
    if side == "SELL":
        return take_profit < entry_price < stop_loss
    return True


def _final_managed_targets_from_context(context: dict | None) -> tuple[Optional[float], Optional[float], str]:
    context = context or {}
    outcome = context.get("outcome") or {}
    outcome_json = _as_dict_json(outcome.get("outcome_json"))
    sl = _safe_positive_float(outcome_json.get("stop_loss"))
    tp = _safe_positive_float(outcome_json.get("take_profit"))
    if sl is not None and tp is not None:
        return sl, tp, "closed_outcome"

    plan = context.get("management_plan") or {}
    plan_json = _as_dict_json(plan.get("plan_json"))
    metadata = _as_dict_json(plan_json.get("metadata"))
    sl = (
        _safe_positive_float(metadata.get("manual_stop_loss"))
        or _safe_positive_float(plan_json.get("stop_loss"))
        or _safe_positive_float(metadata.get("stop_loss"))
    )
    tp = (
        _safe_positive_float(metadata.get("manual_take_profit"))
        or _safe_positive_float(plan_json.get("take_profit"))
        or _safe_positive_float(metadata.get("take_profit"))
    )
    if sl is not None and tp is not None:
        return sl, tp, "management_plan"
    return None, None, ""


def _apply_final_managed_targets_to_trade(trade: dict, context: dict | None) -> bool:
    sl, tp, source = _final_managed_targets_from_context(context)
    if sl is None or tp is None:
        return False
    entry = _safe_positive_float(trade.get("entry_price")) or 0.0
    if not _target_pair_matches_side(str(trade.get("action") or ""), entry, sl, tp):
        return False

    usd_per_price = None
    outcome = (context or {}).get("outcome") or {}
    outcome_json = _as_dict_json(outcome.get("outcome_json"))
    try:
        outcome_profit = _safe_positive_float(abs(float(outcome.get("profit") or 0.0))) if outcome.get("profit") is not None else None
    except Exception:
        outcome_profit = None
    close_price = _safe_positive_float(outcome_json.get("close_price"))
    if entry > 0 and close_price is not None and outcome_profit is not None:
        hit_distance = abs(entry - close_price)
        target_tolerance = max(entry * 0.0002, 0.00001)
        if hit_distance > 0 and (
            abs(close_price - sl) <= target_tolerance
            or abs(close_price - tp) <= target_tolerance
        ):
            usd_per_price = outcome_profit / hit_distance

    old_sl = _safe_positive_float(trade.get("stop_loss"))
    old_tp = _safe_positive_float(trade.get("take_profit"))
    for old_target, amount_key in ((old_sl, "sl_amount_usd"), (old_tp, "tp_amount_usd")):
        if usd_per_price is not None:
            break
        amount = _safe_positive_float(trade.get(amount_key))
        if entry > 0 and old_target is not None and amount is not None:
            distance = abs(entry - old_target)
            if distance > 0:
                usd_per_price = amount / distance
                break

    trade["stop_loss"] = sl
    trade["take_profit"] = tp
    trade["target_source"] = source

    volume = _safe_positive_float(trade.get("volume"))
    if usd_per_price is None:
        contract_size = _safe_positive_float(trade.get("contract_size"))
        if volume is not None and contract_size is not None:
            usd_per_price = volume * contract_size

    if entry > 0 and usd_per_price is not None:
        sl_amount = abs(entry - sl) * usd_per_price
        tp_amount = abs(tp - entry) * usd_per_price
        trade["sl_amount_usd"] = sl_amount
        trade["tp_amount_usd"] = tp_amount
        started = _safe_positive_float(trade.get("started_with_usd"))
        if started is not None:
            trade["sl_pct_of_start"] = (sl_amount / started) * 100.0
            trade["tp_pct_of_start"] = (tp_amount / started) * 100.0
    return True


def _extract_linktrade_comment_amount(comment: str | None, key: str) -> float | None:
    """Extract compact LinkTrade order comment amounts such as TA4525|SLA814|TPA1810."""
    key_text = str(key or "").strip()
    if not key_text:
        return None
    pattern = rf"(?:^|[^A-Z0-9]){re.escape(key_text)}(\d+(?:\.\d+)?)"
    match = re.search(pattern, comment or "", re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except Exception:
        return None
    return value if value > 0 else None


def _apply_order_comment_target_amounts_to_trade(trade: dict, context: dict | None) -> bool:
    """Use app order comment money budgets for app-opened history display."""
    order = (context or {}).get("order") or {}
    comment = str(order.get("comment") or trade.get("comment") or "").strip()
    if not comment:
        return False
    if str(trade.get("origin") or "").lower() != "app_opened" and not order:
        return False

    started = _extract_linktrade_comment_amount(comment, "TA")
    sl_amount = _extract_linktrade_comment_amount(comment, "SLA")
    tp_amount = _extract_linktrade_comment_amount(comment, "TPA")
    if started is None and sl_amount is None and tp_amount is None:
        return False

    capped_legacy = False
    # History can contain trades opened under a different risk mode than the
    # current UI setting. Use the absolute aggressive envelope as a legacy
    # sanity ceiling so valid old aggressive comments remain intact, while
    # impossible values such as TA944/SLA821 are still flagged.
    cap_sl_pct, cap_tp_pct = (0.18, 0.40)
    cap_started = started or _safe_positive_float(trade.get("started_with_usd"))
    if cap_started is not None and cap_started > 0:
        max_sl = cap_started * cap_sl_pct
        max_tp = cap_started * cap_tp_pct
        min_rr = max(1.2, float(getattr(risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
        if sl_amount is not None and sl_amount > max_sl:
            sl_amount = max_sl
            capped_legacy = True
        if tp_amount is not None and tp_amount > max_tp:
            tp_amount = max(max_tp, float(sl_amount or 0.0) * min_rr)
            capped_legacy = True

    changed = False
    if started is not None:
        trade["started_with_usd"] = round(started, 2)
        trade["started_with_source"] = "order_comment"
        changed = True
    effective_started = _safe_positive_float(trade.get("started_with_usd"))
    if sl_amount is not None:
        trade["sl_amount_usd"] = round(sl_amount, 2)
        if effective_started is not None:
            trade["sl_pct_of_start"] = (sl_amount / effective_started) * 100.0
        changed = True
    if tp_amount is not None:
        trade["tp_amount_usd"] = round(tp_amount, 2)
        if effective_started is not None:
            trade["tp_pct_of_start"] = (tp_amount / effective_started) * 100.0
        changed = True
    if changed and not trade.get("target_source"):
        trade["target_source"] = "order_comment"
    if capped_legacy:
        trade["target_amount_source"] = "order_comment_capped"
        trade["target_warning"] = (
            "Legacy app order comment had oversized SL/TP money targets; displayed amounts are capped to the current risk envelope."
        )
    return changed


def _sanitize_app_opened_legacy_target_metrics(trade: dict) -> bool:
    """Flag old app-opened target metrics that exceed today's CFD risk envelope."""
    if str(trade.get("origin") or "").lower() != "app_opened":
        return False
    started = _safe_positive_float(trade.get("started_with_usd"))
    if started is None:
        return False
    max_sl = started * 0.18
    max_tp = started * 0.40
    changed = False
    sl_amount = _safe_positive_float(trade.get("sl_amount_usd"))
    tp_amount = _safe_positive_float(trade.get("tp_amount_usd"))
    if sl_amount is not None and sl_amount > max_sl:
        sl_amount = max_sl
        trade["sl_amount_usd"] = round(sl_amount, 2)
        trade["sl_pct_of_start"] = 18.0
        changed = True
    if tp_amount is not None and tp_amount > max_tp:
        tp_amount = max_tp
        trade["tp_amount_usd"] = round(tp_amount, 2)
        trade["tp_pct_of_start"] = 40.0
        changed = True
    if changed:
        trade["target_amount_source"] = "legacy_target_capped"
        trade["target_warning"] = (
            "Legacy app trade exceeded the current CFD risk envelope; displayed SL/TP amounts are capped while realized P/L remains unchanged."
        )
    return changed


def _sanitize_unmanaged_broker_target_metrics(trade: dict) -> bool:
    """Avoid presenting broker-imported CFD target math as LinkTrade-managed targets."""
    if str(trade.get("origin") or "").lower() == "app_opened":
        return False
    changed = False
    for key in (
        "sl_amount_usd",
        "tp_amount_usd",
        "sl_pct_of_start",
        "tp_pct_of_start",
    ):
        if trade.get(key) is not None:
            trade[key] = None
            changed = True
    if changed:
        trade["origin"] = "broker_imported"
        trade["target_source"] = "broker_imported_unmanaged"
        trade["target_warning"] = (
            "Broker-imported trade; SL/TP values were not generated or managed by LinkTrade."
        )
    return changed


def _sanitize_live_execution_trace_target_warnings(item: dict) -> bool:
    trace = item.get("execution_trace")
    if not isinstance(trace, dict):
        return False
    reason = str(trace.get("reason") or "")
    if "reward:risk" not in reason.lower():
        return False
    entry = _safe_positive_float(item.get("price_open"))
    sl = _safe_positive_float(item.get("stop_loss"))
    tp = _safe_positive_float(item.get("take_profit"))
    if entry is None or sl is None or tp is None:
        return False
    rr = _reward_risk_ratio(entry, sl, tp)
    required_rr = max(1.2, float(getattr(risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
    if rr < (required_rr - 0.02):
        return False
    cleaned = re.sub(r"\s*\|\s*Risk warning:\s*Reward:risk[^|]*", "", reason, flags=re.IGNORECASE).strip()
    trace["reason"] = cleaned or "Request completed"
    trace["target_warning_cleared"] = True
    trace["live_reward_risk"] = round(rr, 3)
    item["execution_trace"] = trace
    return True
_auto_connect_task: Optional[asyncio.Task] = None
_auto_connect_state: dict = {
    "pending": False,
    "last_started_at": 0.0,
    "last_finished_at": 0.0,
    "last_error": "",
    "paused": False,
    "paused_at": 0.0,
    "pause_reason": "",
}
_ibkr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-worker")


def _run_ibkr_sync(callable_obj, *args, **kwargs):
    """Run every ib_insync operation on its single owning event-loop thread."""
    return _ibkr_executor.submit(lambda: callable_obj(*args, **kwargs)).result(timeout=45.0)
IBKR_SCAN_DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD",
    "NFLX", "AVGO", "ORCL", "CRM", "INTC", "QCOM", "JPM", "BAC",
    "GS", "V", "MA", "WMT", "COST", "KO", "DIS", "XOM", "CVX",
    "SPY", "QQQ", "DIA", "IWM", "GLD", "SLV", "USO", "BNO",
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "BTCUSD", "ETHUSD",
]
MT5_DISCONNECTED_TOP_MARKETS = [
    ("EURUSD", "Euro vs US Dollar", "Forex"),
    ("GBPUSD", "British Pound vs US Dollar", "Forex"),
    ("USDJPY", "US Dollar vs Japanese Yen", "Forex"),
    ("USDCHF", "US Dollar vs Swiss Franc", "Forex"),
    ("USDCAD", "US Dollar vs Canadian Dollar", "Forex"),
    ("AUDUSD", "Australian Dollar vs US Dollar", "Forex"),
    ("US500", "US 500 index CFD", "Indices"),
    ("US100", "US Tech 100 index CFD", "Indices"),
    ("US30", "US Wall Street 30 index CFD", "Indices"),
    ("GER40", "Germany 40 index CFD", "Indices"),
    ("UK100", "UK 100 index CFD", "Indices"),
    ("XAUUSD", "Gold spot vs US Dollar", "Commodities"),
    ("XAGUSD", "Silver spot vs US Dollar", "Commodities"),
    ("WTI", "WTI crude oil CFD", "Commodities"),
    ("BRENT", "Brent crude oil CFD", "Commodities"),
    ("AAPL", "Apple CFD", "Stocks"),
    ("MSFT", "Microsoft CFD", "Stocks"),
    ("NVDA", "NVIDIA CFD", "Stocks"),
    ("TSLA", "Tesla CFD", "Stocks"),
    ("AMZN", "Amazon CFD", "Stocks"),
    ("BTCUSD", "Bitcoin vs US Dollar", "Crypto"),
    ("ETHUSD", "Ethereum vs US Dollar", "Crypto"),
]

router = APIRouter()
_POLICY_MODE_RANK = {"safe": 0, "balanced": 1, "aggressive": 2}


def _available_symbols_cache_key(
    *,
    platform: str,
    connected: bool,
    category: Optional[str],
    tradeable_only: bool,
    include_inactive: bool,
) -> tuple[str, bool, str, bool, bool]:
    return (
        str(platform or ""),
        bool(connected),
        str(category or "").strip().lower(),
        bool(tradeable_only),
        bool(include_inactive),
    )


def _mt5_disconnected_top_market_rows() -> list[dict]:
    rows: list[dict] = []
    for name, description, category in MT5_DISCONNECTED_TOP_MARKETS:
        rows.append(
            {
                "name": name,
                "description": description,
                "category": category,
                "bid": 0.0,
                "ask": 0.0,
                "spread": 0.0,
                "visible": False,
                "trade_enabled": True,
                "live_quote": False,
                "requires_connection": True,
                "source": "mt5_disconnected_top_markets",
                "digits": 5 if category == "Forex" else 2,
                "trade_contract_size": 100000.0 if category == "Forex" else 1.0,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "trade_mode": 0,
                "trade_stops_level": 0,
                "commission_per_lot_side": 0.0,
                "commission_round_turn_per_lot": 0.0,
                "commission_model": "mt5_connection_required",
                "commission_percent_rate": None,
                "commission_notional_1lot": None,
            }
        )
    return rows


def _api_key_health_snapshot(*, force_refresh: bool = False) -> dict:
    service = ApiKeyHealthService(
        gemini_status_getter=_current_gemini_status,
        benzinga_adapter=benzinga_adapter,
        config=ApiKeyHealthConfig(enabled=False),
    )
    return service.get_snapshot(force_refresh=force_refresh)


async def verify_gemini_live_availability(*, timeout_seconds: float = 8.0) -> dict:
    """Run a tiny, non-trading Gemini request to prove the configured key works."""
    if not bool(getattr(gemini_agent, "available", False)):
        return {
            "ok": False,
            "status": _current_gemini_status(),
            "reason": getattr(gemini_agent, "unavailable_reason", "Gemini client unavailable."),
        }
    try:
        from google.genai import types as genai_types

        response = await _generate_gemini_content_with_fallback(
            contents="LinkTrade health check. Reply with exactly: LINKTRADE_GEMINI_OK",
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=16,
            ),
            primary_model=str(getattr(gemini_agent, "model_name", "") or "gemini-2.5-flash"),
            extra_models=["gemini-2.5-flash", "gemini-2.0-flash"],
            timeout_seconds=max(2.0, float(timeout_seconds or 8.0)),
        )
        text = str(getattr(response, "text", "") or "").strip()
        return {
            "ok": True,
            "model": str(getattr(gemini_agent, "active_model_name", "") or ""),
            "response_preview": text[:64],
            "status": _current_gemini_status(),
        }
    except Exception as exc:
        if hasattr(gemini_agent, "mark_runtime_error"):
            gemini_agent.mark_runtime_error(str(exc))
        return {
            "ok": False,
            "reason": str(exc),
            "status": _current_gemini_status(),
        }


def _has_usable_broker_quote(row: dict) -> bool:
    try:
        bid = float(row.get("bid", 0.0) or 0.0)
        ask = float(row.get("ask", 0.0) or 0.0)
    except Exception:
        return False
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= 0.0 or ask < bid:
        return False
    mid = (bid + ask) / 2.0
    if mid <= 0.0:
        return False
    return (max(0.0, ask - bid) / mid) <= 0.05


def _recommended_policy_mode_for_setup(confidence: float, quality_score: float) -> str:
    c = float(confidence or 0.0)
    q = float(quality_score or 0.0)
    if c >= 0.70 and q >= 0.80:
        return "safe"
    if c >= 0.62 and q >= 0.70:
        return "balanced"
    return "aggressive"


def _mode_allows_auto_execution(current_mode: str, required_mode: str) -> bool:
    current_rank = _POLICY_MODE_RANK.get(str(current_mode or "balanced").lower(), 1)
    required_rank = _POLICY_MODE_RANK.get(str(required_mode or "aggressive").lower(), 2)
    return current_rank >= required_rank


def _cache_scan_confidence(symbol: str, confidence: float, strategy: str = "") -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    conf = max(0.0, min(0.95, float(confidence or 0.0)))
    if conf <= 0.0:
        return
    _scan_confidence_cache[sym] = (time.time(), conf, str(strategy or ""))


def _cached_scan_confidence(symbol: str) -> tuple[float, str]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return (0.0, "")
    row = _scan_confidence_cache.get(sym)
    if row is None:
        return (0.0, "")
    ts, conf, strategy = row
    if (time.time() - float(ts or 0.0)) > _SCAN_CONFIDENCE_TTL_SECONDS:
        _scan_confidence_cache.pop(sym, None)
        return (0.0, "")
    return (max(0.0, min(0.95, float(conf or 0.0))), str(strategy or ""))


def _news_payload_from_signal(signal_payload: dict | None) -> dict:
    payload = dict(signal_payload or {})
    metadata = dict(payload.get("metadata") or {})
    event_context = dict(metadata.get("event_context") or {})
    news_ctx = dict(event_context.get("news") or metadata.get("news") or {})
    sentiment = str(news_ctx.get("sentiment") or event_context.get("bias") or "neutral").lower()
    impact = str(news_ctx.get("impact") or event_context.get("event_risk") or "low").lower()
    relevance = str(news_ctx.get("relevance") or "indirect").lower()
    strength = float(news_ctx.get("strength", 0.0) or 0.0)
    risk_flag = bool(news_ctx.get("risk_flag", False))
    message = str(news_ctx.get("message") or "").strip()
    if not message:
        if bool(news_ctx.get("news_unavailable")):
            message = "Decision made without news data"
        elif bool(news_ctx.get("execution_block")):
            message = "High-impact news detected"
        elif bool(news_ctx.get("recommend_hold")):
            message = "Trade avoided due to high-impact news"
        elif sentiment in {"bullish", "bearish"} and strength >= 0.5:
            message = "News supports this decision"
        elif bool(event_context.get("contradiction_flag")):
            message = "News contradicts the setup"
        else:
            message = "No major news"
    return {
        "sentiment": sentiment if sentiment in {"bullish", "bearish", "neutral"} else "neutral",
        "impact": impact if impact in {"low", "medium", "high"} else "low",
        "relevance": relevance if relevance in {"direct", "indirect", "global"} else "indirect",
        "strength": max(0.0, min(1.0, strength)),
        "risk_flag": risk_flag,
        "message": message,
        "news_unavailable": bool(news_ctx.get("news_unavailable", False)),
    }


def _decorate_recommendation_mode_fit(rec: dict) -> dict:
    auto_result = rec.get("auto_execution_result")
    if isinstance(auto_result, dict) and bool(auto_result.get("success")):
        rec["recommended_policy_mode"] = rec.get("recommended_policy_mode") or str(getattr(risk_engine.user_policy, "mode", "balanced") or "balanced")
        rec["auto_execution_allowed_by_mode"] = True
        rec["auto_execution_skipped_reason"] = ""
        return rec

    signal = dict(rec.get("signal") or {})
    action = str(signal.get("action", "")).upper()
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    quality_score = float((rec.get("trade_quality") or {}).get("final_trade_quality_score", 0.0) or 0.0)
    current_mode = str(getattr(risk_engine.user_policy, "mode", "balanced") or "balanced").lower()

    if action not in {"BUY", "SELL"}:
        rec["recommended_policy_mode"] = None
        rec["auto_execution_allowed_by_mode"] = False
        rec["auto_execution_skipped_reason"] = "Signal is not executable."
        return rec

    required_mode = _recommended_policy_mode_for_setup(confidence, quality_score)
    auto_allowed = bool(rec.get("ready_to_execute")) and _mode_allows_auto_execution(current_mode, required_mode)
    rec["recommended_policy_mode"] = required_mode
    rec["auto_execution_allowed_by_mode"] = auto_allowed
    if not bool(rec.get("ready_to_execute")):
        rec["auto_execution_skipped_reason"] = rec.get("execution_reason") or "Setup is not ready to execute."
    elif not auto_allowed:
        rec["auto_execution_skipped_reason"] = (
            f"Current mode '{current_mode}' is below the required '{required_mode}' execution level."
        )
    else:
        rec["auto_execution_skipped_reason"] = ""
    return rec


def _sync_route_gemini_cooldown(*models: str) -> None:
    global _gemini_quota_cooldown_until
    candidates = build_structured_output_model_candidates(
        *(models or (gemini_agent.model_name, "gemini-2.5-flash", "gemini-2.0-flash"))
    )
    if next_available_model(candidates, _gemini_route_model_cooldowns) is not None:
        _gemini_quota_cooldown_until = 0.0
        return
    remaining = cooldown_remaining_seconds(candidates, _gemini_route_model_cooldowns)
    _gemini_quota_cooldown_until = time.time() + remaining if remaining > 0 else 0.0


async def _generate_gemini_content_with_fallback(
    *,
    contents,
    config,
    primary_model: str,
    extra_models: list[str] | None = None,
    timeout_seconds: float = 5.0,
):
    if not gemini_agent.available or getattr(gemini_agent, "_client", None) is None:
        raise RuntimeError("Gemini client unavailable.")
    candidates = build_structured_output_model_candidates(primary_model, *(extra_models or []))
    last_error: Exception | None = None
    for model_name in candidates:
        if float(_gemini_route_model_cooldowns.get(model_name, 0.0) or 0.0) > time.time():
            continue
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_agent._client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout_seconds,
            )
            gemini_agent.active_model_name = model_name
            if hasattr(gemini_agent, "mark_runtime_success"):
                gemini_agent.mark_runtime_success()
            _sync_route_gemini_cooldown(*candidates)
            return response
        except asyncio.TimeoutError as exc:
            last_error = exc
            mark_model_cooldown(
                _gemini_route_model_cooldowns,
                model_name,
                error_text="timeout",
                default_seconds=60.0,
            )
            _sync_route_gemini_cooldown(*candidates)
            logger.warning("Gemini route model %s timed out, trying fallback model.", model_name)
            continue
        except Exception as exc:
            last_error = exc
            if is_rate_limit_error(exc):
                if hasattr(gemini_agent, "mark_runtime_error"):
                    gemini_agent.mark_runtime_error("Quota exhausted / rate limited. Using technical logic only until quota resets.")
                mark_model_cooldown(
                    _gemini_route_model_cooldowns,
                    model_name,
                    error_text=str(exc),
                    default_seconds=60.0,
                )
                _sync_route_gemini_cooldown(*candidates)
                logger.warning("Gemini route model %s rate limited, trying fallback model.", model_name)
                continue
            raise
    _sync_route_gemini_cooldown(*candidates)
    if last_error is not None:
        raise last_error
    raise RuntimeError("All configured Gemini route models are in cooldown.")

# Shared state
mt5_connector = MT5Connector()
connector = mt5_connector
ibkr_connector = IBKRConnector()
ibkr_market_data = IBKRMarketDataAdapter(ibkr_connector, runner=_run_ibkr_sync)
ibkr_execution = IBKRExecutionAdapter(ibkr_connector, runner=_run_ibkr_sync)
ibkr_runtime_connector = IBKRConnectionAdapter(ibkr_connector, runner=_run_ibkr_sync)
tradestation_connector = TradeStationConnector()
mt5_market_data = MarketDataService()
market_data = mt5_market_data
mt5_execution = ExecutionEngine()
execution = mt5_execution
risk_engine = RiskEngine()
risk_engine.auto_trade_session_armed = False
db: Optional[Database] = None
universe_service = SymbolUniverseService(config)
tradestation_market_data = TradeStationMarketDataService(tradestation_connector)
tradestation_execution = TradeStationExecutionEngine(tradestation_connector, tradestation_market_data)

# Agent registry - Gemini is the primary trade brain, SmartAgent remains the built-in technical analyst/fallback
gemini_agent = GeminiAgent()
agents = {
    "GeminiAgent": gemini_agent,
    "SmartAgent": SmartAgent(),
    "MockAgent": MockAgent(),
    "SMA_Crossover": SMACrossoverAgent(),
}
active_agent_name = "GeminiAgent"
active_platform: Literal["mt5", "ibkr", "tradestation"] = "mt5"
credential_vault = CredentialVault()
gemini_adapter = GeminiAdapter(
    gemini_agent=gemini_agent,
    timeout_seconds=config.GEMINI_TIMEOUT_SECONDS,
    max_retries=config.GEMINI_MAX_RETRIES,
)
news_ingestion_service = NewsIngestionService()
gemini_news_analysis_service = GeminiNewsAnalysisService(
    gemini_agent=gemini_agent,
    timeout_seconds=config.GEMINI_TIMEOUT_SECONDS,
    max_retries=config.GEMINI_MAX_RETRIES,
)
gemini_strategy_compiler_service = GeminiStrategyCompilerService(
    timeout_seconds=config.GEMINI_TIMEOUT_SECONDS,
    max_retries=config.GEMINI_MAX_RETRIES,
    model_name=config.GEMINI_MODEL,
)
trade_decision_service = TradeDecisionService()
benzinga_adapter = BenzingaAdapter(
    api_key=config.BENZINGA_API_KEY,
    enabled=config.ENABLE_BENZINGA,
    timeout_seconds=config.BENZINGA_TIMEOUT_SECONDS,
    max_retries=config.BENZINGA_MAX_RETRIES,
)
event_normalization_service = EventNormalizationService()
asset_mapping_service = AssetMappingService(universe_service=universe_service)
gemini_event_classifier = GeminiEventClassifier(
    timeout_seconds=config.GEMINI_TIMEOUT_SECONDS,
    max_retries=config.GEMINI_MAX_RETRIES,
    model_name=config.GEMINI_MODEL,
)
event_ingestion_service = EventIngestionService(
    benzinga_adapter=benzinga_adapter,
    normalization_service=event_normalization_service,
    asset_mapping_service=asset_mapping_service,
    gemini_event_classifier=gemini_event_classifier,
)
signal_pipeline = SignalPipelineService(
    connector=connector,
    market_data=market_data,
    execution=execution,
    risk_engine=risk_engine,
    agents=agents,
    gemini_adapter=gemini_adapter,
    universe_service=universe_service,
    news_ingestion_service=news_ingestion_service,
    event_ingestion_service=event_ingestion_service,
    gemini_news_analysis_service=gemini_news_analysis_service,
    trade_decision_service=trade_decision_service,
)
risk_service = signal_pipeline.risk_service
execution_service = ExecutionService(
    execution_engine=execution,
    risk_service=risk_service,
    signal_pipeline=signal_pipeline,
)
kill_switch_service = KillSwitchService()
execution_service.kill_switch_state_getter = kill_switch_service.current_state
broker_symbol_validator = BrokerSymbolValidator()
reconciliation_service = ReconciliationService(execution_engine=execution)

# Auto-trader
from auto_trader import AutoTrader
auto_trader = AutoTrader(
    connector=connector,
    market_data=market_data,
    execution=execution,
    risk_engine=risk_engine,
    agents=agents,
    signal_pipeline=signal_pipeline,
    execution_service=execution_service,
)
auto_trader.kill_switch_state_getter = kill_switch_service.current_state
task_orchestrator = TaskOrchestrator(auto_trader)
analytics_service = AnalyticsService()
replay_service = ReplayService(signal_pipeline)
research_cycle_service: Optional[ResearchCycleService] = None
RUNTIME_STATE_KEY = "runtime_controls_v1"
CHAT_HISTORY_STATE_KEY = "chat_history_v1"
ACTIVE_STRATEGY_STATE_KEY_PREFIX = "active_strategy_v1"
DEFAULT_MARGIN_SL_PCT = 0.08
DEFAULT_MARGIN_TP_PCT = 0.12


class KillSwitchUpdateRequest(BaseModel):
    reason: str = ""


class BrokerSymbolDiagnosticsRequest(BaseModel):
    symbol: str
    action: str = "BUY"
    volume: float = 0.01
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


_cloud_brain_state: dict = {
    "last_command_at": 0.0,
    "last_command_payload": {},
    "last_apply_result": {},
    "last_error": None,
}
cloud_secret_service = CloudSecretService(
    enabled=config.CLOUD_SECRETS_ENABLED,
    supabase_url=config.SUPABASE_URL,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
    table=config.CLOUD_SECRETS_TABLE,
    timeout_seconds=config.CLOUD_SYNC_TIMEOUT_SECONDS,
)
secret_store = SecretStore(
    mode=config.SECRET_STORAGE_MODE,
    encryption_key=config.SECRET_ENCRYPTION_KEY,
    credential_vault=credential_vault,
    cloud_secret_service=cloud_secret_service,
    allow_insecure_dev=(config.APP_MODE == "development"),
)
_saved_credentials_cache: list[dict] = []
supabase_auth = SupabaseAdminService(
    url=config.SUPABASE_URL,
    anon_key=config.SUPABASE_ANON_KEY,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
)


def _apply_active_broker_runtime(platform: str) -> None:
    global connector, market_data, execution
    normalized = str(platform or "mt5").strip().lower()
    if normalized == "tradestation":
        connector = tradestation_connector
        market_data = tradestation_market_data
        execution = tradestation_execution
    elif normalized == "ibkr":
        connector = ibkr_runtime_connector
        market_data = ibkr_market_data
        execution = ibkr_execution
    else:
        connector = mt5_connector
        market_data = mt5_market_data
        execution = mt5_execution

    signal_pipeline.connector = connector
    signal_pipeline.market_data = market_data
    signal_pipeline.execution = execution
    signal_pipeline.portfolio_risk_service.market_data = market_data
    signal_pipeline.risk_service.execution_engine = execution

    execution_service.execution_engine = execution
    execution_service.signal_pipeline = signal_pipeline

    auto_trader.connector = connector
    auto_trader.market_data = market_data
    auto_trader.execution = execution
    auto_trader.signal_pipeline = signal_pipeline
    auto_trader.execution_service = execution_service
    auto_trader.position_manager.connector = connector
    auto_trader.position_manager.market_data = market_data
    auto_trader.position_manager.execution = execution
    reconciliation_service.set_execution_engine(execution)


async def _refresh_saved_credentials_cache(timeout_seconds: float = 3.5) -> list[dict]:
    """Load saved MT5 credential metadata with a bounded timeout.

    Under heavy scan/evaluation load SQLite can be temporarily saturated.
    Keep a warm in-memory cache so the connection page can render immediately
    instead of timing out and showing only local IBKR entries.
    """
    global _saved_credentials_cache
    if db is None:
        return list(_saved_credentials_cache)
    creds = await asyncio.wait_for(db.get_saved_credentials(), timeout=max(1.0, float(timeout_seconds)))
    normalized = list(creds or [])
    _saved_credentials_cache = normalized
    return normalized


async def _get_saved_credentials_with_fallback(timeout_seconds: float = 3.5) -> list[dict]:
    try:
        return await _refresh_saved_credentials_cache(timeout_seconds=timeout_seconds)
    except Exception as exc:
        logger.warning("Saved credentials fetch fell back to in-memory cache: %s", exc)
        return list(_saved_credentials_cache)


async def _get_saved_credential_with_fallback(account_id: int, timeout_seconds: float = 3.0) -> Optional[dict]:
    if db is None:
        return next(
            (row for row in _saved_credentials_cache if int(row.get("account") or 0) == int(account_id)),
            None,
        )
    try:
        row = await asyncio.wait_for(
            db.get_saved_credential(account_id),
            timeout=max(1.0, float(timeout_seconds)),
        )
        # Keep list cache warm/consistent.
        await _get_saved_credentials_with_fallback(timeout_seconds=2.5)
        return row
    except Exception as exc:
        logger.warning("Saved credential fetch fell back to in-memory cache for account %s: %s", account_id, exc)
        return next(
            (row for row in _saved_credentials_cache if int(row.get("account") or 0) == int(account_id)),
            None,
        )


async def _finalize_auto_connect_success(credential: dict):
    if db is None:
        return
    try:
        await db.log_connection_event("auto_connected", credential["account"], credential["server"])
    except Exception as exc:
        logger.warning("Auto-connect event logging failed: %s", exc)
    try:
        await db.update_credential_last_used(credential["account"])
    except Exception as exc:
        logger.warning("Auto-connect last-used update failed: %s", exc)
    try:
        await _get_saved_credentials_with_fallback(timeout_seconds=2.5)
    except Exception as exc:
        logger.warning("Auto-connect credential cache refresh failed: %s", exc)
    try:
        await _sync_active_strategy_runtime_for_user("local-anonymous", preserve_user_policy=True)
    except Exception as exc:
        logger.warning("Auto-connect strategy runtime sync failed: %s", exc)


async def _warm_saved_credentials_cache():
    await _get_saved_credentials_with_fallback(timeout_seconds=8.0)


def _pause_auto_connect(reason: str = "Auto-connect paused after manual disconnect.") -> None:
    global _auto_connect_task
    global _auto_connect_state
    if _auto_connect_task and not _auto_connect_task.done():
        _auto_connect_task.cancel()
    _auto_connect_task = None
    _auto_connect_state["pending"] = False
    _auto_connect_state["paused"] = True
    _auto_connect_state["paused_at"] = time.time()
    _auto_connect_state["pause_reason"] = str(reason or "Auto-connect paused after manual disconnect.")


def _resume_auto_connect() -> None:
    global _auto_connect_state
    _auto_connect_state["paused"] = False
    _auto_connect_state["paused_at"] = 0.0
    _auto_connect_state["pause_reason"] = ""


def _mask_secret(value: str) -> str:
    return redact_secret(value)


def _raise_if_kill_switch_active() -> None:
    state = kill_switch_service.current_state()
    if state.enabled:
        detail = "Global trading kill switch is active."
        if state.reason:
            detail = f"{detail} {state.reason}"
        raise HTTPException(status_code=403, detail=detail)


def _current_account_key() -> str:
    connected = bool(getattr(connector, "connected", True))
    try:
        account = connector.refresh_account() if connected and hasattr(connector, "refresh_account") else None
    except Exception:
        account = None
    login = str(getattr(account, "login", "") or getattr(account, "account", "") or "")
    server = str(getattr(account, "server", "") or getattr(connector, "server", "") or "")
    try:
        is_demo = bool(connector.is_demo()) if connected and hasattr(connector, "is_demo") else False
    except Exception:
        is_demo = False
    demo_flag = "demo" if is_demo else "live"
    return f"{active_platform}:{demo_flag}:{login}:{server}".strip(":")


def _order_row_api_result(row: dict) -> dict:
    return {
        "success": bool(row.get("success")),
        "retcode": int(row.get("retcode") or -1),
        "retcode_desc": str(row.get("retcode_desc") or ""),
        "ticket": row.get("ticket"),
        "volume": row.get("volume"),
        "price": row.get("price"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "comment": row.get("comment") or "",
        "idempotency_key": row.get("idempotency_key") or "",
        "idempotent_replay": True,
    }


async def _existing_idempotent_order(
    *,
    idempotency_key: str,
    account_key: str,
    execution_context: str,
) -> dict | None:
    if not db or not idempotency_key:
        return None
    rows = await db.get_orders_by_idempotency_key(
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
    )
    return rows[0] if rows else None


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not isinstance(authorization, str):
        raise HTTPException(status_code=401, detail="Missing authorization header.")
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid authorization header.")
    token = authorization[len(prefix):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return token


def _anonymous_user() -> dict:
    return {
        "id": "local-anonymous",
        "email": "local@device",
        "role": "local",
    }


async def _resolve_authenticated_user(authorization: Optional[str]) -> dict:
    if not authorization or not isinstance(authorization, str):
        return _anonymous_user()
    if not supabase_auth.configured:
        return _anonymous_user()
    token = _extract_bearer_token(authorization)
    try:
        user = await asyncio.to_thread(supabase_auth.get_user_from_token, token)
        return {
            "id": str(user.get("id") or "local-anonymous"),
            "email": str(user.get("email") or ""),
            "role": str((user.get("app_metadata") or {}).get("role") or (user.get("user_metadata") or {}).get("role") or "user").lower(),
            "app_metadata": dict(user.get("app_metadata") or {}),
            "user_metadata": dict(user.get("user_metadata") or {}),
        }
    except Exception:
        return _anonymous_user()


async def _require_runtime_admin(authorization: Optional[str]) -> dict:
    if not config.AUTH_REQUIRED:
        return _anonymous_user()
    if not authorization or not isinstance(authorization, str):
        raise HTTPException(status_code=401, detail="Missing admin bearer token.")
    if not supabase_auth.configured:
        raise HTTPException(status_code=503, detail="Supabase auth is not configured.")
    token = _extract_bearer_token(authorization)
    try:
        user = await asyncio.to_thread(supabase_auth.get_user_from_token, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid admin bearer token.") from exc
    role = str((user.get("app_metadata") or {}).get("role") or (user.get("user_metadata") or {}).get("role") or "").lower()
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    return {
        "id": str(user.get("id") or "admin"),
        "email": str(user.get("email") or ""),
        "role": role,
    }


DEFAULT_USER_PERMISSIONS: dict[str, bool] = {
    "can_view_market_data": True,
    "can_create_strategy": True,
    "can_generate_signal": True,
    "can_paper_trade": True,
    "can_execute_demo": True,
    "can_execute_live": False,
    "can_enable_autotrader": True,
    "can_manage_risk": True,
    "can_access_logs": True,
    "can_access_research": True,
    "can_connect_broker": True,
    "can_manage_secrets": True,
    "can_access_admin": False,
}


def _normalize_permissions(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: bool(raw.get(key))
        for key in DEFAULT_USER_PERMISSIONS
        if key in raw
    }


async def _effective_user_permissions(user: dict) -> dict[str, bool]:
    role = str(user.get("role") or "").strip().lower()
    if role == "admin":
        return {key: True for key in DEFAULT_USER_PERMISSIONS}
    permissions = dict(DEFAULT_USER_PERMISSIONS)
    if not config.AUTH_REQUIRED and str(user.get("id") or "") == "local-anonymous":
        permissions = {key: True for key in DEFAULT_USER_PERMISSIONS}
        permissions["can_access_admin"] = False
        return permissions

    permissions.update(_normalize_permissions((user.get("app_metadata") or {}).get("permissions")))
    permissions.update(_normalize_permissions((user.get("user_metadata") or {}).get("permissions")))
    if db is not None:
        try:
            access = await db.get_user_access(user_id=str(user.get("id") or ""))
        except Exception:
            access = None
        if access:
            permissions.update(_normalize_permissions(access.get("permissions_json")))
    return permissions


async def _require_user_permission(authorization: Optional[str], permission: str) -> dict:
    user = await _resolve_authenticated_user(authorization)
    if config.AUTH_REQUIRED and str(user.get("id") or "") == "local-anonymous":
        raise HTTPException(status_code=401, detail="Authentication required.")
    permissions = await _effective_user_permissions(user)
    if not bool(permissions.get(permission, False)):
        raise HTTPException(status_code=403, detail=f"Permission required: {permission}")
    return user


async def _require_order_execution_permission(
    authorization: Optional[str],
    *,
    execution_mode: str,
) -> dict:
    mode = str(execution_mode or "live").strip().lower()
    if mode in {"paper", "shadow"}:
        return await _require_user_permission(authorization, "can_paper_trade")
    is_demo_account = True
    try:
        if active_platform == "mt5":
            is_demo_account = bool(connector.is_demo())
        elif active_platform == "ibkr":
            account = getattr(ibkr_connector, "account_info", None)
            is_demo_account = not bool(account and getattr(account, "trade_mode", 0) != 0)
        elif active_platform == "tradestation":
            is_demo_account = True
    except Exception:
        is_demo_account = True
    return await _require_user_permission(
        authorization,
        "can_execute_demo" if is_demo_account else "can_execute_live",
    )


def _current_account_is_demo() -> bool:
    try:
        if active_platform == "ibkr":
            return bool(ibkr_connector.is_demo())
        if active_platform == "mt5":
            return bool(connector.is_demo())
        return True
    except Exception:
        return True


def _enforce_risk_profile_execution_caps(*, auto_trade: bool = False) -> None:
    policy = risk_engine.user_policy
    if auto_trade and not bool(getattr(policy, "auto_trading_allowed", False)):
        raise HTTPException(
            status_code=403,
            detail=(
                "AutoTrader is blocked by the active risk profile. "
                "Enable 'Auto-Trading Allowed By This Profile' in Risk Management, then save the profile."
            ),
        )
    if not _current_account_is_demo() and not bool(getattr(policy, "live_execution_allowed", False)):
        raise HTTPException(
            status_code=403,
            detail="Live execution is blocked by the active risk profile.",
        )


def _strategy_state_key_for_user(user_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]", "_", str(user_id or "local-anonymous"))
    return f"{ACTIVE_STRATEGY_STATE_KEY_PREFIX}:{normalized}"


def _builtin_strategy_profiles() -> list[dict]:
    return [
        {
            "id": "builtin-default-smart",
            "name": "Default Smart",
            "description": "Balanced Gemini-primary strategy using SmartAgent baseline, Benzinga/news context, and meta-model review.",
            "scope": "builtin",
            "is_default": True,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "balanced",
                "allowed_symbols": [],
                "scan_interval_seconds": 30,
                "gemini_role": "advisory",
                "min_reward_risk": 1.8,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: Multi-asset balanced watchlist.\n"
                    "2) Entry logic: SmartAgent builds the baseline chart thesis, then Gemini combines that with last-hours chart structure, Benzinga/news relevance, and meta-model insight to author the trade action.\n"
                    "3) Risk logic: Deterministic risk engine still enforces stop-loss, reward/risk floor, margin protection, and duplicate/cooldown checks.\n"
                    "4) AI role: Gemini is the primary decision brain, but it still cannot bypass execution guards.\n"
                    "5) Best use: Default day-to-day mode when you want stable behavior across markets."
                ),
                "human_summary": "This strategy scans the enabled market universe and chooses BUY, SELL, or HOLD per symbol based on technicals, news, meta-model review, and risk checks.",
                "summary_lines": [
                    "Symbols: All enabled markets",
                    "Market selection: dynamic multi-asset scan",
                    "Asset classes: Any active class",
                    "Entry action: Dynamic (BUY / SELL / HOLD per symbol)",
                    "Fixed lot: risk-sized",
                    "Margin allocation: Not specified",
                    "Buy cadence: 0s",
                    "Sell timer: 0s",
                    "Force mode: OFF",
                    "Policy mode: balanced",
                    "Gemini role: advisory",
                    "Scan interval: 30s",
                    "Region hint: None",
                ],
            },
        },
        {
            "id": "builtin-safe-capital-guard",
            "name": "Capital Guard",
            "description": "Lower-risk profile, strict quality and risk preservation.",
            "scope": "builtin",
            "is_default": False,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "safe",
                "allowed_symbols": [],
                "scan_interval_seconds": 35,
                "gemini_role": "confirmation-required",
                "min_reward_risk": 2.2,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: Broad, but filtered aggressively for cleaner setups.\n"
                    "2) Entry logic: SmartAgent baseline analysis is passed into Gemini, which must still produce a high-quality chart/news/meta-aligned decision before a trade is considered.\n"
                    "3) Risk logic: Higher minimum reward/risk, tighter capital protection, and conservative policy mode.\n"
                    "4) AI role: Gemini remains primary, with stricter confirmation behavior for extra caution.\n"
                    "5) Best use: Capital-preservation periods, volatile sessions, or when reducing drawdown is priority."
                ),
            },
        },
        {
            "id": "builtin-opportunity-seeker",
            "name": "Opportunity Seeker",
            "description": "Faster scans with broader opportunity capture while staying demo-safe.",
            "scope": "builtin",
            "is_default": False,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "aggressive",
                "allowed_symbols": [],
                "scan_interval_seconds": 20,
                "gemini_role": "advisory",
                "min_reward_risk": 1.6,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: Wider watchlist with faster signal refresh.\n"
                    "2) Entry logic: Gemini evaluates faster baseline chart setups with news and meta-model support to capture momentum and continuation opportunities earlier.\n"
                    "3) Risk logic: Still blocked by deterministic risk gates (spread, margin, RR, cooldown, session checks).\n"
                    "4) AI role: Gemini is the main recommender, but the execution stack still has final authority.\n"
                    "5) Best use: Active sessions when you want more opportunities without disabling safety."
                ),
            },
        },
        {
            "id": "builtin-egypt-gold-session",
            "name": "Egypt Gold Session",
            "description": "XAUUSD-focused intraday profile for London and New York sessions.",
            "scope": "builtin",
            "is_default": False,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "balanced",
                "allowed_symbols": ["XAUUSD", "GOLD"],
                "scan_interval_seconds": 20,
                "gemini_role": "advisory",
                "min_reward_risk": 1.9,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: Gold-first (XAUUSD/GOLD), tuned for London + New York liquidity windows.\n"
                    "2) Entry logic: SmartAgent reads the pullback/resume chart pattern and Gemini decides using that structure plus relevant Gold news and meta-model context.\n"
                    "3) Risk logic: Balanced mode with fixed deterministic blocking for bad RR, margin stress, and stale market state.\n"
                    "4) AI role: Gemini is the primary gold-session decision brain and still cannot bypass execution guards.\n"
                    "5) Best use: Traders in Egypt/Gulf timezones focusing on Gold intraday behavior."
                ),
            },
        },
        {
            "id": "builtin-egypt-us-indices",
            "name": "US Indices Momentum",
            "description": "US30/US100/US500 momentum profile aligned with US market hours.",
            "scope": "builtin",
            "is_default": False,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "balanced",
                "allowed_symbols": ["US30", "US100", "US500", "SP500"],
                "scan_interval_seconds": 20,
                "gemini_role": "advisory",
                "min_reward_risk": 1.8,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: US30, US100, US500/SP500 for high-interest index moves.\n"
                    "2) Entry logic: Gemini weighs SmartAgent momentum structure, recent chart action, relevant headlines, and meta-model analysis before recommending direction.\n"
                    "3) Risk logic: Rejects setups with weak RR, wide spread, or margin pressure.\n"
                    "4) AI role: Gemini is the primary recommender; deterministic risk checks remain the final gate.\n"
                    "5) Best use: US session momentum trading with disciplined signal quality."
                ),
            },
        },
        {
            "id": "builtin-egypt-mixed-safe",
            "name": "Egypt Balanced Mix",
            "description": "Popular mixed watchlist (Gold, Silver, Oil, indices) with conservative controls.",
            "scope": "builtin",
            "is_default": False,
            "config": {
                "agent_name": "GeminiAgent",
                "policy_mode": "safe",
                "allowed_symbols": ["XAUUSD", "XAGUSD", "WTI", "US30", "US100", "GER40", "UK100"],
                "scan_interval_seconds": 28,
                "gemini_role": "confirmation-required",
                "min_reward_risk": 2.1,
                "brief": (
                    "How this strategy works:\n"
                    "1) Market scope: Popular Egypt-focused mix (Gold, Silver, Oil, US/Europe indices).\n"
                    "2) Entry logic: SmartAgent baseline analysis is filtered through Gemini's chart/news/meta review so only clearer setups survive.\n"
                    "3) Risk logic: Safe mode, stronger RR requirements, conservative exposure posture.\n"
                    "4) AI role: Gemini stays primary with stricter confirmation behavior and no ability to bypass execution controls.\n"
                    "5) Best use: Users who want diversified exposure with controlled risk profile."
                ),
            },
        },
    ]


def _default_strategy_id() -> str:
    return _builtin_strategy_profiles()[0]["id"]


def _sanitize_custom_strategy_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned[:60]


def _duration_token_to_seconds(amount: str, unit: str) -> int:
    try:
        value = max(0, int(float(amount)))
    except (TypeError, ValueError):
        return 0
    token = (unit or "").lower()
    if token.startswith("sec"):
        return value
    if token.startswith("hour") or token in {"hr", "hrs"}:
        return value * 3600
    if token.startswith("day"):
        return value * 86400
    return value * 60


def _strategy_scope_from_compiled(compiled: dict) -> list[str]:
    explicit = [str(sym).strip().upper() for sym in (compiled.get("symbols") or []) if str(sym).strip()]
    if explicit:
        return explicit
    asset_classes = [str(item).strip().title() for item in (compiled.get("asset_classes") or []) if str(item).strip()]
    selection_mode = str(compiled.get("market_selection_mode") or "").strip().lower()
    if selection_mode in {"top_commodity", "asset_class_filter"} and "Commodities" in asset_classes:
        return ["XAUUSD", "XAGUSD", "WTI", "BRENT"]
    if selection_mode in {"top_index", "asset_class_filter"} and "Indices" in asset_classes:
        return ["US30", "US100", "US500", "GER40", "UK100"]
    if selection_mode in {"top_stock", "asset_class_filter"} and "Stocks" in asset_classes:
        return ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "TSLA"]
    return []


def _format_strategy_duration(seconds: int) -> str:
    total = int(max(0, seconds or 0))
    if total <= 0:
        return "0s"
    if total % 86400 == 0:
        days = total // 86400
        return f"{days} day" + ("s" if days != 1 else "")
    if total % 3600 == 0:
        hours = total // 3600
        return f"{hours} hour" + ("s" if hours != 1 else "")
    if total % 60 == 0:
        minutes = total // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{total} second" + ("s" if total != 1 else "")


def _strategy_symbol_aliases() -> dict[str, str]:
    return {
        "gold": "XAUUSD",
        "xauusd": "XAUUSD",
        "gold spot": "XAUUSD",
        "silver": "XAGUSD",
        "xagusd": "XAGUSD",
        "nasdaq": "US100",
        "us100": "US100",
        "sp500": "US500",
        "s&p 500": "US500",
        "us500": "US500",
        "dax": "GER40",
        "ger40": "GER40",
        "ftse": "UK100",
        "uk100": "UK100",
        "dow": "US30",
        "dow jones": "US30",
        "us30": "US30",
        "oil": "WTI",
        "wti": "WTI",
        "brent": "BRENT",
        "eurusd": "EURUSD",
        "gbpusd": "GBPUSD",
        "usdjpy": "USDJPY",
        "btcusd": "BTCUSD",
        "ethusd": "ETHUSD",
        "apple": "AAPL",
        "aapl": "AAPL",
        "tesla": "TSLA",
        "tsla": "TSLA",
        "nvidia": "NVDA",
        "nvda": "NVDA",
        "microsoft": "MSFT",
        "msft": "MSFT",
        "amd": "AMD",
        "amazon": "AMZN",
        "amzn": "AMZN",
    }


def _compile_strategy_definition(name: str, description: str, *, apply_under_any_conditions: bool = False) -> dict:
    text = f"{name} {description}".strip()
    lower_text = text.lower()

    mode = "balanced"
    if any(token in lower_text for token in ["safe", "conservative", "low risk", "capital protect"]):
        mode = "safe"
    elif any(token in lower_text for token in ["aggressive", "high risk", "scalp", "fast"]):
        mode = "aggressive"

    gemini_role = "advisory"
    if any(token in lower_text for token in ["no ai", "without ai", "disable gemini"]):
        gemini_role = "off"
    elif any(token in lower_text for token in ["confirm", "strict ai", "confirmation"]):
        gemini_role = "confirmation-required"

    cadence_intent = bool(
        re.search(r"\bevery\s+\d+\s*(sec|secs|second|seconds|min|mins|minute|minutes)\b", lower_text)
        or re.search(r"\bafter\s+\d+\s*(sec|secs|second|seconds|min|mins|minute|minutes)\b", lower_text)
    )
    if cadence_intent and mode == "balanced":
        mode = "aggressive"

    scan_interval = 30
    if any(token in lower_text for token in ["fast", "scalp", "quick"]):
        scan_interval = 20
    elif any(token in lower_text for token in ["slow", "swing", "position"]):
        scan_interval = 40
    scan_interval = max(15, min(60, scan_interval))

    allowed_symbols: list[str] = []
    for token, symbol in _strategy_symbol_aliases().items():
        if token in lower_text and symbol not in allowed_symbols:
            allowed_symbols.append(symbol)

    asset_classes: list[str] = []
    market_selection_mode: str | None = None
    if "cfd" in lower_text:
        market_selection_mode = "asset_class_filter"
        asset_classes = ["Indices", "Commodities"]
        if mode == "balanced":
            mode = "aggressive"
            scan_interval = min(scan_interval, 20)
    elif any(token in lower_text for token in ["top commodity", "best commodity", "strongest commodity"]):
        market_selection_mode = "top_commodity"
        asset_classes = ["Commodities"]
    elif any(token in lower_text for token in ["top stock", "best stock", "strongest stock"]):
        market_selection_mode = "top_stock"
        asset_classes = ["Stocks"]
    elif any(token in lower_text for token in ["top index", "best index", "strongest index"]):
        market_selection_mode = "top_index"
        asset_classes = ["Indices"]
    elif any(token in lower_text for token in ["commodities only", "commodity only"]) or ("commodity" in lower_text and not allowed_symbols):
        market_selection_mode = "asset_class_filter"
        asset_classes = ["Commodities"]
    elif any(token in lower_text for token in ["stocks only", "stock only"]):
        market_selection_mode = "asset_class_filter"
        asset_classes = ["Stocks"]
    elif any(token in lower_text for token in ["indices only", "index only"]):
        market_selection_mode = "asset_class_filter"
        asset_classes = ["Indices"]

    market_region_hint = "egypt" if any(token in lower_text for token in [" egypt", "egypt ", " cairo", "cairo "]) else None

    entry_action = "BUY"
    if re.search(r"\bsell\b.{0,80}\bevery\b", lower_text) and not re.search(r"\bbuy\b.{0,80}\bevery\b", lower_text):
        entry_action = "SELL"
    elif "sell it" in lower_text and "buy" not in lower_text:
        entry_action = "SELL"

    buy_every_match = re.search(
        r"\b(buy|sell)\b.{0,120}\bevery\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b",
        lower_text,
    )
    cadence_buy_every_seconds = 0
    if buy_every_match:
        entry_action = buy_every_match.group(1).upper()
        cadence_buy_every_seconds = _duration_token_to_seconds(
            buy_every_match.group(2),
            buy_every_match.group(3),
        )

    sell_after_match = re.search(
        r"\bsell\b.{0,120}\bafter\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b",
        lower_text,
    )
    cadence_sell_after_seconds = 0
    if sell_after_match:
        cadence_sell_after_seconds = _duration_token_to_seconds(
            sell_after_match.group(1),
            sell_after_match.group(2),
        )

    hold_for_seconds = 0
    hold_for_match = re.search(
        r"\b(?:hold|keep|sell)\b.{0,120}\bfor\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b",
        lower_text,
    )
    if not hold_for_match:
        hold_for_match = re.search(
            r"\bfor\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b.{0,80}\bthen\s+sell\b",
            lower_text,
        )
    if hold_for_match:
        hold_for_seconds = _duration_token_to_seconds(
            hold_for_match.group(1),
            hold_for_match.group(2),
        )
    if cadence_sell_after_seconds <= 0 and hold_for_seconds > 0:
        cadence_sell_after_seconds = hold_for_seconds

    lot_match = re.search(r"\b(\d+(?:\.\d+)?)\s*lot(?:s)?\b", lower_text)
    fixed_lot_size: float | None = None
    if lot_match:
        try:
            fixed_lot_size = max(0.01, min(100.0, float(lot_match.group(1))))
        except (TypeError, ValueError):
            fixed_lot_size = None

    allocation_pct_free_margin: float | None = None
    pct_margin_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:my\s+)?(?:(?:free\s+)?margin)\b",
        lower_text,
    )
    if pct_margin_match:
        try:
            allocation_pct_free_margin = max(0.1, min(100.0, float(pct_margin_match.group(1))))
        except (TypeError, ValueError):
            allocation_pct_free_margin = None

    min_rr = 1.8
    if mode == "safe":
        min_rr = 2.2
    elif mode == "aggressive":
        min_rr = 1.6

    warnings: list[str] = []
    if not allowed_symbols and not asset_classes:
        warnings.append("No exact symbol or market class detected. Strategy will use the currently enabled market universe unless you mention a symbol like XAUUSD or a class like commodities.")
    if market_selection_mode == "top_commodity":
        warnings.append("Top commodity mode will scan enabled commodities and trade the strongest qualified commodity setup each cycle.")
    if "lot" in lower_text and fixed_lot_size is None:
        warnings.append("Lot size mention was unclear, so runtime will fall back to risk-sized volume.")
    if "margin" in lower_text and "%" in lower_text and allocation_pct_free_margin is None:
        warnings.append("Margin allocation was mentioned but could not be parsed as a percentage.")
    if "every" in lower_text and cadence_buy_every_seconds <= 0:
        warnings.append("Repeat cadence was mentioned but could not be parsed into seconds.")
    if any(token in lower_text for token in ["after", "for"]) and "sell" in lower_text and cadence_sell_after_seconds <= 0:
        warnings.append("Exit timer was mentioned but could not be parsed into seconds.")

    assumptions: list[str] = []
    missing_details: list[str] = []
    if market_selection_mode == "top_commodity" and not allowed_symbols:
        assumptions.append("Treating 'top commodity' as the strongest qualified setup within the enabled commodity universe.")
    if market_region_hint == "egypt":
        assumptions.append("Using Egypt/Cairo wording as a session preference hint, not as a broker region restriction.")
    if fixed_lot_size is None and allocation_pct_free_margin is None:
        missing_details.append("No fixed lot size or margin allocation was provided, so sizing will follow normal risk-based volume.")

    human_summary_parts = [
        f"This strategy will {entry_action} ",
        f"{', '.join(allowed_symbols)}" if allowed_symbols else (
            "the top commodity setup" if market_selection_mode == "top_commodity" else
            f"{asset_classes[0].lower()} markets" if asset_classes else
            "the enabled market universe"
        ),
    ]
    if allocation_pct_free_margin is not None:
        human_summary_parts.append(f" using about {allocation_pct_free_margin:.2f}% of free margin")
    elif fixed_lot_size is not None:
        human_summary_parts.append(f" with a fixed lot size of {fixed_lot_size:.2f}")
    if cadence_buy_every_seconds > 0:
        human_summary_parts.append(f", repeating every {_format_strategy_duration(cadence_buy_every_seconds)}")
    if cadence_sell_after_seconds > 0:
        human_summary_parts.append(f", and force-closing after {_format_strategy_duration(cadence_sell_after_seconds)}")
    if apply_under_any_conditions:
        human_summary_parts.append(", with force execution enabled")
    human_summary = "".join(human_summary_parts).strip().rstrip(",") + "."

    summary_lines = [
        f"Symbols: {', '.join(allowed_symbols) if allowed_symbols else 'All enabled markets'}",
        f"Market selection: {market_selection_mode or 'named_symbols'}",
        f"Asset classes: {', '.join(asset_classes) if asset_classes else 'Any active class'}",
        f"Entry action: {entry_action}",
        f"Fixed lot: {fixed_lot_size if fixed_lot_size is not None else 'risk-sized'}",
        f"Margin allocation: {f'{allocation_pct_free_margin:.2f}% of free margin' if allocation_pct_free_margin is not None else 'Not specified'}",
        f"Buy cadence: {_format_strategy_duration(cadence_buy_every_seconds or 0)}",
        f"Sell timer: {_format_strategy_duration(cadence_sell_after_seconds or 0)}",
        f"Force mode: {'ON' if apply_under_any_conditions else 'OFF'}",
        f"Policy mode: {mode}",
        f"Gemini role: {gemini_role}",
        f"Scan interval: {scan_interval}s",
        f"Region hint: {market_region_hint or 'None'}",
    ]

    compiled = {
        "version": 1,
        "intent": _sanitize_custom_strategy_name(name),
        "symbols": allowed_symbols,
        "asset_classes": asset_classes,
        "market_selection_mode": market_selection_mode,
        "market_region_hint": market_region_hint,
        "entry_action": entry_action,
        "fixed_lot_size": fixed_lot_size,
        "allocation_pct_free_margin": allocation_pct_free_margin,
        "cadence_buy_every_seconds": int(max(0, cadence_buy_every_seconds)),
        "cadence_sell_after_seconds": int(max(0, cadence_sell_after_seconds)),
        "hold_for_seconds": int(max(0, hold_for_seconds)),
        "apply_under_any_conditions": bool(apply_under_any_conditions),
        "policy_mode": mode,
        "gemini_role": gemini_role,
        "scan_interval_seconds": scan_interval,
        "min_reward_risk": min_rr,
        "human_summary": human_summary,
        "assumptions": assumptions,
        "missing_details": missing_details,
        "warnings": warnings,
        "summary_lines": summary_lines,
    }

    long_brief = (
        "How this strategy works:\n"
        f"1) Strategy intent: {name}.\n"
        f"2) User plan summary: {description}\n"
        f"3) Policy mode: {mode}.\n"
        f"4) Market focus: {', '.join(allowed_symbols) if allowed_symbols else ('top commodity from enabled commodities' if market_selection_mode == 'top_commodity' else 'All enabled markets (no symbol filter specified)')}.\n"
        f"5) Scan behavior: every {scan_interval}s with SmartAgent baseline analysis, Gemini primary decisioning, and deterministic risk gating.\n"
        f"6) AI behavior: Gemini is the primary trade brain in '{gemini_role}' mode, using chart structure, relevant news, built-in analysis, meta-model insight, and trade history while still respecting execution guards.\n"
        f"7) Override mode: {'ON (force execution mode enabled)' if apply_under_any_conditions else 'OFF (normal filtering)'}.\n"
        f"8) Risk guardrails: minimum reward/risk {min_rr:.1f}, spread checks, margin checks, cooldown/duplicate protections, and session validity checks.\n"
        f"9) Cadence: {entry_action} every {_format_strategy_duration(cadence_buy_every_seconds or 0)}, sell after {_format_strategy_duration(cadence_sell_after_seconds or 0)} (0 = not enforced).\n"
        f"10) Sizing: {fixed_lot_size if fixed_lot_size is not None else (f'{allocation_pct_free_margin:.2f}% of free margin' if allocation_pct_free_margin is not None else 'risk-sized')}.\n"
        f"11) Human summary: {human_summary}\n"
        "12) Execution rule: trades execute from compiled strategy config, not repeated text parsing."
    )

    return {
        "agent_name": "GeminiAgent",
        "policy_mode": mode,
        "allowed_symbols": allowed_symbols or _strategy_scope_from_compiled(compiled),
        "scan_interval_seconds": scan_interval,
        "gemini_role": gemini_role,
        "apply_under_any_conditions": bool(apply_under_any_conditions),
        "min_reward_risk": min_rr,
        "cadence_buy_every_seconds": int(max(0, cadence_buy_every_seconds)),
        "cadence_sell_after_seconds": int(max(0, cadence_sell_after_seconds)),
        "fixed_lot_size": fixed_lot_size,
        "allocation_pct_free_margin": allocation_pct_free_margin,
        "hold_for_seconds": int(max(0, hold_for_seconds)),
        "market_selection_mode": market_selection_mode,
        "asset_classes": asset_classes,
        "market_region_hint": market_region_hint,
        "cadence_entry_action": entry_action,
        "brief": long_brief,
        "compiled_strategy": compiled,
    }


def _normalize_strategy_config(name: str, description: str, config: Optional[dict]) -> dict:
    cfg = dict(config or {})
    compiled = _compile_strategy_definition(
        name,
        description,
        apply_under_any_conditions=bool(cfg.get("apply_under_any_conditions", False)),
    )
    merged = dict(compiled)
    merged.update({k: v for k, v in cfg.items() if v not in (None, "", [])})
    if "compiled_strategy" in cfg and isinstance(cfg.get("compiled_strategy"), dict):
        compiled_payload = dict(compiled["compiled_strategy"])
        compiled_payload.update({k: v for k, v in dict(cfg.get("compiled_strategy") or {}).items() if v not in (None, "", [])})
        merged["compiled_strategy"] = compiled_payload
    merged["allowed_symbols"] = merged.get("allowed_symbols") or _strategy_scope_from_compiled(dict(merged.get("compiled_strategy") or {}))
    return merged


def _strategy_ai_overrides(ai_interpretation: Optional[dict]) -> dict:
    hints = dict(ai_interpretation or {})
    ai_used_clean = bool(hints.get("used")) and not bool(hints.get("degraded")) and not bool(hints.get("error"))
    compiled_hint = {
        "symbols": [str(sym).strip().upper() for sym in (hints.get("symbols") or []) if str(sym).strip()],
        "asset_classes": [str(item).strip().title() for item in (hints.get("asset_classes") or []) if str(item).strip()],
        "market_selection_mode": str(hints.get("market_selection_mode") or "").strip().lower() or None,
        "market_region_hint": str(hints.get("market_region_hint") or "").strip().lower() or None,
        "entry_action": str(hints.get("entry_action") or "").strip().upper() or None,
        "fixed_lot_size": hints.get("fixed_lot_size"),
        "allocation_pct_free_margin": hints.get("allocation_pct_free_margin"),
        "cadence_buy_every_seconds": hints.get("cadence_buy_every_seconds"),
        "cadence_sell_after_seconds": hints.get("cadence_sell_after_seconds"),
        "hold_for_seconds": hints.get("hold_for_seconds"),
        "policy_mode": hints.get("policy_mode"),
        "gemini_role": hints.get("gemini_role"),
        "scan_interval_seconds": hints.get("scan_interval_seconds"),
        "min_reward_risk": hints.get("min_reward_risk"),
        "human_summary": str(hints.get("human_summary") or "").strip() or None,
        "assumptions": [str(item).strip() for item in (hints.get("assumptions") or []) if str(item).strip()],
        "missing_details": [str(item).strip() for item in (hints.get("missing_details") or []) if str(item).strip()],
    }
    clean_compiled = {k: v for k, v in compiled_hint.items() if v not in (None, "", [])}
    return {
        "allowed_symbols": clean_compiled.get("symbols"),
        "asset_classes": clean_compiled.get("asset_classes"),
        "market_selection_mode": clean_compiled.get("market_selection_mode"),
        "market_region_hint": clean_compiled.get("market_region_hint"),
        "cadence_entry_action": clean_compiled.get("entry_action"),
        "fixed_lot_size": clean_compiled.get("fixed_lot_size"),
        "allocation_pct_free_margin": clean_compiled.get("allocation_pct_free_margin"),
        "cadence_buy_every_seconds": clean_compiled.get("cadence_buy_every_seconds"),
        "cadence_sell_after_seconds": clean_compiled.get("cadence_sell_after_seconds"),
        "hold_for_seconds": clean_compiled.get("hold_for_seconds"),
        "policy_mode": clean_compiled.get("policy_mode"),
        "gemini_role": clean_compiled.get("gemini_role"),
        "scan_interval_seconds": clean_compiled.get("scan_interval_seconds"),
        "min_reward_risk": clean_compiled.get("min_reward_risk"),
        "compiled_strategy": clean_compiled,
        "ai_interpretation": hints,
        "strategy_builder_source": "gemini" if ai_used_clean else "deterministic",
    }


async def _compile_strategy_config_async(
    name: str,
    description: str,
    *,
    apply_under_any_conditions: bool,
    preview_mode: bool = False,
) -> dict:
    ai_interpretation: dict = {}
    try:
        ai_interpretation = await gemini_strategy_compiler_service.compile(
            name=name,
            description=description,
            apply_under_any_conditions=apply_under_any_conditions,
            timeout_seconds=5.5 if preview_mode else None,
            max_retries=0 if preview_mode else None,
        )
    except Exception as exc:
        logger.warning("Gemini strategy compiler failed, using deterministic parser: %s", exc)
        ai_interpretation = {
            "used": False,
            "available": bool(getattr(gemini_strategy_compiler_service, "available", False)),
            "degraded": True,
            "notes": str(exc),
        }

    base_config = {
        "apply_under_any_conditions": bool(apply_under_any_conditions),
    }
    ai_overrides = _strategy_ai_overrides(ai_interpretation)
    config = _normalize_strategy_config(name, description, {**base_config, **ai_overrides})
    config["apply_under_any_conditions"] = bool(apply_under_any_conditions)
    compiled_payload = dict(config.get("compiled_strategy") or {})
    compiled_payload["apply_under_any_conditions"] = bool(apply_under_any_conditions)
    summary_lines = list(compiled_payload.get("summary_lines") or [])
    updated_summary_lines: list[str] = []
    force_replaced = False
    for line in summary_lines:
        if str(line).lower().startswith("force mode:"):
            updated_summary_lines.append(f"Force mode: {'ON' if apply_under_any_conditions else 'OFF'}")
            force_replaced = True
        else:
            updated_summary_lines.append(line)
    if not force_replaced:
        updated_summary_lines.append(f"Force mode: {'ON' if apply_under_any_conditions else 'OFF'}")
    compiled_payload["summary_lines"] = updated_summary_lines
    config["compiled_strategy"] = compiled_payload
    config["ai_interpretation"] = ai_interpretation
    config["strategy_builder_source"] = ai_overrides.get("strategy_builder_source") or "deterministic"
    return config


def _strategy_response_item(profile: dict, *, selected_id: str) -> dict:
    cfg = _normalize_strategy_config(
        str(profile.get("name") or ""),
        str(profile.get("description") or ""),
        dict(profile.get("config") or {}),
    )
    brief = str(cfg.get("brief") or profile.get("description") or "").strip()
    return {
        "id": str(profile.get("id") or ""),
        "name": str(profile.get("name") or ""),
        "description": str(profile.get("description") or ""),
        "brief": brief,
        "scope": str(profile.get("scope") or "custom"),
        "is_default": bool(profile.get("is_default", False)),
        "is_selected": str(profile.get("id") or "") == str(selected_id or ""),
        "config": cfg,
    }


def _resolve_strategy_from_profiles(strategy_id: str, profiles: list[dict]) -> Optional[dict]:
    for profile in profiles:
        if str(profile.get("id") or "") == str(strategy_id or ""):
            return profile
    return None


def _apply_strategy_runtime(profile: dict, *, preserve_user_policy: bool = False):
    global active_agent_name
    strategy_config = _normalize_strategy_config(
        str(profile.get("name") or ""),
        str(profile.get("description") or ""),
        dict(profile.get("config") or {}),
    )
    compiled = dict(strategy_config.get("compiled_strategy") or {})

    agent_name = str(strategy_config.get("agent_name") or "GeminiAgent")
    if agent_name not in agents:
        agent_name = "GeminiAgent" if "GeminiAgent" in agents else "SmartAgent"
    active_agent_name = agent_name
    auto_trader._auto_agent_name = agent_name

    mode = str(strategy_config.get("policy_mode") or "balanced").lower()
    if mode not in {"safe", "balanced", "aggressive"}:
        mode = "balanced"

    if preserve_user_policy:
        policy = risk_engine.user_policy
    else:
        risk_engine.apply_policy_preset(mode)
        policy = risk_engine.user_policy
    allowed_symbols = strategy_config.get("allowed_symbols") or _strategy_scope_from_compiled(compiled)
    if isinstance(allowed_symbols, list):
        normalized_symbols = [str(x).strip().upper() for x in allowed_symbols if str(x).strip()]
        policy = policy.model_copy(update={"allowed_symbols": normalized_symbols})
        if normalized_symbols:
            allowed_canonical = {universe_service.canonical_symbol(s) for s in normalized_symbols}
            auto_trader._trade_log = [
                item for item in auto_trader._trade_log
                if universe_service.canonical_symbol(str(item.get("symbol") or "")) in allowed_canonical
            ]

    gemini_role = str(strategy_config.get("gemini_role") or policy.gemini_role)
    if gemini_role not in {"off", "advisory", "confirmation-required"}:
        gemini_role = policy.gemini_role
    policy = policy.model_copy(update={"gemini_role": gemini_role})

    force_execute_any_conditions = bool(strategy_config.get("apply_under_any_conditions", False))
    policy = policy.model_copy(update={"force_execute_any_conditions": force_execute_any_conditions})

    min_rr = strategy_config.get("min_reward_risk")
    if min_rr is not None:
        try:
            rr = max(1.2, min(4.0, float(min_rr)))
            policy = policy.model_copy(update={"min_reward_risk": rr})
        except (TypeError, ValueError):
            pass
    risk_engine.update_user_policy(policy)

    interval = strategy_config.get("scan_interval_seconds")
    if interval is not None:
        try:
            risk_engine.auto_trade_scan_interval_seconds = max(15, min(60, int(interval)))
        except (TypeError, ValueError):
            pass

    try:
        auto_trader._cadence_buy_every_seconds = max(
            0,
            int(compiled.get("cadence_buy_every_seconds") or strategy_config.get("cadence_buy_every_seconds") or 0),
        )
    except (TypeError, ValueError):
        auto_trader._cadence_buy_every_seconds = 0
    try:
        auto_trader._cadence_sell_after_seconds = max(
            0,
            int(compiled.get("cadence_sell_after_seconds") or strategy_config.get("cadence_sell_after_seconds") or 0),
        )
    except (TypeError, ValueError):
        auto_trader._cadence_sell_after_seconds = 0
    try:
        auto_trader._fixed_lot_size = max(
            0.0,
            float(compiled.get("fixed_lot_size") or strategy_config.get("fixed_lot_size") or 0.0),
        )
    except (TypeError, ValueError):
        auto_trader._fixed_lot_size = 0.0
    try:
        auto_trader._allocation_pct_free_margin = max(
            0.0,
            min(100.0, float(compiled.get("allocation_pct_free_margin") or strategy_config.get("allocation_pct_free_margin") or 0.0)),
        )
    except (TypeError, ValueError):
        auto_trader._allocation_pct_free_margin = 0.0
    auto_trader._market_selection_mode = str(
        compiled.get("market_selection_mode") or strategy_config.get("market_selection_mode") or ""
    ).strip().lower()
    auto_trader._market_region_hint = str(
        compiled.get("market_region_hint") or strategy_config.get("market_region_hint") or ""
    ).strip().lower()
    auto_trader._cadence_entry_action = str(
        compiled.get("entry_action") or strategy_config.get("cadence_entry_action") or "BUY"
    ).upper()
    hold_seconds = int(compiled.get("hold_for_seconds") or strategy_config.get("hold_for_seconds") or 0)
    if hold_seconds > 0 and auto_trader._cadence_sell_after_seconds <= 0:
        auto_trader._cadence_sell_after_seconds = hold_seconds
    auto_trader.position_manager.manage_interval_seconds = 2
    human_summary = str(
        compiled.get("human_summary")
        or strategy_config.get("human_summary")
        or ""
    ).strip()
    summary_lines = [
        str(line).strip()
        for line in (
            compiled.get("summary_lines")
            or strategy_config.get("summary_lines")
            or []
        )
        if str(line).strip()
    ]
    if str(profile.get("id") or "") == "builtin-default-smart":
        if not human_summary:
            human_summary = (
                "This strategy scans the enabled market universe and chooses BUY, SELL, or HOLD "
                "per symbol based on technicals, news, meta-model review, and risk checks."
            )
        if not summary_lines:
            summary_lines = [
                "Symbols: All enabled markets",
                "Market selection: dynamic multi-asset scan",
                "Asset classes: Any active class",
                "Entry action: Dynamic (BUY / SELL / HOLD per symbol)",
                "Fixed lot: risk-sized",
                "Margin allocation: Not specified",
                "Buy cadence: 0s",
                "Sell timer: 0s",
                "Force mode: OFF",
                "Policy mode: balanced",
                "Gemini role: advisory",
                "Scan interval: 30s",
                "Region hint: None",
            ]
    risk_engine.active_strategy_context = {
        "id": str(profile.get("id") or ""),
        "name": str(profile.get("name") or ""),
        "description": str(profile.get("description") or ""),
        "brief": str(strategy_config.get("brief") or profile.get("description") or "").strip(),
        "policy_mode": mode,
        "gemini_role": gemini_role,
        "compiled_strategy": compiled,
        "human_summary": human_summary,
        "summary_lines": summary_lines,
    }


async def _sync_active_strategy_runtime_for_user(
    user_id: str = "local-anonymous",
    *,
    preserve_user_policy: bool = False,
):
    try:
        selected_strategy_id = await _get_active_strategy_id_for_user(user_id)
        profiles = await _list_strategies_for_user(user_id)
        selected_profile = _resolve_strategy_from_profiles(selected_strategy_id, profiles)
        if selected_profile:
            _apply_strategy_runtime(selected_profile, preserve_user_policy=preserve_user_policy)
    except Exception as exc:
        logger.warning("Failed to sync active strategy runtime for %s: %s", user_id, exc)


def apply_runtime_gemini_credentials(*, api_key: Optional[str] = None, model_name: Optional[str] = None, source: str = "local") -> dict:
    key = (api_key or "").strip()
    model = (model_name or "").strip()
    if key:
        import os
        os.environ["GEMINI_API_KEY"] = key
        if hasattr(gemini_agent, "_runtime_last_error"):
            gemini_agent.clear_runtime_error()
        if hasattr(gemini_agent, "_quota_block_until"):
            gemini_agent._quota_block_until = 0.0
        if hasattr(gemini_agent, "_model_cooldown_until"):
            gemini_agent._model_cooldown_until = {}
        gemini_agent._init_client()
        gemini_event_classifier.api_key = key
        if hasattr(gemini_event_classifier, "_model_cooldown_until"):
            gemini_event_classifier._model_cooldown_until = {}
        if hasattr(gemini_event_classifier, "_quota_cooldown_until"):
            gemini_event_classifier._quota_cooldown_until = 0.0
        gemini_event_classifier._init_client()
        strategy_service = signal_pipeline.gemini_strategy_advisor_service
        strategy_service.api_key = key
        strategy_service._quota_cooldown_until = 0.0
        if hasattr(strategy_service, "_model_cooldown_until"):
            strategy_service._model_cooldown_until = {}
        strategy_service._init_client()
        gemini_strategy_compiler_service.api_key = key
        gemini_strategy_compiler_service._quota_cooldown_until = 0.0
        if hasattr(gemini_strategy_compiler_service, "_model_cooldown_until"):
            gemini_strategy_compiler_service._model_cooldown_until = {}
        gemini_strategy_compiler_service._init_client()
        _gemini_route_model_cooldowns.clear()
    _sync_route_gemini_cooldown(
        gemini_agent.model_name,
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    )
    if model:
        import os
        os.environ["GEMINI_MODEL"] = model
        gemini_agent.model_name = model
        gemini_agent.active_model_name = model
        gemini_event_classifier.model_name = model
        gemini_event_classifier.active_model_name = model
        signal_pipeline.gemini_strategy_advisor_service.model_name = model
        signal_pipeline.gemini_strategy_advisor_service.active_model_name = model
        gemini_strategy_compiler_service.model_name = model
        gemini_strategy_compiler_service.active_model_name = model
    return {
        "source": source,
        "api_key_loaded": bool(key),
        "api_key_masked": _mask_secret(key) if key else "",
        "model": model or gemini_agent.model_name,
        "gemini_available": bool(getattr(gemini_agent, "available", False)),
        "event_classifier_available": bool(getattr(gemini_event_classifier, "available", False)),
        "strategy_advisor_available": bool(getattr(signal_pipeline.gemini_strategy_advisor_service, "available", False)),
        "strategy_compiler_available": bool(getattr(gemini_strategy_compiler_service, "available", False)),
    }


async def hydrate_gemini_credentials_from_cloud() -> dict:
    if not cloud_secret_service.configured:
        return {"source": "local", "configured": False, "loaded": False}
    try:
        key = await asyncio.to_thread(
            secret_store.get_cloud_secret,
            "GEMINI_API_KEY",
            migrate_plaintext=True,
        )
        model = await asyncio.to_thread(
            secret_store.get_cloud_secret,
            "GEMINI_MODEL",
            migrate_plaintext=True,
        )
        key = str(key or "").strip()
        model = str(model or "").strip()
        if not key:
            return {"source": "cloud", "configured": True, "loaded": False, "reason": "GEMINI_API_KEY not found in cloud secrets."}
        applied = apply_runtime_gemini_credentials(api_key=key, model_name=(model or None), source="cloud")
        return {"source": "cloud", "configured": True, "loaded": True, **applied}
    except Exception as exc:
        logger.warning("Cloud Gemini credential hydration failed: %s", exc)
        return {"source": "cloud", "configured": True, "loaded": False, "reason": str(exc)}


def _recommended_trade_amount_from_free_margin(
    *,
    free_margin: float,
    policy_mode: str,
    confidence: float,
    quality_score: float | None,
    ready_to_execute: bool,
) -> tuple[float, float]:
    if not ready_to_execute:
        return 0.0, 0.0
    if free_margin <= 0:
        return 0.0, 0.0

    mode = str(policy_mode or "balanced").lower()
    base_pct_by_mode = {
        "safe": 0.50,
        "balanced": 1.00,
        "aggressive": 1.50,
    }
    base_pct = base_pct_by_mode.get(mode, 1.00)
    signal_strength = max(0.0, min(1.0, float(quality_score if quality_score is not None else confidence)))
    strength_multiplier = 0.80 + (signal_strength * 0.40)  # 0.80x .. 1.20x
    readiness_multiplier = 1.00 if ready_to_execute else 0.70

    pct = base_pct * strength_multiplier * readiness_multiplier
    pct = max(0.25, min(2.50, pct))

    raw_amount = free_margin * (pct / 100.0)
    min_amount = 25.0
    max_amount = max(min_amount, free_margin * 0.10)  # keep within 10% of free margin
    amount = max(min_amount, min(max_amount, raw_amount))
    amount = round(amount, 2)
    pct_effective = round((amount / free_margin) * 100.0, 2) if free_margin > 0 else 0.0
    return amount, pct_effective


def _commission_snapshot_from_symbol_info(symbol_info: Optional[dict]) -> dict:
    info = dict(symbol_info or {})
    per_side = info.get("commission_per_lot_side")
    round_turn = info.get("commission_round_turn_per_lot")
    model = str(info.get("commission_model") or "unknown_or_zero")
    pct_rate = info.get("commission_percent_rate")
    one_lot_notional = info.get("commission_notional_1lot")
    try:
        per_side_val = float(per_side) if per_side is not None else None
    except (TypeError, ValueError):
        per_side_val = None
    try:
        round_turn_val = float(round_turn) if round_turn is not None else None
    except (TypeError, ValueError):
        round_turn_val = None
    try:
        pct_rate_val = float(pct_rate) if pct_rate is not None else None
    except (TypeError, ValueError):
        pct_rate_val = None
    try:
        notional_val = float(one_lot_notional) if one_lot_notional is not None else None
    except (TypeError, ValueError):
        notional_val = None
    return {
        "commission_per_lot_side": per_side_val,
        "commission_round_turn_per_lot": round_turn_val,
        "commission_model": model,
        "commission_percent_rate": pct_rate_val,
        "commission_notional_1lot": notional_val,
    }


def _commission_is_missing_or_zero(snapshot: dict) -> bool:
    per_side = snapshot.get("commission_per_lot_side")
    try:
        return per_side is None or float(per_side) <= 0.0
    except (TypeError, ValueError):
        return True


def _merge_commission_snapshot(base: dict, fallback: Optional[dict]) -> dict:
    if not fallback:
        return base
    if not _commission_is_missing_or_zero(base):
        return base
    merged = dict(base)
    merged.update({
        "commission_per_lot_side": fallback.get("commission_per_lot_side"),
        "commission_round_turn_per_lot": fallback.get("commission_round_turn_per_lot"),
        "commission_model": fallback.get("commission_model") or merged.get("commission_model"),
        "commission_percent_rate": fallback.get("commission_percent_rate"),
        "commission_notional_1lot": fallback.get("commission_notional_1lot"),
        "commission_samples": fallback.get("commission_samples"),
    })
    return merged


async def _on_position_manager_trade_closed(_payload: dict):
    _schedule_incremental_meta_training("position_manager_close")


async def _on_trade_outcome_logged(_payload: dict):
    _schedule_incremental_meta_training("trade_outcome_logged")


def set_database(database: Database):
    global db
    global research_cycle_service
    db = database
    db.set_trade_outcome_callback(_on_trade_outcome_logged)
    signal_pipeline.set_database(database)
    execution_service.db = database
    kill_switch_service.set_database(database)
    reconciliation_service.set_database(database)
    auto_trader.set_database(database)
    event_ingestion_service.set_database(database)
    research_cycle_service = ResearchCycleService(
        database,
        meta_model_service=signal_pipeline.meta_model_service,
    )
    auto_trader.position_manager.set_trade_closed_callback(_on_position_manager_trade_closed)
    asyncio.create_task(kill_switch_service.load())
    asyncio.create_task(_warm_saved_credentials_cache())


def current_research_cycle_service() -> Optional[ResearchCycleService]:
    return research_cycle_service


async def apply_cloud_brain_command(command: dict) -> dict:
    """Apply cloud command as high-level runtime policy controls only.

    Execution/risk checks remain local and deterministic.
    """
    global _cloud_brain_state
    cmd = dict(command or {})
    applied: dict[str, object] = {"updated": [], "rejected": []}

    try:
        mode = cmd.get("mode")
        if isinstance(mode, str) and mode.strip().lower() in {"safe", "balanced", "aggressive"}:
            policy = risk_engine.user_policy.model_copy(update={"mode": mode.strip().lower()})
            risk_engine.update_user_policy(policy)
            applied["updated"].append("mode")

        if "allowed_symbols" in cmd and isinstance(cmd.get("allowed_symbols"), list):
            symbols = [str(s).strip().upper() for s in cmd.get("allowed_symbols", []) if str(s).strip()]
            policy = risk_engine.user_policy.model_copy(update={"allowed_symbols": symbols})
            risk_engine.update_user_policy(policy)
            applied["updated"].append("allowed_symbols")

        if "auto_trade_enabled" in cmd:
            requested_enabled = bool(cmd.get("auto_trade_enabled"))
            if requested_enabled:
                live_policy_blocked = (
                    not connector.is_demo()
                    and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED)
                )
                if not _active_platform_connected() or live_policy_blocked:
                    rejected = applied.setdefault("rejected", [])
                    if isinstance(rejected, list):
                        rejected.append("auto_trade_enabled")
                else:
                    risk_engine.auto_trade_enabled = True
                    if not auto_trader.is_running:
                        task_orchestrator.start_auto_trade()
                    applied["updated"].append("auto_trade_enabled")
            else:
                risk_engine.auto_trade_enabled = False
                _arm_auto_trade_session(False)
                if auto_trader.is_running:
                    task_orchestrator.stop_auto_trade()
                applied["updated"].append("auto_trade_enabled")

        if "scan_interval_seconds" in cmd:
            try:
                interval = int(cmd.get("scan_interval_seconds"))
                risk_engine.auto_trade_scan_interval_seconds = max(30, min(interval, 300))
                applied["updated"].append("scan_interval_seconds")
            except (TypeError, ValueError):
                pass

        if "panic_stop" in cmd:
            panic_enabled = bool(cmd.get("panic_stop"))
            if panic_enabled:
                risk_engine.panic_stopped = True
                task_orchestrator.stop_auto_trade()
            else:
                risk_engine.panic_stopped = False
            applied["updated"].append("panic_stop")

        await _persist_runtime_state()
        _cloud_brain_state = {
            "last_command_at": time.time(),
            "last_command_payload": cmd,
            "last_apply_result": applied,
            "last_error": None,
        }
        return applied
    except Exception as exc:
        _cloud_brain_state = {
            "last_command_at": time.time(),
            "last_command_payload": cmd,
            "last_apply_result": {},
            "last_error": str(exc),
        }
        raise


def cloud_brain_snapshot() -> dict:
    return dict(_cloud_brain_state or {})


def _runtime_state_payload() -> dict:
    return {
        "active_platform": str(active_platform or "mt5"),
        "user_policy": risk_engine.user_policy.model_dump(),
        "auto_trade_enabled": bool(risk_engine.auto_trade_enabled),
        "auto_trade_scan_interval_seconds": int(risk_engine.auto_trade_scan_interval_seconds),
        "auto_trade_budget_mode": str(getattr(risk_engine, "auto_trade_budget_mode", "full_margin") or "full_margin"),
        "auto_trade_budget_usd": (
            float(getattr(risk_engine, "auto_trade_budget_usd", 0.0) or 0.0)
            if getattr(risk_engine, "auto_trade_budget_usd", None) is not None
            else None
        ),
        "portfolio_targets": auto_trader.position_manager.portfolio_targets_snapshot(),
    }


def _arm_auto_trade_session(active: bool) -> None:
    risk_engine.auto_trade_session_armed = bool(active)


async def _persist_runtime_state() -> bool:
    if db is None:
        return False
    payload = _runtime_state_payload()
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                db.save_runtime_state(RUNTIME_STATE_KEY, payload),
                timeout=1.5,
            )
            return True
        except (asyncio.TimeoutError, sqlite3.OperationalError) as exc:
            last_error = exc
            await asyncio.sleep(0.2 * (attempt + 1))
        except Exception as exc:
            logger.warning("Runtime state persistence failed: %s", exc)
            return False
    logger.warning("Runtime state persistence skipped after retries: %s", last_error)
    return False


def _schedule_runtime_state_persist() -> None:
    asyncio.create_task(_persist_runtime_state())


def _schedule_connection_event(event_type: str, **kwargs) -> None:
    if db is None:
        return

    async def _runner():
        try:
            await db.log_connection_event(event_type, **kwargs)
        except (sqlite3.OperationalError, asyncio.TimeoutError) as exc:
            logger.warning("Connection event log skipped for %s: %s", event_type, exc)
        except Exception as exc:
            logger.warning("Connection event log failed for %s: %s", event_type, exc)

    asyncio.create_task(_runner())


async def _list_strategies_for_user(user_id: str) -> list[dict]:
    builtins = _builtin_strategy_profiles()
    if db is None:
        return builtins
    custom_rows = await db.list_user_strategies(owner_user_id=user_id, limit=100)
    custom_profiles = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "description": row.get("description", ""),
            "scope": "custom",
            "is_default": False,
            "config": dict(row.get("config_json") or {}),
        }
        for row in custom_rows
    ]
    return builtins + custom_profiles


async def _get_active_strategy_id_for_user(user_id: str) -> str:
    if db is None:
        return _default_strategy_id()
    state = await db.get_runtime_state(_strategy_state_key_for_user(user_id))
    strategy_id = str((state or {}).get("strategy_id") or "").strip()
    if not strategy_id and str(user_id) != "local-anonymous":
        fallback_state = await db.get_runtime_state(_strategy_state_key_for_user("local-anonymous"))
        strategy_id = str((fallback_state or {}).get("strategy_id") or "").strip()
    return strategy_id or _default_strategy_id()


async def _set_active_strategy_for_user(user_id: str, strategy_id: str):
    if db is None:
        return
    await db.save_runtime_state(
        _strategy_state_key_for_user(user_id),
        {"strategy_id": strategy_id},
    )
    # Keep a global fallback scope synchronized for flows that run without auth tokens.
    if str(user_id) != "local-anonymous":
        await db.save_runtime_state(
            _strategy_state_key_for_user("local-anonymous"),
            {"strategy_id": strategy_id},
        )


def _normalize_chat_history(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in messages[-200:]:
        role = str(item.get("role", "")).lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        normalized.append({"role": role, "content": content[:4000]})
    return normalized


async def _save_chat_history(messages: list[dict]):
    if db is None:
        return
    await db.save_runtime_state(
        CHAT_HISTORY_STATE_KEY,
        {"messages": _normalize_chat_history(messages)},
    )


async def _load_chat_history() -> list[dict]:
    if db is None:
        return []
    state = await db.get_runtime_state(CHAT_HISTORY_STATE_KEY)
    if not state:
        return []
    return _normalize_chat_history(state.get("messages", []))


async def restore_runtime_state():
    global active_platform
    if db is None:
        return
    state = await db.get_runtime_state(RUNTIME_STATE_KEY)
    if not state:
        return
    try:
        restored_platform = "mt5"
        active_platform = restored_platform
        _apply_active_broker_runtime("mt5")
        policy = UserPolicySettings(**(state.get("user_policy") or {}))
        risk_engine.update_user_policy(policy)
        saved_auto_trade_enabled = bool(state.get("auto_trade_enabled", False))
        risk_engine.auto_trade_enabled = False
        _arm_auto_trade_session(False)
        interval = int(state.get("auto_trade_scan_interval_seconds", 30))
        risk_engine.auto_trade_scan_interval_seconds = max(30, min(interval, 300))
        risk_engine.auto_trade_budget_mode = str(state.get("auto_trade_budget_mode", "full_margin") or "full_margin")
        raw_budget = state.get("auto_trade_budget_usd", None)
        risk_engine.auto_trade_budget_usd = float(raw_budget) if raw_budget is not None else None
        portfolio_targets = dict(state.get("portfolio_targets") or {})
        auto_trader.position_manager.set_portfolio_targets(
            stop_loss_usd=portfolio_targets.get("stop_loss_usd"),
            take_profit_usd=portfolio_targets.get("take_profit_usd"),
            mode=portfolio_targets.get("mode"),
            manual_override=False,
        )
        await _sync_active_strategy_runtime_for_user("local-anonymous", preserve_user_policy=True)
        # Keep protective position management always active after restore,
        # even when auto-trading itself is session-armed OFF.
        if connector.connected and not auto_trader.position_manager.is_running:
            auto_trader.position_manager.start()
        logger.info("Restored runtime state: mode=%s, auto_trade_enabled_saved=%s, current_enabled=%s, scan_interval=%ss",
                    risk_engine.user_policy.mode,
                    saved_auto_trade_enabled,
                    risk_engine.auto_trade_enabled,
                    risk_engine.auto_trade_scan_interval_seconds)
        if saved_auto_trade_enabled:
            logger.warning("Saved auto-trade state was ON, but auto-trade remains OFF until explicitly re-enabled in this session.")
    except Exception as exc:
        logger.warning("Failed to restore runtime state: %s", exc)


def _schedule_incremental_meta_training(trigger: str):
    if not config.AUTO_META_TRAINING_ENABLED:
        return
    service = current_research_cycle_service()
    if service is None:
        return

    async def _runner():
        try:
            if trigger in {"post_trade_analysis_saved", "startup_warmup"}:
                result = await service.maybe_train_from_post_analysis_update(
                    min_rows=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
                    cooldown_seconds=(
                        0 if trigger == "startup_warmup"
                        else max(120, int(config.AUTO_META_TRAIN_INTERVAL_SECONDS))
                    ),
                    auto_approve=config.AUTO_META_AUTO_APPROVE,
                    min_precision=config.AUTO_META_MIN_PRECISION,
                    min_f1=config.AUTO_META_MIN_F1,
                    min_positive_samples=config.AUTO_META_MIN_POSITIVE_SAMPLES,
                    target_column=config.AUTO_META_TARGET_COLUMN,
                )
            else:
                result = await service.maybe_train_from_new_trade_outcome(
                    min_rows=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
                    cooldown_seconds=max(60, int(config.AUTO_META_TRAIN_INTERVAL_SECONDS // 2)),
                    auto_approve=config.AUTO_META_AUTO_APPROVE,
                    min_precision=config.AUTO_META_MIN_PRECISION,
                    min_f1=config.AUTO_META_MIN_F1,
                    min_positive_samples=config.AUTO_META_MIN_POSITIVE_SAMPLES,
                    target_column=config.AUTO_META_TARGET_COLUMN,
                )
            logger.info("Incremental meta-training trigger=%s result=%s", trigger, result)
        except Exception as exc:
            logger.warning("Incremental meta-training failed after %s: %s", trigger, exc)

    asyncio.create_task(_runner())


def _requested_agent_from_final_name(agent_name: str) -> str:
    return "GeminiAgent" if "Gemini" in (agent_name or "") else "SmartAgent"


def _resolve_market_symbol(symbol: str) -> str:
    requested_raw = (symbol or "").strip()
    requested = requested_raw.upper()
    broker_aliases: dict[str, list[str]] = {
        "GER40": ["DE40", "DAX", "GER30", "GER30M", "GDAXIM"],
        "DAX": ["DE40", "GER30", "GER30M", "GDAXIM", "GER40"],
        "DE40": ["GER40", "DAX", "GER30", "GER30M", "GDAXIM"],
        "UK100": ["FTSE100", "FTSE"],
        "FTSE100": ["UK100", "FTSE"],
        "US500": ["SP500", "SPX500"],
        "SP500": ["US500", "SPX500"],
        "US100": ["NAS100"],
        "NAS100": ["US100"],
        "US30": ["DJ30"],
        "DJ30": ["US30"],
        "WTID": ["USOIL", "XTIUSD"],
        "WTI": ["USOIL", "XTIUSD"],
        "BRENT": ["UKOIL", "XBRUSD"],
        "UKOIL": ["BRENT", "XBRUSD"],
        "XBRUSD": ["UKOIL", "BRENT"],
    }

    def _try_symbol(candidate: str) -> str | None:
        value = (candidate or "").strip()
        if not value:
            return None
        info = market_data.get_symbol_info(value)
        if info:
            return str(info.get("name") or value).strip() or value
        if market_data.enable_symbol(value):
            info = market_data.get_symbol_info(value)
            if info:
                return str(info.get("name") or value).strip() or value
        return None

    def _has_live_tick(candidate: str) -> bool:
        tick = market_data.get_tick(candidate)
        return _is_fresh_tick(tick)

    normalized = universe_service.canonical_symbol(symbol)
    energy_alias_requested = requested in {"WTI", "WTID", "USOIL", "XTIUSD", "BRENT", "UKOIL", "XBRUSD"} or normalized in {"WTI", "BRENT"}

    if energy_alias_requested:
        for candidate in broker_aliases.get(requested, []) + broker_aliases.get(normalized, []):
            resolved = _try_symbol(candidate)
            if resolved and _has_live_tick(resolved):
                return resolved

    # MT5 parity fix: prefer the exact symbol user requested when broker exposes it.
    # This prevents alias remaps like XAUUSD -> GOLD when both are available.
    # Energy names are special: WTI/BRENT can be stock tickers on MT5, not crude oil.
    if not energy_alias_requested:
        direct = _try_symbol(requested_raw or requested)
        if direct and _has_live_tick(direct):
            return direct

        normalized_direct = _try_symbol(normalized)
        if normalized_direct and _has_live_tick(normalized_direct):
            return normalized_direct

    for candidate in broker_aliases.get(requested, []):
        resolved = _try_symbol(candidate)
        if resolved and _has_live_tick(resolved):
            return resolved
    if normalized and normalized != requested:
        for candidate in broker_aliases.get(normalized, []):
            resolved = _try_symbol(candidate)
            if resolved and _has_live_tick(resolved):
                return resolved

    # Fallback path only when direct symbol lookup fails.
    all_symbols = market_data.get_all_symbols()
    if requested:
        matched_row = next(
            (
                item
                for item in all_symbols
                if str(item.get("name", "")).strip().upper() == requested
            ),
            None,
        )
        # Do not lock to a stale/untradeable alias row (e.g. GER40) if the broker
        # cannot provide executable ticks for it. Let alias resolution choose a
        # live tradable symbol (e.g. DAX) instead.
        if matched_row and (
            bool(matched_row.get("trade_enabled"))
            or bool(matched_row.get("visible"))
        ):
            direct_tick = market_data.get_tick(requested_raw or requested)
            if direct_tick and float(getattr(direct_tick, "bid", 0.0) or 0.0) > 0 and float(getattr(direct_tick, "ask", 0.0) or 0.0) > 0:
                return str(getattr(direct_tick, "symbol", "") or requested_raw or requested)
    resolved = universe_service.resolve_requested_symbols([requested or normalized], all_symbols)
    if resolved:
        candidate = str(resolved[0] or "").strip()
        if candidate:
            validated = _try_symbol(candidate)
            if validated and _has_live_tick(validated):
                return validated

    # Last-resort alias pass over all symbols to avoid returning stale canonical
    # names (like GER40) when only broker aliases (like DAX) are executable.
    alias_candidates: list[str] = []
    alias_candidates.extend(broker_aliases.get(requested, []))
    if normalized:
        alias_candidates.extend(broker_aliases.get(normalized, []))
    for item in all_symbols:
        raw_name = str(item.get("name", "") or "").strip().upper()
        if not raw_name:
            continue
        item_canonical = universe_service.canonical_symbol(raw_name)
        if item_canonical in {requested, normalized}:
            alias_candidates.append(raw_name)
    seen_alias: set[str] = set()
    for alias in alias_candidates:
        key = str(alias or "").strip().upper()
        if not key or key in seen_alias:
            continue
        seen_alias.add(key)
        validated = _try_symbol(key)
        if validated and _has_live_tick(validated):
            return validated

    return normalized


def _is_fresh_tick(tick, *, max_age_seconds: float = 900.0) -> bool:
    if tick is None:
        return False
    try:
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
    except Exception:
        return False
    if bid <= 0.0 or ask <= 0.0:
        return False
    try:
        raw_msc = float(getattr(tick, "time_msc", 0.0) or 0.0)
        raw_sec = float(getattr(tick, "time", 0.0) or 0.0)
    except Exception:
        raw_msc = 0.0
        raw_sec = 0.0
    tick_seconds = (raw_msc / 1000.0) if raw_msc > 1_000_000_000_000 else raw_sec
    if tick_seconds <= 0:
        return False
    return (time.time() - tick_seconds) <= max_age_seconds


def _tick_timestamp_seconds(tick) -> float:
    if tick is None:
        return 0.0
    try:
        raw_msc = float(getattr(tick, "time_msc", 0.0) or 0.0)
        raw_sec = float(getattr(tick, "time", 0.0) or 0.0)
    except Exception:
        return 0.0
    if raw_msc > 1_000_000_000_000:
        return raw_msc / 1000.0
    return raw_sec if raw_sec > 0.0 else 0.0


def _reward_risk_ratio(entry_price: float, stop_loss: float | None, take_profit: float | None) -> float:
    if entry_price <= 0 or stop_loss is None or take_profit is None:
        return 0.0
    sl_distance = abs(entry_price - stop_loss)
    tp_distance = abs(take_profit - entry_price)
    return tp_distance / sl_distance if sl_distance > 0 else 0.0


def _trade_outcome_context(plan_record: dict | None, existing_position=None) -> dict:
    plan_json = plan_record.get("plan_json", {}) if plan_record else {}
    plan_meta = plan_json.get("metadata", {}) if isinstance(plan_json, dict) else {}
    opened_at = existing_position.time if existing_position else time.time()
    return {
        "symbol": (plan_record.get("symbol", "") if plan_record else "") or (existing_position.symbol if existing_position else ""),
        "action": (plan_record.get("action", "") if plan_record else "") or (existing_position.type if existing_position else ""),
        "holding_minutes": max(0.0, (time.time() - opened_at) / 60.0),
        "symbol_category": plan_meta.get("symbol_category", ""),
        "strategy": plan_json.get("strategy") or plan_meta.get("strategy_family", ""),
        "planned_hold_minutes": (
            plan_json.get("planned_hold_minutes")
            or plan_json.get("expected_hold_minutes")
            or plan_meta.get("planned_hold_minutes")
        ),
    }


def _build_execution_trace_metadata(
    *,
    source: str,
    symbol: str,
    action: str,
    signal_id: int | None = None,
    reason: str = "",
    requested_agent_name: str = "",
    amount_usd: float | None = None,
    preflight_reason: str = "",
    signal_snapshot: dict | None = None,
) -> dict:
    strategy_ctx = dict(getattr(risk_engine, "active_strategy_context", {}) or {})
    gemini_status = _current_gemini_status()
    meta_service = getattr(signal_pipeline, "meta_model_service", None)
    metadata: dict = {
        "source": str(source or "").strip() or "unknown",
        "symbol": str(symbol or "").strip().upper(),
        "action": str(action or "").strip().upper(),
        "signal_id": signal_id,
        "reason": str(reason or "").strip(),
        "requested_agent_name": str(requested_agent_name or active_agent_name or "").strip(),
        "active_agent_name": str(active_agent_name or "").strip(),
        "policy_mode": str(getattr(risk_engine.user_policy, "mode", "") or strategy_ctx.get("policy_mode") or "").strip(),
        "amount_usd": float(amount_usd) if amount_usd is not None and float(amount_usd) > 0 else None,
        "preflight_reason": str(preflight_reason or "").strip(),
        "cloud_decision_enabled": bool(config.CLOUD_BRAIN_DECISION_ENABLED and str(config.CLOUD_BRAIN_DECISION_URL or "").strip()),
        "strategy": {
            "id": str(strategy_ctx.get("id") or "").strip(),
            "name": str(strategy_ctx.get("name") or "").strip(),
            "brief": str(strategy_ctx.get("brief") or "").strip(),
            "human_summary": str(strategy_ctx.get("human_summary") or "").strip(),
            "summary_lines": [str(line).strip() for line in (strategy_ctx.get("summary_lines") or []) if str(line).strip()],
        },
        "gemini": {
            "state": str(gemini_status.get("state") or "").strip(),
            "active_model": str(gemini_status.get("active_model") or "").strip(),
            "available": bool(gemini_status.get("available")),
            "cooldown_seconds": int(gemini_status.get("cooldown_seconds") or 0),
        },
        "meta_model": {
            "active": bool(getattr(meta_service, "is_active", False)) if meta_service is not None else False,
            "version_id": str(getattr(meta_service, "active_version_id", "") or "") if meta_service is not None else "",
        },
    }
    if signal_snapshot:
        metadata["signal_snapshot"] = {
            "agent_name": str(signal_snapshot.get("agent_name") or "").strip(),
            "confidence": float(signal_snapshot.get("confidence") or 0.0),
            "reason": str(signal_snapshot.get("reason") or "").strip(),
            "stop_loss": signal_snapshot.get("stop_loss"),
            "take_profit": signal_snapshot.get("take_profit"),
        }
    return metadata


async def _store_management_plan(
    ticket: int,
    signal_id: int | None,
    symbol: str,
    action: str,
    decision,
    *,
    execution_trace: dict | None = None,
):
    if db is None or ticket is None:
        return
    plan_payload = decision.position_management_plan.model_dump()
    if execution_trace:
        metadata = dict(plan_payload.get("metadata") or {})
        metadata["execution_trace"] = execution_trace
        plan_payload["metadata"] = metadata
    await db.save_position_management_plan(
        ticket=ticket,
        signal_id=signal_id,
        symbol=symbol,
        action=action,
        plan=plan_payload,
    )


async def _store_execution_trace_fallback_plan(
    ticket: int,
    signal_id: int | None,
    symbol: str,
    action: str,
    *,
    execution_trace: dict,
):
    if db is None or ticket is None:
        return
    await db.save_position_management_plan(
        ticket=ticket,
        signal_id=signal_id,
        symbol=symbol,
        action=action,
        plan={
            "status": "open",
            "summary": "Execution trace saved without generated management plan.",
            "metadata": {
                "execution_trace": execution_trace,
            },
        },
    )


async def _pre_execution_checks(
    symbol: str,
    action: str,
    volume: float,
    stop_loss: float | None,
    take_profit: float | None,
    reference_spread: float,
    requested_agent_name: str,
):
    preflight = await execution_service.preflight(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reference_spread=reference_spread,
        requested_agent_name=requested_agent_name,
        requested_timeframe="H1",
        evaluation_mode="scan",
    )
    if not preflight.approved:
        raise HTTPException(status_code=409, detail=preflight.reason)

    return {
        "preflight": preflight,
        "entry_price": preflight.entry_price,
        "current_spread": preflight.current_spread,
        "rr_ratio": preflight.reward_risk,
        "margin_required": preflight.margin_required,
        "risk_approval": preflight.risk_approval,
    }


def _minimum_sl_tp_distance(
    *,
    tick,
    symbol_info: dict,
) -> float:
    point = float(symbol_info.get("point", 0.00001) or 0.00001)
    stops_level = float(symbol_info.get("trade_stops_level", 0) or 0)
    freeze_level = float(symbol_info.get("trade_freeze_level", 0) or 0)
    spread_points = float(getattr(tick, "spread", 0) or 0) if tick else 0
    effective_stops = max(stops_level, freeze_level, spread_points, 10.0)
    return effective_stops * point * 1.5


def _sl_tp_distance_error(
    *,
    action: str,
    tick,
    symbol_info: dict | None,
    stop_loss: float | None,
    take_profit: float | None,
    volume: float,
) -> str | None:
    if not tick or not symbol_info:
        return None
    exec_price = tick.ask if action == "BUY" else tick.bid
    if exec_price <= 0:
        return "No live price available for execution."

    min_distance = _minimum_sl_tp_distance(tick=tick, symbol_info=symbol_info)
    contract_size = symbol_info.get("trade_contract_size", 100000)
    if stop_loss and abs(exec_price - stop_loss) < min_distance:
        min_sl_dollars = round(min_distance * volume * contract_size, 2)
        return f"Stop Loss too close to price. Minimum ~${min_sl_dollars:.2f} for your position."
    if take_profit and abs(take_profit - exec_price) < min_distance:
        min_tp_dollars = round(min_distance * volume * contract_size, 2)
        return f"Take Profit too close to price. Minimum ~${min_tp_dollars:.2f} for your position."
    return None


def _adjust_tp_for_min_rr(
    *,
    action: str,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    required_rr: float,
    digits: int,
) -> float | None:
    """Keep SL fixed and adjust TP to satisfy minimum RR if needed."""
    if (
        entry_price <= 0
        or stop_loss is None
        or take_profit is None
        or required_rr <= 0
    ):
        return take_profit
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        return take_profit
    current_rr = abs(take_profit - entry_price) / sl_distance
    if current_rr >= required_rr:
        return take_profit
    action_upper = (action or "").upper()
    target_distance = sl_distance * required_rr
    if action_upper == "BUY":
        return round(entry_price + target_distance, digits)
    return round(entry_price - target_distance, digits)


def _clamp_sl_tp_to_broker_min_distance(
    *,
    action: str,
    tick,
    symbol_info: dict | None,
    stop_loss: float | None,
    take_profit: float | None,
) -> tuple[float | None, float | None]:
    """Ensure SL/TP respects current broker min stop distance.

    Returns possibly adjusted (stop_loss, take_profit). If market data is unavailable,
    returns inputs unchanged.
    """
    if not tick or not symbol_info:
        return stop_loss, take_profit

    digits = int(symbol_info.get("digits", 5) or 5)
    point = float(symbol_info.get("point", 0.00001) or 0.00001)
    min_distance = _minimum_sl_tp_distance(tick=tick, symbol_info=symbol_info)
    # Add one extra point as a safety cushion for fast-moving spreads.
    required = max(min_distance + point, point)

    side = (action or "").upper()
    exec_price = tick.ask if side == "BUY" else tick.bid
    if not exec_price or exec_price <= 0:
        return stop_loss, take_profit

    sl = stop_loss
    tp = take_profit

    if side == "BUY":
        if sl and sl >= exec_price - required:
            sl = round(exec_price - required, digits)
        if tp and tp <= exec_price + required:
            tp = round(exec_price + required, digits)
    else:
        if sl and sl <= exec_price + required:
            sl = round(exec_price + required, digits)
        if tp and tp >= exec_price - required:
            tp = round(exec_price - required, digits)

    return sl, tp


async def _persist_credentials_if_requested(
    account: int,
    server: str,
    password: str,
    terminal_path: str,
    save_credentials: bool,
    label: str = "",
) -> dict:
    if not save_credentials:
        return {"requested": False, "saved": False, "reason": "Remember credentials disabled."}
    if db is None:
        return {"requested": True, "saved": False, "reason": "Database not available."}
    if config.REQUIRE_SECURE_CREDENTIAL_STORAGE and not credential_vault.available:
        logger.warning("Credential save requested but secure storage is unavailable.")
        return {
            "requested": True,
            "saved": False,
            "reason": "Secure credential storage is unavailable on this machine.",
        }

    try:
        secret = credential_vault.save_password(account, server, password)
        await db.save_credentials(
            account=account,
            server=server,
            terminal_path=terminal_path,
            label=label,
            secret_ref=secret.secret_ref,
            secret_backend=secret.backend,
        )
        return {"requested": True, "saved": True, "backend": secret.backend}
    except Exception as exc:
        logger.warning("Failed to save credentials securely: %s", exc)
        return {"requested": True, "saved": False, "reason": str(exc)}


def _build_saved_connection_params(credential: dict) -> ConnectionParams:
    raw_path = (credential.get("terminal_path") or config.DEFAULT_TERMINAL_PATH).strip().strip('"').strip("'")
    password = credential_vault.get_password(
        credential.get("secret_ref", ""),
        credential.get("secret_backend", ""),
    )
    if not password:
        raise HTTPException(
            status_code=409,
            detail="Saved credentials metadata exists, but the secure password is unavailable.",
        )
    return ConnectionParams(
        account=credential["account"],
        password=password,
        server=credential["server"],
        terminal_path=raw_path,
    )


def _policy_settings_response() -> dict:
    return {
        "user_policy": risk_engine.user_policy.model_dump(),
        "presets": risk_engine.policy_presets(),
        "runtime_controls": risk_engine.runtime_controls(),
        "runtime_settings": risk_engine.settings.model_dump(),
        "universe": universe_service.summary_dict(),
        "event_providers": {
            "benzinga": benzinga_adapter.healthcheck(cached_only=True),
        },
    }


def _active_platform_connected() -> bool:
    if active_platform == "ibkr":
        return bool(ibkr_connector.connected)
    if active_platform == "tradestation":
        return bool(tradestation_connector.connected)
    return bool(connector.connected and connector.trade_server_connected())


def _ibkr_portfolio_snapshot(account, positions: list[dict]) -> dict:
    """Build portfolio totals without touching the MT5 market-data service."""
    equity = float(getattr(account, "equity", 0.0) or 0.0) if account is not None else 0.0
    margin = float(getattr(account, "margin", 0.0) or 0.0) if account is not None else 0.0
    free_margin = float(getattr(account, "free_margin", 0.0) or 0.0) if account is not None else 0.0
    exposure_by_symbol: dict[str, float] = {}
    for position in positions or []:
        symbol = str(position.get("symbol") or "").upper()
        notional = abs(float(position.get("volume") or 0.0) * float(position.get("price_current") or 0.0))
        if symbol:
            exposure_by_symbol[symbol] = exposure_by_symbol.get(symbol, 0.0) + notional
    return {
        "margin_utilization_pct": (margin / equity * 100.0) if equity > 0 else 0.0,
        "free_margin_pct": (free_margin / equity * 100.0) if equity > 0 else 0.0,
        "open_positions_total": len(positions or []),
        "exposure_by_symbol": exposure_by_symbol,
        "exposure_by_category": {},
        "exposure_by_sector": {},
        "usd_beta_exposure_pct": 0.0,
        "stocks_equity_exposure_pct": (sum(exposure_by_symbol.values()) / equity * 100.0) if equity > 0 else 0.0,
    }


def _ensure_mt5_platform_contract() -> None:
    """Keep the active broker runtime coherent without overriding valid brokers.

    This function is retained because older route handlers call it before broker
    dispatch. It previously forced every request back to MT5, which disconnected
    the UI state from an otherwise healthy IBKR session.
    """
    global active_platform
    if active_platform not in {"mt5", "ibkr", "tradestation"}:
        active_platform = "mt5"
        _apply_active_broker_runtime("mt5")


def _ensure_position_manager_running_if_connected() -> None:
    """Keep protective position management alive whenever the active broker is connected."""
    # IBKR protective exits are submitted as broker-native OCA stop/limit orders
    # by IBKRConnector. The legacy position manager is wired to MT5 market-data
    # and execution interfaces and must not be started for an IBKR session.
    if active_platform == "ibkr":
        return
    if _active_platform_connected() and not auto_trader.position_manager.is_running:
        auto_trader.position_manager.start()


def _require_active_platform_connection():
    if not _active_platform_connected():
        raise HTTPException(status_code=400, detail="Not connected")


def _safe_tradestation_positions() -> list:
    try:
        return tradestation_execution.get_positions()
    except Exception as exc:
        logger.warning("TradeStation positions refresh fell back to empty snapshot: %s", exc)
        return []


def _build_fast_execution_context(
    *,
    symbol: str,
    tick,
    symbol_info: Optional[dict],
    evaluation_mode: str = "manual_fast",
) -> Optional[MarketContext]:
    if tick is None:
        return None
    account = connector.refresh_account() if connector.connected else None
    tick_payload = {
        "bid": float(getattr(tick, "bid", 0.0) or 0.0),
        "ask": float(getattr(tick, "ask", 0.0) or 0.0),
        "spread": float(getattr(tick, "spread", 0.0) or 0.0),
        "time": float(getattr(tick, "time", 0.0) or 0.0),
    }
    normalized_symbol_info = signal_pipeline._normalize_symbol_info(symbol, tick_payload) if symbol_info else None
    profile = (
        signal_pipeline.profile_service.resolve_profile(symbol, normalized_symbol_info)
        if normalized_symbol_info is not None
        else None
    )
    open_positions = (
        [p.model_dump() for p in execution.get_positions()]
        if connector.connected
        else []
    )
    symbol_positions = [
        p for p in open_positions
        if str(p.get("symbol", "")).upper() == str(symbol).upper()
    ]
    return MarketContext(
        symbol=symbol,
        requested_timeframe="H1",
        evaluation_mode=evaluation_mode,
        user_policy=risk_engine.user_policy.model_dump(),
        symbol_info=normalized_symbol_info,
        profile=profile,
        tick=tick_payload,
        account_balance=float(account.balance) if account else 0.0,
        account_equity=float(account.equity) if account else 0.0,
        account_margin=float(account.margin) if account else 0.0,
        account_free_margin=float(account.free_margin) if account else 0.0,
        account_currency=str(account.currency) if account else "",
        account_leverage=int(account.leverage) if account else 0,
        bars_by_timeframe={},
        symbol_open_positions=symbol_positions,
        all_open_positions=open_positions,
    )


async def _run_ibkr(callable_obj, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ibkr_executor,
        lambda: callable_obj(*args, **kwargs),
    )


def _ibkr_market_symbols(include_inactive: bool = False) -> list[dict]:
    summary = universe_service.summary_dict()
    configured = summary.get("enabled_symbols") or []
    candidates = configured if configured else list(IBKR_SCAN_DEFAULT_SYMBOLS)
    symbols: list[dict] = []
    for raw in candidates:
        canonical = universe_service.canonical_symbol(raw)
        category = universe_service.expected_category_for_symbol(canonical) or "Stocks"
        if not include_inactive and not universe_service.is_symbol_active(canonical, category):
            continue
        symbols.append({
            "name": canonical,
            "description": f"{canonical} via IBKR",
            "path": "IBKR",
            "category": category,
            "visible": True,
            "trade_mode": 4,
            "spread": 0,
            "digits": 2,
            "point": 0.01,
            "volume_min": 1.0,
            "trade_enabled": True,
            # Never inject fake prices (e.g. 1.0). Let live tick endpoints fill quotes.
            "bid": 0.0,
            "ask": 0.0,
        })
    return symbols


async def _evaluate_ibkr_symbol_recommendation(sym: str) -> dict:
    category = universe_service.expected_category_for_symbol(sym) or "Stocks"
    ibkr_universe = _ibkr_market_symbols(include_inactive=False)
    market_guard_reason = signal_pipeline.market_session_guard_for_symbol(sym, ibkr_universe)
    snapshot = await _run_ibkr(ibkr_connector.get_snapshot, sym)
    bid = float((snapshot or {}).get("bid", 0.0) or 0.0)
    ask = float((snapshot or {}).get("ask", 0.0) or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask or 0.0)
    spread = max(0.0, ask - bid) if ask > 0 and bid > 0 else 0.0

    bars_m15 = await _run_ibkr(ibkr_connector.get_bars, sym, "M15", 64)
    bars_h1 = await _run_ibkr(ibkr_connector.get_bars, sym, "H1", 64)
    bars_h4 = await _run_ibkr(ibkr_connector.get_bars, sym, "H4", 48)
    if len(bars_h4) < 32:
        bars_h4 = bars_h1
    if mid <= 0 and bars_m15:
        last_close = float(bars_m15[-1].get("close", 0.0) or 0.0)
        if last_close > 0:
            mid = last_close
    if spread <= 0 and mid > 0:
        recent_range = 0.0
        if bars_m15:
            hi = float(max((b.get("high", 0.0) or 0.0) for b in bars_m15[-12:]) or 0.0)
            lo = float(min((b.get("low", 0.0) or 0.0) for b in bars_m15[-12:]) or 0.0)
            recent_range = max(0.0, hi - lo)
        spread = max(mid * 0.0005, recent_range * 0.02) if mid > 0 else 0.0
        if bid <= 0 and ask <= 0 and mid > 0:
            bid = mid - spread / 2.0
            ask = mid + spread / 2.0
    spread_pct = (spread / mid) if mid > 0 else 1.0

    signal_payload = {
        "action": "HOLD",
        "confidence": 0.0,
        "stop_loss": None,
        "take_profit": None,
        "max_holding_minutes": None,
        "reason": f"Waiting for enough IBKR market data (M15={len(bars_m15)}, H1={len(bars_h1)}).",
        "strategy": "ibkr_wait_data",
        "metadata": {},
    }
    has_min_history = len(bars_h1) >= 34 and len(bars_m15) >= 16
    limited_spread_limit = 0.004 if category in {"Stocks", "Indices"} else 0.006
    spread_quality = max(0.0, 1.0 - min(1.0, spread_pct / max(limited_spread_limit, 1e-6)))
    limited_conf = round(max(0.12, min(0.35, 0.12 + (spread_quality * 0.18))), 2)
    limited_reason = (
        market_guard_reason
        or f"Limited IBKR history right now (M15={len(bars_m15)}, H1={len(bars_h1)}). Holding until enough bars arrive."
    )
    if not has_min_history and mid > 0:
        # Provide a low-confidence HOLD score (instead of hard zero) when quotes exist
        # but IBKR bars are limited outside active sessions/subscriptions.
        signal_payload.update(
            {
                "confidence": limited_conf,
                "reason": limited_reason,
                "strategy": "ibkr_limited_data_hold",
            }
        )

    if has_min_history:
        account = ibkr_connector.account_info
        agent_input = AgentInput(
            symbol=sym,
            timeframe="H1",
            bars=bars_h1,
            spread=spread,
            account_equity=float(account.equity) if account else 0.0,
            open_positions=[],
            multi_tf_bars={"M15": bars_m15, "H1": bars_h1, "H4": bars_h4},
            policy_mode=risk_engine.user_policy.mode,
            force_execute_any_conditions=bool(getattr(risk_engine.user_policy, "force_execute_any_conditions", False)),
        )
        smart_agent = agents.get("SmartAgent")
        model_signal = smart_agent.evaluate(agent_input) if smart_agent else None
        if model_signal:
            signal_payload = {
                "action": model_signal.action,
                "confidence": float(model_signal.confidence or 0.0),
                "stop_loss": model_signal.stop_loss,
                "take_profit": model_signal.take_profit,
                "max_holding_minutes": model_signal.max_holding_minutes,
                "reason": model_signal.reason,
                "strategy": model_signal.strategy or "ibkr_smart_agent",
                "metadata": dict(model_signal.metadata or {}),
            }
            model_reason = str(signal_payload.get("reason") or "")
            model_conf = float(signal_payload.get("confidence", 0.0) or 0.0)
            if (
                mid > 0
                and str(signal_payload.get("action") or "").upper() == "HOLD"
                and model_conf <= 0.0
                and "not enough market data" in model_reason.lower()
            ):
                signal_payload.update(
                    {
                        "confidence": limited_conf,
                        "reason": limited_reason,
                        "strategy": "ibkr_limited_data_hold",
                    }
                )

    action = str(signal_payload.get("action", "HOLD")).upper()
    confidence = float(signal_payload.get("confidence", 0.0) or 0.0)
    event_context = {
        "news_items": 0,
        "bias": "neutral",
        "event_risk": "low",
        "confidence_adjustment": 0.0,
        "contradiction_flag": False,
        "event_ids": [],
    }
    if event_ingestion_service is not None and action in {"BUY", "SELL"}:
        try:
            stored_news = await event_ingestion_service.recent_symbol_news(
                sym,
                universe_service=universe_service,
                limit=8,
            )
            if stored_news:
                direction = "bullish" if action == "BUY" else "bearish"
                weights = {"high": 1.0, "medium": 0.6, "low": 0.3}
                risk_rank = {"low": 0, "medium": 1, "high": 2}
                bias_score = 0.0
                total_w = 0.0
                strongest_risk = "low"
                event_ids: list[str] = []
                for item in stored_news:
                    meta = item.metadata or {}
                    event_id = str(meta.get("external_event_id", "") or "")
                    if event_id:
                        event_ids.append(event_id)
                    bias = str(meta.get("gemini_bias", "neutral")).lower()
                    risk = str(meta.get("gemini_event_risk", "low")).lower()
                    if risk not in {"low", "medium", "high"}:
                        risk = "low"
                    w = weights.get(risk, 0.3)
                    total_w += w
                    if bias == direction:
                        bias_score += 1.0 * w
                    elif bias not in {"neutral", ""}:
                        bias_score -= 1.0 * w
                    if risk_rank[risk] > risk_rank[strongest_risk]:
                        strongest_risk = risk
                normalized_bias = (bias_score / total_w) if total_w > 0 else 0.0
                if normalized_bias > 0.15:
                    bias_label = direction
                elif normalized_bias < -0.15:
                    bias_label = "bearish" if direction == "bullish" else "bullish"
                else:
                    bias_label = "neutral"
                contradiction_flag = bias_label not in {direction, "neutral"}
                confidence_adj = max(-0.08, min(0.08, normalized_bias * 0.06))
                if strongest_risk == "high":
                    confidence_adj = min(confidence_adj, 0.0) - 0.01
                confidence = round(max(0.0, min(0.95, confidence + confidence_adj)), 2)
                event_context = {
                    "news_items": len(stored_news),
                    "bias": bias_label,
                    "event_risk": strongest_risk,
                    "confidence_adjustment": round(confidence_adj, 3),
                    "contradiction_flag": contradiction_flag,
                    "event_ids": list(dict.fromkeys(event_ids)),
                }
        except Exception:
            pass
    signal_payload["confidence"] = confidence
    signal_meta = dict(signal_payload.get("metadata") or {})
    signal_meta["event_context"] = event_context
    signal_meta["news"] = {
        "sentiment": str(event_context.get("bias", "neutral")),
        "impact": str(event_context.get("event_risk", "low")),
        "relevance": "direct" if int(event_context.get("news_items", 0) or 0) > 0 else "indirect",
        "strength": max(0.0, min(1.0, abs(float(event_context.get("confidence_adjustment", 0.0) or 0.0)) * 8.0)),
        "risk_flag": bool(event_context.get("event_risk") == "high"),
        "message": (
            "Trade blocked due to high-impact news risk."
            if bool(event_context.get("event_risk") == "high" and event_context.get("contradiction_flag"))
            else ("No major news" if int(event_context.get("news_items", 0) or 0) <= 0 else "News context loaded")
        ),
        "news_unavailable": False,
    }
    signal_payload["metadata"] = signal_meta

    min_conf = float(risk_engine.settings.auto_trade_min_confidence or 0.7)
    rr = _reward_risk_ratio(mid, signal_payload.get("stop_loss"), signal_payload.get("take_profit"))
    min_rr = float(risk_engine.user_policy.min_reward_risk or 1.6)

    max_spread_pct = 0.004 if category in {"Stocks", "Indices"} else 0.006
    spread_ok = spread_pct <= max_spread_pct
    confidence_ok = confidence >= min_conf
    rr_ok = rr >= min_rr if action in {"BUY", "SELL"} else False
    has_price = mid > 0

    ready_to_execute = bool(
        action in {"BUY", "SELL"}
        and has_price
        and spread_ok
        and confidence_ok
        and rr_ok
    )

    if ready_to_execute:
        reason = "Ready for execution."
        status = "pass"
        machine_reasons: list[str] = []
    else:
        blockers: list[str] = []
        if action not in {"BUY", "SELL"}:
            blockers.append("signal_hold")
        if not has_price:
            blockers.append("no_price")
        if not spread_ok:
            blockers.append("spread_too_wide")
        if not confidence_ok:
            blockers.append("confidence_below_threshold")
        if action in {"BUY", "SELL"} and not rr_ok:
            blockers.append("reward_risk_below_min")
        if event_context.get("contradiction_flag"):
            blockers.append("event_context_contradiction")
        machine_reasons = blockers
        reason = ", ".join(blockers) if blockers else "Blocked by IBKR pre-checks."
        status = "block"

    ibkr_account = ibkr_connector.account_info
    ibkr_free_margin = float(getattr(ibkr_account, "free_margin", 0.0) or 0.0)
    recommended_amount_usd, recommended_amount_pct = _recommended_trade_amount_from_free_margin(
        free_margin=ibkr_free_margin,
        policy_mode=risk_engine.user_policy.mode,
        confidence=confidence,
        quality_score=confidence,
        ready_to_execute=ready_to_execute,
    )
    commission_snapshot = _commission_snapshot_from_symbol_info(None)

    signal_id = None
    if db is not None:
        signal_id = await db.log_signal(
            agent_name="SmartAgent",
            symbol=sym,
            timeframe="H1",
            action=action,
            confidence=confidence,
            stop_loss=signal_payload.get("stop_loss"),
            take_profit=signal_payload.get("take_profit"),
            max_holding_minutes=signal_payload.get("max_holding_minutes"),
            reason=signal_payload.get("reason") or "",
        )

    return {
        "symbol": sym,
        "signal": signal_payload,
        "signal_id": signal_id,
        "risk_decision": {
            "approved": ready_to_execute,
            "reason": reason,
            "adjusted_volume": float(risk_engine.settings.fixed_lot_size or 1.0),
            "warnings": [],
            "status": status,
            "machine_reasons": machine_reasons,
            "metrics_snapshot": {
                "platform": "ibkr",
                "spread": spread,
                "spread_pct": spread_pct,
                "reward_risk": rr,
                "confidence": confidence,
                "confidence_threshold": min_conf,
            },
        },
        "entry_price_estimate": mid,
        "explanation": signal_payload.get("reason") or "IBKR technical scan.",
        "ready_to_execute": ready_to_execute,
        "category": category,
        "description": f"{sym} via IBKR",
        "degraded_reasons": [],
        "trade_quality": {
            "trend_alignment_score": confidence,
            "momentum_quality_score": confidence,
            "entry_timing_score": confidence,
            "volatility_quality_score": 1.0 if len(bars_h1) >= 52 else 0.0,
            "reward_risk_score": min(1.0, rr / max(min_rr, 0.1)) if action in {"BUY", "SELL"} else 0.0,
            "spread_quality_score": max(0.0, 1.0 - min(1.0, spread_pct / max_spread_pct)),
            "portfolio_fit_score": 1.0,
            "news_alignment_score": (
                0.8 if event_context.get("bias") in {"bullish", "bearish"} and not event_context.get("contradiction_flag")
                else 0.3 if event_context.get("contradiction_flag")
                else 0.5
            ),
            "contradiction_penalty": 0.12 if event_context.get("contradiction_flag") else 0.0,
            "final_trade_quality_score": confidence,
            "threshold": min_conf,
            "no_trade_zone": not ready_to_execute,
            "no_trade_reasons": machine_reasons,
            "summary": reason,
        },
        "portfolio_risk": {
            "status": "pass" if ready_to_execute else "block",
            "allow_execute": ready_to_execute,
            "reason": reason,
            "blocking_reasons": machine_reasons,
            "warnings": [],
            "metrics_snapshot": {
                "platform": "ibkr",
                "spread_pct": spread_pct,
                "reward_risk": rr,
            },
            "correlated_symbols": [],
            "margin_required": 0.0,
            "projected_margin_utilization_pct": 0.0,
            "projected_free_margin_pct": 100.0,
            "portfolio_fit_score": 1.0 if ready_to_execute else 0.0,
        },
        "anti_churn": {
            "blocked": False,
            "threshold_boost": 0.0,
            "reasons": [],
            "metadata": {"platform": "ibkr"},
        },
        "gemini_confirmation": None,
        "news": _news_payload_from_signal(signal_payload),
        "execution_reason": reason,
        "recommended_amount_usd": recommended_amount_usd,
        "recommended_amount_pct_free_margin": recommended_amount_pct,
        **commission_snapshot,
    }


async def _ibkr_reference_price(symbol: str) -> float:
    snap = await _run_ibkr(ibkr_connector.get_snapshot, symbol)
    price = float((snap or {}).get("last") or (snap or {}).get("ask") or (snap or {}).get("bid") or 0.0)
    if price > 0:
        return price
    bars = await _run_ibkr(ibkr_connector.get_bars, symbol, "H1", 5)
    if bars:
        price = float(bars[-1].get("close", 0.0) or 0.0)
    return price if price > 0 else 0.0


def _extract_json_object(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty Gemini response")
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _simple_trade_intent_fallback(message: str) -> dict:
    text = (message or "").strip()
    upper = text.upper()
    action = "BUY" if "BUY" in upper else "SELL" if "SELL" in upper else ""
    symbol_match = re.search(r"\b([A-Z]{3,10}(?:USD)?)\b", upper)
    amount_match = re.search(r"\$?\s*(\d+(?:\.\d+)?)", upper)
    if not action or not symbol_match or not amount_match:
        return {
            "reply": "I can help analyze the market. To request a trade, write for example: BUY XAUUSD 1000",
            "intent": "chat",
            "trade": None,
        }
    amount = float(amount_match.group(1))
    return {
        "reply": f"I prepared a {action} request for {symbol_match.group(1)} with ${amount:.2f} margin. Review and confirm.",
        "intent": "trade_request",
        "trade": {
            "symbol": symbol_match.group(1),
            "action": action,
            "amount_usd": amount,
            "stop_loss": None,
            "take_profit": None,
            "reason": "Parsed from request fallback.",
        },
    }


def _is_gemini_availability_query(message: str) -> bool:
    lowered = re.sub(r"\s+", " ", (message or "").strip().lower())
    if not lowered:
        return False

    direct_phrases = {
        "are you available",
        "are u available",
        "are you online",
        "are you working",
        "gemini available",
        "is gemini available",
        "gemini status",
    }
    if lowered in direct_phrases:
        return True

    if "available" in lowered and ("gemini" in lowered or "you" in lowered):
        return True
    if "status" in lowered and "gemini" in lowered:
        return True
    if "online" in lowered and "gemini" in lowered:
        return True
    return False


def _current_gemini_status() -> dict:
    def _normalize_error(msg: str | None) -> str | None:
        text = (msg or "").strip()
        if not text:
            return None
        upper = text.upper()
        if "RESOURCE_EXHAUSTED" in upper or "QUOTA" in upper or "429" in upper:
            return "Quota exhausted / rate limited. Using technical logic only until quota resets."
        if len(text) > 240:
            return text[:240].rstrip() + "..."
        return text

    available = bool(gemini_agent.available)
    now = time.time()
    agent_quota_until = float(getattr(gemini_agent, "_quota_block_until", 0.0) or 0.0)
    route_quota_until = float(_gemini_quota_cooldown_until or 0.0)
    cooldown_seconds = int(max(0.0, max(agent_quota_until - now, route_quota_until - now)))
    # Treat adapter failures as degraded only while recent, to avoid stale red state.
    RECENT_FAILURE_WINDOW_SECONDS = 900.0
    RECENT_QUOTA_WINDOW_SECONDS = 1800.0
    RECENT_SUCCESS_WINDOW_SECONDS = 900.0
    adapter_last_failure_at = float(getattr(gemini_adapter, "last_failure_at", 0.0) or 0.0)
    adapter_failure_recent = bool(
        getattr(gemini_adapter, "last_error", None)
        and adapter_last_failure_at > 0
        and (now - adapter_last_failure_at) <= RECENT_FAILURE_WINDOW_SECONDS
    )
    agent_last_error = str(getattr(gemini_agent, "runtime_last_error", "") or "").strip()
    agent_last_error_at = float(getattr(gemini_agent, "runtime_last_error_at", 0.0) or 0.0)
    agent_last_success_at = float(getattr(gemini_agent, "runtime_last_success_at", 0.0) or 0.0)
    agent_success_recent = bool(
        agent_last_success_at > 0
        and (now - agent_last_success_at) <= RECENT_SUCCESS_WINDOW_SECONDS
    )
    agent_last_error_upper = agent_last_error.upper()
    agent_quota_recent = bool(
        agent_last_error
        and agent_last_error_at > 0
        and (now - agent_last_error_at) <= RECENT_QUOTA_WINDOW_SECONDS
        and (
            "RESOURCE_EXHAUSTED" in agent_last_error_upper
            or "QUOTA" in agent_last_error_upper
            or "RATE LIMIT" in agent_last_error_upper
            or "429" in agent_last_error_upper
        )
    )
    degraded = bool(
        (not available)
        or cooldown_seconds > 0
        or adapter_failure_recent
        or agent_quota_recent
    )

    active_error = None
    if not available:
        active_error = getattr(gemini_agent, "unavailable_reason", "") or None
    elif cooldown_seconds > 0:
        active_error = "Quota exhausted / rate limited. Using technical logic only until quota resets."
    elif agent_quota_recent:
        active_error = agent_last_error
    elif adapter_failure_recent:
        active_error = getattr(gemini_adapter, "last_error", None)
    last_error = _normalize_error(active_error)

    if not available:
        credits_pct = 0
    elif cooldown_seconds > 0 or agent_quota_recent:
        # During quota pressure, model should be considered near-exhausted.
        credits_pct = 5
    elif degraded:
        credits_pct = 40
    else:
        credits_pct = 100

    if available and cooldown_seconds > 0:
        reply = f"Gemini is in quota cooldown (~{cooldown_seconds}s remaining). Technical logic is used until cooldown ends."
        state = "cooldown"
    elif available and not degraded and not agent_success_recent:
        reply = "Gemini API key is configured, but no recent successful Gemini response has verified live availability yet."
        state = "ready"
    elif available and not degraded:
        reply = "Yes. Gemini is available and healthy right now."
        state = "available"
    elif available and degraded:
        reply = f"Gemini is connected but degraded right now. Reason: {last_error or 'Unknown error'}"
        state = "degraded"
    else:
        reply = f"No. Gemini is not available right now. Reason: {last_error or 'Not configured'}"
        state = "unavailable"

    return {
        "state": state,
        "available": available,
        "degraded": degraded,
        "primary_model": str(getattr(gemini_agent, "model_name", "") or ""),
        "active_model": str(
            getattr(gemini_agent, "active_model_name", "")
            or getattr(gemini_agent, "model_name", "")
            or ""
        ),
        "using_fallback_model": bool(
            str(getattr(gemini_agent, "active_model_name", "") or "").strip()
            and str(getattr(gemini_agent, "model_name", "") or "").strip()
            and str(getattr(gemini_agent, "active_model_name", "") or "").strip()
            != str(getattr(gemini_agent, "model_name", "") or "").strip()
        ),
        "last_error": last_error,
        "cooldown_seconds": cooldown_seconds,
        "credits_pct": credits_pct,
        "reply": reply,
    }


# --- Connection Routes ---

class ConnectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    platform: Literal["mt5", "ibkr", "tradestation"] = "mt5"
    terminal_path: Optional[str] = None
    host: str = "127.0.0.1"
    port: Optional[int] = None
    client_id: int = 1
    account_id: Optional[str] = None
    readonly: bool = False
    ibkr_environment: Literal["paper", "live"] = "paper"
    environment: Literal["sim", "live"] = "sim"
    redirect_uri: Optional[str] = None


_tradestation_oauth_states: dict[str, dict[str, str]] = {}


def _tradestation_client_config() -> tuple[str, str]:
    client_id = (
        os.getenv("TRADESTATION_CLIENT_ID")
        or os.getenv("TRADESTATION_API_KEY")
        or ""
    ).strip()
    client_secret = (
        os.getenv("TRADESTATION_CLIENT_SECRET")
        or os.getenv("TRADESTATION_API_SECRET")
        or ""
    ).strip()
    if not client_id:
        raise HTTPException(
            status_code=409,
            detail="TradeStation client ID is not configured. Set TRADESTATION_CLIENT_ID, then connect again.",
        )
    return client_id, client_secret


def _tradestation_authorize_url(req: ConnectRequest) -> str:
    client_id, _client_secret = _tradestation_client_config()
    redirect_uri = str(
        req.redirect_uri
        or os.getenv("TRADESTATION_REDIRECT_URI")
        or "http://127.0.0.1:8000/api/tradestation/oauth/callback"
    ).strip()
    state = secrets.token_urlsafe(24)
    _tradestation_oauth_states[state] = {
        "environment": req.environment,
        "redirect_uri": redirect_uri,
        "account_id": str(req.account_id or "").strip(),
    }
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "audience": "https://api.tradestation.com",
        "state": state,
        "scope": "openid offline_access profile MarketData ReadAccount Trade",
    }
    return f"{TradeStationConnector.AUTH_BASE_URL}/authorize?{urlencode(params)}"


def _exchange_tradestation_code(code: str, redirect_uri: str) -> dict:
    client_id, client_secret = _tradestation_client_config()
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    response = requests.post(
        f"{TradeStationConnector.AUTH_BASE_URL}/oauth/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(3.05, 15.0),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    token_payload = response.json()
    if not str(token_payload.get("access_token") or "").strip():
        raise RuntimeError("TradeStation token response did not include an access token.")
    return token_payload


@router.get("/tradestation/oauth/callback")
async def tradestation_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    global active_platform
    if error:
        raise HTTPException(status_code=400, detail=error_description or error)
    auth_code = str(code or "").strip()
    state_key = str(state or "").strip()
    state_info = _tradestation_oauth_states.pop(state_key, None)
    if not auth_code or not state_info:
        raise HTTPException(status_code=400, detail="Invalid or expired TradeStation authorization response.")
    try:
        token_payload = await asyncio.to_thread(
            _exchange_tradestation_code,
            auth_code,
            state_info["redirect_uri"],
        )
        params = TradeStationConnectionParams(
            environment=state_info.get("environment") or "sim",
            access_token=str(token_payload.get("access_token") or ""),
            refresh_token=str(token_payload.get("refresh_token") or ""),
            client_id=(os.getenv("TRADESTATION_CLIENT_ID") or os.getenv("TRADESTATION_API_KEY") or "").strip(),
            client_secret=(os.getenv("TRADESTATION_CLIENT_SECRET") or os.getenv("TRADESTATION_API_SECRET") or "").strip(),
            account_id=state_info.get("account_id") or None,
        )
        success = await asyncio.to_thread(tradestation_connector.connect, params)
        if not success:
            raise RuntimeError(tradestation_connector.last_error or "TradeStation account lookup failed.")
        if mt5_connector.connected:
            mt5_connector.disconnect()
        if ibkr_connector.connected:
            await _run_ibkr(ibkr_connector.disconnect)
        active_platform = "tradestation"
        _apply_active_broker_runtime("tradestation")
        _ensure_position_manager_running_if_connected()
        await _persist_runtime_state()
        if tradestation_connector.account_info:
            risk_engine.set_daily_start_equity(tradestation_connector.account_info.equity)
        return HTMLResponse(
            "<html><body><h1>TradeStation connected</h1>"
            "<p>You can close this tab and return to LinkTrade.</p></body></html>"
        )
    except Exception as exc:
        logger.exception("TradeStation OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"TradeStation authorization failed: {exc}")


@router.post("/connect")
async def connect(req: ConnectRequest, authorization: Optional[str] = Header(default=None)):
    global active_platform
    await _require_user_permission(authorization, "can_connect_broker")
    _resume_auto_connect()
    requested_platform = str(req.platform or "mt5").strip().lower()
    if requested_platform == "ibkr":
        default_ibkr_port = 7497 if req.ibkr_environment == "paper" else 7496
        params = IBKRConnectionParams(
            host=(req.host or "127.0.0.1").strip() or "127.0.0.1",
            port=int(req.port or default_ibkr_port),
            client_id=int(req.client_id or 1),
            account_id=req.account_id,
            readonly=bool(req.readonly),
            environment=req.ibkr_environment,
        )
        success = await _run_ibkr(ibkr_connector.connect, params)
        if not success:
            raise HTTPException(
                status_code=400,
                detail=ibkr_connector.last_error
                or "Could not connect to local IBKR TWS or IB Gateway. Open and log in there, enable API access, then try again.",
            )
        if mt5_connector.connected:
            mt5_connector.disconnect()
        if tradestation_connector.connected:
            tradestation_connector.disconnect()
        active_platform = "ibkr"
        _apply_active_broker_runtime("ibkr")
        _ensure_position_manager_running_if_connected()
        await _persist_runtime_state()
        if ibkr_connector.account_info:
            risk_engine.set_daily_start_equity(ibkr_connector.account_info.equity)
        return {
            "connected": True,
            "platform": active_platform,
            "account": ibkr_connector.account_info.model_dump() if ibkr_connector.account_info else None,
            "is_demo": ibkr_connector.is_demo(),
            "environment": "paper" if ibkr_connector.is_demo() else "live",
            "credential_status": {
                "requested": False,
                "saved": False,
                "reason": "IBKR credentials stay inside local TWS or IB Gateway.",
            },
        }

    if requested_platform == "tradestation":
        auth_url = _tradestation_authorize_url(req)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        return {
            "connected": False,
            "platform": "tradestation",
            "auth_required": True,
            "auth_url": auth_url,
            "environment": req.environment,
            "credential_status": {
                "requested": False,
                "saved": False,
                "reason": "TradeStation credentials are entered only on TradeStation's authorization page.",
            },
        }

    raw_path = (req.terminal_path or config.DEFAULT_TERMINAL_PATH).strip().strip('"').strip("'")
    params = TerminalSessionParams(terminal_path=raw_path)
    success = mt5_connector.connect_terminal_session(params)

    if success and not mt5_connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        account_info = mt5_connector.account_info
        mt5_connector.disconnect()
        if db:
            _schedule_connection_event(
                "rejected",
                account=account_info.login if account_info else 0,
                server=account_info.server if account_info else "",
                details="Live account rejected - demo only mode",
            )
        raise HTTPException(
            status_code=403,
            detail="Live trading is blocked by policy. Disable demo-only mode and enable LIVE_TRADING_ENABLED to allow live accounts.",
        )

    if success:
        active_platform = "mt5"
        _apply_active_broker_runtime("mt5")
        _ensure_position_manager_running_if_connected()
        await _persist_runtime_state()
        # Risk limits should initialize for every successful MT5 connection,
        # independent of whether database logging is available.
        if mt5_connector.account_info:
            risk_engine.set_daily_start_equity(mt5_connector.account_info.equity)
    if success and db:
        account_info = mt5_connector.account_info
        _schedule_connection_event(
            "connected",
            account=account_info.login if account_info else 0,
            server=account_info.server if account_info else "",
        )

        credential_status = {"requested": False, "saved": False, "reason": "Credentials are handled only inside the local MT5 terminal."}
        if risk_engine.auto_trade_enabled and risk_engine.auto_trade_session_armed and not auto_trader.is_running:
            task_orchestrator.start_auto_trade()
    else:
        credential_status = {"requested": False, "saved": False, "reason": "Credentials are handled only inside the local MT5 terminal."}

    if not success:
        if db:
            await db.log_error("connection", mt5_connector.last_error or "Unknown error")
        detail = mt5_connector.last_error or "Unknown error"
        if "Authorization failed" in detail:
            detail = (
                f"{detail}. Verify MT5 account password (not investor password) "
                "and exact server name."
            )
        raise HTTPException(status_code=400, detail=detail)

    return {
        "connected": True,
        "platform": active_platform,
        "account": connector.account_info.model_dump() if connector.account_info else None,
        "is_demo": connector.is_demo(),
        "credential_status": credential_status,
    }


@router.post("/disconnect")
async def disconnect():
    global active_platform
    _pause_auto_connect()
    mt5_success = mt5_connector.disconnect() if mt5_connector.connected else True
    ibkr_success = await _run_ibkr(ibkr_connector.disconnect) if ibkr_connector.connected else True
    tradestation_success = tradestation_connector.disconnect() if tradestation_connector.connected else True
    success = bool(mt5_success and ibkr_success and tradestation_success)
    active_platform = "mt5"
    _apply_active_broker_runtime("mt5")
    await _persist_runtime_state()
    if db:
        await db.log_connection_event("disconnected")
    return {
        "disconnected": success,
        "auto_connect_paused": True,
        "pause_reason": str(_auto_connect_state.get("pause_reason") or ""),
    }


@router.get("/status")
async def status(
    authorization: Optional[str] = Header(default=None),
    fresh: bool = Query(default=False),
):
    global active_platform
    _ensure_position_manager_running_if_connected()
    user = await _resolve_authenticated_user(authorization)
    user_id = str(user.get("id") or "local-anonymous")
    user_permissions = await _effective_user_permissions(user)
    try:
        selected_strategy_id = await asyncio.wait_for(
            _get_active_strategy_id_for_user(user_id),
            timeout=0.35,
        )
    except Exception as exc:
        logger.warning("Status strategy lookup fell back to default strategy: %s", exc)
        selected_strategy_id = _default_strategy_id()
    gemini_status = _current_gemini_status()
    if active_platform == "tradestation" and tradestation_connector.connected:
        account = tradestation_connector.refresh_account()
        terminal = tradestation_connector.get_terminal_info()
        connected_flag = bool(tradestation_connector.connected)
        last_error = tradestation_connector.last_error
        is_demo = tradestation_connector.is_demo()
        positions_timeout = 1.6 if fresh else 0.8
        try:
            positions_snapshot = await asyncio.wait_for(
                asyncio.to_thread(_safe_tradestation_positions),
                timeout=positions_timeout,
            )
        except Exception as exc:
            logger.warning("TradeStation status position refresh timed out: %s", exc)
            positions_snapshot = []
    elif active_platform == "ibkr" and ibkr_connector.connected:
        # Connection/status polling must never queue behind long IBKR history
        # requests. Account and terminal callbacks are already cached by the
        # connector; slow broker refreshes happen on dedicated data routes.
        account = ibkr_connector.account_info
        terminal = ibkr_connector.get_terminal_info()
        connected_flag = bool(ibkr_connector.connected)
        last_error = ibkr_connector.last_error
        is_demo = ibkr_connector.is_demo()
        cached_position_rows = [
            row for key, row in _positions_payload_cache.items()
            if key[0] == "ibkr" and key[1] == ""
        ]
        positions_snapshot = list(max(cached_position_rows, key=lambda row: row[0])[1]) if cached_position_rows else []
    else:
        connected_flag = bool(connector.connected)
        last_error = connector.last_error
        is_demo = connector.is_demo()
        account = connector.account_info
        terminal = None
        positions_snapshot = []
        if connected_flag:
            account_timeout = 1.6 if fresh else 0.6
            terminal_timeout = 1.2 if fresh else 0.4
            positions_timeout = 1.6 if fresh else 0.8
            try:
                refreshed = await asyncio.wait_for(
                    asyncio.to_thread(connector.refresh_account),
                    timeout=account_timeout,
                )
                if refreshed is not None:
                    account = refreshed
            except Exception as exc:
                logger.warning("MT5 status account refresh fell back to cached account: %s", exc)
            try:
                terminal = await asyncio.wait_for(
                    asyncio.to_thread(connector.get_terminal_info),
                    timeout=terminal_timeout,
                )
            except Exception as exc:
                logger.warning("MT5 status terminal refresh fell back to cached connection state: %s", exc)
            try:
                positions_snapshot = await asyncio.wait_for(
                    asyncio.to_thread(execution.get_positions),
                    timeout=positions_timeout,
                )
            except Exception as exc:
                logger.warning("MT5 status position refresh fell back to empty snapshot: %s", exc)
                positions_snapshot = []

    # Keep runtime connected state stable even if one account refresh call fails.
    # Temporary MT5 IPC hiccups should not flip UI to disconnected and stop live polling.
    portfolio_snapshot = (
        _ibkr_portfolio_snapshot(account, positions_snapshot)
        if connected_flag and active_platform == "ibkr"
        else signal_pipeline.portfolio_risk_service.snapshot(account, positions_snapshot)
    ) if connected_flag else {
        "margin_utilization_pct": 0.0,
        "free_margin_pct": 0.0,
        "open_positions_total": 0,
        "exposure_by_symbol": {},
        "exposure_by_category": {},
        "exposure_by_sector": {},
        "usd_beta_exposure_pct": 0.0,
        "stocks_equity_exposure_pct": 0.0,
    }
    readiness_blockers: list[str] = []
    readiness_warnings: list[str] = []
    if not connected_flag:
        readiness_blockers.append("Trading account is not connected.")
    live_policy_allows_connection = bool(config.LIVE_TRADING_ENABLED) and not bool(risk_engine.user_policy.demo_only_default)
    if connected_flag and not is_demo and not live_policy_allows_connection:
        readiness_blockers.append("Connected account is live, but live trading is blocked by policy or backend configuration.")
    elif connected_flag and not is_demo:
        readiness_warnings.append("Connected account is live. Real-money execution safeguards are active.")
    if risk_engine.panic_stopped:
        readiness_blockers.append("Panic stop is active.")
    kill_switch_state = kill_switch_service.current_state()
    if kill_switch_state.enabled:
        readiness_blockers.append("Global trading kill switch is active.")
    if not bool(gemini_status.get("available", False)):
        readiness_blockers.append("Gemini analysis provider is unavailable.")
    elif bool(gemini_status.get("degraded", False)) or str(gemini_status.get("state") or "") not in {"available", "ready"}:
        readiness_warnings.append("Gemini is available but degraded or cooling down.")
    benzinga_status = benzinga_adapter.healthcheck(cached_only=True)
    if bool(benzinga_status.get("degraded", False)):
        readiness_warnings.append(f"Benzinga event provider degraded: {benzinga_status.get('reason') or 'provider health degraded'}.")
    cloud_decision_configured = bool(getattr(signal_pipeline.cloud_brain_decision_client, "configured", False))
    cloud_decision_reason = getattr(signal_pipeline.cloud_brain_decision_client, "disabled_reason", None)
    cloud_decision_enabled = bool(getattr(signal_pipeline.cloud_brain_decision_client, "enabled", False))
    if cloud_decision_enabled and not cloud_decision_configured:
        readiness_warnings.append(f"Cloud decision is disabled: {cloud_decision_reason or 'not configured'}.")
    position_protection_ready = bool(auto_trader.position_manager.is_running) or bool(
        connected_flag and active_platform == "ibkr"
    )
    if not position_protection_ready and connected_flag:
        readiness_blockers.append("Position manager is not running.")
    if auto_trader.is_running and not risk_engine.auto_trade_enabled:
        readiness_warnings.append("Auto-trader task is running while auto-trade control is disabled.")
    recent_auto_activity = list(getattr(auto_trader, "recent_trades", []) or [])
    recent_cutoff = time.time() - 90.0
    unverifiable_recent = [
        item for item in recent_auto_activity[:10]
        if float(item.get("timestamp") or 0.0) >= recent_cutoff
        and str(item.get("action") or "").upper() in {"BUY", "SELL"}
        and any(
            "cannot verify" in str(reason).lower() or "computed volume is zero" in str(reason).lower()
            for reason in list(item.get("machine_reasons") or []) + list(item.get("blocking_reasons") or [])
        )
    ]
    if len(unverifiable_recent) >= 3:
        readiness_warnings.append("Several recent markets are not executable because live price or volume could not be verified.")
    try:
        closed_trade_count = await asyncio.wait_for(db.count_trade_outcomes(), timeout=0.4) if db is not None else 0
    except Exception:
        try:
            closed_trade_count = await asyncio.to_thread(_count_trade_outcomes_direct)
        except Exception:
            closed_trade_count = 0
    try:
        api_keys_health = _api_key_health_snapshot(force_refresh=False)
    except Exception as exc:
        api_keys_health = {
            "overall_status": "degraded",
            "unhealthy_count": 1,
            "items": [],
            "email_alerts": {"enabled": False, "configured": False, "last_alert_at": None},
            "reason": str(exc),
        }
    unhealthy_api_keys = [
        item for item in (api_keys_health.get("items") or [])
        if str(item.get("status") or "").lower() in {"offline", "degraded", "low_credits", "warning"}
    ]
    inactive_required_keys = [
        item for item in (api_keys_health.get("items") or [])
        if str(item.get("id") or "") in {"gemini", "supabase_anon", "supabase_service_role"}
        and str(item.get("status") or "").lower() == "inactive"
    ]
    if unhealthy_api_keys:
        degraded_api_keys = [
            item for item in unhealthy_api_keys
            if str(item.get("status") or "").lower() != "warning"
        ]
        warning_api_keys = [
            item for item in unhealthy_api_keys
            if str(item.get("status") or "").lower() == "warning"
        ]
        if degraded_api_keys:
            readiness_warnings.append(
                "API/provider health degraded: "
                + ", ".join(str(item.get("name") or item.get("id")) for item in degraded_api_keys[:3])
            )
        if warning_api_keys:
            readiness_warnings.append(
                "API/provider health warning: "
                + ", ".join(str(item.get("name") or item.get("id")) for item in warning_api_keys[:3])
            )
    if inactive_required_keys:
        readiness_warnings.append(
            "Required provider keys missing or inactive: "
            + ", ".join(str(item.get("name") or item.get("id")) for item in inactive_required_keys[:3])
        )
    if closed_trade_count < 50:
        readiness_warnings.append(f"Meta-model training set has only {closed_trade_count} closed trades; 50+ is preferred.")
    readiness_score = 10.0
    readiness_score -= 1.5 * len(readiness_blockers)
    readiness_score -= 0.45 * len(readiness_warnings)
    readiness_score = max(0.0, min(10.0, readiness_score))
    publish_readiness = {
        "score": round(readiness_score, 1),
        "status": "blocked" if readiness_blockers else ("warning" if readiness_warnings else "ready"),
        "blockers": readiness_blockers,
        "warnings": readiness_warnings,
        "checks": {
            "connected": connected_flag,
            "demo_account": bool(is_demo),
            "panic_stop": bool(risk_engine.panic_stopped),
            "kill_switch_enabled": bool(kill_switch_state.enabled),
            "gemini_state": gemini_status.get("state", "unavailable"),
            "benzinga_status": benzinga_status.get("status") or ("degraded" if benzinga_status.get("degraded") else "healthy"),
            "cloud_decision_enabled": cloud_decision_enabled,
            "cloud_decision_configured": cloud_decision_configured,
            "position_manager_running": position_protection_ready,
            "auto_trade_enabled": bool(risk_engine.auto_trade_enabled),
            "closed_trade_count": int(closed_trade_count or 0),
        },
    }
    live_gate_blockers: list[str] = []
    live_gate_warnings: list[str] = []
    if not connected_flag:
        live_gate_blockers.append("Connect an MT5 live trading account.")
    if bool(is_demo):
        live_gate_blockers.append("Connected account is demo. Real-money validation requires a live MT5 account.")
    if not bool(config.LIVE_TRADING_ENABLED):
        live_gate_blockers.append("Set LIVE_TRADING_ENABLED=true in the backend environment.")
    if bool(risk_engine.user_policy.demo_only_default):
        live_gate_blockers.append("Turn Demo-Only By Default off in Policy & Runtime.")
    if bool(risk_engine.panic_stopped):
        live_gate_blockers.append("Release panic stop before live execution.")
    if kill_switch_state.enabled:
        live_gate_blockers.append("Disable the global trading kill switch before execution.")
    if active_platform != "mt5":
        live_gate_warnings.append(f"Active platform is {active_platform}; this readiness check is for MT5 real-money trading.")
    trade_server_ok = False
    if connected_flag:
        try:
            if active_platform == "tradestation":
                trade_server_ok = bool(tradestation_connector.trade_server_connected())
            elif active_platform == "ibkr":
                trade_server_ok = bool(ibkr_connector.trade_server_connected())
            else:
                trade_server_ok = bool(connector.trade_server_connected())
        except Exception:
            trade_server_ok = False
    if connected_flag and not trade_server_ok:
        live_gate_blockers.append("Broker trade server is not connected.")
    if connected_flag and not position_protection_ready:
        live_gate_blockers.append("Position manager is not running.")
    if connected_flag and not bool(gemini_status.get("available", False)):
        live_gate_blockers.append("Gemini analysis provider is unavailable.")
    live_trading_readiness = {
        "status": "blocked" if live_gate_blockers else ("warning" if live_gate_warnings else "ready"),
        "can_execute_live_orders": len(live_gate_blockers) == 0,
        "blockers": live_gate_blockers,
        "warnings": live_gate_warnings,
        "checks": {
            "platform": active_platform,
            "connected": bool(connected_flag),
            "account_is_demo": bool(is_demo),
            "backend_live_trading_enabled": bool(config.LIVE_TRADING_ENABLED),
            "demo_only_default": bool(risk_engine.user_policy.demo_only_default),
            "panic_stop": bool(risk_engine.panic_stopped),
            "kill_switch_enabled": bool(kill_switch_state.enabled),
            "trade_server_connected": bool(trade_server_ok),
            "position_manager_running": bool(auto_trader.position_manager.is_running),
            "auto_trade_enabled": bool(risk_engine.auto_trade_enabled),
            "auto_trade_session_armed": bool(getattr(risk_engine, "auto_trade_session_armed", False)),
            "gemini_available": bool(gemini_status.get("available", False)),
        },
    }
    return {
        "connected": connected_flag,
        "platform": active_platform,
        "platforms_supported": ["mt5", "ibkr", "tradestation"],
        "service_mode": config.APP_SERVICE_MODE,
        "background_services": {
            "cloud_pro_services_enabled": bool(config.service_mode_pro_enabled),
            "auto_services_enabled": bool(config.service_mode_auto_enabled),
            "cloud_sync_enabled": bool(config.CLOUD_SYNC_ENABLED and config.service_mode_pro_enabled),
            "cloud_brain_enabled": bool(config.CLOUD_BRAIN_ENABLED and config.service_mode_pro_enabled),
            "cloud_archive_enabled": bool(config.CLOUD_ARCHIVE_ENABLED and config.service_mode_pro_enabled),
            "meta_training_enabled": bool(config.AUTO_META_TRAINING_ENABLED and config.service_mode_pro_enabled),
            "scheduled_reconciliation_enabled": bool(config.service_mode_auto_enabled),
        },
        "account": account.model_dump() if account else None,
        "terminal": terminal.model_dump() if terminal else None,
        "last_error": last_error,
        "is_demo": is_demo,
        "live_trading_enabled": config.LIVE_TRADING_ENABLED,
        "panic_stop": risk_engine.panic_stopped,
        "kill_switch": kill_switch_state.model_dump(),
        "active_agent": active_agent_name,
        "active_strategy": {
            "id": selected_strategy_id,
            "source": "runtime",
        },
        "credential_storage_available": credential_vault.available,
        "gemini_available": gemini_status.get("available", False),
        "gemini_degraded": gemini_status.get("degraded", False),
        "gemini_last_error": gemini_status.get("last_error"),
        "gemini_state": gemini_status.get("state", "unavailable"),
        "gemini_primary_model": gemini_status.get("primary_model", ""),
        "gemini_active_model": gemini_status.get("active_model", ""),
        "gemini_using_fallback_model": bool(gemini_status.get("using_fallback_model", False)),
        "gemini_cooldown_seconds": int(gemini_status.get("cooldown_seconds", 0) or 0),
        "gemini_credits_pct": int(gemini_status.get("credits_pct", 0) or 0),
        "user_policy": risk_engine.user_policy.model_dump(),
        "runtime_controls": risk_engine.runtime_controls(),
        "permissions": user_permissions,
        "universe": universe_service.summary_dict(),
        "benzinga": benzinga_status,
        "api_keys_health": api_keys_health,
        "portfolio": portfolio_snapshot,
        "portfolio_targets": auto_trader.position_manager.portfolio_targets_snapshot(),
        "publish_readiness": publish_readiness,
        "live_trading_readiness": live_trading_readiness,
        "services": task_orchestrator.status_snapshot(),
        "reconciliation": reconciliation_service.last_report(),
        "auto_connect": {
            "pending": bool(_auto_connect_state.get("pending")),
            "last_started_at": float(_auto_connect_state.get("last_started_at") or 0.0),
            "last_finished_at": float(_auto_connect_state.get("last_finished_at") or 0.0),
            "last_error": str(_auto_connect_state.get("last_error") or ""),
            "paused": bool(_auto_connect_state.get("paused")),
            "paused_at": float(_auto_connect_state.get("paused_at") or 0.0),
            "pause_reason": str(_auto_connect_state.get("pause_reason") or ""),
        },
        "cloud_brain": cloud_brain_snapshot(),
        "cloud_decision": {
            "enabled": cloud_decision_enabled,
            "configured": cloud_decision_configured,
            "url": config.CLOUD_BRAIN_DECISION_URL,
            "last_error": getattr(signal_pipeline.cloud_brain_decision_client, "last_error", None),
            "disabled_reason": cloud_decision_reason,
        },
    }


@router.post("/reconcile")
async def reconcile_runtime_state(limit: int = 300):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform != "mt5":
        raise HTTPException(status_code=400, detail="Runtime reconciliation is currently implemented for MT5.")
    return await reconciliation_service.run(limit=max(1, min(int(limit), 1000)))


@router.get("/execution/simulated-results")
async def get_simulated_execution_results(limit: int = 100):
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    return await db.get_recent_simulated_execution_results(limit=max(1, min(int(limit), 500)))


@router.get("/trade-card/{signal_id}")
async def get_trade_card(signal_id: int):
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    signal = await db.get_signal_by_id(int(signal_id))
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found.")
    risk = await db.get_risk_decision_by_signal(int(signal_id))
    journals = await db.get_evaluation_journal_by_signal_ids([int(signal_id)])
    journal = journals.get(int(signal_id), {})
    order = await db.get_latest_order_by_signal_id(int(signal_id))
    gemini = _as_dict_json(journal.get("gemini_assessment"))
    execution_decision = _as_dict_json(journal.get("execution_decision"))
    technical = _as_dict_json(journal.get("raw_technical_signal"))
    executable = _as_dict_json(journal.get("executable_signal"))
    action = str(signal.get("action") or "HOLD").upper()
    approved = bool((risk or {}).get("approved")) if risk else False
    executed = bool((order or {}).get("success"))
    block_reasons = []
    risk_reason = str((risk or {}).get("reason") or "").strip()
    order_reason = str((order or {}).get("retcode_desc") or "").strip()
    if not approved and risk_reason:
        block_reasons.append(risk_reason)
    if order and not executed and order_reason:
        block_reasons.append(order_reason)
    final_decision = "executed" if executed else ("approved" if approved and action in {"BUY", "SELL"} else ("hold" if action == "HOLD" else "blocked"))
    return {
        "signal_id": int(signal_id),
        "order_id": (order or {}).get("id"),
        "symbol": signal.get("symbol"),
        "market": universe_service.expected_category_for_symbol(str(signal.get("symbol") or "")),
        "action": action,
        "confidence": signal.get("confidence"),
        "risk_score": (execution_decision.get("risk_score") or execution_decision.get("portfolio_fit_score")),
        "risk_mode": str(getattr(risk_engine.user_policy, "mode", "") or ""),
        "entry_reference_price": execution_decision.get("entry_reference_price") or executable.get("entry_price"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "reward": execution_decision.get("reward") or execution_decision.get("reward_risk"),
        "invalidation_level": execution_decision.get("invalidation_level") or signal.get("stop_loss"),
        "timeframe_alignment": {
            "H4": technical.get("trend_h4") or technical.get("h4"),
            "H1": technical.get("trend_h1") or technical.get("h1"),
            "M15": technical.get("trend_m15") or technical.get("m15"),
        },
        "technical_evidence": technical,
        "news_gemini_evidence": gemini,
        "disagreement_warnings": (
            execution_decision.get("warnings")
            or gemini.get("warnings")
            or []
        ),
        "portfolio_exposure_impact": execution_decision.get("portfolio_exposure_impact") or {},
        "broker_preflight_status": {
            "approved": approved,
            "reason": risk_reason,
        },
        "final_decision": final_decision,
        "block_reasons": list(dict.fromkeys([reason for reason in block_reasons if reason])),
        "execution_status": {
            "executed": executed,
            "ticket": (order or {}).get("ticket"),
            "retcode": (order or {}).get("retcode"),
            "retcode_desc": order_reason,
            "execution_mode": (order or {}).get("execution_mode") or config.EXECUTION_MODE,
            "idempotency_key": (order or {}).get("idempotency_key") or "",
        },
        "model_strategy_version": {
            "agent": signal.get("agent_name"),
            "strategy_id": (getattr(risk_engine, "active_strategy_context", {}) or {}).get("id"),
            "model_version": execution_decision.get("model_version") or gemini.get("model_version"),
        },
        "created_at": signal.get("timestamp"),
    }


@router.get("/kill-switch")
async def get_kill_switch():
    return kill_switch_service.current_state().model_dump()


@router.post("/kill-switch")
async def enable_kill_switch(
    req: KillSwitchUpdateRequest,
    authorization: Optional[str] = Header(default=None),
):
    user = await _resolve_authenticated_user(authorization)
    state = await kill_switch_service.enable(
        reason=req.reason or "Enabled from API.",
        set_by=str(user.get("email") or user.get("id") or "local"),
    )
    risk_engine.auto_trade_enabled = False
    _arm_auto_trade_session(False)
    task_orchestrator.stop_auto_trade(keep_position_manager=True)
    _ensure_position_manager_running_if_connected()
    _schedule_runtime_state_persist()
    _schedule_connection_event("kill_switch_enabled", details=state.reason)
    return state.model_dump()


@router.delete("/kill-switch")
async def disable_kill_switch(
    req: KillSwitchUpdateRequest | None = None,
    authorization: Optional[str] = Header(default=None),
):
    user = await _require_runtime_admin(authorization)
    state = await kill_switch_service.disable(
        reason=(req.reason if req else "") or "Disabled from API.",
        set_by=str(user.get("email") or user.get("id") or "local"),
    )
    _schedule_connection_event("kill_switch_disabled", details=state.reason)
    return state.model_dump()


@router.post("/cloud-brain/apply")
async def cloud_brain_apply(command: dict):
    """Apply a cloud-brain command immediately (manual/testing endpoint)."""
    result = await apply_cloud_brain_command(command)
    return {
        "ok": True,
        "result": result,
        "cloud_brain": cloud_brain_snapshot(),
    }


@router.post("/cloud-brain/decide")
async def cloud_brain_decide(payload: dict):
    """Cloud-side high-level decision API.

    Receives analyzed factors from local runtime and returns action guidance.
    This endpoint does not execute trades.
    """
    signal = dict(payload.get("signal") or {})
    quality = dict(payload.get("quality") or {})
    user_policy = dict(payload.get("user_policy") or {})
    gemini = dict(payload.get("gemini") or {}) if isinstance(payload.get("gemini"), dict) else {}
    meta_model = dict(payload.get("meta_model") or {}) if isinstance(payload.get("meta_model"), dict) else {}

    action = str(signal.get("action", "HOLD")).upper()
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    quality_score = float(quality.get("score", 0.0) or 0.0)
    no_trade_zone = bool(quality.get("no_trade_zone", False))
    no_trade_reasons = list(quality.get("no_trade_reasons") or [])
    mode = str(user_policy.get("mode", "balanced")).lower()

    # Cloud policy thresholds can evolve centrally.
    min_conf_by_mode = {
        "safe": 0.70,
        "balanced": 0.60,
        "aggressive": 0.45,
    }
    min_quality_by_mode = {
        "safe": 0.74,
        "balanced": 0.66,
        "aggressive": 0.55,
    }
    min_conf = min_conf_by_mode.get(mode, 0.60)
    min_quality = min_quality_by_mode.get(mode, 0.66)

    reasons: list[str] = []
    final_action = action if action in {"BUY", "SELL"} else "HOLD"

    if no_trade_zone:
        final_action = "HOLD"
        reasons.append("No-trade zone from local quality gate.")
        reasons.extend(no_trade_reasons[:2])

    if confidence < min_conf:
        final_action = "HOLD"
        reasons.append(f"Confidence below {min_conf:.0%} threshold for {mode} mode.")

    if quality_score < min_quality:
        final_action = "HOLD"
        reasons.append(f"Quality below {min_quality:.0%} threshold for {mode} mode.")

    contradiction = bool(gemini.get("contradiction_flag", False))
    if contradiction and mode in {"safe", "balanced"}:
        final_action = "HOLD"
        reasons.append("Gemini contradiction in non-aggressive mode.")

    no_trade_prob = float(meta_model.get("no_trade_probability", 0.0) or 0.0)
    if no_trade_prob >= 0.60:
        final_action = "HOLD"
        reasons.append("Meta-model no-trade probability is high.")

    target_confidence = confidence
    if final_action == "HOLD":
        target_confidence = min(confidence, max(0.0, confidence - 0.08))
    else:
        target_confidence = max(confidence, min(0.95, confidence + 0.02))

    return {
        "source": "cloud_brain",
        "action": final_action,
        "confidence": round(float(target_confidence), 4),
        "reason": "; ".join(reasons) if reasons else "Cloud brain approved local setup.",
        "mode": mode,
        "thresholds": {
            "min_confidence": min_conf,
            "min_quality": min_quality,
        },
    }


@router.post("/storage/compact")
async def compact_local_storage():
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not initialized.")
    result = await db.compact_storage()
    return {
        "ok": True,
        "result": result,
    }


@router.get("/account")
async def account():
    _ensure_mt5_platform_contract()
    if active_platform == "tradestation" and tradestation_connector.connected:
        info = tradestation_connector.refresh_account()
        if info is None:
            raise HTTPException(status_code=500, detail="Failed to get TradeStation account info")
        return info.model_dump()
    elif active_platform == "ibkr" and ibkr_connector.connected:
        info = await _run_ibkr(ibkr_connector.refresh_account)
        if info is None:
            raise HTTPException(status_code=500, detail="Failed to get IBKR account info")
        return info.model_dump()
    _require_active_platform_connection()
    info = connector.refresh_account()
    if info is None:
        raise HTTPException(status_code=500, detail="Failed to get account info")
    return info.model_dump()


@router.get("/ibkr/debug/account-summary")
async def ibkr_debug_account_summary():
    raise HTTPException(status_code=404, detail="IBKR debug routes are unavailable. This runtime supports MT5 only.")


class VerifyTerminalRequest(BaseModel):
    path: Optional[str] = None


@router.post("/verify-terminal")
async def verify_terminal(req: VerifyTerminalRequest):
    cleaned = req.path.strip().strip('"').strip("'") if req.path else None
    return connector.verify_terminal(cleaned)


# --- Market Data Routes ---

class SymbolSelectRequest(BaseModel):
    symbols: list[str]


@router.post("/symbols/select")
async def select_symbols(req: SymbolSelectRequest):
    results = {}
    for sym in req.symbols:
        ok = market_data.enable_symbol(_resolve_market_symbol(sym))
        results[sym] = ok
    return results


@router.get("/market/tick/{symbol}")
async def get_tick(symbol: str):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        snap = await _run_ibkr(ibkr_connector.get_stock_snapshot, symbol)
        if not snap:
            raise HTTPException(status_code=404, detail=f"No IBKR tick data for {symbol}")
        spread = max(0.0, float(snap["ask"]) - float(snap["bid"]))
        now_msc = int(time.time() * 1000)
        snap_time_msc = int(float(snap.get("time_msc") or 0) or 0)
        snap_time = int(float(snap.get("time") or 0) or 0)
        return {
            "symbol": snap["symbol"],
            "bid": float(snap["bid"]),
            "ask": float(snap["ask"]),
            "spread": spread,
            "time": snap_time or int(snap_time_msc / 1000) or int(now_msc / 1000),
            "time_msc": snap_time_msc or (snap_time * 1000 if snap_time else now_msc),
            "source": "ibkr_broker_snapshot",
            "market_data_type": snap.get("market_data_type"),
        }
    tick = market_data.get_tick(_resolve_market_symbol(symbol))
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No tick data for {symbol}")
    if not _is_fresh_tick(tick):
        raise HTTPException(status_code=404, detail=f"No fresh tick data for {symbol}")
    payload = tick.model_dump()
    payload["source"] = "mt5_broker_tick"
    return payload


def _resolve_market_tick_symbol_cached(symbol: str) -> str:
    key = str(symbol or "").strip().upper()
    if not key:
        return ""
    cached = _market_tick_symbol_resolution_cache.get(key)
    if cached:
        return cached
    resolved = _resolve_market_symbol(symbol)
    if resolved:
        _market_tick_symbol_resolution_cache[key] = resolved
    return resolved


def _fetch_mt5_tick_payloads_uncached_sync(requested: list[str], *, include_position_fallback: bool = True) -> dict[str, dict]:
    if not requested:
        return {}
    requested_to_resolved: dict[str, str] = {}
    resolved_symbols: list[str] = []
    seen_resolved: set[str] = set()
    for sym in requested:
        resolved = _resolve_market_tick_symbol_cached(sym)
        requested_to_resolved[sym] = resolved
        if resolved and resolved not in seen_resolved:
            resolved_symbols.append(resolved)
            seen_resolved.add(resolved)

    bulk = market_data.get_ticks(resolved_symbols)
    position_price_by_symbol: dict[str, float] = {}
    if include_position_fallback:
        try:
            open_positions = execution.get_positions()
            requested_keys = {str(s or "").strip().upper() for s in requested + resolved_symbols if str(s or "").strip()}
            for pos in open_positions or []:
                pos_symbol = str(getattr(pos, "symbol", "") or "").strip()
                pos_key = pos_symbol.upper()
                if pos_key not in requested_keys:
                    continue
                pos_price = float(getattr(pos, "price_current", 0.0) or 0.0)
                if pos_symbol and pos_price > 0:
                    position_price_by_symbol[pos_key] = pos_price
        except Exception as exc:
            logger.debug("Position price fallback unavailable for market ticks: %s", exc)

    ticks: dict[str, dict] = {}
    latest_tick_seconds = max((_tick_timestamp_seconds(tick) for tick in bulk.values()), default=0.0)
    now_msc = int(time.time() * 1000)
    for sym in requested:
        resolved = requested_to_resolved.get(sym) or sym
        tick = bulk.get(resolved)
        tick_seconds = _tick_timestamp_seconds(tick)
        tick_recent_in_batch = latest_tick_seconds <= 0.0 or (latest_tick_seconds - tick_seconds) <= 30.0
        if tick is not None and tick_recent_in_batch and _is_fresh_tick(tick, max_age_seconds=30.0):
            payload = tick.model_dump()
            payload["symbol"] = sym
            payload["broker_time"] = payload.get("time")
            payload["broker_time_msc"] = payload.get("time_msc")
            payload["received_at_msc"] = now_msc
            ticks[sym] = payload
            continue
        try:
            direct_tick = market_data.get_tick(resolved or sym)
        except Exception:
            direct_tick = None
        if direct_tick is not None and _is_fresh_tick(direct_tick, max_age_seconds=30.0):
            payload = direct_tick.model_dump()
            payload["symbol"] = sym
            payload["broker_time"] = payload.get("time")
            payload["broker_time_msc"] = payload.get("time_msc")
            payload["received_at_msc"] = now_msc
            ticks[sym] = payload
            continue
        fallback_price = (
            position_price_by_symbol.get(str(resolved or "").upper())
            or position_price_by_symbol.get(str(sym or "").upper())
        )
        if fallback_price and fallback_price > 0:
            now_sec = int(now_msc / 1000)
            ticks[sym] = {
                "symbol": sym,
                "bid": fallback_price,
                "ask": fallback_price,
                "spread": 0.0,
                "time": now_sec,
                "time_msc": now_msc,
                "broker_time": now_sec,
                "broker_time_msc": now_msc,
                "received_at_msc": now_msc,
                "price_source": "mt5_position_price",
            }
    return ticks


async def _fetch_mt5_tick_payloads_uncached(requested: list[str], *, include_position_fallback: bool = True) -> dict[str, dict]:
    return await asyncio.to_thread(
        _fetch_mt5_tick_payloads_uncached_sync,
        list(requested),
        include_position_fallback=include_position_fallback,
    )


def _cache_market_tick_payloads(ticks: dict[str, dict]) -> None:
    now = time.time()
    for sym, payload in (ticks or {}).items():
        key = str(sym or "").strip().upper()
        if key and payload:
            _market_tick_payload_cache[key] = (now, dict(payload))


def _market_tick_refresh_key(requested: list[str]) -> str:
    return "|".join(sorted({str(sym or "").strip().upper() for sym in requested if str(sym or "").strip()}))


async def _refresh_market_tick_payload_cache(requested: list[str]) -> None:
    refresh_key = _market_tick_refresh_key(requested)
    if not refresh_key or refresh_key in _market_tick_refreshing_keys:
        return
    now = time.time()
    last_started = float(_market_tick_refresh_last_started.get(refresh_key, 0.0) or 0.0)
    if (now - last_started) < _MARKET_TICK_REFRESH_MIN_INTERVAL_SECONDS:
        return
    _market_tick_refresh_last_started[refresh_key] = now
    _market_tick_refreshing_keys.add(refresh_key)
    try:
        ticks = await _fetch_mt5_tick_payloads_uncached(requested)
        _cache_market_tick_payloads(ticks)
    except Exception as exc:
        logger.debug("Market tick background refresh failed: %s", exc)
    finally:
        _market_tick_refreshing_keys.discard(refresh_key)


async def _get_mt5_tick_payloads_live(requested: list[str], *, wait_for_missing: bool = True) -> dict[str, dict]:
    now = time.time()
    refresh_key = _market_tick_refresh_key(requested)
    is_refreshing = bool(refresh_key and refresh_key in _market_tick_refreshing_keys)
    cached: dict[str, dict] = {}
    oldest_age = 0.0
    missing: list[str] = []
    for sym in requested:
        key = str(sym or "").strip().upper()
        row = _market_tick_payload_cache.get(key)
        if not row:
            missing.append(sym)
            continue
        age = now - float(row[0] or 0.0)
        oldest_age = max(oldest_age, age)
        if age <= _MARKET_TICK_STALE_RETURN_SECONDS:
            cached[sym] = dict(row[1])
        else:
            missing.append(sym)

    if cached and not missing:
        if oldest_age >= _MARKET_TICK_REFRESH_AFTER_SECONDS and not is_refreshing:
            asyncio.create_task(_refresh_market_tick_payload_cache(list(requested)))
        return cached

    if cached and missing:
        if wait_for_missing and len(requested) <= 10:
            ticks = await _fetch_mt5_tick_payloads_uncached(missing)
            _cache_market_tick_payloads(ticks)
            return {**cached, **ticks}
        if not is_refreshing:
            asyncio.create_task(_refresh_market_tick_payload_cache(list(requested)))
        return cached

    if not wait_for_missing or len(requested) > 10:
        if not is_refreshing:
            asyncio.create_task(_refresh_market_tick_payload_cache(list(requested)))
        return cached

    ticks = await _fetch_mt5_tick_payloads_uncached(requested)
    _cache_market_tick_payloads(ticks)
    return ticks


@router.get("/market/ticks")
async def get_ticks(symbols: str, ui_fast: bool = Query(default=False)):
    """Bulk tick endpoint to keep prices live with one request."""
    _ensure_mt5_platform_contract()
    try:
        _require_active_platform_connection()
    except HTTPException:
        return {"ticks": {}, "source": "disconnected"}
    raw_symbols = [s.strip() for s in (symbols or "").split(",") if s.strip()]
    # Defensive cap to avoid oversized requests from UI.
    requested = raw_symbols[:120]
    if not requested:
        return {"ticks": {}, "source": "ibkr_broker_snapshot" if active_platform == "ibkr" else "mt5_broker_tick"}

    ticks: dict[str, dict] = {}
    if active_platform == "ibkr":
        snapshots = await _run_ibkr(ibkr_connector.get_snapshots, requested)
        for sym in requested:
            snap = (snapshots or {}).get(str(sym or "").upper())
            if not snap:
                continue
            spread = max(0.0, float(snap["ask"]) - float(snap["bid"]))
            now_msc = int(time.time() * 1000)
            snap_time_msc = int(float(snap.get("time_msc") or 0) or 0)
            snap_time = int(float(snap.get("time") or 0) or 0)
            ticks[sym] = {
                "symbol": str(snap["symbol"]),
                "bid": float(snap["bid"]),
                "ask": float(snap["ask"]),
                "spread": spread,
                "time": snap_time or int(snap_time_msc / 1000) or int(now_msc / 1000),
                "time_msc": snap_time_msc or (snap_time * 1000 if snap_time else now_msc),
                "market_data_type": snap.get("market_data_type"),
            }
    else:
        ui_fast_enabled = ui_fast if isinstance(ui_fast, bool) else False
        ticks = await _get_mt5_tick_payloads_live(requested, wait_for_missing=not ui_fast_enabled)

    return {
        "ticks": ticks,
        "source": "ibkr_broker_snapshot" if active_platform == "ibkr" else "mt5_broker_tick",
    }


@router.get("/market/ticks/stream")
async def get_ticks_stream(request: Request, symbols: str):
    """Low-latency SSE stream for live bid/ask updates on the dashboard."""
    _ensure_mt5_platform_contract()
    try:
        _require_active_platform_connection()
    except HTTPException:
        async def disconnected_generator():
            yield "retry: 500\n\n"
            while True:
                if await request.is_disconnected():
                    break
                payload = json.dumps(
                    {"ticks": {}, "emitted_at_msc": int(time.time() * 1000), "source": "disconnected"},
                    separators=(",", ":"),
                )
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(disconnected_generator(), media_type="text/event-stream")
    raw_symbols = [s.strip() for s in (symbols or "").split(",") if s.strip()]
    requested = raw_symbols[:120]
    stream_interval_seconds = max(0.025, float(config.MARKET_TICK_STREAM_INTERVAL_MS) / 1000.0)

    async def event_generator():
        stream_sleep = max(0.025, float(config.MARKET_TICK_STREAM_INTERVAL_MS) / 1000.0)
        last_payload = ""
        yield "retry: 250\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                ticks: dict[str, dict] = {}
                if active_platform == "ibkr":
                    snapshots = await _run_ibkr(ibkr_connector.get_snapshots, requested)
                    for sym in requested:
                        snap = (snapshots or {}).get(str(sym or "").upper())
                        if not snap:
                            continue
                        spread = max(0.0, float(snap["ask"]) - float(snap["bid"]))
                        now_msc = int(time.time() * 1000)
                        snap_time_msc = int(float(snap.get("time_msc") or 0) or 0)
                        snap_time = int(float(snap.get("time") or 0) or 0)
                        ticks[sym] = {
                            "symbol": sym,
                            "bid": float(snap["bid"]),
                            "ask": float(snap["ask"]),
                            "spread": spread,
                            "time": snap_time or int(snap_time_msc / 1000) or int(now_msc / 1000),
                            "time_msc": snap_time_msc or (snap_time * 1000 if snap_time else now_msc),
                            "market_data_type": snap.get("market_data_type"),
                        }
                else:
                    ticks = await _fetch_mt5_tick_payloads_uncached(requested, include_position_fallback=False)

                payload = json.dumps(
                    {
                        "ticks": ticks,
                        "emitted_at_msc": int(time.time() * 1000),
                        "source": "ibkr_broker_snapshot" if active_platform == "ibkr" else "mt5_broker_tick",
                    },
                    separators=(",", ":"),
                )
                if payload != last_payload:
                    last_payload = payload
                    yield f"data: {payload}\n\n"
                else:
                    yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Tick stream iteration failed: %s", exc)
                yield ": error\n\n"
            await asyncio.sleep(stream_sleep)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/market/bars/{symbol}")
async def get_bars(symbol: str, timeframe: str = "H1", count: int = 100):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        canonical = universe_service.canonical_symbol(symbol)
        bars = await _run_ibkr(ibkr_connector.get_bars, canonical, timeframe, int(count))
        if not bars:
            raise HTTPException(status_code=404, detail=f"No IBKR bars for {canonical}")
        return bars
    bars = market_data.get_bars(_resolve_market_symbol(symbol), timeframe, count)
    return [b.model_dump() for b in bars]


@router.get("/market/symbol-info/{symbol}")
async def get_symbol_info(symbol: str):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        canonical = universe_service.canonical_symbol(symbol)
        category = universe_service.expected_category_for_symbol(canonical) or "Stocks"
        return {
            "name": canonical,
            "description": f"{canonical} via IBKR",
            "path": "IBKR",
            "point": 0.01,
            "digits": 2,
            "trade_contract_size": 1.0,
            "volume_min": 1.0,
            "volume_max": 100000.0,
            "volume_step": 1.0,
            "trade_mode": 4,
            "visible": True,
            "trade_stops_level": 0,
            "category": category,
            "commission_per_lot_side": 0.0,
            "commission_round_turn_per_lot": 0.0,
            "commission_model": "ibkr_not_implemented",
            "commission_percent_rate": None,
            "commission_notional_1lot": None,
        }
    info = market_data.get_symbol_info(_resolve_market_symbol(symbol))
    if info is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    base_commission = _commission_snapshot_from_symbol_info(info)
    history_commission = execution.get_recent_commission_by_symbol().get(info.get("name", ""))
    info.update(_merge_commission_snapshot(base_commission, history_commission))
    return info


@router.post("/broker/symbol-diagnostics")
async def broker_symbol_diagnostics(req: BrokerSymbolDiagnosticsRequest):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        return {
            "symbol": universe_service.canonical_symbol(req.symbol),
            "supported": False,
            "diagnostics": [
                {
                    "code": "broker_diagnostics_unavailable",
                    "message": "Broker diagnostics are currently implemented for MT5 symbols only.",
                    "details": {"platform": active_platform},
                    "suggested_fix": "Use MT5 diagnostics for broker-specific lot/stops/session checks.",
                }
            ],
        }

    symbol = _resolve_market_symbol(req.symbol)
    market_data.enable_symbol(symbol)
    tick = market_data.get_tick(symbol)
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No live tick data for {symbol}")
    sym_info = market_data.get_symbol_info(symbol)
    if not sym_info:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    context = _build_fast_execution_context(
        symbol=symbol,
        tick=tick,
        symbol_info=sym_info,
        evaluation_mode="diagnostics",
    )
    if context is None:
        raise HTTPException(status_code=404, detail=f"No live execution context for {symbol}")
    action = str(req.action or "").upper()
    entry_price = tick.ask if action == "BUY" else tick.bid
    try:
        margin_required = execution.estimate_margin(symbol, action, req.volume, entry_price)
    except Exception:
        margin_required = None
    issues = broker_symbol_validator.validate_context_order(
        context=context,
        action=action,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        entry_price=entry_price,
        margin_required=float(margin_required or 0.0),
    )
    return {
        "symbol": symbol,
        "action": action,
        "volume": req.volume,
        "entry_price": entry_price,
        "margin_required": margin_required,
        "supported": not bool(issues),
        "diagnostics": [issue.model_dump() for issue in issues],
        "symbol_info": {
            "digits": sym_info.get("digits"),
            "contract_size": sym_info.get("trade_contract_size"),
            "min_lot": sym_info.get("volume_min"),
            "max_lot": sym_info.get("volume_max"),
            "lot_step": sym_info.get("volume_step"),
            "stops_level": sym_info.get("trade_stops_level"),
            "freeze_level": sym_info.get("trade_freeze_level"),
            "trade_mode": sym_info.get("trade_mode"),
        },
    }


@router.get("/market/available-symbols")
async def get_available_symbols(
    category: Optional[str] = None,
    tradeable_only: bool = False,
    include_inactive: bool = False,
):
    """Get all symbols available on the MT5 terminal, optionally filtered by category."""
    _ensure_mt5_platform_contract()
    connected_for_catalog = _active_platform_connected()
    cache_key = _available_symbols_cache_key(
        platform=active_platform,
        connected=connected_for_catalog,
        category=category,
        tradeable_only=tradeable_only,
        include_inactive=include_inactive,
    )
    cached = _available_symbols_cache.get(cache_key)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) <= 10.0:
        return cached[1]
    if not connected_for_catalog:
        symbols = [] if tradeable_only else _mt5_disconnected_top_market_rows()
    elif active_platform == "ibkr":
        symbols = _ibkr_market_symbols(include_inactive=include_inactive)
    elif tradeable_only:
        symbols = market_data.get_tradeable_symbols()
    else:
        symbols = market_data.get_all_symbols()

    history_commission_map = execution.get_recent_commission_by_symbol() if active_platform != "ibkr" else {}
    if history_commission_map:
        enriched_symbols = []
        for item in symbols:
            row = dict(item)
            name = str(row.get("name", "") or "")
            merged = _merge_commission_snapshot(
                _commission_snapshot_from_symbol_info(row),
                history_commission_map.get(name),
            )
            row.update(merged)
            enriched_symbols.append(row)
        symbols = enriched_symbols

    symbols = universe_service.filter_market_symbols(symbols, include_inactive=include_inactive)
    if tradeable_only and active_platform != "ibkr":
        symbols = [s for s in symbols if _has_usable_broker_quote(s)]

    if category:
        target_category = universe_service.normalize_asset_class(category)
        symbols = [s for s in symbols if s["category"] == target_category]

    # Group by category for easy display
    categories: dict[str, list] = {}
    for s in symbols:
        cat = s["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(s)

    # Sort each category by name
    for cat in categories:
        categories[cat].sort(key=lambda s: s["name"])

    payload = {
        "total": len(symbols),
        "symbols": sorted(symbols, key=lambda s: (str(s.get("category", "")), str(s.get("name", "")))),
        "categories": categories,
        "category_counts": {k: len(v) for k, v in categories.items()},
        "universe": universe_service.summary_dict(),
        "connected": connected_for_catalog,
        "live_quotes": connected_for_catalog,
        "source": (
            "mt5_disconnected_top_markets"
            if not connected_for_catalog
            else "ibkr_broker_contracts"
            if active_platform == "ibkr"
            else "mt5_broker_symbols"
        ),
    }
    _available_symbols_cache[cache_key] = (now_ts, payload)
    return payload


@router.post("/market/auto-detect-symbols")
async def auto_detect_symbols(categories: Optional[list[str]] = None):
    """Auto-detect tradeable symbols and update allowed_symbols list."""
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()

    target_categories = [universe_service.normalize_asset_class(item) for item in (categories or universe_service.summary().active_asset_classes)]
    if active_platform == "ibkr":
        tradeable = _ibkr_market_symbols(include_inactive=False)
    else:
        tradeable = market_data.get_tradeable_symbols()
    filtered_tradeable = universe_service.filter_market_symbols(tradeable)
    matched = [s["name"] for s in filtered_tradeable if s["category"] in target_categories]

    if not matched:
        # Fallback: use visible symbols from MarketWatch
        visible = market_data.get_visible_symbols()
        filtered_visible = universe_service.filter_market_symbols(
            [s for s in visible if s["trade_enabled"]]
        )
        matched = [s["name"] for s in filtered_visible if s["category"] in target_categories]

    # User-triggered action: update the allowed symbol policy explicitly.
    updated_policy = risk_engine.user_policy.model_copy(
        update={"allowed_symbols": universe_service.restrict_symbols(matched)},
        deep=True,
    )
    risk_engine.update_user_policy(updated_policy)
    await _persist_runtime_state()
    logger.info(f"Auto-detected {len(matched)} tradeable symbols: {matched}")

    return {
        "detected": universe_service.restrict_symbols(matched),
        "count": len(universe_service.restrict_symbols(matched)),
        "universe": universe_service.summary_dict(),
    }


class RefreshEventsRequest(BaseModel):
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    news_category: str = "general"
    classify_with_gemini: bool = True


@router.get("/events/benzinga/health")
async def benzinga_health():
    return {
        "provider": benzinga_adapter.healthcheck(),
        "universe": universe_service.summary_dict(),
    }


@router.post("/events/refresh")
async def refresh_events(req: RefreshEventsRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    result = await event_ingestion_service.ingest_latest(
        from_date=req.from_date,
        to_date=req.to_date,
        news_category=req.news_category,
        classify_with_gemini=req.classify_with_gemini,
    )
    return {
        **result,
        "benzinga": benzinga_adapter.healthcheck(),
    }


@router.get("/events/latest")
async def latest_events(
    limit: int = 25,
    event_type: Optional[str] = None,
    order_by: str = "timestamp",
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    events = await event_ingestion_service.latest_events(
        limit=limit,
        usable_only=True,
        event_type=event_type,
        order_by=order_by,
    )
    return {
        "events": events,
        "count": len(events),
    }


@router.get("/events/mappings")
async def latest_event_mappings(limit: int = 100, event_id: Optional[int] = None):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    mappings = await db.get_event_asset_mappings(external_event_id=event_id, limit=limit)
    return {
        "mappings": mappings,
        "count": len(mappings),
    }


@router.get("/events/gemini-assessments")
async def latest_event_assessments(limit: int = 50, event_id: Optional[int] = None):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    assessments = await db.get_latest_gemini_event_assessments(
        external_event_id=event_id,
        limit=limit,
    )
    return {
        "assessments": assessments,
        "count": len(assessments),
    }


@router.get("/events/recommendations")
async def event_recommendations(
    limit: int = 8,
    force_refresh: bool = False,
    classify_with_gemini: bool = True,
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    if not connector.connected and active_platform != "ibkr":
        raise HTTPException(status_code=400, detail="Trading platform not connected")

    if force_refresh:
        await event_ingestion_service.ingest_latest(classify_with_gemini=classify_with_gemini)

    if active_platform == "ibkr":
        market_symbols = _ibkr_market_symbols(include_inactive=False)
    else:
        market_symbols = market_data.get_tradeable_symbols()
        if not market_symbols:
            market_symbols = [s for s in market_data.get_visible_symbols() if s.get("trade_enabled")]

    ranked_symbols = await event_ingestion_service.latest_candidate_rankings(
        universe_service=universe_service,
        market_symbols=market_symbols,
        limit=max(1, min(int(limit or 8), 20)),
    )
    recommendations: list[dict] = []
    for ranked in ranked_symbols:
        symbol = str(ranked.get("symbol") or "").strip()
        if not symbol:
            continue
        news_items = await event_ingestion_service.recent_symbol_news(
            symbol,
            universe_service=universe_service,
            limit=3,
            max_event_age_hours=24.0,
        )
        recommendations.append(
            {
                "symbol": symbol,
                "score": float(ranked.get("score", 0.0) or 0.0),
                "event_title": str(ranked.get("event_title", "") or ""),
                "event_type": str(ranked.get("event_type", "") or ""),
                "bias": str(ranked.get("bias", "neutral") or "neutral"),
                "event_risk": str(ranked.get("event_risk", "low") or "low"),
                "news": [item.model_dump() for item in news_items],
            }
        )

    return {
        "provider": benzinga_adapter.healthcheck(),
        "recommendations": recommendations,
        "count": len(recommendations),
    }


# --- Agent Routes ---

class EvaluateRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    agent_name: Optional[str] = None


class BuildStrategyRequest(BaseModel):
    name: str
    description: str
    apply_under_any_conditions: bool = False


class PreviewStrategyRequest(BaseModel):
    name: str
    description: str
    apply_under_any_conditions: bool = False


class SelectStrategyRequest(BaseModel):
    strategy_id: str


class UpdateStrategyRequest(BaseModel):
    strategy_id: str
    name: str
    description: str
    apply_under_any_conditions: bool = False


class DeleteStrategyRequest(BaseModel):
    strategy_id: str


@router.get("/strategies")
async def list_strategies(authorization: Optional[str] = Header(default=None)):
    user = await _resolve_authenticated_user(authorization)
    user_id = str(user.get("id") or "local-anonymous")
    profiles = await _list_strategies_for_user(user_id)
    selected_strategy_id = await _get_active_strategy_id_for_user(user_id)
    selected_profile = _resolve_strategy_from_profiles(selected_strategy_id, profiles)
    if not selected_profile:
        selected_strategy_id = _default_strategy_id()
    return {
        "active_strategy_id": selected_strategy_id,
        "items": [
            _strategy_response_item(profile, selected_id=selected_strategy_id)
            for profile in profiles
        ],
    }


@router.post("/strategies/preview")
async def preview_strategy(req: PreviewStrategyRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_create_strategy")
    name = _sanitize_custom_strategy_name(req.name)
    description = str(req.description or "").strip()
    if len(name) < 3:
        raise HTTPException(status_code=400, detail="Strategy name must be at least 3 characters.")
    if len(description) < 10:
        raise HTTPException(status_code=400, detail="Please provide a strategy description with more detail.")
    config = await _compile_strategy_config_async(
        name,
        description,
        apply_under_any_conditions=bool(req.apply_under_any_conditions),
        preview_mode=True,
    )
    return {
        "ok": True,
        "strategy": {
            "id": "preview",
            "name": name,
            "description": description,
            "brief": str(config.get("brief") or description),
            "scope": "preview",
            "is_default": False,
            "is_selected": False,
            "config": config,
        },
    }


@router.post("/strategies/build")
async def build_strategy(req: BuildStrategyRequest, authorization: Optional[str] = Header(default=None)):
    user = await _require_user_permission(authorization, "can_create_strategy")
    user_id = str(user.get("id") or "local-anonymous")
    name = _sanitize_custom_strategy_name(req.name)
    if len(name) < 3:
        raise HTTPException(status_code=400, detail="Strategy name must be at least 3 characters.")
    description = str(req.description or "").strip()
    if len(description) < 10:
        raise HTTPException(status_code=400, detail="Please provide a strategy description with more detail.")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available.")

    safe_name = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")[:40] or "custom"
    strategy_id = f"user-{user_id[:8]}-{safe_name}-{int(time.time())}"
    strategy_config = await _compile_strategy_config_async(
        name,
        description,
        apply_under_any_conditions=bool(req.apply_under_any_conditions),
    )
    await db.upsert_user_strategy(
        strategy_id=strategy_id,
        owner_user_id=user_id,
        name=name,
        description=description,
        config=strategy_config,
    )
    await _set_active_strategy_for_user(user_id, strategy_id)

    profile = {
        "id": strategy_id,
        "name": name,
        "description": description,
        "scope": "custom",
        "is_default": False,
        "config": strategy_config,
    }
    _apply_strategy_runtime(profile)
    await _persist_runtime_state()

    return {
        "ok": True,
        "active_strategy_id": strategy_id,
        "strategy": _strategy_response_item(profile, selected_id=strategy_id),
        "active_agent": active_agent_name,
        "user_policy": risk_engine.user_policy.model_dump(),
    }


@router.post("/strategies/select")
async def select_strategy(req: SelectStrategyRequest, authorization: Optional[str] = Header(default=None)):
    user = await _require_user_permission(authorization, "can_create_strategy")
    user_id = str(user.get("id") or "local-anonymous")
    strategy_id = str(req.strategy_id or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required.")

    profiles = await _list_strategies_for_user(user_id)
    selected = _resolve_strategy_from_profiles(strategy_id, profiles)
    if selected is None:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    _apply_strategy_runtime(selected)
    await _set_active_strategy_for_user(user_id, strategy_id)
    await _persist_runtime_state()

    return {
        "ok": True,
        "active_strategy_id": strategy_id,
        "active_strategy": _strategy_response_item(selected, selected_id=strategy_id),
        "active_agent": active_agent_name,
        "user_policy": risk_engine.user_policy.model_dump(),
        "scan_interval": risk_engine.auto_trade_scan_interval_seconds,
    }


@router.post("/strategies/update")
async def update_strategy(req: UpdateStrategyRequest, authorization: Optional[str] = Header(default=None)):
    user = await _require_user_permission(authorization, "can_create_strategy")
    user_id = str(user.get("id") or "local-anonymous")
    strategy_id = str(req.strategy_id or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required.")
    name = _sanitize_custom_strategy_name(req.name)
    if len(name) < 3:
        raise HTTPException(status_code=400, detail="Strategy name must be at least 3 characters.")
    description = str(req.description or "").strip()
    if len(description) < 10:
        raise HTTPException(status_code=400, detail="Please provide a strategy description with more detail.")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available.")

    existing = await db.get_user_strategy(owner_user_id=user_id, strategy_id=strategy_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Custom strategy not found.")

    strategy_config = await _compile_strategy_config_async(
        name,
        description,
        apply_under_any_conditions=bool(req.apply_under_any_conditions),
    )
    await db.upsert_user_strategy(
        strategy_id=strategy_id,
        owner_user_id=user_id,
        name=name,
        description=description,
        config=strategy_config,
    )

    selected_strategy_id = await _get_active_strategy_id_for_user(user_id)
    profile = {
        "id": strategy_id,
        "name": name,
        "description": description,
        "scope": "custom",
        "is_default": False,
        "config": strategy_config,
    }
    if selected_strategy_id == strategy_id:
        _apply_strategy_runtime(profile)
        await _persist_runtime_state()

    return {
        "ok": True,
        "active_strategy_id": selected_strategy_id,
        "strategy": _strategy_response_item(profile, selected_id=selected_strategy_id),
        "active_agent": active_agent_name,
        "user_policy": risk_engine.user_policy.model_dump(),
    }


@router.post("/strategies/delete")
async def delete_strategy(req: DeleteStrategyRequest, authorization: Optional[str] = Header(default=None)):
    user = await _require_user_permission(authorization, "can_create_strategy")
    user_id = str(user.get("id") or "local-anonymous")
    strategy_id = str(req.strategy_id or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required.")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available.")
    if strategy_id.startswith("builtin-"):
        raise HTTPException(status_code=400, detail="Built-in strategies cannot be deleted.")

    deleted_count = await db.delete_user_strategy(owner_user_id=user_id, strategy_id=strategy_id)
    if deleted_count <= 0:
        raise HTTPException(status_code=404, detail="Custom strategy not found.")

    selected_strategy_id = await _get_active_strategy_id_for_user(user_id)
    if selected_strategy_id == strategy_id:
        default_id = _default_strategy_id()
        profiles = await _list_strategies_for_user(user_id)
        fallback = _resolve_strategy_from_profiles(default_id, profiles) or (profiles[0] if profiles else None)
        if fallback is not None:
            _apply_strategy_runtime(fallback)
            await _set_active_strategy_for_user(user_id, str(fallback.get("id") or default_id))
            selected_strategy_id = str(fallback.get("id") or default_id)
            await _persist_runtime_state()

    return {
        "ok": True,
        "deleted_strategy_id": strategy_id,
        "active_strategy_id": selected_strategy_id,
        "active_agent": active_agent_name,
        "user_policy": risk_engine.user_policy.model_dump(),
    }


@router.post("/agent/evaluate")
async def evaluate_signal(req: EvaluateRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_generate_signal")
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        rec = await _evaluate_ibkr_symbol_recommendation(req.symbol)
        return {
            "signal": rec["signal"],
            "signal_id": rec.get("signal_id"),
            "risk_decision": rec["risk_decision"],
            "agent_name": "GeminiAgent" if active_agent_name == "GeminiAgent" else "SmartAgent",
            "degraded_reasons": rec.get("degraded_reasons", []),
            "gemini_confirmation": None,
            "trade_quality": rec.get("trade_quality"),
            "portfolio_risk": rec.get("portfolio_risk"),
            "anti_churn": rec.get("anti_churn"),
            "news": rec.get("news") or _news_payload_from_signal(rec.get("signal")),
            "execution_reason": rec.get("execution_reason", rec["risk_decision"]["reason"]),
            "position_management_plan": {
                "manage_position": True,
                "strategy": rec["signal"].get("strategy"),
                "max_holding_minutes": rec["signal"].get("max_holding_minutes"),
                "planned_hold_minutes": rec["signal"].get("max_holding_minutes"),
                "notes": ["IBKR route currently uses the local technical plan while Gemini-primary routing is applied on the main pipeline."],
            },
        }

    agent_name = req.agent_name or active_agent_name
    if agent_name not in agents:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    execution_decision = await signal_pipeline.evaluate(
        symbol=req.symbol,
        requested_agent_name=agent_name,
        requested_timeframe=req.timeframe,
        evaluation_mode="manual",
        bar_count=req.bar_count,
    )
    signal_id = await signal_pipeline.persist_evaluation(execution_decision, req.timeframe)
    signal = execution_decision.signal_decision.final_signal
    risk_decision = execution_decision.risk_evaluation

    return {
        "signal": signal.to_trade_signal().model_dump(),
        "signal_id": signal_id,
        "risk_decision": risk_decision.model_dump(),
        "agent_name": execution_decision.signal_decision.final_agent_name,
        "degraded_reasons": execution_decision.signal_decision.degraded_reasons,
        "gemini_confirmation": execution_decision.signal_decision.gemini_confirmation.model_dump()
        if execution_decision.signal_decision.gemini_confirmation
        else None,
        "trade_quality": execution_decision.trade_quality_assessment.model_dump(),
        "portfolio_risk": execution_decision.portfolio_risk_assessment.model_dump(),
        "anti_churn": execution_decision.anti_churn_assessment.model_dump(),
        "news": _news_payload_from_signal(signal.to_trade_signal().model_dump()),
        "execution_reason": execution_decision.reason,
        "position_management_plan": execution_decision.position_management_plan.model_dump(),
    }


@router.get("/agents")
async def list_agents():
    return {
        name: {"name": a.name, "description": a.description}
        for name, a in agents.items()
    }


class SetAgentRequest(BaseModel):
    agent_name: str


@router.post("/agent/set")
async def set_agent(req: SetAgentRequest):
    global active_agent_name
    if req.agent_name not in agents:
        raise HTTPException(status_code=404, detail=f"Agent '{req.agent_name}' not found")
    active_agent_name = req.agent_name
    auto_trader._auto_agent_name = req.agent_name
    return {"active_agent": active_agent_name}


# --- Trading Routes ---

@router.get("/trade/calculate-volume")
async def calculate_volume(symbol: str, amount_usd: float):
    """Convert a dollar amount (margin) to trading volume (lots) for a given symbol.

    The amount_usd is treated as margin budget and solved using MT5 margin estimation.
    This avoids incorrect sizing on symbols whose effective margin differs from account leverage.
    """
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        price = await _ibkr_reference_price(symbol)
        if price <= 0:
            raise HTTPException(status_code=400, detail=f"Invalid IBKR quote for {symbol}")
        # For IBKR stocks/ETFs, volume is shares. Keep at least 1 share.
        qty = max(1.0, round(amount_usd / price, 4))
        return {
            "symbol": symbol,
            "amount_usd": amount_usd,
            "volume": qty,
            "actual_cost": round(qty * price, 2),
            "price": price,
            "contract_size": 1.0,
            "volume_min": 1.0,
            "volume_max": 1_000_000.0,
            "min_sl_tp_dollars": 0.0,
            "stops_level": 0,
        }

    resolved_symbol = _resolve_market_symbol(symbol)
    tick = market_data.get_tick(resolved_symbol)
    if not tick:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")
    if not _is_fresh_tick(tick):
        raise HTTPException(status_code=404, detail=f"No fresh tick data for {resolved_symbol}")
    sym_info = market_data.get_symbol_info(resolved_symbol)
    if not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")

    price = tick.ask
    contract_size = sym_info.get("trade_contract_size", 100000)
    vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
    vol_max = float(sym_info.get("volume_max", 100) or 100)
    vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)

    if price <= 0 or contract_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid price or contract size")

    # amount_usd is margin â†’ multiply by leverage to get notional, then divide by lot value
    volume, estimated_margin = _solve_volume_by_margin_target(
        symbol=resolved_symbol,
        action="BUY",
        target_margin_usd=amount_usd,
        price=price,
        vol_min=vol_min,
        vol_max=vol_max,
        vol_step=vol_step,
    )
    if volume <= 0:
        min_required = estimated_margin if estimated_margin is not None else 0.0
        raise HTTPException(
            status_code=400,
            detail=f"Amount too small for this symbol. Minimum required margin is about ${min_required:.2f}.",
        )
    margin_required = round(float(estimated_margin if estimated_margin is not None else amount_usd), 2)

    # Calculate minimum SL/TP dollar amounts based on MT5 STOPLEVEL and spread
    point = sym_info.get("point", 0.00001)
    stops_level = sym_info.get("trade_stops_level", 0)
    spread_points = tick.spread if tick.spread else 10
    # When STOPLEVEL is 0, MT5 uses the current spread as minimum distance
    # Use the larger of: STOPLEVEL, spread, or 10 points as floor
    effective_stops = max(stops_level, spread_points, 10)
    # Add 50% buffer above the minimum to account for spread fluctuation
    min_price_distance = effective_stops * point * 1.5
    min_sl_tp_raw = _price_distance_to_usd(
        symbol_info=sym_info,
        entry_price=price,
        target_price=price + min_price_distance,
        volume=volume,
    )
    min_sl_tp_dollars = round(float(min_sl_tp_raw or 0.0), 2)
    # Ensure at least $1 minimum
    min_sl_tp_dollars = max(min_sl_tp_dollars, 1.0)
    account = connector.refresh_account()

    return {
        "symbol": resolved_symbol,
        "amount_usd": amount_usd,
        "volume": round(volume, 4),
        "actual_cost": margin_required,
        "price": price,
        "contract_size": contract_size,
        "volume_min": vol_min,
        "volume_max": vol_max,
        "trade_tick_value": float(sym_info.get("trade_tick_value", 0.0) or 0.0),
        "trade_tick_size": float(sym_info.get("trade_tick_size", 0.0) or 0.0),
        "point": float(point or 0.0),
        "leverage": (account.leverage if account else 1),
        "min_sl_tp_dollars": min_sl_tp_dollars,
        "stops_level": effective_stops,
    }


def _derive_amount_based_sl_tp(
    *,
    action: str,
    entry_price: float,
    volume: float,
    symbol_info: dict | None,
    sl_amount_usd: float,
    tp_amount_usd: float,
    digits: int,
    min_rr: float = 1.2,
) -> tuple[float, float]:
    price_per_unit = _usd_per_price_distance(symbol_info=symbol_info, volume=volume)
    if entry_price <= 0 or price_per_unit <= 0:
        return 0.0, 0.0

    sl_distance = sl_amount_usd / price_per_unit
    tp_distance = tp_amount_usd / price_per_unit
    # Guardrail: prevent unrealistic SL/TP distances (for example >100% TP on
    # low-priced symbols caused by amount-based conversions and oversized volumes).
    sl_cap_pct, tp_cap_pct = _sl_tp_distance_caps(symbol_info=symbol_info, entry_price=entry_price)
    max_sl_distance = entry_price * sl_cap_pct
    max_tp_distance = entry_price * tp_cap_pct
    if max_sl_distance > 0:
        sl_distance = min(sl_distance, max_sl_distance)
    if max_tp_distance > 0:
        tp_distance = min(tp_distance, max_tp_distance)
    # Keep RR sane even after cap/clamp effects by tightening SL if needed.
    required_rr = max(1.2, float(min_rr or 1.2))
    if sl_distance > 0 and tp_distance > 0 and tp_distance < (sl_distance * required_rr):
        sl_distance = tp_distance / required_rr
    action_upper = (action or "").upper()

    if action_upper == "BUY":
        sl = entry_price - sl_distance
        tp = entry_price + tp_distance
    else:
        sl = entry_price + sl_distance
        tp = entry_price - tp_distance

    return round(sl, digits), round(tp, digits)


def _derive_pct_based_sl_tp_fallback(
    *,
    action: str,
    entry_price: float,
    symbol_info: dict | None,
    min_rr: float,
    digits: int,
) -> tuple[float, float]:
    if entry_price <= 0:
        return 0.0, 0.0
    sl_cap_pct, tp_cap_pct = _sl_tp_distance_caps(symbol_info=symbol_info, entry_price=entry_price)
    # Conservative fallback distance when amount conversion is unavailable.
    sl_pct = max(0.006, min(sl_cap_pct * 0.22, sl_cap_pct * 0.9))
    tp_pct = max(sl_pct * max(1.15, float(min_rr or 1.0)), sl_pct * 1.8)
    tp_pct = min(tp_pct, max(tp_cap_pct * 0.9, sl_pct * 1.2))
    action_upper = str(action or "").upper()
    if action_upper == "BUY":
        sl = entry_price * (1.0 - sl_pct)
        tp = entry_price * (1.0 + tp_pct)
    else:
        sl = entry_price * (1.0 + sl_pct)
        tp = entry_price * (1.0 - tp_pct)
    return round(sl, digits), round(tp, digits)


def _sl_tp_distance_caps(*, symbol_info: dict | None, entry_price: float | None = None) -> tuple[float, float]:
    info = dict(symbol_info or {})
    path = str(info.get("path", "") or "").lower()
    desc = str(info.get("description", "") or "").lower()
    name = str(info.get("name", "") or "").upper()
    text = f"{path} {desc} {name}"
    if any(token in text for token in ("stock", "share", "equity", "nasdaq", "nyse")):
        price = float(entry_price or 0.0)
        if price > 0 and price <= 5:
            return (0.05, 0.12)
        if price > 0 and price <= 20:
            return (0.045, 0.10)
        return (0.05, 0.12)
    if any(token in text for token in ("index", "us30", "us500", "spx", "nas100", "dax", "ftse")):
        return (0.035, 0.08)
    if any(token in text for token in ("gold", "silver", "xau", "xag", "oil", "wti", "brent", "commodity")):
        return (0.035, 0.08)
    if any(token in text for token in ("forex", "eurusd", "gbpusd", "usdjpy")):
        return (0.015, 0.035)
    if any(token in text for token in ("crypto", "btc", "eth")):
        return (0.06, 0.14)
    return (0.04, 0.09)


def _usd_per_price_distance(*, symbol_info: dict | None, volume: float) -> float:
    if volume <= 0:
        return 0.0
    info = dict(symbol_info or {})
    tick_value = float(info.get("trade_tick_value", 0.0) or 0.0)
    tick_size = float(info.get("trade_tick_size", 0.0) or 0.0)
    point = float(info.get("point", 0.0) or 0.0)
    contract_size = float(info.get("trade_contract_size", 0.0) or 0.0)
    contract_based = (volume * contract_size) if contract_size > 0 else 0.0
    if tick_value > 0:
        unit = tick_size if tick_size > 0 else point
        if unit > 0:
            tick_based = volume * (tick_value / unit)
            # Some broker feeds under-report tick_value for metals/CFDs (e.g. XAGUSD),
            # causing SL/TP price conversion to be off by 5-10x. Prefer contract-size
            # pricing when it meaningfully differs upward.
            if contract_based > 0 and tick_based > 0:
                if tick_based < (contract_based * 0.5):
                    return contract_based
                if contract_based < (tick_based * 0.5):
                    return tick_based
                return max(tick_based, contract_based)
            return tick_based
    if contract_based > 0:
        return contract_based
    return 0.0


def _price_distance_to_usd(
    *,
    symbol_info: dict | None,
    entry_price: float,
    target_price: float,
    volume: float,
) -> float | None:
    if entry_price <= 0 or target_price <= 0 or volume <= 0:
        return None
    usd_per_unit = _usd_per_price_distance(symbol_info=symbol_info, volume=volume)
    if usd_per_unit <= 0:
        return None
    return abs(entry_price - target_price) * usd_per_unit


def _build_trade_comment(
    amount_usd: float | None,
    sl_amount_usd: float | None = None,
    tp_amount_usd: float | None = None,
) -> str:
    if amount_usd is None or amount_usd <= 0:
        return "TradingAgent"
    # MT5 order comment constraints are broker-dependent, but many servers reject
    # long/special-char payloads. Keep compact ASCII-only <= 31 chars.
    parts = [f"TA{int(round(float(amount_usd)))}"]
    if sl_amount_usd is not None and sl_amount_usd > 0:
        parts.append(f"SLA{int(round(float(sl_amount_usd)))}")
    if tp_amount_usd is not None and tp_amount_usd > 0:
        parts.append(f"TPA{int(round(float(tp_amount_usd)))}")
    comment = "|".join(parts)
    # Hard cap for broad MT5 compatibility.
    if len(comment) > 31:
        # Keep TA first, then SL/TP if space remains.
        compact = parts[0]
        for token in parts[1:]:
            trial = f"{compact}|{token}"
            if len(trial) <= 31:
                compact = trial
            else:
                break
        comment = compact[:31]
    return comment


def _account_risk_cap_usd(account, fallback_equity: float = 0.0) -> float:
    try:
        equity = float(getattr(account, "equity", 0.0) or 0.0)
    except Exception:
        equity = 0.0
    if equity <= 0:
        equity = float(fallback_equity or 0.0)
    try:
        risk_pct = float(getattr(risk_engine.user_policy, "max_risk_per_trade", 0.0) or 0.0)
    except Exception:
        risk_pct = 0.0
    return equity * (risk_pct / 100.0) if equity > 0 and risk_pct > 0 else 0.0


def _started_money_caps_usd(started_amount_usd: float) -> tuple[float, float]:
    started = max(0.0, float(started_amount_usd or 0.0))
    if started <= 0:
        return 0.0, 0.0
    mode = str(getattr(risk_engine.user_policy, "mode", "balanced") or "balanced").lower()
    if mode == "aggressive":
        return started * 0.18, started * 0.40
    if mode == "safe":
        return started * 0.08, started * 0.18
    return started * 0.12, started * 0.26


def _cap_order_targets_by_money_budget(
    *,
    action: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    symbol_info: dict | None,
    volume: float,
    started_amount_usd: float,
    account=None,
    fallback_equity: float = 0.0,
) -> tuple[float, float, float | None, float | None]:
    if entry_price <= 0 or volume <= 0 or stop_loss <= 0 or take_profit <= 0:
        return stop_loss, take_profit, None, None
    info = dict(symbol_info or {})
    try:
        digits = int(info.get("digits", 5) or 5)
    except Exception:
        digits = 5
    usd_per_price = _usd_per_price_distance(symbol_info=info, volume=volume)
    if usd_per_price <= 0:
        return stop_loss, take_profit, None, None

    started_sl_cap, started_tp_cap = _started_money_caps_usd(started_amount_usd)
    account_sl_cap = _account_risk_cap_usd(account, fallback_equity=fallback_equity)
    sl_caps = [cap for cap in (started_sl_cap, account_sl_cap) if cap > 0]
    if not sl_caps:
        return (
            stop_loss,
            take_profit,
            _price_distance_to_usd(
                symbol_info=info,
                entry_price=entry_price,
                target_price=stop_loss,
                volume=volume,
            ),
            _price_distance_to_usd(
                symbol_info=info,
                entry_price=entry_price,
                target_price=take_profit,
                volume=volume,
            ),
        )

    min_rr = max(1.2, float(getattr(risk_engine.user_policy, "min_reward_risk", 1.6) or 1.6))
    max_sl_amount = max(25.0, min(sl_caps))
    max_tp_amount = max(max_sl_amount * min_rr, started_tp_cap if started_tp_cap > 0 else 0.0)
    cur_sl_amount = abs(entry_price - stop_loss) * usd_per_price
    cur_tp_amount = abs(take_profit - entry_price) * usd_per_price
    next_sl_amount = min(cur_sl_amount if cur_sl_amount > 0 else max_sl_amount, max_sl_amount)
    next_tp_amount = max(cur_tp_amount if cur_tp_amount > 0 else next_sl_amount * min_rr, next_sl_amount * min_rr)
    if max_tp_amount > 0:
        next_tp_amount = min(next_tp_amount, max_tp_amount)
        if next_tp_amount < next_sl_amount * min_rr:
            next_sl_amount = max(25.0, next_tp_amount / min_rr)

    sl_distance = next_sl_amount / usd_per_price
    tp_distance = next_tp_amount / usd_per_price
    if str(action or "").upper() == "BUY":
        next_sl = round(entry_price - sl_distance, digits)
        next_tp = round(entry_price + tp_distance, digits)
    else:
        next_sl = round(entry_price + sl_distance, digits)
        next_tp = round(entry_price - tp_distance, digits)
    return next_sl, next_tp, next_sl_amount, next_tp_amount


def _sl_cap_for_order(
    *,
    started_amount_usd: float,
    account=None,
    fallback_equity: float = 0.0,
) -> float:
    started_sl_cap, _ = _started_money_caps_usd(started_amount_usd)
    account_sl_cap = _account_risk_cap_usd(account, fallback_equity=fallback_equity)
    caps = [cap for cap in (started_sl_cap, account_sl_cap) if cap > 0]
    return max(25.0, min(caps)) if caps else 0.0


def _comment_amount_for_mt5_trade(
    *,
    symbol: str,
    action: str,
    volume: float,
    fallback_amount_usd: float | None,
) -> float | None:
    """Prefer the broker-estimated current margin footprint over the raw requested budget.

    The dashboard previously displayed the requested budget (TAxxxx) as if it were
    actual broker margin, which caused major confusion whenever the final sized
    position differed from the original request. For MT5 trades, store the final
    estimated margin in the compact comment when available, and only fall back to
    the caller-provided amount if margin estimation is unavailable.
    """
    try:
        if volume > 0:
            estimated_margin = execution.estimate_margin(symbol=symbol, action=action, volume=volume)
            if estimated_margin is not None and float(estimated_margin) > 0:
                return float(estimated_margin)
    except Exception:
        pass
    if fallback_amount_usd is None or fallback_amount_usd <= 0:
        return None
    return float(fallback_amount_usd)


def _round_volume_down(
    *,
    raw_volume: float,
    vol_min: float,
    vol_step: float,
    vol_max: float,
) -> float:
    if raw_volume <= 0:
        return 0.0
    step = vol_step if vol_step and vol_step > 0 else max(vol_min, 0.01)
    bounded = min(raw_volume, vol_max) if vol_max > 0 else raw_volume
    units = math.floor((bounded + 1e-12) / step)
    rounded = units * step
    if rounded + 1e-12 < vol_min:
        return 0.0
    return round(rounded, 6)


def _solve_volume_by_margin_target(
    *,
    symbol: str,
    action: str,
    target_margin_usd: float,
    price: float,
    vol_min: float,
    vol_max: float,
    vol_step: float,
) -> tuple[float, float | None]:
    """Find the highest volume whose MT5 estimated margin is <= target margin.

    Returns (volume, estimated_margin). If MT5 margin estimation is unavailable,
    returns (0.0, None) so callers can choose a fallback.
    """
    if target_margin_usd <= 0:
        return 0.0, 0.0

    step = vol_step if vol_step and vol_step > 0 else max(vol_min, 0.01)
    min_units = max(1, math.ceil(vol_min / step))
    max_units = max(min_units, math.floor(vol_max / step)) if vol_max > 0 else max(min_units, 1_000_000)

    def _margin_for_units(units: int) -> float | None:
        volume = round(units * step, 6)
        margin = execution.estimate_margin(symbol=symbol, action=action, volume=volume, price=price)
        if margin is None:
            return None
        return float(margin)

    min_margin = _margin_for_units(min_units)
    if min_margin is None:
        return 0.0, None
    if min_margin > target_margin_usd:
        return 0.0, min_margin

    low = min_units
    high = max_units
    best_units = min_units
    best_margin = min_margin

    while low <= high:
        mid = (low + high) // 2
        margin = _margin_for_units(mid)
        if margin is None:
            return 0.0, None
        if margin <= target_margin_usd:
            best_units = mid
            best_margin = margin
            low = mid + 1
        else:
            high = mid - 1

    return round(best_units * step, 6), float(best_margin)


def _solve_volume_by_margin_target_fast(
    *,
    symbol: str,
    action: str,
    target_margin_usd: float,
    price: float,
    contract_size: float,
    vol_min: float,
    vol_max: float,
    vol_step: float,
) -> tuple[float, float | None]:
    """Fast margin->volume approximation for click-to-trade latency.

    Uses at most a few margin checks (instead of full binary search).
    Falls back to precise solver only when margin estimation is unavailable.
    """
    if target_margin_usd <= 0:
        return 0.0, 0.0

    step = vol_step if vol_step and vol_step > 0 else max(vol_min, 0.01)
    max_volume = vol_max if vol_max and vol_max > 0 else 1_000_000.0

    # Initial rough estimate from leverage formula (fast first guess).
    leverage = 1.0
    try:
        acct = connector.refresh_account()
        leverage = float(acct.leverage) if acct and acct.leverage else 1.0
    except Exception:
        leverage = 1.0
    rough = (
        (target_margin_usd * max(leverage, 1.0)) / (price * max(contract_size, 1e-9))
        if price > 0 and contract_size > 0
        else vol_min
    )
    volume = _round_volume_down(
        raw_volume=max(rough, vol_min),
        vol_min=vol_min,
        vol_step=step,
        vol_max=max_volume,
    )
    if volume <= 0:
        volume = _round_volume_down(raw_volume=vol_min, vol_min=vol_min, vol_step=step, vol_max=max_volume)
    if volume <= 0:
        return 0.0, None

    def _margin(v: float) -> float | None:
        m = execution.estimate_margin(symbol=symbol, action=action, volume=round(v, 6), price=price)
        return float(m) if m is not None else None

    margin = _margin(volume)
    if margin is None:
        return _solve_volume_by_margin_target(
            symbol=symbol,
            action=action,
            target_margin_usd=target_margin_usd,
            price=price,
            vol_min=vol_min,
            vol_max=max_volume,
            vol_step=step,
        )

    # Scale towards target with a couple of bounded iterations.
    for _ in range(3):
        if margin <= 0:
            break
        scale = target_margin_usd / margin
        # If already close enough, stop.
        if 0.96 <= scale <= 1.04:
            break
        candidate = _round_volume_down(
            raw_volume=max(vol_min, volume * max(0.2, min(scale, 5.0))),
            vol_min=vol_min,
            vol_step=step,
            vol_max=max_volume,
        )
        if candidate <= 0 or abs(candidate - volume) < 1e-9:
            break
        cand_margin = _margin(candidate)
        if cand_margin is None:
            break
        volume, margin = candidate, cand_margin

    # Ensure we never exceed target; step down quickly if needed.
    guard = 0
    while margin is not None and margin > target_margin_usd and volume > vol_min + 1e-9 and guard < 5:
        volume = _round_volume_down(
            raw_volume=max(vol_min, volume - step),
            vol_min=vol_min,
            vol_step=step,
            vol_max=max_volume,
        )
        margin = _margin(volume)
        guard += 1

    if margin is None:
        return 0.0, None
    if margin > target_margin_usd:
        min_margin = _margin(vol_min)
        return 0.0, float(min_margin) if min_margin is not None else float(margin)
    return round(volume, 6), float(margin)


def _rebalance_volume_for_started_amount_risk(
    *,
    entry_price: float,
    stop_loss: float | None,
    desired_sl_amount_usd: float | None,
    volume: float,
    contract_size: float,
    vol_min: float,
    vol_step: float,
    vol_max: float,
) -> tuple[float, str | None]:
    """Ensure actual SL-dollar risk stays aligned with the started amount target.

    If broker min distance widens SL, we reduce volume to preserve risk budget.
    """
    if (
        desired_sl_amount_usd is None
        or desired_sl_amount_usd <= 0
        or stop_loss is None
        or stop_loss <= 0
        or entry_price <= 0
        or contract_size <= 0
        or volume <= 0
    ):
        return volume, None

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        return volume, None

    actual_sl_amount = sl_distance * volume * contract_size
    # Allow tiny tolerance to avoid jitter from rounding.
    if actual_sl_amount <= desired_sl_amount_usd * 1.02:
        return volume, None

    max_volume_for_risk = desired_sl_amount_usd / (sl_distance * contract_size)
    adjusted_volume = _round_volume_down(
        raw_volume=max_volume_for_risk,
        vol_min=vol_min,
        vol_step=vol_step,
        vol_max=vol_max,
    )
    if adjusted_volume <= 0:
        min_possible_risk = sl_distance * max(vol_min, 0.0) * contract_size
        return 0.0, (
            f"Broker minimum stop distance implies at least ${min_possible_risk:.2f} risk "
            f"for this symbol at minimum volume, which exceeds your selected risk budget."
        )
    return adjusted_volume, None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _policy_risk_budget_usd(decision=None) -> float | None:
    try:
        equity = 0.0
        if decision is not None:
            equity = float(getattr(decision.signal_decision.market_context, "account_equity", 0.0) or 0.0)
        if equity <= 0:
            if active_platform == "ibkr":
                equity = float(getattr(ibkr_connector.account_info, "equity", 0.0) or 0.0)
            else:
                account = connector.refresh_account()
                equity = float(getattr(account, "equity", 0.0) or 0.0) if account is not None else 0.0
        risk_pct = float(risk_engine.user_policy.max_risk_per_trade or 0.0)
        if equity <= 0 or risk_pct <= 0:
            return None
        return equity * (risk_pct / 100.0)
    except Exception:
        return None


def _effective_trade_margin_budget(requested_amount_usd: float) -> float:
    """Clamp requested trade margin to custom-budget safeguards when enabled.

    This keeps manual and recommendation execution aligned with dashboard budget
    expectations (no single trade exceeding configured per-trade budget cap, and
    no new allocation past remaining custom budget headroom).
    """
    requested = float(requested_amount_usd or 0.0)
    if requested <= 0:
        return 0.0
    mode = str(getattr(risk_engine, "auto_trade_budget_mode", "full_margin") or "full_margin").lower()
    if mode != "custom_budget":
        return requested
    budget_usd = float(getattr(risk_engine, "auto_trade_budget_usd", 0.0) or 0.0)
    if budget_usd <= 0:
        return requested
    per_trade_pct = max(1.0, min(100.0, float(getattr(config, "AUTO_TRADE_MAX_SINGLE_TRADE_BUDGET_PCT", 25.0) or 25.0)))
    per_trade_cap = budget_usd * (per_trade_pct / 100.0)
    margin_used = 0.0
    try:
        if active_platform == "ibkr":
            margin_used = float(getattr(ibkr_connector.account_info, "margin", 0.0) or 0.0)
        else:
            account = connector.refresh_account()
            margin_used = float(getattr(account, "margin", 0.0) or 0.0) if account is not None else 0.0
    except Exception:
        margin_used = 0.0
    remaining_budget = max(0.0, budget_usd - max(0.0, margin_used))
    hard_cap = min(per_trade_cap, remaining_budget) if remaining_budget > 0 else 0.0
    if hard_cap <= 0:
        return 0.0
    return min(requested, hard_cap)


def _execution_trace_from_plan_or_order(plan: dict | None, order_ctx: dict | None) -> dict | None:
    plan_json = dict((plan or {}).get("plan_json") or {})
    metadata = dict(plan_json.get("metadata") or {})
    trace = metadata.get("execution_trace")
    if isinstance(trace, dict) and trace:
        return trace
    if not isinstance(order_ctx, dict) or not order_ctx:
        return None
    strategy_ctx = dict(getattr(risk_engine, "active_strategy_context", {}) or {})
    meta_service = getattr(signal_pipeline, "meta_model_service", None)
    return {
        "source": "order_log",
        "symbol": str(order_ctx.get("symbol") or "").strip().upper(),
        "action": str(order_ctx.get("action") or "").strip().upper(),
        "signal_id": order_ctx.get("signal_id"),
        "reason": str(order_ctx.get("signal_reason") or order_ctx.get("risk_reason") or order_ctx.get("retcode_desc") or "").strip(),
        "requested_agent_name": str(order_ctx.get("agent_name") or "").strip(),
        "active_agent_name": str(order_ctx.get("agent_name") or active_agent_name or "").strip(),
        "policy_mode": str(getattr(risk_engine.user_policy, "mode", "") or strategy_ctx.get("policy_mode") or "").strip(),
        "amount_usd": None,
        "preflight_reason": str(order_ctx.get("risk_reason") or "").strip(),
        "strategy": {
            "id": str(strategy_ctx.get("id") or "").strip(),
            "name": str(strategy_ctx.get("name") or "").strip(),
            "brief": str(strategy_ctx.get("brief") or "").strip(),
            "human_summary": str(strategy_ctx.get("human_summary") or "").strip(),
            "summary_lines": [str(line).strip() for line in (strategy_ctx.get("summary_lines") or []) if str(line).strip()],
        },
        "gemini": _current_gemini_status(),
        "meta_model": {
            "active": bool(getattr(meta_service, "is_active", False)) if meta_service is not None else False,
            "version_id": str(getattr(meta_service, "active_version_id", "") or "") if meta_service is not None else "",
        },
    }


async def _gemini_target_pct_adjustment(
    *,
    mode: str,
    action: str,
    range_pct: float,
    drift_pct: float,
    drift_aligned: bool,
    category: str,
    quality: float,
    threshold: float,
    news_bias: str,
    event_risk: str,
    contradiction_flag: bool,
) -> tuple[float, float]:
    global _gemini_quota_cooldown_until
    if time.time() < _gemini_quota_cooldown_until:
        return 0.0, 0.0
    if not gemini_agent.available or getattr(gemini_agent, "_client", None) is None:
        return 0.0, 0.0
    try:
        from google.genai import types as genai_types

        prompt_payload = {
            "task": "Adjust SL/TP percentages for one trade in a bounded way.",
            "risk_mode": mode,
            "action": action,
            "chart_last_hours": {
                "range_pct": range_pct,
                "drift_pct": drift_pct,
                "drift_aligned_with_action": drift_aligned,
            },
            "context": {
                "category": category,
                "trade_quality": quality,
                "quality_threshold": threshold,
                "news_bias": news_bias,
                "event_risk": event_risk,
                "contradiction_flag": contradiction_flag,
            },
            "constraints": {
                "sl_delta_pct_min": -0.02,
                "sl_delta_pct_max": 0.03,
                "tp_delta_pct_min": -0.03,
                "tp_delta_pct_max": 0.06,
            },
            "output_schema": {
                "sl_delta_pct": "number",
                "tp_delta_pct": "number",
            },
        }
        system_instruction = (
            "Return JSON only. You are a bounded risk assistant. "
            "Do not output absolute prices. Output only sl_delta_pct and tp_delta_pct."
        )
        response = await _generate_gemini_content_with_fallback(
            primary_model=gemini_agent.model_name,
            extra_models=["gemini-2.5-flash", "gemini-2.0-flash"],
            contents=[{"role": "user", "parts": [{"text": json.dumps(prompt_payload, ensure_ascii=True)}]}],
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1,
                response_mime_type="application/json",
            ),
            timeout_seconds=5.0,
        )
        parsed = _extract_json_object((response.text or "").strip())
        sl_delta = _clamp(float(parsed.get("sl_delta_pct", 0.0)), -0.02, 0.03)
        tp_delta = _clamp(float(parsed.get("tp_delta_pct", 0.0)), -0.03, 0.06)
        return sl_delta, tp_delta
    except Exception as exc:
        text = str(exc)
        if is_rate_limit_error(text):
            retry = 60.0
            match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", text, flags=re.IGNORECASE)
            if match:
                retry = max(15.0, float(match.group(1)))
            _gemini_quota_cooldown_until = time.time() + retry
        logger.warning("Gemini SL/TP adjustment unavailable: %s", exc)
        return 0.0, 0.0


async def _derive_dynamic_amount_targets(
    *,
    amount_usd: float,
    action: str,
    symbol: str | None = None,
    decision=None,
) -> tuple[float, float]:
    """
    Build default SL/TP dollar targets from:
    - user risk mode
    - trade quality score vs threshold
    - Gemini/news assessment
    - asset category/profile
    - policy minimum reward:risk
    """
    mode = (risk_engine.user_policy.mode or "balanced").lower()
    min_rr = max(1.2, float(risk_engine.user_policy.min_reward_risk or 1.8))

    base_by_mode = {
        "safe": (0.05, 0.09),
        "balanced": (0.07, 0.12),
        "aggressive": (0.09, 0.16),
    }
    sl_pct, tp_pct = base_by_mode.get(mode, (0.07, 0.12))

    if decision is not None:
        # 1) Last-hours chart behavior (explicit volatility + directional push)
        range_pct = 0.0
        drift_pct = 0.0
        drift_aligned = True
        try:
            h1_bars = (
                decision.signal_decision.market_context.bars_by_timeframe.get("H1", [])
                if decision.signal_decision.market_context and decision.signal_decision.market_context.bars_by_timeframe
                else []
            )
            if len(h1_bars) >= 6:
                recent = h1_bars[-6:]
                highs = [float(b.get("high", 0.0)) for b in recent]
                lows = [float(b.get("low", 0.0)) for b in recent]
                closes = [float(b.get("close", 0.0)) for b in recent]
                avg_close = sum(closes) / max(len(closes), 1)
                if avg_close > 0:
                    range_pct = ((max(highs) - min(lows)) / avg_close) * 100.0
                    drift = closes[-1] - closes[0]
                    drift_pct = (abs(drift) / avg_close) * 100.0
                    drift_aligned = (action == "BUY" and drift > 0) or (action == "SELL" and drift < 0)

                    if range_pct >= 1.2:
                        sl_pct += 0.02
                        tp_pct += 0.03
                    elif range_pct >= 0.8:
                        sl_pct += 0.01
                        tp_pct += 0.015
                    elif range_pct <= 0.35:
                        sl_pct -= 0.01
                        tp_pct -= 0.015

                    if drift_pct >= 0.25 and drift_aligned:
                        tp_pct += 0.01
                    elif drift_pct >= 0.25 and not drift_aligned:
                        sl_pct += 0.005
                        tp_pct -= 0.01
        except Exception:
            pass

        # 2) Trade quality + Gemini/news + profile context
        quality = float(getattr(decision.trade_quality_assessment, "final_trade_quality_score", 0.0) or 0.0)
        threshold = float(getattr(decision.trade_quality_assessment, "threshold", 0.75) or 0.75)
        quality_edge = quality - threshold

        if quality_edge >= 0.10:
            sl_pct -= 0.01
            tp_pct += 0.03
        elif quality_edge >= 0.04:
            sl_pct -= 0.005
            tp_pct += 0.015
        elif quality_edge <= -0.05:
            sl_pct += 0.015
            tp_pct -= 0.02

        gemini = decision.signal_decision.gemini_confirmation
        if gemini is None or getattr(gemini, "degraded", False):
            sl_pct += 0.01
            tp_pct -= 0.01
        else:
            event_risk = str(getattr(gemini, "event_risk", "low")).lower()
            contradiction = bool(getattr(gemini, "contradiction_flag", False))
            news_bias = str(getattr(gemini, "news_bias", "neutral")).lower()
            aligned_bias = (
                (action == "BUY" and news_bias == "bullish")
                or (action == "SELL" and news_bias == "bearish")
            )
            if event_risk == "high" or contradiction:
                sl_pct += 0.015
                tp_pct -= 0.02
            elif aligned_bias and event_risk == "low":
                tp_pct += 0.01

        category = ""
        if decision.signal_decision.market_context.profile is not None:
            category = str(decision.signal_decision.market_context.profile.category or "")
        elif decision.signal_decision.market_context.symbol_info is not None:
            category = str(decision.signal_decision.market_context.symbol_info.category or "")
        category = category.title()
        if category == "Commodities":
            sl_pct += 0.01
            tp_pct += 0.015
        elif category == "Stocks":
            sl_pct += 0.005
            tp_pct += 0.01
        elif category == "Indices":
            sl_pct += 0.005
            tp_pct += 0.008

        # 3) Gemini bounded adjustment with explicit last-two factors input.
        gemini = decision.signal_decision.gemini_confirmation
        sl_delta, tp_delta = await _gemini_target_pct_adjustment(
            mode=mode,
            action=action,
            range_pct=range_pct,
            drift_pct=drift_pct,
            drift_aligned=drift_aligned,
            category=category or "Other",
            quality=quality,
            threshold=threshold,
            news_bias=str(getattr(gemini, "news_bias", "neutral")).lower() if gemini else "neutral",
            event_risk=str(getattr(gemini, "event_risk", "low")).lower() if gemini else "low",
            contradiction_flag=bool(getattr(gemini, "contradiction_flag", False)) if gemini else False,
        )
        sl_pct += sl_delta
        tp_pct += tp_delta
    else:
        # Fallback: still vary by symbol behavior even when no full decision context exists.
        sym = str(symbol or "").strip().upper()
        try:
            h1_bars = market_data.get_bars(sym, "H1", 48) if sym else []
            if len(h1_bars) >= 12:
                closes = [float(b.get("close", 0.0) or 0.0) for b in h1_bars[-12:]]
                highs = [float(b.get("high", 0.0) or 0.0) for b in h1_bars[-12:]]
                lows = [float(b.get("low", 0.0) or 0.0) for b in h1_bars[-12:]]
                avg_close = sum(closes) / max(len(closes), 1)
                if avg_close > 0:
                    range_pct = ((max(highs) - min(lows)) / avg_close) * 100.0
                    if range_pct >= 1.2:
                        sl_pct += 0.015
                        tp_pct += 0.025
                    elif range_pct >= 0.8:
                        sl_pct += 0.008
                        tp_pct += 0.015
                    elif range_pct <= 0.35:
                        sl_pct -= 0.008
                        tp_pct -= 0.01
        except Exception:
            pass
        try:
            expected_cat = universe_service.expected_category_for_symbol(universe_service.canonical_symbol(sym)) if sym else "Other"
            if expected_cat == "Commodities":
                sl_pct += 0.01
                tp_pct += 0.015
            elif expected_cat == "Indices":
                sl_pct += 0.006
                tp_pct += 0.010
            elif expected_cat == "Stocks":
                sl_pct += 0.004
                tp_pct += 0.008
        except Exception:
            pass

    sl_pct = _clamp(sl_pct, 0.03, 0.15)
    tp_pct = _clamp(tp_pct, 0.06, 0.35)

    # Enforce policy reward:risk requirement for default targets.
    tp_pct = max(tp_pct, sl_pct * min_rr)
    tp_pct = _clamp(tp_pct, 0.06, 0.40)

    sl_amount = amount_usd * sl_pct
    policy_risk_budget = _policy_risk_budget_usd(decision)
    if policy_risk_budget is not None and policy_risk_budget > 0:
        sl_amount = min(sl_amount, policy_risk_budget)
    tp_amount = max(sl_amount * min_rr, amount_usd * tp_pct)
    return sl_amount, tp_amount


class QuickBuyRequest(BaseModel):
    symbol: str
    amount_usd: float
    action: str = "BUY"  # BUY or SELL
    custom_stop_loss: Optional[float] = None
    custom_take_profit: Optional[float] = None
    signal_id: Optional[int] = None
    signal_confidence: Optional[float] = None
    quality_score: Optional[float] = None
    signal_reason: Optional[str] = None
    signal_agent_name: Optional[str] = None
    idempotency_key: str = ""


@router.post("/trade/quick-buy")
async def quick_buy(req: QuickBuyRequest, authorization: Optional[str] = Header(default=None)):
    """Buy or Sell any symbol with a dollar amount. Auto-calculates volume, SL, and TP."""
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    _raise_if_kill_switch_active()
    await _require_order_execution_permission(authorization, execution_mode=config.EXECUTION_MODE)
    _enforce_risk_profile_execution_caps()
    account_key = _current_account_key()
    execution_context = "quick_buy"
    idempotency_key = str(req.idempotency_key or "").strip()
    existing_order = await _existing_idempotent_order(
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
    )
    if existing_order:
        return _order_row_api_result(existing_order)
    # Manual quick orders should follow the user's requested amount directly.
    # Custom auto-trade budget safeguards are advisory here, not hard blockers.
    effective_amount_usd = float(req.amount_usd or 0.0)
    if effective_amount_usd <= 0:
        raise HTTPException(
            status_code=400,
            detail="Amount must be greater than zero.",
        )
    if active_platform == "ibkr":
        if risk_engine.panic_stopped:
            raise HTTPException(status_code=403, detail="Trading is paused")
        account = ibkr_connector.account_info
        if account and account.trade_mode != 0 and not config.LIVE_TRADING_ENABLED:
            raise HTTPException(status_code=403, detail="Live trading is disabled")
        price = await _ibkr_reference_price(req.symbol)
        if price <= 0:
            raise HTTPException(status_code=400, detail=f"Invalid IBKR quote for {req.symbol}")
        quantity = max(1.0, round(effective_amount_usd / price, 4))
        result = await _run_ibkr(
            ibkr_connector.place_order,
            req.symbol,
            req.action.upper(),
            quantity,
            req.custom_stop_loss,
            req.custom_take_profit,
            _build_trade_comment(effective_amount_usd, None, None),
        )
        if db:
            await db.log_order(
                signal_id=None,
                symbol=req.symbol,
                action=req.action.upper(),
                volume=quantity,
                price=result.get("price"),
                stop_loss=req.custom_stop_loss,
                take_profit=req.custom_take_profit,
                ticket=result.get("ticket"),
                retcode=result.get("retcode", -1),
                retcode_desc=result.get("retcode_desc", ""),
                success=bool(result.get("success")),
                comment=result.get("comment", ""),
                idempotency_key=idempotency_key,
                account_key=account_key,
                execution_context=execution_context,
                execution_mode=config.EXECUTION_MODE,
            )
        return result
    if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(
            status_code=403,
            detail="Live trading requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
        )
    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Trading is paused")

    symbol = _resolve_market_symbol(req.symbol)
    if str(req.symbol or "").strip().upper() != str(symbol or "").strip().upper():
        logger.warning("QuickBuy symbol remap: requested=%s resolved=%s", req.symbol, symbol)
    else:
        logger.info("QuickBuy symbol: %s", symbol)
    market_data.enable_symbol(symbol)
    tick = market_data.get_tick(symbol)
    if not tick:
        raise HTTPException(status_code=404, detail=f"Cannot get price for {symbol}")
    if not _is_fresh_tick(tick):
        raise HTTPException(status_code=404, detail=f"No fresh tick data for {symbol}")

    sym_info = market_data.get_symbol_info(symbol)
    if not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")

    action = req.action.upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="Action must be BUY or SELL")

    decision_for_targets = None

    is_buy = action == "BUY"
    price = tick.ask if is_buy else tick.bid
    contract_size = sym_info.get("trade_contract_size", 100000)
    vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
    vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)
    vol_max = float(sym_info.get("volume_max", 0.0) or 0.0)

    # Calculate volume from dollar amount (margin-based with leverage)
    if price <= 0 or contract_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid price data")

    volume, estimated_margin = _solve_volume_by_margin_target_fast(
        symbol=symbol,
        action=action,
        target_margin_usd=effective_amount_usd,
        price=price,
        contract_size=contract_size,
        vol_min=vol_min,
        vol_step=vol_step,
        vol_max=vol_max,
    )
    if volume <= 0:
        min_required = estimated_margin if estimated_margin is not None else 0.0
        raise HTTPException(
            status_code=400,
            detail=f"Amount too small for this symbol. Minimum required margin is about ${min_required:.2f}.",
        )

    digits = sym_info.get("digits", 5)
    sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else None
    tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else None
    sl_amount_for_comment: float | None = None
    tp_amount_for_comment: float | None = None

    if sl is None or tp is None:
        # Dynamic defaults from mode + trade quality + Gemini/news + symbol profile.
        sl_amount, tp_amount = await _derive_dynamic_amount_targets(
            amount_usd=effective_amount_usd,
            action=action,
            symbol=symbol,
            decision=decision_for_targets,
        )
        sl_amount_for_comment = sl_amount
        tp_amount_for_comment = tp_amount
        default_sl, default_tp = _derive_amount_based_sl_tp(
            action=action,
            entry_price=price,
            volume=volume,
            symbol_info=sym_info,
            sl_amount_usd=sl_amount,
            tp_amount_usd=tp_amount,
            digits=digits,
        )
        if sl is None:
            sl = default_sl
        if tp is None:
            tp = default_tp

    policy_risk_budget = None
    if sl_amount_for_comment is None and sl is not None and sl > 0:
        sl_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=price,
            target_price=sl,
            volume=volume,
        )
    if tp_amount_for_comment is None and tp is not None and tp > 0:
        tp_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=price,
            target_price=tp,
            volume=volume,
        )

    # Auto-clamp SL/TP to broker minimum distance to avoid manual-trade rejections.
    sl, tp = _clamp_sl_tp_to_broker_min_distance(
        action=action,
        tick=tick,
        symbol_info=sym_info,
        stop_loss=sl,
        take_profit=tp,
    )
    required_rr = max(1.2, float(risk_engine.user_policy.min_reward_risk or 1.2))
    tp = _adjust_tp_for_min_rr(
        action=action,
        entry_price=price,
        stop_loss=sl,
        take_profit=tp,
        required_rr=required_rr,
        digits=digits,
    )
    # Final SL/TP refresh at send-time to avoid last-tick invalid-stops rejections.
    latest_tick = market_data.get_tick(symbol)
    if latest_tick and not _is_fresh_tick(latest_tick):
        latest_tick = None
    if latest_tick:
        sl, tp = _clamp_sl_tp_to_broker_min_distance(
            action=action,
            tick=latest_tick,
            symbol_info=sym_info,
            stop_loss=sl,
            take_profit=tp,
        )
        latest_price = latest_tick.ask if action == "BUY" else latest_tick.bid
        tp = _adjust_tp_for_min_rr(
            action=action,
            entry_price=latest_price,
            stop_loss=sl,
            take_profit=tp,
            required_rr=required_rr,
            digits=digits,
        )
    if (sl is None or float(sl or 0.0) <= 0.0) or (tp is None or float(tp or 0.0) <= 0.0):
        fallback_sl, fallback_tp = _derive_pct_based_sl_tp_fallback(
            action=action,
            entry_price=(latest_tick.ask if action == "BUY" else latest_tick.bid) if latest_tick else price,
            symbol_info=sym_info,
            min_rr=required_rr,
            digits=digits,
        )
        if float(sl or 0.0) <= 0.0:
            sl = fallback_sl
        if float(tp or 0.0) <= 0.0:
            tp = fallback_tp

    if effective_amount_usd > 0:
        desired_sl_amount = sl_amount_for_comment if sl_amount_for_comment and sl_amount_for_comment > 0 else None
        if policy_risk_budget is not None and policy_risk_budget > 0:
            desired_sl_amount = (
                min(desired_sl_amount, policy_risk_budget)
                if desired_sl_amount is not None and desired_sl_amount > 0
                else policy_risk_budget
            )
        adjusted_volume, rebalance_error = _rebalance_volume_for_started_amount_risk(
            entry_price=price,
            stop_loss=sl,
            desired_sl_amount_usd=desired_sl_amount,
            volume=volume,
            contract_size=_usd_per_price_distance(symbol_info=sym_info, volume=1.0),
            vol_min=vol_min,
            vol_step=vol_step,
            vol_max=vol_max,
        )
        if rebalance_error:
            logger.warning("Manual quick trade risk rebalance advisory for %s: %s", symbol, rebalance_error)
        if adjusted_volume > 0 and adjusted_volume < volume:
            volume = adjusted_volume

    linked_signal_id = None
    linked_signal_data = None
    if req.signal_id and db is not None:
        try:
            candidate_signal = await asyncio.wait_for(db.get_signal_by_id(int(req.signal_id)), timeout=0.5)
        except Exception:
            candidate_signal = None
        if candidate_signal and str(candidate_signal.get("action", "")).upper() == action:
            linked_signal_id = int(req.signal_id)
            linked_signal_data = candidate_signal

    def _score_or_none(value) -> float | None:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(score) or score <= 0:
            return None
        return max(0.0, min(1.0, score))

    request_confidence = _score_or_none(req.signal_confidence)
    request_quality = _score_or_none(req.quality_score)
    linked_confidence = _score_or_none((linked_signal_data or {}).get("confidence"))
    execution_confidence = max(
        value for value in [
            request_confidence,
            request_quality,
            linked_confidence,
            0.55,
        ]
        if value is not None
    )
    execution_reason = (
        str(req.signal_reason or "").strip()
        or str((linked_signal_data or {}).get("reason") or "").strip()
        or f"Manual {action.lower()} request"
    )
    execution_agent_name = (
        str(req.signal_agent_name or "").strip()
        or str((linked_signal_data or {}).get("agent_name") or "").strip()
        or "QuickTrade"
    )

    comment_price_ref = (
        (latest_tick.ask if action == "BUY" else latest_tick.bid)
        if latest_tick is not None
        else price
    )
    if sl is not None and sl > 0:
        sl_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=comment_price_ref,
            target_price=sl,
            volume=volume,
        )
    if tp is not None and tp > 0:
        tp_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=comment_price_ref,
            target_price=tp,
            volume=volume,
        )

    comment_amount_usd = _comment_amount_for_mt5_trade(
        symbol=symbol,
        action=action,
        volume=volume,
        fallback_amount_usd=effective_amount_usd,
    )
    order_req = OrderRequest(
        symbol=symbol, action=action,
        volume=volume, stop_loss=sl, take_profit=tp,
        comment=_build_trade_comment(comment_amount_usd, sl_amount_for_comment, tp_amount_for_comment),
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
        execution_mode=config.EXECUTION_MODE,
    )
    quick_context = _build_fast_execution_context(
        symbol=symbol,
        tick=tick,
        symbol_info=sym_info,
        evaluation_mode="manual_fast",
    )
    if quick_context is None:
        raise HTTPException(status_code=404, detail=f"No live context available for {symbol}")
    quick_candidate_signal = TechnicalSignal(
        agent_name=execution_agent_name,
        action=action,
        confidence=execution_confidence,
        stop_loss=sl,
        take_profit=tp,
        max_holding_minutes=None,
        reason=execution_reason,
        strategy="manual_execution",
        metadata={
            "source": "quick_trade",
            "linked_signal_id": linked_signal_id,
            "quality_score": request_quality,
            "raw_signal_confidence": request_confidence,
        },
    )
    quick_preflight = await execution_service.preflight_for_context_signal(
        context=quick_context,
        candidate_signal=quick_candidate_signal,
        volume=volume,
        reference_spread=tick.spread if tick else 0.0,
        evaluation_mode="scan",
    )
    if not quick_preflight.approved:
        return {
            "success": False,
            "retcode": -1,
            "retcode_desc": quick_preflight.reason,
            "ticket": None,
            "volume": volume,
            "price": None,
            "stop_loss": sl,
            "take_profit": tp,
            "comment": order_req.comment,
            "idempotency_key": idempotency_key,
        }
    execution_trace = _build_execution_trace_metadata(
        source="quick_trade",
        symbol=symbol,
        action=action,
        signal_id=linked_signal_id,
        reason=execution_reason,
        requested_agent_name=active_agent_name,
        amount_usd=req.amount_usd,
        preflight_reason=str(quick_preflight.reason or "").strip(),
        signal_snapshot={
            "agent_name": quick_candidate_signal.agent_name,
            "confidence": quick_candidate_signal.confidence,
            "reason": quick_candidate_signal.reason,
            "stop_loss": sl,
            "take_profit": tp,
        },
    )
    place_order_if_approved = getattr(execution_service, "place_order_if_approved", None)
    if callable(place_order_if_approved):
        result = place_order_if_approved(order_req, quick_preflight)
    else:
        result = execution.place_order(order_req)

    if db:
        sid = linked_signal_id
        if sid is None:
            try:
                sid = await db.log_signal(
                    agent_name=execution_agent_name,
                    symbol=symbol,
                    timeframe="manual",
                    action=action,
                    confidence=execution_confidence,
                    stop_loss=sl,
                    take_profit=tp,
                    max_holding_minutes=None,
                    reason=execution_reason,
                )
            except Exception as exc:
                sid = None
                logger.warning("Quick-buy signal persistence failed for %s: %s", symbol, exc)
        try:
            await db.log_order(
                signal_id=sid,
                symbol=symbol,
                action=action,
                volume=volume,
                price=result.price,
                stop_loss=sl,
                take_profit=tp,
                ticket=result.ticket,
                retcode=result.retcode,
                retcode_desc=result.retcode_desc,
                success=result.success,
                comment=order_req.comment,
                idempotency_key=idempotency_key,
                account_key=account_key,
                execution_context=execution_context,
                execution_mode=order_req.execution_mode,
            )
        except Exception as exc:
            logger.warning("Quick-buy order persistence failed for %s: %s", symbol, exc)
        execution_trace["signal_id"] = sid

        async def _persist_quick_buy_async():
            if result.success and result.ticket:
                auto_trader.position_manager.enable_auto_portfolio_targets()
                try:
                    await db.log_ai_activity(
                        action="quick_execute",
                        symbol=symbol,
                        ticket=result.ticket,
                        detail=f"{action} opened via quick trade.",
                        signal_id=sid,
                        decision_reason=execution_trace.get("reason", ""),
                        gemini_summary=json.dumps(execution_trace.get("gemini") or {}),
                        meta_model_summary=json.dumps(execution_trace.get("meta_model") or {}),
                    )
                except Exception as exc:
                    logger.warning("Quick-buy AI activity persistence failed for %s #%s: %s", symbol, result.ticket, exc)

                async def _store_plan_async(ticket: int, linked_sid: int | None, sym: str, side: str):
                    try:
                        decision = await signal_pipeline.evaluate(
                            symbol=sym,
                            requested_agent_name=active_agent_name,
                            requested_timeframe="H1",
                            evaluation_mode="manual",
                            bar_count=100,
                        )
                        await _store_management_plan(
                            ticket,
                            linked_sid,
                            sym,
                            side,
                            decision,
                            execution_trace=execution_trace,
                        )
                    except Exception as exc:
                        logger.warning("Background plan generation failed for %s #%s: %s", sym, ticket, exc)
                        try:
                            await _store_execution_trace_fallback_plan(
                                ticket,
                                linked_sid,
                                sym,
                                side,
                                execution_trace=execution_trace,
                            )
                        except Exception as fallback_exc:
                            logger.warning("Fallback execution-trace plan persistence failed for %s #%s: %s", sym, ticket, fallback_exc)

                asyncio.create_task(_store_plan_async(result.ticket, sid, symbol, action))
                if sid:
                    try:
                        await db.mark_evaluation_outcome(sid, "opened")
                        account = connector.refresh_account() if connector.connected else None
                        await db.mark_trade_candidate_execution(
                            signal_id=sid,
                            executed=True,
                            ticket=result.ticket,
                            fill_price=result.price,
                            slippage_estimate=0.0,
                            margin_snapshot={
                                "balance": float(account.balance) if account else 0.0,
                                "equity": float(account.equity) if account else 0.0,
                                "margin": float(account.margin) if account else 0.0,
                                "free_margin": float(account.free_margin) if account else 0.0,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Quick-buy post-open persistence failed for %s #%s: %s", symbol, result.ticket, exc)

        asyncio.create_task(_persist_quick_buy_async())

    return result.model_dump()


class ChatMessageItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatTradeRequest(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    amount_usd: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessageItem] = []
    execute_trade: bool = False


class ChatHistoryRequest(BaseModel):
    messages: list[ChatMessageItem] = []


@router.get("/chat/history")
async def get_chat_history():
    return {"messages": await _load_chat_history()}


@router.post("/chat/history")
async def save_chat_history(req: ChatHistoryRequest):
    await _save_chat_history([item.model_dump() for item in req.messages])
    return {"saved": True}


@router.delete("/chat/history")
async def clear_chat_history():
    await _save_chat_history([])
    return {"cleared": True}


@router.post("/chat/message")
async def chat_message(req: ChatRequest, authorization: Optional[str] = Header(default=None)):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    if _is_gemini_availability_query(message):
        gemini_status = _current_gemini_status()
        reply = gemini_status["reply"]

        persisted_history = [item.model_dump() for item in req.history]
        persisted_history.append({"role": "user", "content": message})
        persisted_history.append({"role": "assistant", "content": reply})
        await _save_chat_history(persisted_history)

        return {
            "reply": reply,
            "intent": "chat",
            "trade_request": None,
            "trade_preview": None,
            "order_result": None,
            "executed": False,
            "gemini_status": gemini_status,
        }

    system_prompt = """
You are an AI trading copilot inside a broker-connected trading app.
Return STRICT JSON only:
{
  "reply": "string",
  "intent": "chat" | "trade_request",
  "trade": {
    "symbol": "string",
    "action": "BUY" | "SELL",
    "amount_usd": number,
    "stop_loss": number|null,
    "take_profit": number|null,
    "reason": "string"
  } | null
}
If details are missing, set intent=chat and ask a short follow-up.
Never promise guaranteed profits.
"""

    payload = {
        "history": [item.model_dump() for item in req.history[-10:]],
        "message": message,
        "policy_mode": risk_engine.user_policy.mode,
        "auto_trade_min_confidence": risk_engine.settings.auto_trade_min_confidence,
        "connected": connector.connected,
    }

    parsed: dict
    if gemini_agent.available and getattr(gemini_agent, "_client", None) is not None:
        try:
            from google.genai import types as genai_types

            response = await _generate_gemini_content_with_fallback(
                primary_model=gemini_agent.model_name,
            extra_models=["gemini-2.5-flash", "gemini-2.0-flash"],
                contents=[{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=True)}]}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
                timeout_seconds=5.0,
            )
            parsed = _extract_json_object((response.text or "").strip())
        except Exception as exc:
            logger.warning("Gemini chat failed, using fallback parser: %s", exc)
            parsed = _simple_trade_intent_fallback(message)
    else:
        parsed = _simple_trade_intent_fallback(message)

    intent = str(parsed.get("intent", "chat")).lower()
    reply = str(parsed.get("reply", "Done."))
    trade_payload = parsed.get("trade")

    trade_preview = None
    order_result = None
    normalized_trade_request = None

    if intent == "trade_request" and trade_payload:
        trade_req = ChatTradeRequest(**trade_payload)
        symbol = _resolve_market_symbol(trade_req.symbol)
        normalized_trade_request = {**trade_req.model_dump(), "symbol": symbol}

        if not _active_platform_connected():
            reply = "Trade request detected, but no active trading platform is connected. Connect first, then confirm execution."
        elif active_platform == "ibkr":
            side_price = await _ibkr_reference_price(symbol)
            if side_price <= 0:
                reply = f"I detected a trade request, but {symbol} has no valid IBKR quote right now."
            else:
                estimated_volume = max(1, int(trade_req.amount_usd / side_price))
                estimated_margin = float(estimated_volume * side_price)
                digits = 5 if side_price < 10 else 2
                sl_amount, tp_amount = await _derive_dynamic_amount_targets(
                    amount_usd=trade_req.amount_usd,
                    action=trade_req.action,
                    symbol=symbol,
                    decision=None,
                )
                sl, tp = _derive_amount_based_sl_tp(
                    action=trade_req.action,
                    entry_price=side_price,
                    volume=float(estimated_volume),
                    symbol_info={"trade_tick_value": 1.0, "trade_tick_size": 1.0, "point": 1.0},
                    sl_amount_usd=sl_amount,
                    tp_amount_usd=tp_amount,
                    digits=digits,
                )
                trade_preview = {
                    "symbol": symbol,
                    "action": trade_req.action,
                    "amount_usd": trade_req.amount_usd,
                    "estimated_entry": side_price,
                    "estimated_volume": float(estimated_volume),
                    "estimated_margin": round(estimated_margin, 2),
                    "stop_loss": sl,
                    "take_profit": tp,
                    "reason": trade_req.reason,
                }
                if req.execute_trade:
                    order_result = await quick_buy(
                        QuickBuyRequest(
                            symbol=symbol,
                            amount_usd=trade_req.amount_usd,
                            action=trade_req.action,
                            custom_stop_loss=sl,
                            custom_take_profit=tp,
                        ),
                        authorization=authorization,
                    )
                    if order_result.get("success"):
                        reply = f"Trade executed: {trade_req.action} {symbol} with ${trade_req.amount_usd:.2f}."
        else:
            tick = market_data.get_tick(symbol)
            if tick and not _is_fresh_tick(tick):
                tick = None
            sym_info = market_data.get_symbol_info(symbol) if tick else None
            if tick and sym_info:
                side_price = tick.ask if trade_req.action == "BUY" else tick.bid
                contract_size = sym_info.get("trade_contract_size", 100000)
                vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)
                vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
                vol_max = float(sym_info.get("volume_max", 0.0) or 0.0)
                volume, estimated_margin = _solve_volume_by_margin_target(
                    symbol=symbol,
                    action=trade_req.action,
                    target_margin_usd=trade_req.amount_usd,
                    price=side_price,
                    vol_min=vol_min,
                    vol_step=vol_step,
                    vol_max=vol_max,
                )
                if volume <= 0:
                    min_required = estimated_margin if estimated_margin is not None else 0.0
                    reply = (
                        f"I detected a trade request, but ${trade_req.amount_usd:.2f} is too small for {symbol}. "
                        f"Minimum required margin is about ${min_required:.2f}."
                    )
                    trade_preview = {
                        "symbol": symbol,
                        "action": trade_req.action,
                        "amount_usd": trade_req.amount_usd,
                        "estimated_entry": side_price,
                        "estimated_volume": 0.0,
                        "reason": "amount_too_small",
                    }
                    volume = 0.0

                decision_for_targets = None
                try:
                    decision_for_targets = await signal_pipeline.evaluate(
                        symbol=symbol,
                        requested_agent_name=active_agent_name,
                        requested_timeframe="H1",
                        evaluation_mode="manual",
                        bar_count=100,
                    )
                except Exception as exc:
                    logger.warning("Could not build dynamic SL/TP context for chat trade %s: %s", symbol, exc)

                sl_amount, tp_amount = await _derive_dynamic_amount_targets(
                    amount_usd=trade_req.amount_usd,
                    action=trade_req.action,
                    symbol=symbol,
                    decision=decision_for_targets,
                )
                # Safety: chat trades always use amount-based defaults for SL/TP,
                # derived from risk/news/quality context (not raw model prices).
                sl, tp = _derive_amount_based_sl_tp(
                    action=trade_req.action,
                    entry_price=side_price,
                    volume=volume,
                    symbol_info=sym_info,
                    sl_amount_usd=sl_amount,
                    tp_amount_usd=tp_amount,
                    digits=sym_info.get("digits", 5),
                )

                trade_preview = {
                    "symbol": symbol,
                    "action": trade_req.action,
                    "amount_usd": trade_req.amount_usd,
                    "estimated_entry": side_price,
                    "estimated_volume": round(volume, 4),
                    "estimated_margin": round(float(estimated_margin), 2) if estimated_margin is not None else None,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "reason": trade_req.reason,
                }

                if req.execute_trade and volume > 0:
                    order_result = await quick_buy(
                        QuickBuyRequest(
                            symbol=symbol,
                            amount_usd=trade_req.amount_usd,
                            action=trade_req.action,
                            custom_stop_loss=sl,
                            custom_take_profit=tp,
                        ),
                        authorization=authorization,
                    )
                    if order_result.get("success"):
                        reply = f"Trade executed: {trade_req.action} {symbol} with ${trade_req.amount_usd:.2f} margin."
            else:
                reply = f"I detected a trade request, but {symbol} has no fresh MT5 quote right now."

    # Persist chat state so it survives reload/restart.
    persisted_history = [item.model_dump() for item in req.history]
    persisted_history.append({"role": "user", "content": message})
    persisted_history.append({"role": "assistant", "content": reply})
    await _save_chat_history(persisted_history)

    return {
        "reply": reply,
        "intent": "trade_request" if intent == "trade_request" else "chat",
        "trade_request": normalized_trade_request,
        "trade_preview": trade_preview,
        "order_result": order_result,
        "executed": bool(order_result and order_result.get("success")),
    }


class ExecuteTradeRequest(BaseModel):
    symbol: str
    action: str
    volume: float
    stop_loss: float
    take_profit: float
    signal_id: Optional[int] = None
    idempotency_key: str = ""


@router.post("/trade/execute")
async def execute_trade(req: ExecuteTradeRequest, authorization: Optional[str] = Header(default=None)):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    _raise_if_kill_switch_active()
    await _require_order_execution_permission(authorization, execution_mode=config.EXECUTION_MODE)
    _enforce_risk_profile_execution_caps()
    account_key = _current_account_key()
    execution_context = "manual"
    idempotency_key = str(req.idempotency_key or "").strip()
    existing_order = await _existing_idempotent_order(
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
    )
    if existing_order:
        return _order_row_api_result(existing_order)
    if active_platform == "ibkr":
        if risk_engine.panic_stopped:
            raise HTTPException(status_code=403, detail="Panic stop is active")
        account = ibkr_connector.account_info
        if account and account.trade_mode != 0 and not config.LIVE_TRADING_ENABLED:
            raise HTTPException(status_code=403, detail="Live trading is disabled")

        result = await _run_ibkr(
            ibkr_connector.place_order,
            req.symbol,
            req.action.upper(),
            req.volume,
            req.stop_loss,
            req.take_profit,
            "",
        )
        if db:
            await db.log_order(
                signal_id=req.signal_id,
                symbol=req.symbol,
                action=req.action,
                volume=req.volume,
                price=result.get("price"),
                stop_loss=req.stop_loss,
                take_profit=req.take_profit,
                ticket=result.get("ticket"),
                retcode=result.get("retcode", -1),
                retcode_desc=result.get("retcode_desc", ""),
                success=bool(result.get("success")),
                comment=result.get("comment", ""),
                idempotency_key=idempotency_key,
                account_key=account_key,
                execution_context=execution_context,
                execution_mode=config.EXECUTION_MODE,
            )
        return result

    if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(
            status_code=403,
            detail="Live trading requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
        )

    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Panic stop is active")

    if req.stop_loss == 0:
        raise HTTPException(status_code=400, detail="Stop loss is required")

    resolved_symbol = _resolve_market_symbol(req.symbol)
    execution_tick = market_data.get_tick(resolved_symbol)
    if execution_tick is None:
        raise HTTPException(status_code=404, detail=f"Cannot get live tick data for {resolved_symbol}")
    if not _is_fresh_tick(execution_tick):
        raise HTTPException(status_code=404, detail=f"No fresh tick data for {resolved_symbol}")
    requested_agent_name = active_agent_name
    if req.signal_id and db:
        signal = await db.get_signal_by_id(req.signal_id)
        if signal:
            requested_agent_name = _requested_agent_from_final_name(signal.get("agent_name", active_agent_name))

    execution_guard = await _pre_execution_checks(
        symbol=resolved_symbol,
        action=req.action,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        reference_spread=float(getattr(execution_tick, "spread", 0.0) or 0.0),
        requested_agent_name=requested_agent_name,
    )

    order_req = OrderRequest(
        symbol=resolved_symbol,
        action=req.action,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
        execution_mode=config.EXECUTION_MODE,
    )

    result = execution_service.place_order_if_approved(order_req, execution_guard["preflight"])

    if db:
        await db.log_order(
            signal_id=req.signal_id,
            symbol=resolved_symbol,
            action=req.action,
            volume=req.volume,
            price=result.price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            ticket=result.ticket,
            retcode=result.retcode,
            retcode_desc=result.retcode_desc,
            success=result.success,
            comment=order_req.comment,
            idempotency_key=idempotency_key,
            account_key=account_key,
            execution_context=execution_context,
            execution_mode=order_req.execution_mode,
        )
        if result.success and result.ticket:
            auto_trader.position_manager.enable_auto_portfolio_targets()
            await db.log_position_change(
                result.ticket, "opened", resolved_symbol,
                f"{req.action} {req.volume} lots at {result.price}"
            )
            signal = await db.get_signal_by_id(req.signal_id) if req.signal_id else None
            decision = await signal_pipeline.evaluate(
                symbol=resolved_symbol,
                requested_agent_name=requested_agent_name,
                requested_timeframe=signal.get("timeframe", "H1") if signal else "H1",
                evaluation_mode="manual",
                bar_count=100,
            )
            await _store_management_plan(result.ticket, req.signal_id, resolved_symbol, req.action, decision)
            if req.signal_id:
                await db.mark_evaluation_outcome(req.signal_id, "opened")
                account = connector.refresh_account() if connector.connected else None
                await db.mark_trade_candidate_execution(
                    signal_id=req.signal_id,
                    executed=True,
                    ticket=result.ticket,
                    fill_price=result.price,
                    slippage_estimate=0.0,
                    margin_snapshot={
                        "balance": float(account.balance) if account else 0.0,
                        "equity": float(account.equity) if account else 0.0,
                        "margin": float(account.margin) if account else 0.0,
                        "free_margin": float(account.free_margin) if account else 0.0,
                    },
                )

    return result.model_dump()


@router.get("/positions")
async def get_positions(symbol: Optional[str] = None, include_margin: bool = False):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    def _extract_comment_amount_local(comment: str | None, key: str) -> float | None:
        m = re.search(rf"(?:^|\|){re.escape(key)}(\d+(?:\.\d+)?)", comment or "", re.IGNORECASE)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except Exception:
            return None
        return value if value > 0 else None

    symbol_key = str(symbol or "").strip().upper()
    cache_key = (str(active_platform or "mt5"), symbol_key, bool(include_margin))
    cached_row = _positions_payload_cache.get(cache_key)
    cached_payload = list(cached_row[1]) if cached_row else None
    cached_age = (time.time() - float(cached_row[0])) if cached_row else None

    try:
        if active_platform == "ibkr":
            positions = await asyncio.wait_for(
                _run_ibkr(ibkr_connector.get_positions, symbol),
                timeout=_POSITIONS_FETCH_TIMEOUT_SECONDS,
            )
        else:
            positions = await asyncio.wait_for(
                asyncio.to_thread(execution.get_positions, symbol),
                timeout=_POSITIONS_FETCH_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        logger.warning("Positions refresh failed (platform=%s symbol=%s): %s", active_platform, symbol_key or "*", exc)
        if cached_payload is not None and cached_age is not None and cached_age <= _POSITIONS_CACHE_TTL_SECONDS:
            return cached_payload
        raise HTTPException(status_code=504, detail="Timed out while reading broker positions")

    payload: list[dict] = []
    for position in positions:
        if hasattr(position, "model_dump"):
            item = position.model_dump()
        elif isinstance(position, dict):
            item = dict(position)
        else:
            item = {}
        symbol_name = str(item.get("symbol") or getattr(position, "symbol", "") or "")
        position_type = str(item.get("type") or getattr(position, "type", "") or "")
        position_volume = float(item.get("volume") or getattr(position, "volume", 0.0) or 0.0)
        position_price_open = float(item.get("price_open") or getattr(position, "price_open", 0.0) or 0.0)
        position_take_profit = float(item.get("take_profit") or getattr(position, "take_profit", 0.0) or 0.0)
        position_stop_loss = float(item.get("stop_loss") or getattr(position, "stop_loss", 0.0) or 0.0)
        position_ticket = int(item.get("ticket") or getattr(position, "ticket", 0) or 0)
        position_open_time = float(item.get("time") or getattr(position, "time", 0.0) or 0.0)
        position_profit = float(item.get("profit") or getattr(position, "profit", 0.0) or 0.0)
        item["expected_profit_time_minutes"] = None
        item["expected_profit_time_remaining_minutes"] = None
        item["expected_profit_eta_timestamp"] = None

        if not include_margin:
            item.setdefault(
                "target_management_mode",
                "automatic" if position_stop_loss > 0 and position_take_profit > 0 else "pending_auto",
            )
            payload.append(item)
            continue

        estimated_margin = None
        if active_platform == "ibkr":
            # Approximate IBKR notional for dashboard budget/margin display parity.
            px = float(item.get("price_current") or position_price_open or 0.0)
            estimated_margin = (px * abs(position_volume)) if px > 0 and abs(position_volume) > 0 else None
        else:
            try:
                estimated_margin = execution.estimate_margin(
                    symbol=symbol_name,
                    action=position_type,
                    volume=position_volume,
                )
            except Exception:
                estimated_margin = None
        item["estimated_margin"] = (
            round(float(estimated_margin), 2)
            if estimated_margin is not None and float(estimated_margin) > 0
            else None
        )
        try:
            if active_platform == "ibkr":
                symbol_info = {
                    "name": symbol_name,
                    "trade_contract_size": 1.0,
                    "point": 0.01,
                    "trade_tick_size": 0.01,
                    "trade_tick_value": 0.01,
                }
            else:
                symbol_info = market_data.get_symbol_info(symbol_name)
        except Exception:
            symbol_info = None

        # Keep "Started Amount" aligned with user expectation:
        # notional at entry = volume * entry_price * contract_size.
        contract_size_for_entry = 1.0
        if isinstance(symbol_info, dict):
            try:
                contract_size_for_entry = float(symbol_info.get("trade_contract_size") or 1.0)
            except (TypeError, ValueError):
                contract_size_for_entry = 1.0
        entry_market_value_usd = (
            position_price_open * abs(position_volume) * max(contract_size_for_entry, 1e-9)
            if position_price_open > 0 and abs(position_volume) > 0
            else None
        )
        item["entry_market_value_usd"] = (
            round(float(entry_market_value_usd), 2)
            if entry_market_value_usd is not None and float(entry_market_value_usd) > 0
            else None
        )
        comment_text = str(item.get("comment") or getattr(position, "comment", "") or "")
        comment_ta = _extract_comment_amount_local(comment_text, "TA")
        comment_sla = _extract_comment_amount_local(comment_text, "SLA")
        comment_tpa = _extract_comment_amount_local(comment_text, "TPA")
        if comment_ta is not None:
            item["started_with_usd"] = round(float(comment_ta), 2)
        if comment_sla is not None:
            item["sl_amount_usd"] = round(float(comment_sla), 2)
        if comment_tpa is not None:
            item["tp_amount_usd"] = round(float(comment_tpa), 2)

        entry_value_for_margin = _safe_positive_float(item.get("entry_market_value_usd"))
        broker_estimated_margin = _safe_positive_float(item.get("estimated_margin"))
        # Stable per-position "started amount":
        # - stock-style CFDs often consume the full filled value (INTC: 82 * 114.80 = 9413.60)
        # - leveraged CFDs have a huge notional, so use broker margin when it is clearly lower
        margin_used_usd = entry_value_for_margin
        margin_used_source = "entry_market_value" if entry_value_for_margin is not None else None
        if broker_estimated_margin is not None and (
            margin_used_usd is None
            or broker_estimated_margin < (float(margin_used_usd) * 0.5)
        ):
            margin_used_usd = broker_estimated_margin
            margin_used_source = "broker_estimate"
        item["margin_used_usd"] = (
            round(float(margin_used_usd), 2)
            if margin_used_usd is not None and float(margin_used_usd) > 0
            else None
        )
        item["margin_used_source"] = margin_used_source

        started_with_usd = float(item.get("started_with_usd") or 0.0)
        if started_with_usd > 0 and item.get("margin_used_usd") is not None:
            margin_delta = started_with_usd - float(item["margin_used_usd"])
            item["budget_vs_margin_delta_usd"] = round(margin_delta, 2)
            item["budget_vs_margin_delta_pct"] = round((margin_delta / started_with_usd) * 100.0, 3)
        else:
            item["budget_vs_margin_delta_usd"] = None
            item["budget_vs_margin_delta_pct"] = None

        if isinstance(symbol_info, dict) and position_price_open > 0 and position_volume > 0:
            stop_loss = position_stop_loss
            take_profit = position_take_profit
            if stop_loss > 0:
                sl_amount_usd = _price_distance_to_usd(
                    symbol_info=symbol_info,
                    entry_price=position_price_open,
                    target_price=stop_loss,
                    volume=position_volume,
                )
                item["sl_amount_usd"] = round(sl_amount_usd, 2) if sl_amount_usd is not None else None
            elif stop_loss <= 0 and item.get("sl_amount_usd") is None:
                item["sl_amount_usd"] = None
            if take_profit > 0:
                tp_amount_usd = _price_distance_to_usd(
                    symbol_info=symbol_info,
                    entry_price=position_price_open,
                    target_price=take_profit,
                    volume=position_volume,
                )
                item["tp_amount_usd"] = round(tp_amount_usd, 2) if tp_amount_usd is not None else None
            elif take_profit <= 0 and item.get("tp_amount_usd") is None:
                item["tp_amount_usd"] = None
        else:
            if item.get("sl_amount_usd") is None:
                item["sl_amount_usd"] = None
            if item.get("tp_amount_usd") is None:
                item["tp_amount_usd"] = None

        # Fallback ETA when no explicit management plan exists yet.
        expected_profit_minutes_fallback = None
        opened_at = position_open_time
        if opened_at > 1_000_000_000_000:
            opened_at = opened_at / 1000.0
        elapsed_minutes = max(0.0, (time.time() - opened_at) / 60.0) if opened_at > 0 else 0.0

        tp_amount_usd = item.get("tp_amount_usd")
        current_profit_usd = position_profit
        if tp_amount_usd is not None and float(tp_amount_usd) > 0:
            target_profit_usd = float(tp_amount_usd)
            remaining_profit_usd = max(0.0, target_profit_usd - max(0.0, current_profit_usd))
            pace_usd_per_min = (current_profit_usd / elapsed_minutes) if elapsed_minutes > 2.0 and current_profit_usd > 0 else 0.0
            if pace_usd_per_min > 0:
                projected_remaining = int(math.ceil(remaining_profit_usd / max(pace_usd_per_min, 1e-6)))
                expected_profit_minutes_fallback = max(5, min(480, int(math.ceil(elapsed_minutes)) + projected_remaining))

        if expected_profit_minutes_fallback is None:
            # Conservative category defaults when pace-based estimate is unavailable.
            category = universe_service.expected_category_for_symbol(universe_service.canonical_symbol(symbol_name)) or "Other"
            category_defaults = {
                "Commodities": 75,
                "Indices": 90,
                "Stocks": 120,
                "Forex": 90,
                "Crypto": 120,
                "Other": 120,
            }
            fallback_minutes = int(category_defaults.get(category, 120))
            if float(position_take_profit or 0.0) > 0:
                expected_profit_minutes_fallback = fallback_minutes

        if expected_profit_minutes_fallback is not None and expected_profit_minutes_fallback > 0 and opened_at > 0:
            item["expected_profit_time_minutes"] = expected_profit_minutes_fallback
            eta_timestamp = int(opened_at + (expected_profit_minutes_fallback * 60))
            remaining_minutes = max(0, int(math.ceil(expected_profit_minutes_fallback - elapsed_minutes)))
            item["expected_profit_eta_timestamp"] = eta_timestamp
            item["expected_profit_time_remaining_minutes"] = remaining_minutes

        if db is not None:
            try:
                order_ctx = await asyncio.wait_for(
                    db.get_order_with_context_by_ticket(position_ticket),
                    timeout=_POSITIONS_DB_LOOKUP_TIMEOUT_SECONDS,
                )
            except Exception:
                order_ctx = None
            try:
                plan = await asyncio.wait_for(
                    db.get_position_management_plan(position_ticket),
                    timeout=_POSITIONS_DB_LOOKUP_TIMEOUT_SECONDS,
                )
            except Exception:
                plan = None
            expected_profit_minutes = None
            if isinstance(plan, dict) and plan:
                plan_json = dict(plan.get("plan_json") or {})
                plan_meta = dict(plan_json.get("metadata") or {}) if isinstance(plan_json, dict) else {}
                item["target_management_mode"] = (
                    "manual" if bool(plan_meta.get("manual_target_override")) else "automatic"
                )
                if bool(plan_meta.get("manual_target_override")):
                    item["target_management_updated_at"] = plan_meta.get("manual_target_updated_at")
                raw_expected_minutes = (
                    (plan_json.get("expected_hold_minutes") if isinstance(plan_json, dict) else None)
                    or (plan_json.get("planned_hold_minutes") if isinstance(plan_json, dict) else None)
                    or (plan_json.get("max_holding_minutes") if isinstance(plan_json, dict) else None)
                    or plan_meta.get("expected_hold_minutes")
                    or plan_meta.get("planned_hold_minutes")
                    or plan_meta.get("time_stop_minutes")
                )
                try:
                    candidate_expected = int(round(float(raw_expected_minutes)))
                except (TypeError, ValueError):
                    candidate_expected = 0
                if candidate_expected > 0:
                    expected_profit_minutes = candidate_expected

            if expected_profit_minutes is not None:
                item["expected_profit_time_minutes"] = expected_profit_minutes
                opened_at = position_open_time
                if opened_at > 1_000_000_000_000:
                    opened_at = opened_at / 1000.0
                if opened_at > 0:
                    eta_timestamp = int(opened_at + (expected_profit_minutes * 60))
                    elapsed_minutes = max(0.0, (time.time() - opened_at) / 60.0)
                    remaining_minutes = max(0, int(math.ceil(expected_profit_minutes - elapsed_minutes)))
                    item["expected_profit_eta_timestamp"] = eta_timestamp
                    item["expected_profit_time_remaining_minutes"] = remaining_minutes
            execution_trace = _execution_trace_from_plan_or_order(plan, order_ctx)
            if isinstance(plan, dict) and plan and (
                not execution_trace
                or str(execution_trace.get("reason") or "").strip().lower() in {"", "request completed"}
            ):
                plan_json = dict(plan.get("plan_json") or {})
                plan_meta = dict(plan_json.get("metadata") or {}) if isinstance(plan_json, dict) else {}
                plan_signal = None
                plan_risk = None
                plan_signal_id = plan.get("signal_id")
                if plan_signal_id:
                    try:
                        plan_signal = await asyncio.wait_for(
                            db.get_signal_by_id(int(plan_signal_id)),
                            timeout=_POSITIONS_DB_LOOKUP_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        plan_signal = None
                    try:
                        plan_risk = await asyncio.wait_for(
                            db.get_risk_decision_by_signal(int(plan_signal_id)),
                            timeout=_POSITIONS_DB_LOOKUP_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        plan_risk = None
                plan_reason = (
                    str((plan_signal or {}).get("reason") or "").strip()
                    or str(plan_json.get("initial_thesis") or "").strip()
                    or str(plan_json.get("summary") or "").strip()
                    or str(plan_json.get("strategy") or "").strip()
                )
                execution_trace = {
                    "source": "auto_trade_plan",
                    "symbol": symbol_name.strip().upper(),
                    "action": position_type.strip().upper(),
                    "signal_id": plan_signal_id,
                    "reason": plan_reason,
                    "requested_agent_name": str((plan_signal or {}).get("agent_name") or active_agent_name or "").strip(),
                    "active_agent_name": str((plan_signal or {}).get("agent_name") or active_agent_name or "").strip(),
                    "policy_mode": str(getattr(risk_engine.user_policy, "mode", "") or "").strip(),
                    "amount_usd": item.get("started_with_usd") or item.get("estimated_margin"),
                    "preflight_reason": str((plan_risk or {}).get("reason") or "").strip(),
                    "strategy": {
                        "id": str(plan_meta.get("strategy_id") or "").strip(),
                        "name": str(plan_json.get("strategy") or plan_meta.get("strategy_family") or "").strip(),
                        "brief": str(plan_json.get("initial_thesis") or "").strip(),
                        "human_summary": str(plan_json.get("notes") or "").strip(),
                        "summary_lines": [
                            str(plan_json.get("initial_thesis") or "").strip(),
                            f"Quality {float(plan_meta.get('trade_quality_score', 0.0) or 0.0):.2f} vs threshold {float(plan_meta.get('trade_quality_threshold', 0.0) or 0.0):.2f}.",
                            f"News bias {plan_meta.get('news_bias', 'neutral')}; event risk {plan_meta.get('event_risk', 'low')}.",
                        ],
                    },
                    "gemini": _current_gemini_status(),
                    "meta_model": {
                        "active": bool(getattr(getattr(signal_pipeline, "meta_model_service", None), "is_active", False)),
                        "version_id": str(getattr(getattr(signal_pipeline, "meta_model_service", None), "active_version_id", "") or ""),
                    },
                    "signal_snapshot": {
                        "agent_name": str((plan_signal or {}).get("agent_name") or "").strip(),
                        "confidence": float((plan_signal or {}).get("confidence") or 0.0),
                        "reason": str((plan_signal or {}).get("reason") or plan_reason or "").strip(),
                        "stop_loss": position_stop_loss or (plan_signal or {}).get("stop_loss"),
                        "take_profit": position_take_profit or (plan_signal or {}).get("take_profit"),
                    },
                }
            if execution_trace:
                item["execution_trace"] = execution_trace
                _sanitize_live_execution_trace_target_warnings(item)
            if isinstance(order_ctx, dict) and order_ctx:
                item["signal_reason"] = order_ctx.get("signal_reason")
                item["risk_reason"] = order_ctx.get("risk_reason")
        item.setdefault(
            "target_management_mode",
            "automatic" if position_stop_loss > 0 and position_take_profit > 0 else "pending_auto",
        )
        payload.append(item)
    _positions_payload_cache[cache_key] = (time.time(), list(payload))
    return payload


class ClosePositionRequest(BaseModel):
    ticket: int


class ModifyPositionRequest(BaseModel):
    ticket: int
    stop_loss: float = 0.0
    take_profit: float = 0.0


@router.post("/positions/close")
async def close_position(req: ClosePositionRequest):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        result = await _run_ibkr(ibkr_connector.close_position, req.ticket)
        logger.info(
            "IBKR manual close result ticket=%s success=%s retcode=%s detail=%s",
            req.ticket,
            bool(result.get("success")),
            result.get("retcode"),
            result.get("retcode_desc"),
        )
        if db:
            await db.log_order(
                signal_id=None,
                symbol="",
                action="CLOSE",
                volume=result.get("volume") or 0.0,
                price=result.get("price"),
                stop_loss=None,
                take_profit=None,
                ticket=result.get("ticket"),
                retcode=result.get("retcode", -1),
                retcode_desc=result.get("retcode_desc", ""),
                success=bool(result.get("success")),
            )
        if bool(result.get("success")):
            _schedule_incremental_meta_training("ibkr_manual_close")
        return result

    existing_positions = execution.get_positions()
    existing_position = next((p for p in existing_positions if p.ticket == req.ticket), None)
    result = await asyncio.to_thread(execution.close_position, req.ticket)

    if db:
        await db.log_order(
            signal_id=None,
            symbol="",
            action="CLOSE",
            volume=result.volume or 0,
            price=result.price,
            stop_loss=None,
            take_profit=None,
            ticket=result.ticket,
            retcode=result.retcode,
            retcode_desc=result.retcode_desc,
            success=result.success,
        )
        if result.success:
            await db.log_position_change(req.ticket, "closed")
            plan = await db.get_position_management_plan(req.ticket)
            signal_id = plan.get("signal_id") if plan else None
            signal = await db.get_signal_by_id(signal_id) if signal_id else None
            outcome_context = _trade_outcome_context(plan, existing_position)
            realized_profit = execution.get_position_realized_profit(req.ticket)
            if realized_profit is None:
                realized_profit = existing_position.profit if existing_position else 0.0
            await db.log_trade_outcome(
                ticket=req.ticket,
                signal_id=signal_id,
                symbol=outcome_context["symbol"],
                action=outcome_context["action"],
                confidence=float(signal.get("confidence", 0.0)) if signal else 0.0,
                profit=float(realized_profit),
                exit_reason="manual_close",
                holding_minutes=outcome_context["holding_minutes"],
                symbol_category=outcome_context["symbol_category"],
                strategy=outcome_context["strategy"],
                planned_hold_minutes=outcome_context["planned_hold_minutes"],
                outcome_json={
                    "retcode": result.retcode,
                    "retcode_desc": result.retcode_desc,
                    "detail": "Position closed manually by user.",
                },
            )
            await db.mark_evaluation_outcome(signal_id, "closed")
            _schedule_incremental_meta_training("mt5_manual_close")

    return result.model_dump()


@router.post("/positions/cancel-close")
async def cancel_position_close(req: ClosePositionRequest):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform != "ibkr":
        raise HTTPException(status_code=400, detail="Pending close cancellation is currently an IBKR order action")
    result = await _run_ibkr(ibkr_connector.cancel_position_close, req.ticket)
    logger.info(
        "IBKR cancel close result ticket=%s success=%s detail=%s",
        req.ticket, bool(result.get("success")), result.get("retcode_desc"),
    )
    return result


@router.post("/positions/close-all")
async def close_all_positions():
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        positions = await _run_ibkr(ibkr_connector.get_positions, None)
        tickets = [int(p.get("ticket", 0) or 0) for p in (positions or []) if int(p.get("ticket", 0) or 0) > 0]
    else:
        positions = await asyncio.to_thread(execution.get_positions)
        tickets = [int(getattr(p, "ticket", 0) or 0) for p in (positions or []) if int(getattr(p, "ticket", 0) or 0) > 0]

    if not tickets:
        return {
            "success": True,
            "requested": 0,
            "closed": 0,
            "failed": 0,
            "results": [],
        }

    tasks = [
        close_position(ClosePositionRequest(ticket=ticket))
        for ticket in tickets
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    normalized_results: list[dict] = []
    closed = 0
    failed = 0
    for ticket, outcome in zip(tickets, raw_results):
        if isinstance(outcome, Exception):
            failed += 1
            normalized_results.append(
                {
                    "ticket": ticket,
                    "success": False,
                    "retcode": -1,
                    "retcode_desc": str(outcome),
                }
            )
            continue
        item = dict(outcome or {})
        item.setdefault("ticket", ticket)
        ok = bool(item.get("success"))
        if ok:
            closed += 1
        else:
            failed += 1
        normalized_results.append(item)

    return {
        "success": failed == 0,
        "requested": len(tickets),
        "closed": closed,
        "failed": failed,
        "results": normalized_results,
    }


@router.post("/positions/modify")
async def modify_position(req: ModifyPositionRequest):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if float(req.stop_loss or 0.0) <= 0.0 or float(req.take_profit or 0.0) <= 0.0:
        raise HTTPException(status_code=400, detail="Both stop-loss and take-profit are required and must be > 0.")
    if active_platform == "ibkr":
        result = await _run_ibkr(ibkr_connector.modify_position, req.ticket, req.stop_loss, req.take_profit)
        return result

    existing_positions = execution.get_positions()
    existing_position = next((p for p in existing_positions if int(getattr(p, "ticket", 0) or 0) == int(req.ticket)), None)
    if existing_position is None:
        raise HTTPException(status_code=404, detail=f"Position {req.ticket} not found.")

    action = str(getattr(existing_position, "type", "") or "").upper()
    symbol = str(getattr(existing_position, "symbol", "") or "")
    entry_price = float(getattr(existing_position, "price_open", 0.0) or 0.0)
    if entry_price <= 0:
        tick = market_data.get_tick(symbol)
        if tick and _is_fresh_tick(tick):
            entry_price = float(getattr(tick, "ask", 0.0) if action == "BUY" else getattr(tick, "bid", 0.0))
    if entry_price <= 0:
        raise HTTPException(status_code=409, detail="Cannot validate targets without a fresh live/reference entry price.")

    sl = float(req.stop_loss)
    tp = float(req.take_profit)
    if action == "BUY" and not (sl < entry_price < tp):
        raise HTTPException(status_code=400, detail="Invalid BUY targets: stop-loss must be below entry and take-profit above entry.")
    if action == "SELL" and not (tp < entry_price < sl):
        raise HTTPException(status_code=400, detail="Invalid SELL targets: take-profit must be below entry and stop-loss above entry.")

    required_rr = max(1.2, float(risk_engine.user_policy.min_reward_risk or 1.2))
    rr = _reward_risk_ratio(entry_price, sl, tp)
    digits = 5
    symbol_info = market_data.get_symbol_info(symbol)
    if isinstance(symbol_info, dict):
        try:
            digits = int(symbol_info.get("digits", 5) or 5)
        except Exception:
            digits = 5
    if rr < required_rr:
        tp = _adjust_tp_for_min_rr(
            action=action,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            required_rr=required_rr,
            digits=digits,
        )
    tick = market_data.get_tick(symbol)
    if tick and _is_fresh_tick(tick):
        sl, tp = _clamp_sl_tp_to_broker_min_distance(
            action=action,
            tick=tick,
            symbol_info=symbol_info or {},
            stop_loss=sl,
            take_profit=tp,
        )
        live_entry = float(getattr(tick, "ask", entry_price) if action == "BUY" else getattr(tick, "bid", entry_price))
        tp = _adjust_tp_for_min_rr(
            action=action,
            entry_price=live_entry if live_entry > 0 else entry_price,
            stop_loss=sl,
            take_profit=tp,
            required_rr=required_rr,
            digits=digits,
        )

    result = execution.modify_position(req.ticket, sl, tp)
    if result.success and db is not None:
        try:
            stored_plan = await db.get_position_management_plan(req.ticket)
            plan = dict((stored_plan or {}).get("plan_json") or {})
            metadata = dict(plan.get("metadata") or {})
            metadata.update(
                {
                    "manual_target_override": True,
                    "manual_target_updated_at": time.time(),
                    "manual_stop_loss": sl,
                    "manual_take_profit": tp,
                }
            )
            plan["metadata"] = metadata
            plan.setdefault("strategy", "manual_position_targets")
            await db.save_position_management_plan(
                ticket=req.ticket,
                signal_id=(stored_plan or {}).get("signal_id"),
                symbol=symbol,
                action=action,
                plan=plan,
            )
            await db.log_position_change(
                req.ticket,
                "manual_targets_modified",
                symbol,
                f"Manual SL/TP set to SL={sl:.{digits}f}, TP={tp:.{digits}f}",
            )
        except Exception as exc:
            logger.warning("Failed to persist manual target override for %s: %s", req.ticket, exc)
    return result.model_dump()


# --- Risk Settings Routes ---

@router.get("/risk/settings")
async def get_risk_settings():
    return _policy_settings_response()


@router.post("/risk/settings")
async def update_risk_settings(settings: UserPolicySettings, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_manage_risk")
    risk_engine.update_user_policy(settings)
    if not await _persist_runtime_state():
        raise HTTPException(status_code=503, detail="Risk policy changed in memory but could not be saved to local storage. Please retry.")
    return {**_policy_settings_response(), "saved": True, "saved_at": time.time()}


class PanicStopRequest(BaseModel):
    active: bool


class PortfolioTargetsRequest(BaseModel):
    stop_loss_usd: float | None = None
    take_profit_usd: float | None = None
    mode: Optional[Literal["auto", "manual"]] = None


@router.get("/risk/portfolio-targets")
async def get_portfolio_targets():
    return auto_trader.position_manager.portfolio_targets_snapshot()


@router.post("/risk/portfolio-targets")
async def set_portfolio_targets(req: PortfolioTargetsRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_manage_risk")
    target_mode = req.mode
    if target_mode is None and req.stop_loss_usd is None and req.take_profit_usd is None:
        # "Clear" switches back to auto-managed portfolio vision targets.
        target_mode = "auto"
    snapshot = auto_trader.position_manager.set_portfolio_targets(
        stop_loss_usd=req.stop_loss_usd,
        take_profit_usd=req.take_profit_usd,
        mode=target_mode,
        manual_override=req.mode is None,
    )
    asyncio.create_task(_persist_runtime_state())
    return snapshot


@router.post("/risk/panic-stop")
async def panic_stop(req: PanicStopRequest):
    risk_engine.set_panic_stop(req.active)
    if db:
        await db.log_connection_event(
            "panic_stop", details=f"Panic stop {'activated' if req.active else 'deactivated'}"
        )
    return {"panic_stop": risk_engine.panic_stopped}


# --- Logs Routes ---

@router.get("/logs")
async def get_logs(limit: int = 100, log_type: str = "all"):
    if db is None:
        return []
    safe_limit = max(1, min(int(limit or 100), 500))
    safe_type = str(log_type or "all")
    try:
        return await asyncio.wait_for(db.get_logs(safe_limit, safe_type), timeout=2.5)
    except Exception as exc:
        logger.warning("Async log fetch failed; using direct SQLite fallback: %s", exc)

    db_path = str(getattr(db, "path", "") or "")
    if not db_path:
        return []

    def _read_logs_fallback() -> list[dict]:
        table_specs = [
            ("signals", "signal", "signals"),
            ("risk_decisions", "risk_decision", "risk"),
            ("orders", "order", "orders"),
            ("connection_events", "connection", "connections"),
            ("errors", "error", "errors"),
            ("ai_activity", "ai_activity", "ai_activity"),
        ]
        results: list[dict] = []
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            for table_name, entry_type, filter_name in table_specs:
                if safe_type not in ("all", filter_name):
                    continue
                rows = conn.execute(
                    f"SELECT * FROM {table_name} ORDER BY timestamp DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
                for row in rows:
                    entry = dict(row)
                    entry["log_type"] = entry_type
                    results.append(entry)
        finally:
            conn.close()
        results.sort(key=lambda item: float(item.get("timestamp") or 0), reverse=True)
        return results[:safe_limit]

    try:
        return await asyncio.to_thread(_read_logs_fallback)
    except Exception as fallback_exc:
        logger.warning("Direct SQLite log fallback failed: %s", fallback_exc)
        return []


async def _reconcile_missing_mt5_outcomes(max_rows: int = 180) -> int:
    """Backfill trade_outcomes for MT5 trades closed while manager/API was not running.

    Trade history depends on a closed outcome row. If a trade was opened through
    LinkTrade but closed externally (terminal TP/SL/manual) during downtime, this
    reconciles it from broker realized PnL so it appears in history.
    """
    if db is None or active_platform != "mt5" or not connector.connected:
        return 0

    cursor = await db._db.execute(
        """SELECT ticket, signal_id, symbol, action, timestamp
           FROM orders
           WHERE success = 1
             AND action IN ('BUY', 'SELL')
             AND ticket IS NOT NULL
           ORDER BY timestamp DESC
           LIMIT ?""",
        (max(20, int(max_rows)),),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    recent_orders = [dict(zip(cols, row)) for row in rows]
    if not recent_orders:
        return 0

    open_tickets = {int(p.ticket) for p in execution.get_positions() if getattr(p, "ticket", None) is not None}
    missing_closed: list[dict] = []
    for row in recent_orders:
        ticket = row.get("ticket")
        if not isinstance(ticket, int):
            continue
        if ticket in open_tickets:
            continue
        existing = await db.get_trade_outcome_by_ticket(ticket)
        if existing is None:
            missing_closed.append(row)

    if not missing_closed:
        return 0

    ticket_list = [int(item["ticket"]) for item in missing_closed if isinstance(item.get("ticket"), int)]
    realized_map = execution.get_realized_profit_map(ticket_list, lookback_days=90)
    now_ts = time.time()
    backfilled = 0
    for row in missing_closed:
        ticket = int(row.get("ticket"))
        signal_id = row.get("signal_id") if isinstance(row.get("signal_id"), int) else None
        plan = await db.get_position_management_plan(ticket)
        signal = await db.get_signal_by_id(signal_id) if signal_id else None
        ctx = _trade_outcome_context(plan, existing_position=None)
        symbol = (ctx.get("symbol") or row.get("symbol") or "").upper()
        action = (ctx.get("action") or row.get("action") or "").upper()
        if action not in {"BUY", "SELL"}:
            action = str(row.get("action") or "BUY").upper()
        opened_at = float(row.get("timestamp") or now_ts)
        holding_minutes = max(0.0, (now_ts - opened_at) / 60.0)
        profit = float(realized_map.get(ticket, 0.0) or 0.0)
        await db.log_trade_outcome(
            ticket=ticket,
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            confidence=float(signal.get("confidence", 0.0)) if signal else 0.0,
            profit=profit,
            exit_reason="reconciled_external_close",
            holding_minutes=holding_minutes,
            symbol_category=str(ctx.get("symbol_category") or ""),
            strategy=str(ctx.get("strategy") or ""),
            planned_hold_minutes=ctx.get("planned_hold_minutes") or 0,
            outcome_json={
                "detail": "Backfilled from MT5 history because close happened outside active runtime loop.",
            },
        )
        await db.mark_evaluation_outcome(signal_id, "closed")
        backfilled += 1
    return backfilled


@router.get("/trade-history")
async def get_trade_history(limit: int = 50, view: str = "closed"):
    _ensure_mt5_platform_contract()
    _t0 = time.time()
    view_mode = str(view or "closed").strip().lower()
    if view_mode not in {"closed", "all"}:
        view_mode = "closed"
    logger.info(
        "trade-history:start limit=%s view=%s platform=%s connected=%s",
        limit,
        view_mode,
        active_platform,
        connector.connected,
    )
    if active_platform == "tradestation" and tradestation_connector.connected:
        def _to_float_ts(v, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        closed = tradestation_execution.get_recent_closed_trades(
            limit=min(max(int(limit) * 5, 200), 5000),
            lookback_days=3650,
        )
        if view_mode != "all":
            closed = [t for t in closed if str(t.get("status", "")).lower() == "closed"]
        closed.sort(
            key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0),
            reverse=True,
        )
        wins = [t for t in closed if _to_float_ts(t.get("profit_usd")) > 0]
        losses = [t for t in closed if _to_float_ts(t.get("profit_usd")) < 0]
        breakeven = [t for t in closed if _to_float_ts(t.get("profit_usd")) == 0]
        total_profit = sum(_to_float_ts(t.get("profit_usd")) for t in closed)
        total_started = sum(
            _to_float_ts(t.get("started_with_usd"))
            for t in closed
            if t.get("started_with_usd") is not None
        )
        summary = {
            "total_trades": len(closed),
            "closed_trades": len(closed),
            "open_trades": 0,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": len(breakeven),
            "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_profit_usd": total_profit,
            "avg_profit_per_closed_trade_usd": (total_profit / len(closed)) if closed else 0.0,
            "total_started_capital_usd": total_started,
            "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
            "best_trade_usd": max((_to_float_ts(t.get("profit_usd")) for t in closed), default=0.0),
            "worst_trade_usd": min((_to_float_ts(t.get("profit_usd")) for t in closed), default=0.0),
        }
        return {
            "summary": summary,
            "trades": closed[:limit],
            "source": "tradestation_broker_history",
            "view": view_mode,
            "account_login": getattr(tradestation_connector.account_info, "login", None)
            if tradestation_connector.account_info
            else None,
            "account_server": getattr(tradestation_connector.account_info, "server", None)
            if tradestation_connector.account_info
            else None,
        }
    elif active_platform == "ibkr" and ibkr_connector.connected:
        def _to_float_ibkr(v, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        broker_trades = await _run_ibkr(ibkr_connector.get_recent_executions, limit=max(limit, 50))
        closed = [t for t in broker_trades if str(t.get("status", "")).lower() == "closed"]
        closed.sort(
            key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0),
            reverse=True,
        )
        wins = [t for t in closed if _to_float_ibkr(t.get("profit_usd")) > 0]
        losses = [t for t in closed if _to_float_ibkr(t.get("profit_usd")) < 0]
        breakeven = [t for t in closed if _to_float_ibkr(t.get("profit_usd")) == 0]
        total_profit = sum(_to_float_ibkr(t.get("profit_usd")) for t in closed)
        total_started = sum(
            _to_float_ibkr(t.get("started_with_usd"))
            for t in closed
            if t.get("started_with_usd") is not None
        )
        summary = {
            "total_trades": len(closed),
            "closed_trades": len(closed),
            "open_trades": 0,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": len(breakeven),
            "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_profit_usd": total_profit,
            "avg_profit_per_closed_trade_usd": (total_profit / len(closed)) if closed else 0.0,
            "total_started_capital_usd": total_started,
            "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
            "best_trade_usd": max((_to_float_ibkr(t.get("profit_usd")) for t in closed), default=0.0),
            "worst_trade_usd": min((_to_float_ibkr(t.get("profit_usd")) for t in closed), default=0.0),
        }
        return {
            "summary": summary,
            "trades": closed[:limit],
            "source": "ibkr_broker_executions",
        }

    # Broker-deal parity path is used for connected MT5 history so rows are
    # scoped to the active broker account. App-managed rows are overlaid below
    # when local evidence can match the broker deal.
    if active_platform == "mt5" and connector.connected and view_mode == "all":
        # MT5 parity:
        # - query a very wide lookback window
        # - fetch a larger broker window before pagination so mixed assets
        #   (stocks + commodities) are less likely to be truncated.
        requested_limit = max(int(limit), 1)
        mt5_fetch_floor = 200 if view_mode == "closed" and requested_limit <= 20 else (1000 if view_mode == "closed" else 10000)
        mt5_fetch_limit = min(max(int(limit) * 10, mt5_fetch_floor), 100000)
        mt5_rows = execution.get_recent_closed_deal_rows(
            limit=mt5_fetch_limit,
            lookback_days=3650,
        )
        def _to_float_mt5(v, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        closed = [
            t for t in mt5_rows
            if str(t.get("status", "")).lower() == "closed"
            and (
                view_mode == "all"
                or str(t.get("exit_reason") or "").lower() == "broker_closed"
            )
        ]
        wins = [t for t in closed if _to_float_mt5(t.get("profit_usd")) > 0]
        losses = [t for t in closed if _to_float_mt5(t.get("profit_usd")) < 0]
        breakeven = [t for t in closed if _to_float_mt5(t.get("profit_usd")) == 0]
        total_profit = sum(_to_float_mt5(t.get("profit_usd")) for t in closed)
        closed.sort(
            key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0),
            reverse=True,
        )

        if db is not None and closed:
            try:
                local_outcome_limit = min(max(int(limit) * 5, 100), 500)
                try:
                    local_outcomes = await asyncio.wait_for(
                        db.get_trade_outcomes(local_outcome_limit),
                        timeout=1.0,
                    )
                except Exception:
                    local_outcomes = await asyncio.to_thread(
                        _read_trade_outcomes_direct,
                        local_outcome_limit,
                    )
                local_ticket_ids = [
                    int(o.get("ticket"))
                    for o in local_outcomes
                    if isinstance(o.get("ticket"), int)
                ]
                if local_ticket_ids:
                    try:
                        local_context = await asyncio.wait_for(
                            db.get_trade_audit_context_by_tickets(local_ticket_ids[:400]),
                            timeout=4.0,
                        )
                    except Exception:
                        local_context = await asyncio.to_thread(
                            _read_trade_target_context_direct,
                            local_ticket_ids[:400],
                        )
                else:
                    local_context = {}
                try:
                    local_orders = await asyncio.to_thread(
                        _read_recent_orders_direct,
                        min(max(int(limit) * 8, 80), 160),
                    )
                except Exception as exc:
                    logger.warning("trade-history:mt5-all-local-order-overlay-skip due to error: %s", exc)
                    local_orders = []

                def _time_diff_with_server_offset(a: float, b: float) -> float:
                    raw = abs(a - b)
                    # MT5 deal history can be server-time shifted relative to local app timestamps.
                    return min(raw, abs(raw - 10800.0), abs(raw - 7200.0), abs(raw - 3600.0))

                def _match_context(trade: dict) -> Optional[dict]:
                    symbol = str(trade.get("symbol") or "").upper()
                    broker_profit = _to_float_mt5(trade.get("profit_usd"), 0.0)
                    broker_closed_at = _to_float_mt5(trade.get("closed_at"), 0.0)
                    best: tuple[float, dict] | None = None
                    for outcome in local_outcomes:
                        if str(outcome.get("symbol") or "").upper() != symbol:
                            continue
                        ticket = outcome.get("ticket")
                        if not isinstance(ticket, int):
                            continue
                        profit_diff = abs(_to_float_mt5(outcome.get("profit"), 0.0) - broker_profit)
                        if profit_diff > max(1.0, abs(broker_profit) * 0.03):
                            continue
                        outcome_closed_at = _to_float_mt5(outcome.get("closed_at"), 0.0)
                        time_diff = _time_diff_with_server_offset(broker_closed_at, outcome_closed_at)
                        if time_diff > 10 * 60:
                            continue
                        context = local_context.get(ticket)
                        if not context:
                            continue
                        score = profit_diff + (time_diff / 60.0)
                        if best is None or score < best[0]:
                            best = (score, context)
                    if best is not None:
                        return best[1]

                    order_best: tuple[float, dict] | None = None
                    for order in local_orders:
                        if str(order.get("symbol") or "").upper() != symbol:
                            continue
                        ticket = order.get("ticket")
                        if not isinstance(ticket, int):
                            continue
                        order_time = _to_float_mt5(order.get("timestamp"), 0.0)
                        time_diff = _time_diff_with_server_offset(broker_closed_at, order_time) if broker_closed_at > 0 and order_time > 0 else 999999.0
                        if time_diff > 30 * 60:
                            continue
                        comment = str(order.get("comment") or "")
                        expected_amount = (
                            _extract_linktrade_comment_amount(comment, "SLA")
                            if broker_profit < 0
                            else _extract_linktrade_comment_amount(comment, "TPA")
                        )
                        if expected_amount is None:
                            continue
                        amount_diff = 0.0
                        amount_diff = abs(abs(broker_profit) - expected_amount)
                        if amount_diff > max(1.0, abs(expected_amount) * 0.10):
                            continue
                        context = local_context.get(ticket)
                        if not context:
                            context = {
                                "order": order,
                                "management_plan": None,
                                "position_changes": [],
                                "ai_activity": [],
                                "outcome": None,
                                "post_analysis": None,
                            }
                        score = amount_diff + (time_diff / 60.0)
                        if order_best is None or score < order_best[0]:
                            order_best = (score, context)
                    return order_best[1] if order_best is not None else None

                for trade in closed[: min(len(closed), 300)]:
                    context = _match_context(trade)
                    if not context:
                        continue
                    order = context.get("order") or {}
                    outcome = context.get("outcome") or {}
                    post_analysis = context.get("post_analysis") or {}
                    trade["origin"] = "app_opened"
                    trade["app_ticket"] = outcome.get("ticket") or trade.get("app_ticket")
                    broker_close_price = _to_float_mt5(trade.get("entry_price"), 0.0)
                    if order.get("price"):
                        trade["entry_price"] = order.get("price")
                    if order.get("volume"):
                        trade["volume"] = order.get("volume")
                    trade["app_ticket"] = outcome.get("ticket") or order.get("ticket") or trade.get("app_ticket")
                    trade["exit_reason"] = outcome.get("exit_reason") or trade.get("exit_reason")
                    if not outcome and str(trade.get("exit_reason") or "").lower() == "broker_closed":
                        profit = _to_float_mt5(trade.get("profit_usd"), 0.0)
                        close_price = broker_close_price
                        order_sl = _to_float_mt5(order.get("stop_loss"), 0.0)
                        order_tp = _to_float_mt5(order.get("take_profit"), 0.0)
                        tolerance = max(abs(close_price) * 0.0002, 0.00001)
                        if profit < 0 and order_sl > 0 and abs(close_price - order_sl) <= tolerance:
                            trade["exit_reason"] = "stop_loss"
                        elif profit > 0 and order_tp > 0 and abs(close_price - order_tp) <= tolerance:
                            trade["exit_reason"] = "take_profit"
                        else:
                            trade["exit_reason"] = "broker_closed_app_order"
                    if post_analysis:
                        trade["post_analysis"] = post_analysis.get("analysis_json") or trade.get("post_analysis")
                    _apply_final_managed_targets_to_trade(trade, context)
                    _apply_order_comment_target_amounts_to_trade(trade, context)
                    _sanitize_app_opened_legacy_target_metrics(trade)
                    current_pa = trade.get("post_analysis")
                    if isinstance(current_pa, dict) and str(current_pa.get("action") or "").upper() not in {"", str(trade.get("action") or "").upper()}:
                        trade["post_analysis"] = _deterministic_trade_post_analysis(
                            symbol=str(trade.get("symbol") or "").upper(),
                            action=str(trade.get("action") or "HOLD").upper(),
                            profit_usd=_to_float_mt5(trade.get("profit_usd"), 0.0),
                            started_with_usd=_to_float_mt5(trade.get("started_with_usd"), 0.0),
                            signal_reason=str(trade.get("signal_reason") or trade.get("decision_reason") or ""),
                            risk_reason=str(trade.get("risk_reason") or ""),
                            exit_reason=str(trade.get("exit_reason") or ""),
                        )
            except Exception as exc:
                logger.warning("trade-history:mt5-all-local-overlay-skip due to error: %s", exc)

        for trade in closed:
            _sanitize_app_opened_legacy_target_metrics(trade)
            _sanitize_unmanaged_broker_target_metrics(trade)

        # Auto-fill post-analysis for broker history rows so the UI and
        # incremental trainer don't depend on manual Analyze clicks.
        if db is not None and closed:
            try:
                max_enrich = min(max(int(limit), 20), 80)
                target_rows = closed[:max_enrich]
                ticket_ids = [int(t.get("ticket")) for t in target_rows if isinstance(t.get("ticket"), int)]
                existing_map = await db.get_trade_post_analysis_by_tickets(ticket_ids)
                pending_rows: list[dict] = []
                for trade in target_rows:
                    ticket = trade.get("ticket")
                    if isinstance(ticket, int) and ticket in existing_map:
                        trade["post_analysis"] = existing_map[ticket].get("analysis_json")
                        continue

                    profit_usd = _to_float_mt5(trade.get("profit_usd"))
                    started_with_usd = _to_float_mt5(trade.get("started_with_usd"))
                    analysis = _deterministic_trade_post_analysis(
                        symbol=str(trade.get("symbol") or "").upper(),
                        action=str(trade.get("action") or "HOLD").upper(),
                        profit_usd=profit_usd,
                        started_with_usd=started_with_usd,
                        signal_reason=str(trade.get("signal_reason") or trade.get("decision_reason") or ""),
                        risk_reason=str(trade.get("risk_reason") or ""),
                        exit_reason=str(trade.get("exit_reason") or ""),
                    )
                    trade["post_analysis"] = analysis
                    pending_rows.append(trade)

                global _last_auto_post_analysis_at
                now_ts = time.time()
                if pending_rows and now_ts - _last_auto_post_analysis_at >= 15.0:
                    _last_auto_post_analysis_at = now_ts

                    async def _persist_auto_post_analysis(rows: list[dict]):
                        created_count = 0
                        for trade in rows[:30]:
                            ticket = trade.get("ticket")
                            analysis = trade.get("post_analysis")
                            if not isinstance(analysis, dict):
                                continue
                            try:
                                await db.save_trade_post_analysis(
                                    ticket=int(ticket) if isinstance(ticket, int) else None,
                                    signal_id=None,
                                    symbol=str(trade.get("symbol") or "").upper(),
                                    analysis=analysis,
                                )
                                created_count += 1
                            except Exception:
                                continue
                        if created_count > 0:
                            _schedule_incremental_meta_training("post_trade_analysis_saved")

                    asyncio.create_task(_persist_auto_post_analysis(pending_rows))
            except Exception as exc:
                logger.warning("trade-history:auto-post-analysis skipped: %s", exc)

        summary = {
            "total_trades": len(closed),
            "closed_trades": len(closed),
            "open_trades": 0,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": len(breakeven),
            "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_profit_usd": total_profit,
            "avg_profit_per_closed_trade_usd": (total_profit / len(closed)) if closed else 0.0,
            "total_started_capital_usd": 0.0,
            "roi_pct": None,
            "best_trade_usd": max((_to_float_mt5(t.get("profit_usd")) for t in closed), default=0.0),
            "worst_trade_usd": min((_to_float_mt5(t.get("profit_usd")) for t in closed), default=0.0),
        }

        return {
            "summary": summary,
            "trades": closed[:limit],
            "source": "mt5_broker_deals",
            "view": view_mode,
            "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
            "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
        }

    if active_platform == "mt5" and connector.connected and view_mode == "closed":
        # The local orders/trade_outcomes tables do not store MT5 account/server
        # ownership. When a broker account is connected, show broker history
        # from the active MT5 session so the rows belong to that account.
        fetch_limit = min(max(int(limit) * 3, 300), 5000)

        def _to_float_connected_mt5(v, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        async def _fast_local_closed_history_response(source: str):
            if db is not None:
                try:
                    local_outcomes = await asyncio.wait_for(
                        db.get_trade_outcomes(max(int(limit), 50)),
                        timeout=1.0,
                    )
                except Exception:
                    local_outcomes = await asyncio.to_thread(
                        _read_trade_outcomes_direct,
                        max(int(limit), 50),
                    )
            else:
                local_outcomes = []

            ticket_ids = [
                int(outcome.get("ticket"))
                for outcome in local_outcomes
                if isinstance(outcome.get("ticket"), int)
            ]
            try:
                audit_context_by_ticket = await asyncio.to_thread(
                    _read_trade_target_context_direct,
                    ticket_ids[: min(len(ticket_ids), 120)],
                )
            except Exception as exc:
                logger.warning("trade-history:fast-local-context-skip due to error: %s", exc)
                audit_context_by_ticket = {}

            trades = []
            for outcome in local_outcomes[:limit]:
                profit = _to_float_connected_mt5(outcome.get("profit"), 0.0)
                closed_at = _to_float_connected_mt5(outcome.get("closed_at"), 0.0)
                holding_minutes = _to_float_connected_mt5(outcome.get("holding_minutes"), 0.0)
                ticket = outcome.get("ticket")
                context = audit_context_by_ticket.get(ticket) if isinstance(ticket, int) else {}
                order = (context or {}).get("order") or {}
                post_analysis_row = (context or {}).get("post_analysis") or {}
                outcome_json = _as_dict_json(outcome.get("outcome_json"))
                action = str(order.get("action") or outcome.get("action") or "BUY").upper()
                if action not in {"BUY", "SELL"}:
                    action = "BUY"
                symbol = str(order.get("symbol") or outcome.get("symbol") or "").upper()
                opened_at = _to_float_connected_mt5(order.get("timestamp"), 0.0)
                if opened_at <= 0 and closed_at > 0:
                    opened_at = max(0.0, closed_at - (holding_minutes * 60.0))
                started_with = _to_float_connected_mt5(order.get("started_with_usd"), 0.0) or None
                entry_price = _to_float_connected_mt5(order.get("price"), 0.0)
                volume = _to_float_connected_mt5(order.get("volume"), 0.0)
                stop_loss = _to_float_connected_mt5(order.get("stop_loss"), 0.0)
                take_profit = _to_float_connected_mt5(order.get("take_profit"), 0.0)
                outcome_close_price = _to_float_connected_mt5(outcome_json.get("close_price"), 0.0)
                outcome_stop_loss = _to_float_connected_mt5(outcome_json.get("stop_loss"), 0.0)
                outcome_take_profit = _to_float_connected_mt5(outcome_json.get("take_profit"), 0.0)
                if stop_loss <= 0 and outcome_stop_loss > 0:
                    stop_loss = outcome_stop_loss
                if take_profit <= 0 and outcome_take_profit > 0:
                    take_profit = outcome_take_profit
                post_analysis = post_analysis_row.get("analysis_json") if post_analysis_row else None
                if not isinstance(post_analysis, dict):
                    post_analysis = _deterministic_trade_post_analysis(
                        symbol=symbol,
                        action=action,
                        profit_usd=profit,
                        started_with_usd=float(started_with or 0.0),
                        signal_reason=str(order.get("signal_reason") or outcome_json.get("signal_reason") or ""),
                        risk_reason=str(order.get("risk_reason") or outcome_json.get("risk_reason") or ""),
                        exit_reason=str(outcome.get("exit_reason") or "closed"),
                    )
                trades.append(
                    {
                        "ticket": ticket,
                        "signal_id": outcome.get("signal_id"),
                        "symbol": symbol,
                        "origin": "app_opened" if order else "broker_imported",
                        "action": action,
                        "status": "closed",
                        "opened_at": opened_at,
                        "closed_at": closed_at if closed_at > 0 else None,
                        "duration_minutes": holding_minutes,
                        "volume": volume if volume > 0 else None,
                        "entry_price": entry_price if entry_price > 0 else None,
                        "close_price": outcome_close_price if outcome_close_price > 0 else None,
                        "stop_loss": stop_loss if stop_loss > 0 else None,
                        "take_profit": take_profit if take_profit > 0 else None,
                        "profit_usd": profit,
                        "profit_pct": (profit / started_with * 100.0) if started_with else None,
                        "started_with_usd": started_with,
                        "ended_with_usd": (started_with + profit) if started_with else None,
                        "entry_market_value_usd": None,
                        "sl_amount_usd": _extract_linktrade_comment_amount(str(order.get("comment") or ""), "SLA"),
                        "tp_amount_usd": _extract_linktrade_comment_amount(str(order.get("comment") or ""), "TPA"),
                        "sl_pct_of_start": None,
                        "tp_pct_of_start": None,
                        "started_with_source": "provided" if started_with else "unknown",
                        "agent_name": order.get("agent_name"),
                        "signal_confidence": order.get("confidence"),
                        "signal_reason": order.get("signal_reason") or outcome_json.get("signal_reason"),
                        "risk_approved": order.get("approved"),
                        "risk_reason": order.get("risk_reason") or outcome_json.get("risk_reason"),
                        "decision_reason": (
                            order.get("signal_reason")
                            or order.get("risk_reason")
                            or outcome_json.get("detail")
                            or "Persisted trade outcome"
                        ),
                        "trade_reasons": [
                            str(reason)
                            for reason in outcome_json.get("reasons", [])
                            if str(reason).strip()
                        ] if isinstance(outcome_json.get("reasons"), list) else [],
                        "gemini_response": outcome_json.get("gemini_response"),
                        "meta_model": outcome_json.get("meta_model") if isinstance(outcome_json.get("meta_model"), dict) else None,
                        "post_analysis": post_analysis,
                        "exit_reason": outcome.get("exit_reason") or "closed",
                        "audit_trail": [
                            item
                            for item in [
                                {
                                    "time": order.get("timestamp"),
                                    "type": "order",
                                    "title": "Order opened by app",
                                    "detail": (
                                        f"{action} {symbol} volume {volume if volume > 0 else '-'}; "
                                        f"entry {entry_price if entry_price > 0 else '-'}; "
                                        f"SL {stop_loss if stop_loss > 0 else '-'} / TP {take_profit if take_profit > 0 else '-'}"
                                    ),
                                } if order else None,
                                {
                                    "time": closed_at if closed_at > 0 else None,
                                    "type": "close",
                                    "title": "Trade closed",
                                    "detail": str(outcome.get("exit_reason") or "closed"),
                                },
                            ]
                            if item is not None
                        ],
                    }
                )

            wins = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) > 0]
            losses = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) < 0]
            breakeven = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) == 0]
            total_profit = sum(_to_float_connected_mt5(t.get("profit_usd")) for t in trades)
            total_started = sum(
                _to_float_connected_mt5(t.get("started_with_usd"))
                for t in trades
                if t.get("started_with_usd") is not None
            )
            return {
                "summary": {
                    "total_trades": len(trades),
                    "closed_trades": len(trades),
                    "open_trades": 0,
                    "winning_trades": len(wins),
                    "losing_trades": len(losses),
                    "breakeven_trades": len(breakeven),
                    "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
                    "total_profit_usd": total_profit,
                    "avg_profit_per_closed_trade_usd": (total_profit / len(trades)) if trades else 0.0,
                    "total_started_capital_usd": total_started,
                    "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
                    "best_trade_usd": max((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                    "worst_trade_usd": min((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                },
                "trades": trades,
                "source": source,
                "view": view_mode,
                "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
                "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
            }

        requested_limit = max(1, min(int(limit), 500))
        account_key = "mt5:unknown"
        if connector.account_info:
            account_key = f"mt5:{getattr(connector.account_info, 'login', '')}:{getattr(connector.account_info, 'server', '')}"
        cache_key = (account_key, requested_limit, 90)
        cached = _connected_trade_history_cache.get(cache_key)
        now_ts = time.time()
        if cached and (now_ts - cached[0]) <= 20.0:
            return cached[1]

        try:
            broker_rows = await asyncio.wait_for(
                asyncio.to_thread(
                    execution.get_recent_closed_trades_fast,
                    limit=requested_limit,
                    lookback_days=90,
                ),
                timeout=6.0,
            )
        except Exception as exc:
            logger.warning("trade-history:connected-account-fast-fetch-timeout: %s", exc)
            stale = _connected_trade_history_cache.get(cache_key)
            if stale:
                payload = dict(stale[1])
                payload["source"] = "mt5_connected_account_fast_cache_stale"
                payload["warning"] = "MT5 history refresh timed out; showing recent cached connected-account rows."
                return payload
            broker_rows = []

        closed = [row for row in broker_rows if str(row.get("status") or "").lower() == "closed"]
        wins = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) > 0]
        losses = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) < 0]
        breakeven = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) == 0]
        total_profit = sum(_to_float_connected_mt5(t.get("profit_usd")) for t in closed)
        total_started = sum(
            _to_float_connected_mt5(t.get("started_with_usd"))
            for t in closed
            if t.get("started_with_usd") is not None
        )
        payload = {
            "summary": {
                "total_trades": len(closed),
                "closed_trades": len(closed),
                "open_trades": 0,
                "winning_trades": len(wins),
                "losing_trades": len(losses),
                "breakeven_trades": len(breakeven),
                "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
                "total_profit_usd": total_profit,
                "avg_profit_per_closed_trade_usd": (total_profit / len(closed)) if closed else 0.0,
                "total_started_capital_usd": total_started,
                "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
                "best_trade_usd": max((_to_float_connected_mt5(t.get("profit_usd")) for t in closed), default=0.0),
                "worst_trade_usd": min((_to_float_connected_mt5(t.get("profit_usd")) for t in closed), default=0.0),
            },
            "trades": closed[:requested_limit],
            "source": "mt5_connected_account_fast",
            "view": view_mode,
            "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
            "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
        }
        _connected_trade_history_cache[cache_key] = (now_ts, payload)
        return payload

        try:
            broker_rows = await asyncio.wait_for(
                asyncio.to_thread(
                    execution.get_recent_closed_trades,
                    limit=fetch_limit,
                    lookback_days=3650,
                ),
                timeout=8.0,
            )
        except Exception as exc:
            logger.warning("trade-history:connected-mt5-fetch-timeout using local outcomes: %s", exc)
            local_outcomes = await db.get_trade_outcomes(max(int(limit), 50)) if db is not None else []
            trades = []
            for outcome in local_outcomes[:limit]:
                profit = _to_float_connected_mt5(outcome.get("profit"), 0.0)
                trades.append(
                    {
                        "ticket": outcome.get("ticket"),
                        "signal_id": outcome.get("signal_id"),
                        "symbol": str(outcome.get("symbol") or "").upper(),
                        "origin": "app_opened" if outcome.get("signal_id") is not None else "broker_imported",
                        "action": str(outcome.get("action") or "BUY").upper(),
                        "status": "closed",
                        "opened_at": max(
                            0.0,
                            _to_float_connected_mt5(outcome.get("closed_at"), 0.0)
                            - (_to_float_connected_mt5(outcome.get("holding_minutes"), 0.0) * 60.0),
                        ),
                        "closed_at": outcome.get("closed_at"),
                        "duration_minutes": outcome.get("holding_minutes"),
                        "volume": None,
                        "entry_price": None,
                        "stop_loss": None,
                        "take_profit": None,
                        "profit_usd": profit,
                        "profit_pct": None,
                        "started_with_usd": None,
                        "ended_with_usd": None,
                        "entry_market_value_usd": None,
                        "sl_amount_usd": None,
                        "tp_amount_usd": None,
                        "sl_pct_of_start": None,
                        "tp_pct_of_start": None,
                        "started_with_source": "unknown",
                        "decision_reason": "Local app outcome fallback while MT5 history is busy.",
                        "trade_reasons": [],
                        "exit_reason": outcome.get("exit_reason") or "closed",
                        "audit_trail": [
                            {
                                "time": outcome.get("closed_at"),
                                "type": "close",
                                "title": "Trade closed",
                                "detail": str(outcome.get("exit_reason") or "closed"),
                            }
                        ],
                    }
                )
            wins = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) > 0]
            losses = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) < 0]
            breakeven = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) == 0]
            total_profit = sum(_to_float_connected_mt5(t.get("profit_usd")) for t in trades)
            return {
                "summary": {
                    "total_trades": len(trades),
                    "closed_trades": len(trades),
                    "open_trades": 0,
                    "winning_trades": len(wins),
                    "losing_trades": len(losses),
                    "breakeven_trades": len(breakeven),
                    "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
                    "total_profit_usd": total_profit,
                    "avg_profit_per_closed_trade_usd": (total_profit / len(trades)) if trades else 0.0,
                    "total_started_capital_usd": 0.0,
                    "roi_pct": None,
                    "best_trade_usd": max((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                    "worst_trade_usd": min((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                },
                "trades": trades,
                "source": "local_trade_outcomes_mt5_history_busy",
                "view": view_mode,
                "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
                "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
            }

        async def _local_outcome_history_response(source: str):
            if db is not None:
                try:
                    local_outcomes = await asyncio.wait_for(db.get_trade_outcomes(max(int(limit), 50)), timeout=2.0)
                except Exception as exc:
                    logger.warning("trade-history:local-outcomes-timeout: %s", exc)
                    try:
                        local_outcomes = await asyncio.to_thread(
                            _read_trade_outcomes_direct,
                            max(int(limit), 50),
                        )
                    except Exception as fallback_exc:
                        logger.warning("trade-history:local-outcomes-direct-skip: %s", fallback_exc)
                        local_outcomes = []
            else:
                local_outcomes = []
            audit_context_by_ticket: dict[int, dict] = {}
            if db is not None and local_outcomes:
                ticket_ids = [int(o.get("ticket")) for o in local_outcomes if isinstance(o.get("ticket"), int)]
                if ticket_ids:
                    try:
                        audit_context_by_ticket = await asyncio.wait_for(
                            db.get_trade_audit_context_by_tickets(ticket_ids[: min(len(ticket_ids), 80)]),
                            timeout=2.0,
                        )
                    except Exception as exc:
                        logger.warning("trade-history:local-audit-context-skip due to error: %s", exc)
            trades = []
            for outcome in local_outcomes[:limit]:
                profit = _to_float_connected_mt5(outcome.get("profit"), 0.0)
                closed_at = _to_float_connected_mt5(outcome.get("closed_at"), 0.0)
                holding_minutes = _to_float_connected_mt5(outcome.get("holding_minutes"), 0.0)
                ticket = outcome.get("ticket")
                context = audit_context_by_ticket.get(ticket) if isinstance(ticket, int) else {}
                order = (context or {}).get("order") or {}
                plan = (context or {}).get("management_plan") or {}
                post_analysis = (context or {}).get("post_analysis") or {}
                audit_trail = []
                if order:
                    audit_trail.append(
                        {
                            "time": order.get("timestamp"),
                            "type": "order",
                            "title": "Order opened by app",
                            "detail": (
                                f"{order.get('action') or outcome.get('action')} {order.get('symbol') or outcome.get('symbol')} "
                                f"volume {order.get('volume')}; SL {order.get('stop_loss') or '-'} / TP {order.get('take_profit') or '-'}"
                            ),
                        }
                    )
                    if order.get("risk_reason"):
                        audit_trail.append(
                            {
                                "time": order.get("timestamp"),
                                "type": "risk",
                                "title": "Risk decision",
                                "detail": str(order.get("risk_reason") or ""),
                            }
                        )
                plan_json = plan.get("plan_json") if isinstance(plan, dict) else {}
                if isinstance(plan_json, dict) and plan_json:
                    audit_trail.append(
                        {
                            "time": plan.get("created_at"),
                            "type": "plan",
                            "title": "Automatic SL/TP plan",
                            "detail": json.dumps(plan_json, ensure_ascii=True)[:500],
                        }
                    )
                for item in (context or {}).get("ai_activity") or []:
                    detail = str(item.get("decision_reason") or item.get("detail") or "").strip()
                    if detail:
                        audit_trail.append(
                            {
                                "time": item.get("timestamp"),
                                "type": "ai_activity",
                                "title": str(item.get("action") or "AI activity"),
                                "detail": detail,
                            }
                        )
                audit_trail.append(
                    {
                        "time": closed_at if closed_at > 0 else None,
                        "type": "close",
                        "title": "Trade closed",
                        "detail": str(outcome.get("exit_reason") or "closed"),
                    }
                )
                audit_trail = sorted(
                    [item for item in audit_trail if item.get("time")],
                    key=lambda item: float(item.get("time") or 0.0),
                )
                trades.append(
                    {
                        "ticket": ticket,
                        "signal_id": order.get("signal_id") if order else outcome.get("signal_id"),
                        "symbol": str(outcome.get("symbol") or "").upper(),
                        "origin": "app_opened" if outcome.get("signal_id") is not None else "broker_imported",
                        "action": str(outcome.get("action") or "BUY").upper(),
                        "status": "closed",
                        "opened_at": max(0.0, closed_at - (holding_minutes * 60.0)),
                        "closed_at": closed_at if closed_at > 0 else None,
                        "duration_minutes": holding_minutes,
                        "volume": order.get("volume") if order else None,
                        "entry_price": order.get("price") if order else None,
                        "stop_loss": order.get("stop_loss") if order else None,
                        "take_profit": order.get("take_profit") if order else None,
                        "profit_usd": profit,
                        "profit_pct": None,
                        "started_with_usd": None,
                        "ended_with_usd": None,
                        "entry_market_value_usd": None,
                        "sl_amount_usd": None,
                        "tp_amount_usd": None,
                        "sl_pct_of_start": None,
                        "tp_pct_of_start": None,
                        "started_with_source": "unknown",
                        "agent_name": order.get("agent_name") if order else None,
                        "signal_confidence": order.get("confidence") if order else None,
                        "signal_reason": order.get("signal_reason") if order else None,
                        "risk_approved": order.get("approved") if order else None,
                        "risk_reason": order.get("risk_reason") if order else None,
                        "decision_reason": (
                            order.get("signal_reason")
                            or order.get("risk_reason")
                            or "Local app outcome history used while MT5 history is busy."
                        ),
                        "trade_reasons": [],
                        "post_analysis": post_analysis.get("analysis_json") if post_analysis else None,
                        "exit_reason": outcome.get("exit_reason") or "closed",
                        "audit_trail": audit_trail,
                    }
                )
            wins = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) > 0]
            losses = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) < 0]
            breakeven = [t for t in trades if _to_float_connected_mt5(t.get("profit_usd")) == 0]
            total_profit = sum(_to_float_connected_mt5(t.get("profit_usd")) for t in trades)
            return {
                "summary": {
                    "total_trades": len(trades),
                    "closed_trades": len(trades),
                    "open_trades": 0,
                    "winning_trades": len(wins),
                    "losing_trades": len(losses),
                    "breakeven_trades": len(breakeven),
                    "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
                    "total_profit_usd": total_profit,
                    "avg_profit_per_closed_trade_usd": (total_profit / len(trades)) if trades else 0.0,
                    "total_started_capital_usd": 0.0,
                    "roi_pct": None,
                    "best_trade_usd": max((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                    "worst_trade_usd": min((_to_float_connected_mt5(t.get("profit_usd")) for t in trades), default=0.0),
                },
                "trades": trades,
                "source": source,
                "view": view_mode,
                "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
                "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
            }

        if bool(getattr(auto_trader, "is_running", False)):
            return await _local_outcome_history_response("local_trade_outcomes_auto_trading_active")

        def _parse_connected_history_json(value):
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                try:
                    return json.loads(value)
                except Exception:
                    return {}
            return {}

        def _connected_history_meta_model(journal: dict | None) -> Optional[dict]:
            journal_exec = _parse_connected_history_json((journal or {}).get("execution_decision"))
            sig_dec = journal_exec.get("signal_decision") if isinstance(journal_exec, dict) else {}
            final_signal = sig_dec.get("final_signal") if isinstance(sig_dec, dict) else {}
            metadata = final_signal.get("metadata") if isinstance(final_signal, dict) else {}
            meta_model = metadata.get("meta_model") if isinstance(metadata, dict) else {}
            if not isinstance(meta_model, dict) or not meta_model:
                return None
            return {
                "version_id": str(meta_model.get("version_id") or ""),
                "profit_probability": float(meta_model.get("profit_probability") or 0.0),
                "expected_edge": float(meta_model.get("expected_edge") or 0.0),
                "blocked": bool(meta_model.get("blocked", False)),
                "changed_decision": bool(meta_model.get("changed_decision", False)),
                "quality_before": float(meta_model.get("quality_before") or 0.0),
                "quality_after": float(meta_model.get("quality_after") or 0.0),
            }

        def _connected_history_trade_reasons(journal: dict | None) -> list[str]:
            journal_exec = _parse_connected_history_json((journal or {}).get("execution_decision"))
            if not isinstance(journal_exec, dict):
                return []
            tda = journal_exec.get("trade_decision_assessment") or {}
            reasons = tda.get("reasons") if isinstance(tda, dict) else []
            if not isinstance(reasons, list):
                return []
            return list(dict.fromkeys(str(r) for r in reasons if str(r).strip()))

        def _connected_history_gemini_summary(journal: dict | None, candidate: dict | None) -> Optional[str]:
            journal_gemini = _parse_connected_history_json((journal or {}).get("gemini_assessment"))
            value = (
                journal_gemini.get("summary_reason")
                or journal_gemini.get("reason")
                or (candidate or {}).get("gemini_summary")
            )
            return str(value) if value else None

        def _connected_history_audit_trail(trade: dict, context: dict) -> list[dict]:
            trail: list[dict] = []
            order = context.get("order") or {}
            plan = context.get("management_plan") or {}
            outcome = context.get("outcome") or {}
            journal = context.get("journal") or {}
            candidate = context.get("candidate") or {}
            opened_at = _to_float_connected_mt5(trade.get("opened_at") or order.get("timestamp"), 0.0)
            closed_at = _to_float_connected_mt5(trade.get("closed_at") or outcome.get("closed_at"), 0.0)

            if order:
                trail.append(
                    {
                        "time": opened_at or order.get("timestamp"),
                        "type": "order",
                        "title": "Order opened by app",
                        "detail": (
                            f"{order.get('action') or trade.get('action')} {order.get('symbol') or trade.get('symbol')} "
                            f"volume {order.get('volume')}; SL {order.get('stop_loss') or '-'} / TP {order.get('take_profit') or '-'}"
                        ),
                    }
                )
            if order.get("risk_reason"):
                trail.append(
                    {
                        "time": order.get("timestamp") or opened_at,
                        "type": "risk",
                        "title": "Risk decision",
                        "detail": str(order.get("risk_reason") or ""),
                    }
                )
            gemini_summary = _connected_history_gemini_summary(journal, candidate)
            if gemini_summary:
                trail.append(
                    {
                        "time": (journal or {}).get("timestamp") or opened_at,
                        "type": "gemini",
                        "title": "Gemini analysis",
                        "detail": gemini_summary,
                    }
                )
            meta_model = _connected_history_meta_model(journal)
            if meta_model:
                trail.append(
                    {
                        "time": (journal or {}).get("timestamp") or opened_at,
                        "type": "meta_model",
                        "title": "Meta model check",
                        "detail": (
                            f"p={round(float(meta_model.get('profit_probability') or 0) * 100)}%, "
                            f"q={round(float(meta_model.get('quality_after') or 0) * 100)}%, "
                            f"changed={bool(meta_model.get('changed_decision'))}, blocked={bool(meta_model.get('blocked'))}"
                        ),
                    }
                )
            plan_json = plan.get("plan_json") if isinstance(plan, dict) else {}
            if isinstance(plan_json, dict) and plan_json:
                trail.append(
                    {
                        "time": plan.get("created_at") or opened_at,
                        "type": "plan",
                        "title": "Automatic SL/TP plan",
                        "detail": json.dumps(plan_json, ensure_ascii=True)[:500],
                    }
                )
            for item in context.get("position_changes") or []:
                trail.append(
                    {
                        "time": item.get("timestamp"),
                        "type": "position",
                        "title": str(item.get("event") or "Position update"),
                        "detail": str(item.get("details") or ""),
                    }
                )
            if outcome or trade.get("exit_reason"):
                trail.append(
                    {
                        "time": closed_at or outcome.get("closed_at"),
                        "type": "close",
                        "title": "Trade closed",
                        "detail": str(outcome.get("exit_reason") or trade.get("exit_reason") or "broker_closed"),
                    }
                )
            return sorted(
                [item for item in trail if item.get("time")],
                key=lambda item: float(item.get("time") or 0.0),
            )

        closed = [row for row in broker_rows if str(row.get("status", "")).lower() == "closed"]
        closed.sort(
            key=lambda row: float(row.get("closed_at") or row.get("opened_at") or 0.0),
            reverse=True,
        )
        audit_context_by_ticket: dict[int, dict] = {}
        local_outcomes_for_match: list[dict] = []
        local_orders_for_match: list[dict] = []
        local_context_by_ticket: dict[int, dict] = {}
        local_order_context_by_ticket: dict[int, dict] = {}
        if db is not None and closed:
            try:
                ticket_ids = [int(t.get("ticket")) for t in closed if isinstance(t.get("ticket"), int)]
                audit_context_by_ticket = await db.get_trade_audit_context_by_tickets(ticket_ids[: min(len(ticket_ids), 400)])
            except Exception as exc:
                logger.warning("trade-history:audit-context-skip due to error: %s", exc)
            try:
                local_outcome_limit = min(max(int(limit) * 5, 100), 500)
                try:
                    local_outcomes_for_match = await asyncio.wait_for(
                        db.get_trade_outcomes(local_outcome_limit),
                        timeout=1.0,
                    )
                except Exception:
                    local_outcomes_for_match = await asyncio.to_thread(
                        _read_trade_outcomes_direct,
                        local_outcome_limit,
                    )
                local_ticket_ids = [
                    int(o.get("ticket"))
                    for o in local_outcomes_for_match
                    if isinstance(o.get("ticket"), int)
                ]
                missing_ticket_ids = [
                    ticket
                    for ticket in local_ticket_ids
                    if ticket not in audit_context_by_ticket
                ]
                if missing_ticket_ids:
                    try:
                        local_context_by_ticket = await asyncio.wait_for(
                            db.get_trade_audit_context_by_tickets(missing_ticket_ids[:400]),
                            timeout=4.0,
                        )
                    except Exception:
                        local_context_by_ticket = await asyncio.to_thread(
                            _read_trade_target_context_direct,
                            missing_ticket_ids[:400],
                        )
                try:
                    local_orders_for_match = await asyncio.to_thread(
                        _read_recent_orders_direct,
                        min(max(int(limit) * 8, 80), 160),
                    )
                except Exception as exc:
                    logger.warning("trade-history:local-order-context-match-skip due to error: %s", exc)
            except Exception as exc:
                logger.warning("trade-history:local-context-match-skip due to error: %s", exc)

        def _matched_local_context_for_broker_trade(trade: dict) -> Optional[dict]:
            if not local_outcomes_for_match:
                return None
            def _time_diff_with_server_offset(a: float, b: float) -> float:
                raw = abs(a - b)
                return min(raw, abs(raw - 10800.0), abs(raw - 7200.0), abs(raw - 3600.0))

            symbol = str(trade.get("symbol") or "").upper()
            broker_profit = _to_float_connected_mt5(trade.get("profit_usd"), 0.0)
            broker_closed_at = _to_float_connected_mt5(trade.get("closed_at"), 0.0)
            best: tuple[float, dict] | None = None
            for outcome in local_outcomes_for_match:
                if str(outcome.get("symbol") or "").upper() != symbol:
                    continue
                ticket = outcome.get("ticket")
                if not isinstance(ticket, int):
                    continue
                outcome_profit = _to_float_connected_mt5(outcome.get("profit"), 0.0)
                profit_diff = abs(outcome_profit - broker_profit)
                profit_tolerance = max(1.0, abs(broker_profit) * 0.03)
                if profit_diff > profit_tolerance:
                    continue
                outcome_closed_at = _to_float_connected_mt5(outcome.get("closed_at"), 0.0)
                time_diff = _time_diff_with_server_offset(broker_closed_at, outcome_closed_at) if broker_closed_at > 0 and outcome_closed_at > 0 else 0.0
                if time_diff > 10 * 60:
                    continue
                score = profit_diff + (time_diff / 60.0)
                context = local_context_by_ticket.get(ticket) or audit_context_by_ticket.get(ticket)
                if not context:
                    continue
                if best is None or score < best[0]:
                    best = (score, context)
            if best is not None:
                return best[1]

            order_best: tuple[float, dict] | None = None
            for order in local_orders_for_match:
                if str(order.get("symbol") or "").upper() != symbol:
                    continue
                ticket = order.get("ticket")
                if not isinstance(ticket, int):
                    continue
                order_time = _to_float_connected_mt5(order.get("timestamp"), 0.0)
                time_diff = _time_diff_with_server_offset(broker_closed_at, order_time) if broker_closed_at > 0 and order_time > 0 else 999999.0
                if time_diff > 30 * 60:
                    continue
                comment = str(order.get("comment") or "")
                expected_amount = (
                    _extract_linktrade_comment_amount(comment, "SLA")
                    if broker_profit < 0
                    else _extract_linktrade_comment_amount(comment, "TPA")
                )
                if expected_amount is None:
                    continue
                amount_diff = 0.0
                amount_diff = abs(abs(broker_profit) - expected_amount)
                if amount_diff > max(1.0, abs(expected_amount) * 0.10):
                    continue
                context = audit_context_by_ticket.get(ticket) or local_context_by_ticket.get(ticket)
                if not context:
                    context = {
                        "order": order,
                        "management_plan": None,
                        "position_changes": [],
                        "ai_activity": [],
                        "outcome": None,
                        "post_analysis": None,
                    }
                score = amount_diff + (time_diff / 60.0)
                if order_best is None or score < order_best[0]:
                    order_best = (score, context)
            return order_best[1] if order_best is not None else None

        wins = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) > 0]
        losses = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) < 0]
        breakeven = [t for t in closed if _to_float_connected_mt5(t.get("profit_usd")) == 0]
        total_profit = sum(_to_float_connected_mt5(t.get("profit_usd")) for t in closed)
        total_started = sum(
            _to_float_connected_mt5(t.get("started_with_usd"))
            for t in closed
            if t.get("started_with_usd") is not None
        )
        for trade in closed:
            trade["origin"] = "broker_imported"
            trade["decision_reason"] = trade.get("decision_reason") or "mt5_connected_account_history"
            trade["exit_reason"] = trade.get("exit_reason") or "broker_closed"
            ticket = trade.get("ticket")
            context = audit_context_by_ticket.get(ticket) if isinstance(ticket, int) else None
            if not context:
                context = _matched_local_context_for_broker_trade(trade)
            order = (context or {}).get("order") or {}
            outcome = (context or {}).get("outcome") or {}
            post_analysis = (context or {}).get("post_analysis") or {}
            if order:
                trade["origin"] = "app_opened"
                trade["signal_id"] = order.get("signal_id")
                trade["agent_name"] = order.get("agent_name")
                trade["signal_confidence"] = order.get("confidence")
                trade["signal_reason"] = order.get("signal_reason")
                trade["risk_approved"] = order.get("approved")
                trade["risk_reason"] = order.get("risk_reason")
                trade["decision_reason"] = order.get("signal_reason") or order.get("risk_reason") or trade.get("decision_reason")
                if order.get("price"):
                    trade["entry_price"] = trade.get("entry_price") or order.get("price")
                if order.get("stop_loss"):
                    trade["stop_loss"] = trade.get("stop_loss") or order.get("stop_loss")
                if order.get("take_profit"):
                    trade["take_profit"] = trade.get("take_profit") or order.get("take_profit")
            if outcome:
                trade["exit_reason"] = outcome.get("exit_reason") or trade.get("exit_reason")
                trade["signal_id"] = trade.get("signal_id") or outcome.get("signal_id")
            elif order and str(trade.get("exit_reason") or "").lower() == "broker_closed":
                profit = _to_float_connected_mt5(trade.get("profit_usd"), 0.0)
                close_price = _to_float_connected_mt5(trade.get("entry_price"), 0.0)
                order_sl = _to_float_connected_mt5(order.get("stop_loss"), 0.0)
                order_tp = _to_float_connected_mt5(order.get("take_profit"), 0.0)
                tolerance = max(abs(close_price) * 0.0002, 0.00001)
                if profit < 0 and order_sl > 0 and abs(close_price - order_sl) <= tolerance:
                    trade["exit_reason"] = "stop_loss"
                elif profit > 0 and order_tp > 0 and abs(close_price - order_tp) <= tolerance:
                    trade["exit_reason"] = "take_profit"
                else:
                    trade["exit_reason"] = "broker_closed_app_order"
            if post_analysis:
                trade["post_analysis"] = post_analysis.get("analysis_json") or trade.get("post_analysis")
            if context:
                _apply_final_managed_targets_to_trade(trade, context)
                _apply_order_comment_target_amounts_to_trade(trade, context)
                _sanitize_app_opened_legacy_target_metrics(trade)
                trade["trade_reasons"] = _connected_history_trade_reasons(context.get("journal"))
                trade["gemini_response"] = _connected_history_gemini_summary(context.get("journal"), context.get("candidate"))
                trade["meta_model"] = _connected_history_meta_model(context.get("journal"))
                trade["audit_trail"] = _connected_history_audit_trail(trade, context)
            _sanitize_unmanaged_broker_target_metrics(trade)
        summary = {
            "total_trades": len(closed),
            "closed_trades": len(closed),
            "open_trades": 0,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": len(breakeven),
            "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_profit_usd": total_profit,
            "avg_profit_per_closed_trade_usd": (total_profit / len(closed)) if closed else 0.0,
            "total_started_capital_usd": total_started,
            "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
            "best_trade_usd": max((_to_float_connected_mt5(t.get("profit_usd")) for t in closed), default=0.0),
            "worst_trade_usd": min((_to_float_connected_mt5(t.get("profit_usd")) for t in closed), default=0.0),
        }
        return {
            "summary": summary,
            "trades": closed[:limit],
            "source": "mt5_connected_account_history",
            "view": view_mode,
            "account_login": getattr(connector.account_info, "login", None) if connector.account_info else None,
            "account_server": getattr(connector.account_info, "server", None) if connector.account_info else None,
        }

    if active_platform == "mt5" and not connector.connected:
        return {
            "summary": {
                "total_trades": 0,
                "closed_trades": 0,
                "open_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "breakeven_trades": 0,
                "win_rate_pct": 0.0,
                "total_profit_usd": 0.0,
                "avg_profit_per_closed_trade_usd": 0.0,
                "total_started_capital_usd": 0.0,
                "roi_pct": None,
                "best_trade_usd": 0.0,
                "worst_trade_usd": 0.0,
            },
            "trades": [],
            "source": "mt5_broker_deals_unavailable_not_connected",
        }

    if active_platform == "tradestation" and not tradestation_connector.connected:
        return {
            "summary": {
                "total_trades": 0,
                "closed_trades": 0,
                "open_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "breakeven_trades": 0,
                "win_rate_pct": 0.0,
                "total_profit_usd": 0.0,
                "avg_profit_per_closed_trade_usd": 0.0,
                "total_started_capital_usd": 0.0,
                "roi_pct": None,
                "best_trade_usd": 0.0,
                "worst_trade_usd": 0.0,
            },
            "trades": [],
            "source": "tradestation_broker_history_unavailable_not_connected",
        }

    if active_platform == "ibkr" and not ibkr_connector.connected:
        return {
            "summary": {
                "total_trades": 0,
                "closed_trades": 0,
                "open_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "breakeven_trades": 0,
                "win_rate_pct": 0.0,
                "total_profit_usd": 0.0,
                "avg_profit_per_closed_trade_usd": 0.0,
                "total_started_capital_usd": 0.0,
                "roi_pct": None,
                "best_trade_usd": 0.0,
                "worst_trade_usd": 0.0,
            },
            "trades": [],
            "source": "ibkr_broker_executions_unavailable_not_connected",
        }

    if db is None:
        return {
            "summary": {
                "total_trades": 0,
                "closed_trades": 0,
                "open_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "breakeven_trades": 0,
                "win_rate_pct": 0.0,
                "total_profit_usd": 0.0,
                "avg_profit_per_closed_trade_usd": 0.0,
                "total_started_capital_usd": 0.0,
                "roi_pct": None,
                "best_trade_usd": 0.0,
                "worst_trade_usd": 0.0,
            },
            "trades": [],
        }

    global _trade_history_reconcile_cache
    now_ts = time.time()
    try:
        # Reconciliation is expensive and mainly needed for broker-parity "all" view.
        # Keep "closed" fast for dashboard/history refreshes.
        if view_mode == "all" and now_ts - float(_trade_history_reconcile_cache[0] or 0.0) >= 60.0:
            reconcile_rows = min(max(limit * 2, 120), 400)
            reconciled = await _reconcile_missing_mt5_outcomes(max_rows=reconcile_rows)
            _trade_history_reconcile_cache = (now_ts, int(reconciled or 0))
            if reconciled:
                logger.info("trade-history:reconciled-missing-outcomes count=%s", reconciled)
    except Exception as exc:
        logger.warning("trade-history:reconcile-skip due to error: %s", exc)

    raw_limit = max(limit * 3, 90)
    cursor = await db._db.execute(
        """SELECT
               o.id,
               o.timestamp,
               o.signal_id,
               o.symbol,
               o.action,
               o.volume,
               o.price,
               o.stop_loss,
               o.take_profit,
               o.ticket,
               o.success,
               o.comment,
               s.agent_name,
               s.confidence,
               s.reason AS signal_reason
           FROM orders o
           LEFT JOIN signals s ON s.id = o.signal_id
           WHERE o.success = 1
           ORDER BY o.timestamp DESC
           LIMIT ?""",
        (raw_limit,),
    )
    raw_rows = await cursor.fetchall()
    raw_cols = [d[0] for d in cursor.description]
    raw_orders = [dict(zip(raw_cols, row)) for row in raw_rows]
    outcomes = await db.get_trade_outcomes(max(limit * 4, 100))
    logger.info(
        "trade-history:data-loaded raw_orders=%s outcomes=%s elapsed_ms=%s",
        len(raw_orders),
        len(outcomes),
        int((time.time() - _t0) * 1000),
    )

    # Keep trade-history endpoint fast and deterministic by using persisted DB values only.
    # MT5 enrichment calls can block under terminal load and cause UI hangs.
    leverage = 100.0

    def _to_float(v, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _extract_started_amount(comment: str | None, signal_reason: str | None):
        for text in (comment or "", signal_reason or ""):
            m = re.search(
                r"TA:\$(\d+(?:\.\d+)?)|TA(\d+(?:\.\d+)?)|\$(\d+(?:\.\d+)?)",
                text or "",
                re.IGNORECASE,
            )
            if m:
                value = m.group(1) or m.group(2) or m.group(3)
                if value:
                    return float(value), "provided"
        return None, "unknown"

    def _extract_comment_named_amount(comment: str | None, key: str) -> Optional[float]:
        m = re.search(rf"{key}:\$(\d+(?:\.\d+)?)|{key}(\d+(?:\.\d+)?)", comment or "", re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1) or m.group(2))
        except Exception:
            return None

    def _duration_minutes(opened_at: float, closed_at: Optional[float]) -> Optional[float]:
        if opened_at <= 0:
            return None
        if closed_at is None:
            return max(0.0, (time.time() - opened_at) / 60.0)
        return max(0.0, (closed_at - opened_at) / 60.0)

    def _parse_json_blob(value):
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except Exception:
                return None
        return None

    outcome_by_ticket: dict[int, dict] = {}
    outcome_by_signal: dict[int, dict] = {}
    for outcome in outcomes:
        ticket = outcome.get("ticket")
        signal_id = outcome.get("signal_id")
        if isinstance(ticket, int) and ticket not in outcome_by_ticket:
            outcome_by_ticket[ticket] = outcome
        if isinstance(signal_id, int) and signal_id not in outcome_by_signal:
            outcome_by_signal[signal_id] = outcome

    # Keep list endpoint responsive: skip heavy bulk enrichment lookups.
    signal_ids = []
    candidate_by_signal: dict[int, dict] = {}
    journal_by_signal: dict[int, dict] = {}
    post_analysis_by_signal: dict[int, dict] = {}

    # No MT5 symbol-info enrichment in history route to avoid long blocking calls.
    def _contract_size_for_symbol(symbol_name: str) -> Optional[float]:
        _ = symbol_name
        return None

    trades: list[dict] = []
    open_profit_by_ticket: dict[int, float] = {}
    for row in raw_orders:
        action = str(row.get("action", "")).upper()
        if action not in {"BUY", "SELL"}:
            continue
        if not bool(row.get("success", 0)):
            continue

        symbol = str(row.get("symbol", "")).upper()
        ticket = row.get("ticket")
        signal_id = row.get("signal_id")
        opened_at = _to_float(row.get("timestamp"), 0.0)
        entry_price = _to_float(row.get("price"), 0.0)
        volume = _to_float(row.get("volume"), 0.0)
        stop_loss = _to_float(row.get("stop_loss"), 0.0)
        take_profit = _to_float(row.get("take_profit"), 0.0)

        outcome = None
        if isinstance(ticket, int):
            outcome = outcome_by_ticket.get(ticket)
        if outcome is None and isinstance(signal_id, int):
            candidate = outcome_by_signal.get(signal_id)
            if candidate and str(candidate.get("symbol", "")).upper() == symbol:
                outcome = candidate
        if outcome is None:
            # Trade history view should include only completed trades.
            continue

        closed_at = _to_float(outcome.get("closed_at"), 0.0) if outcome else 0.0
        closed_at_value = closed_at if closed_at > 0 else None
        profit = _to_float(outcome.get("profit"), 0.0) if outcome else None
        status = "closed"
        exit_reason = str(outcome.get("exit_reason", "")) if outcome else ""
        if status == "open" and isinstance(ticket, int) and ticket in open_profit_by_ticket:
            profit = float(open_profit_by_ticket[ticket])

        started_with, started_source = _extract_started_amount(row.get("comment"), row.get("signal_reason"))
        contract_size: Optional[float] = None
        if (
            started_with is None
            and entry_price > 0
            and volume > 0
        ):
            contract_size = _contract_size_for_symbol(symbol)
        if (
            started_with is None
            and contract_size is not None
            and entry_price > 0
            and volume > 0
        ):
            started_with = (entry_price * volume * contract_size) / max(leverage, 1.0)
            started_source = "estimated"

        ended_with = None
        if started_with is not None and profit is not None:
            ended_with = started_with + profit

        profit_pct = None
        if started_with and profit is not None and started_with != 0:
            profit_pct = (profit / started_with) * 100.0

        sl_amount = _extract_comment_named_amount(row.get("comment"), "SLA")
        tp_amount = _extract_comment_named_amount(row.get("comment"), "TPA")
        sl_pct = None
        tp_pct = None
        if sl_amount is None and stop_loss > 0 and entry_price > 0 and volume > 0:
            if contract_size is None:
                contract_size = _contract_size_for_symbol(symbol)
        if sl_amount is None and stop_loss > 0 and entry_price > 0 and volume > 0 and contract_size is not None:
            sl_amount = abs(entry_price - stop_loss) * volume * contract_size
        if tp_amount is None and take_profit > 0 and entry_price > 0 and volume > 0:
            if contract_size is None:
                contract_size = _contract_size_for_symbol(symbol)
        if tp_amount is None and take_profit > 0 and entry_price > 0 and volume > 0 and contract_size is not None:
            tp_amount = abs(take_profit - entry_price) * volume * contract_size
        if started_with and started_with > 0 and sl_amount is not None:
            sl_pct = (sl_amount / started_with) * 100.0
        if started_with and started_with > 0 and tp_amount is not None:
            tp_pct = (tp_amount / started_with) * 100.0

        candidate = candidate_by_signal.get(signal_id) if isinstance(signal_id, int) else None
        journal = journal_by_signal.get(signal_id) if isinstance(signal_id, int) else None
        journal_exec = _parse_json_blob(journal.get("execution_decision")) if journal else None
        journal_gemini = _parse_json_blob(journal.get("gemini_assessment")) if journal else None
        decision_reason = (
            (journal_exec or {}).get("reason")
            or row.get("signal_reason")
            or row.get("risk_reason")
            or (candidate or {}).get("risk_decision")
            or ""
        )
        trade_reasons: list[str] = []
        if isinstance(journal_exec, dict):
            tda = journal_exec.get("trade_decision_assessment") or {}
            if isinstance(tda, dict):
                reasons = tda.get("reasons") or []
                if isinstance(reasons, list):
                    trade_reasons.extend(str(r) for r in reasons if str(r).strip())

        gemini_response = (
            (journal_gemini or {}).get("summary_reason")
            or (journal_gemini or {}).get("reason")
            or (candidate or {}).get("gemini_summary")
            or None
        )

        meta_model_summary = None
        if isinstance(journal_exec, dict):
            sig_dec = journal_exec.get("signal_decision") or {}
            final_signal = sig_dec.get("final_signal") if isinstance(sig_dec, dict) else {}
            metadata = final_signal.get("metadata") if isinstance(final_signal, dict) else {}
            meta_model = metadata.get("meta_model") if isinstance(metadata, dict) else {}
            if isinstance(meta_model, dict) and meta_model:
                meta_model_summary = {
                    "version_id": str(meta_model.get("version_id") or ""),
                    "profit_probability": float(meta_model.get("profit_probability") or 0.0),
                    "expected_edge": float(meta_model.get("expected_edge") or 0.0),
                    "blocked": bool(meta_model.get("blocked", False)),
                    "changed_decision": bool(meta_model.get("changed_decision", False)),
                    "quality_before": float(meta_model.get("quality_before") or 0.0),
                    "quality_after": float(meta_model.get("quality_after") or 0.0),
                }
        post_analysis = None
        if isinstance(signal_id, int):
            pa = post_analysis_by_signal.get(signal_id)
            if pa:
                post_analysis = pa.get("analysis_json")
        if isinstance(post_analysis, dict):
            executed_plan = dict(post_analysis.get("executed_plan") or {})
            if entry_price > 0 and "entry_price" not in executed_plan:
                executed_plan["entry_price"] = round(entry_price, 5)
            if stop_loss > 0 and "stop_loss" not in executed_plan:
                executed_plan["stop_loss"] = round(stop_loss, 5)
            if take_profit > 0 and "take_profit" not in executed_plan:
                executed_plan["take_profit"] = round(take_profit, 5)
            if volume > 0 and "volume" not in executed_plan:
                executed_plan["volume"] = volume
            if started_with and started_with > 0 and "started_with_usd" not in executed_plan:
                executed_plan["started_with_usd"] = round(started_with, 2)
            if executed_plan:
                post_analysis = {**post_analysis, "executed_plan": executed_plan}

        trades.append(
            {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
                "origin": "app_opened",
                "action": action,
                "status": status,
                "opened_at": opened_at,
                "closed_at": closed_at_value,
                "duration_minutes": _duration_minutes(opened_at, closed_at_value),
                "volume": volume,
                "entry_price": entry_price if entry_price > 0 else None,
                "stop_loss": stop_loss if stop_loss > 0 else None,
                "take_profit": take_profit if take_profit > 0 else None,
                "profit_usd": profit,
                "profit_pct": profit_pct,
                "started_with_usd": started_with,
                "ended_with_usd": ended_with,
                "entry_market_value_usd": (entry_price * volume * contract_size) if (entry_price > 0 and contract_size is not None) else None,
                "sl_amount_usd": sl_amount,
                "tp_amount_usd": tp_amount,
                "sl_pct_of_start": sl_pct,
                "tp_pct_of_start": tp_pct,
                "started_with_source": started_source,
                "agent_name": row.get("agent_name"),
                "signal_confidence": row.get("confidence"),
                "signal_reason": row.get("signal_reason"),
                "risk_approved": row.get("approved"),
                "risk_reason": row.get("risk_reason"),
                "decision_reason": decision_reason or None,
                "trade_reasons": list(dict.fromkeys(trade_reasons)),
                "gemini_response": gemini_response,
                "meta_model": meta_model_summary,
                "post_analysis": post_analysis,
                "exit_reason": exit_reason or None,
            }
        )
        if len(trades) >= limit:
            break

    # Include closed outcomes that may not have a matching local open-order row
    # (e.g., externally opened/closed trades or runtime gaps) so history stays complete.
    if len(trades) < limit and outcomes:
        existing_keys: set[tuple[int | None, int | None, str]] = set()
        for t in trades:
            existing_keys.add(
                (
                    int(t.get("ticket")) if isinstance(t.get("ticket"), int) else None,
                    int(t.get("signal_id")) if isinstance(t.get("signal_id"), int) else None,
                    str(t.get("symbol") or "").upper(),
                )
            )

        for outcome in outcomes:
            ticket_val = outcome.get("ticket")
            signal_val = outcome.get("signal_id")
            symbol_val = str(outcome.get("symbol") or "").upper()
            key = (
                int(ticket_val) if isinstance(ticket_val, int) else None,
                int(signal_val) if isinstance(signal_val, int) else None,
                symbol_val,
            )
            if key in existing_keys:
                continue
            closed_at = _to_float(outcome.get("closed_at"), 0.0)
            holding_minutes = _to_float(outcome.get("holding_minutes"), 0.0)
            opened_at = max(0.0, closed_at - (holding_minutes * 60.0)) if closed_at > 0 else time.time()
            profit = _to_float(outcome.get("profit"), 0.0)
            trades.append(
                {
                    "ticket": ticket_val,
                    "signal_id": signal_val,
                    "symbol": symbol_val,
                    "origin": "broker_imported",
                    "action": str(outcome.get("action") or "").upper() or "BUY",
                    "status": "closed",
                    "opened_at": opened_at,
                    "closed_at": closed_at if closed_at > 0 else None,
                    "duration_minutes": holding_minutes,
                    "volume": None,
                    "entry_price": None,
                    "stop_loss": None,
                    "take_profit": None,
                    "profit_usd": profit,
                    "profit_pct": None,
                    "started_with_usd": None,
                    "ended_with_usd": None,
                    "entry_market_value_usd": None,
                    "sl_amount_usd": None,
                    "tp_amount_usd": None,
                    "sl_pct_of_start": None,
                    "tp_pct_of_start": None,
                    "started_with_source": "unknown",
                    "agent_name": None,
                    "signal_confidence": None,
                    "signal_reason": None,
                    "risk_approved": None,
                    "risk_reason": None,
                    "decision_reason": None,
                    "trade_reasons": [],
                    "gemini_response": None,
                    "meta_model": None,
                    "post_analysis": None,
                    "exit_reason": str(outcome.get("exit_reason") or "") or None,
                }
            )
            existing_keys.add(key)
            if len(trades) >= limit:
                break
    logger.info("trade-history:rows-built trades=%s elapsed_ms=%s", len(trades), int((time.time() - _t0) * 1000))

    # Reconcile realized PnL with MT5 deal history for displayed closed tickets.
    # This removes close-time race mismatches (e.g., floating P/L captured before fill).
    if active_platform == "mt5" and connector.connected and trades:
        try:
            closed_tickets = [
                int(t.get("ticket"))
                for t in trades
                if t.get("status") == "closed" and isinstance(t.get("ticket"), int)
            ]
            if closed_tickets:
                # Broker-side realized PnL lookup is relatively expensive on larger history loads.
                # Reconcile only the most recent slice where race-condition mismatches matter most,
                # and cache the lookup briefly so repeated history refreshes stay fast.
                closed_tickets = closed_tickets[:40]
                cache_key = tuple(closed_tickets)
                cached_realized = _trade_history_realized_cache.get(cache_key)
                now_ts = time.time()
                if cached_realized and (now_ts - cached_realized[0]) <= 30.0:
                    broker_realized = cached_realized[1]
                else:
                    broker_realized = execution.get_realized_profit_map(closed_tickets, lookback_days=30)
                    _trade_history_realized_cache[cache_key] = (
                        now_ts,
                        broker_realized,
                    )
                for trade in trades:
                    ticket = trade.get("ticket")
                    if not isinstance(ticket, int):
                        continue
                    if ticket not in broker_realized:
                        continue
                    broker_profit = float(broker_realized[ticket] or 0.0)
                    trade["profit_usd"] = broker_profit
                    started = trade.get("started_with_usd")
                    if isinstance(started, (int, float)):
                        started_value = float(started)
                        trade["ended_with_usd"] = started_value + broker_profit
                        if started_value != 0:
                            trade["profit_pct"] = (broker_profit / started_value) * 100.0
        except Exception as exc:
            logger.warning("trade-history:pnl-reconcile-skip due to error: %s", exc)

    closed = [t for t in trades if t.get("status") == "closed" and t.get("profit_usd") is not None]
    managed_closed = [
        t
        for t in closed
        if str(t.get("origin") or "") != "broker_imported"
        and isinstance(t.get("signal_id"), int)
    ]
    summary_rows = managed_closed if managed_closed else closed
    wins = [t for t in summary_rows if _to_float(t.get("profit_usd")) > 0]
    losses = [t for t in summary_rows if _to_float(t.get("profit_usd")) < 0]
    breakeven = [t for t in summary_rows if _to_float(t.get("profit_usd")) == 0]
    total_profit = sum(_to_float(t.get("profit_usd")) for t in summary_rows)
    total_started = sum(_to_float(t.get("started_with_usd")) for t in summary_rows if t.get("started_with_usd") is not None)

    summary = {
        "total_trades": len(summary_rows),
        "closed_trades": len(summary_rows),
        "open_trades": 0,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "win_rate_pct": (len(wins) / len(summary_rows) * 100.0) if summary_rows else 0.0,
        "total_profit_usd": total_profit,
        "avg_profit_per_closed_trade_usd": (total_profit / len(summary_rows)) if summary_rows else 0.0,
        "total_started_capital_usd": total_started,
        "roi_pct": (total_profit / total_started * 100.0) if total_started > 0 else None,
        "best_trade_usd": max((_to_float(t.get("profit_usd")) for t in summary_rows), default=0.0),
        "worst_trade_usd": min((_to_float(t.get("profit_usd")) for t in summary_rows), default=0.0),
        "managed_rows_used": len(managed_closed),
        "imported_rows_ignored": max(0, len(closed) - len(summary_rows)),
    }

    logger.info("trade-history:done total=%s elapsed_ms=%s", len(trades), int((time.time() - _t0) * 1000))
    return {"summary": summary, "trades": trades}


class TradePostAnalyzeRequest(BaseModel):
    force_refresh: bool = False
    use_gemini: bool = True
    signal_id: Optional[int] = None
    symbol: Optional[str] = None
    action: Optional[str] = None
    profit_usd: Optional[float] = None
    started_with_usd: Optional[float] = None
    signal_reason: Optional[str] = None
    risk_reason: Optional[str] = None
    exit_reason: Optional[str] = None


def _deterministic_trade_post_analysis(
    *,
    symbol: str,
    action: str,
    profit_usd: float,
    started_with_usd: float,
    signal_reason: str,
    risk_reason: str,
    exit_reason: str,
    executed_plan: dict | None = None,
) -> dict:
    pnl_pct = (profit_usd / started_with_usd * 100.0) if started_with_usd else 0.0
    won = profit_usd > 0
    quality = {
        "execution_quality": 0.68 if won else 0.42,
        "risk_discipline": 0.72 if abs(pnl_pct) <= 15 else 0.48,
        "timing_quality": 0.62 if won else 0.45,
    }
    mistakes: list[str] = []
    improvements: list[str] = []
    if not won:
        if "spread" in (risk_reason or "").lower():
            mistakes.append("Entered under unfavorable spread/cost conditions.")
            improvements.append("Wait for spread normalization before entry.")
        if "late" in (signal_reason or "").lower() or "extended" in (signal_reason or "").lower():
            mistakes.append("Entry likely happened after move extension.")
            improvements.append("Prefer pullback entry instead of chasing.")
        improvements.append("Reduce position size until quality score improves.")
    else:
        improvements.append("Maintain same setup profile and risk discipline.")

    if not improvements:
        improvements = ["Keep stop-loss and take-profit discipline consistent."]

    reasons_blob = " ".join([signal_reason or "", risk_reason or "", exit_reason or ""]).lower()
    diagnostics = {
        "late_entry_flag": 1 if any(k in reasons_blob for k in ["late", "extended", "chasing"]) else 0,
        "spread_stress_flag": 1 if "spread" in reasons_blob else 0,
        "trend_conflict_flag": 1 if any(k in reasons_blob for k in ["counter", "disagree", "trend conflict"]) else 0,
        "risk_overexposure_flag": 1 if any(k in reasons_blob for k in ["exposure", "margin", "overlever", "over-size"]) else 0,
        "stop_too_tight_flag": 1 if any(k in reasons_blob for k in ["stop too close", "sl too close", "stopped out"]) else 0,
        "target_too_far_flag": 1 if any(k in reasons_blob for k in ["tp too far", "unrealistic target"]) else 0,
        "news_shock_flag": 1 if any(k in reasons_blob for k in ["news", "event", "headline", "macro"]) else 0,
        "execution_delay_flag": 1 if any(k in reasons_blob for k in ["delay", "slippage", "requote"]) else 0,
    }
    expected_win_rate_adjustment = float(max(-0.2, min(0.2, (-0.06 if not won else 0.03))))
    recommended_sl_pct = 0.10
    recommended_tp_pct = 0.20
    if started_with_usd > 0:
        pnl_pct_local = (profit_usd / started_with_usd) * 100.0
        if pnl_pct_local < -10:
            recommended_sl_pct = 0.08
            recommended_tp_pct = 0.16
        elif pnl_pct_local > 10:
            recommended_sl_pct = 0.11
            recommended_tp_pct = 0.22

    return {
        "symbol": symbol,
        "action": action,
        "profit_usd": round(float(profit_usd), 2),
        "profit_pct": round(float(pnl_pct), 2),
        "executed_plan": dict(executed_plan or {}),
        "summary": (
            f"{'Winning' if won else 'Losing'} {action} trade on {symbol}. "
            f"Outcome was {pnl_pct:+.2f}% ({profit_usd:+.2f} USD)."
        ),
        "what_went_well": [] if not won else ["Direction and risk profile aligned with market movement."],
        "mistakes": mistakes,
        "improvement_actions": improvements,
        "root_causes": [
            reason for reason in [signal_reason, risk_reason, exit_reason] if str(reason or "").strip()
        ][:3],
        "future_confidence_adjustment": -0.08 if not won else 0.04,
        "quality_scores": quality,
        "diagnostics": diagnostics,
        "recommendation": {
            "expected_win_rate_adjustment": expected_win_rate_adjustment,
            "suggested_sl_pct_of_start": recommended_sl_pct,
            "suggested_tp_pct_of_start": recommended_tp_pct,
            "next_trade_size_adjustment_pct": -10.0 if not won else 0.0,
        },
        "generated_by": "deterministic_fallback",
        "generated_at": time.time(),
    }


@router.post("/trade-history/{ticket}/analyze")
async def analyze_trade_history_item(ticket: int, req: TradePostAnalyzeRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")

    order = await db.get_order_with_context_by_ticket(ticket)
    outcome = await db.get_trade_outcome_by_ticket(ticket)

    signal_id = (
        order.get("signal_id") if (order and isinstance(order.get("signal_id"), int))
        else (int(req.signal_id) if req.signal_id is not None else None)
    )
    if outcome is None and signal_id is not None:
        outcome = await db.get_trade_outcome_by_signal_id(signal_id)

    symbol = (
        str((order or {}).get("symbol") or (outcome or {}).get("symbol") or req.symbol or "").upper()
    )
    action = (
        str((order or {}).get("action") or (outcome or {}).get("action") or req.action or "HOLD").upper()
    )
    if not symbol:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trade ticket {ticket} has no analyzable symbol context. "
                "Please refresh trade history and retry."
            ),
        )

    if not req.force_refresh:
        existing = await db.get_trade_post_analysis(ticket=ticket, signal_id=signal_id)
        if existing and isinstance(existing.get("analysis_json"), dict) and existing.get("analysis_json"):
            return {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
                "analysis": existing.get("analysis_json"),
                "cached": True,
            }

    signal_reason = str((order or {}).get("signal_reason") or req.signal_reason or "")
    risk_reason = str((order or {}).get("risk_reason") or req.risk_reason or "")
    exit_reason = str((outcome or {}).get("exit_reason") or req.exit_reason or "")
    profit_usd = float((outcome or {}).get("profit") or req.profit_usd or 0.0)
    started_with_usd = float(req.started_with_usd or 0.0)
    entry_price = float((order or {}).get("price") or 0.0)
    volume = float((order or {}).get("volume") or 0.0)
    stop_loss = float((order or {}).get("stop_loss") or 0.0)
    take_profit = float((order or {}).get("take_profit") or 0.0)
    if started_with_usd <= 0 and entry_price > 0 and volume > 0 and connector.connected:
        try:
            info = market_data.get_symbol_info(symbol) or {}
            contract_size = float(info.get("trade_contract_size") or 0.0)
            account = connector.refresh_account()
            leverage = float(getattr(account, "leverage", 100) or 100)
            if contract_size > 0:
                started_with_usd = (entry_price * volume * contract_size) / max(leverage, 1.0)
        except Exception:
            started_with_usd = 0.0

    executed_plan: dict[str, object] = {}
    if entry_price > 0:
        executed_plan["entry_price"] = round(entry_price, 5)
    if stop_loss > 0:
        executed_plan["stop_loss"] = round(stop_loss, 5)
    if take_profit > 0:
        executed_plan["take_profit"] = round(take_profit, 5)
    if volume > 0:
        executed_plan["volume"] = volume
    if started_with_usd > 0:
        executed_plan["started_with_usd"] = round(started_with_usd, 2)
    try:
        symbol_info_for_analysis = market_data.get_symbol_info(symbol) if _active_platform_connected() else None
    except Exception:
        symbol_info_for_analysis = None
    if (
        entry_price > 0
        and volume > 0
        and isinstance(symbol_info_for_analysis, dict)
    ):
        risk_amount = _price_distance_to_usd(
            symbol_info=symbol_info_for_analysis,
            entry_price=entry_price,
            target_price=stop_loss,
            volume=volume,
        ) if stop_loss > 0 else None
        reward_amount = _price_distance_to_usd(
            symbol_info=symbol_info_for_analysis,
            entry_price=entry_price,
            target_price=take_profit,
            volume=volume,
        ) if take_profit > 0 else None
        if risk_amount is not None:
            executed_plan["sl_amount_usd"] = round(risk_amount, 2)
            if started_with_usd > 0:
                executed_plan["sl_pct_of_start"] = round((risk_amount / started_with_usd) * 100.0, 2)
        if reward_amount is not None:
            executed_plan["tp_amount_usd"] = round(reward_amount, 2)
            if started_with_usd > 0:
                executed_plan["tp_pct_of_start"] = round((reward_amount / started_with_usd) * 100.0, 2)

    journal = None
    candidate = None
    if signal_id is not None:
        try:
            journal_map = await db.get_evaluation_journal_by_signal_ids([signal_id])
            journal = journal_map.get(signal_id)
        except Exception:
            journal = None
        try:
            candidate_map = await db.get_trade_candidates_by_signal_ids([signal_id])
            candidate = candidate_map.get(signal_id)
        except Exception:
            candidate = None

    analysis = _deterministic_trade_post_analysis(
        symbol=symbol,
        action=action,
        profit_usd=profit_usd,
        started_with_usd=started_with_usd,
        signal_reason=signal_reason,
        risk_reason=risk_reason,
        exit_reason=exit_reason,
        executed_plan=executed_plan,
    )

    if req.use_gemini and gemini_agent.available and getattr(gemini_agent, "_client", None) is not None:
        try:
            from google.genai import types as genai_types

            prompt_payload = {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
                "action": action,
                "profit_usd": profit_usd,
                "started_with_usd": started_with_usd,
                "executed_plan": executed_plan,
                "signal_reason": signal_reason,
                "risk_reason": risk_reason,
                "exit_reason": exit_reason,
                "trade_outcome": outcome or {},
                "journal": journal or {},
                "candidate": candidate or {},
            }
            system_prompt = """
You are a post-trade analyst for a broker-connected trading system.
Return STRICT JSON only with this shape:
{
  "summary": "one concise paragraph",
  "what_went_well": ["..."],
  "mistakes": ["..."],
  "improvement_actions": ["..."],
  "root_causes": ["..."],
  "future_confidence_adjustment": number,
  "quality_scores": {
    "execution_quality": number,
    "risk_discipline": number,
    "timing_quality": number
  },
  "diagnostics": {
    "late_entry_flag": 0|1,
    "spread_stress_flag": 0|1,
    "trend_conflict_flag": 0|1,
    "risk_overexposure_flag": 0|1,
    "stop_too_tight_flag": 0|1,
    "target_too_far_flag": 0|1,
    "news_shock_flag": 0|1,
    "execution_delay_flag": 0|1
  },
  "recommendation": {
    "expected_win_rate_adjustment": number,
    "suggested_sl_pct_of_start": number,
    "suggested_tp_pct_of_start": number,
    "next_trade_size_adjustment_pct": number
  }
}
Rules:
- Keep values bounded and realistic.
- Do not provide order execution instructions.
- This is analysis only; never claim guaranteed outcomes.
"""
            response = await _generate_gemini_content_with_fallback(
                primary_model=gemini_agent.model_name,
            extra_models=["gemini-2.5-flash", "gemini-2.0-flash"],
                contents=[{"role": "user", "parts": [{"text": json.dumps(prompt_payload, ensure_ascii=True)}]}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.15,
                    response_mime_type="application/json",
                ),
                timeout_seconds=5.0,
            )
            parsed = _extract_json_object((response.text or "").strip())
            parsed_diagnostics = parsed.get("diagnostics") or {}
            base_diagnostics = analysis.get("diagnostics") or {}
            parsed_recommendation = parsed.get("recommendation") or {}
            base_recommendation = analysis.get("recommendation") or {}
            analysis.update(
                {
                    "summary": str(parsed.get("summary") or analysis["summary"]),
                    "what_went_well": list(parsed.get("what_went_well") or analysis.get("what_went_well", [])),
                    "mistakes": list(parsed.get("mistakes") or analysis.get("mistakes", [])),
                    "improvement_actions": list(parsed.get("improvement_actions") or analysis.get("improvement_actions", [])),
                    "root_causes": list(parsed.get("root_causes") or analysis.get("root_causes", [])),
                    "future_confidence_adjustment": float(parsed.get("future_confidence_adjustment") or analysis.get("future_confidence_adjustment", 0.0)),
                    "quality_scores": {
                        "execution_quality": float(((parsed.get("quality_scores") or {}).get("execution_quality") or (analysis.get("quality_scores") or {}).get("execution_quality", 0.5))),
                        "risk_discipline": float(((parsed.get("quality_scores") or {}).get("risk_discipline") or (analysis.get("quality_scores") or {}).get("risk_discipline", 0.5))),
                        "timing_quality": float(((parsed.get("quality_scores") or {}).get("timing_quality") or (analysis.get("quality_scores") or {}).get("timing_quality", 0.5))),
                    },
                    "diagnostics": {
                        "late_entry_flag": int(parsed_diagnostics.get("late_entry_flag", base_diagnostics.get("late_entry_flag", 0)) or 0),
                        "spread_stress_flag": int(parsed_diagnostics.get("spread_stress_flag", base_diagnostics.get("spread_stress_flag", 0)) or 0),
                        "trend_conflict_flag": int(parsed_diagnostics.get("trend_conflict_flag", base_diagnostics.get("trend_conflict_flag", 0)) or 0),
                        "risk_overexposure_flag": int(parsed_diagnostics.get("risk_overexposure_flag", base_diagnostics.get("risk_overexposure_flag", 0)) or 0),
                        "stop_too_tight_flag": int(parsed_diagnostics.get("stop_too_tight_flag", base_diagnostics.get("stop_too_tight_flag", 0)) or 0),
                        "target_too_far_flag": int(parsed_diagnostics.get("target_too_far_flag", base_diagnostics.get("target_too_far_flag", 0)) or 0),
                        "news_shock_flag": int(parsed_diagnostics.get("news_shock_flag", base_diagnostics.get("news_shock_flag", 0)) or 0),
                        "execution_delay_flag": int(parsed_diagnostics.get("execution_delay_flag", base_diagnostics.get("execution_delay_flag", 0)) or 0),
                    },
                    "recommendation": {
                        "expected_win_rate_adjustment": float(parsed_recommendation.get("expected_win_rate_adjustment", base_recommendation.get("expected_win_rate_adjustment", 0.0)) or 0.0),
                        "suggested_sl_pct_of_start": float(parsed_recommendation.get("suggested_sl_pct_of_start", base_recommendation.get("suggested_sl_pct_of_start", 0.10)) or 0.10),
                        "suggested_tp_pct_of_start": float(parsed_recommendation.get("suggested_tp_pct_of_start", base_recommendation.get("suggested_tp_pct_of_start", 0.20)) or 0.20),
                        "next_trade_size_adjustment_pct": float(parsed_recommendation.get("next_trade_size_adjustment_pct", base_recommendation.get("next_trade_size_adjustment_pct", 0.0)) or 0.0),
                    },
                    "generated_by": "gemini_assisted",
                    "generated_at": time.time(),
                }
            )
        except Exception as exc:
            logger.warning("Trade post-analysis Gemini fallback for ticket %s: %s", ticket, exc)

    await db.save_trade_post_analysis(
        ticket=ticket,
        signal_id=signal_id,
        symbol=symbol,
        analysis=analysis,
    )
    _schedule_incremental_meta_training("post_trade_analysis_saved")

    return {
        "ticket": ticket,
        "signal_id": signal_id,
        "symbol": symbol,
        "analysis": analysis,
        "cached": False,
    }


# --- Credentials Routes ---

def _legacy_credentials_disabled() -> None:
    raise HTTPException(
        status_code=410,
        detail="Broker credential storage is disabled. Log in inside the local MT5 terminal and use /connect.",
    )


class SaveCredentialsRequest(BaseModel):
    account: int
    server: str
    password: str
    terminal_path: str = ""
    label: str = ""


@router.post("/credentials")
async def save_credentials(req: SaveCredentialsRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_manage_secrets")
    _legacy_credentials_disabled()
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    status = await _persist_credentials_if_requested(
        account=req.account,
        server=req.server,
        password=req.password,
        terminal_path=req.terminal_path,
        save_credentials=True,
        label=req.label,
    )
    if not status.get("saved"):
        raise HTTPException(status_code=503, detail=status.get("reason", "Failed to save credentials"))
    await _get_saved_credentials_with_fallback(timeout_seconds=4.0)
    return {"saved": True, "backend": status.get("backend")}


@router.get("/credentials")
async def get_credentials():
    _legacy_credentials_disabled()
    if db is None:
        return list(_saved_credentials_cache)
    return await _get_saved_credentials_with_fallback(timeout_seconds=4.0)


@router.delete("/credentials/{account_id}")
async def delete_credentials(account_id: int, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_manage_secrets")
    _legacy_credentials_disabled()
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await _get_saved_credential_with_fallback(account_id, timeout_seconds=3.0)
    if existing:
        credential_vault.delete_password(
            existing.get("secret_ref", ""),
            existing.get("secret_backend", ""),
        )
    await db.delete_saved_credentials(account_id)
    await _get_saved_credentials_with_fallback(timeout_seconds=4.0)
    return {"deleted": True}


@router.get("/credentials/auto-connect")
async def auto_connect(account_id: Optional[int] = None, fast: bool = True, manual: bool = False):
    """Try to connect using the most recently used saved credentials."""
    global active_platform
    global _auto_connect_task
    global _auto_connect_state
    _legacy_credentials_disabled()
    if not manual:
        if bool(_auto_connect_state.get("paused")):
            reason = str(_auto_connect_state.get("pause_reason") or "Auto-connect paused after manual disconnect.")
        else:
            reason = "Auto-connect requires a manual user action."
        return {
            "connected": False,
            "pending": False,
            "paused": bool(_auto_connect_state.get("paused")),
            "reason": reason,
            "platform": active_platform,
        }
    _resume_auto_connect()
    if db is None:
        return {"connected": False, "reason": "No database"}
    if mt5_connector.connected:
        # Handle stale MT5 sessions where account_info still works but the terminal
        # has lost its trade-server connection, which makes orders fail with 10031.
        refreshed = mt5_connector.refresh_account()
        if refreshed is not None and mt5_connector.trade_server_connected():
            if (
                bool(config.LIVE_TRADING_ENABLED)
                and not bool(risk_engine.user_policy.demo_only_default)
                and mt5_connector.is_demo()
            ):
                reason = (
                    "Saved auto-connect found a demo MT5 account while live policy is active. "
                    "Connect a live MT5 account."
                )
                mt5_connector.disconnect()
                _auto_connect_state["last_error"] = reason
                return {
                    "connected": False,
                    "reason": reason,
                    "platform": "mt5",
                }
            else:
                return {
                    "connected": True,
                    "reason": "Already connected",
                    "platform": "mt5",
                    "account": refreshed.model_dump(),
                }
        if mt5_connector.connected:
            return {
                "connected": False,
                "reason": _auto_connect_state.get("last_error") or "Connected MT5 session is not valid for auto-connect.",
                "platform": "mt5",
            }
        logger.warning("MT5 connector was stale during auto-connect; reconnecting trade server session.")
        mt5_connector.disconnect()

    if account_id is not None:
        selected = await _get_saved_credential_with_fallback(account_id, timeout_seconds=3.0)
        creds = [selected] if selected else []
    else:
        creds = await _get_saved_credentials_with_fallback(timeout_seconds=4.0)

    if not creds or creds[0] is None:
        return {"connected": False, "reason": "No saved credentials"}

    best = creds[0]  # Most recently used
    if _auto_connect_task and not _auto_connect_task.done():
        return {
            "connected": bool(mt5_connector.connected),
            "pending": True,
            "reason": "Connecting in background",
            "platform": active_platform,
            "last_error": _auto_connect_state.get("last_error", ""),
        }

    async def _finish_auto_connect(credential: dict):
        global active_platform
        global _auto_connect_state
        _auto_connect_state["pending"] = True
        _auto_connect_state["last_started_at"] = time.time()
        _auto_connect_state["last_error"] = ""
        try:
            params = _build_saved_connection_params(credential)
            success = await asyncio.to_thread(mt5_connector.connect, params)
            if not success:
                _auto_connect_state["last_error"] = str(mt5_connector.last_error or "Connection failed")
                return
            if (
                bool(config.LIVE_TRADING_ENABLED)
                and not bool(risk_engine.user_policy.demo_only_default)
                and mt5_connector.is_demo()
            ):
                mt5_connector.disconnect()
                _auto_connect_state["last_error"] = (
                    "Saved credentials connected to a demo MT5 account, but live policy is active. "
                    "Connect a live MT5 account."
                )
                return
            if not mt5_connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
                mt5_connector.disconnect()
                _auto_connect_state["last_error"] = "Live account rejected - demo only mode"
                return
            if ibkr_connector.connected:
                await _run_ibkr(ibkr_connector.disconnect)
            if tradestation_connector.connected:
                tradestation_connector.disconnect()
            active_platform = "mt5"
            _apply_active_broker_runtime("mt5")
            _ensure_position_manager_running_if_connected()
            if mt5_connector.account_info:
                risk_engine.set_daily_start_equity(mt5_connector.account_info.equity)
            if risk_engine.auto_trade_enabled and risk_engine.auto_trade_session_armed and not auto_trader.is_running:
                task_orchestrator.start_auto_trade()
            asyncio.create_task(_finalize_auto_connect_success(dict(credential)))
        except Exception as exc:
            _auto_connect_state["last_error"] = str(exc)
        finally:
            _auto_connect_state["pending"] = False
            _auto_connect_state["last_finished_at"] = time.time()

    if fast:
        _auto_connect_task = asyncio.create_task(_finish_auto_connect(best))
        return {
            "connected": False,
            "pending": True,
            "reason": "Connecting in background",
            "platform": active_platform,
        }

    await _finish_auto_connect(best)
    if mt5_connector.connected:
        return {
            "connected": True,
            "pending": False,
            "platform": active_platform,
            "account": mt5_connector.account_info.model_dump() if mt5_connector.account_info else None,
        }
    return {
        "connected": False,
        "pending": False,
        "reason": _auto_connect_state.get("last_error") or mt5_connector.last_error or "Connection failed",
        "platform": active_platform,
    }


# --- Smart Evaluate Routes ---

class SmartEvaluateRequest(BaseModel):
    symbols: Optional[list[str]] = None
    manual: bool = False
    auto_execute: bool = False


@router.post("/agent/smart-evaluate")
async def smart_evaluate(req: SmartEvaluateRequest, authorization: Optional[str] = Header(default=None)):
    """Scan multiple symbols with the active Gemini-primary pipeline."""
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if (
        bool(req.auto_execute)
        and not bool(req.manual)
        and active_platform != "ibkr"
        and not connector.is_demo()
        and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED)
    ):
        raise HTTPException(
            status_code=403,
            detail="Auto-executing scan results on live accounts requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
        )

    allowed_scope = {
        universe_service.canonical_symbol(str(sym).strip().upper())
        for sym in (risk_engine.settings.allowed_symbols or [])
        if str(sym).strip()
    }

    if active_platform == "ibkr":
        symbols = req.symbols or risk_engine.settings.allowed_symbols
        ibkr_scan_limit = (
            int(config.SMART_EVAL_MAX_SCAN_SYMBOLS_MANUAL)
            if req.manual
            else int(config.SMART_EVAL_MAX_SCAN_SYMBOLS_AUTO)
        )
        market_universe = universe_service.filter_market_symbols(
            _ibkr_market_symbols(include_inactive=False),
            include_inactive=False,
        )
        if not symbols:
            symbols = universe_service.candidate_universe(market_universe)[:ibkr_scan_limit]
        else:
            symbols = (universe_service.restrict_symbols(symbols))[:ibkr_scan_limit]
        if allowed_scope:
            symbols = [
                sym for sym in symbols
                if universe_service.canonical_symbol(sym) in allowed_scope
            ][:ibkr_scan_limit]
        semaphore = asyncio.Semaphore(1)

        async def _eval_ibkr_symbol(sym: str) -> dict:
            async with semaphore:
                return await _evaluate_ibkr_symbol_recommendation(sym)

        recommendations: list[dict] = []
        for sym in symbols:
            recommendations.append(await _eval_ibkr_symbol(sym))
        recommendations.sort(
            key=lambda r: (
                float(r.get("signal", {}).get("confidence", 0.0) or 0.0),
                float((r.get("trade_quality") or {}).get("final_trade_quality_score", 0.0) or 0.0),
                bool(r.get("ready_to_execute")),
            ),
            reverse=True,
        )

        return {
            "recommendations": recommendations,
            "scanned_at": time.time(),
        }

    # Keep interactive dashboard scans focused on a shortlist instead of trying to
    # deep-evaluate too much of the broker universe in one request.
    MAX_SCAN_SYMBOLS = (
        int(config.SMART_EVAL_MAX_SCAN_SYMBOLS_MANUAL)
        if req.manual
        else int(config.SMART_EVAL_MAX_SCAN_SYMBOLS_AUTO)
    )
    # Keep enough headroom so confidence doesn't collapse to timeout fallbacks.
    base_per_symbol_timeout = float(config.SMART_EVAL_PER_SYMBOL_TIMEOUT_SECONDS)
    # Ensure per-symbol timeout is comfortably above Gemini timeout + pipeline overhead.
    gemini_floor_timeout = float(config.GEMINI_TIMEOUT_SECONDS) + 8.0
    PER_SYMBOL_TIMEOUT_SECONDS = max(base_per_symbol_timeout, gemini_floor_timeout)
    if req.manual:
        PER_SYMBOL_TIMEOUT_SECONDS += 4.0
    # Keep a bounded overall scan while allowing staggered completion.
    base_total_timeout = float(config.SMART_EVAL_TOTAL_TIMEOUT_SECONDS)
    TOTAL_SCAN_TIMEOUT_SECONDS = (base_total_timeout + 15.0) if req.manual else base_total_timeout
    # Lower concurrency to reduce local model contention and timeout churn.
    MAX_CONCURRENT_EVALS = int(config.SMART_EVAL_MAX_CONCURRENT_EVALS)
    symbols = req.symbols or risk_engine.settings.allowed_symbols
    tradeable_fetch = getattr(market_data, "get_tradeable_symbols_cached", None)
    visible_fetch = getattr(market_data, "get_visible_symbols_cached", None)
    try:
        tradeable_symbols = (
            tradeable_fetch(ttl_seconds=2.0) if callable(tradeable_fetch) else market_data.get_tradeable_symbols()
        )
    except Exception:
        tradeable_symbols = []
    if not tradeable_symbols:
        try:
            tradeable_symbols = (
                visible_fetch(ttl_seconds=2.0) if callable(visible_fetch) else market_data.get_visible_symbols()
            )
        except Exception:
            tradeable_symbols = []
    market_universe = universe_service.filter_market_symbols(
        tradeable_symbols,
        include_inactive=False,
    )
    market_universe = [s for s in market_universe if _has_usable_broker_quote(s)]
    symbol_lookup: dict[str, dict] = {}
    for item in market_universe:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        canonical = str(item.get("canonical_symbol") or name).strip()
        for key in {name, name.upper(), canonical, canonical.upper()}:
            symbol_lookup[key] = item

    def _resolve_requested_symbol(sym: str) -> Optional[str]:
        raw = str(sym or "").strip()
        if not raw:
            return None
        candidates = [
            raw,
            raw.upper(),
            universe_service.canonical_symbol(raw),
            universe_service.canonical_symbol(raw).upper(),
        ]
        for key in candidates:
            row = symbol_lookup.get(key)
            if row and row.get("name"):
                return str(row["name"])
        return None
    history_commission_map = execution.get_recent_commission_by_symbol()

    if not symbols:
        # Use canonical universe selection to reduce alias duplicates and include the full active set.
        symbols = universe_service.candidate_universe(market_universe)[:MAX_SCAN_SYMBOLS]
        # Some brokers can return an empty tradeable universe until symbols are explicitly
        # selected/enabled. Fall back to configured canonical shortlist so the dashboard
        # still produces confidence/analysis output instead of an empty result set.
        if not symbols:
            symbols = universe_service.restrict_symbols(
                list(config.AUTO_TRADE_FALLBACK_SYMBOLS or [])
            )[:MAX_SCAN_SYMBOLS]
    else:
        direct: list[str] = []
        seen_direct: set[str] = set()
        for sym in symbols:
            resolved = _resolve_requested_symbol(sym)
            if not resolved and req.manual:
                resolved = str(sym or "").strip()
            if not resolved:
                continue
            normalized = resolved.upper()
            if normalized in seen_direct:
                continue
            seen_direct.add(normalized)
            direct.append(resolved)
        symbols = (
            direct
            or universe_service.candidate_universe(market_universe)
            or universe_service.restrict_symbols(symbols)
        )[:MAX_SCAN_SYMBOLS]
    if allowed_scope:
        symbols = [
            sym for sym in symbols
            if universe_service.canonical_symbol(sym) in allowed_scope
        ][:MAX_SCAN_SYMBOLS]

    open_symbols: list[str] = []
    skipped_rows: list[dict] = []
    for sym in symbols:
        market_guard_reason = signal_pipeline.market_session_guard_for_symbol(sym, market_universe)
        if market_guard_reason:
            logger.info("Smart scan skipped %s: %s", sym, market_guard_reason)
            symbol_info = symbol_lookup.get(sym) or symbol_lookup.get(sym.upper()) or {}
            try:
                info_bid = float(symbol_info.get("bid") or 0.0) if isinstance(symbol_info, dict) else 0.0
            except Exception:
                info_bid = 0.0
            try:
                info_ask = float(symbol_info.get("ask") or 0.0) if isinstance(symbol_info, dict) else 0.0
            except Exception:
                info_ask = 0.0
            reference_price = ((info_bid + info_ask) / 2.0) if info_bid > 0.0 and info_ask > 0.0 else max(info_bid, info_ask, 0.0)
            cached_confidence, cached_strategy = _cached_scan_confidence(sym)
            fallback_confidence = cached_confidence if cached_confidence > 0.0 else 0.35
            fallback_quality = cached_confidence if cached_confidence > 0.0 else 0.35
            skipped_rows.append(
                {
                    "symbol": sym,
                    "signal": {
                        "action": "HOLD",
                        "confidence": fallback_confidence,
                        "stop_loss": 0.0,
                        "take_profit": 0.0,
                        "reason": market_guard_reason,
                        "strategy": cached_strategy or "market_guard_fallback",
                    },
                    "signal_id": None,
                    "risk_decision": {
                        "approved": False,
                        "reason": market_guard_reason,
                        "adjusted_volume": 0.0,
                        "warnings": [],
                        "status": "warn",
                        "machine_reasons": [market_guard_reason],
                        "metrics_snapshot": {},
                    },
                    "entry_price_estimate": reference_price,
                    "explanation": market_guard_reason,
                    "ready_to_execute": False,
                    "category": (symbol_lookup.get(sym) or symbol_lookup.get(sym.upper()) or {}).get("category") or "Other",
                    "description": (symbol_lookup.get(sym) or symbol_lookup.get(sym.upper()) or {}).get("description") or "",
                    "degraded_reasons": ["market_guard"],
                    "trade_quality": {
                        "final_trade_quality_score": fallback_quality,
                        "threshold": 1.0,
                        "no_trade_zone": True,
                        "no_trade_reasons": [market_guard_reason],
                        "summary": market_guard_reason,
                    },
                    "portfolio_risk": {
                        "status": "warn",
                        "allow_execute": False,
                        "reason": market_guard_reason,
                        "blocking_reasons": [market_guard_reason],
                        "warnings": [],
                        "metrics_snapshot": {},
                        "correlated_symbols": [],
                        "margin_required": 0.0,
                        "projected_margin_utilization_pct": 0.0,
                        "projected_free_margin_pct": 0.0,
                        "portfolio_fit_score": 0.0,
                    },
                    "anti_churn": {"blocked": False, "threshold_boost": 0.0, "reasons": [], "metadata": {}},
                    "gemini_confirmation": None,
                    "news": {
                        "sentiment": "neutral",
                        "impact": "low",
                        "relevance": "indirect",
                        "strength": 0.0,
                        "risk_flag": False,
                        "message": market_guard_reason,
                        "news_unavailable": True,
                    },
                    "execution_reason": market_guard_reason,
                    "recommended_amount_usd": 0.0,
                    "recommended_amount_pct_free_margin": 0.0,
                    "recommended_policy_mode": None,
                    "auto_execution_allowed_by_mode": False,
                    "auto_execution_skipped_reason": market_guard_reason,
                    **_merge_commission_snapshot(
                        _commission_snapshot_from_symbol_info(symbol_lookup.get(sym) or symbol_lookup.get(sym.upper()) or {}),
                        history_commission_map.get(sym),
                    ),
                }
            )
            continue
        open_symbols.append(sym)
    symbols = open_symbols[:MAX_SCAN_SYMBOLS]

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVALS)
    if symbols:
        min_required_total = (math.ceil(len(symbols) / max(1, MAX_CONCURRENT_EVALS)) * PER_SYMBOL_TIMEOUT_SECONDS) + 5.0
        TOTAL_SCAN_TIMEOUT_SECONDS = max(TOTAL_SCAN_TIMEOUT_SECONDS, min_required_total)

    def _scan_timeout_row(sym: str) -> dict:
        symbol_info = symbol_lookup.get(sym) or symbol_lookup.get(sym.upper()) or {}
        category = symbol_info.get("category") or "Other"
        description = symbol_info.get("description") or ""
        cached_confidence, cached_strategy = _cached_scan_confidence(sym)
        conservative_floor = max(
            0.35,
            min(0.9, float(risk_engine.settings.auto_trade_min_confidence or 0.45)),
        )
        fallback_quality = float(cached_confidence if cached_confidence > 0.0 else conservative_floor)
        timeout_reason = "Scan timed out for this symbol."
        if cached_confidence > 0.0:
            timeout_reason = (
                f"Scan timed out for this symbol. Reusing last confidence {cached_confidence:.0%}."
            )
        return {
            "symbol": sym,
            "signal": {
                "action": "HOLD",
                "confidence": cached_confidence,
                "stop_loss": 0.0,
                "take_profit": 0.0,
                "reason": timeout_reason,
                "strategy": cached_strategy or "timeout_fallback",
            },
            "signal_id": None,
            "risk_decision": {},
            "entry_price_estimate": 0.0,
            "explanation": timeout_reason,
            "ready_to_execute": False,
            "category": category,
            "description": description,
            "degraded_reasons": ["scan_timeout"],
            "trade_quality": {
                "final_trade_quality_score": fallback_quality,
                "threshold": float(risk_engine.settings.auto_trade_min_confidence or 0.45),
                "no_trade_zone": True,
                "no_trade_reasons": [timeout_reason],
                "summary": timeout_reason,
            },
            "portfolio_risk": {},
            "anti_churn": {},
            "gemini_confirmation": None,
            "news": {
                "sentiment": "neutral",
                "impact": "low",
                "relevance": "indirect",
                "strength": 0.0,
                "risk_flag": False,
                "message": timeout_reason,
                "news_unavailable": True,
            },
            "execution_reason": timeout_reason,
            "recommended_amount_usd": 0.0,
            "recommended_amount_pct_free_margin": 0.0,
            **_merge_commission_snapshot(
                _commission_snapshot_from_symbol_info(symbol_info),
                history_commission_map.get(sym),
            ),
        }

    async def evaluate_symbol(sym: str) -> dict | None:
        try:
            async with semaphore:
                execution_decision = await asyncio.wait_for(
                    signal_pipeline.evaluate(
                        symbol=sym,
                        requested_agent_name="SmartAgent",
                        requested_timeframe="H1",
                        evaluation_mode="scan",
                        bar_count=100,
                    ),
                    timeout=PER_SYMBOL_TIMEOUT_SECONDS,
                )
            try:
                signal_id = await asyncio.wait_for(
                    signal_pipeline.persist_evaluation(execution_decision, "multi"),
                    timeout=3.0,
                )
            except Exception:
                signal_id = None
            signal = execution_decision.signal_decision.final_signal
            risk_decision = execution_decision.risk_evaluation
            context = execution_decision.signal_decision.market_context
            entry_price = execution_decision.entry_price
            category = context.symbol_info.category if context.symbol_info else "Other"
            description = context.symbol_info.description if context.symbol_info else ""
            ready_to_execute = execution_decision.allow_execute
            execution_reason = execution_decision.reason
            auto_execution_result = None
            recommended_policy_mode = None
            auto_execution_allowed_by_mode = False
            auto_execution_skipped_reason = ""
            # Keep route-driven evaluation read-mostly by default. Background
            # auto-trader execution owns automatic order placement so repeated
            # dashboard/polling scans cannot place duplicate live orders.
            route_auto_execute_requested = bool(req.auto_execute and not req.manual)
            if (
                category == "Other"
                and "inactive in the current mode" in str(execution_reason).lower()
            ):
                return None

            preflight = None
            if ready_to_execute and signal.action in {"BUY", "SELL"}:
                preflight = await asyncio.wait_for(
                    execution_service.preflight_for_context_signal(
                        context=context,
                        candidate_signal=signal,
                        volume=max(risk_decision.adjusted_volume, risk_engine.settings.fixed_lot_size),
                        reference_spread=context.tick.get("spread", 0.0) if context.tick else 0.0,
                        evaluation_mode="scan",
                    ),
                    timeout=0.8,
                )
                if not preflight.approved:
                    ready_to_execute = False
                    execution_reason = preflight.reason
                else:
                    tick = market_data.get_tick(sym)
                    sym_info = market_data.get_symbol_info(sym)
                    dist_error = _sl_tp_distance_error(
                        action=signal.action,
                        tick=tick,
                        symbol_info=sym_info,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        volume=max(risk_decision.adjusted_volume, risk_engine.settings.fixed_lot_size),
                    )
                    if dist_error:
                        ready_to_execute = False
                        execution_reason = dist_error

            quality_score = float(
                execution_decision.trade_quality_assessment.final_trade_quality_score
                if execution_decision.trade_quality_assessment
                else signal.confidence
            )
            recommended_amount_usd, recommended_amount_pct = _recommended_trade_amount_from_free_margin(
                free_margin=float(context.account_free_margin or 0.0),
                policy_mode=str((context.user_policy or {}).get("mode", risk_engine.user_policy.mode)),
                confidence=float(signal.confidence or 0.0),
                quality_score=quality_score,
                ready_to_execute=ready_to_execute,
            )
            recommended_policy_mode = _recommended_policy_mode_for_setup(
                float(signal.confidence or 0.0),
                quality_score,
            )
            auto_execution_allowed_by_mode = bool(
                ready_to_execute
                and route_auto_execute_requested
                and risk_engine.auto_trade_enabled
                and _mode_allows_auto_execution(
                    str(getattr(risk_engine.user_policy, "mode", "balanced") or "balanced"),
                    recommended_policy_mode,
                )
            )
            if not ready_to_execute:
                auto_execution_skipped_reason = execution_reason
            elif not route_auto_execute_requested:
                auto_execution_skipped_reason = (
                    "Route auto execution is disabled unless explicitly requested."
                )
            elif not auto_execution_allowed_by_mode:
                auto_execution_skipped_reason = (
                    f"Current mode '{risk_engine.user_policy.mode}' is below the required "
                    f"'{recommended_policy_mode}' execution level."
                )

            if auto_execution_allowed_by_mode and signal.action in {"BUY", "SELL"} and preflight is not None:
                if float(signal.stop_loss or 0.0) <= 0.0 or float(signal.take_profit or 0.0) <= 0.0:
                    auto_execution_skipped_reason = "Auto execution skipped: missing stop-loss/take-profit targets."
                    auto_execution_allowed_by_mode = False
                else:
                    order_volume = max(
                        float(risk_decision.adjusted_volume or 0.0),
                        float(risk_engine.settings.fixed_lot_size or 0.0),
                    )
                    comment_amount_usd = _comment_amount_for_mt5_trade(
                        symbol=sym,
                        action=signal.action,
                        volume=order_volume,
                        fallback_amount_usd=recommended_amount_usd if recommended_amount_usd > 0 else None,
                    )
                    sym_info = market_data.get_symbol_info(sym) or {}
                    live_tick = market_data.get_tick(sym)
                    live_entry = (
                        float(getattr(live_tick, "ask", 0.0) if signal.action == "BUY" else getattr(live_tick, "bid", 0.0))
                        if live_tick
                        else float(entry_price or 0.0)
                    )
                    capped_sl, capped_tp, sl_amount_usd, tp_amount_usd = _cap_order_targets_by_money_budget(
                        action=signal.action,
                        entry_price=live_entry if live_entry > 0 else float(entry_price or 0.0),
                        stop_loss=float(signal.stop_loss or 0.0),
                        take_profit=float(signal.take_profit or 0.0),
                        symbol_info=sym_info,
                        volume=order_volume,
                        started_amount_usd=float(comment_amount_usd or 0.0),
                        account=connector.account_info,
                        fallback_equity=float(context.account_equity or 0.0),
                    )
                    if live_tick:
                        capped_sl, capped_tp = _clamp_sl_tp_to_broker_min_distance(
                            action=signal.action,
                            tick=live_tick,
                            symbol_info=sym_info,
                            stop_loss=capped_sl,
                            take_profit=capped_tp,
                        )
                        capped_sl, capped_tp, sl_amount_usd, tp_amount_usd = _cap_order_targets_by_money_budget(
                            action=signal.action,
                            entry_price=live_entry if live_entry > 0 else float(entry_price or 0.0),
                            stop_loss=float(capped_sl or 0.0),
                            take_profit=float(capped_tp or 0.0),
                            symbol_info=sym_info,
                            volume=order_volume,
                            started_amount_usd=float(comment_amount_usd or 0.0),
                            account=connector.account_info,
                            fallback_equity=float(context.account_equity or 0.0),
                        )
                    sl_cap_usd = _sl_cap_for_order(
                        started_amount_usd=float(comment_amount_usd or 0.0),
                        account=connector.account_info,
                        fallback_equity=float(context.account_equity or 0.0),
                    )
                    if (
                        sl_cap_usd > 0
                        and sl_amount_usd is not None
                        and sl_amount_usd > (sl_cap_usd * 1.05)
                    ):
                        vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
                        vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)
                        vol_max = float(sym_info.get("volume_max", 0.0) or 0.0)
                        scaled_volume = _round_volume_down(
                            raw_volume=order_volume * (sl_cap_usd / max(sl_amount_usd, 0.000001)),
                            vol_min=vol_min,
                            vol_step=vol_step,
                            vol_max=vol_max,
                        )
                        if scaled_volume >= vol_min and scaled_volume < order_volume:
                            order_volume = scaled_volume
                            comment_amount_usd = _comment_amount_for_mt5_trade(
                                symbol=sym,
                                action=signal.action,
                                volume=order_volume,
                                fallback_amount_usd=recommended_amount_usd if recommended_amount_usd > 0 else None,
                            )
                            capped_sl, capped_tp, sl_amount_usd, tp_amount_usd = _cap_order_targets_by_money_budget(
                                action=signal.action,
                                entry_price=live_entry if live_entry > 0 else float(entry_price or 0.0),
                                stop_loss=float(capped_sl or 0.0),
                                take_profit=float(capped_tp or 0.0),
                                symbol_info=sym_info,
                                volume=order_volume,
                                started_amount_usd=float(comment_amount_usd or 0.0),
                                account=connector.account_info,
                                fallback_equity=float(context.account_equity or 0.0),
                            )
                        else:
                            auto_execution_allowed_by_mode = False
                            auto_execution_skipped_reason = (
                                f"Auto execution skipped: broker minimum stop distance makes CFD SL risk "
                                f"${sl_amount_usd:.2f}, above cap ${sl_cap_usd:.2f}."
                            )
                    if not auto_execution_allowed_by_mode:
                        auto_execution_result = {
                            "success": False,
                            "retcode_desc": auto_execution_skipped_reason,
                            "stop_loss": capped_sl,
                            "take_profit": capped_tp,
                        }
                        commission_snapshot = _commission_snapshot_from_symbol_info(symbol_lookup.get(sym))
                        commission_snapshot = _merge_commission_snapshot(
                            commission_snapshot,
                            history_commission_map.get(sym),
                        )
                        result = {
                            "symbol": sym,
                            "signal": signal.to_trade_signal().model_dump(),
                            "signal_id": signal_id,
                            "risk_decision": risk_decision.model_dump(),
                            "entry_price_estimate": entry_price,
                            "explanation": signal.reason,
                            "ready_to_execute": ready_to_execute,
                            "category": category,
                            "description": description,
                            "degraded_reasons": execution_decision.signal_decision.degraded_reasons,
                            "trade_quality": execution_decision.trade_quality_assessment.model_dump(),
                            "portfolio_risk": execution_decision.portfolio_risk_assessment.model_dump(),
                            "anti_churn": execution_decision.anti_churn_assessment.model_dump(),
                            "gemini_confirmation": execution_decision.signal_decision.gemini_confirmation.model_dump()
                            if execution_decision.signal_decision.gemini_confirmation
                            else None,
                            "news": _news_payload_from_signal(signal.to_trade_signal().model_dump()),
                            "execution_reason": execution_reason,
                            "recommended_amount_usd": recommended_amount_usd,
                            "recommended_amount_pct_free_margin": recommended_amount_pct,
                            "recommended_policy_mode": recommended_policy_mode,
                            "auto_execution_allowed_by_mode": False,
                            "auto_execution_skipped_reason": auto_execution_skipped_reason,
                            "auto_execution_result": auto_execution_result,
                            **commission_snapshot,
                        }
                        _cache_scan_confidence(
                            sym,
                            float((result.get("signal") or {}).get("confidence", 0.0) or 0.0),
                            str((result.get("signal") or {}).get("strategy", "") or ""),
                        )
                        return result
                    comment = _build_trade_comment(
                        comment_amount_usd,
                        sl_amount_usd,
                        tp_amount_usd,
                    )
                    order_req = OrderRequest(
                        symbol=sym,
                        action=signal.action,
                        volume=order_volume,
                        stop_loss=float(capped_sl or 0.0),
                        take_profit=float(capped_tp or 0.0),
                        comment=comment,
                    )
                    order_result = execution_service.place_order_if_approved(order_req, preflight)
                    auto_execution_result = order_result.model_dump() if hasattr(order_result, "model_dump") else dict(order_result or {})
                    if bool(auto_execution_result.get("success")):
                        ready_to_execute = False
                        execution_reason = "Order submitted automatically."
                        auto_execution_skipped_reason = ""
                        if db is not None:
                            try:
                                await db.log_order(
                                    signal_id=signal_id,
                                    symbol=sym,
                                    action=signal.action,
                                    volume=float(order_req.volume),
                                    price=auto_execution_result.get("price"),
                                    stop_loss=order_req.stop_loss,
                                    take_profit=order_req.take_profit,
                                    ticket=auto_execution_result.get("ticket"),
                                    retcode=auto_execution_result.get("retcode", -1),
                                    retcode_desc=auto_execution_result.get("retcode_desc", ""),
                                    success=True,
                                    comment=comment,
                                )
                                if signal_id:
                                    await db.mark_evaluation_outcome(signal_id, "opened")
                                ticket = auto_execution_result.get("ticket")
                                if ticket:
                                    await _store_management_plan(
                                        int(ticket),
                                        signal_id,
                                        sym,
                                        signal.action,
                                        execution_decision,
                                    )
                            except Exception as exc:
                                logger.warning("Auto-executed scan order persistence failed for %s: %s", sym, exc)
                    else:
                        auto_execution_skipped_reason = str(
                            auto_execution_result.get("retcode_desc")
                            or auto_execution_result.get("comment")
                            or "Auto execution was not approved."
                        )

            commission_snapshot = _commission_snapshot_from_symbol_info(symbol_lookup.get(sym))
            commission_snapshot = _merge_commission_snapshot(
                commission_snapshot,
                history_commission_map.get(sym),
            )

            result = {
                "symbol": sym,
                "signal": signal.to_trade_signal().model_dump(),
                "signal_id": signal_id,
                "risk_decision": risk_decision.model_dump(),
                "entry_price_estimate": entry_price,
                "explanation": signal.reason,
                "ready_to_execute": ready_to_execute,
                "category": category,
                "description": description,
                "degraded_reasons": execution_decision.signal_decision.degraded_reasons,
                "trade_quality": execution_decision.trade_quality_assessment.model_dump(),
                "portfolio_risk": execution_decision.portfolio_risk_assessment.model_dump(),
                "anti_churn": execution_decision.anti_churn_assessment.model_dump(),
                "gemini_confirmation": execution_decision.signal_decision.gemini_confirmation.model_dump()
                if execution_decision.signal_decision.gemini_confirmation
                else None,
                "news": _news_payload_from_signal(signal.to_trade_signal().model_dump()),
                "execution_reason": execution_reason,
                "recommended_amount_usd": recommended_amount_usd,
                "recommended_amount_pct_free_margin": recommended_amount_pct,
                "recommended_policy_mode": recommended_policy_mode,
                "auto_execution_allowed_by_mode": auto_execution_allowed_by_mode,
                "auto_execution_skipped_reason": auto_execution_skipped_reason,
                "auto_execution_result": auto_execution_result,
                **commission_snapshot,
            }
            _cache_scan_confidence(
                sym,
                float((result.get("signal") or {}).get("confidence", 0.0) or 0.0),
                str((result.get("signal") or {}).get("strategy", "") or ""),
            )
            return result
        except Exception as exc:
            logger.error("Smart evaluate error for %s: %r", sym, exc, exc_info=True)
            symbol_info = symbol_lookup.get(sym, {})
            category = symbol_info.get("category") or "Other"
            description = symbol_info.get("description") or ""
            reason = str(exc) or "Scan failed for this symbol."
            timeout_like = isinstance(exc, TimeoutError) or "timeout" in reason.lower()
            degraded_reason = "scan_timeout" if timeout_like else "scan_failed"
            hold_reason = (
                f"Scan timeout after {int(PER_SYMBOL_TIMEOUT_SECONDS)}s"
                if timeout_like
                else f"Symbol scan failed: {reason}"
            )
            cached_confidence, cached_strategy = _cached_scan_confidence(sym)
            conservative_floor = max(
                0.35,
                min(0.9, float(risk_engine.settings.auto_trade_min_confidence or 0.45)),
            )
            fallback_quality = float(cached_confidence if cached_confidence > 0.0 else conservative_floor)
            if cached_confidence > 0.0 and timeout_like:
                hold_reason = (
                    f"{hold_reason}. Reusing last confidence {cached_confidence:.0%}."
                )
            return {
                "symbol": sym,
                "signal": {
                    "action": "HOLD",
                    "confidence": cached_confidence,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "reason": hold_reason,
                    "strategy": cached_strategy or "scan_error_fallback",
                },
                "signal_id": None,
                "risk_decision": {
                    "approved": False,
                    "reason": hold_reason,
                    "adjusted_volume": 0.0,
                    "warnings": [],
                    "status": "warn",
                    "machine_reasons": [hold_reason],
                    "metrics_snapshot": {},
                },
                "entry_price_estimate": 0.0,
                "explanation": hold_reason,
                "ready_to_execute": False,
                "category": category,
                "description": description,
                "degraded_reasons": [degraded_reason],
                "trade_quality": {
                    "final_trade_quality_score": fallback_quality,
                    "threshold": float(risk_engine.settings.auto_trade_min_confidence or 0.45),
                    "no_trade_zone": True,
                    "no_trade_reasons": [hold_reason],
                    "summary": hold_reason,
                },
                "portfolio_risk": {
                    "status": "warn",
                    "allow_execute": False,
                    "reason": hold_reason,
                    "blocking_reasons": [hold_reason],
                    "warnings": [],
                    "metrics_snapshot": {},
                    "correlated_symbols": [],
                    "margin_required": 0.0,
                    "projected_margin_utilization_pct": 0.0,
                    "projected_free_margin_pct": 0.0,
                    "portfolio_fit_score": 0.0,
                },
                "anti_churn": {"blocked": False, "threshold_boost": 0.0, "reasons": [], "metadata": {}},
                "gemini_confirmation": None,
                "news": {
                    "sentiment": "neutral",
                    "impact": "low",
                    "relevance": "indirect",
                    "strength": 0.0,
                    "risk_flag": False,
                    "message": hold_reason,
                    "news_unavailable": True,
                },
                "execution_reason": hold_reason,
                "recommended_amount_usd": 0.0,
                "recommended_amount_pct_free_margin": 0.0,
                "recommended_policy_mode": None,
                "auto_execution_allowed_by_mode": False,
                "auto_execution_skipped_reason": hold_reason,
                **_merge_commission_snapshot(
                    _commission_snapshot_from_symbol_info(symbol_info),
                    history_commission_map.get(sym),
                ),
            }

    tasks = [asyncio.create_task(evaluate_symbol(sym)) for sym in symbols]
    if not tasks:
        recommendations = list(skipped_rows)
        return {"recommendations": recommendations}
    task_symbol = {task: sym for task, sym in zip(tasks, symbols)}
    done, pending = await asyncio.wait(tasks, timeout=TOTAL_SCAN_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning(
            "Smart evaluate timed out for %d/%d symbols after %.1fs; returning partial results.",
            len(pending),
            len(tasks),
            TOTAL_SCAN_TIMEOUT_SECONDS,
        )
    evaluations = []
    for task in done:
        try:
            evaluations.append(task.result())
        except Exception as exc:
            logger.error("Smart evaluate task failed: %r", exc, exc_info=True)
            evaluations.append(None)
    for task in pending:
        sym = task_symbol.get(task, "")
        if sym:
            evaluations.append(_scan_timeout_row(sym))
    recommendations = [item for item in evaluations if item is not None]
    if skipped_rows:
        recommendations.extend(skipped_rows)

    # Second-pass Gemini enrichment for actionable rows only.
    # Keep this bounded so dashboard scan remains responsive and quota-safe.
    global _last_scan_gemini_enrich_at
    role = str(risk_engine.user_policy.gemini_role or "advisory").lower()
    gemini_status = _current_gemini_status()
    now_ts = time.time()
    GEMINI_SCAN_ENRICH_MIN_INTERVAL_SECONDS = 60.0
    can_enrich_scan_now = (
        role != "off"
        and str(gemini_status.get("state", "")).lower() == "available"
        and (now_ts - float(_last_scan_gemini_enrich_at or 0.0)) >= GEMINI_SCAN_ENRICH_MIN_INTERVAL_SECONDS
    )
    if can_enrich_scan_now:
        actionable_indexes = [
            idx for idx, rec in enumerate(recommendations)
            if str((rec.get("signal") or {}).get("action", "")).upper() in {"BUY", "SELL"}
        ]
        MAX_GEMINI_CONFIRMATIONS = 2
        GEMINI_CONFIRM_TIMEOUT_SECONDS = 3.0
        GEMINI_CONFIRM_CONCURRENCY = 1
        actionable_indexes = actionable_indexes[:MAX_GEMINI_CONFIRMATIONS]
        if actionable_indexes:
            _last_scan_gemini_enrich_at = now_ts
            confirm_sem = asyncio.Semaphore(GEMINI_CONFIRM_CONCURRENCY)

            async def enrich_with_gemini(rec_index: int) -> tuple[int, dict | None]:
                rec = recommendations[rec_index]
                sym = str(rec.get("symbol") or "")
                if not sym:
                    return rec_index, None
                try:
                    async with confirm_sem:
                        decision = await asyncio.wait_for(
                            signal_pipeline.evaluate(
                                symbol=sym,
                                requested_agent_name="GeminiAgent",
                                requested_timeframe="H1",
                                evaluation_mode="manual",
                                bar_count=100,
                            ),
                            timeout=GEMINI_CONFIRM_TIMEOUT_SECONDS,
                        )
                except Exception as exc:
                    logger.info("Gemini confirm pass skipped for %s: %s", sym, exc)
                    return rec_index, None

                signal = decision.signal_decision.final_signal
                risk_decision = decision.risk_evaluation
                context = decision.signal_decision.market_context
                quality_score = float(
                    decision.trade_quality_assessment.final_trade_quality_score
                    if decision.trade_quality_assessment
                    else signal.confidence
                )
                recommended_amount_usd, recommended_amount_pct = _recommended_trade_amount_from_free_margin(
                    free_margin=float(context.account_free_margin or 0.0),
                    policy_mode=str((context.user_policy or {}).get("mode", risk_engine.user_policy.mode)),
                    confidence=float(signal.confidence or 0.0),
                    quality_score=quality_score,
                    ready_to_execute=bool(decision.allow_execute),
                )
                patched = {
                    "signal": signal.to_trade_signal().model_dump(),
                    "risk_decision": risk_decision.model_dump(),
                    "entry_price_estimate": decision.entry_price,
                    "explanation": signal.reason,
                    "ready_to_execute": bool(decision.allow_execute),
                    "degraded_reasons": decision.signal_decision.degraded_reasons,
                    "trade_quality": decision.trade_quality_assessment.model_dump(),
                    "portfolio_risk": decision.portfolio_risk_assessment.model_dump(),
                    "anti_churn": decision.anti_churn_assessment.model_dump(),
                    "gemini_confirmation": decision.signal_decision.gemini_confirmation.model_dump()
                    if decision.signal_decision.gemini_confirmation
                    else None,
                    "news": _news_payload_from_signal(signal.to_trade_signal().model_dump()),
                    "execution_reason": decision.reason,
                    "recommended_amount_usd": recommended_amount_usd,
                    "recommended_amount_pct_free_margin": recommended_amount_pct,
                }
                return rec_index, patched

            enrichment_tasks = [asyncio.create_task(enrich_with_gemini(i)) for i in actionable_indexes]
            done_enrich, pending_enrich = await asyncio.wait(
                enrichment_tasks,
                timeout=(GEMINI_CONFIRM_TIMEOUT_SECONDS * max(1, len(actionable_indexes))),
            )
            for task in pending_enrich:
                task.cancel()
            for task in done_enrich:
                try:
                    rec_index, patched = task.result()
                    if patched is not None:
                        recommendations[rec_index].update(patched)
                except Exception as exc:
                    logger.info("Gemini confirm merge failed: %s", exc)

    recommendations = [_decorate_recommendation_mode_fit(rec) for rec in recommendations]

    recommendations.sort(
        key=lambda r: (
            r["signal"]["confidence"],
            r.get("trade_quality", {}).get("final_trade_quality_score", 0.0),
            r["ready_to_execute"],
        ),
        reverse=True,
    )

    return {
        "recommendations": recommendations,
        "scanned_at": time.time(),
    }


class ExecuteRecommendationRequest(BaseModel):
    signal_id: int
    amount_usd: Optional[float] = None
    custom_stop_loss: Optional[float] = None  # Override AI's stop loss
    custom_take_profit: Optional[float] = None  # Override AI's take profit
    idempotency_key: str = ""


@router.post("/trade/execute-recommendation")
async def execute_recommendation(req: ExecuteRecommendationRequest, authorization: Optional[str] = Header(default=None)):
    """Execute a trade from a previously generated recommendation by signal_id."""
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    _raise_if_kill_switch_active()
    await _require_order_execution_permission(authorization, execution_mode=config.EXECUTION_MODE)
    _enforce_risk_profile_execution_caps()
    account_key = _current_account_key()
    execution_context = "recommendation"
    idempotency_key = str(req.idempotency_key or "").strip()
    existing_order = await _existing_idempotent_order(
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
    )
    if existing_order:
        return _order_row_api_result(existing_order)
    if active_platform == "ibkr":
        if risk_engine.panic_stopped:
            raise HTTPException(status_code=403, detail="Panic stop is active")
        if db is None:
            raise HTTPException(status_code=500, detail="Database not available")
        signal_data = await db.get_signal_by_id(req.signal_id)
        if signal_data is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        action = str(signal_data.get("action", "HOLD")).upper()
        if action not in {"BUY", "SELL"}:
            return {
                "success": False,
                "retcode": -1,
                "retcode_desc": signal_data.get("reason") or "Signal is not executable.",
                "ticket": None,
                "volume": None,
                "price": None,
                "stop_loss": signal_data.get("stop_loss"),
                "take_profit": signal_data.get("take_profit"),
                "comment": "",
            }
        symbol = signal_data["symbol"]
        quantity = float(risk_engine.settings.fixed_lot_size or 1.0)
        if req.amount_usd and req.amount_usd > 0:
            px = await _ibkr_reference_price(symbol)
            if px > 0:
                quantity = max(1.0, round(req.amount_usd / px, 4))
        sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else signal_data.get("stop_loss")
        tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else signal_data.get("take_profit")
        result = await _run_ibkr(
            ibkr_connector.place_order,
            symbol,
            action,
            quantity,
            sl,
            tp,
            _build_trade_comment(req.amount_usd, None, None),
        )
        async def _persist_ibkr_execute_reco_async():
            try:
                await db.log_order(
                    signal_id=req.signal_id,
                    symbol=symbol,
                    action=action,
                    volume=quantity,
                    price=result.get("price"),
                    stop_loss=sl,
                    take_profit=tp,
                    ticket=result.get("ticket"),
                    retcode=result.get("retcode", -1),
                    retcode_desc=result.get("retcode_desc", ""),
                    success=bool(result.get("success")),
                    comment=result.get("comment", ""),
                    idempotency_key=idempotency_key,
                    account_key=account_key,
                    execution_context=execution_context,
                    execution_mode=order_req.execution_mode,
                )
            except Exception as exc:
                logger.warning("IBKR execute-recommendation order persistence failed for %s: %s", symbol, exc)
            if result.get("success"):
                try:
                    await db.mark_evaluation_outcome(req.signal_id, "opened")
                except Exception as exc:
                    logger.warning("IBKR execute-recommendation outcome persistence failed for %s: %s", symbol, exc)

        asyncio.create_task(_persist_ibkr_execute_reco_async())
        return result
    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Panic stop is active")
    if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(
            status_code=403,
            detail="Live trading requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
        )

    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")

    # Look up the signal
    signal_data = await db.get_signal_by_id(req.signal_id)
    if signal_data is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    symbol = signal_data["symbol"]
    requested_agent_name = _requested_agent_from_final_name(signal_data.get("agent_name", "SmartAgent"))
    action = str(signal_data.get("action", "")).upper()
    if action not in ("BUY", "SELL"):
        return {
            "success": False,
            "retcode": -1,
            "retcode_desc": signal_data.get("reason") or "Signal is no longer executable.",
            "ticket": None,
            "volume": None,
            "price": None,
            "stop_loss": signal_data.get("stop_loss"),
            "take_profit": signal_data.get("take_profit"),
            "comment": "",
        }

    current_signal = TechnicalSignal(
        agent_name=signal_data.get("agent_name", "SmartAgent"),
        action=action,
        confidence=float(signal_data.get("confidence", 0.0) or 0.0),
        stop_loss=signal_data.get("stop_loss"),
        take_profit=signal_data.get("take_profit"),
        max_holding_minutes=signal_data.get("max_holding_minutes"),
        reason=signal_data.get("reason", ""),
        strategy="recommendation_snapshot",
        metadata={"source": "signal_snapshot"},
    )

    sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else current_signal.stop_loss
    tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else current_signal.take_profit
    volume = risk_engine.settings.fixed_lot_size
    sl_amount_for_comment: float | None = None
    tp_amount_for_comment: float | None = None
    effective_amount_usd = float(req.amount_usd or 0.0)
    if effective_amount_usd > 0:
        effective_amount_usd = _effective_trade_margin_budget(effective_amount_usd)
        if effective_amount_usd <= 0:
            raise HTTPException(
                status_code=409,
                detail="Custom budget headroom is exhausted. Increase budget or close existing trades first.",
            )
        if effective_amount_usd + 1e-9 < float(req.amount_usd or 0.0):
            logger.info(
                "ExecuteRecommendation amount clamped by custom budget safeguards: requested=%.2f effective=%.2f mode=%s",
                float(req.amount_usd or 0.0),
                float(effective_amount_usd),
                str(getattr(risk_engine, "auto_trade_budget_mode", "full_margin") or "full_margin"),
            )

    # Convert dollar amount to volume if provided
    tick = market_data.get_tick(symbol)
    if not tick:
        raise HTTPException(status_code=404, detail=f"Cannot get live symbol context for {symbol}")
    if not _is_fresh_tick(tick):
        raise HTTPException(status_code=404, detail=f"No fresh tick data for {symbol}")
    sym_info = market_data.get_symbol_info(symbol)
    if not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get live symbol context for {symbol}")
    context = _build_fast_execution_context(
        symbol=symbol,
        tick=tick,
        symbol_info=sym_info,
        evaluation_mode="manual_fast",
    )
    if context is None:
        raise HTTPException(status_code=404, detail=f"No live context available for {symbol}")
    decision_for_targets = None
    if effective_amount_usd > 0 and sym_info and tick:
        try:
            decision_for_targets = await asyncio.wait_for(
                signal_pipeline.evaluate(
                    symbol=symbol,
                    requested_agent_name=requested_agent_name,
                    requested_timeframe=str(signal_data.get("timeframe", "H1") or "H1"),
                    evaluation_mode="manual_fast",
                    bar_count=60,
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Dynamic SL/TP context timed out for recommendation %s; using fast defaults.", symbol)
        except Exception as exc:
            logger.warning("Could not build dynamic SL/TP context for recommendation %s: %s", symbol, exc)

        price = tick.ask if action == "BUY" else tick.bid
        contract_size = sym_info.get("trade_contract_size", 100000)
        vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
        vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)
        vol_max = float(sym_info.get("volume_max", 0.0) or 0.0)
        if price > 0 and contract_size > 0:
            volume, estimated_margin = _solve_volume_by_margin_target_fast(
                symbol=symbol,
                action=action,
                target_margin_usd=effective_amount_usd,
                price=price,
                contract_size=contract_size,
                vol_min=vol_min,
                vol_step=vol_step,
                vol_max=vol_max,
            )
            if volume <= 0:
                min_required = estimated_margin if estimated_margin is not None else 0.0
                raise HTTPException(
                    status_code=400,
                    detail=f"Amount too small for this symbol. Minimum required margin is about ${min_required:.2f}.",
                )
            logger.info(
                "Converted $%s target margin to %s lots for %s (estimated margin=%s)",
                req.amount_usd,
                volume,
                symbol,
                round(float(estimated_margin), 2) if estimated_margin is not None else None,
            )
            if req.custom_stop_loss is None or req.custom_take_profit is None:
                sl_amount, tp_amount = await _derive_dynamic_amount_targets(
                    amount_usd=effective_amount_usd,
                    action=action,
                    symbol=symbol,
                    decision=decision_for_targets,
                )
                sl_amount_for_comment = sl_amount
                tp_amount_for_comment = tp_amount
                default_sl, default_tp = _derive_amount_based_sl_tp(
                    action=action,
                    entry_price=price,
                    volume=volume,
                    symbol_info=sym_info,
                    sl_amount_usd=sl_amount,
                    tp_amount_usd=tp_amount,
                    digits=sym_info.get("digits", 5),
                )
                if req.custom_stop_loss is None:
                    sl = default_sl
                if req.custom_take_profit is None:
                    tp = default_tp

    entry_ref_price = tick.ask if action == "BUY" else tick.bid
    policy_risk_budget = _policy_risk_budget_usd(decision_for_targets)
    if effective_amount_usd > 0 and tick and sym_info:
        if sl_amount_for_comment is None and sl is not None and sl > 0:
            sl_amount_for_comment = _price_distance_to_usd(
                symbol_info=sym_info,
                entry_price=entry_ref_price,
                target_price=sl,
                volume=volume,
            )
        if tp_amount_for_comment is None and tp is not None and tp > 0:
            tp_amount_for_comment = _price_distance_to_usd(
                symbol_info=sym_info,
                entry_price=entry_ref_price,
                target_price=tp,
                volume=volume,
            )

    # Ensure SL/TP remains valid at execution-time prices right before preflight.
    sl, tp = _clamp_sl_tp_to_broker_min_distance(
        action=action,
        tick=tick,
        symbol_info=sym_info,
        stop_loss=sl,
        take_profit=tp,
    )
    required_rr = max(1.2, float((context.user_policy or {}).get("min_reward_risk", risk_engine.user_policy.min_reward_risk or 1.2)))
    tp = _adjust_tp_for_min_rr(
        action=action,
        entry_price=entry_ref_price,
        stop_loss=sl,
        take_profit=tp,
        required_rr=required_rr,
        digits=int(sym_info.get("digits", 5) if sym_info else 5),
    )
    # Final SL/TP refresh at send-time to avoid last-tick invalid-stops rejections.
    latest_tick = market_data.get_tick(symbol)
    if latest_tick and not _is_fresh_tick(latest_tick):
        latest_tick = None
    if latest_tick:
        sl, tp = _clamp_sl_tp_to_broker_min_distance(
            action=action,
            tick=latest_tick,
            symbol_info=sym_info,
            stop_loss=sl,
            take_profit=tp,
        )
        latest_price = latest_tick.ask if action == "BUY" else latest_tick.bid
        tp = _adjust_tp_for_min_rr(
            action=action,
            entry_price=latest_price,
            stop_loss=sl,
            take_profit=tp,
            required_rr=required_rr,
            digits=int(sym_info.get("digits", 5) if sym_info else 5),
        )
    if (sl is None or float(sl or 0.0) <= 0.0) or (tp is None or float(tp or 0.0) <= 0.0):
        fallback_sl, fallback_tp = _derive_pct_based_sl_tp_fallback(
            action=action,
            entry_price=(latest_tick.ask if action == "BUY" else latest_tick.bid) if latest_tick else entry_ref_price,
            symbol_info=sym_info,
            min_rr=required_rr,
            digits=int(sym_info.get("digits", 5) if sym_info else 5),
        )
        if float(sl or 0.0) <= 0.0:
            sl = fallback_sl
        if float(tp or 0.0) <= 0.0:
            tp = fallback_tp

    if req.amount_usd and req.amount_usd > 0 and sym_info:
        desired_sl_amount = sl_amount_for_comment if sl_amount_for_comment and sl_amount_for_comment > 0 else None
        if policy_risk_budget is not None and policy_risk_budget > 0:
            desired_sl_amount = (
                min(desired_sl_amount, policy_risk_budget)
                if desired_sl_amount is not None and desired_sl_amount > 0
                else policy_risk_budget
            )
        adjusted_volume, rebalance_error = _rebalance_volume_for_started_amount_risk(
            entry_price=entry_ref_price,
            stop_loss=sl,
            desired_sl_amount_usd=desired_sl_amount,
            volume=volume,
            contract_size=_usd_per_price_distance(symbol_info=sym_info, volume=1.0),
            vol_min=float(sym_info.get("volume_min", 0.01) or 0.01),
            vol_step=float(sym_info.get("volume_step", 0.01) or 0.01),
            vol_max=float(sym_info.get("volume_max", 0.0) or 0.0),
        )
        if rebalance_error:
            raise HTTPException(status_code=409, detail=rebalance_error)
        if adjusted_volume > 0 and adjusted_volume < volume:
            volume = adjusted_volume
    candidate_signal = current_signal.model_copy(
        update={
            "stop_loss": sl,
            "take_profit": tp,
        }
    )

    preflight = await execution_service.preflight_for_context_signal(
        context=context,
        candidate_signal=candidate_signal,
        volume=volume,
        reference_spread=context.tick.get("spread", 0.0) if context.tick else 0.0,
        evaluation_mode="scan",
    )
    if not preflight.approved:
        raise HTTPException(status_code=409, detail=preflight.reason)

    if preflight.risk_approval and preflight.risk_approval.risk_evaluation.adjusted_volume > 0:
        approved_volume = preflight.risk_approval.risk_evaluation.adjusted_volume
        if effective_amount_usd > 0:
            # Preserve started-amount SL/TP semantics; never scale above user-sized volume.
            volume = min(volume, approved_volume)
        else:
            volume = approved_volume

    comment_price_ref = (
        (latest_tick.ask if action == "BUY" else latest_tick.bid)
        if latest_tick is not None
        else entry_ref_price
    )
    if sl is not None and sl > 0:
        sl_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=comment_price_ref,
            target_price=sl,
            volume=volume,
        )
    if tp is not None and tp > 0:
        tp_amount_for_comment = _price_distance_to_usd(
            symbol_info=sym_info,
            entry_price=comment_price_ref,
            target_price=tp,
            volume=volume,
        )

    comment_amount_usd = _comment_amount_for_mt5_trade(
        symbol=symbol,
        action=action,
        volume=volume,
        fallback_amount_usd=effective_amount_usd,
    )
    amt_label = _build_trade_comment(comment_amount_usd, sl_amount_for_comment, tp_amount_for_comment)
    order_req = OrderRequest(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=sl or 0.0,
        take_profit=tp or 0.0,
        comment=amt_label,
        idempotency_key=idempotency_key,
        account_key=account_key,
        execution_context=execution_context,
        execution_mode=config.EXECUTION_MODE,
    )
    execution_trace = _build_execution_trace_metadata(
        source="execute_recommendation",
        symbol=symbol,
        action=action,
        signal_id=req.signal_id,
        reason=str(signal_data.get("reason") or "").strip() or str(preflight.reason or "").strip(),
        requested_agent_name=requested_agent_name,
        amount_usd=req.amount_usd,
        preflight_reason=str(preflight.reason or "").strip(),
        signal_snapshot={
            "agent_name": signal_data.get("agent_name"),
            "confidence": signal_data.get("confidence"),
            "reason": signal_data.get("reason"),
            "stop_loss": sl,
            "take_profit": tp,
        },
    )
    result = execution_service.place_order_if_approved(order_req, preflight)

    if db:
        async def _persist_execute_reco_async():
            try:
                await db.log_order(
                    signal_id=req.signal_id, symbol=symbol, action=action,
                    volume=order_req.volume, price=result.price,
                    stop_loss=sl, take_profit=tp, ticket=result.ticket,
                    retcode=result.retcode, retcode_desc=result.retcode_desc,
                    success=result.success,
                    comment=order_req.comment,
                    idempotency_key=idempotency_key,
                    account_key=account_key,
                    execution_context=execution_context,
                    execution_mode=order_req.execution_mode,
                )
            except Exception as exc:
                logger.warning("Execute-recommendation order persistence failed for %s: %s", symbol, exc)

            if result.success and result.ticket:
                auto_trader.position_manager.enable_auto_portfolio_targets()
                try:
                    await db.log_ai_activity(
                        action="recommendation_execute",
                        symbol=symbol,
                        ticket=result.ticket,
                        detail=f"{action} opened from saved recommendation #{req.signal_id}.",
                        signal_id=req.signal_id,
                        decision_reason=execution_trace.get("reason", ""),
                        gemini_summary=json.dumps(execution_trace.get("gemini") or {}),
                        meta_model_summary=json.dumps(execution_trace.get("meta_model") or {}),
                    )
                except Exception as exc:
                    logger.warning("Execute-recommendation AI activity persistence failed for %s #%s: %s", symbol, result.ticket, exc)
                try:
                    await db.log_position_change(
                        result.ticket, "opened", symbol,
                        f"{action} {order_req.volume} lots at {result.price}"
                    )
                except Exception as exc:
                    logger.warning("Execute-recommendation position-change persistence failed for %s #%s: %s", symbol, result.ticket, exc)

                async def _store_plan_async(ticket: int, sid: int, sym: str, side: str, timeframe: str):
                    try:
                        decision = await signal_pipeline.evaluate(
                            symbol=sym,
                            requested_agent_name=requested_agent_name,
                            requested_timeframe=timeframe,
                            evaluation_mode="manual",
                            bar_count=100,
                        )
                        await _store_management_plan(
                            ticket,
                            sid,
                            sym,
                            side,
                            decision,
                            execution_trace=execution_trace,
                        )
                    except Exception as exc:
                        logger.warning("Background plan generation failed for %s #%s: %s", sym, ticket, exc)
                        try:
                            await _store_execution_trace_fallback_plan(
                                ticket,
                                sid,
                                sym,
                                side,
                                execution_trace=execution_trace,
                            )
                        except Exception as fallback_exc:
                            logger.warning("Fallback execution-trace plan persistence failed for %s #%s: %s", sym, ticket, fallback_exc)

                asyncio.create_task(
                    _store_plan_async(
                        result.ticket,
                        req.signal_id,
                        symbol,
                        action,
                        signal_data.get("timeframe", "H1"),
                    )
                )
                try:
                    await db.mark_evaluation_outcome(req.signal_id, "opened")
                except Exception as exc:
                    logger.warning("Execute-recommendation outcome persistence failed for %s #%s: %s", symbol, result.ticket, exc)
                try:
                    account = connector.refresh_account() if connector.connected else None
                    await db.mark_trade_candidate_execution(
                        signal_id=req.signal_id,
                        executed=True,
                        ticket=result.ticket,
                        fill_price=result.price,
                        slippage_estimate=0.0,
                        margin_snapshot={
                            "balance": float(account.balance) if account else 0.0,
                            "equity": float(account.equity) if account else 0.0,
                            "margin": float(account.margin) if account else 0.0,
                            "free_margin": float(account.free_margin) if account else 0.0,
                        },
                    )
                except Exception as exc:
                    logger.warning("Execute-recommendation post-open persistence failed for %s #%s: %s", symbol, result.ticket, exc)

        asyncio.create_task(_persist_execute_reco_async())

    return result.model_dump()


class ReplayRequest(BaseModel):
    symbol: str
    steps: int = 20
    with_gemini: bool = True


@router.post("/replay/run")
async def run_replay(req: ReplayRequest):
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    if active_platform == "ibkr":
        raise HTTPException(
            status_code=409,
            detail="Replay currently supports MT5 data pipeline only.",
        )

    steps = max(5, min(req.steps, 60))
    m15_bars = [b.model_dump() for b in market_data.get_bars(req.symbol, "M15", 140 + steps)]
    h1_bars = [b.model_dump() for b in market_data.get_bars(req.symbol, "H1", 140 + steps)]
    h4_bars = [b.model_dump() for b in market_data.get_bars(req.symbol, "H4", 140 + steps)]
    if len(m15_bars) < 120 or len(h1_bars) < 120:
        raise HTTPException(status_code=400, detail="Not enough historical bars to run replay.")

    account = connector.refresh_account()
    symbol_info = signal_pipeline._normalize_symbol_info(req.symbol, {
        "bid": m15_bars[-1]["close"],
        "ask": m15_bars[-1]["close"],
        "spread": 5,
    })
    contexts = []
    for offset in range(steps, 0, -1):
        m15_slice = m15_bars[: len(m15_bars) - offset]
        h1_slice = h1_bars[: len(h1_bars) - min(offset // 4, len(h1_bars) - 1)]
        h4_slice = h4_bars[: len(h4_bars) - min(offset // 16, len(h4_bars) - 1)]
        last_close = m15_slice[-1]["close"]
        profile = signal_pipeline.profile_service.resolve_profile(req.symbol, symbol_info) if symbol_info else None
        contexts.append(MarketContext(
            symbol=req.symbol,
            requested_timeframe="H1",
            evaluation_mode="replay",
            symbol_info=symbol_info,
            profile=profile,
            tick={"bid": last_close, "ask": last_close, "spread": 5},
            account_balance=account.balance if account else 0.0,
            account_equity=account.equity if account else 0.0,
            account_margin=account.margin if account else 0.0,
            account_free_margin=account.free_margin if account else 0.0,
            account_currency=account.currency if account else "",
            account_leverage=account.leverage if account else 100,
            bars_by_timeframe={"M15": m15_slice[-100:], "H1": h1_slice[-100:], "H4": h4_slice[-100:]},
            symbol_open_positions=[],
            all_open_positions=[],
        ))

    replay_result = await replay_service.run_contexts(
        contexts,
        requested_agent_name="GeminiAgent" if req.with_gemini else "SmartAgent",
        with_gemini=req.with_gemini,
    )

    simulated = []
    for index, context in enumerate(contexts):
        decision = await signal_pipeline.evaluate_context(
            context=context,
            requested_agent_name="GeminiAgent" if req.with_gemini else "SmartAgent",
            scan_window_id=f"replay-sim:{index}",
        )
        future_bars = m15_bars[-(index + 12):] if len(m15_bars) > 12 else m15_bars
        simulated.append(replay_service.simulate_outcome(decision, future_bars))

    pnl = sum(item.get("profit", 0.0) for item in simulated)
    return {
        **replay_result,
        "symbol": req.symbol,
        "steps": steps,
        "simulated_outcomes": simulated,
        "simulated_pnl": round(pnl, 5),
    }


def _analytics_profit(outcome: dict) -> float:
    for key in ("profit", "profit_usd", "pnl", "net_pnl"):
        try:
            if outcome.get(key) is not None:
                return float(outcome.get(key) or 0.0)
        except Exception:
            return 0.0
    return 0.0


def _analytics_bucket_rows(outcomes: list[dict], bucket_fn) -> dict:
    buckets: dict[str, dict] = {}
    for row in outcomes:
        key = str(bucket_fn(row) or "unknown")
        item = buckets.setdefault(
            key,
            {
                "bucket": key,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "net_pnl": 0.0,
                "avg_pnl": 0.0,
                "win_rate": 0.0,
            },
        )
        pnl = _analytics_profit(row)
        item["trades"] += 1
        item["net_pnl"] += pnl
        if pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1
    for item in buckets.values():
        trades = max(int(item["trades"]), 1)
        item["avg_pnl"] = item["net_pnl"] / trades
        item["win_rate"] = item["wins"] / trades
    return dict(sorted(buckets.items(), key=lambda kv: (-kv[1]["trades"], kv[0])))


def _analytics_outcome_json(row: dict) -> dict:
    return _as_dict_json(row.get("outcome_json"))


def _confidence_bucket(row: dict) -> str:
    try:
        confidence = float(row.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    if confidence >= 0.80:
        return "80_100"
    if confidence >= 0.65:
        return "65_80"
    if confidence >= 0.50:
        return "50_65"
    if confidence > 0:
        return "0_50"
    return "unknown"


def _session_bucket(row: dict) -> str:
    payload = _analytics_outcome_json(row)
    explicit = str(payload.get("session") or payload.get("market_session") or "").strip()
    if explicit:
        return explicit
    try:
        hour = time.gmtime(float(row.get("timestamp") or row.get("closed_at") or 0.0)).tm_hour
    except Exception:
        return "unknown"
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 21:
        return "new_york"
    return "after_hours"


def _risk_profile_bucket(row: dict) -> str:
    payload = _analytics_outcome_json(row)
    for key in ("risk_profile", "risk_mode", "policy_mode"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _news_bucket(row: dict) -> str:
    payload = _analytics_outcome_json(row)
    for key in ("gemini_sentiment", "news_sentiment", "news_bias", "event_bias"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "unknown"


async def _analytics_outcomes(limit: int) -> list[dict]:
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    return await db.get_trade_outcomes(limit=max(1, min(int(limit), 100000)))


@router.get("/analytics/confidence-calibration")
async def confidence_calibration(limit: int = 200, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    outcomes = await db.get_trade_outcomes(limit=limit)
    return analytics_service.confidence_calibration(outcomes)


@router.get("/analytics/holding-time")
async def holding_time_analytics(limit: int = 500, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    outcomes = await db.get_trade_outcomes(limit=limit)
    return analytics_service.holding_time_analysis(outcomes)


@router.get("/analytics/performance")
async def performance_attribution_reports(limit: int = 2000, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    outcomes = await _analytics_outcomes(limit)
    return {
        "sample_size": len(outcomes),
        "by_symbol": _analytics_bucket_rows(outcomes, lambda row: str(row.get("symbol") or "unknown").upper()),
        "by_strategy": _analytics_bucket_rows(outcomes, lambda row: row.get("strategy") or _analytics_outcome_json(row).get("strategy_id")),
        "by_confidence_bucket": _analytics_bucket_rows(outcomes, _confidence_bucket),
        "by_session": _analytics_bucket_rows(outcomes, _session_bucket),
        "by_risk_profile": _analytics_bucket_rows(outcomes, _risk_profile_bucket),
        "news_gemini_impact": _analytics_bucket_rows(outcomes, _news_bucket),
    }


@router.get("/analytics/performance/{dimension}")
async def performance_attribution_by_dimension(
    dimension: Literal["symbol", "strategy", "confidence", "session", "risk-profile", "news-gemini"],
    limit: int = 2000,
    authorization: Optional[str] = Header(default=None),
):
    await _require_user_permission(authorization, "can_access_research")
    outcomes = await _analytics_outcomes(limit)
    bucket_fns = {
        "symbol": lambda row: str(row.get("symbol") or "unknown").upper(),
        "strategy": lambda row: row.get("strategy") or _analytics_outcome_json(row).get("strategy_id"),
        "confidence": _confidence_bucket,
        "session": _session_bucket,
        "risk-profile": _risk_profile_bucket,
        "news-gemini": _news_bucket,
    }
    return {
        "dimension": dimension,
        "sample_size": len(outcomes),
        "buckets": _analytics_bucket_rows(outcomes, bucket_fns[dimension]),
    }


def _get_research_cycle_service() -> ResearchCycleService:
    if research_cycle_service is None:
        raise HTTPException(status_code=500, detail="Research cycle service is unavailable")
    return research_cycle_service


def _require_research_enabled():
    if not config.ENABLE_RESEARCH_CYCLE:
        raise HTTPException(
            status_code=403,
            detail="Research cycle is disabled. Set ENABLE_RESEARCH_CYCLE=true to use research endpoints.",
        )


class DatasetRebuildRequest(BaseModel):
    output_name: str = "trade_dataset"
    limit: int = 100000
    include_unexecuted: bool = True
    parquet: bool = False


class TrainModelRequest(BaseModel):
    algorithm: Literal["logistic_regression", "gradient_boosting", "empirical_rules"] = "logistic_regression"
    target_column: str = "profitable_after_costs_90m"
    include_unexecuted: bool = True
    min_rows: int = 30


class ResearchReplayRequest(BaseModel):
    version_id: str
    score_threshold: float = 0.55
    include_unexecuted: bool = True
    limit: int = 200000


class WalkForwardRequest(BaseModel):
    algorithm: Literal["logistic_regression", "gradient_boosting", "empirical_rules"] = "logistic_regression"
    target_column: str = "profitable_after_costs_90m"
    score_threshold: float = 0.55
    windows: int = 5
    include_unexecuted: bool = True
    limit: int = 200000


class AttributionReportRequest(BaseModel):
    report_type: str = "full"
    limit: int = 2000


@router.get("/research/status")
async def research_status(authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    service = _get_research_cycle_service()
    try:
        snapshot = await asyncio.wait_for(service.status_snapshot(), timeout=20.0)
    except asyncio.TimeoutError:
        logger.warning("research-status: timed out; returning lightweight fallback snapshot")
        meta_service = getattr(service, "meta_model_service", None)
        if meta_service is not None:
            try:
                await asyncio.wait_for(meta_service.refresh_active_model(force=True), timeout=1.5)
            except Exception:
                pass
        meta_active = bool(getattr(meta_service, "is_active", False))
        snapshot = {
            "meta_model_active": meta_active,
            "meta_model_active_version": str(getattr(meta_service, "active_version_id", "") or ""),
            "active_approved_model": None,
            "last_training_run": None,
            "last_replay_run": None,
            "last_walk_forward_run": None,
            "best_candidate_model": None,
            "recent_attribution_reports": [],
            "incremental_training": {
                "last_run_at": float(getattr(service, "_last_incremental_train_at", 0.0) or 0.0),
                "last_closed_count": int(getattr(service, "_last_incremental_closed_count", 0) or 0),
                "local_closed_count": 0,
            },
            "meta_model_inactive_reason": "" if meta_active else "status_timeout",
        }
        try:
            if db is not None and hasattr(db, "count_trade_outcomes"):
                snapshot["incremental_training"]["local_closed_count"] = int(
                    await asyncio.wait_for(db.count_trade_outcomes(), timeout=0.75)
                )
        except Exception:
            pass
    else:
        inactive_reason = ""
        if not snapshot.get("meta_model_active"):
            approved = snapshot.get("active_approved_model") or {}
            best_candidate = snapshot.get("best_candidate_model") or {}
            if not approved:
                inactive_reason = "no_approved_model"
            elif not snapshot.get("meta_model_active_version"):
                inactive_reason = "approved_model_not_loaded"
            elif best_candidate:
                inactive_reason = "candidate_pending_or_not_promoted"
            else:
                inactive_reason = "inactive"
        snapshot["meta_model_inactive_reason"] = inactive_reason
    return {
        "enabled": bool(config.ENABLE_RESEARCH_CYCLE),
        "config": {
            "AUTO_TRAIN_ON_DEMO": bool(config.AUTO_TRAIN_ON_DEMO),
            "AUTO_PROMOTE_ON_DEMO": bool(config.AUTO_PROMOTE_ON_DEMO),
            "MIN_TRADES_BEFORE_TRAINING": int(config.MIN_TRADES_BEFORE_TRAINING),
            "TRAINING_WINDOW_DAYS": int(config.TRAINING_WINDOW_DAYS),
            "WALK_FORWARD_WINDOWS": int(config.WALK_FORWARD_WINDOWS),
        },
        **snapshot,
    }


@router.post("/research/dataset/rebuild")
async def research_rebuild_dataset(req: DatasetRebuildRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.rebuild_dataset(
        output_name=req.output_name,
        limit=max(100, min(req.limit, 500000)),
        include_unexecuted=req.include_unexecuted,
        parquet=req.parquet,
    )


@router.post("/research/model/train")
async def research_train_model(req: TrainModelRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.train_candidate_model(
        algorithm=req.algorithm,
        target_column=req.target_column,
        include_unexecuted=req.include_unexecuted,
        min_rows=max(10, min(req.min_rows, 50000)),
    )


@router.post("/research/replay/run")
async def research_run_replay(req: ResearchReplayRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.run_replay(
        version_id=req.version_id,
        score_threshold=max(0.0, min(req.score_threshold, 1.0)),
        include_unexecuted=req.include_unexecuted,
        limit=max(100, min(req.limit, 500000)),
    )


@router.post("/research/walk-forward/run")
async def research_run_walk_forward(req: WalkForwardRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.run_walk_forward(
        algorithm=req.algorithm,
        target_column=req.target_column,
        score_threshold=max(0.0, min(req.score_threshold, 1.0)),
        windows=max(2, min(req.windows, 50)),
        include_unexecuted=req.include_unexecuted,
        limit=max(100, min(req.limit, 500000)),
    )


@router.get("/research/models")
async def research_list_models(limit: int = 50, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    service = _get_research_cycle_service()
    return {
        "models": await service.list_model_versions(limit=max(1, min(limit, 200))),
    }


@router.post("/research/models/{version_id}/approve")
async def research_approve_model(version_id: str, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    try:
        model = await service.approve_model(version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activated = await service.activate_approved_model()
    return {
        "approved_model": model,
        "activation": activated,
    }


@router.post("/research/models/activate-approved")
async def research_activate_approved_model(authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    service = _get_research_cycle_service()
    return await service.activate_approved_model()


@router.post("/research/reports/attribution")
async def research_generate_attribution(req: AttributionReportRequest, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.generate_attribution_report(
        report_type=req.report_type,
        limit=max(100, min(req.limit, 100000)),
    )


@router.get("/research/reports")
async def research_list_reports(limit: int = 20, authorization: Optional[str] = Header(default=None)):
    await _require_user_permission(authorization, "can_access_research")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    return {
        "reports": await db.list_attribution_reports(limit=max(1, min(limit, 100))),
    }


# --- Auto-Trade Routes ---

class AutoTradeSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    min_confidence: Optional[float] = None
    scan_interval: Optional[int] = None
    budget_mode: Optional[Literal["full_margin", "custom_budget"]] = None
    budget_usd: Optional[float] = None


class AutoTradeStartRequest(BaseModel):
    budget_mode: Optional[Literal["full_margin", "custom_budget"]] = None
    budget_usd: Optional[float] = None


@router.get("/auto-trade/status")
async def auto_trade_status(authorization: Optional[str] = Header(default=None)):
    """Get auto-trading status and recent activity."""
    _ensure_mt5_platform_contract()
    _ensure_position_manager_running_if_connected()
    user = await _resolve_authenticated_user(authorization)
    user_id = str(user.get("id") or "local-anonymous")
    try:
        selected_strategy_id = await asyncio.wait_for(
            _get_active_strategy_id_for_user(user_id),
            timeout=0.35,
        )
    except Exception:
        selected_strategy_id = "builtin-default-smart"
    settings = risk_engine.settings
    service_status = task_orchestrator.status_snapshot()
    if active_platform == "tradestation" and tradestation_connector.connected:
        try:
            account = await asyncio.wait_for(
                asyncio.to_thread(tradestation_connector.refresh_account),
                timeout=0.8,
            )
        except Exception as exc:
            logger.warning("TradeStation auto-trade status account refresh timed out: %s", exc)
            account = tradestation_connector.account_info
        try:
            positions_snapshot = await asyncio.wait_for(
                asyncio.to_thread(_safe_tradestation_positions),
                timeout=1.0,
            )
        except Exception as exc:
            logger.warning("TradeStation auto-trade status position refresh timed out: %s", exc)
            positions_snapshot = []
        portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
            account,
            positions_snapshot,
        )
    elif active_platform == "ibkr" and ibkr_connector.connected:
        account_snapshot = ibkr_connector.account_info
        cached_position_rows = [
            row for key, row in _positions_payload_cache.items()
            if key[0] == "ibkr" and key[1] == ""
        ]
        positions_snapshot = list(max(cached_position_rows, key=lambda row: row[0])[1]) if cached_position_rows else []
        portfolio_snapshot = _ibkr_portfolio_snapshot(account_snapshot, positions_snapshot)
    elif connector.connected:
        account_snapshot = connector.account_info
        positions_snapshot = []
        try:
            refreshed = await asyncio.wait_for(
                asyncio.to_thread(connector.refresh_account),
                timeout=0.6,
            )
            if refreshed is not None:
                account_snapshot = refreshed
        except Exception as exc:
            logger.warning("MT5 auto-trade status account refresh timed out: %s", exc)
        try:
            positions_snapshot = await asyncio.wait_for(
                asyncio.to_thread(execution.get_positions),
                timeout=0.9,
            )
        except Exception as exc:
            logger.warning("MT5 auto-trade status position refresh timed out: %s", exc)
            positions_snapshot = []
        portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
            account_snapshot,
            positions_snapshot,
        )
    else:
        portfolio_snapshot = {
            "margin_utilization_pct": 0.0,
            "free_margin_pct": 0.0,
            "open_positions_total": 0,
            "exposure_by_symbol": {},
            "exposure_by_category": {},
            "exposure_by_sector": {},
            "usd_beta_exposure_pct": 0.0,
            "stocks_equity_exposure_pct": 0.0,
        }
    gemini_status = _current_gemini_status()
    return {
        "platform": active_platform,
        "platforms_supported": ["mt5", "ibkr", "tradestation"],
        "running": auto_trader.is_running,
        "enabled": risk_engine.auto_trade_enabled,
        "budget_mode": risk_engine.auto_trade_budget_mode,
        "budget_usd": risk_engine.auto_trade_budget_usd,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": risk_engine.auto_trade_scan_interval_seconds,
        "active_strategy_id": selected_strategy_id,
        "last_scan": auto_trader.last_scan_time,
        "recent_trades": auto_trader.recent_trades,
        "panic_stop": risk_engine.panic_stopped,
        "user_policy": risk_engine.user_policy.model_dump(),
        "universe": universe_service.summary_dict(),
        "position_manager_running": auto_trader.position_manager.is_running,
        "managed_tickets": list(auto_trader.position_manager.managed_tickets),
        "portfolio": portfolio_snapshot,
        "gemini": {
            "available": gemini_status.get("available", False),
            "degraded": gemini_status.get("degraded", False),
            "last_error": gemini_status.get("last_error"),
            "state": gemini_status.get("state", "unavailable"),
            "primary_model": gemini_status.get("primary_model", ""),
            "active_model": gemini_status.get("active_model", ""),
            "using_fallback_model": bool(gemini_status.get("using_fallback_model", False)),
            "cooldown_seconds": int(gemini_status.get("cooldown_seconds", 0) or 0),
            "credits_pct": int(gemini_status.get("credits_pct", 0) or 0),
        },
        "event_providers": {
            "benzinga": benzinga_adapter.healthcheck(),
        },
        "services": service_status,
    }


@router.get("/auto-trade/activity")
async def auto_trade_activity(limit: int = 50, authorization: Optional[str] = Header(default=None)):
    """Get combined AI activity feed (scanner + position manager)."""
    user = await _resolve_authenticated_user(authorization)
    user_id = str(user.get("id") or "local-anonymous")
    profiles: list[dict] = []
    selected_strategy_id = "builtin-default-smart"
    try:
        profiles = await asyncio.wait_for(
            _list_strategies_for_user(user_id),
            timeout=0.5,
        )
        selected_strategy_id = await asyncio.wait_for(
            _get_active_strategy_id_for_user(user_id),
            timeout=0.35,
        )
    except Exception:
        profiles = []
        selected_strategy_id = "builtin-default-smart"
    selected_profile = _resolve_strategy_from_profiles(selected_strategy_id, profiles)
    selected_cfg = dict((selected_profile or {}).get("config") or {})
    allowed_symbols_raw = selected_cfg.get("allowed_symbols")
    allowed_canonical: set[str] = set()
    if isinstance(allowed_symbols_raw, list):
        allowed_canonical = {
            universe_service.canonical_symbol(str(sym).strip().upper())
            for sym in allowed_symbols_raw
            if str(sym).strip()
        }
    if not allowed_canonical:
        runtime_allowed = getattr(risk_engine.user_policy, "allowed_symbols", []) or []
        allowed_canonical = {
            universe_service.canonical_symbol(str(sym).strip().upper())
            for sym in runtime_allowed
            if str(sym).strip()
        }

    def _activity_in_scope(item: dict) -> bool:
        if not allowed_canonical:
            return True
        symbol = universe_service.canonical_symbol(str(item.get("symbol") or ""))
        return symbol in allowed_canonical

    # In-memory combined feed
    combined = [item for item in auto_trader.combined_activity if _activity_in_scope(item)][:limit]
    live_symbols = {
        universe_service.canonical_symbol(str(item.get("symbol") or ""))
        for item in combined
        if str(item.get("symbol") or "").strip()
    }

    # Also get DB activity if available
    db_activity = []
    if db:
        try:
            source_rows = await asyncio.wait_for(
                db.get_ai_activity(min(limit * 2, 80)),
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("AI activity async fetch failed; using direct SQLite fallback: %s", exc)
            source_rows = []
            try:
                db_path = str(getattr(db, "path", "") or "")
                if db_path:
                    def _read_ai_activity_fallback() -> list[dict]:
                        conn = sqlite3.connect(db_path, timeout=2.0)
                        conn.row_factory = sqlite3.Row
                        try:
                            rows = conn.execute(
                                "SELECT * FROM ai_activity ORDER BY id DESC LIMIT ?",
                                (min(limit * 2, 80),),
                            ).fetchall()
                            return [dict(row) for row in rows]
                        finally:
                            conn.close()
                    source_rows = await asyncio.to_thread(_read_ai_activity_fallback)
            except Exception as fallback_exc:
                logger.warning("AI activity direct SQLite fallback failed: %s", fallback_exc)
        if source_rows:
            if live_symbols:
                db_activity = [
                    item for item in source_rows
                    if universe_service.canonical_symbol(str(item.get("symbol") or "")) in live_symbols
                ][:limit]
            else:
                db_activity = [item for item in source_rows if _activity_in_scope(item)][:limit]
            if not db_activity:
                # Persisted AI activity is an audit trail. Do not hide it just because
                # the current strategy scope changed or the scanner was restarted.
                db_activity = list(source_rows[:limit])
            normalized_rows: list[dict] = []
            for item in db_activity:
                row = dict(item)
                action = str(row.get("action") or "").upper()
                ticket = int(row.get("ticket") or 0)
                if ticket <= 0 and action in {"BUY", "SELL"}:
                    row["action"] = "HOLD"
                normalized_rows.append(row)
            db_activity = normalized_rows

    return {
        "live_activity": combined,
        "db_activity": db_activity,
        "position_manager": {
            "running": auto_trader.position_manager.is_running,
            "managed_tickets": list(auto_trader.position_manager.managed_tickets),
            "status": auto_trader.position_manager.status_snapshot(),
        },
    }


@router.post("/auto-trade/start")
async def auto_trade_start(req: AutoTradeStartRequest, authorization: Optional[str] = Header(default=None)):
    """Enable and start auto-trading (includes position manager)."""
    _ensure_mt5_platform_contract()
    _require_active_platform_connection()
    _raise_if_kill_switch_active()
    await _require_user_permission(authorization, "can_enable_autotrader")
    await _require_order_execution_permission(authorization, execution_mode=config.AUTO_TRADER_EXECUTION_MODE)
    _enforce_risk_profile_execution_caps(auto_trade=True)
    is_demo_account = ibkr_connector.is_demo() if active_platform == "ibkr" else connector.is_demo()
    if not is_demo_account and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(
            status_code=403,
            detail="Auto-trading on live accounts requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
        )

    try:
        await asyncio.wait_for(
            _sync_active_strategy_runtime_for_user("local-anonymous", preserve_user_policy=True),
            timeout=0.75,
        )
    except Exception as exc:
        logger.warning("Auto-trade start continued without blocking strategy sync: %s", exc)
    account_info = (
        await _run_ibkr(ibkr_connector.refresh_account)
        if active_platform == "ibkr"
        else connector.refresh_account()
    )
    current_margin_used = max(0.0, float(getattr(account_info, "margin", 0.0) or 0.0))

    requested_budget_mode = str(req.budget_mode or risk_engine.auto_trade_budget_mode or "full_margin")
    requested_budget_usd = risk_engine.auto_trade_budget_usd
    if requested_budget_mode == "custom_budget":
        next_budget = float(req.budget_usd or requested_budget_usd or 0.0)
        if next_budget <= 0:
            raise HTTPException(status_code=400, detail="Custom auto-trade budget must be greater than 0.")
        if current_margin_used > 0 and next_budget < current_margin_used:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Custom auto-trade budget (${next_budget:.2f}) cannot be lower than "
                    f"current margin used (${current_margin_used:.2f})."
                ),
            )
        requested_budget_usd = next_budget
    else:
        if req.budget_mode == "full_margin":
            requested_budget_usd = None
        elif req.budget_usd is not None and float(req.budget_usd or 0.0) > 0:
            requested_budget_usd = float(req.budget_usd)

    risk_engine.auto_trade_budget_mode = requested_budget_mode
    risk_engine.auto_trade_budget_usd = requested_budget_usd
    risk_engine.auto_trade_enabled = True
    _arm_auto_trade_session(True)
    task_orchestrator.start_auto_trade()
    if active_platform == "ibkr":
        # IBKR orders receive broker-native OCA SL/TP protection. The local
        # MT5-style polling manager must not issue duplicate close requests.
        auto_trader.position_manager.stop()
    _schedule_runtime_state_persist()
    _schedule_connection_event("auto_trade_started", details="Auto-trading + Position Manager enabled")

    return {
        "running": True,
        "position_manager_running": bool(auto_trader.position_manager.is_running),
        "budget_mode": risk_engine.auto_trade_budget_mode,
        "budget_usd": risk_engine.auto_trade_budget_usd,
        "services": task_orchestrator.status_snapshot(),
        "message": "Auto-trading started with AI position management",
    }


@router.post("/auto-trade/stop")
async def auto_trade_stop():
    """Disable auto-trading while keeping protective position management active."""
    risk_engine.auto_trade_enabled = False
    _arm_auto_trade_session(False)
    task_orchestrator.stop_auto_trade(keep_position_manager=(active_platform != "ibkr"))
    _ensure_position_manager_running_if_connected()
    _schedule_runtime_state_persist()
    _schedule_connection_event("auto_trade_stopped", details="Auto-trading disabled; Position Manager kept active")

    return {
        "running": False,
        "position_manager_running": auto_trader.position_manager.is_running,
        "services": task_orchestrator.status_snapshot(),
        "message": "Auto-trading stopped; protective position management remains active",
    }


@router.post("/auto-trade/settings")
async def auto_trade_settings(req: AutoTradeSettingsRequest, authorization: Optional[str] = Header(default=None)):
    """Update auto-trade settings."""
    _ensure_mt5_platform_contract()
    if req.enabled is True:
        await _require_user_permission(authorization, "can_enable_autotrader")
        await _require_order_execution_permission(authorization, execution_mode=config.AUTO_TRADER_EXECUTION_MODE)
        _enforce_risk_profile_execution_caps(auto_trade=True)
    elif req.enabled is not False:
        await _require_user_permission(authorization, "can_enable_autotrader")
    if req.enabled is True and not _active_platform_connected():
        raise HTTPException(
            status_code=400,
            detail="Connect an MT5 live trading account before enabling auto-trading.",
        )

    account_info = (
        await _run_ibkr(ibkr_connector.refresh_account)
        if active_platform == "ibkr"
        else connector.refresh_account()
    )
    current_margin_used = max(0.0, float(getattr(account_info, "margin", 0.0) or 0.0))

    if req.enabled is not None:
        if req.enabled:
            _raise_if_kill_switch_active()
            is_demo_account = ibkr_connector.is_demo() if active_platform == "ibkr" else connector.is_demo()
            if not is_demo_account and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
                raise HTTPException(
                    status_code=403,
                    detail="Auto-trading on live accounts requires demo-only mode off and LIVE_TRADING_ENABLED=true.",
                )
        risk_engine.auto_trade_enabled = req.enabled
        _arm_auto_trade_session(bool(req.enabled))
        if req.enabled and not auto_trader.is_running and _active_platform_connected():
            task_orchestrator.start_auto_trade()
            if active_platform == "ibkr":
                auto_trader.position_manager.stop()
        elif not req.enabled and auto_trader.is_running:
            task_orchestrator.stop_auto_trade(keep_position_manager=(active_platform != "ibkr"))
        if not req.enabled:
            _ensure_position_manager_running_if_connected()

    if req.min_confidence is not None:
        raise HTTPException(
            status_code=400,
            detail="Minimum confidence is mode-derived now. Change the policy preset instead.",
        )

    if req.scan_interval is not None:
        if req.scan_interval < 30:
            raise HTTPException(status_code=400, detail="Scan interval must be at least 30 seconds")
        risk_engine.auto_trade_scan_interval_seconds = req.scan_interval

    if req.budget_mode is not None:
        risk_engine.auto_trade_budget_mode = req.budget_mode
        if req.budget_mode == "full_margin":
            risk_engine.auto_trade_budget_usd = None

    if req.budget_usd is not None:
        if req.budget_usd <= 0:
            raise HTTPException(status_code=400, detail="Custom auto-trade budget must be greater than 0.")
        if current_margin_used > 0 and float(req.budget_usd) < current_margin_used:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Custom auto-trade budget (${float(req.budget_usd):.2f}) cannot be lower than "
                    f"current margin used (${current_margin_used:.2f})."
                ),
            )
        risk_engine.auto_trade_budget_usd = req.budget_usd
        if req.budget_mode is None:
            risk_engine.auto_trade_budget_mode = "custom_budget"

    _schedule_runtime_state_persist()

    settings = risk_engine.settings
    return {
        "enabled": risk_engine.auto_trade_enabled,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": risk_engine.auto_trade_scan_interval_seconds,
        "budget_mode": risk_engine.auto_trade_budget_mode,
        "budget_usd": risk_engine.auto_trade_budget_usd,
        "services": task_orchestrator.status_snapshot(),
    }
