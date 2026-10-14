from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from dotenv import load_dotenv

from domain.models import CandidateAssetMapping, ExternalEvent, GeminiEventAssessment
from services.gemini_model_fallback import (
    build_structured_output_model_candidates,
    cooldown_remaining_seconds,
    is_rate_limit_error,
    mark_model_cooldown,
    next_available_model,
)
from services.nvidia_chat_client import (
    NvidiaChatCompletionsClient,
    nvidia_api_key,
    nvidia_model_name,
    nvidia_provider_enabled,
)

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


EVENT_CLASSIFIER_INSTRUCTION = """You are a bounded market-event classifier for an MT5 trading system.

You MUST output ONLY valid JSON using this schema:
{
  "event_type": "string",
  "affected_assets": ["string"],
  "importance": "low|medium|high",
  "bias_by_asset": {
    "US100": "bullish|bearish|neutral",
    "US500": "bullish|bearish|neutral",
    "US30": "bullish|bearish|neutral",
    "GER40": "bullish|bearish|neutral",
    "GOLD": "bullish|bearish|neutral",
    "WTI": "bullish|bearish|neutral",
    "BRENT": "bullish|bearish|neutral"
  },
  "persistence_horizon": "short|medium|structural",
  "event_risk": "low|medium|high",
  "confidence_adjustment": -0.10 to 0.10,
  "contradiction_flag": false,
  "summary_reason": "string"
}

Rules:
- You are not allowed to generate executable trade commands.
- You are not allowed to decide lot size or leverage.
- You are not allowed to bypass risk checks.
- Use the candidate mappings as priors, not as guarantees.
- If the event is ambiguous, stale, contradictory, or low-quality, prefer neutral outputs.
- Keep summary_reason short and auditable.
- Output JSON only."""


