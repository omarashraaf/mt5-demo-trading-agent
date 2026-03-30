"""
Gemini advisory layer.

Gemini is intentionally bounded here:
- It does not emit BUY/SELL execution authority.
- It only classifies news/macro context around a technical setup.
- The deterministic SmartAgent + risk pipeline remains the final authority.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv

from agent.interface import AgentInput, TradeSignal, TradingAgent
from domain.models import GeminiAssessment

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


SYSTEM_INSTRUCTION = """You are a bounded market-news analyst.

Your job is to assess news and macro context around a provided technical setup.

You MUST output ONLY valid JSON with these fields:
{
  "news_bias": "bullish" | "bearish" | "neutral",
  "macro_relevance": "low" | "medium" | "high",
  "event_risk": "low" | "medium" | "high",
  "confidence_adjustment": -0.08 to 0.05,
  "contradiction_flag": true | false,
  "summary_reason": "short plain-English explanation",
  "source_quality_score": 0.0 to 1.0
}

Rules:
- You are not deciding a trade direction on your own.
- Use the technical hypothesis as the baseline and assess whether news supports or weakens it.
- Set contradiction_flag=true when the latest catalysts materially weaken the technical case.
- High event_risk means scheduled or breaking events could invalidate the setup.
- Keep summary_reason short, specific, and auditable.
- Never output markdown or extra text outside the JSON object."""

NORMALIZED_NEWS_SYSTEM_INSTRUCTION = """You are a bounded market-news analyst.

You will be given:
- a technical hypothesis
- normalized news inputs with timestamps and affected symbols

You MUST output ONLY valid JSON with these fields:
{
  "affected_symbols": ["symbol", "..."],
  "news_bias": "bullish" | "bearish" | "neutral",
  "macro_relevance": "low" | "medium" | "high",
  "event_risk": "low" | "medium" | "high",
  "contradiction_flag": true | false,
  "confidence_adjustment": -0.08 to 0.05,
  "summary_reason": "short plain-English explanation",
  "source_quality_score": 0.0 to 1.0
}

