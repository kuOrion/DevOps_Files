#!/bin/bash
# Publish a client's sanitized sandbox database + filestore to S3, for
# dev-start.sh to pull down onto a developer's laptop (Option B).
#
# Manual trigger for now (per-client, run after build/sanitize/run_pipeline.sh
# produces a fresh sanitized snapshot) -- the nightly-automated version is a
# post-demo item, not needed until more than one client goes through this.
#
# Runs FROM the developer/build machine (not the sandbox itself): SSHes into
# the sandbox to dump the DB + tar the filestore, scp's both back locally,
# then uploads to S3 using this machine's already-configured `erp16-sandbox`
# AWS profile (confirmed working: full read/write on erp16-sandbox-snapshots).
#
# Usage: ./publish_snapshot.sh <client_id>
#
# Re-pointed 2026-08-05 from the old orion_test-db_orion_test-1/
# orion_test-web_orion_test-1 shared-instance container names to
# sanitize-db/sanitize-web (the single shared working area the rest of
# the pipeline rework introduced) -- see ROADMAP.md's sanitization-rework
# entries. Run this after run_pipeline.sh; that script's own cleanup step
# only removes the raw handoff dump/filestore, never the sanitized output
# itself, so it's still sitting in sanitize-db/sanitize-web waiting here.
set -euo pipefail

CLIENT_ID="${1:?Usage: publish_snapshot.sh <client_id>}"

# Shared with run_pipeline.sh -- one source of truth for the client ->
# sanitized-db-name mapping instead of each script keeping its own copy
# (found duplicated between the two, fixed during pipeline consolidation).
source "$(dirname "${BASH_SOURCE[0]}")/../sanitize/pipeline_clients.sh"
SANITIZED_DB_NAME=$(resolve_sanitized_db "$CLIENT_ID")
if [ "$SANITIZED_DB_NAME" = "${CLIENT_ID}_san" ] && [ "$CLIENT_ID" != "orion_test" ]; then
    echo "WARNING: no explicit sanitized-db mapping for '$CLIENT_ID' in pipeline_clients.sh -- assuming '${SANITIZED_DB_NAME}'. Confirm this matches what run_pipeline.sh actually created." >&2
fi

BUCKET="erp16-sandbox-snapshots"
S3_PREFIX="s3://${BUCKET}/sanitized/${CLIENT_ID}/latest"
AWS_PROFILE="erp16-sandbox"
AWS_REGION="ap-south-1"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "=== Dumping ${SANITIZED_DB_NAME} on the sandbox ==="
ssh sandbox "docker exec sanitize-db pg_dump -U odoo --no-owner --no-privileges -Fc ${SANITIZED_DB_NAME} > /tmp/publish_${CLIENT_ID}.dump"

echo "=== Tarring filestore on the sandbox ==="
# Tar the CONTENTS of the sanitized db's filestore folder, not the folder
# itself -- the tarball must have no leading directory name, since the
# developer's local db is named differently (client_id, e.g. "orion_test")
# than the sandbox's sanitized copy (e.g. "orm_test"). A tarball with an
# "orm_test/" prefix extracts to the wrong path on the developer's
# machine and every attachment/asset lookup 404s -- found exactly this
# way during dev-start.sh's first real end-to-end test.
ssh sandbox "docker exec sanitize-web tar -C /var/lib/odoo/.local/share/Odoo/filestore/${SANITIZED_DB_NAME} -czf /tmp/publish_${CLIENT_ID}_filestore.tar.gz ."
ssh sandbox "docker cp sanitize-web:/tmp/publish_${CLIENT_ID}_filestore.tar.gz /tmp/publish_${CLIENT_ID}_filestore.tar.gz"

echo "=== Pulling both artifacts back to this machine ==="
scp -q "sandbox:/tmp/publish_${CLIENT_ID}.dump" "$WORKDIR/db.dump"
scp -q "sandbox:/tmp/publish_${CLIENT_ID}_filestore.tar.gz" "$WORKDIR/filestore.tar.gz"

echo "=== Cleaning up sandbox-side temp files ==="
ssh sandbox "rm -f /tmp/publish_${CLIENT_ID}.dump /tmp/publish_${CLIENT_ID}_filestore.tar.gz; docker exec sanitize-web rm -f /tmp/publish_${CLIENT_ID}_filestore.tar.gz"

echo "=== Uploading to ${S3_PREFIX} ==="
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORKDIR/published_at.txt"
echo "$SANITIZED_DB_NAME" > "$WORKDIR/source_db.txt"
aws s3 cp "$WORKDIR/db.dump" "${S3_PREFIX}/db.dump" --profile "$AWS_PROFILE" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/filestore.tar.gz" "${S3_PREFIX}/filestore.tar.gz" --profile "$AWS_PROFILE" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/published_at.txt" "${S3_PREFIX}/published_at.txt" --profile "$AWS_PROFILE" --region "$AWS_REGION"
aws s3 cp "$WORKDIR/source_db.txt" "${S3_PREFIX}/source_db.txt" --profile "$AWS_PROFILE" --region "$AWS_REGION"

echo "=== Published: ${S3_PREFIX} ==="
du -sh "$WORKDIR/db.dump" "$WORKDIR/filestore.tar.gz"
