"""SQLite storage: file index, content-addressed summaries, notes, messages, claims.

One database (default ~/.memdex/memdex.db) is shared by every agent and every
project on the machine. WAL mode lets several MCP servers read and write it at
once. Nothing is written inside the project, so nothing ends up in a commit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import embed, keys, scan
from .project import Project

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    root TEXT, path TEXT, size INTEGER, mtime INTEGER, sha TEXT,
    lang TEXT, lines INTEGER, outline TEXT, indexed_at REAL,
    PRIMARY KEY(root, path));
CREATE INDEX IF NOT EXISTS files_sha ON files(sha);
CREATE TABLE IF NOT EXISTS summaries(
    sha TEXT PRIMARY KEY, summary TEXT, author TEXT, created_at REAL, path_hint TEXT,
    use_count INTEGER DEFAULT 0, last_used_at REAL, symbols TEXT, last_used_by TEXT);
CREATE INDEX IF NOT EXISTS summaries_hint ON summaries(path_hint, created_at);
CREATE TABLE IF NOT EXISTS symbols(root TEXT, path TEXT, name TEXT, kind TEXT, line INTEGER, sha TEXT);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(root, path);
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY, project TEXT, kind TEXT, title TEXT, body TEXT,
    author TEXT, refs TEXT, created_at REAL, superseded_by INTEGER, archived INTEGER DEFAULT 0,
    use_count INTEGER DEFAULT 0, last_used_at REAL, pinned INTEGER DEFAULT 0, importance INTEGER DEFAULT 0,
    last_used_by TEXT, confirm_count INTEGER DEFAULT 0, uid TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project, kind, created_at);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, project TEXT, sender TEXT, recipient TEXT, body TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS cursors(project TEXT, agent TEXT, last_id INTEGER, PRIMARY KEY(project, agent));
CREATE TABLE IF NOT EXISTS claims(
    project TEXT, agent TEXT, path TEXT, intent TEXT, created_at REAL, expires_at REAL,
    exclusive INTEGER DEFAULT 0, token INTEGER DEFAULT 0, sha TEXT,
    PRIMARY KEY(project, agent, path));
CREATE TABLE IF NOT EXISTS vectors(src TEXT, ref TEXT, scope TEXT, vec BLOB, PRIMARY KEY(src, ref, scope));
CREATE TABLE IF NOT EXISTS fts_map(rid INTEGER PRIMARY KEY, src TEXT, ref TEXT, scope TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS fts_map_key ON fts_map(src, ref, scope);
CREATE INDEX IF NOT EXISTS fts_map_scope ON fts_map(scope);
"""

NOTE_KINDS = ("note", "decision", "pitfall", "handoff", "todo")

# Bumped whenever SCHEMA changes. Databases carry it in PRAGMA user_version, so a
# newer memdex can refuse to write into a schema it does not understand, and an
# older one can be upgraded instead of failing on a missing column.
SCHEMA_VERSION = 7

# Columns that later versions added. Applied after the migrations as an idempotent top-up, because a
# migration script that alters two columns aborts on the first "duplicate column" and never reaches the
# second. Keeping the list here means a database from any earlier version converges on first open.
LATE_COLUMNS = {
    "claims": (("token", "INTEGER DEFAULT 0"), ("sha", "TEXT")),
    "notes": (("uid", "TEXT"), ("updated_at", "REAL")),
}

# version -> SQL applied when upgrading a database from (version - 1) to version.
# A migration must be safe to run on a fresh database that was just created from
# SCHEMA (so "duplicate column" is ignored, see _migrate_schema).
MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE claims ADD COLUMN exclusive INTEGER DEFAULT 0;",
    # v3 indexes the FTS rows by (src, ref, scope) in a real table. Deleting through FTS's UNINDEXED
    # columns was a full scan per file, which made cold indexing quadratic: 5,000 files took 97 s.
    3: ("CREATE TABLE IF NOT EXISTS fts_map(rid INTEGER PRIMARY KEY, src TEXT, ref TEXT, scope TEXT);"
        "CREATE UNIQUE INDEX IF NOT EXISTS fts_map_key ON fts_map(src, ref, scope);"
        "CREATE INDEX IF NOT EXISTS fts_map_scope ON fts_map(scope);"
        "INSERT OR IGNORE INTO fts_map(rid, src, ref, scope) SELECT rowid, src, ref, scope FROM fts;"),
    # v4: usage signal (how often a memory was actually used later), pinned core notes, an
    # importance the author can set, and per-symbol hashes so staleness is reported per function.
    4: ("ALTER TABLE notes ADD COLUMN use_count INTEGER DEFAULT 0;"
        "ALTER TABLE notes ADD COLUMN last_used_at REAL;"
        "ALTER TABLE notes ADD COLUMN pinned INTEGER DEFAULT 0;"
        "ALTER TABLE notes ADD COLUMN importance INTEGER DEFAULT 0;"
        "ALTER TABLE summaries ADD COLUMN use_count INTEGER DEFAULT 0;"
        "ALTER TABLE summaries ADD COLUMN last_used_at REAL;"
        "ALTER TABLE summaries ADD COLUMN symbols TEXT;"
        "ALTER TABLE symbols ADD COLUMN sha TEXT;"),
    # v5: who used a memory, not just how often. Without it "read by another agent" can only be
    # approximated, and a confirm (reconsolidation) is indistinguishable from an ordinary read.
    5: ("ALTER TABLE notes ADD COLUMN last_used_by TEXT;"
        "ALTER TABLE notes ADD COLUMN confirm_count INTEGER DEFAULT 0;"
        "ALTER TABLE summaries ADD COLUMN last_used_by TEXT;"),
    # v6: a claim is a lease with a fencing token and the content it was taken against, so an edit can be
    # refused when the file moved under the lock instead of quietly clobbering the other agent's work.
    6: "ALTER TABLE claims ADD COLUMN token INTEGER DEFAULT 0;",
    # v7: a note carries a uid that is stable across machines and an updated_at for last-writer-wins, which
    # is what `memdex export --sync` / `memdex merge` exchange.
    7: "ALTER TABLE notes ADD COLUMN uid TEXT;",
}

