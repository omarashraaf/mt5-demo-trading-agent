import { runtimeConfig } from '../config';
import { supabase } from '@/lib/supabase';

const BASE_URL = runtimeConfig.apiBaseUrl;
const LOCAL_PORT_FALLBACK_URL = runtimeConfig.allowLocalFallback
  ? (
    BASE_URL.includes('127.0.0.1:8000')
      ? BASE_URL.replace('127.0.0.1:8000', '127.0.0.1:8002')
      : BASE_URL.includes('localhost:8000')
        ? BASE_URL.replace('localhost:8000', 'localhost:8002')
        : BASE_URL.includes('127.0.0.1:8002')
          ? BASE_URL.replace('127.0.0.1:8002', '127.0.0.1:8000')
          : BASE_URL.includes('localhost:8002')
            ? BASE_URL.replace('localhost:8002', 'localhost:8000')
            : null
  )
  : null;

let preferredBaseUrl: string = BASE_URL;
let cachedAccessToken: string | null = null;
let authCacheInitialized = false;
let authSubscriptionInitialized = false;
const inflightGetRequests = new Map<string, Promise<unknown>>();
const responseCache = new Map<string, { expiresAt: number; value: unknown }>();

function clearRequestCaches() {
  inflightGetRequests.clear();
  responseCache.clear();
}

function stableIdempotencyKey(scope: string, payload: Record<string, unknown>): string {
  const normalized = Object.keys(payload)
    .sort()
    .map((key) => `${key}=${String(payload[key] ?? '')}`)
    .join('|');
  let hash = 0;
  for (let i = 0; i < normalized.length; i += 1) {
    hash = ((hash << 5) - hash + normalized.charCodeAt(i)) | 0;
  }
  return `${scope}:${Math.abs(hash).toString(36)}:${normalized.slice(0, 48)}`;
}

function cacheTtlMs(path: string): number {
  if (
    path.includes('/status')
    || path.includes('/positions')
    || path.includes('/market/ticks')
    || path.includes('/auto-trade/status')
    || path.includes('/auto-trade/activity')
    || path.includes('/agent/smart-evaluate')
    || path.includes('/logs')
  ) {
    return 0;
  }
  if (path.includes('/market/available-symbols')) return 15000;
  if (path.includes('/strategies')) return 10000;
  if (path.includes('/research/status')) return 15000;
  if (path.includes('/trade-history')) return 5000;
  if (path.includes('/events/recommendations')) return 10000;
  return 0;
}

async function ensureAuthCache() {
  if (!supabase) return;
  if (!authSubscriptionInitialized) {
    authSubscriptionInitialized = true;
    supabase.auth.onAuthStateChange((_event, session) => {
      cachedAccessToken = session?.access_token ?? null;
      clearRequestCaches();
    });
  }
  if (authCacheInitialized) return;
  authCacheInitialized = true;
  const { data } = await supabase.auth.getSession();
  cachedAccessToken = data.session?.access_token ?? null;
}

