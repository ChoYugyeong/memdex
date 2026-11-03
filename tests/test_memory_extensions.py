"""Regression coverage for structured experiences and domain-time queries."""

import json
import os
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.store import SCHEMA_VERSION, Store
from memdex.tools import Session


class MemoryExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)
        self.session = Session(self.store, agent_name="test")
        self.session.cwd = self.root

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_experience_stores_structured_steps_and_indexes_context(self):
        saved = self.session.remember(
            "experience", "Recover Windows wheel build", "Build succeeded.",
            outcome="success", context="The stale artifact cache caused packaging failures.",
            steps=["delete build artifacts", "build in a clean venv"],
            evidence="the wheel installed in the clean environment")
        self.assertIn("saved experience #1", saved)
        hits = self.store.search(self.project, "stale artifact cache packaging", kinds=["experience"])
        self.assertEqual([row["id"] for row in hits], [1])
        self.assertEqual(hits[0]["outcome"], "success")
        self.assertEqual(json.loads(hits[0]["steps"]), ["delete build artifacts", "build in a clean venv"])
        full = self.session.note(1)
        self.assertIn("Outcome: success", full)
        self.assertIn("2. build in a clean venv", full)
        self.assertIn("Evidence: the wheel installed", full)

    def test_experience_requires_a_valid_outcome(self):
        result = self.session.remember("experience", "Compile a package")
        self.assertIn("require outcome", result)
        self.assertEqual(self.store.count_notes(self.project), 0)

    def test_domain_validity_is_separate_and_end_is_exclusive(self):
        old = self.session.remember("decision", "TLS policy", "Production verification required.",
                                    valid_from="2023-01-01", valid_until="2024-01-01")
        new = self.session.remember("decision", "TLS policy change", "Development test CA accepted.",
                                    valid_from="2024-01-01")
        self.assertIn("saved decision #1", old)
        self.assertIn("saved decision #2", new)
        before = self.store.search(self.project, "TLS policy", kinds=["decision"], valid_at=1_704_067_199)
        after = self.store.search(self.project, "TLS policy", kinds=["decision"], valid_at=1_704_067_200)
        self.assertEqual([hit["id"] for hit in before], [1])
        self.assertEqual([hit["id"] for hit in after], [2])

    def test_invalid_domain_interval_is_rejected(self):
        result = self.session.remember("decision", "Bad interval", valid_from="2024-02-01",
                                       valid_until="2024-01-01")
        self.assertIn("later than", result)
        self.assertEqual(self.store.count_notes(self.project), 0)

    def test_schema_migrates_with_temporal_and_experience_columns(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 10)
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(notes)")}
        self.assertTrue({"valid_from", "valid_until", "outcome", "context", "steps", "evidence"} <= columns)

    def test_v9_database_migrates_without_losing_existing_notes(self):
        path = os.path.join(self.tmp, "v9.db")
        old = Store(path)
        nid = old.add_note(self.project, "decision", "Existing note", "Keep this body.", "test")
        old.close()
        db = sqlite3.connect(path)
        for column in ("evidence", "steps", "context", "outcome", "valid_until", "valid_from"):
            db.execute(f"ALTER TABLE notes DROP COLUMN {column}")
        db.execute("PRAGMA user_version=9")
        db.commit()
        db.close()
        upgraded = Store(path)
        try:
            row = upgraded.db.execute("SELECT body, valid_from, valid_until, steps FROM notes WHERE id=?",
                                      (nid,)).fetchone()
            self.assertEqual(row["body"], "Keep this body.")
            self.assertIsNone(row["valid_from"])
            self.assertIsNone(row["valid_until"])
            self.assertEqual(row["steps"], "[]")
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
