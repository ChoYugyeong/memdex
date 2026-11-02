<p align="center">
  <img src="https://raw.githubusercontent.com/ChoYugyeong/memdex/master/docs/brand/logo.png" width="380" alt="memdex — a bookmark in the night ledger">
</p>

<h1 align="center">memdex</h1>

<!-- mcp-name: io.github.ChoYugyeong/memdex -->

<p align="center">
  <b>Shared, token-saving memory for AI coding agents.</b><br/>
  One SQLite file. Zero dependencies. Any MCP client. Claude Code, Codex, Cursor and DeepSeek can all remember the same things — and talk to each other.
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/ChoYugyeong/memdex/master/docs/brand/demo.gif" width="860" alt="Two agents, one checkout: claim, inbox and handoff in memdex">
</p>

<p align="center">
  <a href="https://github.com/ChoYugyeong/memdex/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ChoYugyeong/memdex/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/memdex-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/memdex-mcp?label=pypi"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/github/license/ChoYugyeong/memdex"></a>
  <a href="https://glama.ai/mcp/servers/ChoYugyeong/memdex"><img alt="Glama MCP score" src="https://glama.ai/mcp/servers/ChoYugyeong/memdex/badges/score.svg"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#tools">Tools</a> ·
  <a href="#cli">CLI</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#how-it-compares">Comparison</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="README.ko.md">한국어</a>
</p>

---

Every new agent session starts from nothing. It re-reads the same 3,000-token files, re-runs the same
searches and re-discovers the same pitfalls — and you pay for every token of it. When several agents work
on one machine, they can't see what the others learned, or that another one is editing the same file right now.

memdex is a small MCP server that fixes both:

- **A file summary written once stays valid until the file's content changes.** Summaries are keyed to the
  sha256 of the content, not the path. The next session — or the next agent — gets a 100-token summary
  instead of the 3,000-token file, and is told the moment it goes stale.
- **Notes know when they rot.** Decisions, pitfalls and handoffs cite the files they're based on; when those
  files change, the note is flagged.
- **Agents coordinate.** `claim` what you're editing, `post` to other agents, read your `inbox`, leave a
  `handoff` for whoever comes next. All worktrees of a repository share one memory.

```text
$ memdex file src/store.py
src/store.py · python · 444 lines · sha 3adeaf13
summary (claude@memdex, 2h ago, matches current content):
SQLite store. Tables: files, summaries (keyed by sha), symbols, notes, messages, claims, fts.
index() skips files whose stat is unchanged. search() fuses FTS5 and embeddings with RRF.
outline: class Store:53; method Store.index:101; method Store.search:280; …
```

That response is 1.5 KB. The file is 22 KB. Nothing was sent to any model to produce it.

