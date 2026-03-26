import { useState, useEffect } from 'react';
import { Brain, Play, RefreshCw } from 'lucide-react';
import { api } from '../utils/api';
import type { AgentInfo, EvaluateResponse } from '../types';

interface Props {
  connected: boolean;
}

export default function Strategy({ connected }: Props) {
  const [agents, setAgents] = useState<Record<string, AgentInfo>>({});
  const [activeAgent, setActiveAgent] = useState('');
  const [symbol, setSymbol] = useState('EURUSD');
  const [timeframe, setTimeframe] = useState('H1');
  const [barCount, setBarCount] = useState(100);
  const [result, setResult] = useState<EvaluateResponse | null>(null);
  const [history, setHistory] = useState<EvaluateResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getAgents().then(setAgents).catch(() => {});
    api.getStatus().then((s) => setActiveAgent(s.active_agent)).catch(() => {});
  }, []);

  const handleSetAgent = async (name: string) => {
    try {
      await api.setAgent(name);
      setActiveAgent(name);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleEvaluate = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.evaluate(symbol, timeframe, barCount, activeAgent);
      setResult(res);
      setHistory((prev) => [res, ...prev].slice(0, 20));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  if (!connected) {
    return (
      <div>
        <div className="page-header"><h2>Strategy / Agent</h2></div>
        <div className="empty-state">Connect to MT5 first</div>
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h2>Strategy / Agent</h2>
        <p>Configure and run your trading agent</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="grid-2 mb-4">
        <div className="card">
          <div className="card-header">
            <h3>Available Agents</h3>
          </div>
          {Object.entries(agents).map(([key, agent]) => (
            <div
              key={key}
              style={{
                padding: '12px',
                borderRadius: 6,
                border: `1px solid ${activeAgent === key ? 'var(--accent-blue)' : 'var(--border)'}`,
                marginBottom: 8,
                cursor: 'pointer',
                background: activeAgent === key ? 'rgba(59, 130, 246, 0.1)' : 'transparent',
              }}
              onClick={() => handleSetAgent(key)}
            >
              <div className="flex justify-between items-center">
                <span style={{ fontWeight: 600, fontSize: 13 }}>{agent.name}</span>
                {activeAgent === key && <span className="badge badge-blue">Active</span>}
              </div>
              <div className="text-muted text-sm mt-2">{agent.description}</div>
            </div>
          ))}
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Evaluate Signal</h3>
          </div>

          <div className="grid-2 gap-3">
            <div className="form-group">
              <label>Symbol</label>
              <select className="form-input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
                {['EURUSD', 'GBPUSD', 'USDJPY', 'XAUUSD', 'AUDUSD'].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label>Timeframe</label>
              <select className="form-input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                {['M1', 'M5', 'M15', 'H1', 'H4', 'D1'].map((tf) => (
                  <option key={tf}>{tf}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-group">
            <label>Bar Count</label>
            <input
              className="form-input"
              type="number"
              value={barCount}
              onChange={(e) => setBarCount(parseInt(e.target.value) || 100)}
              min={10}
              max={500}
            />
          </div>

          <button className="btn btn-primary" onClick={handleEvaluate} disabled={loading}>
            {loading ? <span className="loading-spinner" /> : <Play size={14} />}
            Generate Signal
          </button>
        </div>
      </div>

      {result && (
        <div className="card mb-4">
          <div className="card-header">
            <h3>Latest Signal</h3>
            <span className="badge badge-blue">{result.agent_name}</span>
          </div>

          <div className="grid-3 gap-4 mb-4">
            <div>
              <div className="stat-label">Action</div>
              <span className={`badge ${
                result.signal.action === 'BUY' ? 'badge-green' :
                result.signal.action === 'SELL' ? 'badge-red' :
                'badge-yellow'
              }`} style={{ fontSize: 16, padding: '4px 16px' }}>
                {result.signal.action}
              </span>
            </div>
            <div>
              <div className="stat-label">Confidence</div>
              <div className="stat-value">{(result.signal.confidence * 100).toFixed(0)}%</div>
            </div>
            <div>
              <div className="stat-label">Risk Decision</div>
              <span className={`badge ${result.risk_decision.approved ? 'badge-green' : 'badge-red'}`}
                    style={{ fontSize: 14, padding: '4px 12px' }}>
                {result.risk_decision.approved ? 'APPROVED' : 'REJECTED'}
              </span>
            </div>
          </div>

          <div className="grid-4 gap-4 mb-4">
            <div>
              <div className="stat-label">Stop Loss</div>
              <div className="font-mono">{result.signal.stop_loss?.toFixed(5) ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Take Profit</div>
              <div className="font-mono">{result.signal.take_profit?.toFixed(5) ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Max Hold (min)</div>
              <div className="font-mono">{result.signal.max_holding_minutes ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Volume</div>
              <div className="font-mono">{result.risk_decision.adjusted_volume || 'N/A'}</div>
            </div>
          </div>

          <div>
            <div className="stat-label">Reason</div>
            <div className="text-sm mt-2" style={{ color: 'var(--text-secondary)' }}>
              {result.signal.reason}
            </div>
          </div>
          {!result.risk_decision.approved && (
            <div className="mt-2">
              <div className="stat-label">Rejection Reason</div>
              <div className="text-sm text-red mt-2">{result.risk_decision.reason}</div>
            </div>
          )}
        </div>
      )}

      {history.length > 1 && (
        <div className="card">
          <div className="card-header">
            <h3>Signal History (this session)</h3>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Action</th>
                <th>Confidence</th>
                <th>Risk</th>
                <th>Volume</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h, i) => (
                <tr key={i}>
                  <td>{h.agent_name}</td>
                  <td>
                    <span className={`badge ${
                      h.signal.action === 'BUY' ? 'badge-green' :
                      h.signal.action === 'SELL' ? 'badge-red' : 'badge-yellow'
                    }`}>{h.signal.action}</span>
                  </td>
                  <td className="font-mono">{(h.signal.confidence * 100).toFixed(0)}%</td>
                  <td>
                    <span className={`badge ${h.risk_decision.approved ? 'badge-green' : 'badge-red'}`}>
                      {h.risk_decision.approved ? 'OK' : 'REJECT'}
                    </span>
                  </td>
                  <td className="font-mono">{h.risk_decision.adjusted_volume || '-'}</td>
                  <td className="text-sm text-muted" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {h.signal.reason}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
