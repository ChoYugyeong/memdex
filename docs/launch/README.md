# Launch kit

Posts for the first public push of memdex. Everything links to the same three places:

- GitHub: https://github.com/ChoYugyeong/memdex
- PyPI: https://pypi.org/project/memdex-mcp/ (`uvx memdex-mcp` / `pipx install memdex-mcp`)
- MCP Registry: `io.github.ChoYugyeong/memdex`

The demo GIF every post uses: [`docs/brand/demo.gif`](../brand/demo.gif) (also the first image in the README).

| File | Platform | Tone | Length |
|---|---|---|---|
| [`hn.md`](hn.md) | Hacker News ("Show HN") | plain, technical, no marketing | 1 title + 1 author comment |
| [`reddit-claudeai.md`](reddit-claudeai.md) | r/ClaudeAI | a story about a real annoyance | ~250 words |
| [`reddit-localllama.md`](reddit-localllama.md) | r/LocalLLaMA | local-first, zero deps, what it does not do | ~200 words |
| [`geeknews.md`](geeknews.md) | 긱뉴스 | Korean, short news summary | ~120 words |
| [`velog.md`](velog.md) | velog (post 1, published) | Korean, hands-on walkthrough | ~900 words |
| [`velog-2.md`](velog-2.md) | velog (post 2, draft) | Korean, design write-up on sync | ~800 words |
| [`hn-answers.md`](hn-answers.md) | Hacker News | rehearsed replies to the ten comments it will get | 3 lines each |

## Before posting

1. **Read each community's self-promotion rules.** r/ClaudeAI and r/LocalLLaMA both expect you to be an active
   participant; a first post that is only a link can be removed. Say you are the author in the first line.
2. **Do not post the same text everywhere.** Each file is written for its platform.
3. **Best windows** (KST): HN 22:00–01:00 (US morning), Reddit 23:00–02:00, 긱뉴스 평일 오전, velog 언제든.
4. **Answer comments for the first 2–3 hours.** The first hour decides reach on HN and Reddit.
5. **Have the numbers ready**: 154,105 → 3,471 tokens on a 66-file repo (`memdex bench`), 347 tests, schema v7, CI
   on Linux/macOS/Windows × Python 3.9/3.13. Current release: **0.3.6**.
6. **Say what is weak.** Every comment thread will ask "why not just a CLAUDE.md / Serena / a vector DB?" — answer
   it directly (see the README's *Limits* section).

## Platform gotchas (learned by publishing, 2026-11-01)

- **velog resets the URL slug when you edit a published post.** Publishing with a custom slug is fine, but
  opening the post in the editor and pressing 수정하기 writes the auto-generated (Korean) slug back, so every
  link you already shared 404s. Either set the slug again in the same save (URL 설정 in the publish dialog),
  or do not touch a published post at all — put a fix in a new post.
- **velog's editor is not a contenteditable.** It renders in a `div` and takes keystrokes through a hidden
  textarea; focus it with a click on the text area, then `keyboard.paste` the markdown. Backticks are
  literal on X, so plain command lines read better there than inline code.
- **긱뉴스 blocks new accounts for a week** ("가입 후 일주일이 지나야 작성할 수 있습니다") — the submit button is
  disabled and the Cloudflare check still shows Success, so the block is easy to misread as a captcha
  failure. Check 가입일 on the profile before planning a post there.
- **X counts Korean as two characters** and treats a URL as 23 regardless of length; the composer's counter
  is the ground truth. A link in a reply is worth posting as its own reply, not squeezed into the first one.

## Checklist

Before:

- [x] `memdex doctor --serve-check` is green on this machine (the post promises it works). ✓ 2026-11-02: 13 tools, protocol 2024-11-05, schema v7.
- [x] `pipx install memdex-mcp` in a clean shell returns the version you are announcing. ✓ 2026-11-02: fresh venv, `pip install memdex-mcp==0.3.6` → `memdex 0.3.6` (pipx/uvx not installed on this machine).
- [x] The link you are about to paste resolves in a private window (slug changes, 404s). ✓ 2026-11-02: all 16 URLs in the drafts and README return 200 (curl, no cookies).
- [x] Numbers match the release: tests (`python -m unittest discover tests | tail -1`), schema version, 0.3.x. ✓ 2026-11-02: v0.3.6 tag = 347 tests (master is at 363, unreleased), schema v7, PyPI latest 0.3.6.
- [x] Author disclosed in the first line, and the post says something the README does not. ✓ 2026-11-02: added "Author here" to hn.md and reddit-localllama.md, "(직접 만든 오픈소스입니다.)" to geeknews.md; r/ClaudeAI uses the flair + "I built".

During (first 2–3 hours):

- [ ] Answer every technical question with a concrete file or command, not a promise.
- [ ] Say what is weak before someone else does (the *Limits* section is the script).
- [ ] Do not argue with "why not X" — name the different job X does and move on.

After:

- [ ] Reply to every "I'll try it" with the exact install line for their client (Claude Code, Codex, Cursor).
- [ ] Collect the first issues: they are the roadmap, not a support burden.
- [ ] If a thread gets traction, repost the demo GIF as a standalone comment — it is the strongest asset.
- [ ] Write down what the thread actually asked; that list is the next release's scope.

## After

- Reply to every "I'll try it" with the exact install line for their client (Claude Code, Codex, Cursor).
- Collect the first issues: they are the roadmap, not a support burden.
- If a thread gets traction, repost the demo GIF as a standalone comment — it is the strongest asset.
