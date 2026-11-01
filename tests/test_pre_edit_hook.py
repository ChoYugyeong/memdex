"""The PreToolUse hook: an edit to a locked file is refused before it happens.

Claude Code sends the tool call as JSON on stdin and treats exit 2 as "blocked", showing stderr to the model.
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

REPO = str(Path(__file__).resolve().parents[1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.env = dict(os.environ, MEMDEX_HOME=self.tmp,
                        PYTHONPATH=os.pathsep.join(x for x in (REPO, os.environ.get("PYTHONPATH")) if x))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, agent="qa"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main([*args, "--path", self.root, "--agent", agent, "--db", self.db])
        return code, buf.getvalue()

    def pre_edit(self, payload, agent="codex", extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "pre-edit", "--path", self.root,
             "--agent", agent, "--db", self.db],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    @staticmethod
    def edit_payload(path, tool="Edit"):
        return {"tool_name": tool, "tool_input": {"file_path": path}}


class PreEditTests(Base):
    def test_edit_is_blocked_by_another_agents_lock(self):
        self.cli("claim", "src/store.py", "--exclusive", "--intent", "refactor", agent="claude")
        rc, out, err = self.pre_edit(self.edit_payload(os.path.join(self.root, "src", "store.py")))
        self.assertEqual(rc, 2, out + err)
        self.assertIn("blocked: locked by claude@", err)
        self.assertIn("refactor", err)
        self.assertIn("post to them or claim --force", err)

    def test_write_tool_is_checked_too(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        rc, _, err = self.pre_edit(self.edit_payload(os.path.join(self.root, "src", "store.py"), tool="Write"))
        self.assertEqual(rc, 2, err)

    def test_relative_path_in_the_payload(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        rc, _, err = self.pre_edit({"tool_name": "Edit", "tool_input": {"file_path": "src/store.py"}})
        self.assertEqual(rc, 2, err)

    def test_unlocked_file_is_allowed(self):
        self.cli("claim", "src/api.py", "--exclusive", agent="claude")
        rc, out, err = self.pre_edit(self.edit_payload(os.path.join(self.root, "src", "store.py")))
        self.assertEqual(rc, 0, out + err)

    def test_advisory_claim_only_notes(self):
        self.cli("claim", "src/store.py", "--intent", "review", agent="claude")
        rc, out, err = self.pre_edit(self.edit_payload(os.path.join(self.root, "src", "store.py")))
        self.assertEqual(rc, 0, err)
        self.assertIn("advisory", out)

    def test_allow_lock_env_bypasses(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        rc, _, err = self.pre_edit(self.edit_payload(os.path.join(self.root, "src", "store.py")),
                                   extra_env={"MEMDEX_ALLOW_LOCK": "1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("ALLOW_LOCK", err)

    def test_other_tools_are_ignored(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        rc, out, err = self.pre_edit({"tool_name": "Bash", "tool_input": {"command": "rm -rf src"}})
        self.assertEqual(rc, 0, out + err)

    def test_path_outside_the_project_is_ignored(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        rc, out, err = self.pre_edit(self.edit_payload(os.path.join(self.tmp, "elsewhere.py")))
        self.assertEqual(rc, 0, out + err)

    def test_missing_path_is_ignored(self):
        rc, out, err = self.pre_edit({"tool_name": "Edit", "tool_input": {}})
        self.assertEqual(rc, 0, out + err)

    def test_git_config_agent_is_used_when_no_flag_is_passed(self):
        self.cli("claim", "src/store.py", "--exclusive", agent="claude")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "memdex.agent", "codex"], cwd=self.root, check=True)
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "pre-edit", "--path", self.root, "--db", self.db],
            input=json.dumps(self.edit_payload(os.path.join(self.root, "src", "store.py"))),
            capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("claude@", proc.stderr)


class InstallClaudeTests(Base):
    def settings(self):
        return Path(self.root, ".claude", "settings.json")

    def read(self):
        return json.loads(self.settings().read_text(encoding="utf-8"))

    def test_install_writes_the_pretooluse_hook(self):
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 0, out)
        data = self.read()
        entries = data["hooks"]["PreToolUse"]
        self.assertEqual(entries[0]["matcher"], "Edit|Write|MultiEdit")
        self.assertIn("hook pre-edit", entries[0]["hooks"][0]["command"])

    def test_install_preserves_existing_hooks_and_backs_up(self):
        self.settings().parent.mkdir(parents=True)
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]},
                    "model": "opus"}
        self.settings().write_text(json.dumps(existing), encoding="utf-8")
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 0, out)
        data = self.read()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"], "other")
        self.assertIn("PreToolUse", data["hooks"])
        self.assertTrue(Path(self.root, ".claude", "settings.json.bak").exists())

    def test_second_install_is_idempotent(self):
        self.cli("hook", "install", "--claude", "--project")
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 0, out)
        self.assertIn("already installed", out)
        self.assertEqual(len(self.read()["hooks"]["PreToolUse"]), 1)

    def test_invalid_json_is_left_alone(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text("{not json", encoding="utf-8")
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 1)
        self.assertIn("not", out)
        self.assertEqual(self.settings().read_text(encoding="utf-8"), "{not json")

    def test_config_snippet_mentions_pre_edit(self):
        code, out = self.cli("config")
        self.assertEqual(code, 0)
        self.assertIn("PreToolUse", out)
        self.assertIn("hook pre-edit", out)


if __name__ == "__main__":
    unittest.main()
