"""Operator-authored, project-visible messages in the desktop Studio."""
import hashlib
import json
from contextlib import closing
from dataclasses import replace

from .board import message_content, read_db
from . import studio_auto, studio_participants as participants
from .store import STUDIO_OPERATOR, Store
from .tools import TASK_FINISHED, render_task_context


def _directory(db, project):
    """Project registration, rather than an IDE history entry, supplies a deliverable address."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tasks = {}
    if "tasks" in tables:
        tasks = {r["id"]: r for r in db.execute("SELECT id,title,status FROM tasks WHERE project=?", (project.key,))}
    addresses, routed = [], set()
    if "chat_routes" in tables:
        for raw in db.execute("SELECT * FROM chat_routes WHERE project=?", (project.key,)):
            row = dict(raw)
            routed.add(row["agent"].lower())
            task = tasks.get(row.get("task_id"))
            if row.get("task_id") is not None and (task is None or task["status"] in TASK_FINISHED):
                continue
            addresses.append(dict(id=row["route"], agent=row["agent"], label=row["label"] or "",
                                  task=row.get("task_id"), title=task["title"] if task else "", seen=row["last_seen_at"]))
    if "sessions" in tables:
        for agent, seen in db.execute("SELECT agent,last_seen_at FROM sessions WHERE project=?", (project.key,)):
            if agent.lower() not in routed:
                addresses.append(dict(id=agent.lower(), agent=agent, label="", task=None, title="", seen=seen))
    return sorted((r for r in addresses if r["agent"].lower().split("@")[0] not in (STUDIO_OPERATOR, "workspace")),
                  key=lambda r: (-(r["seen"] or 0), r["id"]))


def recipients(path, project):
    with read_db(path) as db:
        return _directory(db, project) if db is not None else []


def group_chats(path, project, messages):
    """Keep a thread's identity and title across feed pages and partial search results."""
    parents = {m["id"]: m["reply"] for m in messages}
    subjects = {m["id"]: " ".join(m["body"].split())[:160] for m in messages}
    missing = {p for mid, p in parents.items() if p and p < mid and p not in parents}
    if missing:
        with read_db(path) as db:
            while missing:
                batch = list(missing)[:400]
                missing.difference_update(batch)
                rows = db.execute("SELECT id,reply_to,body FROM messages WHERE project=? AND id IN (" +
                                  ",".join("?" for _ in batch) + ")", [project.key, *batch])
                for mid, parent, body in rows:
                    parents[mid] = parent
                    subjects[mid] = " ".join(message_content(body)[1].split())[:160]
                    if parent and parent < mid and parent not in parents:
                        missing.add(parent)
    roots = {}
    for mid in sorted(parents):
        roots[mid] = roots.get(parents[mid], mid)
    for m in messages:
        root = roots[m["id"]]
        m.update(chatRoot=root, subject=subjects[root])


def send(path, project, body):
    if not isinstance(body, dict):
        raise ValueError("Invalid message.")
    target, text, key = (body.get(k) for k in ("to", "text", "key"))
    task, parent = body.get("task"), body.get("reply")
    models = participants.selection(body["models"]) if "models" in body else []
    if "models" in body:
        if task is not None:
            raise ValueError("Choose a project model chat.")
        target = participants.address(models)
    if not isinstance(target, str) or not target or (not models and len(target) > 512):
        raise ValueError("Choose an AI recipient.")
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError("Enter 1 to 12,000 characters.")
    if any(v is not None and (type(v) is not int or not 0 < v < 2**63) for v in (task, parent)):
        raise ValueError("Invalid task or reply.")
    text = text.strip()
    identity = json.dumps([project.key, STUDIO_OPERATOR, task])
    origin = "route:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    with closing(Store(str(path))) as store:
        # Recover a lost receipt even if the recipient's task has since been closed.
        previous = store.recover_post(project, origin, target, key, parent, task, text)
        if previous is not None:
            return {"id": previous}
        def destination():
            if (models or participants.model_id(target)) and task is None:
                participants.validate_registered(store.db, project.key, models or [target])
                return target
            row = next((r for r in _directory(store.db, project) if r["id"] == target), None)
            if row is None or row["task"] != task or participants.model_id(target):
                raise ValueError("Recipient changed. Refresh the AI list and choose again.")
            return row["id"]
        destination()
        # Each task has its own human return address. It is never registered as a native AI session.
        store.register_route(replace(project, root=project.key), origin, STUDIO_OPERATOR, "Studio", task_id=task)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            previous = store.recover_post(project, origin, target, key, parent, task, text)
            if previous is not None:
                return {"id": previous}
            address = destination()
            if parent is not None:
                msg = store.db.execute("SELECT sender_route,sender,recipient,recipient_route FROM messages "
                                       "WHERE project=? AND id=?", (project.key, parent)).fetchone()
                same_address = msg is not None and (
                    (msg["sender_route"] == origin and (msg["recipient_route"] or msg["recipient"]) == target)
                    or (msg["recipient_route"] == origin and (msg["sender_route"] or msg["sender"]) == target))
                if models or participants.model_id(target):
                    root = participants.root_for(store.db, project.key, parent)
                    if root is None or root["sender"] != STUDIO_OPERATOR or root["sender_route"] != origin:
                        raise ValueError("Select a Studio model chat.")
                    if participants.current(store.db, root) != (models or [target]):
                        raise ValueError("Participants changed. Refresh the chat and choose again.")
                    if models and msg is not None:
                        same_address = ((msg["sender_route"] == origin and participants.targets(msg["recipient"]))
                                        or (msg["recipient_route"] == origin and participants.model_id(msg["sender"])))
                if not same_address:
                    raise ValueError("Select a message between this AI and Studio to continue the conversation.")
            message = text
            if task is not None:
                message = "[task status at send]\n" + render_task_context(store.task_rows(project), task) + "\n\n" + text
            mid = store.post(project, STUDIO_OPERATOR, message, to=address, sender_route=origin,
                             reply_to=parent, request_key=key, original_body=text, task_id=task)
            jobs = []
            if models or participants.model_id(address):
                studio_auto.initialize(store.db)
                if parent is None and len(models) > 1:
                    participants.save(store.db, project.key, mid, models)
                for model in (models if len(models) > 1 else [None]):
                    if studio_auto.reserve(store.db, mid, 1, model):
                        jobs.append(model)
        for model in jobs:
            studio_auto.start(store, mid, 1, model)
        return {"id": mid}
