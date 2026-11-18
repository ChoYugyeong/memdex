"""A handoff written from evidence when a session ends without one.

Agents forget to call `handoff`: they run out of context, the user closes the window, or the task simply
ends. The next session then starts from nothing, which is the one thing memdex exists to prevent. This
module drafts the handoff from what can be observed without a model:

- the session transcript, when a host hook passes one (Claude Code SessionEnd / PreCompact): the first
  request, the last thing the agent said, and the files its own edit tools wrote;
- git: uncommitted paths and the commits made since the session started;
- memdex itself: the agent's claims, its open tasks, and the notes it saved.

It never overrides a handoff the agent wrote, writes nothing when there is no evidence of work, and
replaces its own earlier draft from the same session instead of piling up copies. The draft is labelled
"(auto)" so a reader knows no agent vouched for it.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

from . import scan
from .project import Project, git
from .store import Store, to_rel

AUTO_PREFIX = "auto: "
AUTO_MARK = "(auto)"
# Without a transcript there is no session start to read; look back this far and no further.
DEFAULT_WINDOW = 12 * 3600.0
# A long transcript is read as its head (the request) plus its tail (the outcome and recent edits).
HEAD_BYTES = 1 << 20
TAIL_BYTES = 16 << 20
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "str_replace_editor", "apply_patch", "patch"}
OPEN_STATES = ("in_progress", "blocked", "reported", "todo")
# Credentials an agent may have echoed; a handoff is shared and exportable, so they never travel in one.
_SECRET = re.compile(
    r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|glpat|AKIA|AIza)[-_A-Za-z0-9]{12,}"
    r"|\b(?:api[_-]?key|secret|token|password|passwd|authorization)\b\s*[:=]\s*\S+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def enabled() -> bool:
    return os.environ.get("MEMDEX_AUTO_HANDOFF", "1").strip().lower() not in ("0", "off", "false", "no")


def _clean(text: Any, limit: int) -> str:
    """One line, printable, without credentials, at most `limit` characters."""
    flat = " ".join("".join(ch for ch in str(text or "") if ch.isprintable() or ch.isspace()).split())
    flat = _SECRET.sub("[redacted]", flat)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _stamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _text_of(content: Any) -> str:
    """The prose of a transcript message: plain strings and text blocks, never tool results."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_prompt(text: str) -> bool:
    """A request a person typed, not a hook echo, command wrapper or injected reminder."""
    head = text.lstrip()
    return bool(head) and not head.startswith(("<", "[Request interrupted", "Caveat:"))


