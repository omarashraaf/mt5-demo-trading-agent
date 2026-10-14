import { useState } from 'react';
import { AlertTriangle, Plug, PlugZap, ShieldCheck, Wifi } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse } from '../types';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
  reconnecting?: boolean;
}

export default function ConnectionBar({ status, onRefresh, reconnecting = false }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const connected = status?.connected ?? false;
  const readiness = status?.publish_readiness;
  const killSwitch = status?.kill_switch;
  const autoConnectIssue = status?.auto_connect?.last_error
    || status?.auto_connect?.pause_reason
    || '';
  const liveReadinessIssue = !connected && status?.live_trading_readiness?.status !== 'ready'
    ? (status?.live_trading_readiness?.blockers?.[0] || status?.live_trading_readiness?.warnings?.[0] || '')
    : '';
  const connectionIssue = error || autoConnectIssue || liveReadinessIssue;
  const readinessColor = readiness?.status === 'ready'
    ? 'var(--accent-green)'
    : readiness?.status === 'blocked'
      ? 'var(--accent-red)'
      : 'var(--accent-orange)';
  const readinessIssue = readiness?.blockers?.[0] || readiness?.warnings?.[0] || '';

  const waitForConnection = async () => {
    for (let i = 0; i < 30; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const st = await api.getStatus();
        if (st.connected) {
          onRefresh();
          return true;
        }
        if (!st.auto_connect?.pending && i >= 3) {
          setError(st.auto_connect?.last_error || st.live_trading_readiness?.blockers?.[0] || '');
          return false;
        }
      } catch {
        // keep waiting briefly while backend settles
      }
    }
    return false;
  };

  const handleAutoConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.connect({ platform: 'mt5' });
      if (result.connected) {
        onRefresh();
      } else {
        const connectedNow = await waitForConnection();
        if (!connectedNow) {
          setError((prev) => prev || 'Still connecting. Please wait a few seconds and try again.');
        }
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleConnectAction = () => {
    setError('');
    window.location.hash = '#/connection';
  };
  const handleDisconnect = async () => {
    setLoading(true);
    setError('');
    try {
      await api.disconnect();
      onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };
  if (connected) {
    return (
      <div className="card" style={{ padding: '12px 20px' }}>
        {killSwitch?.enabled && (
          <div className="error-banner" style={{ marginBottom: 10 }}>
            Global trading kill switch is active{killSwitch.reason ? `: ${killSwitch.reason}` : ''}. New orders and AutoTrader are blocked.
          </div>
        )}
        <div className="flex justify-between items-center">
          <div className="flex items-center gap-3">
            <Wifi size={16} className="text-green" />
            <span style={{ fontWeight: 600, fontSize: 14 }}>
              {status?.account?.name || (status?.account?.login ? `Account ${status.account.login}` : 'Trading Session Connected')}
            </span>
            <span className={`badge ${status?.is_demo ? 'badge-green' : 'badge-yellow'}`}>
              {status?.is_demo ? 'DEMO' : 'LIVE'}
            </span>
            {readiness && (
              <span
                className="badge"
                style={{
                  borderColor: readinessColor,
                  color: readinessColor,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  maxWidth: 520,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={readinessIssue}
              >
                {readiness.status === 'ready' ? <ShieldCheck size={12} /> : <AlertTriangle size={12} />}
                Publish {readiness.score.toFixed(1)}/10
                {readinessIssue ? `: ${readinessIssue}` : ''}
              </span>
            )}
          </div>
          <button className="btn btn-secondary btn-sm" onClick={handleDisconnect} disabled={loading}>
            <PlugZap size={12} /> Disconnect
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: '12px 20px' }}>
      {killSwitch?.enabled && (
        <div className="error-banner" style={{ marginBottom: 10 }}>
          Global trading kill switch is active{killSwitch.reason ? `: ${killSwitch.reason}` : ''}. New orders and AutoTrader are blocked.
        </div>
      )}
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          {reconnecting ? <span className="loading-spinner" style={{ width: 14, height: 14 }} /> : <span className="status-dot disconnected" />}
          <span style={{ fontSize: 13 }}>{reconnecting ? 'Reconnecting to trading account...' : 'Not connected to trading account'}</span>
          {connectionIssue && <span className="text-red text-sm">{connectionIssue}</span>}
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-primary" onClick={handleConnectAction} disabled={loading || reconnecting} style={{ fontSize: 13 }}>
            {loading ? <span className="loading-spinner" /> : <Plug size={14} />}
            {reconnecting ? 'Reconnecting...' : 'Connect Broker'}
          </button>
          <button className="btn btn-secondary" onClick={handleConnectAction} disabled={loading || reconnecting} style={{ fontSize: 13 }}>
            <Plug size={14} />
            Connection Page
          </button>
        </div>
      </div>
    </div>
  );
}
