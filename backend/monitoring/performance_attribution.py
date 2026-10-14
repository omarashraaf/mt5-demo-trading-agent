from __future__ import annotations


def summarize_basic_attribution(outcomes: list[dict]) -> dict:
    if not outcomes:
        return {
            "count": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "avg_pnl": 0.0,
            "by_symbol": {},
        }
    net = sum(_outcome_profit(o) for o in outcomes)
    wins = [o for o in outcomes if _outcome_profit(o) > 0]
    by_symbol: dict[str, dict] = {}
    for o in outcomes:
        symbol = str(o.get("symbol", "")).upper()
        if symbol not in by_symbol:
            by_symbol[symbol] = {"count": 0, "net_pnl": 0.0}
        by_symbol[symbol]["count"] += 1
        by_symbol[symbol]["net_pnl"] += _outcome_profit(o)
    return {
        "count": len(outcomes),
        "win_rate": len(wins) / max(len(outcomes), 1),
        "net_pnl": net,
        "avg_pnl": net / len(outcomes),
        "by_symbol": by_symbol,
    }


def _outcome_profit(outcome: dict) -> float:
    for key in ("profit", "profit_usd", "pnl", "net_pnl"):
        if outcome.get(key) is not None:
            return float(outcome.get(key) or 0.0)
    return 0.0
