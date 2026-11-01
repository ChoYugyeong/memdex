"""The tools agents call. Output is short plain text: every byte is a token."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import project as projects
from .project import Project
from .store import NOTE_KINDS, Store

INSTRUCTIONS = """memdex is a memory shared by every AI agent working on this machine.
Use it to avoid re-reading and re-discovering things:
- At the start, call project_brief (latest handoff, pitfalls, messages, who is editing what).
- Before reading a whole file, call file_summary. If it has a fresh summary, that may be enough.
- After you read a file that had no fresh summary, call file_summary_save with 2-6 lines
  (purpose, key functions, gotchas). It stays valid until the file content changes.
- Use where_is for symbol locations and search for past notes before exploring from scratch.
- Record non-obvious decisions and pitfalls with remember. Before you stop, call handoff.
- When other agents may be working too, claim the files you edit and use post/inbox to talk."""


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    s = time.time() - ts
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400 * 2:
        return f"{int(s // 3600)}h ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, Any]
    required: list[str]
    fn: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.params, "required": self.required}}


@dataclass
class Session:
    store: Store
    agent_name: str = ""
    cwd: str = field(default_factory=os.getcwd)
    _project: Optional[Project] = None

    @property
    def project(self) -> Project:
        if self._project is None:
            self._project = projects.resolve(os.environ.get("MEMDEX_PROJECT") or self.cwd)
        return self._project

    @property
    def agent(self) -> str:
        """Agent id like "claude@keychat-uiux": who, and in which checkout."""
        who = os.environ.get("MEMDEX_AGENT") or self.agent_name or "agent"
        who = who.lower().replace(" ", "-")
        return f"{who}@{Path(self.project.root).name}"

    # ------------------------------------------------------------------ tools

    def brief(self, project: str = "") -> str:
        if project:
            self._project = projects.resolve(project)
        return render_brief(self.store, self.project, self.agent)

    def file_summary(self, path: str) -> str:
        info = self.store.file_info(self.project, path)
        if info is None:
            return f"{path}: not indexed (missing, binary, over 512KB, outside the project, or secret-like)."
        head = f"{info['path']} · {info['lang']} · {info['lines']} lines · sha {info['sha'][:8]}"
        if info["summary"]:
            s = info["summary"]
            return f"{head}\nsummary ({s['author']}, {ago(s['created_at'])}, matches current content):\n{s['summary']}\noutline: {clip(info['outline'], 1200)}"
        lines = [head, "no summary for the current content."]
        if info["stale_summary"]:
            s = info["stale_summary"]
            lines.append(f"older summary (file changed since, {ago(s['created_at'])}):\n{s['summary']}")
        lines.append(f"outline: {clip(info['outline'], 1500) or '(none)'}")
        lines.append("After reading the file, save a 2-6 line summary with file_summary_save.")
        return "\n".join(lines)

    def file_summary_save(self, path: str, summary: str) -> str:
        if not summary.strip():
            return "summary is empty."
        sha = self.store.save_summary(self.project, path, summary, self.agent)
        if sha is None:
            return f"{path}: not indexed, summary not saved."
        return f"saved for {path} (sha {sha[:8]}). Valid until the content changes."

    def where_is(self, symbol: str) -> str:
        rows = self.store.find_symbol(self.project, symbol)
        if not rows:
            return f"no symbol matching '{symbol}'. Try search or a reindex (project_brief)."
        return "\n".join(f"{r['path']}:{r['line']} {r['kind']} {r['name']}" for r in rows)

    def search(self, query: str, kinds: Optional[list[str]] = None, limit: int = 6) -> str:
        hits = self.store.search(self.project, query, kinds, max(1, min(int(limit), 20)))
        if not hits:
            return f"nothing found for '{query}'."
        out = []
        for h in hits:
            if h["src"] == "file":
                out.append(f"[file] {h['path']} — {h['snippet']}")
            else:
                stale = f" ⚠ based on files changed since: {', '.join(h['stale'])}" if h["stale"] else ""
                out.append(f"[{h['kind']} #{h['id']}] {h['title']} ({h['author']}, {ago(h['created_at'])}) — "
                           f"{h['snippet']}{stale}")
        return "\n".join(out)

    def remember(self, kind: str, title: str, body: str = "", files: Optional[list[str]] = None,
                 supersedes: Optional[int] = None) -> str:
        if kind not in NOTE_KINDS:
            return f"kind must be one of {', '.join(NOTE_KINDS)}."
        nid = self.store.add_note(self.project, kind, title, body, self.agent, files, supersedes)
        extra = f", replaces #{supersedes}" if supersedes else ""
        return f"saved {kind} #{nid}{extra}."

    def note(self, id: int, action: str = "get") -> str:
        n = self.store.get_note(self.project, int(id))
        if not n:
            return f"note #{id} not found in this project."
        if action == "archive":
            self.store.archive_note(self.project, int(id))
            return f"archived #{id}."
        stale = self.store.stale_refs(self.project, n)
        lines = [f"[{n['kind']} #{n['id']}] {n['title']} ({n['author']}, {ago(n['created_at'])})"]
        if n["superseded_by"]:
            lines.append(f"(replaced by #{n['superseded_by']})")
        if n["body"]:
            lines.append(n["body"])
        if stale:
            lines.append(f"⚠ files changed since this was written: {', '.join(stale)}")
        return "\n".join(lines)

    def handoff(self, done: str, next: str = "", decisions: str = "", pitfalls: str = "",
                files: Optional[list[str]] = None) -> str:
        parts = [f"Done: {done.strip()}"]
        for label, text in (("Next", next), ("Decisions", decisions), ("Pitfalls", pitfalls)):
            if text.strip():
                parts.append(f"{label}: {text.strip()}")
        title = clip(done.splitlines()[0] if done.strip() else "handoff", 80)
        nid = self.store.add_note(self.project, "handoff", title, "\n".join(parts), self.agent, files)
        released = self.store.release(self.project, self.agent)
        tail = f" Released {released} claim(s)." if released else ""
        return f"handoff #{nid} saved; the next session will see it in project_brief.{tail}"

    def post(self, message: str, to: str = "") -> str:
        mid = self.store.post(self.project, self.agent, message, to or None)
        return f"posted #{mid}" + (f" to {to}." if to else " to everyone on this project.")

    def inbox(self, peek: bool = False) -> str:
        msgs = self.store.inbox(self.project, self.agent, peek=bool(peek))
        if not msgs:
            return "no unread messages."
        return "\n".join(f"#{m['id']} {m['sender']}{' → ' + m['recipient'] if m['recipient'] else ''} "
                         f"({ago(m['created_at'])}): {m['body']}" for m in msgs)

    def claim(self, paths: Optional[list[str]] = None, intent: str = "", minutes: int = 120,
              release: bool = False) -> str:
        if release:
            n = self.store.release(self.project, self.agent, paths)
            return f"released {n} claim(s)."
        if not paths:
            return render_claims(self.store.active_claims(self.project)) or "no active claims."
        conflicts = self.store.claim(self.project, self.agent, paths, intent, int(minutes))
        msg = f"claimed {len(paths)} path(s) for {int(minutes)} min."
        if conflicts:
            msg += "\n⚠ overlaps with other agents:\n" + render_claims(conflicts)
        return msg

    # ------------------------------------------------------------------ registry

    def tools(self) -> list[Tool]:
        s = {"type": "string"}
        arr = {"type": "array", "items": {"type": "string"}}
        return [
            Tool("project_brief", "Start here. Short project overview: latest handoff, pitfalls, decisions, "
                 "unread messages, other agents' claims. Also refreshes the file index (changed files only).",
                 {"project": {**s, "description": "Project path, if not the server's working directory."}},
                 [], self.brief),
            Tool("file_summary", "Cached summary + outline of a file, valid while its content hash is unchanged. "
                 "Call before reading a whole file.", {"path": s}, ["path"], self.file_summary),
            Tool("file_summary_save", "Save a 2-6 line summary of a file you read (purpose, key parts, gotchas). "
                 "Keyed to the content hash; shared with all agents.",
                 {"path": s, "summary": s}, ["path", "summary"], self.file_summary_save),
            Tool("where_is", "Find where a function/class/type is defined (name or part of it).",
                 {"symbol": s}, ["symbol"], self.where_is),
            Tool("search", "Search notes, decisions, pitfalls, handoffs and file summaries (keyword + semantic).",
                 {"query": s, "kinds": {**arr, "description": "Filter: file, note, decision, pitfall, handoff, todo"},
                  "limit": {"type": "integer"}}, ["query"], self.search),
            Tool("remember", "Save a note for future sessions and other agents. Link files so it is flagged "
                 "when they change. Use supersedes to replace an outdated note.",
                 {"kind": {"type": "string", "enum": list(NOTE_KINDS)}, "title": s, "body": s, "files": arr,
                  "supersedes": {"type": "integer"}}, ["kind", "title"], self.remember),
            Tool("note", "Read a note in full, or archive it.",
                 {"id": {"type": "integer"}, "action": {"type": "string", "enum": ["get", "archive"]}},
                 ["id"], self.note),
            Tool("handoff", "End-of-session handoff for the next session/agent. Releases your claims.",
                 {"done": s, "next": s, "decisions": s, "pitfalls": s, "files": arr}, ["done"], self.handoff),
            Tool("post", "Message other agents on this project. `to` is an agent like 'deepseek' or "
                 "'claude@repo-wt'; empty means everyone.", {"message": s, "to": s}, ["message"], self.post),
            Tool("inbox", "Unread messages from other agents (marks them read unless peek).",
                 {"peek": {"type": "boolean"}}, [], self.inbox),
            Tool("claim", "Tell other agents which files/dirs you are editing (expires). No paths: list active "
                 "claims. release=true: drop yours.",
                 {"paths": arr, "intent": s, "minutes": {"type": "integer"}, "release": {"type": "boolean"}},
                 [], self.claim),
        ]


def render_claims(rows) -> str:
    return "\n".join(f"- {c['agent']}: {c['path'] or '(whole project)'} — {c['intent'] or 'editing'} "
                     f"({max(0, int((c['expires_at'] - time.time()) // 60))}m left)" for c in rows)


def render_brief(store: Store, project: Project, agent: str) -> str:
    st = store.index(project)
    fs = store.file_stats(project)
    lines = [f"memdex · {project.name} · you are {agent}",
             f"index: {fs['files']} files, {fs['summarized']} with fresh summaries"
             + (f", {st['changed']} changed/new since last check" if st["changed"] else "")
             + (f", {st['removed']} removed" if st["removed"] else "")]
    dirs = store.top_dirs(project)
    if dirs:
        lines.append("layout: " + " ".join(f"{d}({n})" for d, n in dirs))
    unread = store.unread_count(project, agent)
    if unread:
        lines.append(f"inbox: {unread} unread message(s) → call inbox")
    claims = [c for c in store.active_claims(project) if c["agent"] != agent]
    if claims:
        lines.append("other agents are editing:\n" + render_claims(claims))
    ho = store.list_notes(project, "handoff", 1)
    if ho:
        h = ho[0]
        lines.append(f"last handoff #{h['id']} ({h['author']}, {ago(h['created_at'])}):\n{clip(h['body'], 900)}")
    for kind, label, n in (("pitfall", "pitfalls", 6), ("decision", "decisions", 6), ("todo", "todo", 5)):
        notes = store.list_notes(project, kind, n)
        if notes:
            lines.append(f"{label}: " + "; ".join(f"#{x['id']} {clip(x['title'], 70)}" for x in notes))
    if len(lines) <= 3:
        lines.append("No notes yet. Use remember/handoff to start this project's memory.")
    return "\n".join(lines)
