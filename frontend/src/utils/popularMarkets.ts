export type PopularMarketRow = {
  name: string;
  description?: string;
  category?: string;
  bid?: number;
  ask?: number;
  spread?: number;
  visible?: boolean;
  trade_enabled?: boolean;
};

export type AvailableMarketCatalog = {
  symbols?: PopularMarketRow[];
  categories?: Record<string, PopularMarketRow[]>;
};

const SUPPORTED_MARKET_CATEGORIES = ['Forex', 'Indices', 'Commodities', 'Stocks', 'Crypto'];

const COUNTRY_NAMES: Record<string, string> = {
  AE: 'United Arab Emirates',
  EG: 'Egypt',
  GB: 'United Kingdom',
  SA: 'Saudi Arabia',
  US: 'United States',
};

const TIMEZONE_COUNTRY_PREFIXES: Array<[string, string]> = [
  ['America/', 'US'],
  ['Africa/Cairo', 'EG'],
  ['Europe/London', 'GB'],
  ['Asia/Dubai', 'AE'],
  ['Asia/Riyadh', 'SA'],
];

const COUNTRY_MARKET_HINTS: Record<string, string[]> = {
  US: [
    'US500', 'SP500', 'SPY',
    'US100', 'NAS100', 'QQQ',
    'US30', 'DJ30', 'DIA',
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'JPM', 'XOM',
    'BTCUSD', 'ETHUSD',
    'XAUUSD', 'GOLD',
    'WTI', 'USOIL',
    'EURUSD', 'GBPUSD', 'USDJPY',
  ],
  EG: [
    'XAUUSD', 'GOLD',
    'XAGUSD', 'SILVER',
    'US30', 'DJ30',
    'US100', 'NAS100',
    'US500', 'SP500',
    'GER40', 'DAX',
    'UK100', 'FTSE',
    'WTI', 'BRENT',
    'EURUSD', 'GBPUSD', 'USDJPY',
    'AAPL', 'MSFT', 'NVDA', 'TSLA',
  ],
  GB: [
    'UK100', 'FTSE',
    'GER40', 'DAX',
    'US500', 'US100', 'US30',
    'GBPUSD', 'EURGBP', 'EURUSD',
    'XAUUSD', 'XAGUSD',
    'BRENT', 'WTI',
    'AAPL', 'MSFT', 'NVDA',
  ],
  AE: [
    'XAUUSD', 'XAGUSD',
    'US500', 'US100', 'US30',
    'GER40', 'UK100',
    'WTI', 'BRENT',
    'EURUSD', 'GBPUSD', 'USDJPY',
    'BTCUSD', 'ETHUSD',
    'AAPL', 'MSFT', 'NVDA',
  ],
  SA: [
    'XAUUSD', 'XAGUSD',
    'US500', 'US100', 'US30',
    'WTI', 'BRENT',
    'EURUSD', 'GBPUSD', 'USDJPY',
    'BTCUSD', 'ETHUSD',
    'AAPL', 'MSFT', 'NVDA',
  ],
};

const GLOBAL_MARKET_HINTS = [
  'US500', 'US100', 'US30',
  'XAUUSD', 'GOLD',
  'XAGUSD', 'SILVER',
  'WTI', 'BRENT',
  'EURUSD', 'GBPUSD', 'USDJPY',
  'AAPL', 'MSFT', 'NVDA',
  'BTCUSD', 'ETHUSD',
];

export function normalizeMarketCategory(category: string | null | undefined): string {
  const raw = String(category || '').trim();
  const lower = raw.toLowerCase();
  if (lower.includes('forex') || lower.includes('currency') || lower.includes('fx')) return 'Forex';
  if (lower.includes('index') || lower.includes('indices')) return 'Indices';
  if (lower.includes('commod') || lower.includes('metal') || lower.includes('energy')) return 'Commodities';
  if (lower.includes('stock') || lower.includes('share') || lower.includes('equity') || lower.includes('etf')) return 'Stocks';
  if (lower.includes('crypto') || lower.includes('coin')) return 'Crypto';
  return raw || 'Other';
}

export function marketIdentityKey(symbol: string | null | undefined): string {
  const key = String(symbol || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  const aliases: Record<string, string> = {
    GOLD: 'XAUUSD',
    XAUUSD: 'XAUUSD',
    SILVER: 'XAGUSD',
    XAGUSD: 'XAGUSD',
    SP500: 'US500',
    SPY: 'US500',
    US500: 'US500',
    NAS100: 'US100',
    QQQ: 'US100',
    US100: 'US100',
    DJ30: 'US30',
    DIA: 'US30',
    US30: 'US30',
    DAX: 'GER40',
    GER40: 'GER40',
    FTSE: 'UK100',
    FTSE100: 'UK100',
    UK100: 'UK100',
    USOIL: 'WTI',
    XTIUSD: 'WTI',
    WTI: 'WTI',
    UKOIL: 'BRENT',
    XBRUSD: 'BRENT',
    BRENT: 'BRENT',
  };
  return aliases[key] || key;
}

export function hasUsableBrokerQuote(row: PopularMarketRow): boolean {
  const bid = Number(row.bid || 0);
  const ask = Number(row.ask || 0);
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || ask < bid) {
    return false;
  }
  const mid = (bid + ask) / 2;
  if (mid <= 0) return false;
  return Math.max(0, ask - bid) / mid <= 0.05;
}

