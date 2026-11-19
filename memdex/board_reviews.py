"""Task-scoped review comparisons. Each attempt uses an ordinary Memdex review worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from . import project as projects, review, scan, skills
from .board import read_db


PROVIDERS = ("claude", "qwen", "deepseek")
ACTOR = "workspace"


def initialize(db):
    review.initialize(db)
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS board_review_runs (id INTEGER PRIMARY KEY, "
                   "project TEXT NOT NULL, root TEXT NOT NULL, task_id INTEGER NOT NULL, token TEXT NOT NULL, "
                   "spec TEXT NOT NULL, task_context TEXT NOT NULL, skill_name TEXT NOT NULL, digest TEXT NOT NULL, "
                   "origin TEXT NOT NULL, created REAL NOT NULL, UNIQUE(project,token))")
        db.execute("CREATE TABLE IF NOT EXISTS board_review_attempts (job INTEGER PRIMARY KEY, "
                   "run INTEGER NOT NULL, provider TEXT NOT NULL, attempt INTEGER NOT NULL, evidence TEXT, "
                   "UNIQUE(run,provider,attempt))")


def _has(db, table):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                         (table,)).fetchone() is not None


def options(path, project):
    catalog = []
    with read_db(path) as db:
        if _has(db, "skills") and _has(db, "skill_approvals"):
            catalog = [dict(row) for row in db.execute(
                "SELECT s.id,s.name,s.agent,s.sha,EXISTS(SELECT 1 FROM skill_approvals a WHERE a.name=s.name "
                "AND a.sha=s.sha) AS approved FROM skills s ORDER BY approved DESC,s.name,s.agent")]
    return {"files": scan.list_files(project), "skills": catalog}


def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("A positive numeric ID is required.")
    return value


def _open_task(db, project, task_id):
    task = db.execute("SELECT title,criterion,status FROM tasks WHERE project=? AND id=?",
                      (project.key, _id(task_id))).fetchone()
    if task is None or task["status"] in ("verified", "self_verified"):
        raise ValueError("Select an open task in this project.")
    return task


def _input(store, project, spec, task_context):
    if not isinstance(spec["files"], list) or not 1 <= len(spec["files"]) <= 20:
        raise ValueError("Select 1–20 project files.")
    paths = set(scan.list_files(project))
    if any(not isinstance(f, str) or f not in paths for f in spec["files"]):
        raise ValueError("Select files from this checkout's project file list.")
    payload = json.loads(review.snapshot(project, spec["question"], spec["files"], spec["task"]))
    payload["task"] = task_context
    name = ""
    if spec["skill"] is not None:
        skills.ensure(store.db)
        row = store.db.execute("SELECT agent,name FROM skills WHERE id=?", (_id(spec["skill"]),)).fetchone()
        got = skills.show(store, row["name"], row["agent"], body_limit=review.MAX_BYTES) if row else None
        if not got or not got["approved"] or got["row"]["sha"] != spec["skill_sha"]:
            raise ValueError("Skill approval or version changed. Inspect and approve the current bundle in Memdex.")
        if got["truncated"]:
            raise ValueError("The selected SKILL.md exceeds the review input limit.")
        name = row["name"]
        payload["review_skill"] = {"name": name, "sha": spec["skill_sha"], "instructions": got["body"]}
    encoded = json.dumps(payload, ensure_ascii=False)
    raw = encoded.encode("utf-8")
    if len(raw) > review.MAX_BYTES:
        raise ValueError("Combined review input exceeds 200000 bytes. Select fewer files.")
    return encoded, hashlib.sha256(raw).hexdigest(), name


def _reserve(db, project, run, provider, attempt, payload):
    spec = json.loads(run["spec"])
    model = review.providers.selection(provider, "")
    job = review.enqueue(db, project, ACTOR, run["origin"], run["task_id"], spec["question"],
                         f"board:{run['id']}:{provider}:{attempt}", run["digest"], payload, provider, model)
    db.execute("INSERT INTO board_review_attempts(job,run,provider,attempt) VALUES(?,?,?,?)",
               (job, run["id"], provider, attempt))
    return job


def start(store, project, body):
    token = body.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", token):
        raise ValueError("Missing review request ID.")
    spec = {k: body.get(k) for k in ("task", "question", "files", "skill", "skill_sha")}
    serialized = json.dumps(spec, ensure_ascii=False)
    initialize(store.db)

    def existing():
        row = store.db.execute("SELECT id,spec,root FROM board_review_runs WHERE project=? AND token=?",
                               (project.key, token)).fetchone()
        if row and (row["spec"] != serialized or row["root"] != project.root):
            raise ValueError("Request ID already belongs to another review.")
        return row

    previous = existing()
    if previous:
        return {"id": previous["id"]}
    task = _open_task(store.db, project, spec["task"])
    context = {"title": task["title"], "criterion": task["criterion"]}
    payload, digest, name = _input(store, project, spec, context)
    origin = "route:" + hashlib.sha256(json.dumps([project.root, "workspace-review", token]).encode()).hexdigest()[:32]
    store.register_route(project, origin, ACTOR, "QA · " + task["title"], task_id=spec["task"])
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        previous = existing()
        if previous:
            return {"id": previous["id"]}
        _open_task(store.db, project, spec["task"])
        rid = store.db.execute("INSERT INTO board_review_runs(project,root,task_id,token,spec,task_context,"
                               "skill_name,digest,origin,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (project.key, project.root, spec["task"], token, serialized, json.dumps(context),
                                name, digest, origin, time.time())).lastrowid
        run = _run(store.db, project, rid)
        jobs = [_reserve(store.db, project, run, provider, 1, payload) for provider in PROVIDERS]
    for job in jobs:
        review.start_worker(store, job)
    return {"id": rid}


def _run(db, project, rid):
    run = db.execute("SELECT * FROM board_review_runs WHERE project=? AND id=?", (project.key, _id(rid))).fetchone()
    if not run:
        raise ValueError("Review not found in this project.")
    return run


def _attempts(db, run):
    return db.execute("SELECT j.id,j.provider,j.model,j.state,j.started_at,j.result,j.reply_id,a.attempt,a.evidence "
                      "FROM board_review_attempts a JOIN review_jobs j ON j.id=a.job WHERE a.run=? AND "
                      "j.project=? AND j.origin=? ORDER BY j.id", (run["id"], run["project"], run["origin"]))


def _stale(job):
    return job["state"] in ("running", "queued") and time.time() - job["started_at"] > review.TIMEOUT + 120


def _detail(db, run):
    spec = json.loads(run["spec"])
    attempts = []
    for row in _attempts(db, run):
        job = dict(row)
        if _stale(job):
            job.update(state="interrupted", result="No completion was received within 240 seconds.")
        attempts.append(job)
    return {"id": run["id"], "task": run["task_id"], "question": spec["question"], "files": spec["files"],
            "skill": run["skill_name"], "created": run["created"], "attempts": attempts}


def history(path, project, task=None, *, full=False):
    with read_db(path) as db:
        if not _has(db, "board_review_runs"):
            return []
        query, args = "SELECT * FROM board_review_runs WHERE project=?", [project.key]
        if task is not None:
            query += " AND task_id=?"
            args.append(_id(task))
        runs = db.execute(query + " ORDER BY id DESC", args)
        if full:
            return [_detail(db, run) for run in runs]
        return [{"id": r["id"], "question": json.loads(r["spec"])["question"], "created": r["created"]} for r in runs]


def detail(path, project, rid):
    with read_db(path) as db:
        if not _has(db, "board_review_runs"):
            raise ValueError("Review not found in this project.")
        return _detail(db, _run(db, project, rid))


def retry(store, project, body):
    initialize(store.db)
    run = _run(store.db, project, body.get("run"))
    old = _id(body.get("job"))
    jobs = list(_attempts(store.db, run))
    job = next((j for j in jobs if j["id"] == old), None)
    if not job:
        raise ValueError("Attempt not found for this review.")
    if any(j["provider"] == job["provider"] and j["attempt"] > job["attempt"] for j in jobs):
        return {"id": run["id"]}
    if _stale(job):
        review.finish(store, old, "interrupted", "The worker did not confirm completion within 240 seconds.")
    elif job["state"] not in ("failed", "interrupted"):
        raise ValueError("Only a failed or interrupted reviewer can be retried.")
    checkout = projects.resolve(run["root"])
    if not Path(run["root"]).is_dir() or checkout.root != run["root"] or checkout.key != project.key:
        raise ValueError("The original review checkout is unavailable.")
    payload, digest, _ = _input(store, checkout, json.loads(run["spec"]), json.loads(run["task_context"]))
    if digest != run["digest"]:
        raise ValueError("Review inputs have changed. Start a new comparison to use the updated files.")
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        _open_task(store.db, project, run["task_id"])
        latest = next(j for j in reversed(list(_attempts(store.db, run))) if j["provider"] == job["provider"])
        if latest["id"] != old:
            return {"id": run["id"]}
        if latest["state"] not in ("failed", "interrupted"):
            raise ValueError("The reviewer has already finished.")
        new_job = _reserve(store.db, checkout, run, job["provider"], latest["attempt"] + 1, payload)
    review.start_worker(store, new_job)
    return {"id": run["id"]}


def evidence(store, project, body):
    initialize(store.db)
    rid, jid, note = _id(body.get("run")), _id(body.get("job")), body.get("note")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
        raise ValueError("Record the applied change and its test result (1–2000 characters).")
    note = note.strip()
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        run = _run(store.db, project, rid)
        job = next((j for j in _attempts(store.db, run) if j["id"] == jid), None)
        if not job or job["state"] not in ("completed", "truncated"):
            raise ValueError("Select a reviewer that returned an answer.")
        if job["evidence"] is None:
            store.db.execute("UPDATE board_review_attempts SET evidence=? WHERE job=?", (note, jid))
            store.db.execute("INSERT INTO task_events(project,task_id,actor,action,detail,created_at) "
                             "VALUES(?,?,?,'review_evidence',?,?)", (project.key, run["task_id"], ACTOR,
                             f"{job['provider']} · reply #{job['reply_id']}\n{note}", time.time()))
        elif job["evidence"] != note:
            raise ValueError("Implementation evidence was already recorded for this answer.")
    return {"id": rid}
