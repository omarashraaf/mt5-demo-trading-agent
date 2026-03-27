from __future__ import annotations

import time
from typing import Optional


ALLOWED_MODEL_STATUSES = {"training", "candidate", "approved", "rejected", "archived"}


class ModelRegistry:
    def __init__(self, db):
        self.db = db

    async def register_candidate(
        self,
        *,
        version_id: str,
        algorithm: str,
        target_definition: str,
        feature_schema_version: str,
        training_date: float,
        data_range_start: Optional[float],
        data_range_end: Optional[float],
        evaluation_metrics: dict,
        notes: str = "",
    ) -> int:
        return await self.db.register_model_version(
            version_id=version_id,
            algorithm=algorithm,
            target_definition=target_definition,
            feature_schema_version=feature_schema_version,
            training_date=training_date,
            data_range_start=data_range_start,
            data_range_end=data_range_end,
            evaluation_metrics=evaluation_metrics,
            walk_forward_metrics={},
            approval_status="candidate",
            notes=notes,
        )

    async def set_status(
        self,
        *,
        version_id: str,
        status: str,
        notes: str = "",
    ) -> dict:
        normalized = status.strip().lower()
        if normalized not in ALLOWED_MODEL_STATUSES:
            raise ValueError(f"Unsupported model status: {status}")

        if normalized == "approved":
            await self._approve_single_version(version_id=version_id)
        else:
            await self.db.update_model_version(
                version_id=version_id,
                approval_status=normalized,
                notes=notes or None,
            )
        updated = await self.db.get_model_version(version_id)
        if updated is None:
            raise ValueError(f"Model version not found: {version_id}")
        return updated

    async def _approve_single_version(self, *, version_id: str):
        versions = await self.db.list_model_versions(limit=1000)
        found = False
        for item in versions:
            current_version_id = item.get("version_id")
            if current_version_id == version_id:
                found = True
                await self.db.update_model_version(
                    version_id=current_version_id,
                    approval_status="approved",
                    notes=f"Manually approved at {time.time():.0f}",
                )
            elif item.get("approval_status") == "approved":
                await self.db.update_model_version(
                    version_id=current_version_id,
                    approval_status="archived",
                    notes="Archived due to newer approved model.",
                )
        if not found:
            raise ValueError(f"Model version not found: {version_id}")

    async def get_active_approved(self) -> Optional[dict]:
        return await self.db.get_latest_approved_model_version()

    async def list_versions(self, limit: int = 50) -> list[dict]:
        return await self.db.list_model_versions(limit=limit)
