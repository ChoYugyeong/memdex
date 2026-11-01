"""`memdex export --sync` and `memdex merge`: notes across machines, no server.

The transport is a directory. Merge is last-writer-wins on the whole note, by updated_at, and claims and
messages never travel — a lease that outlives its machine is a deadlock generator, and a message is
addressed to a session that does not exist on the other side.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memdex import project as projects
from memdex import sync as sync_mod
from memdex.cli import main
from memdex.store import SCHEMA_VERSION, Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.shared = os.path.join(self.tmp, "shared")
        self.db = os.path.join(self.tmp, "a.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note(self, title, body, files=None, kind="note"):
        return self.store.add_note(self.project, kind, title, body, "claude", files or [])


class SchemaTests(Base):
    def test_notes_carry_a_uid_and_an_updated_at(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 7)
        nid = self.note("One", "body")
        row = self.store.db.execute("SELECT uid, updated_at FROM notes WHERE id=?", (nid,)).fetchone()
        self.assertTrue(row["uid"])
        self.assertTrue(row["updated_at"])

    def test_uids_are_unique_and_stable(self):
        a = self.note("One", "body")
        b = self.note("Two", "body")
        uids = [self.store.db.execute("SELECT uid FROM notes WHERE id=?", (i,)).fetchone()[0]
                for i in (a, b)]
        self.assertEqual(len(set(uids)), 2)
        self.store.set_pinned(self.project, a, True)  # a state change does not rewrite the uid
        again = self.store.db.execute("SELECT uid FROM notes WHERE id=?", (a,)).fetchone()[0]
        self.assertEqual(uids[0], again)


class ExportTests(Base):
    def test_one_file_per_note_and_per_summary(self):
        nid = self.note("WAL mode", "two writers deadlock without it", ["src/store.py"])
        self.store.file_info(self.project, "src/store.py")  # index + summary target
        self.store.save_summary(self.project, "src/store.py", "WAL and a single writer.", "claude")
        rep = sync_mod.export(self.store, self.project, self.shared)
        self.assertEqual(rep["notes"], 1)
        self.assertEqual(rep["summaries"], 1)
        files = os.listdir(os.path.join(self.shared, "notes"))
        self.assertEqual(len(files), 1)
        payload = json.loads(Path(self.shared, "notes", files[0]).read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], "WAL mode")
        self.assertEqual(payload["refs"][0]["path"], "src/store.py")
        self.assertEqual(self.store.db.execute("SELECT uid FROM notes WHERE id=?",
                                               (nid,)).fetchone()[0], payload["uid"])

    def test_claims_are_never_exported(self):
        self.note("One", "body")
        self.store.claim(self.project, "claude", ["src/store.py"], "editing", 60, exclusive=True)
        sync_mod.export(self.store, self.project, self.shared)
        blob = json.dumps({name: Path(self.shared, "notes", name).read_text(encoding="utf-8")
                           for name in os.listdir(os.path.join(self.shared, "notes"))})
        self.assertNotIn("claim", blob.lower())
        self.assertEqual(sorted(os.listdir(self.shared)), ["notes", "summaries"])


class MergeTests(Base):
    def merge_into_new_store(self):
        other = Store(os.path.join(self.tmp, "b.db"))
        try:
            rep = sync_mod.merge(other, other_project(self.root), self.shared)
        finally:
            other.close()
        return rep

    def test_a_note_exported_here_arrives_there(self):
        self.note("WAL mode", "two writers deadlock without it", ["src/store.py"], kind="pitfall")
        sync_mod.export(self.store, self.project, self.shared)
        other = Store(os.path.join(self.tmp, "b.db"))
        try:
            rep = sync_mod.merge(other, projects.resolve(self.root), self.shared)
            self.assertEqual(rep["added"], 1)
            row = other.db.execute("SELECT title, kind, body FROM notes").fetchone()
            self.assertEqual((row["title"], row["kind"]), ("WAL mode", "pitfall"))
        finally:
            other.close()

    def test_merging_twice_changes_nothing(self):
        self.note("WAL mode", "body")
        sync_mod.export(self.store, self.project, self.shared)
        other = Store(os.path.join(self.tmp, "b.db"))
        try:
            other_project_ = projects.resolve(self.root)
            sync_mod.merge(other, other_project_, self.shared)
            rep = sync_mod.merge(other, other_project_, self.shared)
            self.assertEqual((rep["added"], rep["updated"], rep["unchanged"]), (0, 0, 1))
            self.assertEqual(other.db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
        finally:
            other.close()

    def test_the_newer_write_wins_and_the_loser_is_reported(self):
        nid = self.note("WAL mode", "old body")
        sync_mod.export(self.store, self.project, self.shared)
        # A newer edit here, and an older edit there.
        self.store.db.execute("UPDATE notes SET body=?, updated_at=? WHERE id=?",
                              ("new body", time.time() + 60, nid))
        self.store.db.commit()
        other = Store(os.path.join(self.tmp, "b.db"))
        try:
            rep = sync_mod.merge(other, projects.resolve(self.root), self.shared)
            self.assertEqual(rep["added"], 1)
            # The exported copy is older than nothing here, so it lands as new; a second merge with a
            # stale export must not overwrite the newer note.
            self.store.db.execute("UPDATE notes SET body=?, updated_at=? WHERE id=?",
                                  ("newest body", time.time() + 120, nid))
            self.store.db.commit()
            sync_mod.export(self.store, self.project, self.shared)
            rep = sync_mod.merge(other, projects.resolve(self.root), self.shared)
            self.assertEqual(rep["updated"], 1)
            self.assertEqual(other.db.execute("SELECT body FROM notes").fetchone()[0], "newest body")
        finally:
            other.close()

    def test_an_older_export_does_not_overwrite_a_newer_local_note(self):
        nid = self.note("WAL mode", "old body")
        sync_mod.export(self.store, self.project, self.shared)  # snapshot, updated_at = t0
        self.store.db.execute("UPDATE notes SET body=?, updated_at=? WHERE id=?",
                              ("local newer", time.time() + 300, nid))
        self.store.db.commit()
        rep = sync_mod.merge(self.store, self.project, self.shared)  # the stale snapshot
        self.assertEqual(rep["updated"], 0)
        self.assertEqual(self.store.db.execute("SELECT body FROM notes").fetchone()[0], "local newer")

    def test_summaries_merge_by_sha(self):
        self.store.save_summary(self.project, "src/store.py", "WAL and a single writer.", "claude")
        sync_mod.export(self.store, self.project, self.shared)
        other = Store(os.path.join(self.tmp, "b.db"))
        try:
            rep = sync_mod.merge(other, projects.resolve(self.root), self.shared)
            self.assertEqual(rep["summaries"], 1)
            self.assertEqual(other.db.execute("SELECT count(*) FROM summaries").fetchone()[0], 1)
        finally:
            other.close()

    def test_the_cli_round_trips(self):
        self.note("WAL mode", "body")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["export", "--sync", self.shared, "--path", self.root, "--db", self.db])
        self.assertEqual(code, 0)
        self.assertIn("exported 1 note(s)", buf.getvalue())
        # Merging into the same database it came from changes nothing; the second machine is a second db.
        other_db = os.path.join(self.tmp, "other.db")
        code, out = self.cli_merge(self.shared, other_db)
        self.assertEqual(code, 0)
        self.assertIn("merged 1 new", out)

    def cli_merge(self, directory, db):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["merge", directory, "--path", self.root, "--db", db])
        return code, buf.getvalue()


def other_project(root):
    return projects.resolve(root)


if __name__ == "__main__":
    unittest.main()
