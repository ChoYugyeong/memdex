# What memdex borrows from research, and what it deliberately does not

memdex is small, so every idea has to earn its place: it must be implementable with the Python
standard library and SQLite, need no model call inside memdex, and save more tokens than it costs.
This page records a survey (updated 2026-11-03) of four fields — learning science, memory neuroscience,
recent data structures, and 2023–2026 LLM-agent memory research — with the verdict for each idea.
The adopted ones are in the code; the file and function are named so the mapping is checkable.

## Adopted

### Task-aware context (2026-11-10)

The [post by TiagerBao](https://x.com/TiagerBao/status/2120041783568875653) points to a third-party
compilation of Jev design notes. The [upstream notes reproduced with the translation](https://github.com/yibie/jev-engineering-zh/blob/main/source-notes/original.txt)
propose query-aware context selection and sharing retrieval across agents. These are design proposals,
not a measured demonstration of token savings. The compilation's token-share table is illustrative;
memdex does not use its percentages as a benchmark.

`project_brief(task=…)` applies the part a memory MCP server can implement: retrieve from the shared
index for the current task, return small source-addressed note excerpts and hash-checked file summaries,
and omit general history from the task packet. The existing budget bounds the output. Pinned rules,
messages, claims and the work ledger stay global. Without a task, the existing brief is unchanged.

This reuses agents' saved summaries; it does not cache search answers, execute retrieved instructions,
control the caller's KV cache/compaction, load AGENTS.md conditionally or change MCP tool discovery.
Those latter proposals require host/harness support. See [verification and limits](qa/task-context-2026-11-10.md).

| Idea | Origin | What memdex does with it | Where |
|---|---|---|---|
| **Testing effect** — retrieving a memory strengthens it more than re-reading it | Roediger & Karpicke, *Psychological Science* 2006 ([summary](https://cft.vanderbilt.edu/?p=22452)) | Reading a note in full (`note`), confirming it, or being served a fresh summary counts as a *use*: `use_count`/`last_used_at` on notes and summaries. This is the raw signal everything below ranks on. | `store.touch_note`, `store.file_info` |
| **Base-level activation** — accessibility is a power-law sum over past uses | Anderson & Schooler 1991; ACT-R ([worked agent example](https://dev.to/futhgar/eviction-without-deletion-running-an-act-r-decay-policy-for-agent-memory-36hi)) | `activation(note) = ln(age_created^-0.5 + uses · age_last_use^-0.5) + kind prior + importance`. The brief's decisions/todos/pitfalls and the note part of `search` are ordered by it, so the six *most used* notes show, not the six newest. | `store.activation`, `store.list_notes`, `store.search` |
| **Eviction without deletion** — tier, don't forget | same | Notes below `COLD = −3` (≈ a plain note untouched for 13 months) leave the brief and get no ranking bonus, but stay stored and searchable. Nothing is deleted. | `store.COLD`, `store.list_notes` |
| **Forgetting curve / spaced repetition** | Ebbinghaus 1885; Settles & Meeder 2016 ([arXiv 2004.11327](https://arxiv.org/pdf/2004.11327)) | Treated as the motivation for the two rows above; ACT-R's power law replaces the exponential drop (MemoryBank's) and never discards. | — |
| **Reconsolidation** — a retrieved memory is open to update | Nader, Schafe & LeDoux 2000; Lee, Nader & Schiller 2017 ([PMC5605913](https://pmc.ncbi.nlm.nih.gov/articles/PMC5605913/)) | `note(id, "confirm")` re-stamps a note's file/symbol hashes to the current code and clears the ⚠; a stale note read through `note` or `search` ends with "still true? confirm · outdated? supersedes". The moment the agent is looking at both the note and the code is the cheapest moment to fix it. | `store.confirm_note`, `tools.note` |
| **Merkle-style leaves** — hash the parts, not only the whole | Merkle 1979; git tree objects ([explainer](https://joelgustafson.com/posts/2023-05-04/merklizing-the-key-value-store-for-fun-and-profit)) | Every outline symbol's span is hashed under the file's sha. A summary carries the symbol map it was written against, so an edited file reports "2 of 31 symbols changed: Store.index, Store.search; likely still valid for the other 29". Note refs may be `path#Symbol`, flagged only when *that* symbol moves. | `store.symbol_shas`, `store._symbol_diff`, `store._make_refs`, `store.stale_refs` |
| **Core vs archival memory** | MemGPT, Packer et al. 2023 ([arXiv 2310.08560](https://arxiv.org/abs/2310.08560v2)) | `remember(pin=true)` / `note(id, "pin")`: up to five pinned notes (~600 chars) sit at the head of every brief, immune to decay and trimming. Everything else competes on activation. | `tools.PINNED_CHARS`, `render_brief` |
| **Importance at write time** | Generative Agents, Park et al. 2023 ([arXiv 2304.03442](https://arxiv.org/pdf/2304.03442v2)) | A per-kind prior (pitfall > decision > todo) plus an optional `importance` 1–5 set by the agent who did the thinking. Fused into the ranking as a third list (RRF), not a weighted sum, so nothing needs normalising. | `store.KIND_PRIOR`, `store.search` |
| **Lost in the middle** | Liu et al., *TACL* 2024 ([arXiv 2307.03172](https://www.emergentmind.com/papers/2307.03172)) | The brief is laid out for a model's U-shaped attention: blockers, pinned facts and the handoff's *Next* at the head; pitfalls and a one-line "do first" at the tail; context in the middle. Over budget, the middle is trimmed first, lowest-priority line first; the ends are never cut. | `render_brief` |
| **Zeigarnik effect / plan-making** | Zeigarnik 1927; Masicampo & Baumeister 2011 | A handoff renders its `Next:` before its `Done:`; a handoff without a next step is answered with the open todos and a nudge to name what comes first. | `tools.handoff`, `_split_handoff` |
| **Cognitive load theory** — keep a fact and what it qualifies together | Sweller 1988 ([overview](https://edutechwiki.unige.ch/en/Cognitive_load)) | Claim warnings sit in the header of `file_summary`/`read_snippet`; ⚠ sits on the pitfall it belongs to; "changed since last check" names the claimant inline. | `tools._claim_warning`, `render_brief` |
| **Reflexion** — stale reflections may describe bugs that are gone | Shinn et al., NeurIPS 2023 ([arXiv 2303.11366](https://arxiv.org/abs/2303.11366v4)) | Pitfalls with fresh refs rank above pitfalls whose files changed since. | `render_brief` |
| **Transactive memory** — know who knows | Wegner 1986 ([text](https://dtg.sites.fas.harvard.edu/DANWEGNER/wjh/tm.htm)) | Summaries and note refs are grouped by top-level directory and author: the brief shows `experts: src/ → claude (31)`, and a `file_summary` miss says "codex wrote 14 summaries under src/ — ask them with post". Questions get routed instead of broadcast. | `store.experts`, `tools._expert_hint` |
| **Near-duplicate detection** | SimHash, Charikar 2002; Mem0's ADD/UPDATE/NOOP ([arXiv 2504.19413](https://arxiv.org/abs/2504.19413)) | Before a note is saved, a 64-bit SimHash over word 1–3-gram shingles is compared with the project's active notes; within Hamming distance 6, the reply names the similar note and suggests `supersedes`. The decision stays with the agent. | `memdex/simhash.py`, `tools.remember` |
| **Key expansion** (SPLADE without the model) | Formal et al. 2021; LongMemEval, Wu et al. ICLR 2025 ([site](https://xiaowu0162.github.io/long-mem-eval/)) | Identifiers and paths are split into words in the FTS text and in queries (`Store.index` → `store index`, `fts_map` → `fts map`), and a note's cited paths/symbols are part of its key, so searching a file name finds the notes about it. | `memdex/keys.py`, `store._file_text`, `store.add_note`, `store._fts_hits` |
| **Caller-generated recall cues** | LongMemEval, Wu et al. ICLR 2025 ([paper](https://arxiv.org/abs/2410.10813)); mnemonic variability, Cowan et al. PNAS 2024 ([paper](https://doi.org/10.1073/pnas.2311077121)) | The agent's current LLM can supply up to four alternate cues — aliases, related terms, paths or time clues — to `search`. They are fused below the literal query and each result reports the cue that found it. Memdex does not make an LLM call. The PNAS result motivates cue diversity as a hypothesis; its human-learning task does not directly validate code-memory retrieval. | `tools.Session.search`, `store.search` |
| **Point-in-time note versions** | LongMemEval, Wu et al. ICLR 2025 ([paper](https://arxiv.org/abs/2410.10813)); APEX-MEM, Banerjee et al. ACL 2026 ([paper](https://aclanthology.org/2026.acl-long.749/)) | `search(as_of=...)` reconstructs which append-only note version was current at a record time. `supersedes` closes the prior version at replacement creation; schema v9 stores archive time separately from general edits. Older FTS entries are retained/backfilled on schema v8, including notes received by sync. This is record-time history, not a bitemporal claim about when a decision was true in the world. | `store._note_valid_at`, `store.search`, `sync._index_note`, schemas v8–v9 |
| **Domain-validity time** | APEX-MEM, Banerjee et al. ACL 2026 ([paper](https://aclanthology.org/2026.acl-long.749/)) | `valid_from` / `valid_until` store an explicitly known effective interval separately from note creation/replacement time. `search(valid_at=...)` filters by the domain interval; unknown bounds stay open. The timestamps are agent-supplied facts, not inferred. | `store.add_note`, `store._note_domain_valid_at`, `tools.Session.search`, schema v10 |
| **Procedural experience replay** | APEX-EM, Findings of EMNLP 2026 ([paper](https://aclanthology.org/2026.findings-emnlp.1996/)) | `experience` notes keep task context, ordered steps, evidence, and a success/failed/partial outcome. Their fields are indexed and sync across machines. Success gets a small rank tie-break; failures remain visible. This is a structured capture mechanism, not an autonomous reward model. | `store.add_note`, `store.search`, `tools.Session.remember`, schema v10 |
| **Depth Tree / completion ledger** | [unlazy](https://github.com/Leonxlnx/unlazy), inspected 2026-11-03 | `tasks` represents substantial work as parent goals and independently omittable child outcomes. Blockers remain explicit; a leaf must carry report evidence and a separate check before becoming `self_verified` or `verified`; parents cannot be reported with unchecked children. This borrows the ledger and re-verification idea, not the skill's arbitrary shell `CHECK:` runner or optional Stop hook. Evidence is still agent-supplied, so the ledger makes claims inspectable rather than proving them true. | `Store.create_task`, `Store.task_action`, `tools.render_task_overview`, schema v11 |
| **Retrieval regression set** | AMA-Bench, Zhao et al. ICML 2026 ([paper](https://proceedings.mlr.press/v306/zhao26bs.html)); LongMemEval | `memdex eval` runs five deterministic synthetic cases for exact search, cue recall, record-time history, domain-validity, and experiences; it reports Recall@k and MRR. It is a regression set, not a representative user benchmark. | `memdex/evaluation.py`, `tests/test_retrieval_eval.py` |
| **Human skill preflight** | Agent Skills format; Skill-Inject 2026 ([paper](https://arxiv.org/abs/2602.20156)) | `skills show` reports scope, requested tools, bundle hash, local referenced text files, known hazards, and approval state; unapproved instructions are withheld from MCP. The interactive local CLI displays the bundle for review and records approval for its exact hash. Remote links are listed, not fetched. Pattern scans do not prove safety. | `skills._bundle_files`, `skills.approve`, `tools.Session.skills`, `cli.cmd_skills` |

## Deferred (good ideas that need usage data or a model first)

- **Complementary learning systems / sleep-time consolidation** (McClelland et al. 1995; Letta 2025): memdex can queue
  clusters of related notes for an agent to digest into one; it cannot write the digest itself. Planned as
  `memdex digest` once projects accumulate >50 notes.
- **Note↔note links** (A-MEM, NeurIPS 2025) from shared file/symbol refs and SimHash neighbours.
- **Surprise-weighted change lists** (prediction-error encoding; Titans): rank "changed since last check" by how much
  the outline moved and whether notes cite the file.
- **Feedback-calibrated ranking**: use opt-in, anonymized task outcome labels to tune cue and success boosts. The
  current fixed weights and synthetic eval are too small to justify learning from private user memory.

## Rejected

- **Bloom / xor / binary-fuse filters**: every membership question memdex asks is a B-tree lookup in a local SQLite file.
- **HNSW / DiskANN**: a project has hundreds to a few thousand vectors; brute-force cosine is exact and fast enough.
- **Content-defined chunking**: symbols are a better boundary for code; `difflib` answers "how much changed" for prose.
- **Late chunking, LLMLingua, HippoRAG's PageRank, Titans' neural memory**: each needs a model inside memdex.
- **MemoryBank's exponential forgetting**: replaced by ACT-R's power law, which keeps instead of dropping.
- **Generation effect** (Slamecka & Graf 1978): already embodied — agents write their own summaries; memdex never
  auto-fills one from an outline.

## Method

The survey compares ideas with the constraints that Memdex stays local, standard-library/SQLite based, and makes no
model call internally. Human-learning findings are treated as design hypotheses; the retrieval evaluation is synthetic
until real, privacy-preserving misses are available. Corrections and new candidates are welcome as issues.
