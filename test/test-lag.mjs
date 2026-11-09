// Connects to percolator-ws and reports WS message types + /health. With SIMULATE_MARKET_TICKS=1,
// expects token-launch events; otherwise PASS on healthy status stream.
import WebSocket from 'ws';
const base = process.argv[2] || 'ws://localhost:8787';
const secs = Number(process.argv[3] || 45);
const lags = {}; const totals = []; const types = {}; let replayed = 0; const t0 = Date.now();
for (const path of ['/ws', '/']) {
  const ws = new WebSocket(base + path);
  ws.on('message', m => {
    const j = JSON.parse(m);
    if (path === '/') { types['root:' + j.type] = 1; return; }
    types[j.type] = (types[j.type] || 0) + 1;
    if (j.type === 'status') totals.push(j.totalLaunches);
    if (j.type === 'token-launch') {
      const lag = Date.now() - Date.parse(j.time);
      if (Date.now() - t0 < 500) replayed++;
      (lags[j.source || 'unknown'] ??= []).push(lag);
    }
  });
  setTimeout(() => ws.close(), secs * 1000);
}
setTimeout(async () => {
  console.log('message types:', JSON.stringify(types));
  for (const [s, l] of Object.entries(lags)) {
    l.sort((a, b) => a - b);
    console.log(`${s}: n=${l.length} median ${(l[l.length >> 1] / 1000).toFixed(1)}s max ${(l.at(-1) / 1000).toFixed(1)}s`);
  }
  console.log('totalLaunches over time:', totals.join(' -> '));
  const h = await fetch(base.replace('ws', 'http') + '/health');
  const j = await h.json();
  console.log('/health', h.status, JSON.stringify({ status: j.status, totalLaunches: j.totalLaunches, mcp: j.mcp }));
  const all = Object.values(lags).flat();
  const stale = all.filter(x => x > 10 * 60_000).length;
  const hasStatus = !!types.status || !!types['root:status'];
  const ok = h.status === 200 && j.status === 'ok' && hasStatus && stale === 0;
  console.log(ok ? (all.length ? 'PASS no stale launches' : 'PASS health + status (no launch ticks in window)') : 'FAIL');
  process.exit(ok ? 0 : 1);
}, secs * 1000 + 1500);
