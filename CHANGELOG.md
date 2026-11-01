# Changelog

All notable changes to memdex are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-01

### Added
- `memdex bench`: measures how many tokens the outline map saves on a real checkout (`--top`, `--json`).
  Answers "how much does this actually save?" with the user's own repository.
- `memdex prune`: drops index and search rows for project directories that no longer exist, then VACUUMs
  (`--dry-run`, `--all` to also remove notes/messages/claims of missing projects, `--no-vacuum`).
- Outlines for 10 more languages: Elixir, Clojure, Zig, Julia, R, Haskell, Groovy, Protobuf, GraphQL and SQL.
- Unicode BOMs are stripped when a file is read, so a BOM-prefixed first line still yields a symbol.
- README sections "Measure it on your repo" and "Maintenance"; PyPI releases through Trusted Publishing;
  CHANGELOG and CONTRIBUTING.

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
