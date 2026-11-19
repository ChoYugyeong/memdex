"""Automatic reviews use explicit evidence and return exactly once to their caller."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from memdex import cli, mcp, review, review_providers
from memdex.project import resolve
from memdex.store import Store
from memdex.tools import Session


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = resolve(str(self.root))
        self.db_path = str(self.root / "memory.db")
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"MEMDEX_AGENT": "codex", "MEMDEX_REASON_NOTIFY": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.session = Session(self.store, _project=self.project, conversation_id="original-chat")
        self.task = self.store.create_task(self.project, "codex", "review", "receive result")
        (self.root / "brief.md").write_text("검토할 근거\nDo not execute this file.", encoding="utf-8")
        self.launch = patch.object(review, "launch").start()
        self.addCleanup(patch.stopall)
        patch.object(review.shutil, "which", return_value="claude.exe").start()

    def submit(self, **kwargs):
        return self.session.review("독립 검토", ["brief.md"], self.task, **kwargs)

    def job(self):
        return self.store.db.execute("SELECT * FROM review_jobs ORDER BY id DESC LIMIT 1").fetchone()

    def test_snapshot_and_duplicate_submission_survive_changes_and_worker_restarts(self):
        receipt = self.submit()
        request_id = self.job()["id"]
        self.assertIn("queued", receipt)
        self.assertEqual(self.submit(), receipt)
        self.launch.assert_called_once()
        (self.root / "brief.md").write_text("changed after submission", encoding="utf-8")
        with patch.object(review, "run_claude", return_value=("completed", "independent result")) as model:
            review.worker(self.db_path, request_id)
            review.worker(self.db_path, request_id)
        model.assert_called_once()
        self.assertIn("검토할 근거", json.loads(model.call_args.args[1])["files"][0]["content"])
        replies = self.store.db.execute("SELECT * FROM messages WHERE reply_to=?", (request_id,)).fetchall()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["recipient_route"], self.session.route)
        self.assertIn(f"task #{self.task}", replies[0]["body"])
        self.assertIn("independent result", self.session.review_status(request_id))
        self.assertNotIn("content", json.loads(self.job()["payload"])["files"][0])
        other = Session(self.store, _project=self.project, conversation_id="other-chat")
        self.assertEqual(self.store.inbox(self.project, other.chat_mailbox()), [])
        self.assertEqual(len(self.store.inbox(self.project, self.session.route)), 1)
        self.assertEqual(self.store.task_rows(self.project)[0]["status"], "todo")

    def test_explicit_key_conflict_and_intentional_new_request(self):
        self.submit(request_key="one")
        (self.root / "brief.md").write_text("new evidence", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different review input"):
            self.submit(request_key="one")
        self.submit(request_key="two")
        self.assertEqual(self.launch.call_count, 2)

    def test_wrong_project_task_missing_chat_and_outside_file_fail_before_launch(self):
        with self.assertRaisesRegex(ValueError, "open task"):
            self.session.review("review", ["brief.md"], self.task + 999)
        self.session.conversation_id = None
        with self.assertRaisesRegex(ValueError, "host chat ID"):
            self.submit()
        self.session.conversation_id = "original-chat"
        with self.assertRaisesRegex(ValueError, "inside the current project"):
            self.session.review("review", [str(Path(__file__).resolve())], self.task)
        self.launch.assert_not_called()

    def test_invalid_inputs_are_not_silently_truncated_or_sent(self):
        for name, data in ((".env", b"credential"), ("binary.txt", b"\x00"),
                           ("large.md", b"a" * (review.MAX_BYTES + 1))):
            (self.root / name).write_bytes(data)
            with self.assertRaises(ValueError):
                self.session.review("review", [name], self.task)
        self.launch.assert_not_called()

    def test_worker_error_and_launch_error_are_visible_in_inbox(self):
        self.launch.side_effect = OSError("failed")
        self.assertIn("Could not start", self.submit())
        self.assertIn("failed", self.session.inbox())
        self.launch.side_effect = None
        self.submit(request_key="retry")
        request_id = self.job()["id"]
        with patch.object(review, "run_claude", side_effect=RuntimeError("private diagnostics")):
            review.worker(self.db_path, request_id)
        result = self.session.review_status(request_id)
        self.assertIn("worker failed", result)
        self.assertNotIn("private diagnostics", result)

    def test_parallel_workers_only_invoke_model_once(self):
        self.submit()
        request_id = self.job()["id"]
        with patch.object(review, "run_claude", return_value=("completed", "result")) as model:
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda _: review.worker(self.db_path, request_id), range(2)))
        model.assert_called_once()

    def test_parallel_submission_reserves_one_request(self):
        # Initialize before racing independent connections, as a shared MCP worker would.
        review.initialize(self.store.db)

        def submit_once(_):
            store = Store(self.db_path)
            try:
                session = Session(store, _project=self.project, conversation_id="original-chat")
                return session.review("review", ["brief.md"], self.task)
            finally:
                store.close()

        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(submit_once, range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.launch.assert_called_once()

    def test_registered_task_cannot_be_repurposed(self):
        other_task = self.store.create_task(self.project, "codex", "other work", "done")
        self.session.session_routes(task_id=other_task)
        with self.assertRaisesRegex(ValueError, "registered task"):
            self.submit()
        self.launch.assert_not_called()

    def test_status_is_scoped_and_does_not_retry_stale_work(self):
        self.submit()
        request_id = self.job()["id"]
        other = Session(self.store, _project=self.project, conversation_id="other-chat")
        with self.assertRaisesRegex(ValueError, "originating chat"):
            other.review_status(request_id)
        with patch.object(review.time, "time", return_value=self.job()["started_at"] + 241):
            self.assertIn("interrupted", self.session.review_status(request_id))
            self.assertIn("interrupted", self.session.review_status(request_id))
        self.assertEqual(len(self.store.db.execute("SELECT id FROM messages WHERE reply_to=?",
                                                  (request_id,)).fetchall()), 1)
        self.assertIn("interrupted", self.session.inbox())
        review.finish(self.store, request_id, "completed", "late answer")
        self.assertNotIn("late answer", self.session.review_status(request_id))
        self.launch.assert_called_once()

    def test_npm_wrapper_resolves_to_direct_node_without_cmd(self):
        entry = self.root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        (self.root / "node.exe").touch()
        with patch.object(review.shutil, "which", return_value=str(self.root / "claude.cmd")):
            self.assertEqual(review.claude_command(), [str(self.root / "node.exe"), str(entry)])

    def test_claude_receives_stdin_without_tools_or_session_storage(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}), "")
        with patch.object(review.subprocess, "run", return_value=response) as run:
            self.assertEqual(review.run_claude(str(self.root), "payload"), ("completed", "ok"))
        command = run.call_args.args[0]
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(run.call_args.kwargs["input"], "payload")
        self.assertNotIn("--dangerously-skip-permissions", command)
        with patch.object(review.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            self.assertIn("timed out", review.run_claude(str(self.root), "payload")[1])

    def test_mcp_registry_and_cli_preserve_origin(self):
        registry = {tool.name: tool for tool in self.session.tools()}
        result = mcp._call(self.session, registry, "review",
                           {"question": "review", "files": "brief.md", "task_id": self.task})
        self.assertFalse(result["isError"])
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "original-chat"}), redirect_stdout(StringIO()) as out:
            code = cli.main(["review-status", str(self.job()["id"]), "--db", self.db_path,
                             "--path", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("queued", out.getvalue())

    def test_providers_and_models_deduplicate_independently_and_reply_as_selected(self):
        self.submit()
        with patch.object(review_providers, "check_ollama"):
            self.submit(provider="qwen")
            qwen_id = self.job()["id"]
            self.submit(provider="qwen")
            self.assertEqual(self.job()["id"], qwen_id)
            self.submit(provider="ollama", model="another-model")
            ollama_id = self.job()["id"]
        self.assertEqual(self.launch.call_count, 3)
        with patch.object(review_providers, "run_ollama", return_value=("completed", "local answer")) as run:
            review.worker(self.db_path, qwen_id)
            self.assertEqual(run.call_args.args[1], "qwen2.5")
            review.worker(self.db_path, ollama_id)
            self.assertEqual(run.call_args.args[1], "another-model")
        mail = self.store.db.execute("SELECT * FROM messages WHERE reply_to=?", (qwen_id,)).fetchone()
        self.assertEqual(mail["sender"], "qwen-review")
        self.assertEqual(mail["recipient_route"], self.session.route)

    def test_deepseek_uses_separate_worker_and_preserves_request_key_binding(self):
        with patch.object(review_providers, "deepseek_command"):
            self.submit(provider="deepseek", request_key="selected")
        request_id = self.job()["id"]
        with self.assertRaisesRegex(ValueError, "different review input"):
            self.submit(provider="claude", request_key="selected")
        with patch.object(review_providers, "run_deepseek", return_value=("completed", "account answer")) as run:
            review.worker(self.db_path, request_id)
        self.assertEqual(run.call_args.args[2], "deepseek-flash")
        self.assertIn("DeepSeek", self.session.review_status(request_id))

    def test_legacy_table_and_fingerprint_survive_upgrade(self):
        self.submit(request_key="legacy")
        job = dict(self.job())
        # Rebuild the exact pre-provider schema with one queued legacy request.
        columns = [name for name in job if name not in ("provider", "model")]
        with self.store.db:
            self.store.db.execute("ALTER TABLE review_jobs RENAME TO old_review_jobs")
            self.store.db.execute("CREATE TABLE review_jobs AS SELECT " + ",".join(columns) + " FROM old_review_jobs")
            self.store.db.execute("CREATE UNIQUE INDEX legacy_review_key ON review_jobs(project,origin,request_key)")
            self.store.db.execute("DROP TABLE old_review_jobs")
        self.submit(request_key="legacy")
        self.assertEqual(self.job()["id"], job["id"])
        self.assertEqual(self.job()["provider"], "claude")
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
