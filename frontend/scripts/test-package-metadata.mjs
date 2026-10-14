import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packageJson = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const lockJson = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8'));
const rootPackage = lockJson.packages?.[''] || {};

assert.equal(lockJson.name, packageJson.name, 'package-lock root name must match package.json');
assert.equal(rootPackage.name, packageJson.name, 'package-lock package name must match package.json');
assert.equal(lockJson.version, packageJson.version, 'package-lock root version must match package.json');
assert.equal(rootPackage.version, packageJson.version, 'package-lock package version must match package.json');
assert.ok(!/demo/i.test(lockJson.name), 'package metadata must not advertise demo mode');

console.log('package metadata tests passed');
