"""Near-duplicate notes without a model: 64-bit SimHash over word shingles.

Agents write the same lesson twice under different titles. SimHash turns a note into one integer such that
near-identical text differs in a handful of bits and unrelated text differs in about half of them, so a
Hamming distance is enough to say "this looks like #17, supersede it instead".

Nothing is stored: a project has hundreds of active notes, so computing fingerprints on demand is cheaper
than another table to keep in sync with every edit.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

BITS = 64
_WORD = re.compile(r"[0-9A-Za-z가-힣ぁ-んァ-ン一-龥]+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, keep letters/digits (Latin, Hangul, kana, CJK), collapse whitespace."""
    return " ".join(_WORD.findall((text or "").lower()))


def _shingles(text: str) -> list[str]:
    """Word unigrams, bigrams and trigrams.

    Notes are one or two sentences, so a single word n-gram size is too coarse: one changed word moves a
    quarter of the shingles. Overlapping sizes keep a small edit to a few bits (measured: a near-duplicate
    pair sits at 3–5 bits, unrelated text at 28–35).
    """
    words = normalize(text).split()
    if not words:
        return []
    out = list(words)
    out += [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    out += [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    return out


def _hash(shingle: str) -> int:
    return int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")


def fingerprint(text: str) -> int:
    """64-bit SimHash: each shingle votes for the bits of its hash; the majority wins."""
    shingles = _shingles(text)
    if not shingles:
        return 0
    votes = [0] * BITS
    for shingle in shingles:
        h = _hash(shingle)
        for bit in range(BITS):
            votes[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit in range(BITS):
        if votes[bit] > 0:
            value |= 1 << bit
    return value


def hamming(a: int, b: int) -> int:
    """Number of differing bits (0 = identical, 32 = unrelated on average)."""
    return bin(a ^ b).count("1")


def note_text(title: str, body: Optional[str]) -> str:
    return f"{title or ''}\n{body or ''}"


def near_duplicates(store, project_key: str, title: str, body: Optional[str] = "",
                    max_distance: int = 6, limit: int = 3) -> list[tuple]:
    """Active notes of this project within `max_distance` bits of the given text, closest first.

    Returns (sqlite3.Row, distance) pairs. Archived and superseded notes are not candidates: they are not
    what a new note should replace.
    """
    target = fingerprint(note_text(title, body))
    rows = store.db.execute(
        "SELECT * FROM notes WHERE project=? AND archived=0 AND superseded_by IS NULL",
        (project_key,)).fetchall()
    scored = []
    for row in rows:
        distance = hamming(target, fingerprint(note_text(row["title"], row["body"])))
        if distance <= max_distance:
            scored.append((row, distance))
    scored.sort(key=lambda pair: (pair[1], -(pair[0]["created_at"] or 0)))
    return scored[:limit]

