import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import AppConfig
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
