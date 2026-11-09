// Percolator read tools: proxy to the local Python FastMCP (imperial-perps percolator_venue).
// Writes are never registered. Mockable via callImpl for tests.
export const PERCOLATOR_READ_TOOLS = [
  'percolator_list_markets',
  'percolator_read_market',
  'percolator_oracle_price',
  'percolator_quote',
  'percolator_detect_venue',
];

export const PERCOLATOR_WRITE_TOOLS = [
  'percolator_create_user_market',
  'percolator_deposit',
  'percolator_withdraw',
  'percolator_open_position',
  'percolator_close_position',
  'percolator_update_oracle',
];

export function writesEnabled(env = process.env) {
  const v = String(env.PERCOLATOR_MAINNET_WRITES_ENABLED || env.PERCOLATOR_WRITES_ENABLED || '').toLowerCase();
  return v === '1' || v === 'true' || v === 'yes' || v === 'on';
}

const SCHEMAS = {
  percolator_list_markets: {
    type: 'object',
    properties: {
      cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'devnet' },
      limit: { type: 'integer', minimum: 1, maximum: 200, default: 50 },
    },
  },
  percolator_read_market: {
    type: 'object',
    required: ['slab'],
    properties: {
      slab: { type: 'string' },
      cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'devnet' },
      include_accounts: { type: 'boolean', default: false },
    },
  },
  percolator_oracle_price: {
    type: 'object',
    required: ['slab'],
    properties: {
      slab: { type: 'string' },
      cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'devnet' },
    },
  },
  percolator_quote: {
    type: 'object',
    required: ['slab', 'size'],
    properties: {
      slab: { type: 'string' },
      size: { type: 'integer', description: 'Base units; +long / -short' },
      cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'devnet' },
    },
  },
  percolator_detect_venue: {
    type: 'object',
    required: ['mint'],
    properties: {
      mint: { type: 'string' },
      cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'mainnet' },
      min_liquidity: { type: 'integer', default: 1_000_000_000 },
    },
  },
  percolator_create_user_market: {
    type: 'object',
    required: ['mint', 'collateral_mint', 'creator', 'bond'],
    properties: {
      mint: { type: 'string' }, collateral_mint: { type: 'string' }, creator: { type: 'string' },
      bond: { type: 'integer' }, cluster: { type: 'string', enum: ['devnet', 'mainnet', 'localnet'], default: 'devnet' },
      confirm: { type: 'boolean', default: false }, confirm_token: { type: 'string', default: '' },
    },
  },
  percolator_deposit: {
    type: 'object', required: ['slab', 'owner', 'amount'],
    properties: {
      slab: { type: 'string' }, owner: { type: 'string' }, amount: { type: 'integer' },
      cluster: { type: 'string', default: 'devnet' }, confirm: { type: 'boolean', default: false },
      confirm_token: { type: 'string', default: '' }, init: { type: 'boolean', default: false },
    },
  },
  percolator_withdraw: {
    type: 'object', required: ['slab', 'owner', 'amount'],
    properties: {
      slab: { type: 'string' }, owner: { type: 'string' }, amount: { type: 'integer' },
      cluster: { type: 'string', default: 'devnet' }, confirm: { type: 'boolean', default: false },
      confirm_token: { type: 'string', default: '' },
    },
  },
  percolator_open_position: {
    type: 'object', required: ['slab', 'owner', 'lp_owner', 'size'],
    properties: {
      slab: { type: 'string' }, owner: { type: 'string' }, lp_owner: { type: 'string' }, size: { type: 'integer' },
      cluster: { type: 'string', default: 'devnet' }, confirm: { type: 'boolean', default: false },
      confirm_token: { type: 'string', default: '' },
    },
  },
  percolator_close_position: {
    type: 'object', required: ['slab', 'owner', 'lp_owner'],
    properties: {
      slab: { type: 'string' }, owner: { type: 'string' }, lp_owner: { type: 'string' },
      cluster: { type: 'string', default: 'devnet' }, confirm: { type: 'boolean', default: false },
      confirm_token: { type: 'string', default: '' },
    },
  },
  percolator_update_oracle: {
    type: 'object', required: ['slab', 'keeper'],
    properties: {
      slab: { type: 'string' }, keeper: { type: 'string' },
      cluster: { type: 'string', default: 'devnet' }, confirm: { type: 'boolean', default: false },
      confirm_token: { type: 'string', default: '' },
    },
  },
};

