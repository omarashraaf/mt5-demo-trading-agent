import { useState, useCallback, useEffect } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts';
import { api } from '../utils/api';
import { FALLBACK_SYMBOL_LIST, ACTIVE_SYMBOL_MODE_LABEL } from '../utils/symbolUniverse';
import type { TickData, BarData } from '../types';

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'];

interface Props {
  connected: boolean;
}

export default function Market({ connected }: Props) {
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(FALLBACK_SYMBOL_LIST);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([FALLBACK_SYMBOL_LIST[0]]);
  const [activeSymbol, setActiveSymbol] = useState(FALLBACK_SYMBOL_LIST[0]);
  const [timeframe, setTimeframe] = useState('H1');
  const [ticks, setTicks] = useState<Record<string, TickData>>({});
  const [bars, setBars] = useState<BarData[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const enableSymbols = async (syms: string[]) => {
    try {
      await api.selectSymbols(syms);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const refreshTicks = useCallback(async () => {
    if (!connected || selectedSymbols.length === 0) return;
    try {
      const resp = await api.getTicks(selectedSymbols);
      setTicks(resp.ticks || {});
    } catch {
      // keep previous ticks on transient failures
    }
  }, [connected, selectedSymbols]);

  const fetchBars = useCallback(async () => {
    if (!connected || !activeSymbol) return;
    setLoading(true);
    try {
      const data = await api.getBars(activeSymbol, timeframe, 100);
      setBars(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [connected, activeSymbol, timeframe]);

  const toggleSymbol = async (sym: string) => {
    if (selectedSymbols.includes(sym)) {
      setSelectedSymbols((prev) => prev.filter((s) => s !== sym));
    } else {
      setSelectedSymbols((prev) => [...prev, sym]);
      await enableSymbols([sym]);
    }
  };

  useEffect(() => {
    if (connected && selectedSymbols.length > 0) {
      enableSymbols(selectedSymbols);
    }
  }, [connected]);

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
        setSelectedSymbols((prev) => {
          const filtered = prev.filter((symbol) => symbols.includes(symbol));
          return filtered.length ? filtered : [symbols[0]];
        });
        setActiveSymbol((prev) => (symbols.includes(prev) ? prev : symbols[0]));
      })
      .catch(() => {});
  }, [connected]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const run = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        await refreshTicks();
      } finally {
        inFlight = false;
      }
    };
    void run();
    const t = setInterval(run, 150);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [refreshTicks]);

  useEffect(() => {
    fetchBars();
  }, [fetchBars]);

  if (!connected) {
    return (
      <div>
        <div className="page-header">
          <h2>Market</h2>
        </div>
        <div className="empty-state">Connect to MT5 to view market data</div>
      </div>
    );
  }

  const chartData = bars.map((b) => ({
    time: new Date(b.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    close: b.close,
    high: b.high,
    low: b.low,
  }));

  return (
    <div>
      <div className="page-header">
        <h2>Market</h2>
        <p>Symbol watchlist and price data. Current mode: {ACTIVE_SYMBOL_MODE_LABEL}.</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card mb-4">
        <div className="card-header">
          <h3>Symbol Selection</h3>
        </div>
        <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
          {availableSymbols.map((sym) => (
            <button
              key={sym}
              className={`btn btn-sm ${selectedSymbols.includes(sym) ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => toggleSymbol(sym)}
            >
              {sym}
            </button>
          ))}
        </div>
      </div>

      <div className="card mb-4">
        <div className="card-header">
          <h3>Live Quotes</h3>
        </div>
        {selectedSymbols.length === 0 ? (
          <div className="empty-state">Select symbols to watch</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Spread</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {selectedSymbols.map((sym) => {
                const tick = ticks[sym];
                return (
                  <tr key={sym}>
                    <td className="font-mono" style={{ fontWeight: 600 }}>{sym}</td>
                    <td className="font-mono text-red">{tick?.bid?.toFixed(5) ?? '---'}</td>
                    <td className="font-mono text-green">{tick?.ask?.toFixed(5) ?? '---'}</td>
                    <td className="font-mono">{tick?.spread ?? '---'}</td>
                    <td>
                      <button
                        className={`btn btn-sm ${activeSymbol === sym ? 'btn-primary' : 'btn-secondary'}`}
                        onClick={() => setActiveSymbol(sym)}
                      >
                        Chart
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <h3>{activeSymbol} - {timeframe}</h3>
          <div className="flex gap-2">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                className={`btn btn-sm ${timeframe === tf ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setTimeframe(tf)}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : chartData.length === 0 ? (
          <div className="empty-state">No data available</div>
        ) : (
          <ResponsiveContainer width="100%" height={350}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="time"
                stroke="var(--text-muted)"
                fontSize={11}
                interval="preserveStartEnd"
              />
              <YAxis
                stroke="var(--text-muted)"
                fontSize={11}
                domain={['auto', 'auto']}
                tickFormatter={(v: number) => v.toFixed(activeSymbol.includes('JPY') || activeSymbol.includes('XAU') ? 2 : 4)}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  fontSize: 12,
                }}
              />
              <Line
                type="monotone"
                dataKey="close"
                stroke="var(--accent-blue)"
                dot={false}
                strokeWidth={1.5}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
