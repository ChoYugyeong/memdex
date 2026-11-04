"""Exact mail across similar conversations, multiplexed MCP calls and short-lived CLI invocations."""

import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from memdex import mcp
from memdex.store import Store
from memdex.tools import Session


class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name, "demo"))
        Path(self.root).mkdir()
        self.db = str(Path(tmp.name, "test.db"))
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        env = mock.patch.dict(os.environ, {"MEMDEX_PROJECT": self.root})
        env.start()
        self.addCleanup(env.stop)
        for key in ("MEMDEX_AGENT", "MEMDEX_SESSION_ID", "MEMDEX_CONVERSATION_ID",
                    "CODEX_THREAD_ID", "DSH_SESSION_ID", "DSH_SHELL"):
            os.environ.pop(key, None)

    def chat(self, agent, conversation):
        return Session(self.store, agent_name=agent, cwd=self.root, conversation_id=conversation)

    def call(self, server, name, args=None, meta=None):
        params = {"name": name, "arguments": args or {}}
        if meta is not None:
            params["_meta"] = meta
        with contextlib.redirect_stderr(io.StringIO()):
            result = mcp.handle(server, {t.name: t for t in server.tools()},
                                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        return (result["error"]["message"] if "error" in result
                else result["result"]["content"][0]["text"])

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "memdex", *args, "--path", self.root,
                                 "--db", self.db], capture_output=True, text=True, timeout=20,
                                cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_lookalike_sessions_and_correlated_reply(self):
        a, b = [self.chat("deepseek", k) for k in ("aa-100", "aa-101")]
        c, d = [self.chat("codex", k) for k in ("bb-100", "bb-101")]
        for chat in (a, b, c, d):
            chat.session_routes(label="same task")
        self.assertEqual(len({x.route for x in (a, b, c, d)}), 4)
        a.post("original-token", to=c.route)
        mid = self.store.db.execute("SELECT id FROM messages WHERE body='original-token'").fetchone()[0]
        self.assertNotIn("original-token", d.inbox())
        self.assertIn("original-token", c.inbox())
        c.post("reply-token", reply_to=mid)
        self.assertNotIn("reply-token", b.inbox())
        received = a.inbox()
        self.assertIn("reply-token", received)
        self.assertIn(f"in reply to #{mid}", received)
        self.assertNotIn("reply-token", a.inbox())

    def test_discovery_withholds_indistinguishable_or_inactive_destination(self):
        viewer = self.chat("codex", "source")
        a, b = [self.chat("deepseek", k) for k in ("A", "a")]
        for chat in (a, b):
            chat.session_routes("shared")
        listing = viewer.session_routes(query="shared")
        self.assertIn("ambiguous", listing)
        self.assertNotIn(a.route, listing)
        self.assertNotIn(b.route, listing)
        a.session_routes("only-a")
        self.assertIn(a.route, viewer.session_routes(query="only-a"))
        self.store.db.execute("UPDATE chat_routes SET last_seen_at=0 WHERE route=?", (a.route,))
        self.assertNotIn(a.route, viewer.session_routes(query="only-a"))
        self.assertIn(a.route, viewer.session_routes(query=a.route))

    def test_wrong_viewer_and_conflicting_reply_target_create_no_message(self):
        a, b, wrong = [self.chat("deepseek", k) for k in ("one", "two", "three")]
        for chat in (a, b, wrong):
            chat.session_routes()
        a.post("question", to=b.route)
        mid = self.store.db.execute("SELECT id FROM messages WHERE body='question'").fetchone()[0]
        # Both the CLI-facing Session API and MCP may surface refusals as text or ValueError.
        for chat, args in ((wrong, {"reply_to": mid}), (b, {"reply_to": mid, "to": wrong.route})):
            with self.subTest(args=args), contextlib.suppress(ValueError):
                chat.post("misdirected", **args)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_missing_ambiguous_or_unknown_destination_cannot_queue_direct_mail(self):
        sender = self.chat("codex", "origin")
        sender.session_routes()
        for k in ("one", "two"):
            self.chat("deepseek", k).session_routes(label="same repo and task")
        for target in ("", "deepseek", "deepseek@demo", "route:" + "0" * 32):
            with self.subTest(target=target), self.assertRaises(ValueError):
                sender.post("must not send", to=target)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_conflicting_mcp_ids_do_not_consume_either_chat(self):
        sender = self.chat("codex", "origin")
        a, b = [self.chat("deepseek", k) for k in ("one", "two")]
        a.session_routes()
        b.session_routes()
        sender.post("first-token", to=a.route)
        sender.post("second-token", to=b.route)
        server = self.chat("deepseek", None)
        refused = self.call(server, "inbox", meta={"memdex.dev/conversation-id": "one",
                                                   "session_id": "two"})
        self.assertNotIn("first-token", refused)
        self.assertNotIn("second-token", refused)
        self.assertIn("first-token", self.call(server, "inbox", meta={"session_id": "one"}))
        self.assertIn("second-token", self.call(server, "inbox", meta={"session_id": "two"}))

    def test_project_selection_is_scoped_to_the_requesting_chat(self):
        other = str(Path(self.root).with_name("other"))
        Path(other).mkdir()
        server = self.chat("deepseek", None)
        server.home = self.root
        one, two = {"session_id": "one"}, {"session_id": "two"}
        self.call(server, "project_brief", {"project": other}, meta=one)
        self.call(server, "session_routes", {"label": "other-project-only"}, meta=one)
        self.call(server, "session_routes", {"label": "default-project-only"}, meta=two)
        first = self.call(server, "session_routes", meta=one)
        second = self.call(server, "session_routes", meta=two)
        self.assertIn("other-project-only", first)
        self.assertNotIn("default-project-only", first)
        self.assertIn("default-project-only", second)
        self.assertNotIn("other-project-only", second)

    def test_mcp_identity_is_scoped_to_one_call_and_cannot_be_supplied_as_an_argument(self):
        sender = self.chat("codex", "test-source")
        target = self.chat("deepseek", "test-target")
        target.session_routes()
        sender.session_routes()
        sender.post("private-one", to=target.route)
        server = self.chat("deepseek", None)
        meta = {"memdex.dev/conversation-id": "test-target"}
        self.assertIn("private-one", self.call(server, "inbox", meta=meta))
        sender.post("private-two", to=target.route)
        self.assertNotIn("private-two", self.call(server, "inbox"))
        forged = {"conversation_id": "test-target", "_meta": meta}
        self.assertNotIn("private-two", self.call(server, "inbox", args=forged))
        self.assertIn("private-two", self.call(server, "inbox", meta=meta))

    def test_dsh_shell_registration_mcp_receipt_and_cli_reply(self):
        sender = self.chat("codex", "thread-codex")
        sender.session_routes()
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1", "DSH_SESSION_ID": "session-dsh",
                                          "MEMDEX_AGENT": "deepseek"}):
            listing = self.cli("session", "routes", "--label", "work")
        target = self.chat("deepseek", "session-dsh")
        self.assertIn(target.route, listing)
        sender.post("from-codex", to=target.route)
        mid = self.store.db.execute("SELECT id FROM messages WHERE body='from-codex'").fetchone()[0]
        server = self.chat("deepseek", None)
        self.assertIn("from-codex", self.call(server, "inbox", meta={"session_id": "session-dsh"}))
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1", "DSH_SESSION_ID": "session-dsh",
                                          "MEMDEX_AGENT": "deepseek"}):
            self.cli("post", "from-dsh", "--reply-to", str(mid))
        self.assertIn("from-dsh", self.call(self.chat("codex", None), "inbox",
                                           meta={"threadId": "thread-codex"}))

    def test_dsh_profile_registration_matches_cli_and_mcp_and_isolates_siblings(self):
        for profile in ("desktop", "web"):
            with self.subTest(profile=profile):
                env = {"DSH_SESSION_ID": "same-id", "DSH_PROFILE": profile, "DSH_SHELL": "1"}
                target = self.chat("dsh-" + profile, "same-id")
                with mock.patch.dict(os.environ, env):
                    self.assertIn(target.route, self.cli("session", "routes", "--label", profile))
                server = self.chat("deepseek", None)
                with mock.patch.dict(os.environ, {"MEMDEX_AGENT": "dsh-" + profile}):
                    self.assertIn("Your address: " + target.route, self.call(
                        server, "session_routes", meta={"session_id": "same-id"}))
                    self.assertIn("missing per-call chat id", self.call(server, "inbox"))
            self.assertIsNotNone(self.store.get_route(self.chat("viewer", None).project, target.route))
        self.assertEqual(len(self.store.route_rows(self.chat("viewer", None).project)), 2)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.chat("legacy", None).post("private", to="dsh-absent")

    def test_v13_legacy_mail_is_readable_and_cannot_supply_a_reply_route(self):
        old_db = str(Path(self.db).with_name("v13.db"))
        legacy = self.chat("codex", None)
        con = sqlite3.connect(old_db)
        with con:
            con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,project TEXT,sender TEXT,"
                        "recipient TEXT,body TEXT,created_at REAL)")
            con.execute("INSERT INTO messages VALUES(1, ?, 'deepseek@demo','codex','old mail',1)",
                        (legacy.project.key,))
            con.execute("PRAGMA user_version=13")
        con.close()
        upgraded = Store(old_db)
        self.addCleanup(upgraded.close)
        receiver = Session(upgraded, agent_name="codex", cwd=self.root)
        self.assertIn("old mail", receiver.inbox())
        with contextlib.suppress(ValueError):
            receiver.post("unsafe reply", reply_to=1)
        self.assertEqual(upgraded.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
