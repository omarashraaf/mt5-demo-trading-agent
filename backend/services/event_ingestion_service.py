from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from adapters.finnhub_adapter import FinnhubAdapter, FinnhubAdapterError
from domain.models import CandidateAssetMapping, ExternalEvent, GeminiEventAssessment, NormalizedNewsItem
from services.asset_mapping_service import AssetMappingService
from services.event_normalization_service import EventNormalizationService
from services.symbol_universe_service import SymbolUniverseService


logger = logging.getLogger(__name__)


class EventIngestionService:
    def __init__(
        self,
        *,
        finnhub_adapter: FinnhubAdapter,
        normalization_service: EventNormalizationService,
        asset_mapping_service: AssetMappingService,
        db=None,
        gemini_event_classifier=None,
    ):
        self.finnhub_adapter = finnhub_adapter
        self.normalization_service = normalization_service
        self.asset_mapping_service = asset_mapping_service
        self.db = db
        self.gemini_event_classifier = gemini_event_classifier
        self._last_refresh_started_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    def set_database(self, db):
        self.db = db

    async def ingest_latest(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        news_category: str = "general",
        classify_with_gemini: bool = True,
    ) -> dict[str, Any]:
        started_at = time.time()
        if from_date is None or to_date is None:
            from_date, to_date = self.finnhub_adapter.default_date_range()

        stored_events: list[dict[str, Any]] = []
        mapped_assets_total = 0
        gemini_assessments_total = 0
        raw_item_count = 0
        error = ""
        degraded_reasons: list[str] = []

        try:
            calendar_items: list[dict[str, Any]] = []
            news_items: list[dict[str, Any]] = []

            try:
                calendar_items = await asyncio.to_thread(
                    self.finnhub_adapter.get_economic_calendar,
                    from_date,
                    to_date,
                )
            except FinnhubAdapterError as exc:
                degraded_reasons.append(f"economic_calendar: {exc}")

            try:
                news_items = await asyncio.to_thread(
                    self.finnhub_adapter.get_market_news,
                    news_category,
                )
            except FinnhubAdapterError as exc:
                degraded_reasons.append(f"market_news: {exc}")

            if not calendar_items and not news_items and degraded_reasons:
                raise FinnhubAdapterError(" | ".join(degraded_reasons))

            raw_item_count = len(calendar_items) + len(news_items)
            events = [
                *self.normalization_service.normalize_economic_calendar(calendar_items),
                *self.normalization_service.normalize_market_news(news_items),
            ]

            for event in events:
                stored = await self._store_event_pipeline(event, classify_with_gemini=classify_with_gemini)
                stored_events.append(stored)
                mapped_assets_total += len(stored.get("asset_mappings", []))
                gemini_assessments_total += 1 if stored.get("gemini_assessment") else 0

            success = True
            if degraded_reasons:
                error = " | ".join(degraded_reasons)
        except FinnhubAdapterError as exc:
            logger.warning("Finnhub ingestion degraded: %s", exc)
            error = str(exc)
            success = False
        except Exception as exc:  # pragma: no cover - defensive normalization
            logger.exception("Event ingestion failed")
            error = str(exc)
            success = False

        if self.db is not None:
            await self.db.log_event_fetch_run(
                provider="finnhub",
                started_at=started_at,
                finished_at=time.time(),
                success=success,
                item_count=raw_item_count,
                error=error,
            )

        return {
            "provider": "finnhub",
            "success": success,
            "error": error,
            "raw_item_count": raw_item_count,
            "stored_events": stored_events,
            "stored_event_count": len(stored_events),
            "mapped_assets_count": mapped_assets_total,
            "gemini_assessment_count": gemini_assessments_total,
            "degraded": (not success) or bool(degraded_reasons),
            "degraded_reasons": degraded_reasons,
            "from_date": from_date,
            "to_date": to_date,
        }

    async def latest_usable_events(self, limit: int = 25) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        return await self.db.get_latest_external_events(limit=limit, usable_only=True)

    async def maybe_refresh_latest(
        self,
        *,
        min_interval_seconds: float = 300.0,
        classify_with_gemini: bool = True,
    ) -> dict[str, Any] | None:
        if self.db is None or not self.finnhub_adapter.enabled:
            return None
        now = time.time()
        if now - self._last_refresh_started_at < min_interval_seconds:
            return None

        async with self._refresh_lock:
            now = time.time()
            if now - self._last_refresh_started_at < min_interval_seconds:
                return None
            self._last_refresh_started_at = now
            return await self.ingest_latest(classify_with_gemini=classify_with_gemini)

    async def latest_candidate_symbols(
        self,
        *,
        universe_service: SymbolUniverseService,
        market_symbols: list[dict[str, Any]],
        limit: int = 10,
        max_event_age_hours: float = 18.0,
    ) -> list[str]:
        if self.db is None:
            return []

        bundles = await self._load_recent_event_bundles(limit=60)
        if not bundles:
            return []

        active_market = universe_service.filter_market_symbols(market_symbols)
        if not active_market:
            return []

        candidates: dict[str, dict[str, Any]] = {}
        for event, mappings, assessment in bundles:
            if not self._is_recent_event(event, max_event_age_hours=max_event_age_hours):
                continue
            importance_bonus = {"low": 0.0, "medium": 0.06, "high": 0.12}.get(str(event.get("importance", "low")), 0.0)
            for mapping in mappings:
                if not bool(mapping.get("tradable", False)):
                    continue
                canonical = universe_service.canonical_symbol(mapping.get("symbol"))
                if not canonical:
                    continue
                resolved = universe_service.resolve_requested_symbols([canonical], active_market)
                if not resolved:
                    continue
                bias = self._assessment_bias_for_symbol(assessment, canonical)
                score = float(mapping.get("mapping_score", 0.0)) + importance_bonus
                score += max(-0.05, min(0.05, float((assessment or {}).get("confidence_adjustment", 0.0))))
                if bias != "neutral":
                    score += 0.05
                if (assessment or {}).get("event_risk") == "high" and bias == "neutral":
                    score -= 0.08

                existing = candidates.get(canonical)
                if existing is None or score > float(existing["score"]):
                    candidates[canonical] = {
                        "raw_symbol": resolved[0],
                        "score": round(score, 3),
                    }

        ordered = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
        return [item["raw_symbol"] for item in ordered[:limit] if item["score"] >= 0.65]

    async def recent_symbol_news(
        self,
        symbol: str,
        *,
        universe_service: SymbolUniverseService,
        limit: int = 8,
        max_event_age_hours: float = 24.0,
    ) -> list[NormalizedNewsItem]:
        if self.db is None:
            return []

        canonical = universe_service.canonical_symbol(symbol)
        bundles = await self._load_recent_event_bundles(limit=60)
        items: list[NormalizedNewsItem] = []
        for event, mappings, assessment in bundles:
            if not self._is_recent_event(event, max_event_age_hours=max_event_age_hours):
                continue
            if not self._event_relevant_to_symbol(
                canonical_symbol=canonical,
                event=event,
                mappings=mappings,
                assessment=assessment,
                universe_service=universe_service,
            ):
                continue

            affected_symbols = self._affected_symbols_for_event(
                event=event,
                mappings=mappings,
                assessment=assessment,
                universe_service=universe_service,
            )
            summary_parts = []
            if event.get("summary"):
                summary_parts.append(str(event["summary"]).strip())
            if (assessment or {}).get("summary_reason"):
                summary_parts.append(f"Gemini: {str(assessment['summary_reason']).strip()}")

            items.append(
                NormalizedNewsItem(
                    source=str(event.get("source", "finnhub")),
                    category=self._news_category_for_event(event),
                    title=str(event.get("title", "")).strip(),
                    summary=" ".join(part for part in summary_parts if part).strip(),
                    published_at=float(event.get("timestamp_utc", 0.0) or time.time()),
                    received_at=float(event.get("fetched_at", 0.0) or time.time()),
                    affected_symbols=affected_symbols or [canonical],
                    metadata={
                        "external_event_id": event.get("id"),
                        "event_type": event.get("event_type"),
                        "importance": event.get("importance"),
                        "country": event.get("country"),
                        "gemini_event_risk": (assessment or {}).get("event_risk", "low"),
                        "gemini_bias": self._assessment_bias_for_symbol(assessment, canonical),
                        "gemini_confidence_adjustment": float((assessment or {}).get("confidence_adjustment", 0.0)),
                    },
                )
            )

        items.sort(key=lambda item: item.published_at, reverse=True)
        return items[:limit]

    async def _store_event_pipeline(
        self,
        event: ExternalEvent,
        *,
        classify_with_gemini: bool,
    ) -> dict[str, Any]:
        event_id = None
        if self.db is not None:
            event_id = await self.db.save_external_event(event.model_dump())

        mappings = self.asset_mapping_service.map_event(event)
        if self.db is not None and event_id is not None:
            await self.db.save_event_asset_mappings(
                event_id,
                [mapping.model_dump() for mapping in mappings],
            )

        gemini_assessment: GeminiEventAssessment | None = None
        if classify_with_gemini and self.gemini_event_classifier is not None and event_id is not None:
            gemini_assessment = await self.gemini_event_classifier.classify_event(
                event=event,
                candidate_mappings=mappings,
            )
            await self.db.save_gemini_event_assessment(
                event_id,
                gemini_assessment.model_dump(),
                changed_mapping=bool(gemini_assessment.contradiction_flag or gemini_assessment.confidence_adjustment != 0.0),
            )

        return {
            "event_id": event_id,
            "event": event.model_dump(),
            "asset_mappings": [mapping.model_dump() for mapping in mappings],
            "gemini_assessment": gemini_assessment.model_dump() if gemini_assessment else None,
        }

    async def _load_recent_event_bundles(
        self,
        *,
        limit: int,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]]:
        if self.db is None:
            return []

        events = await self.db.get_latest_external_events(limit=limit, usable_only=True)
        if not events:
            return []
        mappings = await self.db.get_event_asset_mappings(limit=limit * 6)
        assessments = await self.db.get_latest_gemini_event_assessments(limit=limit * 4)

        mappings_by_event: dict[int, list[dict[str, Any]]] = {}
        for mapping in mappings:
            mappings_by_event.setdefault(int(mapping["external_event_id"]), []).append(mapping)

        assessments_by_event: dict[int, dict[str, Any]] = {}
        for assessment in assessments:
            event_id = int(assessment["external_event_id"])
            assessments_by_event.setdefault(event_id, assessment["assessment_json"])

        bundles: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]] = []
        for event in events:
            event_id = int(event.get("id", 0))
            bundles.append(
                (
                    event,
                    mappings_by_event.get(event_id, []),
                    assessments_by_event.get(event_id),
                )
            )
        return bundles

    def _is_recent_event(self, event: dict[str, Any], *, max_event_age_hours: float) -> bool:
        timestamp = float(event.get("timestamp_utc", 0.0) or 0.0)
        if timestamp <= 0:
            return False
        age_seconds = max(0.0, time.time() - timestamp)
        return age_seconds <= max_event_age_hours * 3600.0

    def _event_relevant_to_symbol(
        self,
        *,
        canonical_symbol: str,
        event: dict[str, Any],
        mappings: list[dict[str, Any]],
        assessment: dict[str, Any] | None,
        universe_service: SymbolUniverseService,
    ) -> bool:
        affected = {
            universe_service.canonical_symbol(symbol)
            for symbol in event.get("affected_assets", []) or []
            if universe_service.canonical_symbol(symbol)
        }
        mapped = {
            universe_service.canonical_symbol(mapping.get("symbol"))
            for mapping in mappings
            if universe_service.canonical_symbol(mapping.get("symbol"))
        }
        assessed = {
            universe_service.canonical_symbol(symbol)
            for symbol in (assessment or {}).get("affected_assets", []) or []
            if universe_service.canonical_symbol(symbol)
        }
        bias_by_asset = {
            universe_service.canonical_symbol(symbol): bias
            for symbol, bias in ((assessment or {}).get("bias_by_asset", {}) or {}).items()
            if universe_service.canonical_symbol(symbol)
        }
        return (
            canonical_symbol in affected
            or canonical_symbol in mapped
            or canonical_symbol in assessed
            or bias_by_asset.get(canonical_symbol, "neutral") != "neutral"
        )

    def _affected_symbols_for_event(
        self,
        *,
        event: dict[str, Any],
        mappings: list[dict[str, Any]],
        assessment: dict[str, Any] | None,
        universe_service: SymbolUniverseService,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        sources = [
            *(event.get("affected_assets", []) or []),
            *(mapping.get("symbol") for mapping in mappings),
            *((assessment or {}).get("affected_assets", []) or []),
            *(
                symbol
                for symbol, bias in ((assessment or {}).get("bias_by_asset", {}) or {}).items()
                if bias != "neutral"
            ),
        ]
        for symbol in sources:
            canonical = universe_service.canonical_symbol(symbol)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            ordered.append(canonical)
        return ordered

    def _assessment_bias_for_symbol(self, assessment: dict[str, Any] | None, canonical_symbol: str) -> str:
        bias = str(((assessment or {}).get("bias_by_asset", {}) or {}).get(canonical_symbol, "neutral")).lower()
        return bias if bias in {"bullish", "bearish", "neutral"} else "neutral"

    def _news_category_for_event(self, event: dict[str, Any]) -> str:
        event_type = str(event.get("event_type", "unknown")).lower()
        if event_type == "economic_calendar":
            return "calendar"
        if event_type in {"macro_event"}:
            return "macro_event"
        return "headline"
