import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.cli import main
from memdex.mcp import handle
from memdex.store import SCHEMA_VERSION, Store
from memdex.tools import Session


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("x = 1\n", encoding="utf-8")
        self.db_path = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db_path)
        self.project = projects.resolve(self.root)
        self.old_agent = os.environ.pop("MEMDEX_AGENT", None)

    def tearDown(self):
        if self.old_agent is not None:
            os.environ["MEMDEX_AGENT"] = self.old_agent
        else:
            os.environ.pop("MEMDEX_AGENT", None)
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, agent="codex"):
        session = Session(self.store, agent_name=agent)
        session.cwd = self.root
        return session

    def test_separate_blocked_and_active_children_do_not_greenwash_parent(self):
        s = self.session()
        root = s.store.create_task(self.project, s.agent, "Prepare portfolio", "All requested outcomes checked")
        integration = s.store.create_task(self.project, s.agent, "Add DeepSeek MCP", "Connection is visible", root)
        compression = s.store.create_task(self.project, s.agent, "Compress portfolio", "Draft is prepared", root)
        s.tasks("start", root)
        s.tasks("block", integration, reason="No DeepSeek connector is registered in this session")
        s.tasks("start", compression)

        summary = s.brief()
        self.assertIn("work ledger (reported ≠ checked)", summary)
        self.assertIn("Add DeepSeek MCP", summary)
        self.assertIn("blocked: No DeepSeek connector", summary)
        self.assertIn("Compress portfolio", summary)
        with self.assertRaisesRegex(ValueError, "unchecked children"):
            s.tasks("report", root, evidence="Compression draft saved")

    def test_reports_need_evidence_and_peer_check_limit_is_clear(self):
        reporter, reviewer = self.session("claude"), self.session("codex")
        task_id = reporter.store.create_task(self.project, reporter.agent, "Run the checks", "All checks pass")
        reporter.tasks("start", task_id)
        unrelated = reporter.store.create_task(self.project, reporter.agent, "Add DeepSeek MCP", "MCP tools appear")
        reporter.tasks("block", unrelated, reason="No connector is registered")
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            reporter.tasks("report", task_id)
        self.assertIn("reported", reporter.tasks("report", task_id, evidence="pytest: 12 passed"))
        self.assertIn("completion report", reporter.tasks("status", task_id))

        posted = reporter.post("The tests passed", to="codex", task_id=task_id)
        self.assertIn("posted #", posted)
        inbox = reviewer.inbox()
        self.assertIn("[task status at send]", inbox)
        self.assertIn("[reported]", inbox)
        self.assertIn("project task ledger at send", inbox)
        self.assertIn("Add DeepSeek MCP", inbox)
        self.assertIn("blocked: No connector is registered", inbox)
        checked = reviewer.tasks("verify", task_id, evidence="Re-ran pytest: all 12 passed")
        self.assertIn("agent name is self-declared, not authenticated", checked)
        self.assertIn("[verified]", checked)

    def test_parent_requires_checked_children_and_new_work_reopens_ancestors(self):
        reporter, reviewer = self.session("claude"), self.session("codex")
        parent = reporter.store.create_task(self.project, reporter.agent, "Ship feature", "Feature works")
        child = reporter.store.create_task(self.project, reporter.agent, "Implement", "Feature implemented", parent)
        reporter.tasks("start", parent)
        reporter.tasks("start", child)
        reporter.tasks("report", child, evidence="diff contains implementation")
        reporter.tasks("verify", child, evidence="reviewed diff")  # same agent is labeled self-verified
        self.assertIn("self-checked", reporter.tasks("status", child))
        reporter.tasks("report", parent, evidence="all child outcomes checked")
        reviewer.tasks("verify", parent, evidence="reviewed end-to-end result")
        status = reviewer.tasks("status", parent)
        self.assertIn("[verified]", status)
        self.assertIn("identity not authenticated", status)

        reporter.store.create_task(self.project, reporter.agent, "Add docs", "Usage documented", parent)
        reopened = reporter.tasks("status", parent)
        self.assertIn("[in progress]", reopened)
        self.assertIn("1/2 leaf tasks checked", reopened)
        events = reporter.tasks("history", parent)
        self.assertIn("auto_reopened", events)

    def test_handoff_keeps_blockers_and_pending_work_visible(self):
        s = self.session()
        parent = s.store.create_task(self.project, s.agent, "Portfolio task", "All branches checked")
        child = s.store.create_task(self.project, s.agent, "Connect DeepSeek", "MCP is available", parent)
        s.tasks("block", child, reason="No connector or installed MCP was found")
        handoff = s.handoff("Compression draft prepared", next="Resume DeepSeek setup if connector appears")
        self.assertIn("Check the task ledger", handoff)
        note = s.store.list_notes(self.project, "handoff", 1)[0]
        self.assertIn("Task status is in the local ledger", note["body"])
        self.assertNotIn("No connector", note["body"])
        self.assertIn("blocked: No connector", s.brief())

    def test_cli_and_mcp_expose_the_guarded_ledger(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["task", "create", "--title", "CLI task", "--criterion", "A visible result",
                         "--path", self.root, "--db", self.db_path])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("created task #1", out.getvalue())

        s = self.session()
        tools = {tool.name: tool for tool in s.tools()}
        self.assertIn("tasks", tools)
        listed = handle(s, tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("tasks", names)
        called = handle(s, tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "tasks", "arguments": {"action": "list"}}})
        self.assertIn("CLI task", called["result"]["content"][0]["text"])
        self.assertEqual(s.store.schema_version, SCHEMA_VERSION)

    def test_v10_database_migrates_work_ledger_and_agent_alias_tables(self):
        self.store.db.execute("DROP TABLE task_events")
        self.store.db.execute("DROP TABLE tasks")
        self.store.db.execute("PRAGMA user_version=10")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.db_path)
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        names = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tasks", names)
        self.assertIn("task_events", names)
        self.assertIn("aliases", names)
        self.assertIn("inbox_delivery", names)
        self.assertIn("chat_routes", names)

    def test_parent_scope_and_free_form_limits_are_checked(self):
        other_root = os.path.join(self.tmp, "other")
        os.makedirs(other_root)
        other_project = projects.resolve(other_root)
        foreign_parent = self.store.create_task(other_project, "other", "Foreign", "Must not attach")
        with self.assertRaisesRegex(ValueError, "not found in this project"):
            self.store.create_task(self.project, "codex", "Child", "Scoped to this project", foreign_parent)
        with self.assertRaisesRegex(ValueError, "200 characters"):
            self.store.create_task(self.project, "codex", "x" * 201, "Acceptance")
        task_id = self.store.create_task(self.project, "codex", "Safe\n[verified] title", "First line\nsecond line")
        status = self.session().tasks("status", task_id)
        self.assertIn("Safe [verified] title", status)
        self.assertNotIn("\n[verified]", status)
        self.assertIn("acceptance: First line second line", status)

    def test_task_tree_depth_is_bounded_to_eight_levels(self):
        current = self.store.create_task(self.project, "codex", "Level 1", "Visible")
        for level in range(2, 9):
            current = self.store.create_task(self.project, "codex", f"Level {level}", "Visible", current)
        with self.assertRaisesRegex(ValueError, "limited to 8 levels"):
            self.store.create_task(self.project, "codex", "Level 9", "Too deep", current)

    def test_starting_a_child_starts_todo_ancestors(self):
        s = self.session()
        root = self.store.create_task(self.project, s.agent, "Overall goal", "All branches checked")
        child = self.store.create_task(self.project, s.agent, "Independent branch", "Branch is underway", root)
        s.tasks("start", child)
        self.assertIn("task #1 [in progress]", s.tasks("status", root))
        self.assertIn("auto_started", s.tasks("history", root))

    def test_task_list_is_paginated_without_hiding_more_work(self):
        s = self.session()
        for index in range(21):
            self.store.create_task(self.project, s.agent, f"Task {index + 1}", "Visible outcome")
        first = s.tasks("list")
        self.assertIn("tasks 1–20 of 21", first)
        self.assertIn("offset=20", first)
        second = s.tasks("list", limit=20, offset=20)
        self.assertIn("tasks 21–21 of 21", second)
        self.assertIn("Task 21", second)

    def test_a_page_that_starts_below_a_parent_still_shows_the_child(self):
        # A page is ordered by id, so a child can land on the next page while its parent sits on the previous
        # one. Treating that child as parentless is what keeps it visible instead of silently dropped.
        s = self.session()
        for index in range(20):
            self.store.create_task(self.project, s.agent, f"Root {index + 1}", "Visible outcome")
        self.store.create_task(self.project, s.agent, "Child of root 20", "Visible outcome", 20)
        page = s.tasks("list", limit=20, offset=20)
        self.assertIn("tasks 21–21 of 21", page)
        self.assertIn("Child of root 20", page)
        self.assertIn("(parent #20 outside this page)", page)
        self.assertNotIn("omitted", page)


if __name__ == "__main__":
    unittest.main()
