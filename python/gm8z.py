"""GM8z mainnet reads for the percolator MCP — READ-ONLY.

GM8z is toly's percolator program AS DEPLOYED ON MAINNET
(GM8zjJ8LTBMv9xEsverh6H6wLyevgMHEJXcEzyY3rY24, matcher
DHP6DtwXP1yJsz8YzfoeigRFPB979gzmumkmCxDLSkUX). It is NOT the CLAWD fork
(Anw121Fk..., devnet) that percolator_venue.py reads: the fork's slab config
grew and its tags diverged, so the fork decoder must not be used here.

The decoder below is a line-for-line port of the slab decoder in
Solizardking/clawd-market `app/src/lib/gm8z.ts` (decodeGm8zSlab) — the same
code the mainnet desk (mainnet.percolator.tech) uses to decode slabs
byte-for-byte. Layout facts carried over from that file:
  - SlabHeader 72 B, MarketConfig first 320 B, RiskEngine at ENGINE_OFF=392
  - account records 240 B at ENGINE_OFF + 9136, bitmap at ENGINE_OFF + 408
  - num_used u16 at ENGINE_OFF + 920, max 4096 accounts
  - slab length 992,560 B (production engine)
Collateral on GM8z is classic SPL only; the oracle is the admin authority
push (index_feed_id == zero key) or a Pyth feed id. No pool oracle exists.
"""
from __future__ import annotations

import asyncio
import base64
import struct
from typing import Any, Callable

from clawd_perps.layout import _b58
from clawd_perps.rpc import Rpc
from percolator_venue import rpc_for

GM8Z_PROGRAM_ID = "GM8zjJ8LTBMv9xEsverh6H6wLyevgMHEJXcEzyY3rY24"
GM8Z_MATCHER_PROGRAM_ID = "DHP6DtwXP1yJsz8YzfoeigRFPB979gzmumkmCxDLSkUX"
ZERO_KEY = "11111111111111111111111111111111"

MAGIC = 0x504552434F4C4154  # "PERCOLAT"
HEADER_LEN = 72
ENGINE_OFF = 392
SLAB_LEN = 992_560
MAX_ACCOUNTS = 4096
ACCOUNT_SIZE = 240
ACCOUNTS_OFF = 9136  # relative to ENGINE_OFF
NUM_USED_OFF = 920   # relative to ENGINE_OFF


def _u(fmt: str, d: bytes, off: int):
    return struct.unpack_from("<" + fmt, d, off)[0]


def _u128(d: bytes, off: int) -> int:
    return int.from_bytes(d[off:off + 16], "little")


def _i128(d: bytes, off: int) -> int:
    return int.from_bytes(d[off:off + 16], "little", signed=True)


def _key(d: bytes, off: int) -> str:
    return _b58(d[off:off + 32])


