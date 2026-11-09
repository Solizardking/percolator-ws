"""Venue detection + reserve-based pricing that mirrors the on-chain pool_oracle module.

Venues: pump.fun bonding curve (kind 1), Raydium LaunchLab incl. Stonk.fun launches (kind 2),
PumpSwap AMM (kind 3). Stonk.fun public API needs no API key (per-IP rate limits).
"""
from __future__ import annotations

import struct

from solders.pubkey import Pubkey

from . import ix as I
from .rpc import Rpc, http_json

STONK_API = "https://stonkfun.xyz/api/public/v1"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"

DISC_BONDING_CURVE = bytes.fromhex("17b7f83760d8ac60")
DISC_LAUNCHLAB_POOL = bytes.fromhex("f7ede3f5d7c3de46")
DISC_PUMPSWAP_POOL = bytes.fromhex("f19a6d0411b16dbc")


def _u64(d: bytes, o: int) -> int:
    return struct.unpack_from("<Q", d, o)[0]


def _key(d: bytes, o: int) -> str:
    return str(Pubkey.from_bytes(d[o:o + 32]))


def ratio_e6(quote: int, base: int, pow10: int) -> int:
    if base == 0 or pow10 > 12:
        raise ValueError("bad ratio")
    return quote * 10 ** (6 + pow10) // base


def obs_pump_curve(d: bytes, pow10: int) -> dict:
    if d[:8] != DISC_BONDING_CURVE:
        raise ValueError("not a pump bonding curve")
    vt, vs, rt, rs = (_u64(d, o) for o in (8, 16, 24, 32))
    if d[48]:
        return {"migrated": True, "raw_price_e6": 0, "quote_liquidity": 0}
    return {"migrated": False, "raw_price_e6": ratio_e6(vs, vt, pow10), "quote_liquidity": rs,
            "reserves": {"virtual_token": vt, "virtual_sol": vs, "real_token": rt, "real_sol": rs}}


def obs_launchlab(d: bytes, pow10: int) -> dict:
    if d[:8] != DISC_LAUNCHLAB_POOL:
        raise ValueError("not a LaunchLab PoolState")
    status = d[17]
    vb, vq, rb, rq = (_u64(d, o) for o in (37, 45, 53, 61))
    out = {"base_mint": _key(d, 205), "quote_mint": _key(d, 237), "base_vault": _key(d, 269),
           "quote_vault": _key(d, 301), "creator": _key(d, 333), "status": status,
           "migrated": status != 0, "quote_liquidity": rq,
           "reserves": {"virtual_base": vb, "virtual_quote": vq, "real_base": rb, "real_quote": rq}}
    out["raw_price_e6"] = 0 if status else ratio_e6(vq + rq, vb - rb, pow10)
    return out


def obs_pumpswap(pool: bytes, base_vault: bytes, quote_vault: bytes, pow10: int) -> dict:
    if pool[:8] != DISC_PUMPSWAP_POOL:
        raise ValueError("not a PumpSwap pool")
    b, q = _u64(base_vault, 64), _u64(quote_vault, 64)
    return {"migrated": False, "raw_price_e6": ratio_e6(q, b, pow10), "quote_liquidity": q,
            "base_mint": _key(pool, 43), "quote_mint": _key(pool, 75), "base_vault": _key(pool, 139),
            "quote_vault": _key(pool, 171), "reserves": {"base": b, "quote": q}}


def apply_invert(raw_e6: int, invert: int) -> int:
    if not invert:
        return raw_e6
    return 10 ** 12 // raw_e6 if raw_e6 else 0


def stonk_token(mint: str) -> dict | None:
    """GET /api/public/v1/tokens/{mint} -> mint, pool, launchpad, status, quote.mint, transferFee.bps, feeModel."""
    try:
        out = http_json(f"{STONK_API}/tokens/{mint}")
    except Exception:
        return None
    data = out.get("data", out)
    return data.get("token", data) if isinstance(data, dict) else None


def pump_curve_pda(mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], I.PUMP_PROGRAM)[0]


def pumpswap_canonical_pool(mint: Pubkey, quote: Pubkey = I.WSOL) -> Pubkey:
    """Canonical pump.fun-migrated pool: ["pool", 0u16, pool-authority(mint), mint, quote]."""
    authority = Pubkey.find_program_address([b"pool-authority", bytes(mint)], I.PUMP_PROGRAM)[0]
    return Pubkey.find_program_address(
        [b"pool", (0).to_bytes(2, "little"), bytes(authority), bytes(mint), bytes(quote)], I.PUMPSWAP_PROGRAM)[0]


def launchlab_pool_pda(program: Pubkey, mint: Pubkey, quote: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"pool", bytes(mint), bytes(quote)], program)[0]


