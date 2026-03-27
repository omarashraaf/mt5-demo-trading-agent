from __future__ import annotations

import math

LABEL_SCHEMA_VERSION = "v2"


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


def hold_bucket_from_minutes(holding_minutes: float) -> str:
    value = _to_float(holding_minutes, 0.0)
    if value < 15:
        return "0_15m"
    if value < 30:
        return "15_30m"
    if value < 60:
        return "30_60m"
    if value < 180:
        return "60_180m"
    return "180m_plus"


def estimate_costs_from_candidate(candidate: dict, outcome: dict) -> float:
    # Step-2 conservative proxy: spread + slippage estimate from candidate row.
    spread = _to_float(candidate.get("spread_at_eval", 0.0), 0.0)
    slippage = _to_float(candidate.get("execution_slippage_est", 0.0), 0.0)
    # Convert spread points into rough account-currency cost proxy.
    spread_cost = max(0.0, spread) * 0.01
    return max(0.0, spread_cost + max(0.0, slippage))


def _expected_return_for_horizon(net_pnl: float, holding_minutes: float, horizon_minutes: int) -> float:
    if holding_minutes <= 0:
        return net_pnl
    if holding_minutes <= horizon_minutes:
        return net_pnl
    # Decay proxy to avoid overstating returns for shorter horizons when trade
    # lasted significantly longer than the requested label horizon.
    return net_pnl * (horizon_minutes / holding_minutes)


def build_labels(
    *,
    candidate: dict,
    outcome: dict | None,
) -> dict:
    executed = bool(candidate.get("executed", False))
    if not executed:
        return {
            "label_schema_version": LABEL_SCHEMA_VERSION,
            "no_trade_label": 1,
            "profitable_after_costs_30m": 0,
            "profitable_after_costs_90m": 0,
            "profitable_after_costs_180m": 0,
            "hit_target_before_stop": 0,
            "expected_return_30m": 0.0,
            "expected_return_90m": 0.0,
            "expected_return_180m": 0.0,
            "mfe_label": _to_float(candidate.get("mfe", 0.0), 0.0),
            "mae_label": _to_float(candidate.get("mae", 0.0), 0.0),
            "hold_bucket": "0_15m",
        }

    effective_outcome = outcome or {}
    raw_profit = _to_float(
        candidate.get("net_pnl", effective_outcome.get("profit", 0.0)),
        0.0,
    )
    total_cost = estimate_costs_from_candidate(candidate, effective_outcome)
    net_pnl = raw_profit - total_cost
    holding_minutes = _to_float(
        candidate.get("hold_duration_minutes", effective_outcome.get("holding_minutes", 0.0)),
        0.0,
    )

    exp_30 = _expected_return_for_horizon(net_pnl, holding_minutes, 30)
    exp_90 = _expected_return_for_horizon(net_pnl, holding_minutes, 90)
    exp_180 = _expected_return_for_horizon(net_pnl, holding_minutes, 180)

    hit_target = str(candidate.get("exit_reason", effective_outcome.get("exit_reason", ""))).lower() == "take_profit"

    return {
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "no_trade_label": 0,
        "profitable_after_costs_30m": int(exp_30 > 0.0),
        "profitable_after_costs_90m": int(exp_90 > 0.0),
        "profitable_after_costs_180m": int(exp_180 > 0.0),
        "hit_target_before_stop": int(hit_target),
        "expected_return_30m": exp_30,
        "expected_return_90m": exp_90,
        "expected_return_180m": exp_180,
        "mfe_label": _to_float(candidate.get("mfe", 0.0), 0.0),
        "mae_label": _to_float(candidate.get("mae", 0.0), 0.0),
        "hold_bucket": hold_bucket_from_minutes(holding_minutes),
    }
