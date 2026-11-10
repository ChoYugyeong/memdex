# Reason MCP task-scope guard — 2026-11-10

The earlier incident selected an unrelated existing native chat as recipient through Reason's independent
transport. This optional stdio proxy enforces scope at the configured MCP send boundary, rather than
depending on a title or an agent remembering a routing guideline.

## Policy

- Source identity comes from per-call host native metadata when available; otherwise Reason's own
  `who --json` native-session resolver must identify exactly one calling session. Source IDs supplied
  as model arguments are refused.
  When a host supplies a native ID it must agree with the backend's own resolved native session;
  disagreement refuses rather than assuming the downstream server will switch identities correctly.
- Destination must be one exact local person/session (optional exact local machine). It is resolved to
  its native provider/session key in Reason's local sessions table. No family, wildcard, title, nearest,
  idle-session or remote-identity guesses are permitted.
- Provider aliases (`gpt`/`codex`, `claude`/`claude-code`, DSH profile names) are normalized, but matching
  raw IDs under a different provider do not establish identity. PID-only endpoints cannot prove a chat.
- Both native chats must have the same unique open **project + task ID** in memdex. An optional
  `task_id` argument can select the source's registered scope; it cannot create a binding or authorize
  another task. Register from inside the intended chat with `memdex session routes --task-id ID` and
  the correct `--path` and `--agent`.
- Replies must match the original Reason message endpoints. Legacy messages are not blindly
  grandfathered: current scope must match unless this guard recorded approval for that conversation.
  A previously approved exchange can finish after tasks change, but not after the same session name
  is reused by a different native ID. An explicit different task cannot repurpose a reply thread.
- Metadata lookup failure is a refusal. Denials never reach Reason's backend MCP server. The extra
  task argument is stripped on allowed calls; host metadata and the original message are preserved.

The backend executable and Reason daemon are unchanged. Read-only tools still pass through. The guard
adds no MCP tool, package dependency, model call or embedding. MCP instructions explicitly prohibit
bypassing a refusal with the backend CLI or another chat.

## Local setup

Launch the existing server through:

```text
python -m memdex.reason_guard --backend C:\Users\cho46\.reason\bin\reason-network.exe
```

The backend still runs `reason-network.exe mcp`. Each client keeps its existing
`REASON_NETWORK_AGENT` environment configuration. Default metadata paths are `~/.reason/network/network.db`
and `~/.memdex/memdex.db`; the guard ledger is `~/.memdex/reason-guard.db`. `--check PERSON/SESSION`
validates a destination without sending. Optional explicit DB/ledger paths support isolated fixtures.

Before configuration changes, back up the exact client configurations and the SQLite DB using SQLite's
backup API. Existing direct MCP processes must reconnect through the new command; do not terminate the
Reason daemon or the AI chat/application itself.

## Tests and evidence

21 isolated guard tests cover unknown/missing scope, correct native-provider matching, same-title wrong
task, attempts to override task/source identity, conflicting host IDs, stale names/native-ID reuse,
PID endpoints, closed tasks, wildcard/remote guesses, original-message replies, approved replies after
task changes, metadata lookup failure, schema decoration, body-free auditing, and actual stdio refusal
versus allowed forwarding with a mocked backend. No real message is sent during QA.

The complete suite includes the 641 prior tests (662 total). All live smoke tests must use only
initialization, tool discovery, read-only inventory and negative validation; functional allowed sends
are tested against fake backends.

## Deliberate limits

- This protects the MCP configurations that launch this proxy. It does not replace or intercept direct
  invocations of Reason's binary, native bridge, unconfigured remote machines, or future installer changes.
- Remote sessions and PID-only DSH profiles lack verifiable local native-chat metadata and are refused.
  Use memdex's host-bound exact-chat channel for a DSH conversation instead of guessing from a profile PID.
- Work assignment remains cooperative/self-declared: task binding is explicit metadata, not proof of human
  authorization. The guard detects absent/conflicting bindings; it cannot infer intent from a prompt.
- Existing unbound chats must explicitly register their actual task. Historical bindings are not invented
  from titles, message topics, cwd `C:\Dev`, activity timestamps or the user's last unrelated task.
