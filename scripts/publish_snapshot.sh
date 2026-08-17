#!/bin/bash
# Publish a client's sanitized database + filestore to S3, for
# dev-start.sh to pull down onto a developer's laptop (Option B).
#
# Two modes:
#   ./publish_snapshot.sh <client_id>              -- remote mode (original,
#     rehearsal-only): SSHes to a remote host (--host, default "sandbox") to
#     dump/tar, scp's both back to THIS machine, uploads from here.
#   ./publish_snapshot.sh <client_id> --local       -- local mode: runs
#     entirely on the machine this script is invoked on, no SSH/scp at all.
#     This is the real-production mode -- a nightly cron job must work
#     whether or not a laptop is connected, so it cannot depend on SSHing
#     out to fetch data (2026-08-15, corrected after remote mode was found
#     to be a rehearsal-only convenience, not the documented design --
#     docs/specs/Secrets_Management.docx specifies the server's own
#     credential should do this, not a laptop-mediated round-trip).
#
# Manual trigger for now (per-client, run after build/sanitize/run_pipeline.sh
# produces a fresh sanitized snapshot) -- the nightly-automated version is a
# post-demo item on the sandbox, but IS the point of --local mode on real
# production (see ROADMAP.md's sanitization-pipeline plan, Part 8).
#
# Usage: ./publish_snapshot.sh <client_id> [--local] [--host <ssh-alias>]
#   [--aws-profile <profile>]
#
# Re-pointed 2026-08-05 from the old orion_test-db_orion_test-1/
# orion_test-web_orion_test-1 shared-instance container names to
# sanitize-db/sanitize-web (the single shared working area the rest of
# the pipeline rework introduced) -- see ROADMAP.md's sanitization-rework
# entries. Run this after run_pipeline.sh; that script's own cleanup step
# only removes the raw handoff dump/filestore, never the sanitized output
# itself, so it's still sitting in sanitize-db/sanitize-web waiting here.
set -euo pipefail

CLIENT_ID="${1:?Usage: publish_snapshot.sh <client_id> [--local] [--host <ssh-alias>] [--aws-profile <profile>]}"
shift

LOCAL_MODE=false
SSH_HOST="sandbox"
# Empty by default -- real production's instance role (EC2-SSM-Execution-Role,
# extended 2026-08-17 with scoped S3 read/write on the real bucket) is picked
# up automatically by the default credential chain when --profile is omitted
# entirely. Continued sandbox/rehearsal use should pass --aws-profile
# erp16-sandbox explicitly now that this isn't the default.
AWS_PROFILE=""
AWS_REGION="ap-south-1"

while [ $# -gt 0 ]; do
    case "$1" in
        --local) LOCAL_MODE=true; shift ;;
        --host) SSH_HOST="$2"; shift 2 ;;
        --aws-profile) AWS_PROFILE="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Shared with run_pipeline.sh -- one source of truth for the client ->
# sanitized-db-name mapping instead of each script keeping its own copy
# (found duplicated between the two, fixed during pipeline consolidation).
source "$(dirname "${BASH_SOURCE[0]}")/../sanitize/pipeline_clients.sh"
SANITIZED_DB_NAME=$(resolve_sanitized_db "$CLIENT_ID")
if [ "$SANITIZED_DB_NAME" = "${CLIENT_ID}_san" ] && [ "$CLIENT_ID" != "orion_test" ]; then
    echo "WARNING: no explicit sanitized-db mapping for '$CLIENT_ID' in pipeline_clients.sh -- assuming '${SANITIZED_DB_NAME}'. Confirm this matches what run_pipeline.sh actually created." >&2
fi

BUCKET="orion-instruments-erp16-bucket"
S3_PREFIX="s3://${BUCKET}/sanitized/${CLIENT_ID}/latest"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

