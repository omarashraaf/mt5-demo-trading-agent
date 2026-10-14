"""
Position Manager: Autonomous management of open positions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Literal
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
MIN_FORCE_TIME_EXIT_SECONDS = 600
BREAKEVEN_MIN_TP_PROGRESS = 0.35
BREAKEVEN_MIN_PROFIT_USD = 25.0
PROFIT_PULLBACK_MIN_TP_PROGRESS = 0.40
PROFIT_PULLBACK_DEFAULT_GIVEBACK_PCT = 0.35
PROFIT_PULLBACK_MIN_PEAK_PROFIT_USD = 5.0


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
        # Fast cadence helps TP-ladder pullback exits trigger before reversals
        # rebound above the protected floor.
        self._manage_interval_seconds: int = 2
        self._portfolio_stop_loss_usd: float | None = None
        self._portfolio_take_profit_usd: float | None = None
        self._portfolio_targets_updated_at: float = 0.0
        self._portfolio_last_trigger: dict | None = None
        self._portfolio_close_inflight = False
        self._portfolio_targets_mode: Literal["auto", "manual"] = "auto"
        self._portfolio_targets_auto_recalc_at: float = 0.0

    @staticmethod
    def _is_fresh_tick(tick, *, now: float | None = None, max_age_seconds: float = 120.0) -> bool:
        if tick is None:
            return False
        try:
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
        except Exception:
            return False
        if bid <= 0 or ask <= 0:
            return False

        raw_timestamp = 0.0
        for attr in ("time_msc", "time", "timestamp"):
            try:
                raw_timestamp = float(getattr(tick, attr, 0.0) or 0.0)
            except Exception:
                raw_timestamp = 0.0
            if raw_timestamp > 0:
                break
        if raw_timestamp <= 0:
            return False
        if raw_timestamp > 1_000_000_000_000:
            raw_timestamp /= 1000.0

        now_ts = float(now if now is not None else time.time())
        return -5.0 <= (now_ts - raw_timestamp) <= float(max_age_seconds)

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
        state["manage_interval_seconds"] = self._manage_interval_seconds
        state["portfolio_targets"] = self.portfolio_targets_snapshot()
        return state

    def portfolio_targets_snapshot(self) -> dict:
        return {
            "stop_loss_usd": self._portfolio_stop_loss_usd,
            "take_profit_usd": self._portfolio_take_profit_usd,
            "updated_at": self._portfolio_targets_updated_at,
            "close_inflight": self._portfolio_close_inflight,
            "mode": self._portfolio_targets_mode,
            "last_trigger": dict(self._portfolio_last_trigger or {}),
        }

    def set_portfolio_targets(
        self,
        *,
        stop_loss_usd: float | None = None,
        take_profit_usd: float | None = None,
        mode: Literal["auto", "manual"] | None = None,
        manual_override: bool = True,
    ) -> dict:
        def _clean(value: float | None) -> float | None:
            if value is None:
                return None
            try:
                numeric = abs(float(value))
            except (TypeError, ValueError):
                return None
            return round(numeric, 2) if numeric > 0 else None

        if mode in {"auto", "manual"}:
            self._portfolio_targets_mode = mode
        elif manual_override:
            self._portfolio_targets_mode = "manual"
        self._portfolio_stop_loss_usd = _clean(stop_loss_usd)
        self._portfolio_take_profit_usd = _clean(take_profit_usd)
        self._portfolio_targets_updated_at = time.time()
        self._portfolio_last_trigger = None
        self._portfolio_close_inflight = False
        return self.portfolio_targets_snapshot()

    def enable_auto_portfolio_targets(self) -> dict:
        self._portfolio_targets_mode = "auto"
        self._portfolio_targets_auto_recalc_at = 0.0
        self._portfolio_stop_loss_usd = None
        self._portfolio_take_profit_usd = None
        self._portfolio_targets_updated_at = time.time()
        self._portfolio_last_trigger = None
        self._portfolio_close_inflight = False
        return self.portfolio_targets_snapshot()

    @property
    def manage_interval_seconds(self) -> int:
        return self._manage_interval_seconds

    @manage_interval_seconds.setter
    def manage_interval_seconds(self, value: int):
        try:
            self._manage_interval_seconds = max(1, min(60, int(value)))
        except (TypeError, ValueError):
            self._manage_interval_seconds = 2

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
        last_manage_cycle_at = 0.0
        while self._running:
            try:
                await asyncio.sleep(1.0)
                self._state.mark_heartbeat()
                if not self._running:
                    break
                if not self.connector.connected:
                    continue
                all_positions = self.execution.get_positions()
                await self._refresh_auto_portfolio_targets(all_positions)
                await self._check_portfolio_targets(all_positions)
                if self.risk_engine.panic_stopped:
                    continue
                # Keep protective management active even when auto-trading is OFF.
                # SL/TP, money guards, and protective exits must continue to run
                # for existing open trades (including manual positions).
                now = time.time()
                if now - last_manage_cycle_at < float(self._manage_interval_seconds):
                    continue
                last_manage_cycle_at = now
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

    async def _check_portfolio_targets(self, all_positions: list[PositionInfo] | None = None):
        if self._portfolio_close_inflight:
            return
        stop_loss_usd = self._portfolio_stop_loss_usd
        take_profit_usd = self._portfolio_take_profit_usd
        if stop_loss_usd is None and take_profit_usd is None:
            return
        all_positions = all_positions or self.execution.get_positions()
        if not all_positions:
            return
        if self._portfolio_targets_mode == "auto":
            unprotected_positions = [
                pos for pos in all_positions if not self._has_valid_broker_targets(pos)
            ]
            if not unprotected_positions:
                return
            all_positions = unprotected_positions
        # Prevent flash close behavior: portfolio targets should not close
        # positions that were just opened seconds ago.
        youngest_age_seconds = min(
            (self._position_age_seconds(pos) for pos in all_positions),
            default=999999.0,
        )
        if youngest_age_seconds < 120.0:
            return
        # Avoid instant mass-close right after auto targets are recalculated.
        if (time.time() - float(self._portfolio_targets_updated_at or 0.0)) < 20.0:
            return
        total_profit = sum(float(getattr(pos, "profit", 0.0) or 0.0) for pos in all_positions)
        exit_reason: str | None = None
        detail: str | None = None
        if stop_loss_usd is not None:
            sl_value = abs(stop_loss_usd)
            sl_breach_buffer = max(1.0, sl_value * 0.03)
            if total_profit <= -(sl_value + sl_breach_buffer):
                exit_reason = "portfolio_stop_loss"
                detail = (
                    f"Portfolio stop-loss hit at ${total_profit:.2f} "
                    f"(threshold -${sl_value:.2f}). Closing all active trades."
                )
        if not exit_reason and take_profit_usd is not None:
            tp_value = abs(take_profit_usd)
            tp_breach_buffer = max(1.0, tp_value * 0.03)
            if total_profit >= (tp_value + tp_breach_buffer):
                exit_reason = "portfolio_take_profit"
                detail = (
                    f"Portfolio take-profit hit at ${total_profit:.2f} "
                    f"(threshold +${tp_value:.2f}). Closing all active trades."
                )
        if not exit_reason or not detail:
            return
        self._portfolio_last_trigger = {
            "reason": exit_reason,
            "detail": detail,
            "triggered_at": time.time(),
            "total_profit": round(total_profit, 2),
            "positions": len(all_positions),
        }
        self.set_portfolio_targets(stop_loss_usd=None, take_profit_usd=None)
        await self._close_positions_for_portfolio_target(all_positions, exit_reason, detail)

    async def _refresh_auto_portfolio_targets(self, positions: list[PositionInfo]) -> None:
        if self._portfolio_targets_mode != "auto":
            return
        now = time.time()
        if (now - self._portfolio_targets_auto_recalc_at) < 15.0:
            return
        self._portfolio_targets_auto_recalc_at = now
        if not positions:
            if self._portfolio_stop_loss_usd is not None or self._portfolio_take_profit_usd is not None:
                self.set_portfolio_targets(
                    stop_loss_usd=None,
                    take_profit_usd=None,
                    mode="auto",
                    manual_override=False,
                )
            return
        suggested_sl, suggested_tp = self._suggest_portfolio_targets(positions)
        # Never allow tiny auto portfolio targets that can trigger on noise.
        auto_floor = max(25.0, self._portfolio_budget_base() * 0.0025)
        if suggested_sl is not None:
            suggested_sl = max(auto_floor, float(suggested_sl))
        if suggested_tp is not None:
            suggested_tp = max(auto_floor, float(suggested_tp))
        current_sl = float(self._portfolio_stop_loss_usd or 0.0)
        current_tp = float(self._portfolio_take_profit_usd or 0.0)
        next_sl = float(suggested_sl or 0.0)
        next_tp = float(suggested_tp or 0.0)
        if abs(current_sl - next_sl) < 0.5 and abs(current_tp - next_tp) < 0.5:
            return
        self.set_portfolio_targets(
            stop_loss_usd=suggested_sl,
            take_profit_usd=suggested_tp,
            mode="auto",
            manual_override=False,
        )

    def _suggest_portfolio_targets(self, positions: list[PositionInfo]) -> tuple[float | None, float | None]:
        if not positions:
            return (None, None)
        budget_base = self._portfolio_budget_base()
        total_profit = sum(float(getattr(pos, "profit", 0.0) or 0.0) for pos in positions)
        min_trigger_usd = max(10.0, budget_base * 0.001)
        remaining_tp_gain_usd = 0.0
        remaining_sl_loss_usd = 0.0
        tp_samples = 0
        sl_samples = 0

        for pos in positions:
            current_price = float(getattr(pos, "price_current", 0.0) or 0.0)
            if current_price <= 0:
                current_price = float(getattr(pos, "price_open", 0.0) or 0.0)
            if current_price <= 0:
                continue
            volume = float(getattr(pos, "volume", 0.0) or 0.0)
            if volume <= 0:
                continue
            usd_per_price = max(0.0000001, self._get_contract_size(pos.symbol) * volume)
            side = str(getattr(pos, "type", "") or "").upper()
            take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
            stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
            max_reasonable_distance = current_price * 0.35
            max_reasonable_contribution = max(50.0, budget_base * 0.30)

            if take_profit > 0:
                if side == "BUY" and take_profit > current_price:
                    distance = take_profit - current_price
                    if distance <= max_reasonable_distance:
                        contribution = min(distance * usd_per_price, max_reasonable_contribution)
                        remaining_tp_gain_usd += contribution
                        tp_samples += 1
                elif side == "SELL" and take_profit < current_price:
                    distance = current_price - take_profit
                    if distance <= max_reasonable_distance:
                        contribution = min(distance * usd_per_price, max_reasonable_contribution)
                        remaining_tp_gain_usd += contribution
                        tp_samples += 1

            if stop_loss > 0:
                if side == "BUY" and stop_loss < current_price:
                    distance = current_price - stop_loss
                    if distance <= max_reasonable_distance:
                        contribution = min(distance * usd_per_price, max_reasonable_contribution)
                        remaining_sl_loss_usd += contribution
                        sl_samples += 1
                elif side == "SELL" and stop_loss > current_price:
                    distance = stop_loss - current_price
                    if distance <= max_reasonable_distance:
                        contribution = min(distance * usd_per_price, max_reasonable_contribution)
                        remaining_sl_loss_usd += contribution
                        sl_samples += 1

        suggested_tp = None
        if tp_samples > 0:
            expected_tp_profit = total_profit + remaining_tp_gain_usd
            max_portfolio_tp = max(100.0, budget_base * 0.22)
            if expected_tp_profit > 0:
                target = max(min_trigger_usd, expected_tp_profit)
                if total_profit > 0:
                    target = max(target, total_profit + min_trigger_usd)
                suggested_tp = round(min(max_portfolio_tp, target), 2)

        suggested_sl = None
        if sl_samples > 0:
            expected_sl_profit = total_profit - remaining_sl_loss_usd
            max_portfolio_sl = max(100.0, budget_base * 0.12)
            if expected_sl_profit < 0:
                target = max(min_trigger_usd, abs(expected_sl_profit))
                if total_profit < 0:
                    target = max(target, abs(total_profit) + min_trigger_usd)
                suggested_sl = round(min(max_portfolio_sl, target), 2)
            else:
                suggested_sl = None

        return (suggested_sl, suggested_tp)

    def _portfolio_budget_base(self) -> float:
        try:
            mode = str(getattr(self.risk_engine, "auto_trade_budget_mode", "full_margin") or "full_margin").lower()
            custom_budget = float(getattr(self.risk_engine, "auto_trade_budget_usd", 0.0) or 0.0)
            if mode == "custom_budget" and custom_budget > 0:
                return max(100.0, custom_budget)
        except Exception:
            pass
        try:
            account = self.connector.refresh_account()
            equity = float(getattr(account, "equity", 0.0) or 0.0)
            if equity > 0:
                return max(100.0, equity)
        except Exception:
            pass
        return 10000.0

    async def _manage_single(self, pos: PositionInfo):
        symbol = pos.symbol
        stored_plan = None
        order_record = None
        if self.db:
            try:
                stored_plan = await self.db.get_position_management_plan(pos.ticket)
            except Exception as exc:
                logger.warning(
                    "Position Manager: plan lookup failed for %s #%s: %s",
                    pos.symbol,
                    pos.ticket,
                    exc,
                )
            try:
                order_record = await self.db.get_order_open_record(pos.ticket)
            except Exception as exc:
                logger.warning(
                    "Position Manager: order lookup failed for %s #%s: %s",
                    pos.symbol,
                    pos.ticket,
                    exc,
                )
        plan_record = stored_plan or {}
        if not plan_record and isinstance(order_record, dict):
            plan_record = {"created_at": float(order_record.get("timestamp") or 0.0)}
        plan = plan_record.get("plan_json", {}) if stored_plan else {}
        plan_meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}
        self._remember_snapshot(pos, plan, plan_record=plan_record)

        is_buy = pos.type == "BUY"
        tick = self.market_data.get_tick(symbol)
        if tick and not self._is_fresh_tick(tick):
            logger.debug(
                "Position Manager: ignoring stale or undated MT5 tick for %s #%s",
                symbol,
                pos.ticket,
            )
            tick = None
        current_price = (
            (tick.bid if is_buy else tick.ask)
            if tick
            else float(getattr(pos, "price_current", 0.0) or 0.0)
        )
        entry = pos.price_open
        point = max(float(getattr(pos, "point", 0.0) or 0.0), 0.00001)
        price_tolerance = max(point * 2.0, abs(entry) * 0.00005)
        age_seconds = self._position_age_seconds(pos, plan_record=plan_record)
        min_manage_age_seconds = int(
            plan.get("min_manage_age_seconds")
            or plan_meta.get("min_manage_age_seconds")
            or 90
        )
        self._remember_snapshot(pos, plan, current_price=current_price, plan_record=plan_record)

        manual_targets = bool(plan_meta.get("manual_target_override"))
        if tick and not manual_targets and await self._maybe_realign_targets_from_comment(pos, tick):
            return

        if tick and await self._maybe_fix_invalid_targets(pos, plan=plan, plan_meta=plan_meta):
            return
        if not manual_targets and await self._maybe_enforce_min_rr_targets(pos):
            return
        if not manual_targets and await self._maybe_cap_outlier_targets(pos):
            return

        # Hard monetary SL/TP guard (based on intended SLA/TPA from order comment).
        # This keeps behavior aligned with dashboard money-risk limits even when
        # broker-side price stops are delayed or temporarily mis-scaled.
        profit_usd = float(getattr(pos, "profit", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        has_valid_broker_targets = self._has_valid_broker_targets(pos)
        has_any_broker_targets = stop_loss > 0 and take_profit > 0
        allow_non_target_close = not has_valid_broker_targets
        monetary_limits = self._resolve_comment_risk_amounts(pos)
        sl_amount_usd = monetary_limits.get("sla")
        tp_amount_usd = monetary_limits.get("tpa")
        # Money guards are fallback safety only. When the broker already has
        # valid SL/TP prices, let those broker targets decide the exit so the
        # trade does not close "by itself" before reaching the visible target.
        money_guard_allowed = not has_valid_broker_targets
        if money_guard_allowed and sl_amount_usd is not None and profit_usd <= -abs(sl_amount_usd):
            await self._close_with_outcome(
                pos,
                "stop_loss_amount",
                (
                    f"Closed by money stop-loss guard at ${profit_usd:.2f} "
                    f"(limit -${abs(sl_amount_usd):.2f})."
                ),
                activity_action="stop_loss_amount",
                plan_record=plan_record,
                plan=plan,
            )
            return
        if money_guard_allowed and tp_amount_usd is not None and profit_usd >= abs(tp_amount_usd):
            await self._close_with_outcome(
                pos,
                "take_profit_amount",
                (
                    f"Closed by money take-profit guard at ${profit_usd:.2f} "
                    f"(limit +${abs(tp_amount_usd):.2f})."
                ),
                activity_action="take_profit_amount",
                plan_record=plan_record,
                plan=plan,
            )
            return

        # Hard live-test safety rule:
        # if a trade is still losing after 10 minutes, close it instead of
        # letting weak setups linger and stack more same-symbol entries.
        if (not has_valid_broker_targets) and age_seconds >= 600.0 and profit_usd < 0.0:
            await self._close_with_outcome(
                pos,
                "ten_minute_loss_cut",
                (
                    f"Closed losing trade after {age_seconds/60.0:.0f}min at ${profit_usd:.2f}. "
                    "Live safety rule: no trade may stay negative beyond 10 minutes."
                ),
                activity_action="ten_minute_loss_cut",
                plan_record=plan_record,
                plan=plan,
            )
            return

        # Hard per-trade monetary risk cap (fallback guard) so a mis-scaled SL
        # cannot leave a position running far beyond configured risk.
        try:
            account = self.connector.refresh_account()
            equity = float(getattr(account, "equity", 0.0) or 0.0)
        except Exception:
            equity = 0.0
        max_risk_pct = float(getattr(self.risk_engine.user_policy, "max_risk_per_trade", 0.0) or 0.0)
        if equity > 0 and max_risk_pct > 0:
            risk_budget_usd = equity * (max_risk_pct / 100.0)
            hard_cap_loss = max(25.0, risk_budget_usd * 1.25)
            broker_sl_loss = self._broker_stop_loss_usd(pos)
            risk_budget_stop_allowed = (not has_valid_broker_targets) or (
                broker_sl_loss > hard_cap_loss * 1.10
            )
            if risk_budget_stop_allowed and float(getattr(pos, "profit", 0.0) or 0.0) <= -hard_cap_loss:
                await self._close_with_outcome(
                    pos,
                    "risk_budget_stop",
                    (
                        f"Closed by hard risk budget stop at ${float(getattr(pos, 'profit', 0.0) or 0.0):.2f} "
                        f"(cap -${hard_cap_loss:.2f})."
                    ),
                    activity_action="risk_budget_stop",
                    plan_record=plan_record,
                    plan=plan,
                )
                return

        if tick and await self._maybe_manage_tp_ladder(
            pos=pos,
            plan_record=plan_record,
            plan=plan,
            plan_meta=plan_meta,
            current_price=current_price,
        ):
            return

        early_profit_in_price = (current_price - entry) if is_buy else (entry - current_price)
        if tick and await self._maybe_close_profit_pullback(
            pos=pos,
            plan_record=plan_record,
            plan=plan,
            plan_meta=plan_meta,
            current_price=current_price,
            profit_in_price=early_profit_in_price,
        ):
            return

        # Local protective guard: if market price already crossed broker SL/TP but
        # the broker position is still open, close it from manager side.
        broker_price = float(getattr(pos, "price_current", 0.0) or 0.0)
        if tick or broker_price > 0:
            bid_like = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0
            ask_like = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
            if is_buy:
                refs = [v for v in [bid_like, broker_price, current_price] if v > 0]
                if not refs:
                    refs = [current_price] if current_price > 0 else []
                if not refs:
                    return
                ref_low = min(refs)
                ref_high = max(refs)
                sl_hit = stop_loss > 0 and ref_low <= (stop_loss + price_tolerance)
                tp_hit = take_profit > 0 and ref_high >= (take_profit - price_tolerance)
            else:
                refs = [v for v in [ask_like, broker_price, current_price] if v > 0]
                if not refs:
                    refs = [current_price] if current_price > 0 else []
                if not refs:
                    return
                ref_high = max(refs)
                ref_low = min(refs)
                sl_hit = stop_loss > 0 and ref_high >= (stop_loss - price_tolerance)
                tp_hit = take_profit > 0 and ref_low <= (take_profit + price_tolerance)
            if sl_hit or tp_hit:
                exit_reason = "take_profit" if tp_hit else "stop_loss"
                await self._close_with_outcome(
                    pos,
                    exit_reason,
                    (
                        f"Closed by local protective guard after price crossed {'TP' if tp_hit else 'SL'} "
                        f"(TP={take_profit:.5g}, SL={stop_loss:.5g})."
                    ),
                    activity_action="protective_exit",
                    plan_record=plan_record,
                    plan=plan,
                )
                return

        h1_bars = self.market_data.get_bars(symbol, "H1", 60)
        if len(h1_bars) < 20:
            return
        atr = self._calc_atr(h1_bars, 14)
        if atr <= 0:
            return
        profit_in_price = (current_price - entry) if is_buy else (entry - current_price)
        original_risk = abs(entry - pos.stop_loss) if pos.stop_loss > 0 else atr * 1.5
        self._remember_snapshot(pos, plan, current_price=current_price, atr=atr, plan_record=plan_record)

        if not tick:
            return

        asset_class = get_asset_class(symbol)
        params = get_trade_params(asset_class)
        trail_trigger_mult = 1.5 if asset_class != "forex" else 1.0
        trail_distance_mult = 1.2 if asset_class != "forex" else 0.75
        break_even_r_multiple = float(plan_meta.get("break_even_r_multiple", 1.25))
        trailing_activation_r = float(plan_meta.get("trailing_activation_r_multiple", trail_trigger_mult))
        trail_distance_mult = float(plan_meta.get("trail_atr_multiplier", trail_distance_mult))

        if allow_non_target_close and age_seconds >= max(0, min_manage_age_seconds):
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

        if allow_non_target_close and age_seconds >= max(0, min_manage_age_seconds) and self._should_end_session_close(plan, plan_meta):
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

        active_profit_usd = float(getattr(pos, "profit", 0.0) or 0.0)
        target_management_ready = age_seconds >= max(0, min_manage_age_seconds) and active_profit_usd > 0.0

        if target_management_ready and profit_in_price > max(atr * trail_trigger_mult, original_risk * trailing_activation_r):
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

        tp_distance = abs(float(getattr(pos, "take_profit", 0.0) or 0.0) - entry)
        tp_progress = (profit_in_price / tp_distance) if tp_distance > 0 else 0.0
        breakeven_ready = (
            target_management_ready
            and active_profit_usd >= BREAKEVEN_MIN_PROFIT_USD
            and tp_progress >= BREAKEVEN_MIN_TP_PROGRESS
            and profit_in_price >= original_risk * break_even_r_multiple
        )
        if breakeven_ready and pos.stop_loss > 0:
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

        if allow_non_target_close and age_seconds >= max(0, min_manage_age_seconds) and pos.profit < 0:
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

        if (
            allow_non_target_close
            and age_seconds >= max(0, min_manage_age_seconds)
            and len(trend_bars) >= 52
            and float(getattr(pos, "profit", 0.0) or 0.0) <= 0.0
        ):
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
        comment = str(pos.comment or "").strip()
        # Manage all regular open positions by default so SL/TP protections
        # and money guards are consistently enforced (including manual trades).
        # Allow explicit opt-out for diagnostics/recovery.
        if comment.upper().startswith("NO_PM"):
            return False
        return True

    def _remember_snapshot(
        self,
        pos: PositionInfo,
        plan: dict,
        *,
        current_price: float | None = None,
        atr: float | None = None,
        plan_record: dict | None = None,
    ):
        plan_meta = plan.get("metadata", {}) if isinstance(plan, dict) else {}
        info = self.market_data.get_symbol_info(pos.symbol) or {}
        now_ts = time.time()
        existing_snapshot = self._position_snapshots.get(pos.ticket) or {}
        opened_at = self._resolve_position_opened_at(
            pos=pos,
            existing_snapshot=existing_snapshot,
            plan_record=plan_record,
            now_ts=now_ts,
        )
        self._position_snapshots[pos.ticket] = {
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "action": pos.type,
            "price_open": pos.price_open,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "profit": pos.profit,
            "opened_at": opened_at,
            "last_seen": now_ts,
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
            "tp_ladder_enabled": existing_snapshot.get("tp_ladder_enabled"),
            "tp_ladder_step_pct": existing_snapshot.get("tp_ladder_step_pct"),
            "tp_ladder_initialized": bool(existing_snapshot.get("tp_ladder_initialized", False)),
            "tp_ladder_original_target": existing_snapshot.get("tp_ladder_original_target"),
            "tp_ladder_next_target": existing_snapshot.get("tp_ladder_next_target"),
            "tp_ladder_exit_floor": existing_snapshot.get("tp_ladder_exit_floor"),
            "tp_ladder_last_extension_at": existing_snapshot.get("tp_ladder_last_extension_at", 0.0),
            "profit_pullback_peak_price": existing_snapshot.get("profit_pullback_peak_price"),
            "profit_pullback_peak_profit": existing_snapshot.get("profit_pullback_peak_profit"),
            "profit_pullback_activated": bool(existing_snapshot.get("profit_pullback_activated", False)),
        }

    def _update_snapshot_stop_loss(self, ticket: int, stop_loss: float):
        snapshot = self._position_snapshots.get(ticket)
        if snapshot:
            snapshot["stop_loss"] = stop_loss
            snapshot["last_seen"] = time.time()

    def _update_snapshot_take_profit(self, ticket: int, take_profit: float):
        snapshot = self._position_snapshots.get(ticket)
        if snapshot:
            snapshot["take_profit"] = take_profit
            snapshot["last_seen"] = time.time()

    def _tp_ladder_settings(self, plan: dict, plan_meta: dict) -> tuple[bool, float]:
        disabled_raw = plan.get("tp_ladder_disabled")
        if disabled_raw is None:
            disabled_raw = plan_meta.get("tp_ladder_disabled", False)
        enabled = not bool(disabled_raw)
        try:
            step_pct = float(plan.get("tp_ladder_step_pct") or plan_meta.get("tp_ladder_step_pct") or 0.02)
        except (TypeError, ValueError):
            step_pct = 0.02
        return enabled, max(0.0025, min(0.25, step_pct))

    def _tp_ladder_target_price(self, action: str, reference_price: float, step_pct: float, digits: int) -> float:
        if reference_price <= 0:
            return 0.0
        if str(action or "").upper() == "BUY":
            return round(reference_price * (1.0 + step_pct), digits)
        return round(reference_price * max(0.0001, 1.0 - step_pct), digits)

    async def _maybe_manage_tp_ladder(
        self,
        *,
        pos: PositionInfo,
        plan_record: dict,
        plan: dict,
        plan_meta: dict,
        current_price: float,
    ) -> bool:
        if current_price <= 0:
            return False
        # Never run ladder logic in the first minutes of a trade.
        if self._position_age_seconds(pos) < 180.0:
            return False

        if float(getattr(pos, "profit", 0.0) or 0.0) <= 0.0:
            return False

        enabled, step_pct = self._tp_ladder_settings(plan, plan_meta)
        if not enabled:
            return False

        snapshot = self._position_snapshots.get(pos.ticket) or {}
        active_target = float(snapshot.get("tp_ladder_next_target") or pos.take_profit or 0.0)
        if active_target <= 0:
            return False

        info = self.market_data.get_symbol_info(pos.symbol) or {}
        try:
            digits = int(info.get("digits", 5) or 5)
        except (TypeError, ValueError):
            digits = 5

        now_ts = time.time()
        if not bool(snapshot.get("tp_ladder_initialized", False)):
            elevated_tp = self._tp_ladder_target_price(pos.type, active_target, step_pct, digits)
            if elevated_tp > 0 and abs(elevated_tp - float(pos.take_profit or 0.0)) > max(10 ** (-digits), 0.00001):
                result = self.execution.modify_position(pos.ticket, pos.stop_loss, elevated_tp)
                if not result.success:
                    logger.warning(
                        "Position Manager: failed to arm TP ladder for %s (%s): %s",
                        pos.ticket,
                        pos.symbol,
                        result.retcode_desc,
                    )
                    return False
                self._update_snapshot_take_profit(pos.ticket, elevated_tp)
            snapshot = self._position_snapshots.setdefault(pos.ticket, {})
            snapshot["tp_ladder_enabled"] = True
            snapshot["tp_ladder_step_pct"] = step_pct
            snapshot["tp_ladder_initialized"] = True
            snapshot["tp_ladder_original_target"] = active_target
            snapshot["tp_ladder_next_target"] = active_target
            snapshot["tp_ladder_exit_floor"] = None
            snapshot["tp_ladder_last_extension_at"] = now_ts
            await self._log_activity(
                "tp_ladder_armed",
                pos.symbol,
                pos.ticket,
                f"TP ladder armed from {active_target:.5g}; broker TP moved {step_pct*100:.1f}% farther to let the trade run.",
                pos.profit,
            )
            return True

        last_extension_at = float(snapshot.get("tp_ladder_last_extension_at") or 0.0)
        if now_ts - last_extension_at < 2.0:
            return False

        is_buy = pos.type == "BUY"
        exit_floor = float(snapshot.get("tp_ladder_exit_floor") or 0.0)
        target_hit = current_price >= active_target if is_buy else current_price <= active_target
        floor_broken = exit_floor > 0 and (current_price <= exit_floor if is_buy else current_price >= exit_floor)

        if target_hit:
            new_target = self._tp_ladder_target_price(pos.type, current_price, step_pct, digits)
            if new_target <= 0:
                return False
            protected_stop = pos.stop_loss
            if is_buy:
                protected_stop = max(float(pos.stop_loss or 0.0), active_target)
            else:
                current_stop = float(pos.stop_loss or 0.0)
                protected_stop = active_target if current_stop <= 0 else min(current_stop, active_target)
            result = self.execution.modify_position(pos.ticket, protected_stop, new_target)
            if not result.success:
                logger.warning(
                    "Position Manager: failed to extend TP ladder for %s (%s): %s",
                    pos.ticket,
                    pos.symbol,
                    result.retcode_desc,
                )
                return False
            snapshot = self._position_snapshots.setdefault(pos.ticket, {})
            snapshot["tp_ladder_enabled"] = True
            snapshot["tp_ladder_step_pct"] = step_pct
            snapshot["tp_ladder_initialized"] = True
            snapshot["tp_ladder_exit_floor"] = active_target
            snapshot["tp_ladder_next_target"] = new_target
            snapshot["tp_ladder_last_extension_at"] = now_ts
            self._update_snapshot_stop_loss(pos.ticket, protected_stop)
            self._update_snapshot_take_profit(pos.ticket, new_target)
            await self._log_activity(
                "tp_ladder_extend",
                pos.symbol,
                pos.ticket,
                (
                    f"Price reached {active_target:.5g}; broker SL moved to {protected_stop:.5g} "
                    f"and TP extended to {new_target:.5g}."
                ),
                pos.profit,
            )
            return True

        if floor_broken:
            return False

        return False

    def _profit_pullback_settings(self, plan: dict, plan_meta: dict) -> tuple[bool, float, float, float]:
        disabled_raw = plan.get("profit_pullback_disabled")
        if disabled_raw is None:
            disabled_raw = plan_meta.get("profit_pullback_disabled", False)
        enabled_raw = plan.get("profit_pullback_enabled")
        if enabled_raw is None:
            enabled_raw = plan_meta.get("profit_pullback_enabled", True)
        enabled = bool(enabled_raw) and not bool(disabled_raw)

        def _float_setting(key: str, default: float) -> float:
            try:
                return float(plan.get(key) or plan_meta.get(key) or default)
            except (TypeError, ValueError):
                return default

        activation_progress = _float_setting(
            "profit_pullback_activation_progress",
            PROFIT_PULLBACK_MIN_TP_PROGRESS,
        )
        giveback_pct = _float_setting(
            "profit_pullback_giveback_pct",
            PROFIT_PULLBACK_DEFAULT_GIVEBACK_PCT,
        )
        min_peak_profit = _float_setting(
            "profit_pullback_min_peak_profit_usd",
            PROFIT_PULLBACK_MIN_PEAK_PROFIT_USD,
        )
        return (
            enabled,
            max(0.10, min(0.90, activation_progress)),
            max(0.10, min(0.80, giveback_pct)),
            max(0.0, min_peak_profit),
        )

    async def _maybe_close_profit_pullback(
        self,
        *,
        pos: PositionInfo,
        plan_record: dict,
        plan: dict,
        plan_meta: dict,
        current_price: float,
        profit_in_price: float,
    ) -> bool:
        if current_price <= 0 or profit_in_price <= 0:
            return False
        if float(getattr(pos, "profit", 0.0) or 0.0) <= 0.0:
            return False
        if self._position_age_seconds(pos, plan_record=plan_record) < 180.0:
            return False

        enabled, activation_progress, giveback_pct, min_peak_profit = self._profit_pullback_settings(plan, plan_meta)
        if not enabled:
            return False

        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        if entry <= 0 or take_profit <= 0:
            return False

        tp_distance = abs(take_profit - entry)
        if tp_distance <= 0:
            return False
        current_progress = profit_in_price / tp_distance

        snapshot = self._position_snapshots.setdefault(pos.ticket, {})
        is_buy = str(getattr(pos, "type", "") or "").upper() == "BUY"
        peak_price = float(snapshot.get("profit_pullback_peak_price") or 0.0)
        peak_profit = float(snapshot.get("profit_pullback_peak_profit") or 0.0)
        active_profit = float(getattr(pos, "profit", 0.0) or 0.0)

        if peak_price <= 0:
            peak_price = current_price
        if peak_profit <= 0:
            peak_profit = active_profit

        improved_peak = current_price > peak_price if is_buy else current_price < peak_price
        if improved_peak:
            peak_price = current_price
        if active_profit > peak_profit:
            peak_profit = active_profit

        peak_move = (peak_price - entry) if is_buy else (entry - peak_price)
        peak_progress = peak_move / tp_distance if tp_distance > 0 else 0.0
        activated = bool(snapshot.get("profit_pullback_activated", False))
        if peak_progress >= activation_progress and peak_profit >= min_peak_profit:
            activated = True

        snapshot["profit_pullback_peak_price"] = peak_price
        snapshot["profit_pullback_peak_profit"] = peak_profit
        snapshot["profit_pullback_activated"] = activated
        snapshot["last_seen"] = time.time()

        if not activated:
            return False

        giveback_in_price = max(0.0, peak_move - profit_in_price)
        if peak_move <= 0:
            return False
        giveback_ratio = giveback_in_price / peak_move
        if giveback_ratio < giveback_pct:
            return False

        await self._close_with_outcome(
            pos,
            "profit_pullback",
            (
                f"Closed profitable trade after giving back {giveback_ratio*100:.0f}% of peak progress "
                f"before returning to entry. Peak price {peak_price:.5g}, close reference {current_price:.5g}, "
                f"peak P&L ${peak_profit:.2f}, current P&L ${active_profit:.2f}."
            ),
            activity_action="profit_pullback",
            plan_record=plan_record,
            plan=plan,
        )
        return True

    def _resolve_position_opened_at(
        self,
        *,
        pos: PositionInfo,
        existing_snapshot: dict | None = None,
        plan_record: dict | None = None,
        now_ts: float | None = None,
    ) -> float:
        now_ts = float(now_ts or time.time())
        snapshot = existing_snapshot or {}
        raw_opened_at = float(getattr(pos, "time", 0) or 0.0)

        # MT5 can occasionally return a future-looking epoch after reconnects.
        # Prefer persisted plan timestamps when that happens so age-based exits
        # continue to work across backend restarts.
        if 0 < raw_opened_at <= (now_ts + 300):
            return raw_opened_at

        if isinstance(plan_record, dict):
            try:
                planned_opened_at = float(plan_record.get("created_at") or 0.0)
            except (TypeError, ValueError):
                planned_opened_at = 0.0
            if 0 < planned_opened_at <= (now_ts + 300):
                return planned_opened_at

        snapshot_opened = float(snapshot.get("opened_at") or 0.0)
        if 0 < snapshot_opened <= (now_ts + 300):
            return snapshot_opened

        snapshot_seen = float(snapshot.get("last_seen") or 0.0)
        if 0 < snapshot_seen <= (now_ts + 300):
            return snapshot_seen

        return now_ts

    def _position_age_seconds(
        self,
        pos: PositionInfo,
        plan_record: dict | None = None,
        existing_snapshot: dict | None = None,
    ) -> float:
        now_ts = time.time()
        snapshot = existing_snapshot if existing_snapshot is not None else (self._position_snapshots.get(pos.ticket) or {})
        opened_at = self._resolve_position_opened_at(
            pos=pos,
            existing_snapshot=snapshot,
            plan_record=plan_record,
            now_ts=now_ts,
        )
        return max(0.0, now_ts - opened_at)

    def _position_age_minutes(
        self,
        pos: PositionInfo,
        plan_record: dict | None = None,
        existing_snapshot: dict | None = None,
    ) -> float:
        return self._position_age_seconds(
            pos,
            plan_record=plan_record,
            existing_snapshot=existing_snapshot,
        ) / 60.0

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
        force_time_exit_seconds = int(
            plan.get("force_time_exit_seconds")
            or plan_meta.get("force_time_exit_seconds")
            or 0
        )
        if 0 < force_time_exit_seconds < MIN_FORCE_TIME_EXIT_SECONDS:
            force_time_exit_seconds = 0
        if force_time_exit_seconds > 0:
            age_seconds = self._position_age_seconds(pos)
            if age_seconds >= force_time_exit_seconds:
                return (
                    "time_stop",
                    f"Closed by strict strategy timer after {age_seconds:.0f}s (target {force_time_exit_seconds}s).",
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
            if float(getattr(pos, "profit", 0.0) or 0.0) > 0 and progress_r >= 0.05:
                return None
            return (
                "time_stop",
                (
                    f"Closed under high event risk after {age_minutes:.0f}min - "
                    f"progress stalled at {progress_r:.2f}R before the planned {planned_hold}min hold."
                ),
            )

        if age_minutes >= stale_after and progress_r < min_progress:
            if float(getattr(pos, "profit", 0.0) or 0.0) > 0 and progress_r >= 0.05:
                return None
            return (
                "time_stop",
                (
                    f"Closed stale trade after {age_minutes:.0f}min - "
                    f"progress only reached {progress_r:.2f}R (need {min_progress:.2f}R by {stale_after}min)."
                ),
            )

        if age_minutes >= planned_hold:
            if progress_r < target_progress:
                if float(getattr(pos, "profit", 0.0) or 0.0) > 0 and progress_r >= 0.05:
                    return None
                return (
                    "time_stop",
                    (
                        f"Closed by time stop after {age_minutes:.0f}min - "
                        f"trade only reached {progress_r:.2f}R vs required {target_progress:.2f}R."
                    ),
                )
            return None

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
            logger.info(
                "Position Manager: reconciling closed plan without outcome for ticket %s",
                ticket,
            )

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
                "stop_loss": snapshot.get("stop_loss"),
                "take_profit": snapshot.get("take_profit"),
            },
        )
        await self.db.mark_evaluation_outcome(signal_id, "closed")
        if self._on_trade_closed is not None:
            await self._on_trade_closed(
                {
                    "ticket": ticket,
                    "symbol": snapshot.get("symbol", ""),
                    "exit_reason": exit_reason,
                    "profit": float(snapshot.get("profit", 0.0)),
                    "signal_id": signal_id,
                }
            )

    def _classify_passive_exit(self, snapshot: dict) -> str:
        def _safe_float(value, default: float = 0.0) -> float:
            try:
                return float(value if value is not None else default)
            except Exception:
                return float(default)

        entry = _safe_float(snapshot.get("price_open", 0.0))
        stop_loss = _safe_float(snapshot.get("stop_loss", 0.0))
        take_profit = _safe_float(snapshot.get("take_profit", 0.0))
        tp_ladder_floor = _safe_float(snapshot.get("tp_ladder_exit_floor", 0.0))
        last_price = _safe_float(snapshot.get("last_price", 0.0))
        point = max(_safe_float(snapshot.get("point", 0.0001), 0.0001), 0.00001)
        tolerance = max(point * 5, abs(entry) * 0.0005)
        profit = _safe_float(snapshot.get("profit", 0.0))
        action = snapshot.get("action", "BUY")

        if take_profit > 0 and profit > 0 and abs(last_price - take_profit) <= tolerance:
            return "take_profit"

        if tp_ladder_floor > 0 and abs(last_price - tp_ladder_floor) <= tolerance and profit > 0:
            return "tp_ladder_pullback"

        if stop_loss > 0 and profit <= 0 and abs(last_price - stop_loss) <= tolerance:
            if profit >= -1.0 and self._is_breakeven_stop(action, entry, stop_loss, tolerance):
                return "breakeven"
            if self._is_trailing_stop(action, entry, stop_loss, tolerance):
                return "trailing_stop"
            return "stop_loss"

        if take_profit > 0 and profit > 0 and last_price >= take_profit - tolerance and action == "BUY":
            return "take_profit"
        if take_profit > 0 and profit > 0 and last_price <= take_profit + tolerance and action == "SELL":
            return "take_profit"
        if tp_ladder_floor > 0 and profit > 0:
            if action == "BUY" and last_price <= tp_ladder_floor + tolerance:
                return "tp_ladder_pullback"
            if action == "SELL" and last_price >= tp_ladder_floor - tolerance:
                return "tp_ladder_pullback"
        if self._is_trailing_stop(action, entry, stop_loss, tolerance) and profit > 0:
            return "trailing_stop"
        if self._is_breakeven_stop(action, entry, stop_loss, tolerance) and profit >= 0:
            return "breakeven"
        return "external_profit_close" if profit > 0 else "external_loss_close"

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
            execution_trace = meta.get("execution_trace") if isinstance(meta, dict) else None
            if isinstance(execution_trace, dict):
                signal_snapshot = execution_trace.get("signal_snapshot")
                if isinstance(signal_snapshot, dict):
                    try:
                        confidence = float(signal_snapshot.get("confidence") or confidence)
                    except (TypeError, ValueError):
                        confidence = confidence
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
                await self.db.log_ai_activity(
                    action,
                    symbol,
                    ticket,
                    detail,
                    profit,
                    decision_reason=detail,
                )
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
        result = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            result = await asyncio.to_thread(self.execution.close_position, pos.ticket)
            if result.success:
                break
            logger.warning(
                "Position Manager: close attempt %s/%s failed for %s #%s (%s): %s",
                attempt,
                attempts,
                pos.symbol,
                pos.ticket,
                exit_reason,
                result.retcode_desc,
            )
            if attempt < attempts:
                await asyncio.sleep(0.25)

        if result is None or not result.success:
            return

        await self._record_close_result(
            pos,
            result,
            exit_reason,
            detail,
            activity_action=activity_action,
            plan_record=plan_record,
            plan=plan,
        )

    async def _close_positions_for_portfolio_target(
        self,
        positions: list[PositionInfo],
        exit_reason: str,
        detail: str,
    ):
        if not positions:
            return
        self._portfolio_close_inflight = True
        try:
            close_tasks = [
                asyncio.to_thread(self.execution.close_position, pos.ticket)
                for pos in positions
            ]
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for pos, outcome in zip(positions, results):
                if isinstance(outcome, Exception):
                    logger.error(
                        "Portfolio target close failed for %s #%s: %s",
                        pos.symbol,
                        pos.ticket,
                        outcome,
                    )
                    continue
                if not outcome.success:
                    logger.warning(
                        "Portfolio target close rejected for %s #%s: %s",
                        pos.symbol,
                        pos.ticket,
                        outcome.retcode_desc,
                    )
                    continue
                await self._record_close_result(
                    pos,
                    outcome,
                    exit_reason,
                    detail,
                    activity_action=exit_reason,
                )
        finally:
            self._portfolio_close_inflight = False

    async def _record_close_result(
        self,
        pos: PositionInfo,
        result,
        exit_reason: str,
        detail: str,
        *,
        activity_action: str | None = None,
        plan_record: dict | None = None,
        plan: dict | None = None,
    ):

        existing_snapshot = self._position_snapshots.get(pos.ticket) or {}
        await self._log_activity(activity_action or exit_reason, pos.symbol, pos.ticket, detail, pos.profit)
        if self.db:
            try:
                if plan_record is None:
                    plan_record = await self.db.get_position_management_plan(pos.ticket)
                if plan is None and plan_record:
                    plan = plan_record.get("plan_json", {})

                signal_id, confidence, category, strategy, planned_hold_minutes = self._plan_context(plan_record, plan)
                if signal_id:
                    signal = await self.db.get_signal_by_id(signal_id)
                    if signal:
                        confidence = float(signal.get("confidence", 0.0))
                holding_minutes = self._position_age_minutes(
                    pos,
                    plan_record=plan_record,
                    existing_snapshot=existing_snapshot,
                )

                await self.db.log_trade_outcome(
                    ticket=pos.ticket,
                    signal_id=signal_id,
                    symbol=pos.symbol,
                    action=pos.type,
                    confidence=confidence,
                    profit=pos.profit,
                    exit_reason=exit_reason,
                    holding_minutes=holding_minutes,
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
            except Exception as exc:
                logger.warning(
                    "Position Manager: close bookkeeping failed for %s #%s (%s): %s",
                    pos.symbol,
                    pos.ticket,
                    exit_reason,
                    exc,
                )
        self._position_snapshots.pop(pos.ticket, None)
        self._avg_down_tracker.pop(pos.ticket, None)

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

    def _has_valid_broker_targets(self, pos: PositionInfo) -> bool:
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
            return False
        side = str(getattr(pos, "type", "") or "").upper()
        if side == "BUY":
            return stop_loss < entry < take_profit
        if side == "SELL":
            return take_profit < entry < stop_loss
        return False

    def _broker_stop_loss_usd(self, pos: PositionInfo) -> float:
        if not self._has_valid_broker_targets(pos):
            return 0.0
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        volume = float(getattr(pos, "volume", 0.0) or 0.0)
        if entry <= 0 or stop_loss <= 0 or volume <= 0:
            return 0.0
        return abs(entry - stop_loss) * volume * self._get_contract_size(pos.symbol)

    def _usd_per_price_distance(self, symbol: str, volume: float) -> float:
        if volume <= 0:
            return 0.0
        info = self.market_data.get_symbol_info(symbol) or {}
        tick_value = float(info.get("trade_tick_value", 0.0) or 0.0)
        tick_size = float(info.get("trade_tick_size", 0.0) or 0.0)
        point = float(info.get("point", 0.0) or 0.0)
        contract_size = float(info.get("trade_contract_size", 0.0) or 0.0)
        contract_based = (volume * contract_size) if contract_size > 0 else 0.0
        if tick_value > 0:
            unit = tick_size if tick_size > 0 else point
            if unit > 0:
                tick_based = volume * (tick_value / unit)
                if contract_based > 0 and tick_based > 0:
                    # Keep money-distance scaling stable across brokers by
                    # preferring contract-size math when both are present.
                    return contract_based
                return tick_based
        if contract_based > 0:
            return contract_based
        return 0.0

    def _extract_comment_named_amount(self, comment: str | None, key: str) -> float | None:
        text = str(comment or "").strip()
        if not text:
            return None
        m = re.search(rf"(?:^|[\|_\-\s]){re.escape(key)}(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _resolve_comment_risk_amounts(self, pos: PositionInfo) -> dict[str, float | None]:
        comment = getattr(pos, "comment", "")
        ta = self._extract_comment_named_amount(comment, "TA")
        sla = self._extract_comment_named_amount(comment, "SLA")
        tpa = self._extract_comment_named_amount(comment, "TPA")
        comment_ta = ta is not None
        comment_sla = sla is not None
        comment_tpa = tpa is not None
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        volume = float(getattr(pos, "volume", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        usd_per_price = self._usd_per_price_distance(pos.symbol, volume) if entry > 0 and volume > 0 else 0.0
        implied_sla = None
        implied_tpa = None
        if usd_per_price > 0:
            if stop_loss > 0:
                implied_sla = abs(entry - stop_loss) * usd_per_price
            if take_profit > 0:
                implied_tpa = abs(take_profit - entry) * usd_per_price
            if sla is None and implied_sla is not None:
                sla = implied_sla
            if tpa is None and implied_tpa is not None:
                tpa = implied_tpa
            # Keep comment SLA/TPA authoritative when present: these represent
            # intended started-amount risk. Do not override them with implied
            # notional distances (which can be much larger on leveraged symbols).
        # Normalize legacy comments: if TA (started amount) is present, cap SLA/TPA
        # by started-amount risk implied by the current SL/TP price percentages.
        if ta is None:
            for field_name in ("started_with_usd", "entry_market_value_usd", "estimated_margin"):
                try:
                    candidate = float(getattr(pos, field_name, 0.0) or 0.0)
                except Exception:
                    candidate = 0.0
                if candidate > 0:
                    ta = candidate
                    break
        if ta and ta > 0 and entry > 0:
            sl_min_pct, sl_max_pct, tp_min_pct, tp_max_pct = self._target_pct_caps_for_symbol(pos.symbol)
            min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
            started_sl_cap, started_tp_cap = self._started_money_caps_usd(float(ta))
            # When comments do not carry explicit SLA/TPA, anchor to sane started-amount
            # targets by symbol instead of reverse-deriving from a potentially broken broker
            # SL/TP distance.
            target_started_sla = max(25.0, float(ta) * max(sl_min_pct, sl_max_pct))
            target_started_tpa = max(target_started_sla * min_rr, float(ta) * max(tp_min_pct, tp_max_pct))
            if sla is None:
                sla = target_started_sla
            elif not (comment_ta or comment_sla or comment_tpa):
                sla = min(float(sla), target_started_sla * 1.10)
            if tpa is None:
                tpa = target_started_tpa
            elif not (comment_ta or comment_sla or comment_tpa):
                tpa = min(float(tpa), target_started_tpa * 1.10)
            # Explicit SLA/TPA values are authoritative only while they remain
            # within the started-amount risk envelope. Legacy CFD comments can
            # contain impossible values such as TA453/SLA820, which would keep a
            # visible broker SL far beyond the amount the user actually started.
            if started_sl_cap > 0 and sla is not None and float(sla) > started_sl_cap:
                sla = started_sl_cap
            if started_tp_cap > 0 and tpa is not None and float(tpa) > started_tp_cap:
                tpa = max(started_tp_cap, float(sla or 0.0) * min_rr)
        if sla is None:
            try:
                account = self.connector.refresh_account()
                equity = float(getattr(account, "equity", 0.0) or 0.0)
            except Exception:
                equity = 0.0
            max_risk_pct = float(getattr(self.risk_engine.user_policy, "max_risk_per_trade", 0.0) or 0.0)
            if equity > 0 and max_risk_pct > 0:
                sla = max(25.0, equity * (max_risk_pct / 100.0))
        return {"sla": sla, "tpa": tpa}

    def _started_money_caps_usd(self, started_amount_usd: float) -> tuple[float, float]:
        started = max(0.0, float(started_amount_usd or 0.0))
        if started <= 0:
            return 0.0, 0.0
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced") or "balanced").lower()
        sl_pct, tp_pct = {
            "safe": (0.08, 0.18),
            "balanced": (0.12, 0.28),
            "aggressive": (0.18, 0.40),
        }.get(mode, (0.12, 0.28))
        return started * sl_pct, started * tp_pct

    async def _maybe_realign_targets_from_comment(self, pos: PositionInfo, tick) -> bool:
        # Repair legacy/mis-scaled SL/TP placements using the intended SLA/TPA stored in comment.
        # This only fires when broker targets are clearly off from intended started-amount risk,
        # so breakeven/trailing logic remains in control once protection has tightened.
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        volume = float(getattr(pos, "volume", 0.0) or 0.0)
        if entry <= 0 or volume <= 0:
            return False
        usd_per_price = self._usd_per_price_distance(pos.symbol, volume)
        if usd_per_price <= 0:
            return False

        risk_amounts = self._resolve_comment_risk_amounts(pos)
        sla = risk_amounts.get("sla")
        tpa = risk_amounts.get("tpa")
        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
        if sla is not None and tpa is not None and tpa < (sla * min_rr):
            tpa = float(sla) * min_rr
        if sla is None and tpa is None:
            return False

        is_buy = str(getattr(pos, "type", "")).upper() == "BUY"
        point = max(float(getattr(tick, "point", 0.0) or 0.0), 0.00001)
        tolerance = max(point * 5.0, abs(entry) * 0.00005)

        cur_sl = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        cur_tp = float(getattr(pos, "take_profit", 0.0) or 0.0)
        new_sl = cur_sl
        new_tp = cur_tp
        changed = False
        # Never downgrade a protected stop back to a wider risk stop from comment.
        if cur_sl > 0:
            if is_buy and cur_sl >= (entry - tolerance):
                sla = None
            elif (not is_buy) and cur_sl <= (entry + tolerance):
                sla = None

        # Never rewrite SL from comment-derived SLA during live management.
        # SL is actively managed by breakeven/trailing logic and should not
        # be pulled back to an older, wider risk anchor.

        if sla is not None and cur_sl > 0:
            cur_sl_usd = abs(cur_sl - entry) * usd_per_price
            sl_ratio = (cur_sl_usd / sla) if sla > 0 else 1.0
            if sl_ratio < 0.60 or sl_ratio > 1.40:
                sl_distance = sla / usd_per_price
                candidate = (entry - sl_distance) if is_buy else (entry + sl_distance)
                if candidate > 0 and abs(candidate - cur_sl) > tolerance:
                    new_sl = candidate
                    changed = True

        if tpa is not None and cur_tp > 0:
            cur_tp_usd = abs(cur_tp - entry) * usd_per_price
            tp_ratio = (cur_tp_usd / tpa) if tpa > 0 else 1.0
            if tp_ratio < 0.60 or tp_ratio > 1.40:
                tp_distance = tpa / usd_per_price
                candidate = (entry + tp_distance) if is_buy else (entry - tp_distance)
                if candidate > 0 and abs(candidate - cur_tp) > tolerance:
                    new_tp = candidate
                    changed = True

        if not changed:
            return False

        if is_buy and new_sl > 0 and new_tp > 0 and not (new_sl < entry < new_tp):
            return False
        if (not is_buy) and new_sl > 0 and new_tp > 0 and not (new_tp < entry < new_sl):
            return False

        result = self.execution.modify_position(pos.ticket, new_sl, new_tp)
        if not result.success:
            # If comment-derived started-amount targets are already too close to
            # the live market, MT5 can reject them. Fall back to the active
            # position risk model, which uses account risk budget, ATR, symbol
            # caps, and broker-side distance constraints instead of leaving a
            # visibly over-wide SL/TP in place.
            fallback = self._calculate_active_position_targets(pos, plan={}, plan_meta={})
            if fallback is None:
                return False
            fallback_sl, fallback_tp, fallback_detail = fallback
            fallback_result = self.execution.modify_position(pos.ticket, fallback_sl, fallback_tp)
            if not fallback_result.success:
                return False
            self._update_snapshot_stop_loss(pos.ticket, fallback_sl)
            self._update_snapshot_take_profit(pos.ticket, fallback_tp)
            await self._log_activity(
                "targets_realign",
                pos.symbol,
                pos.ticket,
                (
                    "Comment-derived target repair was too close to the live market; "
                    f"{fallback_detail}"
                ),
                pos.profit,
            )
            return True
        if new_sl > 0:
            self._update_snapshot_stop_loss(pos.ticket, new_sl)
        if new_tp > 0:
            self._update_snapshot_take_profit(pos.ticket, new_tp)
        await self._log_activity(
            "targets_realign",
            pos.symbol,
            pos.ticket,
            "Realigned SL/TP to match intended SLA/TPA risk amounts from execution comment.",
            pos.profit,
        )
        return True

    async def _maybe_enforce_min_rr_targets(self, pos: PositionInfo) -> bool:
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
            return False
        is_buy = str(getattr(pos, "type", "")).upper() == "BUY"
        if is_buy and not (stop_loss < entry < take_profit):
            return False
        if (not is_buy) and not (take_profit < entry < stop_loss):
            return False
        sl_dist = abs(entry - stop_loss)
        tp_dist = abs(take_profit - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            return False
        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
        if tp_dist >= (sl_dist * min_rr):
            return False
        target_tp_dist = sl_dist * min_rr
        new_tp = (entry + target_tp_dist) if is_buy else (entry - target_tp_dist)
        if new_tp <= 0:
            return False
        info = self.market_data.get_symbol_info(pos.symbol) or {}
        try:
            digits = int(info.get("digits", 5) or 5)
        except Exception:
            digits = 5
        new_tp = round(new_tp, digits)
        result = self.execution.modify_position(pos.ticket, stop_loss, new_tp)
        if not result.success:
            return False
        self._update_snapshot_take_profit(pos.ticket, new_tp)
        await self._log_activity(
            "targets_rr_fix",
            pos.symbol,
            pos.ticket,
            f"Adjusted TP to enforce minimum reward:risk {min_rr:.2f}:1.",
            pos.profit,
        )
        return True

    async def _maybe_fix_invalid_targets(
        self,
        pos: PositionInfo,
        *,
        plan: dict | None = None,
        plan_meta: dict | None = None,
    ) -> bool:
        """Repair missing/inverted targets so runtime guards can enforce exits reliably."""
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        if entry <= 0:
            return False
        is_buy = str(getattr(pos, "type", "")).upper() == "BUY"

        valid_orientation = (
            (is_buy and stop_loss > 0 and take_profit > 0 and stop_loss < entry < take_profit)
            or ((not is_buy) and stop_loss > 0 and take_profit > 0 and take_profit < entry < stop_loss)
        )
        if valid_orientation:
            return False
        protected_orientation = (
            (is_buy and stop_loss >= entry and take_profit > stop_loss)
            or ((not is_buy) and 0 < stop_loss <= entry and 0 < take_profit < stop_loss)
        )
        if protected_orientation:
            return False

        targets = self._calculate_active_position_targets(pos, plan=plan or {}, plan_meta=plan_meta or {})
        if targets is None:
            return False
        new_sl, new_tp, detail = targets

        result = self.execution.modify_position(pos.ticket, new_sl, new_tp)
        if not result.success:
            return False
        self._update_snapshot_stop_loss(pos.ticket, new_sl)
        self._update_snapshot_take_profit(pos.ticket, new_tp)
        await self._log_activity(
            "targets_orientation_fix",
            pos.symbol,
            pos.ticket,
            detail,
            pos.profit,
        )
        return True

    def _calculate_active_position_targets(
        self,
        pos: PositionInfo,
        *,
        plan: dict,
        plan_meta: dict,
    ) -> tuple[float, float, str] | None:
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        volume = abs(float(getattr(pos, "volume", 0.0) or 0.0))
        if entry <= 0 or volume <= 0:
            return None
        action = str(getattr(pos, "type", "") or "").upper()
        if action not in {"BUY", "SELL"}:
            return None
        is_buy = action == "BUY"
        info = self.market_data.get_symbol_info(pos.symbol) or {}
        try:
            digits = int(info.get("digits", 5) or 5)
        except Exception:
            digits = 5
        try:
            point = float(info.get("point", 0.00001) or 0.00001)
        except Exception:
            point = 0.00001

        sl_min, sl_max, tp_min, tp_max = self._target_pct_caps_for_symbol(pos.symbol)
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced") or "balanced").lower()
        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
        mode_sl_multiplier = {"safe": 0.85, "balanced": 1.0, "aggressive": 1.25}.get(mode, 1.0)
        mode_tp_multiplier = {"safe": 1.0, "balanced": 1.08, "aggressive": 1.18}.get(mode, 1.08)

        atr_pct = 0.0
        try:
            bars = self.market_data.get_bars(pos.symbol, "H1", 60)
            atr = self._calc_atr(bars, 14) if bars else 0.0
            if atr > 0:
                atr_pct = atr / entry
        except Exception:
            atr_pct = 0.0

        base_sl_pct = max(sl_min, min(sl_max, max(atr_pct * mode_sl_multiplier, sl_min)))
        base_tp_pct = max(tp_min, min(tp_max, base_sl_pct * min_rr * mode_tp_multiplier))

        try:
            account = self.connector.refresh_account()
            equity = float(getattr(account, "equity", 0.0) or 0.0)
        except Exception:
            equity = 0.0
        risk_pct = max(0.05, float(getattr(self.risk_engine.user_policy, "max_risk_per_trade", 0.25) or 0.25))
        usd_per_price = self._usd_per_price_distance(pos.symbol, volume)
        if equity > 0 and usd_per_price > 0:
            risk_budget_usd = equity * risk_pct / 100.0
            try:
                open_positions = self.execution.get_positions() or []
            except Exception:
                open_positions = []
            total_positions = max(1, len(open_positions))
            same_symbol_positions = [
                row for row in open_positions
                if str(getattr(row, "symbol", "") or "").upper() == str(pos.symbol or "").upper()
            ]
            exposure_factor = max(0.45, 1.0 - min(0.35, (total_positions - 1) * 0.04))
            same_symbol_factor = max(0.55, 1.0 - min(0.30, max(0, len(same_symbol_positions) - 1) * 0.10))
            risk_budget_usd *= exposure_factor * same_symbol_factor
            risk_distance = risk_budget_usd / usd_per_price
            if risk_distance > 0:
                base_sl_pct = max(sl_min, min(base_sl_pct, risk_distance / entry, sl_max))
                base_tp_pct = max(base_tp_pct, base_sl_pct * min_rr)

        base_tp_pct = max(tp_min, min(base_tp_pct, tp_max))
        if base_tp_pct < base_sl_pct * min_rr:
            base_sl_pct = max(sl_min, min(base_sl_pct, base_tp_pct / min_rr))

        sl_distance = max(entry * base_sl_pct, point * 10.0)
        tp_distance = max(entry * base_tp_pct, sl_distance * min_rr, point * 10.0)
        if is_buy:
            stop_loss = round(entry - sl_distance, digits)
            take_profit = round(entry + tp_distance, digits)
            if not (stop_loss < entry < take_profit):
                return None
        else:
            stop_loss = round(entry + sl_distance, digits)
            take_profit = round(entry - tp_distance, digits)
            if not (take_profit < entry < stop_loss):
                return None

        detail = (
            f"Applied automatic SL/TP from active position context: mode={mode}, "
            f"risk={risk_pct:.2f}% equity, ATR={atr_pct*100:.2f}%, min R:R={min_rr:.2f}:1."
        )
        return stop_loss, take_profit, detail

    def _target_pct_caps_for_symbol(self, symbol: str) -> tuple[float, float, float, float]:
        asset = str(get_asset_class(symbol) or "other").lower()
        if asset in {"gold", "commodity", "commodities"}:
            return (0.0025, 0.015, 0.004, 0.030)
        if asset in {"indices"}:
            return (0.0025, 0.015, 0.004, 0.030)
        if asset in {"stocks"}:
            return (0.003, 0.020, 0.006, 0.040)
        if asset in {"forex"}:
            return (0.0015, 0.010, 0.003, 0.020)
        if asset in {"crypto"}:
            return (0.005, 0.035, 0.010, 0.070)
        return (0.003, 0.020, 0.006, 0.040)

    async def _maybe_cap_outlier_targets(self, pos: PositionInfo) -> bool:
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        stop_loss = float(getattr(pos, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(pos, "take_profit", 0.0) or 0.0)
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
            return False
        is_buy = str(getattr(pos, "type", "")).upper() == "BUY"
        if is_buy and not (stop_loss < entry < take_profit):
            return False
        if (not is_buy) and not (take_profit < entry < stop_loss):
            return False

        sl_pct = abs(entry - stop_loss) / entry
        tp_pct = abs(take_profit - entry) / entry
        sl_min, sl_max, tp_min, tp_max = self._target_pct_caps_for_symbol(pos.symbol)
        # Only repair clear outliers to avoid unnecessary churn.
        if sl_pct <= (sl_max * 1.2) and tp_pct <= (tp_max * 1.2):
            return False

        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.2) or 1.2))
        current_sl_dist = abs(entry - stop_loss)
        if current_sl_dist > 0:
            # Never widen a tighter live stop (e.g., breakeven/trailing).
            new_sl_dist = min(current_sl_dist, entry * sl_max)
        else:
            new_sl_dist = entry * sl_min
        new_tp_dist = max(entry * tp_min, new_sl_dist * min_rr)
        new_tp_dist = min(new_tp_dist, entry * tp_max)
        if new_tp_dist < (new_sl_dist * min_rr):
            new_sl_dist = max(entry * sl_min, min(new_sl_dist, new_tp_dist / min_rr))

        info = self.market_data.get_symbol_info(pos.symbol) or {}
        try:
            digits = int(info.get("digits", 5) or 5)
        except Exception:
            digits = 5

        if is_buy:
            new_sl = round(entry - new_sl_dist, digits)
            new_tp = round(entry + new_tp_dist, digits)
            if not (new_sl < entry < new_tp):
                return False
        else:
            new_sl = round(entry + new_sl_dist, digits)
            new_tp = round(entry - new_tp_dist, digits)
            if not (new_tp < entry < new_sl):
                return False

        result = self.execution.modify_position(pos.ticket, new_sl, new_tp)
        if not result.success:
            return False
        self._update_snapshot_stop_loss(pos.ticket, new_sl)
        self._update_snapshot_take_profit(pos.ticket, new_tp)
        await self._log_activity(
            "targets_cap_fix",
            pos.symbol,
            pos.ticket,
            (
                f"Capped oversized SL/TP to sane bounds "
                f"(SL {sl_pct*100:.1f}% -> {abs(entry-new_sl)/entry*100:.1f}%, "
                f"TP {tp_pct*100:.1f}% -> {abs(new_tp-entry)/entry*100:.1f}%)."
            ),
            pos.profit,
        )
        return True
