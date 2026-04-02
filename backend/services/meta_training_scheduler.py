from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional


logger = logging.getLogger(__name__)


class MetaTrainingScheduler:
    """Background trainer that learns from closed trade history (wins + losses)."""

    def __init__(
        self,
        *,
        db,
        research_cycle_service,
        enabled: bool = True,
        interval_seconds: int = 900,
        min_closed_trades: int = 30,
        auto_approve: bool = True,
        min_precision: float = 0.50,
        min_f1: float = 0.45,
    ):
        self.db = db
        self.research_cycle_service = research_cycle_service
        self.enabled = bool(enabled)
        self.interval_seconds = int(interval_seconds)
        self.min_closed_trades = int(min_closed_trades)
        self.auto_approve = bool(auto_approve)
        self.min_precision = float(min_precision)
        self.min_f1 = float(min_f1)

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_error: str | None = None
        self._last_run_at: float = 0.0
        self._last_train_at: float = 0.0
        self._last_trained_closed_count: int = 0

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def status_snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "min_closed_trades": self.min_closed_trades,
            "auto_approve": self.auto_approve,
            "min_precision": self.min_precision,
            "min_f1": self.min_f1,
            "last_run_at": self._last_run_at,
            "last_train_at": self._last_train_at,
            "last_trained_closed_count": self._last_trained_closed_count,
            "last_error": self._last_error,
        }

    def start(self):
        if not self.enabled:
            logger.info("Meta training scheduler disabled by config.")
            return
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="meta_training_scheduler")
        logger.info("Meta training scheduler started.")

    async def stop(self):
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("Meta training scheduler stopped.")

    async def _run_loop(self):
        first_cycle = True
        while self._running:
            if first_cycle:
                # Avoid startup DB contention with UI/API requests.
                await asyncio.sleep(min(30, max(5, self.interval_seconds // 3)))
                first_cycle = False
            self._last_run_at = time.time()
            try:
                await self._maybe_train_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime guard
                self._last_error = str(exc)
                logger.exception("Meta training scheduler cycle failed: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    async def _maybe_train_once(self):
        if self.db is None or self.research_cycle_service is None:
            return

        closed_count = await self.db.count_trade_outcomes()
        if closed_count < self.min_closed_trades:
            logger.debug(
                "Meta training skipped: closed trades %s < min %s",
                closed_count,
                self.min_closed_trades,
            )
            return
        if closed_count <= self._last_trained_closed_count:
            return

        logger.info(
            "Meta training triggered from trade history: closed=%s (last_trained=%s)",
            closed_count,
            self._last_trained_closed_count,
        )
        summary = await self.research_cycle_service.train_candidate_model(
            algorithm="logistic_regression",
            target_column="profitable_after_costs_90m",
            include_unexecuted=False,  # learn only from real executed outcomes
            min_rows=self.min_closed_trades,
        )
        version_id = str(summary.get("version_id", "")).strip()
        evaluation = summary.get("evaluation", {}) or {}
        precision = float(evaluation.get("precision", 0.0) or 0.0)
        f1 = float(evaluation.get("f1", 0.0) or 0.0)
        sample_count = int(evaluation.get("sample_count", 0) or 0)

        approved = False
        activated = False
        if self.auto_approve and version_id:
            if sample_count >= self.min_closed_trades and precision >= self.min_precision and f1 >= self.min_f1:
                await self.research_cycle_service.approve_model(version_id)
                activation = await self.research_cycle_service.activate_approved_model()
                approved = True
                activated = bool(activation.get("activated"))
                logger.info(
                    "Meta model auto-approved and activation attempted: version=%s activated=%s precision=%.3f f1=%.3f",
                    version_id,
                    activated,
                    precision,
                    f1,
                )
            else:
                logger.info(
                    "Meta model candidate kept unapproved: version=%s sample=%s precision=%.3f f1=%.3f thresholds=(%.3f/%.3f)",
                    version_id,
                    sample_count,
                    precision,
                    f1,
                    self.min_precision,
                    self.min_f1,
                )

        self._last_train_at = time.time()
        self._last_trained_closed_count = closed_count
        logger.info(
            "Meta training cycle complete: version=%s approved=%s activated=%s closed_count=%s",
            version_id or "n/a",
            approved,
            activated,
            closed_count,
        )

