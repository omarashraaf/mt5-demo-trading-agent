from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from domain.models import GeminiAssessment, MarketContext, TechnicalSignal, TradeQualityAssessment


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


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

        # US stocks: regular session only (safer default).
        if cat == "Stocks":
            if self._in_time_window(now_et, 9, 30, 16, 0):
                return True, ""
            return False, "US stocks are outside regular session (09:30-16:00 ET)."

        # Indices: US indices during US cash, GER40 during EU cash.
        if cat == "Indices":
            if any(token in sym for token in ("GER40", "DAX")):
                if now_berlin.weekday() < 5 and self._in_time_window(now_berlin, 9, 0, 17, 30):
                    return True, ""
                return False, "GER40 is outside European cash session (09:00-17:30 CET/CEST)."
            if any(token in sym for token in ("US30", "US100", "US500", "SPX500", "NAS100")):
                if self._in_time_window(now_et, 9, 30, 16, 0):
                    return True, ""
                return False, "US indices are outside US cash session (09:30-16:00 ET)."
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
        if signal.action not in {"BUY", "SELL"}:
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
        entry_price = tick.get("ask", 0.0) if signal.action == "BUY" else tick.get("bid", 0.0)
        rr_ratio = self._reward_risk_ratio(entry_price, signal.stop_loss, signal.take_profit)
        atr_pct = float(meta.get("atr_pct", 0.0))
        spread = float(tick.get("spread", 0.0))
        policy_min_rr = float(user_policy.get("min_reward_risk", 1.8))
        effective_min_rr = max(1.0, policy_min_rr)
        profile_threshold = profile.quality_threshold if profile else 0.75
        threshold = round(min(0.95, profile_threshold + self._mode_threshold_adjustment(user_policy) + threshold_boost), 2)

        h1_trend = meta.get("h1_trend", "flat")
        h4_trend = meta.get("h4_trend", "flat")
        action_bias = "bullish" if signal.action == "BUY" else "bearish"
        aligned = h1_trend == h4_trend == action_bias
        contradicting_structure = h1_trend != h4_trend and h1_trend != "flat" and h4_trend != "flat"

        trend_alignment_score = 0.92 if aligned else 0.45 if h1_trend == action_bias or h4_trend == action_bias else 0.18
        momentum_quality_score = _clamp(0.5 + float(meta.get("momentum_score", 0.0)) * 2.5)
        entry_timing_score = _clamp(0.45 + float(meta.get("entry_score", 0.0)) * 3.0)
        volatility_quality_score = self._volatility_score(atr_pct, profile.min_atr_pct if profile else 0.2)
        reward_risk_score = self._reward_risk_score(rr_ratio, effective_min_rr)
        spread_quality_score = self._spread_score(spread, profile.max_spread if profile else 20.0)
        news_alignment_score, contradiction_penalty = self._news_alignment(signal, gemini_assessment)

        no_trade_reasons: list[str] = []
        if contradicting_structure:
            no_trade_reasons.append("H1 and H4 structure disagree.")
        if not user_policy.get("allow_counter_trend_trades", False) and not aligned:
            no_trade_reasons.append("User policy blocks counter-trend or weakly aligned trades.")
        if profile and atr_pct < profile.min_atr_pct:
            no_trade_reasons.append(
                f"ATR regime too weak for {profile.profile_name} ({atr_pct:.2f}% < {profile.min_atr_pct:.2f}%)."
            )
        if profile and spread > profile.max_spread:
            no_trade_reasons.append(
                f"Spread too wide for {profile.profile_name} ({spread:.1f} > {profile.max_spread:.1f})."
            )
        if rr_ratio < effective_min_rr:
            no_trade_reasons.append(f"Reward:risk deteriorated to {rr_ratio:.2f}.")
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
            else f"High-quality {signal.action} setup with score {final_score:.2f}."
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
            "aggressive": -0.03,
        }.get(mode, 0.0)

    def _reward_risk_ratio(
        self,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> float:
        if entry_price <= 0 or stop_loss is None or take_profit is None:
            return 0.0
        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        if sl_distance <= 0:
            return 0.0
        return tp_distance / sl_distance

    def _volatility_score(self, atr_pct: float, min_atr_pct: float) -> float:
        if atr_pct <= 0:
            return 0.0
        if atr_pct >= min_atr_pct * 1.8:
            return 0.95
        return _clamp(atr_pct / max(min_atr_pct * 1.25, 0.01))

    def _reward_risk_score(self, rr_ratio: float, min_rr: float) -> float:
        if rr_ratio <= 0:
            return 0.0
        if rr_ratio >= min_rr * 1.3:
            return 0.95
        return _clamp(rr_ratio / max(min_rr * 1.15, 0.01))

    def _spread_score(self, spread: float, max_spread: float) -> float:
        if spread <= 0:
            return 0.0
        if spread >= max_spread:
            return 0.0
        return round(_clamp(1.0 - spread / max(max_spread, 1.0)), 3)

    def _news_alignment(
        self,
        signal: TechnicalSignal,
        assessment: GeminiAssessment | None,
    ) -> tuple[float, float]:
        if not assessment or not assessment.used or assessment.degraded:
            return 0.55, 0.0

        direction = "bullish" if signal.action == "BUY" else "bearish"
        base = 0.55
        if assessment.news_bias == direction:
            base = 0.82
        elif assessment.news_bias == "neutral":
            base = 0.5
        else:
            base = 0.18

        risk_penalty = {"low": 0.0, "medium": 0.04, "high": 0.10}[assessment.event_risk]
        contradiction_penalty = 0.12 if assessment.contradiction_flag else 0.0
        alignment = _clamp(base - risk_penalty)
        return alignment, contradiction_penalty

    def _is_choppy(self, meta: dict) -> bool:
        position_in_range = float(meta.get("position_in_range", 0.5))
        entry_signal = str(meta.get("entry_signal", "none"))
        return 0.43 <= position_in_range <= 0.57 and entry_signal in {"none", ""}

    def _is_late_entry(self, meta: dict) -> bool:
        ema_distance_atr = float(meta.get("ema_distance_atr", 0.0))
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
