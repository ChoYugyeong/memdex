"""The MCP server speaks two eras of the protocol from one process.

2026-07-28 is stateless: no handshake, every request carries its protocol version and client capabilities
in `_meta`, the server is found with `server/discover`, and every result says what kind it is. Clients
from 2025-11-25 and earlier still open with `initialize` and must get exactly the answers they always
got. These tests hold both, and the rule that nothing is remembered from one request to the next.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memdex import __version__, mcp
from memdex.store import Store
from memdex.tools import Session, tool_profile

REPO = str(Path(__file__).resolve().parent.parent)
VERSION = "io.modelcontextprotocol/protocolVersion"
CLIENT = "io.modelcontextprotocol/clientInfo"
CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
SERVER = "io.modelcontextprotocol/serverInfo"


def meta(version="2026-07-28", client="ExampleClient", **extra):
    out = {VERSION: version, CAPABILITIES: {}, **extra}
    if client:
        out[CLIENT] = {"name": client, "version": "1.0.0"}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        Path(self.root, "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.session = self.new_session()
        self.tools = {t.name: t for t in self.session.tools()}
        self.ids = 0
        saved = os.environ.pop("MEMDEX_AGENT", None)  # the client's name is only a fallback for this
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "MEMDEX_AGENT", saved)

    def new_session(self):
        session = Session(self.store, agent_name="")
        session.cwd = self.root
        return session

    def ask(self, method, params=None, **meta_args):
        """A 2026-07-28 request: the method's own params plus the per-request `_meta`."""
        self.ids += 1
        return mcp.handle(self.session, self.tools, {
            "jsonrpc": "2.0", "id": self.ids, "method": method,
            "params": {**(params or {}), "_meta": meta(**meta_args)}})

    def old(self, method, params=None):
        """A request the way a 2025-11-25 client sends it: no protocol `_meta` at all."""
        self.ids += 1
        msg = {"jsonrpc": "2.0", "id": self.ids, "method": method}
        if params is not None:
            msg["params"] = params
        return mcp.handle(self.session, self.tools, msg)


class DiscoverTests(Base):
    def test_discover_reports_versions_capabilities_and_identity(self):
        result = self.ask("server/discover")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"][0], "2026-07-28")
        self.assertEqual(result["supportedVersions"], list(mcp.SUPPORTED_VERSIONS))
        self.assertIn("tools", result["capabilities"])
        self.assertIn("project_brief", result["instructions"])
        self.assertEqual(result["_meta"][SERVER], {"name": "memdex", "version": __version__})

    def test_discover_carries_the_caching_hints(self):
        result = self.ask("server/discover")["result"]
        self.assertIsInstance(result["ttlMs"], int)
        self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "public")

    def test_discover_follows_the_tool_profile_like_initialize_does(self):
        self.session.listed, self.session.full_schemas = tool_profile("")
        modern = self.ask("server/discover")["result"]["instructions"]
        legacy = self.old("initialize", {"protocolVersion": "2025-11-25"})["result"]["instructions"]
        self.assertEqual(modern, legacy)
        self.assertNotIn("file_summary", modern)

    def test_discover_without_the_required_meta_is_invalid_params(self):
        bare = mcp.handle(self.session, self.tools, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(bare["error"]["code"], -32602)
        self.assertIn(VERSION, bare["error"]["message"])


class VersionTests(Base):
    def test_an_unknown_version_gets_the_error_the_specification_defines(self):
        for method in ("server/discover", "tools/list", "tools/call"):
            error = self.ask(method, version="1900-01-01")["error"]
            self.assertEqual(error["code"], -32022, method)
            self.assertEqual(error["message"], "Unsupported protocol version")
            self.assertEqual(error["data"], {"supported": list(mcp.SUPPORTED_VERSIONS),
                                             "requested": "1900-01-01"})

    def test_a_later_revision_this_server_has_not_met_is_refused_not_guessed_at(self):
        error = self.ask("tools/list", version="2027-01-01")["error"]
        self.assertEqual(error["code"], -32022)
        self.assertIn("2026-07-28", error["data"]["supported"])

    def test_a_missing_version_is_invalid_params(self):
        resp = mcp.handle(self.session, self.tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                     "params": {"_meta": {CAPABILITIES: {}}}})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn(VERSION, resp["error"]["message"])

    def test_a_version_that_is_not_a_string_is_invalid_params(self):
        resp = mcp.handle(self.session, self.tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                     "params": {"_meta": {VERSION: 20260728, CAPABILITIES: {}}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_missing_client_capabilities_is_invalid_params(self):
        for value in ({VERSION: "2026-07-28"}, {VERSION: "2026-07-28", CAPABILITIES: None},
                      {VERSION: "2026-07-28", CAPABILITIES: "all"}):
            resp = mcp.handle(self.session, self.tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                         "params": {"_meta": value}})
            self.assertEqual(resp["error"]["code"], -32602, value)
            self.assertIn(CAPABILITIES, resp["error"]["message"])

    def test_the_version_is_checked_before_anything_else_in_the_request(self):
        # A client that learns the version is wrong can retry; one told only "invalid params" cannot.
        resp = mcp.handle(self.session, self.tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                     "params": {"_meta": {VERSION: "1900-01-01"}}})
        self.assertEqual(resp["error"]["code"], -32022)

    def test_every_supported_version_is_accepted_when_a_request_names_it(self):
        for version in mcp.SUPPORTED_VERSIONS:
            self.assertIn("result", self.ask("tools/list", version=version), version)

    def test_a_handshake_revision_named_per_request_is_answered_in_its_own_shape(self):
        result = self.ask("tools/list", version="2025-11-25")["result"]
        self.assertEqual(list(result), ["tools"])
        self.assertEqual(self.ask("ping", version="2025-06-18"), {"result": {}})

    def test_notifications_get_no_answer_in_either_era(self):
        for params in ({}, {"_meta": meta()}, {"_meta": meta(version="1900-01-01")}):
            self.assertIsNone(mcp.handle(self.session, self.tools,
                                         {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                          "params": {"requestId": 1, **params}}))


