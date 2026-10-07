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
import functools
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MARKETPLACE = "creai-axis"
FALLBACK_PLUGINS = {"creai-common", "creai-backend", "creai-frontend", "creai-data", "creai-arch"}
COMMAND_RE = re.compile(r"<command-name>/?([a-z0-9-]+(?::[a-z0-9-]+)?)</command-name>")
BATCH_SIZE = 500
HTTP_TIMEOUT_S = 5
# Claude Code kills the hook at 10 s (hooks.json). Stop scanning and sending well before that and
# leave the rest for the next hook: unscanned files stay listed as incomplete, unsent events stay queued.
HOOK_BUDGET_S = 8
STALE_CLAIM_S = 600  # a claimed outbox older than this belongs to a process that died mid-send

STATE_DIR = Path(os.environ.get("CREAI_AXIS_USAGE_DIR") or Path.home() / ".config" / "creai-axis")
CONFIG_FILE = STATE_DIR / "usage.json"
OUTBOX_FILE = STATE_DIR / "usage-outbox.jsonl"
CLAIM_GLOB = "usage-outbox.*.sending"  # flush renames the outbox to one of these before sending it
CURSOR_FILE = STATE_DIR / "usage-cursors.json"
LAST_SEND_FILE = STATE_DIR / "usage-last-send.json"


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
        if candidate == home:
            break  # don't climb into home: a dotfiles repo there would name every session after the user
        try:
            if (candidate / ".git").exists():
                project = candidate
                break
        except OSError:
            break
    if project == home or not project.name or project.name == _os_username():
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
    """Return (events, new_offset, finished) for complete lines of `path` after byte offset `start`.

    `finished` is False when `deadline` (a time.monotonic() value) passed before the end of the file;
    `new_offset` then marks where to resume.
    """
    events = []
    with open(path, "rb") as fh:
        fh.seek(start)
        offset = start
        for raw in fh:
            if deadline is not None and time.monotonic() > deadline:
                return events, offset, False
            if not raw.endswith(b"\n"):
                break  # partial line still being written; pick it up next time
            offset += len(raw)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            events.extend(events_from_record(rec, index))
    return events, offset, True


def session_files(transcript_path):
    """The main transcript plus its subagent transcripts (<session>/subagents/*.jsonl)."""
    main = Path(transcript_path)
    files = [main] if main.is_file() else []
    sub_dir = main.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        files.extend(sorted(sub_dir.glob("*.jsonl")))
    return files


def read_cursors():
    """{"offsets": {path: byte offset}, "incomplete": [paths a deadline stopped mid-file]}."""
    state = read_json(CURSOR_FILE, {})
    return {"offsets": dict(state.get("offsets") or {}), "incomplete": list(state.get("incomplete") or [])}


def append_events(events):
    """Append events to the outbox in one O_APPEND write, so concurrent hooks never interleave or overwrite."""
    if not events:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(ev) + "\n" for ev in events).encode("utf-8")
    fd = os.open(OUTBOX_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def collect(paths, index, deadline=None):
    cursors = read_cursors()
    offsets, incomplete = cursors["offsets"], set(cursors["incomplete"])
    queued = []
    for path in paths:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            incomplete.discard(key)  # gone; nothing left to resume
            continue
        start = offsets.get(key, 0)
        if start > size:  # file was rewritten; rescan (server dedupes by id)
            start = 0
        events, offsets[key], finished = scan_file(path, index, start, deadline)
        queued.extend(events)
        (incomplete.discard if finished else incomplete.add)(key)
    append_events(queued)
    write_json(CURSOR_FILE, {"offsets": offsets, "incomplete": sorted(incomplete)})
    return len(queued)


def post(endpoint, token, events):
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"events": events}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read() or b"{}")


def claim_outbox():
    """Take ownership of everything queued, without blocking other hooks.

    The outbox is renamed to a claim file private to this process, so events other sessions append
    meanwhile go to a fresh outbox and can't be overwritten. Claims left by a process that died
    mid-send are adopted once they are stale.
    """
    if not STATE_DIR.is_dir():
        return []
    stem = f"usage-outbox.{os.getpid()}.{time.time_ns()}"
    claims = []
    try:
        claim = STATE_DIR / f"{stem}.sending"
        os.replace(OUTBOX_FILE, claim)
        os.utime(claim)  # mtime = claim time, so other processes don't adopt it as stale
        claims.append(claim)
    except OSError:
        pass  # nothing queued, or (Windows) another process has the outbox open; next hook retries
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
    claims = claim_outbox()
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
    append_events(remaining)  # requeue before deleting the claims: a crash in between duplicates, never loses
    for claim in claims:
        try:
            claim.unlink()
        except OSError:
            pass
    result.update(sent=sent, pending=len(remaining))
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
        paths = []
        if payload.get("hook_event_name") == "SessionEnd" and payload.get("transcript_path"):
            paths = session_files(payload["transcript_path"])
        # Also resume files an earlier hook ran out of time on.
        paths += [Path(p) for p in read_cursors()["incomplete"] if Path(p) not in paths]
        if paths:
            # Leave room in the budget for at least one send.
            collect(paths, PluginIndex(), deadline - HTTP_TIMEOUT_S - 1)
        flush(config, deadline)
    except Exception:  # noqa: BLE001 - see comment above
        pass
    return 0


def cmd_optin(args):
    if not args.endpoint.startswith("https://"):
        print("The endpoint must be an https:// URL.", file=sys.stderr)
        return 2
    token = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Ingest token: ").strip()
    if len(token) < 32:
        print("That does not look like an ingest token (expected 32+ characters).", file=sys.stderr)
        return 2
    write_json(CONFIG_FILE, {"endpoint": args.endpoint, "token": token,
                             "opted_in_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
               private=True)
    print(f"Opted in. Config written to {CONFIG_FILE} (mode 600).")
    return 0


def cmd_optout(_args):
    claims = sorted(STATE_DIR.glob(CLAIM_GLOB)) if STATE_DIR.is_dir() else []
    for path in (CONFIG_FILE, OUTBOX_FILE, CURSOR_FILE, LAST_SEND_FILE, *claims):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print("Opted out. Local config, queue and cursors deleted. "
          "Events already sent stay on the server until an admin deletes them.")
    return 0


def cmd_backfill(args):
    config = load_config()
    if not config:
        print("Not opted in; run `optin` first.", file=sys.stderr)
        return 1
    files = sorted((claude_dir() / "projects").glob("*/*.jsonl"))
    files += sorted((claude_dir() / "projects").glob("*/*/subagents/*.jsonl"))
    if args.since:
        cutoff = datetime.fromisoformat(args.since).timestamp()
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    queued = collect(files, PluginIndex())
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
