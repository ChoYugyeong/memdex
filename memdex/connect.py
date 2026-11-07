"""Register memdex with the agents already installed on this machine.

`memdex config` prints snippets for a human to paste. This writes them instead: it finds each client's
config file and merges one entry — `mcpServers.memdex` for most JSON clients, `servers.memdex` with
`type: stdio` for VS Code, `context_servers.memdex` for Zed, `[mcp_servers.memdex]` for Codex's TOML,
and a managed `@deepseek-ai/dsh-mcp-client` plugin in DeepSeek Harness profile YAML — and backs the file
up. A client whose config directory does not exist is reported as not installed and
left alone, so `memdex connect` never scatters empty config folders for apps this machine does not have.
A config file that does not parse is never rewritten: an agent's settings are worth more than the
convenience of editing them. Zed's settings file allows comments, and a rewrite would drop them, so a
settings file that is not strict JSON gets the block to paste instead.

`--serve-check` closes the loop: after writing, it starts the server the entry describes and speaks MCP
to it (initialize, then tools/list), so a setup that looks right but cannot start is caught here instead
of in the agent.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import __version__

SERVER = "memdex"


@dataclass(frozen=True)
class Client:
    """One agent's config file.

    `container` is the key holding the server map (JSON clients disagree: `mcpServers` for most,
    `servers` for VS Code, `context_servers` for Zed) and `extra` is whatever else that client needs in
    every entry (VS Code wants `type: stdio`). Codex is the one TOML client, so its entry is rendered
    rather than merged. `jsonc` marks a file a human is likely to have commented, which memdex will not
    rewrite.
    """

    slug: str
    label: str
    path: str
    fmt: str = "json"
    container: str = "mcpServers"
    extra: Optional[dict] = None
    # "user" scope is this machine's config; "project" scope is a file inside the checkout that the team
    # commits, so it is written with a bare `memdex` command rather than this machine's absolute path.
    scope: str = "user"
    jsonc: bool = False


def _home() -> str:
    return os.path.expanduser("~")


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")


def _deepseek_profiles(home: Optional[str] = None) -> list:
    """Find existing DeepSeek Harness profiles without creating profile folders."""
    dsh_home = home or os.environ.get("DSH_HOME") or os.path.join(_home(), ".dsh")
    profiles = os.path.join(dsh_home, "profiles")
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        return []
    return [os.path.join(profiles, name) for name in names
            if os.path.isdir(os.path.join(profiles, name))]


def clients() -> list:
    """Every client memdex knows how to configure, in the order they are tried."""
    system = platform.system()
    if system == "Windows":
        desktop = os.path.join(_appdata(), "Claude", "claude_desktop_config.json")
        code_user = os.path.join(_appdata(), "Code", "User")
        zed = os.path.join(_appdata(), "Zed", "settings.json")
    elif system == "Darwin":
        desktop = os.path.join(_home(), "Library", "Application Support", "Claude",
                               "claude_desktop_config.json")
        code_user = os.path.join(_home(), "Library", "Application Support", "Code", "User")
        zed = os.path.join(_home(), "Library", "Application Support", "Zed", "settings.json")
    else:
        desktop = os.path.join(_home(), ".config", "Claude", "claude_desktop_config.json")
        code_user = os.path.join(_home(), ".config", "Code", "User")
        zed = os.path.join(_home(), ".config", "zed", "settings.json")
    found = [
        Client("claude-desktop", "Claude Desktop", desktop),
        Client("cursor", "Cursor", os.path.join(_home(), ".cursor", "mcp.json")),
        Client("vscode", "VS Code / Copilot", os.path.join(code_user, "mcp.json"),
               container="servers", extra={"type": "stdio"}),
        Client("windsurf", "Windsurf", os.path.join(_home(), ".codeium", "windsurf", "mcp_config.json")),
        Client("zed", "Zed", zed, container="context_servers", jsonc=True),
        Client("cline", "Cline", _cline_path(code_user)),
        Client("gemini", "Gemini CLI", os.path.join(_home(), ".gemini", "settings.json")),
        Client("codex", "Codex CLI", os.path.join(_home(), ".codex", "config.toml"), fmt="toml"),
        # ChatGPT Desktop manages MCP servers through its UI (Settings → MCP); the UI writes this file.
        # Until the user adds one server by hand, the file does not exist and this reports not installed.
        Client("chatgpt-desktop", "ChatGPT Desktop",
               os.path.join(_appdata(), "ChatGPT", "chatgpt_config.json")),
    ]
    found.extend(Client("deepseek-harness", f"DeepSeek Harness ({os.path.basename(profile)})",
                        os.path.join(profile, "cordis.patch.yml"), fmt="dsh-yaml")
                 for profile in _deepseek_profiles())
    return found


def _cline_path(code_user: str, home: Optional[str] = None) -> str:
    """Cline keeps its MCP settings under VS Code's per-extension storage, or `~/.cline` for its CLI."""
    cli = os.path.join(home or _home(), ".cline", "data", "settings", "cline_mcp_settings.json")
    if os.path.isdir(os.path.dirname(cli)):
        return cli
    return os.path.join(code_user, "globalStorage", "saoudrizwan.claude-dev", "settings",
                        "cline_mcp_settings.json")