Listed in the official [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.ChoYugyeong/memdex` and on PyPI as `memdex-mcp`.

On a real 66-file site repo, `memdex bench` measures **154,105 tokens** to read every file versus **3,471 tokens**
of outlines — 98% smaller, before an agent saves a single summary. Run `memdex bench` in your own checkout;
the token count is `chars / 4`, an estimate, and the ratio is the point.

## Quick start

```bash
pipx install memdex-mcp        # PyPI name is memdex-mcp; the command is `memdex`
memdex connect --agent claude  # write the MCP entry into the agents on this machine
```

**Claude Code** (user scope, every project) — when the `claude` CLI is on PATH, `memdex connect` runs this
line for you, and prints it when it is not:

```bash
claude mcp add memdex --scope user -e MEMDEX_AGENT=claude -- memdex serve
```

**Everything else that keeps a config file** — Claude Desktop, Cursor, VS Code (GitHub Copilot), Windsurf,
Zed, Cline, Gemini CLI and Codex CLI: `memdex connect` finds each one that is already installed, merges
`mcpServers.memdex` into its config (`servers.memdex` with `type: stdio` for VS Code,
`context_servers.memdex` for Zed, `[mcp_servers.memdex]` for Codex), keeps the rest of the file byte for
byte, and saves a `.bak` before it writes. A client whose config folder does not exist is reported as not
installed and left alone; a config file it cannot parse is left exactly as it was, and a Zed settings file
with comments gets the block printed to paste instead of being rewritten (a rewrite would drop the
comments). `--list` shows what is installed and configured without writing, `--dry-run` shows the writes,
`--remove` takes the entry out.

**`--serve-check`** is the honest end of setup: after writing, memdex starts the server from the entry it
just wrote and speaks MCP to it (initialize, then `tools/list`), so a wrong path, a missing interpreter or a
database it cannot open is caught now instead of in the agent:

```text
+ serve-check      memdex answered: 13 tools, protocol 2024-11-05
```

**Team scope** — put the config in the repo so everyone who clones it gets memdex:

```bash
memdex connect --project     # .vscode/mcp.json and .mcp.json, written with a bare `memdex` command
```

A committed config cannot carry this machine's absolute path, so project scope writes `command: "memdex"`
and the teammate's PATH has to find it (`pipx install memdex-mcp`). With the `claude` CLI on PATH and
`--project`, the CLI writes `.mcp.json` itself (`claude mcp add memdex --scope project …`) and memdex writes
only the VS Code file, so one file never has two owners. `--no-claude` skips the CLI entirely, and
`--client claude-code` runs only that step.

**By hand**, or for a client memdex does not know about:

```json
{ "mcpServers": { "memdex": { "command": "memdex", "args": ["serve"], "env": { "MEMDEX_AGENT": "codex" } } } }
```

Give each agent its own `MEMDEX_AGENT` name; its id becomes `name@checkout` (e.g. `claude@myrepo-wt`).
`memdex config --agent <name>` prints the snippets above, plus an optional Claude Code `SessionStart` hook
that injects the project brief automatically.

That's it. The server sends its own usage instructions in the MCP `initialize` response, so agents know
the workflow (brief → `file_summary` before reading → `remember` → `handoff`) without extra prompting.

## How it works

```text
          agents (Claude Code, Codex, Cursor, DeepSeek, …)
               │ MCP stdio        │ CLI
               ▼                  ▼
        ┌──────────────── memdex ────────────────┐
        │ project_brief · file_summary · where_is │
        │ read_snippet · search · remember        │
        │ handoff · post/inbox · claim            │
        ├─────────────────────────────────────────┤
        │ incremental index   outlines + symbols  │   only files whose size/mtime
        │ (stat → sha256)     (regex/ast, no LLM) │   moved are re-read
        ├─────────────────────────────────────────┤
        │ summaries keyed by sha256               │   valid until content changes
        │ notes with file refs → stale flags      │
        │ messages, cursors, claims (expiring)    │
        ├─────────────────────────────────────────┤
        │ SQLite WAL · FTS5 trigram · vectors     │   ~/.memdex/memdex.db
        └─────────────────────────────────────────┘
```

| | What it does | Borrowed from |
|---|---|---|
| **Content-hash summary cache** | A summary is stored under the file's sha256. Same content in another worktree or branch shares it; changed content gets "no summary yet, here is the old one and the outline". | content-addressed storage |
| **Incremental index** | `git ls-files` (honours `.gitignore`), then only files whose size or mtime moved are read. An 800-file repo re-indexes in ~0.2 s. | [Everything](https://www.voidtools.com/)'s USN-journal approach |
| **Outlines + symbol map** | Docstring/comment head plus classes, functions and line numbers for 30+ languages (Python via `ast`, the rest via per-language regexes — no tree-sitter, no parser install). `where_is` replaces repeated grepping. | Serena / repo maps |
| **Bounded reads** | `read_snippet` returns ~256 tokens around a match or a line instead of the whole file. | [Kensaku](https://github.com/yjid/kensaku) |
| **Rot-aware notes** | `remember` links a note to files; `search` and `project_brief` show "⚠ changed since" when they move. Notes are append-only (`supersedes`), so concurrent writers never clobber each other. | Basic Memory, mem0 |
| **Agent coordination** | `claim` with expiry and overlap warnings; `post`/`inbox` with per-agent cursors; `handoff` as the first thing the next session sees. | — |
| **Hybrid search** | FTS5 trigram (works for Korean, Japanese and Chinese; 2-character terms fall back to LIKE) fused with optional local embeddings via reciprocal rank fusion. | claude-context |
| **Whole-machine content** | If [Kensaku](https://github.com/yjid/kensaku) is installed, `search` also returns full-text, OCR and semantic hits from PDFs, Office files and screenshots. | Kensaku |

Supported outline languages: Python, JavaScript/TypeScript, Vue, Svelte, Go, Rust, Java, Kotlin, Scala,
C#, Swift, Dart, C, C++, Ruby, PHP, Lua, GDScript, Elixir, Clojure, Zig, Julia, R, Haskell, Groovy,
Protobuf, GraphQL, SQL, shell, PowerShell, Markdown.

## How memdex ranks and forgets

Memory that only grows is as useless as no memory: the brief has to pick a few lines out of hundreds. Every
note gets an **activation**, after ACT-R's base-level learning — the sum of `t^-0.5` over its uses, so a note
read five times last week outranks one written yesterday and never touched. Pitfalls and decisions carry a
prior (ignoring them costs more), and `--importance` raises a note further.

Three things follow from that score:

- **Pinned core** (`--pin`): a small fixed slot at the top of every brief for the handful of facts every
  session must see — a system prompt written by the agents themselves.
- **Cold tier**: below an activation of −3 a note stays stored and searchable but stops spending brief and
  default-search budget. Nothing is deleted; `search` still finds it. Activation is *retrieval* strength;
  a note that keeps earning confirms has *storage* strength and stays in the brief even when it has not
  been needed for a while (Bjork & Bjork 1992) — forty sessions used it, this month's did not.
- **Interference**: when a note is superseded, the surviving notes that share its files rank a little
  lower, because somebody already found one of them wrong (retrieval-induced forgetting, Anderson, Bjork &
  Bjork 1994). They are ranked lower, never hidden.
- **Reconsolidation**: when a note's files or symbols have changed, `note` prints `⚠ changed since`, and
  `note(id, "confirm")` re-stamps its refs to the current code. Confirming is also a use, so a note that keeps
  being re-verified keeps its place.

Staleness is tracked per symbol, not per file: a note about `Store.claim` goes stale when that function
changes, and stays fresh when other functions in the same file change.

When you save a note, memdex compares it against the project's active notes with a 64-bit SimHash over word
n-grams. At ≤12 bits of distance it points at the likely duplicate ("similar to #17 — supersede it instead").
The agent decides; memdex never merges memory on its own.

## Tools

| Tool | When | What you get |
|---|---|---|
| `project_brief` | Start of session | Last handoff, pitfalls, decisions, todos, unread messages, who is editing what. Refreshes the index. |
| `file_summary` | Before reading a file | Cached summary if the content is unchanged, otherwise the old summary (marked stale) and a free outline. |
| `file_summary_save` | After reading a file | Stores 2–6 lines for everyone, keyed to the content hash. |
| `where_is` | "Where is X defined?" | `path:line kind name` matches. |
| `read_snippet` | Reading one part of a file | A bounded slice around `query` or `line`. |
| `search` | Before exploring from scratch | Notes, handoffs and summaries; keyword + semantic; Kensaku hits when available. |
| `remember` / `note` | Something non-obvious | Save / read / archive a `note`, `decision`, `pitfall` or `todo`, optionally linked to files. |
| `handoff` | End of session | Done / next / decisions / pitfalls; releases your claims. |
| `post` / `inbox` | Talking to other agents | Broadcast or `to: "deepseek"`; `inbox` marks read unless `peek`. |
| `claim` | Before editing | Announce paths for N minutes; warns about other agents' overlapping claims. |

The 12 tool schemas cost about 1,000 tokens per request. Responses are short plain text — every byte is a token.

## CLI

Everything the tools do is also a command, for shell-only agents, hooks and humans:

```bash
memdex brief --agent codex              # start of session
memdex file src/store.py                # cached summary + outline
memdex file src/store.py --summary "…"  # save a summary
memdex snippet src/store.py --query "def index"
memdex where_is index                   # where a symbol is defined
memdex search "locked"
memdex remember pitfall "WAL mode needed" --body "…" --files src/store.py
memdex remember decision "sqlite only" --importance 3 --pin
memdex claim src/store.py --intent "refactor"
memdex claim src/store.py --exclusive  # lock: other agents' claims on it are refused
memdex post "store.py is mine for an hour" --to claude
memdex inbox
memdex inbox --follow                    # wait for the next message (mailbox, polled; --timeout N)
memdex note 7                            # read it
memdex note 7 --confirm                 # it still holds: re-stamp its refs, clear the ⚠
memdex note 7 --pin                     # always at the top of the brief
memdex note 7 --archive                 # read a note, or archive it
memdex handoff "Done X" --next "Y" --pitfalls "Z"
memdex index                            # refresh the index for the current project
memdex export --out notes/              # notes as Markdown, one file each
memdex export --sync ../shared          # notes + summaries as JSON, for another machine
memdex merge ../shared                  # apply what the other machine exported (LWW by updated_at)
memdex digest                           # notes that say the same thing, for an agent to consolidate
memdex import                           # read AGENTS.md / CLAUDE.md into notes (cold start)
memdex stats
memdex bench                            # what the outlines save on this repo
memdex prune --dry-run                  # drop index rows of deleted projects
memdex doctor                           # database, schema, project index, optional integrations
memdex doctor --serve-check             # …and start the server to verify the MCP handshake
memdex connect --agent claude           # write the MCP entry into each installed client's config
memdex hook install                     # git pre-commit: refuse commits on a locked file
memdex skills                           # skills this machine's agents share
memdex skills scan                      # (re)read the SKILL.md files, register them
memdex skills show deploy               # print one skill so an agent can follow it
```

## Locks and shared checkouts

Advisory claims are enough between friendly agents, but they do not stop a `git commit`. Two commands close that gap:

```bash
memdex claim src/store.py --exclusive --intent "rewrite the retry loop"
memdex hook install
```

`--exclusive` makes the claim a lock: another agent's claim on that path is refused, and their commits are
refused while it is live. `--force` records the claim anyway and tells them why. Every claim expires, so a
session that dies cannot lock you out forever.

`memdex hook install` writes `.git/hooks/pre-commit` (a small `sh` script that calls `memdex hook pre-commit`);
it only checks the staged files and only reads the local database:

```
$ git commit -m "retry loop"
memdex: commit blocked — these staged files are locked by another agent:
  src/store.py  ←  claude@memdex (rewrite the retry loop, 41m left)
```

An advisory claim still commits — it just prints a note. `MEMDEX_ALLOW_LOCK=1 git commit …` skips the check
for a lock you know is stale, and `--force` on `hook install` replaces a hook that memdex did not write.

A commit is late, though: by then the edit already happened. Harnesses that expose a pre-tool hook can refuse
the edit itself. Claude Code has `PreToolUse`:

```bash
memdex hook install --claude            # ~/.claude/settings.json
memdex hook install --claude --project  # ./.claude/settings.json
```

```json
{"hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit",
  "hooks": [{"type": "command", "command": "memdex hook pre-edit --agent claude"}]}]}}
