import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = resolve(root, '.tmp/live-tick-test');
mkdirSync(outDir, { recursive: true });

execFileSync(
  process.platform === 'win32' ? 'cmd.exe' : 'npx',
  process.platform === 'win32'
    ? ['/c', 'npx', 'tsc', 'src/utils/liveTicks.ts', '--target', 'ES2020', '--module', 'CommonJS', '--moduleResolution', 'node', '--skipLibCheck', '--strict', '--outDir', outDir]
    : ['tsc', 'src/utils/liveTicks.ts', '--target', 'ES2020', '--module', 'CommonJS', '--moduleResolution', 'node', '--skipLibCheck', '--strict', '--outDir', outDir],
  { cwd: root, stdio: 'inherit' },
);

const { isFreshLiveTick, mergeLiveTicks, tickTimestampMs } = await import(pathToFileURL(resolve(outDir, 'liveTicks.js')).href);

const nowMs = 1_800_000_000_000;
const fresh = { symbol: 'XAUUSD', bid: 2400, ask: 2400.5, spread: 5, time: Math.floor(nowMs / 1000), time_msc: nowMs };
const slowSession = { symbol: 'MSFT', bid: 399, ask: 399.4, spread: 4, time: Math.floor((nowMs - 240_000) / 1000), time_msc: nowMs - 240_000 };
const stale = { symbol: 'TSLA', bid: 180, ask: 180.2, spread: 2, time: Math.floor((nowMs - 960_000) / 1000), time_msc: nowMs - 960_000 };
const invalid = { symbol: 'AAPL', bid: 0, ask: 0, spread: 0, time: Math.floor(nowMs / 1000), time_msc: nowMs };
const undated = { symbol: 'EURUSD', bid: 1.08, ask: 1.0801, spread: 1, time: 0, time_msc: 0 };

assert.equal(tickTimestampMs(fresh), nowMs);
assert.equal(isFreshLiveTick(fresh, nowMs), true);
assert.equal(isFreshLiveTick(slowSession, nowMs), true);
assert.equal(isFreshLiveTick(stale, nowMs), false);
assert.equal(isFreshLiveTick(invalid, nowMs), false);
assert.equal(isFreshLiveTick(undated, nowMs), false);

const retained = mergeLiveTicks({ XAUUSD: fresh }, {}, nowMs);
assert.deepEqual(Object.keys(retained), ['XAUUSD']);

const compacted = mergeLiveTicks({ MSFT: stale, AAPL: invalid, EURUSD: undated }, { XAUUSD: fresh }, nowMs);
assert.deepEqual(Object.keys(compacted), ['XAUUSD']);

console.log('live tick utility tests passed');
