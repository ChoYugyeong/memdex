# Changelog

All notable changes to memdex are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `memdex inbox --follow [--timeout N] [--poll S]`: wait for the next message instead of returning empty.
  Delivery is a mailbox, so this is the one way a session — or a supervisor watching a closed one — can wait
  without inventing a daemon.
- `memdex contradictions`: the store's own lie detector. Two notes where one asserts a term the other
  negates ("always sync" vs "never sync") are paired and listed with the terms they disagree on and the
  files they share, for the next session to settle. It is cue-based, not semantic — the docstring says what
  it will and will not catch — and the brief names the top pair with "→ settle before editing".
- `memdex whoami`: session identity and what is waiting in one read and no index pass — agent id and
  checkout, DB path and schema, the last handoff's Next line, your claims, unread count, contradiction
  count. The fast answer to "did I land in the wrong session?", where `brief` is the slow one.
- `post` reports the delivery contract: `not delivered to an inbox yet; it waits for codex's next one`, or
  `codex's last inbox read was 3h ago; it waits for the next one`. Reading the brief counts unread messages
  but consumes nothing, so "not delivered" is the accurate state until `inbox` runs. It also says so when the recipient id
  is the sender's own (`you are codex@demo … a session with the same id would never see this as unread`),
  which is the one silent way a message never arrives.
- `memdex skills suggest <paths…>` and the brief's "skills for what's next" line: which skills fit the
  files a session is about to touch. Two layers, chosen automatically — file stems plus extension hints
  scored against names and descriptions (always on), and cosine over an embedding backend when one is
  configured (`MEMDEX_EMBED=ollama:…`), with skill vectors cached per content hash and a small per-call
  embedding budget so the brief stays fast.
- `memdex skills recommend <names…>`: a project names the skills its work is done with, in
  `.memdex/skills.json` (commit it for the team); the brief then starts with "project skills: …" instead of
  every session rediscovering them one search at a time.

### Fixed
- An agent id that already carried its checkout — `MEMDEX_AGENT=codex@demo`, or the id copied straight out
  of the brief — no longer grows a second suffix (`codex@demo@demo`). The doubled id keyed the read cursor
  under a name no other session used, so two sessions of "the same" agent did not share read state.
- A recipient typed with any other case or padding (`post --to Claude`, `--to " claude "`) is normalized, so
  the message reaches the lowercase agent id instead of waiting in the mailbox forever.
- A stale file summary said "since then 2 of 1 symbols changed" when a symbol moved and one was added; it
  now says "N symbol(s) differ: …", which cannot contradict itself.
- `memdex bench` no longer prints "(-83% smaller)" — for a single file whose outline is larger than the
  file, or for the whole-repo total — such rows now read "(no saving)" / "(no saving overall)".
- A registry with only hand-shared skills re-walked the whole home directory on every brief: `last_scan`
  ignored them, so the rate limit never engaged. A `skills_meta` row now records the last walk, and the
  brief passes the session's skill home through to the refresh.

## [0.3.6] - 2026-11-01

Everything in `docs/LIMITS.md` landed in this release: every documented limit now has a mechanism behind it
instead of a caveat. Schema v7 (notes gain `uid`/`updated_at`, claims gain `token`/`sha`) — an older memdex
refuses a newer database, so upgrade every agent sharing `~/.memdex`.

### Changed
- The brief's middle spends its character budget by exact 0/1 knapsack over line priority instead of
  dropping the weakest line until the text fits: one long low-priority line no longer costs several short
  important ones. Decisions, todos and pitfalls are chosen with maximal marginal relevance over their file
  refs (Carbonell & Goldstein 1998), so three notes about one file stop taking three lines.

### Added
- **Storage strength** (Bjork & Bjork 1992) alongside ACT-R's retrieval strength: the cold tier now asks
  whether a note was ever well learned, not only whether it was needed lately, so a decision that many
  sessions used and this month's did not stays in the brief. Confirms count double — they mean the code
  moved and the note still held.
- **Retrieval-induced forgetting** (Anderson, Bjork & Bjork 1994) as interference: when a note is
  superseded, surviving notes that share its files rank slightly lower (up to 0.3, proportional to the
  overlap). Lower, never hidden.
