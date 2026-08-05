#!/usr/bin/env python3
"""
Render a client's docker-compose.yml/odoo.conf from clients.yaml + templates/.

Secrets are resolved from AWS SSM Parameter Store (SecureString) when a
client has `secrets_ref` set, via the `aws` CLI (not boto3, to keep this
script's dependencies minimal and consistent with the rest of this repo's
tooling). Falls back to build/secrets.local.yaml (gitignored) only for
clients with no secrets_ref configured. Missing parameters are auto-created
with a fresh random value on first use, matching the old local-only
behavior's convenience.
"""
import argparse
import os
import secrets
import string
import subprocess
import sys

import yaml
from jinja2 import Template

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
TEMPLATES_DIR = os.path.join(BUILD_DIR, "templates")
LOCAL_SECRETS = os.path.join(BUILD_DIR, "secrets.local.yaml")
DOCKER_DIR = os.path.join(BUILD_DIR, "docker")


def gen_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_yaml(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    with open(path) as f:
        return yaml.safe_load(f) or (default if default is not None else {})


def _aws_cmd(base_args, aws_profile, aws_region):
    cmd = ["aws"] + base_args + ["--region", aws_region]
    if aws_profile:
        cmd += ["--profile", aws_profile]
    return cmd


def ssm_get(name, aws_profile, aws_region):
    """Return the parameter's decrypted value, or None if it doesn't exist."""
    cmd = _aws_cmd(
        ["ssm", "get-parameter", "--name", name, "--with-decryption",
         "--query", "Parameter.Value", "--output", "text"],
        aws_profile, aws_region,
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if "ParameterNotFound" in result.stderr:
            return None
        raise RuntimeError(f"SSM get-parameter failed for {name}: {result.stderr.strip()}")
    return result.stdout.strip()


def ssm_put(name, value, aws_profile, aws_region):
    cmd = _aws_cmd(
        ["ssm", "put-parameter", "--name", name, "--type", "SecureString",
         "--value", value, "--overwrite"],
        aws_profile, aws_region,
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"SSM put-parameter failed for {name}: {result.stderr.strip()}")


def ssm_get_or_create(name, aws_profile, aws_region):
    value = ssm_get(name, aws_profile, aws_region)
    if value is None:
        value = gen_password()
        ssm_put(name, value, aws_profile, aws_region)
    return value


def resolve_secrets_local(client_id):
    """Stand-in for clients with no secrets_ref configured."""
    store = load_yaml(LOCAL_SECRETS, default={})
    changed = False
    entry = store.setdefault(client_id, {})
    if "db_password" not in entry:
        entry["db_password"] = gen_password()
        changed = True
    if "master_password" not in entry:
        entry["master_password"] = gen_password()
        changed = True
    if changed:
        with open(LOCAL_SECRETS, "w") as f:
            yaml.safe_dump(store, f, default_flow_style=False)
        os.chmod(LOCAL_SECRETS, 0o600)
    return entry["db_password"], entry["master_password"]


def resolve_secrets(client_id, secrets_ref, aws_profile, aws_region):
    if secrets_ref:
        db_password = ssm_get_or_create(f"{secrets_ref}/db_password", aws_profile, aws_region)
        master_password = ssm_get_or_create(f"{secrets_ref}/master_password", aws_profile, aws_region)
        return db_password, master_password
    return resolve_secrets_local(client_id)


def validate_modules(client_id, cfg, addons_path):
    if not addons_path:
        return
    missing = [m for m in cfg.get("modules", []) if not os.path.isdir(os.path.join(addons_path, m))]
    if missing:
        print(f"ERROR: client '{client_id}' lists modules not found under {addons_path}:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        sys.exit(1)


def render(template_name, context):
    with open(os.path.join(TEMPLATES_DIR, template_name)) as f:
        return Template(f.read()).render(**context)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("client_id", help="Key under `clients:` in clients.yaml")
    ap.add_argument("--container-prefix", required=True,
                     help="Scopes every container/volume name for this stack, e.g. "
                          "'live-orion-internal' (live-area), 'staging'/'sanitize' (shared "
                          "single-slot areas, no client_id), or just the bare "
                          "client_id for a developer laptop (no area concept there).")
    ap.add_argument("--addons-path", help="Host path to addons/all/latest, for module validation and the compose mount")
    ap.add_argument("--config-path", help="Host path to write/read odoo.conf from, for the compose mount")
    ap.add_argument("--out", default=None, help="Output dir (default: build/generated/<client_id>)")
    ap.add_argument("--aws-profile", default=None, help="AWS CLI profile for SSM resolution (default: whatever's active)")
    ap.add_argument("--aws-region", default="ap-south-1", help="AWS region for SSM resolution")
    ap.add_argument("--local-secrets", action="store_true",
                     help="Force the local secrets.local.yaml fallback even if the client has "
                          "a secrets_ref configured -- for developer laptops, whose scoped AWS "
                          "identity (e.g. sarthak-dev) is deliberately denied SSM access "
                          "entirely (see IDENTITIES.md). A dev's local container is fully "
                          "isolated from the sandbox's own stack, so its password never needs "
                          "to match the SSM-stored one -- confirmed live 2026-08-05, dev-start.sh "
                          "failed outright under a real scoped dev profile without this flag.")
    args = ap.parse_args()

    clients = load_yaml(CLIENTS_YAML).get("clients", {})
    if args.client_id not in clients:
        print(f"ERROR: no client '{args.client_id}' in {CLIENTS_YAML}", file=sys.stderr)
        sys.exit(1)
    cfg = clients[args.client_id]

    validate_modules(args.client_id, cfg, args.addons_path)

    secrets_ref = None if args.local_secrets else cfg.get("secrets_ref")
    db_password, master_password = resolve_secrets(
        args.client_id, secrets_ref, args.aws_profile, args.aws_region
    )

    # Always absolute -- a relative --out gets embedded as-is into the
    # rendered compose file's volume paths, which Docker Compose then
    # resolves relative to the COMPOSE FILE's own directory, not the
    # caller's cwd, silently doubling the path (found live, not guessed).
    out_dir = os.path.abspath(args.out or os.path.join(BUILD_DIR, "generated", args.client_id))
    os.makedirs(out_dir, exist_ok=True)

    context = {
        "client_id": args.client_id,
        "container_prefix": args.container_prefix,
        "db_name": cfg["db_name"],
        "odoo_version": cfg["odoo_version"],
        "postgres_version": cfg["postgres_version"],
        "list_db": str(cfg.get("list_db", False)).lower(),
        # Every existing client keeps today's behavior (locked to its own db)
        # unless clients.yaml explicitly overrides -- staging is the one area
        # that needs this empty (unfiltered), since it hosts every live
        # client's database at once for admin review.
        "dbfilter": cfg.get("dbfilter", f"^{cfg['db_name']}$"),
        "proxy_mode": str(cfg.get("proxy_mode", False)).lower(),
        "workers": cfg.get("workers", 2),
        "max_cron_threads": 1 if cfg.get("cron_enabled", False) else 0,
        "http_port": cfg["http_port"],
        "longpolling_port": cfg["longpolling_port"],
        "docker_dir": DOCKER_DIR,
        "addons_host_path": os.path.abspath(args.addons_path) if args.addons_path else "/CHANGE_ME/addons",
        "config_host_path": os.path.abspath(args.config_path) if args.config_path else os.path.join(out_dir, "config"),
        "db_password": db_password,
        "master_password": master_password,
    }

    compose_out = render("docker-compose.template.yml.j2", context)
    conf_out = render("odoo.conf.template.j2", context)

    with open(os.path.join(out_dir, "docker-compose.yml"), "w") as f:
        f.write(compose_out)

    conf_dir = context["config_host_path"]
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "odoo.conf"), "w") as f:
        f.write(conf_out)

    print(f"Rendered client '{args.client_id}' -> {out_dir}")
    print(f"  docker-compose.yml : {os.path.join(out_dir, 'docker-compose.yml')}")
    print(f"  odoo.conf          : {os.path.join(conf_dir, 'odoo.conf')}")


if __name__ == "__main__":
    main()
