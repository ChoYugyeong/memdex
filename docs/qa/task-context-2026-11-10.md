# Task-aware brief — evidence (2026-11-10)

## Source and scope

- [TiagerBao's post](https://x.com/TiagerBao/status/2120041783568875653).
- [Jev source notes reproduced by yibie](https://github.com/yibie/jev-engineering-zh/blob/main/source-notes/original.txt),
  particularly “Meta-attention”, “First-principled version of Skills/MCP/Tool calling”, and Appendix 2.
- [Third-party compilation/translation](https://github.com/yibie/jev-engineering-zh).

The applicable proposal: select context after knowing the task, and reuse shared retrieval work.
These notes propose an architecture; the compilation's illustrative token percentages do not establish
savings for memdex. This change uses no Jev service and adds no model dependency.

## Behavior

```text
memdex brief --task "skills security scan"
project_brief(task="skills security scan")
```

The same existing tool retrieves from the project's shared notes/file index. At most four note excerpts
and three files compete for the existing 4,000-character budget (model-tier scaling still applies).
Files use a summary saved by any agent when its content sha still matches; otherwise they use an outline.
Each candidate file is refreshed before its summary is read, including when the brief's capped index pass
has not reached that file. The response retains note IDs, paths, summary author/hash and stale-ref warnings.
Excerpts do not count as full note/summary reads and internal retrieval does not inflate tool-search stats.

Pinned rules, unread-message count, claims and the open-work ledger retain their ordinary global slots.
Without `task`, general briefing is unchanged. With `task`, recent handoffs, decisions and pitfalls are
retrieved rather than universally injected; another task's handoff does not become a `do first` instruction.

Search intentionally prioritizes recall. For startup context, notes need direct textual evidence: at least
two distinct non-stopword task terms, or the one term of a single-term query, in title/body (structured
experience context/evidence is a fallback for empty bodies). References alone are not sufficient.
This also filters unrelated old handoffs that the OR fallback, feedback or note vectors retrieved. File
ranking still uses the existing keyword/vector RRF and shows `by meaning` for vector-only candidates.

No new MCP tool (still 15), SQLite migration (still v16), external package, permanent configuration,
AGENTS.md change, or tool-discovery change.

## Verification

`python -m unittest discover tests`: **623 tests passed**, including 18 new task-context regressions:

- A cold old decision is retrieved for its task instead of unrelated recent decisions.
- Matching handoffs can appear as excerpts; unrelated handoffs never set the next step.
- Shared summaries are reused; edited-file summaries are rejected, even before index-budget catch-up.
- Pins, locks, messages and open work survive filtering; shown pins are not duplicated.
- Stale notes keep warnings; archived/replaced versions stay out.
- Incidental-word, reference-only and unrelated vector-only note hits are filtered.
- Empty/no-match tasks, bounded task text, small output budgets and unchanged empty-task behavior.
- CLI parsing and an actual MCP `tools/call` use the same existing tool.
- Internal retrieval does not inflate search-use counters.

The pre-existing `test_connect.CliTests.test_the_subcommand_is_wired_up` previously failed on this PC
because it called the installed Claude CLI against the user's MCP settings. It now passes `--no-claude`,
keeping the test on its temporary client fixture.

Existing `memdex eval`: deterministic cases Recall@5 **100%**, MRR **1.000**; natural-language keyword-only
Recall@5 **64%**, MRR **0.568**, unchanged. Tests and deterministic evaluation need no Ollama.

## Actual memdex project, local bge-m3

Measured against a SQLite backup of the live shared store, using `C:\Dev\memdex` as the checkout. All
index refreshes and registry/heartbeat writes went to that copy; the measurement did not modify the live store.
Ordinary brief: **2,786 chars**, including the unconditional latest handoff.

| Task | Keyword chars | With bge-m3 chars | With bge-m3: useful retrieved sources |
|---|---:|---:|---|
| `metadata vector search` | 2,608 | 2,608 | `tests/test_metadata_vectors.py`, vector QA report, `tests/test_vectors.py`, matching handoff #214 (marked stale) |
| `skills security scan` | 2,094 | 2,149 | `tests/test_skills.py`, `memdex/skills.py`, cached summary of the security audit (author and current sha shown) |
| `JSON-RPC conversation routing` | 1,849 | 2,084 | `memdex/mcp.py`, `tests/test_chat_routes.py`, `tests/test_connect.py` |

All six responses fit the default budget. Measured wall time was **205–725 ms** for these runs; this is a
small machine-specific sample, not a latency guarantee. Character counts measure response size only,
not tokenizer output, avoided future calls, end-to-end task quality or billed-token savings.

## Limits

- The caller must supply `task`; session-start hooks without a task retain their general briefing.
- The conservative note filter can miss synonyms or cross-language notes with no literal overlap. Use
  `search` with cues for broader recall. Vector file retrieval still works across languages.
- Retrieval is heuristic: keyword-only examples still include weak file matches (`server.json` for a
  JSON-RPC query). It is a source map to inspect, not an automatically verified answer.
- Large files remain represented by one metadata vector; symbol-level cards are outside this change.
- Excerpts are bounded and may omit a later fact in a long note. `note`/`file_summary` retrieves more.
- No semantic answer cache or persistent per-task result cache: summaries remain content-addressed,
  note versions remain append-only, and every task brief is assembled against current shared state.
- Actual compaction, KV-cache policy, conditional system instructions and lazy tool-schema loading
  belong to the host/harness and are not controlled by this MCP server.
