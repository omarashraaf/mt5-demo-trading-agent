from __future__ import annotations

import asyncio
import math

from domain.models import ExecutionPreflightAssessment, TechnicalSignal
from mt5.execution import OrderRequest, OrderResult
from services.broker_symbol_validator import BrokerSymbolValidator


class ExecutionService:
    """Executes MT5 orders only after deterministic approval and live re-checks."""

    def __init__(
        self,
        execution_engine,
        risk_service,
        signal_pipeline,
        db=None,
    ):
        self.execution_engine = execution_engine
        self.risk_service = risk_service
        self.signal_pipeline = signal_pipeline
        self.db = db
        self.kill_switch_state_getter = None
        self.broker_symbol_validator = BrokerSymbolValidator()

    async def preflight(
        self,
        *,
        symbol: str,
        action: str,
        volume: float,
        stop_loss: float | None,
        take_profit: float | None,
        reference_spread: float,
        requested_agent_name: str,
        requested_timeframe: str = "H1",
        evaluation_mode: str = "manual",
        scan_window_id: str | None = None,
    ) -> ExecutionPreflightAssessment:
        decision = await self.signal_pipeline.evaluate(
            symbol=symbol,
            requested_agent_name=requested_agent_name,
            requested_timeframe=requested_timeframe,
            evaluation_mode=evaluation_mode,
            bar_count=100,
            scan_window_id=scan_window_id,
        )
        context = decision.signal_decision.market_context
        candidate_signal = self._candidate_signal(
            base_signal=decision.signal_decision.final_signal,
            action=action,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return await self.preflight_for_context_signal(
            context=context,
            candidate_signal=candidate_signal,
            volume=volume,
            reference_spread=reference_spread,
            evaluation_mode=evaluation_mode,
            scan_window_id=scan_window_id,
        )

    async def preflight_for_context_signal(
        self,
        *,
        context,
        candidate_signal: TechnicalSignal,
        volume: float,
        reference_spread: float,
        evaluation_mode: str = "manual",
        scan_window_id: str | None = None,
    ) -> ExecutionPreflightAssessment:
        symbol = context.symbol
        action = str(candidate_signal.action or "").strip().upper()
        candidate_signal = candidate_signal.model_copy(update={"action": action})
        if action not in {"BUY", "SELL"}:
            return ExecutionPreflightAssessment(
                approved=False,
                reason=f"Invalid execution action: {action or 'NONE'}.",
            )
        volume = self._safe_float(volume)
        if not math.isfinite(volume) or volume <= 0.0:
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Execution blocked: order volume is invalid.",
            )
        stop_loss = self._safe_float(candidate_signal.stop_loss)
        take_profit = self._safe_float(candidate_signal.take_profit)
        profile = context.profile
        tick = context.tick or {}
        entry_price = tick.get("ask", 0.0) if action == "BUY" else tick.get("bid", 0.0)
        entry_price = self._safe_float(entry_price)
        current_spread_points = self._safe_float(tick.get("spread", 0.0))
        symbol_point = self._safe_float(context.symbol_info.point) if context.symbol_info and context.symbol_info.point else 0.0
        spread_cost = max(0.0, current_spread_points) * symbol_point if symbol_point > 0 else 0.0

        if (
            not tick
            or not math.isfinite(entry_price)
            or not math.isfinite(current_spread_points)
            or not math.isfinite(spread_cost)
            or entry_price <= 0
        ):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="No live price available for execution.",
            )

        profile_spread_limit_points = (
            self._spread_limit_points(profile.max_spread, symbol_point)
            if profile
            else 0.0
        )

        if (
            profile
            and self.signal_pipeline.anti_churn_service.spread_deteriorated(
                reference_spread,
                current_spread_points,
                profile_spread_limit_points,
            )
        ):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Spread deteriorated after evaluation.",
                entry_price=entry_price,
                current_spread=current_spread_points,
            )

        if profile and current_spread_points > profile_spread_limit_points:
            spread_price = current_spread_points * symbol_point if symbol_point > 0 else current_spread_points
            return ExecutionPreflightAssessment(
                approved=False,
                reason=(
                    f"Spread {current_spread_points:.1f} pts / {spread_price:.3f} "
                    f"is above the profile limit {profile.max_spread:.3f}."
                ),
                entry_price=entry_price,
                current_spread=current_spread_points,
            )

        if (
            not math.isfinite(stop_loss)
            or not math.isfinite(take_profit)
            or stop_loss <= 0.0
            or take_profit <= 0.0
        ):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Execution blocked: stop-loss and take-profit are required.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
            )

        reward_risk = self._reward_risk_ratio(entry_price, stop_loss, take_profit, spread_cost=spread_cost)
        user_policy = context.user_policy or {}
        policy_min_rr = self._safe_float(user_policy.get("min_reward_risk", 1.8), default=1.8)
        force_execute_any_conditions = bool(user_policy.get("force_execute_any_conditions", False))
        if action == "BUY" and not (stop_loss < entry_price < take_profit):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Invalid BUY targets: stop-loss/take-profit are not around entry price.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
            )
        if action == "SELL" and not (take_profit < entry_price < stop_loss):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Invalid SELL targets: stop-loss/take-profit are not around entry price.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
            )
        required_rr = max(1.0, policy_min_rr)
        rr_tolerance = 0.02  # Small tolerance for live tick drift between evaluate and send.
        if (not force_execute_any_conditions) and reward_risk + rr_tolerance < required_rr:
            return ExecutionPreflightAssessment(
                approved=False,
                reason=f"Reward:risk fell to {reward_risk:.2f}, below the required {required_rr:.2f}.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
                reward_risk=reward_risk,
                required_reward_risk=required_rr,
            )

        try:
            margin_required = self.execution_engine.estimate_margin(symbol, action, volume, entry_price)
        except Exception:
            margin_required = float("nan")
        if margin_required is None and context.symbol_info:
            contract_size = self._safe_float(context.symbol_info.trade_contract_size, default=0.0)
            leverage = self._safe_float(context.account_leverage, default=0.0)
            margin_required = (
                volume
                * entry_price
                * contract_size
                / max(leverage, 1.0)
            )
        margin_required = self._safe_float(margin_required, default=0.0)
        if not math.isfinite(margin_required) or margin_required <= 0.0:
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Execution blocked: margin requirement could not be verified.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
                reward_risk=reward_risk,
                required_reward_risk=required_rr,
            )

        recent_outcomes = []
        recent_evaluations = []
        # Keep manual/scan executions fast and non-blocking under heavy dashboard scans.
        needs_history_lookups = evaluation_mode not in {"scan", "manual_fast", "auto_fast"}
        if self.db is not None and needs_history_lookups:
            lookback_minutes = max(self.signal_pipeline.risk_engine.settings.cooldown_minutes_per_symbol, 1440)
            recent_outcomes = await self.db.get_recent_symbol_outcomes(
                symbol,
                limit=5,
                within_minutes=lookback_minutes,
            )
            recent_evaluations = await self.db.get_recent_symbol_evaluations(
                symbol,
                limit=5,
                within_minutes=lookback_minutes,
            )

        risk_approval = self.risk_service.assess(
            context=context,
            signal=candidate_signal,
            trade_quality=self.signal_pipeline.trade_quality_service.assess(
                context=context,
                signal=candidate_signal,
                gemini_assessment=None,
                portfolio_fit_score=self.risk_service.preview_portfolio_fit(context),
            ),
            evaluation_mode=evaluation_mode,
            recent_outcomes=recent_outcomes,
            recent_evaluations=recent_evaluations,
            scan_window_id=scan_window_id,
            entry_price=entry_price,
        )
        if not risk_approval.approved:
            return ExecutionPreflightAssessment(
                approved=False,
                reason=risk_approval.reasons[0],
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
                reward_risk=reward_risk,
                required_reward_risk=required_rr,
                margin_required=margin_required,
                risk_approval=risk_approval,
            )

        try:
            tradeability = self.execution_engine.get_tradeability(symbol)
        except Exception:
            tradeability = {"trade_enabled": False, "has_tick": False}
        if not tradeability.get("trade_enabled") or not tradeability.get("has_tick"):
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Symbol is not currently tradeable.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
                reward_risk=reward_risk,
                required_reward_risk=required_rr,
                margin_required=margin_required,
                tradeability=tradeability,
                risk_approval=risk_approval,
            )

        broker_issues = self.broker_symbol_validator.validate_context_order(
            context=context,
            action=action,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_price=entry_price,
            margin_required=margin_required,
        )
        if broker_issues:
            diagnostics = [issue.model_dump() for issue in broker_issues]
            first = diagnostics[0]
            return ExecutionPreflightAssessment(
                approved=False,
                reason=str(first.get("message") or "Broker symbol validation failed."),
                entry_price=entry_price,
                current_spread=current_spread_points,
                spread_cost=spread_cost,
                reward_risk=reward_risk,
                required_reward_risk=required_rr,
                margin_required=margin_required,
                tradeability={**dict(tradeability or {}), "diagnostics": diagnostics},
                risk_approval=risk_approval,
            )

        return ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=entry_price,
            current_spread=current_spread_points,
            spread_cost=spread_cost,
            reward_risk=reward_risk,
            required_reward_risk=required_rr,
            margin_required=margin_required,
            tradeability=tradeability,
            risk_approval=risk_approval,
        )

    def _spread_limit_points(self, max_spread: float, point: float) -> float:
        if point <= 0:
            return max_spread
        return max_spread / point

    def place_order_if_approved(
        self,
        order_request: OrderRequest,
        preflight: ExecutionPreflightAssessment,
    ) -> OrderResult:
        self._schedule_preflight_snapshot(order_request, preflight)
        kill_switch_state = (
            self.kill_switch_state_getter()
            if callable(self.kill_switch_state_getter)
            else None
        )
        if bool(getattr(kill_switch_state, "enabled", False)):
            reason = str(getattr(kill_switch_state, "reason", "") or "Trading kill switch is active.")
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"Execution blocked: global trading kill switch is active. {reason}",
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        if not preflight.approved:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=preflight.reason,
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
        )
        # Never place naked trades without valid protective targets.
        action = str(order_request.action or "").strip().upper()
        if action not in {"BUY", "SELL"}:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=f"Execution blocked: invalid order action {action or 'NONE'}.",
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        volume = self._safe_float(order_request.volume)
        if not math.isfinite(volume) or volume <= 0.0:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc="Execution blocked: order volume is invalid.",
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )

        stop_loss = self._safe_float(order_request.stop_loss)
        take_profit = self._safe_float(order_request.take_profit)
        if (
            not math.isfinite(stop_loss)
            or not math.isfinite(take_profit)
            or stop_loss <= 0.0
            or take_profit <= 0.0
        ):
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc="Execution blocked: stop-loss and take-profit are required.",
                stop_loss=stop_loss if math.isfinite(stop_loss) else None,
                take_profit=take_profit if math.isfinite(take_profit) else None,
                comment=order_request.comment,
            )
        live_entry = self._safe_float(preflight.entry_price, default=0.0)
        if not math.isfinite(live_entry):
            live_entry = 0.0
        if live_entry > 0 and action == "BUY" and not (stop_loss < live_entry < take_profit):
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc="Execution blocked: Invalid BUY targets: stop-loss/take-profit are not around entry price.",
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        if live_entry > 0 and action == "SELL" and not (take_profit < live_entry < stop_loss):
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc="Execution blocked: Invalid SELL targets: stop-loss/take-profit are not around entry price.",
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        preflight_required_rr = self._safe_float(getattr(preflight, "required_reward_risk", 0.0), default=0.0)
        if preflight_required_rr > 0:
            required_rr = max(1.0, preflight_required_rr)
        else:
            settings_rr = self._safe_float(self.risk_service.risk_engine.settings.min_reward_risk_ratio, default=1.2)
            required_rr = max(1.2, settings_rr)
        final_rr = self._reward_risk_ratio(
            live_entry,
            order_request.stop_loss,
            order_request.take_profit,
            spread_cost=self._safe_float(getattr(preflight, "spread_cost", 0.0), default=0.0),
        )
        if live_entry > 0 and (final_rr + 0.02) < required_rr:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=(
                    f"Execution blocked: final TP/SL reward:risk {final_rr:.2f} is below required {required_rr:.2f}."
                ),
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        duplicate_block_reason = self._same_side_duplicate_block_reason(order_request, preflight)
        if duplicate_block_reason:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=duplicate_block_reason,
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        normalized_order_request = order_request.model_copy(
            update={
                "action": action,
                "volume": volume,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        execution_mode = str(getattr(normalized_order_request, "execution_mode", "live") or "live").lower()
        if execution_mode in {"paper", "shadow"}:
            label = "Paper" if execution_mode == "paper" else "Shadow"
            return OrderResult(
                success=True,
                retcode=0,
                retcode_desc=f"{label} execution recorded; broker order_send was not called.",
                ticket=None,
                volume=normalized_order_request.volume,
                price=live_entry if live_entry > 0 else None,
                stop_loss=normalized_order_request.stop_loss,
                take_profit=normalized_order_request.take_profit,
                comment=normalized_order_request.comment,
            )
        return self.execution_engine.place_order(normalized_order_request)

    def _schedule_preflight_snapshot(
        self,
        order_request: OrderRequest,
        preflight: ExecutionPreflightAssessment,
    ) -> None:
        if self.db is None or not hasattr(self.db, "log_execution_preflight_snapshot"):
            return
        snapshot = {
            "symbol": order_request.symbol,
            "action": order_request.action,
            "requested_volume": order_request.volume,
            "stop_loss": order_request.stop_loss,
            "take_profit": order_request.take_profit,
            "comment": order_request.comment,
            "idempotency_key": getattr(order_request, "idempotency_key", ""),
            "account_key": getattr(order_request, "account_key", ""),
            "execution_context": getattr(order_request, "execution_context", ""),
            "preflight": preflight.model_dump() if hasattr(preflight, "model_dump") else dict(preflight.__dict__),
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self.db.log_execution_preflight_snapshot(
                symbol=str(order_request.symbol or ""),
                action=str(order_request.action or ""),
                requested_volume=float(order_request.volume or 0.0),
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                execution_mode=str(getattr(order_request, "execution_mode", "live") or "live").lower(),
                idempotency_key=str(getattr(order_request, "idempotency_key", "") or ""),
                account_key=str(getattr(order_request, "account_key", "") or ""),
                execution_context=str(getattr(order_request, "execution_context", "") or ""),
                approved=bool(getattr(preflight, "approved", False)),
                reason=str(getattr(preflight, "reason", "") or ""),
                snapshot=snapshot,
            )
        )

    def _same_side_duplicate_block_reason(
        self,
        order_request: OrderRequest,
        preflight: ExecutionPreflightAssessment,
    ) -> str | None:
        risk_approval = getattr(preflight, "risk_approval", None)
        machine_reasons = [
            str(reason or "").strip()
            for reason in getattr(risk_approval, "machine_reasons", []) or []
            if str(reason or "").strip()
        ]
        warnings = [
            str(reason or "").strip()
            for reason in getattr(risk_approval, "warnings", []) or []
            if str(reason or "").strip()
        ]
        combined_reasons = machine_reasons + warnings
        if any("Adding to a losing" in reason for reason in combined_reasons):
            return "Execution blocked: adding to an already losing same-side position."

        get_positions = getattr(self.execution_engine, "get_positions", None)
        if not callable(get_positions):
            return None

        try:
            live_positions = list(get_positions(symbol=order_request.symbol) or [])
        except TypeError:
            live_positions = list(get_positions() or [])
        except Exception:
            return None

        requested_side = str(order_request.action or "").strip().upper()
        requested_symbol = str(order_request.symbol or "").strip().upper()
        for pos in live_positions:
            pos_symbol = str(getattr(pos, "symbol", "") or "").strip().upper()
            pos_type = str(getattr(pos, "type", "") or "").strip().upper()
            if pos_symbol != requested_symbol or pos_type != requested_side:
                continue
            pos_profit = self._safe_float(getattr(pos, "profit", 0.0), default=float("-inf"))
            if pos_profit < 0.0:
                return (
                    "Execution blocked: same-symbol same-direction position is already open and losing."
                )
        return None

    def _candidate_signal(
        self,
        *,
        base_signal: TechnicalSignal,
        action: str,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> TechnicalSignal:
        return base_signal.model_copy(
            update={
                "action": action,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )

    def _safe_float(self, value, default: float = float("nan")) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if math.isfinite(parsed) else default

    def _reward_risk_ratio(
        self,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        *,
        spread_cost: float = 0.0,
    ) -> float:
        entry = self._safe_float(entry_price)
        sl = self._safe_float(stop_loss)
        tp = self._safe_float(take_profit)
        spread = self._safe_float(spread_cost, default=0.0)
        if (
            not math.isfinite(entry)
            or not math.isfinite(sl)
            or not math.isfinite(tp)
            or not math.isfinite(spread)
            or entry <= 0
            or sl <= 0
            or tp <= 0
        ):
            return 0.0
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)
        adjusted_risk = sl_distance + max(0.0, spread)
        adjusted_reward = max(0.0, tp_distance - max(0.0, spread))
        return adjusted_reward / adjusted_risk if adjusted_risk > 0 else 0.0
