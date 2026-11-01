# Changelog

All notable changes to memdex are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.7] - 2026-11-01

### Added
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
- `memdex note <missing id>` exits 1 instead of 0, so scripts can react.

## [0.2.6] - 2026-11-01

### Added
- `memdex claim --exclusive` makes a claim a lock (refused while another agent overlaps, expires like every
  claim) and `--force` records it anyway with a note. `memdex claim` now exits 1 when refused, so scripts can
  react.
- `memdex hook install` writes a git `pre-commit` hook that refuses a commit staging a file another agent has
  locked; advisory claims only print a note. `MEMDEX_ALLOW_LOCK=1` skips the check.

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
