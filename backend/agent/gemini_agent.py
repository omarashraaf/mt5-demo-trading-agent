"""
Gemini 3 AI Trading Agent — Real-Time Commodity Quant

Uses Google Gemini 3 with:
- Google Search grounding for live news (OPEC, Fed, Geopolitics)
- Thought signature circulation for maintained reasoning quality
- Structured JSON output for reliable parsing
- BUY + SELL signals for commodity trading
- 1:2 Risk/Reward targeting with ATR-based SL/TP
"""

import os
import json
import logging
import time
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types

from agent.interface import TradingAgent, AgentInput, TradeSignal

logger = logging.getLogger(__name__)

# System instruction: commodity quant persona
SYSTEM_INSTRUCTION = """You are a Real-Time Commodity & Financial Markets Quant Analyst.

Your job: Analyze the provided market data AND use Google Search to find the LATEST high-impact news
(OPEC decisions, Fed rate changes, geopolitical events, earnings, sanctions, supply disruptions)
that affect the given symbol.

You MUST output ONLY valid JSON with these fields:
{
  "action": "BUY" or "SELL" or "HOLD",
  "confidence": 0.0 to 1.0,
  "sentiment_score": -1.0 to 1.0,
  "volatility_index": 1 to 10,
  "support_levels": [price1, price2],
  "resistance_levels": [price1, price2],
  "trend_bias": "Bullish" or "Bearish" or "Neutral",
  "stop_loss_atr_mult": 1.0 to 2.5,
  "take_profit_atr_mult": 1.5 to 4.0,
  "risk_reward_ratio": 1.5 to 3.0,
  "reason": "2-3 sentence explanation including any relevant current news.",
  "news_catalyst": "Brief description of the most impactful current news for this asset",
  "risk_level": "low" or "medium" or "high"
}

Rules:
- BUY when sentiment > 0.3 AND price near support AND trend is Bullish
- SELL when sentiment < -0.3 AND price near resistance AND trend is Bearish
- HOLD when uncertain or no clear setup
- Always target minimum 1:1.5 Risk/Reward ratio
- Be aggressive on commodities (Gold, Oil, Silver) when OPEC/Fed news aligns
- For Forex, watch central bank divergence
- For indices, watch earnings season and macro data
- NEVER output anything except the JSON object. No markdown, no explanation outside JSON."""


