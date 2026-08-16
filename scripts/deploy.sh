#!/bin/bash
# Promote a reviewed-in-staging commit to live-area, for every cloud client
# at once -- live-area is one shared worktree (Live_copy_of_Addons), not
# per-client, so a deploy always applies to every cloud client
# simultaneously. Per-client backup/restore stays independent even though
# the code promotion doesn't.
#
# Built and tested incrementally, one subcommand at a time -- each is
# independently runnable/testable before being wired into the full flow:
#   deploy.sh backup <client_id>              -- pre-deploy backup for one client
#   deploy.sh promote <commit>                -- move the live worktree forward
#   deploy.sh healthcheck <client_id>         -- thin liveness check for one client
#   deploy.sh rollback <client_id> <backup_dir> -- restore one client from a backup
#   deploy.sh <commit>                        -- the real full flow (not yet built)
#
# Backups land on sandbox LOCAL DISK at ~/Backups/<client_id>/<timestamp>/
# -- fast restore, minimizes live downtime. Deliberately distinct from the
# daily Kaustubha Udyog disaster-recovery server (off-box, different job).
set -euo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENTS_YAML="$BUILD_DIR/clients.yaml"
LIVE_WORKTREE="$HOME/Live_copy_of_Addons"
BACKUPS_DIR="$HOME/Backups"
DEPLOY_LOG_DIR="/opt/erp16/logs/deploy"

fail() { echo "ERROR: $1" >&2; exit 1; }

# Deploy history -- part of the logging/audit design (docs/ROADMAP.md,
# 2026-08-06): dense JSONL, one file per day, UTC timestamps throughout so
# this correlates against every other log source by plain string sort.
# Audit-bucket source (kept forever, unlike routine logs) -- deploy events
# are inherently low-volume and this is the only durable record that a
# deploy happened at all, since deploy.sh previously only ever printed to
# stdout and nothing persisted after the terminal closed.
write_deploy_log() {
    local result="$1" target="$2" previous="$3" clients="$4" failed="$5" duration="$6"
    mkdir -p "$DEPLOY_LOG_DIR"
    local logfile="$DEPLOY_LOG_DIR/$(date -u +%Y-%m-%d).jsonl"
    # Environment variables, not string-interpolated into the python
    # source -- list_cloud_clients() returns one client per line, and
    # $clients/$failed carry those raw newlines. Interpolating that
    # straight into a single-quoted Python string literal breaks the
    # instant it contains more than one client (found live, 2026-08-06):
    # the shell substitutes the newline in place, and Python can't parse
    # a raw newline inside '...'. Env vars sidestep this whole class of
    # bug regardless of what characters end up in any of these values.
    DEPLOY_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    DEPLOY_RESULT="$result" DEPLOY_TARGET="$target" DEPLOY_PREVIOUS="$previous" \
    DEPLOY_CLIENTS="$clients" DEPLOY_FAILED="$failed" DEPLOY_DURATION="$duration" \
    python3 -c "
import json, os
entry = {
    'ts': os.environ['DEPLOY_TS'],
    'source': 'deploy',
    'level': 'audit',
    'result': os.environ['DEPLOY_RESULT'],
    'target_commit': os.environ['DEPLOY_TARGET'],
    'previous_commit': os.environ['DEPLOY_PREVIOUS'],
    'clients': os.environ['DEPLOY_CLIENTS'].split(),
    'failed_clients': os.environ['DEPLOY_FAILED'].split(),
    'duration_seconds': int(os.environ['DEPLOY_DURATION']),
}
print(json.dumps(entry))
" >> "$logfile"
}

# Real deployable cloud clients only -- clients.yaml has no field that
# actually distinguishes these from the sanitize/staging working areas
# (both are also marked hosting: cloud), so exclude them by name
# explicitly rather than rely on a filter that doesn't exist.
list_cloud_clients() {
    python3 -c "
import yaml
cfg = yaml.safe_load(open('$CLIENTS_YAML'))['clients']
exclude = {'sanitize', 'staging'}
for cid, c in cfg.items():
    if c.get('hosting') == 'cloud' and cid not in exclude:
        print(cid)
"
}

