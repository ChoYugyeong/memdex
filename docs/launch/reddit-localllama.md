# r/LocalLLaMA

**Flair:** Resources / Showcase

**Title:** memdex: local, zero-dependency memory for coding agents over MCP (one SQLite file)

---

Author here (solo project, MIT). Local-first take on the "agents forget everything" problem. No embedding API, no vector DB, no daemon:

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
- **Embeddings are optional and never required**: `MEMDEX_EMBED=ollama:bge-m3` adds a vector tier fused with FTS5 by
  RRF, or you can hand it vectors yourself — `remember(embedding=[…])` / `search(embedding=[…])` — if your agent
  already calls a model. With neither, it is keyword + symbols and nothing leaves the machine. Kensaku (if
  installed) adds PDF/OCR/screenshot hits.
- **One-command setup**: `memdex connect` writes the MCP entry into whichever client you already have installed
  (Claude Desktop, Cursor, VS Code, Windsurf, Zed, Cline, Gemini CLI, Codex) and `--serve-check` starts the server
  afterwards and speaks MCP to it, so a broken setup fails there instead of in your agent.
- **Between your own machines, without a service**: `memdex export --sync DIR` / `memdex merge DIR` move notes and
  summaries through a folder you already sync (git, Syncthing, USB). Last-writer-wins, and summaries merge for free
  because they are keyed by sha. Claims never travel.
- **Cold start**: `memdex import` turns the `AGENTS.md` a repo already has into notes with refs.

Numbers from `memdex bench` on a real 66-file repo: 154,105 tokens to read everything versus 3,471 tokens of outlines
before any summary is saved. The baseline is generous (real agents read slices), so treat it as an upper bound.

Limits, said plainly: regex outlines miss exotic syntax; a plain `claim` warns rather than locks (an exclusive one
carries a fencing token and the file's sha, and the hook refuses a stale edit); there is no server, so two machines
reconcile when they meet rather than live; one maintainer, 347 tests, schema v7.

`uvx memdex-mcp` or `pipx install memdex-mcp`. MIT. Works with any MCP client.

https://github.com/ChoYugyeong/memdex

---

## Notes

- This sub cares about "does it need an API / does data leave the machine" — answer that in the first two lines.
- Mention the Ollama path because it is optional; do not oversell it as a vector search product.
- Expect "why not <X>" comments; answer with the comparison table in the README rather than arguing.
