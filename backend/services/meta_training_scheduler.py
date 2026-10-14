from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from typing import Callable, Optional


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
        min_positive_samples: int = 20,
        target_column: str = "profitable_after_costs_180m",
        should_skip: Optional[Callable[[], bool]] = None,
    ):
        self.db = db
        self.research_cycle_service = research_cycle_service
        self.enabled = bool(enabled)
        self.interval_seconds = int(interval_seconds)
        self.min_closed_trades = int(min_closed_trades)
        self.auto_approve = bool(auto_approve)
        self.min_precision = float(min_precision)
        self.min_f1 = float(min_f1)
        self.min_positive_samples = int(min_positive_samples)
        self.target_column = str(target_column or "profitable_after_costs_180m")
        self.should_skip = should_skip

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
            "min_positive_samples": self.min_positive_samples,
            "target_column": self.target_column,
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
                # Avoid startup DB contention with UI/API requests and live trading.
                await asyncio.sleep(max(60, self.interval_seconds))
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
        try:
            temp_free = shutil.disk_usage(tempfile.gettempdir()).free
            if temp_free < 256 * 1024 * 1024:
                self._last_error = "Meta training skipped: temp disk has less than 256MB free."
                logger.warning(self._last_error)
                return
        except Exception:
            pass
        if self.should_skip is not None:
            try:
                if bool(self.should_skip()):
                    logger.debug("Meta training skipped: runtime is busy.")
                    return
            except Exception:
                pass

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
        result = await self.research_cycle_service.maybe_train_from_new_trade_outcome(
            min_rows=self.min_closed_trades,
            cooldown_seconds=0,
            auto_approve=self.auto_approve,
            min_precision=self.min_precision,
            min_f1=self.min_f1,
            min_positive_samples=self.min_positive_samples,
            target_column=self.target_column,
        )
        version_id = str(result.get("version_id", "")).strip()
        approved = bool(result.get("approved", False))
        activated = bool(result.get("activated", False))
        if result.get("trained"):
            reasons = result.get("auto_approve_reasons", []) or []
            if approved:
                logger.info(
                    "Meta model auto-approved and activation attempted: version=%s activated=%s",
                    version_id,
                    activated,
                )
            else:
                logger.info(
                    "Meta model candidate kept unapproved: version=%s reasons=%s",
                    version_id or "n/a",
                    reasons,
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

