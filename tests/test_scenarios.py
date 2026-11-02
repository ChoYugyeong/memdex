"""Scenario tests: the situations the walkthrough promises, run end to end through the CLI.

Each test starts a throwaway project and drives `memdex` the way two agents would: commands, not
functions. These are the stories behind the unit tests — two agents on one checkout, a closed session,
two worktrees, a cold start, two memories that fight, a stale read, a newer database.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from memdex.cli import main


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo-app")
        os.makedirs(os.path.join(self.root, "src"))
        self.write("src/store.py", "def save(k, v):\n    pass\n\n\ndef load(k):\n    return None\n")
        self.write("src/api.py", "def get(k):\n    return load(k)\n")
        self.write("README.md", "# demo-app\n")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.saved_agent = os.environ.get("MEMDEX_AGENT")
        os.environ.pop("MEMDEX_AGENT", None)
        os.environ["MEMDEX_HOME"] = os.path.join(self.tmp, "home")
        os.environ["MEMDEX_SKILL_HOME"] = os.path.join(self.tmp, "skill-home")
        os.makedirs(os.environ["MEMDEX_HOME"], exist_ok=True)
        os.makedirs(os.environ["MEMDEX_SKILL_HOME"], exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MEMDEX_HOME", None)
        os.environ.pop("MEMDEX_SKILL_HOME", None)
        if self.saved_agent is None:
            os.environ.pop("MEMDEX_AGENT", None)
        else:
            os.environ["MEMDEX_AGENT"] = self.saved_agent

    def write(self, rel, text):
        p = Path(self.root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def cli(self, agent, *args):
        """Run memdex as `agent` against this scenario's project; returns (exit, stdout)."""
        buf, err = io.StringIO(), io.StringIO()
        os.environ["MEMDEX_AGENT"] = agent
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = main([*args, "--path", self.root, "--db", self.db])
            except SystemExit as e:
                code = e.code
            except Exception as exc:  # a refusal is the behaviour under test
                code = 2
                buf.write(str(exc))
        return code, buf.getvalue()


class TwoAgentsOneCheckout(Scenario):
    def test_claim_overlap_message_and_handoff(self):
        code, out = self.cli("claude", "claim", "src/store.py", "--intent", "add retry")
        self.assertEqual(code, 0)
        self.assertIn("claimed 1 path(s)", out)
        code, out = self.cli("codex", "claim", "src/store.py", "--intent", "fix timeout")
        self.assertIn("overlaps with other agents", out)
        self.assertIn("claude@demo-app", out)
        code, out = self.cli("codex", "post", "store.py is yours; I take src/api.py", "--to", "claude")
        self.assertIn("not delivered to an inbox yet", out)
        code, out = self.cli("claude", "inbox")
        self.assertIn("codex@demo-app", out)
        self.assertIn("store.py is yours", out)
        code, out = self.cli("codex", "handoff", "retry + backoff in store.py", "--next", "wire timeout to config")
        self.assertIn("handoff #1 saved", out)
        self.assertIn("Released 1 claim(s)", out)
        code, out = self.cli("claude", "brief")
        self.assertIn("last handoff #1", out)
        self.assertIn("Next: wire timeout to config", out)


class AClosedSession(Scenario):
    def test_a_message_waits_for_the_session_that_starts_later(self):
        self.cli("deepseek", "post", "the build is red", "--to", "codex")
        code, out = self.cli("codex", "inbox")
        self.assertEqual(code, 0)
        self.assertIn("the build is red", out)
        # …and it is delivered exactly once.
        code, out = self.cli("codex", "inbox")
        self.assertIn("no unread messages", out)


class TwoWorktrees(Scenario):
    def test_one_repo_two_worktrees_share_notes_messages_and_summaries(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t",
                        "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], check=True)
        wt = os.path.join(self.tmp, "wt2")
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q", wt], check=True)

        code, out = self.cli("claude", "remember", "decision", "one sqlite file", "--body", "never a service")
        self.assertEqual(code, 0)
        code, out = self.cli("claude", "file", "src/api.py", "--summary", "Thin getter over load().")
        self.assertIn("saved for src/api.py", out)
        code, out = self.cli("codex", "post", "I take README", "--to", "codex")

        os.environ["MEMDEX_AGENT"] = "codex"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["search", "one sqlite", "--path", wt, "--db", self.db])
        self.assertIn("one sqlite file", buf.getvalue())

        with contextlib.redirect_stdout(buf):
            code = main(["file", "src/api.py", "--path", wt, "--db", self.db])
        self.assertIn("matches current content", buf.getvalue())

        with contextlib.redirect_stdout(buf):
            code = main(["inbox", "--path", wt, "--db", self.db])
        self.assertIn("I take README", buf.getvalue())


