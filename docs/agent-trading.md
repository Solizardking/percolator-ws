# Agent trading on CLAWD Percolator (devnet)

How any agent (this platform's official bot, or another user's bot) trades CLAWD Percolator perps
through the MCP **without the server ever holding or using a key**:

```
quote  ->  build unsigned tx  ->  owner's wallet signs  ->  submit  ->  read positions
```

The program (`Anw121Fk3gKvUHXk938cY2xVb9Eq46WX3k15WESS49PA`) is **unaudited** and this is **devnet**.
Collateral is the devnet stand-in CLAWD-dev (`CTjhnHRb85g9m3LwZpfPKkoteKeFZcuX7VHfKeCVFxmW`).

## 1. Quote (read, always on)

`percolator_quote { slab, size, cluster?, collateral? }`

* `size` is base units, `+` long / `-` short.
* `collateral` (collateral units) defaults to initial margin + fee, i.e. max leverage.
* Returns notional, initial/maintenance margin, fee, max leverage, **`liquidation_price_e6`**, **`funding`**
  (`rate_bps_per_slot`, direction, estimated payment per slot/hour/day), `oracle_status` and `tradable`.
* For admin-oracle markets `tradable` is `false` when the pushed price is older than the market's
  `max_staleness_secs`. In that state the program falls back to reading the oracle account as Pyth and
  every crank/trade/withdraw fails with `IllegalOwner`. Someone has to push a new price first.

Show the owner the quote and get a yes for that exact market, side, size and collateral.

## 2. Build the unsigned transaction (write tools, off by default)

Write tools are only listed when the service runs with `PERCOLATOR_WRITES_ENABLED=true`. Every write tool
returns:

| Field | Meaning |
|---|---|
| `transaction_base64` | Legacy `Transaction`, owner as fee payer, recent blockhash, signatures zero-filled |
| `message_base64` | The same message, for wallets that sign raw messages |
| `signers_needed` | Always just the owner (plus nothing else) for deposit/withdraw/open/close |
| `recent_blockhash`, `last_valid_block_height` | Submit before this height or rebuild |
| `simulation` | `{err, units, logs}` from devnet `simulateTransaction` (sigVerify off) |
| `submitted: false`, `server_signed: false` | Always. The server never signs or sends. |

Tools:

* `percolator_deposit { slab, owner, amount, init? }`: `init=true` opens the trader account (InitUser) first.
* `percolator_open_position { slab, owner, size, deposit_amount? }`: same instruction order as the browser
  devnet app (`app/src/lib/percolator.ts` `tradeIxs`):
  `UpdatePoolPrice` (pool markets only) -> `KeeperCrank` -> optional `Deposit` -> `TradeCpi`.
  `TradeCpi` trades against the market's matcher-backed LP (`4urzQCupvnMWvGTovGZsYSWNaGFA9WjsztBYEQJeNnAS`),
  so the owner is the **only** signer. The old `TradeNoCpi` path (LP co-signature) is gone.
* `percolator_close_position { slab, owner }`: trades the opposite of the current size.
* `percolator_withdraw { slab, owner, amount }`: margin-checked on-chain.
* `percolator_update_oracle { slab, keeper }`, `percolator_create_user_market {...}`: same unsigned model.
  Create-market is the one place the server adds a signature: the freshly generated slab key (a new account
  address, discarded after building).

If `simulation.err` is not null, do not ask the owner to sign; fix the inputs.

## 3. The owner's wallet signs

Hand `transaction_base64` to the owner's own wallet (Phantom/Solflare/Backpack via Wallet Standard, or
Privy). Example in a browser:

```js
import { Transaction } from '@solana/web3.js';
const tx = Transaction.from(Buffer.from(b64, 'base64'));
const signed = await wallet.signTransaction(tx);   // owner approves in the wallet UI
```

Never ask for a seed or private key. An agent never signs on the owner's behalf.

## 4. Submit

The signed bytes go to a **devnet** RPC: `connection.sendRawTransaction(signed.serialize())`, or the
desk's devnet `/rpc` proxy on trade.percolator.tech/devnet. The MCP has no submit tool on purpose.

