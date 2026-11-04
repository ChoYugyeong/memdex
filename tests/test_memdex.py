import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from memdex import project as projects
from memdex.mcp import handle
from memdex.store import SCHEMA_VERSION, Store
from memdex.tools import Session, render_brief


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

    def test_stale_summary_says_what_differed_without_a_contradiction(self):
        write(self.root, "core.py", "def save(k, v):\n    pass\n")
        s = self.session()
        s.file_summary_save("core.py", "Saves values.")
        write(self.root, "core.py", "def save(k, v):\n    pass\n\n\ndef load(k):\n    return None\n")
        out = s.file_summary("core.py")
        self.assertIn("symbol(s) differ", out)
        self.assertNotIn("symbols changed:", out)
        self.assertNotIn("of 1 symbols", out)

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
        a.post("only for deepseek", to="deepseek", group=True)
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


class LockTests(Base):
    def test_exclusive_claim_refuses_and_is_refused(self):
        write(self.root, "src/store/db.py", "x = 1\n")
        a, b = self.session("claude"), self.session("deepseek")
        self.assertIn("exclusive", a.claim(["src/store"], "refactor", exclusive=True))
        out = b.claim(["src/store/db.py"], "bugfix")
        self.assertTrue(out.startswith("✗ not claimed"), out)
        self.assertIn("claude@proj", out)
        agents = [r[0] for r in self.store.db.execute("SELECT agent FROM claims")]
        self.assertEqual(agents, ["claude@proj"])  # the refused claim was not recorded
        # an advisory claim elsewhere is fine, and an exclusive claim over an advisory holder is refused
        self.assertIn("claimed 1 path", b.claim(["README.md"], "docs"))
        self.assertTrue(a.claim(["README.md"], exclusive=True).startswith("✗ not claimed"))
        # force records the claim and says so
        self.assertIn("forced past a lock", b.claim(["src/store/db.py"], "hotfix", force=True))
        # the file tools warn the next reader about other agents' claims, never about its own
        self.assertIn("claimed by claude@proj (refactor", b.file_summary("src/store/db.py"))
        self.assertIn("exclusive", b.read_snippet("src/store/db.py"))
        self.assertNotIn("claude@proj", a.file_summary("src/store/db.py"))
        self.assertIn("claimed by deepseek@proj (hotfix", a.file_summary("src/store/db.py"))
        # handoff releases the lock; '.' locks the whole project
        a.handoff("done")
        b.claim(release=True)
        self.assertIn("claimed the whole project", b.claim(["."], "solo", exclusive=True))
        self.assertTrue(self.session("codex").claim(["anything.py"]).startswith("✗ not claimed"))

    def test_brief_lists_changed_files_with_owners(self):
        write(self.root, "a.py", "a = 1\n")
        write(self.root, "b.py", "b = 1\n")
        a, b = self.session("claude"), self.session("deepseek")
        a.brief()
        write(self.root, "a.py", "a = 2\n")
        a.claim(["a.py"], "editing")
        line = next(l for l in b.brief().splitlines() if l.startswith("changed since last check"))
        self.assertEqual(line, "changed since last check: a.py (claimed by claude@proj)")


