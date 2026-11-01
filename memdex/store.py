"""SQLite storage: file index, content-addressed summaries, notes, messages, claims.

One database (default ~/.memdex/memdex.db) is shared by every agent and every
project on the machine. WAL mode lets several MCP servers read and write it at
once. Nothing is written inside the project, so nothing ends up in a commit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import embed, scan
from .project import Project

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    root TEXT, path TEXT, size INTEGER, mtime INTEGER, sha TEXT,
    lang TEXT, lines INTEGER, outline TEXT, indexed_at REAL,
    PRIMARY KEY(root, path));
CREATE INDEX IF NOT EXISTS files_sha ON files(sha);
CREATE TABLE IF NOT EXISTS summaries(
    sha TEXT PRIMARY KEY, summary TEXT, author TEXT, created_at REAL, path_hint TEXT);
CREATE INDEX IF NOT EXISTS summaries_hint ON summaries(path_hint, created_at);
CREATE TABLE IF NOT EXISTS symbols(root TEXT, path TEXT, name TEXT, kind TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(root, path);
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY, project TEXT, kind TEXT, title TEXT, body TEXT,
    author TEXT, refs TEXT, created_at REAL, superseded_by INTEGER, archived INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project, kind, created_at);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, project TEXT, sender TEXT, recipient TEXT, body TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS cursors(project TEXT, agent TEXT, last_id INTEGER, PRIMARY KEY(project, agent));
CREATE TABLE IF NOT EXISTS claims(
    project TEXT, agent TEXT, path TEXT, intent TEXT, created_at REAL, expires_at REAL,
    PRIMARY KEY(project, agent, path));
CREATE TABLE IF NOT EXISTS vectors(src TEXT, ref TEXT, scope TEXT, vec BLOB, PRIMARY KEY(src, ref, scope));
"""

NOTE_KINDS = ("note", "decision", "pitfall", "handoff", "todo")


