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
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from . import embed, scan as scan_mod

SCHEMA = """CREATE TABLE IF NOT EXISTS skills(
    id INTEGER PRIMARY KEY, agent TEXT, name TEXT, description TEXT, path TEXT, invoke TEXT,
    updated_at REAL, mtime REAL, size INTEGER, sha TEXT, origin TEXT, UNIQUE(agent, name));
CREATE TABLE IF NOT EXISTS skills_meta(key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS skill_approvals(
    sha TEXT, name TEXT, approved_at REAL, approved_by TEXT, PRIMARY KEY(sha, name));"""

# Columns added after the first release. `ensure` adds whichever are missing, so a database written by an
# older memdex keeps working and the first refresh fills them in.
LATE_COLUMNS = (("mtime", "REAL"), ("size", "INTEGER"), ("sha", "TEXT"), ("origin", "TEXT"))

# A skill that lives inside a package or an editor's extension folder belongs to whoever shipped it, not to
# the person: that is the one case where an outdated copy is not the user's to fix.
VENDOR_MARKERS = ("/node_modules/", "/site-packages/", "/.vscode/extensions/")

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
MAX_SKILL_BYTES = 1_000_000
MAX_HAZARD_FINDINGS = 100
MAX_BUNDLE_FILES = 64
MAX_BUNDLE_BYTES = 5_000_000
SKILL_TEXT_SUFFIXES = {".md", ".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".ts",
                       ".json", ".yaml", ".yml", ".toml", ".txt"}


def ensure(db) -> None:
    db.executescript(SCHEMA)
    have = {row[1] for row in db.execute("PRAGMA table_info(skills)")}
    for column, kind in LATE_COLUMNS:
        if column not in have:
            db.execute(f"ALTER TABLE skills ADD COLUMN {column} {kind}")
    db.commit()


def discover_paths(root: str) -> list[str]:
    """Every SKILL.md under root, pruned and depth-limited. No file is opened."""
    found = []
    root = os.path.abspath(root)
    if os.path.islink(root) or not os.path.isdir(root):
        return found
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        if dirpath.count(os.sep) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
        if "SKILL.md" in filenames:
            path = os.path.join(dirpath, "SKILL.md")
            if not os.path.islink(path):
                found.append(path)
    return found


def discover(root: str) -> list[tuple[str, str, str]]:
    """(name, description, path) for every SKILL.md under root."""
    found = []
    for path in discover_paths(root):
        name, desc = parse(path)
        if name:
            found.append((name, desc, path))
    return found


def parse(path: str) -> tuple[str, str]:
    """Name and one-paragraph description from a SKILL.md (front matter or first paragraph)."""
    try:
        root = os.path.realpath(os.path.dirname(path))
        safe = _bundle_target(root, path)
        if safe is None:
            return "", ""
        text = _read_regular(safe[0], 8000).decode("utf-8", "replace")
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
    return " ".join(name.split())[:120], " ".join(desc.split())[:400]


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


