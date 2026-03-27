import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, TechnicalSignal, TradeQualityAssessment
from services.anti_churn_service import AntiChurnService


class AntiChurnServiceTests(unittest.TestCase):
    def test_recent_actionable_but_not_opened_does_not_trigger_trade_cap(self):
        service = AntiChurnService()
        context = MarketContext(symbol="XAUUSD", evaluation_mode="auto")
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.65,
            stop_loss=4469.72,
            take_profit=4264.57,
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.82,
            threshold=0.76,
            no_trade_zone=False,
        )

        recent_evals = [
            {
                "id": 1,
                "timestamp": 1.0,
                "symbol": "XAUUSD",
                "executable_action": "SELL",
                "quality_score": 0.81,
                "execution_decision": "{\"allow_execute\": false}",
                "outcome_status": "pending",
            },
            {
                "id": 2,
                "timestamp": 2.0,
                "symbol": "XAUUSD",
                "executable_action": "SELL",
                "quality_score": 0.80,
                "execution_decision": "{\"allow_execute\": false}",
                "outcome_status": "pending",
            },
        ]

        assessment = service.assess(
            context=context,
            signal=signal,
            trade_quality=quality,
            cooldown_minutes=120,
            max_trades_per_symbol=1,
            recent_outcomes=[],
            recent_evaluations=recent_evals,
            scan_window_id="auto:1",
        )

        self.assertFalse(assessment.blocked)
        self.assertFalse(
            any("per-symbol trade cap" in reason.lower() for reason in assessment.reasons)
        )

