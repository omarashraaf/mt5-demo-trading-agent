from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

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


SYSTEM_INSTRUCTION = """You are a bounded strategy advisor for a trading system.

You receive:
- technical setup from SmartAgent
- normalized news/event context
- runtime risk mode (safe|balanced|aggressive)
- active strategy brief and compiled strategy summary
- recent market chart summaries across intraday and multi-day windows

You MUST output ONLY valid JSON with this schema:
{
  "strategy_type": "intraday|swing|event",
  "stop_loss_price": number,
  "take_profit_price": number,
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
- stop_loss_price and take_profit_price must match the trade direction:
  - BUY: stop_loss_price below current entry, take_profit_price above current entry
  - SELL: stop_loss_price above current entry, take_profit_price below current entry
- Use the selected strategy brief, current risk mode, recent news, and recent chart structure together.
- Prefer explicit prices when the chart context is sufficient. Use ATR multipliers as the backup logic.
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
        self._cache_ttl_seconds = 120.0
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
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
        use_cache = context.evaluation_mode in {"auto", "scan"}
        primary_cache_key, secondary_cache_key = self._cache_keys(
            context=context,
            technical_signal=technical_signal,
            event_context=event_context or {},
        )
        cached = self._cache_get(primary_cache_key) or self._cache_get(secondary_cache_key)
        if use_cache and cached is not None:
            return {**cached, "cached": True}
        self._sync_quota_block()
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
                self._cache_set(primary_cache_key, result)
                self._cache_set(secondary_cache_key, result)
                return result
            except asyncio.TimeoutError:
                last_error = RuntimeError(
                    f"Gemini strategy advisor timed out after {self.timeout_seconds:.1f}s "
                    f"(model={self.model_name})."
                )
                if cached is not None:
                    return {
                        **cached,
                        "cached": True,
                        "summary_reason": (
                            f"{cached.get('summary_reason', '')} "
                            "Reused recent Gemini advisory after timeout."
                        ).strip(),
                    }
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
                if is_rate_limit_error(text):
                    self._sync_quota_block()
                    self._last_error = ""
                    return self._fallback(
                        "Gemini is paused due to quota cooldown; deterministic strategy is active.",
                        used=False,
                        degraded=False,
                        error="quota_cooldown",
                    )
                logger.warning(
                    "Gemini strategy advisor failed for %s: %r",
                    context.symbol,
                    exc,
                )

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
            "min_reward_risk": self._bounded_float(
                policy.get("min_reward_risk"),
                default=1.8,
                minimum=0.8,
                maximum=10.0,
            ),
            "allow_counter_trend_trades": bool(policy.get("allow_counter_trend_trades", False)),
            "technical": {
                "action": technical_signal.action,
                "confidence": self._bounded_float(
                    technical_signal.confidence,
                    default=0.0,
                    minimum=0.0,
                    maximum=0.95,
                ),
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
                "recent_news": [
                    {
                        "title": item.title,
                        "summary": item.summary,
                        "published_at": item.published_at,
                        "affected_symbols": item.affected_symbols,
                    }
                    for item in (context.normalized_news or [])[:6]
                ],
            },
            "market_context": {
                "entry_price": self._entry_price(context, technical_signal),
                "tick": dict(context.tick or {}),
                "spread": (context.tick or {}).get("spread", 0.0),
                "bar_counts": {
                    key: len(value)
                    for key, value in (context.bars_by_timeframe or {}).items()
                },
                "chart_summary": self._build_chart_summary(context),
            },
            "strategy_context": self._build_strategy_context(context),
        }

        prompt = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            "Input payload (JSON):\n"
            f"{json.dumps(_json_safe(payload), ensure_ascii=True, allow_nan=False)}"
        )
        config = types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        )
        response = self._generate_with_model_fallback(
            contents=prompt,
            config=config,
        )
        response_text = self._extract_response_text(response)
        return self._parse_response(response_text)

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
                        default_seconds=self._quota_min_cooldown_seconds,
                    )
                    self._sync_quota_block()
                    logger.warning("Gemini strategy advisor model %s rate limited, trying fallback model.", model_name)
                    continue
                raise
        self._sync_quota_block()
        if last_error is not None:
            raise last_error
        raise RuntimeError("All configured Gemini strategy advisor models are in cooldown.")

    def _extract_response_text(self, response: Any) -> str:
        try:
            text = (getattr(response, "text", "") or "").strip()
            if text:
                return text
        except Exception:
            pass

        pieces: list[str] = []
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                txt = getattr(part, "text", None)
                if txt:
                    pieces.append(str(txt))
        merged = "\n".join(piece.strip() for piece in pieces if piece and str(piece).strip()).strip()
        if merged:
            return merged

        prompt_feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(prompt_feedback, "block_reason", None)
        raise RuntimeError(f"Gemini returned empty response (block_reason={block_reason or 'none'}).")

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
        stop_loss_price = self._safe_price(data.get("stop_loss_price"))
        take_profit_price = self._safe_price(data.get("take_profit_price"))
        sl_mult = self._bounded_float(data.get("sl_atr_multiplier"), default=1.5, minimum=0.8, maximum=3.0)
        tp_mult = self._bounded_float(data.get("tp_atr_multiplier"), default=2.5, minimum=1.2, maximum=6.0)
        hold_minutes_value = self._bounded_float(data.get("max_hold_minutes"), default=120.0, minimum=30.0, maximum=720.0)
        hold_minutes = int(hold_minutes_value)
        confidence_adjustment = self._bounded_float(
            data.get("confidence_adjustment"),
            default=0.0,
            minimum=-0.08,
            maximum=0.05,
        )
        contradiction_flag = bool(data.get("contradiction_flag", False))
        summary_reason = str(data.get("summary_reason", "") or "").strip()

        return {
            "used": True,
            "available": True,
            "degraded": False,
            "model": self.active_model_name,
            "strategy_type": strategy_type,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
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
            "stop_loss_price": None,
            "take_profit_price": None,
            "sl_atr_multiplier": 1.5,
            "tp_atr_multiplier": 2.5,
            "max_hold_minutes": 120,
            "confidence_adjustment": 0.0,
            "contradiction_flag": False,
            "summary_reason": reason,
            "error": error,
            "raw_payload": {},
        }

    def _cache_keys(
        self,
        *,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        event_context: dict[str, Any],
    ) -> tuple[str, str]:
        mode = str((context.user_policy or {}).get("mode", "balanced")).lower()
        symbol = str(context.symbol or "").upper()
        action = str(technical_signal.action or "HOLD").upper()
        confidence = round(
            self._bounded_float(technical_signal.confidence, default=0.0, minimum=0.0, maximum=0.95),
            2,
        )
        bias = str(event_context.get("bias", "neutral")).lower()
        risk = str(event_context.get("event_risk", "low")).lower()
        detailed = f"{symbol}|{action}|{mode}|{confidence:.2f}|{bias}|{risk}"
        coarse = f"{symbol}|{action}|{mode}"
        return detailed, coarse

    def _safe_price(self, value: Any) -> float | None:
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price) or price <= 0 or price >= 1_000_000_000:
            return None
        return price

    def _bounded_float(self, value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = float(default)
        if not math.isfinite(parsed):
            parsed = float(default)
        return max(float(minimum), min(float(maximum), parsed))

    def _entry_price(self, context: MarketContext, technical_signal: TechnicalSignal) -> float:
        tick = context.tick or {}
        if technical_signal.action == "BUY":
            return self._safe_float(tick.get("ask")) or 0.0
        if technical_signal.action == "SELL":
            return self._safe_float(tick.get("bid")) or 0.0
        return self._safe_float(tick.get("last")) or 0.0

    def _build_strategy_context(self, context: MarketContext) -> dict[str, Any]:
        strategy_ctx = dict(context.strategy_context or {})
        compiled = dict(strategy_ctx.get("compiled_strategy") or {})
        return {
            "id": str(strategy_ctx.get("id") or ""),
            "name": str(strategy_ctx.get("name") or ""),
            "description": str(strategy_ctx.get("description") or ""),
            "brief": str(strategy_ctx.get("brief") or ""),
            "human_summary": str(strategy_ctx.get("human_summary") or ""),
            "summary_lines": [str(line).strip() for line in (strategy_ctx.get("summary_lines") or []) if str(line).strip()],
            "compiled_strategy": {
                "symbols": compiled.get("symbols"),
                "asset_classes": compiled.get("asset_classes"),
                "market_selection_mode": compiled.get("market_selection_mode"),
                "entry_action": compiled.get("entry_action"),
                "fixed_lot_size": compiled.get("fixed_lot_size"),
                "allocation_pct_free_margin": compiled.get("allocation_pct_free_margin"),
                "cadence_buy_every_seconds": compiled.get("cadence_buy_every_seconds"),
                "cadence_sell_after_seconds": compiled.get("cadence_sell_after_seconds"),
                "hold_for_seconds": compiled.get("hold_for_seconds"),
                "apply_under_any_conditions": compiled.get("apply_under_any_conditions"),
                "policy_mode": compiled.get("policy_mode"),
                "gemini_role": compiled.get("gemini_role"),
                "min_reward_risk": compiled.get("min_reward_risk"),
            },
        }

    def _build_chart_summary(self, context: MarketContext) -> dict[str, Any]:
        def summarize_bars(label: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
            if not bars:
                return {"timeframe": label, "bars": 0}
            valid_bars: list[tuple[float, float, float]] = []
            for bar in bars:
                close = self._safe_float(bar.get("close"))
                if close is None or close <= 0:
                    continue
                high = self._safe_float(bar.get("high"))
                low = self._safe_float(bar.get("low"))
                valid_bars.append((close, high if high is not None else close, low if low is not None else close))
            if not valid_bars:
                return {"timeframe": label, "bars": len(bars)}
            closes = [item[0] for item in valid_bars]
            highs = [item[1] for item in valid_bars]
            lows = [item[2] for item in valid_bars]
            last = closes[-1]
            first = closes[0]
            change_pct = ((last - first) / first * 100.0) if first > 0 else 0.0
            return {
                "timeframe": label,
                "bars": len(closes),
                "first_close": round(first, 6),
                "last_close": round(last, 6),
                "change_pct": round(change_pct, 3),
                "high": round(max(highs), 6),
                "low": round(min(lows), 6),
                "recent_closes": [round(value, 6) for value in closes[-6:]],
            }

        bars_by_tf = context.bars_by_timeframe or {}
        return {
            "M15": summarize_bars("M15", list(bars_by_tf.get("M15") or [])[-32:]),
            "H1": summarize_bars("H1", list(bars_by_tf.get("H1") or [])[-48:]),
            "H4": summarize_bars("H4", list(bars_by_tf.get("H4") or [])[-24:]),
            "D1": summarize_bars("D1", list(bars_by_tf.get("D1") or [])[-10:]),
        }

    def _safe_float(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        item = self._cache.get(key)
        if not item:
            return None
        expires_at, payload = item
        if time.time() >= expires_at:
            self._cache.pop(key, None)
            return None
        return dict(payload)

    def _cache_set(self, key: str, payload: dict[str, Any]) -> None:
        if not key:
            return
        self._cache[key] = (time.time() + self._cache_ttl_seconds, dict(payload))

    def _init_client(self):
        if nvidia_provider_enabled(api_key=self.api_key, model_name=self.model_name):
            if not self.api_key:
                self._client = None
                self._unavailable_reason = "NVIDIA_API_KEY not set"
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
                self._client = None
                self._unavailable_reason = str(exc)
            return

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
