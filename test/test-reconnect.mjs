// Forced-disconnect test: server must run with ENABLE_TEST_HOOKS=1.
const base = process.argv[2] || 'http://localhost:8787';
const h = async () => (await (await fetch(base + '/health')).json());
const before = await h();
console.log('before', JSON.stringify(before.feeds.wss), 'total', before.totalLaunches);
await fetch(base + '/__test/force-disconnect', { method: 'POST' });
await new Promise(r => setTimeout(r, 300));
console.log('right after', JSON.stringify((await h()).feeds.wss));
await new Promise(r => setTimeout(r, 2000));
const after = await h();
console.log('after', JSON.stringify(after.feeds.wss), 'backfilled', after.feeds.backfilled, 'total', after.totalLaunches);
const ok = after.feeds.wss.state === 'open'
  && after.feeds.wss.reconnects > before.feeds.wss.reconnects
  && after.feeds.wss.subscriptions === 2
  && after.totalLaunches > before.totalLaunches;
console.log(ok ? 'PASS reconnect + resubscribe' : 'FAIL');
process.exit(ok ? 0 : 1);
