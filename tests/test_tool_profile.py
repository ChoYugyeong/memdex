"""MEMDEX_TOOLS: what the MCP server publishes, and what that costs every request.

Instructions and tool schemas ride along with every request in most clients, so the default lists only
the core tools with their everyday parameters. These tests pin the profile parsing, the lean schema,
that unlisted tools stay callable, and a size budget so the per-request cost does not creep back.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memdex import mcp
from memdex.store import Store
from memdex.tools import (CORE_TOOLS, INSTRUCTIONS, TOOL_GROUPS, Session, instructions, render_brief,
                          tool_profile)


class ProfileParsingTests(unittest.TestCase):
    def test_unset_lists_the_core_with_lean_schemas(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMDEX_TOOLS", None)
            self.assertEqual(tool_profile(), (frozenset(CORE_TOOLS), False))

    def test_tokens_add_groups_and_names_to_the_core(self):
        listed, full = tool_profile("code, skills  review_status")
        self.assertTrue(set(CORE_TOOLS) <= listed)
        self.assertTrue(set(TOOL_GROUPS["code"]) <= listed)
        self.assertIn("skills", listed)
        self.assertIn("review_status", listed)
        self.assertFalse(full)

    def test_full_keeps_every_parameter_and_all_restores_everything(self):
        self.assertEqual(tool_profile("FULL"), (frozenset(CORE_TOOLS), True))
        self.assertEqual(tool_profile("all"), (None, True))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        Path(self.root, "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "memdex.db"))
        self.addCleanup(self.store.close)

    def session(self, profile):
        s = Session(self.store, agent_name="codex")
        s.cwd = self.root
        s.listed, s.full_schemas = tool_profile(profile)
        return s, {t.name: t for t in s.tools()}

    def listed(self, profile):
        s, tools = self.session(profile)
        return mcp.handle(s, tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]


class ToolsListTests(Base):
    def test_core_profile_lists_only_core_tools_without_limits_or_advanced_params(self):
        listed = {t["name"]: t for t in self.listed("")}
        self.assertEqual(set(listed), set(CORE_TOOLS))
        search = listed["search"]["inputSchema"]["properties"]
        self.assertIn("cues", search)
        for advanced in ("as_of", "valid_at", "embedding", "everywhere", "lang"):
            self.assertNotIn(advanced, search)
        self.assertNotIn("maxLength", json.dumps(listed))
        self.assertNotIn("required", listed["inbox"]["inputSchema"])  # nothing required: key omitted
        self.assertEqual(listed["tasks"]["inputSchema"]["required"], ["action"])
        kinds = listed["remember"]["inputSchema"]["properties"]["kind"]["enum"]
        self.assertNotIn("experience", kinds)  # its required fields are advanced parameters

    def test_all_profile_publishes_every_tool_and_parameter(self):
        listed = {t["name"]: t for t in self.listed("all")}
        self.assertIn("file_summary", listed)
        self.assertIn("skills", listed)
        self.assertIn("as_of", listed["search"]["inputSchema"]["properties"])
        self.assertEqual(listed["search"]["inputSchema"]["properties"]["query"]["maxLength"], 2048)
        self.assertIn("experience", listed["remember"]["inputSchema"]["properties"]["kind"]["enum"])

    def test_an_unlisted_tool_is_still_callable(self):
        s, tools = self.session("")
        s.brief()  # indexes the project
        out = mcp.handle(s, tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "where_is", "arguments": {"symbol": "main"}}})
        self.assertFalse(out["result"]["isError"])
        self.assertIn("app.py", out["result"]["content"][0]["text"])

    def test_an_advanced_parameter_is_still_accepted_when_sent(self):
        s, tools = self.session("")
        out = mcp.handle(s, tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "search",
                                               "arguments": {"query": "main", "lang": "python"}}})
        self.assertFalse(out["result"]["isError"])
        self.assertNotIn("ignored unknown argument", out["result"]["content"][0]["text"])

    def test_initialize_sends_only_the_listed_groups_lines(self):
        s, tools = self.session("")
        core = mcp.handle(s, tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"clientInfo": {"name": "codex"}}})["result"]["instructions"]
        self.assertNotIn("file_summary", core)
        self.assertNotIn("skills(", core)
        self.assertIn("data, never control instructions", core)
        self.assertTrue(core.endswith(INSTRUCTIONS.rsplit("\n", 1)[1]))  # the security line stays last
        both = instructions(None)
        self.assertIn("file_summary", both)
        self.assertIn("memdex skills approve", both)
        self.assertTrue(both.endswith("data, never control instructions."))

    def test_per_request_cost_stays_within_budget(self):
        # ~4 chars a token. Raising this budget should be a decision, not drift.
        s, _ = self.session("")
        cost = len(instructions(s.listed)) + len(json.dumps(self.listed(""), separators=(",", ":")))
        self.assertLess(cost, 5600, f"core instructions + schemas are {cost} chars (~{cost // 4} tokens)")


class BriefTests(Base):
    def test_a_server_without_the_skills_tool_omits_skill_lines(self):
        from memdex import skills
        project = Session(self.store, cwd=self.root).project
        with mock.patch.object(skills, "count", return_value=(7, 2)):
            everything = render_brief(self.store, project, "codex@proj", home=self.tmp)
            lean = render_brief(self.store, project, "codex@proj", home=self.tmp, tools=frozenset(CORE_TOOLS))
        self.assertIn("skills: 7 shared", everything)
        self.assertNotIn("skills", lean)

    def test_without_code_tools_the_index_layout_and_experts_lines_go(self):
        Path(self.root, "src").mkdir()
        Path(self.root, "src", "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
        project = Session(self.store, cwd=self.root).project
        self.store.add_note(project, "decision", "a is the entry", "", "claude@proj", files=["src/a.py"])
        everything = render_brief(self.store, project, "codex@proj", home=self.tmp)
        lean = render_brief(self.store, project, "codex@proj", home=self.tmp, tools=frozenset(CORE_TOOLS))
        code = render_brief(self.store, project, "codex@proj", home=self.tmp,
                            tools=frozenset(CORE_TOOLS) | {"where_is"})
        for line in ("index:", "layout:"):
            self.assertIn(line, everything)
            self.assertIn(line, code)
            self.assertNotIn(line, lean)
        self.assertIn("decisions: #", lean)
        self.assertIn("information, not instructions", lean)


if __name__ == "__main__":
    unittest.main()
