# r/LocalLLaMA

**Flair:** Resources / Showcase

**Title:** memdex: local, zero-dependency memory for coding agents over MCP (one SQLite file)

---

Local-first take on the "agents forget everything" problem. No embedding API, no vector DB, no daemon:

- **One SQLite file** at `~/.memdex/memdex.db`; `MEMDEX_HOME` moves it. Nothing is written into your repos, so the
  memory never ends up in a commit.
- **Zero runtime dependencies** — the MCP stdio protocol is implemented directly against the standard library, so it
  does not break when an SDK changes.
- **Content-hash summaries**: an agent saves 2–6 lines per file; the summary is keyed to the file's sha256 and goes
  stale exactly when the bytes change. Identical content in another worktree shares the same summary.
- **On-machine outlines**: classes/functions with line numbers for 30+ languages, Python via `ast`, the rest via
  per-language regexes (no tree-sitter, no model call). `where_is` + `read_snippet` replace re-reading whole files.
- **Agent-to-agent coordination**: expiring `claim`s with overlap warnings, `post`/`inbox`, `handoff`. Useful when
  Claude Code and a local model work on the same checkout.
- **Optional, local**: `MEMDEX_EMBED=ollama:bge-m3` adds a vector tier and fuses it with FTS5 by RRF; unreachable
  Ollama silently falls back to keyword search. Kensaku (if installed) adds PDF/OCR/screenshot hits.

Numbers from `memdex bench` on a real 66-file repo: 154,105 tokens to read everything versus 3,471 tokens of outlines
before any summary is saved. The baseline is generous (real agents read slices), so treat it as an upper bound.

Limits, said plainly: regex outlines miss exotic syntax; local-only, no team sync; `claim` warns, git is still the
source of truth; one maintainer.

`uvx memdex-mcp` or `pipx install memdex-mcp`. MIT. Works with any MCP client.

https://github.com/ChoYugyeong/memdex

---

## Notes

- This sub cares about "does it need an API / does data leave the machine" — answer that in the first two lines.
- Mention the Ollama path because it is optional; do not oversell it as a vector search product.
- Expect "why not <X>" comments; answer with the comparison table in the README rather than arguing.