# Activation: how accessible a memory is, after ACT-R's base-level learning (Anderson & Schooler
# 1991): the sum of t^-0.5 over every past use. memdex keeps two uses per note — creation and the
# last use, with the use count standing in for the ones between — which is enough to rank "used
# often and recently" above "written recently and never used". Pitfalls and decisions get a prior
# because ignoring them costs more than ignoring a note. Below COLD a note stays stored and
# searchable on request but stops spending brief and default-search budget (never deleted).
KIND_PRIOR = {"pitfall": 1.0, "decision": 0.5, "todo": 0.3, "handoff": 0.0, "note": 0.0}
COLD = -3.0


def activation(note, now: Optional[float] = None) -> float:
    """Retrieval strength: how reachable the memory is *now* (ACT-R base-level activation)."""
    now = now or time.time()
    age_c = max((now - (note["created_at"] or now)) / 86400, 0.01)
    last = note["last_used_at"] or note["created_at"] or now
    age_l = max((now - last) / 86400, 0.01)
    uses = max(int(note["use_count"] or 0), 0)
    base = math.log(age_c ** -0.5 + uses * age_l ** -0.5)
    return base + KIND_PRIOR.get(note["kind"], 0.0) + 0.5 * max(0, int(note["importance"] or 0) - 1)


def _suppression(note, suppressed: set, penalty: float = 0.3) -> float:
    """How much of a note's evidence has been superseded elsewhere: a small, proportional rank penalty."""
    if not suppressed:
        return 0.0
    try:
        paths = {str(r.get("path")) for r in json.loads(note["refs"] or "[]") if r.get("path")}
    except (json.JSONDecodeError, TypeError):
        return 0.0
    if not paths:
        return 0.0
    return penalty * len(paths & suppressed) / len(paths)


def storage_strength(note) -> float:
    """Storage strength: how well learned a memory is, independent of when it was last needed.

    Bjork & Bjork (1992) separate the two, and the separation matters here: a decision that forty sessions
    used and this month's did not is not a *cold* memory, it is a well-learned one that is temporarily
    unreachable. Retrieval strength alone would tier it out; storage strength is what keeps a
    well-connected old note in front of the next session.
    """
    uses = float(note["use_count"] or 0)
    confirms = float(note["confirm_count"] or 0)  # a confirm means the code moved and it still held
    importance = max(0, int(note["importance"] or 0) - 1)
    return math.log1p(uses + 2 * confirms) + 0.5 * importance + KIND_PRIOR.get(note["kind"], 0.0)


# Well-learned enough to stay in the brief even when retrieval strength has decayed: ~2 uses of a plain
# note, or one use of a pitfall (which already carries a prior).
STORAGE_HOT = 2.0


def _add_missing_columns(db, table_columns: dict) -> None:
    """Idempotent top-up for columns that a later version added (see LATE_COLUMNS)."""
    for table, columns in table_columns.items():
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    db.commit()


@dataclass
class ClaimResult:
    granted: bool
    overlapping: list  # other agents' active claims touching the requested paths
    blocking: list     # the subset that refused the claim (exclusive on either side)
    token: int = 0     # fencing token: the grant's number, increasing per project

# Files per write transaction while indexing. Small enough that another agent never waits long for the
# single writer slot, large enough that committing stays cheap on a 5,000-file repo.
INDEX_COMMIT_EVERY = 50


