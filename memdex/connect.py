"""Register memdex with the agents already installed on this machine.

`memdex config` prints snippets for a human to paste. This writes them instead: it finds each client's
config file and merges one entry — `mcpServers.memdex` for most JSON clients, `servers.memdex` with
`type: stdio` for VS Code, `[mcp_servers.memdex]` for Codex's TOML — and backs the file up. A client whose config directory does not exist is reported as not installed and left
alone, so `memdex connect` never scatters empty config folders for apps this machine does not have.
A config file that does not parse is never rewritten: an agent's settings are worth more than the
convenience of editing them.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

SERVER = "memdex"


@dataclass(frozen=True)
class Client:
    """One agent's config file.

    `container` is the key holding the server map (JSON clients disagree: `mcpServers` for most,
    `servers` for VS Code) and `extra` is whatever else that client needs in every entry (VS Code wants
    `type: stdio`). Codex is the one TOML client, so its entry is rendered rather than merged.
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


def _home() -> str:
    return os.path.expanduser("~")


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")


def clients() -> list:
    """Every client memdex knows how to configure, in the order they are tried."""
    system = platform.system()
    if system == "Windows":
        desktop = os.path.join(_appdata(), "Claude", "claude_desktop_config.json")
        code_user = os.path.join(_appdata(), "Code", "User", "mcp.json")
    elif system == "Darwin":
        desktop = os.path.join(_home(), "Library", "Application Support", "Claude",
                               "claude_desktop_config.json")
        code_user = os.path.join(_home(), "Library", "Application Support", "Code", "User", "mcp.json")
    else:
        desktop = os.path.join(_home(), ".config", "Claude", "claude_desktop_config.json")
        code_user = os.path.join(_home(), ".config", "Code", "User", "mcp.json")
    return [
        Client("claude-desktop", "Claude Desktop", desktop),
        Client("cursor", "Cursor", os.path.join(_home(), ".cursor", "mcp.json")),
        Client("vscode", "VS Code / Copilot", code_user, container="servers", extra={"type": "stdio"}),
        Client("windsurf", "Windsurf", os.path.join(_home(), ".codeium", "windsurf", "mcp_config.json")),
        Client("gemini", "Gemini CLI", os.path.join(_home(), ".gemini", "settings.json")),
        Client("codex", "Codex CLI", os.path.join(_home(), ".codex", "config.toml"), fmt="toml"),
    ]


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


def _equivalent(a, b) -> bool:
    """Order-insensitive comparison, so a hand-written entry that means the same thing is left alone."""
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equivalent(x, y) for x, y in zip(a, b))
    return a == b


def entry_for(client: Client, entry: dict) -> dict:
    """The base entry plus whatever this client insists on (VS Code wants `type: stdio`)."""
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


def list_clients(agent: str = "", targets: Optional[list] = None) -> int:
    """Print what is installed and whether it already carries the entry. Writes nothing."""
    entry = build_entry(agent)
    print(f"memdex connect · status · agent {agent or '(unset, the client name is used)'}")
    for client in targets if targets is not None else clients():
        state, detail = inspect(client, entry)
        mark = {"configured": "✓", "missing": "·", "different": "~", "unreadable": "!",
                "absent": "·"}.get(state, "?")
        line = f"{mark} {client.slug:<14} {state:<11}"
        print(line + (client.path if state != "absent" else detail))
        if state in ("different", "unreadable"):
            print(f"  {detail}")
    return 0


def summary(targets: Optional[list] = None) -> tuple:
    """(status, detail) for `memdex doctor`: which installed clients still need `memdex connect`."""
    entry = build_entry()
    installed, missing = [], []
    for client in targets if targets is not None else clients():
        state, _ = inspect(client, entry, exact=False)
        if state == "absent":
            continue
        (installed if state == "configured" else missing).append(client.slug)
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
        claude: Optional[str] = None) -> int:
    """Configure every detected client. Returns 0 when nothing was skipped for a bad reason.

    `claude` is the path to the Claude Code CLI when it should be run as well (None leaves it alone, so
    tests and `--no-claude` never touch the real thing). `scope="project"` writes the configs that live in
    the checkout and are meant to be committed.
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
    want_claude = bool(claude) and (not wanted or "all" in wanted or "claude-code" in wanted)
    if "claude-code" in wanted and not claude:
        print("· claude-code      claude CLI not on PATH; run the command below by hand")
    if in_project and want_claude and "claude-project" not in wanted:
        # `claude mcp add --scope project` writes .mcp.json itself; writing it here too would be two owners
        # for one file. Unless the file was named explicitly, the CLI gets it.
        chosen = [c for c in chosen if c.slug != "claude-project"]
    verb = "remove" if remove else "install"
    print(f"memdex connect · {verb} · {scope} scope · agent {agent or '(unset, the client name is used)'}")
    counts = {"updated": 0, "replaced": 0, "removed": 0, "unchanged": 0, "skipped": 0, "absent": 0}
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
        if client.fmt == "toml":
            status, detail = merge_toml(client.path, wanted_entry, agent, remove=remove)
        else:
            status, detail = merge_json(client.path, wanted_entry, client.container, remove=remove)
        counts[status] = counts.get(status, 0) + 1
        mark = {"updated": "+", "replaced": "~", "removed": "-", "unchanged": "=", "skipped": "!"}.get(status, "?")
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
    changed = counts.get("updated", 0) + counts.get("replaced", 0) + counts.get("removed", 0)
    if changed:
        print(f"\n{changed} client(s) changed. Restart the app, or reconnect its MCP server, to load the entry.")
    else:
        print("\nnothing to change; every detected client is up to date")
    if not remove:
        # Still worth showing: on a machine without the CLI this is the line a human pastes.
        print(f"Claude Code: claude mcp add {SERVER} --scope {scope} "
              f"-e MEMDEX_AGENT={agent or 'claude'} -- {SERVER} serve")
    return 1 if counts.get("skipped") else 0
