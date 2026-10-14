import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ExecutionPreflightAssessment, MarketContext, NormalizedSymbolInfo, SymbolProfile, TechnicalSignal
from mt5.execution import OrderRequest, OrderResult
from risk.rules import RiskSettings
from services.execution_service import ExecutionService


class _ExecutionEngine:
    def __init__(self):
        self.placed = []
        self.margin_calls = []

    def place_order(self, order_request):
        self.placed.append(order_request)
        return OrderResult(
            success=True,
            retcode=10009,
            retcode_desc="Request completed",
            ticket=123,
            stop_loss=order_request.stop_loss,
            take_profit=order_request.take_profit,
            comment=order_request.comment,
        )

    def estimate_margin(self, *args, **kwargs):
        self.margin_calls.append((args, kwargs))
        return 10.0

    def get_tradeability(self, symbol):
        return {"trade_enabled": True, "has_tick": True}


class _InfiniteMarginExecutionEngine(_ExecutionEngine):
    def estimate_margin(self, *args, **kwargs):
        return float("inf")


class _NoMarginExecutionEngine(_ExecutionEngine):
    def estimate_margin(self, *args, **kwargs):
        self.margin_calls.append((args, kwargs))
        return None


class _RaisingMarginExecutionEngine(_ExecutionEngine):
    def estimate_margin(self, *args, **kwargs):
        self.margin_calls.append((args, kwargs))
        raise RuntimeError("broker margin unavailable")


class _RaisingTradeabilityExecutionEngine(_ExecutionEngine):
    def get_tradeability(self, symbol):
        raise RuntimeError("broker tradeability unavailable")


class _LosingBuyPositionExecutionEngine(_ExecutionEngine):
    def get_positions(self, symbol=None):
        if symbol and symbol != "US500":
            return []
        return [
            SimpleNamespace(
                symbol="US500",
                type="BUY",
                profit=-25.0,
            )
        ]


class _MalformedProfitPositionExecutionEngine(_ExecutionEngine):
    def get_positions(self, symbol=None):
        if symbol and symbol != "US500":
            return []
        return [
            SimpleNamespace(
                symbol=" US500 ",
                type=" buy ",
                profit="bad-profit",
            )
        ]


class _RiskEngine:
    @property
    def settings(self):
        return RiskSettings(min_reward_risk_ratio=1.8)


class _RiskService:
    def __init__(self):
        self.risk_engine = _RiskEngine()

    def preview_portfolio_fit(self, context):
        return 0.8

    def assess(self, **kwargs):
        from domain.models import AntiChurnAssessment, PortfolioRiskAssessment, RiskApprovalDecision, RiskEvaluation

        evaluation = RiskEvaluation(approved=True, reason="ok", adjusted_volume=0.01)
        return RiskApprovalDecision(
            approved=True,
            reasons=[],
            risk_evaluation=evaluation,
            portfolio_risk_assessment=PortfolioRiskAssessment(allow_execute=True, reason="ok"),
            anti_churn_assessment=AntiChurnAssessment(),
        )


class _TradeQualityService:
    def assess(self, **kwargs):
        from domain.models import TradeQualityAssessment

        return TradeQualityAssessment(final_trade_quality_score=0.9, threshold=0.7, no_trade_zone=False)


class _AntiChurnService:
    def spread_deteriorated(self, *args, **kwargs):
        return False


class _SignalPipeline:
    def __init__(self):
        self.trade_quality_service = _TradeQualityService()
        self.anti_churn_service = _AntiChurnService()
        self.risk_engine = SimpleNamespace(settings=RiskSettings(cooldown_minutes_per_symbol=120))


