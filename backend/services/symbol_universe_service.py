from __future__ import annotations

from dataclasses import dataclass

from config import AppConfig, config


ASSET_CLASS_ALIASES = {
    "FOREX": "Forex",
    "INDICES": "Indices",
    "COMMODITIES": "Commodities",
    "STOCKS": "Stocks",
    "CRYPTO": "Crypto",
    "OTHER": "Other",
}

SYMBOL_ALIASES = {
    "SPX500": "US500",
    "NAS100": "US100",
    "XAUUSD": "GOLD",
    "XAU": "GOLD",
    "XTIUSD": "WTI",
    "USOIL": "WTI",
    "XBRUSD": "BRENT",
    "UKOIL": "BRENT",
}

PREFERRED_BROKER_SYMBOLS = {
    "US500": ["US500", "SPX500"],
    "US100": ["US100", "NAS100"],
    "US30": ["US30"],
    "GER40": ["GER40", "DAX40"],
    "GOLD": ["XAUUSD", "XAU", "GOLD"],
    "WTI": ["USOIL", "XTIUSD", "WTI"],
    "BRENT": ["UKOIL", "XBRUSD", "BRENT"],
}


@dataclass(frozen=True)
class UniverseSummary:
    mode: str
    active_asset_classes: list[str]
    enabled_symbols: list[str]
    disabled_symbols: list[str]
    symbol_profile_overrides: dict[str, str]