def default_path() -> str:
    home = os.environ.get("MEMDEX_HOME") or os.path.join(Path.home(), ".memdex")
    return os.path.join(home, "memdex.db")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        # Who is using this store, when the caller does not say. A Session sets it to its own agent id, so
        # reads/confirms are attributed without threading an argument through every call site.
        self.default_agent = ""
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Several processes may create the database at the same moment. The busy
        # timeout does not cover the journal-mode switch, so retry it by hand.
        _retry(lambda: self.db.execute("PRAGMA journal_mode=WAL"))
        self.db.execute("PRAGMA synchronous=NORMAL")
        _retry(lambda: self.db.executescript(SCHEMA))
        # FTS first: a migration may reference it (v3 backfills fts_map from the FTS rows).
        self.trigram = _retry(self._make_fts)
        self.schema_version = _retry(self._migrate_schema)
        _retry(lambda: _add_missing_columns(self.db, LATE_COLUMNS))
        _retry(self._backfill_note_ids)
        self.db.commit()

    def _backfill_note_ids(self) -> None:
        """Give every existing note a uid and an updated_at; then uid is unique from here on."""
        rows = self.db.execute("SELECT id FROM notes WHERE uid IS NULL OR uid=''").fetchall()
        for row in rows:
            self.db.execute("UPDATE notes SET uid=? WHERE id=?", (uuid.uuid4().hex, row["id"]))
        self.db.execute("UPDATE notes SET updated_at=created_at WHERE updated_at IS NULL")
        self.db.commit()
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS notes_uid ON notes(uid)")

    def _migrate_schema(self) -> int:
        """Bring PRAGMA user_version up to SCHEMA_VERSION.

        Databases created before versioning report 0 and already contain the v1
        tables, so 0 is simply stamped as 1. A database written by a newer memdex
        is left untouched: silently writing into a schema this code cannot see is
        how data gets corrupted.
        """
        current = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current == SCHEMA_VERSION:
            return current
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current} is newer than this memdex (v{SCHEMA_VERSION}); "
                f"upgrade memdex: pip install -U memdex-mcp")
        if current <= 0:
            # fresh database (tables just created from SCHEMA, already in the latest
            # shape) or one made before user_version was tracked (v1 shape): both
            # go through the migrations, which tolerate columns that already exist.
            current = 1
        for version in range(current + 1, SCHEMA_VERSION + 1):
            sql = MIGRATIONS.get(version)
            if sql:
                try:
                    self.db.executescript(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
            self.db.execute(f"PRAGMA user_version={version}")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return SCHEMA_VERSION

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
        """Replace the search row for (src, ref, scope).

        The delete goes through fts_map's row id, not through FTS's UNINDEXED columns: filtering on those is
        a full scan of the FTS table, and doing it once per file made cold indexing O(n^2).
        """
        row = self.db.execute("SELECT rid FROM fts_map WHERE src=? AND ref=? AND scope=?",
                              (src, ref, scope)).fetchone()
        if row is not None:
            self.db.execute("DELETE FROM fts WHERE rowid=?", (row["rid"],))
            self.db.execute("DELETE FROM fts_map WHERE rid=?", (row["rid"],))
        if text:
            cur = self.db.execute("INSERT INTO fts(text) VALUES(?)", (text,))
            self.db.execute("INSERT OR REPLACE INTO fts_map(rid, src, ref, scope) VALUES(?,?,?,?)",
                            (cur.lastrowid, src, ref, scope))

    def _vec_put(self, src: str, ref: str, scope: str, text: str) -> None:
        vec = embed.embed(text)
        if vec is not None:
            self.db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (src, ref, scope, vec))

    def put_vector(self, src: str, ref: str, scope: str, vec: Optional[bytes]) -> bool:
        """Store a vector the caller computed (see `embed.normalize`). No model is called here."""
        if not vec:
            return False
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (src, ref, scope, vec))
        return True

    def _file_text(self, path: str, outline: str, sha: str) -> str:
        s = self.db.execute("SELECT summary FROM summaries WHERE sha=?", (sha,)).fetchone()
        # keys.expand appends split identifiers and path words ("Store.index" → "store index"), so a
        # query in plain words finds the symbol (key expansion, as in SPLADE/LongMemEval, no model).
        return keys.expand("\n".join(x for x in (path, s["summary"] if s else "", outline) if x))

    # ---------------------------------------------------------------- files

    def index(self, project: Project, budget: Optional[int] = None) -> dict[str, Any]:
        """Bring the file index up to date. Only files whose stat moved are read.

        `budget` caps how many files this call reads, so a call an agent is waiting on (the brief) returns
        in bounded time on a huge or freshly cloned checkout: the rest is reported as `pending` and the
        next call picks it up. `memdex index` passes no budget and always finishes the scan.

        The write transaction is committed every INDEX_COMMIT_EVERY files instead of once at the end. On a
        5,000-file repo the whole scan takes seconds; holding the write lock that long makes every *other*
        agent's write fail with "database is locked" — the shared database has one writer at a time.
        """
        known = {r["path"]: (r["size"], r["mtime"], r["sha"]) for r in
                 self.db.execute("SELECT path, size, mtime, sha FROM files WHERE root=?", (project.root,))}
        current = scan.list_files(project)
        stats: dict[str, Any] = {"files": len(current), "changed": 0, "removed": 0, "skipped": 0,
                                 "pending": 0, "changed_paths": []}  # first few changed paths, for the brief
        # A stat is cheap and a read is not, so the budget counts files that need reading: an unchanged
        # checkout stays a single pass of stats even when the budget is small.
        todo = [(rel, known.get(rel)) for rel in current if not self._unchanged(project, rel, known.get(rel))]
        since_commit = 0
        try:
            for rel in set(known) - set(current):
                self._drop_file(project.root, rel)
                stats["removed"] += 1
                since_commit += 1
                if since_commit >= INDEX_COMMIT_EVERY:
                    self.db.commit()
                    since_commit = 0
            for done, (rel, prev) in enumerate(todo):
                if budget is not None and budget > 0 and done >= budget:
                    stats["pending"] = len(todo) - done
                    break
                result = self._index_file(project, rel, prev)
                if result == "changed":
                    stats["changed"] += 1
                    if len(stats["changed_paths"]) < 20:
                        stats["changed_paths"].append(rel)
                elif result == "skipped":
                    stats["skipped"] += 1
                since_commit += 1
                if since_commit >= INDEX_COMMIT_EVERY:
                    self.db.commit()
                    since_commit = 0
        finally:
            self.db.commit()
        return stats

    @staticmethod
    def _unchanged(project: Project, rel: str, prev: Optional[tuple]) -> bool:
        """True when the stat still matches the indexed row, so reading the file would prove nothing new."""
        if prev is None:
            return False
        try:
            st = os.stat(os.path.join(project.root, rel))
        except OSError:
            return False
        return prev[0] == st.st_size and prev[1] == st.st_mtime_ns

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
        self.db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?)",
                            [(project.root, rel, n, k, ln, h) for n, k, ln, h in symbol_shas(ol.symbols, text)])
        self._fts_put("file", rel, project.root, self._file_text(rel, rendered, sha))
        return "changed"

    def symbol_map(self, project: Project, rel: str) -> dict[str, str]:
        """{symbol name: span hash} for the file's current content."""
        return {r["name"]: r["sha"] for r in self.db.execute(
            "SELECT name, sha FROM symbols WHERE root=? AND path=? AND sha IS NOT NULL", (project.root, rel))}

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

    def file_info(self, project: Project, rel: str, agent: Optional[str] = None) -> Optional[dict[str, Any]]:
        row = self.refresh_file(project, rel)
        if row is None:
            return None
        s = self.db.execute("SELECT summary, author, created_at FROM summaries WHERE sha=?",
                            (row["sha"],)).fetchone()
        info = {"path": row["path"], "sha": row["sha"], "lines": row["lines"], "lang": row["lang"],
                "outline": row["outline"], "summary": None, "stale_summary": None, "symbol_diff": None}
        if s:
            info["summary"] = dict(s)
            # Served fresh: the summary did its job. This is the usage signal behind activation.
            with self.db:
                self.db.execute("UPDATE summaries SET use_count=use_count+1, last_used_at=?, last_used_by=? "
                                "WHERE sha=?", (time.time(), _agent_hint(agent, project, self.default_agent), row["sha"]))
        else:
            old = self.db.execute(
                "SELECT summary, author, created_at, symbols FROM summaries WHERE path_hint=? "
                "ORDER BY created_at DESC LIMIT 1", (f"{project.key}::{row['path']}",)).fetchone()
            if old:
                info["stale_summary"] = dict(old)
                info["symbol_diff"] = _symbol_diff(json.loads(old["symbols"] or "{}"),
                                                   self.symbol_map(project, row["path"]))
        return info

    def save_summary(self, project: Project, rel: str, summary: str, author: str) -> Optional[str]:
        row = self.refresh_file(project, rel)
        if row is None:
            return None
        sha = row["sha"]
        with self.db:
            # The symbol hashes travel with the summary, so a later edit can be described as "these two
            # functions changed" instead of "stale".
            self.db.execute(
                "INSERT OR REPLACE INTO summaries(sha, summary, author, created_at, path_hint, use_count, "
                "last_used_at, symbols) VALUES(?,?,?,?,?,0,NULL,?)",
                (sha, summary.strip(), author, time.time(), f"{project.key}::{row['path']}",
                 json.dumps(self.symbol_map(project, row["path"]))))
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

    def _make_refs(self, project: Project, files: list[str]) -> list[dict[str, Any]]:
        """File refs with current hashes. "path#Symbol" pins the ref to one symbol's span, so the
        note is flagged only when that function changes, not when anything in the file does."""
        refs = []
        for f in files:
            path, _, symbol = str(f).partition("#")
            row = self.refresh_file(project, path)
            ref: dict[str, Any] = {"path": row["path"] if row else path, "sha": row["sha"] if row else None}
            if symbol.strip():
                ref["symbol"] = symbol.strip()
                ref["symbol_sha"] = self.symbol_map(project, ref["path"]).get(ref["symbol"]) if row else None
            refs.append(ref)
        return refs

    def add_note(self, project: Project, kind: str, title: str, body: str, author: str,
                 files: Optional[list[str]] = None, supersedes: Optional[int] = None,
                 importance: int = 0, pinned: bool = False) -> int:
        refs = self._make_refs(project, files or [])
        with self.db:
            now = time.time()
            cur = self.db.execute(
                "INSERT INTO notes(project, kind, title, body, author, refs, created_at, updated_at, "
                "importance, pinned, uid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project.key, kind, title.strip(), body.strip(), author, json.dumps(refs), now, now,
                 max(0, min(int(importance or 0), 5)), int(bool(pinned)), uuid.uuid4().hex))
            nid = cur.lastrowid
            # The paths and symbols a note cites are part of its key: searching a file name finds the notes about it.
            cited = " ".join(f"{r['path']} {r.get('symbol', '')}" for r in refs)
            self._fts_put("note", str(nid), project.key, keys.expand(f"{title}\n{body}\n{cited}"))
            if supersedes:
                self.db.execute("UPDATE notes SET superseded_by=? WHERE id=? AND project=?",
                                (nid, supersedes, project.key))
                self._fts_put("note", str(supersedes), project.key, "")
            self._vec_put("note", str(nid), project.key, f"{title}\n{body}")
        return nid

    def count_notes(self, project: Project, kind: Optional[str] = None) -> int:
        sql = "SELECT count(*) FROM notes WHERE project=? AND archived=0"
        args: list = [project.key]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return int(self.db.execute(sql, args).fetchone()[0])

    def get_note(self, project: Project, nid: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM notes WHERE id=? AND project=?", (nid, project.key)).fetchone()

    def archive_note(self, project: Project, nid: int) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE notes SET archived=1, updated_at=? WHERE id=? AND project=?",
                                  (time.time(), nid, project.key))
            self._fts_put("note", str(nid), project.key, "")
            self.db.execute("DELETE FROM vectors WHERE src='note' AND ref=? AND scope=?", (str(nid), project.key))
        return cur.rowcount > 0

    def list_notes(self, project: Project, kind: Optional[str] = None, limit: int = 10,
                   include_cold: bool = False) -> list[sqlite3.Row]:
        """Active notes, most accessible first (activation), cold ones left out unless asked.
        Handoffs are the exception: the newest one is what the next session needs."""
        sql = "SELECT * FROM notes WHERE project=? AND archived=0 AND superseded_by IS NULL"
        args: list[Any] = [project.key]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.db.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        if kind == "handoff":
            return rows[:limit]
        now = time.time()
        suppressed = self._suppressed_paths(project)
        scored = [(activation(r, now) - _suppression(r, suppressed), r) for r in rows]
        if not include_cold:
            # Retrieval strength OR storage strength keeps a note in the brief: a well-learned note that
            # nobody needed this month is not cold, it is temporarily hard to reach (Bjork & Bjork 1992).
            hot = [(a, r) for a, r in scored
                   if a >= COLD or r["pinned"] or storage_strength(r) >= STORAGE_HOT]
            scored = hot or scored[:limit]  # a project with only cold notes still shows something
        scored.sort(key=lambda ar: (-ar[0], -ar[1]["created_at"]))
        return [r for _, r in scored[:limit]]

    def _suppressed_paths(self, project: Project) -> set:
        """Files that a superseded note was about.

        Retrieval-induced forgetting (Anderson, Bjork & Bjork 1994): retrieving one memory suppresses its
        competitors. Here the "retrieval" is the supersession — somebody worked out that the note on these
        files was wrong — so the notes that share those files are less likely to still hold, and rank a
        little lower rather than being hidden.
        """
        paths: set = set()
        for row in self.db.execute("SELECT refs FROM notes WHERE project=? AND superseded_by IS NOT NULL",
                                   (project.key,)):
            try:
                for ref in json.loads(row["refs"] or "[]"):
                    if ref.get("path"):
                        paths.add(str(ref["path"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return paths

    def pinned_notes(self, project: Project) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM notes WHERE project=? AND archived=0 AND superseded_by IS NULL "
                               "AND pinned=1 ORDER BY created_at DESC", (project.key,)).fetchall()

    def touch_note(self, project: Project, nid: int, agent: Optional[str] = None) -> None:
        """A note was read in full by a later session: that is a use (the testing effect).

        `agent` (default: MEMDEX_AGENT) is recorded so "read by someone other than the author" can be
        counted instead of guessed.
        """
        with self.db:
            self.db.execute("UPDATE notes SET use_count=use_count+1, last_used_at=?, last_used_by=? "
                            "WHERE id=? AND project=?",
                            (time.time(), _agent_hint(agent, project, self.default_agent), nid, project.key))

    def set_pinned(self, project: Project, nid: int, flag: bool) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE notes SET pinned=?, updated_at=? WHERE id=? AND project=?",
                                  (int(flag), time.time(), nid, project.key))
        return cur.rowcount > 0

    def confirm_note(self, project: Project, nid: int, agent: Optional[str] = None) -> int:
        """Reconsolidation: the reader checked the note against the current code and says it still
        holds, so its refs are re-stamped to the current hashes and the ⚠ goes away.

        Counted separately from a plain read: confirms are exactly the notes whose code moved under them,
        so `sum(confirm_count)` is "memories healed after a change".
        """
        note = self.get_note(project, nid)
        if not note:
            return -1
        refs = json.loads(note["refs"] or "[]")
        files = [f"{r['path']}#{r['symbol']}" if r.get("symbol") else r["path"] for r in refs]
        new_refs = self._make_refs(project, files)
        with self.db:
            self.db.execute("UPDATE notes SET refs=?, use_count=use_count+1, last_used_at=?, "
                            "last_used_by=?, confirm_count=confirm_count+1 "
                            "WHERE id=? AND project=?",
                            (json.dumps(new_refs), time.time(), _agent_hint(agent, project, self.default_agent), nid, project.key))
        return len(new_refs)

    def stale_refs(self, project: Project, note: sqlite3.Row) -> list[str]:
        """Refs a note was based on that have changed since: "path" for whole-file refs,
        "path#Symbol" when only that symbol's span is compared."""
        changed = []
        for ref in json.loads(note["refs"] or "[]"):
            if not ref.get("sha"):
                continue
            row = self.refresh_file(project, ref["path"])
            if ref.get("symbol") and ref.get("symbol_sha"):
                current = self.symbol_map(project, ref["path"]).get(ref["symbol"]) if row else None
                if current != ref["symbol_sha"]:
                    changed.append(f"{ref['path']}#{ref['symbol']}")
            elif row is None or row["sha"] != ref["sha"]:
                changed.append(ref["path"])
        return changed

    def experts(self, project: Project, limit: int = 6) -> list[tuple[str, str, int]]:
        """Who knows what: (top-level dir, agent, count) from who wrote the summaries and the notes
        about files there (transactive memory — route the question instead of broadcasting it)."""
        counts: dict[tuple[str, str], int] = {}
        for r in self.db.execute("SELECT f.path AS path, s.author AS author FROM files f "
                                 "JOIN summaries s ON s.sha=f.sha WHERE f.root=?", (project.root,)):
            counts[(_top_dir(r["path"]), r["author"])] = counts.get((_top_dir(r["path"]), r["author"]), 0) + 1
        for r in self.db.execute("SELECT author, refs FROM notes WHERE project=? AND archived=0", (project.key,)):
            for ref in json.loads(r["refs"] or "[]"):
                key = (_top_dir(ref.get("path", "")), r["author"])
                counts[key] = counts.get(key, 0) + 1
        best: dict[str, tuple[str, int]] = {}
        for (d, agent), n in counts.items():
            if n >= 2 and (d not in best or n > best[d][1]):
                best[d] = (agent, n)
        return sorted(((d, a, n) for d, (a, n) in best.items()), key=lambda x: -x[2])[:limit]

    # ---------------------------------------------------------------- search

    def search(self, project: Project, query: str, kinds: Optional[list[str]] = None,
               limit: int = 6, qvec: Optional[bytes] = None) -> list[dict[str, Any]]:
        """Hybrid search over notes and files: FTS5, plus embeddings when available.

        `kinds` may include "file" and any note kind. Results from the rankings are merged with reciprocal
        rank fusion. `qvec` is a query vector the caller computed; without it the configured local model is
        asked, and with neither the search is keyword-only.
        """
        want_files = not kinds or "file" in kinds
        note_kinds = [k for k in (kinds or NOTE_KINDS) if k in NOTE_KINDS]
        ranked: dict[tuple[str, str], float] = {}
        snippets: dict[tuple[str, str], str] = {}
        first = self._fts_hits(project, query, limit * 4)
        for rank, (src, ref, snip) in enumerate(first):
            k = (src, ref)
            ranked[k] = ranked.get(k, 0) + 1 / (60 + rank)
            snippets.setdefault(k, snip)
        # A second query built from what the first one found. Half weight: it may add a result the query
        # did not literally match, but it must never outrank the literal match that produced the terms.
        if first:
            feedback = self._feedback_terms(first, exclude=set(re.findall(r"\w+", query.lower())))
            if feedback:
                for rank, (src, ref, snip) in enumerate(
                        self._fts_hits(project, " ".join(feedback), limit * 4, op_first="OR")):
                    k = (src, ref)
                    ranked[k] = ranked.get(k, 0) + 0.5 / (60 + rank)
                    snippets.setdefault(k, snip)
        if qvec is None and embed.config():
            qvec = embed.embed(query)
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
        # Third ranking, over the candidates only: activation (used often and recently). It re-orders
        # what matched; it never adds a note the query did not match.
        now = time.time()
        notes = {}
        for (src, ref) in ranked:
            if src == "note":
                n = self.get_note(project, int(ref))
                if n:
                    notes[ref] = n
        by_activation = sorted(notes, key=lambda ref: -activation(notes[ref], now))
        for rank, ref in enumerate(by_activation):
            if activation(notes[ref], now) >= COLD or notes[ref]["pinned"]:
                ranked[("note", ref)] += 1 / (60 + rank)
        out = []
        for (src, ref), score in sorted(ranked.items(), key=lambda kv: -kv[1]):
            if src == "file":
                if not want_files:
                    continue
                out.append({"src": "file", "path": ref, "snippet": snippets.get((src, ref), "")})
            else:
                note = notes.get(ref)
                if not note or note["archived"] or note["superseded_by"] or note["kind"] not in note_kinds:
                    continue
                out.append({"src": "note", "id": note["id"], "kind": note["kind"], "title": note["title"],
                            "author": note["author"], "created_at": note["created_at"],
                            "snippet": snippets.get((src, ref), ""), "stale": self.stale_refs(project, note),
                            "pinned": bool(note["pinned"])})
            if len(out) >= limit:
                break
        return out

    def _hit_text(self, src: str, ref: str) -> str:
        """The indexed text of one hit, for the feedback pass."""
        row = self.db.execute(
            "SELECT f.text FROM fts f JOIN fts_map m ON m.rid = f.rowid "
            "WHERE m.src=? AND m.ref=? LIMIT 1", (src, str(ref))).fetchone()
        return row[0] if row else ""

    def _feedback_terms(self, hits: list, limit: int = 8, exclude=()) -> list[str]:
        """RM3-lite (Lavrenko & Croft 2001): the words the top hits repeat, minus the ones everywhere.

        Pure counting, no model: the relevance model is "what the top three results are about", estimated
        as term frequency inside them. Inverse document frequency is used as a *filter*, not a bonus — a
        word that appears in half the index distinguishes nothing, and it is exactly the word that would
        otherwise flood the expansion query. No generative model is involved.
        """
        counts: dict[str, int] = {}
        for src, ref, _ in hits[:3]:
            for term in re.findall(r"[^\W\d_]{4,}", self._hit_text(src, ref).lower()):
                if term in exclude:
                    continue
                counts[term] = counts.get(term, 0) + 1
        if not counts:
            return []
        total = self.db.execute("SELECT count(*) FROM fts").fetchone()[0] or 1
        scored = []
        for term, tf in counts.items():
            df = self.db.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (f'"{term}"',)).fetchone()[0]
            if total >= 5 and df / total >= 0.5:
                continue  # everywhere: a stopword in disguise, or the project's own name
            scored.append((tf, term))
        # Repeated inside the hits first, then the longer word (more specific), then alphabetically, so the
        # expansion query is the same on every run.
        scored.sort(key=lambda tv: (-tv[0], -len(tv[1]), tv[1]))
        return [term for _, term in scored[:limit]]

    def _fts_hits(self, project: Project, query: str, limit: int, op_first: str = "AND",
                  ) -> list[tuple[str, str, str]]:
        terms = [t.replace('"', "") for t in keys.expand(query).split() if t.replace('"', "")]
        if not terms:
            return []
        long = [t for t in terms if len(t) >= 3 or not self.trigram]
        short = [t for t in terms if t not in long]
        base = ("SELECT m.src AS src, m.ref AS ref, snippet(fts, 0, '«', '»', '…', 40) AS snip "
                "FROM fts JOIN fts_map m ON m.rid = fts.rowid WHERE m.scope IN (?, ?)")
        # An expansion query is a list of alternatives, so it is tried with OR first; the caller's own
        # query wants the narrow AND first.
        for op in ((op_first,) + tuple(o for o in ("AND", "OR") if o != op_first)):
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
              minutes: int = 120, exclusive: bool = False, force: bool = False,
              refresh: bool = False) -> ClaimResult:
        """Record what an agent is working on.

        An exclusive claim is a lock: it is refused while another agent holds any
        overlapping claim, and it refuses other agents' claims in turn. A plain claim
        is advisory: only someone else's exclusive claim refuses it. `force` records
        the claim anyway and just reports the overlaps. A path of '' is the whole
        project. Nothing is written when the claim is refused.

        Every grant carries a fencing token (the next number for this project) and the sha of the file it
        was taken against. `refresh` re-stamps both on claims this agent already holds — the "I re-read the
        file, the lock is still mine" move that `hook pre-edit` asks for.
        """
        now = time.time()
        paths = [_claim_path(p) for p in paths]
        with self.db:
            self.db.execute("DELETE FROM claims WHERE expires_at<?", (now,))
            if refresh:
                token = self._next_token(project)
                for p in paths:
                    self.db.execute(
                        "UPDATE claims SET expires_at=?, token=?, sha=? "
                        "WHERE project=? AND agent=? AND path=?",
                        (now + minutes * 60, token, self._claim_sha(project, p), project.key, agent, p))
                row = self.db.execute("SELECT count(*) FROM claims WHERE project=? AND agent=? AND token=?",
                                      (project.key, agent, token)).fetchone()
                return ClaimResult(bool(row[0]), [], [], token)
            others = self.db.execute("SELECT * FROM claims WHERE project=? AND agent<>? AND expires_at>?",
                                     (project.key, agent, now)).fetchall()
            overlapping = [c for c in others if any(_overlaps(c["path"], p) for p in paths)]
            blocking = [c for c in overlapping if exclusive or c["exclusive"]]
            if blocking and not force:
                return ClaimResult(False, overlapping, blocking)
            token = self._next_token(project)
            for p in paths:
                self.db.execute(
                    "INSERT OR REPLACE INTO claims(project, agent, path, intent, created_at, expires_at, "
                    "exclusive, token, sha) VALUES(?,?,?,?,?,?,?,?,?)",
                    (project.key, agent, p, intent, now, now + minutes * 60, int(bool(exclusive)), token,
                     self._claim_sha(project, p)))
        return ClaimResult(True, overlapping, blocking, token)

    def _next_token(self, project: Project) -> int:
        """Fencing token: strictly increasing among the live claims of one project."""
        row = self.db.execute("SELECT COALESCE(MAX(token), 0) FROM claims WHERE project=?",
                              (project.key,)).fetchone()
        return int(row[0]) + 1

    def _claim_sha(self, project: Project, path: str) -> Optional[str]:
        """The content a claim was taken against; None for a directory or the whole project."""
        if not path:
            return None
        full = os.path.join(project.root, path)
        if not os.path.isfile(full):
            return None
        try:
            with open(full, "rb") as f:
                return scan.sha256(f.read())
        except OSError:
            return None

    def claim_sha(self, project: Project, rel: str) -> Optional[str]:
        """Current sha of a claimed path, for the pre-edit check."""
        return self._claim_sha(project, _claim_path(rel))

    def release(self, project: Project, agent: str, paths: Optional[list[str]] = None) -> int:
        with self.db:
            if paths:
                n = 0
                for p in paths:
                    n += self.db.execute("DELETE FROM claims WHERE project=? AND agent=? AND path=?",
                                         (project.key, agent, _claim_path(p))).rowcount
                return n
            return self.db.execute("DELETE FROM claims WHERE project=? AND agent=?",
                                   (project.key, agent)).rowcount

    def active_claims(self, project: Project) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM claims WHERE project=? AND expires_at>? ORDER BY agent, path",
                               (project.key, time.time())).fetchall()

    def claims_on(self, project: Project, rel: str, agent: str) -> list[sqlite3.Row]:
        """Other agents' active claims that cover `rel` ('' = the whole project)."""
        rel = _claim_path(rel)
        return [c for c in self.active_claims(project) if c["agent"] != agent and _overlaps(c["path"], rel)]


