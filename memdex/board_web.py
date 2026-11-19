"""Loopback browser workspace. Review actions require a same-origin JSON request."""
import base64
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import webbrowser

from .board import message_content, snapshot
from . import board_reviews
from .store import Store


ASSETS = {"": ("index.html", "text/html"), "workspace.js": ("workspace.js", "text/javascript"),
          "reviews.js": ("reviews.js", "text/javascript"),
          "workspace.css": ("workspace.css", "text/css")}
ASSET_DIR = Path(__file__).with_name("board_assets")


def _messages(data, full=False):
    result = []
    for row in data.messages:
        _, body, context = message_content(row["body"] or "")
        sender = row.get("sender_route") or row["sender"]
        recipient = row.get("recipient_route") or row["recipient"]
        message = {"id": row["id"], "time": row["created_at"], "from": sender, "to": recipient,
                   "reply": row.get("reply_to"), "task": data.message_tasks.get(row["id"]),
                   "body": body if full else " ".join(body.split())[:240]}
        if full:
            message["context"] = context
        result.append(message)
    return result


def _explorer(data, project):
    sessions = []
    routed = set()
    for route, row in data.routes.items():
        routed.add(row["agent"])
        sessions.append({"key": route, "agent": row["agent"], "checkout": Path(row["checkout"]).name,
                         "label": row["label"], "task": row.get("task_id"), "seen": row["last_seen_at"]})
    for row in data.sessions:
        if row["agent"] not in routed:
            sessions.append({"key": row["agent"], "agent": row["agent"], "checkout": "",
                             "label": "", "task": None, "seen": row["last_seen_at"]})
    return {"project": project.name, "tasks": [dict(t) for t in data.tasks],
            "sessions": sorted(sessions, key=lambda s: -(s["seen"] or 0))}


def page_data(path, project, limit, before=None):
    data = snapshot(path, project, limit + 1, before=before, task_links=True)
    page = _explorer(data, project) if before is None else {}
    page["more"] = len(data.messages) > limit
    data.messages = data.messages[:limit]
    page["messages"] = _messages(data)
    return page


def inline_page(bootstrap):
    script = "\n".join((ASSET_DIR / name).read_text(encoding="utf-8") for name in ("workspace.js", "reviews.js"))
    css = (ASSET_DIR / "workspace.css").read_text(encoding="utf-8")
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    policy = f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; base-uri 'none'"
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    page = page.replace('<link rel="stylesheet" href="workspace.css">',
                        f'<meta http-equiv="Content-Security-Policy" content="{policy}"><style>{css}</style>')
    page = page.replace('<script src="workspace.js" defer></script>', "")
    page = page.replace('<script src="reviews.js" defer></script>', "")
    return page.replace("</body>", bootstrap + "<script>" + script + "</script></body>")


def export_html(path, project, limit, filename):
    data = snapshot(path, project, -1, task_links=True)
    archive = dict(_explorer(data, project), messages=_messages(data, full=True), pageSize=limit,
                   reviews=board_reviews.history(path, project, full=True))
    payload = json.dumps(archive, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    page = inline_page('<script type="application/json" id="archive">' + payload + "</script>")
    target = Path(filename).expanduser().absolute()
    with open(target, "x", encoding="utf-8", newline="\n",
              opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(page)
    print(target)


def read_endpoint(path, project, limit, endpoint):
    if endpoint == "state":
        return page_data(path, project, limit)
    if endpoint == "qa/options":
        return board_reviews.options(path, project)
    match = re.fullmatch(r"(history|thread|qa/history|qa/run)/([1-9][0-9]{0,18})", endpoint)
    if not match or int(match[2]) >= 2**63:
        raise LookupError("Not found")
    item = int(match[2])
    if match[1].startswith("qa/"):
        try:
            if match[1] == "qa/history":
                return board_reviews.history(path, project, item)
            return board_reviews.detail(path, project, item)
        except ValueError as exc:
            raise LookupError("Review not found") from exc
    if match[1] == "history":
        return page_data(path, project, limit, before=item)
    data = snapshot(path, project, limit, item, thread=True, task_links=True)
    if not data.messages:
        raise LookupError("Message not found")
    return {"messages": _messages(data, full=True)}


def _create_task(store, project, body):
    if not all(isinstance(body.get(k), str) for k in ("title", "criterion")):
        raise ValueError("Enter a title and acceptance criterion.")
    return {"id": store.create_task(project, "workspace", body["title"], body["criterion"])}


def write_endpoint(path, project, action, body):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    actions = {"start": board_reviews.start, "retry": board_reviews.retry,
               "evidence": board_reviews.evidence, "task": _create_task}
    if action not in actions:
        raise ValueError("Unknown review action.")
    with closing(Store(str(path))) as store:
        return actions[action](store, project, body)


def handler_for(path, project, limit, prefix):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # A request path contains the private workspace URL.

        def send_body(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or not self.path.startswith(prefix):
                return self.send_body(404, b"Not found", "text/plain")
            endpoint = self.path[len(prefix):]
            try:
                if endpoint in ASSETS:
                    name, mime = ASSETS[endpoint]
                    asset = ASSET_DIR / name
                    return self.send_body(200, asset.read_bytes(), mime)
                if not endpoint.startswith("api/"):
                    return self.send_body(404, b"Not found", "text/plain")
                value = read_endpoint(path, project, limit, endpoint[4:])
                self.send_body(200, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json")
            except LookupError as exc:
                self.send_body(404, str(exc).encode(), "text/plain")
            except (OSError, sqlite3.Error, ValueError):
                self.send_body(503, b"Project board temporarily unavailable", "text/plain")

        def do_POST(self):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or not self.path.startswith(prefix):
                return self.send_body(404, b"Not found", "text/plain")
            action = re.fullmatch(r"api/qa/(start|retry|evidence|task)", self.path[len(prefix):])
            if not action:
                return self.send_body(405, b"Read-only endpoint", "text/plain")
            if self.headers.get("Origin") != "http://" + host or self.headers.get_content_type() != "application/json":
                return self.send_body(403, b"Same-origin JSON request required", "text/plain")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < size <= 100000:
                    return self.send_body(413, b"Invalid request size", "text/plain")
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                result = write_endpoint(path, project, action[1], body)
                self.send_body(200, json.dumps(result).encode(), "application/json")
            except (ValueError, UnicodeError) as exc:
                self.send_body(400, str(exc).encode("utf-8", "replace"), "text/plain")
            except (OSError, sqlite3.Error):
                self.send_body(503, b"Workspace unavailable. Check the project and database.", "text/plain")

    return Handler


def serve(path, project, limit, no_open):
    prefix = "/" + secrets.token_urlsafe(24) + "/"
    with ThreadingHTTPServer(("127.0.0.1", 0), handler_for(path, project, limit, prefix)) as server:
        url = f"http://127.0.0.1:{server.server_port}{prefix}"
        print(f"Memdex workspace: {url}", flush=True)
        print("Press Ctrl+C to close the workspace server.", flush=True)
        if not no_open:
            webbrowser.open(url)
        server.serve_forever()
    return 0
