"""A handoff written from evidence when a session ends without one.

Agents forget to call `handoff`: they run out of context, the user closes the window, or the task simply
ends. The next session then starts from nothing, which is the one thing memdex exists to prevent. This
module drafts the handoff from what can be observed without a model:

- the session transcript, when a host hook passes one (Claude Code SessionEnd / PreCompact): the first
  and the latest request, the last thing the agent said, and the files its own edit tools wrote;
- git: uncommitted paths and the commits made since the session started;
- memdex itself: the agent's claims, its open tasks, and the notes it saved.

The draft is saved in the project that was edited, which is not always the folder the session started
in: a session run from a parent folder leaves one draft in each repository it changed. It never overrides a
handoff the agent wrote, writes nothing when there is no evidence of work, and replaces its own earlier
draft from the same session instead of piling up copies. The draft is labelled
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
# Besides its own project, a session's draft goes to at most this many other repositories it edited.
MAX_OTHER_PROJECTS = 3
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


_WRAPPER = re.compile(r"\A\s*<([A-Za-z][\w:-]*)(?:\s[^>]*)?>.*?</\1>\s*", re.S)
_NOT_A_REQUEST = ("<", "[Request interrupted", "Caveat:", "Another Claude session sent a message")


def _prompt_text(text: str) -> str:
    """What a person typed, or "" for a hook echo, command wrapper or injected reminder.

    Hosts prepend blocks such as <system-reminder>…</system-reminder> to the message a person sent; the
    request is what follows them, so the leading blocks are dropped rather than the whole message.
    """
    while True:
        trimmed = _WRAPPER.sub("", text, count=1)
        if trimmed == text:
            break
        text = trimmed
    text = text.strip()
    return "" if not text or text.startswith(_NOT_A_REQUEST) else text


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
    """What a Claude Code style JSONL transcript says happened. Tolerant: unknown shapes are skipped.

    `edited` maps each absolute path the agent's edit tools wrote to the time of its latest edit; a
    relative path is taken from the session's project. Which project a path belongs to is `targets`' job.
    """
    out: dict[str, Any] = {"prompts": [], "last": "", "edited": {}, "started_at": None}
    if not path or not os.path.isfile(path):
        return out
    edited: dict[str, float] = out["edited"]
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
                text = _prompt_text(_text_of(content))
                if text:
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
                    target = str(args.get("file_path") or args.get("path") or "").strip().strip('"')
                    if not target:
                        continue
                    if not os.path.isabs(target):
                        target = os.path.join(project.root, target)
                    edited[os.path.normpath(target)] = at
    except OSError:
        pass
    return out


def _inside(root: str, path: str) -> bool:
    """Whether `path` is `root` or below it. Both are compared in their real form: a transcript may name a
    file through a symlink (/var for /private/var) or a Windows short name (RUNNER~1), and a project root
    is always the resolved one."""
    root, path = (os.path.normcase(os.path.realpath(p)) for p in (root, path))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def targets(edited: dict[str, float], home: Project) -> list[tuple[Project, dict[str, float]]]:
    """Group edited paths by the project they belong to, the session's own project first.

    A session often runs from a folder above the repositories it edits; a handoff saved under that folder
    would never reach the brief of the project that was changed. An edit belongs to the git repository
    containing it (all worktrees of one repository are one project), else to the session's project when it
    lies inside it. Edits anywhere else (temp folders, a home directory) belong to no project and are left
    out, as are paths that look like secrets.
    """
    from . import project as projects
    groups: dict[str, tuple[Project, dict[str, float]]] = {home.key: (home, {})}
    roots: list[Project] = [home] if home.is_git else []
    plain: set[str] = set()  # folders already found to be outside any repository: one git call each
    for path, at in edited.items():
        path = os.path.realpath(path)  # the same file must not look like two, or like one outside its project
        owner = next((p for p in sorted(roots, key=lambda p: -len(p.root)) if _inside(p.root, path)), None)
        if owner is None:
            folder = os.path.dirname(path)
            while folder and not os.path.isdir(folder):  # a file in a folder that has since been removed
                parent = os.path.dirname(folder)
                if parent == folder:
                    break
                folder = parent
            found = None
            if folder not in plain and os.path.isdir(folder):
                try:
                    found = projects.resolve(folder)
                except (OSError, ValueError):
                    found = None
            if found is not None and found.is_git:
                owner = found
                roots.append(found)
            elif _inside(home.root, path):
                plain.add(folder)
                owner = home
            else:
                plain.add(folder)
                continue
        rel = to_rel(owner, path)
        if not rel or scan.is_secret(rel):
            continue
        files = groups.setdefault(owner.key, (owner, {}))[1]
        files[rel] = max(at, files.get(rel, 0.0))
    first = groups.pop(home.key)
    rest = sorted(groups.values(), key=lambda g: -len(g[1]))
    return [first] + rest[:MAX_OTHER_PROJECTS]


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


def _same_agent(column: str = "author") -> str:
    """SQL for "written by this agent, from any checkout": claude, claude@repo, claude@repo-worktree."""
    return f"({column}=? OR {column} LIKE ? ESCAPE '\\')"


def _agent_args(agent: str) -> tuple[str, str]:
    family = _family(agent)
    return family, family.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "@%"


def _latest_handoff(store: Store, project: Project, agent: str, drafts: Optional[bool] = None, tag: str = ""):
    """This agent's newest live handoff in the project; `drafts` narrows to automatic ones (True, and to the
    session `tag` when given) or to the ones it wrote itself (False)."""
    where, args = "", [project.key, *_agent_args(agent)]
    if drafts is True:
        where = " AND title LIKE ?"
        args.append(AUTO_PREFIX + "%")
        if tag:
            where += " AND body LIKE ?"
            args.append(f"%{AUTO_MARK} {tag} %")
    elif drafts is False:
        where = " AND title NOT LIKE ?"
        args.append(AUTO_PREFIX + "%")
    return store.db.execute(
        "SELECT id, title, body, created_at, author FROM notes WHERE project=? AND kind='handoff' AND "
        f"{_same_agent()} AND archived=0 AND superseded_by IS NULL{where} ORDER BY id DESC LIMIT 1",
        args).fetchone()


def is_auto(note) -> bool:
    return bool(note) and str(note["title"] or "").startswith(AUTO_PREFIX)


def _shown(paths: list[str], n: int = 8) -> str:
    return ", ".join(paths[:n]) + (f" +{len(paths) - n} more" if len(paths) > n else "")


def _draft(store: Store, project: Project, agent: str, seen: dict[str, Any], files: dict[str, float],
           since: Optional[float], explicit: bool, session: str, elsewhere: str, primary: bool,
           now: float) -> Optional[dict[str, Any]]:
    start = since or seen["started_at"] or now - DEFAULT_WINDOW
    tag = f"session {session[:8]}" if session else ""
    own = _latest_handoff(store, project, agent, drafts=False)
    own = own if own is not None and own["created_at"] >= start else None
    if own is not None:
        # The agent handed off and kept working: draft only what came after its own handoff.
        start = own["created_at"]
    # The draft this one replaces is this session's own; another session's draft is not ours to supersede.
    earlier = _latest_handoff(store, project, agent, drafts=True, tag=tag)
    if earlier is not None and (earlier["created_at"] < start or (not tag and earlier["author"] != agent)):
        earlier = None
    edited = [p for p, at in files.items() if own is None or at > start]
    prompts = [text for at, text in seen["prompts"] if own is None or at > start]

    dirty, commits = _git_state(project, start)
    family = _family(agent)
    claimed = [c["path"] for c in store.active_claims(project)
               if c["path"] and (c["agent"] == agent or _family(c["agent"]) == family)]
    tasks = [t for t in store.task_rows(project) if t["owner"] in (agent, family) and t["status"] in OPEN_STATES]
    tasks.sort(key=lambda t: OPEN_STATES.index(t["status"]))
    notes = store.db.execute(
        f"SELECT id, kind, title FROM notes WHERE project=? AND {_same_agent()} AND archived=0 "
        "AND created_at>? AND kind IN ('decision','pitfall') ORDER BY id",
        (project.key, *_agent_args(agent), start)).fetchall()

    # A session that only read and answered leaves nothing to resume; a draft would be noise in the brief.
    if not primary or own is not None:
        # Another project the session touched, or work after its own handoff: only what it provably did.
        evidence = edited or (primary and commits)
    else:
        evidence = edited or commits or claimed or notes or (dirty and (explicit or prompts))
    if not evidence:
        return None

    asked = _clean(prompts[0] if prompts else "", 220)
    latest = _clean(prompts[-1] if len(prompts) > 1 else "", 220)
    last = _clean(seen["last"], 420)
    who = f"{AUTO_MARK} {tag or 'session'}"
    done = [f"{who} ended without a handoff; drafted from evidence, not vouched for by the agent."]
    if own is not None:
        done[0] = (f"{who} kept working after handoff #{own['id']} and ended without another; drafted from "
                   "evidence, not vouched for by the agent.")
    if asked:
        done.append(f'Asked: "{asked}"')
    if latest and latest != asked:
        done.append(f'Latest request: "{latest}"')
    if edited:
        done.append(f"Edited: {_shown(edited)}.")
    if elsewhere:
        done.append(f"Also edited in other projects: {elsewhere}.")
    if commits:
        # Everyone's commits in that window: git cannot say which session made one.
        done.append("Commits since the session began (any author): "
                    + "; ".join(_clean(c, 90) for c in commits[:5]) + ".")
    if last:
        done.append(f'Last message: "{last}"')

    nxt = []
    if tasks:
        nxt.append("Open tasks: " + "; ".join(
            f"#{t['id']} [{t['status']}] {_clean(t['title'], 70)}" for t in tasks[:5]) + ".")
    if dirty:
        # git reports a new folder as one entry; it is the session's if the session edited a file in it.
        loose = [p for p in dirty if p not in edited
                 and not any(e.startswith(p.rstrip("/") + "/") for e in edited)]
        nxt.append(f"Uncommitted: {_shown(dirty)}" + (
            f" ({len(loose)} not edited by this session; check who owns them)." if edited and loose else "."))
    if claimed:
        nxt.append(f"Still claimed by {family}: {_shown(claimed, 5)} (expires on its own).")
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
    return {"title": _clean(AUTO_PREFIX + subject, 80), "body": body, "files": (edited or claimed)[:12],
            "supersedes": earlier["id"] if earlier is not None else None,
            "unchanged": bool(earlier is not None and earlier["body"] == body),
            "project": project, "agent": agent}


def drafts(store: Store, project: Project, agent: str, transcript: str = "",
           since: Optional[float] = None, explicit: bool = False, session: str = "",
           now: Optional[float] = None) -> list[dict[str, Any]]:
    """The handoffs to save: one for the session's project when it shows work, and one for each other
    repository the session's edit tools wrote to."""
    now = now or time.time()
    seen = read_transcript(transcript, project)
    groups = targets(seen["edited"], project)
    family = _family(agent)
    out = []
    for index, (target, files) in enumerate(groups):
        others = [f"{g.name} ({len(f)} file{'s' if len(f) != 1 else ''})"
                  for g, f in groups if g.key != target.key and f]
        # In another repository the agent is known by that checkout's name, as it would be had it run there.
        who = agent if index == 0 else f"{family}@{os.path.basename(target.root.rstrip('/' + os.sep))}"
        made = _draft(store, target, who, seen, files, since, explicit, session, ", ".join(others),
                      primary=index == 0, now=now)
        if made is not None:
            out.append(made)
    return out


