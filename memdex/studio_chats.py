"""Read human and assistant turns from local IDE transcripts for the desktop Studio."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from . import project as projects
from .board import read_db

UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
HEADER_BYTES = 512 * 1024
LINE_BYTES = 4 * 1024 * 1024
PAGE_BYTES = 8 * 1024 * 1024
PAGE_TURNS = 30


def _cwd(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return ""
    if os.name == "nt":
        value = value.removeprefix("\\\\?\\")
        # Only enumerate this machine's local drives; a transcript can contain foreign paths.
        if not re.match(r"^[a-zA-Z]:[\\/]", value):
            return ""
    return os.path.normcase(os.path.normpath(value)) if os.path.isabs(value) else ""


def _rows(stream, budget):
    end = stream.tell() + budget
    while stream.tell() < end:
        pos = stream.tell()
        raw = stream.readline(LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(128 * 1024)
            yield None
            continue
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            if not raw.endswith(b"\n"):
                stream.seek(pos)  # A live writer may still be appending this record.
                return
            continue
        if isinstance(row, dict):
            yield row


def _user_text(text):
    value = text.strip()
    if value.startswith("<send_user_message_question_reply>"):
        try:
            items = json.loads(value.split(">", 1)[1].split("</send_user_message_question_reply>", 1)[0])
            return "\n".join(i["answer"] for i in items if isinstance(i, dict) and isinstance(i.get("answer"), str))
        except (ValueError, TypeError):
            return ""
    while value.startswith("<system-reminder>") and "</system-reminder>" in value:
        value = value.split("</system-reminder>", 1)[1].strip()
    if value.startswith(("# AGENTS.md instructions", "<environment_context>", "<subagent_notification>",
                         "<system-reminder>", "<local-command-caveat>", "<command-name>", "<local-command-stdout>")):
        return ""
    return value


def _turn(provider, row):
    if provider == "codex":
        msg = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(msg, dict) or msg.get("type") != "message":
            return None
    else:
        msg = row.get("message")
        if not isinstance(msg, dict) or row.get("type") != msg.get("role") or row.get("isMeta") or row.get("isSidechain"):
            return None
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [v["text"] for v in content if isinstance(v, dict) and
                 v.get("type") in ("text", "input_text", "output_text") and isinstance(v.get("text"), str)]
    else:
        return None
    if role == "user":
        parts = [_user_text(text) for text in parts]
    text = "\n".join(s for s in parts if s.strip())
    if text:
        return {"role": role, "text": text, "time": row.get("timestamp")}


@dataclass
class Chat:
    provider: str
    uid: str
    path: Path
    journal: Path
    cwd: str
    title: str
    seen: float
    size: int

    @property
    def key(self):
        return self.provider + ":" + self.uid


def _metadata(provider, uid, path, need_title=False):
    cwd = title = ""
    with path.open("rb") as stream:
        for row in _rows(stream, HEADER_BYTES):
            if not row:
                continue
            if provider == "codex":
                if row.get("type") == "session_meta":
                    data = row.get("payload")
                    if not isinstance(data, dict) or uid not in (data.get("id"), data.get("session_id")):
                        return "", ""
                    cwd = _cwd(data.get("cwd"))
                    if not need_title:
                        return cwd, ""
                elif cwd:
                    turn = _turn(provider, row)
                    if turn and turn["role"] == "user":
                        return cwd, " ".join(turn["text"].split())[:120]
                continue
            if row.get("sessionId") != uid or row.get("isSidechain"):
                continue
            cwd = cwd or _cwd(row.get("cwd"))
            if row.get("type") == "user":
                turn = _turn(provider, row)
                if turn and not title:
                    title = " ".join(turn["text"].split())[:120]
            if cwd and title:
                break
        if provider == "claude" and cwd and need_title:
            start = max(0, stream.seek(0, os.SEEK_END) - 65536)
            stream.seek(start)
            if start:
                stream.readline(65536)
            for row in _rows(stream, 65536):
                if row and row.get("sessionId") == uid and row.get("type") == "custom-title":
                    name = row.get("customTitle")
                    if isinstance(name, str) and name.strip():
                        title = " ".join(name.split())[:160]
    return cwd, title


def _safe_path(path, root):
    try:
        # glob is shallow and provider-specific; reject links and directory junctions escaping it.
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True)) and path.is_file() and not path.is_symlink()
    except (OSError, RuntimeError):
        return False


def _codex_titles(home):
    titles = {}
    # Rollouts determine membership. The optional local index supplies display titles only.
    dbs = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem[6:]) if p.stem[6:].isdigit() else -1, reverse=True)
    for db in dbs:
        if not _safe_path(db, home):
            continue
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
                for uid, title in conn.execute("SELECT id,title FROM threads"):
                    if isinstance(title, str) and title.strip():
                        titles[uid] = " ".join(_user_text(title).split())[:160]
            break
        except (sqlite3.Error, OSError):
            continue
    index = home / "session_index.jsonl"
    if _safe_path(index, home):
        try:
            with index.open("rb") as stream:
                for row in _rows(stream, os.fstat(stream.fileno()).st_size):
                    if row and isinstance(row.get("id"), str) and isinstance(row.get("thread_name"), str):
                        title = " ".join(_user_text(row["thread_name"]).split())[:160]
                        if title:
                            titles[row["id"]] = title
        except OSError:
            pass
    return titles


class LocalChats:
    def __init__(self, codex=None, claude=None, memdex_db=None):
        self.memdex_db = memdex_db
        self.codex = Path(codex or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.claude = Path(claude or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        self.files = {}
        self.chats = {}
        self.cursors = {}
        self.checked = None

    def _bindings(self):
        bindings = {}
        if self.memdex_db is None:
            return bindings
        with read_db(self.memdex_db) as db:
            if db is None or "conversation_id" not in {r[1] for r in db.execute("PRAGMA table_info(chat_routes)")}:
                return bindings
            for agent, uid, key in db.execute("SELECT agent,conversation_id,project FROM chat_routes WHERE conversation_id IS NOT NULL"):
                name = (agent or "").lower().split("@")[0].split("-")[0]
                if name in ("codex", "gpt", "claude") and _cwd(key):
                    provider = "claude" if name == "claude" else "codex"
                    bindings.setdefault((provider, uid), set()).add(key)
        return bindings

    def refresh(self, force=False):
        if not force and self.checked is not None and time.monotonic() - self.checked < 30:
            return
        headers, chats, roots = {}, {}, {}
        titles = _codex_titles(self.codex)
        bindings = self._bindings()
        sources = [("codex", self.codex / "sessions", "*/*/*/rollout-*.jsonl"),
                   ("codex", self.codex / "archived_sessions", "rollout-*.jsonl"),
                   ("claude", self.claude / "projects", "*/*.jsonl")]
        for provider, folder, pattern in sources:
            for path in folder.glob(pattern):
                match = re.search("(" + UUID + ")$", path.stem)
                if not match or (provider == "claude" and path.stem != match[1]) or not _safe_path(path, folder):
                    continue
                uid = match[1]
                try:
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    previous = self.files.get(path)
                    if previous and previous[0] == signature:
                        cwd, title = previous[1:]
                    else:
                        cwd, title = _metadata(provider, uid, path, need_title=provider == "claude" or not titles.get(uid))
                    headers[path] = signature, cwd, title
                    if not cwd:
                        continue
                    if cwd not in roots:
                        root = Path(cwd)
                        git = next((p for p in (root, *root.parents) if (p / ".git").exists()), None)
                        roots[cwd] = projects.resolve(str(git)).key if git else cwd
                    chat = Chat(provider, uid, path, folder, cwd, (titles.get(uid) if provider == "codex" else "") or title,
                                stat.st_mtime, stat.st_size)
                    registered = bindings.get((provider, uid), set())
                    key = next(iter(registered)) if len(registered) == 1 else roots[cwd]
                    project = chats.setdefault(key, {})
                    if chat.key not in project or chat.seen > project[chat.key].seen:
                        project[chat.key] = chat
                except (OSError, ValueError, RuntimeError):
                    continue
        self.files, self.chats = headers, chats
        self.checked = time.monotonic()

    def groups(self):
        self.refresh()
        result = {}
        for key, chats in self.chats.items():
            rows = list(chats.values())
            result[key] = {"codex": sum(v.provider == "codex" for v in rows),
                           "claude": sum(v.provider == "claude" for v in rows),
                           "seen": max(v.seen for v in rows)}
        return result

    def listing(self, project):
        self.refresh()
        return [{"id": c.key, "source": c.provider, "title": c.title, "seen": c.seen, "size": c.size}
                for c in sorted(self.chats.get(project, {}).values(), key=lambda c: (-c.seen, c.key))]

    def page(self, project, key, offset=0):
        chat = self.chats.get(project, {}).get(key)
        if not chat or not _safe_path(chat.path, chat.journal):
            raise ValueError("Chat is unavailable for this project.")
        turns, skipped = [], 0
        with chat.path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            ident = stat.st_dev, stat.st_ino
            if offset:
                cursor = self.cursors.get((project, key))
                if not cursor or cursor != (ident, offset) or offset > stat.st_size:
                    raise ValueError("Chat changed. Reopen it to read from the beginning.")
            else:
                if _metadata(chat.provider, chat.uid, chat.path)[0] != chat.cwd:
                    raise ValueError("Chat metadata changed. Refresh the conversation list.")
            stream.seek(offset)
            boundary = False
            for row in _rows(stream, PAGE_BYTES):
                if row is None:
                    skipped += 1
                    continue
                if chat.provider == "claude" and row.get("sessionId") != chat.uid:
                    continue
                if chat.provider == "codex" and row.get("type") == "session_meta":
                    data = row.get("payload")
                    if not isinstance(data, dict) or chat.uid not in (data.get("id"), data.get("session_id")) or _cwd(data.get("cwd")) != chat.cwd:
                        boundary = True
                        break
                turn = _turn(chat.provider, row)
                if turn:
                    turns.append(turn)
                    if len(turns) == PAGE_TURNS:
                        break
            end = stream.tell()
            more = end if not boundary and offset < end < stat.st_size else None
            if more is not None:
                self.cursors[project, key] = ident, more
            return {"messages": turns, "next": more, "skipped": skipped}
