"""CLAWD Percolator venue for the Imperial MCP server.

Permissionless perps markets on pump.fun / PumpSwap / Raydium LaunchLab (Stonk.fun) tokens, priced
by an in-program pool oracle, with CLAWD (Token-2022) collateral. Program source:
github.com/Solizardking/clawd-market (private). Unaudited: **devnet by default**.

Safety model (fail-closed, mirrors check_live_gate):
* cluster defaults to "devnet". Mainnet writes are OFF unless PERCOLATOR_MAINNET_WRITES_ENABLED=true
  AND confirm=true AND confirm_token="CONFIRM_PERCOLATOR_TX" AND cluster=mainnet. The program is **unaudited**.
* every write needs confirm=true AND confirm_token="CONFIRM_PERCOLATOR_TX". Otherwise it only builds and
  simulates the transaction and returns the plan (devnet/localnet).
* with confirmation the server returns a base64 transaction for the user's wallet to sign. The server only
  signs and sends itself on **devnet/localnet** when PERCOLATOR_DEVNET_KEYPAIR is set and is the fee payer.
  On **mainnet** the server never signs, never sends, and refuses any mainnet keypair env. Key material is never returned or logged.
* size/leverage and oracle price-drift caps are enforced before building trade txs.
"""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Callable, Optional

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from clawd_perps import ix as I
from clawd_perps.cli import auto_pow10
from clawd_perps.layout import CLUSTERS, TIERS, decode_slab, slab_len
from clawd_perps.rpc import Rpc
from clawd_perps.tx import load_keypair
from clawd_perps.venue import detect_venue, eligibility

CONFIRM_TOKEN = "CONFIRM_PERCOLATOR_TX"
DEFAULT_PROGRAM_ID = "Anw121Fk3gKvUHXk938cY2xVb9Eq46WX3k15WESS49PA"
CLAWD_MINT = "8cHzQHUS2s2h8TzCmfqPKYiM4dSt4roa3n7MyRLApump"
SLAB_SIZES = sorted({slab_len(t) for t in ("small", "prod")} | {slab_len(t) - 8 for t in ("small", "prod")})


def program_id() -> Pubkey:
    return Pubkey.from_string(os.environ.get("PERCOLATOR_PROGRAM_ID", DEFAULT_PROGRAM_ID))


def rpc_for(cluster: str) -> Rpc:
    if cluster not in CLUSTERS:
        raise ValueError(f"cluster must be one of {sorted(CLUSTERS)}")
    env = {"devnet": "PERCOLATOR_DEVNET_RPC_URL", "localnet": "PERCOLATOR_LOCALNET_RPC_URL",
           "mainnet": "PERCOLATOR_MAINNET_RPC_URL"}[cluster]
    return Rpc(os.environ.get(env) or CLUSTERS[cluster])


