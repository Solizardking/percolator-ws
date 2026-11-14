// Live price feed: slab subscriptions -> percolator_oracle_price polls ->
// price.updated messages for the launch app (useLivePrice.ts).
//
// Protocol (consumed by percolator-launch):
//   in : {type:"subscribe",   slabAddress|slab}
//        {type:"unsubscribe", slabAddress|slab}
//   out: {type:"price.updated", slab, price, priceE6, timestamp, slot,
//         source, cluster, simulated:false}
// Prices come ONLY from the market's own on-chain oracle via the Python
// sidecar (pool EMA / authority price, E6). A failed poll never produces a
// price: subscribers get a throttled oracle_unavailable error instead, and
// the feed reports itself degraded. Simulated ticks (server.mjs) are a
// separate message type and never appear here.
import bs58 from 'bs58';

export function isValidSlab(slab) {
  if (typeof slab !== 'string' || slab.length < 32 || slab.length > 44) return false;
  try {
    return bs58.decode(slab).length === 32;
  } catch {
    return false;
  }
}

const intString = (v) => {
  if (typeof v === 'bigint') return v.toString();
  if (typeof v === 'number' && Number.isFinite(v)) return String(Math.trunc(v));
  if (typeof v === 'string' && /^-?\d+$/.test(v.trim())) return v.trim();
  return null;
};

export function createFeed({
  call,
  env = process.env,
  cluster = env.FEED_CLUSTER || 'devnet',
  pollMs = Number(env.FEED_POLL_MS || 3000),
  resendMs = 15_000,
  errorThrottleMs = 10_000,
  now = Date.now,
} = {}) {
  if (typeof call !== 'function') throw new Error('createFeed: call(name, args) is required');
  // slab -> { subs:Set<ws>, timer, lastPriceE6, lastSentAt, failures, lastErrorAt }
  const entries = new Map();
  let lastOkAt = null; // ms of last successful oracle poll (any slab)
  let consecutiveFailures = 0;

  const send = (ws, obj) => {
    if (ws?.readyState === 1) {
      try { ws.send(JSON.stringify(obj)); } catch {}
    }
  };
  const broadcast = (entry, obj) => { for (const ws of entry.subs) send(ws, obj); };

  async function poll(entry) {
    let r;
    try {
      r = await call('percolator_oracle_price', { slab: entry.slab, cluster });
    } catch (e) {
      r = { ok: false, error: String(e?.message || e) };
    }
    // Entry may have been torn down while the poll was in flight.
    if (entries.get(entry.slab) !== entry || entry.subs.size === 0) return;
    const priceE6 = r && r.ok !== false ? intString(r.price_e6) : null;
    if (priceE6 === null || Number(priceE6) <= 0) {
      entry.failures += 1;
      consecutiveFailures += 1;
      const t = now();
      if (t - entry.lastErrorAt >= errorThrottleMs) {
        entry.lastErrorAt = t;
        broadcast(entry, { type: 'error', code: 'oracle_unavailable', slab: entry.slab });
      }
      return; // never emit a stale or fabricated price
    }
    entry.failures = 0;
    consecutiveFailures = 0;
    lastOkAt = now();
    const changed = entry.lastPriceE6 !== priceE6;
    const stale = now() - entry.lastSentAt >= resendMs;
    entry.lastPriceE6 = priceE6;
    if (!changed && !stale) return;
    entry.lastSentAt = now();
    broadcast(entry, {
      type: 'price.updated',
      slab: entry.slab,
      price: Number(priceE6) / 1e6,
      priceE6,
      timestamp: now(),
      slot: r.last_update_slot ?? null,
      source: r.source ?? null,
      cluster: r.cluster || cluster,
      simulated: false,
    });
  }

  function subscribe(ws, slab) {
    if (!isValidSlab(slab)) return false;
    let entry = entries.get(slab);
    if (!entry) {
      entry = {
        slab, subs: new Set(), timer: null,
        lastPriceE6: null, lastSentAt: 0, failures: 0, lastErrorAt: 0,
      };
      entries.set(slab, entry);
      entry.timer = setInterval(() => { void poll(entry); }, pollMs);
      entry.timer.unref?.();
      void poll(entry); // immediate first price, not after pollMs
    }
    entry.subs.add(ws);
    // A late joiner on an already-polled slab gets the last known price at once.
    if (entry.lastPriceE6 !== null && entry.failures === 0) {
      entry.lastSentAt = now();
      send(ws, {
        type: 'price.updated',
        slab,
        price: Number(entry.lastPriceE6) / 1e6,
        priceE6: entry.lastPriceE6,
        timestamp: now(),
        slot: null,
        source: null,
        cluster,
        simulated: false,
      });
    }
    return true;
  }

  function unsubscribe(ws, slab) {
    const entry = entries.get(slab);
    if (!entry) return false;
    entry.subs.delete(ws);
    if (entry.subs.size === 0) {
      clearInterval(entry.timer);
      entries.delete(slab);
    }
    return true;
  }

  function removeClient(ws) {
    for (const slab of [...entries.keys()]) unsubscribe(ws, slab);
  }

  const subscriptionCount = () => {
    let n = 0;
    for (const e of entries.values()) n += e.subs.size;
    return n;
  };

  // idle: nobody is subscribed. open: ONLY after a successful oracle poll.
  // degraded: the latest polls are failing.
  const state = () => {
    if (entries.size === 0) return 'idle';
    if (consecutiveFailures > 0) return 'degraded';
    if (lastOkAt === null) return 'idle';
    return 'open';
  };

  return {
    subscribe,
    unsubscribe,
    removeClient,
    subscriptionCount,
    state,
    pollOnce: async (slab) => { const e = entries.get(slab); if (e) await poll(e); },
    ingestLagSec: () => (lastOkAt === null ? null : Math.max(0, Math.round((now() - lastOkAt) / 1000))),
    lastOkAt: () => lastOkAt,
    slabs: () => [...entries.keys()],
    stop: () => { for (const e of entries.values()) clearInterval(e.timer); entries.clear(); },
  };
}
