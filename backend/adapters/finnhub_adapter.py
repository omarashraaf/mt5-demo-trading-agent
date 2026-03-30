from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import requests


logger = logging.getLogger(__name__)


class FinnhubAdapterError(RuntimeError):
    """Normalized Finnhub adapter error."""


class FinnhubAdapter:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(
        self,
        *,
        api_key: str = "",
        enabled: bool = False,
        timeout_seconds: float = 8.0,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key.strip()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def healthcheck(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "provider": "finnhub",
                "enabled": False,
                "available": False,
                "degraded": False,
                "reason": "Finnhub is disabled by configuration.",
            }
        if not self.api_key:
            self._last_error = "FINNHUB_API_KEY missing while ENABLE_FINNHUB=true."
            return {
                "provider": "finnhub",
                "enabled": True,
                "available": False,
                "degraded": True,
                "reason": self._last_error,
            }

        news_ok = False
        calendar_ok = False
        news_error = ""
        calendar_error = ""
        sample_count = 0
        calendar_count = 0

        try:
            sample = self.get_market_news(category="general", limit=1)
            sample_count = len(sample)
            news_ok = True
        except FinnhubAdapterError as exc:
            news_error = str(exc)

        try:
            from_date, to_date = self.default_date_range()
            calendar_items = self.get_economic_calendar(from_date, to_date)
            calendar_count = len(calendar_items)
            calendar_ok = True
        except FinnhubAdapterError as exc:
            calendar_error = str(exc)

        available = news_ok or calendar_ok
        # Treat "news works but calendar unavailable" as partial/news-only mode,
        # not a degraded outage.
        partial_news_only = news_ok and not calendar_ok
        degraded = (not available) or (not news_ok and calendar_ok)
        reason_parts = []
        if news_error:
            reason_parts.append(f"news: {news_error}")
        if calendar_error:
            reason_parts.append(f"calendar: {calendar_error}")
        reason = " | ".join(reason_parts)
        self._last_error = reason or None

        return {
            "provider": "finnhub",
            "enabled": True,
            "available": available,
            "degraded": degraded,
            "partial_news_only": partial_news_only,
            "status": "news_only" if partial_news_only else ("healthy" if available and not degraded else "degraded"),
            "reason": reason,
            "capabilities": {
                "market_news": news_ok,
                "economic_calendar": calendar_ok,
            },
            "sample_count": sample_count,
            "calendar_count": calendar_count,
        }

    def get_economic_calendar(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        payload = self._get(
            "/calendar/economic",
            params={"from": from_date, "to": to_date},
        )
        events = payload.get("economicCalendar") if isinstance(payload, dict) else payload
        return [dict(item) for item in (events or []) if isinstance(item, dict)]

    def get_market_news(self, category: str = "general", limit: int | None = None) -> list[dict[str, Any]]:
        payload = self._get("/news", params={"category": category})
        items = [dict(item) for item in (payload or []) if isinstance(item, dict)]
        return items[:limit] if limit else items

    def get_company_news(self, symbol: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
        payload = self._get(
            "/company-news",
            params={"symbol": symbol, "from": from_date, "to": to_date},
        )
        return [dict(item) for item in (payload or []) if isinstance(item, dict)]

    def default_date_range(self, *, lookback_days: int = 1, lookahead_days: int = 3) -> tuple[str, str]:
        today = date.today()
        return (
            (today - timedelta(days=lookback_days)).isoformat(),
            (today + timedelta(days=lookahead_days)).isoformat(),
        )

    def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        if not self.enabled:
            raise FinnhubAdapterError("Finnhub is disabled by configuration.")
        if not self.api_key:
            raise FinnhubAdapterError("FINNHUB_API_KEY missing while ENABLE_FINNHUB=true.")

        request_params = dict(params)
        request_params["token"] = self.api_key

        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(
                    f"{self.BASE_URL}{path}",
                    params=request_params,
                    timeout=self.timeout_seconds,
                )
                if response.status_code >= 400:
                    raise FinnhubAdapterError(
                        f"Finnhub request failed with HTTP {response.status_code}: {response.text[:200]}"
                    )
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    raise FinnhubAdapterError(f"Finnhub returned an error: {payload['error']}")
                self._last_error = None
                return payload
            except FinnhubAdapterError as exc:
                last_error = exc
                break
            except (requests.Timeout, requests.ConnectionError, ValueError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(1.5, 0.25 * attempt))
                    continue
                break
            except Exception as exc:  # pragma: no cover - defensive normalization
                last_error = exc
                break

        message = f"Finnhub request failed for {path}: {last_error}"
        logger.warning(message)
        raise FinnhubAdapterError(message) from last_error
