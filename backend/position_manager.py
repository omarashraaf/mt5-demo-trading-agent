"""
Position Manager: Autonomous management of open positions.

Runs every 30 seconds and manages all open positions:
- Trailing stop: Lock in profits by moving SL
- Breakeven stop: Move SL to entry when profit >= 1x risk
- Trend reversal exit: Close if trend flips against position
- Averaging down: Add to losing position if trend still valid
- Time exit: Close if held too long without profit
"""

import asyncio
import logging
import time
from typing import Optional

from mt5.connector import MT5Connector
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest, PositionInfo
from agent.interface import AgentInput
from agent.smart_agent import get_asset_class, get_trade_params
from risk.rules import RiskEngine
from storage.db import Database

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(
        self,
        connector: MT5Connector,
        market_data: MarketDataService,
        execution: ExecutionEngine,
        risk_engine: RiskEngine,
        agents: dict,
    ):
        self.connector = connector
        self.market_data = market_data
        self.execution = execution
        self.risk_engine = risk_engine
        self.agents = agents
        self.db: Optional[Database] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._activity_log: list[dict] = []
        self._avg_down_tracker: dict[int, bool] = {}  # ticket -> has_averaged_down
        self._managed_tickets: set[int] = set()

    def set_database(self, db: Database):
        self.db = db

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def activity_log(self) -> list[dict]:
        return self._activity_log[-50:]

    @property
    def managed_tickets(self) -> set[int]:
        return self._managed_tickets

    def start(self):
        if self.is_running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Position Manager STARTED")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("Position Manager STOPPED")

    async def _run_loop(self):
        while self._running:
            try:
                await asyncio.sleep(30)
                if not self._running:
                    break
                if not self.connector.connected:
                    continue
                if self.risk_engine.panic_stopped:
                    continue
                # Safety: stop if auto-trade was disabled externally
                if not self.risk_engine.settings.auto_trade_enabled:
                    logger.debug("Position Manager: auto-trade disabled, skipping cycle")
                    continue
                await self._manage_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Position Manager error: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _manage_positions(self):
        all_positions = self.execution.get_positions()

        # Only manage positions opened by auto-trader (comment starts with "TA:" or "TradingAgent" or "AVG:")
        # Skip manually-opened positions so users can manage their own trades
        positions = [
            p for p in all_positions
            if p.comment.startswith("TA:") or p.comment == "TradingAgent" or p.comment.startswith("AVG:")
        ]
        self._managed_tickets = {p.ticket for p in positions}

        if positions:
            logger.debug(f"Position Manager: managing {len(positions)}/{len(all_positions)} auto-traded positions")

        # Clean up avg_down_tracker for closed positions
        open_tickets = {p.ticket for p in all_positions}
        closed = [t for t in self._avg_down_tracker if t not in open_tickets]
        for t in closed:
            del self._avg_down_tracker[t]

        for pos in positions:
            try:
                await self._manage_single(pos)
            except Exception as e:
                logger.error(f"Error managing position {pos.ticket} ({pos.symbol}): {e}")

    async def _manage_single(self, pos: PositionInfo):
        """Manage a single open position."""
        symbol = pos.symbol

        # Get market data for analysis
        h1_bars = self.market_data.get_bars(symbol, "H1", 60)
        if len(h1_bars) < 20:
            return

        tick = self.market_data.get_tick(symbol)
        if not tick:
            return

        atr = self._calc_atr(h1_bars, 14)
        if atr <= 0:
            return

        is_buy = pos.type == "BUY"
        current_price = tick.bid if is_buy else tick.ask  # exit price
        entry = pos.price_open
        profit_in_price = (current_price - entry) if is_buy else (entry - current_price)
        original_risk = abs(entry - pos.stop_loss) if pos.stop_loss > 0 else atr * 1.5

        # Asset-specific parameters
        asset_class = get_asset_class(symbol)
        params = get_trade_params(asset_class)
        # Swing trades: trail wider, hold longer. Day trades: trail tighter, exit sooner.
        trail_trigger_mult = 1.5 if asset_class != "forex" else 1.0
        trail_distance_mult = 1.2 if asset_class != "forex" else 0.75

        # === A: Trailing Stop (when profitable) ===
        if profit_in_price > atr * trail_trigger_mult:
            trail_distance = atr * trail_distance_mult
            if is_buy:
                new_sl = round(current_price - trail_distance, 5)
                if new_sl > pos.stop_loss and new_sl > entry:
                    result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                    if result.success:
                        locked = round((new_sl - entry) * pos.volume * self._get_contract_size(symbol), 2)
                        await self._log_activity(
                            "trailing_stop", symbol, pos.ticket,
                            f"Trailing stop moved to {new_sl:.5g} (locking ~${locked} profit)",
                            pos.profit,
                        )
                    return  # Don't do other actions in same cycle
            else:
                new_sl = round(current_price + trail_distance, 5)
                if new_sl < pos.stop_loss and new_sl < entry:
                    result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                    if result.success:
                        locked = round((entry - new_sl) * pos.volume * self._get_contract_size(symbol), 2)
                        await self._log_activity(
                            "trailing_stop", symbol, pos.ticket,
                            f"Trailing stop moved to {new_sl:.5g} (locking ~${locked} profit)",
                            pos.profit,
                        )
                    return

        # === B: Breakeven Stop (when profit >= 1x risk) ===
        if profit_in_price >= original_risk and pos.stop_loss > 0:
            spread_buffer = (tick.ask - tick.bid) * 1.5
            if is_buy and pos.stop_loss < entry:
                new_sl = round(entry + spread_buffer, 5)
                result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                if result.success:
                    await self._log_activity(
                        "breakeven", symbol, pos.ticket,
                        f"Moved to breakeven at {new_sl:.5g} - risk eliminated",
                        pos.profit,
                    )
                return
            elif not is_buy and pos.stop_loss > entry:
                new_sl = round(entry - spread_buffer, 5)
                result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                if result.success:
                    await self._log_activity(
                        "breakeven", symbol, pos.ticket,
                        f"Moved to breakeven at {new_sl:.5g} - risk eliminated",
                        pos.profit,
                    )
                return

        # === C: Trend Reversal Check (close early if trend flips) ===
        # Use BOTH slow EMA crossover AND fast RSI momentum to detect reversals quickly
        h4_bars = self.market_data.get_bars(symbol, "H4", 60)
        trend_bars = h4_bars if len(h4_bars) >= 52 else h1_bars

        # Fast check: H1 RSI reversal (reacts in minutes, not hours)
        h1_closes = [b.close for b in h1_bars]
        rsi = self._calc_rsi(h1_closes, 14) if len(h1_closes) > 15 else 50.0

        # Fast reversal: losing position + RSI shows strong opposite momentum
        if pos.profit < 0:
            if is_buy and rsi < 35:
                # RSI says strong bearish — cut the losing BUY quickly
                result = self.execution.close_position(pos.ticket)
                if result.success:
                    await self._log_activity(
                        "close_reversal", symbol, pos.ticket,
                        f"Closed losing BUY — RSI dropped to {rsi:.0f} (strong bearish momentum)",
                        pos.profit,
                    )
                return
            elif not is_buy and rsi > 65:
                # RSI says strong bullish — cut the losing SELL quickly
                result = self.execution.close_position(pos.ticket)
                if result.success:
                    await self._log_activity(
                        "close_reversal", symbol, pos.ticket,
                        f"Closed losing SELL — RSI rose to {rsi:.0f} (strong bullish momentum)",
                        pos.profit,
                    )
                return

        # Slow check: EMA crossover on H4/H1 (confirms sustained trend change)
        if len(trend_bars) >= 52:
            closes = [b.close for b in trend_bars]
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)

            if is_buy and ema20[-1] < ema50[-1] and ema20[-2] < ema50[-2]:
                result = self.execution.close_position(pos.ticket)
                if result.success:
                    await self._log_activity(
                        "close_reversal", symbol, pos.ticket,
                        f"Closed BUY — trend reversed to bearish (EMA20 below EMA50)",
                        pos.profit,
                    )
                return
            elif not is_buy and ema20[-1] > ema50[-1] and ema20[-2] > ema50[-2]:
                result = self.execution.close_position(pos.ticket)
                if result.success:
                    await self._log_activity(
                        "close_reversal", symbol, pos.ticket,
                        f"Closed SELL — trend reversed to bullish (EMA20 above EMA50)",
                        pos.profit,
                    )
                return

        # === D: Averaging Down — DISABLED ===
        # Averaging down was causing position count to balloon past the max limit
        # and doubling down on losing trades. Disabled for safer risk management.
        if False and pos.profit < 0 and pos.ticket not in self._avg_down_tracker:
            # Only if loss is significant (> 0.5x ATR in price)
            if abs(profit_in_price) > atr * 0.5:
                smart_agent = self.agents.get("SmartAgent")
                if smart_agent:
                    m15_bars = self.market_data.get_bars(symbol, "M15", 100)
                    account = self.connector.refresh_account()
                    if account and len(m15_bars) >= 22 and len(h1_bars) >= 52:
                        input_data = AgentInput(
                            symbol=symbol, timeframe="H1",
                            bars=[{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in h1_bars],
                            spread=tick.spread,
                            account_equity=account.equity,
                            open_positions=[],  # Empty so it doesn't skip due to existing position
                            multi_tf_bars={
                                "M15": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in m15_bars],
                                "H1": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in h1_bars],
                                "H4": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in (h4_bars if h4_bars else h1_bars)],
                            },
                        )
                        signal = smart_agent.evaluate(input_data)

                        # Only average down if agent agrees with position direction
                        if signal.confidence >= 0.60 and (
                            (is_buy and signal.action == "BUY") or
                            (not is_buy and signal.action == "SELL")
                        ):
                            avg_volume = round(pos.volume * 0.5, 2)
                            avg_volume = max(avg_volume, 0.01)
                            order_req = OrderRequest(
                                symbol=symbol,
                                action=pos.type,
                                volume=avg_volume,
                                stop_loss=pos.stop_loss,
                                take_profit=pos.take_profit,
                                comment=f"AVG:{pos.ticket}",
                            )
                            result = self.execution.place_order(order_req)
                            self._avg_down_tracker[pos.ticket] = True
                            if result.success:
                                await self._log_activity(
                                    "average_down", symbol, pos.ticket,
                                    f"Averaged down with {avg_volume} lots at better price (AI confidence: {signal.confidence:.0%})",
                                    pos.profit,
                                )
                            return

        # === E: Time-Based Exit (asset-aware) ===
        position_age_minutes = (time.time() - pos.time) / 60
        max_hold = params["max_hold_minutes"]  # forex=6h, stocks=3d, commodities=2d
        if position_age_minutes > max_hold and pos.profit <= 0:
            result = self.execution.close_position(pos.ticket)
            if result.success:
                await self._log_activity(
                    "close_timeout", symbol, pos.ticket,
                    f"Closed after {position_age_minutes:.0f}min without profit (max {max_hold}min)",
                    pos.profit,
                )

    # --- Helper methods ---

    def _calc_atr(self, bars, period: int = 14) -> float:
        if len(bars) < period + 1:
            ranges = [b.high - b.low for b in bars[-period:]]
            return sum(ranges) / len(ranges) if ranges else 0.0001
        true_ranges = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
            true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(true_ranges[-period:]) / period

    def _ema(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return data[:]
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        return [0.0] * (len(data) - len(ema)) + ema

    def _calc_rsi(self, closes: list[float], period: int = 14) -> float:
        """Calculate current RSI value."""
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas]
        losses = [max(0, -d) for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _get_contract_size(self, symbol: str) -> float:
        info = self.market_data.get_symbol_info(symbol)
        return info.get("trade_contract_size", 100000) if info else 100000

    async def _log_activity(self, action: str, symbol: str, ticket: int, detail: str, profit: float):
        logger.info(f"[PositionManager] {action}: {symbol} #{ticket} - {detail} (P&L: {profit:.2f})")
        entry = {
            "timestamp": time.time(),
            "action": action,
            "symbol": symbol,
            "ticket": ticket,
            "detail": detail,
            "profit": profit,
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > 100:
            self._activity_log = self._activity_log[-100:]

        if self.db:
            try:
                await self.db.log_ai_activity(action, symbol, ticket, detail, profit)
            except Exception as e:
                logger.error(f"Failed to log AI activity: {e}")
