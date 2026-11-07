"""A string where the schema says array: some MCP clients send "a.py;b.py" or "a.py" for `files`/`paths`.

Passed straight through, Python iterated the string, so a handoff stored one file ref per letter (seen in
the wild: a note with hundreds of refs named "d", "o", "c", ...) and a claim locked single characters.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memdex import digest, mcp
from memdex import project as projects
from memdex.store import Store
from memdex.tools import Session


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "src"))
        Path(self.root, "src", "store.py").write_text("x = 1\n", encoding="utf-8")
        Path(self.root, "src", "api.py").write_text("y = 2\n", encoding="utf-8")
        self.store = Store(os.path.join(self.tmp, "memdex.db"))
        self.session = Session(self.store, agent_name="claude")
        self.session.cwd = self.root
        self.tools = {t.name: t for t in self.session.tools()}

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, name, **args):
        resp = mcp.handle(self.session, self.tools, {"method": "tools/call", "id": 1,
                                                     "params": {"name": name, "arguments": args}})
        return resp["result"]

    def refs(self, nid):
        row = self.store.db.execute("SELECT refs FROM notes WHERE id=?", (nid,)).fetchone()
        return [r["path"] for r in json.loads(row["refs"])]


class StringArrays(Base):
    def test_handoff_files_as_a_delimited_string_become_paths(self):
        res = self.call("handoff", done="did it", files="src/store.py; src/api.py")
        self.assertFalse(res["isError"], res)
        nid = int(res["content"][0]["text"].split("#")[1].split()[0])
        self.assertEqual(self.refs(nid), ["src/store.py", "src/api.py"])

    def test_remember_files_as_one_path_string(self):
        res = self.call("remember", kind="decision", title="WAL", files="src/store.py")
        self.assertFalse(res["isError"], res)
        nid = int(res["content"][0]["text"].split("#")[1].split(".")[0].split()[0])
        self.assertEqual(self.refs(nid), ["src/store.py"])

    def test_claim_paths_as_a_string_locks_the_file_not_its_letters(self):
        res = self.call("claim", paths="src/store.py", exclusive=True)
        self.assertFalse(res["isError"], res)
        held = [r["path"] for r in self.store.db.execute("SELECT path FROM claims").fetchall()]
        self.assertEqual(held, ["src/store.py"])

    def test_duplicate_refs_are_stored_once(self):
        nid = self.store.add_note(self.session.project, "note", "t", "", "claude",
                                  ["src/store.py", "src/store.py"])
        self.assertEqual(self.refs(nid), ["src/store.py"])

    def test_arrays_still_pass_untouched(self):
        self.assertEqual(mcp._coerce({"files": {"type": "array"}}, {"files": ["a,b.py"]}),
                         {"files": ["a,b.py"]})
        self.assertEqual(mcp._coerce({"cues": {"type": "array"}}, {"cues": "old name, alias"}),
                         {"cues": ["old name, alias"]})


class OutsideRefs(Base):
    def test_a_handoff_about_another_repo_says_so(self):
        elsewhere = os.path.join(self.tmp, "other", "app.ts")
        res = self.call("handoff", done="seating shipped", files=[elsewhere])
        text = res["content"][0]["text"]
        self.assertIn("outside", text)
        self.assertIn("right project", text)

    def test_refs_inside_say_nothing(self):
        text = self.call("handoff", done="ok", files=["src/store.py"])["content"][0]["text"]
        self.assertNotIn("outside", text)


class EpisodicNotesDoNotFight(Base):
    def note(self, kind, title, body, files=None):
        return self.store.add_note(self.session.project, kind, title, body, "claude", files or [])

    def test_two_handoffs_are_not_a_contradiction(self):
        self.note("handoff", "fts fix", "Done: fixed search. Full suite passed.")
        self.note("handoff", "seating", "Done: seating. Pitfalls: no full suite passed run on device.")
        self.assertEqual(digest.contradictions(self.store, self.session.project), [])

    def test_notes_pinned_to_different_files_are_different_subjects(self):
        self.note("decision", "Always retry", "retry every request", ["src/api.py"])
        self.note("pitfall", "Never retry", "never retry the store write", ["src/store.py"])
        self.assertEqual(digest.contradictions(self.store, self.session.project), [])


if __name__ == "__main__":
    unittest.main()
