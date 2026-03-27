import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Filter } from 'lucide-react';
import { api } from '../utils/api';
import type { LogEntry, TradeHistoryResponse, TradeHistoryItem } from '../types';

const LOG_TYPES = ['all', 'signals', 'risk', 'orders', 'connections', 'errors'];

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [tradeHistory, setTradeHistory] = useState<TradeHistoryResponse>({
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
  });
  const [logType, setLogType] = useState('all');
  const [limit, setLimit] = useState(100);
  const [tradeLimit, setTradeLimit] = useState(100);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [logData, historyData] = await Promise.all([
        api.getLogs(limit, logType),
        api.getTradeHistory(tradeLimit),
      ]);
      setLogs(logData);
      setTradeHistory(historyData);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [limit, logType, tradeLimit]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const formatTime = (ts: number) => {
    return new Date(ts * 1000).toLocaleString();
  };

  const formatMoney = (v: number | null | undefined) => {
    if (v === null || v === undefined) return '-';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return `${sign}$${n.toFixed(2)}`;
  };

  const formatPct = (v: number | null | undefined) => {
    if (v === null || v === undefined) return '-';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return `${sign}${n.toFixed(2)}%`;
  };

  const formatDuration = (mins: number | null | undefined) => {
    if (mins === null || mins === undefined) return '-';
    if (mins < 60) return `${mins.toFixed(0)}m`;
    const h = Math.floor(mins / 60);
    const m = Math.floor(mins % 60);
    return `${h}h ${m}m`;
  };

  const getLogColor = (type: string) => {
    switch (type) {
      case 'signal': return 'badge-blue';
      case 'risk_decision': return 'badge-purple';
      case 'order': return 'badge-green';
      case 'connection': return 'badge-yellow';
      case 'error': return 'badge-red';
      default: return 'badge-blue';
    }
  };

  const getLogSummary = (log: LogEntry): string => {
    const l = log as any;
    switch (log.log_type) {
      case 'signal':
        return `${l.agent_name}: ${l.action} ${l.symbol} ${l.timeframe} (${(l.confidence * 100).toFixed(0)}%) - ${l.reason}`;
      case 'risk_decision':
        return `${l.approved ? 'APPROVED' : 'REJECTED'} (vol: ${l.adjusted_volume}) - ${l.reason}`;
      case 'order':
        return `${l.action} ${l.symbol} vol=${l.volume} price=${l.price} ticket=${l.ticket} [${l.retcode}] ${l.retcode_desc}`;
      case 'connection':
        return `${l.event} - account=${l.account} server=${l.server} ${l.details || ''}`;
      case 'error':
        return `[${l.source}] ${l.message} ${l.details || ''}`;
      default:
        return JSON.stringify(log);
    }
  };

  const getTradeReason = (trade: TradeHistoryItem): string => {
    return trade.exit_reason || trade.signal_reason || trade.risk_reason || '-';
  };

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Logs</h2>
            <p>System logs and trade history</p>
          </div>
          <button className="btn btn-secondary" onClick={refresh} disabled={loading}>
            {loading ? <span className="loading-spinner" /> : <RefreshCw size={14} />}
            Refresh
          </button>
        </div>
      </div>

      <div className="stats-grid mb-4">
        <div className="stat-card">
          <div className="stat-label">Total Profit</div>
          <div className="stat-value" style={{ color: tradeHistory.summary.total_profit_usd >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {formatMoney(tradeHistory.summary.total_profit_usd)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value">{tradeHistory.summary.total_trades}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Win Rate</div>
          <div className="stat-value">{tradeHistory.summary.win_rate_pct.toFixed(1)}%</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Closed / Open</div>
          <div className="stat-value">
            {tradeHistory.summary.closed_trades} / {tradeHistory.summary.open_trades}
          </div>
        </div>
      </div>

      <div className="card mb-4">
        <div className="flex justify-between items-center mb-3">
          <h3>Trade History Analysis</h3>
          <select
            className="form-input"
            style={{ width: 120 }}
            value={tradeLimit}
            onChange={(e) => setTradeLimit(parseInt(e.target.value))}
          >
            <option value={50}>50 trades</option>
            <option value={100}>100 trades</option>
            <option value={200}>200 trades</option>
            <option value={500}>500 trades</option>
          </select>
        </div>
        {loading && tradeHistory.trades.length === 0 ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : tradeHistory.trades.length === 0 ? (
          <div className="empty-state">No trades yet</div>
        ) : (
          <div style={{ maxHeight: 360, overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Trade</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>P/L</th>
                  <th>Entry</th>
                  <th>SL / TP</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {tradeHistory.trades.map((trade, i) => (
                  <tr key={`${trade.ticket ?? 'no-ticket'}-${trade.opened_at}-${i}`}>
                    <td className="text-sm text-muted">{formatTime(trade.opened_at)}</td>
                    <td className="text-sm">
                      <div style={{ fontWeight: 600 }}>{trade.action} {trade.symbol}</div>
                      <div className="text-muted">#{trade.ticket ?? '-'} • {trade.volume.toFixed(2)} lots</div>
                    </td>
                    <td className="text-sm">
                      <div>{formatMoney(trade.started_with_usd)}</div>
                      <div className="text-muted">
                        {trade.started_with_source === 'provided'
                          ? 'provided'
                          : trade.started_with_source === 'estimated'
                            ? 'estimated'
                            : 'unknown'}
                      </div>
                    </td>
                    <td className="text-sm">{formatMoney(trade.ended_with_usd)}</td>
                    <td className="text-sm">
                      <div style={{ color: (trade.profit_usd || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                        {formatMoney(trade.profit_usd)}
                      </div>
                      <div className="text-muted">{formatPct(trade.profit_pct)}</div>
                    </td>
                    <td className="text-sm">
                      <div>{trade.entry_price ? trade.entry_price.toFixed(5) : '-'}</div>
                      <div className="text-muted">{formatMoney(trade.entry_market_value_usd)}</div>
                    </td>
                    <td className="text-sm">
                      <div>SL {trade.stop_loss ? trade.stop_loss.toFixed(5) : '-'}</div>
                      <div>TP {trade.take_profit ? trade.take_profit.toFixed(5) : '-'}</div>
                      <div className="text-muted">{formatPct(trade.sl_pct_of_start)} / {formatPct(trade.tp_pct_of_start)}</div>
                    </td>
                    <td>
                      <span className={`badge ${trade.status === 'closed' ? 'badge-green' : 'badge-yellow'}`}>{trade.status}</span>
                    </td>
                    <td className="text-sm text-muted">{formatDuration(trade.duration_minutes)}</td>
                    <td className="text-sm" style={{ maxWidth: 300, whiteSpace: 'normal', wordBreak: 'break-word' }}>{getTradeReason(trade)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card mb-4">
        <div className="flex gap-2 items-center">
          <Filter size={14} className="text-muted" />
          {LOG_TYPES.map((type) => (
            <button
              key={type}
              className={`btn btn-sm ${logType === type ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => setLogType(type)}
            >
              {type}
            </button>
          ))}
          <select
            className="form-input"
            style={{ width: 100, marginLeft: 'auto' }}
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value))}
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={200}>200</option>
            <option value={500}>500</option>
          </select>
        </div>
      </div>

      <div className="card">
        {loading ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : logs.length === 0 ? (
          <div className="empty-state">No logs yet</div>
        ) : (
          <div style={{ maxHeight: 'calc(100vh - 300px)', overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 160 }}>Time</th>
                  <th style={{ width: 100 }}>Type</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log, i) => (
                  <tr key={i}>
                    <td className="font-mono text-sm text-muted">
                      {formatTime(log.timestamp)}
                    </td>
                    <td>
                      <span className={`badge ${getLogColor(log.log_type)}`}>
                        {log.log_type}
                      </span>
                    </td>
                    <td className="text-sm" style={{ wordBreak: 'break-word' }}>
                      {getLogSummary(log)}
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