class ModernToolsTests(Base):
    def test_tools_list_is_a_complete_cacheable_result(self):
        result = self.ask("tools/list")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIsInstance(result["ttlMs"], int)
        self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertIn(result["cacheScope"], ("public", "private"))
        self.assertNotIn("nextCursor", result)  # one page
        self.assertEqual(result["_meta"][SERVER]["name"], "memdex")
        self.assertEqual(result["tools"], self.old("tools/list")["result"]["tools"])

    def test_a_cursor_this_server_never_issued_is_invalid_params(self):
        self.assertEqual(self.ask("tools/list", {"cursor": "page-2"})["error"]["code"], -32602)
        self.assertIn("result", self.ask("tools/list", {"cursor": None}))

    def test_tools_call_is_a_complete_result(self):
        result = self.ask("tools/call", {"name": "remember", "arguments": {
            "kind": "note", "title": "stateless", "body": "no handshake"}})["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertFalse(result["isError"])
        self.assertIn("saved", result["content"][0]["text"])
        self.assertEqual(result["_meta"][SERVER]["version"], __version__)
        self.assertNotIn("ttlMs", result)  # only discovery and lists are cacheable

    def test_a_tool_that_fails_is_still_a_result_the_model_can_read(self):
        missing = self.ask("tools/call", {"name": "remember", "arguments": {}})["result"]
        self.assertEqual(missing["resultType"], "complete")
        self.assertTrue(missing["isError"])
        self.assertIn("missing required argument", missing["content"][0]["text"])

    def test_an_unknown_tool_is_a_protocol_error(self):
        for name in ("no_such_tool", None, 7):
            error = self.ask("tools/call", {"name": name})["error"]
            self.assertEqual(error["code"], -32602, name)
            self.assertIn("Unknown tool", error["message"])

    def test_arguments_that_are_not_an_object_are_a_protocol_error(self):
        error = self.ask("tools/call", {"name": "remember", "arguments": ["note", "x"]})["error"]
        self.assertEqual(error["code"], -32602)

    def test_an_unlisted_tool_stays_callable(self):
        self.session.listed, self.session.full_schemas = tool_profile("")
        listed = {t["name"] for t in self.ask("tools/list")["result"]["tools"]}
        self.assertNotIn("file_summary", listed)
        result = self.ask("tools/call", {"name": "file_summary", "arguments": {"path": "main.py"}})["result"]
        self.assertEqual(result["resultType"], "complete")

    def test_host_metadata_rides_along_with_the_protocol_keys(self):
        self.session.agent_name = "claude"
        result = self.ask("tools/call", {"name": "project_brief", "arguments": {}},
                          **{"memdex.dev/conversation-id": "chat-1"})
        self.assertFalse(result["result"]["isError"])
        bad = self.ask("tools/call", {"name": "project_brief", "arguments": {}},
                       **{"memdex.dev/conversation-id": "bad\nid"})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_methods_the_revision_removed_are_not_found(self):
        for method in ("ping", "initialize", "logging/setLevel"):
            self.assertEqual(self.ask(method)["error"]["code"], -32601, method)

    def test_memdex_own_methods_also_say_what_kind_of_result_they_are(self):
        self.store.add_note(self.session.project, "decision", "lock files", "claim first", "claude@x")
        listing = self.ask("artifacts/list")["result"]
        self.assertEqual(listing["resultType"], "complete")
        self.assertEqual(listing["artifacts"][0]["uri"], "memdex://note/1")
        read = self.ask("artifacts/read", {"uri": "memdex://note/1"})["result"]
        self.assertEqual(read["resultType"], "complete")
        self.assertEqual(read["content"]["text"], "claim first")


class ToolOrderTests(Base):
    def names(self, resp):
        return [t["name"] for t in resp["result"]["tools"]]

    def test_the_order_is_the_same_on_every_request(self):
        first = self.ask("tools/list")["result"]["tools"]
        self.ask("tools/call", {"name": "remember", "arguments": {"kind": "note", "title": "t", "body": "b"}})
        self.ask("tools/call", {"name": "project_brief", "arguments": {}})
        self.assertEqual(self.ask("tools/list")["result"]["tools"], first)
        self.assertEqual(self.old("tools/list")["result"]["tools"], first)

    def test_the_order_is_the_same_in_another_process_lifetime(self):
        first = json.dumps(self.ask("tools/list")["result"]["tools"])
        self.session = self.new_session()
        self.tools = {t.name: t for t in self.session.tools()}
        self.assertEqual(json.dumps(self.ask("tools/list")["result"]["tools"]), first)

    def test_a_profile_removes_tools_without_reordering_the_rest(self):
        everything = self.names(self.ask("tools/list"))
        for profile in ("", "code", "review,skills", "skills,review"):
            self.session.listed, self.session.full_schemas = tool_profile(profile)
            listed = self.names(self.ask("tools/list"))
            self.assertEqual(listed, [n for n in everything if n in listed], profile)
        self.session.listed, _ = tool_profile("review,skills")
        one = self.names(self.ask("tools/list"))
        self.session.listed, _ = tool_profile("skills,review")
        self.assertEqual(self.names(self.ask("tools/list")), one)


class StatelessTests(Base):
    def brief(self, **meta_args):
        return self.ask("tools/call", {"name": "project_brief", "arguments": {}},
                        **meta_args)["result"]["content"][0]["text"]

    def test_the_client_is_named_by_each_request_not_by_an_earlier_one(self):
        self.assertIn("you are exampleclient@", self.brief())
        self.assertEqual(self.session.agent_name, "")
        self.assertIn("you are agent@", self.brief(client=""))
        self.assertIn("you are deepseek@", self.brief(client="dsh-mcp-client"))
        self.assertIn("you are codex@", self.brief(client="codex cli"))

    def test_a_malformed_client_name_is_ignored_not_fatal(self):
        for info in ("ExampleClient", {"name": 7}, {"name": "   "}, {}, None):
            resp = mcp.handle(self.session, self.tools, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "project_brief", "arguments": {},
                           "_meta": {VERSION: "2026-07-28", CAPABILITIES: {}, CLIENT: info}}})
            self.assertIn("you are agent@", resp["result"]["content"][0]["text"], info)

    def test_a_configured_agent_wins_over_the_client_name(self):
        os.environ["MEMDEX_AGENT"] = "claude"
        self.addCleanup(os.environ.pop, "MEMDEX_AGENT", None)
        self.assertIn("you are claude@", self.brief())

    def test_the_version_is_not_remembered_between_requests(self):
        self.assertIn("resultType", self.ask("tools/list")["result"])
        self.assertEqual(list(self.old("tools/list")["result"]), ["tools"])
        self.assertEqual(self.ask("tools/list", version="1900-01-01")["error"]["code"], -32022)
        self.assertIn("resultType", self.ask("tools/list")["result"])

    def test_one_process_serves_a_handshake_client_and_a_stateless_one(self):
        init = self.old("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "codex"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.old("ping"), {"result": {}})
        modern = self.ask("tools/list")["result"]
        self.assertEqual(modern["resultType"], "complete")
        self.assertEqual(self.ask("ping")["error"]["code"], -32601)
        self.assertEqual(list(self.old("tools/list")["result"]), ["tools"])


class LegacyTests(Base):
    """What a client from 2025-11-25 or earlier sees must not change."""

    def test_initialize_answers_with_the_client_version_when_it_is_supported(self):
        for version in mcp.LEGACY_VERSIONS:
            result = self.old("initialize", {"protocolVersion": version, "capabilities": {},
                                             "clientInfo": {"name": "codex", "version": "1"}})["result"]
            self.assertEqual(result["protocolVersion"], version)

    def test_initialize_answers_an_unsupported_version_with_the_newest_handshake_one(self):
        for version in ("1900-01-01", "2027-01-01", "2026-07-28", None, 5, ["2025-11-25"]):
            result = self.old("initialize", {"protocolVersion": version})["result"]
            self.assertEqual(result["protocolVersion"], "2025-11-25", version)
        self.assertEqual(self.old("initialize")["result"]["protocolVersion"], "2025-11-25")

    def test_initialize_keeps_its_shape(self):
        result = self.old("initialize", {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "claude code"}})["result"]
        self.assertEqual(sorted(result), ["capabilities", "instructions", "protocolVersion", "serverInfo"])
        self.assertEqual(result["serverInfo"], {"name": "memdex", "version": __version__})
        self.assertEqual(result["capabilities"]["tools"], {"listChanged": False})
        self.assertEqual(self.session.agent_name, "claude")

    def test_initialize_survives_a_malformed_client_info(self):
        for info in ("codex", {"name": 7}, {"name": "  "}, []):
            self.assertIn("result", self.old("initialize", {"clientInfo": info}), info)
        self.assertEqual(self.session.agent_name, "")

    def test_results_carry_none_of_the_new_fields(self):
        self.assertEqual(list(self.old("tools/list")["result"]), ["tools"])
        self.assertEqual(list(self.old("tools/list", {"cursor": "ignored"})["result"]), ["tools"])
        called = self.old("tools/call", {"name": "remember", "arguments": {
            "kind": "note", "title": "t", "body": "b"}})["result"]
        self.assertEqual(sorted(called), ["content", "isError"])
        self.assertEqual(self.old("ping"), {"result": {}})

    def test_an_unknown_tool_is_still_a_tool_result(self):
        result = self.old("tools/call", {"name": "no_such_tool"})["result"]
        self.assertTrue(result["isError"])

    def test_metadata_that_is_not_the_protocol_s_does_not_change_the_era(self):
        self.session.agent_name = "claude"
        self.ids += 1
        resp = mcp.handle(self.session, self.tools, {
            "jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
            "params": {"name": "project_brief", "arguments": {},
                       "_meta": {"memdex.dev/conversation-id": "chat-1", "progressToken": 3}}})
        self.assertEqual(sorted(resp["result"]), ["content", "isError"])

    def test_params_that_are_not_an_object_do_not_crash_the_server(self):
        self.ids += 1
        resp = mcp.handle(self.session, self.tools,
                          {"jsonrpc": "2.0", "id": self.ids, "method": "tools/list", "params": ["x"]})
        self.assertIn("tools", resp["result"])


