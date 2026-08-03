#!/bin/bash
# Full consolidated sanitization pipeline, remote orchestration.
# Runs on the sandbox host, drives docker exec into orion_test-web_orion_test-1
# (which has odoo + the addons image) against the orm_test database.
#
# set -e is critical here -- a prior run silently continued past a failed
# CREATE DATABASE (template db still had an open connection) and ran the
# whole pipeline against a stale, contaminated orm_test without anyone
# noticing until the log was reviewed after the fact. Never again: any
# failed step now aborts the whole script immediately and loudly.
set -euo pipefail

PIPELINE_START=$(date +%s)
WEB=orion_test-web_orion_test-1
DBCONT=orion_test-db_orion_test-1
DBHOST=db_orion_test
DBPASS='F0aclHkVKiTxFwCHsf6UoS26'

echo "=== STEP 0: stop ALL connections to orion_test (both web containers), drop+recreate orm_test ==="
docker stop orm_test-web || true
docker stop "$WEB"
echo "orion_test-web_orion_test-1 stopped -- template db now has zero connections"

docker exec "$DBCONT" psql -U odoo -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('orion_test','orm_test') AND pid <> pg_backend_pid();"
docker exec "$DBCONT" psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS orm_test;"
docker exec "$DBCONT" psql -U odoo -d postgres -c "CREATE DATABASE orm_test TEMPLATE orion_test;"
echo "=== orm_test recreated fresh from orion_test template ==="

echo "=== STEP 0b: restart orion_test-web_orion_test-1, wait until ready ==="
docker start "$WEB"
for i in $(seq 1 30); do
    if docker exec "$WEB" python3 -c "import psycopg2; psycopg2.connect(host='$DBHOST', dbname='orion_test', user='odoo', password='$DBPASS').close()" 2>/dev/null; then
        echo "web container DB connectivity confirmed after ${i}s"
        break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo "FATAL: web container never became ready" >&2
        exit 1
    fi
done

echo "=== STEP 1: write_pass.py (canonical field transforms + job title flattening) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d orm_test --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < /tmp/write_pass.py

echo "=== STEP 1b: reset admin login+password to a known dev value (technical account, not personal data) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d orm_test --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http <<'EOF'
u = env['res.users'].browse(2)
u.write({'login': 'admin', 'password': 'admin'})
env.cr.commit()
print(f"admin login reset: {u.login} (password also reset to a known dev value -- ORM write, properly hashed)")
EOF

echo "=== STEP 2: chatter_email_composite.py (email_from/email_to/email_cc consistent identity) ==="
docker exec -e PYTHONUNBUFFERED=1 "$WEB" python3 -u /tmp/chatter_email_composite.py

echo "=== STEP 3: chatter_bulk.py (flat placeholder + exact-match bulk fields) ==="
docker exec -e PYTHONUNBUFFERED=1 "$WEB" python3 -u /tmp/chatter_bulk.py

echo "=== STEP 4: substring_hunt_scan.py (full-db verification + auto-fix, fixed qualification rule) ==="
docker exec -e PYTHONUNBUFFERED=1 "$WEB" python3 -u /tmp/substring_hunt_scan.py

echo "=== STEP 5: rule_attachment.py (ir.attachment placeholder content) ==="
docker exec -e PYTHONUNBUFFERED=1 -i "$WEB" odoo shell -d orm_test --db_host "$DBHOST" --db_user odoo --db_password "$DBPASS" --no-http < /tmp/rule_attachment.py

echo "=== STEP 5b: rotate_app_secrets.py (database.secret rotation, ir_mail_server credential blanking) ==="
docker exec -e PYTHONUNBUFFERED=1 "$WEB" python3 -u /tmp/rotate_app_secrets.py

echo "=== STEP 6: restart orm_test-web ==="
docker start orm_test-web

PIPELINE_END=$(date +%s)
echo "=== PIPELINE COMPLETE in $((PIPELINE_END - PIPELINE_START))s ==="
