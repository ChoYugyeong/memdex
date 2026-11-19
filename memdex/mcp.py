"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line).

Hand-written instead of using the MCP SDK so memdex installs with no
dependencies and does not break across SDK major versions. Only the parts a
tools-only server needs are implemented: initialize, ping, tools/list,
tools/call.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import traceback
from typing import Any, Optional

from . import __version__
from . import project as projects
from .store import Store
from .tools import Session, instructions, is_dsh_agent, tool_profile

LATEST_PROTOCOL = "2025-06-18"


def _reply(id_: Any, result: Any = None, error: Optional[dict] = None) -> None:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.buffer.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


# Parameters that hold paths: a model that sends "a.py;b.py" or "a.py, b.py" instead of an array means a list.
PATH_PARAMS = {"files", "paths"}
_PATH_SPLIT = re.compile(r"[\n;,]+")


def _coerce(params: dict, args: dict) -> dict:
    """Bend a bare string into the array the schema declares, before Python iterates it letter by letter.

    Some clients serialise a one-item or delimited list as a plain string. Passed through, `for f in files`
    walks its characters: a handoff gets one file ref per letter and a claim locks "d", "o", "c"...
    """
    out = dict(args)
    for key, value in args.items():
        spec = params.get(key) or {}
        if spec.get("type") != "array" or not isinstance(value, str):
            continue
        if key in PATH_PARAMS:
            out[key] = [p.strip() for p in _PATH_SPLIT.split(value) if p.strip()]
        else:
            out[key] = [value] if value.strip() else []
    return out


def _call(session: Session, tools: dict, name: str, args: dict) -> dict:
    tool = tools.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    accepted = inspect.signature(tool.fn).parameters
    args = _coerce(tool.params, args if isinstance(args, dict) else {})
    expected = ", ".join(tool.params) or "none"
    unknown = sorted(k for k in args if k not in accepted)
    missing = [k for k in tool.required if args.get(k) is None]
    if missing:
        # A model that guessed a parameter name ("name" for where_is's "symbol") needs the right name
        # back, not a Python TypeError about positional arguments.
        hint = f"; got {', '.join(unknown)}" if unknown else ""
        return {"content": [{"type": "text", "text": f"{name}: missing required argument(s): {', '.join(missing)}"
                             f"{hint}. Arguments: {expected}."}], "isError": True}
    try:
        text = tool.fn(**{k: v for k, v in args.items() if k in accepted})
        if unknown:
            text += f"\n(ignored unknown argument(s): {', '.join(unknown)}; {name} takes: {expected})"
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:  # report to the agent instead of killing the server
        traceback.print_exc(file=sys.stderr)
        return {"content": [{"type": "text", "text": f"memdex error: {type(e).__name__}: {e}"}], "isError": True}


