#!/usr/bin/env python3
"""Single long-running collector for the logging/audit sources (docs/
ROADMAP.md, 2026-08-06 logging design): Docker container lifecycle events,
Odoo application log lines, and SSH/sudo access. Runs as one systemd
service with worker threads feeding a shared, lock-protected JSONL writer
-- deploy history (scripts/deploy.sh) already established the format this
follows: dense JSONL, one file per day, UTC ISO8601 timestamps throughout.

Docker thread: consumes `docker events` as a live stream (not polling
`docker ps`), so a crash/OOM/restart is captured the instant it happens.

Odoo threads: one per live/staging/sanitize *-web container, each tailing
`docker logs -f` and parsing Odoo's own log line format. Only WARNING and
above are written -- INFO-level Odoo noise (every request, every cron
tick) has no diagnostic value at the volume it would generate. A periodic
rescan picks up containers that don't exist yet at collector startup
without needing a restart.

SSH thread: tails /var/log/auth.log (readable by `ubuntu` via the `adm`
group, no sudo needed) for sshd login/disconnect events and sudo command
invocations. Every identity here except devs (who hold no SSH key at all)
shares the single `ubuntu` OS user, so the only way to tell them apart is
the authenticating key's own fingerprint -- resolved via a small static
map, see SSH_IDENTITY_BY_FINGERPRINT.

Model-audit thread: reruns migrate_client_to_live.sh's infection-pass SQL
continuously against every live/staging database (not just once, at
migration time) -- ties directly to this environment's proven attack
surface (ir_act_server/ir_ui_view/ir_cron/ir_config_parameter), see
docs/incident/03_2026-08-04_dbresident_cryptominer_backdoor.md.

System-audit thread: the OS/process-layer complement to model-audit --
the actual miner runs as a process, not a database row. Checks known
hideout-file paths, an allowlist of expected process-per-container-role,
host-wide execution-from-writable-dir, unrecognized container names, and
a systemd running-unit allowlist.

HAProxy thread: tails /var/log/haproxy.log (the Ubuntu haproxy package's
own rsyslog rule already captures it -- the original documented gap was
that nothing ever read it, not that the logging needed building) for
every routed request: client, status, timers, request line. HAProxy
itself routes by pseudo-domain (<client>.erp16-sandbox.test, tunnel-only,
never a real domain or public port) to each client's already-published
127.0.0.1:<http_port> backend -- same routing/logging mechanism a real
erp16.orion-instruments.io HAProxy would use, reusable at cutover with
nothing more than a domain-string swap.

Docker/Odoo/HAProxy are "routine" in the audit/routine split -- aged out
after RETENTION_DAYS. SSH, model-audit, and system-audit are audit --
kept forever, never pruned.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

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


def _tail_rescan_loop(suffix, worker_fn):
    """Keeps one live tail thread per current container matching `suffix`
    (e.g. "-web", "-db"), calling worker_fn(container, client, stop_event)
    for each. Containers that come and go (restarts, new clients rendered)
    are picked up on the next scan rather than requiring a collector
    restart. Shared by the Odoo and Postgres tailers -- identical
    reconnect-safety logic either way, `docker logs -f` exits the instant
    a container stops (even a plain restart, not just a stop) and does not
    auto-resume once it starts again (found live, 2026-08-06 building the
    Odoo tailer) -- an existing-but-dead thread is otherwise invisible to
    a scan that only tracks container *names*."""
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
                active.pop(name)

        for name in current:
            if name in active or not name.endswith(suffix):
                continue
            client, role = resolve_client(name)
            if client is None:
                continue
            stop_evt = threading.Event()
            t = threading.Thread(
                target=worker_fn, args=(name, client, stop_evt), daemon=True
            )
            active[name] = (t, stop_evt)
            t.start()

        _stop.wait(RESCAN_INTERVAL_SECONDS)

    for _, stop_evt in active.values():
        stop_evt.set()


def odoo_rescan_loop():
    _tail_rescan_loop("-web", odoo_tail_worker)


# ---------------------------------------------------------------------------
# Postgres connection/auth logs (audit -- kept forever, never pruned)
#
# log_connections/log_disconnections are off by default -- without them
# Postgres only ever logs FATAL/ERROR-level events, never a real auth
# failure or a connection's actual source, which is the entire security
# gap this source exists to close (docs/ROADMAP.md, 2026-08-06 logging
# design). Enabled via the docker-compose template's `command:` override
# on every db service (templates/docker-compose.template.yml.j2) --
# requires the -db container to be recreated/restarted to take effect,
# same as the log_connections config itself.
#
# Simplest of the six sources to parse: Postgres already logs in UTC
# natively with an explicit "UTC" marker in every line (confirmed live,
# 2026-08-06) -- no timezone conversion needed, unlike SSH's auth.log.
# ---------------------------------------------------------------------------

_PG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) UTC \[(?P<pid>\d+)\] "
    r"(?P<level>[A-Z]+):\s+(?P<msg>.*)$"
)
_PG_CONN_AUTHORIZED_RE = re.compile(
    r"^connection authorized: user=(?P<user>\S+) database=(?P<database>\S+)"
)
_PG_DISCONNECTION_RE = re.compile(
    r"^disconnection: session time: (?P<session_time>\S+) user=(?P<user>\S+) "
    r"database=(?P<database>\S+)(?: host=(?P<host>\S+))?"
)
# Postgres's own standard, well-documented message text for a rejected
# credential -- not reproducible live on this sandbox (every db container
# uses trust auth for same-network connections, by design, same as every
# `docker exec ... psql` call already made throughout this project), but
# stable and undocumented-to-change wording straight from Postgres's own
# source (auth.c).
_PG_AUTH_FAILURE_RE = re.compile(r"password authentication failed for user")


def _pg_ts_to_iso(raw):
    # "2026-08-06 08:57:38.761" -> "2026-08-06T08:57:38.761Z"
    return raw.replace(" ", "T") + "Z"


class _PostgresTailer:
    """One instance per -db container. ERROR lines are followed by an
    optional STATEMENT: continuation line (the offending SQL) -- buffered
    and attached the same way the Odoo tailer buffers a traceback."""

    def __init__(self, container, client):
        self.container = container
        self.client = client
        self.pending_error = None  # {"ts", "message"} awaiting a possible STATEMENT: line

    def _flush_pending_error(self):
        if self.pending_error:
            write_entry("postgres", {
                "level": "warning",
                "event": "error",
                "client": self.client,
                **self.pending_error,
            })
        self.pending_error = None

    def feed_line(self, line):
        m = _PG_LINE_RE.match(line)
        if not m:
            return  # continuation line with no preceding entry we're tracking
        ts, level, msg = m.group("ts"), m.group("level"), m.group("msg")
        iso_ts = _pg_ts_to_iso(ts)

        if self.pending_error is not None:
            # Postgres emits STATEMENT as its own log level, not a plain
            # continuation line like Odoo's tracebacks -- check level, not msg.
            if level == "STATEMENT":
                self.pending_error["statement"] = msg
                self._flush_pending_error()
                return
            self._flush_pending_error()
            # fall through -- this line is a fresh entry, handle below

        cm = _PG_CONN_AUTHORIZED_RE.match(msg)
        if cm:
            write_entry("postgres", {
                "ts": iso_ts, "level": "info", "event": "connection_authorized",
                "client": self.client, "user": cm.group("user"), "database": cm.group("database"),
            })
            return

        dm = _PG_DISCONNECTION_RE.match(msg)
        if dm:
            write_entry("postgres", {
                "ts": iso_ts, "level": "info", "event": "disconnection",
                "client": self.client, "user": dm.group("user"), "database": dm.group("database"),
                "session_time": dm.group("session_time"), "host": dm.group("host"),
            })
            return

        if level == "FATAL":
            is_auth_failure = bool(_PG_AUTH_FAILURE_RE.search(msg))
            write_entry("postgres", {
                "ts": iso_ts,
                "level": "critical" if is_auth_failure else "warning",
                "event": "auth_failure" if is_auth_failure else "fatal",
                "client": self.client, "message": msg,
            })
            return

        if level == "ERROR":
            self.pending_error = {"ts": iso_ts, "message": msg}
            return

        # LOG-level connection-received/startup/shutdown noise -- skip,
        # redundant with the docker/ source's own start/stop events and
        # connection_authorized/disconnection above already covering the
        # actual security-relevant moments of a session's lifecycle.

    def close(self):
        self._flush_pending_error()


def postgres_tail_worker(container, client, generation_stop):
    tailer = _PostgresTailer(container, client)
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--since", _get_since(container), container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            if _stop.is_set() or generation_stop.is_set():
                break
            raw = line.rstrip("\n")
            m = _PG_LINE_RE.match(raw)
            if m:
                _set_since(container, _pg_ts_to_iso(m.group("ts")))
            tailer.feed_line(raw)
        proc.terminate()
    except Exception:
        pass
    finally:
        tailer.close()


def postgres_rescan_loop():
    _tail_rescan_loop("-db", postgres_tail_worker)


# ---------------------------------------------------------------------------
# SSH / sudo access (audit -- kept forever, never pruned)
# ---------------------------------------------------------------------------

AUTH_LOG = "/var/log/auth.log"

# Every identity here except devs (who hold no SSH key at all, by design --
# see docs/rehearsal/IDENTITIES.md) shares the single `ubuntu` OS user, so
# the authenticating key's own fingerprint is the only thing that actually
# distinguishes them in the log. Extend this map whenever a new key is
# added to ~/.ssh/authorized_keys -- an unrecognized fingerprint still gets
# logged (as "unknown:<fingerprint>"), never silently dropped, so a stale
# map is visible here rather than invisible.
SSH_IDENTITY_BY_FINGERPRINT = {
    "SHA256:N+Y4UXM4BB8w3z132yDimx2vmY4vF09p5lzEPel9dsw": "erp16-sandbox-key",
    "SHA256:BeV7GBw1B654Z2mCR/w1nKnURciU7ZNNhazknx2IOUo": "sachin-admin",
}

_SYSLOG_LINE_RE = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"\S+ (?P<proc>[^:\[]+)(?:\[\d+\])?: (?P<msg>.*)$"
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_SSH_ACCEPTED_RE = re.compile(
    r"^Accepted publickey for (?P<user>\S+) from (?P<ip>\S+) port \d+ \S+: \S+ (?P<fp>SHA256:\S+)$"
)
_SSH_FAILED_RE = re.compile(
    r"^Failed password for (?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port \d+"
)
_SSH_INVALID_RE = re.compile(
    r"^Invalid user (?P<user>\S+) from (?P<ip>\S+) port \d+"
)
_SSH_DISCONNECT_RE = re.compile(
    r"^Disconnected from (?:(?:invalid |authenticating )?user (?P<user>\S+) )?(?P<ip>\S+) port \d+"
)
_SUDO_COMMAND_RE = re.compile(
    r"^\s*(?P<user>\S+)\s*:.*PWD=(?P<pwd>\S+)\s*;\s*USER=(?P<target>\S+)\s*;\s*COMMAND=(?P<command>.*)$"
)

_HOST_TZ = timezone(timedelta(hours=5, minutes=30))  # sandbox host is Asia/Kolkata -- confirmed live, 2026-08-06


def _syslog_ts_to_iso(mon, day, time_str):
    """auth.log timestamps have no year and no timezone -- they're the
    host's local time (IST here, confirmed via `timedatectl` live,
    2026-08-06), not UTC like every other source in this collector.
    Infers the year from the current date; if that puts the parsed
    moment more than a day in the future (only possible right at a
    Dec->Jan rollover), assumes it was actually last year."""
    now = datetime.now(_HOST_TZ)
    hh, mm, ss = (int(x) for x in time_str.split(":"))
    candidate = datetime(now.year, _MONTHS[mon], int(day), hh, mm, ss, tzinfo=_HOST_TZ)
    if candidate > now + timedelta(days=1):
        candidate = candidate.replace(year=now.year - 1)
    return candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_ssh_identity(fingerprint):
    return SSH_IDENTITY_BY_FINGERPRINT.get(fingerprint, f"unknown:{fingerprint}")


def _parse_auth_line(line):
    """Returns an entry dict for the SSH source, or None for lines with no
    audit value (pam_unix session bracketing, systemd-logind noise, sudo's
    own session-open/close lines -- all redundant with the sshd Accepted/
    Disconnected and sudo COMMAND lines this actually keeps)."""
    m = _SYSLOG_LINE_RE.match(line)
    if not m:
        return None
    ts = _syslog_ts_to_iso(m.group("mon"), m.group("day"), m.group("time"))
    proc, msg = m.group("proc"), m.group("msg")

    if proc == "sshd":
        am = _SSH_ACCEPTED_RE.match(msg)
        if am:
            return {
                "ts": ts, "level": "info", "event": "login_success",
                "os_user": am.group("user"), "src_ip": am.group("ip"),
                "fingerprint": am.group("fp"),
                "identity": _resolve_ssh_identity(am.group("fp")),
            }
        fm = _SSH_FAILED_RE.match(msg)
        if fm:
            return {
                "ts": ts, "level": "warning", "event": "login_failure",
                "os_user": fm.group("user"), "src_ip": fm.group("ip"),
            }
        im = _SSH_INVALID_RE.match(msg)
        if im:
            return {
                "ts": ts, "level": "warning", "event": "login_failure",
                "os_user": im.group("user"), "src_ip": im.group("ip"),
            }
        dm = _SSH_DISCONNECT_RE.match(msg)
        if dm:
            return {
                "ts": ts, "level": "info", "event": "disconnect",
                "os_user": dm.group("user"), "src_ip": dm.group("ip"),
            }
        return None

    if proc == "sudo" and "COMMAND=" in msg:
        sm = _SUDO_COMMAND_RE.match(msg)
        if sm:
            return {
                "ts": ts, "level": "audit", "event": "sudo_command",
                "os_user": sm.group("user"), "target_user": sm.group("target"),
                "pwd": sm.group("pwd"), "command": sm.group("command"),
            }
        return None

    return None


def ssh_tail_worker():
    while not _stop.is_set():
        try:
            proc = subprocess.Popen(
                ["tail", "-F", "-n", "0", AUTH_LOG],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout:
                if _stop.is_set():
                    break
                entry = _parse_auth_line(line.rstrip("\n"))
                if entry:
                    write_entry("ssh", entry)
            proc.terminate()
        except FileNotFoundError:
            time.sleep(5)
        except Exception:
            pass
        if not _stop.is_set():
            time.sleep(2)


# ---------------------------------------------------------------------------
# HAProxy access logs (routine -- aged out after RETENTION_DAYS, per the
# original 2026-08-06 logging design's own source table, unlike SSH/
# model-audit/system-audit/postgres which are audit-forever)
#
# Closes the long-standing documented gap from the original June security
# audit (CLAUDE.md's TODO list, never closed until now): "Fix HAProxy
# access logging (syslog local0, nothing captures it)". Turns out the
# Ubuntu haproxy package already ships a working rsyslog rule
# (/etc/rsyslog.d/49-haproxy.conf, installed automatically) that captures
# everything to /var/log/haproxy.log -- the actual gap was that nothing
# ever *read* that file, not that the logging itself needed building.
#
# Same syslog wrapper format as auth.log (confirmed live, 2026-08-06),
# so this reuses _SYSLOG_LINE_RE/_HOST_TZ/_MONTHS from the SSH section
# rather than re-deriving them. HAProxy's own embedded accept_date inside
# the message (millisecond precision) is used for `ts`, not the outer
# syslog wrapper's second-precision timestamp.
# ---------------------------------------------------------------------------

HAPROXY_LOG = "/var/log/haproxy.log"

# HAProxy's default `httplog` format (global `option httplog` in
# haproxy.cfg): client_ip:port [accept_date] frontend backend/server
# Tq/Tw/Tc/Tr/Tt status bytes req_cookie resp_cookie term_state
# actconn/feconn/beconn/srvconn/retries srv_queue/backend_queue "request"
_HAPROXY_HTTPLOG_RE = re.compile(
    r'^(?P<client_ip>\S+):(?P<client_port>\d+) '
    r'\[(?P<accept_date>[^\]]+)\] '
    r'(?P<frontend>\S+) (?P<backend>\S+)/(?P<server>\S+) '
    r'(?P<tq>-?\d+)/(?P<tw>-?\d+)/(?P<tc>-?\d+)/(?P<tr>-?\d+)/(?P<tt>\+?-?\d+) '
    r'(?P<status>\d+) (?P<bytes>\d+) \S+ \S+ (?P<term_state>\S+) '
    r'\d+/\d+/\d+/\d+/\+?\d+ \d+/\d+ '
    r'"(?P<request>[^"]*)"$'
)


def _haproxy_ts_to_iso(accept_date):
    # "06/Aug/2026:14:59:09.016" (host-local IST, confirmed live) -> UTC ISO8601
    dt = datetime.strptime(accept_date, "%d/%b/%Y:%H:%M:%S.%f").replace(tzinfo=_HOST_TZ)
    dt = dt.astimezone(timezone.utc)
    milliseconds = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def _haproxy_level(status):
    if status >= 500:
        return "error"
    if status >= 400:
        return "warning"
    return "info"


def _parse_haproxy_line(line):
    m = _SYSLOG_LINE_RE.match(line)
    if not m or m.group("proc") != "haproxy":
        return None
    hm = _HAPROXY_HTTPLOG_RE.match(m.group("msg"))
    if not hm:
        return None  # haproxy's own [WARNING]/[NOTICE] process-lifecycle lines, not a request
    status = int(hm.group("status"))
    # backend name is "be_<client>" by this config's own naming convention
    # (templates section above) -- "be_unrecognized" for a request whose
    # Host header didn't match any known pseudo-domain, which has no
    # `client` to report.
    backend = hm.group("backend")
    client = backend[len("be_"):] if backend != "be_unrecognized" else None
    return {
        "ts": _haproxy_ts_to_iso(hm.group("accept_date")),
        "level": _haproxy_level(status),
        "client": client,
        "client_ip": hm.group("client_ip"),
        "status": status,
        "bytes": int(hm.group("bytes")),
        "term_state": hm.group("term_state"),
        "request": hm.group("request"),
    }


def haproxy_tail_worker():
    while not _stop.is_set():
        try:
            proc = subprocess.Popen(
                ["tail", "-F", "-n", "0", HAPROXY_LOG],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout:
                if _stop.is_set():
                    break
                entry = _parse_haproxy_line(line.rstrip("\n"))
                if entry:
                    write_entry("haproxy", entry)
            proc.terminate()
        except FileNotFoundError:
            time.sleep(5)
        except Exception:
            pass
        if not _stop.is_set():
            time.sleep(2)


# ---------------------------------------------------------------------------
# Model-level audit (audit -- kept forever, never pruned)
#
# Reuses migrate_client_to_live.sh's infection-pass queries, run
# continuously instead of only at migration time (docs/ROADMAP.md,
# 2026-08-06 logging design) -- ties directly to this environment's real
# incident history (docs/incident/03_2026-08-04_dbresident_cryptominer_
# backdoor.md): every actual compromise here was an attacker writing into
# exactly these tables (ir_act_server/ir_ui_view/ir_cron/
# ir_config_parameter), so this closes the exact proven attack surface,
# not generic hardening.
# ---------------------------------------------------------------------------

MODEL_AUDIT_POLL_SECONDS = 300      # 5 min -- cheap COUNT queries, fast detection
MODEL_AUDIT_HEARTBEAT_SECONDS = 3600  # only log "checked, 0 findings" hourly,
                                       # not every poll -- an absent heartbeat
                                       # is itself the "poller died" signal,
                                       # without flooding an audit-forever bucket

# Same four checks migrate_client_to_live.sh already runs pre-migration,
# generalized from "against one just-pulled dump" to "continuously against
# every live database" -- (1) known-signature name matches from the actual
# strains found in this environment's history, (2)-(4) the broader
# structural checks (any non-system code action / suspicious config key /
# injected base-template view) that the 2026-08-04 exhaustive scan showed
# actually catch what signature-matching alone misses. `nonstandard_cron`
# is new here -- the exhaustive scan's TODO explicitly called out "any
# ir.cron pointing at unexpected actions" as still-unchecked at the time.
MODEL_AUDIT_QUERIES = {
    "known_signature_hits": (
        "select count(*) from ir_act_server where name::text ilike '%\"_ep\"%' "
        "or name::text ilike '%_db_health_monitor%' "
        "or name::text ilike '%_bd_assemble%' "
        "or name::text ilike '%_rce_tmp%';"
    ),
    "nonstandard_code_actions": (
        "select count(*) from ir_act_server where state='code' and create_uid != 1;"
    ),
    "suspicious_config_params": (
        "select count(*) from ir_config_parameter where key ~ '^_[a-z]';"
    ),
    "injected_web_layout_views": (
        "select count(*) from ir_ui_view where (inherit_id=180 or key ilike 'gen_key%') "
        "and create_uid != 1;"
    ),
    "nonstandard_cron": (
        "select count(*) from ir_cron where active=true and create_uid != 1;"
    ),
}


def _list_databases(container):
    out = subprocess.check_output(
        ["docker", "exec", container, "psql", "-U", "odoo", "-d", "postgres", "-tAc",
         "select datname from pg_database where not datistemplate "
         "and datname not in ('postgres','template0','template1');"],
        text=True, stderr=subprocess.DEVNULL,
    )
    return [d.strip() for d in out.splitlines() if d.strip()]


def _run_model_audit_query(container, dbname, sql):
    out = subprocess.check_output(
        ["docker", "exec", container, "psql", "-U", "odoo", "-d", dbname, "-tAc", sql],
        text=True, stderr=subprocess.DEVNULL,
    )
    return int(out.strip())


def model_audit_poll_once():
    """One full pass over every live/staging -db container's actual
    databases. Returns (databases_checked, total_findings) for the
    heartbeat. Sanitize is deliberately excluded -- it hosts a different
    client's data on every pipeline run, so a hit there is far more likely
    ephemeral/hard-to-attribute noise than a real finding, and the pull
    step already has its own infection pass ahead of this poller."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"], text=True
        )
        containers = out.split()
    except Exception:
        return 0, 0

    databases_checked = 0
    total_findings = 0
    for container in containers:
        client, role = resolve_client(container)
        if client is None or role != "db" or client == "sanitize":
            continue
        try:
            dbnames = _list_databases(container)
        except Exception:
            continue
        for dbname in dbnames:
            databases_checked += 1
            for check, sql in MODEL_AUDIT_QUERIES.items():
                try:
                    count = _run_model_audit_query(container, dbname, sql)
                except Exception:
                    continue
                if count > 0:
                    total_findings += count
                    write_entry("model_audit", {
                        "level": "critical",
                        "event": "infection_signature_match",
                        "client": client,
                        "container": container,
                        "db": dbname,
                        "check": check,
                        "count": count,
                    })
    return databases_checked, total_findings


