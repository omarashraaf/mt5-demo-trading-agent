from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from agent.interface import TradeSignal


ExecutionStatus = Literal["pass", "warn", "block"]


class SymbolProfile(BaseModel):
    profile_name: str
    category: str
    max_spread: float
    min_atr_pct: float
    max_hold_minutes: int
    news_hold_min_minutes: int = 30
    news_hold_max_minutes: int = 90
    technical_hold_min_minutes: int = 60
    technical_hold_max_minutes: int = 180
    min_reward_risk: float
    max_positions_per_category: int
    quality_threshold: float
    cooldown_minutes: int
    news_weight: float
    allowed_sessions: list[str] = Field(default_factory=list)
    default_sl_atr_multiplier: float = 1.5
    default_tp_atr_multiplier: float = 2.5
    force_close_before_session_end: bool = False
    allow_overnight_hold: bool = False
    session_close_buffer_minutes: int = 10
    sector: str = "Other"
    theme_bucket: str = "Other"
    usd_beta_weight: float = 0.0


class NormalizedSymbolInfo(BaseModel):
    name: str
    description: str = ""
    category: str = "Other"
    path: str = ""
    point: float = 0.0
    digits: int = 5
    trade_contract_size: float = 0.0
    volume_min: float = 0.01
    volume_max: float = 0.0
    volume_step: float = 0.01
    trade_stops_level: float = 0.0
    visible: bool = False
    trade_enabled: bool = False
    spread: float = 0.0
    sector: str = "Other"
    theme_bucket: str = "Other"
    base_currency: str = ""
    quote_currency: str = ""
    usd_beta_weight: float = 0.0
    correlation_tags: list[str] = Field(default_factory=list)


class NormalizedNewsItem(BaseModel):
    source: str
    category: Literal["headline", "macro_event", "calendar"] = "headline"
    title: str
    summary: str = ""
    published_at: float
    received_at: float
    affected_symbols: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


EventImportance = Literal["low", "medium", "high"]
EventType = Literal["economic_calendar", "market_news", "company_news", "macro_event", "unknown"]
AssetBias = Literal["bullish", "bearish", "neutral"]
EventPersistence = Literal["short", "medium", "structural"]


class ExternalEvent(BaseModel):
    source: str
    source_event_id: str
    dedupe_key: str
    title: str
    summary: str = ""
    timestamp_utc: float
    event_type: EventType = "unknown"
    category: str = ""
    country: str = ""
    importance: EventImportance = "medium"
    actual: Any | None = None
    forecast: Any | None = None
    previous: Any | None = None
    affected_assets: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    fetched_at: float = 0.0
    usable: bool = True
    usability_reason: str = ""


class CandidateAssetMapping(BaseModel):
    symbol: str
    baseline_bias: AssetBias = "neutral"
    needs_gemini_clarification: bool = True
    tradable: bool = False
    mapping_score: float = 0.0
    reason: str = ""


class GeminiEventAssessment(BaseModel):
    used: bool = False
    available: bool = False
    degraded: bool = False
    event_type: str = "unknown"
    affected_assets: list[str] = Field(default_factory=list)
    importance: EventImportance = "medium"
    bias_by_asset: dict[str, AssetBias] = Field(default_factory=dict)
    persistence_horizon: EventPersistence = "short"
    event_risk: EventImportance = "low"
    confidence_adjustment: float = 0.0
    contradiction_flag: bool = False
    summary_reason: str = ""
    error: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class MarketContext(BaseModel):
    symbol: str
    requested_timeframe: str = "H1"
    evaluation_mode: str = "manual"
    user_policy: Optional[dict[str, Any]] = None
    symbol_info: Optional[NormalizedSymbolInfo] = None
    profile: Optional[SymbolProfile] = None
    tick: Optional[dict[str, Any]] = None
    account_balance: float = 0.0
    account_equity: float = 0.0
    account_margin: float = 0.0
    account_free_margin: float = 0.0
    account_currency: str = ""
    account_leverage: int = 0
    bars_by_timeframe: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    symbol_open_positions: list[dict[str, Any]] = Field(default_factory=list)
    all_open_positions: list[dict[str, Any]] = Field(default_factory=list)
    normalized_news: list[NormalizedNewsItem] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "evaluation_mode": self.evaluation_mode,
            "requested_timeframe": self.requested_timeframe,
            "policy_mode": (self.user_policy or {}).get("mode"),
            "category": self.symbol_info.category if self.symbol_info else "Other",
            "profile": self.profile.profile_name if self.profile else None,
            "spread": self.tick.get("spread", 0.0) if self.tick else 0.0,
            "account_equity": self.account_equity,
            "account_margin": self.account_margin,
            "account_free_margin": self.account_free_margin,
            "degraded_reasons": self.degraded_reasons,
            "bar_counts": {
                timeframe: len(bars)
                for timeframe, bars in self.bars_by_timeframe.items()
            },
            "news_items": len(self.normalized_news),
        }


