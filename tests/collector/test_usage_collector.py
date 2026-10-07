"""Tests for plugins/creai-telemetry/hooks/usage-collector.py (stdlib only: python3 -m unittest discover -s tests/collector)."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
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

    def opt_in(self, endpoint="https://x"):
        self.mod.write_json(self.mod.CONFIG_FILE, {"endpoint": endpoint, "token": TOKEN})

    def extract(self):
        events, _, _ = self.mod.scan_file(self.transcript, self.mod.PluginIndex())
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
        self.opt_in()
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
        self.assertEqual(self.mod.pending_count(), 0)

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
        self.assertEqual(self.mod.pending_count(), 0)
        self.assertEqual(len(Sink.received[-1][1]["events"]), 1)

    # --- repo name never identifies the OS user ---

    def test_repo_is_the_git_root_even_from_a_subfolder(self):
        repo = self.tmp / "code" / "agrizar"
        (repo / ".git").mkdir(parents=True)
        (repo / "docs" / "specs").mkdir(parents=True)
        self.assertEqual(self.mod.repo_name(str(repo / "docs" / "specs")), "agrizar")

    def test_repo_falls_back_to_the_folder_outside_git(self):
        folder = self.tmp / "code" / "notes"
        folder.mkdir(parents=True)
        self.assertEqual(self.mod.repo_name(str(folder)), "notes")

    def test_repo_is_never_the_home_folder_or_named_like_the_user(self):
        home = self.tmp / "home" / "jdoe"
        (home / ".git").mkdir(parents=True)  # a dotfiles repo in home
        (home / "notes").mkdir()
        (self.tmp / "other" / "jdoe").mkdir(parents=True)
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(self.mod.getpass, "getuser", return_value="jdoe"):
            self.mod.repo_name.cache_clear()
            self.assertIsNone(self.mod.repo_name(str(home)))
            self.assertEqual(self.mod.repo_name(str(home / "notes")), "notes")  # not climbed up into home
            self.assertIsNone(self.mod.repo_name(str(self.tmp / "other" / "jdoe")))
            # Case-insensitive filesystems: the same folders spelled in another case are still the user's.
            self.assertIsNone(self.mod.repo_name(str(self.tmp / "home" / "JDOE")))
            self.assertIsNone(self.mod.repo_name(str(self.tmp / "other" / "JDoe")))
        self.mod.repo_name.cache_clear()

    # --- the hook stays inside its time budget ---

    def test_scan_stops_at_the_deadline_and_the_next_hook_resumes(self):
        endpoint = self.start_sink()
        self.mod.write_json(self.mod.CONFIG_FILE, {"endpoint": endpoint, "token": TOKEN})
        self.write_records(rec_skill("toolu_1", "creai-implement"))

        events, offset, finished = self.mod.scan_file(self.transcript, self.mod.PluginIndex(), 0, time.monotonic() - 1)
        self.assertEqual((events, offset, finished), ([], 0, "deadline"))
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex(), time.monotonic() - 1), 0)
        self.assertEqual(self.mod.read_cursors()["incomplete"], [str(self.transcript)])

        # A later session's hook (no transcript of its own) picks the file back up and sends it.
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual([e["skill"] for e in Sink.received[-1][1]["events"]], ["creai-implement"])
        self.assertEqual(self.mod.read_cursors()["incomplete"], [])

    def test_flush_does_not_start_a_send_it_cannot_finish_in_time(self):
        self.opt_in()
        self.mod.append_events([{"id": "e1"}, {"id": "e2"}])
        with mock.patch.object(self.mod, "post") as post:
            result = self.mod.flush({"endpoint": "https://x", "token": TOKEN}, deadline=time.monotonic() + 1)
        post.assert_not_called()
        self.assertTrue(result["deferred"])
        self.assertEqual((result["sent"], result["pending"]), (0, 2))
        self.assertEqual(self.mod.pending_count(), 2)

    # --- concurrent hooks never lose events ---

    def test_events_queued_by_another_session_during_a_send_are_kept(self):
        self.mod.append_events([{"id": "mine"}])

        def post_while_another_hook_appends(_endpoint, _token, batch):
            self.mod.append_events([{"id": "theirs"}])  # another session's hook, mid-send
            return {"accepted": len(batch)}

        with mock.patch.object(self.mod, "post", side_effect=post_while_another_hook_appends) as post:
            self.mod.flush({"endpoint": "https://x", "token": TOKEN})
        self.assertEqual([e["id"] for e in post.call_args[0][2]], ["mine"])
        self.assertEqual([e["id"] for e in self.mod.read_events([self.mod.OUTBOX_FILE])], ["theirs"])

    def test_stale_claims_are_adopted_and_live_ones_left_alone(self):
        state = self.tmp / "state"
        state.mkdir()
        stale = state / "usage-outbox.111.1.sending"
        stale.write_text(json.dumps({"id": "orphaned"}) + "\n")
        old = time.time() - self.mod.STALE_CLAIM_S - 60
        os.utime(stale, (old, old))
        live = state / "usage-outbox.222.2.sending"
        live.write_text(json.dumps({"id": "in-flight"}) + "\n")

        with mock.patch.object(self.mod, "post") as post:
            self.mod.flush({"endpoint": "https://x", "token": TOKEN})
        self.assertEqual([e["id"] for e in post.call_args[0][2]], ["orphaned"])
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertEqual(self.mod.pending_count(), 1)  # the live claim still counts as pending

        self.mod.main(["optout"])
        self.assertFalse(live.exists())

    def test_while_another_hook_holds_the_lock_nothing_shared_is_touched(self):
        self.opt_in()
        self.write_records(rec_skill("toolu_1", "creai-implement"))
        self.mod.append_events([{"id": "queued"}])
        self.mod.LOCK_TIMEOUT_S = 0.1
        with self.mod.state_lock() as locked:  # another hook mid-append or mid-claim
            self.assertTrue(locked)
            with mock.patch.object(self.mod, "post") as post:
                self.assertEqual(self.mod.flush({"endpoint": "https://x", "token": TOKEN}), {"sent": 0, "pending": 0})
            post.assert_not_called()
            self.assertTrue(self.mod.OUTBOX_FILE.exists())  # not renamed under the other hook
            self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 0)
            self.assertEqual(self.mod.read_cursors()["offsets"], {})  # so the next hook rescans
        # Once the lock is free, the same calls go through and nothing was lost.
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 1)
        self.assertEqual(self.mod.pending_count(), 2)

    def test_concurrent_hooks_keep_each_others_cursor_progress(self):
        self.opt_in()
        other = self.transcript.with_name("s2.jsonl")
        other.write_text(json.dumps(rec_skill("toolu_2", "creai-create-pr")) + "\n")
        self.write_records(rec_skill("toolu_1", "creai-implement"))
        real_scan = self.mod.scan_file
        interleaved = []

        def scan_then_let_another_hook_finish(path, index, start=0, deadline=None):
            result = real_scan(path, index, start, deadline)
            if not interleaved:  # another session's hook saves its progress while we are scanning
                interleaved.append(True)
                self.mod.collect([other], index)
            return result

        with mock.patch.object(self.mod, "scan_file", side_effect=scan_then_let_another_hook_finish):
            # This hook runs out of time on its own transcript, so it must stay listed as incomplete.
            self.mod.collect([self.transcript], self.mod.PluginIndex(), time.monotonic() - 1)
        cursors = self.mod.read_cursors()
        self.assertIn(str(other), cursors["offsets"])  # the other hook's progress survived
        self.assertEqual(cursors["incomplete"], [str(self.transcript)])  # and so did ours

    def test_a_line_still_being_written_is_resumed_by_a_later_hook(self):
        endpoint = self.start_sink()
        self.opt_in(endpoint)
        line = json.dumps(rec_skill("toolu_1", "creai-implement"))
        self.write_records(partial=line[:20])  # SessionEnd fires before the last line is flushed
        self.run_hook({"hook_event_name": "SessionEnd", "transcript_path": str(self.transcript)})
        self.assertEqual(self.mod.read_cursors()["incomplete"], [str(self.transcript)])

        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(line[20:] + "\n")
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual([e["skill"] for e in Sink.received[-1][1]["events"]], ["creai-implement"])
        self.assertEqual(self.mod.read_cursors()["incomplete"], [])

    def test_a_line_cut_off_for_a_day_is_given_up(self):
        self.opt_in()
        self.write_records(rec_skill("toolu_1", "creai-implement"), partial='{"type": "assist')
        old = time.time() - self.mod.ABANDONED_PARTIAL_S - 60
        os.utime(self.transcript, (old, old))
        self.assertEqual(self.mod.collect([self.transcript], self.mod.PluginIndex()), 1)
        self.assertEqual(self.mod.read_cursors()["incomplete"], [])

    def test_opting_out_mid_send_leaves_nothing_queued(self):
        self.opt_in()
        self.mod.append_events([{"id": "e1"}])

        def optout_then_fail(_endpoint, _token, _batch):
            self.mod.main(["optout"])  # the dev opts out while this hook is sending
            raise self.mod.urllib.error.URLError("offline")

        with mock.patch.object(self.mod, "post", side_effect=optout_then_fail):
            self.mod.flush({"endpoint": "https://x", "token": TOKEN})
        self.assertEqual(self.mod.pending_count(), 0)  # not requeued after opt-out
        self.assertEqual([p.name for p in (self.tmp / "state").iterdir()], ["usage.lock"])  # only the empty lock file

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
