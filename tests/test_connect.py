"""`memdex connect`: write the MCP entry into the agents installed on this machine.

The rules the tests pin down: only touch a client whose config directory exists, merge one key and keep
everything else, back the file up, and never rewrite a file that does not parse.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import connect
from memdex.cli import main


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        # The command is resolved to an absolute path (GUI clients often have a shorter PATH), so the
        # expected entry is built the same way rather than hard-coded.
        self.entry = connect.build_entry("qa")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, slug="cursor", filename="mcp.json", fmt="json", make_dir=True):
        d = os.path.join(self.home, slug)
        if make_dir:
            os.makedirs(d, exist_ok=True)
        return connect.Client(slug, slug.title(), os.path.join(d, filename), fmt=fmt)

    def run_connect(self, targets, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = connect.run(targets=targets, **kw)
        return code, buf.getvalue()


class JsonMergeTests(Base):
    def test_creates_the_entry_and_keeps_other_settings(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}),
                                encoding="utf-8")
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 0)
        self.assertIn("updated", out)
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["mcpServers"]["memdex"], self.entry)

    def test_creates_the_file_when_only_the_directory_exists(self):
        c = self.client()
        code, _ = self.run_connect([c], agent="qa")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(c.path))
        self.assertEqual(json.loads(Path(c.path).read_text())["mcpServers"]["memdex"], self.entry)

    def test_second_run_changes_nothing(self):
        c = self.client()
        self.run_connect([c], agent="qa")
        before = Path(c.path).read_text(encoding="utf-8")
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 0)
        self.assertIn("already configured", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), before)
        self.assertFalse(os.path.exists(c.path + ".bak"))

    def test_key_order_does_not_count_as_a_change(self):
        c = self.client()
        same = {"env": self.entry.get("env", {}), "args": self.entry["args"], "command": self.entry["command"]}
        Path(c.path).write_text(json.dumps({"mcpServers": {"memdex": same}}), encoding="utf-8")
        before = Path(c.path).read_text(encoding="utf-8")
        _, out = self.run_connect([c], agent="qa")
        self.assertIn("already configured", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), before)

    def test_a_differing_entry_is_replaced_and_backed_up(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"mcpServers": {"memdex": {"command": "old"}}}), encoding="utf-8")
        _, out = self.run_connect([c], agent="qa")
        self.assertIn("replaced", out)
        self.assertEqual(json.loads(Path(c.path).read_text())["mcpServers"]["memdex"], self.entry)
        self.assertEqual(json.loads(Path(c.path + ".bak").read_text())["mcpServers"]["memdex"],
                         {"command": "old"})

    def test_unparseable_json_is_left_alone(self):
        c = self.client()
        Path(c.path).write_text('{"mcpServers": {,}}', encoding="utf-8")
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 1)
        self.assertIn("skipped", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), '{"mcpServers": {,}}')
        self.assertFalse(os.path.exists(c.path + ".bak"))

    def test_a_json_array_is_left_alone(self):
        c = self.client()
        Path(c.path).write_text("[1, 2]", encoding="utf-8")
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 1)
        self.assertIn("not a JSON object", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), "[1, 2]")

    def test_remove_drops_only_memdex(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        self.run_connect([c], agent="qa")
        code, out = self.run_connect([c], remove=True)
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"], {"other": {"command": "x"}})

    def test_remove_on_a_file_without_memdex_is_a_no_op(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        code, out = self.run_connect([c], remove=True)
        self.assertEqual(code, 0)
        self.assertIn("no memdex entry", out)


class CodexTomlTests(Base):
    def test_appends_one_table_and_keeps_the_rest(self):
        c = self.client("codex", "config.toml", fmt="toml")
        Path(c.path).write_text('model = "gpt-5"\n', encoding="utf-8")
        _, out = self.run_connect([c], agent="qa")
        self.assertIn("updated", out)
        text = Path(c.path).read_text(encoding="utf-8")
        self.assertTrue(text.startswith('model = "gpt-5"\n'))
        self.assertEqual(text.count("[mcp_servers.memdex]"), 1)
        self.assertIn(f"command = {json.dumps(self.entry['command'])}", text)
        self.assertIn('env = { MEMDEX_AGENT = "qa" }', text)

    def test_appends_only_once(self):
        c = self.client("codex", "config.toml", fmt="toml")
        self.run_connect([c], agent="qa")
        _, out = self.run_connect([c], agent="qa")
        self.assertIn("already configured", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8").count("[mcp_servers.memdex]"), 1)

    def test_remove_cuts_the_table_and_keeps_the_next_one(self):
        c = self.client("codex", "config.toml", fmt="toml")
        Path(c.path).write_text('model = "gpt-5"\n\n[mcp_servers.memdex]\ncommand = "memdex"\n'
                                'args = ["serve"]\n\n[profiles.fast]\nmodel = "gpt-5-mini"\n',
                                encoding="utf-8")
        code, _ = self.run_connect([c], remove=True)
        self.assertEqual(code, 0)
        text = Path(c.path).read_text(encoding="utf-8")
        self.assertNotIn("memdex", text)
        self.assertIn("[profiles.fast]", text)
        self.assertIn('model = "gpt-5"', text)


class SelectionTests(Base):
    def test_a_client_that_is_not_installed_is_not_created(self):
        c = self.client("windsurf", make_dir=False)
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)
        self.assertFalse(os.path.exists(c.path))

    def test_only_configures_the_named_client(self):
        cursor = self.client("cursor")
        codex = self.client("codex", "config.toml", fmt="toml")
        _, out = self.run_connect([cursor, codex], agent="qa", only="codex")
        self.assertIn("codex", out)
        self.assertFalse(os.path.exists(cursor.path))
        self.assertTrue(os.path.exists(codex.path))

    def test_an_unknown_client_is_reported(self):
        c = self.client("cursor")
        code, out = self.run_connect([c], agent="qa", only="nosuch")
        self.assertIn("unknown client", out)
        self.assertFalse(os.path.exists(c.path))

    def test_dry_run_writes_nothing(self):
        c = self.client()
        code, out = self.run_connect([c], agent="qa", dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertFalse(os.path.exists(c.path))

    def test_agent_name_lands_in_the_env(self):
        self.assertEqual(connect.build_entry("claude")["env"], {"MEMDEX_AGENT": "claude"})
        self.assertNotIn("env", connect.build_entry(""))


class VsCodeTests(Base):
    def client(self, slug="vscode", filename="mcp.json", fmt="json", make_dir=True):
        d = os.path.join(self.home, "Code", "User")
        if make_dir:
            os.makedirs(d, exist_ok=True)
        return connect.Client("vscode", "VS Code / Copilot", os.path.join(d, filename),
                              container="servers", extra={"type": "stdio"})

    def test_the_entry_goes_under_servers_with_a_type(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"servers": {"other": {"type": "stdio", "command": "x"}}}),
                                encoding="utf-8")
        self.run_connect([c], agent="qa")
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        entry = data["servers"]["memdex"]
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["command"], self.entry["command"])
        self.assertEqual(entry["env"], {"MEMDEX_AGENT": "qa"})
        self.assertEqual(data["servers"]["other"], {"type": "stdio", "command": "x"})

    def test_no_mcpservers_key_is_invented(self):
        c = self.client()
        self.run_connect([c], agent="qa")
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        self.assertNotIn("mcpServers", data)

    def test_an_entry_without_the_type_is_replaced(self):
        c = self.client()
        Path(c.path).write_text(json.dumps({"servers": {"memdex": {"command": "memdex", "args": ["serve"]}}}),
                                encoding="utf-8")
        _, out = self.run_connect([c], agent="qa")
        self.assertIn("replaced", out)
        self.assertIn("type", json.loads(Path(c.path).read_text(encoding="utf-8"))["servers"]["memdex"])


class InspectTests(Base):
    def test_states_without_writing(self):
        entry = connect.build_entry("qa")
        absent = connect.Client("ghost", "Ghost", os.path.join(self.home, "nope", "x.json"))
        self.assertEqual(connect.inspect(absent, entry)[0], "absent")

        cursor = self.client()
        self.assertEqual(connect.inspect(cursor, entry)[0], "missing")
        self.run_connect([cursor], agent="qa")
        self.assertEqual(connect.inspect(cursor, entry)[0], "configured")
        self.assertEqual(connect.inspect(cursor, connect.build_entry("other"))[0], "different")
        # doctor only asks whether something is there, because it cannot know the other session's name.
        self.assertEqual(connect.inspect(cursor, connect.build_entry("other"), exact=False)[0], "configured")

    def test_list_reports_every_client(self):
        cursor = self.client()
        absent = connect.Client("ghost", "Ghost", os.path.join(self.home, "nope", "x.json"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = connect.list_clients(agent="qa", targets=[cursor, absent])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("missing", out)
        self.assertIn("not installed", out)
        self.assertFalse(os.path.exists(cursor.path))


class DoctorTests(Base):
    def check(self, clients, label="clients"):
        from memdex import doctor
        original = connect.clients
        connect.clients = lambda: clients
        try:
            rep = doctor.run(path=self.home, db_path=os.path.join(self.tmp, "d.db"), offline=True)
        finally:
            connect.clients = original
        return [c for c in rep.checks if c.label == label][0]

    def test_missing_entry_is_a_warning_that_names_the_client(self):
        c = self.check([self.client()])
        self.assertEqual(c.status, "warn")
        self.assertIn("cursor", c.detail)

    def test_configured_client_is_reported_as_ok(self):
        cursor = self.client()
        self.run_connect([cursor], agent="qa")
        c = self.check([cursor])
        self.assertEqual(c.status, "ok")
        self.assertIn("cursor", c.detail)

    def test_no_installed_client_is_not_a_problem(self):
        c = self.check([connect.Client("ghost", "Ghost", os.path.join(self.home, "nope", "x.json"))])
        self.assertEqual(c.status, "ok")


class CliTests(Base):
    def test_the_subcommand_is_wired_up(self):
        c = self.client()
        original = connect.clients
        connect.clients = lambda: [c]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["connect", "--agent", "qa"])
        finally:
            connect.clients = original
        self.assertEqual(code, 0)
        self.assertIn("cursor", buf.getvalue())
        self.assertTrue(os.path.exists(c.path))

    def test_config_still_prints_snippets(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["config", "--agent", "qa"])
        self.assertIn("mcpServers", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
