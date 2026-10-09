#!/usr/bin/env python3
"""creai-axis usage collector (opt-in).

Reads Claude Code session transcripts on this machine, extracts one metadata-only event per
creai-axis skill invocation, and sends them to the team's ingest endpoint.

Nothing runs until the developer opts in (`optin`), which writes the config file below.
Only metadata leaves the machine: timestamp, session id, repo name, branch, Claude Code
version, plugin, plugin version, skill, and whether it was typed (slash) or invoked by Claude
(model). Prompts, skill arguments, code and tool output are never read into an event.

Subcommands:
  hook       SessionStart/SessionEnd hook entry point (reads hook JSON on stdin). Never fails.
  optin      Store endpoint + ingest token (token read from stdin, never from argv).
  optout     Delete local config, outbox and cursors.
  backfill   Scan every transcript on this machine (one-time catch-up after opting in).
  flush      Retry sending queued events.
  status     Show opt-in state, queue size and last send result.
  extract    Print events from the given transcript files as JSONL (debugging; sends nothing).
Design: openspec/changes/axis-usage-telemetry/design.md
"""
import argparse
import contextlib
import functools
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

MARKETPLACE = "creai-axis"
FALLBACK_PLUGINS = {"creai-common", "creai-backend", "creai-frontend", "creai-data", "creai-arch"}
COMMAND_RE = re.compile(r"<command-name>/?([a-z0-9-]+(?::[a-z0-9-]+)?)</command-name>")
BATCH_SIZE = 500
HTTP_TIMEOUT_S = 5
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Claude Code kills the hook at 10 s (hooks.json). Stop scanning and sending well before that and
# leave the rest for the next hook: unscanned files stay listed as incomplete, unsent events stay queued.
HOOK_BUDGET_S = 8
STALE_CLAIM_S = 600  # a claimed outbox older than this belongs to a process that died mid-send
LOCK_TIMEOUT_S = 2  # the lock is only held for small file operations, never while scanning or sending
# Only records containing one of these can hold an event. Everything else (tool output, which can
# be megabytes per line) is skipped without parsing, which keeps scans fast and inside the budget.
RECORD_MARKERS = (b'"Skill"', b"<command-name>")
MAX_RECORD_BYTES = 8 * 1024 * 1024  # a matching record this large is pasted content, not an invocation
ABANDONED_PARTIAL_S = 86400  # a transcript whose last line stayed cut off this long was never finished

STATE_DIR = Path(os.environ.get("CREAI_AXIS_USAGE_DIR") or Path.home() / ".config" / "creai-axis")
CONFIG_FILE = STATE_DIR / "usage.json"
OUTBOX_FILE = STATE_DIR / "usage-outbox.jsonl"
CLAIM_GLOB = "usage-outbox.*.sending"  # flush renames the outbox to one of these before sending it
CURSOR_FILE = STATE_DIR / "usage-cursors.json"
LAST_SEND_FILE = STATE_DIR / "usage-last-send.json"
LOCK_FILE = STATE_DIR / "usage.lock"
# One file per transcript to rescan, written without the lock, for when collect couldn't get it.
RESCAN_DIR = STATE_DIR / "usage-rescan"


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class PluginIndex:
    """Which installed plugins belong to creai-axis, their versions, and which plugin owns each skill."""

    def __init__(self):
        self.versions = {}
        self.skill_owner = {}
        installed = read_json(claude_dir() / "plugins" / "installed_plugins.json", {}).get("plugins", {})
        for key, installs in installed.items():
            name, _, marketplace = key.partition("@")
            if marketplace != MARKETPLACE or not installs:
                continue
            latest = installs[-1]
            self.versions[name] = latest.get("version")
            skills_dir = Path(latest.get("installPath", "")) / "skills"
            if skills_dir.is_dir():
                for skill in skills_dir.iterdir():
                    self.skill_owner.setdefault(skill.name, name)
        if not self.versions:
            self.versions = dict.fromkeys(FALLBACK_PLUGINS)

    def resolve(self, name):
        """'creai-common:creai-implement' or bare 'creai-implement' -> (plugin, skill), or None if not axis."""
        plugin, sep, skill = name.partition(":")
        if not sep:
            skill, plugin = plugin, self.skill_owner.get(plugin)
        if plugin in self.versions and skill:
            return plugin, skill
        return None


def _os_username():
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no usable user name in the environment
        return None


def _same_name(a, b):
    """Case-insensitive match: Windows and default macOS filesystems ignore case, so JDOE is jdoe."""
    return a is not None and b is not None and str(a).casefold() == str(b).casefold()


