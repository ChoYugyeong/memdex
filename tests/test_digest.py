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
from memdex.tools import render_brief


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


class ContradictionTests(Base):
    def test_opposite_decisions_are_a_contradiction(self):
        self.note("decision", "Always use sync mode",
                  "the store writes in sync mode so failures surface immediately", ["src/store.py"])
        self.note("pitfall", "Never use sync mode",
                  "sync mode deadlocks the workers; never use it here", ["src/store.py"])
        found = digest.contradictions(self.store, self.project)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["shared"], ["src/store.py"])
        self.assertTrue({"sync", "mode"} & set(found[0]["tokens"]))

    def test_opposite_terms_without_refs_still_count(self):
        self.note("decision", "Always retry", "retry every request that fails")
        self.note("pitfall", "Never retry", "retrying hides the outage; never retry")
        found = digest.contradictions(self.store, self.project)
        self.assertEqual(len(found), 1)
        self.assertIn("retry", found[0]["tokens"])

    def test_agreeing_notes_are_not_a_contradiction(self):
        self.note("decision", "Use WAL", "WAL for concurrent writers", ["src/store.py"])
        self.note("pitfall", "WAL required", "without WAL two writers deadlock", ["src/store.py"])
        # "without WAL two writers deadlock" negates nothing that the other asserts positively
        # about the same term in a conflicting way: both want WAL.
        self.assertEqual(digest.contradictions(self.store, self.project), [])

    def test_a_single_negation_is_not_enough_on_its_own(self):
        self.note("note", "Lunch note", "do not edit before reading", ["src/store.py"])
        self.note("note", "Unrelated", "the api pages at 50 items", ["src/api.py"])
        self.assertEqual(digest.contradictions(self.store, self.project), [])

    def test_a_quoted_negation_is_a_citation_not_a_contradiction(self):
        self.note("note", "README warning",
                  'the README says: "never use sync mode"', ["src/store.py"])
        self.note("decision", "Use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.assertEqual(digest.contradictions(self.store, self.project), [])

    def test_an_attributed_negation_is_a_citation_not_a_contradiction(self):
        self.note("note", "README warning",
                  "the README says: do not use sync mode", ["src/store.py"])
        self.note("decision", "Use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.assertEqual(digest.contradictions(self.store, self.project), [])

    def test_the_render_names_both_sides_and_the_supersedes_move(self):
        self.note("decision", "Always use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.note("pitfall", "Never use sync mode", "never use sync mode here", ["src/store.py"])
        text = digest.render_contradictions(digest.contradictions(self.store, self.project))
        self.assertIn("Always use sync mode", text)
        self.assertIn("Never use sync mode", text)
        self.assertIn("disagree on:", text)
        self.assertIn("supersede", text)

    def test_empty_says_the_notes_agree(self):
        self.assertIn("no contradictions", digest.render_contradictions([]))

    def test_the_brief_names_the_contradiction(self):
        self.note("decision", "Always use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.note("pitfall", "Never use sync mode", "never use sync mode here", ["src/store.py"])
        out = render_brief(self.store, self.project, "qa@proj")
        self.assertIn("contradiction:", out)
        self.assertIn("settle before editing", out)

    def test_the_cli_prints_pairs_and_json_parses(self):
        self.note("decision", "Always use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.note("pitfall", "Never use sync mode", "never use sync mode here", ["src/store.py"])
        code, out = self.cli("contradictions")
        self.assertEqual(code, 0)
        self.assertIn("contradiction(s)", out)
        code, out = self.cli("contradictions", "--json")
        self.assertEqual(code, 0)
        self.assertIn('"tokens"', out)

    def test_whoami_reads_metadata_without_indexing(self):
        self.note("decision", "Always use sync mode", "the store writes in sync mode", ["src/store.py"])
        self.note("pitfall", "Never use sync mode", "never use sync mode here", ["src/store.py"])
        code, out = self.cli("whoami")
        self.assertEqual(code, 0)
        self.assertIn("you are qa@proj", out)
        self.assertIn("contradictions: 1 pair(s)", out)
        self.assertNotIn("index:", out)  # metadata only, no scan


if __name__ == "__main__":
    unittest.main()