class GeminiEventClassifier:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 12.0,
        max_retries: int = 1,
        model_name: str | None = None,
    ):
        self.api_key = (api_key or nvidia_api_key(os.getenv("GEMINI_API_KEY", ""))).strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.model_name = (model_name or os.getenv("GEMINI_MODEL", "gemma-4-26b-a4b-it")).strip() or "gemma-4-26b-a4b-it"
        self.active_model_name = self.model_name
        self._client = None
        self._unavailable_reason = ""
        self._quota_cooldown_until = 0.0
        self._model_cooldown_until: dict[str, float] = {}
        self._init_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Gemini event classifier unavailable."

    async def classify_event(
        self,
        *,
        event: ExternalEvent,
        candidate_mappings: list[CandidateAssetMapping],
    ) -> GeminiEventAssessment:
        if not self.available:
            return self._fallback_assessment(event, candidate_mappings, error=self.unavailable_reason)
        self._sync_quota_block()
        if time.time() < self._quota_cooldown_until:
            return self._fallback_assessment(event, candidate_mappings, error="quota_cooldown")

        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._classify_sync, event, candidate_mappings),
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                last_error = exc
                if is_rate_limit_error(exc):
                    self._sync_quota_block()
                    return self._fallback_assessment(event, candidate_mappings, error="quota_cooldown")
                logger.warning("Gemini event classification degraded for %s: %s", event.title, exc)
        return self._fallback_assessment(event, candidate_mappings, error=str(last_error) if last_error else "Gemini classification failed.")

    def _classify_sync(
        self,
        event: ExternalEvent,
        candidate_mappings: list[CandidateAssetMapping],
    ) -> GeminiEventAssessment:
        prompt = (
            f"{EVENT_CLASSIFIER_INSTRUCTION}\n\n"
            "Input payload:\n"
            f"{self._build_prompt(event, candidate_mappings)}"
        )
        config = types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        )
        response = self._generate_with_model_fallback(
            contents=prompt,
            config=config,
        )
        text = (response.text or "").strip()
        return self._parse_response(text, event=event)

    def _model_candidates(self) -> list[str]:
        return build_structured_output_model_candidates(
            self.model_name,
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        )

    def _sync_quota_block(self):
        candidates = self._model_candidates()
        if next_available_model(candidates, self._model_cooldown_until) is not None:
            self._quota_cooldown_until = 0.0
            return
        remaining = cooldown_remaining_seconds(candidates, self._model_cooldown_until)
        self._quota_cooldown_until = time.time() + remaining if remaining > 0 else 0.0

    def _generate_with_model_fallback(self, *, contents: str, config):
        last_error: Exception | None = None
        for model_name in self._model_candidates():
            if float(self._model_cooldown_until.get(model_name, 0.0) or 0.0) > time.time():
                continue
            try:
                response = self._client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                self.active_model_name = model_name
                self._sync_quota_block()
                return response
            except Exception as exc:
                last_error = exc
                if is_rate_limit_error(exc):
                    mark_model_cooldown(
                        self._model_cooldown_until,
                        model_name,
                        error_text=str(exc),
                        default_seconds=60.0,
                    )
                    self._sync_quota_block()
                    logger.warning("Gemini event classifier model %s rate limited, trying fallback model.", model_name)
                    continue
                raise
        self._sync_quota_block()
        if last_error is not None:
            raise last_error
        raise RuntimeError("All configured Gemini event classifier models are in cooldown.")

    def _init_client(self):
        if nvidia_provider_enabled(api_key=self.api_key, model_name=self.model_name):
            if not self.api_key:
                self._unavailable_reason = "NVIDIA_API_KEY not set"
                self._client = None
                return
            try:
                self.model_name = nvidia_model_name(self.model_name)
                self.active_model_name = self.model_name
                self._client = NvidiaChatCompletionsClient(
                    api_key=self.api_key,
                    timeout_seconds=self.timeout_seconds,
                )
                self._unavailable_reason = ""
            except Exception as exc:  # pragma: no cover - defensive init guard
                self._unavailable_reason = str(exc)
                self._client = None
            return

        if genai is None or types is None:
            self._unavailable_reason = (
                f"google-genai dependency unavailable: {GEMINI_IMPORT_ERROR}"
                if GEMINI_IMPORT_ERROR
                else "google-genai dependency unavailable"
            )
            return
        if not self.api_key:
            self._unavailable_reason = "GEMINI_API_KEY not set"
            return
        try:
            self._client = genai.Client(api_key=self.api_key)
            self._unavailable_reason = ""
        except Exception as exc:  # pragma: no cover - defensive init guard
            self._unavailable_reason = str(exc)
            self._client = None

    def _build_prompt(
        self,
        event: ExternalEvent,
        candidate_mappings: list[CandidateAssetMapping],
    ):
        candidate_summary = [
            {
                "symbol": mapping.symbol,
                "baseline_bias": mapping.baseline_bias,
                "needs_gemini_clarification": mapping.needs_gemini_clarification,
                "tradable": mapping.tradable,
                "reason": mapping.reason,
            }
            for mapping in candidate_mappings
        ]
        event_summary = {
            "source": event.source,
            "title": event.title,
            "summary": event.summary,
            "country": event.country,
            "category": event.category,
            "importance": event.importance,
            "event_type": event.event_type,
            "timestamp_utc": event.timestamp_utc,
            "affected_assets": event.affected_assets,
        }
        return json.dumps(
            {
                "event": event_summary,
                "candidate_mappings": candidate_summary,
                "allowed_assets": ["US100", "US500", "US30", "GER40", "GOLD", "WTI", "BRENT"],
            },
            ensure_ascii=True,
        )

    def _parse_response(self, text: str, *, event: ExternalEvent) -> GeminiEventAssessment:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

        data = json.loads(cleaned)
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), {})
        if not isinstance(data, dict):
            data = {}
        affected_assets = [str(item).strip().upper() for item in data.get("affected_assets", []) if str(item).strip()]
        bias_by_asset = {}
        for symbol in ("US100", "US500", "US30", "GER40", "GOLD", "WTI", "BRENT"):
            raw_bias = str((data.get("bias_by_asset") or {}).get(symbol, "neutral")).strip().lower()
            bias_by_asset[symbol] = raw_bias if raw_bias in {"bullish", "bearish", "neutral"} else "neutral"
        importance = str(data.get("importance", event.importance)).strip().lower()
        if importance not in {"low", "medium", "high"}:
            importance = event.importance
        persistence = str(data.get("persistence_horizon", "short")).strip().lower()
        if persistence not in {"short", "medium", "structural"}:
            persistence = "short"
        event_risk = str(data.get("event_risk", "medium")).strip().lower()
        if event_risk not in {"low", "medium", "high"}:
            event_risk = "medium"
        return GeminiEventAssessment(
            used=True,
            available=True,
            degraded=False,
            event_type=str(data.get("event_type", event.event_type)),
            affected_assets=affected_assets or event.affected_assets,
            importance=importance,
            bias_by_asset=bias_by_asset,
            persistence_horizon=persistence,
            event_risk=event_risk,
            confidence_adjustment=max(-0.10, min(0.10, float(data.get("confidence_adjustment", 0.0)))),
            contradiction_flag=bool(data.get("contradiction_flag", False)),
            summary_reason=str(data.get("summary_reason", "")),
            raw_payload={"raw_text": cleaned, "model": self.active_model_name},
        )

    def _fallback_assessment(
        self,
        event: ExternalEvent,
        candidate_mappings: list[CandidateAssetMapping],
        *,
        error: str,
    ) -> GeminiEventAssessment:
        bias_by_asset = {symbol: "neutral" for symbol in ("US100", "US500", "US30", "GER40", "GOLD", "WTI", "BRENT")}
        for mapping in candidate_mappings:
            if mapping.symbol in bias_by_asset:
                bias_by_asset[mapping.symbol] = mapping.baseline_bias
        return GeminiEventAssessment(
            used=False,
            available=False,
            degraded=True,
            event_type=event.event_type,
            affected_assets=list(dict.fromkeys([mapping.symbol for mapping in candidate_mappings] or event.affected_assets)),
            importance=event.importance,
            bias_by_asset=bias_by_asset,
            persistence_horizon="short",
            event_risk="medium" if event.importance != "low" else "low",
            confidence_adjustment=0.0,
            contradiction_flag=False,
            summary_reason="Gemini unavailable; using local event mapping only.",
            error=error,
            raw_payload={},
        )
