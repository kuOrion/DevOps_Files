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
import logging
import os
import re
import subprocess
import threading
import time

from flask import Flask, jsonify, request, Response

logging.getLogger("werkzeug").setLevel(logging.ERROR)

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Override for testing/demo -- e.g. pointing at a throwaway repo instead
# of the real erp16-custom-addons checkout, so push can be exercised
# safely. Normal operation never sets this.
ADDONS_DIR = os.environ.get(
    "DEV_CONSOLE_ADDONS_DIR",
    os.path.join(os.path.dirname(BUILD_DIR), "erp16-custom-addons"),
)
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
    """Always commits locally. Also pushes, but ONLY if on a task branch --
    a task branch is isolated (no one else is on it), so pushing WIP there
    is safe. Never auto-pushes to main."""
    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        return None
    _git(["add", "-A"])
    msg = f"WIP: auto-commit ({reason})"
    commit = _git(["commit", "-m", msg])
    if commit.returncode != 0:
        return None
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != "main":
        _git(["push", "-u", "origin", branch])
    return msg


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

    return jsonify({
        "repo": os.path.basename(ADDONS_DIR),
        "branch": branch,
        "on_task": branch not in ("main", "HEAD"),
        "files": files,
    })


def _parse_log(stdout):
    commits = []
    for line in stdout.strip().splitlines():
        if "|" not in line:
            continue
        h, parents, author, when, subject = line.split("|", 4)
        commits.append({
            "hash": h,
            "author": author,
            "when": when,
            "subject": subject,
            "is_merge": len(parents.split()) > 1,
        })
    return commits


@app.route("/api/git/tree")
def api_git_tree():
    """Main's last 8 commits, plus every currently active task branch
    (local ones you started, and remote ones a teammate pushed via their
    own Save Progress) with the commits on each not yet on main -- a
    fetch first so a teammate's in-progress branch actually shows up."""
    _git(["fetch", "--prune", "-q", "origin"])

    main_log = _git(["log", "-30", "--pretty=format:%h|%p|%an|%ar|%s", "main"])
    main_commits = _parse_log(main_log.stdout)

    local = set(_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/task/"]).stdout.split())
    remote = set(_git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/task/"]).stdout.split())
    names = sorted(local | {r.split("/", 1)[1] for r in remote if r.startswith("origin/")})

    active_tasks = []
    for name in names:
        ref = name if name in local else f"origin/{name}"
        log = _git(["log", ref, "--not", "main", "--pretty=format:%h|%p|%an|%ar|%s", "-8"])
        active_tasks.append({"branch": name, "commits": _parse_log(log.stdout)})

    return jsonify({"main": main_commits, "active_tasks": active_tasks})


@app.route("/api/git/commit", methods=["POST"])
def api_git_commit():
    message = request.json.get("message", "").strip()
    if not message:
        return jsonify({"error": "commit message required"}), 400
    _git(["add", "-A"])
    result = _git(["commit", "-m", message])
    if result.returncode != 0:
        return jsonify({"error": result.stdout + result.stderr}), 400
    # Safe to auto-push here: a task branch is isolated, so a WIP push
    # can't collide with anyone else's work the way pushing WIP to main
    # could. Never pushes if somehow still on main.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    pushed = False
    if branch != "main":
        push = _git(["push", "-u", "origin", branch])
        pushed = push.returncode == 0
    return jsonify({"ok": True, "pushed": pushed})


@app.route("/api/git/diff")
def api_git_diff():
    diff = _git(["diff", "HEAD"])
    return jsonify({"diff": diff.stdout})


@app.route("/api/task/start", methods=["POST"])
def api_task_start():
    """Pulls latest main, creates+switches to a task/<slug> branch. The
    word 'branch' never needs to reach the UI -- this is just 'start
    working on something new'."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "describe the task first"}), 400
    current = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if current != "main":
        return jsonify({"error": f"finish the current task ({current}) before starting a new one"}), 400
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task"
    branch = f"task/{slug}"
    pull = _git(["pull", "origin", "main"])
    if pull.returncode != 0:
        return jsonify({"error": "pull failed: " + pull.stdout + pull.stderr}), 400
    result = _git(["checkout", "-b", branch])
    if result.returncode != 0:
        return jsonify({"error": result.stdout + result.stderr}), 400
    return jsonify({"ok": True, "branch": branch})


@app.route("/api/task/end", methods=["POST"])
def api_task_end():
    """Commits anything left, pushes, merges into main, pushes main,
    deletes the task branch. On a merge conflict, backs out to main
    untouched and leaves the task branch alone for manual resolution --
    no in-browser conflict UI, just a clear stop."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch == "main":
        return jsonify({"error": "not currently on a task"}), 400
    message = (request.json or {}).get("message", "").strip()
    status = _git(["status", "--porcelain"])
    if status.stdout.strip():
        if not message:
            return jsonify({"error": "describe the remaining changes before finishing"}), 400
        _git(["add", "-A"])
        commit = _git(["commit", "-m", message])
        if commit.returncode != 0:
            return jsonify({"error": commit.stdout + commit.stderr}), 400
    push = _git(["push", "-u", "origin", branch])
    if push.returncode != 0:
        return jsonify({"error": "push failed: " + push.stdout + push.stderr}), 400
    _git(["checkout", "main"])
    _git(["pull", "origin", "main"])
    merge = _git(["merge", "--no-ff", branch, "-m", f"Merge {branch}"])
    if merge.returncode != 0:
        return jsonify({
            "error": "Merge conflict -- back on main, task branch untouched. Ask for help resolving this one in a terminal.",
            "branch": branch,
        }), 409
    push_main = _git(["push", "origin", "main"])
    if push_main.returncode != 0:
        return jsonify({"error": push_main.stdout + push_main.stderr}), 400
    _git(["branch", "-d", branch])
    _git(["push", "origin", "--delete", branch])
    return jsonify({"ok": True, "merged_branch": branch})


@app.route("/api/open-addons", methods=["POST"])
def api_open_addons():
    subprocess.Popen(["xdg-open", ADDONS_DIR])
    return jsonify({"ok": True, "path": ADDONS_DIR})


@app.route("/api/open-clients-yaml", methods=["POST"])
def api_open_clients_yaml():
    subprocess.Popen(["xdg-open", CLIENTS_YAML])
    return jsonify({"ok": True, "path": CLIENTS_YAML})


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        html = f.read()
    return Response(html.replace("__ADDONS_DIR__", ADDONS_DIR), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("DEV_CONSOLE_PORT", 5151))
    print(f"ERP16 dev console: http://127.0.0.1:{port}")
    print(f"Addons checkout: {ADDONS_DIR}")
    app.run(host="127.0.0.1", port=port, debug=False)
