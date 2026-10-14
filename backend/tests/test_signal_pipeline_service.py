import asyncio
import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.interface import TradeSignal
from domain.models import (
    AntiChurnAssessment,
    ExecutionDecision,
    MarketContext,
    NormalizedNewsItem,
    NormalizedSymbolInfo,
    PortfolioRiskAssessment,
    PositionManagementPlan,
    RiskEvaluation,
    SignalDecision,
    TechnicalSignal,
    TradeDecisionAssessment,
    TradeQualityAssessment,
)
from services.signal_pipeline_service import SignalPipelineService
from services.trade_quality_service import TradeQualityService


class _Tick:
    def __init__(self, bid=1.1, ask=1.1002, spread=2, tick_time=None):
        self.bid = bid
        self.ask = ask
        self.spread = spread
        self.time = tick_time if tick_time is not None else int(time.time())

    def model_dump(self):
        return {"bid": self.bid, "ask": self.ask, "spread": self.spread, "time": self.time}


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


class _BadTickMarketData(_MarketData):
    def get_tick(self, _symbol):
        return _Tick(bid="bad-bid", ask=float("nan"), tick_time=time.time())


class _MalformedAliasFallbackMarketData(_MarketData):
    def get_tradeable_symbols(self):
        return [
            {"name": "XAGUSD", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
            {"name": "XAG", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
            {"name": "SILVER", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
        ]

    def get_tick(self, symbol):
        if symbol == "XAGUSD":
            return None
        if symbol == "XAG":
            return _Tick(bid="bad-bid", ask="bad-ask", tick_time=time.time())
        return _Tick(bid=24.1, ask=24.13, spread=3, tick_time=time.time())


class _BadSymbolInfoMarketData(_MarketData):
    def get_symbol_info(self, symbol):
        raw = super().get_symbol_info(symbol)
        raw.update(
            {
                "point": "bad-point",
                "digits": "bad-digits",
                "trade_contract_size": float("inf"),
                "volume_min": "bad-volume-min",
                "volume_max": float("nan"),
                "volume_step": "bad-step",
                "trade_stops_level": "bad-stops",
            }
        )
        return raw


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
        self.active_strategy_context = {
            "id": "builtin-default-smart",
            "name": "Default Smart",
            "brief": "Uses trend, momentum, spread, and risk filters before trading.",
            "human_summary": "Balanced multi-asset strategy.",
            "summary_lines": ["Policy mode: balanced", "Asset classes: Indices, Commodities, Stocks"],
            "compiled_strategy": {"min_reward_risk": 1.8, "policy_mode": "balanced"},
        }

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


class _SellAgent:
    def evaluate(self, _input_data):
        return TradeSignal(
            action="SELL",
            confidence=0.78,
            stop_loss=1.1012,
            take_profit=1.0980,
            max_holding_minutes=360,
            reason="Bearish setup",
            strategy="trend_follow",
            metadata={
                "h1_trend": "bearish",
                "h4_trend": "bearish",
                "trend_score": 0.2,
                "momentum_score": 0.15,
                "entry_signal": "sell",
                "entry_score": 0.1,
                "sr_score": 0.1,
                "volume_score": 0.05,
                "atr_pct": 0.35,
                "position_in_range": 0.75,
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

    def is_market_open(self, **_kwargs):
        return True, ""


class _DbRecentLosses:
    async def get_recent_symbol_outcomes(self, _symbol, limit=5, within_minutes=1440):
        return [
            {"action": "SELL", "profit": -120.0, "closed_at": 1710000100.0},
            {"action": "SELL", "profit": -80.0, "closed_at": 1710000000.0},
            {"action": "BUY", "profit": 45.0, "closed_at": 1709999900.0},
        ][:limit]

    async def get_recent_symbol_evaluations(self, _symbol, limit=5, within_minutes=1440):
        return []


class _DbRecentLossesWithNetPnl:
    async def get_recent_symbol_outcomes(self, _symbol, limit=5, within_minutes=1440):
        return [
            {"action": "SELL", "net_pnl": -120.0, "closed_at": 1710000100.0},
            {"action": "SELL", "profit_usd": -80.0, "closed_at": 1710000000.0},
            {"action": "BUY", "pnl": 45.0, "closed_at": 1709999900.0},
        ][:limit]

    async def get_recent_symbol_evaluations(self, _symbol, limit=5, within_minutes=1440):
        return []


class _PersistDb:
    async def log_signal(self, **_kwargs):
        return 42

    async def log_risk_decision(self, **_kwargs):
        return None

    async def log_evaluation_journal(self, **_kwargs):
        return None


class _CapturingResearchRepository:
    def __init__(self):
        self.candidate_payload = None

    async def log_candidate_with_features(self, *, candidate_payload, **_kwargs):
        self.candidate_payload = candidate_payload


class _BlockingMetaModelService:
    def set_database(self, _db):
        return None

    @property
    def is_active(self):
        return True

    async def preview_signal_insight(self, *, quality, **_kwargs):
        return {
            "active": True,
            "profit_probability": 0.2,
            "expected_edge": -0.6,
            "no_trade_probability": 0.8,
            "reason": "blocked_by_test",
            "input_quality_score": float(quality.final_trade_quality_score or 0.0),
        }

    async def assess_trade_decision(
        self,
        *,
        context,
        trade_decision,
        gemini_assessment,
        portfolio_risk_assessment,
        anti_churn_blocked=False,
        refresh_model=True,
    ):
        quality = trade_decision.trade_quality_assessment.model_copy(deep=True)
        quality.no_trade_zone = True
        quality.no_trade_reasons = list(dict.fromkeys((quality.no_trade_reasons or []) + ["Meta-model blocked trade candidate."]))
        signal = trade_decision.final_signal.model_copy(deep=True)
        metadata = dict(signal.metadata or {})
        metadata["meta_model"] = {"changed_decision": True, "active": True, "blocked": True}
        signal.metadata = metadata
        updated = trade_decision.model_copy(
            update={
                "trade": False,
                "final_direction": "HOLD",
                "final_signal": signal,
                "trade_quality_assessment": quality,
                "reasons": list(dict.fromkeys((trade_decision.reasons or []) + ["Meta-model blocked trade candidate."])),
            }
        )
        return updated, {
            "active": True,
            "changed_decision": True,
            "blocked": True,
            "profit_probability": 0.2,
            "expected_edge": -0.6,
            "no_trade_probability": 0.8,
            "reason": "blocked_by_test",
        }


class _GeminiPriceAdvisor:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "strategy_type": "intraday",
            "stop_loss_price": 1.095,
            "take_profit_price": 1.1102,
            "sl_atr_multiplier": 1.2,
            "tp_atr_multiplier": 2.2,
            "max_hold_minutes": 95,
            "confidence_adjustment": 0.03,
            "contradiction_flag": False,
            "summary_reason": "Targets aligned with recent chart structure and news support.",
        }


class _MarginalCostGeminiPriceAdvisor:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "strategy_type": "intraday",
            "stop_loss_price": 1.0992,
            "take_profit_price": 1.1025,
            "sl_atr_multiplier": 1.2,
            "tp_atr_multiplier": 2.2,
            "max_hold_minutes": 95,
            "confidence_adjustment": 0.03,
            "contradiction_flag": False,
            "summary_reason": "Explicit targets are directionally valid but weak after spread cost.",
        }


class _InfiniteGeminiPriceAdvisor:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "strategy_type": "intraday",
            "stop_loss_price": 1.095,
            "take_profit_price": float("inf"),
            "sl_atr_multiplier": 1.2,
            "tp_atr_multiplier": 2.2,
            "max_hold_minutes": 95,
            "confidence_adjustment": 0.03,
            "contradiction_flag": False,
            "summary_reason": "Gemini returned a non-finite take-profit target.",
        }


class _NoopGeminiPriceAdvisor:
    async def assess(self, **_kwargs):
        return {
            "used": False,
            "available": False,
            "degraded": False,
            "summary_reason": "",
            "confidence_adjustment": 0.0,
        }


class _NanNewsEngine:
    def analyze(self, *, context, signal):
        return {
            "sentiment": "bullish",
            "impact": "low",
            "items_count": 1,
            "confidence_adjustment": float("nan"),
            "recommend_hold": False,
            "execution_block": False,
        }


class _MalformedStrengthNewsEngine:
    def analyze(self, *, context, signal):
        return {
            "sentiment": "bearish",
            "impact": "medium",
            "items_count": 1,
            "confidence_adjustment": 0.0,
            "recommend_hold": False,
            "execution_block": False,
            "strength": "bad-strength",
        }


class _MalformedCountNewsEngine:
    def analyze(self, *, context, signal):
        return {
            "sentiment": "bullish",
            "impact": "low",
            "items_count": "bad-count",
            "confidence_adjustment": 0.0,
            "recommend_hold": False,
            "execution_block": False,
        }


class _GeminiTradeBrainService:
    available = True

    async def assess(self, *, gemini_assessment, meta_model_insight, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": 0.86,
            "stop_loss_price": 1.094,
            "take_profit_price": 1.1115,
            "max_hold_minutes": 140,
            "strategy": "gemini_primary_brain",
            "summary_reason": (
                f"Gemini combined chart structure, news ({gemini_assessment.news_bias if gemini_assessment else 'neutral'}), "
                f"and meta insight ({meta_model_insight.get('reason', 'none')})."
            ),
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


class _LowRewardGeminiTradeBrainService:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": 0.86,
            "stop_loss_price": 1.094,
            "take_profit_price": 1.101,
            "max_hold_minutes": 140,
            "strategy": "gemini_primary_brain",
            "summary_reason": "Gemini returned directionally valid but poor reward:risk targets.",
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


class _MarginalCostGeminiTradeBrainService:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": 0.86,
            "stop_loss_price": 1.0992,
            "take_profit_price": 1.1025,
            "max_hold_minutes": 140,
            "strategy": "gemini_primary_brain",
            "summary_reason": "Gemini returned gross-valid targets that are weak after spread cost.",
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


class _OverconfidentGeminiTradeBrainService:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": 1.4,
            "stop_loss_price": 1.094,
            "take_profit_price": 1.1115,
            "max_hold_minutes": 140,
            "strategy": "gemini_primary_brain",
            "summary_reason": "Gemini returned an overconfident score.",
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


class _NanConfidenceGeminiTradeBrainService:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": float("nan"),
            "stop_loss_price": 1.094,
            "take_profit_price": 1.1115,
            "max_hold_minutes": "nan",
            "strategy": "gemini_primary_brain",
            "summary_reason": "Gemini returned malformed numeric fields.",
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


class _InfiniteGeminiTradeBrainService:
    available = True

    async def assess(self, **_kwargs):
        return {
            "used": True,
            "available": True,
            "degraded": False,
            "action": "BUY",
            "confidence": 0.86,
            "stop_loss_price": 1.094,
            "take_profit_price": float("inf"),
            "max_hold_minutes": 140,
            "strategy": "gemini_primary_brain",
            "summary_reason": "Gemini returned a non-finite take-profit target.",
            "market_regime": "trend",
            "risk_note": "Respect deterministic risk gate.",
        }


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
            gemini_trade_brain_service=_GeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertEqual(decision.signal_decision.primary_agent_name, "GeminiAgent")
        self.assertEqual(decision.signal_decision.final_signal.action, "BUY")
        self.assertAlmostEqual(decision.signal_decision.final_signal.confidence, 0.86, places=2)
        self.assertEqual(len(decision.signal_decision.market_context.normalized_news), 1)
        self.assertGreaterEqual(decision.trade_quality_assessment.final_trade_quality_score, 0.7)
        self.assertTrue(decision.allow_execute)
        self.assertEqual(decision.position_management_plan.planned_hold_minutes, 120)
        self.assertEqual(decision.signal_decision.final_signal.max_holding_minutes, 120)
        self.assertTrue(decision.signal_decision.primary_signal.metadata.get("gemini_trade_brain", {}).get("used"))

    def test_trade_brain_rejects_low_reward_risk_price_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            gemini_trade_brain_service=_LowRewardGeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        primary_signal = decision.signal_decision.primary_signal
        baseline = primary_signal.metadata.get("smart_agent_baseline", {})
        self.assertAlmostEqual(primary_signal.stop_loss, baseline.get("stop_loss"), places=5)
        self.assertAlmostEqual(primary_signal.take_profit, baseline.get("take_profit"), places=5)
        self.assertNotAlmostEqual(primary_signal.take_profit, 1.101, places=5)
        self.assertEqual(
            primary_signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "rejected_low_reward_risk",
        )

    def test_trade_brain_rejects_spread_adjusted_low_reward_risk_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            gemini_trade_brain_service=_MarginalCostGeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        primary_signal = decision.signal_decision.primary_signal
        baseline = primary_signal.metadata.get("smart_agent_baseline", {})
        self.assertAlmostEqual(primary_signal.stop_loss, baseline.get("stop_loss"), places=5)
        self.assertAlmostEqual(primary_signal.take_profit, baseline.get("take_profit"), places=5)
        self.assertNotAlmostEqual(primary_signal.take_profit, 1.1025, places=5)
        self.assertEqual(
            primary_signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "rejected_low_reward_risk",
        )

    def test_trade_brain_confidence_is_bounded_before_risk_gates(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            gemini_trade_brain_service=_OverconfidentGeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertLessEqual(decision.signal_decision.primary_signal.confidence, 0.95)
        self.assertLessEqual(decision.signal_decision.final_signal.confidence, 0.95)

    def test_trade_brain_malformed_confidence_falls_back_to_baseline(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            gemini_trade_brain_service=_NanConfidenceGeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        primary_signal = decision.signal_decision.primary_signal
        baseline = primary_signal.metadata.get("smart_agent_baseline", {})
        self.assertEqual(primary_signal.action, "BUY")
        self.assertAlmostEqual(primary_signal.confidence, baseline.get("confidence"), places=5)
        self.assertEqual(primary_signal.max_holding_minutes, baseline.get("max_holding_minutes"))

    def test_trade_brain_baseline_metadata_sanitizes_non_finite_confidence(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"min_reward_risk": 1.8},
        )
        baseline_signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=float("nan"),
            stop_loss=1.09,
            take_profit=1.13,
            max_holding_minutes=360,
            reason="baseline",
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline_signal,
            assessment={
                "used": True,
                "available": True,
                "action": "BUY",
                "confidence": float("nan"),
                "stop_loss_price": 1.09,
                "take_profit_price": 1.13,
                "max_hold_minutes": 360,
                "summary_reason": "malformed confidence",
            },
        )

        baseline = signal.metadata.get("smart_agent_baseline", {})
        self.assertEqual(signal.confidence, 0.0)
        self.assertEqual(baseline.get("confidence"), 0.0)
        self.assertTrue(math.isfinite(baseline.get("confidence")))

    def test_trade_brain_rejects_non_finite_price_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            gemini_trade_brain_service=_InfiniteGeminiTradeBrainService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="GeminiAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        primary_signal = decision.signal_decision.primary_signal
        baseline = primary_signal.metadata.get("smart_agent_baseline", {})
        self.assertAlmostEqual(primary_signal.stop_loss, baseline.get("stop_loss"), places=5)
        self.assertAlmostEqual(primary_signal.take_profit, baseline.get("take_profit"), places=5)
        self.assertNotEqual(primary_signal.take_profit, float("inf"))
        self.assertEqual(
            primary_signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "rejected_low_reward_risk",
        )

    def test_trade_brain_blocks_targets_when_spread_is_malformed(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002, "spread": "bad-spread"},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", point=0.0001),
            user_policy={"min_reward_risk": 1.8},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline,
            assessment={
                "used": True,
                "available": True,
                "action": "BUY",
                "confidence": 0.8,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "max_hold_minutes": 120,
            },
        )

        self.assertEqual(signal.action, "HOLD")
        self.assertEqual(
            signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "blocked_low_reward_risk",
        )

    def test_target_reward_risk_validator_sanitizes_malformed_minimum(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        valid = pipeline._targets_meet_reward_risk(
            entry_price=1.1000,
            action="BUY",
            stop_loss=1.0990,
            take_profit=1.1020,
            min_reward_risk="bad-rr",
            spread_points=0.0,
            point=0.0001,
        )

        self.assertTrue(valid)

    def test_target_reward_risk_validator_normalizes_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        valid = pipeline._targets_meet_reward_risk(
            entry_price=1.1000,
            action=" buy ",
            stop_loss=1.0990,
            take_profit=1.1020,
            min_reward_risk=1.8,
            spread_points=0.0,
            point=0.0001,
        )

        self.assertTrue(valid)

    def test_target_reward_risk_validator_normalizes_sell_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        valid = pipeline._targets_meet_reward_risk(
            entry_price=1.1000,
            action=" sell ",
            stop_loss=1.1010,
            take_profit=1.0980,
            min_reward_risk=1.8,
            spread_points=0.0,
            point=0.0001,
        )

        self.assertTrue(valid)

    def test_target_reward_risk_validator_rejects_malformed_spread_cost_inputs(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        valid = pipeline._targets_meet_reward_risk(
            entry_price=1.1000,
            action="BUY",
            stop_loss=1.0990,
            take_profit=1.1020,
            min_reward_risk=1.8,
            spread_points="bad-spread",
            point=0.0001,
        )

        self.assertFalse(valid)

    def test_target_reward_risk_validator_sanitizes_malformed_point_input(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        valid = pipeline._targets_meet_reward_risk(
            entry_price=1.1000,
            action="BUY",
            stop_loss=1.0990,
            take_profit=1.1020,
            min_reward_risk=1.8,
            spread_points=2,
            point="bad-point",
        )

        self.assertTrue(valid)

    def test_trade_brain_sanitizes_malformed_min_reward_risk_policy(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", point=0.0001),
            user_policy={"min_reward_risk": "bad-rr"},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline,
            assessment={
                "used": True,
                "available": True,
                "action": "BUY",
                "confidence": 0.8,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "max_hold_minutes": 120,
            },
        )

        self.assertEqual(signal.action, "BUY")
        self.assertEqual(
            signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "accepted",
        )

    def test_trade_brain_normalizes_action_text_before_target_validation(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", point=0.0001),
            user_policy={"min_reward_risk": 1.8},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline,
            assessment={
                "used": True,
                "available": True,
                "action": " buy ",
                "confidence": 0.8,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "max_hold_minutes": 120,
            },
        )

        self.assertEqual(signal.action, "BUY")
        self.assertEqual(
            signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "accepted",
        )

    def test_trade_brain_blocks_unknown_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", point=0.0001),
            user_policy={"min_reward_risk": 1.8},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline,
            assessment={
                "used": True,
                "available": True,
                "action": "open-long",
                "confidence": 0.8,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "max_hold_minutes": 120,
            },
        )

        self.assertEqual(signal.action, "HOLD")
        self.assertIsNone(signal.stop_loss)
        self.assertIsNone(signal.take_profit)
        self.assertEqual(
            signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "blocked_unknown_action",
        )

    def test_trade_brain_blocks_malformed_entry_price_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": 1.1, "ask": "bad-ask", "spread": 2},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", point=0.0001),
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        baseline = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.65,
            stop_loss=1.099,
            take_profit=1.103,
        )

        signal = pipeline._signal_from_trade_brain(
            context=context,
            baseline_signal=baseline,
            assessment={
                "used": True,
                "available": True,
                "action": "BUY",
                "confidence": 0.7,
                "stop_loss_price": 1.099,
                "take_profit_price": 1.104,
                "max_hold_minutes": 120,
            },
        )

        self.assertEqual(signal.action, "HOLD")
        self.assertIsNone(signal.stop_loss)
        self.assertIsNone(signal.take_profit)
        self.assertEqual(
            signal.metadata.get("gemini_trade_brain", {}).get("target_sanitization"),
            "blocked_low_reward_risk",
        )

    def test_market_live_guard_blocks_malformed_bid_ask_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="EURUSD",
            tick={"bid": "bad-bid", "ask": float("nan"), "time": time.time()},
            symbol_info=NormalizedSymbolInfo(name="EURUSD", category="Forex", trade_enabled=True),
        )

        reason = pipeline._market_live_guard_reason(context)

        self.assertEqual(reason, "Market closed (no executable live bid/ask).")

    def test_session_tick_recency_rejects_malformed_tick_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        fresh = pipeline._has_recent_executable_tick(
            {"bid": "bad-bid", "ask": 1.1002, "time": time.time()},
            max_age_seconds=60,
        )

        self.assertFalse(fresh)

    def test_market_session_guard_blocks_malformed_tick_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_BadTickMarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        reason = pipeline.market_session_guard_for_symbol(
            "US500",
            [{"name": "US500", "category": "Indices", "trade_enabled": True, "visible": True}],
        )

        self.assertEqual(reason, "Market closed or no executable live bid/ask for US500.")

    def test_build_market_context_skips_malformed_alias_tick_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MalformedAliasFallbackMarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        context = pipeline.build_market_context("XAGUSD", requested_timeframe="H1", bar_count=10)

        self.assertEqual(context.symbol, "SILVER")
        self.assertEqual(context.tick["bid"], 24.1)
        self.assertEqual(context.tick["ask"], 24.13)

    def test_symbol_normalization_sanitizes_malformed_tick_spread(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        symbol_info = pipeline._normalize_symbol_info(
            "US500",
            {"bid": 1.1, "ask": 1.1002, "spread": "bad-spread"},
        )

        self.assertIsNotNone(symbol_info)
        self.assertEqual(symbol_info.spread, 0.0)
        self.assertTrue(math.isfinite(symbol_info.spread))

    def test_symbol_normalization_sanitizes_malformed_broker_metadata(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_BadSymbolInfoMarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        symbol_info = pipeline._normalize_symbol_info(
            "US500",
            {"bid": 1.1, "ask": 1.1002, "spread": 2},
        )

        self.assertIsNotNone(symbol_info)
        self.assertEqual(symbol_info.point, 0.0)
        self.assertEqual(symbol_info.digits, 5)
        self.assertEqual(symbol_info.trade_contract_size, 0.0)
        self.assertEqual(symbol_info.volume_min, 0.01)
        self.assertEqual(symbol_info.volume_max, 0.0)
        self.assertEqual(symbol_info.volume_step, 0.01)
        self.assertEqual(symbol_info.trade_stops_level, 0.0)

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
                    {"name": "XAGUSD", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
                    {"name": "EURUSD", "category": "Forex", "path": "forex\\majors", "trade_enabled": True, "visible": True},
                ],
                limit=5,
            )
        )

        self.assertIn("US500", symbols)
        self.assertIn("XAUUSD", symbols)
        self.assertIn("XAGUSD", symbols)
        self.assertIn("TSLA", symbols)
        self.assertNotIn("EURUSD", symbols)

    def test_auto_trade_candidate_symbols_balances_categories_when_limit_is_tight(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_trade_brain_service=_GeminiTradeBrainService(),
        )

        symbols = asyncio.run(
            pipeline.auto_trade_candidate_symbols(
                [
                    {"name": "US500", "category": "Indices", "path": "indices\\cash", "trade_enabled": True, "visible": True},
                    {"name": "US100", "category": "Indices", "path": "indices\\cash", "trade_enabled": True, "visible": True},
                    {"name": "AAPL", "category": "Stocks", "path": "stocks\\us", "trade_enabled": True, "visible": True},
                    {"name": "MSFT", "category": "Stocks", "path": "stocks\\us", "trade_enabled": True, "visible": True},
                    {"name": "XAUUSD", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
                    {"name": "XAGUSD", "category": "Commodities", "path": "commodities\\metals", "trade_enabled": True, "visible": True},
                ],
                limit=3,
            )
        )

        self.assertEqual(len(symbols), 3)
        self.assertTrue(any(symbol in {"AAPL", "MSFT"} for symbol in symbols))
        self.assertTrue(any(symbol in {"US500", "US100"} for symbol in symbols))
        self.assertTrue(any(symbol in {"XAUUSD", "XAGUSD"} for symbol in symbols))

    def test_aggressive_mode_scales_sl_tp_distance_by_5_percent(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=95.0,
            take_profit=110.0,
            metadata={"digits": 2},
        )

        scaled = pipeline._apply_mode_target_scaling(signal, entry_price=100.0, mode="aggressive")

        self.assertEqual(scaled.stop_loss, 94.75)
        self.assertEqual(scaled.take_profit, 110.5)

    def test_mode_target_scaling_skips_non_finite_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=float("nan"),
            take_profit=float("inf"),
            metadata={"digits": 2},
        )

        scaled = pipeline._apply_mode_target_scaling(signal, entry_price=100.0, mode="aggressive")

        self.assertIs(scaled, signal)
        self.assertFalse(math.isfinite(scaled.stop_loss))
        self.assertFalse(math.isfinite(scaled.take_profit))

    def test_mode_target_scaling_skips_directionally_invalid_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=105.0,
            take_profit=90.0,
            metadata={"digits": 2},
        )

        scaled = pipeline._apply_mode_target_scaling(signal, entry_price=100.0, mode="aggressive")

        self.assertIs(scaled, signal)
        self.assertEqual(scaled.stop_loss, 105.0)
        self.assertEqual(scaled.take_profit, 90.0)

    def test_mode_target_scaling_normalizes_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=95.0,
            take_profit=110.0,
            metadata={"digits": 2},
        )
        signal.action = " buy "

        scaled = pipeline._apply_mode_target_scaling(signal, entry_price=100.0, mode="aggressive")

        self.assertEqual(scaled.action, "BUY")
        self.assertEqual(scaled.stop_loss, 94.75)
        self.assertEqual(scaled.take_profit, 110.5)

    def test_mode_target_scaling_normalizes_sell_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.7,
            stop_loss=105.0,
            take_profit=90.0,
            metadata={"digits": 2},
        )
        signal.action = " sell "

        scaled = pipeline._apply_mode_target_scaling(signal, entry_price=100.0, mode="aggressive")

        self.assertEqual(scaled.action, "SELL")
        self.assertEqual(scaled.stop_loss, 105.25)
        self.assertEqual(scaled.take_profit, 89.5)

    def test_outcome_learning_flips_direction_after_repeated_losses(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SellAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            db=_DbRecentLosses(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="auto",
                bar_count=60,
            )
        )

        self.assertEqual(decision.signal_decision.primary_signal.action, "BUY")
        self.assertIn("Direction adapted", decision.signal_decision.primary_signal.reason)

    def test_outcome_learning_reads_alternate_pnl_fields(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SellAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            db=_DbRecentLossesWithNetPnl(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="auto",
                bar_count=60,
            )
        )

        self.assertEqual(decision.signal_decision.primary_signal.action, "BUY")
        learning = decision.signal_decision.primary_signal.metadata.get("outcome_learning", {})
        self.assertEqual(learning.get("same_direction_pnl"), -200.0)
        self.assertEqual(learning.get("opposite_direction_pnl"), 45.0)

    def test_outcome_learning_skips_non_finite_pnl_fields(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        profit = pipeline._outcome_profit(
            {
                "action": "BUY",
                "profit": float("nan"),
                "profit_usd": float("inf"),
                "net_pnl": -120.0,
            }
        )

        self.assertEqual(profit, -120.0)

    def test_outcome_learning_parses_currency_pnl_strings(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        self.assertEqual(pipeline._outcome_profit({"profit": "-$1,200.50"}), -1200.5)
        self.assertEqual(pipeline._outcome_profit({"profit": "($320.25)"}), -320.25)
        self.assertEqual(pipeline._outcome_profit({"profit": "$1,250.75"}), 1250.75)

    def test_outcome_learning_sanitizes_malformed_signal_confidence(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.09,
            take_profit=1.13,
        )
        signal.confidence = "bad-confidence"

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "BUY", "profit": -12.0},
                {"action": "BUY", "profit": -8.0},
            ],
        )

        self.assertEqual(adjusted.confidence, 0.0)
        self.assertTrue(reasons)

    def test_outcome_learning_ignores_malformed_outcome_rows(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
        )

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                ["bad-row"],
                {"action": "BUY", "profit": -12.0},
                "bad-row",
                {"action": "BUY", "profit": -8.0},
            ],
        )

        self.assertLess(adjusted.confidence, 0.7)
        self.assertTrue(reasons)

    def test_outcome_learning_normalizes_outcome_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
        )

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": " buy ", "profit": -12.0},
                {"action": "Buy", "profit": -8.0},
            ],
        )

        self.assertLess(adjusted.confidence, 0.7)
        self.assertTrue(reasons)

    def test_outcome_learning_normalizes_signal_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.09,
            take_profit=1.13,
        )
        signal.action = " buy "

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "BUY", "profit": -12.0},
                {"action": "BUY", "profit": -8.0},
            ],
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertLess(adjusted.confidence, 0.9)
        self.assertTrue(reasons)

    def test_aggressive_outcome_learning_ignores_stale_cfd_loss_pair(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        now = time.time()
        context = MarketContext(
            symbol="XAGUSD",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.7,
            stop_loss=77.0,
            take_profit=73.0,
        )

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "SELL", "profit": -13.8, "exit_reason": "breakeven", "closed_at": now - (20 * 60 * 60)},
                {"action": "SELL", "profit": -73.8, "exit_reason": "stop_loss_amount", "closed_at": now - (21 * 60 * 60)},
            ],
        )

        self.assertEqual(adjusted.action, "SELL")
        self.assertEqual(adjusted.confidence, 0.7)
        self.assertEqual(reasons, [])

    def test_outcome_learning_uses_normalized_action_for_drift_flip(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced"},
            bars_by_timeframe={
                "H1": [
                    {"close": 1.1},
                    {"close": 1.101},
                    {"close": 1.102},
                    {"close": 1.103},
                    {"close": 1.104},
                    {"close": 1.106},
                ]
            },
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.5,
            stop_loss=1.1012,
            take_profit=1.098,
            metadata={"h1_trend": "bearish", "entry_signal": "sell"},
        )
        signal.action = " sell "

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "SELL", "profit": -80.0},
                {"action": "SELL", "profit": -60.0},
                {"action": "SELL", "profit": -40.0},
                {"action": "BUY", "profit": 40.0},
                {"action": "BUY", "profit": -5.0},
            ],
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertTrue(any("Direction adapted" in reason for reason in reasons))

    def test_outcome_learning_normalizes_severe_exit_reason_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.09,
            take_profit=1.13,
        )

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "BUY", "profit": -12.0, "exit_reason": " stop_loss "},
                {"action": "BUY", "profit": -8.0, "exit_reason": "Stop_Loss"},
            ],
        )

        self.assertEqual(adjusted.action, "HOLD")
        self.assertTrue(any("managed loss exits" in reason for reason in reasons))

    def test_outcome_learning_treats_stop_loss_exit_aliases_as_severe(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "aggressive"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.09,
            take_profit=1.13,
        )

        adjusted, reasons = pipeline._apply_outcome_learning(
            context=context,
            signal=signal,
            recent_outcomes=[
                {"action": "BUY", "profit": -12.0, "exit_reason": "Stop Loss"},
                {"action": "BUY", "profit": -8.0, "exit_reason": "stopped out"},
            ],
        )

        self.assertEqual(adjusted.action, "HOLD")
        self.assertTrue(any("managed loss exits" in reason for reason in reasons))

    def test_outcome_learning_flip_sanitizes_non_finite_confidence(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=float("nan"),
            stop_loss=1.1012,
            take_profit=1.098,
            metadata={"h1_trend": "bearish", "entry_signal": "sell"},
        )

        flipped = pipeline._flip_signal_action(context, signal)

        self.assertEqual(flipped.action, "BUY")
        self.assertEqual(flipped.confidence, 0.45)
        self.assertTrue(math.isfinite(flipped.confidence))

    def test_outcome_learning_flip_skips_malformed_tick_without_crashing(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": "bad-bid", "ask": float("nan"), "spread": 2},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.72,
            stop_loss=1.1012,
            take_profit=1.098,
            metadata={"h1_trend": "bearish", "entry_signal": "sell"},
        )

        flipped = pipeline._flip_signal_action(context, signal)

        self.assertIs(flipped, signal)
        self.assertEqual(flipped.action, "SELL")

    def test_recent_drift_skips_malformed_bar_closes(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            bars_by_timeframe={
                "H1": [
                    {"close": "bad-close"},
                    {"close": float("nan")},
                    {"close": 1.1},
                    {"close": 1.099},
                    {"close": 1.098},
                    {"close": 1.096},
                ]
            },
        )

        direction, drift_pct = pipeline._recent_drift(context)

        self.assertEqual(direction, "bearish")
        self.assertGreater(drift_pct, 0.03)
        self.assertTrue(math.isfinite(drift_pct))

    def test_recent_drift_skips_malformed_bar_rows(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(symbol="US500")
        context.bars_by_timeframe = {
            "H1": [
                {"close": 1.1},
                ["bad-row"],
                {"close": 1.099},
                "bad-row",
                {"close": 1.098},
                {"close": 1.096},
            ]
        }

        direction, drift_pct = pipeline._recent_drift(context)

        self.assertEqual(direction, "bearish")
        self.assertGreater(drift_pct, 0.03)

    def test_meta_model_can_block_but_risk_still_remains_in_path(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            meta_model_service=_BlockingMetaModelService(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertFalse(decision.allow_execute)
        self.assertEqual(decision.signal_decision.final_signal.action, "HOLD")
        self.assertTrue(decision.risk_evaluation.metrics_snapshot.get("meta_model", {}).get("blocked"))
        self.assertIn("meta_model", decision.risk_evaluation.metrics_snapshot)

    def test_strategy_advisor_can_apply_explicit_price_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            gemini_strategy_advisor_service=_GeminiPriceAdvisor(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        final_signal = decision.signal_decision.final_signal
        self.assertAlmostEqual(final_signal.stop_loss, 1.095, places=5)
        self.assertAlmostEqual(final_signal.take_profit, 1.1102, places=5)
        self.assertEqual(final_signal.max_holding_minutes, 95)
        self.assertTrue(final_signal.metadata.get("strategy_advisory", {}).get("used_explicit_price_targets"))

    def test_strategy_advisor_normalizes_action_text_before_target_application(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": 5},
        )
        signal.action = " buy "

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 95,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertAlmostEqual(adjusted.stop_loss, 1.095, places=5)
        self.assertAlmostEqual(adjusted.take_profit, 1.112, places=5)
        self.assertTrue(adjusted.metadata.get("strategy_advisory", {}).get("used_explicit_price_targets"))

    def test_strategy_advisor_normalizes_sell_action_text_before_target_application(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="SELL",
            confidence=0.7,
            stop_loss=1.105,
            take_profit=1.09,
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": 5},
        )
        signal.action = " sell "

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "stop_loss_price": 1.105,
                "take_profit_price": 1.09,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 95,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "SELL")
        self.assertAlmostEqual(adjusted.stop_loss, 1.105, places=5)
        self.assertAlmostEqual(adjusted.take_profit, 1.09, places=5)
        self.assertTrue(adjusted.metadata.get("strategy_advisory", {}).get("used_explicit_price_targets"))

    def test_strategy_advisor_blocks_unknown_action_text(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": 5},
        )
        signal.action = "open-long"

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "stop_loss_price": 1.095,
                "take_profit_price": 1.112,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 95,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "HOLD")
        self.assertIsNone(adjusted.stop_loss)
        self.assertIsNone(adjusted.take_profit)
        self.assertEqual(
            adjusted.metadata.get("strategy_advisory", {}).get("target_sanitization"),
            "blocked_unknown_action",
        )

    def test_strategy_advisor_rejects_spread_adjusted_low_reward_risk_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            gemini_strategy_advisor_service=_MarginalCostGeminiPriceAdvisor(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        final_signal = decision.signal_decision.final_signal
        advisory = final_signal.metadata.get("strategy_advisory", {})
        self.assertFalse(advisory.get("used_explicit_price_targets"))
        self.assertNotAlmostEqual(final_signal.stop_loss, 1.0992, places=5)
        self.assertNotAlmostEqual(final_signal.take_profit, 1.1025, places=5)

    def test_strategy_advisor_rejects_non_finite_price_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            gemini_strategy_advisor_service=_InfiniteGeminiPriceAdvisor(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        final_signal = decision.signal_decision.final_signal
        advisory = final_signal.metadata.get("strategy_advisory", {})
        self.assertFalse(advisory.get("used_explicit_price_targets"))
        self.assertNotEqual(final_signal.take_profit, float("inf"))

    def test_strategy_advisor_does_not_turn_nan_confidence_into_max_confidence(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=float("nan"),
            stop_loss=1.09,
            take_profit=1.13,
            max_holding_minutes=360,
            metadata={"atr": 0.01},
        )

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 120,
                "confidence_adjustment": 0.05,
            },
        )

        self.assertEqual(adjusted.confidence, 0.05)
        self.assertLess(adjusted.confidence, 0.95)
        self.assertTrue(math.isfinite(adjusted.confidence))

    def test_strategy_advisor_sanitizes_malformed_rounding_digits(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": "bad-digits"},
        )

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 120,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertTrue(math.isfinite(adjusted.stop_loss))
        self.assertTrue(math.isfinite(adjusted.take_profit))

    def test_strategy_advisor_sanitizes_malformed_atr_metadata(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=360,
            metadata={"atr": "bad-atr", "digits": 5},
        )

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 120,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertTrue(math.isfinite(adjusted.stop_loss))
        self.assertTrue(math.isfinite(adjusted.take_profit))
        self.assertLess(adjusted.stop_loss, 1.1002)
        self.assertGreater(adjusted.take_profit, 1.1002)

    def test_strategy_advisor_handles_malformed_existing_targets(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=float("nan"),
            take_profit=float("inf"),
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": 5},
        )

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 120,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertTrue(math.isfinite(adjusted.stop_loss))
        self.assertTrue(math.isfinite(adjusted.take_profit))
        self.assertLess(adjusted.stop_loss, 1.1002)
        self.assertGreater(adjusted.take_profit, 1.1002)

    def test_strategy_advisor_sanitizes_malformed_min_reward_risk_policy(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced", "min_reward_risk": "bad-rr"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.095,
            take_profit=1.112,
            max_holding_minutes=360,
            metadata={"atr": 0.004, "digits": 5},
        )

        adjusted = pipeline._apply_strategy_advisory(
            context=context,
            signal=signal,
            advisory={
                "used": True,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.7,
                "max_hold_minutes": 120,
                "confidence_adjustment": 0.0,
            },
        )

        self.assertEqual(adjusted.action, "BUY")
        self.assertTrue(math.isfinite(adjusted.stop_loss))
        self.assertTrue(math.isfinite(adjusted.take_profit))

    def test_strategy_advisor_clamp_sanitizes_non_finite_numeric_values(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            user_policy={"mode": "balanced", "min_reward_risk": 1.8},
        )

        clamped = pipeline._clamp_strategy_advisory(
            context,
            {
                "sl_atr_multiplier": float("nan"),
                "tp_atr_multiplier": float("inf"),
                "max_hold_minutes": "nan",
                "confidence_adjustment": float("nan"),
            },
        )

        self.assertEqual(clamped["sl_atr_multiplier"], 1.5)
        self.assertEqual(clamped["tp_atr_multiplier"], 2.7)
        self.assertEqual(clamped["max_hold_minutes"], 120)
        self.assertEqual(clamped["confidence_adjustment"], 0.0)

    def test_event_context_advisory_ignores_non_finite_confidence_adjustment(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            news_engine=_NanNewsEngine(),
        )
        context = MarketContext(
            symbol="US500",
            normalized_news=[
                NormalizedNewsItem(
                    source="wire",
                    category="headline",
                    title="Market update",
                    published_at=1.0,
                    received_at=1.0,
                    affected_symbols=["US500"],
                )
            ],
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.62, reason="baseline")

        adjusted = pipeline._apply_event_context_advisory(context, signal)

        self.assertEqual(adjusted.confidence, 0.62)

    def test_news_conflict_gate_treats_malformed_strength_as_weak(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            event_ingestion_service=_EventIngestionService(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            gemini_strategy_advisor_service=_NoopGeminiPriceAdvisor(),
            news_engine=_MalformedStrengthNewsEngine(),
        )
        pipeline.cloud_brain_decision_client.enabled = False

        decision = asyncio.run(
            pipeline.evaluate(
                symbol="US500",
                requested_agent_name="SmartAgent",
                requested_timeframe="H1",
                evaluation_mode="manual",
                bar_count=60,
            )
        )

        self.assertEqual(decision.signal_decision.final_signal.action, "BUY")
        self.assertNotIn("strong conflicting news", decision.reason.lower())

    def test_event_context_advisory_sanitizes_malformed_news_item_count(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            news_engine=_MalformedCountNewsEngine(),
        )
        context = MarketContext(
            symbol="US500",
            normalized_news=[
                NormalizedNewsItem(
                    source="wire",
                    category="headline",
                    title="Market update",
                    published_at=1.0,
                    received_at=1.0,
                    affected_symbols=["US500"],
                )
            ],
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.62, reason="baseline")

        adjusted = pipeline._apply_event_context_advisory(context, signal)

        self.assertEqual(adjusted.metadata["event_context"]["news_items"], 0)
        self.assertEqual(adjusted.action, "BUY")

    def test_cloud_decision_ignores_non_finite_confidence(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.61,
            stop_loss=1.095,
            take_profit=1.112,
            reason="baseline",
            metadata={"category": "indices"},
        )
        trade_decision = TradeDecisionAssessment(
            trade=True,
            final_direction="BUY",
            final_signal=signal,
            trade_quality_assessment=TradeQualityAssessment(
                final_trade_quality_score=0.8,
                threshold=0.7,
                no_trade_zone=False,
            ),
        )

        updated, cloud = pipeline._apply_cloud_decision(
            trade_decision=trade_decision,
            cloud_decision={
                "used": True,
                "action": "BUY",
                "confidence": float("nan"),
                "reason": "malformed cloud confidence",
            },
        )

        self.assertEqual(updated.final_signal.confidence, 0.61)
        self.assertEqual(cloud["applied_action"], "BUY")

    def test_cloud_timeout_seconds_sanitizes_malformed_client_timeout(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        pipeline.cloud_brain_decision_client.timeout_seconds = "bad-timeout"

        timeout_seconds = pipeline._cloud_decision_timeout_seconds("manual")

        self.assertEqual(timeout_seconds, 6.0)

    def test_cloud_decision_treats_malformed_quality_score_as_not_strong(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
            metadata={"cloud_brain": {"mode": "aggressive"}, "category": "stocks"},
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.8,
            threshold=0.7,
            no_trade_zone=False,
        )
        quality.final_trade_quality_score = "bad-score"
        trade_decision = TradeDecisionAssessment(
            trade=True,
            final_direction="BUY",
            final_signal=signal,
            trade_quality_assessment=quality,
        )

        updated, cloud = pipeline._apply_cloud_decision(
            trade_decision=trade_decision,
            cloud_decision={
                "used": True,
                "action": "HOLD",
                "confidence": 0.4,
                "reason": "meta-model caution",
            },
        )

        self.assertEqual(updated.final_signal.action, "HOLD")
        self.assertEqual(cloud["applied_action"], "HOLD")
        self.assertNotIn("advisory_only", cloud)

    def test_cloud_decision_payload_sanitizes_non_finite_decision_numbers(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(
            symbol="US500",
            requested_timeframe="H1",
            evaluation_mode="manual",
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            user_policy={"mode": "balanced"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=float("nan"),
            stop_loss=1.095,
            take_profit=1.112,
            reason="baseline",
        )
        trade_decision = TradeDecisionAssessment(
            trade=True,
            final_direction="BUY",
            final_signal=signal,
            trade_quality_assessment=TradeQualityAssessment(
                final_trade_quality_score=float("nan"),
                threshold=float("inf"),
                no_trade_zone=False,
            ),
        )

        payload = pipeline._build_cloud_decision_payload(
            context=context,
            trade_decision=trade_decision,
            gemini_assessment=None,
            meta_assessment={"profit_probability": float("nan")},
        )

        self.assertEqual(payload["signal"]["confidence"], 0.0)
        self.assertEqual(payload["quality"]["score"], 0.0)
        self.assertEqual(payload["quality"]["threshold"], 0.0)
        self.assertIsNone(payload["meta_model"]["profit_probability"])
        self.assertTrue(math.isfinite(payload["signal"]["confidence"]))
        self.assertTrue(math.isfinite(payload["quality"]["score"]))
        self.assertTrue(math.isfinite(payload["quality"]["threshold"]))

    def test_persist_evaluation_sanitizes_malformed_candidate_numeric_metadata(self):
        research_repository = _CapturingResearchRepository()
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
            db=_PersistDb(),
        )
        pipeline.research_repository = research_repository
        context = MarketContext(
            symbol="US500",
            tick={"bid": 1.1, "ask": 1.1002, "spread": "bad-spread"},
            symbol_info=NormalizedSymbolInfo(name="US500", category="Indices", point=0.0001),
            user_policy={"mode": "balanced"},
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
            metadata={
                "reward_risk_ratio": "bad-rr",
                "event_context": {
                    "confidence_adjustment": "bad-adjustment",
                    "bias": "bullish",
                    "event_risk": "low",
                },
            },
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.8,
            threshold=0.7,
            no_trade_zone=False,
        )
        execution_decision = ExecutionDecision(
            allow_execute=True,
            reason="Ready for execution",
            signal_decision=SignalDecision(
                requested_agent_name="SmartAgent",
                primary_agent_name="SmartAgent",
                final_agent_name="SmartAgent",
                market_context=context,
                primary_signal=signal,
                final_signal=signal,
            ),
            trade_decision_assessment=TradeDecisionAssessment(
                trade=True,
                final_direction="BUY",
                final_signal=signal,
                trade_quality_assessment=quality,
            ),
            risk_evaluation=RiskEvaluation(
                approved=True,
                reason="Ready for execution",
                adjusted_volume=0.02,
                machine_reasons=[],
            ),
            trade_quality_assessment=quality,
            portfolio_risk_assessment=PortfolioRiskAssessment(
                status="pass",
                allow_execute=True,
                reason="ok",
            ),
            anti_churn_assessment=AntiChurnAssessment(),
            position_management_plan=PositionManagementPlan(
                manage_position=True,
                max_holding_minutes=120,
            ),
            entry_price=1.1002,
        )

        signal_id = asyncio.run(pipeline.persist_evaluation(execution_decision, "H1"))

        self.assertEqual(signal_id, 42)
        self.assertIsNotNone(research_repository.candidate_payload)
        self.assertEqual(research_repository.candidate_payload["reward_risk"], 0.0)
        self.assertEqual(research_repository.candidate_payload["spread_at_eval"], 0.0)
        self.assertEqual(research_repository.candidate_payload["event_confidence_adjustment"], 0.0)

    def test_position_management_plan_sanitizes_malformed_atr_pct(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(symbol="US500")
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
            max_holding_minutes=120,
            strategy="trend_follow",
            metadata={
                "atr_pct": "bad-atr-pct",
                "h1_trend": "bullish",
                "h4_trend": "bullish",
                "entry_signal": "buy",
            },
        )
        quality = TradeQualityAssessment(
            final_trade_quality_score=0.8,
            threshold=0.7,
            no_trade_zone=False,
        )

        plan = pipeline._build_position_management_plan(
            context=context,
            signal=signal,
            trade_quality=quality,
            gemini_assessment=None,
        )

        self.assertEqual(plan.atr_regime, "low")
        self.assertEqual(plan.planned_hold_minutes, 120)

    def test_planned_hold_minutes_sanitizes_malformed_requested_hold(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(symbol="US500")
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
        )
        signal.max_holding_minutes = "bad-hold"

        planned = pipeline._planned_hold_minutes(
            context=context,
            signal=signal,
            strategy_family="intraday_technical",
        )

        self.assertEqual(planned, 180)

    def test_position_management_plan_sanitizes_malformed_quality_score_in_thesis(self):
        pipeline = SignalPipelineService(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=_Execution(),
            risk_engine=_RiskEngine(),
            agents={"SmartAgent": _SmartAgent()},
            gemini_adapter=_GeminiAdapter(),
            trade_quality_service=_AlwaysOpenTradeQualityService(),
        )
        context = MarketContext(symbol="US500")
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.7,
            stop_loss=1.09,
            take_profit=1.13,
            max_holding_minutes=120,
            strategy="trend_follow",
            metadata={"h1_trend": "bullish", "h4_trend": "bullish", "entry_signal": "buy"},
        )
        quality = TradeQualityAssessment(final_trade_quality_score=0.8, threshold=0.7, no_trade_zone=False)
        quality.final_trade_quality_score = "bad-quality"

        plan = pipeline._build_position_management_plan(
            context=context,
            signal=signal,
            trade_quality=quality,
            gemini_assessment=None,
        )

        self.assertIn("quality=0.00", plan.initial_thesis)
        self.assertEqual(plan.planned_hold_minutes, 120)
