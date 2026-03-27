from __future__ import annotations

import logging
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
from services.news_ingestion_service import NewsIngestionService
from services.event_ingestion_service import EventIngestionService
from services.portfolio_risk_service import PortfolioRiskService
from services.risk_service import RiskService
from services.symbol_profile_service import SymbolProfileService
from services.symbol_universe_service import SymbolUniverseService
from services.trade_decision_service import TradeDecisionService
from services.trade_quality_service import TradeQualityService

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
        trade_decision_service: Optional[TradeDecisionService] = None,
        risk_service: Optional[RiskService] = None,
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
        self.trade_decision_service = trade_decision_service or TradeDecisionService(
            trade_quality_service=self.trade_quality_service,
        )
        self.risk_service = risk_service or RiskService(
            risk_engine=self.risk_engine,
            portfolio_risk_service=self.portfolio_risk_service,
            anti_churn_service=self.anti_churn_service,
            execution_engine=self.execution,
        )

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
        if gemini_role == "confirmation-required":
            return True
        if requested_agent_name == "GeminiAgent":
            return True
        return evaluation_mode in {"scan", "auto", "replay"}

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

    def _entry_price(self, context: MarketContext, action: str) -> float:
        if not context.tick:
            return 0.0
        if action == "BUY":
            return float(context.tick.get("ask", 0.0))
        if action == "SELL":
            return float(context.tick.get("bid", 0.0))
        return 0.0

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
        primary_agent_name = self._resolve_primary_agent_name(requested_agent_name)
        primary_agent = self.agents.get(primary_agent_name)
        if primary_agent is None:
            raise ValueError(f"Agent '{primary_agent_name}' not found")

        input_data = self._build_agent_input(context, primary_agent_name)
        raw_signal = primary_agent.evaluate(input_data)
        primary_signal = TechnicalSignal.from_trade_signal(raw_signal, primary_agent_name)

        gemini_assessment: Optional[GeminiAssessment] = None
        if self._should_use_gemini(requested_agent_name, evaluation_mode, context):
            if self.event_ingestion_service is not None:
                await self.event_ingestion_service.maybe_refresh_latest(
                    min_interval_seconds=300.0 if evaluation_mode in {"auto", "scan"} else 900.0,
                    classify_with_gemini=True,
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

        recent_outcomes = []
        recent_evaluations = []
        if self.db is not None:
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
        trade_decision = self.trade_decision_service.decide(
            context=context,
            technical_signal=primary_signal,
            gemini_assessment=gemini_assessment,
            portfolio_fit_score=preview_portfolio_fit,
        )

        entry_price = self._entry_price(context, trade_decision.final_signal.action)
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

        blocking_reasons: list[str] = []
        if not trade_decision.trade:
            blocking_reasons.extend(
                trade_decision.trade_quality_assessment.no_trade_reasons or trade_decision.reasons
            )
        if not risk_approval.approved:
            blocking_reasons.extend(risk_approval.reasons)

        final_signal = trade_decision.final_signal
        if blocking_reasons and final_signal.action in {"BUY", "SELL"}:
            final_signal = self._downgrade_signal(final_signal, blocking_reasons)

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

        signal_decision = SignalDecision(
            requested_agent_name=requested_agent_name or primary_agent_name,
            primary_agent_name=primary_agent_name,
            final_agent_name="SmartAgent+Gemini"
            if gemini_assessment and gemini_assessment.used and not gemini_assessment.degraded
            else primary_agent_name,
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
                classify_with_gemini=True,
            )
            event_symbols = await self.event_ingestion_service.latest_candidate_symbols(
                universe_service=self.universe_service,
                market_symbols=market_symbols,
                limit=limit,
            )
            if event_symbols:
                eligible = _filter_session_eligible(event_symbols)
                if eligible:
                    if len(eligible) >= limit:
                        return eligible[:limit]
                    # If event feed is too narrow (for example only GOLD),
                    # augment with curated symbols to avoid single-symbol lock-in.
                    fallback = self.universe_service.default_auto_trade_symbols(market_symbols, limit=limit)
                    fallback_eligible = _filter_session_eligible(fallback)
                    merged: list[str] = []
                    for symbol in eligible + fallback_eligible:
                        if symbol not in merged:
                            merged.append(symbol)
                    return merged[:limit]
                return _non_stock_fallback(event_symbols)
        fallback = self.universe_service.default_auto_trade_symbols(market_symbols, limit=limit)
        eligible_fallback = _filter_session_eligible(fallback)
        if eligible_fallback:
            return eligible_fallback[:limit]
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
        return signal_id