def _retry(fn, attempts: int = 50):
    """Run fn, retrying on 'database is locked' with short random sleeps (up to ~5 s)."""
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == attempts - 1:
                raise
            time.sleep(0.02 + random.random() * 0.1)


def _agent_hint(agent: Optional[str] = None, project: Optional[Project] = None,
                default: Optional[str] = None) -> str:
    """Who is using a memory, in the same shape as notes.author ("who@checkout").

    Falls back to the caller's default (a session sets Store.default_agent), then MEMDEX_AGENT — how MCP
    clients configure memdex — so a bare Store still records something useful.
    """
    who = (agent or default or os.environ.get("MEMDEX_AGENT") or "unknown").strip().lower().replace(" ", "-")
    if not who:
        who = "unknown"
    if project is not None and "@" not in who:
        who = f"{who}@{Path(project.root).name}"
    return who


def to_rel(project: Project, path: str) -> Optional[str]:
    """Project-relative forward-slash path, or None if it points outside the project."""
    p = path.strip().strip('"')
    if os.path.isabs(p):
        try:
            # realpath: project.root is resolved, so /var → /private/var on macOS must be too.
            p = os.path.relpath(os.path.realpath(p), project.root)
        except ValueError:  # another drive on Windows
            return None
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p == "." or p.startswith("../"):
        return None
    return p


