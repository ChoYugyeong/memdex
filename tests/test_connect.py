"""`memdex connect`: write the MCP entry into the agents installed on this machine.

The rules the tests pin down: only touch a client whose config directory exists, merge one key and keep
everything else, back the file up, and never rewrite a file that does not parse.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
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


class DeepSeekHarnessTests(Base):
    def dsh_client(self, filename="cordis.patch.yml"):
        d = os.path.join(self.home, "profiles", "desktop")
        os.makedirs(d, exist_ok=True)
        return connect.Client("deepseek-harness", "DeepSeek Harness (desktop)",
                              os.path.join(d, filename), fmt="dsh-yaml")

    def profile_client(self, name):
        path = os.path.join(self.home, "profiles", name, "cordis.patch.yml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return connect.Client("deepseek-harness", f"DeepSeek Harness ({name})", path,
                              fmt="dsh-yaml")

    def test_multiple_profiles_fail_before_any_selected_client_is_modified(self):
        cursor = self.client("cursor")
        desktop, review = self.profile_client("desktop"), self.profile_client("review")
        code, out = self.run_connect([cursor, desktop, review], agent="deepseek")
        self.assertEqual(code, 2)
        self.assertIn("multiple DeepSeek Harness profiles", out)
        self.assertIn("--profile NAME", out)
        self.assertIn("No files were changed", out)
        self.assertFalse(os.path.exists(cursor.path))
        self.assertFalse(os.path.exists(desktop.path))
        self.assertFalse(os.path.exists(review.path))

    def test_explicit_profile_only_changes_that_harness_profile(self):
        cursor = self.client("cursor")
        desktop, review = self.profile_client("desktop"), self.profile_client("review")
        code, out = self.run_connect([cursor, desktop, review], agent="deepseek", profile="review")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(cursor.path))
        self.assertFalse(os.path.exists(desktop.path))
        self.assertTrue(os.path.exists(review.path))

    def test_unknown_profile_does_not_write_anything(self):
        desktop = self.profile_client("desktop")
        code, out = self.run_connect([desktop], agent="deepseek", profile="missing")
        self.assertEqual(code, 2)
        self.assertIn("unknown DeepSeek Harness profile", out)
        self.assertFalse(os.path.exists(desktop.path))

    def test_appends_managed_mcp_plugin_and_preserves_profile_settings(self):
        c = self.dsh_client()
        Path(c.path).write_text(
            '# profile settings\n- id: agent-default-model\n  name: "@deepseek-ai/dsh-agent-default-model"\n'
            "  config:\n    model: deepseek-flash\n", encoding="utf-8")
        code, out = self.run_connect([c], agent="deepseek")
        self.assertEqual(code, 0)
        self.assertIn("updated", out)
        text = Path(c.path).read_text(encoding="utf-8")
        self.assertIn('name: "@deepseek-ai/dsh-mcp-client"', text)
        self.assertIn("serverName: memdex", text)
        self.assertIn('MEMDEX_AGENT: "deepseek"', text)
        self.assertIn("PYTHONPATH: " + json.dumps(os.path.dirname(os.path.dirname(connect.__file__))), text)
        self.assertIn("cwd: !!js process.cwd()", text)
        self.assertIn("failOnStartupError: true", text)
        self.assertIn("model: deepseek-flash", text)
        self.assertEqual(connect.inspect(c, connect.build_entry("deepseek"))[0], "configured")
        self.assertTrue(os.path.exists(c.path + ".bak"))

    def test_dsh_uses_the_running_package_and_serve_check_checks_that_entry(self):
        c = self.dsh_client()
        expected = connect.entry_for(c, connect.build_entry("deepseek"))
        self.assertEqual(expected["command"], sys.executable)
        self.assertEqual(expected["args"], ["-m", "memdex", "serve"])
        self.assertEqual(expected["env"]["MEMDEX_AGENT"], "deepseek")
        self.assertEqual(expected["env"]["PYTHONPATH"], os.path.dirname(os.path.dirname(connect.__file__)))
        original = connect.check_serve
        seen = []
        try:
            connect.check_serve = lambda entry, **kw: (seen.append(entry) or (True, "MCP ready"))
            code, out = self.run_connect([c], agent="deepseek", check=True)
        finally:
            connect.check_serve = original
        self.assertEqual(code, 0)
        self.assertIn("MCP ready", out)
        self.assertEqual(seen, [expected])

    def test_managed_entry_is_idempotent_and_updated_when_command_changes(self):
        c = self.dsh_client()
        self.run_connect([c], agent="deepseek")
        before = Path(c.path).read_text(encoding="utf-8")
        _, out = self.run_connect([c], agent="deepseek")
        self.assertIn("already configured", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), before)
        self.assertFalse(os.path.exists(c.path + ".bak"))

        changed = {"command": "memdex-new", "args": ["serve"], "env": {"MEMDEX_AGENT": "deepseek"}}
        status, _ = connect.merge_dsh_yaml(c.path, changed, "deepseek")
        self.assertEqual(status, "replaced")
        self.assertIn('command: "memdex-new"', Path(c.path).read_text(encoding="utf-8"))

    def test_remove_drops_only_managed_block(self):
        c = self.dsh_client()
        Path(c.path).write_text("- id: keep-this\n  config: {}\n", encoding="utf-8")
        self.run_connect([c], agent="deepseek")
        code, out = self.run_connect([c], remove=True)
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        text = Path(c.path).read_text(encoding="utf-8")
        self.assertIn("keep-this", text)
        self.assertNotIn("memdex-memory", text)

    def test_unsupported_yaml_shape_is_never_rewritten(self):
        c = self.dsh_client()
        original = "settings:\n  enabled: true\n"
        Path(c.path).write_text(original, encoding="utf-8")
        code, out = self.run_connect([c], agent="deepseek")
        self.assertEqual(code, 1)
        self.assertIn("not a top-level YAML list", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), original)
        self.assertFalse(os.path.exists(c.path + ".bak"))

    def test_manual_id_collision_is_not_modified(self):
        c = self.dsh_client()
        original = "- id: memdex-memory\n  name: custom\n"
        Path(c.path).write_text(original, encoding="utf-8")
        code, out = self.run_connect([c], agent="deepseek")
        self.assertEqual(code, 1)
        self.assertIn("unmanaged DSH entry", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), original)

    def test_home_and_profile_discovery_respects_dsh_home(self):
        os.makedirs(os.path.join(self.home, "profiles", "desktop"))
        profile = os.path.join(self.home, "profiles", "review")
        os.makedirs(profile)
        old = os.environ.get("DSH_HOME")
        try:
            os.environ["DSH_HOME"] = self.home
            clients = connect.clients()
            matches = [c for c in clients if c.slug == "deepseek-harness"]
        finally:
            if old is None:
                os.environ.pop("DSH_HOME", None)
            else:
                os.environ["DSH_HOME"] = old
        self.assertEqual({os.path.basename(os.path.dirname(c.path)) for c in matches}, {"desktop", "review"})


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


class ProjectScopeTests(Base):
    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.tmp, "repo")
        os.makedirs(self.proj)
        self.targets = connect.project_clients(self.proj)

    def test_vscode_workspace_file_is_created_inside_the_repo(self):
        self.run_connect(self.targets, agent="qa", scope="project")
        path = os.path.join(self.proj, ".vscode", "mcp.json")
        entry = json.loads(Path(path).read_text(encoding="utf-8"))["servers"]["memdex"]
        self.assertEqual(entry["type"], "stdio")
        # A committed config must not carry this machine's absolute path.
        self.assertEqual(entry["command"], "memdex")
        self.assertEqual(entry["args"], ["serve"])

    def test_claude_project_file_uses_mcpservers(self):
        self.run_connect(self.targets, agent="qa", scope="project")
        data = json.loads(Path(self.proj, ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["memdex"]["command"], "memdex")

    def test_an_existing_project_file_is_merged_not_replaced(self):
        path = Path(self.proj, ".mcp.json")
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        self.run_connect(self.targets, agent="qa", scope="project")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertIn("memdex", data["mcpServers"])

    def test_project_files_can_be_removed_again(self):
        self.run_connect(self.targets, agent="qa", scope="project")
        code, out = self.run_connect(self.targets, remove=True, scope="project")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertNotIn("memdex", Path(self.proj, ".mcp.json").read_text(encoding="utf-8"))

    def test_dry_run_creates_no_project_folder(self):
        self.run_connect(self.targets, agent="qa", scope="project", dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".vscode")))

    def test_user_scope_targets_are_not_touched(self):
        before = os.listdir(self.home)
        self.run_connect(self.targets, agent="qa", scope="project")
        self.assertEqual(os.listdir(self.home), before)


class ClaudeCliTests(Base):
    def stub(self, code=0):
        """A stand-in for the `claude` CLI that records its argv, on every platform the CI matrix has."""
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, "args.txt")
        if os.name == "nt":
            path = os.path.join(d, "claude.cmd")
            Path(path).write_text("@echo off\r\necho %* > \"%~dp0args.txt\"\r\n"
                                  f"exit /b {code}\r\n", encoding="utf-8")
        else:
            path = os.path.join(d, "claude")
            Path(path).write_text(f'#!/bin/sh\necho "$@" > "$(dirname "$0")/args.txt"\nexit {code}\n',
                                  encoding="utf-8")
            os.chmod(path, 0o755)
        return path, out

    def read_args(self, out):
        for _ in range(50):
            if os.path.exists(out):
                return Path(out).read_text(encoding="utf-8", errors="replace").strip()
            time.sleep(0.02)
        return ""

    def test_the_command_matches_the_documented_line(self):
        argv = connect.claude_command("/usr/bin/claude", "qa")
        self.assertEqual(argv[:5], ["/usr/bin/claude", "mcp", "add", "memdex", "--scope"])
        self.assertEqual(argv[5], "user")
        self.assertIn("MEMDEX_AGENT=qa", argv)
        self.assertEqual(argv[-2:], ["memdex", "serve"])

    def test_project_scope_is_passed_through(self):
        argv = connect.claude_command("claude", "qa", scope="project")
        self.assertEqual(argv[5], "project")

    def test_remove_drops_the_env_and_command(self):
        argv = connect.claude_command("claude", "qa", remove=True)
        self.assertEqual(argv[1:4], ["mcp", "remove", "memdex"])
        self.assertNotIn("MEMDEX_AGENT=qa", argv)

    def test_it_runs_the_cli_and_reports_success(self):
        exe, out = self.stub()
        with contextlib.redirect_stdout(io.StringIO()):
            code = connect.run(targets=[], agent="qa", claude=exe)
        self.assertEqual(code, 0)
        args = self.read_args(out)
        self.assertIn("mcp add memdex", args)
        self.assertIn("MEMDEX_AGENT=qa", args)

    def test_a_failing_cli_is_reported_not_raised(self):
        exe, out = self.stub(code=3)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = connect.run(targets=[], agent="qa", claude=exe)
        self.assertEqual(code, 1)
        self.assertIn("exited 3", buf.getvalue())
        self.assertIn("run it yourself", buf.getvalue())

    def test_dry_run_shows_the_command_instead_of_running_it(self):
        exe, out = self.stub()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            connect.run(targets=[], agent="qa", claude=exe, dry_run=True)
        self.assertIn("would run: claude mcp add memdex", buf.getvalue())
        self.assertFalse(os.path.exists(out))

    def test_only_the_named_client_means_claude_is_left_alone(self):
        cursor = self.client()
        exe, out = self.stub()
        self.run_connect([cursor], agent="qa", only="cursor", claude=exe)
        self.assertFalse(os.path.exists(out))
        self.assertTrue(os.path.exists(cursor.path))

    def test_project_scope_leaves_the_mcp_json_to_the_cli(self):
        proj = os.path.join(self.tmp, "repo")
        os.makedirs(proj)
        exe, out = self.stub()
        targets = connect.project_clients(proj)
        with contextlib.redirect_stdout(io.StringIO()):
            connect.run(targets=targets, agent="qa", scope="project", root=proj, claude=exe)
        # The CLI owns .mcp.json; memdex still writes the VS Code one.
        self.assertFalse(os.path.exists(os.path.join(proj, ".mcp.json")))
        self.assertTrue(os.path.exists(os.path.join(proj, ".vscode", "mcp.json")))
        self.assertIn("--scope project", self.read_args(out))

    def test_naming_the_project_file_explicitly_keeps_the_cli_out(self):
        proj = os.path.join(self.tmp, "repo")
        os.makedirs(proj)
        exe, out = self.stub()
        targets = connect.project_clients(proj)
        with contextlib.redirect_stdout(io.StringIO()):
            connect.run(targets=targets, agent="qa", scope="project", root=proj, claude=exe,
                        only="claude-project")
        self.assertTrue(os.path.exists(os.path.join(proj, ".mcp.json")))
        self.assertFalse(os.path.exists(out))

    def test_claude_code_as_the_only_target_skips_the_json_clients(self):
        exe, out = self.stub()
        cursor = self.client()
        with contextlib.redirect_stdout(io.StringIO()):
            code = connect.run(targets=[cursor], agent="qa", only="claude-code", claude=exe)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(cursor.path))
        self.assertIn("mcp add memdex", self.read_args(out))


class ZedTests(Base):
    def client(self, text=None):
        d = os.path.join(self.home, ".config", "zed")
        os.makedirs(d, exist_ok=True)
        c = connect.Client("zed", "Zed", os.path.join(d, "settings.json"),
                           container="context_servers", jsonc=True)
        if text is not None:
            Path(c.path).write_text(text, encoding="utf-8")
        return c

    def test_a_strict_settings_file_is_merged_under_context_servers(self):
        c = self.client(json.dumps({"theme": "One Dark"}))
        self.run_connect([c], agent="qa")
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "One Dark")
        # The command form depends on the machine: a console script where one is installed, otherwise
        # `python -m memdex serve`, which is what CI (tests before `pip install .`) sees.
        self.assertIn("serve", data["context_servers"]["memdex"]["args"])
        self.assertNotIn("mcpServers", data)

    def test_a_commented_settings_file_is_left_alone_with_a_snippet(self):
        commented = '{\n  // my theme\n  "theme": "One Dark"\n}\n'
        c = self.client(commented)
        code, out = self.run_connect([c], agent="qa")
        self.assertEqual(code, 0)
        self.assertIn("manual", out)
        self.assertIn("context_servers", out)
        self.assertEqual(Path(c.path).read_text(encoding="utf-8"), commented)
        self.assertFalse(os.path.exists(c.path + ".bak"))

    def test_inspect_reports_manual_for_a_commented_file(self):
        c = self.client('{ // comment\n "theme": "x"\n}')
        state, detail = connect.inspect(c, connect.build_entry("qa"))
        self.assertEqual(state, "manual")
        self.assertIn("comments", detail)


class ClineTests(Base):
    def test_the_cli_path_wins_when_it_exists(self):
        cli_dir = os.path.join(self.home, ".cline", "data", "settings")
        os.makedirs(cli_dir)
        path = connect._cline_path(os.path.join(self.home, "Code", "User"), home=self.home)
        self.assertEqual(path, os.path.join(cli_dir, "cline_mcp_settings.json"))

    def test_the_extension_path_is_used_otherwise(self):
        user = os.path.join(self.home, "Code", "User")
        path = connect._cline_path(user, home=self.home)
        self.assertEqual(path, os.path.join(user, "globalStorage", "saoudrizwan.claude-dev",
                                            "settings", "cline_mcp_settings.json"))

    def test_the_entry_lands_in_mcpservers(self):
        d = os.path.join(self.home, "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings")
        os.makedirs(d)
        c = connect.Client("cline", "Cline", os.path.join(d, "cline_mcp_settings.json"))
        self.run_connect([c], agent="qa")
        data = json.loads(Path(c.path).read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["memdex"]["env"], {"MEMDEX_AGENT": "qa"})


class ServeCheckTests(Base):
    SERVER = '''
import json, sys
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub-memdex", "version": "9.9"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"tools": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}}), flush=True)
'''

    def script(self, body):
        path = os.path.join(self.tmp, "stub_server.py")
        Path(path).write_text(body, encoding="utf-8")
        return {"command": sys.executable, "args": [path]}

    def test_a_working_server_is_reported_with_its_tool_count(self):
        ok, message = connect.check_serve(self.script(self.SERVER), timeout=20)
        self.assertTrue(ok, message)
        self.assertIn("stub-memdex", message)
        self.assertIn("3 tools", message)

    def test_a_server_error_is_reported(self):
        body = ('import json, sys\n'
                'for line in sys.stdin:\n'
                '    msg = json.loads(line)\n'
                '    if msg.get("method") == "initialize":\n'
                '        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],'
                ' "error": {"code": -32601, "message": "no"}}), flush=True)\n')
        ok, message = connect.check_serve(self.script(body), timeout=20)
        self.assertFalse(ok)
        self.assertIn("server error", message)

    def test_a_server_that_dies_is_reported_with_its_exit_code(self):
        ok, message = connect.check_serve(self.script("import sys\nsys.exit(3)\n"), timeout=20)
        self.assertFalse(ok)
        self.assertIn("exited before answering", message)
        self.assertIn("exit 3", message)

    def test_a_command_that_does_not_exist_is_reported(self):
        ok, message = connect.check_serve({"command": os.path.join(self.tmp, "nope")}, timeout=10)
        self.assertFalse(ok)
        self.assertIn("could not start", message)

    def test_run_reports_the_check_as_a_line(self):
        c = self.client()
        original = connect.check_serve
        connect.check_serve = lambda entry, cwd="", timeout=20.0: (True, "memdex answered: 13 tools")
        try:
            code, out = self.run_connect([c], agent="qa", check=True)
        finally:
            connect.check_serve = original
        self.assertEqual(code, 0)
        self.assertIn("serve-check", out)
        self.assertIn("13 tools", out)

    def test_a_failed_check_makes_the_command_fail(self):
        c = self.client()
        original = connect.check_serve
        connect.check_serve = lambda entry, cwd="", timeout=20.0: (False, "no answer within 20s")
        try:
            code, _ = self.run_connect([c], agent="qa", check=True)
        finally:
            connect.check_serve = original
        self.assertEqual(code, 1)


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
