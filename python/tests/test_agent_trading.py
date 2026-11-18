"""Unit tests for the unsigned devnet trade builders, positions, and quote math. No network unless
PERCOLATOR_DEVNET_RPC_URL (or RUN_DEVNET_SIM=1) is set, for the simulation test at the bottom."""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

import percolator_venue as V  # noqa: E402
from clawd_perps import ix as I  # noqa: E402
from clawd_perps import risk as R  # noqa: E402

PROG = Pubkey.from_string(V.DEFAULT_PROGRAM_ID)
SLAB = Pubkey.from_string("7KgTRxE14Rii5kR7YpPuCfKKQSceXoZNorhMtunRJDy3")
USER = Pubkey.from_string("ATMUVvuDVBuPWkBnpDhcHNXSNVtEHooRxNYfwpreBiGT")
LP_OWNER = "GCkyu5yFPdxNY7jJ9R5kNVM1Hp8EWRpSdn4DBv4JmXqZ"
CTX = "6SM7fC1kKYv4nBVUpdmYhrr4zZ2A6M2ZBhs4ZxmADr4A"
ZERO = "11111111111111111111111111111111"
BH = "4jW1BkMcyMfzXBzZkhPWD2e6K7xkAuvyrNypsZi7aXW3"


def market(kind=0, rate=0, accounts=None, auth_ts=1_000, max_stale=3600, price=1_580_000):
    return {
        "slab": str(SLAB), "collateral_mint": "CTjhnHRb85g9m3LwZpfPKkoteKeFZcuX7VHfKeCVFxmW",
        "vault": "CZXGH62x18zkpbzthFPAdB5tS9dxeML1zwUp191AFGz3", "index_feed_id": str(SLAB), "invert": 0,
        "admin": ZERO, "tier": "small", "resolved": False, "authority_price_e6": price,
        "authority_timestamp": auth_ts, "max_staleness_secs": max_stale, "last_effective_price_e6": price,
        "pool": {"kind": kind, "kind_name": "admin-oracle" if not kind else "pumpswap", "paused": False,
                 "migrated": False, "ema_price_e6": price, "base_vault": str(USER), "quote_vault": str(USER)},
        "engine": {"total_open_interest": 0, "insurance": 0, "num_used_accounts": 3,
                   "funding_index_e6": 0, "funding_rate_bps_per_slot": rate, "last_funding_slot": 100,
                   "params": {"initial_margin_bps": 1000, "maintenance_margin_bps": 500, "trading_fee_bps": 10,
                              "liquidation_fee_bps": 50}},
        "accounts": accounts if accounts is not None else [
            {"idx": 0, "kind": "lp", "owner": LP_OWNER, "matcher_program": ZERO, "matcher_context": ZERO,
             "position_size": 0, "entry_price_e6": 0, "capital": 0, "pnl": 0, "funding_index_e6": 0},
            {"idx": 1, "kind": "user", "owner": str(USER), "matcher_program": ZERO, "matcher_context": ZERO,
             "position_size": 2_000_000_000, "entry_price_e6": 1_580_000, "capital": 4_183_866_592, "pnl": 0,
             "funding_index_e6": 0},
            {"idx": 2, "kind": "lp", "owner": LP_OWNER, "matcher_program": str(I.MATCHER_PROGRAM),
             "matcher_context": CTX, "position_size": -2_000_000_000, "entry_price_e6": 1_580_000,
             "capital": 1, "pnl": 0, "funding_index_e6": 0},
        ],
    }


# ---------------------------------------------------------------- risk math
def test_liquidation_long_short_match_closed_form():
    # 10x long at 1.58: equity = im (fee excluded), mm 5% -> P = e*(1-0.1)/(1-0.05)
    assert R.liquidation_price_e6(1_000_000, 1_580_000, 158_000, 500) == int((1_580_000 - 158_000) / 0.95)
    assert R.liquidation_price_e6(-1_000_000, 1_580_000, 158_000, 500) == int((158_000 + 1_580_000) / 1.05)
    assert R.liquidation_price_e6(1_000_000, 1_000_000, 5_000_000, 500) is None  # overcollateralized long
    assert R.liquidation_price_e6(0, 1, 1, 500) is None


