from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
import logging
import time
import asyncio
import json
import re
import math
from concurrent.futures import ThreadPoolExecutor

from adapters.finnhub_adapter import FinnhubAdapter
from mt5.connector import MT5Connector, ConnectionParams
from ibkr.connector import IBKRConnector, IBKRConnectionParams
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
from services.event_ingestion_service import EventIngestionService
from services.event_normalization_service import EventNormalizationService
from services.execution_service import ExecutionService
from services.gemini_news_analysis_service import GeminiNewsAnalysisService
from services.gemini_adapter import GeminiAdapter
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
_last_scan_gemini_enrich_at = 0.0
_ibkr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-worker")
IBKR_SCAN_DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD",
    "SPY", "QQQ", "DIA", "IWM", "GLD", "SLV", "USO", "BNO",
]

router = APIRouter()

# Shared state
connector = MT5Connector()
ibkr_connector = IBKRConnector()
market_data = MarketDataService()
execution = ExecutionEngine()
risk_engine = RiskEngine()
db: Optional[Database] = None
universe_service = SymbolUniverseService(config)

# Agent registry - SmartAgent is the deterministic default, Gemini is advisory
gemini_agent = GeminiAgent()
agents = {
    "GeminiAgent": gemini_agent,
    "SmartAgent": SmartAgent(),
    "MockAgent": MockAgent(),
    "SMA_Crossover": SMACrossoverAgent(),
}
active_agent_name = "SmartAgent"
active_platform: Literal["mt5", "ibkr"] = "mt5"
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
trade_decision_service = TradeDecisionService()
finnhub_adapter = FinnhubAdapter(
    api_key=config.FINNHUB_API_KEY,
    enabled=config.ENABLE_FINNHUB,
    timeout_seconds=config.FINNHUB_TIMEOUT_SECONDS,
    max_retries=config.FINNHUB_MAX_RETRIES,
)
event_normalization_service = EventNormalizationService()
asset_mapping_service = AssetMappingService(universe_service=universe_service)
gemini_event_classifier = GeminiEventClassifier(
    timeout_seconds=config.GEMINI_TIMEOUT_SECONDS,
    max_retries=config.GEMINI_MAX_RETRIES,
    model_name=config.GEMINI_MODEL,
)
event_ingestion_service = EventIngestionService(
    finnhub_adapter=finnhub_adapter,
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
task_orchestrator = TaskOrchestrator(auto_trader)
analytics_service = AnalyticsService()
replay_service = ReplayService(signal_pipeline)
research_cycle_service: Optional[ResearchCycleService] = None
RUNTIME_STATE_KEY = "runtime_controls_v1"
CHAT_HISTORY_STATE_KEY = "chat_history_v1"
DEFAULT_MARGIN_SL_PCT = 0.08
DEFAULT_MARGIN_TP_PCT = 0.12
_cloud_brain_state: dict = {
    "last_command_at": 0.0,
    "last_command_payload": {},
    "last_apply_result": {},
    "last_error": None,
}


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
    auto_trader.set_database(database)
    event_ingestion_service.set_database(database)
    research_cycle_service = ResearchCycleService(
        database,
        meta_model_service=signal_pipeline.meta_model_service,
    )
    auto_trader.position_manager.set_trade_closed_callback(_on_position_manager_trade_closed)


def current_research_cycle_service() -> Optional[ResearchCycleService]:
    return research_cycle_service


async def apply_cloud_brain_command(command: dict) -> dict:
    """Apply cloud command as high-level runtime policy controls only.

    Execution/risk checks remain local and deterministic.
    """
    global _cloud_brain_state
    cmd = dict(command or {})
    applied: dict[str, object] = {"updated": []}

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
            risk_engine.auto_trade_enabled = bool(cmd.get("auto_trade_enabled"))
            if risk_engine.auto_trade_enabled and not auto_trader.is_running and connector.connected:
                task_orchestrator.start_auto_trade()
            elif not risk_engine.auto_trade_enabled and auto_trader.is_running:
                task_orchestrator.stop_auto_trade()
            applied["updated"].append("auto_trade_enabled")

        if "scan_interval_seconds" in cmd:
            try:
                interval = int(cmd.get("scan_interval_seconds"))
                risk_engine.auto_trade_scan_interval_seconds = max(5, min(interval, 30))
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
        "user_policy": risk_engine.user_policy.model_dump(),
        "auto_trade_enabled": bool(risk_engine.auto_trade_enabled),
        "auto_trade_scan_interval_seconds": int(risk_engine.auto_trade_scan_interval_seconds),
    }


async def _persist_runtime_state():
    if db is None:
        return
    await db.save_runtime_state(RUNTIME_STATE_KEY, _runtime_state_payload())


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
    if db is None:
        return
    state = await db.get_runtime_state(RUNTIME_STATE_KEY)
    if not state:
        return
    try:
        policy = UserPolicySettings(**(state.get("user_policy") or {}))
        risk_engine.update_user_policy(policy)
        risk_engine.auto_trade_enabled = bool(state.get("auto_trade_enabled", False))
        interval = int(state.get("auto_trade_scan_interval_seconds", 15))
        risk_engine.auto_trade_scan_interval_seconds = max(5, min(interval, 15))
        logger.info("Restored runtime state: mode=%s, auto_trade_enabled=%s, scan_interval=%ss",
                    risk_engine.user_policy.mode,
                    risk_engine.auto_trade_enabled,
                    risk_engine.auto_trade_scan_interval_seconds)
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
            if trigger == "post_trade_analysis_saved":
                result = await service.maybe_train_from_post_analysis_update(
                    min_rows=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
                    cooldown_seconds=max(120, int(config.AUTO_META_TRAIN_INTERVAL_SECONDS)),
                    auto_approve=config.AUTO_META_AUTO_APPROVE,
                    min_precision=config.AUTO_META_MIN_PRECISION,
                    min_f1=config.AUTO_META_MIN_F1,
                )
            else:
                result = await service.maybe_train_from_new_trade_outcome(
                    min_rows=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
                    cooldown_seconds=max(60, int(config.AUTO_META_TRAIN_INTERVAL_SECONDS // 2)),
                    auto_approve=config.AUTO_META_AUTO_APPROVE,
                    min_precision=config.AUTO_META_MIN_PRECISION,
                    min_f1=config.AUTO_META_MIN_F1,
                )
            logger.info("Incremental meta-training trigger=%s result=%s", trigger, result)
        except Exception as exc:
            logger.warning("Incremental meta-training failed after %s: %s", trigger, exc)

    asyncio.create_task(_runner())


def _requested_agent_from_final_name(agent_name: str) -> str:
    return "GeminiAgent" if "Gemini" in (agent_name or "") else "SmartAgent"


def _resolve_market_symbol(symbol: str) -> str:
    normalized = universe_service.canonical_symbol(symbol)
    # Fast path: if the symbol is directly available in MT5, avoid expensive universe scans.
    info = market_data.get_symbol_info(normalized)
    if info:
        return normalized
    # Fallback path only when direct symbol lookup fails.
    tradeable = market_data.get_tradeable_symbols()
    visible = market_data.get_visible_symbols() if not tradeable else []
    resolved = universe_service.resolve_requested_symbols([normalized], tradeable + visible)
    return resolved[0] if resolved else normalized


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


async def _store_management_plan(ticket: int, signal_id: int | None, symbol: str, action: str, decision):
    if db is None or ticket is None:
        return
    await db.save_position_management_plan(
        ticket=ticket,
        signal_id=signal_id,
        symbol=symbol,
        action=action,
        plan=decision.position_management_plan.model_dump(),
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
    if not password and credential.get("password"):
        password = credential["password"]
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
            "finnhub": finnhub_adapter.healthcheck(),
        },
    }


def _active_platform_connected() -> bool:
    if active_platform == "ibkr":
        return bool(ibkr_connector.connected)
    return bool(connector.connected)


def _require_active_platform_connection():
    if not _active_platform_connected():
        raise HTTPException(status_code=400, detail="Not connected")


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
            "bid": 1.0,
            "ask": 1.0,
        })
    return symbols


