# HN answers, rehearsed

The comments a Show HN thread actually produces, with an answer that is true to the code and three lines long
(HN rewards a short, specific reply and punishes a paragraph of positioning). Facts to keep straight (0.5.0):

- What it is: handoffs, a task ledger, messages and file locks for coding agents from different vendors on one
  machine. One SQLite file, Python standard library only, MIT, `memdex-mcp` on PyPI.
- 10 tools listed by default, about 1,300 tokens of instructions and schemas per request (0.4.2: about 3,500).
  17 tools in all; the rest are opt-in with `MEMDEX_TOOLS` and stay callable.
- 793 tests, schema v17, CI on Linux/macOS/Windows × Python 3.9/3.13. One maintainer.
- Real use on the author's machine, 2026-11-01 → 11-19: 493 handoffs across 40 projects, 242 messages between
  agents (watchdog notices excluded), 241 ledger tasks of which 215 self-checked and 6 checked by a different
  agent; 45 file summaries saved, served fresh 0 times. Only 6 handoffs were drafted automatically so far: the
  hook shipped on 11-19.
- Everything below is checkable in the repo: the named file or command is the answer.

---

## "Why not just a shared AGENTS.md?"

It holds rules, not state. It cannot say who is editing `store.py` right now, that the last session stopped
halfway through task 7, or that a reply is waiting for you. Keep the `AGENTS.md`; memdex carries what changes
between sessions, and `memdex import` reads the file into notes on a cold start.

## "How is this different from claude-mem?"

claude-mem makes one agent remember its own sessions by capturing everything through hooks and compressing it
with a model. memdex records less: what agents hand over, or what the hook can prove from the transcript and git,
with no model call. Its job is the hand-off between different agents. If you only run Claude Code, claude-mem is
probably the better fit.

## "beads already does agent task tracking."

It does, and better if you need a dependency graph synced across machines. memdex's ledger is a local tree with
blockers and one opinion: a report is not done until someone checks it, and it records whether the checker was
the same agent. Use beads for the tracker; the two do not conflict.

## "Memorix does all of this and more."

Yes, it is the closest project and has a much larger surface (git memory, dashboard, orchestration, Node). memdex
is the small one: standard library only, one file you can delete, 10 tools by default. If you want the features,
use Memorix; if you want fewer moving parts, this.

## "The auto handoff reads my transcript. Where does that go?"

Nowhere: it is a note in the local SQLite file, written without a model call. The hook takes the first request,
the last message and the files the agent's edit tools wrote; secret-looking paths are dropped, token-shaped
strings redacted, quotes clipped. `MEMDEX_AUTO_HANDOFF=0` turns it off. Code: `memdex/autohandoff.py`.

## "Agent names are self-declared, so 'verified' means nothing."

Right, and the README says so: `MEMDEX_AGENT` is a label, not an identity. `verified` means a different label
recorded a check, which is a cooperative signal between agents you run yourself, not proof. My own data shows how
rarely it happens (6 of 241 tasks), which is the part I want to make easier, not the part I want to oversell.

## "Your numbers are from your own machine."

They are: one person, five agents, 19 days, and the post says so. The point of showing them is the negative
result (45 summaries, served 0 times), which is why 0.5.0 changed. `memdex stats` prints the same counters for
your machine, including how many handoffs reached an agent that did not write them.

## "1,300 tokens per request is still a lot. Claude Code defers MCP schemas anyway."

It defers the schemas, but the server instructions stay in context, and other clients send everything. 1,300 is
the default for 10 tools; `memdex doctor` prints your number. Before 0.5.0 it was about 3,500 to 4,000.

## "What stops two agents clobbering each other? Advisory locks are not locks."

A plain `claim` only warns. `claim --exclusive` refuses overlaps, a Claude Code `PreToolUse` hook refuses the edit
itself (exit 2, reason shown to the model), and a git `pre-commit` hook refuses the commit. Each lock carries a
fencing token and the file's sha, so an edit computed from a stale read is refused. Git stays the source of truth;
an editor that never asks memdex can still write.

## "Two machines? A team?"

`memdex export --sync DIR` / `memdex merge DIR`: one JSON file per note and summary in a directory you already sync
(git, Syncthing, a shared folder), last writer wins, discarded writes reported. No server, no network code. Claims
and the ledger never travel, so this is "reconcile when they meet", not real time.

## "What breaks first?"

Only Claude Code has the session-end hook; other agents must remember `memdex handoff --auto`. Agent names are
self-declared. The maintainer count is one. `memdex doctor --serve-check` is the first thing to run when something
looks wrong: it starts the server and speaks MCP to it.
