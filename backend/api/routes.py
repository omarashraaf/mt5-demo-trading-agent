from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
import logging
import time
import asyncio
import json
import re

from adapters.finnhub_adapter import FinnhubAdapter
from mt5.connector import MT5Connector, ConnectionParams
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.mock_agent import MockAgent
from agent.sma_crossover_agent import SMACrossoverAgent
from agent.smart_agent import SmartAgent
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
from services.trade_decision_service import TradeDecisionService
from domain.models import MarketContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Shared state
connector = MT5Connector()
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
RUNTIME_STATE_KEY = "runtime_controls_v1"
CHAT_HISTORY_STATE_KEY = "chat_history_v1"
DEFAULT_MARGIN_SL_PCT = 0.08
DEFAULT_MARGIN_TP_PCT = 0.12


def set_database(database: Database):
    global db
    db = database
    signal_pipeline.set_database(database)
    execution_service.db = database
    auto_trader.set_database(database)
    event_ingestion_service.set_database(database)


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
        interval = int(state.get("auto_trade_scan_interval_seconds", 60))
        risk_engine.auto_trade_scan_interval_seconds = max(30, interval)
        logger.info("Restored runtime state: mode=%s, auto_trade_enabled=%s, scan_interval=%ss",
                    risk_engine.user_policy.mode,
                    risk_engine.auto_trade_enabled,
                    risk_engine.auto_trade_scan_interval_seconds)
    except Exception as exc:
        logger.warning("Failed to restore runtime state: %s", exc)


def _requested_agent_from_final_name(agent_name: str) -> str:
    return "GeminiAgent" if "Gemini" in (agent_name or "") else "SmartAgent"


def _resolve_market_symbol(symbol: str) -> str:
    tradeable = market_data.get_tradeable_symbols()
    visible = market_data.get_visible_symbols() if not tradeable else []
    resolved = universe_service.resolve_requested_symbols([symbol], tradeable + visible)
    return resolved[0] if resolved else symbol


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
        evaluation_mode="manual",
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


# --- Connection Routes ---

class ConnectRequest(BaseModel):
    account: int
    password: str
    server: str
    terminal_path: Optional[str] = None
    save_credentials: bool = False


