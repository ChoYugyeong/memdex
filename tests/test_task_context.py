"""Query-aware brief: reuse current shared facts, without inheriting another task's next step."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memdex.cli import main
from memdex.mcp import handle
from memdex.project import resolve
from memdex.store import Store
from memdex.tools import Session, _task_context, render_brief


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "proj")
        self.root.mkdir()
        self.home = Path(self.tmp.name, "home")
        self.home.mkdir()
        self.db = str(Path(self.tmp.name, "memory.db"))
        self.env = mock.patch.dict(os.environ, {"MEMDEX_EMBED": "", "MEMDEX_MODEL_TIER": "standard"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Brief skills scanning belongs to the registry tests, not this retrieval regression set.
        self.refresh = mock.patch("memdex.tools.skills.maybe_refresh")
        self.refresh.start()
        self.addCleanup(self.refresh.stop)
        self.store = Store(self.db)
        self.store.embedding = False
        self.addCleanup(self.store.close)
        self.project = resolve(str(self.root))
        self.session = Session(self.store, agent_name="codex")
        self.session.cwd = str(self.root)
        self.write("billing.py", '"""Invoice settlement reconciliation."""\ndef reconcile():\n    pass\n')
        self.store.index(self.project)

    def write(self, name, text):
        Path(self.root, name).write_text(text, encoding="utf-8")

    def note(self, kind, title, body="", **kwargs):
        return self.store.add_note(self.project, kind, title, body, "teammate", **kwargs)

    def brief(self, task="", **kwargs):
        return render_brief(self.store, self.project, "codex@proj", task=task,
                            home=str(self.home), **kwargs)

    def test_an_old_task_fact_is_retrieved_ahead_of_unrelated_recent_decisions(self):
        nid = self.note("decision", "Invoice settlement", "Reconcile against the bank ledger.")
        # Cold notes are omitted by general list_notes, but still searchable by the current task.
        with self.store.db:
            self.store.db.execute("UPDATE notes SET created_at=1 WHERE id=?", (nid,))
        for i in range(14):
            self.note("decision", f"Theme colour {i}", "Use amber for navigation.")
        generic, focused = self.brief(), self.brief("invoice settlement")
        self.assertNotIn(f"#{nid} Invoice settlement", generic)
        self.assertIn(f"[decision #{nid}] Invoice settlement", focused)
        self.assertIn("bank ledger", focused)
        self.assertNotIn("Theme colour", focused)

    def test_only_task_mode_replaces_the_latest_handoff(self):
        self.note("handoff", "Unrelated artwork", "Done:\npainted\n\nNext:\nDraw a dragon")
        self.assertIn("Draw a dragon", self.brief())
        focused = self.brief("invoice settlement")
        self.assertNotIn("Draw a dragon", focused)
        self.assertNotIn("do first:", focused)
        self.assertIn("task: invoice settlement", focused)

    def test_a_matching_handoff_is_retrieved_as_an_excerpt(self):
        nid = self.note("handoff", "Invoice settlement", "Next: reconcile the pending transfer.")
        self.assertIn(f"[handoff #{nid}]", self.brief("invoice settlement"))

    def test_one_incidental_query_word_does_not_resurrect_an_unrelated_handoff(self):
        self.note("handoff", "Artwork export", "Next: scan and print the dragon drawing.")
        self.assertNotIn("dragon", self.brief("skills security scan"))

    def test_a_note_found_only_through_file_refs_needs_textual_task_evidence(self):
        self.note("decision", "Theme colours", "Use amber for navigation.", files=["billing.py"])
        self.assertNotIn("Theme colours", self.brief("billing invoice settlement"))

    def test_an_unrelated_vector_only_note_is_not_promoted_to_startup_context(self):
        nid = self.note("pitfall", "Theme colours", "Use amber for navigation.")
        hit = {"src": "note", "id": nid, "kind": "pitfall", "title": "Theme colours",
               "stale": [], "matched_by": ["semantic"]}
        with mock.patch.object(self.store, "search", return_value=[hit]):
            self.assertEqual(_task_context(self.store, self.project, "invoice settlement", set()), [])

    def test_a_summary_saved_by_another_agent_is_reused(self):
        sha = self.store.save_summary(self.project, "billing.py", "Use bank IDs to avoid duplicate invoices.", "claude")
        out = self.brief("invoice settlement")
        self.assertIn(f"summary by claude, sha {sha[:8]}", out)
        self.assertIn("Use bank IDs", out)
        self.assertNotIn("def reconcile", out)

    def test_an_edited_files_old_summary_is_not_served(self):
        self.store.save_summary(self.project, "billing.py", "OLD invoice settlement summary", "claude")
        self.write("billing.py", '"""Invoice settlement with new transfer identifiers."""\nx = 4\n')
        out = self.brief("invoice settlement")
        self.assertIn("billing.py (outline)", out)
        self.assertNotIn("OLD", out)

    def test_even_a_file_not_checked_by_the_index_budget_is_rechecked_before_a_summary(self):
        self.store.save_summary(self.project, "billing.py", "OLD summary", "claude")
        self.write("billing.py", '"""Invoice settlement v2."""\nx = 123\n')
        # Same as a brief where this file has not been visited by the capped index pass yet.
        out = "\n".join(_task_context(self.store, self.project, "invoice settlement", set()))
        self.assertNotIn("OLD", out)
        self.assertIn("v2", out)

    def test_pinned_rules_claims_inbox_and_work_ledger_survive_task_filtering(self):
        self.note("decision", "Always use integer cents", "No floating point money.", pinned=True)
        self.store.claim(self.project, "claude@proj", ["billing.py"], "editing invoices", 30, exclusive=True)
        self.store.create_task(self.project, "codex@proj", "Ship settlement", "Bank ledger matches")
        with mock.patch.object(self.store, "unread_count", return_value=2):
            out = self.brief("invoice settlement")
        for phrase in ("Always use integer cents", "locked — do not edit", "billing.py (claude@proj)",
                       "inbox: 2", "work ledger", "Ship settlement", "information, not instructions"):
            self.assertIn(phrase, out)

    def test_a_shown_pinned_note_is_not_repeated_in_matches(self):
        nid = self.note("decision", "Invoice settlement", "Use integer cents.", pinned=True)
        out = self.brief("invoice settlement")
        self.assertIn(f"📌 #{nid}", out)
        self.assertNotIn(f"[decision #{nid}]", out)

    def test_stale_note_references_keep_the_warning(self):
        nid = self.note("pitfall", "Invoice settlement", "Do not round twice.", files=["billing.py"])
        self.write("billing.py", '"""Invoice settlement changed."""\nx = 12\n')
        out = self.brief("invoice settlement")
        self.assertIn(f"[pitfall #{nid}]", out)
        self.assertIn("⚠ changed since: billing.py", out)

    def test_archived_and_replaced_memories_are_not_reintroduced(self):
        old = self.note("decision", "Invoice settlement old", "Obsolete rounding.")
        self.note("decision", "Invoice settlement current", "Use decimal cents.", supersedes=old)
        archived = self.note("note", "Invoice settlement archived", "Discarded advice.")
        self.store.archive_note(self.project, archived)
        out = self.brief("invoice settlement")
        self.assertIn("Invoice settlement current", out)
        self.assertNotIn("Obsolete rounding", out)
        self.assertNotIn("Discarded advice", out)

    def test_a_task_with_no_hits_does_not_fall_back_to_unrelated_history(self):
        self.note("pitfall", "Artwork", "The dragon should have two wings.")
        out = self.brief("zyxwvu")
        self.assertIn("No task matches", out)
        self.assertNotIn("Artwork", out)

    def test_short_budget_keeps_high_ranked_context_without_dumping_the_full_file(self):
        self.note("note", "Invoice settlement", "Bank reference " * 500)
        out = self.brief("invoice settlement", max_chars=1100)
        self.assertLessEqual(len(out), 1300)  # existing trim notice is outside the main budget
        self.assertIn("task match", out)
        self.assertNotIn("Bank reference " * 30, out)

    def test_task_input_is_single_line_bounded_and_empty_is_backward_compatible(self):
        self.note("handoff", "Next work", "Next: Continue painting")
        self.assertIn("Continue painting", self.brief("   \n"))
        out = self.brief("invoice\nsettlement\x00 " + "x" * 2000)
        task_line = next(line for line in out.splitlines() if line.startswith("task:"))
        self.assertLessEqual(len(task_line), 518)
        self.assertNotIn("\x00", out)

    def test_cli_task_argument_and_mcp_schema_use_the_same_existing_tool(self):
        with mock.patch("memdex.cli._session", return_value=self.session):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["brief", "--path", str(self.root), "--db", self.db,
                             "--agent", "codex", "--task", "invoice settlement"])
        self.assertEqual(code, 0)
        self.assertIn("task match [file] billing.py", buf.getvalue())
        registry = {t.name: t for t in self.session.tools()}
        self.assertEqual(len(registry), 15)
        self.assertIn("task", registry["project_brief"].params)
        reply = handle(self.session, registry, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "project_brief", "arguments": {"task": "invoice settlement"}}})
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("task match", reply["result"]["content"][0]["text"])

    def test_internal_brief_retrieval_does_not_inflate_tool_search_metrics(self):
        self.brief("invoice settlement")
        self.assertEqual(self.store.search_stats(), {})


if __name__ == "__main__":
    unittest.main()
