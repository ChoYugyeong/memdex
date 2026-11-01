"""Command line: run the MCP server, index, search, hooks, export."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from typing import Optional

from . import __version__
from . import doctor as doctor_mod
from . import maintenance
from . import project as projects
from . import skills as skills_mod
from .store import COLD, Store, activation, default_path
from .tools import BRIEF_CHARS, Session, render_brief


def _session(args) -> Session:
    s = Session(Store(args.db), agent_name=getattr(args, "agent", "") or "")
    if getattr(args, "path", None):
        s.cwd = args.path
    # Attribute reads/confirms to this agent without threading it through every Session call.
    s.store.default_agent = s.agent
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
    print(render_brief(s.store, s.project, s.agent, max_chars=args.max_chars))
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


def cmd_where_is(args) -> int:
    print(_session(args).where_is(args.symbol))
    return 0


def cmd_note(args) -> int:
    action = "get"
    for flag, label in (("archive", "archive"), ("confirm", "confirm"), ("pin", "pin"), ("unpin", "unpin")):
        if getattr(args, flag, False):
            action = label
            break
    out = _session(args).note(args.id, action)
    print(out)
    return 1 if "not found in this project" in out else 0


def cmd_remember(args) -> int:
    print(_session(args).remember(args.kind, args.title, args.body or "", args.files, args.supersedes,
                                  args.importance, args.pin))
    return 0


def _skills_json(s, action: str, who: str, args) -> int:
    """The same data as JSON, for scripts: one object or list on stdout, nothing else."""
    from . import skills as skills_mod
    skills_mod.ensure(s.store.db)
    if action == "scan":
        payload = skills_mod.scan(s.store, who, s.project.root, home=s.home_dir)
    elif action == "conflicts":
        payload = skills_mod.conflicts(s.store)
    elif action == "show":
        if not args.query:
            print("skills show needs a name, e.g. `memdex skills show deploy`")
            return 2
        got = skills_mod.show(s.store, name=" ".join(args.query))
        payload = None if not got else {**dict(got["row"]), "body": got["body"],
                                        "truncated": got["truncated"]}
    elif action == "find":
        skills_mod.maybe_refresh(s.store, who, s.project.root, home=s.home_dir, max_age=0)
        payload = skills_mod.find(s.store, " ".join(args.query), max(1, min(args.limit, 20)))
    else:
        skills_mod.maybe_refresh(s.store, who, s.project.root, home=s.home_dir, max_age=0)
        payload = skills_mod.rows(s.store)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_skills(args) -> int:
    """The skill registry: list, scan SKILL.md files, show one, search, conflicts, or share one by hand."""
    s = _session(args)
    action = args.action or "list"
    who = s.agent.split("@", 1)[0]
    if getattr(args, "json", False):
        return _skills_json(s, action, who, args)
    if action == "scan":
        print(s.skills("scan"))
        return 0
    if action == "conflicts":
        print(s.skills("conflicts"))
        return 0
    if action == "show":
        if not args.query:
            print("skills show needs a name, e.g. `memdex skills show deploy`")
            return 2
        print(s.skills("show", name=" ".join(args.query)))
        return 0
    if action == "share":
        if not args.query:
            print("skills share needs a name, e.g. `memdex skills share deploy -d \"how to deploy\"`")
            return 2
        print(s.skills("share", name=args.query[0], description=args.description or " ".join(args.query[1:]),
                       invoke=args.invoke or ""))
        return 0
    if action == "find":
        print(s.skills("find", query=" ".join(args.query), limit=args.limit))
        return 0
    # Listing a stale registry is the same mistake as searching one: catch up on changed files first.
    skills_mod.maybe_refresh(s.store, who, s.project.root, home=s.home_dir, max_age=0)
    rows = skills_mod.rows(s.store)
    if not rows:
        print("no skills registered yet; run `memdex skills scan` to read this machine's SKILL.md files")
        return 0
    for r in rows:
        tail = f" · invoke: {r['invoke']}" if r["invoke"] else ""
        origin = f" · {r['origin']}" if r["origin"] else ""
        description = (r["description"] or "").replace("\n", " ")
        if len(description) > 140:
            description = description[:137] + "…"
        print(f"- {r['name']} ({r['agent']}{origin}{tail}) — {description}")
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
    out = _session(args).claim(args.paths or None, args.intent or "", args.minutes, args.release,
                               args.exclusive, args.force, args.refresh)
    print(out)
    return 1 if out.lstrip().startswith("✗") else 0


def _git(project, *args) -> str:
    from .project import git
    return git(list(args), project.root) or ""


def hook_pre_commit(session, args) -> int:
    """Refuse a commit that touches a file another agent holds an exclusive claim on."""
    from .store import _overlaps
    staged = [p for p in _git(session.project, "diff", "--cached", "--name-only").splitlines() if p.strip()]
    if not staged:
        return 0
    claims = [c for c in session.store.active_claims(session.project) if c["agent"] != session.agent]
    locks, advisory = [], []
    for c in claims:
        for path in staged:
            if _overlaps(c["path"], path):
                entry = (path, c)
                (locks if c["exclusive"] else advisory).append(entry)
                break
    for path, c in advisory:
        print(f"memdex: note — {path} is claimed (advisory) by {c['agent']} "
              f"({c['intent'] or 'editing'}, {max(0, int((c['expires_at'] - time.time()) // 60))}m left)")
    if locks:
        print("memdex: commit blocked — these staged files are locked by another agent:")
        for path, c in locks:
            print(f"  {path}  ←  {c['agent']} ({c['intent'] or 'editing'}, "
                  f"{max(0, int((c['expires_at'] - time.time()) // 60))}m left)")
        print("wait for the lock to expire, drop it with `memdex claim --release --agent <them>`, "
              "or commit with MEMDEX_ALLOW_LOCK=1 if you know it is stale.")
        return 1
    return 0


def hook_install(session, args) -> int:
    """Install memdex as this checkout's git pre-commit hook."""
    if not session.project.is_git:
        print("not a git checkout; nothing to install")
        return 1
    hooks_dir = _git(session.project, "rev-parse", "--git-path", "hooks")
    hooks_dir = os.path.join(session.project.root, hooks_dir) if not os.path.isabs(hooks_dir) else hooks_dir
    hooks_dir = os.path.normpath(hooks_dir)
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-commit")
    marker = "hook pre-commit"
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if not (marker in existing and "memdex" in existing.lower()) and not args.force:
            print(f"{target} already exists and was not written by memdex; re-run with --force to replace it")
            return 1
    exe = shutil.which("memdex")
    if exe:
        # git runs hooks with sh, where a Windows backslash path is mangled — use forward slashes.
        runner = '"{0}"'.format(exe.replace(os.sep, "/"))
    else:
        runner = '"{0}" -m memdex'.format(sys.executable.replace(os.sep, "/"))
    agent = session.agent or "agent"
    script = ("#!/bin/sh\n"
              "# installed by `memdex hook install`: refuse commits that touch a file another agent locked.\n"
              f"exec {runner} hook pre-commit --agent {agent} \"$@\"\n")
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    try:
        os.chmod(target, 0o755)
    except OSError:
        pass
    print(f"installed {target}")
    return 0


