"""Exclusive claims from the CLI, and the git pre-commit hook that enforces them.

An exclusive claim is a lock: another agent's claim on the same path is refused, a locked file cannot be
committed, and an advisory claim only produces a note. `memdex hook install` puts that check into git.
"""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from memdex.cli import main


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "src"))
        self.db = os.path.join(self.tmp, "memdex.db")
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, agent="qa"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main([*args, "--path", self.root, "--agent", agent, "--db", self.db])
        return code, buf.getvalue()

    def git(self, *args):
        # The installed hook runs `python -m memdex`. On CI nothing is pip-installed, so the hook's
        # interpreter must find the checkout through PYTHONPATH (the test runner finds it via cwd).
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, MEMDEX_HOME=self.tmp,
                   PYTHONPATH=os.pathsep.join(x for x in (repo, os.environ.get("PYTHONPATH")) if x))
        r = subprocess.run(["git", *args], cwd=self.root, env=env, capture_output=True,
                           text=True, encoding="utf-8")
        return r.returncode, (r.stdout + r.stderr)

    def stage_only(self, *paths):
        self.git("reset", "-q")
        for p in paths:
            rc, out = self.git("add", p)
            assert rc == 0, out


class ClaimCliTests(Base):
    def test_exclusive_claim_refuses_another_agent(self):
        code, out = self.cli("claim", "src/store.py", "--exclusive", "--intent", "refactor", agent="claude")
        self.assertEqual(code, 0)
        self.assertIn("exclusive", out)

        code, out = self.cli("claim", "src/store.py", "--intent", "bugfix", agent="codex")
        self.assertEqual(code, 1, out)
        self.assertIn("not claimed", out)
        self.assertIn("claude", out)

    def test_force_wins_over_a_lock(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        code, out = self.cli("claim", "src/store.py", "--force", "--intent", "hotfix", agent="codex")
        self.assertEqual(code, 0, out)
        self.assertIn("forced", out.lower())

    def test_advisory_claims_do_not_block_each_other(self):
        self.cli("claim", "src/store.py", "--intent", "one", agent="claude")
        code, out = self.cli("claim", "src/store.py", "--intent", "two", agent="codex")
        self.assertEqual(code, 0, out)
        self.assertIn("overlaps", out)

    def test_release(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        code, out = self.cli("claim", "--release", agent="claude")
        self.assertEqual(code, 0)
        self.assertIn("released", out)
        code, out = self.cli("claim", "src/store.py", "--exclusive", agent="codex")
        self.assertEqual(code, 0, out)


class HookTests(Base):
    def test_pre_commit_blocks_a_locked_file(self):
        self.stage_only("src/store.py")
        self.cli("claim", "src/store.py", "--exclusive", "--intent", "refactor", agent="claude")
        code, out = self.cli("hook", "pre-commit", agent="codex")
        self.assertEqual(code, 1, out)
        self.assertIn("commit blocked", out)
        self.assertIn("claude", out)

    def test_pre_commit_allows_an_unlocked_file(self):
        self.stage_only("src/store.py")
        self.cli("claim", "src/api.py", "--exclusive", agent="claude")
        code, out = self.cli("hook", "pre-commit", agent="codex")
        self.assertEqual(code, 0, out)

    def test_pre_commit_warns_on_an_advisory_claim(self):
        self.stage_only("src/store.py")
        self.cli("claim", "src/store.py", "--intent", "review", agent="claude")
        code, out = self.cli("hook", "pre-commit", agent="codex")
        self.assertEqual(code, 0, out)
        self.assertIn("advisory", out)

    def test_allow_lock_env_bypasses(self):
        self.stage_only("src/store.py")
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        old = os.environ.get("MEMDEX_ALLOW_LOCK")
        try:
            os.environ["MEMDEX_ALLOW_LOCK"] = "1"
            code, out = self.cli("hook", "pre-commit", agent="codex")
        finally:
            if old is None:
                os.environ.pop("MEMDEX_ALLOW_LOCK", None)
            else:
                os.environ["MEMDEX_ALLOW_LOCK"] = old
        self.assertEqual(code, 0, out)
        self.assertIn("ALLOW_LOCK", out)

    def test_install_writes_a_hook_and_refuses_foreign_hooks(self):
        code, out = self.cli("hook", "install")
        self.assertEqual(code, 0, out)
        hook = Path(self.root, ".git", "hooks", "pre-commit")
        self.assertTrue(hook.exists())
        text = hook.read_text(encoding="utf-8")
        self.assertIn("hook pre-commit", text)
        self.assertIn("memdex", text.lower())

        hook.write_text("#!/bin/sh\necho other\n", encoding="utf-8")
        code, out = self.cli("hook", "install")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        code, out = self.cli("hook", "install", "--force")
        self.assertEqual(code, 0, out)
        self.assertIn("hook pre-commit", hook.read_text(encoding="utf-8"))

    def test_installed_hook_blocks_a_real_commit(self):
        self.cli("hook", "install", agent="codex")
        self.stage_only("src/store.py")
        self.cli("claim", "src/store.py", "--exclusive", "--intent", "refactor", agent="claude")
        rc, out = self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "blocked?")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("commit blocked", out)


if __name__ == "__main__":
    unittest.main()
