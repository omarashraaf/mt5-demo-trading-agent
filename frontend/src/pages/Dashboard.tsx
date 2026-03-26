import { useState, useEffect, useCallback } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import { api } from '../utils/api';
import type { StatusResponse, PositionInfo, LogEntry } from '../types';

interface Props {
  status: StatusResponse | null;
}

export default function Dashboard({ status }: Props) {
  const [positions, setPositions] = useState<PositionInfo[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);

  const connected = status?.connected ?? false;

  const refresh = useCallback(async () => {
    if (!connected) return;
    setLoading(true);
    try {
      const [pos, lg] = await Promise.all([
        api.getPositions(),
        api.getLogs(20),
      ]);
      setPositions(pos);
      setLogs(lg);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [connected]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const account = status?.account;
  const totalProfit = positions.reduce((sum, p) => sum + p.profit, 0);
  const recentSignals = logs.filter((l) => l.log_type === 'signal');
  const recentRisk = logs.filter((l) => l.log_type === 'risk_decision');
  const approved = recentRisk.filter((r) => r.approved === 1).length;
  const rejected = recentRisk.filter((r) => r.approved === 0).length;

  return (
    <div>
      <div className="page-header">
        <h2>Dashboard</h2>
        <p>Overview of your trading agent</p>
      </div>

      {!connected && (
        <div className="empty-state">
          <p>Not connected to MT5. Go to Connection to get started.</p>
        </div>
      )}

      {connected && account && (
        <>
          <div className="grid-4 mb-4">
            <div className="card">
              <div className="stat-label">Balance</div>
              <div className="stat-value font-mono">
                {account.balance.toFixed(2)}
              </div>
              <div className="text-sm text-muted">{account.currency}</div>
            </div>
            <div className="card">
              <div className="stat-label">Equity</div>
              <div className="stat-value font-mono">
                {account.equity.toFixed(2)}
              </div>
              <div className={`stat-change ${account.equity >= account.balance ? 'positive' : 'negative'}`}>
                {account.equity >= account.balance ? '+' : ''}
                {(account.equity - account.balance).toFixed(2)}
              </div>
            </div>
            <div className="card">
              <div className="stat-label">Open Positions</div>
              <div className="stat-value">{positions.length}</div>
              <div className={`stat-change ${totalProfit >= 0 ? 'positive' : 'negative'}`}>
                P/L: {totalProfit >= 0 ? '+' : ''}{totalProfit.toFixed(2)}
              </div>
            </div>
            <div className="card">
              <div className="stat-label">Signals (Recent)</div>
              <div className="stat-value">{recentSignals.length}</div>
              <div className="text-sm">
                <span className="text-green">{approved} approved</span>
                {' / '}
                <span className="text-red">{rejected} rejected</span>
              </div>
            </div>
          </div>

          <div className="grid-2">
            <div className="card">
              <div className="card-header">
                <h3>Open Positions</h3>
              </div>
              {positions.length === 0 ? (
                <div className="empty-state">No open positions</div>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Type</th>
                      <th>Volume</th>
                      <th>Open</th>
                      <th>Current</th>
                      <th>P/L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => (
                      <tr key={p.ticket}>
                        <td className="font-mono">{p.symbol}</td>
                        <td>
                          <span className={`badge ${p.type === 'BUY' ? 'badge-green' : 'badge-red'}`}>
                            {p.type}
                          </span>
                        </td>
                        <td className="font-mono">{p.volume}</td>
                        <td className="font-mono">{p.price_open.toFixed(5)}</td>
                        <td className="font-mono">{p.price_current.toFixed(5)}</td>
                        <td className={`font-mono ${p.profit >= 0 ? 'text-green' : 'text-red'}`}>
                          {p.profit >= 0 ? '+' : ''}{p.profit.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="card">
              <div className="card-header">
                <h3>Recent Activity</h3>
              </div>
              {logs.length === 0 ? (
                <div className="empty-state">No recent activity</div>
              ) : (
                <div style={{ maxHeight: 300, overflow: 'auto' }}>
                  {logs.slice(0, 10).map((log, i) => (
                    <div
                      key={i}
                      style={{
                        padding: '8px 0',
                        borderBottom: '1px solid var(--border)',
                        fontSize: 12,
                      }}
                    >
                      <div className="flex justify-between">
                        <span className={`badge ${
                          log.log_type === 'signal' ? 'badge-blue' :
                          log.log_type === 'risk_decision' ? 'badge-purple' :
                          log.log_type === 'order' ? 'badge-green' :
                          'badge-yellow'
                        }`}>
                          {log.log_type}
                        </span>
                        <span className="text-muted">
                          {new Date(log.timestamp * 1000).toLocaleTimeString()}
                        </span>
                      </div>
                      <div className="text-muted mt-2">
                        {log.log_type === 'signal' && `${(log as any).action} ${(log as any).symbol} (${((log as any).confidence * 100).toFixed(0)}%)`}
                        {log.log_type === 'risk_decision' && `${(log as any).approved ? 'Approved' : 'Rejected'}: ${(log as any).reason}`}
                        {log.log_type === 'order' && `${(log as any).action} ${(log as any).symbol} - ${(log as any).retcode_desc}`}
                        {log.log_type === 'connection' && `${(log as any).event}`}
                        {log.log_type === 'error' && `${(log as any).source}: ${(log as any).message}`}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="card mt-4">
            <div className="card-header">
              <h3>Account Info</h3>
            </div>
            <div className="grid-4">
              <div>
                <div className="stat-label">Server</div>
                <div className="font-mono text-sm">{account.server}</div>
              </div>
              <div>
                <div className="stat-label">Leverage</div>
                <div className="font-mono text-sm">1:{account.leverage}</div>
              </div>
              <div>
                <div className="stat-label">Free Margin</div>
                <div className="font-mono text-sm">{account.free_margin.toFixed(2)}</div>
              </div>
              <div>
                <div className="stat-label">Agent</div>
                <div className="text-sm">{status.active_agent}</div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
