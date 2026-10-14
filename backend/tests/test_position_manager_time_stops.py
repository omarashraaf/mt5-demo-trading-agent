import asyncio
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5.execution import PositionInfo
from position_manager import PositionManager


class _Connector:
    connected = True

    def refresh_account(self):
        return SimpleNamespace(equity=10000.0)


class _MarketData:
    def get_symbol_info(self, _symbol):
        return {"point": 0.0001, "trade_contract_size": 100000, "digits": 5}

    def get_tick(self, _symbol):
        return SimpleNamespace(bid=1.1002, ask=1.1004, spread=2, time=int(time.time()))

    def get_bars(self, _symbol, _timeframe, _count):
        return [
            SimpleNamespace(
                time=int(time.time()) - (idx * 60),
                open=1.1000,
                high=1.1010,
                low=1.0990,
                close=1.1002,
                volume=100,
            )
            for idx in range(30)
        ]


class _Execution:
    def __init__(self):
        self.modified = []
        self.closed = []

    def get_positions(self):
        return []

    def modify_position(self, ticket, stop_loss, take_profit):
        self.modified.append(
            {
                "ticket": ticket,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return SimpleNamespace(success=True, retcode_desc="ok")

    def close_position(self, ticket):
        self.closed.append(ticket)
        return SimpleNamespace(success=True, retcode_desc="ok")


class _RiskEngine:
    panic_stopped = False
    auto_trade_enabled = True
    user_policy = SimpleNamespace(max_risk_per_trade=0.5, min_reward_risk=1.8, mode="balanced")


class _ClosedPlanNoOutcomeDb:
    def __init__(self):
        self.logged = []
        self.marked = []

    async def get_trade_outcome_by_ticket(self, _ticket):
        return None

    async def get_position_management_plan(self, _ticket):
        return {
            "status": "closed",
            "signal_id": None,
            "plan_json": {"metadata": {"symbol_category": "Commodities"}},
        }

    async def get_signal_by_id(self, _signal_id):
        return None

    async def log_trade_outcome(self, **kwargs):
        self.logged.append(kwargs)

    async def mark_evaluation_outcome(self, signal_id, status):
        self.marked.append((signal_id, status))


class PositionManagerTimeStopTests(unittest.TestCase):
    def setUp(self):
        self.execution = _Execution()
        self.manager = PositionManager(
            connector=_Connector(),
            market_data=_MarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )

    def test_time_exit_reason_closes_stale_trade_without_progress(self):
        pos = PositionInfo(
            ticket=1,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1001,
            stop_loss=1.0950,
            take_profit=1.1100,
            profit=0.0,
            time=int(time.time() - 70 * 60),
            comment="TradingAgent",
        )

        decision = self.manager._time_exit_reason(
            pos=pos,
            plan={
                "planned_hold_minutes": 90,
                "stale_after_minutes": 45,
                "min_progress_r_multiple": 0.25,
                "target_progress_r_multiple": 0.40,
            },
            plan_meta={},
            profit_in_price=0.0002,
            original_risk=0.0050,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision[0], "time_stop")
        self.assertIn("stale trade", decision[1])

    def test_time_exit_reason_keeps_winning_trade_with_some_progress_open(self):
        pos = PositionInfo(
            ticket=101,
            symbol="XAUUSD",
            type="BUY",
            volume=1.0,
            price_open=4520.0,
            price_current=4524.0,
            stop_loss=4490.0,
            take_profit=4586.0,
            profit=1443.52,
            time=int(time.time() - 20 * 60),
            comment="TradingAgent",
        )

        decision = self.manager._time_exit_reason(
            pos=pos,
            plan={
                "planned_hold_minutes": 20,
                "stale_after_minutes": 10,
                "min_progress_r_multiple": 0.25,
                "target_progress_r_multiple": 0.40,
            },
            plan_meta={},
            profit_in_price=4.0,
            original_risk=30.0,
        )

        self.assertIsNone(decision)

    def test_planned_hold_expiry_does_not_close_trade_that_reached_target_progress(self):
        pos = PositionInfo(
            ticket=102,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1045,
            stop_loss=1.0950,
            take_profit=1.1100,
            profit=12.0,
            time=int(time.time() - 95 * 60),
            comment="TradingAgent",
        )

        decision = self.manager._time_exit_reason(
            pos=pos,
            plan={
                "planned_hold_minutes": 90,
                "stale_after_minutes": 45,
                "min_progress_r_multiple": 0.25,
                "target_progress_r_multiple": 0.40,
            },
            plan_meta={},
            profit_in_price=0.0045,
            original_risk=0.0050,
        )

        self.assertIsNone(decision)

    def test_trend_reversal_does_not_close_profitable_trade(self):
        class BearishTrendMarketData(_MarketData):
            def get_bars(self, _symbol, timeframe, _count):
                if timeframe == "H4":
                    return [
                        SimpleNamespace(
                            time=int(time.time()) - (idx * 60),
                            open=1.1000,
                            high=1.1010,
                            low=1.0990,
                            close=1.1300 - (idx * 0.0005),
                            volume=100,
                        )
                        for idx in range(60, 0, -1)
                    ]
                return super().get_bars(_symbol, timeframe, _count)

        manager = PositionManager(
            connector=_Connector(),
            market_data=BearishTrendMarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )
        pos = PositionInfo(
            ticket=103,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1040,
            stop_loss=0.0,
            take_profit=0.0,
            profit=12.0,
            time=int(time.time() - 20 * 60),
            comment="TradingAgent",
        )
        manager._close_with_outcome = AsyncMock()

        asyncio.run(manager._manage_single(pos))

        manager._close_with_outcome.assert_not_awaited()

    def test_session_close_triggers_inside_buffer(self):
        with patch("position_manager.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 3, 26, 16, 50, tzinfo=timezone.utc)
            should_close = self.manager._should_end_session_close(
                {"close_before_session_end": True, "session_close_buffer_minutes": 15},
                {"close_before_session_end": True, "sessions": ["US Open"]},
            )

        self.assertTrue(should_close)

    def test_passive_exit_classifies_breakeven(self):
        reason = self.manager._classify_passive_exit(
            {
                "price_open": 1.1000,
                "stop_loss": 1.10005,
                "take_profit": 1.1100,
                "last_price": 1.10004,
                "point": 0.0001,
                "profit": 0.01,
                "action": "BUY",
            }
        )

        self.assertEqual(reason, "breakeven")

    def test_passive_exit_does_not_label_loss_as_take_profit(self):
        reason = self.manager._classify_passive_exit(
            {
                "price_open": 4.66,
                "stop_loss": 4.44,
                "take_profit": 4.93,
                "last_price": 4.66,
                "point": 0.01,
                "profit": -162.08,
                "action": "BUY",
            }
        )

        self.assertEqual(reason, "external_loss_close")

    def test_passive_exit_does_not_label_negative_breakeven_stop(self):
        reason = self.manager._classify_passive_exit(
            {
                "price_open": 74.865,
                "stop_loss": 74.877,
                "take_profit": 74.566,
                "last_price": 74.877,
                "point": 0.001,
                "profit": -82.2,
                "action": "SELL",
            }
        )

        self.assertEqual(reason, "stop_loss")

    def test_breakeven_management_does_not_move_stop_while_trade_is_losing(self):
        class FavorableSellMarketData(_MarketData):
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1.0968, ask=1.0970, spread=2, time=int(time.time()))

        manager = PositionManager(
            connector=_Connector(),
            market_data=FavorableSellMarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )
        pos = PositionInfo(
            ticket=15,
            symbol="EURUSD",
            type="SELL",
            volume=1.0,
            price_open=1.1000,
            price_current=1.0970,
            stop_loss=1.1010,
            take_profit=1.0980,
            profit=-5.0,
            time=int(time.time() - 5 * 60),
            comment="TA1000_SLA100_TPA200",
        )

        asyncio.run(manager._manage_single(pos))

        self.assertEqual(self.execution.modified, [])

    def test_breakeven_management_ignores_undated_mt5_tick(self):
        class UndatedFavorableMarketData(_MarketData):
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1.1050, ask=1.1052, spread=2)

        manager = PositionManager(
            connector=_Connector(),
            market_data=UndatedFavorableMarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )
        pos = PositionInfo(
            ticket=18,
            symbol="EURUSD",
            type="BUY",
            volume=1.0,
            price_open=1.1000,
            price_current=1.1050,
            stop_loss=1.0980,
            take_profit=1.1100,
            profit=80.0,
            time=int(time.time() - 5 * 60),
            comment="TradingAgent",
        )

        asyncio.run(manager._manage_single(pos))

        self.assertEqual(self.execution.modified, [])

    def test_comment_risk_amount_parser_accepts_mt5_sanitized_separators(self):
        self.assertEqual(
            self.manager._extract_comment_named_amount("TA4525_SLA543_TPA1176", "SLA"),
            543.0,
        )
        self.assertEqual(
            self.manager._extract_comment_named_amount("TA4525-SLA543 TPA1176", "TPA"),
            1176.0,
        )

    def test_truncated_mt5_comment_keeps_explicit_sla_and_broker_tp_amount(self):
        class MetalMarketData(_MarketData):
            def get_symbol_info(self, _symbol):
                return {"point": 0.001, "trade_contract_size": 5000, "digits": 3}

        manager = PositionManager(
            connector=_Connector(),
            market_data=MetalMarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )
        pos = PositionInfo(
            ticket=16,
            symbol="XAGUSD",
            type="SELL",
            volume=1.37,
            price_open=74.865,
            price_current=74.877,
            stop_loss=74.988,
            take_profit=74.566,
            profit=-82.2,
            time=int(time.time()),
            comment="TA5128_SLA843_TP",
        )
        manager.risk_engine.user_policy.mode = "aggressive"

        amounts = manager._resolve_comment_risk_amounts(pos)

        self.assertEqual(amounts["sla"], 843.0)
        self.assertAlmostEqual(amounts["tpa"], 2048.15, places=2)

    def test_legacy_cfd_comment_amounts_are_capped_to_started_risk(self):
        class MetalMarketData(_MarketData):
            def get_symbol_info(self, _symbol):
                return {"point": 0.001, "trade_contract_size": 5000, "digits": 3}

        manager = PositionManager(
            connector=_Connector(),
            market_data=MetalMarketData(),
            execution=self.execution,
            risk_engine=_RiskEngine(),
            agents={},
        )
        manager.risk_engine.user_policy.mode = "aggressive"
        pos = PositionInfo(
            ticket=17,
            symbol="XAGUSD",
            type="SELL",
            volume=0.12,
            price_open=75.476,
            price_current=75.518,
            stop_loss=76.82,
            take_profit=72.557,
            profit=-13.8,
            time=int(time.time()),
            comment="TA453|SLA820|TPA1738",
        )

        amounts = manager._resolve_comment_risk_amounts(pos)

        self.assertAlmostEqual(amounts["sla"], 453.0 * 0.18, places=2)
        self.assertAlmostEqual(amounts["tpa"], 453.0 * 0.40, places=2)

    def test_valid_broker_targets_skip_time_exit_close(self):
        pos = PositionInfo(
            ticket=11,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1002,
            stop_loss=1.0950,
            take_profit=1.1100,
            profit=-1.0,
            time=int(time.time() - 90 * 60),
            comment="TradingAgent",
        )
        self.manager._time_exit_reason = Mock(
            side_effect=AssertionError("time exit should be skipped")
        )
        self.manager._close_with_outcome = AsyncMock()

        asyncio.run(self.manager._manage_single(pos))

        self.manager._close_with_outcome.assert_not_awaited()

    def test_valid_broker_targets_skip_hard_risk_budget_stop_when_sl_is_inside_budget(self):
        pos = PositionInfo(
            ticket=12,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.0960,
            stop_loss=1.0950,
            take_profit=1.1100,
            profit=-80.0,
            time=int(time.time() - 20 * 60),
            comment="TradingAgent",
        )
        self.manager._close_with_outcome = AsyncMock()

        asyncio.run(self.manager._manage_single(pos))

        self.manager._close_with_outcome.assert_not_awaited()

    def test_auto_portfolio_targets_do_not_close_positions_with_valid_individual_targets(self):
        pos = PositionInfo(
            ticket=13,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1200,
            stop_loss=1.0950,
            take_profit=1.1300,
            profit=250.0,
            time=int(time.time() - 30 * 60),
            comment="TradingAgent",
        )
        self.manager.set_portfolio_targets(
            stop_loss_usd=50.0,
            take_profit_usd=100.0,
            mode="auto",
            manual_override=False,
        )
        self.manager._portfolio_targets_updated_at = time.time() - 60

        asyncio.run(self.manager._check_portfolio_targets([pos]))

        self.assertEqual(self.execution.closed, [])

    def test_auto_portfolio_targets_only_close_unprotected_positions(self):
        protected = PositionInfo(
            ticket=21,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1200,
            stop_loss=1.0950,
            take_profit=1.1300,
            profit=250.0,
            time=int(time.time() - 30 * 60),
            comment="TradingAgent",
        )
        unprotected = PositionInfo(
            ticket=22,
            symbol="EURUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1200,
            stop_loss=0.0,
            take_profit=0.0,
            profit=250.0,
            time=int(time.time() - 30 * 60),
            comment="TradingAgent",
        )
        self.manager.set_portfolio_targets(
            stop_loss_usd=50.0,
            take_profit_usd=100.0,
            mode="auto",
            manual_override=False,
        )
        self.manager._portfolio_targets_updated_at = time.time() - 60

        asyncio.run(self.manager._check_portfolio_targets([protected, unprotected]))

        self.assertEqual(self.execution.closed, [22])

    def test_reconcile_disappeared_position_logs_outcome_even_when_plan_already_closed(self):
        db = _ClosedPlanNoOutcomeDb()
        self.manager.set_database(db)

        asyncio.run(
            self.manager._reconcile_disappeared_position(
                123,
                {
                    "symbol": "XAGUSD",
                    "action": "SELL",
                    "price_open": 74.786,
                    "stop_loss": 74.973,
                    "take_profit": 74.37,
                    "last_price": 74.973,
                    "point": 0.001,
                    "profit": -1131.35,
                    "opened_at": time.time() - 60,
                    "last_seen": time.time(),
                },
            )
        )

        self.assertEqual(len(db.logged), 1)
        self.assertEqual(db.logged[0]["exit_reason"], "stop_loss")

    def test_tp_ladder_extends_target_by_two_percent(self):
        pos = PositionInfo(
            ticket=7,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=110.0,
            stop_loss=95.0,
            take_profit=110.0,
            profit=10.0,
            time=int(time.time() - 240),
            comment="TradingAgent",
        )
        plan = {"metadata": {"tp_ladder_enabled": True, "tp_ladder_step_pct": 0.02}}
        self.manager._remember_snapshot(pos, plan)

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=109.5,
                )
            )
        )
        self.assertAlmostEqual(self.execution.modified[-1]["take_profit"], 112.2, places=5)
        self.manager._position_snapshots[pos.ticket]["tp_ladder_last_extension_at"] = 0.0

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=112.25,
                )
            )
        )
        snapshot = self.manager._position_snapshots[pos.ticket]
        self.assertAlmostEqual(snapshot["tp_ladder_exit_floor"], 110.0, places=5)
        self.assertAlmostEqual(snapshot["tp_ladder_next_target"], round(112.25 * 1.02, 5), places=5)

    def test_tp_ladder_is_enabled_by_default_despite_legacy_false_flag(self):
        pos = PositionInfo(
            ticket=77,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=108.0,
            stop_loss=95.0,
            take_profit=110.0,
            profit=8.0,
            time=int(time.time() - 240),
            comment="TradingAgent",
        )
        plan = {"metadata": {"tp_ladder_enabled": False}}
        self.manager._remember_snapshot(pos, plan)

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=108.0,
                )
            )
        )
        self.assertAlmostEqual(self.execution.modified[-1]["take_profit"], 112.2, places=5)

    def test_tp_ladder_applies_to_gold_family_symbols(self):
        pos = PositionInfo(
            ticket=78,
            symbol="XAUUSD",
            type="BUY",
            volume=1.0,
            price_open=2300.0,
            price_current=2350.0,
            stop_loss=2280.0,
            take_profit=2360.0,
            profit=50.0,
            time=int(time.time() - 240),
            comment="TradingAgent",
        )
        plan = {"metadata": {"tp_ladder_step_pct": 0.02}}
        self.manager._remember_snapshot(pos, plan)

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=2350.0,
                )
            )
        )
        self.assertAlmostEqual(self.execution.modified[-1]["take_profit"], 2407.2, places=5)

    def test_tp_ladder_locks_floor_with_broker_stop_instead_of_local_close(self):
        pos = PositionInfo(
            ticket=8,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=111.0,
            stop_loss=95.0,
            take_profit=112.2,
            profit=11.0,
            time=int(time.time() - 240),
            comment="TradingAgent",
        )
        plan = {"metadata": {"tp_ladder_enabled": True, "tp_ladder_step_pct": 0.02}}
        self.manager._remember_snapshot(pos, plan)
        snapshot = self.manager._position_snapshots[pos.ticket]
        snapshot["tp_ladder_initialized"] = True
        snapshot["tp_ladder_next_target"] = 112.2
        snapshot["tp_ladder_exit_floor"] = 110.0
        self.manager._close_with_outcome = AsyncMock()

        self.assertFalse(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=109.95,
                )
            )
        )
        self.manager._close_with_outcome.assert_not_awaited()

        pos.price_current = 112.25
        self.assertTrue(
            asyncio.run(
                self.manager._maybe_manage_tp_ladder(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=112.25,
                )
            )
        )
        self.assertAlmostEqual(self.execution.modified[-1]["stop_loss"], 112.2, places=5)
        self.assertGreater(self.execution.modified[-1]["take_profit"], 112.2)

    def test_profit_pullback_closes_buy_before_profit_returns_to_entry(self):
        pos = PositionInfo(
            ticket=91,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=108.0,
            stop_loss=95.0,
            take_profit=120.0,
            profit=80.0,
            time=int(time.time() - 600),
            comment="TradingAgent",
        )
        plan = {"metadata": {"profit_pullback_enabled": True, "profit_pullback_giveback_pct": 0.25}}
        self.manager._remember_snapshot(pos, plan, current_price=114.0)
        snapshot = self.manager._position_snapshots[pos.ticket]
        snapshot["profit_pullback_peak_price"] = 114.0
        snapshot["profit_pullback_peak_profit"] = 140.0
        self.manager._close_with_outcome = AsyncMock()

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_close_profit_pullback(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=108.0,
                    profit_in_price=8.0,
                )
            )
        )
        self.manager._close_with_outcome.assert_awaited_once()
        self.assertEqual(self.manager._close_with_outcome.await_args.args[1], "profit_pullback")

    def test_profit_pullback_closes_sell_before_profit_returns_to_entry(self):
        pos = PositionInfo(
            ticket=92,
            symbol="US500",
            type="SELL",
            volume=1.0,
            price_open=200.0,
            price_current=190.0,
            stop_loss=210.0,
            take_profit=180.0,
            profit=100.0,
            time=int(time.time() - 600),
            comment="TradingAgent",
        )
        plan = {"metadata": {"profit_pullback_enabled": True, "profit_pullback_giveback_pct": 0.25}}
        self.manager._remember_snapshot(pos, plan, current_price=186.0)
        snapshot = self.manager._position_snapshots[pos.ticket]
        snapshot["profit_pullback_peak_price"] = 186.0
        snapshot["profit_pullback_peak_profit"] = 140.0
        self.manager._close_with_outcome = AsyncMock()

        self.assertTrue(
            asyncio.run(
                self.manager._maybe_close_profit_pullback(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=190.0,
                    profit_in_price=10.0,
                )
            )
        )
        self.manager._close_with_outcome.assert_awaited_once()
        self.assertEqual(self.manager._close_with_outcome.await_args.args[1], "profit_pullback")

    def test_profit_pullback_ignores_small_unconfirmed_profit(self):
        pos = PositionInfo(
            ticket=93,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=103.0,
            stop_loss=95.0,
            take_profit=120.0,
            profit=30.0,
            time=int(time.time() - 600),
            comment="TradingAgent",
        )
        plan = {"metadata": {"profit_pullback_enabled": True}}
        self.manager._remember_snapshot(pos, plan, current_price=104.0)
        snapshot = self.manager._position_snapshots[pos.ticket]
        snapshot["profit_pullback_peak_price"] = 104.0
        snapshot["profit_pullback_peak_profit"] = 40.0
        self.manager._close_with_outcome = AsyncMock()

        self.assertFalse(
            asyncio.run(
                self.manager._maybe_close_profit_pullback(
                    pos=pos,
                    plan_record={},
                    plan=plan,
                    plan_meta=plan["metadata"],
                    current_price=103.0,
                    profit_in_price=3.0,
                )
            )
        )
        self.manager._close_with_outcome.assert_not_awaited()

    def test_invalid_target_repair_leaves_protected_ladder_buy_targets_alone(self):
        pos = PositionInfo(
            ticket=88,
            symbol="US500",
            type="BUY",
            volume=1.0,
            price_open=100.0,
            price_current=111.0,
            stop_loss=110.0,
            take_profit=114.0,
            profit=11.0,
            time=int(time.time() - 240),
            comment="TradingAgent",
        )

        self.assertFalse(asyncio.run(self.manager._maybe_fix_invalid_targets(pos)))
        self.assertEqual(self.execution.modified, [])

    def test_breakeven_does_not_move_stop_on_tiny_profit(self):
        pos = PositionInfo(
            ticket=89,
            symbol="XAUUSD",
            type="BUY",
            volume=0.01,
            price_open=1.1000,
            price_current=1.1002,
            stop_loss=1.0990,
            take_profit=1.1100,
            profit=4.20,
            time=int(time.time() - 120),
            comment="TradingAgent",
        )

        asyncio.run(self.manager._manage_single(pos))

        self.assertEqual(self.execution.modified, [])
