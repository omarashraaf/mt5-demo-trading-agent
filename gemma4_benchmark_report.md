# Gemma 4 Ollama Benchmark Report

- Timestamp (UTC): 2026-04-16T09:03:12.318960+00:00
- Server URL: http://10.0.0.10:11434
- Benchmark model: gemma4:e4b
- Required model check: gemma4:31b
- Required model present: yes
- Benchmark model present: yes

## Available Models

- gemma4:31b
- gemma4:e4b

## A. Simple prompt

- Wall time: 129.337s
- Ollama total_duration: 128.992s
- Ollama load_duration: 59.519s
- Ollama prompt_eval_duration: 1.810s
- Ollama eval_duration: 66.342s
- Prompt tokens: 27
- Eval tokens: 427
- Done: True
- Done reason: stop
- Response preview: * **Measures Momentum:** RSI tells you how fast and how much a stock's price is changing. Think of it as a gauge for buying or selling energy. * **Finds Extremes (Overbought/Oversold):** When the indicator is very hig...

```text
*   **Measures Momentum:** RSI tells you how fast and how much a stock's price is changing. Think of it as a gauge for buying or selling energy.
*   **Finds Extremes (Overbought/Oversold):** When the indicator is very high (overbought), the price might be too high and due for a pullback. When it's very low (oversold), the price might be too low and due for a rebound.
*   **Cautionary Signal:** It is not a guarantee, but it helps traders identify potential reversal points—moments when the current price trend might be exhausted.
```

## B. Warm simple prompt again

- Wall time: 75.690s
- Ollama total_duration: 75.501s
- Ollama load_duration: 1.177s
- Ollama prompt_eval_duration: 0.205s
- Ollama eval_duration: 73.437s
- Prompt tokens: 27
- Eval tokens: 459
- Done: True
- Done reason: stop
- Response preview: * **It measures the speed of price changes:** RSI tracks how quickly an asset's price is rising or falling, helping traders gauge the strength of the current market move. * **Overbought/Oversold Signals:** When the li...

```text
*   **It measures the speed of price changes:** RSI tracks how quickly an asset's price is rising or falling, helping traders gauge the strength of the current market move.
*   **Overbought/Oversold Signals:** When the line is too high (above 70), it suggests the price has risen too fast and might reverse. When it's too low (below 30), it suggests the price has fallen too fast and might bounce back.
*   **A Cautionary Tool:** It is not a guarantee, but a tool that signals potential **reversals**. Traders often use it to time their entries and exits, suggesting caution when the signals are extreme.
```

## C. Realistic LinkTrade decision prompt

- Wall time: 158.283s
- Ollama total_duration: 158.235s
- Ollama load_duration: 1.108s
- Ollama prompt_eval_duration: 9.123s
- Ollama eval_duration: 146.499s
- Prompt tokens: 248
- Eval tokens: 1020
- Done: True
- Done reason: stop
- Response preview: ```json { "action": "HOLD", "confidence": 0.60, "stop_loss_price": "N/A", "take_profit_price": "N/A", "max_hold_minutes": 120, "strategy": "Wait for Directional Confirmation", "summary_reason": "The trade signals are ...

```text
```json
{
  "action": "HOLD",
  "confidence": 0.60,
  "stop_loss_price": "N/A",
  "take_profit_price": "N/A",
  "max_hold_minutes": 120,
  "strategy": "Wait for Directional Confirmation",
  "summary_reason": "The trade signals are mixed (mild SELL bias vs. range-bound 15m). The meta-model confidence is low (0.58), and recent breakout trades have underperformed. Preferring HOLD to wait for a clearer structural break or momentum confirmation.",
  "market_regime": "Range-Bound/Consolidation",
  "risk_note": "High caution advised due to conflicting momentum signals and recent failure of breakout patterns. Avoid initiating trades until a definitive breakout occurs."
}
```
```

## D. Realistic LinkTrade prompt repeated a second time

- Wall time: 121.560s
- Ollama total_duration: 121.397s
- Ollama load_duration: 0.696s
- Ollama prompt_eval_duration: 0.167s
- Ollama eval_duration: 119.406s
- Prompt tokens: 248
- Eval tokens: 814
- Done: True
- Done reason: stop
- Response preview: ```json { "action": "HOLD", "confidence": "low", "stop_loss_price": null, "take_profit_price": null, "max_hold_minutes": 60, "strategy": "Wait for Confirmation", "summary_reason": "Evidence is mixed (bearish rejection...

```text
```json
{
  "action": "HOLD",
  "confidence": "low",
  "stop_loss_price": null,
  "take_profit_price": null,
  "max_hold_minutes": 60,
  "strategy": "Wait for Confirmation",
  "summary_reason": "Evidence is mixed (bearish rejection vs. range-bound 15m trend) and the meta-model preview shows low edge. Given the poor performance of recent breakout trades, pausing is the highest probability move.",
  "market_regime": "Range-bound/Transitional",
  "risk_note": "Await a decisive break above intraday resistance or below key support before considering a directional trade."
}
```
```

## Conclusion

- Recommendation: Too slow to use in current setup