def _hook_payload() -> dict:
    """The JSON harnesses pipe to a session-start or pre-edit hook; never read stdin from a git hook.

    A pre-commit hook inherits git's stdin, which can be an open pipe that stays open for the whole
    commit — reading it there blocks the commit (and `unittest discover`) forever.
    """
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "str_replace_editor", "apply_patch", "patch"}


def hook_pre_edit(session, args, payload: dict) -> int:
    """Claude Code PreToolUse: refuse an edit to a file another agent holds an exclusive claim on.

    Exit 2 is what Claude Code treats as "blocked"; the stderr text is shown to the model, so it can post to
    the lock owner or pick another file. An advisory claim only prints a note (exit 0).
    """
    from .store import _overlaps, to_rel
    tool = str(payload.get("tool_name") or "").strip()
    if tool and tool.lower() not in EDIT_TOOLS:
        return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return 0
    rel = to_rel(session.project, raw_path)
    if not rel:
        return 0  # outside this checkout: memdex has no opinion
    if os.environ.get("MEMDEX_ALLOW_LOCK"):
        print("memdex: MEMDEX_ALLOW_LOCK is set, locks ignored for this edit", file=sys.stderr)
        return 0
    others = [c for c in session.store.active_claims(session.project) if c["agent"] != session.agent]
    locked = [c for c in others if c["exclusive"] and _overlaps(c["path"], rel)]
    advisory = [c for c in others if not c["exclusive"] and _overlaps(c["path"], rel)]
    if locked:
        owners = ", ".join(f"{c['agent']} ({c['intent'] or 'editing'}, "
                           f"{max(0, int((c['expires_at'] - time.time()) // 60))}m left)" for c in locked)
        print(f"blocked: locked by {owners} — post to them or claim --force", file=sys.stderr)
        return 2
    for c in advisory:
        print(f"memdex: note — {rel} is claimed (advisory) by {c['agent']} "
              f"({c['intent'] or 'editing'}, {max(0, int((c['expires_at'] - time.time()) // 60))}m left)")
    # Compare-and-swap on our own exclusive lock: the lock says "I am the one editing this", so an edit
    # computed from a stale read would overwrite whatever moved underneath it. Refuse, and say how to
    # prove the re-read (`claim --refresh`), rather than write blind.
    for c in session.store.active_claims(session.project):
        if c["agent"] != session.agent or not c["exclusive"] or not c["sha"]:
            continue
        if not _overlaps(c["path"], rel):
            continue
        current = session.store.claim_sha(session.project, rel)
        if current and current != c["sha"]:
            print(f"blocked: {rel} changed since you claimed it (token {c['token']}, "
                  f"sha {c['sha'][:8]} → {current[:8]}). Re-read the file, then re-stamp the lock:\n"
                  f"  memdex claim {rel} --exclusive --refresh",
                  file=sys.stderr)
            return 2
    return 0


