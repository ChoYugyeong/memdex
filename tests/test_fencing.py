"""Claims as leases: a fencing token per grant, and the content the grant was taken against.

The token makes a grant distinguishable from the one before it. The sha makes a stale edit visible: if the
file moved under your own exclusive lock, the edit is refused instead of overwriting whatever changed it,
and `claim --refresh` is how you prove you re-read it.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memdex.cli import main
from memdex.store import SCHEMA_VERSION, Store

REPO = str(Path(__file__).resolve().parents[1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        from memdex import project as projects
        self.project = projects.resolve(self.root)
        self.env = dict(os.environ, MEMDEX_HOME=self.tmp,
                        PYTHONPATH=os.pathsep.join(x for x in (REPO, os.environ.get("PYTHONPATH")) if x))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, agent="qa"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main([*args, "--path", self.root, "--agent", agent, "--db", self.db])
        return code, buf.getvalue()

    def pre_edit(self, path, agent="qa", extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        payload = {"tool_name": "Edit", "tool_input": {"file_path": path}}
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "pre-edit", "--path", self.root,
             "--agent", agent, "--db", self.db],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def touch(self, rel, text):
        Path(self.root, rel).write_text(text, encoding="utf-8")


class TokenTests(Base):
    def test_each_grant_gets_the_next_token(self):
        first = self.store.claim(self.project, "claude", ["src/store.py"], "", 60)
        second = self.store.claim(self.project, "codex", ["README.md"], "", 60)
        self.assertEqual((first.token, second.token), (1, 2))

    def test_the_claim_records_the_content_it_was_taken_against(self):
        res = self.store.claim(self.project, "claude", ["src/store.py"], "", 60)
        row = self.store.db.execute("SELECT token, sha FROM claims WHERE agent='claude'").fetchone()
        self.assertEqual(row["token"], res.token)
        self.assertTrue(row["sha"] and len(row["sha"]) == 64)

    def test_the_whole_project_has_no_sha(self):
        self.store.claim(self.project, "claude", ["."], "", 60)
        row = self.store.db.execute("SELECT sha FROM claims WHERE agent='claude'").fetchone()
        self.assertIsNone(row["sha"])

    def test_refresh_bumps_the_token_and_re_stamps_the_sha(self):
        first = self.store.claim(self.project, "claude", ["src/store.py"], "", 60, exclusive=True)
        self.touch("src/store.py", "VALUE = 2\n")
        refreshed = self.store.claim(self.project, "claude", ["src/store.py"], "", 60,
                                     exclusive=True, refresh=True)
        self.assertTrue(refreshed.granted)
        self.assertGreater(refreshed.token, first.token)
        self.assertEqual(self.store.claim_sha(self.project, "src/store.py"),
                         self.store.db.execute("SELECT sha FROM claims WHERE agent='claude'").fetchone()[0])

    def test_refresh_without_a_claim_grants_nothing(self):
        res = self.store.claim(self.project, "claude", ["src/store.py"], "", 60, refresh=True)
        self.assertFalse(res.granted)

    def test_the_schema_says_six_and_the_columns_exist(self):
        self.assertEqual(SCHEMA_VERSION, 6)
        columns = {r[1] for r in self.store.db.execute("PRAGMA table_info(claims)")}
        self.assertTrue({"token", "sha"} <= columns)


class CompareAndSwapTests(Base):
    def test_an_edit_after_the_file_moved_under_our_own_lock_is_blocked(self):
        path = os.path.join(self.root, "src", "store.py")
        self.cli("claim", "src/store.py", "--exclusive", agent="qa")
        self.touch("src/store.py", "VALUE = 99\n")
        code, _, err = self.pre_edit(path)
        self.assertEqual(code, 2)
        self.assertIn("changed since you claimed it", err)
        self.assertIn("--refresh", err)

    def test_refreshing_the_lock_lets_the_edit_through(self):
        path = os.path.join(self.root, "src", "store.py")
        self.cli("claim", "src/store.py", "--exclusive", agent="qa")
        self.touch("src/store.py", "VALUE = 99\n")
        self.cli("claim", "src/store.py", "--exclusive", "--refresh", agent="qa")
        code, _, _ = self.pre_edit(path)
        self.assertEqual(code, 0)

    def test_an_advisory_claim_does_not_block(self):
        path = os.path.join(self.root, "src", "store.py")
        self.cli("claim", "src/store.py", agent="qa")
        self.touch("src/store.py", "VALUE = 99\n")
        code, _, _ = self.pre_edit(path)
        self.assertEqual(code, 0)

    def test_a_fresh_lock_does_not_block(self):
        path = os.path.join(self.root, "src", "store.py")
        self.cli("claim", "src/store.py", "--exclusive", agent="qa")
        code, _, _ = self.pre_edit(path)
        self.assertEqual(code, 0)

    def test_allow_lock_bypasses_the_check(self):
        path = os.path.join(self.root, "src", "store.py")
        self.cli("claim", "src/store.py", "--exclusive", agent="qa")
        self.touch("src/store.py", "VALUE = 99\n")
        code, _, _ = self.pre_edit(path, extra_env={"MEMDEX_ALLOW_LOCK": "1"})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