Rules:
- Do not emit BUY/SELL orders.
- Do not decide lot size.
- Do not bypass risk checks.
- Only judge whether the supplied news supports, weakens, or clouds the existing technical thesis.
- Keep summary_reason short, specific, and auditable.
- Never output markdown or extra text outside the JSON object."""


class GeminiAgent(TradingAgent):
    """Bounded Gemini advisor for news and macro confirmation."""

    def __init__(self):
        self._client = None
        self._last_call_time: float = 0.0
        self._unavailable_reason: str = ""
        self._runtime_last_error: str = ""
        self._quota_block_until: float = 0.0
        self._init_client()

    def _init_client(self):
        if genai is None or types is None:
            self._unavailable_reason = (
                f"google-genai dependency unavailable: {GEMINI_IMPORT_ERROR}"
                if GEMINI_IMPORT_ERROR
                else "google-genai dependency unavailable"
            )
            logger.warning(self._unavailable_reason)
            self._client = None
            return

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            self._unavailable_reason = "GEMINI_API_KEY not set"
            logger.warning(self._unavailable_reason)
            self._client = None
            return

        try:
            self._client = genai.Client(api_key=api_key)
            self._unavailable_reason = ""
            logger.info("Gemini advisory agent initialized")
        except Exception as exc:
            self._unavailable_reason = str(exc)
            logger.error("Failed to initialize Gemini: %s", exc)
            self._client = None

    @property
    def name(self) -> str:
        return "GeminiAgent"

    @property
    def description(self) -> str:
        return "Bounded Gemini advisor for news catalyst quality and event-risk checks."

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Gemini advisor unavailable."

    @property
    def runtime_last_error(self) -> str:
        return self._runtime_last_error

    @property
    def degraded(self) -> bool:
        return bool(self._runtime_last_error)

    def mark_runtime_error(self, error: str):
        self._runtime_last_error = str(error or "").strip()

    def clear_runtime_error(self):
        self._runtime_last_error = ""

    def _quota_block_message(self) -> str:
        remaining = max(0, int(self._quota_block_until - time.time()))
        return f"Gemini temporarily blocked after quota exhaustion. Retry in ~{remaining}s."

    def _is_quota_blocked(self) -> bool:
        return self._quota_block_until > time.time()

    def _mark_quota_exhausted(self, cooldown_seconds: int = 300):
        self._quota_block_until = max(self._quota_block_until, time.time() + cooldown_seconds)

    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        assessment = self.assess(input_data)
        return TradeSignal(
            action="HOLD",
            confidence=max(0.0, min(1.0, 0.5 + assessment.confidence_adjustment)),
            reason=assessment.summary_reason or "Gemini advisory assessment completed.",
            strategy="gemini_advisory",
            metadata=assessment.model_dump(),
        )

    def assess(
        self,
        input_data: AgentInput,
        technical_signal: TradeSignal | None = None,
    ) -> GeminiAssessment:
        if not self._client:
            self._init_client()
            if not self._client:
                return GeminiAssessment(
                    used=False,
                    available=False,
                    degraded=False,
                    summary_reason=f"Gemini unavailable. {self.unavailable_reason}",
                    reason=f"Gemini unavailable. {self.unavailable_reason}",
                )

        if self._is_quota_blocked():
            message = self._quota_block_message()
            return GeminiAssessment(
                used=True,
                available=False,
                degraded=True,
                summary_reason="Gemini quota exhausted; using technical logic only.",
                reason="Gemini quota exhausted; using technical logic only.",
                error=message,
            )

        try:
            now = time.time()
            if now - self._last_call_time < 4.5:
                time.sleep(4.5 - (now - self._last_call_time))
            self._last_call_time = time.time()

            contents = self._build_contents(input_data, technical_signal)
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
                response_mime_type="application/json",
            )
            response = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=config,
            )
            text = (response.text or "").strip()
            assessment = self._parse_assessment(text)
            assessment.used = True
            assessment.available = True
            assessment.raw_payload = {"raw_text": text}
            self.clear_runtime_error()
            return assessment
        except Exception as exc:
            error_str = str(exc)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                logger.warning("Gemini rate limited for %s", input_data.symbol)
                self._mark_quota_exhausted()
            else:
                logger.error("Gemini advisory failed for %s: %s", input_data.symbol, exc)
            self.mark_runtime_error(error_str)
            return GeminiAssessment(
                used=True,
                available=False,
                degraded=True,
                summary_reason="Gemini assessment failed; continuing with technical logic only.",
                reason="Gemini assessment failed; continuing with technical logic only.",
                error=error_str,
            )

    def assess_news_items(
        self,
        *,
        context,
        technical_signal: TradeSignal | None,
        normalized_news: list[dict],
    ) -> GeminiAssessment:
        if not self._client:
            self._init_client()
            if not self._client:
                return GeminiAssessment(
                    used=False,
                    available=False,
                    degraded=False,
                    summary_reason=f"Gemini unavailable. {self.unavailable_reason}",
                    reason=f"Gemini unavailable. {self.unavailable_reason}",
                    affected_symbols=[context.symbol],
                )

        if self._is_quota_blocked():
            message = self._quota_block_message()
            return GeminiAssessment(
                used=True,
                available=False,
                degraded=True,
                summary_reason="Gemini quota exhausted; using technical logic only.",
                reason="Gemini quota exhausted; using technical logic only.",
                error=message,
                affected_symbols=[context.symbol],
            )

        try:
            now = time.time()
            if now - self._last_call_time < 4.5:
                time.sleep(4.5 - (now - self._last_call_time))
            self._last_call_time = time.time()

            config = types.GenerateContentConfig(
                system_instruction=NORMALIZED_NEWS_SYSTEM_INSTRUCTION,
                temperature=0.1,
                response_mime_type="application/json",
            )
            response = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=self._build_normalized_news_contents(context, technical_signal, normalized_news),
                config=config,
            )
            text = (response.text or "").strip()
            assessment = self._parse_assessment(text)
            assessment.used = True
            assessment.available = True
            assessment.affected_symbols = assessment.affected_symbols or [context.symbol]
            assessment.raw_payload = {
                "raw_text": text,
                "normalized_news_count": len(normalized_news),
            }
            self.clear_runtime_error()
            return assessment
        except Exception as exc:
            error_str = str(exc)
            logger.error("Gemini normalized-news assessment failed for %s: %s", context.symbol, exc)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                self._mark_quota_exhausted()
            self.mark_runtime_error(error_str)
            return GeminiAssessment(
                used=True,
                available=False,
                degraded=True,
                summary_reason="Gemini news analysis failed; continuing with technical logic only.",
                reason="Gemini news analysis failed; continuing with technical logic only.",
                error=error_str,
                affected_symbols=[context.symbol],
            )

    def _build_contents(
        self,
        input_data: AgentInput,
        technical_signal: TradeSignal | None,
    ) -> list:
        market_summary = self._build_market_summary(input_data)
        technical_summary = (
            f"Technical hypothesis: action={technical_signal.action}, "
            f"confidence={technical_signal.confidence:.2f}, "
            f"reason={technical_signal.reason}"
            if technical_signal
            else "Technical hypothesis not provided."
        )
        prompt_text = f"""Assess {input_data.symbol} right now.

{technical_summary}

Market context:
{market_summary}

