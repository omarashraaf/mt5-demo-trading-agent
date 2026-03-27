import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, NormalizedSymbolInfo, SymbolProfile
from services.analytics_service import AnalyticsService
from services.replay_service import ReplayService


class _Decision:
    def __init__(self, allow_execute=True, action="BUY", quality=0.8):
        from domain.models import (
            AntiChurnAssessment,
            ExecutionDecision,
            PortfolioRiskAssessment,
            PositionManagementPlan,
            RiskEvaluation,
            SignalDecision,
            TechnicalSignal,
            TradeQualityAssessment,
        )

        context = MarketContext(symbol="EURUSD", evaluation_mode="replay")
        signal = TechnicalSignal(agent_name="SmartAgent", action=action, confidence=0.8)
        self.decision = ExecutionDecision(
            allow_execute=allow_execute,
            reason="ok" if allow_execute else "blocked",
            signal_decision=SignalDecision(
                requested_agent_name="SmartAgent",
                primary_agent_name="SmartAgent",
                final_agent_name="SmartAgent",
                market_context=context,
                primary_signal=signal,
                final_signal=signal if allow_execute else signal.model_copy(update={"action": "HOLD"}),
            ),
            risk_evaluation=RiskEvaluation(approved=allow_execute, reason="ok", adjusted_volume=0.01),
            trade_quality_assessment=TradeQualityAssessment(
                final_trade_quality_score=quality,
                threshold=0.7,
                no_trade_zone=not allow_execute,
                no_trade_reasons=[] if allow_execute else ["quality too low"],
            ),
            portfolio_risk_assessment=PortfolioRiskAssessment(
                allow_execute=allow_execute,
                reason="ok",
            ),
            anti_churn_assessment=AntiChurnAssessment(),
            position_management_plan=PositionManagementPlan(),
            entry_price=1.1,
        )


class _Pipeline:
    def __init__(self):
        self.calls = 0

    async def evaluate_context(self, context, requested_agent_name=None, scan_window_id=None):
        self.calls += 1
        return _Decision(allow_execute=self.calls % 2 == 1, quality=0.82 if self.calls % 2 == 1 else 0.55).decision


class ReplayAndAnalyticsTests(unittest.TestCase):
    def test_replay_consistency_counts_accepted_and_blocked(self):
        service = ReplayService(_Pipeline())
        contexts = [
            MarketContext(
                symbol="EURUSD",
                requested_timeframe="H1",
                evaluation_mode="replay",
                symbol_info=NormalizedSymbolInfo(name="EURUSD"),
                profile=SymbolProfile(
                    profile_name="Forex Majors",
                    category="Forex",
                    max_spread=12,
                    min_atr_pct=0.2,
                    max_hold_minutes=360,
                    min_reward_risk=1.8,
                    max_positions_per_category=2,
                    quality_threshold=0.72,
                    cooldown_minutes=120,
                    news_weight=0.1,
                    default_sl_atr_multiplier=1.2,
                    default_tp_atr_multiplier=2.4,
                ),
                tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
                bars_by_timeframe={"M15": [{"close": 1.1, "high": 1.101, "low": 1.099}] * 100, "H1": [], "H4": []},
            ),
            MarketContext(
                symbol="EURUSD",
                requested_timeframe="H1",
                evaluation_mode="replay",
                symbol_info=NormalizedSymbolInfo(name="EURUSD"),
                profile=SymbolProfile(
                    profile_name="Forex Majors",
                    category="Forex",
                    max_spread=12,
                    min_atr_pct=0.2,
                    max_hold_minutes=360,
                    min_reward_risk=1.8,
                    max_positions_per_category=2,
                    quality_threshold=0.72,
                    cooldown_minutes=120,
                    news_weight=0.1,
                    default_sl_atr_multiplier=1.2,
                    default_tp_atr_multiplier=2.4,
                ),
                tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
                bars_by_timeframe={"M15": [{"close": 1.1, "high": 1.101, "low": 1.099}] * 100, "H1": [], "H4": []},
            ),
        ]
        result = asyncio.run(service.run_contexts(contexts))
        self.assertEqual(result["accepted_trades"], 1)
        self.assertEqual(result["blocked_trades"], 1)

    def test_confidence_calibration_analytics_recommends_higher_threshold_for_bad_low_buckets(self):
        analytics = AnalyticsService()
        result = analytics.confidence_calibration([
            {"confidence": 0.56, "profit": -1.0},
            {"confidence": 0.58, "profit": -0.5},
            {"confidence": 0.62, "profit": -0.4},
            {"confidence": 0.76, "profit": 1.5},
            {"confidence": 0.80, "profit": 2.0},
        ])
        self.assertGreaterEqual(result["recommended_min_confidence"], 0.70)

    def test_holding_time_analytics_groups_buckets_and_recommends_category_hold(self):
        analytics = AnalyticsService()
        result = analytics.holding_time_analysis([
            {"holding_minutes": 12, "profit": 0.5, "symbol_category": "Forex"},
            {"holding_minutes": 25, "profit": -0.2, "symbol_category": "Forex"},
            {"holding_minutes": 42, "profit": 0.8, "symbol_category": "Forex"},
            {"holding_minutes": 75, "profit": 1.1, "symbol_category": "Forex"},
            {"holding_minutes": 210, "profit": -0.6, "symbol_category": "Forex"},
            {"holding_minutes": 170, "profit": 1.4, "symbol_category": "Stocks"},
            {"holding_minutes": 185, "profit": 1.2, "symbol_category": "Stocks"},
        ])

        self.assertEqual(result["buckets"]["0-15 min"]["count"], 1)
        self.assertEqual(result["buckets"]["60-180 min"]["count"], 2)
        self.assertEqual(
            result["recommendations_by_category"]["Forex"]["recommended_max_hold_minutes"],
            180,
        )
        self.assertEqual(
            result["recommendations_by_category"]["Stocks"]["best_bucket"],
            "60-180 min",
        )
