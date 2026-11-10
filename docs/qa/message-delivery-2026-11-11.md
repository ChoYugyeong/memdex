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

## Live native-context correction

The first authorized live send found a further defect: the host supplied the correct native Codex chat
ID, but the proxy's independent `who` subprocess inherited a shared app-server environment without that
ID. It resolved a different caller and correctly failed the consistency check. Matching task IDs alone
could not fix this transport mismatch.

The proxy now validates each host-supplied native ID against local provider/session metadata, builds a
per-call child environment, and uses it for both identity checks and that native chat's downstream MCP
connection. Multiplexed chats use separate bounded backend connections, including for inbox reads.
Initialization is replayed privately before forwarding a call on a new connection. Failed startup never
becomes a usable connection on retry. No source ID is accepted from model arguments, and process-wide
environment variables are not mutated. The notifier uses the same native environment.

Regression cases cover child-environment identity, restoration, provider conflicts, duplicate raw IDs,
and separate downstream connections for two native chats. The full suite reached 680 passing tests;
read-only live authorization resolved the real Codex caller and approved the intended same-task Claude
peer without weakening the guard. Live delivery results are recorded separately after actual receipts.

## Authorized live round-trip

Using the user's existing Claude sign-in, a dedicated Claude Code streaming session connected to only
the two local MCP servers. It self-registered task 32 in the memdex project; the actual Codex chat
registered that same task from its own host identity. No other working chat was reassigned.

- A freshly initialized guarded Reason MCP connection sent `REASON-PING-32-A` from the actual Codex
  native chat. Claude received a native push and used the incoming ID in its guarded Reason reply,
  `REASON-PONG-32-A`. Codex read that reply. Reason's receipt advanced to `replied`.
- A fresh memdex MCP connection queued `MEMDEX-PING-32-B` as message 125 and obtained an accepted Reason
  notification. Claude received the native notification, then called its own project brief and inbox
  (no proactive memdex polling), and replied to message 125 with `MEMDEX-PONG-32-B` as message 126.
  Codex received a native notification for 126 and read the reply from its exact inbox. The two stored
  envelopes reverse the original sender/recipient routes and preserve `reply_to=125`.
- Retrying the original request key recovered message 125 rather than inserting another message.

Full native IDs, provider receipts and the Claude tool-call transcript are retained only in the local
private QA directory, not this public repository. The dedicated Claude runner was stopped after the
two probes. This demonstrates actual guarded MCP delivery, notification-triggered inbox reading and
reply routing; it does not authorize arbitrary unbound legacy chats.

Before the desktop restart, the already-loaded in-app MCP clients still returned the old receipt format
and the old `host/backend native identity mismatch` refusal on the final idempotent retries. Their configuration
was backed up and given a connection revision for the two local servers, but a self-directed follow-up
arrived inside the active turn and did not recreate the connections. No duplicate messages were sent.

The active desktop host exposes no MCP reconnect control to this chat. The separate managed
app-server does not own this native chat, so it was not used to reload unrelated connections. No
desktop UI input, application termination or blanket worker termination was performed. At that point,
task 32 remained incomplete pending a reconnect in the owning app and the same idempotent native-tool
retries. The confirmed fresh-connection round-trips above did not establish that cached clients had
reloaded.

## Native in-app reconnect after restart

After the user restarted the desktop app, this same Codex chat used its ordinary in-app MCP tools:

- `memdex.post` with the original request key recovered message 125 and returned the new
  `Reason notification accepted; inbox read is not yet confirmed` status. This demonstrates that the
  in-app memdex client loaded the updated notifier; the acceptance text itself is not a new read receipt.
- Guarded `Reason.send_message` with the original key recovered the original Reason message ID without
  `host/backend native identity mismatch`. Native `message_status` confirmed that message remained
  `replied`; its corresponding memdex notification remained `handed_to_agent`.
- Native route registration resolved the same Codex chat/address and task 32. Its inbox had no unread
  messages, consistent with the earlier recorded read of reply 126.
- Both retries preserved the original message IDs. The dedicated Claude runner remained stopped; this
  reconnect check reused the actual round-trip receipts above instead of claiming a new peer response.

The in-app deployment blocker is resolved. The earlier 680-test suite result and subsequent focused
guard/proxy checks remain the code validation; this final update changes only QA evidence. Private
reconnect receipts are saved beside the earlier round-trip transcript. Task 32 can now be reported and
self-verified using the combined live-delivery and in-app reconnect evidence.