@router.post("/connect")
async def connect(req: ConnectRequest):
    raw_path = (req.terminal_path or config.DEFAULT_TERMINAL_PATH).strip().strip('"').strip("'")
    params = ConnectionParams(
        account=req.account,
        password=req.password,
        server=req.server,
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

        # Set daily start equity for risk engine
        if connector.account_info:
            risk_engine.set_daily_start_equity(connector.account_info.equity)

        credential_status = await _persist_credentials_if_requested(
            account=req.account,
            server=req.server,
            password=req.password,
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
        raise HTTPException(status_code=400, detail=connector.last_error)

    return {
        "connected": True,
        "account": connector.account_info.model_dump() if connector.account_info else None,
        "is_demo": connector.is_demo(),
        "credential_status": credential_status,
    }


@router.post("/disconnect")
async def disconnect():
    success = connector.disconnect()
    if db:
        await db.log_connection_event("disconnected")
    return {"disconnected": success}


@router.get("/status")
async def status():
    account = connector.refresh_account()
    terminal = connector.get_terminal_info()
    portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
        account,
        execution.get_positions(),
    ) if connector.connected else {
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
        "connected": connector.connected,
        "account": account.model_dump() if account else None,
        "terminal": terminal.model_dump() if terminal else None,
        "last_error": connector.last_error,
        "is_demo": connector.is_demo(),
        "live_trading_enabled": config.LIVE_TRADING_ENABLED,
        "panic_stop": risk_engine.panic_stopped,
        "active_agent": active_agent_name,
        "credential_storage_available": credential_vault.available,
        "gemini_available": gemini_agent.available,
        "gemini_degraded": gemini_adapter.degraded,
        "gemini_last_error": gemini_adapter.last_error,
        "user_policy": risk_engine.user_policy.model_dump(),
        "runtime_controls": risk_engine.runtime_controls(),
        "universe": universe_service.summary_dict(),
        "finnhub": finnhub_adapter.healthcheck(),
        "portfolio": portfolio_snapshot,
        "services": task_orchestrator.status_snapshot(),
    }


@router.get("/account")
async def account():
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    info = connector.refresh_account()
    if info is None:
        raise HTTPException(status_code=500, detail="Failed to get account info")
    return info.model_dump()


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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    tick = market_data.get_tick(_resolve_market_symbol(symbol))
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No tick data for {symbol}")
    return tick.model_dump()


@router.get("/market/bars/{symbol}")
async def get_bars(symbol: str, timeframe: str = "H1", count: int = 100):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    bars = market_data.get_bars(_resolve_market_symbol(symbol), timeframe, count)
    return [b.model_dump() for b in bars]


@router.get("/market/symbol-info/{symbol}")
async def get_symbol_info(symbol: str):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    info = market_data.get_symbol_info(_resolve_market_symbol(symbol))
    if info is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    return info


@router.get("/market/available-symbols")
async def get_available_symbols(
    category: Optional[str] = None,
    tradeable_only: bool = False,
    include_inactive: bool = False,
):
    """Get all symbols available on the MT5 terminal, optionally filtered by category."""
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    if tradeable_only:
        symbols = market_data.get_tradeable_symbols()
    else:
        symbols = market_data.get_all_symbols()

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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    target_categories = [universe_service.normalize_asset_class(item) for item in (categories or universe_service.summary().active_asset_classes)]
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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

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

    The amount_usd is treated as margin: with leverage, it controls a larger position.
    volume = amount_usd * leverage / (price * contract_size)
    """
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    resolved_symbol = _resolve_market_symbol(symbol)
    tick = market_data.get_tick(resolved_symbol)
    sym_info = market_data.get_symbol_info(resolved_symbol)
    if not tick or not sym_info:
        raise HTTPException(status_code=404, detail=f"Cannot get info for {symbol}")

    # Get account leverage
    account = connector.refresh_account()
    leverage = account.leverage if account else 1

    price = tick.ask
    contract_size = sym_info.get("trade_contract_size", 100000)
    vol_min = sym_info.get("volume_min", 0.01)
    vol_max = sym_info.get("volume_max", 100)
    vol_step = sym_info.get("volume_step", 0.01)

    if price <= 0 or contract_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid price or contract size")

    # amount_usd is margin → multiply by leverage to get notional, then divide by lot value
    raw_volume = (amount_usd * leverage) / (price * contract_size)
    if vol_step > 0:
        raw_volume = round(raw_volume / vol_step) * vol_step
    volume = max(raw_volume, vol_min)
    volume = min(volume, vol_max)

    # Actual margin required for this volume
    margin_required = round((volume * price * contract_size) / leverage, 2)

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
        "leverage": leverage,
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
    parts = [f"TA:${float(amount_usd):.2f}"]
    if sl_amount_usd is not None and sl_amount_usd > 0:
        parts.append(f"SLA:${float(sl_amount_usd):.2f}")
    if tp_amount_usd is not None and tp_amount_usd > 0:
        parts.append(f"TPA:${float(tp_amount_usd):.2f}")
    return "|".join(parts)


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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
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
        logger.warning("Could not build dynamic SL/TP context for quick trade %s: %s", symbol, exc)

    is_buy = action == "BUY"
    price = tick.ask if is_buy else tick.bid
    contract_size = sym_info.get("trade_contract_size", 100000)
    vol_min = sym_info.get("volume_min", 0.01)
    vol_step = sym_info.get("volume_step", 0.01)

    # Get account leverage
    account = connector.refresh_account()
    leverage = account.leverage if account else 1

    # Calculate volume from dollar amount (margin-based with leverage)
    if price <= 0 or contract_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid price data")

    raw_volume = (req.amount_usd * leverage) / (price * contract_size)
    if vol_step > 0:
        raw_volume = round(raw_volume / vol_step) * vol_step
    volume = max(raw_volume, vol_min)

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

    # Validate SL/TP minimum distance (STOPLEVEL + spread)
    point = sym_info.get("point", 0.00001)
    stops_level = sym_info.get("trade_stops_level", 0)
    spread_points = tick.spread if tick else 10
    effective_stops = max(stops_level, spread_points, 10)
    min_distance = effective_stops * point * 1.5  # 50% buffer for spread fluctuation
    if sl and abs(price - sl) < min_distance:
        min_sl_dollars = round(min_distance * volume * contract_size, 2)
        raise HTTPException(
            status_code=400,
            detail=f"Stop Loss too close to price. Minimum ~${min_sl_dollars:.2f} for your position. Try a larger SL amount."
        )
    if tp and abs(tp - price) < min_distance:
        min_tp_dollars = round(min_distance * volume * contract_size, 2)
        raise HTTPException(
            status_code=400,
            detail=f"Take Profit too close to price. Minimum ~${min_tp_dollars:.2f} for your position. Try a larger TP amount."
        )

    # Log signal
    signal_id = None
    if db:
        signal_id = await db.log_signal(
            agent_name="QuickTrade", symbol=symbol, timeframe="manual",
            action=action, confidence=0.5, stop_loss=sl, take_profit=tp,
            max_holding_minutes=None, reason=f"Manual {action.lower()} ${req.amount_usd}",
        )

    order_req = OrderRequest(
        symbol=symbol, action=action,
        volume=volume, stop_loss=sl, take_profit=tp,
        comment=_build_trade_comment(req.amount_usd, sl_amount_for_comment, tp_amount_for_comment),
    )
    quick_preflight = await execution_service.preflight(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=sl,
        take_profit=tp,
        reference_spread=tick.spread if tick else 0.0,
        requested_agent_name=active_agent_name,
        requested_timeframe="H1",
        evaluation_mode="manual",
    )
    if not quick_preflight.approved:
        raise HTTPException(status_code=409, detail=quick_preflight.reason)
    result = execution_service.place_order_if_approved(order_req, quick_preflight)

    if db:
        await db.log_order(
            signal_id=signal_id, symbol=symbol, action=action,
            volume=volume, price=result.price,
            stop_loss=sl, take_profit=tp, ticket=result.ticket,
            retcode=result.retcode, retcode_desc=result.retcode_desc,
            success=result.success,
            comment=order_req.comment,
        )
        if result.success and result.ticket:
            decision = await signal_pipeline.evaluate(
                symbol=symbol,
                requested_agent_name=active_agent_name,
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=100,
            )
            await _store_management_plan(result.ticket, signal_id, symbol, action, decision)
            if signal_id:
                await db.mark_evaluation_outcome(signal_id, "opened")

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

        if not connector.connected:
            reply = "Trade request detected, but MT5 is not connected. Connect first, then confirm execution."
        else:
            tick = market_data.get_tick(symbol)
            sym_info = market_data.get_symbol_info(symbol)
            if tick and sym_info:
                side_price = tick.ask if trade_req.action == "BUY" else tick.bid
                account_info = connector.refresh_account()
                leverage = account_info.leverage if account_info else 1
                contract_size = sym_info.get("trade_contract_size", 100000)
                vol_step = sym_info.get("volume_step", 0.01)
                vol_min = sym_info.get("volume_min", 0.01)

                raw_volume = (
                    (trade_req.amount_usd * leverage) / (side_price * contract_size)
                    if side_price > 0 and contract_size > 0
                    else vol_min
                )
                if vol_step > 0:
                    raw_volume = round(raw_volume / vol_step) * vol_step
                volume = max(raw_volume, vol_min)

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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

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

    return result.model_dump()


@router.get("/positions")
async def get_positions(symbol: Optional[str] = None):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    positions = execution.get_positions(symbol)
    return [p.model_dump() for p in positions]


class ClosePositionRequest(BaseModel):
    ticket: int


@router.post("/positions/close")
async def close_position(req: ClosePositionRequest):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

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
            await db.log_trade_outcome(
                ticket=req.ticket,
                signal_id=signal_id,
                symbol=outcome_context["symbol"],
                action=outcome_context["action"],
                confidence=float(signal.get("confidence", 0.0)) if signal else 0.0,
                profit=existing_position.profit if existing_position else 0.0,
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

    raw_orders = await db.get_trade_history(max(limit * 4, 100))
    outcomes = await db.get_trade_outcomes(max(limit * 4, 100))

    account = connector.refresh_account() if connector.connected else None
    leverage = float(getattr(account, "leverage", 100) or 100)

    def _to_float(v, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _extract_started_amount(comment: str | None, signal_reason: str | None):
        for text in (comment or "", signal_reason or ""):
            m = re.search(r"TA:\$(\d+(?:\.\d+)?)|\$(\d+(?:\.\d+)?)", text or "", re.IGNORECASE)
            if m:
                value = m.group(1) or m.group(2)
                if value:
                    return float(value), "provided"
        return None, "unknown"

    def _extract_comment_named_amount(comment: str | None, key: str) -> Optional[float]:
        m = re.search(rf"{key}:\$(\d+(?:\.\d+)?)", comment or "", re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1))
        except Exception:
            return None

    def _duration_minutes(opened_at: float, closed_at: Optional[float]) -> Optional[float]:
        if opened_at <= 0:
            return None
        if closed_at is None:
            return max(0.0, (time.time() - opened_at) / 60.0)
        return max(0.0, (closed_at - opened_at) / 60.0)

    outcome_by_ticket: dict[int, dict] = {}
    outcome_by_signal: dict[int, dict] = {}
    for outcome in outcomes:
        ticket = outcome.get("ticket")
        signal_id = outcome.get("signal_id")
        if isinstance(ticket, int) and ticket not in outcome_by_ticket:
            outcome_by_ticket[ticket] = outcome
        if isinstance(signal_id, int) and signal_id not in outcome_by_signal:
            outcome_by_signal[signal_id] = outcome

    trades: list[dict] = []
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

        closed_at = _to_float(outcome.get("closed_at"), 0.0) if outcome else 0.0
        closed_at_value = closed_at if closed_at > 0 else None
        profit = _to_float(outcome.get("profit"), 0.0) if outcome else None
        status = "closed" if outcome else "open"
        exit_reason = str(outcome.get("exit_reason", "")) if outcome else ""

        info = market_data.get_symbol_info(symbol) or {}
        contract_size = _to_float(info.get("trade_contract_size"), 0.0)
        if contract_size <= 0:
            contract_size = None

        started_with, started_source = _extract_started_amount(row.get("comment"), row.get("signal_reason"))
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
        if sl_amount is None and stop_loss > 0 and entry_price > 0 and volume > 0 and contract_size is not None:
            sl_amount = abs(entry_price - stop_loss) * volume * contract_size
        if tp_amount is None and take_profit > 0 and entry_price > 0 and volume > 0 and contract_size is not None:
            tp_amount = abs(take_profit - entry_price) * volume * contract_size
        if started_with and started_with > 0 and sl_amount is not None:
            sl_pct = (sl_amount / started_with) * 100.0
        if started_with and started_with > 0 and tp_amount is not None:
            tp_pct = (tp_amount / started_with) * 100.0

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
                "exit_reason": exit_reason or None,
            }
        )
        if len(trades) >= limit:
            break

    closed = [t for t in trades if t.get("status") == "closed" and t.get("profit_usd") is not None]
    wins = [t for t in closed if _to_float(t.get("profit_usd")) > 0]
    losses = [t for t in closed if _to_float(t.get("profit_usd")) < 0]
    breakeven = [t for t in closed if _to_float(t.get("profit_usd")) == 0]
    total_profit = sum(_to_float(t.get("profit_usd")) for t in closed)
    total_started = sum(_to_float(t.get("started_with_usd")) for t in closed if t.get("started_with_usd") is not None)

    summary = {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len([t for t in trades if t.get("status") == "open"]),
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

    return {"summary": summary, "trades": trades}


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
    if db is None:
        return {"connected": False, "reason": "No database"}
    if connector.connected:
        return {"connected": True, "reason": "Already connected"}

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
            "account": connector.account_info.model_dump() if connector.account_info else None,
        }
    return {"connected": False, "reason": connector.last_error}


# --- Smart Evaluate Routes ---

class SmartEvaluateRequest(BaseModel):
    symbols: Optional[list[str]] = None


@router.post("/agent/smart-evaluate")
async def smart_evaluate(req: SmartEvaluateRequest):
    """Scan multiple symbols. Uses fast local analysis for bulk scan, Gemini for top picks."""
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    MAX_SCAN_SYMBOLS = 80
    symbols = req.symbols or risk_engine.settings.allowed_symbols
    market_universe = universe_service.filter_market_symbols(
        market_data.get_all_symbols(),
        include_inactive=False,
    )
    symbol_lookup = {
        (item.get("name") or ""): item
        for item in market_universe
        if item.get("name")
    }

    if not symbols:
        symbols = [item.get("name", "") for item in market_universe if item.get("name")]
        symbols = [name for name in symbols if name][:MAX_SCAN_SYMBOLS]
    else:
        symbols = universe_service.restrict_symbols(symbols)[:MAX_SCAN_SYMBOLS]

    recommendations = []
    for sym in symbols:
        try:
            execution_decision = await asyncio.wait_for(
                signal_pipeline.evaluate(
                    symbol=sym,
                    requested_agent_name="SmartAgent",
                    requested_timeframe="H1",
                    evaluation_mode="scan",
                    bar_count=100,
                ),
                timeout=6.0,
            )
            signal_id = await signal_pipeline.persist_evaluation(execution_decision, "multi")
            signal = execution_decision.signal_decision.final_signal
            risk_decision = execution_decision.risk_evaluation
            context = execution_decision.signal_decision.market_context
            entry_price = execution_decision.entry_price
            category = context.symbol_info.category if context.symbol_info else "Other"
            description = context.symbol_info.description if context.symbol_info else ""

            recommendations.append({
                "symbol": sym,
                "signal": signal.to_trade_signal().model_dump(),
                "signal_id": signal_id,
                "risk_decision": risk_decision.model_dump(),
                "entry_price_estimate": entry_price,
                "explanation": signal.reason,
                "ready_to_execute": execution_decision.allow_execute,
                "category": category,
                "description": description,
                "degraded_reasons": execution_decision.signal_decision.degraded_reasons,
                "trade_quality": execution_decision.trade_quality_assessment.model_dump(),
                "portfolio_risk": execution_decision.portfolio_risk_assessment.model_dump(),
                "anti_churn": execution_decision.anti_churn_assessment.model_dump(),
                "gemini_confirmation": execution_decision.signal_decision.gemini_confirmation.model_dump()
                if execution_decision.signal_decision.gemini_confirmation
                else None,
                "execution_reason": execution_decision.reason,
            })
        except Exception as exc:
            logger.error("Smart evaluate error for %s: %s", sym, exc)
            symbol_info = symbol_lookup.get(sym, {})
            recommendations.append({
                "symbol": sym,
                "signal": {
                    "action": "HOLD",
                    "confidence": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "max_holding_minutes": None,
                    "reason": "No executable market data right now.",
                },
                "signal_id": None,
                "risk_decision": {
                    "approved": False,
                    "reason": "No executable market data right now.",
                    "adjusted_volume": 0.0,
                    "warnings": [],
                },
                "entry_price_estimate": float(symbol_info.get("bid") or 0.0),
                "explanation": "Symbol is listed, but cannot be executed now (market closed or missing live quote/history).",
                "ready_to_execute": False,
                "category": universe_service.normalize_asset_class(symbol_info.get("category")),
                "description": symbol_info.get("description", ""),
                "degraded_reasons": ["scan_error"],
                "gemini_confirmation": None,
                "execution_reason": "Symbol unavailable for execution in current market conditions.",
            })

    recommendations.sort(
        key=lambda r: (
            r["ready_to_execute"],
            r.get("trade_quality", {}).get("final_trade_quality_score", 0.0),
            r["signal"]["confidence"],
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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
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
    decision = await signal_pipeline.evaluate(
        symbol=symbol,
        requested_agent_name=requested_agent_name,
        requested_timeframe=signal_data.get("timeframe", "H1"),
        evaluation_mode="manual",
        bar_count=100,
    )
    current_signal = decision.signal_decision.final_signal
    action = current_signal.action
    if action not in ("BUY", "SELL") or not decision.allow_execute:
        return {
            "success": False,
            "retcode": -1,
            "retcode_desc": decision.reason,
            "ticket": None,
            "volume": None,
            "price": None,
            "stop_loss": current_signal.stop_loss,
            "take_profit": current_signal.take_profit,
            "comment": "",
        }

    sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else current_signal.stop_loss
    tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else current_signal.take_profit
    volume = decision.risk_evaluation.adjusted_volume or risk_engine.settings.fixed_lot_size
    sl_amount_for_comment: float | None = None
    tp_amount_for_comment: float | None = None

    # Convert dollar amount to volume if provided
    tick = market_data.get_tick(symbol)
    sym_info = market_data.get_symbol_info(symbol)
    if req.amount_usd and req.amount_usd > 0 and sym_info and tick:
        price = tick.ask if action == "BUY" else tick.bid
        contract_size = sym_info.get("trade_contract_size", 100000)
        vol_min = sym_info.get("volume_min", 0.01)
        vol_step = sym_info.get("volume_step", 0.01)
        if price > 0 and contract_size > 0:
            acct = connector.refresh_account()
            lev = acct.leverage if acct else 1
            raw_volume = (req.amount_usd * lev) / (price * contract_size)
            if vol_step > 0:
                raw_volume = round(raw_volume / vol_step) * vol_step
            volume = max(raw_volume, vol_min)
            logger.info(
                "Converted $%s margin (leverage %sx) to %s lots for %s",
                req.amount_usd,
                lev,
                volume,
                symbol,
            )
            if req.custom_stop_loss is None or req.custom_take_profit is None:
                sl_amount, tp_amount = await _derive_dynamic_amount_targets(
                    amount_usd=req.amount_usd,
                    action=action,
                    decision=decision,
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

    if req.amount_usd and req.amount_usd > 0 and tick and sym_info:
        entry_ref_price = tick.ask if action == "BUY" else tick.bid
        contract_size_for_comment = float(sym_info.get("trade_contract_size", 100000) or 100000)
        if sl_amount_for_comment is None and sl is not None and sl > 0:
            sl_amount_for_comment = abs(entry_ref_price - sl) * volume * contract_size_for_comment
        if tp_amount_for_comment is None and tp is not None and tp > 0:
            tp_amount_for_comment = abs(tp - entry_ref_price) * volume * contract_size_for_comment

    execution_guard = await _pre_execution_checks(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=sl,
        take_profit=tp,
        reference_spread=decision.signal_decision.market_context.tick.get("spread", 0.0)
        if decision.signal_decision.market_context.tick
        else 0.0,
        requested_agent_name=requested_agent_name,
    )

    # Validate SL/TP minimum distance (STOPLEVEL + spread)
    if tick and sym_info:
        exec_price = tick.ask if action == "BUY" else tick.bid
        point = sym_info.get("point", 0.00001)
        stops_level = sym_info.get("trade_stops_level", 0)
        spread_points = tick.spread if tick.spread else 10
        effective_stops = max(stops_level, spread_points, 10)
        min_distance = effective_stops * point * 1.5
        contract_size = sym_info.get("trade_contract_size", 100000)
        if sl and abs(exec_price - sl) < min_distance:
            min_sl_dollars = round(min_distance * volume * contract_size, 2)
            raise HTTPException(
                status_code=400,
                detail=f"Stop Loss too close to price. Minimum ~${min_sl_dollars:.2f} for your position.",
            )
        if tp and abs(tp - exec_price) < min_distance:
            min_tp_dollars = round(min_distance * volume * contract_size, 2)
            raise HTTPException(
                status_code=400,
                detail=f"Take Profit too close to price. Minimum ~${min_tp_dollars:.2f} for your position.",
            )

    amt_label = _build_trade_comment(req.amount_usd, sl_amount_for_comment, tp_amount_for_comment)
    order_req = OrderRequest(
        symbol=symbol,
        action=action,
        volume=volume,
        stop_loss=sl or 0.0,
        take_profit=tp or 0.0,
        comment=amt_label,
    )
    result = execution_service.place_order_if_approved(order_req, execution_guard["preflight"])

    if db:
        await db.log_order(
            signal_id=req.signal_id, symbol=symbol, action=action,
            volume=order_req.volume, price=result.price,
            stop_loss=sl, take_profit=tp, ticket=result.ticket,
            retcode=result.retcode, retcode_desc=result.retcode_desc,
            success=result.success,
            comment=order_req.comment,
        )
        if result.success and result.ticket:
            await db.log_position_change(
                result.ticket, "opened", symbol,
                f"{action} {order_req.volume} lots at {result.price}"
            )
            await _store_management_plan(result.ticket, req.signal_id, symbol, action, decision)
            await db.mark_evaluation_outcome(req.signal_id, "opened")

    return result.model_dump()


class ReplayRequest(BaseModel):
    symbol: str
    steps: int = 20
    with_gemini: bool = True


@router.post("/replay/run")
async def run_replay(req: ReplayRequest):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

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
    portfolio_snapshot = signal_pipeline.portfolio_risk_service.snapshot(
        connector.refresh_account(),
        execution.get_positions(),
    ) if connector.connected else {}
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
            "available": gemini_agent.available,
            "degraded": gemini_adapter.degraded,
            "last_error": gemini_adapter.last_error,
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
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected to MT5")
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
