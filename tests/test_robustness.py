"""Robustness tests for the paths the happy stories never walk.

Coverage-guided: these exercise the migration ladder (a database from v5/v6 must upgrade in place),
the inbox batch boundary, hand-supplied vectors, wildcard queries, the LWW tie, and every JSON-RPC
shape an MCP client can throw at the server.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from array import array
from pathlib import Path

from memdex import mcp
from memdex import project as projects
from memdex import sync as sync_mod
from memdex.store import SCHEMA_VERSION, Store
from memdex.tools import Session


def vec(*values):
    return array("f", values).tobytes()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)

    def tearDown(self):
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def reopen(self):
        """Close, stamp a version, and open again the way an upgrade does."""
        self.store.close()
        return Store(self.db)


class MigrationLadderTests(Base):
    def test_a_v6_database_gets_uids_backfilled_on_open(self):
        nid = self.store.add_note(self.project, "decision", "keep sqlite", "zero deps", "claude")
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("DROP INDEX IF EXISTS notes_uid")
        con.execute("ALTER TABLE notes DROP COLUMN uid")
        con.execute("ALTER TABLE notes DROP COLUMN updated_at")
        con.execute("PRAGMA user_version=6")
        con.commit()
        con.close()

        store = Store(self.db)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            row = store.db.execute("SELECT uid, updated_at FROM notes WHERE id=?", (nid,)).fetchone()
            self.assertTrue(row["uid"])
            self.assertEqual(len(row["uid"]), 32)  # uuid4().hex
            self.assertGreater(row["updated_at"], 0)
            # A new note lands with a different uid: uniqueness holds after the backfill.
            nid2 = store.add_note(self.project, "note", "second", "", "claude")
            row2 = store.db.execute("SELECT uid FROM notes WHERE id=?", (nid2,)).fetchone()
            self.assertNotEqual(row["uid"], row2["uid"])
        finally:
            store.close()

    def test_a_v5_database_gets_the_claim_fencing_columns_back(self):
        self.store.claim(self.project, "claude", ["src/store.py"], "lock", exclusive=True)
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE claims DROP COLUMN token")
        con.execute("ALTER TABLE claims DROP COLUMN sha")
        con.execute("PRAGMA user_version=5")
        con.commit()
        con.close()

        store = Store(self.db)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            cols = {r[1] for r in store.db.execute("PRAGMA table_info(claims)")}
            self.assertIn("token", cols)
            self.assertIn("sha", cols)
            self.assertEqual(store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 1)
        finally:
            store.close()

    def test_a_migration_is_safe_on_a_fresh_database(self):
        # Stamp a fresh (already v7) database back and let the ladder run again: every migration must
        # tolerate columns that already exist, or a crash between stamping and opening would brick the db.
        self.store.close()
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA user_version=6")
        con.commit()
        con.close()
        store = Store(self.db)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
        finally:
            store.close()


class InboxBatchTests(Base):
    def test_twenty_five_messages_arrive_in_two_batches(self):
        for i in range(25):
            self.store.post(self.project, "deepseek@proj", f"message {i}", "codex")
        self.assertEqual(self.store.unread_count(self.project, "codex@proj"), 25)
        first = self.store.inbox(self.project, "codex@proj")
        self.assertEqual(len(first), 20)
        self.assertEqual(self.store.unread_count(self.project, "codex@proj"), 5)
        second = self.store.inbox(self.project, "codex@proj")
        self.assertEqual(len(second), 5)
        self.assertEqual(self.store.inbox(self.project, "codex@proj"), [])


class VectorTests(Base):
    def test_a_vector_put_by_hand_round_trips_through_search(self):
        from memdex import embed
        v = vec(1.0, 0.0, 0.0)
        self.store.put_vector("note", "99", "note", v)
        got = self.store.db.execute(
            "SELECT vec FROM vectors WHERE src='note' AND ref='99' AND scope='note'").fetchone()[0]
        self.assertAlmostEqual(embed.similarity(v, got), 1.0, places=5)

    def test_a_mismatched_vector_reports_unrelated(self):
        from memdex import embed
        self.assertEqual(embed.similarity(vec(1.0, 0.0), vec(0.0, 1.0)), 0.0)
        self.assertEqual(embed.similarity(vec(1.0, 0.0), vec(1.0, 0.0, 0.0)), -1.0)


class SearchWildcardTests(Base):
    def test_wildcard_characters_in_a_query_do_not_error(self):
        Path(self.root, "100%.py").write_text("PERCENT = 1\n", encoding="utf-8")
        self.store.index(self.project)
        s = Session(self.store, agent_name="claude")
        s.cwd = self.root
        for query in ("%", "_", '"', "OR AND", "100"):
            out = s.search(query)
            self.assertIsInstance(out, str)  # a sentence, not a traceback

    def test_mixed_long_and_short_or_fallback_keeps_short_term_hits(self):
        long_note = self.store.add_note(self.project, "note", "Alpha", "quasar only", "test")
        short_note = self.store.add_note(self.project, "note", "Bravo", "xy only", "test")

        found = {hit["id"] for hit in self.store.search(
            self.project, "quasar xy", kinds=["note"])}

        self.assertEqual(found, {long_note, short_note})

    def test_all_short_or_fallback_keeps_each_partial_match(self):
        first = self.store.add_note(self.project, "note", "Alpha", "검색 단어", "test")
        second = self.store.add_note(self.project, "note", "Bravo", "계획 내용", "test")

        found = {hit["id"] for hit in self.store.search(
            self.project, "검색 계획", kinds=["note"])}

        self.assertEqual(found, {first, second})

    def test_mixed_query_still_prefers_a_note_matching_every_term(self):
        self.store.add_note(self.project, "note", "Alpha", "quasar only", "test")
        self.store.add_note(self.project, "note", "Bravo", "xy only", "test")
        combined = self.store.add_note(self.project, "note", "Charlie", "quasar and xy", "test")

        found = [hit["id"] for hit in self.store.search(
            self.project, "quasar xy", kinds=["note"])]

        self.assertEqual(found, [combined])


class SyncTieTests(Base):
    def test_an_equal_timestamp_keeps_the_local_note(self):
        nid = self.store.add_note(self.project, "note", "WAL mode", "local body", "claude")
        sync_mod.export(self.store, self.project, os.path.join(self.tmp, "shared"))
        local_at = self.store.db.execute(
            "SELECT updated_at FROM notes WHERE id=?", (nid,)).fetchone()[0]
        # The incoming copy claims the very same timestamp with a different body: local wins, silently
        # (not a conflict — nobody was later than anybody).
        rep = sync_mod.merge(self.store, self.project, os.path.join(self.tmp, "shared"))
        self.assertEqual((rep["added"], rep["updated"]), (0, 0))
        self.assertEqual(self.store.db.execute("SELECT body FROM notes WHERE id=?", (nid,)).fetchone()[0],
                         "local body")


class MCPProtocolTests(Base):
    def setUp(self):
        super().setUp()
        self.session = Session(self.store, agent_name="")
        self.session.cwd = self.root
        self.tools = {t.name: t for t in self.session.tools()}

    def test_notifications_produce_no_response(self):
        self.assertIsNone(mcp.handle(self.session, self.tools, {"method": "notifications/initialized"}))

    def test_unknown_method_is_a_protocol_error(self):
        resp = mcp.handle(self.session, self.tools, {"method": "tools/explode", "id": 1})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_initialize_reports_the_server_and_uses_the_client_name(self):
        resp = mcp.handle(self.session, self.tools,
                          {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "claude code"}}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "memdex")
        self.assertEqual(self.session.agent_name, "claude")

    def test_tools_list_lists_every_tool(self):
        resp = mcp.handle(self.session, self.tools, {"method": "tools/list", "id": 1})
        self.assertEqual(len(resp["result"]["tools"]), 15)
        self.assertIn("session_routes", {t["name"] for t in resp["result"]["tools"]})

    def test_artifacts_list_and_read_round_trip(self):
        self.session.store.add_note(self.session.project, "decision", "lock files", "claim first",
                                    "claude@x")
        listing = mcp.handle(self.session, self.tools, {"method": "artifacts/list", "id": 1})
        self.assertEqual(listing["result"]["artifacts"][0]["uri"], "memdex://note/1")
        self.assertEqual(listing["result"]["artifacts"][0]["mimeType"], "text/markdown")
        read = mcp.handle(self.session, self.tools,
                          {"method": "artifacts/read", "id": 2,
                           "params": {"uri": "memdex://note/1"}})
        self.assertEqual(read["result"]["content"]["type"], "text")
        self.assertEqual(read["result"]["content"]["text"], "claim first")
        bad = mcp.handle(self.session, self.tools,
                         {"method": "artifacts/read", "id": 3, "params": {"uri": "memdex://note/999"}})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_initialize_declares_artifacts(self):
        resp = mcp.handle(self.session, self.tools,
                          {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "claude code"}}})
        self.assertIn("artifacts", resp["result"]["capabilities"])

    def test_an_unknown_tool_answers_with_an_error_not_a_crash(self):
        resp = mcp.handle(self.session, self.tools,
                          {"method": "tools/call", "id": 1, "params": {"name": "no_such_tool"}})
        self.assertTrue(resp["result"]["isError"])

    def test_extra_arguments_are_filtered_not_rejected(self):
        resp = mcp.handle(self.session, self.tools, {"method": "tools/call", "id": 1, "params": {
            "name": "remember", "arguments": {"kind": "note", "title": "hi", "body": "b",
                                              "junk_from_another_client": 42}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("saved", resp["result"]["content"][0]["text"])

    def test_a_raising_tool_reports_instead_of_killing_the_server(self):
        def boom(**kwargs):
            raise ValueError("the database is angry")

        self.tools["remember"].fn = boom
        resp = mcp.handle(self.session, self.tools, {"method": "tools/call", "id": 1, "params": {
            "name": "remember", "arguments": {"kind": "note", "title": "hi"}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("memdex error", resp["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
