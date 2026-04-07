import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plug, PlugZap, Search, Trash2, AlertTriangle, HelpCircle, CheckCircle, ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse, SavedCredentials } from '../types';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
}

type SavedIbkrAccount = {
  id: string;
  host: string;
  accountType: 'paper' | 'live';
  clientId: string;
  accountId: string;
  label: string;
  createdAt: number;
};

const SAVED_IBKR_KEY = 'trading_saved_ibkr_accounts_v1';
const SAVED_MT5_CACHE_KEY = 'trading_saved_mt5_accounts_v1';

export default function Connection({ status, onRefresh }: Props) {
  const navigate = useNavigate();
  const [platform, setPlatform] = useState<'mt5' | 'ibkr'>((status?.platform as 'mt5' | 'ibkr') || 'mt5');
  const [account, setAccount] = useState('');
  const [password, setPassword] = useState('');
  const [server, setServer] = useState('');
  const [terminalPath, setTerminalPath] = useState('');
  const [ibkrHost, setIbkrHost] = useState('127.0.0.1');
  const [ibkrAccountType, setIbkrAccountType] = useState<'paper' | 'live'>('paper');
  const [ibkrClientId, setIbkrClientId] = useState('1');
  const [ibkrAccountId, setIbkrAccountId] = useState('');
  const [rememberCredentials, setRememberCredentials] = useState(false);
  const [rememberIbkrSettings, setRememberIbkrSettings] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [verifyResult, setVerifyResult] = useState<any>(null);
  const [savedCreds, setSavedCreds] = useState<SavedCredentials[]>([]);
  const [savedCredsError, setSavedCredsError] = useState('');
  const [savedIbkrAccounts, setSavedIbkrAccounts] = useState<SavedIbkrAccount[]>([]);
  const [savedCredsLoaded, setSavedCredsLoaded] = useState(false);
  const [savedCredsRetryCount, setSavedCredsRetryCount] = useState(0);

  const connected = Boolean(status?.connected && status?.account);
  const connectedPlatform = (status?.platform as 'mt5' | 'ibkr') || 'mt5';

  useEffect(() => {
    if (status?.platform) {
      setPlatform(status.platform);
    }
  }, [status?.platform]);

  const ibkrPort = ibkrAccountType === 'live' ? '7496' : '7497';

  const refreshSavedCredentials = useCallback(async () => {
    try {
      const creds = await api.getCredentials();
      const normalized = creds || [];
      setSavedCreds(normalized);
      localStorage.setItem(SAVED_MT5_CACHE_KEY, JSON.stringify(normalized));
      setSavedCredsError('');
      setSavedCredsLoaded(true);
      setSavedCredsRetryCount(0);
    } catch (e: any) {
      setSavedCredsLoaded(true);
      // Keep cached entries on transient backend start delays.
      setSavedCredsError(e?.message || 'Could not load saved MT5 accounts.');
    }
  }, []);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(SAVED_MT5_CACHE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as SavedCredentials[];
        if (Array.isArray(parsed) && parsed.length > 0) {
          setSavedCreds(parsed);
        }
      }
    } catch {
      // ignore malformed local cache
    }
    void refreshSavedCredentials();
  }, [refreshSavedCredentials]);

  useEffect(() => {
    if (connected) return;
    if (savedCreds.length > 0) return;
    if (!savedCredsLoaded) return;
    if (savedCredsRetryCount >= 3) return;
    if (!savedCredsError) return;

    const timer = window.setTimeout(() => {
      setSavedCredsRetryCount((prev) => prev + 1);
      void refreshSavedCredentials();
    }, 2500);

    return () => window.clearTimeout(timer);
  }, [
    connected,
    refreshSavedCredentials,
    savedCreds.length,
    savedCredsError,
    savedCredsLoaded,
    savedCredsRetryCount,
  ]);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(SAVED_IBKR_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as SavedIbkrAccount[];
      if (Array.isArray(parsed)) {
        setSavedIbkrAccounts(parsed);
      }
    } catch {
      // ignore malformed local cache
    }
  }, []);

  const persistIbkrAccounts = (accounts: SavedIbkrAccount[]) => {
    setSavedIbkrAccounts(accounts);
    localStorage.setItem(SAVED_IBKR_KEY, JSON.stringify(accounts));
  };

  const handleDeleteCredentials = async (acct: number) => {
    try {
      await api.deleteCredentials(acct);
      setSavedCreds((prev) => prev.filter((c) => c.account !== acct));
    } catch {}
  };

  const handleDeleteIbkrSettings = (id: string) => {
    persistIbkrAccounts(savedIbkrAccounts.filter((item) => item.id !== id));
  };

  const handleLoadIbkrSettings = (saved: SavedIbkrAccount) => {
    setPlatform('ibkr');
    setIbkrHost(saved.host || '127.0.0.1');
    setIbkrAccountType(saved.accountType || 'paper');
    setIbkrClientId(saved.clientId || '1');
    setIbkrAccountId(saved.accountId || '');
  };

  const handleAutoConnectIbkr = async (saved: SavedIbkrAccount) => {
    setLoading(true);
    setError('');
    try {
      const result = await api.connect({
        platform: 'ibkr',
        ibkr_host: saved.host || '127.0.0.1',
        ibkr_port: saved.accountType === 'live' ? 7496 : 7497,
        ibkr_client_id: parseInt(saved.clientId || '1', 10),
        ibkr_account_id: saved.accountId || undefined,
      });
      if (!result?.connected) {
        setError('Auto-connect failed');
      } else {
        onRefresh();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.connect(
        platform === 'mt5'
          ? {
              platform: 'mt5',
              account: parseInt(account),
              password,
              server,
              terminal_path: terminalPath || undefined,
              save_credentials: rememberCredentials,
            }
          : {
              platform: 'ibkr',
              ibkr_host: ibkrHost.trim() || '127.0.0.1',
              ibkr_port: parseInt(ibkrPort, 10),
              ibkr_client_id: parseInt(ibkrClientId || '1'),
              ibkr_account_id: ibkrAccountId.trim() || undefined,
            }
      );
      if (rememberCredentials && result?.credential_status?.saved === false) {
        setError(result.credential_status.reason || 'Connected, but credentials could not be saved securely.');
      }
      if (platform === 'ibkr' && rememberIbkrSettings) {
        const normalizedHost = ibkrHost.trim() || '127.0.0.1';
        const normalizedClientId = String(parseInt(ibkrClientId || '1', 10));
        const normalizedAccount = ibkrAccountId.trim();
        const deduped = savedIbkrAccounts.filter((item) => !(
          item.host === normalizedHost
          && item.accountType === ibkrAccountType
          && item.clientId === normalizedClientId
          && item.accountId === normalizedAccount
        ));
        const entry: SavedIbkrAccount = {
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          host: normalizedHost,
          accountType: ibkrAccountType,
          clientId: normalizedClientId,
          accountId: normalizedAccount,
          label: normalizedAccount || `${ibkrAccountType.toUpperCase()} @ ${normalizedHost}`,
          createdAt: Date.now(),
        };
        persistIbkrAccounts([entry, ...deduped].slice(0, 30));
      }
      onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await api.disconnect();
      onRefresh();
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

  const handleAutoConnect = async (acct?: number) => {
    setLoading(true);
    setError('');
    try {
      const result = await api.autoConnect(acct);
      if (!result.connected) {
        setError(result.reason || 'Auto-connect failed');
      } else if (!result.account) {
        setError('Connection session is stale. Please click Auto Connect again or use Connect manually.');
      } else {
        onRefresh();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 700, margin: '0 auto' }}>
      <div className="page-header">
        <h2>Connect to Trading Platform</h2>
        <p>Select MT5 or IBKR and connect with the required details</p>
      </div>

      {!connected && platform === 'mt5' && (
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
            Where do I find these details?
          </div>
          <ol style={{ paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <li>Open your <strong>MetaTrader 5</strong> application on your computer</li>
            <li>Your <strong>Account Number</strong> is shown in the Navigator panel (left side) or the title bar</li>
            <li>Your <strong>Password</strong> and <strong>Server</strong> were emailed to you when you created the demo account</li>
            <li>If you don't have a demo account yet, open MT5 and select <strong>File &gt; Open an Account</strong></li>
          </ol>
        </div>
      )}

      {!connected && platform === 'ibkr' && (
        <div style={{
          background: 'rgba(16, 185, 129, 0.06)',
          border: '1px solid rgba(16, 185, 129, 0.15)',
          borderRadius: 8,
          padding: '16px 20px',
          marginBottom: 20,
          fontSize: 13,
          color: 'var(--text-secondary)',
          lineHeight: 1.6,
        }}>
          <div className="flex items-center gap-2" style={{ fontWeight: 600, marginBottom: 8, color: 'var(--accent-green)' }}>
            <HelpCircle size={14} />
            IBKR connection checklist
          </div>
          <ol style={{ paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <li>Open <strong>TWS</strong> or <strong>IB Gateway</strong> on your PC</li>
            <li>Enable API: <strong>Configure &gt; API &gt; Settings &gt; Enable ActiveX and Socket Clients</strong></li>
            <li>Use port <strong>7497</strong> for Paper, <strong>7496</strong> for Live (unless customized)</li>
            <li>Use a unique <strong>Client ID</strong> (example: 1)</li>
          </ol>
        </div>
      )}

      {connected && status?.account && (
        <div className="card" style={{ marginBottom: 20 }}>
          {error && (
            <div className="error-banner" style={{ fontSize: 13, marginBottom: 12 }}>
              {error}
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
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 20 }}>Account Details</h3>

          {error && (
            <div className="error-banner" style={{ fontSize: 13 }}>
              {error}
            </div>
          )}

          <div className="form-group">
            <label>Platform</label>
            <select
              className="form-input"
              value={platform}
              onChange={(e) => setPlatform(e.target.value as 'mt5' | 'ibkr')}
            >
              <option value="mt5">MetaTrader 5 (MT5)</option>
              <option value="ibkr">Interactive Brokers (IBKR)</option>
            </select>
          </div>

          {platform === 'mt5' ? (
            <>
              <div className="form-group">
                <label>Account Number</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="e.g. 12345678"
                  value={account}
                  onChange={(e) => setAccount(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>

              <div className="form-group">
                <label>Password</label>
                <input
                  className="form-input"
                  type="password"
                  placeholder="Your MT5 password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>

              <div className="form-group">
                <label>Server</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="e.g. MetaQuotes-Demo"
                  value={server}
                  onChange={(e) => setServer(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>
            </>
          ) : (
            <>
              <div className="form-group">
                <label>IBKR Host</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="127.0.0.1"
                  value={ibkrHost}
                  onChange={(e) => setIbkrHost(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>
              <div className="form-group">
                <label>Account Type</label>
                <select
                  className="form-input"
                  value={ibkrAccountType}
                  onChange={(e) => setIbkrAccountType(e.target.value as 'paper' | 'live')}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                >
                  <option value="paper">Paper Trading (Port 7497)</option>
                  <option value="live">Live Account (Port 7496)</option>
                </select>
                <div className="text-muted" style={{ fontSize: 12, marginTop: 6 }}>
                  Port is set automatically to <strong>{ibkrPort}</strong> based on account type.
                </div>
              </div>
              <div className="form-group">
                <label>Client ID</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="1"
                  value={ibkrClientId}
                  onChange={(e) => setIbkrClientId(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>
              <div className="form-group">
                <label>Account ID (optional)</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="DUxxxxxxx"
                  value={ibkrAccountId}
                  onChange={(e) => setIbkrAccountId(e.target.value)}
                  style={{ fontSize: 15, padding: '12px 16px' }}
                />
              </div>
            </>
          )}

          {platform === 'mt5' && (
            <label className="flex items-center gap-2" style={{ fontSize: 13, marginBottom: 16 }}>
              <input
                type="checkbox"
                checked={rememberCredentials}
                onChange={(e) => setRememberCredentials(e.target.checked)}
                disabled={status?.credential_storage_available === false}
              />
              Remember this account on this device
            </label>
          )}
          {platform === 'ibkr' && (
            <label className="flex items-center gap-2" style={{ fontSize: 13, marginBottom: 16 }}>
              <input
                type="checkbox"
                checked={rememberIbkrSettings}
                onChange={(e) => setRememberIbkrSettings(e.target.checked)}
              />
              Save account settings on this device
            </label>
          )}
          {status?.credential_storage_available === false && platform === 'mt5' && (
            <div className="warning-banner" style={{ fontSize: 11, padding: '8px 12px', marginBottom: 12 }}>
              <AlertTriangle size={12} />
              Secure credential storage is unavailable, so saved login is disabled.
            </div>
          )}

          {platform === 'mt5' && (
            <>
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: 'var(--text-muted)',
                  fontSize: 12,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  padding: '8px 0',
                  marginBottom: showAdvanced ? 8 : 0,
                }}
              >
                {showAdvanced ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Advanced settings
              </button>

              {showAdvanced && (
                <>
                  <div className="form-group">
                    <label>Terminal Path (usually auto-detected)</label>
                    <input
                      className="form-input"
                      type="text"
                      placeholder="Leave empty for auto-detect"
                      value={terminalPath}
                      onChange={(e) => setTerminalPath(e.target.value)}
                    />
                  </div>
                  <button className="btn btn-secondary btn-sm mb-4" onClick={handleVerify} disabled={loading}>
                    <Search size={12} /> Verify Terminal
                  </button>
                  {verifyResult && (
                    <div className={`text-sm ${verifyResult.ok ? 'text-green' : 'text-red'}`} style={{ marginBottom: 12 }}>
                      {verifyResult.ok
                        ? `Found: ${verifyResult.name} (Build ${verifyResult.build})`
                        : `Not found: ${verifyResult.error}`
                      }
                    </div>
                  )}
                </>
              )}
            </>
          )}

          <button
            className="btn btn-primary"
            onClick={handleConnect}
            disabled={
              loading
              || (platform === 'mt5' && (!account || !password || !server))
              || (platform === 'ibkr' && (!ibkrHost || !ibkrClientId))
            }
            style={{ fontSize: 15, padding: '14px 32px', width: '100%', justifyContent: 'center', marginTop: 8 }}
          >
            {loading ? <span className="loading-spinner" /> : <Plug size={16} />}
            {platform === 'mt5' ? 'Connect to MT5' : 'Connect to IBKR'}
          </button>
        </div>
      )}

      {!connected && (
        <div className="card mt-4">
          <div className="flex justify-between items-center" style={{ marginBottom: 12 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600 }}>Saved Accounts</h3>
            <button className="btn btn-secondary btn-sm" onClick={() => void refreshSavedCredentials()}>
              Refresh
            </button>
          </div>
          <div className="warning-banner" style={{ fontSize: 11, padding: '8px 12px' }}>
            <CheckCircle size={12} />
            Passwords are stored outside SQLite and are never returned to the UI.
          </div>
          {savedCredsError && (
            <div className="error-banner" style={{ fontSize: 12, marginTop: 10 }}>
              {savedCredsError}
            </div>
          )}
          {savedCreds.length === 0 && savedIbkrAccounts.length === 0 && !savedCredsError && savedCredsLoaded && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
              No saved accounts found yet.
            </div>
          )}
          {!savedCredsLoaded && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
              Loading saved MT5 accounts...
            </div>
          )}

          {savedCreds.map((c) => (
            <div key={`mt5-${c.account}`} className="flex justify-between items-center" style={{
              padding: '10px 0',
              borderBottom: '1px solid var(--border)',
              fontSize: 13,
            }}>
              <div className="flex items-center gap-2">
                <span className="badge badge-blue" style={{ fontSize: 10 }}>MT5</span>
                <span style={{ fontWeight: 600 }}>{c.account}</span>
                <span className="text-muted">on {c.server}</span>
              </div>
              <div className="flex gap-2">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => {
                    setPlatform('mt5');
                    setAccount(String(c.account));
                    setServer(c.server);
                    setTerminalPath(c.terminal_path || '');
                    setPassword('');
                  }}
                >
                  Load
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleAutoConnect(c.account)}>
                  Auto Connect
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleDeleteCredentials(c.account)}>
                  <Trash2 size={10} />
                </button>
              </div>
            </div>
          ))}

          {savedIbkrAccounts.map((saved) => (
            <div key={`ibkr-${saved.id}`} className="flex justify-between items-center" style={{
              padding: '10px 0',
              borderBottom: '1px solid var(--border)',
              fontSize: 13,
            }}>
              <div className="flex items-center gap-2">
                <span className="badge badge-purple" style={{ fontSize: 10 }}>IBKR</span>
                <span style={{ fontWeight: 600 }}>{saved.accountId || `${saved.accountType.toUpperCase()} account`}</span>
                <span className="text-muted">on {saved.host}:{saved.accountType === 'live' ? 7496 : 7497}</span>
              </div>
              <div className="flex gap-2">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => handleLoadIbkrSettings(saved)}
                >
                  Load
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleAutoConnectIbkr(saved)}>
                  Auto Connect
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleDeleteIbkrSettings(saved.id)}>
                  <Trash2 size={10} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
