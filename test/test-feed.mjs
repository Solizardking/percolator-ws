// Live price feed tests: mocked sidecar via createPercolator(callImpl).
// No network, no Python sidecar. Run: node --test test/test-feed.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createPercolator } from '../src/percolator.mjs';
import { createFeed, isValidSlab } from '../src/feed.mjs';

const SLAB = 'So11111111111111111111111111111111111111112'; // 32-byte base58
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fakeWs() {
  return {
    readyState: 1,
    sent: [],
    send(s) { this.sent.push(JSON.parse(s)); },
    ofType(t) { return this.sent.filter((m) => m.type === t); },
  };
}

function makeFeed(callImpl, opts = {}) {
  const percolator = createPercolator({ env: {}, callImpl });
  return createFeed({
    call: (name, args) => percolator.call(name, args),
    env: {},
    pollMs: 15,
    errorThrottleMs: 0,
    ...opts,
  });
}

test('subscribe -> schema-exact price.updated from oracle price_e6', async () => {
  const calls = [];
  const feed = makeFeed(async (name, args) => {
    calls.push([name, args]);
    return { ok: true, cluster: 'devnet', slab: args.slab, source: 'pool_ema', price_e6: 123_456_789, last_update_slot: 4242 };
  });
  const ws = fakeWs();
  assert.equal(feed.subscribe(ws, SLAB), true);
  await sleep(50);

  assert.ok(calls.length >= 1);
  assert.equal(calls[0][0], 'percolator_oracle_price');
  assert.deepEqual(calls[0][1], { slab: SLAB, cluster: 'devnet' });

  const prices = ws.ofType('price.updated');
  assert.equal(prices.length, 1); // unchanged price is not re-emitted
  const m = prices[0];
  assert.deepEqual(Object.keys(m).sort(), [
    'cluster', 'price', 'priceE6', 'simulated', 'slab', 'slot', 'source', 'timestamp', 'type',
  ]);
  assert.equal(m.slab, SLAB);
  assert.equal(m.price, 123.456789); // USD float, what useLivePrice.ts consumes
  assert.equal(m.priceE6, '123456789');
  assert.equal(typeof m.timestamp, 'number');
  assert.equal(m.slot, 4242);
  assert.equal(m.source, 'pool_ema');
  assert.equal(m.cluster, 'devnet');
  assert.equal(m.simulated, false);
  assert.equal(feed.state(), 'open'); // open only after a successful poll
  assert.equal(feed.subscriptionCount(), 1);
  assert.equal(feed.ingestLagSec(), 0);
  feed.stop();
});

test('unsubscribe -> silence, poller torn down', async () => {
  let priceE6 = 100_000_000;
  const feed = makeFeed(async () => ({ ok: true, price_e6: priceE6, last_update_slot: 1, source: 'admin' }));
  const ws = fakeWs();
  feed.subscribe(ws, SLAB);
  await sleep(40);
  assert.equal(ws.ofType('price.updated').length, 1);

  assert.equal(feed.unsubscribe(ws, SLAB), true);
  assert.equal(feed.subscriptionCount(), 0);
  assert.equal(feed.state(), 'idle');
  priceE6 = 200_000_000; // would emit if the poller were still alive
  await sleep(60);
  assert.equal(ws.ofType('price.updated').length, 1);
  assert.equal(ws.sent.length, 1);
  feed.stop();
});

test('bad slab -> rejected, oracle never called', async () => {
  let calls = 0;
  const feed = makeFeed(async () => { calls += 1; return { ok: true, price_e6: 1 }; });
  const ws = fakeWs();
  assert.equal(isValidSlab(SLAB), true);
  assert.equal(isValidSlab('not-a-slab!!'), false);
  assert.equal(isValidSlab('1111'), false); // valid base58, wrong length
  assert.equal(isValidSlab(undefined), false);
  assert.equal(feed.subscribe(ws, 'not-a-slab!!'), false);
  await sleep(30);
  assert.equal(calls, 0);
  assert.equal(ws.sent.length, 0);
  assert.equal(feed.subscriptionCount(), 0);
  feed.stop();
});

test('sidecar down -> degraded, error sent, NO price emitted', async () => {
  const feed = makeFeed(async () => ({ ok: false, error: 'sidecar_down' }));
  const ws = fakeWs();
  feed.subscribe(ws, SLAB);
  await sleep(60);
  assert.equal(ws.ofType('price.updated').length, 0); // never stale/simulated
  const errs = ws.ofType('error');
  assert.ok(errs.length >= 1);
  assert.equal(errs[0].code, 'oracle_unavailable');
  assert.equal(errs[0].slab, SLAB);
  assert.equal(feed.state(), 'degraded');
  assert.equal(feed.ingestLagSec(), null); // no successful poll ever happened
  feed.stop();
});

test('sidecar throwing -> degraded, no price emitted', async () => {
  const feed = makeFeed(async () => { throw new Error('connect ECONNREFUSED'); });
  const ws = fakeWs();
  feed.subscribe(ws, SLAB);
  await sleep(60);
  assert.equal(ws.ofType('price.updated').length, 0);
  assert.equal(feed.state(), 'degraded');
  feed.stop();
});
