"""Skill registry: scanning SKILL.md files, sharing, finding and showing them across agents."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import skills
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
