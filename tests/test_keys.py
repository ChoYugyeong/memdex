"""Key expansion: the query "store index" finds `StoreIndex`, `store_index` and `memdex/store.py`."""

import unittest

from memdex import keys


class ExpandTests(unittest.TestCase):
    def test_camel_case_is_split(self):
        self.assertIn("store index", keys.expand("StoreIndex persists summaries"))

    def test_snake_case_is_split(self):
        self.assertIn("fts map", keys.expand("the fts_map table maps rows"))

    def test_path_is_split(self):
        text = keys.expand("see memdex/store.py for the writer")
        self.assertIn("memdex store py", text)

    def test_original_text_is_kept_first(self):
        original = "StoreIndex keeps a cache"
        self.assertTrue(keys.expand(original).startswith(original))

    def test_duplicates_are_dropped(self):
        text = keys.expand("StoreIndex and StoreIndex again")
        self.assertEqual(text.count("store index"), 1)

    def test_plain_words_are_untouched(self):
        self.assertEqual(keys.expand("the store keeps a cache"), "the store keeps a cache")

    def test_korean_is_untouched(self):
        self.assertEqual(keys.expand("재시도에는 지터가 필요하다"), "재시도에는 지터가 필요하다")

    def test_empty(self):
        self.assertEqual(keys.expand(""), "")

    def test_length_is_capped(self):
        long_text = " ".join(f"SomeIdentifier{i}" for i in range(500))
        out = keys.expand(long_text, max_extra=200)
        self.assertLessEqual(len(out), len(long_text) + 200 + 1)

    def test_search_keys_include_both_forms(self):
        text = keys.expand("StoreIndex")
        for fragment in ("StoreIndex", "store index"):
            self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
