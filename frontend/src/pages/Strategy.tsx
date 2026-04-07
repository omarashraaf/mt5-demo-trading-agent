import { useState, useEffect, useCallback } from 'react';
import { Play, Plus, HelpCircle } from 'lucide-react';
import { api } from '../utils/api';
import { FALLBACK_SYMBOL_LIST, ACTIVE_SYMBOL_MODE_LABEL } from '../utils/symbolUniverse';
import type { AgentInfo, EvaluateResponse, StrategyProfile } from '../types';

interface Props {
  connected: boolean;
}

export default function Strategy({ connected }: Props) {
  const [agents, setAgents] = useState<Record<string, AgentInfo>>({});
  const [activeAgent, setActiveAgent] = useState('');
  const [strategies, setStrategies] = useState<StrategyProfile[]>([]);
  const [activeStrategyId, setActiveStrategyId] = useState('');
  const [strategyName, setStrategyName] = useState('');
  const [strategyDescription, setStrategyDescription] = useState('');
  const [strategyLoadingId, setStrategyLoadingId] = useState('');
  const [strategyInfoOpen, setStrategyInfoOpen] = useState(false);
  const [strategyInfo, setStrategyInfo] = useState<StrategyProfile | null>(null);
  const [buildingStrategy, setBuildingStrategy] = useState(false);
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(FALLBACK_SYMBOL_LIST);
  const [symbol, setSymbol] = useState(FALLBACK_SYMBOL_LIST[0]);
  const [timeframe, setTimeframe] = useState('H1');
  const [barCount, setBarCount] = useState(100);
  const [result, setResult] = useState<EvaluateResponse | null>(null);
  const [history, setHistory] = useState<EvaluateResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadStrategies = useCallback(async () => {
    try {
      const response = await api.getStrategies();
      setStrategies(response.items || []);
      setActiveStrategyId(response.active_strategy_id || '');
    } catch {
      // keep page usable if strategies endpoint is temporarily unavailable
    }
  }, []);

  useEffect(() => {
    api.getAgents().then(setAgents).catch(() => {});
    api.getStatus()
      .then((s) => {
        setActiveAgent(s.active_agent);
        setActiveStrategyId(s.active_strategy?.id || '');
      })
      .catch(() => {});
    loadStrategies();
  }, [loadStrategies]);

  useEffect(() => {
    if (!connected) return;
    api.getAvailableSymbols()
      .then((response) => {
        const symbols = Object.values(response.categories)
          .flat()
          .map((item) => item.name)
          .sort((a, b) => a.localeCompare(b));
        if (!symbols.length) return;
        setAvailableSymbols(symbols);
        setSymbol((prev) => (symbols.includes(prev) ? prev : symbols[0]));
      })
      .catch(() => {});
  }, [connected]);

  const handleSetAgent = async (name: string) => {
    try {
      await api.setAgent(name);
      setActiveAgent(name);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleSelectStrategy = async (strategyId: string) => {
    if (!strategyId || strategyLoadingId) return;
    setStrategyLoadingId(strategyId);
    setError('');
    try {
      const response = await api.selectStrategy(strategyId);
      setActiveStrategyId(response.active_strategy_id || strategyId);
      setActiveAgent(response.active_agent || activeAgent);
      setStrategies((prev) => prev.map((item) => ({
        ...item,
        is_selected: item.id === (response.active_strategy_id || strategyId),
      })));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setStrategyLoadingId('');
    }
  };

  const handleBuildStrategy = async () => {
    const name = strategyName.trim();
    const description = strategyDescription.trim();
    if (name.length < 3) {
      setError('Strategy name must be at least 3 characters.');
      return;
    }
    if (description.length < 10) {
      setError('Please describe your strategy in more detail.');
      return;
    }
    setBuildingStrategy(true);
    setError('');
    try {
      const response = await api.buildStrategy({ name, description });
      setActiveStrategyId(response.active_strategy_id);
      setActiveAgent(response.active_agent || activeAgent);
      setStrategyName('');
      setStrategyDescription('');
      await loadStrategies();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBuildingStrategy(false);
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
        <h2>Strategy</h2>
        <p>Configure and run your trading agent. Current mode: {ACTIVE_SYMBOL_MODE_LABEL}.</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card mb-4">
        <div className="card-header">
          <h3>Strategy Builder</h3>
        </div>
        <div className="grid-2 gap-3">
          <div className="form-group">
            <label>Strategy Name</label>
            <input
              className="form-input"
              placeholder="e.g. Gold London Breakout"
              value={strategyName}
              onChange={(e) => setStrategyName(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label>Describe Strategy Logic</label>
            <textarea
              className="form-input"
              placeholder="Example: Focus on Gold and US indices, avoid low momentum sessions, use safer entries."
              value={strategyDescription}
              onChange={(e) => setStrategyDescription(e.target.value)}
              rows={4}
              style={{ resize: 'vertical' }}
            />
          </div>
        </div>
        <button className="btn btn-primary" onClick={handleBuildStrategy} disabled={buildingStrategy}>
          {buildingStrategy ? <span className="loading-spinner" /> : <><Plus size={14} /> Add Strategy</>}
        </button>
        <div className="text-muted text-sm mt-2">
          Built-in strategies are shared with all users. Strategies you build appear only for your account.
        </div>
      </div>

      {strategies.length > 0 && (
        <div className="card mb-4">
          <div className="card-header">
            <h3>Available Strategies</h3>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {strategies.map((strategy, idx) => {
              const selected = strategy.id === activeStrategyId || strategy.is_selected;
              return (
                <div key={strategy.id} style={{ display: 'inline-flex', alignItems: 'center' }}>
                  <button
                    className="btn"
                    onClick={() => handleSelectStrategy(strategy.id)}
                    disabled={strategyLoadingId === strategy.id}
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
          </div>
          <div className="text-muted text-sm mt-3">
            Active strategy: <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
              {strategies.find((s) => s.id === activeStrategyId)?.name || 'Default Smart'}
            </span>
          </div>
        </div>
      )}

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
                {availableSymbols.map((s) => (
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

          {result.trade_quality && (
            <div className="grid-3 gap-4 mb-4">
              <div>
                <div className="stat-label">Trade Quality</div>
                <div className="stat-value">{(result.trade_quality.final_trade_quality_score * 100).toFixed(0)}%</div>
              </div>
              <div>
                <div className="stat-label">Quality Threshold</div>
                <div className="stat-value">{(result.trade_quality.threshold * 100).toFixed(0)}%</div>
              </div>
              <div>
                <div className="stat-label">Gemini</div>
                <div className="font-mono">{result.gemini_confirmation?.degraded ? 'Degraded' : result.gemini_confirmation?.used ? 'Advisory' : 'Off'}</div>
              </div>
            </div>
          )}

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
          {result.execution_reason && (
            <div className="mt-2">
              <div className="stat-label">Execution Reason</div>
              <div className="text-sm mt-2">{result.execution_reason}</div>
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
