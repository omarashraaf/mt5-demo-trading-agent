import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.interface import TradeSignal
from domain.models import NormalizedNewsItem
from services.signal_pipeline_service import SignalPipelineService
from services.trade_quality_service import TradeQualityService


class _Tick:
    def __init__(self, bid=1.1, ask=1.1002, spread=2):
        self.bid = bid
        self.ask = ask
        self.spread = spread

    def model_dump(self):
        return {"bid": self.bid, "ask": self.ask, "spread": self.spread}


class _Bar:
    def __init__(self, close: float):
        self.close = close

    def model_dump(self):
        return {
            "time": 1,
            "open": self.close - 0.001,
            "high": self.close + 0.001,
            "low": self.close - 0.001,
            "close": self.close,
            "volume": 100,
        }


class _Account:
    balance = 10000
    equity = 10000
    margin = 500
    free_margin = 9500
    currency = "USD"
    leverage = 100


class _Position:
    def __init__(self, symbol="US500"):
        self.symbol = symbol

    def model_dump(self):
        return {
            "ticket": 1,
            "symbol": self.symbol,
            "type": "BUY",
            "volume": 0.01,
            "price_open": 1.1,
            "price_current": 1.1,
            "stop_loss": 1.09,
            "take_profit": 1.12,
            "profit": 0.0,
            "time": 1,
            "comment": "TradingAgent",
        }


class _MarketData:
    def enable_symbol(self, _symbol):
        return True

    def get_tradeable_symbols(self):
        return [
            {"name": "US500", "category": "Indices", "trade_enabled": True, "visible": True},
            {"name": "EURUSD", "category": "Forex", "trade_enabled": True, "visible": True},
        ]

    def get_visible_symbols(self):
        return self.get_tradeable_symbols()

    def get_tick(self, _symbol):
        return _Tick()

    def get_bars(self, _symbol, timeframe, count):
        base = {"M15": 1.1, "H1": 1.11, "H4": 1.12}[timeframe]
        return [_Bar(base + i * 0.0001) for i in range(count)]

    def get_symbol_info(self, symbol):
        return {
            "name": symbol,
            "description": "Broker symbol",
            "path": "indices\\cash" if symbol in {"US500", "US100", "US30", "GER40"} else "commodities\\metals",
            "point": 0.0001,
            "digits": 5,
            "trade_contract_size": 100000,
            "volume_min": 0.01,
            "volume_max": 100,
            "volume_step": 0.01,
            "trade_stops_level": 10,
            "trade_mode": 4,
            "visible": True,
        }


class _Connector:
    def refresh_account(self):
        return _Account()


class _Execution:
    def get_positions(self, symbol=None):
        return []


class _RiskDecision:
    approved = True
    reason = "ok"
    adjusted_volume = 0.02
    warnings = []


class _RiskEngine:
    def __init__(self):
        from risk.rules import RiskSettings

        self.settings = RiskSettings()

    def evaluate(self, **_kwargs):
        return _RiskDecision()


class _SmartAgent:
    def evaluate(self, input_data):
        assert "M15" in input_data.multi_tf_bars
        assert "H1" in input_data.multi_tf_bars
        assert "H4" in input_data.multi_tf_bars
        return TradeSignal(
            action="BUY",
            confidence=0.78,
            stop_loss=1.09,
            take_profit=1.13,
            max_holding_minutes=360,
            reason="Trend aligned",
            strategy="trend_follow",
            metadata={
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "trend_score": 0.2,
                "momentum_score": 0.15,
                "entry_signal": "buy",
                "entry_score": 0.1,
                "sr_score": 0.1,
                "volume_score": 0.05,
                "atr_pct": 0.35,
                "position_in_range": 0.25,
                "ema_distance_atr": 0.6,
                "reward_risk_ratio": 2.0,
            },
        )


class _GeminiAdapter:
    async def confirm(self, _input_data, _primary_signal):
        from domain.models import GeminiAssessment

        return GeminiAssessment(
            used=True,
            available=True,
            confirmed=True,
            news_bias="bullish",
            macro_relevance="medium",
            event_risk="low",
            confidence_adjustment=0.05,
            confidence_delta=0.05,
            summary_reason="News aligned",
            reason="News aligned",
            source_quality_score=0.8,
        )


class _EventIngestionService:
    async def maybe_refresh_latest(self, **_kwargs):
        return None

    async def recent_symbol_news(self, symbol, **_kwargs):
        return [
            NormalizedNewsItem(
                source="finnhub",
                category="headline",
                title=f"Catalyst for {symbol}",
                summary="Stored event-backed news item.",
                published_at=1710000000.0,
                received_at=1710000001.0,
                affected_symbols=[symbol],
            )
        ]

    async def latest_candidate_symbols(self, **_kwargs):
        return []


class _AlwaysOpenTradeQualityService(TradeQualityService):
    def session_allowed(self, sessions: list[str]) -> bool:
        return True


class SignalPipelineServiceTests(unittest.TestCase):
    def test_pipeline_uses_consistent_multi_timeframe_context(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertEqual(decision.signal_decision.primary_agent_name, "SmartAgent")
        self.assertEqual(decision.signal_decision.final_signal.action, "BUY")
        self.assertAlmostEqual(decision.signal_decision.final_signal.confidence, 0.83, places=2)
        self.assertEqual(len(decision.signal_decision.market_context.normalized_news), 1)
        self.assertGreaterEqual(decision.trade_quality_assessment.final_trade_quality_score, 0.7)
        self.assertTrue(decision.allow_execute)
        self.assertEqual(decision.position_management_plan.planned_hold_minutes, 120)
        self.assertEqual(decision.signal_decision.final_signal.max_holding_minutes, 120)

    def test_pipeline_blocks_forex_symbols_in_indices_commodities_mode(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="EURUSD",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertFalse(decision.allow_execute)
        self.assertEqual(decision.signal_decision.final_signal.action, "HOLD")
        self.assertIn("inactive in the current mode", decision.reason.lower())

    def test_auto_trade_candidate_symbols_falls_back_to_curated_symbols(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        symbols = asyncio.run(
            pipeline.auto_trade_candidate_symbols(
                [
                    {"name": "US500", "category": "Indices", "path": "indices\\cash", "trade_enabled": True, "visible": True},
                    {"name": "TSLA", "category": "Stocks", "path": "stocks\\us", "trade_enabled": True, "visible": True},
                    {"name": "XAUUSD", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
                    {"name": "EURUSD", "category": "Forex", "path": "forex\\majors", "trade_enabled": True, "visible": True},
                ],
                limit=5,
            )
        )

        self.assertIn("US500", symbols)
        self.assertIn("XAUUSD", symbols)
        self.assertIn("TSLA", symbols)
        self.assertNotIn("EURUSD", symbols)