- `memdex export --sync DIR` and `memdex merge DIR`: notes and summaries across machines, with no server
  and no network code. Schema v7 gives every note a stable `uid` and an `updated_at`; export writes one
  JSON file per note and per summary, and merge applies them last-writer-wins on the whole note, reporting
  the writes it discarded. Summaries are content-addressed by sha, so they merge for free. **Claims and
  messages are never exported** — a lease that outlives its machine is a deadlock generator, and a message
  is addressed to a session that does not exist on the other side.
- `memdex digest` (and `--json`): the consolidation queue. Notes about the same file, or near-duplicates of
  each other, are clustered and handed to the agent with the `supersedes=` move spelled out. memdex cannot
  write the digest — deciding what is still true means reading the code — so it does the bookkeeping and
  says so in the output.
- Bring-your-own vectors: `remember(embedding=[…])` stores a vector the agent computed, and
  `search(embedding=[…])` searches with one. Exact cosine over the existing `vectors` table, so semantic
  search works without Ollama, without a library, and without memdex ever calling a model itself.
- `memdex skills verify` (and `--json`): re-hashes every registered SKILL.md and reports the ones that
  changed or disappeared since the scan, then reads each one for the handful of patterns that turn advice
  into a way to lose a machine (`rm -rf`, `curl … | sh`, `sudo`, secrets, `base64 -d`, force-push, hook
  skipping) with the line number. A changed or missing file exits 1; hazards are reported, not judged, and
  nothing is ever rewritten.
- `memdex import [file…]`: reads `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` (or the files you name) and
  stores each heading section as a note, with refs to the files the section mentions. The import is
  idempotent (a section already in the store is skipped) and gives a new checkout a non-empty memory on
  its first session instead of a cold start. With plain notes in the store, the brief now says
  `memory: N note(s) → search before exploring` — previously only decisions, todos and pitfalls were
  announced, so an imported memory was invisible.
- The brief's decisions, todos and pitfalls get one hop of spreading activation (Collins & Loftus 1975):
  a note whose files are the ones that just changed, or that this agent has claimed, moves ahead of an
  equally accessible note about a file nobody is looking at.
- `search` runs a second, half-weight query built from the terms its first results repeat (RM3-lite,
  Lavrenko & Croft 2001): the words the top hits are about, minus the ones that appear all over the index.
  A note that never says "wal" but is about the same deadlock now turns up when you search for it.
- Claims are leases with a **fencing token** (schema v6): every grant gets the next number for the project
  and the sha of the file it was taken against. When a file moves under your own exclusive lock, the
  `pre-edit` hook now refuses the edit instead of letting a stale read overwrite the change —
  `memdex claim <path> --exclusive --refresh` re-stamps the lock after you re-read it. Advisory claims
  still only warn, and `MEMDEX_ALLOW_LOCK=1` still bypasses everything.
- `docs/LIMITS.md`: for each documented limit, what removing it would take and which published technique
  is the cheapest honest answer — range-based set reconciliation for sync, RM3 for paraphrase, MMR and a
  knapsack for the brief budget, fencing tokens for claims, TUF-style content hashes for skills. Verdicts
  are `now` / `next` / `later` / `no`, so the next release can start from the list.
- `memdex doctor --serve-check`: the same MCP handshake as `memdex connect --serve-check`, but as a
  diagnosis of this install — the server is started the way an agent would start it and must answer
  `initialize` and `tools/list`. A broken launch is a ✗ line (exit code 1), not a warning, because that
  install cannot work for any agent.

## [0.3.5] - 2026-11-01

