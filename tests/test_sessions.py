"""Session liveness and `wake`: if an agent has work waiting but no live session, open one.

A brief is the heartbeat (`render_brief` touches the sessions table), so any agent that works normally
registers itself with zero ceremony. `wake` is the other half: it opens a new session only when two
conditions hold — something is waiting (an unread message or someone else's handoff) and no session of
that agent is live.
"""

import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.cli import main
from memdex.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "app.py").write_text("x = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)
        self.saved_agent = os.environ.get("MEMDEX_AGENT")
        os.environ.pop("MEMDEX_AGENT", None)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.saved_agent is None:
            os.environ.pop("MEMDEX_AGENT", None)
        else:
            os.environ["MEMDEX_AGENT"] = self.saved_agent

    def cli(self, agent, *args):
        buf = io.StringIO()
        os.environ["MEMDEX_AGENT"] = agent
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main([*args, "--path", self.root, "--db", self.db])
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()


class LivenessTests(Base):
    def test_a_brief_registers_the_session(self):
        self.cli("claude", "brief")
        live = self.store.live_sessions(self.project)
        self.assertEqual([row["agent"] for row in live], ["claude@proj"])

    def test_sessions_expire_after_the_ttl(self):
        self.store.touch_session(self.project, "claude@proj")
        self.store.db.execute("UPDATE sessions SET last_seen_at = last_seen_at - 700")
        self.store.db.commit()
        self.assertEqual(self.store.live_sessions(self.project), [])

    def test_ping_and_close(self):
        code, out = self.cli("codex", "session", "ping")
        self.assertIn("pinged codex@proj", out)
        code, out = self.cli("codex", "session", "list")
        self.assertIn("codex@proj", out)
        code, out = self.cli("codex", "session", "close")
        self.assertIn("closed codex", out)
        self.assertEqual(self.store.live_sessions(self.project), [])


class WakeTests(Base):
    def test_nothing_waiting_opens_nothing(self):
        code, out = self.cli("claude", "wake", "--command", "python -c pass")
        self.assertIn("nothing is waiting", out)

    def test_a_live_session_is_left_alone(self):
        self.cli("claude", "brief")  # heartbeat
        self.store.post(self.project, "codex@proj", "for you", "claude")
        code, out = self.cli("claude", "wake", "--command", "python -c pass")
        self.assertIn("already has a live session", out)

    def test_waiting_work_launches_the_command_for_a_dead_session(self):
        self.store.post(self.project, "codex@proj", "the build is red", "claude")
        marker = os.path.join(self.tmp, "woke.txt")
        cmd = f"python -c \"open(r'{marker}', 'w').write('woke')\""
        code, out = self.cli("claude", "wake", "--command", cmd)
        self.assertIn("has no live session", out)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(Path(marker).read_text(), "woke")

    def test_a_handoff_from_someone_else_is_waiting_work(self):
        self.cli("codex", "handoff", "done", "--next", "wire timeout")
        code, out = self.cli("claude", "wake")
        self.assertIn("handoff #1 from codex@proj", out)
        self.assertIn("pass --command", out)

    def test_dry_run_prints_without_running(self):
        self.store.post(self.project, "codex@proj", "hi", "claude")
        marker = os.path.join(self.tmp, "must-not-exist.txt")
        code, out = self.cli("claude", "wake", "--command",
                             f"python -c \"open(r'{marker}', 'w')\"", "--dry-run")
        self.assertIn("would run:", out)
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
