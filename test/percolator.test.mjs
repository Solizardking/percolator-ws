import test from 'node:test';
import assert from 'node:assert/strict';
import { createPercolator, PERCOLATOR_READ_TOOLS, PERCOLATOR_WRITE_TOOLS, writesEnabled, mainnetWritesEnabled } from '../src/percolator.mjs';
import { PROTECTED_TOOLS } from '../src/mcp.mjs';

test('read catalog is the six venue reads (incl. positions) plus the three GM8z mainnet reads', () => {
  assert.deepEqual([...PERCOLATOR_READ_TOOLS].sort(), [
    'percolator_detect_venue',
    'percolator_gm8z_list_markets',
    'percolator_gm8z_oracle_price',
    'percolator_gm8z_read_market',
    'percolator_list_markets',
    'percolator_oracle_price',
    'percolator_positions',
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


test('writes off by default; only PERCOLATOR_WRITES_ENABLED registers the unsigned builders', () => {
  assert.equal(writesEnabled({}), false);
  // The mainnet flag alone no longer registers write tools.
  assert.equal(writesEnabled({ PERCOLATOR_MAINNET_WRITES_ENABLED: 'true' }), false);
  assert.equal(writesEnabled({ PERCOLATOR_WRITES_ENABLED: 'true' }), true);
  assert.equal(mainnetWritesEnabled({ PERCOLATOR_MAINNET_WRITES_ENABLED: 'true' }), false);
  assert.equal(mainnetWritesEnabled({ PERCOLATOR_WRITES_ENABLED: 'true', PERCOLATOR_MAINNET_WRITES_ENABLED: 'true' }), true);
  const off = createPercolator({ env: {}, callImpl: async () => ({ ok: true }) });
  assert.equal(off.info().writes, 0);
  const on = createPercolator({ env: { PERCOLATOR_WRITES_ENABLED: 'true' }, callImpl: async () => ({ ok: true }) });
  assert.equal(on.info().writes, PERCOLATOR_WRITE_TOOLS.length);
  assert.equal(on.info().mainnetWritesEnabled, false);
  for (const n of PERCOLATOR_WRITE_TOOLS) assert.ok(on.names.has(n));
  for (const t of on.tools.filter((x) => PERCOLATOR_WRITE_TOOLS.includes(x.name))) {
    assert.match(t.description, /UNSIGNED/);
    assert.equal(t.annotations.readOnlyHint, false);
  }
});

test('positions and open_position schemas', () => {
  const on = createPercolator({ env: { PERCOLATOR_WRITES_ENABLED: 'true' }, callImpl: async () => ({ ok: true }) });
  const pos = on.tools.find((t) => t.name === 'percolator_positions');
  assert.deepEqual(pos.inputSchema.required, ['owner']);
  assert.equal(pos.annotations.readOnlyHint, true);
  const open = on.tools.find((t) => t.name === 'percolator_open_position');
  assert.deepEqual(open.inputSchema.required, ['slab', 'owner', 'size']);
  assert.ok(open.inputSchema.properties.deposit_amount);
  const quote = on.tools.find((t) => t.name === 'percolator_quote');
  assert.ok(quote.inputSchema.properties.collateral);
});
