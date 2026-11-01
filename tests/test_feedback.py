"""RM3-lite: the second query is built from what the first one found.

A keyword search cannot match a note that never uses the query's words. The fix that fits memdex is the
cheap half of pseudo-relevance feedback: take the top hits, keep the terms that repeat inside them but are
rare in the whole index, and search again with them — at half weight, so an expansion can add a result but
never outrank the literal match that produced it.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note(self, title, body, files=None):
        return self.store.add_note(self.project, "pitfall", title, body, "claude", files or [])

    def titles(self, query, **kw):
        return [r.get("title") or r.get("path") for r in self.store.search(self.project, query, **kw)]


class FeedbackTests(Base):
    def test_a_note_that_only_shares_the_context_is_found(self):
        # The query word ("wal") appears in one note; the second note only shares the vocabulary around
        # it. The expansion is what connects them.
        self.note("WAL mode needed", "sqlite concurrency: enable WAL mode or two writers deadlock")
        self.note("Two writers deadlock", "sqlite concurrency bites when two agents write at once")
        self.note("Formatting", "black and isort run before commit")
        titles = self.titles("wal")
        self.assertIn("WAL mode needed", titles)
        self.assertIn("Two writers deadlock", titles)

    def test_the_literal_match_still_wins(self):
        self.note("WAL mode needed", "sqlite concurrency: enable WAL mode or two writers deadlock")
        self.note("Two writers deadlock", "sqlite concurrency bites when two agents write at once")
        results = self.store.search(self.project, "wal")
        self.assertEqual(results[0].get("title"), "WAL mode needed")

    def test_feedback_terms_are_the_words_the_hit_is_about(self):
        self.note("WAL mode needed", "sqlite concurrency: enable WAL mode or two writers deadlock")
        hits = self.store._fts_hits(self.project, "wal", 20)
        terms = self.store._feedback_terms(hits, exclude={"wal"})
        self.assertIn("sqlite", terms)
        self.assertIn("concurrency", terms)
        self.assertNotIn("wal", terms)
        self.assertNotIn("the", terms)  # too short to be a term at all

    def test_a_word_in_half_the_index_is_not_an_expansion_term(self):
        for i in range(6):
            self.note(f"Note {i}", f"commonword detail number {i} sqlite")
        hits = self.store._fts_hits(self.project, "commonword", 20)
        self.assertNotIn("commonword", self.store._feedback_terms(hits))

    def test_no_hits_means_no_feedback(self):
        self.assertEqual(self.store._feedback_terms([]), [])

    def test_a_query_with_no_match_returns_nothing(self):
        self.note("WAL mode needed", "sqlite concurrency: enable WAL mode or two writers deadlock")
        self.assertEqual(self.store.search(self.project, "kubernetes ingress controller"), [])


if __name__ == "__main__":
    unittest.main()