def handle(session: Session, tools: dict, msg: dict) -> Optional[dict]:
    """Process one JSON-RPC message; return the response body or None for notifications."""
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if id_ is None:
        return None  # notification (initialized, cancelled, ...)
    if method == "initialize":
        client = (params.get("clientInfo") or {}).get("name", "")
        if client and not session.agent_name:
            session.agent_name = "deepseek" if client == "dsh-mcp-client" else client.split()[0]
        return {"result": {
            "protocolVersion": params.get("protocolVersion") or LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False},
                             "artifacts": {"listChanged": False}},
            "serverInfo": {"name": "memdex", "version": __version__},
            "instructions": instructions(session.listed),
        }}
    if method == "ping":
        return {"result": {}}
    if method == "artifacts/list":
        rows = session.store.db.execute(
            "SELECT id, kind, title, body, author, created_at, pinned FROM notes "
            "WHERE project=? AND archived=0 ORDER BY pinned DESC, created_at DESC LIMIT 20",
            (session.project.key,)).fetchall()
        artifacts = [{
            "name": f"{r['kind']}-{r['id']}",
            "uri": f"memdex://note/{r['id']}",
            "mimeType": "text/markdown",
            "description": (r['title'] or "")[:200] or f"{r['kind']} #{r['id']}",
            "lastModified": r['created_at'],
        } for r in rows]
        return {"result": {"artifacts": artifacts}}
    if method == "artifacts/read":
        match = re.fullmatch(r"memdex://note/(\d+)", str(params.get("uri") or ""))
        if match is None:
            return {"error": {"code": -32602, "message": "artifact uri must be memdex://note/<id>"}}
        row = session.store.db.execute(
            "SELECT id, kind, title, body, author, created_at FROM notes WHERE project=? AND id=?",
            (session.project.key, int(match.group(1)))).fetchone()
        if row is None:
            return {"error": {"code": -32602, "message": f"artifact not found: {params.get('uri')}"}}
        return {"result": {
            "artifact": {
                "name": f"{row['kind']}-{row['id']}",
                "uri": f"memdex://note/{row['id']}",
                "mimeType": "text/markdown",
                "description": (row['title'] or "")[:200],
                "lastModified": row['created_at'],
            },
            "content": {"type": "text", "text": row['body'] or "", "mimeType": "text/markdown"},
        }}
    if method == "tools/list":
        # Unlisted tools stay callable (an older prompt or a script may name them); they just stop
        # costing every request their schema.
        return {"result": {"tools": [t.schema(session.full_schemas) for t in tools.values()
                                     if session.lists(t.name)]}}
    if method == "tools/call":
        # One stdio connection can serve several chats. Identity comes from the current host
        # envelope, never from the model's arguments or from a previous tools/call.
        meta = params.get("_meta", {})
        keys = ["memdex.dev/conversation-id"]
        agent = (os.environ.get("MEMDEX_AGENT") or session.agent_name).lower().split("@")[0].split("/")[0]
        dsh = is_dsh_agent(agent) or is_dsh_agent(session.agent_name.lower())
        codex = agent in ("codex", "gpt") or "codex" in session.agent_name.lower()
        claude = agent in ("claude", "claude-code") or "claude" in session.agent_name.lower()
        if dsh:
            keys.append("session_id")
        elif codex:
            keys.extend(("threadId", "thread_id", "codex/threadId"))
        elif claude:
            keys.extend(("session_id", "claude/sessionId"))
        ids = [meta[k] for k in keys if isinstance(meta, dict) and k in meta]
        if not isinstance(meta, dict) or any(not isinstance(i, str) or not i.strip()
                or len(i) > 512 or not i.isprintable() or i != ids[0] for i in ids):
            return {"error": {"code": -32602, "message": "invalid or conflicting conversation metadata"}}
        chat = ids[0] if ids else None
        if not chat and claude:
            # Claude hosts without per-call IDs can expose their process-bound native identity through
            # Reason. Resolve on this call only; never reuse another chat's result on a shared server.
            from .reason_notify import calling_chat
            chat = calling_chat(session.store, "claude")
        label = meta.get("memdex.dev/task-label")
        if label is not None and (not chat or not isinstance(label, str)
                                  or len(label) > 512 or not label.isprintable()):
            return {"error": {"code": -32602, "message": "invalid task-label metadata or missing chat id"}}
        launch_task = meta.get("memdex.dev/task-id")
        launch_path = meta.get("memdex.dev/project")
        bootstrap = "memdex.dev/task-id" in meta or "memdex.dev/project" in meta
        managed_chat = claude and chat and chat == os.environ.get("MEMDEX_LAUNCH_CHAT_ID")
        if managed_chat and not bootstrap:
            launch_task = os.environ.get("MEMDEX_LAUNCH_TASK_ID", "")
            launch_task = int(launch_task) if re.fullmatch(r"[1-9][0-9]{0,18}", launch_task) else None
            launch_path = os.environ.get("MEMDEX_PROJECT")
            bootstrap = True
        if bootstrap and (not (ids or managed_chat) or type(launch_task) is not int
                          or not 0 < launch_task < 2**63
                          or not isinstance(launch_path, str) or not launch_path.isprintable()
                          or not os.path.isabs(launch_path) or not os.path.isdir(launch_path)):
            return {"error": {"code": -32602, "message": "launch binding requires a host chat ID, "
                              "an absolute project directory and a positive task ID"}}
        saved = session.conversation_id, session.require_chat_id, session._project, session.conversation_label
        try:
            session.conversation_id, session.require_chat_id = chat, dsh or codex
            session.conversation_label = label
            if chat:
                session._project = session.chat_projects.get(chat)
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if bootstrap:
                try:
                    if not isinstance(args, dict):
                        raise ValueError("invalid tool arguments")
                    launch_project = projects.resolve(launch_path)
                    requested_project = args.get("project") if name == "project_brief" else None
                    if (session._project is not None and session._project != launch_project
                            or requested_project and projects.resolve(requested_project) != launch_project
                            or name == "session_routes" and args.get("task_id") not in (None, launch_task)):
                        raise ValueError("launch metadata conflicts with the project or task in this chat")
                    session._project = launch_project
                    session.store.register_route(launch_project, session.route, session.agent, label,
                                                 conversation_id=chat, task_id=launch_task, bootstrap=True)
                    session.chat_projects[chat] = launch_project
                except (TypeError, ValueError, OSError) as exc:
                    return {"error": {"code": -32602, "message": str(exc)}}
            result = _call(session, tools, name, args)
            if chat and name == "project_brief" and not result["isError"] and args.get("project"):
                session.chat_projects[chat] = session.project
            return {"result": result}
        finally:
            session.conversation_id, session.require_chat_id = saved[:2]
            session.conversation_label = saved[3]
            if chat:
                session._project = saved[2]
    return {"error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(db_path: Optional[str] = None, cwd: Optional[str] = None) -> None:
    session = Session(Store(db_path))
    if cwd:
        session.cwd = cwd
    session.listed, session.full_schemas = tool_profile()
    tools = {t.name: t for t in session.tools()}
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            out = handle(session, tools, m)
            if out is not None:
                try:
                    _reply(m.get("id"), out.get("result"), out.get("error"))
                except (BrokenPipeError, OSError):
                    return  # the client closed its end; stop instead of holding the database open
