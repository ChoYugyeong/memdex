"""Moving memdex between machines without a server: `export --sync` and `merge`.

The limit this addresses is "nothing is shared between machines" (docs/LIMITS.md #1). The design here is the
smallest one that is honest about conflict:

- Every note gets a `uid` that never changes. The database id is per machine and means nothing elsewhere.
- The transport is a directory. memdex writes one JSON file per note and per summary; how that directory
  travels (git, Syncthing, a USB stick, a shared folder) is the user's choice, and every transport they
  already trust keeps working.
- Merge is last-writer-wins on the whole note, by `updated_at`. Notes are edited as a unit, so per-field
  LWW would be more machinery for the same answer, and the losing write is reported rather than silently
  dropped.
- Summaries are content-addressed by sha, which makes them free to merge: the same sha is the same summary.
- **Claims and messages are never exported.** A lease that outlives its machine is a deadlock generator, and
  a message is addressed to a session that does not exist on the other side.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from typing import Any, Optional

from . import scan
from .project import Project
from .store import NOTE_KINDS, Store, to_rel

FORMAT = 1
MAX_EXPORT_BYTES = 2_000_000
MAX_SYNC_NOTES = 10_000
NOTE_FIELDS = ("uid", "kind", "title", "body", "author", "refs", "created_at", "updated_at",
               "importance", "pinned", "archived", "archived_at", "valid_from", "valid_until",
               "outcome", "context", "steps", "evidence")


def _index_note(store: Store, project: Project, nid: int, vector: bool = False) -> None:
    row = store.get_note(project, nid)
    if not row:
        return
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
    store._fts_put("note", str(nid), project.key,
                   store._note_index_text(row["title"] or "", row["body"] or "", refs,
                                          row["context"] or "", steps, row["evidence"] or ""))
    if vector:
        store._vec_put("note", str(nid), project.key,
                       f"{row['title']}\n{row['body']}\n{row['context'] or ''}\n"
                       f"{' '.join(steps) if isinstance(steps, list) else ''}\n{row['evidence'] or ''}")


def _text(value: Any, default: str = "", limit: int = 100_000) -> str:
    return value[:limit] if isinstance(value, str) else default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value: Any, default: float) -> float:
    stamp = _number(value, default)
    latest = time.time() + 10 * 365 * 86400
    return stamp if 0 <= stamp <= latest else default


def _integer(value: Any, default: int = 0, low: int = 0, high: int = 5) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _flag(value: Any) -> int:
    return int(value is True or value == 1)


def new_uid() -> str:
    return uuid.uuid4().hex


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_") or "note"


def _write_json(path: str, payload: dict[str, Any]) -> None:
    """Atomically replace an export file without following a pre-existing symlink or hardlink."""
    folder = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".memdex-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export(store: Store, project: Project, directory: str) -> dict[str, Any]:
    """Write this project's notes and summaries as one JSON file each. Nothing else leaves the database."""
    notes_dir = os.path.join(directory, "notes")
    summaries_dir = os.path.join(directory, "summaries")
    if os.path.islink(notes_dir) or os.path.islink(summaries_dir):
        raise ValueError("refusing to export through a symlinked notes or summaries directory")
    os.makedirs(notes_dir, exist_ok=True)
    os.makedirs(summaries_dir, exist_ok=True)
    written = 0
    for row in store.db.execute("SELECT * FROM notes WHERE project=?", (project.key,)):
        payload = {field: row[field] for field in NOTE_FIELDS if field in row.keys()}
        try:  # refs are a JSON string in the row and a list in the file: the file is the portable form
            payload["refs"] = json.loads(row["refs"] or "[]")
        except (json.JSONDecodeError, TypeError):
            payload["refs"] = []
        try:
            payload["steps"] = json.loads(row["steps"] or "[]")
        except (json.JSONDecodeError, TypeError):
            payload["steps"] = []
        if row["superseded_by"]:
            replacement = store.db.execute("SELECT uid FROM notes WHERE id=? AND project=?",
                                           (row["superseded_by"], project.key)).fetchone()
            if replacement and replacement["uid"]:
                payload["superseded_by_uid"] = replacement["uid"]
        payload["project"] = project.name
        payload["format"] = FORMAT
        path = os.path.join(notes_dir, f"{_safe(row['uid'] or str(row['id']))}.json")
        _write_json(path, payload)
        written += 1
    summaries = 0
    for row in store.db.execute(
            "SELECT s.* FROM summaries s JOIN files f ON f.sha=s.sha WHERE f.root=?", (project.root,)):
        payload = {"sha": row["sha"], "summary": row["summary"], "author": row["author"],
                   "created_at": row["created_at"], "symbols": row["symbols"], "format": FORMAT}
        path = os.path.join(summaries_dir, f"{_safe(row['sha'])}.json")
        _write_json(path, payload)
        summaries += 1
    return {"notes": written, "summaries": summaries, "directory": os.path.abspath(directory)}


