"""A brief with a memory must not also say "No notes yet".

The two lines come from different places: the `memory: N note(s)` line counts the notes that have no section
of their own (imported AGENTS.md content, plain remember calls), while the "No notes yet" line was gated on
the sections only. A project whose whole memory was one imported note showed both at once.
"""

import os
import shutil
import tempfile
import unittest

from memdex import project as projects
from memdex.store import Store
from memdex.tools import render_brief


class BriefMemoryLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "app.py"), "w") as fh:
            fh.write("x = 1\n")
        self.store = Store(os.path.join(self.tmp, "memdex.db"))
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_plain_note_is_not_also_no_notes_yet(self):
        self.store.add_note(self.project, "note", "The store is one SQLite file", "", "claude@demo")
        out = render_brief(self.store, self.project, "codex@demo")
        self.assertIn("memory: 1 note(s)", out)
        self.assertNotIn("No notes yet", out)

    def test_an_empty_project_still_says_no_notes_yet(self):
        out = render_brief(self.store, self.project, "codex@demo")
        self.assertIn("No notes yet", out)
        self.assertNotIn("memory:", out)

    def test_a_pitfall_alone_also_suppresses_it(self):
        self.store.add_note(self.project, "pitfall", "WAL must be set before the first write", "", "claude@demo")
        out = render_brief(self.store, self.project, "codex@demo")
        self.assertNotIn("No notes yet", out)


if __name__ == "__main__":
    unittest.main()
