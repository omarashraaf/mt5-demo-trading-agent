from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from domain.models import MarketContext, TechnicalSignal


@dataclass
class _NewsScoreItem:
    sentiment: str
    impact: str
    relevance: str
    strength: float
    risk_flag: bool
    imminent: bool
    headline: str


class NewsEngine:
    """Deterministic news intelligence layer for confidence/risk adjustments.

    News is a supporting factor only:
    - It can adjust confidence.
    - It can force HOLD / block execution in high-risk conditions.
    - It never creates BUY/SELL by itself.
    """

    _IMPACT_W = {"low": 0.45, "medium": 0.72, "high": 1.0}
    _RELEVANCE_W = {"indirect": 0.65, "global": 0.85, "direct": 1.0}
    _SENTIMENT_W = {"neutral": 0.40, "bullish": 1.0, "bearish": 1.0}
    _MAJOR_EVENT_KEYWORDS = (
        "interest rate",
        "rate hike",
        "rate cut",
        "fomc",
        "fed",
        "ecb",
        "boe",
        "cpi",
        "inflation",
        "nfp",
        "nonfarm",
        "gdp",
        "powell",
        "unexpected",
        "breaking",
        "attack",
        "war",
        "sanction",
    )

    def analyze(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
    ) -> dict[str, Any]:
        now = time.time()
        raw_items = list(context.normalized_news or [])
        if not raw_items:
            return {
                "sentiment": "neutral",
                "impact": "low",
                "relevance": "indirect",
                "strength": 0.0,
                "risk_flag": False,
                "imminent_high_impact": False,
                "block_trade": False,
                "execution_block": False,
                "recommend_hold": False,
                "confidence_adjustment": 0.0,
                "items_count": 0,
                "news_unavailable": False,
                "message": "No important news affecting this trade.",
                "reason_code": "no_news",
            }

        target = "bullish" if str(signal.action).upper() == "BUY" else "bearish"
        score_items: list[_NewsScoreItem] = []
        fallback_used = False
        for item in raw_items[:12]:
            score_items.append(self._score_item(item, context, now=now))
            meta = item.metadata or {}
            # If upstream pipeline had to fallback to partial/old data, keep track.
            if bool(meta.get("fallback_used")):
                fallback_used = True

        bullish_strength = sum(i.strength for i in score_items if i.sentiment == "bullish")
        bearish_strength = sum(i.strength for i in score_items if i.sentiment == "bearish")
        neutral_strength = sum(i.strength for i in score_items if i.sentiment == "neutral")
        strongest = max(score_items, key=lambda i: i.strength, default=None)

        total_weight = bullish_strength + bearish_strength + neutral_strength
        if total_weight <= 0:
            sentiment = "neutral"
        elif bullish_strength >= bearish_strength * 1.15:
            sentiment = "bullish"
        elif bearish_strength >= bullish_strength * 1.15:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        impact = strongest.impact if strongest else "low"
        relevance = strongest.relevance if strongest else "indirect"
        strength = strongest.strength if strongest else 0.0
        risk_flag = any(i.risk_flag for i in score_items)
        imminent_high_impact = any(i.imminent and i.impact == "high" for i in score_items)
        has_major_headline = any(self._is_major_event(i.headline) for i in score_items)
        high_impact_bullish = sum(
            i.strength for i in score_items
            if i.impact == "high" and i.relevance == "direct" and i.sentiment == "bullish"
        )
        high_impact_bearish = sum(
            i.strength for i in score_items
            if i.impact == "high" and i.relevance == "direct" and i.sentiment == "bearish"
        )
        high_impact_conflict = high_impact_bullish >= 0.45 and high_impact_bearish >= 0.45
        if has_major_headline and impact == "high":
            risk_flag = True
        if high_impact_conflict:
            risk_flag = True

        technical_conf = float(signal.confidence or 0.0)
        uncertain_setup = technical_conf < 0.62 or str(signal.action).upper() not in {"BUY", "SELL"}
        aligned = sentiment == target and sentiment in {"bullish", "bearish"}
        contradicting = sentiment not in {"neutral", target}

        confidence_adjustment = 0.0
        if aligned:
            confidence_adjustment += min(0.08, 0.02 + strength * 0.07)
        elif contradicting:
            confidence_adjustment -= min(0.18, 0.04 + strength * 0.13)

        if risk_flag and uncertain_setup:
            confidence_adjustment -= 0.10
        if high_impact_conflict:
            confidence_adjustment -= 0.12

        recommend_hold = False
        execution_block = False
        reason_code = "neutral"
        message = "No important news affecting this trade."

        if imminent_high_impact:
            recommend_hold = True
            execution_block = True
            reason_code = "imminent_high_impact"
            message = "Execution delayed due to upcoming high-impact event."
        elif high_impact_conflict:
            recommend_hold = True
            reason_code = "high_impact_conflict"
            message = "Trade avoided due to conflicting high-impact news."
        elif risk_flag and impact == "high" and uncertain_setup and strength >= 0.40:
            recommend_hold = True
            reason_code = "high_impact_uncertain"
            message = "Trade avoided due to high-impact news."
        elif contradicting and strength >= 0.58:
            recommend_hold = True
            reason_code = "strong_contradiction"
            message = "News contradicts the setup."
        elif aligned and strength >= 0.5:
            reason_code = "aligned"
            message = "News supports this decision."
        elif contradicting:
            reason_code = "contradiction"
            message = "Confidence is reduced due to conflicting news."

        if fallback_used and confidence_adjustment > 0.0:
            confidence_adjustment = 0.0
            if reason_code == "aligned":
                reason_code = "fallback_data"
                message = "News data is degraded; confidence is not boosted."

        confidence_adjustment = max(-0.22, min(0.10, confidence_adjustment))
        block_trade = recommend_hold or execution_block

        return {
            "sentiment": sentiment,
            "impact": impact,
            "relevance": relevance,
            "strength": round(max(0.0, min(1.0, strength)), 3),
            "risk_flag": bool(risk_flag),
            "imminent_high_impact": bool(imminent_high_impact),
            "block_trade": bool(block_trade),
            "execution_block": bool(execution_block),
            "recommend_hold": bool(recommend_hold),
            "confidence_adjustment": round(confidence_adjustment, 3),
            "items_count": len(score_items),
            "news_unavailable": False,
            "fallback_used": fallback_used,
            "message": message,
            "reason_code": reason_code,
        }

    def unavailable_snapshot(self, reason: str = "") -> dict[str, Any]:
        text = (reason or "").strip() or "Decision made without news data."
        return {
            "sentiment": "neutral",
            "impact": "low",
            "relevance": "indirect",
            "strength": 0.0,
            "risk_flag": False,
            "imminent_high_impact": False,
            "block_trade": False,
            "execution_block": False,
            "recommend_hold": False,
            "confidence_adjustment": 0.0,
            "items_count": 0,
            "news_unavailable": True,
            "fallback_used": True,
            "message": text,
            "reason_code": "unavailable",
        }

    def _score_item(self, item, context: MarketContext, *, now: float) -> _NewsScoreItem:
        meta = dict(item.metadata or {})
        headline = str(meta.get("headline") or item.title or "").strip()

        sentiment = str(meta.get("sentiment") or meta.get("gemini_bias") or meta.get("bias") or "neutral").lower()
        if sentiment not in {"bullish", "bearish", "neutral"}:
            sentiment = "neutral"

        raw_impact = meta.get("impact_level") or meta.get("importance") or meta.get("gemini_event_risk")
        impact = str(raw_impact or "low").lower()
        if impact not in {"low", "medium", "high"}:
            impact = "low"
        if raw_impact is None and self._is_major_event(headline):
            impact = "high"

        relevance = str(meta.get("relevance") or "").lower()
        if relevance not in {"direct", "indirect", "global"}:
            relevance = self._derive_relevance(item=item, context=context)

        confidence_raw = meta.get("confidence")
        if confidence_raw is None:
            confidence = 0.75
        else:
            confidence = self._safe_float(confidence_raw)
            if confidence is None:
                confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        is_recent = meta.get("is_recent")
        if isinstance(is_recent, bool):
            recency = 1.0 if is_recent else 0.55
        else:
            published_at = self._safe_float(item.published_at)
            age_seconds = max(0.0, now - published_at) if published_at is not None else 24 * 3600
            if age_seconds <= 15 * 60:
                recency = 1.0
            elif age_seconds <= 60 * 60:
                recency = 0.85
            elif age_seconds <= 3 * 3600:
                recency = 0.70
            elif age_seconds <= 24 * 3600:
                recency = 0.55
            else:
                recency = 0.35

        strength = (
            self._SENTIMENT_W[sentiment]
            * self._IMPACT_W[impact]
            * self._RELEVANCE_W[relevance]
            * recency
            * confidence
        )
        strength = max(0.0, min(1.0, strength))

        event_ts = self._safe_float(meta.get("event_timestamp")) or self._safe_float(meta.get("timestamp")) or 0.0
        if event_ts <= 0:
            event_ts = self._safe_float(item.published_at) or 0.0
        seconds_to_event = event_ts - now
        imminent = 0 < seconds_to_event <= 90 * 60

        risk_flag = (
            impact == "high"
            or self._is_major_event(headline)
            or bool(meta.get("unexpected_headline"))
        )

        return _NewsScoreItem(
            sentiment=sentiment,
            impact=impact,
            relevance=relevance,
            strength=strength,
            risk_flag=bool(risk_flag),
            imminent=bool(imminent),
            headline=headline,
        )

    def _derive_relevance(self, *, item, context: MarketContext) -> str:
        target = str(context.symbol or "").upper()
        affected = {str(symbol).upper() for symbol in (item.affected_symbols or [])}
        if target and target in affected:
            return "direct"
        category = str((context.symbol_info.category if context.symbol_info else "") or "").upper()
        if category and category in affected:
            return "indirect"
        return "global" if item.category in {"macro_event", "calendar"} else "indirect"

    def _is_major_event(self, headline: str) -> bool:
        normalized = (headline or "").strip().lower()
        return any(keyword in normalized for keyword in self._MAJOR_EVENT_KEYWORDS)

    def _safe_float(self, value: Any) -> float | None:
        try:
            parsed = float(value)
            if math.isfinite(parsed):
                return parsed
        except (TypeError, ValueError):
            return None
        return None
