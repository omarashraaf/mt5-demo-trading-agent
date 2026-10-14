from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    message: str
    local_ticket: int | None = None
    broker_ticket: int | None = None
    details: dict[str, Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "local_ticket": self.local_ticket,
            "broker_ticket": self.broker_ticket,
            "details": dict(self.details or {}),
        }


class ReconciliationService:
    """Read-only comparison between SQLite order records and broker positions."""

    def __init__(self, *, db=None, execution_engine=None):
        self.db = db
        self.execution_engine = execution_engine
        self._last_report: dict[str, Any] = {
            "checked_at": 0.0,
            "status": "unknown",
            "issues": [],
            "local_open_count": 0,
            "broker_open_count": 0,
        }

    def set_database(self, db) -> None:
        self.db = db

    def set_execution_engine(self, execution_engine) -> None:
        self.execution_engine = execution_engine

    def last_report(self) -> dict[str, Any]:
        return dict(self._last_report)

    async def run(self, *, limit: int = 300) -> dict[str, Any]:
        if self.db is None:
            report = self._report(status="unavailable", issues=[], local_rows=[], broker_positions=[], reason="database_unavailable")
            self._last_report = report
            return report
        local_rows = await self.db.get_recent_successful_orders(limit=limit)
        broker_positions = []
        if self.execution_engine is not None and hasattr(self.execution_engine, "get_positions"):
            try:
                broker_positions = list(self.execution_engine.get_positions() or [])
            except Exception as exc:
                report = self._report(
                    status="error",
                    issues=[ReconciliationIssue("broker_positions_unavailable", "error", "Could not read broker open positions.", details={"error": str(exc)})],
                    local_rows=local_rows,
                    broker_positions=[],
                )
                self._last_report = report
                return report

        issues = self._compare(local_rows=local_rows, broker_positions=broker_positions)
        status = "ok" if not issues else "mismatch"
        report = self._report(status=status, issues=issues, local_rows=local_rows, broker_positions=broker_positions)
        self._last_report = report
        return report

    def _compare(self, *, local_rows: list[dict], broker_positions: list[Any]) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        broker_by_ticket = {
            int(getattr(pos, "ticket", 0) or 0): pos
            for pos in broker_positions
            if int(getattr(pos, "ticket", 0) or 0) > 0
        }
        local_by_ticket = {
            int(row.get("ticket") or 0): row
            for row in local_rows
            if int(row.get("ticket") or 0) > 0
        }
        for ticket, row in local_by_ticket.items():
            pos = broker_by_ticket.get(ticket)
            if pos is None:
                issues.append(
                    ReconciliationIssue(
                        "local_order_missing_in_broker_open_positions",
                        "warning",
                        "Local successful order is not present in broker open positions.",
                        local_ticket=ticket,
                        details={"symbol": row.get("symbol"), "action": row.get("action"), "created_at": row.get("timestamp")},
                    )
                )
                continue
            self._compare_position_fields(ticket=ticket, row=row, pos=pos, issues=issues)

        for ticket, pos in broker_by_ticket.items():
            if ticket not in local_by_ticket:
                issues.append(
                    ReconciliationIssue(
                        "broker_position_missing_local_order",
                        "warning",
                        "Broker open position has no matching recent local order record.",
                        broker_ticket=ticket,
                        details={"symbol": getattr(pos, "symbol", ""), "type": getattr(pos, "type", ""), "volume": getattr(pos, "volume", None)},
                    )
                )
        return issues

    def _compare_position_fields(self, *, ticket: int, row: dict, pos: Any, issues: list[ReconciliationIssue]) -> None:
        local_symbol = str(row.get("symbol") or "").upper()
        broker_symbol = str(getattr(pos, "symbol", "") or "").upper()
        if local_symbol and broker_symbol and local_symbol != broker_symbol:
            issues.append(ReconciliationIssue("symbol_mismatch", "error", "Local order symbol differs from broker position symbol.", local_ticket=ticket, broker_ticket=ticket, details={"local": local_symbol, "broker": broker_symbol}))
        local_action = str(row.get("action") or "").upper()
        broker_action = str(getattr(pos, "type", "") or "").upper()
        if local_action and broker_action and local_action != broker_action:
            issues.append(ReconciliationIssue("side_mismatch", "error", "Local order side differs from broker position side.", local_ticket=ticket, broker_ticket=ticket, details={"local": local_action, "broker": broker_action}))
        local_volume = self._num(row.get("volume"))
        broker_volume = self._num(getattr(pos, "volume", 0.0))
        if local_volume > 0 and broker_volume > 0 and abs(local_volume - broker_volume) > 1e-8:
            issues.append(ReconciliationIssue("volume_mismatch", "warning", "Local order volume differs from broker position volume.", local_ticket=ticket, broker_ticket=ticket, details={"local": local_volume, "broker": broker_volume}))
        for field, attr, code in (("stop_loss", "stop_loss", "stop_loss_mismatch"), ("take_profit", "take_profit", "take_profit_mismatch")):
            local_value = self._num(row.get(field))
            broker_value = self._num(getattr(pos, attr, 0.0))
            if local_value > 0 and broker_value > 0 and abs(local_value - broker_value) > max(1e-8, abs(local_value) * 1e-6):
                issues.append(ReconciliationIssue(code, "warning", f"Local {field} differs from broker position.", local_ticket=ticket, broker_ticket=ticket, details={"local": local_value, "broker": broker_value}))

    def _report(self, *, status: str, issues: list[ReconciliationIssue], local_rows: list[dict], broker_positions: list[Any], reason: str = "") -> dict[str, Any]:
        return {
            "checked_at": time.time(),
            "status": status,
            "reason": reason,
            "issue_count": len(issues),
            "issues": [issue.model_dump() for issue in issues],
            "local_open_count": len(local_rows),
            "broker_open_count": len(broker_positions),
        }

    def _num(self, value: Any) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return 0.0
        return out
