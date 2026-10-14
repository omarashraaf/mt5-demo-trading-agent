export type LiveTick = {
  symbol: string;
  bid: number;
  ask: number;
  spread: number;
  time: number;
  time_msc?: number;
  received_at_msc?: number;
  broker_time?: number;
  broker_time_msc?: number;
};

// Some broker feeds update less-liquid instruments only every few minutes.
// Retain the last valid quote long enough for those sessions while still
// dropping genuinely stale values.
const DEFAULT_MAX_TICK_AGE_MS = 5 * 60_000;
export const LIVE_TICK_FRESH_MS = 1_500;

export function tickTimestampMs(tick: LiveTick | null | undefined): number {
  const receivedAt = Number(tick?.received_at_msc || 0);
  if (Number.isFinite(receivedAt) && receivedAt > 1_000_000_000_000) return receivedAt;
  const rawMsc = Number(tick?.time_msc || 0);
  if (Number.isFinite(rawMsc) && rawMsc > 1_000_000_000_000) return rawMsc;
  const rawSec = Number(tick?.time || 0);
  return Number.isFinite(rawSec) && rawSec > 0 ? rawSec * 1000 : 0;
}

export function isFreshLiveTick(
  tick: LiveTick | null | undefined,
  nowMs: number = Date.now(),
  maxAgeMs: number = DEFAULT_MAX_TICK_AGE_MS,
): tick is LiveTick {
  const bid = Number(tick?.bid || 0);
  const ask = Number(tick?.ask || 0);
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || ask < bid) {
    return false;
  }
  const tickMs = tickTimestampMs(tick);
  if (tickMs <= 0) return false;
  return (nowMs - tickMs) <= maxAgeMs;
}

export function isUsableLiveTick(
  tick: LiveTick | null | undefined,
): tick is LiveTick {
  const bid = Number(tick?.bid || 0);
  const ask = Number(tick?.ask || 0);
  return Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0 && ask >= bid;
}

export function liveTickAgeMs(
  tick: LiveTick | null | undefined,
  nowMs: number = Date.now(),
): number {
  const tickMs = tickTimestampMs(tick);
  return tickMs > 0 ? Math.max(0, nowMs - tickMs) : Number.POSITIVE_INFINITY;
}

export function mergeLiveTicks<T extends LiveTick>(
  previous: Record<string, T>,
  incoming: Record<string, T>,
  nowMs: number = Date.now(),
  maxAgeMs: number = DEFAULT_MAX_TICK_AGE_MS,
): Record<string, T> {
  const next: Record<string, T> = {};
  for (const [key, tick] of Object.entries(previous || {})) {
    if (isFreshLiveTick(tick, nowMs, maxAgeMs)) next[key] = tick;
  }
  for (const [key, tick] of Object.entries(incoming || {})) {
    if (isFreshLiveTick(tick, nowMs, maxAgeMs)) next[key] = tick;
  }
  return next;
}
