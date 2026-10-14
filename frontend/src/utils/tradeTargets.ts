export type TradeAction = 'BUY' | 'SELL' | string | null | undefined;

export type SymbolPricingInfo = {
  trade_tick_value?: number | string | null;
  trade_tick_size?: number | string | null;
  point?: number | string | null;
  trade_contract_size?: number | string | null;
};

function safeNumber(value: unknown): number | null {
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) ? num : null;
}

function usdPerPriceDistance({
  symbolInfo,
  volume,
  contractSize,
}: {
  symbolInfo?: SymbolPricingInfo | null;
  volume: number;
  contractSize: number;
}): number {
  const vol = safeNumber(volume);
  const contract = safeNumber(symbolInfo?.trade_contract_size) ?? safeNumber(contractSize);
  if (vol == null || vol <= 0) return 0;
  const tickValue = safeNumber(symbolInfo?.trade_tick_value) ?? 0;
  const tickSize = safeNumber(symbolInfo?.trade_tick_size) ?? 0;
  const point = safeNumber(symbolInfo?.point) ?? 0;
  const contractBased = contract != null && contract > 0 ? vol * contract : 0;

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

  return contractBased;
}

export function dollarTargetToPrice({
  action,
  dollarAmount,
  volume,
  contractSize,
  symbolInfo,
  referencePrice,
  isStopLoss,
}: {
  action: TradeAction;
  dollarAmount: number;
  volume: number;
  contractSize: number;
  symbolInfo?: SymbolPricingInfo | null;
  referencePrice: number;
  isStopLoss: boolean;
}): number | null {
  const amount = safeNumber(dollarAmount);
  const vol = safeNumber(volume);
  const contract = safeNumber(contractSize);
  const price = safeNumber(referencePrice);
  if (amount == null || vol == null || contract == null || price == null) return null;
  if (amount <= 0 || vol <= 0 || contract <= 0 || price <= 0) return null;

  const priceDistanceValue = usdPerPriceDistance({ symbolInfo, volume: vol, contractSize: contract });
  if (priceDistanceValue <= 0) return null;
  const distance = amount / priceDistanceValue;
  const normalizedAction = String(action || '').trim().toUpperCase();
  if (normalizedAction === 'SELL') {
    return isStopLoss ? price + distance : price - distance;
  }
  return isStopLoss ? price - distance : price + distance;
}