export function detectRuntimeCountryCode(input?: {
  locale?: string | null;
  timeZone?: string | null;
}): string {
  const locale = String(input?.locale || '').trim();
  const timeZone = String(input?.timeZone || '').trim();
  for (const [prefix, country] of TIMEZONE_COUNTRY_PREFIXES) {
    if (timeZone === prefix || timeZone.startsWith(prefix)) return country;
  }
  const localeRegion = locale.match(/[-_]([A-Za-z]{2})(?:\b|$)/)?.[1]?.toUpperCase();
  return localeRegion || 'US';
}

export function detectBrowserCountryCode(): string {
  const intl: Intl.ResolvedDateTimeFormatOptions | null = typeof Intl !== 'undefined'
    ? Intl.DateTimeFormat().resolvedOptions()
    : null;
  const nav = typeof navigator !== 'undefined' ? navigator : undefined;
  const locale = nav?.language || (Array.isArray(nav?.languages) ? nav?.languages[0] : '') || '';
  return detectRuntimeCountryCode({
    locale,
    timeZone: typeof intl?.timeZone === 'string' ? intl.timeZone : '',
  });
}

export function popularMarketLabelForCountry(countryCode: string | null | undefined): string {
  const code = String(countryCode || '').trim().toUpperCase() || 'US';
  return `Popular in ${COUNTRY_NAMES[code] || code}`;
}

export function pickPopularSymbolsForCountry(
  allSymbols: PopularMarketRow[],
  countryCode: string | null | undefined,
): string[] {
  const supportedSymbols = allSymbols.filter((row) => (
    SUPPORTED_MARKET_CATEGORIES.includes(normalizeMarketCategory(row.category))
    && row.trade_enabled !== false
  ));
  const quoteReadySymbols = supportedSymbols.filter(hasUsableBrokerQuote);
  const candidateSymbols = [
    ...quoteReadySymbols,
    ...supportedSymbols.filter((row) => !hasUsableBrokerQuote(row)),
  ];
  const uniqueByName = new Map<string, PopularMarketRow>();
  const uniqueByIdentity = new Map<string, PopularMarketRow>();
  for (const row of candidateSymbols) {
    const name = String(row.name || '').trim();
    if (!name) continue;
    if (!uniqueByName.has(name)) uniqueByName.set(name, row);
    const identity = marketIdentityKey(name);
    if (identity && !uniqueByIdentity.has(identity)) uniqueByIdentity.set(identity, row);
  }
  const names = Array.from(uniqueByName.keys());
  const code = String(countryCode || '').trim().toUpperCase();
  const hints = COUNTRY_MARKET_HINTS[code] || GLOBAL_MARKET_HINTS;

  const picked: string[] = [];
  for (const hint of hints) {
    const normalizedHint = marketIdentityKey(hint);
    const sym = names.find((name) => {
      const upper = String(name || '').trim().toUpperCase();
      return upper === hint || upper.includes(hint) || marketIdentityKey(name) === normalizedHint;
    });
    if (sym && !picked.includes(sym)) picked.push(sym);
  }

  const pickedCategories = new Set(
    picked
      .map((symbol) => uniqueByIdentity.get(marketIdentityKey(symbol)))
      .filter((row): row is PopularMarketRow => Boolean(row))
      .map((row) => normalizeMarketCategory(row.category)),
  );
  const availableCategories = SUPPORTED_MARKET_CATEGORIES.filter((categoryName) => (
    supportedSymbols.some((row) => normalizeMarketCategory(row.category) === categoryName)
  ));
  for (const categoryName of availableCategories) {
    if (pickedCategories.has(categoryName)) continue;
    const categoryCandidate = candidateSymbols.find((row) => (
      normalizeMarketCategory(row.category) === categoryName
      && !picked.includes(String(row.name || '').trim())
    ));
    const symbol = String(categoryCandidate?.name || '').trim();
    if (symbol) {
      picked.push(symbol);
      pickedCategories.add(categoryName);
    }
  }

  if (picked.length < 12) {
    const fallback = [...supportedSymbols]
      .sort((a, b) => {
        const aExecutable = hasUsableBrokerQuote(a);
        const bExecutable = hasUsableBrokerQuote(b);
        const aTradeEnabled = Boolean(a.trade_enabled);
        const bTradeEnabled = Boolean(b.trade_enabled);
        const aScore = (aExecutable ? 20 : 0) + (aTradeEnabled ? 10 : 0);
        const bScore = (bExecutable ? 20 : 0) + (bTradeEnabled ? 10 : 0);
        if (aScore !== bScore) return bScore - aScore;
        return Number(a.spread || 0) - Number(b.spread || 0);
      })
      .slice(0, 30)
      .map((s) => String(s.name || '').trim())
      .filter(Boolean);
    for (const sym of fallback) {
      if (!picked.includes(sym)) picked.push(sym);
      if (picked.length >= 20) break;
    }
  }

  return picked;
}

export function selectAllMarketSymbols(allSymbols: PopularMarketRow[]): string[] {
  const picked: string[] = [];
  const seenIdentities = new Set<string>();
  for (const row of allSymbols || []) {
    const name = String(row?.name || '').trim();
    if (!name) continue;
    const identity = marketIdentityKey(name);
    if (identity && seenIdentities.has(identity)) continue;
    if (identity) seenIdentities.add(identity);
    picked.push(name);
  }
  return picked;
}

export function extractAvailableMarketRows(catalog: AvailableMarketCatalog | null | undefined): PopularMarketRow[] {
  if (Array.isArray(catalog?.symbols) && catalog.symbols.length > 0) {
    return catalog.symbols;
  }
  if (!catalog?.categories || typeof catalog.categories !== 'object') {
    return [];
  }
  return Object.values(catalog.categories).flat().filter(Boolean);
}
