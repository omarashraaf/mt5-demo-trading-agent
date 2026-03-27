from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from domain.models import ExternalEvent


class EventNormalizationService:
    def normalize_economic_calendar(
        self,
        items: list[dict[str, Any]],
        *,
        source: str = "finnhub",
    ) -> list[ExternalEvent]:
        events: list[ExternalEvent] = []
        fetched_at = time.time()
        for item in items:
            title = str(item.get("event") or item.get("title") or "").strip()
            if not title:
                continue
            timestamp = self._timestamp(
                item.get("time")
                or item.get("datetime")
                or item.get("date")
            )
            event = ExternalEvent(
                source=source,
                source_event_id=str(item.get("id") or f"{item.get('date', '')}:{title}"),
                dedupe_key=self._dedupe_key(source, "economic_calendar", item),
                title=title,
                summary=str(item.get("country") or "").strip(),
                timestamp_utc=timestamp or fetched_at,
                event_type="economic_calendar",
                category=str(item.get("impact") or item.get("importance") or "calendar"),
                country=str(item.get("country") or "").upper(),
                importance=self._importance(item.get("impact") or item.get("importance")),
                actual=item.get("actual"),
                forecast=item.get("estimate") or item.get("forecast"),
                previous=item.get("prev") or item.get("previous"),
                affected_assets=self._affected_assets_from_calendar(item),
                raw_payload=item,
                fetched_at=fetched_at,
                usable=bool(timestamp),
                usability_reason="" if timestamp else "Calendar event timestamp missing.",
            )
            events.append(event)
        return events

    def normalize_market_news(
        self,
        items: list[dict[str, Any]],
        *,
        source: str = "finnhub",
    ) -> list[ExternalEvent]:
        events: list[ExternalEvent] = []
        fetched_at = time.time()
        for item in items:
            title = str(item.get("headline") or item.get("title") or "").strip()
            if not title:
                continue
            timestamp = self._timestamp(item.get("datetime") or item.get("time"))
            summary = str(item.get("summary") or item.get("description") or "").strip()
            related = str(item.get("related") or "").upper()
            affected_assets = [token for token in related.replace(",", " ").split() if token.strip()]
            events.append(
                ExternalEvent(
                    source=source,
                    source_event_id=str(item.get("id") or item.get("url") or title),
                    dedupe_key=self._dedupe_key(source, "market_news", item),
                    title=title,
                    summary=summary,
                    timestamp_utc=timestamp or fetched_at,
                    event_type="market_news",
                    category=str(item.get("category") or "general"),
                    country=str(item.get("country") or "").upper(),
                    importance=self._importance(item.get("importance") or item.get("category")),
                    affected_assets=affected_assets,
                    raw_payload=item,
                    fetched_at=fetched_at,
                    usable=bool(timestamp),
                    usability_reason="" if timestamp else "Market news timestamp missing.",
                )
            )
        return events

    def normalize_company_news(
        self,
        symbol: str,
        items: list[dict[str, Any]],
        *,
        source: str = "finnhub",
    ) -> list[ExternalEvent]:
        events = self.normalize_market_news(items, source=source)
        normalized: list[ExternalEvent] = []
        for event in events:
            normalized.append(
                event.model_copy(
                    update={
                        "event_type": "company_news",
                        "affected_assets": list(dict.fromkeys([symbol.upper(), *event.affected_assets])),
                    }
                )
            )
        return normalized

    def _importance(self, raw: Any) -> str:
        cleaned = str(raw or "").strip().lower()
        if cleaned in {"high", "h", "3", "red"}:
            return "high"
        if cleaned in {"medium", "med", "m", "2", "yellow"}:
            return "medium"
        return "low"

    def _affected_assets_from_calendar(self, item: dict[str, Any]) -> list[str]:
        country = str(item.get("country") or "").upper()
        title = str(item.get("event") or item.get("title") or "").lower()
        assets: list[str] = []
        if country in {"US", "USA"} or any(token in title for token in ("fed", "fomc", "cpi", "pce", "payroll", "nfp", "jobs", "inflation")):
            assets.extend(["US100", "US500", "US30", "GOLD"])
        if country in {"DE", "GER", "EU", "EMU"} or any(token in title for token in ("ecb", "euro", "german", "eurozone")):
            assets.extend(["GER40", "GOLD"])
        if any(token in title for token in ("oil", "opec", "crude", "inventory", "wti", "brent")):
            assets.extend(["WTI", "BRENT"])
        return list(dict.fromkeys(assets))

    def _dedupe_key(self, source: str, event_type: str, payload: dict[str, Any]) -> str:
        stable = json.dumps(payload, sort_keys=True, default=str)
        digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()
        return f"{source}:{event_type}:{digest}"

    def _timestamp(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            raw = float(value)
            if raw > 10_000_000_000:
                raw = raw / 1000.0
            return raw
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        cleaned = str(value).strip()
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
