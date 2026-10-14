from __future__ import annotations

from dataclasses import dataclass
import math

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
    "DAX": "GER40",
    "DAX40": "GER40",
    "DE40": "GER40",
    "GER30": "GER40",
    "GER30M": "GER40",
    "GDAXIM": "GER40",
    "FTSE100": "UK100",
    "FTSE": "UK100",
    "XAUUSD": "GOLD",
    "XAU": "GOLD",
    "XAGUSD": "SILVER",
    "XAG": "SILVER",
    "XTIUSD": "WTI",
    "USOIL": "WTI",
    "XBRUSD": "BRENT",
    "UKOIL": "BRENT",
}

PREFERRED_BROKER_SYMBOLS = {
    "US500": ["US500", "SPX500"],
    "US100": ["US100", "NAS100"],
    "US30": ["US30"],
    "GER40": ["DE40", "DAX", "GER30", "GER30M", "GDAXIM", "GER40", "DAX40"],
    "UK100": ["UK100", "FTSE100", "FTSE"],
    "GOLD": ["XAUUSD", "XAU", "GOLD"],
    "SILVER": ["XAGUSD", "XAG", "SILVER"],
    "WTI": ["USOIL", "XTIUSD", "WTI"],
    "BRENT": ["UKOIL", "XBRUSD", "BRENT"],
}