def mainnet_writes_enabled() -> bool:
    return os.environ.get("PERCOLATOR_MAINNET_WRITES_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def write_gate(cluster: str, confirm: bool, confirm_token: str) -> Optional[dict[str, Any]]:
    """None = confirmed write allowed. Else a refusal / dry-run marker.

    Mainnet requires PERCOLATOR_MAINNET_WRITES_ENABLED + confirm + token; even then the
    server only builds+simulates unsigned txs (never signs/sends). Program is unaudited.
    """
    if cluster == "mainnet":
        if not mainnet_writes_enabled():
            return {"ok": False, "refused": True, "submitted": False, "gate": "mainnet-writes-disabled",
                    "unaudited": True,
                    "speech": "CLAWD Percolator is unaudited; mainnet writes are disabled. "
                              "Set PERCOLATOR_MAINNET_WRITES_ENABLED=true to allow unsigned user-signed txs."}
        missing = []
        if confirm is not True:
            missing.append("confirm=true")
        if confirm_token != CONFIRM_TOKEN:
            missing.append(f'confirm_token="{CONFIRM_TOKEN}"')
        if missing:
            return {"ok": False, "refused": True, "submitted": False, "gate": "mainnet-confirm-required",
                    "missing": missing, "unaudited": True,
                    "speech": "Mainnet write refused: " + ", ".join(missing)
                              + ". Program is unaudited; server never signs or broadcasts on mainnet."}
        return None
    missing = []
    if confirm is not True:
        missing.append("confirm=true")
    if confirm_token != CONFIRM_TOKEN:
        missing.append(f'confirm_token="{CONFIRM_TOKEN}"')
    if missing:
        return {"dry_run": True, "missing": missing}
    return None


def _server_signer(cluster: str) -> Optional[Keypair]:
    # Never load or use a keypair for mainnet.
    if cluster == "mainnet":
        return None
    if os.environ.get("PERCOLATOR_MAINNET_KEYPAIR"):
        # Fail closed even on other clusters if a mainnet keypair path was set.
        return None
    path = os.environ.get("PERCOLATOR_DEVNET_KEYPAIR")
    return load_keypair(path) if path else None


def check_trade_caps(m: dict[str, Any], size: int) -> Optional[dict[str, Any]]:
    """Refuse trades that violate leverage / size / oracle price-drift caps. Pure."""
    prm = m["engine"]["params"]
    im = prm.get("initial_margin_bps") or 0
    max_lev = (10_000 / im) if im else None
    # Soft absolute size cap from env (base units); default 1e15 to avoid accidental huge ints.
    max_size = int(os.environ.get("PERCOLATOR_MAX_SIZE_UNITS", str(10**15)))
    if abs(size) > max_size:
        return {"ok": False, "refused": True, "gate": "size-cap", "max_size": max_size,
                "unaudited": True, "speech": f"Size {abs(size)} exceeds PERCOLATOR_MAX_SIZE_UNITS={max_size}."}
    # Oracle price-drift: if last_effective diverges from oracle beyond oracle_price_cap_e2bps, refuse.
    cap = m.get("oracle_price_cap_e2bps") or 0
    p = m["pool"]
    oracle = p["ema_price_e6"] if p["kind"] else m.get("authority_price_e6")
    last = m.get("last_effective_price_e6") or oracle
    if cap and oracle and last:
        # e2bps: 10000 = 100%; drift_bps = |last-oracle|/oracle * 10000; cap is in 0.01 bps units (e2bps)
        drift_e2bps = abs(int(last) - int(oracle)) * 1_000_000 // max(int(oracle), 1)
        if drift_e2bps > int(cap):
            return {"ok": False, "refused": True, "gate": "price-drift",
                    "drift_e2bps": drift_e2bps, "oracle_price_cap_e2bps": cap, "unaudited": True,
                    "speech": f"Oracle price drift {drift_e2bps} e2bps exceeds cap {cap}."}
    if max_lev is not None:
        # informational; on-chain enforces margin — surface for callers
        pass
    return None


def _sync_write(cluster: str, ixs: list, fee_payer: Pubkey, ephemeral: list[Keypair], confirm: bool,
                confirm_token: str, extra: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("PERCOLATOR_MAINNET_KEYPAIR"):
        return {"ok": False, "refused": True, "submitted": False, "gate": "mainnet-keypair-forbidden",
                "unaudited": True,
                "speech": "PERCOLATOR_MAINNET_KEYPAIR must never be set; server refuses to proceed."}
    gate = write_gate(cluster, confirm, confirm_token)
    if gate and gate.get("refused"):
        return gate
    rpc = rpc_for(cluster)
    bh = Hash.from_string(rpc.latest_blockhash())
    msg = Message.new_with_blockhash(ixs, fee_payer, bh)
    tx = Transaction.new_unsigned(msg)
    # Mainnet: never server-sign. Devnet: optional throwaway keypair only.
    signer = _server_signer(cluster)
    server_signs = signer is not None and signer.pubkey() == fee_payer and cluster != "mainnet"
    # Ephemeral slab keys (create_user_market) may be signed by the server; fee payer / user never on mainnet.
    keys = list(ephemeral) + ([signer] if server_signs else [])
    if keys:
        tx.partial_sign(keys, bh)
    b64 = base64.b64encode(bytes(tx)).decode()
    sim = rpc.simulate_b64(b64)
    out: dict[str, Any] = {"ok": sim.get("err") is None, "cluster": cluster, "program_id": str(program_id()),
                           "unaudited": True if cluster == "mainnet" else False,
                           "simulation": {"err": sim.get("err"), "units": sim.get("unitsConsumed"),
                                          "logs": (sim.get("logs") or [])[-12:]}, **extra}
    if gate:  # dry run (devnet/localnet only)
        out.update({"submitted": False, "dry_run": True, "missing": gate["missing"],
                    "speech": "Simulated only. Re-run with confirm=true and confirm_token to proceed."})
        return out
    if sim.get("err"):
        out.update({"submitted": False, "speech": "Simulation failed; nothing was sent."})
        return out
    signers_needed = [str(k) for k in msg.account_keys[:msg.header.num_required_signatures]]
    signed = {str(k.pubkey()) for k in keys}
    missing = [s for s in signers_needed if s not in signed]
    if cluster == "mainnet":
        # Never broadcast on mainnet — always return unsigned (user fee-payer) tx.
        out.update({"submitted": False, "server_signed": False, "transaction_base64": b64,
                    "signers_needed": missing or signers_needed,
                    "unaudited": True,
                    "speech": "UNAUDITED mainnet: transaction built+simulated only. "
                              "Sign with your wallet and submit yourself; this server will not sign or send."})
        return out
    if missing:
        out.update({"submitted": False, "transaction_base64": b64, "signers_needed": missing,
                    "speech": f"Transaction built on {cluster}. Sign with your wallet ({len(missing)} signature(s)) and send."})
        return out
    sig = rpc.send_b64(b64)
    out.update({"submitted": True, "signature": sig, "speech": f"Sent on {cluster}: {sig[:12]}..."})
    return out


async def _write(*args, **kwargs) -> dict[str, Any]:
    return await asyncio.to_thread(_sync_write, *args, **kwargs)


def _market(rpc: Rpc, slab: str) -> dict[str, Any]:
    a = rpc.account(slab)
    if a is None:
        raise ValueError(f"slab {slab} not found")
    if a[0] != str(program_id()):
        raise ValueError(f"slab owner {a[0]} is not the CLAWD Percolator program {program_id()}")
    m = decode_slab(a[1])
    m["slab"] = slab
    m["token_program"] = rpc.account(m["collateral_mint"])[0]
    return m


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    p = m["pool"]
    price = p["ema_price_e6"] if p["kind"] else (m["last_effective_price_e6"] or m["authority_price_e6"])
    return {"slab": m["slab"], "collateral_mint": m["collateral_mint"], "oracle": p["kind_name"],
            "pool": m["index_feed_id"] if p["kind"] else None, "price_e6": price, "invert": m["invert"],
            "open_interest": m["engine"]["total_open_interest"], "insurance": m["engine"]["insurance"],
            "accounts": m["engine"].get("num_used_accounts"), "paused": p["paused"], "migrated": p["migrated"],
            "resolved": m["resolved"], "admin": m["admin"], "tier": m["tier"],
            "max_leverage": round(10_000 / m["engine"]["params"]["initial_margin_bps"], 2)
            if m["engine"]["params"]["initial_margin_bps"] else None}


def _err(tool: str, e: Exception) -> dict[str, Any]:
    return {"ok": False, "tool": tool, "error": str(e)[:400], "speech": f"{tool} failed: {str(e)[:120]}"}


def register(tool: Callable[..., Any]) -> list[str]:
    """Register the percolator_* tools with the MCP server's decorator. Returns tool names."""

    @tool()
    async def percolator_list_markets(cluster: str = "devnet", limit: int = 50) -> dict[str, Any]:
        """List CLAWD Percolator perps markets (slabs) on a cluster (default devnet). Read-only."""
        def go():
            rpc = rpc_for(cluster)
            out = []
            for size in SLAB_SIZES:
                accts = rpc.call("getProgramAccounts", [str(program_id()), {
                    "encoding": "base64", "commitment": "confirmed", "filters": [{"dataSize": size}]}])
                for a in accts:
                    try:
                        m = decode_slab(base64.b64decode(a["account"]["data"][0]))
                        m["slab"] = a["pubkey"]
                        out.append(_summary(m))
                    except Exception:
                        continue
            return out[:limit]
        try:
            rows = await asyncio.to_thread(go)
        except Exception as e:
            return _err("percolator_list_markets", e)
        return {"ok": True, "cluster": cluster, "program_id": str(program_id()), "count": len(rows),
                "markets": rows, "speech": f"{len(rows)} CLAWD perps market(s) on {cluster}."}

    @tool()
    async def percolator_read_market(slab: str, cluster: str = "devnet", include_accounts: bool = False) -> dict[str, Any]:
        """Decode a CLAWD Percolator slab: config, pool-oracle state, engine totals (optionally accounts)."""
        try:
            m = await asyncio.to_thread(_market, rpc_for(cluster), slab)
        except Exception as e:
            return _err("percolator_read_market", e)
        if not include_accounts:
            m.pop("accounts", None)
        s = _summary(m)
        return {"ok": True, "cluster": cluster, "summary": s, "market": m,
                "speech": f"Market {slab[:6]}: {s['oracle']} oracle, price_e6 {s['price_e6']}, OI {s['open_interest']}."}

    @tool()
    async def percolator_oracle_price(slab: str, cluster: str = "devnet") -> dict[str, Any]:
        """Current index price of a market: pool EMA (trustless pool markets) or admin-pushed price."""
        try:
            m = await asyncio.to_thread(_market, rpc_for(cluster), slab)
        except Exception as e:
            return _err("percolator_oracle_price", e)
        p = m["pool"]
        out = {"ok": True, "cluster": cluster, "slab": slab, "source": p["kind_name"],
               "price_e6": p["ema_price_e6"] if p["kind"] else m["authority_price_e6"],
               "last_effective_price_e6": m["last_effective_price_e6"],
               "last_update_slot": p["last_update_slot"] if p["kind"] else None,
               "timestamp": p["last_update_ts"] if p["kind"] else m["authority_timestamp"],
               "pool_liquidity": p["last_liquidity"] if p["kind"] else None,
               "paused": p["paused"], "migrated": p["migrated"], "resolved": m["resolved"]}
        if not p["kind"]:
            out["warning"] = "admin-pushed oracle: a single key sets this price"
        out["speech"] = f"Index price_e6 {out['price_e6']} from {out['source']}."
        return out

    @tool()
    async def percolator_detect_venue(mint: str, cluster: str = "mainnet", min_liquidity: int = 1_000_000_000) -> dict[str, Any]:
        """Paste a token mint: detect pump.fun curve / PumpSwap / LaunchLab (Stonk.fun) pool and perps eligibility. Read-only."""
        try:
            d = await asyncio.to_thread(detect_venue, mint, rpc_for(cluster), cluster)
        except Exception as e:
            return _err("percolator_detect_venue", e)
        d["eligibility"] = eligibility(d, min_quote_liquidity=min_liquidity)
        d["ok"] = True
        d["speech"] = (f"{mint[:6]} trades on {d['venue']}; perps eligible: {d['eligibility'].get('eligible')}."
                       if d.get("venue") else f"No supported pool found for {mint[:6]}.")
        return d

    @tool()
    async def percolator_quote(slab: str, size: int, cluster: str = "devnet") -> dict[str, Any]:
        """Quote a position (size in base units, +long/-short): notional, initial margin, fee, leverage cap."""
        try:
            m = await asyncio.to_thread(_market, rpc_for(cluster), slab)
        except Exception as e:
            return _err("percolator_quote", e)
        s = _summary(m)
        notional = abs(size) * s["price_e6"] // 10**6
        prm = m["engine"]["params"]
        out = {"ok": True, "slab": slab, "side": "long" if size > 0 else "short", "size": size,
               "price_e6": s["price_e6"], "notional_units": notional,
               "initial_margin_units": notional * prm["initial_margin_bps"] // 10_000,
               "maintenance_margin_units": notional * prm["maintenance_margin_bps"] // 10_000,
               "fee_units": notional * prm["trading_fee_bps"] // 10_000, "max_leverage": s["max_leverage"],
               "tradable": not (s["paused"] or s["resolved"] or s["migrated"])}
        out["speech"] = f"{out['side']} {abs(size)}: notional {notional}, margin {out['initial_margin_units']} collateral units."
        return out

    @tool()
    async def percolator_create_user_market(mint: str, collateral_mint: str, creator: str, bond: int,
                                            im_bps: int = 2000, invert: bool = False, tier: str = "small",
                                            min_liquidity: int = 1_000_000_000, ema_alpha_bps: int = 500,
                                            cluster: str = "devnet", confirm: bool = False,
                                            confirm_token: str = "") -> dict[str, Any]:
        """Create a permissionless perps market on a pump.fun / PumpSwap / LaunchLab token. One atomic tx:
        create slab + vault, InitMarket, ConfigurePoolOracle, TopUpInsurance(bond). The creator is the admin, but
        pool markets lock every admin price lever. Devnet default. Simulates unless confirm=true and
        confirm_token="CONFIRM_PERCOLATOR_TX". Mainnet needs PERCOLATOR_MAINNET_WRITES_ENABLED (unsigned only)."""
        if cluster == "mainnet":
            g = write_gate(cluster, confirm, confirm_token)
            if g:
                return g
        if tier not in ("small", "prod"):
            return {"ok": False, "error": "tier must be small|prod"}
        try:
            rpc = rpc_for(cluster)
            det = await asyncio.to_thread(detect_venue, mint, rpc, cluster)
            elig = eligibility(det, min_quote_liquidity=min_liquidity)
            if not elig.get("eligible"):
                return {"ok": False, "eligibility": elig, "notes": det.get("notes"),
                        "speech": f"Not eligible: {elig.get('reason')}."}
            prog, payer, col = program_id(), Pubkey.from_string(creator), Pubkey.from_string(collateral_mint)
            tp = Pubkey.from_string((await asyncio.to_thread(rpc.account, collateral_mint))[0])
            inv = 1 if invert else 0
            pow10 = auto_pow10(det["observation"]["raw_price_e6"], inv)
            im = max(im_bps, elig["min_initial_margin_bps"])
            slab = Keypair()
            size = slab_len(tier)
            vauth = I.vault_authority(prog, slab.pubkey())
            vault = I.ata(vauth, col, tp)
            pool = Pubkey.from_string(det["pool"])
            vaults = tuple(Pubkey.from_string(v) for v in det["pool_vaults"]) if det.get("pool_vaults") else None
            rent = await asyncio.to_thread(rpc.rent, size)
            ixs = [
                I.create_account(payer, slab.pubkey(), rent, size, prog),
                I.create_ata_idempotent(payer, vauth, col, tp),
                I.init_market(prog, payer, slab.pubkey(), col, vault, tp, bytes(pool), invert=inv,
                              risk=I.risk_params(im_bps=im, mm_bps=im // 2, max_accounts=TIERS[tier][0])),
                I.configure_pool_oracle(prog, payer, slab.pubkey(), pool, Pubkey.from_string(mint), kind=det["kind"],
                                        price_pow10=pow10, min_quote_liquidity=min_liquidity, bond_units=bond,
                                        ema_alpha_bps=ema_alpha_bps, pool_vaults=vaults),
                I.top_up_insurance(prog, payer, slab.pubkey(), I.ata(payer, col, tp), vault, tp, bond),
            ]
        except Exception as e:
            return _err("percolator_create_user_market", e)
        return await _write(cluster, ixs, payer, [slab], confirm, confirm_token, {
            "slab": str(slab.pubkey()), "vault": str(vault), "venue": det["venue"], "pool": str(pool),
            "kind": det["kind"], "price_pow10": pow10, "invert": inv, "im_bps": im, "bond": bond, "tier": tier,
            "slab_bytes": size, "rent_lamports": rent})

    async def _user_tx(tool_name: str, slab: str, owner: str, cluster: str, confirm: bool, confirm_token: str,
                       build: Callable[[dict, Pubkey, Pubkey, Pubkey, Pubkey, Pubkey, Pubkey], list],
                       size: Optional[int] = None) -> dict[str, Any]:
        if cluster == "mainnet":
            g = write_gate(cluster, confirm, confirm_token)
            if g:
                return g
        try:
            m = await asyncio.to_thread(_market, rpc_for(cluster), slab)
            if size is not None:
                cap = check_trade_caps(m, size)
                if cap:
                    return cap
            prog, s, o = program_id(), Pubkey.from_string(slab), Pubkey.from_string(owner)
            tp, mint, vault = (Pubkey.from_string(m[k]) for k in ("token_program", "collateral_mint", "vault"))
            oracle = Pubkey.from_string(m["index_feed_id"]) if m["pool"]["kind"] else s
            ixs = build(m, prog, s, o, I.ata(o, mint, tp), vault, tp, oracle)
        except Exception as e:
            return _err(tool_name, e)
        return await _write(cluster, ixs, o, [], confirm, confirm_token,
                            {"slab": slab, "tool": tool_name, "unaudited_program": True})

    def _idx(m: dict, owner: Pubkey, idx: Optional[int]) -> int:
        if idx is not None:
            return idx
        for a in m.get("accounts") or []:
            if a["owner"] == str(owner) and a["kind"] == "user":
                return a["idx"]
        raise ValueError("no account for owner in this market; call percolator_deposit with init=true first")

    @tool()
    async def percolator_deposit(slab: str, owner: str, amount: int, user_idx: Optional[int] = None,
                                 init: bool = False, cluster: str = "devnet", confirm: bool = False,
                                 confirm_token: str = "") -> dict[str, Any]:
        """Deposit collateral (base units). init=true first opens the trader account (InitUser). Devnet default; gated."""
        def build(m, prog, s, o, uata, vault, tp, oracle):
            ixs = []
            if init:
                ixs.append(I.init_user(prog, o, s, uata, vault, tp))
                i = user_idx if user_idx is not None else -1
            else:
                i = _idx(m, o, user_idx)
            if i >= 0:
                ixs.append(I.deposit(prog, o, s, uata, vault, tp, i, amount))
            return ixs
        return await _user_tx("percolator_deposit", slab, owner, cluster, confirm, confirm_token, build)

    @tool()
    async def percolator_withdraw(slab: str, owner: str, amount: int, user_idx: Optional[int] = None,
                                  cluster: str = "devnet", confirm: bool = False, confirm_token: str = "") -> dict[str, Any]:
        """Withdraw collateral (margin-checked on-chain at the oracle price). Devnet default; gated."""
        def build(m, prog, s, o, uata, vault, tp, oracle):
            return [I.withdraw(prog, o, s, vault, uata, tp, oracle, _idx(m, o, user_idx), amount)]
        return await _user_tx("percolator_withdraw", slab, owner, cluster, confirm, confirm_token, build)

    async def _trade(tool_name, slab, owner, lp_owner, size, lp_idx, user_idx, cluster, confirm, confirm_token):
        def build(m, prog, s, o, uata, vault, tp, oracle):
            lp = Pubkey.from_string(lp_owner)
            li = lp_idx
            if li is None:
                li = next(a["idx"] for a in m.get("accounts") or [] if a["kind"] == "lp" and a["owner"] == lp_owner)
            ixs = []
            if m["pool"]["kind"]:
                vaults = (Pubkey.from_string(m["pool"]["base_vault"]), Pubkey.from_string(m["pool"]["quote_vault"])) \
                    if m["pool"]["kind"] == I.KIND_PUMPSWAP else None
                ixs.append(I.update_pool_price(prog, o, s, oracle, vaults))
            ixs.append(I.trade_no_cpi(prog, o, lp, s, oracle, li, _idx(m, o, user_idx), size))
            return ixs
        return await _user_tx(tool_name, slab, owner, cluster, confirm, confirm_token, build, size=size)

    @tool()
    async def percolator_open_position(slab: str, owner: str, lp_owner: str, size: int, lp_idx: Optional[int] = None,
                                       user_idx: Optional[int] = None, cluster: str = "devnet", confirm: bool = False,
                                       confirm_token: str = "") -> dict[str, Any]:
        """Open/increase a position against an LP (size base units, +long/-short). Refreshes the pool oracle first.
        TradeNoCpi needs the LP's co-signature (devnet). Gated; mainnet unsigned-only when PERCOLATOR_MAINNET_WRITES_ENABLED."""
        return await _trade("percolator_open_position", slab, owner, lp_owner, size, lp_idx, user_idx,
                            cluster, confirm, confirm_token)

    @tool()
    async def percolator_close_position(slab: str, owner: str, lp_owner: str, user_idx: Optional[int] = None,
                                        lp_idx: Optional[int] = None, cluster: str = "devnet", confirm: bool = False,
                                        confirm_token: str = "") -> dict[str, Any]:
        """Close the owner's whole position (trades the opposite of the current size). Gated; mainnet unsigned if enabled."""
        if cluster == "mainnet":
            g = write_gate(cluster, confirm, confirm_token)
            if g:
                return g
        try:
            m = await asyncio.to_thread(_market, rpc_for(cluster), slab)
            o = Pubkey.from_string(owner)
            i = _idx(m, o, user_idx)
            pos = next(a["position_size"] for a in m["accounts"] if a["idx"] == i)
        except Exception as e:
            return _err("percolator_close_position", e)
        if pos == 0:
            return {"ok": True, "submitted": False, "speech": "No open position."}
        return await _trade("percolator_close_position", slab, owner, lp_owner, -pos, lp_idx, i,
                            cluster, confirm, confirm_token)

    @tool()
    async def percolator_update_oracle(slab: str, keeper: str, cluster: str = "devnet", confirm: bool = False,
                                       confirm_token: str = "") -> dict[str, Any]:
        """Permissionless keeper call: UpdatePoolPrice (read pool reserves into the EMA). Gated; mainnet unsigned-only when PERCOLATOR_MAINNET_WRITES_ENABLED."""
        def build(m, prog, s, o, uata, vault, tp, oracle):
            if not m["pool"]["kind"]:
                raise ValueError("admin-oracle market: no pool to read")
            vaults = (Pubkey.from_string(m["pool"]["base_vault"]), Pubkey.from_string(m["pool"]["quote_vault"])) \
                if m["pool"]["kind"] == I.KIND_PUMPSWAP else None
            return [I.update_pool_price(prog, o, s, oracle, vaults)]
        return await _user_tx("percolator_update_oracle", slab, keeper, cluster, confirm, confirm_token, build)

    return ["percolator_list_markets", "percolator_read_market", "percolator_oracle_price", "percolator_detect_venue",
            "percolator_quote", "percolator_create_user_market", "percolator_deposit", "percolator_withdraw",
            "percolator_open_position", "percolator_close_position", "percolator_update_oracle"]
