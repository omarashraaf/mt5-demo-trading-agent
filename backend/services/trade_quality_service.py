from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from domain.models import GeminiAssessment, MarketContext, TechnicalSignal, TradeQualityAssessment


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    if not math.isfinite(parsed):
        return minimum
    return max(minimum, min(maximum, parsed))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


class TradeQualityService:
    def session_allowed(
        self,
        sessions: list[str],
        *,
        symbol: str = "",
        category: str = "",
        profile_name: str = "",
    ) -> bool:
        market_open, _ = self.is_market_open(symbol=symbol, category=category, profile_name=profile_name)
        return self._session_allowed(sessions) and market_open

    def is_market_open(
        self,
        *,
        symbol: str = "",
        category: str = "",
        profile_name: str = "",
    ) -> tuple[bool, str]:
        sym = (symbol or "").upper()
        cat = (category or "").title()
        profile = (profile_name or "").lower()

        now_et = datetime.now(ZoneInfo("America/New_York"))
        now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))

        # Weekend hard block for these assets.
        if cat in {"Stocks", "Indices", "Commodities"}:
            if now_et.weekday() >= 5:
                return False, "Market closed (weekend)."

        # US stocks: allow extended session (pre/after market) so dashboard rows
        # can transition out of "closed" as soon as premarket opens.
        if cat == "Stocks":
            if self._in_time_window(now_et, 4, 0, 20, 0):
                return True, ""
            return False, "US stocks are outside extended session (04:00-20:00 ET)."

        # Indices: US indices during US cash, GER40 during EU cash.
        if cat == "Indices":
            if any(token in sym for token in ("GER40", "DAX")):
                if now_berlin.weekday() < 5 and self._in_time_window(now_berlin, 9, 0, 17, 30):
                    return True, ""
                return False, "GER40 is outside European cash session (09:00-17:30 CET/CEST)."
            if any(token in sym for token in ("US30", "US100", "US500", "SPX500", "NAS100")):
                if self._in_time_window(now_et, 4, 0, 20, 0):
                    return True, ""
                return False, "US indices are outside extended session (04:00-20:00 ET)."
            # Unknown index fallback: keep conservative US cash window.
            if self._in_time_window(now_et, 9, 30, 16, 0):
                return True, ""
            return False, "Index is outside the configured cash session window."

        # Commodities: keep XAU/XAG/WTI/BRENT available almost all weekday hours,
        # excluding the common daily rollover break 17:00-18:00 ET.
        if cat == "Commodities" or "commodity" in profile or any(token in sym for token in ("XAU", "XAG", "WTI", "BRENT", "USOIL", "UKOIL", "GOLD", "SILV")):
            if self._in_time_window(now_et, 17, 0, 18, 0):
                return False, "Commodities are in daily rollover break (17:00-18:00 ET)."
            return True, ""

        return True, ""

    def assess(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        portfolio_fit_score: float,
        threshold_boost: float = 0.0,
    ) -> TradeQualityAssessment:
        action = str(signal.action or "").strip().upper()
        if action not in {"BUY", "SELL"}:
            summary = signal.reason or "No executable setup."
            return TradeQualityAssessment(
                final_trade_quality_score=0.0,
                threshold=1.0,
                no_trade_zone=True,
                no_trade_reasons=[summary],
                summary=summary,
            )

        profile = context.profile
        user_policy = context.user_policy or {}
        tick = context.tick or {}
        meta = signal.metadata or {}
        entry_price = _safe_float(tick.get("ask", 0.0) if action == "BUY" else tick.get("bid", 0.0), 0.0)
        raw_spread = tick.get("spread", 0.0)
        spread_points = _safe_float(raw_spread, float("nan"))
        spread_valid = math.isfinite(spread_points) and spread_points >= 0.0
        symbol_point = _safe_float(context.symbol_info.point, 0.0) if context.symbol_info and context.symbol_info.point else 0.0
        rr_ratio = self._reward_risk_ratio(
            entry_price,
            signal.stop_loss,
            signal.take_profit,
            spread_points=spread_points if spread_valid else 0.0,
            point=symbol_point,
        )
        targets_valid = self._directionally_valid_targets(
            entry_price,
            action,
            signal.stop_loss,
            signal.take_profit,
        )
        atr_pct = _safe_float(meta.get("atr_pct", 0.0), 0.0)
        policy_min_rr = _safe_float(user_policy.get("min_reward_risk", 1.8), 1.8)
        effective_min_rr = max(0.8, policy_min_rr)
        profile_threshold = profile.quality_threshold if profile else 0.75
        threshold = round(min(0.95, profile_threshold + self._mode_threshold_adjustment(user_policy) + threshold_boost), 2)
        mode = str(user_policy.get("mode", "balanced")).lower()
        spread_mode_multiplier = {
            "safe": 0.9,
            "balanced": 1.0,
            "aggressive": 2.0,
        }.get(mode, 1.0)
        profile_spread_limit_points = (
            self._spread_limit_points(profile.max_spread, symbol_point)
            if profile
            else 20.0
        )
        profile_spread_limit_points *= spread_mode_multiplier

        h1_trend = meta.get("h1_trend", "flat")
        h4_trend = meta.get("h4_trend", "flat")
        action_bias = "bullish" if action == "BUY" else "bearish"
        aligned = h1_trend == h4_trend == action_bias
        contradicting_structure = h1_trend != h4_trend and h1_trend != "flat" and h4_trend != "flat"

        trend_alignment_score = 0.92 if aligned else 0.45 if h1_trend == action_bias or h4_trend == action_bias else 0.18
        momentum_quality_score = _clamp(0.5 + _safe_float(meta.get("momentum_score", 0.0), 0.0) * 2.5)
        entry_timing_score = _clamp(0.45 + _safe_float(meta.get("entry_score", 0.0), 0.0) * 3.0)
        volatility_quality_score = self._volatility_score(atr_pct, profile.min_atr_pct if profile else 0.2)
        reward_risk_score = self._reward_risk_score(rr_ratio, effective_min_rr)
        spread_quality_score = self._spread_score(spread_points, profile_spread_limit_points)
        news_alignment_score, contradiction_penalty = self._news_alignment(
            context=context,
            signal=signal,
            assessment=gemini_assessment,
        )

        no_trade_reasons: list[str] = []
        if contradicting_structure and mode != "aggressive":
            no_trade_reasons.append("H1 and H4 structure disagree.")
        if not user_policy.get("allow_counter_trend_trades", False) and not aligned:
            no_trade_reasons.append("User policy blocks counter-trend or weakly aligned trades.")
        if entry_price <= 0.0:
            no_trade_reasons.append("No valid live entry price is available for trade-quality assessment.")
        if profile and atr_pct < profile.min_atr_pct:
            no_trade_reasons.append(
                f"ATR regime too weak for {profile.profile_name} ({atr_pct:.2f}% < {profile.min_atr_pct:.2f}%)."
            )
        if not spread_valid:
            no_trade_reasons.append("Spread is not available or invalid for trade-quality assessment.")
        elif profile and spread_points > profile_spread_limit_points:
            spread_price = spread_points * symbol_point if symbol_point > 0 else spread_points
            no_trade_reasons.append(
                f"Spread too wide for {profile.profile_name} ({spread_points:.1f} pts / {spread_price:.3f} > {profile.max_spread:.3f})."
            )
        stop_loss = _safe_float(signal.stop_loss, 0.0)
        take_profit = _safe_float(signal.take_profit, 0.0)
        if stop_loss <= 0.0 or take_profit <= 0.0:
            no_trade_reasons.append("Protective stop-loss and take-profit are required before execution.")
        elif not targets_valid:
            no_trade_reasons.append(
                f"Invalid {action} targets: stop-loss and take-profit are not positioned around live entry."
            )
        if rr_ratio < effective_min_rr:
            no_trade_reasons.append(f"Cost-adjusted reward:risk deteriorated to {rr_ratio:.2f}.")
        if self._is_choppy(meta):
            no_trade_reasons.append("Price is too close to chop/mean without clean structure.")
        if self._is_late_entry(meta):
            no_trade_reasons.append("Trend exists, but entry timing is late or extended.")
        session_filters = list(user_policy.get("session_filters") or (profile.allowed_sessions if profile else []))
        if context.evaluation_mode in {"auto", "scan", "replay"}:
            session_ok = self._session_allowed(session_filters)
            market_ok, market_reason = self.is_market_open(
                symbol=context.symbol,
                category=profile.category if profile else (context.symbol_info.category if context.symbol_info else ""),
                profile_name=profile.profile_name if profile else "",
            )
            if not market_ok:
                no_trade_reasons.append(market_reason or "Market is currently closed for this asset.")
            elif not session_ok:
                no_trade_reasons.append("Outside the preferred session window for this profile.")
        if gemini_assessment and gemini_assessment.event_risk == "high" and gemini_assessment.news_bias == "neutral":
            no_trade_reasons.append("Event risk is high while news direction is unclear.")

        weighted_total = (
            trend_alignment_score * 0.18
            + momentum_quality_score * 0.14
            + entry_timing_score * 0.14
            + volatility_quality_score * 0.10
            + reward_risk_score * 0.14
            + spread_quality_score * 0.10
            + _clamp(portfolio_fit_score) * 0.10
            + news_alignment_score * 0.10
        ) - contradiction_penalty
        final_score = round(_clamp(weighted_total), 3)

        no_trade_zone = bool(no_trade_reasons) or final_score < threshold
        if final_score < threshold:
            no_trade_reasons.append(
                f"Trade quality {final_score:.2f} is below the required threshold {threshold:.2f}."
            )

        summary = (
            "Hold the setup."
            if no_trade_zone
            else f"High-quality {action} setup with score {final_score:.2f}."
        )

        return TradeQualityAssessment(
            trend_alignment_score=round(trend_alignment_score, 3),
            momentum_quality_score=round(momentum_quality_score, 3),
            entry_timing_score=round(entry_timing_score, 3),
            volatility_quality_score=round(volatility_quality_score, 3),
            reward_risk_score=round(reward_risk_score, 3),
            spread_quality_score=round(spread_quality_score, 3),
            portfolio_fit_score=round(_clamp(portfolio_fit_score), 3),
            news_alignment_score=round(news_alignment_score, 3),
            contradiction_penalty=round(contradiction_penalty, 3),
            final_trade_quality_score=final_score,
            threshold=threshold,
            no_trade_zone=no_trade_zone,
            no_trade_reasons=no_trade_reasons,
            summary=summary,
        )

    def _mode_threshold_adjustment(self, user_policy: dict) -> float:
        mode = str(user_policy.get("mode", "")).lower()
        return {
            "safe": 0.05,
            "balanced": 0.0,
            "aggressive": -0.08,
        }.get(mode, 0.0)

    def _reward_risk_ratio(
        self,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        *,
        spread_points: float = 0.0,
        point: float = 0.0,
    ) -> float:
        if entry_price <= 0 or stop_loss is None or take_profit is None:
            return 0.0
        entry = _safe_float(entry_price, 0.0)
        sl = _safe_float(stop_loss, 0.0)
        tp = _safe_float(take_profit, 0.0)
        spread = _safe_float(spread_points, 0.0)
        point_value = _safe_float(point, 0.0)
        if entry <= 0 or sl <= 0 or tp <= 0:
            return 0.0
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)
        spread_cost = max(0.0, spread) * point_value if point_value > 0 else 0.0
        adjusted_risk = sl_distance + spread_cost
        adjusted_reward = max(0.0, tp_distance - spread_cost)
        if adjusted_risk <= 0:
            return 0.0
        return adjusted_reward / adjusted_risk

    def _directionally_valid_targets(
        self,
        entry_price: float,
        action: str,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> bool:
        action = str(action or "").strip().upper()
        if entry_price <= 0 or stop_loss is None or take_profit is None:
            return False
        entry = _safe_float(entry_price, 0.0)
        sl = _safe_float(stop_loss, 0.0)
        tp = _safe_float(take_profit, 0.0)
        if entry <= 0 or sl <= 0 or tp <= 0:
            return False
        if action == "BUY":
            return sl < entry < tp
        if action == "SELL":
            return tp < entry < sl
        return False

    def _volatility_score(self, atr_pct: float, min_atr_pct: float) -> float:
        parsed_atr_pct = _safe_float(atr_pct, 0.0)
        parsed_min_atr_pct = _safe_float(min_atr_pct, 0.0)
        if parsed_atr_pct <= 0 or parsed_min_atr_pct <= 0:
            return 0.0
        if parsed_atr_pct >= parsed_min_atr_pct * 1.8:
            return 0.95
        return _clamp(parsed_atr_pct / max(parsed_min_atr_pct * 1.25, 0.01))

    def _reward_risk_score(self, rr_ratio: float, min_rr: float) -> float:
        parsed_rr_ratio = _safe_float(rr_ratio, 0.0)
        parsed_min_rr = _safe_float(min_rr, 0.0)
        if parsed_rr_ratio <= 0 or parsed_min_rr <= 0:
            return 0.0
        if parsed_rr_ratio >= parsed_min_rr * 1.3:
            return 0.95
        return _clamp(parsed_rr_ratio / max(parsed_min_rr * 1.15, 0.01))

    def _spread_score(self, spread: float, max_spread: float) -> float:
        parsed_spread = _safe_float(spread, 0.0)
        parsed_max_spread = _safe_float(max_spread, 0.0)
        if parsed_spread <= 0 or parsed_max_spread <= 0:
            return 0.0
        if parsed_spread >= parsed_max_spread:
            return 0.0
        return round(_clamp(1.0 - parsed_spread / max(parsed_max_spread, 1.0)), 3)

    def _spread_limit_points(self, max_spread: float, point: float) -> float:
        # MT5 tick spread is in points, while profile limits are maintained in
        # price terms. Convert to points before comparison.
        max_spread_value = _safe_float(max_spread, 0.0)
        point_value = _safe_float(point, 0.0)
        if max_spread_value <= 0:
            return 0.0
        if point_value <= 0:
            return max_spread_value
        return max_spread_value / point_value

    def _news_alignment(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        assessment: GeminiAssessment | None,
    ) -> tuple[float, float]:
        if not assessment or not assessment.used or assessment.degraded:
            return self._event_alignment_from_context(context, signal)

        action = str(signal.action or "").strip().upper()
        direction = "bullish" if action == "BUY" else "bearish"
        base = 0.55
        if assessment.news_bias == direction:
            base = 0.82
        elif assessment.news_bias == "neutral":
            base = 0.5
        else:
            base = 0.18

        risk_penalty = {"low": 0.0, "medium": 0.04, "high": 0.10}.get(
            str(assessment.event_risk).lower(),
            0.10,
        )
        contradiction_penalty = 0.12 if assessment.contradiction_flag else 0.0
        alignment = _clamp(base - risk_penalty)
        return alignment, contradiction_penalty

    def _event_alignment_from_context(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
    ) -> tuple[float, float]:
        if not context.normalized_news:
            return 0.55, 0.0

        action = str(signal.action or "").strip().upper()
        direction = "bullish" if action == "BUY" else "bearish"
        weights = {"high": 1.0, "medium": 0.6, "low": 0.3}
        risk_order = {"low": 0, "medium": 1, "high": 2}
        bias_score = 0.0
        contradiction = 0.0
        total_w = 0.0
        strongest_risk = "low"

        for item in context.normalized_news[:8]:
            meta = item.metadata or {}
            bias = str(meta.get("gemini_bias", "neutral")).lower()
            event_risk = str(meta.get("gemini_event_risk", "low")).lower()
            if event_risk not in {"low", "medium", "high"}:
                event_risk = "low"
            w = weights.get(event_risk, 0.3)
            total_w += w
            if bias == direction:
                bias_score += 1.0 * w
            elif bias == "neutral":
                bias_score += 0.0
            else:
                bias_score -= 1.0 * w
                contradiction += 0.10 * w
            if risk_order[event_risk] > risk_order[strongest_risk]:
                strongest_risk = event_risk

        if total_w <= 0:
            return 0.55, 0.0

        normalized_bias = bias_score / total_w  # -1..1
        alignment = _clamp(0.55 + normalized_bias * 0.30)
        risk_penalty = {"low": 0.0, "medium": 0.03, "high": 0.08}.get(strongest_risk, 0.0)
        return _clamp(alignment - risk_penalty), _clamp(contradiction, 0.0, 0.18)

    def _is_choppy(self, meta: dict) -> bool:
        position_in_range = _safe_float(meta.get("position_in_range", 0.5), 0.5)
        entry_signal = str(meta.get("entry_signal", "none"))
        return 0.43 <= position_in_range <= 0.57 and entry_signal in {"none", ""}

    def _is_late_entry(self, meta: dict) -> bool:
        ema_distance_atr = _safe_float(meta.get("ema_distance_atr", 0.0), 0.0)
        return ema_distance_atr >= 1.1

    def _session_allowed(self, sessions: list[str]) -> bool:
        if not sessions or "24/7" in sessions:
            return True
        hour = datetime.now(timezone.utc).hour
        windows = {
            "London": range(7, 16),
            "Europe": range(7, 14),
            "New York": range(12, 21),
            "US Open": range(13, 17),
            "US Midday": range(17, 20),
            "Overlap": range(12, 16),
        }
        return any(hour in windows.get(session, range(0)) for session in sessions)

    def _in_time_window(
        self,
        dt: datetime,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> bool:
        now_minutes = dt.hour * 60 + dt.minute
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        return start <= now_minutes < end
