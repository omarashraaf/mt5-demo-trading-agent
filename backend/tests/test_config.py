import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_app_config


class ConfigTests(unittest.TestCase):
    def test_valid_config_loads(self):
        cfg = load_app_config(
            {
                "API_HOST": "127.0.0.1",
                "API_PORT": "8000",
                "LOG_LEVEL": "debug",
                "GEMINI_TIMEOUT_SECONDS": "15",
                "GEMINI_MAX_RETRIES": "2",
                "ENABLE_FINNHUB": "true",
                "FINNHUB_API_KEY": "demo-key",
                "ACTIVE_ASSET_CLASSES": "Indices,Commodities,Stocks",
                "ENABLED_SYMBOLS": "",
                "AUTO_TRADE_FALLBACK_SYMBOLS": "US500,GOLD,TSLA",
            }
        )
        self.assertEqual(cfg.API_PORT, 8000)
        self.assertEqual(cfg.LOG_LEVEL, "DEBUG")
        self.assertEqual(cfg.api_base_url, "http://127.0.0.1:8000")
        self.assertTrue(cfg.ENABLE_FINNHUB)
        self.assertEqual(cfg.ACTIVE_ASSET_CLASSES, ["Indices", "Commodities", "Stocks"])
        self.assertEqual(cfg.ENABLED_SYMBOLS, [])
        self.assertEqual(cfg.AUTO_TRADE_FALLBACK_SYMBOLS, ["US500", "GOLD", "TSLA"])

    def test_invalid_port_raises(self):
        with self.assertRaises(Exception):
            load_app_config({"API_PORT": "99999"})

    def test_invalid_asset_class_raises(self):
        with self.assertRaises(Exception):
            load_app_config({"ACTIVE_ASSET_CLASSES": "Indices,AlienAssets"})
