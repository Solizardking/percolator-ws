"""clawd-perps CLI. Devnet by default. Writes are dry-run unless --confirm; mainnet sends are refused.

  python -m clawd_perps detect <MINT> [--cluster mainnet]
  python -m clawd_perps read-market <SLAB>
  python -m clawd_perps quote <SLAB> --size N
  python -m clawd_perps create-market <MINT> --collateral <MINT> --bond N --keypair K [--confirm]
  python -m clawd_perps update-price <SLAB> --keypair K [--confirm]
  python -m clawd_perps init-user|deposit|withdraw|trade|crank ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from . import ix as I
from .layout import CLUSTERS, TIERS, decode_slab, slab_len
from .rpc import Rpc
from .tx import load_keypair, run
from .venue import apply_invert, detect_venue, eligibility

DEFAULT_PROGRAM = os.environ.get("CLAWD_PERCOLATOR_PROGRAM_ID", "Anw121Fk3gKvUHXk938cY2xVb9Eq46WX3k15WESS49PA")


def _rpc(a) -> Rpc:
    return Rpc(a.rpc or CLUSTERS[a.cluster])


def _pk(s) -> Pubkey:
    return Pubkey.from_string(s)


def auto_pow10(raw_e6_at_pow0: int, invert: int) -> int:
    """Pick pow10 so the program's integer price keeps >= ~6 significant digits."""
    for p in range(0, 13):
        raw = raw_e6_at_pow0 * 10 ** p if raw_e6_at_pow0 else 0
        if raw == 0:
            continue
        px = apply_invert(raw, invert)
        if invert:
            if px < 10 ** 6:
                return max(0, p - 1)
        elif raw >= 10 ** 6:
            return p
    return 6


def market_ctx(rpc: Rpc, program: Pubkey, slab: Pubkey) -> dict:
    a = rpc.account(str(slab))
    if a is None:
        raise SystemExit(f"slab {slab} not found")
    if a[0] != str(program):
        raise SystemExit(f"slab owner {a[0]} != program {program}")
    m = decode_slab(a[1])
    mint = rpc.account(m["collateral_mint"])
    m["token_program"] = mint[0]
    return m


def cmd_detect(a):
    d = detect_venue(a.mint, _rpc(a), a.cluster)
    d["eligibility"] = eligibility(d, min_quote_liquidity=a.min_liquidity)
    print(json.dumps(d, indent=2, default=str))


def cmd_read(a):
    print(json.dumps(market_ctx(_rpc(a), _pk(a.program), _pk(a.slab)), indent=2))


def cmd_quote(a):
    m = market_ctx(_rpc(a), _pk(a.program), _pk(a.slab))
    px = m["pool"]["ema_price_e6"] if m["pool"]["kind"] else m["last_effective_price_e6"] or m["authority_price_e6"]
    notional = abs(a.size) * px // 10 ** 6
    im = m["engine"]["params"]["initial_margin_bps"]
    fee = m["engine"]["params"]["trading_fee_bps"]
    print(json.dumps({"price_e6": px, "size": a.size, "notional_units": notional,
                      "initial_margin_units": notional * im // 10_000, "fee_units": notional * fee // 10_000,
                      "max_leverage": round(10_000 / im, 2) if im else None, "paused": m["pool"]["paused"],
                      "resolved": m["resolved"]}, indent=2))


def cmd_create(a):
    """Paste a mint -> detect venue -> one atomic tx: create slab, vault ATA, InitMarket, ConfigurePoolOracle, TopUpInsurance."""
    rpc, program = _rpc(a), _pk(a.program)
    payer = load_keypair(a.keypair)
    det = detect_venue(a.mint, rpc, a.cluster)
    elig = eligibility(det, min_quote_liquidity=a.min_liquidity)
    if not elig.get("eligible"):
        raise SystemExit(f"not eligible: {elig} notes={det.get('notes')}")
    collateral = _pk(a.collateral)
    cm = rpc.account(str(collateral))
    token_prog = _pk(cm[0])
    invert = 1 if a.invert else 0
    pow10 = a.pow10 if a.pow10 is not None else auto_pow10(det["observation"]["raw_price_e6"], invert)
    im = max(a.im_bps, elig["min_initial_margin_bps"])
    slab = Keypair()
    tier = a.tier
    size = slab_len(tier)
    vauth = I.vault_authority(program, slab.pubkey())
    vault = I.ata(vauth, collateral, token_prog)
    payer_ata = I.ata(payer.pubkey(), collateral, token_prog)
    pool = _pk(det["pool"])
    vaults = tuple(_pk(v) for v in det["pool_vaults"]) if det.get("pool_vaults") else None
    ixs = [
        I.create_account(payer.pubkey(), slab.pubkey(), rpc.rent(size), size, program),
        I.create_ata_idempotent(payer.pubkey(), vauth, collateral, token_prog),
        I.init_market(program, payer.pubkey(), slab.pubkey(), collateral, vault, token_prog, bytes(pool),
                      invert=invert, risk=I.risk_params(im_bps=im, mm_bps=a.mm_bps or im // 2,
                                                        max_accounts=TIERS[tier][0])),
        I.configure_pool_oracle(program, payer.pubkey(), slab.pubkey(), pool, _pk(a.mint), kind=det["kind"],
                                price_pow10=pow10, min_quote_liquidity=a.min_liquidity, bond_units=a.bond,
                                ema_alpha_bps=a.ema_alpha_bps, pool_vaults=vaults),
        I.top_up_insurance(program, payer.pubkey(), slab.pubkey(), payer_ata, vault, token_prog, a.bond),
    ]
    out = run(rpc, a.cluster, ixs, payer, [slab], confirm=a.confirm)
    out.update({"slab": str(slab.pubkey()), "vault": str(vault), "pool": str(pool), "venue": det["venue"],
                "kind": det["kind"], "price_pow10": pow10, "invert": invert, "im_bps": im, "bond": a.bond,
                "tier": tier, "slab_bytes": size, "rent_lamports": rpc.rent(size)})
    print(json.dumps(out, indent=2))


