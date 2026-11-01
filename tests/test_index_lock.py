"""Indexing must not hold the database's single writer slot for the whole scan.

A 5,000-file index takes seconds. If that work sits in one transaction, every other agent's `remember`
fails with "database is locked" — which is exactly what happened in the wild (a stuck server held the
lock and blocked the user's other sessions). `index()` commits every INDEX_COMMIT_EVERY files, and this
test pins that down: while one thread is indexing, another connection can still write.
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from memdex.project import resolve
from memdex import scan
from memdex.store import INDEX_COMMIT_EVERY, Store


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class IndexLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for i in range(INDEX_COMMIT_EVERY * 2 + 10):
            write(self.root, f"src/m{i}.py", f"def fn{i}():\n    return {i}\n")
        self.db = os.path.join(self.tmp, "memdex.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_writer_is_not_blocked_while_indexing(self):
        writer = Store(self.db)
        # Make indexing slow enough that the old single-transaction version would hold the lock.
        original = writer._index_file
        calls = {"n": 0}

        def slow_index(project, rel, prev):
            calls["n"] += 1
            time.sleep(0.004)
            return original(project, rel, prev)

        writer._index_file = slow_index
        project = resolve(self.root)

        done = threading.Event()

        def run_index():
            try:
                writer.index(project)
            finally:
                done.set()

        t = threading.Thread(target=run_index)
        t.start()
        time.sleep(0.03)  # let the scan start writing

        # A second connection (another agent) writes a note while indexing runs.
        other = Store(self.db)
        started = time.time()
        other.db.execute(
            "INSERT INTO notes(project, kind, title, body, author, refs, created_at) "
            "VALUES(?,?,?,?,?,?,?)", ("k", "note", "during index", "x", "qa", "[]", time.time()))
        other.db.commit()
        waited = time.time() - started
        other.close()

        done.wait(timeout=60)
        t.join(timeout=60)
        self.assertTrue(done.is_set(), "indexing did not finish")
        self.assertLess(waited, 5.0, f"the other writer waited {waited:.1f}s for the index lock")
        self.assertGreater(calls["n"], 0)

        check = Store(self.db)
        try:
            rows = check.db.execute("SELECT count(*) FROM notes WHERE title='during index'").fetchone()[0]
            files = check.db.execute("SELECT count(*) FROM files").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(rows, 1)
        self.assertEqual(files, INDEX_COMMIT_EVERY * 2 + 10)

    def test_batched_commit_size_is_sane(self):
        self.assertGreaterEqual(INDEX_COMMIT_EVERY, 10)
        self.assertLessEqual(INDEX_COMMIT_EVERY, 500)


if __name__ == "__main__":
    unittest.main()
