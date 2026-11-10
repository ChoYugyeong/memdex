"""Reason routing decisions use native keys/project/task bindings; no real sends in these tests."""

import json
import hashlib
import io
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memdex.project import resolve
from memdex.reason_guard import Guard, Refused, decorate, proxy
from memdex.store import Store


class ReasonGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = resolve(str(root))
        self.memory = Store(str(root / "memory.db"))
        self.addCleanup(self.memory.close)
        self.reason_path = root / "reason.db"
        self.reason = sqlite3.connect(str(self.reason_path))
        self.addCleanup(self.reason.close)
        with self.reason:
            self.reason.executescript("CREATE TABLE sessions(name TEXT,key TEXT,agent TEXT);"
                                      "CREATE TABLE messages(id TEXT,sender TEXT,recipient TEXT);")
            self.reason.executemany("INSERT INTO sessions VALUES(?,?,?)", [
                ("source", "claude-code:source-id", "claude-code"),
                ("target", "codex:target-id", "codex"),
                ("wrong", "codex:wrong-id", "codex"),
                ("profile", "proc:12345", "dsh")])
        self.snapshot = {"person": "owner", "machine": "desktop", "sessions": [
            {"name": "source", "is_me": True}]}
        self.guard = Guard("unused", self.reason_path, self.memory.path, root / "guard.db",
                           inventory=lambda: self.snapshot)
        self.addCleanup(self.guard.close)
        self.task = self.memory.create_task(self.project, "owner", "Project A", "Verified result")
        self.other = self.memory.create_task(self.project, "owner", "Project B", "Different result")

    def bind(self, raw, provider, task=None, project=None):
        route = "route:" + hashlib.sha256((provider + raw).encode()).hexdigest()[:32]
        self.memory.register_route(project or self.project, route, provider + "@workspace", "same title",
                                   conversation_id=raw, task_id=task or self.task)
        return route

    def authorize(self, **args):
        return self.guard.authorize(dict(to="owner/target", **args), {})

    def ready(self):
        self.bind("source-id", "claude")
        self.bind("target-id", "gpt")

    def original(self, id="original", sender="owner/source", recipient="owner/target"):
        with self.reason:
            self.reason.execute("INSERT INTO messages VALUES(?,?,?)", (id, sender, recipient))

    def test_missing_metadata_refuses_even_if_names_titles_and_workspace_look_right(self):
        with self.assertRaisesRegex(Refused, "binding"):
            self.authorize()

    def test_same_open_task_and_project_native_keys_are_allowed(self):
        self.ready()
        proof = self.authorize(task_id=self.task)
        self.assertEqual((proof["project"], proof["task_id"]), (self.project.key, self.task))
        self.assertEqual(proof["target"], "codex:target-id")

    def test_same_title_different_work_is_refused(self):
        self.bind("source-id", "claude")
        self.bind("target-id", "gpt", self.other)
        with self.assertRaisesRegex(Refused, "mismatch"):
            self.authorize()

    def test_task_cannot_be_overridden_with_an_argument(self):
        self.ready()
        with self.assertRaisesRegex(Refused, "binding"):
            self.authorize(task_id=self.other)

    def test_missing_target_native_session_never_falls_back_to_another_idle_one(self):
        self.ready()
        with self.assertRaisesRegex(Refused, "missing/ambiguous"):
            self.guard.authorize({"to": "owner/absent"}, {})

    def test_pid_only_profile_does_not_prove_a_native_chat(self):
        self.ready()
        with self.assertRaisesRegex(Refused, "native session"):
            self.guard.authorize({"to": "owner/profile"}, {})

    def test_host_backend_identity_conflict_refuses_instead_of_using_a_reused_default(self):
        self.ready()
        self.snapshot["sessions"] = [{"name": "wrong", "is_me": True}]
        with self.assertRaisesRegex(Refused, "identity mismatch"):
            self.guard.authorize({"to": "owner/target"}, {"memdex.dev/conversation-id": "source-id"})

    def test_conflicting_native_ids_and_model_supplied_sender_are_refused(self):
        self.ready()
        for args, meta in (({"session_id": "source-id"}, {}),
                           ({}, {"threadId": "source-id", "memdex.dev/conversation-id": "other-id"})):
            with self.assertRaises(Refused):
                self.guard.authorize(dict(to="owner/target", **args), meta)

    def test_missing_source_never_uses_a_title_to_guess(self):
        self.ready()
        self.snapshot["sessions"] = []
        with self.assertRaises(Refused):
            self.authorize()

    def test_duplicate_native_id_is_not_an_unambiguous_identity(self):
        self.ready()
        with self.reason:
            self.reason.execute("INSERT INTO sessions VALUES('clone','codex:source-id','codex')")
        with self.assertRaises(Refused):
            self.guard.authorize({"to": "owner/target"}, {"threadId": "source-id"})

    def test_closed_task_refuses_new_work(self):
        self.ready()
        with self.memory.db:
            self.memory.db.execute("UPDATE tasks SET status='verified' WHERE id=?", (self.task,))
        with self.assertRaises(Refused):
            self.authorize()

    def test_same_raw_id_under_another_provider_does_not_authorize_a_chat(self):
        self.bind("source-id", "claude")
        self.bind("target-id", "claude")
        with self.assertRaises(Refused):
            self.authorize()

    def test_wildcard_family_and_remote_metadata_guesses_are_refused(self):
        self.ready()
        for destination in ("*", "repo:demo", "owner/*", "other/target", "owner/target@remote"):
            with self.subTest(destination=destination), self.assertRaises(Refused):
                self.guard.authorize({"to": destination}, {})
        self.assertEqual(self.guard.authorize({"to": "owner/target@desktop"}, {})["task_id"], self.task)

    def test_reply_must_match_original_endpoints(self):
        self.ready()
        self.original(sender="owner/wrong")
        with self.assertRaisesRegex(Refused, "endpoints"):
            self.authorize(reply_to="original")

    def test_unknown_and_legacy_wrong_task_reply_are_not_grandfathered(self):
        with self.assertRaises(Refused):
            self.authorize(reply_to="absent")
        self.original()
        with self.assertRaisesRegex(Refused, "binding"):
            self.authorize(reply_to="original")

    def test_approved_reply_finishes_after_tasks_change_but_not_after_native_name_reuse(self):
        self.ready()
        proof = self.authorize()
        mid = "0" * 26
        self.guard.receipt({"content": [{"type": "text", "text": "message id " + mid}]}, proof)
        self.original(id=mid)
        with self.memory.db:
            self.memory.db.execute("UPDATE tasks SET status='verified' WHERE id=?", (self.task,))
        self.assertEqual(self.authorize(reply_to=mid)["task_id"], self.task)
        with self.assertRaisesRegex(Refused, "another task"):
            self.authorize(reply_to=mid, task_id=self.other)
        with self.reason:
            self.reason.execute("UPDATE sessions SET key='codex:replacement-id' WHERE name='target'")
        with self.assertRaises(Refused):
            self.authorize(reply_to=mid)

    def test_malformed_or_missing_database_metadata_fails_closed(self):
        self.ready()
        self.guard.memdex_db = str(Path(self.tmp.name, "missing.db"))
        with self.assertRaisesRegex(Refused, "metadata"):
            self.authorize()

    def test_schema_decoration_adds_a_task_id_but_no_extra_tool(self):
        response = {"result": {"tools": [{"name": "send_message", "description": "send", "inputSchema": {}}]}}
        decorated = decorate(response)["result"]["tools"]
        self.assertEqual(len(decorated), 1)
        self.assertIn("task_id", decorated[0]["inputSchema"]["properties"])
        init = decorate({"result": {"serverInfo": {}, "instructions": "original"}})
        self.assertIn("original", init["result"]["instructions"])
        self.assertIn("guard", init["result"]["instructions"])

    def test_audit_records_metadata_only_and_receipt_errors_do_not_approve(self):
        self.guard.audit("owner/target", False, "project/task mismatch")
        proof = {"source": "s", "target": "t", "project": "p", "task_id": 1}
        self.guard.receipt({"isError": True, "content": [{"type": "text", "text": "message id " + "0" * 26}]}, proof)
        self.assertEqual(self.guard.db.execute("SELECT count(*) FROM approved").fetchone()[0], 0)
        self.assertEqual(len(self.guard.db.execute("SELECT * FROM audit").fetchone()), 4)

    def protocol(self, request):
        class Pipe(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        downstream = Pipe()
        child = mock.Mock(stdin=downstream, stdout=io.BytesIO())
        child.wait.return_value = 0
        output = io.BytesIO()
        with mock.patch("memdex.reason_guard.subprocess.Popen", return_value=child), \
                mock.patch("memdex.reason_guard.sys.stdin", SimpleNamespace(buffer=io.BytesIO(
                    (json.dumps(request) + "\n").encode()))), \
                mock.patch("memdex.reason_guard.sys.stdout", SimpleNamespace(buffer=output)):
            proxy(self.guard)
        return downstream.saved, output.getvalue()

    def test_denial_never_reaches_backend_even_as_a_stdio_call(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "send_message", "arguments": {"to": "owner/target", "message": "DO NOT SEND"}}}
        sent, output = self.protocol(request)
        self.assertEqual(sent, b"")
        self.assertTrue(json.loads(output)["result"]["isError"])
        self.assertNotIn("DO NOT SEND", output.decode())

    def test_allowed_stdio_call_strips_only_guard_argument_and_preserves_host_metadata(self):
        self.ready()
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "send_message", "arguments": {"to": "owner/target", "message": "review", "task_id": self.task},
            "_meta": {"memdex.dev/conversation-id": "source-id"}}}
        sent, _ = self.protocol(request)
        forwarded = json.loads(sent)
        self.assertNotIn("task_id", forwarded["params"]["arguments"])
        self.assertEqual(forwarded["params"]["_meta"], request["params"]["_meta"])
        self.assertEqual(forwarded["params"]["arguments"]["message"], "review")


if __name__ == "__main__":
    unittest.main()