def _lines(path: str):
    """Transcript lines: all of a small file, the head and the tail of a large one."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= HEAD_BYTES + TAIL_BYTES:
            yield from f
            return
        for raw in f:
            yield raw
            if f.tell() >= HEAD_BYTES:
                break
        f.seek(size - TAIL_BYTES)
        f.readline()  # the seek lands mid-line; drop the fragment
        yield from f


def read_transcript(path: str, project: Project) -> dict[str, Any]:
    """What a Claude Code style JSONL transcript says happened. Tolerant: unknown shapes are skipped."""
    out: dict[str, Any] = {"prompts": [], "last": "", "edited": {}, "started_at": None}
    if not path or not os.path.isfile(path):
        return out
    edited: dict[str, float] = out["edited"]  # path -> time of its latest edit
    try:
        for raw in _lines(path):
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or row.get("isSidechain") or row.get("isMeta"):
                continue
            at = _stamp(row.get("timestamp")) or 0.0
            if out["started_at"] is None and at:
                out["started_at"] = at
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            kind, content = row.get("type") or msg.get("role"), msg.get("content")
            if kind == "user":
                text = _text_of(content)
                if _is_prompt(text):
                    out["prompts"].append((at, text[:400]))
            elif kind == "assistant":
                text = _text_of(content).strip()
                if text:
                    out["last"] = text
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if str(block.get("name") or "").lower() not in EDIT_TOOLS:
                        continue
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    rel = to_rel(project, str(args.get("file_path") or args.get("path") or ""))
                    if rel and not scan.is_secret(rel):
                        edited[rel] = at
    except OSError:
        pass
    return out


def _git_state(project: Project, since: float) -> tuple[list[str], list[str]]:
    """(uncommitted paths, "sha subject" of commits since `since`), both empty outside git."""
    if not project.is_git:
        return [], []
    dirty = []
    # Porcelain v2: every line starts with its record type, so `git()` stripping the output cannot shift
    # a column the way it does to v1's leading-space status.
    status = git(["-c", "core.quotepath=off", "status", "--porcelain=v2", "--untracked-files=normal"],
                 project.root) or ""
    for line in status.splitlines():
        kind, fields = line[:1], {"1": 8, "2": 9, "u": 10}.get(line[:1])
        if kind == "?":
            path = line[2:]
        elif fields and len(line.split(" ", fields)) > fields:
            path = line.split(" ", fields)[fields].split("\t")[0]
        else:
            continue
        path = path.strip().strip('"')
        if path and not scan.is_secret(path):
            dirty.append(path)
    log = git(["log", f"--since=@{int(since)}", "--no-merges", "-n", "8", "--format=%h %s"], project.root) or ""
    return dirty, [c for c in log.splitlines() if c.strip()]


def _family(agent: str) -> str:
    return agent.split("@", 1)[0]


def _latest_handoff(store: Store, project: Project, agent: str):
    return store.db.execute(
        "SELECT id, title, body, created_at FROM notes WHERE project=? AND kind='handoff' AND author=? "
        "AND archived=0 AND superseded_by IS NULL ORDER BY id DESC LIMIT 1", (project.key, agent)).fetchone()


def is_auto(note) -> bool:
    return bool(note) and str(note["title"] or "").startswith(AUTO_PREFIX)


def _shown(paths: list[str], n: int = 8) -> str:
    return ", ".join(paths[:n]) + (f" +{len(paths) - n} more" if len(paths) > n else "")


def draft(store: Store, project: Project, agent: str, transcript: str = "",
          since: Optional[float] = None, explicit: bool = False,
          now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The handoff to save, or None when the agent already left one or nothing shows work was done.

    `explicit` is a person or agent asking for the draft (`memdex handoff --auto`): uncommitted paths then
    count as evidence. From a hook they do not by themselves, because in a shared checkout they may be
    another agent's work.
    """
    now = now or time.time()
    seen = read_transcript(transcript, project)
    start = since or seen["started_at"] or now - DEFAULT_WINDOW
    previous = _latest_handoff(store, project, agent)
    own = bool(previous) and previous["created_at"] >= start and not is_auto(previous)
    if own:
        # The agent handed off and kept working: draft only what came after its own handoff.
        start = previous["created_at"]
    edited = [p for p, at in seen["edited"].items() if not own or at > start]
    prompts = [text for at, text in seen["prompts"] if not own or at > start]

    dirty, commits = _git_state(project, start)
    claimed = [c["path"] for c in store.active_claims(project) if c["agent"] == agent and c["path"]]
    mine = {agent, _family(agent)}
    tasks = [t for t in store.task_rows(project) if t["owner"] in mine and t["status"] in OPEN_STATES]
    tasks.sort(key=lambda t: OPEN_STATES.index(t["status"]))
    notes = store.db.execute(
        "SELECT id, kind, title FROM notes WHERE project=? AND author=? AND archived=0 AND created_at>? "
        "AND kind IN ('decision','pitfall') ORDER BY id", (project.key, agent, start)).fetchall()

    # A session that only read and answered leaves nothing to resume; a draft would be noise in the brief.
    if own:
        evidence = edited or commits  # its handoff already covers the rest
    else:
        evidence = edited or commits or claimed or notes or (dirty and (explicit or prompts))
    if not evidence:
        return None

    asked = _clean(prompts[0] if prompts else "", 220)
    last = _clean(seen["last"], 420)
    done = [f"{AUTO_MARK} session ended without a handoff; drafted from evidence, not vouched for by the agent."]
    if own:
        done[0] = (f"{AUTO_MARK} work continued after handoff #{previous['id']} and the session ended "
                   "without another; drafted from evidence, not vouched for by the agent.")
    if asked:
        done.append(f'Asked: "{asked}"')
    if edited:
        done.append(f"Edited: {_shown(edited)}.")
    if commits:
        done.append("Commits: " + "; ".join(_clean(c, 90) for c in commits[:5]) + ".")
    if last:
        done.append(f'Last message: "{last}"')

    nxt = []
    if tasks:
        nxt.append("Open tasks: " + "; ".join(
            f"#{t['id']} [{t['status']}] {_clean(t['title'], 70)}" for t in tasks[:5]) + ".")
    if dirty:
        loose = [p for p in dirty if p not in edited]
        nxt.append(f"Uncommitted: {_shown(dirty)}" + (
            f" ({len(loose)} not edited by this session; check who owns them)." if edited and loose else "."))
    if claimed:
        nxt.append(f"Still claimed by {agent}: {_shown(claimed, 5)} (expires on its own).")
    if not nxt:
        nxt.append("Nothing open was recorded; confirm with the last message before continuing.")

    parts = ["Done: " + " ".join(done), "Next: " + " ".join(nxt)]
    for kind, label in (("decision", "Decisions"), ("pitfall", "Pitfalls")):
        picked = [f"#{n['id']} {_clean(n['title'], 70)}" for n in notes if n["kind"] == kind]
        if picked:
            parts.append(f"{label}: " + "; ".join(picked[:5]))
    body = "\n".join(parts)
    subject = asked or (f"edited {_shown(edited, 3)}" if edited else
                        f"{len(commits)} commit(s)" if commits else
                        f"working on {_shown(claimed or dirty, 3)}" if claimed or dirty else "session notes")
    redraft = is_auto(previous) and previous["created_at"] >= start
    return {"title": _clean(AUTO_PREFIX + subject, 80), "body": body, "files": (edited or claimed)[:12],
            "supersedes": previous["id"] if redraft else None,
            "unchanged": bool(redraft and previous["body"] == body)}


