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
import re
from typing import Any, Optional

from . import simhash
from .project import Project
from .store import Store

# Two notes about one file are a cluster; the threshold is deliberately low because the cost of asking is
# one line in the output, and the cost of never asking is two memories that disagree.
REF_OVERLAP = 0.4
NEAR_DUPLICATE_BITS = 14
MAX_NOTES = 300

# Contradictions are found the deterministic way, not the semantic one: a token that one note asserts and
# the other negates. These are the words that open a negation window; everything that follows them (up to
# NEGATION_WINDOW tokens) counts as negated.
NEGATION_CUES = {"not", "no", "never", "dont", "avoid", "cannot", "cant", "without", "disable",
                 "disabled", "stop", "stopped", "instead", "deprecated", "removed", "noone"}
NEGATION_WINDOW = 4
EPISODIC_KINDS = {"handoff"}
# "without X, <bad thing> happens" is an argument FOR X, not against it: the negation applies to the
# counterfactual world. If one of these follows inside the window, the window counts as asserted.
BAD_OUTCOME = {"deadlock", "deadlocks", "fail", "fails", "failed", "failure", "break", "breaks", "broken",
               "crash", "crashes", "hang", "hangs", "hung", "lose", "loses", "lost", "corrupt",
               "corrupts", "corrupted", "race", "races", "slow", "slower", "timeout", "timeouts",
               "error", "errors", "leak", "leaks"}
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "will", "have", "has",
    "had", "its", "our", "your", "their", "them", "then", "than", "also", "each", "every", "only",
    "just", "very", "into", "onto", "over", "under", "about", "after", "before", "while", "when",
    "where", "which", "what", "who", "how", "why", "can", "could", "should", "would", "may", "might",
    "must", "does", "did", "doing", "been", "being", "some", "more", "most", "other", "another",
    "such", "same", "still", "even", "use", "used", "using", "file", "files", "code", "line",
    "lines", "note", "notes", "memdex", "this", "there", "here", "now", "all", "any", "one",
}
TOKEN_RE = re.compile(r"[a-z0-9_']+")


def _refs(note) -> set:
    try:
        return {str(r.get("path")) for r in json.loads(note["refs"] or "[]") if r.get("path")}
    except (json.JSONDecodeError, TypeError, KeyError):
        return set()


def _polarized(text: str) -> tuple[set, set]:
    """(asserted, negated) token sets: a cue word flips the next NEGATION_WINDOW tokens to negated.

    "without" is the one cue that usually argues FOR the thing it names ("without WAL, two writers
    deadlock"), so when the window contains a bad outcome it counts as asserted instead.
    """
    raw = TOKEN_RE.findall((text or "").lower())
    asserted: set = set()
    negated: set = set()
    i = 0
    while i < len(raw):
        token = raw[i]
        if len(token) < 3 or token in STOPWORDS:
            i += 1
            continue
        if token not in NEGATION_CUES:
            asserted.add(token)
            i += 1
            continue
        window: list[str] = []
        j = i + 1
        while j < len(raw) and len(window) < NEGATION_WINDOW:
            w = raw[j]
            if len(w) >= 3 and w not in STOPWORDS and w not in NEGATION_CUES:
                window.append(w)
            j += 1
        if token == "without" and any(w in BAD_OUTCOME for w in window):
            asserted.update(window)
        else:
            negated.update(window)
        i = j
    return asserted, negated


def contradictions(store: Store, project: Project, limit: int = 8) -> list[dict[str, Any]]:
    """Pairs of notes where one asserts a term the other negates ("always sync" vs "never sync").

    This is a cue-based detector, not a reader: it catches the words, not the intent. It will miss a
    contradiction phrased without a negation word, and it may flag one that was true at different times.
    That is exactly why the output tells the next session to settle it, not to delete anything.
    """
    # Handoffs are dated progress reports, not standing claims: the next one replaces the last, and their
    # "Done:/Pitfalls:" scaffolding plus unrelated task words ("full suite passed" vs "no full QA") paired
    # any two of them into a fake fight that the brief then told every session to settle.
    rows = [r for r in store.list_notes(project, None, MAX_NOTES, include_cold=True)
            if r["kind"] not in EPISODIC_KINDS]
    if len(rows) < 2:
        return []
    polarity = [(_refs(r), *_polarized(f"{r['title']}\n{r['body'] or ''}")) for r in rows]
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if polarity[i][0] and polarity[j][0] and not (polarity[i][0] & polarity[j][0]):
                continue  # both are pinned to files, and not the same ones: different subjects
            conflict = ((polarity[i][1] & polarity[j][2]) | (polarity[i][2] & polarity[j][1])) - STOPWORDS
            if not conflict:
                continue
            shared = sorted(polarity[i][0] & polarity[j][0])
            out.append({
                "a": {"id": rows[i]["id"], "kind": rows[i]["kind"], "title": rows[i]["title"],
                      "author": rows[i]["author"]},
                "b": {"id": rows[j]["id"], "kind": rows[j]["kind"], "title": rows[j]["title"],
                      "author": rows[j]["author"]},
                "shared": shared,
                "tokens": sorted(conflict),
            })
    out.sort(key=lambda c: (-len(c["tokens"]), min(c["a"]["id"], c["b"]["id"])))
    return out[:limit]


def render_contradictions(found: list[dict[str, Any]]) -> str:
    if not found:
        return "no contradictions: the notes agree, or disagree about different things."
    lines = [f"{len(found)} contradiction(s) — two notes assert opposite things:"]
    for c in found:
        where = f" · about {', '.join(c['shared'])}" if c["shared"] else ""
        a, b = c["a"], c["b"]
        lines.append(f"- #{a['id']} [{a['kind']}] {a['title'][:70]} ({a['author']}){where}")
        lines.append(f"  ↔ #{b['id']} [{b['kind']}] {b['title'][:70]} ({b['author']})")
        lines.append(f"    disagree on: {', '.join(c['tokens'])}")
    lines.append("\nOne of them is lying (or was true once). Read both, pick what is true now, and supersede "
                 "the other (remember(..., supersedes=#…)) instead of leaving two memories that fight.")
    return "\n".join(lines)


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
