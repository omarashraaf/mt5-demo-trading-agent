import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = resolve(root, '.tmp/money-conversion-test');
mkdirSync(outDir, { recursive: true });

const tscArgs = [
  'tsc',
  'src/utils/moneyConversion.ts',
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
  priceDistanceToUsd,
  usdPerPriceDistance,
} = await import(pathToFileURL(resolve(outDir, 'moneyConversion.js')).href);

const silverInfo = {
  trade_contract_size: 5000,
  trade_tick_value: 0.5,
  trade_tick_size: 0.001,
  point: 0.001,
};

assert.equal(usdPerPriceDistance({ symbolInfo: silverInfo, volume: 1.21 }), 6050);
assert.equal(
  Math.round(priceDistanceToUsd({
    symbolInfo: silverInfo,
    entryPrice: 74.858,
    targetPrice: 76.3,
    volume: 1.21,
  }) * 100) / 100,
  8724.1,
);

assert.equal(usdPerPriceDistance({ symbolInfo: { trade_tick_value: 1, trade_tick_size: 0.01 }, volume: 2 }), 200);
assert.equal(priceDistanceToUsd({ symbolInfo: {}, entryPrice: 10, targetPrice: 11, volume: 1 }), null);
assert.equal(priceDistanceToUsd({ symbolInfo: silverInfo, entryPrice: 0, targetPrice: 11, volume: 1 }), null);

console.log('money conversion utility tests passed');
