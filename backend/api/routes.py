from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging

from mt5.connector import MT5Connector, ConnectionParams
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.interface import AgentInput
from agent.mock_agent import MockAgent
from agent.sma_crossover_agent import SMACrossoverAgent
from agent.smart_agent import SmartAgent
from agent.gemini_agent import GeminiAgent
from risk.rules import RiskEngine, RiskSettings
from storage.db import Database
from config import config

logger = logging.getLogger(__name__)

router = APIRouter()

# Shared state
connector = MT5Connector()
market_data = MarketDataService()
execution = ExecutionEngine()
risk_engine = RiskEngine()
db: Optional[Database] = None

# Agent registry - Gemini is the default
gemini_agent = GeminiAgent()
agents = {
    "GeminiAgent": gemini_agent,
    "SmartAgent": SmartAgent(),
    "MockAgent": MockAgent(),
    "SMA_Crossover": SMACrossoverAgent(),
}
active_agent_name = "GeminiAgent"

# Auto-trader
from auto_trader import AutoTrader
auto_trader = AutoTrader(
    connector=connector,
    market_data=market_data,
    execution=execution,
    risk_engine=risk_engine,
    agents=agents,
)


def set_database(database: Database):
    global db
    db = database
    auto_trader.set_database(database)


# --- Connection Routes ---

