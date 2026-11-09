#!/bin/sh
set -e
if [ -n "$PERCOLATOR_DEVNET_KEYPAIR" ] || [ "$IMPERIAL_TRADING_ENABLED" = "true" ]; then
  echo "REFUSED: trading keypair / IMPERIAL_TRADING_ENABLED must not be set" >&2
  exit 1
fi
cd /app/python && python3 server_reads.py &
cd /app && exec node src/server.mjs
