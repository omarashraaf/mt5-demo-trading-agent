import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Cpu, Play, FlaskConical, ShieldCheck, LineChart } from 'lucide-react';
import { api } from '../utils/api';
import type { ResearchModelVersion, ResearchReportRecord, ResearchStatusResponse } from '../types';

type PerformanceBucket = {
  bucket?: string;
  trades?: number;
  wins?: number;
  losses?: number;
  net_pnl?: number;
  avg_pnl?: number;
  win_rate?: number;
};

function performanceRows(report: Record<string, unknown> | null, key: string): PerformanceBucket[] {
  const value = report?.[key];
  if (!value || typeof value !== 'object') return [];
  return Object.values(value as Record<string, PerformanceBucket>)
    .filter((row) => row && typeof row === 'object')
    .slice(0, 6);
}

function formatPnl(value?: number) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}`;
}

export default function ResearchPage() {
  const [status, setStatus] = useState<ResearchStatusResponse | null>(null);
  const [models, setModels] = useState<ResearchModelVersion[]>([]);
  const [reports, setReports] = useState<ResearchReportRecord[]>([]);
  const [performance, setPerformance] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState<string>('');
  const [message, setMessage] = useState<string>('');
  const [error, setError] = useState<string>('');

  const refresh = useCallback(async () => {
    try {
      const [s, m, r, p] = await Promise.all([
        api.getResearchStatus(),
        api.listResearchModels(50),
        api.listAttributionReports(10),
        api.getPerformanceAttribution(2000),
      ]);
      setStatus(s);
      setModels(m.models || []);
      setReports(r.reports || []);
      setPerformance(p);
      setError('');
    } catch (e: any) {
      setError(e.message || 'Failed to load research status');
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 8000);
    return () => clearInterval(timer);
  }, [refresh]);

  const approvedModel = useMemo(
    () => models.find((m) => m.approval_status === 'approved') || status?.active_approved_model || null,
    [models, status],
  );

  const rankedCandidate = useMemo(() => {
    const candidates = models.filter((m) => m.approval_status === 'candidate');
    if (status?.best_candidate_model) return status.best_candidate_model;
    return candidates.sort((a, b) => {
      const aMetrics = a.evaluation_metrics_json || {};
      const bMetrics = b.evaluation_metrics_json || {};
      const aRank = [
        Number(aMetrics.f1 || 0),
        Number(aMetrics.precision || 0),
        Number(aMetrics.recall || 0),
        Number(aMetrics.dataset_row_count || aMetrics.sample_count || 0),
      ];
      const bRank = [
        Number(bMetrics.f1 || 0),
        Number(bMetrics.precision || 0),
        Number(bMetrics.recall || 0),
        Number(bMetrics.dataset_row_count || bMetrics.sample_count || 0),
      ];
      for (let i = 0; i < aRank.length; i += 1) {
        if (bRank[i] !== aRank[i]) return bRank[i] - aRank[i];
      }
      return 0;
    })[0] || null;
  }, [models, status?.best_candidate_model]);

  async function runAction(actionKey: string, action: () => Promise<any>) {
    setBusy(actionKey);
    setMessage('');
    setError('');
    try {
      const result = await action();
      setMessage(typeof result === 'string' ? result : 'Done');
      await refresh();
    } catch (e: any) {
      setError(e.message || 'Action failed');
    } finally {
      setBusy('');
    }
  }

  const candidate = rankedCandidate;
  const candidateAccuracy = Number((candidate?.evaluation_metrics_json || {}).accuracy || 0);
  const modelQuality = status?.model_quality;
  const modelQualityIssue = modelQuality?.blockers?.[0] || modelQuality?.warnings?.[0] || '';
  const topIndicators = ((candidate?.evaluation_metrics_json || {}).feature_importance as Array<{ feature: string; importance: number }> | undefined) || [];
  const reportSections = [
    ['By Symbol', 'by_symbol'],
    ['By Strategy', 'by_strategy'],
    ['By Confidence', 'by_confidence_bucket'],
    ['By Session', 'by_session'],
    ['By Risk Profile', 'by_risk_profile'],
    ['News/Gemini Impact', 'news_gemini_impact'],
  ] as const;

  return (
    <div>
      <div className="flex justify-between items-center mb-4">
        <div>
          <h2 style={{ fontSize: 24, fontWeight: 700 }}>Research Cycle</h2>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            Offline training, replay, walk-forward, and approval. Live trading still stays behind risk checks.
          </div>
        </div>
        <button className="btn btn-secondary" onClick={refresh} disabled={!!busy}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && <div className="error-banner mb-3">{error}</div>}
      {message && <div className="success-banner mb-3">{message}</div>}

      <div className="grid-3 gap-3 mb-4">
        <div className="card">
          <div className="text-muted" style={{ fontSize: 11 }}>AI Brain Quality</div>
          <div
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: modelQuality?.status === 'ready'
                ? 'var(--accent-green)'
                : modelQuality?.status === 'warning'
                  ? 'var(--accent-orange)'
                  : 'var(--accent-red)',
            }}
          >
            {typeof modelQuality?.score === 'number' ? `${modelQuality.score.toFixed(1)}/10` : '-'}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {modelQualityIssue || status?.meta_model_active_version || 'No approved model loaded'}
          </div>
        </div>

        <div className="card">
          <div className="text-muted" style={{ fontSize: 11 }}>Best Candidate</div>
          <div style={{ fontSize: 16, fontWeight: 700 }}>{candidate?.version_id || '-'}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Accuracy: {(candidateAccuracy * 100).toFixed(1)}%
          </div>
        </div>

        <div className="card">
          <div className="text-muted" style={{ fontSize: 11 }}>Last Training Run</div>
          <div style={{ fontSize: 16, fontWeight: 700 }}>{status?.last_training_run?.status || '-'}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Clean closed: {status?.incremental_training?.clean_app_closed_count ?? 0}
          </div>
        </div>
      </div>

      <div className="card mb-4" style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled}
          onClick={() => runAction('dataset', () => api.rebuildResearchDataset({ output_name: 'trade_dataset', include_unexecuted: false }))}
        >
          <FlaskConical size={14} /> Rebuild Executed Dataset
        </button>
        <button
          className="btn btn-primary"
          disabled={!!busy || !status?.enabled}
          onClick={() =>
            runAction('train', () =>
              api.trainResearchModel({
                algorithm: 'empirical_rules',
                target_column: 'profitable_after_costs_90m',
                include_unexecuted: false,
                min_rows: status?.config?.MIN_TRADES_BEFORE_TRAINING || 30,
              }),
            )
          }
        >
          <Cpu size={14} /> Train Empirical Model
        </button>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled || !candidate?.version_id}
          onClick={() =>
            candidate?.version_id &&
            runAction('replay', () =>
              api.runResearchReplay({ version_id: candidate.version_id, score_threshold: 0.55, include_unexecuted: false }),
            )
          }
        >
          <Play size={14} /> Run Replay
        </button>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled}
          onClick={() =>
            runAction('walkfwd', () =>
              api.runResearchWalkForward({
                algorithm: 'logistic_regression',
                target_column: 'profitable_after_costs_90m',
                windows: status?.config?.WALK_FORWARD_WINDOWS || 5,
                include_unexecuted: false,
              }),
            )
          }
        >
          <LineChart size={14} /> Walk Forward
        </button>
        <button
          className="btn btn-success"
          disabled={!!busy || !status?.enabled || !candidate?.version_id}
          onClick={() =>
            candidate?.version_id &&
            runAction('approve', async () => {
              const res = await api.approveResearchModel(candidate.version_id);
              return `Approved ${candidate.version_id}. Active: ${(res as any)?.activation?.active_version_id || '-'}`;
            })
          }
        >
          <ShieldCheck size={14} /> Approve Candidate
        </button>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled}
          onClick={() => runAction('report', () => api.generateAttributionReport({ report_type: 'full', limit: 5000 }))}
        >
          <FlaskConical size={14} /> Generate Attribution
        </button>
      </div>

      <div className="grid-2 gap-3">
        <div className="card">
          <h4 style={{ marginBottom: 10 }}>Top Indicators</h4>
          {topIndicators.length === 0 && <div className="text-muted" style={{ fontSize: 12 }}>No feature importance available yet.</div>}
          {topIndicators.slice(0, 8).map((item, idx) => (
            <div key={`${item.feature}-${idx}`} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: 'var(--text-secondary)' }}>{item.feature}</span>
              <span className="font-mono">{(item.importance * 100).toFixed(2)}</span>
            </div>
          ))}
        </div>

        <div className="card">
          <h4 style={{ marginBottom: 10 }}>Recent Reports</h4>
          {reports.length === 0 && <div className="text-muted" style={{ fontSize: 12 }}>No attribution reports generated yet.</div>}
          {reports.slice(0, 6).map((report) => (
            <div key={report.report_id} style={{ marginBottom: 8, fontSize: 12 }}>
              <div style={{ fontWeight: 600 }}>{report.report_type}</div>
              <div className="text-muted">{new Date((report.generated_at || 0) * 1000).toLocaleString()}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="card mt-4">
        <h4 style={{ marginBottom: 10 }}>Performance Attribution</h4>
        <div className="text-muted" style={{ fontSize: 12, marginBottom: 12 }}>
          Sample size: {Number(performance?.sample_size || 0)}
        </div>
        <div className="grid-3 gap-3">
          {reportSections.map(([label, key]) => {
            const rows = performanceRows(performance, key);
            return (
              <div key={key} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 10 }}>
                <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>{label}</div>
                {rows.length === 0 && <div className="text-muted" style={{ fontSize: 12 }}>No data yet.</div>}
                {rows.map((row) => (
                  <div key={`${key}-${row.bucket}`} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8, fontSize: 12, marginBottom: 6 }}>
                    <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {row.bucket || 'unknown'} ({row.trades || 0})
                    </span>
                    <span className="font-mono" style={{ color: Number(row.net_pnl || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                      {formatPnl(row.net_pnl)}
                    </span>
                    <span className="text-muted">Win rate</span>
                    <span className="font-mono">{Math.round(Number(row.win_rate || 0) * 100)}%</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
