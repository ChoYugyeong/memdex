"""Widen the search keys of a text without a model.

A query says "store index"; the file says `StoreIndex`, `store_index` or `memdex/store.py`. FTS cannot bridge
that. `expand` keeps the original text untouched and appends the split forms, so one index entry answers all
of them — the cheap, offline version of the key expansion a sparse retriever does.
"""

from __future__ import annotations

import re

MAX_EXTRA = 2000  # characters of expansion; the original text is never shortened

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[_.\-/\\:]+")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/\\]*")


def _split_token(token: str) -> list[str]:
    parts: list[str] = []
    for chunk in _SPLIT.split(token):
        parts.extend(_CAMEL.split(chunk))
    return [p.lower() for p in parts if p]


def expansions(text: str) -> list[str]:
    """Split forms of every identifier-like token, in first-seen order, without duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN.findall(text or ""):
        parts = _split_token(token)
        if len(parts) < 2:
            continue
        phrase = " ".join(parts)
        if phrase and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def expand(text: str, max_extra: int = MAX_EXTRA) -> str:
    """Return the original text followed by its expanded keys (or the text alone when there is nothing)."""
    text = text or ""
    extra = expansions(text)
    if not extra:
        return text
    added, size = [], 0
    for phrase in extra:
        if size + len(phrase) + 1 > max_extra:
            break
        added.append(phrase)
        size += len(phrase) + 1
    if not added:
        return text
    return text + "\n" + " ".join(added)
