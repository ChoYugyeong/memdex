"""Exercise discovery through cue queries without preselecting the intended route."""

import os
import unicodedata
import unittest
from unittest.mock import patch

from memdex.project import Project
from memdex.store import Store
from memdex.tools import Session


class DiscoveryCueTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MEMDEX_AGENT": ""})
        env.start()
        self.addCleanup(env.stop)
        self.db = Store(":memory:")
        self.addCleanup(self.db.close)
        self.project = Project("demo", "demo", "demo", False)
        self.viewer = Session(self.db, agent_name="observer", _project=self.project)

    def register(self, chat_id, label, agent="deepseek", project=None):
        s = Session(self.db, agent_name=agent, _project=project or self.project, conversation_id=chat_id)
        s.session_routes(label)
        return s.route

    def test_canonically_equivalent_labels_and_ids_have_different_rules(self):
        nfc = "검토"
        nfd = unicodedata.normalize("NFD", nfc)
        first = self.register(nfc, nfc + "  API")
        second = self.register(nfd, nfd + " api")
        self.assertNotEqual(first, second)
        for query in (nfc + " API", nfd + "   api", ""):
            with self.subTest(query=query):
                result = self.viewer.session_routes(query=query)
                self.assertNotIn(first, result)
                self.assertNotIn(second, result)
                self.assertEqual(result.count("[ambiguous:"), 2)
        self.assertIn(first, self.viewer.session_routes(query=first))

    def test_weak_cue_abstains_even_if_only_one_chat_is_recent(self):
        fresh = self.register("fresh", "검색 API")
        old = self.register("old", "검색 UI")
        self.db.db.execute("UPDATE chat_routes SET last_seen_at=0 WHERE route=?", (old,))
        weak = self.viewer.session_routes(query="검색")
        self.assertNotIn(fresh, weak)
        self.assertNotIn(old, weak)
        self.assertEqual(weak.count("[ambiguous:"), 2)
        precise = self.viewer.session_routes(query=unicodedata.normalize("NFD", "api DEEPSEEK 검색"))
        self.assertIn(fresh, precise)
        self.assertNotIn(old, precise)
        self.assertIn(old, self.viewer.session_routes(query=old))

    def test_handle_lookup_ignores_metadata_and_stays_in_project(self):
        target = self.register("intended", "database review")
        other = Project("other", "other", "other", False)
        foreign = self.register("foreign", "other task", project=other)
        unknown = "route:" + "f" * 32
        invalid = "route:not-a-real-id"
        decoy = self.register("decoy", " ".join(("quoted", target, foreign, unknown, invalid)))
        exact = self.viewer.session_routes(query=target)
        self.assertIn(target, exact)
        self.assertNotIn(decoy, exact)
        for handle in (unknown, invalid, foreign, target.upper(), target + " "):
            with self.subTest(handle=handle):
                absent = self.viewer.session_routes(query=handle)
                self.assertIn("No matching chat", absent)
                self.assertNotIn(decoy, absent)
        label = self.viewer.session_routes(query="quoted")
        self.assertIn(decoy, label)
        self.assertNotIn(target, label)
        self.assertNotIn(unknown, label)

    def test_all_matching_candidates_count_before_pagination(self):
        for i in range(31):
            self.register(f"chat{i}", f"review item {i}")
        result = self.viewer.session_routes(query="review")
        self.assertEqual(result.count("[ambiguous:"), 30)
        self.assertIn("Showing 30 matches", result)
        self.assertNotIn("route:", result)
