#!/usr/bin/env python3
"""Single long-running collector for the two routine logging/audit sources
(docs/ROADMAP.md, 2026-08-06 logging design): Docker container lifecycle
events and Odoo application log lines. Runs as one systemd service with two
kinds of worker thread feeding a shared, lock-protected JSONL writer --
deploy history (scripts/deploy.sh) already established the format this
follows: dense JSONL, one file per day, UTC ISO8601 timestamps throughout.

Docker thread: consumes `docker events` as a live stream (not polling
`docker ps`), so a crash/OOM/restart is captured the instant it happens.

Odoo threads: one per live/staging/sanitize *-web container, each tailing
`docker logs -f` and parsing Odoo's own log line format. Only WARNING and
above are written -- INFO-level Odoo noise (every request, every cron
tick) has no diagnostic value at the volume it would generate. A periodic
rescan picks up containers that don't exist yet at collector startup
without needing a restart.

Both sources are "routine" in the audit/routine split -- aged out after
RETENTION_DAYS, unlike deploy history which is audit and kept forever.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

LOG_DIR = "/opt/erp16/logs"
RETENTION_DAYS = 90
RESCAN_INTERVAL_SECONDS = 15

_write_lock = threading.Lock()
_stop = threading.Event()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_entry(source, entry):
    entry.setdefault("ts", _now_iso())
    entry["source"] = source
    day = entry["ts"][:10]
    path = os.path.join(LOG_DIR, source, f"{day}.jsonl")
    line = json.dumps(entry, separators=(",", ":"))
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")


def resolve_client(container_name):
    """Map a container name to (client_id, role) per render_client.py's
    container_prefix convention (live-<client>-web/db, staging-web/db,
    sanitize-web/db). Returns (None, None) for anything that doesn't match
    -- e.g. leftover containers unrelated to this stack -- so callers can
    silently drop what they can't attribute to a client."""
    if container_name.endswith("-web"):
        role, base = "web", container_name[: -len("-web")]
    elif container_name.endswith("-db"):
        role, base = "db", container_name[: -len("-db")]
    else:
        return None, None
    if base.startswith("live-"):
        return base[len("live-"):], role
    if base in ("staging", "sanitize"):
        return base, role
    return None, None


# ---------------------------------------------------------------------------
# Docker events collector
# ---------------------------------------------------------------------------

_DOCKER_LEVEL = {
    "die": "error",
    "oom": "error",
}


def _docker_event_level(action, attributes):
    if action.startswith("health_status:"):
        return "warning" if "unhealthy" in action else "info"
    if action == "die":
        exit_code = attributes.get("exitCode", "0")
        return "info" if exit_code == "0" else "error"
    return _DOCKER_LEVEL.get(action, "info")


def docker_events_worker():
    while not _stop.is_set():
        try:
            proc = subprocess.Popen(
                ["docker", "events", "--format", "{{json .}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout:
                if _stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("Type") != "container":
                    continue
                attrs = evt.get("Actor", {}).get("Attributes", {})
                name = attrs.get("name", "")
                client, role = resolve_client(name)
                if client is None:
                    continue
                action = evt.get("Action", "")
                write_entry("docker", {
                    "level": _docker_event_level(action, attrs),
                    "client": client,
                    "container": name,
                    "role": role,
                    "event": action,
                    "exit_code": (
                        int(attrs["exitCode"]) if action == "die" and "exitCode" in attrs else None
                    ),
                })
            proc.wait()
        except FileNotFoundError:
            time.sleep(5)
        except Exception:
            pass
        if not _stop.is_set():
            time.sleep(2)  # docker events stream died (daemon restart?) -- retry


# ---------------------------------------------------------------------------
# Odoo log tailer
# ---------------------------------------------------------------------------

_ODOO_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<pid>\d+) (?P<level>\S+) (?P<dbname>\S+) (?P<logger>\S+): (?P<message>.*)$"
)
_ODOO_LEVELS_KEPT = {"warning", "error", "critical"}


def _odoo_ts_to_iso(raw):
    # "2026-08-06 10:15:23,456" -> "2026-08-06T10:15:23.456Z"
    date_part, ms = raw.split(",")
    return date_part.replace(" ", "T") + "." + ms + "Z"


_last_line_ts = {}  # container name -> raw docker timestamp string, cross-generation
_last_ts_lock = threading.Lock()


def _get_since(container):
    with _last_ts_lock:
        return _last_line_ts.get(container, "0s")


def _set_since(container, raw_ts):
    with _last_ts_lock:
        _last_line_ts[container] = raw_ts


