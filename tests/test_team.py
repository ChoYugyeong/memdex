"""Team roster: roles map to an agent and a model; tasks assigned by role wake that agent on that model.

All AI process launches are mocked.
"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from memdex import assigned_wake as watch
from memdex import cli, team
from memdex.tools import render_brief
from tests import test_assigned_wake as wake_fixture

spawn_worker = wake_fixture.spawn_worker


class TeamBase(wake_fixture.GatedWakeTests):
    # Reuses the isolated wake fixture's setUp and helpers; its own tests are not inherited.
    locals().update({name: None for name in dir(wake_fixture.GatedWakeTests) if name.startswith("test_")})

    def seat(self, role, agent, model="", does="", project=None):
        return team.set_role(self.store.db, project or self.project.key, role, agent, model, does)

    def by_role(self, role, owner_agent):
        """A todo task assigned through a role, with the task-tagged request that makes it wakeable."""
        self.sender.tasks("create", title="unit", criterion="proof", owner="role:" + role)
        task = self.store.db.execute("SELECT max(id) FROM tasks").fetchone()[0]
        self.sender.post("unit", to=owner_agent, task_id=task, group=True)
        return task


class RosterTests(TeamBase):
    def test_a_project_role_replaces_the_machine_wide_one(self):
        self.seat("reviewer", "DeepSeek@Somewhere", project=team.GLOBAL)
        self.seat("pm", "claude", "opus-5.5", project=team.GLOBAL)
        self.seat("reviewer", "gpt", "gpt-6.2-astra", "code review")
        seats = team.roster(self.store.db, self.project.key)
        self.assertEqual(list(seats), ["pm", "reviewer"])
        self.assertEqual((seats["reviewer"]["agent"], seats["reviewer"]["scope"]), ("gpt", "project"))
        self.assertEqual((seats["pm"]["agent"], seats["pm"]["scope"]), ("claude", "machine"))
        self.assertTrue(team.remove_role(self.store.db, self.project.key, "reviewer"))
        self.assertEqual(team.roster(self.store.db, self.project.key)["reviewer"]["agent"], "deepseek")

    def test_names_that_could_reach_a_shell_are_refused(self):
        for model in ("opus & calc", 'a"b', "a b", "$(x)", "a|b", "-rf", "x" * 81):
            with self.assertRaises(ValueError, msg=model):
                self.seat("pm", "claude", model)
        for role in ("", "role:pm", "a b", "-x", "x" * 33):
            with self.assertRaises(ValueError, msg=role):
                self.seat(role, "claude")
        with self.assertRaises(ValueError):
            self.seat("pm", "claude; rm")
        self.assertEqual(team.roster(self.store.db, self.project.key), {})
        self.assertEqual(self.seat("리뷰", "claude", "claude-opus-5-5[1m]")["model"], "claude-opus-5-5[1m]")

    def test_readers_tolerate_a_database_without_the_tables(self):
        self.assertEqual(team.roster(self.store.db, self.project.key), {})
        self.assertIsNone(team.task_seat(self.store.db, self.project.key, 1, "gpt"))
        self.assertFalse(team.remove_role(self.store.db, self.project.key, "pm"))
        self.assertEqual(team.brief_line(self.store.db, self.project.key, "claude@x"), "")
        self.assertEqual(self.sender.tasks("create", title="t", criterion="c", owner="gpt")[:14], "created task #")


class BriefTests(TeamBase):
    def test_the_brief_names_the_roster_and_the_readers_own_seat(self):
        self.assertNotIn("team:", render_brief(self.store, self.project, "claude@fixture"))
        self.seat("pm", "claude", "opus-5.5", "plans and integrates")
        self.seat("reviewer", "deepseek", does="code review")
        mine = render_brief(self.store, self.project, "claude@fixture")
        self.assertIn("team: pm=claude (opus-5.5) · reviewer=deepseek — your role: pm (plans and integrates)", mine)
        other = render_brief(self.store, self.project, "codex@fixture")
        self.assertIn("team: pm=claude (opus-5.5) · reviewer=deepseek ·", other)
        self.assertNotIn("your role", other)


class TaskOwnerTests(TeamBase):
    def test_role_owner_resolves_to_the_agent_and_remembers_the_seat(self):
        self.seat("reviewer", "gpt", "gpt-6.2-astra")
        out = self.sender.tasks("create", title="Review", criterion="findings listed", owner="role:Reviewer")
        self.assertIn("owner gpt as reviewer", out)
        row = self.store.db.execute("SELECT id, owner FROM tasks").fetchone()
        self.assertEqual(row["owner"], "gpt")
        self.assertEqual(team.task_seat(self.store.db, self.project.key, row["id"], "gpt")["model"], "gpt-6.2-astra")

    def test_an_unknown_role_changes_nothing_and_lists_the_known_ones(self):
        self.seat("pm", "claude")
        with self.assertRaisesRegex(ValueError, r"no 'art' role .*roles: pm"):
            self.sender.tasks("create", title="Sprite", criterion="drawn", owner="role:art")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_assigning_by_name_clears_the_seat_and_by_role_sets_it(self):
        self.seat("reviewer", "gpt", "gpt-6.2-astra")
        self.seat("pm", "claude", "opus-5.5")
        self.sender.tasks("create", title="t", criterion="c", owner="role:reviewer")
        task = self.store.db.execute("SELECT id FROM tasks").fetchone()[0]
        self.sender.tasks("assign", id=task, owner="gpt")
        self.assertIsNone(team.task_seat(self.store.db, self.project.key, task, "gpt"))
        self.sender.tasks("assign", id=task, owner="role:pm")
        self.assertEqual(self.store.db.execute("SELECT owner FROM tasks").fetchone()[0], "claude")
        self.assertEqual(team.task_seat(self.store.db, self.project.key, task, "claude")["role"], "pm")
        self.assertIsNone(team.task_seat(self.store.db, self.project.key, task, "gpt"))  # no longer gpt's seat

    def test_a_seat_moved_to_another_agent_stops_applying_to_the_old_owner(self):
        self.seat("reviewer", "gpt", "gpt-6.2-astra")
        self.sender.tasks("create", title="t", criterion="c", owner="role:reviewer")
        self.seat("reviewer", "claude", "opus-5.5")
        self.assertIsNone(team.task_seat(self.store.db, self.project.key, 1, "gpt"))


class WakeModelTests(TeamBase):
    def test_a_task_held_through_a_seat_starts_the_agent_on_the_seats_model(self):
        self.seat("reviewer", "gpt", "gpt-6.2-astra", "code review")
        task = self.by_role("reviewer", "gpt")
        events = self.dispatch()
        self.child.assert_called_once()
        binary, args, agent, cwd, mails = self.child.call_args.args
        self.assertEqual(self.child.call_args.kwargs, {"model": "gpt-6.2-astra"})
        self.assertEqual(mails[0]["role"], {"name": "reviewer", "does": "code review"})
        self.assertEqual((mails[0]["task_id"], "model" in mails[0]), (task, False))
        self.assertEqual(events, [{"wake": "gpt", "project": self.project.key, "model": "gpt-6.2-astra"}])

    def test_the_model_is_read_at_launch_so_a_later_change_applies(self):
        self.seat("pm", "claude", "sonnet-5.5")
        self.by_role("pm", "claude")
        self.seat("pm", "claude", "opus-5.5")
        self.dispatch()
        self.assertEqual(self.child.call_args.kwargs, {"model": "opus-5.5"})

    def test_tasks_named_directly_or_on_a_seat_without_a_model_launch_as_before(self):
        self.seat("reviewer", "gpt")
        self.by_role("reviewer", "gpt")
        self.tagged("gpt")
        self.dispatch()
        self.child.assert_called_once()
        self.assertEqual(self.child.call_args.kwargs, {})
        self.assertEqual(len(self.child.call_args.args[-1]), 2)

    def test_seats_on_different_models_get_one_chat_per_model(self):
        self.seat("reviewer", "gpt", "gpt-6.2-astra")
        self.seat("coder", "gpt", "gpt-6.2-mini")
        first, second = self.by_role("reviewer", "gpt"), self.by_role("coder", "gpt")
        plain = self.task("gpt")
        self.tagged("gpt", plain)
        self.dispatch()
        seen = {call.kwargs.get("model", ""): [m["task_id"] for m in call.args[-1]]
                for call in self.child.call_args_list}
        self.assertEqual(seen, {"": [plain], "gpt-6.2-astra": [first], "gpt-6.2-mini": [second]})

    def test_the_model_reaches_each_cli_as_its_own_argument(self):
        mail = [{"task_id": 7, "message_id": 1, "sender": "teammate", "sender_route": None, "message": "m",
                 "role": {"name": "reviewer", "does": "code review"}}]
        for binary, args, agent, expected in (
                ("mock-claude.exe", ["-p"], "claude", ["mock-claude.exe", "-p"]),
                ("mock-codex.cmd", ["exec", "--skip-git-repo-check", "-"], "gpt",
                 ["cmd.exe", "/d", "/c", "mock-codex.cmd", "exec", "--skip-git-repo-check", "-m", "opus-5.5", "-"])):
            with patch.object(subprocess, "Popen") as popen:
                spawn_worker(binary, args, agent, self.project.root, mail, model="opus-5.5")
            command = popen.call_args.args[0]
            self.assertEqual(command[:len(expected)], expected, agent)
            self.assertEqual(command[command.index("opus-5.5") - 1], "-m" if agent == "gpt" else "--model")
            self.assertEqual(command.count("opus-5.5"), 1)
            prompt = popen.return_value.stdin.write.call_args.args[0].decode("utf-8")
            self.assertIn("`role` is the seat you hold", prompt)
            self.assertIn('"does": "code review"', prompt)

    def test_a_qwen_seat_picks_the_local_ollama_model(self):
        self.seat("checker", "qwen-code", "qwen3:14b")
        self.by_role("checker", "qwen-code")
        asked = {}

        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return json.dumps({"response": "ok", "done": True}).encode()

        def opened(request, timeout):
            asked.update(json.loads(request.data))
            return Reply()

        with patch.object(watch, "build_opener") as opener:
            opener.return_value.open.side_effect = opened
            self.dispatch(qwen_ollama=True)
        self.assertEqual(asked["model"], "qwen3:14b")


class CliTests(TeamBase):
    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", watch.MEMDEX_DB, "team", *argv, "--path", self.project.root])
        return code, out.getvalue(), err.getvalue()

    def test_set_list_and_remove(self):
        code, out, _ = self.run_cli("set", "reviewer", "deepseek", "--model", "deepseek-v4.1-flash", "--does", "code review")
        self.assertEqual(code, 0)
        self.assertIn("reviewer = deepseek on deepseek-v4.1-flash", out)
        self.assertIn("memdex does not start deepseek itself", out)
        code, out, _ = self.run_cli("set", "pm", "claude", "--model", "opus-5.5", "--global")
        self.assertIn("(every project)", out)
        self.assertNotIn("does not start", out)
        code, out, _ = self.run_cli()
        self.assertRegex(out, r"pm\s+claude\s+opus-5\.5\s+\[machine\] started by memdex")
        self.assertRegex(out, r"reviewer\s+deepseek\s+deepseek-v4\.1-flash\s+\[project\] started by its own app.* — code review")
        self.assertEqual(self.run_cli("set", "pm", "claude", "--model", "bad model")[0], 1)
        self.assertEqual(self.run_cli("remove", "reviewer")[0], 0)
        self.assertEqual(self.run_cli("remove", "reviewer")[0], 1)


if __name__ == "__main__":
    unittest.main()
