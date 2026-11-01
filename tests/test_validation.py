"""Input validation: the server must answer with a sentence, never store junk.

Agents call these tools with whatever their model produced, so empty strings,
paths outside the project and 20 KB "summaries" all happen in practice.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex.maintenance import human_size
from memdex.project import resolve
from memdex.store import Store
from memdex.tools import SUMMARY_CHARS, Session


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        write(self.root, "src/app.py", "def main():\n    pass\n")
        self.store = Store(os.path.join(self.tmp, "memdex.db"))
        self.session = Session(self.store, agent_name="claude")
        self.session.cwd = self.root
        self.project = resolve(self.root)
        self.store.index(self.project)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class GuardTests(Base):
    def test_remember_needs_a_title(self):
        out = self.session.remember("note", "   ", "body")
        self.assertIn("title is required", out)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 0)

    def test_empty_post_is_not_stored(self):
        out = self.session.post("   ")
        self.assertIn("message is empty", out)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_empty_handoff_is_not_stored(self):
        out = self.session.handoff("", "", "", "")
        self.assertIn("nothing to hand off", out)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 0)

    def test_handoff_with_only_next_still_works(self):
        out = self.session.handoff("", next="finish the CLI")
        self.assertIn("handoff #", out)

    def test_claim_outside_project_is_ignored(self):
        out = self.session.claim(["../../../etc/passwd"], "evil")
        self.assertIn("no path inside this project", out)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)

    def test_claim_keeps_only_inside_paths(self):
        outside = os.path.join(self.tmp, "elsewhere", "x.py")  # absolute, outside the project
        out = self.session.claim(["src/app.py", outside], "mix")
        self.assertIn("claimed 1 path(s)", out)
        self.assertIn("Ignored 1 path(s)", out)
        paths = [r[0] for r in self.store.db.execute("SELECT path FROM claims")]
        self.assertEqual(paths, ["src/app.py"])

    def test_relative_parent_path_is_outside(self):
        out = self.session.claim(["../outside.py"], "up")
        self.assertIn("no path inside this project", out)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)

    def test_claim_normalises_windows_and_dot_paths(self):
        self.session.claim([".\\src\\app.py"], "win")
        paths = [r[0] for r in self.store.db.execute("SELECT path FROM claims")]
        self.assertEqual(paths, ["src/app.py"])


class SummaryLengthTests(Base):
    def test_long_summary_is_trimmed(self):
        out = self.session.file_summary_save("src/app.py", "x" * (SUMMARY_CHARS + 500))
        self.assertIn("trimmed", out)
        saved = self.store.db.execute("SELECT summary FROM summaries").fetchone()[0]
        self.assertLessEqual(len(saved), SUMMARY_CHARS)
        self.assertGreater(len(saved), SUMMARY_CHARS - 10)

    def test_normal_summary_is_untouched(self):
        out = self.session.file_summary_save("src/app.py", "Entry point. Calls nothing.")
        self.assertNotIn("trimmed", out)
        self.assertIn("Entry point", self.session.file_summary("src/app.py"))

    def test_whitespace_only_summary_is_rejected(self):
        self.assertIn("summary is empty", self.session.file_summary_save("src/app.py", "\n\t "))


class HumanSizeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(1024 * 1024 * 3.45), "3.4 MB")
        self.assertEqual(human_size(-5), "0 B")


if __name__ == "__main__":
    unittest.main()
