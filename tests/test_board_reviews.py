"""End-to-end review studio requests use normal jobs, task history and provider transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memdex import board_reviews, board_web, project, review, skills
from memdex.store import Store


class StudioTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.project = project.resolve(self.root)
        self.db = str(self.base / "state.db")
        self.file = self.root / "inbox.py"
        self.file.write_text("preserve_unread = True\n", encoding="utf-8")
        self.handler = board_web.handler_for(self.db, self.project, 10, "/private/")
        launch = patch.object(review, "launch")
        self.launch = launch.start()
        self.addCleanup(launch.stop)
        self.task = self.post("task", {"title":"Inbox QA", "criterion":"Peek preserves unread"})["id"]
        self.spec = {"task":self.task, "files":["inbox.py"], "question":"Check peek behavior",
                     "token":"fixture-request-0001"}

    def request(self, endpoint, body=None, origin="http://127.0.0.1:9876", prefix="/private/"):
        method = "POST" if body is not None else "GET"
        raw = json.dumps(body).encode() if body is not None else b""
        head = (f"{method} {prefix}api/qa/{endpoint} HTTP/1.0\r\nHost: 127.0.0.1:9876\r\n"
                f"Origin: {origin}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n")
        sent = bytearray()
        conn = SimpleNamespace(makefile=lambda *_: io.BytesIO(head.encode()+raw), sendall=sent.extend)
        self.handler(conn, ("127.0.0.1", 3210), SimpleNamespace(server_port=9876))
        headers, result = bytes(sent).split(b"\r\n\r\n", 1)
        return int(headers.split()[1]), result.decode()

    def post(self, endpoint, body=None):
        code, result = self.request(endpoint, body)
        self.assertEqual(code, 200, result)
        return json.loads(result)

    def run_jobs(self, rid):
        with patch.object(review, "run_claude", return_value=("completed","Claude answer")), \
             patch.object(review.providers, "run_ollama", return_value=("failed","Ollama unavailable")), \
             patch.object(review.providers, "run_deepseek", return_value=("completed","DeepSeek answer")):
            for job in self.post(f"run/{rid}")["attempts"]:
                review.worker(self.db, job["id"])
        return {j["provider"]:j for j in self.post(f"run/{rid}")["attempts"]}

    def test_one_snapshot_three_answers_and_independent_retry_evidence(self):
        rid = self.post("start",self.spec)["id"]
        self.assertEqual(self.post("start", self.spec)["id"], rid)
        self.assertEqual(self.launch.call_count, 3)
        with closing(Store(self.db)) as store:
            payloads = [j[0] for j in store.db.execute("SELECT payload FROM review_jobs")]
            self.assertEqual(len(set(payloads)), 1)
            self.assertEqual(json.loads(payloads[0])["task"]["criterion"], "Peek preserves unread")
        jobs = self.run_jobs(rid)
        retry = {"run":rid,"job":jobs["qwen"]["id"]}
        self.assertEqual(self.request("retry",dict(retry,job=jobs["claude"]["id"]))[0],400)
        self.file.write_text("changed",encoding="utf-8")
        self.assertEqual(self.request("retry",retry)[0],400)
        self.file.write_text("preserve_unread = True\n",encoding="utf-8")
        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(lambda _:self.post("retry",retry), range(2)))
        self.assertEqual(receipts, [{"id":rid}]*2)
        self.assertEqual(self.launch.call_count,4)
        attempts = self.post(f"run/{rid}")["attempts"]
        self.assertEqual([j for j in attempts if j["provider"] != "qwen"],
                         [jobs["claude"],jobs["deepseek"]])
        with patch.object(review.providers,"run_ollama",return_value=("completed","Qwen recovered")):
            review.worker(self.db,attempts[-1]["id"])
        note = {"run":rid,"job":jobs["deepseek"]["id"],"note":"Added test_peek, passed."}
        self.post("evidence",note)
        self.post("evidence",note)
        with closing(Store(self.db)) as store:
            events = [r for r in store.task_events(self.project,self.task) if r["action"] == "review_evidence"]
            self.assertEqual(len(events),1)
            self.assertIn(f"reply #{jobs['deepseek']['reply_id']}",events[0]["detail"])
            self.assertEqual(store.task_rows(self.project)[0]["status"],"todo")
            self.assertEqual(store.db.execute("SELECT count(*) FROM cursors").fetchone()[0],0)
        self.assertEqual(self.post(f"history/{self.task}")[0]["id"],rid)
        self.assertEqual(board_reviews.history(self.db,self.project,full=True)[0]["attempts"][-1]["result"],
                         "Qwen recovered")

    def test_approved_skill_uses_full_checked_body_across_providers(self):
        path = self.root / "skills" / "peek" / "SKILL.md"
        path.parent.mkdir(parents=True)
        text = "---\nname: peek\ndescription: Inspect inbox behavior\n---\n" + ("검토\n"*800) + "final rubric"
        path.write_bytes(text.encode("utf-8"))
        with closing(Store(self.db)) as store:
            skills.scan(store,"studio",str(self.root),home=str(self.base/"fake"))
            skill = next(s for s in self.post("options")["skills"] if s["name"] == "peek")
            spec = dict(self.spec,skill=skill["id"],skill_sha=skill["sha"])
            self.assertEqual(self.request("start",spec)[0],400)
            self.assertTrue(skills.approve(store,skill["name"],skill["sha"])["ok"])
            rid = self.post("start",spec)["id"]
            jobs = [dict(r) for r in store.db.execute("SELECT * FROM review_jobs")]
            self.assertEqual(len(jobs),3)
            for j in jobs:
                rubric = json.loads(j["payload"])["review_skill"]
                self.assertEqual(rubric["instructions"],text)
                self.assertEqual(rubric["sha"],skill["sha"])
                review.finish(store,j["id"],"failed","fixture")
            self.assertNotIn("instructions", json.loads(store.db.execute(
                "SELECT payload FROM review_jobs LIMIT 1").fetchone()[0])["review_skill"])
            path.write_text(text+" changed",encoding="utf-8")
            self.assertEqual(self.request("retry",{"run":rid,"job":jobs[0]["id"]})[0],400)
        self.assertEqual(self.launch.call_count,3)

    def test_concurrent_start_and_stale_worker_recovery(self):
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(lambda _:self.post("start",self.spec)["id"],range(2)))
        self.assertEqual(ids[0],ids[1])
        rid = ids[0]
        job = self.post(f"run/{rid}")["attempts"][0]["id"]
        with closing(Store(self.db)) as store, store.db:
            store.db.execute("UPDATE review_jobs SET started_at=0 WHERE id=?",(job,))
        self.assertEqual(self.post(f"run/{rid}")["attempts"][0]["state"],"interrupted")
        self.post("retry",{"run":rid,"job":job})
        with closing(Store(self.db)) as store:
            review.finish(store,job,"completed","late")
            self.assertEqual(store.db.execute("SELECT state FROM review_jobs WHERE id=?",(job,)).fetchone()[0],
                             "interrupted")
        self.assertEqual(self.launch.call_count,4)

    def test_inputs_origins_and_other_projects_are_scoped(self):
        self.assertEqual(self.request("start",self.spec,origin="http://example.invalid")[0],403)
        self.assertEqual(self.request("start",self.spec,prefix="/wrong/")[0],404)
        self.assertEqual(self.request("start",dict(self.spec,files=["../state.db"]))[0],400)
        self.assertEqual(self.request("start",dict(self.spec,task=self.task+100))[0],400)
        self.launch.assert_not_called()
        rid = self.post("start",self.spec)["id"]
        other = project.resolve(self.base)
        self.handler = board_web.handler_for(self.db,other,10,"/private/")
        self.assertEqual(self.post(f"history/{self.task}"),[])
        self.assertEqual(self.request(f"run/{rid}")[0],404)
        self.assertEqual(self.request("retry",{"run":rid,"job":1})[0],400)
        self.assertEqual(self.request("evidence",{"run":rid,"job":1,"note":"foreign"})[0],400)
