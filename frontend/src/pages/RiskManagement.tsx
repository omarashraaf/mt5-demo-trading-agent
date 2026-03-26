import { useState, useEffect } from 'react';
import { Shield, Save, AlertOctagon } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse, RiskSettings } from '../types';

interface Props {
  status: StatusResponse | null;
}

const DEFAULT_SETTINGS: RiskSettings = {
  risk_percent_per_trade: 1.0,
  max_daily_loss_percent: 5.0,
  max_concurrent_positions: 10,
  min_confidence_threshold: 0.55,
  max_spread_threshold: 30.0,
  allowed_symbols: [],
  require_stop_loss: true,
  use_fixed_lot: false,
  fixed_lot_size: 0.01,
  auto_trade_enabled: false,
  auto_trade_min_confidence: 0.55,
  auto_trade_scan_interval_seconds: 60,
};

export default function RiskManagement({ status }: Props) {
  const [settings, setSettings] = useState<RiskSettings>(DEFAULT_SETTINGS);
  const [symbolInput, setSymbolInput] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getRiskSettings().then(setSettings).catch(() => {});
  }, []);

  const handleSave = async () => {
    setLoading(true);
    setError('');
    setSaved(false);
    try {
      const updated = await api.updateRiskSettings(settings);
      setSettings(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
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

  const addSymbol = () => {
    const sym = symbolInput.toUpperCase().trim();
    if (sym && !settings.allowed_symbols.includes(sym)) {
      setSettings({ ...settings, allowed_symbols: [...settings.allowed_symbols, sym] });
    }
    setSymbolInput('');
  };

  const removeSymbol = (sym: string) => {
    setSettings({
      ...settings,
      allowed_symbols: settings.allowed_symbols.filter((s) => s !== sym),
    });
  };

  const update = (field: keyof RiskSettings, value: any) => {
    setSettings({ ...settings, [field]: value });
  };

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Risk Management</h2>
            <p>Configure risk rules and position sizing</p>
          </div>
          <button
            className={`btn ${status?.panic_stop ? 'btn-success' : 'btn-danger'}`}
            onClick={handlePanicStop}
            style={{ fontSize: 14, padding: '10px 20px' }}
          >
            <AlertOctagon size={16} />
            {status?.panic_stop ? 'Deactivate Panic Stop' : 'PANIC STOP'}
          </button>
        </div>
      </div>

      {status?.panic_stop && (
        <div className="error-banner mb-4" style={{ fontSize: 14 }}>
          PANIC STOP IS ACTIVE - All trading is halted. Click the button above to resume.
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>Position Sizing</h3>
          </div>

          <div className="form-group">
            <label>Risk % Per Trade</label>
            <input
              className="form-input"
              type="number"
              step="0.1"
              min="0.1"
              max="10"
              value={settings.risk_percent_per_trade}
              onChange={(e) => update('risk_percent_per_trade', parseFloat(e.target.value) || 1)}
            />
            <div className="text-muted text-sm mt-2">Percentage of equity risked per trade</div>
          </div>

          <div className="form-group">
            <label>
              <input
                type="checkbox"
                checked={settings.use_fixed_lot}
                onChange={(e) => update('use_fixed_lot', e.target.checked)}
                style={{ marginRight: 8 }}
              />
              Use Fixed Lot Size
            </label>
          </div>

          {settings.use_fixed_lot && (
            <div className="form-group">
              <label>Fixed Lot Size</label>
              <input
                className="form-input"
                type="number"
                step="0.01"
                min="0.01"
                value={settings.fixed_lot_size}
                onChange={(e) => update('fixed_lot_size', parseFloat(e.target.value) || 0.01)}
              />
            </div>
          )}

          <div className="form-group">
            <label>Max Daily Loss %</label>
            <input
              className="form-input"
              type="number"
              step="0.5"
              min="1"
              max="50"
              value={settings.max_daily_loss_percent}
              onChange={(e) => update('max_daily_loss_percent', parseFloat(e.target.value) || 5)}
            />
          </div>

          <div className="form-group">
            <label>Max Concurrent Positions</label>
            <input
              className="form-input"
              type="number"
              min="1"
              max="20"
              value={settings.max_concurrent_positions}
              onChange={(e) => update('max_concurrent_positions', parseInt(e.target.value) || 3)}
            />
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Signal Filters</h3>
          </div>

          <div className="form-group">
            <label>Minimum Confidence Threshold</label>
            <input
              className="form-input"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={settings.min_confidence_threshold}
              onChange={(e) => update('min_confidence_threshold', parseFloat(e.target.value) || 0.5)}
            />
            <div className="text-muted text-sm mt-2">Signals below this are rejected (0-1)</div>
          </div>

          <div className="form-group">
            <label>Maximum Spread (points)</label>
            <input
              className="form-input"
              type="number"
              step="1"
              min="1"
              value={settings.max_spread_threshold}
              onChange={(e) => update('max_spread_threshold', parseFloat(e.target.value) || 30)}
            />
          </div>

          <div className="form-group">
            <label>
              <input
                type="checkbox"
                checked={settings.require_stop_loss}
                onChange={(e) => update('require_stop_loss', e.target.checked)}
                style={{ marginRight: 8 }}
              />
              Require Stop Loss on Every Trade
            </label>
          </div>

          <div className="form-group">
            <label>Allowed Symbols</label>
            <div className="flex gap-2 mb-2" style={{ flexWrap: 'wrap' }}>
              {settings.allowed_symbols.map((sym) => (
                <span key={sym} className="badge badge-blue" style={{ cursor: 'pointer' }} onClick={() => removeSymbol(sym)}>
                  {sym} ×
                </span>
              ))}
            </div>
            <div className="flex gap-2">
              <input
                className="form-input"
                placeholder="Add symbol..."
                value={symbolInput}
                onChange={(e) => setSymbolInput(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && addSymbol()}
              />
              <button className="btn btn-secondary btn-sm" onClick={addSymbol}>Add</button>
            </div>
          </div>
        </div>
      </div>

      <div className="mt-4 flex gap-2 items-center">
        <button className="btn btn-primary" onClick={handleSave} disabled={loading}>
          {loading ? <span className="loading-spinner" /> : <Save size={14} />}
          Save Settings
        </button>
        {saved && <span className="text-green text-sm">Settings saved</span>}
      </div>

      <div className="card mt-4">
        <div className="card-header">
          <h3>Safety Rules (Built-in)</h3>
        </div>
        <div className="text-sm text-muted" style={{ lineHeight: 1.8 }}>
          <div>- No martingale strategies allowed</div>
          <div>- No averaging down on losing positions</div>
          <div>- Every trade must include a stop loss</div>
          <div>- Position sizing based on risk % or fixed lot (user choice)</div>
          <div>- Daily loss limit enforced automatically</div>
          <div>- Agent cannot place orders directly - risk engine decides</div>
          <div>- Panic stop halts all trading immediately</div>
        </div>
      </div>
    </div>
  );
}
