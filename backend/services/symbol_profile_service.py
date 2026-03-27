from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from domain.models import NormalizedSymbolInfo, SymbolProfile
from services.symbol_universe_service import SymbolUniverseService


MAJOR_FOREX = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"}
GOLD_SYMBOLS = {"XAUUSD", "XAUEUR", "GLD", "GLDM", "IAU", "GOLD"}
INDEX_SYMBOLS = {"US30", "US500", "US100", "NAS100", "SPX500", "GER40", "UK100", "JPN225", "AUS200"}
ENERGY_SYMBOLS = {"WTI", "BRENT", "USOIL", "UKOIL"}
NATURAL_GAS_SYMBOLS = {"NATGAS", "XNGUSD", "NGAS", "NG"}
TECH_STOCKS = {
    "NVDA", "AMD", "MSFT", "INTC", "AAPL", "GOOG", "GOOGL", "AMZN",
    "TSLA", "META", "NFLX", "AVGO", "CRM", "ORCL", "ADBE", "QCOM",
}


@dataclass(frozen=True)
class ProfileTemplate:
    profile_name: str
    category: str
    max_spread: float
    min_atr_pct: float
    max_hold_minutes: int
    news_hold_min_minutes: int
    news_hold_max_minutes: int
    technical_hold_min_minutes: int
    technical_hold_max_minutes: int
    min_reward_risk: float
    max_positions_per_category: int
    quality_threshold: float
    cooldown_minutes: int
    news_weight: float
    allowed_sessions: tuple[str, ...]
    default_sl_atr_multiplier: float
    default_tp_atr_multiplier: float
    sector: str
    theme_bucket: str
    force_close_before_session_end: bool = False
    allow_overnight_hold: bool = False
    session_close_buffer_minutes: int = 10
    usd_beta_weight: float = 0.0

    def to_model(self) -> SymbolProfile:
        return SymbolProfile(
            profile_name=self.profile_name,
            category=self.category,
            max_spread=self.max_spread,
            min_atr_pct=self.min_atr_pct,
            max_hold_minutes=self.max_hold_minutes,
            news_hold_min_minutes=self.news_hold_min_minutes,
            news_hold_max_minutes=self.news_hold_max_minutes,
            technical_hold_min_minutes=self.technical_hold_min_minutes,
            technical_hold_max_minutes=self.technical_hold_max_minutes,
            min_reward_risk=self.min_reward_risk,
            max_positions_per_category=self.max_positions_per_category,
            quality_threshold=self.quality_threshold,
            cooldown_minutes=self.cooldown_minutes,
            news_weight=self.news_weight,
            allowed_sessions=list(self.allowed_sessions),
            default_sl_atr_multiplier=self.default_sl_atr_multiplier,
            default_tp_atr_multiplier=self.default_tp_atr_multiplier,
            force_close_before_session_end=self.force_close_before_session_end,
            allow_overnight_hold=self.allow_overnight_hold,
            session_close_buffer_minutes=self.session_close_buffer_minutes,
            sector=self.sector,
            theme_bucket=self.theme_bucket,
            usd_beta_weight=self.usd_beta_weight,
        )


