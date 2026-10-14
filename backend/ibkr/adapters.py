from __future__ import annotations

import time
from typing import Callable, Optional

from mt5.execution import OrderRequest, OrderResult, PositionInfo
from mt5.market_data import BarData, TickData

from .connector import IBKRConnector


class IBKRConnectionAdapter:
    """Thread-safe runtime view of the raw ib_insync connector."""

    def __init__(self, connector: IBKRConnector, runner: Callable):
        self.connector = connector
        self._runner = runner

    @property
    def connected(self) -> bool:
        return bool(self.connector.connected)

    @property
    def account_info(self):
        return self.connector.account_info

    @property
    def last_error(self):
        return self.connector.last_error

    def trade_server_connected(self) -> bool:
        return bool(self._runner(self.connector.trade_server_connected))

    def refresh_account(self):
        return self._runner(self.connector.refresh_account)

    def is_demo(self) -> bool:
        return bool(self._runner(self.connector.is_demo))

    def get_terminal_info(self):
        return self._runner(self.connector.get_terminal_info)


class IBKRMarketDataAdapter:
    """Expose IBKR data through the shared MT5-shaped market-data contract."""

    DEFAULT_SYMBOLS = (
        "SPY", "QQQ", "DIA", "IWM", "AAPL", "MSFT", "NVDA", "AMD",
        "AMZN", "TSLA", "GLD", "SLV", "USO", "UNG", "TLT", "BTC",
    )

    def __init__(self, connector: IBKRConnector, runner: Optional[Callable] = None):
        self.connector = connector
        self._runner = runner

    def _call(self, func, *args, **kwargs):
        return self._runner(func, *args, **kwargs) if self._runner else func(*args, **kwargs)

    def resolve_symbol_name(self, symbol: str) -> str:
        return str(symbol or "").strip().upper()

    def enable_symbol(self, symbol: str) -> bool:
        return bool(self.resolve_symbol_name(symbol))

    def get_tick(self, symbol: str) -> Optional[TickData]:
        snap = self._call(self.connector.get_snapshot, symbol)
        if not snap:
            return None
        bid = float(snap.get("bid") or 0.0)
        ask = float(snap.get("ask") or 0.0)
        now = int(time.time())
        return TickData(
            symbol=str(snap.get("symbol") or self.resolve_symbol_name(symbol)),
            bid=bid,
            ask=ask,
            spread=max(0.0, ask - bid),
            time=int(snap.get("time") or now),
            time_msc=int(snap.get("time_msc") or now * 1000),
        )

    def get_ticks(self, symbols: list[str]) -> dict[str, TickData]:
        snapshots = self._call(self.connector.get_snapshots, symbols)
        result: dict[str, TickData] = {}
        for symbol in symbols:
            key = self.resolve_symbol_name(symbol)
            snap = snapshots.get(key)
            if not snap:
                continue
            bid = float(snap.get("bid") or 0.0)
            ask = float(snap.get("ask") or 0.0)
            now = int(time.time())
            result[key] = TickData(
                symbol=str(snap.get("symbol") or key),
                bid=bid,
                ask=ask,
                spread=max(0.0, ask - bid),
                time=int(snap.get("time") or now),
                time_msc=int(snap.get("time_msc") or now * 1000),
            )
        return result

    def get_bars(self, symbol: str, timeframe: str, count: int = 120) -> list[BarData]:
        return [BarData(**row) for row in self._call(self.connector.get_bars, symbol, timeframe, count)]

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        key = self.resolve_symbol_name(symbol)
        if not key:
            return None
        tick = self.get_tick(key)
        forex_currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}
        if len(key) == 6 and key[:3] in forex_currencies and key[3:] in forex_currencies:
            category = "Forex"
        elif key in {"BTC", "ETH", "BTCUSD", "ETHUSD"}:
            category = "Crypto"
        else:
            category = "Stocks"
        return {
            "name": key,
            "description": f"{key} via Interactive Brokers",
            # The signal pipeline uses both category and path to distinguish
            # equities from generic/unknown broker symbols.
            "path": f"IBKR/{category}",
            "category": category,
            "visible": True,
            "trade_enabled": True,
            "trade_mode": 4,
            "point": 0.01,
            "digits": 2,
            "trade_contract_size": 1.0,
            "volume_min": 1.0,
            "volume_max": 1_000_000.0,
            "volume_step": 1.0,
            "bid": float(tick.bid) if tick else 0.0,
            "ask": float(tick.ask) if tick else 0.0,
            "spread": float(tick.spread) if tick else 0.0,
        }

    def _catalog(self) -> list[dict]:
        return [self.get_symbol_info(symbol) for symbol in self.DEFAULT_SYMBOLS]

    def get_all_symbols(self) -> list[dict]:
        return [row for row in self._catalog() if row]

    def get_tradeable_symbols(self, *args, **kwargs) -> list[dict]:
        return self.get_all_symbols()

    def get_visible_symbols(self, *args, **kwargs) -> list[dict]:
        return self.get_all_symbols()


class IBKRExecutionAdapter:
    """Expose IBKR execution through the shared execution-engine contract."""

    def __init__(self, connector: IBKRConnector, runner: Optional[Callable] = None):
        self.connector = connector
        self._runner = runner

    def _call(self, func, *args, **kwargs):
        return self._runner(func, *args, **kwargs) if self._runner else func(*args, **kwargs)

    def get_positions(self, symbol: Optional[str] = None) -> list[PositionInfo]:
        return [PositionInfo(**row) for row in self._call(self.connector.get_positions, symbol)]

    def place_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(**self._call(self.connector.place_order,
            request.symbol,
            request.action,
            request.volume,
            request.stop_loss,
            request.take_profit,
            request.comment,
        ))

    def close_position(self, ticket: int, *args, **kwargs) -> OrderResult:
        return OrderResult(**self._call(self.connector.close_position, ticket))

    def modify_position(self, ticket: int, stop_loss: float, take_profit: float) -> OrderResult:
        return OrderResult(**self._call(self.connector.modify_position, ticket, stop_loss, take_profit))

    def get_recent_commission_by_symbol(self, *args, **kwargs) -> dict:
        return {}

    def get_realized_profit_map(self, *args, **kwargs) -> dict:
        return {}

    def get_position_realized_profit(self, *args, **kwargs) -> float:
        return 0.0

    def get_recent_closed_trades(self, limit: int = 100, *args, **kwargs) -> list[dict]:
        return self._call(self.connector.get_recent_executions, limit=limit)

    def get_recent_closed_trades_fast(self, limit: int = 100, *args, **kwargs) -> list[dict]:
        return self.get_recent_closed_trades(limit=limit)

    def get_recent_closed_deal_rows(self, limit: int = 100, *args, **kwargs) -> list[dict]:
        return self.get_recent_closed_trades(limit=limit)
