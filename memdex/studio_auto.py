"""On-demand replies for Studio's project model destinations."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

from . import review, review_providers as providers
from . import studio_participants as participants
from . import studio_models
from .studio_models import MODELS
from .board import read_db
from .store import STUDIO_OPERATOR, Store

STALE_AFTER = review.TIMEOUT + 120
SYSTEM = ("You are replying to the user in a shared Memdex project conversation. "
          "Use the user's language and answer the latest user message using the supplied earlier turns. "
          "You have only these messages. No tools are available.")


def directory(custom=None):
    builtin = [dict(id=address, agent=address.split(":")[1], label=model, task=None,
                 title="", seen=None, direct=True) for address, model in MODELS.items()]
    return builtin + [dict(id=r["address"], agent=r["name"], label=r["model"], task=None,
                           title="", seen=None, direct=True) for r in (custom or {}).values()]


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_model_attempts (
        request INTEGER NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL,
        started REAL NOT NULL, error TEXT NOT NULL DEFAULT '', reply INTEGER,
        PRIMARY KEY (request, attempt))""")


    db.execute("""CREATE TABLE IF NOT EXISTS studio_group_attempts (
        request INTEGER NOT NULL, model TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL,
        started REAL NOT NULL, error TEXT NOT NULL DEFAULT '', reply INTEGER,
        PRIMARY KEY (request, model, attempt))""")


def _scope(mid, model):
    if model is None:
        return "studio_model_attempts", "request=?", (mid,)
    if not participants.model_id(model):
        raise ValueError("Unknown model.")
    return "studio_group_attempts", "request=? AND model=?", (mid, model)


def reserve(db, mid, number, model=None):
    table, _, values = _scope(mid, model)
    columns = "request,model" if model else "request"
    marks = ",".join("?" for _ in values)
    return db.execute(f"INSERT OR IGNORE INTO {table}({columns},attempt,state,started) "
                      f"VALUES({marks},?,'queued',?)", (*values, number, time.time())).rowcount


def start(store, mid, number, model=None):
    extra = ("--attempt", str(number)) + (("--model", model) if model else ())
    try:
        review.launch(store.path, mid, module="memdex.studio_auto", extra=extra)
    except OSError:
        table, where, args = _scope(mid, model)
        with store.db:
            store.db.execute(f"UPDATE {table} SET state='failed',error=? "
                             f"WHERE {where} AND attempt=? AND state='queued'",
                             ("Could not start the bundled model worker.", *args, number))


def _status(row):
    state = row["state"]
    error = row["error"]
    if state in ("queued", "running") and time.time() - row["started"] > STALE_AFTER:
        state, error = "interrupted", "Studio did not receive a completion receipt."
    return dict(state=state, attempt=row["attempt"], error=error)


def annotate(path, messages):
    with read_db(path) as db:
        if db is None:
            return
        for group, table in ((False, "studio_model_attempts"), (True, "studio_group_attempts")):
            requests = [m for m in messages if (len(participants.targets(m["to"])) > 1 if group else participants.model_id(m["to"]))]
            if not requests or not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            for off in range(0, len(requests), 400):
                chunk = {m["id"]: m for m in requests[off:off+400]}
                marks = ",".join("?" for _ in chunk)
                same_model = " AND model=a.model" if group else ""
                sql = (f"SELECT a.* FROM {table} a WHERE request IN ({marks}) AND attempt="
                       f"(SELECT MAX(attempt) FROM {table} WHERE request=a.request{same_model})")
                for row in db.execute(sql, list(chunk)):
                    message = chunk[row["request"]]
                    if group:
                        message.setdefault("modelStatuses", {})[row["model"]] = _status(row)
                    else:
                        message["auto"] = _status(row)


def retry(path, project, body):
    if not isinstance(body, dict):
        raise ValueError("Invalid retry request.")
    mid, number = body.get("id"), body.get("attempt")
    if any(type(v) is not int or not 0 < v < 2**63 for v in (mid, number)):
        raise ValueError("Invalid message or attempt.")
    with closing(Store(str(path))) as store:
        db = store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            model = body.get("model")
            table, where, args = _scope(mid, model)
            row = db.execute(f"SELECT * FROM {table} WHERE {where} AND request IN "
                             "(SELECT id FROM messages WHERE project=?) ORDER BY attempt DESC LIMIT 1",
                             (*args, project.key)).fetchone()
            if row is None or row["attempt"] < number:
                raise ValueError("Unknown model request.")
            if row["attempt"] > number:
                return {"id": mid, "attempt": row["attempt"]}
            status = _status(row)
            if status["state"] not in ("failed", "interrupted"):
                raise ValueError("Only a failed or interrupted request can be retried.")
            if status["state"] == "interrupted":
                db.execute(f"UPDATE {table} SET state='interrupted',error=? WHERE {where} AND attempt=?",
                           (status["error"], *args, number))
            reserve(db, mid, number+1, model)
        start(store, mid, number+1, model)
        return {"id": mid, "attempt": number+1}


