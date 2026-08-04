#!/bin/bash
# Option B developer workflow: bring up a local Docker Odoo stack for one
# client, using clients.yaml + templates/ for config (render_client.py) and
# a sanitized snapshot pulled from S3 (published by publish_snapshot.sh).
#
# Usage:
#   ./dev-start.sh <client_id> [--refresh] [--addons-path PATH]
#   ./dev-start.sh <client_id> --down
#
#   --refresh       Re-pull the snapshot from S3 even if already cached
#                    locally, and rebuild the DB/filestore from it.
#   --down          Stop the client's containers (keeps the cached
#                    snapshot and rendered config for a fast restart).
#   --addons-path   Host path to the custom addons checkout
#                    (default: ../erp16-custom-addons, sibling to this repo).
#
# Multiple clients can run at once -- each gets its own compose project
# (keyed by client_id) and its own ports (from clients.yaml), so there's no
# collision running e.g. orion_test and another client simultaneously.
set -euo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT_ID="${1:?Usage: dev-start.sh <client_id> [--refresh] [--down]}"
shift || true

REFRESH=false
DOWN=false
ADDONS_PATH="$(dirname "$BUILD_DIR")/erp16-custom-addons"
# A developer laptop has a real named profile pointing at a static key.
# Running this ON the sandbox itself has no static credentials by design --
# it authenticates via its EC2 instance role instead, which needs an EMPTY
# profile (render_client.py's _aws_cmd omits --profile entirely when this
# is empty, falling back to the default credential chain). Use
# --aws-profile "" when running here, not an AWS_PROFILE env var -- the aws
# CLI itself reads that env var directly regardless of any --profile flag,
# so an empty exported value confuses it on its own.
AWS_PROFILE="erp16-sandbox"
AWS_REGION="ap-south-1"

while [ $# -gt 0 ]; do
    case "$1" in
        --refresh) REFRESH=true ;;
        --down) DOWN=true ;;
        --addons-path) ADDONS_PATH="$2"; shift ;;
        --aws-profile) AWS_PROFILE="$2"; shift ;;
        *) echo "ERROR: unknown argument '$1'" >&2; exit 1 ;;
    esac
    shift
done

OUT_DIR="$BUILD_DIR/generated/$CLIENT_ID"
SNAPSHOT_DIR="$OUT_DIR/snapshot"
COMPOSE_FILE="$OUT_DIR/docker-compose.yml"
DB_SERVICE="db_${CLIENT_ID}"
WEB_SERVICE="web_${CLIENT_ID}"

fail() { echo "ERROR: $1" >&2; exit 1; }

# --- prerequisite checks ---
command -v docker >/dev/null 2>&1 || fail "docker not found -- install Docker first"
docker info >/dev/null 2>&1 || fail "docker daemon not reachable -- is Docker running?"
command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"
python3 -c "import yaml, jinja2" >/dev/null 2>&1 || fail "python3 missing pyyaml/jinja2 -- pip install pyyaml jinja2"
[ -d "$ADDONS_PATH" ] || fail "addons path not found: $ADDONS_PATH (pass --addons-path if it's elsewhere)"

if [ "$DOWN" = true ]; then
    [ -f "$COMPOSE_FILE" ] || fail "no rendered stack found for '$CLIENT_ID' -- nothing to stop"
    echo "=== Stopping $CLIENT_ID (snapshot cache kept for fast restart) ==="
    docker compose -f "$COMPOSE_FILE" stop
    exit 0
fi

echo "=== Rendering docker-compose.yml/odoo.conf for '$CLIENT_ID' ==="
python3 "$BUILD_DIR/scripts/render_client.py" "$CLIENT_ID" \
    --addons-path "$ADDONS_PATH" \
    --aws-profile "$AWS_PROFILE" \
    --aws-region "$AWS_REGION"

DB_NAME=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$BUILD_DIR/clients.yaml'))['clients']['$CLIENT_ID']
print(cfg['db_name'])
")
HTTP_PORT=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$BUILD_DIR/clients.yaml'))['clients']['$CLIENT_ID']
print(cfg['http_port'])
")

NEED_RESTORE=false
if [ "$REFRESH" = true ] || [ ! -f "$SNAPSHOT_DIR/db.dump" ]; then
    echo "=== Pulling sanitized snapshot from S3 ==="
    mkdir -p "$SNAPSHOT_DIR"
    S3_PREFIX="s3://erp16-sandbox-snapshots/sanitized/${CLIENT_ID}/latest"
    aws s3 cp "$S3_PREFIX/db.dump" "$SNAPSHOT_DIR/db.dump" --profile "$AWS_PROFILE" --region "$AWS_REGION"
    aws s3 cp "$S3_PREFIX/filestore.tar.gz" "$SNAPSHOT_DIR/filestore.tar.gz" --profile "$AWS_PROFILE" --region "$AWS_REGION"
    aws s3 cp "$S3_PREFIX/published_at.txt" "$SNAPSHOT_DIR/published_at.txt" --profile "$AWS_PROFILE" --region "$AWS_REGION"
    echo "Snapshot published at: $(cat "$SNAPSHOT_DIR/published_at.txt")"
    NEED_RESTORE=true
