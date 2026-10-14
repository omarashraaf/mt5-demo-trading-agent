import { useState, useEffect, useCallback, useRef } from 'react';
import { Play, Plus, HelpCircle, Pencil, Trash2 } from 'lucide-react';
import { api } from '../utils/api';
import { FALLBACK_SYMBOL_LIST, ACTIVE_SYMBOL_MODE_LABEL } from '../utils/symbolUniverse';
import type { AgentInfo, CompiledStrategy, EvaluateResponse, StrategyProfile } from '../types';

interface Props {
  connected: boolean;
}

export default function Strategy({ connected }: Props) {
  const [agents, setAgents] = useState<Record<string, AgentInfo>>({});
  const [activeAgent, setActiveAgent] = useState('');
  const [strategies, setStrategies] = useState<StrategyProfile[]>([]);
  const [activeStrategyId, setActiveStrategyId] = useState('');
  const [strategyName, setStrategyName] = useState('');
  const [strategyDescription, setStrategyDescription] = useState('');
  const [applyUnderAnyConditions, setApplyUnderAnyConditions] = useState(false);
  const [strategyLoadingId, setStrategyLoadingId] = useState('');
  const [strategyInfoOpen, setStrategyInfoOpen] = useState(false);
  const [strategyInfo, setStrategyInfo] = useState<StrategyProfile | null>(null);
  const [strategyPreview, setStrategyPreview] = useState<StrategyProfile | null>(null);
  const [strategyPreviewKey, setStrategyPreviewKey] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [buildingStrategy, setBuildingStrategy] = useState(false);
  const [editingStrategy, setEditingStrategy] = useState<StrategyProfile | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editApplyUnderAnyConditions, setEditApplyUnderAnyConditions] = useState(false);
  const [editStrategyPreview, setEditStrategyPreview] = useState<StrategyProfile | null>(null);
  const [editStrategyPreviewKey, setEditStrategyPreviewKey] = useState('');
  const [editPreviewLoading, setEditPreviewLoading] = useState(false);
  const [editPreviewError, setEditPreviewError] = useState('');
  const [savingStrategy, setSavingStrategy] = useState(false);
  const [deletingStrategyId, setDeletingStrategyId] = useState('');
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(FALLBACK_SYMBOL_LIST);
  const [symbol, setSymbol] = useState(FALLBACK_SYMBOL_LIST[0]);
  const [timeframe, setTimeframe] = useState('H1');
  const [barCount, setBarCount] = useState(100);
  const [result, setResult] = useState<EvaluateResponse | null>(null);
  const [history, setHistory] = useState<EvaluateResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const initialLoadDoneRef = useRef(false);

  const loadStrategies = useCallback(async () => {
    try {
      const response = await api.getStrategies();
      setStrategies(response.items || []);
      setActiveStrategyId(response.active_strategy_id || '');
    } catch {
      // keep page usable if strategies endpoint is temporarily unavailable
    }
  }, []);

  useEffect(() => {
    if (initialLoadDoneRef.current) return;
    initialLoadDoneRef.current = true;
    void Promise.allSettled([
      api.getAgents().then(setAgents),
      api.getStatus().then((s) => {
        setActiveAgent(s.active_agent);
        setActiveStrategyId(s.active_strategy?.id || '');
      }),
      loadStrategies(),
    ]);
  }, [loadStrategies]);

  useEffect(() => {
    if (!connected) return;
    api.getAvailableSymbols()
      .then((response) => {
        const symbols = Object.values(response.categories)
          .flat()
          .map((item) => item.name)
          .sort((a, b) => a.localeCompare(b));
        if (!symbols.length) return;
        setAvailableSymbols(symbols);
        setSymbol((prev) => (symbols.includes(prev) ? prev : symbols[0]));
      })
      .catch(() => {});
  }, [connected]);

  useEffect(() => {
    const name = strategyName.trim();
    const description = strategyDescription.trim();
    const requestKey = JSON.stringify([name, description, Boolean(applyUnderAnyConditions)]);
    if (name.length < 3 || description.length < 10) {
      setStrategyPreview(null);
      setStrategyPreviewKey('');
      setPreviewLoading(false);
      setPreviewError('');
      return;
    }
    let active = true;
    setPreviewLoading(true);
    setPreviewError('');
    const timer = window.setTimeout(async () => {
      try {
        const response = await api.previewStrategy({
          name,
          description,
          apply_under_any_conditions: applyUnderAnyConditions,
        });
        if (active) {
          setStrategyPreview(response.strategy || null);
          setStrategyPreviewKey(requestKey);
        }
      } catch (e: any) {
        if (active) {
          setPreviewError(String(e?.message || 'Preview temporarily unavailable.'));
        }
      } finally {
        if (active) setPreviewLoading(false);
      }
    }, 900);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [strategyName, strategyDescription, applyUnderAnyConditions]);

  useEffect(() => {
    if (!editingStrategy) {
      setEditStrategyPreview(null);
      setEditStrategyPreviewKey('');
      setEditPreviewLoading(false);
      setEditPreviewError('');
      return;
    }
    const name = editName.trim();
    const description = editDescription.trim();
    const requestKey = JSON.stringify([editingStrategy.id, name, description, Boolean(editApplyUnderAnyConditions)]);
    if (name.length < 3 || description.length < 10) {
      setEditStrategyPreview(null);
      setEditStrategyPreviewKey('');
      setEditPreviewLoading(false);
      setEditPreviewError('');
      return;
    }
    let active = true;
    setEditPreviewLoading(true);
    setEditPreviewError('');
    const timer = window.setTimeout(async () => {
      try {
        const response = await api.previewStrategy({
          name,
          description,
          apply_under_any_conditions: editApplyUnderAnyConditions,
        });
        if (active) {
          setEditStrategyPreview(response.strategy || null);
          setEditStrategyPreviewKey(requestKey);
        }
      } catch (e: any) {
        if (active) {
          setEditPreviewError(String(e?.message || 'Preview temporarily unavailable.'));
        }
      } finally {
        if (active) setEditPreviewLoading(false);
      }
    }, 900);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [editingStrategy, editName, editDescription, editApplyUnderAnyConditions]);

  const renderStrategyPreview = (
    preview: StrategyProfile | null,
    loadingState: boolean,
    previewErrorMessage: string | undefined,
    isCurrentPreview: boolean,
    forceModeDraft?: boolean,
  ) => {
    const compiled = ((preview?.config || {}) as { compiled_strategy?: CompiledStrategy }).compiled_strategy;
    const previewConfig = (preview?.config || {}) as Record<string, unknown>;
    const builderSource = String(previewConfig.strategy_builder_source || 'deterministic');
    const aiInterpretation = (previewConfig.ai_interpretation || {}) as Record<string, unknown>;
    const canShowCompiled = Boolean(compiled) && isCurrentPreview;
    if (!canShowCompiled && !loadingState && !previewErrorMessage) return null;
    return (
      <div className="card" style={{ marginTop: 14, borderStyle: 'dashed' }}>
        <div className="card-header">
          <h3>Strategy Preview</h3>
        </div>
        {loadingState ? (
          <div className="text-muted text-sm">Understanding your strategy...</div>
        ) : canShowCompiled ? (
          <>
            <div className="text-muted text-sm" style={{ marginBottom: 10 }}>
              Builder source: <strong style={{ color: 'var(--text-primary)' }}>{builderSource === 'gemini' ? 'Gemini + validator' : 'Deterministic fallback'}</strong>
            </div>
            <div className="grid-2 gap-3">
              <div className="text-sm">
                <div><strong>Symbols:</strong> {compiled.symbols.length ? compiled.symbols.join(', ') : 'All enabled markets'}</div>
                <div><strong>Market selection:</strong> {compiled.market_selection_mode || 'named_symbols'}</div>
                <div><strong>Entry:</strong> {compiled.entry_action}</div>
                <div><strong>Sizing:</strong> {compiled.fixed_lot_size != null ? `${compiled.fixed_lot_size} lots` : (compiled.allocation_pct_free_margin != null ? `${compiled.allocation_pct_free_margin}% of free margin` : 'Risk-sized')}</div>
                <div><strong>Buy every:</strong> {compiled.cadence_buy_every_seconds || 0}s</div>
              </div>
              <div className="text-sm">
                <div><strong>Sell after:</strong> {compiled.cadence_sell_after_seconds || 0}s</div>
                <div><strong>Force mode:</strong> {(typeof forceModeDraft === 'boolean' ? forceModeDraft : compiled.apply_under_any_conditions) ? 'ON' : 'OFF'}</div>
                <div><strong>Policy:</strong> {compiled.policy_mode}</div>
                <div><strong>Gemini:</strong> {compiled.gemini_role}</div>
              </div>
            </div>
            {compiled.human_summary ? (
              <div className="text-sm mt-3" style={{ lineHeight: 1.7 }}>
                <strong>What the app understood:</strong> {compiled.human_summary}
              </div>
            ) : null}
            {compiled.summary_lines?.length ? (
              <div className="text-muted text-sm mt-3" style={{ whiteSpace: 'pre-line', lineHeight: 1.6 }}>
                {compiled.summary_lines.join('\n')}
              </div>
            ) : null}
            {compiled.assumptions?.length ? (
              <div className="text-muted text-sm mt-3" style={{ lineHeight: 1.6 }}>
                <strong>Assumptions:</strong>
                <div style={{ whiteSpace: 'pre-line' }}>{compiled.assumptions.join('\n')}</div>
              </div>
            ) : null}
            {compiled.missing_details?.length ? (
              <div className="mt-3" style={{ color: '#f59e0b', fontSize: 13, lineHeight: 1.6 }}>
                <strong>Missing details:</strong>
                {compiled.missing_details.map((detail, idx) => (
                  <div key={`${detail}-${idx}`}>{detail}</div>
                ))}
              </div>
            ) : null}
            {compiled.warnings?.length ? (
              <div className="mt-3" style={{ color: '#f59e0b', fontSize: 13, lineHeight: 1.6 }}>
                {compiled.warnings.map((warning, idx) => (
                  <div key={`${warning}-${idx}`}>Warning: {warning}</div>
                ))}
              </div>
            ) : null}
            {String(aiInterpretation.notes || '').trim() ? (
              <div className="text-muted text-sm mt-3" style={{ lineHeight: 1.6 }}>
                Gemini notes: {String(aiInterpretation.notes)}
              </div>
            ) : null}
          </>
        ) : null}
        {!loadingState && !isCurrentPreview && !previewErrorMessage ? (
          <div className="text-muted text-sm mt-3">
            Waiting for preview for the current prompt...
          </div>
        ) : null}
        {!loadingState && previewErrorMessage ? (
          <div className="text-muted text-sm mt-3" style={{ color: 'var(--accent-yellow)' }}>
            Preview refresh issue: {previewErrorMessage}
          </div>
        ) : null}
      </div>
    );
  };

  const handleSetAgent = async (name: string) => {
    try {
      await api.setAgent(name);
      setActiveAgent(name);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleSelectStrategy = async (strategyId: string) => {
    if (!strategyId || strategyLoadingId) return;
    setStrategyLoadingId(strategyId);
    setError('');
    try {
      const response = await api.selectStrategy(strategyId);
      setActiveStrategyId(response.active_strategy_id || strategyId);
      setActiveAgent(response.active_agent || activeAgent);
      setStrategies((prev) => prev.map((item) => ({
        ...item,
        is_selected: item.id === (response.active_strategy_id || strategyId),
      })));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setStrategyLoadingId('');
    }
  };

  const handleBuildStrategy = async () => {
    const name = strategyName.trim();
    const description = strategyDescription.trim();
    if (name.length < 3) {
      setError('Strategy name must be at least 3 characters.');
      return;
    }
    if (description.length < 10) {
      setError('Please describe your strategy in more detail.');
      return;
    }
    setBuildingStrategy(true);
    setError('');
    try {
      const response = await api.buildStrategy({
        name,
        description,
        apply_under_any_conditions: applyUnderAnyConditions,
      });
      setActiveStrategyId(response.active_strategy_id);
      setActiveAgent(response.active_agent || activeAgent);
      setStrategyName('');
      setStrategyDescription('');
      setApplyUnderAnyConditions(false);
      await loadStrategies();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBuildingStrategy(false);
    }
  };

  const openEditStrategy = (strategy: StrategyProfile) => {
    const cfg = (strategy.config || {}) as Record<string, unknown>;
    setEditingStrategy(strategy);
    setEditName(strategy.name || '');
    setEditDescription(strategy.description || '');
    setEditApplyUnderAnyConditions(Boolean(cfg.apply_under_any_conditions));
  };

  const handleUpdateStrategy = async () => {
    if (!editingStrategy) return;
    const name = editName.trim();
    const description = editDescription.trim();
    if (name.length < 3) {
      setError('Strategy name must be at least 3 characters.');
      return;
    }
    if (description.length < 10) {
      setError('Please describe your strategy in more detail.');
      return;
    }
    setSavingStrategy(true);
    setError('');
    try {
      const response = await api.updateStrategy({
        strategy_id: editingStrategy.id,
        name,
        description,
        apply_under_any_conditions: editApplyUnderAnyConditions,
      });
      const nextActiveId = response.active_strategy_id || activeStrategyId;
      setActiveStrategyId(nextActiveId);
      setActiveAgent(response.active_agent || activeAgent);
      setStrategies((prev) => prev.map((item) => {
        if (item.id !== editingStrategy.id) return item;
        const updated = response.strategy || item;
        return {
          ...item,
          ...updated,
          name,
          description,
          is_selected: item.id === nextActiveId,
        };
      }));
      setEditingStrategy(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSavingStrategy(false);
    }
  };

  const handleDeleteStrategy = async (strategy: StrategyProfile) => {
    if (strategy.scope !== 'custom') return;
    const confirmed = window.confirm(`Delete strategy "${strategy.name}"? This cannot be undone.`);
    if (!confirmed) return;
    setDeletingStrategyId(strategy.id);
    setError('');
    try {
      const response = await api.deleteStrategy(strategy.id);
      setActiveStrategyId(response.active_strategy_id || '');
      setActiveAgent(response.active_agent || activeAgent);
      if (editingStrategy?.id === strategy.id) {
        setEditingStrategy(null);
      }
      await loadStrategies();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setDeletingStrategyId('');
    }
  };

  const handleEvaluate = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.evaluate(symbol, timeframe, barCount, activeAgent);
      setResult(res);
      setHistory((prev) => [res, ...prev].slice(0, 20));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  if (!connected) {
    return (
      <div>
        <div className="page-header"><h2>Strategy / Agent</h2></div>
        <div className="empty-state">Connect to MT5 first</div>
      </div>
    );
  }

  const currentCreatePreviewKey = JSON.stringify([
    strategyName.trim(),
    strategyDescription.trim(),
    Boolean(applyUnderAnyConditions),
  ]);
  const currentEditPreviewKey = JSON.stringify([
    editingStrategy?.id || '',
    editName.trim(),
    editDescription.trim(),
    Boolean(editApplyUnderAnyConditions),
  ]);

  return (
    <div>
      <div className="page-header">
        <h2>Strategy</h2>
        <p>Configure and run your trading agent. Current mode: {ACTIVE_SYMBOL_MODE_LABEL}.</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card mb-4">
        <div className="card-header">
          <h3>Strategy Builder</h3>
        </div>
        <div className="grid-2 gap-3">
          <div className="form-group">
            <label>Strategy Name</label>
            <input
              className="form-input"
              placeholder="e.g. Gold London Breakout"
              value={strategyName}
              onChange={(e) => setStrategyName(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label>Describe Strategy Logic</label>
            <textarea
              className="form-input"
              placeholder="Example: Focus on Gold and US indices, avoid low momentum sessions, use safer entries."
              value={strategyDescription}
              onChange={(e) => setStrategyDescription(e.target.value)}
              rows={4}
              style={{ resize: 'vertical' }}
            />
            <label style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={applyUnderAnyConditions}
                onChange={(e) => setApplyUnderAnyConditions(e.target.checked)}
              />
              Apply under any conditions (force execution, avoid HOLD when possible)
            </label>
          </div>
        </div>
        <button className="btn btn-primary" onClick={handleBuildStrategy} disabled={buildingStrategy}>
          {buildingStrategy ? <span className="loading-spinner" /> : <><Plus size={14} /> Add Strategy</>}
        </button>
        {renderStrategyPreview(
          strategyPreview,
          previewLoading,
          previewError,
          strategyPreviewKey === currentCreatePreviewKey,
          applyUnderAnyConditions,
        )}
        <div className="text-muted text-sm mt-2">
          Built-in strategies are shared with all users. Strategies you build appear only for your account.
        </div>
      </div>

      {strategies.length > 0 && (
        <div className="card mb-4">
          <div className="card-header">
            <h3>Available Strategies</h3>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {strategies.map((strategy, idx) => {
              const selected = strategy.id === activeStrategyId || strategy.is_selected;
              const isCustom = strategy.scope === 'custom';
              return (
                <div key={strategy.id} style={{ display: 'inline-flex', alignItems: 'center' }}>
                  <button
                    className="btn"
                    onClick={() => handleSelectStrategy(strategy.id)}
                    disabled={strategyLoadingId === strategy.id}
                    title={strategy.description || strategy.name}
                    style={{
                      fontSize: 13,
                      padding: '8px 16px',
                      borderRadius: 12,
                      border: selected ? '1px solid var(--accent-blue)' : '1px solid var(--border)',
                      background: selected ? 'var(--accent-blue)' : 'var(--bg-secondary)',
                      color: selected ? '#fff' : 'var(--text-primary)',
                      fontWeight: 600,
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <span>{idx === 0 ? `${strategy.name} (Default)` : strategy.name}</span>
                    <span
                      role="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        setStrategyInfo(strategy);
                        setStrategyInfoOpen(true);
                      }}
                      title={`About ${strategy.name}`}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        opacity: 0.9,
                        cursor: 'pointer',
                      }}
                    >
                      <HelpCircle size={14} />
                    </span>
                  </button>
                  {isCustom && (
                    <div style={{ display: 'inline-flex', marginLeft: 6, gap: 4 }}>
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => openEditStrategy(strategy)}
                        title={`Edit ${strategy.name}`}
                        style={{ padding: '7px 8px' }}
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="btn btn-danger btn-sm"
                        onClick={() => handleDeleteStrategy(strategy)}
                        disabled={deletingStrategyId === strategy.id}
                        title={`Delete ${strategy.name}`}
                        style={{ padding: '7px 8px' }}
                      >
                        {deletingStrategyId === strategy.id ? <span className="loading-spinner" /> : <Trash2 size={13} />}
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div className="text-muted text-sm mt-3">
            Active strategy: <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
              {strategies.find((s) => s.id === activeStrategyId)?.name || 'Default Smart'}
            </span>
          </div>
        </div>
      )}

      {editingStrategy && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.45)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1200,
            padding: 20,
          }}
          onClick={() => setEditingStrategy(null)}
        >
          <div
            className="card"
            style={{ width: 'min(620px, 94vw)', padding: 18 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex justify-between items-center" style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 16, fontWeight: 700 }}>Edit Strategy</div>
              <button className="btn btn-secondary btn-sm" onClick={() => setEditingStrategy(null)}>
                Close
              </button>
            </div>
            <div className="form-group">
              <label>Strategy Name</label>
              <input
                className="form-input"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                placeholder="Strategy name"
              />
            </div>
            <div className="form-group" style={{ marginTop: 10 }}>
              <label>Describe Strategy Logic</label>
              <textarea
                className="form-input"
                rows={5}
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                placeholder="Describe how this strategy should behave."
                style={{ resize: 'vertical' }}
              />
              <label style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <input
                  type="checkbox"
                  checked={editApplyUnderAnyConditions}
                  onChange={(e) => setEditApplyUnderAnyConditions(e.target.checked)}
                />
                Apply under any conditions (force execution, avoid HOLD when possible)
              </label>
            </div>
            <div className="flex justify-end gap-2" style={{ marginTop: 14 }}>
              <button className="btn btn-secondary" onClick={() => setEditingStrategy(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleUpdateStrategy} disabled={savingStrategy}>
                {savingStrategy ? <span className="loading-spinner" /> : 'Save Changes'}
              </button>
            </div>
              {renderStrategyPreview(
                editStrategyPreview,
                editPreviewLoading,
                editPreviewError,
                editStrategyPreviewKey === currentEditPreviewKey,
                editApplyUnderAnyConditions,
              )}
          </div>
        </div>
      )}

      {strategyInfoOpen && strategyInfo && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.45)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1200,
            padding: 20,
          }}
          onClick={() => setStrategyInfoOpen(false)}
        >
          <div
            className="card"
            style={{ width: 'min(560px, 92vw)', padding: 18 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex justify-between items-center" style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 16, fontWeight: 700 }}>{strategyInfo.name}</div>
              <button className="btn btn-secondary btn-sm" onClick={() => setStrategyInfoOpen(false)}>
                Close
              </button>
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-line' }}>
              {strategyInfo.brief || strategyInfo.description || 'No strategy brief available.'}
            </div>
          </div>
        </div>
      )}

      <div className="grid-2 mb-4">
        <div className="card">
          <div className="card-header">
            <h3>Available Agents</h3>
          </div>
          {Object.entries(agents).map(([key, agent]) => (
            <div
              key={key}
              style={{
                padding: '12px',
                borderRadius: 6,
                border: `1px solid ${activeAgent === key ? 'var(--accent-blue)' : 'var(--border)'}`,
                marginBottom: 8,
                cursor: 'pointer',
                background: activeAgent === key ? 'rgba(59, 130, 246, 0.1)' : 'transparent',
              }}
              onClick={() => handleSetAgent(key)}
            >
              <div className="flex justify-between items-center">
                <span style={{ fontWeight: 600, fontSize: 13 }}>{agent.name}</span>
                {activeAgent === key && <span className="badge badge-blue">Active</span>}
              </div>
              <div className="text-muted text-sm mt-2">{agent.description}</div>
            </div>
          ))}
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Evaluate Signal</h3>
          </div>

          <div className="grid-2 gap-3">
            <div className="form-group">
              <label>Symbol</label>
              <select className="form-input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
                {availableSymbols.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label>Timeframe</label>
              <select className="form-input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                {['M1', 'M5', 'M15', 'H1', 'H4', 'D1'].map((tf) => (
                  <option key={tf}>{tf}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-group">
            <label>Bar Count</label>
            <input
              className="form-input"
              type="number"
              value={barCount}
              onChange={(e) => setBarCount(parseInt(e.target.value) || 100)}
              min={10}
              max={500}
            />
          </div>

          <button className="btn btn-primary" onClick={handleEvaluate} disabled={loading}>
            {loading ? <span className="loading-spinner" /> : <Play size={14} />}
            Generate Signal
          </button>
        </div>
      </div>

      {result && (
        <div className="card mb-4">
          <div className="card-header">
            <h3>Latest Signal</h3>
            <span className="badge badge-blue">{result.agent_name}</span>
          </div>

          <div className="grid-3 gap-4 mb-4">
            <div>
              <div className="stat-label">Action</div>
              <span className={`badge ${
                result.signal.action === 'BUY' ? 'badge-green' :
                result.signal.action === 'SELL' ? 'badge-red' :
                'badge-yellow'
              }`} style={{ fontSize: 16, padding: '4px 16px' }}>
                {result.signal.action}
              </span>
            </div>
            <div>
              <div className="stat-label">Confidence</div>
              <div className="stat-value">{(result.signal.confidence * 100).toFixed(0)}%</div>
            </div>
            <div>
              <div className="stat-label">Risk Decision</div>
              <span className={`badge ${result.risk_decision.approved ? 'badge-green' : 'badge-red'}`}
                    style={{ fontSize: 14, padding: '4px 12px' }}>
                {result.risk_decision.approved ? 'APPROVED' : 'REJECTED'}
              </span>
            </div>
          </div>

          {result.trade_quality && (
            <div className="grid-3 gap-4 mb-4">
              <div>
                <div className="stat-label">Trade Quality</div>
                <div className="stat-value">{(result.trade_quality.final_trade_quality_score * 100).toFixed(0)}%</div>
              </div>
              <div>
                <div className="stat-label">Quality Threshold</div>
                <div className="stat-value">{(result.trade_quality.threshold * 100).toFixed(0)}%</div>
              </div>
              <div>
                <div className="stat-label">Gemini</div>
                <div className="font-mono">{result.gemini_confirmation?.degraded ? 'Degraded' : result.gemini_confirmation?.used ? 'Advisory' : 'Off'}</div>
              </div>
            </div>
          )}

          <div className="grid-4 gap-4 mb-4">
            <div>
              <div className="stat-label">Stop Loss</div>
              <div className="font-mono">{result.signal.stop_loss?.toFixed(5) ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Take Profit</div>
              <div className="font-mono">{result.signal.take_profit?.toFixed(5) ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Max Hold (min)</div>
              <div className="font-mono">{result.signal.max_holding_minutes ?? 'N/A'}</div>
            </div>
            <div>
              <div className="stat-label">Volume</div>
              <div className="font-mono">{result.risk_decision.adjusted_volume || 'N/A'}</div>
            </div>
          </div>

          <div>
            <div className="stat-label">Reason</div>
            <div className="text-sm mt-2" style={{ color: 'var(--text-secondary)' }}>
              {result.signal.reason}
            </div>
          </div>
          {!result.risk_decision.approved && (
            <div className="mt-2">
              <div className="stat-label">Rejection Reason</div>
              <div className="text-sm text-red mt-2">{result.risk_decision.reason}</div>
            </div>
          )}
          {result.execution_reason && (
            <div className="mt-2">
              <div className="stat-label">Execution Reason</div>
              <div className="text-sm mt-2">{result.execution_reason}</div>
            </div>
          )}
        </div>
      )}

      {history.length > 1 && (
        <div className="card">
          <div className="card-header">
            <h3>Signal History (this session)</h3>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Action</th>
                <th>Confidence</th>
                <th>Risk</th>
                <th>Volume</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h, i) => (
                <tr key={i}>
                  <td>{h.agent_name}</td>
                  <td>
                    <span className={`badge ${
                      h.signal.action === 'BUY' ? 'badge-green' :
                      h.signal.action === 'SELL' ? 'badge-red' : 'badge-yellow'
                    }`}>{h.signal.action}</span>
                  </td>
                  <td className="font-mono">{(h.signal.confidence * 100).toFixed(0)}%</td>
                  <td>
                    <span className={`badge ${h.risk_decision.approved ? 'badge-green' : 'badge-red'}`}>
                      {h.risk_decision.approved ? 'OK' : 'REJECT'}
                    </span>
                  </td>
                  <td className="font-mono">{h.risk_decision.adjusted_volume || '-'}</td>
                  <td className="text-sm text-muted" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {h.signal.reason}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
