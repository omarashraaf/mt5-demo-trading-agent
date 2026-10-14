from __future__ import annotations

import json
import logging
import math
import pickle
import time
from pathlib import Path
from typing import Optional

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

_pd = None
_pd_checked = False


def _load_pandas():
    global _pd, _pd_checked
    if _pd_checked:
        return _pd
    _pd_checked = True
    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - optional in cloud runtimes
        logger.warning("Meta-model pandas unavailable during lazy import: %s", exc)
        _pd = None
        return None
    _pd = pd
    return _pd


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
        artifacts_path = Path(artifacts_dir)
        if artifacts_path.is_absolute():
            self.artifacts_dir = artifacts_path
        else:
            backend_root = Path(__file__).resolve().parent.parent
            self.artifacts_dir = (backend_root / artifacts_path).resolve()
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
        return bool(self.enabled and self._model is not None and self._feature_columns and _load_pandas() is not None)

    async def refresh_active_model(self, *, force: bool = False):
        pd = _load_pandas()
        if pd is None:
            self._clear_active()
            return
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
        selected = approved if self._is_usable_live_model(approved) else None
        if selected is None and hasattr(self.db, "list_model_versions"):
            try:
                versions = await self.db.list_model_versions(limit=60)
            except Exception:
                versions = []
            usable_fallbacks = [row for row in versions if self._is_usable_live_model(row)]
            if usable_fallbacks:
                usable_fallbacks.sort(
                    key=lambda row: (
                        float((row.get("evaluation_metrics_json") or {}).get("f1", 0.0) or 0.0),
                        float((row.get("evaluation_metrics_json") or {}).get("precision", 0.0) or 0.0),
                        float(row.get("training_date", 0.0) or 0.0),
                    ),
                    reverse=True,
                )
                selected = usable_fallbacks[0]
                if approved is not None:
                    logger.warning(
                        "Approved meta-model %s is unusable; activating fallback model %s (%s) for live gating.",
                        approved.get("version_id"),
                        selected.get("version_id"),
                        selected.get("approval_status"),
                    )
                else:
                    logger.info(
                        "No approved usable model; activating fallback model %s (%s) for live gating.",
                        selected.get("version_id"),
                        selected.get("approval_status"),
                    )
        if selected is None:
            self._clear_active()
            return

        version_id = str(selected.get("version_id") or "")
        if version_id and version_id == self._active_version_id and self._model is not None:
            return

        artifact_name, summary_name = self._parse_notes_for_artifacts(str(selected.get("notes") or ""))
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

    def _is_usable_live_model(self, model_row: dict | None) -> bool:
        if not model_row:
            return False
        target_definition = str(model_row.get("target_definition") or "").strip().lower()
        if target_definition == "no_trade_label":
            return False
        evaluation = model_row.get("evaluation_metrics_json") or {}
        try:
            precision = float(evaluation.get("precision", 0.0) or 0.0)
            recall = float(evaluation.get("recall", 0.0) or 0.0)
            f1 = float(evaluation.get("f1", 0.0) or 0.0)
            sample_count = int(evaluation.get("dataset_row_count", evaluation.get("sample_count", 0)) or 0)
            confusion = evaluation.get("confusion_matrix", {}) or {}
            positive_count = int(
                evaluation.get(
                    "positive_samples_total",
                    int(confusion.get("tp", 0) or 0) + int(confusion.get("fn", 0) or 0),
                )
                or 0
            )
        except Exception:
            return False
        return (
            sample_count >= 50
            and positive_count >= 12
            and precision >= 0.50
            and recall >= 0.05
            and f1 >= 0.45
        )

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
        refresh_model: bool = True,
    ) -> tuple[TradeDecisionAssessment, dict]:
        pd = _load_pandas()
        if refresh_model:
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
            if not math.isfinite(profit_probability):
                raise ValueError("non-finite profit probability")
            profit_probability = max(0.0, min(1.0, profit_probability))
        except Exception as exc:
            logger.warning("Meta-model scoring failed: %s", exc)
            assessment["reason"] = f"scoring_failed: {exc}"
            return trade_decision, assessment

        expected_edge = (profit_probability * 2.0) - 1.0
        no_trade_probability = 1.0 - profit_probability
        quality_before = float(trade_decision.trade_quality_assessment.final_trade_quality_score or 0.0)
        quality_after = (1.0 - self.quality_blend_alpha) * quality_before + self.quality_blend_alpha * profit_probability
        force_execute_any_conditions = bool((context.user_policy or {}).get("force_execute_any_conditions", False))
        blocked = (
            profit_probability < self.hard_block_threshold
            and trade_decision.final_signal.action in {"BUY", "SELL"}
            and not force_execute_any_conditions
        )
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
        if force_execute_any_conditions and trade_decision.final_signal.action in {"BUY", "SELL"}:
            new_reasons.append("Force execution mode enabled: meta-model hard block bypassed.")
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
            "force_execute_any_conditions": force_execute_any_conditions,
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

    async def preview_signal_insight(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        quality: TradeQualityAssessment,
        gemini_assessment: GeminiAssessment | None,
        portfolio_fit_score: float,
        anti_churn_blocked: bool = False,
        refresh_model: bool = True,
    ) -> dict:
        pd = _load_pandas()
        if refresh_model:
            await self.refresh_active_model()

        assessment = {
            "enabled": bool(self.enabled),
            "active": self.is_active,
            "model_version_id": self._active_version_id if self.is_active else "",
            "profit_probability": 0.0,
            "expected_edge": 0.0,
            "no_trade_probability": 1.0,
            "reason": "no approved model",
            "input_quality_score": float(quality.final_trade_quality_score or 0.0),
            "quality_threshold": float(quality.threshold or 0.0),
        }
        if not self.is_active:
            return assessment

        feature_snapshot = build_feature_snapshot_from_inputs(
            context=context,
            signal=signal,
            quality=quality,
            gemini=gemini_assessment,
            portfolio_risk=PortfolioRiskAssessment(portfolio_fit_score=float(portfolio_fit_score or 0.0)),
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
            if not math.isfinite(profit_probability):
                raise ValueError("non-finite profit probability")
            profit_probability = max(0.0, min(1.0, profit_probability))
        except Exception as exc:
            logger.warning("Meta-model preview scoring failed: %s", exc)
            assessment["reason"] = f"scoring_failed: {exc}"
            return assessment

        assessment["profit_probability"] = round(profit_probability, 4)
        assessment["expected_edge"] = round((profit_probability * 2.0) - 1.0, 4)
        assessment["no_trade_probability"] = round(1.0 - profit_probability, 4)
        if profit_probability < self.hard_block_threshold:
            assessment["reason"] = "hard_block_risk"
        elif profit_probability < self.min_profit_probability:
            assessment["reason"] = "caution"
        else:
            assessment["reason"] = "supportive"
        return assessment
