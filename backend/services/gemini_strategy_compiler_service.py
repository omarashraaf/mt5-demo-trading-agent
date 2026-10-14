from __future__ import annotations

import logging
import os
import re
from typing import Any

from services.nvidia_chat_client import (
    NvidiaChatCompletionsClient,
    nvidia_api_key,
    nvidia_model_name,
    nvidia_provider_enabled,
)

logger = logging.getLogger(__name__)


class GeminiStrategyCompilerService:
    """Lightweight strategy compiler wrapper.

    The router already has a deterministic compiler. This service adds optional
    AI interpretation when available, but must never block strategy creation.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        max_retries: int = 0,
        model_name: str | None = None,
    ):
        self.api_key = (api_key or nvidia_api_key(os.getenv("GEMINI_API_KEY", ""))).strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.model_name = (model_name or os.getenv("GEMINI_MODEL", "gemma-4-26b-a4b-it")).strip() or "gemma-4-26b-a4b-it"
        self.active_model_name = self.model_name
        self._client = None
        self._unavailable_reason = ""
        self._last_error = ""
        self._quota_cooldown_until = 0.0
        self._model_cooldown_until: dict[str, float] = {}
        self._init_client()

    @property
    def available(self) -> bool:
        return bool(self._client)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Gemini strategy compiler unavailable."

    @property
    def last_error(self) -> str:
        return self._last_error

    def _init_client(self):
        # Keep this service non-fatal when google-genai is missing or disabled.
        if nvidia_provider_enabled(api_key=self.api_key, model_name=self.model_name):
            if not self.api_key:
                self._client = None
                self._unavailable_reason = "NVIDIA_API_KEY not set"
                return
            try:
                self.model_name = nvidia_model_name(self.model_name)
                self.active_model_name = self.model_name
                self._client = NvidiaChatCompletionsClient(
                    api_key=self.api_key,
                    timeout_seconds=self.timeout_seconds,
                )
                self._unavailable_reason = ""
            except Exception as exc:
                self._client = None
                self._unavailable_reason = str(exc)
            return

        if not self.api_key:
            self._client = None
            self._unavailable_reason = "GEMINI_API_KEY not set"
            return
        try:
            from google import genai  # type: ignore
        except Exception as exc:
            self._client = None
            self._unavailable_reason = f"google-genai dependency unavailable: {exc}"
            return
        try:
            self._client = genai.Client(api_key=self.api_key)
            self._unavailable_reason = ""
        except Exception as exc:
            self._client = None
            self._unavailable_reason = str(exc)

    async def compile(
        self,
        *,
        name: str,
        description: str,
        apply_under_any_conditions: bool,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds, max_retries
        # Resume safely from the crashed state: emit conservative hints only.
        # The deterministic compiler in routes remains the source of truth.
        text = f"{name} {description}".strip()
        lower = text.lower()
        symbols = self._extract_symbols(lower)
        asset_classes = self._extract_asset_classes(lower)
        entry_action = "SELL" if " sell " in f" {lower} " and " buy " not in f" {lower} " else "BUY"
        fixed_lot_size = self._extract_lot_size(lower)
        allocation_pct = self._extract_pct(lower)
        buy_every = self._extract_duration(lower, r"\bevery\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b")
        sell_after = self._extract_duration(lower, r"\bafter\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b")
        hold_for = self._extract_duration(lower, r"\bfor\s+(\d+(?:\.\d+)?)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b")

        return {
            "used": False,
            "available": bool(self.available),
            "degraded": not bool(self.available),
            "error": self._unavailable_reason or None,
            "symbols": symbols,
            "asset_classes": asset_classes,
            "market_selection_mode": self._market_selection_mode(lower, asset_classes, symbols),
            "market_region_hint": "egypt" if ("egypt" in lower or "cairo" in lower) else None,
            "entry_action": entry_action,
            "fixed_lot_size": fixed_lot_size,
            "allocation_pct_free_margin": allocation_pct,
            "cadence_buy_every_seconds": buy_every,
            "cadence_sell_after_seconds": sell_after or hold_for,
            "hold_for_seconds": hold_for,
            "policy_mode": self._policy_mode(lower),
            "gemini_role": self._gemini_role(lower),
            "scan_interval_seconds": self._scan_interval(lower),
            "min_reward_risk": self._min_reward_risk(lower),
            "human_summary": self._human_summary(name=name, description=description, apply_under_any_conditions=apply_under_any_conditions),
            "assumptions": self._assumptions(symbols, asset_classes, fixed_lot_size, allocation_pct),
            "missing_details": self._missing_details(symbols, buy_every, sell_after or hold_for),
            "notes": "Gemini compiler source was unavailable after the interrupted session; deterministic strategy parsing is active.",
        }

    def _extract_symbols(self, lower: str) -> list[str]:
        aliases = {
            "gold": "XAUUSD",
            "xauusd": "XAUUSD",
            "silver": "XAGUSD",
            "xagusd": "XAGUSD",
            "nasdaq": "US100",
            "us100": "US100",
            "sp500": "US500",
            "s&p 500": "US500",
            "us500": "US500",
            "dow": "US30",
            "dow jones": "US30",
            "us30": "US30",
            "dax": "GER40",
            "ger40": "GER40",
            "ftse": "UK100",
            "uk100": "UK100",
            "wti": "WTI",
            "brent": "BRENT",
            "oil": "WTI",
            "aapl": "AAPL",
            "apple": "AAPL",
            "msft": "MSFT",
            "microsoft": "MSFT",
            "nvda": "NVDA",
            "nvidia": "NVDA",
            "amd": "AMD",
            "amzn": "AMZN",
            "amazon": "AMZN",
            "tsla": "TSLA",
            "tesla": "TSLA",
        }
        found: list[str] = []
        for token, symbol in aliases.items():
            if token in lower and symbol not in found:
                found.append(symbol)
        return found

    def _extract_asset_classes(self, lower: str) -> list[str]:
        found: list[str] = []
        if "cfd" in lower:
            found.extend(["Indices", "Commodities"])
        if "commodit" in lower:
            found.append("Commodities")
        if "stock" in lower or "equit" in lower:
            found.append("Stocks")
        if "indice" in lower or "index" in lower:
            found.append("Indices")
        deduped: list[str] = []
        for asset_class in found:
            if asset_class not in deduped:
                deduped.append(asset_class)
        return deduped

    def _market_selection_mode(self, lower: str, asset_classes: list[str], symbols: list[str]) -> str | None:
        if symbols:
            return None
        if "top commodity" in lower or "best commodity" in lower:
            return "top_commodity"
        if "top stock" in lower or "best stock" in lower:
            return "top_stock"
        if "top index" in lower or "best index" in lower:
            return "top_index"
        if "cfd" in lower:
            return "asset_class_filter"
        if asset_classes:
            return "asset_class_filter"
        return None

    def _policy_mode(self, lower: str) -> str:
        if any(token in lower for token in ("safe", "conservative", "low risk", "capital protect")):
            return "safe"
        if any(token in lower for token in ("aggressive", "high risk", "scalp", "fast", "cfd")):
            return "aggressive"
        return "balanced"

    def _gemini_role(self, lower: str) -> str:
        if any(token in lower for token in ("no ai", "without ai", "disable gemini")):
            return "off"
        if any(token in lower for token in ("confirm", "strict ai", "confirmation")):
            return "confirmation-required"
        return "advisory"

    def _scan_interval(self, lower: str) -> int:
        if any(token in lower for token in ("fast", "scalp", "quick")):
            return 20
        if any(token in lower for token in ("slow", "swing", "position")):
            return 40
        return 30

    def _min_reward_risk(self, lower: str) -> float:
        mode = self._policy_mode(lower)
        if mode == "safe":
            return 2.2
        if mode == "aggressive":
            return 1.6
        return 1.8

    def _extract_lot_size(self, lower: str) -> float | None:
        match = re.search(r"\b(\d+(?:\.\d+)?)\s*lot(?:s)?\b", lower)
        if not match:
            return None
        try:
            return max(0.01, min(100.0, float(match.group(1))))
        except Exception:
            return None

    def _extract_pct(self, lower: str) -> float | None:
        match = re.search(r"\b(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:my\s+)?(?:(?:free\s+)?margin)\b", lower)
        if not match:
            return None
        try:
            return max(0.1, min(100.0, float(match.group(1))))
        except Exception:
            return None

    def _extract_duration(self, lower: str, pattern: str) -> int:
        match = re.search(pattern, lower)
        if not match:
            return 0
        amount = float(match.group(1))
        unit = match.group(2).lower()
        multiplier = 60
        if unit.startswith("sec"):
            multiplier = 1
        elif unit.startswith("hour") or unit in {"hr", "hrs"}:
            multiplier = 3600
        elif unit.startswith("day"):
            multiplier = 86400
        return max(0, int(amount * multiplier))

    def _human_summary(self, *, name: str, description: str, apply_under_any_conditions: bool) -> str:
        summary = f"{name.strip() or 'Custom strategy'}: {description.strip()}".strip(": ")
        if apply_under_any_conditions:
            summary += " Force mode is enabled."
        return summary

    def _assumptions(
        self,
        symbols: list[str],
        asset_classes: list[str],
        fixed_lot_size: float | None,
        allocation_pct: float | None,
    ) -> list[str]:
        items: list[str] = []
        if not symbols and asset_classes:
            items.append("No exact symbol was supplied, so the runtime market universe will choose instruments inside the requested asset classes.")
        if fixed_lot_size is None and allocation_pct is None:
            items.append("Position sizing will fall back to the platform risk-sizing rules.")
        return items

    def _missing_details(self, symbols: list[str], buy_every: int, sell_after: int) -> list[str]:
        items: list[str] = []
        if not symbols:
            items.append("Exact symbols were not specified.")
        if buy_every <= 0:
            items.append("Entry cadence was not specified.")
        if sell_after <= 0:
            items.append("Exit timing was not specified.")
        return items
