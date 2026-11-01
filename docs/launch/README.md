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
| [`velog.md`](velog.md) | velog | Korean, hands-on walkthrough | ~900 words |

## Before posting

1. **Read each community's self-promotion rules.** r/ClaudeAI and r/LocalLLaMA both expect you to be an active
   participant; a first post that is only a link can be removed. Say you are the author in the first line.
2. **Do not post the same text everywhere.** Each file is written for its platform.
3. **Best windows** (KST): HN 22:00–01:00 (US morning), Reddit 23:00–02:00, 긱뉴스 평일 오전, velog 언제든.
4. **Answer comments for the first 2–3 hours.** The first hour decides reach on HN and Reddit.
5. **Have the numbers ready**: 154,105 → 3,471 tokens on a 66-file repo (`memdex bench`), 100 tests, CI on
   Linux/macOS/Windows × Python 3.9/3.13.
6. **Say what is weak.** Every comment thread will ask "why not just a CLAUDE.md / Serena / a vector DB?" — answer
   it directly (see the README's *Limits* section).

## After

- Reply to every "I'll try it" with the exact install line for their client (Claude Code, Codex, Cursor).
- Collect the first issues: they are the roadmap, not a support burden.
- If a thread gets traction, repost the demo GIF as a standalone comment — it is the strongest asset.
