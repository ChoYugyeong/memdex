"""`memdex digest`: which notes are two ways of saying the same thing.

memdex cannot write the digest — deciding what is still true means reading the code — but it can do the
bookkeeping: pair up notes about the same file, or near-duplicates of each other, and hand them over.
"""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
import os

from memdex import digest
from memdex import project as projects
from memdex.cli import main
from memdex.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("x = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("y = 2\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note(self, kind, title, body, files=None):
        return self.store.add_note(self.project, kind, title, body, "claude", files or [])

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main([*args, "--path", self.root, "--agent", "qa", "--db", self.db])
        return code, buf.getvalue()


class ClusterTests(Base):
    def test_two_notes_about_one_file_are_a_cluster(self):
        self.note("pitfall", "WAL mode needed", "two writers deadlock without WAL", ["src/store.py"])
        self.note("decision", "Use WAL", "WAL, not a write lock, for the store", ["src/store.py"])
        found = digest.clusters(self.store, self.project)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["shared"], ["src/store.py"])
        self.assertEqual({n["title"] for n in found[0]["notes"]}, {"WAL mode needed", "Use WAL"})

    def test_unrelated_notes_are_left_alone(self):
        self.note("pitfall", "WAL mode needed", "two writers deadlock without WAL", ["src/store.py"])
        self.note("note", "API pagination", "the api pages at 50 items", ["src/api.py"])
        self.assertEqual(digest.clusters(self.store, self.project), [])

    def test_near_duplicates_cluster_even_without_refs(self):
        body = "the retry loop backs off exponentially between attempts in the worker"
        self.note("pitfall", "Retry loop backs off", body)
        self.note("pitfall", "Retry loop backs off exponentially", body)
        found = digest.clusters(self.store, self.project)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["shared"], [])

    def test_a_single_note_is_not_a_cluster(self):
        self.note("note", "Only one", "about src/store.py", ["src/store.py"])
        self.assertEqual(digest.clusters(self.store, self.project), [])

    def test_the_render_names_the_supersedes_move(self):
        self.note("pitfall", "WAL mode needed", "two writers deadlock without WAL", ["src/store.py"])
        self.note("decision", "Use WAL", "WAL, not a write lock, for the store", ["src/store.py"])
        text = digest.render(digest.clusters(self.store, self.project))
        self.assertIn("supersedes=#", text)
        self.assertIn("memdex cannot write the digest", text)

    def test_empty_says_nothing_to_do(self):
        self.assertIn("no clusters", digest.render(digest.clusters(self.store, self.project)))

    def test_the_cli_prints_and_a_json_flag_parses(self):
        self.note("pitfall", "WAL mode needed", "two writers deadlock without WAL", ["src/store.py"])
        self.note("decision", "Use WAL", "WAL, not a write lock, for the store", ["src/store.py"])
        code, out = self.cli("digest")
        self.assertEqual(code, 0)
        self.assertIn("2 notes", out)
        code, out = self.cli("digest", "--json")
        self.assertEqual(code, 0)
        self.assertIn('"shared"', out)


if __name__ == "__main__":
    unittest.main()
