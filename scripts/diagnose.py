#!/usr/bin/env python3
"""
Objective, judgment-free diagnostic dump for a live client (or all of
them) -- prints raw numbers only: host health, container resource use,
Postgres session/lock/table state, index inventory on a watched set of
tables, per-day error/timeout counts, and basic security-log counts.

Built 2026-08-27 after two consecutive days of investigating orion-internal
slowness by hand, one `docker exec ... psql ...` command at a time. The
point of this script is specifically NOT to diagnose or conclude anything
-- it exists so a client-side developer can be handed real numbers and
draw their own conclusion about whether something is a DevOps/infra
problem or a code/schema problem, without having to trust our narrative
of what we think is going on. Every section prints facts; nothing here
says "this is bad" or "this is the cause."

Read-only. Never writes, vacuums, deletes, or modifies anything. Safe to
run at any time, including during a live incident.

Usage:
    ./diagnose.py                       # all live cloud clients
    ./diagnose.py orion-internal        # one client only
    ./diagnose.py --days 20             # lookback window for log sections (default 15)
    ./diagnose.py --tables stock_move_line,stock_move,stock_quant,stock_quant_package
                                         # override the watched-table list for section C/D
"""
import argparse
import datetime
import glob
import json
import subprocess
import sys
from pathlib import Path

import yaml

CLIENTS_YAML = Path(__file__).resolve().parent.parent / "clients.yaml"
LOG_DIR = Path("/opt/erp16/logs")
DEFAULT_WATCHED_TABLES = [
    "stock_move_line",
    "stock_move",
    "stock_quant",
    "stock_quant_package",
]