class ResearchTests(Base):
    """Activation ranking, cold tier, pinned notes, confirm, symbol-level refs, expert routing,
    duplicate hints and the head/middle/tail brief layout."""

    def test_activation_orders_used_notes_first_and_hides_cold_ones(self):
        s = self.session()
        s.remember("decision", "old unused", "x")
        s.remember("decision", "used often", "y")
        old = time.time() - 1500 * 86400
        self.store.db.execute("UPDATE notes SET created_at=? WHERE id=1", (old,))
        self.store.db.execute("UPDATE notes SET created_at=?, use_count=5, last_used_at=? WHERE id=2",
                              (old, time.time()))
        self.store.db.commit()
        brief = s.brief()
        self.assertIn("decisions: #2 used often", brief)
        self.assertNotIn("old unused", brief)          # cold: out of the brief…
        self.assertIn("[decision #1]", s.search("old unused"))  # …but still found when asked for

    def test_pinned_confirm_and_symbol_refs(self):
        write(self.root, "db.py", "def connect():\n    return 1\n\n\ndef close():\n    return 0\n")
        s = self.session()
        s.remember("pitfall", "close must flush", "or data is lost", files=["db.py#close"])
        s.remember("decision", "use unittest, not pytest", "", pin=True)
        brief = s.brief()
        self.assertIn("pinned:\n📌 #2 use unittest, not pytest", brief)
        self.assertLess(brief.index("pinned:"), brief.index("pitfalls:"))
        # editing connect() does not flag a note that cites close()
        write(self.root, "db.py", "def connect():\n    return 2\n\n\ndef close():\n    return 0\n")
        self.assertNotIn("⚠", s.note(1))
        write(self.root, "db.py", "def connect():\n    return 2\n\n\ndef close():\n    return -1\n")
        out = s.note(1)
        self.assertIn("⚠ changed since this was written: db.py#close", out)
        self.assertIn("note(1, 'confirm')", out)
        self.assertIn("confirmed #1", s.note(1, "confirm"))
        self.assertNotIn("⚠", s.note(1))
        self.assertIn("unpinned #2", s.note(2, "unpin"))
        self.assertNotIn("pinned:", s.brief())

    def test_file_summary_reports_which_symbols_changed(self):
        write(self.root, "svc.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n")
        s = self.session()
        s.file_summary_save("svc.py", "Three helpers.")
        write(self.root, "svc.py", "def a():\n    return 1\n\n\ndef b():\n    return 20\n\n\ndef c():\n    return 3\n")
        out = s.file_summary("svc.py")
        self.assertIn("since then 1 symbol(s) differ: b; likely still valid for the other 2", out)
        self.assertIn("Three helpers.", out)

    def test_duplicate_hint_and_expert_routing(self):
        write(self.root, "src/a.py", "x = 1\n")
        write(self.root, "src/b.py", "y = 2\n")
        write(self.root, "src/c.py", "z = 3\n")
        a, b = self.session("claude"), self.session("deepseek")
        a.file_summary_save("src/a.py", "Holds x.")
        a.file_summary_save("src/b.py", "Holds y.")
        self.assertIn("claude@proj wrote 2 summaries/notes under src/ — ask them", b.file_summary("src/c.py"))
        self.assertIn("experts: src/ → claude@proj (2)", b.brief())
        a.remember("pitfall", "WAL mode needed for concurrent writers", "database is locked otherwise")
        out = a.remember("pitfall", "WAL mode is needed for concurrent writers", "database is locked otherwise")
        self.assertIn("≈ similar to #1", out)
        self.assertIn("supersedes=1", out)

    def test_brief_trims_the_middle_not_the_ends(self):
        s = self.session()
        s.handoff("built it", next="wire the CLI")
        s.remember("pitfall", "never cut the tail", "")
        for i in range(30):
            s.remember("decision", f"decision number {i} with a reasonably long title to fill the budget", "")
        text = render_brief(self.store, s.project, s.agent, max_chars=400)
        self.assertLessEqual(len(text), 600)
        self.assertIn("Next: wire the CLI", text)
        self.assertIn("pitfalls: #2 never cut the tail", text)
        self.assertIn("do first: wire the CLI", text)
        self.assertIn("trimmed", text)
        self.assertNotIn("decisions:", text)


class SchemaMigrationTests(unittest.TestCase):
    def test_v1_claims_table_gains_the_exclusive_column(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memdex.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE claims(project TEXT, agent TEXT, path TEXT, intent TEXT, "
                    "created_at REAL, expires_at REAL, PRIMARY KEY(project, agent, path))")
        con.execute("INSERT INTO claims VALUES('p','a','x','i',0,9e12)")
        con.execute("PRAGMA user_version=1")
        con.commit()
        con.close()
        store = Store(path)
        try:
            self.assertIn("exclusive", [r[1] for r in store.db.execute("PRAGMA table_info(claims)")])
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertEqual(store.db.execute("SELECT exclusive FROM claims").fetchone()[0], 0)
        finally:
            store.close()
            shutil.rmtree(tmp, ignore_errors=True)


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
