# Chat metadata and wrong-task discovery — 2026-11-10

## Observed failure

The user reported a design discussion from a Claude project chat continuing inside an unrelated Codex
review-materials chat. Read-only inspection of the specific Reason message, its session-to-native-ID
mapping and the exact recipient's local rollout established:

- The source explicitly selected the existing unrelated Codex session as recipient.
- Reason delivered to that selected native chat; the recipient answered there.
- Both sessions advertised the workspace root, no repo and no current-task description.
- No contemporary matching message was present in memdex's mailbox. This incident used Reason's
  independent transport, not memdex exact-route delivery.

Private session IDs, message bodies and user chat titles are omitted from this public report. A local
incident note records the evidence; no messages were sent, chats renamed or workers interrupted.

This is a recipient-selection failure, not proof of an ID collision. An idle chat is not an available
chat for a different task. memdex cannot intercept another MCP server's `send_message` tool; the source
must use an explicitly assigned task chat or request a new one. The changes below fix memdex's missing
discovery evidence, not Reason's independently selected recipient.

## memdex changes

The live registry had 86 addresses, 81 without task labels. It stored only an opaque route hash: even
an exact address did not identify the native chat to inspect. Inbox output included the agent and
destination but omitted the sender's exact chat identity and task.

- Persist `conversation_id` and optional `task_id` alongside each route (schema v17). The native ID comes from the host envelope or the
  chat shell environment, never a new model-supplied identity argument. Conflicting rebinding is refused.
- Display `chat_id`, task and checkout in discovery; the native ID can itself be searched. This enables
  inspecting/opening a specific chat with the host's own tools instead of guessing from `gpt@Dev`.
- Automatically register the explicit task from a task-aware MCP/CLI brief. Integrating hosts can send
  `_meta["memdex.dev/task-label"]` with their existing chat ID. Labels do not bleed across multiplexed
  calls. Ordinary calls preserve existing labels; an explicit task takes precedence for that brief.
- A task description/title is a search hint, **not** the work assignment. An explicit `task_id` binding
  must reference an open task in the same project's ledger. Register it from the intended chat with
  `session_routes(task_id=…)` or CLI `session routes --task-id …`. Sending new task-tagged work to an
  unassigned/differently assigned chat fails before inserting a message. If either end is already task-bound,
  an untagged send must also match the sender's binding; a title alone cannot override a conflicting ID.
- Do not offer an unlabelled destination as an automatic match. Retired duplicates of the same
  agent/checkout/task no longer hide a unique active labelled chat. Different task contexts remain
  ambiguous even when all but one are stale. No “pick the newest idle agent” fallback.
- Show the exact source route, current registered task/native ID/checkout and `reply_to=<message ID>`
  below an incoming routed message. Replies still resolve from the original message envelope, never
  from a new task-title search. Displayed task labels are current registry metadata, not historical
  title snapshots.

No new tool or external dependency. Legacy family/broadcast delivery and untagged messages between two
unbound chats remain compatible; use the task-ID path for work assignment. Existing route hashes
and exact message envelopes remain unchanged; an old row's native ID is not guessed or reverse-engineered.

## Verification

`python -m unittest discover tests`: **641 tests passed** (623 previous tests + 18 metadata/scope cases).
The existing ten exact-routing cases and four discovery-cue cases are included.

New cases cover v16 migration/backfill, immutable native IDs, task auto-registration by both CLI and MCP,
invalid/scoped host labels, retired duplicate versus distinct-task ambiguity, unlabelled destinations,
native-ID lookup, source metadata in inbox output, correlated replies reaching only the original chat,
and unchanged route addresses after task-label changes. Scope cases prove equal titles with different
task IDs cannot dispatch work; a same-title but unassigned chat is refused; nonexistent task IDs are
refused; matching IDs permit dispatch without any title, while delayed replies still reach the original
chat after the recipient changes tasks. A lost send receipt is recovered from its original request key
even after the destination's task changes, but a genuinely new request to that changed task is refused.
The existing task-retry regression now explicitly binds its receiving chat before task dispatch.

All tests use temporary databases and mock launches; no real AI message was sent for QA. The live DB
remains v16. This branch must not be pointed at the live DB while older servers remain active. Old rows
acquire native IDs and labels on subsequent registration; unavailable metadata is shown as unavailable,
and blank historical task labels are not filled by guessing.
