# Security Audit Report — memdex

**Date**: 2026-11-03
**Scope**: full (mapped to a local CLI/MCP server's surfaces — the game-engine pattern sets of the
security-audit skill do not apply to this domain, so each category is answered against the real surface)
**Audited by**: this session's source review

**Follow-up**: this revision adds bounded skill-bundle manifests and exact-hash human approval, experience
memory and domain-time inputs, sync support, and a bounded hierarchical task ledger. Task IDs and parent links
are scoped to the resolved project, status changes are validated in the store, free-form fields are length
bounded, and the feature does not execute task content. Task text is labeled untrusted in agent instructions.
Path traversal, symlink escapes, secret-file reads, and malformed sync data were reviewed and covered by
regression tests. The full unit suite and synthetic retrieval evaluation passed; no dedicated penetration
test or external security review was run.

## Executive Summary

| Severity | Count | Must Fix Before Release |
|----------|-------|------------------------|
| CRITICAL | 0 | — |
| HIGH | 0 | — |
| MEDIUM | 0 | — |
| LOW | 4 | Optional |

**Release recommendation**: no open CRITICAL/HIGH/MEDIUM findings were identified in this source review.
The four LOW items are documented trade-offs of the local-first design, one of which (sync plaintext) is now
stated in the README. Unit tests provide regression evidence, not a penetration test or a security guarantee.

## Category answers (mapped)

### 1. Serialization / database
- Only SQLite, written through one `Store` with WAL and a busy retry. Migrations refuse a **newer**
  schema instead of writing blind, and old versions upgrade in place (including the v9→v10 migration test).
- No `pickle`/`yaml.load`/`marshal`/`eval` anywhere (grep clean). Sync merges parse JSON per field with
  fallbacks and last-writer-wins; malformed exports are skipped, not executed.

### 2. Network
- **No network code.** The only outbound path is optional embeddings to Ollama (`MEMDEX_EMBED`),
  defaulting to `http://localhost:11434`, and the URL is user-configured via `MEMDEX_OLLAMA_URL`
  (see LOW-3). MCP itself is stdio to a local client process.

### 3. Input / injection
- JSON-RPC: unknown tools answer an error, extra arguments are filtered by signature, and a raising tool
  is reported to the client without killing the server (tests/test_robustness.py).
- Repository and shared task text: a bounded hazard pattern set (piped downloads, `core.hooksPath`, secret uploads, …) is named
  with line numbers at `memdex import`. Brief and MCP instructions label repository files, notes, messages, and
  task titles/evidence as untrusted data. Task fields are capped, SQL values are parameterized, and no task
  field is passed to a shell or executable (tests/test_injection.py, tests/test_tasks.py).
- Path traversal: `read_snippet`/`file_summary` resolve through `to_rel` and refuse files outside the
  project and secret-like files (`.env`, keys), including nested `..` and symlink escapes. Bulk indexing
  verifies resolved targets before reading; FTS wildcard queries are answered, not executed
  (tests/test_robustness.py).
- All subprocesses run argument lists (`shell=True` absent): git plumbing (timeout 15s), `claude mcp add`,
  the serve-check handshake, optional kensaku CLI. DeepSeek Harness launches `sys.executable -m memdex serve`
  with `PYTHONPATH` pinned to the package that wrote its config, avoiding PATH shims and shell interpolation.

### 4. Data exposure
- No hardcoded secrets (grep over the package and `pyproject.toml` clean).
- Notes, messages, task plans, evidence and status history sit in `~/.memdex/memdex.db` in plaintext — local,
  single-user, per-design (LOW-1).
- Hooks and client configs written by `memdex connect` contain no secrets and keep a `.bak` before writing.
  The DeepSeek Harness adapter adds one managed entry to the existing Cordis YAML list, refuses an unmarked
  entry collision or a non-list document, and leaves other profile patches intact. The configured model can
  call the Memdex tools available to its session, so normal MCP data-access permissions apply.

### 5. Tamper / integrity
- The design is advisory-by-default; `claim --exclusive` carries a fencing token + the file's sha, and the
  pre-edit/pre-commit hooks refuse stale edits. `MEMDEX_ALLOW_LOCK` is a documented escape hatch, not a
  bypass (LOW-4). Task completion is also a collaboration protocol, not tamper-proof attestation: an agent can
  supply inaccurate evidence, and a local actor can edit the database directly. Git and the checked artifact
  remain the source of truth (accepted risk).

### 6. Supply chain
- **Zero runtime dependencies** (`dependencies = []`); build-time `setuptools>=77` pinned. Skill content is
  untrusted input. The preflight manifest hashes each bounded local reference file under the skill folder;
  it does not follow symlinks or fetch remote URLs. `skills show` withholds instructions until the exact
  current bundle hash is approved through the interactive local CLI. MCP cannot approve a skill. Approval
  confirms that a human reviewed that bundle version; it does not prove the skill is safe. Hazard scanning
  is heuristic and may miss malicious instructions.