if [ "$LOCAL_MODE" = true ]; then
    echo "=== [local mode] Dumping ${SANITIZED_DB_NAME} on this machine ==="
    docker exec sanitize-db pg_dump -U odoo --no-owner --no-privileges -Fc "${SANITIZED_DB_NAME}" > "$WORKDIR/db.dump"

    echo "=== [local mode] Tarring filestore on this machine ==="
    # Tar the CONTENTS of the sanitized db's filestore folder, not the folder
    # itself -- see the remote-mode comment below for why this matters, same
    # reasoning applies here.
    docker exec sanitize-web tar -C "/var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME}" -czf "/tmp/publish_${CLIENT_ID}_filestore.tar.gz" .
    docker cp "sanitize-web:/tmp/publish_${CLIENT_ID}_filestore.tar.gz" "$WORKDIR/filestore.tar.gz"
    docker exec -u root sanitize-web rm -f "/tmp/publish_${CLIENT_ID}_filestore.tar.gz"
else
    echo "=== [remote mode, rehearsal-only] Dumping ${SANITIZED_DB_NAME} on ${SSH_HOST} ==="
    ssh "$SSH_HOST" "docker exec sanitize-db pg_dump -U odoo --no-owner --no-privileges -Fc ${SANITIZED_DB_NAME} > /tmp/publish_${CLIENT_ID}.dump"

    echo "=== [remote mode] Tarring filestore on ${SSH_HOST} ==="
    # Tar the CONTENTS of the sanitized db's filestore folder, not the folder
    # itself -- the tarball must have no leading directory name, since the
    # developer's local db is named differently (client_id, e.g. "orion_test")
    # than the sandbox's sanitized copy (e.g. "orm_test"). A tarball with an
    # "orm_test/" prefix extracts to the wrong path on the developer's
    # machine and every attachment/asset lookup 404s -- found exactly this
    # way during dev-start.sh's first real end-to-end test.
    ssh "$SSH_HOST" "docker exec sanitize-web tar -C /var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME} -czf /tmp/publish_${CLIENT_ID}_filestore.tar.gz ."
    ssh "$SSH_HOST" "docker cp sanitize-web:/tmp/publish_${CLIENT_ID}_filestore.tar.gz /tmp/publish_${CLIENT_ID}_filestore.tar.gz"

    echo "=== [remote mode] Pulling both artifacts back to this machine ==="
    scp -q "${SSH_HOST}:/tmp/publish_${CLIENT_ID}.dump" "$WORKDIR/db.dump"
    scp -q "${SSH_HOST}:/tmp/publish_${CLIENT_ID}_filestore.tar.gz" "$WORKDIR/filestore.tar.gz"

    echo "=== [remote mode] Cleaning up ${SSH_HOST}-side temp files ==="
    ssh "$SSH_HOST" "rm -f /tmp/publish_${CLIENT_ID}.dump /tmp/publish_${CLIENT_ID}_filestore.tar.gz; docker exec sanitize-web rm -f /tmp/publish_${CLIENT_ID}_filestore.tar.gz"
fi

# Same "omit --profile entirely when empty" convention already used by
# render_client.py's _aws_cmd -- an explicit --profile "" confuses the aws
# CLI rather than falling back cleanly, so build the flag array conditionally.
AWS_PROFILE_ARGS=()
[ -n "$AWS_PROFILE" ] && AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

echo "=== Uploading to ${S3_PREFIX} (profile: ${AWS_PROFILE:-<instance role / default chain>}) ==="
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORKDIR/published_at.txt"
echo "$SANITIZED_DB_NAME" > "$WORKDIR/source_db.txt"
aws s3 cp "$WORKDIR/db.dump" "${S3_PREFIX}/db.dump" "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/filestore.tar.gz" "${S3_PREFIX}/filestore.tar.gz" "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/published_at.txt" "${S3_PREFIX}/published_at.txt" "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/source_db.txt" "${S3_PREFIX}/source_db.txt" "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION"

echo "=== Published: ${S3_PREFIX} ==="
du -sh "$WORKDIR/db.dump" "$WORKDIR/filestore.tar.gz"
