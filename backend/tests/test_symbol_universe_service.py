import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import AppConfig
from auto_trader import _recovery_gate_thresholds
from services.symbol_universe_service import SymbolUniverseService


class SymbolUniverseServiceTests(unittest.TestCase):
    def test_empty_enabled_symbols_means_all_active_asset_classes(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=["NATGAS"],
            )
        )
        self.assertTrue(service.uses_all_symbols)
        self.assertTrue(service.is_symbol_active("TSLA", "Stocks"))
        self.assertTrue(service.is_symbol_active("US500", "Indices"))
        self.assertTrue(service.is_symbol_active("XAUUSD", "Commodities"))
        self.assertFalse(service.is_symbol_active("EURUSD", "Forex"))

    def test_candidate_universe_returns_all_matching_broker_symbols(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=["NATGAS"],
            )
        )
        market_symbols = [
            {"name": "TSLA", "category": "Stocks"},
            {"name": "US500", "category": "Indices"},
            {"name": "XAUUSD", "category": "Commodities", "canonical_symbol": "GOLD"},
            {"name": "EURUSD", "category": "Forex"},
        ]
        filtered = service.filter_market_symbols(market_symbols)
        candidates = service.candidate_universe(filtered)
        self.assertIn("TSLA", candidates)
        self.assertIn("US500", candidates)
        self.assertIn("XAUUSD", candidates)
        self.assertNotIn("EURUSD", candidates)

    def test_resolve_requested_symbols_prefers_real_commodity_over_stock_alias(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=["NATGAS"],
            )
        )
        market_symbols = [
            {"name": "WTI", "category": "Stocks", "path": "Nasdaq\\Stock\\WTI", "trade_enabled": True, "visible": True},
            {"name": "USOIL", "category": "Commodities", "path": "Commodities\\Energy", "trade_enabled": True, "visible": True},
            {"name": "GOLD", "category": "Stocks", "path": "Nasdaq\\Stock\\GOLD", "trade_enabled": True, "visible": True},
            {"name": "XAUUSD", "category": "Commodities", "path": "Commodities\\Metals", "trade_enabled": True, "visible": True},
        ]
        resolved = service.resolve_requested_symbols(["WTI", "GOLD"], market_symbols)
        self.assertEqual(resolved, ["USOIL", "XAUUSD"])

    def test_aggressive_cfd_symbols_resolves_indices_and_commodities_only(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {"name": "NAS100", "canonical_symbol": "US100", "category": "Indices", "trade_enabled": True, "visible": True},
            {"name": "US30", "category": "Indices", "trade_enabled": True, "visible": True},
            {"name": "XAUUSD", "canonical_symbol": "GOLD", "category": "Commodities", "trade_enabled": True, "visible": True},
            {"name": "USOIL", "canonical_symbol": "WTI", "category": "Commodities", "trade_enabled": True, "visible": True},
            {"name": "AAPL", "category": "Stocks", "trade_enabled": True, "visible": True},
            {"name": "WTI", "category": "Stocks", "trade_enabled": True, "visible": True},
        ]

        resolved = service.aggressive_cfd_symbols(service.filter_market_symbols(market_symbols))

        self.assertEqual(resolved, ["NAS100", "US30", "XAUUSD", "USOIL"])
        self.assertNotIn("AAPL", resolved)
        self.assertNotIn("WTI", resolved)

    def test_aggressive_cfd_recovery_gate_is_less_restrictive_than_stocks(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        settings = type("Settings", (), {"policy_mode": "aggressive"})()

        cfd_confidence, cfd_quality = _recovery_gate_thresholds(
            settings,
            service,
            "XAUUSD",
            total_profit=-100.0,
        )
        stock_confidence, stock_quality = _recovery_gate_thresholds(
            settings,
            service,
            "AAPL",
            total_profit=-100.0,
        )

        self.assertEqual((cfd_confidence, cfd_quality), (0.50, 0.72))
        self.assertEqual((stock_confidence, stock_quality), (0.70, 0.84))

    def test_resolve_requested_symbols_does_not_boost_string_false_flags(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.inactive",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": "false",
                "visible": "false",
            },
            {
                "name": "US100.active",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": True,
                "visible": True,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.active"])

    def test_resolve_requested_symbols_does_not_boost_non_finite_quotes(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.badquote",
                "canonical_symbol": "US100",
                "category": "Indices",
                "bid": float("inf"),
                "ask": 18000.0,
            },
            {
                "name": "US100.live",
                "canonical_symbol": "US100",
                "category": "Indices",
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])

    def test_resolve_requested_symbols_does_not_boost_crossed_quotes(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.crossed",
                "canonical_symbol": "US100",
                "category": "Indices",
                "bid": 18001.0,
                "ask": 18000.0,
            },
            {
                "name": "US100.live",
                "canonical_symbol": "US100",
                "category": "Indices",
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])

    def test_resolve_requested_symbols_prefers_enabled_alias_over_disabled_exact(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": False,
                "visible": True,
                "bid": 17999.0,
                "ask": 18000.0,
            },
            {
                "name": "NAS100",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": True,
                "visible": True,
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["NAS100"])

    def test_resolve_requested_symbols_prefers_visible_alias_over_hidden_exact(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": True,
                "visible": False,
                "bid": 17999.0,
                "ask": 18000.0,
            },
            {
                "name": "NAS100",
                "canonical_symbol": "US100",
                "category": "Indices",
                "trade_enabled": True,
                "visible": True,
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["NAS100"])

    def test_resolve_requested_symbols_sanitizes_non_string_path_metadata(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.badmeta",
                "canonical_symbol": "US100",
                "category": "Indices",
                "path": ["not", "a", "string"],
                "description": {"bad": "metadata"},
            },
            {
                "name": "US100.live",
                "canonical_symbol": "US100",
                "category": "Indices",
                "path": "Indices\\US",
                "description": "US tech index",
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])

    def test_resolve_requested_symbols_sanitizes_non_string_name_metadata(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": ["US100"],
                "canonical_symbol": "US100",
                "category": "Indices",
                "path": "Indices\\US",
            },
            {
                "name": "US100.live",
                "canonical_symbol": "US100",
                "category": "Indices",
                "path": "Indices\\US",
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])

    def test_resolve_requested_symbols_falls_back_from_malformed_canonical_metadata(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.live",
                "canonical_symbol": ["US100"],
                "category": "Indices",
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols(["US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])

    def test_resolve_requested_symbols_ignores_non_string_requested_values(self):
        service = SymbolUniverseService(
            AppConfig(
                ACTIVE_ASSET_CLASSES=["Indices", "Commodities", "Stocks"],
                ENABLED_SYMBOLS=[],
                DISABLED_SYMBOLS=[],
            )
        )
        market_symbols = [
            {
                "name": "US100.live",
                "canonical_symbol": "US100",
                "category": "Indices",
                "bid": 17999.0,
                "ask": 18000.0,
            },
        ]

        resolved = service.resolve_requested_symbols([["US100"], "US100"], market_symbols)

        self.assertEqual(resolved, ["US100.live"])
