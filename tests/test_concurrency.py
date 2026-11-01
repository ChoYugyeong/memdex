"""Several memdex processes on one database must never lose a write.

One SQLite file is shared by every agent on the machine, so two Claude/Codex/
DeepSeek sessions can index, remember and claim at the same time. The contract
is: WAL + a busy timeout means everybody waits instead of failing with
"database is locked", and every write survives.

These tests spawn real processes (not threads) because the failure mode lives
in SQLite's file locking across processes.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCKED = "database is locked"


def write(root, rel, text):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.root)
        os.makedirs(self.home)
        for i in range(12):
            write(self.root, f"src/mod{i}.py", f'"""module {i}"""\n\ndef fn{i}():\n    pass\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def db_path(self):
        return os.path.join(self.home, "memdex.db")

    def env(self):
        env = dict(os.environ)
        env["MEMDEX_HOME"] = self.home
        env["PYTHONPATH"] = os.pathsep.join(x for x in (str(REPO), env.get("PYTHONPATH")) if x)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def argv(self, command, *args, agent="claude"):
        return [sys.executable, "-m", "memdex", command, "--agent", agent, *args]

    def run_cli(self, command, *args, agent="claude"):
        return subprocess.run(self.argv(command, *args, agent=agent), cwd=self.root,
                              env=self.env(), capture_output=True, encoding="utf-8", errors="replace", timeout=120)

    def spawn(self, command, *args, agent="claude"):
        return subprocess.Popen(self.argv(command, *args, agent=agent), cwd=self.root,
                                env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")

    def collect(self, procs):
        results = []
        for p in procs:
            out, _ = p.communicate(timeout=120)
            results.append((p.returncode, out or ""))
        return results

    def assert_all_clean(self, results):
        broken = [(rc, out) for rc, out in results if rc != 0 or LOCKED in out]
        self.assertEqual(broken, [], "processes must all succeed without 'database is locked'")

    def counts(self):
        con = sqlite3.connect(self.db_path)
        try:
            q = lambda table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            return {"files": q("files"), "notes": q("notes"),
                    "messages": q("messages"), "claims": q("claims")}
        finally:
            con.close()


class ColdStartTests(Base):
    def test_first_time_creation_from_many_processes(self):
        """Four processes open the same not-yet-created database at once."""
        procs = [
            self.spawn("remember", "note", f"cold {i}", "--body", "x", agent=f"a{i}")
            for i in range(4)
        ]
        results = self.collect(procs)
        self.assert_all_clean(results)
        self.assertEqual(self.counts()["notes"], 4)

    def test_parallel_index_of_the_same_tree(self):
        procs = [self.spawn("index", agent=f"a{i}") for i in range(3)]
        results = self.collect(procs)
        self.assert_all_clean(results)
        self.assertEqual(self.counts()["files"], 12)

        # a second concurrent round is a no-op, not an error
        procs = [self.spawn("index", agent=f"b{i}") for i in range(3)]
        self.assert_all_clean(self.collect(procs))
        self.assertEqual(self.counts()["files"], 12)


class ParallelWriterTests(Base):
    def test_every_note_message_and_claim_survives(self):
        self.assert_all_clean(self.collect([self.spawn("index")]))
        per_agent = 6
        procs = []
        for i in range(3):
            agent = f"agent{i}"
            for n in range(per_agent):
                procs.append(self.spawn("remember", "decision", f"{agent} note {n}",
                                        "--body", f"body {n}", agent=agent))
            procs.append(self.spawn("post", f"{agent} says hi", "--to", "deepseek", agent=agent))
            procs.append(self.spawn("claim", f"src/mod{i}.py", "--intent", "edit", agent=agent))

        self.assert_all_clean(self.collect(procs))
        got = self.counts()
        self.assertEqual(got["notes"], 3 * per_agent)
        self.assertEqual(got["messages"], 3)
        self.assertEqual(got["claims"], 3)

    def test_notes_and_messages_keep_their_content(self):
        procs = [self.spawn("remember", "pitfall", f"unique title {i}",
                            "--body", f"unique body {i}", agent="writer") for i in range(8)]
        self.assert_all_clean(self.collect(procs))
        con = sqlite3.connect(self.db_path)
        try:
            titles = sorted(r[0] for r in con.execute("SELECT title FROM notes"))
            bodies = sorted(r[0] for r in con.execute("SELECT body FROM notes"))
        finally:
            con.close()
        self.assertEqual(titles, sorted(f"unique title {i}" for i in range(8)))
        self.assertEqual(bodies, sorted(f"unique body {i}" for i in range(8)))


class ReaderWriterTests(Base):
    def test_readers_do_not_block_writers(self):
        self.assert_all_clean(self.collect([self.spawn("index")]))
        writers = [self.spawn("remember", "note", f"w{i}", "--body", "x", agent="w") for i in range(6)]
        readers = []
        for _ in range(6):
            readers.append(self.spawn("brief"))
            readers.append(self.spawn("inbox"))
            readers.append(self.spawn("file", "src/mod0.py"))
        results = self.collect(writers + readers)
        self.assert_all_clean(results)
        self.assertEqual(self.counts()["notes"], 6)

    def test_repeated_rounds_stay_clean(self):
        """Three waves of concurrent writers: no lock error and no lost note."""
        total = 0
        for wave in range(3):
            procs = [self.spawn("remember", "note", f"wave {wave} {i}", "--body", "x", agent=f"w{i}")
                     for i in range(4)]
            self.assert_all_clean(self.collect(procs))
            total += 4
            self.assertEqual(self.counts()["notes"], total)


if __name__ == "__main__":
    unittest.main()

