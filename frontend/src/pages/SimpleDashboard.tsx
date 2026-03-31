import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { RefreshCw, AlertOctagon, X, TrendingUp, TrendingDown, Minus, Play, HelpCircle, DollarSign, ShieldCheck, Clock, Zap, Power, CheckCircle, XCircle } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse, PositionInfo, Recommendation, OrderResult, TickData } from '../types';
import ConnectionBar from '../components/ConnectionBar';
import { getSymbolName, getSymbolEmoji, getSignalStrength, getActionDescription, explainProfitLoss } from '../utils/symbolNames';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
}

type AvailableSymbolRow = {
  name: string;
  description?: string;
  category?: string;
  bid?: number;
  ask?: number;
  spread?: number;
};

const MARKET_SELECTION_STORAGE_KEY = 'trading_selected_markets_v2';
const EGYPT_POPULAR_MARKET_HINTS = [
  'XAUUSD', 'GOLD',
  'XAGUSD', 'SILVER',
  'US30', 'DJ30',
  'US100', 'NAS100',
  'US500', 'SP500',
  'GER40', 'DAX',
  'UK100', 'FTSE',
  'WTI', 'BRENT',
  'AAPL', 'MSFT', 'NVDA', 'TSLA',
];

const IBKR_EXTRA_MARKET_CATALOG: AvailableSymbolRow[] = [
  // Indices
  { name: 'US30', category: 'Indices' },
  { name: 'US100', category: 'Indices' },
  { name: 'US500', category: 'Indices' },
  { name: 'GER40', category: 'Indices' },
  { name: 'UK100', category: 'Indices' },
  { name: 'JPN225', category: 'Indices' },
  { name: 'HK50', category: 'Indices' },
  { name: 'AUS200', category: 'Indices' },
  // Commodities
  { name: 'GOLD', category: 'Commodities' },
  { name: 'XAUUSD', category: 'Commodities' },
  { name: 'SILVER', category: 'Commodities' },
  { name: 'XAGUSD', category: 'Commodities' },
  { name: 'WTI', category: 'Commodities' },
  { name: 'BRENT', category: 'Commodities' },
  { name: 'NATGAS', category: 'Commodities' },
  { name: 'XPDUSD', category: 'Commodities' },
  { name: 'XPTUSD', category: 'Commodities' },
  // US mega-cap & liquid stocks
  { name: 'AAPL', category: 'Stocks' },
  { name: 'MSFT', category: 'Stocks' },
  { name: 'NVDA', category: 'Stocks' },
  { name: 'TSLA', category: 'Stocks' },
  { name: 'AMZN', category: 'Stocks' },
  { name: 'GOOGL', category: 'Stocks' },
  { name: 'META', category: 'Stocks' },
  { name: 'AMD', category: 'Stocks' },
  { name: 'INTC', category: 'Stocks' },
  { name: 'NFLX', category: 'Stocks' },
  { name: 'AVGO', category: 'Stocks' },
  { name: 'ADBE', category: 'Stocks' },
  { name: 'CRM', category: 'Stocks' },
  { name: 'ORCL', category: 'Stocks' },
  { name: 'JPM', category: 'Stocks' },
  { name: 'BAC', category: 'Stocks' },
  { name: 'WMT', category: 'Stocks' },
  { name: 'KO', category: 'Stocks' },
  { name: 'NKE', category: 'Stocks' },
  { name: 'PFE', category: 'Stocks' },
  { name: 'XOM', category: 'Stocks' },
  { name: 'CVX', category: 'Stocks' },
  { name: 'DIS', category: 'Stocks' },
  { name: 'BA', category: 'Stocks' },
  { name: 'V', category: 'Stocks' },
  { name: 'MA', category: 'Stocks' },
  // ETFs (very common)
  { name: 'SPY', category: 'Stocks' },
  { name: 'QQQ', category: 'Stocks' },
  { name: 'DIA', category: 'Stocks' },
  { name: 'IWM', category: 'Stocks' },
  { name: 'GLD', category: 'Stocks' },
  { name: 'SLV', category: 'Stocks' },
  { name: 'USO', category: 'Stocks' },
  { name: 'TLT', category: 'Stocks' },
  { name: 'EEM', category: 'Stocks' },
  { name: 'XLE', category: 'Stocks' },
  { name: 'XLF', category: 'Stocks' },
  { name: 'XLK', category: 'Stocks' },
];

function mergeUniverseRows(baseRows: AvailableSymbolRow[], extraRows: AvailableSymbolRow[]): AvailableSymbolRow[] {
  const byName = new Map<string, AvailableSymbolRow>();
  for (const row of baseRows) {
    const name = String(row.name || '').trim();
    if (!name) continue;
    byName.set(name, row);
  }
  for (const row of extraRows) {
    const name = String(row.name || '').trim();
    if (!name) continue;
    if (!byName.has(name)) {
      byName.set(name, {
        name,
        category: row.category || 'Other',
        description: row.description || `${name} (IBKR catalog)`,
        bid: row.bid ?? 0,
        ask: row.ask ?? 0,
        spread: row.spread ?? 0,
      });
    }
  }
  return Array.from(byName.values());
}

function formatPct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  return `${value.toFixed(1)}%`;
}