db_name_for() {
    python3 -c "
import yaml
cfg = yaml.safe_load(open('$CLIENTS_YAML'))['clients']['$1']
print(cfg['db_name'])
"
}

# --- backup: pg_dump (-Fc, matching pull_from_live.sh's format) + a
# filestore tarball, taken together as one atomic pair -- restoring one
# without the other reproduces the exact "500 on compiled JS/CSS bundles"
# bug already seen twice in this project's history (mismatched
# ir.attachment rows vs. actual filestore content).
backup_client() {
    local client_id="$1"
    local db_name; db_name=$(db_name_for "$client_id")
    local ts; ts=$(date -u +%Y%m%dT%H%M%SZ)
    local dest="$BACKUPS_DIR/$client_id/$ts"
    mkdir -p "$dest"

    # Progress messages go to stderr, NOT stdout -- callers do
    # backup_dirs["$c"]=$(backup_client "$c") to capture the destination
    # path via command substitution, which grabs EVERY line of stdout,
    # not just the last. Without this, backup_dirs[$c] would end up
    # holding the whole multi-line log instead of a clean path, silently
    # breaking rollback_client's later use of it (found in the actual
    # full-flow dry run -- Step 1/3 printed nothing at all, which is
    # exactly what this bug looks like).
    echo "=== [$client_id] backing up '$db_name' -> $dest ===" >&2
    docker exec "live-$client_id-db" pg_dump -U odoo --no-owner --no-privileges -Fc "$db_name" > "$dest/db.dump"

    docker exec "live-$client_id-web" tar -C "/var/lib/odoo/.local/share/Odoo/filestore/$db_name" -czf "/tmp/backup_${client_id}_filestore.tar.gz" .
    docker cp "live-$client_id-web:/tmp/backup_${client_id}_filestore.tar.gz" "$dest/filestore.tar.gz"
    docker exec -u root "live-$client_id-web" rm -f "/tmp/backup_${client_id}_filestore.tar.gz"

    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$dest/backed_up_at.txt"
    echo "$db_name" > "$dest/db_name.txt"
    # Which code commit was actually live when this backup was taken --
    # without this, restoring the data on a different machine (or even
    # this same one, weeks later after more deploys) risks running it
    # against code the dump's ir_module_module state doesn't match. Data
    # alone was never the full "recreate this client from scratch" story
    # (found while walking through exactly that scenario, 2026-08-05).
    git -C "$LIVE_WORKTREE" rev-parse HEAD > "$dest/live_commit.txt"

    echo "=== [$client_id] done: $dest/{db.dump,filestore.tar.gz} ===" >&2
    echo "$dest"
}

# --- promote: move the live worktree to <commit>. Pure git-level action --
# no container restart here (Odoo needs an explicit process restart to
# pick up new files regardless, so combining the two would just hide
# which step actually failed if something breaks). Fetches first, into
# the neutral erp16-custom-addons checkout, since nothing else does this
# automatically (on-demand/admin-triggered by design, no cron/webhook).
promote() {
    local target="$1"
    local fetch_checkout="$HOME/erp16-custom-addons"

    echo "=== fetching latest into the neutral checkout ==="
    git -C "$fetch_checkout" fetch origin

    [ -d "$LIVE_WORKTREE" ] || fail "no live worktree at $LIVE_WORKTREE"

    local before; before=$(git -C "$LIVE_WORKTREE" rev-parse --short HEAD)
    echo "=== live worktree: $before -> $target ==="
    git -C "$LIVE_WORKTREE" checkout "$target"
    local after; after=$(git -C "$LIVE_WORKTREE" rev-parse --short HEAD)

    [ "$after" = "$(git -C "$LIVE_WORKTREE" rev-parse --short "$target")" ] || fail "worktree didn't land on the expected commit"
    echo "=== promoted: $before -> $after ==="
}

