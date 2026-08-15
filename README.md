# DevOps_Files

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
- `scripts/dev-start.sh` + `scripts/git_console/` (the "Git Console") — the
  developer laptop workflow (Option B): Get Latest (pull code + fresh
  sanitized data), Start Container, Send for Review (commit + push, no git
  concepts exposed).
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
- `scripts/logging_collector.py` + `scripts/erp16-logging-collector.service`
  — the 7-source logging/audit collector (Docker events, Odoo app logs,
  SSH/sudo, HAProxy access logs, Postgres connections, a model-level
  security-audit poller, a host process/system audit). Built and proven
  on the sandbox, deployed to real production 2026-08-15 — some
  environment-specific values inside (host timezone, HAProxy backend
  naming, the systemd-unit allowlist, SSH key fingerprints) needed real
  fixes when moving from sandbox to real production; re-check these on
  any future fresh-box redeploy rather than assuming parity.
- `config/haproxy.cfg` — the **sandbox rehearsal's** HAProxy config
  (pseudo-domain routing, tunnel-only, no real TLS). Reference/template
  for how per-client host-based routing should work, not what real
  production runs.
- `config/haproxy-production.cfg` — real production's **actual deployed**
  HAProxy config, captured 2026-08-15 (real TLS, real domains, public
  binds). See its own header comment for what predates this project vs.
  what Phase 3 (real implementation) added, and two real bugs found and
  fixed in the pre-existing setup (broken access logging, missing
  HTTP→HTTPS redirect).
- `config/certbot-hooks/` — the renewal hook scripts real production's
  cert setup depends on, captured 2026-08-15. Not needed by the sandbox
  (never issued a real cert). See its `README.md` for two real,
  confirmed bugs these hooks fix (not preventative hardening).
- `config/DNS_RECORDS.md` — non-secret reference for real production's
  actual DNS records and the runbook for adding a new client's
  subdomain, proven 2026-08-15.

See `docs/specs/Deployment_Lifecycle.docx` and
`docs/specs/Secrets_Management.docx` in the `ERP16` repo for the design
these scripts implement, and `ERP16/docs/rehearsal/` for the sandbox
infrastructure this gets deployed against. Note: this README predates
some of this repo's own files (e.g. `scripts/admin_console/`,
`scripts/deploy.sh`, `scripts/migrate_client_to_live.sh`,
`scripts/pull_from_live.sh`) — it was extended 2026-08-15 for real-
production-specific additions, not audited end-to-end against every
existing file.
