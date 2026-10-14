from __future__ import annotations

import asyncio
import logging
from typing import Optional


logger = logging.getLogger(__name__)


class CloudArchiveService:
    """Lightweight archive loop placeholder.

    The app lifecycle expects a start/stop capable background service here.
    Until full cloud archival behavior is restored, keep startup non-blocking
    and make the service a safe no-op when disabled or when archive work is
    not yet implemented.
    """

    def __init__(
        self,
        *,
        db,
        enabled: bool,
        interval_seconds: float = 300.0,
        retention_days: Optional[dict] = None,
        batch_sizes: Optional[dict] = None,
        max_batches_per_run: int = 1,
    ):
        self.db = db
        self.enabled = bool(enabled)
        self.interval_seconds = max(30.0, float(interval_seconds or 300.0))
        self.retention_days = dict(retention_days or {})
        self.batch_sizes = dict(batch_sizes or {})
        self.max_batches_per_run = max(1, int(max_batches_per_run or 1))
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if not self.enabled:
            logger.info("Cloud archive service disabled.")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="cloud-archive-service")
        logger.info(
            "Cloud archive service started (interval=%ss, max_batches=%s).",
            int(self.interval_seconds),
            self.max_batches_per_run,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Cloud archive service stopped.")

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._archive_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Cloud archive iteration failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _archive_once(self) -> None:
        # The historical archival implementation is not present in this tree.
        # Keep this as an intentionally safe no-op so backend startup remains
        # healthy while preserving the background-service contract.
        logger.debug(
            "Cloud archive heartbeat: retention=%s batch_sizes=%s",
            self.retention_days,
            self.batch_sizes,
        )
