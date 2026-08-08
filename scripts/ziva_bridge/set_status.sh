#!/usr/bin/env bash
# Usage: ./set_status.sh "phase update text"
# Writes status.json, targeting whichever ack the bridge most recently sent.
# The heartbeat timer in index.js polls this on a fixed cadence and edits
# the WhatsApp bubble only when the text actually changed.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEXT="$1"
python3 -c "
import json, sys
with open('$DIR/last_ack.json') as f:
    ack = json.load(f)
with open('$DIR/status.json', 'w') as f:
    json.dump({'active': True, 'text': sys.argv[1], 'to': ack['key']['remoteJid'], 'editKey': ack['key']}, f)
" "$TEXT"
