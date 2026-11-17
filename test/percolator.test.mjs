import test from 'node:test';
import assert from 'node:assert/strict';
import { createPercolator, PERCOLATOR_READ_TOOLS, PERCOLATOR_WRITE_TOOLS, writesEnabled } from '../src/percolator.mjs';
import { PROTECTED_TOOLS } from '../src/mcp.mjs';

test('read catalog is exactly the five venue reads plus the three GM8z mainnet reads', () => {
  assert.deepEqual([...PERCOLATOR_READ_TOOLS].sort(), [
    'percolator_detect_venue',
    'percolator_gm8z_list_markets',
    'percolator_gm8z_oracle_price',
    'percolator_gm8z_read_market',
    'percolator_list_markets',
    'percolator_oracle_price',
    'percolator_quote',
    'percolator_read_market',
  ]);
  for (const n of PERCOLATOR_READ_TOOLS) assert.equal(PROTECTED_TOOLS.has(n), false);
});

test('createPercolator callImpl short-circuit (no network)', async () => {
  const p = createPercolator({
    callImpl: async (name, args) => ({ ok: true, tool: name, args }),
  });
  const r = await p.call('percolator_quote', { slab: 'abc', size: 1 });
  assert.equal(r.ok, true);
  assert.equal(r.tool, 'percolator_quote');
  assert.equal(p.info().writes, 0);
  assert.equal(p.info().tradingEnabled, false);
});


test('writes disabled by default; enabled by PERCOLATOR_MAINNET_WRITES_ENABLED', () => {
  assert.equal(writesEnabled({}), false);
  assert.equal(writesEnabled({ PERCOLATOR_MAINNET_WRITES_ENABLED: 'true' }), true);
  const off = createPercolator({ env: {}, callImpl: async () => ({ ok: true }) });
  assert.equal(off.info().writes, 0);
  const on = createPercolator({ env: { PERCOLATOR_MAINNET_WRITES_ENABLED: 'true' }, callImpl: async () => ({ ok: true }) });
  assert.equal(on.info().writes, PERCOLATOR_WRITE_TOOLS.length);
  for (const n of PERCOLATOR_WRITE_TOOLS) assert.ok(on.names.has(n));
});