async def _evaluate_ibkr_symbol_recommendation(sym: str) -> dict:
    category = universe_service.expected_category_for_symbol(sym) or "Stocks"
    snapshot = await _run_ibkr(ibkr_connector.get_snapshot, sym)
    bid = float((snapshot or {}).get("bid", 0.0) or 0.0)
    ask = float((snapshot or {}).get("ask", 0.0) or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask or 0.0)
    spread = max(0.0, ask - bid) if ask > 0 and bid > 0 else 0.0

    bars_m15 = await _run_ibkr(ibkr_connector.get_bars, sym, "M15", 96)
    bars_h1 = await _run_ibkr(ibkr_connector.get_bars, sym, "H1", 96)
    bars_h4 = await _run_ibkr(ibkr_connector.get_bars, sym, "H4", 72)
    if len(bars_h4) < 52:
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

    if len(bars_h1) >= 52 and len(bars_m15) >= 22:
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
    adapter_last_failure_at = float(getattr(gemini_adapter, "last_failure_at", 0.0) or 0.0)
    adapter_failure_recent = bool(
        getattr(gemini_adapter, "last_error", None)
        and adapter_last_failure_at > 0
        and (now - adapter_last_failure_at) <= RECENT_FAILURE_WINDOW_SECONDS
    )
    degraded = bool(
        (not available)
        or cooldown_seconds > 0
        or adapter_failure_recent
    )

    active_error = None
    if not available:
        active_error = getattr(gemini_agent, "unavailable_reason", "") or None
    elif cooldown_seconds > 0:
        active_error = "Quota exhausted / rate limited. Using technical logic only until quota resets."
    elif adapter_failure_recent:
        active_error = getattr(gemini_adapter, "last_error", None)
    last_error = _normalize_error(active_error)

    if not available:
        credits_pct = 0
    elif cooldown_seconds > 0:
        # During cooldown, model should be considered near-exhausted.
        credits_pct = 5
    elif degraded:
        credits_pct = 40
    else:
        credits_pct = 100

    if available and cooldown_seconds > 0:
        reply = f"Gemini is in quota cooldown (~{cooldown_seconds}s remaining). Technical logic is used until cooldown ends."
        state = "cooldown"
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
        "last_error": last_error,
        "cooldown_seconds": cooldown_seconds,
        "credits_pct": credits_pct,
        "reply": reply,
    }


# --- Connection Routes ---

class ConnectRequest(BaseModel):
    platform: Literal["mt5", "ibkr"] = "mt5"
    account: Optional[int] = None
    password: str = ""
    server: str = ""
    terminal_path: Optional[str] = None
    save_credentials: bool = False
    ibkr_host: Optional[str] = None
    ibkr_port: Optional[int] = None
    ibkr_client_id: Optional[int] = None
    ibkr_account_id: Optional[str] = None


@router.post("/connect")
async def connect(req: ConnectRequest):
    global active_platform
    requested_platform = (req.platform or "mt5").lower()
    if requested_platform == "ibkr":
        try:
            ibkr_params = IBKRConnectionParams(
                host=(req.ibkr_host or "127.0.0.1").strip(),
                port=int(req.ibkr_port or 7497),
                client_id=int(req.ibkr_client_id or 1),
                account_id=(req.ibkr_account_id or "").strip() or None,
            )
            success = await _run_ibkr(ibkr_connector.connect, ibkr_params)
            if not success:
                if db:
                    await db.log_error("connection", ibkr_connector.last_error or "IBKR connection failed")
                raise HTTPException(status_code=400, detail=ibkr_connector.last_error or "IBKR connection failed")
            if success and not ibkr_connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
                await _run_ibkr(ibkr_connector.disconnect)
                raise HTTPException(
                    status_code=403,
                    detail="IBKR live account rejected by policy. Use paper account or enable live trading.",
                )
            if connector.connected:
                connector.disconnect()
            active_platform = "ibkr"
            live_account = await _run_ibkr(ibkr_connector.refresh_account)
            if db:
                await db.log_connection_event(
                    "connected",
                    account=0,
                    server=f"IBKR {ibkr_params.host}:{ibkr_params.port}",
                    details=f"platform=ibkr client_id={ibkr_params.client_id}",
                )
            return {
                "connected": True,
                "platform": active_platform,
                "account": live_account.model_dump() if live_account else (ibkr_connector.account_info.model_dump() if ibkr_connector.account_info else None),
                "is_demo": ibkr_connector.is_demo(),
                "credential_status": {"requested": False, "saved": False, "reason": "IBKR credentials are not stored in this flow."},
            }
        except HTTPException:
            raise
        except Exception as exc:
            if db:
                await db.log_error("connection", f"IBKR connection route failure: {exc}")
            raise HTTPException(
                status_code=400,
                detail=ibkr_connector.last_error or f"IBKR connection failed: {exc}",
            )

    mt5_password = (req.password or "").strip()
    mt5_server = (req.server or "").strip()
    if req.account is None or not mt5_password or not mt5_server:
        raise HTTPException(status_code=400, detail="MT5 requires account, password, and server.")
    raw_path = (req.terminal_path or config.DEFAULT_TERMINAL_PATH).strip().strip('"').strip("'")
    params = ConnectionParams(
        account=req.account,
        password=mt5_password,
        server=mt5_server,
        terminal_path=raw_path,
    )
    success = connector.connect(params)

    if success and not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        connector.disconnect()
        if db:
            await db.log_connection_event(
                "rejected", req.account, req.server,
                "Live account rejected - demo only mode"
            )
        raise HTTPException(
            status_code=403,
            detail="Live trading is blocked by policy. Disable demo-only mode and enable LIVE_TRADING_ENABLED to allow live accounts.",
        )

    if success and db:
        await db.log_connection_event("connected", req.account, req.server)
        active_platform = "mt5"

        # Set daily start equity for risk engine
        if connector.account_info:
            risk_engine.set_daily_start_equity(connector.account_info.equity)

        credential_status = await _persist_credentials_if_requested(
            account=req.account,
            server=mt5_server,
            password=mt5_password,
            terminal_path=raw_path,
            save_credentials=req.save_credentials,
        )
        if risk_engine.auto_trade_enabled and not auto_trader.is_running:
            task_orchestrator.start_auto_trade()
    else:
        credential_status = {"requested": False, "saved": False}

    if not success:
        if db:
            await db.log_error("connection", connector.last_error or "Unknown error")
        detail = connector.last_error or "Unknown error"
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
    mt5_success = connector.disconnect() if connector.connected else True
    ibkr_success = await _run_ibkr(ibkr_connector.disconnect) if ibkr_connector.connected else True
    success = bool(mt5_success and ibkr_success)
    active_platform = "mt5"
    if db:
        await db.log_connection_event("disconnected")
    return {"disconnected": success}


