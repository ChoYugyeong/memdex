# r/ClaudeAI

**Flair:** I made this (or Showcase, whichever the sub uses)

**Title:** My Claude Code sessions kept ending mid-task with no handoff, so I made the hook write one (open source, local)

---

I built this, it's free and MIT. Claude Code is my main agent, and I also run Codex, the ChatGPT desktop app and
DeepSeek on the same repos. The thing that kept costing me wasn't memory in the "remember my preferences" sense.
It was this:

- a session runs out of context or I close the window, and nobody wrote down that task 7 was half done
- the next session (or a different agent) redoes it, or worse, finishes it differently
- two agents edit the same file in the same hour and I find out at `git diff`

**memdex** is a small MCP server for that. One SQLite file in `~/.memdex`, zero dependencies, nothing leaves the
machine, nothing is written into the repo.

The part this sub might like: `memdex hook install --claude` adds a `SessionEnd` and `PreCompact` hook. If a
Claude Code session ends or compacts without a handoff, the hook drafts one from evidence, without calling a
model:

```text
last handoff #41 (claude@shop, 2h ago):
Next: Open tasks: #7 [in_progress] Move checkout to the new tax API. Uncommitted: src/tax.py, tests/test_tax.py.
Done: (auto) session ended without a handoff; drafted from evidence, not vouched for by the agent.
Asked: "Move checkout to the new tax API" Edited: src/tax.py, tests/test_tax.py. Last message: "Two tests
still fail on rounding; see test_tax.py::test_half_cent."
```

The next session, whichever agent it is, sees that first in `project_brief`. If Claude wrote a real handoff, no
draft is made. Secret-looking paths (`.env`, keys) are dropped and token-shaped strings are redacted.

The rest:

- **task ledger**: a tree with owners, blockers and evidence. A report stays `reported` until it's checked, and
  it records whether the same agent or a different one checked it. Parents wait for their children.
- **claim / post / inbox**: announce what you're editing, message another agent, reply to a message.
  `claim --exclusive` plus the `PreToolUse` hook refuses the edit before it happens if another agent holds the file.
- **cheap**: 10 tools listed by default, about 1,300 tokens per request for instructions and schemas.

Why I changed direction: memdex started as a "token-saving memory" with cached file summaries. After 19 days of
my own agents using it: 493 handoffs across 40 projects, 242 messages between agents, 241 tasks, and the 45 file
summaries were never once served to save a read. So 0.5.0 leads with handoffs and the ledger, and the summary
tools are opt-in.

Setup for Claude Code:

```bash
pipx install memdex-mcp
memdex connect --agent claude      # writes the MCP entry (claude mcp add …) and the other clients you have
memdex hook install --claude       # optional: lock-aware edits + the automatic handoff
```

Honest limits: only Claude Code has the session-end hook today (other agents run `memdex handoff --auto`); it's
one machine, no server; agent names are self-declared, so "checked by another agent" is a cooperative signal,
not proof. If you run a single agent, claude-mem may suit you better: it captures everything automatically.
memdex is for when more than one agent touches the same repo.

Does anyone else run two or more agents on one checkout? I'm curious what breaks for you.

GitHub: https://github.com/ChoYugyeong/memdex · PyPI: `memdex-mcp` · MIT

---

## Notes

- Disclose authorship in the first line or flair; r/ClaudeAI removes drive-by links.
- The hook output block is the hook of the post. Keep it.
- Recommending claude-mem for the single-agent case is deliberate: it is true, and it answers the first
  "how is this different from claude-mem" comment before it is written.
- If someone asks "does it work with Claude Desktop / Cursor?": yes, any MCP stdio client; `memdex connect`
  writes it, `memdex config --agent <name>` prints the snippet.