class ConnectRequest(BaseModel):
    account: int
    password: str
    server: str
    terminal_path: Optional[str] = None
    save_credentials: bool = True


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

    if success and db:
        await db.log_connection_event("connected", req.account, req.server)

        # Check if demo account
        if not connector.is_demo() and not config.LIVE_TRADING_ENABLED:
            connector.disconnect()
            await db.log_connection_event(
                "rejected", req.account, req.server,
                "Live account rejected - demo only mode"
            )
            raise HTTPException(
                status_code=403,
                detail="Live trading is disabled. Only demo accounts are allowed. Set LIVE_TRADING_ENABLED=true in config to enable (UNSAFE).",
            )

        # Set daily start equity for risk engine
        if connector.account_info:
            risk_engine.set_daily_start_equity(connector.account_info.equity)

        # Auto-save credentials
        if req.save_credentials:
            await db.save_credentials(
                req.account, req.server, req.password, raw_path,
            )

    if not success:
        if db:
            await db.log_error("connection", connector.last_error or "Unknown error")
        raise HTTPException(status_code=400, detail=connector.last_error)

    return {
        "connected": True,
        "account": connector.account_info.model_dump() if connector.account_info else None,
        "is_demo": connector.is_demo(),
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
    return {
        "connected": connector.connected,
        "account": account.model_dump() if account else None,
        "terminal": terminal.model_dump() if terminal else None,
        "last_error": connector.last_error,
        "is_demo": connector.is_demo(),
        "live_trading_enabled": config.LIVE_TRADING_ENABLED,
        "panic_stop": risk_engine.panic_stopped,
        "active_agent": active_agent_name,
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
        ok = market_data.enable_symbol(sym)
        results[sym] = ok
    return results


@router.get("/market/tick/{symbol}")
async def get_tick(symbol: str):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    tick = market_data.get_tick(symbol)
    if tick is None:
        raise HTTPException(status_code=404, detail=f"No tick data for {symbol}")
    return tick.model_dump()


@router.get("/market/bars/{symbol}")
async def get_bars(symbol: str, timeframe: str = "H1", count: int = 100):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    bars = market_data.get_bars(symbol, timeframe, count)
    return [b.model_dump() for b in bars]


@router.get("/market/symbol-info/{symbol}")
async def get_symbol_info(symbol: str):
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    info = market_data.get_symbol_info(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    return info


@router.get("/market/available-symbols")
async def get_available_symbols(category: Optional[str] = None, tradeable_only: bool = True):
    """Get all symbols available on the MT5 terminal, optionally filtered by category."""
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    if tradeable_only:
        symbols = market_data.get_tradeable_symbols()
    else:
        symbols = market_data.get_all_symbols()

    if category:
        symbols = [s for s in symbols if s["category"].lower() == category.lower()]

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
    }


@router.post("/market/auto-detect-symbols")
async def auto_detect_symbols(categories: Optional[list[str]] = None):
    """Auto-detect tradeable symbols and update allowed_symbols list."""
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected")

    target_categories = categories or ["Crypto", "Stocks", "Indices", "Commodities"]
    tradeable = market_data.get_tradeable_symbols()
    matched = [s["name"] for s in tradeable if s["category"] in target_categories]

    if not matched:
        # Fallback: use visible symbols from MarketWatch
        visible = market_data.get_visible_symbols()
        matched = [s["name"] for s in visible if s["trade_enabled"]]

    # Update risk settings with detected symbols
    risk_engine.settings.allowed_symbols = matched
    logger.info(f"Auto-detected {len(matched)} tradeable symbols: {matched}")

    return {
        "detected": matched,
        "count": len(matched),
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
    agent = agents.get(agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    # Gather data
    market_data.enable_symbol(req.symbol)
    bars = market_data.get_bars(req.symbol, req.timeframe, req.bar_count)
    tick = market_data.get_tick(req.symbol)
    account = connector.refresh_account()
    positions = execution.get_positions(req.symbol)

    if not bars:
        raise HTTPException(status_code=400, detail="No bar data available")

    input_data = AgentInput(
        symbol=req.symbol,
        timeframe=req.timeframe,
        bars=[b.model_dump() for b in bars],
        spread=tick.spread if tick else 0,
        account_equity=account.equity if account else 0,
        open_positions=[p.model_dump() for p in positions],
    )

    signal = agent.evaluate(input_data)

    # Log signal
    signal_id = None
    if db:
        signal_id = await db.log_signal(
            agent_name=agent.name,
            symbol=req.symbol,
            timeframe=req.timeframe,
            action=signal.action,
            confidence=signal.confidence,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            max_holding_minutes=signal.max_holding_minutes,
            reason=signal.reason,
        )

    # Run risk check
    risk_decision = risk_engine.evaluate(
        signal=signal,
        symbol=req.symbol,
        spread=tick.spread if tick else 999,
        equity=account.equity if account else 0,
        open_positions=positions,
    )

    if db and signal_id:
        await db.log_risk_decision(
            signal_id=signal_id,
            approved=risk_decision.approved,
            reason=risk_decision.reason,
            adjusted_volume=risk_decision.adjusted_volume,
        )

    return {
        "signal": signal.model_dump(),
        "signal_id": signal_id,
        "risk_decision": risk_decision.model_dump(),
        "agent_name": agent.name,
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

    tick = market_data.get_tick(symbol)
    sym_info = market_data.get_symbol_info(symbol)
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
        "symbol": symbol,
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

    symbol = req.symbol
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
    result = execution.place_order(order_req)

    if db:
        await db.log_order(
            signal_id=signal_id, symbol=symbol, action=action,
            volume=volume, price=result.price,
            stop_loss=sl, take_profit=tp, ticket=result.ticket,
            retcode=result.retcode, retcode_desc=result.retcode_desc,
            success=result.success,
        )

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

    if not connector.is_demo() and not config.LIVE_TRADING_ENABLED:
        raise HTTPException(status_code=403, detail="Live trading is disabled")

    if risk_engine.panic_stopped:
        raise HTTPException(status_code=403, detail="Panic stop is active")

    if req.stop_loss == 0:
        raise HTTPException(status_code=400, detail="Stop loss is required")

    order_req = OrderRequest(
        symbol=req.symbol,
        action=req.action,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
    )

    result = execution.place_order(order_req)

    if db:
        await db.log_order(
            signal_id=req.signal_id,
            symbol=req.symbol,
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
                result.ticket, "opened", req.symbol,
                f"{req.action} {req.volume} lots at {result.price}"
            )

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

    return result.model_dump()


# --- Risk Settings Routes ---

@router.get("/risk/settings")
async def get_risk_settings():
    return risk_engine.settings.model_dump()


@router.post("/risk/settings")
async def update_risk_settings(settings: RiskSettings):
    risk_engine.update_settings(settings)
    return risk_engine.settings.model_dump()


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
    await db.save_credentials(req.account, req.server, req.password, req.terminal_path, req.label)
    return {
        "saved": True,
        "warning": "Password is stored locally in plaintext. Only use with demo accounts.",
    }


@router.get("/credentials")
async def get_credentials():
    if db is None:
        return []
    creds = await db.get_saved_credentials()
    # Include password since this is a local-only app
    return creds


@router.delete("/credentials/{account_id}")
async def delete_credentials(account_id: int):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    await db.delete_saved_credentials(account_id)
    return {"deleted": True}


@router.get("/credentials/auto-connect")
async def auto_connect():
    """Try to connect using the most recently used saved credentials."""
    if db is None:
        return {"connected": False, "reason": "No database"}
    if connector.connected:
        return {"connected": True, "reason": "Already connected"}

    creds = await db.get_saved_credentials()
    if not creds:
        return {"connected": False, "reason": "No saved credentials"}

    best = creds[0]  # Most recently used
    raw_path = (best.get("terminal_path") or config.DEFAULT_TERMINAL_PATH).strip().strip('"').strip("'")
    params = ConnectionParams(
        account=best["account"],
        password=best["password"],
        server=best["server"],
        terminal_path=raw_path,
    )
    success = connector.connect(params)
    if success:
        await db.log_connection_event("auto_connected", best["account"], best["server"])
        await db.update_credential_last_used(best["account"])
        if not connector.is_demo() and not config.LIVE_TRADING_ENABLED:
            connector.disconnect()
            return {"connected": False, "reason": "Live account rejected - demo only mode"}
        if connector.account_info:
            risk_engine.set_daily_start_equity(connector.account_info.equity)
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
        logger.info("No symbols configured, using all tradeable from MT5 for scan...")
        tradeable = market_data.get_tradeable_symbols()
        if tradeable:
            symbols = [s["name"] for s in tradeable]
        else:
            visible = market_data.get_visible_symbols()
            symbols = [s["name"] for s in visible if s["trade_enabled"]]
        # NOTE: Don't save to settings — manual scan should show everything
        # but auto-trade uses only the curated allowed_symbols list

    # Use FAST local SmartAgent for bulk scan (no API calls, instant)
    # Gemini is used only for individual deep analysis
    scan_agent = agents.get("SmartAgent")

    account = connector.refresh_account()
    all_positions = execution.get_positions()

    recommendations = []
    for sym in symbols:
        try:
            market_data.enable_symbol(sym)
            tick = market_data.get_tick(sym)
            if tick is None:
                continue

            # Fetch multi-timeframe bars
            m15_bars = market_data.get_bars(sym, "M15", 100)
            h1_bars = market_data.get_bars(sym, "H1", 100)
            h4_bars = market_data.get_bars(sym, "H4", 100)

            sym_positions = [p for p in all_positions if p.symbol == sym]

            input_data = AgentInput(
                symbol=sym,
                timeframe="H1",
                bars=[b.model_dump() for b in h1_bars],
                spread=tick.spread,
                account_equity=account.equity if account else 0,
                open_positions=[p.model_dump() for p in sym_positions],
                multi_tf_bars={
                    "M15": [b.model_dump() for b in m15_bars],
                    "H1": [b.model_dump() for b in h1_bars],
                    "H4": [b.model_dump() for b in h4_bars],
                },
            )

            # Use fast local SmartAgent for bulk scan (no slow API calls)
            if scan_agent is None:
                continue

            signal = scan_agent.evaluate(input_data)

            # Log signal
            signal_id = None
            if db:
                signal_id = await db.log_signal(
                    agent_name=active_agent_name, symbol=sym, timeframe="multi",
                    action=signal.action, confidence=signal.confidence,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    max_holding_minutes=signal.max_holding_minutes, reason=signal.reason,
                )

            # Risk check
            current_price = tick.ask if signal.action == "BUY" else tick.bid
            risk_decision = risk_engine.evaluate(
                signal=signal, symbol=sym,
                spread=tick.spread, equity=account.equity if account else 0,
                open_positions=all_positions,
                entry_price=current_price,
            )

            if db and signal_id:
                await db.log_risk_decision(
                    signal_id=signal_id, approved=risk_decision.approved,
                    reason=risk_decision.reason, adjusted_volume=risk_decision.adjusted_volume,
                )

            entry_price = tick.ask if signal.action == "BUY" else tick.bid if signal.action == "SELL" else tick.ask

            # Get category and description for UI filtering
            import MetaTrader5 as mt5_mod
            raw_sym = mt5_mod.symbol_info(sym)
            category = market_data._categorize_symbol(raw_sym) if raw_sym else "Other"
            description = raw_sym.description if raw_sym else ""

            recommendations.append({
                "symbol": sym,
                "signal": signal.model_dump(),
                "signal_id": signal_id,
                "risk_decision": risk_decision.model_dump(),
                "entry_price_estimate": entry_price,
                "explanation": signal.reason,
                "ready_to_execute": risk_decision.approved and signal.action != "HOLD",
                "category": category,
                "description": description,
            })
        except Exception as e:
            logger.error(f"Smart evaluate error for {sym}: {e}")
            continue

    # Sort by confidence, actionable first
    recommendations.sort(key=lambda r: (r["ready_to_execute"], r["signal"]["confidence"]), reverse=True)

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
    if not connector.is_demo() and not config.LIVE_TRADING_ENABLED:
        raise HTTPException(status_code=403, detail="Live trading is disabled")

    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")

    # Look up the signal
    signal_data = await db.get_signal_by_id(req.signal_id)
    if signal_data is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    risk_data = await db.get_risk_decision_by_signal(req.signal_id)

    symbol = signal_data["symbol"]
    action = signal_data["action"]
    sl = req.custom_stop_loss if req.custom_stop_loss and req.custom_stop_loss > 0 else signal_data["stop_loss"]
    tp = req.custom_take_profit if req.custom_take_profit and req.custom_take_profit > 0 else signal_data["take_profit"]
    volume = risk_data["adjusted_volume"] if risk_data else risk_engine.settings.fixed_lot_size

    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail=f"Cannot execute {action} signal")

    # Re-check risk with current state
    from agent.interface import TradeSignal
    re_signal = TradeSignal(
        action=action, confidence=signal_data["confidence"],
        stop_loss=sl, take_profit=tp,
        reason=signal_data.get("reason", ""),
    )
    tick = market_data.get_tick(symbol)
    account = connector.refresh_account()
    positions = execution.get_positions()

    re_risk = risk_engine.evaluate(
        signal=re_signal, symbol=symbol,
        spread=tick.spread if tick else 999,
        equity=account.equity if account else 0,
        open_positions=positions,
    )

    # Only panic stop blocks - everything else is a warning
    if not re_risk.approved and risk_engine.panic_stopped:
        return {
            "success": False, "retcode": -1,
            "retcode_desc": "Panic stop is active",
            "ticket": None, "volume": None, "price": None,
            "stop_loss": sl, "take_profit": tp, "comment": "",
        }

    # Use re-checked volume if available
    if re_risk.adjusted_volume > 0:
        volume = re_risk.adjusted_volume

    # Convert dollar amount to volume if provided
    if req.amount_usd and req.amount_usd > 0:
        sym_info = market_data.get_symbol_info(symbol)
        if sym_info and tick:
            price = tick.ask if action == "BUY" else tick.bid
            contract_size = sym_info.get("trade_contract_size", 100000)
            vol_min = sym_info.get("volume_min", 0.01)
            vol_step = sym_info.get("volume_step", 0.01)

            if price > 0 and contract_size > 0:
                # Calculate how many lots for the given $ margin amount (with leverage)
                acct = connector.refresh_account()
                lev = acct.leverage if acct else 1
                raw_volume = (req.amount_usd * lev) / (price * contract_size)
                # Round to nearest volume step
                if vol_step > 0:
                    raw_volume = round(raw_volume / vol_step) * vol_step
                volume = max(raw_volume, vol_min)
                logger.info(f"Converted ${req.amount_usd} margin (leverage {lev}x) to {volume} lots (price={price}, contract={contract_size})")

    # Validate SL/TP minimum distance (STOPLEVEL + spread)
    if tick:
        exec_price = tick.ask if action == "BUY" else tick.bid
        _sym_info = market_data.get_symbol_info(symbol)
        if _sym_info:
            _point = _sym_info.get("point", 0.00001)
            _stops_level = _sym_info.get("trade_stops_level", 0)
            _spread_points = tick.spread if tick.spread else 10
            _effective_stops = max(_stops_level, _spread_points, 10)
            _min_distance = _effective_stops * _point * 1.5  # 50% buffer
            _contract_size = _sym_info.get("trade_contract_size", 100000)
            _digits = _sym_info.get("digits", 5)

            if sl and abs(exec_price - sl) < _min_distance:
                min_sl_dollars = round(_min_distance * volume * _contract_size, 2)
                raise HTTPException(
                    status_code=400,
                    detail=f"Stop Loss too close to price. Minimum ~${min_sl_dollars:.2f} for your position. Try a larger SL amount."
                )
            if tp and abs(tp - exec_price) < _min_distance:
                min_tp_dollars = round(_min_distance * volume * _contract_size, 2)
                raise HTTPException(
                    status_code=400,
                    detail=f"Take Profit too close to price. Minimum ~${min_tp_dollars:.2f} for your position. Try a larger TP amount."
                )

    amt_label = f"TA:${int(req.amount_usd)}" if req.amount_usd and req.amount_usd > 0 else "TradingAgent"
    order_req = OrderRequest(
        symbol=symbol, action=action,
        volume=volume,
        stop_loss=sl, take_profit=tp,
        comment=amt_label,
    )
    result = execution.place_order(order_req)

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

    return result.model_dump()


# --- Auto-Trade Routes ---

class AutoTradeSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    min_confidence: Optional[float] = None
    scan_interval: Optional[int] = None


@router.get("/auto-trade/status")
async def auto_trade_status():
    """Get auto-trading status and recent activity."""
    settings = risk_engine.settings
    return {
        "running": auto_trader.is_running,
        "enabled": settings.auto_trade_enabled,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": settings.auto_trade_scan_interval_seconds,
        "last_scan": auto_trader.last_scan_time,
        "recent_trades": auto_trader.recent_trades,
        "panic_stop": risk_engine.panic_stopped,
        "position_manager_running": auto_trader.position_manager.is_running,
        "managed_tickets": list(auto_trader.position_manager.managed_tickets),
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
        },
    }


@router.post("/auto-trade/start")
async def auto_trade_start():
    """Enable and start auto-trading (includes position manager)."""
    if not connector.connected:
        raise HTTPException(status_code=400, detail="Not connected to MT5")
    if not connector.is_demo() and not config.LIVE_TRADING_ENABLED:
        raise HTTPException(status_code=403, detail="Auto-trading only allowed on demo accounts")

    risk_engine.settings.auto_trade_enabled = True
    auto_trader.start()

    if db:
        await db.log_connection_event("auto_trade_started", details="Auto-trading + Position Manager enabled")

    return {
        "running": True,
        "position_manager_running": auto_trader.position_manager.is_running,
        "message": "Auto-trading started with AI position management",
    }


@router.post("/auto-trade/stop")
async def auto_trade_stop():
    """Disable and stop auto-trading (includes position manager)."""
    risk_engine.settings.auto_trade_enabled = False
    auto_trader.stop()

    if db:
        await db.log_connection_event("auto_trade_stopped", details="Auto-trading + Position Manager disabled")

    return {
        "running": False,
        "position_manager_running": False,
        "message": "Auto-trading stopped",
    }


@router.post("/auto-trade/settings")
async def auto_trade_settings(req: AutoTradeSettingsRequest):
    """Update auto-trade settings."""
    settings = risk_engine.settings
    if req.enabled is not None:
        settings.auto_trade_enabled = req.enabled
        if req.enabled and not auto_trader.is_running and connector.connected:
            auto_trader.start()
        elif not req.enabled and auto_trader.is_running:
            auto_trader.stop()

    if req.min_confidence is not None:
        if req.min_confidence < 0.5:
            raise HTTPException(status_code=400, detail="Minimum confidence cannot be below 50%")
        settings.auto_trade_min_confidence = req.min_confidence

    if req.scan_interval is not None:
        if req.scan_interval < 30:
            raise HTTPException(status_code=400, detail="Scan interval must be at least 30 seconds")
        settings.auto_trade_scan_interval_seconds = req.scan_interval

    risk_engine.update_settings(settings)
    return {
        "enabled": settings.auto_trade_enabled,
        "min_confidence": settings.auto_trade_min_confidence,
        "scan_interval": settings.auto_trade_scan_interval_seconds,
    }
