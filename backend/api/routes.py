from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging
import time

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
    tradeable_only: bool = True,
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

    # Auto-calculate SL and TP using ATR-like approach
    h1_bars = market_data.get_bars(symbol, "H1", 20)
    if h1_bars and len(h1_bars) >= 5:
        ranges = [b.high - b.low for b in h1_bars[-14:]]
        atr = sum(ranges) / len(ranges)
    else:
        atr = price * 0.01  # Default 1% if no bars

    if is_buy:
        sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else round(price - atr * 1.5, sym_info.get("digits", 5))
        tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else round(price + atr * 2.0, sym_info.get("digits", 5))
    else:  # SELL
        sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else round(price + atr * 1.5, sym_info.get("digits", 5))
        tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else round(price - atr * 2.0, sym_info.get("digits", 5))

    # Validate SL/TP minimum distance (STOPLEVEL + spread)
    point = sym_info.get("point", 0.00001)
    stops_level = sym_info.get("trade_stops_level", 0)
    spread_points = tick.spread if tick else 10
    effective_stops = max(stops_level, spread_points, 10)
    min_distance = effective_stops * point * 1.5  # 50% buffer for spread fluctuation
    digits = sym_info.get("digits", 5)

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
        comment=f"TA:${int(req.amount_usd)}",
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
        return []
    return await db.get_trade_history(limit)


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

    symbols = req.symbols or risk_engine.settings.allowed_symbols

    # If no symbols configured, use defaults but don't overwrite settings
    if not symbols:
        logger.info("No symbols configured, using active event-driven universe from MT5 for scan...")
        tradeable = universe_service.filter_market_symbols(market_data.get_tradeable_symbols())
        if tradeable:
            symbols = universe_service.candidate_universe(tradeable)
        else:
            visible = universe_service.filter_market_symbols(
                [s for s in market_data.get_visible_symbols() if s["trade_enabled"]]
            )
            symbols = universe_service.candidate_universe(visible)
        # NOTE: Don't save to settings — manual scan should show everything
        # but auto-trade uses only the curated allowed_symbols list
    else:
        symbols = universe_service.restrict_symbols(symbols)

    recommendations = []
    for sym in symbols:
        try:
            execution_decision = await signal_pipeline.evaluate(
                symbol=sym,
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="scan",
                bar_count=100,
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
        except Exception as e:
            logger.error(f"Smart evaluate error for {sym}: {e}")
            continue

    # Sort by trade quality, actionable first
    recommendations.sort(
        key=lambda r: (
            r["ready_to_execute"],
            r.get("trade_quality", {}).get("final_trade_quality_score", 0.0),
            r["signal"]["confidence"],
        ),
        reverse=True,
    )

    import time
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

    amt_label = f"TA:${int(req.amount_usd)}" if req.amount_usd and req.amount_usd > 0 else "TradingAgent"
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
