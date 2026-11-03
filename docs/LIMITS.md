# The limits, and what research says about fixing them

`README` lists what memdex is not. This page takes each of those limits and asks the harder question:
what would removing it actually take, and which published technique is the cheapest honest answer?

The constraints do not move: **Python standard library + one SQLite file**, **no model call inside
memdex**, **local-first and private by default**, and every feature must save more tokens than it costs.
Anything that needs a model is still allowed — it just has to be *the agent's* model, the way summaries
are written by the agent today. That constraint is the reason half the candidates below are "let the
agent hand us the numbers" instead of "run a model here".

Verdicts below are the recommendations from the original design survey. The implementation status at the
bottom supersedes these forecasts where work has since landed.

---

## 1. Nothing is shared between machines

**Current state.** One SQLite file per machine; worktrees of one checkout share memory. `export --sync` and
`merge` move notes and summaries through a directory the user controls. Claims and messages remain local.

**Why it is the hard one.** Files give a natural conflict boundary. Notes do not: two agents on two
machines can edit the same note, and both edits are legitimate.

**Research.**

- *Append-only + per-field last-writer-wins* is the classic answer. An OR-Set of note ids (Shapiro et
  al. 2011, *Conflict-free Replicated Data Types*) plus an LWW register per field is enough for notes,
  because a note is small and the fields are independent.
- *Delta-state* replication (Almeida, Shoker & Baquero 2018) is what you want in practice: sync sends
  the changed elements since the last state vector, not the whole set.
