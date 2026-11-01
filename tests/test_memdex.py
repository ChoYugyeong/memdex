import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.mcp import handle
from memdex.store import Store
from memdex.tools import Session


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    # make sure the stat changes even on filesystems with coarse mtimes
    t = time.time() + write.bump
    write.bump += 2
    os.utime(p, (t, t))


write.bump = 0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, agent="claude", root=None):
        s = Session(self.store, agent_name=agent)
        s.cwd = root or self.root
        return s


class IndexTests(Base):
    def test_outline_and_symbols(self):
        write(self.root, "app/main.py", '"""Entry point."""\nclass App:\n    def run(self):\n        pass\n\ndef helper():\n    pass\n')
        write(self.root, "web/ui.ts", "export function render() {}\nexport const handler = async (e) => {}\ninterface Props {}\n")
        s = self.session()
        s.store.index(s.project)
        self.assertIn("app/main.py:3 method App.run", s.where_is("run"))
        self.assertIn("web/ui.ts:2 fn handler", s.where_is("handler"))
        self.assertIn("Entry point.", s.file_summary("app/main.py"))

    def test_incremental_and_removal(self):
        write(self.root, "a.py", "x = 1\n")
        write(self.root, "b.py", "y = 2\n")
        s = self.session()
        self.assertEqual(s.store.index(s.project)["changed"], 2)
        self.assertEqual(s.store.index(s.project)["changed"], 0)
        write(self.root, "a.py", "x = 3\n")
        os.remove(os.path.join(self.root, "b.py"))
        st = s.store.index(s.project)
        self.assertEqual((st["changed"], st["removed"]), (1, 1))

    def test_secrets_and_binaries_skipped(self):
        write(self.root, ".env", "TOKEN=abc")
        write(self.root, "server.key", "x")
        Path(self.root, "img.bin").write_bytes(b"\0\1\2")
        write(self.root, "ok.md", "# Title\n")
        s = self.session()
        s.store.index(s.project)
        self.assertEqual(s.store.file_stats(s.project)["files"], 1)
        self.assertIn("not indexed", s.file_summary(".env"))


class SummaryCacheTests(Base):
    def test_summary_valid_until_content_changes(self):
        write(self.root, "core.py", "def f():\n    return 1\n")
        s = self.session()
        s.file_summary_save("core.py", "Returns one. Used by everything.")
        self.assertIn("matches current content", s.file_summary("core.py"))
        write(self.root, "core.py", "def f():\n    return 2\n")
        out = s.file_summary("core.py")
        self.assertIn("no summary for the current content", out)
        self.assertIn("older summary", out)
        self.assertIn("Returns one", out)

    def test_summary_shared_by_identical_content(self):
        other = os.path.join(self.tmp, "copy")
        os.makedirs(other)
        write(self.root, "x.py", "VALUE = 42\n")
        write(other, "x.py", "VALUE = 42\n")
        self.session().file_summary_save("x.py", "Holds the answer.")
        self.assertIn("Holds the answer.", self.session(root=other).file_summary("x.py"))

    def test_absolute_path_and_outside(self):
        write(self.root, "src/m.py", "A = 1\n")
        s = self.session()
        out = s.file_summary(os.path.join(self.root, "src", "m.py"))
        self.assertTrue(out.startswith("src/m.py · python"), out)
        self.assertIn("not indexed", s.file_summary("../elsewhere.py"))


class NoteTests(Base):
    def test_remember_search_supersede(self):
        write(self.root, "db.py", "def connect():\n    pass\n")
        s = self.session()
        s.remember("pitfall", "WAL 모드 필요", "동시 접근 시 database is locked 오류가 난다", files=["db.py"])
        self.assertIn("WAL 모드 필요", s.search("locked"))
        self.assertIn("WAL 모드 필요", s.search("오류"))  # two-character Korean term
        self.assertIn("pitfalls: #1", s.brief())
        s.remember("pitfall", "WAL + busy_timeout", "busy_timeout도 같이", supersedes=1)
        self.assertNotIn("#1 ", s.search("WAL"))
        write(self.root, "db.py", "def connect(timeout):\n    pass\n")
        self.assertIn("changed since", s.search("busy_timeout") + s.note(1))

    def test_handoff_in_brief(self):
        s = self.session()
        s.handoff("Built the parser", next="Wire up the CLI", pitfalls="Windows paths")
        brief = self.session(agent="deepseek").brief()
        self.assertIn("last handoff", brief)
        self.assertIn("Wire up the CLI", brief)


