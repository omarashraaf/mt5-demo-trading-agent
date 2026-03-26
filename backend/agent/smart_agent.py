from agent.interface import TradingAgent, AgentInput, TradeSignal
from typing import Optional

# Asset classification for mixed strategy
STOCKS = {
    "NVDA", "AMD", "MSFT", "INTC", "AAPL", "GOOG", "GOOGL", "AMZN", "TSLA", "META",
    "NFLX", "AVGO", "CRM", "ORCL", "ADBE", "QCOM", "PYPL", "BA", "DIS", "JPM",
    "V", "MA", "WMT", "KO", "MCD", "NKE", "COST", "PEP", "CSCO",
}
COMMODITIES = {
    "XAUUSD", "XAGUSD", "XAUEUR", "XAGEUR", "XPTUSD", "XPDUSD",  # Metals
    "GLD", "SLV", "USO", "DBC",  # Commodity ETFs (oil, gold, silver, broad)
}
# Everything else is forex


def get_asset_class(symbol: str) -> str:
    """Classify symbol into asset class for strategy selection."""
    if symbol in STOCKS:
        return "stock"
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

        # Use H4 if available, otherwise fall back to H1 for trend
        trend_bars = h4_bars if len(h4_bars) >= 52 else h1_bars

        # Step 1: H4 / long-term trend via EMA
        closes_trend = [b["close"] for b in trend_bars]
        ema20 = self._ema(closes_trend, 20)
        ema50 = self._ema(closes_trend, 50)

        trend = "flat"
        trend_score = 0.0
        if ema20[-1] > ema50[-1] and ema20[-2] > ema50[-2]:
            trend = "bullish"
            trend_score = 0.15
        elif ema20[-1] < ema50[-1] and ema20[-2] < ema50[-2]:
            trend = "bearish"
            trend_score = 0.15

        # Step 2: H1 momentum via RSI
        closes_h1 = [b["close"] for b in h1_bars]
        rsi = self._rsi(closes_h1, 14)
        current_rsi = rsi[-1] if rsi else 50.0

        momentum = "neutral"
        momentum_score = 0.0
        if trend == "bullish" and 40 < current_rsi < 70:
            momentum = "bullish"
            momentum_score = 0.15
        elif trend == "bearish" and 30 < current_rsi < 60:
            momentum = "bearish"
            momentum_score = 0.15
        # Exhaustion zones reduce confidence
        if current_rsi > 75 or current_rsi < 25:
            momentum = "exhausted"
            momentum_score = -0.1

        # Step 3: M15 entry trigger
        entry_signal, entry_score = self._detect_entry(m15_bars, trend)

        # Step 4: Support/Resistance confirmation
        sr_score = self._check_sr_alignment(h1_bars, m15_bars[-1]["close"], trend)

        # Step 5: Volume confirmation
        volume_score = 0.0
        if len(m15_bars) >= 21:
            recent_vol = m15_bars[-1].get("volume", 0)
            avg_vol = sum(b.get("volume", 0) for b in m15_bars[-21:-1]) / 20
            if avg_vol > 0 and recent_vol > avg_vol * 1.2:
                volume_score = 0.05

        # Step 6: Calculate ATR for SL/TP
        atr = self._atr(h1_bars, 14)
        last_close = m15_bars[-1]["close"]

        # Aggregate confidence
        confidence = 0.3 + trend_score + momentum_score + entry_score + sr_score + volume_score
        confidence = round(max(0.0, min(0.95, confidence)), 2)

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
        # With entry trigger: trade at 55%+ confidence
        # Without entry trigger: trade at 60%+ confidence if trend+momentum align
        has_entry = entry_signal not in ("none", "")

        # Get asset-specific parameters (forex=day trade, stocks/gold=swing trade)
        asset_class = get_asset_class(input_data.symbol)
        params = get_trade_params(asset_class)

        if trend == "bullish" and (
            (has_entry and confidence >= 0.55) or confidence >= 0.60
        ):
            action = "BUY"
            sl_mult = params["sl_mult_entry"] if has_entry else params["sl_mult_no_entry"]
            tp_mult = params["tp_mult_entry"] if has_entry else params["tp_mult_no_entry"]
            sl = round(last_close - atr * sl_mult, 5)
            tp = round(last_close + atr * tp_mult, 5)
        elif trend == "bearish" and (
            (has_entry and confidence >= 0.55) or confidence >= 0.60
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
            )

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

    def _detect_entry(self, m15_bars: list[dict], trend: str) -> tuple[str, float]:
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

        return "none", 0.0

    def _check_sr_alignment(self, h1_bars: list[dict], current_price: float, trend: str) -> float:
        """Check if price is near a support (for buys) or resistance (for sells)."""
        if len(h1_bars) < 20:
            return 0.0

        highs = [b["high"] for b in h1_bars[-30:]]
        lows = [b["low"] for b in h1_bars[-30:]]
        recent_high = max(highs)
        recent_low = min(lows)
        range_size = recent_high - recent_low
        if range_size <= 0:
            return 0.0

        # For buys: price near lower third = good support zone
        if trend == "bullish":
            position_in_range = (current_price - recent_low) / range_size
            if position_in_range < 0.4:
                return 0.1
        # For sells: price near upper third = good resistance zone
        elif trend == "bearish":
            position_in_range = (current_price - recent_low) / range_size
            if position_in_range > 0.6:
                return 0.1
        return 0.0

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
            issues.append("The price hasn't reached a good buying level yet")

        if not issues:
            issues.append("Not a strong enough opportunity right now")

        return "No buying opportunity right now. " + ". ".join(issues) + ". The AI will keep watching."
