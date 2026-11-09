import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import crypto from 'node:crypto';
import bs58 from 'bs58';
import { createMcp, PROTECTED_TOOLS } from '../src/mcp.mjs';
import { createPercolator } from '../src/percolator.mjs';
import { gateMcp, siwsChallenge, siwsVerify } from '../src/mcp-access.mjs';
import { CLAWD_MINT, DEFAULT_PAY_TO, FREE_TOOLS } from '../src/mcp-public-gate.mjs';

const env = {
  MCP_AUTH_SECRET: 't'.repeat(32),
  SOLANA_RPC_URL: 'http://rpc.test',
  MCP_X402_FACILITATOR_URL: 'http://facilitator.test',
};

function keypair() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  const rawPub = publicKey.export({ type: 'spki', format: 'der' }).subarray(-32);
  const rawPriv = privateKey.export({ type: 'pkcs8', format: 'der' }).subarray(-32);
  const wallet = bs58.encode(rawPub);
  return {
    wallet,
    sign(message) {
      const sig = crypto.sign(null, Buffer.from(message), privateKey);
      return bs58.encode(sig);
    },
  };
}

const holder = keypair();
const nonHolder = keypair();

const fakeFetch = async (url, init = {}) => {
  const u = String(url);
  if (u === env.SOLANA_RPC_URL) {
    const body = JSON.parse(init.body || '{}');
    if (body.method === 'getTokenAccountsByOwner') {
      const owner = body.params?.[0];
      const amt = owner === holder.wallet ? '1000000' : '0';
      // Return both token program shapes the balance helper may query
      return Response.json({
        result: {
          value: amt === '0' ? [] : [{
            account: {
              owner: 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
              data: { parsed: { info: { tokenAmount: { amount: amt, decimals: 6 } } } },
            },
          }],
        },
      });
    }
    return Response.json({ result: null });
  }
  if (u.startsWith(env.MCP_X402_FACILITATOR_URL)) {
    if (u.endsWith('/verify')) return Response.json({ isValid: true, payer: nonHolder.wallet });
    if (u.endsWith('/settle')) return Response.json({ success: true, transaction: 'settled-sig' });
    return Response.json({});
  }
  return new Response('nope', { status: 404 });
};

async function withServer(fn) {
  const percolator = createPercolator({
    env,
    callImpl: async (name) => ({ ok: true, tool: name, markets: [], speech: 'mock' }),
  });
  const mcp = createMcp({ health: () => ({ status: 'ok' }), env, fetchImpl: fakeFetch, percolator });
  const srv = http.createServer(async (req, res) => {
    if (!(await mcp.http(req, res, new URL(req.url, 'http://x')))) { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  const post = async (path, body, headers = {}) => fetch(base + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json', ...headers },
    body: JSON.stringify(body),
  });
  try { return await fn(post, base); }
  finally { srv.close(); srv.closeAllConnections?.(); }
}

test('initialize and tools/list are free; read tools listed', () => withServer(async (post) => {
  const init = await (await post('/mcp', { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })).json();
  assert.equal(init.result.serverInfo.name, 'percolator-tech');
  const list = await (await post('/mcp', { jsonrpc: '2.0', id: 2, method: 'tools/list' })).json();
  const names = list.result.tools.map((t) => t.name);
  for (const n of FREE_TOOLS) if (n.startsWith('percolator_') || n === 'get_health') assert.ok(names.includes(n), n);
  assert.ok(!names.some((n) => PROTECTED_TOOLS.has(n)), 'writes must not be advertised');
}));

test('percolator read tools/call is free (no 402)', () => withServer(async (post) => {
  const r = await post('/mcp', {
    jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'percolator_list_markets', arguments: { cluster: 'devnet' } },
  });
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.equal(j.result.isError, false);
  assert.equal(j.result.structuredContent.result.ok, true);
}));

test('protected write tools/call without payment returns 402 x402 v1', () => withServer(async (post) => {
  const name = [...PROTECTED_TOOLS][0];
  const r = await post('/mcp', {
    jsonrpc: '2.0', id: 4, method: 'tools/call',
    params: { name, arguments: {} },
  });
  assert.equal(r.status, 402);
  const body = await r.json();
  assert.equal(body.x402Version, 1);
  assert.equal(body.accepts[0].payTo, DEFAULT_PAY_TO);
  assert.equal(body.accepts[0].scheme, 'exact');
  assert.ok(body.alternatives.clawdHolders.mint === CLAWD_MINT);
  // atomic prices: read 2000 / live 10000 / heavy 50000 / default live
  assert.ok(/^\d+$/.test(body.accepts[0].maxAmountRequired));
}));

test('gateMcp: mocked facilitator settles X-PAYMENT; free tools skip gate', async () => {
  const paid = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: [...PROTECTED_TOOLS][0], arguments: {} } };
  const free = { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'percolator_oracle_price', arguments: { slab: 'x' } } };
  assert.equal((await gateMcp(env, {}, free, { fetchImpl: fakeFetch })).allow, true);
  const denied = await gateMcp(env, {}, paid, { fetchImpl: fakeFetch });
  assert.equal(denied.allow, false);
  assert.equal(denied.response.status, 402);
  // fake base64 payment envelope (settlement mocked)
  const tx = Buffer.alloc(100, 1); tx[0] = 1;
  const envelope = Buffer.from(JSON.stringify({
    x402Version: 1, scheme: 'exact', network: 'solana',
    payload: { transaction: tx.toString('base64') },
  })).toString('base64');
  const ok = await gateMcp(env, { 'x-payment': envelope }, paid, { fetchImpl: fakeFetch });
  assert.equal(ok.allow, true);
  assert.equal(ok.via, 'x402');
});

test('SIWS challenge/verify issues bearer; holder passes protected gate', () => withServer(async (post) => {
  const c = await (await post('/auth/siws/challenge', { wallet: holder.wallet })).json();
  assert.equal(c.ok, true);
  const v = await (await post('/auth/siws/verify', {
    wallet: holder.wallet, nonce: c.nonce, signature: holder.sign(c.message),
  })).json();
  assert.equal(v.ok, true);
  assert.ok(v.token);
  const paid = { jsonrpc: '2.0', id: 9, method: 'tools/call', params: { name: [...PROTECTED_TOOLS][0], arguments: {} } };
  const via = await gateMcp(env, { authorization: 'Bearer ' + v.token }, paid, { fetchImpl: fakeFetch });
  assert.equal(via.via, 'clawd_holder');
  // replay nonce rejected
  assert.equal((await post('/auth/siws/verify', {
    wallet: holder.wallet, nonce: c.nonce, signature: holder.sign(c.message),
  })).status, 401);
}));
