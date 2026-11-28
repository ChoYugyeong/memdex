"""Team roster: which agent, on which model, plays which role in a project.

A role is a name the human gives to a seat (pm, reviewer, art); the roster maps it to an agent and,
optionally, the model that seat should run on. It is used in three places:

- the brief tells each agent the roster and which seat is its own;
- `owner="role:reviewer"` on a task resolves to that seat's agent and remembers the seat;
- the assigned-work dispatcher starts the agent on the seat's model when it wakes it for such a task.

A roster entry under project "*" is the machine-wide default; a project's own entry for the same
role replaces it. The tables are created on first write, so readers tolerate their absence.
"""
import re
import sqlite3
import time

GLOBAL = "*"
ROLE = re.compile(r"[^\W_][\w-]{0,31}")
AGENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# The model reaches a CLI as one argv item, on Windows through cmd.exe for a .cmd launcher: no spaces,
# quotes or shell metacharacters.
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}")
# Agents the assigned-work dispatcher can start itself, and how the model reaches each.
LAUNCHED = {"claude": "Claude Code (--model)", "gpt": "Codex CLI (-m)", "qwen-code": "local Ollama model"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_roles (
    project TEXT NOT NULL, role TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    does TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, PRIMARY KEY(project, role));
CREATE TABLE IF NOT EXISTS team_task_roles (
    project TEXT NOT NULL, task INTEGER NOT NULL, role TEXT NOT NULL, PRIMARY KEY(project, task));
"""


def family(agent: str) -> str:
    return (agent or "").lower().partition("@")[0].partition("/")[0]


def _role(value) -> str:
    role = value.strip().lower() if isinstance(value, str) else ""
    if not ROLE.fullmatch(role):
        raise ValueError("a role is 1-32 letters, digits, '-' or '_', starting with a letter or digit")
    return role


def set_role(db, project: str, role: str, agent: str, model: str = "", does: str = "") -> dict:
    role, agent = _role(role), family(agent.strip() if isinstance(agent, str) else "")
    model = (model or "").strip()
    does = " ".join((does or "").split())[:160]
    if not AGENT.fullmatch(agent):
        raise ValueError("name the agent as it signs in to memdex, e.g. claude, gpt, deepseek, qwen-code")
    if model and not MODEL.fullmatch(model):
        raise ValueError("a model id is letters, digits and . _ : / [ ] - without spaces (max 80)")
    with db:
        db.executescript(_SCHEMA)
        db.execute("INSERT INTO team_roles VALUES(?,?,?,?,?,?) ON CONFLICT(project, role) DO UPDATE SET "
                   "agent=excluded.agent, model=excluded.model, does=excluded.does, updated=excluded.updated",
                   (project, role, agent, model, does, time.time()))
    return dict(role=role, agent=agent, model=model, does=does)


def remove_role(db, project: str, role: str) -> bool:
    role = _role(role)
    try:
        with db:
            return db.execute("DELETE FROM team_roles WHERE project=? AND role=?", (project, role)).rowcount > 0
    except sqlite3.OperationalError:
        return False


def roster(db, project: str) -> dict:
    """role -> {role, agent, model, does, scope}; the project's entry wins over the machine-wide one."""
    try:
        rows = db.execute("SELECT project, role, agent, model, does FROM team_roles WHERE project IN (?, ?) "
                          "ORDER BY project=? , role", (project, GLOBAL, project)).fetchall()
    except sqlite3.OperationalError:
        return {}
    seats = {}
    for scope, role, agent, model, does in rows:  # machine-wide rows first, then the project's own
        seats[role] = dict(role=role, agent=agent, model=model, does=does,
                           scope="machine" if scope == GLOBAL else "project")
    return dict(sorted(seats.items()))


def resolve_owner(db, project: str, owner: str):
    """`role:NAME` -> (agent, role); any other owner passes through as (owner, None)."""
    text = (owner or "").strip()
    if not text.lower().startswith("role:"):
        return owner, None
    role = _role(text[5:])
    seat = roster(db, project).get(role)
    if seat is None:
        known = ", ".join(roster(db, project)) or "none yet"
        raise ValueError(f"no '{role}' role in this project's team (roles: {known}); "
                         f"a human adds one with `memdex team set {role} AGENT`")
    return seat["agent"], role


def bind_task(db, project: str, task_id: int, role) -> None:
    """Remember the seat a task was assigned through; None forgets it (the owner was named directly)."""
    try:
        with db:
            if role is None:
                db.execute("DELETE FROM team_task_roles WHERE project=? AND task=?", (project, task_id))
            else:
                db.executescript(_SCHEMA)
                db.execute("INSERT OR REPLACE INTO team_task_roles VALUES(?,?,?)", (project, task_id, role))
    except sqlite3.OperationalError:
        if role is not None:
            raise


def task_seat(db, project: str, task_id: int, owner: str):
    """The seat a task is held through, if its owner is still that seat's agent; else None.

    Read at launch time, so changing a role's model applies to tasks already assigned through it.
    """
    try:
        row = db.execute("SELECT role FROM team_task_roles WHERE project=? AND task=?", (project, task_id)).fetchone()
    except sqlite3.OperationalError:
        return None
    seat = roster(db, project).get(row[0]) if row else None
    if seat is None or seat["agent"] != family(owner):
        return None
    if seat["model"] and not MODEL.fullmatch(seat["model"]):
        return None  # never hand an unvetted string to a launcher
    return seat


def _seat(seat) -> str:
    return f"{seat['role']}={seat['agent']}" + (f" ({seat['model']})" if seat["model"] else "")


def brief_line(db, project: str, agent: str) -> str:
    seats = roster(db, project)
    if not seats:
        return ""
    mine = [s for s in seats.values() if s["agent"] == family(agent)]
    line = "team: " + " · ".join(_seat(s) for s in seats.values())
    if mine:
        line += " — your role: " + "; ".join(s["role"] + (f" ({s['does']})" if s["does"] else "") for s in mine)
    return line + " · assign by seat with owner=role:NAME"


def render(db, project: str) -> str:
    seats = roster(db, project)
    if not seats:
        return ("no team roles yet. Add one: memdex team set reviewer deepseek --does \"code review\"\n"
                "  --model ID sets the model that seat runs on; --global makes it the default for every project.")
    lines = []
    for seat in seats.values():
        launch = LAUNCHED.get(seat["agent"])
        how = (f"started by memdex: {launch}" if launch else "started by its own app; the model is a note")
        lines.append(f"{seat['role']:<12} {seat['agent']:<12} {seat['model'] or '(agent default)':<28} "
                     f"[{seat['scope']}] {how}" + (f" — {seat['does']}" if seat["does"] else ""))
    return "\n".join(lines)
