from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

from agent.interface import AgentInput
from domain.models import (
    AntiChurnAssessment,
    ExecutionDecision,
    GeminiAssessment,
    MarketContext,
    NormalizedSymbolInfo,
    PortfolioRiskAssessment,
    PositionManagementPlan,
    RiskApprovalDecision,
    RiskEvaluation,
    SignalDecision,
    TechnicalSignal,
    TradeDecisionAssessment,
)
from services.anti_churn_service import AntiChurnService
from services.gemini_news_analysis_service import GeminiNewsAnalysisService
from services.gemini_strategy_advisor_service import GeminiStrategyAdvisorService
from services.gemini_trade_brain_service import GeminiTradeBrainService
from services.news_engine import NewsEngine
from services.news_ingestion_service import NewsIngestionService
from services.event_ingestion_service import EventIngestionService
from services.portfolio_risk_service import PortfolioRiskService
from services.risk_service import RiskService
from services.symbol_profile_service import SymbolProfileService
from services.symbol_universe_service import SymbolUniverseService
from services.trade_decision_service import TradeDecisionService
from services.trade_quality_service import TradeQualityService
from services.meta_model_service import MetaModelService
from services.cloud_brain_decision_client import CloudBrainDecisionClient
from config import config
from research.feature_builder import build_feature_snapshot
from storage.research_repository import ResearchRepository

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if math.isfinite(float(value)) else None
    return value