Use Google Search to identify the most recent relevant catalysts.
Return only the required JSON schema."""

        return [types.Content(role="user", parts=[types.Part(text=prompt_text)])]

    def _build_market_summary(self, input_data: AgentInput) -> str:
        mtf = input_data.multi_tf_bars or {}
        h4_bars = mtf.get("H4", [])
        h1_bars = mtf.get("H1", input_data.bars)
        m15_bars = mtf.get("M15", [])
        if not h1_bars:
            return "Insufficient market data."

        current_price = h1_bars[-1]["close"]
        atr = self._calc_atr(h1_bars)
        atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0
        closes = [b["close"] for b in h1_bars]
        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        h1_trend = "bullish" if ema20[-1] > ema50[-1] else "bearish" if ema20[-1] < ema50[-1] else "flat"
        h4_trend = "n/a"
        if len(h4_bars) >= 20:
            h4_closes = [b["close"] for b in h4_bars]
            h4_ema20 = self._ema(h4_closes, 20)
            h4_ema50 = self._ema(h4_closes, 50) if len(h4_closes) >= 50 else h4_ema20
            h4_trend = "bullish" if h4_ema20[-1] > h4_ema50[-1] else "bearish" if h4_ema20[-1] < h4_ema50[-1] else "flat"
        m15_rsi = self._calc_rsi([b["close"] for b in m15_bars]) if len(m15_bars) >= 15 else 50.0

        return (
            f"Price={current_price:.5f}, H1 trend={h1_trend}, H4 trend={h4_trend}, "
            f"H1 RSI={self._calc_rsi(closes):.1f}, M15 RSI={m15_rsi:.1f}, "
            f"ATR%={atr_pct:.2f}, Spread={input_data.spread}, Open positions={len(input_data.open_positions)}"
        )

    def _build_normalized_news_contents(
        self,
        context,
        technical_signal: TradeSignal | None,
        normalized_news: list[dict],
    ) -> list:
        technical_summary = (
            f"Technical hypothesis: action={technical_signal.action}, "
            f"confidence={technical_signal.confidence:.2f}, reason={technical_signal.reason}"
            if technical_signal
            else "Technical hypothesis not provided."
        )
        compact_news = []
        for item in normalized_news[:12]:
            compact_news.append({
                "source": item.get("source"),
                "category": item.get("category"),
                "title": item.get("title"),
                "summary": item.get("summary"),
                "published_at": item.get("published_at"),
                "affected_symbols": item.get("affected_symbols", []),
            })

        prompt_text = f"""Assess normalized news for {context.symbol}.

{technical_summary}

Symbol context:
- category: {context.symbol_info.category if context.symbol_info else 'Other'}
- spread: {context.tick.get('spread', 0.0) if context.tick else 0.0}
- profile: {context.profile.profile_name if context.profile else 'Unknown'}

Normalized news inputs:
{json.dumps(compact_news, ensure_ascii=True)}

Return only the required JSON schema."""

        return [types.Content(role="user", parts=[types.Part(text=prompt_text)])]

    def _parse_assessment(self, text: str) -> GeminiAssessment:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed Gemini JSON: {cleaned[:200]}") from exc

        news_bias = str(data.get("news_bias", "neutral")).lower()
        if news_bias not in {"bullish", "bearish", "neutral"}:
            news_bias = "neutral"
        macro_relevance = str(data.get("macro_relevance", "low")).lower()
        if macro_relevance not in {"low", "medium", "high"}:
            macro_relevance = "low"
        event_risk = str(data.get("event_risk", "low")).lower()
        if event_risk not in {"low", "medium", "high"}:
            event_risk = "low"
        confidence_adjustment = max(-0.08, min(0.05, float(data.get("confidence_adjustment", 0.0))))
        contradiction_flag = bool(data.get("contradiction_flag", False))
        summary_reason = str(data.get("summary_reason", ""))
        source_quality_score = max(0.0, min(1.0, float(data.get("source_quality_score", 0.0))))
        affected_symbols = [
            str(symbol).upper()
            for symbol in data.get("affected_symbols", [])
            if str(symbol).strip()
        ]

        return GeminiAssessment(
            used=True,
            available=True,
            degraded=False,
            confirmed=news_bias != "neutral" and not contradiction_flag,
            contradicted=contradiction_flag,
            news_bias=news_bias,
            macro_relevance=macro_relevance,
            event_risk=event_risk,
            confidence_adjustment=confidence_adjustment,
            contradiction_flag=contradiction_flag,
            summary_reason=summary_reason,
            source_quality_score=source_quality_score,
            affected_symbols=affected_symbols,
            advisory_action="HOLD",
            advisory_confidence=source_quality_score,
            confidence_delta=confidence_adjustment,
            reason=summary_reason,
        )

    def _ema(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return data[:]
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        return [0.0] * (len(data) - len(ema)) + ema

    def _calc_rsi(self, closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas[-period:]]
        losses = [max(0, -d) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1 + avg_gain / avg_loss))

    def _calc_atr(self, bars: list[dict], period: int = 14) -> float:
        if len(bars) < 2:
            return 0.0001
        true_ranges = []
        for i in range(1, len(bars)):
            high = bars[i]["high"]
            low = bars[i]["low"]
            prev_close = bars[i - 1]["close"]
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(true_ranges[-period:]) / min(period, len(true_ranges))
