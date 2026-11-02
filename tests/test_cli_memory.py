"""The memory CLI: pinning, importance, confirm, skills listing and the usage numbers in `stats`."""

import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memdex.cli import main
from memdex.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        # The skills registry refreshes against the session's skill home on find/brief; pin it to an empty
        # dir so a test never sees this machine's real skills (Codex CLI ships its own).
        self.saved_skill_home = os.environ.get("MEMDEX_SKILL_HOME")
        os.environ["MEMDEX_SKILL_HOME"] = os.path.join(self.tmp, "skill-home")
        os.makedirs(os.environ["MEMDEX_SKILL_HOME"], exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.saved_skill_home is None:
            os.environ.pop("MEMDEX_SKILL_HOME", None)
        else:
            os.environ["MEMDEX_SKILL_HOME"] = self.saved_skill_home

    def cli(self, *args, agent="qa"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main([*args, "--path", self.root, "--agent", agent, "--db", self.db])
        return code, buf.getvalue()

    def rows(self, sql, args=()):
        store = Store(self.db)
        try:
            return store.db.execute(sql, args).fetchall()
        finally:
            store.close()


class NoteTests(Base):
    def test_remember_pins_and_sets_importance(self):
        code, out = self.cli("remember", "pitfall", "retries need jitter", "--importance", "3", "--pin")
        self.assertEqual(code, 0, out)
        self.assertIn("pinned", out)
        row = self.rows("SELECT pinned, importance FROM notes WHERE title='retries need jitter'")[0]
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(row["importance"], 3)

    def test_note_pin_unpin(self):
        self.cli("remember", "decision", "keep sqlite")
        self.assertIn("pinned #1", self.cli("note", "1", "--pin")[1])
        self.assertEqual(self.rows("SELECT pinned FROM notes WHERE id=1")[0]["pinned"], 1)
        self.assertIn("unpinned #1", self.cli("note", "1", "--unpin")[1])
        self.assertEqual(self.rows("SELECT pinned FROM notes WHERE id=1")[0]["pinned"], 0)

    def test_note_confirm_re_stamps_refs_and_counts_as_use(self):
        self.cli("remember", "pitfall", "store has no retry", "--files", "src/app.py")
        code, out = self.cli("note", "1", "--confirm")
        self.assertEqual(code, 0, out)
        self.assertIn("re-stamped", out)
        row = self.rows("SELECT use_count FROM notes WHERE id=1")[0]
        self.assertGreaterEqual(row["use_count"], 1)

    def test_note_archive(self):
        self.cli("remember", "todo", "old work")
        code, out = self.cli("note", "1", "--archive")
        self.assertEqual(code, 0, out)
        self.assertIn("archived #1", out)
        self.assertEqual(self.rows("SELECT archived FROM notes WHERE id=1")[0]["archived"], 1)

    def test_note_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.cli("note", "1", "--pin", "--archive")

    def test_missing_note_exits_nonzero(self):
        code, out = self.cli("note", "999")
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


class StatsTests(Base):
    def test_stats_reports_usage_and_ranking(self):
        self.cli("remember", "pitfall", "old lesson", agent="qa")
        self.cli("remember", "note", "pinned core", "--pin")
        # A read by a *different* agent is what "used" means; the author reading their own note is not.
        self.cli("note", "1", agent="claude")
        self.cli("note", "1", "--confirm", agent="claude")
        code, out = self.cli("stats")
        self.assertEqual(code, 0)
        for fragment in ("memory use:", "notes read by another agent 1/2", "re-confirmed after a change 1x",
                         "pinned 1", "ranking:", "cold line", "skills:"):
            self.assertIn(fragment, out)

    def test_reading_your_own_note_is_not_another_agent(self):
        self.cli("remember", "pitfall", "my own lesson", agent="qa")
        self.cli("note", "1", agent="qa")
        code, out = self.cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("notes read by another agent 0/1", out)

    def test_cold_note_count(self):
        self.cli("remember", "note", "ancient note")
        five_years_ago = time.time() - 5 * 365 * 86400
        store = Store(self.db)
        try:
            store.db.execute("UPDATE notes SET created_at=?, last_used_at=NULL, use_count=0",
                             (five_years_ago,))
            store.db.commit()
        finally:
            store.close()
        code, out = self.cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("1 note(s) below the cold line", out)


class SkillsTests(Base):
    def test_share_list_find_show(self):
        code, out = self.cli("skills", "share", "deploy-test",
                             "--description", "how to deploy the test environment",
                             "--invoke", "make deploy")
        self.assertEqual(code, 0, out)
        self.assertIn("shared skill", out)

        code, out = self.cli("skills")
        self.assertEqual(code, 0, out)
        self.assertIn("deploy-test", out)
        self.assertIn("make deploy", out)

        code, out = self.cli("skills", "find", "deploy")
        self.assertEqual(code, 0, out)
        self.assertIn("deploy-test", out)

        code, out = self.cli("skills", "show", "deploy-test")
        self.assertEqual(code, 0, out)
        self.assertIn("deploy-test", out)
        self.assertIn("how to deploy", out)

    def test_empty_registry_says_so(self):
        code, out = self.cli("skills")
        self.assertEqual(code, 0)
        self.assertIn("no skills registered", out)

    def test_show_needs_a_name(self):
        code, out = self.cli("skills", "show")
        self.assertEqual(code, 2)
        self.assertIn("needs a name", out)

    def test_find_without_matches(self):
        self.cli("skills", "share", "deploy-test", "--description", "deploy the test env")
        code, out = self.cli("skills", "find", "kubernetes")
        self.assertEqual(code, 0)
        self.assertIn("no skill matches", out)


if __name__ == "__main__":
    unittest.main()

