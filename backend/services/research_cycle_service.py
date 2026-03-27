from __future__ import annotations

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
        target_column: str = "profitable_after_costs_90m",
        include_unexecuted: bool = True,
        min_rows: int = 30,
    ) -> dict:
        return await self.model_trainer.train_candidate_model(
            algorithm=algorithm,
            target_column=target_column,
            include_unexecuted=include_unexecuted,
            min_rows=min_rows,
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

    async def approve_model(self, version_id: str) -> dict:
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
        spread_buckets = {"tight": {"count": 0, "pnl": 0.0}, "normal": {"count": 0, "pnl": 0.0}, "wide": {"count": 0, "pnl": 0.0}}
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
            pnl = float(row.get("profit", 0.0) or 0.0)
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
        active_approved = await self.db.get_latest_approved_model_version()
        latest_training_run = await self.db.get_latest_model_run()
        latest_replay_run = await self.db.get_latest_replay_run()
        latest_walk_forward = await self.db.get_latest_replay_run(notes_like="walk_forward%")
        recent_reports = await self.db.list_attribution_reports(limit=5)
        versions = await self.db.list_model_versions(limit=20)
        candidates = [v for v in versions if v.get("approval_status") == "candidate"]
        best_candidate = candidates[0] if candidates else None
        if self.meta_model_service is not None:
            await self.meta_model_service.refresh_active_model()
        return {
            "meta_model_active": bool(self.meta_model_service.is_active) if self.meta_model_service else False,
            "meta_model_active_version": self.meta_model_service.active_version_id if self.meta_model_service else "",
            "active_approved_model": active_approved,
            "last_training_run": latest_training_run,
            "last_replay_run": latest_replay_run,
            "last_walk_forward_run": latest_walk_forward,
            "best_candidate_model": best_candidate,
            "recent_attribution_reports": recent_reports,
        }
