import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useRef } from 'react';
import { api } from '../utils/api';
import type { TradeHistoryResponse, TradeHistoryItem, ResearchStatusResponse } from '../types';

function formatMoney(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  const sign = value > 0 ? '+' : '';
  return `${sign}$${value.toFixed(2)}`;
}

function formatPlainMoney(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  return `$${value.toFixed(2)}`;
}

function formatDurationMins(mins?: number | null) {
  if (mins == null || Number.isNaN(mins)) return '-';
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.floor(mins % 60);
  return `${h}h ${m}m`;
}

function formatDateTime(ts?: number | null) {
  if (ts == null || Number.isNaN(ts) || ts <= 0) return '-';
  return new Date(ts * 1000).toLocaleString();
}

function getTradeReason(trade: TradeHistoryItem) {
  if (trade.exit_reason) return trade.exit_reason;
  if (trade.decision_reason) return trade.decision_reason;
  if (trade.trade_reasons && trade.trade_reasons.length > 0) return trade.trade_reasons[0];
  return trade.signal_reason || trade.risk_reason || '-';
}

function formatTargetValue(started: number | null | undefined, delta: number | null | undefined, isTp: boolean) {
  if (started == null || delta == null || Number.isNaN(started) || Number.isNaN(delta)) return '-';
  const value = isTp ? started + delta : Math.max(0, started - delta);
  return `$${value.toFixed(2)}`;
}

function formatInactiveReason(reason?: string | null) {
  const value = String(reason || '').trim();
  if (!value) return '';
  if (value === 'no_approved_model') return 'No approved healthy meta-model yet';
  if (value === 'approved_model_not_loaded') return 'Approved model exists but is not loaded';
  if (value === 'candidate_pending_or_not_promoted') return 'A candidate exists but has not been promoted yet';
  if (value === 'status_timeout') return 'Status refresh timed out';
  return value.replace(/_/g, ' ');
}

function getTradeOriginMeta(trade: TradeHistoryItem) {
  if (trade.origin === 'mt5_connected_account') {
    return {
      label: 'Connected MT5 Account',
      color: 'var(--accent-green)',
      detail: 'This trade was read directly from the currently connected MT5 account history.',
    };
  }
  if (trade.origin === 'broker_imported') {
    return {
      label: 'Imported From Broker',
      color: 'var(--accent-orange)',
      detail: 'This trade came from broker history, not a locally logged app entry.',
    };
  }
  return {
    label: 'Opened By App',
    color: 'var(--accent-blue)',
    detail: 'This trade has a local app-side order record.',
  };
}

const EMPTY_HISTORY: TradeHistoryResponse = {
  summary: {
    total_trades: 0,
    closed_trades: 0,
    open_trades: 0,
    winning_trades: 0,
    losing_trades: 0,
    breakeven_trades: 0,
    win_rate_pct: 0,
    total_profit_usd: 0,
    avg_profit_per_closed_trade_usd: 0,
    total_started_capital_usd: 0,
    roi_pct: null,
    best_trade_usd: 0,
    worst_trade_usd: 0,
  },
  trades: [],
};

