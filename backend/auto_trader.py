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
import math
import re
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
        self._gemini_quota_cooldown_until = 0.0

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
                    self._log_auto_trade(
                        sym,
                        signal,
                        hold_detail,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                if not decision.allow_execute:
                    logger.info("Auto-trader: %s %s rejected: %s", sym, signal.action, decision.reason)
                    self._log_auto_trade(
                        sym,
                        signal,
                        decision.reason,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
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
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Live tick unavailable at execution time",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
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
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Spread deteriorated before execution",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                entry_price = tick.ask if signal.action == "BUY" else tick.bid
                symbol_info = context.symbol_info
                contract_size = symbol_info.trade_contract_size if symbol_info else 100000
                volume = float(risk_evaluation.adjusted_volume)
                estimated_margin = self.execution.estimate_margin(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    price=entry_price,
                )
                investment = max(1.0, float(estimated_margin if estimated_margin is not None else 0.0))
                normalized_sl, normalized_tp, sl_amount_usd, tp_amount_usd = await self._dynamic_amount_based_targets(
                    decision=decision,
                    action=signal.action,
                    entry_price=entry_price,
                    volume=volume,
                    contract_size=contract_size,
                    amount_usd=investment,
                    digits=symbol_info.digits if symbol_info else 5,
                )
                normalized_sl, normalized_tp = self._normalize_live_stops(
                    symbol=sym,
                    action=signal.action,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                )
                min_rr = max(1.0, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.6) or 1.6))
                sl_distance = abs(entry_price - normalized_sl)
                tp_distance = abs(normalized_tp - entry_price)
                if sl_distance > 0 and (tp_distance / sl_distance) < min_rr:
                    target_tp_distance = sl_distance * min_rr
                    if signal.action == "BUY":
                        normalized_tp = round(entry_price + target_tp_distance, symbol_info.digits if symbol_info else 5)
                    else:
                        normalized_tp = round(entry_price - target_tp_distance, symbol_info.digits if symbol_info else 5)
                vol_min = float(symbol_info.volume_min if symbol_info and symbol_info.volume_min else 0.01)
                vol_step = float(symbol_info.volume_step if symbol_info and symbol_info.volume_step else 0.01)
                vol_max = float(symbol_info.volume_max if symbol_info and symbol_info.volume_max else 0.0)
                rebalanced_volume, rebalance_error = self._rebalance_volume_for_started_amount_risk(
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    desired_sl_amount_usd=sl_amount_usd,
                    volume=volume,
                    contract_size=contract_size,
                    vol_min=vol_min,
                    vol_step=vol_step,
                    vol_max=vol_max,
                )
                if rebalance_error:
                    self._log_auto_trade(
                        sym,
                        signal,
                        rebalance_error,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                if rebalanced_volume > 0 and rebalanced_volume < volume:
                    volume = rebalanced_volume
                    estimated_margin = self.execution.estimate_margin(
                        symbol=sym,
                        action=signal.action,
                        volume=volume,
                        price=entry_price,
                    )
                    investment = max(1.0, float(estimated_margin if estimated_margin is not None else investment))
                # Keep broker comment compact and parseable.
                comment = self._build_trade_comment(
                    investment,
                    sl_amount_usd=sl_amount_usd,
                    tp_amount_usd=tp_amount_usd,
                )

                order_req = OrderRequest(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    comment=comment,
                )
                preflight = await self.execution_service.preflight(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
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
                    self._log_auto_trade(
                        sym,
                        signal,
                        preflight.reason,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
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
                    self._log_auto_trade(
                        sym,
                        signal,
                        f"Order #{result.ticket} filled at {result.price}",
                        True,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    self.signal_pipeline.anti_churn_service.mark_symbol_opened(scan_window_id, sym)
                    all_positions = self.execution.get_positions()
                else:
                    logger.warning("AUTO-TRADE FAILED: %s %s", sym, result.retcode_desc)
                    self._log_auto_trade(
                        sym,
                        signal,
                        result.retcode_desc,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )

            except Exception as e:
                logger.error(f"Auto-trader error for {sym}: {e}", exc_info=True)

    def _log_auto_trade(
        self,
        symbol: str,
        signal: TradeSignal,
        detail: str,
        success: bool,
        quality_score: float = 0.0,
        *,
        signal_id: Optional[int] = None,
        decision=None,
    ):
        decision_reason = signal.reason or detail
        gemini_summary = ""
        meta_model_summary = ""
        if decision is not None:
            try:
                decision_reason = (
                    str(getattr(decision, "reason", "") or "")
                    or str(getattr(decision.signal_decision.final_signal, "reason", "") or "")
                    or detail
                )
                gemini = getattr(decision.signal_decision, "gemini_confirmation", None)
                if gemini is not None:
                    gemini_summary = (
                        str(getattr(gemini, "summary_reason", "") or "")
                        or str(getattr(gemini, "reason", "") or "")
                    )
                    if not gemini_summary:
                        if bool(getattr(gemini, "degraded", False)):
                            gemini_summary = "Gemini degraded; technical-only fallback used."
                        elif not bool(getattr(gemini, "used", False)):
                            gemini_summary = "Gemini advisory not used for this decision."
                else:
                    gemini_summary = "Gemini not attached to this decision."

                meta = getattr(decision.signal_decision.final_signal, "metadata", {}) or {}
                meta_model = meta.get("meta_model") or {}
                if meta_model:
                    p = float(meta_model.get("profit_probability", 0.0) or 0.0)
                    qa = float(meta_model.get("quality_after", 0.0) or 0.0)
                    changed = bool(meta_model.get("changed_decision", False))
                    blocked = bool(meta_model.get("blocked", False))
                    meta_model_summary = (
                        f"v={meta_model.get('version_id', '')} "
                        f"p={p:.2f} q={qa:.2f} changed={changed} blocked={blocked}"
                    ).strip()
            except Exception:
                pass

        entry = {
            "timestamp": time.time(),
            "symbol": symbol,
            "action": signal.action,
            "confidence": signal.confidence,
            "quality_score": quality_score,
            "detail": detail,
            "success": success,
            "signal_id": signal_id,
            "decision_reason": decision_reason or detail,
            "gemini_summary": gemini_summary,
            "meta_model_summary": meta_model_summary,
        }
        self._trade_log.append(entry)
        # Keep only last 50
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]
        if self.db:
            try:
                # Auto-trader decisions are scanner events; profit fields are not
                # available at decision time, so they are left as 0 here.
                asyncio.create_task(
                    self.db.log_ai_activity(
                        action=signal.action,
                        symbol=symbol,
                        ticket=0,
                        detail=detail,
                        profit=0.0,
                        signal_id=signal_id,
                        decision_reason=entry.get("decision_reason", ""),
                        gemini_summary=entry.get("gemini_summary", ""),
                        meta_model_summary=entry.get("meta_model_summary", ""),
                    )
                )
            except Exception:
                pass

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _build_trade_comment(
        self,
        amount_usd: float,
        *,
        sl_amount_usd: float | None = None,
        tp_amount_usd: float | None = None,
    ) -> str:
        parts = [f"TA{int(round(float(amount_usd)))}"]
        if sl_amount_usd is not None and sl_amount_usd > 0:
            parts.append(f"SLA{int(round(float(sl_amount_usd)))}")
        if tp_amount_usd is not None and tp_amount_usd > 0:
            parts.append(f"TPA{int(round(float(tp_amount_usd)))}")
        comment = "|".join(parts)
        if len(comment) <= 31:
            return comment
        compact = parts[0]
        for token in parts[1:]:
            trial = f"{compact}|{token}"
            if len(trial) <= 31:
                compact = trial
            else:
                break
        return compact[:31]

    def _round_volume_down(self, raw_volume: float, vol_min: float, vol_step: float, vol_max: float) -> float:
        if raw_volume <= 0:
            return 0.0
        step = vol_step if vol_step and vol_step > 0 else max(vol_min, 0.01)
        bounded = min(raw_volume, vol_max) if vol_max > 0 else raw_volume
        units = math.floor((bounded + 1e-12) / step)
        rounded = units * step
        if rounded + 1e-12 < vol_min:
            return 0.0
        return round(rounded, 6)

    def _rebalance_volume_for_started_amount_risk(
        self,
        *,
        entry_price: float,
        stop_loss: float,
        desired_sl_amount_usd: float,
        volume: float,
        contract_size: float,
        vol_min: float,
        vol_step: float,
        vol_max: float,
    ) -> tuple[float, str | None]:
        if (
            desired_sl_amount_usd <= 0
            or stop_loss <= 0
            or entry_price <= 0
            or volume <= 0
            or contract_size <= 0
        ):
            return volume, None
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return volume, None
        actual_sl_amount = sl_distance * volume * contract_size
        if actual_sl_amount <= desired_sl_amount_usd * 1.02:
            return volume, None
        max_volume_for_risk = desired_sl_amount_usd / (sl_distance * contract_size)
        adjusted = self._round_volume_down(max_volume_for_risk, vol_min, vol_step, vol_max)
        if adjusted <= 0:
            min_possible_risk = sl_distance * max(vol_min, 0.0) * contract_size
            return 0.0, (
                f"Skipped: broker minimum stop distance implies at least ${min_possible_risk:.2f} risk "
                f"for {self.risk_engine.user_policy.mode} sizing."
            )
        return adjusted, None

    def _normalize_live_stops(
        self,
        *,
        symbol: str,
        action: str,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[float, float]:
        """Clamp SL/TP to broker minimum distance using latest tick."""
        tick = self.market_data.get_tick(symbol)
        info = self.market_data.get_symbol_info(symbol)
        if not tick or not info:
            return stop_loss, take_profit

        digits = int(info.get("digits", 5) or 5)
        point = float(info.get("point", 0.00001) or 0.00001)
        stops_level = float(info.get("trade_stops_level", 0) or 0)
        spread_points = float(tick.spread or 10)
        min_distance = max(stops_level, spread_points, 10.0) * point * 1.5
        required = max(min_distance + point, point)
        exec_price = float(tick.ask if action == "BUY" else tick.bid)
        if exec_price <= 0:
            return stop_loss, take_profit

        sl = float(stop_loss)
        tp = float(take_profit)
        if action == "BUY":
            if abs(exec_price - sl) < required:
                sl = round(exec_price - required, digits)
            if abs(tp - exec_price) < required:
                tp = round(exec_price + required, digits)
        else:
            if abs(sl - exec_price) < required:
                sl = round(exec_price + required, digits)
            if abs(exec_price - tp) < required:
                tp = round(exec_price - required, digits)
        return sl, tp

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
        if time.time() < self._gemini_quota_cooldown_until:
            return 0.0, 0.0
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
            if hasattr(gemini, "clear_runtime_error"):
                gemini.clear_runtime_error()
            return sl_delta, tp_delta
        except Exception as exc:
            exc_text = str(exc)
            if "429" in exc_text or "RESOURCE_EXHAUSTED" in exc_text:
                retry = 60.0
                match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", exc_text, flags=re.IGNORECASE)
                if match:
                    retry = max(15.0, float(match.group(1)))
                self._gemini_quota_cooldown_until = time.time() + retry
            if hasattr(gemini, "mark_runtime_error"):
                gemini.mark_runtime_error(exc_text)
            logger.warning("Auto-trader Gemini SL/TP adjustment skipped: %s", exc)
            return 0.0, 0.0
