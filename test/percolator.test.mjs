import test from 'node:test';
import assert from 'node:assert/strict';
import { createPercolator, PERCOLATOR_READ_TOOLS } from '../src/percolator.mjs';
import { PROTECTED_TOOLS } from '../src/mcp.mjs';

test('read catalog is exactly the five venue reads', () => {
  assert.deepEqual([...PERCOLATOR_READ_TOOLS].sort(), [
    'percolator_detect_venue',
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
