#!/bin/bash
# Tier 2 on-premise release installer for earthmech, per
# docs/specs/Deployment_Lifecycle.docx Sec.3 -- wraps a git-pull transport
# in the same safety discipline used everywhere else in this pipeline
# (pre-deploy backup, health check, rollback path), without needing
# inbound SSH from Orion to the client's box.
#
# Deliberately reads from a SEPARATE, scoped repo (earthmech-release),
# never from erp16-custom-addons -- the on-prem site must never have
# access to the full shared monorepo covering every other client's
# custom code. That repo is kept in sync automatically by deploy.sh's
# sync_earthmech_release() (write side, runs on production); this script
# is the read/consume side, meant to run wherever earthmech's on-prem
# Odoo host actually is (today: this laptop, standing in as the on-prem
# understudy -- see docs/rehearsal/DRESS_REHEARSAL.md 2.2).
#
# Usage: install_release.sh <tag>
#   <tag> must be an actual tag (e.g. earthmech-a4e0baf), never a branch
#   -- the whole point of tagging is that this always installs a specific,
#   already-approved release, not whatever main happens to be.
set -euo pipefail

TAG="${1:?Usage: install_release.sh <tag>}"

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_CHECKOUT="$HOME/Earthmech_release"
RELEASE_REMOTE="git@github-earthmech-readonly:kuOrion/earthmech-release.git"
GENERATED_OUT="$BUILD_DIR/generated/earthmech"
BACKUPS_DIR="$HOME/Backups/earthmech"
DB="earthmech-db"
WEB="earthmech-web"
HTTP_PORT=8119
DB_NAME="earthmech"

fail() { echo "ERROR: $1" >&2; exit 1; }

echo "=== [earthmech] fetching release $TAG ==="
if [ ! -d "$RELEASE_CHECKOUT/.git" ]; then
    git clone "$RELEASE_REMOTE" "$RELEASE_CHECKOUT"
else
    git -C "$RELEASE_CHECKOUT" fetch origin --tags
fi
git -C "$RELEASE_CHECKOUT" rev-parse "refs/tags/$TAG" >/dev/null 2>&1 || fail "'$TAG' is not a real tag in earthmech-release -- refusing to install a moving branch"
git -C "$RELEASE_CHECKOUT" checkout "$TAG"

echo "=== [earthmech] backing up current state ==="
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST="$BACKUPS_DIR/$TS"
mkdir -p "$DEST"
if [ "$(docker inspect -f '{{.State.Running}}' "$DB" 2>/dev/null)" = "true" ]; then
    docker exec "$DB" pg_dump -U odoo --no-owner --no-privileges -Fc "$DB_NAME" > "$DEST/db.dump"
    docker exec "$WEB" tar -C "/var/lib/odoo/.local/share/Odoo/filestore/$DB_NAME" -czf "/tmp/earthmech_backup_filestore.tar.gz" .
    docker cp "$WEB:/tmp/earthmech_backup_filestore.tar.gz" "$DEST/filestore.tar.gz"
    docker exec -u root "$WEB" rm -f "/tmp/earthmech_backup_filestore.tar.gz"
    echo "=== [earthmech] backed up to $DEST ==="
else
    echo "=== [earthmech] no running stack to back up (first install) ==="
fi

echo "=== [earthmech] rendering config against the scoped release checkout (not erp16-custom-addons) ==="
python3 "$BUILD_DIR/scripts/render_client.py" earthmech \
    --container-prefix earthmech \
    --addons-path "$RELEASE_CHECKOUT/modules" \
    --config-path "$GENERATED_OUT/config" \
    --local-secrets \
    --out "$GENERATED_OUT"

echo "=== [earthmech] bringing up the stack ==="
docker compose -f "$GENERATED_OUT/docker-compose.yml" up -d --build

MODULES=$(python3 -c "
import yaml
m = yaml.safe_load(open('$RELEASE_CHECKOUT/RELEASE_MANIFEST.yaml'))
print(' '.join(m.get('modules', [])))
")
if [ -n "$MODULES" ]; then
    echo "=== [earthmech] applying module upgrades: $MODULES ==="
    docker exec "$WEB" odoo -d "$DB_NAME" -u "$(echo "$MODULES" | tr ' ' ',')" --stop-after-init --no-http
else
    echo "=== [earthmech] no custom modules in this release -- nothing to upgrade ==="
fi

echo "=== [earthmech] restarting to pick up new code ==="
docker restart "$WEB" >/dev/null

RUNNING=false
for i in $(seq 1 30); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$WEB" 2>/dev/null)" = "true" ]; then
        RUNNING=true
        break
    fi
    sleep 1
done
[ "$RUNNING" = "true" ] || fail "container never reached running state -- see rollback instructions below"

LOGIN_CODE=""
for i in $(seq 1 30); do
    LOGIN_CODE=$(curl -s -o /tmp/earthmech_healthcheck.html -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/web/login" || echo 000)
    [ "$LOGIN_CODE" = "200" ] && break
    sleep 2
done
if [ "$LOGIN_CODE" != "200" ]; then
    rm -f /tmp/earthmech_healthcheck.html
    fail "/web/login returned $LOGIN_CODE after install -- backup is at $DEST, restore manually (see deploy.sh's rollback_client for the pattern) if needed"
fi

ASSET_PATH=$(grep -oE '/web/assets/[A-Za-z0-9_./-]+\.(css|js)' /tmp/earthmech_healthcheck.html | head -1)
rm -f /tmp/earthmech_healthcheck.html
ASSET_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT$ASSET_PATH")
if [ "$ASSET_CODE" != "200" ]; then
    fail "asset $ASSET_PATH returned $ASSET_CODE (DB/filestore mismatch signature) -- backup is at $DEST"
fi

echo "=== [earthmech] INSTALL SUCCEEDED: $TAG live, login=$LOGIN_CODE asset=$ASSET_CODE ==="