# --- earthmech release sync: mirrors whichever modules clients.yaml's
# earthmech.modules lists (only ones already proven live on cloud clients,
# by construction -- a module only gets added to that list after it's
# been through a normal cloud deploy) into the separate, scoped
# earthmech-release repo. Never pushes the full monorepo -- Deployment_
# Lifecycle.docx Sec.3 Tier 2 requires a scoped remote containing only
# that client's own module set, not a clone of everything.
#
# Runs automatically after every successful cloud deploy, but is a true
# no-op (no commit, no push) unless the mirrored content actually
# changed -- earthmech.modules is empty today (stock Odoo only, settled
# 2026-08-14), so in practice this does nothing on ordinary deploys until
# someone actually adds a module to that list.
EARTHMECH_SYNC_CHECKOUT="$HOME/Earthmech_release_sync"
EARTHMECH_RELEASE_REMOTE="git@github-earthmech:kuOrion/earthmech-release.git"

earthmech_modules() {
    python3 -c "
import yaml
cfg = yaml.safe_load(open('$CLIENTS_YAML'))['clients'].get('earthmech', {})
for m in cfg.get('modules', []) or []:
    print(m)
"
}

sync_earthmech_release() {
    local live_commit="$1"
    local modules; modules=$(earthmech_modules)

    if [ ! -d "$EARTHMECH_SYNC_CHECKOUT/.git" ]; then
        echo "=== [earthmech] cloning earthmech-release (first run) ==="
        git clone "$EARTHMECH_RELEASE_REMOTE" "$EARTHMECH_SYNC_CHECKOUT"
    else
        git -C "$EARTHMECH_SYNC_CHECKOUT" fetch origin
        git -C "$EARTHMECH_SYNC_CHECKOUT" checkout main
        git -C "$EARTHMECH_SYNC_CHECKOUT" reset --hard origin/main 2>/dev/null || true
    fi

    mkdir -p "$EARTHMECH_SYNC_CHECKOUT/modules"

    # Bootstrap: repo has no commits yet -- write the initial (today:
    # empty-modules) manifest unconditionally, once, so install_release.sh
    # always has at least one real tag to check out, even before any
    # module is ever added.
    if ! git -C "$EARTHMECH_SYNC_CHECKOUT" rev-parse HEAD >/dev/null 2>&1; then
        echo "=== [earthmech] bootstrapping empty release (stock Odoo only) ==="
        write_earthmech_manifest "$live_commit" "$modules"
        git -C "$EARTHMECH_SYNC_CHECKOUT" add -A
        git -C "$EARTHMECH_SYNC_CHECKOUT" -c user.email=deploy@erp16.local -c user.name=erp16-deploy \
            commit -m "initial release: stock Odoo only, no custom modules"
        local tag="earthmech-$(git -C "$LIVE_WORKTREE" rev-parse --short "$live_commit")"
        git -C "$EARTHMECH_SYNC_CHECKOUT" tag "$tag"
        git -C "$EARTHMECH_SYNC_CHECKOUT" push origin main "$tag"
        echo "=== [earthmech] bootstrap pushed, tagged $tag ==="
        write_earthmech_sync_log "bootstrapped" "$tag" ""
        return 0
    fi

    if [ -z "$modules" ]; then
        echo "=== [earthmech] no modules configured -- nothing to sync ==="
        return 0
    fi

    # Mirror each configured module's current content in from the live
    # worktree -- rsync --delete scoped to just that module's own
    # directory, so a file removed upstream is also removed here, without
    # touching any other module's directory.
    local m
    for m in $modules; do
        rsync -a --delete "$LIVE_WORKTREE/$m/" "$EARTHMECH_SYNC_CHECKOUT/modules/$m/"
    done

    git -C "$EARTHMECH_SYNC_CHECKOUT" add -A
    if git -C "$EARTHMECH_SYNC_CHECKOUT" diff --cached --quiet; then
        echo "=== [earthmech] modules unchanged -- nothing to sync ==="
        return 0
    fi

    write_earthmech_manifest "$live_commit" "$modules"
    git -C "$EARTHMECH_SYNC_CHECKOUT" add -A

    local tag="earthmech-$(git -C "$LIVE_WORKTREE" rev-parse --short "$live_commit")"
    echo "=== [earthmech] modules changed -- committing + tagging $tag ==="
    git -C "$EARTHMECH_SYNC_CHECKOUT" -c user.email=deploy@erp16.local -c user.name=erp16-deploy \
        commit -m "sync: $(echo "$modules" | tr '\n' ' ') @ $live_commit"
    git -C "$EARTHMECH_SYNC_CHECKOUT" tag "$tag"
    git -C "$EARTHMECH_SYNC_CHECKOUT" push origin main "$tag"
    echo "=== [earthmech] pushed, tagged $tag ==="
    write_earthmech_sync_log "synced" "$tag" "$modules"
}

