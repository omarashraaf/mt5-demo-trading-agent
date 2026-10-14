from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from domain.models import GeminiAssessment, MarketContext, TechnicalSignal
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if math.isfinite(float(value)) else None
    return value


SYSTEM_INSTRUCTION = """You are the primary trade-decision brain inside LinkTrade.

You receive:
- the last-hours chart structure and multi-timeframe price context
- the built-in technical analysis already produced by the app
- relevant Benzinga/news context and Gemini news classification
- the app's strategy/risk context
- meta-model insight
- recent trade history and outcome-learning notes

You MUST output ONLY valid JSON matching this schema:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0 to 0.95,
  "stop_loss_price": number | null,
  "take_profit_price": number | null,
  "max_hold_minutes": 30 to 720,
  "strategy": "short strategy label",
  "summary_reason": "short plain-English explanation",
  "market_regime": "trend|range|event|unclear",
  "risk_note": "short safety note"
}

Rules:
- You are the primary recommender, but you are NOT the final execution authority.
- The deterministic risk engine, panic stop, portfolio controls, and execution gates remain final authority.
- Use the existing app analysis as a first-class input. Do not ignore the built-in technical baseline.
- Use Benzinga/news only when it is relevant to the symbol or market regime.
- Respect the recent trade history and learn from repeated losses or poor recent behavior.
- If the evidence is mixed, contradictory, stale, or weak, choose HOLD.
- BUY/SELL must include directionally valid stop_loss_price and take_profit_price:
  - BUY: stop_loss_price below entry, take_profit_price above entry
  - SELL: stop_loss_price above entry, take_profit_price below entry
- Never promise profits. Never mention certainty.
- Never output markdown or extra text outside the JSON object."""