def symbol_shas(symbols: list[tuple[str, str, int]], text: str) -> list[tuple[str, str, int, str]]:
    """(name, kind, line, hash) with each symbol's span hashed: from its line to the line before the
    next symbol (Merkle-style leaves under the file's sha). A class line is its own one-line leaf;
    its methods are leaves of their own, so editing one method moves one hash."""
    lines = text.splitlines()
    ordered = sorted(symbols, key=lambda s: s[2])
    out = []
    for i, (name, kind, line) in enumerate(ordered):
        start = max(line - 1, 0)
        end = ordered[i + 1][2] - 1 if i + 1 < len(ordered) else len(lines)
        span = "\n".join(lines[start:max(end, start + 1)])
        out.append((name, kind, line, hashlib.sha256(span.encode("utf-8", "replace")).hexdigest()[:16]))
    return out


def _symbol_diff(old: dict[str, str], new: dict[str, str]) -> Optional[dict[str, Any]]:
    """What moved between the symbol map a summary was written against and the current one."""
    if not old:
        return None
    changed = sorted(n for n in old if n in new and old[n] != new[n])
    added = sorted(n for n in new if n not in old)
    removed = sorted(n for n in old if n not in new)
    return {"changed": changed, "added": added, "removed": removed,
            "unchanged": len(old) - len(changed) - len(removed), "total": len(old)}


def _top_dir(path: str) -> str:
    return path.split("/", 1)[0] + "/" if "/" in path else "."


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _short_snippet(s: str) -> str:
    return " ".join((s or "").split())[:240]


def _addressed_to(recipient: Optional[str], agent: str) -> bool:
    if not recipient:
        return True
    return recipient == agent or agent.startswith(recipient + "@")


def _claim_path(p: str) -> str:
    """Claim paths are project-relative with forward slashes; '' means the whole project."""
    p = str(p).strip().replace("\\", "/").strip("/")
    while p.startswith("./"):
        p = p[2:]
    return "" if p in ("", ".", "*") else p


def _overlaps(a: str, b: str) -> bool:
    if not a or not b:
        return True  # an empty path claims the whole project
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


