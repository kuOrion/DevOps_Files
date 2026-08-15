#!/bin/bash
# Full consolidated sanitization pipeline, reworked for the per-client
# isolated-container architecture (2026-08-05) -- see ROADMAP.md's
# sanitization-rework entries for the full design discussion.
#
# Runs as erp16-sanitizer (via its narrow sudoers rule for standing up
# sanitize-db/sanitize-web -- this script itself just needs docker exec
# access to those two containers, which erp16-sanitizer does NOT have
# directly either; in practice this is invoked by an orchestrator with
# broader access, same as pull_from_live.sh is invoked by/for
# erp16-puller rather than run interactively as that user).
#
# Never connects to a live client database. Only ever touches:
#   - sanitize-db / sanitize-web (its own throwaway scratch stack)
#   - /opt/erp16/raw/<client_id>/ (read-only handoff dump, written
#     earlier by pull_from_live.sh -- this script does not pull)
#
# set -e is critical here -- see the original script's comment on the
# silent-failure incident this guards against. Still true.
set -euo pipefail

CLIENT_ID="${1:-orion_test}"
PIPELINE_START=$(date +%s)

DBCONT=sanitize-db
WEB=sanitize-web
DBHOST=db   # sanitize-web's own HOST env var / compose network alias -- not a shared-instance alias anymore
RAW_DIR="/opt/erp16/raw/${CLIENT_ID}"

# BUILD_DIR-relative, not a hardcoded /opt/erp16/sanitize/ path -- that
# was the sandbox's original placement for staging/sanitize output,
# since superseded (2026-08-15): everything except logs/runtime-state
# (which genuinely needs to survive a DevOps_Files reclone/reset) now
# lives consistently under DevOps_Files/generated/, matching live-area's
# convention, not split across two locations. See ROADMAP.md's
# reasoning for the switch.
BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Read live from the compose file rather than hardcoding -- this
# container's password is generated fresh per render, unlike the old
# shared instance's long-lived literal.
DBPASS=$(grep -oP 'POSTGRES_PASSWORD: \K.*' "$BUILD_DIR/generated/sanitize/docker-compose.yml" | head -1)

source "$(dirname "${BASH_SOURCE[0]}")/pipeline_clients.sh"
SANITIZED_DB_NAME=$(resolve_sanitized_db "$CLIENT_ID")
# The freshly-restored copy IS the real client data, byte-identical, at
# the instant right after restore -- before any transform below runs.
# Safe to read it as "source" for dictionary-building purposes, then
# transform that same database in place into the sanitized copy. No
# live source database exists in this container at all; ever.
SOURCE_DB_NAME="$SANITIZED_DB_NAME"

echo "=== Client: $CLIENT_ID (sanitized db: $SANITIZED_DB_NAME) ==="

echo "=== STEP -1: verify the puller already dropped a fresh pull here ==="
if [ ! -f "$RAW_DIR/db.dump" ] || [ ! -f "$RAW_DIR/filestore.tar.gz" ]; then
    echo "FATAL: no pulled data at $RAW_DIR -- run pull_from_live.sh $CLIENT_ID first" >&2
    exit 1
fi

echo "=== STEP 0: refresh the sanitize scripts inside sanitize-web ==="
# Unlike the old shared container (scripts manually docker cp'd in once,
# ages ago, and left there), sanitize-web is recreated fresh -- always
# push the current tree so a stale copy can never silently run.
docker cp "$(dirname "${BASH_SOURCE[0]}")/." "${WEB}:/tmp/"

