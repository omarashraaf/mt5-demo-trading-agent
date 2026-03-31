from agent.interface import TradingAgent, AgentInput, TradeSignal
from typing import Optional

# Asset classification for mixed strategy
STOCKS = {
    "NVDA", "AMD", "MSFT", "INTC", "AAPL", "GOOG", "GOOGL", "AMZN", "TSLA", "META",
    "NFLX", "AVGO", "CRM", "ORCL", "ADBE", "QCOM", "PYPL", "BA", "DIS", "JPM",
    "V", "MA", "WMT", "KO", "MCD", "NKE", "COST", "PEP", "CSCO",
}
INDICES = {
    "US500", "SPX500", "US100", "NAS100", "US30", "GER40", "UK100", "JPN225", "AUS200",
}
COMMODITIES = {
    "XAUUSD", "XAGUSD", "XAUEUR", "XAGEUR", "XPTUSD", "XPDUSD",  # Metals
    "GLD", "SLV", "USO", "DBC",  # Commodity ETFs (oil, gold, silver, broad)
    "GOLD", "WTI", "BRENT", "USOIL", "UKOIL",
}
# Everything else is forex


def get_asset_class(symbol: str) -> str:
    """Classify symbol into asset class for strategy selection."""
    if symbol in STOCKS:
        return "stock"
    if symbol in INDICES:
        return "index"
    if symbol in COMMODITIES:
        return "commodity"
    return "forex"


def get_trade_params(asset_class: str) -> dict:
    """Return SL/TP multipliers and hold times per asset class.

    Forex = day trade (tight SL/TP, short hold)
    Stocks & Commodities = swing trade (wider SL/TP, longer hold)
    """
    if asset_class == "stock":
        return {
            "sl_mult_entry": 2.0,    # SL for stocks with entry signal
            "tp_mult_entry": 4.0,    # 2:1 R:R with entry
            "sl_mult_no_entry": 1.8,  # Tighter SL without entry
            "tp_mult_no_entry": 3.6,  # 2:1 R:R without entry
            "max_hold_minutes": 4320,  # 3 days
            "label": "Swing Trade",
        }
    elif asset_class == "commodity":
        return {
            "sl_mult_entry": 1.8,
            "tp_mult_entry": 3.6,    # 2:1 R:R
            "sl_mult_no_entry": 1.5,
            "tp_mult_no_entry": 3.0,  # 2:1 R:R
            "max_hold_minutes": 2880,  # 2 days
            "label": "Swing Trade",
        }
    elif asset_class == "index":
        return {
            "sl_mult_entry": 1.6,
            "tp_mult_entry": 3.2,
            "sl_mult_no_entry": 1.4,
            "tp_mult_no_entry": 2.8,
            "max_hold_minutes": 720,
            "label": "Event Trade",
        }
    else:  # forex
        return {
            "sl_mult_entry": 1.2,    # Tight SL for day trades
            "tp_mult_entry": 2.4,    # 2:1 R:R — closer TP = hit faster
            "sl_mult_no_entry": 1.0,  # Even tighter without entry
            "tp_mult_no_entry": 2.0,  # 2:1 R:R
            "max_hold_minutes": 360,  # 6 hours
            "label": "Day Trade",
        }