@functools.lru_cache(maxsize=512)
def repo_name(cwd):
    """Name of the project a session ran in, or None if that name could identify the OS user.

    Uses the enclosing git repository root when there is one below the home directory, so sessions
    started in a subfolder count toward their repo; otherwise the cwd folder itself. Never the home
    directory (its name is the OS user name) or any folder named like the user.
    """
    if not cwd:
        return None
    path = Path(cwd)
    try:
        home = Path.home()
    except (KeyError, RuntimeError):
        home = None
    project = path
    for candidate in (path, *path.parents):
        if _same_name(candidate, home):
            break  # don't climb into home: a dotfiles repo there would name every session after the user
        try:
            if (candidate / ".git").exists():
                project = candidate
                break
        except OSError:
            break
    if not project.name or _same_name(project, home) or _same_name(project.name, _os_username()):
        return None
    return project.name


def event_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def events_from_record(rec, index):
    rec_type = rec.get("type")
    if rec_type not in ("user", "assistant"):
        return
    content = (rec.get("message") or {}).get("content")
    found = []
    if rec_type == "user" and isinstance(content, str):
        found = [(name, "slash", rec.get("uuid")) for name in COMMAND_RE.findall(content)]
    elif rec_type == "assistant" and isinstance(content, list):
        found = [
            ((block.get("input") or {}).get("skill", ""), "model", block.get("id"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill"
        ]
    for name, trigger, source_id in found:
        resolved = index.resolve(name)
        if not resolved:
            continue
        plugin, skill = resolved
        yield {
            "id": event_id(rec.get("sessionId"), source_id or rec.get("timestamp"), plugin, skill, trigger),
            "ts": rec.get("timestamp"),
            "session": rec.get("sessionId"),
            "repo": repo_name(rec.get("cwd")),
            "branch": rec.get("gitBranch") or None,
            "cc_version": rec.get("version"),
            "plugin": plugin,
            "plugin_version": index.versions.get(plugin),
            "skill": skill,
            "trigger": trigger,
        }


def scan_file(path, index, start=0, deadline=None):
    """Return (events, new_offset, status) for complete lines of `path` after byte offset `start`.

    status is "done" at the end of the file, "deadline" when `deadline` (a time.monotonic() value)
    passed first, or "partial" when the last line is still being written. For the last two,
    `new_offset` marks where to resume.
    """
    events = []
    with open(path, "rb") as fh:
        fh.seek(start)
        offset = start
        for raw in fh:
            if deadline is not None and time.monotonic() > deadline:
                return events, offset, "deadline"
            if not raw.endswith(b"\n"):
                return events, offset, "partial"
            offset += len(raw)
            if len(raw) > MAX_RECORD_BYTES or not any(marker in raw for marker in RECORD_MARKERS):
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            events.extend(events_from_record(rec, index))
    return events, offset, "done"


def transcript_files():
    """Every transcript on this machine: main sessions and their subagents."""
    projects = claude_dir() / "projects"
    return sorted(projects.glob("*/*.jsonl")) + sorted(projects.glob("*/*/subagents/*.jsonl"))


def opted_in_since(config):
    """When the dev consented, as a sub-second timestamp. `opted_in_at` is rounded down to the second,
    so it could let in a transcript written just before consent. Configs written before
    `consent_since` existed fall back to the config file's write time."""
    since = config.get("consent_since")
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return since
    return CONFIG_FILE.stat().st_mtime


def unread_transcripts(since):
    """Transcripts changed since `since` that hold bytes past their cursor.

    SessionEnd doesn't fire when Claude Code is killed or crashes, so the next SessionStart finds
    those sessions here. Files changed only before opt-in are left to `backfill`, which stays the
    dev's choice. Fully read files cost one stat and are never opened.
    """
    offsets = read_cursors()["offsets"]
    found = []
    for path in transcript_files():
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime >= since and st.st_size != offsets.get(str(path), 0):
            found.append(path)
    return found


def session_files(transcript_path):
    """The main transcript plus its subagent transcripts (<session>/subagents/*.jsonl)."""
    main = Path(transcript_path)
    files = [main] if main.is_file() else []
    sub_dir = main.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        files.extend(sorted(sub_dir.glob("*.jsonl")))
    return files


def _rescan_marker(key):
    return RESCAN_DIR / hashlib.sha1(key.encode("utf-8")).hexdigest()


def mark_for_rescan(keys):
    """Remember transcripts to rescan without taking the lock: one atomically written file each."""
    RESCAN_DIR.mkdir(parents=True, exist_ok=True)
    for key in keys:
        marker = _rescan_marker(key)
        tmp = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        tmp.write_text(key, encoding="utf-8")
        os.replace(tmp, marker)


def marked_for_rescan():
    try:
        markers = [m for m in RESCAN_DIR.iterdir() if not m.name.startswith(".")]
    except OSError:
        return []
    keys = []
    for marker in markers:
        try:
            keys.append(marker.read_text(encoding="utf-8"))
        except OSError:
            continue
    return keys


def clear_rescan_marks(keys):
    for key in keys:
        try:
            _rescan_marker(key).unlink()
        except OSError:
            pass


@contextlib.contextmanager
def state_lock(timeout_s=None):
    """Exclusive cross-process lock over the outbox and cursor file. Yields False if it timed out.

    Several Claude sessions run hooks at once. Every read-modify-write of shared state (appending to
    the outbox, renaming it to a claim, updating cursors) happens under this lock; scanning and
    sending happen outside it. Callers that don't get the lock leave state untouched and retry later.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        end = time.monotonic() + (LOCK_TIMEOUT_S if timeout_s is None else timeout_s)
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.02)
        yield acquired
    finally:
        if acquired:
            _unlock(fd)
        os.close(fd)


def read_cursors():
    """{"offsets": {path: byte offset}, "incomplete": [paths a deadline stopped mid-file]}."""
    state = read_json(CURSOR_FILE, {})
    return {"offsets": dict(state.get("offsets") or {}), "incomplete": list(state.get("incomplete") or [])}


def _append_unlocked(events):
    """Append to the outbox. The caller holds state_lock()."""
    if not events:
        return
    data = "".join(json.dumps(ev) + "\n" for ev in events).encode("utf-8")
    fd = os.open(OUTBOX_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def append_events(events):
    """Append events to the outbox. Returns False (nothing written) if the lock timed out."""
    if not events:
        return True
    with state_lock() as locked:
        if locked:
            _append_unlocked(events)
        return locked


def event_time(ts):
    """An event's ISO timestamp ("…T10:05:00.000Z") as epoch seconds, or None if unreadable.
    Python 3.9's fromisoformat doesn't accept the "Z"."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def collect(paths, index, deadline=None, since=None, from_start=False):
    """Scan `paths` from their cursors and queue new events. Returns how many were queued.

    `since` drops events stamped before it (or without a readable time). Hooks pass the consent time,
    so a transcript that was already open at opt-in only sends what came after; older history goes
    only through `backfill`, which passes `from_start` because a hook may have read past (and dropped)
    those events already.
    """
    start_offsets = {} if from_start else read_cursors()["offsets"]
    queued, progress = [], {}  # progress: path -> (new offset or None if gone, finished)
    for path in paths:
        key = str(path)
        if deadline is not None and time.monotonic() > deadline:
            # Out of time: don't open another file. Keep its cursor and list it for the next hook.
            progress[key] = (start_offsets.get(key, 0), False)
            continue
        try:
            st = path.stat()
        except OSError:
            progress[key] = (None, True)  # gone; nothing left to resume
            continue
        start = start_offsets.get(key, 0)
        if start > st.st_size:  # file was rewritten; rescan (server dedupes by id)
            start = 0
        events, offset, status = scan_file(path, index, start, deadline)
        if status == "partial" and time.time() - st.st_mtime > ABANDONED_PARTIAL_S:
            # The writer died mid-line; nothing more will come. Skip the cut-off line too, so the
            # cursor matches the file size and SessionStart stops reopening the file.
            status, offset = "done", st.st_size
        if since is not None:
            events = [ev for ev in events if (event_time(ev["ts"]) or 0) >= since]
        queued.extend(events)
        # Unfinished files (out of time, or a last line still being written) stay listed for later hooks.
        progress[key] = (offset, status == "done")
    with state_lock() as locked:
        if not CONFIG_FILE.exists():
            return 0  # opted out meanwhile: queue nothing
        if not locked:
            # Cursors stay unchanged, so a rescan finds these events again. Mark the files (no lock
            # needed) so the next hook rescans them even if this was its only reference to them.
            mark_for_rescan(key for key, (offset, _) in progress.items() if offset is not None)
            return 0
        # Re-read under the lock and change only our own paths: another hook may have saved its
        # progress on other transcripts since we started, and overwriting it would lose that.
        cursors = read_cursors()
        offsets, incomplete = cursors["offsets"], set(cursors["incomplete"])
        for key, (offset, finished) in progress.items():
            if offset is not None:
                offsets[key] = offset
            (incomplete.discard if finished else incomplete.add)(key)
        _append_unlocked(queued)
        write_json(CURSOR_FILE, {"offsets": offsets, "incomplete": sorted(incomplete)})
        clear_rescan_marks(progress)
    return len(queued)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect with the Authorization header, carrying the token to a URL
    `optin` never checked (possibly plain http elsewhere). A redirect is a failed send instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener_for(endpoint):
    """Never follows redirects. A loopback endpoint also skips proxies: plain http is only allowed
    because the request stays on this machine, and http_proxy would send it (and the token) elsewhere.
    https keeps the environment's proxies, since a corporate proxy only tunnels the encrypted bytes."""
    loopback = urllib.parse.urlsplit(endpoint).hostname in LOOPBACK_HOSTS
    proxies = urllib.request.ProxyHandler({} if loopback else None)
    return urllib.request.build_opener(_NoRedirect, proxies)


def post(endpoint, token, events):
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"events": events}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with _opener_for(endpoint).open(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read() or b"{}")


def _claim_unlocked():
    """Take ownership of everything queued. The caller holds state_lock().

    The outbox is renamed to a claim file private to this process, so events other sessions append
    meanwhile go to a fresh outbox. Claims left by a process that died mid-send are adopted once stale.
    """
    stem = f"usage-outbox.{os.getpid()}.{time.time_ns()}"
    claims = []
    try:
        claim = STATE_DIR / f"{stem}.sending"
        os.replace(OUTBOX_FILE, claim)
        os.utime(claim)  # mtime = claim time, so other processes don't adopt it as stale
        claims.append(claim)
    except OSError:
        pass  # nothing queued
    now = time.time()
    for n, orphan in enumerate(sorted(STATE_DIR.glob(CLAIM_GLOB))):
        if orphan in claims:
            continue
        try:
            if now - orphan.stat().st_mtime < STALE_CLAIM_S:
                continue  # another process is still sending it
            adopted = STATE_DIR / f"{stem}.{n}.sending"
            os.replace(orphan, adopted)  # if two processes race, only one rename succeeds
            os.utime(adopted)
            claims.append(adopted)
        except OSError:
            continue
    return claims


def read_events(paths):
    events, seen = [], set()
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
    return events


def flush(config, deadline=None):
    """Send queued events in batches; whatever isn't sent goes back to the outbox.

    With a `deadline` (time.monotonic() value), a batch is only started if its HTTP timeout still
    fits before it, so the hook never outlives its budget.
    """
    if not STATE_DIR.is_dir():
        return {"sent": 0, "pending": 0}
    with state_lock() as locked:
        claims = _claim_unlocked() if locked else []
    if not claims:
        return {"sent": 0, "pending": 0}
    events = read_events(claims)
    sent = 0
    result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": True}
    try:
        for i in range(0, len(events), BATCH_SIZE):
            if deadline is not None and time.monotonic() + HTTP_TIMEOUT_S > deadline:
                result["deferred"] = True  # out of time; the next hook sends the rest
                break
            batch = events[i:i + BATCH_SIZE]
            post(config["endpoint"], config["token"], batch)
            sent = i + len(batch)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.update(ok=False, error=str(getattr(exc, "code", "")) or type(exc).__name__)
    remaining = events[sent:]
    with state_lock() as locked:
        if locked and not CONFIG_FILE.exists():
            done = claims  # the dev opted out while we were sending: drop, don't requeue
        elif locked:
            # Requeue before deleting the claims: a crash in between duplicates, never loses.
            _append_unlocked(remaining)
            done = claims
        else:
            # Keep the unsent events in our own claim; it goes stale and the next flush adopts it.
            keep = claims[0]
            tmp = keep.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(ev) + "\n" for ev in remaining), encoding="utf-8")
            os.replace(tmp, keep)
            done = claims[1:]
        for claim in done:
            try:
                claim.unlink()
            except OSError:
                pass
    result.update(sent=sent, pending=len(remaining))
    if CONFIG_FILE.exists():  # after an opt-out mid-send, leave no state behind
        write_json(LAST_SEND_FILE, result)
    return result