def decode_gm8z_slab(address: str, raw: bytes) -> dict[str, Any] | None:
    """Port of decodeGm8zSlab (clawd-market app/src/lib/gm8z.ts). None if not a GM8z slab."""
    if len(raw) < ENGINE_OFF + ACCOUNTS_OFF:
        return None
    if _u("Q", raw, 0) != MAGIC:
        return None
    c, e = HEADER_LEN, ENGINE_OFF
    authority_price_e6 = _u("Q", raw, c + 288)
    last_effective_price_e6 = _u("Q", raw, c + 312)
    num_used = _u("H", raw, e + NUM_USED_OFF)
    accounts: list[dict[str, Any]] = []
    for wi in range((MAX_ACCOUNTS + 63) // 64):
        word = _u("Q", raw, e + 408 + wi * 8)
        if word == 0:
            continue
        for bit in range(64):
            if not (word >> bit) & 1:
                continue
            idx = wi * 64 + bit
            a = e + ACCOUNTS_OFF + idx * ACCOUNT_SIZE
            if a + ACCOUNT_SIZE > len(raw):
                continue
            accounts.append({
                "idx": idx,
                "kind": "lp" if raw[a + 24] == 1 else "user",
                "owner": _key(raw, a + 184),
                "capital": _u128(raw, a + 8),
                "pnl": _i128(raw, a + 32),
                "position_size": _i128(raw, a + 80),
                "entry_price": _u("Q", raw, a + 96),
                "fee_credits": _i128(raw, a + 216),
                "matcher_program": _key(raw, a + 120),
                "matcher_context": _key(raw, a + 152),
            })
    return {
        "address": address,
        "admin": _key(raw, 16),
        "resolved": bool(raw[13] & 1),
        "collateral_mint": _key(raw, c),
        "vault": _key(raw, c + 32),
        "index_feed_id": _key(raw, c + 64),
        "invert": raw[c + 107] != 0,
        "unit_scale": _u("I", raw, c + 108),
        "oracle_authority": _key(raw, c + 256),
        "authority_price_e6": authority_price_e6,
        "authority_timestamp": _u("q", raw, c + 296),
        "last_effective_price_e6": last_effective_price_e6,
        "price_e6": last_effective_price_e6 or authority_price_e6,
        "vault_units": _u128(raw, e),
        "insurance": _u128(raw, e + 16),
        "fee_revenue": _u128(raw, e + 32),
        "maintenance_margin_bps": _u("Q", raw, e + 56),
        "initial_margin_bps": _u("Q", raw, e + 64),
        "trading_fee_bps": _u("Q", raw, e + 72),
        "max_accounts": _u("Q", raw, e + 80),
        "current_slot": _u("Q", raw, e + 192),
        "funding_rate_bps_per_slot": _u("q", raw, e + 224),
        "last_crank_slot": _u("Q", raw, e + 232),
        "total_open_interest": _u128(raw, e + 248),
        "c_tot": _u128(raw, e + 264),
        "num_used": num_used,
        "accounts": accounts,
    }


def matcher_lp(m: dict[str, Any]) -> dict[str, Any] | None:
    """Port of matcherLp: first LP account bound to the GM8z matcher program."""
    for a in m["accounts"]:
        if a["kind"] == "lp" and a["matcher_program"] == GM8Z_MATCHER_PROGRAM_ID:
            return a
    return None


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    lp = matcher_lp(m)
    im = m["initial_margin_bps"]
    return {
        "slab": m["address"],
        "program_id": GM8Z_PROGRAM_ID,
        "admin": m["admin"],
        "collateral_mint": m["collateral_mint"],
        "vault": m["vault"],
        "oracle": "authority-push" if m["index_feed_id"] == ZERO_KEY else "pyth",
        "index_feed_id": m["index_feed_id"],
        "oracle_authority": m["oracle_authority"],
        "price_e6": m["price_e6"],
        "vault_units": m["vault_units"],
        "insurance": m["insurance"],
        "open_interest": m["total_open_interest"],
        "accounts": m["num_used"],
        "initial_margin_bps": im,
        "maintenance_margin_bps": m["maintenance_margin_bps"],
        "trading_fee_bps": m["trading_fee_bps"],
        "max_leverage": round(10_000 / im, 2) if im else None,
        "resolved": m["resolved"],
        "invert": m["invert"],
        "lp": None if lp is None else {
            "idx": lp["idx"], "owner": lp["owner"],
            "matcher_program": lp["matcher_program"], "matcher_context": lp["matcher_context"],
        },
    }


def _err(tool: str, e: Exception) -> dict[str, Any]:
    return {"ok": False, "tool": tool, "error": str(e)[:400], "speech": f"{tool} failed: {str(e)[:120]}"}


def _fetch_market(rpc: Rpc, slab: str) -> dict[str, Any]:
    a = rpc.account(slab)
    if a is None:
        raise ValueError(f"slab {slab} not found")
    if a[0] != GM8Z_PROGRAM_ID:
        raise ValueError(f"slab owner {a[0]} is not the GM8z program {GM8Z_PROGRAM_ID}")
    m = decode_gm8z_slab(slab, a[1])
    if m is None:
        raise ValueError(f"account {slab} is not a GM8z slab (magic/length check failed)")
    return m


def _list_markets(limit: int) -> list[dict[str, Any]]:
    rpc = rpc_for("mainnet")
    accts = rpc.call("getProgramAccounts", [GM8Z_PROGRAM_ID, {
        "encoding": "base64", "commitment": "confirmed",
        "filters": [{"dataSize": SLAB_LEN}]}])
    out = []
    for a in accts:
        try:
            m = decode_gm8z_slab(a["pubkey"], base64.b64decode(a["account"]["data"][0]))
        except Exception:
            continue
        if m is not None:
            out.append(_summary(m))
    out.sort(key=lambda s: s["vault_units"], reverse=True)
    return out[:limit]


def register_gm8z(tool: Callable[..., Any]) -> list[str]:
    """Register the percolator_gm8z_* READ tools. Mainnet-only, read-only."""

    @tool()
    async def percolator_gm8z_list_markets(limit: int = 50) -> dict[str, Any]:
        """List GM8z (toly's percolator program on Solana MAINNET) perps markets/slabs. Read-only."""
        try:
            rows = await asyncio.to_thread(_list_markets, limit)
        except Exception as e:
            return _err("percolator_gm8z_list_markets", e)
        return {"ok": True, "cluster": "mainnet", "program_id": GM8Z_PROGRAM_ID,
                "matcher_program_id": GM8Z_MATCHER_PROGRAM_ID, "count": len(rows), "markets": rows,
                "speech": f"{len(rows)} GM8z mainnet market(s)."}

    @tool()
    async def percolator_gm8z_read_market(slab: str, include_accounts: bool = False) -> dict[str, Any]:
        """Decode a GM8z mainnet slab: config, oracle state, engine totals (optionally accounts). Read-only."""
        try:
            m = await asyncio.to_thread(_fetch_market, rpc_for("mainnet"), slab)
        except Exception as e:
            return _err("percolator_gm8z_read_market", e)
        s = _summary(m)  # LP scan needs the full account list — compute before trimming
        if not include_accounts:
            m = {**m, "accounts": [], "accounts_omitted": len(m["accounts"])}
        return {"ok": True, "cluster": "mainnet", "program_id": GM8Z_PROGRAM_ID, "summary": s, "market": m,
                "speech": f"GM8z market {slab[:6]}: price_e6 {m['price_e6']}, OI {m['total_open_interest']}, "
                          f"{m['num_used']} accounts."}

    @tool()
    async def percolator_gm8z_oracle_price(slab: str) -> dict[str, Any]:
        """Current GM8z mainnet mark price: admin authority push or Pyth feed (no pool oracle exists). Read-only."""
        try:
            m = await asyncio.to_thread(_fetch_market, rpc_for("mainnet"), slab)
        except Exception as e:
            return _err("percolator_gm8z_oracle_price", e)
        authority = m["index_feed_id"] == ZERO_KEY
        out = {"ok": True, "cluster": "mainnet", "program_id": GM8Z_PROGRAM_ID, "slab": slab,
               "source": "authority-push" if authority else "pyth",
               "index_feed_id": m["index_feed_id"], "oracle_authority": m["oracle_authority"],
               "price_e6": m["price_e6"], "authority_price_e6": m["authority_price_e6"],
               "last_effective_price_e6": m["last_effective_price_e6"],
               "timestamp": m["authority_timestamp"], "resolved": m["resolved"]}
        if authority:
            out["warning"] = "authority-push oracle: a single key (the market admin's oracle authority) sets this price"
        out["speech"] = f"GM8z mark price_e6 {out['price_e6']} from {out['source']}."
        return out

    return ["percolator_gm8z_list_markets", "percolator_gm8z_read_market", "percolator_gm8z_oracle_price"]
