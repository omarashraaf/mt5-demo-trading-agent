import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Cpu, Play, FlaskConical, ShieldCheck, LineChart } from 'lucide-react';
import { api } from '../utils/api';
import type { ResearchModelVersion, ResearchReportRecord, ResearchStatusResponse } from '../types';

export default function ResearchPage() {
  const [status, setStatus] = useState<ResearchStatusResponse | null>(null);
  const [models, setModels] = useState<ResearchModelVersion[]>([]);
  const [reports, setReports] = useState<ResearchReportRecord[]>([]);
  const [busy, setBusy] = useState<string>('');
  const [message, setMessage] = useState<string>('');
  const [error, setError] = useState<string>('');

  const refresh = useCallback(async () => {
    try {
      const [s, m, r] = await Promise.all([
        api.getResearchStatus(),
        api.listResearchModels(50),
        api.listAttributionReports(10),
      ]);
      setStatus(s);
      setModels(m.models || []);
      setReports(r.reports || []);
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

  const candidate = status?.best_candidate_model || models.find((m) => m.approval_status === 'candidate') || null;
  const candidateAccuracy = Number((candidate?.evaluation_metrics_json || {}).accuracy || 0);
  const topIndicators = ((candidate?.evaluation_metrics_json || {}).feature_importance as Array<{ feature: string; importance: number }> | undefined) || [];

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
          <div className="text-muted" style={{ fontSize: 11 }}>Meta-Model Active</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: status?.meta_model_active ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {status?.meta_model_active ? 'ON' : 'OFF'}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {status?.meta_model_active_version || 'No approved model loaded'}
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
            {status?.last_training_run?.run_id || 'No runs yet'}
          </div>
        </div>
      </div>

      <div className="card mb-4" style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled}
          onClick={() => runAction('dataset', () => api.rebuildResearchDataset({ output_name: 'trade_dataset', include_unexecuted: true }))}
        >
          <FlaskConical size={14} /> Rebuild Dataset
        </button>
        <button
          className="btn btn-primary"
          disabled={!!busy || !status?.enabled}
          onClick={() =>
            runAction('train', () =>
              api.trainResearchModel({
                algorithm: 'logistic_regression',
                target_column: 'profitable_after_costs_90m',
                include_unexecuted: true,
                min_rows: status?.config?.MIN_TRADES_BEFORE_TRAINING || 30,
              }),
            )
          }
        >
          <Cpu size={14} /> Train Candidate
        </button>
        <button
          className="btn btn-secondary"
          disabled={!!busy || !status?.enabled || !candidate?.version_id}
          onClick={() =>
            candidate?.version_id &&
            runAction('replay', () =>
              api.runResearchReplay({ version_id: candidate.version_id, score_threshold: 0.55, include_unexecuted: true }),
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
                include_unexecuted: true,
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
    </div>
  );
}
