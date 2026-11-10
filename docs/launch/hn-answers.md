# HN answers, rehearsed

The comments a Show HN thread actually produces, with an answer that is true to the code and three lines long
(HN rewards a short, specific reply and punishes a paragraph of positioning). Facts to keep straight:

- Optional semantic search (0.4.2): per-file metadata cards embedded by a local Ollama model, fused with
  keyword search; off by default. Eval is 11 questions on one repo (Recall@5 64% → 100%) — say so.
- 605 tests, schema v16, zero runtime dependencies, one SQLite file, MIT, 0.4.2 on PyPI.
- `memdex bench` on a 66-file repo: 154,105 tokens whole-file vs 3,471 of outlines — and that baseline is
  generous, so say so before someone else does.
- Everything below is checkable in the repo: the named file or command is the answer.

---

## "Why not just write a CLAUDE.md / AGENTS.md?"

A prose file cannot expire. A summary here is keyed to the file's sha256, so the moment the bytes change it is
marked stale — that is the whole difference. And a prose file is read by one session at a time; the store is read
by every agent on the machine, with `claim`/`handoff`/`inbox` between them.

It is not either/or: `memdex import` reads `AGENTS.md` into notes with refs, so the prose you already wrote
becomes the seed of the memory instead of its replacement.

## "Why not a vector database? Semantic search is the point."

For code, exact identifiers beat embeddings — `where_is` and FTS on split identifiers find `Store.index` without a
model. Embeddings are an optional third ranking (Ollama locally, or `remember(embedding=[…])` if your agent
already calls a model), fused with RRF, and below ~10⁴ notes brute-force cosine is exact and fast enough.

A vector DB would add a service to run for a recall gain I could not measure on real notes.

## "How is this different from Serena / an LSP?"

Different job: Serena edits with language-server precision, memdex remembers between sessions and coordinates
agents. `where_is` is deliberately a regex-level stand-in, not an LSP — if it misses, use Serena.

They compose: Serena for the edit, memdex for what the next session needs to know.

## "Isn't this just MemGPT / mem0 / a hosted memory layer?"

Those serve LLM applications and usually assume a service and a model. This is the opposite bet: one SQLite file
in `~/.memdex`, nothing leaves the machine, no model call inside memdex, and the client is a coding agent's stdio
MCP connection.

The trade is that nothing is shared between machines by default — see the sync answer.

## "Two machines? Team?"

`memdex export --sync DIR` / `memdex merge DIR`: one JSON file per note and summary into a directory you already
sync (git, Syncthing, a shared folder), merged last-writer-wins and reporting what it discarded. Summaries are
content-addressed by sha, so identical content merges for free.

No server and no network code. Claims never travel — a lease that outlives its machine is a deadlock generator,
which is why the answer is "not real-time" rather than "we built a CRDT".

## "98% smaller is not real."

Correct, and the README says it: the bench compares reading every file whole against taking the outline, and a
real agent greps and reads slices. The number that holds is that nothing is rediscovered twice — `memdex stats`
reports that instead (summaries served fresh, notes read by an agent other than the author, notes re-confirmed
after the code moved).

## "What stops two agents clobbering each other? Advisory locks are not locks."

They are not, and the README says so. What exists: TTL claims, an exclusive claim that refuses overlaps, a git
`pre-commit` hook, and a Claude Code `PreToolUse` hook that refuses the edit itself (exit 2, reason shown to the
model). Each lock carries a fencing token and the file's sha, so an edit computed from a stale read is refused
and has to be re-based with `claim --refresh`.

Git is still the source of truth; this reduces the collision rate, it does not replace version control.

## "Why SQLite and not git-notes / a directory of files?"

Git is content-addressed source of truth; memdex stores what git does not want: summaries keyed to a sha (not to a
path), notes with file refs and staleness, claims, inbox. WAL mode lets several agents read while one writes, and
the index commits every 50 files so another agent's write never waits long.

Opening `~/.memdex/memdex.db` with any SQLite client is the whole inspection story.

## "Zero dependencies, but the MCP protocol?"

The stdio protocol is JSON-RPC over stdin/stdout — a few hundred lines in `memdex/mcp.py`, no SDK. `zero
dependencies` means `pipx install memdex-mcp` pulls nothing else, which matters when the install is one line in a
post.

## "What breaks first?"

Three things, honestly: outlines are regex-level outside Python (unusual syntax is missed); a huge fresh checkout
takes seconds to index and is capped at 2,000 files per brief (`MEMDEX_INDEX_BUDGET=0` lifts it); and the
maintainer count is one.

`memdex doctor --serve-check` is the first thing to run when something looks wrong — it starts the server and
speaks MCP to it.