function compactReasons(reasons?: string[], fallback?: string) {
  if (reasons && reasons.length > 0) return reasons.slice(0, 3);
  return fallback ? [fallback] : [];
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

function formatCommissionLabel(rec: Recommendation, currency: string) {
  const side = rec.commission_per_lot_side;
  const roundTurn = rec.commission_round_turn_per_lot;
  const pct = rec.commission_percent_rate;
  const notional = rec.commission_notional_1lot;
  const model = String(rec.commission_model || '').toLowerCase();
  const samples = typeof rec.commission_samples === 'number' ? rec.commission_samples : null;

  if (typeof side === 'number' && side > 0) {
    const rtText = typeof roundTurn === 'number' ? ` • ${currency} ${roundTurn.toFixed(2)} round-turn` : '';
    const src = model === 'realized_history' && samples && samples > 0
      ? ` • from ${samples} closed deal(s)`
      : '';
    return `Commission: ${currency} ${side.toFixed(2)} / lot / side${rtText}${src}`;
  }
  if (model === 'percent_notional' && typeof pct === 'number' && pct > 0) {
    const sideEstimate = typeof notional === 'number' && notional > 0 ? (notional * pct) / 100 : null;
    const estText = typeof sideEstimate === 'number' ? ` (~${currency} ${sideEstimate.toFixed(2)} / side)` : '';
    return `Commission: ${pct.toFixed(4)}% of notional${estText}`;
  }
  if (model === 'ibkr_not_implemented') {
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

function toFiniteNumber(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function pickEgyptPopularSymbols(allSymbols: AvailableSymbolRow[]): string[] {
  const uniqueByName = new Map<string, AvailableSymbolRow>();
  for (const row of allSymbols) {
    const name = String(row.name || '').trim();
    if (!name) continue;
    if (!uniqueByName.has(name)) uniqueByName.set(name, row);
  }
  const names = Array.from(uniqueByName.keys());
  const upperNames = names.map((n) => n.toUpperCase());

  const picked: string[] = [];
  for (const hint of EGYPT_POPULAR_MARKET_HINTS) {
    const idx = upperNames.findIndex((n) => n === hint || n.includes(hint));
    if (idx >= 0) {
      const sym = names[idx];
      if (!picked.includes(sym)) picked.push(sym);
    }
  }

  // Keep a minimum default set so first-time users never see an almost-empty list.
  if (picked.length < 12) {
    const fallback = allSymbols
      .filter((s) => ['Commodities', 'Indices', 'Stocks'].includes(String(s.category || 'Other')))
      .slice(0, 24)
      .map((s) => String(s.name || '').trim())
      .filter(Boolean);
    for (const sym of fallback) {
      if (!picked.includes(sym)) picked.push(sym);
      if (picked.length >= 18) break;
    }
  }

  return picked;
}

function pickIbkrPopularSymbols(allSymbols: AvailableSymbolRow[]): string[] {
  // Prefer liquid US symbols/ETFs that reliably resolve on IBKR accounts.
  const ibkrHints = [
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'AMD',
    'SPY', 'QQQ', 'DIA', 'IWM', 'GLD', 'SLV', 'XOM', 'CVX', 'JPM', 'BAC',
  ];
  const uniqueByName = new Map<string, AvailableSymbolRow>();
  for (const row of allSymbols) {
    const name = String(row.name || '').trim();
    if (!name) continue;
    if (!uniqueByName.has(name)) uniqueByName.set(name, row);
  }
  const names = Array.from(uniqueByName.keys());
  const upperNames = names.map((n) => n.toUpperCase());
  const picked: string[] = [];
  for (const hint of ibkrHints) {
    const idx = upperNames.findIndex((n) => n === hint || n.includes(hint));
    if (idx >= 0) {
      const sym = names[idx];
      if (!picked.includes(sym)) picked.push(sym);
    }
  }
  if (picked.length < 10) {
    const fallback = allSymbols
      .filter((s) => ['Stocks', 'Indices'].includes(String(s.category || 'Other')))
      .slice(0, 24)
      .map((s) => String(s.name || '').trim())
      .filter(Boolean);
    for (const sym of fallback) {
      if (!picked.includes(sym)) picked.push(sym);
      if (picked.length >= 16) break;
    }
  }
  return picked;
}

const MAX_UI_SYMBOL_ROWS = 400;
const MAX_MT5_SCAN_SYMBOLS = 30;
const MAX_IBKR_SCAN_SYMBOLS = 8;

function normalizeIbkrSymbolAlias(symbol: string): string {
  const raw = String(symbol || '').trim().toUpperCase();
  const aliases: Record<string, string> = {
    US500: 'SPY',
    SPX500: 'SPY',
    US100: 'QQQ',
    NAS100: 'QQQ',
    US30: 'DIA',
    DJ30: 'DIA',
    GER40: 'EWG',
    DAX40: 'EWG',
    UK100: 'EWU',
    FTSE100: 'EWU',
    GOLD: 'GLD',
    XAUUSD: 'GLD',
    SILVER: 'SLV',
    XAGUSD: 'SLV',
    WTI: 'USO',
    USOIL: 'USO',
    XTIUSD: 'USO',
    BRENT: 'BNO',
    UKOIL: 'BNO',
    XBRUSD: 'BNO',
  };
  return aliases[raw] || raw;
}

function createPendingRecommendation(symbol: AvailableSymbolRow): Recommendation {
  const bid = Number(symbol.bid || 0);
  const ask = Number(symbol.ask || 0);
  const entry = bid > 0 && ask > 0 ? (bid + ask) / 2 : Math.max(bid, ask, 0);
  return {
    symbol: String(symbol.name || ''),
    category: String(symbol.category || 'Other'),
    signal: {
      action: 'HOLD',
      confidence: 0,
      stop_loss: null,
      take_profit: null,
      max_holding_minutes: null,
      reason: 'Waiting for scan.',
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
    explanation: 'Pending scan for this market. Click "Scan All" to evaluate it.',
    ready_to_execute: false,
    degraded_reasons: [],
    execution_reason: 'Pending analysis.',
  };
}

// Beginner-friendly recommendation card
function TradeCard({ rec, onExecuted, currency, liveTick }: { rec: Recommendation; onExecuted: () => Promise<void> | void; currency: string; liveTick?: TickData }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [showAmountInput, setShowAmountInput] = useState(false);
  const [amount, setAmount] = useState('');
  const [volumePreview, setVolumePreview] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number } | null>(null);
  // SL/TP as dollar amounts: how much you're willing to lose / want to gain
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');

  if (dismissed) return null;

  const digits = rec.entry_price_estimate < 50 ? 5 : 2;
  const presetAmounts = [10, 25, 50, 100, 250];
  const minSlTp = volumePreview?.min_sl_tp_dollars || 0;

  // Convert dollar SL/TP to price levels for execution
  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumePreview || volumePreview.volume <= 0 || volumePreview.contract_size <= 0) return 0;
    const pricePerUnit = volumePreview.volume * volumePreview.contract_size;
    const livePrice = ask || volumePreview.price || rec.entry_price_estimate || 0;
    if (livePrice <= 0) return 0;
    return isSL ? livePrice - dollarAmount / pricePerUnit : livePrice + dollarAmount / pricePerUnit;
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumePreview(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const preview = await api.calculateVolume(rec.symbol, num);
        setVolumePreview(preview);
        // Default SL/TP = 10% of investment, but at least the minimum required
        const defaultSlTp = Math.max(Math.round(num * 0.10), Math.ceil(preview.min_sl_tp_dollars));
        // Default to >= 2:1 reward:risk so manual orders don't get blocked by RR policy.
        const defaultTp = Math.max(Math.ceil(defaultSlTp * 2), defaultSlTp + 1);
        setSlDollar(String(defaultSlTp));
        setTpDollar(String(defaultTp));
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

  const handleExecute = async () => {
    if (!rec.signal_id) return;
    const amountNum = parseFloat(amount);
    if (!amountNum || amountNum <= 0) return;
    // Convert dollar SL/TP to price levels
    const slAmt = parseFloat(slDollar) || 0;
    const tpAmt = parseFloat(tpDollar) || 0;
    const slPrice = slAmt > 0 ? dollarToPrice(slAmt, true) : undefined;
    const tpPrice = tpAmt > 0 ? dollarToPrice(tpAmt, false) : undefined;
    setLoading(true);
    try {
      const res = await api.executeRecommendation(rec.signal_id, amountNum, slPrice, tpPrice);
      setResult(res);
      setShowAmountInput(false);
      void onExecuted();
    } catch (e: any) {
      setResult({
        success: false, retcode: -1, retcode_desc: formatUiError(e),
        ticket: null, volume: null, price: null,
        stop_loss: null, take_profit: null, comment: '',
      });
    } finally {
      setLoading(false);
    }
  };

  const bid = liveTick?.bid ?? null;
  const ask = liveTick?.ask ?? null;

  const isBuy = rec.signal.action === 'BUY';
  const isSell = rec.signal.action === 'SELL';
  const isHold = rec.signal.action === 'HOLD';
  const strength = getSignalStrength(rec.signal.confidence);
  const symbolName = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);
  const qualityScore = rec.trade_quality?.final_trade_quality_score && rec.trade_quality.final_trade_quality_score > 0
    ? rec.trade_quality.final_trade_quality_score
    : (rec.signal.confidence || 0);
  const qualityThreshold = rec.trade_quality?.threshold ?? 0;
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
          {bid !== null && ask !== null ? (
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
            {strength.label} Signal
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
      <button
        className="btn btn-secondary btn-sm mb-3"
        onClick={() => setShowDetails(!showDetails)}
        style={{ fontSize: 11, opacity: 0.6 }}
      >
        {showDetails ? 'Hide' : 'Show'} technical details
      </button>

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
            : `Could not place trade: ${result.retcode_desc}`
          }
        </div>
      ) : !showAmountInput ? (
      <div className="flex gap-3">
        <button
          className="btn btn-success"
          onClick={() => {
              setShowAmountInput(true);
              if (suggestedAmount && !amount) {
                void handleAmountChange(String(suggestedAmount));
              }
            }}
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
              disabled={loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
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
        </div>
      )}
    </div>
  );
}

// Beginner-friendly position card
function PositionCard({ position, currency, onClose, leverage = 100 }: { position: PositionInfo; currency: string; onClose: (ticket: number) => void; leverage?: number }) {
  const [closing, setClosing] = useState(false);
  const [contractSize, setContractSize] = useState<number | null>(null);
  const symbolName = getSymbolName(position.symbol);
  const emoji = getSymbolEmoji(position.symbol);
  const isBuy = position.type === 'BUY';
  const profitColor = position.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  const profitText = explainProfitLoss(position.profit, currency);
  const digs = position.price_open < 50 ? 5 : 2;

  // Fetch contract size for dollar SL/TP calculation
  useEffect(() => {
    api.getSymbolInfo(position.symbol).then((info: any) => {
      if (info?.trade_contract_size) setContractSize(info.trade_contract_size);
    }).catch(() => {});
  }, [position.symbol]);

  // Calculate dollar SL/TP amounts
  const calcDollarDistance = (priceLevel: number) => {
    if (!contractSize || priceLevel <= 0) return null;
    const distance = Math.abs(position.price_open - priceLevel);
    return distance * position.volume * contractSize;
  };
  const amountMatch = position.comment?.match(/TA:\$(\d+(?:\.\d+)?)|TA(\d+(?:\.\d+)?)/i);
  const slMatch = position.comment?.match(/SLA:\$(\d+(?:\.\d+)?)|SLA(\d+(?:\.\d+)?)/i);
  const tpMatch = position.comment?.match(/TPA:\$(\d+(?:\.\d+)?)|TPA(\d+(?:\.\d+)?)/i);
  const commentAmount = amountMatch ? (amountMatch[1] || amountMatch[2]) : undefined;
  const commentSlAmount = slMatch ? (slMatch[1] || slMatch[2]) : undefined;
  const commentTpAmount = tpMatch ? (tpMatch[1] || tpMatch[2]) : undefined;
  const slDollars = commentSlAmount ? parseFloat(commentSlAmount) : (position.stop_loss > 0 ? calcDollarDistance(position.stop_loss) : null);
  const tpDollars = commentTpAmount ? parseFloat(commentTpAmount) : (position.take_profit > 0 ? calcDollarDistance(position.take_profit) : null);

  // Calculate investment amount — use comment if available, otherwise compute from position data
  const computedInvestment = contractSize
    ? Math.round(position.volume * position.price_open * contractSize / leverage)
    : 0;
  const investedNum = commentAmount ? parseFloat(commentAmount) : computedInvestment;
  const currentValue = investedNum > 0 ? investedNum + position.profit : 0;
  const slTargetValue = investedNum > 0 && slDollars != null ? Math.max(0, investedNum - slDollars) : null;
  const tpTargetValue = investedNum > 0 && tpDollars != null ? investedNum + tpDollars : null;
  const slPct = investedNum > 0 && slDollars != null ? (slDollars / investedNum) * 100 : null;
  const tpPct = investedNum > 0 && tpDollars != null ? (tpDollars / investedNum) * 100 : null;
  const slExceedsStart = investedNum > 0 && slDollars != null && slDollars > investedNum;

  const handleClose = async () => {
    setClosing(true);
    try {
      await onClose(position.ticket);
    } finally {
      setClosing(false);
    }
  };

  // Time since opened
  const openTime = new Date(position.time * 1000);
  const minutesOpen = Math.round((Date.now() - openTime.getTime()) / 60000);
  const timeStr = minutesOpen < 60 ? `${minutesOpen}m ago` : `${Math.round(minutesOpen / 60)}h ${minutesOpen % 60}m ago`;

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
            {position.profit >= 0 ? '+' : ''}{position.profit.toFixed(2)} {currency}
          </div>
          <div className="text-muted" style={{ fontSize: 11 }}>{profitText}</div>
        </div>
      </div>

      {/* Investment summary - big numbers */}
      {investedNum > 0 && (
        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 10,
          background: 'var(--bg-tertiary)', borderRadius: 8, padding: '10px 14px',
        }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Started With</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace' }}>${investedNum}</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Current Value</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
              ${currentValue.toFixed(2)}
            </div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Return</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
              {position.profit >= 0 ? '+' : ''}{investedNum > 0 ? ((position.profit / investedNum) * 100).toFixed(1) : '0'}%
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
          <span className="font-mono">{position.price_current.toFixed(digs)}</span>
        </span>
        {position.stop_loss > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-red)' }}>SL:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-red)' }}>
              {slExceedsStart
                ? `Risk > $${investedNum.toFixed(0)}`
                : (slTargetValue != null ? `$${slTargetValue.toFixed(2)}` : (slDollars != null ? `-$${slDollars.toFixed(2)}` : position.stop_loss.toFixed(digs)))}
            </span>
            {slDollars != null && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` (-$${slDollars.toFixed(2)}`}
                {slPct != null ? `, -${slPct.toFixed(1)}%` : ''}
                {`) (${position.stop_loss.toFixed(digs)})`}
              </span>
            )}
          </span>
        )}
        {position.take_profit > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-green)' }}>TP:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-green)' }}>
              {tpTargetValue != null ? `$${tpTargetValue.toFixed(2)}` : (tpDollars != null ? `+$${tpDollars.toFixed(2)}` : position.take_profit.toFixed(digs))}
            </span>
            {tpDollars != null && (
              <span className="text-muted" style={{ fontSize: 10 }}>
                {` (+$${tpDollars.toFixed(2)}`}
                {tpPct != null ? `, +${tpPct.toFixed(1)}%` : ''}
                {`) (${position.take_profit.toFixed(digs)})`}
              </span>
            )}
          </span>
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-between items-center mt-3">
        <div className="flex gap-3" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {position.stop_loss > 0 && <span style={{ color: 'var(--accent-green)' }}>✓ Stop-loss {slDollars != null ? `(-$${slDollars.toFixed(0)})` : 'active'}</span>}
          {position.take_profit > 0 && <span style={{ color: 'var(--accent-green)' }}>✓ Take-profit {tpDollars != null ? `(+$${tpDollars.toFixed(0)})` : 'set'}</span>}
          {position.stop_loss === 0 && position.take_profit === 0 && <span>No SL/TP set</span>}
        </div>
        <button
          className={`btn ${position.profit >= 0 ? 'btn-success' : 'btn-warning'} btn-sm`}
          onClick={handleClose}
          disabled={closing}
          style={{ fontSize: 12 }}
        >
          {closing ? <span className="loading-spinner" /> : <DollarSign size={12} />}
          {position.profit >= 0 ? 'Sell & Take Profit' : 'Sell & Cut Loss'}
        </button>
      </div>
    </div>
  );
}

