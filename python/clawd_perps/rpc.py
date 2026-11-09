"""Tiny dependency-free JSON-RPC / HTTP helpers (urllib only)."""
from __future__ import annotations

import base64
import json
import urllib.request

UA = {"User-Agent": "clawd-perps/0.1", "Content-Type": "application/json"}


def http_json(url: str, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        out = http_json(self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]

    def account(self, pubkey: str):
        """Returns (owner, data bytes, lamports) or None."""
        v = self.call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": "confirmed"}])["value"]
        if v is None:
            return None
        return v["owner"], base64.b64decode(v["data"][0]), v["lamports"]

    def accounts(self, pubkeys: list[str]):
        vals = self.call("getMultipleAccounts", [pubkeys, {"encoding": "base64", "commitment": "confirmed"}])["value"]
        return [None if v is None else (v["owner"], base64.b64decode(v["data"][0]), v["lamports"]) for v in vals]

    def latest_blockhash(self) -> str:
        return self.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]

    def rent(self, size: int) -> int:
        return self.call("getMinimumBalanceForRentExemption", [size])

    def send_b64(self, tx_b64: str) -> str:
        return self.call("sendTransaction", [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}])

    def simulate_b64(self, tx_b64: str):
        return self.call("simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed"}])["value"]
