from __future__ import annotations

import time

from domain.models import CandidateAssetMapping, ExternalEvent
from services.symbol_universe_service import SymbolUniverseService


class AssetMappingService:
    """Maps normalized external events to the configured tradable universe."""

    def __init__(self, universe_service: SymbolUniverseService | None = None):
        self.universe_service = universe_service or SymbolUniverseService()

    def map_event(self, event: ExternalEvent) -> list[CandidateAssetMapping]:
        if not event.usable:
            return []
        if self._is_stale(event):
            return []

        candidates: dict[str, CandidateAssetMapping] = {}
        for symbol in event.affected_assets:
            self._upsert_candidate(
                candidates,
                symbol=symbol,
                baseline_bias="neutral",
                needs_gemini=True,
                tradable=event.importance in {"medium", "high"},
                mapping_score=0.65,
                reason=f"Provider tagged {symbol} as affected.",
            )

        title = event.title.lower()
        summary = event.summary.lower()
        haystack = f"{title} {summary}".strip()
        high_importance = event.importance == "high"

        if any(token in haystack for token in ("cpi", "inflation", "fomc", "fed", "pce", "payroll", "nfp", "jobs", "rates", "treasury")):
            self._upsert_candidate(candidates, "US100", "bearish", True, high_importance, 0.82, "US macro/rates event.")
            self._upsert_candidate(candidates, "US500", "bearish", True, high_importance, 0.80, "US macro/rates event.")
            self._upsert_candidate(candidates, "US30", "bearish", True, high_importance, 0.76, "US macro/rates event.")
            self._upsert_candidate(candidates, "GOLD", "neutral", True, high_importance, 0.70, "Inflation/rates event often impacts gold, but context matters.")

        if any(token in haystack for token in ("dovish", "rate cut", "stimulus", "liquidity", "easing")):
            self._upsert_candidate(candidates, "US100", "bullish", False, True, 0.78, "Dovish/liquidity impulse.")
            self._upsert_candidate(candidates, "US500", "bullish", False, True, 0.76, "Dovish/liquidity impulse.")
            self._upsert_candidate(candidates, "US30", "bullish", False, True, 0.74, "Dovish/liquidity impulse.")
            self._upsert_candidate(candidates, "GOLD", "bullish", True, True, 0.68, "Dovish macro can support gold.")

        if any(token in haystack for token in ("ecb", "german", "eurozone", "europe", "bund")) or event.country in {"DE", "GER", "EU", "EMU"}:
            self._upsert_candidate(candidates, "GER40", "neutral", True, event.importance != "low", 0.74, "European macro event.")
            self._upsert_candidate(candidates, "GOLD", "neutral", True, False, 0.55, "European macro event may shift risk tone.")

        if any(token in haystack for token in ("opec", "oil", "crude", "inventory", "production", "pipeline", "refinery", "supply")):
            self._upsert_candidate(candidates, "WTI", "bullish", True, event.importance != "low", 0.83, "Oil supply/demand catalyst.")
            self._upsert_candidate(candidates, "BRENT", "bullish", True, event.importance != "low", 0.83, "Oil supply/demand catalyst.")

        if any(token in haystack for token in ("recession", "risk-off", "geopolitical", "shock", "bank stress", "credit stress")):
            self._upsert_candidate(candidates, "US100", "bearish", False, True, 0.78, "Risk-off shock.")
            self._upsert_candidate(candidates, "US500", "bearish", False, True, 0.78, "Risk-off shock.")
            self._upsert_candidate(candidates, "US30", "bearish", False, True, 0.75, "Risk-off shock.")
            self._upsert_candidate(candidates, "GER40", "bearish", False, True, 0.74, "Risk-off shock.")
            self._upsert_candidate(candidates, "GOLD", "bullish", True, True, 0.72, "Risk-off shock can benefit gold.")

        result = []
        for candidate in candidates.values():
            symbol = self.universe_service.canonical_symbol(candidate.symbol)
            inferred_category = self._infer_category(symbol)
            if not self.universe_service.is_symbol_active(symbol, inferred_category):
                continue
            result.append(candidate.model_copy(update={"symbol": symbol}))

        result.sort(key=lambda item: (item.tradable, item.mapping_score), reverse=True)
        return result

    def _upsert_candidate(
        self,
        candidates: dict[str, CandidateAssetMapping],
        symbol: str,
        baseline_bias: str,
        needs_gemini: bool,
        tradable: bool,
        mapping_score: float,
        reason: str,
    ):
        canonical = self.universe_service.canonical_symbol(symbol)
        if not canonical:
            return
        existing = candidates.get(canonical)
        candidate = CandidateAssetMapping(
            symbol=canonical,
            baseline_bias=baseline_bias,
            needs_gemini_clarification=needs_gemini,
            tradable=tradable,
            mapping_score=mapping_score,
            reason=reason,
        )
        if existing is None or candidate.mapping_score >= existing.mapping_score:
            candidates[canonical] = candidate

    def _is_stale(self, event: ExternalEvent) -> bool:
        age_seconds = max(0.0, time.time() - event.timestamp_utc)
        if event.event_type == "economic_calendar":
            return age_seconds > 24 * 60 * 60
        return age_seconds > 12 * 60 * 60

    def _infer_category(self, symbol: str) -> str:
        normalized = self.universe_service.canonical_symbol(symbol)
        if len(normalized) == 6 and normalized.isalpha():
            return "Forex"
        if normalized in {"US500", "US100", "US30", "GER40", "SPX500", "NAS100"}:
            return "Indices"
        if normalized in {"GOLD", "WTI", "BRENT", "XAUUSD", "USOIL", "UKOIL"}:
            return "Commodities"
        return "Stocks"
