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
import sys
import traceback
from typing import Any, Optional

from . import __version__
from .store import Store
from .tools import INSTRUCTIONS, Session, is_dsh_agent

LATEST_PROTOCOL = "2025-06-18"


def _reply(id_: Any, result: Any = None, error: Optional[dict] = None) -> None:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.buffer.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _call(session: Session, tools: dict, name: str, args: dict) -> dict:
    tool = tools.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    accepted = inspect.signature(tool.fn).parameters
    try:
        text = tool.fn(**{k: v for k, v in (args or {}).items() if k in accepted})
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
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "memdex", "version": __version__},
            "instructions": INSTRUCTIONS,
        }}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": [t.schema() for t in tools.values()]}}
    if method == "tools/call":
        # One stdio connection can serve several chats. Identity comes from the current host
        # envelope, never from the model's arguments or from a previous tools/call.
        meta = params.get("_meta", {})
        keys = ["memdex.dev/conversation-id"]
        agent = (os.environ.get("MEMDEX_AGENT") or session.agent_name).lower().split("@")[0].split("/")[0]
        dsh = is_dsh_agent(agent) or is_dsh_agent(session.agent_name.lower())
        codex = agent in ("codex", "gpt") or "codex" in session.agent_name.lower()
        if dsh:
            keys.append("session_id")
        elif codex:
            keys.extend(("threadId", "thread_id", "codex/threadId"))
        ids = [meta[k] for k in keys if isinstance(meta, dict) and k in meta]
        if not isinstance(meta, dict) or any(not isinstance(i, str) or not i.strip()
                or len(i) > 512 or not i.isprintable() or i != ids[0] for i in ids):
            return {"error": {"code": -32602, "message": "invalid or conflicting conversation metadata"}}
        chat = ids[0] if ids else None
        saved = session.conversation_id, session.require_chat_id, session._project
        try:
            session.conversation_id, session.require_chat_id = chat, dsh or codex
            if chat:
                session._project = session.chat_projects.get(chat)
            name = params.get("name", "")
            args = params.get("arguments") or {}
            result = _call(session, tools, name, args)
            if chat and name == "project_brief" and not result["isError"] and args.get("project"):
                session.chat_projects[chat] = session.project
            return {"result": result}
        finally:
            session.conversation_id, session.require_chat_id = saved[:2]
            if chat:
                session._project = saved[2]
    return {"error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(db_path: Optional[str] = None, cwd: Optional[str] = None) -> None:
    session = Session(Store(db_path))
    if cwd:
        session.cwd = cwd
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