class GeminiAgent(TradingAgent):
    """AI-powered trading agent using Google Gemini 3 with search grounding."""

    def __init__(self):
        self._client = None
        self._thought_history: dict[str, list] = {}  # symbol -> chat history with thought signatures
        self._last_call_time: float = 0
        self._init_client()

    def _init_client(self):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set - Gemini agent will not work")
            return
        try:
            self._client = genai.Client(api_key=api_key)
            logger.info("Gemini 3 AI agent initialized (with Google Search grounding)")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini: {e}")
            self._client = None

    @property
    def name(self) -> str:
        return "GeminiAgent"

    @property
    def description(self) -> str:
        return "Gemini 3 AI with Google Search grounding for real-time news + commodity trading."

    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        if not self._client:
            self._init_client()
            if not self._client:
                return TradeSignal(
                    action="HOLD", confidence=0.0,
                    reason="Gemini AI is not configured. Please check your GEMINI_API_KEY.",
                )

        try:
            # Rate limit: respect Gemini quotas
            now = time.time()
            if now - self._last_call_time < 4.5:
                time.sleep(4.5 - (now - self._last_call_time))
            self._last_call_time = time.time()

            market_summary = self._build_market_summary(input_data)
            contents = self._build_contents(input_data.symbol, market_summary, input_data)

            # Gemini 3 config: low temperature, JSON output, Google Search grounding
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
                response_mime_type="application/json",
            )

            response = self._client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=contents,
                config=config,
            )

            # Extract and store thought signatures for this symbol
            self._store_thought_signatures(input_data.symbol, response)

            text = response.text.strip()
            logger.info(f"Gemini 3 OK for {input_data.symbol}: {text[:150]}...")

            return self._parse_response(text, input_data)

        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                logger.warning(f"Gemini rate limited for {input_data.symbol}, using smart fallback")
            else:
                logger.error(f"Gemini 3 failed for {input_data.symbol}: {e}")
            return self._fallback_analysis(input_data)

    def _build_contents(self, symbol: str, market_summary: str, input_data: AgentInput) -> list:
        """Build contents list with thought signature history for Gemini 3."""
        prompt_text = f"""Analyze {symbol} for a trading decision NOW.

LIVE MARKET DATA:
{market_summary}

ACCOUNT: ${input_data.account_equity:.2f} equity, {len(input_data.open_positions)} open positions

Use Google Search to find the LATEST news affecting {symbol} right now.
Output your analysis as the required JSON."""

        # Build contents with prior thought signatures if available
        contents = []

        # Include previous thought signatures to maintain reasoning chain
        prior = self._thought_history.get(symbol, [])
        if prior:
            # Only keep last exchange to avoid context bloat
            contents.extend(prior[-2:])

        # Add new user message
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=prompt_text)],
        ))

        return contents

    def _store_thought_signatures(self, symbol: str, response):
        """Store thought signatures from Gemini 3 response for future context."""
        try:
            if not response.candidates:
                return

            candidate = response.candidates[0]
            if not candidate.content or not candidate.content.parts:
                return

            # Build the model response content preserving thought signatures
            preserved_parts = []
            for part in candidate.content.parts:
                # Keep thought signature parts — Gemini 3 needs these circulated back
                if hasattr(part, 'thought_signature') and part.thought_signature:
                    preserved_parts.append(types.Part(thought_signature=part.thought_signature))
                elif hasattr(part, 'text') and part.text:
                    preserved_parts.append(types.Part(text=part.text))

            if preserved_parts:
                # Store the user prompt + model response for next call
                model_content = types.Content(role="model", parts=preserved_parts)
                self._thought_history[symbol] = [model_content]

                # Cap history to prevent memory growth (keep last 2 exchanges)
                if len(self._thought_history) > 50:
                    oldest = sorted(self._thought_history.keys())[0]
                    del self._thought_history[oldest]

        except Exception as e:
            logger.debug(f"Could not store thought signatures for {symbol}: {e}")

    def _build_market_summary(self, input_data: AgentInput) -> str:
        mtf = input_data.multi_tf_bars or {}
        h4_bars = mtf.get("H4", [])
        h1_bars = mtf.get("H1", input_data.bars)
        m15_bars = mtf.get("M15", [])

        if not h1_bars or len(h1_bars) < 10:
            return "Insufficient price data available."

        current_price = h1_bars[-1]["close"]
        high_24h = max(b["high"] for b in h1_bars[-24:]) if len(h1_bars) >= 24 else max(b["high"] for b in h1_bars)
        low_24h = min(b["low"] for b in h1_bars[-24:]) if len(h1_bars) >= 24 else min(b["low"] for b in h1_bars)

        change_24h = 0
        if len(h1_bars) >= 24:
            change_24h = ((current_price - h1_bars[-24]["close"]) / h1_bars[-24]["close"]) * 100

        closes = [b["close"] for b in h1_bars]
        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        rsi = self._calc_rsi(closes)
        atr = self._calc_atr(h1_bars)
        volatility_pct = (atr / current_price * 100) if current_price > 0 else 0

        # H4 trend
        h4_trend = "N/A"
        if h4_bars and len(h4_bars) >= 20:
            h4_closes = [b["close"] for b in h4_bars]
            h4_ema20 = self._ema(h4_closes, 20)
            h4_ema50 = self._ema(h4_closes, 50) if len(h4_closes) >= 50 else h4_ema20
            h4_trend = "Bullish" if h4_ema20[-1] > h4_ema50[-1] else "Bearish" if h4_ema20[-1] < h4_ema50[-1] else "Flat"

        # M15 momentum
        m15_momentum = "N/A"
        if m15_bars and len(m15_bars) >= 5:
            m15_closes = [b["close"] for b in m15_bars]
            m15_rsi = self._calc_rsi(m15_closes)
            m15_momentum = f"RSI={m15_rsi:.0f}, {'Bullish' if m15_closes[-1] > m15_closes[-3] else 'Bearish'}"

        # Recent candle pattern
        last_candles = []
        for b in h1_bars[-5:]:
            body_pct = abs(b["close"] - b["open"]) / max(b["high"] - b["low"], 0.0001) * 100
            color = "green" if b["close"] > b["open"] else "red"
            last_candles.append(f"{color}({body_pct:.0f}%)")

        # Support/Resistance from recent H1 data
        highs = sorted([b["high"] for b in h1_bars[-30:]], reverse=True)
        lows = sorted([b["low"] for b in h1_bars[-30:]])
        resistance = highs[0] if highs else current_price
        support = lows[0] if lows else current_price

        return f"""Symbol: {input_data.symbol}
Current Price: {current_price}
24h Change: {change_24h:+.2f}%
24h Range: {low_24h} - {high_24h}
H1 EMA20: {ema20[-1]:.5f} (price {'above' if current_price > ema20[-1] else 'below'})
H1 EMA50: {ema50[-1]:.5f} (price {'above' if current_price > ema50[-1] else 'below'})
H1 RSI(14): {rsi:.1f}
H1 ATR(14): {atr:.5f} ({volatility_pct:.2f}% volatility)
H4 Trend: {h4_trend}
M15 Momentum: {m15_momentum}
Spread: {input_data.spread}
Recent Support: {support:.5f}
Recent Resistance: {resistance:.5f}
Last 5 H1 candles: {', '.join(last_candles)}
H1 Trend: {'Uptrend' if ema20[-1] > ema50[-1] else 'Downtrend' if ema20[-1] < ema50[-1] else 'Sideways'}"""

    def _parse_response(self, text: str, input_data: AgentInput) -> TradeSignal:
        # Clean potential markdown wrapping
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Gemini 3 response: {text[:200]}")
            return self._fallback_analysis(input_data)

        action = data.get("action", "HOLD").upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        sentiment = float(data.get("sentiment_score", 0))
        trend_bias = data.get("trend_bias", "Neutral")
        news_catalyst = data.get("news_catalyst", "")
        risk_level = data.get("risk_level", "medium")
        reason = data.get("reason", "AI analysis complete.")

        # Build rich reason string
        reason_parts = [reason]
        if news_catalyst:
            reason_parts.append(f"News: {news_catalyst}")
        reason_parts.append(f"[Sentiment: {sentiment:+.1f} | Trend: {trend_bias} | Risk: {risk_level}]")
        full_reason = " ".join(reason_parts)

        # Calculate SL/TP using ATR multipliers from Gemini
        current_price = input_data.bars[-1]["close"] if input_data.bars else 0
        atr = self._calc_atr(input_data.bars) if input_data.bars else 0

        sl_mult = float(data.get("stop_loss_atr_mult", 1.5))
        tp_mult = float(data.get("take_profit_atr_mult", 2.0))

        # Clamp multipliers to reasonable range
        sl_mult = max(0.8, min(3.0, sl_mult))
        tp_mult = max(1.0, min(4.0, tp_mult))

        if action == "BUY" and current_price > 0:
            stop_loss = round(current_price - atr * sl_mult, 5)
            take_profit = round(current_price + atr * tp_mult, 5)
        elif action == "SELL" and current_price > 0:
            stop_loss = round(current_price + atr * sl_mult, 5)
            take_profit = round(current_price - atr * tp_mult, 5)
        else:
            stop_loss = None
            take_profit = None

        return TradeSignal(
            action=action,
            confidence=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_holding_minutes=360,
            reason=full_reason,
            strategy="gemini_quant",
        )

    def _fallback_analysis(self, input_data: AgentInput) -> TradeSignal:
        """Smart fallback when Gemini API is unavailable — uses local technical analysis."""
        bars = input_data.bars
        if not bars or len(bars) < 20:
            return TradeSignal(action="HOLD", confidence=0.0, reason="Not enough data for analysis.")

        closes = [b["close"] for b in bars]
        current = closes[-1]
        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        rsi = self._calc_rsi(closes)
        atr = self._calc_atr(bars)

        # Bullish setup
        if ema20[-1] > ema50[-1] and 40 < rsi < 70:
            return TradeSignal(
                action="BUY", confidence=0.55,
                stop_loss=round(current - atr * 1.5, 5),
                take_profit=round(current + atr * 2.0, 5),
                max_holding_minutes=360,
                reason="Fallback analysis: Price above moving averages with healthy momentum. Bullish setup detected.",
                strategy="gemini_fallback",
            )

        # Bearish setup
        if ema20[-1] < ema50[-1] and 30 < rsi < 60:
            return TradeSignal(
                action="SELL", confidence=0.55,
                stop_loss=round(current + atr * 1.5, 5),
                take_profit=round(current - atr * 2.0, 5),
                max_holding_minutes=360,
                reason="Fallback analysis: Price below moving averages with bearish momentum. Short setup detected.",
                strategy="gemini_fallback",
            )

        return TradeSignal(
            action="HOLD", confidence=0.3,
            reason="No clear setup. AI is watching for a better entry point.",
            strategy="gemini_fallback",
        )

    # --- Technical indicators ---

    def _ema(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return data[:]
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        return [0.0] * (len(data) - len(ema)) + ema

    def _calc_rsi(self, closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas[-period:]]
        losses = [max(0, -d) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    def _calc_atr(self, bars: list, period: int = 14) -> float:
        if len(bars) < 2:
            return 0.0001
        true_ranges = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(true_ranges[-period:]) / min(period, len(true_ranges))
