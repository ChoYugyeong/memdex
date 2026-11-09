"""File metadata vectors: cards keyed by content sha, incremental embedding, filtered hybrid search.

No model runs here. A fake embedder maps a handful of concept words to dimensions, so "the printable bill"
and "Render invoices to PDF" land close together without sharing a word, the way a real model places them.
"""

import contextlib
import io
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memdex import embed
from memdex import project as projects
from memdex.cli import main
from memdex.evaluation import evaluate_natural_language
from memdex.store import CARD_CHARS, SCHEMA_VERSION, Store, file_card, file_intro
from memdex.tools import Session

CONCEPTS = [
    ("retry", "backoff", "again", "flaky"),
    ("login", "logged", "session", "cookie"),
    ("invoice", "bill", "pdf", "printable"),
    ("schema", "migration", "alter", "database"),
    ("colour", "color", "dark", "theme", "palette"),
]


def fake_vector(text: str) -> list:
    t = text.lower()
    return [float(sum(t.count(w) for w in group)) for group in CONCEPTS] + [0.05]


def fake_embed_many(texts, timeout=60):
    return [embed._unit(fake_vector(t)) for t in texts]


FILES = {
    "net/backoff.py": '"""Exponential backoff with jitter for flaky HTTP calls."""\n\ndef retry_with_backoff(fn):\n    return fn()\n',
    "auth/session.py": '"""Issue and verify signed session cookies."""\n\ndef issue_cookie(user):\n    return user\n',
    "billing/invoice_pdf.py": '"""Render invoices to PDF with line items."""\n\ndef render_invoice(order):\n    return order\n',
    "storage/migrate.py": '"""Apply versioned ALTER TABLE scripts in order."""\n\ndef upgrade(db):\n    return db\n',
    "ui/palette.ts": "/** Colour tokens for the light and the dark theme. */\nexport const dark = 1;\n",
    "docs/guide.md": "# Guide\n\nHow the shop works, end to end.\n",
    "data/rows.json": '{"rows": [1, 2, 3]}\n',
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "shop")
        self.write_files(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)
        embed._disabled_after_error = False
        self.env = mock.patch.dict(os.environ, {"MEMDEX_EMBED": "ollama:fake"})
        self.env.start()
        self.calls = []

        def recording(texts, timeout=60):
            self.calls.append(list(texts))
            return fake_embed_many(texts, timeout)

        self.model = mock.patch("memdex.embed.embed_many", side_effect=recording)
        self.model.start()
        self.warm = mock.patch("memdex.embed.warm", return_value=True)
        self.warm.start()

    def tearDown(self):
        self.warm.stop()
        self.model.stop()
        self.env.stop()
        embed._disabled_after_error = False
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write_files(root):
        for rel, text in FILES.items():
            p = Path(root, *rel.split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:  # write_text(newline=) is 3.10+
                fh.write(text)

    def index(self, project=None):
        project = project or self.project
        self.store.index(project)
        return self.store.embed_pending(project)

    def card_count(self):
        return self.store.db.execute("SELECT count(*) FROM card_vectors").fetchone()[0]


class CardTextTests(unittest.TestCase):
    def test_a_card_holds_path_words_language_summary_intro_and_symbols(self):
        card = file_card("billing/invoice_pdf.py", "python",
                         "Render invoices.\nfn render_invoice:3; fn total:9", summary="Builds the PDF bill.",
                         intro="Render invoices to PDF with line items and VAT.")
        self.assertTrue(card.startswith("billing/invoice_pdf.py\npython file: billing invoice pdf py"))
        self.assertIn("Builds the PDF bill.", card)
        self.assertIn("line items and VAT", card)
        self.assertIn("fn render_invoice; fn total", card)  # line numbers move on every edit: dropped
        self.assertNotIn(":3", card)

    def test_a_card_stays_short(self):
        card = file_card("a.py", "python", "fn x:1; " * 2000)
        self.assertLessEqual(len(card), CARD_CHARS)

    def test_the_intro_is_the_first_paragraph_docstring_or_comment_block(self):
        self.assertEqual(file_intro("markdown", "---\ntitle: x\n---\n# Deploy\n\nBuild the image,\npush it.\n\nMore."),
                         "Build the image, push it.")
        self.assertEqual(file_intro("python", '#!/usr/bin/env python\n"""Retry calls.\n\nWith jitter."""\nx = 1\n'),
                         "Retry calls. With jitter.")
        self.assertEqual(file_intro("js", "'use strict';\n/**\n * Colour tokens.\n * Light and dark.\n */\nexport x\n"),
                         "Colour tokens. Light and dark.")
        self.assertEqual(file_intro("go", "package x\n\nfunc A() {}\n"), "")


class QuantizeTests(unittest.TestCase):
    def test_int8_cards_score_like_float_vectors(self):
        rng = random.Random(7)
        for _ in range(20):
            a = embed._unit([rng.gauss(0, 1) for _ in range(256)])
            b = embed._unit([rng.gauss(0, 1) for _ in range(256)])
            exact = sum(x * y for x, y in zip(a, b))
            self.assertAlmostEqual(embed.score_q8(embed.quantize(a), tuple(b)), exact, delta=0.01)

    def test_a_card_is_a_quarter_of_the_float32_size(self):
        v = embed._unit([1.0] * 1024)
        self.assertEqual(len(embed.quantize(v)), 4 + 1024)

    def test_a_dimension_mismatch_scores_minus_one(self):
        self.assertEqual(embed.score_q8(embed.quantize([1.0, 0.0]), (1.0, 0.0, 0.0)), -1.0)


class EmbeddingTests(Base):
    def test_cards_are_embedded_once_per_content_across_worktrees(self):
        other = os.path.join(self.tmp, "shop-worktree")
        self.write_files(other)
        first = self.index()
        second = self.index(projects.resolve(other))
        self.assertEqual(first["embedded"], 6)      # rows.json is data, not a card
        self.assertEqual(second["embedded"], 0)     # same content, same sha: nothing new
        self.assertEqual(self.card_count(), 6)
        self.assertEqual(self.index()["embedded"], 0)

    def test_an_edit_makes_the_file_pending_and_drops_the_old_vector(self):
        self.index()
        Path(self.root, "net", "backoff.py").write_text('"""Retry again."""\n', encoding="utf-8")
        self.store.index(self.project)
        self.assertEqual(self.store.card_stats(self.project)["pending"], 1)
        self.assertEqual(self.store.embed_pending(self.project)["embedded"], 1)
        self.assertEqual(self.card_count(), 6)  # the vector of the old content is gone

    def test_data_files_get_no_card_but_prose_text_does(self):
        Path(self.root, "notes.txt").write_text("Deploy notes for the shop.\n", encoding="utf-8")
        Path(self.root, "dump.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        self.index()
        shas = {r[0] for r in self.store.db.execute("SELECT f.path FROM files f JOIN card_vectors v ON v.sha=f.sha")}
        self.assertIn("notes.txt", shas)
        self.assertNotIn("dump.csv", shas)
        self.assertNotIn("data/rows.json", shas)

    def test_inputs_go_in_batches_and_a_cap_leaves_the_rest_pending(self):
        with mock.patch.object(embed, "BATCH", 2):
            self.store.index(self.project)
            got = self.store.embed_pending(self.project, max_cards=3)
        self.assertEqual(got["embedded"], 3)
        self.assertEqual(got["pending"], 3)
        self.assertTrue(all(len(c) <= 2 for c in self.calls))

    def test_the_card_reaches_the_model_with_its_intro(self):
        self.index()
        sent = "\n".join(t for call in self.calls for t in call)
        self.assertIn("Render invoices to PDF with line items.", sent)

    def test_a_new_summary_rebuilds_the_card(self):
        self.index()
        self.store.save_summary(self.project, "billing/invoice_pdf.py", "Builds the printable bill.", "qa")
        self.assertEqual(self.store.card_stats(self.project)["pending"], 1)
        self.store.embed_pending(self.project)
        self.assertIn("Builds the printable bill.", self.calls[-1][0])

    def test_a_dead_server_stops_embedding_quietly(self):
        self.model.stop()
        with mock.patch("memdex.embed.embed_many", return_value=None):
            got = self.index()
        self.model.start()
        self.assertEqual(got["embedded"], 0)
        self.assertEqual(got["pending"], 6)
        self.assertEqual(self.card_count(), 0)

    def test_a_cold_model_is_loaded_in_the_background_not_waited_for(self):
        self.store.index(self.project)
        with mock.patch("memdex.embed.warm", return_value=False), \
                mock.patch("memdex.embed.preload") as preload:
            got = self.store.embed_pending(self.project, seconds=2.0, only_if_warm=True)
        self.assertEqual(got["embedded"], 0)
        self.assertEqual(got["pending"], 6)
        preload.assert_called_once()
        self.assertEqual(self.calls, [])


class NoModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "shop")
        Base.write_files(self.root)
        self.store = Store(os.path.join(self.tmp, "db", "memdex.db"))
        self.project = projects.resolve(self.root)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MEMDEX_EMBED", None)

    def tearDown(self):
        self.env.stop()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_a_model_nothing_is_embedded_and_search_is_keyword_only(self):
        with mock.patch("memdex.embed.embed_many") as model:
            self.store.index(self.project)
            got = self.store.embed_pending(self.project)
            hits = self.store.search(self.project, "invoice", ["file"])
        model.assert_not_called()
        self.assertEqual(got, {"embedded": 0, "pending": 0, "model": None})
        self.assertEqual([h["path"] for h in hits][:1], ["billing/invoice_pdf.py"])

    def test_a_caller_vector_is_not_scored_against_cards(self):
        # Cards come from the configured model; a vector from the caller's own model may not match it.
        self.store.index(self.project)
        self.store.db.execute("INSERT INTO card_vectors VALUES('x','fake',1,?,0)", (embed.quantize([1.0, 0.0]),))
        hits = self.store.search(self.project, "zzz", ["file"], qvec=embed.normalize([1.0, 0.0]))
        self.assertEqual(hits, [])


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.index()
        self.session = Session(self.store, agent_name="qa")
        self.session.cwd = self.root
        self.session.home = self.root

    def test_a_question_in_other_words_finds_the_file_by_meaning(self):
        hits = self.store.search(self.project, "printable statement", ["file"])
        self.assertEqual(hits[0]["path"], "billing/invoice_pdf.py")
        self.assertEqual(hits[0]["matched_by"], ["semantic"])
        self.assertIn("Render invoices", hits[0]["snippet"])
        out = self.session.search("printable statement", kinds=["file"])
        self.assertIn("billing/invoice_pdf.py [by meaning]", out)

    def test_without_the_model_the_same_question_finds_nothing(self):
        self.store.embedding = False
        self.assertEqual(self.store.search(self.project, "printable statement", ["file"]), [])

    def test_keyword_and_vector_agreement_ranks_first(self):
        hits = self.store.search(self.project, "session cookie", ["file"])
        self.assertEqual(hits[0]["path"], "auth/session.py")
        self.assertIn("query", hits[0]["matched_by"])
        self.assertIn("semantic", hits[0]["matched_by"])

    def test_lang_and_path_filter_both_rankings(self):
        everything = {h["path"] for h in self.store.search(self.project, "dark colour theme", ["file"], 10)}
        self.assertIn("ui/palette.ts", everything)
        python_only = {h["path"] for h in self.store.search(self.project, "dark colour theme", ["file"], 10,
                                                            lang="python")}
        self.assertNotIn("ui/palette.ts", python_only)
        self.assertTrue(all(p.endswith(".py") for p in python_only))
        ts = [h["path"] for h in self.store.search(self.project, "dark colour theme", ["file"], 10, lang="ts")]
        self.assertEqual(ts, ["ui/palette.ts"])  # "ts" is recorded as js; the alias maps it
        under = {h["path"] for h in self.store.search(self.project, "invoice retry cookie", ["file"], 10,
                                                      path="./billing/")}
        self.assertEqual(under, {"billing/invoice_pdf.py"})

    def test_a_path_filter_keeps_notes_that_reference_matching_files(self):
        self.store.add_note(self.project, "pitfall", "Invoice totals round per line", "", "qa",
                            files=["billing/invoice_pdf.py"])
        self.store.add_note(self.project, "pitfall", "Invoice cookie banner", "", "qa", files=["ui/palette.ts"])
        titles = [h.get("title") for h in self.store.search(self.project, "invoice", None, 10, path="billing")]
        self.assertIn("Invoice totals round per line", titles)
        self.assertNotIn("Invoice cookie banner", titles)

    def test_since_filters_by_change_time(self):
        self.assertEqual(self.store.search(self.project, "invoice", ["file"], since=4102444800.0), [])

    def test_tool_searches_are_counted_and_internal_ones_are_not(self):
        self.store.search(self.project, "printable statement", ["file"])
        self.assertEqual(self.store.search_stats(), {})
        self.session.search("printable statement", kinds=["file"])
        self.session.search("render_invoice", kinds=["file"])
        stats = self.store.search_stats(self.project)
        self.assertEqual(stats["searches"], 2)
        self.assertEqual(stats["vector_searches"], 2)
        self.assertGreaterEqual(stats["vector_only_results"], 1)

    def test_stats_reports_vector_use_and_card_storage(self):
        self.session.search("printable statement", kinds=["file"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--db", self.store.path, "stats"])
        out = buf.getvalue()
        self.assertIn("vector search: used in 1/1 tool searches", out)
        self.assertIn("file card vectors: fake 6", out)

    def test_the_search_tool_gains_two_filters_and_no_new_tool(self):
        tools = {t.name: t for t in self.session.tools()}
        self.assertIn("lang", tools["search"].params)
        self.assertIn("path", tools["search"].params)
        self.assertFalse([n for n in tools if "vector" in n or "embed" in n])

    def test_the_brief_says_when_vectors_are_waiting(self):
        Path(self.root, "net", "backoff.py").write_text('"""Retry again."""\n', encoding="utf-8")
        with mock.patch("memdex.embed.warm", return_value=False), mock.patch("memdex.embed.preload"):
            out = self.session.brief()
        self.assertIn("1 file(s) awaiting vectors", out)


class SchemaTests(unittest.TestCase):
    def test_a_v15_database_gains_the_new_tables(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "memdex.db")
            Store(path).close()
            con = sqlite3.connect(path)
            con.execute("DROP TABLE card_vectors")
            con.execute("DROP TABLE search_stats")
            con.execute("PRAGMA user_version=15")
            con.commit()
            con.close()
            store = Store(path)
            try:
                self.assertEqual(store.schema_version, SCHEMA_VERSION)
                names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"card_vectors", "search_stats"} <= names)
            finally:
                store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class EvaluationTests(unittest.TestCase):
    def test_without_a_model_the_vector_side_is_skipped_not_guessed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMDEX_EMBED", None)
            report = evaluate_natural_language(k=5)
        self.assertIsNone(report["vectors"])
        self.assertIn("MEMDEX_EMBED", report["skipped"])
        self.assertEqual(report["case_count"], len(report["keyword"]["cases"]))
        self.assertTrue(0.0 <= report["keyword"]["all"]["recall_at_k"] <= 1.0)

    def test_with_a_model_both_sides_are_measured_on_the_same_index(self):
        embed._disabled_after_error = False
        with mock.patch.dict(os.environ, {"MEMDEX_EMBED": "ollama:fake"}), \
                mock.patch("memdex.embed.embed_many", side_effect=fake_embed_many):
            report = evaluate_natural_language(k=5)
        self.assertEqual(report["model"], "fake")
        self.assertGreater(report["cards_embedded"], 10)
        self.assertEqual(len(report["vectors"]["cases"]), len(report["keyword"]["cases"]))

    def test_keyword_side_is_deterministic(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMDEX_EMBED", None)
            a = evaluate_natural_language(k=5)["keyword"]
            b = evaluate_natural_language(k=5)["keyword"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