```

The hook reads Claude Code's JSON on stdin, turns `tool_input.file_path` into a project-relative path, and
exits 2 (blocked, with stderr shown to the model) when another agent holds an exclusive claim there:

```
blocked: locked by deepseek@memdex (refactor, 40m left) — post to them or claim --force
```

Advisory claims print a note and let the edit through, edits outside the checkout are ignored, and
`MEMDEX_ALLOW_LOCK=1` bypasses the check. The installer merges into an existing `settings.json` (backing it
up as `.bak`) and never rewrites a file it cannot parse. `memdex config` prints the same snippet.

A lock is a lease with a number. Every grant carries a fencing token and the sha of the file it was taken
against, so a lock can be told apart from the one before it — and an edit computed from a stale read is
refused instead of overwriting what moved underneath:

```
blocked: src/store.py changed since you claimed it (token 7, sha 3adeaf13 → b71f0c92).
Re-read the file, then re-stamp the lock:
  memdex claim src/store.py --exclusive --refresh
```

`--refresh` re-stamps your own claim (new token, current sha); it never takes a new lock and never overrides
someone else's.

## Measure it on your repo

The first thing anyone asks is "how much does this actually save?". Run it yourself:

```bash
cd your-repo && memdex bench
```

```
your-repo: 812 files · 3.4 MB indexed in 0.21s (812 changed)
reading every file whole: ~871,000 tokens
outline instead:          ~96,120 tokens  (89% smaller; 640 files have symbols)
biggest wins (file → outline):
  src/generated/api.ts                            144.0 KB →    1.6 KB  ( 99% smaller)
  src/store.ts                                     22.2 KB →    1.0 KB  ( 95% smaller)