### Added
- `memdex connect` now also configures Zed (`context_servers.memdex`) and Cline (`mcpServers.memdex`,
  under VS Code's per-extension storage, or `~/.cline` when its CLI is installed). Zed's settings file
  allows comments and a rewrite would drop them, so a settings file that is not strict JSON is left alone
  and the block to paste is printed instead of being replaced.
- `memdex connect --serve-check`: after writing, start the server from the entry that was just written and
  speak MCP to it — `initialize`, then `tools/list` — reporting `memdex answered: N tools, protocol …`. A
  wrong path, a missing interpreter or a database the server cannot open now fails at setup, not in the
  agent. A server that exits is reported immediately (with its exit code) instead of waiting out the
  timeout.

## [0.3.4] - 2026-11-01

### Changed
- Packaging follows PEP 639: `license = "MIT"` and `license-files`, so the wheel carries
  `License-Expression: MIT` and `License-File: LICENSE` instead of the `license = { text = "MIT" }` table
  that setuptools 77 deprecates. The `License ::` classifier goes with it, and `setuptools>=77` is now the
  build requirement. Nothing about the installed package changes.

## [0.3.3] - 2026-11-01

### Added
- `memdex connect`: writes the MCP server entry into the agents already installed on this machine —
  Claude Desktop, Cursor, VS Code (GitHub Copilot), Windsurf and Gemini CLI (`mcpServers.memdex`),
  VS Code (`servers.memdex` with `type: stdio`) and Codex CLI (`[mcp_servers.memdex]`). It merges that one
  key and leaves the rest of the file alone, saves the old file as `.bak` before writing, never creates a
  config folder for a client that is not installed, and refuses to rewrite a file it cannot parse.
  `--list` reports what is installed and configured without writing, `--dry-run` previews the writes,
  `--remove` undoes them, `--client` narrows it to one client. `memdex config` still prints the snippets
  for pasting by hand.
- `memdex connect --project`: the configs that belong in the checkout — `.vscode/mcp.json` and `.mcp.json` —
  written with a bare `memdex` command so the file can be committed and shared, and the `.vscode` folder
  created on purpose (a user-scope client that is not installed is still skipped).
- `memdex connect` runs `claude mcp add` itself when the Claude Code CLI is on PATH (`--no-claude` opts out,
  `--client claude-code` runs only that step). In project scope the CLI owns `.mcp.json` and memdex writes
  only the VS Code file, so no file has two writers; a failing CLI is reported with the exact command to run
  by hand instead of being swallowed.
- `memdex doctor` gained a `clients` line: which installed clients already carry the entry, and which need
  `memdex connect`.

### Changed
- The index behind `project_brief` stops after `INDEX_BUDGET` (2,000) files that need reading and says how
  many are left; the next brief continues where it stopped. On a fresh 20,000-file checkout the first brief
  now answers in about a second instead of blocking one agent-facing call for nearly two minutes until it
  times out. `memdex index` is still uncapped, and `MEMDEX_INDEX_BUDGET=0` removes the cap for the brief too.

## [0.3.2] - 2026-11-01

### Changed
- PyPI metadata carries Repository, Source and Changelog links; the READMEs show the Glama MCP score badge.
- Launch drafts under `docs/launch/` updated to 0.3.1 (edit-time locks, skill registry, schema v5).

## [0.3.1] - 2026-11-01

### Added
- `memdex hook pre-edit`: a Claude Code `PreToolUse` hook. It reads the tool call from stdin, resolves
  `tool_input.file_path` against the checkout, and **exits 2** (blocked, with stderr shown to the model) when
  another agent holds an exclusive claim on that file — a lock enforced at edit time, not at commit time.
  Advisory claims only print a note; edits outside the checkout are ignored; `MEMDEX_ALLOW_LOCK=1` bypasses.
- `memdex hook install --claude [--project]`: merges that hook into `~/.claude/settings.json` (or
  `./.claude/settings.json`), keeps existing hooks, backs the file up as `.bak`, and leaves unparseable files
  alone. `memdex config` prints the same snippet.
- Schema v5: `notes.last_used_by`, `notes.confirm_count`, `summaries.last_used_by`, so `memdex stats` can say
  **notes read by another agent** and **re-confirmed after a change** exactly instead of approximating with
  `use_count`. Usage is attributed to the session's agent (explicit argument, `Store.default_agent`, then
  `MEMDEX_AGENT`).

### Changed
- `memdex stats` counts a read by another agent only when the reader is not the author, and counts confirms
  separately (a confirm means the code moved under the note and the note still held).

## [0.3.0] - 2026-11-01

Built by Claude Code and DeepSeek on one checkout, coordinated through memdex itself. Schema v4
(databases from 0.1.0 onward are upgraded in place). The research behind the ranking changes is in
[docs/RESEARCH.md](docs/RESEARCH.md).

### Added
- **Locks.** `claim(exclusive=true)` is refused while another agent holds any overlapping claim and refuses
  theirs in turn; plain claims are refused only by an exclusive one; `force=true` records anyway and says so.
  A refused claim writes nothing. `"."` claims the whole project. `file_summary` and `read_snippet` carry
  "claimed by <agent> (…)" for other agents' claims, and the brief lists files changed since the last
  refresh with their claimants — so a session learns about the other session before it edits.
