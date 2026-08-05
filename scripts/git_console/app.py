#!/usr/bin/env python3
"""
ERP16 Git Console -- a thin local web UI wrapping dev-start.sh and git,
nothing more. Every action here shells out to the same scripts/commands a
developer would type by hand; this never reimplements container or git
logic itself.

Rebuilt 2026-08-05 around how devs at Orion actually work (learned
directly from them, not assumed): native PyCharm editing, no container
day-to-day, a container run only as a final check, and -- critically --
nobody has ever used git. So this console never surfaces a single git
noun (branch, commit, merge, conflict). Three actions only:
  - Get Latest    -- fresh code + fresh sanitized data for one client
  - Start Container -- final check before sending, dev_mode on so a saved
                        edit just works without a manual restart
  - Send for Review -- commit (mandatory description) + push straight to
                        main. No task branches -- main IS the pending-
                        review queue admin's staging picks up from.

Conflict safety net (the one place real git mechanics matter and nobody
here can resolve them by hand): any pull that can't merge cleanly NEVER
asks the dev to fix it. Whatever's on their local main -- including a
generic auto-commit of dirty work -- gets preserved on a timestamped
wip/backup-<ts> branch, pushed, and local main is hard-reset to match
origin/main so they always have a clean base to keep working from. Rare
in practice (small team, usually different files), but the failure mode
if it's ever hit unhandled is silent data loss, so it's handled for real.

Run: python3 scripts/git_console/app.py
Then open http://127.0.0.1:5151
"""
import difflib
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, request, Response

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDONS_DIR = os.environ.get(
    "DEV_CONSOLE_ADDONS_DIR",
    os.path.join(os.path.dirname(BUILD_DIR), "erp16-custom-addons"),
)
CLIENTS_YAML = os.path.join(BUILD_DIR, "clients.yaml")
DEV_START = os.path.join(BUILD_DIR, "scripts", "dev-start.sh")

app = Flask(__name__)

# In-memory job state -- one dev's own laptop, one console, no need for a
# real job queue/database. {client_id: {"state": ..., "log": [...]}}
_jobs = {}
_jobs_lock = threading.Lock()


def load_clients():
    import yaml
    with open(CLIENTS_YAML) as f:
        return yaml.safe_load(f)["clients"]


def docker_ps():
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
    web_name = f"{client_id}-web"
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


def _job_log(client_id, line):
    with _jobs_lock:
        _jobs[client_id]["log"].append(line)


def _run_job(client_id, args):
    with _jobs_lock:
        _jobs[client_id] = {"state": "running", "log": []}
    cmd = [DEV_START, client_id] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        _job_log(client_id, line.rstrip("\n"))
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
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _auto_commit_if_dirty(reason):
    """Commits any uncommitted changes locally. NEVER pushes -- this is
    purely a safety net so work is never at risk of being lost to a
    mistake (a bad checkout, a pull, closing the laptop for the day). The
    only thing that ever reaches anyone else is Send for Review."""
    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        return None
    _git(["add", "-A"])
    msg = f"WIP: auto-commit ({reason})"
    commit = _git(["commit", "-m", msg])
    return msg if commit.returncode == 0 else None


def _pull_with_conflict_safety():
    """Pull latest main. If it can't merge cleanly, never ask the dev to
    resolve it -- nobody here can. Whatever's on local main right now
    (including any commit just made) is preserved on a timestamped backup
    branch, pushed, and local main is hard-reset to match origin/main so
    there's always a clean base to keep working from. Returns
    (ok: bool, message: str|None)."""
    pull = _git(["pull", "--no-rebase", "origin", "main"])
    if pull.returncode == 0:
        return True, None
    _git(["merge", "--abort"])
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_branch = f"wip/backup-{ts}"
    _git(["branch", backup_branch])
    _git(["push", "-u", "origin", backup_branch])
    _git(["reset", "--hard", "origin/main"])
    return False, (
        f"Someone else's changes conflicted with yours. Nothing was lost -- "
        f"your work is safely saved on '{backup_branch}'. Ask your admin for help merging it in."
    )


