import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTransition } from 'react';
import { RefreshCw, AlertOctagon, X, TrendingUp, TrendingDown, Minus, Play, HelpCircle, DollarSign, ShieldCheck, Clock, Zap, Power, CheckCircle, XCircle, Plus, Pencil } from 'lucide-react';
import {
  Area,
  Bar,
  Cell,
  ComposedChart,
  XAxis,
  YAxis,
  ReferenceLine,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import { api } from '../utils/api';
import { runtimeConfig } from '../config';
import type { StatusResponse, PositionInfo, Recommendation, OrderResult, TickData, StrategyProfile, BarData, ExplainableTradeCard } from '../types';
import ConnectionBar from '../components/ConnectionBar';
import { getSymbolName, getSymbolEmoji, getSignalStrength, getActionDescription, explainProfitLoss } from '../utils/symbolNames';
import {
  detectBrowserCountryCode,
  extractAvailableMarketRows,
  pickPopularSymbolsForCountry,
  popularMarketLabelForCountry,
  selectAllMarketSymbols,
} from '../utils/popularMarkets';
import { priceDistanceToUsd, usdPerPriceDistance } from '../utils/moneyConversion';
import { dollarTargetToPrice } from '../utils/tradeTargets';
import {
  commentStartedAmount,
  positionMarginUsedAmount,
  positionStartedAmount,
  positiveAmount,
} from '../utils/positionValues';
import { isUsableLiveTick, mergeLiveTicks } from '../utils/liveTicks';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
  reconnecting?: boolean;
}
type AvailableSymbolRow = {
  name: string;
  description?: string;
  category?: string;
  bid?: number;
  ask?: number;
  spread?: number;
  visible?: boolean;
  trade_enabled?: boolean;
  live_quote?: boolean;
  requires_connection?: boolean;
  source?: string;
  active_in_current_mode?: boolean;
  inactive_reason?: string;
};

const MARKET_SELECTION_STORAGE_KEY = 'trading_selected_markets_v2';
const MARKET_SELECTION_MANUAL_KEY = 'trading_selected_markets_manual_v1';
const DASHBOARD_SCAN_CACHE_KEY = 'trading_dashboard_scan_cache_v3';
const POSITIONS_CACHE_KEY = 'trading_positions_cache_v1';
const POSITIONS_CACHE_TTL_MS = 2 * 60 * 1000;
const DECLINED_TRADE_RETRY_SECONDS = 8;
const LIVE_CONFIDENCE_REFRESH_MIN_INTERVAL_MS = 30000;
const AUTO_TRADE_LOG_PREVIEW_CHARS = 150;

function compactAutoTradeLogText(log: {
  detail?: string;
  decision_reason?: string;
}) {
  const detail = String(log.detail || '').trim();
  const reason = String(log.decision_reason || '').trim();
  if (!reason || reason === detail || detail.includes(reason)) {
    return detail || reason || 'Activity updated.';
  }
  return `${detail || 'Activity updated.'} Reason: ${reason}`;
}

function autoTradeLogColor(log: {
  action?: string;
  success?: boolean;
}) {
  if (log.success === true) return 'var(--accent-green)';
  if (log.success === false) return 'var(--accent-red)';
  const action = String(log.action || '').toUpperCase();
  if (action === 'BUY') return 'var(--accent-green)';
  if (action === 'SELL') return 'var(--accent-red)';
  if (action.includes('CLOSE') || action.includes('REVERSAL')) return 'var(--accent-blue)';
  if (action === 'HOLD') return 'var(--accent-yellow)';
  return 'var(--text-secondary)';
}

function normalizePlatformId(platform: string | null | undefined): 'mt5' {
  void platform;
  return 'mt5';
}

function scopedStorageKey(baseKey: string, scope: string | null | undefined): string {
  const raw = String(scope || '').trim().toLowerCase();
  const normalized = raw.includes(':') ? raw.replace(/[^a-z0-9:_-]/g, '_') : normalizePlatformId(raw);
  return `${baseKey}:${normalized}`;
}
function formatPct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  return `${value.toFixed(1)}%`;
}

function compactReasons(reasons?: string[], fallback?: string) {
  if (reasons && reasons.length > 0) return reasons.slice(0, 3);
  return fallback ? [fallback] : [];
}

function isClosedMarketReason(reason: string | null | undefined) {
  const text = String(reason || '').trim().toLowerCase();
  if (!text) return false;
  return (
    text.includes('market closed')
    || text.includes('outside regular session')
    || text.includes('outside us cash session')
    || text.includes('outside european cash session')
    || text.includes('outside the configured cash session')
    || text.includes('outside the preferred session window')
    || text.includes('daily rollover break')
    || text.includes('weekend')
    || text.includes('no executable live bid/ask')
  );
}
function isClosedMarketRecommendation(rec: Recommendation) {
  const reasons = [
    ...(rec.trade_quality?.no_trade_reasons || []),
    ...(rec.portfolio_risk?.blocking_reasons || []),
    ...(rec.anti_churn?.reasons || []),
    rec.execution_reason || '',
    rec.signal.reason || '',
  ];
  return reasons.some((reason) => isClosedMarketReason(reason));
}

function summarizeGeminiError(error?: string | null) {
  const raw = (error || '').trim();
  if (!raw) return null;
  const lowered = raw.toLowerCase();
  if (lowered.includes('resource_exhausted') || lowered.includes('quota exceeded')) {
    return 'Quota exhausted. Gemini is temporarily unavailable.';
  }
  if (lowered.includes('api key') && lowered.includes('not set')) {
    return 'Gemini API key is missing.';
  }
  if (lowered.includes('timeout')) {
    return 'Gemini request timed out.';
  }
  if (lowered.includes('permission') || lowered.includes('forbidden') || lowered.includes('403')) {
    return 'Gemini permission was denied.';
  }
  return raw.length > 140 ? `${raw.slice(0, 140)}...` : raw;
}

function formatUiError(error: unknown) {
  const raw = String((error as any)?.message || error || '').trim();
  const lower = raw.toLowerCase();
  if (!raw) return 'Request failed. Please try again.';
  if (lower.includes('failed to fetch') || lower.includes('network request failed')) {
    return 'Backend connection issue. Please wait a few seconds and try again.';
  }
  if (lower.includes('timed out') || lower.includes('timeout')) {
    return 'Request timed out. Please try again.';
  }
  if (lower.includes('retrying automatically')) {
    return 'Backend is reconnecting. Please retry now.';
  }
  return raw;
}

function shouldRouteToManualConnect(error: unknown) {
  const raw = String((error as any)?.message || error || '').trim().toLowerCase();
  if (!raw) return true;
  return (
    raw.includes('not found')
    || raw.includes('no saved')
    || raw.includes('failed to fetch')
    || raw.includes('network request failed')
    || raw.includes('timed out')
    || raw.includes('timeout')
    || raw.includes('retrying automatically')
  );
}
function isSoftTimeoutLikeError(message: string) {
  const lower = String(message || '').trim().toLowerCase();
  return (
    lower.includes('timeout')
    || lower.includes('timed out')
    || lower.includes('failed to fetch')
    || lower.includes('network request failed')
    || lower.includes('retrying automatically')
  );
}

function buildTickStreamUrl(symbols: string[]) {
  const base = runtimeConfig.apiBaseUrl.replace(/\/+$/, '');
  return `${base}/market/ticks/stream?symbols=${encodeURIComponent(symbols.join(','))}`;
}

function alternateLocalTickStreamUrl(symbols: string[]) {
  if (!runtimeConfig.allowLocalFallback) return null;
  const base = runtimeConfig.apiBaseUrl.replace(/\/+$/, '');
  const query = encodeURIComponent(symbols.join(','));
  if (base.includes('127.0.0.1:8000')) {
    return `${base.replace('127.0.0.1:8000', '127.0.0.1:8002')}/market/ticks/stream?symbols=${query}`;
  }
  if (base.includes('localhost:8000')) {
    return `${base.replace('localhost:8000', 'localhost:8002')}/market/ticks/stream?symbols=${query}`;
  }
  if (base.includes('127.0.0.1:8002')) {
    return `${base.replace('127.0.0.1:8002', '127.0.0.1:8000')}/market/ticks/stream?symbols=${query}`;
  }
  if (base.includes('localhost:8002')) {
    return `${base.replace('localhost:8002', 'localhost:8000')}/market/ticks/stream?symbols=${query}`;
  }
  return null;
}

function hasTickPayloadChanged(
  prev: Record<string, TickData>,
  next: Record<string, TickData>,
) {
  const prevKeys = Object.keys(prev || {});
  const nextKeys = Object.keys(next || {});
  if (prevKeys.length !== nextKeys.length) return true;
  for (const key of nextKeys) {
    const a = (prev as any)?.[key] as any;
    const b = (next as any)?.[key] as any;
    if (!a || !b) return true;
    if (
      Number(a.bid || 0) !== Number(b.bid || 0)
      || Number(a.ask || 0) !== Number(b.ask || 0)
      || Number(a.time || 0) !== Number(b.time || 0)
    ) {
      return true;
    }
  }
  return false;
}

function formatCommissionLabel(rec: Recommendation, currency: string) {
  const side = rec.commission_per_lot_side;
  const roundTurn = rec.commission_round_turn_per_lot;
  const pct = rec.commission_percent_rate;
  const notional = rec.commission_notional_1lot;
  const model = String(rec.commission_model || '').toLowerCase();
  const samples = typeof rec.commission_samples === 'number' ? rec.commission_samples : null;

  if (typeof side === 'number' && side > 0) {
    const rtText = typeof roundTurn === 'number' ? ` - ${currency} ${roundTurn.toFixed(2)} round-turn` : '';
    const src = model === 'realized_history' && samples && samples > 0
      ? ` - from ${samples} closed deal(s)`
      : '';
    return `Commission: ${currency} ${side.toFixed(2)} / lot / side${rtText}${src}`;
  }
  if (model === 'percent_notional' && typeof pct === 'number' && pct > 0) {
    const sideEstimate = typeof notional === 'number' && notional > 0 ? (notional * pct) / 100 : null;
    const estText = typeof sideEstimate === 'number' ? ` (~${currency} ${sideEstimate.toFixed(2)} / side)` : '';
    return `Commission: ${pct.toFixed(4)}% of notional${estText}`;
  }
  if (String(model || '').endsWith('_not_implemented')) {
    return 'Commission: broker-side model (not available in current feed)';
  }
  return 'Commission: unknown from broker feed (will auto-learn after first closed trade)';
}

function formatCooldown(seconds?: number | null) {
  const s = Math.max(0, Math.floor(Number(seconds || 0)));
  if (s <= 0) return '0s';
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m <= 0) return `${s}s`;
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function formatElapsedOpenTime(openedAtSeconds?: number | null, nowMs: number = Date.now()) {
  const rawOpened = Number(openedAtSeconds || 0);
  if (!rawOpened || Number.isNaN(rawOpened)) return '00:00';
  let openedMs = rawOpened > 1_000_000_000_000 ? rawOpened : rawOpened * 1000;
  // Some MT5 brokers expose position open time in a server-local clock that lands a few
  // hours ahead of the browser. Normalize that skew so the timer keeps counting live.
  if (openedMs > nowMs + 60_000) {
    const futureHours = Math.max(1, Math.ceil((openedMs - nowMs) / 3_600_000));
    openedMs -= futureHours * 3_600_000;
  }
  const elapsedSeconds = Math.max(0, Math.floor((nowMs - openedMs) / 1000));
  const days = Math.floor(elapsedSeconds / 86400);
  const hours = Math.floor((elapsedSeconds % 86400) / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  const seconds = elapsedSeconds % 60;
  const hh = String(hours).padStart(2, '0');
  const mm = String(minutes).padStart(2, '0');
  const ss = String(seconds).padStart(2, '0');
  if (days > 0) return `${days} day${days === 1 ? '' : 's'} ${hh}:${mm}`;
  if (hours > 0) return `${hh}:${mm}:${ss}`;
  return `${mm}:${ss}`;
}

function formatMinutesCompact(totalMinutes?: number | null) {
  const minutes = Math.max(0, Math.floor(Number(totalMinutes || 0)));
  if (!Number.isFinite(minutes) || minutes <= 0) return '0m';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rem = minutes % 60;
  if (rem <= 0) return `${hours}h`;
  return `${hours}h ${rem}m`;
}

function formatEtaTime(unixSeconds?: number | null) {
  const raw = Number(unixSeconds || 0);
  if (!Number.isFinite(raw) || raw <= 0) return '';
  const date = new Date(raw * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatRetryCountdown(targetMs?: number | null, nowMs: number = Date.now()) {
  const remainingSeconds = Math.max(0, Math.ceil(((targetMs || 0) - nowMs) / 1000));
  return formatCooldown(remainingSeconds);
}

function toFiniteNumber(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

type GeminiTelemetry = {
  available: boolean;
  degraded: boolean;
  last_error: string | null;
  state: string;
  primary_model: string;
  active_model: string;
  using_fallback_model: boolean;
  cooldown_seconds: number;
  credits_pct: number;
};

function normalizeGeminiTelemetry(
  incoming: Partial<GeminiTelemetry>,
  previous?: GeminiTelemetry,
): GeminiTelemetry {
  const available = incoming.available ?? previous?.available ?? false;
  const cooldown = Math.max(0, toFiniteNumber(incoming.cooldown_seconds) ?? previous?.cooldown_seconds ?? 0);
  const rawState = String(incoming.state || previous?.state || '').trim();
  const isCooldown = rawState === 'cooldown' || cooldown > 0;
  const degraded = incoming.degraded ?? previous?.degraded ?? false;
  const state = isCooldown
    ? 'cooldown'
    : degraded
      ? 'degraded'
      : rawState === 'ready'
        ? 'ready'
      : available
        ? 'available'
        : 'unavailable';
  const credits = toFiniteNumber(incoming.credits_pct);
  return {
    available,
    degraded: degraded || isCooldown,
    last_error: incoming.last_error ?? previous?.last_error ?? null,
    state,
    primary_model: String(incoming.primary_model ?? previous?.primary_model ?? '').trim(),
    active_model: String(incoming.active_model ?? previous?.active_model ?? '').trim(),
    using_fallback_model: Boolean(incoming.using_fallback_model ?? previous?.using_fallback_model ?? false),
    cooldown_seconds: cooldown,
    credits_pct: Math.max(0, Math.min(100, credits ?? (degraded || isCooldown ? 5 : previous?.credits_pct ?? 0))),
  };
}

function readCachedPositions(platform: string | null | undefined): PositionInfo[] {
  const key = scopedStorageKey(POSITIONS_CACHE_KEY, platform);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { savedAt?: number; items?: PositionInfo[] };
    if (!parsed || !Array.isArray(parsed.items)) return [];
    const savedAt = Number(parsed.savedAt || 0);
    if (!Number.isFinite(savedAt) || (Date.now() - savedAt) > POSITIONS_CACHE_TTL_MS) {
      localStorage.removeItem(key);
      return [];
    }
    return parsed.items;
  } catch {
    return [];
  }
}

function writeCachedPositions(platform: string | null | undefined, items: PositionInfo[]) {
  const key = scopedStorageKey(POSITIONS_CACHE_KEY, platform);
  try {
    if (!Array.isArray(items) || items.length === 0) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify({
      savedAt: Date.now(),
      items,
    }));
  } catch {
    // ignore cache errors and keep UI responsive
  }
}

function hasExecutionTrace(trace: PositionInfo['execution_trace'] | null | undefined): boolean {
  if (!trace) return false;
  return Boolean(
    String(trace.source || '').trim()
    || String(trace.reason || '').trim()
    || String(trace.policy_mode || '').trim()
    || String(trace.active_agent_name || trace.requested_agent_name || '').trim()
    || String(trace.strategy?.name || trace.strategy?.brief || '').trim()
    || String(trace.gemini?.active_model || '').trim()
    || String(trace.meta_model?.version_id || '').trim()
    || String(trace.signal_snapshot?.reason || '').trim()
  );
}

function mergeStablePositionSnapshot(prev: PositionInfo[], next: PositionInfo[]): PositionInfo[] {
  const keepDetail = <T,>(value: T | null | undefined, fallback: T | null | undefined): T | null | undefined => {
    return value !== undefined && value !== null ? value : fallback;
  };
  const previousByTicket = new Map(prev.map((position, index) => [position.ticket, { position, index }]));
  const merged = next.map((position) => {
    const previous = previousByTicket.get(position.ticket)?.position;
    if (!previous) return position;
    return {
      ...position,
      execution_trace: hasExecutionTrace(position.execution_trace) ? position.execution_trace : previous.execution_trace,
      signal_reason: position.signal_reason || previous.signal_reason || null,
      risk_reason: position.risk_reason || previous.risk_reason || null,
      expected_profit_time_minutes: position.expected_profit_time_minutes ?? previous.expected_profit_time_minutes ?? null,
      expected_profit_time_remaining_minutes: position.expected_profit_time_remaining_minutes ?? previous.expected_profit_time_remaining_minutes ?? null,
      expected_profit_eta_timestamp: position.expected_profit_eta_timestamp ?? previous.expected_profit_eta_timestamp ?? null,
      estimated_margin: keepDetail(position.estimated_margin, previous.estimated_margin) ?? null,
      started_with_usd: keepDetail(position.started_with_usd, previous.started_with_usd) ?? null,
      entry_market_value_usd: keepDetail(position.entry_market_value_usd, previous.entry_market_value_usd) ?? null,
      margin_used_usd: keepDetail(position.margin_used_usd, previous.margin_used_usd) ?? null,
      margin_used_source: keepDetail(position.margin_used_source, previous.margin_used_source) ?? null,
      budget_vs_margin_delta_usd: keepDetail(position.budget_vs_margin_delta_usd, previous.budget_vs_margin_delta_usd) ?? null,
      budget_vs_margin_delta_pct: keepDetail(position.budget_vs_margin_delta_pct, previous.budget_vs_margin_delta_pct) ?? null,
      sl_amount_usd: keepDetail(position.sl_amount_usd, previous.sl_amount_usd) ?? null,
      tp_amount_usd: keepDetail(position.tp_amount_usd, previous.tp_amount_usd) ?? null,
      target_management_mode: keepDetail(position.target_management_mode, previous.target_management_mode),
      target_management_updated_at: keepDetail(position.target_management_updated_at, previous.target_management_updated_at) ?? null,
    };
  });

  return merged.sort((a, b) => {
    const prevA = previousByTicket.get(a.ticket)?.index;
    const prevB = previousByTicket.get(b.ticket)?.index;
    if (prevA != null && prevB != null) return prevA - prevB;
    if (prevA != null) return -1;
    if (prevB != null) return 1;
    const timeDiff = Number(a.time || 0) - Number(b.time || 0);
    if (timeDiff !== 0) return timeDiff;
    return Number(a.ticket || 0) - Number(b.ticket || 0);
  });
}

function normalizeUnitInterval(value: unknown, fallback: number = 0): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(1, n));
}

function recommendationConfidence(rec: Recommendation): number {
  const direct = toFiniteNumber(rec?.signal?.confidence);
  const qualityFallback = toFiniteNumber(rec?.trade_quality?.final_trade_quality_score);
  if (direct != null && direct > 0 && qualityFallback != null && qualityFallback > 0) {
    return normalizeUnitInterval(Math.max(direct, qualityFallback), 0);
  }
  if (direct != null && direct > 0) {
    return normalizeUnitInterval(direct, 0);
  }
  if (qualityFallback != null && qualityFallback > 0) {
    return normalizeUnitInterval(qualityFallback, 0);
  }
  if (direct != null) {
    return normalizeUnitInterval(direct, 0);
  }
  return 0;
}
function recommendationQuality(rec: Recommendation): number {
  const quality = normalizeUnitInterval(rec?.trade_quality?.final_trade_quality_score, -1);
  if (quality >= 0) return quality;
  return recommendationConfidence(rec);
}

function recommendationQualityThreshold(rec: Recommendation): number {
  return normalizeUnitInterval(rec?.trade_quality?.threshold, 0);
}

function hasUnavailableScore(rec: Recommendation): boolean {
  const reasons = [
    String(rec?.execution_reason || ''),
    String(rec?.signal?.reason || ''),
    String(rec?.explanation || ''),
  ].join(' ').toLowerCase();
  const degraded = Array.isArray(rec?.degraded_reasons)
    ? rec.degraded_reasons.map((r) => String(r || '').toLowerCase()).join(' ')
    : '';
  const quality = toFiniteNumber(rec?.trade_quality?.final_trade_quality_score);
  const confidence = toFiniteNumber(rec?.signal?.confidence);
  const hasNumericScore = (quality != null && quality > 0) || (confidence != null && confidence > 0);
  if (hasNumericScore) {
    return false;
  }
  if (
    reasons.includes('waiting for scan')
    || reasons.includes('pending analysis')
    || reasons.includes('waiting for live quote')
    || reasons.includes('no live tick')
    || reasons.includes('no live quote')
    || reasons.includes('quote unavailable')
    || reasons.includes('scan timed out')
    || reasons.includes('market closed')
    || reasons.includes('not executable')
    || degraded.includes('timeout')
    || degraded.includes('market_guard')
    || degraded.includes('scan_failed')
  ) {
    return true;
  }
  return (quality == null || quality <= 0) && (confidence == null || confidence <= 0);
}

function shouldKeepStableRecommendation(prev: Recommendation, next: Recommendation): boolean {
  const prevConfidence = recommendationConfidence(prev);
  const nextConfidence = recommendationConfidence(next);
  const degradedReasons = Array.isArray(next?.degraded_reasons) ? next.degraded_reasons : [];
  const nextReason = String(next?.execution_reason || next?.signal?.reason || '').toLowerCase();
  const unstable = degradedReasons.some((reason) => {
    const text = String(reason || '').toLowerCase();
    return text.includes('timeout') || text.includes('market_guard') || text.includes('failed');
  }) || nextReason.includes('timeout')
    || nextReason.includes('outside')
    || nextReason.includes('market closed')
    || nextReason.includes('waiting for live quote')
    || nextReason.includes('no live tick')
    || nextReason.includes('no live quote')
    || nextReason.includes('quote unavailable');
  return prevConfidence > 0 && nextConfidence <= 0 && unstable;
}

function mergeRecommendationStable(prev: Recommendation, next: Recommendation): Recommendation {
  if (!shouldKeepStableRecommendation(prev, next)) {
    return { ...prev, ...next, category: next.category || prev.category };
  }
  return {
    ...prev,
    ...next,
    signal: prev.signal,
    trade_quality: prev.trade_quality,
    category: next.category || prev.category,
  };
}

function latestActivityScoresByIdentity(logs: Array<any>): Map<string, { confidence?: number; quality?: number; timestamp: number }> {
  const byIdentity = new Map<string, { confidence?: number; quality?: number; timestamp: number }>();
  for (const log of logs) {
    const identity = marketIdentityKey(log?.symbol);
    if (!identity) continue;
    const confidence = normalizeUnitInterval(log?.confidence, -1);
    const quality = normalizeUnitInterval(log?.quality_score, -1);
    if (confidence < 0 && quality < 0) continue;
    const timestamp = Number(log?.timestamp || 0);
    const prev = byIdentity.get(identity);
    if (prev && prev.timestamp > timestamp) continue;
    byIdentity.set(identity, {
      confidence: confidence >= 0 ? confidence : undefined,
      quality: quality >= 0 ? quality : undefined,
      timestamp: Number.isFinite(timestamp) ? timestamp : 0,
    });
  }
  return byIdentity;
}

function mergeActivityScores(rec: Recommendation, score: { confidence?: number; quality?: number }): Recommendation {
  const confidence = typeof score.confidence === 'number' ? score.confidence : null;
  const quality = typeof score.quality === 'number' ? score.quality : null;
  if (confidence == null && quality == null) return rec;
  const currentConfidence = recommendationConfidence(rec);
  const currentQuality = recommendationQuality(rec);
  const nextConfidence = confidence != null && confidence > 0 ? confidence : currentConfidence;
  const nextQuality = quality != null && quality > 0 ? quality : currentQuality;
  if (nextConfidence === currentConfidence && nextQuality === currentQuality) return rec;
  return {
    ...rec,
    signal: {
      ...rec.signal,
      confidence: nextConfidence,
    },
    trade_quality: {
      ...(rec.trade_quality || {}),
      final_trade_quality_score: nextQuality,
    },
  };
}

function symbolKey(symbol: string | null | undefined): string {
  return String(symbol || '').trim().toUpperCase();
}

function normalizeCategoryName(category: string | null | undefined): string {
  const raw = String(category || '').trim().toLowerCase();
  if (!raw) return 'Other';
  if (raw.includes('commod') || raw.includes('metal') || raw.includes('energy')) return 'Commodities';
  if (raw.includes('indice') || raw.includes('index')) return 'Indices';
  if (raw.includes('stock') || raw.includes('equit') || raw.includes('share') || raw.includes('etf')) return 'Stocks';
  if (raw.includes('forex') || raw.includes('fx') || raw.includes('currency')) return 'Forex';
  if (raw.includes('crypto') || raw.includes('coin') || raw.includes('token')) return 'Crypto';
  if (raw === 'other' || raw === 'others' || raw === 'misc') return 'Other';
  return 'Other';
}

function marketIdentityKey(symbol: string | null | undefined): string {
  const key = symbolKey(symbol);
  const aliases: Record<string, string> = {
    GOLD: 'XAUUSD',
    XAUUSD: 'XAUUSD',
    SILVER: 'XAGUSD',
    XAGUSD: 'XAGUSD',
    SP500: 'US500',
    US500: 'US500',
    DAX: 'GER40',
    GER40: 'GER40',
    FTSE100: 'UK100',
    UK100: 'UK100',
    WTID: 'WTI',
    WTI: 'WTI',
    USOIL: 'WTI',
    XTIUSD: 'WTI',
    BRENT: 'BRENT',
    UKOIL: 'BRENT',
    XBRUSD: 'BRENT',
    // Common display-name -> ticker aliases used by MT5 broker catalogs
    APPLE: 'AAPL',
    MICROSOFT: 'MSFT',
    NVIDIA: 'NVDA',
    TESLA: 'TSLA',
    AMAZON: 'AMZN',
    GOOGLE: 'GOOGL',
    'META (FACEBOOK)': 'META',
    META: 'META',
    'DOW JONES': 'US30',
    'S&P500': 'US500',
    NASDAQ: 'US100',
  };
  return aliases[key] || key;
}

