from __future__ import annotations

import unittest
import time
from unittest.mock import patch

from backend.scripts import verify_runtime_readiness as readiness


def _api_keys_health() -> dict:
    return {
        "overall_status": "healthy",
        "unhealthy_count": 0,
        "items": [
            {"id": "gemini", "name": "Gemini", "configured": True, "available": True, "status": "healthy"},
            {"id": "benzinga", "name": "Benzinga", "configured": True, "available": True, "status": "healthy"},
            {"id": "supabase_anon", "name": "Supabase Anon", "configured": True, "available": True, "status": "healthy"},
            {
                "id": "supabase_service_role",
                "name": "Supabase Service Role",
                "configured": True,
                "available": True,
                "status": "healthy",
            },
        ],
    }


def _api_keys_health_without_benzinga() -> dict:
    snapshot = _api_keys_health()
    snapshot["items"] = [
        item for item in snapshot["items"]
        if item.get("id") != "benzinga"
    ]
    return snapshot


def _api_keys_health_warning() -> dict:
    return {
        "overall_status": "warning",
        "unhealthy_count": 0,
        "warning_count": 1,
        "items": [
            {
                "id": "gemini",
                "name": "Gemini",
                "configured": True,
                "available": True,
                "status": "warning",
                "reason": "Gemini API key is configured, but no recent successful Gemini response has verified live availability yet.",
            },
            {"id": "benzinga", "name": "Benzinga", "configured": True, "available": True, "status": "healthy"},
            {"id": "supabase_anon", "name": "Supabase Anon", "configured": True, "available": True, "status": "healthy"},
            {
                "id": "supabase_service_role",
                "name": "Supabase Service Role",
                "configured": True,
                "available": True,
                "status": "healthy",
            },
        ],
    }


def _status_payload(*, connected: bool = False, platform: str = "mt5") -> dict:
    is_live_mt5 = connected and platform == "mt5"
    return {
        "connected": connected,
        "platform": platform,
        "platforms_supported": ["mt5", "ibkr", "tradestation"] if platform == "mt5" else [platform, "mt5"],
        "account": {"server": "MetaQuotes-Live"} if connected else None,
        "is_demo": False,
        "live_trading_enabled": True,
        "live_trading_readiness": {
            "status": "ready" if connected else "blocked",
            "can_execute_live_orders": connected,
            "blockers": [] if connected else ["Connect an MT5 live trading account."],
            "warnings": [],
            "checks": {
                "platform": platform,
                "account_is_demo": False,
                "demo_only_default": not is_live_mt5,
                "live_trading_enabled": True,
            },
        },
        "publish_readiness": {
            "status": "ready" if connected else "blocked",
            "score": 10.0 if connected else 8.5,
            "blockers": [] if connected else ["Trading account is not connected."],
            "warnings": [],
        },
        "api_keys_health": _api_keys_health(),
        "user_policy": {"demo_only_default": not is_live_mt5},
        "runtime_controls": {
            "auto_trade_enabled": False,
            "auto_trade_session_armed": False,
            "auto_trade_scan_interval_seconds": 30,
            "auto_trade_budget_mode": "full_margin",
            "auto_trade_budget_usd": None,
            "panic_stop": False,
        },
        "gemini_available": True,
        "gemini_state": "ready",
        "gemini_primary_model": "google/gemma-4-31b-it",
        "gemini_active_model": "google/gemma-4-31b-it",
        "gemini_using_fallback_model": False,
        "gemini_credits_pct": 100,
        "universe": {
            "active_asset_classes": ["Forex", "Indices", "Commodities", "Stocks", "Crypto"],
        },
        "services": {
            "position_manager": {"desired_running": connected, "running": connected, "last_error": None},
            "auto_trader": {"desired_running": False, "running": False, "last_error": None},
        },
        "cloud_decision": {"enabled": True, "configured": True},
    }


def _demo_status_payload() -> dict:
    status = _status_payload(connected=True)
    status["account"] = {"server": "MetaQuotes-Demo"}
    status["is_demo"] = True
    status["user_policy"] = {"demo_only_default": True}
    status["live_trading_readiness"] = {
        **status["live_trading_readiness"],
        "status": "blocked",
        "can_execute_live_orders": False,
        "blockers": ["Connected MT5 account is demo."],
        "checks": {
            **status["live_trading_readiness"]["checks"],
            "account_is_demo": True,
            "demo_only_default": True,
        },
    }
    return status


