"""Exact recipients and replies remain stable when the sender retries a committed post."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from memdex import mcp
from memdex.project import resolve
from memdex.store import Store
from memdex.tools import Session


class PostRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.file = str(Path(self.root, "memdex.db"))
        self.db = self.open_store()
        self.project = resolve(self.root)
        patch = mock.patch.dict(os.environ, {"MEMDEX_AGENT": "codex", "MEMDEX_PROJECT": self.root})
        patch.start()
        self.addCleanup(patch.stop)
        self.a, self.b, self.c = [self.chat(x) for x in ("first", "second", "third")]
        for chat in (self.a, self.b, self.c):
            chat.session_routes()

    def open_store(self):
        store = Store(self.file)
        self.addCleanup(store.close)
        return store

    def chat(self, conversation, store=None):
        return Session(store or self.db, _project=self.project, conversation_id=conversation)

    def test_lost_response_replayed_after_reconnect_through_cli_and_mcp(self):
        # Treat the successful CLI response as lost. A fresh server must return the same ID.
        result = subprocess.run([sys.executable, "-m", "memdex", "post", "same", "--to", self.b.route,
                                 "--request-key", "request-A", "--path", self.root, "--db", self.file],
                                cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, CODEX_THREAD_ID="first"),
                                capture_output=True, text=True, check=True, timeout=15)
        server = self.chat(None, self.open_store())
        params = {"name": "post", "arguments": {"message": "same", "to": self.b.route,
                  "request_key": "request-A"}, "_meta": {"threadId": "first"}}
        reply = mcp.handle(server, {t.name: t for t in server.tools()},
                           {"method": "tools/call", "id": 77, "params": params})["result"]
        self.assertFalse(reply["isError"])
        self.assertEqual(result.stdout.strip(), reply["content"][0]["text"])
        self.assertEqual(self.b.inbox().count("): same"), 1)
        self.assertEqual(self.c.inbox(), "no unread messages.")

    def test_changed_task_snapshot_reuses_the_original_but_changed_intent_refuses(self):
        tid = self.db.create_task(self.project, self.a.agent, "deliver", "accepted")
        kwargs = {"to": self.b.route, "task_id": tid, "request_key": "r-1"}
        receipt = self.a.post("check", **kwargs)
        self.db.task_action(self.project, self.a.agent, tid, "start")
        self.assertEqual(self.a.post("check", **kwargs), receipt)
        for update in ({"to": self.c.route}, {"task_id": None}, {"message": "different"}):
            args = dict({"message": "check"}, **kwargs)
            args.update(update)
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, "already used"):
                self.a.post(**args)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertIn("[todo]", self.b.inbox())

    def test_reply_to_and_independent_operations(self):
        one, two = [self.db.post(self.project, self.b.agent, "question", sender_route=self.b.route,
                                 recipient_route=self.a.route) for _ in range(2)]
        receipt = self.a.post("answer", reply_to=one, request_key="one")
        self.assertEqual(self.a.post("answer", reply_to=one, request_key="one"), receipt)
        with self.assertRaisesRegex(ValueError, "already used"):
            self.a.post("answer", reply_to=two, request_key="one")
        self.assertNotEqual(self.a.post("answer", reply_to=one, request_key="two"), receipt)
        self.assertNotEqual(self.c.post("answer", to=self.b.route, request_key="one"), receipt)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 5)

    def test_concurrent_connections_queue_one_message(self):
        senders = [self.chat("first", self.open_store()) for _ in range(2)]
        gate = threading.Barrier(2)
        def send(chat):
            gate.wait(timeout=10)
            return chat.post("one operation", to=self.b.route, request_key="concurrent")
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(send, senders))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_delayed_reply_retry_survives_label_reuse_and_reconnect(self):
        self.a.session_routes("routing owner")
        mid = self.db.post(self.project, self.a.agent, "review", sender_route=self.a.route,
                           recipient_route=self.b.route)
        self.assertIn("review", self.b.inbox())
        receipt = self.b.post("review done", reply_to=mid, request_key="late-reply")
        with self.db.db:
            self.db.db.execute("UPDATE chat_routes SET last_seen_at=0 WHERE route=?", (self.a.route,))
        self.c.session_routes("routing owner")
        self.assertEqual(self.chat("second", self.open_store()).post(
            "review done", reply_to=mid, request_key="late-reply"), receipt)
        self.assertEqual(self.c.inbox(), "no unread messages.")
        reopened = self.chat("first", self.open_store())
        self.assertEqual(reopened.inbox().count("review done"), 1)
        self.assertEqual(reopened.inbox(), "no unread messages.")

    def test_keys_require_an_exact_recipient_and_a_bounded_identifier(self):
        for bad in ("", "a"*129, "two words", "x\n", False):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                self.a.post("unsafe", to=self.b.route, request_key=bad)
        with self.assertRaisesRegex(ValueError, "exact"):
            self.a.post("group", group=True, request_key="abc")
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_v14_message_without_request_key_survives_and_unkeyed_repeats_stay_distinct(self):
        old_path = str(Path(self.root, "v14.db"))
        con = sqlite3.connect(old_path)
        with con:
            con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, project TEXT, sender TEXT, "
                        "recipient TEXT, body TEXT,created_at REAL,sender_route TEXT,recipient_route TEXT,reply_to INTEGER)")
            con.execute("INSERT INTO messages(id,project,sender,body) VALUES(1,?,?,?)",
                        (self.project.key, "other", "before"))
            con.execute("PRAGMA user_version=14")
        con.close()
        upgraded = Store(old_path)
        self.addCleanup(upgraded.close)
        self.assertIsNone(upgraded.db.execute("SELECT request_key FROM messages WHERE body='before'").fetchone()[0])
        self.assertNotEqual(upgraded.post(self.project, "sender", "new"),
                            upgraded.post(self.project, "sender", "new"))
