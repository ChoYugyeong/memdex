"""Skill registry: which agent has which skill, searchable by every agent.

Harnesses ship skills as SKILL.md files with YAML front matter (Claude Code
`~/.claude/skills`, plugin marketplaces, the Agent Skills layout
`~/.agents/skills/<name>/SKILL.md`, project-level `.claude/skills`, …). Two
problems follow: finding the right one among hundreds is itself work, and an
agent cannot see another harness's skills at all. memdex scans the known
locations, keeps name/description/path per agent, and answers "is there a skill
for X?" for everyone. A SKILL.md is plain instructions, so an agent that lacks
the skill can read it through `show` and follow it anyway.

Search is a keyword score over a few hundred rows — deliberately independent
of the FTS index so this module never couples to the store's internals.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """CREATE TABLE IF NOT EXISTS skills(
    id INTEGER PRIMARY KEY, agent TEXT, name TEXT, description TEXT, path TEXT, invoke TEXT,
    updated_at REAL, UNIQUE(agent, name));"""

# Home-relative roots and the agent they belong to. ".agents" is the shared Agent Skills
# directory, so its skills are registered as usable by anyone; it is scanned first so a
# harness's own copy of the same file is recognised as a duplicate.
HOME_ROOTS = {
    ".agents": "any", ".claude": "claude", ".codex": "codex", ".dsh": "deepseek",
    ".cursor": "cursor", ".gemini": "gemini", ".windsurf": "windsurf",
}
# Project-relative roots; registered under the scanning agent's own name.
PROJECT_ROOTS = [".claude/skills", ".codex/skills", ".agents/skills", "skills"]
# Directories that never hold skills but can be huge (transcripts, caches).
SKIP = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "projects", "sessions",
    "shell-snapshots", "file-history", "paste-cache", "backups", "downloads", "telemetry",
    "state", "daemon", "ide", "chrome", "session-env", "scheduled-tasks", "cache", "todos",
}
MAX_DEPTH = 7
SHOW_CHARS = 4000


def ensure(db) -> None:
    db.executescript(SCHEMA)


def discover(root: str) -> list[tuple[str, str, str]]:
    """(name, description, path) for every SKILL.md under root, pruned and depth-limited."""
    found = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return found
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        if dirpath.count(os.sep) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
        if "SKILL.md" in filenames:
            path = os.path.join(dirpath, "SKILL.md")
            name, desc = parse(path)
            if name:
                found.append((name, desc, path))
    return found


def parse(path: str) -> tuple[str, str]:
    """Name and one-paragraph description from a SKILL.md (front matter or first paragraph)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(8000)
    except OSError:
        return "", ""
    text = text.lstrip("﻿")
    name = desc = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            fm, body = text[3:end], text[end + 4:]
            name, desc = _fm_value(fm, "name"), _fm_value(fm, "description")
    if not name:
        name = Path(path).parent.name
    if not desc:
        for para in re.split(r"\n\s*\n", body.strip()):
            para = para.strip()
            if para and not para.startswith("#"):
                desc = para
                break
    return name.strip(), " ".join(desc.split())[:400]


def _fm_value(fm: str, key: str) -> str:
    """A front-matter scalar, including folded/literal block scalars (`>-`, `|`)."""
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{key}\s*:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1).strip()
        if value in ("", ">", ">-", "|", "|-", ">+", "|+"):
            block = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "" or nxt.startswith((" ", "\t")):
                    block.append(nxt.strip())
                else:
                    break
            value = " ".join(x for x in block if x)
        return value.strip().strip("'\"")
    return ""


