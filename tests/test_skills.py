"""Skill registry: scanning SKILL.md files, sharing, finding and showing them across agents."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memdex import skills
from memdex.cli import main
from memdex.store import Store
from memdex.tools import Session


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


FOLDED = (
    "---\n"
    "name: pdf-fill\n"
    "description: >-\n"
    "  Fill PDF forms from JSON, flatten the result and\n"
    "  verify every field. Use for any fillable PDF.\n"
    "---\n"
    "# PDF fill\n\nSteps go here.\n"
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, agent="claude"):
        s = Session(self.store, agent_name=agent)
        s.cwd = self.root
        # The registry refreshes itself on find and on the brief; point it at the fake home so a test never
        # walks the machine running the suite.
        s.home = self.home
        return s


class ParseTests(unittest.TestCase):
    def test_folded_front_matter(self):
        tmp = tempfile.mkdtemp()
        try:
            p = write(tmp, "pdf-fill/SKILL.md", FOLDED)
            name, desc = skills.parse(p)
            self.assertEqual(name, "pdf-fill")
            self.assertEqual(desc, "Fill PDF forms from JSON, flatten the result and verify every field. "
                                   "Use for any fillable PDF.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_front_matter_uses_directory_and_first_paragraph(self):
        tmp = tempfile.mkdtemp()
        try:
            p = write(tmp, "deploy/SKILL.md", "# Deploy\n\nShip the thing to staging.\n\nMore.\n")
            self.assertEqual(skills.parse(p), ("deploy", "Ship the thing to staging."))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ScanTests(Base):
    def test_scan_maps_roots_to_agents_and_prunes(self):
        write(self.home, ".claude/skills/pdf-fill/SKILL.md", FOLDED)
        write(self.home, ".agents/skills/git-bisect/SKILL.md",
              "---\nname: git-bisect\ndescription: Find the commit that broke a test.\n---\n")
        write(self.home, ".dsh/skills/kotlin-build/SKILL.md",
              "---\nname: kotlin-build\ndescription: Build the Android app with the _env batch file.\n---\n")
        write(self.home, ".claude/projects/x/SKILL.md", "---\nname: noise\ndescription: transcript dir\n---\n")
        write(self.root, ".claude/skills/release/SKILL.md", "---\nname: release\ndescription: Tag and publish.\n---\n")
        st = skills.scan(self.store, "codex", self.root, home=self.home)
        self.assertEqual(st["found"], 4)
        rows = {(r["agent"], r["name"]) for r in self.store.db.execute("SELECT agent, name FROM skills")}
        self.assertEqual(rows, {("claude", "pdf-fill"), ("any", "git-bisect"), ("deepseek", "kotlin-build"),
                                ("codex", "release")})

    def test_rescan_drops_missing_files_but_keeps_shared(self):
        p = write(self.home, ".claude/skills/pdf-fill/SKILL.md", FOLDED)
        skills.scan(self.store, "claude", home=self.home)
        skills.share(self.store, "claude", "ask-ops", "How to request a prod change.", "post to ops")
        os.remove(p)
        st = skills.scan(self.store, "claude", home=self.home)
        self.assertEqual(st["removed"], 1)
        names = sorted(r[0] for r in self.store.db.execute("SELECT name FROM skills"))
        self.assertEqual(names, ["ask-ops"])


class RefreshTests(Base):
    def test_a_new_skill_is_picked_up_by_find_without_a_scan(self):
        write(self.home, ".claude/skills/deploy/SKILL.md", "---\nname: deploy\ndescription: Ship it.\n---\n")
        skills.scan(self.store, "claude", home=self.home)
        write(self.home, ".claude/skills/braille/SKILL.md",
              "---\nname: braille\ndescription: Read a braille display from the terminal.\n---\n")
        out = self.session().skills("find", query="braille display")
        self.assertIn("braille", out)

    def test_refresh_reads_only_the_files_that_moved(self):
        write(self.home, ".claude/skills/a/SKILL.md", "---\nname: alpha\ndescription: First skill.\n---\n")
        write(self.home, ".claude/skills/b/SKILL.md", "---\nname: beta\ndescription: Second skill.\n---\n")
        skills.scan(self.store, "claude", home=self.home)
        seen = []
        original = skills.parse
        skills.parse = lambda p: (seen.append(p), original(p))[1]
        try:
            st = skills.refresh(self.store, "claude", home=self.home)
            self.assertEqual(seen, [])
            self.assertEqual(st["unchanged"], 2)
            time.sleep(0.01)
            write(self.home, ".claude/skills/b/SKILL.md",
                  "---\nname: beta\ndescription: Second skill, now with a longer description.\n---\n")
            st = skills.refresh(self.store, "claude", home=self.home)
        finally:
            skills.parse = original
        self.assertEqual([os.path.basename(os.path.dirname(p)) for p in seen], ["b"])
        self.assertEqual(st["updated"], 1)
        self.assertEqual(st["unchanged"], 1)
        desc = self.store.db.execute("SELECT description FROM skills WHERE name='beta'").fetchone()[0]
        self.assertIn("longer description", desc)

    def test_a_deleted_file_leaves_on_the_next_refresh(self):
        p = write(self.home, ".claude/skills/gone/SKILL.md", "---\nname: gone\ndescription: Temporary.\n---\n")
        skills.scan(self.store, "claude", home=self.home)
        os.remove(p)
        st = skills.refresh(self.store, "claude", home=self.home)
        self.assertEqual(st["removed"], 1)
        self.assertIsNone(self.store.db.execute("SELECT id FROM skills WHERE name='gone'").fetchone())

    def test_force_scan_re_reads_an_unchanged_file(self):
        write(self.home, ".claude/skills/a/SKILL.md", "---\nname: alpha\ndescription: First skill.\n---\n")
        skills.scan(self.store, "claude", home=self.home)
        seen = []
        original = skills.parse
        skills.parse = lambda p: (seen.append(p), original(p))[1]
        try:
            skills.scan(self.store, "claude", home=self.home)
        finally:
            skills.parse = original
        self.assertEqual(len(seen), 1)

    def test_an_empty_registry_is_not_filled_by_a_search(self):
        write(self.home, ".claude/skills/a/SKILL.md", "---\nname: alpha\ndescription: First skill.\n---\n")
        self.assertIn("run skills(action=scan) first", self.session().skills("find", query="alpha"))


class OriginTests(Base):
    def test_project_user_and_vendor_are_told_apart(self):
        write(self.root, ".claude/skills/rel/SKILL.md", "---\nname: rel\ndescription: Tag and publish.\n---\n")
        write(self.home, ".claude/skills/usr/SKILL.md", "---\nname: usr\ndescription: My own notes.\n---\n")
        vendor = os.path.join(self.tmp, "vendor", "site-packages", "pkg", "SKILL.md")
        write(os.path.join(self.tmp, "vendor", "site-packages", "pkg"), "SKILL.md",
              "---\nname: pkg\ndescription: Shipped by a package.\n---\n")
        self.assertTrue(os.path.exists(vendor))
        skills.scan(self.store, "claude", self.root, home=self.home,
                    extra_roots=[os.path.join(self.tmp, "vendor")])
        origins = {r["name"]: r["origin"] for r in skills.rows(self.store)}
        self.assertEqual(origins["rel"], "project")
        self.assertEqual(origins["usr"], "user")
        self.assertEqual(origins["pkg"], "vendor")


class ConflictTests(Base):
    def test_the_same_name_in_two_harnesses_is_reported(self):
        write(self.home, ".claude/skills/deploy/SKILL.md",
              "---\nname: deploy\ndescription: Deploy the claude way.\n---\n")
        write(self.home, ".codex/skills/deploy/SKILL.md",
              "---\nname: deploy\ndescription: Deploy the codex way, a different file.\n---\n")
        skills.scan(self.store, "claude", home=self.home)
        found = skills.conflicts(self.store)
        self.assertEqual([c["name"] for c in found], ["deploy"])
        self.assertEqual({e["agent"] for e in found[0]["entries"]}, {"claude", "codex"})
        self.assertEqual(skills.stats(self.store)["conflicts"], 1)

    def test_one_file_installed_twice_is_not_a_conflict(self):
        write(self.home, ".agents/skills/pdf-fill/SKILL.md", FOLDED)
        write(self.home, ".claude/skills/pdf-fill/SKILL.md", FOLDED)
        skills.scan(self.store, "claude", home=self.home)
        self.assertEqual(skills.conflicts(self.store), [])


class JsonTests(Base):
    def setUp(self):
        super().setUp()
        self.env = os.environ.get("MEMDEX_SKILL_HOME")
        os.environ["MEMDEX_SKILL_HOME"] = self.home

    def tearDown(self):
        if self.env is None:
            os.environ.pop("MEMDEX_SKILL_HOME", None)
        else:
            os.environ["MEMDEX_SKILL_HOME"] = self.env
        super().tearDown()

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main([*args, "--path", self.root, "--db", self.store.path])
        return code, buf.getvalue()

    def test_scan_list_find_and_conflicts_are_json(self):
        write(self.home, ".claude/skills/deploy/SKILL.md",
              "---\nname: deploy\ndescription: Deploy the claude way.\n---\n")
        write(self.home, ".codex/skills/deploy/SKILL.md",
              "---\nname: deploy\ndescription: Deploy the codex way, a different file.\n---\n")
        code, out = self.cli("skills", "scan", "--json")
        self.assertEqual(code, 0)
        self.assertIn("found", json.loads(out))
        code, out = self.cli("skills", "list", "--json")
        rows = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["name"] for r in rows), ["deploy", "deploy"])
        self.assertEqual({r["origin"] for r in rows}, {"user"})
        code, out = self.cli("skills", "find", "deploy", "--json")
        hits = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(hits[0]["name"], "deploy")
        code, out = self.cli("skills", "conflicts", "--json")
        self.assertEqual([c["name"] for c in json.loads(out)], ["deploy"])

    def test_show_json_carries_the_body(self):
        write(self.home, ".claude/skills/deploy/SKILL.md",
              "---\nname: deploy\ndescription: Ship it.\n---\n\nSteps go here.\n")
        self.cli("skills", "scan")
        code, out = self.cli("skills", "show", "deploy", "--json")
        got = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(got["name"], "deploy")
        self.assertIn("Steps go here.", got["body"])


class ToolTests(Base):
    def test_find_show_share_and_brief(self):
        write(self.home, ".claude/skills/pdf-fill/SKILL.md", FOLDED)
        write(self.home, ".agents/skills/git-bisect/SKILL.md",
              "---\nname: git-bisect\ndescription: Find the commit that broke a test.\n---\nRun git bisect.\n")
        skills.scan(self.store, "claude", home=self.home)
        s = self.session("deepseek")
        out = s.skills("find", query="fill a PDF form")
        self.assertTrue(out.startswith("- pdf-fill (claude)"), out)
        self.assertNotIn("git-bisect", out)
        shown = s.skills("show", name="pdf-fill")
        self.assertIn("pdf-fill · claude", shown)
        self.assertIn("Steps go here.", shown)
        self.assertIn("share needs name", s.skills("share", name="x"))
        self.assertIn("shared skill 'ego-qa' as deepseek", s.skills("share", name="ego-qa",
                      description="Open the dev server in the ego browser and screenshot it.", invoke="ego-browser skill"))
        self.assertIn("- ego-qa (deepseek · ego-browser skill)", s.skills("find", query="screenshot browser"))
        self.assertIn("skills: 3 shared by 3 agent(s)", s.brief())

    def test_duplicates_merge_and_words_match_whole(self):
        write(self.home, ".agents/skills/pdf-fill/SKILL.md", FOLDED)
        write(self.home, ".claude/skills/pdf-fill/SKILL.md", FOLDED)  # same file installed twice
        write(self.home, ".claude/skills/asset-audit/SKILL.md",
              "---\nname: asset-audit\ndescription: Audit assets against file size budgets and format standards.\n---\n")
        st = skills.scan(self.store, "claude", home=self.home)
        self.assertEqual(st["found"], 2)
        self.assertEqual(st["agents"], ["any", "claude"])
        hits = skills.find(self.store, "fill form")
        self.assertEqual([h["name"] for h in hits], ["pdf-fill"])  # "form" must not match "format"
        self.assertEqual(hits[0]["agents"], ["any"])

    def test_empty_registry_hint(self):
        self.assertIn("run skills(action=scan) first", self.session().skills("find", query="anything"))


if __name__ == "__main__":
    unittest.main()
