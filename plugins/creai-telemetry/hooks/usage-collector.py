#!/usr/bin/env python3
"""creai-axis usage collector (opt-in).

Reads Claude Code session transcripts on this machine, extracts one metadata-only event per
creai-axis skill invocation, and sends them to the team's ingest endpoint.

Nothing runs until the developer opts in (`optin`), which writes the config file below.
Only metadata leaves the machine: timestamp, session id, repo basename, branch, Claude Code
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
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MARKETPLACE = "creai-axis"
FALLBACK_PLUGINS = {"creai-common", "creai-backend", "creai-frontend", "creai-data", "creai-arch"}
COMMAND_RE = re.compile(r"<command-name>/?([a-z0-9-]+(?::[a-z0-9-]+)?)</command-name>")
BATCH_SIZE = 500
HTTP_TIMEOUT_S = 5

STATE_DIR = Path(os.environ.get("CREAI_AXIS_USAGE_DIR") or Path.home() / ".config" / "creai-axis")
CONFIG_FILE = STATE_DIR / "usage.json"
OUTBOX_FILE = STATE_DIR / "usage-outbox.jsonl"
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
            "repo": os.path.basename(rec.get("cwd") or "") or None,
            "branch": rec.get("gitBranch") or None,
            "cc_version": rec.get("version"),
            "plugin": plugin,
            "plugin_version": index.versions.get(plugin),
            "skill": skill,
            "trigger": trigger,
        }


def scan_file(path, index, start=0):
    """Return (events, new_offset) for complete lines of `path` after byte offset `start`."""
    events = []
    with open(path, "rb") as fh:
        fh.seek(start)
        offset = start
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial line still being written; pick it up next time
            offset += len(raw)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            events.extend(events_from_record(rec, index))
    return events, offset


def session_files(transcript_path):
    """The main transcript plus its subagent transcripts (<session>/subagents/*.jsonl)."""
    main = Path(transcript_path)
    files = [main] if main.is_file() else []
    sub_dir = main.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        files.extend(sorted(sub_dir.glob("*.jsonl")))
    return files


def collect(paths, index):
    cursors = read_json(CURSOR_FILE, {})
    queued = []
    for path in paths:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = cursors.get(key, 0)
        if start > size:  # file was rewritten; rescan (server dedupes by id)
            start = 0
        events, cursors[key] = scan_file(path, index, start)
        queued.extend(events)
    if queued:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTBOX_FILE, "a", encoding="utf-8") as fh:
            for ev in queued:
                fh.write(json.dumps(ev) + "\n")
    write_json(CURSOR_FILE, cursors)
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


def flush(config):
    try:
        lines = OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"sent": 0, "pending": 0}
    events, seen = [], set()
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("id") not in seen:
            seen.add(ev.get("id"))
            events.append(ev)
    sent = 0
    result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        for i in range(0, len(events), BATCH_SIZE):
            post(config["endpoint"], config["token"], events[i:i + BATCH_SIZE])
            sent = i + len(events[i:i + BATCH_SIZE])
        result["ok"] = True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.update(ok=False, error=str(getattr(exc, "code", "")) or type(exc).__name__)
    remaining = events[sent:]
    tmp = OUTBOX_FILE.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(ev) + "\n" for ev in remaining), encoding="utf-8")
    os.replace(tmp, OUTBOX_FILE)
    result.update(sent=sent, pending=len(remaining))
    write_json(LAST_SEND_FILE, result)
    return result


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
        payload = json.load(sys.stdin)
        if payload.get("hook_event_name") == "SessionEnd" and payload.get("transcript_path"):
            collect(session_files(payload["transcript_path"]), PluginIndex())
        flush(config)
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
    for path in (CONFIG_FILE, OUTBOX_FILE, CURSOR_FILE, LAST_SEND_FILE):
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
    try:
        pending = sum(1 for _ in open(OUTBOX_FILE, encoding="utf-8"))
    except OSError:
        pending = 0
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
        events, _ = scan_file(path, index)
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