class ExecutionServiceTests(unittest.TestCase):
    def test_place_order_uses_preflight_reward_risk_requirement(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss=99.0,
            take_profit=101.6,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=1.6,
            required_reward_risk=1.6,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertTrue(result.success)
        self.assertEqual(len(execution_engine.placed), 1)

    def test_place_order_normalizes_action_before_dispatch(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest.model_construct(
            symbol="US500",
            action=" buy ",
            volume=0.01,
            stop_loss=99.0,
            take_profit=103.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=3.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertTrue(result.success)
        self.assertEqual(execution_engine.placed[0].action, "BUY")

    def test_place_order_duplicate_guard_normalizes_action_before_losing_position_check(self):
        execution_engine = _LosingBuyPositionExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest.model_construct(
            symbol="US500",
            action=" buy ",
            volume=0.01,
            stop_loss=99.0,
            take_profit=103.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=3.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("same-symbol same-direction", result.retcode_desc)

    def test_place_order_duplicate_guard_blocks_malformed_same_side_position_profit(self):
        execution_engine = _MalformedProfitPositionExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss=99.0,
            take_profit=103.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=3.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("same-symbol same-direction", result.retcode_desc)

    def test_place_order_blocks_when_spread_cost_breaks_reward_risk(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="EURUSD",
            action="BUY",
            volume=0.01,
            stop_loss=1.0992,
            take_profit=1.1020,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=1.1002,
            reward_risk=1.8,
            required_reward_risk=1.8,
            spread_cost=0.0002,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("reward:risk", result.retcode_desc)

    def test_place_order_blocks_non_finite_protective_targets(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss=99.0,
            take_profit=float("inf"),
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=999.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("stop-loss and take-profit", result.retcode_desc)

    def test_place_order_blocks_malformed_protective_targets(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest.model_construct(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss="bad-stop",
            take_profit=101.6,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=1.6,
            required_reward_risk=1.6,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("stop-loss and take-profit", result.retcode_desc)

    def test_place_order_blocks_non_finite_volume(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=float("inf"),
            stop_loss=99.0,
            take_profit=101.6,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=1.6,
            required_reward_risk=1.6,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("volume", result.retcode_desc.lower())

    def test_place_order_blocks_unknown_action(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest.model_construct(
            symbol="US500",
            action="WAIT",
            volume=0.01,
            stop_loss=99.0,
            take_profit=103.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=3.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("action", result.retcode_desc.lower())

    def test_place_order_blocks_directionally_invalid_buy_targets(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="BUY",
            volume=0.01,
            stop_loss=101.0,
            take_profit=105.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=5.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("Invalid BUY targets", result.retcode_desc)

    def test_place_order_blocks_directionally_invalid_sell_targets(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=None,
        )
        order = OrderRequest(
            symbol="US500",
            action="SELL",
            volume=0.01,
            stop_loss=99.0,
            take_profit=95.0,
            comment="test",
        )
        preflight = ExecutionPreflightAssessment(
            approved=True,
            reason="Execution conditions still valid.",
            entry_price=100.0,
            reward_risk=5.0,
            required_reward_risk=1.8,
        )

        result = service.place_order_if_approved(order, preflight)

        self.assertFalse(result.success)
        self.assertEqual(len(execution_engine.placed), 0)
        self.assertIn("Invalid SELL targets", result.retcode_desc)

    def test_reward_risk_ratio_sanitizes_malformed_targets(self):
        service = ExecutionService(
            execution_engine=_ExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=None,
        )

        ratio = service._reward_risk_ratio(
            1.1002,
            "bad-stop",
            1.1080,
            spread_cost=0.0002,
        )

        self.assertEqual(ratio, 0.0)

    def test_preflight_blocks_non_finite_live_entry_price(self):
        service = ExecutionService(
            execution_engine=_ExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": float("inf"), "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0990,
            take_profit=1.1030,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("live price", result.reason)

    def test_preflight_blocks_non_finite_margin_estimate(self):
        service = ExecutionService(
            execution_engine=_InfiniteMarginExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0990,
            take_profit=1.1030,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("margin", result.reason.lower())

    def test_preflight_blocks_malformed_fallback_margin_contract_size(self):
        service = ExecutionService(
            execution_engine=_NoMarginExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext.model_construct(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo.model_construct(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size="bad-contract",
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("margin", result.reason.lower())

    def test_preflight_blocks_when_margin_estimator_raises(self):
        service = ExecutionService(
            execution_engine=_RaisingMarginExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("margin", result.reason.lower())

    def test_preflight_blocks_when_tradeability_lookup_raises(self):
        service = ExecutionService(
            execution_engine=_RaisingTradeabilityExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("tradeable", result.reason.lower())

    def test_preflight_blocks_malformed_live_spread(self):
        service = ExecutionService(
            execution_engine=_ExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": "bad-spread"},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0990,
            take_profit=1.1030,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("live price", result.reason)

    def test_preflight_sanitizes_malformed_min_reward_risk_policy(self):
        service = ExecutionService(
            execution_engine=_ExecutionEngine(),
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": "bad-rr"},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0992,
            take_profit=1.1020,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("Reward:risk", result.reason)
        self.assertEqual(result.required_reward_risk, 1.8)

    def test_preflight_blocks_malformed_protective_targets(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal.model_construct(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss="bad-stop",
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("stop-loss and take-profit", result.reason)
        self.assertEqual(len(execution_engine.margin_calls), 0)

    def test_preflight_blocks_malformed_volume_before_margin_check(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume="bad-volume",
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("volume", result.reason.lower())
        self.assertEqual(len(execution_engine.margin_calls), 0)

    def test_preflight_blocks_non_finite_volume_before_margin_check(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action="BUY",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=float("inf"),
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("volume", result.reason.lower())
        self.assertEqual(len(execution_engine.margin_calls), 0)

    def test_preflight_blocks_unknown_action_before_margin_check(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal.model_construct(
            agent_name="SmartAgent",
            action="WAIT",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertFalse(result.approved)
        self.assertIn("Invalid execution action", result.reason)
        self.assertEqual(len(execution_engine.margin_calls), 0)

    def test_preflight_normalizes_action_before_live_entry_and_margin_check(self):
        execution_engine = _ExecutionEngine()
        service = ExecutionService(
            execution_engine=execution_engine,
            risk_service=_RiskService(),
            signal_pipeline=_SignalPipeline(),
        )
        context = MarketContext(
            symbol="EURUSD",
            evaluation_mode="auto",
            user_policy={"min_reward_risk": 1.8},
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2.0},
            symbol_info=NormalizedSymbolInfo(
                name="EURUSD",
                category="Forex",
                point=0.0001,
                trade_contract_size=100000,
            ),
            profile=SymbolProfile(
                profile_name="Forex",
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
            account_equity=10000,
            account_leverage=100,
        )
        signal = TechnicalSignal(
            agent_name="SmartAgent",
            action=" buy ",
            confidence=0.9,
            stop_loss=1.0970,
            take_profit=1.1080,
        )

        import asyncio

        result = asyncio.run(
            service.preflight_for_context_signal(
                context=context,
                candidate_signal=signal,
                volume=0.01,
                reference_spread=2.0,
                evaluation_mode="auto",
            )
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.entry_price, 1.1002)
        self.assertEqual(execution_engine.margin_calls[0][0][1], "BUY")


if __name__ == "__main__":
    unittest.main()
