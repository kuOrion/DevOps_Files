#!/usr/bin/env python3
"""
ERP16 developer console -- a thin local web UI wrapping dev-start.sh and
git, nothing more. Every action here shells out to the same scripts/
commands a developer would type by hand; this never reimplements
container or git logic itself, matching the same "one source of truth"
lesson the sanitization pipeline's registry consolidation already taught.

Run: python3 build/scripts/dev_console/app.py
Then open http://127.0.0.1:5151

Auto-commit-on-stop policy (deliberately narrow, discussed before
building): stopping a client always commits any uncommitted changes in
the addons repo LOCALLY first -- safe, always reversible (git reset/
revert). It never pushes. Push stays a separate, deliberate action for
the developer, every time, no exceptions.
"""
import json
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, request, Response

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_DIR = os.path.dirname(BUILD_DIR)
ADDONS_DIR = os.path.join(os.path.dirname(REPO_DIR), "erp16-custom-addons")
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
DEV_START = os.path.join(BUILD_DIR, "scripts", "dev-start.sh")

app = Flask(__name__)

# In-memory job state -- one dev's own laptop, one console, no need for
# a real job queue/database. {client_id: {"state": ..., "log": [...]}}
_jobs = {}
_jobs_lock = threading.Lock()


def load_clients():
    import yaml
    with open(CLIENTS_YAML) as f:
        return yaml.safe_load(f)["clients"]


def docker_ps():
    """{container_name: status_line} for everything currently running."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True,
    )
    out = {}
    for line in result.stdout.strip().splitlines():
        if "\t" in line:
            name, status = line.split("\t", 1)
            out[name] = status
    return out


def client_status(client_id, cfg, running_containers):
    web_name = f"{client_id}-web_{client_id}-1"
    running = web_name in running_containers
    return {
        "client_id": client_id,
        "display_name": cfg.get("display_name", client_id),
        "running": running,
        "status_line": running_containers.get(web_name, ""),
        "url": f"http://127.0.0.1:{cfg['http_port']}" if running else None,
    }


@app.route("/api/clients")
def api_clients():
    clients = load_clients()
    running = docker_ps()
    out = [client_status(cid, cfg, running) for cid, cfg in clients.items()]
    with _jobs_lock:
        jobs_copy = {k: v["state"] for k, v in _jobs.items()}
    return jsonify({"clients": out, "jobs": jobs_copy})


def _run_job(client_id, args):
    with _jobs_lock:
        _jobs[client_id] = {"state": "running", "log": []}
    cmd = [DEV_START, client_id] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        with _jobs_lock:
            _jobs[client_id]["log"].append(line.rstrip("\n"))
    proc.wait()
    with _jobs_lock:
        _jobs[client_id]["state"] = "done" if proc.returncode == 0 else "error"


@app.route("/api/start/<client_id>", methods=["POST"])
def api_start(client_id):
    with _jobs_lock:
        if _jobs.get(client_id, {}).get("state") == "running":
            return jsonify({"error": "already starting"}), 409
    t = threading.Thread(target=_run_job, args=(client_id, []), daemon=True)
    t.start()
    return jsonify({"ok": True})


def _git(args, cwd=ADDONS_DIR):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
    )


def _auto_commit_if_dirty(reason):
    """Local commit only, never push -- see module docstring."""
    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        return None
    _git(["add", "-A"])
    msg = f"WIP: auto-commit ({reason})"
    commit = _git(["commit", "-m", msg])
    return msg if commit.returncode == 0 else None


@app.route("/api/stop/<client_id>", methods=["POST"])
def api_stop(client_id):
    committed = _auto_commit_if_dirty(f"dev console stop, {client_id}")
    t = threading.Thread(target=_run_job, args=(client_id, ["--down"]), daemon=True)
    t.start()
    return jsonify({"ok": True, "committed": committed})


@app.route("/api/job/<client_id>")
def api_job(client_id):
    with _jobs_lock:
        job = _jobs.get(client_id, {"state": "idle", "log": []})
        return jsonify(job)


@app.route("/api/git/status")
def api_git_status():
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    ahead_behind = _git(["rev-list", "--left-right", "--count", "origin/main...HEAD"])
    ahead = 0
    if ahead_behind.returncode == 0 and "\t" in ahead_behind.stdout:
        ahead = int(ahead_behind.stdout.strip().split("\t")[1])
    status = _git(["status", "--porcelain"])
    files = []
    # splitlines() on the raw stdout, NOT stdout.strip() first -- porcelain
    # format's leading space (e.g. " M path" for an unstaged modification)
    # is meaningful per-line; stripping the whole string first eats only
    # the very first line's leading space, silently corrupting just that
    # one file's parsed path. Found via a real bug: the first file in the
    # list always lost its first character.
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2].strip(), line[3:]
        numstat = _git(["diff", "--numstat", "HEAD", "--", path])
        added, removed = "0", "0"
        if numstat.stdout.strip():
            parts = numstat.stdout.strip().split("\t")
            if len(parts) >= 2:
                added, removed = parts[0], parts[1]
        files.append({"code": code or "?", "path": path, "added": added, "removed": removed})
    return jsonify({"branch": branch, "ahead": ahead, "files": files})


@app.route("/api/git/log")
def api_git_log():
    log = _git(["log", "-4", "--pretty=format:%h|%ar|%s"])
    commits = []
    for line in log.stdout.strip().splitlines():
        if "|" in line:
            h, when, subject = line.split("|", 2)
            commits.append({"hash": h, "when": when, "subject": subject})
    return jsonify({"commits": commits})


@app.route("/api/git/commit", methods=["POST"])
def api_git_commit():
    message = request.json.get("message", "").strip()
    if not message:
        return jsonify({"error": "commit message required"}), 400
    _git(["add", "-A"])
    result = _git(["commit", "-m", message])
    if result.returncode != 0:
        return jsonify({"error": result.stdout + result.stderr}), 400
    return jsonify({"ok": True})


@app.route("/api/open-addons", methods=["POST"])
def api_open_addons():
    subprocess.Popen(["xdg-open", ADDONS_DIR])
    return jsonify({"ok": True, "path": ADDONS_DIR})


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        html = f.read()
    return Response(html.replace("__ADDONS_DIR__", ADDONS_DIR), mimetype="text/html")


if __name__ == "__main__":
    print(f"ERP16 dev console: http://127.0.0.1:5151")
    print(f"Addons checkout: {ADDONS_DIR}")
    app.run(host="127.0.0.1", port=5151, debug=False)
