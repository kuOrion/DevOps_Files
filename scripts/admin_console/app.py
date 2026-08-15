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

git fetch runs on its own timer (FETCH_INTERVAL_SECONDS below), not a
button -- deliberately reversing the earlier "on-demand only" decision
for fetch specifically, since fetch is read-only and touches nothing
live; staging and deploy stay exactly as admin-triggered as before.

Every action here shells out to the same deploy.sh a human would type
by hand -- this never reimplements deploy/rollback logic itself.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, request, Response

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
DEPLOY_SH = os.path.join(BUILD_DIR, "scripts", "deploy.sh")

FETCH_CHECKOUT = os.path.expanduser("~/erp16-custom-addons")
STAGING_WORKTREE = os.path.expanduser("~/Staging_copy_of_Addons")
STAGING_PORT = 8209  # clients.yaml's staging entry
LIVE_WORKTREE = os.path.expanduser("~/Live_copy_of_Addons")
DEPLOY_LOG_DIR = "/opt/erp16/logs/deploy"

FETCH_INTERVAL_SECONDS = 45
PENDING_COMMIT_LIMIT = 30

app = Flask(__name__)

_state_lock = threading.Lock()
_state = {
    "live_commit": None,
    "origin_commit": None,
    "staging_commit": None,
    "pending_commits": [],
    "last_fetch_at": None,
    "last_fetch_error": None,
}

_job_lock = threading.Lock()
_job = {"kind": None, "state": "idle", "log": []}


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


