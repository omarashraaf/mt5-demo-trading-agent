import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import MarketContext, NormalizedSymbolInfo, SymbolProfile, TechnicalSignal
from risk.rules import RiskSettings
from services.portfolio_risk_service import PortfolioRiskService


class _MarketData:
    def get_symbol_info(self, symbol):
        base = {
            "name": symbol,
            "description": "Instrument",
            "path": "forex\\majors" if symbol.endswith("USD") else "stocks\\us",
            "point": 0.0001,
            "digits": 5,
            "trade_contract_size": 100000 if symbol.endswith("USD") else 1,
            "volume_min": 0.01,
            "volume_max": 100,
            "volume_step": 0.01,
            "trade_stops_level": 10,
            "trade_mode": 4,
            "visible": True,
            "spread": 2,
        }
        if symbol in {"AAPL", "MSFT"}:
            base["path"] = "stocks\\us"
            base["trade_contract_size"] = 1
            base["digits"] = 2
        return base


class PortfolioRiskServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = PortfolioRiskService(_MarketData())
        self.settings = RiskSettings(
            max_margin_utilization_pct=15,
            min_free_margin_pct=50,
            max_open_positions_total=3,
            max_positions_per_symbol=1,
            cooldown_minutes_per_symbol=120,
            max_sector_exposure_pct=20,
            max_usd_beta_exposure_pct=10,
            max_equity_exposure_pct_for_stocks=12,
            max_correlated_positions=1,
        )

    def _context(self, symbol="EURUSD", positions=None, margin=200, free_margin=9800):
        is_forex = len(symbol) == 6 and symbol.isalpha()
        info = NormalizedSymbolInfo(
            name=symbol,
            description="Forex pair",
            category="Forex" if is_forex else "Stocks",
            path="forex\\majors" if is_forex else "stocks\\us",
            point=0.0001,
            digits=5 if is_forex else 2,
            trade_contract_size=100000 if is_forex else 1,
            volume_min=0.01,
            volume_step=0.01,
            trade_enabled=True,
            spread=2,
            sector="FX" if is_forex else "Technology",
            theme_bucket="USD" if is_forex else "US Tech",
            base_currency="EUR" if symbol == "EURUSD" else ("USD" if symbol == "USDJPY" else ""),
            quote_currency="USD" if symbol == "EURUSD" else ("JPY" if symbol == "USDJPY" else ""),
            usd_beta_weight=-1.0 if symbol == "EURUSD" else 1.0 if symbol == "USDJPY" else 0.3,
            correlation_tags=["FX", "USD"] if is_forex else ["Stocks", "Technology", "US Tech"],
        )
        profile = SymbolProfile(
            profile_name="Forex Majors" if is_forex else "Stocks",
            category=info.category,
            max_spread=12 if is_forex else 20,
            min_atr_pct=0.2,
            max_hold_minutes=360,
            min_reward_risk=1.8,
            max_positions_per_category=2,
            quality_threshold=0.72,
            cooldown_minutes=120,
            news_weight=0.1,
            default_sl_atr_multiplier=1.2,
            default_tp_atr_multiplier=2.4,
            sector=info.sector,
            theme_bucket=info.theme_bucket,
        )
        return MarketContext(
            symbol=symbol,
            symbol_info=info,
            profile=profile,
            tick={"bid": 1.1, "ask": 1.1002, "spread": 2},
            account_equity=10000,
            account_margin=margin,
            account_free_margin=free_margin,
            account_leverage=100,
            all_open_positions=positions or [],
        )

    def test_duplicate_symbol_block(self):
        context = self._context(positions=[{"symbol": "EURUSD", "volume": 0.02, "price_current": 1.1, "price_open": 1.1}])
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.8)
        assessment = self.service.assess(context, signal, 0.02, 1.1002, self.settings, recent_outcomes=[], is_auto_trade=True)
        self.assertFalse(assessment.allow_execute)
        self.assertIn("EURUSD already has", assessment.reason)

    def test_correlated_exposure_block(self):
        context = self._context(
            symbol="USDJPY",
            positions=[
                {"symbol": "EURUSD", "volume": 0.02, "price_current": 1.1, "price_open": 1.1},
                {"symbol": "GBPUSD", "volume": 0.02, "price_current": 1.3, "price_open": 1.3},
            ],
        )
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.8)
        assessment = self.service.assess(context, signal, 0.02, 145.0, self.settings, recent_outcomes=[], is_auto_trade=True)
        self.assertFalse(assessment.allow_execute)
        self.assertTrue(any("Correlated exposure" in reason for reason in assessment.blocking_reasons))

    def test_margin_utilization_block(self):
        context = self._context(margin=1300, free_margin=8700)
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.8)
        assessment = self.service.assess(context, signal, 0.3, 1.1002, self.settings, recent_outcomes=[], is_auto_trade=True)
        self.assertFalse(assessment.allow_execute)
        self.assertTrue(any("Projected margin utilization" in reason for reason in assessment.blocking_reasons))

    def test_free_margin_floor_block(self):
        context = self._context(margin=3000, free_margin=1200)
        signal = TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.8)
        assessment = self.service.assess(context, signal, 0.2, 1.1002, self.settings, recent_outcomes=[], is_auto_trade=True)
        self.assertFalse(assessment.allow_execute)
        self.assertTrue(any("Projected free margin" in reason for reason in assessment.blocking_reasons))