def _research_payload(*, active: bool = True) -> dict:
    return {
        "enabled": True,
        "meta_model_active": active,
        "meta_model_active_version": "approved-v1" if active else "",
        "meta_model_inactive_reason": "" if active else "no_approved_model",
        "incremental_training": {"local_closed_count": 100},
    }


def _fresh_tick_payload() -> dict:
    now = int(time.time())
    return {"bid": 1.1, "ask": 1.1002, "time": now, "time_msc": now * 1000}


def _auto_trade_status_payload(**overrides) -> dict:
    payload = {
        "platform": "mt5",
        "platforms_supported": ["mt5"],
        "running": False,
        "runtime_controls": {"auto_trade_enabled": False},
    }
    payload.update(overrides)
    return payload


def _disconnected_top_markets_payload() -> dict:
    categories = {
        "Forex": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD"],
        "Indices": ["US500", "US100", "US30", "GER40", "UK100"],
        "Commodities": ["XAUUSD", "XAGUSD", "WTI", "BRENT"],
        "Stocks": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
        "Crypto": ["BTCUSD", "ETHUSD"],
    }
    symbols = [
        {"name": symbol, "category": category}
        for category, rows in categories.items()
        for symbol in rows
    ]
    return {
        "total": len(symbols),
        "symbols": symbols,
        "category_counts": {category: len(rows) for category, rows in categories.items()},
        "source": "mt5_disconnected_top_markets",
    }


def _connected_mt5_markets_payload() -> dict:
    return {
        "total": 10,
        "symbols": [{"name": "EURUSD"}, {"name": "XAUUSD"}],
        "category_counts": {"Forex": 2, "Indices": 2, "Commodities": 2, "Stocks": 2, "Crypto": 2},
        "source": "mt5_broker_symbols",
    }


