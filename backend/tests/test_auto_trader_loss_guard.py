import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_trader as auto_trader_module
from auto_trader import AutoTrader


class _Universe:
    def canonical_symbol(self, symbol):
        return str(symbol or "").upper()


class _Pipeline:
    universe_service = _Universe()
    meta_model_service = None


class _MetaActive:
    is_active = True


class _PipelineWithMeta:
    universe_service = _Universe()
    meta_model_service = _MetaActive()


class _Execution:
    def get_positions(self, symbol=None):
        return []


class _Connector:
    def __init__(self, *, is_demo=True):
        self._is_demo = is_demo
        self.connected = True

    def is_demo(self):
        return self._is_demo

    def refresh_account(self):
        return SimpleNamespace(balance=10000.0, equity=10000.0, margin=0.0, free_margin=10000.0)


class _PositionManager:
    def _position_age_seconds(self, pos):
        return 0.0


class _Db:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.generic_outcomes = outcomes

    async def get_recent_symbol_outcomes(self, symbol, limit=12, within_minutes=None):
        return self.outcomes[:limit]

    async def get_recent_app_trade_outcomes(self, limit=50):
        return self.outcomes[:limit]

    async def get_trade_outcomes(self, limit=50):
        return self.generic_outcomes[:limit]

    async def log_ai_activity(self, *args, **kwargs):
        return None


class _RiskEngine:
    auto_trade_budget_mode = "full_margin"
    auto_trade_budget_usd = None
    settings = SimpleNamespace(policy_mode="balanced")
    user_policy = SimpleNamespace(demo_only_default=True)


class _Account:
    def __init__(self, *, margin=0.0, balance=10000.0, equity=10000.0):
        self.margin = margin
        self.balance = balance
        self.equity = equity


def _auto_trader_with_outcomes(outcomes):
    trader = AutoTrader.__new__(AutoTrader)
    trader.connector = _Connector()
    trader.signal_pipeline = _Pipeline()
    trader.execution = _Execution()
    trader.position_manager = _PositionManager()
    trader.risk_engine = _RiskEngine()
    trader.db = _Db(outcomes)
    trader._symbol_direction_cooldown_until = {}
    trader._symbol_direction_last_opened_at = {}
    trader._last_cadence_entry_ts = {}
    trader._cycles_without_entry = 0
    trader._symbol_confidence_cache = {}
    trader._trade_log = []
    trader._recent_performance_cache = (0.0, {})
    trader._fixed_lot_size = 0.0
    trader._allocation_pct_free_margin = 0.0
    return trader