class AgentTests(Base):
    def test_messages(self):
        a, b = self.session("claude"), self.session("deepseek")
        a.post("I'm refactoring store.py, hold off")
        a.post("only for deepseek", to="deepseek")
        a.post("only for codex", to="codex")
        self.assertIn("inbox: 2 unread", b.brief())
        out = b.inbox()
        self.assertIn("refactoring", out)
        self.assertIn("only for deepseek", out)
        self.assertNotIn("codex", out)
        self.assertEqual(b.inbox(), "no unread messages.")
        self.assertEqual(a.inbox(), "no unread messages.")  # own messages are not delivered back

    def test_claims_conflict_and_release(self):
        a, b = self.session("claude"), self.session("deepseek")
        a.claim(["src/store"], "refactor")
        out = b.claim(["src/store/db.py"], "bugfix")
        self.assertIn("overlaps", out)
        self.assertIn("claude@proj", out)
        self.assertIn("other agents are editing", b.brief())
        a.handoff("done")
        self.assertNotIn("claude@proj", b.claim())


class GitWorktreeTests(Base):
    def test_worktrees_share_notes_not_files(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        run = lambda *a, cwd=self.root: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        write(self.root, "main.py", "def main():\n    pass\n")
        write(self.root, "ignored.log", "x")
        write(self.root, ".gitignore", "*.log\n")
        run("add", ".")
        run("commit", "-qm", "init")
        wt = os.path.join(self.tmp, "proj-wt")
        run("worktree", "add", "-q", wt)
        a, b = self.session("deepseek"), self.session("claude", root=wt)
        self.assertEqual(a.project.key, b.project.key)
        self.assertNotEqual(a.project.root, b.project.root)
        a.remember("decision", "Use SQLite", "zero dependencies")
        self.assertIn("Use SQLite", b.brief())
        self.assertIn("claude@proj-wt", b.agent)
        files = [r["path"] for r in self.store.db.execute("SELECT path FROM files WHERE root=?", (a.project.root,))]
        self.assertNotIn("ignored.log", files)


class McpTests(Base):
    def test_protocol(self):
        s = self.session(agent="")
        tools = {t.name: t for t in s.tools()}
        init = handle(s, tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "codex"}}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertTrue(s.agent.startswith("codex@"))
        self.assertIsNone(handle(s, tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = handle(s, tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertIn("file_summary", [t["name"] for t in listed])
        called = handle(s, tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "remember", "arguments": {"kind": "bogus", "title": "x"}}})
        self.assertIn("kind must be", called["result"]["content"][0]["text"])
        bad = handle(s, tools, {"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(bad["error"]["code"], -32601)


class SnippetTests(Base):
    def test_query_centered_within_budget(self):
        body = "\n".join(f"line {i}" for i in range(1, 401)).replace("line 200", "def target():")
        write(self.root, "big.txt", body)
        out = self.session().read_snippet("big.txt", query="TARGET", max_tokens=100)  # ~400 chars
        head = out.splitlines()[0]
        self.assertIn("big.txt:", head)
        self.assertIn("truncated", head)
        self.assertIn("def target():", out)
        self.assertIn("line 199", out)  # some context before the match
        self.assertLess(len(out), 700)

    def test_line_no_match_and_guards(self):
        write(self.root, "f.py", "a = 1\nb = 2\nc = 3\n")
        s = self.session()
        self.assertTrue(s.read_snippet("f.py", line=2).startswith("f.py:1-3 of 3 lines\n"))
        self.assertIn("no match for 'zzz'", s.read_snippet("f.py", query="zzz"))
        self.assertIn("not read", s.read_snippet("../outside.py"))
        self.assertIn("not readable", s.read_snippet("missing.py"))


class KensakuTests(Base):
    def test_hits_merged_and_deduplicated(self):
        from memdex import kensaku
        write(self.root, "docs/auth.md", "# Auth flow\nTokens rotate hourly.\n")
        s = self.session()
        s.file_summary_save("docs/auth.md", "Auth flow doc: token rotation.")
        fake = [{"path": os.path.join(self.root, "docs", "auth.md"), "snippet": "Tokens rotate", "match": "Content"},
                {"path": os.path.join(self.root, "docs", "design.pdf"), "snippet": "auth design", "match": "Semantic"}]
        orig = kensaku.search
        kensaku.search = lambda query, root=None, limit=5, timeout=15: fake
        try:
            out = s.search("auth")
        finally:
            kensaku.search = orig
        self.assertEqual(out.count("docs/auth.md"), 1)
        self.assertIn("[kensaku] docs/design.pdf — auth design", out)

    def test_off_switch_and_missing_binary(self):
        from memdex import kensaku
        old = os.environ.get("MEMDEX_KENSAKU")
        try:
            os.environ["MEMDEX_KENSAKU"] = "off"
            self.assertIsNone(kensaku.command())
            os.environ["MEMDEX_KENSAKU"] = os.path.join(self.tmp, "no-such-kensaku-cli")
            self.assertEqual(kensaku.search("anything"), [])
        finally:
            if old is None:
                os.environ.pop("MEMDEX_KENSAKU", None)
            else:
                os.environ["MEMDEX_KENSAKU"] = old


if __name__ == "__main__":
    unittest.main()
