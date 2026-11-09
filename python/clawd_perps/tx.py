"""Transaction helpers. Every write is simulate-first; sending requires confirm=True and is refused on mainnet."""
from __future__ import annotations

import base64
import json
import os

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.transaction import Transaction

from .rpc import Rpc


class MainnetRefused(RuntimeError):
    pass


def load_keypair(path: str) -> Keypair:
    with open(os.path.expanduser(path)) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


def build(rpc: Rpc, ixs: list, payer: Keypair, signers: list[Keypair]) -> Transaction:
    bh = Hash.from_string(rpc.latest_blockhash())
    msg = Message.new_with_blockhash(ixs, payer.pubkey(), bh)
    uniq = {bytes(k.pubkey()): k for k in [payer, *signers]}
    return Transaction(list(uniq.values()), msg, bh)


def run(rpc: Rpc, cluster: str, ixs: list, payer: Keypair, signers: list[Keypair] | None = None,
        *, confirm: bool = False) -> dict:
    """Simulate always; send only with confirm=True on devnet/localnet."""
    if confirm and cluster == "mainnet":
        raise MainnetRefused("this client never sends mainnet transactions")
    tx = build(rpc, ixs, payer, signers or [])
    b64 = base64.b64encode(bytes(tx)).decode()
    sim = rpc.simulate_b64(b64)
    out = {"cluster": cluster, "simulated": True, "sim_err": sim.get("err"), "logs": (sim.get("logs") or [])[-25:],
           "units": sim.get("unitsConsumed"), "sent": False}
    if not confirm:
        out["note"] = "dry run: pass --confirm (CLI) / confirm=true (API) to send"
        return out
    if sim.get("err"):
        out["note"] = "simulation failed; not sent"
        return out
    out["signature"] = rpc.send_b64(b64)
    out["sent"] = True
    return out
