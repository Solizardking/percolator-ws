"""Instruction builders for the CLAWD Percolator program (1-byte tag wire format)."""
from __future__ import annotations

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

SYSVAR_CLOCK = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
SYSVAR_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPSWAP_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
LAUNCHLAB_PROGRAM = {
    "mainnet": Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"),
    # Devnet build of the program (feature `devnet`) expects this owner; unverified as LaunchLab.
    "devnet": Pubkey.from_string("DRay6fNdQ5J82H7xV6uq2aV3mNrUZ1J4PgSKsWgptcm6"),
}
WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")

KIND_NONE, KIND_PUMP_CURVE, KIND_LAUNCHLAB, KIND_PUMPSWAP = 0, 1, 2, 3


def _w(k: Pubkey) -> AccountMeta:
    return AccountMeta(k, is_signer=False, is_writable=True)


def _r(k: Pubkey) -> AccountMeta:
    return AccountMeta(k, is_signer=False, is_writable=False)


def _s(k: Pubkey, writable: bool = True) -> AccountMeta:
    return AccountMeta(k, is_signer=True, is_writable=writable)


def vault_authority(program_id: Pubkey, slab: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"vault", bytes(slab)], program_id)[0]


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(token_program), bytes(mint)], ATA_PROGRAM)[0]


def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Instruction:
    return Instruction(ATA_PROGRAM, bytes([1]), [
        _s(payer), _w(ata(owner, mint, token_program)), _r(owner), _r(mint), _r(SYSTEM_PROGRAM), _r(token_program)])


def create_account(payer: Pubkey, new: Pubkey, lamports: int, space: int, owner: Pubkey) -> Instruction:
    data = struct.pack("<IQQ", 0, lamports, space) + bytes(owner)
    return Instruction(SYSTEM_PROGRAM, data, [_s(payer), _s(new)])


def risk_params(*, im_bps: int, mm_bps: int, max_accounts: int, trading_fee_bps: int = 10, warmup_slots: int = 0,
                new_account_fee: int = 0, risk_reduction_threshold: int = 0, maintenance_fee_per_slot: int = 0,
                max_crank_staleness_slots: int = 2**64 - 1, liquidation_fee_bps: int = 50,
                liquidation_fee_cap: int = 10**12, liquidation_buffer_bps: int = 100, min_liquidation_abs: int = 0) -> bytes:
    u128 = lambda v: int(v).to_bytes(16, "little")  # noqa: E731
    return (struct.pack("<QQQQQ", warmup_slots, mm_bps, im_bps, trading_fee_bps, max_accounts)
            + u128(new_account_fee) + u128(risk_reduction_threshold) + u128(maintenance_fee_per_slot)
            + struct.pack("<QQ", max_crank_staleness_slots, liquidation_fee_bps) + u128(liquidation_fee_cap)
            + struct.pack("<Q", liquidation_buffer_bps) + u128(min_liquidation_abs))


def init_market(program_id: Pubkey, admin: Pubkey, slab: Pubkey, mint: Pubkey, vault: Pubkey, token_program: Pubkey,
                index_feed_id: bytes, *, invert: int, risk: bytes, max_staleness_secs: int = 3600,
                conf_filter_bps: int = 500, unit_scale: int = 0, initial_mark_price_e6: int = 0) -> Instruction:
    assert len(index_feed_id) == 32
    data = (bytes([0]) + bytes(admin) + bytes(mint) + index_feed_id
            + struct.pack("<QHBIQ", max_staleness_secs, conf_filter_bps, invert, unit_scale, initial_mark_price_e6) + risk)
    return Instruction(program_id, data, [
        _s(admin), _w(slab), _r(mint), _w(vault), _r(token_program), _r(SYSVAR_CLOCK), _r(SYSVAR_RENT),
        _r(vault_authority(program_id, slab)), _r(SYSTEM_PROGRAM)])


def configure_pool_oracle(program_id: Pubkey, admin: Pubkey, slab: Pubkey, pool: Pubkey, base_mint: Pubkey, *,
                          kind: int, price_pow10: int, min_quote_liquidity: int, bond_units: int,
                          ema_alpha_bps: int = 500, pool_vaults: tuple[Pubkey, Pubkey] | None = None) -> Instruction:
    data = bytes([25, kind, price_pow10]) + struct.pack("<QQQ", min_quote_liquidity, bond_units, ema_alpha_bps)
    metas = [_s(admin, False), _w(slab), _r(SYSVAR_CLOCK), _r(pool), _r(base_mint)]
    if kind == KIND_PUMPSWAP:
        assert pool_vaults, "PumpSwap needs (base_vault, quote_vault)"
        metas += [_r(pool_vaults[0]), _r(pool_vaults[1])]
    return Instruction(program_id, data, metas)


