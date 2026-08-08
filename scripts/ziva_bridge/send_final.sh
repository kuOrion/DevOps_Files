#!/usr/bin/env bash
# Usage: ./send_final.sh "final answer text"
# Sends the definitive reply (edits the same bubble the ack/heartbeat used)
# and marks final:true so the bridge deactivates the heartbeat the moment
# this send succeeds -- no race with a stale status.json update.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEXT="$1"
FNAME="final_$(date +%s%N).json"
python3 -c "
import json, sys
with open('$DIR/last_ack.json') as f:
    ack = json.load(f)
with open('$DIR/outbox/$FNAME', 'w') as f:
    json.dump({'to': ack['key']['remoteJid'], 'text': sys.argv[1], 'editKey': ack['key'], 'final': True}, f)
" "$TEXT"
