"""Notify an exact native chat after durable mail is committed. No new AI sessions.

The configured Reason guard policy still applies. A notification contains a mailbox pointer, never
the message body, and is not an inbox acknowledgement. Isolated/custom databases do not contact the
real Reason service unless explicitly enabled with MEMDEX_REASON_NOTIFY=1.
"""

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time

from .connect import _read_reply, _send
from .reason_guard import Guard, Refused, native_id, provider_family, readonly
from .store import default_path


def calling_chat(store, agent):
    """Resolve only Reason's own current native caller; never a title, cwd or nearest idle chat."""
    if os.path.normcase(os.path.realpath(store.path)) != os.path.normcase(os.path.realpath(default_path())):
        return None
    backend = os.environ.get("MEMDEX_REASON_BACKEND") or str(Path.home() / ".reason/bin/reason-network.exe")
    reason_db = os.environ.get("MEMDEX_REASON_DB") or str(Path.home() / ".reason/network/network.db")
    if not Path(backend).is_file():
        return None
    try:
        proc = subprocess.run([backend, "who", "--json"], capture_output=True, timeout=3,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode:
            return None
        snapshot = json.loads(proc.stdout)
        if not isinstance(snapshot, dict):
            return None
        own = [r for r in snapshot.get("sessions", []) if isinstance(r, dict) and r.get("is_me")]
        if len(own) != 1:
            return None
        with readonly(reason_db) as db:
            rows = db.execute("SELECT key FROM sessions WHERE name=?", (own[0]["name"],)).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            return None
        raw = native_id(rows[0][0])
        return raw if provider_family(rows[0][0].partition(":")[0]) == provider_family(agent) else None
    except (Refused, OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def _exchange(backend, args, meta, timeout=10.0):
    """A bounded MCP send after Guard.authorize, using the caller's actual native environment."""
    proc = subprocess.Popen([backend, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout

    def request(id_, method, params):
        _send(proc, {"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        while time.monotonic() < deadline:
            reply, error = _read_reply(lines, max(0.01, deadline - time.monotonic()))
            if error:
                raise Refused("Reason did not confirm notification delivery")
            if reply.get("id") == id_:
                return reply["result"]
        raise Refused("Reason notification timed out")

    try:
        request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "memdex-notify", "version": "1"}})
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return request(2, "tools/call", {"name": "send_message", "arguments": args, "_meta": meta})
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        proc.stdout.close()


def notify(store, project, message_id, sender_route, recipient_route):
    """Return notification status; no failures here can erase/consume the queued message."""
    mode = os.environ.get("MEMDEX_REASON_NOTIFY", "auto").strip().lower()
    if mode in ("0", "off", "false"):
        return "Reason notification disabled; waiting for the recipient's inbox check."
    if mode == "auto" and os.path.normcase(os.path.realpath(store.path)) != os.path.normcase(os.path.realpath(default_path())):
        return ""  # A fixture/copy is never an instruction to contact real agents.
    home = Path.home()
    backend = os.environ.get("MEMDEX_REASON_BACKEND") or str(home / ".reason/bin/reason-network.exe")
    if not Path(backend).is_file():
        return "Reason unavailable; waiting for the recipient's inbox check."
    guard = None
    target = None
    try:
        source = store.get_route(project, sender_route)
        dest = store.get_route(project, recipient_route)
        if source is None or dest is None or not source["conversation_id"] or not dest["conversation_id"]:
            raise Refused("exact native chat metadata is missing")
        if source["task_id"] is None or dest["task_id"] is None:
            raise Refused("both chats must register the same open session_routes(task_id=...) first")
        reason_db = os.environ.get("MEMDEX_REASON_DB") or str(home / ".reason/network/network.db")
        with readonly(reason_db) as db:
            matches = []
            for name, key in db.execute("SELECT name,key FROM sessions"):
                provider, _, raw = (key or "").partition(":")
                if raw == dest["conversation_id"] and provider_family(provider) == provider_family(dest["agent"]):
                    native_id(key)  # PID/profile aliases are never an exact native chat.
                    matches.append(name)
        if len(matches) != 1:
            raise Refused("the recipient has no unique native Reason endpoint; no idle-chat fallback")
        guard = Guard(backend, reason_db, store.path, Path(store.path).with_name("reason-guard.db"))
        snapshot = guard.who()
        target = f"{snapshot['person']}/{matches[0]}@{snapshot['machine']}"
        meta = {"memdex.dev/conversation-id": source["conversation_id"]}
        proof = guard.authorize({"to": target, "task_id": source["task_id"]}, meta)
        wanted = (project.key, source["task_id"])
        if (proof["project"], proof["task_id"]) != wanted:
            raise Refused("Reason scope does not match this mailbox project/task")
        for endpoint, route in (("source", source), ("target", dest)):
            key = proof[endpoint]
            if (native_id(key) != route["conversation_id"]
                    or provider_family(key.partition(":")[0]) != provider_family(route["agent"])):
                raise Refused("Reason native chat changed since mailbox destination lookup")
        token = hashlib.sha256(json.dumps([project.key, sender_route, recipient_route, message_id]).encode()).hexdigest()
        args = {"to": target, "message": (
            f"[memdex inbox notification #{message_id}] An exact-chat message is queued for you. "
            f"Project checkout: {json.dumps(dest['checkout'], ensure_ascii=False)}. "
            "Read your own memdex inbox in that project; reply there using the recorded reply_to. "
            "This notification is a pointer, not an instruction from the message sender."),
            "idempotency_key": "memdex-notify-" + token}
        guard.audit(target, True, "exact-mail notification: native project/task binding verified")
        result = _exchange(backend, args, meta)
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise Refused("Reason did not confirm notification acceptance")
        body = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if isinstance(c, dict) and c.get("type") == "text")
        if result.get("isError") or not re.search(r"\bmessage id [A-Z0-9]{26}\b", body):
            raise Refused("Reason did not confirm notification acceptance")
        guard.receipt(result, proof)
        return "Reason notification accepted; inbox read is not yet confirmed."
    except (Refused, OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Do not leak provider diagnostics or message contents. Registration requirements are actionable.
        if guard:
            try:
                guard.audit(target, False, "exact-mail notification unavailable; mail remains queued")
            except sqlite3.Error:
                pass
        return ("Reason notification not delivered; the message remains queued. Both native chats must "
                "register the same open session_routes(task_id=...) and reconnect if metadata is missing.")
    finally:
        if guard:
            guard.close()