@router.get("/status")
async def status():
    gemini_status = _current_gemini_status()
    if active_platform == "ibkr" and ibkr_connector.connected:
        account = await _run_ibkr(ibkr_connector.refresh_account)
        terminal = ibkr_connector.get_terminal_info()
        connected_flag = bool(ibkr_connector.connected)
        last_error = ibkr_connector.last_error
        is_demo = ibkr_connector.is_demo()
        positions_snapshot = await _run_ibkr(ibkr_connector.get_positions)
    else:
        account = connector.refresh_account()
        terminal = connector.get_terminal_info()
        connected_flag = bool(connector.connected)
        last_error = connector.last_error
        is_demo = connector.is_demo()
        positions_snapshot = execution.get_positions()

    # Keep runtime connected state stable even if one account refresh call fails.
    # Temporary MT5 IPC hiccups should not flip UI to disconnected and stop live polling.
    portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
        account,
        positions_snapshot,
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
    return {
        "connected": connected_flag,
        "platform": active_platform,
        "platforms_supported": ["mt5", "ibkr"],
        "account": account.model_dump() if account else None,
        "terminal": terminal.model_dump() if terminal else None,
        "last_error": last_error,
        "is_demo": is_demo,
        "live_trading_enabled": config.LIVE_TRADING_ENABLED,
        "panic_stop": risk_engine.panic_stopped,
        "active_agent": active_agent_name,
        "credential_storage_available": credential_vault.available,
        "gemini_available": gemini_status.get("available", False),
        "gemini_degraded": gemini_status.get("degraded", False),
        "gemini_last_error": gemini_status.get("last_error"),
        "gemini_state": gemini_status.get("state", "unavailable"),
        "gemini_cooldown_seconds": int(gemini_status.get("cooldown_seconds", 0) or 0),
        "gemini_credits_pct": int(gemini_status.get("credits_pct", 0) or 0),
        "user_policy": risk_engine.user_policy.model_dump(),
        "runtime_controls": risk_engine.runtime_controls(),
        "universe": universe_service.summary_dict(),
        "finnhub": finnhub_adapter.healthcheck(),
        "portfolio": portfolio_snapshot,
        "services": task_orchestrator.status_snapshot(),
        "cloud_brain": cloud_brain_snapshot(),
        "cloud_decision": {
            "enabled": bool(config.CLOUD_BRAIN_DECISION_ENABLED),
            "configured": bool(getattr(signal_pipeline.cloud_brain_decision_client, "configured", False)),
            "url": config.CLOUD_BRAIN_DECISION_URL,
            "last_error": getattr(signal_pipeline.cloud_brain_decision_client, "last_error", None),
        },
    }


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


@router.get("/account")
async def account():
    if active_platform == "ibkr" and ibkr_connector.connected:
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
    if active_platform != "ibkr" or not ibkr_connector.connected:
        raise HTTPException(status_code=400, detail="IBKR is not connected")
    return ibkr_connector.debug_account_summary()


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
    _require_active_platform_connection()
    if active_platform == "ibkr":
        snap = await _run_ibkr(ibkr_connector.get_stock_snapshot, symbol)
        if not snap:
            raise HTTPException(status_code=404, detail=f"No IBKR tick data for {symbol}")
        spread = max(0.0, float(snap["ask"]) - float(snap["bid"]))
        return {
            "symbol": snap["symbol"],
            "bid": float(snap["bid"]),
            "ask": float(snap["ask"]),
            "spread": spread,
            "time": int(time.time()),
        }
    tick = market_data.get_tick(_resolve_market_symbol(symbol))
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No tick data for {symbol}")
    return tick.model_dump()


@router.get("/market/ticks")
async def get_ticks(symbols: str):
    """Bulk tick endpoint to keep prices live with one request."""
    _require_active_platform_connection()
    raw_symbols = [s.strip() for s in (symbols or "").split(",") if s.strip()]
    # Defensive cap to avoid oversized requests from UI.
    requested = raw_symbols[:120]
    if not requested:
        return {"ticks": {}}

    ticks: dict[str, dict] = {}
    if active_platform == "ibkr":
        for sym in requested:
            snap = await _run_ibkr(ibkr_connector.get_stock_snapshot, sym)
            if not snap:
                continue
            spread = max(0.0, float(snap["ask"]) - float(snap["bid"]))
            ticks[sym] = {
                "symbol": str(snap["symbol"]),
                "bid": float(snap["bid"]),
                "ask": float(snap["ask"]),
                "spread": spread,
                "time": int(time.time()),
            }
    else:
        # Bulk lookup first for lower latency under fast dashboard polling.
        bulk = market_data.get_ticks(requested)
        for sym in requested:
            tick = bulk.get(sym)
            if tick is not None:
                ticks[sym] = tick.model_dump()

    return {"ticks": ticks}


@router.get("/market/bars/{symbol}")
async def get_bars(symbol: str, timeframe: str = "H1", count: int = 100):
    _require_active_platform_connection()
    if active_platform == "ibkr":
        raise HTTPException(
            status_code=501,
            detail="IBKR historical bars are not wired into this endpoint yet.",
        )
    bars = market_data.get_bars(_resolve_market_symbol(symbol), timeframe, count)
    return [b.model_dump() for b in bars]


@router.get("/market/symbol-info/{symbol}")
async def get_symbol_info(symbol: str):
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


@router.get("/market/available-symbols")
async def get_available_symbols(
    category: Optional[str] = None,
    tradeable_only: bool = False,
    include_inactive: bool = False,
):
    """Get all symbols available on the MT5 terminal, optionally filtered by category."""
    _require_active_platform_connection()
    if active_platform == "ibkr":
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

    return {
        "total": len(symbols),
        "categories": categories,
        "category_counts": {k: len(v) for k, v in categories.items()},
        "universe": universe_service.summary_dict(),
    }


@router.post("/market/auto-detect-symbols")
async def auto_detect_symbols(categories: Optional[list[str]] = None):
    """Auto-detect tradeable symbols and update allowed_symbols list."""
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


@router.get("/events/finnhub/health")
async def finnhub_health():
    return {
        "provider": finnhub_adapter.healthcheck(),
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
        "finnhub": finnhub_adapter.healthcheck(),
    }


