"""Percolator.tech MCP — percolator_* reads, plus unsigned-tx write builders behind PERCOLATOR_WRITES_ENABLED.

Reuses imperial-perps-mcp-server percolator_venue.py + vendored clawd_perps/.
Never loads PERCOLATOR_DEVNET_KEYPAIR. Writes are not registered unless PERCOLATOR_WRITES_ENABLED=true.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure vendored package is importable when launched from any cwd.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Fail closed: refuse to run if a trading keypair env is set on this host.
if os.environ.get("PERCOLATOR_DEVNET_KEYPAIR"):
    raise SystemExit("REFUSED: PERCOLATOR_DEVNET_KEYPAIR must not be set on percolator-ws")
if os.environ.get("IMPERIAL_TRADING_ENABLED", "").lower() in ("1", "true", "yes"):
    raise SystemExit("REFUSED: IMPERIAL_TRADING_ENABLED must not be set on percolator-ws")

from mcp.server.fastmcp import FastMCP  # type: ignore
import gm8z
import percolator_venue

READ_NAMES = {
    "percolator_list_markets",
    "percolator_read_market",
    "percolator_oracle_price",
    "percolator_quote",
    "percolator_detect_venue",
    "percolator_positions",
    # GM8z mainnet program reads (M4) — additive, mainnet-only, read-only.
    "percolator_gm8z_list_markets",
    "percolator_gm8z_read_market",
    "percolator_gm8z_oracle_price",
}
WRITE_NAMES = {
    "percolator_create_user_market",
    "percolator_deposit",
    "percolator_withdraw",
    "percolator_open_position",
    "percolator_close_position",
    "percolator_update_oracle",
}

# Write tools (unsigned tx builders) register only with PERCOLATOR_WRITES_ENABLED. Mainnet builds additionally
# need PERCOLATOR_MAINNET_WRITES_ENABLED plus confirm gates inside percolator_venue (and stay unsigned).
_writes_on = os.environ.get("PERCOLATOR_WRITES_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

ALLOWED = set(READ_NAMES) | (WRITE_NAMES if _writes_on else set())

mcp = FastMCP(
    "percolator-tech",
    # The Node /mcp proxy is stateless (one POST per tools/call, no session
    # handshake), so the sidecar must not require an MCP session id.
    stateless_http=True,
    json_response=True,
    instructions=(
        "CLAWD Percolator tools for percolator.tech. "
        "Reads always on. Write tools register only when PERCOLATOR_WRITES_ENABLED is true; they "
        "return UNSIGNED transactions plus a simulation for the user's wallet to sign. The server never "
        "signs or sends. Mainnet additionally needs PERCOLATOR_MAINNET_WRITES_ENABLED and confirm gates. "
        "Program is unaudited."
    ),
)

_registered: list[str] = []


def _tool(*args, **kwargs):
    def deco(fn):
        name = fn.__name__
        if name not in ALLOWED:
            return fn  # skip unregistered writes
        _registered.append(name)
        return mcp.tool(*args, **kwargs)(fn)
    return deco


percolator_venue.register(_tool)
gm8z.register_gm8z(_tool)

# FastMCP streamable HTTP — path /mcp when mounted; standalone uses its own.
if __name__ == "__main__":
    host = os.environ.get("PERCOLATOR_PY_HOST", "127.0.0.1")
    port = int(os.environ.get("PERCOLATOR_PY_PORT", "8791"))
    print(f"percolator reads MCP on http://{host}:{port}/mcp tools={_registered}", flush=True)
    # mcp SDK streamable HTTP
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
