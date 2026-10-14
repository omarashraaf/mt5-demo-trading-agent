import logging
import math
import time
import threading
from typing import Optional
from pydantic import BaseModel
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception:  # pragma: no cover - handled for non-Windows/cloud runtimes
    class _MT5Stub:
        def __getattr__(self, name):
            if name.isupper():
                return 0
            return lambda *args, **kwargs: None

    mt5 = _MT5Stub()
    MT5_AVAILABLE = False

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    "M1": getattr(mt5, "TIMEFRAME_M1", 1),
    "M5": getattr(mt5, "TIMEFRAME_M5", 5),
    "M15": getattr(mt5, "TIMEFRAME_M15", 15),
    "M30": getattr(mt5, "TIMEFRAME_M30", 30),
    "H1": getattr(mt5, "TIMEFRAME_H1", 16385),
    "H4": getattr(mt5, "TIMEFRAME_H4", 16388),
    "D1": getattr(mt5, "TIMEFRAME_D1", 16408),
}


class TickData(BaseModel):
    symbol: str
    bid: float
    ask: float
    spread: float
    time: int
    time_msc: int | None = None


class BarData(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int


class MarketDataService:
    def __init__(self):
        self._point_cache: dict[str, float] = {}
        self._enabled_symbols: set[str] = set()
        self._symbols_cache: dict[str, tuple[float, list[dict]]] = {}
        self._symbol_name_cache: dict[str, str] = {}
        self._tick_failure_until: dict[str, float] = {}
        self._last_session_recover_at = 0.0
        self._mt5_lock = threading.RLock()

    def _is_ipc_session_error(self, error: object) -> bool:
        text = str(error or "").lower()
        if not text:
            return False
        return any(
            token in text
            for token in (
                "ipc send failed",
                "no ipc connection",
                "terminal is not connected",
                "not initialized",
            )
        )

    def _recover_mt5_session_if_needed(self, error: object) -> bool:
        if not MT5_AVAILABLE or not self._is_ipc_session_error(error):
            return False
        now = time.time()
        # Prevent reconnect storms during bulk scans.
        if (now - float(self._last_session_recover_at or 0.0)) < 2.0:
            return False
        self._last_session_recover_at = now
        try:
            mt5.shutdown()
        except Exception:
            pass
        try:
            ok = bool(mt5.initialize(timeout=7000))
            if ok:
                logger.warning("Recovered MT5 IPC session after error: %s", error)
            else:
                logger.warning("MT5 IPC recovery attempt failed: %s", mt5.last_error())
            return ok
        except Exception:
            logger.debug("MT5 IPC recovery raised exception", exc_info=True)
            return False

    def resolve_symbol_name(self, symbol: str) -> str:
        """Return the broker's exact symbol name, preserving case-sensitive suffixes."""
        raw = str(symbol or "").strip()
        if not raw or not MT5_AVAILABLE:
            return raw
        cache_key = raw.upper()
        cached = self._symbol_name_cache.get(cache_key)
        if cached:
            return cached

        with self._mt5_lock:
            info = mt5.symbol_info(raw)
            if info is not None:
                actual = str(getattr(info, "name", raw) or raw).strip() or raw
                self._symbol_name_cache[cache_key] = actual
                return actual

            symbols = mt5.symbols_get()
        if symbols is None:
            err = mt5.last_error()
            if self._recover_mt5_session_if_needed(err):
                with self._mt5_lock:
                    symbols = mt5.symbols_get()
        if symbols is not None:
            for item in symbols:
                name = str(getattr(item, "name", "") or "").strip()
                if name and name.upper() == cache_key:
                    self._symbol_name_cache[cache_key] = name
                    return name
        return raw

    def _point_for_symbol(self, symbol: str) -> float:
        if not MT5_AVAILABLE:
            return 0.00001
        symbol = self.resolve_symbol_name(symbol)
        cached = self._point_cache.get(symbol)
        if cached is not None and cached > 0:
            return cached
        with self._mt5_lock:
            info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.00001) or 0.00001) if info else 0.00001
        if point > 0:
            self._point_cache[symbol] = point
        return point

    def _extract_commission_fields(self, symbol_info) -> dict:
        """
        Build a normalized commission snapshot for a symbol.
        Values are best-effort because broker feeds vary by server.
        """
        payload = symbol_info._asdict() if hasattr(symbol_info, "_asdict") else {}

        def _num(value) -> Optional[float]:
            if value is None:
                return None
            try:
                out = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(out):
                return None
            return out

        def _pick(keys: list[str]) -> Optional[float]:
            for key in keys:
                raw = payload.get(key, getattr(symbol_info, key, None))
                val = _num(raw)
                if val is not None and abs(val) > 0:
                    return abs(val)
            return None

        fixed_per_lot_side = _pick([
            "trade_commission",
            "trade_commission_per_lot",
            "commission_per_lot",
            "commission_lots",
            "commission",
        ])
        percent_rate = _pick([
            "trade_commission_percent",
            "commission_percent",
            "commission_rate",
        ])

        contract_size = _num(payload.get("trade_contract_size", getattr(symbol_info, "trade_contract_size", 0.0))) or 0.0
        bid = _num(payload.get("bid", getattr(symbol_info, "bid", 0.0))) or 0.0
        ask = _num(payload.get("ask", getattr(symbol_info, "ask", 0.0))) or 0.0
        mid = ((bid + ask) / 2.0) if (bid > 0 and ask > 0) else (bid or ask or 0.0)
        notional_one_lot = (contract_size * mid) if (contract_size > 0 and mid > 0) else None

        if fixed_per_lot_side is not None:
            per_lot_side = fixed_per_lot_side
            model = "fixed_per_lot"
        elif percent_rate is not None and notional_one_lot:
            per_lot_side = (notional_one_lot * percent_rate) / 100.0
            model = "percent_notional"
        else:
            per_lot_side = 0.0
            model = "unknown_or_zero"

        round_turn = per_lot_side * 2.0
        return {
            "commission_per_lot_side": round(float(per_lot_side), 6),
            "commission_round_turn_per_lot": round(float(round_turn), 6),
            "commission_model": model,
            "commission_percent_rate": round(float(percent_rate), 6) if percent_rate is not None else None,
            "commission_notional_1lot": round(float(notional_one_lot), 6) if notional_one_lot else None,
        }

    def enable_symbol(self, symbol: str) -> bool:
        if not MT5_AVAILABLE:
            return False
        symbol = self.resolve_symbol_name(symbol)
        if symbol in self._enabled_symbols:
            return True
        with self._mt5_lock:
            info = mt5.symbol_info(symbol)
        if info is None:
            logger.debug("Symbol %s not found", symbol)
            return False
        if not info.visible:
            with self._mt5_lock:
                selected = mt5.symbol_select(symbol, True)
            if not selected:
                logger.error(f"Failed to enable symbol {symbol} in MarketWatch")
                return False
            logger.info(f"Enabled {symbol} in MarketWatch")
        self._enabled_symbols.add(symbol)
        return True

    def get_tick(self, symbol: str) -> Optional[TickData]:
        if not MT5_AVAILABLE:
            return None
        symbol = self.resolve_symbol_name(symbol)
        self.enable_symbol(symbol)
        with self._mt5_lock:
            tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            with self._mt5_lock:
                mt5.symbol_select(symbol, True)
            time.sleep(0.05)
            with self._mt5_lock:
                tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            err = mt5.last_error()
            if self._recover_mt5_session_if_needed(err):
                self.enable_symbol(symbol)
                tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error(f"Failed to get tick for {symbol}: {err}")
                return None
        point = self._point_for_symbol(symbol)
        spread = round((tick.ask - tick.bid) / point) if point > 0 else 0
        return TickData(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            spread=spread,
            time=tick.time,
            time_msc=int(getattr(tick, "time_msc", 0) or (tick.time * 1000)),
        )

    def _tick_for_symbol(self, symbol: str) -> Optional[TickData]:
        if not MT5_AVAILABLE:
            return None
        symbol = self.resolve_symbol_name(symbol)
        self.enable_symbol(symbol)
        with self._mt5_lock:
            tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            with self._mt5_lock:
                mt5.symbol_select(symbol, True)
            time.sleep(0.05)
            with self._mt5_lock:
                tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            err = mt5.last_error()
            if self._recover_mt5_session_if_needed(err):
                self.enable_symbol(symbol)
                tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return None
        try:
            bid = float(tick.bid)
            ask = float(tick.ask)
        except Exception:
            return None
        point = self._point_for_symbol(symbol)
        spread = round((ask - bid) / point) if point > 0 else 0
        return TickData(
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread=spread,
            time=tick.time,
            time_msc=int(getattr(tick, "time_msc", 0) or (tick.time * 1000)),
        )

    def get_ticks(self, symbols: list[str]) -> dict[str, TickData]:
        """Bulk tick retrieval optimized for fast dashboard refresh loops."""
        if not MT5_AVAILABLE:
            return {}
        requested = [str(s or "").strip() for s in symbols if str(s or "").strip()]
        if not requested:
            return {}

        out: dict[str, TickData] = {}
        now = time.time()
        failure_cooldown_seconds = 300.0
        # MetaTrader5's Python bridge is process-global and can stall when many
        # threads hit the terminal IPC at once. Keep bulk reads serialized so
        # dashboard polling does not starve status, history, and scan requests.
        for symbol in requested:
            resolved = self.resolve_symbol_name(symbol)
            blocked_until = float(self._tick_failure_until.get(resolved, 0.0) or 0.0)
            if blocked_until > now:
                continue
            try:
                tick = self._tick_for_symbol(resolved)
            except Exception:
                tick = None
            if tick is not None:
                out[resolved] = tick
                self._tick_failure_until.pop(resolved, None)
            else:
                self._tick_failure_until[resolved] = now + failure_cooldown_seconds
        return out

    def get_bars(
        self, symbol: str, timeframe: str, count: int = 100
    ) -> list[BarData]:
        if not MT5_AVAILABLE:
            return []
        symbol = self.resolve_symbol_name(symbol)
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
        if not MT5_AVAILABLE:
            return None
        symbol = self.resolve_symbol_name(symbol)
        info = mt5.symbol_info(symbol)
        if info is None:
            err = mt5.last_error()
            if self._recover_mt5_session_if_needed(err):
                info = mt5.symbol_info(symbol)
            if info is None:
                return None
        base = {
            "name": info.name,
            "description": info.description,
            "path": info.path,
            "point": info.point,
            "digits": info.digits,
            "trade_contract_size": info.trade_contract_size,
            "trade_tick_value": getattr(info, "trade_tick_value", 0.0),
            "trade_tick_size": getattr(info, "trade_tick_size", 0.0),
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_mode": info.trade_mode,
            "visible": info.visible,
            "trade_stops_level": getattr(info, "trade_stops_level", 0),
        }
        base.update(self._extract_commission_fields(info))
        return base

    def _cached_symbols(self, cache_key: str, ttl_seconds: float) -> Optional[list[dict]]:
        row = self._symbols_cache.get(cache_key)
        if not row:
            return None
        ts, payload = row
        if (time.time() - float(ts or 0.0)) > float(max(ttl_seconds, 0.1)):
            self._symbols_cache.pop(cache_key, None)
            return None
        return payload

    def _store_symbols_cache(self, cache_key: str, payload: list[dict]) -> list[dict]:
        self._symbols_cache[cache_key] = (time.time(), payload)
        return payload

    def get_tradeable_symbols_cached(self, ttl_seconds: float = 2.0) -> list[dict]:
        cached = self._cached_symbols("tradeable", ttl_seconds)
        if cached is not None:
            return cached
        return self._store_symbols_cache("tradeable", self.get_tradeable_symbols())

    def get_visible_symbols_cached(self, ttl_seconds: float = 2.0) -> list[dict]:
        cached = self._cached_symbols("visible", ttl_seconds)
        if cached is not None:
            return cached
        return self._store_symbols_cache("visible", self.get_visible_symbols())

    def get_all_symbols(self) -> list[dict]:
        """Get ALL symbols available on the broker's MT5 terminal."""
        if not MT5_AVAILABLE:
            return []
        symbols = mt5.symbols_get()
        if symbols is None:
            err = mt5.last_error()
            if self._recover_mt5_session_if_needed(err):
                symbols = mt5.symbols_get()
            if symbols is None:
                logger.error(f"Failed to get symbols: {err}")
                return []

        result = []
        for s in symbols:
            # trade_mode: 0=disabled, 4=full access
            category = self._categorize_symbol(s)
            row = {
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
            }
            row.update(self._extract_commission_fields(s))
            result.append(row)
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
        candidates = [
            dict(s)
            for s in all_syms
            if s.get("trade_enabled") and (float(s.get("bid") or 0.0) > 0.0 or bool(s.get("visible")))
        ]
        names = [str(s.get("name") or "").strip() for s in candidates if str(s.get("name") or "").strip()]
        live_ticks = self.get_ticks(names)
        refreshed: list[dict] = []
        for row in candidates:
            name = str(row.get("name") or "").strip()
            tick = live_ticks.get(name)
            if tick is not None:
                row["bid"] = float(tick.bid)
                row["ask"] = float(tick.ask)
                row["spread"] = float(tick.spread)
                row["time"] = int(tick.time)
                row["time_msc"] = int(tick.time_msc or 0)
            if float(row.get("bid") or 0.0) > 0.0 and float(row.get("ask") or 0.0) > 0.0:
                refreshed.append(row)
        return refreshed

    def _categorize_symbol(self, symbol_info) -> str:
        """Categorize a symbol based on its path and name."""
        path = (symbol_info.path or "").lower()
        name = (symbol_info.name or "").upper()
        desc = (symbol_info.description or "").lower()
        compact_name = "".join(ch for ch in name if ch.isalnum())

        def _starts_with_known(token: str) -> bool:
            return bool(token and (compact_name == token or compact_name.startswith(token)))

        # Crypto detection
        crypto_keywords = ["crypto", "bitcoin", "btc", "eth", "ltc", "xrp", "doge", "sol", "ada", "bnb", "coin"]
        if any(k in path for k in crypto_keywords) or any(k in desc for k in crypto_keywords):
            return "Crypto"
        if (compact_name.endswith("USD") or len(compact_name) > 6) and compact_name[:3] in ["BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "BNB", "DOG", "AVA", "DOT", "LIN", "UNI", "MAT"]:
            return "Crypto"

        # Stocks detection (must run before index detection so Nasdaq stocks are not mis-labeled)
        stock_keywords = ["stock", "share", "equity", "equities", "cfd"]
        if any(k in path for k in stock_keywords):
            return "Stocks"
        # Common stock symbols (US tech stocks, etc.)
        stock_names = ["AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "TSLA", "META", "NVDA", "NFLX", "AMD", "INTC", "DIS", "BA", "V", "JPM", "WMT", "PFE", "KO", "NKE"]
        stock_root = name.replace(".US", "").replace("_US", "").replace("-US", "")
        if name in stock_names or stock_root in stock_names or any(_starts_with_known(stock) for stock in stock_names):
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
            or any(_starts_with_known(token) for token in ("GOLD", "WTI", "BRENT", "NATGAS", "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"))
            or name in {"GOLD", "WTI", "BRENT", "NATGAS"}
            or "OIL" in name
            or "BRENT" in name
            or "WTI" in name
            or "GAS" in name
        ):
            return "Commodities"
        # Commodity ETFs
        if any(_starts_with_known(token) for token in ("GLD", "SLV", "USO", "DBC", "GLDM", "IAU", "PPLT", "PALL", "PDBC", "GSG")):
            return "Commodities"

        # Indices detection
        index_keywords = ["index", "indices", "us30", "us500", "nas100", "dax", "ftse", "nikkei", "sp500", "dowjones", "dow"]
        if any(k in path for k in index_keywords) or any(k in desc for k in index_keywords):
            return "Indices"
        if any(_starts_with_known(token) for token in ["US30", "US500", "NAS100", "US100", "SPX500", "GER40", "UK100", "JPN225", "AUS200", "DE40", "DAX40"]):
            return "Indices"

        # Forex (default for currency pairs)
        forex_keywords = ["forex", "fx", "currency", "major", "minor", "exotic"]
        if any(k in path for k in forex_keywords):
            return "Forex"
        # Standard forex pair pattern: 6 chars, all letters
        major_currency_codes = {
            "AUD", "CAD", "CHF", "CNH", "EUR", "GBP", "HKD", "JPY", "MXN", "NOK",
            "NZD", "SEK", "SGD", "TRY", "USD", "ZAR",
        }
        if (
            len(compact_name) >= 6
            and compact_name[:6].isalpha()
            and compact_name[:3] in major_currency_codes
            and compact_name[3:6] in major_currency_codes
            and compact_name[:3] != compact_name[3:6]
        ):
            return "Forex"

        return "Other"