fi

if [ "$REFRESH" = true ]; then
    echo "=== --refresh: tearing down existing volumes for a clean restore ==="
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
fi

echo "=== Starting $DB_SERVICE ==="
docker compose -f "$COMPOSE_FILE" up -d "$DB_SERVICE"

echo "=== Waiting for Postgres to be ready ==="
for i in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" pg_isready -U odoo >/dev/null 2>&1; then
        break
    fi
    [ "$i" -eq 30 ] && fail "Postgres never became ready"
    sleep 1
done

DB_EXISTS=$(docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" psql -U odoo -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null | tr -d '[:space:]')

if [ "$NEED_RESTORE" = true ] || [ "$DB_EXISTS" != "1" ]; then
    if [ "$DB_EXISTS" = "1" ]; then
        echo "=== Dropping existing '$DB_NAME' for a fresh restore ==="
        docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" psql -U odoo -d postgres -c "DROP DATABASE \"$DB_NAME\";"
    fi
    echo "=== Creating database '$DB_NAME' ==="
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" psql -U odoo -d postgres -c "CREATE DATABASE \"$DB_NAME\";"

    echo "=== Restoring dump into '$DB_NAME' ==="
    docker compose -f "$COMPOSE_FILE" cp "$SNAPSHOT_DIR/db.dump" "${DB_SERVICE}:/tmp/db.dump"
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" pg_restore -U odoo --no-owner --no-privileges -d "$DB_NAME" /tmp/db.dump
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" rm -f /tmp/db.dump

    echo "=== Restoring filestore ==="
    # Extract directly into the named volume via a throwaway helper
    # container bind-mounting both the volume and the local snapshot dir --
    # NOT "compose cp into the web container, then compose run" (run spins
    # up a *different* ephemeral container instance than the one cp'd
    # into, so the copied file was never actually there for it to read).
    docker compose -f "$COMPOSE_FILE" up --no-start "$WEB_SERVICE"
    WEB_VOLUME="${CLIENT_ID}_odoo-web-data-${CLIENT_ID}"
    docker run --rm \
        -v "${WEB_VOLUME}:/var/lib/odoo" \
        -v "$SNAPSHOT_DIR:/snapshot:ro" \
        alpine:3 \
        sh -c "mkdir -p /var/lib/odoo/.local/share/Odoo/filestore/${DB_NAME} && tar -C /var/lib/odoo/.local/share/Odoo/filestore/${DB_NAME} -xzf /snapshot/filestore.tar.gz && chown -R 101:101 /var/lib/odoo"
    # uid 101 = the odoo image's non-root 'odoo' user -- without this, the
    # extracted tree is root-owned and Odoo can't create sibling dirs
    # (e.g. .local/share/Odoo/sessions) at boot, failing every request
    # with PermissionError even though the container itself starts fine.
else
    echo "=== '$DB_NAME' already restored, reusing existing volume ==="
fi

echo "=== Starting $WEB_SERVICE ==="
# The custom image (build/docker/Dockerfile.odoo, referenced by the
# rendered compose file) already bakes in py3o.formats/py3o.template --
# see that file's comment for why. No runtime pip-install/restart needed
# here anymore; docker compose builds/caches the image automatically.
docker compose -f "$COMPOSE_FILE" up -d "$WEB_SERVICE"

echo "=== Waiting for Odoo to finish loading (health-check) ==="
for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:${HTTP_PORT}/web/login" >/dev/null 2>&1; then
        echo "Odoo responded after ${i}s"
        break
    fi
    if [ "$i" -eq 90 ]; then
        echo "WARNING: Odoo did not respond within 90s -- check 'docker compose -f $COMPOSE_FILE logs $WEB_SERVICE'" >&2
        exit 1
    fi
    sleep 1
done

echo ""
echo "=== '$CLIENT_ID' is up ==="
echo "  URL: http://127.0.0.1:${HTTP_PORT}"
echo "  DB:  $DB_NAME"
echo "  Master password: resolved via SSM (/erp16-sandbox/${CLIENT_ID}/master_password)"
echo "  Stop with: $0 $CLIENT_ID --down"
