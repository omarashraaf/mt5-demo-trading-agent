import { useCallback, useEffect, useState } from 'react';
import { RefreshCcw, Newspaper, Link as LinkIcon } from 'lucide-react';
import { api } from '../utils/api';
import type { EventAssetMappingRecord, ExternalEventRecord, GeminiEventAssessmentRecord, ProviderHealth } from '../types';

function formatTs(ts?: number): string {
  if (!ts || ts <= 0) return '-';
  return new Date(ts * 1000).toLocaleString();
}

function parseJsonField(raw?: string): unknown {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

export default function EventsPage() {
  const UI_REFRESH_MS = 5000;
  const INGEST_REFRESH_MS = 30000;
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [activePlatform, setActivePlatform] = useState<'mt5' | 'ibkr'>('mt5');
  const [activeTab, setActiveTab] = useState<'all' | 'ibkr'>('all');
  const [ibkrSymbols, setIbkrSymbols] = useState<string[]>([]);
  const [events, setEvents] = useState<ExternalEventRecord[]>([]);
  const [mappings, setMappings] = useState<EventAssetMappingRecord[]>([]);
  const [assessments, setAssessments] = useState<GeminiEventAssessmentRecord[]>([]);
  const [refreshSummary, setRefreshSummary] = useState<string>('');
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [lastLiveSyncAt, setLastLiveSyncAt] = useState<number | null>(null);
  const [liveBusy, setLiveBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [status, h, e, m, a] = await Promise.all([
        api.getStatus(),
        api.getFinnhubHealth(),
        api.getLatestEvents(25),
        api.getEventMappings(50),
        api.getGeminiEventAssessments(25),
      ]);
      setActivePlatform((status.platform || 'mt5') as 'mt5' | 'ibkr');
      setHealth(h.provider);
      setEvents(e.events || []);
      setMappings(m.mappings || []);
      setAssessments(a.assessments || []);
      const universe = ((h as unknown as { universe?: Record<string, unknown> }).universe || {}) as Record<string, unknown>;
      const fallbackRaw = universe['auto_trade_fallback_symbols'];
      const enabledRaw = universe['enabled_symbols'];
      const fallback = Array.isArray(fallbackRaw)
        ? fallbackRaw
        : [];
      const enabled = Array.isArray(enabledRaw) ? enabledRaw : [];
      const symbols = (enabled.length > 0 ? enabled : fallback)
        .map((s) => String(s || '').toUpperCase())
        .filter(Boolean);
      setIbkrSymbols(symbols);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load Finnhub data.';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, UI_REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const runLiveIngest = useCallback(async () => {
    if (!liveEnabled || liveBusy) return;
    try {
      setLiveBusy(true);
      setError(null);
      await api.refreshEvents({ news_category: 'general', classify_with_gemini: false });
      setLastLiveSyncAt(Date.now());
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Live refresh failed.';
      setError(message);
    } finally {
      setLiveBusy(false);
    }
  }, [liveBusy, liveEnabled, load]);

  useEffect(() => {
    if (!liveEnabled) return;
    void runLiveIngest();
    const t = setInterval(() => {
      void runLiveIngest();
    }, INGEST_REFRESH_MS);
    return () => clearInterval(t);
  }, [liveEnabled, runLiveIngest]);

  useEffect(() => {
    if (activePlatform !== 'ibkr' && activeTab === 'ibkr') {
      setActiveTab('all');
    }
  }, [activePlatform, activeTab]);

  const onRefreshNow = async () => {
    try {
      setRefreshing(true);
      setError(null);
      const out = await api.refreshEvents({ news_category: 'general', classify_with_gemini: false });
      setRefreshSummary(
        `Fetched ${out.raw_item_count} raw items, stored ${out.stored_event_count} events, mapped ${out.mapped_assets_count} assets.`,
      );
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Refresh failed.';
      setError(message);
    } finally {
      setRefreshing(false);
    }
  };

  const ibkrSymbolSet = new Set(ibkrSymbols.map((s) => s.toUpperCase()));
  const ibkrEventIds = new Set(
    mappings
      .filter((m) => ibkrSymbolSet.has(String(m.symbol || '').toUpperCase()))
      .map((m) => m.external_event_id),
  );
  const filteredEvents = activeTab === 'ibkr'
    ? events.filter((ev) => {
      const direct = (ev.affected_assets || []).some((a) => ibkrSymbolSet.has(String(a || '').toUpperCase()));
      return direct || ibkrEventIds.has(ev.id);
    })
    : events;
  const filteredMappings = activeTab === 'ibkr'
    ? mappings.filter((m) => ibkrSymbolSet.has(String(m.symbol || '').toUpperCase()))
    : mappings;
  const filteredAssessments = activeTab === 'ibkr'
    ? assessments.filter((a) => ibkrEventIds.has(a.external_event_id))
    : assessments;

  const showIbkrTab = activePlatform === 'ibkr';
  const statusLabel = health?.partial_news_only ? 'News Only' : health?.degraded ? 'Degraded' : 'Healthy';
  const statusBadgeClass = health?.partial_news_only
    ? 'badge-blue'
    : health?.degraded
      ? 'badge-yellow'
      : 'badge-green';

  return (
    <div className="card">
      <div className="flex justify-between items-center mb-4">
        <div>
          <h2 className="text-white text-lg font-semibold flex items-center gap-2">
            <Newspaper size={18} />
            Finnhub News Feed
          </h2>
          <p className="text-muted" style={{ marginTop: 4 }}>
            Live external events, mapped assets, and Gemini event assessments.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className={`btn btn-sm ${liveEnabled ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setLiveEnabled((prev) => !prev)}
            disabled={refreshing}
            title="Enable/disable live auto-refresh"
          >
            {liveEnabled ? 'Live On' : 'Live Off'}
          </button>
          <button className="btn btn-secondary" onClick={onRefreshNow} disabled={refreshing}>
            <RefreshCcw size={14} />
            {refreshing ? 'Refreshing...' : 'Refresh Now'}
          </button>
        </div>
      </div>

      {health && (
        <div className="card mb-4" style={{ background: 'rgba(255,255,255,0.02)' }}>
          <div className="flex items-center gap-3 mb-2">
            <span className={`badge ${statusBadgeClass}`}>
              {statusLabel}
            </span>
            <span className="text-muted">Provider: {health.provider}</span>
            <span className="text-muted">Market News: {health.capabilities?.market_news ? 'On' : 'Off'}</span>
            <span className="text-muted">Calendar: {health.capabilities?.economic_calendar ? 'On' : 'Off'}</span>
          </div>
          {health.reason && (
            <div className="text-sm text-warning" style={{ whiteSpace: 'pre-wrap' }}>
              {health.reason}
            </div>
          )}
          {refreshSummary && <div className="text-sm text-muted mt-2">{refreshSummary}</div>}
          <div className="text-sm text-muted mt-2">
            Auto refresh: {liveEnabled ? `On (every ${Math.round(INGEST_REFRESH_MS / 1000)}s)` : 'Off'}
            {lastLiveSyncAt ? ` • Last sync: ${new Date(lastLiveSyncAt).toLocaleTimeString()}` : ''}
            {liveBusy ? ' • syncing...' : ''}
          </div>
        </div>
      )}

      {error && <div className="alert alert-error mb-4">{error}</div>}
      {loading ? (
        <div className="empty-state">Loading events...</div>
      ) : (
        <div className="grid gap-4" style={{ gridTemplateColumns: '2fr 1fr' }}>
          <div className="card" style={{ background: 'rgba(255,255,255,0.02)' }}>
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-white">Latest Events ({filteredEvents.length})</h3>
              <div className="flex items-center gap-2">
                <button
                  className={`btn btn-sm ${activeTab === 'all' ? 'btn-primary' : 'btn-secondary'}`}
                  onClick={() => setActiveTab('all')}
                >
                  All
                </button>
                {showIbkrTab && (
                  <button
                    className={`btn btn-sm ${activeTab === 'ibkr' ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => setActiveTab('ibkr')}
                  >
                    IBKR
                  </button>
                )}
              </div>
            </div>
            <div style={{ maxHeight: 560, overflowY: 'auto' }}>
              {filteredEvents.length === 0 ? (
                <div className="empty-state">No events yet.</div>
              ) : (
                filteredEvents.map((ev) => (
                  <div
                    key={ev.id}
                    style={{ padding: '10px 0', borderBottom: '1px solid rgba(255,255,255,0.07)' }}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="badge badge-blue">{ev.category || ev.event_type}</span>
                      <span className="text-muted text-xs">{formatTs(ev.timestamp_utc)}</span>
                    </div>
                    <div className="text-white" style={{ fontSize: 14, fontWeight: 600 }}>{ev.title}</div>
                    {ev.summary && <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>{ev.summary}</div>}
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="grid gap-4">
            <div className="card" style={{ background: 'rgba(255,255,255,0.02)' }}>
              <h3 className="text-white mb-3">Asset Mappings ({filteredMappings.length})</h3>
              <div style={{ maxHeight: 260, overflowY: 'auto' }}>
                {filteredMappings.length === 0 ? (
                  <div className="empty-state">No mappings.</div>
                ) : (
                  filteredMappings.map((m) => (
                    <div key={m.id} style={{ padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
                      <div className="flex items-center justify-between">
                        <strong>{m.symbol}</strong>
                        <span className="badge badge-blue">{m.baseline_bias}</span>
                      </div>
                      <div className="text-muted text-xs">
                        Score: {m.mapping_score.toFixed(2)} • {m.reason}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="card" style={{ background: 'rgba(255,255,255,0.02)' }}>
              <h3 className="text-white mb-3">Gemini Event Assessments ({filteredAssessments.length})</h3>
              <div style={{ maxHeight: 260, overflowY: 'auto' }}>
                {filteredAssessments.length === 0 ? (
                  <div className="empty-state">No Gemini assessments.</div>
                ) : (
                  filteredAssessments.map((a) => {
                    const assets = parseJsonField(a.affected_assets_json);
                    return (
                      <div key={a.id} style={{ padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
                        <div className="flex items-center justify-between">
                          <span className="badge badge-blue">{a.event_type || 'event'}</span>
                          <span className={`badge ${a.degraded ? 'badge-yellow' : 'badge-green'}`}>
                            {a.degraded ? 'Degraded' : 'OK'}
                          </span>
                        </div>
                        <div className="text-muted text-xs" style={{ marginTop: 4 }}>
                          Risk: {a.event_risk} • Importance: {a.importance}
                        </div>
                        <div className="text-muted text-xs">
                          Assets: {Array.isArray(assets) ? assets.join(', ') : '-'}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="text-muted mt-4" style={{ fontSize: 12 }}>
        <LinkIcon size={12} style={{ display: 'inline', marginRight: 6 }} />
        Source endpoints: <code>/events/finnhub/health</code>, <code>/events/refresh</code>, <code>/events/latest</code>, <code>/events/mappings</code>, <code>/events/gemini-assessments</code>
      </div>
    </div>
  );
}
