"""Command line: run the MCP server, index, search, hooks, export."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Optional

from . import __version__
from . import maintenance
from . import project as projects
from .store import Store, default_path
from .tools import Session, render_brief


def _session(args) -> Session:
    s = Session(Store(args.db), agent_name=getattr(args, "agent", "") or "")
    if getattr(args, "path", None):
        s.cwd = args.path
    return s


def cmd_serve(args) -> int:
    from .mcp import serve
    serve(args.db, args.path)
    return 0


def cmd_index(args) -> int:
    s = _session(args)
    t = time.time()
    st = s.store.index(s.project)
    print(f"{s.project.name}: {st['files']} files, {st['changed']} changed, {st['removed']} removed "
          f"({time.time() - t:.2f}s) → {s.store.path}")
    return 0


def cmd_brief(args) -> int:
    s = _session(args)
    print(render_brief(s.store, s.project, s.agent))
    return 0


def cmd_search(args) -> int:
    print(_session(args).search(" ".join(args.query), limit=args.limit))
    return 0


def cmd_file(args) -> int:
    s = _session(args)
    print(s.file_summary_save(args.file, args.summary) if args.summary else s.file_summary(args.file))
    return 0


def cmd_snippet(args) -> int:
    print(_session(args).read_snippet(args.file, args.query or "", args.line or 0, args.max_tokens))
    return 0


def cmd_remember(args) -> int:
    print(_session(args).remember(args.kind, args.title, args.body or "", args.files, args.supersedes))
    return 0


def cmd_handoff(args) -> int:
    print(_session(args).handoff(args.done, args.next or "", args.decisions or "", args.pitfalls or "", args.files))
    return 0


def cmd_post(args) -> int:
    print(_session(args).post(args.message, args.to or ""))
    return 0


def cmd_inbox(args) -> int:
    print(_session(args).inbox(args.peek))
    return 0


def cmd_claim(args) -> int:
    print(_session(args).claim(args.paths or None, args.intent or "", args.minutes, args.release))
    return 0


def cmd_hook(args) -> int:
    """Hook entry points. session-start prints the brief, which Claude Code adds to context."""
    payload = {}
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (json.JSONDecodeError, OSError):
            payload = {}
    args.path = args.path or payload.get("cwd") or os.getcwd()
    s = _session(args)
    if args.event == "session-start":
        print(render_brief(s.store, s.project, s.agent))
        print("(memdex MCP tools: file_summary before reading files, remember/handoff to save.)")
    return 0


def cmd_export(args) -> int:
    """Write the project's notes as Markdown files (one per note) for reading or backup."""
    s = _session(args)
    out = args.out or os.path.join(os.getcwd(), "memdex-export")
    os.makedirs(out, exist_ok=True)
    rows = s.store.db.execute(
        "SELECT * FROM notes WHERE project=? AND archived=0 ORDER BY id", (s.project.key,)).fetchall()
    for n in rows:
        slug = re.sub(r"[^\w가-힣ぁ-んァ-ン一-龥-]+", "-", n["title"]).strip("-")[:50] or "note"
        with open(os.path.join(out, f"{n['id']:04d}-{n['kind']}-{slug}.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nid: {n['id']}\nkind: {n['kind']}\nauthor: {n['author']}\n"
                    f"created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(n['created_at']))}\n"
                    f"superseded_by: {n['superseded_by'] or ''}\nrefs: {n['refs']}\n---\n\n"
                    f"# {n['title']}\n\n{n['body']}\n")
    print(f"exported {len(rows)} notes → {out}")
    return 0


def cmd_config(args) -> int:
    agent = args.agent or "claude"
    exe = "memdex"
    print(f"""# Claude Code (user scope, all projects)
claude mcp add memdex --scope user -e MEMDEX_AGENT={agent} -- {exe} serve

# Any MCP client (Claude Desktop, Cursor, Codex, DeepSeek harness, ...): mcpServers entry
{json.dumps({"mcpServers": {"memdex": {"command": exe, "args": ["serve"], "env": {"MEMDEX_AGENT": agent}}}}, indent=2)}

# Claude Code SessionStart hook (~/.claude/settings.json): injects the brief automatically
{json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"{exe} hook session-start --agent {agent}"}]}]}}, indent=2)}
""")
    return 0


def cmd_stats(args) -> int:
    st = Store(args.db)
    q = lambda sql: st.db.execute(sql).fetchone()[0]
    size = os.path.getsize(st.path) if os.path.exists(st.path) else 0
    print(f"db: {st.path} ({size / 1024:.0f} KB)\nfiles: {q('SELECT count(*) FROM files')}  "
          f"summaries: {q('SELECT count(*) FROM summaries')}  notes: {q('SELECT count(*) FROM notes')}  "
          f"messages: {q('SELECT count(*) FROM messages')}  trigram: {st.trigram}")
    return 0


def cmd_bench(args) -> int:
    """Measure, on a real checkout, how much context the index saves."""
    s = _session(args)
    started = time.time()
    st = s.store.index(s.project)
    elapsed = time.time() - started
    rep = maintenance.savings(s.store, s.project, top=args.top)
    if args.json:
        print(json.dumps({**rep, "index_seconds": round(elapsed, 3), "changed": st["changed"]},
                         indent=2, ensure_ascii=False))
        return 0
    print(f"{rep['project']}: {rep['files']:,} files · {rep['bytes'] / 1024:,.0f} KB indexed in "
          f"{elapsed:.2f}s ({st['changed']} changed)")
    if not rep["files"]:
        print("nothing indexed — run inside a project, or pass --path")
        return 0
    print(f"reading every file whole: ~{rep['full_tokens']:,} tokens")
    print(f"outline instead:          ~{rep['outline_tokens']:,} tokens  "
          f"({100 * (1 - rep['ratio']):.0f}% smaller; {rep['files_with_outline']:,} files have symbols)")
    print("biggest wins (file → outline):")
    for f in rep["top"]:
        pct = 100 * (1 - f["outline_chars"] / f["bytes"]) if f["bytes"] else 0.0
        print(f"  {f['path']:<50} {f['bytes'] / 1024:8,.1f} KB → {f['outline_chars'] / 1024:6,.1f} KB  "
              f"({pct:3.0f}% smaller)")
    return 0


def cmd_prune(args) -> int:
    """Drop index rows for project directories that no longer exist."""
    st = Store(args.db)
    rep = maintenance.prune(st, dry_run=args.dry_run, all_projects=args.all,
                            vacuum=not args.no_vacuum)
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0
    total_notes = rep["notes"] + rep["messages"] + rep["claims"] + rep["cursors"]
    if not rep["stale_roots"] and not total_notes:
        print("nothing to prune")
        return 0
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {rep['files']:,} files, {rep['symbols']:,} symbols and {rep['fts']:,} search rows "
          f"from {len(rep['stale_roots'])} missing root(s)")
    for root in rep["stale_roots"]:
        print(f"  {root}")
    if total_notes:
        print(f"{verb} {rep['notes']:,} notes, {rep['messages']:,} messages, "
              f"{rep['claims']:,} claims and {rep['cursors']:,} cursors")
    if not args.dry_run:
        print(f"db: {rep['bytes_before'] / 1024:,.0f} KB → {rep['bytes_after'] / 1024:,.0f} KB"
              + ("" if rep.get("vacuumed") else " (vacuum skipped)"))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="memdex", description="Shared, token-saving memory for AI coding agents.")
    p.add_argument("--version", action="version", version=f"memdex {__version__}")
    p.add_argument("--db", default=None, help=f"database path (default {default_path()}, or $MEMDEX_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument("--path", default=None, help="project path (default: current directory)")
        sp.add_argument("--agent", default="", help="agent name, e.g. claude, deepseek, codex")
        return sp

    add("serve", cmd_serve, "run the MCP server on stdio")
    add("index", cmd_index, "update the file index (changed files only)")
    add("brief", cmd_brief, "print the project brief")
    sp = add("search", cmd_search, "search notes and file summaries")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=8)
    sp = add("file", cmd_file, "show a file's cached summary and outline, or save a summary")
    sp.add_argument("file")
    sp.add_argument("--summary", default=None, help="save this 2-6 line summary for the file's current content")
    sp = add("snippet", cmd_snippet, "read a bounded part of a file around a query or line")
    sp.add_argument("file")
    sp.add_argument("--query", default=None)
    sp.add_argument("--line", type=int, default=0)
    sp.add_argument("--max-tokens", type=int, default=256)
    sp = add("remember", cmd_remember, "save a note, decision, pitfall or todo")
    sp.add_argument("kind", choices=["note", "decision", "pitfall", "todo"])
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.add_argument("--files", nargs="*", default=None, help="files this note is about (flagged when they change)")
    sp.add_argument("--supersedes", type=int, default=None, help="id of the note this one replaces")
    sp = add("handoff", cmd_handoff, "end-of-session handoff for the next session or agent")
    sp.add_argument("done")
    sp.add_argument("--next", default="")
    sp.add_argument("--decisions", default="")
    sp.add_argument("--pitfalls", default="")
    sp.add_argument("--files", nargs="*", default=None)
    sp = add("post", cmd_post, "message other agents on this project")
    sp.add_argument("message")
    sp.add_argument("--to", default="", help="agent name such as claude or deepseek; default everyone")
    sp = add("inbox", cmd_inbox, "unread messages from other agents")
    sp.add_argument("--peek", action="store_true", help="do not mark as read")
    sp = add("claim", cmd_claim, "announce files you are editing; no paths lists active claims")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--intent", default="")
    sp.add_argument("--minutes", type=int, default=120)
    sp.add_argument("--release", action="store_true")
    sp = add("hook", cmd_hook, "hook entry point for agent harnesses")
    sp.add_argument("event", choices=["session-start"])
    sp = add("export", cmd_export, "export notes as Markdown")
    sp.add_argument("--out", default=None)
    add("config", cmd_config, "print setup snippets for MCP clients and hooks")
    add("stats", cmd_stats, "database statistics")
    sp = add("bench", cmd_bench, "measure the tokens outlines save on this project")
    sp.add_argument("--top", type=int, default=10, help="how many biggest wins to list")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp = add("prune", cmd_prune, "drop index rows for project directories that are gone")
    sp.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    sp.add_argument("--all", action="store_true",
                    help="also drop notes/messages/claims/cursors of missing projects (destructive)")
    sp.add_argument("--no-vacuum", action="store_true", help="skip VACUUM after pruning")
    sp.add_argument("--json", action="store_true", help="machine-readable output")

    args = p.parse_args(argv)
    return args.fn(args)
