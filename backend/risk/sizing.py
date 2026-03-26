import MetaTrader5 as mt5
import logging

logger = logging.getLogger(__name__)


def calculate_position_size(
    equity: float,
    risk_percent: float,
    stop_loss_distance: float,
    symbol: str,
    use_fixed: bool = False,
    fixed_lot: float = 0.01,
    open_position_count: int = 0,
    max_concurrent: int = 15,
    action: str = "BUY",
) -> float:
    if use_fixed:
        return fixed_lot

    if stop_loss_distance <= 0:
        logger.warning("SL distance is zero; falling back to minimum lot")
        return _get_min_lot(symbol)

    risk_amount = equity * (risk_percent / 100.0)

    info = mt5.symbol_info(symbol)
    if info is None:
        logger.error(f"Cannot get symbol info for {symbol}")
        return 0.0

    contract_size = info.trade_contract_size
    point = info.point
    min_lot = info.volume_min
    max_lot = info.volume_max
    lot_step = info.volume_step

    if point <= 0 or contract_size <= 0:
        logger.error(f"Invalid symbol params for {symbol}: point={point}, contract={contract_size}")
        return min_lot

    # Calculate how much 1 lot moves per point
    tick_value = _get_tick_value(symbol, info)
    if tick_value <= 0:
        tick_value = point * contract_size  # rough fallback

    sl_points = stop_loss_distance / point
    risk_per_lot = sl_points * tick_value

    if risk_per_lot <= 0:
        return min_lot

    raw_lots = risk_amount / risk_per_lot

    # Scale down gently as positions accumulate (keep minimum 40% of base size)
    if open_position_count > 0:
        scale = max(0.40, 1.0 - (open_position_count * 0.05))
        raw_lots *= scale

    # Round to lot step
    lots = max(min_lot, min(max_lot, _round_to_step(raw_lots, lot_step)))

    # --- MARGIN CAP: ensure each trade uses at most equity/max_positions margin ---
    # This prevents stocks (1:1 margin) from eating all available margin
    max_margin_per_trade = equity / max(max_concurrent, 1)
    lots = _cap_by_margin(symbol, lots, max_margin_per_trade, action, lot_step, min_lot)

    return lots


def _cap_by_margin(symbol: str, lots: float, max_margin: float, action: str, lot_step: float, min_lot: float) -> float:
    """Scale down volume if required margin exceeds max_margin per trade."""
    try:
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return lots
        price = tick.ask if action == "BUY" else tick.bid
        if price <= 0:
            return lots

        margin_needed = mt5.order_calc_margin(order_type, symbol, lots, price)
        if margin_needed is None or margin_needed <= 0:
            return lots

        if margin_needed > max_margin:
            # Scale down proportionally
            scale = max_margin / margin_needed
            scaled_lots = _round_to_step(lots * scale, lot_step)
            scaled_lots = max(min_lot, scaled_lots)
            logger.info(
                f"Margin cap {symbol}: {lots} lots needs ${margin_needed:.0f} margin, "
                f"capped to {scaled_lots} lots (max ${max_margin:.0f}/trade)"
            )
            return scaled_lots
        return lots
    except Exception as e:
        logger.warning(f"Margin check failed for {symbol}: {e}, using uncapped volume")
        return lots


def _get_tick_value(symbol: str, info) -> float:
    """Get tick value in account currency."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return 0.0
    # For most Forex pairs the tick_value is available through order checking
    # Using a simple calculation as fallback
    return info.trade_tick_value if hasattr(info, "trade_tick_value") else 0.0


def _get_min_lot(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    return info.volume_min if info else 0.01


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 8)
