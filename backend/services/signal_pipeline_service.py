from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

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
        self.trade_decision_service = trade_decision_service or TradeDecisionService(
            trade_quality_service=self.trade_quality_service,
        )
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
        resolved = self.universe_service.resolve_requested_symbols([symbol], market_symbols)
        broker_symbol = resolved[0] if resolved else symbol
        raw = self.market_data.get_symbol_info(broker_symbol)
        if raw is None:
            return True
        tick_model = self.market_data.get_tick(broker_symbol)
        tick = tick_model.model_dump() if tick_model else None
        symbol_info = self._normalize_symbol_info(broker_symbol, tick)
        if symbol_info is None:
            return True
        profile = self.profile_service.resolve_profile(symbol, symbol_info)
        user_policy = self.risk_engine.user_policy.model_dump() if hasattr(self.risk_engine, "user_policy") else {}
        policy_sessions = list(user_policy.get("session_filters") or [])
        sessions = policy_sessions or list(profile.allowed_sessions or [])
        canonical_symbol = self.universe_service.canonical_symbol(symbol)
        try:
            return self.trade_quality_service.session_allowed(
                sessions,
                symbol=canonical_symbol,
                category=profile.category,
                profile_name=profile.profile_name,
            )
        except TypeError:
            # Backward compatibility with older implementations that only
            # accept the sessions list.
            return self.trade_quality_service.session_allowed(sessions)

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
        desc = (raw.get("description") or "").lower()
        path = (raw.get("path") or "").lower()

        is_commodity = (
            name in {"GOLD", "WTI", "BRENT", "NATGAS"}
            or name.startswith(("XAU", "XAG", "XPT", "XPD"))
            or any(token in desc for token in ("gold", "silver", "oil", "crude", "brent"))
        )
        is_index = name in {"US30", "US500", "NAS100", "US100", "GER40", "UK100", "JPN225", "AUS200"}
        is_crypto = any(token in path for token in ("crypto", "bitcoin", "eth"))
        is_stock = any(token in path for token in ("stock", "share", "equity")) or (
            "cfd" in path and not is_commodity and not is_index
        )
        is_forex = len(name) == 6 and name.isalpha() and not is_commodity and not is_index and not is_crypto

        if is_crypto:
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

        normalized = NormalizedSymbolInfo(
            name=name or raw.get("name", canonical_symbol),
            description=raw.get("description", ""),
            category=category,
            path=raw.get("path", ""),
            point=raw.get("point", 0.0),
            digits=raw.get("digits", 5),
            trade_contract_size=raw.get("trade_contract_size", 0.0),
            volume_min=raw.get("volume_min", 0.01),
            volume_max=raw.get("volume_max", 0.0),
            volume_step=raw.get("volume_step", 0.01),
            trade_stops_level=raw.get("trade_stops_level", 0.0),
            visible=raw.get("visible", False),
            trade_enabled=bool(tick and tick.get("bid", 0) > 0),
            spread=tick.get("spread", 0.0) if tick else 0.0,
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
        market_candidates = self.universe_service.filter_market_symbols(
            self.market_data.get_tradeable_symbols() or self.market_data.get_visible_symbols(),
            include_inactive=True,
        )
        resolved_symbols = self.universe_service.resolve_requested_symbols(
            [symbol],
            market_candidates,
        )
        broker_symbol = resolved_symbols[0] if resolved_symbols else (canonical_symbol or symbol)
        self.market_data.enable_symbol(broker_symbol)

        tick_model = self.market_data.get_tick(broker_symbol)
        tick = tick_model.model_dump() if tick_model else None
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
        if not self.universe_service.is_symbol_enabled(canonical_symbol or symbol):
            degraded.append(self.universe_service.inactive_reason(canonical_symbol or symbol))

        symbol_info = self._normalize_symbol_info(broker_symbol, tick)
        profile = self.profile_service.resolve_profile(canonical_symbol or broker_symbol, symbol_info) if symbol_info else None
        if symbol_info and not self.universe_service.is_symbol_active(canonical_symbol or broker_symbol, symbol_info.category):
            degraded.append(self.universe_service.inactive_reason(canonical_symbol or broker_symbol, symbol_info.category))

        return MarketContext(
            symbol=broker_symbol,
            requested_timeframe=requested_timeframe,
            evaluation_mode=evaluation_mode,
            user_policy=self.risk_engine.user_policy.model_dump() if hasattr(self.risk_engine, "user_policy") else None,
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
        if not requested_agent_name or requested_agent_name == "GeminiAgent":
            return "SmartAgent"
        return requested_agent_name

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

        bid = float(context.tick.get("bid", 0.0) or 0.0)
        ask = float(context.tick.get("ask", 0.0) or 0.0)
        tick_time = float(context.tick.get("time", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return "Market closed (no executable live bid/ask)."
        if tick_time <= 0:
            return "Market data timestamp unavailable."

        age_seconds = max(0.0, time.time() - tick_time)
        max_age = self._max_tick_age_seconds(context)
        if age_seconds > max_age:
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
        return {
            "symbol": context.symbol,
            "timeframe": context.requested_timeframe,
            "evaluation_mode": context.evaluation_mode,
            "user_policy": context.user_policy or {},
            "signal": {
                "action": final_signal.action,
                "confidence": float(final_signal.confidence or 0.0),
                "strategy": final_signal.strategy,
                "reason": final_signal.reason,
                "stop_loss": final_signal.stop_loss,
                "take_profit": final_signal.take_profit,
            },
            "quality": {
                "score": float(qa.final_trade_quality_score or 0.0),
                "threshold": float(qa.threshold or 0.0),
                "projected_margin_after_costs_pct": float(qa.projected_margin_after_costs_pct or 0.0),
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
        cloud_confidence = cloud_decision.get("confidence")
        try:
            cloud_confidence = float(cloud_confidence) if cloud_confidence is not None else None
        except (TypeError, ValueError):
            cloud_confidence = None

        # Safety rule: opposite-direction cloud command is converted to HOLD.
        if requested_action in {"BUY", "SELL"} and requested_action != final_signal.action:
            final_signal.action = "HOLD"
            final_signal.reason = f"{final_signal.reason} Cloud brain opposed local direction; holding for safety."
            if cloud_confidence is not None:
                final_signal.confidence = max(0.0, min(0.95, cloud_confidence))
            cloud_decision["applied_action"] = "HOLD"
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
            notes=["Trading blocked before signal generation because market appears closed or stale."],
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
        # Protect free-tier Gemini quota: avoid per-symbol Gemini calls in bulk
        # scan/auto loops unless the user explicitly requires confirmation mode.
        if evaluation_mode in {"scan", "auto"} and gemini_role != "confirmation-required":
            return False
        if gemini_role == "confirmation-required":
            return True
        if requested_agent_name == "GeminiAgent":
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
        if not context.normalized_news:
            return {
                "news_items": 0,
                "bias": "neutral",
                "event_risk": "low",
                "confidence_adjustment": 0.0,
                "contradiction_flag": False,
                "event_ids": [],
            }

        direction = "bullish" if signal.action == "BUY" else "bearish"
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        bias_score = 0.0
        weight_total = 0.0
        strongest_risk = "low"
        event_ids: list[str] = []

        for item in context.normalized_news[:8]:
            meta = item.metadata or {}
            event_id = str(meta.get("external_event_id", "") or "")
            if event_id:
                event_ids.append(event_id)
            bias = str(meta.get("gemini_bias", "neutral")).lower()
            risk = str(meta.get("gemini_event_risk", "low")).lower()
            if risk not in {"low", "medium", "high"}:
                risk = "low"
            w = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(risk, 0.3)
            weight_total += w
            if bias == direction:
                bias_score += 1.0 * w
            elif bias == "neutral":
                bias_score += 0.0
            else:
                bias_score -= 1.0 * w
            if risk_rank[risk] > risk_rank[strongest_risk]:
                strongest_risk = risk

        if weight_total <= 0:
            return {
                "news_items": len(context.normalized_news),
                "bias": "neutral",
                "event_risk": strongest_risk,
                "confidence_adjustment": 0.0,
                "contradiction_flag": False,
                "event_ids": list(dict.fromkeys(event_ids)),
            }

        normalized_bias = bias_score / weight_total
        if normalized_bias > 0.15:
            bias = direction
        elif normalized_bias < -0.15:
            bias = "bearish" if direction == "bullish" else "bullish"
        else:
            bias = "neutral"

        contradiction_flag = bias not in {direction, "neutral"}
        base_adjustment = max(-0.06, min(0.06, normalized_bias * 0.06))
        if strongest_risk == "high":
            base_adjustment = min(base_adjustment, 0.0)
            base_adjustment -= 0.01
        return {
            "news_items": len(context.normalized_news),
            "bias": bias,
            "event_risk": strongest_risk,
            "confidence_adjustment": round(max(-0.08, min(0.08, base_adjustment)), 3),
            "contradiction_flag": contradiction_flag,
            "event_ids": list(dict.fromkeys(event_ids)),
        }

    def _apply_event_context_advisory(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
    ) -> TechnicalSignal:
        event_ctx = self._extract_event_context(context, signal)
        adjusted = signal.model_copy(deep=True)
        adjusted.confidence = round(
            max(0.0, min(0.95, adjusted.confidence + float(event_ctx.get("confidence_adjustment", 0.0)))),
            2,
        )
        meta = dict(adjusted.metadata or {})
        meta["event_context"] = event_ctx
        adjusted.metadata = meta
        if event_ctx.get("news_items", 0) <= 0:
            return adjusted
        if event_ctx.get("contradiction_flag"):
            adjusted.reason = (
                f"{adjusted.reason} Event context contradicts direction; confidence reduced."
            )
        elif event_ctx.get("bias") in {"bullish", "bearish"}:
            adjusted.reason = (
                f"{adjusted.reason} Event context aligns {event_ctx.get('bias')}."
            )
        return adjusted

    def _should_use_strategy_advisor(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        evaluation_mode: str,
    ) -> bool:
        if signal.action not in {"BUY", "SELL"}:
            return False
        if signal.confidence < 0.52:
            return False
        policy = context.user_policy or {}
        if str(policy.get("gemini_role", "advisory")).lower() == "off":
            return False
        # Keep scan loops responsive and quota-friendly.
        if evaluation_mode == "scan":
            return False
        return True

    def _clamp_strategy_advisory(self, context: MarketContext, advisory: dict) -> dict:
        mode = str((context.user_policy or {}).get("mode", "balanced")).lower()
        mode_bounds = {
            "safe": {"sl_min": 0.9, "sl_max": 1.8, "tp_min": 1.8, "tp_max": 3.0, "hold_max": 180},
            "balanced": {"sl_min": 0.9, "sl_max": 2.2, "tp_min": 1.8, "tp_max": 3.6, "hold_max": 240},
            "aggressive": {"sl_min": 0.8, "sl_max": 2.6, "tp_min": 1.6, "tp_max": 4.2, "hold_max": 360},
        }.get(mode, {"sl_min": 0.9, "sl_max": 2.2, "tp_min": 1.8, "tp_max": 3.6, "hold_max": 240})
        min_rr = float((context.user_policy or {}).get("min_reward_risk", 1.8) or 1.8)

        clamped = dict(advisory or {})
        clamped["sl_atr_multiplier"] = max(
            mode_bounds["sl_min"],
            min(mode_bounds["sl_max"], float(clamped.get("sl_atr_multiplier", 1.5) or 1.5)),
        )
        clamped["tp_atr_multiplier"] = max(
            mode_bounds["tp_min"],
            min(mode_bounds["tp_max"], float(clamped.get("tp_atr_multiplier", 2.5) or 2.5)),
        )
        clamped["max_hold_minutes"] = max(
            30,
            min(mode_bounds["hold_max"], int(float(clamped.get("max_hold_minutes", 120) or 120))),
        )
        clamped["confidence_adjustment"] = max(
            -0.08,
            min(0.05, float(clamped.get("confidence_adjustment", 0.0) or 0.0)),
        )
        # Never let advisory reduce configured minimum reward:risk discipline.
        if clamped["tp_atr_multiplier"] < clamped["sl_atr_multiplier"] * min_rr:
            clamped["tp_atr_multiplier"] = round(clamped["sl_atr_multiplier"] * min_rr, 3)
        return clamped

    def _apply_strategy_advisory(
        self,
        *,
        context: MarketContext,
        signal: TechnicalSignal,
        advisory: dict,
    ) -> TechnicalSignal:
        adjusted = signal.model_copy(deep=True)
        clamped = self._clamp_strategy_advisory(context, advisory)

        adjusted.confidence = round(
            max(0.0, min(0.95, adjusted.confidence + float(clamped.get("confidence_adjustment", 0.0)))),
            2,
        )

        metadata = dict(adjusted.metadata or {})
        metadata["strategy_advisory"] = clamped
        adjusted.metadata = metadata

        if adjusted.action not in {"BUY", "SELL"}:
            return adjusted
        if not context.tick:
            return adjusted

        entry = self._entry_price(context, adjusted.action)
        if entry <= 0:
            return adjusted
        if adjusted.stop_loss is None or adjusted.take_profit is None:
            adjusted.max_holding_minutes = int(clamped.get("max_hold_minutes", adjusted.max_holding_minutes or 120))
            return adjusted

        current_sl_dist = abs(entry - float(adjusted.stop_loss))
        current_tp_dist = abs(float(adjusted.take_profit) - entry)
        atr = float(metadata.get("atr", 0.0) or 0.0)
        if atr <= 0:
            atr = current_sl_dist

        target_sl_dist = atr * float(clamped.get("sl_atr_multiplier", 1.5))
        target_tp_dist = atr * float(clamped.get("tp_atr_multiplier", 2.5))

        # Keep advisory conservative: do not loosen stop distance materially.
        max_sl_dist = current_sl_dist * 1.05 if current_sl_dist > 0 else target_sl_dist
        min_sl_dist = current_sl_dist * 0.70 if current_sl_dist > 0 else target_sl_dist
        sl_dist = max(min_sl_dist, min(max_sl_dist, target_sl_dist))

        min_rr = float((context.user_policy or {}).get("min_reward_risk", 1.8) or 1.8)
        tp_dist = max(target_tp_dist, sl_dist * min_rr)
        if current_tp_dist > 0:
            tp_dist = max(tp_dist, current_tp_dist * 0.9)

        digits = int(metadata.get("digits", 5) or 5)
        if adjusted.action == "BUY":
            adjusted.stop_loss = round(entry - sl_dist, digits)
            adjusted.take_profit = round(entry + tp_dist, digits)
        else:
            adjusted.stop_loss = round(entry + sl_dist, digits)
            adjusted.take_profit = round(entry - tp_dist, digits)

        adjusted.max_holding_minutes = int(clamped.get("max_hold_minutes", adjusted.max_holding_minutes or 120))
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
            start = float(recent[0].get("close", 0.0))
            end = float(recent[-1].get("close", 0.0))
            if start <= 0:
                return "flat", 0.0
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
        current_entry = float(context.tick.get("ask", 0.0)) if current_action == "BUY" else float(context.tick.get("bid", 0.0))
        opposite_entry = float(context.tick.get("ask", 0.0)) if opposite_action == "BUY" else float(context.tick.get("bid", 0.0))
        if current_entry <= 0 or opposite_entry <= 0:
            return signal

        sl = signal.stop_loss
        tp = signal.take_profit
        sl_dist = abs(current_entry - float(sl)) if sl is not None else 0.0
        tp_dist = abs(float(tp) - current_entry) if tp is not None else 0.0

        flipped = signal.model_copy(deep=True)
        flipped.action = opposite_action
        flipped.confidence = round(max(0.45, min(0.95, float(signal.confidence) - 0.02)), 2)
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
    ) -> tuple[TechnicalSignal, list[str]]:
        if signal.action not in {"BUY", "SELL"}:
            return signal, []
        if not recent_outcomes:
            return signal, []

        same_dir = [o for o in recent_outcomes if str(o.get("action", "")).upper() == signal.action]
        opposite_action = "BUY" if signal.action == "SELL" else "SELL"
        opp_dir = [o for o in recent_outcomes if str(o.get("action", "")).upper() == opposite_action]
        same_losses = [o for o in same_dir[:4] if float(o.get("profit", 0.0)) <= 0.0]
        same_wins = [o for o in same_dir[:4] if float(o.get("profit", 0.0)) > 0.0]
        same_pnl = sum(float(o.get("profit", 0.0)) for o in same_dir[:4])
        opp_pnl = sum(float(o.get("profit", 0.0)) for o in opp_dir[:4])
        drift_dir, drift_pct = self._recent_drift(context)
        drift_opposes_signal = (
            (signal.action == "BUY" and drift_dir == "bearish")
            or (signal.action == "SELL" and drift_dir == "bullish")
        )

        reasons: list[str] = []
        adjusted = signal.model_copy(deep=True)

        if len(same_losses) >= 2 and len(same_wins) == 0 and same_pnl < 0:
            adjusted.confidence = round(max(0.0, adjusted.confidence - 0.10), 2)
            reasons.append(
                f"Outcome learning: recent {signal.action} trades on {context.symbol} are losing ({len(same_losses)} losses, pnl {same_pnl:.2f})."
            )

        if len(same_losses) >= 2 and drift_opposes_signal and drift_pct >= 0.03:
            adjusted = self._flip_signal_action(context, adjusted)
            reasons.append(
                f"Direction adapted: flipped to {adjusted.action} because last-hours drift is {drift_dir} ({drift_pct:.2f}%) and recent {signal.action} outcomes are negative."
            )
        elif adjusted.confidence < 0.45 and len(same_losses) >= 2:
            adjusted.action = "HOLD"
            reasons.append("Outcome learning: confidence reduced below execution floor after repeated same-direction losses.")

        if reasons:
            meta = dict(adjusted.metadata or {})
            meta["outcome_learning"] = {
                "same_direction_losses": len(same_losses),
                "same_direction_wins": len(same_wins),
                "same_direction_pnl": round(same_pnl, 2),
                "opposite_direction_pnl": round(opp_pnl, 2),
                "drift_direction": drift_dir,
                "drift_pct": round(drift_pct, 4),
            }
            adjusted.metadata = meta
            adjusted.reason = f"{adjusted.reason} {' '.join(reasons)}"

        return adjusted, reasons

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        if action == "BUY":
            return float(context.tick.get("ask", 0.0))
        if action == "SELL":
            return float(context.tick.get("bid", 0.0))
        return 0.0

    def _apply_mode_target_scaling(
        self,
        signal: TechnicalSignal,
        entry_price: float,
        mode: str,
    ) -> TechnicalSignal:
        if signal.action not in {"BUY", "SELL"}:
            return signal
        if not signal.stop_loss or not signal.take_profit or entry_price <= 0:
            return signal

        multipliers = {
            "safe": 0.95,
            "balanced": 1.00,
            "aggressive": 1.05,
        }
        multiplier = multipliers.get((mode or "balanced").lower(), 1.00)
        if abs(multiplier - 1.0) < 1e-6:
            return signal

        sl_distance = abs(entry_price - signal.stop_loss) * multiplier
        tp_distance = abs(signal.take_profit - entry_price) * multiplier
        digits = (signal.metadata or {}).get("digits")
        if not isinstance(digits, int):
            digits = 5

        if signal.action == "BUY":
            new_sl = round(entry_price - sl_distance, digits)
            new_tp = round(entry_price + tp_distance, digits)
        else:
            new_sl = round(entry_price + sl_distance, digits)
            new_tp = round(entry_price - tp_distance, digits)

        updated = signal.model_copy(deep=True)
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
        requested = int(requested_base)
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
        atr_pct = float(meta.get("atr_pct", 0.0))
        if profile and atr_pct >= profile.min_atr_pct * 1.5:
            atr_regime = "high"
        elif profile and atr_pct >= profile.min_atr_pct:
            atr_regime = "normal"

        thesis = (
            f"{signal.action} thesis: {meta.get('h1_trend', 'flat')} H1 / {meta.get('h4_trend', 'flat')} H4 "
            f"alignment, entry={meta.get('entry_signal', 'none')}, quality={trade_quality.final_trade_quality_score:.2f}."
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
        if not self.universe_service.is_symbol_enabled(context.symbol):
            reason = self.universe_service.inactive_reason(context.symbol)
            return self._inactive_universe_decision(
                context=context,
                requested_agent_name=requested_agent_name or "SmartAgent",
                reason=reason,
            )
        if context.symbol_info and not self.universe_service.is_symbol_active(context.symbol, context.symbol_info.category):
            reason = self.universe_service.inactive_reason(context.symbol, context.symbol_info.category)
            return self._inactive_universe_decision(
                context=context,
                requested_agent_name=requested_agent_name or "SmartAgent",
                reason=reason,
            )
        market_guard_reason: str | None = None
        if evaluation_mode in {"manual", "scan", "auto"}:
            # Keep technical analysis running so UI still gets confidence/quality,
            # but block execution later when market is closed/stale.
            market_guard_reason = self._market_live_guard_reason(context)
        primary_agent_name = self._resolve_primary_agent_name(requested_agent_name)
        primary_agent = self.agents.get(primary_agent_name)
        if primary_agent is None:
            raise ValueError(f"Agent '{primary_agent_name}' not found")

        input_data = self._build_agent_input(context, primary_agent_name)
        raw_signal = primary_agent.evaluate(input_data)
        primary_signal = TechnicalSignal.from_trade_signal(raw_signal, primary_agent_name)
        if context.symbol_info:
            signal_meta = dict(primary_signal.metadata or {})
            signal_meta.setdefault("point", context.symbol_info.point)
            signal_meta.setdefault("digits", context.symbol_info.digits)
            signal_meta.setdefault("category", context.symbol_info.category)
            primary_signal.metadata = signal_meta

        gemini_assessment: Optional[GeminiAssessment] = None
        if self._should_use_gemini(requested_agent_name, evaluation_mode, context):
            if self.event_ingestion_service is not None:
                await self.event_ingestion_service.maybe_refresh_latest(
                    min_interval_seconds=300.0 if evaluation_mode in {"auto", "scan"} else 900.0,
                    # Keep evaluation responsive: background refresh should not block
                    # per-symbol decisions on bulk Gemini event classification.
                    classify_with_gemini=False,
                )
                stored_news = await self.event_ingestion_service.recent_symbol_news(
                    context.symbol,
                    universe_service=self.universe_service,
                    limit=10,
                )
                if stored_news:
                    context = context.model_copy(update={"normalized_news": stored_news})
                    input_data = self._build_agent_input(context, primary_agent_name)
            if not context.normalized_news:
                normalized_news = await self.news_ingestion_service.ingest_for_context(context)
                context = context.model_copy(update={"normalized_news": normalized_news})
                input_data = self._build_agent_input(context, primary_agent_name)
            gemini_assessment = await self._assess_news(context, input_data, primary_signal)
        # Deterministic event-aware advisory path (used even when Gemini is off).
        primary_signal = self._apply_event_context_advisory(context, primary_signal)
        strategy_advisory: dict = {
            "used": False,
            "available": bool(getattr(self.gemini_strategy_advisor_service, "available", False)),
            "degraded": False,
            "summary_reason": "Strategy advisor skipped.",
        }
        if self._should_use_strategy_advisor(
            context=context,
            signal=primary_signal,
            evaluation_mode=evaluation_mode,
        ):
            event_context = (primary_signal.metadata or {}).get("event_context", {})
            strategy_advisory = await self.gemini_strategy_advisor_service.assess(
                context=context,
                technical_signal=primary_signal,
                gemini_assessment=gemini_assessment,
                event_context=event_context,
            )
            primary_signal = self._apply_strategy_advisory(
                context=context,
                signal=primary_signal,
                advisory=strategy_advisory,
            )

        recent_outcomes = []
        recent_evaluations = []
        # Manual dashboard scans should stay responsive; skip heavy learning-history
        # lookups and use direct technical + risk evaluation only.
        if self.db is not None and evaluation_mode != "manual":
            lookback_minutes = max(self.risk_engine.settings.cooldown_minutes_per_symbol, 1440)
            recent_outcomes = await self.db.get_recent_symbol_outcomes(
                context.symbol,
                limit=5,
                within_minutes=lookback_minutes,
            )
            recent_evaluations = await self.db.get_recent_symbol_evaluations(
                context.symbol,
                limit=5,
                within_minutes=lookback_minutes,
            )

        preview_portfolio_fit = self.risk_service.preview_portfolio_fit(context)
        learned_signal, learning_reasons = self._apply_outcome_learning(
            context=context,
            signal=primary_signal,
            recent_outcomes=recent_outcomes,
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
            trade_decision, meta_assessment = await self.meta_model_service.assess_trade_decision(
                context=context,
                trade_decision=trade_decision,
                gemini_assessment=gemini_assessment,
                portfolio_risk_assessment=PortfolioRiskAssessment(
                    portfolio_fit_score=preview_portfolio_fit,
                ),
                anti_churn_blocked=False,
            )
        if self.cloud_brain_decision_client is not None:
            payload = self._build_cloud_decision_payload(
                context=context,
                trade_decision=trade_decision,
                gemini_assessment=gemini_assessment,
                meta_assessment=meta_assessment,
            )
            cloud_decision = await self.cloud_brain_decision_client.decide(payload)
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
                trade_decision, meta_assessment = await self.meta_model_service.assess_trade_decision(
                    context=context,
                    trade_decision=trade_decision,
                    gemini_assessment=gemini_assessment,
                    portfolio_risk_assessment=risk_approval.portfolio_risk_assessment,
                    anti_churn_blocked=risk_approval.anti_churn_assessment.blocked,
                )

        blocking_reasons: list[str] = []
        if market_guard_reason:
            blocking_reasons.append(market_guard_reason)
        if not trade_decision.trade:
            blocking_reasons.extend(
                trade_decision.trade_quality_assessment.no_trade_reasons or trade_decision.reasons
            )
        if not risk_approval.approved:
            blocking_reasons.extend(risk_approval.reasons)

        final_signal = trade_decision.final_signal
        if blocking_reasons and final_signal.action in {"BUY", "SELL"}:
            final_signal = self._downgrade_signal(final_signal, blocking_reasons)
        if market_guard_reason:
            # Closed/stale market must never appear as a tradable confidence setup.
            final_signal = final_signal.model_copy(
                update={
                    "action": "HOLD",
                    "confidence": 0.0,
                    "reason": f"{final_signal.reason} Market closed guard: {market_guard_reason}",
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
        final_agent_parts: list[str] = [primary_agent_name]
        if gemini_assessment and gemini_assessment.used and not gemini_assessment.degraded:
            final_agent_parts.append("Gemini")
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

        def _non_stock_fallback(symbols: list[str]) -> list[str]:
            preferred: list[str] = []
            for symbol in symbols:
                raw = self.market_data.get_symbol_info(symbol)
                if raw is None:
                    continue
                tick_model = self.market_data.get_tick(symbol)
                tick = tick_model.model_dump() if tick_model else None
                normalized = self._normalize_symbol_info(symbol, tick)
                if normalized and normalized.category in {"Indices", "Commodities"}:
                    preferred.append(symbol)
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
                    fallback = self.universe_service.default_auto_trade_symbols(market_symbols, limit=limit)
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
                        return merged[:limit]
                    # If event feed is too narrow (for example only GOLD),
                    # augment with curated symbols to avoid single-symbol lock-in.
                    merged: list[str] = []
                    for symbol in eligible + commodity_eligible + fallback_eligible:
                        if symbol not in merged:
                            merged.append(symbol)
                    return merged[:limit]
                return _non_stock_fallback(event_symbols)
        fallback = self.universe_service.default_auto_trade_symbols(market_symbols, limit=limit)
        discovered_commodities = _market_commodity_candidates(market_symbols)
        eligible_fallback = _filter_session_eligible(fallback)
        commodity_eligible = _filter_session_eligible(discovered_commodities)
        merged_fallback: list[str] = []
        for symbol in commodity_eligible + eligible_fallback:
            if symbol not in merged_fallback:
                merged_fallback.append(symbol)
        if merged_fallback:
            return merged_fallback[:limit]
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
                    "reward_risk": float(meta.get("reward_risk_ratio", 0.0) or 0.0),
                    "spread_at_eval": float(context.tick.get("spread", 0.0)) if context.tick else 0.0,
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
                    "event_confidence_adjustment": float((meta.get("event_context") or {}).get("confidence_adjustment", 0.0) or 0.0),
                    "strategy_advisor_used": bool((meta.get("strategy_advisory") or {}).get("used", False)),
                    "strategy_advisor_summary": str((meta.get("strategy_advisory") or {}).get("summary_reason", "")),
                    "risk_decision": execution_decision.risk_evaluation.reason,
                    "rejection_reasons": list(dict.fromkeys([r for r in rejection_reasons if r])),
                    "executed": bool(execution_decision.allow_execute),
                    "gemini_changed_decision": bool(gemini and gemini.used),
                    "meta_model_changed_decision": bool((meta.get("meta_model") or {}).get("changed_decision", False)),
                },
                feature_snapshot=build_feature_snapshot(execution_decision),
            )
        return signal_id