```

`--json` prints the same numbers for scripts. Tokens are estimated as `chars / 4`; the ratio is the point,
not the absolute number — and `file_summary` gets smaller still once agents save real 2–6 line summaries.

## Maintenance

One SQLite file is shared by every project on the machine, so it grows. `memdex prune` drops index rows for
project directories that no longer exist, then VACUUMs:

```bash
memdex prune --dry-run     # report only
memdex prune               # drop stale file index, VACUUM
memdex prune --all         # also notes/messages/claims of missing projects (destructive)
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MEMDEX_AGENT` | `agent` (or the MCP client's name) | Who this agent is. Shown on notes, messages and claims. |
| `MEMDEX_HOME` | `~/.memdex` | Where `memdex.db` lives. Nothing is ever written inside a repository. |
| `MEMDEX_PROJECT` | current directory | Project root, when the server is not started inside it. |
| `MEMDEX_EMBED` | off | `ollama:<model>` turns on semantic search, e.g. `ollama:bge-m3` (multilingual). |
| `MEMDEX_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint. Unreachable → silent fallback to keyword search. |
| `MEMDEX_KENSAKU` | `kensaku-cli` on PATH | Path to Kensaku's CLI, or `off`. |

**What is never read:** files that look like secrets (`.env*`, `*.pem`, `*.key`, `*secret*`, `*credential*`,
`.npmrc`, …), binaries, lockfiles, files over 512 KB and anything git ignores.