class SymbolProfileService:
    def __init__(self, universe_service: SymbolUniverseService | None = None):
        self.universe_service = universe_service or SymbolUniverseService()
        self._profiles = {
            "forex_majors": ProfileTemplate(
                profile_name="Forex Majors",
                category="Forex",
                max_spread=12.0,
                min_atr_pct=0.15,
                max_hold_minutes=360,
                news_hold_min_minutes=45,
                news_hold_max_minutes=90,
                technical_hold_min_minutes=90,
                technical_hold_max_minutes=180,
                min_reward_risk=1.8,
                max_positions_per_category=2,
                quality_threshold=0.72,
                cooldown_minutes=90,
                news_weight=0.10,
                allowed_sessions=("London", "New York", "Overlap"),
                default_sl_atr_multiplier=1.2,
                default_tp_atr_multiplier=2.4,
                sector="FX",
                theme_bucket="G10FX",
            ),
            "gold": ProfileTemplate(
                profile_name="Gold",
                category="Commodities",
                max_spread=45.0,
                min_atr_pct=0.35,
                max_hold_minutes=1440,
                news_hold_min_minutes=30,
                news_hold_max_minutes=75,
                technical_hold_min_minutes=90,
                technical_hold_max_minutes=180,
                min_reward_risk=2.0,
                max_positions_per_category=1,
                quality_threshold=0.76,
                cooldown_minutes=180,
                news_weight=0.18,
                allowed_sessions=("London", "New York"),
                default_sl_atr_multiplier=1.8,
                default_tp_atr_multiplier=3.6,
                sector="Precious Metals",
                theme_bucket="Gold",
                usd_beta_weight=0.6,
            ),
            "indices": ProfileTemplate(
                profile_name="Indices",
                category="Indices",
                max_spread=20.0,
                min_atr_pct=0.25,
                max_hold_minutes=720,
                news_hold_min_minutes=30,
                news_hold_max_minutes=60,
                technical_hold_min_minutes=60,
                technical_hold_max_minutes=120,
                min_reward_risk=2.0,
                max_positions_per_category=2,
                quality_threshold=0.75,
                cooldown_minutes=180,
                news_weight=0.14,
                allowed_sessions=("Europe", "US Open"),
                default_sl_atr_multiplier=1.6,
                default_tp_atr_multiplier=3.2,
                force_close_before_session_end=True,
                allow_overnight_hold=False,
                session_close_buffer_minutes=10,
                sector="Equity Indices",
                theme_bucket="Index",
                usd_beta_weight=0.4,
            ),
            "stocks": ProfileTemplate(
                profile_name="Stocks",
                category="Stocks",
                max_spread=25.0,
                min_atr_pct=0.80,
                max_hold_minutes=4320,
                news_hold_min_minutes=30,
                news_hold_max_minutes=60,
                technical_hold_min_minutes=90,
                technical_hold_max_minutes=180,
                min_reward_risk=2.0,
                max_positions_per_category=3,
                quality_threshold=0.78,
                cooldown_minutes=240,
                news_weight=0.12,
                allowed_sessions=("US Open", "US Midday"),
                default_sl_atr_multiplier=2.0,
                default_tp_atr_multiplier=4.0,
                force_close_before_session_end=True,
                allow_overnight_hold=False,
                session_close_buffer_minutes=10,
                sector="Equities",
                theme_bucket="Stocks",
                usd_beta_weight=0.3,
            ),
            "crypto": ProfileTemplate(
                profile_name="Crypto",
                category="Crypto",
                max_spread=40.0,
                min_atr_pct=1.20,
                max_hold_minutes=720,
                news_hold_min_minutes=45,
                news_hold_max_minutes=90,
                technical_hold_min_minutes=120,
                technical_hold_max_minutes=180,
                min_reward_risk=2.2,
                max_positions_per_category=2,
                quality_threshold=0.80,
                cooldown_minutes=180,
                news_weight=0.08,
                allowed_sessions=("24/7",),
                default_sl_atr_multiplier=1.8,
                default_tp_atr_multiplier=4.0,
                sector="Crypto",
                theme_bucket="Crypto",
            ),
            "commodities": ProfileTemplate(
                profile_name="Commodities",
                category="Commodities",
                max_spread=35.0,
                min_atr_pct=0.40,
                max_hold_minutes=2880,
                news_hold_min_minutes=30,
                news_hold_max_minutes=75,
                technical_hold_min_minutes=120,
                technical_hold_max_minutes=180,
                min_reward_risk=2.0,
                max_positions_per_category=1,
                quality_threshold=0.76,
                cooldown_minutes=180,
                news_weight=0.16,
                allowed_sessions=("London", "New York"),
                default_sl_atr_multiplier=1.8,
                default_tp_atr_multiplier=3.6,
                sector="Commodities",
                theme_bucket="Commodities",
            ),
        }

    def resolve_profile(
        self,
        symbol: str,
        symbol_info: NormalizedSymbolInfo | None,
    ) -> SymbolProfile:
        normalized = self.universe_service.canonical_symbol(symbol)
        category = symbol_info.category if symbol_info else "Other"
        override = self.universe_service.profile_override_for(normalized)

        if override and override in self._profiles:
            template = self._profiles[override]
        elif normalized in GOLD_SYMBOLS or normalized.startswith("XAU"):
            template = self._profiles["gold"]
        elif normalized in ENERGY_SYMBOLS:
            template = self._profiles["commodities"]
        elif normalized in NATURAL_GAS_SYMBOLS:
            template = self._profiles["commodities"]
        elif normalized in MAJOR_FOREX or category == "Forex":
            template = self._profiles["forex_majors"]
        elif normalized in INDEX_SYMBOLS or category == "Indices":
            template = self._profiles["indices"]
        elif category == "Crypto":
            template = self._profiles["crypto"]
        elif category == "Stocks":
            template = self._profiles["stocks"]
        elif category == "Commodities":
            template = self._profiles["commodities"]
        else:
            template = self._profiles["stocks"]

        model = template.to_model()
        if normalized in TECH_STOCKS:
            model.sector = "Technology"
            model.theme_bucket = "US Tech"
        elif normalized in {"NFLX", "DIS"}:
            model.sector = "Media"
            model.theme_bucket = "Consumer Media"
        elif normalized in {"JPM", "V", "MA"}:
            model.sector = "Financials"
            model.theme_bucket = "US Financials"

        return model

    def enrich_symbol_info(
        self,
        symbol: str,
        symbol_info: NormalizedSymbolInfo,
    ) -> NormalizedSymbolInfo:
        profile = self.resolve_profile(symbol, symbol_info)
        base, quote = self._extract_currencies(symbol)
        usd_beta = 0.0
        if base == "USD":
            usd_beta = 1.0
        elif quote == "USD":
            usd_beta = -1.0
        elif profile.category in {"Stocks", "Indices", "Commodities"}:
            usd_beta = profile.usd_beta_weight

        correlation_tags = sorted(
            set(
                self._non_empty(
                    profile.category,
                    profile.sector,
                    profile.theme_bucket,
                    base,
                    quote,
                    "USD" if "USD" in {base, quote} else "",
                )
            )
        )

        return symbol_info.model_copy(
            update={
                "sector": profile.sector,
                "theme_bucket": profile.theme_bucket,
                "base_currency": base,
                "quote_currency": quote,
                "usd_beta_weight": usd_beta,
                "correlation_tags": correlation_tags,
            }
        )

    def correlation_tags(self, symbol_info: NormalizedSymbolInfo | None) -> list[str]:
        if symbol_info is None:
            return []
        return list(symbol_info.correlation_tags)

    def _extract_currencies(self, symbol: str) -> tuple[str, str]:
        normalized = self.universe_service.canonical_symbol(symbol)
        if len(normalized) >= 6 and normalized[:6].isalpha():
            return normalized[:3], normalized[3:6]
        return "", ""

    def _non_empty(self, *values: str) -> Iterable[str]:
        return (value for value in values if value)