- **Skill registry.** `skills(action=find|show|scan|share)`: scans every harness's `SKILL.md` files on the
  machine (`~/.agents/skills`, `~/.claude`, `~/.codex`, `~/.dsh`, project `.claude/skills`, …), ranks by
  IDF-weighted whole-word matches, merges duplicate installs by content hash, and prints a skill so an agent
  that lacks it can follow it anyway.
- **Memory that ranks, forgets and heals.** Reading a note in full, confirming it, or being served a fresh
  summary counts as a use; ACT-R base-level activation (uses × recency + kind prior + importance) orders the
  brief and re-ranks search's note hits; notes below the cold line leave the brief but stay searchable and are
  never deleted. `remember(importance=1-5, pin=true)`, `note(id, "pin"|"unpin")`: pinned notes head every
  brief. `note(id, "confirm")` re-stamps a note's hashes to the current code and clears its ⚠.
- **Per-symbol staleness.** Every outline symbol's span is hashed; a summary carries the symbol map it was
  written against, so an edited file reports "2 of 31 symbols changed: Store.index, Store.search; likely still
  valid for the rest". Note refs may be `path#Symbol`, flagged only when that symbol's span moves.
- **Brief layout for how models read.** Blockers, pinned notes and the handoff's *Next* at the head; pitfalls
  (fresh refs first) and a one-line "do first" at the tail; context in the middle, which is what gets trimmed
  when the budget is exceeded. A handoff without a next step is answered with the open todos.
- **Who knows what.** `experts: src/ → claude (31)` in the brief, and "codex wrote 14 summaries under src/ —
  ask them" when `file_summary` has nothing for a file.
- **Near-duplicate hint** on `remember` (64-bit SimHash over word 1–3-gram shingles, `memdex/simhash.py`) and
  **key expansion** for search (`Store.index` → "store index", cited paths and symbols in a note's key,
  `memdex/keys.py`).
- `memdex claim --exclusive` makes a claim a lock (refused while another agent overlaps, expires like every
  claim) and `--force` records it anyway with a note. `memdex claim` now exits 1 when refused, so scripts can
  react.
- `memdex hook install` writes a git `pre-commit` hook that refuses a commit staging a file another agent has
  locked; advisory claims only print a note. `MEMDEX_ALLOW_LOCK=1` skips the check.
- `memdex skills [list|find|show|scan|share]`: the skill registry from the CLI, so a shell-only agent can find
  the skills this machine's agents share and follow one.
- `memdex note <id> --confirm|--pin|--unpin`: reconsolidate a note against the current code, or pin the few
  facts every session must see. `memdex remember --importance 1-5 --pin` sets ranking weight at write time.
- `memdex stats` now reports the usage signal: summaries served fresh, notes read by a later session, pinned and
  cold tiers, and the skill registry size. A note below the cold line is still stored and searchable — it just
  stops spending brief budget.
- README/README.ko: "How memdex ranks and forgets" (activation, pinned core, cold tier, reconsolidation,
  per-symbol staleness, SimHash near-duplicate hints).

### Changed
- `project_brief` is laid out head/middle/tail as above; `search` output marks pinned notes and ends stale
  hits with the confirm-or-supersede hint.
- `memdex note <missing id>` exits 1 instead of 0, so scripts can react.

### Fixed
- Cold indexing was O(n^2): the FTS delete filtered on UNINDEXED columns, a full table scan per file.
  `fts_map` (schema v3) maps `(src, ref, scope)` to the FTS row id, so the delete hits one row.
  Measured on a 5,000-file synthetic tree: **96.8 s → 2.5 s** (39x); on a real 1,382-file Godot repo:
  **62.0 s → 1.5 s** (41x). Peak RSS stayed at ~30 MB; warm re-index is unchanged (0.3 s).
- `index()` commits every 50 files instead of holding the single writer slot for the whole scan, so another
  agent's write does not fail with `database is locked` while a large tree is being indexed.
- `memdex serve` exits when the client closes its end instead of lingering with the database open.

## [0.2.5] - 2026-11-01

