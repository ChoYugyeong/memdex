# Exact message notification delivery — 2026-11-11

The reported symptom was a Claude response telling the human to ask Codex to check memdex manually.
Read-only inspection found three distinct failures: missing native project/task bindings blocked Reason
replies; fallback memdex messages had a bare route hash in the workspace mailbox instead of a full route
in the recipient's project; and an obsolete five-minute wake task loaded schema-v13 code against a v17 DB.
Saving a message therefore did not establish correct addressing, a notification, or an inbox read.

## Changes

- Reject truncated/missing-prefix route destinations before storage. Never treat a route hash as a family
  name. Existing incorrectly addressed historical messages are not moved to another project/chat by guess.
- Accept Claude native IDs from the current host envelope or shell. If the host omits its ID, resolve only
  Reason's exact current native caller, with a provider match. Resolve on each call, not from a previous
  chat's cached identity. A custom/isolated database does not query the user's live Reason service.
- After an exact post commits to the default DB, attempt a pointer-only Reason notification to the
  registered native recipient. Resolve provider plus native ID, then apply the existing guard, including
  backend/source agreement and a shared open project/task. Verify the returned proof still identifies
  the exact destination after lookup. Never fall back to a same-name, newest, idle or unrelated chat.
- Reuse a stable notification idempotency key on post retries. Keep mail and its cursor unchanged on
  denial, unavailable metadata, backend error or timeout. Report queued mail and notification acceptance
  separately; acceptance does not prove that the recipient read the inbox.
- No new tool, dependency, AI worker or polling daemon. `MEMDEX_REASON_NOTIFY=0` disables notifications;
  custom database copies require explicit opt-in. Default backend paths follow the existing local Reason
  installation; `MEMDEX_REASON_BACKEND` and `MEMDEX_REASON_DB` permit other local installations and fixtures.

The installed Reason task guard is deliberately unchanged in scope: legacy/unbound conversations do not
gain permission merely because an incoming message exists. Both intended chats must register their actual
open task ID. The changed refusal identifies which endpoint still needs registration.

## Verification

The notification tests use temporary SQLite stores and mocked delivery. They cover commit-before-notify,
body-free payloads, unchanged unread cursors, retry keys, cross-task refusal, missing bindings, transport
failure, wrong backend identity, native-name reuse, copied DB isolation and malformed route rejection.
A real stdio exchange against a Python fake backend tests initialization, sending and child reaping.
Claude tests exercise per-call ID handling, metadata conflicts and exact native-caller fallback without
identity leaking between calls. No live peer message is sent by these tests.

The local installation additionally verified its native caller using read-only Reason metadata. The
obsolete wake task definition was exported before disabling it; the separate assigned-work and DeepSeek
maintenance tasks remain enabled. This OS task change is local deployment state, not a repository action.

Existing long-running MCP processes need to reconnect to load changed Python code. A newly launched CLI
uses the new code immediately. No successful live-peer wake or reply is claimed by the isolated tests.
