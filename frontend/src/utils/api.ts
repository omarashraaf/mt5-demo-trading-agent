import { runtimeConfig } from '../config';
import { supabase } from '@/lib/supabase';

const BASE_URL = runtimeConfig.apiBaseUrl;
const LOCAL_PORT_FALLBACK_URL = BASE_URL.includes('127.0.0.1:8000')
  ? null
  : BASE_URL.includes('localhost:8000')
    ? null
    : BASE_URL.includes('127.0.0.1:8002')
      ? BASE_URL.replace('127.0.0.1:8002', '127.0.0.1:8000')
      : BASE_URL.includes('localhost:8002')
        ? BASE_URL.replace('localhost:8002', 'localhost:8000')
        : null;

let preferredBaseUrl: string = BASE_URL;

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
    const { data } = await supabase.auth.getSession();
    const accessToken = data.session?.access_token;
    if (accessToken) {
      authHeader = { Authorization: `Bearer ${accessToken}` };
    }
  }
  const requestOptions: RequestInit = {
    headers: { 'Content-Type': 'application/json', ...authHeader },
    ...options,
  };

  const candidates = LOCAL_PORT_FALLBACK_URL
    ? [preferredBaseUrl, preferredBaseUrl === BASE_URL ? LOCAL_PORT_FALLBACK_URL : BASE_URL]
    : [preferredBaseUrl];

  let res: Response | null = null;
  let lastError: unknown = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const base = candidates[i];
    const tradeExecutionPath = (
      path.includes('/trade/execute-recommendation')
      || path.includes('/trade/quick-buy')
      || path.includes('/trade/execute')
    );
    // Never retry trade execution requests on the client side.
    // A second attempt can feel like lag and may risk duplicate intents.
    const maxAttemptsForBase = tradeExecutionPath ? 1 : (i === 0 ? 2 : 1);
    for (let attemptIdx = 0; attemptIdx < maxAttemptsForBase; attemptIdx += 1) {
      try {
        const scanLikePath = path.includes('/agent/smart-evaluate') || path.includes('/auto-trade/activity');
        const availableSymbolsPath = path.includes('/market/available-symbols');
        const connectPath = (
          path.includes('/connect')
          || path.includes('/credentials/auto-connect')
        );
        const statusPath = path.includes('/status') || path.includes('/auto-trade/status');
        const timeoutMs = scanLikePath
          ? (i === 0 ? 45000 : 55000)
          : availableSymbolsPath
            ? (i === 0 ? 70000 : 80000)
          : tradeExecutionPath
            ? (i === 0 ? 120000 : 130000)
            : connectPath
              ? (i === 0 ? 15000 : 22000)
            : statusPath
              ? (i === 0 ? 15000 : 22000)
              : (i === 0 ? 25000 : 32000);
        const attempt = await fetchWithTimeout(`${base}${path}`, requestOptions, timeoutMs);
        preferredBaseUrl = base;
        res = attempt;
        break;
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
          lastError = new Error('Connection timed out. Please verify backend is running and IBKR TWS/Gateway API is reachable.');
        } else if (err instanceof Error && err.message.toLowerCase().includes('signal is aborted')) {
          lastError = new Error('Request was interrupted. Please retry connection and keep this page open until the response returns.');
        } else if (err instanceof Error && err.message.toLowerCase().includes('failed to fetch')) {
          lastError = new Error('Failed to fetch from backend. Retrying automatically...');
        } else {
          lastError = err;
        }
        if (attemptIdx < maxAttemptsForBase - 1) {
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
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  // Auth/Admin
  authMe: () =>
    request<{
      id: string;
      email: string;
      role: string;
      user_metadata: Record<string, unknown>;
      app_metadata: Record<string, unknown>;
    }>('/auth/me'),

  bootstrapAdmin: () =>
    request<{ ok: boolean; username: string; email: string; created: boolean }>('/auth/bootstrap-admin', {
      method: 'POST',
    }),

  adminListUsers: () =>
    request<{ users: Array<Record<string, unknown>>; count: number }>('/admin/users'),

  adminCreateUser: (data: { email: string; password: string; role: 'admin' | 'user' }) =>
    request<{ ok: boolean; user: Record<string, unknown> }>('/admin/users', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  adminUpdateRole: (data: { user_id: string; role: 'admin' | 'user' }) =>
    request<{ ok: boolean; user: Record<string, unknown> }>('/admin/users/role', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  adminActivity: (limit: number = 200) =>
    request<{ activity: Array<Record<string, unknown>>; count: number }>(`/admin/activity?limit=${limit}`),

  // Connection
  connect: (data: {
    platform?: 'mt5' | 'ibkr';
    account?: number;
    password?: string;
    server?: string;
    terminal_path?: string;
    save_credentials?: boolean;
    ibkr_host?: string;
    ibkr_port?: number;
    ibkr_client_id?: number;
    ibkr_account_id?: string;
  }) =>
    request<{
      connected: boolean;
      platform?: 'mt5' | 'ibkr';
      account?: import('@/types').AccountInfo;
      is_demo?: boolean;
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

  getTicks: (symbols: string[]) =>
    request<{ ticks: Record<string, import('@/types').TickData> }>(
      `/market/ticks?symbols=${encodeURIComponent(symbols.join(','))}`
    ),

  getBars: (symbol: string, timeframe: string, count: number = 100) =>
    request<import('@/types').BarData[]>(`/market/bars/${symbol}?timeframe=${timeframe}&count=${count}`),

  getSymbolInfo: (symbol: string) =>
    request(`/market/symbol-info/${symbol}`),

  getAvailableSymbols: (category?: string, tradeableOnly: boolean = false) =>
    request<{
      total: number;
      categories: Record<string, Array<{
        name: string; description: string; category: string;
        visible: boolean; trade_enabled: boolean; bid: number; ask: number; spread: number;
      }>>;
      category_counts: Record<string, number>;
    }>(`/market/available-symbols?tradeable_only=${tradeableOnly}${category ? `&category=${category}` : ''}`),

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

  // Trading
  executeTrade: (data: {
    symbol: string;
    action: string;
    volume: number;
    stop_loss: number;
    take_profit: number;
    signal_id?: number;
  }) =>
    request<import('@/types').OrderResult>('/trade/execute', { method: 'POST', body: JSON.stringify(data) }),

  getPositions: (symbol?: string) =>
    request<import('@/types').PositionInfo[]>(`/positions${symbol ? `?symbol=${symbol}` : ''}`),

  closePosition: (ticket: number) =>
    request<import('@/types').OrderResult>('/positions/close', { method: 'POST', body: JSON.stringify({ ticket }) }),

  // Risk
  getRiskSettings: () =>
    request<import('@/types').PolicySettingsResponse>('/risk/settings'),

  updateRiskSettings: (settings: import('@/types').UserPolicySettings) =>
    request<import('@/types').PolicySettingsResponse>('/risk/settings', { method: 'POST', body: JSON.stringify(settings) }),

  setPanicStop: (active: boolean) =>
    request('/risk/panic-stop', { method: 'POST', body: JSON.stringify({ active }) }),

  // Logs
  getLogs: (limit: number = 100, logType: string = 'all') =>
    request<import('@/types').LogEntry[]>(`/logs?limit=${limit}&log_type=${logType}`),

  getTradeHistory: (limit: number = 50) =>
    request<import('@/types').TradeHistoryResponse>(`/trade-history?limit=${limit}`),

  // Credentials
  getCredentials: () =>
    request<import('@/types').SavedCredentials[]>('/credentials'),

  saveCredentials: (data: { account: number; server: string; password: string; terminal_path?: string }) =>
    request('/credentials', { method: 'POST', body: JSON.stringify(data) }),

  deleteCredentials: (account: number) =>
    request(`/credentials/${account}`, { method: 'DELETE' }),

  autoConnect: (accountId?: number) =>
    request<{ connected: boolean; reason?: string; account?: import('@/types').AccountInfo }>(
      `/credentials/auto-connect${accountId ? `?account_id=${accountId}` : ''}`
    ),

  // Smart Evaluate
  smartEvaluate: (symbols?: string[]) =>
    request<import('@/types').SmartEvaluateResponse>('/agent/smart-evaluate', {
      method: 'POST',
      body: JSON.stringify({ symbols }),
    }),

  executeRecommendation: (signalId: number, amountUsd?: number, sl?: number, tp?: number) =>
    request<import('@/types').OrderResult>('/trade/execute-recommendation', {
      method: 'POST',
      body: JSON.stringify({ signal_id: signalId, amount_usd: amountUsd || undefined, custom_stop_loss: sl || undefined, custom_take_profit: tp || undefined }),
    }),

  quickBuy: (symbol: string, amountUsd: number, sl?: number, tp?: number, action: string = 'BUY') =>
    request<import('@/types').OrderResult>('/trade/quick-buy', {
      method: 'POST',
      body: JSON.stringify({ symbol, amount_usd: amountUsd, action, custom_stop_loss: sl || undefined, custom_take_profit: tp || undefined }),
    }),

  calculateVolume: (symbol: string, amountUsd: number) =>
    request<{
      symbol: string; amount_usd: number; volume: number;
      actual_cost: number; price: number; contract_size: number;
      volume_min: number; volume_max: number;
      min_sl_tp_dollars: number; stops_level: number;
    }>(`/trade/calculate-volume?symbol=${symbol}&amount_usd=${amountUsd}`),

  // Auto-Trading
  getAutoTradeStatus: () =>
    request<{
      running: boolean;
      enabled: boolean;
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
      }>;
      panic_stop: boolean;
      position_manager_running?: boolean;
      managed_tickets?: number[];
      portfolio?: import('@/types').PortfolioSnapshot;
      user_policy?: import('@/types').UserPolicySettings;
      gemini?: {
        available: boolean;
        degraded: boolean;
        last_error?: string | null;
        state?: 'available' | 'degraded' | 'cooldown' | 'unavailable' | string;
        cooldown_seconds?: number;
        credits_pct?: number;
      };
    }>('/auto-trade/status'),

  startAutoTrade: () =>
    request('/auto-trade/start', { method: 'POST' }),

  stopAutoTrade: () =>
    request('/auto-trade/stop', { method: 'POST' }),

  updateAutoTradeSettings: (settings: { enabled?: boolean; min_confidence?: number; scan_interval?: number }) =>
    request('/auto-trade/settings', { method: 'POST', body: JSON.stringify(settings) }),

  getAIActivity: (limit: number = 50) =>
    request<{
      live_activity: import('@/types').AIActivity[];
      db_activity: Array<{
        id: number; timestamp: number; action: string;
        symbol: string; ticket: number; detail: string; profit: number;
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

  // External events (Finnhub)
  getFinnhubHealth: () =>
    request<{ provider: import('@/types').ProviderHealth; universe?: Record<string, unknown> }>('/events/finnhub/health'),

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
      finnhub?: import('@/types').ProviderHealth;
    }>('/events/refresh', {
      method: 'POST',
      body: JSON.stringify(data || {}),
    }),

  getLatestEvents: (limit: number = 25) =>
    request<{ events: import('@/types').ExternalEventRecord[]; count: number }>(`/events/latest?limit=${limit}`),

  getEventMappings: (limit: number = 100, eventId?: number) =>
    request<{ mappings: import('@/types').EventAssetMappingRecord[]; count: number }>(
      `/events/mappings?limit=${limit}${eventId ? `&event_id=${eventId}` : ''}`,
    ),

  getGeminiEventAssessments: (limit: number = 50, eventId?: number) =>
    request<{ assessments: import('@/types').GeminiEventAssessmentRecord[]; count: number }>(
      `/events/gemini-assessments?limit=${limit}${eventId ? `&event_id=${eventId}` : ''}`,
    ),

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
    algorithm?: 'logistic_regression' | 'gradient_boosting';
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
    algorithm?: 'logistic_regression' | 'gradient_boosting';
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
};