def _safe_pull_latest(reason):
    """Used by Get Latest: commit any dirty work locally first (generic
    message, never pushed), then pull with the same conflict safety net
    Send for Review uses."""
    _auto_commit_if_dirty(reason)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != "main":
        _git(["checkout", "main"])
    return _pull_with_conflict_safety()


@app.route("/api/stop/<client_id>", methods=["POST"])
def api_stop(client_id):
    committed = _auto_commit_if_dirty(f"stopped {client_id}")
    t = threading.Thread(target=_run_job, args=(client_id, ["--down"]), daemon=True)
    t.start()
    return jsonify({"ok": True, "committed": committed})


@app.route("/api/save-changes", methods=["POST"])
def api_save_changes():
    """A lightweight checkpoint -- local commit only, generic message, same
    safety net Stop already uses. No description needed; this isn't
    Send for Review, it's just 'don't lose this.'"""
    committed = _auto_commit_if_dirty("manual save")
    if not committed:
        return jsonify({"error": "Nothing to save."}), 400
    return jsonify({"ok": True})


@app.route("/api/job/<client_id>")
def api_job(client_id):
    with _jobs_lock:
        job = _jobs.get(client_id, {"state": "idle", "log": []})
        return jsonify(job)


def _run_get_latest(client_id):
    with _jobs_lock:
        _jobs[client_id] = {"state": "running", "log": ["Pulling latest code..."]}
    ok, msg = _safe_pull_latest(f"get latest before working on {client_id}")
    _job_log(client_id, msg or "Code up to date.")
    if not ok:
        with _jobs_lock:
            _jobs[client_id]["state"] = "error"
        return

    _job_log(client_id, "Refreshing sanitized data...")
    cmd = [DEV_START, client_id, "--refresh"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        _job_log(client_id, line.rstrip("\n"))
    proc.wait()
    with _jobs_lock:
        _jobs[client_id]["state"] = "done" if proc.returncode == 0 else "error"


@app.route("/api/get-latest/<client_id>", methods=["POST"])
def api_get_latest(client_id):
    with _jobs_lock:
        if _jobs.get(client_id, {}).get("state") == "running":
            return jsonify({"error": "already working"}), 409
    t = threading.Thread(target=_run_get_latest, args=(client_id,), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/git/status")
def api_git_status():
    status = _git(["status", "--porcelain"])
    files = []
    # splitlines() on the raw stdout, NOT stdout.strip() first -- porcelain
    # format's leading space (e.g. " M path" for an unstaged modification)
    # is meaningful per-line; stripping the whole string first eats only
    # the very first line's leading space, silently corrupting just that
    # one file's parsed path.
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

    return jsonify({"repo": os.path.basename(ADDONS_DIR), "files": files})


CONTEXT_LINES = 3


def _file_lines(path, ref=None):
    """File content as a list of lines, either from the working tree
    (ref=None) or a git ref (e.g. 'HEAD'). Empty list if the file doesn't
    exist there -- covers new/deleted files without a special case."""
    if ref:
        result = _git(["show", f"{ref}:{path}"])
        return result.stdout.splitlines() if result.returncode == 0 else []
    full = os.path.join(ADDONS_DIR, path)
    if not os.path.isfile(full):
        return []
    with open(full, errors="replace") as f:
        return f.read().splitlines()


def _word_spans(old_line, new_line):
    """Character-level diff of one line pair -- returns (old_spans,
    new_spans), each a list of [text, changed] pairs, so the frontend can
    highlight just the substring that actually changed (e.g. a version
    bump '16.0.1.2' -> '16.0.1.3') instead of marking the whole line."""
    sm = difflib.SequenceMatcher(None, old_line, new_line)
    old_spans, new_spans = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            old_spans.append([old_line[i1:i2], False])
            new_spans.append([new_line[j1:j2], False])
        else:
            if i1 != i2:
                old_spans.append([old_line[i1:i2], True])
            if j1 != j2:
                new_spans.append([new_line[j1:j2], True])
    return old_spans, new_spans


def _file_diff_hunks(path, status_code):
    is_new = "A" in status_code or "?" in status_code
    is_deleted = "D" in status_code
    old_lines = [] if is_new else _file_lines(path, "HEAD")
    new_lines = [] if is_deleted else _file_lines(path)

    hunks = []
    ops = difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()
    for idx, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            lines = old_lines[i1:i2]
            at_start, at_end = idx == 0, idx == len(ops) - 1
            head = [] if at_start else lines[:CONTEXT_LINES]
            tail = [] if at_end else lines[-CONTEXT_LINES:]
            hidden = len(lines) - len(head) - len(tail)
            for l in head:
                hunks.append({"type": "context", "text": l})
            if hidden > 0:
                hunks.append({"type": "skip", "count": hidden})
            for l in (tail if hidden > 0 else lines[len(head):]):
                hunks.append({"type": "context", "text": l})
        elif op == "delete":
            for l in old_lines[i1:i2]:
                hunks.append({"type": "del", "spans": [[l, False]]})
        elif op == "insert":
            for l in new_lines[j1:j2]:
                hunks.append({"type": "add", "spans": [[l, False]]})
        elif op == "replace":
            old_block, new_block = old_lines[i1:i2], new_lines[j1:j2]
            # Word-level highlight only makes sense line-for-line -- an
            # asymmetric block (e.g. 1 line replaced by 5) falls back to
            # plain whole-line del/add, same as a normal diff would show.
            if len(old_block) == len(new_block) and len(old_block) <= 5:
                for ol, nl in zip(old_block, new_block):
                    old_spans, new_spans = _word_spans(ol, nl)
                    hunks.append({"type": "del", "spans": old_spans})
                    hunks.append({"type": "add", "spans": new_spans})
            else:
                for l in old_block:
                    hunks.append({"type": "del", "spans": [[l, False]]})
                for l in new_block:
                    hunks.append({"type": "add", "spans": [[l, False]]})
    return hunks


@app.route("/api/git/diff")
def api_git_diff():
    status = _git(["status", "--porcelain"])
    files = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2].strip(), line[3:]
        files.append({"path": path, "hunks": _file_diff_hunks(path, code)})
    return jsonify({"files": files})


@app.route("/api/send-for-review", methods=["POST"])
def api_send_for_review():
    message = (request.json or {}).get("message", "").strip()
    if not message:
        return jsonify({"error": "Describe what changed before sending -- a few words is enough."}), 400

    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        return jsonify({"error": "Nothing to send."}), 400

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != "main":
        _git(["checkout", "main"])

    _git(["add", "-A"])
    commit = _git(["commit", "-m", message])
    if commit.returncode != 0:
        return jsonify({"error": commit.stdout + commit.stderr}), 400

    ok, conflict_msg = _pull_with_conflict_safety()
    if not ok:
        return jsonify({"error": conflict_msg}), 409

    push = _git(["push", "origin", "main"])
    if push.returncode != 0:
        # Rare race: someone else pushed between our pull and our push.
        # One retry covers it without bothering the dev.
        ok2, conflict_msg2 = _pull_with_conflict_safety()
        if not ok2:
            return jsonify({"error": conflict_msg2}), 409
        push = _git(["push", "origin", "main"])
        if push.returncode != 0:
            return jsonify({"error": "Send failed -- please try again: " + push.stdout + push.stderr}), 500

    return jsonify({"ok": True})


@app.route("/api/recent-sends")
def api_recent_sends():
    _git(["fetch", "-q", "origin", "main"])
    log = _git(["log", "-20", "--pretty=format:%h|%an|%ar|%s", "origin/main"])
    sends = []
    for line in log.stdout.strip().splitlines():
        if "|" not in line:
            continue
        h, author, when, subject = line.split("|", 3)
        sends.append({"hash": h, "author": author, "when": when, "subject": subject})
    return jsonify({"sends": sends})


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        html = f.read()
    return Response(html.replace("__ADDONS_DIR__", ADDONS_DIR), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("DEV_CONSOLE_PORT", 5151))
    print(f"ERP16 Git Console: http://127.0.0.1:{port}")
    print(f"Addons checkout: {ADDONS_DIR}")
    app.run(host="127.0.0.1", port=port, debug=False)
