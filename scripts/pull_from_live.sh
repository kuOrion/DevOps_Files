#!/bin/bash
# Pull a consistent copy of a client's real Postgres + filestore data FROM
# live-area -- the only place real/authoritative data exists. Used by both
# staging-area (pre-promotion validation) and sanitize-area (its raw
# source refresh). Never used the other direction -- live-area is never
# written back to by either.
#
# Postgres and filestore need genuinely different treatment when the
# client isn't currently live:
#   - Postgres: pg_dump must run AS A PROCESS INSIDE the container (via
#     docker exec), connecting to its own local Postgres socket -- that's
#     what makes it a real, consistent MVCC snapshot even while real users
#     are actively writing when live. A raw file copy of an actively-
#     written data directory risks a torn/inconsistent copy. When NOT
#     live, nobody's writing to it either way, but pg_dump still needs a
#     running Postgres to connect to -- so this briefly starts just the
#     db service (not the full serving stack), dumps, and stops it again
#     immediately. Minimizes how long anything touches real client data,
#     same principle as sanitize-area's own start-for-the-run lifecycle.
#   - Filestore: it's plain files, not a live database connection. When
#     the client isn't live, nothing is writing to it, so a raw volume
#     copy via a throwaway container is safe WITHOUT starting anything --
#     genuinely no process needs to run at all for this part.
#
# Usage: pull_from_live.sh <client_id> <dest_dir>
set -euo pipefail

# Meant to run as the narrow erp16-puller OS user (2026-08-20 -- see
# /etc/sudoers.d/erp16-narrow-users), which deliberately isn't in the
# docker group, so every `docker` call below needs to go through sudo for
# the narrow per-container sudoers rules to have any effect. Shadowing
# the command name here means every existing call site below picks this
# up automatically, without needing every individual line touched.
docker() { command sudo /usr/bin/docker "$@"; }

CLIENT_ID="${1:?Usage: pull_from_live.sh <client_id> <dest_dir>}"
DEST_DIR="${2:?Usage: pull_from_live.sh <client_id> <dest_dir>}"

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_COMPOSE="$BUILD_DIR/generated/live/$CLIENT_ID/docker-compose.yml"
DB_CONTAINER="live-${CLIENT_ID}-db"
WEB_CONTAINER="live-${CLIENT_ID}-web"
WEB_VOLUME="live-${CLIENT_ID}_odoo-web-data"

fail() { echo "ERROR: $1" >&2; exit 1; }

[ -f "$LIVE_COMPOSE" ] || fail "no live-area stack found for '$CLIENT_ID' at $LIVE_COMPOSE -- has it ever been deployed?"

DB_NAME=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$BUILD_DIR/clients.yaml'))['clients']['$CLIENT_ID']
print(cfg['db_name'])
")

mkdir -p "$DEST_DIR"

is_running() { docker ps --filter "name=^${1}$" --format '{{.Names}}' | grep -q .; }

DB_WAS_STOPPED=false
if ! is_running "$DB_CONTAINER"; then
    DB_WAS_STOPPED=true
    echo "=== '$CLIENT_ID' db is not currently live -- starting it briefly to dump it ==="
    docker compose -f "$LIVE_COMPOSE" up -d db
    for i in $(seq 1 30); do
        docker exec "$DB_CONTAINER" pg_isready -U odoo >/dev/null 2>&1 && break
        [ "$i" -eq 30 ] && fail "Postgres never became ready"
        sleep 1
    done
fi

echo "=== Dumping '$DB_NAME' from live-area (pg_dump running inside $DB_CONTAINER) ==="
docker exec "$DB_CONTAINER" pg_dump -U odoo --no-owner --no-privileges -Fc "$DB_NAME" > "$DEST_DIR/db.dump"

if [ "$DB_WAS_STOPPED" = true ]; then
    echo "=== Stopping db again (was not live before this pull) ==="
    docker compose -f "$LIVE_COMPOSE" stop db
fi

echo "=== Copying filestore from live-area ==="
if is_running "$WEB_CONTAINER"; then
    docker exec "$WEB_CONTAINER" tar -C "/var/lib/odoo/.local/share/Odoo/filestore/${DB_NAME}" -czf "/tmp/pull_${CLIENT_ID}_filestore.tar.gz" .
    docker cp "${WEB_CONTAINER}:/tmp/pull_${CLIENT_ID}_filestore.tar.gz" "$DEST_DIR/filestore.tar.gz"
    docker exec "$WEB_CONTAINER" rm -f "/tmp/pull_${CLIENT_ID}_filestore.tar.gz"
else
    # Not live -- nothing's writing to it, so a raw volume copy is safe
    # without starting anything at all, unlike the db case above.
    docker run --rm \
        -v "${WEB_VOLUME}:/data:ro" \
        -v "$DEST_DIR:/dest" \
        alpine:3 \
        sh -c "tar -C /data/.local/share/Odoo/filestore/${DB_NAME} -czf /dest/filestore.tar.gz ." \
        || fail "filestore volume '$WEB_VOLUME' not found or empty -- has '$CLIENT_ID' ever been live?"
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DEST_DIR/pulled_at.txt"
echo "=== Done: $DEST_DIR/db.dump + filestore.tar.gz (pulled from live-area's '$CLIENT_ID') ==="
