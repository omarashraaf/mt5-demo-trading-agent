import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../utils/api';
import type { TradeHistoryResponse, TradeHistoryItem } from '../types';

function formatMoney(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  const sign = value > 0 ? '+' : '';
  return `${sign}$${value.toFixed(2)}`;
}

function formatDurationMins(mins?: number | null) {
  if (mins == null || Number.isNaN(mins)) return '-';
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.floor(mins % 60);
  return `${h}h ${m}m`;
}

function getTradeReason(trade: TradeHistoryItem) {
  return trade.exit_reason || trade.signal_reason || trade.risk_reason || '-';
}

function formatTargetValue(started: number | null | undefined, delta: number | null | undefined, isTp: boolean) {
  if (started == null || delta == null || Number.isNaN(started) || Number.isNaN(delta)) return '-';
  const value = isTp ? started + delta : Math.max(0, started - delta);
  return `$${value.toFixed(2)}`;
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
  const [loading, setLoading] = useState(false);
  const [limit, setLimit] = useState(100);

  const refresh = useCallback(async () => {
    if (!connected) return;
    setLoading(true);
    try {
      const data = await api.getTradeHistory(limit);
      setHistory(data);
    } finally {
      setLoading(false);
    }
  }, [connected, limit]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  if (!connected) {
    return (
      <div className="card">
        <h2>Trade History</h2>
        <p style={{ color: 'var(--text-muted)' }}>Connect to MT5 to view history analytics.</p>
      </div>
    );
  }

  const summary = history.summary;

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Trade History</h2>
            <p>Performance summary and per-trade analysis</p>
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
            </select>
            <button className="btn btn-secondary" onClick={refresh} disabled={loading}>
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

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {loading && history.trades.length === 0 ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : history.trades.length === 0 ? (
          <div className="empty-state">No trade history yet.</div>
        ) : (
          <div style={{ maxHeight: 'calc(100vh - 300px)', overflowY: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Trade</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>P/L</th>
                  <th>Entry</th>
                  <th>SL / TP Target</th>
                  <th>Duration</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {history.trades.map((trade, idx) => (
                  <tr key={`${trade.ticket || 'trade'}-${trade.opened_at}-${idx}`}>
                    <td className="text-sm text-muted">{new Date(trade.opened_at * 1000).toLocaleString()}</td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{trade.action} {trade.symbol}</div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        #{trade.ticket ?? '-'} • {trade.status}
                      </div>
                    </td>
                    <td>{formatMoney(trade.started_with_usd)}</td>
                    <td>{formatMoney(trade.ended_with_usd)}</td>
                    <td>
                      <div style={{ color: (trade.profit_usd || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                        {formatMoney(trade.profit_usd)}
                      </div>
                      <div className="text-muted" style={{ fontSize: 11 }}>
                        {trade.profit_pct == null ? '-' : `${trade.profit_pct >= 0 ? '+' : ''}${trade.profit_pct.toFixed(2)}%`}
                      </div>
                    </td>
                    <td>{trade.entry_price == null ? '-' : trade.entry_price.toFixed(5)}</td>
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
                    <td>{formatDurationMins(trade.duration_minutes)}</td>
                    <td style={{ maxWidth: 380, whiteSpace: 'normal', wordBreak: 'break-word', fontSize: 12 }}>
                      {getTradeReason(trade)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