## LOW Findings

### SEC-001: notes, messages and task records are plaintext in `~/.memdex/memdex.db`
- **Category**: Data exposure
- **File**: `memdex/store.py` (design)
- **Description**: the memory store is an unencrypted SQLite file on the user's disk.
- **Attack scenario**: anyone with read access to the user profile can read summaries, notes, messages, task
  descriptions, blockers and completion evidence.
- **Remediation**: none required for the local-first design; a documented boundary ("one machine, one
  user") is the mitigation.
- **Effort**: N/A — Accepted Risk

### SEC-002: `export --sync` writes plaintext into a directory
- **Category**: Data exposure
- **File**: `memdex/sync.py`
- **Description**: notes and summaries are exported as plaintext JSON into a user-chosen directory.
- **Remediation**: README now states it: export only into a directory you trust; claims and messages never
  travel.
- **Effort**: Low — done (README)

### SEC-003: `MEMDEX_OLLAMA_URL` can point embeddings at any host
- **Category**: Network
- **Description**: the optional embedding backend URL is an environment variable; setting it to a remote
  host would send the indexed text there.
- **Remediation**: document that the variable means "send text to this host"; default is localhost and the
  feature is off unless configured.
- **Effort**: Low — Accepted Risk (user-configured, opt-in)

### SEC-004: `MEMDEX_ALLOW_LOCK` overrides the commit hook
- **Category**: Tamper
- **Description**: the escape hatch lets any commit bypass the pre-commit lock check.
- **Remediation**: it is deliberate (a human who knows the lock is stale must be able to commit); keep it
  documented.
- **Effort**: N/A — Accepted Risk

### SEC-010: Memdex agent names are self-declared
- **Category**: Identity / audit integrity
- **File**: `memdex/tools.py`, `memdex/store.py`
- **Description**: a process can choose `MEMDEX_AGENT`; task `verified` means a different label recorded a
  check, not that Memdex authenticated the reviewer.
- **Remediation**: task output and setup docs now state this boundary, and call the state a peer check. The
  local collaboration workflow does not claim authenticated identities.
- **Effort**: Low — documented; authentication is outside the current local-first trust model.

### SEC-011: Multiple DeepSeek Harness profiles could receive the same managed MCP entry
- **Category**: Configuration scope
- **File**: `memdex/connect.py`
- **Description**: automatic discovery can find multiple Harness profiles. Writing to all of them on a
  broad `connect` command can configure a profile the user did not intend to change.
- **Remediation**: when multiple Harness profiles are selected, `connect` now stops before writing and asks
  for `--profile NAME`; a single discovered profile remains automatic for compatibility.
- **Effort**: Low — fixed with a pre-write selection check.

## Dependency Inventory

| Library | Version | Source | Known CVEs |
|---------|---------|--------|------------|
| (runtime) | — none — | standard library only | — |
| setuptools (build) | >=77 | PyPI | none known |

## Remediation Priority Order
1. No blocking findings identified in this review. Re-audit after any feature that adds a new input surface.

## Re-Audit Trigger
Re-run after adding any network feature, any new file-reading path, or a new config writer.
A human pentest is recommended before this tool is ever used to hold secrets (it is not built to).

## Follow-up Findings Closed in This Revision

- **SEC-005 (MEDIUM) — nested path traversal**: `sub/../../file` previously passed the string-prefix check.
  `to_rel` now resolves the target and checks `commonpath` containment.
- **SEC-006 (MEDIUM) — project symlink escape**: indexing verifies resolved targets against the checkout
  boundary and secret-name rules before opening files.
- **SEC-007 (MEDIUM) — skill symlink and unreviewed use**: scans refuse symlinked or out-of-folder paths,
  sensitive-looking files, and unsupported or unbounded references; bounded bundle manifests cover supported
  local text files and show hash drift. Descriptions and findings are labeled untrusted, and displayed remote
  URLs omit credentials and query values. `show` withholds instructions until a human approves the exact
  current hash in the interactive CLI; MCP cannot approve. Remote references are not fetched, and the hazard
  scan is heuristic, so approval remains a user trust decision.
- **SEC-008 (MEDIUM) — sync import symlinks and malformed fields**: import now skips symlinks/out-of-folder
  paths and oversized files, validates field types and refs, and export replaces files atomically.
- **SEC-009 (LOW) — local database permissions**: POSIX default storage directories use mode `0700`; the
  database and existing WAL/SHM files are restricted to `0600`. Windows uses its user-profile ACLs.
