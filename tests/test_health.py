"""Health surface: schema versioning, `memdex doctor`, and the brief budget.

These are the things a user hits when something is wrong (an old database, a
project that is not indexed) or when a session spends too much context on the
brief. They must fail with a sentence, not a traceback.
"""

import os
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path

from memdex import doctor
from memdex.project import resolve
from memdex.store import MIGRATIONS, SCHEMA_VERSION, Store
from memdex.tools import BRIEF_CHARS, Session, render_brief


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.db = os.path.join(self.tmp, "memdex.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def user_version(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()

    def test_fresh_database_is_stamped(self):
        store = Store(self.db)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertEqual(self.user_version(), SCHEMA_VERSION)
        finally:
            store.close()

    def test_database_from_before_versioning_is_upgraded_in_place(self):
        store = Store(self.db)
        store.add_note(resolve(self.root), "decision", "keep sqlite", "zero deps", "claude")
        store.db.execute("PRAGMA user_version=0")  # what 0.1.0 databases look like
        store.db.commit()
        store.close()
        self.assertEqual(self.user_version(), 0)

        store = Store(self.db)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertEqual(self.user_version(), SCHEMA_VERSION)
            self.assertEqual(store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
        finally:
            store.close()

    def test_newer_database_is_refused_with_an_explanation(self):
        Store(self.db).close()
        con = sqlite3.connect(self.db)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError) as caught:
            Store(self.db)
        message = str(caught.exception)
        self.assertIn("newer", message)
        self.assertIn("pip install -U memdex-mcp", message)

    def test_every_declared_migration_targets_a_version(self):
        for version in MIGRATIONS:
            self.assertGreater(version, 0)
            self.assertLessEqual(version, SCHEMA_VERSION)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.db = os.path.join(self.tmp, "memdex.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthy_install_has_no_failures(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        store = Store(self.db)
        session = Session(store, agent_name="doctor")
        session.cwd = self.root
        store.index(session.project)
        store.close()
        rep = doctor.run(path=self.root, db_path=self.db, offline=True)
        text = rep.render()
        self.assertFalse(rep.failed)
        self.assertIn("memdex doctor", text)
        self.assertIn("schema v", text)
        self.assertIn("writable", text)
        self.assertIn("mcp tools", text)
        self.assertIn("all good", text)

    def test_empty_project_is_only_a_warning(self):
        rep = doctor.run(path=self.root, db_path=self.db, offline=True)
        self.assertFalse(rep.failed)
        self.assertTrue(rep.warned)
        self.assertIn("not indexed yet", rep.render())

    def test_database_from_the_future_is_reported_not_raised(self):
        Store(self.db).close()
        con = sqlite3.connect(self.db)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 3}")
        con.commit()
        con.close()
        rep = doctor.run(path=self.root, db_path=self.db, offline=True)
        self.assertTrue(rep.failed)
        self.assertIn("newer", rep.render())

    def test_optional_integrations_are_described(self):
        rep = doctor.run(path=self.root, db_path=self.db, offline=True)
        text = rep.render()
        self.assertIn("kensaku", text)
        self.assertIn("ollama", text)


class BriefBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store = Store(os.path.join(self.tmp, "memdex.db"))
        self.session = Session(self.store, agent_name="claude")
        self.session.cwd = self.root
        self.project = resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_long_brief_is_trimmed_with_a_note(self):
        for i in range(12):
            self.session.remember("pitfall", f"pitfall number {i} " + "x" * 120, "body " + "y" * 200)
        text = render_brief(self.store, self.project, "claude", max_chars=400)
        self.assertLessEqual(len(text), 400 + 200)  # trim message is one extra line
        self.assertIn("trimmed", text)
        self.assertIn("max-chars 0", text)

    def test_zero_means_no_limit(self):
        for i in range(12):
            self.session.remember("pitfall", f"pitfall number {i} " + "x" * 120, "body " + "y" * 200)
        text = render_brief(self.store, self.project, "claude", max_chars=0)
        self.assertNotIn("trimmed", text)
        self.assertIn("pitfalls:", text)

    def test_short_brief_is_left_alone(self):
        text = render_brief(self.store, self.project, "claude")
        self.assertNotIn("trimmed", text)
        self.assertLess(len(text), BRIEF_CHARS)


if __name__ == "__main__":
    unittest.main()
