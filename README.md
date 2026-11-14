# percolator-ws

Local **WSS + paid MCP** server for [percolator.tech](https://percolator.tech).

- **MCP** at `/mcp` (Streamable HTTP), mirroring clawd-ws-v2 / pumpai access:
  - `initialize`, `tools/list`, `resources/*` — free
  - `percolator_*` **reads** — free (`list_markets`, `read_market`, `oracle_price`, `quote`, `detect_venue`)
  - Write tools — **not registered** (disabled). Reserved names still 402 via x402 if called.
  - x402 Solana USDC (tiers $0.002 / $0.01 / $0.05) via `MCP_X402_FACILITATOR_URL` (default `https://musebook.trade/api/x402`)
  - `$CLAWD` holders free after SIWS (`POST /auth/siws/challenge` + `/verify` → 1h Bearer)
- **WSS** on `/` and `/ws`: live `price.updated` feed (see below) + status + optional simulated market ticks
- **Python sidecar** reuses `imperial-perps-mcp-server` `percolator_venue.py` + vendored `clawd_perps/` (reads only)

## Live price feed (WSS)

The launch app (`percolator-launch`, `useLivePrice.ts`) consumes this feed:

- Client connects to `/ws`, then sends `{"type":"subscribe","slabAddress":"<slab>"}` (`slab` also accepted). `{"type":"unsubscribe","slabAddress":"<slab>"}` on cleanup.
- Server polls that market's own on-chain oracle through the Python sidecar (`percolator_oracle_price`, pool EMA / authority price) — one shared poller per slab, `FEED_POLL_MS` (default `3000`), cluster `FEED_CLUSTER` (default `devnet`) — and emits on change (plus a 15s resend):
  `{"type":"price.updated","slab":"<slab>","price":<USD float>,"priceE6":"<int string>","timestamp":<ms>,"slot":<last_update_slot>,"source":"<oracle source>","cluster":"devnet","simulated":false}`
- Invalid slab -> `{"type":"error","code":"bad_slab"}`. Oracle/sidecar failure -> throttled `{"type":"error","code":"oracle_unavailable","slab":"<slab>"}` and the feed turns `degraded` — a failed poll **never** produces a price.
- A 30s `{"type":"status",...}` heartbeat is kept type-distinct from price messages, so "socket open" is never confused with "prices flowing".
- `/health` reports the feed honestly: `feeds.wss.state` is `idle` (no subscriptions), `open` (**only** after a successful oracle poll), or `degraded`; `feeds.wss.subscriptions` is the real registry count; `ingestLagSec` is seconds since the last successful poll (`null` if none yet); `feeds.simulated` mirrors `SIMULATE_MARKET_TICKS`.

**Simulated != live.** `SIMULATE_MARKET_TICKS=1` emits demo `token-launch` ticks labelled `"simulated":true`. They carry no price, are never sent as `price` / `price.updated`, and are not counted as markets or launches.

Tests: `node --test test/test-feed.mjs` (mocked sidecar; covers subscribe -> `price.updated`, unsubscribe silence, bad slab, sidecar-down degraded).

## Safety

Never set `PERCOLATOR_DEVNET_KEYPAIR` or `IMPERIAL_TRADING_ENABLED` on this host — the process refuses to boot if either is set.

## Run locally

```bash
cp .env.example .env   # set MCP_AUTH_SECRET (24+ chars)
npm install
# optional Python reads upstream:
python3 -m venv python/.venv && python/.venv/bin/pip install -r python/requirements.txt
# terminal A:
PERCOLATOR_PY_PORT=8791 python/.venv/bin/python python/server_reads.py
# terminal B:
MCP_AUTH_SECRET=dev-secret-at-least-24-chars npm start
```

Without the Python sidecar, inject `callImpl` in tests or point `PERCOLATOR_PY_MCP_URL` at a running Imperial MCP that exposes `percolator_*`.

## Tests

```bash
npm run test:unit          # access + catalog (mocked facilitator; no real payments)
ENABLE_TEST_HOOKS=1 SIMULATE_MARKET_TICKS=1 MCP_AUTH_SECRET=dev-secret-at-least-24-chars npm start &
node test/test-lag.mjs ws://127.0.0.1:8787 8
node test/test-reconnect.mjs http://127.0.0.1:8787
python3 test/page_check.py http://127.0.0.1:8787 15 out.png   # needs playwright
```

Upstream Imperial suite (vendored venue): from `../imperial-perps-mcp-server`, `.venv/bin/pytest -q` and `scripts/smoke_client.py`.

## Deploy notes (not done here)

To put this on **percolator.tech**:

1. Create a GitHub repo and push this tree (not done in this task).
2. Railway (or similar): Dockerfile build, set `MCP_AUTH_SECRET`, `SOLANA_RPC_URL`, optional `MCP_X402_*`, `PERCOLATOR_*_RPC_URL`.
3. DNS: point `percolator.tech` (and `www`) A/CNAME at the Railway service; terminate TLS.
4. Confirm facilitator `https://musebook.trade/api/x402` accepts `payTo` and that SIWS domain text matches the public host.
5. Do **not** enable write tools or set a trading keypair on the public host.

## Mainnet unsigned writes (opt-in)

Set `PERCOLATOR_MAINNET_WRITES_ENABLED=true` (and the same on the Python sidecar) to advertise
write tools. Calls still require x402 / $CLAWD holder payment, then venue gates
`confirm=true` + `confirm_token=CONFIRM_PERCOLATOR_TX` + `cluster=mainnet`. The server
**never** signs or broadcasts mainnet txs (returns `transaction_base64` + `signers_needed`).
Program is **unaudited**. Never set `PERCOLATOR_DEVNET_KEYPAIR` / `IMPERIAL_TRADING_ENABLED` /
`PERCOLATOR_MAINNET_KEYPAIR` on this host.