def project_clients(root: str) -> list:
    """Configs that live in the checkout and are meant to be committed, so the team gets memdex too."""
    root = os.path.abspath(root)
    return [
        Client("vscode-workspace", "VS Code (.vscode/mcp.json)", os.path.join(root, ".vscode", "mcp.json"),
               container="servers", extra={"type": "stdio"}, scope="project"),
        Client("claude-project", "Claude Code (.mcp.json)", os.path.join(root, ".mcp.json"), scope="project"),
    ]


def build_entry(agent: str = "", absolute: bool = True) -> dict:
    """The server entry.

    Falls back to `python -m memdex` when the console script is not on PATH. `absolute=False` keeps the
    bare command, which is what a committed project config needs: the file is shared, this machine's path is
    not.
    """
    if not absolute:
        exe = SERVER
    else:
        exe = shutil.which(SERVER)
    if exe:
        command, args = exe, ["serve"]
    else:
        command, args = sys.executable, ["-m", "memdex", "serve"]
    entry = {"command": command, "args": args}
    if agent:
        entry["env"] = {"MEMDEX_AGENT": agent}
    return entry


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _toml_block(entry: dict, agent: str) -> str:
    env = entry.get("env") or {}
    env_toml = ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items())
    lines = [f"[mcp_servers.{SERVER}]",
             f"command = {json.dumps(entry['command'])}",
             f"args = {json.dumps(entry['args'])}"]
    if env_toml:
        lines.append(f"env = {{ {env_toml} }}")
    return "\n".join(lines) + "\n"


def merge_toml(path: str, entry: dict, agent: str = "", remove: bool = False) -> tuple:
    """Append (or cut) one `[mcp_servers.memdex]` table. The rest of the file is copied byte for byte."""
    marker = f"[mcp_servers.{SERVER}]"
    existing = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        except (OSError, UnicodeDecodeError) as e:
            # Same rule as the JSON path: a file that is not plain UTF-8 text is not ours to rewrite.
            return ("skipped", f"not readable text ({e})")
    has_table = any(line.strip() == marker for line in existing.splitlines())
    if remove:
        if not has_table:
            return ("unchanged", "no memdex table")
        kept, skipping = [], False
        for line in existing.splitlines(True):
            if line.strip() == marker:
                skipping = True
                continue
            if skipping and line.lstrip().startswith("["):
                skipping = False
            if not skipping:
                kept.append(line)
        _write_text(path, "".join(kept))
        return ("removed", "dropped the memdex table")
    if has_table:
        return ("unchanged", "already configured")
    block = _toml_block(entry, agent)
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    if body:
        body += "\n"
    _write_text(path, body + block)
    return ("updated", "appended [mcp_servers.memdex]")


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


_DSH_BEGIN = "# BEGIN MEMDEX MANAGED — memdex connect"
_DSH_END = "# END MEMDEX MANAGED — memdex connect"


