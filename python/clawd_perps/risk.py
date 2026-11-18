"""Pure risk math for CLAWD Percolator positions and quotes (no RPC, no keys).

Mirrors the engine (engine/src/percolator.rs in Solizardking/clawd-market):

* mark PnL      = size * (P - entry) / 1e6                      (mark_pnl_for_position; size signed)
* equity_mtm(P) = capital + pnl + mark_pnl(P) - pending_funding (account_equity_mtm_at_oracle; the
                  positive-PnL haircut is assumed 1, so this is the optimistic side for pnl > 0)
* liquidatable  when equity_mtm(P) <= |size| * P / 1e6 * mm_bps / 1e4   (is_above_maintenance_margin_mtm)
* funding       ΔF = P * rate_bps_per_slot * Δslots / 1e4 (accrue_funding); an account pays
                  size * (F_global - F_account) / 1e6 (settle_account_funding). rate > 0 => longs pay.

Prices are engine e6 units (already inverted for invert=1 markets); sizes are base units;
capital/pnl/fees are collateral base units.

Liquidation price (solve equity_mtm(P) = maintenance(P), m = mm_bps / 1e4, E = capital + pnl - funding):
  long  (q > 0):  P_liq = (q * entry - E * 1e6) / (q * (1 - m))
  short (q < 0):  P_liq = (E * 1e6 + |q| * entry) / (|q| * (1 + m))
A long whose P_liq <= 0 cannot be liquidated by price alone (returned as None).
"""
from __future__ import annotations

from typing import Any, Optional

E6 = 1_000_000
BPS = 10_000
# Solana targets ~400 ms slots. Used only for per-hour/day funding estimates.
SLOTS_PER_HOUR = 9_000
SLOTS_PER_DAY = SLOTS_PER_HOUR * 24


def mark_pnl(size: int, entry_e6: int, price_e6: int) -> int:
    if size == 0:
        return 0
    diff = (price_e6 - entry_e6) if size > 0 else (entry_e6 - price_e6)
    return int(diff * abs(size) / E6)


def projected_funding_index(engine: dict[str, Any], price_e6: int, now_slot: Optional[int] = None) -> int:
    """Global funding index projected to now_slot at the stored rate (engine accrues lazily on crank/touch)."""
    fi = int(engine.get("funding_index_e6") or 0)
    rate = int(engine.get("funding_rate_bps_per_slot") or 0)
    last = int(engine.get("last_funding_slot") or 0)
    if now_slot is None or now_slot <= last or rate == 0:
        return fi
    return fi + price_e6 * rate * (now_slot - last) // BPS


def pending_funding(size: int, account_fi: int, global_fi: int) -> int:
    """Funding the account owes (+) or is owed (-) since its last settlement, collateral units."""
    raw = size * (global_fi - account_fi)
    if raw > 0:
        return (raw + E6 - 1) // E6
    return -((-raw) // E6)


def liquidation_price_e6(size: int, entry_e6: int, equity_units: int, mm_bps: int) -> Optional[int]:
    if size == 0:
        return None
    m = mm_bps / BPS
    q = abs(size)
    if size > 0:
        p = (q * entry_e6 - equity_units * E6) / (q * (1 - m))
        return int(p) if p > 0 else None
    return int((equity_units * E6 + q * entry_e6) / (q * (1 + m)))


def funding_estimate(size: int, price_e6: int, rate_bps_per_slot: int) -> dict[str, Any]:
    per_slot = size * price_e6 * rate_bps_per_slot / BPS / E6
    return {
        "rate_bps_per_slot": rate_bps_per_slot,
        "direction": "longs pay shorts" if rate_bps_per_slot > 0 else
                     ("shorts pay longs" if rate_bps_per_slot < 0 else "no funding"),
        "payment_per_slot_units": per_slot,
        "payment_per_hour_units": per_slot * SLOTS_PER_HOUR,
        "payment_per_day_units": per_slot * SLOTS_PER_DAY,
        "sign": "positive = this position pays",
        "note": "Rate is the last value set at crank (inventory-based); it can change on the next KeeperCrank. "
                f"Hour/day assume {SLOTS_PER_HOUR} slots/hour.",
    }


def quote(size: int, price_e6: int, params: dict[str, Any], rate_bps_per_slot: int,
          collateral_units: Optional[int] = None) -> dict[str, Any]:
    """Quote a fresh position at the mark. collateral defaults to initial margin + fee (max leverage)."""
    notional = abs(size) * price_e6 // E6
    im = notional * params["initial_margin_bps"] // BPS
    mm = notional * params["maintenance_margin_bps"] // BPS
    fee = notional * params["trading_fee_bps"] // BPS
    coll = im + fee if collateral_units is None else int(collateral_units)
    eq = coll - fee
    liq = liquidation_price_e6(size, price_e6, eq, params["maintenance_margin_bps"])
    return {
        "notional_units": notional, "initial_margin_units": im, "maintenance_margin_units": mm, "fee_units": fee,
        "collateral_units": coll, "meets_initial_margin": eq >= im,
        "effective_leverage": round(notional / eq, 3) if eq > 0 else None,
        "liquidation_price_e6": liq,
        "liquidation_fee_bps": params.get("liquidation_fee_bps"),
        "funding": funding_estimate(size, price_e6, rate_bps_per_slot),
        "assumptions": "Entry at the current mark; the matcher's spread/fee moves the real fill slightly. "
                       "Liquidation: equity_mtm <= |size|*P*mm_bps/1e4/1e6 with equity = collateral - fee.",
    }


def position(acct: dict[str, Any], engine: dict[str, Any], price_e6: int, now_slot: Optional[int] = None) -> dict[str, Any]:
    size = int(acct["position_size"])
    entry = int(acct["entry_price_e6"])
    capital = int(acct["capital"])
    pnl = int(acct["pnl"])
    gfi = projected_funding_index(engine, price_e6, now_slot)
    fund = pending_funding(size, int(acct.get("funding_index_e6") or 0), gfi)
    mpnl = mark_pnl(size, entry, price_e6)
    upnl = pnl + mpnl - fund
    equity = max(0, capital + upnl)
    prm = engine["params"]
    notional = abs(size) * price_e6 // E6
    maint = notional * prm["maintenance_margin_bps"] // BPS
    return {
        "idx": acct["idx"], "side": "long" if size > 0 else ("short" if size < 0 else "flat"), "size": size,
        "entry_price_e6": entry, "mark_price_e6": price_e6, "collateral_units": capital,
        "settled_pnl_units": pnl, "mark_pnl_units": mpnl, "pending_funding_units": fund,
        "unrealized_pnl_units": upnl, "equity_units": equity, "notional_units": notional,
        "maintenance_margin_units": maint,
        "margin_ratio": round(equity / notional, 6) if notional else None,
        "liquidatable": bool(size) and equity <= maint,
        "liquidation_price_e6": liquidation_price_e6(size, entry, capital + pnl - fund, prm["maintenance_margin_bps"]),
        "funding": funding_estimate(size, price_e6, int(engine.get("funding_rate_bps_per_slot") or 0)) if size else None,
        "entry_note": "entry_price_e6 is the last mark the position was settled at (variation margin), "
                      "not an average fill price; realized moves sit in settled_pnl_units.",
    }
