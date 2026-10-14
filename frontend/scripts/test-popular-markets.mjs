import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = resolve(root, '.tmp/popular-market-test');
mkdirSync(outDir, { recursive: true });

const tscArgs = [
    'tsc',
    'src/utils/popularMarkets.ts',
    '--target',
    'ES2020',
    '--module',
    'CommonJS',
    '--moduleResolution',
    'node',
    '--skipLibCheck',
    '--strict',
    '--esModuleInterop',
    '--outDir',
    outDir,
];

execFileSync(
  process.platform === 'win32' ? 'cmd.exe' : 'npx',
  process.platform === 'win32' ? ['/c', 'npx', ...tscArgs] : tscArgs,
  { cwd: root, stdio: 'inherit' },
);

const {
  detectRuntimeCountryCode,
  extractAvailableMarketRows,
  pickPopularSymbolsForCountry,
  popularMarketLabelForCountry,
  selectAllMarketSymbols,
} = await import(pathToFileURL(resolve(outDir, 'popularMarkets.js')).href);

function categoryForSymbol(name) {
  if (['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'USDCAD'].includes(name)) return 'Forex';
  if (['US500', 'US100', 'US30', 'GER40', 'UK100'].includes(name)) return 'Indices';
  if (['XAUUSD', 'XAGUSD', 'WTI', 'BRENT'].includes(name)) return 'Commodities';
  if (['BTCUSD', 'ETHUSD'].includes(name)) return 'Crypto';
  return 'Stocks';
}

const rows = [
  'XAUUSD', 'XAGUSD', 'US500', 'US100', 'US30', 'GER40', 'UK100',
  'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'USDCAD',
  'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'JPM', 'XOM',
  'BTCUSD', 'ETHUSD', 'WTI', 'BRENT',
].map((name) => ({
  name,
  category: categoryForSymbol(name),
  bid: 100,
  ask: 101,
  trade_enabled: true,
}));

assert.equal(detectRuntimeCountryCode({ locale: 'en-US', timeZone: 'America/New_York' }), 'US');
assert.equal(detectRuntimeCountryCode({ locale: 'ar-EG', timeZone: 'Africa/Cairo' }), 'EG');

const us = pickPopularSymbolsForCountry(rows, 'US');
assert.deepEqual(us.slice(0, 5), ['US500', 'US100', 'US30', 'AAPL', 'MSFT']);
assert(us.includes('BTCUSD'));
assert(!us.slice(0, 8).includes('GER40'));

const eg = pickPopularSymbolsForCountry(rows, 'EG');
assert.deepEqual(eg.slice(0, 5), ['XAUUSD', 'XAGUSD', 'US30', 'US100', 'US500']);
assert(eg.includes('GER40'));
const egCategories = new Set(eg.map((symbol) => categoryForSymbol(symbol)));
assert.deepEqual(
  Array.from(egCategories).sort(),
  ['Commodities', 'Crypto', 'Forex', 'Indices', 'Stocks'],
);

const zeroQuoteRows = [
  { name: 'XAUUSD', category: 'Commodities', bid: 2400, ask: 2401, trade_enabled: true },
  { name: 'AAPL', category: 'Stocks', bid: 0, ask: 0, trade_enabled: true },
  { name: 'MSFT', category: 'Stocks', bid: 0, ask: 0, trade_enabled: true },
  { name: 'US500', category: 'Indices', bid: 0, ask: 0, trade_enabled: true },
  { name: 'US100', category: 'Indices', bid: 0, ask: 0, trade_enabled: true },
  { name: 'EURUSD', category: 'Forex', bid: 0, ask: 0, trade_enabled: true },
];
const zeroQuoteUs = pickPopularSymbolsForCountry(zeroQuoteRows, 'US');
assert.deepEqual(zeroQuoteUs.slice(0, 5), ['US500', 'US100', 'AAPL', 'MSFT', 'XAUUSD']);
assert(zeroQuoteUs.includes('EURUSD'));

const imbalancedRows = [
  { name: 'XAUUSD', category: 'Commodities', bid: 2400, ask: 2401, trade_enabled: true },
  { name: 'XAGUSD', category: 'Commodities', bid: 30, ask: 30.1, trade_enabled: true },
  { name: 'WTI', category: 'Commodities', bid: 76, ask: 76.1, trade_enabled: true },
  { name: 'BRENT', category: 'Commodities', bid: 80, ask: 80.1, trade_enabled: true },
  { name: 'EURUSD', category: 'Forex', bid: 1.1, ask: 1.1002, trade_enabled: true },
  { name: 'US500', category: 'Indices', bid: 5000, ask: 5001, trade_enabled: true },
  { name: 'AAPL', category: 'Stocks', bid: 220, ask: 220.1, trade_enabled: true },
  { name: 'BTCUSD', category: 'Crypto', bid: 65000, ask: 65010, trade_enabled: true },
];
const aePopular = pickPopularSymbolsForCountry(imbalancedRows, 'AE');
const categoryBySymbol = new Map(imbalancedRows.map((row) => [row.name, row.category]));
const aeCategories = new Set(aePopular.map((symbol) => categoryBySymbol.get(symbol)).filter(Boolean));
assert.deepEqual(
  Array.from(aeCategories).sort(),
  ['Commodities', 'Crypto', 'Forex', 'Indices', 'Stocks'],
);

assert.equal(popularMarketLabelForCountry('US'), 'Popular in United States');
assert.equal(popularMarketLabelForCountry('EG'), 'Popular in Egypt');

assert.deepEqual(
  selectAllMarketSymbols([
    { name: 'XAUUSD', category: 'Commodities' },
    { name: 'GOLD', category: 'Commodities' },
    { name: 'EURUSD', category: 'Forex' },
    { name: 'US500', category: 'Indices' },
    { name: '', category: 'Crypto' },
    { name: 'BTCUSD', category: 'Crypto' },
  ]),
  ['XAUUSD', 'EURUSD', 'US500', 'BTCUSD'],
);

const flatOnlyCatalogRows = extractAvailableMarketRows({
  total: 5,
  symbols: [
    { name: 'XAUUSD', category: 'Commodities' },
    { name: 'EURUSD', category: 'Forex' },
    { name: 'US500', category: 'Indices' },
    { name: 'AAPL', category: 'Stocks' },
    { name: 'BTCUSD', category: 'Crypto' },
  ],
});
assert.deepEqual(
  selectAllMarketSymbols(flatOnlyCatalogRows),
  ['XAUUSD', 'EURUSD', 'US500', 'AAPL', 'BTCUSD'],
);

console.log('popular market utility tests passed');
