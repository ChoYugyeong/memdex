"""Maintenance helpers: what the index saves, and how to prune dead data.

Kept out of ``cli.py`` so they can be tested directly (no subprocesses), and so
the MCP layer could reuse them later.
"""

from __future__ import annotations

import os
from typing import Any

from .project import Project
from .store import Store

# Only used for human-facing estimates; memdex never charges anyone per token.
CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    return max(0, (int(chars) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def savings(store: Store, project: Project, top: int = 10) -> dict[str, Any]:
    """Compare reading every indexed file with taking its outline instead.

    The index must be up to date; callers run ``store.index(project)`` first so
    this stays a pure read (and cheap to test).
    """
    rows = store.db.execute(
        "SELECT path, size, lines, lang, COALESCE(length(outline), 0) AS outline_chars "
        "FROM files WHERE root=?",
        (project.root,),
    ).fetchall()

    total_bytes = 0
    total_outline = 0
    with_outline = 0
    per_file: list[dict[str, Any]] = []
    for row in rows:
        size = int(row["size"] or 0)
        outline_chars = int(row["outline_chars"] or 0)
        total_bytes += size
        total_outline += outline_chars
        if outline_chars:
            with_outline += 1
        per_file.append({
            "path": row["path"],
            "bytes": size,
            "outline_chars": outline_chars,
            "saved_chars": max(0, size - outline_chars),
            "lang": row["lang"],
            "lines": int(row["lines"] or 0),
        })

    full_tokens = estimate_tokens(total_bytes)
    outline_tokens = estimate_tokens(total_outline)
    per_file.sort(key=lambda f: -f["saved_chars"])
    return {
        "project": project.name,
        "root": project.root,
        "files": len(per_file),
        "files_with_outline": with_outline,
        "bytes": total_bytes,
        "outline_chars": total_outline,
        "full_tokens": full_tokens,
        "outline_tokens": outline_tokens,
        "saved_tokens": max(0, full_tokens - outline_tokens),
        "ratio": (outline_tokens / full_tokens) if full_tokens else 0.0,
        "top": per_file[: max(0, top)],
    }


def prune(
    store: Store,
    dry_run: bool = False,
    all_projects: bool = False,
    vacuum: bool = True,
) -> dict[str, Any]:
    """Drop index rows for roots that no longer exist on disk.

    ``all_projects`` additionally removes notes, messages, claims and cursors for
    projects whose directory is gone. That is destructive, so it is opt-in: a
    project on an unmounted drive looks exactly like a deleted one.
    """
    db = store.db
    roots = [r[0] for r in db.execute("SELECT DISTINCT root FROM files")]
    stale_roots = [r for r in roots if not os.path.isdir(r)]

    size_before = os.path.getsize(store.path) if os.path.exists(store.path) else 0
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "stale_roots": stale_roots,
        "files": 0,
        "symbols": 0,
        "fts": 0,
        "notes": 0,
        "messages": 0,
        "claims": 0,
        "cursors": 0,
        "bytes_before": size_before,
        "bytes_after": size_before,
    }
    if not stale_roots and not all_projects:
        return report

    def count(sql: str, params: tuple = ()) -> int:
        return int(db.execute(sql, params).fetchone()[0])

    for root in stale_roots:
        report["files"] += count("SELECT count(*) FROM files WHERE root=?", (root,))
        report["symbols"] += count("SELECT count(*) FROM symbols WHERE root=?", (root,))
        report["fts"] += count("SELECT count(*) FROM fts WHERE src='file' AND scope=?", (root,))

    project_keys: list[str] = []
    if all_projects:
        keys = {r[0] for r in db.execute("SELECT DISTINCT project FROM notes")}
        keys |= {r[0] for r in db.execute("SELECT DISTINCT project FROM messages")}
        keys |= {r[0] for r in db.execute("SELECT DISTINCT project FROM claims")}
        keys |= {r[0] for r in db.execute("SELECT DISTINCT project FROM cursors")}
        project_keys = [k for k in keys if k and not os.path.isdir(k)]
        for key in project_keys:
            report["notes"] += count("SELECT count(*) FROM notes WHERE project=?", (key,))
            report["messages"] += count("SELECT count(*) FROM messages WHERE project=?", (key,))
            report["claims"] += count("SELECT count(*) FROM claims WHERE project=?", (key,))
            report["cursors"] += count("SELECT count(*) FROM cursors WHERE project=?", (key,))

    if dry_run:
        return report

    with db:
        for root in stale_roots:
            db.execute("DELETE FROM files WHERE root=?", (root,))
            db.execute("DELETE FROM symbols WHERE root=?", (root,))
            db.execute("DELETE FROM fts WHERE src='file' AND scope=?", (root,))
        for key in project_keys:
            db.execute("DELETE FROM notes WHERE project=?", (key,))
            db.execute("DELETE FROM messages WHERE project=?", (key,))
            db.execute("DELETE FROM claims WHERE project=?", (key,))
            db.execute("DELETE FROM cursors WHERE project=?", (key,))

    if vacuum:
        try:
            db.execute("VACUUM")
            report["vacuumed"] = True
        except Exception:  # noqa: BLE001 - busy database: pruning still succeeded
            report["vacuumed"] = False
    report["bytes_after"] = os.path.getsize(store.path) if os.path.exists(store.path) else 0
    return report
