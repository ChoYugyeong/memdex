# The limits, and what research says about fixing them

`README` lists what memdex is not. This page takes each of those limits and asks the harder question:
what would removing it actually take, and which published technique is the cheapest honest answer?

The constraints do not move: **Python standard library + one SQLite file**, **no model call inside
memdex**, **local-first and private by default**, and every feature must save more tokens than it costs.
Anything that needs a model is still allowed — it just has to be *the agent's* model, the way summaries
are written by the agent today. That constraint is the reason half the candidates below are "let the
agent hand us the numbers" instead of "run a model here".

Verdicts are `now` (days, small diffs), `next` (a release or two), `later` (needs the store to grow
first), `no` (costs more than the limit).

---

## 1. Nothing is shared between machines

**Current state.** One SQLite file per machine, by design. Worktrees of one checkout share memory; two
laptops do not. `prune` exists because databases go stale, not because they sync.

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

**The smallest version memdex could ship.** `memdex export --sync <dir>` writes one JSONL per note and
per summary, named by `uid`, into a directory the user already syncs (a git repo, a Dropbox folder).
`memdex merge <dir>` applies the deltas: notes by LWW per field, summaries by sha (identical content is
the same summary, so the merge is free). No network code in memdex, no server, and the transport is the
user's problem — which is the point, because every transport they already trust keeps working.

**What not to sync.** Claims. A lease that outlives a machine is a deadlock generator; cross-machine
coordination needs fencing tokens (Kleppmann 2016, *How to do distributed locking*) and a clock, and
neither is worth it here. Claims stay local; `export` says so.

**Verdict: next.** It needs the `uid` column `RESEARCH.md` already parked, and it is the one limit users
ask about first.

---

## 2. Search is keyword-based, not semantic

**Current state.** FTS5 (trigram) + identifier expansion (`keys.py`) + optional Ollama embeddings, fused
with RRF. Good on exact names, blind to paraphrase.

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

**Smallest version.** RM3-lite inside `store.search`: top-3 hits, pull their 20 rarest terms, rerun,
fuse the two result lists with the RRF already there. ~40 lines, no schema change.

**Verdict: now** for RM3-lite; **next** for `remember(embedding=...)`.

---

## 3. Note ranking is a single scalar

**Current state.** ACT-R base-level activation + kind prior + importance; one number decides the brief.

**Research.**

- *Storage vs retrieval strength* (Bjork & Bjork 1992): a note that is well-connected but unread lately
  is not the same as a note nobody ever read. Two numbers instead of one — the deferred item in
  `RESEARCH.md`.
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

**Verdict: now** for one-hop spreading activation from the session's working files; **next** for
capture-on-handoff.

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

**Current state.** 200 skills across harnesses, refreshed by mtime, conflicts reported by name. Contents
are whatever is on disk: a skill is executable advice, i.e. a prompt-injection surface.

**Research.**

- *Supply chain*: a skill bundle named by its content hash and signed is what TUF (Samuel et al. 2010,
  *Survivable Key Compromise in Software Update Systems*) and Sigstore/in-toto solve at package scale. For
  memdex, the cheap 80% is: show the `sha` next to the skill, and let a synced registry carry the sha
  that the author published — a mismatch is then visible instead of invisible.
- *Prompt injection* as a security property is not solved; the practical mitigation is static review of
  what a skill asks the agent to run (the same approach marketplace scanners take: dangerous command
  patterns, secret reads, network calls). memdex can flag, not judge.
- Cross-machine skill sharing is limit #1 again: a skill is a file, so it is the *easiest* thing to sync
  — and the most dangerous to sync, which is exactly the asymmetry a signature fixes.

**Verdict: next** — `skills verify` (sha + optional signature) and a static scan of obvious hazards.

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

**Verdict: now** for `import`; **next** for `digest`.

---

## Status (2026-11-01)

Every item below except the `later` row has landed, in the order the table lists them: the brief budget
(knapsack + MMR), claim fencing and compare-and-swap, RM3-lite search, spreading activation, `import`,
`export --sync`/`merge` with note `uid`s, bring-your-own vectors, `skills verify`, and `digest`. The
`later` row waits for projects to accumulate the notes it is about.

## What this adds up to

| Verdict | Item | Rough size |
|---|---|---|
| now | RM3-lite query expansion | ~40 lines + tests |
| now | One-hop spreading activation from the session's files | ~20 lines |
| now | MMR + knapsack in `render_brief` | ~80 lines |
| now | Claim fencing token + CAS in the pre-edit hook | schema + hook |
| now | `memdex import AGENTS.md` | one command |
| next | `uid` + `export --sync` / `merge` (notes, summaries; never claims) | the big one |
| next | `remember(embedding=...)`, exact cosine in SQLite | medium |
| next | `skills verify` + hazard scan | medium |
| next | `digest` queue for the agent to consolidate | medium |
| later | Storage/retrieval strength split, retrieval-induced forgetting | after ~50 notes |

The order is deliberate: everything in the `now` row is local, model-free, and testable against the
existing suite. The `next` row is where the architecture changes, and #1 is the one that changes it —
which is why the rest waits for it.