- *Range-based set reconciliation* (Meyer 2023, [thesis](https://github.com/AljoschaMeyer/master_thesis);
  Eppstein et al. 2011, *What's the difference?*) reconciles two sets of hashes that differ in a few
  places without either side sending its whole list — O(log n) messages over a dumb transport. This is
  the technique that makes "sync over a git remote / a shared folder / Syncthing" cheap.
- Content-addressed summaries are already Merkle leaves (see `RESEARCH.md`); a per-project root hash
  over `(path, sha)` is what `git` does for trees (Merkle 1987). "Which summaries disagree" becomes a
  tree walk, not a diff of databases.
- The prior art that already works: `cr-sqlite` ([vlcn-io/cr-sqlite](https://github.com/vlcn-io/cr-sqlite))
  turns a SQLite file multi-master; Syncthing's [BEP](https://docs.syncthing.net/specs/bep-v1.html) does
  block-level sync for whole files.

**Implemented version.** `memdex export --sync <dir>` writes one JSON file per note and summary, keyed by
stable `uid`; `memdex merge <dir>` applies whole-note last-writer-wins by `updated_at` and reports conflicts.
This is not the per-field LWW proposed above. No network code or server is involved.

**What not to sync.** Claims. A lease that outlives a machine is a deadlock generator; cross-machine
coordination needs fencing tokens (Kleppmann 2016, *How to do distributed locking*) and a clock, and
neither is worth it here. Claims stay local; `export` says so.

**Verdict: implemented.** Future work is incremental/range-based reconciliation if whole-directory exports
become too costly; current sync is intended for small local-first stores.

---

## 2. Search is keyword-based, not semantic

**Current state.** FTS5 (trigram), identifier expansion, RM3-lite, optional local embeddings, and up to four
caller-generated recall cues are fused with RRF. `memdex eval` checks a small deterministic synthetic set.

**Research.**

- *Pseudo-relevance feedback* / RM3 (Lavrenko & Croft 2001; Abdul-Jaleel et al. 2004): run the query,
  take the top-k results, add their most distinctive terms, rerun. Pure statistics over the local
  corpus, no model, and it recovers a surprising share of paraphrase misses. Cost: one extra query per
  search over a table of a few hundred rows.
- *Statistical semantics* (Church & Hanks 1990, word association norms): co-occurrence with pointwise
  mutual information gives an expansion list that is corpus-specific — `bug` expands to `defect`,
  `flaky`, `retry` in *this* project, not in general English.
- *Bring-your-own vectors*: the agent already runs a model. `remember(embedding=[...])` lets it hand
  memdex a vector it computed with whatever provider it is using; memdex stores floats and does exact
  cosine in SQLite. This keeps the "no model inside memdex" rule and removes the Ollama dependency for
  semantic search. `HNSW` is still unnecessary below ~10⁴ notes (see `RESEARCH.md`'s rejection).

**Implemented version.** RM3-lite is in `store.search`; optional cues are supplied by the caller. The next
step is adding anonymized misses to evaluation before changing rank weights.

**Verdict: implemented with a synthetic evaluation baseline.** The five current cases are not representative
of real workloads, so no quality claim should be inferred from their score.

---

## 3. Note ranking is a single scalar

**Current state.** ACT-R retrieval strength, Bjork-style storage strength, kind priors, importance,
retrieval-induced forgetting, and a small success preference for procedural experiences inform ranking.

**Research.**

- *Storage vs retrieval strength* (Bjork & Bjork 1992): already implemented; a well-confirmed old note can
  stay in the brief after retrieval activation decays.
- *Retrieval-induced forgetting* (Anderson, Bjork & Bjork 1994): retrieving one memory suppresses its
  competitors. Applied here: when a note is superseded, its neighbours in the same file/topic should
  lose rank, not just the superseded one being hidden.
- *Spreading activation* (Collins & Loftus 1975, *A spreading-activation theory of semantic processing*):
  activation flows along links, so a note that cites a file the session is working in should outrank a
  better-provenanced note about an unrelated file. memdex already has the links (file/symbol refs) — it
  just does not propagate along them. A one-hop boost with a decay factor is a dozen lines in
  `list_notes`.
- *Synaptic tagging and capture* (Frey & Morris 1997): a weak memory formed near a strong event gets
  consolidated. memdex has the equivalent moment — `handoff` — and could promote the notes that were
  read during a session that ended in a clean handoff. That is a cheap proxy for "this memory was
  useful", and it is the only outcome signal memdex is ever going to get without asking the user.

**Verdict: implemented.** A future improvement would compare task outcomes against which memories were used,
but that requires privacy-preserving real usage data before any weight learning.

---

## 4. Locks are enforced where memdex is asked, not by the file system

**Current state.** TTL claims, an exclusive claim, a Claude Code `PreToolUse` hook, a git `pre-commit`
hook. A session without the hook can still write.

**Research.**

- *Fencing tokens* (Kleppmann 2016): every lock grant carries a monotonically increasing token, and the
  resource refuses writes carrying a stale one. memdex's claims already have an expiry; they lack the
  token, so a timed-out session cannot be told apart from a live one.
- *Optimistic concurrency* — compare-and-swap on `(size, mtime, sha)`: the pre-edit hook can refuse an
  edit when the file changed since the claim was taken, even if the claim still looks valid. That is the
  cheap version of "the lock means something", and it catches the real failure (two agents editing the
  same file in the same minute) about as well as a filesystem lock would.
- The general result that advisory locks are not locks (Gray & Reuter, *Transaction Processing*) is why
  memdex should keep saying "Git is still the source of truth" rather than promise enforcement.

**Verdict: now** — a `token` column on claims and a CAS check in `hook_pre_edit` is a small, testable
change; the README's caveat then becomes true in a stronger sense.

---

## 5. The brief is trimmed by priority, not by value per token

**Current state.** Lines carry a priority, the middle is cut when the budget is exceeded.

**Research.**

- *Submodular coverage maximisation* (Nemhauser, Wolsey & Fisher 1978): choosing what to show so that
  the union of covered facts is maximal, with the greedy algorithm guaranteed within (1−1/e) of optimal.
  Needs a "what does this line cover" notion — for memdex, the files/symbols the line mentions.
- *Maximal marginal relevance* (Carbonell & Goldstein 1998): penalise a line for repeating what an
  already-chosen line says. The brief currently shows three pitfalls about the same file; MMR would show
  one and spend the space elsewhere.
- *Knapsack instead of priority*: with per-line token cost and a value, the exact optimum is a DP over
  the budget (items ≤ 100, budget ~1000 tokens — trivial). Priority-greedy is an approximation of this
  and is measurably worse when line sizes vary (they do).

**Verdict: now** — MMR plus a knapsack over the existing per-line priority score is contained inside
`render_brief` and directly cut by the same tests. No new concept for the agent to learn.

---

## 6. The skill registry is one machine, and trusts what it finds

**Current state.** Skills are registered by harness and name, with local bundle hashes, referenced-file scans,
hazard findings, approval state, and conflicts. A skill remains untrusted advice and can contain prompt injection.

**Research.**

- *Supply chain*: the local bundle content hash is shown and human approval is attached to that exact hash.
  Cryptographic signatures and publisher identity remain future work; local hash approval does not establish
  who authored a skill.
- *Prompt injection* as a security property is not solved; the practical mitigation is static review of
  what a skill asks the agent to run (the same approach marketplace scanners take: dangerous command
  patterns, secret reads, network calls). memdex can flag, not judge.
- Cross-machine skill sharing is limit #1 again: a skill is a file, so it is the *easiest* thing to sync
  — and the most dangerous to sync, which is exactly the asymmetry a signature fixes.

**Verdict: partially implemented.** Local references are hashed and scanned, and the CLI requires an
interactive exact-hash approval before MCP returns instructions. Remote links are only listed; static
patterns cannot establish that a skill is safe or appropriate for a task.

---

## 7. A tiny repo does not need it (cold start)

**Current state.** Under ~20 files a hand-written `AGENTS.md` is cheaper.

**Research.**

- Recommender cold start is solved at write time, not at query time: seed the store with what already
  exists. `memdex import AGENTS.md CLAUDE.md README.md` would turn the project's existing prose into
  notes with file refs — the first session then starts from a non-empty memory instead of an empty one.
- *Complementary learning systems* (McClelland, McNaughton & O'Reilly 1995; Kumaran, Hassabis &
  McClelland 2016): fast episodic storage (handoffs, notes) and slow semantic consolidation (distilled
  facts) are different systems, and consolidation happens offline. memdex can host the queue — cluster
  related notes, and have the *agent* write the digest when it has context (`memdex digest`), the same
  division of labour as summaries. Sleep replay (Wilson & McNaughton 1994) is the biological existence
  proof that an offline pass over the day's episodes is worth the time.

**Verdict: implemented as an agent-reviewed digest queue.** Memdex proposes note clusters; the agent writes
the consolidated result when it has enough task context.

---

## Implementation status (2026-11-03)

The earlier `now` / `next` table was a planning snapshot and is now retired. The repository includes the
brief budget (knapsack + MMR), claim fencing and compare-and-swap, RM3-lite and multi-cue search, spreading
activation, `import`, `export --sync` / `merge`, bring-your-own vectors, `skills verify`, `digest`, and the
storage/retrieval strength split. This revision adds:

- `memdex eval`, a deterministic synthetic Recall@k / MRR regression set. Its five cases cover exact search,
  cue recall, record-time history, domain-validity, and procedural experience retrieval.
- `experience` notes with task context, ordered steps, evidence, and explicit outcome labels.
- `valid_from` / `valid_until` and `search(valid_at=...)`, distinct from `search(as_of=...)` record history.
- Skill bundle hashing over bounded local references, metadata/hazard review, and interactive approval of an
  exact hash before MCP returns the skill body. This improves reviewability but does not make prompt injection
  decidable by static scanning.
- A hierarchical work ledger with observable criteria, assignment, explicit blockers, report evidence, and
  separate self-check/peer-check states. Parent work cannot be reported while children are unchecked, and
  handoffs/briefs keep unfinished branches visible. It is local-only and cooperative; it cannot verify free-form
  evidence or stop an agent from ignoring the ledger.

Remaining research items are note-to-note links, surprise-weighted changed-file lists, publisher signatures for
skills, and ranking calibration from privacy-preserving real task outcomes.
