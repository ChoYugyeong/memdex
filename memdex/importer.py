"""Turning the prose a project already has into notes.

Every project of a certain age has an `AGENTS.md`, a `CLAUDE.md`, a `CONTRIBUTING.md` — the knowledge that a
new session needs is *already written*, just somewhere memdex cannot rank, cite or expire it. `memdex import`
reads those files once, splits them at their headings, and stores each section as a note with refs to the
files it mentions, so the first session in a repo does not start from an empty memory (cold start is the
one limit no ranking algorithm fixes; see docs/LIMITS.md).

It is deliberately dumb: headings become titles, mentions of files that exist become refs, and a section
that is already in the store is skipped. There is no model in the loop and no rewriting of the source.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from . import skills
from .project import Project
from .store import Store

# A note is a memory, not a copy: a section longer than this is cut, and the file stays the source of truth.
MAX_SECTION_CHARS = 4000
MIN_SECTION_CHARS = 20
# Paths in prose are usually in backticks, sometimes in quotes; only ones that exist become refs.
PATH_RE = re.compile(r"[`\"']([\w./\\-]+\.[A-Za-z0-9]{1,6})[`\"']")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def split_sections(text: str) -> list:
    """(title, body) pairs, one per heading; text before the first heading is its own section."""
    sections: list = []
    title, body = "", []
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if title or any(part.strip() for part in body):
                sections.append((title, "\n".join(body).strip()))
            title, body = m.group(2).strip(), []
            continue
        body.append(line)
    if title or any(part.strip() for part in body):
        sections.append((title, "\n".join(body).strip()))
    return [(_title or "preamble", body) for _title, body in sections if body]


def refs_in(body: str, project: Project) -> list:
    """The files a section mentions that actually exist in this checkout."""
    found: list = []
    for match in PATH_RE.finditer(body):
        rel = match.group(1).replace("\\", "/").lstrip("./")
        if rel in found:
            continue
        if os.path.isfile(os.path.join(project.root, rel)):
            found.append(rel)
    return found


def run(store: Store, project: Project, paths: list, author: str,
        kind: str = "note") -> dict:
    """Import each file's sections as notes. Returns what happened, per file and in total."""
    total = {"imported": 0, "skipped": 0, "missing": [], "files": []}
    for path in paths:
        full = path if os.path.isabs(path) else os.path.join(project.root, path)
        label = os.path.relpath(full, project.root) if os.path.isabs(full) else path
        if not os.path.isfile(full):
            total["missing"].append(label)
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            total["missing"].append(f"{label} ({e})")
            continue
        file_counts = {"file": label, "imported": 0, "skipped": 0, "hazards": []}
        # The most dangerous text an agent reads is the repository's own instructions (an AGENTS.md is
        # where a hijack plants itself most effectively), so every imported file is scanned for the same
        # patterns skills.verify uses, and the report says what to treat as data instead of instructions.
        file_counts["hazards"] = skills.hazards_in_text(text)
        for title, body in split_sections(text):
            body = body[:MAX_SECTION_CHARS].strip()
            if len(body) < MIN_SECTION_CHARS:
                continue
            exists = store.db.execute(
                "SELECT id FROM notes WHERE project=? AND title=? AND body=? AND archived=0",
                (project.key, title.strip()[:200], body)).fetchone()
            if exists:
                file_counts["skipped"] += 1
                total["skipped"] += 1
                continue
            store.add_note(project, kind, title.strip()[:200], body, author, refs_in(body, project))
            file_counts["imported"] += 1
            total["imported"] += 1
        total["files"].append(file_counts)
    return total


def default_paths(project: Project) -> list:
    """The files a project is most likely to already be keeping its agent knowledge in."""
    candidates = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")
    return [name for name in candidates if os.path.isfile(os.path.join(project.root, name))]