def test_liquidation_price_is_where_engine_check_flips():
    size, entry, eq, mm = 3_000_000, 2_000_000, 900_000, 500
    p = R.liquidation_price_e6(size, entry, eq, mm)

    def above(px):
        equity = eq + R.mark_pnl(size, entry, px)
        return equity > abs(size) * px // 1_000_000 * mm // 10_000
    assert above(p + 50) and not above(p - 50)


def test_funding_sign_and_projection():
    f = R.funding_estimate(1_000_000, 2_000_000, 3)
    assert f["direction"] == "longs pay shorts" and f["payment_per_slot_units"] > 0
    assert R.funding_estimate(-1_000_000, 2_000_000, 3)["payment_per_slot_units"] < 0
    eng = {"funding_index_e6": 10, "funding_rate_bps_per_slot": 2, "last_funding_slot": 100}
    assert R.projected_funding_index(eng, 1_000_000, 110) == 10 + 1_000_000 * 2 * 10 // 10_000
    assert R.pending_funding(1_000_000, 0, 1) == 1  # payer rounds up
    assert R.pending_funding(-1_000_000, 0, 1) == -1


def test_quote_adds_liq_and_funding_and_keeps_old_fields():
    q = V.quote_from_market(market(), 1_000_000, now_ts=1_100)
    for k in ("notional_units", "initial_margin_units", "maintenance_margin_units", "fee_units", "max_leverage"):
        assert k in q
    assert q["notional_units"] == 1_580_000 and q["fee_units"] == 1_580
    assert q["liquidation_price_e6"] == R.liquidation_price_e6(1_000_000, 1_580_000, 158_000, 500)
    assert q["funding"]["direction"] == "no funding"
    assert q["tradable"] is True


def test_stale_admin_oracle_marks_untradable():
    q = V.quote_from_market(market(auth_ts=0, max_stale=3600), 1_000_000, now_ts=10_000)
    assert q["tradable"] is False and q["oracle_status"]["stale"] is True


# ---------------------------------------------------------------- positions
def test_positions_for_owner():
    rows = V.positions_for_owner([market()], str(USER), now_slot=200)
    assert len(rows) == 1
    r = rows[0]
    assert r["side"] == "long" and r["size"] == 2_000_000_000 and r["collateral_units"] == 4_183_866_592
    assert r["unrealized_pnl_units"] == 0 and r["liquidatable"] is False
    assert r["liquidation_price_e6"] is None  # >100% collateralised
    assert V.positions_for_owner([market()], LP_OWNER) == []  # LP accounts are not user positions


def test_positions_upnl_includes_mark_and_funding():
    m = market(rate=1)
    m["accounts"][1].update(position_size=1_000_000, capital=200_000, entry_price_e6=1_500_000)
    r = V.positions_for_owner([m], str(USER), now_slot=101)[0]
    gfi = 1_580_000 * 1 * 1 // 10_000
    assert r["mark_pnl_units"] == 80_000
    assert r["pending_funding_units"] == R.pending_funding(1_000_000, 0, gfi)
    assert r["unrealized_pnl_units"] == 80_000 - r["pending_funding_units"]
    assert r["liquidation_price_e6"] == R.liquidation_price_e6(1_000_000, 1_500_000, 200_000 - r["pending_funding_units"], 500)


# ---------------------------------------------------------------- unsigned builders
def _ixs(m, size=1_000_000, deposit=0):
    tp = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    oracle = Pubkey.from_string(m["index_feed_id"]) if m["pool"]["kind"] else SLAB
    mint = Pubkey.from_string(m["collateral_mint"])
    return V.trade_ixs(m, PROG, SLAB, USER, I.ata(USER, mint, tp), Pubkey.from_string(m["vault"]), tp, oracle,
                       size, 1, deposit_amount=deposit)


