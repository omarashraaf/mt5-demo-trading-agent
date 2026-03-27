import { useEffect, useState } from 'react';
import { Shield, Save, AlertOctagon, Lock, Bot, Activity } from 'lucide-react';
import { api } from '../utils/api';
import type {
  PolicyMode,
  PolicySettingsResponse,
  StatusResponse,
  UserPolicySettings,
} from '../types';

interface Props {
  status: StatusResponse | null;
}

const SESSION_OPTIONS = ['24/7', 'London', 'New York', 'Overlap', 'US Open'];

const FALLBACK_POLICY: UserPolicySettings = {
  mode: 'safe',
  allowed_symbols: [],
  max_risk_per_trade: 0.25,
  max_daily_drawdown: 2.5,
  max_open_trades: 3,
  max_trades_per_symbol: 1,
  max_margin_utilization: 12,
  min_free_margin: 70,
  min_reward_risk: 2.2,
  allow_counter_trend_trades: false,
  allow_overnight_holding: false,
  gemini_role: 'confirmation-required',
  session_filters: ['London', 'New York', 'Overlap'],
  demo_only_default: true,
};

const PRESET_DESCRIPTIONS: Record<PolicyMode, string> = {
  safe: 'Lowest risk, stricter trade quality, Gemini confirmation required.',
  balanced: 'Moderate trade flow with advisory Gemini and broader sessions.',
  aggressive: 'Looser preset, still bounded by the same deterministic risk gate.',
};

function formatPct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-';
  return `${value.toFixed(1)}%`;
}