class TechnicalSignal(BaseModel):
    agent_name: str
    action: str
    confidence: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    max_holding_minutes: Optional[int] = None
    reason: str = ""
    strategy: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_trade_signal(
        cls,
        signal: TradeSignal,
        agent_name: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "TechnicalSignal":
        merged_metadata = dict(signal.metadata or {})
        if metadata:
            merged_metadata.update(metadata)
        return cls(
            agent_name=agent_name,
            action=signal.action,
            confidence=signal.confidence,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            max_holding_minutes=signal.max_holding_minutes,
            reason=signal.reason,
            strategy=signal.strategy,
            metadata=merged_metadata,
        )

    def to_trade_signal(self) -> TradeSignal:
        return TradeSignal(
            action=self.action,
            confidence=self.confidence,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            max_holding_minutes=self.max_holding_minutes,
            reason=self.reason,
            strategy=self.strategy,
            metadata=self.metadata,
        )


class GeminiAssessment(BaseModel):
    used: bool = False
    available: bool = False
    degraded: bool = False
    confirmed: bool = False
    contradicted: bool = False
    news_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    macro_relevance: Literal["low", "medium", "high"] = "low"
    event_risk: Literal["low", "medium", "high"] = "low"
    confidence_adjustment: float = 0.0
    contradiction_flag: bool = False
    summary_reason: str = ""
    source_quality_score: float = 0.0
    affected_symbols: list[str] = Field(default_factory=list)
    advisory_action: str = "HOLD"
    advisory_confidence: float = 0.0
    confidence_delta: float = 0.0
    reason: str = ""
    error: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class TradeQualityAssessment(BaseModel):
    trend_alignment_score: float = 0.0
    momentum_quality_score: float = 0.0
    entry_timing_score: float = 0.0
    volatility_quality_score: float = 0.0
    reward_risk_score: float = 0.0
    spread_quality_score: float = 0.0
    portfolio_fit_score: float = 0.0
    news_alignment_score: float = 0.0
    contradiction_penalty: float = 0.0
    final_trade_quality_score: float = 0.0
    threshold: float = 0.0
    no_trade_zone: bool = False
    no_trade_reasons: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def approved(self) -> bool:
        return not self.no_trade_zone and self.final_trade_quality_score >= self.threshold


class AntiChurnAssessment(BaseModel):
    blocked: bool = False
    threshold_boost: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioRiskAssessment(BaseModel):
    status: ExecutionStatus = "pass"
    allow_execute: bool = True
    reason: str = ""
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    correlated_symbols: list[str] = Field(default_factory=list)
    margin_required: float = 0.0
    projected_margin_utilization_pct: float = 0.0
    projected_free_margin_pct: float = 0.0
    portfolio_fit_score: float = 0.0


class SignalDecision(BaseModel):
    requested_agent_name: str
    primary_agent_name: str
    final_agent_name: str
    market_context: MarketContext
    primary_signal: TechnicalSignal
    final_signal: TechnicalSignal
    gemini_confirmation: Optional[GeminiAssessment] = None
    degraded_reasons: list[str] = Field(default_factory=list)


class TradeDecisionAssessment(BaseModel):
    trade: bool = False
    final_direction: str = "HOLD"
    final_signal: TechnicalSignal
    trade_quality_assessment: TradeQualityAssessment
    reasons: list[str] = Field(default_factory=list)

    @property
    def final_trade_quality_score(self) -> float:
        return self.trade_quality_assessment.final_trade_quality_score


class RiskEvaluation(BaseModel):
    approved: bool
    reason: str
    adjusted_volume: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    mode: str = "manual"
    status: ExecutionStatus = "pass"
    machine_reasons: list[str] = Field(default_factory=list)
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_risk_decision(cls, decision: Any, mode: str) -> "RiskEvaluation":
        status: ExecutionStatus = "pass"
        if not getattr(decision, "approved", False):
            status = "block"
        elif getattr(decision, "warnings", []):
            status = "warn"
        return cls(
            approved=decision.approved,
            reason=decision.reason,
            adjusted_volume=decision.adjusted_volume,
            warnings=list(decision.warnings),
            mode=mode,
            status=status,
            machine_reasons=list(getattr(decision, "warnings", [])),
        )


class PositionManagementPlan(BaseModel):
    manage_position: bool = True
    strategy: Optional[str] = None
    max_holding_minutes: Optional[int] = None
    planned_hold_minutes: Optional[int] = None
    initial_thesis: str = ""
    expected_hold_minutes: Optional[int] = None
    atr_regime: str = "normal"
    close_before_session_end: bool = False
    session_close_buffer_minutes: int = 0
    stale_after_minutes: Optional[int] = None
    min_progress_r_multiple: float = 0.0
    target_progress_r_multiple: float = 0.0
    break_even_activation_rule: str = ""
    trailing_rule: str = ""
    invalidation_condition: str = ""
    time_stop_rule: str = ""
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskApprovalDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    risk_evaluation: RiskEvaluation
    portfolio_risk_assessment: PortfolioRiskAssessment
    anti_churn_assessment: AntiChurnAssessment


class ExecutionPreflightAssessment(BaseModel):
    approved: bool
    reason: str
    entry_price: float = 0.0
    current_spread: float = 0.0
    reward_risk: float = 0.0
    margin_required: float = 0.0
    tradeability: dict[str, Any] = Field(default_factory=dict)
    risk_approval: Optional[RiskApprovalDecision] = None


class ExecutionDecision(BaseModel):
    allow_execute: bool
    reason: str
    signal_decision: SignalDecision
    trade_decision_assessment: Optional[TradeDecisionAssessment] = None
    risk_evaluation: RiskEvaluation
    trade_quality_assessment: TradeQualityAssessment
    portfolio_risk_assessment: PortfolioRiskAssessment
    anti_churn_assessment: AntiChurnAssessment
    position_management_plan: PositionManagementPlan
    entry_price: float = 0.0


# Backward-compatible aliases used by the earlier phase-1 code/tests.
AgentSignal = TechnicalSignal
GeminiConfirmation = GeminiAssessment
