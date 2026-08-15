# Certbot renewal hooks — real production only

Captured 2026-08-15 from `erp16-production`'s live
`/etc/letsencrypt/renewal-hooks/`. Not used by the sandbox rehearsal
(which never issued a real cert). Deploy on a fresh box by copying each
script to the matching directory and `chmod +x`:

- `pre-haproxy-stop.sh` → `/etc/letsencrypt/renewal-hooks/pre/`
- `post-haproxy-start.sh` → `/etc/letsencrypt/renewal-hooks/post/`
- `deploy-rebuild-haproxy-bundle.sh` → `/etc/letsencrypt/renewal-hooks/deploy/`

## Why these exist — two real, confirmed bugs, not preventative hardening

1. **Certbot's `authenticator = standalone` needs port 80 free, but
   HAProxy permanently holds it.** Without the `pre`/`post` pair, the
   already-running `certbot.timer` would silently fail at its next real
   renewal — confirmed missing on real production 2026-08-15, before it
   ever actually failed, not caught reactively.
2. **HAProxy doesn't read certbot's own `/etc/letsencrypt/live/.../
   fullchain.pem` directly** — it reads a separately-maintained bundle at
   `/etc/ssl/private/erp16.orion-instruments.io.pem` that nothing else
   keeps in sync. Without the `deploy` hook, every renewal (successful or
   not) leaves HAProxy silently serving a stale/expired cert. Found this
   the hard way: certbot reported success, `openssl x509` on its own live
   cert confirmed the right SANs, but a real TLS handshake against a
   newly-added subdomain still failed — HAProxy just wasn't reading the
   file certbot had updated.

See `config/haproxy-production.cfg`'s header comment for the full context
this fits into.
