#!/bin/bash
# Nightly sanitization cron job -- pulls, sanitizes, and publishes all 5
# real clients' data, unattended, no laptop involved (see ROADMAP.md's
# 2026-08-15 sanitization-pipeline plan for the full reasoning: this must
# run whether or not a developer is connected).
#
# Brings sanitize-db/sanitize-web up fresh, runs the full pipeline for
# every client, then brings them back down -- these are a working area,
# not a permanent service, same treatment staging already gets. A
# real-but-briefly-restored copy of client data shouldn't sit in a
# running container 24/7 when the pipeline only needs it for the ~10-15
# minutes it's actually being processed.
#
# One client's failure doesn't block the others -- each client's
# pull/sanitize/publish is fully independent (unlike deploy.sh's atomic
# all-or-nothing promote across the shared worktree). Logs a summary
# audit-forever entry per run, same JSONL convention as deploy.sh's
# write_deploy_log, so logging_collector.py picks it up without any
# collector-side change (only _ROUTINE_SOURCES-listed sources get
# pruned; "sanitize" isn't in that list, so this is kept forever).
set -uo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW_BASE="/opt/erp16/raw"
SANITIZE_LOG_DIR="/opt/erp16/logs/sanitize"
SANITIZE_COMPOSE="$BUILD_DIR/generated/sanitize/docker-compose.yml"

CLIENTS=(orion_test db_test parus_instruments puna_eye_care orion-internal)

RUN_START=$(date +%s)
OK_CLIENTS=()
FAILED_CLIENTS=()

echo "=== Nightly sanitize run starting: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

echo "=== Bringing up sanitize-db/sanitize-web ==="
docker compose -f "$SANITIZE_COMPOSE" up -d
# Give Postgres a moment to actually accept connections before the first
# client's restore -- compose "up" returns as soon as the container
# starts, not once it's ready.
sleep 10

for CLIENT_ID in "${CLIENTS[@]}"; do
    echo
    echo "=== [$CLIENT_ID] pull ==="
    if ! bash "$BUILD_DIR/scripts/pull_from_live.sh" "$CLIENT_ID" "$RAW_BASE/$CLIENT_ID"; then
        echo "=== [$CLIENT_ID] FAILED at pull -- skipping ==="
        FAILED_CLIENTS+=("$CLIENT_ID")
        continue
    fi

    echo "=== [$CLIENT_ID] sanitize ==="
    if ! bash "$BUILD_DIR/sanitize/run_pipeline.sh" "$CLIENT_ID"; then
        echo "=== [$CLIENT_ID] FAILED at sanitize -- not publishing, skipping ==="
        FAILED_CLIENTS+=("$CLIENT_ID")
        continue
    fi

    echo "=== [$CLIENT_ID] publish ==="
    # No --aws-profile: publish_snapshot.sh defaults to the real bucket +
    # empty profile now (2026-08-17), picked up automatically via this
    # box's own instance role (EC2-SSM-Execution-Role, extended with scoped
    # S3 access) -- the temporary sanitize-publish static credential this
    # used to need is retired, superseded by the real permanent fix.
    if ! bash "$BUILD_DIR/scripts/publish_snapshot.sh" "$CLIENT_ID" --local; then
        echo "=== [$CLIENT_ID] FAILED at publish ==="
        FAILED_CLIENTS+=("$CLIENT_ID")
        continue
    fi

    echo "=== [$CLIENT_ID] OK ==="
    OK_CLIENTS+=("$CLIENT_ID")
done

echo
echo "=== Bringing sanitize-db/sanitize-web back down ==="
docker compose -f "$SANITIZE_COMPOSE" down

RUN_END=$(date +%s)
RESULT="success"
if [ "${#FAILED_CLIENTS[@]}" -gt 0 ]; then
    RESULT="partial_failure"
fi

mkdir -p "$SANITIZE_LOG_DIR"
LOGFILE="$SANITIZE_LOG_DIR/$(date -u +%Y-%m-%d).jsonl"
SANITIZE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
SANITIZE_RESULT="$RESULT" \
SANITIZE_OK="${OK_CLIENTS[*]:-}" \
SANITIZE_FAILED="${FAILED_CLIENTS[*]:-}" \
SANITIZE_DURATION="$((RUN_END - RUN_START))" \
python3 -c "
import json, os
entry = {
    'ts': os.environ['SANITIZE_TS'],
    'source': 'sanitize',
    'level': 'audit',
    'result': os.environ['SANITIZE_RESULT'],
    'clients_ok': os.environ['SANITIZE_OK'].split(),
    'clients_failed': os.environ['SANITIZE_FAILED'].split(),
    'duration_seconds': int(os.environ['SANITIZE_DURATION']),
}
print(json.dumps(entry))
" >> "$LOGFILE"

echo
echo "=== Nightly sanitize run done in $((RUN_END - RUN_START))s: ${#OK_CLIENTS[@]} ok, ${#FAILED_CLIENTS[@]} failed ==="
if [ "${#FAILED_CLIENTS[@]}" -gt 0 ]; then
    echo "Failed: ${FAILED_CLIENTS[*]}"
    exit 1
fi
