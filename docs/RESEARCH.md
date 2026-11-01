# What memdex borrows from research, and what it deliberately does not

memdex is small, so every idea has to earn its place: it must be implementable with the Python
standard library and SQLite, need no model call inside memdex, and save more tokens than it costs.
This page records a survey (2026-11-01) of four fields — learning science, memory neuroscience,
recent data structures, and 2023–2026 LLM-agent memory research — with the verdict for each idea.
The adopted ones are in the code; the file and function are named so the mapping is checkable.

## Adopted

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
| **Skill directory** | Agent Skills layout; transactive memory again | `skills` registers every harness's `SKILL.md` files on the machine, so an agent asks "is there a skill for X?" once instead of searching, and can read another harness's skill to follow it. | `memdex/skills.py` |

## Deferred (good ideas that need usage data or a model first)

- **Complementary learning systems / sleep-time consolidation** (McClelland et al. 1995; Letta 2025): memdex can queue
  clusters of related notes for an agent to digest into one; it cannot write the digest itself. Planned as
  `memdex digest` once projects accumulate >50 notes.
- **Note↔note links** (A-MEM, NeurIPS 2025) from shared file/symbol refs and SimHash neighbours.
- **Bi-temporal validity** (Zep/Graphiti): `invalidated_at` on superseded notes, `valid_until` on temporary decisions.
- **Storage vs retrieval strength** (Bjork & Bjork): a second number guarding the cold tier so a well-linked old
  decision is never tiered out just because it was not needed lately.
- **uid/origin columns** for a future cross-machine merge (append-only notes are a trivial CRDT).
- **Surprise-weighted change lists** (prediction-error encoding; Titans): rank "changed since last check" by how much
  the outline moved and whether notes cite the file.
- **Usage-based `memdex stats`**: summaries served fresh vs files re-read, notes read by someone other than their
  author, stale notes confirmed vs ignored — the honest number for the README.

## Rejected

- **Bloom / xor / binary-fuse filters**: every membership question memdex asks is a B-tree lookup in a local SQLite file.
- **HNSW / DiskANN**: a project has hundreds to a few thousand vectors; brute-force cosine is exact and fast enough.
- **Content-defined chunking**: symbols are a better boundary for code; `difflib` answers "how much changed" for prose.
- **Late chunking, LLMLingua, HippoRAG's PageRank, Titans' neural memory**: each needs a model inside memdex.
- **MemoryBank's exponential forgetting**: replaced by ACT-R's power law, which keeps instead of dropping.
- **Generation effect** (Slamecka & Graf 1978): already embodied — agents write their own summaries; memdex never
  auto-fills one from an outline.

## Method

The survey was run by a research agent with web search across the four fields, producing thirty candidates with
sources, a mapping to memdex, an effort estimate and a verdict; the maintainers then implemented the adopted set
and recorded the rest here so the reasoning is not lost. Corrections and new candidates are welcome as issues.
