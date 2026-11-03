"""Messages between sessions: who is listening, and waiting for one that is not there yet.

memdex has no push channel — a message waits in the mailbox until a session reads it. Three things follow,
and all three are tested here: an id copied from a brief must not grow a second checkout suffix, `post` must
say whether the message was handed over or is waiting, and `inbox --follow` gives a session a way to wait for
the next one without inventing a daemon.
"""

import contextlib
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from memdex import cli as cli_mod
from memdex import project as projects
from memdex.cli import main
from memdex.store import Store
from memdex.tools import Session


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("x = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)
        self.env_agent = os.environ.get("MEMDEX_AGENT")
        os.environ.pop("MEMDEX_AGENT", None)

    def tearDown(self):
        if self.env_agent is not None:
            os.environ["MEMDEX_AGENT"] = self.env_agent
        else:
            os.environ.pop("MEMDEX_AGENT", None)
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, agent="claude"):
        s = Session(self.store, agent_name=agent)
        s.cwd = self.root
        s.home = self.root
        return s

    def cli(self, *args, env_agent=None):
        buf, err = io.StringIO(), io.StringIO()
        old = os.environ.get("MEMDEX_AGENT")
        if env_agent is None:
            os.environ.pop("MEMDEX_AGENT", None)
        else:
            os.environ["MEMDEX_AGENT"] = env_agent
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = main([*args, "--path", self.root, "--db", self.db])
        finally:
            if old is None:
                os.environ.pop("MEMDEX_AGENT", None)
            else:
                os.environ["MEMDEX_AGENT"] = old
        return code, buf.getvalue(), err.getvalue()


class AgentIdTests(Base):
    def test_a_plain_name_gets_the_checkout(self):
        self.assertEqual(self.session("codex").agent, "codex@demo")

    def test_a_name_that_already_has_a_checkout_is_left_alone(self):
        self.assertEqual(self.session("codex@demo").agent, "codex@demo")

    def test_the_env_var_is_normalised_the_same_way(self):
        os.environ["MEMDEX_AGENT"] = "Codex@demo"
        try:
            self.assertEqual(self.session().agent, "codex@demo")
            os.environ["MEMDEX_AGENT"] = "codex"
            self.assertEqual(self.session().agent, "codex@demo")
        finally:
            os.environ.pop("MEMDEX_AGENT", None)

    def test_the_brief_no_longer_doubles_the_suffix(self):
        code, out, _ = self.cli("brief", env_agent="codex@demo")
        self.assertEqual(code, 0)
        self.assertIn("you are codex@demo", out)
        self.assertNotIn("codex@demo@demo", out)

    def test_an_id_from_the_brief_can_be_used_as_the_env_var(self):
        # The trap the fix closes: copy "you are codex@demo" into MEMDEX_AGENT and keep one cursor.
        self.cli("post", "for you", "--to", "codex", env_agent="deepseek")
        code, out, _ = self.cli("inbox", env_agent="codex@demo")
        self.assertEqual(code, 0)
        self.assertIn("for you", out)