def pending_count():
    return len(read_events([OUTBOX_FILE, *sorted(STATE_DIR.glob(CLAIM_GLOB))])) if STATE_DIR.is_dir() else 0


def load_config():
    config = read_json(CONFIG_FILE, None)
    if not config or not config.get("endpoint") or not config.get("token"):
        return None
    return config


def cmd_hook(_args):
    # A telemetry hook must never block or break a session: swallow everything, always exit 0.
    try:
        config = load_config()
        if not config:
            return 0
        deadline = time.monotonic() + HOOK_BUDGET_S
        payload = json.load(sys.stdin)
        event = payload.get("hook_event_name")
        paths = []
        if event == "SessionEnd" and payload.get("transcript_path"):
            paths = session_files(payload["transcript_path"])
        # Also resume files an earlier hook ran out of time on, or couldn't save because of the lock.
        pending = [Path(key) for key in [*read_cursors()["incomplete"], *marked_for_rescan()]]
        if event == "SessionStart":
            pending += unread_transcripts(opted_in_since(config))
        seen = set(paths)
        for path in pending:
            if path not in seen:
                seen.add(path)
                paths.append(path)
        if paths:
            # Leave room in the budget for at least one send.
            collect(paths, PluginIndex(), deadline - HTTP_TIMEOUT_S - 1, since=opted_in_since(config))
        flush(config, deadline)
    except Exception:  # noqa: BLE001 - see comment above
        pass
    return 0


