"""
Auto-Trader: Background task that scans markets and auto-executes trades
when confidence exceeds the user's threshold.

Also manages the PositionManager which autonomously handles open positions
(trailing stops, breakeven, trend reversal exit, averaging down, time exit).

Safety: Only works in demo mode. Every trade has stop-loss.
The risk engine must approve every trade before execution.
"""

import asyncio
import logging
import time
from typing import Optional

from mt5.connector import MT5Connector
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.interface import AgentInput, TradeSignal
from risk.rules import RiskEngine
from storage.db import Database
from position_manager import PositionManager

logger = logging.getLogger(__name__)


class AutoTrader:
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
        self._last_scan: float = 0
        self._trade_log: list[dict] = []  # Recent auto-trade log for UI

        # Position Manager — autonomous management of open positions
        self.position_manager = PositionManager(
            connector=connector,
            market_data=market_data,
            execution=execution,
            risk_engine=risk_engine,
            agents=agents,
        )

    def set_database(self, db: Database):
        self.db = db
        self.position_manager.set_database(db)

    @property
    def is_running(self) -> bool:
        if self._running and self._task is not None and self._task.done():
            # Task crashed — auto-restart it
            logger.warning("Auto-trade task died unexpectedly, restarting...")
            try:
                exc = self._task.exception()
                logger.error(f"Auto-trade crash reason: {exc}")
            except Exception:
                pass
            self._task = asyncio.create_task(self._run_loop())
            self.position_manager.start()
        return self._running and self._task is not None and not self._task.done()

    @property
    def last_scan_time(self) -> float:
        return self._last_scan

    @property
    def recent_trades(self) -> list[dict]:
        return self._trade_log[-20:]  # Last 20 auto-trades

    @property
    def combined_activity(self) -> list[dict]:
        """Merge trade log + position manager activity, sorted by time."""
        trades = [
            {**t, "source": "scanner"} for t in self._trade_log
        ]
        pm_activity = [
            {**a, "source": "position_manager"} for a in self.position_manager.activity_log
        ]
        combined = trades + pm_activity
        combined.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return combined[:50]

    def start(self):
        if self.is_running:
            logger.info("Auto-trader already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.position_manager.start()
        logger.info("Auto-trader STARTED (with Position Manager)")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.position_manager.stop()
        logger.info("Auto-trader STOPPED (with Position Manager)")

    async def _run_loop(self):
        """Main auto-trading loop."""
        logger.info("Auto-trade loop started")
        while self._running:
            try:
                interval = self.risk_engine.settings.auto_trade_scan_interval_seconds
                await asyncio.sleep(interval)

                if not self._running:
                    break

                # Pre-flight checks
                if not self.connector.connected:
                    logger.debug("Auto-trader: not connected, skipping")
                    continue

                if self.risk_engine.panic_stopped:
                    logger.debug("Auto-trader: panic stop active, skipping")
                    continue

                if not self.risk_engine.settings.auto_trade_enabled:
                    logger.debug("Auto-trader: auto-trade disabled, skipping")
                    continue

                await self._scan_and_trade()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-trader error: {e}", exc_info=True)
                await asyncio.sleep(10)  # Back off on error

        logger.info("Auto-trade loop ended")

    async def _scan_and_trade(self):
        """Scan all allowed symbols and auto-execute strong signals."""
        settings = self.risk_engine.settings
        min_conf = settings.auto_trade_min_confidence
        symbols = settings.allowed_symbols

        # Auto-detect symbols if list is empty — prefer liquid, well-known instruments
        if not symbols:
            logger.info("Auto-trader: no symbols configured, auto-detecting...")
            # Preferred symbols — stocks (swing) + forex (day) + commodities (swing)
            preferred = [
                # Top Stocks — swing trade
                "NVDA", "AMD", "MSFT", "INTC", "AAPL", "GOOG", "AMZN", "TSLA", "META", "NFLX",
                "AVGO", "CRM", "ORCL", "ADBE", "QCOM",
                # Major Forex — day trade
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
                # Metals — swing trade (gold, silver, platinum, palladium)
                "XAUUSD", "XAGUSD", "XAUEUR", "XAGEUR", "XPTUSD", "XPDUSD",
                # Commodity ETFs — swing trade (oil, gold, silver, broad commodities)
                "GLD", "SLV", "USO", "DBC",
            ]
            tradeable = self.market_data.get_tradeable_symbols()
            tradeable_names = {s["name"] for s in tradeable}

            # First: pick preferred symbols that are available
            symbols = [s for s in preferred if s in tradeable_names]

            # If not enough, add non-forex tradeable (commodities, indices, crypto)
            if len(symbols) < 5:
                non_forex = [s["name"] for s in tradeable if s["category"] != "Forex" and s["name"] not in symbols]
                symbols.extend(non_forex[:10])

            # Limit to 15 symbols max
            symbols = symbols[:15]

            if symbols:
                settings.allowed_symbols = symbols
                logger.info(f"Auto-detected {len(symbols)} preferred symbols: {symbols}")

        account = self.connector.refresh_account()
        if not account:
            return

        all_positions = self.execution.get_positions()
        self._last_scan = time.time()

        logger.info(f"Auto-trader scanning {len(symbols)} symbols (min confidence: {min_conf})")

        for sym in symbols:
            try:
                # Check if we can still trade
                if self.risk_engine.panic_stopped:
                    break
                if len(all_positions) >= settings.max_concurrent_positions:
                    logger.info("Auto-trader: max positions reached, stopping scan")
                    break

                self.market_data.enable_symbol(sym)
                tick = self.market_data.get_tick(sym)
                if tick is None:
                    logger.debug(f"Auto-trader: {sym} no tick data (market closed?), skipping")
                    continue

                # Skip if no valid price (market might be closed for stocks)
                if tick.bid <= 0 or tick.ask <= 0:
                    logger.debug(f"Auto-trader: {sym} no price data, skipping")
                    continue

                # Skip symbols with excessive spreads (exotic pairs)
                # But allow spread=0 for newly-enabled symbols that haven't loaded yet
                if tick.spread > settings.max_spread_threshold and tick.spread > 0:
                    logger.debug(f"Auto-trader: {sym} spread {tick.spread} > {settings.max_spread_threshold}, skipping")
                    continue

                # Multi-timeframe analysis
                m15_bars = self.market_data.get_bars(sym, "M15", 100)
                h1_bars = self.market_data.get_bars(sym, "H1", 100)
                h4_bars = self.market_data.get_bars(sym, "H4", 100)

                sym_positions = [p for p in all_positions if p.symbol == sym]

                input_data = AgentInput(
                    symbol=sym,
                    timeframe="H1",
                    bars=[b.model_dump() for b in h1_bars],
                    spread=tick.spread,
                    account_equity=account.equity,
                    open_positions=[p.model_dump() for p in sym_positions],
                    multi_tf_bars={
                        "M15": [b.model_dump() for b in m15_bars],
                        "H1": [b.model_dump() for b in h1_bars],
                        "H4": [b.model_dump() for b in h4_bars],
                    },
                )

                smart_agent = self.agents.get("SmartAgent")
                if not smart_agent:
                    continue

                # Pass 1: Fast local SmartAgent scan
                signal = smart_agent.evaluate(input_data)

                # Skip HOLD signals and low confidence
                if signal.action == "HOLD":
                    continue
                if signal.confidence < min_conf:
                    logger.debug(f"Auto-trader: {sym} {signal.action} confidence {signal.confidence:.2f} < {min_conf}, skipping")
                    continue

                # Pass 2: Gemini deep analysis for confirmation (if available)
                # Gemini adds news awareness and can override or boost the signal
                gemini_agent = self.agents.get("GeminiAgent")
                agent_used = "SmartAgent"
                # Save SmartAgent's SL/TP before Gemini potentially overrides
                smart_sl = signal.stop_loss
                smart_tp = signal.take_profit
                smart_confidence = signal.confidence

                if gemini_agent:
                    try:
                        gemini_signal = gemini_agent.evaluate(input_data)
                        if gemini_signal.action != "HOLD" and gemini_signal.confidence > 0:
                            if gemini_signal.action == signal.action:
                                # Both agree — boost confidence but KEEP SmartAgent's SL/TP
                                # (SmartAgent has proper ATR-based R:R, Gemini's SL/TP are unreliable)
                                signal.confidence = min(0.95, max(signal.confidence, gemini_signal.confidence) + 0.05)
                                agent_used = "SmartAgent+Gemini"
                                logger.info(f"Auto-trader: Gemini CONFIRMS {signal.action} {sym} (boosted to {signal.confidence:.0%})")
                            elif gemini_signal.confidence > signal.confidence:
                                # Gemini disagrees but more confident — use Gemini's direction
                                # but keep SmartAgent's SL/TP for proper R:R
                                signal.action = gemini_signal.action
                                signal.confidence = gemini_signal.confidence
                                signal.reason = gemini_signal.reason
                                # Recalculate SL/TP using SmartAgent logic for new direction
                                recalc = smart_agent.evaluate(AgentInput(
                                    symbol=sym, timeframe="multi", bars=input_data.bars,
                                    spread=input_data.spread, account_equity=input_data.account_equity,
                                    open_positions=input_data.open_positions, multi_tf_bars=input_data.multi_tf_bars,
                                ))
                                if recalc.stop_loss and recalc.take_profit:
                                    signal.stop_loss = recalc.stop_loss
                                    signal.take_profit = recalc.take_profit
                                agent_used = "GeminiAgent+SmartSLTP"
                                logger.info(f"Auto-trader: Gemini OVERRIDES to {signal.action} {sym} ({signal.confidence:.0%})")
                            else:
                                logger.info(f"Auto-trader: Gemini disagrees ({gemini_signal.action}) but lower confidence, keeping SmartAgent signal")
                        else:
                            logger.debug(f"Auto-trader: Gemini returned HOLD for {sym}, using SmartAgent signal")
                    except Exception as e:
                        logger.warning(f"Auto-trader: Gemini analysis failed for {sym}: {e}, proceeding with SmartAgent")

                # Log signal
                signal_id = None
                if self.db:
                    signal_id = await self.db.log_signal(
                        agent_name=agent_used, symbol=sym, timeframe="multi",
                        action=signal.action, confidence=signal.confidence,
                        stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                        max_holding_minutes=signal.max_holding_minutes, reason=signal.reason,
                    )

                # Risk check (auto-trade mode = hard blocks on limits)
                # Use the actual price for accurate R:R calculation
                current_price = tick.ask if signal.action == "BUY" else tick.bid
                risk_decision = self.risk_engine.evaluate(
                    signal=signal, symbol=sym,
                    spread=tick.spread, equity=account.equity,
                    open_positions=all_positions,
                    is_auto_trade=True,
                    entry_price=current_price,
                )

                if self.db and signal_id:
                    await self.db.log_risk_decision(
                        signal_id=signal_id, approved=risk_decision.approved,
                        reason=risk_decision.reason, adjusted_volume=risk_decision.adjusted_volume,
                    )

                if not risk_decision.approved:
                    logger.info(f"Auto-trader: {sym} {signal.action} rejected by risk: {risk_decision.reason}")
                    self._log_auto_trade(sym, signal, risk_decision.reason, False)
                    continue

                # EXECUTE THE TRADE
                logger.info(f"AUTO-TRADE: {signal.action} {sym} confidence={signal.confidence:.2f} volume={risk_decision.adjusted_volume}")

                # Calculate investment amount for display (volume × price × contract_size / leverage)
                entry_price = tick.ask if signal.action == "BUY" else tick.bid
                sym_info = self.market_data.get_symbol_info(sym)
                contract_size = sym_info.get("trade_contract_size", 100000) if sym_info else 100000
                leverage = account.leverage or 100
                investment = round(risk_decision.adjusted_volume * entry_price * contract_size / leverage, 0)
                comment = f"TA:${int(investment)}"

                order_req = OrderRequest(
                    symbol=sym,
                    action=signal.action,
                    volume=risk_decision.adjusted_volume,
                    stop_loss=signal.stop_loss or 0,
                    take_profit=signal.take_profit or 0,
                    comment=comment,
                )
                result = self.execution.place_order(order_req)

                if self.db and signal_id:
                    await self.db.log_order(
                        signal_id=signal_id, symbol=sym, action=signal.action,
                        volume=order_req.volume, price=result.price,
                        stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                        ticket=result.ticket, retcode=result.retcode,
                        retcode_desc=result.retcode_desc, success=result.success,
                    )
                    if result.success and result.ticket:
                        await self.db.log_position_change(
                            result.ticket, "auto_opened", sym,
                            f"AUTO {signal.action} {order_req.volume} lots at {result.price} (conf={signal.confidence:.0%})"
                        )

                if result.success:
                    logger.info(f"AUTO-TRADE SUCCESS: {sym} ticket={result.ticket} price={result.price}")
                    self._log_auto_trade(sym, signal, f"Order #{result.ticket} filled at {result.price}", True)
                    # Update position list for next symbol
                    all_positions = self.execution.get_positions()
                else:
                    logger.warning(f"AUTO-TRADE FAILED: {sym} {result.retcode_desc}")
                    self._log_auto_trade(sym, signal, result.retcode_desc, False)

            except Exception as e:
                logger.error(f"Auto-trader error for {sym}: {e}", exc_info=True)

    def _log_auto_trade(self, symbol: str, signal: TradeSignal, detail: str, success: bool):
        self._trade_log.append({
            "timestamp": time.time(),
            "symbol": symbol,
            "action": signal.action,
            "confidence": signal.confidence,
            "detail": detail,
            "success": success,
        })
        # Keep only last 50
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]