def _refresh_state(do_fetch):
    with _state_lock:
        if do_fetch:
            fetch = _git(["fetch", "origin"], cwd=FETCH_CHECKOUT)
            _state["last_fetch_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _state["last_fetch_error"] = None if fetch.returncode == 0 else (fetch.stderr.strip() or "fetch failed")

        live = _rev_parse(LIVE_WORKTREE)
        origin = _rev_parse(FETCH_CHECKOUT, "origin/main")
        staging = _rev_parse(STAGING_WORKTREE)

        _state["live_commit"] = live
        _state["origin_commit"] = origin
        _state["staging_commit"] = staging
        _state["pending_commits"] = _pending_commits(FETCH_CHECKOUT, live, origin)


def _fetch_loop():
    while True:
        try:
            _refresh_state(do_fetch=True)
        except Exception:
            pass
        time.sleep(FETCH_INTERVAL_SECONDS)


def _health():
    try:
        import yaml
        with open(CLIENTS_YAML) as f:
            clients = yaml.safe_load(f)["clients"]
        expected = sum(2 for c in clients.values() if c.get("hosting") == "cloud")
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


@app.route("/api/status")
def api_status():
    with _state_lock:
        state = dict(_state)
    staging_matches_pending = bool(
        state["staging_commit"] and state["origin_commit"]
        and state["staging_commit"] == state["origin_commit"]
    )
    has_pending = bool(
        state["live_commit"] and state["origin_commit"]
        and state["live_commit"] != state["origin_commit"]
    )
    with _job_lock:
        job = {"kind": _job["kind"], "state": _job["state"], "log": _job["log"][-15:]}
    return jsonify({
        "live_commit": _short(state["live_commit"]),
        "origin_commit": _short(state["origin_commit"]),
        "staging_commit": _short(state["staging_commit"]),
        "pending_commits": state["pending_commits"],
        "has_pending": has_pending,
        "staging_matches_pending": staging_matches_pending,
        "ready_to_deploy": has_pending and staging_matches_pending,
        "last_fetch_at": state["last_fetch_at"],
        "last_fetch_error": state["last_fetch_error"],
        "health": _health(),
        "deploy_history": _deploy_history(),
        "job": job,
    })


def _job_log(line):
    with _job_lock:
        _job["log"].append(line)


def _wait_for_staging_healthy(timeout_seconds=60):
    """Polls for a real HTTP 200 on /web/login after a container restart --
    `docker restart` returning success only means the container process
    started, not that Odoo has finished loading modules/building its
    registry. Found live, 2026-08-06: a restart briefly reports success
    while Odoo is still mid-boot, and a request landing in that window
    hits HAProxy's own 502 (backend not accepting connections yet), not
    a clean "not ready" response -- same class of gap deploy.sh's own
    thin liveness check exists to close for live deploys, just missing
    here until now. No asset-URL check like deploy.sh's -- staging is
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


def _run_review():
    with _job_lock:
        if _job["state"] == "running":
            return
        _job.update({"kind": "review", "state": "running", "log": ["Moving staging to the latest commit..."]})
    with _state_lock:
        target = _state["origin_commit"]
    if not target:
        _job_log("No commit to stage yet -- try again after the next fetch.")
        with _job_lock:
            _job["state"] = "error"
        return

    checkout = _git(["checkout", target], cwd=STAGING_WORKTREE)
    _job_log(checkout.stdout.strip() or checkout.stderr.strip() or f"Checked out {target[:7]}.")
    if checkout.returncode != 0:
        with _job_lock:
            _job["state"] = "error"
        return

    _job_log("Restarting staging-web...")
    restart = subprocess.run(["docker", "restart", "staging-web"], capture_output=True, text=True)
    if restart.returncode != 0:
        _job_log(restart.stderr.strip() or "Restart failed.")
        with _job_lock:
            _job["state"] = "error"
        return

    _job_log("Waiting for staging-web to actually accept requests...")
    if not _wait_for_staging_healthy():
        _job_log("staging-web did not come up healthy within 60s -- check `docker logs staging-web` before trusting the review.")
        with _job_lock:
            _job["state"] = "error"
        return

    _job_log("Staging updated and confirmed responding. Review the client data through the tunnel, then approve when ready.")
    _refresh_state(do_fetch=False)
    with _job_lock:
        _job["state"] = "done"


@app.route("/api/review", methods=["POST"])
def api_review():
    with _job_lock:
        if _job["state"] == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_review, daemon=True)
    t.start()
    return jsonify({"ok": True})


def _run_deploy():
    with _job_lock:
        if _job["state"] == "running":
            return
        _job.update({"kind": "deploy", "state": "running", "log": ["Starting deploy..."]})

    with _state_lock:
        target = _state["origin_commit"]
        staging = _state["staging_commit"]
    if not target or staging != target:
        _job_log("Staging no longer matches the pending commit -- review again before deploying.")
        with _job_lock:
            _job["state"] = "error"
        return

    proc = subprocess.Popen(
        [DEPLOY_SH, target], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        _job_log(line.rstrip("\n"))
    proc.wait()
    _refresh_state(do_fetch=False)
    with _job_lock:
        _job["state"] = "done" if proc.returncode == 0 else "error"


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    with _state_lock:
        ready = bool(
            _state["staging_commit"] and _state["origin_commit"]
            and _state["staging_commit"] == _state["origin_commit"]
            and _state["live_commit"] != _state["origin_commit"]
        )
    if not ready:
        return jsonify({"error": "Staging isn't reviewed against the pending commit yet."}), 409
    with _job_lock:
        if _job["state"] == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_deploy, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    # On-demand counterpart to the 45s background fetch loop -- lets the
    # admin force an immediate check instead of waiting up to
    # FETCH_INTERVAL_SECONDS for a just-sent commit to show up.
    try:
        _refresh_state(do_fetch=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/recent-changes")
def api_recent_changes():
    # Same query Git Console's own /api/recent-sends already uses --
    # last N commits on main, regardless of deploy status. Deliberately
    # separate from pending_commits (which only ever shows what's *not*
    # yet deployed): this stays populated even once everything's live,
    # so there's always something to glance at, not just when idle.
    log = _git(["log", "-20", "--pretty=format:%h|%an|%ar|%s", "origin/main"], cwd=FETCH_CHECKOUT)
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
    _refresh_state(do_fetch=True)
    threading.Thread(target=_fetch_loop, daemon=True).start()
    print(f"ERP16 Admin Console: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
