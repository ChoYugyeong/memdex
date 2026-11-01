"""One hop of spreading activation in the brief.

A note about a file this session is touching is worth more than an equally accessible note about a file
nobody is looking at. The links already exist (every note records the files it was based on), so the hop is
a set intersection — no graph is invented from co-occurrence.
"""

import json
import unittest

from memdex import tools


def note(i, *paths):
    return {"id": i, "refs": json.dumps([{"path": p} for p in paths]) if paths else "[]"}


class SpreadTests(unittest.TestCase):
    def test_a_note_about_a_seeded_file_moves_ahead(self):
        rows = [note(1, "other.py"), note(2, "src/store.py"), note(3)]
        out = tools._spread(rows, {"src/store.py"})
        self.assertEqual([r["id"] for r in out], [2, 1, 3])

    def test_without_seeds_the_order_is_untouched(self):
        rows = [note(1, "a.py"), note(2, "b.py")]
        self.assertEqual(tools._spread(rows, set()), rows)

    def test_accessibility_order_survives_within_the_boosted_group(self):
        rows = [note(1, "x.py"), note(2, "a.py"), note(3, "b.py")]
        out = tools._spread(rows, {"b.py", "a.py"})
        self.assertEqual([r["id"] for r in out], [2, 3, 1])

    def test_a_note_without_refs_is_not_boosted(self):
        rows = [note(1), note(2, "src/store.py")]
        out = tools._spread(rows, {"src/store.py"})
        self.assertEqual([r["id"] for r in out], [2, 1])


if __name__ == "__main__":
    unittest.main()
