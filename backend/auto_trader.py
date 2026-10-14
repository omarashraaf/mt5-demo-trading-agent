"""
Auto-Trader: Background task that scans markets and auto-executes trades
when confidence exceeds the user's threshold.

Also manages the PositionManager which autonomously handles open positions
(trailing stops, breakeven, trend reversal exit, averaging down, time exit).

Safety: live MT5 accounts are allowed only when the backend live-trading
flag is enabled and demo-only policy is off. Every trade has stop-loss.
The risk engine must approve every trade before execution.
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from typing import Optional

from mt5.connector import MT5Connector
from mt5.market_data import MarketDataService
from mt5.execution import ExecutionEngine, OrderRequest
from agent.interface import TradeSignal
from agent.smart_agent import get_asset_class
from risk.rules import RiskEngine
from storage.db import Database
from position_manager import PositionManager
from services.background_state import BackgroundServiceState
from services.execution_service import ExecutionService
from services.symbol_universe_service import AGGRESSIVE_CFD_FOCUS_SYMBOLS
from config import config

logger = logging.getLogger(__name__)

AUTO_EVALUATION_TIMEOUT_SECONDS = 14.0
AUTO_EVALUATION_TIMEOUT_CAP_SECONDS = 24.0
AUTO_SCAN_SYMBOL_LIMIT = 8
MIN_STRATEGY_FORCE_EXIT_SECONDS = 600  # 10 minutes minimum timer for forced exits.
AUTO_MIN_ENTRY_SPACING_SECONDS = 180
AUTO_NEAR_ENTRY_RECOVERY_PCT = 0.0015  # 0.15% around entry is considered "near recovery".
MAX_AUTO_QUOTE_SPREAD_PCT = 0.05


def _is_aggressive_cfd_focus_symbol(universe_service, symbol: str) -> bool:
    try:
        canonical = universe_service.canonical_symbol(symbol)
        expected = universe_service.expected_category_for_symbol(canonical)
    except Exception:
        return False
    return expected in {"Indices", "Commodities"}


def _recovery_gate_thresholds(settings, universe_service, symbol: str, *, total_profit: float) -> tuple[float, float]:
    mode = str(getattr(settings, "policy_mode", "") or "").lower()
    if mode == "aggressive" and _is_aggressive_cfd_focus_symbol(universe_service, symbol):
        # Aggressive CFD mode should stay active, but only after the setup has
        # already passed trade-quality, empirical-model, and portfolio gates.
        if total_profit >= 0.0:
            return (0.48, 0.70)
        return (0.50, 0.72)
    if total_profit >= 0.0:
        return (0.66, 0.80)
    return (0.70, 0.84)


def _is_commodity_like_symbol(symbol: str) -> bool:
    s = str(symbol or "").upper()
    return (
        "XAU" in s
        or "GOLD" in s
        or "XAG" in s
        or "SILVER" in s
        or "WTI" in s
        or "BRENT" in s
        or "USO" in s
        or "BNO" in s
    )


def _auto_eval_timeout_seconds(symbol: str = "") -> float:
    base = float(AUTO_EVALUATION_TIMEOUT_SECONDS)
    # Keep enough headroom for Gemini/news overlays, but avoid stretching
    # each per-symbol evaluation into minute-scale latency.
    gemini_budget = float(config.GEMINI_TIMEOUT_SECONDS) + 6.0
    timeout = min(max(base, gemini_budget), float(AUTO_EVALUATION_TIMEOUT_CAP_SECONDS))
    # Commodity feeds + news overlays tend to be the noisiest; give them
    # a small extra window to avoid repetitive timeout HOLD rows.
    if _is_commodity_like_symbol(symbol):
        timeout = min(float(AUTO_EVALUATION_TIMEOUT_CAP_SECONDS), timeout + 3.0)
    return timeout


def _usable_quote_from_prices(bid: float, ask: float) -> tuple[bool, str]:
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= 0.0:
        return False, "bid/ask unavailable"
    if ask < bid:
        return False, "ask is below bid"
    mid = (bid + ask) / 2.0
    if mid <= 0.0:
        return False, "mid price is unavailable"
    spread_pct = max(0.0, ask - bid) / mid
    if spread_pct > MAX_AUTO_QUOTE_SPREAD_PCT:
        return False, f"spread is too wide ({spread_pct:.1%})"
    return True, ""


def _fresh_live_tick_ok(tick, *, now: float | None = None, max_age_seconds: float = 120.0) -> tuple[bool, str]:
    if tick is None:
        return False, "live quote unavailable"
    try:
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
    except Exception:
        return False, "bid/ask unavailable"
    quote_ok, quote_reason = _usable_quote_from_prices(bid, ask)
    if not quote_ok:
        return False, quote_reason
    try:
        raw_msc = float(getattr(tick, "time_msc", 0.0) or 0.0)
        raw_sec = float(getattr(tick, "time", 0.0) or 0.0)
    except Exception:
        raw_msc = 0.0
        raw_sec = 0.0
    tick_ts = (raw_msc / 1000.0) if raw_msc > 1_000_000_000_000 else raw_sec
    if tick_ts <= 0:
        return False, "quote timestamp unavailable"
    age = (time.time() if now is None else now) - tick_ts
    if age > max_age_seconds:
        return False, f"quote is stale (>{int(max_age_seconds)}s)"
    return True, ""


def _usable_quote_from_row(row: dict) -> bool:
    try:
        bid = float(row.get("bid", 0.0) or 0.0)
        ask = float(row.get("ask", 0.0) or 0.0)
    except Exception:
        return False
    return _usable_quote_from_prices(bid, ask)[0]


def _symbol_info_value(symbol_info, name: str, default=None):
    if symbol_info is None:
        return default
    if isinstance(symbol_info, dict):
        return symbol_info.get(name, default)
    return getattr(symbol_info, name, default)


def _usable_trade_metadata(symbol_info) -> tuple[bool, str]:
    if not symbol_info:
        return False, "symbol metadata unavailable"
    try:
        trade_mode = int(float(_symbol_info_value(symbol_info, "trade_mode", 0) or 0))
    except Exception:
        trade_mode = 0
    if trade_mode == 0:
        return False, "broker trading disabled for this symbol"
    try:
        volume_min = float(_symbol_info_value(symbol_info, "volume_min", 0.0) or 0.0)
        volume_step = float(_symbol_info_value(symbol_info, "volume_step", 0.0) or 0.0)
        contract_size = float(_symbol_info_value(symbol_info, "trade_contract_size", 0.0) or 0.0)
    except Exception:
        return False, "invalid broker symbol metadata"
    if volume_min <= 0.0 or volume_step <= 0.0:
        return False, "broker volume limits unavailable"
    if contract_size <= 0.0:
        return False, "contract size unavailable"
    return True, ""


def _auto_execution_thresholds(settings, quality) -> tuple[float, float]:
    mode = str(getattr(settings, "policy_mode", "") or "").lower()
    base_confidence = float(
        getattr(settings, "auto_trade_min_confidence", 0.0)
        or getattr(settings, "min_confidence_threshold", 0.0)
        or 0.65
    )
    base_quality = float(getattr(quality, "threshold", 0.0) or base_confidence or 0.68)
    if mode == "aggressive":
        return max(0.45, base_confidence), max(base_quality, 0.68)
    if mode == "safe":
        return max(0.70, base_confidence), max(base_quality, 0.84)
    return max(0.62, base_confidence), max(base_quality, 0.76)


class AutoTrader:
    def __init__(
        self,
        connector: MT5Connector,
        market_data: MarketDataService,
        execution: ExecutionEngine,
        risk_engine: RiskEngine,
        agents: dict,
        signal_pipeline,
        execution_service: ExecutionService | None = None,
    ):
        self.connector = connector
        self.market_data = market_data
        self.execution = execution
        self.risk_engine = risk_engine
        self.agents = agents
        self.signal_pipeline = signal_pipeline
        self.execution_service = execution_service or ExecutionService(
            execution_engine=execution,
            risk_service=signal_pipeline.risk_service,
            signal_pipeline=signal_pipeline,
        )
        self.db: Optional[Database] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_scan: float = 0
        self._trade_log: list[dict] = []  # Recent auto-trade log for UI
        self._state = BackgroundServiceState(name="auto_trader")
        self._gemini_quota_cooldown_until = 0.0
        self._auto_agent_name = "SmartAgent"
        self._cadence_buy_every_seconds = 0
        self._cadence_sell_after_seconds = 0
        self._fixed_lot_size = 0.0
        self._allocation_pct_free_margin = 0.0
        self._cadence_entry_action = "BUY"
        self._last_cadence_entry_ts: dict[str, float] = {}
        self._market_selection_mode = ""
        self._market_region_hint = ""
        self._symbol_confidence_cache: dict[str, tuple[float, float]] = {}
        self._symbol_direction_cooldown_until: dict[str, float] = {}
        self._symbol_direction_last_opened_at: dict[str, float] = {}
        self._symbol_timeout_streak: dict[str, int] = {}
        self._symbol_timeout_cooldown_until: dict[str, float] = {}
        self._cycles_without_entry: int = 0
        self._symbol_rotation_offset: int = 0
        self._recent_performance_cache: tuple[float, dict] = (0.0, {})
        self.kill_switch_state_getter = None

        # Position Manager - autonomous management of open positions
        self.position_manager = PositionManager(
            connector=connector,
            market_data=market_data,
            execution=execution,
            risk_engine=risk_engine,
            agents=agents,
        )

    def set_database(self, db: Database):
        self.db = db
        self.signal_pipeline.set_database(db)
        self.execution_service.db = db
        self.position_manager.set_database(db)

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def last_scan_time(self) -> float:
        return self._last_scan

    @property
    def recent_trades(self) -> list[dict]:
        return self._trade_log[-20:]  # Last 20 auto-trades

    def _live_trading_policy_block_reason(self) -> str | None:
        try:
            is_demo = bool(self.connector.is_demo())
        except Exception:
            is_demo = True
        if is_demo:
            return None
        demo_only = bool(getattr(getattr(self.risk_engine, "user_policy", None), "demo_only_default", True))
        if demo_only or not bool(config.LIVE_TRADING_ENABLED):
            return "Live auto-trading requires demo-only mode off and LIVE_TRADING_ENABLED=true."
        return None

    def _kill_switch_block_reason(self) -> str | None:
        state = self.kill_switch_state_getter() if callable(self.kill_switch_state_getter) else None
        if bool(getattr(state, "enabled", False)):
            reason = str(getattr(state, "reason", "") or "Trading kill switch is active.")
            return f"Global trading kill switch is active. {reason}"
        return None

    @property
    def combined_activity(self) -> list[dict]:
        """Merge trade log + position manager activity, sorted by time."""
        trades = [
            {**t, "source": "scanner"} for t in self._trade_log
        ]
        pm_activity = [
            {**a, "source": "position_manager"} for a in self.position_manager.activity_log
        ]
        combined = trades + pm_activity
        combined.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return combined[:50]

    async def _recent_performance_gate(
        self,
        *,
        symbol: str = "",
        confidence: float,
        quality_score: float,
    ) -> tuple[bool, str]:
        """Raise the entry bar when recent closed-trade quality is weak."""
        if self.db is None:
            return True, ""
        now = time.time()
        cached_at, cached = self._recent_performance_cache
        if now - cached_at > 60.0:
            try:
                if hasattr(self.db, "get_recent_app_trade_outcomes"):
                    outcomes = await asyncio.wait_for(self.db.get_recent_app_trade_outcomes(limit=50), timeout=0.8)
                else:
                    outcomes = await asyncio.wait_for(self.db.get_trade_outcomes(limit=50), timeout=0.8)
            except Exception:
                outcomes = []
            closed = [
                row for row in outcomes
                if row is not None and str(row.get("symbol") or "").strip()
            ][:50]
            wins = sum(1 for row in closed if float(row.get("profit") or 0.0) > 0.0)
            total = len(closed)
            total_profit = sum(float(row.get("profit") or 0.0) for row in closed)
            cached = {
                "total": total,
                "wins": wins,
                "win_rate": (wins / total) if total else 1.0,
                "profit": total_profit,
            }
            self._recent_performance_cache = (now, cached)

        meta_service = getattr(self.signal_pipeline, "meta_model_service", None)
        meta_active = bool(getattr(meta_service, "is_active", False))
        if not meta_active:
            required_quality = 0.84
            required_confidence = 0.70
            if quality_score < required_quality or confidence < required_confidence:
                return (
                    False,
                    (
                        "Blocked: meta-model is not active, so auto-trade requires "
                        f"confidence >= {required_confidence:.0%} and quality >= {required_quality:.0%}; "
                        f"got confidence {confidence:.0%}, quality {quality_score:.0%}."
                    ),
                )

        total = int(cached.get("total") or 0)
        if total < 20:
            return True, ""
        win_rate = float(cached.get("win_rate") or 0.0)
        total_profit = float(cached.get("profit") or 0.0)
        if win_rate >= 0.45 and total_profit >= 0.0:
            return True, ""

        required_confidence, required_quality = _recovery_gate_thresholds(
            self.risk_engine.settings,
            self.signal_pipeline.universe_service,
            symbol,
            total_profit=total_profit,
        )
        if quality_score >= required_quality and confidence >= required_confidence:
            return True, ""
        return (
            False,
            (
                "Blocked: recent closed-trade win rate is "
                f"{win_rate:.0%}, so recovery mode requires confidence >= {required_confidence:.0%} "
                f"and quality >= {required_quality:.0%}; got confidence {confidence:.0%}, "
                f"quality {quality_score:.0%}."
            ),
        )

    def _empirical_execution_quality_gate(self, decision) -> str:
        """Block historically weak app-owned setup slices before order placement."""
        try:
            signal = decision.signal_decision.final_signal
            context = decision.signal_decision.market_context
            quality = decision.trade_quality_assessment
            portfolio = decision.portfolio_risk_assessment
            plan = decision.position_management_plan
        except Exception:
            return ""

        confidence = self._finite_float(getattr(signal, "confidence", 0.0), 0.0)
        quality_score = self._finite_float(getattr(quality, "final_trade_quality_score", 0.0), 0.0)
        trend_alignment = self._finite_float(getattr(quality, "trend_alignment_score", 0.0), 0.0)
        free_margin_pct = self._finite_float(getattr(portfolio, "projected_free_margin_pct", 100.0), 100.0)
        margin_util_pct = self._finite_float(getattr(portfolio, "projected_margin_utilization_pct", 0.0), 0.0)
        atr_regime = str(getattr(plan, "atr_regime", "") or "").strip().lower()
        tick = getattr(context, "tick", {}) or {}
        spread = self._finite_float(tick.get("spread", 0.0) if isinstance(tick, dict) else 0.0, 0.0)

        if quality_score < 0.70 and free_margin_pct < 99.88:
            return (
                "Blocked: historical app-owned outcomes show low-quality setups with reduced free margin "
                "have poor win rate. Require quality >= 70% or projected free margin >= 99.88%."
            )
        if spread >= 28.0 and free_margin_pct < 99.88:
            return (
                "Blocked: historical app-owned outcomes show wide-spread setups with reduced free margin "
                "underperform. Require spread < 28 points or projected free margin >= 99.88%."
            )
        if confidence < 0.58 and spread >= 7.0 and margin_util_pct >= 0.35:
            return (
                "Blocked: historical app-owned outcomes show low-confidence entries with spread pressure "
                "and margin utilization underperform. Require confidence >= 58%, spread < 7, or lower margin use."
            )
        if atr_regime == "medium" and spread >= 7.0 and trend_alignment < 0.92:
            return (
                "Blocked: historical app-owned outcomes show medium-volatility setups with spread pressure "
                "and weak trend alignment underperform. Require trend alignment >= 92% or spread < 7."
            )
        return ""

    @staticmethod
    def _finite_float(value, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed):
            return float(default)
        return parsed

    def status_snapshot(self) -> dict:
        state = self._state.model_dump()
        state["last_scan"] = self._last_scan
        state["recent_trades"] = self.recent_trades
        return state

    def _cache_symbol_confidence(self, symbol: str, confidence: float):
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        try:
            conf = float(confidence or 0.0)
        except (TypeError, ValueError):
            return
        if not math.isfinite(conf):
            return
        conf = max(0.0, min(0.95, conf))
        if conf <= 0.0:
            return
        self._symbol_confidence_cache[sym] = (time.time(), conf)

    def _cached_symbol_confidence(self, symbol: str) -> float:
        sym = str(symbol or "").strip().upper()
        row = self._symbol_confidence_cache.get(sym)
        if not row:
            return 0.0
        ts, conf = row
        try:
            ts_value = float(ts or 0.0)
            conf_value = float(conf or 0.0)
        except (TypeError, ValueError):
            self._symbol_confidence_cache.pop(sym, None)
            return 0.0
        if not (math.isfinite(ts_value) and math.isfinite(conf_value)):
            self._symbol_confidence_cache.pop(sym, None)
            return 0.0
        if (time.time() - ts_value) > 1800.0:
            self._symbol_confidence_cache.pop(sym, None)
            return 0.0
        return max(0.0, min(0.95, conf_value))

    def _normalized_force_exit_seconds(self, raw_value: int | float | None) -> int:
        try:
            value = int(float(raw_value or 0))
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return 0
        if value < MIN_STRATEGY_FORCE_EXIT_SECONDS:
            logger.info(
                "Ignoring short force-exit timer (%ss); minimum is %ss.",
                value,
                MIN_STRATEGY_FORCE_EXIT_SECONDS,
            )
            return 0
        return value

    def _symbol_timeout_cooldown_seconds(self, streak: int) -> float:
        if streak >= 5:
            return 900.0
        if streak >= 3:
            return 420.0
        if streak >= 2:
            return 180.0
        return 0.0

    def _rotating_scan_selection(self, symbols: list[str], limit: int) -> list[str]:
        if len(symbols) <= limit:
            return symbols[:limit]
        # After several empty cycles, rotate through a broader pool instead of
        # hammering the same stale shortlist forever.
        if self._cycles_without_entry < 4:
            return symbols[:limit]
        start = int(self._symbol_rotation_offset % len(symbols))
        selected: list[str] = []
        seen: set[str] = set()
        for idx in range(len(symbols)):
            sym = symbols[(start + idx) % len(symbols)]
            if sym in seen:
                continue
            seen.add(sym)
            selected.append(sym)
            if len(selected) >= limit:
                break
        step = max(1, limit // 2)
        self._symbol_rotation_offset = (start + step) % len(symbols)
        return selected

    def _direct_executable_aggressive_cfd_symbols(self, existing: list[str], limit: int) -> list[str]:
        symbols: list[str] = list(existing or [])
        seen = {self.signal_pipeline.universe_service.canonical_symbol(sym) for sym in symbols}
        for preferred in AGGRESSIVE_CFD_FOCUS_SYMBOLS:
            canonical = self.signal_pipeline.universe_service.canonical_symbol(preferred)
            if canonical in seen:
                continue
            broker_candidates = self.signal_pipeline.universe_service.preferred_broker_names(preferred) or [preferred]
            actual = ""
            tick = None
            symbol_info = None
            for broker_symbol in broker_candidates:
                try:
                    candidate_tick = self.market_data.get_tick(broker_symbol)
                    candidate_info = self.market_data.get_symbol_info(broker_symbol)
                except Exception:
                    continue
                if not candidate_tick or not candidate_info:
                    continue
                actual = broker_symbol
                tick = candidate_tick
                symbol_info = candidate_info
                break
            if not actual:
                continue
            if not tick or not symbol_info:
                continue
            quote_ok, _ = _fresh_live_tick_ok(tick)
            meta_ok, _ = _usable_trade_metadata(symbol_info)
            if not quote_ok or not meta_ok:
                continue
            symbols.append(actual)
            seen.add(canonical)
            if len(symbols) >= limit:
                break
        return symbols[:limit]

    def _session_open_symbols(self, symbols: list[str], market_symbols: list[dict]) -> list[str]:
        open_symbols: list[str] = []
        for sym in symbols:
            try:
                guard = self.signal_pipeline.market_session_guard_for_symbol(sym, market_symbols)
            except Exception:
                guard = None
            if guard:
                continue
            open_symbols.append(sym)
        return open_symbols

    def _auto_trade_budget_cap_usd(self, account=None) -> float | None:
        mode = str(getattr(self.risk_engine, "auto_trade_budget_mode", "full_margin") or "full_margin").lower()
        if mode != "custom_budget":
            return None
        raw_budget = getattr(self.risk_engine, "auto_trade_budget_usd", None)
        try:
            budget = float(raw_budget) if raw_budget is not None else 0.0
        except Exception:
            budget = 0.0
        if not math.isfinite(budget) or budget <= 0:
            return None
        return budget

    def _remaining_custom_budget_usd(self, account=None, budget_cap_usd: float | None = None) -> float | None:
        cap = budget_cap_usd if budget_cap_usd is not None else self._auto_trade_budget_cap_usd(account)
        if cap is None:
            return None
        used_margin = 0.0
        if account is not None:
            try:
                parsed_margin = float(getattr(account, "margin", 0.0) or 0.0)
            except Exception:
                parsed_margin = 0.0
            used_margin = max(0.0, parsed_margin) if math.isfinite(parsed_margin) else 0.0
        return max(0.0, float(cap) - used_margin)

    def _auto_trade_budget_ratio(self, account=None) -> float:
        budget_cap = self._auto_trade_budget_cap_usd(account)
        if budget_cap is None or account is None:
            return 1.0
        try:
            balance = float(getattr(account, "balance", 0.0) or 0.0)
        except Exception:
            balance = 0.0
        try:
            equity = float(getattr(account, "equity", 0.0) or 0.0)
        except Exception:
            equity = 0.0
        if not math.isfinite(balance):
            balance = 0.0
        if not math.isfinite(equity):
            equity = 0.0
        reference_capital = balance if balance > 0 else equity
        if reference_capital <= 0:
            return 1.0
        return max(0.0001, min(1.0, budget_cap / reference_capital))

    def _max_single_trade_budget_usd(self, budget_cap_usd: float | None) -> float | None:
        if budget_cap_usd is None or budget_cap_usd <= 0:
            return None
        pct = float(getattr(config, "AUTO_TRADE_MAX_SINGLE_TRADE_BUDGET_PCT", 25.0) or 25.0)
        pct = max(1.0, min(100.0, pct))
        return budget_cap_usd * (pct / 100.0)

    def _execution_allowed(self) -> bool:
        return bool(
            self._running
            and self.connector.connected
            and self.connector.trade_server_connected()
            and self.risk_engine.auto_trade_enabled
            and getattr(self.risk_engine, "auto_trade_session_armed", False)
            and not self.risk_engine.panic_stopped
        )

    def start(self):
        if self.is_running:
            logger.info("Auto-trader already running")
            return
        self._running = True
        self._state.mark_started()
        self._task = asyncio.create_task(self._run_loop())
        self.position_manager.start()
        logger.info("Auto-trader STARTED (with Position Manager)")

    def stop(self, *, keep_position_manager: bool = False):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if keep_position_manager:
            self.position_manager.start()
        else:
            self.position_manager.stop()
        self._state.mark_stopped()
        logger.info(
            "Auto-trader STOPPED (%s Position Manager)",
            "kept" if keep_position_manager else "with",
        )

    async def _run_loop(self):
        """Main auto-trading loop."""
        logger.info("Auto-trade loop started")
        while self._running:
            try:
                self._state.mark_heartbeat()

                if not self._running:
                    break

                # Pre-flight checks
                if not self.connector.connected or not self.connector.trade_server_connected():
                    logger.debug("Auto-trader: not connected, skipping")
                    await asyncio.sleep(1)
                    continue

                if self.risk_engine.panic_stopped:
                    logger.debug("Auto-trader: panic stop active, skipping")
                    await asyncio.sleep(1)
                    continue

                if not self.risk_engine.auto_trade_enabled:
                    logger.debug("Auto-trader: auto-trade disabled, skipping")
                    await asyncio.sleep(1)
                else:
                    await self._scan_and_trade()
                    self._state.mark_cycle()

                if not self._running:
                    break
                interval = self.risk_engine.settings.auto_trade_scan_interval_seconds
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._state.mark_error(e)
                logger.error(f"Auto-trader error: {e}", exc_info=True)
                await asyncio.sleep(10)  # Back off on error

        logger.info("Auto-trade loop ended")
        self._state.mark_stopped()

    async def _scan_and_trade(self):
        """Scan all allowed symbols and auto-execute strong signals."""
        if not self._execution_allowed():
            return
        executed_any_trade = False
        self._last_scan = time.time()
        settings = self.risk_engine.settings
        allowed_canonical_scope = {
            self.signal_pipeline.universe_service.canonical_symbol(str(sym).strip().upper())
            for sym in (settings.allowed_symbols or [])
            if str(sym).strip()
        }
        policy_mode = str(
            getattr(settings, "policy_mode", "")
            or getattr(getattr(self.risk_engine, "user_policy", None), "mode", "")
            or ""
        ).strip().lower()
        aggressive_cfd_focus = policy_mode == "aggressive" and not allowed_canonical_scope
        tradeable_fetch = getattr(self.market_data, "get_tradeable_symbols_cached", None)
        try:
            tradeable_market = (
                tradeable_fetch(ttl_seconds=3.0) if callable(tradeable_fetch) else self.market_data.get_tradeable_symbols()
            )
        except Exception:
            logger.warning("Auto-trader: failed to load tradeable symbols; using empty set this cycle.", exc_info=True)
            tradeable_market = []
        tradeable_market = [
            item for item in tradeable_market
            if isinstance(item, dict)
            and _usable_quote_from_row(item)
        ]
        symbols = self.signal_pipeline.universe_service.resolve_requested_symbols(
            settings.allowed_symbols,
            self.signal_pipeline.universe_service.filter_market_symbols(tradeable_market),
        )

        # Auto-detect symbols if list is empty - prefer liquid, well-known instruments
        if not symbols:
            logger.info("Auto-trader: no symbols configured, auto-detecting...")
            filtered_tradeable = self.signal_pipeline.universe_service.filter_market_symbols(tradeable_market)
            candidate_limit = AUTO_SCAN_SYMBOL_LIMIT
            if self._cycles_without_entry >= 4:
                candidate_limit = min(24, AUTO_SCAN_SYMBOL_LIMIT * 3)
            if aggressive_cfd_focus:
                symbols = self.signal_pipeline.universe_service.aggressive_cfd_symbols(
                    filtered_tradeable,
                    limit=candidate_limit,
                )
                if symbols:
                    logger.info("Auto-trader: aggressive CFD focus enabled: %s", symbols)
            if not symbols:
                try:
                    symbols = await asyncio.wait_for(
                        self.signal_pipeline.auto_trade_candidate_symbols(
                            filtered_tradeable,
                            limit=candidate_limit,
                        ),
                        timeout=8.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Auto-trader candidate discovery timed out; using curated fallback symbols this cycle."
                    )
                    symbols = (
                        self.signal_pipeline.universe_service.aggressive_cfd_symbols(
                            filtered_tradeable,
                            limit=candidate_limit,
                        )
                        if aggressive_cfd_focus
                        else self.signal_pipeline.universe_service.default_auto_trade_symbols(
                            filtered_tradeable,
                            limit=candidate_limit,
                        )
                    )
                except Exception:
                    logger.warning(
                        "Auto-trader candidate discovery failed; using curated fallback symbols this cycle.",
                        exc_info=True,
                    )
                    symbols = (
                        self.signal_pipeline.universe_service.aggressive_cfd_symbols(
                            filtered_tradeable,
                            limit=candidate_limit,
                        )
                        if aggressive_cfd_focus
                        else self.signal_pipeline.universe_service.default_auto_trade_symbols(
                            filtered_tradeable,
                            limit=candidate_limit,
                        )
                    )
            if symbols:
                logger.info("Auto-trader: using event-driven/curated symbol list: %s", symbols)
            else:
                # Stable fallback keeps the scanner productive when event feed
                # is empty or temporarily degraded.
                symbols = (
                    self.signal_pipeline.universe_service.aggressive_cfd_symbols(
                        filtered_tradeable,
                        limit=AUTO_SCAN_SYMBOL_LIMIT,
                    )
                    if aggressive_cfd_focus
                    else self.signal_pipeline.universe_service.default_auto_trade_symbols(
                        filtered_tradeable,
                        limit=AUTO_SCAN_SYMBOL_LIMIT,
                    )
                )
                if symbols:
                    logger.info("Auto-trader: using curated fallback symbol list: %s", symbols)
                else:
                    logger.info("Auto-trader: no event-backed candidates available this cycle")

        if aggressive_cfd_focus:
            symbols = self._direct_executable_aggressive_cfd_symbols(symbols, min(24, AUTO_SCAN_SYMBOL_LIMIT * 3))
            session_open = self._session_open_symbols(symbols, tradeable_market)
            if session_open:
                closed_count = len(symbols) - len(session_open)
                if closed_count > 0:
                    logger.info(
                        "Auto-trader: removed %s closed aggressive CFD symbol(s) from this cycle.",
                        closed_count,
                    )
                symbols = session_open

        # De-duplicate by canonical symbol to avoid repeated same-symbol gating in one scan.
        deduped: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            canonical = self.signal_pipeline.universe_service.canonical_symbol(sym)
            if canonical in seen:
                continue
            seen.add(canonical)
            deduped.append(sym)
        # If UI selection grew too large (or was accidentally expanded), keep auto-trader
        # focused on liquid fallback instruments first instead of long-tail symbols.
        filtered_tradeable = self.signal_pipeline.universe_service.filter_market_symbols(tradeable_market)
        market_meta_by_name = {
            str(item.get("name") or "").strip().upper(): item
            for item in filtered_tradeable
            if str(item.get("name") or "").strip()
        }
        fallback_priority: dict[str, int] = {}
        priority_symbols = (
            self.signal_pipeline.universe_service.aggressive_cfd_symbols(filtered_tradeable)
            if aggressive_cfd_focus
            else list(config.AUTO_TRADE_FALLBACK_SYMBOLS or [])
        )
        for idx, fallback_symbol in enumerate(priority_symbols):
            canonical = self.signal_pipeline.universe_service.canonical_symbol(fallback_symbol)
            if canonical and canonical not in fallback_priority:
                fallback_priority[canonical] = idx

        def _symbol_rank(sym: str) -> tuple[int, int, int, float, str]:
            raw = str(sym or "").strip().upper()
            canonical = self.signal_pipeline.universe_service.canonical_symbol(raw)
            meta = market_meta_by_name.get(raw, {})
            priority = fallback_priority.get(canonical, 999)
            trade_enabled = 0 if bool(meta.get("trade_enabled")) else 1
            visible = 0 if bool(meta.get("visible")) else 1
            spread_raw = meta.get("spread")
            try:
                spread = float(spread_raw) if spread_raw is not None else 9999.0
            except Exception:
                spread = 9999.0
            return (priority, trade_enabled, visible, spread, raw)

        sorted_symbols = sorted(deduped, key=_symbol_rank)
        if len(sorted_symbols) > AUTO_SCAN_SYMBOL_LIMIT:
            # When scope is very large, prioritize curated fallback instruments first
            # (liquid indices/stocks/commodities) to reduce timeout churn on long-tail symbols.
            by_canonical: dict[str, str] = {}
            for s in sorted_symbols:
                can = self.signal_pipeline.universe_service.canonical_symbol(s)
                if can and can not in by_canonical:
                    by_canonical[can] = s
            preferred: list[str] = []
            for fallback_symbol in priority_symbols:
                can = self.signal_pipeline.universe_service.canonical_symbol(fallback_symbol)
                mapped = by_canonical.get(can)
                if mapped and mapped not in preferred:
                    preferred.append(mapped)
                if len(preferred) >= AUTO_SCAN_SYMBOL_LIMIT:
                    break
            if preferred:
                symbols = self._rotating_scan_selection(preferred, AUTO_SCAN_SYMBOL_LIMIT)
            else:
                symbols = self._rotating_scan_selection(sorted_symbols, AUTO_SCAN_SYMBOL_LIMIT)
        else:
            symbols = sorted_symbols[:AUTO_SCAN_SYMBOL_LIMIT]

        if self._cycles_without_entry >= 4 and len(symbols) >= AUTO_SCAN_SYMBOL_LIMIT:
            logger.info(
                "Auto-trader: rotating scan shortlist after %s no-entry cycles: %s",
                self._cycles_without_entry,
                symbols,
            )

        account = self.connector.refresh_account()
        if not account:
            return

        # Daily realized-loss brake (from closed trade history):
        # if we breach the configured daily drawdown, pause auto-trading to
        # prevent continued loss spirals until user intervention.
        if self.db is not None:
            try:
                outcomes = await asyncio.wait_for(
                    self.db.get_trade_outcomes(limit=500),
                    timeout=3.0,
                )
                now_ts = time.time()
                day_cutoff = now_ts - (24 * 60 * 60)
                realized_24h = self._runtime_realized_pnl_since(outcomes, since_ts=day_cutoff)
                budget_ref = float(
                    self._auto_trade_budget_cap_usd(account)
                    or getattr(account, "balance", 0.0)
                    or getattr(account, "equity", 0.0)
                    or 0.0
                )
                max_daily_loss_pct = float(
                    getattr(settings, "max_daily_loss_percent", 0.0)
                    or getattr(self.risk_engine.user_policy, "max_daily_drawdown", 4.5)
                    or 4.5
                )
                loss_limit_usd = max(1.0, budget_ref * max(0.5, max_daily_loss_pct) / 100.0)
                if realized_24h <= -loss_limit_usd:
                    detail = (
                        f"Daily loss brake triggered: realized P/L last 24h is ${realized_24h:.2f}, "
                        f"limit is -${loss_limit_usd:.2f}. Auto-trading paused for safety."
                    )
                    self._trade_log.append({
                        "timestamp": now_ts,
                        "symbol": "SYSTEM",
                        "action": "HOLD",
                        "confidence": 0.0,
                        "quality_score": 0.0,
                        "detail": detail,
                        "success": False,
                    })
                    self._trade_log = self._trade_log[-50:]
                    self.risk_engine.auto_trade_enabled = False
                    self.risk_engine.auto_trade_session_armed = False
                    logger.warning(detail)
                    return
            except asyncio.TimeoutError:
                logger.warning("Auto-trader daily realized-loss brake check timed out; skipping this cycle.")
            except Exception:
                logger.debug("Auto-trader daily realized-loss brake check skipped", exc_info=True)

        try:
            all_positions = await asyncio.wait_for(
                asyncio.to_thread(self.execution.get_positions),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Auto-trader position snapshot timed out; using empty position list this cycle.")
            all_positions = []
        except Exception:
            logger.warning("Auto-trader position snapshot failed; using empty position list this cycle.", exc_info=True)
            all_positions = []
        self._hydrate_open_position_locks(all_positions)
        scan_window_id = f"auto:{int(self._last_scan)}"
        self.signal_pipeline.anti_churn_service.begin_scan_window(scan_window_id)

        # Skip stale/non-executable symbols before expensive model evaluation.
        executable_symbols: list[str] = []
        precheck_now = time.time()
        for sym in symbols:
            try:
                tick = self.market_data.get_tick(sym)
            except Exception:
                tick = None
            if not tick:
                self._log_auto_trade(
                    sym,
                    TradeSignal(action="HOLD", confidence=0.0, reason="No live quote"),
                    "Skipped before evaluation: no live quote for this symbol.",
                    False,
                    0.0,
                )
                continue
            quote_ok, quote_reason = _fresh_live_tick_ok(tick, now=precheck_now)
            if not quote_ok:
                self._log_auto_trade(
                    sym,
                    TradeSignal(action="HOLD", confidence=0.0, reason="Invalid quote"),
                    f"Skipped before evaluation: fresh live quote is unavailable ({quote_reason}).",
                    False,
                    0.0,
                )
                continue
            try:
                symbol_info = self.market_data.get_symbol_info(sym)
            except Exception:
                symbol_info = None
            meta_ok, meta_reason = _usable_trade_metadata(symbol_info)
            if not meta_ok:
                self._log_auto_trade(
                    sym,
                    TradeSignal(action="HOLD", confidence=0.0, reason="Non-executable symbol"),
                    f"Skipped before evaluation: symbol is non-executable ({meta_reason}).",
                    False,
                    0.0,
                )
                continue
            executable_symbols.append(sym)
        symbols = executable_symbols

        logger.info(f"Auto-trader scanning {len(symbols)} symbols")
        if not symbols:
            self._trade_log.append({
                "timestamp": time.time(),
                "symbol": "SYSTEM",
                "action": "HOLD",
                "confidence": 0.0,
                "quality_score": 0.0,
                "detail": "No eligible auto-trade symbols this cycle. The default strategy now rotates indices, stocks, and commodities, but symbols without a live executable broker session can still be skipped.",
                "success": False,
            })
            self._trade_log = self._trade_log[-50:]
            return

        for sym in symbols:
            try:
                # Stop immediately if the user turned auto-trade off mid-scan.
                if not self._execution_allowed():
                    break
                now_ts = time.time()
                canonical_sym = self.signal_pipeline.universe_service.canonical_symbol(sym)
                if allowed_canonical_scope and canonical_sym not in allowed_canonical_scope:
                    self._log_auto_trade(
                        sym,
                        TradeSignal(action="HOLD", confidence=0.0, reason="Out-of-scope symbol"),
                        f"Skipped out-of-scope symbol {sym}; active strategy scope is {sorted(allowed_canonical_scope)}.",
                        False,
                        0.0,
                    )
                    continue
                market_session_guard = self.signal_pipeline.market_session_guard_for_symbol(sym, tradeable_market)
                if market_session_guard:
                    self._log_auto_trade(
                        sym,
                        TradeSignal(action="HOLD", confidence=0.0, reason="Market closed or not executable"),
                        market_session_guard,
                        False,
                        0.0,
                    )
                    continue
                max_positions = settings.max_open_positions_total or settings.max_concurrent_positions
                if len(all_positions) >= max_positions:
                    logger.info("Auto-trader: max positions reached, stopping scan")
                    break

                if self._cadence_buy_every_seconds > 0:
                    last_entry_ts = float(self._last_cadence_entry_ts.get(canonical_sym, 0.0) or 0.0)
                    if last_entry_ts > 0 and (now_ts - last_entry_ts) < self._cadence_buy_every_seconds:
                        wait_seconds = int(self._cadence_buy_every_seconds - (now_ts - last_entry_ts))
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=0.0, reason="Cadence wait"),
                            f"Cadence wait active: next buy check in ~{max(wait_seconds, 1)}s.",
                            False,
                            0.0,
                        )
                        continue

                timeout_cooldown_until = float(self._symbol_timeout_cooldown_until.get(canonical_sym, 0.0) or 0.0)
                if timeout_cooldown_until > now_ts:
                    continue

                auto_eval_timeout_seconds = _auto_eval_timeout_seconds(sym)
                timeout_streak = int(self._symbol_timeout_streak.get(canonical_sym, 0) or 0)
                eval_mode = "auto_fast" if timeout_streak >= 2 else "auto"
                eval_timeout = min(auto_eval_timeout_seconds, 10.0) if eval_mode == "auto_fast" else auto_eval_timeout_seconds
                decision = None
                try:
                    decision = await asyncio.wait_for(
                        self.signal_pipeline.evaluate(
                            symbol=sym,
                            requested_agent_name=self._auto_agent_name,
                            requested_timeframe="H1",
                            evaluation_mode=eval_mode,
                            bar_count=100,
                            scan_window_id=scan_window_id,
                        ),
                        timeout=eval_timeout,
                    )
                    self._symbol_timeout_streak[canonical_sym] = 0
                    self._symbol_timeout_cooldown_until.pop(canonical_sym, None)
                except TimeoutError:
                    next_timeout_streak = min(8, timeout_streak + 1)
                    self._symbol_timeout_streak[canonical_sym] = next_timeout_streak
                    timeout_backoff = self._symbol_timeout_cooldown_seconds(next_timeout_streak)
                    if timeout_backoff > 0:
                        self._symbol_timeout_cooldown_until[canonical_sym] = time.time() + timeout_backoff
                    # Fast technical fallback to avoid repeated timeout-only HOLD rows
                    # on symbols that frequently hit LLM/news latency spikes.
                    if "SmartAgent" in self.agents:
                        fast_timeout = max(4.0, min(8.0, auto_eval_timeout_seconds * 0.45))
                        try:
                            decision = await asyncio.wait_for(
                                self.signal_pipeline.evaluate(
                                    symbol=sym,
                                    requested_agent_name="SmartAgent",
                                    requested_timeframe="H1",
                                    evaluation_mode="auto_fast",
                                    bar_count=100,
                                    scan_window_id=scan_window_id,
                                ),
                                timeout=fast_timeout,
                            )
                            self._symbol_timeout_streak[canonical_sym] = 0
                            self._symbol_timeout_cooldown_until.pop(canonical_sym, None)
                            logger.info(
                                "Auto-trader: primary timeout for %s after %.1fs (streak=%s); used SmartAgent fast fallback (%.1fs)",
                                sym,
                                eval_timeout,
                                int(self._symbol_timeout_streak.get(canonical_sym, 0) or 0),
                                fast_timeout,
                            )
                        except Exception:
                            decision = None
                    if decision is not None:
                        # Continue normal flow with fallback decision.
                        pass
                    else:
                        cached_confidence = self._cached_symbol_confidence(sym)
                        conservative_floor = max(
                            0.35,
                            min(0.9, float(self.risk_engine.settings.min_confidence_threshold or 0.45)),
                        )
                        timeout_confidence = cached_confidence if cached_confidence > 0 else conservative_floor
                        strict_timeout_force = False
                        logger.info(
                            "Auto-trader timeout fallback check: symbol=%s force=%s fixed_lot=%.2f cadence_buy=%s scope=%s",
                            canonical_sym,
                            strict_timeout_force,
                            float(self._fixed_lot_size or 0.0),
                            int(self._cadence_buy_every_seconds or 0),
                            sorted(allowed_canonical_scope),
                        )
                        if strict_timeout_force:
                            same_symbol_positions = [
                                p for p in all_positions
                                if str(getattr(p, "symbol", "") or "").upper() == canonical_sym
                            ]
                            if same_symbol_positions:
                                force_seconds = self._normalized_force_exit_seconds(self._cadence_sell_after_seconds)
                                if self.db and force_seconds > 0:
                                    for open_pos in same_symbol_positions:
                                        try:
                                            existing_plan = await self.db.get_position_management_plan(open_pos.ticket)
                                            plan_json = dict((existing_plan or {}).get("plan_json") or {})
                                            meta = dict(plan_json.get("metadata") or {})
                                            existing_force = self._normalized_force_exit_seconds(
                                                plan_json.get("force_time_exit_seconds")
                                                or meta.get("force_time_exit_seconds")
                                                or 0
                                            )
                                            if existing_force <= 0:
                                                await self.db.save_position_management_plan(
                                                    ticket=open_pos.ticket,
                                                    signal_id=(existing_plan or {}).get("signal_id"),
                                                    symbol=open_pos.symbol,
                                                    action=open_pos.type,
                                                    plan={
                                                        **plan_json,
                                                        "strategy": plan_json.get("strategy") or "strict_cadence",
                                                        "force_time_exit_seconds": force_seconds,
                                                        "planned_hold_minutes": max(1, math.ceil(force_seconds / 60.0)),
                                                        "metadata": {
                                                            **meta,
                                                            "strategy_family": meta.get("strategy_family") or "strict_cadence",
                                                            "symbol_category": meta.get("symbol_category") or get_asset_class(sym),
                                                            "force_time_exit_seconds": force_seconds,
                                                        },
                                                    },
                                                )
                                        except Exception:
                                            logger.debug(
                                                "Strict cadence plan repair skipped for ticket %s",
                                                getattr(open_pos, "ticket", 0),
                                                exc_info=True,
                                            )
                                self._log_auto_trade(
                                    sym,
                                    TradeSignal(action="HOLD", confidence=0.0, reason="Cadence position already open"),
                                    f"Already have an open position on {canonical_sym}; strict cadence fallback skipped.",
                                    False,
                                    0.0,
                                )
                                continue
                            executed = await self._execute_strict_cadence_fallback(
                                symbol=sym,
                                canonical_symbol=canonical_sym,
                                action=self._cadence_entry_action,
                                quality_score=1.0,
                            )
                            if executed:
                                all_positions = self.execution.get_positions()
                                continue
                        timeout_signal = TradeSignal(
                            action="HOLD",
                            confidence=timeout_confidence,
                            reason=(
                                f"Scan timeout after {auto_eval_timeout_seconds:.0f}s"
                                + (
                                    f" (reused last confidence {cached_confidence:.0%})."
                                    if cached_confidence > 0
                                    else f" (using conservative baseline {timeout_confidence:.0%})."
                                )
                            ),
                        )
                        self._log_auto_trade(
                            sym,
                            timeout_signal,
                            timeout_signal.reason,
                            False,
                            float(timeout_confidence),
                        )
                        logger.warning(
                            "Auto-trader: evaluate timeout for %s after %.1fs (streak=%s)",
                            sym,
                            eval_timeout,
                            int(self._symbol_timeout_streak.get(canonical_sym, 0) or 0),
                        )
                        continue
                signal_id = None
                try:
                    signal_id = await self.signal_pipeline.persist_evaluation(decision, "multi")
                except Exception as persist_exc:
                    # Do not block live execution on telemetry persistence contention.
                    logger.warning(
                        "Auto-trader persistence skipped for %s: %s",
                        sym,
                        persist_exc,
                    )

                signal = decision.signal_decision.final_signal.to_trade_signal()
                context = decision.signal_decision.market_context
                risk_evaluation = decision.risk_evaluation
                quality = decision.trade_quality_assessment

                strategy_force_entry = bool(
                    settings.force_execute_any_conditions
                    and self._cadence_entry_action in {"BUY", "SELL"}
                    and (not allowed_canonical_scope or canonical_sym in allowed_canonical_scope)
                )
                strict_cadence_force = bool(strategy_force_entry and self._cadence_buy_every_seconds > 0)
                if signal.action == "HOLD" and strategy_force_entry:
                    signal = TradeSignal(
                        action=str(self._cadence_entry_action or "BUY").upper(),
                        confidence=max(float(signal.confidence or 0.0), 0.99),
                        reason=(
                            f"Force mode is enabled, so the strategy is overriding HOLD and forcing "
                            f"{str(self._cadence_entry_action or 'BUY').upper()} on {canonical_sym}."
                        ),
                        strategy="strict_strategy_override" if not strict_cadence_force else "strict_cadence",
                        metadata={
                            "strict_cadence_forced": bool(strict_cadence_force),
                            "force_strategy_override": True,
                        },
                    )

                if signal.action == "HOLD":
                    hold_detail = signal.reason or decision.reason
                    self._log_auto_trade(
                        sym,
                        signal,
                        hold_detail,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                if not decision.allow_execute and not strict_cadence_force:
                    logger.info("Auto-trader: %s %s rejected: %s", sym, signal.action, decision.reason)
                    self._log_auto_trade(
                        sym,
                        signal,
                        decision.reason,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                if not strict_cadence_force:
                    min_auto_confidence, min_auto_quality = _auto_execution_thresholds(
                        self.risk_engine.settings,
                        quality,
                    )
                    gemini_confirmation = getattr(decision.signal_decision, "gemini_confirmation", None)
                    gemini_usable = bool(
                        gemini_confirmation is not None
                        and getattr(gemini_confirmation, "used", False)
                        and not getattr(gemini_confirmation, "degraded", False)
                    )
                    if float(signal.confidence or 0.0) < min_auto_confidence:
                        block_reason = (
                            f"Blocked: auto execution requires confidence >= {min_auto_confidence:.0%}; "
                            f"got {float(signal.confidence or 0.0):.0%}."
                        )
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=block_reason),
                            block_reason,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    if float(quality.final_trade_quality_score or 0.0) < min_auto_quality:
                        block_reason = (
                            f"Blocked: auto execution requires trade quality >= {min_auto_quality:.0%}; "
                            f"got {float(quality.final_trade_quality_score or 0.0):.0%}."
                        )
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=block_reason),
                            block_reason,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    gemini_role = str(
                        getattr(self.risk_engine.user_policy, "gemini_role", "")
                        or getattr(self.risk_engine.settings, "gemini_role", "")
                        or "advisory"
                    ).lower()
                    if gemini_role == "confirmation-required" and not gemini_usable:
                        block_reason = "Blocked: auto execution requires a non-degraded Gemini confirmation."
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=block_reason),
                            block_reason,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue

                    empirical_block_reason = self._empirical_execution_quality_gate(decision)
                    if empirical_block_reason:
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=empirical_block_reason),
                            empirical_block_reason,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue

                    perf_allowed, perf_block_reason = await self._recent_performance_gate(
                        symbol=sym,
                        confidence=float(signal.confidence or 0.0),
                        quality_score=float(quality.final_trade_quality_score or 0.0),
                    )
                    if not perf_allowed:
                        self._log_auto_trade(
                            sym,
                            TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=perf_block_reason),
                            perf_block_reason,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue

                loss_guard_block = await self._symbol_loss_guard(
                    symbol=sym,
                    action=str(signal.action or "").upper(),
                    open_positions=all_positions,
                )
                if loss_guard_block:
                    self._log_auto_trade(
                        sym,
                        TradeSignal(action="HOLD", confidence=float(signal.confidence or 0.0), reason=loss_guard_block),
                        loss_guard_block,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                tick = self.market_data.get_tick(sym)
                if not tick:
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Live tick unavailable at execution time",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                # Final live duplicate check right before sizing/execution.
                # This closes the gap between scan-time state and actual order send,
                # so a second auto cycle cannot stack another same-side position
                # if one was opened moments earlier.
                try:
                    final_live_positions = list(self.execution.get_positions(symbol=sym) or [])
                except Exception:
                    final_live_positions = []
                final_same_side_open = False
                for pos in final_live_positions:
                    try:
                        pos_symbol = self.signal_pipeline.universe_service.canonical_symbol(
                            str(getattr(pos, "symbol", "") or "").upper()
                        )
                        pos_action = self._position_action(pos)
                    except Exception:
                        continue
                    if pos_symbol == canonical_sym and pos_action == str(signal.action or "").upper():
                        final_same_side_open = True
                        break
                if final_same_side_open:
                    block_reason = (
                        f"Blocked at execution: {canonical_sym} already has an open "
                        f"{str(signal.action or '').upper()} position."
                    )
                    self._remember_symbol_direction_opened(canonical_sym, str(signal.action or "").upper())
                    self._log_auto_trade(
                        sym,
                        TradeSignal(
                            action="HOLD",
                            confidence=float(signal.confidence or 0.0),
                            reason=block_reason,
                        ),
                        block_reason,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                symbol_point = context.symbol_info.point if context.symbol_info and context.symbol_info.point else 0.0
                spread_limit_points = (
                    (context.profile.max_spread / symbol_point)
                    if context.profile and symbol_point > 0
                    else settings.max_spread_threshold
                )
                if self.signal_pipeline.anti_churn_service.spread_deteriorated(
                    decision.signal_decision.market_context.tick.get("spread", 0.0) if decision.signal_decision.market_context.tick else 0.0,
                    tick.spread,
                    spread_limit_points,
                ):
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Spread deteriorated before execution",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue

                entry_price = tick.ask if signal.action == "BUY" else tick.bid
                symbol_info = context.symbol_info
                volume = float(risk_evaluation.adjusted_volume)
                budget_cap_usd = self._auto_trade_budget_cap_usd(account)
                remaining_budget_usd = self._remaining_custom_budget_usd(account, budget_cap_usd)
                if budget_cap_usd is not None and (remaining_budget_usd is None or remaining_budget_usd <= 0.0):
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Custom budget fully used by current open margin. No new entries until margin is released.",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                max_single_trade_budget_usd = self._max_single_trade_budget_usd(budget_cap_usd)
                effective_trade_cap_usd = max_single_trade_budget_usd
                if remaining_budget_usd is not None:
                    effective_trade_cap_usd = (
                        min(float(effective_trade_cap_usd), float(remaining_budget_usd))
                        if effective_trade_cap_usd is not None
                        else float(remaining_budget_usd)
                    )
                budget_ratio = self._auto_trade_budget_ratio(account)
                def _symbol_field(name: str, default: float | int):
                    if isinstance(symbol_info, dict):
                        value = symbol_info.get(name, default)
                    else:
                        value = getattr(symbol_info, name, default)
                    return default if value in (None, "") else value

                vol_min = float(_symbol_field("volume_min", 0.01) or 0.01)
                vol_step = float(_symbol_field("volume_step", 0.01) or 0.01)
                vol_max = float(_symbol_field("volume_max", 0.0) or 0.0)
                contract_size = float(_symbol_field("trade_contract_size", 100000.0) or 100000.0)
                if budget_ratio < 0.999999:
                    budgeted_volume = self._round_volume_down(volume * budget_ratio, vol_min, vol_step, vol_max)
                    if budgeted_volume <= 0:
                        self._log_auto_trade(
                            sym,
                            signal,
                            "Custom auto-trade budget is too small for the broker minimum trade size.",
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    volume = budgeted_volume
                estimated_margin = self.execution.estimate_margin(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    price=entry_price,
                )
                investment = max(1.0, float(estimated_margin if estimated_margin is not None else 0.0))
                normalized_sl, normalized_tp, sl_amount_usd, tp_amount_usd = await self._dynamic_amount_based_targets(
                    decision=decision,
                    action=signal.action,
                    entry_price=entry_price,
                    volume=volume,
                    symbol_info=symbol_info,
                    amount_usd=investment,
                    digits=symbol_info.digits if symbol_info else 5,
                )
                normalized_sl, normalized_tp = self._normalize_live_stops(
                    symbol=sym,
                    action=signal.action,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                )
                normalized_sl, normalized_tp = self._cap_symbol_target_distances(
                    action=signal.action,
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    digits=int(_symbol_field("digits", 5) or 5),
                )
                min_rr = max(1.0, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.6) or 1.6))
                sl_distance = abs(entry_price - normalized_sl)
                tp_distance = abs(normalized_tp - entry_price)
                if sl_distance > 0 and (tp_distance / sl_distance) < min_rr:
                    target_tp_distance = sl_distance * min_rr
                    if signal.action == "BUY":
                        normalized_tp = round(entry_price + target_tp_distance, int(_symbol_field("digits", 5) or 5))
                    else:
                        normalized_tp = round(entry_price - target_tp_distance, int(_symbol_field("digits", 5) or 5))
                # Keep comment SLA/TPA aligned to the final normalized targets.
                sl_amount_usd, tp_amount_usd = self._compute_target_amounts_usd(
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    volume=volume,
                )
                if self._fixed_lot_size > 0:
                    fixed_volume = self._round_volume_down(self._fixed_lot_size, vol_min, vol_step, vol_max)
                    if fixed_volume <= 0:
                        self._log_auto_trade(
                            sym,
                            signal,
                            f"Configured fixed lot {self._fixed_lot_size:.2f} is invalid for broker min/step.",
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    volume = fixed_volume
                elif self._allocation_pct_free_margin > 0 and account:
                    target_margin = float(account.free_margin) * (float(self._allocation_pct_free_margin) / 100.0)
                    if effective_trade_cap_usd is not None:
                        target_margin = min(target_margin, effective_trade_cap_usd)
                    target_volume, target_margin_estimate = self._solve_volume_by_margin_target(
                        symbol=sym,
                        action=signal.action,
                        target_margin_usd=target_margin,
                        price=entry_price,
                        contract_size=contract_size,
                        vol_min=vol_min,
                        vol_step=vol_step,
                        vol_max=vol_max,
                    )
                    if target_volume <= 0:
                        self._log_auto_trade(
                            sym,
                            signal,
                            f"Configured margin allocation {self._allocation_pct_free_margin:.2f}% is too small for broker minimum size.",
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    volume = target_volume
                    investment = max(1.0, float(target_margin_estimate if target_margin_estimate is not None else target_margin))
                else:
                    rebalanced_volume, rebalance_error = self._rebalance_volume_for_started_amount_risk(
                        entry_price=entry_price,
                        stop_loss=normalized_sl,
                        desired_sl_amount_usd=sl_amount_usd,
                        volume=volume,
                        symbol_info=symbol_info,
                        vol_min=vol_min,
                        vol_step=vol_step,
                        vol_max=vol_max,
                    )
                    if rebalance_error:
                        self._log_auto_trade(
                            sym,
                            signal,
                            rebalance_error,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    if rebalanced_volume > 0 and rebalanced_volume < volume:
                        volume = rebalanced_volume

                if budget_cap_usd is not None:
                    target_budget = float(effective_trade_cap_usd or 0.0)
                    budgeted_volume, budgeted_margin = self._solve_volume_by_margin_target(
                        symbol=sym,
                        action=signal.action,
                        target_margin_usd=target_budget,
                        price=entry_price,
                        contract_size=contract_size,
                        vol_min=vol_min,
                        vol_step=vol_step,
                        vol_max=vol_max,
                    )
                    if budgeted_volume <= 0:
                        self._log_auto_trade(
                            sym,
                            signal,
                            "Custom auto-trade budget is too small for the broker minimum trade size.",
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    current_margin_estimate = self.execution.estimate_margin(
                        symbol=sym,
                        action=signal.action,
                        volume=volume,
                        price=entry_price,
                    )
                    if (
                        current_margin_estimate is not None
                        and current_margin_estimate > (target_budget * 1.02)
                        and budgeted_volume < volume
                    ):
                        volume = budgeted_volume
                        investment = max(
                            1.0,
                            float(budgeted_margin if budgeted_margin is not None else target_budget),
                        )

                logger.info(
                    "AUTO-TRADE: %s %s confidence=%.2f quality=%.2f volume=%.4f strict=%s",
                    signal.action,
                    sym,
                    signal.confidence,
                    quality.final_trade_quality_score,
                    volume,
                    strict_cadence_force,
                )
                estimated_margin = self.execution.estimate_margin(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    price=entry_price,
                )
                if effective_trade_cap_usd is not None and estimated_margin is not None:
                    if float(estimated_margin) > (float(effective_trade_cap_usd) * 1.02):
                        capped_volume, capped_margin = self._solve_volume_by_margin_target(
                            symbol=sym,
                            action=signal.action,
                            target_margin_usd=float(effective_trade_cap_usd),
                            price=entry_price,
                            contract_size=contract_size,
                            vol_min=vol_min,
                            vol_step=vol_step,
                            vol_max=vol_max,
                        )
                        if capped_volume > 0 and capped_volume < volume:
                            volume = capped_volume
                            estimated_margin = capped_margin
                        elif capped_volume <= 0:
                            self._log_auto_trade(
                                sym,
                                signal,
                                "Custom budget cap is below broker minimum size for this symbol right now.",
                                False,
                                quality.final_trade_quality_score,
                                signal_id=signal_id,
                                decision=decision,
                            )
                            continue
                investment = max(1.0, float(estimated_margin if estimated_margin is not None else investment))
                # Volume may have changed after initial target calculation (budget/fixed lot).
                # Recompute SLA/TPA with final volume so money guards and UI remain accurate.
                sl_amount_usd, tp_amount_usd = self._compute_target_amounts_usd(
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    volume=volume,
                )
                account_equity = float(getattr(account, "equity", 0.0) or 0.0) if account else 0.0
                max_risk_pct = float(getattr(self.risk_engine.user_policy, "max_risk_per_trade", 0.0) or 0.0)
                max_risk_usd = account_equity * (max_risk_pct / 100.0) if account_equity > 0 and max_risk_pct > 0 else 0.0
                if max_risk_usd > 0 and sl_amount_usd > (max_risk_usd * 1.02):
                    risk_capped_volume, rebalance_error = self._rebalance_volume_for_started_amount_risk(
                        entry_price=entry_price,
                        stop_loss=normalized_sl,
                        desired_sl_amount_usd=max_risk_usd,
                        volume=volume,
                        symbol_info=symbol_info,
                        vol_min=vol_min,
                        vol_step=vol_step,
                        vol_max=vol_max,
                    )
                    if rebalance_error:
                        self._log_auto_trade(
                            sym,
                            signal,
                            rebalance_error,
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    if risk_capped_volume <= 0:
                        self._log_auto_trade(
                            sym,
                            signal,
                            (
                                f"Skipped: actual broker SL risk ${sl_amount_usd:.2f} exceeds "
                                f"max allowed ${max_risk_usd:.2f}, and broker minimum size is too large."
                            ),
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                    if risk_capped_volume < volume:
                        volume = risk_capped_volume
                        estimated_margin = self.execution.estimate_margin(
                            symbol=sym,
                            action=signal.action,
                            volume=volume,
                            price=entry_price,
                        )
                        investment = max(1.0, float(estimated_margin if estimated_margin is not None else investment))
                        sl_amount_usd, tp_amount_usd = self._compute_target_amounts_usd(
                            entry_price=entry_price,
                            stop_loss=normalized_sl,
                            take_profit=normalized_tp,
                            symbol_info=symbol_info,
                            volume=volume,
                        )
                    if sl_amount_usd > (max_risk_usd * 1.02):
                        self._log_auto_trade(
                            sym,
                            signal,
                            (
                                f"Skipped: actual broker SL risk ${sl_amount_usd:.2f} exceeds "
                                f"max allowed ${max_risk_usd:.2f}."
                            ),
                            False,
                            quality.final_trade_quality_score,
                            signal_id=signal_id,
                            decision=decision,
                        )
                        continue
                # Keep broker comment compact and parseable.
                normalized_sl, normalized_tp, sl_amount_usd, tp_amount_usd = self._cap_targets_by_money_budget(
                    action=signal.action,
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    volume=volume,
                    started_amount_usd=investment,
                    account=account,
                    digits=int(_symbol_field("digits", 5) or 5),
                )
                normalized_sl, normalized_tp = self._normalize_live_stops(
                    symbol=sym,
                    action=signal.action,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                )
                sl_amount_usd, tp_amount_usd = self._compute_target_amounts_usd(
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    volume=volume,
                )
                normalized_sl, normalized_tp, sl_amount_usd, tp_amount_usd = self._cap_targets_by_money_budget(
                    action=signal.action,
                    entry_price=entry_price,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    symbol_info=symbol_info,
                    volume=volume,
                    started_amount_usd=investment,
                    account=account,
                    digits=int(_symbol_field("digits", 5) or 5),
                )
                comment = self._build_trade_comment(
                    investment,
                    sl_amount_usd=sl_amount_usd,
                    tp_amount_usd=tp_amount_usd,
                )

                order_req = OrderRequest(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    comment=comment,
                    idempotency_key=f"auto:{signal_id}" if signal_id else "",
                    account_key=f"mt5:{'demo' if self.connector.is_demo() else 'live'}:{getattr(account, 'login', '')}",
                    execution_context="auto",
                    execution_mode=config.AUTO_TRADER_EXECUTION_MODE,
                )
                preflight = await self.execution_service.preflight(
                    symbol=sym,
                    action=signal.action,
                    volume=volume,
                    stop_loss=normalized_sl,
                    take_profit=normalized_tp,
                    reference_spread=decision.signal_decision.market_context.tick.get("spread", 0.0)
                    if decision.signal_decision.market_context.tick
                    else 0.0,
                    requested_agent_name=self._auto_agent_name,
                    requested_timeframe="H1",
                    evaluation_mode="auto",
                    scan_window_id=scan_window_id,
                )
                if not self._execution_allowed():
                    self._log_auto_trade(
                        sym,
                        signal,
                        "Auto-trading was turned off before execution.",
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                if not preflight.approved:
                    self._log_auto_trade(
                        sym,
                        signal,
                        preflight.reason,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                kill_switch_block = self._kill_switch_block_reason()
                if kill_switch_block:
                    self.risk_engine.auto_trade_enabled = False
                    self.risk_engine.auto_trade_session_armed = False
                    self._log_auto_trade(
                        sym,
                        signal,
                        kill_switch_block,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                live_policy_block = self._live_trading_policy_block_reason()
                if live_policy_block:
                    self._log_auto_trade(
                        sym,
                        signal,
                        live_policy_block,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    continue
                reserved_opened_at = time.time()
                self._remember_symbol_direction_opened(canonical_sym, signal.action, reserved_opened_at)
                result = self.execution_service.place_order_if_approved(order_req, preflight)

                if self.db and signal_id:
                    await self.db.log_order(
                        signal_id=signal_id,
                        symbol=sym,
                        action=signal.action,
                        volume=order_req.volume,
                        price=result.price,
                        stop_loss=normalized_sl,
                        take_profit=normalized_tp,
                        ticket=result.ticket,
                        retcode=result.retcode,
                        retcode_desc=result.retcode_desc,
                        success=result.success,
                        comment=order_req.comment,
                        idempotency_key=order_req.idempotency_key,
                        account_key=order_req.account_key,
                        execution_context=order_req.execution_context,
                        execution_mode=order_req.execution_mode,
                    )
                    if result.success and result.ticket:
                        await self.db.log_position_change(
                            result.ticket,
                            "auto_opened",
                            sym,
                            f"AUTO {signal.action} {order_req.volume} lots at {result.price} (conf={signal.confidence:.0%})",
                        )
                        await self.db.save_position_management_plan(
                            ticket=result.ticket,
                            signal_id=signal_id,
                            symbol=sym,
                            action=signal.action,
                            plan=self._with_strategy_time_rules(decision.position_management_plan.model_dump()),
                        )
                        await self.db.mark_evaluation_outcome(signal_id, "opened")
                        account = self.connector.refresh_account() if self.connector.connected else None
                        await self.db.mark_trade_candidate_execution(
                            signal_id=signal_id,
                            executed=True,
                            ticket=result.ticket,
                            fill_price=result.price,
                            slippage_estimate=0.0,
                            margin_snapshot={
                                "balance": float(account.balance) if account else 0.0,
                                "equity": float(account.equity) if account else 0.0,
                                "margin": float(account.margin) if account else 0.0,
                                "free_margin": float(account.free_margin) if account else 0.0,
                            },
                        )

                if result.success:
                    self.position_manager.enable_auto_portfolio_targets()
                    logger.info("AUTO-TRADE SUCCESS: %s ticket=%s price=%s", sym, result.ticket, result.price)
                    executed_any_trade = True
                    self._last_cadence_entry_ts[canonical_sym] = reserved_opened_at
                    self._log_auto_trade(
                        sym,
                        signal,
                        f"Order #{result.ticket} filled at {result.price}",
                        True,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    self.signal_pipeline.anti_churn_service.mark_symbol_opened(scan_window_id, sym)
                    all_positions = self.execution.get_positions()
                else:
                    same_side_still_open = False
                    try:
                        failed_live_positions = list(self.execution.get_positions(symbol=sym) or [])
                    except Exception:
                        failed_live_positions = []
                    for pos in failed_live_positions:
                        try:
                            pos_symbol = self.signal_pipeline.universe_service.canonical_symbol(
                                str(getattr(pos, "symbol", "") or "").upper()
                            )
                            pos_action = self._position_action(pos)
                        except Exception:
                            continue
                        if pos_symbol == canonical_sym and pos_action == str(signal.action or "").upper():
                            same_side_still_open = True
                            break
                    if not same_side_still_open:
                        self._symbol_direction_last_opened_at.pop(
                            self._symbol_direction_cooldown_key(canonical_sym, signal.action),
                            None,
                        )
                    broker_auto_rejected = int(getattr(result, "retcode", 0) or 0) in {10026, 10027}
                    if broker_auto_rejected:
                        self.risk_engine.auto_trade_enabled = False
                        self.risk_engine.auto_trade_session_armed = False
                        self._running = False
                        self._state.mark_stopped()
                        logger.warning(
                            "Auto-trade disarmed after broker rejected API trading: %s",
                            result.retcode_desc,
                        )
                    logger.warning("AUTO-TRADE FAILED: %s %s", sym, result.retcode_desc)
                    self._log_auto_trade(
                        sym,
                        signal,
                        result.retcode_desc,
                        False,
                        quality.final_trade_quality_score,
                        signal_id=signal_id,
                        decision=decision,
                    )
                    if broker_auto_rejected:
                        return

            except Exception as e:
                logger.error(f"Auto-trader error for {sym}: {e}", exc_info=True)
                self._log_auto_trade(
                    sym,
                    TradeSignal(action="HOLD", confidence=0.0, reason="Auto-trader symbol error"),
                    f"Symbol scan failed: {e}",
                    False,
                    0.0,
                )

    def _symbol_direction_cooldown_key(self, symbol: str, action: str) -> str:
        canonical = self.signal_pipeline.universe_service.canonical_symbol(symbol)
        return f"{canonical}|{str(action or '').upper()}"

    def _remember_symbol_direction_opened(self, symbol: str, action: str, opened_at: float | None = None) -> None:
        key = self._symbol_direction_cooldown_key(symbol, action)
        self._symbol_direction_last_opened_at[key] = float(opened_at or time.time())

    def _recent_symbol_direction_open_seconds(self, symbol: str, action: str) -> float:
        key = self._symbol_direction_cooldown_key(symbol, action)
        opened_at = float(self._symbol_direction_last_opened_at.get(key, 0.0) or 0.0)
        if opened_at <= 0.0:
            return 0.0
        age = max(0.0, time.time() - opened_at)
        if age > 86400.0:
            self._symbol_direction_last_opened_at.pop(key, None)
            return 0.0
        return age

    def _hydrate_open_position_locks(self, open_positions: list | None = None) -> None:
        positions = list(open_positions or [])
        if not positions:
            try:
                positions = list(self.execution.get_positions() or [])
            except Exception:
                positions = []
        now_ts = time.time()
        for pos in positions:
            try:
                symbol = self.signal_pipeline.universe_service.canonical_symbol(
                    str(getattr(pos, "symbol", "") or "").upper()
                )
                action = self._position_action(pos)
            except Exception:
                continue
            if symbol and action in {"BUY", "SELL"}:
                key = self._symbol_direction_cooldown_key(symbol, action)
                self._symbol_direction_last_opened_at[key] = max(
                    float(self._symbol_direction_last_opened_at.get(key, 0.0) or 0.0),
                    now_ts,
                )

    def _position_action(self, pos) -> str:
        raw = getattr(pos, "type", None)
        # MT5 position type is typically numeric: BUY=0, SELL=1.
        try:
            raw_int = int(raw)
            if raw_int == 0:
                return "BUY"
            if raw_int == 1:
                return "SELL"
        except Exception:
            pass
        text = str(raw or "").strip().upper()
        if text in {"0", "BUY", "LONG"}:
            return "BUY"
        if text in {"1", "SELL", "SHORT"}:
            return "SELL"
        return text

    def _outcome_profit(self, row: dict) -> float:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            if row.get(key) is not None:
                try:
                    value = float(row.get(key) or 0.0)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    return value
        return 0.0

    def _outcome_closed_at(self, row: dict) -> float:
        for key in ("closed_at", "timestamp"):
            if row.get(key) is not None:
                try:
                    value = float(row.get(key) or 0.0)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    return value
        return 0.0

    def _runtime_realized_pnl_since(self, outcomes: list[dict], *, since_ts: float) -> float:
        realized = 0.0
        for row in outcomes or []:
            if not isinstance(row.get("signal_id"), int):
                continue
            closed_at = self._outcome_closed_at(row)
            if closed_at < since_ts:
                continue
            realized += self._outcome_profit(row)
        return realized

    def _is_position_near_entry_recovery(self, pos) -> bool:
        try:
            entry = float(getattr(pos, "price_open", 0.0) or 0.0)
            current = float(
                getattr(pos, "price_current", 0.0)
                or getattr(pos, "price", 0.0)
                or 0.0
            )
        except Exception:
            return False
        if entry <= 0 or current <= 0:
            return False
        distance_pct = abs(current - entry) / entry
        return distance_pct <= AUTO_NEAR_ENTRY_RECOVERY_PCT

    async def _symbol_loss_guard(self, *, symbol: str, action: str, open_positions: list) -> str | None:
        """Prevent repeated same-symbol losing entries and apply adaptive cooldowns."""
        if action not in {"BUY", "SELL"}:
            return None
        now_ts = time.time()
        canonical = self.signal_pipeline.universe_service.canonical_symbol(symbol)
        cooldown_key = self._symbol_direction_cooldown_key(canonical, action)
        cooldown_until = float(self._symbol_direction_cooldown_until.get(cooldown_key, 0.0) or 0.0)
        near_entry_recovery = False
        for pos in open_positions or []:
            try:
                pos_symbol = self.signal_pipeline.universe_service.canonical_symbol(
                    str(getattr(pos, "symbol", "") or "").upper()
                )
                pos_action = self._position_action(pos)
                pos_profit = float(getattr(pos, "profit", 0.0) or 0.0)
            except Exception:
                continue
            if pos_symbol == canonical and pos_action == action and pos_profit < 0:
                if self._is_position_near_entry_recovery(pos):
                    near_entry_recovery = True
                    break
        if cooldown_until > now_ts and not near_entry_recovery:
            wait_minutes = max(1, int((cooldown_until - now_ts) / 60.0))
            return (
                f"{canonical} is in loss-recovery cooldown for {action}. "
                f"Waiting ~{wait_minutes} min before new entries."
            )
        if cooldown_until > now_ts and near_entry_recovery:
            # Price recovered near entry; release this direction immediately.
            self._symbol_direction_cooldown_until.pop(cooldown_key, None)

        last_entry_ts = float(self._last_cadence_entry_ts.get(canonical, 0.0) or 0.0)
        if last_entry_ts > 0 and (now_ts - last_entry_ts) < AUTO_MIN_ENTRY_SPACING_SECONDS:
            wait_seconds = max(1, int(AUTO_MIN_ENTRY_SPACING_SECONDS - (now_ts - last_entry_ts)))
            return (
                f"{canonical} entry spacing guard active. "
                f"Next {action} check in ~{wait_seconds}s."
            )

        recent_open_age = self._recent_symbol_direction_open_seconds(canonical, action)
        if recent_open_age > 0.0 and recent_open_age < 600.0:
            wait_minutes = max(1, int((600.0 - recent_open_age) / 60.0))
            return (
                f"Blocked: auto-trader already opened {canonical} {action} recently "
                f"({int(recent_open_age // 60)} min ago). "
                f"Same-direction re-entry is locked for ~{wait_minutes} more min."
            )

        live_positions_by_symbol: list = []
        for candidate in (symbol, canonical):
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            try:
                fetched = list(self.execution.get_positions(symbol=candidate_text) or [])
            except Exception:
                fetched = []
            if fetched:
                live_positions_by_symbol = fetched
                break
        if live_positions_by_symbol:
            self._hydrate_open_position_locks(live_positions_by_symbol)
            merged_by_ticket: dict[int, object] = {}
            for pos in (open_positions or []):
                try:
                    merged_by_ticket[int(getattr(pos, "ticket", 0) or 0)] = pos
                except Exception:
                    continue
            for pos in live_positions_by_symbol:
                try:
                    merged_by_ticket[int(getattr(pos, "ticket", 0) or 0)] = pos
                except Exception:
                    continue
            open_positions = list(merged_by_ticket.values())

        same_symbol_positions = []
        symbol_unrealized = 0.0
        same_direction_count = 0
        same_direction_positions = []
        # Never add to a currently losing same-direction position on the same symbol.
        for pos in open_positions or []:
            try:
                pos_symbol = self.signal_pipeline.universe_service.canonical_symbol(
                    str(getattr(pos, "symbol", "") or "").upper()
                )
                pos_action = self._position_action(pos)
                pos_profit = float(getattr(pos, "profit", 0.0) or 0.0)
                pos_age_seconds = self.position_manager._position_age_seconds(pos)
            except Exception:
                continue
            if pos_symbol != canonical:
                continue
            same_symbol_positions.append(pos)
            symbol_unrealized += pos_profit
            if pos_action == action:
                same_direction_count += 1
                same_direction_positions.append((pos, pos_profit, pos_age_seconds))
            # If the same side is already losing for more than a short warmup,
            # don't let auto-trade stack another entry into the same idea.
            if (
                pos_action == action
                and pos_profit < 0.0
                and pos_age_seconds >= 180.0
                and not self._is_position_near_entry_recovery(pos)
            ):
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (45 * 60)
                return (
                    f"Blocked: existing {action} on {canonical} is still losing after "
                    f"{int(pos_age_seconds // 60)} min (${pos_profit:.2f}). "
                    "No same-direction re-entry until recovery or exit."
                )
            # Tighten same-direction averaging-down guard.
            if (
                pos_action == action
                and pos_profit <= -2.0
                and not self._is_position_near_entry_recovery(pos)
            ):
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (30 * 60)
                return (
                    f"Blocked: existing {action} on {canonical} is losing (${pos_profit:.2f}). "
                    "No averaging down; cooling this direction for 30 min (until near-entry recovery or exit)."
                )

        # Warmup add-on guard:
        # do not let auto-trade place a rapid second same-direction entry while the
        # first one is still fresh and unproven. Manual trading can still do this;
        # this guard only protects automated stacking behavior.
        if same_direction_positions:
            youngest_same_dir_age = min(age for _, _, age in same_direction_positions)
            net_same_dir_profit = sum(profit for _, profit, _ in same_direction_positions)
            best_same_dir_profit = max((profit for _, profit, _ in same_direction_positions), default=0.0)
            if youngest_same_dir_age < 600.0:
                wait_minutes = max(1, int((600.0 - youngest_same_dir_age) / 60.0))
                return (
                    f"Blocked: existing {action} on {canonical} is still in warmup "
                    f"({int(youngest_same_dir_age // 60)} min old, net ${net_same_dir_profit:.2f}, "
                    f"best open P/L ${best_same_dir_profit:.2f}). "
                    f"Auto-trade cannot add another same-direction position for ~{wait_minutes} min."
                )

        # If symbol is already stacked and underwater, block any new side to prevent
        # churn/over-exposure spirals in noisy sessions.
        if len(same_symbol_positions) >= 2 and symbol_unrealized <= -30.0:
            cooldown_minutes = 45 if symbol_unrealized > -200.0 else 90
            self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cooldown_minutes * 60)
            return (
                f"Blocked: {canonical} has {len(same_symbol_positions)} open positions with "
                f"net unrealized ${symbol_unrealized:.2f}. Cooling new {action} entries for {cooldown_minutes} min."
            )

        # Prevent rapid same-direction pyramiding even before losses become large.
        if same_direction_count >= 1:
            return (
                f"Blocked: already holding {same_direction_count} open {action} position"
                f"{'' if same_direction_count == 1 else 's'} on {canonical}. "
                "Auto-trader will not stack another same-direction entry until the current one closes or recovers."
            )

        if not self.db:
            return None
        try:
            outcomes = await self.db.get_recent_symbol_outcomes(
                canonical,
                limit=12,
                within_minutes=3 * 24 * 60,
            )
        except Exception:
            return None
        if not outcomes:
            return None

        # Keep loss guards focused on runtime-managed outcomes and ignore
        # backfilled broker-history rows that do not represent current strategy behavior.
        #
        # IMPORTANT:
        # Some legitimate runtime closes may not carry a signal_id (e.g. fast
        # management exits). We still need these outcomes to participate in
        # cooldown logic; otherwise repeated losing entries can slip through.
        filtered_outcomes: list[dict] = []
        for row in outcomes:
            try:
                exit_reason = str(row.get("exit_reason", "") or "").lower()
                if exit_reason in {"reconciled_external_close"}:
                    continue
                signal_id = row.get("signal_id")
                strategy = str(row.get("strategy", "") or "").strip().lower()
                # If there is no signal id and no strategy marker, keep only
                # clear runtime-managed exits; skip ambiguous manual closes.
                if not isinstance(signal_id, int):
                    runtime_exit_reasons = {
                        "time_stop",
                        "ten_minute_loss_cut",
                        "risk_budget_stop",
                        "stop_loss",
                        "take_profit",
                        "trailing_stop",
                        "breakeven_stop",
                        "trend_reversal",
                        "portfolio_target",
                    }
                    if exit_reason == "manual_close":
                        continue
                    if exit_reason not in runtime_exit_reasons and not strategy:
                        continue
                outcome_blob = row.get("outcome_json")
                if isinstance(outcome_blob, str) and outcome_blob.strip():
                    parsed = json.loads(outcome_blob)
                elif isinstance(outcome_blob, dict):
                    parsed = outcome_blob
                else:
                    parsed = {}
                detail_text = str((parsed or {}).get("detail") or "").lower()
                if "backfilled from mt5 history" in detail_text:
                    continue
            except Exception:
                pass
            filtered_outcomes.append(row)
        outcomes = filtered_outcomes
        if not outcomes:
            return None

        # Absolute post-stop-loss lock: one recent stop-based loss on this side
        # is enough to cool that side and prevent immediate re-entry churn.
        for row in outcomes:
            row_side = str(row.get("action", "") or "").upper()
            if row_side != action:
                continue
            reason = str(row.get("exit_reason", "") or "").lower()
            if reason not in {"stop_loss_amount", "risk_budget_stop", "stop_loss", "ten_minute_loss_cut"}:
                continue
            loss = self._outcome_profit(row)
            if loss >= 0:
                continue
            closed_at = self._outcome_closed_at(row)
            age_minutes = (now_ts - closed_at) / 60.0 if closed_at > 0 else 9999.0
            if age_minutes <= 120.0:
                cool_min = 120
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cool_min * 60)
                return (
                    f"Blocked: recent {canonical} {action} closed by {reason} at ${loss:.2f}. "
                    f"Cooling this direction for {cool_min} min."
                )

        # Hard post-loss brake:
        # if the most recent close for this same direction was a large
        # runtime loss, pause that direction for a longer window.
        same_dir_latest = next(
            (
                row for row in outcomes
                if str(row.get("action", "") or "").upper() == action
            ),
                    None,
                )

        if same_dir_latest is not None:
            latest_profit = self._outcome_profit(same_dir_latest)
            latest_reason = str(same_dir_latest.get("exit_reason", "") or "").lower()
            latest_closed = self._outcome_closed_at(same_dir_latest)
            age_minutes = (now_ts - latest_closed) / 60.0 if latest_closed > 0 else 9999.0
            severe_loss_reasons = {"risk_budget_stop", "stop_loss", "time_stop", "ten_minute_loss_cut"}
            if (
                latest_profit < -250.0
                and latest_reason in severe_loss_reasons
                and age_minutes <= 180.0
            ):
                cool_min = 120
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cool_min * 60)
                return (
                    f"Blocked: latest {action} on {canonical} closed as {latest_reason} "
                    f"with ${latest_profit:.2f}. Cooling this direction for {cool_min} min."
                )

        # Side-performance gate:
        # compare recent BUY vs SELL outcomes; if one side is clearly
        # underperforming, block that side until distribution improves.
        side_rows = outcomes[:8]
        side_profit = {"BUY": 0.0, "SELL": 0.0}
        side_count = {"BUY": 0, "SELL": 0}
        side_losses = {"BUY": 0, "SELL": 0}
        for row in side_rows:
            side = str(row.get("action", "") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            pnl = self._outcome_profit(row)
            side_profit[side] += pnl
            side_count[side] += 1
            if pnl < 0:
                side_losses[side] += 1
        opposite = "SELL" if action == "BUY" else "BUY"
        # Require enough samples on both sides to avoid noise.
        if side_count[action] >= 3 and side_count[opposite] >= 2:
            action_avg = side_profit[action] / max(1, side_count[action])
            opposite_avg = side_profit[opposite] / max(1, side_count[opposite])
            action_loss_rate = side_losses[action] / max(1, side_count[action])
            if (
                action_avg < -80.0
                and opposite_avg > 20.0
                and action_loss_rate >= 0.67
            ):
                cool_min = 90
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cool_min * 60)
                return (
                    f"Blocked: recent {canonical} {action} edge is negative "
                    f"(avg ${action_avg:.2f}, loss-rate {action_loss_rate:.0%}) while "
                    f"{opposite} performs better (avg ${opposite_avg:.2f}). "
                    f"Cooling {action} for {cool_min} min."
                )

        rolling_loss_window_seconds = 6 * 60 * 60
        same_dir_recent = []
        for row in outcomes:
            if str(row.get("action", "") or "").upper() != action:
                continue
            closed_at = self._outcome_closed_at(row)
            if closed_at > 0 and (now_ts - closed_at) > rolling_loss_window_seconds:
                continue
            same_dir_recent.append(row)
        if len(same_dir_recent) < 2:
            same_dir_recent = []

        if same_dir_recent:
            first_two = same_dir_recent[:2]
            if all(self._outcome_profit(row) < 0.0 for row in first_two):
                rolling_loss = abs(sum(self._outcome_profit(row) for row in first_two))
                recent_runtime_cuts = 0
                for row in first_two:
                    reason = str(row.get("exit_reason", "") or "").lower()
                    if reason in {"ten_minute_loss_cut", "time_stop", "stop_loss", "risk_budget_stop"}:
                        recent_runtime_cuts += 1
                # Bigger hit or repeated managed loss exits => longer cooloff.
                cooldown_minutes = 45 if rolling_loss < 300 else 120
                if recent_runtime_cuts >= 2 or rolling_loss >= 50.0:
                    cooldown_minutes = max(cooldown_minutes, 180)
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cooldown_minutes * 60)
                return (
                    f"Blocked: last 2 closed {action} trades on {canonical} were losses "
                    f"(~${rolling_loss:.2f} combined). Cooling this direction for {cooldown_minutes} min."
                )

        # Same-session direction brake:
        # if one side keeps failing in the current session/day, stop retrying it
        # for a longer period instead of feeding another similar entry.
        session_window_seconds = 12 * 60 * 60
        session_same_side = []
        for row in outcomes:
            try:
                row_action = str(row.get("action", "") or "").upper()
                closed_at = self._outcome_closed_at(row)
                pnl = self._outcome_profit(row)
            except Exception:
                continue
            if row_action != action or closed_at <= 0:
                continue
            if (now_ts - closed_at) <= session_window_seconds:
                session_same_side.append((pnl, str(row.get("exit_reason", "") or "").lower()))
        if len(session_same_side) >= 2:
            session_loss_count = sum(1 for pnl, _ in session_same_side if pnl < 0.0)
            session_total_pnl = sum(pnl for pnl, _ in session_same_side)
            managed_loss_count = sum(
                1
                for pnl, reason in session_same_side
                if pnl < 0.0 and reason in {"ten_minute_loss_cut", "time_stop", "stop_loss", "risk_budget_stop"}
            )
            if session_loss_count >= 2 and session_total_pnl <= -50.0:
                cooldown_minutes = 240 if managed_loss_count >= 2 else 180
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cooldown_minutes * 60)
                return (
                    f"Blocked: {canonical} {action} is underperforming this session "
                    f"({session_loss_count}/{len(session_same_side)} losses, net ${session_total_pnl:.2f}). "
                    f"Cooling this direction for {cooldown_minutes} min."
                )

        # Multi-day symbol-side brake:
        # if a side keeps failing across the last few days, pause that side long
        # enough to avoid recycling a clearly broken edge.
        multi_day_same_side = []
        multi_day_window_seconds = 3 * 24 * 60 * 60
        for row in outcomes:
            try:
                row_action = str(row.get("action", "") or "").upper()
                closed_at = self._outcome_closed_at(row)
                pnl = self._outcome_profit(row)
                exit_reason = str(row.get("exit_reason", "") or "").lower()
            except Exception:
                continue
            if row_action != action or closed_at <= 0:
                continue
            if (now_ts - closed_at) <= multi_day_window_seconds:
                multi_day_same_side.append((pnl, exit_reason))
        if len(multi_day_same_side) >= 6:
            multi_day_wins = sum(1 for pnl, _ in multi_day_same_side if pnl > 0.0)
            multi_day_win_rate = multi_day_wins / max(1, len(multi_day_same_side))
            multi_day_pnl = sum(pnl for pnl, _ in multi_day_same_side)
            multi_day_managed_losses = sum(
                1
                for pnl, reason in multi_day_same_side
                if pnl < 0.0 and reason in {"ten_minute_loss_cut", "time_stop", "stop_loss", "risk_budget_stop"}
            )
            if (
                multi_day_win_rate < 0.35
                and multi_day_pnl < 0.0
                and multi_day_managed_losses >= 3
            ):
                cooldown_minutes = 12 * 60
                self._symbol_direction_cooldown_until[cooldown_key] = now_ts + (cooldown_minutes * 60)
                return (
                    f"Blocked: {canonical} {action} is failing over the last 3 days "
                    f"({multi_day_wins}/{len(multi_day_same_side)} wins, net ${multi_day_pnl:.2f}). "
                    f"Cooling this direction for 12 hours."
                )

        # Cross-direction streak guard:
        # if the symbol is failing repeatedly in either direction, pause both sides.
        recent_three = outcomes[:3]
        if len(recent_three) == 3 and all(self._outcome_profit(r) < 0.0 for r in recent_three):
            combined_loss = abs(sum(self._outcome_profit(r) for r in recent_three))
            block_minutes = 60 if combined_loss < 300 else 150
            for side in ("BUY", "SELL"):
                side_key = self._symbol_direction_cooldown_key(canonical, side)
                self._symbol_direction_cooldown_until[side_key] = now_ts + (block_minutes * 60)
            return (
                f"Blocked: {canonical} recorded 3 consecutive losses "
                f"(~${combined_loss:.2f}) across directions. Cooling all entries for {block_minutes} min."
            )
        return None

    def _log_auto_trade(
        self,
        symbol: str,
        signal: TradeSignal,
        detail: str,
        success: bool,
        quality_score: float = 0.0,
        *,
        signal_id: Optional[int] = None,
        decision=None,
    ):
        self._cache_symbol_confidence(symbol, float(getattr(signal, "confidence", 0.0) or 0.0))
        decision_reason = signal.reason or detail
        gemini_summary = ""
        meta_model_summary = ""
        blocking_reasons: list[str] = []
        risk_warnings: list[str] = []
        machine_reasons: list[str] = []
        portfolio_status = ""
        allow_execute = False
        portfolio_fit_score: float | None = None
        projected_margin_utilization_pct: float | None = None
        projected_free_margin_pct: float | None = None
        gemini_state = "not-attached"
        gemini_bias = ""
        gemini_event_risk = ""
        if decision is not None:
            try:
                decision_reason = (
                    str(getattr(decision, "reason", "") or "")
                    or str(getattr(decision.signal_decision.final_signal, "reason", "") or "")
                    or detail
                )
                gemini = getattr(decision.signal_decision, "gemini_confirmation", None)
                if gemini is not None:
                    gemini_state = "degraded" if bool(getattr(gemini, "degraded", False)) else ("used" if bool(getattr(gemini, "used", False)) else "available")
                    gemini_bias = str(getattr(gemini, "news_bias", "") or "")
                    gemini_event_risk = str(getattr(gemini, "event_risk", "") or "")
                    gemini_summary = (
                        str(getattr(gemini, "summary_reason", "") or "")
                        or str(getattr(gemini, "reason", "") or "")
                    )
                    if not gemini_summary:
                        if bool(getattr(gemini, "degraded", False)):
                            gemini_summary = "Gemini degraded; technical-only fallback used."
                        elif not bool(getattr(gemini, "used", False)):
                            gemini_summary = "Gemini advisory not used for this decision."
                else:
                    gemini_summary = "Gemini not attached to this decision."

                meta = getattr(decision.signal_decision.final_signal, "metadata", {}) or {}
                meta_model = meta.get("meta_model") or {}
                if meta_model:
                    p = float(meta_model.get("profit_probability", 0.0) or 0.0)
                    qa = float(meta_model.get("quality_after", 0.0) or 0.0)
                    changed = bool(meta_model.get("changed_decision", False))
                    blocked = bool(meta_model.get("blocked", False))
                    meta_model_summary = (
                        f"v={meta_model.get('version_id', '')} "
                        f"p={p:.2f} q={qa:.2f} changed={changed} blocked={blocked}"
                    ).strip()
                portfolio = getattr(decision, "portfolio_risk_assessment", None)
                if portfolio is not None:
                    blocking_reasons = list(getattr(portfolio, "blocking_reasons", []) or [])
                    risk_warnings = list(getattr(portfolio, "warnings", []) or [])
                    portfolio_status = str(getattr(portfolio, "status", "") or "")
                    allow_execute = bool(getattr(portfolio, "allow_execute", False))
                    portfolio_fit_score = float(getattr(portfolio, "portfolio_fit_score", 0.0) or 0.0)
                    projected_margin_utilization_pct = float(getattr(portfolio, "projected_margin_utilization_pct", 0.0) or 0.0)
                    projected_free_margin_pct = float(getattr(portfolio, "projected_free_margin_pct", 0.0) or 0.0)
                risk_eval = getattr(decision, "risk_evaluation", None)
                if risk_eval is not None:
                    for warning in list(getattr(risk_eval, "warnings", []) or []):
                        if warning not in risk_warnings:
                            risk_warnings.append(warning)
                    machine_reasons = list(getattr(risk_eval, "machine_reasons", []) or [])
            except Exception:
                pass

        entry = {
            "timestamp": time.time(),
            "symbol": symbol,
            "action": ("HOLD" if (not success and str(signal.action).upper() in {"BUY", "SELL"}) else signal.action),
            "confidence": signal.confidence,
            "quality_score": quality_score,
            "detail": detail,
            "success": success,
            "signal_id": signal_id,
            "decision_reason": decision_reason or detail,
            "gemini_summary": gemini_summary,
            "meta_model_summary": meta_model_summary,
            "blocking_reasons": blocking_reasons,
            "risk_warnings": risk_warnings,
            "machine_reasons": machine_reasons,
            "portfolio_status": portfolio_status,
            "allow_execute": allow_execute,
            "portfolio_fit_score": portfolio_fit_score,
            "projected_margin_utilization_pct": projected_margin_utilization_pct,
            "projected_free_margin_pct": projected_free_margin_pct,
            "gemini_state": gemini_state,
            "gemini_bias": gemini_bias,
            "gemini_event_risk": gemini_event_risk,
        }
        self._trade_log.append(entry)
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]
        if self.db:
            try:
                # Auto-trader decisions are scanner events; profit fields are not
                # available at decision time, so they are left as 0 here.
                async def _safe_log_ai_activity():
                    try:
                        await self.db.log_ai_activity(
                            action=entry["action"],
                            symbol=symbol,
                            ticket=0,
                            detail=detail,
                            profit=0.0,
                            signal_id=signal_id,
                            decision_reason=entry.get("decision_reason", ""),
                            gemini_summary=entry.get("gemini_summary", ""),
                            meta_model_summary=entry.get("meta_model_summary", ""),
                        )
                    except Exception as exc:
                        msg = str(exc).lower()
                        if "database is locked" in msg:
                            logger.debug("Skipping ai_activity write due to DB lock for %s", symbol)
                            return
                        logger.warning("ai_activity logging failed for %s: %s", symbol, exc)

                asyncio.create_task(_safe_log_ai_activity())
            except Exception:
                pass

    def _with_strategy_time_rules(self, plan: dict) -> dict:
        plan_copy = dict(plan or {})
        meta = dict(plan_copy.get("metadata") or {})
        force_seconds = self._normalized_force_exit_seconds(self._cadence_sell_after_seconds)
        if force_seconds > 0:
            meta["force_time_exit_seconds"] = force_seconds
            plan_copy["force_time_exit_seconds"] = force_seconds
            plan_copy["planned_hold_minutes"] = max(1, math.ceil(force_seconds / 60.0))
        plan_copy["metadata"] = meta
        return plan_copy

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _symbol_info_field(self, symbol_info, name: str, default=None):
        if symbol_info is None:
            return default
        if isinstance(symbol_info, dict):
            value = symbol_info.get(name, default)
        else:
            value = getattr(symbol_info, name, default)
        return default if value in (None, "") else value

    def _build_trade_comment(
        self,
        amount_usd: float,
        *,
        sl_amount_usd: float | None = None,
        tp_amount_usd: float | None = None,
    ) -> str:
        parts = [f"TA{int(round(float(amount_usd)))}"]
        if sl_amount_usd is not None and sl_amount_usd > 0:
            parts.append(f"SLA{int(round(float(sl_amount_usd)))}")
        if tp_amount_usd is not None and tp_amount_usd > 0:
            parts.append(f"TPA{int(round(float(tp_amount_usd)))}")
        comment = "|".join(parts)
        if len(comment) <= 31:
            return comment
        compact = parts[0]
        for token in parts[1:]:
            trial = f"{compact}|{token}"
            if len(trial) <= 31:
                compact = trial
            else:
                break
        return compact[:31]

    def _compute_target_amounts_usd(
        self,
        *,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        symbol_info,
        volume: float,
    ) -> tuple[float, float]:
        if entry_price <= 0 or volume <= 0:
            return 0.0, 0.0
        price_per_unit = max(
            self._usd_per_price_distance(symbol_info=symbol_info, volume=volume),
            1e-9,
        )
        sl_amount_usd = float(abs(entry_price - float(stop_loss or 0.0)) * price_per_unit)
        tp_amount_usd = float(abs(float(take_profit or 0.0) - entry_price) * price_per_unit)
        return sl_amount_usd, tp_amount_usd

    def _started_money_risk_caps(self, started_amount_usd: float) -> tuple[float, float]:
        started = max(0.0, float(started_amount_usd or 0.0))
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced") or "balanced").lower()
        sl_pct, tp_pct = {
            "safe": (0.08, 0.18),
            "balanced": (0.12, 0.28),
            "aggressive": (0.18, 0.40),
        }.get(mode, (0.12, 0.28))
        if started <= 0:
            return 0.0, 0.0
        return started * sl_pct, started * tp_pct

    def _account_money_risk_cap(self, account) -> float:
        try:
            equity = float(getattr(account, "equity", 0.0) or 0.0)
        except Exception:
            equity = 0.0
        risk_pct = float(getattr(self.risk_engine.user_policy, "max_risk_per_trade", 0.0) or 0.0)
        return equity * (risk_pct / 100.0) if equity > 0 and risk_pct > 0 else 0.0

    def _cap_targets_by_money_budget(
        self,
        *,
        action: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        symbol_info,
        volume: float,
        started_amount_usd: float,
        account,
        digits: int,
    ) -> tuple[float, float, float, float]:
        if entry_price <= 0 or volume <= 0 or stop_loss <= 0 or take_profit <= 0:
            return stop_loss, take_profit, 0.0, 0.0
        price_per_unit = self._usd_per_price_distance(symbol_info=symbol_info, volume=volume)
        if price_per_unit <= 0:
            return stop_loss, take_profit, 0.0, 0.0

        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.6) or 1.6))
        started_sl_cap, started_tp_cap = self._started_money_risk_caps(started_amount_usd)
        account_sl_cap = self._account_money_risk_cap(account)
        sl_caps = [cap for cap in (started_sl_cap, account_sl_cap) if cap > 0]
        max_sl_amount = min(sl_caps) if sl_caps else 0.0
        if max_sl_amount <= 0:
            return self._compute_capped_target_amounts(
                action=action,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                price_per_unit=price_per_unit,
                digits=digits,
            )
        max_sl_amount = max(25.0, max_sl_amount)
        max_tp_amount = max(max_sl_amount * min_rr, started_tp_cap if started_tp_cap > 0 else 0.0)

        cur_sl_amount = abs(entry_price - float(stop_loss)) * price_per_unit
        cur_tp_amount = abs(float(take_profit) - entry_price) * price_per_unit
        next_sl_amount = min(cur_sl_amount if cur_sl_amount > 0 else max_sl_amount, max_sl_amount)
        next_tp_amount = cur_tp_amount if cur_tp_amount > 0 else (next_sl_amount * min_rr)
        next_tp_amount = max(next_tp_amount, next_sl_amount * min_rr)
        if max_tp_amount > 0:
            next_tp_amount = min(next_tp_amount, max_tp_amount)
            if next_tp_amount < next_sl_amount * min_rr:
                next_sl_amount = max(25.0, next_tp_amount / min_rr)

        sl_distance = next_sl_amount / price_per_unit
        tp_distance = next_tp_amount / price_per_unit
        if str(action or "").upper() == "BUY":
            next_sl = round(entry_price - sl_distance, digits)
            next_tp = round(entry_price + tp_distance, digits)
        else:
            next_sl = round(entry_price + sl_distance, digits)
            next_tp = round(entry_price - tp_distance, digits)
        return next_sl, next_tp, next_sl_amount, next_tp_amount

    def _compute_capped_target_amounts(
        self,
        *,
        action: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        price_per_unit: float,
        digits: int,
    ) -> tuple[float, float, float, float]:
        sl_amount = abs(entry_price - float(stop_loss or 0.0)) * price_per_unit
        tp_amount = abs(float(take_profit or 0.0) - entry_price) * price_per_unit
        return stop_loss, take_profit, sl_amount, tp_amount

    def _started_amount_target_pct_caps(self, symbol_info) -> tuple[float, float]:
        text = " ".join(
            [
                str(self._symbol_info_field(symbol_info, "path", "") or "").lower(),
                str(self._symbol_info_field(symbol_info, "description", "") or "").lower(),
                str(self._symbol_info_field(symbol_info, "name", "") or "").lower(),
            ]
        )
        if any(token in text for token in ("gold", "silver", "xau", "xag", "oil", "wti", "brent", "commodity", "index", "us30", "us500", "spx", "nas100", "dax", "ftse")):
            return (0.035, 0.08)
        if any(token in text for token in ("stock", "share", "equity", "nasdaq", "nyse")):
            return (0.05, 0.12)
        if any(token in text for token in ("forex", "eurusd", "gbpusd", "usdjpy")):
            return (0.015, 0.035)
        if any(token in text for token in ("crypto", "btc", "eth")):
            return (0.06, 0.14)
        return (0.04, 0.09)

    def _compute_started_amount_target_amounts_usd(
        self,
        *,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        started_amount_usd: float,
        symbol_info=None,
    ) -> tuple[float | None, float | None]:
        if (
            entry_price <= 0
            or started_amount_usd <= 0
            or stop_loss <= 0
            or take_profit <= 0
        ):
            return None, None
        sl_pct = abs(entry_price - float(stop_loss)) / entry_price
        tp_pct = abs(float(take_profit) - entry_price) / entry_price
        sl_cap_pct, tp_cap_pct = self._started_amount_target_pct_caps(symbol_info)
        if sl_cap_pct > 0:
            sl_pct = min(sl_pct, sl_cap_pct)
        if tp_cap_pct > 0:
            tp_pct = min(tp_pct, tp_cap_pct)
        sl_amount = max(0.0, float(started_amount_usd) * float(sl_pct))
        tp_amount = max(0.0, float(started_amount_usd) * float(tp_pct))
        return sl_amount, tp_amount

    async def _execute_strict_cadence_fallback(
        self,
        *,
        symbol: str,
        canonical_symbol: str,
        action: str,
        quality_score: float = 1.0,
    ) -> bool:
        tick = self.market_data.get_tick(symbol)
        fresh_tick_ok, fresh_tick_reason = _fresh_live_tick_ok(tick)
        if not fresh_tick_ok:
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason="Strict cadence fallback unavailable"),
                f"Strict cadence fallback skipped: fresh live quote is unavailable ({fresh_tick_reason}).",
                False,
                quality_score,
            )
            return False
        symbol_info = self.market_data.get_symbol_info(symbol)
        if not symbol_info:
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason="Strict cadence fallback unavailable"),
                "Strict cadence fallback skipped: symbol info is unavailable.",
                False,
                quality_score,
            )
            return False

        def _sym_field(name: str, default: float | int):
            if isinstance(symbol_info, dict):
                value = symbol_info.get(name, default)
            else:
                value = getattr(symbol_info, name, default)
            return default if value in (None, "") else value

        action = str(action or "BUY").upper()
        entry_price = float(tick.ask if action == "BUY" else tick.bid)
        if entry_price <= 0:
            return False
        account = self.connector.refresh_account()
        budget_cap_usd = self._auto_trade_budget_cap_usd(account)
        remaining_budget_usd = self._remaining_custom_budget_usd(account, budget_cap_usd)
        if budget_cap_usd is not None and (remaining_budget_usd is None or remaining_budget_usd <= 0.0):
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason="Strict cadence budget exhausted"),
                "Strict cadence fallback skipped: custom budget is already fully used by open margin.",
                False,
                quality_score,
            )
            return False
        max_single_trade_budget_usd = self._max_single_trade_budget_usd(budget_cap_usd)
        effective_trade_cap_usd = max_single_trade_budget_usd
        if remaining_budget_usd is not None:
            effective_trade_cap_usd = (
                min(float(effective_trade_cap_usd), float(remaining_budget_usd))
                if effective_trade_cap_usd is not None
                else float(remaining_budget_usd)
            )
        vol_min = float(_sym_field("volume_min", 0.01) or 0.01)
        vol_step = float(_sym_field("volume_step", 0.01) or 0.01)
        vol_max = float(_sym_field("volume_max", 0.0) or 0.0)
        effective_fixed_lot = self._fixed_lot_size if self._fixed_lot_size > 0 else 1.0
        if self._fixed_lot_size > 0:
            volume = self._round_volume_down(effective_fixed_lot, vol_min, vol_step, vol_max)
        elif self._allocation_pct_free_margin > 0:
            target_margin = float(getattr(account, "free_margin", 0.0) or 0.0) * (float(self._allocation_pct_free_margin) / 100.0)
            if effective_trade_cap_usd is not None:
                target_margin = min(target_margin, effective_trade_cap_usd)
            volume, _ = self._solve_volume_by_margin_target(
                symbol=symbol,
                action=action,
                target_margin_usd=target_margin,
                price=entry_price,
                contract_size=float(_sym_field("trade_contract_size", 100000) or 100000),
                vol_min=vol_min,
                vol_step=vol_step,
                vol_max=vol_max,
            )
        else:
            volume = self._round_volume_down(effective_fixed_lot, vol_min, vol_step, vol_max)
        if volume <= 0:
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason="Strict cadence volume invalid"),
                f"Strict cadence fallback skipped: configured lot {effective_fixed_lot:.2f} is invalid for broker min/step.",
                False,
                quality_score,
            )
            return False

        digits = int(_sym_field("digits", 5) or 5)

        point = float(_sym_field("point", 0.01) or 0.01)
        # Keep strict-cadence fallback alive longer than a few seconds by avoiding ultra-tight stops.
        min_rr = max(1.2, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.6) or 1.6))
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced") or "balanced").lower()
        fallback_pct_by_mode = {
            "safe": (0.0080, 0.0160),
            "balanced": (0.0120, 0.0260),
            "aggressive": (0.0180, 0.0450),
        }
        sl_fallback_pct, tp_fallback_pct = fallback_pct_by_mode.get(mode, fallback_pct_by_mode["balanced"])
        sl_distance = max(entry_price * sl_fallback_pct, point * 250)
        tp_distance = max(entry_price * max(tp_fallback_pct, sl_fallback_pct * min_rr), point * 450)
        if action == "BUY":
            stop_loss = round(entry_price - sl_distance, digits)
            take_profit = round(entry_price + tp_distance, digits)
        else:
            stop_loss = round(entry_price + sl_distance, digits)
            take_profit = round(entry_price - tp_distance, digits)
        stop_loss, take_profit = self._normalize_live_stops(
            symbol=symbol,
            action=action,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        stop_loss, take_profit = self._cap_symbol_target_distances(
            action=action,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            symbol_info=symbol_info,
            digits=digits,
        )

        estimated_margin = self.execution.estimate_margin(
            symbol=symbol,
            action=action,
            volume=volume,
            price=entry_price,
        )
        if effective_trade_cap_usd is not None and estimated_margin is not None:
            if float(estimated_margin) > (float(effective_trade_cap_usd) * 1.02):
                capped_volume, capped_margin = self._solve_volume_by_margin_target(
                    symbol=symbol,
                    action=action,
                    target_margin_usd=float(effective_trade_cap_usd),
                    price=entry_price,
                    contract_size=float(_sym_field("trade_contract_size", 100000) or 100000),
                    vol_min=vol_min,
                    vol_step=vol_step,
                    vol_max=vol_max,
                )
                if capped_volume <= 0:
                    self._log_auto_trade(
                        symbol,
                        TradeSignal(action="HOLD", confidence=0.0, reason="Strict cadence over custom budget cap"),
                        "Strict cadence fallback skipped: custom budget cap is below broker minimum volume.",
                        False,
                        quality_score,
                    )
                    return False
                volume = capped_volume
                estimated_margin = capped_margin
        started_amount = max(1.0, float(estimated_margin or 0.0))
        stop_loss, take_profit, sl_amount_usd, tp_amount_usd = self._cap_targets_by_money_budget(
            action=action,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            symbol_info=symbol_info,
            volume=volume,
            started_amount_usd=started_amount,
            account=account,
            digits=digits,
        )
        stop_loss, take_profit = self._normalize_live_stops(
            symbol=symbol,
            action=action,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        sl_amount_usd, tp_amount_usd = self._compute_target_amounts_usd(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            symbol_info=symbol_info,
            volume=volume,
        )
        stop_loss, take_profit, sl_amount_usd, tp_amount_usd = self._cap_targets_by_money_budget(
            action=action,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            symbol_info=symbol_info,
            volume=volume,
            started_amount_usd=started_amount,
            account=account,
            digits=digits,
        )
        started_sl_amount_usd, started_tp_amount_usd = self._compute_started_amount_target_amounts_usd(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            started_amount_usd=started_amount,
            symbol_info=symbol_info,
        )
        comment = self._build_trade_comment(
            started_amount,
            sl_amount_usd=sl_amount_usd or started_sl_amount_usd,
            tp_amount_usd=tp_amount_usd or started_tp_amount_usd,
        )
        order_req = OrderRequest(
            symbol=symbol,
            action=action,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment=comment,
            execution_context="auto_strict_cadence",
            execution_mode=config.AUTO_TRADER_EXECUTION_MODE,
        )
        live_policy_block = self._live_trading_policy_block_reason()
        if live_policy_block:
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason=live_policy_block),
                f"Strict cadence fallback skipped: {live_policy_block}",
                False,
                quality_score,
            )
            return False
        kill_switch_block = self._kill_switch_block_reason()
        if kill_switch_block:
            self.risk_engine.auto_trade_enabled = False
            self.risk_engine.auto_trade_session_armed = False
            self._log_auto_trade(
                symbol,
                TradeSignal(action="HOLD", confidence=0.0, reason=kill_switch_block),
                f"Strict cadence fallback skipped: {kill_switch_block}",
                False,
                quality_score,
            )
            return False
        execution_mode = str(getattr(order_req, "execution_mode", "live") or "live").lower()
        if execution_mode in {"paper", "shadow"}:
            self._log_auto_trade(
                symbol,
                TradeSignal(action=action, confidence=1.0, reason=f"{execution_mode} mode"),
                f"Strict cadence fallback skipped broker order_send because AutoTrader execution mode is {execution_mode}.",
                True,
                quality_score,
            )
            return True
        result = self.execution.place_order(order_req)
        if self.db:
            await self.db.log_order(
                signal_id=None,
                symbol=symbol,
                action=action,
                volume=volume,
                price=result.price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                ticket=result.ticket,
                retcode=result.retcode,
                retcode_desc=result.retcode_desc,
                success=result.success,
                comment=comment,
                execution_mode=order_req.execution_mode,
            )
            if result.success and result.ticket:
                await self.db.log_position_change(
                    result.ticket,
                    "auto_opened",
                    symbol,
                    f"STRICT CADENCE {action} {volume} lots at {result.price}",
                )
                await self.db.save_position_management_plan(
                    ticket=result.ticket,
                    signal_id=None,
                    symbol=symbol,
                    action=action,
                    plan=self._with_strategy_time_rules(
                        {
                            "strategy": "strict_cadence",
                            "force_time_exit_seconds": self._normalized_force_exit_seconds(self._cadence_sell_after_seconds),
                            "planned_hold_minutes": (
                                max(
                                    1,
                                    math.ceil(
                                        self._normalized_force_exit_seconds(self._cadence_sell_after_seconds) / 60.0
                                    ),
                                )
                                if self._normalized_force_exit_seconds(self._cadence_sell_after_seconds) > 0
                                else 0
                            ),
                            "metadata": {
                                "strategy_family": "strict_cadence",
                                "symbol_category": get_asset_class(symbol),
                                "force_time_exit_seconds": self._normalized_force_exit_seconds(self._cadence_sell_after_seconds),
                            },
                        }
                    ),
                )

        if result.success:
            self.position_manager.enable_auto_portfolio_targets()
            opened_now = time.time()
            self._last_cadence_entry_ts[canonical_symbol] = opened_now
            self._remember_symbol_direction_opened(canonical_symbol, action, opened_now)
            self._log_auto_trade(
                symbol,
                TradeSignal(action=action, confidence=1.0, reason="Strict cadence timeout fallback entry"),
                f"Strict cadence order #{result.ticket} filled at {result.price} ({volume:.2f} lots).",
                True,
                quality_score,
            )
            logger.info(
                "AUTO-TRADE STRICT CADENCE SUCCESS: %s %s ticket=%s volume=%.2f",
                action,
                symbol,
                result.ticket,
                volume,
            )
            return True

        self._log_auto_trade(
            symbol,
            TradeSignal(action=action, confidence=1.0, reason="Strict cadence timeout fallback failed"),
            result.retcode_desc or "Strict cadence execution failed.",
            False,
            quality_score,
        )
        return False

    def _round_volume_down(self, raw_volume: float, vol_min: float, vol_step: float, vol_max: float) -> float:
        if raw_volume <= 0:
            return 0.0
        step = vol_step if vol_step and vol_step > 0 else max(vol_min, 0.01)
        bounded = min(raw_volume, vol_max) if vol_max > 0 else raw_volume
        units = math.floor((bounded + 1e-12) / step)
        rounded = units * step
        if rounded + 1e-12 < vol_min:
            return 0.0
        return round(rounded, 6)

    def _solve_volume_by_margin_target(
        self,
        *,
        symbol: str,
        action: str,
        target_margin_usd: float,
        price: float,
        contract_size: float,
        vol_min: float,
        vol_step: float,
        vol_max: float,
    ) -> tuple[float, float | None]:
        if target_margin_usd <= 0:
            return 0.0, 0.0
        leverage = 1.0
        try:
            acct = self.connector.refresh_account()
            leverage = float(acct.leverage) if acct and acct.leverage else 1.0
        except Exception:
            leverage = 1.0
        rough = (
            (target_margin_usd * max(leverage, 1.0)) / (price * max(contract_size, 1e-9))
            if price > 0 and contract_size > 0
            else vol_min
        )
        volume = self._round_volume_down(
            raw_volume=max(rough, vol_min),
            vol_min=vol_min,
            vol_step=vol_step,
            vol_max=vol_max if vol_max > 0 else 1_000_000.0,
        )
        if volume <= 0:
            return 0.0, None

        def _margin(v: float) -> float | None:
            m = self.execution.estimate_margin(symbol=symbol, action=action, volume=round(v, 6), price=price)
            return float(m) if m is not None else None

        margin = _margin(volume)
        if margin is None:
            return volume, None
        for _ in range(3):
            if margin <= 0:
                break
            scale = target_margin_usd / margin
            if 0.96 <= scale <= 1.04:
                break
            candidate = self._round_volume_down(
                raw_volume=max(vol_min, volume * max(0.2, min(scale, 5.0))),
                vol_min=vol_min,
                vol_step=vol_step,
                vol_max=vol_max if vol_max > 0 else 1_000_000.0,
            )
            if candidate <= 0 or abs(candidate - volume) < 1e-9:
                break
            cand_margin = _margin(candidate)
            if cand_margin is None:
                break
            volume, margin = candidate, cand_margin
        return volume, margin

    def _rebalance_volume_for_started_amount_risk(
        self,
        *,
        entry_price: float,
        stop_loss: float,
        desired_sl_amount_usd: float,
        volume: float,
        symbol_info,
        vol_min: float,
        vol_step: float,
        vol_max: float,
    ) -> tuple[float, str | None]:
        usd_per_unit = self._usd_per_price_distance(symbol_info=symbol_info, volume=volume)
        if (
            desired_sl_amount_usd <= 0
            or stop_loss <= 0
            or entry_price <= 0
            or volume <= 0
            or usd_per_unit <= 0
        ):
            return volume, None
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return volume, None
        actual_sl_amount = sl_distance * usd_per_unit
        if actual_sl_amount <= desired_sl_amount_usd * 1.02:
            return volume, None
        max_volume_for_risk = desired_sl_amount_usd / max(sl_distance * (usd_per_unit / max(volume, 1e-9)), 1e-9)
        adjusted = self._round_volume_down(max_volume_for_risk, vol_min, vol_step, vol_max)
        if adjusted <= 0:
            min_possible_risk = sl_distance * self._usd_per_price_distance(
                symbol_info=symbol_info,
                volume=max(vol_min, 0.0),
            )
            return 0.0, (
                f"Skipped: broker minimum stop distance implies at least ${min_possible_risk:.2f} risk "
                f"for {self.risk_engine.user_policy.mode} sizing."
            )
        return adjusted, None

    def _cap_symbol_target_distances(
        self,
        *,
        action: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        symbol_info,
        digits: int,
    ) -> tuple[float, float]:
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            return stop_loss, take_profit
        sl_cap_pct, tp_cap_pct = self._started_amount_target_pct_caps(symbol_info)
        if sl_cap_pct <= 0 or tp_cap_pct <= 0:
            return stop_loss, take_profit
        sl_distance = min(abs(entry_price - stop_loss), entry_price * sl_cap_pct)
        tp_distance = min(abs(take_profit - entry_price), entry_price * tp_cap_pct)
        if str(action or "").upper() == "BUY":
            return round(entry_price - sl_distance, digits), round(entry_price + tp_distance, digits)
        return round(entry_price + sl_distance, digits), round(entry_price - tp_distance, digits)

    def _normalize_live_stops(
        self,
        *,
        symbol: str,
        action: str,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[float, float]:
        """Clamp SL/TP to broker minimum distance using latest tick."""
        tick = self.market_data.get_tick(symbol)
        info = self.market_data.get_symbol_info(symbol)
        if not tick or not info:
            return stop_loss, take_profit

        digits = int(self._symbol_info_field(info, "digits", 5) or 5)
        point = float(self._symbol_info_field(info, "point", 0.00001) or 0.00001)
        stops_level = float(self._symbol_info_field(info, "trade_stops_level", 0) or 0)
        spread_points = float(tick.spread or 10)
        min_distance = max(stops_level, spread_points, 10.0) * point * 1.5
        required = max(min_distance + point, point)
        exec_price = float(tick.ask if action == "BUY" else tick.bid)
        if exec_price <= 0:
            return stop_loss, take_profit

        sl = float(stop_loss)
        tp = float(take_profit)
        if action == "BUY":
            if abs(exec_price - sl) < required:
                sl = round(exec_price - required, digits)
            if abs(tp - exec_price) < required:
                tp = round(exec_price + required, digits)
        else:
            if abs(sl - exec_price) < required:
                sl = round(exec_price + required, digits)
            if abs(exec_price - tp) < required:
                tp = round(exec_price - required, digits)
        return sl, tp

    def _usd_per_price_distance(self, *, symbol_info, volume: float) -> float:
        if volume <= 0:
            return 0.0
        if symbol_info is None:
            return 0.0
        if isinstance(symbol_info, dict):
            info = dict(symbol_info)
        else:
            info = {
                "trade_tick_value": getattr(symbol_info, "trade_tick_value", 0.0),
                "trade_tick_size": getattr(symbol_info, "trade_tick_size", 0.0),
                "point": getattr(symbol_info, "point", 0.0),
                "trade_contract_size": getattr(symbol_info, "trade_contract_size", 0.0),
            }
        tick_value = float(info.get("trade_tick_value", 0.0) or 0.0)
        tick_size = float(info.get("trade_tick_size", 0.0) or 0.0)
        point = float(info.get("point", 0.0) or 0.0)
        contract_size = float(info.get("trade_contract_size", 0.0) or 0.0)
        contract_based = (volume * contract_size) if contract_size > 0 else 0.0
        if tick_value > 0:
            unit = tick_size if tick_size > 0 else point
            if unit > 0:
                tick_based = volume * (tick_value / unit)
                # Prefer contract-size scaling whenever available; many brokers publish
                # noisy tick-value metadata for CFDs/metals that can inflate USD distance.
                if contract_based > 0 and tick_based > 0:
                    return contract_based
                return tick_based
        if contract_based > 0:
            return contract_based
        return 0.0

    async def _dynamic_amount_based_targets(
        self,
        *,
        decision,
        action: str,
        entry_price: float,
        volume: float,
        symbol_info,
        amount_usd: float,
        digits: int,
    ) -> tuple[float, float, float, float]:
        mode = str(getattr(self.risk_engine.user_policy, "mode", "balanced")).lower()
        min_rr = max(1.0, float(getattr(self.risk_engine.user_policy, "min_reward_risk", 1.8) or 1.8))
        # Generate symbol-specific targets from price volatility first (not flat amount ratios).
        base_price_pct_by_mode = {
            "safe": (0.0060, 0.0120),
            "balanced": (0.0100, 0.0200),
            "aggressive": (0.0150, 0.0350),
        }
        sl_price_pct, tp_price_pct = base_price_pct_by_mode.get(mode, (0.0050, 0.0100))
        range_pct = 0.0
        drift_pct = 0.0
        drift_aligned = True
        atr_pct = 0.0

        # 1) Last-hours chart behavior (explicit volatility + directional push)
        try:
            h1_bars = (
                decision.signal_decision.market_context.bars_by_timeframe.get("H1", [])
                if decision.signal_decision.market_context and decision.signal_decision.market_context.bars_by_timeframe
                else []
            )
            if len(h1_bars) >= 6:
                recent = h1_bars[-6:]
                highs = [float(b.get("high", 0.0)) for b in recent]
                lows = [float(b.get("low", 0.0)) for b in recent]
                closes = [float(b.get("close", 0.0)) for b in recent]
                avg_close = sum(closes) / max(len(closes), 1)
                if avg_close > 0:
                    range_pct = ((max(highs) - min(lows)) / avg_close) * 100.0
                    drift = closes[-1] - closes[0]
                    drift_pct = (abs(drift) / avg_close) * 100.0
                    drift_aligned = (action == "BUY" and drift > 0) or (action == "SELL" and drift < 0)

                    # ATR(14) as percent of price, used to personalize SL/TP per market.
                    if len(recent) >= 2:
                        trs: list[float] = []
                        prev_close = float(recent[0].get("close", avg_close))
                        for bar in recent[1:]:
                            high = float(bar.get("high", prev_close))
                            low = float(bar.get("low", prev_close))
                            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                            trs.append(max(0.0, tr))
                            prev_close = float(bar.get("close", prev_close))
                        if trs and avg_close > 0:
                            atr_pct = (sum(trs) / len(trs)) / avg_close

                    if range_pct >= 1.2:
                        sl_price_pct += 0.0015
                        tp_price_pct += 0.0030
                    elif range_pct >= 0.8:
                        sl_price_pct += 0.0010
                        tp_price_pct += 0.0020
                    elif range_pct <= 0.35:
                        sl_price_pct -= 0.0006
                        tp_price_pct -= 0.0010

                    if drift_pct >= 0.25 and drift_aligned:
                        tp_price_pct += 0.0012
                    elif drift_pct >= 0.25 and not drift_aligned:
                        sl_price_pct += 0.0008
                        tp_price_pct -= 0.0012
        except Exception:
            pass

        if atr_pct > 0:
            atr_sl = atr_pct * (0.9 if mode == "safe" else (1.1 if mode == "balanced" else 1.35))
            atr_tp = atr_sl * max(1.25, min_rr)
            sl_price_pct = max(sl_price_pct, atr_sl)
            tp_price_pct = max(tp_price_pct, atr_tp)

        # 2) Trade quality + Gemini/news + profile context
        quality = float(getattr(decision.trade_quality_assessment, "final_trade_quality_score", 0.0) or 0.0)
        threshold = float(getattr(decision.trade_quality_assessment, "threshold", 0.75) or 0.75)
        quality_edge = quality - threshold
        if quality_edge >= 0.10:
            sl_price_pct -= 0.0007
            tp_price_pct += 0.0015
        elif quality_edge >= 0.04:
            sl_price_pct -= 0.0004
            tp_price_pct += 0.0010
        elif quality_edge <= -0.05:
            sl_price_pct += 0.0012
            tp_price_pct -= 0.0018

        gemini = decision.signal_decision.gemini_confirmation
        if gemini is None or getattr(gemini, "degraded", False):
            sl_price_pct += 0.0008
            tp_price_pct -= 0.0010
        else:
            event_risk = str(getattr(gemini, "event_risk", "low")).lower()
            contradiction = bool(getattr(gemini, "contradiction_flag", False))
            news_bias = str(getattr(gemini, "news_bias", "neutral")).lower()
            aligned_bias = (
                (action == "BUY" and news_bias == "bullish")
                or (action == "SELL" and news_bias == "bearish")
            )
            if event_risk == "high" or contradiction:
                sl_price_pct += 0.0015
                tp_price_pct -= 0.0020
            elif aligned_bias and event_risk == "low":
                tp_price_pct += 0.0010

        category = ""
        if decision.signal_decision.market_context.profile is not None:
            category = str(decision.signal_decision.market_context.profile.category or "")
        elif decision.signal_decision.market_context.symbol_info is not None:
            category = str(decision.signal_decision.market_context.symbol_info.category or "")
        category = category.title()
        if category == "Commodities":
            sl_price_pct += 0.0008
            tp_price_pct += 0.0016
        elif category == "Stocks":
            sl_price_pct += 0.0005
            tp_price_pct += 0.0010
        elif category == "Indices":
            sl_price_pct += 0.0006
            tp_price_pct += 0.0012

        # 3) Ask Gemini for bounded SL/TP percentage deltas from last-hours chart + mode.
        sl_delta, tp_delta = await self._gemini_target_pct_adjustment(
            mode=mode,
            action=action,
            range_pct=range_pct,
            drift_pct=drift_pct,
            drift_aligned=drift_aligned,
            category=category or "Other",
            quality=quality,
            threshold=threshold,
            news_bias=str(getattr(gemini, "news_bias", "neutral")).lower() if gemini else "neutral",
            event_risk=str(getattr(gemini, "event_risk", "low")).lower() if gemini else "low",
            contradiction_flag=bool(getattr(gemini, "contradiction_flag", False)) if gemini else False,
        )
        # Gemini deltas are scaled down before applying to price-percent model.
        sl_price_pct += (sl_delta * 0.15)
        tp_price_pct += (tp_delta * 0.15)

        category_bounds = {
            # Wider caps keep automated trades from being stopped by ordinary noise,
            # especially in aggressive mode, while still bounding worst-case distance.
            "Commodities": (0.0060, 0.0350, 0.0120, 0.0800),
            "Stocks": (0.0080, 0.0500, 0.0160, 0.1200),
            "Indices": (0.0060, 0.0300, 0.0120, 0.0700),
            "Forex": (0.0025, 0.0150, 0.0050, 0.0350),
            "Crypto": (0.0100, 0.0600, 0.0200, 0.1400),
            "Other": (0.0060, 0.0400, 0.0120, 0.0900),
        }
        sl_min, sl_max, tp_min, tp_max = category_bounds.get(category or "Other", category_bounds["Other"])
        sl_price_pct = self._clamp(sl_price_pct, sl_min, sl_max)
        tp_price_pct = self._clamp(tp_price_pct, tp_min, tp_max)
        tp_price_pct = max(tp_price_pct, sl_price_pct * min_rr)
        tp_price_pct = self._clamp(tp_price_pct, tp_min, tp_max)
        if tp_price_pct < (sl_price_pct * min_rr):
            # Preserve minimum RR even when clamped by lowering SL if needed.
            sl_price_pct = max(sl_min, tp_price_pct / max(min_rr, 1.0))

        sl_distance = entry_price * sl_price_pct
        tp_distance = entry_price * tp_price_pct

        if action == "BUY":
            sl = round(entry_price - sl_distance, digits)
            tp = round(entry_price + tp_distance, digits)
        else:
            sl = round(entry_price + sl_distance, digits)
            tp = round(entry_price - tp_distance, digits)
        price_per_unit = max(self._usd_per_price_distance(symbol_info=symbol_info, volume=volume), 1e-9)
        sl_amount_usd = float(abs(entry_price - sl) * price_per_unit)
        tp_amount_usd = float(abs(tp - entry_price) * price_per_unit)
        return sl, tp, sl_amount_usd, tp_amount_usd

    async def _gemini_target_pct_adjustment(
        self,
        *,
        mode: str,
        action: str,
        range_pct: float,
        drift_pct: float,
        drift_aligned: bool,
        category: str,
        quality: float,
        threshold: float,
        news_bias: str,
        event_risk: str,
        contradiction_flag: bool,
    ) -> tuple[float, float]:
        if time.time() < self._gemini_quota_cooldown_until:
            return 0.0, 0.0
        gemini = self.agents.get("GeminiAgent")
        client = getattr(gemini, "_client", None)
        if not getattr(gemini, "available", False) or client is None:
            return 0.0, 0.0
        try:
            from google.genai import types as genai_types

            payload = {
                "risk_mode": mode,
                "action": action,
                "chart_last_hours": {
                    "range_pct": range_pct,
                    "drift_pct": drift_pct,
                    "drift_aligned_with_action": drift_aligned,
                },
                "context": {
                    "category": category,
                    "trade_quality": quality,
                    "quality_threshold": threshold,
                    "news_bias": news_bias,
                    "event_risk": event_risk,
                    "contradiction_flag": contradiction_flag,
                },
                "constraints": {
                    "sl_delta_pct_min": -0.02,
                    "sl_delta_pct_max": 0.03,
                    "tp_delta_pct_min": -0.03,
                    "tp_delta_pct_max": 0.06,
                },
                "output_schema": {"sl_delta_pct": "number", "tp_delta_pct": "number"},
            }
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=os.getenv("GEMINI_MODEL", "gemma-4-26b-a4b-it").strip() or "gemma-4-26b-a4b-it",
                    contents=[{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=True)}]}],
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Return JSON only with sl_delta_pct and tp_delta_pct.",
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=5.0,
            )
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`").replace("json", "", 1).strip()
            data = json.loads(text)
            sl_delta = self._clamp(float(data.get("sl_delta_pct", 0.0)), -0.02, 0.03)
            tp_delta = self._clamp(float(data.get("tp_delta_pct", 0.0)), -0.03, 0.06)
            if hasattr(gemini, "clear_runtime_error"):
                gemini.clear_runtime_error()
            return sl_delta, tp_delta
        except Exception as exc:
            exc_text = str(exc)
            if "429" in exc_text or "RESOURCE_EXHAUSTED" in exc_text:
                retry = 60.0
                match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", exc_text, flags=re.IGNORECASE)
                if match:
                    retry = max(15.0, float(match.group(1)))
                self._gemini_quota_cooldown_until = time.time() + retry
            if hasattr(gemini, "mark_runtime_error"):
                gemini.mark_runtime_error(exc_text)
            logger.warning("Auto-trader Gemini SL/TP adjustment skipped: %s", exc)
            return 0.0, 0.0
