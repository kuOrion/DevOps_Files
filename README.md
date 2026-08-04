# erp16-operations

The operational tooling for ERP16 (Orion Instruments Odoo infrastructure) —
`clients.yaml`, config templates, developer/admin scripts, the dev console,
and the sanitization pipeline. Split out from the private `ERP16` planning
repo on 2026-08-04 specifically so it can be cloned onto the sandbox and
scoped to real dev/admin identities, without exposing planning docs,
incident records, or client-commercial content to anyone else.

- `clients.yaml` — single source of truth (Odoo/Postgres version, hosting
  type, on-prem access tier, module list) per client. Generates each
  client's `docker-compose.yml`/`odoo.conf` from `templates/` at
  stack-start time.
- `templates/` — the shared Jinja templates referenced above.
- `scripts/dev-start.sh` + `scripts/dev_console/` — the developer laptop
  workflow (Option B): pull a sanitized snapshot, start the container pair,
  a git flow wrapper (Start Task/Finish Task) on top.
- `scripts/bootstrap-dev-laptop.sh` — one-time new-laptop setup.
- `scripts/render_client.py`/`render-client.sh` — internal, called by
  `dev-start.sh`, not run directly by devs.
- `scripts/publish_snapshot.sh` — publishes a sanitized snapshot to S3.
  Admin/sanitizer-side, not a dev tool.
- `scripts/check_missing_deps.py` — occasional dependency audit, run when
  the addons checkout changes, not on every boot.
- `sanitize/` — the sanitization pipeline (dictionary build → filter →
  value mapping → ORM write → substring-hunt verification → attachment
  sanitization, orchestrated by `run_pipeline.sh`). Admin/sanitizer-side.
- `docker/Dockerfile.odoo` — the image `dev-start.sh` builds from.

See `docs/specs/Deployment_Lifecycle.docx` and
`docs/specs/Secrets_Management.docx` in the `ERP16` repo for the design
these scripts implement, and `ERP16/docs/rehearsal/` for the sandbox
infrastructure this gets deployed against.
