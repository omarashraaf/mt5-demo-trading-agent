import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.routes as routes
from fastapi import HTTPException
from pydantic import ValidationError
from api.routes import (
    _apply_final_managed_targets_to_trade,
    _apply_order_comment_target_amounts_to_trade,
    _build_trade_comment,
    _cap_order_targets_by_money_budget,
    _is_fresh_tick,
    _sanitize_app_opened_legacy_target_metrics,
    _sanitize_live_execution_trace_target_warnings,
    _sanitize_unmanaged_broker_target_metrics,
    risk_engine,
)
from mt5.execution import PositionInfo, OrderResult


class RouteTargetCapTests(unittest.TestCase):
    def test_connect_request_accepts_supported_local_login_platforms(self):
        self.assertEqual(routes.ConnectRequest(platform="mt5").platform, "mt5")
        self.assertEqual(routes.ConnectRequest(platform="ibkr").platform, "ibkr")
        self.assertEqual(routes.ConnectRequest(platform="tradestation").platform, "tradestation")

    def test_connect_request_rejects_app_collected_mt5_credentials(self):
        with self.assertRaises(ValidationError):
            routes.ConnectRequest(
                platform="mt5",
                account=123456,
                password="secret",
                server="Broker-Live",
            )

    def test_connect_request_rejects_ibkr_and_tradestation_credentials(self):
        for platform in ("ibkr", "tradestation"):
            with self.assertRaises(ValidationError):
                routes.ConnectRequest(
                    platform=platform,
                    username="user",
                    password="secret",
                    access_token="token",
                )

    def test_legacy_credential_storage_routes_are_disabled(self):
        with self.assertRaises(HTTPException) as save_cm:
            asyncio.run(routes.save_credentials(routes.SaveCredentialsRequest(
                account=123456,
                server="Broker-Live",
                password="secret",
            )))
        with self.assertRaises(HTTPException) as list_cm:
            asyncio.run(routes.get_credentials())
        with self.assertRaises(HTTPException) as delete_cm:
            asyncio.run(routes.delete_credentials(123456))

        for cm in (save_cm, list_cm, delete_cm):
            self.assertEqual(cm.exception.status_code, 410)
            self.assertIn("Broker credential storage is disabled", str(cm.exception.detail))

    def test_ibkr_debug_endpoint_is_unavailable_in_mt5_only_runtime(self):
        old_active_platform = routes.active_platform
        old_ibkr = routes.ibkr_connector
        try:
            routes.active_platform = "ibkr"
            routes.ibkr_connector = SimpleNamespace(
                connected=True,
                debug_account_summary=lambda: {"NetLiquidation": "1000"},
            )

            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(routes.ibkr_debug_account_summary())
        finally:
            routes.active_platform = old_active_platform
            routes.ibkr_connector = old_ibkr

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("MT5", str(ctx.exception.detail))

    def test_status_advertises_mt5_only_supported_platform(self):
        class Account:
            login = 123
            balance = 1000.0
            equity = 1000.0
            margin = 0.0
            free_margin = 1000.0
            currency = "USD"

            def model_dump(self):
                return {
                    "login": self.login,
                    "balance": self.balance,
                    "equity": self.equity,
                    "margin": self.margin,
                    "free_margin": self.free_margin,
                    "currency": self.currency,
                }

        class Connector:
            connected = True
            account_info = Account()
            last_error = None

            def refresh_account(self):
                return self.account_info

            def get_terminal_info(self):
                return None

            def is_demo(self):
                return False

            def trade_server_connected(self):
                return True

        class PositionManager:
            is_running = True

            def portfolio_targets_snapshot(self):
                return {}

        class AutoTrader:
            is_running = False
            position_manager = PositionManager()

        class Risk:
            panic_stopped = False
            auto_trade_enabled = False
            auto_trade_session_armed = False
            user_policy = SimpleNamespace(demo_only_default=False, model_dump=lambda: {})

            def runtime_controls(self):
                return {}

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_mt5 = routes.mt5_connector
        old_auto_trader = routes.auto_trader
        old_risk = routes.risk_engine
        old_config_live = routes.config.LIVE_TRADING_ENABLED
        old_execution = routes.execution
        old_current_gemini = routes._current_gemini_status
        old_benzinga_adapter = routes.benzinga_adapter
        old_api_keys_snapshot = routes._api_key_health_snapshot
        old_closed = routes._count_trade_outcomes_direct
        old_cloud_enabled = routes.config.CLOUD_BRAIN_DECISION_ENABLED
        old_credential_vault = routes.credential_vault
        old_universe = routes.universe_service
        old_task_orchestrator = routes.task_orchestrator
        old_signal_pipeline = routes.signal_pipeline
        old_resolve_user = routes._resolve_authenticated_user
        old_active_strategy = routes._get_active_strategy_id_for_user
        old_db = routes.db
        try:
            routes.active_platform = "mt5"
            connector = Connector()
            routes.connector = connector
            routes.mt5_connector = connector
            routes.execution = SimpleNamespace(get_positions=lambda: [])
            routes.auto_trader = AutoTrader()
            routes.risk_engine = Risk()
            routes.config.LIVE_TRADING_ENABLED = True
            routes.config.CLOUD_BRAIN_DECISION_ENABLED = False
            routes._current_gemini_status = lambda: {"state": "ready", "available": True}
            routes.benzinga_adapter = SimpleNamespace(healthcheck=lambda cached_only=True: {"status": "healthy", "degraded": False})
            routes._api_key_health_snapshot = lambda force_refresh=False: {"overall_status": "healthy", "items": [], "unhealthy_count": 0}
            routes._count_trade_outcomes_direct = lambda: 1
            routes.credential_vault = SimpleNamespace(available=True)
            routes.universe_service = SimpleNamespace(summary_dict=lambda: {})
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {})
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=SimpleNamespace(snapshot=lambda account, positions: {}),
                risk_service=SimpleNamespace(execution_engine=None),
                cloud_brain_decision_client=SimpleNamespace(
                    configured=False,
                    disabled_reason=None,
                    enabled=False,
                    last_error=None,
                ),
            )
            async def fake_user(_authorization):
                return {"id": "test-user"}

            routes._resolve_authenticated_user = fake_user

            async def fake_active_strategy(_user_id):
                return "default"

            routes._get_active_strategy_id_for_user = fake_active_strategy
            routes.db = None

            payload = asyncio.run(routes.status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.mt5_connector = old_mt5
            routes.execution = old_execution
            routes.auto_trader = old_auto_trader
            routes.risk_engine = old_risk
            routes.config.LIVE_TRADING_ENABLED = old_config_live
            routes._current_gemini_status = old_current_gemini
            routes.benzinga_adapter = old_benzinga_adapter
            routes._api_key_health_snapshot = old_api_keys_snapshot
            routes._count_trade_outcomes_direct = old_closed
            routes.config.CLOUD_BRAIN_DECISION_ENABLED = old_cloud_enabled
            routes.credential_vault = old_credential_vault
            routes.universe_service = old_universe
            routes.task_orchestrator = old_task_orchestrator
            routes.signal_pipeline = old_signal_pipeline
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_active_strategy
            routes.db = old_db

        self.assertEqual(payload["platforms_supported"], ["mt5", "ibkr", "tradestation"])
        self.assertEqual(payload["platform"], "mt5")

    def test_status_preserves_supported_ibkr_active_platform(self):
        class Connector:
            connected = False
            account_info = None
            last_error = None

            def is_demo(self):
                return False

        class PositionManager:
            is_running = False

            def portfolio_targets_snapshot(self):
                return {}

        class AutoTrader:
            is_running = False
            position_manager = PositionManager()
            recent_trades = []

        class Risk:
            panic_stopped = False
            auto_trade_enabled = False
            auto_trade_session_armed = False
            user_policy = SimpleNamespace(demo_only_default=False, model_dump=lambda: {})

            def runtime_controls(self):
                return {}

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_mt5 = routes.mt5_connector
        old_auto_trader = routes.auto_trader
        old_risk = routes.risk_engine
        old_config_live = routes.config.LIVE_TRADING_ENABLED
        old_current_gemini = routes._current_gemini_status
        old_benzinga_adapter = routes.benzinga_adapter
        old_api_keys_snapshot = routes._api_key_health_snapshot
        old_closed = routes._count_trade_outcomes_direct
        old_cloud_enabled = routes.config.CLOUD_BRAIN_DECISION_ENABLED
        old_credential_vault = routes.credential_vault
        old_universe = routes.universe_service
        old_task_orchestrator = routes.task_orchestrator
        old_signal_pipeline = routes.signal_pipeline
        old_resolve_user = routes._resolve_authenticated_user
        old_active_strategy = routes._get_active_strategy_id_for_user
        old_db = routes.db
        try:
            connector = Connector()
            routes.active_platform = "ibkr"
            routes.connector = connector
            routes.mt5_connector = connector
            routes.auto_trader = AutoTrader()
            routes.risk_engine = Risk()
            routes.config.LIVE_TRADING_ENABLED = True
            routes.config.CLOUD_BRAIN_DECISION_ENABLED = False
            routes._current_gemini_status = lambda: {"state": "ready", "available": True}
            routes.benzinga_adapter = SimpleNamespace(healthcheck=lambda cached_only=True: {"status": "healthy", "degraded": False})
            routes._api_key_health_snapshot = lambda force_refresh=False: {"overall_status": "healthy", "items": [], "unhealthy_count": 0}
            routes._count_trade_outcomes_direct = lambda: 50
            routes.credential_vault = SimpleNamespace(available=True)
            routes.universe_service = SimpleNamespace(summary_dict=lambda: {})
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {})
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=SimpleNamespace(snapshot=lambda account, positions: {}),
                risk_service=SimpleNamespace(execution_engine=None),
                cloud_brain_decision_client=SimpleNamespace(
                    configured=False,
                    disabled_reason=None,
                    enabled=False,
                    last_error=None,
                ),
            )

            async def fake_user(_authorization):
                return {"id": "test-user"}

            routes._resolve_authenticated_user = fake_user

            async def fake_active_strategy(_user_id):
                return "default"

            routes._get_active_strategy_id_for_user = fake_active_strategy
            routes.db = None

            payload = asyncio.run(routes.status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.mt5_connector = old_mt5
            routes.auto_trader = old_auto_trader
            routes.risk_engine = old_risk
            routes.config.LIVE_TRADING_ENABLED = old_config_live
            routes._current_gemini_status = old_current_gemini
            routes.benzinga_adapter = old_benzinga_adapter
            routes._api_key_health_snapshot = old_api_keys_snapshot
            routes._count_trade_outcomes_direct = old_closed
            routes.config.CLOUD_BRAIN_DECISION_ENABLED = old_cloud_enabled
            routes.credential_vault = old_credential_vault
            routes.universe_service = old_universe
            routes.task_orchestrator = old_task_orchestrator
            routes.signal_pipeline = old_signal_pipeline
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_active_strategy
            routes.db = old_db

        self.assertEqual(payload["platforms_supported"], ["mt5", "ibkr", "tradestation"])
        self.assertEqual(payload["platform"], "ibkr")
        self.assertEqual(payload["live_trading_readiness"]["checks"]["platform"], "ibkr")
        self.assertIn("Active platform is ibkr", "\n".join(payload["live_trading_readiness"]["warnings"]))

    def test_auto_trade_status_advertises_supported_platforms(self):
        class Risk:
            panic_stopped = False
            auto_trade_enabled = False
            auto_trade_budget_mode = "full_margin"
            auto_trade_budget_usd = None
            auto_trade_scan_interval_seconds = 30
            settings = SimpleNamespace(auto_trade_min_confidence=0.75)
            user_policy = SimpleNamespace(model_dump=lambda: {"demo_only_default": False})

        class PositionManager:
            is_running = False
            managed_tickets = set()

        class AutoTrader:
            is_running = False
            last_scan_time = None
            recent_trades = []
            position_manager = PositionManager()

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_risk = routes.risk_engine
        old_auto_trader = routes.auto_trader
        old_task_orchestrator = routes.task_orchestrator
        old_signal_pipeline = routes.signal_pipeline
        old_current_gemini = routes._current_gemini_status
        old_resolve_user = routes._resolve_authenticated_user
        old_active_strategy = routes._get_active_strategy_id_for_user
        old_universe = routes.universe_service
        try:
            routes.active_platform = "mt5"
            routes.connector = SimpleNamespace(connected=False)
            routes.risk_engine = Risk()
            routes.auto_trader = AutoTrader()
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {})
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=SimpleNamespace(snapshot=lambda account, positions: {})
            )
            routes._current_gemini_status = lambda: {"available": True, "degraded": False, "state": "available"}
            routes.universe_service = SimpleNamespace(summary_dict=lambda: {"active_asset_classes": []})

            async def fake_user(_authorization):
                return {"id": "test-user"}

            async def fake_active_strategy(_user_id):
                return "builtin-default-smart"

            routes._resolve_authenticated_user = fake_user
            routes._get_active_strategy_id_for_user = fake_active_strategy

            payload = asyncio.run(routes.auto_trade_status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.risk_engine = old_risk
            routes.auto_trader = old_auto_trader
            routes.task_orchestrator = old_task_orchestrator
            routes.signal_pipeline = old_signal_pipeline
            routes._current_gemini_status = old_current_gemini
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_active_strategy
            routes.universe_service = old_universe

        self.assertEqual(payload["platform"], "mt5")
        self.assertEqual(payload["platforms_supported"], ["mt5", "ibkr", "tradestation"])

    def test_available_symbols_includes_flat_symbols_list(self):
        raw_symbols = [
            {
                "name": "XAUUSD",
                "description": "Gold vs US Dollar",
                "category": "Commodities",
                "visible": True,
                "trade_enabled": True,
                "bid": 2400.0,
                "ask": 2400.5,
                "spread": 5,
            },
            {
                "name": "EURUSD",
                "description": "Euro vs US Dollar",
                "category": "Forex",
                "visible": True,
                "trade_enabled": True,
                "bid": 1.08,
                "ask": 1.0801,
                "spread": 1,
            },
        ]

        class MarketData:
            def get_all_symbols(self):
                return list(raw_symbols)

        class Execution:
            def get_recent_commission_by_symbol(self):
                return {}

        class Universe:
            def filter_market_symbols(self, symbols, include_inactive=False):
                return list(symbols)

            def summary_dict(self):
                return {"active_asset_classes": ["Forex", "Commodities"]}

        old_active_platform = routes.active_platform
        old_market_data = routes.market_data
        old_execution = routes.execution
        old_universe = routes.universe_service
        old_require = routes._require_active_platform_connection
        old_active_connected = routes._active_platform_connected
        old_cache = dict(routes._available_symbols_cache)
        try:
            routes.active_platform = "mt5"
            routes.market_data = MarketData()
            routes.execution = Execution()
            routes.universe_service = Universe()
            routes._require_active_platform_connection = lambda: None
            routes._active_platform_connected = lambda: True
            routes._available_symbols_cache.clear()

            payload = asyncio.run(routes.get_available_symbols(tradeable_only=False))
        finally:
            routes.active_platform = old_active_platform
            routes.market_data = old_market_data
            routes.execution = old_execution
            routes.universe_service = old_universe
            routes._require_active_platform_connection = old_require
            routes._active_platform_connected = old_active_connected
            routes._available_symbols_cache.clear()
            routes._available_symbols_cache.update(old_cache)

        self.assertEqual(payload["total"], 2)
        self.assertEqual([row["name"] for row in payload["symbols"]], ["XAUUSD", "EURUSD"])
        self.assertEqual(payload["category_counts"], {"Commodities": 1, "Forex": 1})
        self.assertEqual(payload["categories"]["Commodities"][0]["name"], "XAUUSD")

    def test_available_symbols_disconnected_mt5_returns_top_market_fallback(self):
        class MarketData:
            def get_all_symbols(self):
                raise AssertionError("broker catalog should not be read while disconnected")

            def get_tradeable_symbols(self):
                raise AssertionError("broker catalog should not be read while disconnected")

        class Execution:
            def get_recent_commission_by_symbol(self):
                return {}

        class Universe:
            def filter_market_symbols(self, symbols, include_inactive=False):
                return list(symbols)

            def summary_dict(self):
                return {"active_asset_classes": ["Forex", "Indices", "Commodities", "Stocks", "Crypto"]}

        old_active_platform = routes.active_platform
        old_market_data = routes.market_data
        old_execution = routes.execution
        old_universe = routes.universe_service
        old_active_connected = routes._active_platform_connected
        old_cache = dict(routes._available_symbols_cache)
        try:
            routes.active_platform = "mt5"
            routes.market_data = MarketData()
            routes.execution = Execution()
            routes.universe_service = Universe()
            routes._active_platform_connected = lambda: False
            routes._available_symbols_cache.clear()

            payload = asyncio.run(routes.get_available_symbols(tradeable_only=False))
            tradeable_payload = asyncio.run(routes.get_available_symbols(tradeable_only=True))
        finally:
            routes.active_platform = old_active_platform
            routes.market_data = old_market_data
            routes.execution = old_execution
            routes.universe_service = old_universe
            routes._active_platform_connected = old_active_connected
            routes._available_symbols_cache.clear()
            routes._available_symbols_cache.update(old_cache)

        self.assertEqual(payload["source"], "mt5_disconnected_top_markets")
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["live_quotes"])
        self.assertGreaterEqual(payload["total"], 20)
        self.assertEqual(
            set(payload["category_counts"]),
            {"Forex", "Indices", "Commodities", "Stocks", "Crypto"},
        )
        self.assertTrue(all(row["requires_connection"] for row in payload["symbols"]))
        self.assertEqual(tradeable_payload["total"], 0)

    def test_mt5_bulk_ticks_filters_zero_and_stale_prices(self):
        now = 1_800_000_000.0

        class Connector:
            def trade_server_connected(self):
                return True

        class MarketData:
            def get_ticks(self, symbols):
                return {
                    "XAUUSD": SimpleNamespace(
                        symbol="XAUUSD",
                        bid=2400.0,
                        ask=2400.5,
                        spread=5,
                        time=int(now),
                        time_msc=int(now * 1000),
                        model_dump=lambda: {
                            "symbol": "XAUUSD",
                            "bid": 2400.0,
                            "ask": 2400.5,
                            "spread": 5,
                            "time": int(now),
                            "time_msc": int(now * 1000),
                        },
                    ),
                    "MSFT": SimpleNamespace(
                        symbol="MSFT",
                        bid=399.0,
                        ask=399.4,
                        spread=4,
                        time=int(now - 960),
                        time_msc=int((now - 960) * 1000),
                        model_dump=lambda: {
                            "symbol": "MSFT",
                            "bid": 399.0,
                            "ask": 399.4,
                            "spread": 4,
                            "time": int(now - 960),
                            "time_msc": int((now - 960) * 1000),
                        },
                    ),
                    "AAPL": SimpleNamespace(
                        symbol="AAPL",
                        bid=0.0,
                        ask=0.0,
                        spread=0,
                        time=int(now),
                        time_msc=int(now * 1000),
                        model_dump=lambda: {
                            "symbol": "AAPL",
                            "bid": 0.0,
                            "ask": 0.0,
                            "spread": 0,
                            "time": int(now),
                            "time_msc": int(now * 1000),
                        },
                    ),
                }

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_time = routes.time.time
        try:
            routes.active_platform = "mt5"
            routes.connector = Connector()
            routes.market_data = MarketData()
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes.time.time = lambda: now

            payload = asyncio.run(routes.get_ticks("XAUUSD,MSFT,AAPL"))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes.time.time = old_time

        self.assertEqual(set(payload["ticks"].keys()), {"XAUUSD"})
        self.assertEqual(payload["ticks"]["XAUUSD"]["bid"], 2400.0)

    def test_ibkr_manual_smart_evaluate_does_not_cap_requested_symbols_at_six(self):
        requested = [f"SYM{i}" for i in range(10)]
        old_active_platform = routes.active_platform
        old_run_ibkr = routes._run_ibkr
        old_require = routes._require_active_platform_connection
        old_ibkr_symbols = routes._ibkr_market_symbols
        old_evaluate = routes._evaluate_ibkr_symbol_recommendation
        old_allowed = routes.risk_engine.settings.allowed_symbols
        try:
            routes.active_platform = "ibkr"
            routes._require_active_platform_connection = lambda: None
            routes.risk_engine.settings.allowed_symbols = []
            routes._ibkr_market_symbols = lambda include_inactive=False: [
                {
                    "name": sym,
                    "category": "Stocks",
                    "trade_enabled": True,
                    "visible": True,
                    "bid": 10.0,
                    "ask": 10.1,
                }
                for sym in requested
            ]

            async def fake_run_ibkr(fn, *args):
                return fn(*args)

            async def fake_evaluate(sym):
                return {
                    "symbol": sym,
                    "signal": {"confidence": 0.1},
                    "trade_quality": {"final_trade_quality_score": 0.1},
                    "ready_to_execute": False,
                }

            routes._run_ibkr = fake_run_ibkr
            routes._evaluate_ibkr_symbol_recommendation = fake_evaluate

            payload = asyncio.run(routes.smart_evaluate(routes.SmartEvaluateRequest(symbols=requested, manual=True)))
        finally:
            routes.active_platform = old_active_platform
            routes._run_ibkr = old_run_ibkr
            routes._require_active_platform_connection = old_require
            routes._ibkr_market_symbols = old_ibkr_symbols
            routes._evaluate_ibkr_symbol_recommendation = old_evaluate
            routes.risk_engine.settings.allowed_symbols = old_allowed

        self.assertEqual(len(payload["recommendations"]), len(requested))

    def test_bulk_ticks_preserves_ibkr_platform_source(self):
        now = 1_800_000_000.0

        class Tick:
            bid = 2400.0
            ask = 2400.5
            spread = 5
            time = int(now)
            time_msc = int(now * 1000)

            def model_dump(self):
                return {
                    "symbol": "XAUUSD",
                    "bid": self.bid,
                    "ask": self.ask,
                    "spread": self.spread,
                    "time": self.time,
                    "time_msc": self.time_msc,
                }

        class StaleMarketData:
            def get_ticks(self, symbols):
                raise AssertionError("stale non-MT5 market data must not be used")

        class Mt5MarketData:
            def get_ticks(self, symbols):
                self.requested = list(symbols)
                return {"XAUUSD": Tick()}

        old_active_platform = routes.active_platform
        old_require = routes._require_active_platform_connection
        old_market_data = routes.market_data
        old_mt5_market_data = routes.mt5_market_data
        old_apply = routes._apply_active_broker_runtime
        old_resolve = routes._resolve_market_symbol
        old_time = routes.time.time
        mt5_market_data = Mt5MarketData()
        try:
            routes.active_platform = "ibkr"
            routes._require_active_platform_connection = lambda: None
            routes.market_data = StaleMarketData()
            routes.mt5_market_data = mt5_market_data

            def fake_apply(platform):
                self.assertEqual(platform, "mt5")
                routes.market_data = routes.mt5_market_data

            routes._apply_active_broker_runtime = fake_apply
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes.time.time = lambda: now

            payload = asyncio.run(routes.get_ticks("XAUUSD"))
        finally:
            routes.active_platform = old_active_platform
            routes._require_active_platform_connection = old_require
            routes.market_data = old_market_data
            routes.mt5_market_data = old_mt5_market_data
            routes._apply_active_broker_runtime = old_apply
            routes._resolve_market_symbol = old_resolve
            routes.time.time = old_time

        self.assertEqual(payload["source"], "ibkr_broker_snapshot")
        self.assertEqual(payload["ticks"], {})

    def test_stale_ticks_are_not_treated_as_live_prices(self):
        import time

        fresh = SimpleNamespace(bid=1.0, ask=1.1, time=time.time(), time_msc=int(time.time() * 1000))
        slow_session = SimpleNamespace(bid=1.0, ask=1.1, time=time.time() - 600, time_msc=int((time.time() - 600) * 1000))
        stale = SimpleNamespace(bid=1.0, ask=1.1, time=time.time() - 960, time_msc=int((time.time() - 960) * 1000))
        bad = SimpleNamespace(bid=0.0, ask=1.1, time=time.time(), time_msc=int(time.time() * 1000))
        undated = SimpleNamespace(bid=1.0, ask=1.1, time=0, time_msc=0)

        self.assertTrue(_is_fresh_tick(fresh))
        self.assertTrue(_is_fresh_tick(slow_session))
        self.assertFalse(_is_fresh_tick(stale))
        self.assertFalse(_is_fresh_tick(bad))
        self.assertFalse(_is_fresh_tick(undated))

    def test_resolve_market_symbol_prefers_fresh_broker_alias_over_stale_exact_symbol(self):
        now = 1_800_000_000.0

        class MarketData:
            def get_symbol_info(self, symbol):
                if str(symbol).upper() in {"GER40", "DAX"}:
                    return {"name": str(symbol).upper()}
                return None

            def enable_symbol(self, _symbol):
                return True

            def get_tick(self, symbol):
                upper = str(symbol).upper()
                if upper == "GER40":
                    return SimpleNamespace(bid=18000.0, ask=18001.0, time=0, time_msc=0)
                if upper == "DAX":
                    return SimpleNamespace(bid=18002.0, ask=18003.0, time=int(now), time_msc=int(now * 1000))
                return None

            def get_all_symbols(self):
                return [
                    {"name": "GER40", "trade_enabled": True, "visible": True},
                    {"name": "DAX", "trade_enabled": True, "visible": True},
                ]

        class Universe:
            def canonical_symbol(self, symbol):
                return str(symbol or "").strip().upper()

            def resolve_requested_symbols(self, _symbols, _all_symbols):
                return ["GER40"]

        old_market_data = routes.market_data
        old_universe = routes.universe_service
        old_time = routes.time.time
        try:
            routes.market_data = MarketData()
            routes.universe_service = Universe()
            routes.time.time = lambda: now

            resolved = routes._resolve_market_symbol("GER40")
        finally:
            routes.market_data = old_market_data
            routes.universe_service = old_universe
            routes.time.time = old_time

        self.assertEqual(resolved, "DAX")

    def test_single_tick_endpoint_rejects_undated_mt5_tick(self):
        class UndatedTick:
            bid = 1.0
            ask = 1.1
            spread = 10
            time = 0
            time_msc = 0

            def model_dump(self):
                return {
                    "symbol": "EURUSD",
                    "bid": self.bid,
                    "ask": self.ask,
                    "spread": self.spread,
                    "time": self.time,
                    "time_msc": self.time_msc,
                }

        class MarketData:
            def get_tick(self, _symbol):
                return UndatedTick()

        old_active_platform = routes.active_platform
        old_market_data = routes.market_data
        old_require = routes._require_active_platform_connection
        old_resolve = routes._resolve_market_symbol
        try:
            routes.active_platform = "mt5"
            routes.market_data = MarketData()
            routes._require_active_platform_connection = lambda: None
            routes._resolve_market_symbol = lambda symbol: symbol

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(routes.get_tick("EURUSD"))
        finally:
            routes.active_platform = old_active_platform
            routes.market_data = old_market_data
            routes._require_active_platform_connection = old_require
            routes._resolve_market_symbol = old_resolve

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("No fresh tick data", str(raised.exception.detail))

    def test_positions_fast_snapshot_skips_detail_lookups(self):
        class FastExecution:
            def get_positions(self, symbol=None):
                return [
                    PositionInfo(
                        ticket=123,
                        symbol="NVDA",
                        type="BUY",
                        volume=1.0,
                        price_open=220.0,
                        price_current=224.5,
                        stop_loss=218.0,
                        take_profit=228.0,
                        profit=45.0,
                        time=1_700_000_000,
                        comment="TA1000|SLA100|TPA200",
                    )
                ]

            def estimate_margin(self, **_kwargs):
                raise AssertionError("fast position snapshot should not estimate margin")

        class NoDetailMarketData:
            def get_symbol_info(self, _symbol):
                raise AssertionError("fast position snapshot should not load symbol info")

        class NoDetailDb:
            async def get_order_with_context_by_ticket(self, _ticket):
                raise AssertionError("fast position snapshot should not query order context")

            async def get_position_management_plan(self, _ticket):
                raise AssertionError("fast position snapshot should not query management plans")

        old_active_platform = routes.active_platform
        old_execution = routes.execution
        old_market_data = routes.market_data
        old_db = routes.db
        old_require = routes._require_active_platform_connection
        try:
            routes.active_platform = "mt5"
            routes.execution = FastExecution()
            routes.market_data = NoDetailMarketData()
            routes.db = NoDetailDb()
            routes._positions_payload_cache.clear()
            routes._require_active_platform_connection = lambda: None

            payload = asyncio.run(routes.get_positions(include_margin=False))
        finally:
            routes.active_platform = old_active_platform
            routes.execution = old_execution
            routes.market_data = old_market_data
            routes.db = old_db
            routes._require_active_platform_connection = old_require
            routes._positions_payload_cache.clear()

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["ticket"], 123)
        self.assertEqual(payload[0]["price_current"], 224.5)
        self.assertEqual(payload[0]["profit"], 45.0)
        self.assertEqual(payload[0]["target_management_mode"], "automatic")
        self.assertNotIn("estimated_margin", payload[0])

    def test_positions_do_not_invent_target_amounts_without_symbol_pricing(self):
        class MarginExecution:
            def get_positions(self, symbol=None):
                return [
                    PositionInfo(
                        ticket=456,
                        symbol="XAGUSD",
                        type="SELL",
                        volume=1.21,
                        price_open=74.858,
                        price_current=74.8,
                        stop_loss=76.3,
                        take_profit=72.117,
                        profit=0.0,
                        time=1_700_000_000,
                        comment="TA3481",
                    )
                ]

            def estimate_margin(self, **_kwargs):
                return 3480.9

        class MissingPricingMarketData:
            def get_symbol_info(self, _symbol):
                return {"name": "XAGUSD"}

        class NoDetailDb:
            async def get_order_with_context_by_ticket(self, _ticket):
                return None

            async def get_position_management_plan(self, _ticket):
                return None

        old_active_platform = routes.active_platform
        old_execution = routes.execution
        old_market_data = routes.market_data
        old_db = routes.db
        old_require = routes._require_active_platform_connection
        try:
            routes.active_platform = "mt5"
            routes.execution = MarginExecution()
            routes.market_data = MissingPricingMarketData()
            routes.db = NoDetailDb()
            routes._positions_payload_cache.clear()
            routes._require_active_platform_connection = lambda: None

            payload = asyncio.run(routes.get_positions(include_margin=True))
        finally:
            routes.active_platform = old_active_platform
            routes.execution = old_execution
            routes.market_data = old_market_data
            routes.db = old_db
            routes._require_active_platform_connection = old_require
            routes._positions_payload_cache.clear()

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["ticket"], 456)
        self.assertEqual(payload[0]["estimated_margin"], 3480.9)
        self.assertIsNone(payload[0]["sl_amount_usd"])
        self.assertIsNone(payload[0]["tp_amount_usd"])

    def test_auto_trade_settings_enable_blocks_live_account_when_policy_disallows(self):
        class LiveConnector:
            def refresh_account(self):
                return SimpleNamespace(margin=0.0)

            def is_demo(self):
                return False

        class TaskOrchestrator:
            def __init__(self):
                self.started = False

            def start_auto_trade(self):
                self.started = True

            def stop_auto_trade(self, *, keep_position_manager=False):
                pass

            def status_snapshot(self):
                return {"auto_trader": {"running": self.started}}

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_task = routes.task_orchestrator
        old_auto_trader = routes.auto_trader
        old_active_connected = routes._active_platform_connected
        old_persist = routes._schedule_runtime_state_persist
        old_enabled = routes.risk_engine.auto_trade_enabled
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        task = TaskOrchestrator()
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.task_orchestrator = task
            routes.auto_trader = SimpleNamespace(is_running=False)
            routes._active_platform_connected = lambda: True
            routes._schedule_runtime_state_persist = lambda: None
            routes.risk_engine.auto_trade_enabled = False
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.auto_trade_settings(routes.AutoTradeSettingsRequest(enabled=True)))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.task_orchestrator = old_task
            routes.auto_trader = old_auto_trader
            routes._active_platform_connected = old_active_connected
            routes._schedule_runtime_state_persist = old_persist
            routes.risk_engine.auto_trade_enabled = old_enabled
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertEqual(cm.exception.status_code, 403)
        self.assertFalse(task.started)

    def test_auto_trade_settings_enable_requires_connected_mt5_account(self):
        class DisconnectedConnector:
            connected = False

            def refresh_account(self):
                return None

            def is_demo(self):
                return False

        class TaskOrchestrator:
            def __init__(self):
                self.started = False

            def start_auto_trade(self):
                self.started = True

            def stop_auto_trade(self, *, keep_position_manager=False):
                pass

            def status_snapshot(self):
                return {"auto_trader": {"running": self.started}}

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_task = routes.task_orchestrator
        old_auto_trader = routes.auto_trader
        old_active_connected = routes._active_platform_connected
        old_persist = routes._schedule_runtime_state_persist
        old_enabled = routes.risk_engine.auto_trade_enabled
        old_session_armed = routes.risk_engine.auto_trade_session_armed
        task = TaskOrchestrator()
        try:
            routes.active_platform = "mt5"
            routes.connector = DisconnectedConnector()
            routes.task_orchestrator = task
            routes.auto_trader = SimpleNamespace(is_running=False)
            routes._active_platform_connected = lambda: False
            routes._schedule_runtime_state_persist = lambda: None
            routes.risk_engine.auto_trade_enabled = False
            routes.risk_engine.auto_trade_session_armed = False

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.auto_trade_settings(routes.AutoTradeSettingsRequest(enabled=True)))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.task_orchestrator = old_task
            routes.auto_trader = old_auto_trader
            routes._active_platform_connected = old_active_connected
            routes._schedule_runtime_state_persist = old_persist
            routes.risk_engine.auto_trade_enabled = old_enabled
            routes.risk_engine.auto_trade_session_armed = old_session_armed

        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Connect an MT5 live trading account", str(cm.exception.detail))
        self.assertFalse(routes.risk_engine.auto_trade_enabled)
        self.assertFalse(routes.risk_engine.auto_trade_session_armed)
        self.assertFalse(task.started)

    def test_cloud_command_cannot_enable_auto_trade_while_mt5_disconnected(self):
        class DisconnectedConnector:
            connected = False

            def trade_server_connected(self):
                return False

            def is_demo(self):
                return False

        class TaskOrchestrator:
            def __init__(self):
                self.started = False

            def start_auto_trade(self):
                self.started = True

            def stop_auto_trade(self):
                pass

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_task = routes.task_orchestrator
        old_auto_trader = routes.auto_trader
        old_persist = routes._persist_runtime_state
        old_enabled = routes.risk_engine.auto_trade_enabled
        old_session_armed = routes.risk_engine.auto_trade_session_armed
        task = TaskOrchestrator()
        try:
            routes.active_platform = "mt5"
            routes.connector = DisconnectedConnector()
            routes.task_orchestrator = task
            routes.auto_trader = SimpleNamespace(is_running=False)
            routes._persist_runtime_state = lambda: asyncio.sleep(0)
            routes.risk_engine.auto_trade_enabled = False
            routes.risk_engine.auto_trade_session_armed = False

            result = asyncio.run(routes.apply_cloud_brain_command({"auto_trade_enabled": True}))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.task_orchestrator = old_task
            routes.auto_trader = old_auto_trader
            routes._persist_runtime_state = old_persist
            routes.risk_engine.auto_trade_enabled = old_enabled
            routes.risk_engine.auto_trade_session_armed = old_session_armed

        self.assertFalse(result["updated"])
        self.assertIn("auto_trade_enabled", result["rejected"])
        self.assertFalse(routes.risk_engine.auto_trade_enabled)
        self.assertFalse(routes.risk_engine.auto_trade_session_armed)
        self.assertFalse(task.started)

    def test_cloud_command_scan_interval_keeps_settings_route_safety_floor(self):
        old_interval = routes.risk_engine.auto_trade_scan_interval_seconds
        old_persist = routes._persist_runtime_state
        try:
            routes.risk_engine.auto_trade_scan_interval_seconds = 30
            routes._persist_runtime_state = lambda: asyncio.sleep(0)

            result = asyncio.run(routes.apply_cloud_brain_command({"scan_interval_seconds": 5}))
        finally:
            restored_interval = routes.risk_engine.auto_trade_scan_interval_seconds
            routes.risk_engine.auto_trade_scan_interval_seconds = old_interval
            routes._persist_runtime_state = old_persist

        self.assertIn("scan_interval_seconds", result["updated"])
        self.assertEqual(restored_interval, 30)

    def test_restore_runtime_state_keeps_auto_trade_scan_interval_safety_floor(self):
        class Db:
            async def get_runtime_state(self, key):
                if key == routes.RUNTIME_STATE_KEY:
                    return {
                        "user_policy": {"demo_only_default": False},
                        "auto_trade_enabled": True,
                        "auto_trade_scan_interval_seconds": 5,
                        "auto_trade_budget_mode": "full_margin",
                        "auto_trade_budget_usd": None,
                        "portfolio_targets": {},
                    }
                return None

        class Risk:
            def __init__(self):
                self.auto_trade_enabled = True
                self.auto_trade_session_armed = True
                self.auto_trade_scan_interval_seconds = 10
                self.auto_trade_budget_mode = "custom_budget"
                self.auto_trade_budget_usd = 100.0

            def update_user_policy(self, policy):
                self.user_policy = policy

        class PositionManager:
            is_running = True

            def set_portfolio_targets(self, **_kwargs):
                pass

        old_db = routes.db
        old_risk = routes.risk_engine
        old_auto_trader = routes.auto_trader
        old_connector = routes.connector
        old_apply = routes._apply_active_broker_runtime
        old_sync = routes._sync_active_strategy_runtime_for_user
        try:
            routes.db = Db()
            routes.risk_engine = Risk()
            routes.auto_trader = SimpleNamespace(position_manager=PositionManager())
            routes.connector = SimpleNamespace(connected=False)
            routes._apply_active_broker_runtime = lambda _platform: None

            async def fake_sync(*_args, **_kwargs):
                return None

            routes._sync_active_strategy_runtime_for_user = fake_sync

            asyncio.run(routes.restore_runtime_state())
            restored_interval = routes.risk_engine.auto_trade_scan_interval_seconds
            restored_enabled = routes.risk_engine.auto_trade_enabled
            restored_armed = routes.risk_engine.auto_trade_session_armed
        finally:
            routes.db = old_db
            routes.risk_engine = old_risk
            routes.auto_trader = old_auto_trader
            routes.connector = old_connector
            routes._apply_active_broker_runtime = old_apply
            routes._sync_active_strategy_runtime_for_user = old_sync

        self.assertEqual(restored_interval, 30)
        self.assertFalse(restored_enabled)
        self.assertFalse(restored_armed)

    def test_auto_trade_start_allows_live_mt5_when_policy_allows(self):
        class LiveConnector:
            connected = True

            def trade_server_connected(self):
                return True

            def refresh_account(self):
                return SimpleNamespace(margin=250.0)

            def is_demo(self):
                return False

        class TaskOrchestrator:
            def __init__(self):
                self.started = False

            def start_auto_trade(self):
                self.started = True

            def status_snapshot(self):
                return {"auto_trader": {"running": self.started}}

        task = TaskOrchestrator()
        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_task = routes.task_orchestrator
        old_auto_trader = routes.auto_trader
        old_sync = routes._sync_active_strategy_runtime_for_user
        old_persist = routes._schedule_runtime_state_persist
        old_event = routes._schedule_connection_event
        old_enabled = routes.risk_engine.auto_trade_enabled
        old_session_armed = routes.risk_engine.auto_trade_session_armed
        old_budget_mode = routes.risk_engine.auto_trade_budget_mode
        old_budget_usd = routes.risk_engine.auto_trade_budget_usd
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        enabled_after_start = False
        armed_after_start = False
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.task_orchestrator = task
            routes.auto_trader = SimpleNamespace(position_manager=SimpleNamespace(is_running=True))

            async def fake_sync(*_args, **_kwargs):
                return None

            routes._sync_active_strategy_runtime_for_user = fake_sync
            routes._schedule_runtime_state_persist = lambda: None
            routes._schedule_connection_event = lambda *_args, **_kwargs: None
            routes.risk_engine.auto_trade_enabled = False
            routes.risk_engine.auto_trade_session_armed = False
            routes.risk_engine.auto_trade_budget_mode = "full_margin"
            routes.risk_engine.auto_trade_budget_usd = None
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False

            payload = asyncio.run(routes.auto_trade_start(routes.AutoTradeStartRequest()))
            enabled_after_start = bool(routes.risk_engine.auto_trade_enabled)
            armed_after_start = bool(routes.risk_engine.auto_trade_session_armed)
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.task_orchestrator = old_task
            routes.auto_trader = old_auto_trader
            routes._sync_active_strategy_runtime_for_user = old_sync
            routes._schedule_runtime_state_persist = old_persist
            routes._schedule_connection_event = old_event
            routes.risk_engine.auto_trade_enabled = old_enabled
            routes.risk_engine.auto_trade_session_armed = old_session_armed
            routes.risk_engine.auto_trade_budget_mode = old_budget_mode
            routes.risk_engine.auto_trade_budget_usd = old_budget_usd
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertTrue(payload["running"])
        self.assertTrue(task.started)
        self.assertTrue(enabled_after_start)
        self.assertTrue(armed_after_start)

    def test_auto_trade_start_live_blocker_explains_required_live_policy(self):
        class LiveConnector:
            connected = True

            def trade_server_connected(self):
                return True

            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.auto_trade_start(routes.AutoTradeStartRequest()))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("LIVE_TRADING_ENABLED=true", str(cm.exception.detail))
        self.assertIn("demo-only mode off", str(cm.exception.detail))

    def test_auto_trade_settings_enable_allows_live_mt5_when_policy_allows(self):
        class LiveConnector:
            def refresh_account(self):
                return SimpleNamespace(margin=0.0)

            def is_demo(self):
                return False

        class TaskOrchestrator:
            def __init__(self):
                self.started = False

            def start_auto_trade(self):
                self.started = True

            def stop_auto_trade(self, *, keep_position_manager=False):
                pass

            def status_snapshot(self):
                return {"auto_trader": {"running": self.started}}

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_task = routes.task_orchestrator
        old_auto_trader = routes.auto_trader
        old_active_connected = routes._active_platform_connected
        old_persist = routes._schedule_runtime_state_persist
        old_enabled = routes.risk_engine.auto_trade_enabled
        old_session_armed = routes.risk_engine.auto_trade_session_armed
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        task = TaskOrchestrator()
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.task_orchestrator = task
            routes.auto_trader = SimpleNamespace(is_running=False)
            routes._active_platform_connected = lambda: True
            routes._schedule_runtime_state_persist = lambda: None
            routes.risk_engine.auto_trade_enabled = False
            routes.risk_engine.auto_trade_session_armed = False
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False

            payload = asyncio.run(routes.auto_trade_settings(routes.AutoTradeSettingsRequest(enabled=True)))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.task_orchestrator = old_task
            routes.auto_trader = old_auto_trader
            routes._active_platform_connected = old_active_connected
            routes._schedule_runtime_state_persist = old_persist
            routes.risk_engine.auto_trade_enabled = old_enabled
            routes.risk_engine.auto_trade_session_armed = old_session_armed
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertTrue(payload["enabled"])
        self.assertTrue(task.started)

    def test_status_live_mt5_policy_allowed_reports_warning_not_blocker(self):
        class LiveConnector:
            connected = True
            last_error = ""
            account_info = SimpleNamespace(
                equity=100000.0,
                balance=100000.0,
                margin=0.0,
                currency="USD",
                model_dump=lambda: {
                    "equity": 100000.0,
                    "balance": 100000.0,
                    "margin": 0.0,
                    "currency": "USD",
                },
            )

            def trade_server_connected(self):
                return True

            def refresh_account(self):
                return self.account_info

            def get_terminal_info(self):
                return SimpleNamespace(model_dump=lambda: {"connected": True})

            def is_demo(self):
                return False

        class Execution:
            def get_positions(self):
                return []

        class PortfolioRisk:
            def snapshot(self, account, positions):
                return {
                    "margin_utilization_pct": 0.0,
                    "free_margin_pct": 100.0,
                    "open_positions_total": 0,
                    "exposure_by_symbol": {},
                    "exposure_by_category": {},
                    "exposure_by_sector": {},
                    "usd_beta_exposure_pct": 0.0,
                    "stocks_equity_exposure_pct": 0.0,
                }

        class PositionManager:
            is_running = True
            managed_tickets = []

            def start(self):
                self.is_running = True

            def portfolio_targets_snapshot(self):
                return {}

        class Db:
            async def count_trade_outcomes(self):
                return 100

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_execution = routes.execution
        old_signal_pipeline = routes.signal_pipeline
        old_auto_trader = routes.auto_trader
        old_task = routes.task_orchestrator
        old_risk_engine = routes.risk_engine
        old_db = routes.db
        old_credential_vault = routes.credential_vault
        old_benzinga = routes.benzinga_adapter
        old_resolve_user = routes._resolve_authenticated_user
        old_strategy_id = routes._get_active_strategy_id_for_user
        old_gemini_status = routes._current_gemini_status
        old_api_key_health = routes._api_key_health_snapshot
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.execution = Execution()
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=PortfolioRisk(),
                cloud_brain_decision_client=SimpleNamespace(
                    enabled=False,
                    configured=False,
                    disabled_reason="",
                ),
            )
            routes.auto_trader = SimpleNamespace(
                position_manager=PositionManager(),
                is_running=False,
                recent_trades=[],
            )
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {"auto_trader": {"running": False}})
            routes.risk_engine = SimpleNamespace(
                user_policy=SimpleNamespace(
                    demo_only_default=False,
                    model_dump=lambda: {"demo_only_default": False},
                ),
                panic_stopped=False,
                auto_trade_enabled=False,
                runtime_controls=lambda: {},
                settings=SimpleNamespace(model_dump=lambda: {}),
            )
            routes.db = Db()
            routes.credential_vault = SimpleNamespace(available=True)
            routes.benzinga_adapter = SimpleNamespace(healthcheck=lambda **_kwargs: {"degraded": False, "status": "healthy"})

            async def fake_user(_authorization=None):
                return {"id": "local-anonymous"}

            async def fake_strategy_id(_user_id):
                return "builtin-default-smart"

            routes._resolve_authenticated_user = fake_user
            routes._get_active_strategy_id_for_user = fake_strategy_id
            routes._current_gemini_status = lambda: {
                "available": True,
                "degraded": False,
                "state": "available",
                "primary_model": "test",
                "active_model": "test",
            }
            routes._api_key_health_snapshot = lambda force_refresh=False: {
                "overall_status": "degraded",
                "unhealthy_count": 1,
                "items": [
                    {
                        "id": "supabase_service_role",
                        "name": "Supabase Service Role",
                        "configured": False,
                        "enabled": True,
                        "available": False,
                        "degraded": True,
                        "state": "missing",
                        "status": "degraded",
                        "reason": "SUPABASE_SERVICE_ROLE_KEY missing.",
                        "checked_at": 123.0,
                    }
                ],
                "email_alerts": {"enabled": False, "configured": False, "last_alert_at": None},
            }
            routes.config.LIVE_TRADING_ENABLED = True

            payload = asyncio.run(routes.status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.execution = old_execution
            routes.signal_pipeline = old_signal_pipeline
            routes.auto_trader = old_auto_trader
            routes.task_orchestrator = old_task
            routes.risk_engine = old_risk_engine
            routes.db = old_db
            routes.credential_vault = old_credential_vault
            routes.benzinga_adapter = old_benzinga
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_strategy_id
            routes._current_gemini_status = old_gemini_status
            routes._api_key_health_snapshot = old_api_key_health
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled

        readiness = payload["publish_readiness"]
        self.assertEqual(payload["is_demo"], False)
        self.assertEqual(payload["live_trading_enabled"], True)
        self.assertNotIn(
            "Connected account is live, but live trading is blocked by policy or backend configuration.",
            readiness["blockers"],
        )
        self.assertIn("Connected account is live. Real-money execution safeguards are active.", readiness["warnings"])
        self.assertIn("API/provider health degraded: Supabase Service Role", readiness["warnings"])
        self.assertEqual(readiness["status"], "warning")
        self.assertEqual(payload["api_keys_health"]["overall_status"], "degraded")
        live_readiness = payload["live_trading_readiness"]
        self.assertEqual(live_readiness["status"], "ready")
        self.assertTrue(live_readiness["can_execute_live_orders"])
        self.assertEqual(live_readiness["blockers"], [])
        self.assertTrue(live_readiness["checks"]["backend_live_trading_enabled"])
        self.assertFalse(live_readiness["checks"]["demo_only_default"])
        self.assertFalse(live_readiness["checks"]["account_is_demo"])

    def test_status_live_mt5_policy_blocked_reports_blocker(self):
        class LiveConnector:
            connected = True
            last_error = ""
            account_info = SimpleNamespace(
                equity=100000.0,
                balance=100000.0,
                margin=0.0,
                currency="USD",
                model_dump=lambda: {},
            )

            def trade_server_connected(self):
                return True

            def refresh_account(self):
                return self.account_info

            def get_terminal_info(self):
                return SimpleNamespace(model_dump=lambda: {"connected": True})

            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_execution = routes.execution
        old_signal_pipeline = routes.signal_pipeline
        old_auto_trader = routes.auto_trader
        old_task = routes.task_orchestrator
        old_risk_engine = routes.risk_engine
        old_db = routes.db
        old_credential_vault = routes.credential_vault
        old_benzinga = routes.benzinga_adapter
        old_resolve_user = routes._resolve_authenticated_user
        old_strategy_id = routes._get_active_strategy_id_for_user
        old_gemini_status = routes._current_gemini_status
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.execution = SimpleNamespace(get_positions=lambda: [])
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=SimpleNamespace(snapshot=lambda _account, _positions: {}),
                cloud_brain_decision_client=SimpleNamespace(enabled=False, configured=False, disabled_reason=""),
            )
            routes.auto_trader = SimpleNamespace(
                position_manager=SimpleNamespace(
                    is_running=True,
                    start=lambda: None,
                    portfolio_targets_snapshot=lambda: {},
                ),
                is_running=False,
                recent_trades=[],
            )
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {})
            routes.risk_engine = SimpleNamespace(
                user_policy=SimpleNamespace(
                    demo_only_default=True,
                    model_dump=lambda: {"demo_only_default": True},
                ),
                panic_stopped=False,
                auto_trade_enabled=False,
                runtime_controls=lambda: {},
                settings=SimpleNamespace(model_dump=lambda: {}),
            )
            routes.db = SimpleNamespace(count_trade_outcomes=lambda: asyncio.sleep(0, result=100))
            routes.credential_vault = SimpleNamespace(available=True)
            routes.benzinga_adapter = SimpleNamespace(healthcheck=lambda **_kwargs: {"degraded": False, "status": "healthy"})

            async def fake_user(_authorization=None):
                return {"id": "local-anonymous"}

            async def fake_strategy_id(_user_id):
                return "builtin-default-smart"

            routes._resolve_authenticated_user = fake_user
            routes._get_active_strategy_id_for_user = fake_strategy_id
            routes._current_gemini_status = lambda: {"available": True, "degraded": False, "state": "available"}
            routes.config.LIVE_TRADING_ENABLED = False

            payload = asyncio.run(routes.status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.execution = old_execution
            routes.signal_pipeline = old_signal_pipeline
            routes.auto_trader = old_auto_trader
            routes.task_orchestrator = old_task
            routes.risk_engine = old_risk_engine
            routes.db = old_db
            routes.credential_vault = old_credential_vault
            routes.benzinga_adapter = old_benzinga
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_strategy_id
            routes._current_gemini_status = old_gemini_status
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled

        readiness = payload["publish_readiness"]
        self.assertEqual(readiness["status"], "blocked")
        self.assertIn(
            "Connected account is live, but live trading is blocked by policy or backend configuration.",
            readiness["blockers"],
        )
        live_readiness = payload["live_trading_readiness"]
        self.assertEqual(live_readiness["status"], "blocked")
        self.assertFalse(live_readiness["can_execute_live_orders"])
        self.assertIn("Set LIVE_TRADING_ENABLED=true in the backend environment.", live_readiness["blockers"])
        self.assertIn("Turn Demo-Only By Default off in Policy & Runtime.", live_readiness["blockers"])

    def test_status_demo_mt5_blocks_real_money_readiness_even_when_policy_allows(self):
        class DemoConnector:
            connected = True
            last_error = ""
            account_info = SimpleNamespace(
                equity=100000.0,
                balance=100000.0,
                margin=0.0,
                currency="USD",
                model_dump=lambda: {},
            )

            def trade_server_connected(self):
                return True

            def refresh_account(self):
                return self.account_info

            def get_terminal_info(self):
                return SimpleNamespace(model_dump=lambda: {"connected": True})

            def is_demo(self):
                return True

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_execution = routes.execution
        old_signal_pipeline = routes.signal_pipeline
        old_auto_trader = routes.auto_trader
        old_task = routes.task_orchestrator
        old_risk_engine = routes.risk_engine
        old_db = routes.db
        old_credential_vault = routes.credential_vault
        old_benzinga = routes.benzinga_adapter
        old_resolve_user = routes._resolve_authenticated_user
        old_strategy_id = routes._get_active_strategy_id_for_user
        old_gemini_status = routes._current_gemini_status
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        try:
            routes.active_platform = "mt5"
            routes.connector = DemoConnector()
            routes.execution = SimpleNamespace(get_positions=lambda: [])
            routes.signal_pipeline = SimpleNamespace(
                portfolio_risk_service=SimpleNamespace(snapshot=lambda _account, _positions: {}),
                cloud_brain_decision_client=SimpleNamespace(enabled=False, configured=False, disabled_reason=""),
            )
            routes.auto_trader = SimpleNamespace(
                position_manager=SimpleNamespace(
                    is_running=True,
                    start=lambda: None,
                    portfolio_targets_snapshot=lambda: {},
                ),
                is_running=False,
                recent_trades=[],
            )
            routes.task_orchestrator = SimpleNamespace(status_snapshot=lambda: {})
            routes.risk_engine = SimpleNamespace(
                user_policy=SimpleNamespace(
                    demo_only_default=False,
                    model_dump=lambda: {"demo_only_default": False},
                ),
                panic_stopped=False,
                auto_trade_enabled=False,
                auto_trade_session_armed=False,
                runtime_controls=lambda: {},
                settings=SimpleNamespace(model_dump=lambda: {}),
            )
            routes.db = SimpleNamespace(count_trade_outcomes=lambda: asyncio.sleep(0, result=100))
            routes.credential_vault = SimpleNamespace(available=True)
            routes.benzinga_adapter = SimpleNamespace(healthcheck=lambda **_kwargs: {"degraded": False, "status": "healthy"})

            async def fake_user(_authorization=None):
                return {"id": "local-anonymous"}

            async def fake_strategy_id(_user_id):
                return "builtin-default-smart"

            routes._resolve_authenticated_user = fake_user
            routes._get_active_strategy_id_for_user = fake_strategy_id
            routes._current_gemini_status = lambda: {"available": True, "degraded": False, "state": "available"}
            routes.config.LIVE_TRADING_ENABLED = True

            payload = asyncio.run(routes.status())
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.execution = old_execution
            routes.signal_pipeline = old_signal_pipeline
            routes.auto_trader = old_auto_trader
            routes.task_orchestrator = old_task
            routes.risk_engine = old_risk_engine
            routes.db = old_db
            routes.credential_vault = old_credential_vault
            routes.benzinga_adapter = old_benzinga
            routes._resolve_authenticated_user = old_resolve_user
            routes._get_active_strategy_id_for_user = old_strategy_id
            routes._current_gemini_status = old_gemini_status
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled

        live_readiness = payload["live_trading_readiness"]
        self.assertEqual(live_readiness["status"], "blocked")
        self.assertFalse(live_readiness["can_execute_live_orders"])
        self.assertIn(
            "Connected account is demo. Real-money validation requires a live MT5 account.",
            live_readiness["blockers"],
        )
        self.assertTrue(live_readiness["checks"]["account_is_demo"])

    def test_connect_live_mt5_allowed_sets_daily_start_equity_without_db(self):
        class Account:
            equity = 123456.78

            def model_dump(self):
                return {"equity": self.equity}

        class LiveConnector:
            connected = False
            account_info = Account()
            last_error = ""

            def connect_terminal_session(self, params):
                self.connected = True
                self.params = params
                return True

            def is_demo(self):
                return False

            def trade_server_connected(self):
                return True

            def disconnect(self):
                self.connected = False
                return True

        class RiskEngine:
            def __init__(self):
                self.user_policy = SimpleNamespace(demo_only_default=False, model_dump=lambda: {})
                self.auto_trade_enabled = False
                self.auto_trade_session_armed = False
                self.auto_trade_scan_interval_seconds = 30
                self.auto_trade_budget_mode = "full_margin"
                self.auto_trade_budget_usd = None
                self.daily_start_equity = None

            def set_daily_start_equity(self, value):
                self.daily_start_equity = value

        connector = LiveConnector()
        risk = RiskEngine()
        old_active_platform = routes.active_platform
        old_mt5 = routes.mt5_connector
        old_connector = routes.connector
        old_risk = routes.risk_engine
        old_db = routes.db
        old_apply = routes._apply_active_broker_runtime
        old_ensure_pm = routes._ensure_position_manager_running_if_connected
        old_persist = routes._persist_runtime_state
        old_persist_creds = routes._persist_credentials_if_requested
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        try:
            routes.active_platform = "mt5"
            routes.mt5_connector = connector
            routes.connector = connector
            routes.risk_engine = risk
            routes.db = None
            routes._apply_active_broker_runtime = lambda _platform: None
            routes._ensure_position_manager_running_if_connected = lambda: None

            async def fake_persist():
                return None

            async def fake_creds(**_kwargs):
                raise AssertionError("credential persistence must not run for terminal-session connect")

            routes._persist_runtime_state = fake_persist
            routes._persist_credentials_if_requested = fake_creds
            routes.config.LIVE_TRADING_ENABLED = True

            payload = asyncio.run(routes.connect(routes.ConnectRequest(
                platform="mt5",
                terminal_path="C:/Program Files/MetaTrader 5/terminal64.exe",
            )))
        finally:
            routes.active_platform = old_active_platform
            routes.mt5_connector = old_mt5
            routes.connector = old_connector
            routes.risk_engine = old_risk
            routes.db = old_db
            routes._apply_active_broker_runtime = old_apply
            routes._ensure_position_manager_running_if_connected = old_ensure_pm
            routes._persist_runtime_state = old_persist
            routes._persist_credentials_if_requested = old_persist_creds
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled

        self.assertTrue(payload["connected"])
        self.assertFalse(payload["is_demo"])
        self.assertEqual(risk.daily_start_equity, 123456.78)
        self.assertEqual(payload["credential_status"]["requested"], False)

    def test_connect_ibkr_attaches_to_local_tws_without_credentials(self):
        class Account:
            equity = 50000.0

            def model_dump(self):
                return {"login": 0, "server": "IBKR", "equity": self.equity}

        class IBKR:
            connected = False
            last_error = ""
            account_info = Account()

            def connect(self, params):
                self.params = params
                self.connected = True
                return True

            def is_demo(self):
                return True

        class RiskEngine:
            def __init__(self):
                self.daily_start_equity = None

            def set_daily_start_equity(self, value):
                self.daily_start_equity = value

        ibkr = IBKR()
        risk = RiskEngine()
        old_active_platform = routes.active_platform
        old_ibkr = routes.ibkr_connector
        old_mt5 = routes.mt5_connector
        old_ts = routes.tradestation_connector
        old_risk = routes.risk_engine
        old_run_ibkr = routes._run_ibkr
        old_apply = routes._apply_active_broker_runtime
        old_ensure_pm = routes._ensure_position_manager_running_if_connected
        old_persist = routes._persist_runtime_state
        try:
            routes.active_platform = "mt5"
            routes.ibkr_connector = ibkr
            routes.mt5_connector = SimpleNamespace(connected=False)
            routes.tradestation_connector = SimpleNamespace(connected=False)
            routes.risk_engine = risk

            async def fake_run_ibkr(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            async def fake_persist():
                return None

            routes._run_ibkr = fake_run_ibkr
            routes._apply_active_broker_runtime = lambda _platform: None
            routes._ensure_position_manager_running_if_connected = lambda: None
            routes._persist_runtime_state = fake_persist

            payload = asyncio.run(routes.connect(routes.ConnectRequest(
                platform="ibkr",
                host="127.0.0.1",
                port=7497,
                client_id=8,
                readonly=True,
            )))
        finally:
            routes.active_platform = old_active_platform
            routes.ibkr_connector = old_ibkr
            routes.mt5_connector = old_mt5
            routes.tradestation_connector = old_ts
            routes.risk_engine = old_risk
            routes._run_ibkr = old_run_ibkr
            routes._apply_active_broker_runtime = old_apply
            routes._ensure_position_manager_running_if_connected = old_ensure_pm
            routes._persist_runtime_state = old_persist

        self.assertTrue(payload["connected"])
        self.assertEqual(payload["platform"], "ibkr")
        self.assertEqual(ibkr.params.port, 7497)
        self.assertEqual(ibkr.params.client_id, 8)
        self.assertTrue(ibkr.params.readonly)
        self.assertEqual(risk.daily_start_equity, 50000.0)
        self.assertIn("TWS", payload["credential_status"]["reason"])

    def test_connect_tradestation_returns_broker_authorization_url(self):
        old_open = routes.webbrowser.open
        old_client_id = routes.os.environ.get("TRADESTATION_CLIENT_ID")
        old_states = dict(routes._tradestation_oauth_states)
        opened: list[str] = []
        try:
            routes._tradestation_oauth_states.clear()
            routes.os.environ["TRADESTATION_CLIENT_ID"] = "client-123"
            routes.webbrowser.open = lambda url: opened.append(url) or True
            payload = asyncio.run(routes.connect(routes.ConnectRequest(
                platform="tradestation",
                redirect_uri="http://127.0.0.1:8000/api/tradestation/oauth/callback",
            )))
        finally:
            routes.webbrowser.open = old_open
            if old_client_id is None:
                routes.os.environ.pop("TRADESTATION_CLIENT_ID", None)
            else:
                routes.os.environ["TRADESTATION_CLIENT_ID"] = old_client_id
            routes._tradestation_oauth_states.clear()
            routes._tradestation_oauth_states.update(old_states)

        self.assertFalse(payload["connected"])
        self.assertTrue(payload["auth_required"])
        self.assertEqual(payload["platform"], "tradestation")
        self.assertIn("https://signin.tradestation.com/authorize?", payload["auth_url"])
        self.assertIn("client_id=client-123", payload["auth_url"])
        self.assertEqual(opened, [payload["auth_url"]])

    def test_tradestation_oauth_callback_exchanges_code_and_connects(self):
        class Account:
            equity = 64000.0

            def model_dump(self):
                return {"login": 0, "server": "TradeStation sim", "equity": self.equity}

        class TradeStation:
            connected = False
            last_error = ""
            account_info = Account()

            def connect(self, params):
                self.params = params
                self.connected = True
                return True

        class TokenResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"access_token": "access-123", "refresh_token": "refresh-123"}

        class RiskEngine:
            def __init__(self):
                self.daily_start_equity = None

            def set_daily_start_equity(self, value):
                self.daily_start_equity = value

        opened: list[str] = []
        ts = TradeStation()
        risk = RiskEngine()
        old_open = routes.webbrowser.open
        old_post = routes.requests.post
        old_client_id = routes.os.environ.get("TRADESTATION_CLIENT_ID")
        old_client_secret = routes.os.environ.get("TRADESTATION_CLIENT_SECRET")
        old_states = dict(routes._tradestation_oauth_states)
        old_active_platform = routes.active_platform
        old_ts = routes.tradestation_connector
        old_mt5 = routes.mt5_connector
        old_ibkr = routes.ibkr_connector
        old_risk = routes.risk_engine
        old_run_ibkr = routes._run_ibkr
        old_apply = routes._apply_active_broker_runtime
        old_ensure_pm = routes._ensure_position_manager_running_if_connected
        old_persist = routes._persist_runtime_state
        try:
            routes._tradestation_oauth_states.clear()
            routes.os.environ["TRADESTATION_CLIENT_ID"] = "client-123"
            routes.os.environ["TRADESTATION_CLIENT_SECRET"] = "secret-123"
            routes.webbrowser.open = lambda url: opened.append(url) or True
            routes.requests.post = lambda *args, **kwargs: TokenResponse()
            routes.tradestation_connector = ts
            routes.mt5_connector = SimpleNamespace(connected=False)
            routes.ibkr_connector = SimpleNamespace(connected=False)
            routes.risk_engine = risk

            async def fake_run_ibkr(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            async def fake_persist():
                return None

            routes._run_ibkr = fake_run_ibkr
            routes._apply_active_broker_runtime = lambda _platform: None
            routes._ensure_position_manager_running_if_connected = lambda: None
            routes._persist_runtime_state = fake_persist

            auth_payload = asyncio.run(routes.connect(routes.ConnectRequest(
                platform="tradestation",
                environment="sim",
                redirect_uri="http://127.0.0.1:8000/api/tradestation/oauth/callback",
            )))
            state = parse_qs(urlparse(auth_payload["auth_url"]).query)["state"][0]
            response = asyncio.run(routes.tradestation_oauth_callback(code="code-123", state=state))
        finally:
            routes.webbrowser.open = old_open
            routes.requests.post = old_post
            if old_client_id is None:
                routes.os.environ.pop("TRADESTATION_CLIENT_ID", None)
            else:
                routes.os.environ["TRADESTATION_CLIENT_ID"] = old_client_id
            if old_client_secret is None:
                routes.os.environ.pop("TRADESTATION_CLIENT_SECRET", None)
            else:
                routes.os.environ["TRADESTATION_CLIENT_SECRET"] = old_client_secret
            routes._tradestation_oauth_states.clear()
            routes._tradestation_oauth_states.update(old_states)
            routes.active_platform = old_active_platform
            routes.tradestation_connector = old_ts
            routes.mt5_connector = old_mt5
            routes.ibkr_connector = old_ibkr
            routes.risk_engine = old_risk
            routes._run_ibkr = old_run_ibkr
            routes._apply_active_broker_runtime = old_apply
            routes._ensure_position_manager_running_if_connected = old_ensure_pm
            routes._persist_runtime_state = old_persist

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ts.connected)
        self.assertEqual(ts.params.environment, "sim")
        self.assertEqual(ts.params.access_token, "access-123")
        self.assertEqual(ts.params.refresh_token, "refresh-123")
        self.assertEqual(ts.params.client_id, "client-123")
        self.assertEqual(risk.daily_start_equity, 64000.0)

    def test_legacy_saved_credential_auto_connect_is_disabled(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(routes.auto_connect(manual=True))

        self.assertEqual(cm.exception.status_code, 410)
        self.assertIn("Log in inside the local MT5 terminal", str(cm.exception.detail))

    def test_connect_live_mt5_blocked_disconnects_and_reports_policy(self):
        class LiveConnector:
            connected = False
            account_info = SimpleNamespace(model_dump=lambda: {})
            last_error = ""

            def __init__(self):
                self.disconnected = False

            def connect_terminal_session(self, params):
                self.connected = True
                self.params = params
                return True

            def is_demo(self):
                return False

            def disconnect(self):
                self.disconnected = True
                self.connected = False
                return True

        connector = LiveConnector()
        old_mt5 = routes.mt5_connector
        old_connector = routes.connector
        old_risk = routes.risk_engine
        old_db = routes.db
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        try:
            routes.mt5_connector = connector
            routes.connector = connector
            routes.risk_engine = SimpleNamespace(
                user_policy=SimpleNamespace(demo_only_default=True),
            )
            routes.db = None
            routes.config.LIVE_TRADING_ENABLED = False

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.connect(routes.ConnectRequest(
                    platform="mt5",
                )))
        finally:
            routes.mt5_connector = old_mt5
            routes.connector = old_connector
            routes.risk_engine = old_risk
            routes.db = old_db
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled

        self.assertEqual(cm.exception.status_code, 403)
        self.assertTrue(connector.disconnected)
        self.assertIn("Disable demo-only mode", str(cm.exception.detail))

    def test_execute_trade_live_mt5_blocker_explains_required_live_policy(self):
        class LiveConnector:
            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_require = routes._require_active_platform_connection
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes._require_active_platform_connection = lambda: None
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.execute_trade(routes.ExecuteTradeRequest(
                    symbol="XAUUSD",
                    action="BUY",
                    volume=0.01,
                    stop_loss=1990.0,
                    take_profit=2030.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes._require_active_platform_connection = old_require
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("LIVE_TRADING_ENABLED=true", str(cm.exception.detail))
        self.assertIn("demo-only mode off", str(cm.exception.detail))

    def test_quick_buy_live_mt5_blocker_explains_required_live_policy(self):
        class LiveConnector:
            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_require = routes._require_active_platform_connection
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes._require_active_platform_connection = lambda: None
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.quick_buy(routes.QuickBuyRequest(
                    symbol="XAUUSD",
                    action="BUY",
                    amount_usd=1000.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes._require_active_platform_connection = old_require
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("LIVE_TRADING_ENABLED=true", str(cm.exception.detail))
        self.assertIn("demo-only mode off", str(cm.exception.detail))

    def test_quick_buy_allows_live_mt5_when_policy_allows(self):
        class LiveConnector:
            connected = True

            def is_demo(self):
                return False

        class MarketData:
            def enable_symbol(self, _symbol):
                return True

            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=1_800_000_000, time_msc=1_800_000_000_000)

            def get_symbol_info(self, _symbol):
                return {
                    "trade_contract_size": 100.0,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 10.0,
                    "digits": 2,
                    "point": 0.01,
                    "trade_tick_value": 1.0,
                    "trade_tick_size": 0.01,
                }

        class Execution:
            def __init__(self):
                self.orders = []

            def place_order(self, order_req):
                self.orders.append(order_req)
                return OrderResult(
                    success=True,
                    retcode=10009,
                    retcode_desc="Request completed",
                    ticket=654,
                    volume=order_req.volume,
                    price=2000.0,
                    stop_loss=order_req.stop_loss,
                    take_profit=order_req.take_profit,
                    comment=order_req.comment,
                )

        class ExecutionService:
            async def preflight_for_context_signal(self, **_kwargs):
                return SimpleNamespace(approved=True, reason="ok")

        execution = Execution()
        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_execution = routes.execution
        old_execution_service = routes.execution_service
        old_build_context = routes._build_fast_execution_context
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_solve_volume = routes._solve_volume_by_margin_target_fast
        old_db = routes.db
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.market_data = MarketData()
            routes.execution = execution
            routes.execution_service = ExecutionService()
            routes._build_fast_execution_context = lambda **_kwargs: SimpleNamespace(symbol="XAUUSD")
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes._solve_volume_by_margin_target_fast = lambda **_kwargs: (0.01, 1000.0)
            routes.db = None
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            payload = asyncio.run(routes.quick_buy(routes.QuickBuyRequest(
                symbol="xauusd",
                action="BUY",
                amount_usd=1000.0,
                custom_stop_loss=1990.0,
                custom_take_profit=2030.0,
            )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes.execution = old_execution
            routes.execution_service = old_execution_service
            routes._build_fast_execution_context = old_build_context
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes._solve_volume_by_margin_target_fast = old_solve_volume
            routes.db = old_db
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertTrue(payload["success"])
        self.assertEqual(payload["ticket"], 654)
        self.assertEqual(len(execution.orders), 1)
        self.assertEqual(execution.orders[0].symbol, "XAUUSD")

    def test_quick_buy_rejects_undated_mt5_tick_before_value_calculation(self):
        class LiveConnector:
            connected = True

            def is_demo(self):
                return False

        class MarketData:
            def enable_symbol(self, _symbol):
                return True

            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        class Execution:
            def __init__(self):
                self.orders = []

            def place_order(self, order_req):
                self.orders.append(order_req)
                return OrderResult(success=True, retcode=10009, retcode_desc="Request completed")

        execution = Execution()
        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_execution = routes.execution
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_db = routes.db
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.market_data = MarketData()
            routes.execution = execution
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes.db = None
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(routes.quick_buy(routes.QuickBuyRequest(
                    symbol="xauusd",
                    action="BUY",
                    amount_usd=1000.0,
                    custom_stop_loss=1990.0,
                    custom_take_profit=2030.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes.execution = old_execution
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes.db = old_db
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("No fresh tick data", str(raised.exception.detail))
        self.assertEqual(execution.orders, [])

    def test_calculate_volume_uses_tick_pricing_for_min_sl_tp_dollars(self):
        class Connector:
            def refresh_account(self):
                return SimpleNamespace(leverage=100)

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(ask=10.0, bid=9.99, spread=20, time=1_800_000_000, time_msc=1_800_000_000_000)

            def get_symbol_info(self, _symbol):
                return {
                    "trade_contract_size": 100.0,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 10.0,
                    "digits": 2,
                    "point": 0.01,
                    "trade_stops_level": 10,
                    "trade_tick_value": 1.0,
                    "trade_tick_size": 0.01,
                }

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_solve_volume = routes._solve_volume_by_margin_target
        try:
            routes.active_platform = "mt5"
            routes.connector = Connector()
            routes.market_data = MarketData()
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes._solve_volume_by_margin_target = lambda **_kwargs: (2.0, 25.0)

            payload = asyncio.run(routes.calculate_volume("xauusd", 25.0))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes._solve_volume_by_margin_target = old_solve_volume

        self.assertEqual(payload["symbol"], "XAUUSD")
        self.assertEqual(payload["volume"], 2.0)
        self.assertEqual(payload["trade_tick_value"], 1.0)
        self.assertEqual(payload["trade_tick_size"], 0.01)
        self.assertEqual(payload["point"], 0.01)
        self.assertEqual(payload["min_sl_tp_dollars"], 60.0)

    def test_calculate_volume_rejects_undated_mt5_tick_before_value_preview(self):
        class Connector:
            def refresh_account(self):
                return SimpleNamespace(leverage=100)

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(ask=10.0, bid=9.99, spread=20, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        try:
            routes.active_platform = "mt5"
            routes.connector = Connector()
            routes.market_data = MarketData()
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(routes.calculate_volume("xauusd", 25.0))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("No fresh tick data", str(raised.exception.detail))

    def test_execute_trade_allows_live_mt5_when_policy_allows(self):
        class LiveConnector:
            def is_demo(self):
                return False

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=1_800_000_000, time_msc=1_800_000_000_000)

        class ExecutionService:
            def __init__(self):
                self.orders = []

            def place_order_if_approved(self, order_req, preflight):
                self.orders.append((order_req, preflight))
                return OrderResult(
                    success=True,
                    retcode=10009,
                    retcode_desc="Request completed",
                    ticket=321,
                    volume=order_req.volume,
                    price=2000.0,
                    stop_loss=order_req.stop_loss,
                    take_profit=order_req.take_profit,
                    comment=order_req.comment,
                )

        execution_service = ExecutionService()
        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_execution_service = routes.execution_service
        old_preflight = routes._pre_execution_checks
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_db = routes.db
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.market_data = MarketData()
            routes.execution_service = execution_service
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes.db = None
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            async def fake_preflight(**_kwargs):
                return {"preflight": SimpleNamespace(approved=True, reason="ok")}

            routes._pre_execution_checks = fake_preflight

            payload = asyncio.run(routes.execute_trade(routes.ExecuteTradeRequest(
                symbol="xauusd",
                action="BUY",
                volume=0.01,
                stop_loss=1990.0,
                take_profit=2030.0,
            )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes.execution_service = old_execution_service
            routes._pre_execution_checks = old_preflight
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes.db = old_db
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertTrue(payload["success"])
        self.assertEqual(payload["ticket"], 321)
        self.assertEqual(len(execution_service.orders), 1)
        self.assertEqual(execution_service.orders[0][0].symbol, "XAUUSD")

    def test_execute_trade_rejects_undated_mt5_tick_before_order_preflight(self):
        class LiveConnector:
            def is_demo(self):
                return False

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=0, time_msc=0)

        class ExecutionService:
            def __init__(self):
                self.orders = []

            def place_order_if_approved(self, order_req, preflight):
                self.orders.append((order_req, preflight))
                return OrderResult(success=True, retcode=10009, retcode_desc="Request completed")

        execution_service = ExecutionService()
        preflight_called = False
        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_market_data = routes.market_data
        old_execution_service = routes.execution_service
        old_preflight = routes._pre_execution_checks
        old_resolve = routes._resolve_market_symbol
        old_require = routes._require_active_platform_connection
        old_db = routes.db
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.market_data = MarketData()
            routes.execution_service = execution_service
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._require_active_platform_connection = lambda: None
            routes.db = None
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            async def fake_preflight(**_kwargs):
                nonlocal preflight_called
                preflight_called = True
                return {"preflight": SimpleNamespace(approved=True, reason="ok")}

            routes._pre_execution_checks = fake_preflight

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(routes.execute_trade(routes.ExecuteTradeRequest(
                    symbol="xauusd",
                    action="BUY",
                    volume=0.01,
                    stop_loss=1990.0,
                    take_profit=2030.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.market_data = old_market_data
            routes.execution_service = old_execution_service
            routes._pre_execution_checks = old_preflight
            routes._resolve_market_symbol = old_resolve
            routes._require_active_platform_connection = old_require
            routes.db = old_db
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("No fresh tick data", str(raised.exception.detail))
        self.assertFalse(preflight_called)
        self.assertEqual(execution_service.orders, [])

    def test_execute_recommendation_live_mt5_blocker_explains_required_live_policy(self):
        class LiveConnector:
            connected = True

            def trade_server_connected(self):
                return True

            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.execute_recommendation(routes.ExecuteRecommendationRequest(
                    signal_id=123,
                    amount_usd=1000.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("LIVE_TRADING_ENABLED=true", str(cm.exception.detail))
        self.assertIn("demo-only mode off", str(cm.exception.detail))

    def test_execute_recommendation_rejects_undated_mt5_tick_before_context_build(self):
        class LiveConnector:
            connected = True
            account_info = SimpleNamespace(equity=10_000.0, free_margin=10_000.0)

            def trade_server_connected(self):
                return True

            def is_demo(self):
                return False

        class DB:
            async def get_signal_by_id(self, _signal_id):
                return {
                    "symbol": "XAUUSD",
                    "action": "BUY",
                    "agent_name": "SmartAgent",
                    "confidence": 0.9,
                    "stop_loss": 1990.0,
                    "take_profit": 2030.0,
                    "reason": "test",
                    "timeframe": "H1",
                }

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_require = routes._require_active_platform_connection
        old_db = routes.db
        old_market_data = routes.market_data
        old_panic = routes.risk_engine._panic_stopped if hasattr(routes.risk_engine, "_panic_stopped") else None
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes._require_active_platform_connection = lambda: None
            routes.db = DB()
            routes.market_data = MarketData()
            routes.config.LIVE_TRADING_ENABLED = True
            routes.risk_engine.user_policy.demo_only_default = False
            if hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = False

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.execute_recommendation(routes.ExecuteRecommendationRequest(
                    signal_id=123,
                    amount_usd=1000.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes._require_active_platform_connection = old_require
            routes.db = old_db
            routes.market_data = old_market_data
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only
            if old_panic is not None and hasattr(routes.risk_engine, "_panic_stopped"):
                routes.risk_engine._panic_stopped = old_panic

        self.assertEqual(cm.exception.status_code, 404)
        self.assertIn("No fresh tick data", str(cm.exception.detail))

    def test_modify_position_rejects_undated_tick_when_entry_price_missing(self):
        class Position:
            ticket = 123
            symbol = "XAUUSD"
            type = "BUY"
            price_open = 0.0

        class Execution:
            def __init__(self):
                self.modified = []

            def get_positions(self):
                return [Position()]

            def modify_position(self, ticket, stop_loss, take_profit):
                self.modified.append((ticket, stop_loss, take_profit))
                return OrderResult(
                    success=True,
                    retcode=10009,
                    retcode_desc="Request completed",
                    ticket=ticket,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )

        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        execution = Execution()
        old_active_platform = routes.active_platform
        old_execution = routes.execution
        old_market_data = routes.market_data
        old_require = routes._require_active_platform_connection
        old_db = routes.db
        try:
            routes.active_platform = "mt5"
            routes.execution = execution
            routes.market_data = MarketData()
            routes._require_active_platform_connection = lambda: None
            routes.db = None

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.modify_position(routes.ModifyPositionRequest(
                    ticket=123,
                    stop_loss=1990.0,
                    take_profit=2030.0,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.execution = old_execution
            routes.market_data = old_market_data
            routes._require_active_platform_connection = old_require
            routes.db = old_db

        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("Cannot validate targets without a fresh live/reference entry price.", str(cm.exception.detail))
        self.assertEqual(execution.modified, [])

    def test_chat_trade_preview_rejects_undated_mt5_tick_values(self):
        class MarketData:
            def get_tick(self, _symbol):
                return SimpleNamespace(bid=1999.0, ask=2000.0, spread=0.1, time=0, time_msc=0)

            def get_symbol_info(self, _symbol):
                raise AssertionError("symbol info should not be read for stale ticks")

        old_active_platform = routes.active_platform
        old_market_data = routes.market_data
        old_connected = routes._active_platform_connected
        old_resolve = routes._resolve_market_symbol
        old_fallback = routes._simple_trade_intent_fallback
        old_save = routes._save_chat_history
        old_gemini_agent = routes.gemini_agent
        try:
            routes.active_platform = "mt5"
            routes.market_data = MarketData()
            routes._active_platform_connected = lambda: True
            routes._resolve_market_symbol = lambda symbol: str(symbol).upper()
            routes._simple_trade_intent_fallback = lambda _message: {
                "reply": "Previewing trade.",
                "intent": "trade_request",
                "trade": {
                    "symbol": "XAUUSD",
                    "action": "BUY",
                    "amount_usd": 1000.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "reason": "test",
                },
            }

            async def fake_save(_messages):
                return None

            routes._save_chat_history = fake_save
            routes.gemini_agent = SimpleNamespace(available=False, _client=None)

            payload = asyncio.run(routes.chat_message(routes.ChatRequest(message="buy xauusd for 1000")))
        finally:
            routes.active_platform = old_active_platform
            routes.market_data = old_market_data
            routes._active_platform_connected = old_connected
            routes._resolve_market_symbol = old_resolve
            routes._simple_trade_intent_fallback = old_fallback
            routes._save_chat_history = old_save
            routes.gemini_agent = old_gemini_agent

        self.assertEqual(payload["intent"], "trade_request")
        self.assertIsNone(payload["trade_preview"])
        self.assertIn("no fresh MT5 quote", payload["reply"])

    def test_smart_evaluate_auto_execute_blocks_live_mt5_when_policy_disallows(self):
        class LiveConnector:
            def is_demo(self):
                return False

        old_active_platform = routes.active_platform
        old_connector = routes.connector
        old_require = routes._require_active_platform_connection
        old_live_enabled = routes.config.LIVE_TRADING_ENABLED
        old_demo_only = routes.risk_engine.user_policy.demo_only_default
        try:
            routes.active_platform = "mt5"
            routes.connector = LiveConnector()
            routes._require_active_platform_connection = lambda: None
            routes.config.LIVE_TRADING_ENABLED = False
            routes.risk_engine.user_policy.demo_only_default = True

            with self.assertRaises(HTTPException) as cm:
                asyncio.run(routes.smart_evaluate(routes.SmartEvaluateRequest(
                    symbols=["XAUUSD"],
                    manual=False,
                    auto_execute=True,
                )))
        finally:
            routes.active_platform = old_active_platform
            routes.connector = old_connector
            routes._require_active_platform_connection = old_require
            routes.config.LIVE_TRADING_ENABLED = old_live_enabled
            routes.risk_engine.user_policy.demo_only_default = old_demo_only

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("LIVE_TRADING_ENABLED=true", str(cm.exception.detail))
        self.assertIn("demo-only mode off", str(cm.exception.detail))

    def test_route_auto_execution_caps_cfd_targets_and_comment_amounts(self):
        original_mode = risk_engine.user_policy.mode
        original_rr = risk_engine.user_policy.min_reward_risk
        original_risk = risk_engine.user_policy.max_risk_per_trade
        risk_engine.user_policy.mode = "aggressive"
        risk_engine.user_policy.min_reward_risk = 1.8
        risk_engine.user_policy.max_risk_per_trade = 0.75
        info = {
            "digits": 3,
            "point": 0.001,
            "trade_contract_size": 5000,
            "trade_tick_value": 0.5,
            "trade_tick_size": 0.001,
        }

        try:
            sl, tp, sl_amount, tp_amount = _cap_order_targets_by_money_budget(
                action="SELL",
                entry_price=74.786,
                stop_loss=76.161,
                take_profit=71.921,
                symbol_info=info,
                volume=1.21,
                started_amount_usd=4525.0,
                account=SimpleNamespace(equity=113220.63),
            )
        finally:
            risk_engine.user_policy.mode = original_mode
            risk_engine.user_policy.min_reward_risk = original_rr
            risk_engine.user_policy.max_risk_per_trade = original_risk

        self.assertEqual(sl, 74.921)
        self.assertEqual(tp, 74.487)
        self.assertLessEqual(sl_amount, 4525.0 * 0.18)
        self.assertGreaterEqual(tp_amount / sl_amount, 1.8)
        self.assertEqual(_build_trade_comment(4525, sl_amount, tp_amount), "TA4525|SLA814|TPA1810")

    def test_history_overlay_prefers_final_closed_targets_over_stale_order_targets(self):
        trade = {
            "symbol": "XAGUSD",
            "action": "SELL",
            "entry_price": 74.786,
            "volume": 1.21,
            "stop_loss": 76.161,
            "take_profit": 71.921,
            "started_with_usd": 4525.0,
            "sl_amount_usd": 8318.75,
            "tp_amount_usd": 17333.25,
        }
        context = {
            "outcome": {
                "profit": -1131.35,
                "outcome_json": {
                    "stop_loss": 74.973,
                    "take_profit": 74.37,
                    "close_price": 74.973,
                }
            }
        }

        self.assertTrue(_apply_final_managed_targets_to_trade(trade, context))

        self.assertEqual(trade["stop_loss"], 74.973)
        self.assertEqual(trade["take_profit"], 74.37)
        self.assertAlmostEqual(trade["sl_amount_usd"], 1131.35, places=2)
        self.assertAlmostEqual(trade["tp_amount_usd"], 2516.80, places=2)
        self.assertEqual(trade["target_source"], "closed_outcome")

    def test_unmanaged_broker_history_does_not_show_app_target_metrics(self):
        trade = {
            "symbol": "XAGUSD",
            "origin": "broker_imported",
            "entry_price": 74.858,
            "stop_loss": 76.3,
            "take_profit": 72.117,
            "started_with_usd": 3480.9,
            "sl_amount_usd": 6705.3,
            "tp_amount_usd": 12745.65,
            "sl_pct_of_start": 192.6,
            "tp_pct_of_start": 366.2,
        }

        self.assertTrue(_sanitize_unmanaged_broker_target_metrics(trade))

        self.assertIsNone(trade["sl_amount_usd"])
        self.assertIsNone(trade["tp_amount_usd"])
        self.assertIsNone(trade["sl_pct_of_start"])
        self.assertIsNone(trade["tp_pct_of_start"])
        self.assertEqual(trade["origin"], "broker_imported")
        self.assertEqual(trade["target_source"], "broker_imported_unmanaged")

    def test_app_opened_history_prefers_order_comment_amounts(self):
        trade = {
            "symbol": "XAGUSD",
            "origin": "app_opened",
            "entry_price": 74.858,
            "stop_loss": 74.973,
            "take_profit": 74.37,
            "started_with_usd": 8600.0,
            "started_with_source": "estimated",
            "sl_amount_usd": 1131.35,
            "tp_amount_usd": 2516.80,
            "sl_pct_of_start": 13.15,
            "tp_pct_of_start": 29.26,
        }
        context = {"order": {"comment": "TA5128|SLA843|TPA2048"}}

        self.assertTrue(_apply_order_comment_target_amounts_to_trade(trade, context))

        self.assertEqual(trade["started_with_usd"], 5128.0)
        self.assertEqual(trade["started_with_source"], "order_comment")
        self.assertEqual(trade["sl_amount_usd"], 843.0)
        self.assertEqual(trade["tp_amount_usd"], 2048.0)
        self.assertAlmostEqual(trade["sl_pct_of_start"], 16.44, places=2)
        self.assertAlmostEqual(trade["tp_pct_of_start"], 39.94, places=2)

    def test_truncated_order_comment_preserves_missing_tp_amount(self):
        trade = {
            "symbol": "XAGUSD",
            "origin": "app_opened",
            "started_with_usd": 7000.0,
            "started_with_source": "estimated",
            "sl_amount_usd": 900.0,
            "tp_amount_usd": 2100.0,
            "tp_pct_of_start": 30.0,
        }
        context = {"order": {"comment": "TA5128_SLA843_TP"}}

        self.assertTrue(_apply_order_comment_target_amounts_to_trade(trade, context))

        self.assertEqual(trade["started_with_usd"], 5128.0)
        self.assertEqual(trade["sl_amount_usd"], 843.0)
        self.assertEqual(trade["tp_amount_usd"], 2100.0)
        self.assertAlmostEqual(trade["sl_pct_of_start"], 16.44, places=2)
        self.assertEqual(trade["tp_pct_of_start"], 30.0)

    def test_legacy_app_comment_amounts_are_capped_for_history_display(self):
        original_mode = risk_engine.user_policy.mode
        original_rr = risk_engine.user_policy.min_reward_risk
        risk_engine.user_policy.mode = "aggressive"
        risk_engine.user_policy.min_reward_risk = 1.8
        trade = {
            "symbol": "XAUUSD",
            "origin": "app_opened",
            "started_with_usd": 944.0,
            "started_with_source": "estimated",
            "sl_amount_usd": 821.0,
            "tp_amount_usd": 1478.0,
        }
        context = {"order": {"comment": "TA944|SLA821|TPA1478"}}

        try:
            self.assertTrue(_apply_order_comment_target_amounts_to_trade(trade, context))
        finally:
            risk_engine.user_policy.mode = original_mode
            risk_engine.user_policy.min_reward_risk = original_rr

        self.assertEqual(trade["started_with_usd"], 944.0)
        self.assertAlmostEqual(trade["sl_amount_usd"], 169.92, places=2)
        self.assertAlmostEqual(trade["tp_amount_usd"], 377.6, places=2)
        self.assertAlmostEqual(trade["sl_pct_of_start"], 18.0, places=2)
        self.assertAlmostEqual(trade["tp_pct_of_start"], 40.0, places=2)
        self.assertEqual(trade["target_amount_source"], "order_comment_capped")
        self.assertIn("Legacy app order comment", trade["target_warning"])

    def test_app_opened_legacy_closed_outcome_amounts_are_capped_for_display(self):
        trade = {
            "symbol": "XAGUSD",
            "origin": "app_opened",
            "started_with_usd": 4535.87,
            "sl_amount_usd": 1131.35,
            "tp_amount_usd": 2516.8,
            "sl_pct_of_start": 24.94,
            "tp_pct_of_start": 55.48,
            "target_source": "closed_outcome",
            "profit_usd": -1131.35,
        }

        self.assertTrue(_sanitize_app_opened_legacy_target_metrics(trade))

        self.assertAlmostEqual(trade["sl_amount_usd"], 816.46, places=2)
        self.assertAlmostEqual(trade["tp_amount_usd"], 1814.35, places=2)
        self.assertEqual(trade["sl_pct_of_start"], 18.0)
        self.assertEqual(trade["tp_pct_of_start"], 40.0)
        self.assertEqual(trade["profit_usd"], -1131.35)
        self.assertEqual(trade["target_amount_source"], "legacy_target_capped")
        self.assertIn("realized P/L remains unchanged", trade["target_warning"])

    def test_live_trace_clears_stale_rr_warning_when_targets_are_valid(self):
        original_rr = risk_engine.user_policy.min_reward_risk
        risk_engine.user_policy.min_reward_risk = 1.8
        item = {
            "price_open": 74.91,
            "stop_loss": 74.81,
            "take_profit": 75.09,
            "execution_trace": {
                "reason": "Request completed | Risk warning: Reward:risk fell to 1.37, below the required 1.80.",
            },
        }

        try:
            self.assertTrue(_sanitize_live_execution_trace_target_warnings(item))
        finally:
            risk_engine.user_policy.min_reward_risk = original_rr

        self.assertEqual(item["execution_trace"]["reason"], "Request completed")
        self.assertTrue(item["execution_trace"]["target_warning_cleared"])
        self.assertAlmostEqual(item["execution_trace"]["live_reward_risk"], 1.8, places=2)


if __name__ == "__main__":
    unittest.main()