AGGRESSIVE_CFD_FOCUS_SYMBOLS = [
    "US500",
    "US100",
    "US30",
    "GER40",
    "UK100",
    "GOLD",
    "SILVER",
    "WTI",
    "BRENT",
]


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
            mode="broker-all-supported-asset-classes",
            active_asset_classes=list(self.config.ACTIVE_ASSET_CLASSES),
            enabled_symbols=list(self.config.ENABLED_SYMBOLS),
            disabled_symbols=list(self.config.DISABLED_SYMBOLS),
            symbol_profile_overrides=dict(self.config.SYMBOL_PROFILE_OVERRIDES),
        )

    def summary_dict(self) -> dict:
        return {
            "mode": "broker-all-supported-asset-classes",
            "active_asset_classes": list(self.config.ACTIVE_ASSET_CLASSES),
            "enabled_symbols": list(self.config.ENABLED_SYMBOLS),
            "auto_trade_fallback_symbols": list(self.config.AUTO_TRADE_FALLBACK_SYMBOLS),
            "disabled_symbols": list(self.config.DISABLED_SYMBOLS),
            "symbol_profile_overrides": dict(self.config.SYMBOL_PROFILE_OVERRIDES),
            "selection_mode": "all_active_asset_classes" if self.uses_all_symbols else "explicit_symbols",
            "aggressive_focus": "cfd_indices_commodities",
            "aggressive_cfd_symbols": list(AGGRESSIVE_CFD_FOCUS_SYMBOLS),
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
        cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
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
        if override == "silver":
            return "Commodities"
        if override == "stocks":
            return "Stocks"
        if canonical in {"US500", "US100", "US30", "GER40", "SPX500", "NAS100"}:
            return "Indices"
        if canonical in {"GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "XAUUSD", "XAGUSD", "USOIL", "UKOIL", "XTIUSD", "XBRUSD"}:
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
            if not isinstance(symbol_info, dict):
                continue
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
            if not isinstance(item, dict):
                continue
            canonical = self._item_canonical_symbol(item)
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
            if not isinstance(item, dict):
                continue
            canonical = self._item_canonical_symbol(item)
            if not canonical:
                continue
            grouped.setdefault(canonical, []).append(item)

        resolved: list[str] = []
        seen_actual: set[str] = set()
        for symbol in symbols:
            canonical = self.canonical_symbol(symbol)
            actual = self._pick_best_market_symbol(
                canonical=canonical,
                requested_raw=self._symbol_name(symbol).upper(),
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

    def aggressive_cfd_symbols(self, market_symbols: list[dict], *, limit: int | None = None) -> list[str]:
        resolved = self.resolve_requested_symbols(AGGRESSIVE_CFD_FOCUS_SYMBOLS, market_symbols)
        category_by_name = {
            self._symbol_name(item.get("name")).upper(): self.normalize_asset_class(item.get("category"))
            for item in market_symbols
            if isinstance(item, dict) and self._symbol_name(item.get("name"))
        }
        filtered: list[str] = []
        seen: set[str] = set()
        for symbol in resolved:
            canonical = self.canonical_symbol(symbol)
            expected = self.expected_category_for_symbol(canonical)
            if expected not in {"Indices", "Commodities"}:
                continue
            actual_category = category_by_name.get(self._symbol_name(symbol).upper())
            if actual_category and actual_category not in {"Indices", "Commodities"}:
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            filtered.append(symbol)
        return filtered[:limit] if limit is not None else filtered

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
        return self._symbol_name(best.get("name"))

    def _score_market_symbol(
        self,
        *,
        canonical: str,
        requested_raw: str,
        item: dict,
    ) -> int:
        raw = self._symbol_name(item.get("name")).upper()
        category = self.normalize_asset_class(item.get("category"))
        path = self._lower_text(item.get("path"))
        desc = self._lower_text(item.get("description"))
        expected_category = self.expected_category_for_symbol(canonical)

        score = 0
        if raw:
            score += 10
        if requested_raw and raw == requested_raw:
            score += 120

        preferred = self.preferred_broker_names(canonical)
        if raw in preferred:
            score += 90 - preferred.index(raw) * 5

        if expected_category and category == expected_category:
            score += 45
        elif expected_category == "Commodities" and category == "Stocks":
            score -= 160
        elif expected_category == "Indices" and category == "Stocks":
            score -= 120
        elif expected_category and category not in {expected_category, "Other"}:
            score -= 20

        trade_enabled = item.get("trade_enabled")
        if self._truthy_flag(trade_enabled):
            score += 12
        elif self._falsey_flag(trade_enabled):
            score -= 140
        visible = item.get("visible")
        if self._truthy_flag(visible):
            score += 5
        elif self._falsey_flag(visible):
            score -= 140
        # Prefer symbols that currently have executable quotes.
        try:
            bid = float(item.get("bid", 0.0) or 0.0)
            ask = float(item.get("ask", 0.0) or 0.0)
        except Exception:
            bid = 0.0
            ask = 0.0
        if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > bid:
            score += 20

        if self._matches_expected_hints(canonical, path, desc):
            score += 25

        if expected_category == "Commodities" and any(token in path for token in ("stock", "share", "nasdaq", "nyse", "equity")):
            score -= 45
        if expected_category == "Stocks" and any(token in path for token in ("stock", "share", "equity")):
            score += 15

        return score

    def _truthy_flag(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        if isinstance(value, (int, float)):
            return value == 1
        return False

    def _falsey_flag(self, value) -> bool:
        if isinstance(value, bool):
            return not value
        if isinstance(value, str):
            return value.strip().lower() in {"0", "false", "no", "n", "off"}
        if isinstance(value, (int, float)):
            return value == 0
        return False

    def _lower_text(self, value) -> str:
        return value.lower() if isinstance(value, str) else ""

    def _symbol_name(self, value) -> str:
        return value.strip() if isinstance(value, str) else ""

    def _item_canonical_symbol(self, item: dict) -> str:
        canonical = self.canonical_symbol(self._symbol_name(item.get("canonical_symbol")))
        if canonical:
            return canonical
        name = self._symbol_name(item.get("name"))
        canonical = self.canonical_symbol(name)
        if canonical:
            for separator in (".", "-", "_"):
                prefix = name.split(separator, 1)[0]
                prefix_canonical = self.canonical_symbol(prefix)
                if prefix_canonical in PREFERRED_BROKER_SYMBOLS:
                    return prefix_canonical
        return canonical

    def _matches_expected_hints(self, canonical: str, path: str, desc: str) -> bool:
        haystack = f"{path} {desc}"
        hints = {
            "GOLD": ("gold", "bullion", "metal", "xau", "metals"),
            "SILVER": ("silver", "xag", "metal", "metals"),
            "WTI": ("wti", "oil", "crude", "energy", "usoil"),
            "BRENT": ("brent", "oil", "crude", "energy", "ukoil"),
            "US500": ("index", "spx", "s&p", "cash", "indices"),
            "US100": ("index", "nas", "nasdaq", "cash", "indices"),
            "US30": ("index", "dow", "cash", "indices"),
            "GER40": ("index", "dax", "cash", "indices"),
        }.get(canonical, ())
        return any(token in haystack for token in hints)