def _claude_settings_path(session, project: bool) -> str:
    if project:
        return os.path.join(session.project.root, ".claude", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def hook_install_claude(session, args) -> int:
    """Merge a PreToolUse hook for Edit/Write/MultiEdit into Claude Code's settings.json."""
    target = _claude_settings_path(session, bool(getattr(args, "project", False)))
    exe = shutil.which("memdex")
    runner = '"{0}"'.format(exe.replace(os.sep, "/")) if exe else \
        '"{0}" -m memdex'.format(sys.executable.replace(os.sep, "/"))
    agent = (session.agent or "agent").split("@", 1)[0]
    command = f"{runner} hook pre-edit --agent {agent}"
    settings = {}
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                settings = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"{target} is not readable JSON ({e}); not touching it")
            return 1
        if not isinstance(settings, dict):
            print(f"{target} does not contain a JSON object; not touching it")
            return 1
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        print(f"{target}: hooks.PreToolUse is not a list; not touching it")
        return 1
    for entry in pre:
        for hook in (entry or {}).get("hooks") or []:
            if "hook pre-edit" in str(hook.get("command", "")):
                print(f"already installed in {target}")
                return 0
    if os.path.exists(target):
        shutil.copyfile(target, target + ".bak")
    pre.append({"matcher": "Edit|Write|MultiEdit",
                "hooks": [{"type": "command", "command": command}]})
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    backup = f" (backup: {target}.bak)" if os.path.exists(target + ".bak") else ""
    print(f"installed PreToolUse Edit|Write|MultiEdit hook in {target}{backup}")
    return 0


def cmd_hook(args) -> int:
    """Hook entry points. session-start prints the brief, which Claude Code adds to context."""
    payload = _hook_payload() if args.event in ("session-start", "pre-edit") else {}
    args.path = args.path or payload.get("cwd") or os.getcwd()
    if args.event == "pre-edit" and not (os.environ.get("MEMDEX_AGENT") or args.agent):
        # Claude Code passes the agent when installing the hook; a hand-written one may not.
        cfg = _git(projects.resolve(args.path), "config", "--get", "memdex.agent").strip()
        if cfg:
            args.agent = cfg
    s = _session(args)
    if args.event == "session-start":
        print(render_brief(s.store, s.project, s.agent, max_chars=args.max_chars))
        print("(memdex MCP tools: file_summary before reading files, remember/handoff to save.)")
        return 0
    if args.event == "pre-edit":
        return hook_pre_edit(s, args, payload)
    if args.event == "pre-commit":
        if os.environ.get("MEMDEX_ALLOW_LOCK"):
            print("memdex: MEMDEX_ALLOW_LOCK is set, locks ignored for this commit")
            return 0
        return hook_pre_commit(s, args)
    if args.event == "install":
        return hook_install_claude(s, args) if getattr(args, "claude", False) else hook_install(s, args)
    print(f"unknown hook event: {args.event}")
    return 2


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


