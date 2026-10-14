const env = import.meta.env;

function ensureTrailingApiPath(base: string): string {
  const normalized = base.replace(/\/+$/, '');
  return normalized.endsWith('/api') ? normalized : `${normalized}/api`;
}

function buildApiBaseUrl(): string {
  const explicit = env.VITE_API_BASE_URL?.trim();
  if (explicit) {
    try {
      return ensureTrailingApiPath(new URL(explicit).toString());
    } catch {
      throw new Error(`Invalid VITE_API_BASE_URL: ${explicit}`);
    }
  }

  const hostname = window.location.hostname || '';
  const isLocalHost = (
    hostname === 'localhost'
    || hostname === '127.0.0.1'
    || hostname.endsWith('.local')
  );
  if (!isLocalHost) {
    // Cloud-first default: if app is served from a real domain, call same-origin API.
    return ensureTrailingApiPath(window.location.origin);
  }

  const host = env.VITE_API_HOST?.trim() || window.location.hostname || '127.0.0.1';
  const port = env.VITE_API_PORT?.trim() || '8000';
  if (!/^\d+$/.test(port)) {
    throw new Error(`Invalid VITE_API_PORT: ${port}`);
  }
  return `http://${host}:${port}/api`;
}

export const runtimeConfig = {
  apiBaseUrl: buildApiBaseUrl(),
  allowLocalFallback: env.VITE_ALLOW_LOCAL_FALLBACK?.trim().toLowerCase() === 'true',
  appMode: (env.VITE_APP_MODE?.trim().toLowerCase() || 'runtime') as 'runtime' | 'portal',
  localRuntimeUrl: (env.VITE_LOCAL_RUNTIME_URL?.trim() || 'http://127.0.0.1:5173').replace(/\/+$/, ''),
  requireAuth:
    (env.VITE_REQUIRE_AUTH?.trim().toLowerCase() === 'true')
    || ((env.VITE_REQUIRE_AUTH?.trim() ?? '') === '' && (env.VITE_APP_MODE?.trim().toLowerCase() || 'runtime') === 'portal'),
};
