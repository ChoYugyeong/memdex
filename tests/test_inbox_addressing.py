"""Mailbox addressing, delivery windows, and rename aliases.

These pin the behaviour a peer review of task #5 relied on: an @checkout recipient stays inside that
checkout, a bare name broadcasts, a sub-address never reaches its parent, a digest window cannot
double-deliver, and a rename carries live state without losing mail.
"""

import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.store import Store, _addressed_to


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("x = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, sender, body, to=None):
        return self.store.post(self.project, sender, body, to)

    def inbox(self, agent, limit=20):
        return self.store.inbox_snapshot(self.project, agent, limit=limit)


class AddressingMatrixTests(unittest.TestCase):
    """Who reads what. The table is the contract."""

    CASES = [
        # recipient,              agent,                   expected, why
        (None, "gpt@repoB", True, "no recipient is a broadcast"),
        ("gpt", "gpt@repoB", True, "bare name reaches any checkout"),
        ("gpt", "gpt/resume", True, "bare name reaches a sub-addressed session"),
        ("gpt", "gpt/resume@repoB", True, "bare name reaches sub-address in any checkout"),
        ("gpt@repoA", "gpt@repoA", True, "exact checkout"),
        ("gpt@repoA", "gpt@repoB", False, "another checkout must not receive"),
        ("gpt@repoA", "gpt/resume@repoA", True, "checkout reaches its own sub-address"),
        ("gpt@repoA", "gpt/resume@repoB", False, "checkout must not leak to another checkout"),
        ("gpt/resume", "gpt", False, "a sub-address never reaches the parent"),
        ("gpt/resume1", "gpt/resume2", False, "sibling sub-addresses stay apart"),
        ("gpt", "codex", False, "different agents"),
        ("GPT@repoA", "gpt@repoA", True, "matching is case-insensitive"),
        ("gpt@RepoA", "gpt@repoA", True, "checkout comparison is case-insensitive"),
    ]

    def test_matrix(self):
        for recipient, agent, expected, why in self.CASES:
            with self.subTest(recipient=recipient, agent=agent, why=why):
                self.assertIs(_addressed_to(recipient, agent), expected, why)


class MailboxCursorTests(Base):
    def test_peek_does_not_consume(self):
        self.post("codex@demo", "hello", "gpt@demo")
        first, _ = self.inbox("gpt@demo")
        second, _ = self.inbox("gpt@demo")
        self.assertEqual([m["id"] for m in first], [m["id"] for m in second])
        self.store.inbox(self.project, "gpt@demo")
        third, _ = self.inbox("gpt@demo")
        self.assertEqual(third, [])

    def test_limit_keeps_the_rest_for_the_next_read(self):
        ids = [self.post("codex@demo", f"m{i}", "gpt@demo") for i in range(5)]
        page, _ = self.inbox("gpt@demo", limit=2)
        self.assertEqual([m["id"] for m in page], ids[:2])
        self.store.inbox(self.project, "gpt@demo", limit=2)
        rest = self.store.inbox(self.project, "gpt@demo")
        self.assertEqual(len(rest), 3)

    def test_messages_for_someone_else_are_skipped_but_pass_the_cursor(self):
        self.post("codex@demo", "not yours", "claude@demo")
        self.post("codex@demo", "yours", "gpt@demo")
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual([m["body"] for m in seen], ["yours"])

    def test_a_sender_never_sees_its_own_message(self):
        self.post("gpt@demo", "note to self", "gpt@demo")
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual(seen, [])

    def test_a_mixed_case_sender_does_not_echo(self):
        self.post("GPT@demo", "posted by me with odd case", "gpt@demo")
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual([m["body"] for m in seen], [])

    def test_a_mixed_case_recipient_still_arrives(self):
        self.post("codex@demo", "hello", "GPT@Demo")
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual([m["body"] for m in seen], ["hello"])


class DeliveryWindowTests(Base):
    AGENT = "gpt@demo"

    def setUp(self):
        super().setUp()
        self.post("codex@demo", "one", self.AGENT)
        self.post("codex@demo", "two", self.AGENT)

    def batch(self, now, interval=60.0, limit=20):
        return self.store.prepare_inbox_batch(
            self.project, self.AGENT, interval, now=now, limit=limit
        )

    def test_the_first_poll_opens_a_window_and_holds(self):
        msgs, cursor, due, next_at = self.batch(1000.0)
        self.assertFalse(due)
        self.assertEqual(len(msgs), 2)
        self.assertAlmostEqual(next_at, 1060.0)
        self.assertGreaterEqual(cursor, 1)

    def test_repeated_polls_inside_the_window_stay_quiet(self):
        self.batch(1000.0)
        for now in (1001.0, 1030.0, 1059.9):
            msgs, _, due, next_at = self.batch(now)
            self.assertFalse(due, now)
            self.assertAlmostEqual(next_at, 1060.0)
            self.assertEqual(len(msgs), 2)

    def test_the_due_poll_delivers_and_moves_the_window(self):
        self.batch(1000.0)
        msgs, _, due, next_at = self.batch(1060.0)
        self.assertTrue(due)
        self.assertEqual(len(msgs), 2)
        self.assertAlmostEqual(next_at, 1120.0)

    def test_a_second_caller_at_the_same_instant_cannot_double_deliver(self):
        self.batch(1000.0)
        _, _, first_due, _ = self.batch(1060.0)
        _, _, second_due, _ = self.batch(1060.0)
        self.assertTrue(first_due)
        self.assertFalse(second_due)

    def test_no_messages_does_not_open_a_window(self):
        self.store.inbox(self.project, self.AGENT)  # drain
        msgs, _, due, next_at = self.batch(2000.0)
        self.assertEqual(msgs, [])
        self.assertFalse(due)
        self.assertIsNone(next_at)

    def test_delivery_retries_when_the_caller_never_acknowledges(self):
        self.batch(1000.0)
        _, _, due, _ = self.batch(1060.0)          # reserved, but the caller "crashes"
        self.assertTrue(due)
        _, _, due_again, _ = self.batch(1061.0)
        self.assertFalse(due_again)                # window moved on, not lost
        msgs, _, due_later, _ = self.batch(1120.0)
        self.assertTrue(due_later)
        self.assertEqual(len(msgs), 2)

    def test_acknowledging_after_delivery_ends_the_mail(self):
        self.batch(1000.0)
        msgs, cursor, due, _ = self.batch(1060.0)
        self.store.acknowledge_inbox(self.project, self.AGENT, cursor)
        remaining, _, _, _ = self.batch(1061.0)
        self.assertEqual(remaining, [])

    def test_an_interval_must_be_positive_and_finite(self):
        for bad in (0.0, -5.0, float("inf"), float("nan")):
            with self.subTest(interval=bad):
                with self.assertRaises(ValueError):
                    self.store.prepare_inbox_batch(self.project, self.AGENT, bad, now=1.0)


class AckAndRenewTests(Base):
    def test_acknowledge_never_moves_the_cursor_backwards(self):
        self.post("codex@demo", "a", "gpt@demo")
        self.post("codex@demo", "b", "gpt@demo")
        self.store.acknowledge_inbox(self.project, "gpt@demo", 2)
        self.store.acknowledge_inbox(self.project, "gpt@demo", 1)
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual(seen, [])

    def test_acknowledge_ignores_a_non_positive_cursor(self):
        self.post("codex@demo", "a", "gpt@demo")
        self.store.acknowledge_inbox(self.project, "gpt@demo", 0)
        seen, _ = self.inbox("gpt@demo")
        self.assertEqual(len(seen), 1)


class RenameTests(Base):
    def test_rename_carries_live_state_and_leaves_an_alias(self):
        self.post("codex@demo", "for the old name", "deepseek@demo")
        self.store.acknowledge_inbox(self.project, "deepseek@demo", 0)
        counts = self.store.rename_agent("deepseek", "dsh@demo")
        self.assertGreaterEqual(counts["messages"], 1)
        seen, _ = self.inbox("dsh@demo")
        self.assertEqual([m["body"] for m in seen], ["for the old name"])
        self.assertEqual(self.store.canonical_agent("deepseek@demo"), "dsh@demo")

    def test_a_message_sent_to_the_old_name_lands_on_the_new_one(self):
        self.store.rename_agent("deepseek", "dsh@demo")
        canonical = self.store.canonical_agent("deepseek")
        self.assertEqual(canonical, "dsh@demo")
        self.post("codex@demo", "after rename", canonical)
        seen, _ = self.inbox("dsh@demo")
        self.assertEqual([m["body"] for m in seen], ["after rename"])

    def test_rename_keeps_each_rows_own_checkout(self):
        self.post("codex@demo", "to repoA", "deepseek@repoA")
        self.post("codex@demo", "to repoB", "deepseek@repoB")
        self.store.rename_agent("deepseek", "dsh")
        a, _ = self.inbox("dsh@repoA")
        b, _ = self.inbox("dsh@repoB")
        self.assertEqual([m["body"] for m in a], ["to repoA"])
        self.assertEqual([m["body"] for m in b], ["to repoB"])

    def test_rename_to_a_qualified_identity_does_not_double_the_suffix(self):
        self.post("codex@demo", "hello", "deepseek@demo")
        self.store.rename_agent("deepseek", "dsh@demo")
        seen, _ = self.inbox("dsh@demo")
        self.assertEqual([m["body"] for m in seen], ["hello"])

    def test_canonical_agent_follows_a_chain(self):
        self.store.rename_agent("a", "b")
        self.store.rename_agent("b", "c")
        self.assertEqual(self.store.canonical_agent("a"), "c")
        self.assertEqual(self.store.canonical_agent("b"), "c")
        self.assertIsNone(self.store.canonical_agent("c"))

    def test_renaming_to_the_same_name_is_a_no_op(self):
        self.assertEqual(self.store.rename_agent("gpt", "gpt"), {})

    def test_rename_onto_live_state_raises(self):
        # Both identities need live state for the move to collide on a primary key.
        self.store.acknowledge_inbox(self.project, "mover@demo", 1)
        self.store.acknowledge_inbox(self.project, "taken@demo", 1)
        with self.assertRaises(ValueError):
            self.store.rename_agent("mover", "taken")

    def test_alias_of_an_alias_is_rejected(self):
        self.store.rename_agent("old", "new")
        with self.assertRaises(ValueError):
            self.store.rename_agent("old", "other")


class AddressBookEdgeTests(Base):
    def test_unknown_recipient_has_no_canonical_name(self):
        self.assertIsNone(self.store.canonical_agent("nobody"))
        self.assertIsNone(self.store.canonical_agent(""))

    def test_task_owner_follows_a_rename(self):
        tid = self.store.create_task(self.project, "deepseek", "t", "c", owner="deepseek")
        self.store.rename_agent("deepseek", "dsh")
        row = self.store.db.execute(
            "SELECT owner FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        self.assertEqual(row["owner"], "dsh")


if __name__ == "__main__":
    unittest.main()