@router.get("/events/latest")
async def latest_events(limit: int = 25):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    events = await event_ingestion_service.latest_usable_events(limit=limit)
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


# --- Agent Routes ---

class EvaluateRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    bar_count: int = 100
    agent_name: Optional[str] = None


@router.post("/agent/evaluate")
async def evaluate_signal(req: EvaluateRequest):
    _require_active_platform_connection()
    if active_platform == "ibkr":
        rec = await _evaluate_ibkr_symbol_recommendation(req.symbol)
        return {
            "signal": rec["signal"],
            "signal_id": rec.get("signal_id"),
            "risk_decision": rec["risk_decision"],
            "agent_name": "SmartAgent",
            "degraded_reasons": rec.get("degraded_reasons", []),
            "gemini_confirmation": None,
            "trade_quality": rec.get("trade_quality"),
            "portfolio_risk": rec.get("portfolio_risk"),
            "anti_churn": rec.get("anti_churn"),
            "execution_reason": rec.get("execution_reason", rec["risk_decision"]["reason"]),
            "position_management_plan": {
                "manage_position": True,
                "strategy": rec["signal"].get("strategy"),
                "max_holding_minutes": rec["signal"].get("max_holding_minutes"),
                "planned_hold_minutes": rec["signal"].get("max_holding_minutes"),
                "notes": ["IBKR route uses deterministic technical plan."],
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
    return {"active_agent": active_agent_name}


# --- Trading Routes ---

@router.get("/trade/calculate-volume")
async def calculate_volume(symbol: str, amount_usd: float):
    """Convert a dollar amount (margin) to trading volume (lots) for a given symbol.

    The amount_usd is treated as margin budget and solved using MT5 margin estimation.
    This avoids incorrect sizing on symbols whose effective margin differs from account leverage.
    """
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
    sym_info = market_data.get_symbol_info(resolved_symbol)
    if not tick or not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")

    price = tick.ask
    contract_size = sym_info.get("trade_contract_size", 100000)
    vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
    vol_max = float(sym_info.get("volume_max", 100) or 100)
    vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)

    if price <= 0 or contract_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid price or contract size")

    # amount_usd is margin → multiply by leverage to get notional, then divide by lot value
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
    # Convert price distance to dollar amount: dollars = price_distance * volume * contract_size
    min_sl_tp_dollars = round(min_price_distance * volume * contract_size, 2)
    # Ensure at least $1 minimum
    min_sl_tp_dollars = max(min_sl_tp_dollars, 1.0)

    return {
        "symbol": resolved_symbol,
        "amount_usd": amount_usd,
        "volume": round(volume, 4),
        "actual_cost": margin_required,
        "price": price,
        "contract_size": contract_size,
        "volume_min": vol_min,
        "volume_max": vol_max,
        "leverage": (connector.refresh_account().leverage if connector.refresh_account() else 1),
        "min_sl_tp_dollars": min_sl_tp_dollars,
        "stops_level": effective_stops,
    }


def _derive_amount_based_sl_tp(
    *,
    action: str,
    entry_price: float,
    volume: float,
    contract_size: float,
    sl_amount_usd: float,
    tp_amount_usd: float,
    digits: int,
) -> tuple[float, float]:
    price_per_unit = volume * contract_size
    if entry_price <= 0 or price_per_unit <= 0:
        return 0.0, 0.0

    sl_distance = sl_amount_usd / price_per_unit
    tp_distance = tp_amount_usd / price_per_unit
    action_upper = (action or "").upper()

    if action_upper == "BUY":
        sl = entry_price - sl_distance
        tp = entry_price + tp_distance
    else:
        sl = entry_price + sl_distance
        tp = entry_price - tp_distance

    return round(sl, digits), round(tp, digits)


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
        response = await asyncio.wait_for(
            asyncio.to_thread(
                gemini_agent._client.models.generate_content,
                model="gemini-2.5-flash",
                contents=[{"role": "user", "parts": [{"text": json.dumps(prompt_payload, ensure_ascii=True)}]}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            ),
            timeout=5.0,
        )
        parsed = _extract_json_object((response.text or "").strip())
        sl_delta = _clamp(float(parsed.get("sl_delta_pct", 0.0)), -0.02, 0.03)
        tp_delta = _clamp(float(parsed.get("tp_delta_pct", 0.0)), -0.03, 0.06)
        return sl_delta, tp_delta
    except Exception as exc:
        text = str(exc)
        if "429" in text or "RESOURCE_EXHAUSTED" in text:
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
    min_rr = max(1.0, float(risk_engine.user_policy.min_reward_risk or 1.8))

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

    sl_pct = _clamp(sl_pct, 0.03, 0.15)
    tp_pct = _clamp(tp_pct, 0.06, 0.35)

    # Enforce policy reward:risk requirement for default targets.
    tp_pct = max(tp_pct, sl_pct * min_rr)
    tp_pct = _clamp(tp_pct, 0.06, 0.40)

    sl_amount = amount_usd * sl_pct
    tp_amount = amount_usd * tp_pct
    return sl_amount, tp_amount


class QuickBuyRequest(BaseModel):
    symbol: str
    amount_usd: float
    action: str = "BUY"  # BUY or SELL
    custom_stop_loss: Optional[float] = None
    custom_take_profit: Optional[float] = None


@router.post("/trade/quick-buy")
async def quick_buy(req: QuickBuyRequest):
    """Buy or Sell any symbol with a dollar amount. Auto-calculates volume, SL, and TP."""
    _require_active_platform_connection()
    if active_platform == "ibkr":
        if risk_engine.panic_stopped:
            raise HTTPException(status_code=403, detail="Trading is paused")
        account = ibkr_connector.account_info
        if account and account.trade_mode != 0 and not config.LIVE_TRADING_ENABLED:
            raise HTTPException(status_code=403, detail="Live trading is disabled")
        price = await _ibkr_reference_price(req.symbol)
        if price <= 0:
            raise HTTPException(status_code=400, detail=f"Invalid IBKR quote for {req.symbol}")
        quantity = max(1.0, round(req.amount_usd / price, 4))
        result = await _run_ibkr(
            ibkr_connector.place_order,
            req.symbol,
            req.action.upper(),
            quantity,
            req.custom_stop_loss,
            req.custom_take_profit,
            _build_trade_comment(req.amount_usd, None, None),
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
            )
        return result
    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Trading is paused")

    symbol = _resolve_market_symbol(req.symbol)
    market_data.enable_symbol(symbol)
    tick = market_data.get_tick(symbol)
    if not tick:
        raise HTTPException(status_code=404, detail=f"Cannot get price for {symbol}")

    sym_info = market_data.get_symbol_info(symbol)
    if not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")

    action = req.action.upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="Action must be BUY or SELL")

    # Keep quick/manual execution fast: avoid a second full evaluation round-trip here.
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
        target_margin_usd=req.amount_usd,
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
            amount_usd=req.amount_usd,
            action=action,
            decision=decision_for_targets,
        )
        sl_amount_for_comment = sl_amount
        tp_amount_for_comment = tp_amount
        default_sl, default_tp = _derive_amount_based_sl_tp(
            action=action,
            entry_price=price,
            volume=volume,
            contract_size=contract_size,
            sl_amount_usd=sl_amount,
            tp_amount_usd=tp_amount,
            digits=digits,
        )
        if sl is None:
            sl = default_sl
        if tp is None:
            tp = default_tp

    if sl_amount_for_comment is None and sl is not None and sl > 0:
        sl_amount_for_comment = abs(price - sl) * volume * contract_size
    if tp_amount_for_comment is None and tp is not None and tp > 0:
        tp_amount_for_comment = abs(tp - price) * volume * contract_size

    # Auto-clamp SL/TP to broker minimum distance to avoid manual-trade rejections.
    sl, tp = _clamp_sl_tp_to_broker_min_distance(
        action=action,
        tick=tick,
        symbol_info=sym_info,
        stop_loss=sl,
        take_profit=tp,
    )
    required_rr = max(1.0, float(risk_engine.user_policy.min_reward_risk or 1.0))
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

    if req.amount_usd and req.amount_usd > 0:
        desired_sl_amount = sl_amount_for_comment if sl_amount_for_comment and sl_amount_for_comment > 0 else None
        adjusted_volume, rebalance_error = _rebalance_volume_for_started_amount_risk(
            entry_price=price,
            stop_loss=sl,
            desired_sl_amount_usd=desired_sl_amount,
            volume=volume,
            contract_size=contract_size,
            vol_min=vol_min,
            vol_step=vol_step,
            vol_max=vol_max,
        )
        if rebalance_error:
            raise HTTPException(status_code=409, detail=rebalance_error)
        if adjusted_volume > 0 and adjusted_volume < volume:
            volume = adjusted_volume

    # Keep click-to-order path fast: persist logs asynchronously after send.
    signal_id = None

    order_req = OrderRequest(
        symbol=symbol, action=action,
        volume=volume, stop_loss=sl, take_profit=tp,
        comment=_build_trade_comment(req.amount_usd, sl_amount_for_comment, tp_amount_for_comment),
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
        agent_name="QuickTrade",
        action=action,
        confidence=0.55,
        stop_loss=sl,
        take_profit=tp,
        max_holding_minutes=None,
        reason=f"Manual {action.lower()} request",
        strategy="manual_execution",
        metadata={},
    )
    quick_preflight = await execution_service.preflight_for_context_signal(
        context=quick_context,
        candidate_signal=quick_candidate_signal,
        volume=volume,
        reference_spread=tick.spread if tick else 0.0,
        evaluation_mode="scan",
    )
    if not quick_preflight.approved:
        raise HTTPException(status_code=409, detail=quick_preflight.reason)
    result = execution_service.place_order_if_approved(order_req, quick_preflight)

    if db:
        async def _persist_quick_buy_async():
            sid = None
            try:
                sid = await db.log_signal(
                    agent_name="QuickTrade", symbol=symbol, timeframe="manual",
                    action=action, confidence=0.5, stop_loss=sl, take_profit=tp,
                    max_holding_minutes=None, reason=f"Manual {action.lower()} ${req.amount_usd}",
                )
            except Exception as exc:
                logger.warning("Quick-buy signal persistence failed for %s: %s", symbol, exc)

            try:
                await db.log_order(
                    signal_id=sid, symbol=symbol, action=action,
                    volume=volume, price=result.price,
                    stop_loss=sl, take_profit=tp, ticket=result.ticket,
                    retcode=result.retcode, retcode_desc=result.retcode_desc,
                    success=result.success,
                    comment=order_req.comment,
                )
            except Exception as exc:
                logger.warning("Quick-buy order persistence failed for %s: %s", symbol, exc)

            if result.success and result.ticket:
                async def _store_plan_async(ticket: int, linked_sid: int | None, sym: str, side: str):
                    try:
                        decision = await signal_pipeline.evaluate(
                            symbol=sym,
                            requested_agent_name=active_agent_name,
                            requested_timeframe="H1",
                            evaluation_mode="manual",
                            bar_count=100,
                        )
                        await _store_management_plan(ticket, linked_sid, sym, side, decision)
                    except Exception as exc:
                        logger.warning("Background plan generation failed for %s #%s: %s", sym, ticket, exc)

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
async def chat_message(req: ChatRequest):
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
You are an AI trading copilot inside a demo trading app.
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

            response = await asyncio.to_thread(
                gemini_agent._client.models.generate_content,
                model="gemini-2.5-flash",
                contents=[{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=True)}]}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
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
            reply = "IBKR chat execution is not wired yet in this route. Use MT5 for chat-driven execution currently."
        else:
            tick = market_data.get_tick(symbol)
            sym_info = market_data.get_symbol_info(symbol)
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
                    decision=decision_for_targets,
                )
                # Safety: chat trades always use amount-based defaults for SL/TP,
                # derived from risk/news/quality context (not raw model prices).
                sl, tp = _derive_amount_based_sl_tp(
                    action=trade_req.action,
                    entry_price=side_price,
                    volume=volume,
                    contract_size=contract_size,
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
                        )
                    )
                    if order_result.get("success"):
                        reply = f"Trade executed: {trade_req.action} {symbol} with ${trade_req.amount_usd:.2f} margin."
            else:
                reply = f"I detected a trade request, but {symbol} is not currently tradable."

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