def model_audit_loop():
    last_heartbeat = 0.0
    while not _stop.is_set():
        databases_checked, total_findings = model_audit_poll_once()
        now = time.time()
        if total_findings == 0 and now - last_heartbeat >= MODEL_AUDIT_HEARTBEAT_SECONDS:
            write_entry("model_audit", {
                "level": "info",
                "event": "poll_heartbeat",
                "databases_checked": databases_checked,
                "total_findings": 0,
            })
            last_heartbeat = now
        _stop.wait(MODEL_AUDIT_POLL_SECONDS)


# ---------------------------------------------------------------------------
# System/process audit (audit -- kept forever, never pruned)
#
# model_audit/ catches the backdoor at the DB layer (where it's actually
# planted); this catches it at the OS/process layer (where it actually
# runs) -- the miner itself is a process, not a database row, so this is
# the complement, not a duplicate. Baselined against this sandbox's real
# running state (2026-08-06), not guessed.
# ---------------------------------------------------------------------------

SYSTEM_AUDIT_POLL_SECONDS = 300
SYSTEM_AUDIT_HEARTBEAT_SECONDS = 3600

# The exact paths the actual XMRig backdoor used in this environment's own
# incident history (docs/incident/03_2026-08-04_dbresident_cryptominer_
# backdoor.md) to cache/disguise its binary. Checked on the host and
# inside every live/staging/sanitize container -- COPY PROGRAM (the
# mechanism that planted these originally) executes on whichever
# container's Postgres process runs it, not the host.
HIDEOUT_PATHS = (
    "/tmp/.odoo_worker_monitor",
    "/dev/shm/.pg_health",
    "/var/tmp/.odoo_pg_health",
)

