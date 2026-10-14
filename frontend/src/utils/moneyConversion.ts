export type SymbolPricingInfo = {
  trade_tick_value?: number | string | null;
  trade_tick_size?: number | string | null;
  point?: number | string | null;
  trade_contract_size?: number | string | null;
};

function safeNumber(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export function usdPerPriceDistance({
  symbolInfo,
  volume,
}: {
  symbolInfo?: SymbolPricingInfo | null;
  volume: number;
}): number {
  const vol = safeNumber(volume);
  if (vol <= 0) return 0;
  const info = symbolInfo || {};
  const tickValue = safeNumber(info.trade_tick_value);
  const tickSize = safeNumber(info.trade_tick_size);
  const point = safeNumber(info.point);
  const contractSize = safeNumber(info.trade_contract_size);
  const contractBased = contractSize > 0 ? vol * contractSize : 0;

  if (tickValue > 0) {
    const unit = tickSize > 0 ? tickSize : point;
    if (unit > 0) {
      const tickBased = vol * (tickValue / unit);
      if (contractBased > 0 && tickBased > 0) {
        if (tickBased < contractBased * 0.5) return contractBased;
        if (contractBased < tickBased * 0.5) return tickBased;
        return Math.max(tickBased, contractBased);
      }
      return tickBased;
    }
  }

  return contractBased > 0 ? contractBased : 0;
}

export function priceDistanceToUsd({
  symbolInfo,
  entryPrice,
  targetPrice,
  volume,
}: {
  symbolInfo?: SymbolPricingInfo | null;
  entryPrice: number;
  targetPrice: number;
  volume: number;
}): number | null {
  const entry = safeNumber(entryPrice);
  const target = safeNumber(targetPrice);
  const vol = safeNumber(volume);
  if (entry <= 0 || target <= 0 || vol <= 0) return null;
  const usdPerUnit = usdPerPriceDistance({ symbolInfo, volume: vol });
  if (usdPerUnit <= 0) return null;
  return Math.abs(entry - target) * usdPerUnit;
}