write_earthmech_manifest() {
    local live_commit="$1" modules="$2"
    EM_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" EM_COMMIT="$live_commit" EM_MODULES="$modules" \
    python3 -c "
import yaml, os
manifest = {
    'odoo_version': '16.0',
    'postgres_version': '14.15',
    'source_commit': os.environ['EM_COMMIT'],
    'synced_at': os.environ['EM_TS'],
    'modules': os.environ['EM_MODULES'].split(),
}
with open('$EARTHMECH_SYNC_CHECKOUT/RELEASE_MANIFEST.yaml', 'w') as f:
    yaml.safe_dump(manifest, f, sort_keys=False)
"
}

write_earthmech_sync_log() {
    local result="$1" tag="$2" modules="$3"
    mkdir -p "$DEPLOY_LOG_DIR"
    local logfile="$DEPLOY_LOG_DIR/$(date -u +%Y-%m-%d).jsonl"
    EM_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" EM_RESULT="$result" EM_TAG="$tag" EM_MODULES="$modules" \
    python3 -c "
import json, os
entry = {
    'ts': os.environ['EM_TS'],
    'source': 'earthmech_sync',
    'level': 'audit',
    'result': os.environ['EM_RESULT'],
    'tag': os.environ['EM_TAG'],
    'modules': os.environ['EM_MODULES'].split(),
}
print(json.dumps(entry))
" >> "$logfile"
}

http_port_for() {
    python3 -c "
import yaml
cfg = yaml.safe_load(open('$CLIENTS_YAML'))['clients']['$1']
print(cfg['http_port'])
"
}

# --- restart + thin liveness check: NOT a QA gate (staging already did
# that) -- just "did the restart mechanically work". Deliberately checks
# a real asset URL, not just /web/login's HTML, because a DB/filestore
# mismatch produces a 200 on /web/login with a broken CSS/JS bundle
# underneath -- exactly the bug already seen twice in this project's
# history, and a login-page-only check would have missed both times too.
restart_and_check() {
    local client_id="$1"
    local port; port=$(http_port_for "$client_id")
    local web="live-$client_id-web"

    echo "=== [$client_id] restarting $web to pick up the new code ==="
    docker restart "$web" >/dev/null

    # NOTE: deliberately return 1 here, never call fail() (which does a
    # hard `exit`) -- this function's failures must be catchable by the
    # caller's `if ! restart_and_check ...` so one client failing doesn't
    # kill the whole full_deploy run before the other clients get checked
    # or the rollback branch runs.
    local i
    local running=false
    for i in $(seq 1 30); do
        if [ "$(docker inspect -f '{{.State.Running}}' "$web" 2>/dev/null)" = "true" ]; then
            running=true
            break
        fi
        sleep 1
    done
    if [ "$running" != "true" ]; then
        echo "=== [$client_id] FAIL: container never reached running state ==="
        return 1
    fi

    local login_code
    for i in $(seq 1 30); do
        login_code=$(curl -s -o /tmp/healthcheck_login.html -w '%{http_code}' "http://127.0.0.1:$port/web/login" || echo 000)
        [ "$login_code" = "200" ] && break
        sleep 2
    done
    if [ "$login_code" != "200" ]; then
        echo "=== [$client_id] FAIL: /web/login returned $login_code ==="
        return 1
    fi

    # pull a real asset URL out of the actual rendered page rather than
    # guess/hardcode one -- the bundle hash changes per build
    local asset_path
    asset_path=$(grep -oE '/web/assets/[A-Za-z0-9_./-]+\.(css|js)' /tmp/healthcheck_login.html | head -1)
    rm -f /tmp/healthcheck_login.html
    if [ -z "$asset_path" ]; then
        echo "=== [$client_id] FAIL: no asset URL found in /web/login response ==="
        return 1
    fi

    local asset_code
    asset_code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$asset_path")
    if [ "$asset_code" != "200" ]; then
        echo "=== [$client_id] FAIL: asset $asset_path returned $asset_code (DB/filestore mismatch signature) ==="
        return 1
    fi

    echo "=== [$client_id] OK: login=$login_code, asset $asset_path=$asset_code ==="
}

