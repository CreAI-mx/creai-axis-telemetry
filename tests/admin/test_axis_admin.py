"""Tests for scripts/axis_admin.py, with a stand-in psql so no database is needed."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import axis_admin  # noqa: E402

FAKE_PSQL = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_PSQL_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "password": os.environ.get("PGPASSWORD")}) + "\\n")
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

    def test_a_failed_database_write_leaves_no_token_file(self):
        target = self.tmp / "token"
        with mock.patch.dict(os.environ, {"FAKE_PSQL_RC": "1"}), self.assertRaises(SystemExit):
            self.issue("--out", str(target))
        self.assertFalse(target.exists())

    def test_the_database_password_goes_through_the_environment_not_argv(self):
        self.issue("--out", str(self.tmp / "token"))
        (call,) = self.psql_calls()
        self.assertEqual(call["password"], "p@ss:w/rd")
        self.assertNotIn("p%40ss", " ".join(call["argv"]))
        self.assertNotIn("p@ss", " ".join(call["argv"]))
        self.assertIn("postgresql://admin@db.example.com:5432/postgres", call["argv"])


if __name__ == "__main__":
    unittest.main()