class AutoTraderLossGuardTests(unittest.TestCase):
    def test_live_policy_allows_demo_accounts_even_when_live_disabled(self):
        trader = _auto_trader_with_outcomes([])
        trader.connector = _Connector(is_demo=True)
        original_live = auto_trader_module.config.LIVE_TRADING_ENABLED
        original_demo_only = trader.risk_engine.user_policy.demo_only_default
        try:
            auto_trader_module.config.LIVE_TRADING_ENABLED = False
            trader.risk_engine.user_policy.demo_only_default = True

            self.assertIsNone(trader._live_trading_policy_block_reason())
        finally:
            auto_trader_module.config.LIVE_TRADING_ENABLED = original_live
            trader.risk_engine.user_policy.demo_only_default = original_demo_only

    def test_live_policy_blocks_live_accounts_when_config_or_policy_disallows(self):
        trader = _auto_trader_with_outcomes([])
        trader.connector = _Connector(is_demo=False)
        original_live = auto_trader_module.config.LIVE_TRADING_ENABLED
        original_demo_only = trader.risk_engine.user_policy.demo_only_default
        try:
            auto_trader_module.config.LIVE_TRADING_ENABLED = False
            trader.risk_engine.user_policy.demo_only_default = True

            reason = trader._live_trading_policy_block_reason()
        finally:
            auto_trader_module.config.LIVE_TRADING_ENABLED = original_live
            trader.risk_engine.user_policy.demo_only_default = original_demo_only

        self.assertIn("LIVE_TRADING_ENABLED=true", reason)
        self.assertIn("demo-only mode off", reason)

    def test_live_policy_allows_live_accounts_when_config_and_policy_allow(self):
        trader = _auto_trader_with_outcomes([])
        trader.connector = _Connector(is_demo=False)
        original_live = auto_trader_module.config.LIVE_TRADING_ENABLED
        original_demo_only = trader.risk_engine.user_policy.demo_only_default
        try:
            auto_trader_module.config.LIVE_TRADING_ENABLED = True
            trader.risk_engine.user_policy.demo_only_default = False

            self.assertIsNone(trader._live_trading_policy_block_reason())
        finally:
            auto_trader_module.config.LIVE_TRADING_ENABLED = original_live
            trader.risk_engine.user_policy.demo_only_default = original_demo_only

    def test_strict_cadence_fallback_does_not_place_live_order_when_policy_blocks(self):
        now = time.time()

        metadata_reads = []

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=99.9, ask=100.0, spread=0.1, time=now, time_msc=int(now * 1000))

            def get_symbol_info(self, _symbol):
                return {
                    "digits": 2,
                    "point": 0.01,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 10.0,
                    "trade_contract_size": 100.0,
                    "trade_tick_value": 1.0,
                    "trade_tick_size": 0.01,
                }

        class Execution:
            def __init__(self):
                self.placed = []

            def estimate_margin(self, **_kwargs):
                return 100.0

            def place_order(self, order_req):
                self.placed.append(order_req)
                raise AssertionError("strict cadence fallback must not place live order when policy blocks")

        trader = _auto_trader_with_outcomes([])
        trader.connector = _Connector(is_demo=False)
        trader.market_data = MarketData()
        trader.execution = Execution()
        original_live = auto_trader_module.config.LIVE_TRADING_ENABLED
        original_demo_only = trader.risk_engine.user_policy.demo_only_default
        try:
            auto_trader_module.config.LIVE_TRADING_ENABLED = False
            trader.risk_engine.user_policy.demo_only_default = True

            executed = asyncio.run(trader._execute_strict_cadence_fallback(
                symbol="US500",
                canonical_symbol="US500",
                action="BUY",
            ))
        finally:
            auto_trader_module.config.LIVE_TRADING_ENABLED = original_live
            trader.risk_engine.user_policy.demo_only_default = original_demo_only

        self.assertFalse(executed)
        self.assertEqual(trader.execution.placed, [])
        self.assertIn("LIVE_TRADING_ENABLED=true", trader.recent_trades[-1]["detail"])

    def test_strict_cadence_fallback_rejects_undated_live_tick_before_sizing(self):
        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=99.9, ask=100.0, spread=0.1, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        class Execution:
            def __init__(self):
                self.placed = []

            def estimate_margin(self, **_kwargs):
                raise AssertionError("margin should not be estimated for stale ticks")

            def place_order(self, order_req):
                self.placed.append(order_req)
                raise AssertionError("strict cadence fallback must not place order with stale tick")

        trader = _auto_trader_with_outcomes([])
        trader.connector = _Connector(is_demo=False)
        trader.market_data = MarketData()
        trader.execution = Execution()
        original_live = auto_trader_module.config.LIVE_TRADING_ENABLED
        original_demo_only = trader.risk_engine.user_policy.demo_only_default
        try:
            auto_trader_module.config.LIVE_TRADING_ENABLED = True
            trader.risk_engine.user_policy.demo_only_default = False

            executed = asyncio.run(trader._execute_strict_cadence_fallback(
                symbol="US500",
                canonical_symbol="US500",
                action="BUY",
            ))
        finally:
            auto_trader_module.config.LIVE_TRADING_ENABLED = original_live
            trader.risk_engine.user_policy.demo_only_default = original_demo_only

        self.assertFalse(executed)
        self.assertEqual(trader.execution.placed, [])
        self.assertIn("fresh live quote", trader.recent_trades[-1]["detail"])

    def test_aggressive_cfd_discovery_rejects_undated_preferred_broker_tick(self):
        class Universe:
            def canonical_symbol(self, symbol):
                return str(symbol or "").upper()

            def preferred_broker_names(self, _symbol):
                return ["US500"]

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=4999.0, ask=5000.0, spread=1.0, time=0, time_msc=0)

            def get_symbol_info(self, symbol):
                return {
                    "name": str(symbol).upper(),
                    "trade_mode": 4,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 10.0,
                    "trade_contract_size": 100.0,
                }

        trader = _auto_trader_with_outcomes([])
        trader.signal_pipeline = SimpleNamespace(universe_service=Universe())
        trader.market_data = MarketData()
        original_focus = auto_trader_module.AGGRESSIVE_CFD_FOCUS_SYMBOLS
        try:
            auto_trader_module.AGGRESSIVE_CFD_FOCUS_SYMBOLS = ["US500"]

            symbols = trader._direct_executable_aggressive_cfd_symbols([], 8)
        finally:
            auto_trader_module.AGGRESSIVE_CFD_FOCUS_SYMBOLS = original_focus

        self.assertEqual(symbols, [])

    def test_scan_precheck_rejects_undated_tick_before_symbol_metadata(self):
        class Universe:
            def canonical_symbol(self, symbol):
                return str(symbol or "").upper()

            def filter_market_symbols(self, rows):
                return list(rows)

            def resolve_requested_symbols(self, symbols, _rows):
                return list(symbols)

            def default_auto_trade_symbols(self, _rows, limit=8):
                return []

            def aggressive_cfd_symbols(self, _rows, limit=8):
                return []

        class Pipeline:
            universe_service = Universe()
            meta_model_service = None
            anti_churn_service = SimpleNamespace(begin_scan_window=lambda _scan_id: None)

            def market_session_guard_for_symbol(self, _symbol, _market_symbols):
                return None

            async def auto_trade_candidate_symbols(self, _rows, limit=8):
                return []

            async def evaluate(self, **_kwargs):
                raise AssertionError("undated tick should not reach model evaluation")

        metadata_reads = []

        class MarketData:
            def get_tradeable_symbols_cached(self, ttl_seconds=3.0):
                return [
                    {
                        "name": "US500",
                        "category": "Indices",
                        "trade_enabled": True,
                        "visible": True,
                        "bid": 4999.0,
                        "ask": 5000.0,
                        "spread": 1.0,
                    }
                ]

            def get_tick(self, _symbol):
                return SimpleNamespace(bid=4999.0, ask=5000.0, spread=1.0, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                metadata_reads.append(_symbol)
                return {
                    "name": str(_symbol).upper(),
                    "trade_mode": 4,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 10.0,
                    "trade_contract_size": 100.0,
                }

        trader = _auto_trader_with_outcomes([])
        trader.signal_pipeline = Pipeline()
        trader.market_data = MarketData()
        trader.risk_engine.settings = SimpleNamespace(
            allowed_symbols=["US500"],
            policy_mode="balanced",
            max_open_positions_total=5,
            max_concurrent_positions=5,
        )
        trader._execution_allowed = lambda: True
        trader._hydrate_open_position_locks = lambda _positions=None: None

        asyncio.run(trader._scan_and_trade())

        self.assertEqual(metadata_reads, [])
        self.assertTrue(any("fresh live quote" in row["detail"] for row in trader.recent_trades))
        self.assertTrue(any(row["symbol"] == "SYSTEM" for row in trader.recent_trades))

    def test_money_target_cap_keeps_cfd_sl_inside_started_and_account_risk(self):
        trader = _auto_trader_with_outcomes([])
        trader.risk_engine.user_policy = SimpleNamespace(
            mode="aggressive",
            max_risk_per_trade=0.75,
            min_reward_risk=1.8,
        )
        account = _Account(equity=110000.0)
        symbol_info = {
            "name": "XAGUSD",
            "description": "Silver vs US Dollar",
            "path": "Metals\\XAGUSD",
            "trade_contract_size": 5000.0,
            "trade_tick_value": 0.5,
            "trade_tick_size": 0.001,
            "point": 0.001,
        }

        sl, tp, sl_amount, tp_amount = trader._cap_targets_by_money_budget(
            action="SELL",
            entry_price=75.505,
            stop_loss=76.596,
            take_profit=73.326,
            symbol_info=symbol_info,
            volume=1.23,
            started_amount_usd=4644.0,
            account=account,
            digits=3,
        )

        self.assertGreater(sl, 75.505)
        self.assertLess(tp, 75.505)
        self.assertLessEqual(sl_amount, min(4644.0 * 0.18, 110000.0 * 0.0075) + 1.0)
        self.assertGreaterEqual(tp_amount, sl_amount * 1.79)

    def test_custom_budget_cap_ignores_non_finite_budget(self):
        trader = _auto_trader_with_outcomes([])
        trader.risk_engine.auto_trade_budget_mode = "custom_budget"
        trader.risk_engine.auto_trade_budget_usd = float("inf")

        self.assertIsNone(trader._auto_trade_budget_cap_usd())

    def test_remaining_custom_budget_ignores_non_finite_account_margin(self):
        trader = _auto_trader_with_outcomes([])

        remaining = trader._remaining_custom_budget_usd(
            account=_Account(margin=float("inf")),
            budget_cap_usd=500.0,
        )

        self.assertEqual(remaining, 500.0)

    def test_budget_ratio_uses_equity_when_balance_is_non_finite(self):
        trader = _auto_trader_with_outcomes([])
        trader.risk_engine.auto_trade_budget_mode = "custom_budget"
        trader.risk_engine.auto_trade_budget_usd = 2500.0

        ratio = trader._auto_trade_budget_ratio(_Account(balance=float("inf"), equity=10000.0))

        self.assertEqual(ratio, 0.25)

    def test_confidence_cache_ignores_non_finite_values(self):
        trader = _auto_trader_with_outcomes([])

        trader._cache_symbol_confidence("XAUUSD", float("nan"))

        self.assertEqual(trader._cached_symbol_confidence("XAUUSD"), 0.0)

    def test_confidence_cache_discards_corrupt_stored_values(self):
        trader = _auto_trader_with_outcomes([])
        trader._symbol_confidence_cache["XAUUSD"] = (time.time(), float("inf"))

        self.assertEqual(trader._cached_symbol_confidence("XAUUSD"), 0.0)
        self.assertNotIn("XAUUSD", trader._symbol_confidence_cache)

    def test_daily_realized_pnl_uses_timestamp_when_closed_at_is_malformed(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([])

        realized = trader._runtime_realized_pnl_since(
            [
                {
                    "symbol": "XAUUSD",
                    "action": "BUY",
                    "profit": "bad-feed-value",
                    "net_pnl": -75.0,
                    "closed_at": "bad-timestamp",
                    "timestamp": now - 600,
                    "signal_id": 42,
                },
                {
                    "symbol": "XAUUSD",
                    "action": "SELL",
                    "profit_usd": 25.0,
                    "closed_at": now - (26 * 60 * 60),
                    "signal_id": 43,
                },
                {
                    "symbol": "XAUUSD",
                    "action": "SELL",
                    "profit_usd": -30.0,
                    "timestamp": now - 300,
                },
            ],
            since_ts=now - (24 * 60 * 60),
        )

        self.assertEqual(realized, -75.0)

    def test_stop_loss_cooldown_reads_net_pnl(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([
            {
                "symbol": "XAUUSD",
                "action": "BUY",
                "net_pnl": -85.0,
                "exit_reason": "stop_loss",
                "closed_at": now - 600,
                "signal_id": 42,
            }
        ])

        result = asyncio.run(trader._symbol_loss_guard(symbol="XAUUSD", action="BUY", open_positions=[]))

        self.assertIsNotNone(result)
        self.assertIn("recent XAUUSD BUY closed by stop_loss", result)
        self.assertIn("85.00", result)

    def test_stop_loss_cooldown_ignores_malformed_primary_pnl_field(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([
            {
                "symbol": "XAUUSD",
                "action": "BUY",
                "profit": "bad-feed-value",
                "net_pnl": -85.0,
                "exit_reason": "stop_loss",
                "closed_at": now - 600,
                "signal_id": 42,
            }
        ])

        result = asyncio.run(trader._symbol_loss_guard(symbol="XAUUSD", action="BUY", open_positions=[]))

        self.assertIsNotNone(result)
        self.assertIn("recent XAUUSD BUY closed by stop_loss", result)
        self.assertIn("85.00", result)
        self.assertNotIn("bad-feed-value", result)

    def test_stop_loss_cooldown_uses_timestamp_when_closed_at_is_malformed(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([
            {
                "symbol": "XAUUSD",
                "action": "BUY",
                "net_pnl": -85.0,
                "exit_reason": "stop_loss",
                "closed_at": "bad-timestamp",
                "timestamp": now - 600,
                "signal_id": 42,
            }
        ])

        result = asyncio.run(trader._symbol_loss_guard(symbol="XAUUSD", action="BUY", open_positions=[]))

        self.assertIsNotNone(result)
        self.assertIn("recent XAUUSD BUY closed by stop_loss", result)
        self.assertIn("85.00", result)

    def test_rolling_same_direction_losses_read_alternate_pnl_fields(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([
            {
                "symbol": "XAUUSD",
                "action": "SELL",
                "profit_usd": -35.0,
                "exit_reason": "take_profit",
                "closed_at": now - 600,
                "signal_id": 43,
            },
            {
                "symbol": "XAUUSD",
                "action": "SELL",
                "pnl": -20.0,
                "exit_reason": "trailing_stop",
                "closed_at": now - 1200,
                "signal_id": 44,
            },
        ])

        result = asyncio.run(trader._symbol_loss_guard(symbol="XAUUSD", action="SELL", open_positions=[]))

        self.assertIsNotNone(result)
        self.assertIn("last 2 closed SELL trades", result)
        self.assertIn("55.00", result)

    def test_rolling_same_direction_losses_ignore_stale_pair(self):
        now = time.time()
        trader = _auto_trader_with_outcomes([
            {
                "symbol": "XAGUSD",
                "action": "SELL",
                "profit": -73.8,
                "exit_reason": "stop_loss_amount",
                "closed_at": now - (20 * 60 * 60),
                "signal_id": 43,
            },
            {
                "symbol": "XAGUSD",
                "action": "SELL",
                "profit": -13.8,
                "exit_reason": "breakeven",
                "closed_at": now - (21 * 60 * 60),
                "signal_id": 44,
            },
        ])

        result = asyncio.run(trader._symbol_loss_guard(symbol="XAGUSD", action="SELL", open_positions=[]))

        self.assertIsNone(result)

    def test_recent_performance_gate_requires_stronger_signal_without_meta_model(self):
        outcomes = [{"symbol": "XAUUSD", "profit": 10.0} for _ in range(30)]
        trader = _auto_trader_with_outcomes(outcomes)

        allowed, reason = asyncio.run(
            trader._recent_performance_gate(confidence=0.69, quality_score=0.83)
        )

        self.assertFalse(allowed)
        self.assertIn("meta-model is not active", reason)

    def test_recent_performance_gate_uses_app_owned_outcomes_before_generic_history(self):
        weak_app_outcomes = [{"symbol": "XAUUSD", "profit": -10.0} for _ in range(30)]
        strong_generic_outcomes = [{"symbol": "XAUUSD", "profit": 10.0} for _ in range(30)]
        trader = _auto_trader_with_outcomes(weak_app_outcomes)
        trader.signal_pipeline = _PipelineWithMeta()
        trader.db.generic_outcomes = strong_generic_outcomes

        allowed, reason = asyncio.run(
            trader._recent_performance_gate(confidence=0.69, quality_score=0.83)
        )

        self.assertFalse(allowed)
        self.assertIn("recent closed-trade win rate is 0%", reason)

    def test_empirical_execution_quality_gate_blocks_wide_spread_reduced_margin(self):
        trader = _auto_trader_with_outcomes([])
        decision = SimpleNamespace(
            signal_decision=SimpleNamespace(
                final_signal=SimpleNamespace(confidence=0.72),
                market_context=SimpleNamespace(tick={"spread": 31.0}),
            ),
            trade_quality_assessment=SimpleNamespace(
                final_trade_quality_score=0.82,
                trend_alignment_score=0.95,
            ),
            portfolio_risk_assessment=SimpleNamespace(
                projected_free_margin_pct=99.0,
                projected_margin_utilization_pct=1.0,
            ),
            position_management_plan=SimpleNamespace(atr_regime="high"),
        )

        reason = trader._empirical_execution_quality_gate(decision)

        self.assertIn("wide-spread setups", reason)

    def test_empirical_execution_quality_gate_blocks_medium_volatility_weak_trend(self):
        trader = _auto_trader_with_outcomes([])
        decision = SimpleNamespace(
            signal_decision=SimpleNamespace(
                final_signal=SimpleNamespace(confidence=0.72),
                market_context=SimpleNamespace(tick={"spread": 10.0}),
            ),
            trade_quality_assessment=SimpleNamespace(
                final_trade_quality_score=0.82,
                trend_alignment_score=0.80,
            ),
            portfolio_risk_assessment=SimpleNamespace(
                projected_free_margin_pct=99.95,
                projected_margin_utilization_pct=0.1,
            ),
            position_management_plan=SimpleNamespace(atr_regime="medium"),
        )

        reason = trader._empirical_execution_quality_gate(decision)

        self.assertIn("medium-volatility setups", reason)


if __name__ == "__main__":
    unittest.main()