def cmd_connect(args) -> int:
    """Write the MCP entry into every detected agent's config, instead of printing a snippet."""
    from . import connect as connect_mod
    root = projects.resolve(args.path or os.getcwd()).root
    scope = "project" if args.project else "user"
    if args.list:
        targets = connect_mod.project_clients(root) if args.project else None
        return connect_mod.list_clients(agent=args.agent or "", targets=targets)
    return connect_mod.run(agent=args.agent or "", only=args.client, dry_run=args.dry_run,
                           remove=args.remove, scope=scope, root=root, check=args.serve_check,
                           claude=None if args.no_claude else connect_mod.find_claude())


def cmd_config(args) -> int:
    agent = args.agent or "claude"
    exe = "memdex"
    print(f"""# Claude Code (user scope, all projects)
claude mcp add memdex --scope user -e MEMDEX_AGENT={agent} -- {exe} serve

# Any MCP client (Claude Desktop, Cursor, Codex, DeepSeek harness, ...): mcpServers entry
{json.dumps({"mcpServers": {"memdex": {"command": exe, "args": ["serve"], "env": {"MEMDEX_AGENT": agent}}}}, indent=2)}

# Claude Code SessionStart hook (~/.claude/settings.json): injects the brief automatically
{json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"{exe} hook session-start --agent {agent}"}]}]}}, indent=2)}

# Claude Code PreToolUse hook: refuse an edit to a file another agent has locked (exit 2 = blocked)
#   memdex hook install --claude          writes this into ~/.claude/settings.json
#   memdex hook install --claude --project writes it into ./.claude/settings.json
{json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": f"{exe} hook pre-edit --agent {agent}"}]}]}}, indent=2)}
""")
    return 0


def cmd_stats(args) -> int:
    st = Store(args.db)
    q = lambda sql: st.db.execute(sql).fetchone()[0]
    size = os.path.getsize(st.path) if os.path.exists(st.path) else 0
    print(f"db: {st.path} ({size / 1024:.0f} KB)\nfiles: {q('SELECT count(*) FROM files')}  "
          f"summaries: {q('SELECT count(*) FROM summaries')}  notes: {q('SELECT count(*) FROM notes')}  "
          f"messages: {q('SELECT count(*) FROM messages')}  trigram: {st.trigram}")
    # Usage signal: a memory read by a later session is worth more than one written and never touched.
    served = q("SELECT COALESCE(sum(use_count), 0) FROM summaries")
    served_files = q("SELECT count(*) FROM summaries WHERE use_count > 0")
    notes_total = q("SELECT count(*) FROM notes WHERE archived=0")
    read_by_other = q("SELECT count(*) FROM notes WHERE archived=0 AND last_used_by IS NOT NULL "
                      "AND last_used_by <> COALESCE(author, '')")
    confirmed = q("SELECT COALESCE(sum(confirm_count), 0) FROM notes")
    pinned = q("SELECT count(*) FROM notes WHERE archived=0 AND pinned=1")
    notes = st.db.execute("SELECT * FROM notes WHERE archived=0").fetchall()
    cold = sum(1 for n in notes if activation(n) < COLD)
    n_skills, skill_agents = skills_mod.count(st)
    print(f"memory use: summaries served fresh {served}x ({served_files} files)  "
          f"notes read by another agent {read_by_other}/{notes_total}  "
          f"re-confirmed after a change {confirmed}x  pinned {pinned}")
    print(f"ranking: {cold} note(s) below the cold line — still stored and searchable, out of the brief budget")
    print(f"skills: {n_skills} from {skill_agents} agent(s)")
    return 0


