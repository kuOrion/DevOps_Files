#!/usr/bin/env bash
# Migrate a client from the legacy shared-cluster volume into its own
# live-<client_id>-db/-web container pair under the new naming.
#
# Lessons baked in from the parus_instruments migration (2026-08-05):
#   1. render_client.py resolves secrets via AWS SSM -- must run wherever
#      `aws`+the erp16-sandbox profile actually live (locally), not on the
#      sandbox itself (no aws CLI there). Two paths in its output (the
#      Docker build context, the odoo.conf volume mount) come out as local
#      paths and need rewriting to the sandbox's /home/ubuntu/... before
#      deploying -- done here automatically, not by hand.
#   2. NEVER bring up `web` before the real data is restored -- Odoo
#      auto-initializes an empty demo database on first boot if none
#      exists, silently clobbering the slot you're about to restore into.
#      Fix: start `db` alone, restore, *then* start `web`.
#   3. Compiled asset-bundle ir.attachment rows (ir.ui.view + JS/CSS) can
#      reference filestore files missing from filestore_staging -- clear
#      just those (never anything else) so Odoo regenerates them fresh,
#      instead of discovering a blank UI and debugging it live.
#
# Usage: ./migrate_client_to_live.sh <client_id>
# Requires: run from a machine with `aws` configured under profile
# erp16-sandbox, and passwordless SSH to the `sandbox` host alias.

set -euo pipefail

CLIENT_ID="${1:?Usage: $0 <client_id>}"
SANDBOX_HOME="/home/ubuntu"
LOCAL_OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH_DIR="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_DIR"' EXIT

echo "=== [$CLIENT_ID] 1/7: infection pass against the legacy volume ==="
ssh sandbox "docker rm -f tmp-migrate-check 2>/dev/null; docker run -d --name tmp-migrate-check -v orion_test_odoo-db-data-orion_test:/var/lib/postgresql/data -e POSTGRES_PASSWORD=dummy postgres:14.15 >/dev/null"
sleep 4
INFECTION_HITS=$(ssh sandbox "docker exec tmp-migrate-check psql -U odoo -d $CLIENT_ID -tAc \"
  select count(*) from ir_act_server where name::text ilike '%\\\"_ep\\\"%' or name::text ilike '%_db_health_monitor%' or name::text ilike '%_bd_assemble%' or name::text ilike '%_rce_tmp%';
\"")
NONSTANDARD_ACTIONS=$(ssh sandbox "docker exec tmp-migrate-check psql -U odoo -d $CLIENT_ID -tAc \"select count(*) from ir_act_server where state='code' and create_uid != 1;\"")
SUSPICIOUS_PARAMS=$(ssh sandbox "docker exec tmp-migrate-check psql -U odoo -d $CLIENT_ID -tAc \"select count(*) from ir_config_parameter where key ~ '^_[a-z]';\"")
INJECTED_VIEWS=$(ssh sandbox "docker exec tmp-migrate-check psql -U odoo -d $CLIENT_ID -tAc \"select count(*) from ir_ui_view where (inherit_id=180 or key ilike 'gen_key%') and create_uid != 1;\"")

if [ "$INFECTION_HITS" -gt 0 ] || [ "$NONSTANDARD_ACTIONS" -gt 0 ] || [ "$SUSPICIOUS_PARAMS" -gt 0 ] || [ "$INJECTED_VIEWS" -gt 0 ]; then
  echo "!!! INFECTION PASS FAILED for $CLIENT_ID -- STOPPING, not migrating."
  echo "    known-signature hits: $INFECTION_HITS, non-system actions: $NONSTANDARD_ACTIONS, suspicious params: $SUSPICIOUS_PARAMS, injected views: $INJECTED_VIEWS"
  exit 1
fi
echo "    clean (0 hits across all checks)"

echo "=== [$CLIENT_ID] 2/7: render locally, fix paths for the sandbox ==="
cd "$LOCAL_OPS_DIR"
python3 scripts/render_client.py "$CLIENT_ID" \
  --container-prefix "live-$CLIENT_ID" \
  --addons-path "$SANDBOX_HOME/erp16-custom-addons" \
  --out "$SCRATCH_DIR/render" \
  --aws-profile erp16-sandbox

sed -i \
  -e "s#$LOCAL_OPS_DIR/docker#$SANDBOX_HOME/DevOps_Files/docker#" \
  -e "s#$SCRATCH_DIR/render/config#$SANDBOX_HOME/DevOps_Files/generated/live/$CLIENT_ID/config#" \
  "$SCRATCH_DIR/render/docker-compose.yml"

echo "=== [$CLIENT_ID] 3/7: sync rendered files to the sandbox ==="
ssh sandbox "mkdir -p ~/DevOps_Files/generated/live/$CLIENT_ID/config"
scp -q "$SCRATCH_DIR/render/docker-compose.yml" "sandbox:~/DevOps_Files/generated/live/$CLIENT_ID/docker-compose.yml"
scp -q "$SCRATCH_DIR/render/config/odoo.conf" "sandbox:~/DevOps_Files/generated/live/$CLIENT_ID/config/odoo.conf"

echo "=== [$CLIENT_ID] 4/7: start db ONLY (not web -- avoids the auto-init trap) ==="
ssh sandbox "cd ~/DevOps_Files/generated/live/$CLIENT_ID && docker compose up -d db"
sleep 5