async function fetchWithTimeout(url: string, options: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let authHeader: Record<string, string> = {};
  if (supabase) {
    await ensureAuthCache();
    if (cachedAccessToken) {
      authHeader = { Authorization: `Bearer ${cachedAccessToken}` };
    }
  }
  const optionHeaders = options?.headers
    ? Object.fromEntries(new Headers(options.headers).entries())
    : {};
  const method = String(options?.method || 'GET').toUpperCase();
  const isMutation = method !== 'GET' && method !== 'HEAD';
  const hasBody = options?.body != null && method !== 'GET' && method !== 'HEAD';
  const mergedHeaders: Record<string, string> = { ...authHeader, ...optionHeaders };
  if (hasBody && !Object.keys(mergedHeaders).some((k) => k.toLowerCase() === 'content-type')) {
    mergedHeaders['Content-Type'] = 'application/json';
  }
  const requestOptions: RequestInit = {
    ...options,
    headers: mergedHeaders,
  };
  const adminPath = path.startsWith('/admin/');
  const cacheKey = `${method}:${path}`;
  const ttlMs = method === 'GET' ? cacheTtlMs(path) : 0;

  if (method === 'GET') {
    const cached = responseCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) {
      return cached.value as T;
    }
    const inflight = inflightGetRequests.get(cacheKey);
    if (inflight) {
      return inflight as Promise<T>;
    }
  } else {
    clearRequestCaches();
  }

  const executeRequest = async (): Promise<T> => {
    const candidates = adminPath
      ? (
        LOCAL_PORT_FALLBACK_URL
          ? [BASE_URL, LOCAL_PORT_FALLBACK_URL]
          : [BASE_URL]
      )
      : (
        LOCAL_PORT_FALLBACK_URL
          ? [preferredBaseUrl, preferredBaseUrl === BASE_URL ? LOCAL_PORT_FALLBACK_URL : BASE_URL]
          : [preferredBaseUrl]
      );

    let res: Response | null = null;
    let lastError: unknown = null;
    for (let i = 0; i < candidates.length; i += 1) {
      const base = candidates[i];
      const tradeExecutionPath = (
        path.includes('/trade/execute-recommendation')
        || path.includes('/trade/quick-buy')
        || path.includes('/trade/execute')
        || path.includes('/positions/close')
        || path.includes('/positions/modify')
      );
      const liveTickPath = path.includes('/market/ticks');
      const connectPath = (
        path.includes('/connect')
        || path.includes('/credentials/auto-connect')
      );
      const autoConnectPath = path.includes('/credentials/auto-connect');
      const strategyPreviewPath = path.includes('/strategies/preview');
      const strategyBuildPath = path.includes('/strategies/build') || path.includes('/strategies/update');
      const autoTradeControlPath = (
        path.includes('/auto-trade/start')
        || path.includes('/auto-trade/stop')
        || path.includes('/auto-trade/settings')
      );
      const maxAttemptsForBase = (adminPath || tradeExecutionPath || liveTickPath || isMutation || connectPath) ? 1 : (i === 0 ? 2 : 1);
      for (let attemptIdx = 0; attemptIdx < maxAttemptsForBase; attemptIdx += 1) {
        try {
          const scanLikePath = path.includes('/agent/smart-evaluate') || path.includes('/auto-trade/activity');
          const availableSymbolsPath = path.includes('/market/available-symbols');
          const statusPath = path.includes('/status') || path.includes('/auto-trade/status');
          const timeoutMs = liveTickPath
            ? (i === 0 ? 1000 : 1500)
            : adminPath
            ? (i === 0 ? 9000 : 12000)
            : scanLikePath
            ? (i === 0 ? 45000 : 55000)
            : availableSymbolsPath
              ? (i === 0 ? 70000 : 80000)
              : tradeExecutionPath
                ? (i === 0 ? 90000 : 100000)
                : strategyPreviewPath
                  ? (i === 0 ? 18000 : 22000)
                  : strategyBuildPath
                    ? (i === 0 ? 20000 : 26000)
                    : autoTradeControlPath
                      ? (i === 0 ? 30000 : 40000)
                      : autoConnectPath
                        ? (i === 0 ? 10000 : 15000)
                        : connectPath
                          ? (i === 0 ? 35000 : 50000)
                          : statusPath
                            ? (i === 0 ? 8000 : 12000)
                            : isMutation
                              ? (i === 0 ? 12000 : 16000)
                              : (i === 0 ? 25000 : 32000);
          const attempt = await fetchWithTimeout(`${base}${path}`, requestOptions, timeoutMs);
          preferredBaseUrl = base;
          res = attempt;
          break;
        } catch (err) {
          if (err instanceof Error && err.name === 'AbortError') {
            if (strategyPreviewPath) {
              lastError = new Error('Strategy preview is taking longer than expected. Please wait a moment and try again.');
            } else if (connectPath) {
              lastError = new Error('Connection timed out. Please verify backend is running and the MT5 terminal is reachable.');
            } else if (tradeExecutionPath) {
              lastError = new Error('Trade request timed out while waiting for broker confirmation. Check Trade History/Activity before retrying; the broker may still respond after the browser timeout.');
            } else {
              lastError = new Error('Request timed out. Please verify backend is running and reachable.');
            }
          } else if (err instanceof Error && err.message.toLowerCase().includes('signal is aborted')) {
            lastError = new Error('Request was interrupted. Please retry connection and keep this page open until the response returns.');
          } else if (err instanceof Error && err.message.toLowerCase().includes('failed to fetch')) {
            lastError = new Error('Failed to fetch from backend. Retrying automatically...');
          } else {
            lastError = err;
          }
          if (!isMutation && attemptIdx < maxAttemptsForBase - 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
          }
        }
      }
      if (res) break;
    }

    if (!res) {
      throw lastError instanceof Error ? lastError : new Error('Network request failed');
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      if (path.startsWith('/admin/') && (res.status === 401 || res.status === 403)) {
        localStorage.removeItem('linktrade_admin_token');
        throw new Error('Admin session expired. Please login again.');
      }
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const json = await res.json();
    if (ttlMs > 0) {
      responseCache.set(cacheKey, { expiresAt: Date.now() + ttlMs, value: json });
    }
    return json as T;
  };

  if (method === 'GET') {
    const promise = executeRequest().finally(() => {
      inflightGetRequests.delete(cacheKey);
    });
    inflightGetRequests.set(cacheKey, promise);
    return promise;
  }

  return executeRequest();
}

function adminAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('linktrade_admin_token');
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

export const api = {
  // Auth/Admin
  authMe: () =>
    request<{
      id: string;
      email: string;
      role: string;
      approved: boolean;
      access_status: 'pending' | 'approved' | 'rejected' | string;
      user_metadata: Record<string, unknown>;
      app_metadata: Record<string, unknown>;
    }>('/auth/me'),

  publicRegister: (data: { email: string; password: string }) =>
    request<{ ok: boolean; message: string }>('/public/register', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  bootstrapAdmin: () =>
    request<{ ok: boolean; username: string; email: string; created: boolean }>('/auth/bootstrap-admin', {
      method: 'POST',
    }),

  adminListUsers: () =>
    request<{ users: Array<Record<string, unknown>>; count: number }>('/admin/users', {
      headers: adminAuthHeaders(),
    }),

  adminCreateUser: (data: { email: string; password: string; role: 'admin' | 'user' }) =>
    request<{ ok: boolean; user: Record<string, unknown> }>('/admin/users', {
      method: 'POST',
      headers: adminAuthHeaders(),
      body: JSON.stringify(data),
    }),

  adminUpdateRole: (data: { user_id: string; role: 'admin' | 'user' }) =>
    request<{ ok: boolean; user: Record<string, unknown> }>('/admin/users/role', {
      method: 'POST',
      headers: adminAuthHeaders(),
      body: JSON.stringify(data),
    }),

  adminActivity: (limit: number = 200) =>
    request<{ activity: Array<Record<string, unknown>>; count: number }>(`/admin/activity?limit=${limit}`, {
      headers: adminAuthHeaders(),
    }),

  adminListAccessRequests: (status?: 'pending' | 'approved' | 'rejected') =>
    request<{ items: Array<Record<string, unknown>>; count: number }>(
      `/admin/access-requests${status ? `?status=${status}` : ''}`,
      { headers: adminAuthHeaders() },
    ),

  adminUpdateAccessRequest: (data: { user_id: string; status: 'pending' | 'approved' | 'rejected'; notes?: string }) =>
    request<{ ok: boolean }>('/admin/access-requests', {
      method: 'POST',
      headers: adminAuthHeaders(),
      body: JSON.stringify(data),
    }),

  adminCustomers: () =>
    request<{ items: Array<Record<string, unknown>>; count: number }>('/admin/customers', {
      headers: adminAuthHeaders(),
    }),

  adminUserDetail: (userId: string) =>
    request<{ user: Record<string, unknown>; activity: Array<Record<string, unknown>>; activity_count: number }>(
      `/admin/users/${encodeURIComponent(userId)}`,
      { headers: adminAuthHeaders() },
    ),

  adminLogin: (username: string, password: string) =>
    request<{ ok: boolean; token: string; expires_in: number; username: string }>('/admin/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  adminSession: () =>
    request<{ ok: boolean; username: string; expires_at: number }>('/admin/session', {
      headers: adminAuthHeaders(),
    }),

  adminApiKeysHealth: (forceRefresh: boolean = false) =>
    request<import('@/types').ApiKeysHealthResponse>(
      `/admin/api-keys-health${forceRefresh ? '?force_refresh=1' : ''}`,
      { headers: adminAuthHeaders() },
    ),

  adminSetApiHealthEmailAlerts: (enabled: boolean) =>
    request<import('@/types').ApiKeysHealthResponse>('/admin/api-keys-health/email-alerts', {
      method: 'POST',
      headers: adminAuthHeaders(),
      body: JSON.stringify({ enabled }),
    }),

  // Connection
  connect: (data: {
    platform?: 'mt5' | 'ibkr' | 'tradestation';
    terminal_path?: string;
    host?: string;
    port?: number;
    client_id?: number;
    account_id?: string;
    readonly?: boolean;
    ibkr_environment?: 'paper' | 'live';
    environment?: 'sim' | 'live';
    redirect_uri?: string;
  }) =>
    request<{
      connected: boolean;
      platform?: 'mt5' | 'ibkr' | 'tradestation';
      account?: import('@/types').AccountInfo;
      is_demo?: boolean;
      auth_required?: boolean;
      auth_url?: string;
      environment?: 'paper' | 'sim' | 'live';
      credential_status?: { requested?: boolean; saved?: boolean; reason?: string };
    }>('/connect', { method: 'POST', body: JSON.stringify(data) }),

  disconnect: () =>
    request('/disconnect', { method: 'POST' }),

  getStatus: () =>
    request<import('@/types').StatusResponse>('/status'),

  getAccount: () =>
    request<import('@/types').AccountInfo>('/account'),

  verifyTerminal: (path?: string) =>
    request('/verify-terminal', { method: 'POST', body: JSON.stringify({ path }) }),

  // Market Data
  selectSymbols: (symbols: string[]) =>
    request('/symbols/select', { method: 'POST', body: JSON.stringify({ symbols }) }),

  getTick: (symbol: string) =>
    request<import('@/types').TickData>(`/market/tick/${symbol}`),

  getTicks: (symbols: string[], uiFast: boolean = true) =>
    request<{ ticks: Record<string, import('@/types').TickData> }>(
      `/market/ticks?symbols=${encodeURIComponent(symbols.join(','))}${uiFast ? '&ui_fast=1' : ''}`
    ),

  getBars: (symbol: string, timeframe: string, count: number = 100) =>
    request<import('@/types').BarData[]>(`/market/bars/${symbol}?timeframe=${timeframe}&count=${count}`),

  getSymbolInfo: (symbol: string) =>
    request(`/market/symbol-info/${symbol}`),

  getAvailableSymbols: (category?: string, tradeableOnly: boolean = false, includeInactive: boolean = false) =>
    request<{
      total: number;
      symbols: Array<{
        name: string; description: string; category: string;
        visible: boolean; trade_enabled: boolean; bid: number; ask: number; spread: number;
        live_quote?: boolean; requires_connection?: boolean; source?: string;
        active_in_current_mode?: boolean; inactive_reason?: string;
      }>;
      categories: Record<string, Array<{
        name: string; description: string; category: string;
        visible: boolean; trade_enabled: boolean; bid: number; ask: number; spread: number;
        live_quote?: boolean; requires_connection?: boolean; source?: string;
        active_in_current_mode?: boolean; inactive_reason?: string;
      }>>;
      category_counts: Record<string, number>;
      connected?: boolean;
      live_quotes?: boolean;
      source?: string;
    }>(`/market/available-symbols?tradeable_only=${tradeableOnly}&include_inactive=${includeInactive}${category ? `&category=${category}` : ''}`),

  autoDetectSymbols: (categories?: string[]) =>
    request<{ detected: string[]; count: number }>('/market/auto-detect-symbols', {
      method: 'POST',
      body: JSON.stringify(categories),
    }),

  // Agent
  evaluate: (symbol: string, timeframe: string, barCount?: number, agentName?: string) =>
    request<import('@/types').EvaluateResponse>('/agent/evaluate', {
      method: 'POST',
      body: JSON.stringify({
        symbol,
        timeframe,
        bar_count: barCount || 100,
        agent_name: agentName,
      }),
    }),

  getAgents: () =>
    request<Record<string, import('@/types').AgentInfo>>('/agents'),

  setAgent: (agentName: string) =>
    request('/agent/set', { method: 'POST', body: JSON.stringify({ agent_name: agentName }) }),

  getStrategies: () =>
    request<import('@/types').StrategiesResponse>('/strategies'),

  previewStrategy: (data: { name: string; description: string; apply_under_any_conditions?: boolean }) =>
    request<{
      ok: boolean;
      strategy: import('@/types').StrategyProfile;
    }>('/strategies/preview', { method: 'POST', body: JSON.stringify(data) }),

  buildStrategy: (data: { name: string; description: string; apply_under_any_conditions?: boolean }) =>
    request<{
      ok: boolean;
      active_strategy_id: string;
      strategy: import('@/types').StrategyProfile;
      active_agent: string;
      user_policy: import('@/types').UserPolicySettings;
    }>('/strategies/build', { method: 'POST', body: JSON.stringify(data) }),

  selectStrategy: (strategyId: string) =>
    request<{
      ok: boolean;
      active_strategy_id: string;
      active_strategy: import('@/types').StrategyProfile;
      active_agent: string;
      user_policy: import('@/types').UserPolicySettings;
      scan_interval: number;
    }>('/strategies/select', { method: 'POST', body: JSON.stringify({ strategy_id: strategyId }) }),

  updateStrategy: (data: { strategy_id: string; name: string; description: string; apply_under_any_conditions?: boolean }) =>
    request<{
      ok: boolean;
      active_strategy_id: string;
      strategy: import('@/types').StrategyProfile;
      active_agent: string;
      user_policy: import('@/types').UserPolicySettings;
    }>('/strategies/update', { method: 'POST', body: JSON.stringify(data) }),

  deleteStrategy: (strategyId: string) =>
    request<{
      ok: boolean;
      deleted_strategy_id: string;
      active_strategy_id: string;
      active_agent: string;
      user_policy: import('@/types').UserPolicySettings;
    }>('/strategies/delete', { method: 'POST', body: JSON.stringify({ strategy_id: strategyId }) }),

  // Trading
  executeTrade: (data: {
    symbol: string;
    action: string;
    volume: number;
    stop_loss: number;
    take_profit: number;
    signal_id?: number;
    idempotency_key?: string;
  }) => {
    const payload = {
      ...data,
      idempotency_key: data.idempotency_key || stableIdempotencyKey('manual', data),
    };
    return request<import('@/types').OrderResult>('/trade/execute', { method: 'POST', body: JSON.stringify(payload) });
  },

  getPositions: (symbol?: string, includeMargin: boolean = false) =>
    request<import('@/types').PositionInfo[]>(
      `/positions?include_margin=${includeMargin ? 'true' : 'false'}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`
    ),

  closePosition: (ticket: number) =>
    request<import('@/types').OrderResult>('/positions/close', { method: 'POST', body: JSON.stringify({ ticket }) }),
  cancelPositionClose: (ticket: number) =>
    request<import('@/types').OrderResult>('/positions/cancel-close', { method: 'POST', body: JSON.stringify({ ticket }) }),

  closeAllPositions: () =>
    request<{
      success: boolean;
      requested: number;
      closed: number;
      failed: number;
      results: Array<Record<string, unknown>>;
    }>('/positions/close-all', { method: 'POST' }),

  modifyPosition: (ticket: number, stopLoss: number, takeProfit: number) =>
    request<import('@/types').OrderResult>('/positions/modify', {
      method: 'POST',
      body: JSON.stringify({ ticket, stop_loss: stopLoss, take_profit: takeProfit }),
    }),

  // Risk
  getRiskSettings: () =>
    request<import('@/types').PolicySettingsResponse>('/risk/settings'),

  updateRiskSettings: (settings: import('@/types').UserPolicySettings) =>
    request<import('@/types').PolicySettingsResponse>('/risk/settings', { method: 'POST', body: JSON.stringify(settings) }),

  setPanicStop: (active: boolean) =>
    request('/risk/panic-stop', { method: 'POST', body: JSON.stringify({ active }) }),

  getPortfolioTargets: () =>
    request<import('@/types').PortfolioTargetsResponse>('/risk/portfolio-targets'),

  setPortfolioTargets: (data: {
    stop_loss_usd?: number | null;
    take_profit_usd?: number | null;
    mode?: 'auto' | 'manual';
  }) =>
    request<import('@/types').PortfolioTargetsResponse>('/risk/portfolio-targets', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // Logs
  getLogs: (limit: number = 100, logType: string = 'all') =>
    request<import('@/types').LogEntry[]>(`/logs?limit=${limit}&log_type=${logType}`),

  getTradeHistory: (limit: number = 50, view: 'closed' | 'all' = 'closed') =>
    request<import('@/types').TradeHistoryResponse>(
      `/trade-history?limit=${limit}&view=${encodeURIComponent(view)}`
    ),

  reconcileRuntimeState: (limit: number = 300) =>
    request<import('@/types').ReconciliationReport>(`/reconcile?limit=${encodeURIComponent(String(limit))}`, {
      method: 'POST',
    }),

  getSimulatedExecutionResults: (limit: number = 100) =>
    request<import('@/types').SimulatedExecutionResult[]>(
      `/execution/simulated-results?limit=${encodeURIComponent(String(limit))}`
    ),

  getTradeCard: (signalId: number) =>
    request<import('@/types').ExplainableTradeCard>(
      `/trade-card/${encodeURIComponent(String(signalId))}`
    ),

  analyzeTradeHistoryItem: (
    ticket: number,
    forceRefresh: boolean = false,
    useGemini: boolean = true,
    tradeFallback?: {
      signal_id?: number | null;
      symbol?: string;
      action?: 'BUY' | 'SELL';
      profit_usd?: number | null;
      started_with_usd?: number | null;
      signal_reason?: string;
      risk_reason?: string;
      exit_reason?: string | null;
    },
  ) =>
    request<{ ticket: number; signal_id: number | null; symbol: string; analysis: Record<string, unknown>; cached: boolean }>(
      `/trade-history/${ticket}/analyze`,
      {
        method: 'POST',
        body: JSON.stringify({
          force_refresh: forceRefresh,
          use_gemini: useGemini,
          ...tradeFallback,
        }),
      },
    ),

  // Smart Evaluate
  smartEvaluate: (symbols?: string[], options?: { manual?: boolean; autoExecute?: boolean }) =>
    request<import('@/types').SmartEvaluateResponse>('/agent/smart-evaluate', {
      method: 'POST',
      body: JSON.stringify({ symbols, manual: !!options?.manual, auto_execute: !!options?.autoExecute }),
    }),

  executeRecommendation: (signalId: number, amountUsd?: number, sl?: number, tp?: number) => {
    const payload = {
      signal_id: signalId,
      amount_usd: amountUsd || undefined,
      custom_stop_loss: sl || undefined,
      custom_take_profit: tp || undefined,
    };
    return request<import('@/types').OrderResult>('/trade/execute-recommendation', {
      method: 'POST',
      body: JSON.stringify({
        ...payload,
        idempotency_key: stableIdempotencyKey('recommendation', payload),
      }),
    });
  },

  quickBuy: (
    symbol: string,
    amountUsd: number,
    sl?: number,
    tp?: number,
    action: string = 'BUY',
    context?: {
      signal_id?: number | null;
      signal_confidence?: number | null;
      quality_score?: number | null;
      signal_reason?: string | null;
      signal_agent_name?: string | null;
    },
  ) =>
    request<import('@/types').OrderResult>('/trade/quick-buy', {
      method: 'POST',
      body: JSON.stringify({
        symbol,
        amount_usd: amountUsd,
        action,
        custom_stop_loss: sl || undefined,
        custom_take_profit: tp || undefined,
        signal_id: context?.signal_id ?? undefined,
        signal_confidence: context?.signal_confidence ?? undefined,
        quality_score: context?.quality_score ?? undefined,
        signal_reason: context?.signal_reason ?? undefined,
        signal_agent_name: context?.signal_agent_name ?? undefined,
        idempotency_key: stableIdempotencyKey('quick_buy', {
          symbol,
          amount_usd: amountUsd,
          action,
          custom_stop_loss: sl || undefined,
          custom_take_profit: tp || undefined,
          signal_id: context?.signal_id ?? undefined,
        }),
      }),
    }),

  calculateVolume: (symbol: string, amountUsd: number) =>
    request<{
      symbol: string; amount_usd: number; volume: number;
      actual_cost: number; price: number; contract_size: number;
      volume_min: number; volume_max: number;
      trade_tick_value?: number; trade_tick_size?: number; point?: number;
      min_sl_tp_dollars: number; stops_level: number;
    }>(`/trade/calculate-volume?symbol=${symbol}&amount_usd=${amountUsd}`),

  // Auto-Trading
  getAutoTradeStatus: () =>
    request<{
      running: boolean;
      enabled: boolean;
      budget_mode?: 'full_margin' | 'custom_budget';
      budget_usd?: number | null;
      min_confidence: number;
      scan_interval: number;
      last_scan: number;
      recent_trades: Array<{
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
        blocking_reasons?: string[];
        risk_warnings?: string[];
        machine_reasons?: string[];
        portfolio_status?: string;
        allow_execute?: boolean;
        portfolio_fit_score?: number | null;
        projected_margin_utilization_pct?: number | null;
        projected_free_margin_pct?: number | null;
        gemini_state?: string;
        gemini_bias?: string;
        gemini_event_risk?: string;
      }>;
      panic_stop: boolean;
      budget_mode?: 'full_margin' | 'custom_budget';
      budget_usd?: number | null;
      position_manager_running?: boolean;
      managed_tickets?: number[];
      portfolio?: import('@/types').PortfolioSnapshot;
      user_policy?: import('@/types').UserPolicySettings;
      gemini?: {
        available: boolean;
        degraded: boolean;
        last_error?: string | null;
        state?: 'available' | 'degraded' | 'cooldown' | 'unavailable' | string;
        primary_model?: string;
        active_model?: string;
        using_fallback_model?: boolean;
        cooldown_seconds?: number;
        credits_pct?: number;
      };
    }>('/auto-trade/status'),

  startAutoTrade: (settings?: {
    budget_mode?: 'full_margin' | 'custom_budget';
    budget_usd?: number;
  }) =>
    request('/auto-trade/start', { method: 'POST', body: JSON.stringify(settings ?? {}) }),

  stopAutoTrade: () =>
    request('/auto-trade/stop', { method: 'POST' }),

  updateAutoTradeSettings: (settings: {
    enabled?: boolean;
    min_confidence?: number;
    scan_interval?: number;
    budget_mode?: 'full_margin' | 'custom_budget';
    budget_usd?: number;
  }) =>
    request('/auto-trade/settings', { method: 'POST', body: JSON.stringify(settings) }),

  getAIActivity: (limit: number = 50) =>
    request<{
      live_activity: import('@/types').AIActivity[];
      db_activity: Array<{
        id: number; timestamp: number; action: string;
        symbol: string; ticket: number; detail: string; profit: number;
        signal_id?: number | null;
        profit_pct?: number | null;
        decision_reason?: string;
        gemini_summary?: string;
        meta_model_summary?: string;
        blocking_reasons?: string[];
        risk_warnings?: string[];
        machine_reasons?: string[];
        portfolio_status?: string;
        allow_execute?: boolean;
        portfolio_fit_score?: number | null;
        projected_margin_utilization_pct?: number | null;
        projected_free_margin_pct?: number | null;
        gemini_state?: string;
        gemini_bias?: string;
        gemini_event_risk?: string;
      }>;
      position_manager: {
        running: boolean;
        managed_tickets: number[];
      };
    }>(`/auto-trade/activity?limit=${limit}`),

  sendChatMessage: (
    message: string,
    history: import('@/types').ChatMessageItem[] = [],
    executeTrade: boolean = false,
  ) =>
    request<import('@/types').ChatResponse>('/chat/message', {
      method: 'POST',
      body: JSON.stringify({
        message,
        history,
        execute_trade: executeTrade,
      }),
    }),

  getChatHistory: () =>
    request<import('@/types').ChatHistoryResponse>('/chat/history'),

  saveChatHistory: (messages: import('@/types').ChatMessageItem[]) =>
    request<{ saved: boolean }>('/chat/history', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),

  clearChatHistory: () =>
    request<{ cleared: boolean }>('/chat/history', { method: 'DELETE' }),

  // External events (Benzinga)
  getBenzingaHealth: () =>
    request<{ provider: import('@/types').ProviderHealth; universe?: Record<string, unknown> }>('/events/benzinga/health'),

  refreshEvents: (data?: {
    from_date?: string;
    to_date?: string;
    news_category?: string;
    classify_with_gemini?: boolean;
  }) =>
    request<{
      provider: string;
      success: boolean;
      error?: string;
      raw_item_count: number;
      stored_event_count: number;
      mapped_assets_count: number;
      gemini_assessment_count: number;
      degraded: boolean;
      degraded_reasons?: string[];
      from_date?: string;
      to_date?: string;
      benzinga?: import('@/types').ProviderHealth;
    }>('/events/refresh', {
      method: 'POST',
      body: JSON.stringify(data || {}),
    }),

  getLatestEvents: (limit: number = 25) =>
    request<{ events: import('@/types').ExternalEventRecord[]; count: number }>(`/events/latest?limit=${limit}`),
  getFilteredEvents: (params?: { limit?: number; eventType?: string; orderBy?: 'timestamp' | 'created' }) => {
    const q = new URLSearchParams();
    q.set('limit', String(params?.limit ?? 25));
    if (params?.eventType) q.set('event_type', params.eventType);
    if (params?.orderBy) q.set('order_by', params.orderBy);
    return request<{ events: import('@/types').ExternalEventRecord[]; count: number }>(`/events/latest?${q.toString()}`);
  },

  getEventMappings: (limit: number = 100, eventId?: number) =>
    request<{ mappings: import('@/types').EventAssetMappingRecord[]; count: number }>(
      `/events/mappings?limit=${limit}${eventId ? `&event_id=${eventId}` : ''}`,
    ),

  getGeminiEventAssessments: (limit: number = 50, eventId?: number) =>
    request<{ assessments: import('@/types').GeminiEventAssessmentRecord[]; count: number }>(
      `/events/gemini-assessments?limit=${limit}${eventId ? `&event_id=${eventId}` : ''}`,
    ),
  getEventRecommendations: (params?: { limit?: number; forceRefresh?: boolean; classifyWithGemini?: boolean }) => {
    const q = new URLSearchParams();
    q.set('limit', String(params?.limit ?? 8));
    if (params?.forceRefresh != null) q.set('force_refresh', params.forceRefresh ? '1' : '0');
    if (params?.classifyWithGemini != null) q.set('classify_with_gemini', params.classifyWithGemini ? '1' : '0');
    return request<{ provider: import('@/types').ProviderHealth; recommendations: import('@/types').EventMarketRecommendation[]; count: number }>(
      `/events/recommendations?${q.toString()}`
    );
  },

  // Research cycle
  getResearchStatus: () =>
    request<import('@/types').ResearchStatusResponse>('/research/status'),

  rebuildResearchDataset: (data: {
    output_name?: string;
    limit?: number;
    include_unexecuted?: boolean;
    parquet?: boolean;
  }) =>
    request('/research/dataset/rebuild', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  trainResearchModel: (data: {
    algorithm?: 'logistic_regression' | 'gradient_boosting' | 'empirical_rules';
    target_column?: string;
    include_unexecuted?: boolean;
    min_rows?: number;
  }) =>
    request('/research/model/train', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  runResearchReplay: (data: {
    version_id: string;
    score_threshold?: number;
    include_unexecuted?: boolean;
    limit?: number;
  }) =>
    request('/research/replay/run', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  runResearchWalkForward: (data: {
    algorithm?: 'logistic_regression' | 'gradient_boosting' | 'empirical_rules';
    target_column?: string;
    score_threshold?: number;
    windows?: number;
    include_unexecuted?: boolean;
    limit?: number;
  }) =>
    request('/research/walk-forward/run', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  listResearchModels: (limit: number = 50) =>
    request<{ models: import('@/types').ResearchModelVersion[] }>(`/research/models?limit=${limit}`),

  approveResearchModel: (versionId: string) =>
    request('/research/models/' + encodeURIComponent(versionId) + '/approve', { method: 'POST' }),

  activateApprovedResearchModel: () =>
    request('/research/models/activate-approved', { method: 'POST' }),

  generateAttributionReport: (data?: { report_type?: string; limit?: number }) =>
    request('/research/reports/attribution', {
      method: 'POST',
      body: JSON.stringify(data || {}),
    }),

  listAttributionReports: (limit: number = 20) =>
    request<{ reports: import('@/types').ResearchReportRecord[] }>(`/research/reports?limit=${limit}`),

  getPerformanceAttribution: (limit: number = 2000) =>
    request<Record<string, unknown>>(`/analytics/performance?limit=${encodeURIComponent(String(limit))}`),
};
