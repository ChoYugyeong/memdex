"""A small, deterministic regression set for note retrieval, plus a natural-language set.

This synthetic benchmark measures retrieval mechanics only. It does not make claims about user data or
general agent quality; extend the cases with anonymized real misses before tuning ranking weights.

The natural-language set asks for files and notes in other words than they use, and compares keyword-only
ranking with the vector ranking on the same index. Its keyword side is deterministic; its vector side needs
a local model and is skipped without one.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from . import project as projects
from .store import Store


def _ts(date: str) -> float:
    parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def evaluate_retrieval(k: int = 5) -> dict[str, Any]:
    """Run exact, cue, record-time, domain-time and procedural-memory retrieval cases."""
    k = max(1, min(int(k), 100))
    with tempfile.TemporaryDirectory(prefix="memdex-eval-") as root:
        project_root = os.path.join(root, "project")
        os.makedirs(project_root)
        store = Store(":memory:")
        store.embedding = False  # this set measures the deterministic keyword mechanics only
        try:
            project = projects.resolve(project_root)
            exact = store.add_note(project, "pitfall", "Enable WAL for competing agent sessions",
                                   "Use a single-writer journal layout to avoid lock failures.", "eval")
            cue = store.add_note(project, "decision", "Keep a bounded request cache",
                                 "WAL writer lock contention rose after cache growth.", "eval")
            old = store.add_note(project, "decision", "Cache invalidation uses age tags",
                                 "The legacy cache policy expires entries by max age.", "eval")
            new = store.add_note(project, "decision", "Cache invalidation uses versioned keys",
                                 "The current cache policy invalidates on version changes.", "eval",
                                 supersedes=old)
            old_at, new_at = _ts("2024-01-01T00:00:00Z"), _ts("2024-06-01T00:00:00Z")
            store.db.execute("UPDATE notes SET created_at=?, updated_at=? WHERE id=?", (old_at, old_at, old))
            store.db.execute("UPDATE notes SET created_at=?, updated_at=? WHERE id=?", (new_at, new_at, new))

            store.add_note(project, "decision", "TLS certificate verification required",
                           "Production clients reject invalid certificates.", "eval",
                           valid_from=_ts("2023-01-01"), valid_until=_ts("2024-01-01"))
            current_policy = store.add_note(project, "decision", "TLS certificate verification exceptions",
                                            "Local development may use the test CA.", "eval",
                                            valid_from=_ts("2024-01-01"))
            success = store.add_note(project, "experience", "Windows wheel build recovery",
                                     "A clean isolated build succeeded.", "eval", outcome="success",
                                     context="Windows native wheel packaging failed on a stale build cache.",
                                     steps=["remove build outputs", "rebuild in a clean venv"],
                                     evidence="wheel built and installed in the clean environment")
            failed = store.add_note(project, "experience", "Windows wheel build recovery attempt",
                                    "Reusing the stale cache reproduced the error.", "eval", outcome="failed",
                                    context="Windows native wheel packaging failed on a stale build cache.",
                                    steps=["reuse the old build cache"], evidence="same compiler error returned")

            cases = [
                {"name": "exact query", "category": "exact", "query": "WAL competing agent sessions",
                 "expected": [exact], "kinds": ["pitfall"]},
                {"name": "alternate recall cue", "category": "cue", "query": "concurrent transaction freeze",
                 "cues": ["WAL writer lock"], "expected": [cue], "kinds": ["decision"]},
                {"name": "record-time history", "category": "record_time",
                 "query": "cache invalidation policy", "as_of": old_at + 1,
                 "expected": [old], "kinds": ["decision"]},
                {"name": "domain-validity", "category": "domain_time",
                 "query": "TLS certificate verification", "valid_at": _ts("2024-05-01"),
                 "expected": [current_policy], "kinds": ["decision"]},
                {"name": "procedural outcome memory", "category": "experience",
                 "query": "Windows wheel build cache failure", "expected": [success, failed],
                 "kinds": ["experience"]},
            ]
            details = []
            cue_hits, cue_baseline = 0, 0
            reciprocal_ranks = []
            for case in cases:
                hits = store.search(project, case["query"], case.get("kinds"), k,
                                    cues=case.get("cues"), as_of=case.get("as_of"),
                                    valid_at=case.get("valid_at"))
                found = [hit["id"] for hit in hits if hit["src"] == "note"]
                expected = case["expected"]
                matched = [nid for nid in expected if nid in found]
                first_rank = min((found.index(nid) + 1 for nid in matched), default=None)
                reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
                if case["category"] == "cue":
                    cue_hits += bool(matched)
                    baseline = store.search(project, case["query"], case.get("kinds"), k,
                                            as_of=case.get("as_of"), valid_at=case.get("valid_at"))
                    cue_baseline += any(hit["id"] in expected for hit in baseline if hit["src"] == "note")
                details.append({"name": case["name"], "category": case["category"],
                                "expected": expected, "found": found, "matched": matched,
                                "first_relevant_rank": first_rank})
            relevant = sum(len(case["expected"]) for case in cases)
            retrieved = sum(len(d["matched"]) for d in details)
            categories: dict[str, dict[str, float]] = {}
            for category in sorted({case["category"] for case in cases}):
                group = [(case, detail) for case, detail in zip(cases, details)
                         if case["category"] == category]
                denom = sum(len(case["expected"]) for case, _ in group)
                numer = sum(len(detail["matched"]) for _, detail in group)
                categories[category] = {"recall_at_k": numer / denom if denom else 0.0,
                                        "cases": len(group)}
            return {"k": k, "cases": details, "case_count": len(cases),
                    "recall_at_k": retrieved / relevant if relevant else 0.0,
                    "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
                    "by_category": categories,
                    "cue_case_recall_at_k": float(cue_hits),
                    "cue_baseline_recall_at_k": float(cue_baseline)}
        finally:
            store.close()


# A small synthetic checkout for the natural-language set. Every file says what it does in its docstring or
# heading; the questions ask for that behaviour in other words, the way an agent new to a codebase asks.
_NL_FILES = {
    "net/backoff.py": (
        '"""Exponential backoff with jitter for flaky HTTP calls."""\n\n'
        "import random\nimport time\n\n\n"
        "def retry_with_backoff(fn, attempts=5, base=0.2):\n"
        "    for n in range(attempts):\n"
        "        try:\n            return fn()\n"
        "        except ConnectionError:\n"
        "            time.sleep(base * 2 ** n + random.random() / 10)\n"
        "    raise TimeoutError('gave up')\n"),
    "auth/session_tokens.py": (
        '"""Issue and verify signed session cookies; rotate the signing keys."""\n\n'
        "import hmac\n\n\n"
        "def issue_cookie(user_id, key):\n    return f'{user_id}.' + hmac.new(key, str(user_id).encode(), 'sha256').hexdigest()\n\n\n"
        "def verify_cookie(cookie, key):\n    user_id, _, mac = cookie.partition('.')\n"
        "    return hmac.compare_digest(issue_cookie(user_id, key), cookie)\n"),
    "billing/invoice_pdf.py": (
        '"""Render invoices to PDF: line items, VAT and the payment due date."""\n\n\n'
        "def render_invoice(order, out_path):\n    lines = [f'{i.name} {i.price}' for i in order.items]\n"
        "    return write_pdf(out_path, lines)\n"),
    "storage/migrations.py": (
        '"""Schema upgrades: apply versioned ALTER TABLE scripts in order."""\n\n'
        "SCRIPTS = {2: 'ALTER TABLE users ADD COLUMN email TEXT'}\n\n\n"
        "def upgrade(db, current):\n    for version in sorted(v for v in SCRIPTS if v > current):\n"
        "        db.execute(SCRIPTS[version])\n"),
    "jobs/scheduler.go": (
        "// Package jobs runs periodic cleanup work on a cron-like timetable.\n"
        "package jobs\n\n"
        "func Every(spec string, job func()) {}\n\n"
        "func PurgeExpiredUploads() {}\n"),
    "i18n/translate.py": (
        '"""Look up localized strings by locale, falling back to English."""\n\n'
        "CATALOG = {'ko': {}, 'ja': {}}\n\n\n"
        "def gettext(key, locale):\n    return CATALOG.get(locale, {}).get(key) or key\n"),
    "docs/deploy.md": (
        "# Deploying to production\n\n"
        "Build the container image, push it to the registry, then roll it out with a blue-green switch.\n"),
    "ui/palette.ts": (
        "/** Colour tokens for the light and the dark theme. */\n"
        "export const light = { bg: '#fff' };\nexport const dark = { bg: '#111' };\n"),
    "utils/strings.py": (
        '"""Small string helpers."""\n\n\ndef slugify(text):\n    return text.lower().replace(" ", "-")\n'),
    "README.md": "# Shop\n\nAn example web shop used by the memdex retrieval evaluation.\n",
    # Distractors, several of them close neighbours of an answer, so neither ranking wins by elimination.
    "net/http_client.py": ('"""Thin wrapper around urllib with default headers and timeouts."""\n\n\n'
                           "def get(url, timeout=10):\n    return open_url(url, timeout)\n"),
    "auth/passwords.py": ('"""Hash and check passwords with scrypt."""\n\n\n'
                          "def hash_password(raw):\n    return scrypt(raw)\n\n\n"
                          "def check_password(raw, stored):\n    return hash_password(raw) == stored\n"),
    "billing/tax.py": ('"""VAT rates per country and rounding rules."""\n\nRATES = {"DE": 0.19, "FR": 0.20}\n\n\n'
                       "def vat(amount, country):\n    return round(amount * RATES[country], 2)\n"),
    "storage/cache.py": ('"""In-process LRU cache for product lookups."""\n\n\n'
                         "class ProductCache:\n    def get(self, key):\n        return None\n"),
    "storage/models.py": ('"""Table definitions for users, orders and products."""\n\n\n'
                          "class User:\n    pass\n\n\nclass Order:\n    pass\n"),
    "jobs/queue.go": ("// Package jobs keeps an in-memory FIFO of pending work items.\npackage jobs\n\n"
                      "type Queue struct{}\n\nfunc (q *Queue) Push(item string) {}\n"),
    "api/routes.py": ('"""HTTP routes for the storefront: products, cart and checkout."""\n\n\n'
                      "def checkout(request):\n    return render_invoice(request.order, '/tmp/x.pdf')\n"),
    "api/errors.py": ('"""Map exceptions to JSON error responses."""\n\n\n'
                      "def to_response(exc):\n    return {'error': str(exc)}\n"),
    "ui/button.tsx": "/** Primary and secondary buttons. */\nexport function Button() { return null; }\n",
    "ui/layout.tsx": "/** Page shell: header, navigation and footer. */\nexport function Layout() { return null; }\n",
    "config/settings.py": ('"""Read settings from environment variables."""\n\nimport os\n\n'
                           "DEBUG = os.environ.get('DEBUG') == '1'\n"),
    "scripts/build.sh": "#!/bin/sh\n# Build the frontend bundle and copy static assets.\nnpm run build\n",
    "docs/architecture.md": "# Architecture\n\nThe storefront talks to one database; background jobs share it.\n",
    "CHANGELOG.md": "# Changelog\n\n## 1.2.0\n\nFaster product search.\n",
    "tests/test_cart.py": '"""Cart totals and discounts."""\n\n\ndef test_total():\n    assert 1 + 1 == 2\n',
}

_NL_FILE_CASES = [
    ("where do we try a failed request again after waiting?", "net/backoff.py"),
    ("how does a user stay logged in between page loads", "auth/session_tokens.py"),
    ("where is the printable bill for a customer made", "billing/invoice_pdf.py"),
    ("how are database structure changes rolled out", "storage/migrations.py"),
    ("what deletes old uploaded files every night", "jobs/scheduler.go"),
    ("where is the Korean or Japanese text for the interface chosen", "i18n/translate.py"),
    ("how do I ship a new release to the live servers", "docs/deploy.md"),
    ("어디서 다크 모드 색을 정하지?", "ui/palette.ts"),
]

_NL_NOTES = [
    ("pitfall", "WAL checkpoints stall under a long reader",
     "A reader that never closes keeps the -wal file from being truncated; checkpoint after closing it."),
    ("decision", "Prices are stored in integer cents",
     "Floats rounded totals wrongly on refunds, so every amount is an integer number of cents."),
    ("pitfall", "CI on Windows needs LF checkout for golden files",
     "core.autocrlf rewrote the expected outputs and every snapshot test failed."),
]

_NL_NOTE_CASES = [
    ("why does the database log file keep growing", 0),
    ("money rounding was off after a refund", 1),
    ("snapshot tests fail only on the Windows runner", 2),
]


def _rank_metrics(ranks: list) -> dict[str, float]:
    found = [r for r in ranks if r]
    return {"recall_at_k": len(found) / len(ranks) if ranks else 0.0,
            "mrr": sum(1.0 / r for r in found) / len(ranks) if ranks else 0.0}


def evaluate_natural_language(k: int = 5) -> dict[str, Any]:
    """Natural-language questions about files and notes, keyword-only versus with vectors.

    Keyword-only is deterministic and always measured. The vector side needs a model (MEMDEX_EMBED); without
    one it is reported as skipped rather than guessed. Both sides search the same index of the same files.
    """
    k = max(1, min(int(k), 100))
    with tempfile.TemporaryDirectory(prefix="memdex-eval-nl-") as root:
        project_root = os.path.join(root, "shop")
        for rel, text in _NL_FILES.items():
            full = os.path.join(project_root, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        store = Store(":memory:")
        try:
            project = projects.resolve(project_root)
            store.index(project)
            model = store._embed_model()
            note_ids = [store.add_note(project, kind, title, body, "eval") for kind, title, body in _NL_NOTES]
            embedded = store.embed_pending(project) if model else {"embedded": 0}

            def run(vectors: bool) -> dict[str, Any]:
                store.embedding = vectors
                details, file_ranks, note_ranks = [], [], []
                for query, expected in _NL_FILE_CASES:
                    hits = store.search(project, query, ["file"], k)
                    found = [h["path"] for h in hits]
                    rank = found.index(expected) + 1 if expected in found else None
                    file_ranks.append(rank)
                    details.append({"query": query, "expected": expected, "rank": rank, "found": found})
                for query, index in _NL_NOTE_CASES:
                    hits = store.search(project, query, None, k)
                    found = [h["id"] for h in hits if h["src"] == "note"]
                    rank = found.index(note_ids[index]) + 1 if note_ids[index] in found else None
                    note_ranks.append(rank)
                    details.append({"query": query, "expected": f"note #{note_ids[index]}", "rank": rank,
                                    "found": found})
                return {"files": _rank_metrics(file_ranks), "notes": _rank_metrics(note_ranks),
                        "all": _rank_metrics(file_ranks + note_ranks), "cases": details}

            report: dict[str, Any] = {"k": k, "case_count": len(_NL_FILE_CASES) + len(_NL_NOTE_CASES),
                                      "keyword": run(False)}
            if model:
                report["vectors"] = run(True)
                report["model"] = model
                report["cards_embedded"] = embedded["embedded"]
            else:
                report["vectors"] = None
                report["skipped"] = "no model: set MEMDEX_EMBED=ollama:bge-m3 to measure the vector side"
            store.embedding = True
            return report
        finally:
            store.close()
