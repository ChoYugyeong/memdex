"""Work out which project a path belongs to.

`root` is the checkout the agent is working in (a git worktree has its own).
`key` identifies the project as a whole: every worktree of one repository
shares it, so notes written in one worktree are visible from the others.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Project:
    root: str
    key: str
    name: str
    is_git: bool


def git(args: list[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").strip()


def real(path: str) -> str:
    return str(Path(path).resolve())


def norm(path: str) -> str:
    return os.path.normcase(real(path))


def resolve(path: Optional[str] = None) -> Project:
    start = str(Path(path or os.getcwd()).resolve())
    if os.path.isfile(start):
        start = os.path.dirname(start)
    top = git(["rev-parse", "--show-toplevel"], start)
    if top:
        common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start)
        main = str(Path(common).parent) if common else top
        return Project(root=real(top), key=norm(main), name=Path(main).name, is_git=True)
    return Project(root=real(start), key=norm(start), name=Path(start).name, is_git=False)
