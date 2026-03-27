import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import (
    AntiChurnAssessment,
    MarketContext,
    PortfolioRiskAssessment,
    RiskEvaluation,
    TechnicalSignal,
    TradeQualityAssessment,
)
from risk.rules import RiskEngine
from services.risk_service import RiskService


class _PortfolioRiskService:
    def snapshot(self, account, positions):
        return {
            "margin_utilization_pct": 0.0,
            "free_margin_pct": 100.0,
            "open_positions_total": len(positions),
        }

    def assess(self, **kwargs):
        return PortfolioRiskAssessment(
            status="pass",
            allow_execute=True,
            reason="ok",
            blocking_reasons=[],
            warnings=[],
            metrics_snapshot={},
            portfolio_fit_score=0.8,
        )


class _AntiChurnService:
    def assess(self, **kwargs):
        return AntiChurnAssessment(blocked=False, threshold_boost=0.0, reasons=[], metadata={})


class _ExecutionEngine:
    def get_positions(self):
        return []


class RiskServiceTests(unittest.TestCase):
    def setUp(self):
        self.risk_engine = RiskEngine()
        self.risk_engine.apply_policy_preset("balanced")
        self.service = RiskService(
            risk_engine=self.risk_engine,
            portfolio_risk_service=_PortfolioRiskService(),
            anti_churn_service=_AntiChurnService(),
            execution_engine=_ExecutionEngine(),
        )
        self.context = MarketContext(
            symbol="XAUUSD",
            evaluation_mode="auto",
            account_equity=100000,
            account_margin=0,
            account_free_margin=100000,
            tick={"bid": 3000.0, "ask": 3000.5, "spread": 15.0},
        )

    def test_balanced_blocks_low_confidence_auto_trade(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.62,
            stop_loss=2990.0,
            take_profit=3025.0,
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.79,
            threshold=0.76,
            no_trade_zone=False,
        )
        result = self.service.assess(
            context=self.context,
            signal=signal,
            trade_quality=quality,
            evaluation_mode="auto",
            recent_outcomes=[],
            recent_evaluations=[],
            entry_price=3000.5,
        )
        self.assertFalse(result.approved)
        self.assertTrue(any("below the required" in reason for reason in result.reasons))

    def test_exceptional_quality_can_use_modest_discount(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.64,
            stop_loss=2990.0,
            take_profit=3025.0,
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.86,
            threshold=0.76,
            no_trade_zone=False,
        )
        result = self.service.assess(
            context=self.context,
            signal=signal,
            trade_quality=quality,
            evaluation_mode="auto",
            recent_outcomes=[],
            recent_evaluations=[],
            entry_price=3000.5,
        )
        self.assertTrue(result.approved)

