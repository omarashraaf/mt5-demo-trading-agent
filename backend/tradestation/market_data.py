from __future__ import annotations

import logging
import math
import time
from typing import Optional

from pydantic import BaseModel


logger = logging.getLogger(__name__)


class TickData(BaseModel):
    symbol: str
    bid: float
    ask: float
    spread: float
    time: int


class BarData(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int


TRADESTATION_PROXY_MAP: dict[str, dict[str, object]] = {
    "US500": {"broker_symbol": "SPY", "description": "S&P 500 proxy via SPY", "category": "Indices"},
    "US100": {"broker_symbol": "QQQ", "description": "Nasdaq 100 proxy via QQQ", "category": "Indices"},
    "US30": {"broker_symbol": "DIA", "description": "Dow 30 proxy via DIA", "category": "Indices"},
    "GER40": {"broker_symbol": "EWG", "description": "Germany proxy via EWG", "category": "Indices"},
    "UK100": {"broker_symbol": "EWU", "description": "UK proxy via EWU", "category": "Indices"},
    "GOLD": {"broker_symbol": "GLD", "description": "Gold proxy via GLD", "category": "Commodities"},
    "SILVER": {"broker_symbol": "SLV", "description": "Silver proxy via SLV", "category": "Commodities"},
    "WTI": {"broker_symbol": "USO", "description": "WTI crude proxy via USO", "category": "Commodities"},
    "BRENT": {"broker_symbol": "BNO", "description": "Brent crude proxy via BNO", "category": "Commodities"},
    "AAPL": {"broker_symbol": "AAPL", "description": "Apple Inc.", "category": "Stocks"},
    "MSFT": {"broker_symbol": "MSFT", "description": "Microsoft Corp.", "category": "Stocks"},
    "NVDA": {"broker_symbol": "NVDA", "description": "NVIDIA Corp.", "category": "Stocks"},
    "AMD": {"broker_symbol": "AMD", "description": "Advanced Micro Devices", "category": "Stocks"},
    "AMZN": {"broker_symbol": "AMZN", "description": "Amazon.com Inc.", "category": "Stocks"},
    "TSLA": {"broker_symbol": "TSLA", "description": "Tesla Inc.", "category": "Stocks"},
}

TRADESTATION_REVERSE_PROXY_MAP = {
    str(meta.get("broker_symbol") or "").upper(): canonical
    for canonical, meta in TRADESTATION_PROXY_MAP.items()
    if str(meta.get("broker_symbol") or "").strip()
}


class TradeStationMarketDataService:
    def __init__(self, connector):
        self.connector = connector

    def enable_symbol(self, symbol: str) -> bool:
        return bool(self.get_symbol_info(symbol))

    def get_tick(self, symbol: str) -> Optional[TickData]:
        canonical = self._canonical_symbol(symbol)
        quote = self.connector.get_quote_snapshot(self._broker_symbol(symbol))
        return self._tick_from_quote(canonical, quote or {})

    def get_ticks(self, symbols: list[str]) -> dict[str, TickData]:
        out: dict[str, TickData] = {}
        broker_map = {self._broker_symbol(symbol): self._canonical_symbol(symbol) for symbol in symbols if str(symbol or "").strip()}
        if not broker_map:
            return out
        bulk = self.connector.get_quote_snapshots(list(broker_map))
        for broker_symbol, quote in bulk.items():
            canonical = broker_map.get(broker_symbol, broker_symbol)
            tick = self._tick_from_quote(canonical, quote or {})
            if tick is not None:
                out[canonical] = tick
        return out

    def get_bars(self, symbol: str, timeframe: str, count: int = 100) -> list[BarData]:
        interval, unit = self._timeframe_params(timeframe)
        bars = self.connector.get_bars(
            self._broker_symbol(symbol),
            interval=interval,
            unit=unit,
            barsback=max(1, int(count)),
        )
        rows: list[BarData] = []
        for bar in bars[-max(1, int(count)):]:
            epoch = int(self._to_float(bar.get("Epoch") or bar.get("epoch") or 0.0))
            if epoch > 10_000_000_000:
                epoch = int(epoch / 1000.0)
            rows.append(
                BarData(
                    time=epoch or int(time.time()),
                    open=self._to_float(bar.get("Open") or bar.get("open")),
                    high=self._to_float(bar.get("High") or bar.get("high")),
                    low=self._to_float(bar.get("Low") or bar.get("low")),
                    close=self._to_float(bar.get("Close") or bar.get("close")),
                    volume=int(self._to_float(bar.get("TotalVolume") or bar.get("volume") or 0.0)),
                )
            )
        return rows

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        canonical = self._canonical_symbol(symbol)
        catalog = dict(TRADESTATION_PROXY_MAP.get(canonical, {}))
        broker_symbol = str(catalog.get("broker_symbol") or canonical or "").upper()
        if not broker_symbol:
            return None
        quote = self.connector.get_quote_snapshot(broker_symbol) or {}
        tick = self._tick_from_quote(canonical, quote) if quote else None
        point = self._point_from_quote(quote)
        return {
            "name": canonical,
            "broker_symbol": broker_symbol,
            "description": str(catalog.get("description") or canonical),
            "path": f"TradeStation/{catalog.get('category') or self._category_for_symbol(canonical)}",
            "category": str(catalog.get("category") or self._category_for_symbol(canonical)),
            "point": point,
            "digits": 2 if point >= 0.01 else 4,
            "trade_contract_size": 1.0,
            "trade_tick_value": 1.0,
            "trade_tick_size": point,
            "volume_min": 1.0,
            "volume_max": 100000.0,
            "volume_step": 1.0,
            "trade_mode": 4,
            "visible": True,
            "trade_stops_level": 0.0,
            "commission_per_lot_side": 0.0,
            "commission_round_turn_per_lot": 0.0,
            "commission_model": "unknown_or_zero",
            "commission_percent_rate": None,
            "commission_notional_1lot": None,
            "bid": tick.bid if tick else 0.0,
            "ask": tick.ask if tick else 0.0,
            "trade_enabled": bool(tick and tick.bid > 0 and tick.ask > 0),
        }

    def get_all_symbols(self) -> list[dict]:
        rows: list[dict] = []
        bulk = self.get_ticks(list(TRADESTATION_PROXY_MAP))
        for canonical in TRADESTATION_PROXY_MAP:
            info = self.get_symbol_info(canonical) or {}
            tick = bulk.get(canonical)
            rows.append(
                {
                    **info,
                    "visible": True,
                    "trade_enabled": bool(tick and tick.bid > 0 and tick.ask > 0),
                    "spread": tick.spread if tick else 0.0,
                    "bid": tick.bid if tick else 0.0,
                    "ask": tick.ask if tick else 0.0,
                }
            )
        return rows

    def get_symbols_by_category(self, category: str) -> list[dict]:
        requested = str(category or "").strip().lower()
        return [row for row in self.get_all_symbols() if str(row.get("category", "")).lower() == requested]

    def get_visible_symbols(self) -> list[dict]:
        return self.get_all_symbols()

    def get_tradeable_symbols(self) -> list[dict]:
        return [row for row in self.get_all_symbols() if row.get("trade_enabled")]

    def _tick_from_quote(self, canonical: str, quote: dict) -> Optional[TickData]:
        bid = self._to_float(quote.get("Bid") or quote.get("bid") or quote.get("Last") or quote.get("last"))
        ask = self._to_float(quote.get("Ask") or quote.get("ask") or quote.get("Last") or quote.get("last"))
        if bid <= 0 and ask > 0:
            bid = ask
        if ask <= 0 and bid > 0:
            ask = bid
        if bid <= 0 or ask <= 0:
            return None
        point = self._point_from_quote(quote)
        spread = round((ask - bid) / point) if point > 0 else 0
        ts = int(self._to_float(quote.get("Epoch") or quote.get("epoch") or time.time()))
        if ts > 10_000_000_000:
            ts = int(ts / 1000.0)
        return TickData(symbol=canonical, bid=bid, ask=ask, spread=spread, time=ts)

    def _broker_symbol(self, symbol: str) -> str:
        canonical = self._canonical_symbol(symbol)
        return str(TRADESTATION_PROXY_MAP.get(canonical, {}).get("broker_symbol") or canonical).upper()

    def _canonical_symbol(self, symbol: str) -> str:
        cleaned = str(symbol or "").strip().upper()
        return TRADESTATION_REVERSE_PROXY_MAP.get(cleaned, cleaned)

    def _category_for_symbol(self, canonical: str) -> str:
        if canonical in {"US500", "US100", "US30", "GER40", "UK100"}:
            return "Indices"
        if canonical in {"GOLD", "SILVER", "WTI", "BRENT"}:
            return "Commodities"
        return "Stocks"

    def _point_from_quote(self, quote: dict) -> float:
        min_move = self._to_float(quote.get("MinMove") or quote.get("minMove") or 0.01)
        price_scale = self._to_float(quote.get("PriceScale") or quote.get("priceScale") or 1.0)
        if min_move > 0 and price_scale > 0:
            point = min_move / price_scale
            if point > 0:
                return point
        return 0.01

    def _timeframe_params(self, timeframe: str) -> tuple[int, str]:
        tf = str(timeframe or "H1").upper()
        mapping = {
            "M1": (1, "Minute"),
            "M5": (5, "Minute"),
            "M15": (15, "Minute"),
            "M30": (30, "Minute"),
            "H1": (60, "Minute"),
            "H4": (240, "Minute"),
            "D1": (1, "Daily"),
        }
        return mapping.get(tf, (60, "Minute"))

    def _to_float(self, value: object) -> float:
        try:
            out = float(value or 0.0)
        except Exception:
            return 0.0
        if not math.isfinite(out):
            return 0.0
        return out
