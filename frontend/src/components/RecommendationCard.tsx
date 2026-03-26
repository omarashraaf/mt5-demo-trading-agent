import { useState } from 'react';
import { TrendingUp, TrendingDown, Minus, Play, X } from 'lucide-react';
import { api } from '../utils/api';
import type { Recommendation, OrderResult } from '../types';

interface Props {
  rec: Recommendation;
  onExecuted: () => void;
}

export default function RecommendationCard({ rec, onExecuted }: Props) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [dismissed, setDismissed] = useState(false);

  if (dismissed) return null;

  const handleExecute = async () => {
    if (!rec.signal_id) return;
    setLoading(true);
    try {
      const res = await api.executeRecommendation(rec.signal_id);
      setResult(res);
      onExecuted();
    } catch (e: any) {
      setResult({
        success: false, retcode: -1, retcode_desc: e.message,
        ticket: null, volume: null, price: null,
        stop_loss: null, take_profit: null, comment: '',
      });
    } finally {
      setLoading(false);
    }
  };

  const isBuy = rec.signal.action === 'BUY';
  const isSell = rec.signal.action === 'SELL';
  const isHold = rec.signal.action === 'HOLD';
  const confPct = Math.round(rec.signal.confidence * 100);
  const confColor = confPct >= 70 ? 'var(--accent-green)' : confPct >= 55 ? 'var(--accent-yellow)' : 'var(--text-muted)';

  const digits = rec.entry_price_estimate < 50 ? 5 : 2;

  return (
    <div className="card" style={{
      borderLeft: `3px solid ${isBuy ? 'var(--accent-green)' : isSell ? 'var(--accent-red)' : 'var(--border)'}`,
    }}>
      <div className="flex justify-between items-center mb-2">
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 18, fontWeight: 700 }}>{rec.symbol}</span>
          <span className={`badge ${isBuy ? 'badge-green' : isSell ? 'badge-red' : 'badge-yellow'}`}
                style={{ fontSize: 13, padding: '3px 12px' }}>
            {isBuy && <TrendingUp size={13} style={{ marginRight: 4 }} />}
            {isSell && <TrendingDown size={13} style={{ marginRight: 4 }} />}
            {isHold && <Minus size={13} style={{ marginRight: 4 }} />}
            {rec.signal.action}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <div style={{ width: 80, height: 6, background: 'var(--bg-tertiary)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${confPct}%`, height: '100%', background: confColor, borderRadius: 3 }} />
          </div>
          <span style={{ fontSize: 12, fontWeight: 600, color: confColor }}>{confPct}%</span>
        </div>
      </div>

      {!isHold && (
        <div className="grid-3 gap-3 mb-4" style={{ fontSize: 12 }}>
          <div>
            <span className="text-muted">Entry </span>
            <span className="font-mono">{rec.entry_price_estimate.toFixed(digits)}</span>
          </div>
          <div>
            <span className="text-muted">Stop Loss </span>
            <span className="font-mono text-red">{rec.signal.stop_loss?.toFixed(digits) ?? '-'}</span>
          </div>
          <div>
            <span className="text-muted">Take Profit </span>
            <span className="font-mono text-green">{rec.signal.take_profit?.toFixed(digits) ?? '-'}</span>
          </div>
        </div>
      )}

      <p style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.5, marginBottom: 12 }}>
        {rec.explanation}
      </p>

      {!rec.risk_decision.approved && !isHold && (
        <div className="text-sm text-red mb-2">
          Risk check: {rec.risk_decision.reason}
        </div>
      )}

      {result ? (
        <div className={`text-sm ${result.success ? 'text-green' : 'text-red'}`} style={{ fontWeight: 500 }}>
          {result.success
            ? `Order filled! Ticket #${result.ticket} at ${result.price?.toFixed(digits)}`
            : `Failed: ${result.retcode_desc}`
          }
        </div>
      ) : (
        <div className="flex gap-2">
          {rec.ready_to_execute && (
            <button
              className={`btn ${isBuy ? 'btn-success' : 'btn-danger'}`}
              onClick={handleExecute}
              disabled={loading}
              style={{ fontSize: 13 }}
            >
              {loading ? <span className="loading-spinner" /> : <Play size={14} />}
              Execute {rec.signal.action}
            </button>
          )}
          <button className="btn btn-secondary btn-sm" onClick={() => setDismissed(true)}>
            <X size={12} /> Skip
          </button>
        </div>
      )}
    </div>
  );
}
