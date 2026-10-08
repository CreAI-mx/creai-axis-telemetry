"""Tests for scripts/axis_admin.py (with a stand-in psql, so no database is needed) and the\nsmoke test's HTTP rules."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import axis_admin  # noqa: E402
import smoke_ingest  # noqa: E402

FAKE_PSQL = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_PSQL_LOG"], "a") as log:
    peek = os.environ.get("FAKE_PSQL_PEEK")
    on_disk = open(peek).read() if peek and os.path.exists(peek) else None
    log.write(json.dumps({"argv": sys.argv[1:], "password": os.environ.get("PGPASSWORD"), "on_disk": on_disk}) + "\\n")
sys.stdin.read()
sys.exit(int(os.environ.get("FAKE_PSQL_RC", "0")))
"""


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        psql = bin_dir / "psql"
        psql.write_text(FAKE_PSQL)
        psql.chmod(0o755)
        self.log = self.tmp / "psql.log"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_PSQL_LOG": str(self.log),
               "AXIS_DB_URL": "postgresql://admin:p%40ss:w%2Frd@db.example.com:5432/postgres"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def issue(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            axis_admin.main(["issue", "dev@creai.mx", "Dev Name", *extra])
        return out.getvalue()

    def psql_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_out_creates_a_new_owner_only_file_with_the_token(self):
        target = self.tmp / "token"
        self.issue("--out", str(target))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertRegex(target.read_text(), r"^[A-Za-z0-9_-]{43}\n$")
        self.assertEqual(len(self.psql_calls()), 1)

    def test_out_refuses_an_existing_file_and_leaves_it_and_the_database_alone(self):
        target = self.tmp / "token"
        target.write_text("old\n")
        target.chmod(0o644)
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()):
            self.issue("--out", str(target))
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(self.psql_calls(), [])

    def test_out_refuses_a_planted_link(self):
        victim = self.tmp / "victim"
        victim.write_text("keep\n")
        link = self.tmp / "token"
        link.symlink_to(victim)
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            self.issue("--out", str(link))
        self.assertEqual(victim.read_text(), "keep\n")
        self.assertEqual(self.psql_calls(), [])

    def test_a_failed_database_write_keeps_the_token_file(self):
        # psql can fail after the rotation committed, so the file may hold the only working token.
        target = self.tmp / "token"
        with mock.patch.dict(os.environ, {"FAKE_PSQL_RC": "1"}), self.assertRaises(SystemExit) as caught:
            self.issue("--out", str(target))
        self.assertIn("outcome is unknown", str(caught.exception))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertRegex(target.read_text(), r"^[A-Za-z0-9_-]{43}\n$")

    def test_the_database_password_goes_through_the_environment_not_argv(self):
        self.issue("--out", str(self.tmp / "token"))
        (call,) = self.psql_calls()
        self.assertEqual(call["password"], "p@ss:w/rd")
        self.assertNotIn("p%40ss", " ".join(call["argv"]))
        self.assertNotIn("p@ss", " ".join(call["argv"]))
        self.assertIn("postgresql://admin@db.example.com:5432/postgres", call["argv"])


    def test_a_password_in_the_query_string_also_stays_out_of_argv(self):
        url = "postgresql://admin@db.example.com/postgres?sslmode=require&password=se%26cr%3Det&application_name=a%20b"
        with mock.patch.dict(os.environ, {"AXIS_DB_URL": url}):
            self.issue("--out", str(self.tmp / "token"))
        (call,) = self.psql_calls()
        self.assertEqual(call["password"], "se&cr=et")
        self.assertNotIn("se%26cr", " ".join(call["argv"]))
        self.assertIn("postgresql://admin@db.example.com/postgres?sslmode=require&application_name=a%20b", call["argv"])

    def test_urls_without_a_password_pass_through_unchanged(self):
        for url in ("postgresql://admin@db.example.com/postgres?sslmode=require", "postgresql:///postgres"):
            self.assertEqual(axis_admin.split_password(url), (url, None))

    def test_the_token_is_on_disk_before_the_database_rotates_it(self):
        target = self.tmp / "token"
        with mock.patch.dict(os.environ, {"FAKE_PSQL_PEEK": str(target)}):
            self.issue("--out", str(target))
        (call,) = self.psql_calls()
        self.assertEqual(call["on_disk"], target.read_text())
        self.assertRegex(call["on_disk"], r"^[A-Za-z0-9_-]{43}\n$")

    def test_a_failed_token_write_leaves_the_database_alone(self):
        target = self.tmp / "token"
        with mock.patch("os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.issue("--out", str(target))
        self.assertFalse(target.exists())
        self.assertEqual(self.psql_calls(), [])


class Recorder(BaseHTTPRequestHandler):
    """A local server that records what reaches it; `reply` sets the status and extra headers."""
    def do_POST(self):
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, headers = type(self).reply
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

    def log_message(self, *args):
        pass


class SmokeHttpTest(unittest.TestCase):
    def serve(self, reply):
        handler = type("Handler", (Recorder,), {"hits": [], "reply": reply})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", handler

    def test_loopback_requests_skip_proxies_and_never_follow_redirects(self):
        proxy_url, proxy = self.serve((200, {}))
        elsewhere, target = self.serve((200, {}))
        endpoint, sink = self.serve((302, {"Location": elsewhere + "/stolen"}))
        env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "no_proxy": "", "NO_PROXY": ""}
        with mock.patch.dict(os.environ, env):
            status, _ = smoke_ingest.post(endpoint + "/ingest", "tok", {"events": []})
            http_status, _ = smoke_ingest.http("GET", endpoint + "/rest", headers={"Authorization": "Bearer tok"})
        self.assertEqual((status, http_status), (302, 302))
        self.assertEqual(sink.hits, [("/ingest", "Bearer tok"), ("/rest", "Bearer tok")])
        self.assertEqual(proxy.hits, [])   # the token never went to the proxy
        self.assertEqual(target.hits, [])  # nor to the redirect target

    def test_https_requests_keep_the_environment_proxies(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.example:3128"}), \
             mock.patch.object(urllib.request.OpenerDirector, "open", lambda self, *a, **k: self):
            opener = smoke_ingest.urlopen(urllib.request.Request("https://ingest.example/x"))
        proxies = [h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxies[0].get("https"), "http://proxy.example:3128")

if __name__ == "__main__":
    unittest.main()