def default_path() -> str:
    home = os.environ.get("MEMDEX_HOME") or os.path.join(Path.home(), ".memdex")
    return os.path.join(home, "memdex.db")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.trigram = self._make_fts()
        self.db.commit()

    def _make_fts(self) -> bool:
        exists = self.db.execute("SELECT sql FROM sqlite_master WHERE name='fts'").fetchone()
        if exists:
            return "trigram" in exists["sql"]
        # IF NOT EXISTS: two processes may create the database at the same moment.
        try:
            # trigram matches inside words, so Korean/Japanese/Chinese work without a tokenizer.
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
                            "text, src UNINDEXED, ref UNINDEXED, scope UNINDEXED, tokenize='trigram')")
            return True
        except sqlite3.OperationalError:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
                            "text, src UNINDEXED, ref UNINDEXED, scope UNINDEXED)")
            return False

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- search index

    def _fts_put(self, src: str, ref: str, scope: str, text: str) -> None:
        self.db.execute("DELETE FROM fts WHERE src=? AND ref=? AND scope=?", (src, ref, scope))
        if text:
            self.db.execute("INSERT INTO fts(text, src, ref, scope) VALUES(?,?,?,?)", (text, src, ref, scope))

    def _vec_put(self, src: str, ref: str, scope: str, text: str) -> None:
        vec = embed.embed(text)
        if vec is not None:
            self.db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (src, ref, scope, vec))

    def _file_text(self, path: str, outline: str, sha: str) -> str:
        s = self.db.execute("SELECT summary FROM summaries WHERE sha=?", (sha,)).fetchone()
        return "\n".join(x for x in (path, s["summary"] if s else "", outline) if x)

    # ---------------------------------------------------------------- files

    def index(self, project: Project) -> dict[str, int]:
        """Bring the file index up to date. Only files whose stat moved are read."""
        known = {r["path"]: (r["size"], r["mtime"], r["sha"]) for r in
                 self.db.execute("SELECT path, size, mtime, sha FROM files WHERE root=?", (project.root,))}
        current = scan.list_files(project)
        stats = {"files": len(current), "changed": 0, "removed": 0, "skipped": 0}
        with self.db:
            for rel in set(known) - set(current):
                self._drop_file(project.root, rel)
                stats["removed"] += 1
            for rel in current:
                result = self._index_file(project, rel, known.get(rel))
                if result == "changed":
                    stats["changed"] += 1
                elif result == "skipped":
                    stats["skipped"] += 1
        return stats

    def _drop_file(self, root: str, rel: str) -> None:
        self.db.execute("DELETE FROM files WHERE root=? AND path=?", (root, rel))
        self.db.execute("DELETE FROM symbols WHERE root=? AND path=?", (root, rel))
        self._fts_put("file", rel, root, "")

    def _index_file(self, project: Project, rel: str, prev: Optional[tuple]) -> str:
        full = os.path.join(project.root, rel)
        try:
            st = os.stat(full)
        except OSError:
            if prev:
                self._drop_file(project.root, rel)
            return "skipped"
        if prev and prev[0] == st.st_size and prev[1] == st.st_mtime_ns:
            return "same"
        got = scan.read_text(full)
        if got is None:
            if prev:
                self._drop_file(project.root, rel)
            return "skipped"
        raw, text = got
        sha = scan.sha256(raw)
        if prev and prev[2] == sha:
            self.db.execute("UPDATE files SET size=?, mtime=? WHERE root=? AND path=?",
                            (st.st_size, st.st_mtime_ns, project.root, rel))
            return "same"
        ol = scan.outline(rel, text)
        rendered = ol.render()
        self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                        (project.root, rel, st.st_size, st.st_mtime_ns, sha, ol.lang, ol.lines,
                         rendered, time.time()))
        self.db.execute("DELETE FROM symbols WHERE root=? AND path=?", (project.root, rel))
        self.db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?)",
                            [(project.root, rel, n, k, ln) for n, k, ln in ol.symbols])
        self._fts_put("file", rel, project.root, self._file_text(rel, rendered, sha))
        return "changed"

    def refresh_file(self, project: Project, rel: str) -> Optional[sqlite3.Row]:
        """Re-check one file and return its row (None if missing or not indexable)."""
        rel = to_rel(project, rel)
        if rel is None:
            return None
        prev =self.db.execute("SELECT size, mtime, sha FROM files WHERE root=? AND path=?",
                               (project.root, rel)).fetchone()
        if scan.is_secret(rel):
            return None
        with self.db:
            self._index_file(project, rel, tuple(prev) if prev else None)
        return self.db.execute("SELECT * FROM files WHERE root=? AND path=?", (project.root, rel)).fetchone()

    def file_info(self, project: Project, rel: str) -> Optional[dict[str, Any]]:
        row = self.refresh_file(project, rel)
        if row is None:
            return None
        s = self.db.execute("SELECT summary, author, created_at FROM summaries WHERE sha=?",
                            (row["sha"],)).fetchone()
        info = {"path": row["path"], "sha": row["sha"], "lines": row["lines"], "lang": row["lang"],
                "outline": row["outline"], "summary": None, "stale_summary": None}
        if s:
            info["summary"] = dict(s)
        else:
            old = self.db.execute(
                "SELECT summary, author, created_at FROM summaries WHERE path_hint=? "
                "ORDER BY created_at DESC LIMIT 1", (f"{project.key}::{row['path']}",)).fetchone()
            if old:
                info["stale_summary"] = dict(old)
        return info

    def save_summary(self, project: Project, rel: str, summary: str, author: str) -> Optional[str]:
        row = self.refresh_file(project, rel)
        if row is None:
            return None
        sha = row["sha"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?)",
                            (sha, summary.strip(), author, time.time(), f"{project.key}::{row['path']}"))
            # Every checkout holding identical content gets the summary too.
            for r in self.db.execute("SELECT root, path, outline FROM files WHERE sha=?", (sha,)).fetchall():
                self._fts_put("file", r["path"], r["root"], self._file_text(r["path"], r["outline"], sha))
            self._vec_put("summary", sha, "*", f"{row['path']}\n{summary}")
        return sha

    def find_symbol(self, project: Project, name: str, limit: int = 20) -> list[sqlite3.Row]:
        exact = self.db.execute(
            "SELECT path, name, kind, line FROM symbols WHERE root=? AND (name=? COLLATE NOCASE "
            "OR name LIKE ? ESCAPE '\\') ORDER BY length(name), path LIMIT ?",
            (project.root, name, "%." + _like_escape(name), limit)).fetchall()
        if exact:
            return exact
        return self.db.execute(
            "SELECT path, name, kind, line FROM symbols WHERE root=? AND name LIKE ? ESCAPE '\\' "
            "ORDER BY length(name), path LIMIT ?",
            (project.root, "%" + _like_escape(name) + "%", limit)).fetchall()

    def file_stats(self, project: Project) -> dict[str, int]:
        total = self.db.execute("SELECT count(*) FROM files WHERE root=?", (project.root,)).fetchone()[0]
        summarized = self.db.execute(
            "SELECT count(*) FROM files f JOIN summaries s ON s.sha=f.sha WHERE f.root=?",
            (project.root,)).fetchone()[0]
        return {"files": total, "summarized": summarized}

    def top_dirs(self, project: Project, limit: int = 12) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for (p,) in self.db.execute("SELECT path FROM files WHERE root=?", (project.root,)):
            head = p.split("/", 1)[0] if "/" in p else "."
            counts[head] = counts.get(head, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    # ---------------------------------------------------------------- notes

    def add_note(self, project: Project, kind: str, title: str, body: str, author: str,
                 files: Optional[list[str]] = None, supersedes: Optional[int] = None) -> int:
        refs = []
        for f in files or []:
            row = self.refresh_file(project, f)
            refs.append({"path": row["path"] if row else f, "sha": row["sha"] if row else None})
        with self.db:
            cur = self.db.execute(
                "INSERT INTO notes(project, kind, title, body, author, refs, created_at) VALUES(?,?,?,?,?,?,?)",
                (project.key, kind, title.strip(), body.strip(), author, json.dumps(refs), time.time()))
            nid = cur.lastrowid
            self._fts_put("note", str(nid), project.key, f"{title}\n{body}")
            if supersedes:
                self.db.execute("UPDATE notes SET superseded_by=? WHERE id=? AND project=?",
                                (nid, supersedes, project.key))
                self._fts_put("note", str(supersedes), project.key, "")
            self._vec_put("note", str(nid), project.key, f"{title}\n{body}")
        return nid

    def get_note(self, project: Project, nid: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM notes WHERE id=? AND project=?", (nid, project.key)).fetchone()

    def archive_note(self, project: Project, nid: int) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE notes SET archived=1 WHERE id=? AND project=?", (nid, project.key))
            self._fts_put("note", str(nid), project.key, "")
            self.db.execute("DELETE FROM vectors WHERE src='note' AND ref=? AND scope=?", (str(nid), project.key))
        return cur.rowcount > 0

    def list_notes(self, project: Project, kind: Optional[str] = None, limit: int = 10) -> list[sqlite3.Row]:
        sql = "SELECT * FROM notes WHERE project=? AND archived=0 AND superseded_by IS NULL"
        args: list[Any] = [project.key]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def stale_refs(self, project: Project, note: sqlite3.Row) -> list[str]:
        """Paths a note was based on that have changed since it was written."""
        changed = []
        for ref in json.loads(note["refs"] or "[]"):
            if not ref.get("sha"):
                continue
            row = self.refresh_file(project, ref["path"])
            if row is None or row["sha"] != ref["sha"]:
                changed.append(ref["path"])
        return changed

    # ---------------------------------------------------------------- search

    def search(self, project: Project, query: str, kinds: Optional[list[str]] = None,
               limit: int = 6) -> list[dict[str, Any]]:
        """Hybrid search over notes and files: FTS5, plus embeddings when enabled.

        `kinds` may include "file" and any note kind. Results from the two
        rankings are merged with reciprocal rank fusion.
        """
        want_files = not kinds or "file" in kinds
        note_kinds = [k for k in (kinds or NOTE_KINDS) if k in NOTE_KINDS]
        ranked: dict[tuple[str, str], float] = {}
        snippets: dict[tuple[str, str], str] = {}
        for rank, (src, ref, snip) in enumerate(self._fts_hits(project, query, limit * 4)):
            k = (src, ref)
            ranked[k] = ranked.get(k, 0) + 1 / (60 + rank)
            snippets.setdefault(k, snip)
        qvec = embed.embed(query) if embed.config() else None
        if qvec is not None:
            sims = []
            for r in self.db.execute("SELECT src, ref, vec FROM vectors WHERE scope IN (?, '*')", (project.key,)):
                sims.append((embed.similarity(qvec, r["vec"]), r["src"], r["ref"]))
            sims.sort(reverse=True)
            for rank, (_, src, ref) in enumerate(sims[: limit * 4]):
                if src == "summary":
                    row = self.db.execute("SELECT path FROM files WHERE root=? AND sha=?",
                                          (project.root, ref)).fetchone()
                    if not row:
                        continue
                    src, ref = "file", row["path"]
                ranked[(src, ref)] = ranked.get((src, ref), 0) + 1 / (60 + rank)
        out = []
        for (src, ref), score in sorted(ranked.items(), key=lambda kv: -kv[1]):
            if src == "file":
                if not want_files:
                    continue
                out.append({"src": "file", "path": ref, "snippet": snippets.get((src, ref), "")})
            else:
                note = self.get_note(project, int(ref))
                if not note or note["archived"] or note["superseded_by"] or note["kind"] not in note_kinds:
                    continue
                out.append({"src": "note", "id": note["id"], "kind": note["kind"], "title": note["title"],
                            "author": note["author"], "created_at": note["created_at"],
                            "snippet": snippets.get((src, ref), ""), "stale": self.stale_refs(project, note)})
            if len(out) >= limit:
                break
        return out

    def _fts_hits(self, project: Project, query: str, limit: int) -> list[tuple[str, str, str]]:
        terms = [t.replace('"', "") for t in query.split() if t.replace('"', "")]
        if not terms:
            return []
        long = [t for t in terms if len(t) >= 3 or not self.trigram]
        short = [t for t in terms if t not in long]
        base = ("SELECT src, ref, snippet(fts, 0, '«', '»', '…', 40) AS snip FROM fts "
                "WHERE scope IN (?, ?)")
        for op in ("AND", "OR"):
            sql, args = base, [project.key, project.root]
            if long:
                sql += " AND fts MATCH ?"
                args.append(f" {op} ".join(f'"{t}"' for t in long))
            if short and (op == "AND" or not long):
                # trigram can't match terms under 3 characters (common in Korean), so fall back to LIKE.
                for t in short:
                    sql += " AND text LIKE ? ESCAPE '\\'"
                    args.append("%" + _like_escape(t) + "%")
            sql += (" ORDER BY rank" if long else "") + " LIMIT ?"
            args.append(limit)
            try:
                rows = self.db.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return [(r["src"], r["ref"], _short_snippet(r["snip"])) for r in rows]
            if len(terms) == 1:
                break
        return []

    # ---------------------------------------------------------------- agents: messages and claims

    def post(self, project: Project, sender: str, body: str, to: Optional[str] = None) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO messages(project, sender, recipient, body, created_at) "
                                  "VALUES(?,?,?,?,?)", (project.key, sender, to or None, body.strip(), time.time()))
        return cur.lastrowid

    def inbox(self, project: Project, agent: str, peek: bool = False, limit: int = 20) -> list[sqlite3.Row]:
        row = self.db.execute("SELECT last_id FROM cursors WHERE project=? AND agent=?",
                              (project.key, agent)).fetchone()
        last = row["last_id"] if row else 0
        rows = self.db.execute("SELECT * FROM messages WHERE project=? AND id>? AND sender<>? ORDER BY id",
                               (project.key, last, agent)).fetchall()
        mine = [r for r in rows if _addressed_to(r["recipient"], agent)][:limit]
        if not peek and rows:
            new_last = mine[-1]["id"] if len(mine) == limit else rows[-1]["id"]
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?)", (project.key, agent, new_last))
        return mine

    def unread_count(self, project: Project, agent: str) -> int:
        return len(self.inbox(project, agent, peek=True, limit=1000))

    def claim(self, project: Project, agent: str, paths: list[str], intent: str,
              minutes: int = 120) -> list[sqlite3.Row]:
        """Record what an agent is working on; return other agents' overlapping claims."""
        now = time.time()
        paths = [p.replace("\\", "/").strip("/") for p in paths]
        with self.db:
            self.db.execute("DELETE FROM claims WHERE expires_at<?", (now,))
            for p in paths:
                self.db.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?,?)",
                                (project.key, agent, p, intent, now, now + minutes * 60))
        others = self.db.execute("SELECT * FROM claims WHERE project=? AND agent<>? AND expires_at>?",
                                 (project.key, agent, now)).fetchall()
        return [c for c in others if any(_overlaps(c["path"], p) for p in paths)]

    def release(self, project: Project, agent: str, paths: Optional[list[str]] = None) -> int:
        with self.db:
            if paths:
                n = 0
                for p in paths:
                    n += self.db.execute("DELETE FROM claims WHERE project=? AND agent=? AND path=?",
                                         (project.key, agent, p.replace("\\", "/").strip("/"))).rowcount
                return n
            return self.db.execute("DELETE FROM claims WHERE project=? AND agent=?",
                                   (project.key, agent)).rowcount

    def active_claims(self, project: Project) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM claims WHERE project=? AND expires_at>? ORDER BY agent, path",
                               (project.key, time.time())).fetchall()


def to_rel(project: Project, path: str) -> Optional[str]:
    """Project-relative forward-slash path, or None if it points outside the project."""
    p = path.strip().strip('"')
    if os.path.isabs(p):
        try:
            p = os.path.relpath(os.path.abspath(p), project.root)
        except ValueError:  # another drive on Windows
            return None
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p == "." or p.startswith("../"):
        return None
    return p


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _short_snippet(s: str) -> str:
    return " ".join((s or "").split())[:240]


def _addressed_to(recipient: Optional[str], agent: str) -> bool:
    if not recipient:
        return True
    return recipient == agent or agent.startswith(recipient + "@")


def _overlaps(a: str, b: str) -> bool:
    if not a or not b:
        return True  # an empty path claims the whole project
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")
