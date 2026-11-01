"""The CLI promises that every MCP tool has a command. These tests hold it to that.

Each case runs `memdex` in-process (memdex.cli.main) against a throwaway project
and database, and checks the printed line the user would see.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex.cli import main

TOOLS = {"project_brief", "file_summary", "file_summary_save", "where_is", "read_snippet",
         "search", "remember", "note", "handoff", "post", "inbox", "claim"}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        Path(self.root, "main.go").write_text("package main\n\nfunc (s *Server) Start() {}\n", encoding="utf-8")
        self.db = os.path.join(self.tmp, "memdex.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main([*args, "--path", self.root, "--agent", "qa", "--db", self.db])
        return code, buf.getvalue()

    def test_every_tool_has_a_command(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            main(["--help"])
        text = buf.getvalue()
        commands = {"project_brief": "brief", "file_summary": "file", "file_summary_save": "file",
                    "where_is": "where_is", "read_snippet": "snippet", "search": "search",
                    "remember": "remember", "note": "note", "handoff": "handoff", "post": "post",
                    "inbox": "inbox", "claim": "claim"}
        for tool in TOOLS:
            self.assertIn(commands[tool], text, f"tool {tool} has no CLI command")

    def test_where_is(self):
        self.run_cli("index")
        code, out = self.run_cli("where_is", "Start")
        self.assertEqual(code, 0)
        self.assertIn("main.go", out)
        self.assertIn("method Server.Start", out)

    def test_where_is_missing_symbol(self):
        code, out = self.run_cli("where_is", "nope_nothing")
        self.assertEqual(code, 0)
        self.assertIn("no symbol matching", out)

    def test_note_get_and_archive(self):
        _, out = self.run_cli("remember", "decision", "use sqlite", "--body", "zero deps")
        self.assertIn("saved decision #1", out)
        code, out = self.run_cli("note", "1")
        self.assertEqual(code, 0)
        self.assertIn("use sqlite", out)
        self.assertIn("zero deps", out)
        code, out = self.run_cli("note", "1", "--archive")
        self.assertEqual(code, 0)
        self.assertIn("archived #1", out)

    def test_note_missing(self):
        code, out = self.run_cli("note", "999")
        self.assertEqual(code, 1)  # scripts can tell a missing note from a printed one
        self.assertIn("not found", out)

    def test_db_flag_after_the_subcommand(self):
        """`--db` works in both positions: before the command (global) and after it."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["doctor", "--db", self.db, "--offline", "--path", self.root])
        self.assertEqual(code, 0)
        self.assertIn("schema v", buf.getvalue())
        self.assertTrue(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
