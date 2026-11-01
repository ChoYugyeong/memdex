"""Bring-your-own vectors: semantic search without a model inside memdex.

The agent already has a model. It can compute the vector for a note it saves and the vector for a query,
and memdex stores the first and does exact cosine against it — no Ollama, no library, and the rule that
nothing inside memdex calls out to a model stays true.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import embed
from memdex import project as projects
from memdex.store import Store
from memdex.tools import Session


class NormalizeTests(unittest.TestCase):
    def test_the_vector_comes_back_unit_length(self):
        blob = embed.normalize([3.0, 4.0])
        self.assertIsNotNone(blob)
        self.assertAlmostEqual(embed.similarity(blob, blob), 1.0, places=5)

    def test_similarity_is_the_cosine(self):
        a = embed.normalize([1.0, 0.0])
        b = embed.normalize([1.0, 1.0])
        self.assertAlmostEqual(embed.similarity(a, b), 0.7071067, places=4)

    def test_junk_is_refused_not_raised(self):
        self.assertIsNone(embed.normalize([]))
        self.assertIsNone(embed.normalize(["not", "numbers"]))
        self.assertIsNone(embed.normalize(None))


class VectorSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("x = 1\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)
        self.session = Session(self.store, agent_name="qa")
        self.session.cwd = self.root
        self.session.home = self.root
        os.environ.pop("MEMDEX_EMBED", None)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_saved_vector_is_found_by_a_query_vector(self):
        # Two notes about different things; the vectors say which is which, the words do not.
        self.session.remember("note", "Retry loop", "the worker retries on failure",
                              embedding=[1.0, 0.0, 0.0])
        self.session.remember("note", "Colour scheme", "the palette uses slate and amber",
                              embedding=[0.0, 1.0, 0.0])
        out = self.session.search("worker", embedding=[0.95, 0.05, 0.0])
        self.assertIn("Retry loop", out)

    def test_without_a_query_vector_and_without_ollama_search_is_keyword_only(self):
        self.session.remember("note", "Retry loop", "the worker retries on failure",
                              embedding=[1.0, 0.0, 0.0])
        self.assertNotIn("Retry loop", self.session.search("palette"))

    def test_the_vector_lands_in_the_vectors_table(self):
        self.session.remember("note", "Retry loop", "the worker retries on failure",
                              embedding=[1.0, 0.0, 0.0])
        rows = self.store.db.execute("SELECT src, ref FROM vectors").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["src"], "note")

    def test_a_note_saved_without_a_vector_still_saves(self):
        self.assertIn("saved note", self.session.remember("note", "Plain", "no vector here"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)

    def test_a_bad_vector_does_not_lose_the_note(self):
        self.assertIn("saved note", self.session.remember("note", "Retry", "body", embedding=["x"]))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
