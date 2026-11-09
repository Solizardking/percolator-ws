// Streamable HTTP /mcp for percolator.tech. initialize / tools/list / resources free.
// percolator_* reads are free (FREE_TOOLS). Protected tools (none registered by default;
// reserved write names) go through gateMcp (x402 USDC or $CLAWD SIWS holder).
import { randomUUID } from 'node:crypto';
import { gateMcp, siwsChallenge, siwsVerify, protectedResourceMetadata, sendWebResponse } from './mcp-access.mjs';
import { PERCOLATOR_READ_TOOLS, PERCOLATOR_WRITE_TOOLS as WRITE_FROM_MODULE } from './percolator.mjs';

const RO = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true };

// Write tool names from percolator_venue — never registered here, but reserved for gating if enabled later.
export const PERCOLATOR_WRITE_TOOLS = WRITE_FROM_MODULE;
export const PROTECTED_TOOLS = new Set(PERCOLATOR_WRITE_TOOLS);

export function createMcp({ health, env = process.env, fetchImpl = fetch, percolator = null, now = Date.now } = {}) {
  const getHealth = typeof health === 'function' ? health : () => health || { status: 'ok' };

  const tools = () => [
    { name: 'get_health', description: 'Percolator.tech service health.', inputSchema: { type: 'object', properties: {} }, annotations: RO },
    ...(percolator?.tools || []),
  ];

  async function handleMessage(msg) {
    const id = msg?.id ?? null;
    const ok = (result) => ({ jsonrpc: '2.0', id, result });
    const err = (code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
    switch (msg?.method) {
      case 'initialize':
        return ok({
          protocolVersion: '2024-11-05',
          capabilities: { tools: {}, resources: {} },
          serverInfo: { name: 'percolator-tech', version: '0.1.0' },
        });
      case 'notifications/initialized':
        return null;
      case 'ping':
        return ok({});
      case 'tools/list':
        return ok({ tools: tools() });
      case 'resources/list':
        return ok({ resources: [] });
      case 'tools/call': {
        const name = String(msg?.params?.name || '');
        const a = msg?.params?.arguments || {};
        if (name === 'get_health') {
          const h = getHealth();
          return ok({ content: [{ type: 'text', text: JSON.stringify(h) }], structuredContent: { result: h } });
        }
        if (percolator?.names?.has(name)) {
          let data;
          try { data = await percolator.call(name, a); }
          catch (e) { data = { ok: false, error: String(e?.message || e).slice(0, 300), tool: name }; }
          return ok({
            content: [{ type: 'text', text: JSON.stringify(data) }],
            structuredContent: { result: data },
            isError: data?.ok === false,
          });
        }
        if (PROTECTED_TOOLS.has(name)) {
          return ok({
            content: [{ type: 'text', text: JSON.stringify({ ok: false, blocked: true, error: 'percolator_writes_disabled' }) }],
            structuredContent: { result: { ok: false, blocked: true, error: 'percolator_writes_disabled' } },
            isError: true,
          });
        }
        return err(-32601, `Unknown tool: ${name}`);
      }
      default:
        return err(-32601, `Method not found: ${msg?.method}`);
    }
  }

  async function http(req, res, url) {
    const path = url.pathname;
    if (path === '/.well-known/oauth-protected-resource' || path === '/.well-known/oauth-protected-resource/mcp') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(protectedResourceMetadata()));
      return true;
    }
    if (path === '/auth/siws/challenge' && req.method === 'POST') {
      const body = await readJson(req);
      const out = siwsChallenge(env, body?.wallet);
      res.writeHead(out.ok ? 200 : 400, { 'content-type': 'application/json' });
      res.end(JSON.stringify(out));
      return true;
    }
    if (path === '/auth/siws/verify' && req.method === 'POST') {
      const body = await readJson(req);
      const out = siwsVerify(env, body);
      res.writeHead(out.ok ? 200 : 401, { 'content-type': 'application/json' });
      res.end(JSON.stringify(out));
      return true;
    }
    if (path !== '/mcp') return false;

    if (req.method === 'GET') {
      // SSE stream placeholder
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
      res.write(`event: endpoint\ndata: /mcp\n\n`);
      req.on('close', () => {});
      return true;
    }
    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': 'content-type, authorization, x-payment, mcp-session-id',
        'access-control-allow-methods': 'POST, GET, OPTIONS',
      });
      res.end();
      return true;
    }
    if (req.method !== 'POST') {
      res.writeHead(405); res.end(); return true;
    }

    const body = await readJson(req);
    const msgs = Array.isArray(body) ? body : [body];
    const headers = lowerHeaders(req.headers);

    // Gate protected tools/call only (reads + initialize/list stay free).
    const gated = msgs.filter((x) => x?.method === 'tools/call' && PROTECTED_TOOLS.has(String(x?.params?.name || '')));
    if (gated.length) {
      const decision = await gateMcp(env, headers, gated.length === 1 ? gated[0] : gated, { fetchImpl });
      if (!decision.allow) {
        await sendWebResponse(res, decision.response);
        return true;
      }
    }

    const results = [];
    for (const m of msgs) {
      const r = await handleMessage(m);
      if (r) results.push(r);
    }
    const out = Array.isArray(body) ? results : results[0] ?? { jsonrpc: '2.0', id: body?.id ?? null, result: {} };
    res.writeHead(200, {
      'content-type': 'application/json',
      'access-control-allow-origin': '*',
      'mcp-session-id': req.headers['mcp-session-id'] || randomUUID(),
    });
    res.end(JSON.stringify(out));
    return true;
  }

  return { http, tools, handleMessage };
}

function lowerHeaders(h) {
  const o = {};
  for (const [k, v] of Object.entries(h || {})) o[String(k).toLowerCase()] = v;
  return o;
}

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString('utf8');
  if (!raw) return {};
  try { return JSON.parse(raw); } catch { return {}; }
}

export { PERCOLATOR_READ_TOOLS };
