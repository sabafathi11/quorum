#!/usr/bin/env bash
# Start the Quorum server. Everything else is a plugin.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || { echo "no .venv — see README.md (Setup)"; exit 1; }
exec .venv/bin/python -m quorum serve "$@"
