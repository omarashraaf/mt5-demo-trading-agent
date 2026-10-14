export type PositionValueInput = {
  volume?: number | string | null;
  price_open?: number | string | null;
  comment?: string | null;
  margin_used_usd?: number | string | null;
  estimated_margin?: number | string | null;
  started_with_usd?: number | string | null;
  entry_market_value_usd?: number | string | null;
};

export function positiveAmount(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function commentStartedAmount(comment: string | null | undefined): number {
  const amountMatch = String(comment || '').match(/TA:\$(\d+(?:\.\d+)?)|TA(\d+(?:\.\d+)?)/i);
  const raw = amountMatch ? (amountMatch[1] || amountMatch[2]) : undefined;
  const amount = raw ? parseFloat(raw) : 0;
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

export function positionStartedAmount(
  position: PositionValueInput,
  leverage: number = 100,
  contractSize?: number | null,
): number {
  const leverageNormalized = Number.isFinite(leverage) && leverage > 0 ? leverage : 1;
  const contract = positiveAmount(contractSize);
  const volume = Number(position.volume || 0);
  const openPrice = Number(position.price_open || 0);
  const notional = positiveAmount(position.entry_market_value_usd);
  return (
    positiveAmount(position.margin_used_usd)
    ?? positiveAmount(position.estimated_margin)
    ?? positiveAmount(position.started_with_usd)
    ?? positiveAmount(commentStartedAmount(position.comment))
    ?? (notional != null ? positiveAmount(notional / leverageNormalized) : null)
    ?? (contract != null
      ? positiveAmount((volume * openPrice * contract) / leverageNormalized)
      : null)
    ?? positiveAmount((volume * openPrice) / leverageNormalized)
    ?? 0
  );
}

export function positionMarginUsedAmount(
  position: PositionValueInput,
  leverage: number = 100,
  contractSize?: number | null,
): number {
  const leverageNormalized = Number.isFinite(leverage) && leverage > 0 ? leverage : 1;
  const contract = positiveAmount(contractSize);
  const volume = Number(position.volume || 0);
  const openPrice = Number(position.price_open || 0);
  const notional = positiveAmount(position.entry_market_value_usd);
  return (
    positiveAmount(position.margin_used_usd)
    ?? positiveAmount(position.estimated_margin)
    ?? (notional != null ? positiveAmount(notional / leverageNormalized) : null)
    ?? (contract != null
      ? positiveAmount((volume * openPrice * contract) / leverageNormalized)
      : null)
    ?? positiveAmount((volume * openPrice) / leverageNormalized)
    ?? 0
  );
}
