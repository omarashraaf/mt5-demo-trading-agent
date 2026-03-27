import MetaTrader5 as mt5
import logging
import pandas as pd
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


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


class MarketDataService:
    def enable_symbol(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"Symbol {symbol} not found")
            return False
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.error(f"Failed to enable symbol {symbol} in MarketWatch")
                return False
            logger.info(f"Enabled {symbol} in MarketWatch")
        return True

    def get_tick(self, symbol: str) -> Optional[TickData]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Failed to get tick for {symbol}: {mt5.last_error()}")
            return None
        info = mt5.symbol_info(symbol)
        point = info.point if info else 0.00001
        spread = round((tick.ask - tick.bid) / point) if point > 0 else 0
        return TickData(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            spread=spread,
            time=tick.time,
        )

    def get_bars(
        self, symbol: str, timeframe: str, count: int = 100
    ) -> list[BarData]:
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error(f"Invalid timeframe: {timeframe}")
            return []

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to get bars for {symbol}: {mt5.last_error()}")
            return []

        bars = []
        for r in rates:
            bars.append(
                BarData(
                    time=int(r["time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(r["tick_volume"]),
                )
            )
        return bars

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return {
            "name": info.name,
            "description": info.description,
            "path": info.path,
            "point": info.point,
            "digits": info.digits,
            "trade_contract_size": info.trade_contract_size,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_mode": info.trade_mode,
            "visible": info.visible,
            "trade_stops_level": getattr(info, "trade_stops_level", 0),
        }

    def get_all_symbols(self) -> list[dict]:
        """Get ALL symbols available on the broker's MT5 terminal."""
        symbols = mt5.symbols_get()
        if symbols is None:
            logger.error(f"Failed to get symbols: {mt5.last_error()}")
            return []

        result = []
        for s in symbols:
            # trade_mode: 0=disabled, 4=full access
            category = self._categorize_symbol(s)
            result.append({
                "name": s.name,
                "description": s.description,
                "path": s.path,
                "category": category,
                "visible": s.visible,
                "trade_mode": s.trade_mode,
                "spread": s.spread,
                "digits": s.digits,
                "point": s.point,
                "volume_min": s.volume_min,
                "trade_enabled": s.trade_mode != 0,
                "bid": s.bid,
                "ask": s.ask,
            })
        return result

    def get_symbols_by_category(self, category: str) -> list[dict]:
        """Get symbols filtered by category: crypto, stocks, indices, commodities, forex."""
        all_syms = self.get_all_symbols()
        return [s for s in all_syms if s["category"].lower() == category.lower()]

    def get_visible_symbols(self) -> list[dict]:
        """Get only symbols currently visible in MarketWatch."""
        all_syms = self.get_all_symbols()
        return [s for s in all_syms if s["visible"]]

    def get_tradeable_symbols(self) -> list[dict]:
        """Get symbols that have trading enabled and a live price."""
        all_syms = self.get_all_symbols()
        return [s for s in all_syms if s["trade_enabled"] and s["bid"] > 0]

    def _categorize_symbol(self, symbol_info) -> str:
        """Categorize a symbol based on its path and name."""
        path = (symbol_info.path or "").lower()
        name = (symbol_info.name or "").upper()
        desc = (symbol_info.description or "").lower()

        # Crypto detection
        crypto_keywords = ["crypto", "bitcoin", "btc", "eth", "ltc", "xrp", "doge", "sol", "ada", "bnb", "coin"]
        if any(k in path for k in crypto_keywords) or any(k in desc for k in crypto_keywords):
            return "Crypto"
        if name.endswith("USD") and name[:3] in ["BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "BNB", "DOG", "AVA", "DOT", "LIN", "UNI", "MAT"]:
            return "Crypto"

        # Stocks detection (must run before index detection so Nasdaq stocks are not mis-labeled)
        stock_keywords = ["stock", "share", "equity", "equities", "cfd"]
        if any(k in path for k in stock_keywords):
            return "Stocks"
        # Common stock symbols (US tech stocks, etc.)
        stock_names = ["AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "TSLA", "META", "NVDA", "NFLX", "AMD", "INTC", "DIS", "BA", "V", "JPM", "WMT", "PFE", "KO", "NKE"]
        if name in stock_names or name.replace(".US", "") in stock_names:
            return "Stocks"

        # Commodities detection
        commodity_keywords = ["commodity", "gold", "silver", "oil", "natural", "copper", "platinum"]
        if any(k in path for k in commodity_keywords) or any(k in desc for k in commodity_keywords):
            return "Commodities"
        if (
            name.startswith("XAU")
            or name.startswith("XAG")
            or name.startswith("XPT")
            or name.startswith("XPD")
            or name in {"GOLD", "WTI", "BRENT", "NATGAS"}
            or "OIL" in name
            or "BRENT" in name
            or "WTI" in name
            or "GAS" in name
        ):
            return "Commodities"
        # Commodity ETFs
        if name in ("GLD", "SLV", "USO", "DBC", "GLDM", "IAU", "PPLT", "PALL", "PDBC", "GSG"):
            return "Commodities"

        # Indices detection
        index_keywords = ["index", "indices", "us30", "us500", "nas100", "dax", "ftse", "nikkei", "sp500", "dowjones", "dow"]
        if any(k in path for k in index_keywords) or any(k in desc for k in index_keywords):
            return "Indices"
        if name in ["US30", "US500", "NAS100", "US100", "SPX500", "GER40", "UK100", "JPN225", "AUS200"]:
            return "Indices"

        # Forex (default for currency pairs)
        forex_keywords = ["forex", "fx", "currency", "major", "minor", "exotic"]
        if any(k in path for k in forex_keywords):
            return "Forex"
        # Standard forex pair pattern: 6 chars, all letters
        if len(name) == 6 and name.isalpha() and name[:3] != name[3:]:
            return "Forex"

        return "Other"
