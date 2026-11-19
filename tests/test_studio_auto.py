"""On-demand model delivery uses a durable message and a single claimed attempt."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memdex import studio_auto as auto, studio_models
from memdex.project import resolve
from memdex.store import Store
from memdex.studio_messages import send
from memdex.studio_rpc import Studio


class AutoReplyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = resolve(self.root)
        self.path = self.root / "store.db"
        env = patch.dict(os.environ, {"MEMDEX_HOME": temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(str(self.path))
        self.addCleanup(self.store.close)
        launch = patch.object(auto.review, "launch")
        self.launch = launch.start()
        self.addCleanup(launch.stop)
        self.view = Studio(self.path, 20)
        self.token = self.view.select(self.project, True)["token"]

    def read(self, endpoint):
        return self.view.dispatch("read", dict(token=self.token, endpoint=endpoint))

    def send(self, provider="claude", text="question", key="once", **kwargs):
        return send(self.path, self.project, dict(to="model:"+provider, key=key, text=text, **kwargs))["id"]

    def test_empty_project_directory_and_concurrent_submission(self):
        rows = self.read("message/recipients")
        self.assertEqual({r["id"] for r in rows if r["direct"]}, set(auto.MODELS))
        self.launch.assert_not_called()
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(lambda _: self.send(), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.launch.assert_called_once()
        self.assertEqual(self.read("state")["messages"][0]["auto"]["state"], "queued")
        with self.assertRaises(ValueError):
            self.send(text="changed")
        with patch.object(auto, "run_model", return_value=("completed", "reply")) as model:
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda _: auto.worker(self.path, ids[0], 1), range(2)))
        model.assert_called_once_with("model:claude","question")
        thread = self.read(f"thread/{ids[0]}")["messages"]
        self.assertEqual(len(thread),2)
        self.assertEqual((thread[0]["auto"]["state"],thread[1]["reply"],thread[1]["from"]),
                         ("completed",ids[0],"model:claude"))

    def test_reservation_failure_rolls_back_the_message(self):
        with patch.object(auto, "reserve", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.send()
        self.launch.assert_called_once()

    def test_followup_only_sends_the_selected_branch(self):
        root = self.send()
        self.send("qwen", "other project topic", "separate")
        with patch.object(auto, "run_model", return_value=("completed", "only this reply")):
            auto.worker(self.path,root,1)
        reply = self.read(f"thread/{root}")["messages"][1]["id"]
        sibling = self.send(text="sibling",key="sibling",reply=root)
        mid = self.send(text="follow up",key="follow",reply=reply)
        with patch.object(auto, "run_model", return_value=("completed", "last")) as model:
            auto.worker(self.path,mid,1)
        self.assertEqual(json.loads(model.call_args.args[1]),
                         [{"role":"user","content":"question"},{"role":"assistant","content":"only this reply"},
                          {"role":"user","content":"follow up"}])
        with self.assertRaises(ValueError):
            self.send("deepseek","cross-link","invalid",reply=sibling)

    def test_retry_is_scoped_and_two_windows_only_reserve_once(self):
        with patch.object(auto.review, "launch", side_effect=OSError("launch failed")):
            mid = self.send()
        ok = self.send("qwen",key="other")
        with patch.object(auto, "run_model", return_value=("completed","ok")):
            auto.worker(self.path,ok,1)
        self.assertEqual(self.read(f"thread/{mid}")["messages"][0]["auto"]["state"],"failed")
        retry = dict(id=mid,attempt=1)
        previous = self.read(f"thread/{ok}")["messages"]
        with self.assertRaises(ValueError):
            auto.retry(self.path,resolve(self.root/"different"),retry)
        with self.assertRaises(ValueError):
            auto.retry(self.path,self.project,dict(id=ok,attempt=1))
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:auto.retry(self.path,self.project,retry),range(2)))
        self.assertEqual(results,[dict(id=mid,attempt=2)]*2)
        self.assertEqual(self.launch.call_count,2)  # Qwen and the one Claude retry
        with patch.object(auto, "run_model", return_value=("completed","success")):
            auto.worker(self.path,mid,2)
        self.assertEqual(self.read(f"thread/{ok}")["messages"], previous)

    def test_interrupted_attempt_cannot_publish_a_late_answer(self):
        mid=self.send()
        def expire_and_retry(*args):
            with self.store.db:
                self.store.db.execute("UPDATE studio_model_attempts SET started=0 WHERE request=?",(mid,))
            self.assertEqual(self.read(f"thread/{mid}")["messages"][0]["auto"]["state"],"interrupted")
            auto.retry(self.path,self.project,dict(id=mid,attempt=1))
            return "completed","late reply"
        with patch.object(auto,"run_model",side_effect=expire_and_retry):
            auto.worker(self.path,mid,1)
        self.assertEqual(len(self.read(f"thread/{mid}")["messages"]),1)
        with patch.object(auto,"run_model",return_value=("completed","final reply")):
            auto.worker(self.path,mid,2)
        self.assertEqual(self.read(f"thread/{mid}")["messages"][1]["body"],"final reply")

    def test_group_membership_and_per_model_retry(self):
        members = ["model:claude", "model:deepseek"]
        root = self.send(models=members)
        self.assertEqual(self.send(models=list(reversed(members))), root)
        self.assertEqual(self.launch.call_count, 2)
        with patch.object(auto, "run_model", return_value=("completed", "first answer")):
            auto.worker(self.path, root, 1, members[0])
        with patch.object(auto, "run_model", return_value=("failed", "unavailable")):
            auto.worker(self.path, root, 1, members[1])
        retry = dict(id=root, attempt=1, model=members[1])
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: auto.retry(self.path, self.project, retry), range(2)))
        self.assertEqual(self.launch.call_count, 3)
        with patch.object(auto, "run_model", return_value=("completed", "second answer")):
            auto.worker(self.path, root, 2, members[1])
        thread = self.read(f"thread/{root}")["messages"]
        self.assertEqual([r["from"] for r in thread[1:]], members)
        self.assertEqual([r["reply"] for r in thread[1:]], [root, root])
        self.assertEqual([thread[0]["modelStatuses"][m]["attempt"] for m in members], [1, 2])

        def change(expected, new):
            return self.view.dispatch("write", dict(token=self.token, action="message/participants",
                                      body=dict(root=root, expected=expected, models=new)))
        self.assertEqual(change(members, []), [])
        with self.assertRaises(ValueError):
            self.send(key="stale", reply=root, models=members)
        with self.assertRaises(ValueError):
            change(members, [members[0]])
        self.assertEqual(self.send(models=members), root)  # recovery survives membership changes
        change([], ["model:qwen"])
        self.assertEqual(self.read("state")["participants"][root], ["model:qwen"])
        mid = self.send(key="follow", text="follow up", reply=thread[-1]["id"], models=["model:qwen"])
        with patch.object(auto, "run_model", return_value=("completed","ok")) as model:
            auto.worker(self.path, mid, 1)
        self.assertEqual(model.call_args.args[0], "model:qwen")
        self.assertEqual([t["content"] for t in json.loads(model.call_args.args[1])],
                         ["question","first answer","second answer","follow up"])
        self.assertEqual(self.launch.call_count, 4)

    def test_custom_models_are_project_scoped_and_support_group_delivery(self):
        def add(name, provider, model):
            return self.view.dispatch("write", dict(token=self.token, action="model/add",
                                      body=dict(name=name, provider=provider, model=model)))["id"]
        a = add("Local QA", "ollama", "qwen2.5:latest")
        b = add("Local QA 2", "ollama", "qwen2.5:latest")
        self.assertEqual(a, add("Local QA", "ollama", "qwen2.5:latest"))
        with self.assertRaises(ValueError):
            add("local qa", "ollama", "other")
        with self.assertRaises(ValueError):
            add("Codex", "codex", "other")
        self.assertEqual(self.read("state")["customModels"], {a:"Local QA", b:"Local QA 2"})
        self.assertIn(a, [r["id"] for r in self.read("message/recipients")])
        mid = self.send(models=[a,b,"model:codex"])
        for model in (a,b,"model:codex"):
            with patch.object(auto, "run_model", return_value=("completed","reply")) as run:
                auto.worker(self.path,mid,1,model)
            self.assertEqual(run.call_args.args[0],model)
            if model != "model:codex":
                self.assertEqual(run.call_args.args[2]["model"],"qwen2.5:latest")
        thread = self.read(f"thread/{mid}")["messages"]
        self.assertEqual({m["state"] for m in thread[0]["modelStatuses"].values()}, {"completed"})
        self.assertEqual([m["from"] for m in thread[1:]], [a,b,"model:codex"])
        single = self.send(key="custom-once", models=[a])
        self.assertEqual(self.read(f"thread/{single}")["messages"][0]["auto"]["state"], "queued")
        with self.assertRaises(ValueError):
            send(self.path, resolve(self.root/"other"), dict(to=a,key="bad",text="no"))

    def test_compatible_endpoint_credentials_stay_bound_to_base_url(self):
        from io import BytesIO
        for base in ("https://user:pass@example.com/v1","http://example.com/v1",
                     "https://example.com/v1?key=secret", "file:///tmp/model"):
            with self.assertRaises(ValueError):
                studio_models.base_url(base)
        first, second = "https://a.invalid/v1", "https://b.invalid/v1"
        with patch.dict(os.environ, {"MEMDEX_STUDIO_API_KEYS": json.dumps({first:"example-token"})}), \
             patch.object(studio_models,"build_opener") as build:
            for base in (first,second):
                build.return_value.open.return_value=BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
                config=dict(base=base,model="example")
                self.assertEqual(studio_models.run_compatible(config,"hello","context",2),("completed","ok"))
                request=build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, base+"/chat/completions")
                self.assertEqual(request.get_header("Authorization"), "Bearer example-token" if base==first else None)
        self.assertIsNone(studio_models._NoRedirect().redirect_request(None,None,302,"",{},second))

    def test_ollama_existing_service_does_not_spawn_and_missing_model_is_reported(self):
        with patch.object(auto.socket,"create_connection"), patch.object(auto.subprocess,"Popen") as start:
            auto._ensure_ollama()
        start.assert_not_called()
        with patch.object(auto,"_ensure_ollama"), patch.object(auto.providers,"run_ollama",return_value=("failed","model missing")):
            mid=self.send("qwen")
            auto.worker(self.path,mid,1)
        self.assertEqual(self.read(f"thread/{mid}")["messages"][0]["auto"]["error"],"model missing")

    def test_stopped_ollama_is_started_locally_on_demand(self):
        from unittest.mock import MagicMock
        with patch.object(auto.socket, "create_connection", side_effect=[ConnectionRefusedError(), MagicMock()]), \
             patch.object(auto.shutil, "which", return_value="C:/Ollama/ollama.exe"), \
             patch.object(auto.subprocess, "Popen") as start:
            auto._ensure_ollama()
        self.assertEqual(start.call_args.args[0], ["C:/Ollama/ollama.exe", "serve"])
        self.assertEqual(start.call_args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
