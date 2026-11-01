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
import os
import time
import uuid
from typing import Any, Optional

from .project import Project
from .store import Store

FORMAT = 1
NOTE_FIELDS = ("uid", "kind", "title", "body", "author", "refs", "created_at", "updated_at",
               "importance", "pinned", "archived")


def new_uid() -> str:
    return uuid.uuid4().hex


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_") or "note"


def export(store: Store, project: Project, directory: str) -> dict[str, Any]:
    """Write this project's notes and summaries as one JSON file each. Nothing else leaves the database."""
    notes_dir = os.path.join(directory, "notes")
    summaries_dir = os.path.join(directory, "summaries")
    os.makedirs(notes_dir, exist_ok=True)
    os.makedirs(summaries_dir, exist_ok=True)
    written = 0
    for row in store.db.execute("SELECT * FROM notes WHERE project=?", (project.key,)):
        payload = {field: row[field] for field in NOTE_FIELDS if field in row.keys()}
        try:  # refs are a JSON string in the row and a list in the file: the file is the portable form
            payload["refs"] = json.loads(row["refs"] or "[]")
        except (json.JSONDecodeError, TypeError):
            payload["refs"] = []
        payload["project"] = project.name
        payload["format"] = FORMAT
        path = os.path.join(notes_dir, f"{_safe(row['uid'] or str(row['id']))}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        written += 1
    summaries = 0
    for row in store.db.execute(
            "SELECT s.* FROM summaries s JOIN files f ON f.sha=s.sha WHERE f.root=?", (project.root,)):
        payload = {"sha": row["sha"], "summary": row["summary"], "author": row["author"],
                   "created_at": row["created_at"], "symbols": row["symbols"], "format": FORMAT}
        path = os.path.join(summaries_dir, f"{_safe(row['sha'])}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        summaries += 1
    return {"notes": written, "summaries": summaries, "directory": os.path.abspath(directory)}


def _load(directory: str, sub: str) -> list[dict]:
    out = []
    folder = os.path.join(directory, sub)
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def merge(store: Store, project: Project, directory: str, now: Optional[float] = None) -> dict[str, Any]:
    """Apply the exported notes and summaries into this project. Idempotent; LWW by updated_at."""
    now = now or time.time()
    added = updated = unchanged = superseded = 0
    conflicts = []
    for payload in _load(directory, "notes"):
        uid = payload.get("uid")
        if not uid:
            continue
        existing = store.db.execute("SELECT * FROM notes WHERE uid=?", (uid,)).fetchone()
        incoming_at = float(payload.get("updated_at") or payload.get("created_at") or 0)
        if existing is None:
            store.db.execute(
                "INSERT INTO notes(project, kind, title, body, author, refs, created_at, updated_at, "
                "superseded_by, archived, importance, pinned, uid) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                (project.key, payload.get("kind") or "note", payload.get("title") or "",
                 payload.get("body") or "", payload.get("author") or "unknown",
                 json.dumps(payload.get("refs") or []), float(payload.get("created_at") or now),
                 incoming_at or now, int(bool(payload.get("archived"))),
                 int(payload.get("importance") or 0), int(bool(payload.get("pinned"))), uid))
            added += 1
            continue
        if existing["project"] != project.key:
            # The same note exported from another checkout: keep it here as a copy, do not move it.
            superseded += 1
            continue
        local_at = float(existing["updated_at"] or existing["created_at"] or 0)
        if incoming_at <= local_at:
            unchanged += 1
            if incoming_at < local_at and payload.get("body") != existing["body"]:
                conflicts.append({"uid": uid, "title": existing["title"], "kept": "local",
                                  "discarded_at": incoming_at})
            continue
        store.db.execute(
            "UPDATE notes SET title=?, body=?, kind=?, importance=?, pinned=?, archived=?, refs=?, "
            "updated_at=? WHERE id=?",
            (payload.get("title") or existing["title"], payload.get("body") or "",
             payload.get("kind") or existing["kind"], int(payload.get("importance") or 0),
             int(bool(payload.get("pinned"))), int(bool(payload.get("archived"))),
             json.dumps(payload.get("refs") or []), incoming_at, existing["id"]))
        updated += 1
        if payload.get("body") != existing["body"]:
            conflicts.append({"uid": uid, "title": payload.get("title"), "kept": "incoming",
                              "discarded_at": local_at})
    store.db.commit()

    summaries = 0
    for payload in _load(directory, "summaries"):
        sha = payload.get("sha")
        if not sha or not payload.get("summary"):
            continue
        have = store.db.execute("SELECT sha FROM summaries WHERE sha=?", (sha,)).fetchone()
        if have:
            continue
        store.db.execute(
            "INSERT INTO summaries(sha, summary, author, created_at, path_hint, symbols) "
            "VALUES(?,?,?,?,NULL,?)",
            (sha, payload["summary"], payload.get("author") or "unknown",
             float(payload.get("created_at") or now), payload.get("symbols")))
        summaries += 1
    store.db.commit()
    return {"added": added, "updated": updated, "unchanged": unchanged, "skipped": superseded,
            "summaries": summaries, "conflicts": conflicts,
            "directory": os.path.abspath(directory)}
