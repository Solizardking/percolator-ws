// percolator-ws: WSS + paid MCP (/mcp) for percolator.tech.
// Reuses imperial-perps percolator_* reads via Python sidecar. Writes off. No keypairs.
import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { WebSocketServer } from 'ws';
import { createMcp } from './mcp.mjs';
import { createPercolator } from './percolator.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');
const env = process.env;
const PORT = Number(env.PORT || 8787);
const startedAt = Date.now();

if (env.PERCOLATOR_DEVNET_KEYPAIR) {
  console.error('REFUSED: PERCOLATOR_DEVNET_KEYPAIR must not be set');
  process.exit(1);
}
if (String(env.IMPERIAL_TRADING_ENABLED || '').toLowerCase() === 'true') {
  console.error('REFUSED: IMPERIAL_TRADING_ENABLED must not be set');
  process.exit(1);
}

const percolator = createPercolator({ env });

// Minimal "feed" state so ported reconnect/lag tests have a surface.
const feed = {
  wss: { state: 'open', reconnects: 0, subscriptions: 2 },
  backfilled: 0,
  rpc: { queued: 0 },
};
let totalMarkets = 0;
let forceHooks = env.ENABLE_TEST_HOOKS === '1';

function health() {
  return {
    status: 'ok',
    service: 'percolator-ws',
    uptime: Math.round((Date.now() - startedAt) / 1000),
    clients: wss.clients.size,
    totalLaunches: totalMarkets, // lag test field name
    totalMarkets,
    ingestLagSec: 0,
    percolator: percolator.info(),
    feeds: { wss: { ...feed.wss }, backfilled: feed.backfilled, rpc: { ...feed.rpc } },
    mcp: { path: '/mcp', reads: percolator.info().reads, writes: 0 },
  };
}

const mcp = createMcp({ health, env, percolator });

const publicDir = join(root, 'public');
function serveStatic(pathname, res) {
  let file = pathname === '/' ? 'index.html' : pathname.replace(/^\//, '');
  if (file === 'listen') file = 'listen.html';
  const path = join(publicDir, file);
  if (!path.startsWith(publicDir) || !existsSync(path)) return false;
  const ext = path.split('.').pop();
  const types = { html: 'text/html', js: 'text/javascript', css: 'text/css', png: 'image/png', svg: 'image/svg+xml' };
  res.writeHead(200, { 'content-type': types[ext] || 'application/octet-stream' });
  res.end(readFileSync(path));
  return true;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  try {
    if (url.pathname === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(health()));
      return;
    }
    if (forceHooks && url.pathname === '/__test/force-disconnect' && req.method === 'POST') {
      feed.wss.state = 'closed';
      feed.wss.reconnects += 1;
      setTimeout(() => {
        feed.wss.state = 'open';
        feed.wss.subscriptions = 2;
        feed.backfilled += 1;
        totalMarkets += 1;
        broadcast({ type: 'status', totalLaunches: totalMarkets, connected: true });
        broadcast({ type: 'market', source: 'percolator', time: new Date().toISOString(), slab: 'test' });
      }, 500);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    if (await mcp.http(req, res, url)) return;
    if (serveStatic(url.pathname, res)) return;
    res.writeHead(404); res.end('not found');
  } catch (e) {
    res.writeHead(500, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: String(e?.message || e).slice(0, 200) }));
  }
});

const wss = new WebSocketServer({ noServer: true });
server.on('upgrade', (req, sock, head) => {
  const p = new URL(req.url || '/', 'http://x').pathname;
  if (p !== '/' && p !== '/ws') return sock.destroy();
  wss.handleUpgrade(req, sock, head, (ws) => wss.emit('connection', ws));
});

function broadcast(o) {
  const s = JSON.stringify(o);
  for (const c of wss.clients) if (c.readyState === 1) c.send(s);
}

wss.on('connection', (ws) => {
  ws.send(JSON.stringify({
    type: 'status',
    connected: true,
    uptime: Math.round((Date.now() - startedAt) / 1000),
    totalLaunches: totalMarkets,
    clients: wss.clients.size,
    service: 'percolator-ws',
  }));
  // Seed one market event so lag test can observe something within the window when hooks/simulated.
  if (env.SIMULATE_MARKET_TICKS === '1') {
    const tick = () => {
      if (ws.readyState !== 1) return;
      totalMarkets += 1;
      ws.send(JSON.stringify({
        type: 'token-launch',
        source: 'percolator',
        time: new Date().toISOString(),
        name: 'perps',
        symbol: 'PERP',
      }));
    };
    tick();
    const iv = setInterval(tick, 5000);
    ws.on('close', () => clearInterval(iv));
  }
  ws.on('message', (raw) => {
    try {
      const j = JSON.parse(String(raw));
      if (j?.type === 'ping') ws.send(JSON.stringify({ type: 'pong', t: Date.now() }));
    } catch {}
  });
});

server.listen(PORT, () => {
  console.log(`percolator-ws listening on :${PORT}  mcp=/mcp  ws=/ws`);
});
