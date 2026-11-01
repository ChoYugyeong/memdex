"""`memdex bench` and `memdex prune`: the numbers users judge memdex by, and the
cleanup that keeps a long-lived database from growing forever.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex.maintenance import estimate_tokens, prune, savings
from memdex.project import resolve
from memdex.store import Store


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self, sql, params=()):
        return int(self.store.db.execute(sql, params).fetchone()[0])


class EstimateTests(unittest.TestCase):
    def test_rounds_up_and_never_negative(self):
        self.assertEqual(estimate_tokens(0), 0)
        self.assertEqual(estimate_tokens(1), 1)
        self.assertEqual(estimate_tokens(4), 1)
        self.assertEqual(estimate_tokens(5), 2)
        self.assertEqual(estimate_tokens(-10), 0)


class SavingsTests(Base):
    def test_large_files_save_the_most(self):
        write(self.root, "small.py", "X = 1\n")
        body = "\n".join(f"def fn{i}():\n    return {i}" for i in range(120))
        write(self.root, "big.py", body + "\n")
        self.store.index(self.project)

        rep = savings(self.store, self.project, top=5)
        self.assertEqual(rep["files"], 2)
        self.assertGreater(rep["bytes"], 0)
        self.assertGreater(rep["full_tokens"], rep["outline_tokens"])
        self.assertGreater(rep["saved_tokens"], 0)
        self.assertLess(rep["ratio"], 1.0)
        self.assertEqual([f["path"] for f in rep["top"]][0], "big.py")
        self.assertEqual(rep["top"][0]["saved_chars"],
                         rep["top"][0]["bytes"] - rep["top"][0]["outline_chars"])

    def test_sorted_by_saved_chars(self):
        write(self.root, "a.py", "\n".join(f"def a{i}(): pass" for i in range(40)) + "\n")
        write(self.root, "b.py", "\n".join(f"def b{i}(): pass" for i in range(120)) + "\n")
        self.store.index(self.project)
        saved = [f["saved_chars"] for f in savings(self.store, self.project, top=10)["top"]]
        self.assertEqual(saved, sorted(saved, reverse=True))

    def test_empty_project_is_safe(self):
        rep = savings(self.store, self.project)
        self.assertEqual(rep["files"], 0)
        self.assertEqual(rep["ratio"], 0.0)
        self.assertEqual(rep["top"], [])

    def test_outline_is_much_smaller_than_the_source(self):
        write(self.root, "service.py", "\n".join(f"class C{i}:\n    def m(self):\n        pass" for i in range(200)))
        self.store.index(self.project)
        rep = savings(self.store, self.project)
        self.assertLess(rep["ratio"], 0.5)


class PruneTests(Base):
    def test_missing_root_is_pruned(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store.index(self.project)
        self.assertEqual(self.count("SELECT count(*) FROM files"), 1)
        self.assertGreater(self.count("SELECT count(*) FROM symbols"), 0)

        shutil.rmtree(self.root)
        rep = prune(self.store, vacuum=False)
        self.assertEqual(rep["stale_roots"], [os.path.realpath(self.root)])
        self.assertEqual(rep["files"], 1)
        self.assertGreaterEqual(rep["symbols"], 1)
        self.assertEqual(self.count("SELECT count(*) FROM files"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM symbols"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM fts_map WHERE src='file'"), 0)

    def test_dry_run_changes_nothing(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store.index(self.project)
        shutil.rmtree(self.root)
        rep = prune(self.store, dry_run=True, vacuum=False)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["files"], 1)
        self.assertEqual(self.count("SELECT count(*) FROM files"), 1)

    def test_live_projects_are_left_alone(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        write(self.root, "a.py", "def a():\n    pass\n")
        write(other, "b.py", "def b():\n    pass\n")
        self.store.index(self.project)
        self.store.index(resolve(other))

        shutil.rmtree(self.root)
        prune(self.store, vacuum=False)
        paths = [r[0] for r in self.store.db.execute("SELECT path FROM files")]
        self.assertEqual(paths, ["b.py"])

    def test_notes_survive_unless_all_is_given(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store.index(self.project)
        self.store.add_note(self.project, "decision", "Keep SQLite", "zero deps", "claude")

        moved = os.path.join(self.tmp, "moved-away")
        os.rename(self.root, moved)  # looks deleted to prune
        rep = prune(self.store, vacuum=False)
        self.assertEqual(rep["notes"], 0)
        self.assertEqual(self.count("SELECT count(*) FROM notes"), 1)

        rep = prune(self.store, all_projects=True, vacuum=False)
        self.assertEqual(rep["notes"], 1)
        self.assertEqual(self.count("SELECT count(*) FROM notes"), 0)

    def test_nothing_to_do_is_cheap_and_quiet(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store.index(self.project)
        rep = prune(self.store, vacuum=False)
        self.assertEqual(rep["stale_roots"], [])
        self.assertEqual(rep["files"], 0)
        self.assertEqual(rep["bytes_after"], rep["bytes_before"])

    def test_vacuum_runs_after_pruning(self):
        write(self.root, "app.py", "def main():\n    pass\n")
        self.store.index(self.project)
        shutil.rmtree(self.root)
        rep = prune(self.store, vacuum=True)
        self.assertIn("vacuumed", rep)
        self.assertEqual(self.count("SELECT count(*) FROM files"), 0)


if __name__ == "__main__":
    unittest.main()