function dedupeSymbolsByIdentity(symbols: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const symbol of symbols) {
    const raw = String(symbol || '').trim();
    if (!raw) continue;
    const identity = marketIdentityKey(raw);
    if (seen.has(identity)) continue;
    seen.add(identity);
    out.push(raw);
  }
  return out;
}

function dedupeRowsByIdentity(rows: AvailableSymbolRow[]): AvailableSymbolRow[] {
  const byIdentity = new Map<string, AvailableSymbolRow>();
  const rowScore = (row: AvailableSymbolRow): number => {
    const categoryScore = normalizeCategoryName(row.category) !== 'Other' ? 4 : 0;
    const liveQuoteScore = row.live_quote === false ? 0 : 3;
    const connectedRowScore = row.requires_connection ? 0 : 2;
    const tradeEnabledScore = row.trade_enabled ? 2 : 0;
    const quoteReadyScore = (Number(row.bid || 0) > 0 && Number(row.ask || 0) > 0) ? 1 : 0;
    return categoryScore + liveQuoteScore + connectedRowScore + tradeEnabledScore + quoteReadyScore;
  };
  for (const row of rows) {
    const identity = marketIdentityKey(row.name);
    if (!identity) continue;
    const prev = byIdentity.get(identity);
    if (!prev) {
      byIdentity.set(identity, row);
      continue;
    }
    if (rowScore(row) > rowScore(prev)) {
      byIdentity.set(identity, row);
    }
  }
  return Array.from(byIdentity.values());
}

function isPreviewMarketRow(row: AvailableSymbolRow): boolean {
  return row.live_quote === false || row.requires_connection === true || String(row.source || '').includes('disconnected');
}

function liveQuoteSortScore(row: AvailableSymbolRow): number {
  if (hasUsableBrokerQuote(row)) return 3;
  if (!isPreviewMarketRow(row) && row.live_quote !== false) return 2;
  if (row.trade_enabled) return 1;
  return 0;
}

function hasUsableBrokerQuote(row: AvailableSymbolRow): boolean {
  const bid = Number(row.bid || 0);
  const ask = Number(row.ask || 0);
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || ask < bid) {
    return false;
  }
  const mid = (bid + ask) / 2;
  if (mid <= 0) return false;
  const spreadPrice = Math.max(0, ask - bid);
  const spreadPct = spreadPrice / mid;
  return spreadPct <= 0.05;
}

function dedupeRecommendationsByIdentity(recommendations: Recommendation[]): Recommendation[] {
  const out: Recommendation[] = [];
  const seen = new Set<string>();
  for (const rec of recommendations) {
    const identity = marketIdentityKey(rec.symbol);
    if (!identity || seen.has(identity)) continue;
    seen.add(identity);
    out.push(rec);
  }
  return out;
}

function chunkSymbols(symbols: string[], size: number): string[][] {
  if (!Array.isArray(symbols) || symbols.length === 0) return [];
  const chunkSize = Math.max(1, Math.floor(size || 1));
  const out: string[][] = [];
  for (let i = 0; i < symbols.length; i += chunkSize) {
    out.push(symbols.slice(i, i + chunkSize));
  }
  return out;
}

function loadDashboardScanCache(platform: string | null | undefined): {
  recommendations: Recommendation[];
  allSymbols: AvailableSymbolRow[];
  analyzedCount: number;
  availableMarketCount: number;
  lastScan: number | null;
} | null {
  const key = scopedStorageKey(DASHBOARD_SCAN_CACHE_KEY, platform);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const scannedAt = Number(parsed?.lastScan || 0);
    if (!Number.isFinite(scannedAt) || scannedAt <= 0) return null;
    if ((Date.now() / 1000) - scannedAt > 15 * 60) return null;
    return {
      recommendations: Array.isArray(parsed?.recommendations) ? parsed.recommendations : [],
      allSymbols: Array.isArray(parsed?.allSymbols) ? parsed.allSymbols : [],
      analyzedCount: Number(parsed?.analyzedCount || 0),
      availableMarketCount: Number(parsed?.availableMarketCount || 0),
      lastScan: scannedAt,
    };
  } catch {
    return null;
  }
}

function saveDashboardScanCache(platform: string | null | undefined, data: {
  recommendations: Recommendation[];
  allSymbols: AvailableSymbolRow[];
  analyzedCount: number;
  availableMarketCount: number;
  lastScan: number | null;
}) {
  const key = scopedStorageKey(DASHBOARD_SCAN_CACHE_KEY, platform);
  try {
    localStorage.setItem(key, JSON.stringify(data));
  } catch {
    // ignore storage pressure
  }
}

function buildPreferredScanIdentitySet(
  platform: string | undefined,
  allSymbols: AvailableSymbolRow[],
  countryCode: string,
): Set<string> {
  void platform;
  const preferred = pickPopularSymbolsForCountry(allSymbols, countryCode);
  return new Set(preferred.map((sym) => marketIdentityKey(sym)).filter(Boolean));
}

const MAX_UI_SYMBOL_ROWS = 400;
const MAX_MT5_SCAN_SYMBOLS = 20;
const MAX_MANUAL_SCAN_SYMBOLS = 60;
const SUPPORTED_MARKET_CATEGORIES = ['Forex', 'Indices', 'Commodities', 'Stocks', 'Crypto'];

function createPendingRecommendation(symbol: AvailableSymbolRow): Recommendation {
  const bid = Number(symbol.bid || 0);
  const ask = Number(symbol.ask || 0);
  const entry = bid > 0 && ask > 0 ? (bid + ask) / 2 : Math.max(bid, ask, 0);
  const hasLiveQuote = bid > 0 && ask > 0;
  return {
    symbol: String(symbol.name || ''),
    category: normalizeCategoryName(symbol.category),
    signal: {
      action: 'HOLD',
      confidence: 0,
      stop_loss: null,
      take_profit: null,
      max_holding_minutes: null,
      reason: hasLiveQuote ? 'Waiting for scan.' : 'Waiting for live quote and scan.',
    },
    signal_id: null,
    risk_decision: {
      approved: false,
      reason: 'Pending analysis.',
      adjusted_volume: 0,
      warnings: [],
      status: 'warn',
      machine_reasons: [],
      metrics_snapshot: {},
    },
    entry_price_estimate: entry,
    explanation: hasLiveQuote
      ? 'Pending scan for this market.'
      : 'Live quote is not available yet for this market. Analysis will start after quote arrives.',
    ready_to_execute: false,
    degraded_reasons: [],
    execution_reason: 'Pending analysis.',
  };
}

function prioritizeRowsBySymbols(rows: AvailableSymbolRow[], prioritySymbols: string[]): AvailableSymbolRow[] {
  if (!prioritySymbols.length || !rows.length) return rows;
  const priorityIndex = new Map<string, number>();
  prioritySymbols.forEach((symbol, index) => {
    const key = marketIdentityKey(symbol);
    if (key && !priorityIndex.has(key)) priorityIndex.set(key, index);
  });
  const prioritized: AvailableSymbolRow[] = [];
  const remaining: AvailableSymbolRow[] = [];
  for (const row of rows) {
    const key = marketIdentityKey(row.name);
    if (priorityIndex.has(key)) prioritized.push(row);
    else remaining.push(row);
  }
  prioritized.sort((a, b) => {
    const aIndex = priorityIndex.get(marketIdentityKey(a.name)) ?? Number.MAX_SAFE_INTEGER;
    const bIndex = priorityIndex.get(marketIdentityKey(b.name)) ?? Number.MAX_SAFE_INTEGER;
    return aIndex - bIndex;
  });
  return [...prioritized, ...remaining];
}

function formatTradeCardNumber(value?: number | null, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return '-';
  return Number(value).toFixed(digits);
}

function formatTradeCardPercent(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return '-';
  return `${Math.round(Number(value) * 100)}%`;
}

function compactTradeCardList(items?: unknown[], fallback = '-') {
  if (!Array.isArray(items) || items.length === 0) return fallback;
  return items
    .map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
    .filter(Boolean)
    .slice(0, 4);
}

function ExplainableTradeCardPanel({ signalId, open }: { signalId?: number | null; open: boolean }) {
  const [card, setCard] = useState<ExplainableTradeCard | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !signalId) return undefined;
    let ignore = false;
    setLoading(true);
    setError('');
    api.getTradeCard(signalId)
      .then((result) => {
        if (!ignore) setCard(result);
      })
      .catch((err: any) => {
        if (!ignore) setError(String(err?.message || 'Trade card unavailable.'));
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [open, signalId]);

  if (!open) return null;
  if (!signalId) {
    return (
      <div className="text-muted text-sm mb-3" style={{ padding: 10, border: '1px solid var(--border)', borderRadius: 6 }}>
        This recommendation has not been persisted as a signal yet, so no audit card is available.
      </div>
    );
  }
  if (loading && !card) {
    return (
      <div className="text-muted text-sm mb-3" style={{ padding: 10, border: '1px solid var(--border)', borderRadius: 6 }}>
        Loading trade audit...
      </div>
    );
  }
  if (error && !card) {
    return (
      <div className="text-sm mb-3" style={{ padding: 10, border: '1px solid rgba(239,68,68,0.25)', borderRadius: 6, color: 'var(--accent-red)' }}>
        {error}
      </div>
    );
  }
  if (!card) return null;

  const priceDigits = Number(card.entry_reference_price || 0) < 50 ? 5 : 2;
  const blockReasons = compactTradeCardList(card.block_reasons);
  const warnings = compactTradeCardList(card.disagreement_warnings, '');
  const brokerApproved = card.broker_preflight_status?.approved;

  return (
    <div className="mb-3" style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: 'var(--bg-primary)' }}>
      <div className="flex justify-between items-center" style={{ marginBottom: 10 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700 }}>Explainable Trade Card</div>
          <div className="text-muted" style={{ fontSize: 11 }}>
            Signal #{card.signal_id} {card.model_strategy_version?.strategy ? `- ${String(card.model_strategy_version.strategy)}` : ''}
          </div>
        </div>
        <span
          className={`badge ${card.final_decision === 'approved' || card.final_decision === 'executed' ? 'badge-green' : 'badge-red'}`}
          style={{ fontSize: 11 }}
        >
          {String(card.final_decision || 'pending').toUpperCase()}
        </span>
      </div>

      <div className="grid-3 gap-3" style={{ fontSize: 12, marginBottom: 10 }}>
        <div>
          <div className="text-muted">Confidence</div>
          <strong>{formatTradeCardPercent(card.confidence)}</strong>
        </div>
        <div>
          <div className="text-muted">Risk Mode</div>
          <strong>{card.risk_mode || '-'}</strong>
        </div>
        <div>
          <div className="text-muted">Broker Preflight</div>
          <strong style={{ color: brokerApproved === false ? 'var(--accent-red)' : 'var(--accent-green)' }}>
            {brokerApproved === false ? 'Blocked' : brokerApproved === true ? 'Approved' : 'Pending'}
          </strong>
        </div>
        <div>
          <div className="text-muted">Entry</div>
          <span className="font-mono">{formatTradeCardNumber(card.entry_reference_price, priceDigits)}</span>
        </div>
        <div>
          <div className="text-muted">SL / TP</div>
          <span className="font-mono">
            {formatTradeCardNumber(card.stop_loss, priceDigits)} / {formatTradeCardNumber(card.take_profit, priceDigits)}
          </span>
        </div>
        <div>
          <div className="text-muted">Reward</div>
          <span className="font-mono">{formatTradeCardNumber(card.reward, 2)}</span>
        </div>
      </div>

      {Array.isArray(blockReasons) ? (
        <div style={{ fontSize: 12, color: 'var(--accent-red)', marginBottom: 8 }}>
          {blockReasons.map((reason, index) => (
            <div key={`${reason}-${index}`}>Block: {reason}</div>
          ))}
        </div>
      ) : null}
      {Array.isArray(warnings) && warnings.length > 0 ? (
        <div style={{ fontSize: 12, color: 'var(--accent-yellow)', marginBottom: 8 }}>
          {warnings.map((warning, index) => (
            <div key={`${warning}-${index}`}>Warning: {warning}</div>
          ))}
        </div>
      ) : null}
      {card.broker_preflight_status?.reason ? (
        <div className="text-muted" style={{ fontSize: 12 }}>
          Broker: {card.broker_preflight_status.reason}
        </div>
      ) : null}
    </div>
  );
}

