"""Desktop bridge isolation and double-click startup without a display server."""
from contextlib import closing
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from memdex import cli, desktop, project
from memdex.store import Store


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = root / "memdex.db"
        self.a = root / "first project"
        self.b = root / "second project"
        self.a.mkdir()
        self.b.mkdir()
        self.settings = root / "studio-project.txt"
        env = patch.dict(os.environ, {"MEMDEX_HOME": str(root), "MEMDEX_PROJECT": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_current_window_uses_the_existing_board_and_invalidates_old_actions(self):
        with closing(Store(str(self.db))) as store:
            mid = store.post(project.resolve(str(self.a)), "codex", "First project only")
        view = desktop.DesktopBoard(self.db, 12)
        page = MagicMock()
        view._open(self.a, page)
        markup = page.call_args.args[1]
        token = re.search('id="desktop-session" value="([a-f0-9]+)"', markup)[1]
        self.assertNotIn('src="workspace.js"', markup)
        self.assertEqual(view.read(token, "thread/" + str(mid))["messages"][0]["body"], "First project only")
        created = view.write(token, "task", {"title": "QA", "criterion": "A reply"})
        self.assertIn(created["id"], [task["id"] for task in view.read(token, "state")["tasks"]])
        view._open(self.b, page)
        other = re.search('id="desktop-session" value="([a-f0-9]+)"', page.call_args.args[1])[1]
        with self.assertRaisesRegex(ValueError, "closed"):
            view.write(token, "task", {"title": "Stale form", "criterion": "Old project"})
        self.assertEqual(view.read(other, "state")["tasks"], [])
        with self.assertRaises(LookupError):
            view.read(other, "thread/" + str(mid))
        with self.assertRaisesRegex(ValueError, "Unknown"):
            view.write(other, "shell", {})
        with self.assertRaisesRegex(ValueError, "does not exist"):
            view._open(self.b / "missing", page)
        self.assertEqual(view.read(other, "state")["project"], self.b.name)

    def test_first_launch_cancel_open_and_restore(self):
        view = types.ModuleType("webview")
        menu = types.ModuleType("webview.menu")
        menu.Menu = lambda _name, items: items
        menu.MenuAction = lambda _name, callback: callback
        view.FileDialog = types.SimpleNamespace(FOLDER=20)
        view.errors = types.SimpleNamespace(WebViewException=RuntimeError)
        window = MagicMock()
        view.create_window = MagicMock(return_value=window)
        window.create_file_dialog.side_effect = [None, [str(self.b)]]

        def first(startup, **opts):
            self.assertIsNotNone(startup)
            startup()
            self.assertFalse(self.settings.exists())
            opts["menu"][0][0]()
            self.assertEqual(window.load_html.call_count, 1)

        with patch.dict(sys.modules, {"webview": view, "webview.menu": menu}), patch("webbrowser.open") as browser, \
             patch.object(desktop, "launch", desktop._launch_webview):
            view.start = first
            self.assertEqual(desktop.main([]), 0)
            bridge = view.create_window.call_args.kwargs["js_api"]
            self.assertIsNone(bridge._context)
            self.assertEqual(desktop._last_folder(self.settings), self.b)
            view.start = MagicMock()
            self.assertEqual(desktop.main([]), 0)
            self.assertIsNone(view.start.call_args.args[0])
            self.assertIn(self.b.name, view.create_window.call_args.args[0])
            browser.assert_not_called()
        self.b.rmdir()
        self.assertIsNone(desktop._last_folder(self.settings))

    def test_cli_opens_current_checkout_with_existing_db(self):
        with patch("memdex.desktop.launch", return_value=0) as launch:
            self.assertEqual(cli.main(["board", "--desktop", "--path", str(self.a),
                                       "--db", str(self.db), "--limit", "12"]), 0)
        launch.assert_called_once_with(str(self.db), str(self.a.resolve()), 12)
