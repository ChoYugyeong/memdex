"""A small, deterministic regression set for note retrieval.

This synthetic benchmark measures retrieval mechanics only. It does not make claims about user data or
general agent quality; extend the cases with anonymized real misses before tuning ranking weights.
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
