"""`memdex import`: the project's own prose becomes the first notes.

Cold start is the one limit no ranking algorithm fixes — a store with nothing in it cannot save a token.
So the cheapest fix is to seed it from what the project already wrote down, with refs to the files those
sections mention, and to make the import idempotent: running it twice must not double the memory.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import importer
from memdex import project as projects
from memdex.cli import main
from memdex.store import Store

DOC = """# Project notes

Some preamble that mentions `src/store.py` and does not fit in a heading.

## Build

Run `make build`. The build reads `src/store.py` and `README.md`.

## Deploy

Deployment is manual and nobody remembers how.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        Path(self.root, "README.md").write_text("# readme\n", encoding="utf-8")
        Path(self.root, "AGENTS.md").write_text(DOC, encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main([*args, "--path", self.root, "--agent", "qa", "--db", self.db])
        return code, buf.getvalue()

    def titles(self):
        return sorted(r["title"] for r in
                      self.store.db.execute("SELECT title FROM notes WHERE project=?", (self.project.key,)))


class SplitTests(unittest.TestCase):
    def test_headings_become_sections(self):
        sections = importer.split_sections(DOC)
        self.assertEqual([title for title, _ in sections], ["Project notes", "Build", "Deploy"])

    def test_text_without_headings_is_one_section(self):
        self.assertEqual(importer.split_sections("just a paragraph about the build"),
                         [("preamble", "just a paragraph about the build")])

    def test_empty_sections_are_dropped(self):
        self.assertEqual(importer.split_sections("## A\n## B\n"), [])


class ImportTests(Base):
    def test_sections_become_notes_with_refs(self):
        rep = importer.run(self.store, self.project, ["AGENTS.md"], "qa")
        self.assertEqual(rep["imported"], 3)
        notes = {r["title"]: r for r in self.store.db.execute(
            "SELECT title, refs FROM notes WHERE project=?", (self.project.key,))}
        refs = {r["path"] for r in __import__("json").loads(notes["Build"]["refs"])}
        self.assertEqual(refs, {"src/store.py", "README.md"})
        self.assertEqual(notes["Deploy"]["refs"], "[]")

    def test_a_second_run_adds_nothing(self):
        importer.run(self.store, self.project, ["AGENTS.md"], "qa")
        rep = importer.run(self.store, self.project, ["AGENTS.md"], "qa")
        self.assertEqual((rep["imported"], rep["skipped"]), (0, 3))
        self.assertEqual(len(self.titles()), 3)

    def test_a_missing_file_is_reported_not_fatal(self):
        rep = importer.run(self.store, self.project, ["AGENTS.md", "NOPE.md"], "qa")
        self.assertEqual(rep["missing"], ["NOPE.md"])
        self.assertEqual(rep["imported"], 3)

    def test_default_paths_find_the_usual_names(self):
        self.assertEqual(importer.default_paths(self.project), ["AGENTS.md"])

    def test_the_cli_imports_and_says_so(self):
        code, out = self.cli("import")
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md: 3 section(s) imported", out)
        self.assertIn("imported 3 note(s)", out)

    def test_the_imported_notes_show_up_in_the_brief(self):
        self.cli("import")
        code, out = self.cli("brief")
        self.assertEqual(code, 0)
        self.assertIn("memory: 3 note(s) → search before exploring", out)

    def test_naming_a_file_that_does_not_exist_fails(self):
        code, out = self.cli("import", "NOPE.md")
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
