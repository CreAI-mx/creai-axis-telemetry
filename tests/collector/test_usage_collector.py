"""Tests for plugins/creai-telemetry/hooks/usage-collector.py (stdlib only: python3 -m unittest discover -s tests/collector)."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins" / "creai-telemetry" / "hooks" / "usage-collector.py"
TOKEN = "t" * 40


def load_collector(state_dir, claude_dir):
    os.environ["CREAI_AXIS_USAGE_DIR"] = str(state_dir)
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    spec = importlib.util.spec_from_file_location("usage_collector", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rec_user(uuid, text, branch="feature/DAIL-1-x"):
    return {"type": "user", "uuid": uuid, "timestamp": "2026-10-07T10:00:00.000Z", "sessionId": "s1",
            "cwd": "/home/dev/agrizar", "gitBranch": branch, "version": "2.1.290",
            "message": {"role": "user", "content": text}}


def rec_skill(tool_id, skill):
    return {"type": "assistant", "uuid": "a-" + tool_id, "timestamp": "2026-10-07T10:05:00.000Z",
            "sessionId": "s1", "cwd": "/home/dev/agrizar", "gitBranch": "feature/DAIL-1-x",
            "version": "2.1.290",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "secret prompt-adjacent text"},
                {"type": "tool_use", "id": tool_id, "name": "Skill", "input": {"skill": skill, "args": "SECRET-ARGS"}},
            ]}}


class Sink(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Sink.received.append((self.headers["Authorization"], body))
        self.send_response(Sink.status)
        self.end_headers()
        self.wfile.write(b'{"accepted": 1}')

    def log_message(self, *args):
        pass


class CollectorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.claude = self.tmp / "claude"
        install = self.claude / "plugins" / "cache" / "creai-axis" / "creai-common" / "0.15.0"
        for skill in ("creai-implement", "creai-create-pr"):
            (install / "skills" / skill).mkdir(parents=True)
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "creai-common@creai-axis": [{"installPath": str(install), "version": "0.15.0"}],
            "swebok@swebok-skills": [{"installPath": "/nowhere", "version": "0.1.0"}],
        }}))
        self.mod = load_collector(self.tmp / "state", self.claude)
        self.transcript = self.claude / "projects" / "-home-dev-agrizar" / "s1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        Sink.received, Sink.status = [], 200

    def write_records(self, *records, partial=None):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
            if partial:
                fh.write(partial)

    def extract(self):
        events, _ = self.mod.scan_file(self.transcript, self.mod.PluginIndex())
        return events

    def test_slash_full_name_and_bare_name_resolve_to_plugin(self):
        self.write_records(
            rec_user("u1", "<command-name>/creai-common:creai-implement</command-name> args"),
            rec_user("u2", "<command-name>/creai-create-pr</command-name>"),
        )
        events = self.extract()
        self.assertEqual([(e["plugin"], e["skill"], e["trigger"]) for e in events],
                         [("creai-common", "creai-implement", "slash"), ("creai-common", "creai-create-pr", "slash")])
        self.assertEqual(events[0]["plugin_version"], "0.15.0")
        self.assertEqual(events[0]["repo"], "agrizar")

    def test_model_skill_call_captured_without_arguments_or_text(self):
        self.write_records(rec_skill("toolu_1", "creai-common:creai-implement"))
        events = self.extract()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trigger"], "model")
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertNotIn("prompt-adjacent", json.dumps(events))

    def test_non_axis_and_builtin_commands_are_ignored(self):
        self.write_records(
            rec_skill("toolu_2", "swebok:swebok-arch-review"),
            rec_user("u3", "<command-name>/clear</command-name>"),
            rec_user("u4", "please run creai-implement for me"),
        )
        self.assertEqual(self.extract(), [])

    def test_ids_are_stable_and_distinct(self):
        self.write_records(rec_skill("toolu_a", "creai-implement"), rec_skill("toolu_b", "creai-implement"))
        first = [e["id"] for e in self.extract()]
        self.assertEqual(len(set(first)), 2)
        self.assertEqual(first, [e["id"] for e in self.extract()])

    def test_cursor_skips_processed_lines_and_waits_for_partial_line(self):
        self.write_records(rec_skill("toolu_1", "creai-implement"), partial='{"type": "assist')
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 1)
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 0)
        # complete the partial line as a different, valid record
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write('ant"}\n')
        self.write_records(rec_user("u9", "<command-name>/creai-create-pr</command-name>"))
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 1)

    def run_hook(self, payload):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return self.mod.main(["hook"])

    def test_hook_is_a_noop_until_opted_in(self):
        self.write_records(rec_skill("toolu_1", "creai-implement"))
        self.assertEqual(self.run_hook({"hook_event_name": "SessionEnd", "transcript_path": str(self.transcript)}), 0)
        self.assertFalse((self.tmp / "state").exists())

    def test_hook_never_fails_on_garbage_input(self):
        self.mod.write_json(self.mod.CONFIG_FILE, {"endpoint": "https://127.0.0.1:9", "token": TOKEN})
        with mock.patch.object(sys, "stdin", io.StringIO("not json")):
            self.assertEqual(self.mod.main(["hook"]), 0)

    def start_sink(self):
        server = HTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/ingest"

    def test_session_end_sends_main_and_subagent_events_then_empties_outbox(self):
        endpoint = self.start_sink()
        self.mod.write_json(self.mod.CONFIG_FILE, {"endpoint": endpoint, "token": TOKEN})
        self.write_records(rec_user("u1", "<command-name>/creai-common:creai-implement</command-name>"))
        sub = self.transcript.with_suffix("") / "subagents" / "agent-x.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps(rec_skill("toolu_sub", "creai-create-pr")) + "\n")

        self.assertEqual(self.run_hook({"hook_event_name": "SessionEnd", "transcript_path": str(self.transcript)}), 0)

        self.assertEqual(len(Sink.received), 1)
        auth, body = Sink.received[0]
        self.assertEqual(auth, f"Bearer {TOKEN}")
        self.assertEqual(sorted(e["skill"] for e in body["events"]), ["creai-create-pr", "creai-implement"])
        self.assertEqual(self.mod.OUTBOX_FILE.read_text(), "")

    def test_failed_send_keeps_events_queued_for_next_session(self):
        endpoint = self.start_sink()
        Sink.status = 500
        self.mod.write_json(self.mod.CONFIG_FILE, {"endpoint": endpoint, "token": TOKEN})
        self.write_records(rec_skill("toolu_1", "creai-implement"))
        self.run_hook({"hook_event_name": "SessionEnd", "transcript_path": str(self.transcript)})
        self.assertEqual(len(self.mod.OUTBOX_FILE.read_text().splitlines()), 1)
        self.assertFalse(self.mod.read_json(self.mod.LAST_SEND_FILE, {})["ok"])

        Sink.status = 200
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual(self.mod.OUTBOX_FILE.read_text(), "")
        self.assertEqual(len(Sink.received[-1][1]["events"]), 1)

    def test_optin_rejects_http_and_stores_token_privately(self):
        with mock.patch.object(sys, "stdin", io.StringIO(TOKEN + "\n")):
            self.assertEqual(self.mod.main(["optin", "--endpoint", "http://insecure"]), 2)
        with mock.patch.object(sys, "stdin", io.StringIO(TOKEN + "\n")):
            self.assertEqual(self.mod.main(["optin", "--endpoint", "https://x.supabase.co/functions/v1/ingest"]), 0)
        self.assertEqual(self.mod.CONFIG_FILE.stat().st_mode & 0o777, 0o600)
        self.mod.main(["optout"])
        self.assertFalse(self.mod.CONFIG_FILE.exists())


if __name__ == "__main__":
    unittest.main()
