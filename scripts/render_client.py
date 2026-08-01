#!/usr/bin/env python3
"""
Render a client's docker-compose.yml/odoo.conf from clients.yaml + templates/.

Secrets are resolved from build/secrets.local.yaml (gitignored) — a local
stand-in until milestone 2 (AWS SSM Parameter Store) replaces this resolver.
Auto-generates and saves fresh random values on first use for a given client.
"""
import argparse
import os
import secrets
import string
import sys

import yaml
from jinja2 import Template

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
TEMPLATES_DIR = os.path.join(BUILD_DIR, "templates")
LOCAL_SECRETS = os.path.join(BUILD_DIR, "secrets.local.yaml")


def gen_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_yaml(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    with open(path) as f:
        return yaml.safe_load(f) or (default if default is not None else {})


def resolve_secrets(client_id):
    """TODO(milestone 2): swap this for an SSM Parameter Store resolver,
    keyed off the client's secrets_ref, without changing the caller's interface."""
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
    ap.add_argument("--addons-path", help="Host path to addons/all/latest, for module validation and the compose mount")
    ap.add_argument("--config-path", help="Host path to write/read odoo.conf from, for the compose mount")
    ap.add_argument("--out", default=None, help="Output dir (default: build/generated/<client_id>)")
    args = ap.parse_args()

    clients = load_yaml(CLIENTS_YAML).get("clients", {})
    if args.client_id not in clients:
        print(f"ERROR: no client '{args.client_id}' in {CLIENTS_YAML}", file=sys.stderr)
        sys.exit(1)
    cfg = clients[args.client_id]

    validate_modules(args.client_id, cfg, args.addons_path)

    db_password, master_password = resolve_secrets(args.client_id)

    out_dir = args.out or os.path.join(BUILD_DIR, "generated", args.client_id)
    os.makedirs(out_dir, exist_ok=True)

    context = {
        "client_id": args.client_id,
        "db_name": cfg["db_name"],
        "odoo_version": cfg["odoo_version"],
        "postgres_version": cfg["postgres_version"],
        "list_db": str(cfg.get("list_db", False)).lower(),
        "proxy_mode": str(cfg.get("proxy_mode", False)).lower(),
        "workers": cfg.get("workers", 2),
        "max_cron_threads": 1 if cfg.get("cron_enabled", False) else 0,
        "http_port": cfg["http_port"],
        "longpolling_port": cfg["longpolling_port"],
        "addons_host_path": args.addons_path or "/CHANGE_ME/addons",
        "config_host_path": args.config_path or os.path.join(out_dir, "config"),
        "db_password": db_password,
        "master_password": master_password,
    }

    compose_out = render("docker-compose.template.yml.j2", context)
    conf_out = render("odoo.conf.template.j2", context)

    with open(os.path.join(out_dir, "docker-compose.yml"), "w") as f:
        f.write(compose_out)

    conf_dir = args.config_path or os.path.join(out_dir, "config")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "odoo.conf"), "w") as f:
        f.write(conf_out)

    print(f"Rendered client '{args.client_id}' -> {out_dir}")
    print(f"  docker-compose.yml : {os.path.join(out_dir, 'docker-compose.yml')}")
    print(f"  odoo.conf          : {os.path.join(conf_dir, 'odoo.conf')}")


if __name__ == "__main__":
    main()
