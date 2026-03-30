from __future__ import annotations

from domain.models import ExecutionPreflightAssessment, TechnicalSignal
from mt5.execution import OrderRequest, OrderResult


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
        action = candidate_signal.action
        stop_loss = candidate_signal.stop_loss
        take_profit = candidate_signal.take_profit
        profile = context.profile
        tick = context.tick or {}
        entry_price = tick.get("ask", 0.0) if action == "BUY" else tick.get("bid", 0.0)
        current_spread_points = float(tick.get("spread", 0.0))
        symbol_point = float(context.symbol_info.point) if context.symbol_info and context.symbol_info.point else 0.0

        if not tick or entry_price <= 0:
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

        reward_risk = self._reward_risk_ratio(entry_price, stop_loss, take_profit)
        user_policy = context.user_policy or {}
        policy_min_rr = float(user_policy.get("min_reward_risk", 1.8))
        required_rr = max(1.0, policy_min_rr)
        rr_tolerance = 0.02  # Small tolerance for live tick drift between evaluate and send.
        if reward_risk + rr_tolerance < required_rr:
            return ExecutionPreflightAssessment(
                approved=False,
                reason=f"Reward:risk fell to {reward_risk:.2f}, below the required {required_rr:.2f}.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                reward_risk=reward_risk,
            )

        margin_required = self.execution_engine.estimate_margin(symbol, action, volume, entry_price)
        if margin_required is None and context.symbol_info:
            margin_required = (
                volume
                * entry_price
                * context.symbol_info.trade_contract_size
                / max(context.account_leverage or 1, 1)
            )
        margin_required = float(margin_required or 0.0)

        recent_outcomes = []
        recent_evaluations = []
        if self.db is not None:
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
                reward_risk=reward_risk,
                margin_required=margin_required,
                risk_approval=risk_approval,
            )

        tradeability = self.execution_engine.get_tradeability(symbol)
        if not tradeability["trade_enabled"] or not tradeability["has_tick"]:
            return ExecutionPreflightAssessment(
                approved=False,
                reason="Symbol is not currently tradeable.",
                entry_price=entry_price,
                current_spread=current_spread_points,
                reward_risk=reward_risk,
                margin_required=margin_required,
                tradeability=tradeability,
                risk_approval=risk_approval,
            )

        return ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=entry_price,
            current_spread=current_spread_points,
            reward_risk=reward_risk,
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
        if not preflight.approved:
            return OrderResult(
                success=False,
                retcode=-1,
                retcode_desc=preflight.reason,
                stop_loss=order_request.stop_loss,
                take_profit=order_request.take_profit,
                comment=order_request.comment,
            )
        return self.execution_engine.place_order(order_request)

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

    def _reward_risk_ratio(
        self,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> float:
        if entry_price <= 0 or stop_loss is None or take_profit is None:
            return 0.0
        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        return tp_distance / sl_distance if sl_distance > 0 else 0.0
