"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line).

Hand-written instead of using the MCP SDK so memdex installs with no
dependencies and does not break across SDK major versions. The price is
following the protocol by hand. Only the parts a tools-only server needs are
implemented, for both eras of the protocol:

- 2026-07-28 (stateless): server/discover, tools/list, tools/call. Every request
  carries its protocol version and client capabilities in `_meta`; there is no
  handshake and no ping.
- 2025-11-25 and earlier (handshake): initialize, ping, tools/list, tools/call.

One process serves both. How a request opens selects the behaviour: per-request
`_meta` is answered statelessly, `initialize` keeps the older semantics.
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

# Revisions that carry version, identity and capabilities in each request's `_meta`.
MODERN_VERSIONS = ("2026-07-28",)
# Revisions negotiated once with `initialize`, newest first.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
LATEST_PROTOCOL = MODERN_VERSIONS[0]

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"

INVALID_PARAMS = -32602
UNSUPPORTED_VERSION = -32022  # -32020..-32099 belongs to the MCP specification

CAPABILITIES = {"tools": {"listChanged": False}, "artifacts": {"listChanged": False}}
# The listed tools and the instructions are fixed for the life of the process (MEMDEX_TOOLS is read once
# at start), so a client may reuse them. The hint stays short for a client that keeps its cache across a
# restart: after an upgrade it sees the new list within minutes.
CACHE_TTL_MS = 300_000


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


def _agent_from_client(info: Any) -> str:
    """Agent label from the client's self-reported name: a fallback for when MEMDEX_AGENT is not set."""
    name = info.get("name") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name.strip():
        return ""
    return "deepseek" if name == "dsh-mcp-client" else name.split()[0]


def _invalid(message: str) -> dict:
    return {"error": {"code": INVALID_PARAMS, "message": message}}


def handle(session: Session, tools: dict, msg: dict) -> Optional[dict]:
    """Process one JSON-RPC message; return the response body or None for notifications."""
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if id_ is None:
        return None  # notification (initialized, cancelled, ...)
    meta = params.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    if method != "server/discover" and META_VERSION not in meta and META_CAPABILITIES not in meta:
        return _dispatch(session, tools, method, params, modern=False)  # a handshake-era client
    # From here the request declares its own protocol version. Nothing is remembered from earlier
    # requests: the version is checked, and the client named, on every one.
    version = meta.get(META_VERSION)
    if not isinstance(version, str):
        return _invalid(f"missing _meta[{META_VERSION!r}]")
    if version not in SUPPORTED_VERSIONS:
        return {"error": {"code": UNSUPPORTED_VERSION, "message": "Unsupported protocol version",
                          "data": {"supported": list(SUPPORTED_VERSIONS), "requested": version}}}
    if version in LEGACY_VERSIONS and method != "server/discover":
        # Every version in `supported` is honoured: a handshake revision named here is answered in
        # that revision's shape.
        return _dispatch(session, tools, method, params, modern=False)
    if not isinstance(meta.get(META_CAPABILITIES), dict):
        return _invalid(f"missing _meta[{META_CAPABILITIES!r}]")
    saved = session.agent_name
    session.agent_name = saved or _agent_from_client(meta.get(META_CLIENT))
    try:
        out = _dispatch(session, tools, method, params, modern=True)
    finally:
        session.agent_name = saved
    if "result" in out:
        result = out["result"]
        server = {META_SERVER: {"name": "memdex", "version": __version__}}
        out = {"result": {"resultType": "complete", **result, "_meta": {**result.get("_meta", {}), **server}}}
    return out


def _dispatch(session: Session, tools: dict, method: Any, params: dict, modern: bool) -> dict:
    """Answer one request. `modern` is the stateless 2026-07-28 form; otherwise the handshake form."""
    if modern and method == "server/discover":
        return {"result": {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": CAPABILITIES,
            "instructions": instructions(session.listed),
            "ttlMs": CACHE_TTL_MS, "cacheScope": "public",
        }}
    if not modern and method == "initialize":
        client = _agent_from_client(params.get("clientInfo"))
        if client and not session.agent_name:
            session.agent_name = client
        # The client's version if this server speaks it, else the newest handshake revision it does;
        # the client then decides whether it can continue.
        asked = params.get("protocolVersion")
        return {"result": {
            "protocolVersion": asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": "memdex", "version": __version__},
            "instructions": instructions(session.listed),
        }}
    if not modern and method == "ping":  # removed in 2026-07-28
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
        # costing every request their schema. The order is the order Session.tools() declares them in:
        # the same on every request and in every process, so a client can cache the list and the
        # model's prompt cache keeps hitting.
        listed = [t.schema(session.full_schemas) for t in tools.values() if session.lists(t.name)]
        if not modern:
            return {"result": {"tools": listed}}
        if params.get("cursor") is not None:
            return _invalid("invalid cursor: tools/list is a single page")
        return {"result": {"tools": listed, "ttlMs": CACHE_TTL_MS, "cacheScope": "public"}}
    if method == "tools/call":
        if modern:
            # 2026-07-28 reports these as protocol errors; a client may refresh its tool list on one.
            name = params.get("name")
            if not isinstance(name, str) or name not in tools:
                return _invalid(f"Unknown tool: {name}")
            if not isinstance(params.get("arguments") or {}, dict):
                return _invalid("tools/call arguments must be an object")
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
            if not isinstance(m, dict):  # valid JSON, but not a JSON-RPC message
                _reply(None, error={"code": -32600, "message": "invalid request"})
                continue
            out = handle(session, tools, m)
            if out is not None:
                try:
                    _reply(m.get("id"), out.get("result"), out.get("error"))
                except (BrokenPipeError, OSError):
                    return  # the client closed its end; stop instead of holding the database open