class SmartAgent(TradingAgent):
    """Multi-timeframe analysis agent with mixed trading strategy.

    Forex = Day trading: H4 trend, H1 momentum, M15 entry, tight SL/TP, 6h max hold.
    Stocks & Gold = Swing trading: wider SL/TP, hold for days, ride trends.
    """

    @property
    def name(self) -> str:
        return "SmartAgent"

    @property
    def description(self) -> str:
        return "AI-powered mixed strategy: day trade forex, swing trade stocks & gold."

    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        policy_mode = str(getattr(input_data, "policy_mode", "balanced") or "balanced").lower()
        mtf = input_data.multi_tf_bars or {}
        h4_bars = mtf.get("H4", [])
        h1_bars = mtf.get("H1", [])
        m15_bars = mtf.get("M15", input_data.bars)

        # Need minimum data
        if len(h1_bars) < 52 or len(m15_bars) < 22:
            return TradeSignal(
                action="HOLD", confidence=0.0,
                reason="Not enough market data yet. Waiting for more price history to analyze.",
            )

        # Step 1: H4/H1 trend agreement via EMA
        h1_closes = [b["close"] for b in h1_bars]
        h1_ema20 = self._ema(h1_closes, 20)
        h1_ema50 = self._ema(h1_closes, 50)
        h1_trend = self._classify_trend(h1_ema20, h1_ema50)

        if len(h4_bars) >= 52:
            h4_closes = [b["close"] for b in h4_bars]
            h4_ema20 = self._ema(h4_closes, 20)
            h4_ema50 = self._ema(h4_closes, 50)
            h4_trend = self._classify_trend(h4_ema20, h4_ema50)
        else:
            h4_trend = h1_trend

        trend = h1_trend
        trend_score = 0.0
        if h1_trend == h4_trend and h1_trend in {"bullish", "bearish"}:
            trend_score = 0.2
        elif h1_trend in {"bullish", "bearish"}:
            trend_score = 0.08

        asset_class = get_asset_class(input_data.symbol)

        # Step 2: H1 momentum via RSI
        closes_h1 = h1_closes
        rsi = self._rsi(closes_h1, 14)
        current_rsi = rsi[-1] if rsi else 50.0

        momentum = "neutral"
        momentum_score = 0.0
        bull_upper = 72 if asset_class in {"stock", "index"} else 65
        bear_lower = 28 if asset_class in {"stock", "index"} else 35
        exhaustion_high = 80 if asset_class in {"stock", "index"} else 75
        exhaustion_low = 20 if asset_class in {"stock", "index"} else 25
        if trend == "bullish" and 45 < current_rsi < bull_upper:
            momentum = "bullish_pullback"
            momentum_score = 0.15
        elif trend == "bearish" and bear_lower < current_rsi < 55:
            momentum = "bearish_pullback"
            momentum_score = 0.15
        elif asset_class in {"stock", "index"} and trend == "bullish" and bull_upper <= current_rsi < exhaustion_high:
            momentum = "bullish_trend_persistence"
            momentum_score = 0.08
        elif asset_class in {"stock", "index"} and trend == "bearish" and exhaustion_low < current_rsi <= bear_lower:
            momentum = "bearish_trend_persistence"
            momentum_score = 0.08
        # Exhaustion zones reduce confidence
        if current_rsi > exhaustion_high or current_rsi < exhaustion_low:
            momentum = "exhausted"
            momentum_score = -0.1

        # Step 3: M15 entry trigger
        entry_signal, entry_score = self._detect_entry(m15_bars, h1_bars, trend, asset_class)

        # Step 4: Support/Resistance confirmation
        sr_score, position_in_range = self._check_sr_alignment(h1_bars, m15_bars[-1]["close"], trend)

        # Step 5: Volume confirmation
        volume_score = 0.0
        if len(m15_bars) >= 21:
            recent_vol = m15_bars[-1].get("volume", 0)
            avg_vol = sum(b.get("volume", 0) for b in m15_bars[-21:-1]) / 20
            if avg_vol > 0 and recent_vol > avg_vol * 1.25:
                volume_score = 0.05

        # Step 6: Calculate ATR for SL/TP
        atr = self._atr(h1_bars, 14)
        last_close = m15_bars[-1]["close"]
        atr_pct = (atr / last_close) * 100 if last_close > 0 else 0.0
        m15_closes = [b["close"] for b in m15_bars]
        m15_ema20 = self._ema(m15_closes, 20)
        ema_distance_atr = abs(last_close - m15_ema20[-1]) / max(atr, 0.0000001)

        # Aggregate confidence
        confidence = 0.25 + trend_score + momentum_score + entry_score + sr_score + volume_score
        confidence = round(max(0.0, min(0.95, confidence)), 2)
        trend_conflict = h1_trend != h4_trend and h1_trend != "flat" and h4_trend != "flat"

        # Position awareness — don't stack same-direction trades
        for pos in input_data.open_positions:
            pos_type = pos.get("type", "")
            if pos.get("symbol") == input_data.symbol:
                if trend == "bullish" and pos_type == "BUY":
                    return TradeSignal(
                        action="HOLD", confidence=confidence, strategy="trend_follow",
                        reason=f"Already have a BUY position on {input_data.symbol}. Monitoring existing trade.",
                    )
                if trend == "bearish" and pos_type == "SELL":
                    return TradeSignal(
                        action="HOLD", confidence=confidence, strategy="trend_follow",
                        reason=f"Already have a SELL position on {input_data.symbol}. Monitoring existing trade.",
                    )

        # Determine action — BUY and SELL
        # With entry trigger: trade only when confidence clears a stricter bar.
        has_entry = entry_signal not in ("none", "")

        # Get asset-specific parameters (forex=day trade, stocks/gold=swing trade)
        params = get_trade_params(asset_class)
        min_entry_conf, min_non_entry_conf = self._confidence_thresholds(asset_class, has_entry, policy_mode)
        metadata = {
            "direction": trend if trend in {"bullish", "bearish"} else "flat",
            "asset_class": asset_class,
            "technical_confidence": round(confidence, 3),
            "h1_trend": h1_trend,
            "h4_trend": h4_trend,
            "trend_alignment": round(1.0 if h1_trend == h4_trend and h1_trend in {"bullish", "bearish"} else 0.5 if h1_trend == trend or h4_trend == trend else 0.0, 3),
            "trend_score": round(trend_score, 3),
            "momentum": momentum,
            "momentum_score": round(momentum_score, 3),
            "entry_signal": entry_signal,
            "entry_score": round(entry_score, 3),
            "entry_quality": round(max(0.0, min(1.0, 0.45 + entry_score * 2.5 + sr_score * 1.5 + volume_score)), 3),
            "sr_score": round(sr_score, 3),
            "volume_score": round(volume_score, 3),
            "atr": round(atr, 6),
            "atr_pct": round(atr_pct, 4),
            "position_in_range": round(position_in_range, 4),
            "ema_distance_atr": round(ema_distance_atr, 4),
            "trend_conflict": trend_conflict,
        }

        if trend_conflict and policy_mode != "aggressive":
            return TradeSignal(
                action="HOLD",
                confidence=max(0.0, confidence - 0.08),
                strategy="trend_follow",
                reason="No trade. H1 and H4 trend structure disagree, so the setup is too noisy.",
                metadata=metadata,
            )
        if trend_conflict and policy_mode == "aggressive":
            # In aggressive mode we allow H1-led continuation attempts even with H4 disagreement.
            confidence = round(max(0.0, min(0.95, confidence - 0.03)), 2)
            metadata["aggressive_trend_override"] = True

        if trend == "bullish" and (
            (has_entry and confidence >= min_entry_conf) or confidence >= min_non_entry_conf
        ):
            action = "BUY"
            sl_mult = params["sl_mult_entry"] if has_entry else params["sl_mult_no_entry"]
            tp_mult = params["tp_mult_entry"] if has_entry else params["tp_mult_no_entry"]
            sl = round(last_close - atr * sl_mult, 5)
            tp = round(last_close + atr * tp_mult, 5)
        elif trend == "bearish" and (
            (has_entry and confidence >= min_entry_conf) or confidence >= min_non_entry_conf
        ):
            action = "SELL"
            sl_mult = params["sl_mult_entry"] if has_entry else params["sl_mult_no_entry"]
            tp_mult = params["tp_mult_entry"] if has_entry else params["tp_mult_no_entry"]
            sl = round(last_close + atr * sl_mult, 5)
            tp = round(last_close - atr * tp_mult, 5)
        else:
            return TradeSignal(
                action="HOLD", confidence=confidence, strategy="trend_follow",
                reason=self._build_hold_reason(trend, momentum, current_rsi, entry_signal),
                metadata=metadata,
            )

        rr_ratio = abs(tp - last_close) / max(abs(last_close - sl), 0.0000001)
        metadata["reward_risk_ratio"] = round(rr_ratio, 3)
        metadata["reward_risk"] = round(rr_ratio, 3)
        metadata["late_entry"] = ema_distance_atr >= 1.1

        reason = self._build_reason(
            action, trend, momentum, current_rsi, entry_signal,
            atr, last_close, sl, tp, confidence, params["label"],
        )

        return TradeSignal(
            action=action,
            confidence=confidence,
            stop_loss=sl,
            take_profit=tp,
            max_holding_minutes=params["max_hold_minutes"],
            reason=reason,
            strategy=f"{params['label'].lower().replace(' ', '_')}_{asset_class}",
            metadata=metadata,
        )

    # --- Technical indicators ---

    def _ema(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return data[:]
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        # Pad the beginning
        return [0.0] * (len(data) - len(ema)) + ema

    def _rsi(self, closes: list[float], period: int = 14) -> list[float]:
        if len(closes) < period + 1:
            return [50.0] * len(closes)
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas]
        losses = [max(0, -d) for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_values = [0.0] * period
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(100.0 - (100.0 / (1.0 + rs)))
        return rsi_values

    def _atr(self, bars: list[dict], period: int = 14) -> float:
        if len(bars) < period + 1:
            ranges = [b["high"] - b["low"] for b in bars[-period:]]
            return sum(ranges) / len(ranges) if ranges else 0.0001
        true_ranges = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(true_ranges[-period:]) / period

    # --- Entry detection ---

    def _detect_entry(self, m15_bars: list[dict], h1_bars: list[dict], trend: str, asset_class: str) -> tuple[str, float]:
        """Look for pullback + reversal candle on M15."""
        if len(m15_bars) < 22:
            return "none", 0.0

        closes = [b["close"] for b in m15_bars]
        ema20 = self._ema(closes, 20)

        last = m15_bars[-1]
        prev = m15_bars[-2]
        body = last["close"] - last["open"]
        prev_body = prev["close"] - prev["open"]

        near_ema = abs(last["close"] - ema20[-1]) < abs(last["high"] - last["low"]) * 0.5

        if trend == "bullish":
            # Bullish engulfing or strong green candle near EMA
            if body > 0 and prev_body < 0 and abs(body) > abs(prev_body) * 0.8 and near_ema:
                return "buy", 0.1
            if body > 0 and last["low"] <= ema20[-1] * 1.001:
                return "neutral_lean_buy", 0.05
        elif trend == "bearish":
            # Bearish engulfing or strong red candle near EMA
            if body < 0 and prev_body > 0 and abs(body) > abs(prev_body) * 0.8 and near_ema:
                return "sell", 0.1
            if body < 0 and last["high"] >= ema20[-1] * 0.999:
                return "neutral_lean_sell", 0.05

        # Stocks, indices, and commodities often trend without giving clean M15 pullbacks.
        # Allow a continuation entry only when higher-timeframe structure is already strong
        # and the move is not too extended versus ATR.
        if asset_class in {"stock", "index", "commodity"} and len(h1_bars) >= 30:
            h1_closes = [b["close"] for b in h1_bars]
            h1_ema20 = self._ema(h1_closes, 20)
            h1_ema50 = self._ema(h1_closes, 50)
            h1_body = h1_bars[-1]["close"] - h1_bars[-1]["open"]
            h1_range = max(h1_bars[-1]["high"] - h1_bars[-1]["low"], 0.0000001)
            h1_body_ratio = abs(h1_body) / h1_range
            m15_atr = self._atr(m15_bars, 14)
            extended = abs(last["close"] - ema20[-1]) / max(m15_atr, 0.0000001) > 1.55
            if trend == "bullish":
                if (
                    h1_ema20[-1] > h1_ema50[-1]
                    and h1_bars[-1]["close"] > h1_ema20[-1]
                    and h1_body > 0
                    and h1_body_ratio >= 0.35
                    and not extended
                ):
                    return "trend_continuation_buy", 0.08
            elif trend == "bearish":
                if (
                    h1_ema20[-1] < h1_ema50[-1]
                    and h1_bars[-1]["close"] < h1_ema20[-1]
                    and h1_body < 0
                    and h1_body_ratio >= 0.35
                    and not extended
                ):
                    return "trend_continuation_sell", 0.08

        return "none", 0.0

    def _confidence_thresholds(self, asset_class: str, has_entry: bool, policy_mode: str = "balanced") -> tuple[float, float]:
        if asset_class == "stock":
            entry, non_entry = 0.56, 0.62
        elif asset_class == "index":
            entry, non_entry = 0.55, 0.60
        elif asset_class == "commodity":
            entry, non_entry = 0.56, 0.61
        else:
            entry, non_entry = 0.60, 0.67

        mode = (policy_mode or "balanced").lower()
        if mode == "aggressive":
            entry -= 0.12
            non_entry -= 0.15
        elif mode == "safe":
            entry += 0.06
            non_entry += 0.07
        else:
            entry += 0.00
            non_entry += 0.00

        entry = max(0.40, min(0.90, entry))
        non_entry = max(entry, min(0.92, non_entry))
        return round(entry, 2), round(non_entry, 2)

    def _check_sr_alignment(self, h1_bars: list[dict], current_price: float, trend: str) -> tuple[float, float]:
        """Check if price is near a support (for buys) or resistance (for sells)."""
        if len(h1_bars) < 20:
            return 0.0, 0.5

        highs = [b["high"] for b in h1_bars[-30:]]
        lows = [b["low"] for b in h1_bars[-30:]]
        recent_high = max(highs)
        recent_low = min(lows)
        range_size = recent_high - recent_low
        if range_size <= 0:
            return 0.0, 0.5

        position_in_range = (current_price - recent_low) / range_size
        # For buys: price near lower third = good support zone
        if trend == "bullish":
            if position_in_range < 0.4:
                return 0.1, position_in_range
        # For sells: price near upper third = good resistance zone
        elif trend == "bearish":
            if position_in_range > 0.6:
                return 0.1, position_in_range
        return 0.0, position_in_range

    def _classify_trend(self, ema20: list[float], ema50: list[float]) -> str:
        if len(ema20) < 2 or len(ema50) < 2:
            return "flat"
        if ema20[-1] > ema50[-1] and ema20[-2] > ema50[-2]:
            return "bullish"
        if ema20[-1] < ema50[-1] and ema20[-2] < ema50[-2]:
            return "bearish"
        return "flat"

    # --- Reasoning ---

    def _build_reason(
        self, action, trend, momentum, rsi, entry_signal,
        atr, price, sl, tp, confidence, trade_label="Day Trade",
    ) -> str:
        if action == "BUY":
            parts = [
                f"[{trade_label}] Price is trending upward — good buying opportunity.",
                f"Momentum is {momentum} with RSI at {rsi:.0f}.",
            ]
            if "buy" in entry_signal:
                parts.append("Price dipped to a good level and is starting to bounce back up.")
        else:  # SELL
            parts = [
                f"[{trade_label}] Price is trending downward — good selling opportunity.",
                f"Momentum is {momentum} with RSI at {rsi:.0f}.",
            ]
            if "sell" in entry_signal:
                parts.append("Price rallied to a resistance level and is starting to drop.")

        sl_distance = abs(price - sl)
        tp_distance = abs(tp - price)
        sl_pct = (sl_distance / price) * 100 if price > 0 else 0
        tp_pct = (tp_distance / price) * 100 if price > 0 else 0
        parts.append(f"SL: {sl_pct:.1f}% risk, TP: {tp_pct:.1f}% target.")

        return " ".join(parts)

    def _build_hold_reason(self, trend, momentum, rsi, entry_signal) -> str:
        issues = []
        if trend == "flat":
            issues.append("The price isn't moving in a clear direction")
        if momentum == "exhausted":
            if rsi > 70:
                issues.append("The price went up too fast and might drop soon")
            else:
                issues.append("The price dropped a lot and might bounce, but it's risky")
        if momentum == "neutral":
            issues.append("Hard to tell which way the price will go")
        if entry_signal == "none":
            issues.append("The price hasn't reached a clean entry zone yet")
        elif "continuation" in entry_signal:
            issues.append("The trend is there, but the continuation entry is not strong enough yet")

        if not issues:
            issues.append("Not a strong enough opportunity right now")
        direction_hint = {
            "bullish": "No buying setup right now.",
            "bearish": "No selling setup right now.",
            "flat": "No trade setup right now.",
        }.get(trend, "No trade setup right now.")
        return f"{direction_hint} " + ". ".join(issues) + ". The AI will keep watching."
