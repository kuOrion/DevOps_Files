# DNS records — real production

Non-secret reference: what's actually configured, not how to change it
(record management needs the client's own registrar login, see below).

## Registrar

`orion-instruments.io` is managed at a third-party registrar/reseller
panel — **QualiSpace** (`shop.qualispace.com`, WHMCS-based client area),
nameservers at `managedns.org`. Entirely outside AWS — not Route53, no
IAM/CLI access this project has covers it. Record changes are manual,
done by whoever holds the QualiSpace login (confirmed 2026-08-15: the
user has this access directly).

## Records this project depends on

All of the below are **A records**, all pointing to the same target —
production's public IP. If the box's IP ever changes (fresh-EC2 cutover,
Elastic IP reassignment), every one of these needs updating manually at
the registrar; nothing here auto-updates.

| Host name | Target | Client | Notes |
|---|---|---|---|
| `erp16.orion-instruments.io` | `65.0.241.134` | `orion-internal` | Pre-existing, predates this project |
| `oriontest.erp16.orion-instruments.io` | `65.0.241.134` | `orion_test` | Added 2026-08-15. **Temporary** — see clients.yaml's note on this client |
| `dbtest.erp16.orion-instruments.io` | `65.0.241.134` | `db_test` | Added 2026-08-15. **Temporary** — see clients.yaml's note on this client |
| `parusinstruments.erp16.orion-instruments.io` | `65.0.241.134` | `parus_instruments` | Added 2026-08-15 |
| `punaeyecare.erp16.orion-instruments.io` | `65.0.241.134` | `puna_eye_care` | Added 2026-08-15 |

## Naming convention

`<client_id-without-underscores>.erp16.orion-instruments.io` — a
sub-subdomain under the existing `erp16.` zone, deliberately not the bare
`orion-instruments.io` root, which also hosts the company website and
real Google Workspace email (MX/DKIM/SPF records live in the same zone,
confirmed present 2026-08-15 — smaller blast radius to stick to the
already-dedicated subzone).

## Why explicit records, not a wildcard

A `*.erp16.orion-instruments.io` wildcard would need only one DNS entry,
but forces the TLS cert onto a DNS-01 challenge — which needs either API
access to this registrar (none exists) or a manual TXT-record step at
every renewal, breaking the fully-automated `certbot` timer that
explicit-SAN + HTTP-01 keeps working (see
`config/certbot-hooks/README.md`). Adding a new client later is one more
explicit A record plus one `certbot certonly --expand -d
<new>.erp16.orion-instruments.io` — a small, known, repeatable cost
versus that tradeoff.

## Adding a new client's subdomain (the actual runbook, proven 2026-08-15)

1. Add the A record at the registrar (host name
   `<client>.erp16.orion-instruments.io`, target = the box's public IP).
2. Confirm resolution via the **authoritative** nameserver directly
   (`dig @<ns> +short <host>`) before touching anything else — the
   registrar's own panel can lag its own nameservers by several minutes.
3. `sudo systemctl stop haproxy && sudo certbot certonly --cert-name
   erp16.orion-instruments.io --expand -d erp16.orion-instruments.io -d
   <every other existing subdomain> -d <new one> --standalone
   --non-interactive --agree-tos -m admin@orion-instruments.io; sudo
   systemctl start haproxy` — brief full outage across every client on
   this box, budget for it. The `deploy` hook rebuilds HAProxy's cert
   bundle automatically; confirm with `openssl x509 -in
   /etc/ssl/private/erp16.orion-instruments.io.pem -noout -text | grep
   -A2 'Subject Alternative Name'`.
4. Add the new client's ACL (`hdr_dom(host)`) + dedicated `be_<client_id>`
   backend to `config/haproxy-production.cfg`, validate (`haproxy -c -f`),
   deploy via `reload` (not `restart`).
5. Set `proxy_mode = true` on that client in `clients.yaml`, re-render,
   restart its web container.
6. Verify against the real domain, not just the backend: HTTPS 200, DOM
   backdoor scan clean, a real compiled asset URL 200, real client IP
   showing in `docker logs` (not the Docker-bridge address).
