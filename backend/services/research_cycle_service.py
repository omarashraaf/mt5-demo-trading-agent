from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

from monitoring.performance_attribution import summarize_basic_attribution
from research.model_registry import ModelRegistry
from research.model_trainer import ModelTrainer
from research.replay_runner import ReplayRunner
from research.trade_dataset_builder import TradeDatasetBuilder
from research.walk_forward_runner import WalkForwardRunner
from services.meta_model_service import MetaModelService


class ResearchCycleService:
    def __init__(
        self,
        db,
        *,
        meta_model_service: Optional[MetaModelService] = None,
        artifacts_dir: str = "research_artifacts/models",
        exports_dir: str = "research_exports",
    ):
        self.db = db
        self.meta_model_service = meta_model_service
        self.dataset_builder = TradeDatasetBuilder(db)
        self.model_registry = ModelRegistry(db)
        self.model_trainer = ModelTrainer(db, artifacts_dir=artifacts_dir)
        self.replay_runner = ReplayRunner(db, artifacts_dir=artifacts_dir)
        self.walk_forward_runner = WalkForwardRunner(db)
        self.exports_dir = Path(exports_dir)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._last_incremental_train_at: float = 0.0
        self._last_incremental_closed_count: int = 0
        self._last_analysis_train_at: float = 0.0
        self._training_lock = asyncio.Lock()

    async def rebuild_dataset(
        self,
        *,
        output_name: str = "trade_dataset",
        limit: int = 100000,
        include_unexecuted: bool = True,
        parquet: bool = False,
    ) -> dict:
        base = self.exports_dir / output_name
        csv_path = await self.dataset_builder.export_csv(
            str(base.with_suffix(".csv")),
            limit=limit,
            include_unexecuted=include_unexecuted,
        )
        metadata_path = await self.dataset_builder.export_metadata_json(
            str(base.with_suffix(".metadata.json")),
            limit=limit,
            include_unexecuted=include_unexecuted,
        )
        parquet_path = None
        if parquet:
            parquet_path = await self.dataset_builder.export_parquet(
                str(base.with_suffix(".parquet")),
                limit=limit,
                include_unexecuted=include_unexecuted,
            )
        return {
            "csv_path": str(Path(csv_path).resolve()),
            "metadata_path": str(Path(metadata_path).resolve()),
            "parquet_path": str(Path(parquet_path).resolve()) if parquet_path else None,
            "limit": limit,
            "include_unexecuted": include_unexecuted,
            "parquet": parquet,
        }

    async def train_candidate_model(
        self,
        *,
        algorithm: str = "logistic_regression",
        target_column: str = "profitable_after_costs_180m",
        include_unexecuted: bool = True,
        min_rows: int = 30,
        min_positive_samples: int = 20,
    ) -> dict:
        return await self.model_trainer.train_candidate_model(
            algorithm=algorithm,
            target_column=target_column,
            include_unexecuted=include_unexecuted,
            min_rows=min_rows,
            min_positive_rows=min_positive_samples,
        )

    async def run_replay(
        self,
        *,
        version_id: str,
        score_threshold: float = 0.55,
        include_unexecuted: bool = True,
        limit: int = 200000,
    ) -> dict:
        return await self.replay_runner.run_replay(
            version_id=version_id,
            score_threshold=score_threshold,
            include_unexecuted=include_unexecuted,
            limit=limit,
        )

    async def run_walk_forward(
        self,
        *,
        algorithm: str = "logistic_regression",
        target_column: str = "profitable_after_costs_90m",
        score_threshold: float = 0.55,
        windows: int = 5,
        include_unexecuted: bool = True,
        limit: int = 200000,
    ) -> dict:
        return await self.walk_forward_runner.run_walk_forward(
            algorithm=algorithm,
            target_column=target_column,
            score_threshold=score_threshold,
            windows=windows,
            include_unexecuted=include_unexecuted,
            limit=limit,
        )

    async def list_model_versions(self, limit: int = 50) -> list[dict]:
        return await self.model_registry.list_versions(limit=limit)

    def _model_approval_gate(
        self,
        model: dict,
        *,
        min_precision: float = 0.50,
        min_recall: float = 0.05,
        min_f1: float = 0.45,
        min_samples: int = 50,
        min_positive_samples: int = 12,
    ) -> tuple[bool, list[str]]:
        evaluation = model.get("evaluation_metrics_json", {}) or {}
        target_definition = str(model.get("target_definition") or "").strip().lower()
        reasons: list[str] = []
        if target_definition == "no_trade_label":
            reasons.append("no_trade_label models cannot be approved for live gating")
        sample_count = int(evaluation.get("dataset_row_count", evaluation.get("sample_count", 0)) or 0)
        precision = float(evaluation.get("precision", 0.0) or 0.0)
        recall = float(evaluation.get("recall", 0.0) or 0.0)
        f1 = float(evaluation.get("f1", 0.0) or 0.0)
        confusion = evaluation.get("confusion_matrix", {}) or {}
        positive_count = int(
            evaluation.get(
                "positive_samples_total",
                int(confusion.get("tp", 0) or 0) + int(confusion.get("fn", 0) or 0),
            )
            or 0
        )
        if sample_count < min_samples:
            reasons.append(f"sample_count={sample_count} below min_samples={min_samples}")
        if positive_count < min_positive_samples:
            reasons.append(f"positive_samples={positive_count} below min_positive_samples={min_positive_samples}")
        if precision < min_precision:
            reasons.append(f"precision={precision:.3f} below min_precision={min_precision:.3f}")
        if recall < min_recall:
            reasons.append(f"recall={recall:.3f} below min_recall={min_recall:.3f}")
        if f1 < min_f1:
            reasons.append(f"f1={f1:.3f} below min_f1={min_f1:.3f}")
        return not reasons, reasons

    def _model_quality_rank(self, model: dict | None) -> tuple[float, float, float, int, float]:
        if not model:
            return (0.0, 0.0, 0.0, 0, 0.0)
        evaluation = model.get("evaluation_metrics_json", {}) or {}
        try:
            f1 = float(evaluation.get("f1", 0.0) or 0.0)
            precision = float(evaluation.get("precision", 0.0) or 0.0)
            recall = float(evaluation.get("recall", 0.0) or 0.0)
            sample_count = int(evaluation.get("dataset_row_count", evaluation.get("sample_count", 0)) or 0)
            training_date = float(model.get("training_date", 0.0) or 0.0)
        except Exception:
            return (0.0, 0.0, 0.0, 0, 0.0)
        enough_evidence = 1 if sample_count >= 50 else 0
        return (float(enough_evidence), f1, precision, recall, sample_count, training_date)

    async def approve_model(self, version_id: str) -> dict:
        versions = await self.model_registry.list_versions(limit=200)
        target = next((row for row in versions if str(row.get("version_id") or "") == str(version_id)), None)
        if not target:
            raise ValueError(f"Model version not found: {version_id}")
        ok, reasons = self._model_approval_gate(target)
        if not ok:
            raise ValueError("Model does not pass approval gate: " + "; ".join(reasons))
        return await self.model_registry.set_status(version_id=version_id, status="approved")

    async def activate_approved_model(self) -> dict:
        if self.meta_model_service is None:
            return {"activated": False, "reason": "meta_model_service_unavailable"}
        await self.meta_model_service.refresh_active_model(force=True)
        return {
            "activated": bool(self.meta_model_service.is_active),
            "active_version_id": self.meta_model_service.active_version_id,
        }

    async def generate_attribution_report(
        self,
        *,
        report_type: str = "full",
        limit: int = 2000,
    ) -> dict:
        outcomes = await self.db.get_trade_outcomes(limit=limit)
        versions = await self.db.list_model_versions(limit=10)
        latest_model = versions[0] if versions else None
        latest_eval = latest_model.get("evaluation_metrics_json", {}) if latest_model else {}
        feature_importance = latest_eval.get("feature_importance", [])

        symbol_profitability = summarize_basic_attribution(outcomes).get("by_symbol", {})
        hold_buckets: dict[str, dict] = {}
        spread_buckets = {
            "tight": {"count": 0, "pnl": 0.0},
            "normal": {"count": 0, "pnl": 0.0},
            "wide": {"count": 0, "pnl": 0.0},
        }
        for row in outcomes:
            hold = row.get("holding_minutes", 0.0)
            if hold < 15:
                key = "0_15m"
            elif hold < 30:
                key = "15_30m"
            elif hold < 60:
                key = "30_60m"
            elif hold < 180:
                key = "60_180m"
            else:
                key = "180m_plus"
            item = hold_buckets.setdefault(key, {"count": 0, "pnl": 0.0, "wins": 0})
            pnl = self._outcome_profit(row)
            item["count"] += 1
            item["pnl"] += pnl
            if pnl > 0:
                item["wins"] += 1

            spread_proxy = abs(float(row.get("confidence", 0.0) or 0.0))
            bucket = "tight" if spread_proxy < 0.45 else "normal" if spread_proxy < 0.75 else "wide"
            spread_buckets[bucket]["count"] += 1
            spread_buckets[bucket]["pnl"] += pnl

        event_type_attr: dict[str, dict] = {}
        rows, _ = await self.dataset_builder.build_dataset(limit=limit, include_unexecuted=True)
        for row in rows:
            if int(row.get("executed", 0)) != 1:
                continue
            event_type = str(row.get("event_type") or "unknown")
            pnl = float(row.get("expected_return_180m", 0.0) or 0.0)
            item = event_type_attr.setdefault(event_type, {"count": 0, "pnl": 0.0})
            item["count"] += 1
            item["pnl"] += pnl

        report = {
            "report_id": f"attr-{uuid.uuid4().hex[:12]}",
            "report_type": report_type,
            "generated_at": time.time(),
            "sample_size_outcomes": len(outcomes),
            "feature_importance_report": feature_importance,
            "symbol_profitability_report": symbol_profitability,
            "hold_duration_report": hold_buckets,
            "event_type_attribution_report": event_type_attr,
            "spread_cost_impact_report": spread_buckets,
            "meta_model_context": {
                "latest_model_version": latest_model.get("version_id") if latest_model else "",
                "latest_model_status": latest_model.get("approval_status") if latest_model else "",
            },
        }
        await self.db.save_attribution_report(
            report_id=report["report_id"],
            report_type=report_type,
            data_range_start=min((float(o.get("timestamp", 0.0) or 0.0) for o in outcomes), default=None),
            data_range_end=max((float(o.get("closed_at", 0.0) or 0.0) for o in outcomes), default=None),
            report=report,
        )
        return report

    async def status_snapshot(self) -> dict:
        closed_count = 0
        clean_closed_count = 0
        if hasattr(self.db, "count_trade_outcomes"):
            try:
                closed_count = int(await self.db.count_trade_outcomes())
            except Exception:
                closed_count = 0
        if hasattr(self.db, "count_clean_app_trade_outcomes"):
            try:
                clean_closed_count = int(await self.db.count_clean_app_trade_outcomes())
            except Exception:
                clean_closed_count = 0
        active_approved = await self.db.get_latest_approved_model_version()
        latest_training_run = await self.db.get_latest_model_run()
        latest_replay_run = await self.db.get_latest_replay_run()
        latest_walk_forward = await self.db.get_latest_replay_run(notes_like="walk_forward%")
        recent_reports = await self.db.list_attribution_reports(limit=5)
        versions = await self.db.list_model_versions(limit=20)
        candidates = [v for v in versions if v.get("approval_status") == "candidate"]
        best_candidate = max(candidates, key=self._model_quality_rank) if candidates else None
        if self.meta_model_service is not None:
            await self.meta_model_service.refresh_active_model()
        model_quality = self._training_quality_summary(
            active_model=active_approved,
            best_candidate=best_candidate,
            meta_model_active=bool(self.meta_model_service.is_active) if self.meta_model_service else False,
            clean_closed_count=clean_closed_count,
        )
        return {
            "meta_model_active": bool(self.meta_model_service.is_active) if self.meta_model_service else False,
            "meta_model_active_version": self.meta_model_service.active_version_id if self.meta_model_service else "",
            "active_approved_model": active_approved,
            "last_training_run": latest_training_run,
            "last_replay_run": latest_replay_run,
            "last_walk_forward_run": latest_walk_forward,
            "best_candidate_model": best_candidate,
            "recent_attribution_reports": recent_reports,
            "model_quality": model_quality,
            "incremental_training": {
                "last_run_at": self._last_incremental_train_at,
                "last_closed_count": self._last_incremental_closed_count,
                "local_closed_count": closed_count,
                "clean_app_closed_count": clean_closed_count,
            },
        }

    def _training_quality_summary(
        self,
        *,
        active_model: dict | None,
        best_candidate: dict | None,
        meta_model_active: bool,
        clean_closed_count: int,
    ) -> dict:
        model = active_model if (meta_model_active and active_model) else (best_candidate or active_model or {})
        evaluation = model.get("evaluation_metrics_json", {}) if isinstance(model, dict) else {}
        precision = float((evaluation or {}).get("precision", 0.0) or 0.0)
        recall = float((evaluation or {}).get("recall", 0.0) or 0.0)
        f1 = float((evaluation or {}).get("f1", 0.0) or 0.0)
        sample_count = int((evaluation or {}).get("dataset_row_count", (evaluation or {}).get("sample_count", 0)) or 0)
        blockers: list[str] = []
        warnings: list[str] = []
        if not meta_model_active:
            blockers.append("Meta-model is not active.")
        if clean_closed_count < 50:
            blockers.append(f"Clean app-owned closed trades {clean_closed_count} below minimum 50.")
        if precision <= 0.0 or recall <= 0.0 or f1 <= 0.0:
            blockers.append("Current model has no proven positive-class precision/recall/F1.")
        elif precision < 0.50 or f1 < 0.45:
            warnings.append(f"Model predictive metrics are weak: precision={precision:.2f}, f1={f1:.2f}.")
        score = 10.0
        score -= 2.0 * len(blockers)
        score -= 0.75 * len(warnings)
        return {
            "score": round(max(0.0, min(10.0, score)), 1),
            "status": "blocked" if blockers else ("warning" if warnings else "ready"),
            "blockers": blockers,
            "warnings": warnings,
            "checks": {
                "meta_model_active": bool(meta_model_active),
                "clean_app_closed_count": int(clean_closed_count or 0),
                "sample_count": sample_count,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "model_version": model.get("version_id") if isinstance(model, dict) else "",
                "approval_status": model.get("approval_status") if isinstance(model, dict) else "",
            },
        }

    def _outcome_profit(self, row: dict) -> float:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            if row.get(key) is not None:
                return float(row.get(key) or 0.0)
        return 0.0

    async def maybe_train_from_new_trade_outcome(
        self,
        *,
        min_rows: int = 30,
        cooldown_seconds: int = 180,
        auto_approve: bool = True,
        min_precision: float = 0.50,
        min_f1: float = 0.45,
        min_positive_samples: int = 20,
        target_column: str = "profitable_after_costs_180m",
    ) -> dict:
        if not hasattr(self.db, "count_trade_outcomes"):
            return {"trained": False, "reason": "count_trade_outcomes unavailable"}

        clean_count = (
            await self.db.count_clean_app_trade_outcomes()
            if hasattr(self.db, "count_clean_app_trade_outcomes")
            else await self.db.count_trade_outcomes()
        )
        if clean_count < min_rows:
            return {
                "trained": False,
                "reason": f"clean app-owned closed trades {clean_count} below minimum {min_rows}",
                "closed_count": clean_count,
            }
        if clean_count <= self._last_incremental_closed_count:
            return {
                "trained": False,
                "reason": "no new closed trades since last incremental training",
                "closed_count": clean_count,
            }
        now = time.time()
        if now - self._last_incremental_train_at < cooldown_seconds:
            return {
                "trained": False,
                "reason": f"cooldown active ({cooldown_seconds}s)",
                "closed_count": clean_count,
            }

        if self._training_lock.locked():
            return {
                "trained": False,
                "reason": "meta training already running",
                "closed_count": clean_count,
            }

        async with self._training_lock:
            summary = await self.train_candidate_model(
                algorithm="logistic_regression",
                target_column=target_column,
                include_unexecuted=False,
                min_rows=min_rows,
                min_positive_samples=min_positive_samples,
            )
        self._last_incremental_train_at = now
        self._last_incremental_closed_count = clean_count

        version_id = str(summary.get("version_id", "")).strip()
        evaluation = summary.get("evaluation", {}) or {}
        precision = float(evaluation.get("precision", 0.0) or 0.0)
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

        approved = False
        activated = False
        auto_approve_reasons: list[str] = []
        if sample_count < min_rows:
            auto_approve_reasons.append(f"sample_count={sample_count} below min_rows={min_rows}")
        if precision < min_precision:
            auto_approve_reasons.append(f"precision={precision:.3f} below min_precision={min_precision:.3f}")
        if f1 < min_f1:
            auto_approve_reasons.append(f"f1={f1:.3f} below min_f1={min_f1:.3f}")
        if positive_count < min_positive_samples:
            auto_approve_reasons.append(
                f"positive_samples={positive_count} below min_positive_samples={min_positive_samples}"
            )
        champion_ok, champion_reason = await self._candidate_not_worse_than_active(evaluation=evaluation)
        if not champion_ok:
            auto_approve_reasons.append(champion_reason)

        if auto_approve and version_id and not auto_approve_reasons:
            await self.approve_model(version_id)
            activation = await self.activate_approved_model()
            approved = True
            activated = bool(activation.get("activated", False))

        return {
            "trained": True,
            "closed_count": clean_count,
            "version_id": version_id,
            "evaluation": evaluation,
            "positive_samples": positive_count,
            "approved": approved,
            "activated": activated,
            "auto_approve_reasons": auto_approve_reasons,
        }

    async def maybe_train_from_post_analysis_update(
        self,
        *,
        min_rows: int = 30,
        cooldown_seconds: int = 600,
        auto_approve: bool = True,
        min_precision: float = 0.50,
        min_f1: float = 0.45,
        min_positive_samples: int = 20,
        target_column: str = "profitable_after_costs_180m",
    ) -> dict:
        if not hasattr(self.db, "count_trade_outcomes"):
            return {"trained": False, "reason": "count_trade_outcomes unavailable"}

        clean_count = (
            await self.db.count_clean_app_trade_outcomes()
            if hasattr(self.db, "count_clean_app_trade_outcomes")
            else await self.db.count_trade_outcomes()
        )
        if clean_count < min_rows:
            return {
                "trained": False,
                "reason": f"clean app-owned closed trades {clean_count} below minimum {min_rows}",
                "closed_count": clean_count,
            }
        now = time.time()
        if now - self._last_analysis_train_at < cooldown_seconds:
            return {
                "trained": False,
                "reason": f"analysis-train cooldown active ({cooldown_seconds}s)",
                "closed_count": clean_count,
            }

        if self._training_lock.locked():
            return {
                "trained": False,
                "reason": "meta training already running",
                "closed_count": clean_count,
            }

        async with self._training_lock:
            summary = await self.train_candidate_model(
                algorithm="logistic_regression",
                target_column=target_column,
                include_unexecuted=False,
                min_rows=min_rows,
                min_positive_samples=min_positive_samples,
            )
        self._last_analysis_train_at = now
        self._last_incremental_train_at = now
        self._last_incremental_closed_count = clean_count

        version_id = str(summary.get("version_id", "")).strip()
        evaluation = summary.get("evaluation", {}) or {}
        precision = float(evaluation.get("precision", 0.0) or 0.0)
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

        approved = False
        activated = False
        auto_approve_reasons: list[str] = []
        if sample_count < min_rows:
            auto_approve_reasons.append(f"sample_count={sample_count} below min_rows={min_rows}")
        if precision < min_precision:
            auto_approve_reasons.append(f"precision={precision:.3f} below min_precision={min_precision:.3f}")
        if f1 < min_f1:
            auto_approve_reasons.append(f"f1={f1:.3f} below min_f1={min_f1:.3f}")
        if positive_count < min_positive_samples:
            auto_approve_reasons.append(
                f"positive_samples={positive_count} below min_positive_samples={min_positive_samples}"
            )
        champion_ok, champion_reason = await self._candidate_not_worse_than_active(evaluation=evaluation)
        if not champion_ok:
            auto_approve_reasons.append(champion_reason)

        if auto_approve and version_id and not auto_approve_reasons:
            await self.approve_model(version_id)
            activation = await self.activate_approved_model()
            approved = True
            activated = bool(activation.get("activated", False))

        return {
            "trained": True,
            "closed_count": clean_count,
            "version_id": version_id,
            "evaluation": evaluation,
            "positive_samples": positive_count,
            "approved": approved,
            "activated": activated,
            "auto_approve_reasons": auto_approve_reasons,
        }

    async def _candidate_not_worse_than_active(self, *, evaluation: dict) -> tuple[bool, str]:
        active = await self.model_registry.get_active_approved()
        if not active:
            return True, ""
        active_eval = active.get("evaluation_metrics_json", {}) or {}
        candidate_precision = float(evaluation.get("precision", 0.0) or 0.0)
        candidate_f1 = float(evaluation.get("f1", 0.0) or 0.0)
        active_precision = float(active_eval.get("precision", 0.0) or 0.0)
        active_f1 = float(active_eval.get("f1", 0.0) or 0.0)

        # Conservative gate: do not auto-promote candidates clearly weaker than champion.
        tolerance = 0.01
        if (candidate_precision + tolerance) < active_precision and (candidate_f1 + tolerance) < active_f1:
            return (
                False,
                "candidate weaker than approved model "
                f"(candidate p/f1={candidate_precision:.3f}/{candidate_f1:.3f}, "
                f"active p/f1={active_precision:.3f}/{active_f1:.3f})",
            )
        return True, ""
