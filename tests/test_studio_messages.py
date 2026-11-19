"""Shared Studio conversations with a single project-local destination."""
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memdex.project import resolve
from memdex.store import Store
from memdex.studio_messages import recipients, send
from memdex.studio_rpc import Studio
from memdex.tools import Session


class StudioMessageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.folder = root / "demo"
        self.folder.mkdir()
        self.db = root / "sample.db"
        env = patch.dict(os.environ, {"MEMDEX_REASON_NOTIFY": "0", "MEMDEX_HOME": str(root)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMDEX_AGENT", None)
        self.project = resolve(self.folder)
        self.store = Store(str(self.db))
        self.addCleanup(self.store.close)
        self.first = self.chat("claude", "first")
        self.second = self.chat("claude", "second")
        self.named = self.chat("qwen-code")

    def chat(self, name, chat_id=None):
        session = Session(self.store, agent_name=name, cwd=str(self.folder), conversation_id=chat_id)
        self.store.touch_session(self.project, session.agent)
        session.chat_mailbox()
        return session

    def request(self, to, **kwargs):
        return dict({"to": to, "text": "한글 질문", "key": "request-1"}, **kwargs)

    def post(self, to, **kwargs):
        return send(self.db, self.project, self.request(to, **kwargs))["id"]

    def test_exact_inbox_and_shared_thread_read_by_another_studio(self):
        origin = Studio(self.db, 5)
        token = origin.select(self.project, True)["token"]
        first = origin.dispatch("write", dict(token=token, action="message", body=self.request(self.first.route)))["id"]
        def inbox(who):
            return [m["id"] for m in self.store.inbox(self.project, who)]
        self.assertEqual(inbox(self.second.route), [])
        self.assertEqual(inbox(self.first.agent), [])
        self.assertEqual(inbox(self.first.route), [first])
        self.assertEqual(inbox(self.first.route), [])
        self.first.post("답변", reply_to=first, request_key="answer")
        observer = Studio(self.db, 5)
        token = observer.select(self.project, True)["token"]
        thread = observer.dispatch("read", dict(token=token, endpoint=f"thread/{first}"))["messages"]
        self.assertEqual([m["body"] for m in thread], ["한글 질문", "답변"])
        self.assertEqual(thread[1]["reply"], first)
        self.assertEqual(thread[1]["to"], thread[0]["from"])
        alternate = replace(self.project, root=str(self.folder.parent / "worktree"))
        follow = send(self.db, alternate, self.request(self.first.route, key="follow", reply=thread[1]["id"]))
        rows = self.store.inbox(self.project, self.first.route)
        self.assertEqual([m["id"] for m in rows], [follow["id"]])
        self.assertFalse(any(r["agent"] == "studio-user" for r in recipients(self.db, self.project)))

    def test_named_inbox_excludes_subaddress_and_other_named_agents(self):
        sibling = self.chat("qwen-code/other")
        first = self.post(self.named.agent)
        self.assertEqual(self.store.inbox(self.project, sibling.agent), [])
        with self.assertRaises(ValueError):
            sibling.post("another sender", reply_to=first)
        self.assertEqual([m["id"] for m in self.store.inbox(self.project, self.named.agent)], [first])
        self.named.post("named reply", reply_to=first)
        answer = self.store.db.execute("SELECT * FROM messages WHERE reply_to=?", (first,)).fetchone()
        self.assertEqual(answer["body"], "named reply")
        self.post(self.named.agent, key="next", reply=answer["id"])
        with self.assertRaises(ValueError):
            self.post(self.second.route, key="wrong-parent", reply=first)

    def test_parallel_duplicate_and_conflicting_reuse(self):
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(lambda _: self.post(self.first.route), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "different"):
            self.post(self.second.route)

    def test_chat_list_keeps_root_across_pages_and_separates_identical_titles(self):
        first = self.post(self.named.agent, text="같은 제목")
        self.named.post("첫 답장", reply_to=first)
        answer = self.store.db.execute("SELECT id FROM messages WHERE reply_to=?", (first,)).fetchone()[0]
        second = self.post(self.named.agent, text="같은 제목", key="separate")
        latest = self.post(self.named.agent, text="후속 질문", reply=answer, key="follow")
        studio = Studio(self.db, 1)
        token = studio.select(self.project, True)["token"]
        def page(endpoint):
            return studio.dispatch("read", dict(token=token, endpoint=endpoint))
        last = page("state")
        self.assertEqual(last["projectKey"], self.project.key)
        self.assertEqual([(m["id"],m["chatRoot"],m["subject"]) for m in last["messages"]],
                         [(latest,first,"같은 제목")])
        self.assertEqual(page(f"history/{latest}")["messages"][0]["chatRoot"], second)
        self.assertEqual({m["chatRoot"] for m in page(f"thread/{answer}")["messages"]}, {first})

    def test_foreign_parent_does_not_supply_a_chat_title(self):
        other = resolve(self.folder.parent / "foreign")
        private = self.store.post(other, "agent", "다른 프로젝트")
        local = self.store.post(self.project, "agent", "현재 프로젝트", reply_to=private)
        studio = Studio(self.db, 10)
        token = studio.select(self.project, True)["token"]
        row = studio.dispatch("read", dict(token=token, endpoint="state"))["messages"][0]
        self.assertEqual((row["chatRoot"],row["subject"]), (local,"현재 프로젝트"))

    def test_closed_and_changed_task_routes_and_retry_receipt(self):
        task = self.store.create_task(self.project, "qa", "할 일", "확인")
        self.first.session_routes(label="화면", task_id=task)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.post(self.first.route)
        mid = self.post(self.first.route, task=task)
        self.first.post("확인", reply_to=mid)
        self.assertIn("task #"+str(task), self.store.inbox(self.project,self.first.route)[0]["body"])
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET status='verified' WHERE id=?", (task,))
        self.assertEqual(self.post(self.first.route, task=task), mid)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.post(self.first.route, task=task, key="new")
        addresses = {r["id"] for r in recipients(self.db, self.project)}
        self.assertNotIn(self.first.route, addresses)
        self.assertNotIn(self.first.agent, addresses)
        self.assertIn(self.second.route, addresses)

    def test_invalid_payload_cross_project_and_read_only(self):
        other = self.folder.parent / "other"
        other.mkdir()
        project = resolve(other)
        self.assertEqual(recipients(self.db, project), [])
        with self.assertRaises(ValueError):
            send(self.db, project, self.request(self.first.route))
        for update in [dict(text="   "),dict(text="x"*12001),dict(task=True),dict(reply=-1),dict(key=None),dict(to="")]:
            with self.subTest(update=list(update)), self.assertRaises(ValueError):
                send(self.db, self.project, dict(self.request(self.first.route), **update))
        view = Studio(self.db, 5)
        old = view.select(self.project, True)["token"]
        current = view.select(project, False)["token"]
        for token in (old, current):
            with self.assertRaises(ValueError):
                view.dispatch("write", dict(token=token, action="message", body=self.request(self.first.route)))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
