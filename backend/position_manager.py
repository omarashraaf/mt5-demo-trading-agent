"""
Position Manager: Autonomous management of open positions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from agent.interface import AgentInput
from agent.smart_agent import get_asset_class, get_trade_params
from mt5.connector import MT5Connector
from mt5.execution import ExecutionEngine, OrderRequest, PositionInfo
from mt5.market_data import MarketDataService
from risk.rules import RiskEngine
from services.background_state import BackgroundServiceState
from storage.db import Database

logger = logging.getLogger(__name__)

SESSION_WINDOWS_UTC: dict[str, range] = {
    "London": range(7, 16),
    "Europe": range(7, 14),
    "New York": range(12, 21),
    "US Open": range(13, 17),
    "US Midday": range(17, 20),
    "Overlap": range(12, 16),
}


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
        self._avg_down_tracker: dict[int, bool] = {}
        self._managed_tickets: set[int] = set()
        self._position_snapshots: dict[int, dict] = {}
        self._state = BackgroundServiceState(name="position_manager")
        self._on_trade_closed: Optional[Callable[[dict], Awaitable[None]]] = None

    def set_database(self, db: Database):
        self.db = db

    def set_trade_closed_callback(self, callback: Optional[Callable[[dict], Awaitable[None]]]):
        self._on_trade_closed = callback

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def activity_log(self) -> list[dict]:
        return self._activity_log[-50:]

    @property
    def managed_tickets(self) -> set[int]:
        return self._managed_tickets

    def status_snapshot(self) -> dict:
        state = self._state.model_dump()
        state["managed_tickets"] = list(self._managed_tickets)
        return state

    def start(self):
        if self.is_running:
            return
        self._running = True
        self._state.mark_started()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Position Manager STARTED")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._state.mark_stopped()
        logger.info("Position Manager STOPPED")

    async def _run_loop(self):
        while self._running:
            try:
                await asyncio.sleep(30)
                self._state.mark_heartbeat()
                if not self._running:
                    break
                if not self.connector.connected:
                    continue
                if self.risk_engine.panic_stopped:
                    continue
                if not self.risk_engine.auto_trade_enabled:
                    logger.debug("Position Manager: auto-trade disabled, skipping cycle")
                    continue
                await self._manage_positions()
                self._state.mark_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._state.mark_error(exc)
                logger.error("Position Manager error: %s", exc, exc_info=True)
                await asyncio.sleep(10)
        self._state.mark_stopped()

    async def _manage_positions(self):
        all_positions = self.execution.get_positions()
        open_tickets = {p.ticket for p in all_positions}
        disappeared_tickets = set(self._position_snapshots) - open_tickets
        for ticket in list(disappeared_tickets):
            snapshot = self._position_snapshots.pop(ticket, None)
            if snapshot:
                await self._reconcile_disappeared_position(ticket, snapshot)

        positions = [p for p in all_positions if self._is_managed_position(p)]
        self._managed_tickets = {p.ticket for p in positions}

        if positions:
            logger.debug(
                "Position Manager: managing %s/%s auto-traded positions",
                len(positions),
                len(all_positions),
            )

        closed_avg = [ticket for ticket in self._avg_down_tracker if ticket not in open_tickets]
        for ticket in closed_avg:
            del self._avg_down_tracker[ticket]

        stale_snapshots = [ticket for ticket in self._position_snapshots if ticket not in self._managed_tickets]
        for ticket in stale_snapshots:
            self._position_snapshots.pop(ticket, None)

        for pos in positions:
            try:
                await self._manage_single(pos)
            except Exception as exc:
                logger.error("Error managing position %s (%s): %s", pos.ticket, pos.symbol, exc)

    async def _manage_single(self, pos: PositionInfo):
        symbol = pos.symbol
        stored_plan = await self.db.get_position_management_plan(pos.ticket) if self.db else None
        plan_record = stored_plan or {}
        plan = plan_record.get("plan_json", {}) if stored_plan else {}
        plan_meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}
        self._remember_snapshot(pos, plan)

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
        current_price = tick.bid if is_buy else tick.ask
        entry = pos.price_open
        profit_in_price = (current_price - entry) if is_buy else (entry - current_price)
        original_risk = abs(entry - pos.stop_loss) if pos.stop_loss > 0 else atr * 1.5
        self._remember_snapshot(pos, plan, current_price=current_price, atr=atr)

        asset_class = get_asset_class(symbol)
        params = get_trade_params(asset_class)
        trail_trigger_mult = 1.5 if asset_class != "forex" else 1.0
        trail_distance_mult = 1.2 if asset_class != "forex" else 0.75
        break_even_r_multiple = float(plan_meta.get("break_even_r_multiple", 1.25))
        trailing_activation_r = float(plan_meta.get("trailing_activation_r_multiple", trail_trigger_mult))
        trail_distance_mult = float(plan_meta.get("trail_atr_multiplier", trail_distance_mult))

        time_exit = self._time_exit_reason(
            pos=pos,
            plan=plan,
            plan_meta=plan_meta,
            profit_in_price=profit_in_price,
            original_risk=original_risk,
        )
        if time_exit:
            exit_reason, detail = time_exit
            await self._close_with_outcome(
                pos,
                exit_reason,
                detail,
                activity_action="time_exit",
                plan_record=plan_record,
                plan=plan,
            )
            return

        if self._should_end_session_close(plan, plan_meta):
            planned_hold = int(
                plan.get("planned_hold_minutes")
                or plan.get("expected_hold_minutes")
                or plan_meta.get("planned_hold_minutes")
                or params["max_hold_minutes"]
            )
            age_minutes = self._position_age_minutes(pos)
            await self._close_with_outcome(
                pos,
                "end_of_session_close",
                (
                    f"Closed before session end after {age_minutes:.0f}min "
                    f"(planned hold {planned_hold}min, overnight disabled)."
                ),
                activity_action="session_close",
                plan_record=plan_record,
                plan=plan,
            )
            return

        if profit_in_price > max(atr * trail_trigger_mult, original_risk * trailing_activation_r):
            trail_distance = atr * trail_distance_mult
            if is_buy:
                new_sl = round(current_price - trail_distance, 5)
                if new_sl > pos.stop_loss and new_sl > entry:
                    result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                    if result.success:
                        self._update_snapshot_stop_loss(pos.ticket, new_sl)
                        locked = round((new_sl - entry) * pos.volume * self._get_contract_size(symbol), 2)
                        await self._log_activity(
                            "trailing_stop",
                            symbol,
                            pos.ticket,
                            f"Trailing stop moved to {new_sl:.5g} (locking ~${locked} profit)",
                            pos.profit,
                        )
                    return
            else:
                new_sl = round(current_price + trail_distance, 5)
                if new_sl < pos.stop_loss and new_sl < entry:
                    result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                    if result.success:
                        self._update_snapshot_stop_loss(pos.ticket, new_sl)
                        locked = round((entry - new_sl) * pos.volume * self._get_contract_size(symbol), 2)
                        await self._log_activity(
                            "trailing_stop",
                            symbol,
                            pos.ticket,
                            f"Trailing stop moved to {new_sl:.5g} (locking ~${locked} profit)",
                            pos.profit,
                        )
                    return

        if profit_in_price >= original_risk * break_even_r_multiple and pos.stop_loss > 0:
            spread_buffer = (tick.ask - tick.bid) * 1.5
            if is_buy and pos.stop_loss < entry:
                new_sl = round(entry + spread_buffer, 5)
                result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                if result.success:
                    self._update_snapshot_stop_loss(pos.ticket, new_sl)
                    await self._log_activity(
                        "breakeven",
                        symbol,
                        pos.ticket,
                        f"Moved to breakeven at {new_sl:.5g} - risk eliminated",
                        pos.profit,
                    )
                return
            if not is_buy and pos.stop_loss > entry:
                new_sl = round(entry - spread_buffer, 5)
                result = self.execution.modify_position(pos.ticket, new_sl, pos.take_profit)
                if result.success:
                    self._update_snapshot_stop_loss(pos.ticket, new_sl)
                    await self._log_activity(
                        "breakeven",
                        symbol,
                        pos.ticket,
                        f"Moved to breakeven at {new_sl:.5g} - risk eliminated",
                        pos.profit,
                    )
                return

        h4_bars = self.market_data.get_bars(symbol, "H4", 60)
        trend_bars = h4_bars if len(h4_bars) >= 52 else h1_bars
        h1_closes = [bar.close for bar in h1_bars]
        rsi = self._calc_rsi(h1_closes, 14) if len(h1_closes) > 15 else 50.0

        if pos.profit < 0:
            if is_buy and rsi < 35:
                await self._close_with_outcome(
                    pos,
                    "time_stop",
                    f"Closed losing BUY early - RSI dropped to {rsi:.0f}, invalidating the thesis.",
                    activity_action="close_reversal",
                    plan_record=plan_record,
                    plan=plan,
                )
                return
            if not is_buy and rsi > 65:
                await self._close_with_outcome(
                    pos,
                    "time_stop",
                    f"Closed losing SELL early - RSI rose to {rsi:.0f}, invalidating the thesis.",
                    activity_action="close_reversal",
                    plan_record=plan_record,
                    plan=plan,
                )
                return

        if len(trend_bars) >= 52:
            closes = [bar.close for bar in trend_bars]
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            if is_buy and ema20[-1] < ema50[-1] and ema20[-2] < ema50[-2]:
                await self._close_with_outcome(
                    pos,
                    "time_stop",
                    "Closed BUY - stored trend thesis reversed to bearish.",
                    activity_action="close_reversal",
                    plan_record=plan_record,
                    plan=plan,
                )
                return
            if not is_buy and ema20[-1] > ema50[-1] and ema20[-2] > ema50[-2]:
                await self._close_with_outcome(
                    pos,
                    "time_stop",
                    "Closed SELL - stored trend thesis reversed to bullish.",
                    activity_action="close_reversal",
                    plan_record=plan_record,
                    plan=plan,
                )
                return

        if False and pos.profit < 0 and pos.ticket not in self._avg_down_tracker:
            if abs(profit_in_price) > atr * 0.5:
                smart_agent = self.agents.get("SmartAgent")
                if smart_agent:
                    m15_bars = self.market_data.get_bars(symbol, "M15", 100)
                    account = self.connector.refresh_account()
                    if account and len(m15_bars) >= 22 and len(h1_bars) >= 52:
                        input_data = AgentInput(
                            symbol=symbol,
                            timeframe="H1",
                            bars=[self._bar_to_dict(bar) for bar in h1_bars],
                            spread=tick.spread,
                            account_equity=account.equity,
                            open_positions=[],
                            multi_tf_bars={
                                "M15": [self._bar_to_dict(bar) for bar in m15_bars],
                                "H1": [self._bar_to_dict(bar) for bar in h1_bars],
                                "H4": [self._bar_to_dict(bar) for bar in (h4_bars if h4_bars else h1_bars)],
                            },
                        )
                        signal = smart_agent.evaluate(input_data)
                        if signal.confidence >= 0.60 and (
                            (is_buy and signal.action == "BUY") or (not is_buy and signal.action == "SELL")
                        ):
                            avg_volume = max(round(pos.volume * 0.5, 2), 0.01)
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
                                    "average_down",
                                    symbol,
                                    pos.ticket,
                                    f"Averaged down with {avg_volume} lots at better price (AI confidence: {signal.confidence:.0%})",
                                    pos.profit,
                                )
                            return

    def _is_managed_position(self, pos: PositionInfo) -> bool:
        return (
            pos.comment.startswith("TA:")
            or pos.comment == "TradingAgent"
            or pos.comment.startswith("AVG:")
        )

    def _remember_snapshot(
        self,
        pos: PositionInfo,
        plan: dict,
        *,
        current_price: float | None = None,
        atr: float | None = None,
    ):
        plan_meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}
        info = self.market_data.get_symbol_info(pos.symbol) or {}
        self._position_snapshots[pos.ticket] = {
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "action": pos.type,
            "price_open": pos.price_open,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "profit": pos.profit,
            "opened_at": pos.time,
            "last_seen": time.time(),
            "last_price": current_price if current_price is not None else pos.price_current,
            "atr": atr or 0.0,
            "point": float(info.get("point", 0.0001) or 0.0001),
            "planned_hold_minutes": int(
                plan.get("planned_hold_minutes")
                or plan.get("expected_hold_minutes")
                or plan_meta.get("planned_hold_minutes")
                or 0
            ),
            "symbol_category": plan_meta.get("symbol_category", ""),
            "strategy": plan.get("strategy") or plan_meta.get("strategy_family", ""),
            "plan": plan,
        }

    def _update_snapshot_stop_loss(self, ticket: int, stop_loss: float):
        snapshot = self._position_snapshots.get(ticket)
        if snapshot:
            snapshot["stop_loss"] = stop_loss
            snapshot["last_seen"] = time.time()

    def _position_age_minutes(self, pos: PositionInfo) -> float:
        return max(0.0, (time.time() - pos.time) / 60.0)

    def _time_exit_reason(
        self,
        *,
        pos: PositionInfo,
        plan: dict,
        plan_meta: dict,
        profit_in_price: float,
        original_risk: float,
    ) -> tuple[str, str] | None:
        planned_hold = int(
            plan.get("planned_hold_minutes")
            or plan.get("expected_hold_minutes")
            or plan_meta.get("planned_hold_minutes")
            or plan_meta.get("time_stop_minutes")
            or 0
        )
        if planned_hold <= 0:
            return None

        age_minutes = self._position_age_minutes(pos)
        progress_r = profit_in_price / max(original_risk, 0.0000001)
        stale_after = int(
            plan.get("stale_after_minutes")
            or plan_meta.get("stale_after_minutes")
            or max(15, planned_hold * 0.65)
        )
        min_progress = float(
            plan.get("min_progress_r_multiple")
            or plan_meta.get("min_progress_r_multiple")
            or 0.20
        )
        target_progress = float(
            plan.get("target_progress_r_multiple")
            or plan_meta.get("target_progress_r_multiple")
            or 0.35
        )
        thesis_event_risk = str(plan_meta.get("event_risk", "low")).lower()

        if thesis_event_risk == "high" and age_minutes >= planned_hold * 0.5 and progress_r < min_progress:
            return (
                "time_stop",
                (
                    f"Closed under high event risk after {age_minutes:.0f}min - "
                    f"progress stalled at {progress_r:.2f}R before the planned {planned_hold}min hold."
                ),
            )

        if age_minutes >= stale_after and progress_r < min_progress:
            return (
                "time_stop",
                (
                    f"Closed stale trade after {age_minutes:.0f}min - "
                    f"progress only reached {progress_r:.2f}R (need {min_progress:.2f}R by {stale_after}min)."
                ),
            )

        if age_minutes >= planned_hold:
            if progress_r < target_progress:
                return (
                    "time_stop",
                    (
                        f"Closed by time stop after {age_minutes:.0f}min - "
                        f"trade only reached {progress_r:.2f}R vs required {target_progress:.2f}R."
                    ),
                )
            return (
                "time_stop",
                f"Closed by time stop after planned hold window expired at {age_minutes:.0f}min.",
            )

        return None

    def _should_end_session_close(self, plan: dict, plan_meta: dict) -> bool:
        close_before_session_end = bool(
            plan.get("close_before_session_end") or plan_meta.get("close_before_session_end")
        )
        if not close_before_session_end:
            return False

        sessions = list(plan_meta.get("sessions") or [])
        if not sessions or "24/7" in sessions:
            return False

        now = datetime.now(timezone.utc)
        active_session_ends = []
        all_session_ends_today = []
        for session in sessions:
            hours = SESSION_WINDOWS_UTC.get(session)
            if not hours:
                continue
            start_hour = hours.start
            end_hour = hours.stop
            session_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            session_end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
            all_session_ends_today.append(session_end)
            if session_start <= now < session_end:
                active_session_ends.append(session_end)

        buffer_minutes = int(
            plan.get("session_close_buffer_minutes")
            or plan_meta.get("session_close_buffer_minutes")
            or 10
        )
        if active_session_ends:
            nearest_end = min(active_session_ends)
            minutes_to_end = (nearest_end - now).total_seconds() / 60.0
            return minutes_to_end <= buffer_minutes

        if all_session_ends_today and now >= max(all_session_ends_today):
            return True

        return False

    async def _reconcile_disappeared_position(self, ticket: int, snapshot: dict):
        if self.db is None:
            return

        existing_outcome = await self.db.get_trade_outcome_by_ticket(ticket)
        if existing_outcome is not None:
            return

        plan_record = await self.db.get_position_management_plan(ticket)
        if plan_record and plan_record.get("status") == "closed":
            return

        signal_id = plan_record.get("signal_id") if plan_record else None
        confidence = 0.0
        if signal_id:
            signal = await self.db.get_signal_by_id(signal_id)
            if signal:
                confidence = float(signal.get("confidence", 0.0))

        exit_reason = self._classify_passive_exit(snapshot)
        holding_minutes = max(
            0.0,
            (snapshot.get("last_seen", time.time()) - snapshot.get("opened_at", time.time())) / 60.0,
        )
        await self.db.log_trade_outcome(
            ticket=ticket,
            signal_id=signal_id,
            symbol=snapshot.get("symbol", ""),
            action=snapshot.get("action", ""),
            confidence=confidence,
            profit=float(snapshot.get("profit", 0.0)),
            exit_reason=exit_reason,
            holding_minutes=holding_minutes,
            symbol_category=snapshot.get("symbol_category", ""),
            strategy=snapshot.get("strategy", ""),
            planned_hold_minutes=snapshot.get("planned_hold_minutes", 0),
            outcome_json={
                "detail": "Position closed outside the manager loop; reconciled from last known state.",
                "close_price": snapshot.get("last_price"),
            },
        )
        await self.db.mark_evaluation_outcome(signal_id, "closed")

    def _classify_passive_exit(self, snapshot: dict) -> str:
        entry = float(snapshot.get("price_open", 0.0))
        stop_loss = float(snapshot.get("stop_loss", 0.0))
        take_profit = float(snapshot.get("take_profit", 0.0))
        last_price = float(snapshot.get("last_price", 0.0))
        point = max(float(snapshot.get("point", 0.0001)), 0.00001)
        tolerance = max(point * 25, abs(entry) * 0.0015)
        profit = float(snapshot.get("profit", 0.0))
        action = snapshot.get("action", "BUY")

        if take_profit > 0 and abs(last_price - take_profit) <= tolerance:
            return "take_profit"

        if stop_loss > 0 and abs(last_price - stop_loss) <= tolerance:
            if self._is_breakeven_stop(action, entry, stop_loss, tolerance):
                return "breakeven"
            if self._is_trailing_stop(action, entry, stop_loss, tolerance):
                return "trailing_stop"
            return "stop_loss"

        if take_profit > 0 and profit > 0 and last_price >= take_profit - tolerance and action == "BUY":
            return "take_profit"
        if take_profit > 0 and profit > 0 and last_price <= take_profit + tolerance and action == "SELL":
            return "take_profit"
        if self._is_trailing_stop(action, entry, stop_loss, tolerance) and profit > 0:
            return "trailing_stop"
        if self._is_breakeven_stop(action, entry, stop_loss, tolerance) and profit >= 0:
            return "breakeven"
        return "take_profit" if profit > 0 else "stop_loss"

    def _is_breakeven_stop(self, action: str, entry: float, stop_loss: float, tolerance: float) -> bool:
        if action == "BUY":
            return stop_loss >= entry - tolerance and stop_loss <= entry + tolerance * 2
        return stop_loss <= entry + tolerance and stop_loss >= entry - tolerance * 2

    def _is_trailing_stop(self, action: str, entry: float, stop_loss: float, tolerance: float) -> bool:
        if action == "BUY":
            return stop_loss > entry + tolerance
        return stop_loss < entry - tolerance

    def _plan_context(self, plan_record: dict | None, plan: dict | None) -> tuple[Optional[int], float, str, str, int]:
        signal_id = plan_record.get("signal_id") if plan_record else None
        confidence = 0.0
        category = ""
        strategy = ""
        planned_hold_minutes = 0
        if plan:
            meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}
            category = meta.get("symbol_category", "")
            strategy = plan.get("strategy") or meta.get("strategy_family", "")
            planned_hold_minutes = int(
                plan.get("planned_hold_minutes")
                or plan.get("expected_hold_minutes")
                or meta.get("planned_hold_minutes")
                or 0
            )
        return signal_id, confidence, category, strategy, planned_hold_minutes

    async def _log_activity(self, action: str, symbol: str, ticket: int, detail: str, profit: float):
        logger.info("[PositionManager] %s: %s #%s - %s (P&L: %.2f)", action, symbol, ticket, detail, profit)
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
            except Exception as exc:
                logger.error("Failed to log AI activity: %s", exc)

    async def _close_with_outcome(
        self,
        pos: PositionInfo,
        exit_reason: str,
        detail: str,
        *,
        activity_action: str | None = None,
        plan_record: dict | None = None,
        plan: dict | None = None,
    ):
        result = self.execution.close_position(pos.ticket)
        if not result.success:
            return

        await self._log_activity(activity_action or exit_reason, pos.symbol, pos.ticket, detail, pos.profit)
        self._position_snapshots.pop(pos.ticket, None)
        self._avg_down_tracker.pop(pos.ticket, None)

        if self.db:
            if plan_record is None:
                plan_record = await self.db.get_position_management_plan(pos.ticket)
            if plan is None and plan_record:
                plan = plan_record.get("plan_json", {})

            signal_id, confidence, category, strategy, planned_hold_minutes = self._plan_context(plan_record, plan)
            if signal_id:
                signal = await self.db.get_signal_by_id(signal_id)
                if signal:
                    confidence = float(signal.get("confidence", 0.0))

            await self.db.log_trade_outcome(
                ticket=pos.ticket,
                signal_id=signal_id,
                symbol=pos.symbol,
                action=pos.type,
                confidence=confidence,
                profit=pos.profit,
                exit_reason=exit_reason,
                holding_minutes=self._position_age_minutes(pos),
                symbol_category=category,
                strategy=strategy,
                planned_hold_minutes=planned_hold_minutes,
                outcome_json={
                    "action": activity_action or exit_reason,
                    "ticket": result.ticket,
                    "close_price": result.price,
                    "detail": detail,
                },
            )
            await self.db.mark_evaluation_outcome(signal_id, "closed")
            if self._on_trade_closed is not None:
                await self._on_trade_closed(
                    {
                        "ticket": pos.ticket,
                        "symbol": pos.symbol,
                        "exit_reason": exit_reason,
                        "profit": pos.profit,
                        "signal_id": signal_id,
                    }
                )

    def _bar_to_dict(self, bar) -> dict:
        return {
            "time": bar.time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }

    def _calc_atr(self, bars, period: int = 14) -> float:
        if len(bars) < period + 1:
            ranges = [bar.high - bar.low for bar in bars[-period:]]
            return sum(ranges) / len(ranges) if ranges else 0.0001
        true_ranges = []
        for index in range(1, len(bars)):
            high, low, previous_close = bars[index].high, bars[index].low, bars[index - 1].close
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        return sum(true_ranges[-period:]) / period

    def _ema(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return data[:]
        smoothing = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * smoothing + ema[-1] * (1 - smoothing))
        return [0.0] * (len(data) - len(ema)) + ema

    def _calc_rsi(self, closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
        gains = [max(0, delta) for delta in deltas]
        losses = [max(0, -delta) for delta in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for index in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[index]) / period
            avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _get_contract_size(self, symbol: str) -> float:
        info = self.market_data.get_symbol_info(symbol)
        return info.get("trade_contract_size", 100000) if info else 100000