def _dsh_block(entry: dict, agent: str = "") -> str:
    """Render the official DeepSeek Harness MCP-client plugin entry as a Cordis patch item."""
    who = agent or "deepseek"
    env = dict(entry.get("env") or {})
    env.setdefault("MEMDEX_AGENT", who)
    env_lines = "".join(
        f"          {key}: {json.dumps(str(value), ensure_ascii=False)}\n"
        for key, value in sorted(env.items())
    )
    return (f"{_DSH_BEGIN}\n"
            "- insert:\n"
            "    - id: memdex-memory\n"
            '      name: "@deepseek-ai/dsh-mcp-client"\n'
            "      config:\n"
            "        serverName: memdex\n"
            "        transport: stdio\n"
            f"        command: {json.dumps(entry['command'], ensure_ascii=False)}\n"
            f"        args: {json.dumps(entry.get('args') or [], ensure_ascii=False)}\n"
            "        env:\n"
            f"{env_lines}"
            "        cwd: !!js process.cwd()\n"
            "        failOnStartupError: true\n"
            f"{_DSH_END}\n")


def merge_dsh_yaml(path: str, entry: dict, agent: str = "", remove: bool = False) -> tuple:
    """Manage only the marked DSH profile patch block; refuse unknown YAML shapes or manual collisions.

    We deliberately avoid a YAML dependency and never rewrite a block the user owns. The recognized
    DSH format is a top-level list of Cordis patch entries. An exact marker pair makes updates and
    removal local to the block memdex created.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    except (OSError, UnicodeDecodeError) as e:
        return ("skipped", f"not readable UTF-8 YAML ({e})")

    begin_count, end_count = existing.count(_DSH_BEGIN), existing.count(_DSH_END)
    if begin_count != end_count or begin_count > 1:
        return ("skipped", "managed marker is incomplete or duplicated")
    if begin_count:
        start = existing.index(_DSH_BEGIN)
        end_start = existing.find(_DSH_END, start + len(_DSH_BEGIN))
        if end_start < 0:
            return ("skipped", "managed markers are out of order")
        finish = end_start + len(_DSH_END)
        end_with_newline = finish + (1 if existing[finish:finish + 1] == "\n" else 0)
        old_block = existing[start:end_with_newline]
        if remove:
            updated = existing[:start] + existing[end_with_newline:]
            _write_text(path, updated)
            return ("removed", "dropped the managed DeepSeek Harness entry")
        wanted = _dsh_block(entry, agent)
        if old_block == wanted:
            return ("unchanged", "already configured")
        _write_text(path, existing[:start] + wanted + existing[end_with_newline:])
        return ("replaced", "updated the managed DeepSeek Harness entry (backup has the previous version)")

    # Do not delete or overwrite a manually configured server. Give the user a clear path to reconcile it.
    if "id: memdex-memory" in existing:
        return ("skipped", "an unmanaged DSH entry uses id memdex-memory; review it before editing")
    if remove:
        return ("unchanged", "no managed DeepSeek Harness entry")

    first = next((line for line in existing.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
    if first and not first.startswith("-"):
        return ("skipped", "profile patch is not a top-level YAML list; left it unchanged")
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    if body and not body.endswith("\n\n"):
        body += "\n"
    _write_text(path, body + _dsh_block(entry, agent))
    return ("updated", "added the DeepSeek Harness MCP-client plugin entry")


def merge_json(path: str, entry: dict, container: str = "mcpServers", remove: bool = False) -> tuple:
    """Merge (or drop) one key in a JSON config. Returns (status, detail)."""
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            return ("skipped", f"not readable JSON ({e})")
        if not isinstance(data, dict):
            return ("skipped", "config is not a JSON object")
    servers = data.get(container, {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return ("skipped", f"{container} is not an object")
    current = servers.get(SERVER)
    if remove:
        if current is None:
            return ("unchanged", "no memdex entry")
        servers.pop(SERVER, None)
        data[container] = servers
        _write_json(path, data)
        return ("removed", "dropped the memdex entry")
    if current is not None and not _equivalent(current, entry):
        # Someone edited the entry by hand. Replace it, but say so: the backup has their version.
        servers[SERVER] = entry
        data[container] = servers
        _write_json(path, data)
        return ("replaced", "the existing entry differed (backup has it)")
    if current is not None:
        return ("unchanged", "already configured")
    servers[SERVER] = entry
    data[container] = servers
    _write_json(path, data)
    return ("updated", "added the memdex entry")


def _is_strict_json(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False


def snippet(client: Client, entry: dict) -> str:
    """The block to paste by hand, for a file memdex will not rewrite (Zed allows comments)."""
    if client.fmt == "dsh-yaml":
        return _dsh_block(entry, (entry.get("env") or {}).get("MEMDEX_AGENT", "deepseek"))
    return json.dumps({client.container: {SERVER: entry}}, indent=2)


def _send(proc, message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_reply(lines: "queue.Queue", timeout: float) -> tuple:
    """First JSON-RPC reply on stdout. A server may log lines before it; skip anything that is not JSON."""
    deadline = time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0:
            return (None, f"no answer within {timeout:.0f}s")
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            return (None, f"no answer within {timeout:.0f}s")
        if line is None:
            # The pump sends None when the server's stdout closes: it died instead of answering.
            return (None, "the server exited before answering")
        line = (line or "").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or not ("result" in message or "error" in message):
            continue
        if "error" in message:
            return (None, f"server error: {message['error']}")
        return (message, "")


def check_serve(entry: dict, cwd: str = "", timeout: float = 20.0) -> tuple:
    """Start the server the entry describes and speak MCP to it: initialize, then tools/list.

    This is the difference between "the config looks right" and "the agent will get tools": a wrong path,
    a missing interpreter or a database the server cannot open all show up here. Returns (ok, message).
    """
    argv = _launchable([entry["command"], *(entry.get("args") or [])])
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd or None, env=env,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"could not start {argv[0]}: {e}")
    lines: "queue.Queue" = queue.Queue()

    def pump():
        for line in proc.stdout or []:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "memdex connect", "version": __version__}}})
        reply, error = _read_reply(lines, timeout)
        if error:
            code = proc.poll()
            if code is None:
                try:
                    code = proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    code = None
            return (False, error + (f" (exit {code})" if code is not None else ""))
        result = reply.get("result") or {}
        name = ((result.get("serverInfo") or {}).get("name")) or "unknown"
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools, error = _read_reply(lines, timeout)
        if error:
            return (False, f"initialize answered, but tools/list did not: {error}")
        count = len(((tools.get("result") or {}).get("tools")) or [])
        return (True, f"{name} answered: {count} tools, protocol {result.get('protocolVersion', '?')}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for stream in (proc.stdout,):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


def _equivalent(a, b) -> bool:
    """Order-insensitive comparison, so a hand-written entry that means the same thing is left alone."""
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equivalent(x, y) for x, y in zip(a, b))
    return a == b


def entry_for(client: Client, entry: dict) -> dict:
    """The base entry plus whatever this client insists on (VS Code wants `type: stdio`)."""
    if client.fmt == "dsh-yaml":
        # A PATH `memdex` shim can point at a different installed release than the code that ran connect.
        # Use this package's interpreter and import root so the MCP tools match the package being configured.
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(entry.get("env") or {})
        # Two Harness profiles must not share one mailbox identity: if the caller did not name an
        # agent, derive it from the profile directory instead of collapsing every profile onto
        # "deepseek" (which made separately selected profiles read each other's mail).
        profile = _profile_name(client)
        env.setdefault("MEMDEX_AGENT", f"dsh-{profile}" if profile else "deepseek")
        env["PYTHONPATH"] = package_root
        return {"command": sys.executable, "args": ["-m", "memdex", "serve"], "env": env}
    if not client.extra:
        return entry
    return {**client.extra, **entry}


def inspect(client: Client, entry: dict, exact: bool = True) -> tuple:
    """(state, detail) without writing anything: configured, missing, different, unreadable, absent.

    `exact=False` only asks whether an entry is there at all — what `memdex doctor` wants, since it cannot
    know which agent name a different session used.
    """
    if not os.path.isdir(os.path.dirname(client.path)):
        return ("absent", "not installed")
    wanted = entry_for(client, entry)
    if client.fmt == "dsh-yaml":
        if not os.path.exists(client.path):
            return ("missing", "no managed DeepSeek Harness entry")
        try:
            with open(client.path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return ("unreadable", str(e))
        begin = text.find(_DSH_BEGIN)
        end = text.find(_DSH_END, begin + len(_DSH_BEGIN)) if begin >= 0 else -1
        if begin >= 0 and end >= 0:
            block = text[begin:end + len(_DSH_END)] + "\n"
            if exact and block != _dsh_block(wanted, (wanted.get("env") or {}).get("MEMDEX_AGENT", "deepseek")):
                return ("different", "managed entry differs (command, arguments or agent name)")
            return ("configured", "configured")
        if _DSH_BEGIN in text or _DSH_END in text:
            return ("different", "managed markers are incomplete or out of order")
        if "@deepseek-ai/dsh-mcp-client" in text and "serverName: memdex" in text:
            return ("configured", "configured manually")
        if "id: memdex-memory" in text:
            return ("different", "an unmanaged DSH entry uses id memdex-memory")
        return ("missing", "no memdex MCP plugin entry")
    if client.fmt == "toml":
        try:
            with open(client.path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return ("missing", "no memdex table")
        except (OSError, UnicodeDecodeError) as e:
            return ("unreadable", str(e))
        return ("configured" if f"[mcp_servers.{SERVER}]" in text else "missing", "toml")
    if not os.path.exists(client.path):
        return ("missing", "no memdex entry")
    if client.jsonc and not _is_strict_json(client.path):
        return ("manual", "settings file has comments; memdex will not rewrite it")
    try:
        with open(client.path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return ("unreadable", str(e))
    servers = data.get(client.container) if isinstance(data, dict) else None
    current = servers.get(SERVER) if isinstance(servers, dict) else None
    if current is None:
        return ("missing", "no memdex entry")
    if exact and not _equivalent(current, wanted):
        return ("different", "entry differs (args, command or agent name)")
    return ("configured", "configured")


def _profile_name(client: Client) -> str:
    return os.path.basename(os.path.dirname(client.path))


def _select_profile(targets: list, profile: str) -> tuple[list, Optional[str]]:
    """Keep non-DSH targets and select one named Harness profile, if requested."""
    dsh = [c for c in targets if c.fmt == "dsh-yaml"]
    if not profile:
        return targets, None
    if not dsh:
        return targets, "--profile requires DeepSeek Harness in the selected clients"
    matches = [c for c in dsh if _profile_name(c) == profile]
    if not matches:
        names = ", ".join(_profile_name(c) for c in dsh) or "none"
        return targets, f"unknown DeepSeek Harness profile {profile!r} (selected profiles: {names})"
    return [c for c in targets if c.fmt != "dsh-yaml"] + matches, None


def list_clients(agent: str = "", targets: Optional[list] = None, profile: str = "") -> int:
    """Print what is installed and whether it already carries the entry. Writes nothing."""
    chosen = targets if targets is not None else clients()
    if profile:
        chosen, error = _select_profile(chosen, profile)
        if error:
            print(f"! {error}")
            return 2
    entry = build_entry(agent)
    print(f"memdex connect · status · agent {agent or '(unset, the client name is used)'}")
    for client in chosen:
        state, detail = inspect(client, entry)
        mark = {"configured": "✓", "missing": "·", "different": "~", "unreadable": "!",
                "manual": "?", "absent": "·"}.get(state, "?")
        print(f"{mark} {client.slug:<16} {state:<11}" + (client.path if state != "absent" else detail))
        if state in ("different", "unreadable", "manual"):
            print(f"  {detail}")
    return 0


def summary(targets: Optional[list] = None) -> tuple:
    """(status, detail) for `memdex doctor`: which installed clients still need `memdex connect`."""
    entry = build_entry()
    installed, missing = [], []
    found = list(targets if targets is not None else clients())
    slugs = [c.slug for c in found]
    for client in found:
        state, _ = inspect(client, entry, exact=False)
        if state == "absent":
            continue
        # Several DeepSeek Harness profiles share one slug; "deepseek-harness, deepseek-harness" says
        # nothing about which one to fix, so a repeated slug is shown by its label instead.
        name = client.slug if slugs.count(client.slug) == 1 else client.label
        (installed if state == "configured" else missing).append(name)
    if missing:
        return ("warn", f"run memdex connect: {', '.join(missing)}")
    if installed:
        return ("ok", ", ".join(installed))
    return ("ok", "no known client config here (memdex config prints the snippet)")


def find_claude() -> Optional[str]:
    """The Claude Code CLI, when this machine has one. `memdex connect` offers to run it."""
    return shutil.which("claude")


def claude_command(exe: str, agent: str = "", remove: bool = False, scope: str = "user") -> list:
    """`claude mcp add|remove` argv, kept separate so it can be checked without running anything."""
    argv = [exe, "mcp", "remove" if remove else "add", SERVER, "--scope", scope]
    if not remove:
        argv += ["-e", f"MEMDEX_AGENT={agent or 'claude'}", "--", SERVER, "serve"]
    return argv


def _launchable(argv: list) -> list:
    """Windows ships `claude` as a .cmd shim, which CreateProcess cannot start on its own."""
    if os.name == "nt" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def run_claude(exe: str, agent: str = "", remove: bool = False, scope: str = "user",
               dry_run: bool = False, root: str = "") -> tuple:
    """Run `claude mcp add|remove`. Returns (ok, message); never raises."""
    argv = claude_command(exe, agent, remove, scope)
    shown = " ".join(argv[1:])
    if dry_run:
        return (True, f"would run: claude {shown}")
    try:
        proc = subprocess.run(_launchable(argv), cwd=root or None, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return (False, f"could not run claude ({e}) — run it yourself: claude {shown}")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return (True, output.splitlines()[0] if output else "done")
    return (False, f"claude exited {proc.returncode}: {output or 'no output'} "
                   f"— run it yourself: claude {shown}")


def run(agent: str = "", only: str = "", dry_run: bool = False, remove: bool = False,
        targets: Optional[list] = None, scope: str = "user", root: str = "",
        claude: Optional[str] = None, check: bool = False, profile: str = "") -> int:
    """Configure every detected client. Returns 0 when nothing was skipped for a bad reason.

    `claude` is the path to the Claude Code CLI when it should be run as well (None leaves it alone, so
    tests and `--no-claude` never touch the real thing). `scope="project"` writes the configs that live in
    the checkout and are meant to be committed. `check` starts the configured server and speaks MCP to it
    after writing.
    """
    in_project = scope == "project"
    chosen = targets if targets is not None else (project_clients(root) if in_project else clients())
    wanted = [w.strip() for w in (only or "").split(",") if w.strip()]
    if wanted and "all" not in wanted:
        known = {c.slug for c in chosen} | {"claude-code"}
        unknown = [w for w in wanted if w not in known]
        for slug in unknown:
            print(f"! {slug}: unknown client (known: {', '.join(sorted(known))})")
        chosen = [c for c in chosen if c.slug in wanted]
    selected_dsh = [c for c in chosen if c.fmt == "dsh-yaml"]
    if profile:
        chosen, error = _select_profile(chosen, profile)
        if error:
            print(f"! {error}. No files were changed.")
            return 2
    elif len(selected_dsh) > 1:
        names = ", ".join(_profile_name(c) for c in selected_dsh)
        print(f"! multiple DeepSeek Harness profiles are selected ({names}); pass --profile NAME. "
              "No files were changed.")
        return 2
    want_claude = bool(claude) and (not wanted or "all" in wanted or "claude-code" in wanted)
    if "claude-code" in wanted and not claude:
        print("· claude-code      claude CLI not on PATH; run the command below by hand")
    if in_project and want_claude and "claude-project" not in wanted:
        # `claude mcp add --scope project` writes .mcp.json itself; writing it here too would be two owners
        # for one file. Unless the file was named explicitly, the CLI gets it.
        chosen = [c for c in chosen if c.slug != "claude-project"]
    verb = "remove" if remove else "install"
    print(f"memdex connect · {verb} · {scope} scope · agent {agent or '(unset, the client name is used)'}")
    counts = {"updated": 0, "replaced": 0, "removed": 0, "unchanged": 0, "skipped": 0, "absent": 0,
              "manual": 0}
    for client in chosen:
        parent = os.path.dirname(client.path)
        if not os.path.isdir(parent) and client.scope != "project":
            print(f"· {client.slug:<16} not installed")
            counts["absent"] += 1
            continue
        if dry_run:
            print(f"? {client.slug:<16} would be {verb}ed in {client.path}")
            continue
        if not os.path.isdir(parent):
            # Project scope: the file is the deliverable, so its folder is created on purpose.
            os.makedirs(parent, exist_ok=True)
        wanted_entry = entry_for(client, build_entry(agent, absolute=client.scope != "project"))
        if client.jsonc and os.path.exists(client.path) and not _is_strict_json(client.path):
            counts["manual"] = counts.get("manual", 0) + 1
            print(f"? {client.slug:<16} manual: {client.path} has comments, so memdex will not rewrite it")
            print("  paste this block into it:")
            for line in snippet(client, wanted_entry).splitlines():
                print(f"    {line}")
            continue
        if client.fmt == "dsh-yaml":
            status, detail = merge_dsh_yaml(client.path, wanted_entry, agent, remove=remove)
        elif client.fmt == "toml":
            status, detail = merge_toml(client.path, wanted_entry, agent, remove=remove)
        else:
            status, detail = merge_json(client.path, wanted_entry, client.container, remove=remove)
        counts[status] = counts.get(status, 0) + 1
        mark = {"updated": "+", "replaced": "~", "removed": "-", "unchanged": "=", "skipped": "!",
                "manual": "?"}.get(status, "?")
        backup = f" (backup: {client.path}.bak)" if status in ("updated", "replaced", "removed") else ""
        print(f"{mark} {client.slug:<16} {status}: {client.path}{backup}")
        if detail:
            print(f"  {detail}" + ("; left exactly as it was" if status == "skipped" else ""))
    if want_claude:
        ok, message = run_claude(claude, agent, remove=remove, scope=scope, dry_run=dry_run, root=root)
        print(f"{'+' if ok else '!'} {'claude-code':<16} {message}")
        if ok:
            counts["updated" if not remove else "removed"] += 1
        else:
            counts["skipped"] += 1
    if dry_run:
        print("dry run: nothing written")
        return 0
    if check and not remove:
        check_entry = build_entry(agent, absolute=not in_project)
        dsh = next((c for c in chosen if c.fmt == "dsh-yaml"), None)
        if dsh:
            check_entry = entry_for(dsh, check_entry)
        ok, message = check_serve(check_entry, cwd=root if in_project else "")
        print(f"{'+' if ok else '!'} {'serve-check':<16} {message}")
        if not ok:
            counts["skipped"] += 1
    changed = counts.get("updated", 0) + counts.get("replaced", 0) + counts.get("removed", 0)
    manual = counts.get("manual", 0)
    if changed:
        print(f"\n{changed} client(s) changed. Restart the app, or reconnect its MCP server, to load the entry.")
    elif not manual:
        print("\nnothing to change; every detected client is up to date")
    if manual:
        print(f"\n{manual} client(s) left alone: paste the block printed above into that file by hand.")
    if not remove:
        # Still worth showing: on a machine without the CLI this is the line a human pastes.
        print(f"Claude Code: claude mcp add {SERVER} --scope {scope} "
              f"-e MEMDEX_AGENT={agent or 'claude'} -- {SERVER} serve")
    return 1 if counts.get("skipped") else 0
