import assert from 'node:assert/strict';
import { existsSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const outDir = resolve('frontend/.tmp/position-values-tests');
if (existsSync(outDir)) rmSync(outDir, { recursive: true, force: true });

const tscArgs = [
  'src/utils/positionValues.ts',
  '--target',
  'ES2020',
  '--module',
  'CommonJS',
  '--moduleResolution',
  'node',
  '--skipLibCheck',
  '--strict',
  '--outDir',
  outDir,
];

const tsc = process.platform === 'win32'
  ? spawnSync('cmd', ['/c', 'npx', 'tsc', ...tscArgs], { cwd: resolve('frontend'), stdio: 'inherit' })
  : spawnSync('npx', ['tsc', ...tscArgs], { cwd: resolve('frontend'), stdio: 'inherit' });
if (tsc.status !== 0) process.exit(tsc.status || 1);

const {
  commentStartedAmount,
  positionStartedAmount,
  positionMarginUsedAmount,
} = await import(pathToFileURL(resolve(outDir, 'positionValues.js')).href);

assert.equal(commentStartedAmount('TA:$5000 SLA250 TPA600'), 5000);
assert.equal(commentStartedAmount('TA5000'), 5000);
assert.equal(commentStartedAmount('no amount'), 0);

const leveragedPosition = {
  volume: 1,
  price_open: 2500,
  margin_used_usd: null,
  estimated_margin: 1250,
  started_with_usd: 5000,
  entry_market_value_usd: 100000,
  comment: 'TA:$5000',
};

assert.equal(positionStartedAmount(leveragedPosition, 100, 40), 1250);
assert.equal(positionMarginUsedAmount(leveragedPosition, 100, 40), 1250);

const noMarginButBudget = {
  volume: 1,
  price_open: 2500,
  margin_used_usd: null,
  estimated_margin: null,
  started_with_usd: 5000,
  entry_market_value_usd: 100000,
  comment: '',
};

assert.equal(positionStartedAmount(noMarginButBudget, 100, 40), 5000);
assert.equal(positionMarginUsedAmount(noMarginButBudget, 100, 40), 1000);

const notionalOnly = {
  volume: 1,
  price_open: 2500,
  margin_used_usd: null,
  estimated_margin: null,
  started_with_usd: null,
  entry_market_value_usd: 100000,
  comment: '',
};

assert.equal(positionStartedAmount(notionalOnly, 100, null), 1000);
assert.equal(positionMarginUsedAmount(notionalOnly, 100, null), 1000);

console.log('position value utility tests passed');
