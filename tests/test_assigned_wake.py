"""Isolated tests with memdex v0.4 Store/Session fixtures. All AI process launches are mocked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from memdex import project as projects
from memdex.store import Store
from memdex.tools import Session
from memdex import assigned_wake as watch

spawn_worker = watch._spawn


class GatedWakeTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        top = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)))
        self.base = top / "dev"
        self.base.mkdir()
        for name, value in dict(MEMDEX_DB=str(top/"memdex.db"), WORKSPACE=self.base,
                                WAKE_LEDGER=top/"wake.db",
                                CLAUDE_BIN="mock-claude.exe", CODEX_BIN="mock-codex.cmd").items():
            stack.enter_context(patch.object(watch, name, value))
        stack.enter_context(patch.object(projects, "git", return_value=None))
        self.now = 2000000.0
        stack.enter_context(patch.object(watch.time, "time", lambda: self.now))
        stack.enter_context(patch.dict(os.environ, {"MEMDEX_AGENT":"teammate"}))
        os.environ.pop("MEMDEX_PROJECT", None)
        self.new_project("fixture")
        self.store = Store(watch.MEMDEX_DB)
        stack.callback(self.store.db.close)
        self.sender = Session(self.store, agent_name="teammate")
        self.sender._project = self.project
        self.child = stack.enter_context(patch.object(watch, "_spawn"))


    def new_project(self, name):
        path = self.base / name
        path.mkdir()
        self.project = projects.resolve(str(path))
        return self.project

    def task(self, owner="gpt"):
        return self.store.create_task(self.project, "teammate", "unit", "proof", owner=owner)

    def tagged(self, owner="gpt", task=None):
        task = task or self.task(owner)
        self.sender.post("unit", to=owner, task_id=task, group=True)
        return self.store.db.execute("SELECT max(id) FROM messages").fetchone()[0]

    def direct(self, recipient, body="unit", **kwargs):
        return self.store.post(self.project, "teammate", body, recipient, **kwargs)

    def dispatch(self):
        with redirect_stdout(StringIO()):
            watch.dispatch_pending()

    def test_two_linked_tasks_one_launch_even_after_restart_and_ttl(self):
        self.tagged()
        self.tagged()
        self.dispatch()
        self.now += 1000
        self.dispatch()
        self.child.assert_called_once()
        binary, args, agent, cwd, mails = self.child.call_args.args
        self.assertEqual((binary,args,agent), ("mock-codex.cmd",["exec","--skip-git-repo-check","-"],"gpt"))
        self.assertEqual(os.path.normcase(cwd),self.project.key)
        self.assertEqual(len(mails), 2)
        self.tagged()
        self.dispatch()
        self.assertEqual(self.child.call_count, 2)

    def test_concurrent_checks_claim_pending_claude_once(self):
        self.tagged("claude")
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: watch.dispatch_pending(), range(4)))
        self.child.assert_called_once()
        self.assertEqual(self.child.call_args.args[2], "claude")

    def test_bare_legacy_group_broadcast_and_exact_alone_never_trigger(self):
        task = self.task()
        for recipient in (None, "", "gpt", "gpt@fixture", "gpt/other", "gpt-proxy"):
            self.direct(recipient)
        route = "route:" + "a" * 32
        header = f"[task status at send]\ntask #{task} [todo]; owner gpt: unit\n\nunit"
        self.direct(route, header, recipient_route=route)
        self.direct(None, header)
        self.direct("gpt", "imitated quote\n"+header)
        self.dispatch()
        self.child.assert_not_called()

    def test_no_open_task_nor_current_task_assignment_no_launch(self):
        self.direct("gpt","[task status at send]\ntask #73 [todo]; owner gpt: nowhere")
        self.tagged()
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET status='in_progress'")
        self.dispatch()
        self.child.assert_not_called()

    def test_inbox_cursor_of_any_recipient_sibling_suppresses_read_mail(self):
        msg = self.tagged()
        self.store.acknowledge_inbox(self.project, "gpt/one@fixture", msg)
        self.store.acknowledge_inbox(self.project, "gpt/two@fixture", msg-1)
        self.dispatch()
        self.child.assert_not_called()

    def test_project_local_chat_route_blocks_and_similar_name_does_not(self):
        self.tagged()
        route = "route:"+"a"*32
        self.store.register_route(self.project,route,"codex/sub@fixture")
        self.dispatch()
        self.child.assert_not_called()
        self.now += 601
        self.store.touch_session(self.project, "gpt-mirror@fixture")
        other = self.new_project("other")
        self.store.touch_session(other, "gpt@other")
        self.dispatch()
        self.child.assert_called_once()

    def test_live_session_then_cursor_read_no_launch(self):
        mid = self.tagged("claude")
        self.store.touch_session(self.project, "claude/sub@fixture")
        self.dispatch()
        self.now += 601
        self.store.acknowledge_inbox(self.project, "claude@fixture", mid)
        self.dispatch()
        self.child.assert_not_called()

    def test_missing_binary_not_reserved(self):
        self.tagged("claude")
        with patch.object(watch, "CLAUDE_BIN", None):
            self.dispatch()
        self.child.assert_not_called()
        self.dispatch()
        self.child.assert_called_once()

    def test_missing_outside_and_wrongly_mapped_project_skip(self):
        self.tagged()
        valid = self.project.key
        for bad in (str(self.base.parent), str(self.base/"missing"), "relative"):
            with self.store.db:
                self.store.db.execute("UPDATE tasks SET project=?", (bad,))
                self.store.db.execute("UPDATE messages SET project=?", (bad,))
            self.dispatch()
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET project=?", (valid,))
            self.store.db.execute("UPDATE messages SET project=?", (valid,))
        with patch.object(watch, "resolve", return_value=projects.Project(valid,"wrong","x",False)):
            self.dispatch()
        self.child.assert_not_called()

    def test_crash_after_reservation_never_retries_same_task(self):
        self.tagged()
        self.child.side_effect = OSError("simulated")
        with self.assertRaises(OSError):
            self.dispatch()
        self.now += 700
        self.dispatch()
        self.child.assert_called_once()

    def test_dispatch_only_does_not_run_maintenance_or_consume(self):
        self.tagged()
        with patch("sys.argv", ["watch","--workspace",str(self.base)]), redirect_stdout(StringIO()):
            self.assertEqual(watch.main(),0)
        self.child.assert_called_once()

        self.assertEqual(len(self.store.inbox(self.project,"gpt@fixture",peek=True)), 1)

    def test_only_reserved_task_messages_reach_child(self):
        self.direct("gpt", "private unrelated")
        target = self.tagged()
        self.store.acknowledge_inbox(self.project, "route:"+"a"*32, target)
        self.store.acknowledge_inbox(self.project, "codex@fixture", target)
        self.dispatch()
        messages=self.child.call_args.args[-1]
        self.assertEqual([msg["message_id"] for msg in messages], [target])
        self.assertEqual(len(self.store.inbox(self.project,"gpt@fixture",peek=True)),2)
        with patch.dict(os.environ, {"CODEX_THREAD_ID":"parent", "MEMDEX_CONVERSATION_ID":"parent"}):
            child = watch.child_env("gpt",self.project.root)
            self.assertNotIn("CODEX_THREAD_ID",child)
            self.assertNotIn("MEMDEX_CONVERSATION_ID",child)
            self.assertEqual(child["MEMDEX_AGENT"], "gpt")

    def test_message_data_only_enters_stdin_of_project_scoped_worker(self):
        worker = Mock()
        injected = "&& fake-command | plus $(fake)"
        with patch.object(watch.subprocess, "Popen", return_value=worker) as popen:
            spawn_worker("codex.cmd", ["exec", "--skip-git-repo-check", "-"], "gpt",
                         self.project.root, [{"message": injected}])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["cmd.exe", "/d", "/c", "codex.cmd", "exec", "--skip-git-repo-check", "-"])
        self.assertEqual(popen.call_args.kwargs["cwd"], self.project.root)
        self.assertEqual(popen.call_args.kwargs["env"]["MEMDEX_AGENT"], "gpt")
        self.assertIn(injected.encode(), worker.stdin.write.call_args.args[0])
        worker.stdin.close.assert_called_once()