def endpoint_allowed(url):
    """https anywhere; plain http only to this machine (the local Docker backend), so a token
    never crosses a network in clear text."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in LOOPBACK_HOSTS and "@" not in parts.netloc


def cmd_optin(args):
    if not endpoint_allowed(args.endpoint):
        print("The endpoint must be an https:// URL (http:// only for localhost).", file=sys.stderr)
        return 2
    token = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Ingest token: ").strip()
    if len(token) < 32:
        print("That does not look like an ingest token (expected 32+ characters).", file=sys.stderr)
        return 2
    # Re-running optin to change the token or endpoint keeps the original consent time, so SessionStart
    # still catches up on sessions killed since then. optout deletes the file, and with it that time.
    previous = load_config()
    now = time.time()
    consent_since = opted_in_since(previous) if previous else now
    opted_in_at = ((previous or {}).get("opted_in_at")
                   or datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"))
    write_json(CONFIG_FILE, {"endpoint": args.endpoint, "token": token, "opted_in_at": opted_in_at,
                             "consent_since": consent_since}, private=True)
    print(f"Opted in. Config written to {CONFIG_FILE} (mode 600).")
    return 0


def cmd_optout(_args):
    # Under the lock, so no hook is mid-append or mid-claim. A hook already sending re-checks the
    # config under the lock afterwards and drops its events. The lock file stays: deleting it would
    # let two processes lock different inodes. Without the lock in time, delete anyway.
    with (state_lock(timeout_s=10) if STATE_DIR.is_dir() else contextlib.nullcontext(False)):
        claims = sorted(STATE_DIR.glob(CLAIM_GLOB)) if STATE_DIR.is_dir() else []
        for path in (CONFIG_FILE, OUTBOX_FILE, CURSOR_FILE, LAST_SEND_FILE, *claims):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(RESCAN_DIR, ignore_errors=True)
    print("Opted out. Local config, queue and cursors deleted. "
          "Events already sent stay on the server until an admin deletes them.")
    return 0


def cmd_backfill(args):
    config = load_config()
    if not config:
        print("Not opted in; run `optin` first.", file=sys.stderr)
        return 1
    files = transcript_files()
    if args.since:
        cutoff = datetime.fromisoformat(args.since).timestamp()
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    queued = collect(files, PluginIndex(), from_start=True)  # the server ignores events it already has
    result = flush(config)
    print(f"Scanned {len(files)} transcripts, queued {queued} events; "
          f"sent {result['sent']}, pending {result['pending']}.")
    return 0 if result.get("ok", True) else 1


def cmd_flush(_args):
    config = load_config()
    if not config:
        print("Not opted in.", file=sys.stderr)
        return 1
    result = flush(config)
    print(json.dumps(result))
    return 0 if result.get("ok", True) else 1


def cmd_status(_args):
    config = load_config()
    pending = pending_count()
    print(json.dumps({
        "opted_in": bool(config),
        "endpoint": config["endpoint"] if config else None,
        "opted_in_at": config.get("opted_in_at") if config else None,
        "pending_events": pending,
        "last_send": read_json(LAST_SEND_FILE, None),
        "axis_plugins": PluginIndex().versions,
    }, indent=2))
    return 0


def cmd_extract(args):
    index = PluginIndex()
    for path in args.files:
        events, _, _ = scan_file(path, index)
        for ev in events:
            print(json.dumps(ev))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="creai-axis usage collector (opt-in)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook").set_defaults(fn=cmd_hook)
    p = sub.add_parser("optin")
    p.add_argument("--endpoint", required=True)
    p.set_defaults(fn=cmd_optin)
    sub.add_parser("optout").set_defaults(fn=cmd_optout)
    p = sub.add_parser("backfill")
    p.add_argument("--since", help="Only transcripts modified on/after this ISO date")
    p.set_defaults(fn=cmd_backfill)
    sub.add_parser("flush").set_defaults(fn=cmd_flush)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("extract")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(fn=cmd_extract)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