export default function RiskManagement({ status }: Props) {
  const [policy, setPolicy] = useState<UserPolicySettings>(FALLBACK_POLICY);
  const [settingsResponse, setSettingsResponse] = useState<PolicySettingsResponse | null>(null);
  const [autoTradeStatus, setAutoTradeStatus] = useState<Awaited<ReturnType<typeof api.getAutoTradeStatus>> | null>(null);
  const [symbolInput, setSymbolInput] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getRiskSettings()
      .then((response) => {
        setSettingsResponse(response);
        setPolicy(response.user_policy);
      })
      .catch(() => {});
    api.getAutoTradeStatus()
      .then(setAutoTradeStatus)
      .catch(() => {});
  }, []);

  const refreshSettings = async () => {
    const response = await api.getRiskSettings();
    setSettingsResponse(response);
    setPolicy(response.user_policy);
  };

  const handleSave = async () => {
    setLoading(true);
    setError('');
    setSaved(false);
    try {
      const updated = await api.updateRiskSettings(policy);
      setSettingsResponse(updated);
      setPolicy(updated.user_policy);
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

  const applyPreset = (mode: PolicyMode) => {
    const preset = settingsResponse?.presets?.[mode];
    if (!preset) return;
    setPolicy({ ...preset });
    setSaved(false);
  };

  const update = <K extends keyof UserPolicySettings>(field: K, value: UserPolicySettings[K]) => {
    setPolicy((prev) => ({ ...prev, [field]: value }));
  };

  const addSymbol = () => {
    const sym = symbolInput.toUpperCase().trim();
    if (sym && !policy.allowed_symbols.includes(sym)) {
      update('allowed_symbols', [...policy.allowed_symbols, sym]);
    }
    setSymbolInput('');
  };

  const removeSymbol = (sym: string) => {
    update('allowed_symbols', policy.allowed_symbols.filter((s) => s !== sym));
  };

  const toggleSession = (session: string) => {
    const selected = policy.session_filters.includes(session);
    if (selected) {
      update('session_filters', policy.session_filters.filter((item) => item !== session));
      return;
    }
    if (session === '24/7') {
      update('session_filters', ['24/7']);
      return;
    }
    update(
      'session_filters',
      [...policy.session_filters.filter((item) => item !== '24/7'), session],
    );
  };

  const runtimeSettings = settingsResponse?.runtime_settings ?? {};
  const runtimeControls = settingsResponse?.runtime_controls ?? status?.runtime_controls;
  const recentDecisions = autoTradeStatus?.recent_trades?.slice(0, 4) ?? [];
  const universe = settingsResponse?.universe ?? status?.universe;
  const finnhub = settingsResponse?.event_providers?.finnhub ?? status?.finnhub;

  return (
    <div>
      <div className="page-header">
        <div className="flex justify-between items-center">
          <div>
            <h2>Policy & Runtime</h2>
            <p>User policy stays fixed. The bot may reject more trades at runtime, but it will not relax your policy.</p>
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
          PANIC STOP IS ACTIVE - All trading is halted until you resume.
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      <div className="card mb-4">
        <div className="card-header">
          <h3>User Policy Layer</h3>
          <span className="badge badge-blue">Fixed during trading</span>
        </div>
        <div className="text-sm text-muted mb-4" style={{ lineHeight: 1.7 }}>
          These are the limits you choose. The bot trades only inside this envelope and does not raise risk on its own.
        </div>

        <div className="grid-3 gap-3 mb-4">
          {(['safe', 'balanced', 'aggressive'] as PolicyMode[]).map((mode) => (
            <button
              key={mode}
              className={`btn ${policy.mode === mode ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => applyPreset(mode)}
              style={{ padding: '14px 16px', alignItems: 'flex-start', textAlign: 'left', flexDirection: 'column' as const }}
            >
              <span style={{ fontWeight: 700, textTransform: 'capitalize' }}>{mode}</span>
              <span style={{ fontSize: 12, opacity: 0.8, marginTop: 6 }}>{PRESET_DESCRIPTIONS[mode]}</span>
            </button>
          ))}
        </div>

        <div className="grid-2 gap-4">
          <div>
            <div className="form-group">
              <label>Policy Mode</label>
              <select className="form-input" value={policy.mode} onChange={(e) => applyPreset(e.target.value as PolicyMode)}>
                <option value="safe">Safe</option>
                <option value="balanced">Balanced</option>
                <option value="aggressive">Aggressive</option>
              </select>
            </div>

            <div className="grid-2 gap-3">
              <div className="form-group">
                <label>Max Risk Per Trade (%)</label>
                <input
                  className="form-input"
                  type="number"
                  step="0.05"
                  min="0.05"
                  max="5"
                  value={policy.max_risk_per_trade}
                  onChange={(e) => update('max_risk_per_trade', parseFloat(e.target.value) || 0.25)}
                />
              </div>
              <div className="form-group">
                <label>Max Daily Drawdown (%)</label>
                <input
                  className="form-input"
                  type="number"
                  step="0.5"
                  min="1"
                  max="20"
                  value={policy.max_daily_drawdown}
                  onChange={(e) => update('max_daily_drawdown', parseFloat(e.target.value) || 2.5)}
                />
              </div>
              <div className="form-group">
                <label>Max Open Trades</label>
                <input
                  className="form-input"
                  type="number"
                  min="1"
                  max="20"
                  value={policy.max_open_trades}
                  onChange={(e) => update('max_open_trades', parseInt(e.target.value) || 3)}
                />
              </div>
              <div className="form-group">
                <label>Max Trades Per Symbol</label>
                <input
                  className="form-input"
                  type="number"
                  min="1"
                  max="10"
                  value={policy.max_trades_per_symbol}
                  onChange={(e) => update('max_trades_per_symbol', parseInt(e.target.value) || 1)}
                />
              </div>
              <div className="form-group">
                <label>Max Margin Utilization (%)</label>
                <input
                  className="form-input"
                  type="number"
                  step="1"
                  min="5"
                  max="80"
                  value={policy.max_margin_utilization}
                  onChange={(e) => update('max_margin_utilization', parseFloat(e.target.value) || 12)}
                />
              </div>
              <div className="form-group">
                <label>Min Free Margin (%)</label>
                <input
                  className="form-input"
                  type="number"
                  step="1"
                  min="10"
                  max="95"
                  value={policy.min_free_margin}
                  onChange={(e) => update('min_free_margin', parseFloat(e.target.value) || 70)}
                />
              </div>
            </div>

            <div className="grid-2 gap-3">
              <div className="form-group">
                <label>Min Reward:Risk</label>
                <input
                  className="form-input"
                  type="number"
                  step="0.1"
                  min="1"
                  max="5"
                  value={policy.min_reward_risk}
                  onChange={(e) => update('min_reward_risk', parseFloat(e.target.value) || 2.2)}
                />
              </div>
              <div className="form-group">
                <label>Gemini Role</label>
                <select
                  className="form-input"
                  value={policy.gemini_role}
                  onChange={(e) => update('gemini_role', e.target.value as UserPolicySettings['gemini_role'])}
                >
                  <option value="off">Off</option>
                  <option value="advisory">Advisory</option>
                  <option value="confirmation-required">Confirmation Required</option>
                </select>
              </div>
            </div>

            <div className="form-group">
              <label>
                <input
                  type="checkbox"
                  checked={policy.allow_counter_trend_trades}
                  onChange={(e) => update('allow_counter_trend_trades', e.target.checked)}
                  style={{ marginRight: 8 }}
                />
                Allow Counter-Trend Trades
              </label>
            </div>

            <div className="form-group">
              <label>
                <input
                  type="checkbox"
                  checked={policy.allow_overnight_holding}
                  onChange={(e) => update('allow_overnight_holding', e.target.checked)}
                  style={{ marginRight: 8 }}
                />
                Allow Overnight Holding For Session-Based Instruments
              </label>
            </div>

            <div className="form-group">
              <label>
                <input
                  type="checkbox"
                  checked={policy.demo_only_default}
                  onChange={(e) => update('demo_only_default', e.target.checked)}
                  style={{ marginRight: 8 }}
                />
                Demo-Only By Default
              </label>
            </div>
          </div>

          <div>
            <div className="form-group">
              <label>Allowed Symbols</label>
              <div className="flex gap-2 mb-2" style={{ flexWrap: 'wrap' }}>
                {policy.allowed_symbols.map((sym) => (
                  <span
                    key={sym}
                    className="badge badge-blue"
                    style={{ cursor: 'pointer' }}
                    onClick={() => removeSymbol(sym)}
                  >
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

            <div className="form-group">
              <label>Session Filters</label>
              <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
                {SESSION_OPTIONS.map((session) => (
                  <button
                    key={session}
                    className={`btn btn-sm ${policy.session_filters.includes(session) ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => toggleSession(session)}
                    type="button"
                  >
                    {session}
                  </button>
                ))}
              </div>
              <div className="text-muted text-sm mt-2">
                Sessions are user policy. The bot may still skip trades inside them if runtime conditions are weak.
              </div>
            </div>

            <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', marginTop: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <Lock size={16} style={{ color: 'var(--accent-blue)' }} />
                <strong>Policy Guarantees</strong>
              </div>
              <div className="text-sm text-muted" style={{ lineHeight: 1.8 }}>
                <div>- The bot cannot raise your max risk, margin cap, or drawdown limits automatically.</div>
                <div>- Runtime logic can reject more trades, but it cannot loosen your policy.</div>
                <div>- Safe is the default preset whenever the app starts fresh.</div>
              </div>
            </div>

            <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', marginTop: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <Shield size={16} style={{ color: 'var(--accent-blue)' }} />
                <strong>Active Market Universe</strong>
              </div>
              <div className="text-sm text-muted" style={{ lineHeight: 1.8 }}>
                <div>- Mode: {universe?.mode ?? 'event-driven-indices-commodities'}</div>
                <div>- Asset classes: {(universe?.active_asset_classes ?? []).join(', ') || 'Indices, Commodities'}</div>
                <div>- Enabled symbols: {(universe?.enabled_symbols ?? []).join(', ') || 'All broker symbols in active asset classes'}</div>
                <div>- Disabled symbols: {(universe?.disabled_symbols ?? []).join(', ') || '-'}</div>
              </div>
            </div>
          </div>
        </div>

        <div className="mt-4 flex gap-2 items-center">
          <button className="btn btn-primary" onClick={handleSave} disabled={loading}>
            {loading ? <span className="loading-spinner" /> : <Save size={14} />}
            Save Policy
          </button>
          <button className="btn btn-secondary" onClick={refreshSettings} disabled={loading}>
            Reset to Saved
          </button>
          {saved && <span className="text-green text-sm">Policy saved</span>}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Automatic Decision Layer</h3>
          <span className="badge badge-yellow">Read only</span>
        </div>
        <div className="text-sm text-muted mb-4" style={{ lineHeight: 1.7 }}>
          These runtime checks are computed live by the bot. They can block or downgrade trades when market conditions deteriorate.
        </div>

        <div className="grid-3 gap-3 mb-4">
          <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Activity size={15} />
              <strong>Signal Quality</strong>
            </div>
            <div className="text-sm text-muted">
              Mode-derived minimum confidence: {typeof runtimeSettings.min_confidence_threshold === 'number'
                ? `${Math.round((runtimeSettings.min_confidence_threshold as number) * 100)}%`
                : '-'}
            </div>
            <div className="text-sm text-muted">
              Auto-trade floor: {typeof runtimeSettings.auto_trade_min_confidence === 'number'
                ? `${Math.round((runtimeSettings.auto_trade_min_confidence as number) * 100)}%`
                : '-'}
            </div>
          </div>

          <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Bot size={15} />
              <strong>News Alignment</strong>
            </div>
            <div className="text-sm text-muted">Policy role: {policy.gemini_role}</div>
            <div className="text-sm text-muted">
              Gemini state: {status?.gemini_degraded ? 'Degraded' : status?.gemini_available ? 'Available' : 'Offline'}
            </div>
            <div className="text-sm text-muted">
              Finnhub: {!finnhub?.enabled ? 'Disabled' : finnhub?.available ? 'Available' : finnhub?.degraded ? 'Degraded' : 'Offline'}
            </div>
          </div>

          <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Shield size={15} />
              <strong>Portfolio Fit</strong>
            </div>
            <div className="text-sm text-muted">Margin utilization: {formatPct(status?.portfolio?.margin_utilization_pct)}</div>
            <div className="text-sm text-muted">Free margin: {formatPct(status?.portfolio?.free_margin_pct)}</div>
            <div className="text-sm text-muted">Open positions: {status?.portfolio?.open_positions_total ?? 0}</div>
          </div>
        </div>

        <div className="grid-2 gap-4">
          <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div className="card-header" style={{ paddingBottom: 8 }}>
              <h3 style={{ fontSize: 15 }}>Live Runtime Gates</h3>
            </div>
            <div className="text-sm text-muted" style={{ lineHeight: 1.9 }}>
              <div>- Spread quality is checked again just before execution.</div>
              <div>- Volatility regime is inferred from ATR and symbol profile.</div>
              <div>- Execution feasibility checks tradeability, margin, and reward:risk at live price.</div>
              <div>- Position management actions remain automatic and plan-driven after entry.</div>
              <div>- Auto-trade: {runtimeControls?.auto_trade_enabled ? 'Enabled' : 'Disabled'}</div>
              <div>- Scan interval: {runtimeControls?.auto_trade_scan_interval_seconds ?? '-'}s</div>
            </div>
          </div>

          <div className="card" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div className="card-header" style={{ paddingBottom: 8 }}>
              <h3 style={{ fontSize: 15 }}>Recent Bot Decisions</h3>
            </div>
            {recentDecisions.length === 0 ? (
              <div className="text-sm text-muted">No recent auto-trade decisions.</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {recentDecisions.map((decision, index) => (
                  <div key={`${decision.timestamp}-${index}`} style={{ padding: '10px 12px', borderRadius: 8, background: 'var(--bg-tertiary)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                      <strong>{decision.symbol} {decision.action}</strong>
                      <span className={`badge ${decision.success ? 'badge-green' : 'badge-red'}`}>
                        {decision.success ? 'Passed' : 'Blocked'}
                      </span>
                    </div>
                    <div className="text-sm text-muted" style={{ marginTop: 6 }}>
                      Quality: {decision.quality_score != null ? `${Math.round(decision.quality_score * 100)}%` : '-'}
                    </div>
                    <div className="text-sm text-muted">{decision.detail}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
