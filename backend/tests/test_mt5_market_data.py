import unittest
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5.market_data import MarketDataService


class MT5MarketDataCategorizationTests(unittest.TestCase):
    def test_categorizes_common_broker_suffix_symbols(self):
        service = MarketDataService()

        cases = [
            ("EURUSDm", "", "", "Forex"),
            ("US500.cash", "", "", "Indices"),
            ("AAPL.US", "", "", "Stocks"),
            ("XAUUSD.pro", "", "", "Commodities"),
            ("BTCUSDm", "", "", "Crypto"),
        ]

        for name, path, description, expected in cases:
            with self.subTest(name=name):
                symbol = SimpleNamespace(name=name, path=path, description=description)
                self.assertEqual(service._categorize_symbol(symbol), expected)


if __name__ == "__main__":
    unittest.main()
