# build/

This is where the actual redesign implementation lands — as opposed to `docs/`,
which holds planning documents and the accepted proposal.

Expected contents as the redesign progresses:

- `clients.yaml` — single source of truth (Odoo/Postgres version, hosting type,
  on-prem access tier, module list) per client. Generates each client's
  `docker-compose.yml`/`odoo.conf` from a shared template at stack-start time.
- `docker-compose.template.yml` — the shared template referenced above.
- `scripts/dev-start.sh` — developer laptop stack startup (Option B).
- `scripts/deploy.sh` — production deployment (per-client container pairs).
- `scripts/install_release.sh` — on-premise Tier 2 git-pull deployment.

See `docs/specs/Deployment_Lifecycle.docx` and `docs/specs/Secrets_Management.docx`
for the design these scripts implement, and the sandbox EC2 (see CLAUDE.md) for
where to build/test them against real data before touching production.