class WireTests(unittest.TestCase):
    """The real server over stdio: one line in, one line out, both eras in one process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MEMDEX_")}
        self.env.update(MEMDEX_HOME=self.tmp, MEMDEX_PROJECT=self.tmp, PYTHONPATH=REPO, PYTHONIOENCODING="utf-8")

    def talk(self, messages):
        proc = subprocess.run([sys.executable, "-m", "memdex", "serve"], cwd=self.tmp, env=self.env,
                              input="".join(json.dumps(m) + "\n" for m in messages).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]

    def test_a_stateless_client_and_a_handshake_client_share_one_process(self):
        m = meta()
        replies = self.talk([
            {"jsonrpc": "2.0", "id": "discover-1", "method": "server/discover", "params": {"_meta": m}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": m}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "remember", "arguments": {"kind": "note", "title": "wire", "body": "b"}, "_meta": m}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
             "params": {"_meta": meta(version="1900-01-01")}},
            {"jsonrpc": "2.0", "id": 5, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "old-client", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        ])
        self.assertEqual([r["id"] for r in replies], ["discover-1", 2, 3, 4, 5, 6, 7])
        discover, listed, called, refused, init, ping, old_list = replies
        self.assertEqual(discover["result"]["resultType"], "complete")
        self.assertIn("2026-07-28", discover["result"]["supportedVersions"])
        self.assertEqual(listed["result"]["cacheScope"], "public")
        self.assertEqual(called["result"]["resultType"], "complete")
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(refused["error"]["code"], -32022)
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(ping["result"], {})
        self.assertEqual(list(old_list["result"]), ["tools"])
        self.assertEqual(old_list["result"]["tools"], listed["result"]["tools"])

    def test_json_that_is_not_a_message_gets_an_error_and_the_server_lives_on(self):
        proc = subprocess.run([sys.executable, "-m", "memdex", "serve"], cwd=self.tmp, env=self.env,
                              input=b'5\n"hello"\n{not json\n'
                                    + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                                                  "params": {"_meta": meta()}}).encode("utf-8") + b"\n",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies], [-32600, -32600, -32700, None])
        self.assertEqual(replies[-1]["result"]["resultType"], "complete")


if __name__ == "__main__":
    unittest.main()
