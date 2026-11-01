"""`memdex doctor`: one command that explains why memdex is not working.

A single database shared by every agent is easy to set up and hard to debug
from a bug report. ``doctor`` prints the facts a maintainer would ask for:
where the database is, whether it is writable, which schema version it is on,
whether the current project is indexed, and which optional integrations are on.
Nothing is modified beyond creating the database on a first run.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from . import __version__, embed, kensaku
from .project import resolve
from .store import Store, default_path

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, status: str, label: str, detail: str = "") -> "Report":
        self.checks.append(Check(status, label, detail))
        return self

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def warned(self) -> bool:
        return any(c.status == WARN for c in self.checks)

    def render(self) -> str:
        lines = [f"memdex doctor · {__version__}"]
        for c in self.checks:
            lines.append(f"{_ICON[c.status]} {c.label:<16}{c.detail}".rstrip())
        lines.append("")
        if self.failed:
            lines.append("fix the ✗ lines first; everything else is optional")
        elif self.warned:
            lines.append("usable; ! lines are optional or worth a look")
        else:
            lines.append("all good")
        return "\n".join(lines)


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    seconds = max(0.0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit} ago"
    return "just now"


def _ollama_reachable(timeout: float = 2.0) -> tuple[bool, str]:
    url = os.environ.get("MEMDEX_OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
        return True, f"{len(data.get('models') or [])} model(s)"
    except Exception as e:  # noqa: BLE001 - doctor reports, never raises
        return False, str(e)


def run(path: Optional[str] = None, db_path: Optional[str] = None, offline: bool = False) -> Report:
    """Collect checks for the database, the current project and optional extras."""
    rep = Report()

    v = sys.version_info
    if v >= (3, 9):
        rep.add(OK, "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        rep.add(FAIL, "python", f"{v.major}.{v.minor} — memdex needs 3.9 or newer")

    db_path = db_path or default_path()
    existed = os.path.exists(db_path)
    try:
        store = Store(db_path)
    except RuntimeError as e:  # database written by a newer memdex
        rep.add(FAIL, "database", str(e))
        return rep
    except (sqlite3.Error, OSError) as e:  # noqa: BLE001
        rep.add(FAIL, "database", f"{db_path}: {e}")
        return rep

    try:
        size = os.path.getsize(store.path) if existed else 0
        rep.add(OK, "database", f"{store.path} ({size / 1024:,.0f} KB, schema v{store.schema_version}, "
                                f"trigram={store.trigram}{'' if existed else ', created now'})")

        try:
            store.db.execute("BEGIN IMMEDIATE")
            store.db.execute("ROLLBACK")
            rep.add(OK, "writable", "WAL mode; several agents can write at once")
        except sqlite3.Error as e:
            rep.add(FAIL, "writable", f"{e} — check file permissions")

        project = resolve(path or os.getcwd())
        count, last = store.db.execute(
            "SELECT count(*), max(indexed_at) FROM files WHERE root=?", (project.root,)).fetchone()
        if count:
            rep.add(OK, "project", f"{project.name} · {count:,} files indexed · {_ago(last)}")
        else:
            rep.add(WARN, "project", f"{project.name} · not indexed yet (first tool call indexes it)")

        agent = os.environ.get("MEMDEX_AGENT", "").strip()
        rep.add(OK, "agent", agent or "(unset — the MCP client's name is used)")

        try:
            from . import connect as connect_mod
            status, detail = connect_mod.summary()
            rep.add(status, "clients", detail)
        except Exception as e:  # noqa: BLE001 - doctor reports, never raises
            rep.add(WARN, "clients", str(e))

        try:
            from .tools import Session
            tools = Session(store, agent_name="doctor").tools()
            rep.add(OK, "mcp tools", f"{len(tools)} tools over stdio")
        except Exception as e:  # noqa: BLE001
            rep.add(WARN, "mcp tools", str(e))
    finally:
        store.close()

    cmd = kensaku.command()
    if cmd:
        rep.add(OK, "kensaku", f"{cmd} (optional, on)")
    else:
        rep.add(OK, "kensaku", "off (optional: install kensaku-cli for PDF/OCR search)")

    cfg = embed.config()
    if not cfg:
        rep.add(OK, "ollama", "off (optional: MEMDEX_EMBED=ollama:bge-m3 for semantic search)")
    elif offline:
        rep.add(WARN, "ollama", f"{cfg[1]} configured, not checked (--offline)")
    else:
        reachable, detail = _ollama_reachable()
        rep.add(OK if reachable else WARN, "ollama",
                f"{cfg[1]} · {detail}" + ("" if reachable else " — search falls back to keyword"))

    return rep
