"""Memdex Studio. Its native window calls the local project board directly."""
import argparse
import locale
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading

from . import project as projects
from .board_web import ASSET_DIR, inline_page, read_endpoint, write_endpoint
from .store import default_path


def _last_folder(settings):
    try:
        folder = Path(settings.read_text(encoding="utf-8"))
        if folder.is_absolute() and folder.is_dir():
            return folder
    except (OSError, ValueError):
        pass
    return None


def _remember(settings, folder):
    # Two Studio windows can close or switch projects independently.
    temporary = None
    try:
        settings.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.parent, delete=False, mode="w", encoding="utf-8") as f:
            temporary = Path(f.name)
            f.write(folder)
        os.replace(temporary, settings)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DesktopBoard:
    """Only read and write are exposed to the embedded view."""
    def __init__(self, db, limit):
        self._db = Path(db).absolute()
        self._limit = limit
        self._context = None

    def _open(self, folder, display):
        if not Path(folder).is_dir():
            raise ValueError(f"Project folder does not exist: {folder}")
        project = projects.resolve(str(folder))
        token = secrets.token_hex(24)
        page = inline_page(f'<input type="hidden" id="desktop-session" value="{token}">')
        previous = self._context
        self._context = (token, project)
        try:
            display(f"{project.name} — Memdex Studio", page)
        except Exception:
            self._context = previous
            raise
        return project.root

    def _project(self, token):
        context = self._context
        if context is None or token != context[0]:
            raise ValueError("This project view is closed.")
        return context[1]

    def read(self, token, endpoint):
        return read_endpoint(self._db, self._project(token), self._limit, endpoint)

    def write(self, token, action, body):
        return write_endpoint(self._db, self._project(token), action, body)


def _error(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), "Memdex Studio", 0x10)
    else:
        from tkinter import messagebox
        messagebox.showerror("Memdex Studio", str(message))
    return 1


def launch(db, folder=None, limit=25):
    repo = Path(__file__).resolve().parents[1]
    studio = repo / "studio"
    module = studio / "node_modules/electron"
    packaged = _last_folder(repo / "dist/studio-build.txt")
    if packaged:
        packaged = packaged / "Memdex-Studio.exe"
    if (studio / "main.cjs").is_file() and ((packaged and packaged.is_file()) or (module / "path.txt").is_file()):
        if packaged and packaged.is_file():
            args = [str(packaged)]
        else:
            dist = Path(os.environ.get("ELECTRON_OVERRIDE_DIST_PATH") or module / "dist")
            binary = dist / (module / "path.txt").read_text(encoding="utf-8").strip()
            args = [str(binary), str(studio)]
        args.extend(["--db=" + str(Path(db).absolute()), f"--limit={limit}"])
        if folder:
            args.append("--project=" + str(Path(folder).absolute()))
        env = dict(os.environ, MEMDEX_PYTHON=sys.executable)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        try:
            return subprocess.run(args, env=env).returncode
        except OSError as exc:
            return _error(f"Memdex Studio: {exc}")
    return _launch_webview(db, folder, limit)


def _launch_webview(db, folder=None, limit=25):
    try:
        import webview
        from webview.menu import Menu, MenuAction
    except ImportError:
        return _error('Install the desktop extra: python -m pip install "memdex-mcp[desktop]"')

    korean = (locale.getlocale()[0] or "").lower().startswith(("ko", "korean"))
    choose = "프로젝트 폴더 열기…" if korean else "Open Project Folder…"
    project_menu = "프로젝트" if korean else "Project"
    instruction = ("프로젝트 메뉴에서 작업할 폴더를 선택하세요." if korean
                   else "Choose Open Project Folder from the Project menu.")
    settings = Path(default_path()).with_name("studio-project.txt")
    initial = folder or _last_folder(settings)
    board = DesktopBoard(db, limit)
    current = ""
    # The native menu dispatches on worker threads. Only one chooser may own the window.
    choosing = threading.Lock()
    lifecycle = threading.Lock()
    closed = False

    def create(title, page=None):
        nonlocal window
        welcome = ('<meta charset="utf-8"><style>body{background:#101b1e;color:#eae7dd;'
                   'font:16px system-ui;padding:8vw}h1{font-size:36px}</style>'
                   f'<h1>Memdex Studio</h1><p>{instruction}</p>')
        window = webview.create_window(title, html=page or welcome, js_api=board, width=1320,
                                       height=840, min_size=(760, 500), background_color="#101b1e",
                                       text_select=True)

    def navigate(title, page):
        window.load_html(page)
        window.set_title(title)

    def open_folder():
        nonlocal current
        if not choosing.acquire(blocking=False):
            return
        try:
            picked = window.create_file_dialog(webview.FileDialog.FOLDER, directory=current)
            if picked:
                with lifecycle:
                    if not closed:
                        current = board._open(picked[0], navigate)
                        _remember(settings, current)
        except (OSError, ValueError, webview.errors.WebViewException) as exc:
            if not closed:
                _error(str(exc))
        finally:
            choosing.release()

    window = None
    try:
        if initial:
            current = board._open(initial, create)
        else:
            create("Memdex Studio")
        menu = [Menu(project_menu, [MenuAction(choose, open_folder)])]
        icon = str(ASSET_DIR / "studio.ico") if sys.platform == "win32" else None
        if initial:
            window.events.shown += lambda: _remember(settings, current)
        webview.start(None if initial else open_folder, gui="edgechromium" if sys.platform == "win32" else None,
                      menu=menu, icon=icon)
        return 0
    except (ImportError, OSError, RuntimeError, ValueError, webview.errors.WebViewException) as exc:
        return _error(f"Memdex Studio: {exc}")
    finally:
        with lifecycle:
            closed = True
            board._context = None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="memdex-studio", description="Open Memdex Studio")
    parser.add_argument("folder", nargs="?", help="project folder to open")
    parser.add_argument("--db", help="use a specific Memdex database")
    args = parser.parse_args(argv)
    return launch(args.db or default_path(), args.folder or os.environ.get("MEMDEX_PROJECT"))


if __name__ == "__main__":
    raise SystemExit(main())