class SignalPipelineService:
    def __init__(
        self,
        connector,
        market_data,
        execution,
        risk_engine,
        agents: dict,
        gemini_adapter,
        db=None,
        universe_service: Optional[SymbolUniverseService] = None,
        profile_service: Optional[SymbolProfileService] = None,
        trade_quality_service: Optional[TradeQualityService] = None,
        portfolio_risk_service: Optional[PortfolioRiskService] = None,
        anti_churn_service: Optional[AntiChurnService] = None,
        news_ingestion_service: Optional[NewsIngestionService] = None,
        event_ingestion_service: Optional[EventIngestionService] = None,
        gemini_news_analysis_service: Optional[GeminiNewsAnalysisService] = None,
        gemini_strategy_advisor_service: Optional[GeminiStrategyAdvisorService] = None,
        gemini_trade_brain_service: Optional[GeminiTradeBrainService] = None,
        news_engine: Optional[NewsEngine] = None,
        trade_decision_service: Optional[TradeDecisionService] = None,
        risk_service: Optional[RiskService] = None,
        meta_model_service: Optional[MetaModelService] = None,
    ):
        self.connector = connector
        self.market_data = market_data
        self.execution = execution
        self.risk_engine = risk_engine
        self.agents = agents
        self.gemini_adapter = gemini_adapter
        self.db = db
        self.universe_service = universe_service or SymbolUniverseService()
        self.profile_service = profile_service or SymbolProfileService(universe_service=self.universe_service)
        self.trade_quality_service = trade_quality_service or TradeQualityService()
        self.portfolio_risk_service = portfolio_risk_service or PortfolioRiskService(
            market_data=market_data,
            profile_service=self.profile_service,
        )
        self.anti_churn_service = anti_churn_service or AntiChurnService()
        self.news_ingestion_service = news_ingestion_service or NewsIngestionService()
        self.event_ingestion_service = event_ingestion_service
        if gemini_news_analysis_service is not None:
            self.gemini_news_analysis_service = gemini_news_analysis_service
        else:
            gemini_agent = getattr(gemini_adapter, "gemini_agent", None)
            self.gemini_news_analysis_service = GeminiNewsAnalysisService(
                gemini_agent=gemini_agent,
                timeout_seconds=getattr(gemini_adapter, "timeout_seconds", 12.0),
                max_retries=getattr(gemini_adapter, "max_retries", 1),
            ) if gemini_agent is not None else None
        self.gemini_strategy_advisor_service = gemini_strategy_advisor_service or GeminiStrategyAdvisorService(
            timeout_seconds=getattr(gemini_adapter, "timeout_seconds", 12.0),
            max_retries=getattr(gemini_adapter, "max_retries", 1),
            model_name=config.GEMINI_MODEL,
        )
        self.gemini_trade_brain_service = gemini_trade_brain_service or GeminiTradeBrainService(
            timeout_seconds=getattr(gemini_adapter, "timeout_seconds", 12.0),
            max_retries=getattr(gemini_adapter, "max_retries", 1),
            model_name=config.GEMINI_MODEL,
        )
        self.trade_decision_service = trade_decision_service or TradeDecisionService(
            trade_quality_service=self.trade_quality_service,
        )
        self.news_engine = news_engine or NewsEngine()
        self.risk_service = risk_service or RiskService(
            risk_engine=self.risk_engine,
            portfolio_risk_service=self.portfolio_risk_service,
            anti_churn_service=self.anti_churn_service,
            execution_engine=self.execution,
        )
        self.meta_model_service = meta_model_service or MetaModelService(db=db)
        self.cloud_brain_decision_client = CloudBrainDecisionClient(
            enabled=config.CLOUD_BRAIN_DECISION_ENABLED,
            url=config.CLOUD_BRAIN_DECISION_URL,
            timeout_seconds=config.CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS,
        )
        self.research_repository = ResearchRepository(db) if db is not None else None

    def _session_allowed_for_symbol(self, symbol: str, market_symbols: list[dict]) -> bool:
        return self.market_session_guard_for_symbol(symbol, market_symbols) is None

    def market_session_guard_for_symbol(self, symbol: str, market_symbols: list[dict]) -> str | None:
        resolved = self.universe_service.resolve_requested_symbols([symbol], market_symbols)
        broker_symbol = resolved[0] if resolved else symbol
        raw = self.market_data.get_symbol_info(broker_symbol)
        if raw is None:
            return None
        tick_model = self.market_data.get_tick(broker_symbol)
        tick = tick_model.model_dump() if tick_model else None
        symbol_info = self._normalize_symbol_info(broker_symbol, tick)
        if symbol_info is None:
            return None
        profile = self.profile_service.resolve_profile(symbol, symbol_info)
        user_policy = self.risk_engine.user_policy.model_dump() if hasattr(self.risk_engine, "user_policy") else {}
        policy_sessions = list(user_policy.get("session_filters") or [])
        sessions = policy_sessions or list(profile.allowed_sessions or [])
        canonical_symbol = self.universe_service.canonical_symbol(symbol)
        market_open, market_reason = self.trade_quality_service.is_market_open(
            symbol=canonical_symbol,
            category=profile.category,
            profile_name=profile.profile_name,
        )
        if market_open:
            try:
                session_allowed = self.trade_quality_service.session_allowed(
                    sessions,
                    symbol=canonical_symbol,
                    category=profile.category,
                    profile_name=profile.profile_name,
                )
            except TypeError:
                session_allowed = self.trade_quality_service.session_allowed(sessions)
            if not session_allowed:
                return "Outside the preferred session window for this profile."
        else:
            return market_reason or f"Market is currently closed for {canonical_symbol}."

        if not tick:
            return f"No live tick available for {canonical_symbol}."
        bid = self._safe_float(tick.get("bid"), 0.0)
        ask = self._safe_float(tick.get("ask"), 0.0)
        if bid <= 0 or ask <= 0:
            return f"Market closed or no executable live bid/ask for {canonical_symbol}."
        if not self._has_recent_executable_tick(tick, max_age_seconds=self._max_session_tick_age_seconds(profile.category)):
            return f"Market data is stale for {canonical_symbol}; skipping until a fresh executable tick arrives."
        return None

    def _has_recent_executable_tick(self, tick: dict | None, *, max_age_seconds: int = 30 * 60) -> bool:
        if not tick:
            return False
        bid = self._safe_float(tick.get("bid"), 0.0)
        ask = self._safe_float(tick.get("ask"), 0.0)
        tick_time = self._safe_float(tick.get("time"), 0.0)
        if bid <= 0 or ask <= 0 or tick_time <= 0:
            return False
        return max(0.0, time.time() - tick_time) <= max_age_seconds

    def _max_session_tick_age_seconds(self, category: str) -> int:
        normalized = str(category or "").strip().lower()
        if normalized == "stocks":
            return 15 * 60
        if normalized == "indices":
            return 20 * 60
        if normalized == "commodities":
            return 45 * 60
        return 30 * 60

    def _strategy_asset_classes(self) -> set[str]:
        context = dict(getattr(self.risk_engine, "active_strategy_context", {}) or {})
        compiled = dict(context.get("compiled_strategy") or {})
        raw_classes = compiled.get("asset_classes") or context.get("asset_classes") or []
        classes = {
            self.universe_service.normalize_asset_class(item)
            for item in raw_classes
            if str(item or "").strip()
        }
        if classes:
            return classes

        for line in context.get("summary_lines") or []:
            text = str(line or "").strip()
            if not text.lower().startswith("asset classes:"):
                continue
            _, _, raw = text.partition(":")
            parsed = {
                self.universe_service.normalize_asset_class(item)
                for item in raw.split(",")
                if item.strip() and item.strip().lower() != "any active class"
            }
            if parsed:
                return parsed
        return set()

    def _symbol_universe_inactive_reason(self, symbol: str, category: str | None = None) -> str | None:
        if not self.universe_service.is_symbol_enabled(symbol):
            return self.universe_service.inactive_reason(symbol)
        if category and not self.universe_service.is_symbol_active(symbol, category):
            return self.universe_service.inactive_reason(symbol, category)
        strategy_classes = self._strategy_asset_classes()
        normalized_category = self.universe_service.normalize_asset_class(category)
        if strategy_classes and normalized_category not in strategy_classes:
            return f"{normalized_category} is inactive in the current mode."
        return None

    def set_database(self, db):
        self.db = db
        if self.meta_model_service is not None:
            self.meta_model_service.set_database(db)
        self.research_repository = ResearchRepository(db) if db is not None else None

    def _normalize_symbol_info(self, symbol: str, tick) -> Optional[NormalizedSymbolInfo]:
        canonical_symbol = self.universe_service.canonical_symbol(symbol)
        raw = self.market_data.get_symbol_info(symbol) or self.market_data.get_symbol_info(canonical_symbol)
        if raw is None:
            return None

        name = self.universe_service.canonical_symbol(raw.get("name", canonical_symbol))
        alias_name = canonical_symbol.upper()
        expected_category = self.universe_service.expected_category_for_symbol(canonical_symbol)
        desc = (raw.get("description") or "").lower()
        path = (raw.get("path") or "").lower()

        is_commodity = (
            name in {"GOLD", "WTI", "WTID", "BRENT", "NATGAS", "SILVER"}
            or alias_name in {"GOLD", "XAUUSD", "SILVER", "XAGUSD", "WTI", "WTID", "BRENT", "USOIL", "UKOIL"}
            or name.startswith(("XAU", "XAG", "XPT", "XPD"))
            or any(token in desc for token in ("gold", "silver", "oil", "crude", "brent"))
        )
        is_index = (
            name in {"US30", "US500", "SP500", "SPX500", "NAS100", "US100", "GER40", "DAX", "UK100", "FTSE100", "JPN225", "AUS200"}
            or alias_name in {"US30", "US500", "SP500", "SPX500", "NAS100", "US100", "GER40", "DAX", "UK100", "FTSE100", "JPN225", "AUS200"}
        )
        is_crypto = any(token in path for token in ("crypto", "bitcoin", "eth"))
        is_stock = any(token in path for token in ("stock", "share", "equity")) or (
            "cfd" in path and not is_commodity and not is_index
        )
        is_forex = len(name) == 6 and name.isalpha() and not is_commodity and not is_index and not is_crypto

        if expected_category == "Forex":
            category = "Forex"
        elif is_crypto:
            category = "Crypto"
        elif is_commodity:
            category = "Commodities"
        elif is_index:
            category = "Indices"
        elif is_stock:
            category = "Stocks"
        elif is_forex:
            category = "Forex"
        else:
            category = "Other"

        live_bid = self._safe_float(tick.get("bid"), 0.0) if tick else 0.0
        live_spread = self._safe_float(tick.get("spread"), 0.0) if tick else 0.0
        digits = int(self._bounded_float(raw.get("digits"), default=5.0, minimum=0.0, maximum=10.0))
        normalized = NormalizedSymbolInfo(
            name=name or raw.get("name", canonical_symbol),
            description=raw.get("description", ""),
            category=category,
            path=raw.get("path", ""),
            point=self._safe_float(raw.get("point"), 0.0),
            digits=digits,
            trade_contract_size=self._safe_float(raw.get("trade_contract_size"), 0.0),
            volume_min=self._safe_float(raw.get("volume_min"), 0.01),
            volume_max=self._safe_float(raw.get("volume_max"), 0.0),
            volume_step=self._safe_float(raw.get("volume_step"), 0.01),
            trade_stops_level=self._safe_float(raw.get("trade_stops_level"), 0.0),
            visible=raw.get("visible", False),
            trade_enabled=bool(tick and live_bid > 0),
            spread=live_spread,
        )
        return self.profile_service.enrich_symbol_info(canonical_symbol, normalized)

    def build_market_context(
        self,
        symbol: str,
        requested_timeframe: str = "H1",
        evaluation_mode: str = "manual",
        bar_count: int = 100,
    ) -> MarketContext:
        canonical_symbol = self.universe_service.canonical_symbol(symbol)
        tradeable_fetch = getattr(self.market_data, "get_tradeable_symbols_cached", None)
        visible_fetch = getattr(self.market_data, "get_visible_symbols_cached", None)
        try:
            tradeable_symbols = (
                tradeable_fetch(ttl_seconds=2.0) if callable(tradeable_fetch) else self.market_data.get_tradeable_symbols()
            )
        except Exception:
            tradeable_symbols = []
        try:
            visible_symbols = (
                visible_fetch(ttl_seconds=2.0) if callable(visible_fetch) else self.market_data.get_visible_symbols()
            )
        except Exception:
            visible_symbols = []
        market_candidates = self.universe_service.filter_market_symbols(
            tradeable_symbols or visible_symbols,
            include_inactive=True,
        )
        resolved_symbols = self.universe_service.resolve_requested_symbols(
            [symbol],
            market_candidates,
        )
        broker_symbol = resolved_symbols[0] if resolved_symbols else (canonical_symbol or symbol)
        try:
            self.market_data.enable_symbol(broker_symbol)
        except Exception:
            logger.debug("Failed to pre-enable symbol %s", broker_symbol, exc_info=True)

        try:
            tick_model = self.market_data.get_tick(broker_symbol)
            tick = tick_model.model_dump() if tick_model else None
        except Exception:
            tick = None
        # Symbol-level live-feed fallback: if the first alias has no tick,
        # try preferred broker aliases (important for SILVER/XAGUSD mapping).
        if tick is None:
            preferred_aliases = [
                alias
                for alias in self.universe_service.preferred_broker_names(canonical_symbol or symbol)
                if str(alias or "").strip()
            ]
            if broker_symbol not in preferred_aliases:
                preferred_aliases.append(broker_symbol)
            fallback_candidates = self.universe_service.resolve_requested_symbols(
                preferred_aliases,
                market_candidates,
            )
            for candidate_symbol in fallback_candidates:
                if candidate_symbol == broker_symbol:
                    continue
                try:
                    self.market_data.enable_symbol(candidate_symbol)
                    candidate_tick_model = self.market_data.get_tick(candidate_symbol)
                    candidate_tick = candidate_tick_model.model_dump() if candidate_tick_model else None
                except Exception:
                    candidate_tick = None
                if candidate_tick is None:
                    continue
                bid = self._safe_float(candidate_tick.get("bid"), 0.0)
                ask = self._safe_float(candidate_tick.get("ask"), 0.0)
                if bid <= 0 or ask <= 0:
                    continue
                broker_symbol = candidate_symbol
                tick = candidate_tick
                break
        account = self.connector.refresh_account()
        all_positions = [p.model_dump() for p in self.execution.get_positions()]
        symbol_positions = [p for p in all_positions if self.universe_service.canonical_symbol(p.get("symbol")) == canonical_symbol]

        counts = max(bar_count, 100)
        bars_by_tf = {
            "M15": [b.model_dump() for b in self.market_data.get_bars(broker_symbol, "M15", counts)],
            "H1": [b.model_dump() for b in self.market_data.get_bars(broker_symbol, "H1", counts)],
            "H4": [b.model_dump() for b in self.market_data.get_bars(broker_symbol, "H4", counts)],
        }

        degraded = []
        for timeframe, bars in bars_by_tf.items():
            if not bars:
                degraded.append(f"{timeframe} bars unavailable")
        if tick is None:
            degraded.append("Tick data unavailable")
        symbol_info = self._normalize_symbol_info(broker_symbol, tick)
        profile = self.profile_service.resolve_profile(canonical_symbol or broker_symbol, symbol_info) if symbol_info else None
        inactive_reason = self._symbol_universe_inactive_reason(
            canonical_symbol or broker_symbol,
            symbol_info.category if symbol_info else None,
        )
        if inactive_reason:
            degraded.append(inactive_reason)

        return MarketContext(
            symbol=broker_symbol,
            requested_timeframe=requested_timeframe,
            evaluation_mode=evaluation_mode,
            user_policy=self.risk_engine.user_policy.model_dump() if hasattr(self.risk_engine, "user_policy") else None,
            strategy_context=dict(getattr(self.risk_engine, "active_strategy_context", {}) or {}),
            symbol_info=symbol_info,
            profile=profile,
            tick=tick,
            account_balance=account.balance if account else 0.0,
            account_equity=account.equity if account else 0.0,
            account_margin=account.margin if account else 0.0,
            account_free_margin=account.free_margin if account else 0.0,
            account_currency=account.currency if account else "",
            account_leverage=account.leverage if account else 0,
            bars_by_timeframe=bars_by_tf,
            symbol_open_positions=symbol_positions,
            all_open_positions=all_positions,
            degraded_reasons=degraded,
        )

    def _inactive_universe_decision(
        self,
        context: MarketContext,
        requested_agent_name: str,
        reason: str,
    ) -> ExecutionDecision:
        hold_signal = TechnicalSignal(
            agent_name="UniverseGuard",
            action="HOLD",
            confidence=0.0,
            reason=reason,
            strategy="inactive_universe",
            metadata={"blocked_by": "symbol_universe"},
        )
        trade_quality = self.trade_quality_service.assess(
            context=context,
            signal=hold_signal,
            gemini_assessment=None,
            portfolio_fit_score=0.0,
        )
        risk_evaluation = RiskEvaluation(
            approved=False,
            reason=reason,
            adjusted_volume=0.0,
            warnings=[],
            mode=context.evaluation_mode,
            status="block",
            machine_reasons=[reason],
            metrics_snapshot={"blocked_by": "symbol_universe"},
        )
        portfolio_risk = PortfolioRiskAssessment(
            status="block",
            allow_execute=False,
            reason=reason,
            blocking_reasons=[reason],
            warnings=[],
            metrics_snapshot={"blocked_by": "symbol_universe"},
        )
        anti_churn = AntiChurnAssessment(blocked=False)
        position_management_plan = PositionManagementPlan(
            manage_position=False,
            strategy="inactive_universe",
            initial_thesis=reason,
            time_stop_rule="No trade opened.",
            notes=["Trading blocked before technical evaluation because the symbol is outside the active universe."],
            metadata={"blocked_by": "symbol_universe"},
        )
        signal_decision = SignalDecision(
            requested_agent_name=requested_agent_name,
            primary_agent_name="UniverseGuard",
            final_agent_name="UniverseGuard",
            market_context=context,
            primary_signal=hold_signal,
            final_signal=hold_signal,
            gemini_confirmation=None,
            degraded_reasons=list(dict.fromkeys(context.degraded_reasons + [reason])),
        )
        return ExecutionDecision(
            allow_execute=False,
            reason=reason,
            signal_decision=signal_decision,
            trade_decision_assessment=TradeDecisionAssessment(
                trade=False,
                final_direction="HOLD",
                final_signal=hold_signal,
                trade_quality_assessment=trade_quality,
                reasons=[reason],
            ),
            risk_evaluation=risk_evaluation,
            trade_quality_assessment=trade_quality,
            portfolio_risk_assessment=portfolio_risk,
            anti_churn_assessment=anti_churn,
            position_management_plan=position_management_plan,
            entry_price=0.0,
        )

    def _resolve_primary_agent_name(self, requested_agent_name: Optional[str]) -> str:
        if not requested_agent_name:
            return "GeminiAgent"
        return requested_agent_name

    def _resolve_baseline_agent_name(self) -> str:
        if "SmartAgent" in self.agents:
            return "SmartAgent"
        for name in self.agents:
            if name != "GeminiAgent":
                return name
        raise ValueError("No baseline technical agent is configured.")

    def _should_use_trade_brain(
        self,
        *,
        requested_agent_name: Optional[str],
        primary_agent_name: str,
        context: MarketContext,
    ) -> bool:
        # Keep auto/scan loops resilient when Gemini quotas are tight.
        # Bulk loops should prioritize deterministic baseline continuity.
        if str(context.evaluation_mode or "").strip().lower() in {"auto", "scan"}:
            return False
        if primary_agent_name != "GeminiAgent":
            return False
        gemini_role = str((context.user_policy or {}).get("gemini_role", "advisory")).lower()
        if gemini_role == "off":
            return False
        return bool(getattr(self.gemini_trade_brain_service, "available", False))

    def _signal_from_trade_brain(
        self,
        *,
        context: MarketContext,
        baseline_signal: TechnicalSignal,
        assessment: dict[str, Any],
    ) -> TechnicalSignal:
        action = str(assessment.get("action", baseline_signal.action or "HOLD")).strip().upper()
        stop_loss = assessment.get("stop_loss_price", baseline_signal.stop_loss)
        take_profit = assessment.get("take_profit_price", baseline_signal.take_profit)
        target_sanitization = "accepted"
        if action not in {"BUY", "SELL", "HOLD"}:
            action = "HOLD"
            stop_loss = None
            take_profit = None
            target_sanitization = "blocked_unknown_action"
        if action in {"BUY", "SELL"}:
            min_rr = self._safe_float((context.user_policy or {}).get("min_reward_risk"), 0.0)
            if min_rr <= 0:
                min_rr = self._safe_float(context.profile.min_reward_risk, 1.8) if context.profile else 1.8
            entry = self._entry_price(context, action)
            spread_points, point = self._spread_cost_inputs(context)
            assessment_targets_ok = self._targets_meet_reward_risk(
                entry_price=entry,
                action=action,
                stop_loss=stop_loss,
                take_profit=take_profit,
                min_reward_risk=min_rr,
                spread_points=spread_points,
                point=point,
            )
            if not assessment_targets_ok:
                baseline_entry = self._entry_price(context, baseline_signal.action)
                baseline_targets_ok = (
                    baseline_signal.action == action
                    and self._targets_meet_reward_risk(
                        entry_price=baseline_entry,
                        action=baseline_signal.action,
                        stop_loss=baseline_signal.stop_loss,
                        take_profit=baseline_signal.take_profit,
                        min_reward_risk=min_rr,
                        spread_points=spread_points,
                        point=point,
                    )
                )
                if baseline_targets_ok:
                    stop_loss = baseline_signal.stop_loss
                    take_profit = baseline_signal.take_profit
                    target_sanitization = "rejected_low_reward_risk"
                else:
                    action = "HOLD"
                    stop_loss = None
                    take_profit = None
                    target_sanitization = "blocked_low_reward_risk"

        metadata = dict(baseline_signal.metadata or {})
        baseline_confidence = self._safe_float(baseline_signal.confidence, 0.0)
        metadata["smart_agent_baseline"] = {
            "action": baseline_signal.action,
            "confidence": baseline_confidence,
            "stop_loss": baseline_signal.stop_loss,
            "take_profit": baseline_signal.take_profit,
            "max_holding_minutes": baseline_signal.max_holding_minutes,
            "reason": baseline_signal.reason,
            "strategy": baseline_signal.strategy,
        }
        metadata["gemini_trade_brain"] = {
            "used": bool(assessment.get("used")),
            "available": bool(assessment.get("available", False)),
            "degraded": bool(assessment.get("degraded", False)),
            "market_regime": assessment.get("market_regime", "unclear"),
            "risk_note": assessment.get("risk_note", ""),
            "target_sanitization": target_sanitization,
            "raw_payload": assessment.get("raw_payload", {}),
        }
        signal_confidence = self._bounded_float(
            assessment.get("confidence"),
            default=baseline_confidence,
            minimum=0.0,
            maximum=0.95,
        )
        baseline_hold_minutes = self._safe_float(baseline_signal.max_holding_minutes, 120.0)
        hold_minutes = int(
            self._bounded_float(
                assessment.get("max_hold_minutes"),
                default=baseline_hold_minutes,
                minimum=30.0,
                maximum=720.0,
            )
        )
        return TechnicalSignal(
            agent_name="GeminiAgent",
            action=action,
            confidence=signal_confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_holding_minutes=hold_minutes,
            reason=str(assessment.get("summary_reason", "") or baseline_signal.reason or "Gemini primary trade analysis completed.").strip(),
            strategy=str(assessment.get("strategy", "") or baseline_signal.strategy or "gemini_primary_brain").strip(),
            metadata=metadata,
        )

    def _targets_meet_reward_risk(
        self,
        *,
        entry_price: float,
        action: str,
        stop_loss: Any,
        take_profit: Any,
        min_reward_risk: float,
        spread_points: float = 0.0,
        point: float = 0.0,
    ) -> bool:
        normalized_action = str(action or "").strip().upper()
        try:
            entry = float(entry_price)
            sl = float(stop_loss)
            tp = float(take_profit)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(entry) and math.isfinite(sl) and math.isfinite(tp)):
            return False
        if entry <= 0 or sl <= 0 or tp <= 0:
            return False
        if normalized_action == "BUY" and not (sl < entry < tp):
            return False
        if normalized_action == "SELL" and not (tp < entry < sl):
            return False
        if normalized_action not in {"BUY", "SELL"}:
            return False
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        parsed_spread_points = self._safe_float(spread_points, float("inf"))
        parsed_point = self._safe_float(point, 0.0)
        if not math.isfinite(parsed_spread_points):
            return False
        spread_cost = max(0.0, parsed_spread_points) * parsed_point if parsed_point > 0 else 0.0
        if not math.isfinite(spread_cost):
            return False
        adjusted_risk = risk + spread_cost
        adjusted_reward = max(0.0, reward - spread_cost)
        if not (math.isfinite(adjusted_risk) and math.isfinite(adjusted_reward)):
            return False
        if adjusted_risk <= 0:
            return False
        return (adjusted_reward / adjusted_risk) >= max(0.8, self._safe_float(min_reward_risk, 1.8))

    def _spread_cost_inputs(self, context: MarketContext) -> tuple[float, float]:
        tick = context.tick or {}
        point = self._safe_float(context.symbol_info.point, 0.0) if context.symbol_info and context.symbol_info.point else 0.0
        raw_spread = tick.get("spread", 0.0)
        spread_points = self._safe_float(raw_spread, float("inf"))
        if not math.isfinite(spread_points):
            return float("inf"), point if point > 0 else 1.0
        return spread_points, point

    def _max_tick_age_seconds(self, context: MarketContext) -> int:
        category = (context.symbol_info.category if context.symbol_info else "").lower()
        if category == "stocks":
            return 600
        if category in {"commodities", "indices"}:
            return 900
        return 1200

    def _market_live_guard_reason(self, context: MarketContext) -> str | None:
        category = (context.symbol_info.category if context.symbol_info else "").lower()
        # Explicit weekend guard for non-24/7 markets to avoid showing tradable-looking
        # confidence while venues are closed.
        if category in {"stocks", "indices", "commodities"}:
            utc_weekday = datetime.now(timezone.utc).weekday()  # 5=Sat, 6=Sun
            if utc_weekday in {5, 6}:
                return f"Market closed for weekend ({category.title()})."

        if context.symbol_info and not context.symbol_info.trade_enabled:
            return "Market closed or trading disabled for this symbol."
        if not context.tick:
            return "No live tick available for this symbol."

        bid = self._safe_float(context.tick.get("bid"), 0.0)
        ask = self._safe_float(context.tick.get("ask"), 0.0)
        tick_time = self._safe_float(context.tick.get("time"), 0.0)
        if bid <= 0 or ask <= 0:
            return "Market closed (no executable live bid/ask)."
        if tick_time <= 0:
            return "Market data timestamp unavailable."

        age_seconds = max(0.0, time.time() - tick_time)
        max_age = self._max_tick_age_seconds(context)
        if age_seconds > max_age:
            # Broker feeds can pause/lag even while order routing remains executable.
            # If we have valid bid/ask, avoid false "market closed" blocks for moderate
            # delays and let deterministic spread/risk gates decide.
            executable_delay_tolerance_seconds = 4 * 60 * 60  # 4 hours
            if age_seconds <= executable_delay_tolerance_seconds:
                return None
            if category in {"stocks", "indices", "commodities"}:
                return (
                    f"No fresh executable tick for {context.symbol} "
                    f"(last tick {int(age_seconds)}s ago). Market data may be delayed or the symbol is illiquid."
                )
            return (
                f"Market appears closed or stale for {context.symbol} "
                f"(last tick {int(age_seconds)}s ago)."
            )
        return None

    def _build_cloud_decision_payload(
        self,
        *,
        context: MarketContext,
        trade_decision: TradeDecisionAssessment,
        gemini_assessment: GeminiAssessment | None,
        meta_assessment: dict,
    ) -> dict:
        final_signal = trade_decision.final_signal
        qa = trade_decision.trade_quality_assessment
        payload = {
            "symbol": context.symbol,
            "timeframe": context.requested_timeframe,
            "evaluation_mode": context.evaluation_mode,
            "user_policy": context.user_policy or {},
            "signal": {
                "action": final_signal.action,
                "confidence": self._safe_float(final_signal.confidence, 0.0),
                "strategy": final_signal.strategy,
                "reason": final_signal.reason,
                "stop_loss": final_signal.stop_loss,
                "take_profit": final_signal.take_profit,
            },
            "quality": {
                "score": self._safe_float(qa.final_trade_quality_score, 0.0),
                "threshold": self._safe_float(qa.threshold, 0.0),
                "projected_margin_after_costs_pct": self._safe_float(
                    getattr(
                        qa,
                        "projected_margin_after_costs_pct",
                        getattr(qa, "projected_margin_utilization_pct", 0.0),
                    ),
                    0.0,
                ),
                "no_trade_zone": bool(qa.no_trade_zone),
                "no_trade_reasons": list(qa.no_trade_reasons or []),
            },
            "gemini": gemini_assessment.model_dump() if gemini_assessment else None,
            "meta_model": dict(meta_assessment or {}),
            "market": {
                "tick": context.tick or {},
                "symbol_category": context.symbol_info.category if context.symbol_info else "Other",
                "degraded_reasons": list(context.degraded_reasons or []),
            },
        }
        return _json_safe(payload)

    def _apply_cloud_decision(
        self,
        *,
        trade_decision: TradeDecisionAssessment,
        cloud_decision: dict,
    ) -> tuple[TradeDecisionAssessment, dict]:
        if not cloud_decision.get("used"):
            return trade_decision, cloud_decision

        requested_action = str(cloud_decision.get("action", "")).upper()
        final_signal = trade_decision.final_signal.model_copy(deep=True)
        cloud_reason = str(cloud_decision.get("reason", "cloud_brain_applied")).strip()
        cloud_reason_lower = cloud_reason.lower()
        raw_cloud_confidence = cloud_decision.get("confidence")
        cloud_confidence = None
        if raw_cloud_confidence is not None:
            parsed_cloud_confidence = self._safe_float(raw_cloud_confidence, default=float("nan"))
            if math.isfinite(parsed_cloud_confidence):
                cloud_confidence = parsed_cloud_confidence
        local_quality = trade_decision.trade_quality_assessment
        local_quality_score = self._safe_float(getattr(local_quality, "final_trade_quality_score", 0.0), 0.0)
        local_quality_threshold = self._safe_float(getattr(local_quality, "threshold", 0.0), 0.0)
        local_is_strong_setup = (
            final_signal.action in {"BUY", "SELL"}
            and not bool(local_quality.no_trade_zone)
            and local_quality_score >= local_quality_threshold
        )
        mode = str((trade_decision.final_signal.metadata or {}).get("cloud_brain", {}).get("mode", "")).lower()
        if not mode:
            mode = ""
        category = str((trade_decision.final_signal.metadata or {}).get("category", "")).lower()
        generic_cloud_hold = requested_action == "HOLD" and any(
            phrase in cloud_reason_lower
            for phrase in (
                "meta-model no-trade probability is high",
                "meta-model caution",
                "no-trade zone from local quality gate",
            )
        )
        preserve_local_setup = (
            generic_cloud_hold
            and local_is_strong_setup
            and (category == "stocks" or mode == "aggressive")
        )

        # Safety rule: opposite-direction cloud command is converted to HOLD.
        if requested_action in {"BUY", "SELL"} and requested_action != final_signal.action:
            final_signal.action = "HOLD"
            final_signal.reason = f"{final_signal.reason} Cloud brain opposed local direction; holding for safety."
            if cloud_confidence is not None:
                final_signal.confidence = max(0.0, min(0.95, cloud_confidence))
            cloud_decision["applied_action"] = "HOLD"
        elif preserve_local_setup:
            if cloud_reason:
                final_signal.reason = f"{final_signal.reason} Cloud brain caution: {cloud_reason}"
            cloud_decision["applied_action"] = final_signal.action
            cloud_decision["advisory_only"] = True
        elif requested_action in {"BUY", "SELL", "HOLD"}:
            final_signal.action = requested_action
            if cloud_confidence is not None:
                final_signal.confidence = max(0.0, min(0.95, cloud_confidence))
            if cloud_reason:
                final_signal.reason = f"{final_signal.reason} Cloud brain: {cloud_reason}"
            cloud_decision["applied_action"] = requested_action
        else:
            cloud_decision["applied_action"] = final_signal.action

        meta = dict(final_signal.metadata or {})
        meta["cloud_brain"] = cloud_decision
        final_signal.metadata = meta

        new_trade_flag = final_signal.action in {"BUY", "SELL"}
        updated = trade_decision.model_copy(update={"final_signal": final_signal, "trade": new_trade_flag})
        return updated, cloud_decision

    def _cloud_decision_timeout_seconds(self, evaluation_mode: str) -> float:
        normalized = str(evaluation_mode or "").strip().lower()
        if normalized == "scan":
            # Dashboard scans need partial answers quickly; cloud advice is optional here.
            return 0.45
        if normalized == "auto":
            # Autonomous loops should prefer continuity over waiting on the cloud overlay.
            return 1.0
        return self._safe_float(self.cloud_brain_decision_client.timeout_seconds, 6.0)

    def _strategy_advisor_timeout_seconds(self, evaluation_mode: str) -> float | None:
        normalized = str(evaluation_mode or "").strip().lower()
        if normalized == "scan":
            return 1.2
        if normalized == "auto":
            return 1.8
        return None

    def _meta_model_timeout_seconds(self, evaluation_mode: str) -> float | None:
        normalized = str(evaluation_mode or "").strip().lower()
        if normalized == "scan":
            return 0.9
        if normalized == "auto":
            return 1.2
        return None

    async def _assess_strategy_advisory(
        self,
        *,
        context: MarketContext,
        technical_signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        event_context: dict,
        evaluation_mode: str,
    ) -> dict:
        fallback = {
            "used": False,
            "available": bool(getattr(self.gemini_strategy_advisor_service, "available", False)),
            "degraded": True,
            "summary_reason": "Strategy advisor timed out.",
        }
        try:
            timeout_seconds = self._strategy_advisor_timeout_seconds(evaluation_mode)
            assessment = self.gemini_strategy_advisor_service.assess(
                context=context,
                technical_signal=technical_signal,
                gemini_assessment=gemini_assessment,
                event_context=event_context,
            )
            if timeout_seconds is None:
                return await assessment
            return await asyncio.wait_for(assessment, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.info(
                "Strategy advisor skipped for %s in %s mode after timeout.",
                context.symbol,
                evaluation_mode,
            )
            return fallback
        except Exception as exc:
            logger.debug("Strategy advisor failed for %s: %s", context.symbol, exc)
            return {
                **fallback,
                "summary_reason": f"Strategy advisor failed: {exc}",
            }

    async def _assess_trade_decision_with_meta_model(
        self,
        *,
        context: MarketContext,
        trade_decision: TradeDecisionAssessment,
        gemini_assessment: GeminiAssessment | None,
        portfolio_risk_assessment: PortfolioRiskAssessment,
        anti_churn_blocked: bool,
    ) -> tuple[TradeDecisionAssessment, dict]:
        if self.meta_model_service is None:
            return trade_decision, {
                "active": False,
                "changed_decision": False,
                "blocked": False,
                "profit_probability": 0.0,
                "expected_edge": 0.0,
                "no_trade_probability": 1.0,
                "reason": "not_configured",
            }

        bulk_mode = str(context.evaluation_mode or "").strip().lower() in {"scan", "auto"}
        refresh_model = not bulk_mode
        if bulk_mode and not self.meta_model_service.is_active:
            # Keep bulk loops fast, but periodically re-check whether an approved
            # freshly-trained model became available.
            try:
                await asyncio.wait_for(self.meta_model_service.refresh_active_model(force=True), timeout=1.5)
            except Exception:
                pass
            if not self.meta_model_service.is_active:
                return trade_decision, {
                    "active": False,
                    "changed_decision": False,
                    "blocked": False,
                    "profit_probability": 0.0,
                    "expected_edge": 0.0,
                    "no_trade_probability": 1.0,
                    "reason": "meta_model_skipped_in_bulk_mode",
                }

        timeout_seconds = self._meta_model_timeout_seconds(context.evaluation_mode)
        try:
            assessment = self.meta_model_service.assess_trade_decision(
                context=context,
                trade_decision=trade_decision,
                gemini_assessment=gemini_assessment,
                portfolio_risk_assessment=portfolio_risk_assessment,
                anti_churn_blocked=anti_churn_blocked,
                refresh_model=refresh_model,
            )
            if timeout_seconds is None:
                return await assessment
            return await asyncio.wait_for(assessment, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.info(
                "Meta-model skipped for %s in %s mode after %.1fs timeout.",
                context.symbol,
                context.evaluation_mode,
                timeout_seconds,
            )
            return trade_decision, {
                "active": False,
                "changed_decision": False,
                "blocked": False,
                "profit_probability": 0.0,
                "expected_edge": 0.0,
                "no_trade_probability": 1.0,
                "reason": "meta_model_timeout",
            }
        except Exception as exc:
            logger.debug("Meta-model assessment failed for %s: %s", context.symbol, exc)
            return trade_decision, {
                "active": False,
                "changed_decision": False,
                "blocked": False,
                "profit_probability": 0.0,
                "expected_edge": 0.0,
                "no_trade_probability": 1.0,
                "reason": f"meta_model_failed: {exc}",
            }

    def _market_unavailable_decision(
        self,
        context: MarketContext,
        requested_agent_name: str,
        reason: str,
    ) -> ExecutionDecision:
        hold_signal = TechnicalSignal(
            agent_name="MarketGuard",
            action="HOLD",
            confidence=0.0,
            reason=reason,
            strategy="market_unavailable",
            metadata={"blocked_by": "market_live_guard"},
        )
        trade_quality = self.trade_quality_service.assess(
            context=context,
            signal=hold_signal,
            gemini_assessment=None,
            portfolio_fit_score=0.0,
        )
        risk_evaluation = RiskEvaluation(
            approved=False,
            reason=reason,
            adjusted_volume=0.0,
            warnings=[],
            mode=context.evaluation_mode,
            status="block",
            machine_reasons=[reason],
            metrics_snapshot={"blocked_by": "market_live_guard"},
        )
        portfolio_risk = PortfolioRiskAssessment(
            status="block",
            allow_execute=False,
            reason=reason,
            blocking_reasons=[reason],
            warnings=[],
            metrics_snapshot={"blocked_by": "market_live_guard"},
        )
        anti_churn = AntiChurnAssessment(blocked=False)
        position_management_plan = PositionManagementPlan(
            manage_position=False,
            strategy="market_unavailable",
            initial_thesis=reason,
            time_stop_rule="No trade opened.",
            notes=["Trading blocked before signal generation because fresh executable market data was unavailable."],
            metadata={"blocked_by": "market_live_guard"},
        )
        signal_decision = SignalDecision(
            requested_agent_name=requested_agent_name,
            primary_agent_name="MarketGuard",
            final_agent_name="MarketGuard",
            market_context=context,
            primary_signal=hold_signal,
            final_signal=hold_signal,
            gemini_confirmation=None,
            degraded_reasons=list(dict.fromkeys(context.degraded_reasons + [reason])),
        )
        return ExecutionDecision(
            allow_execute=False,
            reason=reason,
            signal_decision=signal_decision,
            trade_decision_assessment=TradeDecisionAssessment(
                trade=False,
                final_direction="HOLD",
                final_signal=hold_signal,
                trade_quality_assessment=trade_quality,
                reasons=[reason],
            ),
            risk_evaluation=risk_evaluation,
            trade_quality_assessment=trade_quality,
            portfolio_risk_assessment=portfolio_risk,
            anti_churn_assessment=anti_churn,
            position_management_plan=position_management_plan,
            entry_price=0.0,
        )

    def _should_use_gemini(
        self,
        requested_agent_name: Optional[str],
        evaluation_mode: str,
        context: MarketContext | None = None,
    ) -> bool:
        user_policy = (context.user_policy or {}) if context is not None else {}
        gemini_role = str(user_policy.get("gemini_role", "advisory")).lower()
        if gemini_role == "off":
            return False
        if requested_agent_name == "GeminiAgent":
            return True
        # Protect free-tier Gemini quota: avoid per-symbol Gemini calls in bulk
        # scan/auto loops unless the user explicitly requires confirmation mode.
        if evaluation_mode in {"scan", "auto"}:
            return False
        if gemini_role == "confirmation-required":
            return True
        return evaluation_mode in {"manual", "replay"}

    async def _assess_news(
        self,
        context: MarketContext,
        input_data: AgentInput,
        primary_signal: TechnicalSignal,
    ) -> GeminiAssessment | None:
        if self.gemini_news_analysis_service is not None:
            return await self.gemini_news_analysis_service.assess(
                context=context,
                technical_signal=primary_signal,
                normalized_news=context.normalized_news,
            )
        if self.gemini_adapter is not None and hasattr(self.gemini_adapter, "confirm"):
            return await self.gemini_adapter.confirm(input_data, primary_signal)
        return None

    def _extract_event_context(self, context: MarketContext, signal: TechnicalSignal) -> dict:
        if not context.normalized_news and "news_data_unavailable" in (context.degraded_reasons or []):
            snapshot = self.news_engine.unavailable_snapshot("Decision made without news data.")
        else:
            snapshot = self.news_engine.analyze(context=context, signal=signal)
        event_ids: list[str] = []
        for item in context.normalized_news[:12]:
            meta = item.metadata or {}
            event_id = str(meta.get("external_event_id", "") or "")
            if event_id:
                event_ids.append(event_id)
        news_items = int(self._bounded_float(snapshot.get("items_count"), default=0.0, minimum=0.0, maximum=1000.0))
        return {
            "news_items": news_items,
            "bias": str(snapshot.get("sentiment", "neutral")),
            "event_risk": str(snapshot.get("impact", "low")),
            "confidence_adjustment": self._safe_float(snapshot.get("confidence_adjustment"), 0.0),
            "contradiction_flag": bool(snapshot.get("sentiment") not in {"neutral", "bullish" if signal.action == "BUY" else "bearish"}),
            "event_ids": list(dict.fromkeys(event_ids)),
            "news": snapshot,
        }

    def _apply_event_context_advisory(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
    ) -> TechnicalSignal:
        event_ctx = self._extract_event_context(context, signal)
        adjusted = signal.model_copy(deep=True)
        news = dict(event_ctx.get("news") or {})
        adjusted.confidence = round(
            max(
                0.0,
                min(
                    0.95,
                    self._safe_float(adjusted.confidence, 0.0)
                    + self._safe_float(event_ctx.get("confidence_adjustment"), 0.0),
                ),
            ),
            2,
        )
        meta = dict(adjusted.metadata or {})
        meta["event_context"] = event_ctx
        meta["news"] = news
        adjusted.metadata = meta
        if event_ctx.get("news_items", 0) <= 0:
            if news.get("news_unavailable"):
                adjusted.reason = f"{adjusted.reason} Decision made without news data."
            return adjusted
        if bool(news.get("recommend_hold")) and adjusted.action in {"BUY", "SELL"}:
            adjusted.action = "HOLD"
            if bool(news.get("execution_block")):
                adjusted.reason = f"{adjusted.reason} Trade blocked due to high-impact news risk."
            else:
                adjusted.reason = f"{adjusted.reason} Trade avoided due to high-impact news."
            return adjusted
        if event_ctx.get("contradiction_flag"):
            adjusted.reason = f"{adjusted.reason} News contradicts the setup."
        elif event_ctx.get("bias") in {"bullish", "bearish"}:
            adjusted.reason = f"{adjusted.reason} News supports this decision."
        return adjusted

    async def _load_news_for_context(self, context: MarketContext, *, evaluation_mode: str) -> MarketContext:
        updated = context
        mode = str(evaluation_mode or "").strip().lower()
        # Low-latency scan mode: avoid news/event refresh lookups that can
        # dominate per-symbol latency during auto-trade loops.
        if mode in {"auto_fast", "scan_fast"}:
            degraded = list(updated.degraded_reasons or [])
            degraded.append("news_skipped_low_latency")
            return updated.model_copy(update={"degraded_reasons": list(dict.fromkeys(degraded))})
        try:
            if self.event_ingestion_service is not None:
                if mode not in {"auto", "scan"}:
                    await self.event_ingestion_service.maybe_refresh_latest(
                        min_interval_seconds=900.0,
                        classify_with_gemini=False,
                    )
                stored_news = await asyncio.wait_for(
                    self.event_ingestion_service.recent_symbol_news(
                        context.symbol,
                        universe_service=self.universe_service,
                        limit=10,
                    ),
                    timeout=1.0 if mode in {"auto", "scan"} else 4.0,
                )
                if stored_news:
                    updated = updated.model_copy(update={"normalized_news": stored_news})
            if not updated.normalized_news and mode not in {"auto", "scan"}:
                normalized_news = await asyncio.wait_for(
                    self.news_ingestion_service.ingest_for_context(updated),
                    timeout=4.0,
                )
                if normalized_news:
                    updated = updated.model_copy(update={"normalized_news": normalized_news})
        except Exception as exc:
            degraded = list(updated.degraded_reasons or [])
            degraded.append("news_skipped_low_latency" if mode in {"auto", "scan"} else "news_data_unavailable")
            updated = updated.model_copy(update={"degraded_reasons": list(dict.fromkeys(degraded))})
            logger.debug("News load failed for %s: %s", context.symbol, exc)
        return updated

    def _should_use_strategy_advisor(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        evaluation_mode: str,
    ) -> bool:
        # Strategy advisory is optional; disable it for bulk loops to reduce
        # quota pressure and avoid cascading per-symbol timeouts.
        if str(evaluation_mode or "").strip().lower() in {"auto", "scan"}:
            return False
        if signal.action not in {"BUY", "SELL"}:
            return False
        if signal.confidence < 0.52:
            return False
        policy = context.user_policy or {}
        if str(policy.get("gemini_role", "advisory")).lower() == "off":
            return False
        return True

    def _clamp_strategy_advisory(self, context: MarketContext, advisory: dict) -> dict:
        mode = str((context.user_policy or {}).get("mode", "balanced")).lower()
        mode_bounds = {
            "safe": {"sl_min": 0.9, "sl_max": 1.8, "tp_min": 1.8, "tp_max": 3.0, "hold_max": 180},
            "balanced": {"sl_min": 0.9, "sl_max": 2.2, "tp_min": 1.8, "tp_max": 3.6, "hold_max": 240},
            "aggressive": {"sl_min": 0.8, "sl_max": 2.6, "tp_min": 1.6, "tp_max": 4.2, "hold_max": 360},
        }.get(mode, {"sl_min": 0.9, "sl_max": 2.2, "tp_min": 1.8, "tp_max": 3.6, "hold_max": 240})
        min_rr = self._safe_float((context.user_policy or {}).get("min_reward_risk"), 1.8)

        clamped = dict(advisory or {})
        clamped["sl_atr_multiplier"] = self._bounded_float(
            clamped.get("sl_atr_multiplier"),
            default=1.5,
            minimum=mode_bounds["sl_min"],
            maximum=mode_bounds["sl_max"],
        )
        clamped["tp_atr_multiplier"] = self._bounded_float(
            clamped.get("tp_atr_multiplier"),
            default=2.5,
            minimum=mode_bounds["tp_min"],
            maximum=mode_bounds["tp_max"],
        )
        clamped["max_hold_minutes"] = int(
            self._bounded_float(
                clamped.get("max_hold_minutes"),
                default=120.0,
                minimum=30.0,
                maximum=float(mode_bounds["hold_max"]),
            )
        )
        clamped["confidence_adjustment"] = self._bounded_float(
            clamped.get("confidence_adjustment"),
            default=0.0,
            minimum=-0.08,
            maximum=0.05,
        )
        # Never let advisory reduce configured minimum reward:risk discipline.
        if clamped["tp_atr_multiplier"] < clamped["sl_atr_multiplier"] * min_rr:
            clamped["tp_atr_multiplier"] = round(clamped["sl_atr_multiplier"] * min_rr, 3)
        return clamped

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return float(default)
        if not math.isfinite(parsed):
            return float(default)
        return parsed

    def _bounded_float(self, value: Any, *, default: float, minimum: float, maximum: float) -> float:
        parsed = self._safe_float(value, default)
        return max(float(minimum), min(float(maximum), parsed))

    def _apply_strategy_advisory(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        advisory: dict,
    ) -> TechnicalSignal:
        adjusted = signal.model_copy(deep=True)
        action = str(adjusted.action or "").strip().upper()
        if action in {"BUY", "SELL"}:
            adjusted.action = action
        elif action and action != "HOLD":
            adjusted.action = "HOLD"
            adjusted.stop_loss = None
            adjusted.take_profit = None
        clamped = self._clamp_strategy_advisory(context, advisory)

        base_confidence = self._safe_float(adjusted.confidence, 0.0)
        confidence_adjustment = self._safe_float(clamped.get("confidence_adjustment"), 0.0)
        adjusted.confidence = round(
            max(0.0, min(0.95, base_confidence + confidence_adjustment)),
            2,
        )

        metadata = dict(adjusted.metadata or {})
        metadata["strategy_advisory"] = clamped
        if action and action not in {"BUY", "SELL", "HOLD"}:
            metadata["strategy_advisory"] = {
                **clamped,
                "target_sanitization": "blocked_unknown_action",
            }
        adjusted.metadata = metadata

        if adjusted.action not in {"BUY", "SELL"}:
            return adjusted
        if not context.tick:
            return adjusted

        entry = self._entry_price(context, action)
        if entry <= 0:
            return adjusted
        if adjusted.stop_loss is None or adjusted.take_profit is None:
            adjusted.max_holding_minutes = int(clamped.get("max_hold_minutes", adjusted.max_holding_minutes or 120))
            return adjusted

        current_sl = self._safe_float(adjusted.stop_loss, 0.0)
        current_tp = self._safe_float(adjusted.take_profit, 0.0)
        current_sl_dist = abs(entry - current_sl) if current_sl > 0 else 0.0
        current_tp_dist = abs(current_tp - entry) if current_tp > 0 else 0.0
        atr = self._safe_float(metadata.get("atr"), 0.0)
        if atr <= 0:
            atr = current_sl_dist

        explicit_sl = advisory.get("stop_loss_price")
        explicit_tp = advisory.get("take_profit_price")
        min_rr = self._safe_float((context.user_policy or {}).get("min_reward_risk"), 1.8)
        spread_points, point = self._spread_cost_inputs(context)
        explicit_targets_valid = self._targets_meet_reward_risk(
            entry_price=entry,
            action=action,
            stop_loss=explicit_sl,
            take_profit=explicit_tp,
            min_reward_risk=min_rr,
            spread_points=spread_points,
            point=point,
        )
        if explicit_targets_valid:
            sl_dist = abs(entry - float(explicit_sl))
            tp_dist = abs(float(explicit_tp) - entry)
        else:
            target_sl_dist = atr * float(clamped.get("sl_atr_multiplier", 1.5))
            target_tp_dist = atr * float(clamped.get("tp_atr_multiplier", 2.5))

            # Keep advisory conservative: do not loosen stop distance materially when
            # falling back to multiplier-based targets.
            max_sl_dist = current_sl_dist * 1.05 if current_sl_dist > 0 else target_sl_dist
            min_sl_dist = current_sl_dist * 0.70 if current_sl_dist > 0 else target_sl_dist
            sl_dist = max(min_sl_dist, min(max_sl_dist, target_sl_dist))
            tp_dist = target_tp_dist

        tp_dist = max(tp_dist, sl_dist * min_rr)
        if current_tp_dist > 0 and not explicit_targets_valid:
            tp_dist = max(tp_dist, current_tp_dist * 0.9)

        digits = int(self._bounded_float(metadata.get("digits"), default=5.0, minimum=0.0, maximum=10.0))
        if action == "BUY":
            adjusted.stop_loss = round(entry - sl_dist, digits)
            adjusted.take_profit = round(entry + tp_dist, digits)
        else:
            adjusted.stop_loss = round(entry + sl_dist, digits)
            adjusted.take_profit = round(entry - tp_dist, digits)

        adjusted.max_holding_minutes = int(clamped.get("max_hold_minutes", adjusted.max_holding_minutes or 120))
        metadata["strategy_advisory"] = {
            **clamped,
            "used_explicit_price_targets": bool(explicit_targets_valid),
        }
        adjusted.metadata = metadata
        if clamped.get("summary_reason"):
            adjusted.reason = f"{adjusted.reason} Strategy advisor: {clamped.get('summary_reason')}"
        if bool(clamped.get("contradiction_flag", False)):
            adjusted.reason = f"{adjusted.reason} Advisor flagged contradiction risk."
        return adjusted

    def _build_agent_input(
        self,
        context: MarketContext,
        agent_name: str,
    ) -> AgentInput:
        requested = context.requested_timeframe.upper()
        primary_bars = context.bars_by_timeframe.get(requested) or context.bars_by_timeframe.get("H1", [])

        if agent_name == "SmartAgent":
            primary_bars = context.bars_by_timeframe.get("H1", primary_bars)

        return AgentInput(
            symbol=context.symbol,
            timeframe=context.requested_timeframe,
            bars=primary_bars,
            spread=context.tick.get("spread", 0.0) if context.tick else 0.0,
            account_equity=context.account_equity,
            open_positions=context.symbol_open_positions,
            multi_tf_bars=context.bars_by_timeframe,
            policy_mode=str((context.user_policy or {}).get("mode", "balanced") or "balanced"),
            force_execute_any_conditions=bool((context.user_policy or {}).get("force_execute_any_conditions", False)),
        )

    def _apply_gemini_assessment(
        self,
        primary_signal: TechnicalSignal,
        assessment: GeminiAssessment | None,
    ) -> TechnicalSignal:
        final_signal = primary_signal.model_copy(deep=True)
        if not assessment or not assessment.used:
            return final_signal

        final_signal.confidence = round(
            max(0.0, min(0.95, final_signal.confidence + assessment.confidence_adjustment)),
            2,
        )
        metadata = dict(final_signal.metadata)
        metadata["gemini_assessment"] = assessment.model_dump()
        final_signal.metadata = metadata

        if assessment.degraded:
            final_signal.reason = (
                f"{final_signal.reason} Gemini advisory unavailable; using deterministic technical logic only."
            )
        elif assessment.contradiction_flag:
            final_signal.reason = f"{final_signal.reason} Gemini flagged contradictory catalyst risk."
        elif assessment.news_bias != "neutral":
            final_signal.reason = f"{final_signal.reason} Gemini saw {assessment.news_bias} catalyst support."

        return final_signal

    def _downgrade_signal(self, signal: TechnicalSignal, reasons: list[str]) -> TechnicalSignal:
        if signal.action == "HOLD":
            return signal
        downgraded = signal.model_copy(deep=True)
        downgraded.action = "HOLD"
        if reasons:
            downgraded.reason = f"{signal.reason} Blocked: {' '.join(reasons)}"
        return downgraded

    def _recent_drift(self, context: MarketContext, bars: int = 6) -> tuple[str, float]:
        try:
            h1_bars = context.bars_by_timeframe.get("H1", []) if context.bars_by_timeframe else []
            if len(h1_bars) < max(2, bars):
                return "flat", 0.0
            recent = h1_bars[-bars:]
            closes: list[float] = []
            for bar in recent:
                if not isinstance(bar, dict):
                    continue
                close = self._safe_float(bar.get("close"), 0.0)
                if close > 0:
                    closes.append(close)
            if len(closes) < 2:
                return "flat", 0.0
            start = closes[0]
            end = closes[-1]
            drift_pct = ((end - start) / start) * 100.0
            if drift_pct > 0.03:
                return "bullish", abs(drift_pct)
            if drift_pct < -0.03:
                return "bearish", abs(drift_pct)
            return "flat", abs(drift_pct)
        except Exception:
            return "flat", 0.0

    def _flip_signal_action(self, context: MarketContext, signal: TechnicalSignal) -> TechnicalSignal:
        if signal.action not in {"BUY", "SELL"}:
            return signal
        if not context.tick:
            return signal

        current_action = signal.action
        opposite_action = "BUY" if current_action == "SELL" else "SELL"
        current_entry = self._entry_price(context, current_action)
        opposite_entry = self._entry_price(context, opposite_action)
        if current_entry <= 0 or opposite_entry <= 0:
            return signal

        sl = signal.stop_loss
        tp = signal.take_profit
        sl_value = self._safe_float(sl, 0.0) if sl is not None else 0.0
        tp_value = self._safe_float(tp, 0.0) if tp is not None else 0.0
        sl_dist = abs(current_entry - sl_value) if sl_value > 0 else 0.0
        tp_dist = abs(tp_value - current_entry) if tp_value > 0 else 0.0

        flipped = signal.model_copy(deep=True)
        flipped.action = opposite_action
        flipped.confidence = round(max(0.45, min(0.95, self._safe_float(signal.confidence, 0.0) - 0.02)), 2)
        meta = dict(flipped.metadata or {})
        for trend_key in ("h1_trend", "h4_trend"):
            trend_val = str(meta.get(trend_key, "")).lower()
            if trend_val == "bullish":
                meta[trend_key] = "bearish"
            elif trend_val == "bearish":
                meta[trend_key] = "bullish"
        entry_signal = str(meta.get("entry_signal", "")).lower()
        if entry_signal == "buy":
            meta["entry_signal"] = "sell"
        elif entry_signal == "sell":
            meta["entry_signal"] = "buy"
        meta["direction_flipped_by_learning"] = True
        flipped.metadata = meta
        if sl_dist > 0 and tp_dist > 0:
            if opposite_action == "BUY":
                flipped.stop_loss = opposite_entry - sl_dist
                flipped.take_profit = opposite_entry + tp_dist
            else:
                flipped.stop_loss = opposite_entry + sl_dist
                flipped.take_profit = opposite_entry - tp_dist
        return flipped

    def _apply_outcome_learning(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        recent_outcomes: list[dict],
        *,
        enforce_age_filter: bool = True,
    ) -> tuple[TechnicalSignal, list[str]]:
        signal_action = str(signal.action or "").strip().upper()
        if signal_action not in {"BUY", "SELL"}:
            return signal, []
        if not recent_outcomes:
            return signal, []
        recent_outcomes = [outcome for outcome in recent_outcomes if isinstance(outcome, dict)]
        if not recent_outcomes:
            return signal, []

        mode = str((context.user_policy or {}).get("mode", "balanced") or "balanced").strip().lower()
        profiles = {
            "safe": {
                "history_window": 6,
                "loss_memory_minutes": 1440,
                "penalty_min_trades": 3,
                "penalty_loss_rate": 0.60,
                "penalty_soft": 0.10,
                "penalty_hard": 0.14,
                "flip_min_same_trades": 4,
                "flip_loss_rate": 0.75,
                "flip_min_opp_trades": 3,
                "flip_opp_win_rate": 0.67,
                "flip_drift_pct": 0.12,
                "hold_loss_rate": 0.60,
                "hold_conf_floor": 0.70,
            },
            "aggressive": {
                "history_window": 4,
                "loss_memory_minutes": 360,
                "penalty_min_trades": 2,
                "penalty_loss_rate": 0.50,
                "penalty_soft": 0.12,
                "penalty_hard": 0.20,
                "flip_min_same_trades": 2,
                "flip_loss_rate": 0.50,
                "flip_min_opp_trades": 1,
                "flip_opp_win_rate": 0.50,
                "flip_drift_pct": 0.03,
                "hold_loss_rate": 0.50,
                "hold_conf_floor": 0.62,
            },
            "balanced": {
                "history_window": 6,
                "loss_memory_minutes": 720,
                "penalty_min_trades": 3,
                "penalty_loss_rate": 0.67,
                "penalty_soft": 0.08,
                "penalty_hard": 0.12,
                "flip_min_same_trades": 3,
                "flip_loss_rate": 0.67,
                "flip_min_opp_trades": 2,
                "flip_opp_win_rate": 0.50,
                "flip_drift_pct": 0.08,
                "hold_loss_rate": 0.75,
                "hold_conf_floor": 0.60,
            },
        }
        profile = profiles.get(mode, profiles["balanced"])
        history_window = int(profile["history_window"])
        loss_memory_minutes = float(profile.get("loss_memory_minutes", 720) or 720)
        now_ts = time.time()
        if enforce_age_filter:
            learning_outcomes = [
                o for o in recent_outcomes
                if self._outcome_age_minutes(o, now_ts) <= loss_memory_minutes
            ]
        else:
            learning_outcomes = list(recent_outcomes)
        if not learning_outcomes:
            return signal, []
        same_dir = [o for o in learning_outcomes if self._outcome_action(o) == signal_action]
        opposite_action = "BUY" if signal_action == "SELL" else "SELL"
        opp_dir = [o for o in learning_outcomes if self._outcome_action(o) == opposite_action]
        same_recent = same_dir[:history_window]
        opp_recent = opp_dir[:history_window]
        same_losses = [o for o in same_recent if self._outcome_profit(o) <= 0.0]
        same_wins = [o for o in same_recent if self._outcome_profit(o) > 0.0]
        opp_losses = [o for o in opp_recent if self._outcome_profit(o) <= 0.0]
        opp_wins = [o for o in opp_recent if self._outcome_profit(o) > 0.0]
        same_pnl = sum(self._outcome_profit(o) for o in same_recent)
        opp_pnl = sum(self._outcome_profit(o) for o in opp_recent)
        drift_dir, drift_pct = self._recent_drift(context)
        drift_opposes_signal = (
            (signal_action == "BUY" and drift_dir == "bearish")
            or (signal_action == "SELL" and drift_dir == "bullish")
        )
        same_count = len(same_recent)
        opp_count = len(opp_recent)
        same_loss_rate = (len(same_losses) / same_count) if same_count else 0.0
        opp_win_rate = (len(opp_wins) / opp_count) if opp_count else 0.0
        severe_same_losses = [
            o for o in same_recent
            if self._outcome_profit(o) < 0.0
            and self._outcome_exit_reason(o) in {
                "ten_minute_loss_cut",
                "risk_budget_stop",
                "stop_loss",
                "stop_loss_amount",
                "time_stop",
            }
        ]

        reasons: list[str] = []
        adjusted = signal.model_copy(deep=True)
        adjusted.action = signal_action
        adjusted.confidence = self._safe_float(adjusted.confidence, 0.0)

        if (
            same_count >= int(profile["penalty_min_trades"])
            and same_loss_rate >= float(profile["penalty_loss_rate"])
            and same_pnl < 0
        ):
            penalty = float(profile["penalty_soft"]) if same_loss_rate < 0.8 else float(profile["penalty_hard"])
            adjusted.confidence = round(max(0.0, adjusted.confidence - penalty), 2)
            reasons.append(
                f"Outcome learning: recent {signal_action} trades on {context.symbol} are underperforming ({len(same_losses)}/{same_count} losses, pnl {same_pnl:.2f})."
            )

        can_flip = (
            same_count >= int(profile["flip_min_same_trades"])
            and same_loss_rate >= float(profile["flip_loss_rate"])
            and same_pnl < 0
            and opp_count >= int(profile["flip_min_opp_trades"])
            and opp_win_rate >= float(profile["flip_opp_win_rate"])
            and opp_pnl > 0
        )
        can_flip_on_structure = can_flip and (drift_opposes_signal and drift_pct >= float(profile["flip_drift_pct"]))
        can_flip_on_outcomes = (
            can_flip
            and same_loss_rate >= 0.75
            and opp_win_rate >= 0.60
            and adjusted.confidence >= 0.55
        )
        can_flip_fast_recovery = (
            same_count >= 2
            and len(same_losses) >= 2
            and same_pnl < 0
            and opp_count >= 1
            and opp_win_rate >= 0.50
            and opp_pnl > 0
            and adjusted.confidence >= 0.55
        )
        emergency_same_side_block = (
            same_count >= 2
            and len(same_losses) >= 2
            and same_pnl <= -40.0
            and opp_pnl <= 0
        )
        severe_runtime_loss_block = (
            len(severe_same_losses) >= 2
            and same_pnl < 0
        )
        recent_managed_loss_cluster = (
            len(severe_same_losses) >= 3
            and same_pnl <= -15.0
            and opp_pnl <= 0.0
        )
        if emergency_same_side_block or severe_runtime_loss_block:
            adjusted.confidence = round(min(adjusted.confidence, 0.35), 2)
            adjusted.action = "HOLD"
            if recent_managed_loss_cluster:
                adjusted.confidence = round(min(adjusted.confidence, 0.25), 2)
                reasons.append(
                    "Outcome learning: blocked this direction because recent managed losses show the current regime is hostile to this side."
                )
            elif severe_runtime_loss_block:
                reasons.append(
                    "Outcome learning: blocked this direction after repeated managed loss exits."
                )
            else:
                reasons.append(
                    "Outcome learning: blocked this direction after repeated same-side losses with no opposite-side edge."
                )
        elif can_flip_on_structure or can_flip_on_outcomes or can_flip_fast_recovery:
            adjusted = self._flip_signal_action(context, adjusted)
            reasons.append(
                f"Direction adapted: flipped to {adjusted.action}; opposite direction is outperforming ({len(opp_wins)}/{opp_count} wins, pnl {opp_pnl:.2f})."
            )
        elif same_count >= 2 and len(same_losses) >= 2 and same_pnl < 0 and adjusted.confidence < 0.62:
            adjusted.action = "HOLD"
            reasons.append("Outcome learning: blocked repeated same-direction entry after back-to-back losses.")
        elif (
            same_count >= int(profile["penalty_min_trades"])
            and same_loss_rate >= float(profile["hold_loss_rate"])
            and adjusted.confidence < float(profile["hold_conf_floor"])
        ):
            adjusted.action = "HOLD"
            reasons.append("Outcome learning: switched to HOLD after repeated same-direction losses and weak confidence.")
        elif adjusted.confidence < 0.45 and len(same_losses) >= int(profile["penalty_min_trades"]):
            adjusted.action = "HOLD"
            reasons.append("Outcome learning: confidence reduced below execution floor after repeated same-direction losses.")

        if reasons:
            meta = dict(adjusted.metadata or {})
            meta["outcome_learning"] = {
                "mode": mode,
                "history_window": history_window,
                "thresholds": {
                    "flip_drift_pct": float(profile["flip_drift_pct"]),
                    "flip_loss_rate": float(profile["flip_loss_rate"]),
                    "hold_loss_rate": float(profile["hold_loss_rate"]),
                    "hold_conf_floor": float(profile["hold_conf_floor"]),
                },
                "same_direction_losses": len(same_losses),
                "same_direction_wins": len(same_wins),
                "same_direction_pnl": round(same_pnl, 2),
                "same_direction_loss_rate": round(same_loss_rate, 3),
                "severe_same_side_losses": len(severe_same_losses),
                "opposite_direction_wins": len(opp_wins),
                "opposite_direction_losses": len(opp_losses),
                "opposite_direction_pnl": round(opp_pnl, 2),
                "opposite_direction_win_rate": round(opp_win_rate, 3),
                "drift_direction": drift_dir,
                "drift_pct": round(drift_pct, 4),
                "recent_managed_loss_cluster": bool(recent_managed_loss_cluster),
            }
            adjusted.metadata = meta
            adjusted.reason = f"{adjusted.reason} {' '.join(reasons)}"

        return adjusted, reasons

    def _outcome_profit(self, outcome: dict) -> float:
        for key in ("profit", "profit_usd", "pnl", "net_pnl"):
            try:
                value = outcome.get(key)
                if value is not None:
                    parsed = self._parse_outcome_number(value)
                    if math.isfinite(parsed):
                        return parsed
            except (TypeError, ValueError):
                continue
        return 0.0

    def _outcome_action(self, outcome: dict) -> str:
        return str(outcome.get("action", "") or "").strip().upper()

    def _outcome_exit_reason(self, outcome: dict) -> str:
        reason = str(outcome.get("exit_reason", "") or "").strip().lower()
        normalized = reason.replace("-", "_").replace(" ", "_")
        aliases = {
            "sl": "stop_loss",
            "stopout": "stop_loss",
            "stop_out": "stop_loss",
            "stopped_out": "stop_loss",
            "stoppedout": "stop_loss",
        }
        return aliases.get(normalized, normalized)

    def _outcome_age_minutes(self, outcome: dict, now_ts: float) -> float:
        for key in ("closed_at", "timestamp"):
            try:
                value = outcome.get(key)
                if value is None:
                    continue
                parsed = self._parse_outcome_number(value)
                if math.isfinite(parsed) and parsed > 0:
                    return max(0.0, (now_ts - parsed) / 60.0)
            except (TypeError, ValueError):
                continue
        # Unknown age is treated as current so sparse runtime rows and existing
        # unit tests still participate in learning. Timestamped stale rows are
        # filtered out by the caller.
        return 0.0

    def _parse_outcome_number(self, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip()
            negative = text.startswith("(") and text.endswith(")")
            if negative:
                text = text[1:-1].strip()
            text = text.replace("$", "").replace(",", "")
            parsed = float(text)
            return -abs(parsed) if negative else parsed
        return float(value)

    async def _build_meta_model_preview(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
        portfolio_fit_score: float,
    ) -> dict[str, Any]:
        if self.meta_model_service is None:
            return {
                "active": False,
                "reason": "not_configured",
                "profit_probability": 0.0,
                "expected_edge": 0.0,
                "no_trade_probability": 1.0,
            }
        quality = self.trade_decision_service.trade_quality_service.assess(
            context=context,
            signal=signal,
            gemini_assessment=gemini_assessment,
            portfolio_fit_score=portfolio_fit_score,
        )
        return await self.meta_model_service.preview_signal_insight(
            context=context,
            signal=signal,
            quality=quality,
            gemini_assessment=gemini_assessment,
            portfolio_fit_score=portfolio_fit_score,
            anti_churn_blocked=False,
            refresh_model=str(context.evaluation_mode or "").strip().lower() not in {"scan", "auto"},
        )

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        if action == "BUY":
            return self._safe_float(context.tick.get("ask"), 0.0)
        if action == "SELL":
            return self._safe_float(context.tick.get("bid"), 0.0)
        return 0.0

    def _apply_mode_target_scaling(
        self,
        signal: TechnicalSignal,
        entry_price: float,
        mode: str,
    ) -> TechnicalSignal:
        action = str(signal.action or "").strip().upper()
        if action not in {"BUY", "SELL"}:
            return signal
        if not signal.stop_loss or not signal.take_profit or entry_price <= 0:
            return signal
        entry = self._safe_float(entry_price, 0.0)
        stop_loss = self._safe_float(signal.stop_loss, 0.0)
        take_profit = self._safe_float(signal.take_profit, 0.0)
        if not (math.isfinite(entry) and math.isfinite(stop_loss) and math.isfinite(take_profit)):
            return signal
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
            return signal
        if action == "BUY" and not (stop_loss < entry < take_profit):
            return signal
        if action == "SELL" and not (take_profit < entry < stop_loss):
            return signal

        multipliers = {
            "safe": 0.95,
            "balanced": 1.00,
            "aggressive": 1.05,
        }
        multiplier = multipliers.get((mode or "balanced").lower(), 1.00)
        if abs(multiplier - 1.0) < 1e-6:
            return signal

        sl_distance = abs(entry - stop_loss) * multiplier
        tp_distance = abs(take_profit - entry) * multiplier
        digits = (signal.metadata or {}).get("digits")
        if not isinstance(digits, int):
            digits = 5

        if action == "BUY":
            new_sl = round(entry - sl_distance, digits)
            new_tp = round(entry + tp_distance, digits)
        else:
            new_sl = round(entry + sl_distance, digits)
            new_tp = round(entry - tp_distance, digits)

        updated = signal.model_copy(deep=True)
        updated.action = action
        updated.stop_loss = new_sl
        updated.take_profit = new_tp
        updated.metadata = {
            **(updated.metadata or {}),
            "mode_target_scaling": {
                "mode": mode,
                "multiplier": multiplier,
            },
        }
        return updated

    def _strategy_family(
        self,
        signal: TechnicalSignal,
        gemini_assessment: GeminiAssessment | None,
    ) -> str:
        strategy = (signal.strategy or "").lower()
        if "news" in strategy:
            return "news"
        if gemini_assessment and gemini_assessment.used:
            if gemini_assessment.macro_relevance == "high":
                return "news"
            if gemini_assessment.event_risk == "high" and gemini_assessment.news_bias != "neutral":
                return "news"
        return "intraday_technical"

    def _planned_hold_minutes(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        strategy_family: str,
    ) -> int | None:
        profile = context.profile
        if profile is None:
            if strategy_family == "news":
                lower, upper = 30, 90
            else:
                lower, upper = 60, 180
        elif strategy_family == "news":
            lower, upper = profile.news_hold_min_minutes, profile.news_hold_max_minutes
        else:
            lower, upper = profile.technical_hold_min_minutes, profile.technical_hold_max_minutes

        requested_base = signal.max_holding_minutes
        if requested_base is None:
            requested_base = profile.max_hold_minutes if profile else upper
        requested = int(self._bounded_float(requested_base, default=float(upper), minimum=float(lower), maximum=float(upper)))
        return max(lower, min(requested, upper))

    def _build_position_management_plan(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        trade_quality,
        gemini_assessment: GeminiAssessment | None,
    ) -> PositionManagementPlan:
        profile = context.profile
        meta = signal.metadata or {}
        atr_regime = "low"
        atr_pct = self._safe_float(meta.get("atr_pct"), 0.0)
        if profile and atr_pct >= profile.min_atr_pct * 1.5:
            atr_regime = "high"
        elif profile and atr_pct >= profile.min_atr_pct:
            atr_regime = "normal"

        quality_score = self._safe_float(getattr(trade_quality, "final_trade_quality_score", 0.0), 0.0)
        thesis = (
            f"{signal.action} thesis: {meta.get('h1_trend', 'flat')} H1 / {meta.get('h4_trend', 'flat')} H4 "
            f"alignment, entry={meta.get('entry_signal', 'none')}, quality={quality_score:.2f}."
        )
        if gemini_assessment and gemini_assessment.used and not gemini_assessment.degraded:
            thesis = f"{thesis} News={gemini_assessment.news_bias}, event_risk={gemini_assessment.event_risk}."

        strategy_family = self._strategy_family(signal, gemini_assessment)
        expected_hold_minutes = self._planned_hold_minutes(context, signal, strategy_family)
        stale_after_minutes = None
        min_progress_r_multiple = 0.25
        target_progress_r_multiple = 0.45
        if expected_hold_minutes:
            if strategy_family == "news":
                stale_after_minutes = max(15, int(round(expected_hold_minutes * 0.5)))
                min_progress_r_multiple = 0.30
                target_progress_r_multiple = 0.45
            else:
                stale_after_minutes = max(30, int(round(expected_hold_minutes * 0.65)))
                min_progress_r_multiple = 0.20
                target_progress_r_multiple = 0.35

        user_policy = context.user_policy or {}
        allow_overnight_hold = bool(user_policy.get("allow_overnight_holding", False)) or bool(
            profile.allow_overnight_hold if profile else False
        )
        close_before_session_end = bool(profile.force_close_before_session_end) and not allow_overnight_hold if profile else False
        return PositionManagementPlan(
            manage_position=signal.action in {"BUY", "SELL"},
            strategy=signal.strategy,
            max_holding_minutes=expected_hold_minutes,
            planned_hold_minutes=expected_hold_minutes,
            initial_thesis=thesis,
            expected_hold_minutes=expected_hold_minutes,
            atr_regime=atr_regime,
            close_before_session_end=close_before_session_end,
            session_close_buffer_minutes=profile.session_close_buffer_minutes if profile else 0,
            stale_after_minutes=stale_after_minutes,
            min_progress_r_multiple=min_progress_r_multiple,
            target_progress_r_multiple=target_progress_r_multiple,
            break_even_activation_rule="Move to breakeven only after >=1.25R and structure still aligns.",
            trailing_rule="Activate trailing after >=1.8R while H1/H4 trend remains supportive.",
            invalidation_condition="Exit if stored trend thesis breaks, spread quality collapses, or event risk invalidates the setup.",
            time_stop_rule=(
                f"Exit within {expected_hold_minutes or 0} minutes, or sooner if progress stays below "
                f"{min_progress_r_multiple:.2f}R by minute {stale_after_minutes or 0}."
            ),
            notes=[
                "Created by canonical signal pipeline.",
                "Averaging down remains disabled.",
            ],
            metadata={
                "strategy_family": strategy_family,
                "symbol_category": profile.category if profile else (context.symbol_info.category if context.symbol_info else "Other"),
                "trade_quality_score": trade_quality.final_trade_quality_score,
                "trade_quality_threshold": trade_quality.threshold,
                "news_bias": gemini_assessment.news_bias if gemini_assessment else "neutral",
                "event_risk": gemini_assessment.event_risk if gemini_assessment else "low",
                "break_even_r_multiple": 1.25,
                "trailing_activation_r_multiple": 1.8,
                "trail_atr_multiplier": 1.1 if context.profile and context.profile.category != "Forex" else 0.8,
                "time_stop_minutes": expected_hold_minutes,
                "planned_hold_minutes": expected_hold_minutes,
                "stale_after_minutes": stale_after_minutes,
                "min_progress_r_multiple": min_progress_r_multiple,
                "target_progress_r_multiple": target_progress_r_multiple,
                "close_before_session_end": close_before_session_end,
                "allow_overnight_hold": allow_overnight_hold,
                "session_close_buffer_minutes": profile.session_close_buffer_minutes if profile else 0,
                "sessions": list((profile.allowed_sessions if profile else []) or []),
                # Keep default behavior predictable: close at configured TP unless
                # a strategy explicitly opts into TP laddering.
                "tp_ladder_enabled": False,
                "tp_ladder_step_pct": 0.02,
            },
        )

    async def evaluate(
        self,
        symbol: str,
        requested_agent_name: Optional[str] = None,
        requested_timeframe: str = "H1",
        evaluation_mode: str = "manual",
        bar_count: int = 100,
        scan_window_id: Optional[str] = None,
    ) -> ExecutionDecision:
        context = self.build_market_context(
            symbol=symbol,
            requested_timeframe=requested_timeframe,
            evaluation_mode=evaluation_mode,
            bar_count=bar_count,
        )
        return await self.evaluate_context(
            context=context,
            requested_agent_name=requested_agent_name,
            scan_window_id=scan_window_id,
        )

    async def evaluate_context(
        self,
        context: MarketContext,
        requested_agent_name: Optional[str] = None,
        scan_window_id: Optional[str] = None,
    ) -> ExecutionDecision:
        evaluation_mode = context.evaluation_mode
        inactive_reason = self._symbol_universe_inactive_reason(
            context.symbol,
            context.symbol_info.category if context.symbol_info else None,
        )
        if inactive_reason:
            return self._inactive_universe_decision(
                context=context,
                requested_agent_name=requested_agent_name or "GeminiAgent",
                reason=inactive_reason,
            )
        market_guard_reason: str | None = None
        if evaluation_mode in {"manual", "scan", "auto", "auto_fast", "scan_fast"}:
            # Keep technical analysis running so UI still gets confidence/quality,
            # but block execution later when market is closed/stale.
            market_guard_reason = self._market_live_guard_reason(context)
        primary_agent_name = self._resolve_primary_agent_name(requested_agent_name)
        baseline_agent_name = primary_agent_name if primary_agent_name != "GeminiAgent" else self._resolve_baseline_agent_name()
        baseline_agent = self.agents.get(baseline_agent_name)
        if baseline_agent is None:
            raise ValueError(f"Agent '{baseline_agent_name}' not found")

        input_data = self._build_agent_input(context, baseline_agent_name)
        raw_signal = baseline_agent.evaluate(input_data)
        baseline_signal = TechnicalSignal.from_trade_signal(raw_signal, baseline_agent_name)
        if context.symbol_info:
            signal_meta = dict(baseline_signal.metadata or {})
            signal_meta.setdefault("point", context.symbol_info.point)
            signal_meta.setdefault("digits", context.symbol_info.digits)
            signal_meta.setdefault("category", context.symbol_info.category)
            baseline_signal.metadata = signal_meta

        context = await self._load_news_for_context(context, evaluation_mode=evaluation_mode)
        input_data = self._build_agent_input(context, baseline_agent_name)
        # Deterministic event-aware advisory path (used even when Gemini is off).
        baseline_signal = self._apply_event_context_advisory(context, baseline_signal)
        gemini_assessment: Optional[GeminiAssessment] = None
        if self._should_use_gemini(requested_agent_name, evaluation_mode, context):
            gemini_assessment = await self._assess_news(context, input_data, baseline_signal)
        strategy_advisory: dict = {
            "used": False,
            "available": bool(getattr(self.gemini_strategy_advisor_service, "available", False)),
            "degraded": False,
            "summary_reason": "Strategy advisor skipped.",
        }
        if self._should_use_strategy_advisor(
            context=context,
            signal=baseline_signal,
            evaluation_mode=evaluation_mode,
        ):
            event_context = (baseline_signal.metadata or {}).get("event_context", {})
            strategy_advisory = await self._assess_strategy_advisory(
                context=context,
                technical_signal=baseline_signal,
                gemini_assessment=gemini_assessment,
                event_context=event_context,
                evaluation_mode=evaluation_mode,
            )
            baseline_signal = self._apply_strategy_advisory(
                context=context,
                signal=baseline_signal,
                advisory=strategy_advisory,
            )

        recent_outcomes = []
        recent_evaluations = []
        # Auto-trade must respect recent exits too; otherwise a manual close or
        # stop-out can be immediately reopened on the next scan cycle.
        if self.db is not None and evaluation_mode in {"manual", "replay", "auto", "auto_fast", "scan_fast"}:
            lookback_minutes = max(self.risk_engine.settings.cooldown_minutes_per_symbol, 3 * 24 * 60)
            recent_limit = 8 if evaluation_mode in {"auto", "auto_fast", "scan_fast"} else 5
            recent_outcomes = await self.db.get_recent_symbol_outcomes(
                context.symbol,
                limit=recent_limit,
                within_minutes=lookback_minutes,
            )
            recent_evaluations = await self.db.get_recent_symbol_evaluations(
                context.symbol,
                limit=recent_limit,
                within_minutes=lookback_minutes,
            )

        preview_portfolio_fit = self.risk_service.preview_portfolio_fit(context)
        meta_model_preview = await self._build_meta_model_preview(
            context=context,
            signal=baseline_signal,
            gemini_assessment=gemini_assessment,
            portfolio_fit_score=preview_portfolio_fit,
        )
        trade_brain_assessment: dict[str, Any] = {
            "used": False,
            "available": bool(getattr(self.gemini_trade_brain_service, "available", False)),
            "degraded": False,
            "summary_reason": "Gemini trade brain skipped.",
        }
        if self._should_use_trade_brain(
            requested_agent_name=requested_agent_name,
            primary_agent_name=primary_agent_name,
            context=context,
        ):
            trade_brain_assessment = await self.gemini_trade_brain_service.assess(
                context=context,
                baseline_signal=baseline_signal,
                gemini_assessment=gemini_assessment,
                meta_model_insight=meta_model_preview,
                recent_outcomes=recent_outcomes,
                recent_evaluations=recent_evaluations,
            )
            if trade_brain_assessment.get("used") and not trade_brain_assessment.get("degraded"):
                primary_signal = self._signal_from_trade_brain(
                    context=context,
                    baseline_signal=baseline_signal,
                    assessment=trade_brain_assessment,
                )
            else:
                primary_signal = baseline_signal.model_copy(deep=True)
        else:
            primary_signal = baseline_signal.model_copy(deep=True)
        learned_signal, learning_reasons = self._apply_outcome_learning(
            context=context,
            signal=primary_signal,
            recent_outcomes=recent_outcomes,
            enforce_age_filter=False,
        )
        if learning_reasons:
            primary_signal = learned_signal

        trade_decision = self.trade_decision_service.decide(
            context=context,
            technical_signal=primary_signal,
            gemini_assessment=gemini_assessment,
            portfolio_fit_score=preview_portfolio_fit,
        )

        entry_price = self._entry_price(context, trade_decision.final_signal.action)
        policy_mode = str((context.user_policy or {}).get("mode", "balanced"))
        scaled_signal = self._apply_mode_target_scaling(
            trade_decision.final_signal,
            entry_price,
            policy_mode,
        )
        trade_decision = trade_decision.model_copy(update={"final_signal": scaled_signal})

        meta_assessment = {
            "active": False,
            "changed_decision": False,
            "blocked": False,
            "profit_probability": 0.0,
            "expected_edge": 0.0,
            "no_trade_probability": 1.0,
            "reason": "not_evaluated",
        }
        cloud_decision = {
            "used": False,
            "reason": "cloud_decision_not_evaluated",
        }
        if self.meta_model_service is not None:
            trade_decision, meta_assessment = await self._assess_trade_decision_with_meta_model(
                context=context,
                trade_decision=trade_decision,
                gemini_assessment=gemini_assessment,
                portfolio_risk_assessment=PortfolioRiskAssessment(
                    portfolio_fit_score=preview_portfolio_fit,
                ),
                anti_churn_blocked=False,
            )
        # Cloud-brain overlay can provide the centralized recommendation layer
        # for manual, scan, and autonomous evaluation paths. Local risk and
        # execution checks still remain the final safety gate.
        if self.cloud_brain_decision_client is not None:
            payload = self._build_cloud_decision_payload(
                context=context,
                trade_decision=trade_decision,
                gemini_assessment=gemini_assessment,
                meta_assessment=meta_assessment,
            )
            cloud_decision = await self.cloud_brain_decision_client.decide(
                payload,
                timeout_seconds=self._cloud_decision_timeout_seconds(evaluation_mode),
            )
            trade_decision, cloud_decision = self._apply_cloud_decision(
                trade_decision=trade_decision,
                cloud_decision=cloud_decision,
            )

        risk_approval = self.risk_service.assess(
            context=context,
            signal=trade_decision.final_signal,
            trade_quality=trade_decision.trade_quality_assessment,
            evaluation_mode=evaluation_mode,
            recent_outcomes=recent_outcomes,
            recent_evaluations=recent_evaluations,
            scan_window_id=scan_window_id,
            entry_price=entry_price,
        )

        if (
            risk_approval.anti_churn_assessment.threshold_boost > 0
            or abs(
                risk_approval.portfolio_risk_assessment.portfolio_fit_score
                - preview_portfolio_fit
            ) >= 0.05
        ):
            trade_decision = self.trade_decision_service.decide(
                context=context,
                technical_signal=primary_signal,
                gemini_assessment=gemini_assessment,
                portfolio_fit_score=risk_approval.portfolio_risk_assessment.portfolio_fit_score,
                threshold_boost=risk_approval.anti_churn_assessment.threshold_boost,
            )
            scaled_signal = self._apply_mode_target_scaling(
                trade_decision.final_signal,
                entry_price,
                policy_mode,
            )
            trade_decision = trade_decision.model_copy(update={"final_signal": scaled_signal})
            if self.meta_model_service is not None:
                trade_decision, meta_assessment = await self._assess_trade_decision_with_meta_model(
                    context=context,
                    trade_decision=trade_decision,
                    gemini_assessment=gemini_assessment,
                    portfolio_risk_assessment=risk_approval.portfolio_risk_assessment,
                    anti_churn_blocked=risk_approval.anti_churn_assessment.blocked,
                )

        final_signal = trade_decision.final_signal
        blocking_reasons: list[str] = []
        if market_guard_reason:
            blocking_reasons.append(market_guard_reason)
        event_context = (final_signal.metadata or {}).get("event_context", {})
        news_context = dict(event_context.get("news") or {})
        if bool(news_context.get("execution_block")):
            blocking_reasons.append("Trade is blocked due to high-impact news risk.")
        elif bool(news_context.get("recommend_hold")):
            blocking_reasons.append("Trade avoided due to high-impact news.")
        elif bool(event_context.get("contradiction_flag")) and self._safe_float(news_context.get("strength"), 0.0) >= 0.58:
            blocking_reasons.append("Trade blocked due to strong conflicting news signals.")
        if not trade_decision.trade:
            blocking_reasons.extend(
                trade_decision.trade_quality_assessment.no_trade_reasons or trade_decision.reasons
            )
        if not risk_approval.approved:
            blocking_reasons.extend(risk_approval.reasons)

        if blocking_reasons and final_signal.action in {"BUY", "SELL"}:
            final_signal = self._downgrade_signal(final_signal, blocking_reasons)
        if market_guard_reason:
            # Missing/stale executable market data must never appear as a tradable setup.
            final_signal = final_signal.model_copy(
                update={
                    "action": "HOLD",
                    "confidence": 0.0,
                    "reason": f"{final_signal.reason} Market data guard: {market_guard_reason}",
                }
            )

        risk_evaluation = risk_approval.risk_evaluation
        risk_evaluation.approved = risk_approval.approved and trade_decision.trade
        risk_evaluation.reason = "Ready for execution" if risk_evaluation.approved else (
            blocking_reasons[0] if blocking_reasons else risk_evaluation.reason
        )
        risk_evaluation.status = "pass" if risk_evaluation.approved else "block"
        risk_evaluation.metrics_snapshot = {
            **risk_evaluation.metrics_snapshot,
            "trade_quality_score": trade_decision.trade_quality_assessment.final_trade_quality_score,
            "trade_quality_threshold": trade_decision.trade_quality_assessment.threshold,
            "meta_model": meta_assessment,
            "cloud_brain": cloud_decision,
        }

        position_management_plan = self._build_position_management_plan(
            context=context,
            signal=final_signal,
            trade_quality=trade_decision.trade_quality_assessment,
            gemini_assessment=gemini_assessment,
        )
        final_signal = final_signal.model_copy(
            update={"max_holding_minutes": position_management_plan.max_holding_minutes}
        )

        strategy_advisor_used = bool(
            ((final_signal.metadata or {}).get("strategy_advisory") or {}).get("used")
        )
        trade_brain_used = bool(((primary_signal.metadata or {}).get("gemini_trade_brain") or {}).get("used"))
        final_agent_parts: list[str] = ["GeminiTradeBrain" if trade_brain_used else primary_agent_name]
        if gemini_assessment and gemini_assessment.used and not gemini_assessment.degraded:
            final_agent_parts.append("GeminiNews")
        if strategy_advisor_used:
            final_agent_parts.append("StrategyAdvisor")
        if meta_assessment.get("active"):
            final_agent_parts.append("MetaModel")
        if cloud_decision.get("used"):
            final_agent_parts.append("CloudBrain")

        signal_decision = SignalDecision(
            requested_agent_name=requested_agent_name or primary_agent_name,
            primary_agent_name=primary_agent_name,
            final_agent_name="+".join(final_agent_parts),
            market_context=context,
            primary_signal=primary_signal,
            final_signal=final_signal,
            gemini_confirmation=gemini_assessment,
            degraded_reasons=context.degraded_reasons,
        )

        allow_execute = risk_evaluation.approved and final_signal.action in {"BUY", "SELL"}
        reason = "Ready for execution" if allow_execute else (blocking_reasons[0] if blocking_reasons else risk_evaluation.reason)
        return ExecutionDecision(
            allow_execute=allow_execute,
            reason=reason,
            signal_decision=signal_decision,
            trade_decision_assessment=trade_decision,
            risk_evaluation=risk_evaluation,
            trade_quality_assessment=trade_decision.trade_quality_assessment,
            portfolio_risk_assessment=risk_approval.portfolio_risk_assessment,
            anti_churn_assessment=risk_approval.anti_churn_assessment,
            position_management_plan=position_management_plan,
            entry_price=entry_price,
        )

    async def auto_trade_candidate_symbols(
        self,
        market_symbols: list[dict],
        *,
        limit: int = 12,
    ) -> list[str]:
        category_by_symbol: dict[str, str] = {}
        for item in market_symbols:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            normalized_category = self.universe_service.normalize_asset_class(item.get("category"))
            if not normalized_category:
                continue
            category_by_symbol.setdefault(name.upper(), normalized_category)
            canonical = self.universe_service.canonical_symbol(name)
            if canonical:
                category_by_symbol.setdefault(canonical, normalized_category)

        def _market_commodity_candidates(symbols: list[dict]) -> list[str]:
            # Discover broker-available commodities so auto-trade is not
            # restricted to the static fallback list (e.g., include XAGUSD).
            priority = [
                "XAUUSD", "GOLD", "XAGUSD", "SILVER", "USOIL", "WTI", "UKOIL", "BRENT", "NATGAS",
            ]
            buckets: dict[str, str] = {}
            for item in symbols:
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                category = self.universe_service.normalize_asset_class(item.get("category"))
                if category != "Commodities":
                    continue
                canonical = self.universe_service.canonical_symbol(name)
                if not self.universe_service.is_symbol_active(canonical or name, category):
                    continue
                buckets.setdefault(canonical or name.upper(), name)

            ordered: list[str] = []
            for preferred in priority:
                canonical = self.universe_service.canonical_symbol(preferred)
                if canonical in buckets:
                    ordered.append(buckets[canonical])
            for canonical, raw_name in buckets.items():
                if raw_name not in ordered:
                    ordered.append(raw_name)
            return ordered

        def _filter_session_eligible(symbols: list[str]) -> list[str]:
            eligible: list[str] = []
            for symbol in symbols:
                if self._session_allowed_for_symbol(symbol, market_symbols):
                    eligible.append(symbol)
            return eligible

        def _symbol_category(symbol: str) -> str:
            direct = category_by_symbol.get((symbol or "").strip().upper())
            if direct:
                return direct
            resolved = self.universe_service.resolve_requested_symbols([symbol], market_symbols)
            broker_symbol = resolved[0] if resolved else symbol
            raw = self.market_data.get_symbol_info(broker_symbol)
            if raw is not None:
                tick_model = self.market_data.get_tick(broker_symbol)
                tick = tick_model.model_dump() if tick_model else None
                normalized = self._normalize_symbol_info(broker_symbol, tick)
                if normalized and normalized.category:
                    return normalized.category
            expected = self.universe_service.expected_category_for_symbol(symbol)
            return expected or "Other"

        def _balanced_symbol_mix(symbols: list[str]) -> list[str]:
            buckets: dict[str, list[str]] = {
                "Indices": [],
                "Stocks": [],
                "Commodities": [],
                "Crypto": [],
                "Forex": [],
                "Other": [],
            }
            seen: set[str] = set()
            for symbol in symbols:
                if symbol in seen:
                    continue
                seen.add(symbol)
                buckets.setdefault(_symbol_category(symbol), []).append(symbol)

            def _commodity_priority(symbol: str) -> tuple[int, str]:
                canonical = self.universe_service.canonical_symbol(symbol)
                if canonical in {"GOLD", "SILVER", "XAUUSD", "XAGUSD"}:
                    return (0, canonical)
                if canonical in {"WTI", "BRENT", "USOIL", "UKOIL", "XTIUSD", "XBRUSD"}:
                    return (2, canonical)
                return (1, canonical)

            commodities = buckets.get("Commodities") or []
            if commodities:
                commodities.sort(key=_commodity_priority)

            # If we already have enough tradable indices/stocks, keep energy
            # contracts from dominating the short scan list.
            liquid_non_commodities = len((buckets.get("Indices") or [])) + len((buckets.get("Stocks") or []))
            if liquid_non_commodities >= max(4, limit // 2) and commodities:
                favored = []
                deferred_energy = []
                for symbol in commodities:
                    canonical = self.universe_service.canonical_symbol(symbol)
                    if canonical in {"WTI", "BRENT"}:
                        deferred_energy.append(symbol)
                    else:
                        favored.append(symbol)
                buckets["Commodities"] = favored + deferred_energy

            ordered: list[str] = []
            category_cycle = ("Indices", "Stocks", "Commodities", "Crypto", "Forex", "Other")
            while len(ordered) < limit:
                added = False
                for category in category_cycle:
                    bucket = buckets.get(category) or []
                    if not bucket:
                        continue
                    ordered.append(bucket.pop(0))
                    added = True
                    if len(ordered) >= limit:
                        break
                if not added:
                    break
            return ordered

        def _non_stock_fallback(symbols: list[str]) -> list[str]:
            preferred: list[str] = []
            for symbol in symbols:
                resolved = self.universe_service.resolve_requested_symbols([symbol], market_symbols)
                broker_symbol = resolved[0] if resolved else symbol
                raw = self.market_data.get_symbol_info(broker_symbol)
                if raw is None:
                    continue
                tick_model = self.market_data.get_tick(broker_symbol)
                tick = tick_model.model_dump() if tick_model else None
                normalized = self._normalize_symbol_info(broker_symbol, tick)
                if normalized and normalized.category in {"Indices", "Commodities"}:
                    preferred.append(broker_symbol)
            return preferred[:limit]

        if self.event_ingestion_service is not None:
            await self.event_ingestion_service.maybe_refresh_latest(
                min_interval_seconds=300.0,
                # Candidate discovery should stay lightweight and non-blocking.
                classify_with_gemini=False,
            )
            event_symbols = await self.event_ingestion_service.latest_candidate_symbols(
                universe_service=self.universe_service,
                market_symbols=market_symbols,
                limit=limit,
            )
            if event_symbols:
                eligible = _filter_session_eligible(event_symbols)
                if eligible:
                    fallback = self.universe_service.default_auto_trade_symbols(market_symbols)
                    discovered_commodities = _market_commodity_candidates(market_symbols)
                    fallback_eligible = _filter_session_eligible(fallback)
                    commodity_eligible = _filter_session_eligible(discovered_commodities)
                    if len(eligible) >= limit:
                        # Diversify: avoid full lock-in to event-only symbols (often a narrow
                        # ETF/metals cluster) so auto-trader still evaluates broad liquid majors.
                        event_quota = max(4, limit // 2)
                        merged: list[str] = []
                        for symbol in eligible[:event_quota] + commodity_eligible + fallback_eligible + eligible[event_quota:]:
                            if symbol not in merged:
                                merged.append(symbol)
                        return _balanced_symbol_mix(merged)
                    # If event feed is too narrow (for example only GOLD),
                    # augment with curated symbols to avoid single-symbol lock-in.
                    merged: list[str] = []
                    for symbol in eligible + commodity_eligible + fallback_eligible:
                        if symbol not in merged:
                            merged.append(symbol)
                    return _balanced_symbol_mix(merged)
                return _non_stock_fallback(event_symbols)
        fallback = self.universe_service.default_auto_trade_symbols(market_symbols)
        discovered_commodities = _market_commodity_candidates(market_symbols)
        eligible_fallback = _filter_session_eligible(fallback)
        commodity_eligible = _filter_session_eligible(discovered_commodities)
        merged_fallback: list[str] = []
        for symbol in eligible_fallback + commodity_eligible:
            if symbol not in merged_fallback:
                merged_fallback.append(symbol)
        if merged_fallback:
            return _balanced_symbol_mix(merged_fallback)
        return _non_stock_fallback(fallback)

    async def persist_evaluation(
        self,
        execution_decision: ExecutionDecision,
        timeframe_label: str,
    ) -> Optional[int]:
        if self.db is None:
            return None

        executable_signal = execution_decision.signal_decision.final_signal
        signal_id = await self.db.log_signal(
            agent_name=execution_decision.signal_decision.final_agent_name,
            symbol=execution_decision.signal_decision.market_context.symbol,
            timeframe=timeframe_label,
            action=executable_signal.action,
            confidence=executable_signal.confidence,
            stop_loss=executable_signal.stop_loss,
            take_profit=executable_signal.take_profit,
            max_holding_minutes=executable_signal.max_holding_minutes,
            reason=executable_signal.reason,
        )
        await self.db.log_risk_decision(
            signal_id=signal_id,
            approved=execution_decision.risk_evaluation.approved,
            reason=execution_decision.risk_evaluation.reason,
            adjusted_volume=execution_decision.risk_evaluation.adjusted_volume,
        )
        await self.db.log_evaluation_journal(
            symbol=execution_decision.signal_decision.market_context.symbol,
            timeframe=timeframe_label,
            evaluation_mode=execution_decision.signal_decision.market_context.evaluation_mode,
            requested_agent_name=execution_decision.signal_decision.requested_agent_name,
            primary_agent_name=execution_decision.signal_decision.primary_agent_name,
            final_agent_name=execution_decision.signal_decision.final_agent_name,
            raw_technical_signal=execution_decision.signal_decision.primary_signal.model_dump(),
            executable_signal=execution_decision.signal_decision.final_signal.model_dump(),
            market_context_summary=execution_decision.signal_decision.market_context.summary(),
            gemini_assessment=execution_decision.signal_decision.gemini_confirmation.model_dump()
            if execution_decision.signal_decision.gemini_confirmation
            else None,
            trade_quality=execution_decision.trade_quality_assessment.model_dump(),
            portfolio_risk=execution_decision.portfolio_risk_assessment.model_dump(),
            anti_churn=execution_decision.anti_churn_assessment.model_dump(),
            risk_evaluation=execution_decision.risk_evaluation.model_dump(),
            execution_decision=execution_decision.model_dump(),
            position_management_plan=execution_decision.position_management_plan.model_dump(),
            signal_id=signal_id,
        )
        if self.research_repository is not None:
            context = execution_decision.signal_decision.market_context
            final_signal = execution_decision.signal_decision.final_signal
            meta = final_signal.metadata or {}
            gemini = execution_decision.signal_decision.gemini_confirmation
            sessions = ((context.user_policy or {}).get("session_filters") or [])
            candidate_id = f"{context.symbol}-{signal_id}-{int(time.time()*1000)}"
            rejection_reasons: list[str] = []
            if not execution_decision.allow_execute:
                rejection_reasons.append(execution_decision.reason)
                rejection_reasons.extend(execution_decision.risk_evaluation.machine_reasons or [])

            await self.research_repository.log_candidate_with_features(
                candidate_id=candidate_id,
                signal_id=signal_id,
                candidate_payload={
                    "symbol": context.symbol,
                    "asset_class": context.symbol_info.category if context.symbol_info else "Other",
                    "strategy_mode": str((context.user_policy or {}).get("mode", "balanced")),
                    "session": ",".join(sessions),
                    "day_of_week": time.gmtime().tm_wday,
                    "technical_direction": final_signal.action,
                    "smart_agent_summary": execution_decision.signal_decision.primary_signal.reason or "",
                    "gemini_summary": (gemini.summary_reason if gemini else "") or "",
                    "quality_score": execution_decision.trade_quality_assessment.final_trade_quality_score,
                    "confidence_score": final_signal.confidence,
                    "trend_h1": str(meta.get("h1_trend", "")),
                    "trend_h4": str(meta.get("h4_trend", "")),
                    "stop_loss": final_signal.stop_loss or 0.0,
                    "take_profit": final_signal.take_profit or 0.0,
                    "reward_risk": self._safe_float(meta.get("reward_risk_ratio"), 0.0),
                    "spread_at_eval": self._safe_float(context.tick.get("spread"), 0.0) if context.tick else 0.0,
                    "atr_regime": str(meta.get("atr_regime", "")),
                    "support_resistance_context": str(meta.get("sr_context", "")),
                    "event_id": (
                        ",".join((meta.get("event_context") or {}).get("event_ids", []))
                        or str(((context.normalized_news[0].metadata or {}).get("external_event_id", "") if context.normalized_news else ""))
                    ),
                    "event_type": str((context.normalized_news[0].category if context.normalized_news else "")),
                    "event_importance": str(getattr(gemini, "macro_relevance", "") if gemini else ""),
                    "contradiction_flag": bool(getattr(gemini, "contradiction_flag", False)) if gemini else False,
                    "event_bias": str((meta.get("event_context") or {}).get("bias", "neutral")),
                    "event_risk": str((meta.get("event_context") or {}).get("event_risk", "low")),
                    "event_confidence_adjustment": self._safe_float((meta.get("event_context") or {}).get("confidence_adjustment"), 0.0),
                    "strategy_advisor_used": bool((meta.get("strategy_advisory") or {}).get("used", False)),
                    "strategy_advisor_summary": str((meta.get("strategy_advisory") or {}).get("summary_reason", "")),
                    "risk_decision": execution_decision.risk_evaluation.reason,
                    "rejection_reasons": list(dict.fromkeys([r for r in rejection_reasons if r])),
                    # This row is a candidate journal entry at evaluation time.
                    # Mark actual execution only after a real ticket/fill is recorded.
                    "executed": False,
                    "gemini_changed_decision": bool(gemini and gemini.used),
                    "meta_model_changed_decision": bool((meta.get("meta_model") or {}).get("changed_decision", False)),
                },
                feature_snapshot=build_feature_snapshot(execution_decision),
            )
        return signal_id