class DeliveryTests(Base):
    def test_post_says_when_nothing_has_reached_an_inbox(self):
        out = self.session("deepseek").post("for you", to="codex")
        self.assertIn("not delivered to an inbox yet", out)
        self.assertIn("waits for codex's next one", out)

    def test_a_brief_look_does_not_count_as_delivery(self):
        self.session("deepseek").post("first", to="codex")
        self.session("codex").brief()          # counts unread, consumes nothing
        out = self.session("deepseek").post("second", to="codex")
        self.assertIn("not delivered to an inbox yet", out)

    def test_post_says_when_the_recipient_last_read_its_inbox(self):
        self.session("deepseek").post("first", to="codex")
        self.session("codex").inbox()          # the recipient consumes it
        out = self.session("deepseek").post("second", to="codex")
        self.assertIn("codex's last inbox read was", out)

    def test_a_broadcast_does_not_claim_a_recipient_state(self):
        out = self.session("deepseek").post("hello all")
        self.assertIn("to everyone on this project", out)
        self.assertNotIn("queued", out)

    def test_posting_to_your_own_id_is_reported(self):
        out = self.session("codex").post("note to self", to="codex")
        self.assertIn("you are codex@demo", out)
        self.assertIn("never see this as unread", out)

    def test_a_message_to_an_uppercase_recipient_still_arrives(self):
        self.store.post(self.project, "deepseek@demo", "hi there", "Claude")
        out = self.session("claude").inbox()
        self.assertIn("hi there", out)
        self.assertNotIn("no unread", out)

    def test_a_whitespace_recipient_still_arrives(self):
        self.store.post(self.project, "deepseek@demo", "hi there", " claude ")
        self.assertIn("hi there", self.session("claude").inbox())

    def test_a_message_waits_for_a_closed_session_and_arrives_later(self):
        self.session("deepseek").post("store.py is yours", to="claude")
        out = self.session("claude").inbox()
        self.assertIn("store.py is yours", out)
        self.assertIn("no unread", self.session("claude").inbox())  # and only once

    def test_snapshot_does_not_consume_and_ack_does_not_skip_a_newer_message(self):
        self.store.post(self.project, "deepseek@demo", "first", "claude")
        msgs, cursor = self.store.inbox_snapshot(self.project, "claude@demo")
        self.assertEqual([m["body"] for m in msgs], ["first"])
        again, _ = self.store.inbox_snapshot(self.project, "claude@demo")
        self.assertEqual([m["body"] for m in again], ["first"])

        self.store.post(self.project, "deepseek@demo", "arrived while printing", "claude")
        self.store.acknowledge_inbox(self.project, "claude@demo", cursor)
        remaining = self.store.inbox(self.project, "claude@demo")
        self.assertEqual([m["body"] for m in remaining], ["arrived while printing"])

    def test_acknowledgement_never_moves_a_shared_cursor_backwards(self):
        self.store.post(self.project, "deepseek@demo", "first", "claude")
        _, first_cursor = self.store.inbox_snapshot(self.project, "claude@demo")
        self.store.post(self.project, "deepseek@demo", "second", "claude")
        _, second_cursor = self.store.inbox_snapshot(self.project, "claude@demo")
        self.store.acknowledge_inbox(self.project, "claude@demo", second_cursor)
        self.store.acknowledge_inbox(self.project, "claude@demo", first_cursor)
        self.assertEqual(self.store.inbox(self.project, "claude@demo"), [])


class FollowTests(Base):
    def test_follow_returns_as_soon_as_a_message_arrives(self):
        def later():
            time.sleep(0.6)
            # Post through the store: the CLI call below sets MEMDEX_AGENT for this whole process, and a
            # Session would read it and post as codex, which is the self-message case.
            self.store.post(self.project, "deepseek@demo", "the build is red", "codex")

        threading.Thread(target=later, daemon=True).start()
        code, out, err = self.cli("inbox", "--follow", "--timeout", "10", "--poll", "0.2",
                                  env_agent="codex")
        self.assertEqual(code, 0)
        self.assertIn("the build is red", out)
        self.assertIn("waiting for a message as codex@demo", err)

    def test_notify_batch_peeks_until_the_interval_then_consumes_once(self):
        clock = [0.0]
        calls = []
        consumed = [False]

        class FakeSession:
            agent = "codex@demo"

            def inbox_batch(self):
                calls.append((clock[0], "peek"))
                if consumed[0]:
                    return "no unread messages.", 0
                return "#42 deepseek: first\n#43 deepseek: second", 43

            def acknowledge_inbox(self, cursor):
                calls.append((clock[0], "ack", cursor))
                consumed[0] = True

        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli_mod, "_session", return_value=FakeSession()), \
                mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(cli_mod.time, "sleep",
                                  side_effect=lambda duration: clock.__setitem__(0, clock[0] + duration)), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli_mod.main(["inbox", "--follow", "--timeout", "2", "--poll", ".2",
                                 "--notify-every", "1"])
        self.assertEqual(code, 0)
        self.assertIn("#42 deepseek: first", buf.getvalue())
        self.assertIn("#43 deepseek: second", buf.getvalue())
        self.assertIn("poll 0.2s, notify every 1s", err.getvalue())
        ack_calls = [call for call in calls if len(call) == 3]
        self.assertEqual(len(ack_calls), 1)
        self.assertGreaterEqual(ack_calls[0][0], 1.0)
        self.assertEqual(ack_calls[0][2], 43)
        self.assertTrue(buf.getvalue())  # output was emitted before the cursor was acknowledged

    def test_notify_interval_requires_follow(self):
        code, out, err = self.cli("inbox", "--notify-every", "60")
        self.assertEqual(code, 2)
        self.assertIn("requires --follow", err)

    def test_follow_times_out_with_a_failure(self):
        code, out, _ = self.cli("inbox", "--follow", "--timeout", "0.6", "--poll", "0.2",
                                env_agent="codex")
        self.assertEqual(code, 1)
        self.assertIn("no message within the timeout", out)


if __name__ == "__main__":
    unittest.main()