export default function TradeHistory({ connected }: { connected: boolean }) {
  const [history, setHistory] = useState<TradeHistoryResponse>(EMPTY_HISTORY);
  const [research, setResearch] = useState<ResearchStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [researchLoading, setResearchLoading] = useState(false);
  const researchLoadingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);
  const [historyView, setHistoryView] = useState<'closed' | 'all'>('closed');
  const [assetFilter, setAssetFilter] = useState<'all' | 'commodities' | 'stocks'>('all');
  const [expandedAudits, setExpandedAudits] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const historyData = await api.getTradeHistory(limit, historyView);
      setHistory(historyData);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Trade history could not be loaded.';
      setError(message);
      setHistory((current) => current.trades.length > 0 ? current : EMPTY_HISTORY);
    } finally {
      setLoading(false);
    }

  }, [historyView, limit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const refreshResearch = useCallback(async () => {
    if (researchLoadingRef.current) return;
    researchLoadingRef.current = true;
    setResearchLoading(true);
    try {
      setResearch(await api.getResearchStatus());
    } catch {
      setResearch(null);
    } finally {
      researchLoadingRef.current = false;
      setResearchLoading(false);
    }
  }, []);

  const pendingAnalysisCount = useMemo(
    () => history.trades.filter((trade) => trade.status === 'closed' && trade.ticket && !trade.post_analysis).length,
    [history.trades],
  );

  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === 'visible' && historyView === 'closed') {
        void refresh();
      }
    };
    const t = setInterval(tick, pendingAnalysisCount > 0 ? 12000 : 45000);
    return () => clearInterval(t);
  }, [historyView, pendingAnalysisCount, refresh]);

  const summary = history.summary;
  const commoditySet = useMemo(() => new Set([
    'XAUUSD', 'XAGUSD', 'XPDUSD', 'XPTUSD', 'XAUEUR', 'GOLD', 'SILVER', 'WTI', 'BRENT',
    'AGQ', 'AGMI', 'AAAU', 'IAUM', 'GLDM', 'GLL', 'PHYS',
  ]), []);

  const visibleTrades = useMemo(() => {
    if (assetFilter === 'all') return history.trades;
    if (assetFilter === 'commodities') {
      return history.trades.filter((t) => commoditySet.has(String(t.symbol || '').toUpperCase()));
    }
    return history.trades.filter((t) => !commoditySet.has(String(t.symbol || '').toUpperCase()));
  }, [assetFilter, history.trades, commoditySet]);

  const getWhyLines = useCallback((trade: TradeHistoryItem) => {
    const lines = [
      trade.signal_reason ? `Signal: ${trade.signal_reason}` : '',
      trade.risk_reason ? `Risk: ${trade.risk_reason}` : '',
      trade.decision_reason ? `Decision: ${trade.decision_reason}` : '',
      trade.exit_reason ? `Exit: ${trade.exit_reason}` : '',
      ...(trade.trade_reasons || []).map((reason) => `Reason: ${reason}`),
      ...(trade.post_analysis?.root_causes || []).map((reason) => `Cause: ${reason}`),
    ].filter((line, index, all) => line && all.indexOf(line) === index);
    if (lines.length > 0) return lines;
    if (trade.origin === 'broker_imported') {
      return ['Imported from MT5 broker history; no local signal/risk journal was found for this ticket.'];
    }
    return ['No detailed local signal/risk reason was recorded for this trade.'];
  }, []);

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Trade History</h2>
            <p>
              Performance summary and per-trade analysis
              {!connected ? ' (showing local history while MT5 is disconnected)' : ''}
            </p>
          </div>
          <div className="flex gap-2">
            <select
              className="form-input"
              style={{ width: 120 }}
              value={limit}
              onChange={(e) => setLimit(parseInt(e.target.value))}
            >
              <option value={50}>50 trades</option>
              <option value={100}>100 trades</option>
              <option value={200}>200 trades</option>
              <option value={500}>500 trades</option>
              <option value={1000}>1,000 trades</option>
              <option value={2000}>2,000 trades</option>
              <option value={5000}>5,000 trades</option>
            </select>
            <select
              className="form-input"
              style={{ width: 160 }}
              value={historyView}
              onChange={(e) => setHistoryView(e.target.value as 'closed' | 'all')}
            >
              <option value="closed">Closed Only</option>
              <option value="all">All MT5 Deals (slow)</option>
            </select>
            <select
              className="form-input"
              style={{ width: 160 }}
              value={assetFilter}
              onChange={(e) => setAssetFilter(e.target.value as 'all' | 'commodities' | 'stocks')}
            >
              <option value="all">All Assets</option>
              <option value="commodities">Commodities Only</option>
              <option value="stocks">Stocks/Other Only</option>
            </select>
            <button className="btn btn-secondary" onClick={() => void refresh()} disabled={loading}>
              {loading ? <span className="loading-spinner" /> : <RefreshCw size={14} />}
              Refresh
            </button>
          </div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 12, marginBottom: 12 }}>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Total Profit</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: summary.total_profit_usd >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {formatMoney(summary.total_profit_usd)}
          </div>
        </div>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Total Trades</div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{summary.total_trades}</div>
        </div>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Win Rate</div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{summary.win_rate_pct.toFixed(1)}%</div>
        </div>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>ROI</div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{summary.roi_pct == null ? '-' : `${summary.roi_pct.toFixed(2)}%`}</div>
        </div>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Best Trade</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--accent-green)' }}>{formatMoney(summary.best_trade_usd)}</div>
        </div>
        <div className="card" style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Worst Trade</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--accent-red)' }}>{formatMoney(summary.worst_trade_usd)}</div>
        </div>
      </div>
      <div className="card" style={{ padding: '10px 14px', marginBottom: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          Meta model{' '}
          <span style={{ color: research == null ? 'var(--text-muted)' : research.meta_model_active ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
            {research == null ? 'Not loaded' : research.meta_model_active ? 'Active' : 'Inactive'}
          </span>
          {research?.meta_model_active_version ? ` • ${research.meta_model_active_version}` : ''}
          {!research?.meta_model_active && research?.meta_model_inactive_reason
            ? ` • ${formatInactiveReason(research.meta_model_inactive_reason)}`
            : ''}
          {research?.incremental_training?.local_closed_count != null
            ? ` • training set has ${research.incremental_training.local_closed_count} closed trades`
            : research?.incremental_training?.last_closed_count != null
              ? ` • incremental trainer last saw ${research.incremental_training.last_closed_count} closed trades`
            : ''}
          {researchLoading ? ' • loading model status in background' : ''}
          {pendingAnalysisCount > 0 ? ` • ${pendingAnalysisCount} trade analyses queued` : ''}
          <button
            type="button"
            className="btn btn-secondary"
            style={{ marginLeft: 10, padding: '4px 8px', fontSize: 11 }}
            onClick={() => void refreshResearch()}
            disabled={researchLoading}
          >
            {researchLoading ? 'Loading...' : 'Load model status'}
          </button>
        </div>
      </div>

      {error && (
        <div
          className="card"
          style={{ padding: '10px 14px', marginBottom: 12, borderColor: 'rgba(239, 68, 68, 0.45)' }}
        >
          <div style={{ fontSize: 12, color: 'var(--accent-red)', fontWeight: 600 }}>
            Trade history request failed
          </div>
          <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
            {error}. The table keeps the last loaded trades when available.
          </div>
        </div>
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {loading && history.trades.length === 0 ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : visibleTrades.length === 0 ? (
          <div className="empty-state">
            {error ? 'Trade history could not be loaded. Check the backend port and retry.' : 'No trade history yet.'}
          </div>
        ) : (
          <div style={{ maxHeight: 'calc(100vh - 300px)', overflowY: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Trade</th>
                  <th>Started Budget</th>
                  <th>Ended Budget</th>
                  <th>P/L</th>
                  <th>Entry / Filled Value</th>
                  <th>SL / TP Target</th>
                  <th>Duration</th>
                  <th>Reason</th>
                  <th>Analysis</th>
                </tr>
              </thead>
              <tbody>
                {visibleTrades.map((trade, idx) => {
                  const tradeKey = `${trade.ticket || 'trade'}-${trade.opened_at}-${idx}`;
                  const auditTrail = trade.audit_trail || [];
                  const auditExpanded = !!expandedAudits[tradeKey];
                  const visibleAuditTrail = auditExpanded ? auditTrail : auditTrail.slice(0, 3);
                  return (
                  <tr key={tradeKey}>
                    <td className="text-sm text-muted">{formatDateTime(trade.opened_at)}</td>
                    <td>
                      {(() => {
                        const originMeta = getTradeOriginMeta(trade);
                        return (
                          <>
                      <div style={{ fontWeight: 600 }}>{trade.action} {trade.symbol}</div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        #{trade.ticket ?? '-'} • {trade.status}
                      </div>
                            <div style={{ fontSize: 10, marginTop: 4, color: originMeta.color, fontWeight: 600 }}>
                              {originMeta.label}
                            </div>
                          </>
                        );
                      })()}
                    </td>
                    <td>{formatPlainMoney(trade.started_with_usd)}</td>
                    <td>{formatPlainMoney(trade.ended_with_usd)}</td>
                    <td>
                      <div style={{ color: (trade.profit_usd || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                        {formatMoney(trade.profit_usd)}
                      </div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        {trade.profit_pct == null ? '-' : `${trade.profit_pct >= 0 ? '+' : ''}${trade.profit_pct.toFixed(2)}%`}
                      </div>
                    </td>
                    <td>
                      <div>{trade.entry_price == null ? '-' : trade.entry_price.toFixed(5)}</div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        filled {formatPlainMoney(trade.entry_market_value_usd)}
                      </div>
                      {trade.close_price != null && (
                        <div className="text-muted" style={{ fontSize: 11 }}>
                          close {trade.close_price.toFixed(5)}
                        </div>
                      )}
                      {trade.entry_price == null && trade.close_price != null && (
                        <div className="text-muted" style={{ fontSize: 10 }}>
                          Entry was not persisted for this reconstructed broker outcome.
                        </div>
                      )}
                    </td>
                    <td>
                      <div style={{ fontSize: 12, color: 'var(--accent-red)' }}>
                        SL {formatTargetValue(trade.started_with_usd, trade.sl_amount_usd, false)}
                        {trade.sl_pct_of_start == null ? '' : ` (${trade.sl_pct_of_start.toFixed(1)}%)`}
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--accent-green)' }}>
                        TP {formatTargetValue(trade.started_with_usd, trade.tp_amount_usd, true)}
                        {trade.tp_pct_of_start == null ? '' : ` (${trade.tp_pct_of_start.toFixed(1)}%)`}
                      </div>
                      <div className="text-muted" style={{ fontSize: 10 }}>
                        Price SL {trade.stop_loss == null ? '-' : trade.stop_loss.toFixed(5)} / TP {trade.take_profit == null ? '-' : trade.take_profit.toFixed(5)}
                      </div>
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>
                        {formatDurationMins(trade.duration_minutes)}
                      </div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        Opened: {formatDateTime(trade.opened_at)}
                      </div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        Closed: {formatDateTime(trade.closed_at)}
                      </div>
                    </td>
                    <td style={{ maxWidth: 380, whiteSpace: 'normal', wordBreak: 'break-word', fontSize: 12 }}>
                      <div>{getTradeReason(trade)}</div>
                      <div style={{ marginTop: 6, display: 'grid', gap: 3 }}>
                        <div style={{ color: 'var(--text-secondary)', fontSize: 10, fontWeight: 700 }}>
                          Why this trade happened
                        </div>
                        {getWhyLines(trade).slice(0, 5).map((line, lineIdx) => (
                          <div
                            key={`${tradeKey}-why-${lineIdx}`}
                            className="text-muted"
                            style={{ fontSize: 10, lineHeight: 1.35 }}
                          >
                            {line}
                          </div>
                        ))}
                      </div>
                      {trade.trade_reasons && trade.trade_reasons.length > 1 && (
                        <div className="text-muted" style={{ fontSize: 11, marginTop: 3 }}>
                          {trade.trade_reasons.slice(1).join(' • ')}
                        </div>
                      )}
                      {trade.gemini_response && (
                        <div style={{ fontSize: 11, marginTop: 4, color: 'var(--accent-blue)' }}>
                          Gemini: {trade.gemini_response}
                        </div>
                      )}
                      {trade.meta_model && (
                        <div className="text-muted" style={{ fontSize: 11, marginTop: 4 }}>
                          Meta model ({trade.meta_model.version_id || 'active'}): p={Math.round((trade.meta_model.profit_probability || 0) * 100)}%
                          {trade.meta_model.changed_decision ? ' • changed decision' : ''}
                          {trade.meta_model.blocked ? ' • blocked' : ''}
                        </div>
                      )}
                    </td>
                    <td style={{ minWidth: 220, maxWidth: 320 }}>
                      {trade.status !== 'closed' || !trade.ticket ? (
                        <span className="text-muted" style={{ fontSize: 11 }}>Available after close</span>
                      ) : (
                        <div style={{ display: 'grid', gap: 6 }}>
                          {(() => {
                            const executedEntry = trade.entry_price ?? trade.post_analysis?.executed_plan?.entry_price ?? null;
                            const executedSl = trade.stop_loss ?? trade.post_analysis?.executed_plan?.stop_loss ?? null;
                            const executedTp = trade.take_profit ?? trade.post_analysis?.executed_plan?.take_profit ?? null;
                            const executedSlUsd = trade.sl_amount_usd ?? trade.post_analysis?.executed_plan?.sl_amount_usd ?? null;
                            const executedTpUsd = trade.tp_amount_usd ?? trade.post_analysis?.executed_plan?.tp_amount_usd ?? null;
                            const executedSlPct = trade.sl_pct_of_start ?? trade.post_analysis?.executed_plan?.sl_pct_of_start ?? null;
                            const executedTpPct = trade.tp_pct_of_start ?? trade.post_analysis?.executed_plan?.tp_pct_of_start ?? null;

                            return (
                              <div style={{ fontSize: 10, whiteSpace: 'normal', wordBreak: 'break-word' }}>
                                <div style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Executed trade plan</div>
                                <div className="text-muted">
                                  Entry {executedEntry == null ? '-' : executedEntry.toFixed(5)}
                                  {' • '}SL {executedSl == null ? '-' : executedSl.toFixed(5)}
                                  {' • '}TP {executedTp == null ? '-' : executedTp.toFixed(5)}
                                </div>
                                {(executedSlUsd != null || executedTpUsd != null || executedSlPct != null || executedTpPct != null) && (
                                  <div className="text-muted">
                                    Risk {executedSlUsd == null ? '-' : formatMoney(-Math.abs(executedSlUsd))}
                                    {executedSlPct == null ? '' : ` (${executedSlPct.toFixed(1)}%)`}
                                    {' • '}Reward {executedTpUsd == null ? '-' : formatMoney(executedTpUsd)}
                                    {executedTpPct == null ? '' : ` (${executedTpPct.toFixed(1)}%)`}
                                  </div>
                                )}
                              </div>
                            );
                          })()}
                          <div className="text-muted" style={{ fontSize: 11 }}>
                            {trade.post_analysis ? 'Analysis ready' : 'Analysis queued automatically'}
                          </div>
                          {trade.post_analysis?.summary && (
                            <div style={{ fontSize: 11, color: 'var(--text-secondary)', whiteSpace: 'normal', wordBreak: 'break-word' }}>
                              {trade.post_analysis.summary}
                            </div>
                          )}
                          <div className="text-muted" style={{ fontSize: 10, whiteSpace: 'normal', wordBreak: 'break-word' }}>
                            {getTradeOriginMeta(trade).detail}
                          </div>
                          {auditTrail.length > 0 && (
                            <div style={{ display: 'grid', gap: 4, marginTop: 4 }}>
                              <div style={{ fontSize: 10, color: 'var(--text-secondary)', fontWeight: 600 }}>
                                Trade audit trail
                              </div>
                              {visibleAuditTrail.map((item, auditIdx) => (
                                <div
                                  key={`${item.time || auditIdx}-${item.type}-${auditIdx}`}
                                  className="text-muted"
                                  style={{ fontSize: 10, whiteSpace: 'normal', wordBreak: 'break-word' }}
                                >
                                  <span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>
                                    {item.title}
                                  </span>
                                  {item.time ? ` • ${formatDateTime(item.time)}` : ''}
                                  <br />
                                  {item.detail}
                                </div>
                              ))}
                              {auditTrail.length > 3 && (
                                <button
                                  type="button"
                                  className="btn btn-secondary"
                                  style={{ width: 'fit-content', padding: '4px 8px', fontSize: 10 }}
                                  onClick={() => setExpandedAudits((prev) => ({ ...prev, [tradeKey]: !prev[tradeKey] }))}
                                >
                                  {auditExpanded ? 'Show less' : `Read more (${auditTrail.length - 3})`}
                                </button>
                              )}
                            </div>
                          )}
                          {trade.post_analysis?.quality_scores && (
                            <div className="text-muted" style={{ fontSize: 10 }}>
                              Q: exec {Math.round((trade.post_analysis.quality_scores.execution_quality || 0) * 100)}%
                              {' • '}risk {Math.round((trade.post_analysis.quality_scores.risk_discipline || 0) * 100)}%
                              {' • '}timing {Math.round((trade.post_analysis.quality_scores.timing_quality || 0) * 100)}%
                            </div>
                          )}
                          {trade.post_analysis?.diagnostics && (
                            <div className="text-muted" style={{ fontSize: 10, whiteSpace: 'normal', wordBreak: 'break-word' }}>
                              Flags:
                              {trade.post_analysis.diagnostics.late_entry_flag ? ' late-entry' : ''}
                              {trade.post_analysis.diagnostics.spread_stress_flag ? ' spread-stress' : ''}
                              {trade.post_analysis.diagnostics.trend_conflict_flag ? ' trend-conflict' : ''}
                              {trade.post_analysis.diagnostics.risk_overexposure_flag ? ' overexposure' : ''}
                              {trade.post_analysis.diagnostics.stop_too_tight_flag ? ' stop-tight' : ''}
                              {trade.post_analysis.diagnostics.target_too_far_flag ? ' target-far' : ''}
                              {trade.post_analysis.diagnostics.news_shock_flag ? ' news-shock' : ''}
                              {trade.post_analysis.diagnostics.execution_delay_flag ? ' execution-delay' : ''}
                            </div>
                          )}
                          {trade.post_analysis?.recommendation && (
                            <div className="text-muted" style={{ fontSize: 10 }}>
                              Suggested next trade: SL {Math.round((trade.post_analysis.recommendation.suggested_sl_pct_of_start || 0) * 100)}%
                              {' • '}TP {Math.round((trade.post_analysis.recommendation.suggested_tp_pct_of_start || 0) * 100)}%
                              {' • '}size adj {trade.post_analysis.recommendation.next_trade_size_adjustment_pct || 0}%
                            </div>
                          )}
                          {!trade.post_analysis && (
                            <div className="text-muted" style={{ fontSize: 10 }}>
                              Closed trades are analyzed automatically after they appear in history.
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