// Beginner-friendly recommendation card
function TradeCard({
  rec,
  onExecuted,
  currency,
  liveTick,
  executionBlockedReason = '',
}: {
  rec: Recommendation;
  onExecuted: () => Promise<void> | void;
  currency: string;
  liveTick?: TickData;
  executionBlockedReason?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [retryAvailableAtMs, setRetryAvailableAtMs] = useState<number | null>(null);
  const [retryNowMs, setRetryNowMs] = useState(() => Date.now());
  const [dismissed, setDismissed] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [showExplainableCard, setShowExplainableCard] = useState(false);
  const [showAmountInput, setShowAmountInput] = useState(false);
  const [amount, setAmount] = useState('');
  const [volumePreview, setVolumePreview] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number; trade_tick_value?: number; trade_tick_size?: number; point?: number } | null>(null);
  // SL/TP as dollar amounts: how much you're willing to lose / want to gain
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');

  useEffect(() => {
    if (!retryAvailableAtMs || !result || result.success) return undefined;
    setRetryNowMs(Date.now());
    const interval = window.setInterval(() => {
      const now = Date.now();
      setRetryNowMs(now);
      if (now >= retryAvailableAtMs) {
        setResult(null);
        setRetryAvailableAtMs(null);
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [retryAvailableAtMs, result]);

  if (dismissed) return null;

  const digits = rec.entry_price_estimate < 50 ? 5 : 2;
  const presetAmounts = [10, 25, 50, 100, 250];
  const minSlTp = volumePreview?.min_sl_tp_dollars || 0;

  // Convert dollar SL/TP to price levels for execution
  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumePreview || volumePreview.volume <= 0 || volumePreview.contract_size <= 0) return 0;
    const livePrice = isSell
      ? (bid || volumePreview.price || rec.entry_price_estimate || 0)
      : (ask || volumePreview.price || rec.entry_price_estimate || 0);
    return dollarTargetToPrice({
      action: rec.signal.action,
      dollarAmount,
      volume: volumePreview.volume,
      contractSize: volumePreview.contract_size,
      symbolInfo: {
        trade_contract_size: volumePreview.contract_size,
        trade_tick_value: volumePreview.trade_tick_value,
        trade_tick_size: volumePreview.trade_tick_size,
        point: volumePreview.point,
      },
      referencePrice: livePrice,
      isStopLoss: isSL,
    }) || 0;
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumePreview(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const preview = await api.calculateVolume(rec.symbol, num);
        setVolumePreview(preview);
        // Leave SL/TP overrides empty by default so the backend can derive them
        // from strategy context, risk mode, news, and recent chart structure.
        setSlDollar('');
        setTpDollar('');
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

  const handleExecute = async () => {
    const amountNum = parseFloat(amount);
    if (!amountNum || amountNum <= 0) return;
    // Convert dollar SL/TP to price levels
    const slAmt = parseFloat(slDollar) || 0;
    const tpAmt = parseFloat(tpDollar) || 0;
    const slPrice = slAmt > 0 ? dollarToPrice(slAmt, true) : undefined;
    const tpPrice = tpAmt > 0 ? dollarToPrice(tpAmt, false) : undefined;
    setLoading(true);
    try {
      let res: OrderResult;
      if (rec.signal_id) {
        res = await api.executeRecommendation(rec.signal_id, amountNum, slPrice, tpPrice);
      } else {
        // Fallback path: some freshly built recommendations may not yet have a persisted signal_id.
        // Execute directly against symbol while keeping deterministic backend risk checks in place.
        res = await api.quickBuy(rec.symbol, amountNum, slPrice, tpPrice, rec.signal.action, {
          signal_id: rec.signal_id ?? null,
          signal_confidence: recommendationConfidence(rec),
          quality_score: recommendationQuality(rec),
          signal_reason: rec.signal.reason || rec.explanation || null,
          signal_agent_name: (rec.signal as any)?.agent_name || null,
        });
      }
      setResult(res);
      setRetryAvailableAtMs(res.success ? null : Date.now() + DECLINED_TRADE_RETRY_SECONDS * 1000);
      setShowAmountInput(false);
      void onExecuted();
    } catch (e: any) {
      setResult({
        success: false, retcode: -1, retcode_desc: formatUiError(e),
        ticket: null, volume: null, price: null,
        stop_loss: null, take_profit: null, comment: '',
      });
      setRetryAvailableAtMs(Date.now() + DECLINED_TRADE_RETRY_SECONDS * 1000);
    } finally {
      setLoading(false);
    }
  };

  const liveBid = Number(liveTick?.bid || 0);
  const liveAsk = Number(liveTick?.ask || 0);
  const hasLiveBidAsk = liveBid > 0 && liveAsk > 0;
  const referencePrice = Number(rec.entry_price_estimate || volumePreview?.price || 0);
  const bid = hasLiveBidAsk ? liveBid : null;
  const ask = hasLiveBidAsk ? liveAsk : null;

  const isBuy = rec.signal.action === 'BUY';
  const isSell = rec.signal.action === 'SELL';
  const isHold = rec.signal.action === 'HOLD';
  const confidenceScore = recommendationConfidence(rec);
  const strength = getSignalStrength(confidenceScore);
  const symbolName = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);
  const qualityScore = recommendationQuality(rec);
  const qualityThreshold = recommendationQualityThreshold(rec);
  const suggestedAmount = rec.ready_to_execute && rec.recommended_amount_usd && rec.recommended_amount_usd > 0
    ? Math.round(rec.recommended_amount_usd)
    : null;
  const suggestedPct = rec.ready_to_execute && rec.recommended_amount_pct_free_margin && rec.recommended_amount_pct_free_margin > 0
    ? rec.recommended_amount_pct_free_margin
    : null;
  const blockReasons = compactReasons(
    [
      ...(rec.trade_quality?.no_trade_reasons || []),
      ...(rec.portfolio_risk?.blocking_reasons || []),
      ...(rec.anti_churn?.reasons || []),
    ],
    rec.execution_reason,
  );
  const retryCountdown = result && !result.success && retryAvailableAtMs
    ? formatRetryCountdown(retryAvailableAtMs, retryNowMs)
    : null;
  const executionBlocked = Boolean(executionBlockedReason);
  if (isHold) {
    return (
      <div className="card" style={{ padding: '16px 20px', opacity: 0.6, borderLeft: '3px solid var(--border)' }}>
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 20 }}>{emoji}</span>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{symbolName}</div>
            <div className="text-muted" style={{ fontSize: 12 }}>
              {rec.explanation || 'No good opportunity right now - waiting'}
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
              Confidence {Math.round(confidenceScore * 100)}%
              {' � '}
              Quality {Math.round(qualityScore * 100)}% / need {Math.round(qualityThreshold * 100)}%
            </div>
            {blockReasons.slice(0, 2).map((reason, index) => (
              <div key={index} style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                {reason}
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  const borderColor = isBuy ? 'var(--accent-green)' : 'var(--accent-red)';
  const actionWord = isBuy ? 'Buy' : 'Sell';
  const actionIcon = isBuy ? <TrendingUp size={16} /> : <TrendingDown size={16} />;
  const actionExplain = isBuy
    ? 'The AI thinks this price will go UP. You buy now and sell later when the price is higher to make a profit.'
    : 'The AI thinks this price will go DOWN. You sell now and buy back later when the price is lower to make a profit.';

  return (
    <div className="card" style={{ borderLeft: `4px solid ${borderColor}`, padding: '20px 24px' }}>
      {/* Header */}
      <div className="flex justify-between items-center mb-3">
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 24 }}>{emoji}</span>
          <div>
            <div style={{ fontSize: 16, fontWeight: 700 }}>{symbolName}</div>
            <div className="text-muted" style={{ fontSize: 12 }}>{rec.symbol}</div>
          </div>
        </div>
        {/* Live Prices */}
        <div style={{ textAlign: 'center', fontFamily: 'monospace' }}>
          {bid !== null && ask !== null && bid > 0 && ask > 0 ? (
            <div className="flex gap-4">
              <div>
                <div style={{ fontSize: 9, color: 'var(--accent-red)', fontWeight: 700 }}>SELL</div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{bid.toFixed(digits)}</div>
              </div>
              <div>
                <div style={{ fontSize: 9, color: 'var(--accent-green)', fontWeight: 700 }}>BUY</div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{ask.toFixed(digits)}</div>
              </div>
            </div>
          ) : referencePrice > 0 ? (
            <div>
              <div style={{ fontSize: 9, color: 'var(--text-muted)', fontWeight: 700 }}>REF</div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{referencePrice.toFixed(digits)}</div>
            </div>
          ) : (
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading prices...</span>
          )}
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className={`badge ${isBuy ? 'badge-green' : 'badge-red'}`} style={{ fontSize: 14, padding: '5px 16px' }}>
            {actionIcon}
            <span style={{ marginLeft: 4 }}>{actionWord}</span>
          </div>
          <div style={{ fontSize: 11, marginTop: 4, color: strength.color, fontWeight: 600 }}>
            {strength.label} Signal � {Math.round(confidenceScore * 100)}%
          </div>
        </div>
      </div>

      {/* Simple explanation */}
      <div style={{
        background: 'var(--bg-tertiary)',
        borderRadius: 8,
        padding: '12px 16px',
        marginBottom: 16,
        fontSize: 13,
        lineHeight: 1.6,
        color: 'var(--text-secondary)',
      }}>
        {rec.explanation}
      </div>

      {/* What this means for you */}
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
        <HelpCircle size={12} />
        {actionExplain}
      </div>

      {/* Safety info */}
      <div className="flex gap-3 mb-3" style={{ fontSize: 11 }}>
        <div className="flex items-center gap-2" style={{ color: 'var(--accent-green)' }}>
          <ShieldCheck size={12} />
          Stop-loss protection included
        </div>
        <div className="flex items-center gap-2 text-muted">
          <DollarSign size={12} />
          Risk managed automatically
        </div>
        {rec.signal.max_holding_minutes && (
          <div className="flex items-center gap-2 text-muted">
            <Clock size={12} />
            ~{rec.signal.max_holding_minutes}min hold
          </div>
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 10 }}>
        {formatCommissionLabel(rec, currency)}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, marginBottom: 12 }}>
        <div style={{ background: 'var(--bg-primary)', borderRadius: 8, padding: '10px 12px' }}>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Trade Quality</div>
          <div style={{ fontSize: 16, fontWeight: 700, color: qualityScore >= qualityThreshold ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {Math.round(qualityScore * 100)}%
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Need {Math.round(qualityThreshold * 100)}%</div>
        </div>
        <div style={{ background: 'var(--bg-primary)', borderRadius: 8, padding: '10px 12px' }}>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Projected Margin</div>
          <div style={{ fontSize: 16, fontWeight: 700 }}>
            {formatPct(rec.portfolio_risk?.projected_margin_utilization_pct)}
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>after fill</div>
        </div>
        <div style={{ background: 'var(--bg-primary)', borderRadius: 8, padding: '10px 12px' }}>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Gemini</div>
          <div style={{ fontSize: 14, fontWeight: 700 }}>
            {rec.gemini_confirmation?.degraded ? 'Degraded' : rec.gemini_confirmation?.used ? 'Advisory' : 'Off'}
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            {rec.gemini_confirmation?.news_bias || 'technical only'}
          </div>
        </div>
      </div>

      {/* Expandable details */}
      <div className="flex gap-2 mb-3" style={{ flexWrap: 'wrap' }}>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => setShowDetails(!showDetails)}
          style={{ fontSize: 11, opacity: 0.6 }}
        >
          {showDetails ? 'Hide' : 'Show'} technical details
        </button>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => setShowExplainableCard(!showExplainableCard)}
          style={{ fontSize: 11, opacity: 0.75 }}
        >
          {showExplainableCard ? 'Hide' : 'Show'} trade audit
        </button>
      </div>

      {showDetails && (
        <div className="grid-3 gap-3 mb-3" style={{ fontSize: 12, background: 'var(--bg-primary)', padding: 12, borderRadius: 6 }}>
          <div>
            <span className="text-muted">Entry Price </span>
            <span className="font-mono">{rec.entry_price_estimate.toFixed(rec.entry_price_estimate < 50 ? 5 : 2)}</span>
          </div>
          <div>
            <span className="text-muted">Stop Loss </span>
            <span className="font-mono text-red">{rec.signal.stop_loss?.toFixed(rec.entry_price_estimate < 50 ? 5 : 2) ?? '-'}</span>
          </div>
          <div>
            <span className="text-muted">Take Profit </span>
            <span className="font-mono text-green">{rec.signal.take_profit?.toFixed(rec.entry_price_estimate < 50 ? 5 : 2) ?? '-'}</span>
          </div>
        </div>
      )}

      <ExplainableTradeCardPanel signalId={rec.signal_id} open={showExplainableCard} />

      {blockReasons.length > 0 && (
        <div style={{
          background: 'rgba(59, 130, 246, 0.08)',
          border: '1px solid rgba(59, 130, 246, 0.18)',
          borderRadius: 6,
          padding: '10px 14px',
          fontSize: 12,
          color: 'var(--text-secondary)',
          marginBottom: 12,
        }}>
          {blockReasons.map((reason, index) => (
            <div key={index} style={{ marginBottom: index < blockReasons.length - 1 ? 4 : 0 }}>
              Decision note: {reason}
            </div>
          ))}
        </div>
      )}

      {/* Risk warnings (alerts, not blocks) */}
      {rec.risk_decision?.warnings && rec.risk_decision.warnings.length > 0 && (
        <div style={{
          background: 'rgba(234, 179, 8, 0.08)',
          border: '1px solid rgba(234, 179, 8, 0.2)',
          borderRadius: 6,
          padding: '10px 14px',
          fontSize: 12,
          color: 'var(--accent-yellow)',
          marginBottom: 12,
        }}>
          {rec.risk_decision.warnings.map((w, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: i < rec.risk_decision.warnings.length - 1 ? 4 : 0 }}>
              <span style={{ fontSize: 10 }}>&#9888;</span> {w}
            </div>
          ))}
        </div>
      )}

      {/* Result */}
      {result ? (
        <div style={{
          background: result.success ? 'rgba(34, 197, 94, 0.08)' : 'rgba(239, 68, 68, 0.08)',
          border: `1px solid ${result.success ? 'rgba(34, 197, 94, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
          borderRadius: 8,
          padding: '14px 18px',
          fontSize: 13,
          color: result.success ? 'var(--accent-green)' : 'var(--accent-red)',
        }}>
          {result.success
            ? `Trade placed successfully! Order #${result.ticket}`
            : `Could not place trade: ${result.retcode_desc}${retryCountdown ? ` Retry in ${retryCountdown}.` : ''}`
          }
        </div>
      ) : !showAmountInput ? (
      <div className="flex gap-3">
        <button
          className="btn btn-success"
          onClick={() => {
              if (executionBlocked) return;
              setShowAmountInput(true);
              if (suggestedAmount && !amount) {
                void handleAmountChange(String(suggestedAmount));
              }
            }}
            disabled={executionBlocked}
            title={executionBlockedReason || undefined}
            style={{ fontSize: 15, padding: '12px 28px', fontWeight: 600 }}
          >
            <Play size={16} />
            {actionWord} Now
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => setDismissed(true)}
            style={{ fontSize: 13, padding: '12px 20px' }}
          >
            Skip
          </button>
          {executionBlockedReason && (
            <span style={{ alignSelf: 'center', fontSize: 11, color: 'var(--accent-yellow)' }}>
              {executionBlockedReason}
            </span>
          )}
        </div>
      ) : (
        <div style={{
          background: 'var(--bg-tertiary)',
          borderRadius: 10,
          padding: '16px 20px',
          border: `1px solid ${borderColor}`,
        }}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>
            How much do you want to invest? ({currency})
          </div>
          {suggestedAmount && (
            <div style={{ fontSize: 12, color: 'var(--accent-blue)', marginBottom: 10 }}>
              AI suggested amount: <span style={{ fontWeight: 700 }}>${suggestedAmount}</span>
              {suggestedPct ? ` (${suggestedPct.toFixed(2)}% of free margin)` : ''}
            </div>
          )}

          {/* Preset amounts */}
          <div className="flex gap-2" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            {presetAmounts.map((preset) => (
              <button
                key={preset}
                className={`btn ${amount === String(preset) ? 'btn-success' : 'btn-secondary'}`}
                onClick={() => handleAmountChange(String(preset))}
                style={{ fontSize: 13, padding: '8px 16px', fontWeight: 600 }}
              >
                ${preset}
              </button>
            ))}
          </div>

          {/* Custom amount */}
          <div className="flex items-center gap-3" style={{ marginBottom: 12 }}>
            <div style={{ position: 'relative', flex: 1 }}>
              <span style={{
                position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                color: 'var(--text-muted)', fontSize: 15, fontWeight: 600,
              }}>$</span>
              <input
                className="form-input"
                type="number"
                placeholder="Enter amount"
                value={amount}
                onChange={(e) => handleAmountChange(e.target.value)}
                style={{
                  fontSize: 18, padding: '12px 12px 12px 28px', fontWeight: 600,
                  fontFamily: 'monospace', textAlign: 'right',
                }}
                min="1"
                autoFocus
              />
            </div>
          </div>

          {/* Volume preview */}
          {volumePreview && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12 }}>
              This will trade <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{volumePreview.volume} lots</span>
              {' '}(~${volumePreview.actual_cost.toLocaleString()} market value)
            </div>
          )}

          {/* Stop Loss & Take Profit - dollar based */}
          {volumePreview && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--accent-red)', fontWeight: 600, display: 'block', marginBottom: 4 }}>
                Max Loss (auto-sell if you lose this much)
              </label>
              <div style={{ position: 'relative' }}>
                <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--accent-red)', fontSize: 14, fontWeight: 600 }}>$</span>
                <input
                  className="form-input"
                  type="number"
                  value={slDollar}
                  onChange={(e) => setSlDollar(e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 14, padding: '8px 12px 8px 24px', fontFamily: 'monospace', borderColor: parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? 'rgba(239,68,68,0.8)' : 'rgba(239,68,68,0.3)' }}
                  step="1" min={Math.ceil(minSlTp)}
                />
              </div>
              {parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp && (
                <div style={{ fontSize: 10, color: '#ef4444', marginTop: 3, fontWeight: 600 }}>
                  ⚠ Minimum ${Math.ceil(minSlTp)} required (broker limit)
                </div>
              )}
              {slDollar && parseFloat(slDollar) >= minSlTp && parseFloat(amount) > 0 && (
                <div style={{ fontSize: 10, color: 'var(--accent-red)', marginTop: 3 }}>
                  Your ${amount} drops to <span style={{ fontWeight: 700 }}>${(parseFloat(amount) - parseFloat(slDollar)).toFixed(0)}</span>
                  {' '}({((parseFloat(slDollar) / parseFloat(amount)) * 100).toFixed(0)}% loss)
                  {dollarToPrice(parseFloat(slDollar), true) > 0 && (
                    <span style={{ opacity: 0.7 }}> · price: {dollarToPrice(parseFloat(slDollar), true).toFixed(digits)}</span>
                  )}
                </div>
              )}
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--accent-green)', fontWeight: 600, display: 'block', marginBottom: 4 }}>
                Target Profit (auto-sell when you gain this much)
              </label>
              <div style={{ position: 'relative' }}>
                <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--accent-green)', fontSize: 14, fontWeight: 600 }}>$</span>
                <input
                  className="form-input"
                  type="number"
                  value={tpDollar}
                  onChange={(e) => setTpDollar(e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 14, padding: '8px 12px 8px 24px', fontFamily: 'monospace', borderColor: parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? 'rgba(34,197,94,0.8)' : 'rgba(34,197,94,0.3)' }}
                  step="1" min={Math.ceil(minSlTp)}
                />
              </div>
              {parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp && (
                <div style={{ fontSize: 10, color: '#ef4444', marginTop: 3, fontWeight: 600 }}>
                  ⚠ Minimum ${Math.ceil(minSlTp)} required (broker limit)
                </div>
              )}
              {tpDollar && parseFloat(tpDollar) >= minSlTp && parseFloat(amount) > 0 && (
                <div style={{ fontSize: 10, color: 'var(--accent-green)', marginTop: 3 }}>
                  Your ${amount} grows to <span style={{ fontWeight: 700 }}>${(parseFloat(amount) + parseFloat(tpDollar)).toFixed(0)}</span>
                  {' '}({((parseFloat(tpDollar) / parseFloat(amount)) * 100).toFixed(0)}% gain)
                  {dollarToPrice(parseFloat(tpDollar), false) > 0 && (
                    <span style={{ opacity: 0.7 }}> · price: {dollarToPrice(parseFloat(tpDollar), false).toFixed(digits)}</span>
                  )}
                </div>
              )}
            </div>
          </div>
          )}

          {/* Execute + Cancel */}
          <div className="flex gap-2">
            <button
              className="btn btn-success"
              onClick={handleExecute}
              disabled={executionBlocked || loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
              title={executionBlockedReason || undefined}
              style={{ fontSize: 14, padding: '10px 24px', fontWeight: 600 }}
            >
              {loading ? <span className="loading-spinner" /> : <Play size={14} />}
              Confirm Buy {amount ? `$${amount}` : ''}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => { setShowAmountInput(false); setAmount(''); setVolumePreview(null); setSlDollar(''); setTpDollar(''); }}
              style={{ fontSize: 13, padding: '10px 16px' }}
            >
              Cancel
            </button>
          </div>
          {executionBlockedReason && (
            <div className="text-muted" style={{ fontSize: 11, marginTop: 8, color: 'var(--accent-yellow)' }}>
              {executionBlockedReason}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function positionLiveClosePrice(position: PositionInfo, liveTick?: TickData): number {
  const isBuy = position.type === 'BUY';
  const brokerPositionPrice = Number(position.price_current || 0);
  const liveCloseSide = Number((isBuy ? liveTick?.bid : liveTick?.ask) || 0);
  return brokerPositionPrice > 0 ? brokerPositionPrice : liveCloseSide;
}

function positionDisplayProfit(position: PositionInfo, liveTick?: TickData, contractSize?: number | null): number {
  const brokerProfit = Number(position.profit);
  if (Number.isFinite(brokerProfit)) {
    return brokerProfit;
  }
  const isBuy = position.type === 'BUY';
  const liveCloseSide = Number((isBuy ? liveTick?.bid : liveTick?.ask) || 0);
  const unitsFromEntryValue = (
    position.entry_market_value_usd
    && position.price_open > 0
    && Number.isFinite(position.entry_market_value_usd)
  )
    ? Number(position.entry_market_value_usd) / position.price_open
    : 0;
  const units = (
    contractSize && Number.isFinite(contractSize) && contractSize > 0
      ? position.volume * contractSize
      : unitsFromEntryValue
  );
  const liveProfitEstimate = (
    liveCloseSide > 0
    && position.price_open > 0
    && units > 0
  )
    ? (
      isBuy
        ? (liveCloseSide - position.price_open) * units
        : (position.price_open - liveCloseSide) * units
    )
    : null;
  return Number.isFinite(liveProfitEstimate as number)
    ? Number(liveProfitEstimate)
    : (Number.isFinite(position.profit) ? position.profit : 0);
}

// Beginner-friendly position card
function PositionCard({
  position,
  currency,
  onClose,
  onCancelClose,
  onModify,
  leverage = 100,
  liveTick,
}: {
  position: PositionInfo;
  currency: string;
  onClose: (ticket: number) => void;
  onCancelClose: (ticket: number) => void;
  onModify: (position: PositionInfo) => void;
  leverage?: number;
  liveTick?: TickData;
}) {
  const [closing, setClosing] = useState(false);
  const [contractSize, setContractSize] = useState<number | null>(null);
  const [tickValue, setTickValue] = useState<number | null>(null);
  const [tickSize, setTickSize] = useState<number | null>(null);
  const [pointSize, setPointSize] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const symbolName = getSymbolName(position.symbol);
  const emoji = getSymbolEmoji(position.symbol);
  const isBuy = position.type === 'BUY';
  const liveNowPrice = positionLiveClosePrice(position, liveTick);

  const effectiveContractSize = useMemo(() => {
    if (contractSize && Number.isFinite(contractSize) && contractSize > 0) {
      return contractSize;
    }
    return null;
  }, [contractSize]);

  const displayProfit = positionDisplayProfit(position, liveTick, effectiveContractSize);
  const profitColor = displayProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  const profitText = explainProfitLoss(displayProfit, currency);
  const digs = position.price_open < 50 ? 5 : 2;

  // Fetch contract size for dollar SL/TP calculation
  useEffect(() => {
    api.getSymbolInfo(position.symbol).then((info: any) => {
      if (info?.trade_contract_size) setContractSize(info.trade_contract_size);
      if (info?.trade_tick_value) setTickValue(info.trade_tick_value);
      if (info?.trade_tick_size) setTickSize(info.trade_tick_size);
      if (info?.point) setPointSize(info.point);
    }).catch(() => {});
  }, [position.symbol]);

  useEffect(() => {
    setNowMs(Date.now());
    const intervalId = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [position.ticket, position.time]);

  const isDirectionallyValidTarget = (priceLevel: number, kind: 'sl' | 'tp') => {
    if (!(priceLevel > 0)) return false;
    if (isBuy) return kind === 'sl' ? priceLevel < position.price_open : priceLevel > position.price_open;
    return kind === 'sl' ? priceLevel > position.price_open : priceLevel < position.price_open;
  };

  // Calculate dollar SL/TP amounts
  const calcDollarDistance = (priceLevel: number) => {
    if (priceLevel <= 0) return null;
    if (priceLevel === position.stop_loss && position.sl_amount_usd != null && Number.isFinite(position.sl_amount_usd)) {
      return Math.abs(position.sl_amount_usd);
    }
    if (priceLevel === position.take_profit && position.tp_amount_usd != null && Number.isFinite(position.tp_amount_usd)) {
      return Math.abs(position.tp_amount_usd);
    }
    return priceDistanceToUsd({
      symbolInfo: {
        trade_contract_size: effectiveContractSize,
        trade_tick_value: tickValue,
        trade_tick_size: tickSize,
        point: pointSize,
      },
      entryPrice: position.price_open,
      targetPrice: priceLevel,
      volume: position.volume,
    });
  };
  const amountMatch = position.comment?.match(/TA:\$(\d+(?:\.\d+)?)|TA(\d+(?:\.\d+)?)/i);
  const commentAmount = amountMatch ? (amountMatch[1] || amountMatch[2]) : undefined;
  const commentBudget = commentAmount ? parseFloat(commentAmount) : 0;
  const validStopLoss = isDirectionallyValidTarget(position.stop_loss, 'sl') ? position.stop_loss : null;
  const validTakeProfit = isDirectionallyValidTarget(position.take_profit, 'tp') ? position.take_profit : null;
  const slDollars = validStopLoss != null ? calcDollarDistance(validStopLoss) : null;
  const tpDollars = validTakeProfit != null ? calcDollarDistance(validTakeProfit) : null;

  // Started amount should represent the actual filled margin/value of the position.
  // The requested budget from the order comment is only secondary context.
  const startedAmountRaw = positionStartedAmount(position, leverage, effectiveContractSize);
  const startedAmount = startedAmountRaw;
  const marginUsedAmount = positionMarginUsedAmount(position, leverage, effectiveContractSize);
  const requestedBudgetAmount = (
    positiveAmount(position.started_with_usd)
    ?? positiveAmount(commentStartedAmount(position.comment))
    ?? null
  );
  const budgetMarginDelta = Number(position.budget_vs_margin_delta_usd);
  const budgetMarginDeltaPct = Number(position.budget_vs_margin_delta_pct);
  const hasBudgetMarginDelta = Number.isFinite(budgetMarginDelta)
    && Math.abs(budgetMarginDelta) >= 0.01
    && startedAmount > 0
    && marginUsedAmount > 0;
  const amountWithProfit = startedAmount > 0 ? startedAmount + displayProfit : 0;
  const profitLossPct = startedAmount > 0 ? ((displayProfit / startedAmount) * 100) : 0;
  const slPricePct = (validStopLoss != null && position.price_open > 0)
    ? (Math.abs(position.price_open - validStopLoss) / position.price_open) * 100
    : null;
  const tpPricePct = (validTakeProfit != null && position.price_open > 0)
    ? (Math.abs(validTakeProfit - position.price_open) / position.price_open) * 100
    : null;
  const slTargetPctOfStart = (slDollars != null && startedAmount > 0)
    ? (Math.abs(slDollars) / startedAmount) * 100
    : slPricePct;
  const tpTargetPctOfStart = (tpDollars != null && startedAmount > 0)
    ? (Math.abs(tpDollars) / startedAmount) * 100
    : tpPricePct;
  // Display SL/TP money impact against "Started Amount" (margin/budget used),
  // not notional contract P/L, to keep target-started-amount values logical.
  const slStartedDelta = (slDollars != null && startedAmount > 0)
    ? Math.max(0, Math.abs(slDollars))
    : null;
  const tpStartedDelta = (tpDollars != null && startedAmount > 0)
    ? Math.max(0, Math.abs(tpDollars))
    : (tpDollars != null && startedAmount > 0 ? Math.max(0, tpDollars) : null);
  const slStartedAmountAtTarget = (slStartedDelta != null && startedAmount > 0)
    ? startedAmount - slStartedDelta
    : null;
  const tpStartedAmountAtTarget = (tpStartedDelta != null && startedAmount > 0)
    ? Math.max(0, startedAmount + tpStartedDelta)
    : null;
  const primaryAmountLabel = 'Started Amount';
  const middleAmountLabel = 'Started Amount +/- P/L';
  const returnAmountLabel = 'Profit / Loss %';
  const slTargetPct = slTargetPctOfStart;
  const tpTargetPct = tpTargetPctOfStart;
  const slTriggerSide = isBuy ? 'Bid' : 'Ask';
  const slDistancePctFromTrigger = (
    validStopLoss != null
    && position.price_open > 0
    && liveNowPrice > 0
  )
    ? (
      isBuy
        ? ((liveNowPrice - validStopLoss) / position.price_open) * 100
        : ((validStopLoss - liveNowPrice) / position.price_open) * 100
    )
    : null;
  const slIsCrossedByDisplayedPrice = slDistancePctFromTrigger != null && slDistancePctFromTrigger <= 0;
  const executionTrace = position.execution_trace || null;
  const strategyName = String(executionTrace?.strategy?.name || '').trim();
  const executionReason = String(
    executionTrace?.reason
    || position.signal_reason
    || position.risk_reason
    || ''
  ).trim();
  const executionSource = String(executionTrace?.source || '').trim();
  const executionMode = String(executionTrace?.policy_mode || '').trim();
  const geminiModel = String(executionTrace?.gemini?.active_model || '').trim();
  const metaVersion = String(executionTrace?.meta_model?.version_id || '').trim();
  const signalSnapshot = executionTrace?.signal_snapshot || null;
  const signalConfidence = Number(signalSnapshot?.confidence || 0);
  const signalReason = String(signalSnapshot?.reason || '').trim();
  const preflightReason = String(executionTrace?.preflight_reason || '').trim();
  const activeAgentName = String(executionTrace?.active_agent_name || executionTrace?.requested_agent_name || '').trim();
  const strategyLines = Array.isArray(executionTrace?.strategy?.summary_lines)
    ? executionTrace.strategy.summary_lines.map((line) => String(line || '').trim()).filter(Boolean)
    : [];
  const whyDetails = [
    signalReason ? `Signal: ${signalReason}` : '',
    executionReason && executionReason !== signalReason ? `Execution: ${executionReason}` : '',
    preflightReason && preflightReason !== executionReason ? `Risk/preflight: ${preflightReason}` : '',
    strategyLines.length > 0 ? `Strategy: ${strategyLines.slice(0, 2).join(' ')}` : '',
  ].filter(Boolean);

  const handleClose = async () => {
    setClosing(true);
    try {
      await onClose(position.ticket);
    } finally {
      setClosing(false);
    }
  };

  const timeStr = formatElapsedOpenTime(position.time, nowMs);
  const expectedProfitMinutes = Number(position.expected_profit_time_minutes || 0);
  const expectedProfitRemaining = Number(position.expected_profit_time_remaining_minutes || 0);
  const expectedProfitEtaText = formatEtaTime(position.expected_profit_eta_timestamp);
  const hasExpectedProfitTiming = Number.isFinite(expectedProfitMinutes) && expectedProfitMinutes > 0;
  const hasExpectedProfitRemaining = Number.isFinite(expectedProfitRemaining) && expectedProfitRemaining > 0;

  return (
    <div className="card" style={{
      borderLeft: `3px solid ${profitColor}`,
      padding: '16px 20px',
    }}>
      {/* Header row */}
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 20 }}>{emoji}</span>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{symbolName}</div>
            <div className="flex items-center gap-2" style={{ fontSize: 12, marginTop: 2 }}>
              <span className={`badge ${isBuy ? 'badge-green' : 'badge-red'}`} style={{ fontSize: 10 }}>
                {position.type}
              </span>
              <span className="text-muted">Opened {timeStr}</span>
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: profitColor, fontFamily: 'monospace' }}>
            {displayProfit >= 0 ? '+' : ''}{displayProfit.toFixed(2)} {currency}
          </div>
          <div className="text-muted" style={{ fontSize: 11 }}>{profitText}</div>
        </div>
      </div>

      {/* Investment summary - big numbers */}
        {startedAmount > 0 && (
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 10,
            background: 'var(--bg-tertiary)', borderRadius: 8, padding: '10px 14px',
          }}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>{primaryAmountLabel}</div>
              <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace' }}>${startedAmount.toFixed(2)}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>{middleAmountLabel}</div>
              <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
                ${amountWithProfit.toFixed(2)}
              </div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>{returnAmountLabel}</div>
              <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
                {displayProfit >= 0 ? '+' : ''}{profitLossPct.toFixed(1)}%
              </div>
            </div>
          </div>
        )}

      {/* Trade details row */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 10, fontSize: 11, color: 'var(--text-muted)' }}>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Volume:</span>{' '}
          <span className="font-mono">{position.volume} lots</span>
        </span>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Entry:</span>{' '}
          <span className="font-mono">{position.price_open.toFixed(digs)}</span>
        </span>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Now:</span>{' '}
          <span className="font-mono">{liveNowPrice.toFixed(digs)}</span>
        </span>
        {requestedBudgetAmount != null && hasBudgetMarginDelta && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Requested budget:</span>{' '}
            <span className="font-mono">${requestedBudgetAmount.toFixed(2)}</span>
            <span className="text-muted" style={{ fontSize: 10, marginLeft: 4 }}>
              {budgetMarginDelta > 0
                ? `($${budgetMarginDelta.toFixed(2)} not filled`
                : `($${Math.abs(budgetMarginDelta).toFixed(2)} over fill`}
              {Number.isFinite(budgetMarginDeltaPct) ? `, ${Math.abs(budgetMarginDeltaPct).toFixed(2)}%` : ''}
              {`)`}
            </span>
          </span>
        )}
        {hasExpectedProfitTiming && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-blue)' }}>Expected TP:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-blue)' }}>
              ~{formatMinutesCompact(expectedProfitMinutes)}
            </span>
            {hasExpectedProfitRemaining && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` (${formatMinutesCompact(expectedProfitRemaining)} left)`}
              </span>
            )}
            {expectedProfitEtaText && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` ETA ${expectedProfitEtaText}`}
              </span>
            )}
          </span>
        )}
        {position.stop_loss > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-red)' }}>SL:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-red)' }}>
              {validStopLoss != null ? validStopLoss.toFixed(digs) : 'Invalid target'}
            </span>
            {(slStartedDelta != null || slDollars != null) && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` (-$${(slStartedDelta ?? slDollars ?? 0).toFixed(2)}`}
                {slTargetPct != null ? `, -${slTargetPct.toFixed(1)}%` : ''}
                {slStartedAmountAtTarget != null ? `, target started amount $${slStartedAmountAtTarget.toFixed(2)}` : ''}
                {`)`}
              </span>
            )}
            {slDistancePctFromTrigger != null && (
              <span className="text-muted" style={{ fontSize: 10, marginLeft: 6 }}>
                {slIsCrossedByDisplayedPrice
                  ? `(${slTriggerSide} crossed SL by ${Math.abs(slDistancePctFromTrigger).toFixed(2)}%)`
                  : `(${slTriggerSide} is ${slDistancePctFromTrigger.toFixed(2)}% away from SL)`}
              </span>
            )}
          </span>
        )}
        {position.take_profit > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-green)' }}>TP:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-green)' }}>
              {validTakeProfit != null ? validTakeProfit.toFixed(digs) : 'Invalid target'}
            </span>
            {(tpStartedDelta != null || tpDollars != null) && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` (+$${(tpStartedDelta ?? tpDollars ?? 0).toFixed(2)}`}
                {tpTargetPct != null ? `, +${tpTargetPct.toFixed(1)}%` : ''}
                {tpStartedAmountAtTarget != null ? `, target started amount $${tpStartedAmountAtTarget.toFixed(2)}` : ''}
                {`)`}
              </span>
            )}
          </span>
        )}
      </div>

      {(whyDetails.length > 0 || strategyName || executionSource || executionMode || activeAgentName || geminiModel || metaVersion) && (
        <div style={{
          marginTop: 10,
          padding: '10px 12px',
          borderRadius: 8,
          background: 'rgba(59,130,246,0.08)',
          border: '1px solid rgba(59,130,246,0.18)',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          fontSize: 11,
          color: 'var(--text-muted)',
        }}>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {activeAgentName && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Agent:</span> {activeAgentName}</span>}
            {strategyName && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Strategy:</span> {strategyName}</span>}
            {executionSource && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Source:</span> {executionSource}</span>}
            {executionMode && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Mode:</span> {executionMode}</span>}
            {signalConfidence > 0 && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Confidence:</span> {Math.round(signalConfidence * 100)}%</span>}
          </div>
          {whyDetails.length > 0 && (
            <div>
              <span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Why this trade happened:</span>{' '}
              <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 3 }}>
                {whyDetails.map((detail, index) => (
                  <span key={`${position.ticket}-why-${index}`}>{detail}</span>
                ))}
              </div>
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {geminiModel && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Gemini:</span> {geminiModel}</span>}
            {metaVersion && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Meta-model:</span> {metaVersion}</span>}
            {signalSnapshot?.stop_loss && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Planned SL:</span> {Number(signalSnapshot.stop_loss).toFixed(digs)}</span>}
            {signalSnapshot?.take_profit && <span><span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Planned TP:</span> {Number(signalSnapshot.take_profit).toFixed(digs)}</span>}
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex justify-between items-center mt-3">
        <div className="flex gap-3" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {position.stop_loss > 0 && (
            <span style={{ color: validStopLoss != null ? 'var(--accent-green)' : 'var(--accent-yellow)' }}>
              Stop-loss {slStartedAmountAtTarget != null ? `(target started amount $${slStartedAmountAtTarget.toFixed(0)})` : slStartedDelta != null ? `(-$${slStartedDelta.toFixed(0)})` : slDollars != null ? `(-$${slDollars.toFixed(0)})` : validStopLoss != null ? 'active' : 'needs review'}
            </span>
          )}
          {position.take_profit > 0 && (
            <span style={{ color: validTakeProfit != null ? 'var(--accent-green)' : 'var(--accent-yellow)' }}>
              Take-profit {tpStartedAmountAtTarget != null ? `(target started amount $${tpStartedAmountAtTarget.toFixed(0)})` : tpStartedDelta != null ? `(+$${tpStartedDelta.toFixed(0)})` : tpDollars != null ? `(+$${tpDollars.toFixed(0)})` : validTakeProfit != null ? 'set' : 'needs review'}
            </span>
          )}
          {position.stop_loss === 0 && position.take_profit === 0 && <span>No SL/TP set</span>}
        </div>
        <div className="flex gap-2">
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => onModify(position)}
            disabled={closing}
            style={{ fontSize: 12 }}
          >
            <Pencil size={12} />
            Edit Targets
          </button>
          <button
            className={`btn ${displayProfit >= 0 ? 'btn-success' : 'btn-warning'} btn-sm`}
            onClick={() => position.close_pending ? onCancelClose(position.ticket) : handleClose()}
            disabled={closing}
            style={{ fontSize: 12 }}
          >
            {closing ? <span className="loading-spinner" /> : <DollarSign size={12} />}
            {position.close_pending
              ? `Cancel Close (${position.close_order_status || 'Pending'})`
              : displayProfit >= 0 ? 'Sell & Take Profit' : 'Sell & Cut Loss'}
          </button>
        </div>
      </div>
    </div>
  );
}

