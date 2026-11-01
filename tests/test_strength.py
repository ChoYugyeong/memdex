"""Two strengths and interference: the last two rows of docs/LIMITS.md.

Retrieval strength is ACT-R's activation — how reachable a memory is now. Storage strength (Bjork & Bjork
1992) is how well learned it is, regardless of when it was last needed. memdex used only the first, so a
decision that forty sessions used but this month's did not could fall out of the brief as "cold"; the gate
now accepts either.

Retrieval-induced forgetting (Anderson, Bjork & Bjork 1994) is the interference side: when one memory is
worked over, its competitors come to mind less. Here the "retrieval" is a supersession — somebody decided
the note on those files was wrong — so surviving notes that share those files rank slightly lower.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.store import COLD, STORAGE_HOT, Store, activation, storage_strength


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("x = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("y = 2\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note(self, kind, title, body, files=None):
        return self.store.add_note(self.project, kind, title, body, "claude", files or [])

    def age(self, nid, *, created_days=0, used_days=None, uses=0, confirms=0):
        now = time.time()
        self.store.db.execute(
            "UPDATE notes SET created_at=?, last_used_at=?, use_count=?, confirm_count=? WHERE id=?",
            (now - created_days * 86400,
             None if used_days is None else now - used_days * 86400, uses, confirms, nid))
        self.store.db.commit()
        return self.store.get_note(self.project, nid)


class StrengthTests(Base):
    def test_storage_strength_ignores_recency(self):
        nid = self.note("decision", "sqlite only", "one file, no server")
        fresh = self.store.get_note(self.project, nid)
        old = self.age(nid, created_days=400, used_days=400, uses=40)
        self.assertGreater(storage_strength(old), storage_strength(fresh) - 1)  # uses raise it
        self.assertGreaterEqual(storage_strength(old), STORAGE_HOT)

    def test_activation_decays_but_storage_does_not(self):
        # Confirmed three times, never read since, two and a half years old: hard to reach right now,
        # well learned all the same.
        nid = self.note("todo", "sqlite only", "one file, no server")
        old = self.age(nid, created_days=900, confirms=3)
        self.assertLess(activation(old), COLD)                        # retrieval strength: cold
        self.assertGreaterEqual(storage_strength(old), STORAGE_HOT)   # storage strength: not cold

    def test_a_well_learned_old_note_stays_in_the_brief(self):
        nid = self.note("todo", "sqlite only", "one file, no server")
        self.age(nid, created_days=900, confirms=3)
        titles = [r["title"] for r in self.store.list_notes(self.project, "todo", 5)]
        self.assertIn("sqlite only", titles)

    def test_a_note_nobody_ever_used_still_goes_cold(self):
        self.age(self.note("note", "scratch thought", "something nobody needed"), created_days=900)
        self.note("note", "fresh thought", "written today")
        titles = [r["title"] for r in self.store.list_notes(self.project, "note", 5)]
        self.assertEqual(titles, ["fresh thought"])

    def test_confirms_count_toward_storage_strength(self):
        nid = self.note("decision", "sqlite only", "one file, no server")
        before = storage_strength(self.store.get_note(self.project, nid))
        self.store.confirm_note(self.project, nid, "claude")
        after = storage_strength(self.store.get_note(self.project, nid))
        self.assertGreater(after, before)


class InterferenceTests(Base):
    def test_a_supersession_on_the_same_file_lowers_the_survivor(self):
        bad = self.note("pitfall", "WAL breaks on NFS", "old claim", ["src/store.py"])
        good = self.note("pitfall", "WAL is fine on NFS", "it was a mount option", ["src/store.py"])
        other = self.note("pitfall", "api paging", "50 per page", ["src/api.py"])
        self.store.db.execute("UPDATE notes SET superseded_by=? WHERE id=?", (good, bad))
        self.store.db.commit()
        rows = self.store.list_notes(self.project, "pitfall", 5)
        order = [r["title"] for r in rows]
        self.assertLess(order.index("WAL is fine on NFS"), order.index("api paging") + 99)
        # The penalty is real but small: the survivor is still listed.
        self.assertIn("WAL is fine on NFS", order)

    def test_no_supersessions_means_no_penalty(self):
        a = self.note("pitfall", "one", "text", ["src/store.py"])
        b = self.note("pitfall", "two", "text", ["src/api.py"])
        self.assertEqual(self.store._suppressed_paths(self.project), set())
        self.assertEqual([r["title"] for r in self.store.list_notes(self.project, "pitfall", 5)]
                         .count("one"), 1)

    def test_the_penalty_is_proportional_and_bounded(self):
        from memdex.store import _suppression
        note = {"refs": json.dumps([{"path": "src/store.py"}, {"path": "src/api.py"}])}
        self.assertEqual(_suppression(note, set()), 0.0)
        self.assertAlmostEqual(_suppression(note, {"src/store.py"}), 0.15)
        self.assertAlmostEqual(_suppression(note, {"src/store.py", "src/api.py"}), 0.3)
        self.assertAlmostEqual(_suppression(note, {"elsewhere.py"}), 0.0)


if __name__ == "__main__":
    unittest.main()
