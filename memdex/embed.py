"""Optional semantic search through a local embedding model.

Off by default. Set MEMDEX_EMBED=ollama:<model> (for example ollama:bge-m3,
which handles Korean, Japanese and English) to turn it on. Vectors are
normalised float32 blobs searched by brute force, which stays fast for the
few thousand notes and summaries a project accumulates, with no extra library.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from array import array
from typing import Optional

_disabled_after_error = False


def config() -> Optional[tuple[str, str]]:
    spec = os.environ.get("MEMDEX_EMBED", "").strip()
    if not spec or _disabled_after_error:
        return None
    provider, _, model = spec.partition(":")
    if provider != "ollama" or not model:
        return None
    return provider, model


def embed(text: str) -> Optional[bytes]:
    global _disabled_after_error
    cfg = config()
    if not cfg or not text.strip():
        return None
    url = os.environ.get("MEMDEX_OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/embed"
    req = urllib.request.Request(
        url, data=json.dumps({"model": cfg[1], "input": text[:8000]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            vec = json.loads(r.read())["embeddings"][0]
    except Exception:
        _disabled_after_error = True  # don't stall every call on a dead server
        return None
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array("f", (x / n for x in vec)).tobytes()


def normalize(vec) -> Optional[bytes]:
    """A vector the caller computed, normalised the way a model's output is.

    memdex never runs a model itself, but the agent it serves has one. Handing memdex the vectors keeps
    semantic search working without Ollama — and keeps the promise that nothing inside memdex calls out to
    a model.
    """
    try:
        values = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    if not values:
        return None
    n = math.sqrt(sum(x * x for x in values)) or 1.0
    return array("f", (x / n for x in values)).tobytes()


def similarity(a: bytes, b: bytes) -> float:
    va, vb = array("f"), array("f")
    va.frombytes(a)
    vb.frombytes(b)
    if len(va) != len(vb):
        return -1.0
    return sum(x * y for x, y in zip(va, vb))
