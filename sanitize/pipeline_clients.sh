#!/bin/bash
# Shared client -> db-name mapping for the sanitization pipeline scripts
# (run_pipeline.sh, publish_snapshot.sh). One source of truth instead of
# each script keeping its own copy -- found duplicated between the two
# during the pipeline-consolidation pass, fixed before it could drift.
#
# Source db name (the real, untouched client data) and the sanitized
# scratch db name the pipeline writes to, per client. orion_test's
# "orm_test" name predates this parameterization and is kept as-is (it's
# already live -- published to S3, running on orm_test-web) rather than
# renamed for consistency's sake. New clients get a plain "<client_id>_san"
# name instead of inventing a new contraction each time.
declare -A SOURCE_DB=(
    [orion_test]=orion_test
)
declare -A SANITIZED_DB=(
    [orion_test]=orm_test
)

resolve_source_db() {
    echo "${SOURCE_DB[$1]:-$1}"
}

resolve_sanitized_db() {
    echo "${SANITIZED_DB[$1]:-${1}_san}"
}
