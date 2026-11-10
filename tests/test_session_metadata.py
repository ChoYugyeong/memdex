"""Task metadata must identify the native chat, not merely an agent name and C:\\Dev."""

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
from memdex.project import resolve
from memdex.store import Store
from memdex.tools import Session


class SessionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name, "workspace"))
        Path(self.root).mkdir()
        env = mock.patch.dict(os.environ, {"MEMDEX_PROJECT": self.root, "MEMDEX_AGENT": "codex",
                                          "MEMDEX_EMBED": ""})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(str(Path(self.tmp.name, "memory.db")))
        self.addCleanup(self.store.close)
        self.project = resolve(self.root)
        self.refresh = mock.patch("memdex.tools.skills.maybe_refresh")
        self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def chat(self, chat_id):
        s = Session(self.store, agent_name="codex", cwd=self.root, conversation_id=chat_id)
        s.home = self.tmp.name
        return s

    def call(self, server, name, args=None, meta=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return mcp.handle(server, {t.name: t for t in server.tools()},
                              {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args or {}, "_meta": meta or {}}})

    def test_native_id_is_kept_and_not_reassigned(self):
        a = self.chat("native-review-chat")
        a.session_routes("Review materials")
        row = self.store.get_route(self.project, a.route)
        self.assertEqual(row["conversation_id"], "native-review-chat")
        self.assertIn('chat_id="native-review-chat"', a.session_routes(query=a.route))
        with self.assertRaises(ValueError):
            self.store.register_route(self.project, a.route, a.agent, conversation_id="another-chat")

    def test_v16_routes_are_upgraded_without_guessing_the_old_native_id(self):
        db = str(Path(self.tmp.name, "v16.db"))
        con = sqlite3.connect(db)
        with con:
            con.execute("CREATE TABLE chat_routes(project TEXT,route TEXT,agent TEXT,checkout TEXT,"
                        "label TEXT,last_seen_at REAL,PRIMARY KEY(project,route))")
            con.execute("INSERT INTO chat_routes VALUES(?,?,?,?,?,?)",
                        (self.project.key, "route:" + "a" * 32, "codex@workspace", self.project.root, "old task", 1))
            con.execute("PRAGMA user_version=16")
        con.close()
        migrated = Store(db)
        try:
            self.assertEqual(migrated.schema_version, 17)
            row = migrated.get_route(self.project, "route:" + "a" * 32)
            self.assertIsNone(row["conversation_id"])
            self.assertEqual(row["label"], "old task")
            migrated.register_route(self.project, row["route"], row["agent"], conversation_id="host-reappeared")
            self.assertEqual(migrated.get_route(self.project, row["route"])["conversation_id"], "host-reappeared")
        finally:
            migrated.close()

    def test_task_brief_registers_its_task_instead_of_an_empty_label(self):
        a = self.chat("review-materials")
        a.brief(task="Prepare review materials")
        row = self.store.get_route(self.project, a.route)
        self.assertEqual(row["label"], "Prepare review materials")
        self.assertEqual(row["conversation_id"], "review-materials")
        a.inbox(peek=True)
        self.assertEqual(self.store.get_route(self.project, a.route)["label"], "Prepare review materials")

    def test_host_task_metadata_is_scoped_per_call_and_explicit_task_takes_precedence(self):
        server = self.chat(None)
        self.call(server, "session_routes", meta={"threadId": "review", "memdex.dev/task-label": "Review materials"})
        self.call(server, "session_routes", meta={"threadId": "other"})
        self.assertEqual(self.store.get_route(self.project, self.chat("other").route)["label"], "")
        self.call(server, "project_brief", {"task": "AgentKube design"},
                  {"threadId": "review", "memdex.dev/task-label": "Review materials"})
        self.assertEqual(self.store.get_route(self.project, self.chat("review").route)["label"], "AgentKube design")
        self.assertIsNone(server.conversation_id)
        self.assertIsNone(server.conversation_label)

    def test_bad_host_metadata_creates_no_route_and_cannot_claim_a_chat(self):
        server = self.chat(None)
        for meta in ({"memdex.dev/task-label": "task"},
                     {"threadId": "a", "memdex.dev/task-label": ["task"]},
                     {"threadId": "a", "memdex.dev/task-label": "bad\nspoof"}):
            out = self.call(server, "session_routes", meta=meta)
            self.assertIn("error", out)
        self.assertEqual(self.store.route_rows(self.project), [])

    def test_a_retired_same_task_does_not_hide_the_only_active_labelled_chat(self):
        a, retired = self.chat("active-native"), self.chat("retired-native")
        a.session_routes("AgentKube design review")
        retired.session_routes("AgentKube design review")
        with self.store.db:
            self.store.db.execute("UPDATE chat_routes SET last_seen_at=0 WHERE route=?", (retired.route,))
        out = self.chat("viewer").session_routes(query="AgentKube design")
        self.assertIn(a.route, out)
        self.assertNotIn(retired.route, out)
        self.assertIn("inactive", out)

    def test_multiple_active_tasks_stay_ambiguous_and_a_bare_workspace_is_not_a_task(self):
        a, b = self.chat("a"), self.chat("b")
        a.session_routes("Review materials")
        b.session_routes("AgentKube design review")
        out = self.chat("viewer").session_routes(query="review")
        self.assertIn("ambiguous", out)
        self.assertNotIn(a.route, out)
        self.assertNotIn(b.route, out)
        self.assertIn('chat_id="a"', out)  # exact native IDs allow inspecting the actual chat before choosing
        self.assertIn(a.route, self.chat("viewer").session_routes(query="materials"))

    def test_a_different_retired_task_still_makes_a_weak_cue_ambiguous(self):
        a, b = self.chat("a"), self.chat("b")
        a.session_routes("AgentKube review")
        b.session_routes("Materials review")
        with self.store.db:
            self.store.db.execute("UPDATE chat_routes SET last_seen_at=0 WHERE route=?", (b.route,))
        out = self.chat("viewer").session_routes(query="review")
        self.assertIn("ambiguous", out)
        self.assertNotIn(a.route, out)

    def test_native_id_can_be_used_to_inspect_the_exact_registered_chat(self):
        a = self.chat("known-native-id")
        a.session_routes("Prepare review materials")
        self.assertIn(a.route, self.chat("viewer").session_routes(query="known-native-id"))

    def test_unlabelled_destinations_are_not_offered_for_automatic_selection(self):
        a = self.chat("unknown-work")
        a.session_routes()
        out = self.chat("viewer").session_routes(query="codex")
        self.assertIn("unlabelled", out)
        self.assertNotIn(a.route, out)

    def test_inbox_names_source_chat_and_reply_remains_with_the_original_native_chat(self):
        source, target, unrelated = [self.chat(x) for x in ("AgentKube-chat", "requested-review", "materials-chat")]
        for s, label in ((source, "AgentKube design"), (target, "Requested design review"),
                         (unrelated, "Prepare review materials")):
            s.session_routes(label)
        source.post("Please review this design", to=target.route)
        mid = self.store.db.execute("SELECT max(id) FROM messages").fetchone()[0]
        received = target.inbox()
        self.assertIn('task="AgentKube design"', received)
        self.assertIn('chat_id="AgentKube-chat"', received)
        self.assertIn(f"reply_to={mid}", received)
        self.assertNotIn("Please review", unrelated.inbox())
        target.post("Design review result", reply_to=mid)
        self.assertIn("Design review result", source.inbox())
        self.assertNotIn("Design review result", unrelated.inbox())

    def test_new_metadata_columns_do_not_change_existing_route_addresses(self):
        a = self.chat("stable-native")
        before = a.route
        a.session_routes("first task")
        a.session_routes("second task")
        self.assertEqual(a.route, before)
        self.assertEqual(self.store.get_route(self.project, before)["label"], "second task")

    def test_cli_task_registers_the_same_native_chat_as_mcp(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "cli-native", "MEMDEX_SKILL_HOME": self.tmp.name}):
            p = subprocess.run([sys.executable, "-m", "memdex", "brief", "--db", self.store.path,
                                "--path", self.root, "--agent", "codex", "--task", "Review materials"],
                               cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        row = self.store.get_route(self.project, self.chat("cli-native").route)
        self.assertEqual(row["label"], "Review materials")
        self.assertEqual(row["conversation_id"], "cli-native")

    def task(self, title):
        return self.store.create_task(self.project, "codex", title, "Observed test evidence")

    def test_same_label_does_not_authorize_cross_task_dispatch(self):
        project_task, materials_task = self.task("AgentKube design"), self.task("Prepare materials")
        source, wrong = self.chat("source"), self.chat("wrong")
        source.session_routes(label="review", task_id=project_task)
        wrong.session_routes(label="review", task_id=materials_task)
        with self.assertRaisesRegex(ValueError, "another task"):
            source.post("Do the design review", to=wrong.route)
        with self.assertRaisesRegex(ValueError, "another task"):
            source.post("Do the design review", to=wrong.route, task_id=project_task)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_new_work_cannot_be_assigned_to_an_unscoped_idle_chat(self):
        task = self.task("Design")
        source, unknown = self.chat("source"), self.chat("unknown")
        source.session_routes(task_id=task)
        unknown.session_routes(label="Design")
        with self.assertRaisesRegex(ValueError, "unassigned"):
            source.post("Review", to=unknown.route)

    def test_task_binding_requires_a_real_open_task_in_the_same_project(self):
        with self.assertRaisesRegex(ValueError, "open task"):
            self.chat("new").session_routes(task_id=999999)

    def test_matched_task_dispatch_and_correlated_reply_do_not_need_title_matching(self):
        task = self.task("Design")
        source, target = self.chat("source"), self.chat("target")
        source.session_routes(task_id=task)
        target.session_routes(task_id=task)
        source.post("Please review", to=target.route)
        mid = self.store.db.execute("SELECT max(id) FROM messages").fetchone()[0]
        self.assertIn("Please review", target.inbox())
        # Reply routing stays tied to the original message even after the review chat changes its task.
        target.session_routes(task_id=self.task("Other work"))
        target.post("Review done", reply_to=mid)
        self.assertIn("Review done", source.inbox())
        self.assertIn(source.route, target.session_routes(query=f"task:{task}"))

    def test_lost_receipt_is_recovered_even_after_the_destination_changes_its_task(self):
        task = self.task("Design")
        source, target = self.chat("source"), self.chat("target")
        source.session_routes(task_id=task)
        target.session_routes(task_id=task)
        receipt = source.post("Please review", to=target.route, request_key="review-1")
        target.session_routes(task_id=self.task("Other work"))
        self.assertEqual(source.post("Please review", to=target.route, request_key="review-1"), receipt)
        with self.assertRaises(ValueError):
            source.post("Please review again", to=target.route, request_key="review-2")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
