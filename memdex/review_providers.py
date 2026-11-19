"""Local review transports. Authentication stays with the installed agent or Ollama."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .assigned_wake import child_env

PROVIDERS = ("claude", "deepseek", "qwen", "ollama")
LABELS = {"claude": "Claude", "deepseek": "DeepSeek", "qwen": "Qwen", "ollama": "Ollama"}
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_CONTEXT = 16384
OLLAMA_INPUT_BYTES = 12000


def selection(provider, model):
    if provider not in PROVIDERS:
        raise ValueError("provider must be claude, deepseek, qwen or ollama")
    if not isinstance(model, str) or len(model) > 120 or any(c.isspace() for c in model):
        raise ValueError("model must be a name without whitespace, at most 120 characters")
    if provider == "claude" and model:
        raise ValueError("Claude uses the existing CLI model selection; omit model")
    if provider == "ollama" and not model:
        raise ValueError("ollama requires an installed model name")
    return model or {"claude": "", "deepseek": "deepseek-flash", "qwen": "qwen2.5"}.get(provider, "")


def deepseek_command():
    """Prefer the installed Windows host's direct CLI entry, avoiding cmd.exe children."""
    # Packaged Windows apps can redirect LOCALAPPDATA while programs stay in the user profile.
    bases = [Path.home() / "AppData" / "Local"]
    if os.environ.get("LOCALAPPDATA"):
        bases.insert(0, Path(os.environ["LOCALAPPDATA"]))
    for base in dict.fromkeys(bases):
        host = base / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"
        archive = host.parent / "resources" / "app.asar"
        if host.is_file() and archive.is_file():
            entry = archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
            return [str(host), "--expose-internals", str(entry)], {"ELECTRON_RUN_AS_NODE": "1"}
    binary = shutil.which("dsh")
    if binary and not binary.lower().endswith((".cmd", ".bat")):
        return [binary], {}
    raise ValueError("DeepSeek Harness CLI is unavailable; use its installed desktop host or dsh executable")


def deepseek_overlay(config, model):
    # --dump-config is canonical YAML with top-level id/name rows. Never emit its contents:
    # user overrides can contain credentials. Refuse unfamiliar shapes before model work.
    rows = re.findall(r"^- id: ([\w-]+)\r?\n  name: ['\"]?([^'\"\r\n]+)['\"]?", config, re.M)
    ids = {id_ for id_, _ in rows}
    required = {"tools", "agent-loop", "headless-runner", "credentials", "authorization",
                "deepseek-account", "agent-default-model", "session-persistence-jsonl"}
    if not required <= ids:
        raise ValueError("Unsupported DeepSeek headless profile; required review controls are missing")
    disabled = {"agent-instructions", "skill-filesystem", "plan-mode", "user-questions",
                "session-persistence-jsonl", "session-query-sqlite", "session-title-llm", "session-telemetry-otel",
                "session-log-deepseek", "session-checkpoint-policy", "agent-team", "mcp-resources"}
    overlay = [{"id": id_, "disabled": False}
               for id_ in ("credentials", "authorization", "deepseek-account")]
    for id_, name in rows:
        tool = (id_.startswith("tool-") and id_ not in ("tool-executor-native", "tool-executor-ptc"))
        if tool or "dsh-mcp-client" in name or id_ in disabled:
            overlay.append({"id": id_, "disabled": True})
    overlay.append({"id": "agent-default-model", "config": {"provider": "deepseek-account", "model": model}})
    return overlay


def run_deepseek(root, payload, model, system, timeout):
    try:
        command, extra_env = deepseek_command()
        env = child_env("deepseek-review", root)
        env.update(extra_env)
        command += ["--profile", "headless"]
        config = subprocess.run(command + ["--dump-config"], cwd=root, env=env, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if config.returncode:
            return "failed", "DeepSeek could not load its headless profile."
        overlay = deepseek_overlay(config.stdout, model)
        with tempfile.TemporaryDirectory(prefix="memdex-review-") as temporary:
            path = Path(temporary) / "review.json"
            path.write_text(json.dumps(overlay), encoding="utf-8")
            proc = subprocess.run(command + ["--patch", str(path), "--json", "--permission-mode", "read-only",
                                             "--approval-policy", "never", "-"],
                                  cwd=root, env=env, input=system + "\n\n" + payload,
                                  encoding="utf-8", errors="replace", capture_output=True, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_deepseek(proc)
    except subprocess.TimeoutExpired:
        return "failed", "DeepSeek review timed out."
    except ValueError as exc:
        return "failed", str(exc)
    except OSError as exc:
        return "failed", f"DeepSeek review transport unavailable ({type(exc).__name__}). Check headless CLI compatibility."


def parse_deepseek(proc):
    final = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # Loader diagnostics are not answers.
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_call":
            return "failed", "DeepSeek exposed a tool call in the review profile; review result rejected."
        if event.get("type") == "final":
            final = event
    if not isinstance(final, dict):
        return "failed", "DeepSeek returned no final event. Check its headless profile."
    reason = final.get("reason")
    if not isinstance(reason, dict):
        return "failed", "DeepSeek returned no terminal completion reason."
    if proc.returncode or reason.get("kind") != "completed":
        error = reason.get("error")
        code = error.get("code", "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
            code = "UNKNOWN"
        return "failed", f"DeepSeek returned {code}. Check the existing Harness login and account usage limits."
    return answer(final.get("text"))


def local_json(path, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def check_ollama(model, prompt):
    # Byte count conservatively bounds tokenizer input, leaving room for chat formatting and output.
    if len(prompt.encode("utf-8")) > OLLAMA_INPUT_BYTES:
        raise ValueError("local review input exceeds 12000 UTF-8 bytes; select smaller source/doc files")
    try:
        tags = local_json("/api/tags")
    except (OSError, ValueError):
        raise ValueError("Ollama is unavailable at localhost:11434; start the installed local service") from None
    if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
        raise ValueError("Ollama returned an invalid model catalog")
    names = {item.get("name") for item in tags["models"] if isinstance(item, dict)}
    if model not in names and model + ":latest" not in names:
        raise ValueError(f"Ollama model {model!r} is not installed; choose an installed model")


def run_ollama(payload, model, system, timeout):
    try:
        prompt = system + "\n\n" + payload
        check_ollama(model, prompt)
        event = local_json("/api/generate", {"model": model, "system": system, "prompt": payload,
                           "stream": False, "think": False, "keep_alive": "1m",
                           "options": {"temperature": 0, "num_ctx": OLLAMA_CONTEXT, "num_predict": 2048}}, timeout)
        if not isinstance(event, dict) or event.get("done") is not True:
            return "failed", "Ollama did not confirm completion."
        return answer(event.get("response"), event.get("done_reason") == "length")
    except HTTPError as exc:
        return "failed", f"Ollama returned HTTP {exc.code}; check model availability and server logs."
    except TimeoutError:
        return "failed", "Ollama review timed out."
    except ValueError as exc:
        return "failed", str(exc)
    except OSError:
        return "failed", "Ollama connection failed; check the local service."


def answer(text, truncated=False):
    if not isinstance(text, str) or not text.strip():
        return "failed", "The reviewer returned no answer text."
    if truncated or len(text) > 24000:
        return "truncated", text[:24000].strip() + "\n[Review truncated at the output limit.]"
    return "completed", text.strip()