def _metadata(text: str) -> dict[str, str]:
    """Read the small set of SKILL.md fields that identify scope and requested tool access."""
    text = (text or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    frontmatter = text[3:end]
    fields = ("name", "description", "allowed-tools", "user-invocable", "model", "context",
              "argument-hint", "compatibility")
    return {key: value for key in fields if (value := _fm_value(frontmatter, key))}


def _roots(home: str, project_root: Optional[str], agent: str,
           extra_roots: Optional[list[str]]) -> list[tuple[str, str]]:
    """(root, agent) pairs, home roots first so a harness's copy of a shared file is the duplicate."""
    jobs: list[tuple[str, str]] = [(os.path.join(home, d), who) for d, who in HOME_ROOTS.items()]
    if project_root:
        jobs += [(os.path.join(project_root, d), agent) for d in PROJECT_ROOTS]
    for d in (extra_roots or []) + [x for x in os.environ.get("MEMDEX_SKILL_DIRS", "").split(os.pathsep) if x]:
        jobs.append((d, agent))
    return jobs


def _origin(path: str, project_root: Optional[str], home: str) -> str:
    """Where a skill came from: this checkout, this user, or a package/extension that shipped it."""
    p = "/" + os.path.abspath(path).replace(os.sep, "/").lstrip("/")
    if any(marker in p for marker in VENDOR_MARKERS):
        return "vendor"
    if project_root:
        root = "/" + os.path.abspath(project_root).replace(os.sep, "/").lstrip("/") + "/"
        if p.startswith(root):
            return "project"
    base = "/" + os.path.abspath(home).replace(os.sep, "/").lstrip("/") + "/"
    return "user" if p.startswith(base) else "vendor"


def _under(path: str, root: str) -> bool:
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def refresh(store, agent: str, project_root: Optional[str] = None, home: Optional[str] = None,
            extra_roots: Optional[list[str]] = None, force: bool = False) -> dict[str, Any]:
    """Bring the registry up to date, reading only the files whose mtime/size moved.

    A walk for SKILL.md names is cheap; parsing and hashing every file is not, and the registry is
    refreshed when an agent searches it. `force=True` re-reads everything (`memdex skills scan`), which is
    the escape hatch when a file changed without its mtime moving.
    """
    ensure(store.db)
    home = home or str(Path.home())
    jobs = _roots(home, project_root, agent, extra_roots)
    known = {r["path"]: r for r in store.db.execute(
        "SELECT id, agent, name, path, mtime, size, sha FROM skills WHERE path<>''")}
    now = time.time()
    seen_paths: set[str] = set()
    seen_sha: set[str] = set()
    per_agent: dict[str, set[str]] = {}
    found = updated = unchanged = removed = 0
    with store.db:
        for root, who in jobs:
            for path in discover_paths(root):
                try:
                    if os.path.islink(path):
                        continue
                    st = os.stat(path)
                except OSError:
                    continue
                seen_paths.add(path)
                prev = known.get(path)
                if (not force and prev is not None and prev["sha"] and prev["agent"] == who
                        and prev["mtime"] == st.st_mtime and prev["size"] == st.st_size):
                    seen_sha.add(prev["sha"])
                    per_agent.setdefault(who, set()).add(prev["name"])
                    unchanged += 1
                    continue
                name, desc = parse(path)
                if not name:
                    continue
                sha = _sha(path)
                if sha and sha in seen_sha:
                    # The same SKILL.md installed in two places is one skill: the first root wins.
                    if prev is not None and store.db.execute("DELETE FROM skills WHERE id=?",
                                                             (prev["id"],)).rowcount:
                        removed += 1
                    continue
                if sha:
                    seen_sha.add(sha)
                store.db.execute(
                    "INSERT INTO skills(agent, name, description, path, invoke, updated_at, mtime, size, sha, origin)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(agent, name) DO UPDATE SET description=excluded.description, "
                    "path=excluded.path, updated_at=excluded.updated_at, mtime=excluded.mtime, "
                    "size=excluded.size, sha=excluded.sha, origin=excluded.origin",
                    (who, name, desc, path, "", now, st.st_mtime, st.st_size, sha,
                     _origin(path, project_root, home)))
                per_agent.setdefault(who, set()).add(name)
                found += 1
                if prev is not None:
                    updated += 1
        # Skill files that disappeared since the last scan are dropped; hand-shared ones (no path) stay.
        for path, prev in known.items():
            if path in seen_paths or not any(_under(path, root) for root, _ in jobs):
                continue
            if store.db.execute("DELETE FROM skills WHERE id=?", (prev["id"],)).rowcount:
                removed += 1
        _meta_put(store.db, "scan_at", now)
    return {"found": found, "updated": updated, "unchanged": unchanged, "removed": removed,
            "agents": sorted(who for who, names in per_agent.items() if names)}


def scan(store, agent: str, project_root: Optional[str] = None, home: Optional[str] = None,
         extra_roots: Optional[list[str]] = None) -> dict[str, Any]:
    """Re-read every skill file (the explicit `skills scan`). `refresh` is the incremental version."""
    return refresh(store, agent, project_root, home, extra_roots, force=True)


def last_scan(store) -> Optional[float]:
    """When a scanned skill was last checked; hand-shared rows do not count."""
    ensure(store.db)
    row = store.db.execute("SELECT max(updated_at) FROM skills WHERE path<>''").fetchone()
    if row and row[0]:
        return row[0]
    # A registry that only has hand-shared rows still knows when its last walk happened.
    row = store.db.execute("SELECT value FROM skills_meta WHERE key='scan_at'").fetchone()
    return row[0] if row and row[0] else None


def _meta_put(db, key: str, value: float) -> None:
    db.execute("INSERT OR REPLACE INTO skills_meta VALUES(?,?)", (key, value))


def maybe_refresh(store, agent: str, project_root: Optional[str] = None, home: Optional[str] = None,
                  max_age: float = 3600.0) -> bool:
    """Refresh at most once per `max_age` seconds, and never to bootstrap an empty registry.

    An empty registry stays empty: filling it is `skills scan`'s job, so a first search in a fresh install
    still tells the agent to run it instead of quietly walking the whole home directory.
    """
    if count(store)[0] == 0:
        return False
    last = last_scan(store)
    if last and max_age > 0 and time.time() - last < max_age:
        return False
    refresh(store, agent, project_root, home)
    return True


def _sha(path: str) -> Optional[str]:
    return _inspect_file(path).get("manifest_sha")


def share(store, agent: str, name: str, description: str, invoke: str = "", path: str = "") -> int:
    ensure(store.db)
    name = " ".join(str(name or "").split())[:120]
    description = " ".join(str(description or "").split())[:400]
    invoke = " ".join(str(invoke or "").split())[:400]
    with store.db:
        cur = store.db.execute(
            "INSERT INTO skills(agent, name, description, path, invoke, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(agent, name) DO UPDATE SET description=excluded.description, invoke=excluded.invoke, "
            "path=excluded.path, updated_at=excluded.updated_at",
            (agent, name, description, path, invoke, time.time()))
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
    audit = _inspect_file(row["path"] or "", row["sha"])
    approval = store.db.execute("SELECT 1 FROM skill_approvals WHERE name=? AND sha=?",
                                (row["name"], row["sha"])).fetchone() if row["sha"] else None
    audit["approved"] = bool(approval and audit["state"] == "ok" and audit["scan_complete"])
    if not audit["approved"]:
        audit["body"] = ""
    return {"row": row, **audit}


def approve(store, name: str, expected_sha: str, approved_by: str = "user") -> dict[str, Any]:
    """Record a human's exact-hash approval; this is only exposed through the interactive CLI."""
    got = show(store, name)
    if not got:
        return {"ok": False, "error": f"no skill named {name!r}"}
    row = got["row"]
    if not row["path"]:
        return {"ok": False, "error": "hand-shared recipes have no verifiable file bundle; cannot approve"}
    if got["state"] != "ok" or not got["scan_complete"] or not row["sha"]:
        return {"ok": False, "error": f"bundle is {got['state']} or the scan is incomplete; run skills scan and review it"}
    if not expected_sha or expected_sha.strip().lower() != str(row["sha"]).lower():
        return {"ok": False, "error": "approval hash did not exactly match the current bundle"}
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO skill_approvals(sha, name, approved_at, approved_by) "
                         "VALUES(?,?,?,?)", (row["sha"], row["name"], time.time(), approved_by[:120]))
    return {"ok": True, "name": row["name"], "sha": row["sha"]}


