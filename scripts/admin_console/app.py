#!/usr/bin/env python3
"""
ERP16 Admin Console -- the sandbox-side counterpart to Git Console.
Runs on the sandbox itself (not the admin's laptop), reachable only via
SSH tunnel + the erp16-sandbox.test pseudo-domain HAProxy routing, same
tunnel-only posture as staging.

Design locked 2026-08-06 (docs/ROADMAP.md): Pending review -> Health ->
Deploy history, no error-log panel (admin can't act on anything
technical, see the logging/audit architecture instead). One button,
"Review", always the same action regardless of staging's current state.
"Approve and deploy" only ever appears once staging's own commit
actually matches the pending commit -- a hard UI gate, not a checkbox,
mapped onto a real signal the backend already tracks.

Rebuilt 2026-08-30 around the per-client module versioning migration
(docs/PER_CLIENT_MODULE_VERSIONING.md): ONE uniform per-client status/
review/deploy model for all 5 real clients, not a split "shared repo"
vs "scoped client" shape. A client with `git_repo` set in clients.yaml
resolves its own dedicated worktrees (~/Live_copy_of_<id>,
~/Staging_copy_of_<id>); a client without it resolves the shared ones
-- purely a routing detail, invisible above _client_worktrees(). This
lets every future migrated client "graduate" with zero API/frontend
changes, only its worktree resolution silently switching underneath.

One honest exception the uniform model can't paper over: clients still
sharing the monorepo genuinely share one commit/one deploy action --
promoting the shared worktree can't be scoped to just one of them. Each
client's status exposes `deploy_peers` (the other client_ids that would
also move) so the UI can disclose this plainly instead of pretending
those cards are independent when they aren't yet.

git fetch runs on its own timer (FETCH_INTERVAL_SECONDS below), not a
button -- fetch is read-only and touches nothing live; staging and
deploy stay admin-triggered.

Every action here shells out to the same deploy.sh a human would type
by hand -- this never reimplements deploy/rollback logic itself.
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

import yaml
from flask import Flask, jsonify, request, Response

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
DEPLOY_SH = os.path.join(BUILD_DIR, "scripts", "deploy.sh")

# Shared-monorepo worktrees -- still the only path for any client without
# its own git_repo. Untouched in shape from before this rewrite.
FETCH_CHECKOUT = os.path.expanduser("~/erp16-custom-addons")
STAGING_WORKTREE = os.path.expanduser("~/Staging_copy_of_Addons")
LIVE_WORKTREE = os.path.expanduser("~/Live_copy_of_Addons")

STAGING_PORT = 8209  # clients.yaml's staging entry
DEPLOY_LOG_DIR = "/opt/erp16/logs/deploy"
BACKUPS_DIR = os.path.expanduser("~/Backups")

FETCH_INTERVAL_SECONDS = 45
PENDING_COMMIT_LIMIT = 30

app = Flask(__name__)

_state_lock = threading.Lock()
_client_state = {}  # {client_id: {live_commit, origin_commit, staging_commit, pending_commits, last_fetch_at, last_fetch_error}}

_job_lock = threading.Lock()
_client_jobs = {}  # {client_id: {kind, state, log}}

# Staging is one shared container slot, dynamically re-rendered per
# review (this box has shown real CPU/memory pressure this session --
# a separate always-on staging container per client wasn't justified by
# review frequency). Only one client's review can be valid at a time --
# reviewing client B silently invalidates client A's "ready to deploy"
# state if it was pointed at A. Tracked explicitly so the UI can show it
# and grey out Deploy everywhere except wherever staging actually is.
_staging_lock = threading.Lock()
_staging_points_at = None  # client_id, or None if never reviewed this run


def _clients_yaml():
    with open(CLIENTS_YAML) as f:
        return yaml.safe_load(f)["clients"]


def real_clients():
    """Every real cloud client -- same exclusion convention as deploy.sh's
    list_cloud_clients(): clients.yaml has no field distinguishing these
    from the sanitize/staging working areas, so exclude by name."""
    exclude = {"sanitize", "staging"}
    return {cid: cfg for cid, cfg in _clients_yaml().items() if cfg.get("hosting") == "cloud" and cid not in exclude}


def client_has_own_repo(client_id, clients=None):
    clients = clients or real_clients()
    return bool(clients.get(client_id, {}).get("git_repo"))


def client_worktrees(client_id, clients=None):
    """The one place shared-vs-scoped routing happens -- everything else
    in this file calls this and stays uniform. Returns
    (live_worktree, staging_worktree, fetch_checkout)."""
    if client_has_own_repo(client_id, clients):
        live = os.path.expanduser(f"~/Live_copy_of_{client_id}")
        staging = os.path.expanduser(f"~/Staging_copy_of_{client_id}")
        return live, staging, live  # fetch directly in the live worktree -- safe, `git fetch` never touches the working tree
    return LIVE_WORKTREE, STAGING_WORKTREE, FETCH_CHECKOUT


def client_deploy_peers(client_id, clients=None):
    """Which other clients would also move if this one's deployed --
    empty for any client with its own repo, every other shared-repo
    client for one that doesn't. Lets the UI disclose real blast radius
    instead of a per-client card implying an independence that isn't
    there yet for the clients still sharing the monorepo."""
    clients = clients or real_clients()
    if client_has_own_repo(client_id, clients):
        return []
    return sorted(cid for cid in clients if cid != client_id and not client_has_own_repo(cid, clients))


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _rev_parse(cwd, ref="HEAD"):
    r = _git(["rev-parse", ref], cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def _short(sha):
    return sha[:7] if sha else None


def _pending_commits(cwd, base, target):
    if not base or not target or base == target:
        return []
    log = _git(
        ["log", f"-{PENDING_COMMIT_LIMIT}", "--pretty=format:%H|%h|%an|%ar|%s", f"{base}..{target}"],
        cwd=cwd,
    )
    commits = []
    for line in log.stdout.strip().splitlines():
        if "|" not in line:
            continue
        full, short, author, when, subject = line.split("|", 4)
        commits.append({"hash": full, "short": short, "author": author, "when": when, "subject": subject})
    commits.reverse()  # oldest first -- reads top-to-bottom as "what happened, in order"
    return commits


def _refresh_client_state(client_id, do_fetch, clients=None):
    live_wt, staging_wt, fetch_checkout = client_worktrees(client_id, clients)
    with _state_lock:
        entry = _client_state.setdefault(client_id, {
            "live_commit": None, "origin_commit": None, "staging_commit": None,
            "pending_commits": [], "last_fetch_at": None, "last_fetch_error": None,
        })
        if do_fetch:
            fetch = _git(["fetch", "origin"], cwd=fetch_checkout)
            entry["last_fetch_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["last_fetch_error"] = None if fetch.returncode == 0 else (fetch.stderr.strip() or "fetch failed")

        live = _rev_parse(live_wt)
        origin = _rev_parse(fetch_checkout, "origin/main")
        staging = _rev_parse(staging_wt)

        entry["live_commit"] = live
        entry["origin_commit"] = origin
        entry["staging_commit"] = staging
        entry["pending_commits"] = _pending_commits(fetch_checkout, live, origin)


def _fetch_loop():
    while True:
        try:
            clients = real_clients()
            for client_id in clients:
                _refresh_client_state(client_id, do_fetch=True, clients=clients)
        except Exception:
            pass
        time.sleep(FETCH_INTERVAL_SECONDS)


def _health():
    try:
        # Every cloud-hosted container actually running on this box,
        # including staging/sanitize -- NOT real_clients() (which
        # deliberately excludes those two for the client-cards model).
        # Using real_clients() here undercounts against what docker ps
        # actually reports, producing a false "unhealthy" mismatch
        # (found live, 2026-08-30, right after this file's uniform-model
        # rewrite -- containers_up=12 vs containers_total=10).
        expected = sum(2 for c in _clients_yaml().values() if c.get("hosting") == "cloud")
    except Exception:
        expected = 0

    ps = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    running = len([l for l in ps.stdout.splitlines() if l.strip()])

    disk_pct = None
    df = subprocess.run(["df", "-P", "/"], capture_output=True, text=True)
    lines = df.stdout.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5 and parts[4].endswith("%"):
            disk_pct = int(parts[4].rstrip("%"))

    mem_pct = None
    free = subprocess.run(["free"], capture_output=True, text=True)
    for line in free.stdout.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            total, used = int(parts[1]), int(parts[2])
            if total:
                mem_pct = round(used / total * 100)
            break

    return {
        "containers_up": running,
        "containers_total": expected,
        "disk_pct": disk_pct,
        "mem_pct": mem_pct,
    }


def _deploy_history(limit=10):
    if not os.path.isdir(DEPLOY_LOG_DIR):
        return []
    files = sorted(
        (f for f in os.listdir(DEPLOY_LOG_DIR) if f.endswith(".jsonl")), reverse=True
    )
    entries = []
    for fname in files:
        path = os.path.join(DEPLOY_LOG_DIR, fname)
        try:
            with open(path) as f:
                lines = [l for l in f if l.strip()]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                return entries
    return entries


def _client_backups(client_id, limit=15):
    """Available local backups for one client, newest first -- each
    paired with the code commit that was live when it was taken (see
    deploy.sh's backup_client()). Bounded to whatever BACKUP_RETENTION_DAYS
    (10, as of 2026-08-30) has actually kept -- this is the real ceiling
    on how far back a snapshot restore can go, not a UI limitation."""
    d = os.path.join(BACKUPS_DIR, client_id)
    if not os.path.isdir(d):
        return []
    out = []
    for ts in sorted(os.listdir(d), reverse=True)[:limit]:
        entry_dir = os.path.join(d, ts)
        commit_path = os.path.join(entry_dir, "live_commit.txt")
        commit = None
        if os.path.isfile(commit_path):
            with open(commit_path) as f:
                commit = f.read().strip()
        out.append({"timestamp": ts, "commit": _short(commit), "full_commit": commit})
    return out


def _derive_status(state):
    staging_matches_pending = bool(
        state["staging_commit"] and state["origin_commit"]
        and state["staging_commit"] == state["origin_commit"]
    )
    has_pending = bool(
        state["live_commit"] and state["origin_commit"]
        and state["live_commit"] != state["origin_commit"]
    )
    return {
        "live_commit": _short(state["live_commit"]),
        "origin_commit": _short(state["origin_commit"]),
        "staging_commit": _short(state["staging_commit"]),
        "pending_commits": state["pending_commits"],
        "has_pending": has_pending,
        "staging_matches_pending": staging_matches_pending,
        "ready_to_deploy": has_pending and staging_matches_pending,
        "last_fetch_at": state["last_fetch_at"],
        "last_fetch_error": state["last_fetch_error"],
    }


@app.route("/api/status")
def api_status():
    clients = real_clients()
    with _state_lock:
        state_copy = {cid: dict(s) for cid, s in _client_state.items()}
    with _job_lock:
        job_copy = {cid: {"kind": j["kind"], "state": j["state"], "log": j["log"][-15:]} for cid, j in _client_jobs.items()}
    with _staging_lock:
        staging_points_at = _staging_points_at

    out = []
    for client_id, cfg in clients.items():
        s = state_copy.get(client_id, {
            "live_commit": None, "origin_commit": None, "staging_commit": None,
            "pending_commits": [], "last_fetch_at": None, "last_fetch_error": None,
        })
        entry = _derive_status(s)
        entry["client_id"] = client_id
        entry["display_name"] = cfg.get("display_name", client_id)
        entry["has_own_repo"] = client_has_own_repo(client_id, clients)
        entry["deploy_peers"] = client_deploy_peers(client_id, clients)
        entry["staging_is_showing_this_client"] = staging_points_at == client_id
        entry["job"] = job_copy.get(client_id, {"kind": None, "state": "idle", "log": []})
        entry["available_backups"] = _client_backups(client_id, limit=1)  # count only, full list on demand
        out.append(entry)
    out.sort(key=lambda e: e["client_id"])

    return jsonify({
        "clients": out,
        "staging_points_at": staging_points_at,
        "health": _health(),
        "deploy_history": _deploy_history(),
    })


def _job_log(client_id, line):
    with _job_lock:
        _client_jobs.setdefault(client_id, {"kind": None, "state": "idle", "log": []})
        _client_jobs[client_id]["log"].append(line)


def _wait_for_staging_healthy(timeout_seconds=60):
    """Polls for a real HTTP 200 on /web/login after a container restart --
    `docker restart` returning success only means the container process
    started, not that Odoo has finished loading modules/building its
    registry. No asset-URL check like deploy.sh's -- staging is
    admin-only and reviewed visually right after, so login=200 is enough
    signal that Odoo is actually accepting requests again."""
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{STAGING_PORT}/web/login"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def _render_staging_for(addons_path):
    """Re-render staging's docker-compose.yml pointed at a specific addons
    checkout -- one shared staging container slot, dynamically re-pointed
    per review. Called on every review so staging always ends up pointed
    at the right worktree regardless of what was reviewed last.

    --local-secrets (2026-08-30): clients.yaml's `staging` entry still has
    `secrets_ref: /erp16-sandbox/staging`, a leftover from before the real
    cutover -- production's own EC2 instance role has no ssm:PutParameter
    on that path (found live: identical AccessDeniedException reproduced
    against both a shared client's and orion_test's staging render, so
    this was silently broken for every review, not new). The `/api/status`
    "staging matches pending" signal is computed purely from git HEAD, so
    this failure was invisible in the UI -- staging's container was never
    actually rebuilt even when the card showed a match.
    secrets.local.yaml on production was pre-seeded with staging's real,
    already-live Postgres/master password (read out of the last-good,
    Aug-15 generated/staging/docker-compose.yml/odoo.conf) before this
    flag was added, so this doesn't generate a mismatched new password
    against the already-initialized staging-db volume."""
    cmd = [
        "python3", os.path.join(BUILD_DIR, "scripts", "render_client.py"), "staging",
        "--container-prefix", "staging",
        "--addons-path", addons_path,
        "--config-path", os.path.join(BUILD_DIR, "generated", "staging", "config"),
        "--out", os.path.join(BUILD_DIR, "generated", "staging"),
        "--local-secrets",
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_review(client_id):
    global _staging_points_at
    with _job_lock:
        job = _client_jobs.setdefault(client_id, {"kind": None, "state": "idle", "log": []})
        if job["state"] == "running":
            return
        job.update({"kind": "review", "state": "running", "log": [f"Moving staging to {client_id}'s latest commit..."]})

    with _state_lock:
        target = _client_state.get(client_id, {}).get("origin_commit")
    if not target:
        _job_log(client_id, "No commit to stage yet -- try again after the next fetch.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    _, staging_wt, _ = client_worktrees(client_id)

    # Own-repo clients (orion_test) have Live_copy_of_<id> and
    # Staging_copy_of_<id> as two fully independent `git clone`s, not
    # linked `git worktree`s sharing one object database like the shared
    # clients' Live/Staging/fetch-checkout trio -- fetching into the live
    # worktree (which _refresh_client_state already does) never populates
    # staging's own objects for these. Found live 2026-08-30: checkout
    # failed with "fatal: reference is not a tree" on a commit that had
    # just been fetched into live moments earlier. Fetching here first is
    # correct and safe for shared clients too (a linked worktree's fetch
    # is a normal, idempotent git operation regardless of which worktree
    # runs it), so this isn't conditional on client type.
    fetch = _git(["fetch", "origin"], cwd=staging_wt)
    if fetch.returncode != 0:
        _job_log(client_id, fetch.stderr.strip() or "git fetch failed in staging worktree.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    checkout = _git(["checkout", target], cwd=staging_wt)
    _job_log(client_id, checkout.stdout.strip() or checkout.stderr.strip() or f"Checked out {target[:7]}.")
    if checkout.returncode != 0:
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    _job_log(client_id, "Re-rendering staging to point at this client's own code...")
    render = _render_staging_for(staging_wt)
    if render.returncode != 0:
        _job_log(client_id, render.stdout.strip() + render.stderr.strip())
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    _job_log(client_id, "Recreating staging-web with the newly-rendered config...")
    # `docker restart` (used here until 2026-08-30) only restarts the
    # container's existing process -- it keeps whatever bind mounts were
    # baked in at the container's last creation, completely ignoring a
    # freshly re-rendered docker-compose.yml. Harmless for shared clients
    # (their addons mount path never changes between reviews, only the
    # code inside it), but found live to silently break orion_test's
    # review: `docker inspect` showed staging-web still bind-mounted from
    # Staging_copy_of_Addons (the old shared folder) even after a
    # successful checkout+render into Staging_copy_of_orion_test -- the
    # container was simply never told its mount path had changed. `docker
    # compose up -d` recreates the container against the current compose
    # file when its config differs, and is a safe no-op when it doesn't.
    compose_file = os.path.join(BUILD_DIR, "generated", "staging", "docker-compose.yml")
    restart = subprocess.run(["docker", "compose", "-f", compose_file, "up", "-d", "web"], capture_output=True, text=True)
    if restart.returncode != 0:
        _job_log(client_id, restart.stderr.strip() or "Restart failed.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    _job_log(client_id, "Waiting for staging-web to actually accept requests...")
    if not _wait_for_staging_healthy():
        _job_log(client_id, "staging-web did not come up healthy within 60s -- check `docker logs staging-web` before trusting the review.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    _job_log(client_id, "Staging updated and confirmed responding. Review the client data through the tunnel, then approve when ready.")
    with _staging_lock:
        _staging_points_at = client_id
    _refresh_client_state(client_id, do_fetch=False)
    with _job_lock:
        _client_jobs[client_id]["state"] = "done"


@app.route("/api/review/<client_id>", methods=["POST"])
def api_review(client_id):
    if client_id not in real_clients():
        return jsonify({"error": f"unknown client '{client_id}'"}), 404
    with _job_lock:
        job = _client_jobs.get(client_id, {})
        if job.get("state") == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_review, args=(client_id,), daemon=True)
    t.start()
    return jsonify({"ok": True})


def _run_deploy(client_id):
    with _job_lock:
        job = _client_jobs.setdefault(client_id, {"kind": None, "state": "idle", "log": []})
        if job["state"] == "running":
            return
        job.update({"kind": "deploy", "state": "running", "log": ["Starting deploy..."]})

    with _state_lock:
        s = _client_state.get(client_id, {})
        target = s.get("origin_commit")
        staging = s.get("staging_commit")
    with _staging_lock:
        staging_ok = _staging_points_at == client_id
    if not target or staging != target or not staging_ok:
        _job_log(client_id, "Staging no longer matches the pending commit -- review again before deploying.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    clients = real_clients()
    peers = client_deploy_peers(client_id, clients)
    if peers:
        _job_log(client_id, f"This client shares its code with: {', '.join(peers)} -- deploying will move all of them together.")

    cmd = [DEPLOY_SH, "deploy-client", client_id, target] if client_has_own_repo(client_id, clients) else [DEPLOY_SH, target]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        _job_log(client_id, line.rstrip("\n"))
    proc.wait()

    # A shared deploy just moved every peer too -- refresh all of them,
    # not just the one that was clicked, so their cards don't show stale
    # state until the next 45s fetch cycle happens to catch up.
    _refresh_client_state(client_id, do_fetch=False, clients=clients)
    for peer in peers:
        _refresh_client_state(peer, do_fetch=False, clients=clients)

    with _job_lock:
        _client_jobs[client_id]["state"] = "done" if proc.returncode == 0 else "error"


@app.route("/api/deploy/<client_id>", methods=["POST"])
def api_deploy(client_id):
    if client_id not in real_clients():
        return jsonify({"error": f"unknown client '{client_id}'"}), 404
    with _state_lock:
        s = _client_state.get(client_id, {})
        ready = bool(
            s.get("staging_commit") and s.get("origin_commit")
            and s["staging_commit"] == s["origin_commit"]
            and s.get("live_commit") != s.get("origin_commit")
        )
    with _staging_lock:
        staging_ok = _staging_points_at == client_id
    if not ready or not staging_ok:
        return jsonify({"error": "Staging isn't reviewed against this client's pending commit yet."}), 409
    with _job_lock:
        job = _client_jobs.get(client_id, {})
        if job.get("state") == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_deploy, args=(client_id,), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    # On-demand counterpart to the 45s background fetch loop -- lets the
    # admin force an immediate check instead of waiting up to
    # FETCH_INTERVAL_SECONDS for a just-sent commit to show up.
    try:
        clients = real_clients()
        for client_id in clients:
            _refresh_client_state(client_id, do_fetch=True, clients=clients)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/backups/<client_id>")
def api_backups(client_id):
    if client_id not in real_clients():
        return jsonify({"error": f"unknown client '{client_id}'"}), 404
    return jsonify({"backups": _client_backups(client_id, limit=15)})


def _run_restore(client_id, timestamp):
    with _job_lock:
        job = _client_jobs.setdefault(client_id, {"kind": None, "state": "idle", "log": []})
        if job["state"] == "running":
            return
        job.update({"kind": "restore", "state": "running", "log": [f"Restoring {client_id} from {timestamp}..."]})

    backup_dir = os.path.join(BACKUPS_DIR, client_id, timestamp)
    commit_path = os.path.join(backup_dir, "live_commit.txt")
    if not os.path.isdir(backup_dir) or not os.path.isfile(commit_path):
        _job_log(client_id, f"No backup found at {timestamp}.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return
    with open(commit_path) as f:
        backup_commit = f.read().strip()

    clients = real_clients()
    scoped = client_has_own_repo(client_id, clients)

    _job_log(client_id, "Restoring data (db + filestore)...")
    proc = subprocess.run([DEPLOY_SH, "rollback", client_id, backup_dir], capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        _job_log(client_id, line)
    if proc.returncode != 0:
        _job_log(client_id, "Data restore failed -- stopping before touching code.")
        with _job_lock:
            _client_jobs[client_id]["state"] = "error"
        return

    if scoped:
        _job_log(client_id, f"Reverting code to {backup_commit[:7]} (this client's own repo, no other client affected)...")
        proc = subprocess.run([DEPLOY_SH, "promote-client", client_id, backup_commit], capture_output=True, text=True)
        for line in (proc.stdout + proc.stderr).splitlines():
            _job_log(client_id, line)
        if proc.returncode != 0:
            _job_log(client_id, "Code checkout failed -- data has been restored but code was not reverted to match. Needs manual attention.")
            with _job_lock:
                _client_jobs[client_id]["state"] = "error"
            return
    else:
        _job_log(client_id, "Code was NOT reverted -- this client still shares its repo with others, so its code can't be scoped-reverted without moving them too. Only data was restored.")

    _job_log(client_id, "Restarting and verifying...")
    proc = subprocess.run([DEPLOY_SH, "healthcheck", client_id], capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        _job_log(client_id, line)
    ok = proc.returncode == 0

    _refresh_client_state(client_id, do_fetch=False, clients=clients)
    with _job_lock:
        _client_jobs[client_id]["state"] = "done" if ok else "error"


@app.route("/api/restore/<client_id>", methods=["POST"])
def api_restore(client_id):
    if client_id not in real_clients():
        return jsonify({"error": f"unknown client '{client_id}'"}), 404
    timestamp = (request.json or {}).get("timestamp", "").strip()
    if not timestamp:
        return jsonify({"error": "no backup selected"}), 400
    with _job_lock:
        job = _client_jobs.get(client_id, {})
        if job.get("state") == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_restore, args=(client_id, timestamp), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/recent-changes/<client_id>")
def api_recent_changes(client_id):
    # Same query Git Console's own /api/recent-sends already uses --
    # last N commits on this client's own origin/main, regardless of
    # deploy status. Deliberately separate from pending_commits (which
    # only ever shows what's *not* yet deployed).
    if client_id not in real_clients():
        return jsonify({"error": f"unknown client '{client_id}'"}), 404
    _, _, fetch_checkout = client_worktrees(client_id)
    log = _git(["log", "-20", "--pretty=format:%h|%an|%ar|%s", "origin/main"], cwd=fetch_checkout)
    changes = []
    for line in log.stdout.strip().splitlines():
        if "|" not in line:
            continue
        h, author, when, subject = line.split("|", 3)
        changes.append({"hash": h, "author": author, "when": when, "subject": subject})
    return jsonify({"changes": changes})


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        return Response(f.read(), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_CONSOLE_PORT", 5252))
    _clients = real_clients()
    for _cid in _clients:
        _refresh_client_state(_cid, do_fetch=True, clients=_clients)
    threading.Thread(target=_fetch_loop, daemon=True).start()
    print(f"ERP16 Admin Console: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