def sh(cmd, timeout=30):
    """Run a shell command, return stdout (empty string on failure)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""


def psql(db_container, db_name, query, timeout=30):
    """Run a query via docker exec + psql, tuples-only unaligned, pipe-separated."""
    escaped = query.replace('"', '\\"')
    cmd = f'sudo docker exec {db_container} psql -U odoo -d {db_name} -t -A -F"|" -c "{escaped}"'
    return sh(cmd, timeout=timeout)


def load_live_clients(explicit_client_id=None):
    with open(CLIENTS_YAML) as f:
        data = yaml.safe_load(f)
    clients = data["clients"]
    result = []
    for client_id, cfg in clients.items():
        if explicit_client_id and client_id != explicit_client_id:
            continue
        if not explicit_client_id and cfg.get("hosting") != "cloud":
            continue
        if not explicit_client_id and not cfg.get("domain"):
            continue
        result.append((client_id, cfg.get("db_name", client_id)))
    return result


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def host_health():
    section("A. HOST HEALTH")
    print(sh("uptime").strip())
    print(sh("free -h").strip())
    print(sh("df -h /").strip())


def container_status_and_resources():
    section("B. CONTAINER STATUS + LIVE RESOURCE SNAPSHOT")
    print(sh('sudo docker ps -a --format "table {{.Names}}\\t{{.Status}}"').strip())
    print()
    print(sh('sudo docker stats --no-stream --format "table {{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.MemPerc}}"').strip())


def postgres_sessions(client_id, db_container, db_name):
    section(f"C. POSTGRES SESSION STATE — {client_id}")
    out = psql(
        db_container, db_name,
        "SELECT pid, state, wait_event_type, "
        "EXTRACT(EPOCH FROM (now()-query_start))::int AS query_age_seconds, "
        "EXTRACT(EPOCH FROM (now()-xact_start))::int AS xact_age_seconds, "
        "left(query,100) FROM pg_stat_activity "
        "WHERE datname='" + db_name + "' AND state != 'idle' "
        "ORDER BY xact_age_seconds DESC NULLS LAST LIMIT 15;",
    )
    print("pid|state|wait_event_type|query_age_s|xact_age_s|query_prefix")
    print(out.strip() or "(no non-idle sessions)")

    blocking = psql(
        db_container, db_name,
        "SELECT count(*) FROM pg_locks WHERE NOT granted;",
    )
    print(f"\nlock_waits_not_granted_count: {blocking.strip()}")


def table_stats(client_id, db_container, db_name, tables):
    section(f"C2. TABLE STATS (dead tuples / vacuum-analyze recency) — {client_id}")
    table_list = ",".join(f"'{t}'" for t in tables)
    out = psql(
        db_container, db_name,
        f"SELECT relname, n_live_tup, n_dead_tup, "
        f"CASE WHEN n_live_tup+n_dead_tup=0 THEN 0 ELSE round(100.0*n_dead_tup/(n_live_tup+n_dead_tup),1) END AS dead_pct, "
        f"last_vacuum, last_autovacuum, last_analyze, last_autoanalyze "
        f"FROM pg_stat_user_tables WHERE relname IN ({table_list}) ORDER BY n_dead_tup DESC;",
    )
    print("relname|n_live_tup|n_dead_tup|dead_pct|last_vacuum|last_autovacuum|last_analyze|last_autoanalyze")
    print(out.strip() or "(no matching tables)")


def index_inventory(client_id, db_container, db_name, tables):
    section(f"D. INDEX INVENTORY — {client_id}")
    for t in tables:
        out = psql(db_container, db_name, f"SELECT indexname, indexdef FROM pg_indexes WHERE tablename='{t}';")
        print(f"-- {t} --")
        print(out.strip() or "(no indexes found / table does not exist)")
        print()


def cron_state(client_id, db_container, db_name):
    section(f"E. IR_CRON STATE (active jobs) — {client_id}")
    out = psql(
        db_container, db_name,
        "SELECT id, cron_name->>'en_US', active, lastcall, nextcall, interval_number, interval_type "
        "FROM ir_cron WHERE active=true ORDER BY lastcall DESC NULLS LAST;",
    )
    print("id|name|active|lastcall|nextcall|interval_number|interval_type")
    print(out.strip() or "(no active crons)")


def daily_timeout_counts(client_id, web_container, days):
    section(f"F. WORKER TIMEOUT COUNT PER DAY, last {days}d — {client_id}")
    out = sh(
        f'sudo docker logs {web_container} 2>&1 | '
        r'grep -oE "^2026-[0-9]{2}-[0-9]{2}.*timeout after 120s" | cut -c1-10 | sort | uniq -c',
        timeout=60,
    )
    print(out.strip() or "(no timeout lines found in retained container log)")


def daily_haproxy_status_counts(client_id, days):
    section(f"G. HAPROXY STATUS CODE COUNTS PER DAY, last {days}d — {client_id}")
    # Reports the exact 'client' string as logged, with no attribution
    # guessing. A handful of entries have been observed with client values
    # that don't match any real backend name (e.g. "p-in", "ps-in") --
    # _parse_haproxy_line's own regex can't produce a truncated backend
    # name, so this looks like a separate, real, unexplained data-quality
    # issue in the collector (possibly concurrent writer threads
    # interleaving into the same file) -- not something to paper over by
    # guessing which client it "really" was. Anomalous labels are reported
    # under their own literal string, visibly, not folded into a client's
    # count.
    today = datetime.date.today()
    for i in range(days, -1, -1):
        d = today - datetime.timedelta(days=i)
        fn = LOG_DIR / "haproxy" / f"{d.isoformat()}.jsonl"
        if not fn.exists():
            continue
        counts = {}
        with open(fn) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                label = e.get("client")
                if label != client_id:
                    continue
                status = str(e.get("status"))
                counts[status] = counts.get(status, 0) + 1
        total = sum(counts.values())
        breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"{d.isoformat()} | total={total} | {breakdown}")


def haproxy_anomalous_client_labels(days):
    section(f"G2. HAPROXY CLIENT LABELS NOT MATCHING ANY KNOWN client_id, last {days}d")
    known = {c for c, _ in load_live_clients()}
    today = datetime.date.today()
    counts = {}
    for i in range(days, -1, -1):
        d = today - datetime.timedelta(days=i)
        fn = LOG_DIR / "haproxy" / f"{d.isoformat()}.jsonl"
        if not fn.exists():
            continue
        with open(fn) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                label = e.get("client")
                if label is None or label in known:
                    continue
                counts[label] = counts.get(label, 0) + 1
    if counts:
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{label!r}: {n}")
    else:
        print("(none found)")


def security_log_counts(days):
    section(f"H. SECURITY-LOG COUNTS (global, not per-client), last {days}d")
    today = datetime.date.today()
    total_fail = 0
    fail_by_ip = {}
    unknown_success = 0
    for i in range(days, -1, -1):
        d = today - datetime.timedelta(days=i)
        fn = LOG_DIR / "ssh" / f"{d.isoformat()}.jsonl"
        if not fn.exists():
            continue
        with open(fn) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("event") == "login_failure":
                    total_fail += 1
                    ip = e.get("src_ip", "?")
                    fail_by_ip[ip] = fail_by_ip.get(ip, 0) + 1
                elif e.get("event") == "login_success" and "unknown" in str(e.get("identity", "")).lower():
                    unknown_success += 1
    print(f"ssh_login_failures_total: {total_fail}")
    print(f"ssh_login_failures_distinct_ips: {len(fail_by_ip)}")
    top = sorted(fail_by_ip.items(), key=lambda kv: -kv[1])[:5]
    print(f"ssh_login_failures_top5_ips: {top}")
    print(f"ssh_unknown_identity_successful_logins: {unknown_success}")

    model_audit_findings = 0
    for i in range(days, -1, -1):
        d = today - datetime.timedelta(days=i)
        fn = LOG_DIR / "model_audit" / f"{d.isoformat()}.jsonl"
        if not fn.exists():
            continue
        with open(fn) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                model_audit_findings += e.get("total_findings", 0) or 0
    print(f"model_audit_total_findings: {model_audit_findings}")

    sys_audit_critical = 0
    for i in range(days, -1, -1):
        d = today - datetime.timedelta(days=i)
        fn = LOG_DIR / "system_audit" / f"{d.isoformat()}.jsonl"
        if not fn.exists():
            continue
        with open(fn) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("level") == "critical":
                    sys_audit_critical += 1
    print(f"system_audit_critical_count: {sys_audit_critical}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client_id", nargs="?", default=None, help="single client_id, e.g. orion-internal (default: all live cloud clients)")
    ap.add_argument("--days", type=int, default=15, help="lookback window for per-day log sections (default 15)")
    ap.add_argument("--tables", default=",".join(DEFAULT_WATCHED_TABLES), help="comma-separated table list for table-stats/index sections")
    args = ap.parse_args()

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    clients = load_live_clients(args.client_id)
    if not clients:
        print(f"No matching client found for '{args.client_id}'", file=sys.stderr)
        sys.exit(1)

    print(f"# diagnose.py run at {datetime.datetime.utcnow().isoformat()}Z UTC")
    print(f"# clients: {[c for c, _ in clients]}")
    print(f"# watched tables: {tables}")
    print(f"# lookback days: {args.days}")

    host_health()
    container_status_and_resources()

    for client_id, db_name in clients:
        db_container = f"live-{client_id}-db"
        web_container = f"live-{client_id}-web"

        postgres_sessions(client_id, db_container, db_name)
        table_stats(client_id, db_container, db_name, tables)
        index_inventory(client_id, db_container, db_name, tables)
        cron_state(client_id, db_container, db_name)
        daily_timeout_counts(client_id, web_container, args.days)
        daily_haproxy_status_counts(client_id, args.days)

    haproxy_anomalous_client_labels(args.days)
    security_log_counts(args.days)

    print("\n# end of report — no interpretation included by design")


if __name__ == "__main__":
    main()
