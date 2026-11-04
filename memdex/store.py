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
    archived_at REAL,
    use_count INTEGER DEFAULT 0, last_used_at REAL, pinned INTEGER DEFAULT 0, importance INTEGER DEFAULT 0,
    last_used_by TEXT, confirm_count INTEGER DEFAULT 0, uid TEXT, updated_at REAL,
    valid_from REAL, valid_until REAL, outcome TEXT, context TEXT, steps TEXT DEFAULT '[]', evidence TEXT);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project, kind, created_at);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, project TEXT, sender TEXT, recipient TEXT, body TEXT, created_at REAL,
    sender_route TEXT, recipient_route TEXT, reply_to INTEGER, request_key TEXT, request_hash TEXT);
CREATE TABLE IF NOT EXISTS chat_routes(
    project TEXT NOT NULL, route TEXT NOT NULL, agent TEXT NOT NULL, checkout TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '', last_seen_at REAL NOT NULL, PRIMARY KEY(project,route));
CREATE TABLE IF NOT EXISTS cursors(project TEXT, agent TEXT, last_id INTEGER, PRIMARY KEY(project, agent));
CREATE TABLE IF NOT EXISTS inbox_delivery(
    project TEXT, agent TEXT, next_notify_at REAL NOT NULL, PRIMARY KEY(project, agent));
CREATE TABLE IF NOT EXISTS sessions(
    project TEXT, agent TEXT, started_at REAL, last_seen_at REAL, PRIMARY KEY(project, agent));
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY, project TEXT NOT NULL, parent_id INTEGER, title TEXT NOT NULL,
    criterion TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN (
        'todo','in_progress','blocked','reported','self_verified','verified')),
    blocker TEXT NOT NULL DEFAULT '', report_evidence TEXT NOT NULL DEFAULT '',
    verification_evidence TEXT NOT NULL DEFAULT '', reporter TEXT NOT NULL DEFAULT '',
    verifier TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, reported_at REAL, verified_at REAL);