def inspect_bundle(path: str, expected_sha: Optional[str] = None) -> dict[str, Any]:
    """Return bounded human-review text for the local CLI, never exposed through the MCP tool."""
    audit = _inspect_file(path, expected_sha)
    if audit["state"] in ("missing", "symlink", "oversized", "shared"):
        return audit
    root = os.path.realpath(os.path.dirname(path))
    blocks = []
    for item in audit["files"]:
        rel = item["path"]
        if scan_mod.is_secret(rel) or re.search(
                r"(?:^|[._/-])(?:\.env|credentials?|id_rsa|id_ed25519|private[-_]?key|"
                r"api[_-]?keys?|tokens?|secrets?|passwords?)(?:$|[._/-])", rel, re.IGNORECASE):
            blocks.append(f"--- {rel} ({item['sha']}) ---\n[content withheld: sensitive-looking filename]")
            continue
        target = os.path.join(root, *rel.split("/"))
        try:
            safe = _bundle_target(root, target)
            if safe is None:
                blocks.append(f"--- {rel} ({item['sha']}) ---\n[content withheld: bundle path changed]")
                continue
            resolved, actual_rel = safe
            if scan_mod.is_secret(actual_rel):
                blocks.append(f"--- {rel} ({item['sha']}) ---\n[content withheld: sensitive-looking filename]")
                continue
            raw = _read_regular(resolved, MAX_BUNDLE_BYTES)
        except (OSError, ValueError):
            blocks.append(f"--- {rel} ({item['sha']}) ---\n[content unavailable]")
            continue
        if hashlib.sha256(raw).hexdigest() != item["sha"]:
            blocks.append(f"--- {rel} ({item['sha']}) ---\n[content changed during inspection; rescan]")
            continue
        blocks.append(f"--- {rel} ({item['sha']}) ---\n{raw.decode('utf-8', 'replace')}")
    audit["review_text"] = "\n".join(blocks)
    return audit


