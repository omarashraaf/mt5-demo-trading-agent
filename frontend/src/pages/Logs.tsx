import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Filter } from 'lucide-react';
import { api } from '../utils/api';
import type { LogEntry } from '../types';

const LOG_TYPES = ['all', 'signals', 'risk', 'orders', 'connections', 'errors'];

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [logType, setLogType] = useState('all');
  const [limit, setLimit] = useState(100);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getLogs(limit, logType);
      setLogs(data);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [limit, logType]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const formatTime = (ts: number) => {
    return new Date(ts * 1000).toLocaleString();
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
