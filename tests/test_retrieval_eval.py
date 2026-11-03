"""The deterministic synthetic retrieval regression set also runs through the public CLI entry point."""

import unittest

from memdex.evaluation import evaluate_retrieval


class RetrievalEvaluationTests(unittest.TestCase):
    def test_synthetic_cases_have_full_recall_and_cues_add_a_hit(self):
        report = evaluate_retrieval(k=5)
        self.assertEqual(report["case_count"], 5)
        self.assertEqual(report["recall_at_k"], 1.0, report["cases"])
        self.assertEqual(report["cue_case_recall_at_k"], 1.0)
        self.assertEqual(report["cue_baseline_recall_at_k"], 0.0)
        self.assertEqual(report["by_category"]["record_time"]["recall_at_k"], 1.0)
        self.assertEqual(report["by_category"]["domain_time"]["recall_at_k"], 1.0)


if __name__ == "__main__":
    unittest.main()