def _load(directory: str, sub: str) -> list[dict]:
    out = []
    folder = os.path.join(directory, sub)
    if os.path.islink(folder) or not os.path.isdir(folder):
        return out
    root_real = os.path.normcase(os.path.realpath(directory))
    folder_real = os.path.normcase(os.path.realpath(folder))
    try:
        if os.path.commonpath((root_real, folder_real)) != root_real:
            return out
    except ValueError:
        return out
    for name in sorted(os.listdir(folder))[:MAX_SYNC_NOTES]:
        if not name.endswith(".json"):
            continue
        try:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                continue
            resolved = os.path.realpath(path)
            if os.path.commonpath((folder_real, os.path.normcase(resolved))) != folder_real:
                continue
            if os.path.getsize(path) > MAX_EXPORT_BYTES:
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def merge(store: Store, project: Project, directory: str, now: Optional[float] = None) -> dict[str, Any]:
    """Apply the exported notes and summaries into this project. Idempotent; LWW by updated_at."""
    now = now or time.time()
    added = updated = unchanged = superseded = invalid = 0
    conflicts = []
    links = []
    for payload in _load(directory, "notes")[:MAX_SYNC_NOTES]:
        uid = payload.get("uid")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            continue
        replacement_uid = payload.get("superseded_by_uid")
        if isinstance(replacement_uid, str) and replacement_uid and len(replacement_uid) <= 128:
            links.append((uid, replacement_uid))
        raw_refs = payload.get("refs")
        refs = []
        if isinstance(raw_refs, list):
            for ref in raw_refs[:100]:
                if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                    continue
                rel = to_rel(project, ref["path"])
                if not rel or scan.is_secret(rel):
                    continue
                safe_ref = {"path": rel}
                symbol = ref.get("symbol")
                if isinstance(symbol, str) and symbol.strip():
                    safe_ref["symbol"] = symbol.strip()[:200]
                for field in ("sha", "symbol_sha"):
                    value = ref.get(field)
                    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                        safe_ref[field] = value
                refs.append(safe_ref)
        created_at = _timestamp(payload.get("created_at"), now)
        incoming_at = _timestamp(payload.get("updated_at"), created_at)
        kind = payload.get("kind") if payload.get("kind") in NOTE_KINDS else "note"
        outcome = payload.get("outcome") if payload.get("outcome") in ("success", "failed", "partial") else None
        context = _text(payload.get("context"), limit=2000)
        evidence = _text(payload.get("evidence"), limit=4000)
        raw_steps = payload.get("steps")
        steps = [_text(step, limit=1000).strip() for step in raw_steps[:30]
                 if isinstance(step, str) and step.strip()] if isinstance(raw_steps, list) else []
        valid_from = _optional_number(payload.get("valid_from"))
        valid_until = _optional_number(payload.get("valid_until"))
        if ((payload.get("valid_from") is not None and valid_from is None) or
                (payload.get("valid_until") is not None and valid_until is None) or
                (valid_from is not None and valid_until is not None and valid_until <= valid_from) or
                (kind == "experience" and
                 (outcome is None or not context.strip() or not steps or not evidence.strip()))):
            invalid += 1
            continue
        if kind != "experience":
            outcome, context, steps, evidence = None, "", [], ""
        title = _text(payload.get("title"), limit=500)
        body = _text(payload.get("body"))
        author = _text(payload.get("author"), "unknown", 200)
        importance = _integer(payload.get("importance"))
        pinned = _flag(payload.get("pinned"))
        archived = _flag(payload.get("archived"))
        archived_at = max(created_at, _timestamp(payload.get("archived_at"), incoming_at)) if archived else None
        existing = store.db.execute("SELECT * FROM notes WHERE uid=?", (uid,)).fetchone()
        if existing is None:
            cur = store.db.execute(
                "INSERT INTO notes(project, kind, title, body, author, refs, created_at, updated_at, "
                "superseded_by, archived, importance, pinned, uid, archived_at, valid_from, valid_until, "
                "outcome, context, steps, evidence) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (project.key, kind, title, body, author, json.dumps(refs), created_at,
                 incoming_at, archived, importance, pinned, uid, archived_at, valid_from, valid_until,
                 outcome, context or None, json.dumps(steps), evidence or None))
            _index_note(store, project, cur.lastrowid, vector=True)
            added += 1
            continue
        if existing["project"] != project.key:
            # The same note exported from another checkout: keep it here as a copy, do not move it.
            superseded += 1
            continue
        local_at = float(existing["updated_at"] or existing["created_at"] or 0)
        if incoming_at <= local_at:
            unchanged += 1
            if incoming_at < local_at and body != existing["body"]:
                conflicts.append({"uid": uid, "title": existing["title"], "kept": "local",
                                  "discarded_at": incoming_at})
            continue
        try:
            old_steps = json.loads(existing["steps"] or "[]")
        except (json.JSONDecodeError, TypeError):
            old_steps = []
        if not isinstance(old_steps, list):
            old_steps = []
        content_changed = (title != existing["title"] or body != existing["body"] or
                           context != (existing["context"] or "") or steps != old_steps or
                           evidence != (existing["evidence"] or ""))
        store.db.execute(
            "UPDATE notes SET title=?, body=?, kind=?, importance=?, pinned=?, archived=?, archived_at=?, "
            "refs=?, updated_at=?, valid_from=?, valid_until=?, outcome=?, context=?, steps=?, evidence=? "
            "WHERE id=?",
            (title or existing["title"], body, kind, importance,
             pinned, archived, archived_at,
             json.dumps(refs), incoming_at, valid_from, valid_until, outcome, context or None,
             json.dumps(steps), evidence or None, existing["id"]))
        _index_note(store, project, existing["id"])
        if content_changed:
            store.db.execute("DELETE FROM vectors WHERE src='note' AND ref=? AND scope=?",
                             (str(existing["id"]), project.key))
            _index_note(store, project, existing["id"], vector=True)
        updated += 1
        if body != existing["body"]:
            conflicts.append({"uid": uid, "title": title, "kept": "incoming",
                              "discarded_at": local_at})
    for uid, replacement_uid in links:
        source = store.db.execute("SELECT id FROM notes WHERE uid=? AND project=?",
                                  (uid, project.key)).fetchone()
        replacement = store.db.execute("SELECT id FROM notes WHERE uid=? AND project=?",
                                       (replacement_uid, project.key)).fetchone()
        if source and replacement and source["id"] != replacement["id"]:
            store.db.execute("UPDATE notes SET superseded_by=? WHERE id=? AND project=?",
                             (replacement["id"], source["id"], project.key))
    store.db.commit()

    summaries = 0
    for payload in _load(directory, "summaries"):
        sha = payload.get("sha")
        summary = payload.get("summary")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            continue
        if not isinstance(summary, str) or not summary.strip():
            continue
        have = store.db.execute("SELECT sha FROM summaries WHERE sha=?", (sha,)).fetchone()
        if have:
            continue
        author = _text(payload.get("author"), "unknown", 200)
        symbols = payload.get("symbols")
        if isinstance(symbols, str):
            try:
                parsed_symbols = json.loads(symbols)
                symbols = symbols if isinstance(parsed_symbols, dict) else None
            except json.JSONDecodeError:
                symbols = None
        else:
            symbols = None
        store.db.execute(
            "INSERT INTO summaries(sha, summary, author, created_at, path_hint, symbols) "
            "VALUES(?,?,?,?,NULL,?)",
            (sha, summary[:100_000], author, _timestamp(payload.get("created_at"), now), symbols))
        summaries += 1
    store.db.commit()
    return {"added": added, "updated": updated, "unchanged": unchanged, "skipped": superseded,
            "invalid": invalid,
            "summaries": summaries, "conflicts": conflicts,
            "directory": os.path.abspath(directory)}
