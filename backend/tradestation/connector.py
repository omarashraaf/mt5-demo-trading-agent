from __future__ import annotations

import logging
import math
from typing import Any, Optional, Literal

import requests
from pydantic import BaseModel

from mt5.connector import AccountInfo, TerminalInfo


logger = logging.getLogger(__name__)


class TradeStationConnectionParams(BaseModel):
    environment: Literal["sim", "live"] = "sim"
    access_token: str
    refresh_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    account_id: Optional[str] = None


class TradeStationConnector:
    AUTH_BASE_URL = "https://signin.tradestation.com"
    LIVE_BASE_URL = "https://api.tradestation.com"
    SIM_BASE_URL = "https://sim-api.tradestation.com"

    def __init__(self, *, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._connected = False
        self._last_error: Optional[str] = None
        self._account_info: Optional[AccountInfo] = None
        self._terminal_info: Optional[TerminalInfo] = None
        self._environment: Literal["sim", "live"] = "sim"
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._client_id: str = ""
        self._client_secret: str = ""
        self._active_account_id: str = ""
        self._last_quotes: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def account_info(self) -> Optional[AccountInfo]:
        return self._account_info

    @property
    def active_account_id(self) -> str:
        return self._active_account_id

    def connect(self, params: TradeStationConnectionParams) -> bool:
        try:
            self._environment = params.environment
            self._access_token = str(params.access_token or "").strip()
            self._refresh_token = str(params.refresh_token or "").strip()
            self._client_id = str(params.client_id or "").strip()
            self._client_secret = str(params.client_secret or "").strip()
            self._active_account_id = str(params.account_id or "").strip()
            if not self._access_token:
                raise RuntimeError("TradeStation access token is required.")

            refreshed = self.refresh_account()
            if refreshed is None:
                raise RuntimeError(self._last_error or "TradeStation account lookup failed.")

            self._connected = True
            self._last_error = None
            self._terminal_info = self._build_terminal_info()
            logger.info(
                "Connected to TradeStation %s account %s",
                self._environment,
                self._active_account_id or "auto",
            )
            return True
        except Exception as exc:
            self._connected = False
            self._account_info = None
            self._terminal_info = None
            self._last_error = f"TradeStation connect failed: {exc}"
            logger.exception(self._last_error)
            return False

    def disconnect(self) -> bool:
        self._connected = False
        self._account_info = None
        self._terminal_info = None
        self._last_error = None
        self._active_account_id = ""
        self._access_token = ""
        self._refresh_token = ""
        self._client_id = ""
        self._client_secret = ""
        return True

    def refresh_account(self) -> Optional[AccountInfo]:
        if not self._access_token:
            self._last_error = "TradeStation access token is missing."
            return None
        try:
            accounts_payload = self._request("GET", "/v3/brokerage/accounts")
            account = self._select_account(accounts_payload, preferred_account_id=self._active_account_id)
            if not account:
                raise RuntimeError("No TradeStation brokerage account was returned.")

            account_id = str(
                account.get("AccountID")
                or account.get("accountID")
                or account.get("AccountId")
                or account.get("accountId")
                or self._active_account_id
            ).strip()
            if not account_id:
                raise RuntimeError("TradeStation account response did not include an account identifier.")
            self._active_account_id = account_id

            balances_payload = self._request("GET", f"/v3/brokerage/accounts/{account_id}/balances")
            self._account_info = self._map_account_info(account, balances_payload)
            self._terminal_info = self._build_terminal_info()
            self._connected = True
            self._last_error = None
            return self._account_info
        except Exception as exc:
            self._connected = False
            self._last_error = f"TradeStation account refresh failed: {exc}"
            logger.warning(self._last_error)
            return None

    def get_terminal_info(self) -> Optional[TerminalInfo]:
        if not self._connected:
            return None
        if self._terminal_info is None:
            self._terminal_info = self._build_terminal_info()
        return self._terminal_info

    def is_demo(self) -> bool:
        return self._environment == "sim"

    def _request(self, method: str, path: str, *, allow_refresh: bool = True) -> Any:
        response = self._session.request(
            method,
            f"{self._base_url()}{path}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
            timeout=(3.05, 12.0),
        )
        if response.status_code == 401 and allow_refresh and self._refresh_token and self._client_id:
            self._refresh_access_token()
            return self._request(method, path, allow_refresh=False)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        if not response.content:
            return {}
        return response.json()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> Any:
        response = self._session.request(
            method,
            f"{self._base_url()}{path}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params=params,
            json=json_body,
            timeout=(3.05, 12.0),
        )
        if response.status_code == 401 and allow_refresh and self._refresh_token and self._client_id:
            self._refresh_access_token()
            return self.request_json(
                method,
                path,
                params=params,
                json_body=json_body,
                allow_refresh=False,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        if not response.content:
            return {}
        return response.json()

    def get_quote_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        requested = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
        if not requested:
            return {}
        for path in (
            f"/v3/marketdata/quotes/{','.join(requested)}",
            f"/v3/marketdata/quotes/{','.join(requested)}?",
        ):
            try:
                payload = self.request_json("GET", path)
                quotes = self._extract_quote_items(payload)
                if quotes:
                    self._last_quotes.update(quotes)
                    return quotes
            except Exception:
                continue
        out: dict[str, dict[str, Any]] = {}
        for symbol in requested:
            cached = self._last_quotes.get(symbol)
            if cached:
                out[symbol] = dict(cached)
                continue
            try:
                payload = self.request_json("GET", f"/v3/marketdata/quotes/{symbol}")
                quotes = self._extract_quote_items(payload)
                if quotes.get(symbol):
                    out[symbol] = dict(quotes[symbol])
                    self._last_quotes[symbol] = dict(quotes[symbol])
            except Exception:
                continue
        return out

    def get_quote_snapshot(self, symbol: str) -> dict[str, Any] | None:
        quotes = self.get_quote_snapshots([symbol])
        return quotes.get(str(symbol or "").strip().upper()) or self._last_quotes.get(str(symbol or "").strip().upper())

    def get_bars(self, symbol: str, *, interval: int, unit: str, barsback: int) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            f"/v3/marketdata/barcharts/{symbol}",
            params={
                "interval": int(interval),
                "unit": str(unit),
                "barsback": int(max(1, barsback)),
            },
        )
        if isinstance(payload, dict):
            items = payload.get("Bars") or payload.get("bars") or []
            return [dict(item) for item in items if isinstance(item, dict)]
        return []

    def get_positions(self) -> list[dict[str, Any]]:
        account_id = str(self._active_account_id or "").strip()
        if not account_id:
            raise RuntimeError("TradeStation account is not selected.")
        payload = self.request_json("GET", f"/v3/brokerage/accounts/{account_id}/positions")
        return self._extract_dict_rows(
            payload,
            list_keys=("Positions", "positions", "Items", "items"),
            singleton_keys=("Symbol", "symbol", "AveragePrice", "averagePrice", "Quantity", "quantity"),
        )

    def get_orders(self, *, limit: int = 200) -> list[dict[str, Any]]:
        account_id = str(self._active_account_id or "").strip()
        if not account_id:
            raise RuntimeError("TradeStation account is not selected.")
        params = {
            "pageSize": int(max(1, min(limit, 500))),
        }
        last_error: Exception | None = None
        candidates = (
            ("/v3/brokerage/accounts/{account_id}/orders", params),
            ("/v3/brokerage/accounts/{account_id}/historicalorders", params),
            ("/v2/accounts/{account_id}/orders", params),
            ("/v2/accounts/{account_id}/historicalorders", params),
        )
        for template, query in candidates:
            try:
                payload = self.request_json("GET", template.format(account_id=account_id), params=query)
                rows = self._extract_dict_rows(
                    payload,
                    list_keys=("Orders", "orders", "Items", "items"),
                    singleton_keys=("OrderID", "orderID", "OrderType", "orderType", "Status", "status"),
                )
                if rows:
                    return rows
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return []

    def cancel_order(self, order_id: str | int) -> dict[str, Any]:
        payload = self.request_json("DELETE", f"/v2/orders/{order_id}")
        return dict(payload) if isinstance(payload, dict) else {"raw": payload}

    def update_order(self, order_id: str | int, body: dict[str, Any]) -> dict[str, Any]:
        payload = self.request_json("PUT", f"/v2/orders/{order_id}", json_body=body)
        return dict(payload) if isinstance(payload, dict) else {"raw": payload}

    def submit_group_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self.request_json("POST", "/v2/orders/groups", json_body=body)
        if isinstance(payload, list):
            return {
                "success": True,
                "orders": [dict(item) for item in payload if isinstance(item, dict)],
            }
        if isinstance(payload, dict):
            return {
                "success": str(payload.get("OrderStatus") or payload.get("status") or "Ok").lower() != "failed",
                "orders": [dict(payload)],
            }
        return {"success": True, "orders": []}

    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for path in ("/v3/orderexecution/orders", "/v2/orderexecution/orders"):
            try:
                payload = self.request_json("POST", path, json_body=body)
                ticket = self._extract_order_ticket(payload)
                return {
                    "success": True,
                    "retcode": 0,
                    "retcode_desc": "Submitted",
                    "ticket": ticket,
                    "price": None,
                    "raw": payload,
                }
            except Exception as exc:
                last_error = exc
                continue
        return {
            "success": False,
            "retcode": -1,
            "retcode_desc": str(last_error or "TradeStation order submission failed"),
            "ticket": None,
            "price": None,
        }

    def _refresh_access_token(self) -> None:
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        response = self._session.post(
            f"{self.AUTH_BASE_URL}/oauth/token",
            data=payload,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=(3.05, 12.0),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"refresh failed with HTTP {response.status_code}: {response.text[:200]}")
        token_payload = response.json()
        next_access = str(token_payload.get("access_token") or "").strip()
        if not next_access:
            raise RuntimeError("refresh response did not include an access_token")
        self._access_token = next_access
        next_refresh = str(token_payload.get("refresh_token") or "").strip()
        if next_refresh:
            self._refresh_token = next_refresh

    def _base_url(self) -> str:
        return self.SIM_BASE_URL if self._environment == "sim" else self.LIVE_BASE_URL

    def _select_account(self, payload: Any, *, preferred_account_id: str) -> dict[str, Any] | None:
        items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            raw_items = payload.get("Accounts") or payload.get("accounts") or payload.get("Items") or payload.get("items") or []
            if isinstance(raw_items, list):
                items = [dict(item) for item in raw_items if isinstance(item, dict)]
        elif isinstance(payload, list):
            items = [dict(item) for item in payload if isinstance(item, dict)]
        if not items:
            return None
        if preferred_account_id:
            for item in items:
                current = str(
                    item.get("AccountID")
                    or item.get("accountID")
                    or item.get("AccountId")
                    or item.get("accountId")
                    or ""
                ).strip()
                if current == preferred_account_id:
                    return item
        return items[0]

    def _map_account_info(self, account_payload: dict[str, Any], balances_payload: Any) -> AccountInfo:
        balances = self._extract_balances(balances_payload)
        currency = str(
            balances.get("Currency")
            or balances.get("currency")
            or account_payload.get("Currency")
            or account_payload.get("currency")
            or "USD"
        ).strip() or "USD"
        account_id = self._active_account_id
        display_name = str(
            account_payload.get("AccountName")
            or account_payload.get("accountName")
            or account_payload.get("Alias")
            or account_payload.get("alias")
            or f"TradeStation {account_id}"
        ).strip()
        balance = self._first_number(
            balances,
            "CashBalance",
            "cashBalance",
            "BuyingPower",
            "buyingPower",
            "NetLiquidationValue",
            "netLiquidationValue",
        )
        equity = self._first_number(
            balances,
            "NetLiquidationValue",
            "netLiquidationValue",
            "Equity",
            "equity",
            "AccountValue",
            "accountValue",
        )
        margin = self._first_number(
            balances,
            "InitialMargin",
            "initialMargin",
            "MarginRequirement",
            "marginRequirement",
            default=0.0,
        )
        free_margin = self._first_number(
            balances,
            "BuyingPower",
            "buyingPower",
            "AvailableFunds",
            "availableFunds",
            default=max(0.0, equity - margin),
        )
        trade_mode = 0 if self.is_demo() else 2
        login_digits = "".join(ch for ch in account_id if ch.isdigit())
        return AccountInfo(
            login=int(login_digits or "0"),
            name=display_name,
            server=f"TradeStation {self._environment}",
            balance=balance,
            equity=equity,
            margin=margin,
            free_margin=free_margin,
            leverage=1,
            currency=currency,
            trade_mode=trade_mode,
        )

    def _extract_balances(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            for key in ("Balances", "balances", "Balance", "balance"):
                value = payload.get(key)
                if isinstance(value, list) and value:
                    first = value[0]
                    if isinstance(first, dict):
                        return dict(first)
                if isinstance(value, dict):
                    return dict(value)
            return dict(payload)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return dict(payload[0])
        return {}

    def _first_number(self, payload: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            if key not in payload:
                continue
            value = self._to_float(payload.get(key), default=None)
            if value is not None:
                return value
        return float(default)

    def _to_float(self, value: Any, default: float | None = 0.0) -> float | None:
        try:
            num = float(value)
        except Exception:
            return default
        if not math.isfinite(num):
            return default
        return num

    def _build_terminal_info(self) -> TerminalInfo:
        return TerminalInfo(
            connected=True,
            path=f"TradeStation Web API ({self._environment})",
            data_path="",
            community_account=False,
            build=3,
            name="TradeStation",
        )

    def _extract_quote_items(self, payload: Any) -> dict[str, dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in ("Quotes", "quotes", "Items", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    items = [dict(item) for item in value if isinstance(item, dict)]
                    break
            if not items:
                symbol = str(
                    payload.get("Symbol")
                    or payload.get("symbol")
                    or payload.get("Key")
                    or payload.get("key")
                    or ""
                ).strip().upper()
                if symbol:
                    items = [dict(payload)]
        elif isinstance(payload, list):
            items = [dict(item) for item in payload if isinstance(item, dict)]
        return {
            str(item.get("Symbol") or item.get("symbol") or item.get("Key") or item.get("key") or "").strip().upper(): item
            for item in items
            if str(item.get("Symbol") or item.get("symbol") or item.get("Key") or item.get("key") or "").strip()
        }

    def _extract_order_ticket(self, payload: Any) -> int | None:
        if isinstance(payload, dict):
            for key in ("Orders", "orders", "Items", "items"):
                value = payload.get(key)
                if isinstance(value, list) and value:
                    return self._extract_order_ticket(value[0])
            for key in ("OrderID", "orderID", "OrderId", "orderId", "ID", "id"):
                try:
                    value = int(payload.get(key) or 0)
                except Exception:
                    value = 0
                if value > 0:
                    return value
        if isinstance(payload, list) and payload:
            return self._extract_order_ticket(payload[0])
        return None

    def _extract_dict_rows(
        self,
        payload: Any,
        *,
        list_keys: tuple[str, ...],
        singleton_keys: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            for key in list_keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [dict(item) for item in value if isinstance(item, dict)]
            if any(key in payload for key in singleton_keys):
                return [dict(payload)]
            return []
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        return []