class GeminiTradeBrainResponse(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"] = "HOLD"
    confidence: float = 0.0
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    max_hold_minutes: int = 120
    strategy: str = "gemini_primary_brain"
    summary_reason: str = ""
    market_regime: Literal["trend", "range", "event", "unclear"] = "unclear"
    risk_note: str = ""


class GeminiTradeBrainService:
    """Gemini primary decision layer that authors the trade recommendation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 12.0,
        max_retries: int = 1,
        model_name: str | None = None,
    ):
        self.api_key = (api_key or nvidia_api_key(os.getenv("GEMINI_API_KEY", ""))).strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.model_name = (model_name or os.getenv("GEMINI_MODEL", "gemma-4-26b-a4b-it")).strip() or "gemma-4-26b-a4b-it"
        self.active_model_name = self.model_name
        if "31b" in self.model_name.lower() and self.timeout_seconds < 20.0:
            self.timeout_seconds = 20.0
        self._client = None
        self._unavailable_reason = ""
        self._last_error = ""
        self._quota_cooldown_until = 0.0
        self._model_cooldown_until: dict[str, float] = {}
        self._quota_min_cooldown_seconds = 900.0
        self._cache_ttl_seconds = 45.0
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._init_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Gemini trade brain unavailable."

    @property
    def last_error(self) -> str:
        return self._last_error

    async def assess(
        self,
        *,
        context: MarketContext,
        baseline_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        meta_model_insight: dict[str, Any] | None = None,
        recent_outcomes: list[dict[str, Any]] | None = None,
        recent_evaluations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if baseline_signal.action not in {"BUY", "SELL"}:
            return self._fallback("Built-in analysis did not find an actionable setup.", used=False)
        if not self.available:
            return self._fallback(self.unavailable_reason, used=False)
        self._sync_quota_block()
        if time.time() < self._quota_cooldown_until:
            return self._fallback(
                "Gemini is paused due to quota cooldown; built-in analysis is active.",
                used=False,
                degraded=False,
                error="quota_cooldown",
            )

        cache_key = self._cache_key(
            context=context,
            baseline_signal=baseline_signal,
            gemini_assessment=gemini_assessment,
            meta_model_insight=meta_model_insight or {},
        )
        use_cache = context.evaluation_mode in {"auto", "scan"}
        cached = self._cache_get(cache_key)
        if use_cache and cached is not None:
            return {**cached, "cached": True}

        # Bulk scan/auto loops must stay responsive. Avoid stacking retries there,
        # otherwise one slow model response can consume the whole per-symbol budget.
        bulk_mode = context.evaluation_mode in {"auto", "scan"}
        attempts = 1 if bulk_mode else max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._assess_sync,
                        context,
                        baseline_signal,
                        gemini_assessment,
                        meta_model_insight or {},
                        list(recent_outcomes or []),
                        list(recent_evaluations or []),
                    ),
                    timeout=self.timeout_seconds,
                )
                self._last_error = ""
                self._cache_set(cache_key, result)
                return result
            except asyncio.TimeoutError:
                last_error = RuntimeError(
                    f"Gemini trade brain timed out after {self.timeout_seconds:.1f}s (model={self.model_name})."
                )
                if cached is not None:
                    return {
                        **cached,
                        "cached": True,
                        "summary_reason": (
                            f"{cached.get('summary_reason', '')} Reused recent Gemini trade-brain output after timeout."
                        ).strip(),
                    }
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                last_error = exc
                text = str(exc)
                if is_rate_limit_error(text):
                    self._sync_quota_block()
                    self._last_error = ""
                    return self._fallback(
                        "Gemini is paused due to quota cooldown; built-in analysis is active.",
                        used=False,
                        degraded=False,
                        error="quota_cooldown",
                    )
                logger.warning("Gemini trade brain failed for %s: %r", context.symbol, exc)

        self._last_error = str(last_error) if last_error else "Gemini trade brain failed."
        return self._fallback(
            "Gemini trade brain is temporarily unavailable; built-in analysis remains active.",
            used=True,
            degraded=True,
            error=self._last_error,
        )

    def _assess_sync(
        self,
        context: MarketContext,
        baseline_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        meta_model_insight: dict[str, Any],
        recent_outcomes: list[dict[str, Any]],
        recent_evaluations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "symbol": context.symbol,
            "evaluation_mode": context.evaluation_mode,
            "requested_timeframe": context.requested_timeframe,
            "category": context.symbol_info.category if context.symbol_info else "Other",
            "entry_price": self._entry_price(context, baseline_signal.action),
            "policy": {
                "mode": str((context.user_policy or {}).get("mode", "balanced")),
                "min_reward_risk": float((context.user_policy or {}).get("min_reward_risk", 1.8) or 1.8),
                "gemini_role": str((context.user_policy or {}).get("gemini_role", "advisory")),
                "force_execute_any_conditions": bool((context.user_policy or {}).get("force_execute_any_conditions", False)),
            },
            "baseline_analysis": {
                "agent_name": baseline_signal.agent_name,
                "action": baseline_signal.action,
                "confidence": float(baseline_signal.confidence),
                "stop_loss": baseline_signal.stop_loss,
                "take_profit": baseline_signal.take_profit,
                "max_holding_minutes": baseline_signal.max_holding_minutes,
                "reason": baseline_signal.reason,
                "strategy": baseline_signal.strategy,
                "metadata": {
                    "h1_trend": (baseline_signal.metadata or {}).get("h1_trend"),
                    "h4_trend": (baseline_signal.metadata or {}).get("h4_trend"),
                    "entry_signal": (baseline_signal.metadata or {}).get("entry_signal"),
                    "atr": (baseline_signal.metadata or {}).get("atr"),
                    "atr_pct": (baseline_signal.metadata or {}).get("atr_pct"),
                    "reward_risk_ratio": (baseline_signal.metadata or {}).get("reward_risk_ratio"),
                    "event_context": (baseline_signal.metadata or {}).get("event_context"),
                    "strategy_advisory": (baseline_signal.metadata or {}).get("strategy_advisory"),
                },
            },
            "market_context": {
                "tick": dict(context.tick or {}),
                "bar_counts": {key: len(value) for key, value in (context.bars_by_timeframe or {}).items()},
                "chart_summary": self._build_chart_summary(context),
            },
            "news_context": {
                "benzinga_relevant_items": [
                    {
                        "source": item.source,
                        "title": item.title,
                        "summary": item.summary,
                        "published_at": item.published_at,
                        "affected_symbols": item.affected_symbols,
                    }
                    for item in (context.normalized_news or [])[:8]
                ],
                "gemini_news_assessment": gemini_assessment.model_dump() if gemini_assessment else None,
            },
            "meta_model_insight": dict(meta_model_insight or {}),
            "strategy_context": dict(context.strategy_context or {}),
            "recent_trade_history": self._build_recent_trade_summary(recent_outcomes),
            "recent_evaluations": self._build_recent_eval_summary(recent_evaluations),
        }

        prompt = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            "Input payload (JSON):\n"
            f"{json.dumps(_json_safe(payload), ensure_ascii=True, allow_nan=False)}"
        )
        config = types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=GeminiTradeBrainResponse,
        )
        response = self._generate_with_model_fallback(
            contents=prompt,
            config=config,
        )
        parsed = self._extract_response_payload(response)
        normalized = self._normalize_response(context, baseline_signal, parsed)
        normalized["used"] = True
        normalized["available"] = True
        normalized["degraded"] = False
        normalized["raw_payload"] = {"model": self.active_model_name, "response": parsed}
        return normalized

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
                        default_seconds=self._quota_min_cooldown_seconds,
                    )
                    self._sync_quota_block()
                    logger.warning("Gemini trade brain model %s rate limited, trying fallback model.", model_name)
                    continue
                raise
        self._sync_quota_block()
        if last_error is not None:
            raise last_error
        raise RuntimeError("All configured Gemini trade-brain models are in cooldown.")

    def _extract_response_payload(self, response: Any) -> dict[str, Any]:
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            if isinstance(parsed, BaseModel):
                return parsed.model_dump()
            if isinstance(parsed, dict):
                return dict(parsed)
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise RuntimeError("Gemini trade brain returned an empty response.")
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return dict(json.loads(cleaned))

    def _normalize_response(
        self,
        context: MarketContext,
        baseline_signal: TechnicalSignal,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        action = str(payload.get("action", "HOLD")).upper()
        if action not in {"BUY", "SELL", "HOLD"}:
            action = "HOLD"
        baseline_confidence = self._safe_float(baseline_signal.confidence) or 0.0
        confidence = self._safe_float(payload.get("confidence"))
        if confidence is None:
            confidence = baseline_confidence
        confidence = max(0.0, min(0.95, confidence))
        max_hold_raw = self._safe_float(payload.get("max_hold_minutes"))
        if max_hold_raw is None:
            max_hold_raw = self._safe_float(baseline_signal.max_holding_minutes) or 120.0
        max_hold_minutes = max(30, min(720, int(max_hold_raw)))
        stop_loss = self._safe_float(payload.get("stop_loss_price"))
        take_profit = self._safe_float(payload.get("take_profit_price"))
        baseline_stop_loss = self._safe_float(baseline_signal.stop_loss)
        baseline_take_profit = self._safe_float(baseline_signal.take_profit)
        entry = self._entry_price(context, action)
        if action == "BUY":
            if stop_loss is None or stop_loss >= entry:
                stop_loss = baseline_stop_loss
            if take_profit is None or take_profit <= entry:
                take_profit = baseline_take_profit
            if not (stop_loss is not None and take_profit is not None and stop_loss < entry < take_profit):
                action = "HOLD"
                stop_loss = None
                take_profit = None
        elif action == "SELL":
            if stop_loss is None or stop_loss <= entry:
                stop_loss = baseline_stop_loss
            if take_profit is None or take_profit >= entry:
                take_profit = baseline_take_profit
            if not (stop_loss is not None and take_profit is not None and take_profit < entry < stop_loss):
                action = "HOLD"
                stop_loss = None
                take_profit = None
        else:
            stop_loss = None
            take_profit = None

        return {
            "action": action,
            "confidence": round(confidence, 2),
            "stop_loss_price": stop_loss,
            "take_profit_price": take_profit,
            "max_hold_minutes": max_hold_minutes,
            "strategy": str(payload.get("strategy", "") or baseline_signal.strategy or "gemini_primary_brain").strip(),
            "summary_reason": str(payload.get("summary_reason", "") or "Gemini primary trade analysis completed.").strip(),
            "market_regime": str(payload.get("market_regime", "unclear") or "unclear").strip().lower(),
            "risk_note": str(payload.get("risk_note", "") or "").strip(),
        }

    def _build_chart_summary(self, context: MarketContext) -> dict[str, Any]:
        def _summarize_tf(tf: str, lookback: int) -> dict[str, Any]:
            bars = list((context.bars_by_timeframe or {}).get(tf, [])[-lookback:])
            if len(bars) < 2:
                return {"timeframe": tf, "trend": "flat", "change_pct": 0.0, "high": 0.0, "low": 0.0}
            closes = [self._safe_float(bar.get("close")) or 0.0 for bar in bars]
            highs = [
                self._safe_float(bar.get("high")) if self._safe_float(bar.get("high")) is not None else close
                for bar, close in zip(bars, closes)
            ]
            lows = [
                self._safe_float(bar.get("low")) if self._safe_float(bar.get("low")) is not None else close
                for bar, close in zip(bars, closes)
            ]
            start = closes[0]
            end = closes[-1]
            change_pct = ((end - start) / start * 100.0) if start > 0 else 0.0
            trend = "flat"
            if change_pct > 0.03:
                trend = "bullish"
            elif change_pct < -0.03:
                trend = "bearish"
            return {
                "timeframe": tf,
                "trend": trend,
                "change_pct": round(change_pct, 4),
                "high": max(highs) if highs else 0.0,
                "low": min(lows) if lows else 0.0,
                "close": end,
            }

        return {
            "M15": _summarize_tf("M15", 12),
            "H1": _summarize_tf("H1", 12),
            "H4": _summarize_tf("H4", 8),
        }

    def _build_recent_trade_summary(self, recent_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        normalized = recent_outcomes[:6]
        realized = [self._outcome_profit(item) for item in normalized]
        return {
            "count": len(normalized),
            "net_profit": round(sum(realized), 2),
            "wins": sum(1 for value in realized if value > 0),
            "losses": sum(1 for value in realized if value < 0),
            "recent": [
                {
                    "action": str(item.get("action", "")).upper(),
                    "profit": self._outcome_profit(item),
                    "closed_at": item.get("closed_at"),
                }
                for item in normalized
            ],
        }

    def _outcome_profit(self, outcome: dict[str, Any]) -> float:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            try:
                value = outcome.get(key)
                if value is not None:
                    parsed = self._safe_float(value)
                    if parsed is not None:
                        return parsed
            except (TypeError, ValueError):
                continue
        return 0.0

    def _build_recent_eval_summary(self, recent_evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summarized: list[dict[str, Any]] = []
        for item in recent_evaluations[:4]:
            summarized.append(
                {
                    "action": str(item.get("action", "")).upper(),
                    "confidence": self._safe_float(item.get("confidence")) or 0.0,
                    "trade_quality_score": self._safe_float(item.get("trade_quality_score")) or 0.0,
                    "reason": str(item.get("reason", "") or "").strip(),
                }
            )
        return summarized

    def _cache_key(
        self,
        *,
        context: MarketContext,
        baseline_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        meta_model_insight: dict[str, Any],
    ) -> str:
        last_h1 = ""
        h1_bars = (context.bars_by_timeframe or {}).get("H1", [])
        if h1_bars:
            last_h1 = str(h1_bars[-1].get("close", ""))
        news_headlines = [item.title for item in (context.normalized_news or [])[:4]]
        return json.dumps(
            {
                "symbol": context.symbol,
                "mode": context.evaluation_mode,
                "action": baseline_signal.action,
                "confidence": round(self._safe_float(baseline_signal.confidence) or 0.0, 2),
                "last_h1": last_h1,
                "news": news_headlines,
                "gemini_news_bias": gemini_assessment.news_bias if gemini_assessment else "neutral",
                "meta_active": bool(meta_model_insight.get("active")),
                "meta_prob": round(self._safe_float(meta_model_insight.get("profit_probability")) or 0.0, 3),
            },
            ensure_ascii=True,
            sort_keys=True,
        )

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        item = self._cache.get(key)
        if item is None:
            return None
        cached_at, payload = item
        if (time.time() - cached_at) > self._cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return dict(payload)

    def _cache_set(self, key: str, payload: dict[str, Any]):
        self._cache[key] = (time.time(), dict(payload))

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        if action == "BUY":
            return self._safe_float(context.tick.get("ask")) or 0.0
        if action == "SELL":
            return self._safe_float(context.tick.get("bid")) or 0.0
        return 0.0

    def _fallback(
        self,
        reason: str,
        *,
        used: bool,
        degraded: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "used": used,
            "available": self.available,
            "degraded": degraded,
            "action": "HOLD",
            "confidence": 0.0,
            "stop_loss_price": None,
            "take_profit_price": None,
            "max_hold_minutes": 120,
            "strategy": "gemini_primary_brain",
            "summary_reason": reason,
            "market_regime": "unclear",
            "risk_note": "Fallback to built-in analysis.",
            "error": error,
        }

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            parsed = float(value)
            if math.isfinite(parsed):
                return parsed
        except (TypeError, ValueError, OverflowError):
            return None
        return None

    def _init_client(self):
        if nvidia_provider_enabled(api_key=self.api_key, model_name=self.model_name):
            if not self.api_key:
                self._unavailable_reason = "NVIDIA_API_KEY not set"
                logger.warning(self._unavailable_reason)
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
            except Exception as exc:
                self._unavailable_reason = str(exc)
                logger.error("Failed to initialize NVIDIA trade brain: %s", exc)
                self._client = None
            return

        if genai is None or types is None:
            self._unavailable_reason = (
                f"google-genai dependency unavailable: {GEMINI_IMPORT_ERROR}"
                if GEMINI_IMPORT_ERROR
                else "google-genai dependency unavailable"
            )
            logger.warning(self._unavailable_reason)
            self._client = None
            return
        if not self.api_key:
            self._unavailable_reason = "GEMINI_API_KEY not set"
            logger.warning(self._unavailable_reason)
            self._client = None
            return
        try:
            self._client = genai.Client(api_key=self.api_key)
            self._unavailable_reason = ""
        except Exception as exc:
            self._unavailable_reason = str(exc)
            logger.error("Failed to initialize Gemini trade brain: %s", exc)
            self._client = None

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
