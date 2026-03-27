from __future__ import annotations

import time
from typing import Optional

from research.feature_builder import FEATURE_SCHEMA_VERSION as FEATURE_SCHEMA_VERSION_V2


class ResearchRepository:
    """Thin repository wrapper for research/meta-model journaling."""

    FEATURE_SCHEMA_VERSION = FEATURE_SCHEMA_VERSION_V2

    def __init__(self, db):
        self.db = db

    async def log_candidate_with_features(
        self,
        *,
        candidate_id: str,
        signal_id: Optional[int],
        candidate_payload: dict,
        feature_snapshot: dict,
    ) -> tuple[int, int]:
        candidate_row_id = await self.db.log_trade_candidate(
            candidate_id=candidate_id,
            signal_id=signal_id,
            symbol=candidate_payload.get("symbol", ""),
            asset_class=candidate_payload.get("asset_class", ""),
            strategy_mode=candidate_payload.get("strategy_mode", ""),
            session=candidate_payload.get("session", ""),
            day_of_week=int(candidate_payload.get("day_of_week", 0)),
            technical_direction=candidate_payload.get("technical_direction", "HOLD"),
            smart_agent_summary=candidate_payload.get("smart_agent_summary", ""),
            gemini_summary=candidate_payload.get("gemini_summary", ""),
            quality_score=float(candidate_payload.get("quality_score", 0.0)),
            confidence_score=float(candidate_payload.get("confidence_score", 0.0)),
            trend_h1=candidate_payload.get("trend_h1", ""),
            trend_h4=candidate_payload.get("trend_h4", ""),
            stop_loss=float(candidate_payload.get("stop_loss", 0.0)),
            take_profit=float(candidate_payload.get("take_profit", 0.0)),
            reward_risk=float(candidate_payload.get("reward_risk", 0.0)),
            spread_at_eval=float(candidate_payload.get("spread_at_eval", 0.0)),
            atr_regime=candidate_payload.get("atr_regime", ""),
            support_resistance_context=candidate_payload.get("support_resistance_context", ""),
            event_id=candidate_payload.get("event_id", ""),
            event_type=candidate_payload.get("event_type", ""),
            event_importance=candidate_payload.get("event_importance", ""),
            contradiction_flag=bool(candidate_payload.get("contradiction_flag", False)),
            risk_decision=candidate_payload.get("risk_decision", ""),
            rejection_reasons=list(candidate_payload.get("rejection_reasons", [])),
            executed=bool(candidate_payload.get("executed", False)),
            gemini_changed_decision=bool(candidate_payload.get("gemini_changed_decision", False)),
            meta_model_changed_decision=bool(candidate_payload.get("meta_model_changed_decision", False)),
        )
        feature_row_id = await self.db.log_feature_snapshot(
            candidate_id=candidate_id,
            signal_id=signal_id,
            schema_version=self.FEATURE_SCHEMA_VERSION,
            features={
                **(feature_snapshot or {}),
                "schema_version": self.FEATURE_SCHEMA_VERSION,
                "logged_at": time.time(),
            },
        )
        return candidate_row_id, feature_row_id

    async def mark_signal_executed(
        self,
        *,
        signal_id: Optional[int],
        ticket: Optional[int],
        fill_price: Optional[float],
        slippage_estimate: Optional[float] = None,
        margin_snapshot: Optional[dict] = None,
    ):
        await self.db.mark_trade_candidate_execution(
            signal_id=signal_id,
            executed=True,
            ticket=ticket,
            fill_price=fill_price,
            slippage_estimate=slippage_estimate,
            margin_snapshot=margin_snapshot or {},
        )