## 5. Read positions (read, always on)

`percolator_positions { owner, market?, cluster? }` (scans every program market when `market` is omitted).
Per position: `side`, `size`, `entry_price_e6`, `mark_price_e6`, `collateral_units`, `settled_pnl_units`,
`mark_pnl_units`, `pending_funding_units`, `unrealized_pnl_units`, `equity_units`, `maintenance_margin_units`,
`margin_ratio`, `liquidatable`, `liquidation_price_e6`, `funding`.

`entry_price_e6` is the engine's last settlement mark (variation margin), not an average fill price; realized
moves are already in `settled_pnl_units`.

## Formulas

All from `engine/src/percolator.rs` in `Solizardking/clawd-market`. Prices are engine e6 units (already
inverted for `invert=1` markets), sizes are base units, money is collateral units. Code: `python/clawd_perps/risk.py`.

* Mark PnL: `size * (P - entry) / 1e6` (`mark_pnl_for_position`).
* Equity: `capital + pnl + mark_pnl(P) - pending_funding` (`account_equity_mtm_at_oracle`; the positive-PnL
  haircut is taken as 1, which is the optimistic side when `pnl > 0`).
* Liquidatable when `equity(P) <= |size| * P / 1e6 * mm_bps / 1e4` (`is_above_maintenance_margin_mtm`).
* Liquidation price, with `m = mm_bps / 1e4` and `E = capital + pnl - pending_funding`:
  * long: `P_liq = (size * entry - E * 1e6) / (size * (1 - m))` (none if `<= 0`)
  * short: `P_liq = (E * 1e6 + |size| * entry) / (|size| * (1 + m))`
* Quote: entry = current mark, `E = collateral - fee`. The matcher's 5 bps fee + 10 bps spread move the real fill.
* Funding: the global index moves `ΔF = P * rate_bps_per_slot * Δslots / 1e4` (`accrue_funding`); an account
  pays `size * (F_global - F_account) / 1e6` (`settle_account_funding`, payer rounds up). `rate > 0` means
  longs pay shorts. The rate is set from LP inventory at each crank and can change at the next one.
  Hour/day estimates assume 9,000 slots per hour.

## Flags

| Env | Default | Effect |
|---|---|---|
| `PERCOLATOR_WRITES_ENABLED` | unset (off) | Lists the unsigned write builders (Node catalog and Python sidecar). |
| `PERCOLATOR_MAINNET_WRITES_ENABLED` | unset (off) | Also allows `cluster=mainnet` builds; still needs `confirm=true` + `confirm_token=CONFIRM_PERCOLATOR_TX`, still unsigned. On its own it no longer lists write tools. |
| `PERCOLATOR_DEVNET_RPC_URL` | public devnet | RPC used for reads, blockhash and simulation. |
| `PERCOLATOR_DEVNET_KEYPAIR`, `PERCOLATOR_MAINNET_KEYPAIR` | must be unset | Sidecar refuses to boot / builders refuse if set. |

Write tool calls go through the existing MCP access gate (`gateMcp`: x402 USDC or a $CLAWD holder SIWS
token). Reads, quote and positions stay free.

## Clawd chain (`6Grr…`) status

Not built here. What is missing for the same flow on clawd chain:

* A decoder for the wrapper's layout VERSION 18 slab (different offsets and instruction tags from `Anw121…`;
  do not reuse this decoder).
* Instruction builders for the wrapper (deposit, trade, close, withdraw) and its LP/matcher model.
* An RPC setting (`https://rpc.clawdchain.trade`) and a cluster value for it.
* An oracle: the clawd markets run at fixed seeded prices with no keeper (per `clawd-chain-main/docs/percolator-markets.md`).
* Live market addresses come from `https://percolator.clawdchain.trade/api/markets` (`perp` is the slab); the
  addresses in that doc predate the current registry.

## Tests

```bash
python -m pytest -q python/tests                     # unit; devnet simulation skipped
RUN_DEVNET_SIM=1 python -m pytest -q python/tests    # + simulate an open on a pool market (no send)
npm run test:unit
```
