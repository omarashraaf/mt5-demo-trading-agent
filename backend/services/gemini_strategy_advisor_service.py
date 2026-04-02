from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

from domain.models import GeminiAssessment, MarketContext, TechnicalSignal

load_dotenv()

try:
    from google import genai
    from google.genai import types
    GEMINI_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - optional dependency
    genai = None
    types = None
    GEMINI_IMPORT_ERROR = exc

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """You are a bounded strategy advisor for a trading system.

You receive:
- technical setup from SmartAgent
- normalized news/event context
- runtime risk mode (safe|balanced|aggressive)

You MUST output ONLY valid JSON with this schema:
{
  "strategy_type": "intraday|swing|event",
  "sl_atr_multiplier": 0.8 to 3.0,
  "tp_atr_multiplier": 1.2 to 6.0,
  "max_hold_minutes": 30 to 720,
  "confidence_adjustment": -0.08 to 0.05,
  "contradiction_flag": false,
  "summary_reason": "short explanation"
}

Rules:
- Never output executable trade commands.
- Never set lot size, leverage, or margin settings.
- Never bypass risk checks.
- Prefer conservative recommendations when uncertain.
- If setup is weak/contradictory, use a negative confidence_adjustment.
- Output JSON only."""


class GeminiStrategyAdvisorService:
    """Gemini advisory layer for strategy shape only (never execution authority)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 1,
        model_name: str = "gemini-2.5-flash",
    ):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.model_name = model_name
        self._client = None
        self._unavailable_reason = ""
        self._last_error = ""
        self._quota_cooldown_until = 0.0
        self._quota_min_cooldown_seconds = 900.0
        self._init_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Gemini strategy advisor unavailable."

    @property
    def last_error(self) -> str:
        return self._last_error

    async def assess(
        self,
        *,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        event_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if technical_signal.action not in {"BUY", "SELL"}:
            return self._fallback("No actionable direction to advise.", used=False)
        if not self.available:
            return self._fallback(self.unavailable_reason, used=False)
        if time.time() < self._quota_cooldown_until:
            return self._fallback(
                "Gemini is paused due to quota cooldown; deterministic strategy is active.",
                used=False,
                degraded=False,
                error="quota_cooldown",
            )

        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._assess_sync,
                        context,
                        technical_signal,
                        gemini_assessment,
                        event_context or {},
                    ),
                    timeout=self.timeout_seconds,
                )
                self._last_error = ""
                return result
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                last_error = exc
                text = str(exc)
                upper = text.upper()
                if "API_KEY_INVALID" in upper or "API KEY EXPIRED" in upper:
                    self._last_error = "Gemini API key is invalid or expired."
                    self._unavailable_reason = self._last_error
                    self._client = None
                    return self._fallback(
                        "Gemini API key is invalid or expired; technical strategy is active.",
                        used=False,
                        degraded=False,
                        error="api_key_invalid",
                    )
                if "429" in text or "RESOURCE_EXHAUSTED" in text:
                    retry = self._quota_min_cooldown_seconds
                    match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", text, flags=re.IGNORECASE)
                    if match:
                        retry = max(self._quota_min_cooldown_seconds, float(match.group(1)))
                    self._quota_cooldown_until = time.time() + retry
                    self._last_error = ""
                    return self._fallback(
                        "Gemini is paused due to quota cooldown; deterministic strategy is active.",
                        used=False,
                        degraded=False,
                        error="quota_cooldown",
                    )
                logger.warning("Gemini strategy advisor failed for %s: %s", context.symbol, exc)

        self._last_error = str(last_error) if last_error else "Gemini strategy advisor failed."
        return self._fallback(
            "Gemini strategy advisor is temporarily unavailable; technical strategy is active.",
            used=True,
            error=self._last_error,
            degraded=True,
        )

    def _assess_sync(
        self,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        event_context: dict[str, Any],
    ) -> dict[str, Any]:
        policy = context.user_policy or {}
        payload = {
            "symbol": context.symbol,
            "category": context.symbol_info.category if context.symbol_info else "Other",
            "mode": str(policy.get("mode", "balanced")),
            "min_reward_risk": float(policy.get("min_reward_risk", 1.8) or 1.8),
            "allow_counter_trend_trades": bool(policy.get("allow_counter_trend_trades", False)),
            "technical": {
                "action": technical_signal.action,
                "confidence": float(technical_signal.confidence),
                "reason": technical_signal.reason,
                "strategy": technical_signal.strategy,
                "metadata": {
                    "h1_trend": (technical_signal.metadata or {}).get("h1_trend"),
                    "h4_trend": (technical_signal.metadata or {}).get("h4_trend"),
                    "entry_signal": (technical_signal.metadata or {}).get("entry_signal"),
                    "atr": (technical_signal.metadata or {}).get("atr"),
                    "atr_pct": (technical_signal.metadata or {}).get("atr_pct"),
                    "reward_risk_ratio": (technical_signal.metadata or {}).get("reward_risk_ratio"),
                },
            },
            "news_context": {
                "normalized_news_items": len(context.normalized_news),
                "event_context": event_context,
                "gemini_news": gemini_assessment.model_dump() if gemini_assessment else None,
            },
            "market_context": {
                "spread": (context.tick or {}).get("spread", 0.0),
                "bar_counts": {
                    key: len(value)
                    for key, value in (context.bars_by_timeframe or {}).items()
                },
            },
        }

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json",
        )
        response = self._client.models.generate_content(
            model=self.model_name,
            contents=json.dumps(payload, ensure_ascii=True),
            config=config,
        )
        return self._parse_response(response.text or "")

    def _parse_response(self, text: str) -> dict[str, Any]:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

        data = json.loads(cleaned)
        strategy_type = str(data.get("strategy_type", "intraday")).strip().lower()
        if strategy_type not in {"intraday", "swing", "event"}:
            strategy_type = "intraday"
        sl_mult = max(0.8, min(3.0, float(data.get("sl_atr_multiplier", 1.5) or 1.5)))
        tp_mult = max(1.2, min(6.0, float(data.get("tp_atr_multiplier", 2.5) or 2.5)))
        hold_minutes = max(30, min(720, int(float(data.get("max_hold_minutes", 120) or 120))))
        confidence_adjustment = max(-0.08, min(0.05, float(data.get("confidence_adjustment", 0.0) or 0.0)))
        contradiction_flag = bool(data.get("contradiction_flag", False))
        summary_reason = str(data.get("summary_reason", "") or "").strip()

        return {
            "used": True,
            "available": True,
            "degraded": False,
            "strategy_type": strategy_type,
            "sl_atr_multiplier": round(sl_mult, 3),
            "tp_atr_multiplier": round(tp_mult, 3),
            "max_hold_minutes": int(hold_minutes),
            "confidence_adjustment": round(confidence_adjustment, 3),
            "contradiction_flag": contradiction_flag,
            "summary_reason": summary_reason,
            "raw_payload": {"raw_text": cleaned},
        }

    def _fallback(
        self,
        reason: str,
        *,
        used: bool,
        degraded: bool = False,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "used": used,
            "available": self.available,
            "degraded": degraded,
            "strategy_type": "intraday",
            "sl_atr_multiplier": 1.5,
            "tp_atr_multiplier": 2.5,
            "max_hold_minutes": 120,
            "confidence_adjustment": 0.0,
            "contradiction_flag": False,
            "summary_reason": reason,
            "error": error,
            "raw_payload": {},
        }

    def _init_client(self):
        if genai is None or types is None:
            self._client = None
            self._unavailable_reason = (
                f"google-genai dependency unavailable: {GEMINI_IMPORT_ERROR}"
                if GEMINI_IMPORT_ERROR
                else "google-genai dependency unavailable"
            )
            return
        if not self.api_key:
            self._client = None
            self._unavailable_reason = "GEMINI_API_KEY not set"
            return
        try:
            self._client = genai.Client(api_key=self.api_key)
            self._unavailable_reason = ""
        except Exception as exc:  # pragma: no cover - defensive init guard
            self._client = None
            self._unavailable_reason = str(exc)
