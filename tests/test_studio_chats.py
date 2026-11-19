from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from memdex.studio_chats import LocalChats, PAGE_TURNS
from memdex.studio_rpc import Studio


def line(row):
    return json.dumps(row, ensure_ascii=False) + "\n"


def codex_turn(role, text):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


def claude_turn(uid, folder, role, blocks, **extra):
    return {"sessionId": uid, "cwd": str(folder), "type": role,
            "message": {"role": role, "content": blocks}, **extra}


class StudioChatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a = self.root / "one" / "앱"
        self.b = self.root / "two" / "앱"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.chats = LocalChats(self.codex, self.claude)
        self.key = os.path.normcase(str(self.a))

    def codex_file(self, folder, records, archived=False):
        uid = str(uuid4())
        dest = self.codex / ("archived_sessions" if archived else "sessions/2026/11/18")
        dest.mkdir(exist_ok=True, parents=True)
        path = dest / ("rollout-date-" + uid + ".jsonl")
        path.write_text(line({"type": "session_meta", "payload": {"id": uid, "cwd": str(folder)}}) +
                        "".join(map(line, records)), encoding="utf-8")
        return "codex:" + uid, path

    def test_both_sources_in_project_only_and_visible_turns(self):
        cid, _ = self.codex_file(self.a, [codex_turn("developer", "hidden"), codex_turn("user", "# AGENTS.md instructions"),
            codex_turn("user", "한글 질문"), {"type": "event_msg", "payload": {"type": "agent_message", "message": "duplicate"}},
            codex_turn("assistant", "<b>plain text</b>")], archived=True)
        other, _ = self.codex_file(self.b, [codex_turn("user", "다른 프로젝트")])
        uid = str(uuid4())
        path = self.claude / "projects/arbitrary-encoded-name" / (uid + ".jsonl")
        path.parent.mkdir(parents=True)
        rows = [claude_turn(uid, self.a, "user", "원문"), claude_turn(uid, self.a, "user", "internal", isMeta=True),
                claude_turn(uid, self.a, "assistant", [{"type": "thinking", "thinking": "hidden"},
                    {"type": "tool_use", "name": "hidden"}, {"type": "text", "text": "답장"}]),
                claude_turn(str(uuid4()), self.b, "user", "foreign"), claude_turn(uid, self.a, "user", "sub", isSidechain=True)]
        path.write_text("malformed\n" + "".join(map(line, rows)), encoding="utf-8")
        rows = self.chats.listing(self.key)
        self.assertEqual({r["title"] for r in rows}, {"원문", "한글 질문"})
        self.assertEqual([m["text"] for m in self.chats.page(self.key, cid)["messages"]], ["한글 질문", "<b>plain text</b>"])
        self.assertEqual([m["text"] for m in self.chats.page(self.key, "claude:"+uid)["messages"]], ["원문", "답장"])
        with self.assertRaises(ValueError):
            self.chats.page(self.key, other)

    def test_native_display_names_and_provider_collision(self):
        cid, _ = self.codex_file(self.a, [codex_turn("user", "prompt")])
        uid = cid.split(":")[1]
        (self.codex/"session_index.jsonl").write_text(line({"id": uid, "thread_name":"Renamed Codex"}), encoding="utf8")
        path = self.claude/"projects/group"/(uid+".jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(line(claude_turn(uid, self.a,"user","first")) +
                        line({"type":"custom-title","sessionId":uid,"customTitle":"Renamed Claude"}), encoding="utf8")
        listing = {r["id"]:r["title"] for r in self.chats.listing(self.key)}
        self.assertEqual(listing, {cid:"Renamed Codex", "claude:"+uid:"Renamed Claude"})

    def test_refresh_page_boundaries_and_partial_write(self):
        cid, path = self.codex_file(self.a, [codex_turn("user", str(i)) for i in range(PAGE_TURNS+3)])
        self.chats.listing(self.key)
        with self.assertRaises(ValueError):
            self.chats.page(self.key, cid, 17)
        first = self.chats.page(self.key, cid)
        partial = line(codex_turn("assistant","last"))
        with path.open("a", encoding="utf-8") as f:
            f.write(partial[:15])
        tail = self.chats.page(self.key, cid, first["next"])
        self.assertEqual([m["text"] for m in first["messages"]+tail["messages"]], list(map(str, range(PAGE_TURNS+3))))
        with path.open("a", encoding="utf-8") as f:
            f.write(partial[15:])
        self.chats.refresh(force=True)
        first = self.chats.page(self.key, cid)
        tail = self.chats.page(self.key, cid, first["next"])
        self.assertEqual(tail["messages"][-1]["text"], "last")
        self.assertIsNone(tail["next"])

    def test_conflicting_header_and_missing_file(self):
        cid, path = self.codex_file(self.a, [codex_turn("user","correct"),
            {"type":"session_meta","payload":{"id":str(uuid4()),"cwd":str(self.b)}},codex_turn("user","other")])
        self.chats.listing(self.key)
        page = self.chats.page(self.key,cid)
        self.assertEqual([m["text"] for m in page["messages"]], ["correct"])
        self.assertIsNone(page["next"])
        path.unlink()
        with self.assertRaises(ValueError):
            self.chats.page(self.key, cid)
        self.chats.refresh(force=True)
        self.assertEqual(self.chats.listing(self.key), [])

    def test_registered_native_identity_and_ambiguous_binding(self):
        dbfile = self.root / "memdex.db"
        cid, _ = self.codex_file(self.a, [codex_turn("user","prompt")])
        self.chats.memdex_db = dbfile
        other_key = os.path.normcase(str(self.b))
        with closing(sqlite3.connect(dbfile)) as db, db:
            db.execute("create table chat_routes(agent,conversation_id,project)")
            db.execute("insert into chat_routes values(?,?,?)", ("codex", cid[6:], other_key))
        self.assertEqual([r["id"] for r in self.chats.listing(other_key)], [cid])
        self.assertEqual(self.chats.listing(self.key), [])
        with closing(sqlite3.connect(dbfile)) as db, db:
            db.execute("insert into chat_routes values(?,?,?)", ("codex", cid[6:], self.key))
        self.chats.refresh(force=True)
        self.assertEqual([r["id"] for r in self.chats.listing(self.key)], [cid])
        self.assertEqual(self.chats.listing(other_key), [])

    def test_native_only_project_and_token_on_standard_rpc(self):
        cid, _ = self.codex_file(self.a, [codex_turn("user","hello")])
        with patch.dict(os.environ, {"CODEX_HOME":str(self.codex), "CLAUDE_CONFIG_DIR":str(self.claude),
                                     "MEMDEX_HOME":str(self.root/"settings")}):
            studio = Studio(self.root/"fresh.db", 25)
            current = studio.dispatch("initial", {})
            self.assertEqual(current["key"], self.key)
            self.assertEqual(studio.groups()[0]["codex"],1)
            endpoint = "ide/chat/"+cid+"/0"
            page = studio.dispatch("read", {"endpoint":endpoint, "token":current["token"]})
            self.assertEqual(page["messages"][0]["text"], "hello")
            second = studio.dispatch("folder", {"folder":str(self.b)})
            with self.assertRaisesRegex(ValueError,"closed"):
                studio.dispatch("read", {"endpoint":endpoint, "token":current["token"]})
            with self.assertRaises(ValueError):
                studio.dispatch("read", {"endpoint":endpoint,"token":second["token"]})