### Fixed
- The README says every tool has a CLI command; `where_is` and `note` were the two that did not. Added
  `memdex where_is <symbol>` and `memdex note <id> [--archive]`, plus a test that keeps the list honest.
- `--db` now works after the subcommand as well as before it (`memdex doctor --db X`), which is where most
  people type it.

## [0.2.4] - 2026-11-01

### Added
- README demo GIF: two agents (Claude + Codex) on one checkout going claim → inbox → handoff without
  touching the same file. Recorded from real CLI output; the generator is in
  [`docs/brand/demo/`](docs/brand/demo/).

## [0.2.3] - 2026-11-01

### Added
- Brand identity **Ledger Ribbon**: logo (SVG + PNG, light and dark grounds), design tokens and a 13-page
  guidelines PDF under [`docs/brand/`](docs/brand/). Both READMEs now carry the logo and a colour/type summary.

## [0.2.2] - 2026-11-01

### Fixed
- Tool inputs are validated instead of stored blindly: `remember` needs a title, `post` needs a message,
  `handoff` needs at least one non-empty field, and `claim` accepts only paths inside the project
  (Windows separators and `./` are normalised; outside paths are reported and dropped).
- A saved file summary longer than 4,000 chars is trimmed, and the reply says so — a summary that long
  defeats the token saving it exists for.
- `memdex bench` prints small files in bytes (`779 B`) instead of a misleading `0.0 KB`.

## [0.2.1] - 2026-11-01

### Changed
- README carries the `mcp-name: io.github.ChoYugyeong/memdex` marker, which the MCP Registry checks
  against the PyPI package to verify ownership.
- `server.json` validates against the current `2025-12-11` registry schema (no field changes needed).

## [0.2.0] - 2026-11-01

### Added
- `memdex bench`: measures how many tokens the outline map saves on a real checkout (`--top`, `--json`).
  Answers "how much does this actually save?" with the user's own repository.
- `memdex prune`: drops index and search rows for project directories that no longer exist, then VACUUMs
  (`--dry-run`, `--all` to also remove notes/messages/claims of missing projects, `--no-vacuum`).
- `memdex doctor`: database path and size, schema version, writability, whether the current project is
  indexed, and which optional integrations (Kensaku, Ollama) are on. Exit code 1 when something must be fixed.
- Database schema versioning through `PRAGMA user_version`. Databases from before versioning are upgraded in
  place; a database written by a newer memdex is refused with an explanation instead of being corrupted.
- `project_brief` and `memdex brief` are capped at 4,000 chars (`--max-chars 0` lifts the cap); the
  `session-start` hook uses the same budget.
- Outlines for 10 more languages: Elixir, Clojure, Zig, Julia, R, Haskell, Groovy, Protobuf, GraphQL and SQL.
- Unicode BOMs are stripped when a file is read, so a BOM-prefixed first line still yields a symbol.
- README "Limits", "Measure it on your repo" and "Maintenance" sections; PyPI releases through Trusted
  Publishing; CHANGELOG and CONTRIBUTING.

### Changed
- Per-language regexes are compiled once per process instead of once per file.

## [0.1.0] - 2026-11-01

First public release.

### Added
- MCP server over stdio with no dependencies (`initialize`, `ping`, `tools/list`, `tools/call`).
- Content-hash file summary cache: summaries are keyed to the file's sha256 and shared across worktrees.
- Incremental index that re-reads only files whose size or mtime moved.
- Outlines and a symbol map for 30+ languages (Python via `ast`, others via per-language regexes).
- `read_snippet`: bounded reads around a query or a line.
- Notes (`note`, `decision`, `pitfall`, `todo`, `handoff`) linked to files and flagged when those files change.
- Agent coordination: `post`/`inbox` messages, expiring `claim`s with overlap warnings, `handoff`.
- Hybrid search: FTS5 trigram plus optional Ollama embeddings, merged with reciprocal rank fusion.
- Optional bridge to [Kensaku](https://github.com/yjid/kensaku) for whole-machine content search.
- CLI covering every tool, plus `index`, `export`, `config`, `stats` and a `hook session-start` entry point.
- Tests on Linux, Windows and macOS with Python 3.9 and 3.13, including a real multi-process concurrency suite.

[Unreleased]: https://github.com/ChoYugyeong/memdex/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ChoYugyeong/memdex/releases/tag/v0.1.0
