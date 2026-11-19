"""Project-wide observation from the CLI must leave each chat's mailbox intact."""
import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from memdex import board, board_web, cli, project
from memdex.store import Store
from memdex.tools import Session


class BoardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "app")
        self.root.mkdir()
        self.db = self.root.parent / "shared db.sqlite"
        self.project = project.resolve(self.root)
        env = patch.dict(os.environ, {"MEMDEX_AGENT": "", "MEMDEX_PROJECT": str(self.root), "COLUMNS": "120"})
        env.start()
        self.addCleanup(env.stop)

    def command(self, *args, root=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["board", "--db", str(self.db), "--path", str(root or self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def store(self):
        store = Store(str(self.db))
        self.addCleanup(store.close)
        return store

    def test_uninitialized_board_does_not_create_database(self):
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        for heading in ("SESSIONS", "TASK TREE", "RECENT MESSAGES"):
            self.assertIn(heading, out)
        self.assertFalse(self.db.exists())

    @unittest.skipUnless(shutil.which("git"), "git required for linked worktrees")
    def test_shared_worktrees_exact_chats_task_tree_and_unread_reply(self):
        def git(*args):
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=qa@example.test",
                            "-c", "commit.gpgsign=false", *args], cwd=self.root,
                           check=True, capture_output=True, timeout=20)

        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "fixture")
        sibling = self.root.with_name("review")
        git("worktree", "add", "-q", "-b", "review", str(sibling))
        store = self.store()
        a = Session(store, agent_name="claude", cwd=str(self.root), conversation_id="first")
        b = Session(store, agent_name="qwen", cwd=str(sibling), conversation_id="second")
        c = Session(store, agent_name="qwen", cwd=str(sibling), conversation_id="third")
        goal = store.create_task(a.project, a.agent, "Ship inbox", "Reviewed")
        task = store.create_task(a.project, a.agent, "수신함 검증", "Read once", parent_id=goal, owner="qwen")
        for chat in (a, b, c):
            chat.session_routes(label="QA", task_id=task)
        store.touch_session(a.project, "deepseek@app")
        a.post("Check the inbox", to=b.route)
        mid = store.inbox(b.project, b.route, peek=True)[0]["id"]
        b.post("Checked both reads", reply_to=mid)
        with patch.dict(os.environ, {"COLUMNS": "160"}):
            code, out, err = self.command(root=sibling)
        self.assertEqual((code, err), (0, ""))
        for term in (f"  #{task} [todo] qwen", "수신함 검증", "deepseek@app",
                     f"task #{task} · review", f"qwen~{c.route[-8:]}",
                     f"claude~{a.route[-8:]} → qwen~{b.route[-8:]}",
                     "Check the inbox", "Checked both reads", f"reply to #{mid}"):
            self.assertIn(term, out)
        self.assertLess(out.index("Checked both reads"), out.index("Check the inbox"))
        self.assertNotIn("[task status at send]", out)
        code, full, err = self.command("--message", str(mid))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[task status at send]", full)
        self.assertIn("Check the inbox", full)
        self.assertIsNone(store.last_read(a.project, b.route))
        self.assertIn("Check the inbox", b.inbox())
        self.assertNotIn("Check the inbox", b.inbox())
        self.assertIn("Checked both reads", a.inbox())

    def test_message_details_and_recent_list_are_project_scoped(self):
        store = self.store()
        foreign = self.root.with_name("other")
        foreign.mkdir()
        private = store.post(project.resolve(foreign), "test", "other-project message")
        mid = store.post(self.project, "a", "old message")
        tail = "마지막 줄" * 40
        new = store.post(self.project, "b", "first line\n" + tail, to="a")
        code, out, _ = self.command("--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("first line", out)
        self.assertNotIn("old message", out)
        self.assertNotIn("other-project message", self.command()[1])
        self.assertIn(tail, self.command("--message", str(new))[1])
        self.assertIn("old message", self.command("--message", str(mid))[1])
        code, out, err = self.command("--message", str(private))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not found in this project", err)

    def test_legacy_mail_without_chat_routes_is_read_without_migration(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,project,sender,recipient,body,created_at)")
            db.execute("INSERT INTO messages VALUES (1,?,?,?,?,0)", (self.project.key, "deepseek", None, "hi"))
        original = self.db.read_bytes()
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("deepseek → everyone", out)
        self.assertEqual(self.db.read_bytes(), original)

    def test_watching_an_empty_project_observes_a_later_post_once(self):
        calls = 0

        def tick(_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.store().post(self.project, "deepseek", "new event")
            if calls == 3:
                raise KeyboardInterrupt

        with patch("memdex.board.time.sleep", side_effect=tick):
            code, out, err = self.command("--watch")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("No messages recorded.", out)
        self.assertEqual(out.count("new event"), 1)
        self.assertNotIn("\x1b", out)

    def test_small_split_and_wide_terminal_keep_all_sections_and_sanitize_output(self):
        store = self.store()
        for _ in range(12):
            store.post(self.project, "claude", "한글 테스트" * 12 + "\x1b[2J")
        data = board.snapshot(self.db, self.project, 12)
        for width, height in ((44, 21), (120, 30)):
            with self.subTest(width=width):
                frame = board.render(data, self.project, width, height)
                self.assertLessEqual(len(frame.splitlines()), height)
                self.assertNotIn("\x1b", frame)
                for heading in ("SESSIONS", "TASK TREE", "RECENT MESSAGES", "한글"):
                    self.assertIn(heading, frame)
                self.assertTrue(all(sum(board._cell_width(ch) for ch in line) <= width
                                    for line in frame.splitlines()))
        self.assertNotIn("\x1b", self.command("--message", "1")[1])

    def test_invalid_message_and_limit_give_a_cli_error(self):
        for args in (("--limit", "0"), ("--limit", "101"), ("--message", str(2**63)), ("--message", "-1")):
            with self.subTest(args=args):
                code, out, err = self.command(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(args[0], err)

    def web(self, endpoint, host="127.0.0.1:5678", method="GET"):
        # Exercise the actual HTTP parser without relying on permission to open a client socket.
        raw = f"{method} {endpoint} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode("ascii")
        sent = bytearray()
        connection = SimpleNamespace(makefile=lambda *_: io.BytesIO(raw), sendall=sent.extend)
        handler = board_web.handler_for(self.db, self.project, 1, "/local/")
        handler(connection, ("127.0.0.1", 4321), SimpleNamespace(server_port=5678))
        headers, body = bytes(sent).split(b"\r\n\r\n", 1)
        code = int(headers.split(b" ", 2)[1])
        return code, headers, body

    def test_web_history_and_thread_keep_task_context_across_pages(self):
        store = self.store()
        foreign = self.root.with_name("other")
        foreign.mkdir()
        other = project.resolve(foreign)
        msg = store.post(self.project, "a", "[Qwen review task #9]\nReview inbox")
        response = store.post(self.project, "qwen-review", "Reviewed", reply_to=msg)
        latest = store.post(self.project, "a", "Thanks", reply_to=response)
        secret = store.post(other, "other", "another project", reply_to=msg)
        code, headers, body = self.web("/local/api/state")
        self.assertEqual(code, 200)
        self.assertIn(b"Cache-Control: no-store", headers)
        first = json.loads(body)
        self.assertEqual([m["id"] for m in first["messages"]], [latest])
        self.assertTrue(first["more"])
        self.assertEqual(first["messages"][0]["task"], 9)
        history = json.loads(self.web(f"/local/api/history/{latest}")[2])
        self.assertEqual([m["id"] for m in history["messages"]], [response])
        oldest = json.loads(self.web(f"/local/api/history/{response}")[2])
        self.assertFalse(oldest["more"])
        self.assertEqual(oldest["messages"][0]["body"], "Review inbox")
        thread = json.loads(self.web(f"/local/api/thread/{response}")[2])["messages"]
        self.assertEqual([m["id"] for m in thread], [msg, response, latest])
        self.assertEqual(thread[0]["context"], "[Qwen review task #9]")
        self.assertEqual(self.web(f"/local/api/thread/{secret}")[0], 404)
        self.assertIsNone(store.last_read(self.project, "a"))

    def test_web_only_serves_scoped_assets_and_read_routes(self):
        for asset, marker in (("", b"workspace.js"), ("workspace.js", b"readThread"), ("workspace.css", b".explorer")):
            code, headers, body = self.web("/local/"+asset)
            self.assertEqual(code, 200)
            self.assertIn(marker, body)
            self.assertIn(b"frame-ancestors 'none'", headers)
        for path in ("/", "/api/state", "/local/../store.py", "/local/api/thread/0",
                     "/local/api/thread/9223372036854775808", "/local/api/state?project=other"):
            self.assertEqual(self.web(path)[0], 404)
        self.assertEqual(self.web("/local/api/state", host="other.example")[0], 404)
        self.assertEqual(self.web("/local/api/state", method="POST")[0], 405)
        self.assertFalse(self.db.exists())

    def test_web_cli_passes_resolved_project_and_explicit_database(self):
        with patch("memdex.board_web.serve", return_value=0) as serve:
            code, _, _ = self.command("--web", "--no-open", "--limit", "20")
        self.assertEqual(code, 0)
        serve.assert_called_once_with(str(self.db), self.project, 20, True)
        self.assertEqual(self.command("--no-open")[0], 2)

    def test_portable_workspace_embeds_only_project_data_and_packaged_script(self):
        store = self.store()
        store.post(self.project, "a", "원문 </script> 끝")
        foreign = self.root.with_name("foreign")
        foreign.mkdir()
        store.post(project.resolve(foreign), "b", "not-in-export")
        target = self.root / "board.html"
        self.assertEqual(self.command("--html", str(target))[0], 0)
        page = target.read_text(encoding="utf-8")
        self.assertNotIn("not-in-export", page)
        self.assertNotIn('<script src=', page)
        archive = json.loads(re.search(r'id="archive">(.*?)</script>', page).group(1))
        self.assertEqual([m["body"] for m in archive["messages"]], ["원문 </script> 끝"])
        script = re.search(r'<script>(.*?)</script>', page, re.S).group(1)
        encoded = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
        self.assertIn("sha256-"+encoded, page)
        self.assertEqual(self.command("--html", str(target))[0], 1)
        self.assertEqual(page, target.read_text(encoding="utf-8"))
