#!/bin/bash
# Full consolidated sanitization pipeline, remote orchestration.
# Runs on the sandbox host, drives docker exec into orion_test-web_orion_test-1
# (which has odoo + the addons image) against whichever client's scratch
# sanitized DB this run targets.
#
# set -e is critical here -- a prior run silently continued past a failed
# CREATE DATABASE (template db still had an open connection) and ran the
# whole pipeline against a stale, contaminated orm_test without anyone
# noticing until the log was reviewed after the fact. Never again: any
# failed step now aborts the whole script immediately and loudly.
set -euo pipefail

CLIENT_ID="${1:-orion_test}"
PIPELINE_START=$(date +%s)

# All client DBs on the sandbox share ONE Postgres container/network alias
# and ONE admin password -- this is the same shared-blast-radius Postgres
# instance the original incident's design flaw was about, not a per-client
# secret. Still only defined ONCE here rather than repeated as a literal
# in every sanitize/*.py script -- see SOURCE_DB/SANITIZED_DB below for the
# part that actually does vary per client.
WEB=orion_test-web_orion_test-1
DBCONT=orion_test-db_orion_test-1
DBHOST=db_orion_test
DBPASS='F0aclHkVKiTxFwCHsf6UoS26'

source "$(dirname "${BASH_SOURCE[0]}")/pipeline_clients.sh"
SOURCE_DB_NAME=$(resolve_source_db "$CLIENT_ID")
SANITIZED_DB_NAME=$(resolve_sanitized_db "$CLIENT_ID")
VIEW_CONTAINER="${SANITIZED_DB_NAME}-web"  # e.g. orm_test-web -- optional, see STEP 6

echo "=== Client: $CLIENT_ID (source db: $SOURCE_DB_NAME, sanitized db: $SANITIZED_DB_NAME) ==="

echo "=== STEP 0: stop ALL connections to $SOURCE_DB_NAME (both web containers), drop+recreate $SANITIZED_DB_NAME ==="
docker stop "$VIEW_CONTAINER" 2>/dev/null || true
docker stop "$WEB"
echo "$WEB stopped -- template db now has zero connections"

docker exec "$DBCONT" psql -U odoo -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('${SOURCE_DB_NAME}','${SANITIZED_DB_NAME}') AND pid <> pg_backend_pid();"
docker exec "$DBCONT" psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS \"${SANITIZED_DB_NAME}\";"
docker exec "$DBCONT" psql -U odoo -d postgres -c "CREATE DATABASE \"${SANITIZED_DB_NAME}\" TEMPLATE \"${SOURCE_DB_NAME}\";"
echo "=== $SANITIZED_DB_NAME recreated fresh from $SOURCE_DB_NAME template ==="

echo "=== STEP 0b: restart $WEB, wait until ready ==="
docker start "$WEB"
for i in $(seq 1 30); do
    if docker exec "$WEB" python3 -c "import psycopg2; psycopg2.connect(host='$DBHOST', dbname='${SOURCE_DB_NAME}', user='odoo', password='$DBPASS').close()" 2>/dev/null; then
        echo "web container DB connectivity confirmed after ${i}s"
        break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo "FATAL: web container never became ready" >&2
        exit 1
    fi
done

echo "=== STEP 0f: sync filestore ($SOURCE_DB_NAME -> $SANITIZED_DB_NAME) ==="
# CREATE DATABASE ... TEMPLATE only clones Postgres rows -- the filestore
# is separate files on disk, never touched by that. Without this step,
# SANITIZED_DB_NAME's filestore silently drifts stale relative to the real
# source (new attachments, new cached asset bundles) with nothing to catch
# it -- found via the exact same bug class in dev-start.sh's snapshot
# restore. Additive only (never deletes) so the sanitized copy's own
# placeholder files (written by rule_attachment.py) are never touched.
docker exec "$WEB" python3 -c "
import os, shutil
src = '/var/lib/odoo/.local/share/Odoo/filestore/${SOURCE_DB_NAME}'
dst = '/var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME}'
os.makedirs(dst, exist_ok=True)
copied = 0
for root, dirs, files in os.walk(src):
    rel = os.path.relpath(root, src)
    dst_dir = os.path.join(dst, rel) if rel != '.' else dst
    os.makedirs(dst_dir, exist_ok=True)
    for f in files:
        s, d = os.path.join(root, f), os.path.join(dst_dir, f)
        if not os.path.exists(d):
            shutil.copy2(s, d)
            copied += 1
print(f'filestore sync: {copied} new file(s) copied from ${SOURCE_DB_NAME} to ${SANITIZED_DB_NAME}')
"

echo "=== STEP 0c: build_pii_dictionary.py (dump real values from $SOURCE_DB_NAME) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/build_pii_dictionary.py

echo "=== STEP 0d: filter_pii_dictionary.py (FULL + SUBSTRING-HUNT sets) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" "$WEB" python3 -u /tmp/filter_pii_dictionary.py

echo "=== STEP 0e: build_value_mapping.py (deterministic value -> transformed mapping) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" "$WEB" python3 -u /tmp/build_value_mapping.py

echo "=== STEP 1: write_pass.py (canonical field transforms + job title flattening) ==="
docker exec -e PYTHONUNBUFFERED=1 -e SOURCE_DB_NAME="$SOURCE_DB_NAME" -i "$WEB" odoo shell -d "$SANITIZED_DB_NAME" --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < /tmp/write_pass.py

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

echo "=== STEP 4: substring_hunt_scan.py (full-db verification + auto-fix, fixed qualification rule) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" -e SOURCE_DB_NAME="$SOURCE_DB_NAME" \
    "$WEB" python3 -u /tmp/substring_hunt_scan.py

echo "=== STEP 5: rule_attachment.py (ir.attachment placeholder content) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d "$SANITIZED_DB_NAME" --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < /tmp/rule_attachment.py

echo "=== STEP 5b: rotate_app_secrets.py (database.secret rotation, ir_mail_server credential blanking) ==="
docker exec -e PYTHONUNBUFFERED=1 -e DBHOST="$DBHOST" -e DBPASS="$DBPASS" -e SANITIZED_DB_NAME="$SANITIZED_DB_NAME" \
    "$WEB" python3 -u /tmp/rotate_app_secrets.py

echo "=== STEP 6: restart $VIEW_CONTAINER (if it exists -- optional viewing container, not required for sanitization itself) ==="
docker start "$VIEW_CONTAINER" 2>/dev/null || echo "no $VIEW_CONTAINER container to restart (fine -- create one separately to browse this client's result)"

PIPELINE_END=$(date +%s)
echo "=== PIPELINE COMPLETE in $((PIPELINE_END - PIPELINE_START))s ==="