# Expected process pattern per container role -- an allowlist (structural,
# same "positive match" philosophy as model_audit's create_uid != 1 checks)
# rather than a signature blacklist, so it catches an unknown/renamed
# strain too, not just known ones. Confirmed live against every currently
# running container (2026-08-06): every -web container runs exactly one
# odoo process family as uid 101, every -db container runs exactly the
# standard postgres process family as uid 999 -- nothing else, ever, in a
# correctly-functioning container. `uid` is checked strictly here -- the
# actual persistent service process running under the wrong uid would
# itself be a real anomaly.
_EXPECTED_PROCESS = {
    "web": (101, re.compile(r"^/usr/bin/python3 /usr/bin/odoo\b")),
    # \b, not "(:|$)" -- the postgres/ logging source (2026-08-06) added a
    # `command: ["postgres", "-c", "log_connections=on", ...]` override to
    # every db service, which changes the *main* postgres process's own
    # argv to "postgres -c log_connections=on ..." instead of a bare
    # "postgres". Worker processes still rename themselves via setproctitle
    # to "postgres: checkpointer" etc regardless of how the parent was
    # invoked. \b matches all three shapes correctly.
    "db": (999, re.compile(r"^postgres\b")),
}

# This collector's own `docker exec <container> psql ...` (model_audit)
# and `docker exec <container> test -e ...` (system_audit's own hideout-
# path check) run *inside* the exact containers this check audits, and
# `docker exec` defaults to root (uid 0) regardless of the image's normal
# runtime user -- found live, 2026-08-06, both showed up as false-positive
# "unexpected process" findings against their own audit run. Allowlisted
# by command, not uid, and deliberately narrow (whole-word match only):
# the actual exploit mechanism found in this environment's history (COPY
# ... FROM PROGRAM) spawns a shell/binary child of the Postgres backend
# process itself, never a top-level `psql` client invocation, so this
# doesn't weaken detection of the real attack pattern -- an attacker
# spawning curl/wget/bash/an unknown binary still doesn't match either
# word and still gets flagged.
_AUDIT_TOOL_RE = re.compile(r"\b(psql|test)\b")

