"""Isolated tests with memdex v0.4 Store/Session fixtures. All AI process launches are mocked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from memdex import project as projects
from memdex.store import Store
from memdex.tools import Session
from memdex import assigned_wake as watch

spawn_worker = watch._spawn


def link_dir(target, alias):
    """A directory junction on Windows (what a move to another drive leaves behind), else a symlink."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(alias))
        else:
            os.symlink(target, alias, target_is_directory=True)
    except (OSError, AttributeError, ImportError) as exc:
        raise unittest.SkipTest(f"cannot create a directory link here: {exc}")


class GatedWakeTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        top = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)))
        self.top = top
        self.base = top / "dev"
        self.base.mkdir()
        for name, value in dict(MEMDEX_DB=str(top/"memdex.db"), WORKSPACES=(self.base,),
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


    def new_project(self, name, base=None):
        path = (base or self.base) / name
        path.mkdir()
        self.project = projects.resolve(str(path))
        return self.project

    def use(self, project):
        # Later tasks and task-tagged posts from the teammate go to this project.
        self.project = self.sender._project = project
        return project

    def second_workspace(self):
        # Where repositories moved to (G:\Dev beside C:\Dev), outside the first workspace.
        moved = self.top / "moved"
        moved.mkdir()
        return moved

    def stored_at(self, path):
        # The project as recorded while this path was still a real folder; it is a link now.
        return projects.Project(root=str(path), key=os.path.normcase(str(path)), name=path.name, is_git=False)

    def reply_to(self, message_id):
        return self.store.db.execute("SELECT * FROM messages WHERE reply_to=?", (message_id,)).fetchone()

    def task(self, owner="gpt"):
        return self.store.create_task(self.project, "teammate", "unit", "proof", owner=owner)

    def tagged(self, owner="gpt", task=None):
        task = task or self.task(owner)
        self.sender.post("unit", to=owner, task_id=task, group=True)
        return self.store.db.execute("SELECT max(id) FROM messages").fetchone()[0]

    def direct(self, recipient, body="unit", **kwargs):
        return self.store.post(self.project, "teammate", body, recipient, **kwargs)

    def dispatch(self, **kwargs):
        out = StringIO()
        with redirect_stdout(out):
            watch.dispatch_pending(**kwargs)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def dispatch_qwen(self):
        with patch.object(watch, "run_ollama_qwen", return_value=("completed", "review result")) as model:
            return self.dispatch(qwen_ollama=True), model

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

    def test_qwen_receiver_is_opt_in_and_returns_to_exact_requester(self):
        self.sender.conversation_id = "requester-chat"
        incoming = self.tagged("qwen-code")
        self.tagged("gpt")
        with patch.object(watch, "run_qwen", return_value=("completed", "review result")) as worker:
            self.dispatch()
            worker.assert_not_called()
            self.child.assert_called_once()
            with patch.object(watch.shutil, "which", return_value="node.exe"), redirect_stdout(StringIO()):
                watch.dispatch_pending(self.base / "cli-entry.js")
                watch.dispatch_pending(self.base / "cli-entry.js")
        worker.assert_called_once()
        self.child.assert_called_once()
        row = self.store.db.execute("SELECT * FROM messages WHERE reply_to=?", (incoming,)).fetchone()
        self.assertEqual(row["recipient_route"], self.sender.route)
        self.assertEqual(row["sender"], "qwen-code@fixture")
        self.assertIn("review result", row["body"])

    def test_qwen_input_omits_project_ledger_and_auth_error_is_explicit(self):
        error = [{"is_error": True, "error": {"message": "No auth type is selected."}}]
        with patch.object(watch.subprocess, "run", return_value=Mock(returncode=1, stdout=json.dumps(error))) as run:
            status, _ = watch.run_qwen("node.exe", self.base / "cli-entry.js", self.project.root,
                                       {"task_id": 3, "message": "private ledger header\n\nreview this"})
        self.assertEqual(status, "auth-required")
        self.assertIn("review this", run.call_args.kwargs["input"])
        self.assertNotIn("private ledger header", run.call_args.kwargs["input"])
        self.assertIn("--max-tool-calls", run.call_args.args[0])

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

    def test_a_second_workspace_is_trusted_once_named(self):
        moved = self.second_workspace()
        self.use(self.new_project("seta", base=moved))
        self.sender.conversation_id = "requester-chat"
        incoming = self.tagged("qwen-code")
        self.tagged("gpt")
        out, model = self.dispatch_qwen()
        self.assertEqual(out, [{"skip": "qwen-code", "project": self.project.key,
                                "reason": "outside every --workspace"}])
        self.dispatch()
        model.assert_not_called()
        self.child.assert_not_called()
        with patch.object(watch, "WORKSPACES", (self.base, moved)):
            out, model = self.dispatch_qwen()
            self.dispatch()
        model.assert_called_once()
        self.assertEqual(out[0]["reply"], self.reply_to(incoming)["id"])
        self.child.assert_called_once()
        self.assertEqual(os.path.normcase(self.child.call_args.args[3]), self.project.key)

    def test_a_junction_left_by_a_move_is_answered_under_its_old_key(self):
        moved = self.second_workspace()
        (moved / "legacy").mkdir()
        alias = self.base / "legacy"
        link_dir(moved / "legacy", alias)
        # Requests sent before the move are stored under the old path, which is now a junction.
        key = self.use(self.stored_at(alias)).key
        self.sender.conversation_id = "requester-chat"
        incoming = self.tagged("qwen-code")
        self.tagged("gpt")
        with patch.object(watch, "WORKSPACES", (self.base, moved)):
            out, _ = self.dispatch_qwen()
            skipped = self.dispatch()
        row = self.reply_to(incoming)
        self.assertEqual((row["id"], row["project"], row["sender"], row["recipient_route"]),
                         (out[0]["reply"], key, "qwen-code@legacy", self.sender.route))
        self.assertIn("review result", row["body"])
        # A Codex chat would open the new directory's own project and never see this task.
        self.child.assert_not_called()
        self.assertEqual([(entry["skip"], entry["project"]) for entry in skipped], [("gpt", key)])
        self.assertIn("link", skipped[0]["reason"])

    def test_a_key_or_target_outside_every_workspace_stays_refused(self):
        outside = self.top / "outside"
        outside.mkdir()
        escape, shortcut = self.base / "escape", self.top / "shortcut"
        link_dir(outside, escape)  # inside a workspace, pointing out of it
        link_dir(self.base / "fixture", shortcut)  # stored outside, pointing into one
        stored = (self.stored_at(escape), self.stored_at(shortcut), projects.resolve(str(outside)))
        self.sender.conversation_id = "requester-chat"
        for project in stored:
            self.use(project)
            self.tagged("qwen-code")
            self.tagged("gpt")
        out, model = self.dispatch_qwen()
        skipped = self.dispatch()
        model.assert_not_called()
        self.child.assert_not_called()
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM messages WHERE sender LIKE 'qwen-code@%'").fetchone())
        expected = sorted((agent, project.key) for project in stored for agent in ("gpt", "qwen-code"))
        self.assertEqual(sorted((entry["skip"], entry["project"]) for entry in out + skipped), expected)
        self.assertEqual({entry["reason"] for entry in out + skipped}, {"outside every --workspace"})

    def test_repeated_workspace_flags_are_all_trusted(self):
        first = self.project.key
        self.tagged()
        moved = self.second_workspace()
        self.use(self.new_project("seta", base=moved))
        self.tagged()
        argv = ["watch", "--workspace", str(self.base), "--workspace", str(moved)]
        with patch("sys.argv", argv), redirect_stdout(StringIO()):
            self.assertEqual(watch.main(), 0)
        launched = sorted(os.path.normcase(call.args[3]) for call in self.child.call_args_list)
        self.assertEqual(launched, sorted([first, self.project.key]))

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

    def test_single_claude_task_gets_a_native_id_name_and_launch_scope(self):
        with patch.object(watch.subprocess, "Popen", return_value=Mock()) as popen:
            spawn_worker("claude.exe", ["-p"], "claude", self.project.root, [{"task_id": 35}])
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        chat_id = env["MEMDEX_LAUNCH_CHAT_ID"]
        self.assertEqual(argv, ["claude.exe", "-p", "--session-id", chat_id,
                               "--name", f"fixture-task-35-claude-{chat_id[-8:]}"])
        self.assertEqual(env["MEMDEX_LAUNCH_TASK_ID"], "35")
