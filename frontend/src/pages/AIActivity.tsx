import { useCallback, useEffect, useState } from 'react';
import { Activity, CheckCircle, XCircle, Zap } from 'lucide-react';
import { api } from '../utils/api';
import type { AIActivity, StatusResponse } from '../types';
import { getSymbolEmoji, getSymbolName } from '../utils/symbolNames';

interface Props {
  status: StatusResponse | null;
}

export default function AIActivityPage({ status }: Props) {
  const [autoTradeLogs, setAutoTradeLogs] = useState<Array<{
    timestamp: number;
    symbol: string;
    action: string;
    confidence: number;
    quality_score?: number;
    detail: string;
    success: boolean;
    signal_id?: number | null;
    decision_reason?: string;
    gemini_summary?: string;
    meta_model_summary?: string;
  }>>([]);
  const [aiActivity, setAiActivity] = useState<AIActivity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!status?.connected) return;
    setLoading(true);
    setError('');
    try {
      const [auto, brain] = await Promise.all([
        api.getAutoTradeStatus(),
        api.getAIActivity(120),
      ]);
      setAutoTradeLogs(auto.recent_trades || []);
      const merged = [...(brain.live_activity || []), ...(brain.db_activity || [])]
        .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
        .slice(0, 140);
      setAiActivity(merged);
    } catch (e: any) {
      setError(e.message || 'Failed to load AI activity.');
    } finally {
      setLoading(false);
    }
  }, [status?.connected]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  if (!status?.connected) {
    return (
      <div>
        <div className="page-header">
          <h2>AI Brain Activity</h2>
          <p>Connect to MT5 to view full AI activity and auto-trade decisions.</p>
        </div>
        <div className="empty-state">Not connected</div>
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h2>AI Brain Activity</h2>
        <p>Live stream of AI decisions and reasons. Logs are shown in full with no cropping.</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="card-header">
          <h3>Recent Auto-Trades</h3>
          <span className="badge badge-blue">{autoTradeLogs.length} entries</span>
        </div>
        {loading && autoTradeLogs.length === 0 ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : autoTradeLogs.length === 0 ? (
          <div className="empty-state">No recent auto-trade decisions.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 320, overflowY: 'auto' }}>
            {autoTradeLogs.slice().reverse().map((log, i) => (
              <div key={`${log.timestamp}-${i}`} style={{ borderBottom: '1px solid var(--border)', paddingBottom: 8 }}>
                <div className="flex items-center gap-2" style={{ fontSize: 12 }}>
                  {log.success ? (
                    <CheckCircle size={12} style={{ color: 'var(--accent-green)', flexShrink: 0 }} />
                  ) : (
                    <XCircle size={12} style={{ color: 'var(--accent-red)', flexShrink: 0 }} />
                  )}
                  <span style={{ fontWeight: 600 }}>
                    {getSymbolEmoji(log.symbol)} {log.action} {getSymbolName(log.symbol)}
                  </span>
                  <span className="text-muted">({Math.round(log.confidence * 100)}%)</span>
                  {log.quality_score != null && (
                    <span className="text-muted">Q {Math.round(log.quality_score * 100)}%</span>
                  )}
                  <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11 }}>
                    {new Date(log.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
                <div style={{ marginTop: 4, color: log.success ? 'var(--accent-green)' : 'var(--accent-red)', fontSize: 12, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                  {log.detail}
                </div>
                {(log.decision_reason || log.gemini_summary || log.meta_model_summary) && (
                  <div style={{ marginTop: 6, display: 'grid', gap: 4 }}>
                    {log.decision_reason && (
                      <div className="text-muted" style={{ fontSize: 11, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Decision: {log.decision_reason}
                      </div>
                    )}
                    {log.gemini_summary && (
                      <div style={{ fontSize: 11, color: 'var(--accent-blue)', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Gemini: {log.gemini_summary}
                      </div>
                    )}
                    {log.meta_model_summary && (
                      <div style={{ fontSize: 11, color: 'var(--text-secondary)', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Meta model: {log.meta_model_summary}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <h3><Activity size={14} style={{ display: 'inline', marginRight: 6 }} />Live Brain Events</h3>
          <span className="badge badge-blue">{aiActivity.length} events</span>
        </div>
        {loading && aiActivity.length === 0 ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : aiActivity.length === 0 ? (
          <div className="empty-state">No AI brain events yet.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 420, overflowY: 'auto' }}>
            {aiActivity.map((activity, i) => (
              <div key={`${activity.timestamp}-${i}`} style={{ borderBottom: '1px solid var(--border)', paddingBottom: 10 }}>
                <div className="flex items-center gap-2" style={{ fontSize: 12 }}>
                  <Zap size={12} style={{ color: 'var(--accent-blue)', flexShrink: 0 }} />
                  <span style={{ fontWeight: 600 }}>
                    {getSymbolEmoji(activity.symbol)} {getSymbolName(activity.symbol)}
                  </span>
                  <span className="badge badge-blue" style={{ fontSize: 10 }}>{activity.action}</span>
                  <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11 }}>
                    {new Date(activity.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
                <div style={{ marginTop: 4, color: 'var(--text-secondary)', fontSize: 12, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                  {activity.detail || '-'}
                </div>
                {(activity.decision_reason || activity.gemini_summary || activity.meta_model_summary || activity.profit_pct != null) && (
                  <div style={{ marginTop: 6, display: 'grid', gap: 4 }}>
                    {activity.decision_reason && (
                      <div className="text-muted" style={{ fontSize: 11, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Decision: {activity.decision_reason}
                      </div>
                    )}
                    {activity.gemini_summary && (
                      <div style={{ fontSize: 11, color: 'var(--accent-blue)', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Gemini: {activity.gemini_summary}
                      </div>
                    )}
                    {activity.meta_model_summary && (
                      <div className="text-muted" style={{ fontSize: 11, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                        Meta model: {activity.meta_model_summary}
                      </div>
                    )}
                    {activity.profit_pct != null && (
                      <div style={{ fontSize: 11, color: (activity.profit_pct || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                        P/L %: {activity.profit_pct >= 0 ? '+' : ''}{activity.profit_pct.toFixed(2)}%
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
