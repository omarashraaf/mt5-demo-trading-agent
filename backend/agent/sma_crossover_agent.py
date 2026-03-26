from agent.interface import TradingAgent, AgentInput, TradeSignal


class SMACrossoverAgent(TradingAgent):
    """Simple Moving Average crossover strategy.

    Uses fast (10) and slow (30) SMA crossover to generate signals.
    This is a basic rule-based strategy for demonstration purposes.
    """

    def __init__(self, fast_period: int = 10, slow_period: int = 30):
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def name(self) -> str:
        return "SMA_Crossover"

    @property
    def description(self) -> str:
        return (
            f"SMA crossover strategy (fast={self.fast_period}, slow={self.slow_period}). "
            "Generates BUY when fast SMA crosses above slow SMA, SELL when it crosses below."
        )

    def _sma(self, closes: list[float], period: int) -> list[float]:
        result = []
        for i in range(len(closes)):
            if i < period - 1:
                result.append(0.0)
            else:
                window = closes[i - period + 1 : i + 1]
                result.append(sum(window) / period)
        return result

    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        bars = input_data.bars
        min_bars = self.slow_period + 2
        if len(bars) < min_bars:
            return TradeSignal(
                action="HOLD",
                confidence=0.0,
                reason=f"Need at least {min_bars} bars, got {len(bars)}",
            )

        closes = [b["close"] for b in bars]
        fast_sma = self._sma(closes, self.fast_period)
        slow_sma = self._sma(closes, self.slow_period)

        current_fast = fast_sma[-1]
        current_slow = slow_sma[-1]
        prev_fast = fast_sma[-2]
        prev_slow = slow_sma[-2]

        last_close = closes[-1]
        atr = self._calculate_atr(bars, 14)

        # Determine if the symbol uses small or large point values
        point = 0.0001 if last_close < 50 else 0.01

        # Bullish crossover
        if prev_fast <= prev_slow and current_fast > current_slow:
            gap = abs(current_fast - current_slow)
            confidence = min(0.85, 0.5 + (gap / (atr if atr > 0 else point)))
            sl = round(last_close - atr * 1.5, 5)
            tp = round(last_close + atr * 2.5, 5)
            return TradeSignal(
                action="BUY",
                confidence=round(confidence, 2),
                stop_loss=sl,
                take_profit=tp,
                max_holding_minutes=240,
                reason=f"Bullish SMA crossover: fast({self.fast_period})={current_fast:.5f} crossed above slow({self.slow_period})={current_slow:.5f}",
            )

        # Bearish crossover
        if prev_fast >= prev_slow and current_fast < current_slow:
            gap = abs(current_slow - current_fast)
            confidence = min(0.85, 0.5 + (gap / (atr if atr > 0 else point)))
            sl = round(last_close + atr * 1.5, 5)
            tp = round(last_close - atr * 2.5, 5)
            return TradeSignal(
                action="SELL",
                confidence=round(confidence, 2),
                stop_loss=sl,
                take_profit=tp,
                max_holding_minutes=240,
                reason=f"Bearish SMA crossover: fast({self.fast_period})={current_fast:.5f} crossed below slow({self.slow_period})={current_slow:.5f}",
            )

        # No crossover
        direction = "above" if current_fast > current_slow else "below"
        return TradeSignal(
            action="HOLD",
            confidence=0.1,
            reason=f"No crossover. Fast SMA is {direction} slow SMA. Waiting for crossover signal.",
        )

    def _calculate_atr(self, bars: list[dict], period: int = 14) -> float:
        if len(bars) < period + 1:
            # Fallback: use simple high-low range
            ranges = [b["high"] - b["low"] for b in bars[-period:]]
            return sum(ranges) / len(ranges) if ranges else 0.0001

        true_ranges = []
        for i in range(1, len(bars)):
            high = bars[i]["high"]
            low = bars[i]["low"]
            prev_close = bars[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        recent = true_ranges[-period:]
        return sum(recent) / len(recent)
