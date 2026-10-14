import assert from 'node:assert/strict';
import { existsSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const outDir = resolve('frontend/.tmp/trade-target-tests');
if (existsSync(outDir)) rmSync(outDir, { recursive: true, force: true });

const tscArgs = [
  'src/utils/tradeTargets.ts',
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

const { dollarTargetToPrice } = await import(pathToFileURL(resolve(outDir, 'tradeTargets.js')).href);

assert.equal(dollarTargetToPrice({
  action: 'BUY',
  dollarAmount: 50,
  volume: 1,
  contractSize: 100,
  referencePrice: 10,
  isStopLoss: true,
}), 9.5);
assert.equal(dollarTargetToPrice({
  action: 'BUY',
  dollarAmount: 50,
  volume: 1,
  contractSize: 100,
  referencePrice: 10,
  isStopLoss: false,
}), 10.5);
assert.equal(dollarTargetToPrice({
  action: 'SELL',
  dollarAmount: 50,
  volume: 1,
  contractSize: 100,
  referencePrice: 10,
  isStopLoss: true,
}), 10.5);
assert.equal(dollarTargetToPrice({
  action: 'SELL',
  dollarAmount: 50,
  volume: 1,
  contractSize: 100,
  referencePrice: 10,
  isStopLoss: false,
}), 9.5);
assert.equal(dollarTargetToPrice({
  action: 'SELL',
  dollarAmount: 0,
  volume: 1,
  contractSize: 100,
  referencePrice: 10,
  isStopLoss: true,
}), null);

assert.equal(dollarTargetToPrice({
  action: 'BUY',
  dollarAmount: 50,
  volume: 2,
  contractSize: 1,
  symbolInfo: { trade_tick_value: 1, trade_tick_size: 0.01 },
  referencePrice: 10,
  isStopLoss: true,
}), 9.75);

console.log('trade target conversion tests passed');