def draft(store: Store, project: Project, agent: str, transcript: str = "",
          since: Optional[float] = None, explicit: bool = False, session: str = "",
          now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The handoff for the session's own project, or None when the agent already left one or nothing shows
    work was done there.

    `explicit` is a person or agent asking for the draft (`memdex handoff --auto`): uncommitted paths then
    count as evidence. From a hook they do not by themselves, because in a shared checkout they may be
    another agent's work.
    """
    made = [d for d in drafts(store, project, agent, transcript, since, explicit, session, now)
            if d["project"].key == project.key]
    return made[0] if made else None


def save(store: Store, project: Project, agent: str, transcript: str = "",
         since: Optional[float] = None, explicit: bool = False, session: str = "") -> str:
    """Draft and store the handoffs; returns one line saying what happened. Claims are left to expire:
    an ended process is not proof the work is over (a resumed or compacted session continues it)."""
    if not enabled():
        return "auto handoff is off (MEMDEX_AUTO_HANDOFF=0)."
    made = drafts(store, project, agent, transcript, since, explicit, session)
    if not made:
        return "no auto handoff: the agent left its own, or nothing shows work was done."
    lines = []
    for item in made:
        where = "" if item["project"].key == project.key else f" in {item['project'].name}"
        if item["unchanged"]:
            lines.append(f"auto handoff unchanged{where}.")
            continue
        nid = store.add_note(item["project"], "handoff", item["title"], item["body"], item["agent"],
                             item["files"] or None, supersedes=item["supersedes"])
        replaced = f" (replaces #{item['supersedes']})" if item["supersedes"] else ""
        lines.append(f"auto handoff #{nid} saved{where}{replaced}; the next session will see it in project_brief.")
    return " ".join(lines)


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
