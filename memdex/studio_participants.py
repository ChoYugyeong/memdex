"""Membership and shared context of a Studio model conversation."""
from contextlib import closing
import json

from .board import read_db
from .store import STUDIO_OPERATOR, Store

from .studio_models import MODELS, custom_id, entries


def model_id(value):
    return isinstance(value, str) and (value in MODELS or custom_id(value))


def validate_registered(db, project_key, models):
    custom = [m for m in models if custom_id(m)]
    if custom and not set(custom).issubset(entries(db, project_key)):
        raise ValueError("This custom model is not registered in this project.")


def selection(values):
    if not isinstance(values, list) or any(not model_id(v) for v in values):
        raise ValueError("Choose valid Studio models.")
    return sorted(set(values))


def address(models):
    if not models:
        raise ValueError("Select at least one participant to send a message.")
    return models[0] if len(models) == 1 else "models:" + "+".join(m[6:] for m in models)


def targets(value):
    if model_id(value):
        return [value]
    if isinstance(value, str) and value.startswith("models:"):
        models = ["model:" + m for m in value[7:].split("+")]
        if len(models) >= 2 and all(model_id(m) for m in models) and models == sorted(set(models)):
            return models
    return []


def _exists(db):
    return db is not None and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_participants'").fetchone()


def catalog(path, project):
    with read_db(path) as db:
        if not _exists(db):
            return {}
        return {root: json.loads(models) for root, models in db.execute(
            "SELECT root,models FROM studio_participants WHERE project=?", (project.key,))}


def root_for(db, project_key, mid):
    return db.execute("""WITH RECURSIVE chain(id,reply_to) AS (
        SELECT id,reply_to FROM messages WHERE project=? AND id=?
        UNION ALL SELECT m.id,m.reply_to FROM messages m JOIN chain c ON m.id=c.reply_to
        WHERE m.project=? AND m.id<c.id)
        SELECT m.* FROM messages m JOIN chain c ON m.id=c.id WHERE c.reply_to IS NULL""",
        (project_key, mid, project_key)).fetchone()


def current(db, root):
    if _exists(db):
        row = db.execute("SELECT models FROM studio_participants WHERE project=? AND root=?",
                         (root["project"], root["id"])).fetchone()
        if row is not None:
            return json.loads(row[0])
    return targets(root["recipient"])


def save(db, project_key, root, models):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_participants (
        project TEXT NOT NULL, root INTEGER NOT NULL, models TEXT NOT NULL, PRIMARY KEY(project,root))""")
    db.execute("INSERT INTO studio_participants(project,root,models) VALUES(?,?,?) "
               "ON CONFLICT(project,root) DO UPDATE SET models=excluded.models",
               (project_key, root, json.dumps(models)))


def change(path, project, body):
    if not isinstance(body, dict) or type(body.get("root")) is not int:
        raise ValueError("Select a Studio model chat.")
    models, expected = selection(body.get("models")), selection(body.get("expected"))
    with closing(Store(str(path))) as store, store.db as db:
        db.execute("BEGIN IMMEDIATE")
        root = db.execute("SELECT * FROM messages WHERE project=? AND id=? AND reply_to IS NULL",
                          (project.key, body["root"])).fetchone()
        if root is None or root["sender"] != STUDIO_OPERATOR or not targets(root["recipient"]):
            raise ValueError("Select a Studio model chat.")
        validate_registered(db, project.key, models)
        previous = current(db, root)
        if previous != models and previous != expected:
            raise ValueError("Participants changed in another window. Choose again.")
        save(db, project.key, root["id"], models)
    return models


def shared_turns(db, msg):
    """Snapshot this chat at question time. Exclude later sibling responses and other projects."""
    if not _exists(db):
        return None
    root = root_for(db, msg["project"], msg["id"])
    if not root or not db.execute("SELECT 1 FROM studio_participants WHERE project=? AND root=?",
                                  (msg["project"], root["id"])).fetchone():
        return None
    rows = db.execute("""WITH RECURSIVE chat(id) AS (
        SELECT ? UNION ALL SELECT m.id FROM messages m JOIN chat c ON m.reply_to=c.id
        WHERE m.project=? AND m.id>c.id AND m.id<=?)
        SELECT m.* FROM messages m JOIN chat c ON m.id=c.id
        WHERE (sender=? AND sender_route=?) OR recipient_route=? ORDER BY m.id DESC LIMIT 16""",
        (root["id"], msg["project"], msg["id"], STUDIO_OPERATOR, msg["sender_route"], msg["sender_route"]))
    names = entries(db, msg["project"])
    turns = []
    for row in rows:
        if row["sender"] == STUDIO_OPERATOR and targets(row["recipient"]):
            turns.append(dict(role="user", content=row["body"]))
        elif model_id(row["sender"]):
            turns.append(dict(role="assistant", name=names.get(row["sender"], {}).get("name", row["sender"][6:]), content=row["body"]))
    return list(reversed(turns))