# Confirmed live via `systemctl list-units --type=service --state=running`
# (2026-08-06) -- stock Ubuntu/Docker/AWS-SSM services plus this collector
# itself. Extend when a real new service is intentionally added; anything
# else running is exactly what this check exists to catch.
KNOWN_SYSTEMD_UNITS = {
    "acpid.service", "chrony.service", "containerd.service", "cron.service",
    "dbus.service", "docker.service", "erp16-logging-collector.service",
    "getty@tty1.service", "irqbalance.service", "multipathd.service",
    "networkd-dispatcher.service", "polkit.service", "rsyslog.service",
    "serial-getty@ttyS0.service",
    "snap.amazon-ssm-agent.amazon-ssm-agent.service", "snapd.service",
    "ssh.service", "systemd-fsckd.service", "systemd-hostnamed.service",
    "systemd-journald.service", "systemd-logind.service",
    "systemd-networkd.service", "systemd-resolved.service",
    "systemd-timedated.service", "systemd-udevd.service",
    "unattended-upgrades.service", "user@1000.service",
}

# Any process (host-wide `ps`, which on this box's default non-namespaced
# Docker setup already includes every container's processes too -- verified
# live, container uids 101/999 are directly visible in host `ps`) whose
# *executed binary* -- not just some argument it was passed -- sits under
# one of these writable, non-standard directories: exactly where this
# attacker has staged every binary found in this environment's history,
# and a general execution-from-writable-dir heuristic that doesn't depend
# on knowing the binary's name in advance. Anchored to the start of the
# command (optionally past a thin `nohup`/`sh -c`/`bash -c` wrapper, since
# GNU nohup execve()s its target so the wrapped binary ends up as the
# command's own argv[0]) -- deliberately NOT a bare substring search.
# Found live, 2026-08-06, two false-positive classes an unanchored search
# produced: (1) `runc`'s own internal `--process /tmp/runc-process<n>`
# bookkeeping, spawned by every single `docker exec` call including this
# collector's own; (2) this collector's own hideout-path check itself,
# `docker exec <container> test -e /dev/shm/.pg_health` -- the path is an
# *argument* to `test`, not something being executed.
_WRITABLE_DIR_RE = re.compile(r"^(?:nohup\s+|sh\s+-c\s+|bash\s+-c\s+)?(/tmp/|/dev/shm/|/var/tmp/)\S+")


