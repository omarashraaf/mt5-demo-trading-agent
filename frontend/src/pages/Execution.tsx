import { useState, useEffect, useCallback } from 'react';
import { X, Play, AlertOctagon } from 'lucide-react';
import { api } from '../utils/api';
import { FALLBACK_SYMBOL_LIST, ACTIVE_SYMBOL_MODE_LABEL } from '../utils/symbolUniverse';
import type { StatusResponse, PositionInfo, OrderResult, EvaluateResponse } from '../types';

interface Props {
  connected: boolean;
  status: StatusResponse | null;
}

export default function Execution({ connected, status }: Props) {
  const [positions, setPositions] = useState<PositionInfo[]>([]);
  const [lastSignal, setLastSignal] = useState<EvaluateResponse | null>(null);
  const [lastOrder, setLastOrder] = useState<OrderResult | null>(null);
  const [orderHistory, setOrderHistory] = useState<OrderResult[]>([]);
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(FALLBACK_SYMBOL_LIST);
  const [symbol, setSymbol] = useState(FALLBACK_SYMBOL_LIST[0]);
  const [timeframe, setTimeframe] = useState('H1');
  const [loading, setLoading] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState('');

  const refreshPositions = useCallback(async () => {
    if (!connected) return;
    try {
      const pos = await api.getPositions();
      setPositions(pos);
    } catch {
      // ignore
    }
  }, [connected]);

  useEffect(() => {
    refreshPositions();
    const t = setInterval(refreshPositions, 3000);
    return () => clearInterval(t);
  }, [refreshPositions]);

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

  const handleFullCycle = async () => {
    setLoading(true);
    setError('');
    setLastOrder(null);
    try {
      // Step 1: Generate signal
      const signal = await api.evaluate(symbol, timeframe);
      setLastSignal(signal);

      // Step 2: If approved, execute
      if (signal.risk_decision.approved && signal.signal.action !== 'HOLD') {
        setExecuting(true);
        const order = await api.executeTrade({
          symbol,
          action: signal.signal.action,
          volume: signal.risk_decision.adjusted_volume,
          stop_loss: signal.signal.stop_loss!,
          take_profit: signal.signal.take_profit!,
          signal_id: signal.signal_id ?? undefined,
        });
        setLastOrder(order);
        setOrderHistory((prev) => [order, ...prev].slice(0, 20));
        await refreshPositions();
        setExecuting(false);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
      setExecuting(false);
    }
  };

  const handleClosePosition = async (ticket: number) => {
    try {
      const result = await api.closePosition(ticket);
      setOrderHistory((prev) => [result, ...prev].slice(0, 20));
      await refreshPositions();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handlePanicStop = async () => {
    try {
      const isPanic = status?.panic_stop ?? false;
      await api.setPanicStop(!isPanic);
    } catch (e: any) {
      setError(e.message);
    }
  };

  if (!connected) {
    return (
      <div>
        <div className="page-header"><h2>Execution</h2></div>
        <div className="empty-state">Connect to MT5 first</div>
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Execution</h2>
            <p>Run the full signal-to-trade pipeline. Current mode: {ACTIVE_SYMBOL_MODE_LABEL}.</p>
          </div>
          <button
            className={`btn ${status?.panic_stop ? 'btn-success' : 'btn-danger'}`}
            onClick={handlePanicStop}
            style={{ fontSize: 14 }}
          >
            <AlertOctagon size={16} />
            {status?.panic_stop ? '▶ Resume Trading' : 'PANIC STOP'}
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {status?.panic_stop && (
        <div className="error-banner mb-4" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span>⚠ PANIC STOP ACTIVE - Trading halted</span>
          <button className="btn btn-success btn-sm" onClick={handlePanicStop} style={{ fontSize: 12 }}>
            ▶ Resume Trading
          </button>
        </div>
      )}

      <div className="card mb-4">
        <div className="card-header">
          <h3>Execute Trading Cycle</h3>
        </div>
        <p className="text-sm text-muted mb-4">
          Generates a signal from the active agent, runs risk checks, and executes the trade if approved.
        </p>

        <div className="flex gap-3 items-center mb-4">
          <div className="form-group" style={{ margin: 0, flex: 1 }}>
            <label>Symbol</label>
            <select className="form-input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {availableSymbols.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </div>
          <div className="form-group" style={{ margin: 0, flex: 1 }}>
            <label>Timeframe</label>
            <select className="form-input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {['M1', 'M5', 'M15', 'H1'].map((tf) => (
                <option key={tf}>{tf}</option>
              ))}
            </select>
          </div>
        </div>

        <button
          className="btn btn-primary"
          onClick={handleFullCycle}
          disabled={loading || status?.panic_stop}
          style={{ fontSize: 14, padding: '10px 24px' }}
        >
          {loading ? (
            <>
              <span className="loading-spinner" />
              {executing ? 'Executing...' : 'Evaluating...'}
            </>
          ) : (
            <>
              <Play size={14} />
              Run Full Cycle
            </>
          )}
        </button>
      </div>

      {lastSignal && (
        <div className="grid-2 mb-4">
          <div className="card">
            <div className="card-header">
              <h3>Signal Result</h3>
              <span className="badge badge-blue">{lastSignal.agent_name}</span>
            </div>
            <div className="grid-3 gap-3">
              <div>
                <div className="stat-label">Action</div>
                <span className={`badge ${
                  lastSignal.signal.action === 'BUY' ? 'badge-green' :
                  lastSignal.signal.action === 'SELL' ? 'badge-red' : 'badge-yellow'
                }`}>{lastSignal.signal.action}</span>
              </div>
              <div>
                <div className="stat-label">Confidence</div>
                <div className="font-mono">{(lastSignal.signal.confidence * 100).toFixed(0)}%</div>
              </div>
              <div>
                <div className="stat-label">Risk</div>
                <span className={`badge ${lastSignal.risk_decision.approved ? 'badge-green' : 'badge-red'}`}>
                  {lastSignal.risk_decision.approved ? 'APPROVED' : 'REJECTED'}
                </span>
              </div>
            </div>
            <div className="text-sm text-muted mt-4">{lastSignal.signal.reason}</div>
            {!lastSignal.risk_decision.approved && (
              <div className="text-sm text-red mt-2">{lastSignal.risk_decision.reason}</div>
            )}
          </div>

          {lastOrder && (
            <div className="card">
              <div className="card-header">
                <h3>Order Result</h3>
                <span className={`badge ${lastOrder.success ? 'badge-green' : 'badge-red'}`}>
                  {lastOrder.success ? 'FILLED' : 'FAILED'}
                </span>
              </div>
              <div className="grid-2 gap-3">
                <div>
                  <div className="stat-label">Ticket</div>
                  <div className="font-mono">{lastOrder.ticket ?? 'N/A'}</div>
                </div>
                <div>
                  <div className="stat-label">Retcode</div>
                  <div className="font-mono">{lastOrder.retcode}</div>
                </div>
                <div>
                  <div className="stat-label">Fill Price</div>
                  <div className="font-mono">{lastOrder.price?.toFixed(5) ?? 'N/A'}</div>
                </div>
                <div>
                  <div className="stat-label">Volume</div>
                  <div className="font-mono">{lastOrder.volume ?? 'N/A'}</div>
                </div>
                <div>
                  <div className="stat-label">SL</div>
                  <div className="font-mono">{lastOrder.stop_loss?.toFixed(5) ?? 'N/A'}</div>
                </div>
                <div>
                  <div className="stat-label">TP</div>
                  <div className="font-mono">{lastOrder.take_profit?.toFixed(5) ?? 'N/A'}</div>
                </div>
              </div>
              <div className="text-sm text-muted mt-4">{lastOrder.retcode_desc}</div>
            </div>
          )}
        </div>
      )}

      <div className="card mb-4">
        <div className="card-header">
          <h3>Open Positions</h3>
          <span className="badge badge-blue">{positions.length} open</span>
        </div>
        {positions.length === 0 ? (
          <div className="empty-state">No open positions</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Symbol</th>
                <th>Type</th>
                <th>Volume</th>
                <th>Open Price</th>
                <th>Current</th>
                <th>SL</th>
                <th>TP</th>
                <th>P/L</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.ticket}>
                  <td className="font-mono">{p.ticket}</td>
                  <td className="font-mono">{p.symbol}</td>
                  <td>
                    <span className={`badge ${p.type === 'BUY' ? 'badge-green' : 'badge-red'}`}>
                      {p.type}
                    </span>
                  </td>
                  <td className="font-mono">{p.volume}</td>
                  <td className="font-mono">{p.price_open.toFixed(5)}</td>
                  <td className="font-mono">{p.price_current.toFixed(5)}</td>
                  <td className="font-mono">{p.stop_loss > 0 ? p.stop_loss.toFixed(5) : '-'}</td>
                  <td className="font-mono">{p.take_profit > 0 ? p.take_profit.toFixed(5) : '-'}</td>
                  <td className={`font-mono ${p.profit >= 0 ? 'text-green' : 'text-red'}`}>
                    {p.profit >= 0 ? '+' : ''}{p.profit.toFixed(2)}
                  </td>
                  <td>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => handleClosePosition(p.ticket)}
                    >
                      <X size={12} /> Close
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {orderHistory.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Order History (this session)</h3>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Status</th>
                <th>Retcode</th>
                <th>Price</th>
                <th>Volume</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {orderHistory.map((o, i) => (
                <tr key={i}>
                  <td className="font-mono">{o.ticket ?? '-'}</td>
                  <td>
                    <span className={`badge ${o.success ? 'badge-green' : 'badge-red'}`}>
                      {o.success ? 'OK' : 'FAIL'}
                    </span>
                  </td>
                  <td className="font-mono">{o.retcode}</td>
                  <td className="font-mono">{o.price?.toFixed(5) ?? '-'}</td>
                  <td className="font-mono">{o.volume ?? '-'}</td>
                  <td className="text-sm text-muted">{o.retcode_desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
