from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from domain.models import MarketContext, NormalizedNewsItem


NewsProvider = Callable[..., Iterable[dict[str, Any]] | Awaitable[Iterable[dict[str, Any]]]]


class NewsIngestionService:
    """Fetches and normalizes news inputs from pluggable providers.

    Providers are intentionally injected so the trading pipeline can consume
    headlines, macro events, and calendar data without hard-coding a single
    external vendor into core decision logic.
    """

    def __init__(
        self,
        headline_providers: list[NewsProvider] | None = None,
        macro_event_providers: list[NewsProvider] | None = None,
        calendar_providers: list[NewsProvider] | None = None,
    ):
        self.headline_providers = headline_providers or []
        self.macro_event_providers = macro_event_providers or []
        self.calendar_providers = calendar_providers or []

    async def ingest_for_context(
        self,
        context: MarketContext,
        *,
        max_items: int = 25,
    ) -> list[NormalizedNewsItem]:
        raw_items: list[dict[str, Any]] = []
        raw_items.extend(await self._collect(self.headline_providers, context=context, category="headline"))
        raw_items.extend(await self._collect(self.macro_event_providers, context=context, category="macro_event"))
        raw_items.extend(await self._collect(self.calendar_providers, context=context, category="calendar"))

        normalized: list[NormalizedNewsItem] = []
        for item in raw_items:
            news_item = self._normalize_item(item, context=context)
            if not news_item:
                continue
            if self._is_relevant(news_item, context):
                normalized.append(news_item)

        normalized.sort(key=lambda item: item.published_at, reverse=True)
        return normalized[:max_items]

    async def _collect(
        self,
        providers: list[NewsProvider],
        *,
        context: MarketContext,
        category: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for provider in providers:
            try:
                result = provider(symbol=context.symbol, context=context)
                if asyncio.iscoroutine(result):
                    result = await result
                for raw in result or []:
                    payload = dict(raw)
                    payload.setdefault("category", category)
                    items.append(payload)
            except Exception:
                continue
        return items

    def _normalize_item(
        self,
        raw: dict[str, Any],
        *,
        context: MarketContext,
    ) -> NormalizedNewsItem | None:
        title = str(raw.get("title") or raw.get("headline") or "").strip()
        if not title:
            return None

        summary = str(raw.get("summary") or raw.get("description") or "").strip()
        source = str(raw.get("source") or raw.get("provider") or "unknown").strip() or "unknown"
        affected_symbols = [
            str(symbol).upper()
            for symbol in raw.get("affected_symbols", raw.get("symbols", [])) or []
            if str(symbol).strip()
        ]
        if not affected_symbols and context.symbol:
            affected_symbols = [context.symbol.upper()]

        published_at = self._normalize_timestamp(raw.get("published_at") or raw.get("timestamp") or raw.get("time"))
        received_at = self._normalize_timestamp(raw.get("received_at")) or time.time()

        return NormalizedNewsItem(
            source=source,
            category=str(raw.get("category") or "headline"),
            title=title,
            summary=summary,
            published_at=published_at or received_at,
            received_at=received_at,
            affected_symbols=affected_symbols,
            url=str(raw.get("url") or raw.get("link") or "") or None,
            metadata={
                key: value
                for key, value in raw.items()
                if key not in {
                    "source",
                    "provider",
                    "title",
                    "headline",
                    "summary",
                    "description",
                    "published_at",
                    "received_at",
                    "timestamp",
                    "time",
                    "affected_symbols",
                    "symbols",
                    "url",
                    "link",
                    "category",
                }
            },
        )

    def _is_relevant(self, item: NormalizedNewsItem, context: MarketContext) -> bool:
        if not item.affected_symbols:
            return True
        target = context.symbol.upper()
        category = (context.symbol_info.category if context.symbol_info else "").upper()
        return target in item.affected_symbols or category in item.affected_symbols

    def _normalize_timestamp(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except ValueError:
                pass
            try:
                dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                return None
        return None
