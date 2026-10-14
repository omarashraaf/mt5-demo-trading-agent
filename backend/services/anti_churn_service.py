from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

from domain.models import AntiChurnAssessment, MarketContext, TechnicalSignal, TradeQualityAssessment

NEAR_ENTRY_RECOVERY_PCT = 0.0015  # 0.15% around entry.


class AntiChurnService:
    def __init__(self):
        self._scan_window_entries: dict[str, set[str]] = defaultdict(set)

    def begin_scan_window(self, scan_window_id: str):
        self._scan_window_entries[scan_window_id] = set()

    def mark_symbol_opened(self, scan_window_id: str | None, symbol: str):
        if not scan_window_id:
            return
        seen = self._scan_window_entries.setdefault(scan_window_id, set())
        seen.add(symbol.upper())

    def assess(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        trade_quality: TradeQualityAssessment,
        cooldown_minutes: int,
        max_trades_per_symbol: int = 1,
        recent_outcomes: list[dict] | None = None,
        recent_evaluations: list[dict] | None = None,
        scan_window_id: Optional[str] = None,
    ) -> AntiChurnAssessment:
        if signal.action not in {"BUY", "SELL"}:
            return AntiChurnAssessment()

        reasons: list[str] = []
        threshold_boost = 0.0
        symbol = context.symbol.upper()
        direction = str(signal.action or "").upper()
        hedge_opposite_loser = self._is_opposite_hedge_candidate(
            positions=context.symbol_open_positions,
            direction=direction,
        )

        # Do not pyramid into the same direction while that symbol is already losing.
        # This avoids repeated averaging/add-on entries when market structure is invalidating.
        if context.symbol_open_positions:
            losing_same_direction_open = []
            for pos in context.symbol_open_positions:
                pos_dir = self._normalize_direction(pos.get("type") or pos.get("action"))
                pos_profit = self._position_profit(pos)
                if (
                    pos_dir == direction
                    and pos_profit is not None
                    and pos_profit < 0
                    and not self._is_position_near_entry_recovery(pos)
                ):
                    losing_same_direction_open.append(pos)
            if losing_same_direction_open:
                reasons.append(
                    f"{symbol} already has {direction} position(s) in loss; add-on entry is blocked until recovery near entry or exit."
                )

        if scan_window_id and not hedge_opposite_loser:
            seen = self._scan_window_entries.setdefault(scan_window_id, set())
            if symbol in seen:
                reasons.append("Symbol already opened once in the current scan window.")

        if recent_outcomes:
            if (not hedge_opposite_loser) and len(recent_outcomes) >= max(max_trades_per_symbol, 1):
                reasons.append(
                    f"User policy caps {symbol} to {max_trades_per_symbol} trade(s) in the recent trading window."
                )
            if not hedge_opposite_loser:
                for outcome in recent_outcomes:
                    minutes_since = self._minutes_since(outcome.get("closed_at") or outcome.get("timestamp"))
                    if minutes_since is not None and minutes_since < cooldown_minutes:
                        reasons.append(f"Recent {symbol} exit is still inside cooldown ({minutes_since:.0f}m ago).")
                        break

            failed_same_side = [
                outcome for outcome in recent_outcomes[:2]
                if self._outcome_profit(outcome) <= 0
                and self._normalize_direction(outcome.get("action")) == direction
            ]
            if len(failed_same_side) >= 1:
                threshold_boost += 0.05
            if len(failed_same_side) >= 2:
                threshold_boost += 0.03

            same_side_recent = [
                outcome for outcome in recent_outcomes[:4]
                if self._normalize_direction(outcome.get("action")) == direction
            ]
            same_side_losses = [outcome for outcome in same_side_recent if self._outcome_profit(outcome) < 0]
            if len(same_side_recent) >= 3 and len(same_side_losses) >= 3:
                reasons.append(
                    f"Recent {symbol} {direction} streak is underperforming ({len(same_side_losses)}/{len(same_side_recent)} losses); re-entry blocked."
                )

            last_outcome = recent_outcomes[0]
            if (
                (not hedge_opposite_loser)
                and
                last_outcome.get("action") in {"BUY", "SELL"}
                and last_outcome.get("action") != signal.action
                and self._minutes_since(last_outcome.get("closed_at") or last_outcome.get("timestamp")) is not None
                and self._minutes_since(last_outcome.get("closed_at") or last_outcome.get("timestamp")) < cooldown_minutes
                and trade_quality.trend_alignment_score < 0.88
            ):
                reasons.append("Rapid BUY/SELL flip-flop blocked without strong reversal quality.")

        if recent_evaluations:
            opened_only = [
                journal for journal in recent_evaluations
                if self._counts_as_opened_trade(journal)
            ]
            if (not hedge_opposite_loser) and len(opened_only) >= max(max_trades_per_symbol, 1):
                reasons.append(
                    f"{symbol} already hit the recent per-symbol trade cap in this policy."
                )
            strong_rejections = [
                journal for journal in recent_evaluations[:3]
                if float(journal.get("quality_score", 0.0)) < trade_quality.threshold
                and journal.get("executable_action") == signal.action
            ]
            if strong_rejections:
                threshold_boost += 0.03

        blocked = bool(reasons)
        if blocked and trade_quality.final_trade_quality_score >= trade_quality.threshold + 0.08:
            # Allow truly exceptional reversals to override churn blocks.
            reasons = [reason for reason in reasons if "flip-flop" not in reason]
            blocked = bool(reasons)

        return AntiChurnAssessment(
            blocked=blocked,
            threshold_boost=round(threshold_boost, 3),
            reasons=reasons,
            metadata={
                "recent_outcomes_checked": len(recent_outcomes or []),
                "recent_evaluations_checked": len(recent_evaluations or []),
            },
        )

    def _normalize_direction(self, value: object) -> str:
        raw = str(value or "").strip().upper()
        if raw in {"BUY", "SELL"}:
            return raw
        if raw in {"LONG"}:
            return "BUY"
        if raw in {"SHORT"}:
            return "SELL"
        return ""

    def _outcome_profit(self, outcome: dict) -> float:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            try:
                val = outcome.get(key)
                if val is not None:
                    return float(val)
            except Exception:
                continue
        return 0.0

    def _position_profit(self, position: dict) -> float | None:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            try:
                val = position.get(key)
                if val is not None:
                    return float(val)
            except Exception:
                continue
        return None

    def _is_position_near_entry_recovery(self, position: dict) -> bool:
        try:
            entry = float(position.get("price_open", 0.0) or 0.0)
            current = float(
                position.get("price_current", 0.0)
                or position.get("current_price", 0.0)
                or position.get("price", 0.0)
                or 0.0
            )
        except Exception:
            return False
        if entry <= 0 or current <= 0:
            return False
        distance_pct = abs(current - entry) / entry
        return distance_pct <= NEAR_ENTRY_RECOVERY_PCT

    def _is_opposite_hedge_candidate(self, *, positions: list[dict] | None, direction: str) -> bool:
        if direction not in {"BUY", "SELL"} or not positions:
            return False
        for pos in positions:
            pos_dir = self._normalize_direction(pos.get("type") or pos.get("action"))
            pos_profit = self._position_profit(pos)
            if pos_dir and pos_dir != direction and pos_profit is not None and pos_profit < 0:
                return True
        return False

    def _counts_as_opened_trade(self, journal: dict) -> bool:
        status = str(journal.get("outcome_status", "")).lower()
        if status in {"opened", "executed", "filled"}:
            return True
        # Do not infer "opened" from allow_execute. A signal may pass evaluation
        # but still fail preflight/order_send and must not consume per-symbol cap.
        return False

    def spread_deteriorated(
        self,
        reference_spread: float,
        current_spread: float,
        max_spread: float,
    ) -> bool:
        if current_spread <= 0:
            return True
        if current_spread > max_spread:
            return True
        if reference_spread <= 0:
            return False
        return current_spread > reference_spread * 1.35

    def clear_expired_scan_windows(self, max_age_minutes: int = 120):
        cutoff = time.time() - max_age_minutes * 60
        stale_ids = [
            key for key in self._scan_window_entries
            if self._scan_window_timestamp(key) < cutoff
        ]
        for key in stale_ids:
            self._scan_window_entries.pop(key, None)

    def _scan_window_timestamp(self, scan_window_id: str) -> float:
        try:
            return float(scan_window_id.split(":")[-1])
        except Exception:
            return time.time()

    def _minutes_since(self, timestamp: float | None) -> float | None:
        if not timestamp:
            return None
        try:
            parsed = float(timestamp)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed <= 0:
            return None
        return max(0.0, (time.time() - parsed) / 60.0)