def detect_venue(mint_s: str, rpc: Rpc, cluster: str = "devnet", *, use_stonk: bool = True,
                 use_dexscreener: bool = True) -> dict:
    """Paste a mint -> {venue, kind, pool, pool_vaults?, observation, mint_info, notes}."""
    mint = Pubkey.from_string(mint_s)
    notes: list[str] = []
    m = rpc.account(mint_s)
    if m is None:
        raise ValueError(f"mint {mint_s} not found on {cluster}")
    mint_owner, mint_data, _ = m
    mint_info = {"token_program": mint_owner, "decimals": mint_data[44],
                 "token_2022": mint_owner == str(I.TOKEN_2022_PROGRAM)}

    stonk = stonk_token(mint_s) if (use_stonk and cluster == "mainnet") else None
    if stonk:
        notes.append(f"stonk.fun: launchpad={stonk.get('launchpad')} status={stonk.get('status')} "
                     f"quote={(stonk.get('quote') or {}).get('symbol')} transferFeeBps={(stonk.get('transferFee') or {}).get('bps', 0)}")

    # 1) pump.fun bonding curve
    curve = pump_curve_pda(mint)
    a = rpc.account(str(curve))
    if a and a[0] == str(I.PUMP_PROGRAM) and a[1][:8] == DISC_BONDING_CURVE:
        o = obs_pump_curve(a[1], 0)
        if not o["migrated"]:
            return {"venue": "pump.fun bonding curve", "kind": I.KIND_PUMP_CURVE, "pool": str(curve),
                    "quote_mint": str(I.WSOL), "observation": o, "mint_info": mint_info, "notes": notes, "stonk": stonk}
        notes.append("pump.fun curve complete (graduated) -> looking for PumpSwap pool")

    # 2) PumpSwap canonical pool, then DexScreener fallback
    candidates = [str(pumpswap_canonical_pool(mint))]
    if use_dexscreener and cluster == "mainnet":
        try:
            pairs = http_json(DEXSCREENER + mint_s).get("pairs") or []
            candidates += [p["pairAddress"] for p in pairs if p.get("dexId") == "pumpswap"]
        except Exception as e:  # pragma: no cover
            notes.append(f"dexscreener lookup failed: {e}")
    for c in dict.fromkeys(candidates):
        a = rpc.account(c)
        if a and a[0] == str(I.PUMPSWAP_PROGRAM) and a[1][:8] == DISC_PUMPSWAP_POOL and _key(a[1], 43) == mint_s:
            bv, qv = _key(a[1], 139), _key(a[1], 171)
            b, q = rpc.accounts([bv, qv])
            o = obs_pumpswap(a[1], b[1], q[1], 0)
            return {"venue": "PumpSwap", "kind": I.KIND_PUMPSWAP, "pool": c, "pool_vaults": [bv, qv],
                    "quote_mint": o["quote_mint"], "observation": o, "mint_info": mint_info, "notes": notes, "stonk": stonk}

    # 3) Raydium LaunchLab (Stonk.fun launches use non-SOL quotes, e.g. xStocks)
    ll_prog = I.LAUNCHLAB_PROGRAM["mainnet" if cluster == "mainnet" else "devnet"]
    quotes = [str(I.WSOL), "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
    if stonk and (stonk.get("quote") or {}).get("mint"):
        quotes.insert(0, stonk["quote"]["mint"])
    pools = [stonk["pool"]] if stonk and stonk.get("pool") else []
    pools += [str(launchlab_pool_pda(ll_prog, mint, Pubkey.from_string(q))) for q in quotes]
    for p in dict.fromkeys(pools):
        a = rpc.account(p)
        if a and a[0] == str(ll_prog) and a[1][:8] == DISC_LAUNCHLAB_POOL and _key(a[1], 205) == mint_s:
            o = obs_launchlab(a[1], 0)
            venue = "Stonk.fun (Raydium LaunchLab)" if stonk else "Raydium LaunchLab"
            if o["migrated"]:
                notes.append("LaunchLab pool migrated (CPMM); perps on graduated LaunchLab tokens not supported yet (future kind 4)")
            return {"venue": venue, "kind": I.KIND_LAUNCHLAB, "pool": p, "quote_mint": o["quote_mint"],
                    "observation": o, "mint_info": mint_info, "notes": notes, "stonk": stonk}

    return {"venue": None, "kind": I.KIND_NONE, "pool": None, "mint_info": mint_info, "notes": notes + [
        "no supported pool found (pump curve / PumpSwap / LaunchLab)"], "stonk": stonk}


def eligibility(det: dict, *, min_quote_liquidity: int) -> dict:
    """Perps eligibility + the program's leverage cap for a detected venue."""
    o = det.get("observation") or {}
    if not det.get("kind"):
        return {"eligible": False, "reason": "no supported venue"}
    if o.get("migrated"):
        return {"eligible": False, "reason": "pool migrated"}
    liq = o.get("quote_liquidity", 0)
    if liq < max(min_quote_liquidity, 1_000_000):
        return {"eligible": False, "reason": f"quote liquidity {liq} below floor {max(min_quote_liquidity, 1_000_000)}"}
    stonk = det.get("stonk") or {}
    min_im = 5000 if liq < min_quote_liquidity * 10 else 2000
    return {"eligible": True, "min_initial_margin_bps": min_im, "max_leverage": round(10000 / min_im, 2),
            "transfer_fee_bps": (stonk.get("transferFee") or {}).get("bps", 0)}
