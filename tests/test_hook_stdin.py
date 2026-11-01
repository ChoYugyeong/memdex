"""A git hook inherits git's stdin. Reading it there hangs the commit.

`cmd_hook` used to read stdin for every event when stdin was not a tty; a pre-commit hook launched by
`git commit` can inherit an open pipe, so the read never returned and a full `unittest discover` stalled
(module-by-module runs passed because their stdin happened to be closed). Only session-start reads it now.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])


class HookStdinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self):
        return dict(os.environ, MEMDEX_HOME=self.tmp,
                    PYTHONPATH=os.pathsep.join(x for x in (REPO, os.environ.get("PYTHONPATH")) if x))

    def spawn(self, *args):
        return subprocess.Popen(
            [sys.executable, "-m", "memdex", "hook", *args, "--path", self.root,
             "--agent", "qa", "--db", self.db],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env())

    def assert_exits(self, proc, label):
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail(f"{label} waited on stdin instead of exiting")
        self.assertEqual(rc, 0, proc.stdout.read())

    def test_pre_commit_does_not_wait_on_an_open_stdin(self):
        self.assert_exits(self.spawn("pre-commit"), "hook pre-commit")

    def test_install_does_not_wait_on_an_open_stdin(self):
        self.assert_exits(self.spawn("install"), "hook install")

    def test_session_start_reads_the_json_payload(self):
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "session-start", "--agent", "qa", "--db", self.db],
            input=json.dumps({"cwd": self.root}), capture_output=True, text=True,
            encoding="utf-8", env=self.env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("index:", proc.stdout)

    def test_session_start_with_garbage_stdin_still_prints_the_brief(self):
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "session-start", "--path", self.root,
             "--agent", "qa", "--db", self.db],
            input="not json at all", capture_output=True, text=True,
            encoding="utf-8", env=self.env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("memdex", proc.stdout)


if __name__ == "__main__":
    unittest.main()
