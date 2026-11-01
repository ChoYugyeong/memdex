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
    sp = add("hook", cmd_hook, "hook entry point for agent harnesses")
    sp.add_argument("event", choices=["session-start"])
    sp = add("export", cmd_export, "export notes as Markdown")
    sp.add_argument("--out", default=None)
    add("config", cmd_config, "print setup snippets for MCP clients and hooks")
    add("stats", cmd_stats, "database statistics")

    args = p.parse_args(argv)
    return args.fn(args)