@router.post("/trade/execute")
async def execute_trade(req: ExecuteTradeRequest):
    _require_active_platform_connection()
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
            )
        return result

    if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(status_code=403, detail="Live trading is disabled")

    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Panic stop is active")

    if req.stop_loss == 0:
        raise HTTPException(status_code=400, detail="Stop loss is required")

    resolved_symbol = _resolve_market_symbol(req.symbol)
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
        reference_spread=market_data.get_tick(resolved_symbol).spread if market_data.get_tick(resolved_symbol) else 0.0,
        requested_agent_name=requested_agent_name,
    )

    order_req = OrderRequest(
        symbol=resolved_symbol,
        action=req.action,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
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
        )
        if result.success and result.ticket:
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
async def get_positions(symbol: Optional[str] = None):
    _require_active_platform_connection()
    if active_platform == "ibkr":
        return await _run_ibkr(ibkr_connector.get_positions, symbol)
    positions = execution.get_positions(symbol)
    return [p.model_dump() for p in positions]


class ClosePositionRequest(BaseModel):
    ticket: int


@router.post("/positions/close")
async def close_position(req: ClosePositionRequest):
    _require_active_platform_connection()
    if active_platform == "ibkr":
        result = await _run_ibkr(ibkr_connector.close_position, req.ticket)
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
    result = execution.close_position(req.ticket)

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


# --- Risk Settings Routes ---

@router.get("/risk/settings")
async def get_risk_settings():
    return _policy_settings_response()


