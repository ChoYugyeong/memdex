"""Durable exact mail can wake only its task-bound native recipient. All sends are fake."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from memdex import reason_notify
from memdex.project import resolve
from memdex.reason_guard import Guard
from memdex.store import Store
from memdex.tools import Session


class NotificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(str(self.root / "memory.db"))
        self.addCleanup(self.store.close)
        self.reason_file = self.root / "network.db"
        self.reason = sqlite3.connect(str(self.reason_file))
        self.addCleanup(self.reason.close)
        with self.reason:
            self.reason.execute("CREATE TABLE sessions(name TEXT,key TEXT,agent TEXT)")
            self.reason.executemany("INSERT INTO sessions VALUES(?,?,?)", [
                ("source", "claude-code:source-id", "claude-code"),
                ("target", "codex:target-id", "codex"),
                ("wrong", "codex:wrong-id", "codex")])
        self.backend = self.root / "fake-backend"
        self.backend.touch()
        env = mock.patch.dict(os.environ, {"MEMDEX_AGENT": "", "MEMDEX_REASON_NOTIFY": "1",
                              "MEMDEX_REASON_BACKEND": str(self.backend),
                              "MEMDEX_REASON_DB": str(self.reason_file), "MEMDEX_EMBED": ""})
        env.start()
        self.addCleanup(env.stop)
        self.project = resolve(str(self.root))
        self.source = Session(self.store, agent_name="claude", _project=self.project, conversation_id="source-id")
        self.dest = Session(self.store, agent_name="codex", _project=self.project, conversation_id="target-id")
        self.task = self.store.create_task(self.project, "claude", "message delivery", "exact recipient receives")
        self.source.session_routes(task_id=self.task)
        self.dest.session_routes(task_id=self.task)
        self.snapshot = {"person": "owner", "machine": "desktop", "sessions": [
            {"name": "source", "is_me": True}, {"name": "target", "is_me": False}]}
        patch = mock.patch.object(Guard, "who", side_effect=lambda: self.snapshot)
        patch.start()
        self.addCleanup(patch.stop)
        self.exchange = mock.patch("memdex.reason_notify._exchange", return_value={
            "content": [{"type": "text", "text": "queued; message id " + "A" * 26}]}).start()
        self.addCleanup(mock.patch.stopall)

    def test_exact_post_commits_before_notification_and_does_not_consume_the_inbox(self):
        def send(backend, args, meta, environment=None):
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(args["to"], "owner/target@desktop")
            self.assertEqual(meta, {"memdex.dev/conversation-id": "source-id"})
            self.assertEqual(environment["CLAUDE_SESSION_ID"], "source-id")
            self.assertNotIn("PRIVATE REVIEW BODY", json.dumps(args))
            return {"content": [{"type": "text", "text": "message id " + "A" * 26}]}
        self.exchange.side_effect = send
        receipt = self.source.post("PRIVATE REVIEW BODY", to=self.dest.route)
        self.assertIn("Reason notification accepted", receipt)
        self.assertIn("read is not yet confirmed", receipt)
        self.assertIsNone(self.store.db.execute("SELECT last_id FROM cursors WHERE agent=?", (self.dest.route,)).fetchone())
        self.assertIn("PRIVATE REVIEW BODY", self.dest.inbox())

    def test_retry_keeps_one_mail_and_one_notification_idempotency_key(self):
        args = {"to": self.dest.route, "request_key": "lost-receipt"}
        first = self.source.post("review", **args)
        second = self.source.post("review", **args)
        self.assertEqual(first, second)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        sent = [call.args[1]["idempotency_key"] for call in self.exchange.call_args_list]
        self.assertEqual(len(set(sent)), 1)

    def test_different_task_never_reaches_the_transport(self):
        other = self.store.create_task(self.project, "codex", "other work", "separate")
        self.dest.session_routes(task_id=other)
        with self.assertRaisesRegex(ValueError, "another task"):
            self.source.post("review", to=self.dest.route)
        self.exchange.assert_not_called()

    def test_missing_binding_is_honestly_reported_and_the_exact_mail_remains(self):
        with self.store.db:
            self.store.db.execute("UPDATE chat_routes SET task_id=NULL")
        receipt = self.source.post("review", to=self.dest.route)
        self.assertIn("notification not delivered", receipt)
        self.assertIn("session_routes(task_id=...)", receipt)
        self.exchange.assert_not_called()
        self.assertIn("review", self.dest.inbox())

    def test_timeout_and_audit_failure_do_not_turn_committed_mail_into_a_tool_error(self):
        self.exchange.side_effect = subprocess.TimeoutExpired("fake", 1)
        with mock.patch.object(Guard, "audit", side_effect=sqlite3.OperationalError("locked")):
            receipt = self.source.post("keep this", to=self.dest.route)
        self.assertIn("remains queued", receipt)
        self.assertIn("keep this", self.dest.inbox())

    def test_a_transport_error_is_not_reported_as_a_wake(self):
        self.exchange.return_value = {"isError": True, "content": [{"type": "text", "text": "message id " + "A"*26}]}
        receipt = self.source.post("keep this", to=self.dest.route)
        self.assertIn("notification not delivered", receipt)
        self.assertNotIn("notification accepted", receipt)
        self.assertIn("keep this", self.dest.inbox())

    def test_backend_identity_must_match_the_posting_chat(self):
        self.snapshot["sessions"] = [{"name": "wrong", "is_me": True}]
        self.assertIn("notification not delivered", self.source.post("review", to=self.dest.route))
        self.exchange.assert_not_called()

    def test_reused_reason_name_cannot_redirect_to_a_different_native_chat_on_the_same_task(self):
        replacement = Session(self.store, agent_name="codex", _project=self.project,
                              conversation_id="replacement-id")
        replacement.session_routes(task_id=self.task)
        authorize = Guard.authorize
        def swap(guard, args, meta):
            with self.reason:
                self.reason.execute("UPDATE sessions SET key='codex:replacement-id' WHERE name='target'")
            return authorize(guard, args, meta)
        with mock.patch.object(Guard, "authorize", autospec=True, side_effect=swap):
            receipt = self.source.post("review", to=self.dest.route)
        self.assertIn("notification not delivered", receipt)
        self.exchange.assert_not_called()

    def test_custom_database_does_not_contact_the_live_service_by_default(self):
        with mock.patch.dict(os.environ, {"MEMDEX_REASON_NOTIFY": "auto"}):
            receipt = self.source.post("review", to=self.dest.route)
        self.assertNotIn("notification", receipt)
        self.exchange.assert_not_called()

    def test_bare_route_hash_is_never_a_legacy_family_mailbox(self):
        for group in (False, True):
            with self.subTest(group=group), self.assertRaisesRegex(ValueError, "missing 'route:'"):
                legacy = Session(self.store, agent_name="claude", _project=self.project)
                legacy.post("must not vanish", to=self.dest.route.partition(":")[2], group=group)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.exchange.assert_not_called()

    def test_store_also_refuses_a_truncated_exact_address(self):
        for to in ("b"*32, "route:bad", "route:" + "a"*31):
            with self.subTest(to=to), self.assertRaises(ValueError):
                self.store.post(self.project, "claude", "no", to=to)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_native_caller_resolution_uses_the_exact_backend_identity_and_provider(self):
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(self.snapshot).encode())
        with mock.patch("memdex.reason_notify.default_path", return_value=self.store.path), \
                mock.patch("memdex.reason_notify.subprocess.run", return_value=proc):
            self.assertEqual(reason_notify.calling_chat(self.store, "claude"), "source-id")
            self.assertIsNone(reason_notify.calling_chat(self.store, "gpt"))
            self.snapshot["sessions"].append({"name": "wrong", "is_me": True})
            proc.stdout = json.dumps(self.snapshot).encode()
            self.assertIsNone(reason_notify.calling_chat(self.store, "claude"))


class ExchangeTests(unittest.TestCase):
    def test_real_stdio_exchange_uses_a_fake_backend_and_reaps_its_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "fake_backend.py")
            script.write_text('''import json, sys
for line in sys.stdin:
    req=json.loads(line)
    if 'id' not in req: continue
    result={'serverInfo':{'name':'fake'}} if req['method']=='initialize' else {'content':[{'type':'text','text':'message id '+'B'*26}]}
    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}),flush=True)
''', encoding="utf-8")
            launch = subprocess.Popen
            children = []
            def spawn(cmd, **kwargs):
                proc = launch([sys.executable, str(script)], **kwargs)
                children.append(proc)
                return proc
            with mock.patch("memdex.reason_notify.subprocess.Popen", side_effect=spawn):
                result = reason_notify._exchange("fake", {"to": "owner/target", "message": "pointer"}, {})
            self.assertIn("message id", result["content"][0]["text"])
            self.assertTrue(all(child.poll() is not None for child in children))


if __name__ == "__main__":
    unittest.main()
