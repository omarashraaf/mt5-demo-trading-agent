import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plug, PlugZap, Search, AlertTriangle, HelpCircle, CheckCircle, ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse } from '../types';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void | Promise<void>;
}
const LAST_PLATFORM_KEY = 'linktrade_last_connected_platform';
type ConnectionPlatform = 'mt5' | 'ibkr' | 'tradestation';

export default function Connection({ status, onRefresh }: Props) {
  const navigate = useNavigate();
  const [localStatusOverride, setLocalStatusOverride] = useState<StatusResponse | null>(null);
  const [selectedPlatform, setSelectedPlatform] = useState<ConnectionPlatform>('mt5');
  const [terminalPath, setTerminalPath] = useState('');
  const [ibkrHost, setIbkrHost] = useState('127.0.0.1');
  const [ibkrEnvironment, setIbkrEnvironment] = useState<'paper' | 'live'>('paper');
  const [ibkrPort, setIbkrPort] = useState('7497');
  const [ibkrClientId, setIbkrClientId] = useState('1');
  const [ibkrReadonly, setIbkrReadonly] = useState(false);
  const [tradeStationEnvironment, setTradeStationEnvironment] = useState<'sim' | 'live'>('sim');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [verifyResult, setVerifyResult] = useState<any>(null);

  const effectiveStatus = localStatusOverride ?? status;
  const connected = Boolean(effectiveStatus?.connected && effectiveStatus?.account);
  const connectedPlatform = effectiveStatus?.platform || 'mt5';
  const autoConnectIssue = effectiveStatus?.auto_connect?.last_error
    || effectiveStatus?.auto_connect?.pause_reason
    || '';
  const liveReadiness = effectiveStatus?.live_trading_readiness;
  const liveReadinessIssue = !connected && liveReadiness?.status !== 'ready'
    ? (liveReadiness?.blockers?.[0] || liveReadiness?.warnings?.[0] || '')
    : '';
  const connectionIssue = error || autoConnectIssue || liveReadinessIssue;

  useEffect(() => {
    if (status?.connected) {
      setLocalStatusOverride(status);
    } else if (!status?.connected) {
      setLocalStatusOverride(null);
    }
  }, [status, loading]);

  const buildOptimisticStatus = useCallback((result: any, nextPlatform: ConnectionPlatform): StatusResponse => ({
    connected: true,
    platform: nextPlatform,
    platforms_supported: ['mt5', 'ibkr', 'tradestation'],
    account: result?.account ?? null,
    terminal: effectiveStatus?.terminal ?? null,
    last_error: null,
    is_demo: Boolean(result?.is_demo ?? effectiveStatus?.is_demo ?? true),
    live_trading_enabled: effectiveStatus?.live_trading_enabled ?? true,
    panic_stop: effectiveStatus?.panic_stop ?? false,
    active_agent: effectiveStatus?.active_agent || 'SmartAgent',
    active_strategy: effectiveStatus?.active_strategy,
    credential_storage_available: effectiveStatus?.credential_storage_available,
    gemini_available: effectiveStatus?.gemini_available,
    gemini_degraded: effectiveStatus?.gemini_degraded,
    gemini_last_error: effectiveStatus?.gemini_last_error,
    gemini_state: effectiveStatus?.gemini_state,
    gemini_cooldown_seconds: effectiveStatus?.gemini_cooldown_seconds,
    gemini_credits_pct: effectiveStatus?.gemini_credits_pct,
    user_policy: effectiveStatus?.user_policy,
    runtime_controls: effectiveStatus?.runtime_controls,
    universe: effectiveStatus?.universe,
    benzinga: effectiveStatus?.benzinga,
    portfolio: effectiveStatus?.portfolio,
    services: effectiveStatus?.services,
    auto_connect: { pending: false },
  }), [effectiveStatus]);

  const syncConnectedState = useCallback(async (fallback: StatusResponse) => {
    setLocalStatusOverride(fallback);
    try {
      localStorage.setItem(LAST_PLATFORM_KEY, String(fallback.platform || 'mt5'));
    } catch {
      // ignore storage issues
    }
    for (let i = 0; i < 5; i += 1) {
      try {
        const live = await api.getStatus();
        if (live?.connected) {
          setLocalStatusOverride(live);
          try {
            localStorage.setItem(LAST_PLATFORM_KEY, String(live.platform || fallback.platform || 'mt5'));
          } catch {
            // ignore storage issues
          }
          await onRefresh();
          navigate('/');
          return true;
        }
      } catch {
        // keep optimistic state briefly while backend status catches up
      }
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
    await onRefresh();
    navigate('/');
    return false;
  }, [navigate, onRefresh]);

  const handleConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.connect({
        platform: selectedPlatform,
        terminal_path: selectedPlatform === 'mt5' ? terminalPath || undefined : undefined,
        host: selectedPlatform === 'ibkr' ? ibkrHost : undefined,
        port: selectedPlatform === 'ibkr' ? Number(ibkrPort || 7497) : undefined,
        client_id: selectedPlatform === 'ibkr' ? Number(ibkrClientId || 1) : undefined,
        readonly: selectedPlatform === 'ibkr' ? ibkrReadonly : undefined,
        ibkr_environment: selectedPlatform === 'ibkr' ? ibkrEnvironment : undefined,
        environment: selectedPlatform === 'tradestation' ? tradeStationEnvironment : undefined,
      });
      if (result?.auth_required && result.auth_url) {
        window.open(result.auth_url, '_blank', 'noopener,noreferrer');
        setError('TradeStation login opened in your browser. Complete authorization on TradeStation, then return here.');
        return;
      }
      if (!result?.connected) {
        setError('Connection did not complete. Please try again.');
        return;
      }
      await syncConnectedState(buildOptimisticStatus(result, selectedPlatform));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      setLocalStatusOverride(null);
      await api.disconnect();
      await onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async () => {
    setLoading(true);
    setVerifyResult(null);
    try {
      const result = await api.verifyTerminal(terminalPath || undefined);
      setVerifyResult(result);
    } catch (e: any) {
      setVerifyResult({ ok: false, error: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 700, margin: '0 auto' }}>
      <div className="page-header">
        <h2>Connect Broker Platform</h2>
        <p>Choose a platform and authenticate only inside the broker-owned local app or browser page.</p>
      </div>

      {!connected && (
        <div style={{
          background: 'rgba(59, 130, 246, 0.06)',
          border: '1px solid rgba(59, 130, 246, 0.15)',
          borderRadius: 8,
          padding: '16px 20px',
          marginBottom: 20,
          fontSize: 13,
          color: 'var(--text-secondary)',
          lineHeight: 1.6,
        }}>
          <div className="flex items-center gap-2" style={{ fontWeight: 600, marginBottom: 8, color: 'var(--accent-blue)' }}>
            <HelpCircle size={14} />
            How this connection works
          </div>
          <ol style={{ paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <li><strong>MT5</strong>: open or log in inside MetaTrader 5 locally, then connect.</li>
            <li><strong>IBKR</strong>: open and log in inside TWS or IB Gateway with API access enabled, then connect to localhost.</li>
            <li><strong>TradeStation</strong>: the app opens TradeStation's authorization page in your browser.</li>
            <li>Live accounts still require live trading enabled and <strong>Demo-Only By Default</strong> turned off in Policy & Runtime.</li>
          </ol>
          {liveReadinessIssue && (
            <div className="warning-banner" style={{ fontSize: 12, marginTop: 12, padding: '8px 12px' }}>
              <AlertTriangle size={12} />
              {liveReadinessIssue}
            </div>
          )}
        </div>
      )}

      {connected && status?.account && (
        <div className="card" style={{ marginBottom: 20 }}>
          {connectionIssue && (
            <div className="error-banner" style={{ fontSize: 13, marginBottom: 12 }}>
              {connectionIssue}
            </div>
          )}
          <div className="flex items-center gap-3" style={{ marginBottom: 16 }}>
            <CheckCircle size={24} style={{ color: 'var(--accent-green)' }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 16 }}>Connected Successfully!</div>
              <div className="text-muted" style={{ fontSize: 13 }}>
                Your {connectedPlatform.toUpperCase()} account is ready
              </div>
            </div>
          </div>
          <div style={{
            background: 'var(--bg-tertiary)',
            borderRadius: 8,
            padding: 16,
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 12,
            fontSize: 13,
            marginBottom: 16,
          }}>
            <div>
              <span className="text-muted">Account: </span>
              <span style={{ fontWeight: 600 }}>{status.account.name || status.account.login}</span>
            </div>
            <div>
              <span className="text-muted">Server: </span>
              <span>{status.account.server}</span>
            </div>
            <div>
              <span className="text-muted">Balance: </span>
              <span style={{ fontWeight: 600 }}>{status.account.balance.toFixed(2)} {status.account.currency}</span>
            </div>
            <div>
              <span className="text-muted">Platform: </span>
              <span className="badge badge-green">{connectedPlatform.toUpperCase()}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => navigate('/')}
            >
              Go to Dashboard
            </button>
            <button className="btn btn-secondary" onClick={handleDisconnect} disabled={loading}>
              <PlugZap size={14} /> Disconnect
            </button>
          </div>
        </div>
      )}

      {!connected && (
        <div className="card">
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 20 }}>Local Platform Connection</h3>

          {connectionIssue && (
            <div className="error-banner" style={{ fontSize: 13 }}>
              {connectionIssue}
            </div>
          )}

          <div className="form-group">
            <label>Platform</label>
            <select
              className="form-input"
              value={selectedPlatform}
              onChange={(e) => setSelectedPlatform(e.target.value as ConnectionPlatform)}
              style={{ fontSize: 15, padding: '12px 16px' }}
            >
              <option value="mt5">MetaTrader 5 (local terminal)</option>
              <option value="ibkr">Interactive Brokers (local TWS / IB Gateway)</option>
              <option value="tradestation">TradeStation (browser authorization)</option>
            </select>
          </div>

          <div className="warning-banner" style={{ fontSize: 12, padding: '10px 12px', marginBottom: 12 }}>
            <AlertTriangle size={13} />
            This app never asks for broker login passwords. Log in inside MT5, TWS / IB Gateway, or TradeStation's own browser page.
          </div>

          {selectedPlatform === 'ibkr' && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 120px 120px', gap: 10, marginBottom: 12 }}>
              <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                <label>IBKR account environment</label>
                <select
                  className="form-input"
                  value={ibkrEnvironment}
                  onChange={(e) => {
                    const environment = e.target.value as 'paper' | 'live';
                    setIbkrEnvironment(environment);
                    setIbkrPort(environment === 'paper' ? '7497' : '7496');
                  }}
                >
                  <option value="paper">Paper trading</option>
                  <option value="live">Live account</option>
                </select>
                <div className="text-muted" style={{ fontSize: 11, marginTop: 5 }}>
                  TWS defaults: paper 7497, live 7496. IB Gateway commonly uses paper 4002 and live 4001.
                </div>
              </div>
              <div className="form-group">
                <label>TWS / Gateway host</label>
                <input className="form-input" value={ibkrHost} onChange={(e) => setIbkrHost(e.target.value)} />
              </div>
              <div className="form-group">
                <label>Port</label>
                <input className="form-input" value={ibkrPort} onChange={(e) => setIbkrPort(e.target.value)} />
              </div>
              <div className="form-group">
                <label>Client ID</label>
                <input className="form-input" value={ibkrClientId} onChange={(e) => setIbkrClientId(e.target.value)} />
              </div>
              <label className="flex items-center gap-2" style={{ fontSize: 12, gridColumn: '1 / -1' }}>
                <input type="checkbox" checked={ibkrReadonly} onChange={(e) => setIbkrReadonly(e.target.checked)} />
                Read-only IBKR API session
              </label>
            </div>
          )}

          {selectedPlatform === 'tradestation' && (
            <div className="form-group">
              <label>TradeStation environment</label>
              <select
                className="form-input"
                value={tradeStationEnvironment}
                onChange={(e) => setTradeStationEnvironment(e.target.value as 'sim' | 'live')}
              >
                <option value="sim">SIM</option>
                <option value="live">LIVE</option>
              </select>
            </div>
          )}

          {selectedPlatform === 'mt5' && <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setShowAdvanced((value) => !value)}
            style={{ marginBottom: showAdvanced ? 12 : 0 }}
          >
            {showAdvanced ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            Terminal path
          </button>}

          {selectedPlatform === 'mt5' && showAdvanced && (
            <div style={{ marginBottom: 12 }}>
              <div className="form-group">
                <label>MT5 terminal executable</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="Optional path to terminal64.exe"
                  value={terminalPath}
                  onChange={(e) => setTerminalPath(e.target.value)}
                  style={{ fontSize: 13, padding: '10px 12px' }}
                />
              </div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={handleVerify} disabled={loading}>
                <Search size={13} />
                Verify terminal
              </button>
              {verifyResult && (
                <div
                  className={verifyResult.ok ? 'warning-banner' : 'error-banner'}
                  style={{ fontSize: 12, marginTop: 10, padding: '8px 12px' }}
                >
                  {verifyResult.ok
                    ? `Terminal found: ${verifyResult.name || 'MetaTrader 5'} build ${verifyResult.build || 'unknown'}`
                    : (verifyResult.error || 'Terminal verification failed')}
                </div>
              )}
            </div>
          )}

          <button
            className="btn btn-primary"
            onClick={handleConnect}
            disabled={loading}
            style={{ fontSize: 15, padding: '14px 32px', width: '100%', justifyContent: 'center', marginTop: 8 }}
          >
            {loading ? <span className="loading-spinner" /> : <Plug size={16} />}
            {selectedPlatform === 'mt5'
              ? 'Open / Connect Local MT5'
              : selectedPlatform === 'ibkr'
              ? `Connect IBKR ${ibkrEnvironment === 'paper' ? 'Paper' : 'Live'} Account`
              : 'Open TradeStation Login'}
          </button>
        </div>
      )}
    </div>
  );
}
