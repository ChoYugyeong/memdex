"""Optional bridge to Kensaku (https://github.com/yjid/kensaku).

Kensaku is a local desktop search engine: file names, full text of PDF/DOCX/
EPUB, OCR'd screenshots and a static-vector semantic tier, read-only, nothing
leaves the machine. memdex indexes a project's source; Kensaku indexes the whole
computer. When `kensaku-cli` is on PATH (or MEMDEX_KENSAKU names it), memdex
search also returns Kensaku's content hits, so an agent can find "the PDF about
the auth flow" or "the screenshot with the CUDA error" without a second tool.

Set MEMDEX_KENSAKU=off to disable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional


def command() -> Optional[str]:
    spec = os.environ.get("MEMDEX_KENSAKU", "").strip()
    if spec.lower() in ("off", "0", "false"):
        return None
    return spec or shutil.which("kensaku-cli")


def _run(args: list[str], timeout: float) -> Optional[str]:
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:  # 3 = no index yet; 2 = usage
        return None
    return r.stdout.decode("utf-8", "replace")


def search(query: str, root: Optional[str] = None, limit: int = 5, timeout: float = 15) -> list[dict[str, Any]]:
    """Ranked Kensaku hits as {path, snippet, match}. Empty when unavailable."""
    cmd = command()
    if not cmd or not query.strip():
        return []
    args = [cmd, "search", query, "--json", "--limit", str(max(1, min(limit, 50)))]
    if root:
        args += ["--path", root]
    out = _run(args, timeout)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    hits = []
    for it in (data.get("results") or [])[:limit]:
        if not isinstance(it, dict) or not it.get("path"):
            continue
        snippet = " ".join(str(it.get("snippet") or "").split())[:240]
        hits.append({"path": str(it["path"]), "snippet": snippet, "match": str(it.get("match_type") or "")})
    return hits
