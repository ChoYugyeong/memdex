"""A local operator's project-wide view of Memdex coordination."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata

from . import project as projects
from .store import default_path
from .tools import render_task_tree


@dataclass
class Snapshot:
    tasks: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    routes: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    message_tasks: dict = field(default_factory=dict)


@contextmanager
def read_db(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("refusing to open a symlinked Memdex database path")
    if not path.exists():
        yield None
        return
    # Opening a Store would initialize or migrate the DB. The board also works before the first writer.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db


def snapshot(path, project, limit, message_id=None, *, before=None, thread=False, task_links=False):
    """Read one consistent SQLite snapshot, including an empty or pre-routing database."""
    result = Snapshot()
    with read_db(path) as db:
        if db is None:
            return result
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        key = (project.key,)
        if "chat_routes" in tables:
            result.routes = {row["route"]: dict(row) for row in db.execute(
                "SELECT * FROM chat_routes WHERE project=? ORDER BY last_seen_at DESC,route", key)}
        if "messages" in tables:
            if before is not None:
                rows = db.execute("SELECT * FROM messages WHERE project=? AND id<? ORDER BY id DESC LIMIT ?",
                                  (*key, before, limit))
            elif message_id is None:
                rows = db.execute("SELECT * FROM messages WHERE project=? ORDER BY id DESC LIMIT ?",
                                  (*key, limit))
            elif thread and any(row["name"] == "reply_to" for row in db.execute("PRAGMA table_info(messages)")):
                rows = db.execute("""
                    WITH RECURSIVE ancestors(id, reply_to) AS (
                        SELECT id,reply_to FROM messages WHERE project=? AND id=?
                        UNION SELECT m.id,m.reply_to FROM messages m JOIN ancestors a ON m.id=a.reply_to
                        WHERE m.project=?
                    ), family(id) AS (
                        SELECT id FROM ancestors
                        UNION SELECT m.id FROM messages m JOIN family f ON m.reply_to=f.id WHERE m.project=?
                    )
                    SELECT m.* FROM messages m JOIN family f ON m.id=f.id ORDER BY m.id
                """, (project.key, message_id, project.key, project.key))
            else:
                rows = db.execute("SELECT * FROM messages WHERE project=? AND id=?", (*key, message_id))
            result.messages = [dict(row) for row in rows]
            if task_links:
                result.message_tasks = _message_tasks(db, project.key, result.messages)
        if message_id is None and before is None and "tasks" in tables:
            result.tasks = db.execute(
                "SELECT id,parent_id,title,status,owner,blocker,criterion FROM tasks WHERE project=? "
                "ORDER BY status IN ('verified','self_verified'), updated_at DESC,id", key).fetchall()
        if message_id is None and before is None and "sessions" in tables:
            result.sessions = db.execute("SELECT agent,last_seen_at FROM sessions WHERE project=? "
                                         "ORDER BY last_seen_at DESC,agent", key).fetchall()
    return result


def message_content(body):
    """Separate the actual post from the task context captured when it was sent."""
    context, separator, content = body.partition("\n\n")
    tag = re.match(r"^\[task status at send\]\ntask #([0-9]{1,19}) \[", context)
    if tag and separator:
        return int(tag[1]), content, context
    review = re.match(r"^\[(?:Claude|DeepSeek|Qwen|Ollama) review task #([0-9]{1,19})(?: · [^\]\n]+)?\]\n", body)
    if review:
        return int(review[1]), body[review.end():], body[:review.end()].strip()
    return None, body, ""


def _message_tasks(db, project_key, messages):
    # A reply can land on a later page, after either chat moved to another task.
    known = {m["id"]: (message_content(m["body"] or "")[0], m.get("reply_to")) for m in messages}
    resolved = {}
    for mid in list(known):
        chain = set()
        current = mid
        task = None
        while current is not None and current not in chain:
            if current in resolved:
                task = resolved[current]
                break
            if current not in known:
                parent = db.execute("SELECT body,reply_to FROM messages WHERE project=? AND id=?",
                                    (project_key, current)).fetchone()
                if parent is None:
                    break
                known[current] = (message_content(parent["body"] or "")[0], parent["reply_to"])
            chain.add(current)
            task, current = known[current]
            if task is not None:
                break
        resolved.update(dict.fromkeys(chain, task))
    return resolved


def _safe(text):
    return "".join(ch if ch.isprintable() else " " for ch in str(text or ""))


def _cell_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _fit(text, width):
    """Clip by terminal cells so a Korean title keeps the adjacent column aligned."""
    used = 0
    for index, ch in enumerate(text):
        size = _cell_width(ch)
        if used + size > width:
            prefix = text[:index]
            while prefix and used >= width:
                used -= _cell_width(prefix[-1])
                prefix = prefix[:-1]
            return prefix + "…"
        used += size
    return text


def _stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts is not None else "?"


def _endpoint(routes, route, fallback):
    if route in routes:
        name = routes[route]["agent"].partition("@")[0]
        return f"{_safe(name)}~{_safe(route[-8:])}"
    return _safe(route or fallback or "everyone")


def _session_lines(data, now):
    rows = []
    routed = set()
    for route, chat in data.routes.items():
        routed.add(chat["agent"])
        name = _endpoint(data.routes, route, "")
        task = f"task #{chat['task_id']}" if chat.get("task_id") is not None else "unassigned"
        info = [task, _safe(Path(chat["checkout"]).name)]
        rows.append((chat["last_seen_at"], name, info, chat["label"]))
    for session in data.sessions:
        if session["agent"] not in routed:
            rows.append((session["last_seen_at"], _safe(session["agent"]), [], ""))
    rows.sort(key=lambda r: (-(r[0] or 0), r[1]))
    lines = []
    for ts, name, details, label in rows:
        seen = "recent" if ts is not None and now - ts < 600 else _stamp(ts)
        lines.append(" · ".join(x for x in [name, *details, seen, _safe(label)] if x))
    return lines or ["No registered sessions."]


def _message_lines(data, message, full=False):
    src = _endpoint(data.routes, message.get("sender_route"), message["sender"])
    dest = _endpoint(data.routes, message.get("recipient_route"), message["recipient"])
    body = message["body"] or ""
    meta = [f"#{message['id']}", _stamp(message["created_at"])]
    if message.get("reply_to") is not None:
        meta.append(f"reply to #{message['reply_to']}")
    # Task-tagged posts contain a ledger snapshot followed by the actual message. Display the
    # recorded tag, never infer an old post's task from the chat's current assignment.
    task, content, _ = message_content(body)
    if task is not None:
        meta.append(f"task #{task}")
        if not full:
            body = content
    lines = [" · ".join(meta), f"{src} → {dest}"]
    if full:
        return lines + [""] + [_safe(line) for line in body.splitlines()]
    return lines + ["  " + " ".join(_safe(body).split())]


def _cap(lines, count):
    if count is None or len(lines) <= count:
        return lines
    return lines[:count - 1] + [f"… {len(lines) - count + 1} more lines"]


def render(data, project, width, height=None):
    """Static output can scroll. A watched frame reserves space for each section."""
    width = max(12, width)
    wide = width >= 94
    budget = None if height is None else max(4, (height - 2) if wide else (height - 3) // 2)
    sessions = _session_lines(data, time.time())
    tasks = render_task_tree(data.tasks, max_items=max(1, len(data.tasks)), compact=True,
                             max_chars=500000).splitlines() if data.tasks else ["No tasks recorded."]
    session_budget = None if budget is None else max(1, budget - 2 - min(len(tasks), budget // 2))
    sessions = _cap(sessions, session_budget)
    task_budget = None if budget is None else max(1, budget - len(sessions) - 2)
    left = ["SESSIONS", *sessions, f"TASK TREE ({len(data.tasks)})", *_cap(tasks, task_budget)]
    right = ["RECENT MESSAGES · newest first"]
    for message in data.messages:
        right.extend(_message_lines(data, message))
    if not data.messages:
        right.append("No messages recorded.")
    right = _cap(right, budget)
    output = [_fit("MEMDEX  /  " + _safe(project.name), width)]
    if wide:
        column = min(62, (width - 3) * 46 // 100)
        for a, b in zip_longest(left, right, fillvalue=""):
            a = _fit(_safe(a), column)
            pad = column - sum(_cell_width(ch) for ch in a)
            output.append(a + " " * pad + " │ " + _fit(_safe(b), width - column - 3))
    else:
        output.extend(_fit(_safe(line), width) for line in [*left, "", *right])
    return "\n".join(output)


def _can_redraw(stack):
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    # Classic Windows consoles may have VT processing disabled. Restore their mode on exit.
    import ctypes
    import msvcrt
    console = ctypes.windll.kernel32
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdout.fileno()))
    mode = ctypes.c_uint()
    if not console.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    if mode.value & 0x0004:
        return True
    if not console.SetConsoleMode(handle, mode.value | 0x0004):
        return False
    stack.callback(console.SetConsoleMode, handle, mode.value)
    return True


def run(args):
    if not 1 <= args.limit <= 100:
        print("--limit must be between 1 and 100", file=sys.stderr)
        return 2
    if args.message is not None and not 0 < args.message < 2**63:
        print("--message needs a positive message ID", file=sys.stderr)
        return 2
    path = args.db or default_path()
    project = projects.resolve(args.path or os.environ.get("MEMDEX_PROJECT"))
    try:
        if args.no_open and not args.web:
            print("--no-open requires --web", file=sys.stderr)
            return 2
        if args.desktop:
            from .desktop import launch
            return launch(path, project.root, args.limit)
        if args.web:
            from .board_web import serve
            return serve(path, project, args.limit, args.no_open)
        if args.html is not None:
            from .board_web import export_html
            export_html(path, project, args.limit, args.html)
            return 0
        with ExitStack() as stack:
            live = args.watch and _can_redraw(stack)
            previous = None
            while True:
                data = snapshot(path, project, args.limit, args.message)
                if args.message is not None:
                    if not data.messages:
                        print(f"Message #{args.message} was not found in this project.", file=sys.stderr)
                        return 1
                    print("\n".join(_message_lines(data, data.messages[0], full=True)))
                    return 0
                width, height = shutil.get_terminal_size(fallback=(110, 30))
                frame = render(data, project, width - 1, height if live else None)
                if frame != previous:
                    prefix = "\x1b[H\x1b[2J" if live else ("\n" if previous is not None else "")
                    print(prefix + frame, flush=True)
                    previous = frame
                if not args.watch:
                    return 0
                time.sleep(2)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"Cannot read project board: {_safe(exc)}", file=sys.stderr)
        return 1
