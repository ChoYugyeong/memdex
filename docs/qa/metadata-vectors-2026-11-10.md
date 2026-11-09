# Metadata vector search — measurements (2026-11-10)

Model `bge-m3` (Ollama 0.40.15, F16, 1024 dimensions) on an RTX 3050 8 GB, Windows 11, Python 3.12.
Real-data runs used a read-only snapshot of the machine's shared database, not the live file.

## `memdex eval` natural-language set

11 questions (8 about files, 3 about notes) asked in other words than the files and notes use, over a
25-file synthetic checkout that includes close distractors (`net/http_client.py` next to `net/backoff.py`,
`billing/tax.py` next to `billing/invoice_pdf.py`, …). Same index for both sides.

| | Recall@5 | MRR | files Recall@5 | notes Recall@5 |
|---|---|---|---|---|
| keyword only | 64% (7/11) | 0.568 | 50% | 100% |
| with vectors (bge-m3) | 100% (11/11) | 0.768 | 100% | 100% |

The deterministic synthetic set stays at Recall@5 100% / MRR 1.000 with or without the model (it now runs
with embedding switched off, so `MEMDEX_EMBED` cannot change it).

Two fusion rules came out of reading the misses, and both only apply when a vector ranking exists, so
keyword-only results are unchanged:

- When no result holds every query word, the keyword list is the OR fallback (anything sharing "how" or
  "the"). It counts half next to the vector ranking.
- In that case the RM3 feedback pass is skipped: it assumes the top hits are relevant, and words borrowed
  from incidental matches only add more of them.

## Real questions on this machine's memdex checkout

Files only, top 5. "Rank" is the first expected file.

| question | expected | keyword | vectors |
|---|---|---|---|
| 어디서 다른 에이전트에게 메시지를 보내지? | store.py / tools.py | – (launch docs) | – (but `tests/test_messages.py`, `tests/test_inbox_addressing.py`) |
| where do we notice that a note went stale after the code changed? | store.py | – | – |
| 다른 에이전트가 편집 중인 파일을 내가 고치려 하면 어디서 막지? | cli.py / store.py | – (launch docs) | – (but `tests/test_pre_edit_hook.py` at 2) |
| how is the JSON-RPC protocol spoken over stdin | mcp.py | 4 | 1 |
| 어디서 스킬 파일의 보안 위험을 검사하지? | skills.py | – (no hit at all) | 2 |

Strict: keyword 1/5, vectors 2/5. Counting the feature's own test file as a pointer: vectors 4/5. The
misses share a cause: `store.py`, `tools.py` and `cli.py` are 1,200–2,300-line files that do many things,
and one card per file dilutes them. Symbol-level cards would be the next step.

## Storage, fidelity, speed

| | |
|---|---|
| card vector size | 1,028 bytes (int8 + float32 scale) vs 4,096 float32, 2,048 float16 |
| 2,321 cards (memdex, penlink, keychat) | 2.3 MB (float32 would be 9.5 MB) |
| int8 vs float32 | max cosine error 0.0008 on the same vectors; top-10 agreement 99% over 10 queries |
| float16 | rejected: half of float32 but ~3x slower to decode in pure Python (`struct.unpack`, 42 µs vs 14 µs per 1024-d vector) |
| search, penlink (2,071 cards) | 105 ms with vectors vs 70 ms keyword-only (median of 5, model warm) |
| embedding throughput | ~38 cards/s (keychat: 750 cards in 19.9 s) |
| model load | 3.3 s from the file cache (20 s first time from disk); the brief never waits for it |
| eligible files on this machine | code, markdown and prose text only; JSON/XML/CSV/lock/log (most of the 14.9k indexed files) get no card |

A side finding: `MEMDEX_OLLAMA_URL` defaulted to `http://localhost:11434`. On Windows, urllib tries `::1`
first, Ollama listens on IPv4 only, and every embedding call waited ~2 s for the fallback (the search above
took 2.2 s before the fix). The default is now `http://127.0.0.1:11434`.
