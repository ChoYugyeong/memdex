"""SimHash: near-identical notes stay close, unrelated ones do not, Korean included."""

import os
import shutil
import tempfile
import time
import unittest

from memdex import simhash
from memdex.project import resolve
from memdex.store import Store


class SimHashTests(unittest.TestCase):
    A = "retries need jitter, the whole herd comes back at once and the server falls over"
    A_NEAR = "retries need jitter, the whole herd comes back at once and the server falls over hard"
    B = "the postgres connection pool must be sized to the worker count"

    def test_near_identical_text_is_close(self):
        distance = simhash.hamming(simhash.fingerprint(self.A), simhash.fingerprint(self.A_NEAR))
        self.assertLessEqual(distance, 6, f"distance was {distance}")

    def test_identical_text_is_zero(self):
        self.assertEqual(simhash.hamming(simhash.fingerprint(self.A), simhash.fingerprint(self.A)), 0)

    def test_unrelated_text_is_far(self):
        distance = simhash.hamming(simhash.fingerprint(self.A), simhash.fingerprint(self.B))
        self.assertGreater(distance, 20, f"distance was {distance}")

    def test_korean_near_and_far(self):
        a = "재시도에는 지터가 필요하다 모든 요청이 동시에 몰리면 서버가 쓰러진다"
        near = "재시도에는 지터가 필요하다 모든 요청이 동시에 몰리면 서버가 완전히 쓰러진다"
        other = "포스트그레스 연결 풀은 워커 수에 맞춰야 한다"
        self.assertLessEqual(simhash.hamming(simhash.fingerprint(a), simhash.fingerprint(near)), 6)
        self.assertGreater(simhash.hamming(simhash.fingerprint(a), simhash.fingerprint(other)), 20)

    def test_empty_text(self):
        self.assertEqual(simhash.fingerprint(""), 0)
        self.assertEqual(simhash.hamming(0, 0), 0)

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(simhash.fingerprint(self.A.upper()), simhash.fingerprint(self.A.replace(",", "")))


class NearDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.project = resolve(self.root)
        self.store = Store(os.path.join(self.tmp, "memdex.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, title, body="", **extra):
        values = dict(project=self.project.key, kind="pitfall", title=title, body=body,
                      author="claude", refs="[]", created_at=time.time(), superseded_by=None,
                      archived=0)
        values.update(extra)
        cur = self.store.db.execute(
            "INSERT INTO notes(project, kind, title, body, author, refs, created_at, superseded_by, archived) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (values["project"], values["kind"], values["title"], values["body"], values["author"],
             values["refs"], values["created_at"], values["superseded_by"], values["archived"]))
        self.store.db.commit()
        return cur.lastrowid

    def test_near_duplicate_is_found(self):
        note_id = self.add(SimHashTests.A)
        rows = simhash.near_duplicates(self.store, self.project.key, SimHashTests.A_NEAR)
        self.assertTrue(rows, "nothing matched")
        self.assertEqual(rows[0][0]["id"], note_id)
        self.assertLessEqual(rows[0][1], 6)

    def test_unrelated_note_is_not_returned(self):
        self.add(SimHashTests.B)
        self.assertEqual(simhash.near_duplicates(self.store, self.project.key, SimHashTests.A), [])

    def test_archived_and_superseded_are_skipped(self):
        self.add(SimHashTests.A, archived=1)
        self.add(SimHashTests.A, superseded_by=99)
        self.assertEqual(simhash.near_duplicates(self.store, self.project.key, SimHashTests.A), [])

    def test_limit_and_order(self):
        self.add(SimHashTests.A)
        self.add(SimHashTests.A_NEAR)
        rows = simhash.near_duplicates(self.store, self.project.key, SimHashTests.A, limit=1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
