"""Electron's private stdio connection to the existing Memdex board."""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import sys

from . import project as projects
from .board import read_db
from .board_web import read_endpoint, write_endpoint
from .desktop import _last_folder, _remember
from .store import default_path
from .studio_chats import LocalChats
from . import studio_auto, studio_messages, studio_models, studio_participants


def project_groups(path, current=None):
    """Aggregate recorded identities. No heuristics based on labels, senders or message text."""
    groups = {}

    def group(key):
        if key not in groups:
            groups[key] = dict(key=key, name=Path(key).name or key, messages=0, tasks=0,
                               seen=0, checkouts=[])
        return groups[key]

    with read_db(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} if db else set()
        for table, stamp in (("messages", "created_at"), ("tasks", "updated_at"), ("sessions", "last_seen_at")):
            if table in tables:
                for key, count, seen in db.execute(
                    f"SELECT project,COUNT(*),MAX({stamp}) FROM {table} WHERE project <> '' GROUP BY project"
                ):
                    row = group(key)
                    if table != "sessions":
                        row[table] = count
                    row["seen"] = max(row["seen"], seen or 0)
        if "chat_routes" in tables:
            for key, checkout, seen in db.execute(
                "SELECT project,checkout,MAX(last_seen_at) FROM chat_routes WHERE project <> '' "
                "GROUP BY project,checkout ORDER BY MAX(last_seen_at) DESC"
            ):
                row = group(key)
                if checkout:
                    row["checkouts"].append(checkout)
                row["seen"] = max(row["seen"], seen or 0)
    if current:
        row = group(current.key)
        if current.root:
            row["checkouts"].insert(0, current.root)
    for row in groups.values():
        roots = [row["key"], *row["checkouts"]]
        row["checkouts"] = list({os.path.normcase(p): p for p in roots}.values())
    return sorted(groups.values(), key=lambda row: (-row["seen"], row["key"]))


class Studio:
    def __init__(self, db, limit):
        self.db = db
        self.limit = limit
        self.settings = Path(default_path()).with_name("studio-project.txt")
        self.project = None
        self.live = False
        self.token = ""
        self.chats = LocalChats(memdex_db=db)

    def select(self, project, live):
        self.project, self.live = project, live
        self.chats.cursors.clear()
        self.token = secrets.token_hex(24)
        if live:
            _remember(self.settings, project.root)
        return dict(token=self.token, key=project.key if project else "", name=project.name if project else "",
                    folder=project.root if live else None)

    def groups(self):
        rows = {r["key"]: r for r in project_groups(self.db, self.project)}
        for key, counts in self.chats.groups().items():
            row = rows.setdefault(key, dict(key=key, name=Path(key).name or key, messages=0, tasks=0,
                                            seen=0, checkouts=[key]))
            row.update(codex=counts["codex"], claude=counts["claude"])
            row["seen"] = max(row["seen"], counts["seen"])
        return sorted(rows.values(), key=lambda row: (-row["seen"], row["key"]))

    def open_group(self, key, checkout=None):
        row = next((g for g in self.groups() if g["key"] == key), None)
        if not row or (checkout is not None and checkout not in row["checkouts"]):
            raise ValueError("Unknown project or checkout.")
        for folder in ([checkout] if checkout else row["checkouts"]):
            if Path(folder).is_absolute() and Path(folder).is_dir():
                project = projects.resolve(folder)
                if project.key == key:
                    return self.select(project, True)
        if checkout:
            raise ValueError("This checkout is unavailable or belongs to a different project.")
        return self.select(projects.Project("", key, row["name"], False), False)

    def dispatch(self, method, args):
        if method == "initial":
            folder = args.get("folder") or _last_folder(self.settings)
            if folder:
                return self.dispatch("folder", {"folder": folder})
            rows = self.groups()
            return self.open_group(rows[0]["key"]) if rows else self.select(None, False)
        if method == "folder":
            if not Path(args["folder"]).is_dir():
                raise ValueError("Project folder is unavailable.")
            return self.select(projects.resolve(args["folder"]), True)
        if method == "groups":
            return self.groups()
        if args.get("token") != self.token or not self.token:
            raise ValueError("This project view is closed.")
        if method == "select":
            return self.open_group(args["key"], args.get("checkout"))
        if method == "read":
            endpoint = args["endpoint"]
            if endpoint == "message/recipients":
                if not self.live:
                    return []
                models = studio_models.listing(self.db, self.project)
                return studio_auto.directory(models) + [r for r in studio_messages.recipients(self.db, self.project)
                                                  if not studio_participants.model_id(r["id"])]
            project_key = self.project.key if self.project else ""
            if endpoint in ("ide/chats", "ide/refresh"):
                if endpoint == "ide/refresh":
                    self.chats.refresh(force=True)
                return self.chats.listing(project_key)
            if endpoint.startswith("ide/chat/"):
                match = re.fullmatch(r"ide/chat/((?:codex|claude):[0-9a-fA-F-]{36})/([0-9]{1,15})", endpoint)
                if not match:
                    raise ValueError("Unknown chat.")
                return self.chats.page(project_key, match[1], int(match[2]))
            if not self.project and args["endpoint"] == "state":
                return dict(project="", tasks=[], sessions=[], messages=[], more=False)
            if not self.live and args["endpoint"] == "qa/options":
                return dict(files=[], skills=[])
            if self.project:
                result = read_endpoint(self.db, self.project, self.limit, endpoint)
                if endpoint == "state" or endpoint.startswith(("thread/", "history/")):
                    studio_auto.annotate(self.db, result["messages"])
                    studio_messages.group_chats(self.db, self.project, result["messages"])
                    if endpoint == "state":
                        result["customModels"] = {k: v["name"] for k,v in studio_models.listing(self.db, self.project).items()}
                        result["participants"] = studio_participants.catalog(self.db, self.project)
                        result["projectKey"] = self.project.key
                return result
        if method in ("terminalFolder", "write") and self.live:
            if not Path(self.project.root).is_dir() or projects.resolve(self.project.root).key != self.project.key:
                raise ValueError("The checkout changed. Reopen the project folder.")
            if method == "terminalFolder":
                return self.project.root
            if args["action"] == "message":
                return studio_messages.send(self.db, self.project, args["body"])
            if args["action"] == "model/add":
                return studio_models.register(self.db, self.project, args["body"])
            if args["action"] == "message/participants":
                return studio_participants.change(self.db, self.project, args["body"])
            if args["action"] == "message/retry":
                return studio_auto.retry(self.db, self.project, args["body"])
            return write_endpoint(self.db, self.project, args["action"], args["body"])
        raise ValueError("Open a local project folder to use this action.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=default_path())
    parser.add_argument("--limit", type=int, default=25, choices=range(1, 101))
    opts = parser.parse_args()
    studio = Studio(opts.db, opts.limit)
    for line in sys.stdin.buffer:
        rid = None
        try:
            request = json.loads(line)
            rid = request["id"]
            result = studio.dispatch(request["method"], request["args"])
            reply = {"id": rid, "value": result}
        except Exception as exc:
            reply = {"id": rid, "error": str(exc)}
        print(json.dumps(reply, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
