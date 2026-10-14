from __future__ import annotations

import time
from dataclasses import asdict, dataclass


KILL_SWITCH_RUNTIME_KEY = "trading_kill_switch"


@dataclass(frozen=True)
class KillSwitchState:
    enabled: bool = False
    reason: str = ""
    set_by: str = ""
    set_at: float = 0.0

    def model_dump(self) -> dict:
        return asdict(self)


class KillSwitchService:
    def __init__(self, db=None):
        self.db = db
        self._state = KillSwitchState()

    def set_database(self, db) -> None:
        self.db = db

    def current_state(self) -> KillSwitchState:
        return self._state

    async def load(self) -> KillSwitchState:
        if self.db is None:
            return self._state
        payload = await self.db.get_runtime_state(KILL_SWITCH_RUNTIME_KEY)
        if isinstance(payload, dict):
            self._state = KillSwitchState(
                enabled=bool(payload.get("enabled", False)),
                reason=str(payload.get("reason", "") or ""),
                set_by=str(payload.get("set_by", "") or ""),
                set_at=float(payload.get("set_at", 0.0) or 0.0),
            )
        return self._state

    async def enable(self, *, reason: str, set_by: str) -> KillSwitchState:
        self._state = KillSwitchState(
            enabled=True,
            reason=str(reason or "Trading kill switch enabled.").strip(),
            set_by=str(set_by or "unknown").strip(),
            set_at=time.time(),
        )
        await self._persist()
        return self._state

    async def disable(self, *, set_by: str, reason: str = "") -> KillSwitchState:
        self._state = KillSwitchState(
            enabled=False,
            reason=str(reason or "Trading kill switch disabled.").strip(),
            set_by=str(set_by or "unknown").strip(),
            set_at=time.time(),
        )
        await self._persist()
        return self._state

    async def _persist(self) -> None:
        if self.db is None:
            return
        await self.db.save_runtime_state(KILL_SWITCH_RUNTIME_KEY, self._state.model_dump())
