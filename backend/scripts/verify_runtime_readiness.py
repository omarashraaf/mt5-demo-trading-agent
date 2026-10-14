from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REQUIRED_PLATFORM = "mt5"
REQUIRED_PLATFORMS_SUPPORTED = ["mt5", "ibkr", "tradestation"]
REQUIRED_MARKET_CLASSES = {"Forex", "Indices", "Commodities", "Stocks", "Crypto"}
REQUIRED_API_KEYS = {"benzinga", "gemini", "supabase_anon", "supabase_service_role"}
MIN_DISCONNECTED_TOP_MARKETS = 20
LEGACY_BROKER_ROUTES = ("/ibkr/debug/account-summary",)


def _json_get(api_base: str, path: str, *, timeout: float = 20.0, params: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = _json_any_get(api_base, path, timeout=timeout, params=params)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GET {api_base.rstrip('/')}{path} returned {type(parsed).__name__}, expected object")
    return parsed


def _json_any_get(api_base: str, path: str, *, timeout: float = 20.0, params: dict[str, Any] | None = None) -> Any:
    base = api_base.rstrip("/")
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GET {url} did not return JSON") from exc
    return parsed


def _category_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts = payload.get("category_counts")
    if isinstance(counts, dict):
        return {str(k): int(v or 0) for k, v in counts.items()}
    categories = payload.get("categories")
    if isinstance(categories, dict):
        return {str(k): len(v) if isinstance(v, list) else 0 for k, v in categories.items()}
    return {}


def _is_fresh_tick_payload(value: Any, *, now: float | None = None, max_age_seconds: float = 120.0) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        bid = float(value.get("bid") or 0.0)
        ask = float(value.get("ask") or 0.0)
    except Exception:
        return False
    if bid <= 0 or ask <= 0:
        return False

    timestamp = 0.0
    for key in ("time_msc", "time", "timestamp"):
        try:
            timestamp = float(value.get(key) or 0.0)
        except Exception:
            timestamp = 0.0
        if timestamp > 0:
            break
    if timestamp <= 0:
        return False
    if timestamp > 1_000_000_000_000:
        timestamp /= 1000.0

    now_ts = float(now if now is not None else time.time())
    age = now_ts - timestamp
    return -5.0 <= age <= float(max_age_seconds)


def _sample_ticks(api_base: str, symbols: list[str]) -> dict[str, Any]:
    if not symbols:
        return {"requested": [], "returned": 0, "sample": {}}
    payload = _json_get(
        api_base,
        "/market/ticks",
        timeout=10.0,
        params={"symbols": ",".join(symbols[:12])},
    )
    ticks = payload.get("ticks") if isinstance(payload.get("ticks"), dict) else {}
    fresh_ticks = {
        key: value
        for key, value in ticks.items()
        if _is_fresh_tick_payload(value)
    }
    return {
        "requested": symbols[:12],
        "returned": len(ticks),
        "fresh_returned": len(fresh_ticks),
        "source": str(payload.get("source") or ""),
        "sample": {
            key: {
                "bid": value.get("bid"),
                "ask": value.get("ask"),
                "time": value.get("time"),
                "time_msc": value.get("time_msc"),
                "fresh": _is_fresh_tick_payload(value),
            }
            for key, value in list(ticks.items())[:5]
            if isinstance(value, dict)
        },
    }


def _refresh_api_health_for_strict_check(api_base: str, warnings: list[str]) -> dict[str, Any] | None:
    try:
        return _json_get(api_base, "/internal/api-keys-health/cron", timeout=35.0)
    except Exception as exc:
        warnings.append(f"API health force-refresh unavailable before strict check: {exc}")
        return None


def _legacy_broker_route_errors(api_base: str) -> list[str]:
    errors: list[str] = []
    for path in LEGACY_BROKER_ROUTES:
        try:
            _json_any_get(api_base, path, timeout=8.0)
        except Exception as exc:
            message = str(exc)
            if "HTTP 404" in message or "HTTP 405" in message:
                continue
            errors.append(f"Legacy broker route {path} availability could not be verified: {message}")
            continue
        errors.append(f"Legacy broker route {path} must be unavailable in MT5-only runtime.")
    return errors


def _is_legacy_broker_source(value: Any) -> bool:
    source = str(value or "").strip().lower()
    return "ibkr" in source or "tradestation" in source


def _api_key_items_by_id(api_keys_health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id") or ""): item
        for item in (api_keys_health.get("items") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _market_class_names(categories: dict[str, int]) -> set[str]:
    aliases = {
        "forex": "Forex",
        "fx": "Forex",
        "indices": "Indices",
        "index": "Indices",
        "commodities": "Commodities",
        "commodity": "Commodities",
        "stocks": "Stocks",
        "stock": "Stocks",
        "equities": "Stocks",
        "equity": "Stocks",
        "crypto": "Crypto",
        "cryptocurrency": "Crypto",
        "cryptocurrencies": "Crypto",
    }
    normalized: set[str] = set()
    for raw_name, count in categories.items():
        if int(count or 0) <= 0:
            continue
        compact = str(raw_name or "").strip().lower().replace("_", " ").replace("-", " ")
        normalized.add(aliases.get(compact, str(raw_name or "").strip()))
    return normalized


def _market_class_names_from_symbols(symbols: list[Any]) -> set[str]:
    categories: dict[str, int] = {}
    for item in symbols:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        if not category:
            continue
        categories[category] = categories.get(category, 0) + 1
    return _market_class_names(categories)


def _readiness_dict(status: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    value = status.get(key)
    if not isinstance(value, dict):
        errors.append(f"/status is missing {key}.")
        return {}
    return value


def _is_finite_number(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except Exception:
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _positive_number(value: Any) -> float | None:
    if not _is_finite_number(value):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if number > 0 else None


def _row_action(row: dict[str, Any]) -> str:
    return str(row.get("action") or row.get("type") or "").upper()


def _row_entry_price(row: dict[str, Any]) -> float | None:
    for key in ("entry_price", "entry_price_estimate", "price_open", "open_price"):
        value = _positive_number(row.get(key))
        if value is not None:
            return value
    return None


def _validate_value_row(row: dict[str, Any], source: str, index: int) -> list[str]:
    errors: list[str] = []
    numeric_fields = (
        "entry_price",
        "entry_price_estimate",
        "price_open",
        "open_price",
        "stop_loss",
        "take_profit",
        "started_with_usd",
        "entry_market_value_usd",
        "margin_used_usd",
        "estimated_margin",
        "sl_amount_usd",
        "tp_amount_usd",
        "sl_pct_of_start",
        "tp_pct_of_start",
        "profit",
        "profit_pct",
        "volume",
    )
    label = f"{source}[{index}]"
    for field in numeric_fields:
        if field in row and not _is_finite_number(row.get(field)):
            errors.append(f"{label}.{field} is not finite.")

    for field in ("started_with_usd", "entry_market_value_usd", "margin_used_usd", "estimated_margin", "sl_amount_usd", "tp_amount_usd", "volume"):
        if field in row and row.get(field) is not None:
            try:
                if float(row.get(field) or 0.0) < 0:
                    errors.append(f"{label}.{field} is negative.")
            except Exception:
                pass

    action = _row_action(row)
    entry = _row_entry_price(row)
    stop_loss = _positive_number(row.get("stop_loss"))
    take_profit = _positive_number(row.get("take_profit"))
    if action in {"BUY", "SELL"} and entry is not None and stop_loss is not None and take_profit is not None:
        if action == "BUY" and not (stop_loss < entry < take_profit):
            errors.append(f"{label} BUY targets are on the wrong side of entry.")
        if action == "SELL" and not (take_profit < entry < stop_loss):
            errors.append(f"{label} SELL targets are on the wrong side of entry.")

    started = _positive_number(row.get("started_with_usd"))
    sl_amount = _positive_number(row.get("sl_amount_usd"))
    tp_amount = _positive_number(row.get("tp_amount_usd"))
    if started is not None and sl_amount is not None and sl_amount > started:
        errors.append(f"{label}.sl_amount_usd exceeds started_with_usd.")
    if started is not None and tp_amount is not None and tp_amount > started * 2.0:
        errors.append(f"{label}.tp_amount_usd exceeds 2x started_with_usd.")
    return errors


def _validate_rows(rows: Any, source: str, *, limit: int = 100) -> tuple[int, list[str]]:
    if not isinstance(rows, list):
        return 0, [f"{source} payload is not a list."]
    errors: list[str] = []
    checked = 0
    for index, row in enumerate(rows[:limit]):
        if not isinstance(row, dict):
            errors.append(f"{source}[{index}] is not an object.")
            continue
        checked += 1
        errors.extend(_validate_value_row(row, source, index))
    return checked, errors


def _usd_per_price_distance(*, symbol_info: dict[str, Any] | None, volume: float, contract_size: float = 0.0) -> float:
    if volume <= 0:
        return 0.0
    info = dict(symbol_info or {})
    tick_value = float(info.get("trade_tick_value", 0.0) or 0.0)
    tick_size = float(info.get("trade_tick_size", 0.0) or 0.0)
    point = float(info.get("point", 0.0) or 0.0)
    contract = float(info.get("trade_contract_size", 0.0) or contract_size or 0.0)
    contract_based = volume * contract if contract > 0 else 0.0
    if tick_value > 0:
        unit = tick_size if tick_size > 0 else point
        if unit > 0:
            tick_based = volume * (tick_value / unit)
            if contract_based > 0 and tick_based > 0:
                if tick_based < contract_based * 0.5:
                    return contract_based
                if contract_based < tick_based * 0.5:
                    return tick_based
                return max(tick_based, contract_based)
            return tick_based
    return contract_based


def _dollar_target_to_price(
    *,
    action: str,
    dollar_amount: float,
    volume: float,
    contract_size: float,
    reference_price: float,
    is_stop_loss: bool,
    symbol_info: dict[str, Any] | None = None,
) -> float | None:
    if dollar_amount <= 0 or volume <= 0 or contract_size <= 0 or reference_price <= 0:
        return None
    usd_per_unit = _usd_per_price_distance(
        symbol_info=symbol_info,
        volume=volume,
        contract_size=contract_size,
    )
    if usd_per_unit <= 0:
        return None
    distance = dollar_amount / usd_per_unit
    side = str(action or "").strip().upper()
    if side == "SELL":
        return reference_price + distance if is_stop_loss else reference_price - distance
    return reference_price - distance if is_stop_loss else reference_price + distance


def _conversion_self_checks() -> dict[str, Any]:
    errors: list[str] = []
    silver_info = {
        "trade_contract_size": 5000.0,
        "trade_tick_value": 0.5,
        "trade_tick_size": 0.001,
        "point": 0.001,
    }
    tick_info = {"trade_tick_value": 1.0, "trade_tick_size": 0.01}
    silver_usd = _usd_per_price_distance(symbol_info=silver_info, volume=1.21)
    tick_based_usd = _usd_per_price_distance(symbol_info=tick_info, volume=2.0, contract_size=1.0)
    buy_sl_price = _dollar_target_to_price(
        action="BUY",
        dollar_amount=50.0,
        volume=2.0,
        contract_size=1.0,
        reference_price=10.0,
        is_stop_loss=True,
        symbol_info=tick_info,
    )
    if round(silver_usd, 6) != 6050.0:
        errors.append(f"silver usd-per-price expected 6050.0, got {silver_usd}.")
    if round(tick_based_usd, 6) != 200.0:
        errors.append(f"tick-based usd-per-price expected 200.0, got {tick_based_usd}.")
    if buy_sl_price is None or round(buy_sl_price, 6) != 9.75:
        errors.append(f"BUY $50 SL price expected 9.75, got {buy_sl_price}.")
    return {
        "ok": not errors,
        "checked": 3,
        "errors": errors,
        "sample": {
            "silver_usd_per_price": silver_usd,
            "tick_based_usd_per_price": tick_based_usd,
            "buy_sl_price": buy_sl_price,
        },
    }


def build_report(
    api_base: str,
    *,
    expect_live_ready: bool = False,
    expect_demo_rehearsal: bool = False,
) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    api_health_refresh: dict[str, Any] | None = None
    strict_provider_check = expect_live_ready or expect_demo_rehearsal
    if strict_provider_check:
        api_health_refresh = _refresh_api_health_for_strict_check(api_base, warnings)
    status = _json_get(api_base, "/status", timeout=25.0)

    markets: dict[str, Any]
    try:
        markets = _json_get(
            api_base,
            "/market/available-symbols",
            timeout=30.0,
            params={"tradeable_only": "false"},
        )
    except Exception as exc:
        warnings.append(str(exc))
        markets = {}

    auto_trade: dict[str, Any]
    try:
        auto_trade = _json_get(api_base, "/auto-trade/status", timeout=20.0)
    except Exception as exc:
        warnings.append(str(exc))
        auto_trade = {}

    research_status: dict[str, Any]
    try:
        research_status = _json_get(api_base, "/research/status", timeout=25.0)
    except Exception as exc:
        warnings.append(f"Research/meta-model status unavailable: {exc}")
        research_status = {}

    positions_payload: Any = []
    if bool(status.get("connected")):
        try:
            positions_payload = _json_any_get(api_base, "/positions", timeout=20.0)
        except Exception as exc:
            warnings.append(f"Positions unavailable: {exc}")

    trade_history: dict[str, Any]
    try:
        trade_history = _json_get(api_base, "/trade-history", timeout=25.0)
    except Exception as exc:
        warnings.append(f"Trade history unavailable: {exc}")
        trade_history = {}

    auto_activity: dict[str, Any]
    try:
        auto_activity = _json_get(api_base, "/auto-trade/activity", timeout=25.0)
    except Exception as exc:
        warnings.append(f"Auto-trade activity unavailable: {exc}")
        auto_activity = {}

    categories = _category_counts(markets)
    flat_symbols = markets.get("symbols") if isinstance(markets.get("symbols"), list) else []
    tick_candidates = [
        str(item.get("name") or "").strip()
        for item in flat_symbols
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    tick_report: dict[str, Any]
    if bool(status.get("connected")) and tick_candidates:
        try:
            tick_report = _sample_ticks(api_base, tick_candidates)
        except Exception as exc:
            warnings.append(str(exc))
            tick_report = {"requested": tick_candidates[:12], "returned": 0, "fresh_returned": 0, "sample": {}}
    else:
        tick_report = {"requested": [], "returned": 0, "fresh_returned": 0, "sample": {}}

    live_readiness = _readiness_dict(status, "live_trading_readiness", errors)
    api_keys_health = status.get("api_keys_health") if isinstance(status.get("api_keys_health"), dict) else {}
    publish_readiness = _readiness_dict(status, "publish_readiness", errors)
    services = status.get("services") if isinstance(status.get("services"), dict) else {}
    service_position_manager = services.get("position_manager") if isinstance(services.get("position_manager"), dict) else {}
    service_auto_trader = services.get("auto_trader") if isinstance(services.get("auto_trader"), dict) else {}
    runtime_controls = status.get("runtime_controls") if isinstance(status.get("runtime_controls"), dict) else {}
    user_policy = status.get("user_policy") if isinstance(status.get("user_policy"), dict) else {}
    cloud_decision = status.get("cloud_decision") if isinstance(status.get("cloud_decision"), dict) else {}
    platforms_supported = status.get("platforms_supported") if isinstance(status.get("platforms_supported"), list) else []
    auto_trade_platforms_supported = (
        auto_trade.get("platforms_supported")
        if isinstance(auto_trade.get("platforms_supported"), list)
        else []
    )
    connected = bool(status.get("connected"))
    auto_trade_endpoint_enabled = (
        bool(auto_trade.get("runtime_controls", {}).get("auto_trade_enabled"))
        if isinstance(auto_trade.get("runtime_controls"), dict)
        else bool(auto_trade.get("enabled"))
    )
    auto_trade_runtime_enabled = bool(runtime_controls.get("auto_trade_enabled"))
    auto_trade_session_armed = bool(runtime_controls.get("auto_trade_session_armed"))
    auto_trade_running = bool(auto_trade.get("running"))
    service_auto_trade_running = bool(service_auto_trader.get("running"))
    service_auto_trade_desired = bool(service_auto_trader.get("desired_running"))
    scan_interval_raw = runtime_controls.get("auto_trade_scan_interval_seconds", auto_trade.get("scan_interval"))
    try:
        scan_interval_seconds = float(scan_interval_raw)
    except (TypeError, ValueError):
        scan_interval_seconds = None

    if status.get("platform") != REQUIRED_PLATFORM:
        errors.append(f"/status platform must be {REQUIRED_PLATFORM}.")
    if platforms_supported != REQUIRED_PLATFORMS_SUPPORTED:
        errors.append(f"/status platforms_supported must be exactly {REQUIRED_PLATFORMS_SUPPORTED}.")
    if isinstance(live_readiness.get("checks"), dict) and str(live_readiness["checks"].get("platform") or "") != REQUIRED_PLATFORM:
        errors.append("live_trading_readiness checks must report MT5.")
    if auto_trade:
        if str(auto_trade.get("platform") or "") != REQUIRED_PLATFORM:
            errors.append(f"/auto-trade/status platform must be {REQUIRED_PLATFORM}.")
        if auto_trade_platforms_supported != [REQUIRED_PLATFORM]:
            errors.append("/auto-trade/status platforms_supported must be exactly ['mt5'].")
    errors.extend(_legacy_broker_route_errors(api_base))

    key_items = _api_key_items_by_id(api_keys_health)
    missing_required_keys = sorted(REQUIRED_API_KEYS.difference(key_items))
    if not api_keys_health:
        errors.append("/status is missing api_keys_health.")
    elif missing_required_keys:
        errors.append(f"api_keys_health is missing required keys: {', '.join(missing_required_keys)}.")
    elif str(api_keys_health.get("overall_status") or "").lower() not in {"healthy", "warning"}:
        errors.append(f"api_keys_health overall_status is {api_keys_health.get('overall_status') or 'missing'}.")
    for key_id in sorted(REQUIRED_API_KEYS.intersection(key_items)):
        item = key_items[key_id]
        if not bool(item.get("configured")) or not bool(item.get("available")):
            errors.append(f"Required API key {key_id} is not configured and available.")
        if str(item.get("status") or "").lower() in {"inactive", "offline", "degraded", "low_credits", "missing"}:
            errors.append(f"Required API key {key_id} status is {item.get('status')}.")
    if strict_provider_check and str(api_keys_health.get("overall_status") or "").lower() != "healthy":
        errors.append(f"API/provider health is not fully verified: {api_keys_health.get('overall_status') or 'missing'}.")
        for item in api_keys_health.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_status = str(item.get("status") or "").lower()
            if item_status and item_status != "healthy":
                name = str(item.get("name") or item.get("id") or "provider")
                reason = str(item.get("reason") or "").strip()
                errors.append(
                    f"API/provider blocker: {name} status={item.get('status')}"
                    + (f" reason={reason}" if reason else "")
                )
    if expect_live_ready:
        live_checks = live_readiness.get("checks") if isinstance(live_readiness.get("checks"), dict) else {}
        account_is_demo = bool(status.get("is_demo")) or bool(live_checks.get("account_is_demo"))
        if account_is_demo:
            errors.append("Strict live readiness cannot use a demo MT5 account.")
        if not bool(status.get("live_trading_enabled")) or live_checks.get("live_trading_enabled") is False:
            errors.append("Strict live readiness requires backend live trading enabled.")
        if bool(user_policy.get("demo_only_default", True)) or live_checks.get("demo_only_default") is True:
            errors.append("Strict live readiness requires demo-only policy turned off.")
    demo_checks = live_readiness.get("checks") if isinstance(live_readiness.get("checks"), dict) else {}
    account_is_demo = bool(status.get("is_demo")) or bool(demo_checks.get("account_is_demo"))
    demo_only_default = bool(user_policy.get("demo_only_default", True)) or demo_checks.get("demo_only_default") is True
    if expect_demo_rehearsal:
        if not connected or not account_is_demo:
            errors.append("Demo rehearsal requires a connected MT5 demo account.")
        if not demo_only_default:
            errors.append("Demo rehearsal requires demo-only policy enabled.")
        if bool(live_readiness.get("can_execute_live_orders")):
            errors.append("Demo rehearsal must not report real-money live order readiness.")

    if not bool(status.get("gemini_available")):
        errors.append("Gemini is not available.")
    if str(status.get("gemini_state") or "") not in {"available", "ready"}:
        errors.append(f"Gemini state is {status.get('gemini_state') or 'missing'}.")
    if not str(status.get("gemini_active_model") or "").strip():
        errors.append("Gemini active model is missing.")
    if not str(status.get("gemini_primary_model") or "").strip():
        errors.append("Gemini primary model is missing.")

    universe = status.get("universe") if isinstance(status.get("universe"), dict) else {}
    active_asset_classes = {
        str(item or "").strip()
        for item in (universe.get("active_asset_classes") or [])
        if str(item or "").strip()
    }
    missing_universe_classes = sorted(REQUIRED_MARKET_CLASSES.difference(active_asset_classes))
    if missing_universe_classes:
        errors.append(f"Universe is missing active asset classes: {', '.join(missing_universe_classes)}.")

    row_classes = _market_class_names_from_symbols(flat_symbols)
    available_classes = row_classes or _market_class_names(categories)
    missing_available_classes = sorted(REQUIRED_MARKET_CLASSES.difference(available_classes))
    if connected and markets:
        market_source = str(markets.get("source") or "").strip()
        if market_source != "mt5_broker_symbols":
            errors.append(f"Connected MT5 market catalog source must be mt5_broker_symbols, got {market_source or 'missing'}.")
        if missing_available_classes:
            errors.append(f"Connected MT5 market catalog is missing classes: {', '.join(missing_available_classes)}.")
        if int(markets.get("total") or 0) < len(REQUIRED_MARKET_CLASSES):
            errors.append("Connected MT5 market catalog returned too few symbols.")
    elif markets:
        if str(markets.get("source") or "") != "mt5_disconnected_top_markets":
            errors.append("Disconnected MT5 top-market catalog source must be mt5_disconnected_top_markets.")
        if missing_available_classes:
            errors.append(
                "Disconnected MT5 top-market catalog is missing classes: "
                + ", ".join(missing_available_classes)
                + "."
            )
        if int(markets.get("total") or 0) < MIN_DISCONNECTED_TOP_MARKETS:
            errors.append("Disconnected MT5 top-market catalog returned too few symbols.")
    else:
        errors.append("Disconnected MT5 top-market catalog is unavailable.")

    if connected and tick_candidates and int(tick_report.get("returned") or 0) <= 0:
        errors.append("Connected MT5 runtime returned no live ticks for available symbols.")
    if connected and tick_candidates and int(tick_report.get("returned") or 0) > 0 and int(tick_report.get("fresh_returned") or 0) <= 0:
        errors.append("Connected MT5 runtime returned no fresh live ticks for sampled symbols.")
    if connected and tick_candidates and int(tick_report.get("returned") or 0) > 0:
        tick_source = str(tick_report.get("source") or "").strip()
        if tick_source != "mt5_broker_tick":
            errors.append(f"Connected MT5 tick source must be mt5_broker_tick, got {tick_source or 'missing'}.")
    if connected and not bool(service_position_manager.get("running")):
        errors.append("Connected MT5 runtime has position manager stopped.")
    if bool(service_auto_trader.get("running")) and not auto_trade_endpoint_enabled:
        errors.append("Auto-trader is running while auto-trade control is disabled.")
    if auto_trade_running != service_auto_trade_running:
        errors.append("Auto-trade endpoint running state does not match service state.")
    if auto_trade_endpoint_enabled != auto_trade_runtime_enabled:
        errors.append("Auto-trade endpoint enabled state does not match runtime controls.")
    if not connected:
        if auto_trade_runtime_enabled or auto_trade_endpoint_enabled:
            errors.append("Disconnected MT5 runtime must not have auto-trade enabled.")
        if auto_trade_session_armed:
            errors.append("Disconnected MT5 runtime must not have auto-trade session armed.")
        if auto_trade_running or service_auto_trade_running or service_auto_trade_desired:
            errors.append("Disconnected MT5 runtime must not have auto-trader running or desired.")
    if not bool(live_readiness.get("can_execute_live_orders")) and (auto_trade_runtime_enabled or auto_trade_session_armed):
        errors.append("Auto-trade cannot be enabled or armed while live trading readiness is blocked.")
    if scan_interval_seconds is None:
        errors.append("Auto-trade scan interval is missing or invalid.")
    elif scan_interval_seconds < 30:
        errors.append("Auto-trade scan interval must be at least 30 seconds.")

    meta_model_active = bool(research_status.get("meta_model_active")) if research_status else False
    meta_model_version = str(research_status.get("meta_model_active_version") or "").strip() if research_status else ""
    if strict_provider_check and (not meta_model_active or not meta_model_version):
        errors.append("Meta-model is not active with an approved version.")
    if strict_provider_check:
        cloud_enabled = bool(cloud_decision.get("enabled"))
        cloud_configured = bool(cloud_decision.get("configured"))
        if not cloud_enabled or not cloud_configured:
            reason = str(cloud_decision.get("disabled_reason") or cloud_decision.get("last_error") or "").strip()
            errors.append(
                "Cloud decision service is not live-ready"
                + (f": {reason}" if reason else ".")
            )

    positions_checked, position_value_errors = _validate_rows(positions_payload, "positions")
    errors.extend(position_value_errors)
    trade_rows = trade_history.get("trades") if isinstance(trade_history.get("trades"), list) else []
    trade_history_source = str(trade_history.get("source") or "").strip()
    if _is_legacy_broker_source(trade_history_source):
        errors.append(f"Trade history source must be MT5-only, got {trade_history_source}.")
    trade_history_checked, trade_history_value_errors = _validate_rows(trade_rows, "trade_history")
    errors.extend(trade_history_value_errors)
    live_activity_checked, live_activity_value_errors = _validate_rows(
        auto_activity.get("live_activity") if isinstance(auto_activity.get("live_activity"), list) else [],
        "auto_trade.live_activity",
    )
    db_activity_checked, db_activity_value_errors = _validate_rows(
        auto_activity.get("db_activity") if isinstance(auto_activity.get("db_activity"), list) else [],
        "auto_trade.db_activity",
    )
    errors.extend(live_activity_value_errors)
    errors.extend(db_activity_value_errors)
    conversion_checks = _conversion_self_checks()
    if not conversion_checks["ok"]:
        errors.extend(f"value conversion check failed: {message}" for message in conversion_checks["errors"])

    report = {
        "checked_at": int(time.time()),
        "api_base": api_base.rstrip("/"),
        "connection": {
            "connected": connected,
            "platform": status.get("platform"),
            "platforms_supported": platforms_supported,
            "server": (status.get("account") or {}).get("server") if isinstance(status.get("account"), dict) else None,
            "is_demo": bool(status.get("is_demo")),
            "live_trading_enabled": bool(status.get("live_trading_enabled")),
        },
        "live_trading_readiness": {
            "status": live_readiness.get("status", "missing"),
            "can_execute_live_orders": bool(live_readiness.get("can_execute_live_orders")),
            "blockers": live_readiness.get("blockers") if isinstance(live_readiness.get("blockers"), list) else [],
            "warnings": live_readiness.get("warnings") if isinstance(live_readiness.get("warnings"), list) else [],
        },
        "publish_readiness": {
            "status": publish_readiness.get("status", "missing"),
            "score": publish_readiness.get("score"),
            "blockers": publish_readiness.get("blockers") if isinstance(publish_readiness.get("blockers"), list) else [],
            "warnings": publish_readiness.get("warnings") if isinstance(publish_readiness.get("warnings"), list) else [],
        },
        "api_keys_health": {
            "overall_status": api_keys_health.get("overall_status", "missing"),
            "unhealthy_count": int(api_keys_health.get("unhealthy_count") or 0),
            "warning_count": int(api_keys_health.get("warning_count") or 0),
            "strict_refresh": {
                "attempted": bool(strict_provider_check),
                "ok": bool(api_health_refresh and api_health_refresh.get("ok")),
                "gemini_live_verification_ok": bool(
                    api_health_refresh and api_health_refresh.get("gemini_live_verification_ok")
                ),
                "overall_status": (
                    api_health_refresh.get("overall_status")
                    if isinstance(api_health_refresh, dict)
                    else None
                ),
            },
            "items": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "configured": bool(item.get("configured")),
                    "available": bool(item.get("available")),
                    "status": item.get("status"),
                    "reason": item.get("reason") or "",
                }
                for item in (api_keys_health.get("items") or [])
                if isinstance(item, dict)
            ],
        },
        "gemini": {
            "available": bool(status.get("gemini_available")),
            "state": status.get("gemini_state"),
            "primary_model": status.get("gemini_primary_model"),
            "active_model": status.get("gemini_active_model"),
            "using_fallback_model": bool(status.get("gemini_using_fallback_model")),
            "credits_pct": status.get("gemini_credits_pct"),
        },
        "meta_model": {
            "status_endpoint_available": bool(research_status),
            "enabled": bool(research_status.get("enabled")) if research_status else None,
            "active": meta_model_active,
            "active_version": meta_model_version,
            "inactive_reason": research_status.get("meta_model_inactive_reason") if research_status else "status_unavailable",
            "incremental_training": research_status.get("incremental_training") if research_status else None,
        },
        "markets": {
            "total": int(markets.get("total") or 0),
            "flat_symbols": len(flat_symbols),
            "category_counts": categories,
            "required_classes": sorted(REQUIRED_MARKET_CLASSES),
            "available_classes": sorted(available_classes),
            "missing_classes": missing_available_classes,
            "source": markets.get("source"),
        },
        "ticks": tick_report,
        "services": {
            "position_manager": {
                "desired_running": bool(service_position_manager.get("desired_running")),
                "running": bool(service_position_manager.get("running")),
                "last_error": service_position_manager.get("last_error"),
            },
            "auto_trader": {
                "desired_running": bool(service_auto_trader.get("desired_running")),
                "running": bool(service_auto_trader.get("running")),
                "last_error": service_auto_trader.get("last_error"),
            },
        },
        "cloud_decision": cloud_decision,
        "auto_trade": {
            "platform": auto_trade.get("platform"),
            "platforms_supported": auto_trade_platforms_supported,
            "running": auto_trade_running,
            "enabled": auto_trade_endpoint_enabled,
            "recent_decisions": len(auto_trade.get("recent_trades") or []) if isinstance(auto_trade.get("recent_trades"), list) else 0,
        },
        "auto_trade_safety": {
            "runtime_enabled": auto_trade_runtime_enabled,
            "session_armed": auto_trade_session_armed,
            "endpoint_enabled": auto_trade_endpoint_enabled,
            "endpoint_running": auto_trade_running,
            "service_desired_running": service_auto_trade_desired,
            "service_running": service_auto_trade_running,
            "demo_only_default": bool(user_policy.get("demo_only_default", True)),
            "panic_stop": bool(runtime_controls.get("panic_stop")),
            "scan_interval_seconds": scan_interval_raw,
            "budget_mode": runtime_controls.get("auto_trade_budget_mode") or auto_trade.get("budget_mode"),
            "budget_usd": runtime_controls.get("auto_trade_budget_usd", auto_trade.get("budget_usd")),
        },
        "demo_rehearsal_readiness": {
            "ready": (
                connected
                and account_is_demo
                and demo_only_default
                and not bool(live_readiness.get("can_execute_live_orders"))
                and int(tick_report.get("fresh_returned") or 0) > 0
                and not missing_available_classes
                and meta_model_active
                and bool(cloud_decision.get("enabled"))
                and bool(cloud_decision.get("configured"))
                and str(api_keys_health.get("overall_status") or "").lower() == "healthy"
            ),
            "account_is_demo": account_is_demo,
            "demo_only_default": demo_only_default,
            "fresh_ticks": int(tick_report.get("fresh_returned") or 0),
            "live_money_orders_disabled": not bool(live_readiness.get("can_execute_live_orders")),
        },
        "value_integrity": {
            "positions_checked": positions_checked,
            "trade_history_checked": trade_history_checked,
            "auto_trade_live_activity_checked": live_activity_checked,
            "auto_trade_db_activity_checked": db_activity_checked,
            "trade_history_source": trade_history.get("source"),
            "errors": position_value_errors + trade_history_value_errors + live_activity_value_errors + db_activity_value_errors,
            "conversion_checks": conversion_checks,
        },
    }
    return report, errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LinkTrade runtime readiness against a running backend API.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api", help="Backend API base URL ending with /api")
    parser.add_argument(
        "--expect-live-ready",
        action="store_true",
        help="Exit nonzero unless live_trading_readiness.can_execute_live_orders is true.",
    )
    parser.add_argument(
        "--expect-demo-rehearsal",
        action="store_true",
        help="Exit nonzero unless a connected MT5 demo account passes operational rehearsal checks.",
    )
    args = parser.parse_args()

    try:
        report, errors, warnings = build_report(
            args.api_base,
            expect_live_ready=args.expect_live_ready,
            expect_demo_rehearsal=args.expect_demo_rehearsal,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2

    ok = not errors
    if args.expect_live_ready and not report["live_trading_readiness"]["can_execute_live_orders"]:
        ok = False
        errors.append("Live trading readiness is not ready.")
        for blocker in report["live_trading_readiness"].get("blockers", []):
            message = f"Live blocker: {blocker}"
            if message not in errors:
                errors.append(message)
        for warning in report["live_trading_readiness"].get("warnings", []):
            message = f"Live warning: {warning}"
            if message not in warnings:
                warnings.append(message)
    if args.expect_demo_rehearsal and not report.get("demo_rehearsal_readiness", {}).get("ready"):
        ok = False
        if "Demo rehearsal readiness is not ready." not in errors:
            errors.append("Demo rehearsal readiness is not ready.")
    output = {"ok": ok, "errors": errors, "warnings": warnings, "report": report}
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
