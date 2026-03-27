from __future__ import annotations

from typing import Iterable

from domain.models import ExecutionDecision, MarketContext


class ReplayService:
    """Runs the exact canonical pipeline over historical snapshots."""

    def __init__(self, signal_pipeline):
        self.signal_pipeline = signal_pipeline

    async def run_contexts(
        self,
        contexts: Iterable[MarketContext],
        requested_agent_name: str = "GeminiAgent",
        with_gemini: bool = True,
    ) -> dict:
        accepted = 0
        blocked = 0
        decisions: list[dict] = []

        for index, context in enumerate(contexts):
            requested = requested_agent_name if with_gemini else "SmartAgent"
            decision = await self.signal_pipeline.evaluate_context(
                context=context.model_copy(update={"evaluation_mode": "replay"}),
                requested_agent_name=requested,
                scan_window_id=f"replay:{index}",
            )
            decisions.append(self._summarize_decision(decision))
            if decision.allow_execute:
                accepted += 1
            else:
                blocked += 1

        return {
            "accepted_trades": accepted,
            "blocked_trades": blocked,
            "decisions": decisions,
        }

    def simulate_outcome(
        self,
        decision: ExecutionDecision,
        future_bars: list[dict],
    ) -> dict:
        signal = decision.signal_decision.final_signal
        if not decision.allow_execute or signal.action not in {"BUY", "SELL"} or not future_bars:
            return {"status": "blocked", "profit": 0.0}

        entry = decision.entry_price
        stop = signal.stop_loss or entry
        target = signal.take_profit or entry
        for bar in future_bars:
            if signal.action == "BUY":
                if bar["low"] <= stop:
                    return {"status": "stop_loss", "profit": stop - entry}
                if bar["high"] >= target:
                    return {"status": "take_profit", "profit": target - entry}
            else:
                if bar["high"] >= stop:
                    return {"status": "stop_loss", "profit": entry - stop}
                if bar["low"] <= target:
                    return {"status": "take_profit", "profit": entry - target}

        return {"status": "time_stop", "profit": future_bars[-1]["close"] - entry if signal.action == "BUY" else entry - future_bars[-1]["close"]}

    def _summarize_decision(self, decision: ExecutionDecision) -> dict:
        return {
            "symbol": decision.signal_decision.market_context.symbol,
            "action": decision.signal_decision.final_signal.action,
            "quality_score": decision.trade_quality_assessment.final_trade_quality_score,
            "blocked_reasons": decision.trade_quality_assessment.no_trade_reasons
            + decision.anti_churn_assessment.reasons
            + decision.portfolio_risk_assessment.blocking_reasons,
            "allow_execute": decision.allow_execute,
            "gemini_used": bool(
                decision.signal_decision.gemini_confirmation
                and decision.signal_decision.gemini_confirmation.used
                and not decision.signal_decision.gemini_confirmation.degraded
            ),
        }
