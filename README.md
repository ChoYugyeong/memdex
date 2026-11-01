# memdex

**Shared, token-saving memory for AI coding agents.** Zero dependencies, one SQLite file, any MCP client.

[한국어 README](README.ko.md)

Every new agent session starts from nothing: it re-reads the same files, re-runs the same searches
and re-discovers the same pitfalls. When several agents (Claude Code, Codex, Cursor, DeepSeek, …)
work on one machine, they also can't see what the others learned or are doing right now.

memdex fixes that with a small MCP server:

| Feature | What it does | Borrowed idea |
|---|---|---|
| **Content-hash summary cache** | An agent reads a 3,000-token file once and saves a 100-token summary. The summary is keyed to the file's sha256, so it stays valid until the content changes, then is flagged stale automatically. Identical content in other worktrees or branches shares it. | content-addressed storage |
| **Incremental index** | Only files whose size or mtime moved are re-read. Re-indexing an 800-file repo takes ~0.2 s. | [Everything](https://www.voidtools.com/)'s USN-journal approach |
| **Outline + symbol map** | Free outlines (docstring + classes/functions with line numbers) for Python, JS/TS, Vue/Svelte, Go (receiver methods as `Type.Method`), Rust, Java, Kotlin, Scala, C#, Swift, Dart, C/C++, Ruby, PHP, Lua, GDScript, shell, PowerShell, Markdown, Elixir, Clojure, Zig, Julia, R, Haskell, Groovy, Protobuf, GraphQL, SQL. `where_is` replaces repeated grepping. | Serena / repo maps, without tree-sitter |
| **Notes that know when they rot** | Decisions, pitfalls, todos and handoffs, linked to the files they're about. When those files change, the note is marked "⚠ changed since". Notes are append-only (`supersedes`), safe for concurrent agents. | Basic Memory, mem0 |
| **Agent coordination** | `post`/`inbox` messaging and `claim`s ("I'm editing src/store, 2h") with conflict warnings. All worktrees of a repo share one project memory. | — |
| **Hybrid search** | SQLite FTS5 trigram (works for Korean/Japanese/Chinese, with a fallback for 2-character terms) + optional local embeddings via Ollama, merged by reciprocal rank fusion. | claude-context |
| **Bounded reads** | `read_snippet` returns ~256 tokens around a match or a line instead of the whole file. Pair it with `where_is`. | [Kensaku](https://github.com/yjid/kensaku)'s `read_snippet` |
| **Whole-machine content search** | With Kensaku installed, `search` also returns full-text, OCR and semantic hits from PDFs, Office docs and screenshots. | Kensaku |

Measured on a real 800-file TypeScript repo: a 144 KB source file → 1.6 KB `file_summary` response (outline only,
before any agent summary was saved).

## Install

```bash
pipx install memdex-mcp        # or: pip install memdex-mcp
```

The PyPI name is `memdex-mcp` (plain `memdex` was taken); the command, package and MCP server are all `memdex`.
From source: `pipx install git+https://github.com/ChoYugyeong/memdex` or `pip install -e .` in a checkout.

Python 3.9+. No other dependencies — the MCP stdio protocol is implemented directly, so it won't break across MCP SDK versions.

## Connect your agents

`memdex config --agent <name>` prints these snippets.

**Claude Code**

```bash
claude mcp add memdex --scope user -e MEMDEX_AGENT=claude -- memdex serve
```

Optional: inject the brief at every session start (`~/.claude/settings.json`):

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "memdex hook session-start --agent claude" } ] } ] } }
```

**Any other MCP client** (Claude Desktop, Cursor, Codex, Windsurf, DeepSeek harness, …)

```json
{ "mcpServers": { "memdex": { "command": "memdex", "args": ["serve"], "env": { "MEMDEX_AGENT": "codex" } } } }
```

Give each agent its own `MEMDEX_AGENT` name. Its full id becomes `name@checkout`, e.g. `claude@myrepo-wt`.

## Tools

| Tool | Use |
|---|---|
| `project_brief` | Start of session: last handoff, pitfalls, decisions, todos, unread messages, other agents' claims. Refreshes the index. |
| `file_summary` | Before reading a file: cached summary (if the content is unchanged) + outline. |
| `file_summary_save` | After reading a file: store a 2–6 line summary for everyone. |
| `where_is` | Symbol definition lookup. |
| `read_snippet` | A bounded slice of a file around `query` or `line` (default ~256 tokens). |
| `search` | Notes, handoffs and file summaries; keyword + semantic. Plus Kensaku content hits when installed (`everywhere: true` for the whole machine). |
| `remember` / `note` | Save / read / archive notes (`note`, `decision`, `pitfall`, `todo`, `handoff`). |
| `handoff` | End of session: done / next / decisions / pitfalls. Releases your claims. |
| `post` / `inbox` | Messages between agents (broadcast or `to: "deepseek"`). |
| `claim` | Announce what you're editing; warns about overlaps. |

The server sends usage instructions in its MCP `initialize` response, so agents know the workflow without extra prompting.
The 12 tool schemas cost about 1,000 tokens per request.

## CLI

Everything the MCP tools do is also a command, for shell-capable agents and for humans:

```bash
memdex brief --agent codex              # start of session: handoff, notes, inbox, claims
memdex file src/store.py                # cached summary + outline
memdex file src/store.py --summary "…"  # save a summary
memdex snippet src/store.py --query "def index"
memdex search "locked"
memdex remember pitfall "WAL mode needed" --body "…" --files src/store.py
memdex claim src/store.py --intent "refactor"
memdex post "store.py is mine for an hour" --to claude
memdex inbox
memdex handoff "Done X" --next "Y" --pitfalls "Z"
memdex index · memdex export --out notes/ · memdex stats
```

## Where data lives

Everything is in `~/.memdex/memdex.db` (override with `MEMDEX_HOME`). Nothing is written into your
repositories, so memory never ends up in a commit. Files that look like secrets (`.env*`, `*.pem`, `*.key`,
`*secret*`, `*credential*`, …), binaries, lockfiles, files over 512 KB and anything git ignores are never read.

A project is identified by its git common directory, so all worktrees of a repository share notes,
messages and claims, while each worktree keeps its own file index.

## Kensaku integration (optional)

[Kensaku](https://github.com/yjid/kensaku) is a local, read-only desktop search engine that indexes
file names, the full text of PDF/DOCX/EPUB, OCR'd screenshots and a semantic tier. memdex covers a
project's source; Kensaku covers the rest of the machine. If `kensaku-cli` is on PATH (or
`MEMDEX_KENSAKU` points to it), `search` appends Kensaku's content hits, scoped to the project unless
`everywhere` is set. `MEMDEX_KENSAKU=off` disables it.

## Semantic search (optional)

```bash
ollama pull bge-m3
export MEMDEX_EMBED=ollama:bge-m3   # MEMDEX_OLLAMA_URL defaults to http://localhost:11434
```

Notes and summaries get embedded when saved; search merges keyword and vector rankings.
If Ollama is unreachable, memdex silently falls back to keyword search.

## Development

```bash
python -m unittest discover tests
```

## License

MIT