# --- rollback: restore one client's db+filestore from a backup taken by
# backup_client, as one atomic pair (same reasoning as the backup itself
# -- restoring one without the other reproduces the exact broken-asset
# bug this whole design is trying to avoid). Filestore chown is
# UNCONDITIONAL, not conditional on anything -- this exact step was
# skipped/forgotten three separate times earlier in this project's real
# history (orion_test, then retroactively on 3 more clients), so it's
# hardcoded here rather than left as a "remember to do this" step.
rollback_client() {
    local client_id="$1"
    local backup_dir="$2"
    local db="live-$client_id-db"
    local web="live-$client_id-web"

    [ -f "$backup_dir/db.dump" ] || fail "no db.dump at $backup_dir"
    [ -f "$backup_dir/filestore.tar.gz" ] || fail "no filestore.tar.gz at $backup_dir"
    local db_name; db_name=$(cat "$backup_dir/db_name.txt")

    echo "=== [$client_id] restoring '$db_name' from $backup_dir ==="
    docker cp "$backup_dir/db.dump" "$db:/tmp/rollback_${client_id}.dump"

    # Postgres refuses to DROP a database with an active session, and
    # Odoo's web worker always holds one open -- found live during this
    # script's own failure-path drill (2026-08-05): the very first
    # rollback attempt died here, aborting under set -e before any of
    # the OTHER clients got rolled back or the worktree got reverted.
    # Not optional, not a "usually fine" step -- unconditional, every time.
    docker exec "$db" psql -U odoo -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db_name';" >/dev/null
    docker exec "$db" psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS $db_name;"
    docker exec "$db" psql -U odoo -d postgres -c "CREATE DATABASE $db_name OWNER odoo;"
    docker exec "$db" pg_restore -U odoo -d "$db_name" --no-owner --no-acl -j 2 "/tmp/rollback_${client_id}.dump" || true
    docker exec "$db" rm -f "/tmp/rollback_${client_id}.dump"

    echo "=== [$client_id] restoring filestore ==="
    docker exec "$web" rm -rf "/var/lib/odoo/.local/share/Odoo/filestore/$db_name"
    docker exec "$web" mkdir -p "/var/lib/odoo/.local/share/Odoo/filestore/$db_name"
    docker cp "$backup_dir/filestore.tar.gz" "$web:/tmp/rollback_${client_id}_filestore.tar.gz"
    docker exec -u root "$web" tar -C "/var/lib/odoo/.local/share/Odoo/filestore/$db_name" -xzf "/tmp/rollback_${client_id}_filestore.tar.gz"
    docker exec -u root "$web" rm -f "/tmp/rollback_${client_id}_filestore.tar.gz"
    docker exec -u root "$web" chown -R odoo:odoo "/var/lib/odoo/.local/share/Odoo/filestore/$db_name"

    echo "=== [$client_id] rollback restore complete ==="
}