// Compact market row with Buy button for any symbol
function MarketRow({
  rec,
  currency,
  onExecuted,
  isLast,
  liveTick,
  executionBlockedReason = '',
}: {
  rec: Recommendation;
  currency: string;
  onExecuted: () => Promise<void> | void;
  isLast: boolean;
  liveTick?: TickData;
  executionBlockedReason?: string;
}) {
  const CHART_ZOOM_LEVELS = [
    { label: '1Y', timeframe: 'D1', count: 365, refreshMs: 7000 },
    { label: '1M', timeframe: 'H4', count: 180, refreshMs: 5000 },
    { label: '7D', timeframe: 'H1', count: 168, refreshMs: 3500 },
    { label: '24H', timeframe: 'M15', count: 96, refreshMs: 2500 },
    { label: '4H', timeframe: 'M5', count: 48, refreshMs: 1700 },
    { label: '1H', timeframe: 'M1', count: 60, refreshMs: 1200 },
  ] as const;
  const [expanded, setExpanded] = useState(false);
  const [amount, setAmount] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [retryAvailableAtMs, setRetryAvailableAtMs] = useState<number | null>(null);
  const [retryNowMs, setRetryNowMs] = useState(() => Date.now());
  const [volumeInfo, setVolumeInfo] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number; trade_tick_value?: number; trade_tick_size?: number; point?: number } | null>(null);
  // Manual action override: user can choose BUY or SELL regardless of AI signal
  const [manualAction, setManualAction] = useState<'BUY' | 'SELL'>(rec.signal.action === 'SELL' ? 'SELL' : 'BUY');

  // SL/TP as dollar amounts
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');
  const [chartBars, setChartBars] = useState<BarData[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartError, setChartError] = useState('');
  const [chartZoomIndex, setChartZoomIndex] = useState(3);
  const safeZoomIndex = Math.max(0, Math.min(chartZoomIndex, CHART_ZOOM_LEVELS.length - 1));
  const chartZoom = CHART_ZOOM_LEVELS[safeZoomIndex];
  const chartTimeframe = chartZoom.timeframe;
  const chartBarCount = chartZoom.count;
  const chartRefreshMs = chartZoom.refreshMs;
  const chartZoomLabel = chartZoom.label;

  const price = rec.entry_price_estimate || 0;
  const digits = price < 50 ? 5 : 2;
  const minSlTp = volumeInfo?.min_sl_tp_dollars || 0;

  const liveBid = Number(liveTick?.bid || 0);
  const liveAsk = Number(liveTick?.ask || 0);
  const hasLiveBidAsk = liveBid > 0 && liveAsk > 0;
  const referencePrice = Number(rec.entry_price_estimate || volumeInfo?.price || 0);
  const bid = hasLiveBidAsk ? liveBid : null;
  const ask = hasLiveBidAsk ? liveAsk : null;

  // Reset state when symbol changes (React may reuse component for different rec)
  const prevSymbol = useRef(rec.symbol);
  useEffect(() => {
    if (prevSymbol.current !== rec.symbol) {
      prevSymbol.current = rec.symbol;
      setExpanded(false);
      setSlDollar('');
      setTpDollar('');
      setAmount('');
      setResult(null);
      setRetryAvailableAtMs(null);
      setVolumeInfo(null);
    }
  }, [rec.symbol]);

  useEffect(() => {
    if (!retryAvailableAtMs || !result || result.success) return undefined;
    setRetryNowMs(Date.now());
    const interval = window.setInterval(() => {
      const now = Date.now();
      setRetryNowMs(now);
      if (now >= retryAvailableAtMs) {
        setResult(null);
        setRetryAvailableAtMs(null);
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [retryAvailableAtMs, result]);

  const name = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);
  const confidenceScore = recommendationConfidence(rec);
  const conf = Math.round(confidenceScore * 100);
  const strength = getSignalStrength(confidenceScore);
  const qualityScore = Math.round(recommendationQuality(rec) * 100);
  const unavailableScore = hasUnavailableScore(rec);
  const showScoreAsUnavailable = unavailableScore && conf <= 0 && qualityScore <= 0;
  const confidenceLabel = showScoreAsUnavailable ? '--' : `${conf}%`;
  const qualityLabel = showScoreAsUnavailable ? 'Q --' : `Q ${qualityScore}%`;
  const qualityThreshold = Math.round(recommendationQualityThreshold(rec) * 100);
  const closedMarket = isClosedMarketRecommendation(rec);
  const volumeMetaScore = Number((rec.signal as any)?.metadata?.volume_score ?? 0);
  const volumeMetaActive = Number.isFinite(volumeMetaScore) ? Math.max(0, Math.min(1, volumeMetaScore)) : 0;
  const volumeMetaPct = Math.round(volumeMetaActive * 100);
  const suggestedAmount = rec.ready_to_execute && rec.recommended_amount_usd && rec.recommended_amount_usd > 0
    ? Math.round(rec.recommended_amount_usd)
    : null;
  const suggestedPct = rec.ready_to_execute && rec.recommended_amount_pct_free_margin && rec.recommended_amount_pct_free_margin > 0
    ? rec.recommended_amount_pct_free_margin
    : null;
  const holdReasons = compactReasons(
    [
      ...(rec.trade_quality?.no_trade_reasons || []),
      ...(rec.portfolio_risk?.blocking_reasons || []),
      ...(rec.anti_churn?.reasons || []),
    ],
    rec.execution_reason,
  );
  const news = rec.news || {
    sentiment: 'neutral',
    impact: 'low',
    relevance: 'indirect',
    strength: 0,
    risk_flag: false,
    message: 'No major news',
    news_unavailable: true,
  };
  const newsSentiment = String(news.sentiment || 'neutral').toLowerCase();
  const newsImpact = String(news.impact || 'low').toLowerCase();
  const newsStrengthPct = Math.round(Math.max(0, Math.min(1, Number(news.strength || 0))) * 100);
  const newsBadgeColor =
    newsSentiment === 'bullish'
      ? 'var(--accent-green)'
      : newsSentiment === 'bearish'
        ? 'var(--accent-red)'
        : 'var(--text-muted)';
  const newsImpactColor =
    newsImpact === 'high'
      ? 'var(--accent-red)'
      : newsImpact === 'medium'
        ? 'var(--accent-yellow)'
        : 'var(--accent-green)';
  const retryCountdown = result && !result.success && retryAvailableAtMs
    ? formatRetryCountdown(retryAvailableAtMs, retryNowMs)
    : null;
  const executionBlocked = Boolean(executionBlockedReason);
  const chartPoints = useMemo(
    () => chartBars.map((bar) => ({
      t: bar.time,
      label: chartTimeframe === 'D1' || chartTimeframe === 'H4'
        ? new Date(bar.time * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })
        : new Date(bar.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      close: Number(bar.close || 0),
      open: Number(bar.open || 0),
      volume: Number(bar.volume || 0),
      isUp: Number(bar.close || 0) >= Number(bar.open || 0),
    })),
    [chartBars, chartTimeframe],
  );
  const latestChartPoint = chartPoints.length > 0 ? chartPoints[chartPoints.length - 1] : null;
  const lastMarketVolume = latestChartPoint ? Math.max(0, latestChartPoint.volume) : 0;
  const avgMarketVolume = useMemo(() => {
    if (!chartPoints.length) return 0;
    const sample = chartPoints.slice(-20);
    const total = sample.reduce((sum, point) => sum + Math.max(0, point.volume), 0);
    return sample.length > 0 ? total / sample.length : 0;
  }, [chartPoints]);
  const volumePulse = avgMarketVolume > 0 ? (lastMarketVolume / avgMarketVolume) : 0;

  const refreshMarketPanel = useCallback(async () => {
    if (!expanded) return;
    try {
      if (chartBars.length === 0) {
        setChartLoading(true);
      }
      const bars = await api.getBars(rec.symbol, chartTimeframe, chartBarCount);
      setChartBars(Array.isArray(bars) ? bars : []);
      setChartError('');
    } catch (err: any) {
      setChartError(formatUiError(err));
    } finally {
      setChartLoading(false);
    }
  }, [expanded, chartBars.length, rec.symbol, chartTimeframe, chartBarCount]);

  useEffect(() => {
    if (!expanded) {
      setChartLoading(false);
      setChartError('');
      setChartBars([]);
      return undefined;
    }
    let stopped = false;
    const run = async () => {
      if (stopped) return;
      await refreshMarketPanel();
    };
    void run();
    const interval = window.setInterval(() => {
      void run();
    }, chartRefreshMs);
    return () => {
      stopped = true;
      window.clearInterval(interval);
    };
  }, [expanded, refreshMarketPanel, chartTimeframe, chartRefreshMs]);
  const isBuyAction = manualAction === 'BUY';
  const computeAutoManualTargets = (
    amountUsd: number,
    info: { volume: number; contract_size: number; price: number; min_sl_tp_dollars: number; trade_tick_value?: number; trade_tick_size?: number; point?: number },
  ) => {
    const usdPerUnit = usdPerPriceDistance({
      symbolInfo: {
        trade_contract_size: info.contract_size,
        trade_tick_value: info.trade_tick_value,
        trade_tick_size: info.trade_tick_size,
        point: info.point,
      },
      volume: Number(info.volume || 0),
    });
    const livePrice = isBuyAction ? (ask || info.price || rec.entry_price_estimate || 0) : (bid || info.price || rec.entry_price_estimate || 0);
    if (!(amountUsd > 0) || !(usdPerUnit > 0) || !(livePrice > 0)) {
      return { slUsd: 0, tpUsd: 0 };
    }

    const referenceEntry = rec.entry_price_estimate > 0 ? rec.entry_price_estimate : livePrice;
    let slPct = 0;
    let tpPct = 0;
    if ((rec.signal.stop_loss || 0) > 0 && (rec.signal.take_profit || 0) > 0 && referenceEntry > 0) {
      slPct = Math.abs(referenceEntry - Number(rec.signal.stop_loss || 0)) / referenceEntry;
      tpPct = Math.abs(Number(rec.signal.take_profit || 0) - referenceEntry) / referenceEntry;
    }

    if (!(slPct > 0)) slPct = 0.012;
    if (!(tpPct > 0)) tpPct = Math.max(0.02, slPct * 1.8);
    slPct = Math.min(Math.max(slPct, 0.004), 0.06);
    tpPct = Math.min(Math.max(tpPct, Math.max(slPct * 1.4, 0.008)), 0.12);

    const slPrice = isBuyAction ? livePrice * (1 - slPct) : livePrice * (1 + slPct);
    const tpPrice = isBuyAction ? livePrice * (1 + tpPct) : livePrice * (1 - tpPct);
    const slRaw = Math.abs(livePrice - slPrice) * usdPerUnit;
    const tpRaw = Math.abs(tpPrice - livePrice) * usdPerUnit;
    const minDollar = Math.max(1, Math.ceil(Number(info.min_sl_tp_dollars || 0)));
    const slUsd = Math.max(minDollar, Math.round(slRaw));
    const tpUsd = Math.max(minDollar, Math.round(tpRaw));
    return { slUsd, tpUsd };
  };

  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumeInfo || volumeInfo.volume <= 0 || volumeInfo.contract_size <= 0) return 0;
    const livePrice = isBuyAction ? (ask || volumeInfo.price || rec.entry_price_estimate || 0) : (bid || volumeInfo.price || rec.entry_price_estimate || 0);
    return dollarTargetToPrice({
      action: manualAction,
      dollarAmount,
      volume: volumeInfo.volume,
      contractSize: volumeInfo.contract_size,
      symbolInfo: {
        trade_contract_size: volumeInfo.contract_size,
        trade_tick_value: volumeInfo.trade_tick_value,
        trade_tick_size: volumeInfo.trade_tick_size,
        point: volumeInfo.point,
      },
      referencePrice: livePrice,
      isStopLoss: isSL,
    }) || 0;
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumeInfo(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const info = await api.calculateVolume(rec.symbol, num);
        setVolumeInfo(info);
        const autoTargets = computeAutoManualTargets(num, info);
        setSlDollar(autoTargets.slUsd > 0 ? String(autoTargets.slUsd) : '');
        setTpDollar(autoTargets.tpUsd > 0 ? String(autoTargets.tpUsd) : '');
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

  useEffect(() => {
    if (!amount || parseFloat(amount) <= 0) return;
    void handleAmountChange(amount);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manualAction]);

  const handleTrade = async () => {
    const num = parseFloat(amount);
    if (!num || num <= 0) return;
    // Convert dollar SL/TP to price levels
    const slAmt = parseFloat(slDollar) || 0;
    const tpAmt = parseFloat(tpDollar) || 0;
    const slNum = slAmt > 0 ? dollarToPrice(slAmt, true) : undefined;
    const tpNum = tpAmt > 0 ? dollarToPrice(tpAmt, false) : undefined;
    setLoading(true);
    try {
      const actionMatchesRecommendation = manualAction === rec.signal.action;
      const res = await api.quickBuy(rec.symbol, num, slNum, tpNum, manualAction, {
        signal_id: actionMatchesRecommendation ? (rec.signal_id ?? null) : null,
        signal_confidence: actionMatchesRecommendation ? recommendationConfidence(rec) : null,
        quality_score: actionMatchesRecommendation ? recommendationQuality(rec) : null,
        signal_reason: actionMatchesRecommendation ? (rec.signal.reason || rec.explanation || null) : null,
        signal_agent_name: actionMatchesRecommendation ? ((rec.signal as any)?.agent_name || null) : null,
      });
      setResult(res);
      setRetryAvailableAtMs(res.success ? null : Date.now() + DECLINED_TRADE_RETRY_SECONDS * 1000);
      if (res.success) void onExecuted();
    } catch (e: any) {
      setResult({ success: false, retcode: -1, retcode_desc: formatUiError(e), ticket: null, volume: null, price: null, stop_loss: null, take_profit: null, comment: '' });
      setRetryAvailableAtMs(Date.now() + DECLINED_TRADE_RETRY_SECONDS * 1000);
    } finally {
      setLoading(false);
    }
  };

  const handleExpand = () => {
    if (!result) {
      const next = !expanded;
      setExpanded(next);
      if (next && suggestedAmount && !amount) {
        void handleAmountChange(String(suggestedAmount));
      }
    }
  };

  return (
    <div style={{ borderBottom: isLast ? 'none' : '1px solid var(--border)' }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 16px', cursor: 'pointer',
      }} onClick={handleExpand}>
        <div className="flex items-center gap-3" style={{ flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: 16 }}>{emoji}</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{name}</div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{rec.symbol}</div>
          </div>
        </div>

        {/* Live Bid / Ask */}
        <div style={{ minWidth: 130, textAlign: 'center', flexShrink: 0, marginRight: 8 }}>
          {bid !== null && ask !== null && bid > 0 && ask > 0 ? (
            <div style={{ display: 'flex', gap: 8, fontSize: 11, fontFamily: 'monospace' }}>
              <div>
                <span style={{ fontSize: 9, color: 'var(--accent-red)', fontWeight: 600, display: 'block' }}>SELL</span>
                <span style={{ color: 'var(--text-secondary)' }}>{bid.toFixed(digits)}</span>
              </div>
              <div>
                <span style={{ fontSize: 9, color: 'var(--accent-green)', fontWeight: 600, display: 'block' }}>BUY</span>
                <span style={{ color: 'var(--text-secondary)' }}>{ask.toFixed(digits)}</span>
              </div>
            </div>
          ) : referencePrice > 0 ? (
            <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
              <span style={{ fontSize: 9, color: 'var(--text-muted)', fontWeight: 600, display: 'block' }}>REF</span>
              <span style={{ color: 'var(--text-secondary)' }}>{referencePrice.toFixed(digits)}</span>
            </div>
          ) : (
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>--</span>
          )}
        </div>

        <div className="flex items-center gap-2" style={{ width: 100, flexShrink: 0 }}>
          <div style={{ flex: 1, height: 5, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
            <div
              style={{
                width: `${showScoreAsUnavailable ? 0 : Math.max(4, conf)}%`,
                height: '100%',
                background: showScoreAsUnavailable ? 'var(--text-muted)' : strength.color,
                borderRadius: 3,
              }}
            />
          </div>
          <span style={{ fontSize: 10, color: showScoreAsUnavailable ? 'var(--text-muted)' : strength.color, fontWeight: 600 }}>
            {confidenceLabel}
          </span>
        </div>

        <div
          style={{
            width: 72,
            textAlign: 'right',
            fontSize: 10,
            fontWeight: 700,
            color: closedMarket
              ? 'var(--text-muted)'
              : (qualityScore >= qualityThreshold ? 'var(--accent-green)' : 'var(--accent-red)'),
          }}
        >
          {qualityLabel}
        </div>

        {result ? (
          <span style={{ fontSize: 11, color: result.success ? 'var(--accent-green)' : 'var(--accent-red)', marginLeft: 12 }}>
            {result.success
              ? `${manualAction === 'SELL' ? 'Sold' : 'Bought'}! #${result.ticket}${String(result.retcode_desc || '').includes('Risk warning:') ? ' (risk warning)' : ''}`
              : `${result.retcode_desc}${retryCountdown ? ` Retry in ${retryCountdown}.` : ''}`}
          </span>
        ) : (
          <button
            className={`btn ${manualAction === 'SELL' ? 'btn-danger' : 'btn-success'} btn-sm`}
            onClick={(e) => { e.stopPropagation(); handleExpand(); }}
            style={{ fontSize: 11, padding: '4px 12px', marginLeft: 12, flexShrink: 0 }}
          >
            <DollarSign size={10} /> Trade
          </button>
        )}
      </div>

      {expanded && !result && (
        <div style={{ padding: '0 16px 12px 16px' }}>
          {holdReasons.length > 0 && (
            <div style={{
              background: 'rgba(59, 130, 246, 0.08)',
              border: '1px solid rgba(59, 130, 246, 0.16)',
              borderRadius: 8,
              padding: '10px 12px',
              fontSize: 11,
              color: 'var(--text-secondary)',
              marginBottom: 10,
            }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Why this is filtered</div>
              {holdReasons.map((reason, index) => (
                <div key={index} style={{ marginBottom: index < holdReasons.length - 1 ? 4 : 0 }}>
                  {reason}
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginBottom: 10 }}>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '8px 10px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Trade Quality</div>
              <div style={{ fontWeight: 700 }}>{qualityScore}%</div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Need {qualityThreshold}%</div>
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '8px 10px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Projected Margin</div>
              <div style={{ fontWeight: 700 }}>{formatPct(rec.portfolio_risk?.projected_margin_utilization_pct)}</div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>after fill</div>
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '8px 10px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Gemini</div>
              <div style={{ fontWeight: 700 }}>
                {rec.gemini_confirmation?.degraded ? 'Degraded' : rec.gemini_confirmation?.used ? 'Advisory' : 'Off'}
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{rec.gemini_confirmation?.news_bias || 'technical only'}</div>
            </div>
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 8 }}>
            {formatCommissionLabel(rec, currency)}
          </div>
          <div style={{
            background: 'var(--bg-secondary)',
            borderRadius: 8,
            padding: '8px 10px',
            marginBottom: 10,
            border: news.risk_flag ? '1px solid rgba(239,68,68,0.35)' : '1px solid var(--border)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 4 }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>News Impact</div>
              <div style={{ fontSize: 10, color: newsImpactColor, fontWeight: 700 }}>{String(news.impact || 'low').toUpperCase()}</div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <div style={{ fontWeight: 700, color: newsBadgeColor }}>{String(news.sentiment || 'neutral').toUpperCase()}</div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Strength {newsStrengthPct}%</div>
            </div>
            <div style={{ fontSize: 10, color: news.risk_flag ? 'var(--accent-red)' : 'var(--text-muted)', marginTop: 4 }}>
              {news.message || (news.risk_flag ? 'High-impact news detected' : 'No major news')}
            </div>
          </div>
          <div style={{
            background: 'var(--bg-secondary)',
            borderRadius: 8,
            padding: '8px 10px',
            marginBottom: 10,
            border: '1px solid var(--border)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6, gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Live Market Chart</div>
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      setChartZoomIndex((prev) => Math.max(0, prev - 1));
                    }}
                    disabled={safeZoomIndex <= 0}
                    title="Zoom out"
                    style={{ minWidth: 24, height: 22, padding: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
                  >
                    <Minus size={12} />
                  </button>
                  <div style={{ minWidth: 30, textAlign: 'center', fontWeight: 700, fontSize: 11, color: 'var(--text-primary)' }}>
                    {chartZoomLabel}
                  </div>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      setChartZoomIndex((prev) => Math.min(CHART_ZOOM_LEVELS.length - 1, prev + 1));
                    }}
                    disabled={safeZoomIndex >= CHART_ZOOM_LEVELS.length - 1}
                    title="Zoom in"
                    style={{ minWidth: 24, height: 22, padding: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
                  >
                    <Plus size={12} />
                  </button>
                </div>
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', textAlign: 'right' }}>
                {latestChartPoint ? `Last: ${latestChartPoint.label}` : 'Waiting for bars...'}
              </div>
            </div>
            {chartLoading && chartPoints.length === 0 ? (
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading chart...</div>
            ) : chartPoints.length === 0 ? (
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {chartError || 'No live bar data yet for this market.'}
              </div>
            ) : (
              <div style={{ width: '100%', height: 220 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={chartPoints} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis dataKey="label" stroke="var(--text-muted)" fontSize={10} minTickGap={20} />
                    <YAxis yAxisId="price" orientation="right" stroke="var(--text-muted)" fontSize={10} domain={['auto', 'auto']} width={52} />
                    <YAxis yAxisId="volume" hide domain={[0, 'auto']} />
                    <Tooltip
                      contentStyle={{
                        background: 'var(--bg-tertiary)',
                        border: '1px solid var(--border)',
                        borderRadius: 8,
                        fontSize: 11,
                      }}
                      formatter={(value: any, key: string) => {
                        if (key === 'volume') return [Number(value || 0).toLocaleString(), 'Volume'];
                        return [Number(value || 0).toFixed(digits), 'Price'];
                      }}
                      labelFormatter={(label) => `${chartZoomLabel} ${label}`}
                    />
                    <Bar yAxisId="volume" dataKey="volume" barSize={4} opacity={0.85}>
                      {chartPoints.map((point, index) => (
                        <Cell key={`v-${point.t}-${index}`} fill={point.isUp ? 'rgba(34,197,94,0.55)' : 'rgba(239,68,68,0.55)'} />
                      ))}
                    </Bar>
                    <Area
                      yAxisId="price"
                      type="monotone"
                      dataKey="close"
                      stroke="var(--accent-blue)"
                      strokeWidth={2}
                      fill="rgba(59,130,246,0.28)"
                      dot={false}
                      isAnimationActive={false}
                    />
                    {latestChartPoint && (
                      <ReferenceLine
                        yAxisId="price"
                        y={latestChartPoint.close}
                        stroke="rgba(239,68,68,0.7)"
                        strokeDasharray="4 4"
                        ifOverflow="extendDomain"
                        label={{
                          value: latestChartPoint.close.toFixed(digits),
                          position: 'right',
                          fill: 'var(--accent-red)',
                          fontSize: 10,
                        }}
                      />
                    )}
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 8 }}>
              <div style={{ fontSize: 11 }}>
                <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>Market Volume (last)</div>
                <div style={{ fontWeight: 700 }}>{lastMarketVolume.toLocaleString()}</div>
              </div>
              <div style={{ fontSize: 11 }}>
                <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>Avg Volume (20 bars)</div>
                <div style={{ fontWeight: 700 }}>{Math.round(avgMarketVolume).toLocaleString()}</div>
              </div>
              <div style={{ fontSize: 11 }}>
                <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>Volume Pulse</div>
                <div style={{ fontWeight: 700, color: volumePulse >= 1 ? 'var(--accent-green)' : 'var(--text-secondary)' }}>
                  {avgMarketVolume > 0 ? `${volumePulse.toFixed(2)}x` : '--'}
                </div>
              </div>
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>
              Volume effect on decision: {volumeMetaActive > 0 ? `+${volumeMetaPct}% confidence support` : 'No extra volume boost for this setup.'}
            </div>
          </div>
          {/* Buy/Sell Toggle */}
          <div className="flex items-center gap-1 mb-2">
            <button
              className={`btn btn-sm ${manualAction === 'BUY' ? 'btn-success' : 'btn-secondary'}`}
              onClick={(e) => { e.stopPropagation(); setManualAction('BUY'); }}
              style={{ fontSize: 11, padding: '3px 14px', fontWeight: 600 }}
            >
              ▲ Buy
            </button>
            <button
              className={`btn btn-sm ${manualAction === 'SELL' ? 'btn-danger' : 'btn-secondary'}`}
              onClick={(e) => { e.stopPropagation(); setManualAction('SELL'); }}
              style={{ fontSize: 11, padding: '3px 14px', fontWeight: 600 }}
            >
              ▼ Sell
            </button>
            {rec.signal.action !== 'HOLD' && manualAction !== rec.signal.action && (
              <span style={{ fontSize: 9, color: 'var(--accent-yellow)', marginLeft: 6 }}>⚠ AI recommends {rec.signal.action}</span>
            )}
          </div>
          {/* Amount row */}
          {suggestedAmount && (
            <div style={{ fontSize: 10, color: 'var(--accent-blue)', marginBottom: 6 }}>
              AI suggested amount: <span style={{ fontWeight: 700 }}>${suggestedAmount}</span>
              {suggestedPct ? ` (${suggestedPct.toFixed(2)}% free margin)` : ''}
            </div>
          )}
          <div className="flex items-center gap-2" style={{ flexWrap: 'wrap' }}>
            {[10, 25, 50, 100].map((preset) => (
              <button
                key={preset}
                className={`btn ${amount === String(preset) ? (manualAction === 'SELL' ? 'btn-danger' : 'btn-success') : 'btn-secondary'} btn-sm`}
                onClick={() => handleAmountChange(String(preset))}
                style={{ fontSize: 11, padding: '4px 10px' }}
              >
                ${preset}
              </button>
            ))}
            <div style={{ position: 'relative', width: 100 }}>
              <span style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', fontSize: 12 }}>$</span>
              <input
                className="form-input"
                type="number"
                placeholder="Amount"
                value={amount}
                onChange={(e) => handleAmountChange(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                style={{ fontSize: 12, padding: '4px 8px 4px 20px', width: '100%' }}
                min="1"
              />
            </div>
            <button
              className={`btn ${manualAction === 'SELL' ? 'btn-danger' : 'btn-success'} btn-sm`}
              onClick={(e) => { e.stopPropagation(); handleTrade(); }}
              disabled={executionBlocked || loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
              title={executionBlockedReason || undefined}
              style={{ fontSize: 11, padding: '4px 14px', fontWeight: 600 }}
            >
              {loading ? <span className="loading-spinner" style={{ width: 10, height: 10 }} /> : <Play size={10} />}
              {amount ? `${manualAction === 'SELL' ? 'Sell' : 'Buy'} $${amount}` : (manualAction === 'SELL' ? 'Sell' : 'Buy')}
            </button>
          </div>
          {executionBlockedReason && (
            <div style={{ fontSize: 10, color: 'var(--accent-yellow)', marginTop: 4 }}>
              {executionBlockedReason}
            </div>
          )}

          {/* Volume info */}
          {volumeInfo && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
              Volume: <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{volumeInfo.volume} lots</span>
              {' '}(~${volumeInfo.actual_cost.toLocaleString()} market value)
            </div>
          )}

          {/* SL / TP row - dollar based */}
          {volumeInfo && (
            <div className="flex items-center gap-3 mt-2" style={{ flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 10, color: 'var(--accent-red)', fontWeight: 600 }}>Max Loss $</span>
                <input className="form-input" type="number" value={slDollar}
                  onChange={(e) => setSlDollar(e.target.value)} onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 11, padding: '3px 6px', width: 70, fontFamily: 'monospace', borderColor: parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? '#ef4444' : undefined }} step="1" min={Math.ceil(minSlTp)} />
                {parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? (
                  <span style={{ fontSize: 9, color: '#ef4444', fontWeight: 600 }}>min ${Math.ceil(minSlTp)}</span>
                ) : slDollar && parseFloat(amount) > 0 ? (
                  <span style={{ fontSize: 9, color: 'var(--accent-red)' }}>
                    ${amount}→${(parseFloat(amount) - parseFloat(slDollar)).toFixed(0)}
                  </span>
                ) : null}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 10, color: 'var(--accent-green)', fontWeight: 600 }}>Target +$</span>
                <input className="form-input" type="number" value={tpDollar}
                  onChange={(e) => setTpDollar(e.target.value)} onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 11, padding: '3px 6px', width: 70, fontFamily: 'monospace', borderColor: parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? '#ef4444' : undefined }} step="1" min={Math.ceil(minSlTp)} />
                {parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? (
                  <span style={{ fontSize: 9, color: '#ef4444', fontWeight: 600 }}>min ${Math.ceil(minSlTp)}</span>
                ) : tpDollar && parseFloat(amount) > 0 ? (
                  <span style={{ fontSize: 9, color: 'var(--accent-green)' }}>
                    ${amount}→${(parseFloat(amount) + parseFloat(tpDollar)).toFixed(0)}
                  </span>
                ) : null}
              </div>
            </div>
          )}
          {rec.explanation && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>{rec.explanation}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default function SimpleDashboard({ status, onRefresh, reconnecting = false }: Props) {
  const navigate = useNavigate();
  const [, startAnalysisTransition] = useTransition();
  const activePlatform = normalizePlatformId(status?.platform);
  const activeMarketScope = `${activePlatform}:${status?.account?.login || 'no-account'}:${status?.account?.server || ''}`;
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [allSymbols, setAllSymbols] = useState<AvailableSymbolRow[]>([]);
  const [selectedMarketSymbols, setSelectedMarketSymbols] = useState<string[]>([]);
  const [dashboardScopeSymbols, setDashboardScopeSymbols] = useState<string[]>([]);
  const [reconcileLoading, setReconcileLoading] = useState(false);
  const [reconcileError, setReconcileError] = useState<string | null>(null);
  const [localReconciliation, setLocalReconciliation] = useState(status?.reconciliation || null);

  useEffect(() => {
    if (status?.reconciliation) {
      setLocalReconciliation(status.reconciliation);
    }
  }, [status?.reconciliation]);

  const runReconciliation = useCallback(async () => {
    setReconcileLoading(true);
    setReconcileError(null);
    try {
      const report = await api.reconcileRuntimeState(300);
      setLocalReconciliation(report);
      onRefresh();
    } catch (err) {
      setReconcileError(formatUiError(err));
    } finally {
      setReconcileLoading(false);
    }
  }, [onRefresh]);
  const [hasManualMarketSelection, setHasManualMarketSelection] = useState(false);
  const [marketDraftSymbols, setMarketDraftSymbols] = useState<string[]>([]);
  const [showMarketSelector, setShowMarketSelector] = useState(false);
  const [marketSearch, setMarketSearch] = useState('');
  const [availableMarketCount, setAvailableMarketCount] = useState(0);
  const [liveTicks, setLiveTicks] = useState<Record<string, TickData>>({});
  const [analyzedCount, setAnalyzedCount] = useState(0);
  const [positions, setPositions] = useState<PositionInfo[]>(() => readCachedPositions(activeMarketScope));
  const [scanning, setScanning] = useState(false);
  const scanningRef = useRef(false);
  const scanGenerationRef = useRef(0);
  const liveConfidenceRefreshingRef = useRef(false);
  const lastLiveConfidenceRefreshAtRef = useRef(0);
  const strategiesLoadingRef = useRef(false);
  const autoTradeStatusLoadingRef = useRef(false);
  const positionsLoadingRef = useRef(false);
  const emptyPositionsStreakRef = useRef(0);
  const lastDetailedPositionsRefreshAtRef = useRef(0);
  const [lastScan, setLastScan] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string>('All');
  const [visibleMarketRows, setVisibleMarketRows] = useState<number>(60);
  const marketSelectorInitializedRef = useRef(false);
  const marketCatalogLoadingRef = useRef(false);
  const marketCatalogLoadedForScopeRef = useRef(false);

  // Auto-trade state
  const [autoTradeRunning, setAutoTradeRunning] = useState(false);
  const [autoTradeLoading, setAutoTradeLoading] = useState(false);
  const [showAutoTradeBudgetModal, setShowAutoTradeBudgetModal] = useState(false);
  const [autoTradeBudgetMode, setAutoTradeBudgetMode] = useState<'full_margin' | 'custom_budget'>('full_margin');
  const [autoTradeBudgetInput, setAutoTradeBudgetInput] = useState('');
  const [activeAutoTradeBudgetMode, setActiveAutoTradeBudgetMode] = useState<'full_margin' | 'custom_budget'>(
    status?.runtime_controls?.auto_trade_budget_mode === 'custom_budget' ? 'custom_budget' : 'full_margin',
  );
  const [activeAutoTradeBudgetUsd, setActiveAutoTradeBudgetUsd] = useState<number | null>(
    typeof status?.runtime_controls?.auto_trade_budget_usd === 'number' ? status.runtime_controls.auto_trade_budget_usd : null,
  );
  const [autoTradeLogs, setAutoTradeLogs] = useState<Array<{
    timestamp: number; symbol: string; action: string;
    confidence?: number; quality_score?: number; detail: string; success?: boolean;
    decision_reason?: string; gemini_summary?: string; meta_model_summary?: string;
  }>>([]);
  const [expandedAutoTradeLogs, setExpandedAutoTradeLogs] = useState<Set<string>>(() => new Set());
  const [posManagerRunning, setPosManagerRunning] = useState(false);
  const [portfolioSnapshot, setPortfolioSnapshot] = useState(status?.portfolio || null);
  const [geminiState, setGeminiState] = useState<GeminiTelemetry>(() => normalizeGeminiTelemetry({
    available: status?.gemini_available ?? false,
    degraded: status?.gemini_degraded ?? false,
    last_error: status?.gemini_last_error ?? null,
    state: status?.gemini_state ?? ((status?.gemini_degraded ?? false) ? 'degraded' : (status?.gemini_available ? 'available' : 'unavailable')),
    primary_model: status?.gemini_primary_model ?? '',
    active_model: status?.gemini_active_model ?? '',
    using_fallback_model: status?.gemini_using_fallback_model ?? false,
    cooldown_seconds: status?.gemini_cooldown_seconds ?? 0,
    credits_pct: status?.gemini_credits_pct ?? 0,
  }));
  const geminiLiveRef = useRef(false);
  const [strategies, setStrategies] = useState<StrategyProfile[]>([]);
  const [activeStrategyId, setActiveStrategyId] = useState<string>(status?.active_strategy?.id || '');
  const [strategyLoadingId, setStrategyLoadingId] = useState<string>('');
  const [strategyInfoOpen, setStrategyInfoOpen] = useState(false);
  const [strategyInfo, setStrategyInfo] = useState<StrategyProfile | null>(null);
  const [connectLoading, setConnectLoading] = useState(false);
  const [connectError, setConnectError] = useState('');

  const connected = status?.connected ?? false;
  const account = status?.account;
  const killSwitchActive = Boolean(status?.kill_switch?.enabled);
  const killSwitchReason = status?.kill_switch?.reason || 'Global trading kill switch is active.';
  const canEnableAutoTrader = status?.permissions?.can_enable_autotrader !== false;
  const canAutoTradeExecutionMode = status?.permissions?.can_paper_trade !== false;
  const manualExecutionBlockedReason = killSwitchActive
    ? killSwitchReason
    : status?.account && status?.is_demo && status?.permissions?.can_execute_demo === false
      ? 'Your account is not permitted to execute demo trades.'
      : status?.account && !status?.is_demo && status?.permissions?.can_execute_live === false
        ? 'Your account is not permitted to execute live trades.'
        : status?.account && !status?.is_demo && status?.live_trading_readiness?.can_execute_live_orders === false
          ? (status.live_trading_readiness.blockers?.[0] || 'Live execution is blocked by backend readiness checks.')
          : '';
  const autoTradeBlockedReason = !canEnableAutoTrader
    ? 'Your account is not permitted to enable AutoTrader.'
    : !canAutoTradeExecutionMode
      ? 'Your account is not permitted to run paper/shadow AutoTrader.'
      : killSwitchActive
        ? killSwitchReason
        : status?.user_policy?.auto_trading_allowed === false
          ? 'AutoTrader is disabled by the active risk profile. Enable "Auto-Trading Allowed By This Profile" in Risk Management.'
        : '';
  const currency = account?.currency || 'USD';
  const runtimeCountryCode = useMemo(() => detectBrowserCountryCode(), []);
  const runtimePopularMarketLabel = useMemo(
    () => popularMarketLabelForCountry(runtimeCountryCode),
    [runtimeCountryCode],
  );
  const geminiErrorSummary = summarizeGeminiError(geminiState.last_error);
  const activeStrategy = useMemo(
    () => strategies.find((item) => item.id === activeStrategyId) || null,
    [strategies, activeStrategyId],
  );
  const strategyAllowedSymbols = useMemo(() => {
    const raw = activeStrategy?.config?.allowed_symbols;
    if (!Array.isArray(raw)) return [] as string[];
    return raw
      .map((item) => String(item || '').trim().toUpperCase())
      .filter(Boolean);
  }, [activeStrategy]);
  const marketCatalogHasBroadCoverage = useMemo(() => {
    const categories = new Set(allSymbols.map((row) => normalizeCategoryName(row.category)));
    return SUPPORTED_MARKET_CATEGORIES.every((category) => categories.has(category));
  }, [allSymbols]);

  const loadStrategies = useCallback(async () => {
    if (!connected || strategiesLoadingRef.current) return;
    strategiesLoadingRef.current = true;
    try {
      const response = await api.getStrategies();
      const items = Array.isArray(response.items) ? response.items : [];
      setStrategies(items);
      setActiveStrategyId(response.active_strategy_id || items.find((x) => x.is_selected)?.id || '');
    } catch {
      // Keep trading UI responsive even if strategy list is temporarily unavailable.
    } finally {
      strategiesLoadingRef.current = false;
    }
  }, [connected]);

  const handleGetStartedConnect = useCallback(() => {
    setConnectError('');
    window.location.hash = '#/connection';
  }, []);

  useEffect(() => {
    // Treat platform switch as a separate dashboard session to prevent
    // cross-platform contamination of visible markets/recommendations.
    scanGenerationRef.current += 1;
    setRecommendations([]);
    setAllSymbols([]);
    setSelectedMarketSymbols([]);
    setDashboardScopeSymbols([]);
    setHasManualMarketSelection(false);
    setMarketDraftSymbols([]);
    setShowMarketSelector(false);
    setLiveTicks({});
    setAnalyzedCount(0);
    setAvailableMarketCount(0);
    setLastScan(null);
    setPositions(readCachedPositions(activeMarketScope));
    marketCatalogLoadedForScopeRef.current = false;
  }, [activeMarketScope]);

  useEffect(() => {
    try {
      const manualKey = scopedStorageKey(MARKET_SELECTION_MANUAL_KEY, activeMarketScope);
      const selectionKey = scopedStorageKey(MARKET_SELECTION_STORAGE_KEY, activeMarketScope);
      const manualFlag = localStorage.getItem(manualKey);
      if (manualFlag === '1') {
        setHasManualMarketSelection(true);
      }
      const raw = localStorage.getItem(selectionKey);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        const cleaned = parsed.map((x) => String(x || '').trim()).filter(Boolean);
        setSelectedMarketSymbols(cleaned);
        setDashboardScopeSymbols(cleaned);
      }
    } catch {
      // ignore malformed local cache
    }
  }, [activeMarketScope]);

  useEffect(() => {
    const cached = loadDashboardScanCache(activeMarketScope);
    if (!cached) return;
    startAnalysisTransition(() => {
      const cachedSymbols = dedupeRowsByIdentity(cached.allSymbols);
      const usableIdentities = new Set(cachedSymbols.map((row) => marketIdentityKey(row.name)).filter(Boolean));
      setRecommendations(dedupeRecommendationsByIdentity(cached.recommendations)
        .filter((rec) => usableIdentities.size === 0 || usableIdentities.has(marketIdentityKey(rec.symbol))));
      setAllSymbols(cachedSymbols);
      setAnalyzedCount(cached.analyzedCount);
      setAvailableMarketCount(cached.availableMarketCount);
      setLastScan(cached.lastScan);
    });
  }, [activeMarketScope, activePlatform, startAnalysisTransition]);

  const currentRecommendationSymbols = useMemo(
    () => dedupeSymbolsByIdentity(recommendations.map((rec) => String(rec.symbol || '').trim()).filter(Boolean)),
    [recommendations],
  );

  useEffect(() => {
    if (!showMarketSelector) {
      marketSelectorInitializedRef.current = false;
      return;
    }
    if (marketSelectorInitializedRef.current) return;
    const popular = pickPopularSymbolsForCountry(allSymbols, runtimeCountryCode);
    const base = hasManualMarketSelection && selectedMarketSymbols.length > 0
      ? selectedMarketSymbols
      : currentRecommendationSymbols.length > 0
        ? currentRecommendationSymbols
        : popular;
    setMarketDraftSymbols(base);
    marketSelectorInitializedRef.current = true;
  }, [showMarketSelector, selectedMarketSymbols, hasManualMarketSelection, allSymbols, status?.platform, currentRecommendationSymbols, runtimeCountryCode]);

  useEffect(() => {
    if (status?.portfolio) setPortfolioSnapshot(status.portfolio);
    // Avoid oscillation between /status (app-level poll) and /auto-trade/status (dashboard poll).
    // Once we have live dashboard Gemini telemetry, keep it as the source of truth.
    if (geminiLiveRef.current) return;
    setGeminiState((prev) => {
      return normalizeGeminiTelemetry({
        available: status?.gemini_available,
        degraded: status?.gemini_degraded,
        last_error: status?.gemini_last_error ?? prev.last_error ?? null,
        state: status?.gemini_state,
        primary_model: status?.gemini_primary_model,
        active_model: status?.gemini_active_model,
        using_fallback_model: status?.gemini_using_fallback_model,
        cooldown_seconds: status?.gemini_cooldown_seconds,
        credits_pct: status?.gemini_credits_pct,
      }, prev);
    });
    if (status?.active_strategy?.id) {
      setActiveStrategyId(status.active_strategy.id);
    }
  }, [status]);

  useEffect(() => {
    if (!connected) return;
    loadStrategies();
  }, [connected, loadStrategies]);

  useEffect(() => {
    if (!connected) return;
    const t = setInterval(() => {
      loadStrategies();
    }, 15000);
    return () => clearInterval(t);
  }, [connected, loadStrategies]);

  const refreshPositions = useCallback(async (options?: { detailed?: boolean }) => {
    if (!connected || positionsLoadingRef.current) return;
    positionsLoadingRef.current = true;
    try {
      const now = Date.now();
      const shouldLoadDetails = options?.detailed === true
        || lastDetailedPositionsRefreshAtRef.current <= 0
        || now - lastDetailedPositionsRefreshAtRef.current > 8000;
      const pos = await api.getPositions(undefined, shouldLoadDetails);
      if (shouldLoadDetails) {
        lastDetailedPositionsRefreshAtRef.current = now;
      }
      const normalized = Array.isArray(pos) ? pos : [];
      setPositions((prev) => {
        // MT5/bridge can occasionally return a transient empty snapshot; keep the
        // previous non-empty list for one cycle to avoid active-trades flicker.
        if (normalized.length === 0 && prev.length > 0) {
          emptyPositionsStreakRef.current += 1;
          if (emptyPositionsStreakRef.current < 2) return prev;
        } else {
          emptyPositionsStreakRef.current = 0;
        }
        const stable = mergeStablePositionSnapshot(prev, normalized);
        writeCachedPositions(activeMarketScope, stable);
        return stable;
      });
    } catch {
    } finally {
      positionsLoadingRef.current = false;
    }
  }, [activeMarketScope, connected]);

  const refreshPositionsBurst = useCallback(async () => {
    // Make manual trade feedback feel immediate: refresh rapidly for a short window.
    await refreshPositions({ detailed: true });
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      await refreshPositions({ detailed: false });
    }
  }, [refreshPositions]);

  const saveSelectedMarkets = useCallback((symbols: string[], options?: { manual?: boolean }) => {
    const unique = Array.from(new Set(symbols.map((s) => String(s || '').trim()).filter(Boolean)));
    const manual = options?.manual !== false;
    if (manual) {
      scanGenerationRef.current += 1;
    }
    if (unique.length > 1) {
      setSelectedCategory('All');
    }
    setHasManualMarketSelection(manual);
    setSelectedMarketSymbols(unique);
    setDashboardScopeSymbols(unique);
    setRecommendations((prev) => {
      const symbolByName = new Map<string, AvailableSymbolRow>();
      const symbolByIdentity = new Map<string, AvailableSymbolRow>();
      for (const row of allSymbols) {
        const name = String(row.name || '').trim();
        if (!name) continue;
        symbolByName.set(name, row);
        symbolByName.set(name.toUpperCase(), row);
        const identity = marketIdentityKey(name);
        if (identity && !symbolByIdentity.has(identity)) symbolByIdentity.set(identity, row);
      }
      const resolveRow = (rawSymbol: string) => {
        const key = String(rawSymbol || '').trim();
        if (!key) return undefined;
        return (
          symbolByName.get(key)
          || symbolByName.get(key.toUpperCase())
          || symbolByIdentity.get(marketIdentityKey(key))
        );
      };
      const prevByIdentity = new Map<string, Recommendation>();
      prev.forEach((item) => {
        const identity = marketIdentityKey(item.symbol);
        if (identity && !prevByIdentity.has(identity)) {
          prevByIdentity.set(identity, item);
        }
      });
      return dedupeRecommendationsByIdentity(unique.map((symbol) => {
        const identity = marketIdentityKey(symbol);
        const prevRec = identity ? prevByIdentity.get(identity) : null;
        if (prevRec) return prevRec;
        const row = resolveRow(symbol);
        return row ? createPendingRecommendation(row) : {
          symbol,
          category: 'Other',
          description: '',
          signal: { action: 'HOLD', confidence: 0, stop_loss: null, take_profit: null, max_holding_minutes: null, reason: 'Waiting for scan.' },
          signal_id: null,
          risk_decision: { approved: false, reason: 'Pending analysis.', adjusted_volume: 0, warnings: [], status: 'warn' },
          entry_price_estimate: 0,
          explanation: 'Waiting for scan.',
          ready_to_execute: false,
        } as Recommendation;
      }));
    });
    const selectionKey = scopedStorageKey(MARKET_SELECTION_STORAGE_KEY, activeMarketScope);
    const manualKey = scopedStorageKey(MARKET_SELECTION_MANUAL_KEY, activeMarketScope);
    localStorage.setItem(selectionKey, JSON.stringify(unique));
    if (manual) {
      localStorage.setItem(manualKey, '1');
    } else {
      localStorage.removeItem(manualKey);
    }
  }, [activeMarketScope, allSymbols]);

  const saveScanScopeSymbols = useCallback((symbols: string[], options?: { manual?: boolean }) => {
    const unique = Array.from(new Set(symbols.map((s) => String(s || '').trim()).filter(Boolean)));
    const manual = options?.manual !== false;
    setHasManualMarketSelection(manual);
    setSelectedMarketSymbols(unique);
    setDashboardScopeSymbols(unique);
    const selectionKey = scopedStorageKey(MARKET_SELECTION_STORAGE_KEY, activeMarketScope);
    const manualKey = scopedStorageKey(MARKET_SELECTION_MANUAL_KEY, activeMarketScope);
    localStorage.setItem(selectionKey, JSON.stringify(unique));
    if (manual) {
      localStorage.setItem(manualKey, '1');
    } else {
      localStorage.removeItem(manualKey);
    }
  }, [activeMarketScope]);

  const loadAvailableMarketCatalog = useCallback(async () => {
    if (marketCatalogLoadingRef.current) return;
    marketCatalogLoadingRef.current = true;
    try {
      const available = await api.getAvailableSymbols(undefined, false, true);
      const availableTotal = Number(available.total || 0);
      const brokerSymbols = extractAvailableMarketRows(available) as AvailableSymbolRow[];
      const fullUniverse = dedupeRowsByIdentity(brokerSymbols.map((row) => ({
        ...row,
        category: normalizeCategoryName(row.category),
      })));
      startAnalysisTransition(() => {
        setAvailableMarketCount(fullUniverse.length || availableTotal || 0);
        setAllSymbols(fullUniverse);
      });
      marketCatalogLoadedForScopeRef.current = true;
    } catch {
      // Keep cached symbols if the local backend is starting or temporarily unavailable.
    } finally {
      marketCatalogLoadingRef.current = false;
    }
  }, [startAnalysisTransition]);

  useEffect(() => {
    if (!allSymbols.length || !hasManualMarketSelection || !selectedMarketSymbols.length) return;
    const popular = pickPopularSymbolsForCountry(allSymbols, runtimeCountryCode);
    const current = selectedMarketSymbols.map((sym) => marketIdentityKey(sym)).filter(Boolean);
    const baseline = popular.map((sym) => marketIdentityKey(sym)).filter(Boolean);
    if (current.length === baseline.length && current.every((sym, index) => sym === baseline[index])) {
      setHasManualMarketSelection(false);
      localStorage.removeItem(scopedStorageKey(MARKET_SELECTION_MANUAL_KEY, activeMarketScope));
    }
  }, [activeMarketScope, allSymbols, hasManualMarketSelection, selectedMarketSymbols, runtimeCountryCode]);

  useEffect(() => {
    const needsInitialCatalog = allSymbols.length === 0;
    const needsBroadSelectorCatalog = showMarketSelector && !marketCatalogHasBroadCoverage && !marketCatalogLoadedForScopeRef.current;
    if (needsInitialCatalog || needsBroadSelectorCatalog) {
      void loadAvailableMarketCatalog();
    }
  }, [allSymbols.length, loadAvailableMarketCatalog, marketCatalogHasBroadCoverage, showMarketSelector]);


  const scanMarkets = useCallback(async (symbolsOverride?: string[], options?: { manual?: boolean }) => {
    if (!connected || scanningRef.current) return;
    const runId = scanGenerationRef.current + 1;
    scanGenerationRef.current = runId;
    scanningRef.current = true;
    setScanning(true);
    setError('');
    try {
      let availableTotal = 0;
      let fullUniverse = allSymbols;
      if (!fullUniverse || fullUniverse.length === 0 || !!symbolsOverride) {
        const available = await api.getAvailableSymbols(undefined, false, true);
        availableTotal = Number(available.total || 0);
        const brokerSymbols = extractAvailableMarketRows(available) as AvailableSymbolRow[];
        fullUniverse = brokerSymbols;
      }
      fullUniverse = dedupeRowsByIdentity(fullUniverse.map((row) => ({
        ...row,
        category: normalizeCategoryName(row.category),
      })));
      startAnalysisTransition(() => {
        setAvailableMarketCount(fullUniverse.length || availableTotal || 0);
        setAllSymbols(fullUniverse);
      });

      const eventPriorities = connected
        ? await Promise.race([
          api.getEventRecommendations({ limit: 8, forceRefresh: false, classifyWithGemini: false }),
          new Promise<null>((resolve) => setTimeout(() => resolve(null), 1500)),
        ]).catch(() => null)
        : null;
      const eventPrioritySymbols = (eventPriorities?.recommendations || [])
        .map((item) => String(item.symbol || '').trim())
        .filter(Boolean);

      // Fast opportunity scan: score a focused subset first to avoid very long cycles.
      const mode = String(status?.user_policy?.mode || 'balanced').toLowerCase();
      const categoryPriority: Record<string, number> = mode === 'aggressive'
        ? { Indices: 0, Commodities: 1, Stocks: 2, Crypto: 3, Forex: 4, Other: 5 }
        : mode === 'safe'
          ? { Stocks: 0, Indices: 1, Commodities: 2, Crypto: 3, Forex: 4, Other: 5 }
          : { Commodities: 0, Indices: 1, Stocks: 2, Crypto: 3, Forex: 4, Other: 5 };

      const symbolByName = new Map<string, AvailableSymbolRow>();
      const symbolByIdentity = new Map<string, AvailableSymbolRow>();
      for (const row of fullUniverse) {
        const name = String(row.name || '').trim();
        if (!name) continue;
        symbolByName.set(name, row);
        symbolByName.set(name.toUpperCase(), row);
        const identity = marketIdentityKey(name);
        if (identity && !symbolByIdentity.has(identity)) {
          symbolByIdentity.set(identity, row);
        }
      }
      const resolveRow = (rawSymbol: string): AvailableSymbolRow | undefined => {
        const key = String(rawSymbol || '').trim();
        if (!key) return undefined;
        return (
          symbolByName.get(key)
          || symbolByName.get(key.toUpperCase())
          || symbolByIdentity.get(marketIdentityKey(key))
        );
      };

      const manualScan = !!options?.manual;
      const defaultPopular = pickPopularSymbolsForCountry(fullUniverse, runtimeCountryCode);
      const selectedInputRaw = (symbolsOverride && symbolsOverride.length > 0)
        ? symbolsOverride
        : (manualScan
          ? fullUniverse.map((row) => String(row.name || '').trim()).filter(Boolean)
          : hasManualMarketSelection
          ? selectedMarketSymbols
          : defaultPopular);
      let selectedInput = selectedInputRaw;
      selectedInput = dedupeSymbolsByIdentity(selectedInput);
      if (strategyAllowedSymbols.length > 0) {
        const allowed = new Set(strategyAllowedSymbols);
        selectedInput = selectedInput.filter((sym) => allowed.has(String(sym || '').trim().toUpperCase()));
        if (selectedInput.length === 0) {
          selectedInput = strategyAllowedSymbols.filter((sym) => symbolByName.has(sym));
        }
      }
      let selectedRowsFromInput = dedupeRowsByIdentity(selectedInput
        .map((sym) => resolveRow(sym))
        .filter(Boolean) as AvailableSymbolRow[]);
      if (selectedRowsFromInput.length === 0 && defaultPopular.length > 0) {
        selectedRowsFromInput = dedupeRowsByIdentity(defaultPopular
          .map((sym) => resolveRow(sym))
          .filter(Boolean) as AvailableSymbolRow[]);
      }
      const selectedNow = selectedRowsFromInput.map((row) => row.name);
      if (manualScan && selectedNow.length > 0) {
        await api.selectSymbols(selectedNow).catch(() => null);
      }
      if (!manualScan && !hasManualMarketSelection && defaultPopular.length > 0) {
        saveScanScopeSymbols(defaultPopular, { manual: false });
      }

      const selectedRowsAll = dedupeRowsByIdentity(prioritizeRowsBySymbols(selectedNow
        .map((sym) => resolveRow(sym))
        .filter(Boolean) as AvailableSymbolRow[], eventPrioritySymbols));
      const selectedRows = manualScan ? selectedRowsAll : selectedRowsAll.slice(0, MAX_UI_SYMBOL_ROWS);
      const quoteReadyRows = selectedRows.filter(hasUsableBrokerQuote);
      const preferredRows = manualScan
        ? selectedRows
        : quoteReadyRows.length >= Math.min(8, selectedRows.length)
        ? quoteReadyRows
        : selectedRows;

      const byCategory = new Map<string, Array<any>>();
      for (const item of preferredRows) {
        const cat = normalizeCategoryName(item.category);
        const bucket = byCategory.get(cat) || [];
        bucket.push(item);
        byCategory.set(cat, bucket);
      }
      for (const bucket of byCategory.values()) {
        bucket.sort((a, b) => Number(a.spread || 0) - Number(b.spread || 0));
      }

      // Keep broad market representation; avoid one-category dominance.
      const targetCount = Math.max(12, Math.min(48, selectedRows.length || 24));
      const categoryOrder = Array.from(byCategory.keys()).sort(
        (a, b) => (categoryPriority[a] ?? 99) - (categoryPriority[b] ?? 99),
      );
      const perCategoryCap = mode === 'aggressive' ? 8 : 6;
      const selected: Array<any> = [];
      const used = new Set<string>();

      let added = true;
      while (selected.length < targetCount && added) {
        added = false;
        for (const cat of categoryOrder) {
          const bucket = byCategory.get(cat) || [];
          const takenInCat = selected.filter((x) => normalizeCategoryName(x.category) === cat).length;
          if (takenInCat >= perCategoryCap) continue;
          const next = bucket.find((x) => !used.has(String(x.name || '')));
          if (!next) continue;
          selected.push(next);
          used.add(String(next.name || ''));
          added = true;
          if (selected.length >= targetCount) break;
        }
      }

      const scanLimit = MAX_MT5_SCAN_SYMBOLS;
      const preferredIdentitySet = buildPreferredScanIdentitySet(status?.platform, fullUniverse, runtimeCountryCode);
      const scanRows = (selected.length > 0 ? selected : preferredRows) as AvailableSymbolRow[];
      const prioritizedForAi = prioritizeRowsBySymbols(
        [...scanRows].sort((a, b) => {
          const aIdentity = marketIdentityKey(a.name);
          const bIdentity = marketIdentityKey(b.name);
          const aPreferred = preferredIdentitySet.has(aIdentity) ? 1 : 0;
          const bPreferred = preferredIdentitySet.has(bIdentity) ? 1 : 0;
          if (aPreferred !== bPreferred) return bPreferred - aPreferred;
          const aQuoteReady = hasUsableBrokerQuote(a) ? 1 : 0;
          const bQuoteReady = hasUsableBrokerQuote(b) ? 1 : 0;
          if (aQuoteReady !== bQuoteReady) return bQuoteReady - aQuoteReady;
          return Number(a.spread || 0) - Number(b.spread || 0);
        }),
        eventPrioritySymbols,
      );
      const symbolsForAi = prioritizedForAi
        .map((s) => s.name)
        .filter(Boolean)
        .slice(0, manualScan ? Math.min(MAX_MANUAL_SCAN_SYMBOLS, Math.max(scanLimit, selectedRows.length)) : scanLimit);

      const symbolChunks = chunkSymbols(
        symbolsForAi,
        manualScan ? 6 : 5,
      );
      const mergedRecommendations: Recommendation[] = [];
      let scannedAt = Date.now() / 1000;
      let successfulChunks = 0;
      let lastChunkError: unknown = null;
      for (const symbolsChunk of symbolChunks) {
        try {
          const partial = await api.smartEvaluate(symbolsChunk, {
            manual: manualScan,
            autoExecute: autoTradeRunning && !manualScan,
          });
          mergedRecommendations.push(...(partial.recommendations || []));
          if (partial.scanned_at) scannedAt = partial.scanned_at;
          successfulChunks += 1;
          if (runId !== scanGenerationRef.current) {
            return;
          }
        } catch (chunkError) {
          lastChunkError = chunkError;
          // Keep scanning remaining chunks so one timeout doesn't zero all rows.
        }
      }
      if (successfulChunks <= 0) {
        throw (lastChunkError || new Error('AI scan failed'));
      }
      const result = {
        recommendations: mergedRecommendations,
        scanned_at: scannedAt,
      };
      const scanned = (result.recommendations || []).map((r) => ({
        ...r,
        category: normalizeCategoryName(
          r.category
          || symbolByName.get(r.symbol)?.category
          || symbolByIdentity.get(marketIdentityKey(r.symbol))?.category
          || 'Other',
        ),
      }));
      if (runId !== scanGenerationRef.current) {
        return;
      }
      const scannedBySymbol = new Map<string, Recommendation>();
      scanned.forEach((rec) => {
        const identity = marketIdentityKey(rec.symbol);
        if (identity && !scannedBySymbol.has(identity)) {
          scannedBySymbol.set(identity, rec);
        }
      });

      startAnalysisTransition(() => {
        setAnalyzedCount(scanned.length);
        if (result.scanned_at) setLastScan(result.scanned_at);
        setRecommendations((prev) => {
          const prevBySymbol = new Map<string, Recommendation>();
          prev.forEach((item) => {
            const identity = marketIdentityKey(item.symbol);
            if (identity && !prevBySymbol.has(identity)) {
              prevBySymbol.set(identity, item);
            }
          });
          const nextRecommendations = dedupeRecommendationsByIdentity(selectedRows.map((row) => {
            const identity = marketIdentityKey(row.name);
            const scannedRec = scannedBySymbol.get(identity);
            const prevRec = prevBySymbol.get(identity);
            const mergedRec = scannedRec && prevRec ? mergeRecommendationStable(prevRec, scannedRec) : (scannedRec || prevRec || createPendingRecommendation(row));
            const rowBid = Number(row.bid || 0);
            const rowAsk = Number(row.ask || 0);
            const rowReferencePrice = rowBid > 0 && rowAsk > 0 ? (rowBid + rowAsk) / 2 : Math.max(rowBid, rowAsk, 0);
            if (Number(mergedRec.entry_price_estimate || 0) <= 0 && rowReferencePrice > 0) {
              return { ...mergedRec, entry_price_estimate: rowReferencePrice };
            }
            return mergedRec;
          }));
          saveDashboardScanCache(activeMarketScope, {
            recommendations: nextRecommendations,
            allSymbols: fullUniverse,
            analyzedCount: scanned.length,
            availableMarketCount: fullUniverse.length || availableTotal || 0,
            lastScan: result.scanned_at || Date.now() / 1000,
          });
          return nextRecommendations;
        });
      });
    } catch (e: any) {
      // Keep full MT5 list visible even when AI scoring degrades.
      const msg = String(e?.message || '');
      if (!isSoftTimeoutLikeError(msg)) {
        setError(msg);
      }
    } finally {
      scanningRef.current = false;
      setScanning(false);
    }
  }, [activeMarketScope, activePlatform, autoTradeRunning, connected, status?.platform, status?.user_policy?.mode, selectedMarketSymbols, hasManualMarketSelection, saveScanScopeSymbols, allSymbols, strategyAllowedSymbols, runtimeCountryCode]);

  const refreshLiveConfidence = useCallback(async () => {
    if (!connected || liveConfidenceRefreshingRef.current || scanningRef.current) return;
    const now = Date.now();
    if (now - lastLiveConfidenceRefreshAtRef.current < LIVE_CONFIDENCE_REFRESH_MIN_INTERVAL_MS) return;
    if (recommendations.length === 0) return;
    const allowed = strategyAllowedSymbols.length > 0 ? new Set(strategyAllowedSymbols) : null;
    const primarySymbols = recommendations
      .filter((r) => {
        if (allowed && !allowed.has(String(r.symbol || '').trim().toUpperCase())) return false;
        if (isClosedMarketRecommendation(r)) return false;
        return r.signal.action === 'BUY'
          || r.signal.action === 'SELL'
          || recommendationConfidence(r) > 0
          || hasUnavailableScore(r);
      })
      .map((r) => String(r.symbol || '').trim())
      .filter(Boolean);
    const fallbackSymbols = recommendations
      .filter((r) => {
        if (allowed && !allowed.has(String(r.symbol || '').trim().toUpperCase())) return false;
        return !isClosedMarketRecommendation(r);
      })
      .map((r) => String(r.symbol || '').trim())
      .filter(Boolean);
    const symbols = dedupeSymbolsByIdentity([...primarySymbols, ...fallbackSymbols])
      .slice(0, 12);
    if (symbols.length === 0) return;
    liveConfidenceRefreshingRef.current = true;
    try {
      const result = await api.smartEvaluate(symbols);
      const scanned = (result.recommendations || []).filter((r) => !!r?.symbol);
      if (scanned.length === 0) return;
      const scannedBySymbol = new Map<string, Recommendation>();
      scanned.forEach((rec) => {
        const identity = marketIdentityKey(rec.symbol);
        if (identity && !scannedBySymbol.has(identity)) {
          scannedBySymbol.set(identity, rec);
        }
      });
      startAnalysisTransition(() => {
        setRecommendations((prev) => dedupeRecommendationsByIdentity(prev.map((rec) => {
          const identity = marketIdentityKey(rec.symbol);
          const live = scannedBySymbol.get(identity);
          return live ? mergeRecommendationStable(rec, live) : rec;
        })));
      });
    } catch {
      // Keep latest stable confidence values if this refresh pass fails.
    } finally {
      lastLiveConfidenceRefreshAtRef.current = Date.now();
      liveConfidenceRefreshingRef.current = false;
    }
  }, [connected, recommendations, strategyAllowedSymbols]);

  useEffect(() => {
    refreshPositionsBurst();
    const pollMs = positions.length > 0 ? 2000 : 1000;
    const t = setInterval(refreshPositions, pollMs);
    return () => clearInterval(t);
  }, [refreshPositions, refreshPositionsBurst, positions.length]);


  useEffect(() => {
    if (connected) {
      const kickoff = window.setTimeout(() => {
        void scanMarkets();
      }, 150);
      const t = setInterval(scanMarkets, 300000);
      return () => {
        window.clearTimeout(kickoff);
        clearInterval(t);
      };
    }
  }, [connected, scanMarkets]);

  useEffect(() => {
    if (!connected) return;
    refreshLiveConfidence();
    const t = setInterval(refreshLiveConfidence, LIVE_CONFIDENCE_REFRESH_MIN_INTERVAL_MS);
    return () => clearInterval(t);
  }, [connected, refreshLiveConfidence]);

  // Poll auto-trade status
  const refreshAutoTrade = useCallback(async () => {
    if (autoTradeStatusLoadingRef.current) return;
    autoTradeStatusLoadingRef.current = true;
    try {
      const [st, activity] = await Promise.all([
        api.getAutoTradeStatus(),
        api.getAIActivity(120).catch(() => null),
      ]);
      setAutoTradeRunning(st.running);
      const statusLogs = Array.isArray(st.recent_trades) ? st.recent_trades : [];
      const brainLogs = activity
        ? [...(activity.live_activity || []), ...(activity.db_activity || [])]
        : [];
      const seen = new Set<string>();
      const boundedLogs = [...statusLogs, ...brainLogs]
        .filter((log) => {
          const key = [
            Math.floor(Number(log.timestamp || 0) / 5),
            log.symbol || '',
            log.action || '',
            log.detail || '',
          ].join('|');
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0))
        .slice(-200);
      setAutoTradeLogs(boundedLogs);
      const latestScores = latestActivityScoresByIdentity(boundedLogs);
      if (latestScores.size > 0) {
        setRecommendations((prev) => dedupeRecommendationsByIdentity(prev.map((rec) => {
          const score = latestScores.get(marketIdentityKey(rec.symbol));
          return score ? mergeActivityScores(rec, score) : rec;
        })));
      }
      setPosManagerRunning(st.position_manager_running ?? false);
      setPortfolioSnapshot(st.portfolio || null);
      const hasConcreteBudgetMode = st.budget_mode === 'custom_budget' || st.budget_mode === 'full_margin';
      const hasConcreteBudgetUsd = st.budget_usd === null || typeof st.budget_usd === 'number';
      const fallbackBudgetMode = status?.runtime_controls?.auto_trade_budget_mode === 'custom_budget' ? 'custom_budget' : activeAutoTradeBudgetMode;
      const fallbackBudgetUsd = typeof status?.runtime_controls?.auto_trade_budget_usd === 'number'
        ? status.runtime_controls.auto_trade_budget_usd
        : activeAutoTradeBudgetUsd;
      setActiveAutoTradeBudgetMode(hasConcreteBudgetMode ? st.budget_mode : fallbackBudgetMode);
      setActiveAutoTradeBudgetUsd(hasConcreteBudgetUsd ? (st.budget_usd ?? null) : fallbackBudgetUsd);
      if (st.gemini) {
        geminiLiveRef.current = true;
      }
      setGeminiState((prev) => {
        const g = st.gemini || {};
        return normalizeGeminiTelemetry({
          available: (g as any).available,
          degraded: (g as any).degraded,
          last_error: (g as any).last_error ?? prev.last_error ?? null,
          state: (g as any).state,
          primary_model: (g as any).primary_model,
          active_model: (g as any).active_model,
          using_fallback_model: (g as any).using_fallback_model,
          cooldown_seconds: (g as any).cooldown_seconds,
          credits_pct: (g as any).credits_pct,
        }, prev);
      });
    } catch {
    } finally {
      autoTradeStatusLoadingRef.current = false;
    }
  }, [status?.runtime_controls?.auto_trade_budget_mode, status?.runtime_controls?.auto_trade_budget_usd, activeAutoTradeBudgetMode, activeAutoTradeBudgetUsd]);

  const handleSelectStrategy = useCallback(async (strategyId: string) => {
    if (!strategyId || strategyLoadingId) return;
    setStrategyLoadingId(strategyId);
    setError('');
    try {
      const response = await api.selectStrategy(strategyId);
      const nextId = response.active_strategy_id || strategyId;
      setActiveStrategyId(nextId);
      setStrategies((prev) => prev.map((item) => ({
        ...item,
        is_selected: item.id === nextId,
      })));
      await refreshAutoTrade();
      void scanMarkets();
      onRefresh();
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setStrategyLoadingId('');
    }
  }, [strategyLoadingId, refreshAutoTrade, scanMarkets, onRefresh]);

  useEffect(() => {
    refreshAutoTrade();
    const t = setInterval(refreshAutoTrade, 10000);
    return () => clearInterval(t);
  }, [refreshAutoTrade]);

  const autoTradeBudgetSummary = activeAutoTradeBudgetMode === 'custom_budget' && activeAutoTradeBudgetUsd && activeAutoTradeBudgetUsd > 0
    ? `Budget: $${activeAutoTradeBudgetUsd.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : 'Budget: Full account margin';

  const openAutoTradeBudgetModal = useCallback(() => {
    setAutoTradeBudgetMode(activeAutoTradeBudgetMode);
    setAutoTradeBudgetInput(activeAutoTradeBudgetUsd && activeAutoTradeBudgetUsd > 0 ? String(activeAutoTradeBudgetUsd) : '');
    setShowAutoTradeBudgetModal(true);
  }, [activeAutoTradeBudgetMode, activeAutoTradeBudgetUsd]);

  const handleStartAutoTrade = useCallback(async () => {
    if (autoTradeLoading) return;
    if (autoTradeBlockedReason) {
      setError(`AutoTrader is blocked: ${autoTradeBlockedReason}`);
      return;
    }
    if (killSwitchActive) {
      setError(`AutoTrader is blocked by the global trading kill switch: ${killSwitchReason}`);
      return;
    }
    let nextBudgetUsd: number | undefined;
    const currentMarginUsed = Math.max(0, Number(account?.margin || 0));
    if (autoTradeBudgetMode === 'custom_budget') {
      const parsedBudget = Number(autoTradeBudgetInput.trim());
      if (!Number.isFinite(parsedBudget) || parsedBudget <= 0) {
        setError('Enter a valid custom auto-trade budget greater than 0.');
        return;
      }
      if (currentMarginUsed > 0 && parsedBudget < currentMarginUsed) {
        setError('Custom budget cannot be lower than current margin used ($' + currentMarginUsed.toFixed(2) + ').');
        return;
      }
      nextBudgetUsd = parsedBudget;
    }

    setAutoTradeLoading(true);
    try {
      if (autoTradeRunning) {
        await api.updateAutoTradeSettings({
          budget_mode: autoTradeBudgetMode,
          budget_usd: nextBudgetUsd,
        });
      } else {
        await api.startAutoTrade({
          budget_mode: autoTradeBudgetMode,
          budget_usd: nextBudgetUsd,
        });
        setAutoTradeRunning(true);
      }
      setActiveAutoTradeBudgetMode(autoTradeBudgetMode);
      setActiveAutoTradeBudgetUsd(nextBudgetUsd ?? null);
      setShowAutoTradeBudgetModal(false);
      await refreshAutoTrade();
      onRefresh();
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setAutoTradeLoading(false);
    }
  }, [account?.margin, autoTradeBlockedReason, autoTradeBudgetInput, autoTradeBudgetMode, autoTradeLoading, autoTradeRunning, killSwitchActive, killSwitchReason, onRefresh, refreshAutoTrade]);

  const toggleAutoTrade = async () => {
    if (autoTradeLoading) return;
    if (!autoTradeRunning && autoTradeBlockedReason) {
      setError(`AutoTrader is blocked: ${autoTradeBlockedReason}`);
      return;
    }
    if (!autoTradeRunning) {
      openAutoTradeBudgetModal();
      return;
    }
    setAutoTradeLoading(true);
    try {
      await api.stopAutoTrade();
      setAutoTradeRunning(false);
      await refreshAutoTrade();
      onRefresh();
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setAutoTradeLoading(false);
    }
  };

  const handleClosePosition = async (ticket: number) => {
    try {
      const result = await api.closePosition(ticket);
      await refreshPositions();
      const detail = String(result.retcode_desc || '');
      if (result.success && detail !== 'Filled') {
        window.alert(`Close order accepted by IBKR. ${detail}. The position remains visible until IBKR fills the order.`);
      }
    } catch (e: any) {
      setError(formatUiError(e));
    }
  };

  const handleCancelPositionClose = async (ticket: number) => {
    try {
      const result = await api.cancelPositionClose(ticket);
      if (!result.success) throw new Error(result.retcode_desc || 'IBKR did not cancel the close order');
      await refreshPositions();
    } catch (e: any) {
      setError(formatUiError(e));
    }
  };

  const handleModifyPosition = async (position: PositionInfo) => {
    const slInput = window.prompt('Enter new stop loss price (0 to clear):', String(position.stop_loss || 0));
    if (slInput == null) return;
    const tpInput = window.prompt('Enter new take profit price (0 to clear):', String(position.take_profit || 0));
    if (tpInput == null) return;

    const nextSL = Number(slInput);
    const nextTP = Number(tpInput);
    if (!Number.isFinite(nextSL) || !Number.isFinite(nextTP)) {
      setError('Stop loss and take profit must be valid numbers.');
      return;
    }

    try {
      await api.modifyPosition(position.ticket, nextSL, nextTP);
      refreshPositions();
      onRefresh();
    } catch (e: any) {
      setError(formatUiError(e));
    }
  };

  const handlePanicStop = async () => {
    try {
      const isPanic = status?.panic_stop ?? false;
      await api.setPanicStop(!isPanic);
      onRefresh();
    } catch {}
  };

  // Portfolio SL/TP state
  const [portfolioSL, setPortfolioSL] = useState<number | null>(null);  // dollar amount to close at loss
  const [portfolioTP, setPortfolioTP] = useState<number | null>(null);  // dollar amount to close at profit
  const [editingPortfolioTargets, setEditingPortfolioTargets] = useState(false);
  const [savingPortfolioTargets, setSavingPortfolioTargets] = useState(false);
  const [tempSL, setTempSL] = useState('');
  const [tempTP, setTempTP] = useState('');

  useEffect(() => {
    if (editingPortfolioTargets) return;
    const targets = status?.portfolio_targets;
    if (!targets) return;
    const nextSL = typeof targets.stop_loss_usd === 'number' ? Math.abs(targets.stop_loss_usd) : null;
    const nextTP = typeof targets.take_profit_usd === 'number' ? Math.abs(targets.take_profit_usd) : null;
    setPortfolioSL(nextSL);
    setPortfolioTP(nextTP);
  }, [status?.portfolio_targets, editingPortfolioTargets]);

  const [closingAll, setClosingAll] = useState(false);
  const handleSellAll = async () => {
    if (closingAll || positions.length === 0) return;
    setClosingAll(true);
    try {
      try {
        await api.closeAllPositions();
      } catch {
        // Fallback: still close in parallel if bulk endpoint is unavailable.
        await Promise.allSettled(
          positions.map((p) => api.closePosition(p.ticket))
        );
      }
      refreshPositions();
      onRefresh();
      // One extra delayed sync because broker close confirmations can lag a bit.
      setTimeout(() => {
        refreshPositions();
        onRefresh();
      }, 500);
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setClosingAll(false);
    }
  };

  const draftMarketSet = useMemo(() => new Set(marketDraftSymbols), [marketDraftSymbols]);
  const marketSelectorRows = useMemo(() => {
    const term = marketSearch.trim().toLowerCase();
    const rows = [...allSymbols];
    const draftOrder = new Map<string, number>();
    marketDraftSymbols.forEach((symbol, index) => {
      draftOrder.set(String(symbol || '').trim(), index);
    });
    rows.sort((a, b) => {
      const aName = String(a.name || '').trim();
      const bName = String(b.name || '').trim();
      const aIdx = draftOrder.has(aName) ? Number(draftOrder.get(aName)) : Number.POSITIVE_INFINITY;
      const bIdx = draftOrder.has(bName) ? Number(draftOrder.get(bName)) : Number.POSITIVE_INFINITY;
      if (aIdx !== bIdx) return aIdx - bIdx;
      const aLiveScore = liveQuoteSortScore(a);
      const bLiveScore = liveQuoteSortScore(b);
      if (aLiveScore !== bLiveScore) return bLiveScore - aLiveScore;
      const catCompare = normalizeCategoryName(a.category).localeCompare(normalizeCategoryName(b.category));
      if (catCompare !== 0) return catCompare;
      return aName.localeCompare(bName);
    });
    if (!term) return rows;
    return rows.filter((row) => {
      const n = String(row.name || '').toLowerCase();
      const d = String(row.description || '').toLowerCase();
      const c = String(row.category || '').toLowerCase();
      return n.includes(term) || d.includes(term) || c.includes(term);
    });
  }, [allSymbols, marketSearch, marketDraftSymbols]);

  const setMarketSymbolChecked = (symbol: string, checked: boolean) => {
    if (!symbol) return;
    const base = [...marketDraftSymbols];
    const has = base.includes(symbol);
    if (checked && !has) {
      setMarketDraftSymbols([...base, symbol]);
      return;
    }
    if (!checked && has) {
      setMarketDraftSymbols(base.filter((s) => s !== symbol));
    }
  };

  const removeMarketFromDraft = (symbol: string) => {
    setMarketDraftSymbols((prev) => prev.filter((s) => s !== symbol));
  };

  const moveDraftMarket = (symbol: string, direction: -1 | 1) => {
    setMarketDraftSymbols((prev) => {
      const idx = prev.indexOf(symbol);
      if (idx < 0) return prev;
      const nextIdx = idx + direction;
      if (nextIdx < 0 || nextIdx >= prev.length) return prev;
      const copy = [...prev];
      const [item] = copy.splice(idx, 1);
      copy.splice(nextIdx, 0, item);
      return copy;
    });
  };

  const resetToRuntimePopular = () => {
    const popular = pickPopularSymbolsForCountry(allSymbols, runtimeCountryCode);
    setMarketDraftSymbols(popular);
  };

  const allSelectableMarketSymbols = useMemo(
    () => selectAllMarketSymbols(allSymbols),
    [allSymbols],
  );

  const selectAllDraftMarkets = () => {
    setSelectedCategory('All');
    setMarketDraftSymbols(allSelectableMarketSymbols);
  };

  const clearAllDraftMarkets = () => {
    setMarketDraftSymbols([]);
  };

  // Category filter
  const filteredRecs = useMemo(() => {
    const allowed = strategyAllowedSymbols.length > 0 ? new Set(strategyAllowedSymbols) : null;
    const selectedScope = dashboardScopeSymbols.length > 0
      ? new Set(dashboardScopeSymbols.map((sym) => marketIdentityKey(sym)).filter(Boolean))
      : null;
    const scoped = recommendations.filter((r) => {
      const upper = String(r.symbol || '').trim().toUpperCase();
      if (allowed && !allowed.has(upper)) return false;
      if (selectedScope && !selectedScope.has(marketIdentityKey(r.symbol))) return false;
      return true;
    });
    return selectedCategory === 'All'
      ? scoped
      : scoped.filter((r) => normalizeCategoryName(r.category) === selectedCategory);
  }, [selectedCategory, recommendations, strategyAllowedSymbols, dashboardScopeSymbols]);
  const filteredOrderByIdentity = useMemo(() => {
    const order = new Map<string, number>();
    filteredRecs.forEach((rec, index) => {
      const identity = marketIdentityKey(rec.symbol);
      if (identity && !order.has(identity)) order.set(identity, index);
    });
    return order;
  }, [filteredRecs]);
  const stableMarketOrder = useCallback((a: Recommendation, b: Recommendation) => {
    const aClosed = isClosedMarketRecommendation(a);
    const bClosed = isClosedMarketRecommendation(b);
    if (aClosed !== bClosed) return aClosed ? 1 : -1;
    const aOrder = filteredOrderByIdentity.get(marketIdentityKey(a.symbol)) ?? Number.MAX_SAFE_INTEGER;
    const bOrder = filteredOrderByIdentity.get(marketIdentityKey(b.symbol)) ?? Number.MAX_SAFE_INTEGER;
    if (aOrder !== bOrder) return aOrder - bOrder;
    return marketIdentityKey(a.symbol).localeCompare(marketIdentityKey(b.symbol));
  }, [filteredOrderByIdentity]);
  const actionableRecs = useMemo(
    () => filteredRecs
      .filter((r) => r.signal.action === 'BUY' || r.signal.action === 'SELL')
      .sort(stableMarketOrder),
    [filteredRecs, stableMarketOrder],
  );
  const marketRows = useMemo(
    () => [...filteredRecs].sort(stableMarketOrder),
    [filteredRecs, stableMarketOrder],
  );
  const visibleMarketRecs = useMemo(
    () => marketRows.slice(0, visibleMarketRows),
    [marketRows, visibleMarketRows],
  );
  const remainingMarketRows = Math.max(0, marketRows.length - visibleMarketRecs.length);

  useEffect(() => {
    setVisibleMarketRows(60);
  }, [selectedCategory, recommendations.length, dashboardScopeSymbols.length]);

  // Count categories for tabs
  const categoryCounts: Record<string, number> = { All: recommendations.length };
  recommendations.forEach((r) => {
    const cat = normalizeCategoryName(r.category);
    categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
  });
  const categoryOrder = ['All', 'Stocks', 'Crypto', 'Indices', 'Commodities', 'Forex', 'Other'];
  const availableCategories = categoryOrder.filter((c) => (categoryCounts[c] || 0) > 0);

  const preferredTickSymbolByIdentity = useMemo(() => {
    const byIdentity = new Map<string, AvailableSymbolRow>();
    for (const row of allSymbols) {
      const identity = marketIdentityKey(row.name);
      if (!identity) continue;
      const current = byIdentity.get(identity);
      if (!current) {
        byIdentity.set(identity, row);
        continue;
      }
      const rowTrade = Boolean(row.trade_enabled);
      const curTrade = Boolean(current.trade_enabled);
      if (rowTrade !== curTrade) {
        if (rowTrade) byIdentity.set(identity, row);
        continue;
      }
      const rowVisible = Boolean(row.visible);
      const curVisible = Boolean(current.visible);
      if (rowVisible !== curVisible) {
        if (rowVisible) byIdentity.set(identity, row);
        continue;
      }
      const rowSpread = Number.isFinite(Number(row.spread)) ? Number(row.spread) : Number.POSITIVE_INFINITY;
      const curSpread = Number.isFinite(Number(current.spread)) ? Number(current.spread) : Number.POSITIVE_INFINITY;
      if (rowSpread < curSpread) {
        byIdentity.set(identity, row);
      }
    }
    const out: Record<string, string> = {};
    for (const [identity, row] of byIdentity.entries()) {
      out[identity] = String(row.name || '').trim() || identity;
    }
    return out;
  }, [allSymbols]);

  const liveTickSymbols = useMemo(() => {
    const opened = positions.map((p) => p.symbol);
    const actionableVisible = actionableRecs.map((r) => r.symbol);
    const marketVisible = visibleMarketRecs.map((r) => r.symbol);
    const rawSymbols = [...opened, ...actionableVisible, ...marketVisible]
      .map((s) => String(s || '').trim())
      .filter(Boolean);
    const unique: string[] = [];
    const seen = new Set<string>();
    for (const raw of rawSymbols) {
      const rawKey = symbolKey(raw);
      if (rawKey && !seen.has(rawKey)) {
        seen.add(rawKey);
        unique.push(raw);
      }
      const identity = marketIdentityKey(raw);
      const preferred = preferredTickSymbolByIdentity[identity];
      const preferredKey = symbolKey(preferred);
      if (preferred && preferredKey && !seen.has(preferredKey)) {
        seen.add(preferredKey);
        unique.push(preferred);
      }
    }
    return unique.slice(0, 120);
  }, [actionableRecs, visibleMarketRecs, visibleMarketRows, positions, preferredTickSymbolByIdentity]);
  const liveTickSymbolsKey = useMemo(
    () => liveTickSymbols.map((symbol) => symbolKey(symbol)).filter(Boolean).join('|'),
    [liveTickSymbols],
  );

  const liveTickBySymbol = useMemo(() => {
    const out: Record<string, TickData> = {};
    for (const [rawSymbol, tick] of Object.entries(liveTicks || {})) {
      if (!tick) continue;
      if (!isUsableLiveTick(tick)) continue;
      const rawKey = symbolKey(rawSymbol);
      const tickKey = symbolKey((tick as any).symbol);
      const identity = marketIdentityKey(rawSymbol);
      if (rawKey) out[rawKey] = tick;
      if (tickKey) out[tickKey] = tick;
      if (identity && !out[identity]) out[identity] = tick;
    }
    return out;
  }, [liveTicks]);

  const getLiveTickForSymbol = useCallback((symbol: string | null | undefined) => {
    const rawKey = symbolKey(symbol);
    const identity = marketIdentityKey(symbol);
    return (rawKey ? liveTickBySymbol[rawKey] : undefined) || (identity ? liveTickBySymbol[identity] : undefined);
  }, [liveTickBySymbol]);
  const totalProfit = useMemo(
    () => positions.reduce((sum, p) => {
      const tick = getLiveTickForSymbol(p.symbol);
      return sum + positionDisplayProfit(p, tick);
    }, 0),
    [positions, getLiveTickForSymbol],
  );
  const winningTrades = useMemo(
    () => positions.filter((p) => {
      const tick = getLiveTickForSymbol(p.symbol);
      return positionDisplayProfit(p, tick) > 0;
    }).length,
    [positions, getLiveTickForSymbol],
  );
  const losingTrades = useMemo(
    () => positions.filter((p) => {
      const tick = getLiveTickForSymbol(p.symbol);
      return positionDisplayProfit(p, tick) < 0;
    }).length,
    [positions, getLiveTickForSymbol],
  );

  // Live prices in one bulk request so bid/ask updates feel instant without request storms.
  useEffect(() => {
    if (!connected) {
      setLiveTicks({});
      return;
    }
    if (liveTickSymbols.length === 0) {
      setLiveTicks({});
      return;
    }
    let cancelled = false;
    let inFlight = false;
    let fallbackTimer: ReturnType<typeof setInterval> | null = null;
    let safetyPollTimer: ReturnType<typeof setInterval> | null = null;
    let eventSource: EventSource | null = null;
    let streamUrlIndex = 0;
    const streamUrls = [
      buildTickStreamUrl(liveTickSymbols),
      alternateLocalTickStreamUrl(liveTickSymbols),
    ].filter((url): url is string => Boolean(url));
    const commitTicks = (ticks: Record<string, TickData>) => {
      if (cancelled) return;
      setLiveTicks((prev) => {
        const merged = mergeLiveTicks(prev, ticks);
        return hasTickPayloadChanged(prev, merged) ? merged : prev;
      });
    };

    const scheduleTickCommit = (ticks: Record<string, TickData>) => {
      if (cancelled) return;
      commitTicks(ticks);
    };

    const refreshTicks = async () => {
      if (inFlight) return;
      inFlight = true;
      const chunkSize = 4;
      const chunks: string[][] = [];
      for (let i = 0; i < liveTickSymbols.length; i += chunkSize) {
        chunks.push(liveTickSymbols.slice(i, i + chunkSize));
      }
      let remaining = chunks.length;
      if (remaining === 0) {
        inFlight = false;
        return;
      }
      const finishChunk = () => {
        remaining -= 1;
        if (remaining <= 0) {
          inFlight = false;
        }
      };
      chunks.forEach((chunk) => {
        api.getTicks(chunk)
          .then((resp) => {
            if (!cancelled) {
              scheduleTickCommit(resp.ticks || {});
            }
          })
          .catch(() => {
            // keep previous ticks on transient errors
          })
          .finally(finishChunk);
      });
    };

    const refreshTicksBlocking = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const resp = await api.getTicks(liveTickSymbols.slice(0, 4));
        if (!cancelled) scheduleTickCommit(resp.ticks || {});
      } catch {
        // keep previous ticks on transient errors
      } finally {
        inFlight = false;
      }
    };

    const startFallbackPolling = () => {
      if (fallbackTimer) return;
      void refreshTicksBlocking();
      const fallbackMs = 250;
      fallbackTimer = setInterval(refreshTicks, fallbackMs);
    };

    const openStream = () => {
      if (cancelled || streamUrlIndex >= streamUrls.length) {
        startFallbackPolling();
        return;
      }
      eventSource = new EventSource(streamUrls[streamUrlIndex]);
      eventSource.onmessage = (event) => {
        if (cancelled) return;
        try {
          const payload = JSON.parse(event.data || '{}') as { ticks?: Record<string, TickData> };
          scheduleTickCommit(payload.ticks || {});
        } catch {
          // ignore malformed stream frames and keep the stream alive
        }
      };
      eventSource.onerror = () => {
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
        streamUrlIndex += 1;
        openStream();
      };
    };

    try {
      openStream();
      void refreshTicks();
      safetyPollTimer = setInterval(refreshTicks, 500);
    } catch {
      startFallbackPolling();
    }

    return () => {
      cancelled = true;
      if (eventSource) eventSource.close();
      if (fallbackTimer) clearInterval(fallbackTimer);
      if (safetyPollTimer) clearInterval(safetyPollTimer);
    };
  }, [connected, liveTickSymbolsKey]);

  // Not connected state
  if (!connected) {
    if (reconnecting) {
      return (
        <div>
          <ConnectionBar status={status} onRefresh={onRefresh} reconnecting />
          <div style={{
            textAlign: 'center',
            padding: '80px 40px',
            maxWidth: 500,
            margin: '0 auto',
          }}>
            <div className="loading-spinner" style={{ width: 34, height: 34, margin: '0 auto 14px auto' }} />
            <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>Reconnecting to your account...</h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.7 }}>
              Restoring your MT5 session automatically. Please wait a few seconds.
            </p>
          </div>
        </div>
      );
    }
    return (
      <div>
        <ConnectionBar status={status} onRefresh={onRefresh} />
        <div style={{
          textAlign: 'center',
          padding: '80px 40px',
          maxWidth: 500,
          margin: '0 auto',
        }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>&#128202;</div>
          <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>Welcome to LinkTrade</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.7, marginBottom: 24 }}>
            This app uses AI to analyze currency and commodity markets, then gives you
            simple buy/sell recommendations. You just click a button to trade.
          </p>
          {connectError && (
            <div className="error-banner" style={{ marginBottom: 16, textAlign: 'left', fontSize: 12 }}>
              {connectError}
            </div>
          )}
          <div style={{
            background: 'var(--bg-secondary)',
            borderRadius: 12,
            padding: '24px',
            textAlign: 'left',
            marginBottom: 24,
          }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>How it works:</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {[
                { step: '1', text: 'Connect your MT5 account' },
                { step: '2', text: 'The AI scans markets automatically' },
                { step: '3', text: 'You see clear Buy/Sell recommendations' },
                { step: '4', text: 'Click "Buy Now" or "Sell Now" to trade' },
                { step: '5', text: 'The app protects you with automatic stop-loss' },
              ].map((item) => (
                <div key={item.step} className="flex items-center gap-3" style={{ fontSize: 13 }}>
                  <div style={{
                    width: 28, height: 28, borderRadius: '50%',
                    background: 'var(--accent-blue)', color: 'white',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 12, fontWeight: 700, flexShrink: 0,
                  }}>
                    {item.step}
                  </div>
                  <span>{item.text}</span>
                </div>
              ))}
            </div>
          </div>
          <button
            className="btn btn-primary"
            style={{ fontSize: 15, padding: '14px 32px' }}
            onClick={handleGetStartedConnect}
            disabled={connectLoading}
          >
            {connectLoading ? <span className="loading-spinner" /> : 'Get Started - Connect Account'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <ConnectionBar status={status} onRefresh={onRefresh} />

      {(localReconciliation || reconcileError) && (
        <div
          className="card"
          style={{
            marginTop: 12,
            padding: 12,
            borderColor: localReconciliation?.issue_count
              ? 'rgba(245, 158, 11, 0.45)'
              : 'rgba(34, 197, 94, 0.28)',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          <div className="flex justify-between items-center" style={{ gap: 12, flexWrap: 'wrap' }}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 700 }}>
                Reconciliation
                {localReconciliation ? (
                  <span style={{
                    marginLeft: 8,
                    color: localReconciliation.issue_count > 0 ? 'var(--accent-yellow)' : 'var(--accent-green)',
                  }}>
                    {localReconciliation.status}
                  </span>
                ) : null}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                {localReconciliation
                  ? `${localReconciliation.issue_count} issue(s), ${localReconciliation.local_open_count} local open, ${localReconciliation.broker_open_count} broker open`
                  : 'Runtime state has not been reconciled yet.'}
              </div>
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => void runReconciliation()}
              disabled={reconcileLoading}
              title="Compare local orders with open MT5 positions"
            >
              {reconcileLoading ? <span className="loading-spinner" /> : <RefreshCw size={13} />}
              Reconcile
            </button>
          </div>
          {reconcileError && (
            <div style={{ fontSize: 12, color: 'var(--accent-red)' }}>{reconcileError}</div>
          )}
          {localReconciliation?.issues?.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {localReconciliation.issues.slice(0, 3).map((issue, index) => (
                <div key={`${issue.code}-${issue.local_ticket || issue.broker_ticket || index}`} style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                  <strong>{issue.code}</strong>: {issue.message}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}

      {/* Account Summary */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginTop: 16 }}>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Balance</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.balance.toFixed(2)}
          </div>
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Equity (Live)</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.equity.toFixed(2)}
          </div>
          {account && account.equity !== account.balance && (
            <div style={{
              fontSize: 11, fontWeight: 600, marginTop: 2,
              color: account.equity >= account.balance ? 'var(--accent-green)' : 'var(--accent-red)',
            }}>
              {account.equity >= account.balance ? '+' : ''}{(account.equity - account.balance).toFixed(2)}
            </div>
          )}
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Margin Used</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace', color: (account?.margin || 0) > 0 ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
            ${account?.margin?.toFixed(2) || '0.00'}
          </div>
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Free Margin</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.free_margin?.toFixed(2) || '0.00'}
          </div>
          {account && account.leverage > 0 && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
              Leverage: 1:{account.leverage}
            </div>
          )}
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Active Trades</div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{positions.length}</div>
          {positions.length > 0 && (
            <div style={{
              fontSize: 11, fontWeight: 600, marginTop: 2,
              color: totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
            }}>
              {totalProfit >= 0 ? '+' : ''}{totalProfit.toFixed(2)} {currency}
            </div>
          )}
        </div>
      </div>
      {/* Balance explanation */}
      {positions.length > 0 && account && account.equity !== account.balance && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6, textAlign: 'center' }}>
          Balance stays the same while trades are open. Equity = Balance + open trade profits/losses. Free Margin = what you can still use.
        </div>
      )}

      <div className="card mt-4" style={{ padding: '10px 14px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Gemini</div>
            <div style={{ fontWeight: 700, fontSize: 13, color: geminiState.state === 'cooldown' || geminiState.state === 'ready' || geminiState.degraded ? 'var(--accent-yellow)' : geminiState.available ? 'var(--accent-green)' : 'var(--accent-red)' }}>
              {geminiState.state === 'cooldown'
                ? 'Cooldown'
                : geminiState.state === 'ready'
                  ? 'Ready'
                : geminiState.degraded
                  ? 'Degraded'
                  : geminiState.available
                    ? 'Online'
                    : 'Offline'}
            </div>
          </div>
          <div style={{ minWidth: 220, flex: 1, maxWidth: 360 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)' }}>
              <span>AI health</span>
              <span>{Math.max(0, Math.min(100, Number(geminiState.credits_pct || 0))).toFixed(0)}%</span>
            </div>
            <div style={{ marginTop: 4, height: 6, borderRadius: 999, background: 'var(--bg-primary)', overflow: 'hidden' }}>
              <div
                style={{
                  width: `${Math.max(0, Math.min(100, Number(geminiState.credits_pct || 0))).toFixed(0)}%`,
                  height: '100%',
                  background: (geminiState.credits_pct || 0) >= 60
                    ? 'var(--accent-green)'
                    : (geminiState.credits_pct || 0) >= 25
                      ? 'var(--accent-yellow)'
                      : 'var(--accent-red)',
                  transition: 'width 220ms ease',
                }}
              />
            </div>
          </div>
        </div>
        {(Number(geminiState.cooldown_seconds || 0) > 0 || !!geminiErrorSummary) && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>
            {Number(geminiState.cooldown_seconds || 0) > 0 ? `Cooldown: ${formatCooldown(geminiState.cooldown_seconds)}. ` : ''}
            {geminiErrorSummary || ''}
          </div>
        )}
        {(geminiState.primary_model || geminiState.active_model) && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>
            Primary: <span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace' }}>{geminiState.primary_model || geminiState.active_model}</span>
            {geminiState.active_model && geminiState.active_model !== geminiState.primary_model && (
              <>
                {' '}| active fallback:{' '}
                <span style={{ color: 'var(--accent-amber)', fontFamily: 'monospace' }}>{geminiState.active_model}</span>
              </>
            )}
          </div>
        )}

        {strategies.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8 }}>Strategy</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {strategies.map((strategy, idx) => {
              const selected = strategy.id === activeStrategyId || strategy.is_selected;
              const disabled = strategyLoadingId === strategy.id;
              return (
                <div key={strategy.id} style={{ display: 'inline-flex', alignItems: 'center' }}>
                  <button
                    className="btn"
                    onClick={() => handleSelectStrategy(strategy.id)}
                    disabled={disabled}
                    title={strategy.description || strategy.name}
                    style={{
                      fontSize: 13,
                      padding: '8px 16px',
                      borderRadius: 12,
                      border: selected ? '1px solid var(--accent-blue)' : '1px solid var(--border)',
                      background: selected ? 'var(--accent-blue)' : 'var(--bg-secondary)',
                      color: selected ? '#fff' : 'var(--text-primary)',
                      fontWeight: 600,
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <span>{idx === 0 ? `${strategy.name} (Default)` : strategy.name}</span>
                    <span
                      role="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        setStrategyInfo(strategy);
                        setStrategyInfoOpen(true);
                      }}
                      title={`About ${strategy.name}`}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        opacity: 0.9,
                        cursor: 'pointer',
                      }}
                    >
                      <HelpCircle size={14} />
                    </span>
                  </button>
                </div>
              );
            })}
              <button
                className="btn"
                onClick={() => navigate('/strategy')}
                style={{
                  fontSize: 13,
                  padding: '8px 16px',
                  borderRadius: 12,
                  border: '1px solid var(--border)',
                  background: 'var(--bg-secondary)',
                  color: 'var(--text-primary)',
                  fontWeight: 600,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                <Plus size={14} /> Add Strategy
              </button>
            </div>
          </div>
        )}
      </div>

      {strategyInfoOpen && strategyInfo && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.45)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1200,
            padding: 20,
          }}
          onClick={() => setStrategyInfoOpen(false)}
        >
          <div
            className="card"
            style={{ width: 'min(560px, 92vw)', padding: 18 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex justify-between items-center" style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 16, fontWeight: 700 }}>{strategyInfo.name}</div>
              <button className="btn btn-secondary btn-sm" onClick={() => setStrategyInfoOpen(false)}>
                Close
              </button>
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-line' }}>
              {strategyInfo.brief || strategyInfo.description || 'No strategy brief available.'}
            </div>
          </div>
        </div>
      )}

      {/* Panic Stop */}
      {status?.panic_stop && (
        <div style={{
          background: 'rgba(239, 68, 68, 0.1)',
          border: '1px solid rgba(239, 68, 68, 0.3)',
          borderRadius: 8,
          padding: '16px 20px',
          marginTop: 16,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div className="flex items-center gap-3">
            <AlertOctagon size={20} style={{ color: 'var(--accent-red)' }} />
            <div>
              <div style={{ fontWeight: 600, color: 'var(--accent-red)', fontSize: 14 }}>Trading Paused</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>All trading is stopped. Click Resume when ready.</div>
            </div>
          </div>
          <button className="btn btn-success" onClick={handlePanicStop} style={{ fontSize: 14, padding: '10px 24px' }}>
            Resume Trading
          </button>
        </div>
      )}

      {/* Auto-Trade Panel */}
      <div className="card mt-4" style={{
        borderLeft: `4px solid ${autoTradeRunning ? 'var(--accent-green)' : 'var(--border)'}`,
        padding: '16px 20px',
      }}>
        <div className="flex justify-between items-center">
          <div className="flex items-center gap-3">
            <Zap size={20} style={{ color: autoTradeRunning ? 'var(--accent-green)' : 'var(--text-muted)' }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 15 }}>
                Auto-Trading {autoTradeRunning ? 'Active' : 'Off'}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {autoTradeRunning
            ? `AI is scanning, trading & managing positions autonomously${posManagerRunning ? ' - Position Manager active' : ''}`
                  : 'Turn on to let the AI trade and manage positions for you'
                }
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>
                {autoTradeBlockedReason ? `Blocked: ${autoTradeBlockedReason}` : autoTradeBudgetSummary}
              </div>
              {!autoTradeRunning && status?.user_policy?.auto_trading_allowed === false && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={{ marginTop: 8, padding: '6px 10px', fontSize: 12 }}
                  onClick={() => navigate('/risk')}
                >
                  Open Risk Management
                </button>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {autoTradeRunning && (
              <button
                className="btn btn-secondary"
                onClick={openAutoTradeBudgetModal}
                disabled={autoTradeLoading}
                style={{ fontSize: 13, padding: '9px 16px', fontWeight: 600 }}
              >
                Edit Budget
              </button>
            )}
            <button
              className={'btn ' + (autoTradeRunning ? 'btn-danger' : 'btn-success')}
              onClick={toggleAutoTrade}
              disabled={autoTradeLoading || (!autoTradeRunning && (!!status?.panic_stop || !!autoTradeBlockedReason))}
              style={{ fontSize: 14, padding: '10px 24px', fontWeight: 600 }}
              title={!autoTradeRunning && autoTradeBlockedReason ? autoTradeBlockedReason : undefined}
            >
              {autoTradeLoading ? (
                <span className="loading-spinner" />
              ) : autoTradeRunning ? (
                <><Power size={14} /> Turn Off</>
              ) : (
                <><Zap size={14} /> Turn On</>
              )}
            </button>
          </div>
        </div>

        {/* Auto-trade activity log */}
        {autoTradeLogs.length > 0 && (
          <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--text-muted)' }}>
              AI Brain Activity
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 150, overflowY: 'auto' }}>
              {autoTradeLogs.slice().reverse().slice(0, 10).map((log, i) => {
                const key = `${log.timestamp}-${log.symbol}-${log.action}-${i}`;
                const fullText = compactAutoTradeLogText(log);
                const needsReadMore = fullText.length > AUTO_TRADE_LOG_PREVIEW_CHARS;
                const expanded = expandedAutoTradeLogs.has(key);
                const shownText = needsReadMore && !expanded
                  ? `${fullText.slice(0, AUTO_TRADE_LOG_PREVIEW_CHARS).trimEnd()}...`
                  : fullText;
                const logColor = autoTradeLogColor(log);
                return (
                  <div key={key} className="flex items-start gap-2" style={{ fontSize: 12, padding: '4px 0' }}>
                    {log.success === true ? (
                      <CheckCircle size={12} style={{ color: 'var(--accent-green)', flexShrink: 0 }} />
                    ) : log.success === false ? (
                      <XCircle size={12} style={{ color: 'var(--accent-red)', flexShrink: 0 }} />
                    ) : (
                      <Clock size={12} style={{ color: 'var(--accent-blue)', flexShrink: 0 }} />
                    )}
                    <span style={{ fontWeight: 600, flexShrink: 0, color: logColor }}>
                      {getSymbolEmoji(log.symbol)} {log.action} {getSymbolName(log.symbol)}
                    </span>
                    <span style={{ color: logColor, flex: 1, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                      {shownText}
                      {needsReadMore && (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => {
                            setExpandedAutoTradeLogs((prev) => {
                              const next = new Set(prev);
                              if (next.has(key)) next.delete(key);
                              else next.add(key);
                              return next;
                            });
                          }}
                          style={{ marginLeft: 6, padding: '1px 6px', fontSize: 10 }}
                        >
                          {expanded ? 'Show less' : 'Read more'}
                        </button>
                      )}
                    </span>
                    <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11, flexShrink: 0 }}>
                      {new Date(log.timestamp * 1000).toLocaleTimeString()}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {autoTradeRunning && autoTradeLogs.length === 0 && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-muted)', textAlign: 'center', padding: 8 }}>
            <span className="loading-spinner" style={{ width: 12, height: 12, marginRight: 6 }} />
            Waiting for next market scan...
          </div>
        )}
      </div>

      {/* Active Trades */}
      {positions.length > 0 && (
        <div className="mt-4">
          {/* Portfolio Summary Card */}
          {(() => {
            const totalStartedAmount = positions.reduce((sum, p) => sum + positionStartedAmount(p, account?.leverage || 100), 0);
            const profitPct = totalStartedAmount > 0 ? (totalProfit / totalStartedAmount * 100) : 0;
            const profitColor = totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
            const winning = winningTrades;
            const losing = losingTrades;
            const totalPositionMarginUsed = positions.reduce((sum, p) => {
              const value = positionMarginUsedAmount(p, account?.leverage || 100);
              if (Number.isFinite(value) && value > 0) return sum + value;
              return sum;
            }, 0);
            const brokerMarginUsed = Math.max(0, Number(account?.margin || 0));
            const marginUsedDisplay = brokerMarginUsed > 0 ? brokerMarginUsed : totalPositionMarginUsed;
            const customBudget = activeAutoTradeBudgetMode === 'custom_budget'
              ? Math.max(0, Number(activeAutoTradeBudgetUsd || 0))
              : 0;
            const fullAccountBudget = Math.max(0, Number(account?.balance || account?.equity || 0));
            const marginBase = customBudget > 0 ? customBudget : fullAccountBudget;
            const marginUsedPctRaw = marginBase > 0 ? (marginUsedDisplay / marginBase) * 100 : 0;
            const marginUsedPct = Math.max(0, Math.min(999, marginUsedPctRaw));
            const marginUsedPctLabel = customBudget > 0 ? 'of budget' : 'of account budget';
            const overBudget = customBudget > 0 && marginUsedDisplay > customBudget;

            return (
              <div className="card" style={{ padding: '16px 20px', marginBottom: 12, borderLeft: `3px solid ${profitColor}` }}>
                {/* Row 1: Title + Sell All button */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                  <h3 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                    Your Active Trades
                    <span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-muted)', marginLeft: 8 }}>
                      ({positions.length})
                    </span>
                  </h3>
                  <button
                    className="btn"
                    onClick={handleSellAll}
                    disabled={closingAll}
                    style={{
                      fontSize: 12, padding: '6px 16px',
                      background: 'var(--accent-red)', color: '#fff',
                      border: 'none', borderRadius: 6, cursor: 'pointer',
                      opacity: closingAll ? 0.6 : 1,
                    }}
                  >
                    {closingAll ? 'Closing...' : `Sell All (${positions.length})`}
                  </button>
                </div>

                {/* Row 2: Combined P/L summary */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Combined P/L</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
                      {totalProfit >= 0 ? '+' : ''}{totalProfit.toFixed(2)}
                    </div>
                    <div style={{ fontSize: 11, color: profitColor, fontFamily: 'monospace' }}>
                      {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                    </div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Winning</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: 'var(--accent-green)' }}>{winning}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>trades</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Losing</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: 'var(--accent-red)' }}>{losing}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>trades</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Win Rate</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace' }}>
                      {positions.length > 0 ? Math.round(winning / positions.length * 100) : 0}%
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>ratio</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Total Margin Used</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace' }}>
                      ${marginUsedDisplay.toFixed(2)}
                    </div>
                    <div style={{ fontSize: 11, color: overBudget ? 'var(--accent-red)' : 'var(--text-muted)' }}>
                      {marginUsedPct.toFixed(1)}% {marginUsedPctLabel}{overBudget ? ' (over budget)' : ''}
                    </div>
                  </div>
                </div>

                {/* P/L Progress Bar */}
                {(portfolioSL !== null || portfolioTP !== null) && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>
                      <span style={{ color: 'var(--accent-red)' }}>SL: -${Math.abs(portfolioSL || 0)}</span>
                      <span style={{ fontWeight: 600 }}>${totalProfit.toFixed(2)}</span>
                      <span style={{ color: 'var(--accent-green)' }}>TP: +${Math.abs(portfolioTP || 0)}</span>
                    </div>
                    <div style={{ height: 8, background: 'var(--bg-tertiary)', borderRadius: 4, overflow: 'hidden', position: 'relative' }}>
                      {(() => {
                        const sl = Math.abs(portfolioSL || 500);
                        const tp = Math.abs(portfolioTP || 500);
                        const range = sl + tp;
                        const pos = ((totalProfit + sl) / range) * 100;
                        const clamped = Math.max(0, Math.min(100, pos));
                        return (
                          <>
                            {/* SL zone (red, left half) */}
                            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${(sl / range) * 100}%`, background: 'rgba(239,68,68,0.15)' }} />
                            {/* TP zone (green, right half) */}
                            <div style={{ position: 'absolute', right: 0, top: 0, bottom: 0, width: `${(tp / range) * 100}%`, background: 'rgba(34,197,94,0.15)' }} />
                            {/* Current position marker */}
                            <div style={{
                              position: 'absolute', left: `${clamped}%`, top: -2, width: 4, height: 12, borderRadius: 2,
                              background: totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                              transform: 'translateX(-50%)',
                              transition: 'left 0.5s ease',
                            }} />
                            {/* Center line (breakeven) */}
                            <div style={{ position: 'absolute', left: `${(sl / range) * 100}%`, top: 0, bottom: 0, width: 1, background: 'var(--text-muted)', opacity: 0.4 }} />
                          </>
                        );
                      })()}
                    </div>
                  </div>
                )}

                {/* Portfolio SL/TP Controls */}
                {editingPortfolioTargets ? (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ fontSize: 11, color: 'var(--accent-red)', fontWeight: 600 }}>SL $</span>
                      <input
                        type="number" value={tempSL} onChange={(e) => setTempSL(e.target.value)}
                        placeholder="e.g. 500"
                        style={{ width: 80, padding: '4px 8px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-tertiary)', color: 'var(--text-primary)' }}
                      />
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ fontSize: 11, color: 'var(--accent-green)', fontWeight: 600 }}>TP $</span>
                      <input
                        type="number" value={tempTP} onChange={(e) => setTempTP(e.target.value)}
                        placeholder="e.g. 1000"
                        style={{ width: 80, padding: '4px 8px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-tertiary)', color: 'var(--text-primary)' }}
                      />
                    </div>
                    <button
                      onClick={async () => {
                        const nextSL = tempSL ? Math.abs(parseFloat(tempSL)) : null;
                        const nextTP = tempTP ? Math.abs(parseFloat(tempTP)) : null;
                        setSavingPortfolioTargets(true);
                        try {
                          await api.setPortfolioTargets({
                            stop_loss_usd: Number.isFinite(nextSL as number) ? nextSL : null,
                            take_profit_usd: Number.isFinite(nextTP as number) ? nextTP : null,
                            mode: 'manual',
                          });
                          setPortfolioSL(nextSL);
                          setPortfolioTP(nextTP);
                          setEditingPortfolioTargets(false);
                          onRefresh();
                        } catch (error) {
                          alert(formatUiError(error));
                        } finally {
                          setSavingPortfolioTargets(false);
                        }
                      }}
                      disabled={savingPortfolioTargets}
                      style={{ fontSize: 11, padding: '4px 12px', background: 'var(--accent-blue)', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer' }}
                    >
                      {savingPortfolioTargets ? 'Saving...' : 'Set'}
                    </button>
                    <button
                      onClick={() => { setEditingPortfolioTargets(false); }}
                      style={{ fontSize: 11, padding: '4px 8px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer' }}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11 }}>
                    <span style={{ color: 'var(--text-muted)', background: 'rgba(59,130,246,0.08)', padding: '2px 8px', borderRadius: 4 }}>
                      {status?.portfolio_targets?.mode === 'manual' ? 'Manual portfolio target' : 'AI vision target'}
                    </span>
                    {portfolioSL !== null && (
                      <span style={{ color: 'var(--accent-red)', background: 'rgba(239,68,68,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Stop Loss: -${Math.abs(portfolioSL)} {totalProfit <= -Math.abs(portfolioSL) * 0.8 ? 'Near stop' : ''}
                      </span>
                    )}
                    {portfolioTP !== null && (
                      <span style={{ color: 'var(--accent-green)', background: 'rgba(34,197,94,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Take Profit: +${Math.abs(portfolioTP)} {totalProfit >= Math.abs(portfolioTP) * 0.8 ? 'Near target' : ''}
                      </span>
                    )}
                    <button
                      onClick={() => {
                        setTempSL(portfolioSL ? String(Math.abs(portfolioSL)) : '');
                        setTempTP(portfolioTP ? String(Math.abs(portfolioTP)) : '');
                        setEditingPortfolioTargets(true);
                      }}
                      style={{ fontSize: 11, padding: '2px 10px', background: 'transparent', color: 'var(--accent-blue)', border: '1px solid var(--accent-blue)', borderRadius: 4, cursor: 'pointer' }}
                    >
                      {portfolioSL !== null || portfolioTP !== null ? 'Edit Targets' : 'Set Portfolio SL/TP'}
                    </button>
                    {(portfolioSL !== null || portfolioTP !== null) && (
                      <button
                        onClick={async () => {
                          setSavingPortfolioTargets(true);
                          try {
                            await api.setPortfolioTargets({ stop_loss_usd: null, take_profit_usd: null, mode: 'auto' });
                            setPortfolioSL(null);
                            setPortfolioTP(null);
                            onRefresh();
                          } catch (error) {
                            alert(formatUiError(error));
                          } finally {
                            setSavingPortfolioTargets(false);
                          }
                        }}
                        style={{ fontSize: 11, padding: '2px 8px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer' }}
                      >
                        Clear
                      </button>
                    )}
                    {status?.portfolio_targets?.close_inflight && (
                      <span style={{ color: 'var(--accent-orange)', background: 'rgba(245,158,11,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Closing active trades...
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })()}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {positions.map((p) => (
              <PositionCard
                key={p.ticket}
                position={p}
                currency={currency}
                onClose={handleClosePosition}
                onCancelClose={handleCancelPositionClose}
                onModify={handleModifyPosition}
                leverage={account?.leverage || 100}
                liveTick={getLiveTickForSymbol(p.symbol)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Available Markets */}
      <div className="mt-4">
        <div className="flex justify-between items-center mb-3">
          <div>
            <h3 style={{ fontSize: 16, fontWeight: 600 }}>Available Markets</h3>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              {lastScan
                ? `MT5 available ${availableMarketCount} symbols \u2022 AI analyzed ${analyzedCount} \u2022 Last checked: ${new Date(lastScan * 1000).toLocaleTimeString()}`
                : 'Click "Scan All" to analyze every available market'
              }
            </p>
          </div>
          <div className="flex gap-2">
            <button
              className="btn btn-secondary"
              onClick={() => setShowMarketSelector(true)}
              style={{ fontSize: 12 }}
            >
              Add Markets
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => setShowMarketSelector(true)}
              style={{ fontSize: 12 }}
            >
              Edit Markets
            </button>
            <button className="btn btn-primary" onClick={() => void scanMarkets(undefined, { manual: true })} disabled={scanning} style={{ fontSize: 13, padding: '10px 20px' }}>
              {scanning ? <span className="loading-spinner" /> : <RefreshCw size={14} />}
              {scanning ? 'Analyzing...' : 'Scan All'}
            </button>
            {!status?.panic_stop ? (
              <button className="btn btn-secondary" onClick={handlePanicStop} style={{ fontSize: 12, color: 'var(--accent-red)' }}>
                <AlertOctagon size={12} />
                Stop All
              </button>
            ) : null}
          </div>
        </div>

        {showAutoTradeBudgetModal && (
          <div
            onClick={() => !autoTradeLoading && setShowAutoTradeBudgetModal(false)}
            style={{
              position: 'fixed',
              inset: 0,
              background: 'rgba(0, 0, 0, 0.55)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 9998,
              padding: 16,
            }}
          >
            <div
              className="card"
              onClick={(e) => e.stopPropagation()}
              style={{
                width: 'min(520px, 96vw)',
                padding: 18,
                display: 'flex',
                flexDirection: 'column',
                gap: 14,
              }}
            >
              <div>
                <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 6 }}>
                  Auto-trade budget
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.5 }}>
                  Choose whether auto-trading should use your whole account margin or behave like it only has a smaller wallet.
                </div>
              </div>

              <label
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'flex-start',
                  padding: 12,
                  border: `1px solid ${autoTradeBudgetMode === 'full_margin' ? 'var(--accent-green)' : 'var(--border)'}`,
                  borderRadius: 10,
                  cursor: 'pointer',
                }}
              >
                <input
                  type="radio"
                  name="auto-trade-budget"
                  checked={autoTradeBudgetMode === 'full_margin'}
                  onChange={() => setAutoTradeBudgetMode('full_margin')}
                />
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Use full margin</div>
                  <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
                    Auto-trading sizes positions from the full connected account.
                  </div>
                </div>
              </label>

              <label
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'flex-start',
                  padding: 12,
                  border: `1px solid ${autoTradeBudgetMode === 'custom_budget' ? 'var(--accent-green)' : 'var(--border)'}`,
                  borderRadius: 10,
                  cursor: 'pointer',
                }}
              >
                <input
                  type="radio"
                  name="auto-trade-budget"
                  checked={autoTradeBudgetMode === 'custom_budget'}
                  onChange={() => setAutoTradeBudgetMode('custom_budget')}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Use custom budget</div>
                  <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, marginBottom: 8 }}>
                    Example: with a $100,000 account, set $5,000 to make auto-trading treat that as its working budget.
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--accent-yellow)', marginBottom: 8 }}>
                    {`Minimum allowed: current margin used ($${Math.max(0, Number(account?.margin || 0)).toFixed(2)}).`}
                  </div>
                  <input
                    className="form-input"
                    type="number"
                    min="0"
                    step="0.01"
                    inputMode="decimal"
                    value={autoTradeBudgetInput}
                    onChange={(e) => setAutoTradeBudgetInput(e.target.value)}
                    placeholder="5000"
                    disabled={autoTradeBudgetMode !== 'custom_budget'}
                  />
                </div>
              </label>

              <div className="flex justify-end gap-2">
                <button
                  className="btn btn-secondary"
                  onClick={() => setShowAutoTradeBudgetModal(false)}
                  disabled={autoTradeLoading}
                >
                  Cancel
                </button>
                <button
                  className="btn btn-success"
                  onClick={handleStartAutoTrade}
                  disabled={autoTradeLoading || !!autoTradeBlockedReason}
                  title={autoTradeBlockedReason || undefined}
                >
                  {autoTradeLoading ? <span className="loading-spinner" /> : 'Start Auto-Trade'}
                </button>
              </div>
            </div>
          </div>
        )}

        {showMarketSelector && (
          <div
            onClick={() => setShowMarketSelector(false)}
            style={{
              position: 'fixed',
              inset: 0,
              background: 'rgba(0, 0, 0, 0.55)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 9999,
              padding: 16,
            }}
          >
          <div
            className="card"
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 'min(900px, 96vw)',
              maxHeight: '88vh',
              overflow: 'auto',
              padding: 14,
            }}
          >
            <div className="flex justify-between items-center" style={{ marginBottom: 10, gap: 10, flexWrap: 'wrap' }}>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                Default mode: <strong>{runtimePopularMarketLabel}</strong>.
                {' '}You can add/remove extra markets below.
                {' '}Selected: <strong>{marketDraftSymbols.length}</strong>
                {!connected && (
                  <>
                    {' '}Preview list only; connect MT5 for live broker prices.
                  </>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); setShowMarketSelector(false); }}
                >
                  Close
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    saveSelectedMarkets(marketDraftSymbols);
                    setShowMarketSelector(false);
                  }}
                >
                  Save Selection
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); resetToRuntimePopular(); }}
                >
                  Reset to Popular
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); selectAllDraftMarkets(); }}
                >
                  Select All
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); clearAllDraftMarkets(); }}
                >
                  Clear All
                </button>
              </div>
            </div>
            <input
              className="form-input"
              placeholder="Search symbol (e.g. XAUUSD, US30, AAPL)"
              value={marketSearch}
              onChange={(e) => setMarketSearch(e.target.value)}
              style={{ marginBottom: 10, fontSize: 12 }}
            />
            <div style={{ display: 'grid', gridTemplateColumns: '1.05fr 1fr', gap: 10 }}>
              <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 8 }}>
                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8 }}>
                  Selected Markets ({marketDraftSymbols.length})
                </div>
                <div style={{ maxHeight: 260, overflow: 'auto' }}>
                  {marketDraftSymbols.length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-muted)', padding: '8px 6px' }}>
                      No markets selected. Pick from the list on the right.
                    </div>
                  ) : marketDraftSymbols.map((symbol, idx) => (
                    <div
                      key={`draft-${symbol}-${idx}`}
                      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderBottom: '1px solid var(--border)' }}
                    >
                      <span style={{ fontSize: 11, width: 20, color: 'var(--text-muted)' }}>{idx + 1}</span>
                      <span style={{ fontSize: 12, fontWeight: 600, minWidth: 70 }}>{symbol}</span>
                      <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
                        <button type="button" className="btn btn-secondary btn-sm" onClick={(e) => { e.stopPropagation(); moveDraftMarket(symbol, -1); }} disabled={idx === 0} title="Move up">Up</button>
                        <button type="button" className="btn btn-secondary btn-sm" onClick={(e) => { e.stopPropagation(); moveDraftMarket(symbol, 1); }} disabled={idx === marketDraftSymbols.length - 1} title="Move down">Down</button>
                        <button type="button" className="btn btn-secondary btn-sm" onClick={(e) => { e.stopPropagation(); removeMarketFromDraft(symbol); }} title="Remove">X</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 8 }}>
                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8 }}>
                  Available Markets ({marketSelectorRows.length})
                </div>
                <div style={{ maxHeight: 260, overflow: 'auto' }}>
                  {marketSelectorRows.map((row) => {
                    const symbol = String(row.name || '');
                    const checked = draftMarketSet.has(symbol);
                    const previewOnly = isPreviewMarketRow(row);
                    return (
                      <label
                        key={symbol}
                        style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderBottom: '1px solid var(--border)' }}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => setMarketSymbolChecked(symbol, e.target.checked)}
                        />
                        <span style={{ fontSize: 12, minWidth: 80, fontWeight: 600 }}>{symbol}</span>
                        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{normalizeCategoryName(row.category)}</span>
                        {previewOnly ? (
                          <span
                            title="Connect MT5 to verify this symbol and load live bid/ask."
                            style={{
                              marginLeft: 'auto',
                              fontSize: 10,
                              color: 'var(--accent-yellow)',
                              border: '1px solid rgba(245, 158, 11, 0.35)',
                              borderRadius: 6,
                              padding: '2px 6px',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            MT5 preview
                          </span>
                        ) : hasUsableBrokerQuote(row) ? (
                          <span
                            title="Live MT5 bid/ask loaded."
                            style={{
                              marginLeft: 'auto',
                              fontSize: 10,
                              color: 'var(--accent-green)',
                              border: '1px solid rgba(34, 197, 94, 0.35)',
                              borderRadius: 6,
                              padding: '2px 6px',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            Live
                          </span>
                        ) : null}
                      </label>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
          </div>
        )}

        {error && !isSoftTimeoutLikeError(error) && (
          <div className="error-banner" style={{ fontSize: 13 }}>
            Something went wrong: {error}
          </div>
        )}

        {/* Category filter tabs */}
        {recommendations.length > 0 && (
          <div className="flex gap-2 mb-3" style={{ flexWrap: 'wrap' }}>
            {availableCategories.map((cat) => (
              <button
                key={cat}
                className={`btn ${selectedCategory === cat ? 'btn-primary' : 'btn-secondary'} btn-sm`}
                onClick={() => setSelectedCategory(cat)}
                style={{ fontSize: 12, padding: '6px 14px', borderRadius: 20 }}
              >
                {cat === 'All' ? '\uD83C\uDF10' : cat === 'Stocks' ? '\uD83D\uDCC8' : cat === 'Crypto' ? '\uD83D\uDCB0' : cat === 'Indices' ? '\uD83D\uDCCA' : cat === 'Commodities' ? '\uD83C\uDF1F' : cat === 'Forex' ? '\uD83D\uDCB1' : '\uD83D\uDCCB'}{' '}
                {cat} ({categoryCounts[cat]})
              </button>
            ))}
          </div>
        )}

        {scanning && recommendations.length === 0 && (
          <div style={{ textAlign: 'center', padding: '40px 20px' }}>
            <span className="loading-spinner" style={{ width: 32, height: 32, borderWidth: 3 }} />
            <p style={{ marginTop: 16, color: 'var(--text-muted)', fontSize: 14 }}>
              Scanning all available markets... This may take a moment.
            </p>
          </div>
        )}

        {!scanning && recommendations.length === 0 && (
          <div style={{
            textAlign: 'center',
            padding: '40px 20px',
            background: 'var(--bg-secondary)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}>
            <div style={{ fontSize: 32, marginBottom: 8 }}>&#128269;</div>
            <p style={{ fontSize: 14, color: 'var(--text-muted)' }}>
              Click "Scan All" to see every available market with AI safety ratings
            </p>
          </div>
        )}

        {/* Show ALL recommendations - buy opportunities first, then waiting */}
        {recommendations.length > 0 && (
          <div>
            {/* Trade opportunities (BUY + SELL) */}
            {actionableRecs.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--accent-blue)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <TrendingUp size={14} />
                  Trade Opportunities ({actionableRecs.length})
                  <span style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-muted)' }}>
                    {actionableRecs.filter((r) => r.signal.action === 'BUY').length} BUY
                    {' / '}
                    {actionableRecs.filter((r) => r.signal.action === 'SELL').length} SELL
                  </span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {actionableRecs.map((rec) => (
                    <TradeCard
                      key={`trade-${marketIdentityKey(rec.symbol)}`}
                      rec={rec}
                      onExecuted={refreshPositionsBurst}
                      currency={currency}
                      liveTick={getLiveTickForSymbol(rec.symbol)}
                      executionBlockedReason={manualExecutionBlockedReason}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* All other symbols - with Buy button */}
            {marketRows.length > 0 && (
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Clock size={14} />
                  All Markets ({marketRows.length})
                </div>
                <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                  {visibleMarketRecs.map((rec, i) => (
                    <MarketRow
                      key={`hold-${marketIdentityKey(rec.symbol) || i}`}
                      rec={rec}
                      currency={currency}
                      onExecuted={refreshPositionsBurst}
                      isLast={i === visibleMarketRecs.length - 1}
                      liveTick={getLiveTickForSymbol(rec.symbol)}
                      executionBlockedReason={manualExecutionBlockedReason}
                    />
                  ))}
                </div>
                {remainingMarketRows > 0 && (
                  <div style={{ padding: 12, borderTop: '1px solid var(--border)', textAlign: 'center' }}>
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => setVisibleMarketRows((v) => Math.min(v + 60, marketRows.length))}
                    >
                      Load More ({remainingMarketRows} remaining)
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Beginner tip */}
      <div style={{
        marginTop: 24,
        padding: '14px 18px',
        background: 'rgba(59, 130, 246, 0.06)',
        border: '1px solid rgba(59, 130, 246, 0.15)',
        borderRadius: 8,
        fontSize: 12,
        color: 'var(--text-secondary)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <HelpCircle size={16} style={{ color: 'var(--accent-blue)', flexShrink: 0 }} />
        <span>
          <strong>Tip:</strong> Green "Buy" cards are opportunities the AI recommends. The bar shows how confident the AI is.
          Items in "Waiting" are being watched but aren't ready yet. Every trade includes automatic stop-loss protection.
        </span>
      </div>
    </div>
  );
}