const DESCRIPTIONS = {
  percolator_list_markets: 'List CLAWD Percolator perps markets (slabs) on a cluster (default devnet). Read-only.',
  percolator_read_market: 'Decode a CLAWD Percolator slab: config, pool-oracle state, engine totals.',
  percolator_oracle_price: 'Current index price of a market (pool EMA or admin-pushed).',
  percolator_quote: 'Quote a position (size in base units, +long/-short): notional, margin, fee, leverage cap.',
  percolator_detect_venue: 'Detect pump.fun / PumpSwap / LaunchLab pool for a mint and perps eligibility. Read-only.',
  percolator_create_user_market: 'Create a permissionless perps market (unsigned on mainnet when enabled). UNAUDITED.',
  percolator_deposit: 'Deposit collateral. Mainnet: unsigned tx for user wallet when enabled. UNAUDITED.',
  percolator_withdraw: 'Withdraw collateral. Mainnet: unsigned tx for user wallet when enabled. UNAUDITED.',
  percolator_open_position: 'Open/increase position. Mainnet: unsigned tx when enabled. UNAUDITED.',
  percolator_close_position: 'Close position. Mainnet: unsigned tx when enabled. UNAUDITED.',
  percolator_update_oracle: 'Update pool oracle EMA. Mainnet: unsigned tx when enabled. UNAUDITED.',
};

export function createPercolator({ env = process.env, fetchImpl = fetch, callImpl = null } = {}) {
  const base = (env.PERCOLATOR_PY_MCP_URL || 'http://127.0.0.1:8791/mcp').replace(/\/$/, '');
  const enabledWrites = writesEnabled(env);
  const catalog = [...PERCOLATOR_READ_TOOLS, ...(enabledWrites ? PERCOLATOR_WRITE_TOOLS : [])];
  const tools = catalog.map((name) => ({
    name,
    description: DESCRIPTIONS[name],
    inputSchema: SCHEMAS[name],
    annotations: PERCOLATOR_WRITE_TOOLS.includes(name)
      ? { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true }
      : { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }));
  const names = new Set(catalog);

  async function proxyCall(name, args) {
    if (callImpl) return callImpl(name, args);
    const body = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args || {} } };
    const r = await fetchImpl(base, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
      body: JSON.stringify(body),
    });
    const text = await r.text();
    let payload;
    try { payload = JSON.parse(text); } catch {
      // streamable HTTP may return SSE; scrape last data: JSON line
      const line = text.split('\n').filter((l) => l.startsWith('data:')).pop();
      payload = line ? JSON.parse(line.slice(5).trim()) : { error: { message: text.slice(0, 200) } };
    }
    if (payload?.error) return { ok: false, error: payload.error.message || String(payload.error), tool: name };
    const sc = payload?.result?.structuredContent;
    if (sc) return sc.result ?? sc;
    const t = payload?.result?.content?.[0]?.text;
    if (typeof t === 'string') {
      try { return JSON.parse(t); } catch { return { ok: true, text: t }; }
    }
    return payload?.result ?? { ok: false, error: 'empty_upstream', tool: name };
  }

  return {
    tools,
    names,
    call: proxyCall,
    info: () => ({
      reads: PERCOLATOR_READ_TOOLS.length,
      writes: enabledWrites ? PERCOLATOR_WRITE_TOOLS.length : 0,
      writesEnabled: enabledWrites,
      pyMcp: base,
      tradingEnabled: false,
      keypairSet: false,
      mainnetWritesEnabled: writesEnabled(env),
    }),
  };
}