echo "=== [$CLIENT_ID] 5/7: restore real data from the legacy volume ==="
ssh sandbox "docker exec live-$CLIENT_ID-db psql -U odoo -d postgres -tAc \"DROP DATABASE IF EXISTS $CLIENT_ID;\""
ssh sandbox "docker exec live-$CLIENT_ID-db psql -U odoo -d postgres -c 'CREATE DATABASE $CLIENT_ID OWNER odoo;'"
ssh sandbox "docker exec tmp-migrate-check pg_dump -U odoo -d $CLIENT_ID -Fc" > "$SCRATCH_DIR/$CLIENT_ID.dump"
scp -q "$SCRATCH_DIR/$CLIENT_ID.dump" "sandbox:/tmp/$CLIENT_ID.dump"
ssh sandbox "docker cp /tmp/$CLIENT_ID.dump live-$CLIENT_ID-db:/tmp/$CLIENT_ID.dump"
ssh sandbox "docker exec live-$CLIENT_ID-db pg_restore -U odoo -d $CLIENT_ID --no-owner --no-acl /tmp/$CLIENT_ID.dump" || true
ssh sandbox "docker rm -f tmp-migrate-check"

echo "=== [$CLIENT_ID] 6/7: copy filestore, clear only-genuinely-missing asset-bundle cache ==="
ssh sandbox "cd ~/DevOps_Files/generated/live/$CLIENT_ID && docker compose up -d web"
sleep 5
ssh sandbox "docker exec live-$CLIENT_ID-web mkdir -p /var/lib/odoo/.local/share/Odoo/filestore/"
ssh sandbox "docker cp ~/filestore_staging/filestore/$CLIENT_ID live-$CLIENT_ID-web:/var/lib/odoo/.local/share/Odoo/filestore/$CLIENT_ID" || echo "    (no filestore_staging entry for $CLIENT_ID -- skipping)"

# filestore_staging content pulled directly from production as the `ubuntu`
# host user (uid 1000) doesn't match the container's `odoo` user (uid 101) --
# docker cp preserves source ownership as-is. Without this, ir.attachment
# unlink() fails later with FileNotFoundError trying to write a GC marker
# file (found live, 2026-08-05, orion_test). -u root is required since the
# odoo user itself can't chown files it doesn't own.
ssh sandbox "docker exec -u root live-$CLIENT_ID-web chown -R odoo:odoo /var/lib/odoo/.local/share/Odoo/filestore/$CLIENT_ID"

DB_PASS=$(grep -oP 'POSTGRES_PASSWORD: \K.*' "$SCRATCH_DIR/render/docker-compose.yml" | head -1)
ssh sandbox "docker exec live-$CLIENT_ID-db psql -U odoo -d $CLIENT_ID -tAc \"select store_fname from ir_attachment where store_fname is not null order by store_fname;\"" > "$SCRATCH_DIR/dbrefs.txt"
ssh sandbox "docker exec live-$CLIENT_ID-web find /var/lib/odoo/.local/share/Odoo/filestore/$CLIENT_ID -type f -printf '%P\n' 2>/dev/null | sort" > "$SCRATCH_DIR/disk.txt"
comm -23 "$SCRATCH_DIR/dbrefs.txt" "$SCRATCH_DIR/disk.txt" > "$SCRATCH_DIR/missing.txt"
MISSING_COUNT=$(wc -l < "$SCRATCH_DIR/missing.txt")
if [ "$MISSING_COUNT" -gt 0 ]; then
  MISSING_PY=$(python3 -c "print([l.strip() for l in open('$SCRATCH_DIR/missing.txt') if l.strip()])")
  echo "
missing = $MISSING_PY
recs = env['ir.attachment'].search([('store_fname','in',missing),'|',('res_model','=','ir.ui.view'),('mimetype','in',['application/javascript','text/css'])])
non_cache = env['ir.attachment'].search([('store_fname','in',missing)]) - recs
if non_cache:
    print('WARNING: %d missing files are real business attachments, not cache -- left alone, needs separate investigation:' % len(non_cache))
    for r in non_cache[:20]:
        print(' ', r.id, r.res_model, r.mimetype, r.name)
recs.unlink()
env.cr.commit()
print('Cleared %d stale asset-bundle cache rows' % len(recs))
" | ssh sandbox "docker exec -i live-$CLIENT_ID-web odoo shell -c /etc/odoo/odoo.conf -d $CLIENT_ID --db_host=db --db_user=odoo --db_password=$DB_PASS --no-http" 2>&1 | grep -E "WARNING|Cleared|res\.|product\.|ir\.ui"
fi

echo "=== [$CLIENT_ID] 7/7: verify ==="
HTTP_CODE=$(ssh sandbox "curl -s -o /dev/null -w '%{http_code}' http://localhost:\$(grep -oP '\"127.0.0.1:\K[0-9]+(?=:8069)' ~/DevOps_Files/generated/live/$CLIENT_ID/docker-compose.yml)/web/login")
echo "    login page HTTP: $HTTP_CODE"
ssh sandbox "docker logs live-$CLIENT_ID-web --tail 5 2>&1"

echo "=== [$CLIENT_ID] done. Reminder: run the login-page DOM check (read_page filter=all) before treating this as verified clean. ==="
