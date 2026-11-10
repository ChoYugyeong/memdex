"""Optional stdio policy proxy for Reason Network. Standard library only.

Native IDs come from host metadata (or Reason's own native-session resolver), never titles or model
arguments. This guards the configured MCP transport; it cannot intercept a separately invoked binary.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Optional


class Refused(ValueError):
    pass


@contextmanager
def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        yield db
    finally:
        db.close()


def native_id(key):
    provider, sep, value = (key or "").partition(":")
    if not sep or provider not in ("codex", "claude-code", "dsh", "deepseek", "reason") or not value:
        raise Refused("unknown native session identity; no task can be assigned by agent name alone")
    return value


class Guard:
    def __init__(self, backend, reason_db, memdex_db, ledger, inventory=None):
        self.backend, self.reason_db, self.memdex_db = backend, reason_db, memdex_db
        self.inventory = inventory
        self.lock = threading.Lock()
        Path(ledger).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(ledger), check_same_thread=False, timeout=2)
        self.db.execute("CREATE TABLE IF NOT EXISTS approved(message TEXT PRIMARY KEY, source TEXT, "
                        "target TEXT, project TEXT, task_id INTEGER, created_at REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS audit(created_at REAL, allowed INTEGER, "
                        "target TEXT, reason TEXT)")
        self.db.commit()

    def close(self):
        self.db.close()

    def who(self):
        if self.inventory is not None:
            return self.inventory()
        p = subprocess.run([self.backend, "who", "--json"], capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode:
            raise Refused("Reason session metadata unavailable; no send was attempted")
        return json.loads(p.stdout)

    @staticmethod
    def address(value, person, machine):
        if not isinstance(value, str) or len(value) > 256:
            raise Refused("use one exact person/session address")
        match = re.fullmatch(r"([^/@\s*]+)/([^/@\s*]+)(?:@([^/@\s*]+))?", value)
        if not match or match[1] != person or (match[3] and match[3] != machine):
            raise Refused("destination has no verifiable local native metadata; broadcasts/remote guesses refused")
        return match[2]

    def scopes(self, key, expected_task=None):
        raw_id = native_id(key)
        family = key.partition(":")[0]
        if family == "deepseek":
            family = "dsh"

        def same_provider(agent):
            base = agent.lower().split("@", 1)[0].split("/", 1)[0]
            aliases = {"gpt": "codex", "claude": "claude-code", "deepseek": "dsh"}
            base = "dsh" if base.startswith("dsh-") else aliases.get(base, base)
            return base == family

        with readonly(self.memdex_db) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT DISTINCT r.project,r.task_id,r.agent FROM chat_routes r JOIN tasks t "
                              "ON t.project=r.project AND t.id=r.task_id WHERE r.conversation_id=? "
                              "AND t.status NOT IN ('verified','self_verified')", (raw_id,)).fetchall()
        scopes = {(r["project"], r["task_id"]) for r in rows if same_provider(r["agent"])}
        if expected_task is not None:
            scopes = {s for s in scopes if s[1] == expected_task}
        if len(scopes) != 1:
            raise Refused("native chat has no unique open project/task binding; register session_routes(task_id=…) "
                          "inside the intended chat; do not reuse an idle chat or infer work from its title")
        return next(iter(scopes))

    def authorize(self, args, meta):
        try:
            return self._authorize(args, meta)
        except (sqlite3.Error, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise Refused("required metadata could not be read; no send was attempted") from exc

    def _authorize(self, args, meta):
        if not isinstance(args, dict) or not isinstance(meta, dict):
            raise Refused("invalid host envelope")
        if args.get("session_id") is not None:
            raise Refused("source session_id must come from host metadata, not model arguments")
        snapshot = self.who()
        person, machine = snapshot.get("person"), snapshot.get("machine")
        target_name = self.address(args.get("to"), person, machine)
        task = args.get("task_id")
        if task is not None and (type(task) is not int or task <= 0):
            raise Refused("task_id must be a positive integer")
        ids = [meta[k] for k in ("memdex.dev/conversation-id", "threadId", "thread_id", "codex/threadId",
                                 "session_id") if k in meta]
        if ids and any(not isinstance(i, str) or not i.strip() or len(i) > 512 or not i.isprintable()
                       or i != ids[0] for i in ids):
            raise Refused("conflicting or invalid host native IDs")
        with readonly(self.reason_db) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT name,key,agent FROM sessions").fetchall()
            targets = [r for r in rows if r["name"] == target_name]
            own = [r for r in snapshot.get("sessions", []) if r.get("is_me")]
            if ids:
                sources = [r for r in rows if r["key"] and r["key"].partition(":")[2] == ids[0]]
                if len(own) != 1 or len(sources) != 1 or sources[0]["name"] != own[0].get("name"):
                    raise Refused("host/backend native identity mismatch; reconnect in the intended chat")
            else:
                # Reason itself resolves the calling native session from the host process environment.
                # Neither the recipient nor the model supplies this identity.
                sources = [r for r in rows if len(own) == 1 and r["name"] == own[0].get("name")]
            if len(targets) != 1 or len(sources) != 1:
                raise Refused("source or destination native session is missing/ambiguous; no nearest/idle fallback")
            source, target = sources[0], targets[0]
            if source["key"] == target["key"]:
                raise Refused("destination is your own native chat")
            source_id, target_id = native_id(source["key"]), native_id(target["key"])
            reply = args.get("reply_to")
            original = None
            if reply is not None:
                original = db.execute("SELECT sender,recipient FROM messages WHERE id=?", (reply,)).fetchone()
                if original is None:
                    raise Refused("reply origin is unknown; no new recipient will be guessed")
                endpoints = {self.address(original["sender"], person, machine),
                             self.address(original["recipient"], person, machine)}
                if endpoints != {source["name"], target["name"]}:
                    raise Refused("reply recipient does not match the original message endpoints")
        # A recorded, previously approved conversation may finish after a chat changes tasks. Names are
        # insufficient: both native keys must still match (including after session-name reuse).
        if original is not None:
            with self.lock:
                approved = self.db.execute("SELECT source,target,project,task_id FROM approved WHERE message=?",
                                           (reply,)).fetchone()
            if approved and {approved[0], approved[1]} == {source["key"], target["key"]}:
                if task is not None and task != approved[3]:
                    raise Refused("a reply cannot repurpose the original conversation for another task")
                return {"source": source["key"], "target": target["key"], "project": approved[2],
                        "task_id": approved[3]}
        a, b = self.scopes(source["key"], task), self.scopes(target["key"])
        if a != b:
            raise Refused("project/task mismatch: this destination belongs to different work; no send attempted")
        return {"source": source["key"], "target": target["key"], "project": a[0], "task_id": a[1]}

    def audit(self, target, allowed, reason):
        with self.lock, self.db:
            self.db.execute("INSERT INTO audit VALUES(?,?,?,?)", (time.time(), int(allowed), str(target)[:256], reason))

    def receipt(self, result, proof):
        if result.get("isError"):
            return
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        match = re.search(r"\bmessage id ([A-Z0-9]{26})\b", text)
        if match:
            with self.lock, self.db:
                self.db.execute("INSERT OR IGNORE INTO approved VALUES(?,?,?,?,?,?)",
                                (match[1], proof["source"], proof["target"], proof["project"],
                                 proof["task_id"], time.time()))


GUIDANCE = ("\nLocal routing guard is active. New work requires the exact native source and destination to be "
            "bound to the same open memdex project/task ID via session_routes(task_id=…). Titles, cwd=C:/Dev, "
            "idle status and agent names do not prove task assignment. Missing metadata is a refusal. "
            "Do not bypass refusals with the Reason binary, CLI, broadcasts or another chat. Replies must "
            "match the original message endpoints and an approved or currently same-task conversation.")


def decorate(message):
    result = message.get("result", {})
    if "serverInfo" in result:
        result["instructions"] = result.get("instructions", "") + GUIDANCE
    for tool in result.get("tools", []):
        if tool.get("name") == "send_message":
            tool["description"] += GUIDANCE
            tool.setdefault("inputSchema", {}).setdefault("properties", {})["task_id"] = {
                "type": "integer", "minimum": 1, "description": "Open memdex task ID; must match native chat bindings"}
    return message


def proxy(guard):
    child = subprocess.Popen([guard.backend, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=sys.stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output_lock, pending_lock = threading.Lock(), threading.Lock()
    pending = {}

    def emit(message):
        with output_lock:
            sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            sys.stdout.buffer.flush()

    def read_backend():
        for raw in child.stdout:
            try:
                message = json.loads(raw)
                with pending_lock:
                    proof = pending.pop(json.dumps(message.get("id")), None)
                if proof and "result" in message:
                    guard.receipt(message["result"], proof)
                emit(decorate(message))
            except (ValueError, sqlite3.Error, OSError) as exc:
                print("Reason guard response error: " + type(exc).__name__, file=sys.stderr)

    thread = threading.Thread(target=read_backend, daemon=True)
    thread.start()
    try:
        for raw in sys.stdin.buffer:
            try:
                batch = json.loads(raw)
            except ValueError:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}})
                continue
            for msg in batch if isinstance(batch, list) else [batch]:
                if not isinstance(msg, dict):
                    continue
                params = msg.get("params") or {}
                if msg.get("method") == "tools/call" and params.get("name") == "send_message":
                    args = params.get("arguments") or {}
                    try:
                        if "id" not in msg:
                            raise Refused("send_message requires a request ID")
                        proof = guard.authorize(args, params.get("_meta") or {})
                        guard.audit(args.get("to"), True, "native project/task binding verified")
                    except (Refused, sqlite3.Error) as exc:
                        try:
                            guard.audit(args.get("to") if isinstance(args, dict) else None, False, str(exc))
                        except sqlite3.Error:
                            pass  # still refuse; a broken audit DB never makes a send permissible
                        emit({"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                            "isError": True, "content": [{"type": "text", "text": "Reason guard refused: " + str(exc)}]}})
                        continue
                    with pending_lock:
                        pending[json.dumps(msg["id"])] = proof
                    params["arguments"] = {k: v for k, v in args.items() if k != "task_id"}
                child.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
                child.stdin.flush()
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)
        thread.join(timeout=2)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", default=str(Path.home() / ".reason/bin/reason-network.exe"))
    p.add_argument("--reason-db", default=str(Path.home() / ".reason/network/network.db"))
    p.add_argument("--memdex-db", default=str(Path(os.environ.get("MEMDEX_HOME") or Path.home() / ".memdex") / "memdex.db"))
    p.add_argument("--ledger", default=str(Path.home() / ".memdex/reason-guard.db"))
    p.add_argument("--check", help="only validate this destination, never send")
    p.add_argument("--task-id", type=int)
    a = p.parse_args()
    guard = Guard(a.backend, a.reason_db, a.memdex_db, a.ledger)
    try:
        if a.check:
            try:
                print(json.dumps(guard.authorize({"to": a.check, "task_id": a.task_id}, {})))
            except Refused as exc:
                print("REFUSED: " + str(exc))
                return 2
        else:
            proxy(guard)
    finally:
        guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