class RuntimeReadinessVerifierTest(unittest.TestCase):
    def _run_with_payloads(
        self,
        payloads: dict[str, dict],
        *,
        expect_live_ready: bool = False,
        expect_demo_rehearsal: bool = False,
    ):
        original_json_get = readiness._json_get
        original_json_any_get = readiness._json_any_get
        defaults = {
            "/positions": [],
            "/trade-history": {"trades": [], "source": "test"},
            "/auto-trade/activity": {"live_activity": [], "db_activity": []},
            "/ibkr/debug/account-summary": RuntimeError("GET http://test/api/ibkr/debug/account-summary failed: HTTP 404"),
            "/internal/api-keys-health/cron": {
                "ok": True,
                "overall_status": "healthy",
                "unhealthy_count": 0,
                "gemini_live_verification_ok": True,
            },
        }

        def fake_json_get(_api_base: str, path: str, **_kwargs):
            if path not in payloads and path not in defaults:
                raise AssertionError(f"unexpected endpoint {path}")
            value = payloads.get(path, defaults.get(path))
            if isinstance(value, Exception):
                raise value
            if not isinstance(value, dict):
                raise AssertionError(f"{path} should be fetched as non-object payload")
            return value

        def fake_json_any_get(_api_base: str, path: str, **_kwargs):
            if path not in payloads and path not in defaults:
                raise AssertionError(f"unexpected endpoint {path}")
            value = payloads.get(path, defaults.get(path))
            if isinstance(value, Exception):
                raise value
            return value

        readiness._json_get = fake_json_get
        readiness._json_any_get = fake_json_any_get
        try:
            return readiness.build_report(
                "http://test/api",
                expect_live_ready=expect_live_ready,
                expect_demo_rehearsal=expect_demo_rehearsal,
            )
        finally:
            readiness._json_get = original_json_get
            readiness._json_any_get = original_json_any_get

    def test_disconnected_mt5_with_all_required_runtime_shape_passes_diagnostic(self):
        report, errors, warnings = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(report["connection"]["platform"], "mt5")
        self.assertEqual(report["connection"]["platforms_supported"], ["mt5", "ibkr", "tradestation"])

    def test_rejects_reachable_legacy_broker_debug_route(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
                "/ibkr/debug/account-summary": {"NetLiquidation": "1000"},
            }
        )

        self.assertIn("Legacy broker route /ibkr/debug/account-summary must be unavailable in MT5-only runtime.", errors)

    def test_rejects_legacy_broker_trade_history_source(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
                "/trade-history": {"trades": [], "source": "ibkr_broker_executions"},
            }
        )

        self.assertIn("Trade history source must be MT5-only, got ibkr_broker_executions.", errors)

    def test_rejects_non_mt5_runtime_contract(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False, platform="ibkr"),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("/status platform must be mt5.", errors)
        self.assertIn("/status platforms_supported must be exactly ['mt5', 'ibkr', 'tradestation'].", errors)
        self.assertIn("live_trading_readiness checks must report MT5.", errors)

    def test_rejects_non_mt5_auto_trade_status_contract(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": {
                    "platform": "ibkr",
                    "platforms_supported": ["ibkr", "mt5"],
                    "running": False,
                    "runtime_controls": {"auto_trade_enabled": False},
                },
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("/auto-trade/status platform must be mt5.", errors)
        self.assertIn("/auto-trade/status platforms_supported must be exactly ['mt5'].", errors)

    def test_connected_only_commodities_catalog_and_empty_ticks_fail(self):
        market_symbols = [{"name": f"GOLD{i}"} for i in range(6)]
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": {
                    "total": 6,
                    "symbols": market_symbols,
                    "category_counts": {"Commodities": 6},
                    "source": "mt5",
                },
                "/market/ticks": {"ticks": {}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        joined = "\n".join(errors)
        self.assertIn("Connected MT5 market catalog is missing classes", joined)
        self.assertIn("Connected MT5 runtime returned no live ticks for available symbols.", errors)

    def test_disconnected_market_catalog_requires_broad_top_market_preview(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": {
                    "total": 6,
                    "symbols": [{"name": f"GOLD{i}"} for i in range(6)],
                    "category_counts": {"Commodities": 6},
                    "source": "mt5_disconnected_top_markets",
                },
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        joined = "\n".join(errors)
        self.assertIn("Disconnected MT5 top-market catalog is missing classes", joined)
        self.assertIn("Disconnected MT5 top-market catalog returned too few symbols.", errors)

    def test_disconnected_market_catalog_validates_actual_symbol_classes(self):
        markets = _disconnected_top_markets_payload()
        markets["symbols"] = [
            {"name": f"XAUUSD{i}", "category": "Commodities"}
            for i in range(markets["total"])
        ]
        markets["category_counts"] = {"Forex": 6, "Indices": 5, "Commodities": 4, "Stocks": 5, "Crypto": 2}

        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": markets,
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        joined = "\n".join(errors)
        self.assertIn("Disconnected MT5 top-market catalog is missing classes", joined)

    def test_disconnected_market_catalog_requires_mt5_top_market_source(self):
        markets = _disconnected_top_markets_payload()
        markets["source"] = "ibkr_broker_contracts"

        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": markets,
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("Disconnected MT5 top-market catalog source must be mt5_disconnected_top_markets.", errors)

    def test_connected_catalog_with_stale_or_undated_ticks_fails(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": {
                    "total": 10,
                    "symbols": [{"name": "EURUSD"}, {"name": "XAUUSD"}],
                    "category_counts": {"Forex": 2, "Indices": 2, "Commodities": 2, "Stocks": 2, "Crypto": 2},
                },
                "/market/ticks": {
                    "ticks": {
                        "EURUSD": {"bid": 1.1, "ask": 1.1002, "time": 0, "time_msc": 0},
                        "XAUUSD": {"bid": 2400.0, "ask": 2400.2, "time": int(time.time()) - 3600},
                    }
                },
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("Connected MT5 runtime returned no fresh live ticks for sampled symbols.", errors)

    def test_connected_catalog_rejects_legacy_broker_tick_source(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {
                    "source": "ibkr_broker_snapshot",
                    "ticks": {"EURUSD": _fresh_tick_payload()},
                },
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("Connected MT5 tick source must be mt5_broker_tick, got ibkr_broker_snapshot.", errors)

    def test_connected_catalog_rejects_legacy_broker_market_source(self):
        markets = _connected_mt5_markets_payload()
        markets["source"] = "ibkr_broker_contracts"

        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": markets,
                "/market/ticks": {
                    "source": "mt5_broker_tick",
                    "ticks": {"EURUSD": _fresh_tick_payload()},
                },
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn(
            "Connected MT5 market catalog source must be mt5_broker_symbols, got ibkr_broker_contracts.",
            errors,
        )

    def test_strict_live_ready_rejects_demo_account_even_if_status_claims_ready(self):
        status = _status_payload(connected=True)
        status["is_demo"] = True
        status["live_trading_readiness"] = {
            **status["live_trading_readiness"],
            "status": "ready",
            "can_execute_live_orders": True,
            "blockers": [],
            "checks": {
                **status["live_trading_readiness"]["checks"],
                "account_is_demo": True,
            },
        }

        _, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            },
            expect_live_ready=True,
        )

        self.assertIn("Strict live readiness cannot use a demo MT5 account.", errors)

    def test_demo_rehearsal_accepts_connected_demo_with_live_mt5_ticks(self):
        report, errors, warnings = self._run_with_payloads(
            {
                "/status": _demo_status_payload(),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            },
            expect_demo_rehearsal=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertTrue(report["demo_rehearsal_readiness"]["ready"])
        self.assertTrue(report["demo_rehearsal_readiness"]["account_is_demo"])
        self.assertFalse(report["live_trading_readiness"]["can_execute_live_orders"])

    def test_demo_rehearsal_requires_demo_account_not_live_account(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            },
            expect_demo_rehearsal=True,
        )

        self.assertIn("Demo rehearsal requires a connected MT5 demo account.", errors)

    def test_strict_live_ready_requires_active_meta_model_version(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=False),
            },
            expect_live_ready=True,
        )

        self.assertIn("Meta-model is not active with an approved version.", errors)

    def test_strict_live_ready_requires_fully_verified_api_health(self):
        status = _status_payload(connected=True)
        status["api_keys_health"] = _api_keys_health_warning()
        _, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            },
            expect_live_ready=True,
        )

        joined = "\n".join(errors)
        self.assertIn("API/provider health is not fully verified: warning.", joined)
        self.assertIn("API/provider blocker: Gemini status=warning", joined)

    def test_requires_benzinga_provider_health(self):
        status = _status_payload(connected=False)
        status["api_keys_health"] = _api_keys_health_without_benzinga()
        _, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": {},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertIn("api_keys_health is missing required keys: benzinga.", errors)

    def test_strict_live_ready_requires_cloud_decision_ready(self):
        status = _status_payload(connected=True)
        status["cloud_decision"] = {
            "enabled": False,
            "configured": False,
            "disabled_reason": "configured endpoint returned HTTP 404",
        }
        _, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            },
            expect_live_ready=True,
        )

        self.assertIn(
            "Cloud decision service is not live-ready: configured endpoint returned HTTP 404",
            errors,
        )

    def test_strict_live_ready_attempts_api_health_refresh_before_status(self):
        calls: list[str] = []
        original_json_get = readiness._json_get
        original_json_any_get = readiness._json_any_get

        def fake_json_get(_api_base: str, path: str, **_kwargs):
            calls.append(path)
            payloads = {
                "/internal/api-keys-health/cron": {
                    "ok": True,
                    "overall_status": "healthy",
                    "unhealthy_count": 0,
                    "gemini_live_verification_ok": True,
                },
                "/status": _status_payload(connected=True),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
                "/trade-history": {"trades": [], "source": "test"},
                "/auto-trade/activity": {"live_activity": [], "db_activity": []},
            }
            if path not in payloads:
                raise AssertionError(f"unexpected endpoint {path}")
            return payloads[path]

        def fake_json_any_get(_api_base: str, path: str, **_kwargs):
            defaults = {
                "/positions": [],
                "/trade-history": {"trades": [], "source": "test"},
                "/auto-trade/activity": {"live_activity": [], "db_activity": []},
            }
            if path not in defaults:
                raise AssertionError(f"unexpected endpoint {path}")
            return defaults[path]

        readiness._json_get = fake_json_get
        readiness._json_any_get = fake_json_any_get
        try:
            report, errors, warnings = readiness.build_report("http://test/api", expect_live_ready=True)
        finally:
            readiness._json_get = original_json_get
            readiness._json_any_get = original_json_any_get

        self.assertEqual(warnings, [])
        self.assertNotIn("API/provider health is not fully verified", "\n".join(errors))
        self.assertLess(calls.index("/internal/api-keys-health/cron"), calls.index("/status"))
        self.assertTrue(report["api_keys_health"]["strict_refresh"]["attempted"])
        self.assertTrue(report["api_keys_health"]["strict_refresh"]["gemini_live_verification_ok"])

    def test_value_integrity_flags_bad_trade_values(self):
        _, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=True),
                "/market/available-symbols": _connected_mt5_markets_payload(),
                "/market/ticks": {"source": "mt5_broker_tick", "ticks": {"EURUSD": _fresh_tick_payload()}},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
                "/positions": [
                    {
                        "symbol": "EURUSD",
                        "action": "SELL",
                        "price_open": 1.1000,
                        "stop_loss": 1.0900,
                        "take_profit": 1.1200,
                        "started_with_usd": 100.0,
                        "sl_amount_usd": 150.0,
                    }
                ],
            }
        )

        joined = "\n".join(errors)
        self.assertIn("positions[0] SELL targets are on the wrong side of entry.", joined)
        self.assertIn("positions[0].sl_amount_usd exceeds started_with_usd.", joined)

    def test_value_integrity_reports_conversion_self_checks(self):
        report, errors, _ = self._run_with_payloads(
            {
                "/status": _status_payload(connected=False),
                "/market/available-symbols": _disconnected_top_markets_payload(),
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertEqual(errors, [])
        checks = report["value_integrity"]["conversion_checks"]
        self.assertTrue(checks["ok"])
        self.assertEqual(checks["checked"], 3)
        self.assertEqual(checks["errors"], [])
        self.assertAlmostEqual(checks["sample"]["tick_based_usd_per_price"], 200.0)
        self.assertAlmostEqual(checks["sample"]["buy_sl_price"], 9.75)

    def test_disconnected_mt5_flags_armed_or_running_auto_trade(self):
        status = _status_payload(connected=False)
        status["runtime_controls"] = {
            **status["runtime_controls"],
            "auto_trade_enabled": True,
            "auto_trade_session_armed": True,
        }
        status["services"]["auto_trader"] = {
            "desired_running": True,
            "running": True,
            "last_error": None,
        }

        _, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": {},
                "/auto-trade/status": _auto_trade_status_payload(
                    running=True,
                    runtime_controls={"auto_trade_enabled": True},
                ),
                "/research/status": _research_payload(active=True),
            }
        )

        joined = "\n".join(errors)
        self.assertIn("Disconnected MT5 runtime must not have auto-trade enabled.", joined)
        self.assertIn("Disconnected MT5 runtime must not have auto-trade session armed.", joined)
        self.assertIn("Disconnected MT5 runtime must not have auto-trader running or desired.", joined)
        self.assertIn("Auto-trade cannot be enabled or armed while live trading readiness is blocked.", joined)

    def test_flags_unsafe_auto_trade_scan_interval(self):
        status = _status_payload(connected=False)
        status["runtime_controls"] = {
            **status["runtime_controls"],
            "auto_trade_scan_interval_seconds": 5,
        }

        report, errors, _ = self._run_with_payloads(
            {
                "/status": status,
                "/market/available-symbols": {},
                "/auto-trade/status": _auto_trade_status_payload(),
                "/research/status": _research_payload(active=True),
            }
        )

        self.assertEqual(report["auto_trade_safety"]["scan_interval_seconds"], 5)
        self.assertIn("Auto-trade scan interval must be at least 30 seconds.", "\n".join(errors))

    def test_strict_cli_promotes_live_blockers_to_top_level_errors(self):
        original_build_report = readiness.build_report

        def fake_build_report(_api_base: str, *, expect_live_ready: bool = False, expect_demo_rehearsal: bool = False):
            self.assertTrue(expect_live_ready)
            self.assertFalse(expect_demo_rehearsal)
            return (
                {
                    "live_trading_readiness": {
                        "can_execute_live_orders": False,
                        "blockers": [
                            "Connect an MT5 live trading account.",
                            "Turn Demo-Only By Default off in Policy & Runtime.",
                        ],
                        "warnings": ["Example warning."],
                    }
                },
                [],
                [],
            )

        readiness.build_report = fake_build_report
        try:
            with patch("sys.argv", ["verify_runtime_readiness.py", "--expect-live-ready"]):
                with patch("builtins.print") as mocked_print:
                    exit_code = readiness.main()
        finally:
            readiness.build_report = original_build_report

        self.assertEqual(exit_code, 1)
        output = mocked_print.call_args.args[0]
        self.assertIn("Live blocker: Connect an MT5 live trading account.", output)
        self.assertIn("Live blocker: Turn Demo-Only By Default off in Policy & Runtime.", output)
        self.assertIn("Live warning: Example warning.", output)


if __name__ == "__main__":
    unittest.main()
