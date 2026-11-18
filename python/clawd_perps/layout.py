"""Slab layout for the CLAWD Percolator fork (SBF, u128 align 8).

SlabHeader 72 B | MarketConfig 496 B | RiskEngine @ ENGINE_OFF=568.
Offsets are pinned by program/tests/clawd_fork.rs and the engine layout dump.
"""
from __future__ import annotations

import struct

from solders.pubkey import Pubkey

CLUSTERS = {
    "devnet": "https://api.devnet.solana.com",
    "localnet": "http://127.0.0.1:8899",
    # Read-only. The CLI refuses to *send* anything on mainnet.
    "mainnet": "https://api.mainnet-beta.solana.com",
}

HEADER_LEN = 72
CFG = 72
CFG_LEN = 496
ENGINE_OFF = 568
MAGIC = 0x504552434F4C4154  # "PERCOLAT" (unchanged from upstream for wire compatibility)

# tier -> (max_accounts, engine_size, num_used_off, accounts_off)
TIERS = {
    "test": (64, None, None, None),
    "small": (256, 62416, 440, 976),
    "prod": (4096, 992176, 920, 9136),
}
ACCOUNT_SIZE = 240
POOL_KINDS = {0: "admin-oracle", 1: "pump-bonding-curve", 2: "raydium-launchlab", 3: "pumpswap"}


def slab_len(tier: str = "small") -> int:
    return ENGINE_OFF + TIERS[tier][1]


def _b58(b: bytes) -> str:
    return str(Pubkey.from_bytes(bytes(b)))


def _u(fmt: str, d: bytes, off: int):
    return struct.unpack_from("<" + fmt, d, off)[0]


def _u128(d: bytes, off: int) -> int:
    return int.from_bytes(d[off:off + 16], "little")


def _i128(d: bytes, off: int) -> int:
    return int.from_bytes(d[off:off + 16], "little", signed=True)


def decode_slab(d: bytes) -> dict:
    if len(d) < ENGINE_OFF + 448:
        raise ValueError(f"slab too small: {len(d)}")
    tier = next((t for t, v in TIERS.items() if v[1] and len(d) in (ENGINE_OFF + v[1], ENGINE_OFF + v[1] - 8)), None)
    flags = d[13]
    c = CFG
    out = {
        "len": len(d),
        "tier": tier,
        "magic_ok": _u("Q", d, 0) == MAGIC,
        "version": _u("I", d, 8),
        "resolved": bool(flags & 1),
        "admin": _b58(d[16:48]),
        "collateral_mint": _b58(d[c:c + 32]),
        "vault": _b58(d[c + 32:c + 64]),
        "index_feed_id": _b58(d[c + 64:c + 96]),
        "max_staleness_secs": _u("Q", d, c + 96),
        "invert": d[c + 107],
        "unit_scale": _u("I", d, c + 108),
        "oracle_authority": _b58(d[c + 256:c + 288]),
        "authority_price_e6": _u("Q", d, c + 288),
        "authority_timestamp": _u("q", d, c + 296),
        "oracle_price_cap_e2bps": _u("Q", d, c + 304),
        "last_effective_price_e6": _u("Q", d, c + 312),
        "pending_admin": _b58(d[c + 320:c + 352]),
        "pool": {
            "kind": d[c + 416],
            "kind_name": POOL_KINDS.get(d[c + 416], "unknown"),
            "flags": d[c + 417],
            "paused": bool(d[c + 417] & 1),
            "migrated": bool(d[c + 417] & 2),
            "price_pow10": d[c + 418],
            "base_vault": _b58(d[c + 352:c + 384]),
            "quote_vault": _b58(d[c + 384:c + 416]),
            "min_quote_liquidity": _u("Q", d, c + 424),
            "min_insurance_units": _u("Q", d, c + 432),
            "ema_price_e6": _u("Q", d, c + 440),
            "ema_alpha_bps": _u("Q", d, c + 448),
            "last_update_slot": _u("Q", d, c + 456),
            "last_update_ts": _u("q", d, c + 464),
            "paused_since_slot": _u("Q", d, c + 472),
            "last_liquidity": _u("Q", d, c + 480),
        },
    }
    e = ENGINE_OFF
    out["engine"] = {
        "vault": _u128(d, e),
        "insurance": _u128(d, e + 16),
        "total_open_interest": _u128(d, e + 248),
        "c_tot": _u128(d, e + 264),
        "params": {
            "warmup_period_slots": _u("Q", d, e + 48),
            "maintenance_margin_bps": _u("Q", d, e + 56),
            "initial_margin_bps": _u("Q", d, e + 64),
            "trading_fee_bps": _u("Q", d, e + 72),
            "max_accounts": _u("Q", d, e + 80),
            "maintenance_fee_per_slot": _u128(d, e + 120),
            "max_crank_staleness_slots": _u("Q", d, e + 136),
            "liquidation_fee_bps": _u("Q", d, e + 144),
            "liquidation_fee_cap": _u128(d, e + 152),
            "liquidation_buffer_bps": _u("Q", d, e + 168),
            "min_liquidation_abs": _u128(d, e + 176),
        },
        # Funding (engine/src/percolator.rs RiskEngine): index is quote-per-base e6;
        # rate is bps per slot in effect since last_funding_slot (>0 => longs pay shorts).
        "current_slot": _u("Q", d, e + 192),
        "funding_index_e6": _i128(d, e + 200),
        "last_funding_slot": _u("Q", d, e + 216),
        "funding_rate_bps_per_slot": _u("q", d, e + 224),
        "last_crank_slot": _u("Q", d, e + 232),
    }
    if tier:
        _, _, nu, ao = TIERS[tier]
        out["engine"]["num_used_accounts"] = _u("H", d, e + nu)
        accts = []
        bitmap_words = TIERS[tier][0] // 64
        for w in range(bitmap_words):
            word = _u("Q", d, e + 408 + 8 * w)
            while word:
                b = (word & -word).bit_length() - 1
                idx = w * 64 + b
                a = e + ao + idx * ACCOUNT_SIZE
                accts.append({
                    "idx": idx,
                    "kind": "lp" if d[a + 24] == 1 else "user",
                    "capital": _u128(d, a + 8),
                    "pnl": _i128(d, a + 32),
                    "position_size": _i128(d, a + 80),
                    "entry_price_e6": _u("Q", d, a + 96),
                    "funding_index_e6": _i128(d, a + 104),
                    "matcher_program": _b58(d[a + 120:a + 152]),
                    "matcher_context": _b58(d[a + 152:a + 184]),
                    "owner": _b58(d[a + 184:a + 216]),
                })
                word &= word - 1
        out["accounts"] = accts
    return out
