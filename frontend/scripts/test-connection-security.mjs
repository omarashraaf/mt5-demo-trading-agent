import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const connectionSource = readFileSync(resolve(root, 'src/pages/Connection.tsx'), 'utf8');
const connectionBarSource = readFileSync(resolve(root, 'src/components/ConnectionBar.tsx'), 'utf8');
const apiSource = readFileSync(resolve(root, 'src/utils/api.ts'), 'utf8');

for (const forbidden of [
  'setPassword',
  'rememberCredentials',
  'Save account for quick auto-connect',
  'Saved Accounts',
  'Your MT5 password',
  'Account Number',
]) {
  assert(!connectionSource.includes(forbidden), `Connection page must not contain ${forbidden}`);
}

assert(!apiSource.includes('password?: string'), 'connect API type must not accept broker passwords');
assert(!apiSource.includes('account?: number'), 'connect API type must not accept broker account numbers');
assert(!apiSource.includes('server?: string'), 'connect API type must not accept broker server names');
assert(!apiSource.includes('saveCredentials:'), 'frontend API must not expose broker credential saving');
assert(!apiSource.includes('getCredentials:'), 'frontend API must not expose broker credential loading');
assert(!apiSource.includes('autoConnect:'), 'frontend API must not expose saved broker credential auto-connect');
assert(connectionSource.includes('api.connect({'), 'Connection page should use local terminal connect');
assert(!connectionSource.includes('api.autoConnect'), 'Connection page should not use saved credential auto-connect');
assert(!connectionBarSource.includes('api.getCredentials'), 'Connection bar should not load saved broker credentials');
assert(!connectionBarSource.includes('api.autoConnect'), 'Connection bar quick connect should use local terminal connect');
assert(connectionSource.includes('Interactive Brokers (local TWS / IB Gateway)'), 'Connection page should include IBKR local login option');
assert(connectionSource.includes('TradeStation (browser authorization)'), 'Connection page should include TradeStation browser auth option');
assert(connectionSource.includes('TWS / Gateway host'), 'Connection page should connect to local IBKR TWS/Gateway host');
assert(connectionSource.includes('Open TradeStation Login'), 'Connection page should open TradeStation-owned login');

console.log('connection security tests passed');