class _OdooTailer:
    """One instance per -web container. Buffers a traceback that trails an
    ERROR/CRITICAL line (any non-timestamped line is a continuation of
    whatever came before it -- Odoo's own log format has no other way to
    tell a multi-line traceback apart from a fresh entry)."""

    def __init__(self, container, client):
        self.container = container
        self.client = client
        self.pending = None

    def _flush(self):
        if self.pending and self.pending["level"] in _ODOO_LEVELS_KEPT:
            entry = {
                "ts": self.pending["ts"],
                "level": self.pending["level"],
                "client": self.client,
                "logger": self.pending["logger"],
                "message": self.pending["message"],
            }
            if self.pending["traceback"]:
                entry["traceback"] = self.pending["traceback"].rstrip("\n")
            write_entry("odoo", entry)
        self.pending = None

    def feed_line(self, line):
        m = _ODOO_LINE_RE.match(line)
        if m:
            self._flush()
            # Remember this line's own timestamp (regardless of level) as
            # the resume point if this tailer's process dies and gets
            # respawned -- not just the ts of lines we actually keep, so a
            # long stretch of dropped INFO lines doesn't push the resume
            # point stale.
            _set_since(self.container, _odoo_ts_to_iso(m.group("ts")))
            self.pending = {
                "ts": _odoo_ts_to_iso(m.group("ts")),
                "level": m.group("level").lower(),
                "logger": m.group("logger"),
                "message": m.group("message"),
                "traceback": "",
            }
        elif self.pending is not None:
            self.pending["traceback"] += line + "\n"
        # else: continuation line with no preceding entry (e.g. collector
        # started mid-traceback) -- nothing to attach it to, drop it.

    def close(self):
        self._flush()


def odoo_tail_worker(container, client, generation_stop):
    tailer = _OdooTailer(container, client)
    try:
        # Resume from the last line we actually saw, not "now" -- `docker
        # logs -f` exits when a container stops (even a plain restart) and
        # does not auto-resume, so between that death and the next rescan
        # picking it back up, `--since 0s` would silently skip everything
        # emitted in the gap, including the fast boot-time WARNING/ERROR
        # lines Odoo tends to log within the first second of startup
        # (found live, 2026-08-06). `--since <ts>` is inclusive, so the
        # boundary line may be re-emitted once -- an occasional duplicate
        # JSONL entry is a fine tradeoff for never silently losing one.
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--since", _get_since(container), container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            if _stop.is_set() or generation_stop.is_set():
                break
            tailer.feed_line(line.rstrip("\n"))
        proc.terminate()
    except Exception:
        pass
    finally:
        tailer.close()


def odoo_rescan_loop():
    """Keeps one live tail thread per current -web container. Containers
    that come and go (restarts, new clients rendered) are picked up on the
    next scan rather than requiring a collector restart."""
    active = {}  # container name -> (thread, stop_event)
    while not _stop.is_set():
        try:
            out = subprocess.check_output(
                ["docker", "ps", "--format", "{{.Names}}"], text=True
            )
            current = set(out.split())
        except Exception:
            current = set(active.keys())

        for name in list(active):
            if name not in current:
                _, stop_evt = active.pop(name)
                stop_evt.set()
            elif not active[name][0].is_alive():
                # `docker logs -f` exits when the container stops -- even
                # briefly, on a plain `docker restart` -- and does not
                # auto-resume once it starts again (found live, 2026-08-06).
                # Same container name, dead thread: drop it here so the
                # loop below respawns a fresh tail against the new stream.
                active.pop(name)

        for name in current:
            if name in active or not name.endswith("-web"):
                continue
            client, role = resolve_client(name)
            if client is None:
                continue
            stop_evt = threading.Event()
            t = threading.Thread(
                target=odoo_tail_worker, args=(name, client, stop_evt), daemon=True
            )
            active[name] = (t, stop_evt)
            t.start()

        _stop.wait(RESCAN_INTERVAL_SECONDS)

    for _, stop_evt in active.values():
        stop_evt.set()


# ---------------------------------------------------------------------------
# Retention pruning (routine sources only -- deploy/ is audit, kept forever)
# ---------------------------------------------------------------------------

_ROUTINE_SOURCES = ("docker", "odoo")


def prune_loop():
    while not _stop.is_set():
        cutoff = time.time() - RETENTION_DAYS * 86400
        for source in _ROUTINE_SOURCES:
            d = os.path.join(LOG_DIR, source)
            if not os.path.isdir(d):
                continue
            for fname in os.listdir(d):
                path = os.path.join(d, fname)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        _stop.wait(86400)  # once a day is plenty for a 90-day window


def _handle_sigterm(signum, frame):
    _stop.set()


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    threads = [
        threading.Thread(target=docker_events_worker, daemon=True),
        threading.Thread(target=odoo_rescan_loop, daemon=True),
        threading.Thread(target=prune_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while not _stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        for t in threads:
            t.join(timeout=5)


if __name__ == "__main__":
    main()