def count(store) -> tuple[int, int]:
    ensure(store.db)
    n, agents = store.db.execute("SELECT count(*), count(DISTINCT agent) FROM skills").fetchone()
    return int(n), int(agents)


def rows(store) -> list[dict[str, Any]]:
    """Every registered skill, name-ordered, with the origin the scan worked out."""
    ensure(store.db)
    return [dict(r) for r in store.db.execute(
        "SELECT name, agent, origin, invoke, description, path, updated_at, mtime, sha FROM skills "
        "ORDER BY name, agent")]


def conflicts(store) -> list[dict[str, Any]]:
    """Names held by more than one harness. Two copies drift apart, and an agent that finds one cannot
    tell it is the older one — so the registry names the disagreement instead of picking a winner."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows(store):
        grouped.setdefault(row["name"], []).append({"agent": row["agent"], "origin": row["origin"] or "user",
                                                    "path": row["path"] or "(shared by hand)"})
    return [{"name": name, "entries": entries}
            for name, entries in sorted(grouped.items())
            if len({e["agent"] for e in entries}) > 1]


# A skill is executable advice: whatever it says, the agent will do. These are the patterns that turn
# "advice" into "a way to lose a machine", listed with the line they were found on so a human can judge.
# memdex flags; it does not judge, and it never edits a skill file.
HAZARDS = (
    ("rm -rf", re.compile(r"\brm\s+-(?:rf|fr|r|f)\b")),
    ("pipes a download into a shell", re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|da)?sh")),
    ("sudo", re.compile(r"\bsudo\b")),
    ("chmod 777", re.compile(r"\bchmod\s+(?:-R\s+)?777\b")),
    # The repo-injection framings come before the broad "reads secrets" pattern, so the specific ones
    # get the chance to name the line first.
    ("redirects git hooks", re.compile(r"\bgit\s+config[^\n]*core\.hooksPath")),
    ("sends secrets to a host", re.compile(r"\b(?:curl|wget)\b[^\n]*(?:https?://)[^\n]*(?:env|secret|token|key)")),
    ("uploads secrets", re.compile(r"\b(?:curl|wget)\b[^\n|]*\.env|\bcat\s+\.env\s*\|")),
    ("reads secrets", re.compile(r"\.env\b|id_rsa|\.ssh/|credentials|\.aws/")),
    ("decodes base64 into a command", re.compile(r"base64\s+(?:-d|--decode)")),
    ("eval/exec", re.compile(r"\beval\b|\bexec\s*\(")),
    ("runs a shell command from skill markup", re.compile(r"^\s*!`[^`]+`\s*$")),
    ("overrides agent instructions", re.compile(
        r"\b(?:ignore|disregard|override|forget)\b.{0,60}\b(?:previous|prior|system|developer)\s+instructions\b",
        re.IGNORECASE)),
    ("conceals actions from the user", re.compile(
        r"\b(?:do not|don't|never)\s+(?:tell|inform|show|disclose to)\s+(?:the\s+)?user\b",
        re.IGNORECASE)),
    ("destructive PowerShell command", re.compile(
        r"\b(?:Remove-Item|Remove-ItemProperty)\b[^\n]*(?:-Recurse|-Force)|\b(?:iwr|Invoke-WebRequest)\b[^\n]*\|\s*iex\b",
        re.IGNORECASE)),
    ("runs downloaded code", re.compile(
        r"\b(?:curl|wget|Invoke-WebRequest|iwr)\b[^\n]*(?:https?://)[^\n]*(?:\|\s*(?:sh|bash|iex)|-OutFile|--output)",
        re.IGNORECASE)),
    ("force-pushes", re.compile(r"git\s+push[^\n]*--force")),
    ("skips hooks", re.compile(r"--no-verify")),
    ("writes outside the checkout", re.compile(r">\s*/(?:etc|usr|bin|System)\b")),
)
HAZARD_SCAN_BYTES = 200_000


def hazards_in_text(text: str) -> list[dict[str, Any]]:
    """The lines in a text that a human should look at before following it (a file is one text)."""
    found = []
    for number, line in enumerate((text or "").splitlines(), 1):
        for label, pattern in HAZARDS:
            if pattern.search(line):
                if len(found) >= MAX_HAZARD_FINDINGS:
                    found.append({"line": number, "label": "more flagged lines omitted",
                                  "text": f"report capped at {MAX_HAZARD_FINDINGS} findings"})
                    return found
                found.append({"line": number, "label": label, "text": " ".join(line.split())[:160]})
                break
    return found


def hazards(path: str) -> list[dict[str, Any]]:
    """The lines in a skill file that a human should look at before following it."""
    try:
        root = os.path.realpath(os.path.dirname(path))
        safe = _bundle_target(root, path)
        if safe is None:
            return [{"line": 0, "label": "symlink refused", "text": "skill file was not read"}]
        raw = _read_regular(safe[0], MAX_SKILL_BYTES)
        if len(raw) > MAX_SKILL_BYTES:
            return [{"line": 0, "label": "scan incomplete", "text": "skill exceeds the 1 MB scan limit"}]
    except OSError:
        return []
    return hazards_in_text(raw[:HAZARD_SCAN_BYTES].decode("utf-8", "replace"))


_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_SKILL_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:\.?\.?/)*(?:scripts|references|assets)/"
    r"[A-Za-z0-9_./-]+\.(?:md|py|sh|ps1|bat|cmd|js|ts|json|ya?ml|toml|txt)\b",
    re.IGNORECASE)
_REMOTE_URL = re.compile(r"https?://[^\s)>'\"]+", re.IGNORECASE)


def _display_remote_url(value: str) -> str:
    """Show a remote target without exposing URL credentials, query values, or fragments."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[malformed remote URL omitted]"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        host += f":{port}"
    shown = f"{parsed.scheme}://{host}{parsed.path}"
    if parsed.query:
        shown += "?[query omitted]"
    if parsed.fragment:
        shown += "#[fragment omitted]"
    return shown