CREATE INDEX IF NOT EXISTS tasks_project_parent ON tasks(project, parent_id, id);
CREATE INDEX IF NOT EXISTS tasks_project_status ON tasks(project, status, updated_at);
CREATE TABLE IF NOT EXISTS task_events(
    id INTEGER PRIMARY KEY, project TEXT NOT NULL, task_id INTEGER NOT NULL,
    actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(project, task_id, id);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, canonical TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS claims(
    project TEXT, agent TEXT, path TEXT, intent TEXT, created_at REAL, expires_at REAL,
    exclusive INTEGER DEFAULT 0, token INTEGER DEFAULT 0, sha TEXT,
    PRIMARY KEY(project, agent, path));
CREATE TABLE IF NOT EXISTS vectors(src TEXT, ref TEXT, scope TEXT, vec BLOB, PRIMARY KEY(src, ref, scope));
CREATE TABLE IF NOT EXISTS fts_map(rid INTEGER PRIMARY KEY, src TEXT, ref TEXT, scope TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS fts_map_key ON fts_map(src, ref, scope);
CREATE INDEX IF NOT EXISTS fts_map_scope ON fts_map(scope);
"""

NOTE_KINDS = ("note", "decision", "pitfall", "handoff", "todo", "experience")

# Bumped whenever SCHEMA changes. Databases carry it in PRAGMA user_version, so a
# newer memdex can refuse to write into a schema it does not understand, and an
# older one can be upgraded instead of failing on a missing column.
SCHEMA_VERSION = 15

# Columns that later versions added. Applied after the migrations as an idempotent top-up, because a
# migration script that alters two columns aborts on the first "duplicate column" and never reaches the
# second. Keeping the list here means a database from any earlier version converges on first open.
LATE_COLUMNS = {
    "messages": (("sender_route", "TEXT"), ("recipient_route", "TEXT"), ("reply_to", "INTEGER"),
                 ("request_key", "TEXT"), ("request_hash", "TEXT")),
    "claims": (("token", "INTEGER DEFAULT 0"), ("sha", "TEXT")),
    "notes": (("uid", "TEXT"), ("updated_at", "REAL"), ("valid_from", "REAL"),
              ("valid_until", "REAL"), ("outcome", "TEXT"), ("context", "TEXT"),
              ("steps", "TEXT DEFAULT '[]'"), ("evidence", "TEXT")),
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
    # v8: superseded notes remain in the search index so a point-in-time query can recover what agents
    # considered current before a later note replaced it. The backfill is done in Python after this marker.
    8: "SELECT 1;",
    # v9: keep the archive event separate from updated_at, which also changes for pinning and sync edits.
    9: "ALTER TABLE notes ADD COLUMN archived_at REAL;",
    # v10: explicit domain-validity and structured procedural experiences, separate from record time.
    10: ("ALTER TABLE notes ADD COLUMN valid_from REAL;"
         "ALTER TABLE notes ADD COLUMN valid_until REAL;"
         "ALTER TABLE notes ADD COLUMN outcome TEXT;"
         "ALTER TABLE notes ADD COLUMN context TEXT;"
         "ALTER TABLE notes ADD COLUMN steps TEXT DEFAULT '[]';"
         "ALTER TABLE notes ADD COLUMN evidence TEXT;"),
    # v11: a shared, hierarchical work ledger distinguishes a completion report from an evidence-backed
    # check and preserves blockers and status changes for other agents.
    11: ("CREATE TABLE IF NOT EXISTS tasks("
         "id INTEGER PRIMARY KEY, project TEXT NOT NULL, parent_id INTEGER, title TEXT NOT NULL,"
         "criterion TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',"
         "status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ("
         "'todo','in_progress','blocked','reported','self_verified','verified')),"
         "blocker TEXT NOT NULL DEFAULT '', report_evidence TEXT NOT NULL DEFAULT '',"
         "verification_evidence TEXT NOT NULL DEFAULT '', reporter TEXT NOT NULL DEFAULT '',"
         "verifier TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at REAL NOT NULL,"
         "updated_at REAL NOT NULL, reported_at REAL, verified_at REAL);"
         "CREATE INDEX IF NOT EXISTS tasks_project_parent ON tasks(project, parent_id, id);"
         "CREATE INDEX IF NOT EXISTS tasks_project_status ON tasks(project, status, updated_at);"
         "CREATE TABLE IF NOT EXISTS task_events("
         "id INTEGER PRIMARY KEY, project TEXT NOT NULL, task_id INTEGER NOT NULL, actor TEXT NOT NULL,"
         "action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);"
         "CREATE INDEX IF NOT EXISTS task_events_task ON task_events(project, task_id, id);"),
    # v12: remember renamed agent identities so replies to the old name still reach the current session.
    12: "CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, canonical TEXT, updated_at REAL);",
    # v13: persist the next inbox digest time so short-lived pollers can check often without notifying often.
    13: ("CREATE TABLE IF NOT EXISTS inbox_delivery("
         "project TEXT, agent TEXT, next_notify_at REAL NOT NULL, PRIMARY KEY(project, agent));"),
    # v14: preserve the concrete chat at each end, including after a delayed reply.
    14: ("ALTER TABLE messages ADD COLUMN sender_route TEXT;"
         "ALTER TABLE messages ADD COLUMN recipient_route TEXT;"
         "ALTER TABLE messages ADD COLUMN reply_to INTEGER;"),
    15: ("ALTER TABLE messages ADD COLUMN request_key TEXT;"
         "ALTER TABLE messages ADD COLUMN request_hash TEXT;"),
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


def _clean_task_text(value: Any) -> str:
    """Keep task fields bounded and single-line so user text cannot spoof ledger structure."""
    visible = "".join(ch for ch in str(value or "") if ch.isprintable() or ch.isspace())
    return " ".join(visible.split())


def _task_id(value: Any) -> int:
    try:
        task_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("task id must be a positive integer") from exc
    if not 0 < task_id <= 2**63 - 1:
        raise ValueError("task id must be a positive 64-bit integer")
    return task_id


class Store:
    def __init__(self, path: Optional[str] = None):
        use_default_path = path is None
        self.path = path or default_path()
        # Who is using this store, when the caller does not say. A Session sets it to its own agent id, so
        # reads/confirms are attributed without threading an argument through every call site.
        self.default_agent = ""
        if self.path != ":memory:":
            if os.path.islink(self.path):
                raise ValueError("refusing to open a symlinked Memdex database path")
            db_dir = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(db_dir, mode=0o700 if use_default_path else 0o755, exist_ok=True)
            if use_default_path and os.name == "posix" and not os.path.islink(db_dir):
                try:
                    os.chmod(db_dir, 0o700)
                except OSError:
                    pass
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
        # Older message tables do not have request_key until the migration/top-up above.
        _retry(lambda: self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS messages_request_key "
            "ON messages(project,sender_route,request_key) WHERE request_key IS NOT NULL"))
        self.db.execute("UPDATE notes SET archived_at=COALESCE(archived_at, updated_at, created_at) "
                        "WHERE archived=1 AND archived_at IS NULL")
        _retry(self._backfill_note_ids)
        self.db.commit()
        if self.path != ":memory:" and os.name == "posix":
            # Notes/messages are plaintext. Restrict the database and SQLite sidecars even when
            # the caller supplied a custom --db path; the default directory itself is private too.
            for suffix in ("", "-wal", "-shm"):
                try:
                    sidecar = self.path + suffix
                    if not os.path.islink(sidecar):
                        os.chmod(sidecar, 0o600)
                except OSError:
                    pass

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
            if version == 8:
                self._restore_superseded_note_index()
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
        root = os.path.realpath(project.root)
        target = os.path.realpath(full)
        try:
            inside = os.path.commonpath((os.path.normcase(root), os.path.normcase(target))) == os.path.normcase(root)
        except ValueError:
            inside = False
        target_rel = os.path.relpath(target, root).replace("\\", "/") if inside else ""
        if not inside or scan.is_secret(target_rel):
            if prev:
                self._drop_file(project.root, rel)
            return "skipped"
        # Open the resolved in-project path so a normal symlink cannot redirect the read after validation.
        full = target
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
            rel = to_rel(project, path)
            if not rel or scan.is_secret(rel):
                continue
            row = self.refresh_file(project, rel)
            ref: dict[str, Any] = {"path": row["path"] if row else rel, "sha": row["sha"] if row else None}
            if symbol.strip():
                ref["symbol"] = symbol.strip()
                ref["symbol_sha"] = self.symbol_map(project, ref["path"]).get(ref["symbol"]) if row else None
            refs.append(ref)
        return refs

    def add_note(self, project: Project, kind: str, title: str, body: str, author: str,
                 files: Optional[list[str]] = None, supersedes: Optional[int] = None,
                 importance: int = 0, pinned: bool = False, valid_from: Optional[float] = None,
                 valid_until: Optional[float] = None, outcome: Optional[str] = None,
                 context: str = "", steps: Optional[list[str]] = None, evidence: str = "") -> int:
        if kind not in NOTE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTE_KINDS)}")
        for label, value in (("valid_from", valid_from), ("valid_until", valid_until)):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{label} must be a finite timestamp")
        if valid_from is not None and valid_until is not None and float(valid_until) <= float(valid_from):
            raise ValueError("valid_until must be later than valid_from")
        if kind == "experience":
            if outcome not in ("success", "failed", "partial"):
                raise ValueError("experience notes require outcome=success, failed, or partial")
            raw_steps = steps if isinstance(steps, list) else []
            steps = [str(step).strip()[:1000] for step in raw_steps if str(step).strip()][:30]
            context, evidence = str(context or "").strip()[:2000], str(evidence or "").strip()[:4000]
            if not context or not steps or not evidence:
                raise ValueError("experience notes require context, at least one step, and observed evidence")
        else:
            if any((outcome, context, steps, evidence)):
                raise ValueError("outcome, context, steps, and evidence are only valid for experience notes")
            steps = []
        refs = self._make_refs(project, files or [])
        with self.db:
            now = time.time()
            cur = self.db.execute(
                "INSERT INTO notes(project, kind, title, body, author, refs, created_at, updated_at, "
                "importance, pinned, uid, valid_from, valid_until, outcome, context, steps, evidence) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project.key, kind, title.strip(), body.strip(), author, json.dumps(refs), now, now,
                 max(0, min(int(importance or 0), 5)), int(bool(pinned)), uuid.uuid4().hex,
                 valid_from, valid_until, outcome, context or None, json.dumps(steps or []), evidence or None))
            nid = cur.lastrowid
            self._fts_put("note", str(nid), project.key,
                          self._note_index_text(title, body, refs, context, steps, evidence))
            if supersedes:
                self.db.execute("UPDATE notes SET superseded_by=? WHERE id=? AND project=?",
                                (nid, supersedes, project.key))
                # Keep the old row indexed. Default search filters it out; an `as_of` search can still
                # retrieve the version that was current before this replacement.
            self._vec_put("note", str(nid), project.key,
                          f"{title}\n{body}\n{context}\n{' '.join(steps or [])}\n{evidence}")
        return nid

    @staticmethod
    def _note_index_text(title: str, body: str, refs: list[dict[str, Any]],
                         context: str = "", steps: Optional[list[str]] = None,
                         evidence: str = "") -> str:
        """Searchable note text, including the files and symbols the note is grounded in."""
        refs = refs if isinstance(refs, list) else []
        cited = " ".join(f"{r.get('path', '')} {r.get('symbol', '')}"
                         for r in refs if isinstance(r, dict))
        procedure = " ".join(steps or []) if isinstance(steps, list) else str(steps or "")
        return keys.expand(f"{title}\n{body}\n{context}\n{procedure}\n{evidence}\n{cited}")

    def _restore_superseded_note_index(self) -> None:
        """Backfill note rows whose FTS entries were removed when superseded or archived."""
        have = {row[1] for row in self.db.execute("PRAGMA table_info(notes)")}
        optional = [name if name in have else fallback for name, fallback in (
            ("context", "'' AS context"), ("steps", "'[]' AS steps"), ("evidence", "'' AS evidence"))]
        rows = self.db.execute("SELECT id, project, title, body, refs, " + ", ".join(optional)
                               + " FROM notes").fetchall()
        for row in rows:
            try:
                refs = json.loads(row["refs"] or "[]")
            except (json.JSONDecodeError, TypeError):
                refs = []
            if not isinstance(refs, list):
                refs = []
            try:
                steps = json.loads(row["steps"] or "[]")
            except (json.JSONDecodeError, TypeError):
                steps = []
            self._fts_put("note", str(row["id"]), row["project"],
                          self._note_index_text(row["title"] or "", row["body"] or "", refs,
                                                row["context"] or "", steps, row["evidence"] or ""))

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
        now = time.time()
        with self.db:
            cur = self.db.execute("UPDATE notes SET archived=1, archived_at=COALESCE(archived_at, ?), "
                                  "updated_at=? WHERE id=? AND project=?",
                                  (now, now, nid, project.key))
            # Keep the index and vector for an explicit historical `as_of` lookup. Current searches
            # hide archived notes in _note_valid_at; updated_at records when archiving took effect.
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

    def _note_valid_at(self, project: Project, note: sqlite3.Row, as_of: float) -> bool:
        """Whether this append-only note was the current recorded version at `as_of`.

        A replacement closes the old note's interval at the replacement's creation time. This is
        record-time history, not a claim that the code or decision was true for every instant in that
        interval.
        """
        if (note["created_at"] or 0) > as_of:
            return False
        archived_at = note["archived_at"] or note["updated_at"] or note["created_at"] or 0
        if note["archived"] and archived_at <= as_of:
            return False
        if note["superseded_by"]:
            replacement = self.get_note(project, int(note["superseded_by"]))
            if not replacement or (replacement["created_at"] or 0) <= as_of:
                return False
        return True

    @staticmethod
    def _note_domain_valid_at(note: sqlite3.Row, valid_at: Optional[float]) -> bool:
        """Apply optional domain-time bounds, distinct from when memdex recorded the note."""
        if valid_at is None:
            return True
        start, end = note["valid_from"], note["valid_until"]
        return (start is None or float(start) <= valid_at) and (end is None or valid_at < float(end))

    def _eligible_search_hits(self, project: Project, hits: list[tuple[str, str, str]],
                              note_kinds: list[str], as_of: float,
                              valid_at: Optional[float] = None) -> list[tuple[str, str, str]]:
        eligible = []
        for src, ref, snippet in hits:
            if src == "note":
                note = self.get_note(project, int(ref))
                if (not note or note["kind"] not in note_kinds
                        or not self._note_valid_at(project, note, as_of)
                        or not self._note_domain_valid_at(note, valid_at)):
                    continue
            eligible.append((src, ref, snippet))
        return eligible

    def search(self, project: Project, query: str, kinds: Optional[list[str]] = None,
               limit: int = 6, qvec: Optional[bytes] = None, cues: Optional[list[str]] = None,
               as_of: Optional[float] = None, valid_at: Optional[float] = None) -> list[dict[str, Any]]:
        """Hybrid search over notes and files: FTS5, plus embeddings when available.

        `kinds` may include "file" and any note kind. Results from the rankings are merged with reciprocal
        rank fusion. `cues` are alternate recall cues from the caller (aliases, related concepts, time or
        symbol names); each has lower weight than `query`. `as_of` reconstructs the note version recorded
        as current at that Unix time. `valid_at` filters notes by optional domain-time bounds. `qvec` is a
        query vector the caller computed; without it the configured local model is asked, and with neither
        the search is keyword-only.
        """
        want_files = not kinds or "file" in kinds
        note_kinds = [k for k in (kinds or NOTE_KINDS) if k in NOTE_KINDS]
        query = str(query or "").strip()[:2048]
        cue_inputs = cues or []
        cues = []
        seen_cues = {query.casefold()}
        for cue in cue_inputs[:4]:
            if not isinstance(cue, str):
                continue
            cue = cue.strip()[:240]
            if cue and cue.casefold() not in seen_cues:
                seen_cues.add(cue.casefold())
                cues.append(cue)
        now = time.time()
        as_of = now if as_of is None else float(as_of)
        valid_at = None if valid_at is None else float(valid_at)
        if not math.isfinite(as_of) or (valid_at is not None and not math.isfinite(valid_at)):
            raise ValueError("as_of and valid_at must be finite timestamps")
        ranked: dict[tuple[str, str], float] = {}
        snippets: dict[tuple[str, str], str] = {}
        matched_by: dict[tuple[str, str], list[str]] = {}

        def add_hits(hits: list[tuple[str, str, str]], weight: float, reason: str) -> None:
            for rank, (src, ref, snip) in enumerate(hits):
                key = (src, ref)
                ranked[key] = ranked.get(key, 0) + weight / (60 + rank)
                snippets.setdefault(key, snip)
                reasons = matched_by.setdefault(key, [])
                if reason not in reasons:
                    reasons.append(reason)

        first = self._eligible_search_hits(
            project, self._fts_hits(project, query, limit * 4, note_kinds=note_kinds, as_of=as_of,
                                    valid_at=valid_at), note_kinds, as_of, valid_at)
        add_hits(first, 1.0, "query")
        # A second query built from what the first one found. Half weight: it may add a result the query
        # did not literally match, but it must never outrank the literal match that produced the terms.
        if first:
            excluded = set(re.findall(r"\w+", " ".join([query] + cues).lower()))
            feedback = self._feedback_terms(first, exclude=excluded)
            if feedback:
                expanded = self._eligible_search_hits(
                    project, self._fts_hits(project, " ".join(feedback), limit * 4, op_first="OR",
                                            note_kinds=note_kinds, as_of=as_of, valid_at=valid_at),
                    note_kinds, as_of, valid_at)
                add_hits(expanded, 0.5, "feedback")
        # The caller's model can supply several plausible ways to cue the same memory. Keep each one
        # deliberately weak: alternate phrasings widen recall, while an exact query remains the anchor.
        for cue in cues:
            cue_hits = self._eligible_search_hits(
                project, self._fts_hits(project, cue, limit * 4, note_kinds=note_kinds, as_of=as_of,
                                        valid_at=valid_at), note_kinds, as_of, valid_at)
            add_hits(cue_hits, 0.15, f"cue: {cue}")
        if qvec is None and embed.config():
            qvec = embed.embed(query)
        if qvec is not None:
            sims = []
            for r in self.db.execute("SELECT src, ref, vec FROM vectors WHERE scope IN (?, '*')", (project.key,)):
                sims.append((embed.similarity(qvec, r["vec"]), r["src"], r["ref"]))
            sims.sort(reverse=True)
            semantic_hits = []
            for _, src, ref in sims:
                if src == "summary":
                    row = self.db.execute("SELECT path FROM files WHERE root=? AND sha=?",
                                          (project.root, ref)).fetchone()
                    if not row:
                        continue
                    src, ref = "file", row["path"]
                if src == "note":
                    note = self.get_note(project, int(ref))
                    if (not note or note["kind"] not in note_kinds
                            or not self._note_valid_at(project, note, as_of)
                            or not self._note_domain_valid_at(note, valid_at)):
                        continue
                semantic_hits.append((src, ref, ""))
                if len(semantic_hits) >= limit * 4:
                    break
            add_hits(semantic_hits, 1.0, "semantic")
        # Third ranking, over the candidates only: activation (used often and recently). It re-orders
        # what matched; it never adds a note the query did not match.
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
            # Give successful procedural memories a small tie-break without hiding failed attempts.
            if notes[ref]["kind"] == "experience":
                if notes[ref]["outcome"] == "success":
                    ranked[("note", ref)] *= 1.05
                elif notes[ref]["outcome"] == "failed":
                    ranked[("note", ref)] *= 0.98
        out = []
        for (src, ref), score in sorted(ranked.items(), key=lambda kv: -kv[1]):
            if src == "file":
                if not want_files:
                    continue
                out.append({"src": "file", "path": ref, "snippet": snippets.get((src, ref), ""),
                            "matched_by": matched_by.get((src, ref), [])})
            else:
                note = notes.get(ref)
                if (not note or note["kind"] not in note_kinds
                        or not self._note_valid_at(project, note, as_of)
                        or not self._note_domain_valid_at(note, valid_at)):
                    continue
                out.append({"src": "note", "id": note["id"], "kind": note["kind"], "title": note["title"],
                            "author": note["author"], "created_at": note["created_at"],
                            "snippet": snippets.get((src, ref), ""), "stale": self.stale_refs(project, note),
                            "pinned": bool(note["pinned"]), "replaced_by": note["superseded_by"],
                            "as_of": as_of, "valid_from": note["valid_from"],
                            "valid_until": note["valid_until"], "outcome": note["outcome"],
                            "context": note["context"], "steps": note["steps"], "evidence": note["evidence"],
                            "matched_by": matched_by.get((src, ref), [])})
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
                  note_kinds: Optional[list[str]] = None, as_of: Optional[float] = None,
                  valid_at: Optional[float] = None,
                  ) -> list[tuple[str, str, str]]:
        terms = [t.replace('"', "") for t in keys.expand(query).split() if t.replace('"', "")]
        if not terms:
            return []
        long = [t for t in terms if len(t) >= 3 or not self.trigram]
        short = [t for t in terms if t not in long]
        base = ("SELECT m.src AS src, m.ref AS ref, snippet(fts, 0, '«', '»', '…', 40) AS snip "
                "FROM fts JOIN fts_map m ON m.rid = fts.rowid WHERE m.scope IN (?, ?)")
        base_args: list[Any] = [project.key, project.root]
        if note_kinds is not None:
            if note_kinds:
                placeholders = ",".join("?" for _ in note_kinds)
                stamp = time.time() if as_of is None else as_of
                base += (" AND (m.src!='note' OR EXISTS (SELECT 1 FROM notes n "
                         "WHERE n.id=CAST(m.ref AS INTEGER) AND n.project=? "
                         f"AND n.kind IN ({placeholders}) AND n.created_at<=? "
                         "AND (n.archived=0 OR COALESCE(n.archived_at,n.updated_at,n.created_at)>?) "
                         "AND (n.superseded_by IS NULL OR EXISTS (SELECT 1 FROM notes replacement "
                         "WHERE replacement.id=n.superseded_by AND replacement.project=n.project "
                         "AND replacement.created_at>?))))")
                base_args.extend([project.key, *note_kinds, stamp, stamp, stamp])
                if valid_at is not None:
                    base += (" AND (m.src!='note' OR EXISTS (SELECT 1 FROM notes n "
                             "WHERE n.id=CAST(m.ref AS INTEGER) AND n.project=? "
                             "AND (n.valid_from IS NULL OR n.valid_from<=?) "
                             "AND (n.valid_until IS NULL OR n.valid_until>?)))")
                    base_args.extend([project.key, valid_at, valid_at])
            else:
                base += " AND m.src!='note'"
        # An expansion query is a list of alternatives, so it is tried with OR first; the caller's own
        # query wants the narrow AND first.
        for op in ((op_first,) + tuple(o for o in ("AND", "OR") if o != op_first)):
            if op == "OR" and long and short:
                # Trigram FTS cannot match short terms, so search those independently with LIKE and
                # interleave results to keep a common long term from crowding them all out.
                long_sql = base + " AND fts MATCH ? ORDER BY rank LIMIT ?"
                long_args = [*base_args, " OR ".join(f'"{t}"' for t in long), limit]
                short_sql = (base + " AND (" + " OR ".join("text LIKE ? ESCAPE '\\'" for _ in short)
                             + ") ORDER BY m.src, m.ref LIMIT ?")
                short_args = [*base_args, *("%" + _like_escape(t) + "%" for t in short), limit]
                try:
                    long_rows = self.db.execute(long_sql, long_args).fetchall()
                except sqlite3.OperationalError:
                    long_rows = []
                try:
                    short_rows = self.db.execute(short_sql, short_args).fetchall()
                except sqlite3.OperationalError:
                    short_rows = []
                rows = []
                seen = set()
                for index in range(max(len(long_rows), len(short_rows))):
                    for candidates in (long_rows, short_rows):
                        if index >= len(candidates):
                            continue
                        row = candidates[index]
                        key = (row["src"], row["ref"])
                        if key not in seen:
                            seen.add(key)
                            rows.append(row)
                            if len(rows) >= limit:
                                break
                    if len(rows) >= limit:
                        break
                if rows:
                    return [(r["src"], r["ref"], _short_snippet(r["snip"])) for r in rows]
                continue

            sql, args = base, list(base_args)
            if long:
                sql += " AND fts MATCH ?"
                args.append(f" {op} ".join(f'"{t}"' for t in long))
            if short:
                # trigram can't match terms under 3 characters (common in Korean), so fall back to LIKE.
                joiner = " AND " if op == "AND" else " OR "
                sql += " AND (" + joiner.join("text LIKE ? ESCAPE '\\'" for _ in short) + ")"
                args.extend("%" + _like_escape(t) + "%" for t in short)
            sql += (" ORDER BY rank" if long else " ORDER BY m.src, m.ref") + " LIMIT ?"
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

    # ---------------------------------------------------------------- tasks: explicit, evidence-backed completion

    TASK_MAX_PER_PROJECT = 500
    TASK_MAX_DEPTH = 8

    def create_task(self, project: Project, actor: str, title: str, criterion: str,
                    parent_id: Optional[int] = None, owner: str = "") -> int:
        """Add one observable work item. New children reopen a previously completed parent."""
        title, criterion, owner = _clean_task_text(title), _clean_task_text(criterion), _clean_task_text(owner).lower()
        actor = _clean_task_text(actor).lower()
        if not title or len(title) > 200:
            raise ValueError("task title must contain 1–200 characters")
        if not criterion or len(criterion) > 2000:
            raise ValueError("task acceptance criterion must contain 1–2000 characters")
        if len(owner) > 120:
            raise ValueError("task owner must be 120 characters or fewer")
        if not actor or len(actor) > 120:
            raise ValueError("task actor must contain 1–120 characters")
        now = time.time()
        with self.db:
            count = self.db.execute("SELECT count(*) FROM tasks WHERE project=?", (project.key,)).fetchone()[0]
            if count >= self.TASK_MAX_PER_PROJECT:
                raise ValueError(f"project task limit reached ({self.TASK_MAX_PER_PROJECT})")
            if parent_id is not None:
                parent_id = _task_id(parent_id)
                parent = self.db.execute("SELECT id, parent_id FROM tasks WHERE project=? AND id=?",
                                         (project.key, parent_id)).fetchone()
                if parent is None:
                    raise ValueError(f"parent task #{parent_id} was not found in this project")
                depth, ancestor = 1, parent_id
                while ancestor is not None:
                    depth += 1
                    if depth > self.TASK_MAX_DEPTH:
                        raise ValueError(f"task tree is limited to {self.TASK_MAX_DEPTH} levels")
                    row = self.db.execute("SELECT parent_id FROM tasks WHERE project=? AND id=?",
                                          (project.key, ancestor)).fetchone()
                    ancestor = row["parent_id"] if row else None
            cur = self.db.execute(
                "INSERT INTO tasks(project,parent_id,title,criterion,owner,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'todo',?,?,?)",
                (project.key, parent_id, title, criterion, owner, actor, now, now))
            task_id = int(cur.lastrowid)
            self.db.execute("INSERT INTO task_events(project,task_id,actor,action,detail,created_at) "
                            "VALUES(?,?,?,'created',?,?)",
                            (project.key, task_id, actor,
                             json.dumps({"title": title, "criterion": criterion, "owner": owner},
                                        ensure_ascii=False), now))
            if parent_id is not None:
                self._reopen_completed_ancestors(project.key, int(parent_id), actor,
                                                 f"new child task #{task_id} added", now)
        return task_id

    def _reopen_completed_ancestors(self, project_key: str, task_id: int, actor: str,
                                    reason: str, now: float) -> None:
        """Keep roll-up reports honest when a completed branch gets new or reopened work."""
        current: Optional[int] = task_id
        while current is not None:
            row = self.db.execute("SELECT id,parent_id,status FROM tasks WHERE project=? AND id=?",
                                  (project_key, current)).fetchone()
            if row is None:
                break
            if row["status"] in ("reported", "self_verified", "verified"):
                self.db.execute("UPDATE tasks SET status='in_progress', blocker='', report_evidence='', "
                                "verification_evidence='', reporter='', verifier='', reported_at=NULL, "
                                "verified_at=NULL, updated_at=? WHERE project=? AND id=?",
                                (now, project_key, current))
                self.db.execute("INSERT INTO task_events(project,task_id,actor,action,detail,created_at) "
                                "VALUES(?,?,?,'auto_reopened',?,?)",
                                (project_key, current, actor, reason, now))
            current = row["parent_id"]

    def _start_todo_ancestors(self, project_key: str, task_id: int, actor: str,
                              reason: str, now: float) -> None:
        """Do not leave an objective labeled todo after one of its child tasks has started."""
        current: Optional[int] = task_id
        while current is not None:
            row = self.db.execute("SELECT id,parent_id,status FROM tasks WHERE project=? AND id=?",
                                  (project_key, current)).fetchone()
            if row is None:
                break
            if row["status"] == "todo":
                self.db.execute("UPDATE tasks SET status='in_progress',updated_at=? WHERE project=? AND id=?",
                                (now, project_key, current))
                self.db.execute("INSERT INTO task_events(project,task_id,actor,action,detail,created_at) "
                                "VALUES(?,?,?,'auto_started',?,?)",
                                (project_key, current, actor, reason, now))
            current = row["parent_id"]

    def task_rows(self, project: Project, limit: int = TASK_MAX_PER_PROJECT,
                  offset: int = 0) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks WHERE project=? ORDER BY id LIMIT ? OFFSET ?",
                               (project.key, max(1, min(int(limit), self.TASK_MAX_PER_PROJECT)),
                                max(0, min(int(offset), self.TASK_MAX_PER_PROJECT)))).fetchall()

    def task_count(self, project: Project) -> int:
        return int(self.db.execute("SELECT count(*) FROM tasks WHERE project=?", (project.key,)).fetchone()[0])

    def task_events(self, project: Project, task_id: int, limit: int = 30) -> list[sqlite3.Row]:
        task_id = _task_id(task_id)
        exists = self.db.execute("SELECT 1 FROM tasks WHERE project=? AND id=?",
                                 (project.key, int(task_id))).fetchone()
        if not exists:
            raise ValueError(f"task #{task_id} was not found in this project")
        return self.db.execute("SELECT * FROM task_events WHERE project=? AND task_id=? "
                               "ORDER BY id DESC LIMIT ?",
                               (project.key, int(task_id), max(1, min(int(limit), 100)))).fetchall()[::-1]

    def task_action(self, project: Project, actor: str, task_id: int, action: str,
                    detail: str = "", owner: str = "") -> str:
        """Apply one guarded transition. A report is not completion until it is checked."""
        action = str(action).strip().lower()
        actor, task_id = _clean_task_text(actor).lower(), _task_id(task_id)
        if not actor or len(actor) > 120:
            raise ValueError("task actor must contain 1–120 characters")
        detail, owner = _clean_task_text(detail), _clean_task_text(owner).lower()
        if len(detail) > 4000:
            raise ValueError("task evidence or reason must be 4,000 characters or fewer")
        if action not in ("start", "block", "report", "verify", "reopen", "assign"):
            raise ValueError("task action must be start, block, report, verify, reopen, or assign")
        now = time.time()
        with self.db:
            row = self.db.execute("SELECT * FROM tasks WHERE project=? AND id=?",
                                  (project.key, int(task_id))).fetchone()
            if row is None:
                raise ValueError(f"task #{task_id} was not found in this project")
            status = row["status"]
            if action == "start":
                if status not in ("todo", "blocked"):
                    raise ValueError(f"task #{task_id} is {status}; only todo or blocked work can start")
                self.db.execute("UPDATE tasks SET status='in_progress',blocker='',updated_at=? "
                                "WHERE project=? AND id=?", (now, project.key, int(task_id)))
                self._start_todo_ancestors(project.key, row["parent_id"], actor,
                                           f"child task #{task_id} started", now)
            elif action == "block":
                if status not in ("todo", "in_progress"):
                    raise ValueError(f"task #{task_id} is {status}; reopen it before changing its status")
                if not detail:
                    raise ValueError("blocking a task requires a concrete reason")
                self.db.execute("UPDATE tasks SET status='blocked',blocker=?,updated_at=? "
                                "WHERE project=? AND id=?", (detail, now, project.key, int(task_id)))
            elif action == "report":
                if status != "in_progress":
                    raise ValueError(f"task #{task_id} is {status}; start it before reporting completion")
                if not detail:
                    raise ValueError("a completion report requires evidence (for example a test result, file, or observed setting)")
                unfinished = self.db.execute(
                    "SELECT id,status FROM tasks WHERE project=? AND parent_id=? AND status NOT IN ('self_verified','verified') "
                    "ORDER BY id", (project.key, int(task_id))).fetchall()
                if unfinished:
                    refs = ", ".join(f"#{c['id']} {c['status']}" for c in unfinished[:8])
                    raise ValueError(f"task #{task_id} still has unchecked children: {refs}")
                self.db.execute("UPDATE tasks SET status='reported',report_evidence=?,verification_evidence='', "
                                "reporter=?,verifier='',reported_at=?,verified_at=NULL,updated_at=? "
                                "WHERE project=? AND id=?",
                                (detail, actor, now, now, project.key, int(task_id)))
            elif action == "verify":
                if status != "reported":
                    raise ValueError(f"task #{task_id} is {status}; only a reported task can be checked")
                if not detail:
                    raise ValueError("verification requires evidence or a review note")
                final_state = "self_verified" if actor == row["reporter"] else "verified"
                self.db.execute("UPDATE tasks SET status=?,verification_evidence=?,verifier=?,verified_at=?,updated_at=? "
                                "WHERE project=? AND id=?",
                                (final_state, detail, actor, now, now, project.key, int(task_id)))
            elif action == "reopen":
                if status not in ("reported", "self_verified", "verified"):
                    raise ValueError(f"task #{task_id} is {status}; only reported or checked work can reopen")
                if not detail:
                    raise ValueError("reopening a task requires a reason")
                self.db.execute("UPDATE tasks SET status='in_progress',blocker='',report_evidence='', "
                                "verification_evidence='',reporter='',verifier='',reported_at=NULL, "
                                "verified_at=NULL,updated_at=? WHERE project=? AND id=?",
                                (now, project.key, int(task_id)))
                self._reopen_completed_ancestors(project.key, row["parent_id"], actor,
                                                 f"descendant task #{task_id} reopened", now)
            else:  # assign
                if status not in ("todo", "in_progress", "blocked"):
                    raise ValueError(f"task #{task_id} is {status}; reopen it before reassigning")
                if not owner or len(owner) > 120:
                    raise ValueError("assign requires an owner of 1–120 characters")
                self.db.execute("UPDATE tasks SET owner=?,updated_at=? WHERE project=? AND id=?",
                                (owner, now, project.key, int(task_id)))
                detail = f"assigned to {owner}"
            self.db.execute("INSERT INTO task_events(project,task_id,actor,action,detail,created_at) "
                            "VALUES(?,?,?,?,?,?)", (project.key, int(task_id), actor, action, detail, now))
            after = self.db.execute("SELECT status,reporter,verifier FROM tasks WHERE project=? AND id=?",
                                    (project.key, int(task_id))).fetchone()
        state = after["status"].replace("_", " ")
        suffix = " (peer-checked; agent name is self-declared, not authenticated)" \
            if after["status"] == "verified" else \
                 " (self-checked; no second agent reviewed it)" if after["status"] == "self_verified" else ""
        return f"task #{task_id}: {state}{suffix}."

    # ---------------------------------------------------------------- agents: messages and claims

    def register_route(self, project: Project, route: str, agent: str,
                       label: Optional[str] = None) -> None:
        """Refresh one concrete conversation without silently reassigning its address."""
        _check_chat_route(route)
        if label is not None:
            label = _clean_task_text(label)[:160]
        with self.db:
            cur = self.db.execute(
                "INSERT INTO chat_routes(project,route,agent,checkout,label,last_seen_at) "
                "VALUES(?,?,?,?,COALESCE(?,''),?) ON CONFLICT(project,route) DO UPDATE SET "
                "label=COALESCE(?,chat_routes.label),last_seen_at=excluded.last_seen_at "
                "WHERE chat_routes.agent=excluded.agent AND chat_routes.checkout=excluded.checkout",
                (project.key, route, agent, project.root, label, time.time(), label))
            if not cur.rowcount:
                raise ValueError("chat route already belongs to another agent or checkout")

    def route_rows(self, project: Project) -> list[sqlite3.Row]:
        """Include offline routes: an expired heartbeat does not transfer a chat's identity."""
        return self.db.execute("SELECT * FROM chat_routes WHERE project=? "
                               "ORDER BY last_seen_at DESC,route", (project.key,)).fetchall()

    def get_route(self, project: Project, route: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM chat_routes WHERE project=? AND route=?",
                               (project.key, route)).fetchone()

    def post(self, project: Project, sender: str, body: str, to: Optional[str] = None,
             sender_route: Optional[str] = None, recipient_route: Optional[str] = None,
             reply_to: Optional[int] = None, request_key: Optional[str] = None,
             original_body: Optional[str] = None, task_id: Optional[int] = None) -> int:
        # Agent ids are lowercase ("claude@demo-app"); a recipient typed as "Claude" must still match one,
        # otherwise the message sits in the mailbox and no session ever sees it as unread.
        if to is not None:
            to = str(to).strip().lower() or None
        if sender_route is not None:
            _check_chat_route(sender_route)
        if recipient_route is not None:
            _check_chat_route(recipient_route)
            # A pre-upgrade process might still be running. Its inbox must not interpret exact mail
            # as a family address or a NULL (broadcast) when it ignores the new envelope columns.
            to = recipient_route
        request_hash = None
        if request_key is not None:
            if not sender_route or not recipient_route:
                raise ValueError("request_key requires exact sender and recipient routes")
            if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_key):
                raise ValueError("request_key must be 1–128 ASCII letters, digits, '.', '_', ':' or '-'")
            # Compare the caller's request, not a time-varying snapshot attached to its message.
            intent = [recipient_route, reply_to, task_id, body if original_body is None else original_body]
            request_hash = hashlib.sha256(json.dumps(intent, separators=(",", ":")).encode()).hexdigest()
        with self.db:
            cur = self.db.execute("INSERT INTO messages(project,sender,recipient,body,created_at,"
                                  "sender_route,recipient_route,reply_to,request_key,request_hash) "
                                  "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,sender_route,request_key) "
                                  "WHERE request_key IS NOT NULL DO NOTHING",
                                  (project.key, sender, to, body.strip(), time.time(), sender_route,
                                   recipient_route, reply_to, request_key, request_hash))
            if cur.rowcount:
                return cur.lastrowid
            previous = self.db.execute("SELECT id,request_hash FROM messages WHERE project=? "
                                       "AND sender_route=? AND request_key=?",
                                       (project.key, sender_route, request_key)).fetchone()
            if previous["request_hash"] != request_hash:
                raise ValueError("request_key already used for a different message, destination, reply or task")
            return previous["id"]

    def get_reply_origin(self, project: Project, message_id: int, agent: str) -> str:
        """Resolve a visible incoming message to its recorded origin with no family fallback."""
        _check_chat_route(agent)
        if type(message_id) is not int or not 0 < message_id < 2**63:
            raise ValueError("reply must identify a positive 64-bit message id")
        row = self.db.execute("SELECT * FROM messages WHERE project=? AND id=?",
                              (project.key, message_id)).fetchone()
        if row is None or not _mail_visible(row, agent):
            raise ValueError("original message is not addressed to this mailbox in this project")
        if not row["sender_route"]:
            raise ValueError("original message has no exact sender route")
        return row["sender_route"]

    def inbox_snapshot(self, project: Project, agent: str,
                       limit: int = 20) -> tuple[list[sqlite3.Row], int]:
        """Return one inbox page and the cursor position it would acknowledge."""
        row = self.db.execute("SELECT last_id FROM cursors WHERE project=? AND agent=?",
                              (project.key, agent)).fetchone()
        last = row["last_id"] if row else 0
        rows = self.db.execute("SELECT * FROM messages WHERE project=? AND id>? "
                               "AND (recipient_route IS NULL OR recipient_route=?) "
                               "AND (sender_route IS NULL OR sender_route<>?) "
                               "AND LOWER(sender)<>LOWER(?) ORDER BY id",
                               (project.key, last, agent, agent, agent)).fetchall()
        mine = [r for r in rows if _mail_visible(r, agent)][:limit]
        new_last = (mine[-1]["id"] if len(mine) == limit else rows[-1]["id"]) if rows else last
        return mine, new_last

    def prepare_inbox_batch(self, project: Project, agent: str, interval: float,
                            now: Optional[float] = None, limit: int = 20
                            ) -> tuple[list[sqlite3.Row], int, bool, Optional[float]]:
        """Check unread mail and reserve a delivery window without consuming messages.

        This supports schedulers that start a fresh `inbox --notify-every N` process on each poll.
        The first poll with unread mail opens a window; later polls keep checking and leave its cursor
        alone until the window is due. A due window is reserved before returning, preventing two
        overlapping pollers for the same agent from emitting the same digest. If output fails, messages
        remain unread and are retried at the next window.
        """
        interval = float(interval)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("batch interval must be a finite number greater than zero")
        now = time.time() if now is None else float(now)
        nested_transaction = self.db.in_transaction
        savepoint = "memdex_inbox_batch"
        if nested_transaction:
            self.db.execute(f"SAVEPOINT {savepoint}")
        else:
            self.db.execute("BEGIN IMMEDIATE")

        def finish_transaction() -> None:
            if nested_transaction:
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.commit()

        try:
            msgs, cursor = self.inbox_snapshot(project, agent, limit=limit)
            if not msgs:
                # Skip messages addressed only to other agents, just like an ordinary inbox read.
                if cursor > 0:
                    self.db.execute(
                        "INSERT INTO cursors(project,agent,last_id) VALUES(?,?,?) "
                        "ON CONFLICT(project,agent) DO UPDATE SET "
                        "last_id=MAX(cursors.last_id,excluded.last_id)",
                        (project.key, agent, int(cursor)))
                finish_transaction()
                return [], cursor, False, None

            row = self.db.execute(
                "SELECT next_notify_at FROM inbox_delivery WHERE project=? AND agent=?",
                (project.key, agent)).fetchone()
            if row is None:
                next_at = now + interval
                self.db.execute(
                    "INSERT INTO inbox_delivery(project,agent,next_notify_at) VALUES(?,?,?)",
                    (project.key, agent, next_at))
                finish_transaction()
                return msgs, cursor, False, next_at

            next_at = float(row["next_notify_at"])
            if now < next_at:
                finish_transaction()
                return msgs, cursor, False, next_at

            # Reserve the next window now. If this process dies before writing/acknowledging output,
            # the still-unread messages will be retried after one interval rather than lost.
            next_at = now + interval
            self.db.execute(
                "UPDATE inbox_delivery SET next_notify_at=? WHERE project=? AND agent=?",
                (next_at, project.key, agent))
            finish_transaction()
            return msgs, cursor, True, next_at
        except BaseException:
            if nested_transaction:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif self.db.in_transaction:
                self.db.rollback()
            raise

    def acknowledge_inbox(self, project: Project, agent: str, last_id: int) -> None:
        """Advance a cursor after output succeeds; never move it backwards past a concurrent reader."""
        if last_id <= 0:
            return
        with self.db:
            self.db.execute("INSERT INTO cursors(project,agent,last_id) VALUES(?,?,?) "
                            "ON CONFLICT(project,agent) DO UPDATE SET last_id=MAX(cursors.last_id,excluded.last_id)",
                            (project.key, agent, int(last_id)))

    def inbox(self, project: Project, agent: str, peek: bool = False, limit: int = 20) -> list[sqlite3.Row]:
        msgs, new_last = self.inbox_snapshot(project, agent, limit=limit)
        if not peek:
            self.acknowledge_inbox(project, agent, new_last)
        return msgs

    def last_read(self, project: Project, recipient: str) -> Optional[float]:
        """When a session answering to `recipient` last advanced its inbox, or None if none ever did.

        Delivery here is a mailbox, not a push: a message waits until a session reads it. This is how the
        sender can be told the difference between "handed over" and "waiting for someone to start".
        """
        if not recipient:
            return None
        best: Optional[float] = None
        for row in self.db.execute(
                "SELECT c.agent AS agent, m.created_at AS seen FROM cursors c "
                "JOIN messages m ON m.id = c.last_id WHERE c.project=?", (project.key,)):
            if recipient.startswith("route:"):
                matches = recipient == row["agent"]
            else:
                matches = not row["agent"].startswith("route:") and _addressed_to(recipient, row["agent"])
            if not matches:
                continue
            seen = row["seen"] or 0
            if best is None or seen > best:
                best = seen
        return best

    def unread_count(self, project: Project, agent: str) -> int:
        return len(self.inbox(project, agent, peek=True, limit=1000))

    # ------------------------------------------------------------------ sessions

    def touch_session(self, project: Project, agent: str) -> None:
        """Mark this agent's session as alive on this project; `wake` and `sessions` read it.

        A brief is the natural heartbeat: whatever runs a brief is a live session, with no extra
        discipline for the agent to remember.
        """
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO sessions(project, agent, started_at, last_seen_at) VALUES(?,?,?,?) "
                "ON CONFLICT(project, agent) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (project.key, agent, now, now))

    def live_sessions(self, project: Project, ttl: float = 600.0) -> list[sqlite3.Row]:
        """Sessions whose last heartbeat is within `ttl` seconds, newest first."""
        cutoff = time.time() - ttl
        return self.db.execute(
            "SELECT agent, started_at, last_seen_at FROM sessions "
            "WHERE project=? AND last_seen_at>? ORDER BY last_seen_at DESC",
            (project.key, cutoff)).fetchall()

    def stale_sessions(self, project: Project, ttl: float = 600.0) -> list[sqlite3.Row]:
        """Sessions that stopped heartbeating, oldest silence first — the died-mid-task list."""
        cutoff = time.time() - ttl
        return self.db.execute(
            "SELECT agent, started_at, last_seen_at FROM sessions "
            "WHERE project=? AND last_seen_at<=? ORDER BY last_seen_at ASC",
            (project.key, cutoff)).fetchall()

    def close_session(self, project: Project, agent: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM sessions WHERE project=? AND agent=?",
                                  (project.key, agent))
        return cur.rowcount > 0

    def rename_agent(self, old: str, new: str) -> dict[str, int]:
        """An agent renamed itself: move its live state to the new name and remember the alias.

        History (note authorship, message senders) keeps the old name — that is who wrote it. Live state
        follows the agent: unread messages, read cursors, inbox digest cadence, active claims and the
        session row. The alias row makes future `post --to <old>` land on `<new>` instead of vanishing,
        which is the trap this exists for.
        """
        old, new = old.strip().lower(), new.strip().lower()
        if not old or not new or old == new:
            return {}
        old_alias = self.db.execute("SELECT canonical FROM aliases WHERE alias=?", (old,)).fetchone()
        if old_alias and old_alias["canonical"] != old:
            raise ValueError(f"{old} is an alias for {old_alias['canonical']}; rename the current identity instead")
        new_alias = self.db.execute("SELECT canonical FROM aliases WHERE alias=?", (new,)).fetchone()
        if new_alias and new_alias["canonical"] not in (old, new):
            raise ValueError(f"{new} already redirects to {new_alias['canonical']}")
        old_name, _, old_checkout = old.partition("@")
        new_name = new.partition("@")[0]
        counts = {}
        try:
            with self.db:
                # Match the literal agent name, including % and _, rather than treating identity text as
                # a SQL LIKE pattern. Only the *name* part is replaced and each row keeps its own
                # checkout: concatenating the whole new identity doubled the suffix when the caller
                # renamed to something like "dsh@demo" ("dsh@demo@demo", so no session ever saw the mail).
                def move(table: str, column: str) -> int:
                    extra = ""
                    args = [old, new, new_name, old_name, old, old_name, old_name]
                    if old_checkout:
                        # Renaming one checkout must not drag the other checkouts of that name along.
                        extra = f" AND substr({column}, -length(?)-1) = '@'||?"
                        args += [old_checkout, old_checkout]
                    return self.db.execute(
                        f"UPDATE {table} SET {column} = CASE WHEN {column} = ? THEN ? "
                        f"ELSE ? || substr({column}, length(?)+1) END "
                        f"WHERE ({column} = ? OR substr({column},1,length(?)+1)=?||'@'){extra}",
                        tuple(args)).rowcount

                counts["messages"] = move("messages", "recipient")
                counts["claims"] = move("claims", "agent")
                counts["cursors"] = move("cursors", "agent")
                counts["inbox_delivery"] = move("inbox_delivery", "agent")
                counts["sessions"] = move("sessions", "agent")
                counts["tasks"] = move("tasks", "owner")
                # Flatten prior aliases as the canonical name changes; reverse renames cannot create a cycle.
                self.db.execute("UPDATE aliases SET canonical=? WHERE canonical=?", (new, old))
                self.db.execute(
                    "INSERT OR REPLACE INTO aliases(alias, canonical, updated_at) VALUES(?,?,?)",
                    (old, new, time.time()))
        except sqlite3.IntegrityError as e:
            raise ValueError("rename collides with existing live state; reconcile the destination identity first") from e
        return counts

    def canonical_agent(self, recipient: str) -> Optional[str]:
        """The agent a message should really go to, following rename aliases (checkout suffix kept).

        Returns None when the name is current. A chain (a→b→c) resolves to the end of the chain.
        """
        recipient = str(recipient or "").strip().lower()
        if not recipient:
            return None
        exact = self.db.execute("SELECT canonical FROM aliases WHERE alias=?", (recipient,)).fetchone()
        if exact:
            seen, current = {recipient}, exact["canonical"]
            while current not in seen:
                seen.add(current)
                row = self.db.execute("SELECT canonical FROM aliases WHERE alias=?", (current,)).fetchone()
                if row is None:
                    break
                current = row["canonical"]
            return None if current == recipient else current
        name, _, checkout = recipient.partition("@")
        seen, current = set(), name
        while current not in seen:
            seen.add(current)
            row = self.db.execute("SELECT canonical FROM aliases WHERE alias=?",
                                  (current,)).fetchone()
            if row is None:
                break
            current = row["canonical"]
        if current == name:
            return None
        if checkout:
            # Rebuild the checkout from the recipient's own identity, not from the alias target: an
            # alias to "dsh@demo" plus a "@demo" recipient used to produce "dsh@demo@demo".
            return current.partition("@")[0] + "@" + checkout
        return current

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
        rel = to_rel(project, path)
        if not rel or scan.is_secret(rel):
            return None
        full = os.path.join(project.root, rel)
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
    p = str(path or "").strip().strip('"')
    root = os.path.realpath(project.root)
    candidate = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    try:
        common = os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate)))
    except ValueError:  # another drive on Windows
        return None
    if common != os.path.normcase(root):
        return None
    rel = os.path.relpath(candidate, root).replace("\\", "/")
    return None if rel in ("", ".") else rel


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


def _check_chat_route(route: str) -> None:
    if not isinstance(route, str) or not re.fullmatch(r"route:[0-9a-f]{32}", route):
        raise ValueError("invalid exact chat route")


def _mail_visible(message: sqlite3.Row, agent: str) -> bool:
    """Apply the same project mailbox rules for reading and replying."""
    if message["sender_route"] == agent or message["sender"].lower() == agent.lower():
        return False
    target = message["recipient_route"]
    if target is not None:
        return target == agent
    if agent.startswith("route:"):
        return not message["recipient"]  # bound chats share public broadcasts only
    return _addressed_to(message["recipient"], agent)


def _addressed_to(recipient: Optional[str], agent: str) -> bool:
    """Does this agent read this recipient line?

    Bare names broadcast to matching sub-addresses across checkouts. Checkout-qualified recipients
    stay within that checkout, while a sub-address matches its own subtree.
    """
    if not recipient:
        return True
    r = recipient.strip().lower()
    a = agent.strip().lower()
    rname, _, rcheckout = r.partition("@")
    aname, _, acheckout = a.partition("@")
    same_family = aname == rname or aname.startswith(rname + "/")
    if rcheckout:
        return same_family and acheckout == rcheckout
    return same_family


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


