from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import Any

import requests


logger = logging.getLogger(__name__)


class BenzingaAdapterError(RuntimeError):
    """Normalized Benzinga adapter error."""


class BenzingaAdapter:
    NEWS_URL = "https://api.benzinga.com/api/v2/news"
    ECONOMICS_URL = "https://api.benzinga.com/api/v2.1/calendar/economics"

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
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.session = session or requests.Session()
        self._last_error: str | None = None
        self._healthcheck_cache: dict[str, Any] | None = None
        self._healthcheck_cache_until: float = 0.0

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def default_date_range(self, *, lookback_days: int = 1, lookahead_days: int = 3) -> tuple[str, str]:
        today = date.today()
        return (
            (today - timedelta(days=lookback_days)).isoformat(),
            (today + timedelta(days=lookahead_days)).isoformat(),
        )

    def healthcheck(self, *, force_refresh: bool = False, cached_only: bool = False) -> dict[str, Any]:
        now = time.time()
        if self._healthcheck_cache and now < self._healthcheck_cache_until and not force_refresh:
            return dict(self._healthcheck_cache)

        baseline = {
            "provider": "benzinga",
            "enabled": bool(self.enabled),
            "available": False,
            "degraded": False,
            "reason": "",
            "capabilities": {
                "market_news": False,
                "economic_calendar": False,
            },
            "news_count": 0,
            "calendar_count": 0,
        }
        if not self.enabled:
            baseline["reason"] = "Benzinga is disabled by configuration."
            self._healthcheck_cache = dict(baseline)
            self._healthcheck_cache_until = now + 15.0
            return dict(baseline)
        if not self.api_key:
            baseline["degraded"] = True
            baseline["reason"] = "BENZINGA_API_KEY missing while ENABLE_BENZINGA=true."
            self._last_error = str(baseline["reason"])
            self._healthcheck_cache = dict(baseline)
            self._healthcheck_cache_until = now + 30.0
            return dict(baseline)
        if cached_only and self._healthcheck_cache is not None:
            return dict(self._healthcheck_cache)
        if cached_only:
            return {
                **baseline,
                "available": True,
                "reason": "Cached Benzinga health not ready yet.",
            }

        news_ok = False
        calendar_ok = False
        news_error = ""
        calendar_error = ""
        news_count = 0
        calendar_count = 0

        try:
            sample_news = self.get_market_news(limit=1)
            news_count = len(sample_news)
            news_ok = True
        except BenzingaAdapterError as exc:
            news_error = str(exc)

        try:
            from_date, to_date = self.default_date_range()
            sample_calendar = self.get_economic_calendar(from_date, to_date, limit=1)
            calendar_count = len(sample_calendar)
            calendar_ok = True
        except BenzingaAdapterError as exc:
            calendar_error = str(exc)

        available = news_ok or calendar_ok
        degraded = not available
        reason_parts = []
        if news_error:
            reason_parts.append(f"news: {news_error}")
        if calendar_error:
            reason_parts.append(f"calendar: {calendar_error}")
        reason = " | ".join(reason_parts)
        self._last_error = reason or None

        payload = {
            "provider": "benzinga",
            "enabled": True,
            "available": available,
            "degraded": degraded,
            "reason": reason,
            "capabilities": {
                "market_news": news_ok,
                "economic_calendar": calendar_ok,
            },
            "news_count": news_count,
            "calendar_count": calendar_count,
        }
        self._healthcheck_cache = dict(payload)
        self._healthcheck_cache_until = now + (90.0 if degraded else 45.0)
        return dict(payload)

    def get_market_news(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        payload = self._get(
            self.NEWS_URL,
            params={
                "page": 0,
                "pagesize": max(1, min(int(limit or 50), 100)),
            },
        )
        if isinstance(payload, dict):
            items = payload.get("news") or payload.get("data") or payload.get("items") or []
        else:
            items = payload
        normalized = [dict(item) for item in (items or []) if isinstance(item, dict)]
        return normalized[:limit] if limit else normalized

    def get_economic_calendar(
        self,
        from_date: str,
        to_date: str,
        *,
        limit: int | None = None,
        importance: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": 0,
            "pagesize": max(1, min(int(limit or 50), 1000)),
            "parameters[date_from]": from_date,
            "parameters[date_to]": to_date,
        }
        if importance is not None:
            params["parameters[importance]"] = int(importance)
        payload = self._get(self.ECONOMICS_URL, params=params)
        if isinstance(payload, dict):
            items = payload.get("economics") or payload.get("calendar") or payload.get("data") or []
        else:
            items = payload
        normalized = [dict(item) for item in (items or []) if isinstance(item, dict)]
        return normalized[:limit] if limit else normalized

    def _get(self, url: str, *, params: dict[str, Any]) -> Any:
        if not self.enabled:
            raise BenzingaAdapterError("Benzinga is disabled by configuration.")
        if not self.api_key:
            raise BenzingaAdapterError("BENZINGA_API_KEY missing while ENABLE_BENZINGA=true.")

        request_params = dict(params)
        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(
                    url,
                    params=request_params,
                    headers={
                        "accept": "application/json",
                        "Authorization": f"token {self.api_key}",
                    },
                    timeout=(3.05, self.timeout_seconds),
                )
                if response.status_code >= 400:
                    response_detail = self._redact_sensitive_text(response.text[:200])
                    raise BenzingaAdapterError(
                        f"Benzinga request failed with HTTP {response.status_code}: {response_detail}"
                    )
                payload = response.json()
                self._last_error = None
                return payload
            except BenzingaAdapterError as exc:
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

        message = self._redact_sensitive_text(f"Benzinga request failed for {url}: {last_error}")
        logger.warning(message)
        raise BenzingaAdapterError(message) from last_error

    def _redact_sensitive_text(self, value: object) -> str:
        text = str(value or "")
        if self.api_key:
            text = text.replace(self.api_key, "[REDACTED]")
        return re.sub(
            r"(?i)(token(?:=|%3D)|authorization:\s*token\s+)[^&\s\"']+",
            r"\1[REDACTED]",
            text,
        )
