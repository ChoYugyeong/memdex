"""Optional semantic search through a local embedding model.

Off by default. Set MEMDEX_EMBED=ollama:<model> (for example ollama:bge-m3,
which handles Korean, Japanese and English) to turn it on. Vectors are
normalised and searched by brute force, which stays fast for the few thousand
notes, summaries and file cards a project accumulates, with no extra library.

Two encodings live side by side. Notes and summaries keep the original float32
blobs (4 bytes per dimension). File metadata cards, which outnumber them a
hundredfold, are stored as int8 with one float32 scale per vector: a quarter of
the size, and in pure Python just as fast to score. float16 was measured too:
half the size of float32 but about three times slower to decode without numpy,
because `array` has no half-float type and `struct.unpack` builds a float per
dimension.
"""

from __future__ import annotations

import json
import math
import operator
import os
import struct
import urllib.request
from array import array
from typing import Optional, Sequence

_disabled_after_error = False

# One /api/embed request carries at most this many inputs. Larger batches barely help on a laptop GPU and
# make a single slow call (model cold start) hold up the caller for longer.
BATCH = 32
# Characters per input. bge-m3 accepts 8k tokens, but a file card is meant to be short; the cap keeps a
# pathological outline from turning one batch into a minute of GPU time.
MAX_INPUT_CHARS = 8000

_sumprod = getattr(math, "sumprod", None)  # Python 3.12+; a C loop, about 3x faster than map/sum


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    if _sumprod is not None:
        return _sumprod(a, b)
    return sum(map(operator.mul, a, b))


def base_url() -> str:
    """Ollama's address. 127.0.0.1, not localhost: on Windows urllib tries localhost's IPv6 address first,
    and Ollama listens on IPv4 only, so every call waited about two seconds for the fallback."""
    return os.environ.get("MEMDEX_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def config() -> Optional[tuple[str, str]]:
    spec = os.environ.get("MEMDEX_EMBED", "").strip()
    if not spec or _disabled_after_error:
        return None
    provider, _, model = spec.partition(":")
    if provider != "ollama" or not model:
        return None
    return provider, model


def model_name() -> Optional[str]:
    cfg = config()
    return cfg[1] if cfg else None


def _unit(vec) -> Optional[list[float]]:
    try:
        values = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    if not values or not all(math.isfinite(x) for x in values):
        return None
    n = math.sqrt(sum(x * x for x in values)) or 1.0
    return [x / n for x in values]


def embed_many(texts: list[str], timeout: float = 60) -> Optional[list[list[float]]]:
    """Unit vectors for several texts in one request, or None when no model answers.

    One failure turns embedding off for the rest of the process, exactly like `embed`: a dead or missing
    server must not cost every later call another timeout.
    """
    global _disabled_after_error
    cfg = config()
    if not cfg or not texts:
        return None
    url = base_url() + "/api/embed"
    payload = {"model": cfg[1], "input": [str(t)[:MAX_INPUT_CHARS] for t in texts]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["embeddings"]
        out = [_unit(v) for v in raw]
        if len(out) != len(texts) or any(v is None for v in out):
            raise ValueError("embedding count or shape mismatch")
    except Exception:
        _disabled_after_error = True  # don't stall every call on a dead server
        return None
    return out  # type: ignore[return-value]


def embed(text: str) -> Optional[bytes]:
    if not config() or not text.strip():
        return None
    got = embed_many([text], timeout=30)
    if not got:
        return None
    return array("f", got[0]).tobytes()


def normalize(vec) -> Optional[bytes]:
    """A vector the caller computed, normalised the way a model's output is.

    memdex never runs a model itself, but the agent it serves has one. Handing memdex the vectors keeps
    semantic search working without Ollama — and keeps the promise that nothing inside memdex calls out to
    a model.
    """
    values = _unit(vec) if vec is not None else None
    if values is None:
        return None
    return array("f", values).tobytes()


def floats(blob: bytes) -> array:
    """A float32 blob (notes, summaries, query vectors) back as numbers."""
    out = array("f")
    out.frombytes(blob)
    return out


def similarity(a: bytes, b: bytes) -> float:
    va, vb = floats(a), floats(b)
    if len(va) != len(vb):
        return -1.0
    return dot(va, vb)


# ------------------------------------------------------------------ int8 file-card vectors

def quantize(vec: Sequence[float]) -> bytes:
    """int8 with a per-vector scale: 4 + d bytes instead of 4d.

    The scale is the largest magnitude, so every component uses the full -127..127 range. On a unit vector
    of 1024 dimensions the rounding error moves a cosine by well under 0.001, far below the gaps that order
    search results.
    """
    peak = max((abs(x) for x in vec), default=0.0) or 1.0
    step = peak / 127.0
    q = array("b", (max(-127, min(127, int(round(x / step)))) for x in vec))
    return struct.pack("<f", step) + q.tobytes()


def dim_q8(blob: bytes) -> int:
    return max(len(blob) - 4, 0)


def score_q8(blob: bytes, query: Sequence[float]) -> float:
    """Cosine between a quantized card and a unit query (already a sequence of floats)."""
    if len(blob) - 4 != len(query):
        return -1.0
    (step,) = struct.unpack_from("<f", blob)
    q = array("b")
    q.frombytes(blob[4:])
    return dot(q, query) * step


# ------------------------------------------------------------------ warm-up

def warm() -> bool:
    """Whether the configured model is already loaded, so an embed call costs ~0.1 s instead of a load.

    Asked of Ollama's /api/ps with a short timeout. A failure here says nothing about the model, so it
    returns False without turning embedding off.
    """
    cfg = config()
    if not cfg:
        return False
    url = base_url() + "/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            models = json.loads(r.read()).get("models") or []
    except Exception:
        return False
    want = cfg[1] if ":" in cfg[1] else cfg[1] + ":latest"
    return any(m.get("name") in (cfg[1], want) or m.get("model") in (cfg[1], want) for m in models)


def preload() -> None:
    """Ask Ollama to load the model in the background; the caller does not wait.

    A load takes seconds (3 s for bge-m3 from the file cache, 20 s from a cold disk). The brief must not
    pay that, so it only embeds when the model is warm and otherwise starts the load for the next call.
    The thread touches no database, only the local HTTP endpoint.
    """
    cfg = config()
    if not cfg:
        return
    import threading

    def run() -> None:
        url = base_url() + "/api/embed"
        req = urllib.request.Request(url, data=json.dumps({"model": cfg[1], "input": ["warm"]}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).close()
        except Exception:
            pass

    threading.Thread(target=run, name="memdex-embed-preload", daemon=True).start()
