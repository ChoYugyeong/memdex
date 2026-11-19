# r/LocalLLaMA

**Flair:** Resources / Showcase

**Title:** memdex: local handoffs, task ledger and messages so a local model can work alongside Claude Code and Codex (one SQLite file, zero deps)

---

Author here (solo project, MIT). No API key, no service, no telemetry: one SQLite file at `~/.memdex/memdex.db`,
Python standard library only, nothing leaves the machine.

The problem it solves is coordination between coding agents on one repo, not chat memory. I run Claude Code,
Codex, DeepSeek Harness and a local Qwen (through Ollama) on the same checkouts, and what goes wrong is: a
session stops halfway and nobody knows where, two agents edit the same file, or something gets marked done
that nobody checked.

What a local model gets out of it:

- **It joins without an MCP client.** Any model that can run shell commands is a full participant:

  ```bash
  MEMDEX_AGENT=qwen memdex brief                      # last handoff, open tasks, unread messages
  MEMDEX_AGENT=qwen memdex claim src/store.py --intent "add retry"
  MEMDEX_AGENT=qwen memdex post "retry done, please check" --group --to claude --task 7
  MEMDEX_AGENT=qwen memdex handoff --auto             # drafts a handoff from git + its open tasks
  ```

- **Task ledger with honest states.** A report is `reported` until checked; the same agent checking it is
  `self_verified`, a different agent is `verified`. A cheap local model is a good second checker.
- **Reviews by a local model.** `memdex review --provider qwen --task 7 --files brief.md src/x.py` sends those
  files to a local Ollama model (no tool loop, nothing pulled automatically) and puts the answer in the asking
  chat's inbox. Claude and DeepSeek can be reviewers too, through their own logins.
- **Optional local embeddings.** `MEMDEX_EMBED=ollama:bge-m3` adds a vector tier fused with keyword search. Off
  by default, nothing breaks without it.
- **Small for MCP clients too.** The server lists 10 tools by default, about 1,300 tokens per request. That
  matters more for a local model with a small context than for Claude.

Real use, my machine, 19 days: 493 handoffs across 40 projects, 242 messages between agents, 241 ledger tasks.
Also real: of those tasks, only 6 were checked by a different agent, and the cached file summaries I built first
were never used, which is why 0.5.0 drops them from the default tool list.

Limits, said plainly: one machine (two machines reconcile through a folder you sync, last writer wins); agent
names are self-declared, not authenticated; a plain `claim` warns rather than locks; one maintainer. 793 tests,
CI on Linux/macOS/Windows.

`pipx install memdex-mcp` or `uvx memdex-mcp`. https://github.com/ChoYugyeong/memdex

---

## Notes

- This sub cares about "does it need an API / does data leave the machine": the first two lines answer it.
- Lead with "a local model joins through the CLI"; it is the part no other memory tool in the thread will say.
- Do not oversell the Ollama review or embeddings. Both are optional; the review has a 12,000-byte input limit.
- Expect "why not <X>": point at the README comparison table rather than arguing.
