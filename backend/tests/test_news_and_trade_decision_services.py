import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import GeminiAssessment, MarketContext, NormalizedSymbolInfo, SymbolProfile, TechnicalSignal
from services.news_ingestion_service import NewsIngestionService
from services.trade_decision_service import TradeDecisionService


class NewsAndTradeDecisionServiceTests(unittest.TestCase):
    def _context(self) -> MarketContext:
        return MarketContext(
            symbol="EURUSD",
            requested_timeframe="H1",
            evaluation_mode="auto",
            user_policy={
                "mode": "balanced",
                "gemini_role": "advisory",
                "allow_counter_trend_trades": False,
                "session_filters": ["24/7"],
            },
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

    def test_news_ingestion_normalizes_and_filters_relevant_items(self):
        service = NewsIngestionService(
            headline_providers=[
                lambda **_: [
                    {
                        "source": "wire",
                        "title": "Dollar softens ahead of CPI",
                        "summary": "USD pairs in focus.",
                        "published_at": "2026-03-26T08:00:00Z",
                        "affected_symbols": ["EURUSD", "USDJPY"],
                    },
                    {
                        "source": "wire",
                        "title": "Oil steadies overnight",
                        "published_at": "2026-03-26T07:00:00Z",
                        "affected_symbols": ["XAUUSD"],
                    },
                ]
            ]
        )

        items = self._run(service.ingest_for_context(self._context()))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].affected_symbols, ["EURUSD", "USDJPY"])
        self.assertIsInstance(items[0].published_at, float)

    def test_trade_decision_never_flips_direction_from_gemini(self):
        service = TradeDecisionService()
        context = self._context()
        technical_signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.82,
            stop_loss=1.097,
            take_profit=1.106,
            reason="Technical breakout setup.",
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
        gemini = GeminiAssessment(
            used=True,
            available=True,
            news_bias="bearish",
            macro_relevance="high",
            event_risk="high",
            contradiction_flag=True,
            confidence_adjustment=-0.08,
            summary_reason="Macro event risk contradicts the technical case.",
            affected_symbols=["EURUSD"],
        )

        decision = service.decide(context, technical_signal, gemini, portfolio_fit_score=0.8)

        self.assertFalse(decision.trade)
        self.assertEqual(decision.final_direction, "HOLD")
        self.assertEqual(decision.final_signal.action, "HOLD")
        self.assertNotEqual(decision.final_signal.action, "SELL")

    def test_confirmation_required_policy_blocks_when_gemini_missing(self):
        service = TradeDecisionService()
        context = self._context().model_copy(
            update={
                "user_policy": {
                    "mode": "safe",
                    "gemini_role": "confirmation-required",
                    "allow_counter_trend_trades": False,
                    "session_filters": ["24/7"],
                }
            }
        )
        technical_signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.85,
            stop_loss=1.097,
            take_profit=1.106,
            reason="Technical breakout setup.",
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

        decision = service.decide(context, technical_signal, None, portfolio_fit_score=0.85)

        self.assertFalse(decision.trade)
        self.assertEqual(decision.final_signal.action, "HOLD")
        self.assertTrue(any("Gemini confirmation" in reason for reason in decision.reasons))

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)