def test_trade_ixs_order_tags_and_only_user_signs():
    ixs = _ixs(market(), deposit=5)
    assert [bytes(i.data)[0] for i in ixs] == [5, 3, 10]  # crank, deposit, TradeCpi (admin oracle: no pool update)
    pool = _ixs(market(kind=3))
    assert [bytes(i.data)[0] for i in pool] == [23, 5, 10]
    cpi = ixs[-1]
    assert [str(a.pubkey) for a in cpi.accounts if a.is_signer] == [str(USER)]
    assert str(cpi.accounts[5].pubkey) == str(I.MATCHER_PROGRAM) and str(cpi.accounts[6].pubkey) == CTX
    assert str(cpi.accounts[7].pubkey) == str(I.lp_pda(PROG, SLAB, 2))
    d = bytes(cpi.data)
    assert int.from_bytes(d[1:3], "little") == 2 and int.from_bytes(d[3:5], "little") == 1
    assert int.from_bytes(d[5:21], "little", signed=True) == 1_000_000


def test_trade_needs_matcher_lp():
    m = market()
    m["accounts"] = [a for a in m["accounts"] if a["idx"] != 2]
    with pytest.raises(ValueError, match="matcher"):
        _ixs(m)
    with pytest.raises(ValueError, match="not matcher-backed"):
        V.matcher_lp(market(), 0)


def test_build_unsigned_has_user_fee_payer_and_no_signatures():
    built = V.build_unsigned(_ixs(market()), USER, BH)
    tx = Transaction.from_bytes(base64.b64decode(built["transaction_base64"]))
    assert str(tx.message.account_keys[0]) == str(USER)
    assert built["signers_needed"] == [str(USER)]
    assert all(bytes(s) == bytes(64) for s in tx.signatures)
    assert str(tx.message.recent_blockhash) == BH


def test_write_gates(monkeypatch):
    monkeypatch.delenv("PERCOLATOR_MAINNET_WRITES_ENABLED", raising=False)
    assert V.write_gate("devnet", False, "") is None
    assert V.write_gate("mainnet", True, V.CONFIRM_TOKEN)["gate"] == "mainnet-writes-disabled"
    monkeypatch.setenv("PERCOLATOR_MAINNET_WRITES_ENABLED", "true")
    assert V.write_gate("mainnet", False, "")["gate"] == "mainnet-confirm-required"
    assert V.write_gate("mainnet", True, V.CONFIRM_TOKEN) is None


def test_server_keypair_env_refuses(monkeypatch):
    monkeypatch.setenv("PERCOLATOR_DEVNET_KEYPAIR", "/nonexistent")
    out = V._sync_write("devnet", [], USER, [], False, "", {})
    assert out["refused"] and out["gate"] == "server-keypair-forbidden"


def test_no_send_path_in_module():
    src = (Path(V.__file__)).read_text()
    assert "send_b64" not in src and "load_keypair" not in src


# ---------------------------------------------------------------- devnet simulation (network)
RPC = os.environ.get("PERCOLATOR_DEVNET_RPC_URL")


@pytest.mark.skipif(not (RPC or os.environ.get("RUN_DEVNET_SIM")), reason="set PERCOLATOR_DEVNET_RPC_URL or RUN_DEVNET_SIM=1")
def test_devnet_simulate_open_on_pool_market():
    tools = {}

    def tool(*a, **k):
        def d(fn):
            tools[fn.__name__] = fn
            return fn
        return d
    V.register(tool)
    slab = os.environ.get("PERCOLATOR_SIM_SLAB", "6GvLYn4qepjipw8E4cBHN2sXEQgXfKGDH9FLYqC9zzpU")
    rm = asyncio.run(tools["percolator_read_market"](slab, include_accounts=True))
    owner = next(a["owner"] for a in rm["market"]["accounts"] if a["kind"] == "user")
    out = asyncio.run(tools["percolator_open_position"](slab, owner, -1_000_000))
    assert out["submitted"] is False and out["server_signed"] is False
    assert out["signers_needed"] == [owner]
    assert out["simulation"]["err"] is None, out["simulation"]
    assert out["simulation"]["units"] > 0