def cmd_update_price(a):
    rpc, program = _rpc(a), _pk(a.program)
    payer = load_keypair(a.keypair)
    m = market_ctx(rpc, program, _pk(a.slab))
    pool = _pk(m["index_feed_id"])
    vaults = (_pk(m["pool"]["base_vault"]), _pk(m["pool"]["quote_vault"])) if m["pool"]["kind"] == 3 else None
    ixs = [I.update_pool_price(program, payer.pubkey(), _pk(a.slab), pool, vaults)]
    print(json.dumps(run(rpc, a.cluster, ixs, payer, confirm=a.confirm), indent=2))


def _user_ixs(a, kind: str):
    rpc, program, slab = _rpc(a), _pk(a.program), _pk(a.slab)
    user = load_keypair(a.keypair)
    m = market_ctx(rpc, program, slab)
    tp, mint, vault = _pk(m["token_program"]), _pk(m["collateral_mint"]), _pk(m["vault"])
    uata = I.ata(user.pubkey(), mint, tp)
    oracle = _pk(m["index_feed_id"]) if m["pool"]["kind"] else slab
    signers = []
    if kind == "init-user":
        ixs = [I.init_user(program, user.pubkey(), slab, uata, vault, tp)]
    elif kind == "deposit":
        ixs = [I.deposit(program, user.pubkey(), slab, uata, vault, tp, a.idx, a.amount)]
    elif kind == "withdraw":
        ixs = [I.withdraw(program, user.pubkey(), slab, vault, uata, tp, oracle, a.idx, a.amount)]
    elif kind == "close":
        ixs = [I.close_account(program, user.pubkey(), slab, vault, uata, tp, oracle, a.idx)]
    elif kind == "crank":
        ixs = [I.keeper_crank(program, user.pubkey(), slab, oracle)]
    elif kind == "trade":
        lp = load_keypair(a.lp_keypair)
        signers = [lp]
        ixs = [I.trade_no_cpi(program, user.pubkey(), lp.pubkey(), slab, oracle, a.lp_idx, a.idx, a.size)]
    else:
        raise SystemExit(kind)
    print(json.dumps(run(rpc, a.cluster, ixs, user, signers, confirm=a.confirm), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="clawd-perps", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cluster", default=os.environ.get("CLAWD_CLUSTER", "devnet"), choices=list(CLUSTERS))
    p.add_argument("--rpc", default=os.environ.get("CLAWD_RPC_URL"))
    p.add_argument("--program", default=DEFAULT_PROGRAM)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("detect"); s.add_argument("mint"); s.add_argument("--min-liquidity", type=int, default=10**9)
    s.set_defaults(f=cmd_detect)
    s = sp.add_parser("read-market"); s.add_argument("slab"); s.set_defaults(f=cmd_read)
    s = sp.add_parser("quote"); s.add_argument("slab"); s.add_argument("--size", type=int, required=True)
    s.set_defaults(f=cmd_quote)

    s = sp.add_parser("create-market"); s.add_argument("mint")
    s.add_argument("--collateral", required=True); s.add_argument("--bond", type=int, required=True)
    s.add_argument("--keypair", required=True); s.add_argument("--tier", default="small", choices=["small", "prod"])
    s.add_argument("--im-bps", type=int, default=2000); s.add_argument("--mm-bps", type=int)
    s.add_argument("--min-liquidity", type=int, default=10**9); s.add_argument("--ema-alpha-bps", type=int, default=500)
    s.add_argument("--pow10", type=int); s.add_argument("--invert", action="store_true")
    s.add_argument("--confirm", action="store_true"); s.set_defaults(f=cmd_create)

    s = sp.add_parser("update-price"); s.add_argument("slab"); s.add_argument("--keypair", required=True)
    s.add_argument("--confirm", action="store_true"); s.set_defaults(f=cmd_update_price)

    for k in ("init-user", "deposit", "withdraw", "close", "crank", "trade"):
        s = sp.add_parser(k); s.add_argument("slab"); s.add_argument("--keypair", required=True)
        s.add_argument("--idx", type=int, default=0); s.add_argument("--amount", type=int, default=0)
        s.add_argument("--size", type=int, default=0); s.add_argument("--lp-idx", type=int, default=0)
        s.add_argument("--lp-keypair"); s.add_argument("--confirm", action="store_true")
        s.set_defaults(f=lambda a, k=k: _user_ixs(a, k))

    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
