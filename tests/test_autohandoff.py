"""A session that ends without a handoff still leaves one, drafted from evidence.

Covers the Claude Code SessionEnd/PreCompact hook (`memdex hook session-end`, transcript on stdin),
`memdex handoff --auto` for agents without a hook, and the pickup counter behind `memdex stats`.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from memdex import autohandoff
from memdex import project as projects
from memdex.cli import main
from memdex.store import Store
from memdex.tools import render_brief

REPO = str(Path(__file__).resolve().parents[1])


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("VALUE = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.env = dict(os.environ, MEMDEX_HOME=self.tmp,
                        PYTHONPATH=os.pathsep.join(x for x in (REPO, os.environ.get("PYTHONPATH")) if x))
        self.env.pop("MEMDEX_AUTO_HANDOFF", None)
        self.env.pop("MEMDEX_AGENT", None)
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)
        self.t0 = time.time() - 3600

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       cwd=self.root, check=True, capture_output=True)

    def cli(self, *args, agent="claude"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*args, "--path", self.root, "--agent", agent, "--db", self.db])
        return code, out.getvalue() + err.getvalue()

    def transcript(self, rows):
        path = os.path.join(self.tmp, "session.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def session(self, last="Renamed VALUE and updated the callers.", edits=("src/store.py",), offset=0):
        """A short Claude Code style transcript: a request, edits, a tool result, a closing message."""
        at = self.t0 + offset
        rows = [
            {"type": "user", "timestamp": iso(at),
             "message": {"role": "user", "content": "<system-reminder>ignore me</system-reminder>"}},
            {"type": "user", "timestamp": iso(at + 1),
             "message": {"role": "user", "content": "Rename VALUE to LIMIT in the store"}},
        ]
        for i, rel in enumerate(edits):
            rows.append({"type": "assistant", "timestamp": iso(at + 10 + i), "message": {
                "role": "assistant", "content": [
                    {"type": "text", "text": "Editing."},
                    {"type": "tool_use", "name": "Edit",
                     "input": {"file_path": os.path.join(self.root, *rel.split("/"))}}]}})
            rows.append({"type": "user", "timestamp": iso(at + 11 + i), "message": {
                "role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
        rows.append({"type": "assistant", "timestamp": iso(at + 60),
                     "message": {"role": "assistant", "content": [{"type": "text", "text": last}]}})
        return rows

    def handoffs(self, active=True):
        where = "AND archived=0 AND superseded_by IS NULL" if active else ""
        return self.store.db.execute(
            f"SELECT * FROM notes WHERE kind='handoff' {where} ORDER BY id").fetchall()

    def hook(self, payload, agent="claude", extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        proc = subprocess.run(
            [sys.executable, "-m", "memdex", "hook", "session-end", "--path", self.root,
             "--agent", agent, "--db", self.db],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr


class DraftTests(Base):
    def test_transcript_becomes_a_labelled_handoff(self):
        path = self.transcript(self.session())
        out = autohandoff.save(self.store, self.project, "claude@proj", path)
        self.assertIn("auto handoff #1 saved", out)
        note = self.handoffs()[0]
        self.assertTrue(note["title"].startswith("auto: Rename VALUE to LIMIT"))
        self.assertIn("(auto)", note["body"])
        self.assertIn('Asked: "Rename VALUE to LIMIT in the store"', note["body"])
        self.assertIn("Edited: src/store.py.", note["body"])
        self.assertIn('Last message: "Renamed VALUE and updated the callers."', note["body"])
        self.assertNotIn("ignore me", note["body"])
        self.assertIn("src/store.py", note["refs"])

    def test_brief_shows_the_draft_to_the_next_agent(self):
        autohandoff.save(self.store, self.project, "claude@proj", self.transcript(self.session()))
        brief = render_brief(self.store, self.project, "codex@proj")
        self.assertIn("last handoff #1 (claude@proj", brief)
        self.assertIn("(auto)", brief)
        self.assertIn("Edited: src/store.py", brief)

    def test_agents_own_handoff_is_never_overridden(self):
        self.cli("handoff", "renamed VALUE", "--next", "update docs")
        out = autohandoff.save(self.store, self.project, "claude@proj", self.transcript(self.session()))
        self.assertIn("no auto handoff", out)
        self.assertEqual(len(self.handoffs()), 1)

    def test_work_after_the_agents_handoff_is_drafted_separately(self):
        self.cli("handoff", "renamed VALUE")
        rows = self.session(edits=("src/store.py",)) + self.session(
            last="Also fixed the api.", edits=("src/api.py",), offset=7200)
        out = autohandoff.save(self.store, self.project, "claude@proj", self.transcript(rows))
        self.assertIn("auto handoff #2 saved", out)
        body = self.handoffs()[-1]["body"]
        self.assertIn("kept working after handoff #1", body)
        self.assertIn("Edited: src/api.py.", body)
        self.assertNotIn("src/store.py", body)

    def test_reading_only_session_leaves_nothing(self):
        path = self.transcript(self.session(edits=()))
        out = autohandoff.save(self.store, self.project, "claude@proj", path)
        self.assertIn("no auto handoff", out)
        self.assertEqual(self.handoffs(), [])

    def test_second_draft_of_a_session_replaces_the_first(self):
        path = self.transcript(self.session())
        autohandoff.save(self.store, self.project, "claude@proj", path)
        self.assertEqual(autohandoff.save(self.store, self.project, "claude@proj", path),
                         "auto handoff unchanged.")
        path = self.transcript(self.session(last="Now the tests pass.", edits=("src/store.py", "src/api.py")))
        out = autohandoff.save(self.store, self.project, "claude@proj", path)
        self.assertIn("replaces #1", out)
        active = self.handoffs()
        self.assertEqual(len(active), 1)
        self.assertIn("Now the tests pass.", active[0]["body"])
        self.assertEqual(len(self.handoffs(active=False)), 2)  # the first is kept, superseded

    def test_credentials_and_secret_files_stay_out(self):
        Path(self.root, ".env").write_text("X=1\n", encoding="utf-8")
        rows = self.session(last="Set api_key=sk-abcdefghijklmnop1234 and token: hunter2secret for the client.",
                            edits=("src/store.py", ".env"))
        autohandoff.save(self.store, self.project, "claude@proj", self.transcript(rows))
        body = self.handoffs()[0]["body"]
        self.assertNotIn("sk-abcdefghijklmnop1234", body)
        self.assertNotIn("hunter2secret", body)
        self.assertNotIn(".env", body)
        self.assertIn("[redacted]", body)

    def test_open_tasks_claims_and_notes_are_carried(self):
        self.cli("task", "create", "Ship the rename", "--criterion", "tests pass", "--owner", "claude")
        self.cli("claim", "src/api.py", "--intent", "rename")
        self.cli("remember", "pitfall", "LIMIT is also read by the exporter")
        autohandoff.save(self.store, self.project, "claude@proj", self.transcript(self.session()))
        body = self.handoffs()[0]["body"]
        self.assertIn("Open tasks: #1 [todo] Ship the rename", body)
        self.assertIn("Still claimed by claude: src/api.py", body)
        self.assertIn("Pitfalls: #", body)
        # A draft is not proof the work is over: claims are left to expire.
        self.assertEqual(len(self.store.active_claims(self.project)), 1)

    def test_git_changes_and_commits_are_reported(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        Path(self.root, "src", "store.py").write_text("LIMIT = 1\n", encoding="utf-8")
        Path(self.root, "src", "새 파일.py").write_text("X = 1\n", encoding="utf-8")
        self.store.close()
        self.store = Store(self.db)
        self.project = projects.resolve(self.root)
        path = self.transcript(self.session())
        autohandoff.save(self.store, self.project, f"claude@{self.project.name}", path)
        body = self.handoffs()[0]["body"]
        self.assertIn("Commits since the session began (any author): ", body)
        self.assertIn("base", body)
        self.assertIn("Uncommitted: ", body)
        self.assertIn("src/store.py", body)
        self.assertIn("src/새 파일.py", body)
        self.assertIn("1 not edited by this session", body)

    def test_long_transcript_keeps_the_request_and_the_outcome(self):
        rows = self.session()
        filler = {"type": "assistant", "timestamp": iso(self.t0 + 30),
                  "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 2000}]}}
        rows[3:3] = [filler] * 40
        path = self.transcript(rows)
        head, tail = autohandoff.HEAD_BYTES, autohandoff.TAIL_BYTES
        autohandoff.HEAD_BYTES, autohandoff.TAIL_BYTES = 600, 1500
        try:
            seen = autohandoff.read_transcript(path, self.project)
        finally:
            autohandoff.HEAD_BYTES, autohandoff.TAIL_BYTES = head, tail
        self.assertEqual(seen["prompts"][0][1], "Rename VALUE to LIMIT in the store")
        self.assertEqual(seen["last"], "Renamed VALUE and updated the callers.")

    def test_garbage_transcript_is_ignored(self):
        path = os.path.join(self.tmp, "bad.jsonl")
        Path(path).write_bytes(b"\xff\xfe not json\n[1,2]\n{\"type\":\"user\"}\n")
        self.assertIn("no auto handoff", autohandoff.save(self.store, self.project, "claude@proj", path))
        self.assertIn("no auto handoff", autohandoff.save(self.store, self.project, "claude@proj", "missing"))


class HookAndCliTests(Base):
    def test_session_end_hook_writes_the_draft_and_exits_zero(self):
        rc, out = self.hook({"hook_event_name": "SessionEnd", "cwd": self.root,
                             "transcript_path": self.transcript(self.session())})
        self.assertEqual(rc, 0, out)
        self.assertIn("auto handoff #1 saved", out)
        self.assertEqual(len(self.handoffs()), 1)

    def test_hook_survives_a_bad_payload(self):
        for payload in ({}, {"transcript_path": 7}, {"transcript_path": os.path.join(self.tmp, "nope")}):
            rc, out = self.hook(payload)
            self.assertEqual(rc, 0, out)
        self.assertEqual(self.handoffs(), [])

    def test_env_switch_turns_it_off(self):
        rc, out = self.hook({"transcript_path": self.transcript(self.session())},
                            extra_env={"MEMDEX_AUTO_HANDOFF": "0"})
        self.assertEqual(rc, 0, out)
        self.assertIn("off", out)
        self.assertEqual(self.handoffs(), [])

    def test_handoff_auto_from_a_shell_uses_uncommitted_work(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        Path(self.root, "src", "api.py").write_text("VALUE = 3\n", encoding="utf-8")
        code, out = self.cli("handoff", "--auto", "--since", "30m", agent="codex")
        self.assertEqual(code, 0, out)
        self.assertIn("auto handoff #1 saved", out)
        body = self.handoffs()[0]["body"]
        self.assertIn("Uncommitted: src/api.py", body)
        code, out = self.cli("handoff", "--auto", "--since", "nonsense", agent="codex")
        self.assertEqual(code, 2)

    def test_plain_handoff_still_needs_content(self):
        code, out = self.cli("handoff")
        self.assertEqual(code, 2)
        self.assertIn("--auto", out)
        code, out = self.cli("handoff", "shipped it")
        self.assertEqual(code, 0, out)

    def test_install_adds_session_end_hooks_once(self):
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 0, out)
        hooks = json.loads(Path(self.root, ".claude", "settings.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("SessionEnd", "PreCompact"):
            self.assertEqual(len(hooks[event]), 1)
            self.assertIn("hook session-end --agent claude", hooks[event][0]["hooks"][0]["command"])
            self.assertNotIn("matcher", hooks[event][0])
        self.cli("hook", "install", "--claude", "--project")
        hooks = json.loads(Path(self.root, ".claude", "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual([len(hooks[e]) for e in ("PreToolUse", "SessionEnd", "PreCompact")], [1, 1, 1])

    def test_install_upgrades_a_settings_file_that_has_only_the_edit_hook(self):
        target = Path(self.root, ".claude", "settings.json")
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": "memdex hook pre-edit --agent claude"}]}]}}), encoding="utf-8")
        code, out = self.cli("hook", "install", "--claude", "--project")
        self.assertEqual(code, 0, out)
        self.assertIn("SessionEnd", out)
        hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(len(hooks["PreToolUse"]), 1)
        self.assertIn("SessionEnd", hooks)

    def test_config_prints_the_session_end_snippet(self):
        code, out = self.cli("config")
        self.assertEqual(code, 0)
        self.assertIn("hook session-end", out)


class PickupTests(Base):
    def stats(self):
        return autohandoff.pickup_stats(self.store)

    def test_another_agents_brief_counts_once_per_handoff(self):
        self.cli("handoff", "renamed VALUE", "--next", "docs", agent="claude")
        self.assertEqual(self.stats()["other_agent"], 0)
        render_brief(self.store, self.project, "codex@proj")
        render_brief(self.store, self.project, "codex@proj")
        render_brief(self.store, self.project, "deepseek@proj")
        got = self.stats()
        self.assertEqual((got["written"], got["auto"], got["other_agent"], got["later_session"]), (1, 0, 1, 0))

    def test_authors_immediate_brief_is_not_a_pickup_but_a_later_one_is(self):
        self.cli("handoff", "renamed VALUE", agent="claude")
        render_brief(self.store, self.project, "claude@proj")
        self.assertEqual(self.stats()["later_session"], 0)
        self.store.db.execute("UPDATE notes SET created_at=created_at-3600 WHERE kind='handoff'")
        self.store.db.commit()
        render_brief(self.store, self.project, "claude@proj")
        got = self.stats()
        self.assertEqual((got["other_agent"], got["later_session"]), (0, 1))

    def test_stats_command_reports_handoff_pickups(self):
        autohandoff.save(self.store, self.project, "claude@proj", self.transcript(self.session()))
        render_brief(self.store, self.project, "codex@proj")
        code, out = self.cli("stats")
        self.assertEqual(code, 0, out)
        self.assertIn("handoffs: 1 written (1 drafted automatically)  shown to another agent 1", out)


class RoutingTests(unittest.TestCase):
    """A session started in a folder above the repositories it edits (the common desktop setup): the draft
    belongs to each repository that changed, not to the parent folder."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.work = os.path.join(self.tmp, "work")
        self.alpha, self.beta = os.path.join(self.work, "alpha"), os.path.join(self.work, "beta")
        for repo in (self.alpha, self.beta):
            os.makedirs(os.path.join(repo, "src"))
            Path(repo, "src", "app.py").write_text("X = 1\n", encoding="utf-8")
            self.git(repo, "init", "-q")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-q", "-m", "base")
        os.makedirs(os.path.join(self.work, "notes"))
        self.db = os.path.join(self.tmp, "memdex.db")
        self.env = dict(os.environ, MEMDEX_HOME=self.tmp,
                        PYTHONPATH=os.pathsep.join(x for x in (REPO, os.environ.get("PYTHONPATH")) if x))
        for name in ("MEMDEX_AUTO_HANDOFF", "MEMDEX_AGENT", "MEMDEX_PROJECT"):
            self.env.pop(name, None)
        self.store = Store(self.db)
        self.home = projects.resolve(self.work)
        self.t0 = time.time() - 3600

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def git(cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       cwd=cwd, check=True, capture_output=True)

    def transcript(self, edits, prompts=("Tidy both services",), last="Done with both.", name="s.jsonl"):
        rows, at = [], self.t0
        for text in prompts[:1]:
            rows.append({"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": text}})
        for i, path in enumerate(edits):
            rows.append({"type": "assistant", "timestamp": iso(at + 10 + i), "message": {
                "role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]}})
        for j, text in enumerate(prompts[1:]):
            rows.append({"type": "user", "timestamp": iso(at + 500 + j),
                         "message": {"role": "user", "content": text}})
        rows.append({"type": "assistant", "timestamp": iso(at + 900),
                     "message": {"role": "assistant", "content": [{"type": "text", "text": last}]}})
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def notes(self, repo):
        project = projects.resolve(repo)
        return self.store.db.execute(
            "SELECT * FROM notes WHERE project=? AND kind='handoff' AND archived=0 AND superseded_by IS NULL "
            "ORDER BY id", (project.key,)).fetchall()

    def test_draft_lands_in_each_edited_repository_not_the_parent_folder(self):
        path = self.transcript([os.path.join(self.alpha, "src", "app.py"),
                                os.path.join(self.alpha, "src", "new.py"),
                                os.path.join(self.beta, "src", "app.py")])
        out = autohandoff.save(self.store, self.home, "claude@work", path, session="abcdef0123456789")
        self.assertIn("in alpha", out)
        self.assertIn("in beta", out)
        self.assertEqual(self.notes(self.work), [])
        alpha, beta = self.notes(self.alpha)[0], self.notes(self.beta)[0]
        self.assertEqual(alpha["author"], "claude@alpha")
        self.assertIn("Edited: src/app.py, src/new.py.", alpha["body"])
        self.assertNotIn("beta/", alpha["body"])
        self.assertIn("Also edited in other projects: beta (1 file).", alpha["body"])
        self.assertIn("Edited: src/app.py.", beta["body"])
        self.assertIn("Also edited in other projects: alpha (2 files).", beta["body"])
        self.assertIn("(auto) session abcdef01 ended without a handoff", beta["body"])
        # The next agent in that repository sees it; the parent folder's brief stays clean.
        self.assertIn("last handoff", render_brief(self.store, projects.resolve(self.beta), "codex@beta"))
        self.assertNotIn("last handoff", render_brief(self.store, self.home, "codex@work"))

    def test_worktree_edits_belong_to_the_repository(self):
        tree = os.path.join(self.work, "alpha-wt")
        self.git(self.alpha, "worktree", "add", "-q", tree, "-b", "side")
        path = self.transcript([os.path.join(tree, "src", "app.py"), os.path.join(self.alpha, "src", "app.py")])
        autohandoff.save(self.store, self.home, "claude@work", path, session="s1")
        notes = self.notes(self.alpha)
        self.assertEqual(len(notes), 1)
        self.assertIn("Edited: src/app.py.", notes[0]["body"])
        self.assertEqual(self.notes(tree)[0]["id"], notes[0]["id"])

    def test_files_in_the_parent_stay_there_and_files_outside_any_project_are_dropped(self):
        outside = os.path.join(self.tmp, "elsewhere", "scratch.py")
        os.makedirs(os.path.dirname(outside))
        path = self.transcript([os.path.join(self.work, "notes", "plan.md"), outside,
                                os.path.join(self.work, "gone", "deep", "old.py")])
        autohandoff.save(self.store, self.home, "claude@work", path, session="s1")
        body = self.notes(self.work)[0]["body"]
        self.assertIn("Edited: notes/plan.md, gone/deep/old.py.", body)
        self.assertNotIn("scratch.py", body)
        self.assertNotIn("Also edited", body)

    def test_handoff_written_from_inside_the_repository_counts_as_the_agents_own(self):
        code = main(["handoff", "renamed X", "--path", self.alpha, "--agent", "claude", "--db", self.db])
        self.assertEqual(code, 0)
        self.store.db.execute("UPDATE notes SET created_at=? WHERE kind='handoff'", (self.t0 + 300,))
        self.store.db.commit()
        # Everything in the transcript happened before that handoff: nothing to add.
        path = self.transcript([os.path.join(self.alpha, "src", "app.py")])
        out = autohandoff.save(self.store, self.home, "claude@work", path, session="s1")
        self.assertIn("no auto handoff", out)
        self.assertEqual(len(self.notes(self.alpha)), 1)

    def test_two_sessions_keep_their_own_drafts_and_one_session_replaces_its_own(self):
        edit = [os.path.join(self.alpha, "src", "app.py")]
        first = self.transcript(edit, last="Session one stopped here.", name="one.jsonl")
        second = self.transcript(edit, last="Session two stopped here.", name="two.jsonl")
        autohandoff.save(self.store, self.home, "claude@work", first, session="11111111aaaa")
        autohandoff.save(self.store, self.home, "claude@work", second, session="22222222bbbb")
        self.assertEqual(len(self.notes(self.alpha)), 2)
        again = self.transcript(edit, last="Session one, later.", name="one.jsonl")
        out = autohandoff.save(self.store, self.home, "claude@work", again, session="11111111aaaa")
        self.assertIn("replaces #1", out)
        bodies = [n["body"] for n in self.notes(self.alpha)]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("Session two stopped here." in b for b in bodies))
        self.assertTrue(any("Session one, later." in b for b in bodies))
        self.assertEqual(autohandoff.save(self.store, self.home, "claude@work", again, session="11111111aaaa"),
                         "auto handoff unchanged in alpha.")

    def test_latest_request_is_reported_and_host_wrappers_are_peeled_off(self):
        path = self.transcript(
            [os.path.join(self.alpha, "src", "app.py")],
            prompts=("<system-reminder>\nmemory index…\n</system-reminder>\n<system-reminder>x</system-reminder>\n\n"
                     "Tidy both services",
                     "<system-reminder>[SYSTEM NOTIFICATION] task done</system-reminder>",
                     "<system-reminder>linked sessions…</system-reminder>\n\nNow push it and check CI",
                     "Another Claude session sent a message:\n<cross-session-message>hi</cross-session-message>"))
        autohandoff.save(self.store, self.home, "claude@work", path, session="s1")
        note = self.notes(self.alpha)[0]
        self.assertEqual(note["title"], "auto: Tidy both services")
        self.assertIn('Asked: "Tidy both services"', note["body"])
        self.assertIn('Latest request: "Now push it and check CI"', note["body"])
        self.assertNotIn("system-reminder", note["body"])
        self.assertNotIn("memory index", note["body"])

    def test_hook_reads_a_non_ascii_folder_as_utf8(self):
        repo = os.path.join(self.work, "한글 프로젝트")
        os.makedirs(repo)
        Path(repo, "메모.py").write_text("X = 1\n", encoding="utf-8")
        self.git(repo, "init", "-q")
        path = self.transcript([os.path.join(repo, "메모.py")])
        payload = json.dumps({"hook_event_name": "SessionEnd", "cwd": repo, "transcript_path": path,
                              "session_id": "feedbeef0000"}, ensure_ascii=False).encode("utf-8")
        proc = subprocess.run([sys.executable, "-m", "memdex", "hook", "session-end", "--agent", "claude",
                               "--db", self.db], input=payload, capture_output=True, env=self.env,
                              cwd=self.tmp, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        notes = self.notes(repo)
        self.assertEqual(len(notes), 1, proc.stdout.decode("utf-8", "replace"))
        self.assertEqual(notes[0]["author"], "claude@한글 프로젝트")
        self.assertIn("Edited: 메모.py.", notes[0]["body"])
        self.assertIn("(auto) session feedbeef ended", notes[0]["body"])

    def test_a_new_folder_the_session_filled_is_not_reported_as_someone_elses(self):
        fresh = os.path.join(self.alpha, "docs", "guide.md")
        os.makedirs(os.path.dirname(fresh))
        Path(fresh).write_text("# guide", encoding="utf-8")
        Path(self.alpha, "stray.txt").write_text("x", encoding="utf-8")
        autohandoff.save(self.store, self.home, "claude@work", self.transcript([fresh]), session="s1")
        body = self.notes(self.alpha)[0]["body"]
        self.assertIn("Uncommitted: docs/, stray.txt (1 not edited by this session", body)

    def test_no_more_than_the_cap_of_other_repositories_get_a_draft(self):
        edits = [os.path.join(self.alpha, "src", "app.py"), os.path.join(self.beta, "src", "app.py")]
        cap = autohandoff.MAX_OTHER_PROJECTS
        autohandoff.MAX_OTHER_PROJECTS = 1
        try:
            edits.append(os.path.join(self.alpha, "src", "more.py"))
            out = autohandoff.save(self.store, self.home, "claude@work", self.transcript(edits), session="s1")
        finally:
            autohandoff.MAX_OTHER_PROJECTS = cap
        self.assertIn("in alpha", out)
        self.assertNotIn("in beta", out)
        self.assertEqual(self.notes(self.beta), [])


if __name__ == "__main__":
    unittest.main()