// Compact market row with Buy button for any symbol
function MarketRow({ rec, currency, onExecuted, isLast, liveTick }: { rec: Recommendation; currency: string; onExecuted: () => Promise<void> | void; isLast: boolean; liveTick?: TickData }) {
  const [expanded, setExpanded] = useState(false);
  const [amount, setAmount] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [volumeInfo, setVolumeInfo] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number } | null>(null);
  // Manual action override: user can choose BUY or SELL regardless of AI signal
  const [manualAction, setManualAction] = useState<'BUY' | 'SELL'>(rec.signal.action === 'SELL' ? 'SELL' : 'BUY');

  // SL/TP as dollar amounts
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');

  const price = rec.entry_price_estimate || 0;
  const digits = price < 50 ? 5 : 2;
  const minSlTp = volumeInfo?.min_sl_tp_dollars || 0;

  const bid = liveTick?.bid ?? null;
  const ask = liveTick?.ask ?? null;

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
      setVolumeInfo(null);
    }
  }, [rec.symbol]);

  const name = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);
  const conf = Math.round(rec.signal.confidence * 100);
  const strength = getSignalStrength(rec.signal.confidence);
  const pendingEvaluation = !rec.signal_id && conf === 0;
  const qualityScore = Math.round((
    (rec.trade_quality?.final_trade_quality_score && rec.trade_quality.final_trade_quality_score > 0)
      ? rec.trade_quality.final_trade_quality_score
      : (rec.signal.confidence || 0)
  ) * 100);
  const qualityThreshold = Math.round((rec.trade_quality?.threshold || 0) * 100);
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

  // Convert dollar SL/TP to price levels for execution
  const isBuyAction = manualAction === 'BUY';
  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumeInfo || volumeInfo.volume <= 0 || volumeInfo.contract_size <= 0) return 0;
    const pricePerUnit = volumeInfo.volume * volumeInfo.contract_size;
    const livePrice = isBuyAction ? (ask || volumeInfo.price || rec.entry_price_estimate || 0) : (bid || volumeInfo.price || rec.entry_price_estimate || 0);
    if (livePrice <= 0) return 0;
    if (isBuyAction) {
      return isSL ? livePrice - dollarAmount / pricePerUnit : livePrice + dollarAmount / pricePerUnit;
    } else {
      // SELL: SL is above price, TP is below price
      return isSL ? livePrice + dollarAmount / pricePerUnit : livePrice - dollarAmount / pricePerUnit;
    }
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumeInfo(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const info = await api.calculateVolume(rec.symbol, num);
        setVolumeInfo(info);
        // Default SL/TP = 10% of investment, but at least the minimum required
        const defaultSlTp = Math.max(Math.round(num * 0.10), Math.ceil(info.min_sl_tp_dollars));
        // Default to >= 2:1 reward:risk so manual orders don't get blocked by RR policy.
        const defaultTp = Math.max(Math.ceil(defaultSlTp * 2), defaultSlTp + 1);
        setSlDollar(String(defaultSlTp));
        setTpDollar(String(defaultTp));
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

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
      const res = await api.quickBuy(rec.symbol, num, slNum, tpNum, manualAction);
      setResult(res);
      if (res.success) void onExecuted();
    } catch (e: any) {
      setResult({ success: false, retcode: -1, retcode_desc: formatUiError(e), ticket: null, volume: null, price: null, stop_loss: null, take_profit: null, comment: '' });
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
          {bid !== null && ask !== null ? (
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
          ) : (
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>--</span>
          )}
        </div>

        <div className="flex items-center gap-2" style={{ width: 100, flexShrink: 0 }}>
          <div style={{ flex: 1, height: 5, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${pendingEvaluation ? 4 : conf}%`, height: '100%', background: pendingEvaluation ? 'var(--text-muted)' : strength.color, borderRadius: 3 }} />
          </div>
          <span style={{ fontSize: 10, color: pendingEvaluation ? 'var(--text-muted)' : strength.color, fontWeight: 600 }}>
            {pendingEvaluation ? '...' : `${conf}%`}
          </span>
        </div>

        <div style={{ width: 72, textAlign: 'right', fontSize: 10, fontWeight: 700, color: pendingEvaluation ? 'var(--text-muted)' : (qualityScore >= qualityThreshold ? 'var(--accent-green)' : 'var(--accent-red)') }}>
          {pendingEvaluation ? 'Q --' : `Q ${qualityScore}%`}
        </div>

        {result ? (
          <span style={{ fontSize: 11, color: result.success ? 'var(--accent-green)' : 'var(--accent-red)', marginLeft: 12 }}>
            {result.success ? `${manualAction === 'SELL' ? 'Sold' : 'Bought'}! #${result.ticket}` : result.retcode_desc}
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
              disabled={loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
              style={{ fontSize: 11, padding: '4px 14px', fontWeight: 600 }}
            >
              {loading ? <span className="loading-spinner" style={{ width: 10, height: 10 }} /> : <Play size={10} />}
              {amount ? `${manualAction === 'SELL' ? 'Sell' : 'Buy'} $${amount}` : (manualAction === 'SELL' ? 'Sell' : 'Buy')}
            </button>
          </div>

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

export default function SimpleDashboard({ status, onRefresh }: Props) {
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [allSymbols, setAllSymbols] = useState<AvailableSymbolRow[]>([]);
  const [selectedMarketSymbols, setSelectedMarketSymbols] = useState<string[]>([]);
  const [marketDraftSymbols, setMarketDraftSymbols] = useState<string[]>([]);
  const [showMarketSelector, setShowMarketSelector] = useState(false);
  const [marketSearch, setMarketSearch] = useState('');
  const [availableMarketCount, setAvailableMarketCount] = useState(0);
  const [liveTicks, setLiveTicks] = useState<Record<string, TickData>>({});
  const [analyzedCount, setAnalyzedCount] = useState(0);
  const [positions, setPositions] = useState<PositionInfo[]>([]);
  const [scanning, setScanning] = useState(false);
  const scanningRef = useRef(false);
  const [lastScan, setLastScan] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string>('All');
  const [visibleMarketRows, setVisibleMarketRows] = useState<number>(60);

  // Auto-trade state
  const [autoTradeRunning, setAutoTradeRunning] = useState(false);
  const [autoTradeLoading, setAutoTradeLoading] = useState(false);
  const [autoTradeLogs, setAutoTradeLogs] = useState<Array<{
    timestamp: number; symbol: string; action: string;
    confidence: number; quality_score?: number; detail: string; success: boolean;
  }>>([]);
  const [aiActivity, setAiActivity] = useState<Array<{
    timestamp: number; action: string; symbol: string;
    ticket: number; detail: string; profit: number; source?: string;
  }>>([]);
  const [posManagerRunning, setPosManagerRunning] = useState(false);
  const [portfolioSnapshot, setPortfolioSnapshot] = useState(status?.portfolio || null);
  const [geminiState, setGeminiState] = useState({
    available: status?.gemini_available ?? false,
    degraded: status?.gemini_degraded ?? false,
    last_error: status?.gemini_last_error ?? null,
    state: status?.gemini_state ?? ((status?.gemini_degraded ?? false) ? 'degraded' : (status?.gemini_available ? 'available' : 'unavailable')),
    cooldown_seconds: status?.gemini_cooldown_seconds ?? 0,
    credits_pct: status?.gemini_credits_pct ?? ((status?.gemini_available ?? false) ? 100 : 0),
  });
  const geminiLiveRef = useRef(false);

  const connected = status?.connected ?? false;
  const account = status?.account;
  const currency = account?.currency || 'USD';
  const geminiErrorSummary = summarizeGeminiError(geminiState.last_error);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(MARKET_SELECTION_STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        const cleaned = parsed.map((x) => String(x || '').trim()).filter(Boolean);
        if (cleaned.length > 0) setSelectedMarketSymbols(cleaned);
      }
    } catch {
      // ignore malformed local cache
    }
  }, []);

  useEffect(() => {
    if (!showMarketSelector) return;
    const popular = status?.platform === 'ibkr'
      ? pickIbkrPopularSymbols(allSymbols)
      : pickEgyptPopularSymbols(allSymbols);
    const base = selectedMarketSymbols.length > 0 ? selectedMarketSymbols : popular;
    setMarketDraftSymbols(base);
  }, [showMarketSelector, selectedMarketSymbols, allSymbols, status?.platform]);

  useEffect(() => {
    if (status?.portfolio) setPortfolioSnapshot(status.portfolio);
    // Avoid oscillation between /status (app-level poll) and /auto-trade/status (dashboard poll).
    // Once we have live dashboard Gemini telemetry, keep it as the source of truth.
    if (geminiLiveRef.current) return;
    setGeminiState((prev) => {
      const available = status?.gemini_available ?? prev.available;
      const degraded = status?.gemini_degraded ?? prev.degraded;
      const state = status?.gemini_state
        ?? (degraded ? 'degraded' : (available ? 'available' : 'unavailable'));
      const cooldown = toFiniteNumber(status?.gemini_cooldown_seconds);
      const credits = toFiniteNumber(status?.gemini_credits_pct);
      return {
        available,
        degraded,
        last_error: status?.gemini_last_error ?? prev.last_error ?? null,
        state,
        cooldown_seconds: cooldown ?? prev.cooldown_seconds ?? 0,
        credits_pct: credits ?? prev.credits_pct ?? (available ? 100 : 0),
      };
    });
  }, [status]);

  const refreshPositions = useCallback(async () => {
    if (!connected) return;
    try {
      const pos = await api.getPositions();
      setPositions(pos);
    } catch {}
  }, [connected]);

  const refreshPositionsBurst = useCallback(async () => {
    // Make manual trade feedback feel immediate: refresh rapidly for a short window.
    await refreshPositions();
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      await refreshPositions();
    }
  }, [refreshPositions]);

  const saveSelectedMarkets = useCallback((symbols: string[]) => {
    const unique = Array.from(new Set(symbols.map((s) => String(s || '').trim()).filter(Boolean)));
    setSelectedMarketSymbols(unique);
    localStorage.setItem(MARKET_SELECTION_STORAGE_KEY, JSON.stringify(unique));
  }, []);


  const scanMarkets = useCallback(async (symbolsOverride?: string[]) => {
    if (!connected || scanningRef.current) return;
    scanningRef.current = true;
    setScanning(true);
    setError('');
    try {
      let availableTotal = 0;
      let fullUniverse = allSymbols;
      if (!fullUniverse || fullUniverse.length === 0 || !!symbolsOverride) {
        const available = await api.getAvailableSymbols(undefined, false);
        availableTotal = Number(available.total || 0);
        const brokerSymbols = Object.values(available.categories || {}).flat() as AvailableSymbolRow[];
        fullUniverse = (status?.platform === 'ibkr')
          ? mergeUniverseRows(brokerSymbols, IBKR_EXTRA_MARKET_CATALOG)
          : brokerSymbols;
      }
      setAvailableMarketCount(fullUniverse.length || availableTotal || 0);
      setAllSymbols(fullUniverse);
      setRecommendations([]);
      setAnalyzedCount(0);
      setLastScan(Date.now() / 1000);

      // Fast opportunity scan: score a focused subset first to avoid very long cycles.
      const mode = String(status?.user_policy?.mode || 'balanced').toLowerCase();
      const categoryPriority: Record<string, number> = mode === 'aggressive'
        ? { Indices: 0, Commodities: 1, Stocks: 2, Forex: 3, Other: 4 }
        : mode === 'safe'
          ? { Stocks: 0, Indices: 1, Commodities: 2, Forex: 3, Other: 4 }
          : { Commodities: 0, Indices: 1, Stocks: 2, Forex: 3, Other: 4 };

      const symbolByName = new Map<string, AvailableSymbolRow>();
      for (const row of fullUniverse) {
        const name = String(row.name || '').trim();
        if (!name) continue;
        symbolByName.set(name, row);
      }

      const defaultPopular = status?.platform === 'ibkr'
        ? pickIbkrPopularSymbols(fullUniverse)
        : pickEgyptPopularSymbols(fullUniverse);
      const selectedInputRaw = (symbolsOverride && symbolsOverride.length > 0)
        ? symbolsOverride
        : (selectedMarketSymbols.length > 0 ? selectedMarketSymbols : defaultPopular);
      const selectedInput = status?.platform === 'ibkr'
        ? selectedInputRaw.map((sym) => normalizeIbkrSymbolAlias(sym))
        : selectedInputRaw;
      const selectedNow = selectedInput
        .filter((sym) => symbolByName.has(sym));
      if (status?.platform === 'ibkr' && selectedNow.length > 0) {
        const savedRaw = selectedMarketSymbols.map((sym) => String(sym || '').trim().toUpperCase());
        const savedNormalized = selectedNow.map((sym) => String(sym || '').trim().toUpperCase());
        if (savedRaw.join(',') !== savedNormalized.join(',')) {
          saveSelectedMarkets(selectedNow);
        }
      } else if (selectedMarketSymbols.length === 0 && defaultPopular.length > 0) {
        saveSelectedMarkets(defaultPopular);
      }

      const selectedRowsAll = selectedNow
        .map((sym) => symbolByName.get(sym))
        .filter(Boolean) as AvailableSymbolRow[];
      const selectedRows = selectedRowsAll.slice(0, MAX_UI_SYMBOL_ROWS);
      const liveSymbols = selectedRows.filter((s) => Number(s.bid || 0) > 0 && Number(s.ask || 0) > 0);

      const byCategory = new Map<string, Array<any>>();
      for (const item of liveSymbols) {
        const cat = String(item.category || 'Other');
        const bucket = byCategory.get(cat) || [];
        bucket.push(item);
        byCategory.set(cat, bucket);
      }
      for (const bucket of byCategory.values()) {
        bucket.sort((a, b) => Number(a.spread || 0) - Number(b.spread || 0));
      }

      // Keep broad market representation; avoid one-category dominance.
      const targetCount = Math.max(20, Math.min(120, selectedRows.length || 60));
      const categoryOrder = Array.from(byCategory.keys()).sort(
        (a, b) => (categoryPriority[a] ?? 99) - (categoryPriority[b] ?? 99),
      );
      const perCategoryCap = mode === 'aggressive' ? 20 : 16;
      const selected: Array<any> = [];
      const used = new Set<string>();

      let added = true;
      while (selected.length < targetCount && added) {
        added = false;
        for (const cat of categoryOrder) {
          const bucket = byCategory.get(cat) || [];
          const takenInCat = selected.filter((x) => String(x.category || 'Other') === cat).length;
          if (takenInCat >= perCategoryCap) continue;
          const next = bucket.find((x) => !used.has(String(x.name || '')));
          if (!next) continue;
          selected.push(next);
          used.add(String(next.name || ''));
          added = true;
          if (selected.length >= targetCount) break;
        }
      }

      const scanLimit = status?.platform === 'ibkr' ? MAX_IBKR_SCAN_SYMBOLS : MAX_MT5_SCAN_SYMBOLS;
      const symbolsForAi = (selected.length > 0 ? selected : selectedRows)
        .map((s) => s.name)
        .filter(Boolean)
        .slice(0, scanLimit);

      const result = await Promise.race([
        api.smartEvaluate(symbolsForAi),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error('AI scan timeout')), 25000),
        ),
      ]);
      const scanned = (result.recommendations || []).map((r) => ({
        ...r,
        category: r.category || symbolByName.get(r.symbol)?.category || 'Other',
      }));
      setAnalyzedCount(scanned.length);
      if (result.scanned_at) setLastScan(result.scanned_at);
      const scannedBySymbol = new Map<string, Recommendation>();
      scanned.forEach((rec) => scannedBySymbol.set(String(rec.symbol || '').toUpperCase(), rec));

      const mergedRecommendations = selectedRows.map((row) => {
        const key = String(row.name || '').toUpperCase();
        return scannedBySymbol.get(key) || createPendingRecommendation(row);
      });
      setRecommendations(mergedRecommendations);
    } catch (e: any) {
      // Keep full MT5 list visible even when AI scoring degrades.
      const msg = String(e?.message || '');
      const lower = msg.toLowerCase();
      if (
        !lower.includes('timeout')
        && !lower.includes('failed to fetch')
        && !lower.includes('retrying automatically')
      ) {
        setError(msg);
      }
    } finally {
      scanningRef.current = false;
      setScanning(false);
    }
  }, [connected, status?.platform, status?.user_policy?.mode, selectedMarketSymbols, saveSelectedMarkets, allSymbols]);

  useEffect(() => {
    refreshPositions();
    const t = setInterval(refreshPositions, 5000);
    return () => clearInterval(t);
  }, [refreshPositions]);


  useEffect(() => {
    if (connected) {
      scanMarkets();
      const t = setInterval(scanMarkets, 300000);
      return () => clearInterval(t);
    }
  }, [connected, scanMarkets]);

  // Poll auto-trade status
  const refreshAutoTrade = useCallback(async () => {
    if (!connected) return;
    try {
      const st = await api.getAutoTradeStatus();
      setAutoTradeRunning(st.running);
      setAutoTradeLogs(st.recent_trades);
      setPosManagerRunning(st.position_manager_running ?? false);
      setPortfolioSnapshot(st.portfolio || null);
      if (st.gemini) {
        geminiLiveRef.current = true;
      }
      setGeminiState((prev) => {
        const g = st.gemini || {};
        const available = (g as any).available ?? prev.available;
        const degraded = (g as any).degraded ?? prev.degraded;
        const state = (g as any).state
          ?? (degraded ? 'degraded' : (available ? 'available' : prev.state || 'unavailable'));
        const cooldown = toFiniteNumber((g as any).cooldown_seconds);
        const credits = toFiniteNumber((g as any).credits_pct);
        return {
          available,
          degraded,
          last_error: (g as any).last_error ?? prev.last_error ?? null,
          state,
          cooldown_seconds: cooldown ?? prev.cooldown_seconds ?? 0,
          credits_pct: credits ?? prev.credits_pct ?? (available ? 100 : 0),
        };
      });
    } catch {}
  }, [connected]);

  useEffect(() => {
    refreshAutoTrade();
    const t = setInterval(refreshAutoTrade, 10000);
    return () => clearInterval(t);
  }, [refreshAutoTrade]);

  // Poll AI activity when auto-trade is running
  const refreshAIActivity = useCallback(async () => {
    if (!connected || !autoTradeRunning) return;
    try {
      const data = await api.getAIActivity(30);
      setAiActivity(data.live_activity || []);
      setPosManagerRunning(data.position_manager?.running ?? false);
    } catch {}
  }, [connected, autoTradeRunning]);

  useEffect(() => {
    if (autoTradeRunning) {
      refreshAIActivity();
      const t = setInterval(refreshAIActivity, 15000);
      return () => clearInterval(t);
    } else {
      setAiActivity([]);
    }
  }, [autoTradeRunning, refreshAIActivity]);

  const toggleAutoTrade = async () => {
    setAutoTradeLoading(true);
    try {
      if (autoTradeRunning) {
        await api.stopAutoTrade();
        setAutoTradeRunning(false);
      } else {
        await api.startAutoTrade();
        setAutoTradeRunning(true);
      }
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setAutoTradeLoading(false);
    }
  };

  const handleClosePosition = async (ticket: number) => {
    try {
      await api.closePosition(ticket);
      refreshPositions();
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
  const [tempSL, setTempSL] = useState('');
  const [tempTP, setTempTP] = useState('');

  // Auto-close when portfolio SL/TP is hit
  useEffect(() => {
    if (positions.length === 0) return;
    const total = positions.reduce((s, p) => s + p.profit, 0);
    if (portfolioSL !== null && total <= -Math.abs(portfolioSL)) {
      // Hit portfolio stop loss — close all
      setPortfolioSL(null);
      setPortfolioTP(null);
      (async () => {
        for (const p of positions) {
          try { await api.closePosition(p.ticket); } catch {}
        }
        refreshPositions();
        onRefresh();
      })();
    }
    if (portfolioTP !== null && total >= Math.abs(portfolioTP)) {
      // Hit portfolio take profit — close all
      setPortfolioSL(null);
      setPortfolioTP(null);
      (async () => {
        for (const p of positions) {
          try { await api.closePosition(p.ticket); } catch {}
        }
        refreshPositions();
        onRefresh();
      })();
    }
  }, [positions, portfolioSL, portfolioTP]);

  const [closingAll, setClosingAll] = useState(false);
  const handleSellAll = async () => {
    if (!confirm(`Close all ${positions.length} positions? This cannot be undone.`)) return;
    setClosingAll(true);
    try {
      let closed = 0;
      for (const p of positions) {
        try {
          await api.closePosition(p.ticket);
          closed++;
        } catch {}
      }
      refreshPositions();
      onRefresh();
    } catch (e: any) {
      setError(formatUiError(e));
    } finally {
      setClosingAll(false);
    }
  };

  const totalProfit = positions.reduce((s, p) => s + p.profit, 0);

  const draftMarketSet = useMemo(() => new Set(marketDraftSymbols), [marketDraftSymbols]);
  const defaultPopularSymbols = useMemo(() => pickEgyptPopularSymbols(allSymbols), [allSymbols]);
  const marketSelectorRows = useMemo(() => {
    const term = marketSearch.trim().toLowerCase();
    const rows = [...allSymbols];
    rows.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
    if (!term) return rows;
    return rows.filter((row) => {
      const n = String(row.name || '').toLowerCase();
      const d = String(row.description || '').toLowerCase();
      const c = String(row.category || '').toLowerCase();
      return n.includes(term) || d.includes(term) || c.includes(term);
    });
  }, [allSymbols, marketSearch]);

  const setMarketSymbolChecked = (symbol: string, checked: boolean) => {
    if (!symbol) return;
    const base = marketDraftSymbols.length > 0
      ? [...marketDraftSymbols]
      : [...defaultPopularSymbols];
    const has = base.includes(symbol);
    if (checked && !has) {
      setMarketDraftSymbols([...base, symbol]);
      return;
    }
    if (!checked && has) {
      setMarketDraftSymbols(base.filter((s) => s !== symbol));
    }
  };

  const resetToEgyptPopular = () => {
    const popular = pickEgyptPopularSymbols(allSymbols);
    setMarketDraftSymbols(popular);
  };

  // Category filter
  const filteredRecs = useMemo(
    () => (selectedCategory === 'All'
      ? recommendations
      : recommendations.filter((r) => (r.category || 'Other') === selectedCategory)),
    [selectedCategory, recommendations],
  );
  const actionableRecs = useMemo(
    () => filteredRecs.filter((r) => r.signal.action === 'BUY' || r.signal.action === 'SELL'),
    [filteredRecs],
  );
  const holdRecs = useMemo(
    () => filteredRecs.filter((r) => r.signal.action === 'HOLD'),
    [filteredRecs],
  );
  const visibleHoldRecs = useMemo(
    () => holdRecs.slice(0, visibleMarketRows),
    [holdRecs, visibleMarketRows],
  );

  useEffect(() => {
    setVisibleMarketRows(60);
  }, [selectedCategory, recommendations.length]);

  // Count categories for tabs
  const categoryCounts: Record<string, number> = { All: recommendations.length };
  recommendations.forEach((r) => {
    const cat = r.category || 'Other';
    categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
  });
  const categoryOrder = ['All', 'Stocks', 'Crypto', 'Indices', 'Commodities', 'Forex', 'Other'];
  const availableCategories = categoryOrder.filter((c) => (categoryCounts[c] || 0) > 0);

  const liveTickSymbols = useMemo(() => {
    const hot = actionableRecs.slice(0, 25).map((r) => r.symbol);
    const warm = visibleHoldRecs.slice(0, 80).map((r) => r.symbol);
    return Array.from(new Set([...hot, ...warm].filter(Boolean)));
  }, [actionableRecs, visibleHoldRecs]);

  // Live prices in one bulk request so bid/ask updates feel instant without request storms.
  useEffect(() => {
    if (!connected || recommendations.length === 0 || scanning) {
      setLiveTicks({});
      return;
    }
    if (liveTickSymbols.length === 0) {
      setLiveTicks({});
      return;
    }
    let cancelled = false;
    const refreshTicks = async () => {
      try {
        const resp = await api.getTicks(liveTickSymbols);
        if (!cancelled) {
          setLiveTicks(resp.ticks || {});
        }
      } catch {
        // keep previous ticks on transient errors
      }
    };
    refreshTicks();
    const t = setInterval(refreshTicks, 1500);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [connected, recommendations.length, scanning, liveTickSymbols]);

  // Not connected state
  if (!connected) {
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
          <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>Welcome to Your Trading Assistant</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.7, marginBottom: 24 }}>
            This app uses AI to analyze currency and commodity markets, then gives you
            simple buy/sell recommendations. You just click a button to trade.
          </p>
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
                { step: '1', text: 'Connect your MT5 demo account' },
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
          <a href="#/connection" className="btn btn-primary" style={{
            textDecoration: 'none',
            fontSize: 15,
            padding: '14px 32px',
          }}>
            Get Started - Connect Account
          </a>
        </div>
      </div>
    );
  }

  return (
    <div>
      <ConnectionBar status={status} onRefresh={onRefresh} />

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

      {portfolioSnapshot && (
        <div className="card mt-4" style={{ padding: '16px 20px' }}>
          <div className="flex justify-between items-start" style={{ marginBottom: 12 }}>
            <div>
              <div style={{ fontSize: 15, fontWeight: 700 }}>Portfolio Guard</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                Margin, exposure, and Gemini advisory health for the current book.
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Gemini</div>
              <div style={{ fontWeight: 700, color: geminiState.degraded ? 'var(--accent-yellow)' : geminiState.available ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                {geminiState.state === 'cooldown'
                  ? 'Cooldown'
                  : geminiState.degraded
                    ? 'Degraded'
                    : geminiState.available
                      ? 'Online'
                      : 'Offline'}
              </div>
              <div style={{ marginTop: 6, width: 220, marginLeft: 'auto' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)' }}>
                  <span>Gemini credits</span>
                  <span>{Math.max(0, Math.min(100, Number(geminiState.credits_pct || 0))).toFixed(0)}%</span>
                </div>
                <div style={{ marginTop: 4, height: 8, borderRadius: 999, background: 'var(--bg-primary)', overflow: 'hidden' }}>
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
                {Number(geminiState.cooldown_seconds || 0) > 0 && (
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>
                    Cooldown remaining: {formatCooldown(geminiState.cooldown_seconds)}
                  </div>
                )}
              </div>
              {geminiErrorSummary && (
                <div style={{ fontSize: 10, color: 'var(--text-muted)', maxWidth: 260, whiteSpace: 'normal' }}>
                  {geminiErrorSummary}
                </div>
              )}
              {geminiState.last_error && (
                <details style={{ marginTop: 4, maxWidth: 260 }}>
                  <summary style={{ fontSize: 10, color: 'var(--text-muted)', cursor: 'pointer' }}>Details</summary>
                  <div
                    style={{
                      fontSize: 10,
                      color: 'var(--text-muted)',
                      marginTop: 4,
                      whiteSpace: 'normal',
                      wordBreak: 'break-word',
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {geminiState.last_error}
                  </div>
                </details>
              )}
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 12 }}>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Margin Utilization</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: (portfolioSnapshot.margin_utilization_pct || 0) > 18 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                {formatPct(portfolioSnapshot.margin_utilization_pct)}
              </div>
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Free Margin</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: (portfolioSnapshot.free_margin_pct || 0) < 60 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                {formatPct(portfolioSnapshot.free_margin_pct)}
              </div>
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>USD Beta</div>
              <div style={{ fontSize: 18, fontWeight: 700 }}>{formatPct(portfolioSnapshot.usd_beta_exposure_pct)}</div>
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>Stock Exposure</div>
              <div style={{ fontSize: 18, fontWeight: 700 }}>{formatPct(portfolioSnapshot.stocks_equity_exposure_pct)}</div>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 8 }}>Exposure By Category</div>
              {Object.keys(portfolioSnapshot.exposure_by_category || {}).length === 0 ? (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>No active exposure.</div>
              ) : Object.entries(portfolioSnapshot.exposure_by_category).map(([key, value]) => (
                <div key={key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 4 }}>
                  <span>{key}</span>
                  <span style={{ fontWeight: 600 }}>{formatPct(value)}</span>
                </div>
              ))}
            </div>
            <div style={{ background: 'var(--bg-secondary)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 8 }}>Exposure By Symbol</div>
              {Object.keys(portfolioSnapshot.exposure_by_symbol || {}).length === 0 ? (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>No active exposure.</div>
              ) : Object.entries(portfolioSnapshot.exposure_by_symbol).slice(0, 6).map(([key, value]) => (
                <div key={key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 4 }}>
                  <span>{key}</span>
                  <span style={{ fontWeight: 600 }}>{formatPct(value)}</span>
                </div>
              ))}
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
                  ? `AI is scanning, trading & managing positions autonomously${posManagerRunning ? ' • Position Manager active' : ''}`
                  : 'Turn on to let the AI trade and manage positions for you'
                }
              </div>
            </div>
          </div>
          <button
            className={`btn ${autoTradeRunning ? 'btn-danger' : 'btn-success'}`}
            onClick={toggleAutoTrade}
            disabled={autoTradeLoading || !!status?.panic_stop}
            style={{ fontSize: 14, padding: '10px 24px', fontWeight: 600 }}
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

        {/* Auto-trade activity log */}
        {autoTradeRunning && autoTradeLogs.length > 0 && (
          <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--text-muted)' }}>
              Recent Auto-Trades
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 150, overflowY: 'auto' }}>
              {autoTradeLogs.slice().reverse().slice(0, 10).map((log, i) => (
                <div key={i} className="flex items-start gap-2" style={{ fontSize: 12, padding: '4px 0' }}>
                  {log.success ? (
                    <CheckCircle size={12} style={{ color: 'var(--accent-green)', flexShrink: 0 }} />
                  ) : (
                    <XCircle size={12} style={{ color: 'var(--accent-red)', flexShrink: 0 }} />
                  )}
                  <span style={{ fontWeight: 600 }}>
                    {getSymbolEmoji(log.symbol)} {log.action} {getSymbolName(log.symbol)}
                  </span>
                  <span className="text-muted">
                    ({Math.round(log.confidence * 100)}%)
                  </span>
                  {log.quality_score != null && (
                    <span className="text-muted">
                      Q {Math.round(((log.quality_score && log.quality_score > 0) ? log.quality_score : log.confidence) * 100)}%
                    </span>
                  )}
                  <span style={{ color: log.success ? 'var(--accent-green)' : 'var(--accent-red)', flex: 1, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                    {log.detail}
                  </span>
                  <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11, flexShrink: 0 }}>
                    {new Date(log.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
              ))}
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
            const totalInvested = positions.reduce((s, p) => s + Math.abs(p.volume * p.price_open * 100), 0); // approximate
            const profitPct = account?.balance ? (totalProfit / account.balance * 100) : 0;
            const profitColor = totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
            const winning = positions.filter(p => p.profit > 0).length;
            const losing = positions.filter(p => p.profit < 0).length;

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
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
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
                      onClick={() => {
                        setPortfolioSL(tempSL ? parseFloat(tempSL) : null);
                        setPortfolioTP(tempTP ? parseFloat(tempTP) : null);
                        setEditingPortfolioTargets(false);
                      }}
                      style={{ fontSize: 11, padding: '4px 12px', background: 'var(--accent-blue)', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer' }}
                    >
                      Set
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
                    {portfolioSL !== null && (
                      <span style={{ color: 'var(--accent-red)', background: 'rgba(239,68,68,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Stop Loss: -${Math.abs(portfolioSL)} {totalProfit <= -Math.abs(portfolioSL) * 0.8 ? '⚠️' : ''}
                      </span>
                    )}
                    {portfolioTP !== null && (
                      <span style={{ color: 'var(--accent-green)', background: 'rgba(34,197,94,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Take Profit: +${Math.abs(portfolioTP)} {totalProfit >= Math.abs(portfolioTP) * 0.8 ? '🎯' : ''}
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
                      {portfolioSL !== null || portfolioTP !== null ? '✏️ Edit Targets' : '🎯 Set Portfolio SL/TP'}
                    </button>
                    {(portfolioSL !== null || portfolioTP !== null) && (
                      <button
                        onClick={() => { setPortfolioSL(null); setPortfolioTP(null); }}
                        style={{ fontSize: 11, padding: '2px 8px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer' }}
                      >
                        Clear
                      </button>
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
                leverage={account?.leverage || 100}
              />
            ))}
          </div>
        </div>
      )}

      {/* AI Brain Activity — shows when auto-trade is on */}
      {false && autoTradeRunning && aiActivity.length > 0 && (
        <div className="mt-4">
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Zap size={16} style={{ color: 'var(--accent-blue)' }} />
            AI Brain Activity
            {posManagerRunning && (
              <span className="badge badge-blue" style={{ fontSize: 10, fontWeight: 500 }}>
                Position Manager Active
              </span>
            )}
          </h3>
          <div className="card" style={{ padding: 0, maxHeight: 260, overflowY: 'auto' }}>
            {aiActivity.map((activity, i) => {
              const actionIcons: Record<string, string> = {
                trailing_stop: '📈', breakeven: '🛡️', close_reversal: '🔄',
                close_timeout: '⏰', average_down: '📉', scanner: '🔍',
              };
              const actionColors: Record<string, string> = {
                trailing_stop: 'var(--accent-green)', breakeven: 'var(--accent-blue)',
                close_reversal: 'var(--accent-red)', close_timeout: 'var(--accent-red)',
                average_down: 'var(--accent-yellow, #f59e0b)', scanner: 'var(--accent-blue)',
              };
              const icon = actionIcons[activity.action] || (activity.source === 'scanner' ? '🔍' : '🤖');
              const color = actionColors[activity.action] || 'var(--text-secondary)';
              const timeStr = new Date(activity.timestamp * 1000).toLocaleTimeString();

              return (
                <div
                  key={`${activity.timestamp}-${activity.ticket ?? 0}-${i}`}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '10px 14px', fontSize: 12,
                    borderBottom: i < aiActivity.length - 1 ? '1px solid var(--border)' : 'none',
                  }}
                >
                  <span style={{ fontSize: 16, flexShrink: 0 }}>{icon}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontWeight: 600, color }}>{getSymbolEmoji(activity.symbol)} {getSymbolName(activity.symbol)}</span>
                      <span className="badge" style={{ fontSize: 9, background: color, color: '#fff', padding: '1px 6px' }}>
                        {activity.action.replace('_', ' ')}
                      </span>
                    </div>
                    <div className="text-muted" style={{ fontSize: 11, marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {activity.detail ?? ''}
                    </div>
                  </div>
                  <div style={{ textAlign: 'right', flexShrink: 0 }}>
                    {activity.profit != null && activity.profit !== 0 && (
                      <div style={{ fontFamily: 'monospace', fontWeight: 600, fontSize: 11, color: activity.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                        {activity.profit >= 0 ? '+' : ''}{activity.profit.toFixed(2)}
                      </div>
                    )}
                    <div className="text-muted" style={{ fontSize: 10 }}>{timeStr}</div>
                  </div>
                </div>
              );
            })}
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
                ? `${status?.platform === 'ibkr' ? 'IBKR' : 'MT5'} available ${availableMarketCount} symbols \u2022 AI analyzed ${analyzedCount} \u2022 Last checked: ${new Date(lastScan * 1000).toLocaleTimeString()}`
                : 'Click "Scan All" to analyze every available market'
              }
            </p>
          </div>
          <div className="flex gap-2">
            <button
              className="btn btn-secondary"
              onClick={() => setShowMarketSelector((v) => !v)}
              style={{ fontSize: 12 }}
            >
              {showMarketSelector ? 'Hide Market Picker' : 'Add Markets'}
            </button>
            <button className="btn btn-primary" onClick={scanMarkets} disabled={scanning} style={{ fontSize: 13, padding: '10px 20px' }}>
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
                Default mode: <strong>{status?.platform === 'ibkr' ? 'IBKR popular liquid symbols' : 'Popular in Egypt'}</strong>.
                {' '}You can add/remove extra markets below.
                {status?.platform === 'ibkr' ? ' IBKR catalog symbols are also available in this list.' : ''}
                {' '}Selected: <strong>{marketDraftSymbols.length || defaultPopularSymbols.length}</strong>
              </div>
              <div className="flex gap-2">
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={() => setShowMarketSelector(false)}
                >
                  Close
                </button>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={() => {
                    saveSelectedMarkets(marketDraftSymbols.length > 0 ? marketDraftSymbols : defaultPopularSymbols);
                    setShowMarketSelector(false);
                  }}
                >
                  Save Selection
                </button>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={resetToEgyptPopular}
                >
                  Reset to Popular
                </button>
                <button
                  className="btn btn-primary btn-sm"
                  onClick={async () => {
                    const toSave = marketDraftSymbols.length > 0 ? marketDraftSymbols : defaultPopularSymbols;
                    saveSelectedMarkets(toSave);
                    setShowMarketSelector(false);
                    await scanMarkets(toSave);
                  }}
                  disabled={scanning}
                >
                  Save & Scan
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
            <div style={{ maxHeight: 220, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 8, padding: 8 }}>
              {marketSelectorRows.map((row) => {
                const symbol = String(row.name || '');
                const checked = draftMarketSet.has(symbol) || (marketDraftSymbols.length === 0 && defaultPopularSymbols.includes(symbol));
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
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{row.category || 'Other'}</span>
                  </label>
                );
              })}
            </div>
          </div>
          </div>
        )}

        {error && !error.toLowerCase().includes('timeout') && !error.toLowerCase().includes('failed to fetch') && (
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
                  {actionableRecs.map((rec, i) => (
                    <TradeCard
                      key={`${rec.symbol}-${i}`}
                      rec={rec}
                      onExecuted={refreshPositionsBurst}
                      currency={currency}
                      liveTick={liveTicks[rec.symbol]}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* All other symbols - with Buy button */}
            {holdRecs.length > 0 && (
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Clock size={14} />
                  All Markets ({holdRecs.length})
                </div>
                <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                  {visibleHoldRecs.map((rec, i) => (
                    <MarketRow
                      key={`hold-${i}`}
                      rec={rec}
                      currency={currency}
                      onExecuted={refreshPositionsBurst}
                      isLast={i === visibleHoldRecs.length - 1}
                      liveTick={liveTicks[rec.symbol]}
                    />
                  ))}
                </div>
                {holdRecs.length > visibleHoldRecs.length && (
                  <div style={{ padding: 12, borderTop: '1px solid var(--border)', textAlign: 'center' }}>
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => setVisibleMarketRows((v) => Math.min(v + 60, holdRecs.length))}
                    >
                      Load More ({holdRecs.length - visibleHoldRecs.length} remaining)
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