def _write_system_audit(event, **fields):
    entry = {"level": "critical", "event": event}
    entry.update(fields)
    write_entry("system_audit", entry)


def _check_hideout_paths():
    findings = 0
    for path in HIDEOUT_PATHS:
        if os.path.exists(path):
            findings += 1
            _write_system_audit("hideout_path_exists", scope="host", path=path)
    try:
        out = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True)
        containers = out.split()
    except Exception:
        containers = []
    for container in containers:
        for path in HIDEOUT_PATHS:
            try:
                rc = subprocess.run(
                    ["docker", "exec", container, "test", "-e", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ).returncode
            except Exception:
                continue
            if rc == 0:
                findings += 1
                _write_system_audit(
                    "hideout_path_exists", scope="container", container=container, path=path
                )
    return findings


def _check_container_processes():
    findings = 0
    try:
        out = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True)
        containers = out.split()
    except Exception:
        return 0
    for container in containers:
        client, role = resolve_client(container)
        if role not in _EXPECTED_PROCESS:
            continue  # unrecognized-name containers are check 4's job, not this one's
        expected_uid, expected_cmd_re = _EXPECTED_PROCESS[role]
        try:
            out = subprocess.check_output(
                ["docker", "top", container, "-eo", "uid,pid,args"], text=True
            )
        except Exception:
            continue
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            uid_str, pid, cmd = parts
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            if expected_cmd_re.match(cmd):
                if uid != expected_uid:
                    findings += 1
                    _write_system_audit(
                        "unexpected_container_process", container=container, client=client,
                        role=role, uid=uid, pid=pid, command=cmd,
                        reason="service process running as wrong uid",
                    )
                continue
            if _AUDIT_TOOL_RE.search(cmd):
                continue  # this collector's own psql/test diagnostic invocation
            findings += 1
            _write_system_audit(
                "unexpected_container_process", container=container, client=client,
                role=role, uid=uid, pid=pid, command=cmd,
                reason="unrecognized process",
            )
    return findings


