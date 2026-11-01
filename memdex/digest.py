"""The consolidation queue: which notes are two ways of saying the same thing.

Complementary learning systems theory (McClelland, McNaughton & O'Reilly 1995; Kumaran, Hassabis &
McClelland 2016) says a fast episodic store and a slow semantic one are different systems, and that the
second is built offline from the first. memdex can hold the episodic half — but it cannot write the digest,
because writing one means reading the code and deciding what is true, and that is the agent's job.

So `memdex digest` does the part that is bookkeeping: it finds clusters of notes that are about the same
file or are near-duplicates of each other, and hands them to whoever is next in the session. The agent
consolidates; memdex keeps score.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import simhash
from .project import Project
from .store import Store

# Two notes about one file are a cluster; the threshold is deliberately low because the cost of asking is
# one line in the output, and the cost of never asking is two memories that disagree.
REF_OVERLAP = 0.4
NEAR_DUPLICATE_BITS = 14
MAX_NOTES = 300


def _refs(note) -> set:
    try:
        return {str(r.get("path")) for r in json.loads(note["refs"] or "[]") if r.get("path")}
    except (json.JSONDecodeError, TypeError, KeyError):
        return set()


def clusters(store: Store, project: Project, limit: int = 10) -> list[dict[str, Any]]:
    """Groups of notes that are about the same thing, biggest first. Read-only."""
    rows = store.list_notes(project, None, MAX_NOTES, include_cold=True)
    if len(rows) < 2:
        return []
    refs = [(_refs(r), simhash.fingerprint(simhash.note_text(r["title"], r["body"]))) for r in rows]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = refs[i][0], refs[j][0]
            joined = bool(a and b) and len(a & b) / len(a | b) >= REF_OVERLAP
            if not joined and simhash.hamming(refs[i][1], refs[j][1]) <= NEAR_DUPLICATE_BITS:
                joined = True
            if joined:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(rows[i])
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        shared = set.intersection(*[(_refs(m) or {None}) for m in members])
        out.append({
            "shared": sorted(p for p in shared if p),
            "notes": [{"id": m["id"], "kind": m["kind"], "title": m["title"], "author": m["author"],
                       "created_at": m["created_at"]} for m in members],
        })
    out.sort(key=lambda c: (-len(c["notes"]), c["notes"][0]["id"]))
    return out[:limit]


def render(found: list[dict[str, Any]]) -> str:
    if not found:
        return "no clusters: every note is about something else. Nothing to consolidate."
    lines = [f"{len(found)} cluster(s) that say the same thing more than once:"]
    for cluster in found:
        where = f" (about {', '.join(cluster['shared'])})" if cluster["shared"] else " (near-duplicates)"
        lines.append(f"- {len(cluster['notes'])} notes{where}:")
        for n in cluster["notes"]:
            lines.append(f"    #{n['id']} [{n['kind']}] {n['title']} ({n['author']})")
        first, rest = cluster["notes"][0], cluster["notes"][1:]
        supersedes = ", ".join(f"supersedes=#{n['id']}" for n in rest)
        lines.append(f"  → one note that holds what is still true, e.g. remember('decision', '…', "
                     f"{supersedes}) and archive the rest")
    lines.append("\nmemdex cannot write the digest: deciding what is still true means reading the code. "
                 "That is the agent's half of consolidation.")
    return "\n".join(lines)
