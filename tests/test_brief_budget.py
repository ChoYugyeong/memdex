"""Choosing what fits in the brief: knapsack for the budget, MMR for diversity.

The brief used to drop the lowest-priority line until the text fit, which punishes a short high-value line
for the crime of a long low-value one, and it happily spent three lines on the same file. These are the two
replacement policies, tested as functions so the behaviour does not depend on a full session.
"""

import json
import unittest

from memdex import tools


def note(i, *paths):
    return {"id": i, "refs": json.dumps([{"path": p} for p in paths]) if paths else "[]"}


class KnapsackTests(unittest.TestCase):
    def test_a_small_high_value_line_beats_a_long_low_value_one(self):
        items = [(1, "x" * 100), (4, "small")]
        kept = tools._knapsack(items, budget=20)
        self.assertEqual(kept, [(4, "small")])

    def test_everything_is_kept_when_it_fits(self):
        items = [(1, "a"), (4, "b"), (2, "c")]
        self.assertEqual(tools._knapsack(items, budget=100), items)

    def test_order_is_preserved(self):
        # Room for two of three: the best pair is a and c, and they come back in their original order.
        items = [(3, "a" * 10), (1, "b" * 10), (2, "c" * 10)]
        kept = tools._knapsack(items, budget=23)
        self.assertEqual([text[0] for _, text in kept], ["a", "c"])

    def test_a_zero_budget_keeps_nothing(self):
        self.assertEqual(tools._knapsack([(5, "x")], budget=0), [])

    def test_it_beats_greedy_on_the_case_greedy_loses(self):
        # Budget 30: greedy-by-priority takes the 3-point 25-char line and then nothing.
        # The right answer is the two 2-point 14-char lines: 4 points for 28 chars.
        items = [(3, "a" * 25), (2, "b" * 14), (2, "c" * 14)]
        kept = tools._knapsack(items, budget=30)
        self.assertEqual([t[0] for _, t in kept], ["b", "c"])


class MmrTests(unittest.TestCase):
    def test_the_second_pick_avoids_repeating_the_first_file(self):
        picked = tools._mmr_pick([note(1, "a.py"), note(2, "a.py"), note(3, "b.py")], limit=2)
        self.assertEqual([r["id"] for r in picked], [1, 3])

    def test_it_keeps_the_accessibility_order_when_nothing_overlaps(self):
        picked = tools._mmr_pick([note(1, "a.py"), note(2, "b.py"), note(3, "c.py")], limit=3)
        self.assertEqual([r["id"] for r in picked], [1, 2, 3])

    def test_notes_without_refs_do_not_collide(self):
        picked = tools._mmr_pick([note(1), note(2), note(3)], limit=2)
        self.assertEqual([r["id"] for r in picked], [1, 2])

    def test_a_broken_refs_value_is_survivable(self):
        broken = {"id": 9, "refs": "not json"}
        picked = tools._mmr_pick([note(1, "a.py"), broken], limit=2)
        self.assertEqual({r["id"] for r in picked}, {1, 9})


if __name__ == "__main__":
    unittest.main()