def update_pool_price(program_id: Pubkey, caller: Pubkey, slab: Pubkey, pool: Pubkey,
                      pool_vaults: tuple[Pubkey, Pubkey] | None = None) -> Instruction:
    metas = [_s(caller, False), _w(slab), _r(SYSVAR_CLOCK), _r(pool)]
    if pool_vaults:
        metas += [_r(pool_vaults[0]), _r(pool_vaults[1])]
    return Instruction(program_id, bytes([23]), metas)


def resolve_pool_market(program_id: Pubkey, caller: Pubkey, slab: Pubkey, pool: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([24]), [_s(caller, False), _w(slab), _r(SYSVAR_CLOCK), _r(pool)])


def init_user(program_id: Pubkey, user: Pubkey, slab: Pubkey, user_ata: Pubkey, vault: Pubkey, token_program: Pubkey,
              fee: int = 0) -> Instruction:
    return Instruction(program_id, bytes([1]) + struct.pack("<Q", fee), [
        _s(user), _w(slab), _w(user_ata), _w(vault), _r(token_program)])


def init_lp(program_id: Pubkey, owner: Pubkey, slab: Pubkey, owner_ata: Pubkey, vault: Pubkey, token_program: Pubkey,
            matcher_program: Pubkey, matcher_context: Pubkey, fee: int = 0) -> Instruction:
    data = bytes([2]) + bytes(matcher_program) + bytes(matcher_context) + struct.pack("<Q", fee)
    return Instruction(program_id, data, [_s(owner), _w(slab), _w(owner_ata), _w(vault), _r(token_program)])


def deposit(program_id: Pubkey, user: Pubkey, slab: Pubkey, user_ata: Pubkey, vault: Pubkey, token_program: Pubkey,
            user_idx: int, amount: int) -> Instruction:
    return Instruction(program_id, bytes([3]) + struct.pack("<HQ", user_idx, amount), [
        _s(user), _w(slab), _w(user_ata), _w(vault), _r(token_program), _r(SYSVAR_CLOCK)])


def withdraw(program_id: Pubkey, user: Pubkey, slab: Pubkey, vault: Pubkey, user_ata: Pubkey, token_program: Pubkey,
             oracle: Pubkey, user_idx: int, amount: int) -> Instruction:
    return Instruction(program_id, bytes([4]) + struct.pack("<HQ", user_idx, amount), [
        _s(user), _w(slab), _w(vault), _w(user_ata), _r(vault_authority(program_id, slab)), _r(token_program),
        _r(SYSVAR_CLOCK), _r(oracle)])


def keeper_crank(program_id: Pubkey, caller: Pubkey, slab: Pubkey, oracle: Pubkey, caller_idx: int = 0xFFFF,
                 allow_panic: int = 0) -> Instruction:
    return Instruction(program_id, bytes([5]) + struct.pack("<HB", caller_idx, allow_panic), [
        _s(caller), _w(slab), _r(SYSVAR_CLOCK), _r(oracle)])


def trade_no_cpi(program_id: Pubkey, user: Pubkey, lp: Pubkey, slab: Pubkey, oracle: Pubkey, lp_idx: int,
                 user_idx: int, size: int) -> Instruction:
    data = bytes([6]) + struct.pack("<HH", lp_idx, user_idx) + int(size).to_bytes(16, "little", signed=True)
    return Instruction(program_id, data, [_s(user), _s(lp), _w(slab), _r(SYSVAR_CLOCK), _r(oracle)])


def close_account(program_id: Pubkey, user: Pubkey, slab: Pubkey, vault: Pubkey, user_ata: Pubkey,
                  token_program: Pubkey, oracle: Pubkey, user_idx: int) -> Instruction:
    return Instruction(program_id, bytes([8]) + struct.pack("<H", user_idx), [
        _s(user), _w(slab), _w(vault), _w(user_ata), _r(vault_authority(program_id, slab)), _r(token_program),
        _r(SYSVAR_CLOCK), _r(oracle)])


def top_up_insurance(program_id: Pubkey, user: Pubkey, slab: Pubkey, user_ata: Pubkey, vault: Pubkey,
                     token_program: Pubkey, amount: int) -> Instruction:
    return Instruction(program_id, bytes([9]) + struct.pack("<Q", amount), [
        _s(user), _w(slab), _w(user_ata), _w(vault), _r(token_program)])


def set_oracle_authority(program_id: Pubkey, admin: Pubkey, slab: Pubkey, authority: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([16]) + bytes(authority), [_s(admin), _w(slab)])


def push_oracle_price(program_id: Pubkey, authority: Pubkey, slab: Pubkey, price_e6: int, ts: int) -> Instruction:
    return Instruction(program_id, bytes([17]) + struct.pack("<Qq", price_e6, ts), [_s(authority), _w(slab)])


def update_admin(program_id: Pubkey, admin: Pubkey, slab: Pubkey, new_admin: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([12]) + bytes(new_admin), [_s(admin), _w(slab)])


def accept_admin(program_id: Pubkey, new_admin: Pubkey, slab: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([22]), [_s(new_admin), _w(slab)])