@router.post("/risk/settings")
async def update_risk_settings(settings: UserPolicySettings):
    risk_engine.update_user_policy(settings)
    await _persist_runtime_state()
    return _policy_settings_response()


class PanicStopRequest(BaseModel):
    active: bool


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
    return await db.get_logs(limit, log_type)


@router.get("/trade-history")
async def get_trade_history(limit: int = 50):
    _t0 = time.time()
    logger.info("trade-history:start limit=%s platform=%s connected=%s", limit, active_platform, connector.connected)
    if active_platform == "ibkr" and ibkr_connector.connected:
        broker_trades = await _run_ibkr(ibkr_connector.get_recent_executions, limit=max(limit, 50))
        closed = [t for t in broker_trades if str(t.get("status", "")).lower() == "closed"]
        summary = {
            "total_trades": len(closed),
            "closed_trades": len(closed),
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
        }
        return {
            "summary": summary,
            "trades": closed[:limit],
            "source": "ibkr_broker_executions",
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

        trades.append(
            {
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": symbol,
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
    logger.info("trade-history:rows-built trades=%s elapsed_ms=%s", len(trades), int((time.time() - _t0) * 1000))

    # Realized PnL MT5 reconciliation is intentionally skipped here for responsiveness.

    closed = [t for t in trades if t.get("status") == "closed" and t.get("profit_usd") is not None]
    wins = [t for t in closed if _to_float(t.get("profit_usd")) > 0]
    losses = [t for t in closed if _to_float(t.get("profit_usd")) < 0]
    breakeven = [t for t in closed if _to_float(t.get("profit_usd")) == 0]
    total_profit = sum(_to_float(t.get("profit_usd")) for t in closed)
    total_started = sum(_to_float(t.get("started_with_usd")) for t in closed if t.get("started_with_usd") is not None)

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
        "best_trade_usd": max((_to_float(t.get("profit_usd")) for t in closed), default=0.0),
        "worst_trade_usd": min((_to_float(t.get("profit_usd")) for t in closed), default=0.0),
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
                "signal_reason": signal_reason,
                "risk_reason": risk_reason,
                "exit_reason": exit_reason,
                "trade_outcome": outcome or {},
                "journal": journal or {},
                "candidate": candidate or {},
            }
            system_prompt = """
You are a post-trade analyst for a demo trading system.
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
            response = await asyncio.to_thread(
                gemini_agent._client.models.generate_content,
                model="gemini-2.5-flash",
                contents=[{"role": "user", "parts": [{"text": json.dumps(prompt_payload, ensure_ascii=True)}]}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.15,
                    response_mime_type="application/json",
                ),
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

class SaveCredentialsRequest(BaseModel):
    account: int
    server: str
    password: str
    terminal_path: str = ""
    label: str = ""


@router.post("/credentials")
async def save_credentials(req: SaveCredentialsRequest):
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
    return {"saved": True, "backend": status.get("backend")}


@router.get("/credentials")
async def get_credentials():
    if db is None:
        return []
    creds = await db.get_saved_credentials()
    return creds


@router.delete("/credentials/{account_id}")
async def delete_credentials(account_id: int):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await db.get_saved_credential(account_id)
    if existing:
        credential_vault.delete_password(
            existing.get("secret_ref", ""),
            existing.get("secret_backend", ""),
        )
    await db.delete_saved_credentials(account_id)
    return {"deleted": True}


@router.get("/credentials/auto-connect")
async def auto_connect(account_id: Optional[int] = None):
    """Try to connect using the most recently used saved credentials."""
    global active_platform
    if db is None:
        return {"connected": False, "reason": "No database"}
    if connector.connected:
        # Handle stale MT5 IPC sessions where connector flag is true but account refresh fails.
        refreshed = connector.refresh_account()
        if refreshed is not None:
            return {
                "connected": True,
                "reason": "Already connected",
                "platform": active_platform,
                "account": refreshed.model_dump(),
            }
        connector.disconnect()

    if account_id is not None:
        selected = await db.get_saved_credential(account_id)
        creds = [selected] if selected else []
    else:
        creds = await db.get_saved_credentials()

    if not creds or creds[0] is None:
        return {"connected": False, "reason": "No saved credentials"}

    best = creds[0]  # Most recently used
    params = _build_saved_connection_params(best)
    success = connector.connect(params)
    if success:
        active_platform = "mt5"
        await db.log_connection_event("auto_connected", best["account"], best["server"])
        await db.update_credential_last_used(best["account"])
        if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
            connector.disconnect()
            return {"connected": False, "reason": "Live account rejected - demo only mode"}
        if connector.account_info:
            risk_engine.set_daily_start_equity(connector.account_info.equity)
        if risk_engine.auto_trade_enabled and not auto_trader.is_running:
            task_orchestrator.start_auto_trade()
        return {
            "connected": True,
            "platform": active_platform,
            "account": connector.account_info.model_dump() if connector.account_info else None,
        }
    return {"connected": False, "reason": connector.last_error}


# --- Smart Evaluate Routes ---

class SmartEvaluateRequest(BaseModel):
    symbols: Optional[list[str]] = None


@router.post("/agent/smart-evaluate")
async def smart_evaluate(req: SmartEvaluateRequest):
    """Scan multiple symbols. Uses fast local analysis for bulk scan, Gemini for top picks."""
    _require_active_platform_connection()

    if active_platform == "ibkr":
        symbols = req.symbols or risk_engine.settings.allowed_symbols
        market_universe = universe_service.filter_market_symbols(
            _ibkr_market_symbols(include_inactive=False),
            include_inactive=False,
        )
        if not symbols:
            symbols = universe_service.candidate_universe(market_universe)[:8]
        else:
            symbols = (universe_service.restrict_symbols(symbols))[:8]
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

    # Keep scan wide enough so categories (especially commodities) are represented.
    MAX_SCAN_SYMBOLS = 30
    # Keep per-symbol scan strict so the dashboard doesn't stall with all rows pending.
    PER_SYMBOL_TIMEOUT_SECONDS = 2.5
    # Hard cap the full scan so UI gets partial results quickly instead of waiting on stragglers.
    TOTAL_SCAN_TIMEOUT_SECONDS = 12.0
    MAX_CONCURRENT_EVALS = 8
    symbols = req.symbols or risk_engine.settings.allowed_symbols
    market_universe = universe_service.filter_market_symbols(
        market_data.get_tradeable_symbols(),
        include_inactive=False,
    )
    symbol_lookup = {
        (item.get("name") or ""): item
        for item in market_universe
        if item.get("name")
    }
    history_commission_map = execution.get_recent_commission_by_symbol()

    if not symbols:
        # Use canonical universe selection to reduce alias duplicates and include the full active set.
        symbols = universe_service.candidate_universe(market_universe)[:MAX_SCAN_SYMBOLS]
    else:
        direct = []
        for sym in symbols:
            key = str(sym or "").strip()
            if key and key in symbol_lookup:
                direct.append(key)
        symbols = (direct or universe_service.restrict_symbols(symbols))[:MAX_SCAN_SYMBOLS]

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVALS)

    async def evaluate_symbol(sym: str) -> dict | None:
        try:
            async with semaphore:
                execution_decision = await asyncio.wait_for(
                    signal_pipeline.evaluate(
                        symbol=sym,
                        requested_agent_name="SmartAgent",
                        requested_timeframe="H1",
                        # Keep bulk dashboard scan deterministic and fast (no Gemini/news round-trip).
                        evaluation_mode="scan",
                        bar_count=100,
                    ),
                    timeout=PER_SYMBOL_TIMEOUT_SECONDS,
                )
            signal_id = await signal_pipeline.persist_evaluation(execution_decision, "multi")
            signal = execution_decision.signal_decision.final_signal
            risk_decision = execution_decision.risk_evaluation
            context = execution_decision.signal_decision.market_context
            entry_price = execution_decision.entry_price
            category = context.symbol_info.category if context.symbol_info else "Other"
            description = context.symbol_info.description if context.symbol_info else ""
            ready_to_execute = execution_decision.allow_execute
            execution_reason = execution_decision.reason
            if (
                category == "Other"
                and "inactive in the current mode" in str(execution_reason).lower()
            ):
                return None

            if ready_to_execute and signal.action in {"BUY", "SELL"}:
                preflight = await execution_service.preflight_for_context_signal(
                    context=context,
                    candidate_signal=signal,
                    volume=max(risk_decision.adjusted_volume, risk_engine.settings.fixed_lot_size),
                    reference_spread=context.tick.get("spread", 0.0) if context.tick else 0.0,
                    evaluation_mode="scan",
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
            commission_snapshot = _commission_snapshot_from_symbol_info(symbol_lookup.get(sym))
            commission_snapshot = _merge_commission_snapshot(
                commission_snapshot,
                history_commission_map.get(sym),
            )

            return {
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
                "execution_reason": execution_reason,
                "recommended_amount_usd": recommended_amount_usd,
                "recommended_amount_pct_free_margin": recommended_amount_pct,
                **commission_snapshot,
            }
        except Exception as exc:
            logger.error("Smart evaluate error for %s: %r", sym, exc, exc_info=True)
            symbol_info = symbol_lookup.get(sym, {})
            return None

    tasks = [asyncio.create_task(evaluate_symbol(sym)) for sym in symbols]
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
    recommendations = [item for item in evaluations if item is not None]

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
                                requested_agent_name="SmartAgent",
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


@router.post("/trade/execute-recommendation")
async def execute_recommendation(req: ExecuteRecommendationRequest):
    """Execute a trade from a previously generated recommendation by signal_id."""
    _require_active_platform_connection()
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
        raise HTTPException(status_code=403, detail="Live trading is disabled")

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

    # Convert dollar amount to volume if provided
    tick = market_data.get_tick(symbol)
    sym_info = market_data.get_symbol_info(symbol)
    if not tick or not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get live symbol context for {symbol}")
    context = _build_fast_execution_context(
        symbol=symbol,
        tick=tick,
        symbol_info=sym_info,
        evaluation_mode="manual_fast",
    )
    if context is None:
        raise HTTPException(status_code=404, detail=f"No live context available for {symbol}")
    if req.amount_usd and req.amount_usd > 0 and sym_info and tick:
        # Keep recommendation execution responsive: do not run a full evaluate cycle here.
        decision_for_targets = None

        price = tick.ask if action == "BUY" else tick.bid
        contract_size = sym_info.get("trade_contract_size", 100000)
        vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
        vol_step = float(sym_info.get("volume_step", 0.01) or 0.01)
        vol_max = float(sym_info.get("volume_max", 0.0) or 0.0)
        if price > 0 and contract_size > 0:
            volume, estimated_margin = _solve_volume_by_margin_target_fast(
                symbol=symbol,
                action=action,
                target_margin_usd=req.amount_usd,
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
                    amount_usd=req.amount_usd,
                    action=action,
                    decision=decision_for_targets,
                )
                sl_amount_for_comment = sl_amount
                tp_amount_for_comment = tp_amount
                default_sl, default_tp = _derive_amount_based_sl_tp(
                    action=action,
                    entry_price=price,
                    volume=volume,
                    contract_size=contract_size,
                    sl_amount_usd=sl_amount,
                    tp_amount_usd=tp_amount,
                    digits=sym_info.get("digits", 5),
                )
                if req.custom_stop_loss is None:
                    sl = default_sl
                if req.custom_take_profit is None:
                    tp = default_tp

    entry_ref_price = tick.ask if action == "BUY" else tick.bid
    if req.amount_usd and req.amount_usd > 0 and tick and sym_info:
        contract_size_for_comment = float(sym_info.get("trade_contract_size", 100000) or 100000)
        if sl_amount_for_comment is None and sl is not None and sl > 0:
            sl_amount_for_comment = abs(entry_ref_price - sl) * volume * contract_size_for_comment
        if tp_amount_for_comment is None and tp is not None and tp > 0:
            tp_amount_for_comment = abs(tp - entry_ref_price) * volume * contract_size_for_comment

    # Ensure SL/TP remains valid at execution-time prices right before preflight.
    sl, tp = _clamp_sl_tp_to_broker_min_distance(
        action=action,
        tick=tick,
        symbol_info=sym_info,
        stop_loss=sl,
        take_profit=tp,
    )
    required_rr = max(1.0, float((context.user_policy or {}).get("min_reward_risk", risk_engine.user_policy.min_reward_risk or 1.0)))
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

    if req.amount_usd and req.amount_usd > 0 and sym_info:
        desired_sl_amount = sl_amount_for_comment if sl_amount_for_comment and sl_amount_for_comment > 0 else None
        adjusted_volume, rebalance_error = _rebalance_volume_for_started_amount_risk(
            entry_price=entry_ref_price,
            stop_loss=sl,
            desired_sl_amount_usd=desired_sl_amount,
            volume=volume,
            contract_size=float(sym_info.get("trade_contract_size", 100000) or 100000),
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
        if req.amount_usd and req.amount_usd > 0:
            # Preserve started-amount SL/TP semantics; never scale above user-sized volume.
            volume = min(volume, approved_volume)
        else:
            volume = approved_volume

    amt_label = _build_trade_comment(req.amount_usd, sl_amount_for_comment, tp_amount_for_comment)
    order_req = OrderRequest(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=sl or 0.0,
        take_profit=tp or 0.0,
        comment=amt_label,
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
                )
            except Exception as exc:
                logger.warning("Execute-recommendation order persistence failed for %s: %s", symbol, exc)

            if result.success and result.ticket:
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
                        await _store_management_plan(ticket, sid, sym, side, decision)
                    except Exception as exc:
                        logger.warning("Background plan generation failed for %s #%s: %s", sym, ticket, exc)

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


@router.get("/analytics/confidence-calibration")
async def confidence_calibration(limit: int = 200):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    outcomes = await db.get_trade_outcomes(limit=limit)
    return analytics_service.confidence_calibration(outcomes)


@router.get("/analytics/holding-time")
async def holding_time_analytics(limit: int = 500):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    outcomes = await db.get_trade_outcomes(limit=limit)
    return analytics_service.holding_time_analysis(outcomes)


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
    algorithm: Literal["logistic_regression", "gradient_boosting"] = "logistic_regression"
    target_column: str = "profitable_after_costs_90m"
    include_unexecuted: bool = True
    min_rows: int = 30


class ResearchReplayRequest(BaseModel):
    version_id: str
    score_threshold: float = 0.55
    include_unexecuted: bool = True
    limit: int = 200000


class WalkForwardRequest(BaseModel):
    algorithm: Literal["logistic_regression", "gradient_boosting"] = "logistic_regression"
    target_column: str = "profitable_after_costs_90m"
    score_threshold: float = 0.55
    windows: int = 5
    include_unexecuted: bool = True
    limit: int = 200000


class AttributionReportRequest(BaseModel):
    report_type: str = "full"
    limit: int = 2000


@router.get("/research/status")
async def research_status():
    service = _get_research_cycle_service()
    snapshot = await service.status_snapshot()
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
async def research_rebuild_dataset(req: DatasetRebuildRequest):
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.rebuild_dataset(
        output_name=req.output_name,
        limit=max(100, min(req.limit, 500000)),
        include_unexecuted=req.include_unexecuted,
        parquet=req.parquet,
    )


@router.post("/research/model/train")
async def research_train_model(req: TrainModelRequest):
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.train_candidate_model(
        algorithm=req.algorithm,
        target_column=req.target_column,
        include_unexecuted=req.include_unexecuted,
        min_rows=max(10, min(req.min_rows, 50000)),
    )


@router.post("/research/replay/run")
async def research_run_replay(req: ResearchReplayRequest):
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.run_replay(
        version_id=req.version_id,
        score_threshold=max(0.0, min(req.score_threshold, 1.0)),
        include_unexecuted=req.include_unexecuted,
        limit=max(100, min(req.limit, 500000)),
    )


@router.post("/research/walk-forward/run")
async def research_run_walk_forward(req: WalkForwardRequest):
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
async def research_list_models(limit: int = 50):
    service = _get_research_cycle_service()
    return {
        "models": await service.list_model_versions(limit=max(1, min(limit, 200))),
    }


@router.post("/research/models/{version_id}/approve")
async def research_approve_model(version_id: str):
    _require_research_enabled()
    service = _get_research_cycle_service()
    model = await service.approve_model(version_id)
    activated = await service.activate_approved_model()
    return {
        "approved_model": model,
        "activation": activated,
    }


@router.post("/research/models/activate-approved")
async def research_activate_approved_model():
    service = _get_research_cycle_service()
    return await service.activate_approved_model()


@router.post("/research/reports/attribution")
async def research_generate_attribution(req: AttributionReportRequest):
    _require_research_enabled()
    service = _get_research_cycle_service()
    return await service.generate_attribution_report(
        report_type=req.report_type,
        limit=max(100, min(req.limit, 100000)),
    )


@router.get("/research/reports")
async def research_list_reports(limit: int = 20):
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


@router.get("/auto-trade/status")
async def auto_trade_status():
    """Get auto-trading status and recent activity."""
    settings = risk_engine.settings
    service_status = task_orchestrator.status_snapshot()
    if active_platform == "ibkr" and ibkr_connector.connected:
        account_snapshot = await _run_ibkr(ibkr_connector.refresh_account)
        positions_snapshot = await _run_ibkr(ibkr_connector.get_positions)
        portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
            account_snapshot,
            positions_snapshot,
        )
    elif connector.connected:
        portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
            connector.refresh_account(),
            execution.get_positions(),
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
        "running": auto_trader.is_running,
        "enabled": risk_engine.auto_trade_enabled,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": risk_engine.auto_trade_scan_interval_seconds,
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
            "cooldown_seconds": int(gemini_status.get("cooldown_seconds", 0) or 0),
            "credits_pct": int(gemini_status.get("credits_pct", 0) or 0),
        },
        "event_providers": {
            "finnhub": finnhub_adapter.healthcheck(),
        },
        "services": service_status,
    }


@router.get("/auto-trade/activity")
async def auto_trade_activity(limit: int = 50):
    """Get combined AI activity feed (scanner + position manager)."""
    # In-memory combined feed
    combined = auto_trader.combined_activity[:limit]

    # Also get DB activity if available
    db_activity = []
    if db:
        db_activity = await db.get_ai_activity(limit)

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
async def auto_trade_start():
    """Enable and start auto-trading (includes position manager)."""
    _require_active_platform_connection()
    if active_platform == "ibkr":
        raise HTTPException(
            status_code=409,
            detail="IBKR auto-trading loop is not wired yet. Use MT5 for auto-trade currently.",
        )
    if not connector.is_demo() and (risk_engine.user_policy.demo_only_default or not config.LIVE_TRADING_ENABLED):
        raise HTTPException(status_code=403, detail="Auto-trading only allowed on demo accounts")

    risk_engine.auto_trade_enabled = True
    task_orchestrator.start_auto_trade()
    await _persist_runtime_state()

    if db:
        await db.log_connection_event("auto_trade_started", details="Auto-trading + Position Manager enabled")

    return {
        "running": True,
        "position_manager_running": auto_trader.position_manager.is_running,
        "services": task_orchestrator.status_snapshot(),
        "message": "Auto-trading started with AI position management",
    }


@router.post("/auto-trade/stop")
async def auto_trade_stop():
    """Disable and stop auto-trading (includes position manager)."""
    risk_engine.auto_trade_enabled = False
    task_orchestrator.stop_auto_trade()
    await _persist_runtime_state()

    if db:
        await db.log_connection_event("auto_trade_stopped", details="Auto-trading + Position Manager disabled")

    return {
        "running": False,
        "position_manager_running": False,
        "services": task_orchestrator.status_snapshot(),
        "message": "Auto-trading stopped",
    }


@router.post("/auto-trade/settings")
async def auto_trade_settings(req: AutoTradeSettingsRequest):
    """Update auto-trade settings."""
    if req.enabled is not None:
        risk_engine.auto_trade_enabled = req.enabled
        if req.enabled and not auto_trader.is_running and connector.connected:
            task_orchestrator.start_auto_trade()
        elif not req.enabled and auto_trader.is_running:
            task_orchestrator.stop_auto_trade()

    if req.min_confidence is not None:
        raise HTTPException(
            status_code=400,
            detail="Minimum confidence is mode-derived now. Change the policy preset instead.",
        )

    if req.scan_interval is not None:
        if req.scan_interval < 30:
            raise HTTPException(status_code=400, detail="Scan interval must be at least 30 seconds")
        risk_engine.auto_trade_scan_interval_seconds = req.scan_interval

    await _persist_runtime_state()

    settings = risk_engine.settings
    return {
        "enabled": risk_engine.auto_trade_enabled,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": risk_engine.auto_trade_scan_interval_seconds,
        "services": task_orchestrator.status_snapshot(),
    }