**Project identity:** the git common directory. All worktrees of a repository share notes, messages and claims;
each worktree keeps its own file index.

## Limits (read this before adopting it)

memdex is honest about what it is not. [docs/LIMITS.md](docs/LIMITS.md) takes each item below and records
what removing it would take, and which published technique is the cheapest honest answer:

- **The bench baseline is generous.** `memdex bench` compares "read every file whole" with "take the outline";
  a real agent greps and reads slices, so the saving is smaller than 98%. `memdex stats` reports the usage
  signal instead (summaries served fresh, notes read by another agent, re-confirmed after a change, cold notes),
  which is the honest number.
  The win that does hold is that nobody re-discovers the same file, symbol or pitfall twice.
- **Nothing is shared between machines.** One SQLite file, local only. There is no team sync, no cloud, no SSO —
  by design, but it does mean it cannot replace a team knowledge base.
- **Locks are enforced where memdex is asked, not at the file system.** `claim(exclusive=true)` refuses other
  agents' claims, the `pre-edit` hook (Claude Code) refuses the edit, and the pre-commit hook refuses the commit —
  but memdex cannot stop an editor writing bytes through a path it never sees: a session without the hook, or a
  tool that edits outside the checkout, can still write. Git is still the source of truth.
- **Messages are a mailbox, not a push.** `post` writes to the database and the recipient sees it on its next
  `inbox`/`brief`. A closed session is not notified — nothing runs in memdex while no session is up — so
  `post` now says whether the message was handed to a live reader or is waiting, and `inbox --follow` is the
  way to wait for one. Two sessions must also use different `MEMDEX_AGENT` names: with one shared id, inbox
  cannot tell the sender from the reader.
- **A tiny repo may not need it.** Under ~20 files, a hand-written `AGENTS.md` is cheaper than any tool.
- **The first brief on a huge checkout is partial.** Indexing runs at about 0.4 ms per file (20,000 files ≈
  8 s), but a first read of a fresh clone costs far more on Windows, where the antivirus scans each new file.
  So the brief indexes at most `INDEX_BUDGET` (2,000) files per call and says how many are left; the next brief
  continues. `memdex index` is uncapped, `MEMDEX_INDEX_BUDGET=0` lifts the cap, `memdex brief` never blocks
  for more than that one batch.
- **It is young.** One maintainer. Schema v5 with in-place migrations from every earlier version; a database
  written by a newer memdex is refused rather than silently corrupted. Expect rough edges.
- **Outlines are regex-level, not semantic.** Python uses `ast`; everything else is per-language regexes. Expect
  good results on normal code and occasional misses in exotic syntax. `where_is` still falls back to substring search.

