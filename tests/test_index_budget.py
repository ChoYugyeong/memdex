"""The brief's index is capped so one agent-facing call cannot run for minutes.

`Store.index(project, budget=n)` reads at most n files and reports the rest as `pending`; the next call
continues where it stopped. `memdex index` passes no budget, so the CLI still finishes the whole scan.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import project as projects
from memdex import tools
from memdex.cli import main
from memdex.store import Store
from memdex.tools import render_brief


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        for i in range(6):
            Path(self.root, "src", f"mod{i}.py").write_text(f"VALUE = {i}\n", encoding="utf-8")
        Path(self.root, "README.md").write_text("# demo\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)
        self.env = os.environ.get("MEMDEX_INDEX_BUDGET")

    def tearDown(self):
        self.store.close()
        if self.env is None:
            os.environ.pop("MEMDEX_INDEX_BUDGET", None)
        else:
            os.environ["MEMDEX_INDEX_BUDGET"] = self.env
        shutil.rmtree(self.tmp, ignore_errors=True)


class BudgetTests(Base):
    def test_a_budget_stops_early_and_reports_the_rest(self):
        st = self.store.index(self.project, budget=3)
        self.assertEqual(st["files"], 7)
        self.assertEqual(st["changed"], 3)
        self.assertEqual(st["pending"], 4)

    def test_the_next_call_continues_where_it_stopped(self):
        self.store.index(self.project, budget=3)
        st = self.store.index(self.project, budget=3)
        self.assertEqual(st["changed"], 3)
        self.assertEqual(st["pending"], 1)
        st = self.store.index(self.project, budget=3)
        self.assertEqual(st["pending"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 7)

    def test_no_budget_indexes_everything(self):
        st = self.store.index(self.project)
        self.assertEqual(st["changed"], 7)
        self.assertEqual(st["pending"], 0)

    def test_a_zero_budget_is_not_a_budget(self):
        st = self.store.index(self.project, budget=0)
        self.assertEqual(st["changed"], 7)


class EnvTests(Base):
    def test_default_is_the_constant(self):
        os.environ.pop("MEMDEX_INDEX_BUDGET", None)
        self.assertEqual(tools.index_budget(), tools.INDEX_BUDGET)

    def test_zero_means_no_cap(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "0"
        self.assertIsNone(tools.index_budget())

    def test_a_bad_value_falls_back_to_the_default(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "lots"
        self.assertEqual(tools.index_budget(), tools.INDEX_BUDGET)

    def test_an_explicit_value_is_used(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "42"
        self.assertEqual(tools.index_budget(), 42)


class BriefTests(Base):
    def test_the_brief_says_how_much_is_left(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "2"
        text = render_brief(self.store, self.project, "tester")
        self.assertIn("7 files", text)
        self.assertIn("5 left for the next brief", text)

    def test_the_brief_is_quiet_when_the_index_is_complete(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "0"
        text = render_brief(self.store, self.project, "tester")
        self.assertNotIn("left for the next brief", text)

    def test_the_cli_index_is_not_capped(self):
        os.environ["MEMDEX_INDEX_BUDGET"] = "2"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["index", "--path", self.root, "--db", self.store.path])
        self.assertIn("7 files", buf.getvalue())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main()
