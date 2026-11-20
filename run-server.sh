#!/usr/bin/env bash
# Start the x402-flask-gate server
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8080}"

echo "Starting x402-flask-gate server on port ${PORT}..."
python3 server.py "$@"