def scan(store, agent: str, project_root: Optional[str] = None, home: Optional[str] = None,
         extra_roots: Optional[list[str]] = None) -> dict[str, Any]:
    """Register this machine's skill files. Home roots map to their harness's agent name;
    project-level and extra roots are registered under `agent`."""
    ensure(store.db)
    home = home or str(Path.home())
    jobs: list[tuple[str, str]] = [(os.path.join(home, d), who) for d, who in HOME_ROOTS.items()]
    if project_root:
        jobs += [(os.path.join(project_root, d), agent) for d in PROJECT_ROOTS]
    for d in (extra_roots or []) + [x for x in os.environ.get("MEMDEX_SKILL_DIRS", "").split(os.pathsep) if x]:
        jobs.append((d, agent))
    now = time.time()
    # Every scanned agent gets an entry, so a root that lost all its skills is cleaned up too.
    seen: dict[str, set[str]] = {who: set() for _, who in jobs}
    seen_sha: set[str] = set()  # the same SKILL.md installed in two places is one skill
    total = 0
    with store.db:
        for root, who in jobs:
            for name, desc, path in discover(root):
                sha = _sha(path)
                if sha in seen_sha:
                    continue
                seen_sha.add(sha)
                store.db.execute(
                    "INSERT INTO skills(agent, name, description, path, invoke, updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(agent, name) DO UPDATE SET description=excluded.description, "
                    "path=excluded.path, updated_at=excluded.updated_at",
                    (who, name, desc, path, "", now))
                seen.setdefault(who, set()).add(name)
                total += 1
        # Skill files that disappeared since the last scan are dropped; hand-shared ones (no path) stay.
        removed = 0
        for who, names in seen.items():
            rows = store.db.execute("SELECT id, name FROM skills WHERE agent=? AND path<>''", (who,)).fetchall()
            for r in rows:
                if r["name"] not in names:
                    store.db.execute("DELETE FROM skills WHERE id=?", (r["id"],))
                    removed += 1
    return {"found": total, "removed": removed, "agents": sorted(who for who, names in seen.items() if names)}


def _sha(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return path


def share(store, agent: str, name: str, description: str, invoke: str = "", path: str = "") -> int:
    ensure(store.db)
    with store.db:
        cur = store.db.execute(
            "INSERT INTO skills(agent, name, description, path, invoke, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(agent, name) DO UPDATE SET description=excluded.description, invoke=excluded.invoke, "
            "path=excluded.path, updated_at=excluded.updated_at",
            (agent, name.strip(), " ".join(description.split())[:400], path, invoke.strip(), time.time()))
    return cur.lastrowid


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def _hit(term: str, words: list[str], text: str) -> bool:
    """Whole-word match with simple plurals (form → forms, not format); CJK terms match as substrings."""
    if not term.isascii():
        return term in text
    return any(w == term or w == term + "s" or w == term + "es" or (len(term) >= 5 and w.startswith(term))
               for w in words)


def find(store, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Keyword matches, rare terms weighted higher (IDF), a term in the name worth three in the
    description. Rows with the same name and description are merged into one entry listing
    every agent that has it."""
    ensure(store.db)
    terms = [t for t in _tokens(query) if len(t) > 1]
    rows = store.db.execute("SELECT * FROM skills").fetchall()
    if not terms or not rows:
        return []
    docs = [(r, _tokens(r["name"]), _tokens(r["description"]), (r["description"] or "").lower(),
             r["name"].lower()) for r in rows]
    n = len(docs)
    weight = {}
    for t in terms:
        df = sum(1 for _, nw, dw, dtext, ntext in docs if _hit(t, nw + dw, ntext + " " + dtext))
        weight[t] = math.log(1 + n / (1 + df))
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r, nw, dw, dtext, ntext in docs:
        score = sum(weight[t] * ((3 if _hit(t, nw, ntext) else 0) + (1 if _hit(t, dw, dtext) else 0))
                    for t in terms)
        if score <= 0:
            continue
        key = (r["name"], (r["description"] or "")[:80])
        m = merged.setdefault(key, {"name": r["name"], "description": r["description"] or "",
                                    "invoke": r["invoke"] or "", "agents": [], "score": 0.0})
        m["agents"].append(r["agent"])
        m["invoke"] = m["invoke"] or (r["invoke"] or "")
        m["score"] = max(m["score"], score)
    out = sorted(merged.values(), key=lambda m: (-m["score"], m["name"]))[:limit]
    for m in out:
        m["agents"] = sorted(set(m["agents"]), key=lambda a: (a != "any", a))
    return out


def show(store, name: str, agent: Optional[str] = None) -> Optional[dict[str, Any]]:
    ensure(store.db)
    sql, args = "SELECT * FROM skills WHERE name=?", [name.strip()]
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    row = store.db.execute(sql + " ORDER BY agent LIMIT 1", args).fetchone()
    if not row:
        return None
    body = ""
    if row["path"]:
        try:
            with open(row["path"], "r", encoding="utf-8", errors="replace") as f:
                body = f.read(SHOW_CHARS + 1)
        except OSError:
            body = "(skill file is missing; run skills scan)"
    truncated = len(body) > SHOW_CHARS
    return {"row": row, "body": body[:SHOW_CHARS], "truncated": truncated}


def count(store) -> tuple[int, int]:
    ensure(store.db)
    n, agents = store.db.execute("SELECT count(*), count(DISTINCT agent) FROM skills").fetchone()
    return int(n), int(agents)