def _reference_targets(text: str) -> tuple[list[str], list[str]]:
    local, remote = set(), set()
    for raw in _MARKDOWN_LINK.findall(text or ""):
        target = raw.strip().strip("<>")
        try:
            parsed = urlsplit(target)
        except ValueError:
            if target.lower().startswith(("http://", "https://")):
                remote.add("[malformed remote URL omitted]")
            else:
                local.add(target)
            continue
        if parsed.scheme in ("http", "https"):
            remote.add(_display_remote_url(target))
            continue
        if parsed.scheme:
            local.add(parsed.path or target)
            continue
        if target:
            local_target = target.split("#", 1)[0].split("?", 1)[0]
            if local_target:
                local.add(unquote(local_target))
    for target in _SKILL_PATH.findall(text or ""):
        local.add(target)
    remote.update(_display_remote_url(target) for target in _REMOTE_URL.findall(text or ""))
    return sorted(local), sorted(remote)


def _bundle_target(root: str, source: str) -> Optional[tuple[str, str]]:
    """Resolve a bundle path only when every component stays inside the skill tree without symlinks."""
    root = os.path.realpath(root)
    source = os.path.abspath(source)
    try:
        relative = os.path.relpath(source, root)
        if relative in (".", "..") or relative.startswith(".." + os.sep):
            return None
        current = root
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            if os.path.islink(current):
                return None
        target = os.path.realpath(source)
        inside = os.path.commonpath((os.path.normcase(root), os.path.normcase(target))) == os.path.normcase(root)
        if not inside:
            return None
        actual_relative = os.path.relpath(target, root).replace("\\", "/")
        if actual_relative in ("", "."):
            return None
        return target, actual_relative
    except (OSError, ValueError):
        return None


