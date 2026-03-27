from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel


class BackgroundServiceState(BaseModel):
    name: str
    desired_running: bool = False
    running: bool = False
    last_started_at: float = 0.0
    last_stopped_at: float = 0.0
    last_heartbeat_at: float = 0.0
    last_cycle_at: float = 0.0
    last_error: Optional[str] = None

    def mark_started(self):
        now = time.time()
        self.desired_running = True
        self.running = True
        self.last_started_at = now
        self.last_heartbeat_at = now
        self.last_error = None

    def mark_stopped(self):
        now = time.time()
        self.running = False
        self.desired_running = False
        self.last_stopped_at = now
        self.last_heartbeat_at = now

    def mark_heartbeat(self):
        self.last_heartbeat_at = time.time()

    def mark_cycle(self):
        now = time.time()
        self.last_cycle_at = now
        self.last_heartbeat_at = now

    def mark_error(self, error: Exception | str):
        self.last_error = str(error)
        self.last_heartbeat_at = time.time()

