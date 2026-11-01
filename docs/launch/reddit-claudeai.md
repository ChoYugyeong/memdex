# r/ClaudeAI

**Flair:** I made this (or Showcase, whichever the sub uses)

**Title:** I got Claude Code and Codex to stop stepping on the same files (open source, local)

---

I use Claude Code as my main agent and Codex for a second pair of hands. The problem is they don't know each other
exists:

- both start from zero every session, re-reading the same 3k-token files
- they edit the same file in the same hour, and I only notice at `git diff`
- everything Claude learns about a tricky module is invisible to the next session

I built **memdex** to fix that. It's an MCP server — one SQLite file in `~/.memdex`, zero dependencies, nothing leaves
the machine, nothing is written into the repo.

What it actually changes in a session:

- `project_brief` at the start: last handoff, pitfalls, decisions, unread messages, who is editing what
- `file_summary` before reading a file: a cached 2–6 line summary that is keyed to the file's sha256, so it stays
  valid until the content changes and is marked stale the moment it does
- `where_is` / `read_snippet`: symbol lookup, then ~256 tokens around it instead of the whole file
- `claim` / `post` / `inbox` / `handoff`: the coordination part — an agent announces what it is editing, the other
  one sees "overlaps with claude: src/store.py — add retry (119m left)" and picks another file
- `claim --exclusive` is a real lock, and `memdex hook install --claude` turns it into a Claude Code `PreToolUse`
  hook: when another agent holds the file, the edit is refused **before it happens** (exit 2, reason shown to the
  model), not at commit time. Each lock carries a fencing token and the file's sha, so an edit computed from a stale
  read is refused too, and `claim --refresh` is how you prove you re-read it
- `memdex import` reads the `AGENTS.md` this repo already has into notes, so a fresh checkout starts with a memory
  instead of an empty one
- `memdex export --sync DIR` / `memdex merge DIR` move notes and summaries between machines through a directory you
  already sync (git, Syncthing, a shared folder) — last writer wins, and claims never travel
- `memdex digest` clusters the notes that say the same thing, for whoever consolidates them next

The demo is two agents on one checkout (claim → inbox → handoff) in ~15 seconds:
https://github.com/ChoYugyeong/memdex#readme

Setup for Claude Code is one line:

```bash
claude mcp add memdex --scope user -e MEMDEX_AGENT=claude -- memdex serve
```

and for Codex/Cursor/Windsurf/Claude Desktop, `memdex connect --agent codex` writes that entry into whichever of
them you already have installed (merges one key, keeps the rest of the file, saves a `.bak` first). By hand:

```json
{ "mcpServers": { "memdex": { "command": "memdex", "args": ["serve"], "env": { "MEMDEX_AGENT": "codex" } } } }
```

(`pipx install memdex-mcp`, or `uvx memdex-mcp`.)

Honest limits: outlines are regex-level (Python uses `ast`), there is no server — sync is a directory you choose,
so two machines reconcile when they meet rather than in real time — and a plain `claim` warns instead of locking. If
you run a single agent on one small repo, a hand-written `AGENTS.md` is probably enough (although `memdex import`
will at least turn that file into something searchable).

Curious whether the coordination part (claims/inbox) is useful to anyone else, or if I'm the only one running two
agents on one checkout. Feedback very welcome.

GitHub: https://github.com/ChoYugyeong/memdex · PyPI: `memdex-mcp` · MIT

---

## Notes

- Disclose authorship in the first line or flair; r/ClaudeAI removes drive-by links.
- The comment question at the end is deliberate: it invites "me too" replies, which is what makes a thread visible.
- If someone asks "does it work with Claude Desktop / Cursor?" — yes, any MCP stdio client; `memdex config --agent
  <name>` prints the snippet.