def _read_regular(path: str, limit: int) -> bytes:
    """Read a bounded regular file and refuse a final-component symlink where the OS supports it."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _bundle_files(path: str) -> dict[str, Any]:
    """Boundedly resolve and scan local files directly referenced by a skill.

    Only text-like files under the skill directory are read. Symlink escapes, missing targets, oversized
    files, and reference-count/byte caps make the scan incomplete instead of being treated as clean.
    """
    root = os.path.realpath(os.path.dirname(path))
    queue: list[tuple[str, str]] = [(path, "SKILL.md")]
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    hazards: list[dict[str, Any]] = []
    remote_refs: set[str] = set()
    scan_complete = True
    total = 0
    digest = hashlib.sha256()
    while queue:
        source, rel = queue.pop(0)
        safe = _bundle_target(root, source)
        if safe is None:
            scan_complete = False
            hazards.append({"path": rel, "line": 0,
                            "label": "reference escapes or links inside skill directory",
                            "text": "symlinked or out-of-folder path was not read"})
            continue
        target, actual_rel = safe
        key = os.path.normcase(target)
        if key in seen:
            continue
        if len(files) >= MAX_BUNDLE_FILES:
            scan_complete = False
            break
        try:
            if scan_mod.is_secret(actual_rel):
                scan_complete = False
                hazards.append({"path": actual_rel, "line": 0, "label": "secret-like reference refused",
                                "text": "sensitive-looking bundle file was not read"})
                continue
            raw = _read_regular(target, min(MAX_SKILL_BYTES, MAX_BUNDLE_BYTES - total))
        except FileNotFoundError:
            if rel != "SKILL.md":
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "referenced file missing",
                                "text": "local skill reference could not be read"})
            continue
        except (OSError, ValueError):
            scan_complete = False
            hazards.append({"path": rel, "line": 0, "label": "referenced file unreadable",
                            "text": "local skill reference could not be read"})
            continue
        if len(raw) > MAX_SKILL_BYTES or total + len(raw) > MAX_BUNDLE_BYTES:
            scan_complete = False
            hazards.append({"path": actual_rel, "line": 0, "label": "scan incomplete",
                            "text": "referenced file exceeds the bounded scan size"})
            continue
        seen.add(key)
        total += len(raw)
        text = raw.decode("utf-8", "replace")
        file_sha = hashlib.sha256(raw).hexdigest()
        files.append({"path": actual_rel, "sha": file_sha, "size": len(raw)})
        digest.update(actual_rel.encode("utf-8", "replace") + b"\0")
        digest.update(raw + b"\0")
        for finding in hazards_in_text(text[:HAZARD_SCAN_BYTES]):
            hazards.append({"path": actual_rel, **finding})
            if len(hazards) > MAX_HAZARD_FINDINGS:
                hazards = hazards[:MAX_HAZARD_FINDINGS]
                hazards.append({"path": actual_rel, "line": 0,
                                "label": "more flagged lines omitted", "text": "finding limit reached"})
                scan_complete = False
                break
        if len(raw) > HAZARD_SCAN_BYTES:
            scan_complete = False
        local_refs, external_refs = _reference_targets(text)
        remote_refs.update(external_refs)
        for ref in local_refs:
            suffix = Path(ref).suffix.lower()
            norm = os.path.normpath(ref)
            if scan_mod.is_secret(norm.replace("\\", "/")):
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "secret-like reference refused",
                                "text": f"{ref[:120]} was not read"})
                continue
            if os.path.isabs(norm) or re.match(r"^[A-Za-z]:", norm):
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "reference escapes skill directory",
                                "text": "absolute references are not followed"})
                continue
            if suffix not in SKILL_TEXT_SUFFIXES:
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "unsupported local reference",
                                "text": f"{ref[:120]} is not a supported text file; approval is blocked"})
                continue
            candidates = [os.path.join(os.path.dirname(source), norm), os.path.join(root, norm)]
            child = next((c for c in candidates if os.path.exists(c)), candidates[0])
            child_safe = _bundle_target(root, child)
            if child_safe is None:
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "reference escapes skill directory",
                                "text": f"{ref[:120]} was not read"})
                continue
            child_real, relative = child_safe
            if scan_mod.is_secret(relative):
                scan_complete = False
                hazards.append({"path": rel, "line": 0, "label": "secret-like reference refused",
                                "text": f"{ref[:120]} was not read"})
                continue
            queue.append((child_real, relative))
    files.sort(key=lambda item: item["path"])
    if len(remote_refs) > 100:
        scan_complete = False
        hazards.append({"path": "SKILL.md", "line": 0, "label": "too many external references",
                        "text": "remote-reference report is capped at 100; approval is blocked"})
    return {"files": files, "hazards": hazards, "scan_complete": scan_complete,
            "remote_refs": sorted(remote_refs)[:100], "manifest_sha": digest.hexdigest(),
            "bundle_bytes": total}


def _inspect_file(path: str, expected_sha: Optional[str] = None) -> dict[str, Any]:
    """Read a bounded skill bundle once for metadata, hash, references and hazard scanning."""
    result: dict[str, Any] = {"state": "missing", "hazards": [], "scan_complete": False,
                              "body": "", "metadata": {}, "truncated": False,
                              "files": [], "remote_refs": [], "manifest_sha": None}
    if not path:
        result["state"] = "shared"
        return result
    root = os.path.realpath(os.path.dirname(path))
    safe = _bundle_target(root, path)
    if safe is None:
        result["state"] = "symlink"
        result["hazards"] = [{"line": 0, "label": "symlink or out-of-folder path refused",
                              "text": "skill file was not read"}]
        return result
    target, _ = safe
    try:
        raw = _read_regular(target, MAX_SKILL_BYTES)
    except OSError:
        return result

    if len(raw) > MAX_SKILL_BYTES:
        result["state"] = "oversized"
        return result
    bundle = _bundle_files(path)
    result.update(bundle)
    result["body"] = raw[:SHOW_CHARS + 1].decode("utf-8", "replace")
    result["metadata"] = _metadata(raw[:8000].decode("utf-8", "replace"))
    result["truncated"] = len(raw) > SHOW_CHARS
    main = next((item for item in bundle["files"] if item["path"] == "SKILL.md"), None)
    if not main or hashlib.sha256(raw[:MAX_SKILL_BYTES]).hexdigest() != main["sha"]:
        result["scan_complete"] = False
        result["hazards"].append({"path": "SKILL.md", "line": 0,
                                  "label": "skill changed during inspection",
                                  "text": "content and bundle manifest did not match; rescan"})
    current_sha = bundle["manifest_sha"]
    result["state"] = ("changed" if current_sha != expected_sha else "ok") if expected_sha else "unverified"
    return result


def verify(store) -> list[dict[str, Any]]:
    """Re-hash every registered skill file: is it still the file that was scanned, and is it safe-looking?

    The sha is what a shared registry needs (TUF-style: a name plus a content hash, so a swapped file is
    visible), and the hazard scan is what the person installing it needs. Both are read-only.
    """
    out = []
    for row in rows(store):
        audit = _inspect_file(row["path"] or "", row["sha"])
        approval = store.db.execute("SELECT 1 FROM skill_approvals WHERE name=? AND sha=?",
                                    (row["name"], row["sha"])).fetchone() if row["sha"] else None
        audit["approved"] = bool(approval and audit["state"] == "ok" and audit["scan_complete"])
        audit.pop("body", None)
        audit.pop("truncated", None)
        audit.pop("metadata", None)
        entry = {"name": row["name"], "agent": row["agent"], "origin": row["origin"] or "user",
                 "path": row["path"], **audit}
        out.append(entry)
    return out


def stats(store) -> dict[str, Any]:
    n, agents = count(store)
    return {"skills": n, "agents": agents, "last_scan": last_scan(store), "conflicts": len(conflicts(store))}


# ------------------------------------------------------------------ suggesting

# A file's extension is itself metadata about which skill it needs, even when the filename says nothing:
# "report-v3.docx" should surface the office skill. The hints are the one curated mapping; everything else
# is the stem tokens and the registry's own name/description.
EXTENSION_HINTS = {
    "doc": "office", "docx": "office", "xlsx": "office", "pptx": "office", "pdf": "office", "csv": "office",
    "blend": "blender", "fbx": "blender", "glb": "blender", "gltf": "blender", "obj": "blender",
    "html": "design", "css": "design", "scss": "design", "svg": "design", "vue": "design", "jsx": "design",
    "png": "asset", "jpg": "asset", "jpeg": "asset", "webp": "asset", "gif": "asset", "ico": "asset",
    "mp4": "video", "mov": "video", "webm": "video", "mp3": "audio", "wav": "audio",
    "md": "docs", "rst": "docs", "txt": "docs",
    "py": "python", "ts": "typescript", "tsx": "typescript", "js": "javascript", "go": "golang",
    "rs": "rust", "kt": "kotlin", "swift": "swift", "cs": "dotnet", "java": "java",
}

# The brief must stay fast, so suggestion fills at most this many uncached skill vectors per call; the
# rest is keyword-only until a later call fills them in.
EMBED_BUDGET = 2


def suggest(store, paths: list[str], text: str = "", limit: int = 3) -> list[dict[str, Any]]:
    """Which skills fit what this session is about to touch.

    Two layers, chosen automatically: the keyword layer (file stems, extension hints, the handoff's Next
    line scored against names and descriptions — always on, zero cost) and, when an embedding backend is
    configured, the vector layer (cosine between the session context and each skill's name+description,
    cached per skill content hash). With no backend the answer is keyword-only; with one, a semantic match
    with no shared words can still win. The budget keeps a suggestion inside a brief fast.
    """
    ensure(store.db)
    terms: set[str] = set()
    context_words: list[str] = []
    for p in paths or []:
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        ext = os.path.splitext(p)[1].lstrip(".").lower()
        for t in _tokens(stem):
            if len(t) > 2:
                terms.add(t)
                context_words.append(t)
        if ext in EXTENSION_HINTS:
            terms.add(EXTENSION_HINTS[ext])
            context_words.append(EXTENSION_HINTS[ext])
    context_words += _tokens(text or "")
    if not terms and not context_words:
        return []

    rows = store.db.execute("SELECT * FROM skills").fetchall()
    if not rows:
        return []

    keyword: dict[str, float] = {}
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        nw, dw = _tokens(r["name"]), _tokens(r["description"] or "")
        ntext, dtext = r["name"].lower(), (r["description"] or "").lower()
        matched = sorted(t for t in terms if _hit(t, nw, ntext) or _hit(t, dw, dtext))
        kw = sum(3 if _hit(t, nw, ntext) else 1 for t in matched)
        m = merged.setdefault(r["name"], {"name": r["name"], "description": r["description"] or "",
                                          "invoke": r["invoke"] or "", "agents": [], "matched": [],
                                          "score": 0.0, "similarity": 0.0, "sha": r["sha"] or ""})
        m["agents"].append(r["agent"])
        m["invoke"] = m["invoke"] or (r["invoke"] or "")
        m["matched"] = sorted(set(m["matched"]) | set(matched))
        m["sha"] = m["sha"] or (r["sha"] or "")
        if kw:
            keyword[r["name"]] = max(keyword.get(r["name"], 0.0), kw)

    if not keyword and not context_words:
        return []

    similarity: dict[str, float] = {}
    qvec = embed.embed(" ".join(context_words)[:8000]) if context_words else None
    if qvec is not None:
        cached = {ref: vec for ref, vec in store.db.execute(
            "SELECT ref, vec FROM vectors WHERE src='skill' AND scope='skill'")}
        used_budget = 0
        for name, m in merged.items():
            ref = f"{name}:{m['sha']}" if m["sha"] else name
            vec = cached.get(ref)
            if vec is None and used_budget < EMBED_BUDGET:
                vec = embed.embed(f"{name} {m['description']}"[:8000])
                if vec is not None:
                    store.put_vector("skill", ref, "skill", vec)
                    used_budget += 1
            if vec is not None:
                similarity[name] = max(0.0, embed.similarity(qvec, vec))

    max_kw = max(keyword.values()) if keyword else 1.0
    for name, m in merged.items():
        kw_norm = min(1.0, keyword.get(name, 0.0) / max(3.0, max_kw))
        cos = similarity.get(name, 0.0)
        # Semantic match can win without a shared word; a keyword match can win without a backend. With
        # both, they average — a skill both named and semantically close sorts first.
        m["score"] = round(0.55 * kw_norm + 0.45 * cos, 3)
        m["similarity"] = round(cos, 3)

    out = sorted(merged.values(), key=lambda m: (-m["score"], m["name"]))[:limit]
    for m in out:
        m["agents"] = sorted(set(m["agents"]), key=lambda a: (a != "any", a))
    return out


RECOMMEND_FILE = ".memdex/skills.json"


def recommendations(store, project_root: Optional[str]) -> list[dict[str, Any]]:
    """The skills a project names in .memdex/skills.json, with registry state attached.

    {"recommend": ["office", "slides"]} — a checkout says which skills its work is done with, so a fresh
    session reaches for them instead of rediscovering them one search at a time.
    """
    ensure(store.db)
    path = os.path.join(project_root or "", RECOMMEND_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = [str(n).strip() for n in (json.load(f).get("recommend") or []) if str(n).strip()]
    except (OSError, ValueError):
        return []
    known = {r["name"] for r in store.db.execute("SELECT DISTINCT name FROM skills")}
    return [{"name": n, "known": n in known} for n in names]


def write_recommendation(project_root: Optional[str], names: list[str]) -> str:
    """Write {"recommend": [names]} into the project's .memdex/skills.json (create or replace)."""
    path = os.path.join(project_root or "", RECOMMEND_FILE)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"recommend": [str(n).strip() for n in names if str(n).strip()]}, f, indent=2)
        f.write("\n")
    return path