class SymbolUniverseService:
    """Constrains the active trading universe for the current market mode."""

    def __init__(self, app_config: AppConfig | None = None):
        self.config = app_config or config

    def summary(self) -> UniverseSummary:
        return UniverseSummary(
            mode="broker-stocks-indices-commodities-no-forex",
            active_asset_classes=list(self.config.ACTIVE_ASSET_CLASSES),
            enabled_symbols=list(self.config.ENABLED_SYMBOLS),
            disabled_symbols=list(self.config.DISABLED_SYMBOLS),
            symbol_profile_overrides=dict(self.config.SYMBOL_PROFILE_OVERRIDES),
        )

    def summary_dict(self) -> dict:
        return {
            "mode": "broker-stocks-indices-commodities-no-forex",
            "active_asset_classes": list(self.config.ACTIVE_ASSET_CLASSES),
            "enabled_symbols": list(self.config.ENABLED_SYMBOLS),
            "auto_trade_fallback_symbols": list(self.config.AUTO_TRADE_FALLBACK_SYMBOLS),
            "disabled_symbols": list(self.config.DISABLED_SYMBOLS),
            "symbol_profile_overrides": dict(self.config.SYMBOL_PROFILE_OVERRIDES),
            "selection_mode": "all_active_asset_classes" if self.uses_all_symbols else "explicit_symbols",
        }

    @property
    def uses_all_symbols(self) -> bool:
        return len(self.config.ENABLED_SYMBOLS) == 0

    def normalize_asset_class(self, category: str | None) -> str:
        if not category:
            return "Other"
        cleaned = category.strip()
        if not cleaned:
            return "Other"
        return ASSET_CLASS_ALIASES.get(cleaned.upper(), cleaned.title())

    def canonical_symbol(self, symbol: str | None) -> str:
        cleaned = (symbol or "").strip().upper()
        if not cleaned:
            return ""
        return SYMBOL_ALIASES.get(cleaned, cleaned)

    def profile_override_for(self, symbol: str) -> str | None:
        return self.config.SYMBOL_PROFILE_OVERRIDES.get(self.canonical_symbol(symbol))

    def expected_category_for_symbol(self, symbol: str | None) -> str | None:
        canonical = self.canonical_symbol(symbol)
        override = (self.profile_override_for(canonical) or "").lower()
        if override == "indices":
            return "Indices"
        if override in {"commodities", "gold"}:
            return "Commodities"
        if override == "stocks":
            return "Stocks"
        if canonical in {"US500", "US100", "US30", "GER40", "SPX500", "NAS100"}:
            return "Indices"
        if canonical in {"GOLD", "WTI", "BRENT", "NATGAS", "XAUUSD", "USOIL", "UKOIL", "XTIUSD", "XBRUSD"}:
            return "Commodities"
        if len(canonical) == 6 and canonical.isalpha():
            return "Forex"
        return None

    def preferred_broker_names(self, symbol: str | None) -> list[str]:
        return list(PREFERRED_BROKER_SYMBOLS.get(self.canonical_symbol(symbol), []))

    def is_asset_class_active(self, category: str | None) -> bool:
        normalized = self.normalize_asset_class(category)
        return normalized in self.config.ACTIVE_ASSET_CLASSES

    def is_symbol_enabled(self, symbol: str | None) -> bool:
        canonical = self.canonical_symbol(symbol)
        if not canonical:
            return False
        if canonical in self.config.DISABLED_SYMBOLS:
            return False
        if self.uses_all_symbols:
            return True
        return canonical in self.config.ENABLED_SYMBOLS

    def is_symbol_active(self, symbol: str | None, category: str | None = None) -> bool:
        if not self.is_symbol_enabled(symbol):
            return False
        if category is None:
            return True
        return self.is_asset_class_active(category)

    def restrict_symbols(self, symbols: list[str]) -> list[str]:
        filtered: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            canonical = self.canonical_symbol(symbol)
            if canonical in seen or not self.is_symbol_enabled(canonical):
                continue
            seen.add(canonical)
            filtered.append(canonical)
        return filtered

    def filter_market_symbols(self, symbols: list[dict], *, include_inactive: bool = False) -> list[dict]:
        filtered: list[dict] = []
        for symbol_info in symbols:
            symbol = self.canonical_symbol(symbol_info.get("name", ""))
            category = self.normalize_asset_class(symbol_info.get("category"))
            is_active = self.is_symbol_active(symbol, category)
            if not include_inactive and not is_active:
                continue
            item = dict(symbol_info)
            item["canonical_symbol"] = symbol
            item["category"] = category
            item["active_in_current_mode"] = is_active
            item["inactive_reason"] = "" if is_active else self.inactive_reason(symbol, category)
            filtered.append(item)
        return filtered

    def candidate_universe(self, market_symbols: list[dict]) -> list[str]:
        grouped: dict[str, list[dict]] = {}
        ordered: list[str] = []
        for item in market_symbols:
            canonical = self.canonical_symbol(item.get("canonical_symbol") or item.get("name", ""))
            if not canonical:
                continue
            if canonical not in grouped:
                grouped[canonical] = []
                ordered.append(canonical)
            grouped[canonical].append(item)
        if self.uses_all_symbols:
            requested = ordered
        else:
            requested = [symbol for symbol in self.config.ENABLED_SYMBOLS if symbol in grouped]

        candidates: list[str] = []
        seen: set[str] = set()
        for canonical in requested:
            actual = self._pick_best_market_symbol(
                canonical=canonical,
                requested_raw=canonical,
                candidates=grouped.get(canonical, []),
            )
            if not actual or actual in seen:
                continue
            seen.add(actual)
            candidates.append(actual)
        return candidates

    def resolve_requested_symbols(self, symbols: list[str], market_symbols: list[dict]) -> list[str]:
        grouped: dict[str, list[dict]] = {}
        for item in market_symbols:
            canonical = self.canonical_symbol(item.get("canonical_symbol") or item.get("name", ""))
            if not canonical:
                continue
            grouped.setdefault(canonical, []).append(item)

        resolved: list[str] = []
        seen_actual: set[str] = set()
        for symbol in symbols:
            canonical = self.canonical_symbol(symbol)
            actual = self._pick_best_market_symbol(
                canonical=canonical,
                requested_raw=(symbol or "").strip().upper(),
                candidates=grouped.get(canonical, []),
            )
            if not actual or actual in seen_actual:
                continue
            seen_actual.add(actual)
            resolved.append(actual)
        return resolved

    def default_auto_trade_symbols(self, market_symbols: list[dict], *, limit: int | None = None) -> list[str]:
        requested = self.config.AUTO_TRADE_FALLBACK_SYMBOLS or self.config.ENABLED_SYMBOLS
        resolved = self.resolve_requested_symbols(requested, market_symbols)
        return resolved[:limit] if limit is not None else resolved

    def inactive_reason(self, symbol: str | None, category: str | None = None) -> str:
        canonical = self.canonical_symbol(symbol)
        normalized_category = self.normalize_asset_class(category)
        if canonical in self.config.DISABLED_SYMBOLS:
            return f"{canonical} is disabled in the current mode."
        if not self.uses_all_symbols and canonical and canonical not in self.config.ENABLED_SYMBOLS:
            return f"{canonical} is outside the configured symbol universe."
        if normalized_category and normalized_category not in self.config.ACTIVE_ASSET_CLASSES:
            return f"{normalized_category} is inactive in the current mode."
        return "Symbol is inactive in the current mode."

    def _pick_best_market_symbol(
        self,
        *,
        canonical: str,
        requested_raw: str,
        candidates: list[dict],
    ) -> str:
        if not candidates:
            return ""
        best = max(
            candidates,
            key=lambda item: self._score_market_symbol(
                canonical=canonical,
                requested_raw=requested_raw,
                item=item,
            ),
        )
        return best.get("name", "")

    def _score_market_symbol(
        self,
        *,
        canonical: str,
        requested_raw: str,
        item: dict,
    ) -> int:
        raw = (item.get("name", "") or "").strip().upper()
        category = self.normalize_asset_class(item.get("category"))
        path = (item.get("path", "") or "").lower()
        desc = (item.get("description", "") or "").lower()
        expected_category = self.expected_category_for_symbol(canonical)

        score = 0
        if requested_raw and raw == requested_raw:
            score += 120

        preferred = self.preferred_broker_names(canonical)
        if raw in preferred:
            score += 90 - preferred.index(raw) * 5

        if expected_category and category == expected_category:
            score += 45
        elif expected_category == "Commodities" and category == "Stocks":
            score -= 60
        elif expected_category == "Indices" and category == "Stocks":
            score -= 35
        elif expected_category and category not in {expected_category, "Other"}:
            score -= 20

        if item.get("trade_enabled"):
            score += 12
        if item.get("visible"):
            score += 5

        if self._matches_expected_hints(canonical, path, desc):
            score += 25

        if expected_category == "Commodities" and any(token in path for token in ("stock", "share", "nasdaq", "nyse", "equity")):
            score -= 45
        if expected_category == "Stocks" and any(token in path for token in ("stock", "share", "equity")):
            score += 15

        return score

    def _matches_expected_hints(self, canonical: str, path: str, desc: str) -> bool:
        haystack = f"{path} {desc}"
        hints = {
            "GOLD": ("gold", "bullion", "metal", "xau", "metals"),
            "WTI": ("wti", "oil", "crude", "energy", "usoil"),
            "BRENT": ("brent", "oil", "crude", "energy", "ukoil"),
            "US500": ("index", "spx", "s&p", "cash", "indices"),
            "US100": ("index", "nas", "nasdaq", "cash", "indices"),
            "US30": ("index", "dow", "cash", "indices"),
            "GER40": ("index", "dax", "cash", "indices"),
        }.get(canonical, ())
        return any(token in haystack for token in hints)
