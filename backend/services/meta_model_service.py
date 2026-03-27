from __future__ import annotations

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from domain.models import (
    AntiChurnAssessment,
    GeminiAssessment,
    MarketContext,
    PortfolioRiskAssessment,
    TechnicalSignal,
    TradeDecisionAssessment,
    TradeQualityAssessment,
)
from research.feature_builder import build_feature_snapshot_from_inputs

logger = logging.getLogger(__name__)


class MetaModelService:
    """
    Offline-trained meta-model scorer used as a quality filter on top of
    SmartAgent/Gemini signals. It never places orders and never bypasses risk.
    """

    def __init__(
        self,
        db=None,
        *,
        artifacts_dir: str = "research_artifacts/models",
        enabled: bool = True,
        min_profit_probability: float = 0.55,
        hard_block_threshold: float = 0.35,
        quality_blend_alpha: float = 0.35,
    ):
        self.db = db
        self.artifacts_dir = Path(artifacts_dir)
        self.enabled = enabled
        self.min_profit_probability = float(min_profit_probability)
        self.hard_block_threshold = float(hard_block_threshold)
        self.quality_blend_alpha = float(quality_blend_alpha)

        self._active_version_id: str = ""
        self._model = None
        self._feature_columns: list[str] = []
        self._numeric_feature_columns: list[str] = []
        self._categorical_feature_columns: list[str] = []
        self._last_refresh_at: float = 0.0

    def set_database(self, db):
        self.db = db
        self._last_refresh_at = 0.0

    @property
    def active_version_id(self) -> str:
        return self._active_version_id

    @property
    def is_active(self) -> bool:
        return bool(self.enabled and self._model is not None and self._feature_columns)

    async def refresh_active_model(self, *, force: bool = False):
        if not self.enabled or self.db is None:
            self._clear_active()
            return
        if not hasattr(self.db, "get_latest_approved_model_version"):
            self._clear_active()
            return
        if not force and (time.time() - self._last_refresh_at) < 30.0:
            return
        self._last_refresh_at = time.time()

        approved = await self.db.get_latest_approved_model_version()
        if approved is None:
            self._clear_active()
            return
        target_definition = str(approved.get("target_definition") or "").strip().lower()
        if target_definition == "no_trade_label":
            logger.warning(
                "Approved meta-model %s targets no_trade_label; keeping meta-model inactive for live gating.",
                approved.get("version_id"),
            )
            self._clear_active()
            return

        version_id = str(approved.get("version_id") or "")
        if version_id and version_id == self._active_version_id and self._model is not None:
            return

        artifact_name, summary_name = self._parse_notes_for_artifacts(str(approved.get("notes") or ""))
        artifact_path = self.artifacts_dir / (artifact_name or f"{version_id}.pkl")
        summary_path = self.artifacts_dir / (summary_name or f"{version_id}.json")
        if not artifact_path.exists() or not summary_path.exists():
            logger.warning(
                "Approved meta-model artifacts missing (version=%s, artifact=%s, summary=%s). Meta-model inactive.",
                version_id,
                artifact_path,
                summary_path,
            )
            self._clear_active()
            return

        with artifact_path.open("rb") as handle:
            model = pickle.load(handle)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        feature_columns = list(summary.get("feature_columns") or [])
        if not feature_columns:
            logger.warning("Approved meta-model summary has no feature_columns; version=%s", version_id)
            self._clear_active()
            return

        self._active_version_id = version_id
        self._model = model
        self._feature_columns = feature_columns
        self._numeric_feature_columns = []
        self._categorical_feature_columns = list(feature_columns)
        try:
            pre = model.named_steps.get("preprocessor")
            if pre is not None and hasattr(pre, "transformers_"):
                for name, _transformer, cols in pre.transformers_:
                    if not isinstance(cols, list):
                        continue
                    if name == "num":
                        self._numeric_feature_columns = list(cols)
                    elif name == "cat":
                        self._categorical_feature_columns = list(cols)
        except Exception:
            pass
        logger.info("Meta-model activated: version=%s", version_id)

    def _clear_active(self):
        self._active_version_id = ""
        self._model = None
        self._feature_columns = []
        self._numeric_feature_columns = []
        self._categorical_feature_columns = []

    @staticmethod
    def _parse_notes_for_artifacts(notes: str) -> tuple[str, str]:
        artifact = ""
        summary = ""
        for part in notes.split(";"):
            piece = part.strip()
            if piece.startswith("artifact="):
                artifact = piece.split("=", 1)[1].strip()
            elif piece.startswith("summary="):
                summary = piece.split("=", 1)[1].strip()
        return artifact, summary

    async def assess_trade_decision(
        self,
        *,
        context: MarketContext,
        trade_decision: TradeDecisionAssessment,
        gemini_assessment: GeminiAssessment | None,
        portfolio_risk_assessment: PortfolioRiskAssessment,
        anti_churn_blocked: bool = False,
    ) -> tuple[TradeDecisionAssessment, dict]:
        await self.refresh_active_model()

        assessment = {
            "enabled": bool(self.enabled),
            "active": self.is_active,
            "model_version_id": self._active_version_id if self.is_active else "",
            "profit_probability": 0.0,
            "expected_edge": 0.0,
            "no_trade_probability": 1.0,
            "hold_bucket": "",
            "reason": "no approved model",
            "changed_decision": False,
            "blocked": False,
            "quality_before": float(trade_decision.trade_quality_assessment.final_trade_quality_score or 0.0),
            "quality_after": float(trade_decision.trade_quality_assessment.final_trade_quality_score or 0.0),
        }
        if not self.is_active:
            return trade_decision, assessment

        feature_snapshot = build_feature_snapshot_from_inputs(
            context=context,
            signal=trade_decision.final_signal,
            quality=trade_decision.trade_quality_assessment,
            gemini=gemini_assessment,
            portfolio_risk=portfolio_risk_assessment,
            anti_churn=AntiChurnAssessment(blocked=bool(anti_churn_blocked)),
        )

        feature_row = {c: feature_snapshot.get(c.removeprefix("f_"), "") for c in self._feature_columns}
        frame = pd.DataFrame([feature_row])
        for col in self._numeric_feature_columns:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0).astype(float)
        for col in self._categorical_feature_columns:
            if col in frame.columns:
                frame[col] = frame[col].fillna("").astype(str)
        try:
            probs = self._model.predict_proba(frame[self._feature_columns])
            profit_probability = float(probs[0][1])
        except Exception as exc:
            logger.warning("Meta-model scoring failed: %s", exc)
            assessment["reason"] = f"scoring_failed: {exc}"
            return trade_decision, assessment

        expected_edge = (profit_probability * 2.0) - 1.0
        no_trade_probability = 1.0 - profit_probability
        quality_before = float(trade_decision.trade_quality_assessment.final_trade_quality_score or 0.0)
        quality_after = (1.0 - self.quality_blend_alpha) * quality_before + self.quality_blend_alpha * profit_probability
        blocked = profit_probability < self.hard_block_threshold and trade_decision.final_signal.action in {"BUY", "SELL"}
        caution = profit_probability < self.min_profit_probability and trade_decision.final_signal.action in {"BUY", "SELL"}

        updated_quality: TradeQualityAssessment = trade_decision.trade_quality_assessment.model_copy(deep=True)
        updated_quality.final_trade_quality_score = max(0.0, min(1.0, quality_after))
        new_reasons = list(updated_quality.no_trade_reasons or [])
        if blocked:
            new_reasons.append(
                f"Meta-model blocked setup: profit_probability={profit_probability:.2f} below hard block {self.hard_block_threshold:.2f}"
            )
            updated_quality.no_trade_zone = True
        elif caution:
            new_reasons.append(
                f"Meta-model caution: profit_probability={profit_probability:.2f} below preferred {self.min_profit_probability:.2f}"
            )
        if new_reasons:
            updated_quality.no_trade_reasons = list(dict.fromkeys(new_reasons))

        updated_signal: TechnicalSignal = trade_decision.final_signal.model_copy(deep=True)
        signal_meta = dict(updated_signal.metadata or {})
        signal_meta["meta_model"] = {
            "version_id": self._active_version_id,
            "profit_probability": profit_probability,
            "expected_edge": expected_edge,
            "no_trade_probability": no_trade_probability,
            "blocked": blocked,
            "quality_before": quality_before,
            "quality_after": updated_quality.final_trade_quality_score,
        }
        updated_signal.metadata = signal_meta

        changed_decision = False
        updated_trade = bool(trade_decision.trade)
        updated_final_direction = trade_decision.final_direction
        updated_reasons = list(trade_decision.reasons or [])
        if blocked and updated_trade:
            changed_decision = True
            updated_trade = False
            updated_final_direction = "HOLD"
            updated_reasons.append("Meta-model blocked trade candidate.")

        updated = trade_decision.model_copy(
            update={
                "trade": updated_trade,
                "final_direction": updated_final_direction,
                "final_signal": updated_signal,
                "trade_quality_assessment": updated_quality,
                "reasons": list(dict.fromkeys(updated_reasons)),
            }
        )
        assessment.update(
            {
                "profit_probability": profit_probability,
                "expected_edge": expected_edge,
                "no_trade_probability": no_trade_probability,
                "reason": "scored",
                "changed_decision": changed_decision,
                "blocked": blocked,
                "quality_before": quality_before,
                "quality_after": float(updated_quality.final_trade_quality_score or quality_before),
            }
        )
        return updated, assessment
