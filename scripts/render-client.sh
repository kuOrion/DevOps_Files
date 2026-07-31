#!/bin/bash
# Thin wrapper around render_client.py — see that file for the actual logic.
# Usage: render-client.sh <client_id> [--addons-path P] [--config-path P] [--out P]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/render_client.py" "$@"