def save(store: Store, project: Project, agent: str, transcript: str = "",
         since: Optional[float] = None, explicit: bool = False) -> str:
    """Draft and store the handoff; returns one line saying what happened. Claims are left to expire:
    an ended process is not proof the work is over (a resumed or compacted session continues it)."""
    if not enabled():
        return "auto handoff is off (MEMDEX_AUTO_HANDOFF=0)."
    made = draft(store, project, agent, transcript, since, explicit)
    if made is None:
        return "no auto handoff: the agent left its own, or nothing shows work was done."
    if made["unchanged"]:
        return "auto handoff unchanged."
    nid = store.add_note(project, "handoff", made["title"], made["body"], agent, made["files"] or None,
                         supersedes=made["supersedes"])
    replaced = f" (replaces #{made['supersedes']})" if made["supersedes"] else ""
    return f"auto handoff #{nid} saved{replaced}; the next session will see it in project_brief."


def parse_since(value: str, now: Optional[float] = None) -> Optional[float]:
    """`90m`, `2h`, `1d` back from now, or an ISO-8601 time; None when empty."""
    value = (value or "").strip()
    if not value:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", value.lower())
    if m:
        return (now or time.time()) - float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    stamp = _stamp(value)
    if stamp is None:
        raise ValueError("--since takes 90m, 2h, 1d or an ISO-8601 time")
    return stamp


# ---------------------------------------------------------------------------- pickup counting

PICKUP = "handoff_pickup:"
# The author's own brief straight after writing a handoff is not a later session picking it up.
SAME_AGENT_GAP = 600.0


def record_pickup(store: Store, project: Project, note, reader: str) -> None:
    """Remember that `reader`'s brief showed this handoff. One counter row per (handoff, reader), kept in
    the per-project counter table so no schema change is needed; never fails or stalls the brief."""
    try:
        store._bump_stats(project, {f"{PICKUP}{int(note['id'])}:{reader}": 1})
    except Exception:  # noqa: BLE001 - counting must not cost anyone a brief
        pass


def pickup_stats(store: Store) -> dict[str, int]:
    """Across every project: handoffs written, how many were drafts, and how many reached a reader.

    `other_agent` counts handoffs shown to an agent that did not write them; `later_session` counts the
    rest that the author itself was shown again at least ten minutes after writing.
    """
    db = store.db
    written = db.execute("SELECT count(*) FROM notes WHERE kind='handoff'").fetchone()[0]
    auto = db.execute("SELECT count(*) FROM notes WHERE kind='handoff' AND title LIKE ?",
                      (AUTO_PREFIX + "%",)).fetchone()[0]
    other: set[tuple[str, int]] = set()
    later: set[tuple[str, int]] = set()
    rows = db.execute("SELECT project, name, updated_at FROM search_stats WHERE name LIKE ?",
                      (PICKUP + "%",)).fetchall()
    for row in rows:
        ident, _, reader = row["name"][len(PICKUP):].partition(":")
        if not ident.isdigit():
            continue
        note = db.execute("SELECT author, created_at FROM notes WHERE project=? AND id=?",
                          (row["project"], int(ident))).fetchone()
        if note is None:
            continue
        key = (row["project"], int(ident))
        if reader != (note["author"] or ""):
            other.add(key)
        elif (row["updated_at"] or 0) - (note["created_at"] or 0) >= SAME_AGENT_GAP:
            later.add(key)
    return {"written": written, "auto": auto, "other_agent": len(other),
            "later_session": len(later - other), "tracked": len({(r["project"], r["name"].split(":")[1])
                                                                 for r in rows if r["name"].count(":") >= 2})}