def cmd_doctor(args) -> int:
    """Print the facts a maintainer would ask for in a bug report."""
    report = doctor_mod.run(path=args.path, db_path=args.db, offline=args.offline,
                            serve_check=args.serve_check)
    print(report.render())
    return 1 if report.failed else 0


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
    print(f"{rep['project']}: {rep['files']:,} files · {maintenance.human_size(rep['bytes'])} indexed in "
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
        print(f"  {f['path']:<50} {maintenance.human_size(f['bytes']):>9} → "
              f"{maintenance.human_size(f['outline_chars']):>9}  ({pct:3.0f}% smaller)")
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
        # Also accept --db after the subcommand; SUPPRESS keeps the global value when it is not given.
        sp.add_argument("--db", default=argparse.SUPPRESS, help="database path (same as the global --db)")
        return sp

    add("serve", cmd_serve, "run the MCP server on stdio")
    add("index", cmd_index, "update the file index (changed files only)")
    sp = add("brief", cmd_brief, "print the project brief (last handoff, notes, inbox, claims)")
    sp.add_argument("--max-chars", type=int, default=BRIEF_CHARS,
                    help="trim the brief to this many characters (0 = no limit)")
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
    sp = add("where_is", cmd_where_is, "find where a function/class/type is defined")
    sp.add_argument("symbol")
    sp = add("note", cmd_note, "read a note, or archive/confirm/pin it")
    sp.add_argument("id", type=int)
    action = sp.add_mutually_exclusive_group()
    action.add_argument("--archive", action="store_true", help="archive this note instead of printing it")
    action.add_argument("--confirm", action="store_true",
                        help="it still holds: re-stamp its refs to the current code and clear the warning")
    action.add_argument("--pin", action="store_true", help="show it at the top of every brief")
    action.add_argument("--unpin", action="store_true", help="stop showing it at the top of every brief")
    sp = add("remember", cmd_remember, "save a note, decision, pitfall or todo")
    sp.add_argument("kind", choices=["note", "decision", "pitfall", "todo"])
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.add_argument("--files", nargs="*", default=None, help="files this note is about (flagged when they change)")
    sp.add_argument("--supersedes", type=int, default=None, help="id of the note this one replaces")
    sp.add_argument("--importance", type=int, default=0, choices=[0, 1, 2, 3, 4, 5],
                    help="1-5; above 1 raises the note's ranking in the brief")
    sp.add_argument("--pin", action="store_true", help="keep it at the top of every brief")
    sp = add("skills", cmd_skills, "skills shared by every agent on this machine")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "find", "show", "scan", "conflicts", "share"])
    sp.add_argument("--json", action="store_true", help="machine-readable output for list, find, show, scan, conflicts")
    sp.add_argument("query", nargs="*")
    sp.add_argument("--description", "-d", default="", help="share: what the skill does, when to use it")
    sp.add_argument("--invoke", default="", help="share: how another agent runs it")
    sp.add_argument("--limit", type=int, default=8, help="find: how many matches")
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
    sp.add_argument("--exclusive", action="store_true",
                    help="lock: refused if another agent is on the path, and refuses their claims")
    sp.add_argument("--force", action="store_true", help="record the claim even if a lock overlaps it")
    sp.add_argument("--refresh", action="store_true",
                    help="re-stamp a claim you already hold (new fencing token, current file sha)")
    sp = add("hook", cmd_hook, "hook entry points (session-start, pre-edit, pre-commit, install)")
    sp.add_argument("event", choices=["session-start", "pre-edit", "pre-commit", "install"])
    sp.add_argument("--max-chars", type=int, default=BRIEF_CHARS,
                    help="trim the injected brief to this many characters (0 = no limit)")
    sp.add_argument("--force", action="store_true", help="install: replace an existing non-memdex hook")
    sp.add_argument("--claude", action="store_true",
                    help="install: write the PreToolUse edit hook into Claude Code's settings.json instead of git")
    sp.add_argument("--project", action="store_true", help="install --claude: write .claude/settings.json here")
    sp = add("export", cmd_export, "export notes as Markdown")
    sp.add_argument("--out", default=None)
    sp = add("connect", cmd_connect, "write the MCP entry into the agents installed on this machine")
    sp.add_argument("--client", default="", help="claude-desktop, cursor, windsurf, gemini, codex (comma separated)")
    sp.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    sp.add_argument("--remove", action="store_true", help="drop the memdex entry again")
    sp.add_argument("--list", action="store_true", help="only report which clients are installed and configured")
    sp.add_argument("--project", action="store_true",
                    help="write the configs that belong in the repo (.vscode/mcp.json, .mcp.json) to commit")
    sp.add_argument("--no-claude", action="store_true",
                    help="do not run `claude mcp add` even when the Claude Code CLI is on PATH")
    sp.add_argument("--serve-check", action="store_true",
                    help="after writing, start the server and verify the MCP handshake (initialize, tools/list)")
    add("config", cmd_config, "print setup snippets for MCP clients and hooks")
    add("stats", cmd_stats, "database statistics")
    sp = add("doctor", cmd_doctor, "check the database, project index and optional integrations")
    sp.add_argument("--offline", action="store_true", help="do not probe Ollama")
    sp.add_argument("--serve-check", action="store_true",
                    help="start the server and verify the MCP handshake (initialize, tools/list)")
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
