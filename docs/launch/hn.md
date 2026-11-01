# Show HN: memdex – one memory shared by every coding agent on a machine

**Title (pick one, ≤ 80 chars):**

- `Show HN: memdex – one memory shared by every coding agent on a machine` (70)
- `Show HN: Shared memory for AI coding agents (one SQLite file, zero deps)` (73)
- `Show HN: memdex – Claude Code and Codex can share one memory` (61)

**URL:** https://github.com/ChoYugyeong/memdex

---

## Author comment (post immediately, first comment)

I run more than one coding agent on the same checkout — Claude Code, Codex, sometimes Cursor or a local model.
Two things kept hurting:

1. Each session starts from zero and re-reads the same files, so I pay for the same context over and over.
2. The agents can't see each other. Two of them edit `src/store.py` in the same hour and I find out from `git diff`.

memdex is a small MCP server that fixes both, and it is deliberately boring:

- **One SQLite file** in `~/.memdex`, no daemon, no embedding API, no vector DB. Zero runtime dependencies; the MCP
  stdio protocol is implemented directly.
- **Summaries are content-addressed.** An agent reads a file once and saves 2–6 lines; the summary is keyed to the
  file's sha256, so it stays valid until the bytes change and is flagged the moment they do. Worktrees of one repo
  share the memory.
- **Free outlines.** Every indexed file gets a docstring/comment head plus classes and functions with line numbers
  (Python via `ast`, 30+ other languages via per-language regexes — no tree-sitter). `where_is` replaces repetitive
  grepping; `read_snippet` returns ~256 tokens around a match instead of the file.
- **Coordinated, not locked.** `claim` warns about overlapping edits with expiry, `post`/`inbox` carry messages,
  `handoff` is what the next session sees first. `claim --exclusive` is a real lock, and there are two hooks that
  enforce it: a git `pre-commit` hook refuses a commit staging a locked file, and a Claude Code `PreToolUse` hook
  refuses the **edit** itself (exit 2, with the reason printed for the model). It complements git; it does not try
  to replace it.
- **Memory that ranks.** Notes carry an ACT-R-style activation (uses decayed by time), pitfalls and decisions get a
  prior, a few can be pinned into every brief, and notes nobody uses go cold — still stored and searchable, out of
  the default context budget. Usage is attributed (`notes read by another agent`, `re-confirmed after a change`),
  and a near-duplicate is flagged at save time with a 64-bit SimHash instead of being silently duplicated.

Measured on a real 66-file repo, `memdex bench` reports **154,105 tokens** to read every file versus **3,471 tokens**
of outlines — 98% smaller before a single summary is saved. That baseline is generous (a real agent greps and reads
slices), so the honest claim is smaller; the part that holds is that nothing gets rediscovered twice.

What it is not: not a vector database (exact names beat embeddings for code), not a team knowledge base (local,
no sync), not a code navigator as precise as an LSP (outlines are regex-level).

185 tests, CI on Linux/macOS/Windows × Python 3.9/3.13. MIT. Happy to hear what breaks.

`uvx memdex-mcp` or `pipx install memdex-mcp`.

---

## Notes

- Keep the title boring. HN penalises hype; "one memory shared by every coding agent" is the factual claim.
- Do not paste the README. One short comment, then answer questions.
- Expect: "why not CLAUDE.md?" → answer with content-hash staleness + multi-agent coordination.
  "Why not Serena?" → different job; Serena edits, memdex remembers; `where_is` is a stand-in.
  "Why not a vector DB?" → exact symbol names; embeddings optional via Ollama.