## How it compares

memdex is deliberately small. These tools are good at things memdex doesn't try to do, and several of them inspired it.

| | Focus | Dependencies | Relationship |
|---|---|---|---|
| **memdex** | Cross-session, cross-agent memory: content-hash summaries, rot-aware notes, coordination | none | — |
| [Serena](https://github.com/oraios/serena) | Precise symbol navigation and editing through language servers | LSP servers per language | Use both: Serena for editing, memdex for memory. memdex's `where_is` is a regex-level stand-in. |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | Markdown knowledge base with a graph of notes | Python packages, Markdown files in your project | memdex keeps notes out of the repo and ties them to file hashes. |
| [mem0](https://github.com/mem0ai/mem0) | Memory layer for LLM apps, hosted or self-hosted | vector DB / LLM | memdex is local-only and aimed at coding agents on one machine. |
| [claude-context](https://github.com/zilliztech/claude-context) | Vector search over a codebase | Milvus/Zilliz, embedding API | memdex searches notes and summaries; code search stays keyword + symbols, optionally embeddings via Ollama. |
| [Kensaku](https://github.com/yjid/kensaku) | Desktop search: file names, PDF/Office text, OCR, semantic, for humans and agents | Rust desktop app | Complementary: memdex bridges to it for whole-machine content search. |

## FAQ

**Does anything leave my machine?** No. SQLite file, local subprocesses, optional local Ollama. No telemetry.

**Who writes the summaries?** The agents, when they read a file that has no fresh summary. The server's
instructions ask for 2–6 lines. You can also write them: `memdex file path --summary "…"`.

**What if two agents edit the same file?** `claim` warns, it doesn't lock. memdex coordinates; it doesn't
replace git.

**How much context does it cost?** About 1,000 tokens of tool schemas per request, a few hundred for
`project_brief` (hard-capped at 4,000 chars; `memdex brief --max-chars 0` shows everything).
It pays for itself the first time a file isn't re-read.

**Something is broken — what do I run?** `memdex doctor`. It reports the database path, size, schema version,
whether it is writable, whether the current project is indexed, which installed agents carry the MCP entry,
and which optional integrations are on. Add `--serve-check` and it starts the server the way an agent would
and speaks MCP to it, so "installed but cannot start" shows up as a ✗ instead of a mystery:

```text
✓ serve-check     memdex answered: 13 tools, protocol 2024-11-05
```

**Can I see or edit the memory?** `memdex export` writes every note as a Markdown file. The database is plain
SQLite. Delete `~/.memdex` to start over.

**Why not just a vector database?** Exact names beat embeddings for code; embeddings help for fuzzy
recollection of notes. memdex does keyword + symbols by default and adds vectors only when you turn them on.

## Brand

The identity is **Ledger Ribbon**: a bookmark left in the file, set in Hahmlet, on the night ledger's ink with one
amber rule.

- Logo: [`logo.svg`](docs/brand/logo.svg) · dark ground: [`logo-on-dark.svg`](docs/brand/logo-on-dark.svg) · symbol: [`symbol.svg`](docs/brand/symbol.svg)
- Colour: Ledger Ink `#0e1a1c` · Lamp Amber `#f0b03f` · Bone `#ece5d6`
- Type: Hahmlet (display) · Gothic A1 (text) · Nanum Gothic Coding (mono) — all OFL 1.1
- Tokens: [`tokens.css`](docs/brand/tokens.css) · guidelines: [`memdex-guidelines.pdf`](docs/brand/memdex-guidelines.pdf) (13 pages) · notes in [`docs/brand/`](docs/brand/README.md)

## Contributing

```bash
git clone https://github.com/ChoYugyeong/memdex && cd memdex
pip install -e .
python -m unittest discover tests     # 363 tests, ~35 s, spawns real processes for the concurrency suite
```

Ground rules are short: no runtime dependencies, keep tool descriptions terse (they are tokens on every
request), and every outline regex captures the symbol name in group 1. See [CONTRIBUTING.md](CONTRIBUTING.md).

memdex was built by Claude Code and DeepSeek working on the same checkout — coordinated through memdex itself.

## License

[MIT](LICENSE)


