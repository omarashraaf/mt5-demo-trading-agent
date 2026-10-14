import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import GeminiAssessment, MarketContext, NormalizedSymbolInfo, SymbolProfile, TechnicalSignal
from services.anti_churn_service import AntiChurnService
from services.trade_quality_service import TradeQualityService


class TradeQualityServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = TradeQualityService()
        self.context = MarketContext(
            symbol="EURUSD",
            requested_timeframe="H1",
            evaluation_mode="auto",
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                description="Forex pair",
                category="Forex",
                point=0.0001,
                digits=5,
                trade_contract_size=100000,
                trade_enabled=True,
                spread=2,
                sector="FX",
                theme_bucket="USD",
                correlation_tags=["FX", "USD"],
            ),
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
                allowed_sessions=["24/7"],
                default_sl_atr_multiplier=1.2,
                default_tp_atr_multiplier=2.4,
                sector="FX",
                theme_bucket="USD",
            ),
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
        )

    def test_trade_quality_scoring_passes_strong_setup(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.22,
                "ema_distance_atr": 0.55,
            },
        )
        gemini = GeminiAssessment(
            used=True,
            available=True,
            news_bias="bullish",
            macro_relevance="medium",
            event_risk="low",
            confidence_adjustment=0.03,
            source_quality_score=0.8,
            summary_reason="Macro supports the move.",
        )
        result = self.service.assess(self.context, signal, gemini, portfolio_fit_score=0.8)
        self.assertFalse(result.no_trade_zone)
        self.assertGreaterEqual(result.final_trade_quality_score, result.threshold)

    def test_assess_normalizes_action_before_quality_scoring(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.108,
            reason="Breakout.",
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.32,
                "position_in_range": 0.2,
                "ema_distance_atr": 0.55,
            },
        )
        signal.action = " buy "

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertFalse(result.no_trade_zone)
        self.assertIn("BUY", result.summary)

    def test_trade_quality_sanitizes_malformed_portfolio_fit_score(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.22,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score="bad-fit")

        self.assertFalse(result.no_trade_zone)
        self.assertGreaterEqual(result.final_trade_quality_score, result.threshold)

    def test_news_alignment_treats_malformed_gemini_event_risk_as_high(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={},
        )
        assessment = GeminiAssessment.model_construct(
            used=True,
            available=True,
            degraded=False,
            news_bias="bullish",
            event_risk="bad-risk",
            contradiction_flag=False,
        )

        alignment, contradiction = self.service._news_alignment(
            context=self.context,
            signal=signal,
            assessment=assessment,
        )

        self.assertEqual(alignment, 0.72)
        self.assertEqual(contradiction, 0.0)

    def test_volatility_score_sanitizes_malformed_inputs(self):
        self.assertEqual(self.service._volatility_score("bad-atr", 0.2), 0.0)
        self.assertEqual(self.service._volatility_score(0.35, "bad-min-atr"), 0.0)

    def test_reward_risk_score_sanitizes_malformed_inputs(self):
        self.assertEqual(self.service._reward_risk_score("bad-rr", 1.8), 0.0)
        self.assertEqual(self.service._reward_risk_score(2.2, "bad-min-rr"), 0.0)

    def test_spread_score_sanitizes_malformed_inputs(self):
        self.assertEqual(self.service._spread_score("bad-spread", 20.0), 0.0)
        self.assertEqual(self.service._spread_score(2.0, "bad-max-spread"), 0.0)

    def test_spread_limit_points_sanitizes_malformed_inputs(self):
        self.assertEqual(
            self.service._spread_limit_points("bad-max-spread", 0.0001),
            0.0,
        )
        self.assertEqual(
            self.service._spread_limit_points(0.0012, "bad-point"),
            0.0012,
        )

    def test_trade_quality_blocks_no_trade_zone(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.65,
            stop_loss=1.0995,
            take_profit=1.101,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bearish",
                "momentum_score": 0.02,
                "entry_score": 0.0,
                "entry_signal": "none",
                "atr_pct": 0.05,
                "position_in_range": 0.5,
                "ema_distance_atr": 1.3,
            },
        )
        gemini = GeminiAssessment(
            used=True,
            available=True,
            news_bias="neutral",
            macro_relevance="high",
            event_risk="high",
            confidence_adjustment=-0.05,
            contradiction_flag=True,
            source_quality_score=0.7,
            summary_reason="High-risk event with unclear direction.",
        )
        result = self.service.assess(self.context, signal, gemini, portfolio_fit_score=0.2)
        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("H1 and H4" in reason for reason in result.no_trade_reasons))

    def test_execution_cancels_on_deteriorated_spread(self):
        anti_churn = AntiChurnService()
        self.assertTrue(anti_churn.spread_deteriorated(reference_spread=2, current_spread=4, max_spread=12))

    def test_hold_reason_is_preserved_in_quality_assessment(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="HOLD",
            confidence=0.45,
            reason="No buying opportunity right now. The price hasn't reached a clean entry zone yet.",
            metadata={"entry_signal": "none"},
        )
        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.5)
        self.assertTrue(result.no_trade_zone)
        self.assertIn("clean entry zone", result.no_trade_reasons[0])

    def test_reward_risk_uses_user_policy_threshold(self):
        context = self.context.model_copy(
            update={
                "user_policy": {
                    "mode": "aggressive",
                    "min_reward_risk": 1.6,
                    "session_filters": ["24/7"],
                }
            }
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.67,
            stop_loss=1.0990,
            take_profit=1.1028,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "entry_signal": "buy",
                "entry_score": 0.1,
                "momentum_score": 0.1,
                "atr_pct": 0.25,
                "position_in_range": 0.3,
                "ema_distance_atr": 0.7,
            },
        )
        result = self.service.assess(context, signal, None, portfolio_fit_score=0.7)
        self.assertFalse(any("Reward:risk deteriorated" in reason for reason in result.no_trade_reasons))

    def test_reward_risk_accounts_for_spread_cost(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.0992,
            take_profit=1.1020,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Cost-adjusted reward:risk" in reason for reason in result.no_trade_reasons))

    def test_directionally_invalid_targets_are_no_trade(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.1030,
            take_profit=1.0940,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Invalid BUY targets" in reason for reason in result.no_trade_reasons))

    def test_missing_protective_targets_are_no_trade(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.82,
            stop_loss=None,
            take_profit=None,
            metadata={
                "h1_trend": "bearish",
                "h4_trend": "bearish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "sell",
                "atr_pct": 0.35,
                "position_in_range": 0.75,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Protective stop-loss and take-profit are required" in reason for reason in result.no_trade_reasons))

    def test_non_finite_live_entry_is_no_trade(self):
        context = self.context.model_copy(update={"tick": {"bid": 1.1, "ask": float("inf"), "spread": 2}})
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("live entry" in reason for reason in result.no_trade_reasons))

    def test_malformed_live_spread_is_no_trade(self):
        context = self.context.model_copy(update={"tick": {"bid": 1.1, "ask": 1.1002, "spread": "bad-spread"}})
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Spread" in reason for reason in result.no_trade_reasons))

    def test_non_finite_protective_targets_are_no_trade(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=float("inf"),
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Protective stop-loss and take-profit" in reason for reason in result.no_trade_reasons))

    def test_malformed_protective_targets_are_no_trade(self):
        signal = TechnicalSignal.model_construct(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss="bad-stop",
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("Protective stop-loss and take-profit" in reason for reason in result.no_trade_reasons))

    def test_non_finite_atr_is_no_trade(self):
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.107,
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "buy",
                "atr_pct": float("inf"),
                "position_in_range": 0.25,
                "ema_distance_atr": 0.55,
            },
        )

        result = self.service.assess(self.context, signal, None, portfolio_fit_score=0.8)

        self.assertTrue(result.no_trade_zone)
        self.assertTrue(any("ATR regime" in reason for reason in result.no_trade_reasons))

    def test_spread_limit_is_converted_to_points(self):
        context = self.context.model_copy(
            update={
                "symbol": "XPDUSD",
                "symbol_info": self.context.symbol_info.model_copy(
                    update={
                        "name": "XPDUSD",
                        "category": "Commodities",
                        "point": 0.001,
                        "digits": 3,
                    }
                ),
                "profile": self.context.profile.model_copy(
                    update={
                        "profile_name": "Commodities",
                        "category": "Commodities",
                        "max_spread": 35.0,
                    }
                ),
                "tick": {"bid": 1376.0, "ask": 1380.68, "spread": 4680.0},
                "evaluation_mode": "manual",
            }
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.7,
            stop_loss=1385.0,
            take_profit=1360.0,
            metadata={
                "h1_trend": "bearish",
                "h4_trend": "bearish",
                "momentum_score": 0.15,
                "entry_score": 0.1,
                "entry_signal": "sell",
                "atr_pct": 0.5,
                "position_in_range": 0.6,
                "ema_distance_atr": 0.8,
            },
        )
        result = self.service.assess(context, signal, None, portfolio_fit_score=0.7)
        self.assertFalse(any("Spread too wide for Commodities" in reason for reason in result.no_trade_reasons))