echo "=== STEP 0a: drop+recreate $SANITIZED_DB_NAME, restore from the handoff dump ==="
docker exec "$DBCONT" psql -U odoo -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${SANITIZED_DB_NAME}' AND pid <> pg_backend_pid();" || true
docker exec "$DBCONT" psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS \"${SANITIZED_DB_NAME}\";"
docker exec "$DBCONT" psql -U odoo -d postgres -c "CREATE DATABASE \"${SANITIZED_DB_NAME}\" OWNER odoo;"
docker cp "$RAW_DIR/db.dump" "${DBCONT}:/tmp/${CLIENT_ID}_pull.dump"
docker exec "$DBCONT" pg_restore -U odoo -d "${SANITIZED_DB_NAME}" --no-owner --no-acl "/tmp/${CLIENT_ID}_pull.dump"
docker exec "$DBCONT" rm -f "/tmp/${CLIENT_ID}_pull.dump"
echo "=== $SANITIZED_DB_NAME restored fresh from this run's pull ==="

echo "=== STEP 0b: restore filestore into sanitize-web, under the sanitized db name ==="
docker exec "$WEB" mkdir -p "/var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME}"
docker cp "$RAW_DIR/filestore.tar.gz" "${WEB}:/tmp/${CLIENT_ID}_filestore.tar.gz"
docker exec -u root "$WEB" tar -xzf "/tmp/${CLIENT_ID}_filestore.tar.gz" -C "/var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME}"
docker exec -u root "$WEB" rm -f "/tmp/${CLIENT_ID}_filestore.tar.gz"
docker exec -u root "$WEB" chown -R odoo:odoo "/var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME}"

echo "=== STEP 0c: build_pii_dictionary.py (real values, read before any transform) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/build_pii_dictionary.py

echo "=== STEP 0d: filter_pii_dictionary.py (FULL + SUBSTRING-HUNT sets) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" "$WEB" python3 -u /tmp/filter_pii_dictionary.py

echo "=== STEP 0e: build_value_mapping.py (deterministic value -> transformed mapping) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" "$WEB" python3 -u /tmp/build_value_mapping.py

echo "=== STEP 1: write_pass.py (canonical field transforms + job title flattening) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" -i "$WEB" odoo shell -d "$SANITIZED_DB_NAME" --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < "$(dirname "${BASH_SOURCE[0]}")/write_pass.py"

echo "=== STEP 1b: reset admin login+password to a known dev value (technical account, not personal data) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d "$SANITIZED_DB_NAME" --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http <<'EOF'
u = env['res.users'].browse(2)
u.write({'login': 'admin', 'password': 'admin'})
env.cr.commit()
print(f"admin login reset: {u.login} (password also reset to a known dev value -- ORM write, properly hashed)")
EOF

echo "=== STEP 2: chatter_email_composite.py (email_from/email_to/email_cc consistent identity) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/chatter_email_composite.py

echo "=== STEP 3: chatter_bulk.py (flat placeholder + exact-match bulk fields) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/chatter_bulk.py

echo "=== STEP 4: substring_hunt_scan.py (full-db verification + auto-fix) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/substring_hunt_scan.py

echo "=== STEP 5: rule_attachment.py (ir.attachment placeholder content) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d "$SANITIZED_DB_NAME" --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < "$(dirname "${BASH_SOURCE[0]}")/rule_attachment.py"

echo "=== STEP 5b: rotate_app_secrets.py (database.secret rotation, ir_mail_server credential blanking) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" \
    "$WEB" python3 -u /tmp/rotate_app_secrets.py

echo "=== STEP 6: cleanup -- intermediate CSVs + this run's handoff dump/filestore ==="
docker exec "$WEB" rm -f "/tmp/pii_"*"_${SOURCE_DB_NAME}.csv" "/tmp/substring_hunt_hits_${SOURCE_DB_NAME}.csv" 2>/dev/null || true
rm -f "$RAW_DIR/db.dump" "$RAW_DIR/filestore.tar.gz"
echo "handoff dump/filestore for $CLIENT_ID removed -- next run needs a fresh pull_from_live.sh pull"

PIPELINE_END=$(date +%s)
echo "=== PIPELINE COMPLETE in $((PIPELINE_END - PIPELINE_START))s ==="
