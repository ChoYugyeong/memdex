from contextlib import closing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from memdex import project
from memdex.store import Store
from memdex.studio_rpc import project_groups, Studio


class ProjectGroupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "requires Git worktrees")
    def test_worktrees_same_names_and_historical_messages(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"MEMDEX_HOME": temp}):
            base = Path(temp)
            repo, other = base / "one" / "app", base / "two" / "app"
            repo.mkdir(parents=True)
            other.mkdir(parents=True)
            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            git("init", "-q")
            git("-c", "user.name=QA", "-c", "user.email=qa@example.test", "commit", "-q", "--allow-empty", "-m", "initial")
            tree = base / "alternate"
            git("worktree", "add", "-q", "--detach", str(tree))
            main, linked, separate = (project.resolve(p) for p in (repo, tree, other))
            db = base / "test.db"
            with closing(Store(str(db))) as store:
                first = store.post(main, "same-agent", "first")
                second = store.post(linked, "same-agent", "second", reply_to=first)
                other_mid = store.post(separate, "same-agent", "message mentioning " + main.key)
                store.register_route(linked, "route:" + "a" * 32, "same-agent", "app")
                store.create_task(linked, "same-agent", "task", "criterion")
            rows = {r["key"]: r for r in project_groups(db)}
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[main.key]["messages"], rows[main.key]["tasks"]), (2, 1))
            self.assertEqual(rows[separate.key]["messages"], 1)
            self.assertIn(linked.root, rows[main.key]["checkouts"])
            studio = Studio(db, 5)
            one = studio.open_group(main.key, linked.root)
            self.assertEqual(one["folder"], linked.root)
            self.assertEqual([m["id"] for m in studio.dispatch("read", dict(token=one["token"], endpoint=f"thread/{second}"))["messages"]],
                             [first, second])
            with self.assertRaises(LookupError):
                studio.dispatch("read", dict(token=one["token"], endpoint=f"thread/{other_mid}"))
            other.rmdir()
            two = studio.open_group(separate.key)
            self.assertIsNone(two["folder"])
            self.assertEqual(studio.dispatch("read", dict(token=two["token"], endpoint="state"))["messages"][0]["id"], other_mid)
            for action in ("write", "terminalFolder"):
                with self.assertRaises(ValueError):
                    studio.dispatch(action, dict(token=two["token"]))
            with self.assertRaisesRegex(ValueError, "closed"):
                studio.dispatch("read", dict(token=one["token"], endpoint="state"))
            git("worktree", "remove", str(tree))