def _check_writable_dir_execution():
    findings = 0
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,uid,args", "--no-headers"], text=True
        )
    except Exception:
        return 0
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, uid, cmd = parts
        if _WRITABLE_DIR_RE.search(cmd):
            findings += 1
            _write_system_audit("writable_dir_execution", scope="host", uid=uid, pid=pid, command=cmd)
    return findings


def _check_unrecognized_containers():
    findings = 0
    try:
        out = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True)
        containers = out.split()
    except Exception:
        return 0
    for container in containers:
        client, role = resolve_client(container)
        if client is None:
            findings += 1
            _write_system_audit("unrecognized_container", container=container)
    return findings


def _check_systemd_units():
    findings = 0
    try:
        out = subprocess.check_output(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend", "--plain"], text=True
        )
    except Exception:
        return 0
    for line in out.splitlines():
        unit = line.split()[0] if line.split() else None
        if unit and unit not in KNOWN_SYSTEMD_UNITS:
            findings += 1
            _write_system_audit("systemd_unit_not_allowlisted", unit=unit)
    return findings


def system_audit_poll_once():
    total = 0
    total += _check_hideout_paths()
    total += _check_container_processes()
    total += _check_writable_dir_execution()
    total += _check_unrecognized_containers()
    total += _check_systemd_units()
    return total


def system_audit_loop():
    last_heartbeat = 0.0
    while not _stop.is_set():
        total_findings = system_audit_poll_once()
        now = time.time()
        if total_findings == 0 and now - last_heartbeat >= SYSTEM_AUDIT_HEARTBEAT_SECONDS:
            write_entry("system_audit", {
                "level": "info", "event": "poll_heartbeat", "total_findings": 0,
            })
            last_heartbeat = now
        _stop.wait(SYSTEM_AUDIT_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Retention pruning (routine sources only -- deploy/, ssh/, model_audit/,
# and system_audit/ are audit, kept forever)
# ---------------------------------------------------------------------------

_ROUTINE_SOURCES = ("docker", "odoo", "haproxy")


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
        threading.Thread(target=postgres_rescan_loop, daemon=True),
        threading.Thread(target=ssh_tail_worker, daemon=True),
        threading.Thread(target=haproxy_tail_worker, daemon=True),
        threading.Thread(target=model_audit_loop, daemon=True),
        threading.Thread(target=system_audit_loop, daemon=True),
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
