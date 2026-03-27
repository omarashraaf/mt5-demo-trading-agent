from __future__ import annotations

import time
import math
from collections import defaultdict
from typing import Any

from domain.models import MarketContext, PortfolioRiskAssessment, TechnicalSignal
from services.symbol_profile_service import SymbolProfileService


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


class PortfolioRiskService:
    def __init__(self, market_data, profile_service: SymbolProfileService | None = None):
        self.market_data = market_data
        self.profile_service = profile_service or SymbolProfileService()

    def assess(
        self,
        context: MarketContext,
        signal: TechnicalSignal,
        proposed_volume: float,
        entry_price: float,
        settings: Any,
        recent_outcomes: list[dict] | None = None,
        is_auto_trade: bool = False,
    ) -> PortfolioRiskAssessment:
        equity = max(context.account_equity, 0.0)
        current_margin = max(context.account_margin, 0.0)
        free_margin = max(context.account_free_margin, 0.0)
        positions = context.all_open_positions
        symbol_info = context.symbol_info
        profile = context.profile

        margin_required = self._estimate_margin(
            symbol_info=symbol_info,
            volume=proposed_volume,
            price=entry_price,
            leverage=max(context.account_leverage, 1),
        )
        projected_margin = current_margin + margin_required
        projected_margin_util = (projected_margin / equity * 100) if equity > 0 else 0.0
        projected_free_margin_pct = ((free_margin - margin_required) / equity * 100) if equity > 0 else 0.0

        exposures = self._exposure_breakdown(context, positions)
        sector_exposure = exposures["by_sector"].get(symbol_info.sector if symbol_info else "Other", 0.0)
        category_exposure = exposures["by_category"].get(profile.category if profile else "Other", 0.0)
        same_symbol_positions = [
            pos for pos in positions
            if str(pos.get("symbol", "")).upper() == context.symbol.upper()
        ]
        correlated_symbols = self._correlated_positions(context, positions)

        blocking_reasons: list[str] = []
        warnings: list[str] = []

        if projected_margin_util > settings.max_margin_utilization_pct:
            blocking_reasons.append(
                f"Projected margin utilization {projected_margin_util:.1f}% exceeds cap {settings.max_margin_utilization_pct:.1f}%."
            )
        if projected_free_margin_pct < settings.min_free_margin_pct:
            blocking_reasons.append(
                f"Projected free margin {projected_free_margin_pct:.1f}% falls below floor {settings.min_free_margin_pct:.1f}%."
            )
        if len(positions) >= settings.max_open_positions_total:
            blocking_reasons.append(
                f"Open position cap reached ({len(positions)}/{settings.max_open_positions_total})."
            )
        if len(same_symbol_positions) >= settings.max_positions_per_symbol:
            blocking_reasons.append(
                f"{context.symbol} already has {len(same_symbol_positions)} open position(s)."
            )
        if recent_outcomes and self._recent_symbol_trade_block(context.symbol, recent_outcomes, settings.cooldown_minutes_per_symbol):
            blocking_reasons.append(
                f"{context.symbol} is still inside the per-symbol cooldown window."
            )
        if profile and exposures["counts_by_category"].get(profile.category, 0) >= profile.max_positions_per_category:
            blocking_reasons.append(
                f"{profile.profile_name} category already has the configured maximum open positions."
            )
        if sector_exposure + self._exposure_pct(margin_required, equity) > settings.max_sector_exposure_pct:
            blocking_reasons.append(
                f"Sector exposure would exceed {settings.max_sector_exposure_pct:.1f}% of equity."
            )
        usd_beta_projection = exposures["usd_beta_exposure_pct"] + self._projected_usd_beta_pct(context, margin_required, equity)
        if abs(usd_beta_projection) > settings.max_usd_beta_exposure_pct:
            blocking_reasons.append(
                f"USD beta exposure would reach {usd_beta_projection:.1f}%."
            )
        if profile and profile.category == "Stocks":
            stocks_projection = exposures["stocks_equity_exposure_pct"] + self._exposure_pct(margin_required, equity)
            if stocks_projection > settings.max_equity_exposure_pct_for_stocks:
                blocking_reasons.append(
                    f"Stock exposure would exceed {settings.max_equity_exposure_pct_for_stocks:.1f}% of equity."
                )
        if len(correlated_symbols) >= settings.max_correlated_positions:
            blocking_reasons.append(
                f"Correlated exposure already exists across {', '.join(correlated_symbols[:4])}."
            )

        status = "block" if blocking_reasons else "pass"
        allow_execute = not blocking_reasons
        if not allow_execute and not is_auto_trade:
            warnings.extend(blocking_reasons)
            status = "warn"
            allow_execute = False

        portfolio_fit_score = self._portfolio_fit_score(
            projected_margin_util,
            projected_free_margin_pct,
            len(correlated_symbols),
            settings.max_correlated_positions,
        )
        reason = blocking_reasons[0] if blocking_reasons else "Portfolio exposure is within limits."

        return PortfolioRiskAssessment(
            status=status,
            allow_execute=allow_execute,
            reason=reason,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
            correlated_symbols=correlated_symbols,
            margin_required=round(margin_required, 2),
            projected_margin_utilization_pct=round(projected_margin_util, 2),
            projected_free_margin_pct=round(projected_free_margin_pct, 2),
            portfolio_fit_score=round(portfolio_fit_score, 3),
            metrics_snapshot={
                "margin_utilization_pct": round((current_margin / equity * 100) if equity > 0 else 0.0, 2),
                "projected_margin_utilization_pct": round(projected_margin_util, 2),
                "free_margin_pct": round((free_margin / equity * 100) if equity > 0 else 0.0, 2),
                "projected_free_margin_pct": round(projected_free_margin_pct, 2),
                "open_positions_total": len(positions),
                "open_positions_same_symbol": len(same_symbol_positions),
                "sector_exposure_pct": round(sector_exposure, 2),
                "category_exposure_pct": round(category_exposure, 2),
                "usd_beta_exposure_pct": round(usd_beta_projection, 2),
                "stocks_equity_exposure_pct": round(exposures["stocks_equity_exposure_pct"], 2),
                "exposure_by_symbol": exposures["by_symbol"],
                "exposure_by_category": exposures["by_category"],
                "exposure_by_sector": exposures["by_sector"],
            },
        )

    def fit_volume_to_margin_limits(
        self,
        *,
        context: MarketContext,
        proposed_volume: float,
        entry_price: float,
        settings: Any,
    ) -> tuple[float, str | None]:
        """Scale volume down to fit portfolio margin caps when possible.

        Returns (fitted_volume, reason). If fitted_volume is 0, the trade should be blocked.
        """
        if proposed_volume <= 0:
            return 0.0, "Computed volume is zero."
        if not context.symbol_info or entry_price <= 0:
            return proposed_volume, None

        equity = max(context.account_equity, 0.0)
        current_margin = max(context.account_margin, 0.0)
        free_margin = max(context.account_free_margin, 0.0)
        if equity <= 0:
            return 0.0, "Account equity is zero."

        margin_cap_amount = equity * float(settings.max_margin_utilization_pct) / 100.0
        min_free_margin_amount = equity * float(settings.min_free_margin_pct) / 100.0
        allowed_by_margin_cap = margin_cap_amount - current_margin
        allowed_by_free_margin_floor = free_margin - min_free_margin_amount
        margin_budget = min(allowed_by_margin_cap, allowed_by_free_margin_floor)
        if margin_budget <= 0:
            return 0.0, (
                "No margin budget available under policy limits "
                f"(cap {settings.max_margin_utilization_pct:.1f}%, floor {settings.min_free_margin_pct:.1f}%)."
            )

        leverage = max(context.account_leverage, 1)
        margin_per_lot = self._estimate_margin(
            symbol_info=context.symbol_info,
            volume=1.0,
            price=entry_price,
            leverage=leverage,
        )
        if margin_per_lot <= 0:
            return proposed_volume, None

        max_volume_by_budget = margin_budget / margin_per_lot
        info = context.symbol_info
        step = max(float(info.volume_step or 0.01), 0.00000001)
        min_volume = max(float(info.volume_min or step), step)
        max_volume = float(info.volume_max or proposed_volume)

        fitted = min(proposed_volume, max_volume_by_budget, max_volume)
        fitted = self._floor_to_step(fitted, step)
        if fitted < min_volume:
            return 0.0, (
                f"Minimum lot {min_volume:g} exceeds safe margin budget "
                f"under cap {settings.max_margin_utilization_pct:.1f}%."
            )

        if fitted + 1e-12 < proposed_volume:
            return fitted, (
                f"Volume scaled down from {proposed_volume:g} to {fitted:g} "
                "to respect margin/free-margin policy limits."
            )
        return fitted, None

    def snapshot(self, account, positions: list[Any]) -> dict[str, Any]:
        equity = getattr(account, "equity", 0.0) if account else 0.0
        margin = getattr(account, "margin", 0.0) if account else 0.0
        free_margin = getattr(account, "free_margin", 0.0) if account else 0.0
        context = MarketContext(
            symbol="",
            all_open_positions=[
                pos.model_dump() if hasattr(pos, "model_dump") else pos
                for pos in positions
            ],
            account_equity=equity,
            account_margin=margin,
            account_free_margin=free_margin,
        )
        exposures = self._exposure_breakdown(context, context.all_open_positions)
        return {
            "margin_utilization_pct": round((margin / equity * 100) if equity > 0 else 0.0, 2),
            "free_margin_pct": round((free_margin / equity * 100) if equity > 0 else 0.0, 2),
            "open_positions_total": len(context.all_open_positions),
            "exposure_by_symbol": exposures["by_symbol"],
            "exposure_by_category": exposures["by_category"],
            "exposure_by_sector": exposures["by_sector"],
            "usd_beta_exposure_pct": round(exposures["usd_beta_exposure_pct"], 2),
            "stocks_equity_exposure_pct": round(exposures["stocks_equity_exposure_pct"], 2),
        }

    def _estimate_margin(self, symbol_info, volume: float, price: float, leverage: int) -> float:
        if not symbol_info or volume <= 0 or price <= 0:
            return 0.0
        contract_size = max(symbol_info.trade_contract_size, 1.0)
        return volume * price * contract_size / max(leverage, 1)

    def _floor_to_step(self, value: float, step: float) -> float:
        if step <= 0:
            return value
        units = math.floor((value / step) + 1e-12)
        return round(units * step, 8)

    def _exposure_breakdown(self, context: MarketContext, positions: list[dict]) -> dict[str, Any]:
        by_symbol: dict[str, float] = defaultdict(float)
        by_category: dict[str, float] = defaultdict(float)
        by_sector: dict[str, float] = defaultdict(float)
        counts_by_category: dict[str, int] = defaultdict(int)
        equity = max(context.account_equity, 0.0)
        usd_beta_exposure_pct = 0.0
        stocks_equity_exposure_pct = 0.0

        for position in positions:
            symbol = str(position.get("symbol", "")).upper()
            if not symbol:
                continue
            info = self.market_data.get_symbol_info(symbol)
            if info is None:
                continue
            normalized = self.profile_service.enrich_symbol_info(
                symbol,
                self._normalized_info(symbol, info, position),
            )
            profile = self.profile_service.resolve_profile(symbol, normalized)
            price = float(position.get("price_current") or position.get("price_open") or 0.0)
            volume = float(position.get("volume") or 0.0)
            leverage = max(context.account_leverage, 1)
            margin_equivalent = volume * price * max(normalized.trade_contract_size, 1.0) / leverage
            exposure_pct = self._exposure_pct(margin_equivalent, equity)
            by_symbol[symbol] += round(exposure_pct, 2)
            by_category[profile.category] += round(exposure_pct, 2)
            by_sector[profile.sector] += round(exposure_pct, 2)
            counts_by_category[profile.category] += 1
            usd_beta_exposure_pct += exposure_pct * normalized.usd_beta_weight
            if profile.category == "Stocks":
                stocks_equity_exposure_pct += exposure_pct

        return {
            "by_symbol": dict(by_symbol),
            "by_category": dict(by_category),
            "by_sector": dict(by_sector),
            "counts_by_category": dict(counts_by_category),
            "usd_beta_exposure_pct": usd_beta_exposure_pct,
            "stocks_equity_exposure_pct": stocks_equity_exposure_pct,
        }

    def _normalized_info(self, symbol: str, raw_info: dict, position: dict):
        from domain.models import NormalizedSymbolInfo

        return NormalizedSymbolInfo(
            name=raw_info.get("name", symbol),
            description=raw_info.get("description", ""),
            category=raw_info.get("category", "Other"),
            path=raw_info.get("path", ""),
            point=raw_info.get("point", 0.0),
            digits=raw_info.get("digits", 5),
            trade_contract_size=raw_info.get("trade_contract_size", 0.0),
            volume_min=raw_info.get("volume_min", 0.01),
            volume_max=raw_info.get("volume_max", 0.0),
            volume_step=raw_info.get("volume_step", 0.01),
            trade_stops_level=raw_info.get("trade_stops_level", 0.0),
            visible=raw_info.get("visible", False),
            trade_enabled=raw_info.get("trade_mode", 0) != 0,
            spread=float(position.get("spread", raw_info.get("spread", 0.0) or 0.0)),
        )

    def _correlated_positions(self, context: MarketContext, positions: list[dict]) -> list[str]:
        target_tags = set(context.symbol_info.correlation_tags if context.symbol_info else [])
        correlated: list[str] = []
        for position in positions:
            symbol = str(position.get("symbol", "")).upper()
            if not symbol or symbol == context.symbol.upper():
                continue
            info = self.market_data.get_symbol_info(symbol)
            if info is None:
                continue
            normalized = self.profile_service.enrich_symbol_info(
                symbol,
                self._normalized_info(symbol, info, position),
            )
            if target_tags.intersection(normalized.correlation_tags):
                correlated.append(symbol)
        return correlated

    def _projected_usd_beta_pct(self, context: MarketContext, margin_required: float, equity: float) -> float:
        if equity <= 0 or not context.symbol_info:
            return 0.0
        exposure_pct = self._exposure_pct(margin_required, equity)
        return exposure_pct * context.symbol_info.usd_beta_weight

    def _portfolio_fit_score(
        self,
        projected_margin_util: float,
        projected_free_margin_pct: float,
        correlated_count: int,
        max_correlated: int,
    ) -> float:
        margin_score = _clamp(1.0 - projected_margin_util / 100.0)
        free_margin_score = _clamp(projected_free_margin_pct / 100.0)
        correlated_score = _clamp(1.0 - correlated_count / max(max_correlated, 1))
        return margin_score * 0.4 + free_margin_score * 0.4 + correlated_score * 0.2

    def _exposure_pct(self, value: float, equity: float) -> float:
        if equity <= 0:
            return 0.0
        return value / equity * 100

    def _recent_symbol_trade_block(
        self,
        symbol: str,
        recent_outcomes: list[dict],
        cooldown_minutes: int,
    ) -> bool:
        for outcome in recent_outcomes:
            if str(outcome.get("symbol", "")).upper() != symbol.upper():
                continue
            closed_at = outcome.get("closed_at") or outcome.get("timestamp")
            if closed_at is None:
                continue
            minutes_since = max(0.0, (time.time() - float(closed_at)) / 60.0)
            if minutes_since < cooldown_minutes:
                return True
        return False
