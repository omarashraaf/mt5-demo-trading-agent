"""
Auto-Trader: Background task that scans markets and auto-executes trades
when confidence exceeds the user's threshold.

Also manages the PositionManager which autonomously handles open positions
(trailing stops, breakeven, trend reversal exit, averaging down, time exit).

Safety: Only works in demo mode. Every trade has stop-loss.
The risk engine must approve every trade before execution.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from mt5.connector import MT5Connector
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.interface import TradeSignal
from risk.rules import RiskEngine
from storage.db import Database
from position_manager import PositionManager
from services.background_state import BackgroundServiceState
from services.execution_service import ExecutionService

logger = logging.getLogger(__name__)


class AutoTrader:
    def __init__(
        self,
        connector: MT5Connector,
        market_data: MarketDataService,
        execution: ExecutionEngine,
        risk_engine: RiskEngine,
        agents: dict,
        signal_pipeline,
        execution_service: ExecutionService | None = None,
    ):
        self.connector = connector
        self.market_data = market_data
        self.execution = execution
        self.risk_engine = risk_engine
        self.agents = agents
        self.signal_pipeline = signal_pipeline
        self.execution_service = execution_service or ExecutionService(
            execution_engine=execution,
            risk_service=signal_pipeline.risk_service,
            signal_pipeline=signal_pipeline,
        )
        self.db: Optional[Database] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_scan: float = 0
        self._trade_log: list[dict] = []  # Recent auto-trade log for UI
        self._state = BackgroundServiceState(name="auto_trader")

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
        self.signal_pipeline.set_database(db)
        self.execution_service.db = db
        self.position_manager.set_database(db)

    @property
    def is_running(self) -> bool:
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

    def status_snapshot(self) -> dict:
        state = self._state.model_dump()
        state["last_scan"] = self._last_scan
        state["recent_trades"] = self.recent_trades
        return state

    def start(self):
        if self.is_running:
            logger.info("Auto-trader already running")
            return
        self._running = True
        self._state.mark_started()
        self._task = asyncio.create_task(self._run_loop())
        self.position_manager.start()
        logger.info("Auto-trader STARTED (with Position Manager)")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.position_manager.stop()
        self._state.mark_stopped()
        logger.info("Auto-trader STOPPED (with Position Manager)")

    async def _run_loop(self):
        """Main auto-trading loop."""
        logger.info("Auto-trade loop started")
        while self._running:
            try:
                self._state.mark_heartbeat()

                if not self._running:
                    break

                # Pre-flight checks
                if not self.connector.connected:
                    logger.debug("Auto-trader: not connected, skipping")
                    continue

                if self.risk_engine.panic_stopped:
                    logger.debug("Auto-trader: panic stop active, skipping")
                    continue

                if not self.risk_engine.auto_trade_enabled:
                    logger.debug("Auto-trader: auto-trade disabled, skipping")
                else:
                    await self._scan_and_trade()
                    self._state.mark_cycle()

                if not self._running:
                    break
                interval = self.risk_engine.settings.auto_trade_scan_interval_seconds
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._state.mark_error(e)
                logger.error(f"Auto-trader error: {e}", exc_info=True)
                await asyncio.sleep(10)  # Back off on error

        logger.info("Auto-trade loop ended")
        self._state.mark_stopped()

    async def _scan_and_trade(self):
        """Scan all allowed symbols and auto-execute strong signals."""
        settings = self.risk_engine.settings
        tradeable_market = self.market_data.get_tradeable_symbols()
        symbols = self.signal_pipeline.universe_service.resolve_requested_symbols(
            settings.allowed_symbols,
            self.signal_pipeline.universe_service.filter_market_symbols(tradeable_market),
        )

        # Auto-detect symbols if list is empty — prefer liquid, well-known instruments
        if not symbols:
            logger.info("Auto-trader: no symbols configured, auto-detecting...")
            filtered_tradeable = self.signal_pipeline.universe_service.filter_market_symbols(tradeable_market)
            symbols = await self.signal_pipeline.auto_trade_candidate_symbols(
                filtered_tradeable,
                limit=12,
            )
            if symbols:
                logger.info("Auto-trader: using event-driven/curated symbol list: %s", symbols)
            else:
                logger.info("Auto-trader: no event-backed candidates available this cycle")

        # De-duplicate by canonical symbol to avoid repeated same-symbol gating in one scan.
        deduped: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            canonical = self.signal_pipeline.universe_service.canonical_symbol(sym)
            if canonical in seen:
                continue
            seen.add(canonical)
            deduped.append(sym)
        symbols = deduped

        account = self.connector.refresh_account()
        if not account:
            return

        all_positions = self.execution.get_positions()
        self._last_scan = time.time()
        scan_window_id = f"auto:{int(self._last_scan)}"
        self.signal_pipeline.anti_churn_service.begin_scan_window(scan_window_id)

        logger.info(f"Auto-trader scanning {len(symbols)} symbols")
        if not symbols:
            self._trade_log.append({
                "timestamp": time.time(),
                "symbol": "SYSTEM",
                "action": "HOLD",
                "confidence": 0.0,
                "quality_score": 0.0,
                "detail": "No eligible auto-trade symbols this cycle. Stocks are session-filtered; only indices/commodities with valid availability will be scanned.",
                "success": False,
            })
            self._trade_log = self._trade_log[-50:]
            return

        for sym in symbols:
            try:
                # Check if we can still trade
                if self.risk_engine.panic_stopped:
                    break
                max_positions = settings.max_open_positions_total or settings.max_concurrent_positions
                if len(all_positions) >= max_positions:
                    logger.info("Auto-trader: max positions reached, stopping scan")
                    break

                decision = await self.signal_pipeline.evaluate(
                    symbol=sym,
                    requested_agent_name="GeminiAgent",
                    requested_timeframe="H1",
                    evaluation_mode="auto",
                    bar_count=100,
                    scan_window_id=scan_window_id,
                )
                signal_id = await self.signal_pipeline.persist_evaluation(decision, "multi")

                signal = decision.signal_decision.final_signal.to_trade_signal()
                context = decision.signal_decision.market_context
                risk_evaluation = decision.risk_evaluation
                quality = decision.trade_quality_assessment

                if signal.action == "HOLD":
                    hold_detail = signal.reason or decision.reason
                    self._log_auto_trade(sym, signal, hold_detail, False, quality.final_trade_quality_score)
                    continue

                if not decision.allow_execute:
                    logger.info("Auto-trader: %s %s rejected: %s", sym, signal.action, decision.reason)
                    self._log_auto_trade(sym, signal, decision.reason, False, quality.final_trade_quality_score)
                    continue

                logger.info(
                    "AUTO-TRADE: %s %s confidence=%.2f quality=%.2f volume=%.4f",
                    signal.action,
                    sym,
                    signal.confidence,
                    quality.final_trade_quality_score,
                    risk_evaluation.adjusted_volume,
                )

                tick = self.market_data.get_tick(sym)
                if not tick:
                    self._log_auto_trade(sym, signal, "Live tick unavailable at execution time", False, quality.final_trade_quality_score)
                    continue
                symbol_point = context.symbol_info.point if context.symbol_info and context.symbol_info.point else 0.0
                spread_limit_points = (
                    (context.profile.max_spread / symbol_point)
                    if context.profile and symbol_point > 0
                    else settings.max_spread_threshold
                )
                if self.signal_pipeline.anti_churn_service.spread_deteriorated(
                    decision.signal_decision.market_context.tick.get("spread", 0.0) if decision.signal_decision.market_context.tick else 0.0,
                    tick.spread,
                    spread_limit_points,
                ):
                    self._log_auto_trade(sym, signal, "Spread deteriorated before execution", False, quality.final_trade_quality_score)
                    continue

                entry_price = tick.ask if signal.action == "BUY" else tick.bid
                symbol_info = context.symbol_info
                contract_size = symbol_info.trade_contract_size if symbol_info else 100000
                leverage = context.account_leverage or 100
                investment = round(risk_evaluation.adjusted_volume * entry_price * contract_size / leverage, 0)
                normalized_sl, normalized_tp, sl_amount_usd, tp_amount_usd = await self._dynamic_amount_based_targets(
                    decision=decision,
                    action=signal.action,
                    entry_price=entry_price,
                    volume=risk_evaluation.adjusted_volume,
                    contract_size=contract_size,
                    amount_usd=max(float(investment), 1.0),
                    digits=symbol_info.digits if symbol_info else 5,
                )
                comment = f"TA:${float(investment):.2f}|SLA:${float(sl_amount_usd):.2f}|TPA:${float(tp_amount_usd):.2f}"

                order_req = OrderRequest(
                    symbol=sym,
                    action=signal.action,
                    volume=risk_evaluation.adjusted_volume,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    comment=comment,
                )
                preflight = await self.execution_service.preflight(
                    symbol=sym,
                    action=signal.action,
                    volume=risk_evaluation.adjusted_volume,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    reference_spread=decision.signal_decision.market_context.tick.get("spread", 0.0)
                    if decision.signal_decision.market_context.tick
                    else 0.0,
                    requested_agent_name="GeminiAgent",
                    requested_timeframe="H1",
                    evaluation_mode="auto",
                    scan_window_id=scan_window_id,
                )
                if not preflight.approved:
                    self._log_auto_trade(sym, signal, preflight.reason, False, quality.final_trade_quality_score)
                    continue
                result = self.execution_service.place_order_if_approved(order_req, preflight)

                if self.db and signal_id:
                    await self.db.log_order(
                        signal_id=signal_id,
                        symbol=sym,
                        action=signal.action,
                        volume=order_req.volume,
                        price=result.price,
                        stop_loss=normalized_sl,
                        take_profit=normalized_tp,
                        ticket=result.ticket,
                        retcode=result.retcode,
                        retcode_desc=result.retcode_desc,
                        success=result.success,
                        comment=order_req.comment,
                    )
                    if result.success and result.ticket:
                        await self.db.log_position_change(
                            result.ticket,
                            "auto_opened",
                            sym,
                            f"AUTO {signal.action} {order_req.volume} lots at {result.price} (conf={signal.confidence:.0%})",
                        )
                        await self.db.save_position_management_plan(
                            ticket=result.ticket,
                            signal_id=signal_id,
                            symbol=sym,
                            action=signal.action,
                            plan=decision.position_management_plan.model_dump(),
                        )
                        await self.db.mark_evaluation_outcome(signal_id, "opened")
                        account = self.connector.refresh_account() if self.connector.connected else None
                        await self.db.mark_trade_candidate_execution(
                            signal_id=signal_id,
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

                if result.success:
                    logger.info("AUTO-TRADE SUCCESS: %s ticket=%s price=%s", sym, result.ticket, result.price)
                    self._log_auto_trade(sym, signal, f"Order #{result.ticket} filled at {result.price}", True, quality.final_trade_quality_score)
                    self.signal_pipeline.anti_churn_service.mark_symbol_opened(scan_window_id, sym)
                    all_positions = self.execution.get_positions()
                else:
                    logger.warning("AUTO-TRADE FAILED: %s %s", sym, result.retcode_desc)
                    self._log_auto_trade(sym, signal, result.retcode_desc, False, quality.final_trade_quality_score)

            except Exception as e:
                logger.error(f"Auto-trader error for {sym}: {e}", exc_info=True)

    def _log_auto_trade(self, symbol: str, signal: TradeSignal, detail: str, success: bool, quality_score: float = 0.0):
        self._trade_log.append({
            "timestamp": time.time(),
            "symbol": symbol,
            "action": signal.action,
            "confidence": signal.confidence,
            "quality_score": quality_score,
            "detail": detail,
            "success": success,
        })
        # Keep only last 50
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    async def _dynamic_amount_based_targets(
        self,
        *,
        decision,
        action: str,
        entry_price: float,
        volume: float,
        contract_size: float,
        amount_usd: float,
        digits: int,
    ) -> tuple[float, float, float, float]:
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced")).lower()
        min_rr = max(1.0, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.8) or 1.8))
        base_by_mode = {
            "safe": (0.05, 0.09),
            "balanced": (0.07, 0.12),
            "aggressive": (0.09, 0.16),
        }
        sl_pct, tp_pct = base_by_mode.get(mode, (0.07, 0.12))
        range_pct = 0.0
        drift_pct = 0.0
        drift_aligned = True

        # 1) Last-hours chart behavior (explicit volatility + directional push)
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

        # 3) Ask Gemini for bounded SL/TP percentage deltas from last-hours chart + mode.
        sl_delta, tp_delta = await self._gemini_target_pct_adjustment(
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

        sl_pct = self._clamp(sl_pct, 0.03, 0.15)
        tp_pct = self._clamp(tp_pct, 0.06, 0.35)
        tp_pct = max(tp_pct, sl_pct * min_rr)
        tp_pct = self._clamp(tp_pct, 0.06, 0.40)

        price_per_unit = max(volume * contract_size, 1e-9)
        sl_distance = (amount_usd * sl_pct) / price_per_unit
        tp_distance = (amount_usd * tp_pct) / price_per_unit

        sl_amount_usd = float(amount_usd * sl_pct)
        tp_amount_usd = float(amount_usd * tp_pct)

        if action == "BUY":
            sl = round(entry_price - sl_distance, digits)
            tp = round(entry_price + tp_distance, digits)
        else:
            sl = round(entry_price + sl_distance, digits)
            tp = round(entry_price - tp_distance, digits)
        return sl, tp, sl_amount_usd, tp_amount_usd

    async def _gemini_target_pct_adjustment(
        self,
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
        gemini = self.agents.get("GeminiAgent")
        client = getattr(gemini, "_client", None)
        if not getattr(gemini, "available", False) or client is None:
            return 0.0, 0.0
        try:
            from google.genai import types as genai_types

            payload = {
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
                "output_schema": {"sl_delta_pct": "number", "tp_delta_pct": "number"},
            }
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.5-flash",
                    contents=[{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=True)}]}],
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Return JSON only with sl_delta_pct and tp_delta_pct.",
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=5.0,
            )
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`").replace("json", "", 1).strip()
            data = json.loads(text)
            sl_delta = self._clamp(float(data.get("sl_delta_pct", 0.0)), -0.02, 0.03)
            tp_delta = self._clamp(float(data.get("tp_delta_pct", 0.0)), -0.03, 0.06)
            return sl_delta, tp_delta
        except Exception as exc:
            logger.warning("Auto-trader Gemini SL/TP adjustment skipped: %s", exc)
            return 0.0, 0.0