# --- full flow: back up every cloud client -> promote -> restart+check
# every client -> if ANY fail, roll back ALL of them (all-or-nothing,
# since it's one shared worktree -- you can't revert one client's code
# without reverting everyone's) -> print a summary either way.
full_deploy() {
    local target="$1"
    local start_ts; start_ts=$(date +%s)
    local clients; clients=$(list_cloud_clients)
    [ -n "$clients" ] || fail "no cloud clients found in $CLIENTS_YAML"

    local previous_commit
    previous_commit=$(git -C "$LIVE_WORKTREE" rev-parse HEAD)

    echo "=== DEPLOY START: $previous_commit -> $target ==="
    echo "=== clients: $(echo "$clients" | tr '\n' ' ') ==="
    echo

    echo "--- Step 1/3: pre-deploy backups ---"
    declare -A backup_dirs
    local c
    for c in $clients; do
        backup_dirs["$c"]=$(backup_client "$c")
    done
    echo

    echo "--- Step 2/3: promote ---"
    promote "$target"
    echo

    echo "--- Step 3/3: restart + healthcheck every client ---"
    local failed=""
    for c in $clients; do
        if ! restart_and_check "$c"; then
            failed="$failed $c"
        fi
    done
    echo

    if [ -z "$failed" ]; then
        echo "=== DEPLOY SUCCEEDED: all clients healthy on $target ==="
        write_deploy_log "success" "$target" "$previous_commit" "$clients" "" "$(( $(date +%s) - start_ts ))"
        echo
        echo "--- earthmech release sync (only fires if configured modules changed) ---"
        sync_earthmech_release "$target" || echo "=== [earthmech] sync failed -- does NOT affect the cloud deploy above, investigate separately ==="
        return 0
    fi

    echo "=== DEPLOY FAILED for:$failed ==="
    echo "=== rolling back ALL clients (shared worktree -- can't revert one without reverting everyone) ==="
    for c in $clients; do
        rollback_client "$c" "${backup_dirs[$c]}"
    done
    promote "$(git -C "$LIVE_WORKTREE" rev-parse --short "$previous_commit")"
    for c in $clients; do
        restart_and_check "$c" || echo "=== [$c] WARNING: still unhealthy after rollback -- needs manual attention ==="
    done

    echo
    echo "=== ROLLBACK SUMMARY ==="
    echo "Target commit attempted: $target"
    echo "Reverted to:             $previous_commit"
    echo "Clients that failed the healthcheck on $target:$failed"
    echo "All clients' data + code restored to pre-deploy state."
    write_deploy_log "rolled_back" "$target" "$previous_commit" "$clients" "$failed" "$(( $(date +%s) - start_ts ))"
    return 1
}

case "${1:-}" in
    backup)
        [ -n "${2:-}" ] || fail "Usage: $0 backup <client_id>"
        backup_client "$2"
        ;;
    promote)
        [ -n "${2:-}" ] || fail "Usage: $0 promote <commit>"
        promote "$2"
        ;;
    healthcheck)
        [ -n "${2:-}" ] || fail "Usage: $0 healthcheck <client_id>"
        restart_and_check "$2"
        ;;
    sync-earthmech)
        # Standalone entry point, same function full_deploy calls
        # automatically on success -- for testing/re-running the sync in
        # isolation without needing a full cloud deploy each time.
        sync_earthmech_release "$(git -C "$LIVE_WORKTREE" rev-parse HEAD)"
        ;;
    rollback)
        [ -n "${3:-}" ] || fail "Usage: $0 rollback <client_id> <backup_dir>"
        rollback_client "$2" "$3"
        ;;
    "")
        fail "Usage: $0 <commit>  |  $0 {backup|promote|healthcheck|rollback} ..."
        ;;
    *)
        # anything else is treated as the full-flow target commit
        full_deploy "$1"
        ;;
esac
