import random
from agent.interface import TradingAgent, AgentInput, TradeSignal


class MockAgent(TradingAgent):
    """Mock agent for testing. Returns random signals with low confidence."""

    @property
    def name(self) -> str:
        return "MockAgent"

    @property
    def description(self) -> str:
        return "Random signal generator for testing the pipeline. Not a real strategy."

    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        if len(input_data.bars) < 2:
            return TradeSignal(action="HOLD", confidence=0.0, reason="Not enough data")

        last_bar = input_data.bars[-1]
        action = random.choice(["BUY", "SELL", "HOLD"])
        confidence = round(random.uniform(0.1, 0.6), 2)

        close = last_bar.get("close", 0)
        point = 0.0001 if close < 50 else 0.01  # rough heuristic

        if action == "HOLD":
            return TradeSignal(
                action="HOLD",
                confidence=confidence,
                reason="Mock agent randomly chose HOLD",
            )

        sl_distance = point * random.randint(20, 50)
        tp_distance = point * random.randint(30, 80)

        if action == "BUY":
            sl = round(close - sl_distance, 5)
            tp = round(close + tp_distance, 5)
        else:
            sl = round(close + sl_distance, 5)
            tp = round(close - tp_distance, 5)

        return TradeSignal(
            action=action,
            confidence=confidence,
            stop_loss=sl,
            take_profit=tp,
            max_holding_minutes=random.randint(15, 120),
            reason=f"Mock agent random {action} signal for testing",
        )
