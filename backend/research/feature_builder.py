from __future__ import annotations

import math
import time

from domain.models import (
    AntiChurnAssessment,
    ExecutionDecision,
    GeminiAssessment,
    MarketContext,
    PortfolioRiskAssessment,
    TechnicalSignal,
    TradeQualityAssessment,
)

FEATURE_SCHEMA_VERSION = "v2"


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return float(default)
        return parsed
    except (TypeError, ValueError):
        return float(default)


def _safe_text(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _categorize_volatility(atr_pct: float) -> str:
    if atr_pct <= 0:
        return "unknown"
    if atr_pct < 0.3:
        return "low"
    if atr_pct < 1.0:
        return "medium"
    return "high"


def _spread_percentile_proxy(spread: float) -> float:
    # Percentile proxy without requiring a full rolling-window history in Step 2.
    # 0.0 means very tight spread, 1.0 means very wide spread.
    if spread <= 0:
        return 0.0
    return min(1.0, spread / 100.0)


def _volatility_percentile_proxy(atr_pct: float) -> float:
    # ATR% scaled proxy to support early model iterations before a robust
    # historical percentile calibration is introduced.
    if atr_pct <= 0:
        return 0.0
    return min(1.0, atr_pct / 3.0)


def build_feature_snapshot_from_inputs(
    *,
    context: MarketContext,
    signal: TechnicalSignal,
    quality: TradeQualityAssessment,
    gemini: GeminiAssessment | None,
    portfolio_risk: PortfolioRiskAssessment,
    anti_churn: AntiChurnAssessment,
) -> dict:
    meta = signal.metadata or {}

    trend_h1 = _safe_text(meta.get("h1_trend", ""))
    trend_h4 = _safe_text(meta.get("h4_trend", ""))
    action = _safe_text(signal.action, "HOLD")
    atr_pct = _to_float(meta.get("atr_pct", 0.0), 0.0)
    spread = _to_float(context.tick.get("spread", 0.0), 0.0) if context.tick else 0.0
    reward_risk = _to_float(meta.get("reward_risk_ratio", 0.0), 0.0)

    mtf_agreement = float(trend_h1 == trend_h4 and trend_h1 in {"bullish", "bearish"})
    trend_alignment = _to_float(quality.trend_alignment_score, 0.0)
    momentum_quality = _to_float(quality.momentum_quality_score, 0.0)
    entry_timing = _to_float(quality.entry_timing_score, 0.0)
    extension_score = 1.0 - max(0.0, min(1.0, entry_timing))

    return {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "symbol": context.symbol,
        "asset_class": context.symbol_info.category if context.symbol_info else "Other",
        "session": ",".join((context.user_policy or {}).get("session_filters", []) or []),
        "day_of_week": int(time.gmtime().tm_wday),
        "strategy_mode": _safe_text((context.user_policy or {}).get("mode", "balanced")),
        "technical_direction": action,
        "trend_h1": trend_h1,
        "trend_h4": trend_h4,
        "multi_timeframe_agreement": mtf_agreement,
        "trend_alignment_score": trend_alignment,
        "momentum_quality_score": momentum_quality,
        "entry_timing_score": entry_timing,
        "entry_extension_score": extension_score,
        "confidence": _to_float(signal.confidence, 0.0),
        "quality_score": _to_float(quality.final_trade_quality_score, 0.0),
        "quality_threshold": _to_float(quality.threshold, 0.0),
        "rsi_proxy": _to_float(meta.get("rsi", meta.get("rsi_value", 50.0)), 50.0),
        "macd_proxy": _to_float(meta.get("macd_hist", meta.get("macd", 0.0)), 0.0),
        "ema_slope_proxy": _to_float(meta.get("ema_slope", meta.get("ma_slope", 0.0)), 0.0),
        "atr_pct": atr_pct,
        "atr_regime": _safe_text(meta.get("atr_regime", _categorize_volatility(atr_pct)), "unknown"),
        "volatility_percentile": _volatility_percentile_proxy(atr_pct),
        "spread_at_eval": spread,
        "spread_percentile": _spread_percentile_proxy(spread),
        "reward_risk_ratio": reward_risk,
        "distance_to_support": _to_float(meta.get("distance_to_support", 0.0), 0.0),
        "distance_to_resistance": _to_float(meta.get("distance_to_resistance", 0.0), 0.0),
        "support_resistance_context": _safe_text(meta.get("sr_context", ""), ""),
        "stop_loss": _to_float(signal.stop_loss, 0.0),
        "take_profit": _to_float(signal.take_profit, 0.0),
        "trend_following": float(bool(meta.get("trend_following", action in {"BUY", "SELL"} and trend_h1 == trend_h4))),
        "counter_trend": float(bool(meta.get("counter_trend", action in {"BUY", "SELL"} and trend_h1 != trend_h4))),
        "gemini_used": float(bool(gemini.used)) if gemini else 0.0,
        "gemini_news_bias": _safe_text(gemini.news_bias if gemini else "neutral", "neutral"),
        "gemini_event_risk": _safe_text(gemini.event_risk if gemini else "low", "low"),
        "gemini_event_type": _safe_text(gemini.raw_payload.get("event_type", "unknown") if gemini else "unknown", "unknown"),
        "gemini_event_importance": _safe_text(gemini.macro_relevance if gemini else "low", "low"),
        "gemini_contradiction_flag": float(bool(gemini.contradiction_flag)) if gemini else 0.0,
        "gemini_confidence_adjustment": _to_float(gemini.confidence_adjustment, 0.0) if gemini else 0.0,
        "portfolio_fit_score": _to_float(portfolio_risk.portfolio_fit_score, 0.0),
        "portfolio_margin_utilization_pct": _to_float(
            portfolio_risk.projected_margin_utilization_pct, 0.0
        ),
        "portfolio_free_margin_pct": _to_float(
            portfolio_risk.projected_free_margin_pct, 0.0
        ),
        "duplicate_exposure_count": _to_float(
            len(portfolio_risk.correlated_symbols or []), 0.0
        ),
        "anti_churn_blocked": float(bool(anti_churn.blocked)),
    }


def build_feature_snapshot(decision: ExecutionDecision) -> dict:
    return build_feature_snapshot_from_inputs(
        context=decision.signal_decision.market_context,
        signal=decision.signal_decision.final_signal,
        quality=decision.trade_quality_assessment,
        gemini=decision.signal_decision.gemini_confirmation,
        portfolio_risk=decision.portfolio_risk_assessment,
        anti_churn=decision.anti_churn_assessment,
    )
