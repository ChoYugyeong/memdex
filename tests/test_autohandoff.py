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
        self.assertIn("continued after handoff #1", body)
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
        self.assertIn("Still claimed by claude@proj: src/api.py", body)
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
        self.assertIn("Commits: ", body)
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


if __name__ == "__main__":
    unittest.main()