class ColdStart(Scenario):
    def test_importing_agents_md_feeds_the_brief_and_search(self):
        self.write("AGENTS.md", "# rules\n\n## one rule\n\nThe store is one SQLite file, never a service.\n")
        code, out = self.cli("claude", "import")
        self.assertIn("1 section(s) imported", out)
        code, out = self.cli("codex", "brief")
        self.assertIn("memory: 1 note(s)", out)
        self.assertNotIn("No notes yet", out)
        code, out = self.cli("codex", "search", "never a service")
        self.assertIn("one rule", out)


class FightingMemories(Scenario):
    def test_contradictory_decisions_are_named_by_brief_and_command(self):
        self.cli("claude", "remember", "decision", "Always use sync mode",
                 "--body", "the store writes in sync mode", "--files", "src/store.py")
        self.cli("codex", "remember", "pitfall", "Never use sync mode",
                 "--body", "sync mode deadlocks the workers", "--files", "src/store.py")
        code, out = self.cli("codex", "contradictions")
        self.assertIn("disagree on:", out)
        self.assertIn("sync", out)
        code, out = self.cli("codex", "brief")
        self.assertIn("contradiction:", out)
        self.assertIn("settle before editing", out)
        code, out = self.cli("codex", "whoami")
        self.assertIn("contradictions: 1 pair(s)", out)


class AStaleRead(Scenario):
    def test_the_hook_refuses_an_edit_computed_from_a_stale_read(self):
        self.cli("claude", "claim", "src/store.py", "--exclusive", "--intent", "lock")
        self.write("src/store.py", "def save(k, v):\n    pass\n\n\ndef load(k):\n    return None\n# changed\n")
        payload = json.dumps({"tool_input": {"file_path": os.path.join(self.root, "src", "store.py")}})
        env = dict(os.environ)
        env["MEMDEX_AGENT"] = "claude"
        import memdex as _memdex  # the subprocess needs the package on its path, not the harness's cwd
        repo = os.path.dirname(os.path.dirname(os.path.abspath(_memdex.__file__)))
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        # Redirect to files: this machine's harness cannot capture a child through piped stdio, and the
        # refusal message is what matters, not the pipe.
        out_f = os.path.join(self.tmp, "hook-out.txt")
        err_f = os.path.join(self.tmp, "hook-err.txt")
        with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
            proc = subprocess.run(
                [__import__("sys").executable, "-m", "memdex", "hook", "pre-edit",
                 "--path", self.root, "--db", self.db],
                input=payload.encode("utf-8"), env=env, cwd=self.root, stdout=fo, stderr=fe,
            )
        def read(name):
            with open(name, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        text = read(out_f) + read(err_f)
        self.assertEqual(proc.returncode, 2, text)
        self.assertIn("changed since you claimed it", text)


class AStaleSummary(Scenario):
    def test_a_changed_file_flags_its_summary_and_names_what_moved(self):
        self.cli("claude", "file", "src/store.py", "--summary", "Saves values.")
        self.write("src/store.py", "def save(k, v):\n    pass\n\n\ndef load(k):\n    return None\n\n\ndef wipe(k):\n    pass\n")
        code, out = self.cli("claude", "file", "src/store.py")
        self.assertIn("no summary for the current content", out)
        self.assertIn("symbol(s) differ", out)
        self.assertIn("Saves values.", out)


class ANewerDatabase(Scenario):
    def test_a_database_from_the_future_is_refused_not_clobbered(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version = 99")
        code, out = self.cli("claude", "brief")
        self.assertEqual(code, 2)
        self.assertIn("schema v99 is newer", out)


class WhoAmI(Scenario):
    def test_identity_is_readable_without_an_index_pass(self):
        code, out = self.cli("claude", "whoami")
        self.assertEqual(code, 0)
        self.assertIn("you are claude@demo-app", out)
        self.assertIn("schema v", out)
        self.assertNotIn("index:", out)


class ProjectSkills(Scenario):
    def test_a_project_names_its_skills_and_the_brief_repeats_them(self):
        code, out = self.cli("claude", "skills", "share", "office",
                             "--description", "word excel documents")
        self.assertIn("shared skill 'office'", out)
        code, out = self.cli("claude", "skills", "recommend", "office")
        self.assertIn(".memdex/skills.json", out)
        self.write("docs/report.docx", "x")
        code, out = self.cli("codex", "brief")
        self.assertIn("project skills: office", out)
        self.assertIn("skills for what's next: office", out)


if __name__ == "__main__":
    unittest.main()