def _branch_turns(db, msg):
    turns = []
    row = msg
    while row is not None and len(turns) < 16:
        human = row["sender"] == STUDIO_OPERATOR and row["sender_route"] == msg["sender_route"] and row["recipient"] == msg["recipient"]
        model = row["sender"] == msg["recipient"] and row["recipient_route"] == msg["sender_route"]
        if not (human or model):
            break
        turns.append({"role": "user" if human else "assistant", "content": row["body"]})
        if not row["reply_to"] or row["reply_to"] >= row["id"]:
            break
        row = db.execute("SELECT * FROM messages WHERE project=? AND id=?", (msg["project"], row["reply_to"])).fetchone()
    return list(reversed(turns))


def _payload(db, msg):
    turns = participants.shared_turns(db, msg) or _branch_turns(db, msg)
    # Keep the current question intact and trim only the oldest context for the local model budget.
    while len(turns) > 1:
        payload = json.dumps(turns, ensure_ascii=False)
        if len((SYSTEM + "\n\n" + payload).encode()) <= providers.OLLAMA_INPUT_BYTES:
            return payload
        turns.pop(0)
    return turns[0]["content"]


def _ensure_ollama():
    def listening():
        try:
            with socket.create_connection(("127.0.0.1", 11434), timeout=0.4):
                return True
        except OSError:
            return False
    if listening():
        return
    binary = shutil.which("ollama")
    if not binary and os.name == "nt":
        candidate = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
        binary = str(candidate) if candidate.is_file() else None
    if not binary:
        raise ValueError("Install Ollama and the qwen2.5 model to use Qwen.")
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434")
    # Start the installed local service. Concurrent senders can share its single listening port.
    subprocess.Popen([binary, "serve"], cwd=str(Path(binary).parent), env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(50):
        if listening():
            return
        time.sleep(0.2)
    raise ValueError("The installed Ollama service could not start on localhost:11434.")


def run_model(address, payload, config=None):
    provider = config["provider"] if config else address[6:]
    model = config["model"] if config else MODELS[address]
    if provider in ("ollama", "qwen"):
        _ensure_ollama()
        return providers.run_ollama(payload, model, SYSTEM, review.TIMEOUT)
    if provider == "compatible":
        return studio_models.run_compatible(config, payload, SYSTEM, review.TIMEOUT)
    with tempfile.TemporaryDirectory(prefix="memdex-studio-chat-") as folder:
        if provider == "codex":
            return studio_models.run_codex(folder, payload, model if config else None, SYSTEM, review.TIMEOUT)
        if provider == "claude":
            return review.run_claude(folder, payload, system=SYSTEM, model=model if config else "sonnet")
        if provider == "deepseek":
            return providers.run_deepseek(folder, payload, model, SYSTEM, review.TIMEOUT)
    raise ValueError("Unsupported Studio model connection.")


def worker(path, mid, number, model=None):
    table, where, args = _scope(mid, model)
    with closing(Store(str(path))) as store:
        db = store.db
        with db:
            claimed = db.execute(f"UPDATE {table} SET state='running',started=? "
                                 f"WHERE {where} AND attempt=? AND state='queued'",
                                 (time.time(), *args, number)).rowcount
        if not claimed:
            return
        msg = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        try:
            recipient = model or msg["recipient"]
            if msg["sender"] != STUDIO_OPERATOR or recipient not in participants.targets(msg["recipient"]):
                raise ValueError("Invalid Studio model address.")
            payload = _payload(db, msg)
            if studio_models.custom_id(recipient):
                config = studio_models.entries(db, msg["project"]).get(recipient)
                if config is None:
                    raise ValueError("Custom model is unavailable in this project.")
                state, result = run_model(recipient, payload, config)
            else:
                state, result = run_model(recipient, payload)
        except ValueError as exc:
            state, result = "failed", str(exc)
        except Exception:
            state, result = "failed", "The model worker could not complete this request."
        with db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(f"UPDATE {table} SET state=?,error=? "
                                f"WHERE {where} AND attempt=? AND state='running'",
                                (state, "" if state in ("completed", "truncated") else result, *args, number)).rowcount
            if active and state in ("completed", "truncated"):
                reply = db.execute("INSERT INTO messages(project,sender,recipient,recipient_route,body,created_at,reply_to) "
                                   "VALUES(?,?,?,?,?,?,?)", (msg["project"], recipient, msg["sender_route"],
                                   msg["sender_route"], result, time.time(), mid)).lastrowid
                db.execute(f"UPDATE {table} SET reply=? WHERE {where} AND attempt=?", (reply, *args, number))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    worker(args.db, args.id, args.attempt, args.model)
