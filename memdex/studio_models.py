"""Project model registrations and additional chat transports for the desktop Studio."""
from contextlib import closing
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .assigned_wake import child_env
from .board import read_db
from .review_providers import answer
from .store import Store

MODELS = {"model:claude": "Sonnet", "model:codex": "Codex", "model:qwen": "qwen2.5:latest",
          "model:deepseek": "deepseek-flash"}
PROVIDERS = ("ollama", "codex", "claude", "deepseek", "compatible")


def custom_id(value):
    return isinstance(value, str) and re.fullmatch(r"model:custom-[0-9a-f]{20}", value) is not None


def entries(db, project_key):
    if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_custom_models'").fetchone():
        return {}
    return {row["address"]: dict(row) for row in db.execute(
        "SELECT address,name,provider,model,base FROM studio_custom_models WHERE project=? ORDER BY name,address",
        (project_key,))}


def listing(path, project):
    with read_db(path) as db:
        return entries(db, project.key)


def base_url(raw):
    if not isinstance(raw, str) or len(raw) > 1024:
        raise ValueError("Enter a valid API base URL.")
    raw = raw.strip().rstrip("/")
    uri = urlsplit(raw)
    try:
        local = uri.hostname == "localhost" or ipaddress.ip_address(uri.hostname or "").is_loopback
    except ValueError:
        local = False
    if (not uri.hostname or uri.username is not None or uri.password is not None or uri.query or uri.fragment
            or re.search(r"[\s\\\x00-\x1f]", raw) or (uri.scheme != "https" and not (uri.scheme == "http" and local))):
        raise ValueError("Use an HTTPS base URL, or HTTP on localhost, without credentials, query or fragment.")
    _ = uri.port  # Reject malformed ports before saving.
    return raw


def register(path, project, body):
    if not isinstance(body, dict) or body.get("provider") not in PROVIDERS:
        raise ValueError("Choose a supported connection.")
    provider = body["provider"]
    name, model = body.get("name"), body.get("model")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or not name.isprintable():
        raise ValueError("Enter a display name of 1 to 48 characters.")
    name = name.strip()
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,159}", model.strip()):
        raise ValueError("Enter a model ID without spaces, such as qwen2.5:latest.")
    model = model.strip()
    base = base_url(body.get("base")) if provider == "compatible" else ""
    if "model:" + name.casefold() in MODELS:
        raise ValueError("This display name already exists.")
    config = [name, provider, model, base]
    address = "model:custom-" + hashlib.sha256(json.dumps(config).encode()).hexdigest()[:20]
    with closing(Store(str(path))) as store, store.db as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_custom_models (
            project TEXT NOT NULL,address TEXT NOT NULL, name TEXT NOT NULL,name_key TEXT NOT NULL,
            provider TEXT NOT NULL,model TEXT NOT NULL,base TEXT NOT NULL,
            PRIMARY KEY(project,address), UNIQUE(project,name_key))""")
        existing = db.execute("SELECT address FROM studio_custom_models WHERE project=? AND name_key=?",
                              (project.key, name.casefold())).fetchone()
        if existing and existing[0] != address:
            raise ValueError("This display name already exists.")
        if not existing:
            db.execute("INSERT INTO studio_custom_models VALUES(?,?,?,?,?,?,?)",
                       (project.key, address, name, name.casefold(), provider, model, base))
    return dict(id=address, name=name)


def codex_command():
    binary = shutil.which("codex")
    if binary and not binary.lower().endswith((".cmd", ".bat", ".ps1")):
        return [binary]
    if binary:
        entry = Path(binary).parent / "node_modules/@openai/codex/bin/codex.js"
        if entry.is_file() and os.name == "nt":
            arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
            platform = {"AMD64": ("x64", "x86_64"), "ARM64": ("arm64", "aarch64")}.get(arch)
            if platform:
                package, target = platform
                vendor = entry.parents[1] / f"node_modules/@openai/codex-win32-{package}/vendor/{target}-pc-windows-msvc"
                for native in (vendor / "bin/codex.exe", vendor / "codex/codex.exe"):
                    if native.is_file():
                        return [str(native)]
        node = shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
    native = shutil.which("codex.exe")
    if native:
        return [native]
    raise ValueError("Codex CLI is unavailable. Install Codex CLI and sign in with codex login.")


def run_codex(root, payload, model, system, timeout):
    output = Path(root) / "reply.txt"
    command = codex_command() + ["exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                                 "--sandbox", "read-only", "-c", 'web_search="disabled"',
                                 "-c", "project_doc_max_bytes=0"]
    for feature in ("apps", "plugins", "hooks", "shell_tool", "unified_exec"):
        command.extend(["--disable", feature])
    if model:
        command.extend(["--model", model])
    try:
        result = subprocess.run(command + ["--output-last-message", str(output), "-"], cwd=root,
                                env=child_env("studio-codex", root), input=system + "\n\n" + payload,
                                encoding="utf-8", errors="replace", capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not output.is_file():
            if re.search(r"rate.limit|usage.limit|quota", result.stderr, re.I):
                return "failed", "Codex usage limit reached. Check the account quota and retry."
            return "failed", "Codex returned no answer. Check codex login, CLI version and model availability."
        return answer(output.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        return "failed", "Codex timed out."
    except OSError:
        return "failed", "Could not start Codex CLI or read its reply."


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run_compatible(config, payload, system, timeout):
    base = base_url(config["base"])
    headers = {"Content-Type": "application/json"}
    try:
        # Credentials are explicitly bound to an exact base URL on this machine.
        keys = json.loads(os.getenv("MEMDEX_STUDIO_API_KEYS") or "{}")
        if not isinstance(keys, dict):
            raise ValueError
        key = keys.get(base)
        if key:
            if not isinstance(key, str) or not key.isascii() or not key.isprintable():
                raise ValueError
            headers["Authorization"] = "Bearer " + key
    except ValueError:
        return "failed", "Check the local MEMDEX_STUDIO_API_KEYS JSON settings."
    data = {"model": config["model"], "stream": False, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": payload}]}
    req = Request(base + "/chat/completions", data=json.dumps(data).encode(), headers=headers)
    try:
        with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            return "failed", "The response exceeded the size limit."
        choice = json.loads(raw)["choices"][0]
        return answer(choice["message"]["content"], choice.get("finish_reason") == "length")
    except HTTPError as exc:
        return "failed", f"API returned HTTP {exc.code}. Check the base URL, model ID and credentials."
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "failed", "Could not read a Chat Completions reply from this API."
