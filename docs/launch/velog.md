# velog — Claude Code와 Codex가 한 저장소에서 싸우지 않게 하기 (memdex)

> 태그: MCP, AI에이전트, ClaudeCode, 개발도구, 오픈소스

요즘 한 저장소에서 에이전트를 두 개 이상 돌립니다. Claude Code로 주요 작업을 하고, Codex나 로컬 모델에게
두 번째 손을 맡깁니다. 그런데 두 에이전트는 서로의 존재를 모릅니다. 같은 파일을 같은 시간에 고치고, 저는
`git diff`를 보고 나서야 압니다. 세션이 바뀌면 어제 파악한 모듈 구조도 다시 읽어야 합니다.

**memdex**는 이 두 문제를 해결하는 작은 MCP 서버입니다. 의존성 0개, SQLite 파일 하나, 로컬 전용입니다.
(저장소 링크: https://github.com/ChoYugyeong/memdex)

## 1. 무엇을 하는가

| 도구 | 하는 일 |
|---|---|
| `project_brief` | 세션 시작: 마지막 핸드오프, 함정, 결정, 안 읽은 메시지, 다른 에이전트가 잡고 있는 파일 |
| `file_summary` | 파일을 읽기 전에 캐시된 2~6줄 요약 + 개요. 요약은 파일의 sha256에 묶여 있어 내용이 바뀌면 즉시 "낡음"이 됩니다 |
| `where_is` / `read_snippet` | 심볼 위치 찾고, 그 주변 256토큰만 읽기 (파일 전체 대신) |
| `claim` / `post` / `inbox` / `handoff` | 에이전트끼리 편집 구역을 알리고, 메시지를 주고받고, 작업을 넘기기 |

포인트는 두 가지입니다. **요약이 "해시"에 묶인다**는 것(경로가 아니라 내용), 그리고 **에이전트들이 같은 기억을
공유한다**는 것입니다. worktree가 달라도 같은 저장소면 노트와 메시지가 공유됩니다.

## 2. 설치

```bash
pipx install memdex-mcp     # 또는 uvx memdex-mcp
memdex --version
memdex doctor                # DB·스키마·현재 프로젝트 색인 상태 점검
memdex connect --agent claude   # 설치된 클라이언트 설정에 MCP 항목을 직접 씁니다
```

## 3. 두 에이전트 연결

Claude Code:

```bash
claude mcp add memdex --scope user -e MEMDEX_AGENT=claude -- memdex serve
```

다른 MCP 클라이언트(Codex, Cursor, Claude Desktop 등):

```json
{ "mcpServers": { "memdex": { "command": "memdex", "args": ["serve"],
  "env": { "MEMDEX_AGENT": "codex" } } } }
```

`MEMDEX_AGENT`만 다르게 주면 됩니다. 같은 DB를 보지만 각자 이름으로 노트를 남깁니다.

## 4. 실제 흐름 (터미널로 보기)

데모 GIF: https://github.com/ChoYugyeong/memdex#readme

```bash
# claude 에이전트
memdex brief --agent claude
memdex claim src/store.py --intent "add retry"

# codex 에이전트 — 같은 파일을 잡으면 경고가 옵니다
memdex claim src/store.py --intent "fix timeout"
#  ⚠ overlaps with other agents:
#    - claude@demo-app: src/store.py — add retry (119m left)

memdex post "store.py is yours; I take src/api.py" --to claude

# claude 쪽
memdex inbox
# #1 codex@demo-app → claude (just now): store.py is yours; I take src/api.py

memdex handoff "retry + backoff in store.py" --next "wire timeout to config"
# handoff #1 saved; the next session will see it in project_brief. Released 1 claim(s).

# 다음 세션(codex)
memdex brief --agent codex
# last handoff #1 (claude@demo-app, just now):
# Done: retry + backoff in store.py
```

이게 전부입니다. 잠그는 게 아니라 **보이게** 하는 것이라, git을 대체하지 않고 그 위에서 충돌을 미리 줄입니다.

## 5. 토큰은 얼마나 아끼나

```bash
memdex bench --path .
# reading every file whole: ~154,105 tokens
# outline instead:          ~3,471 tokens  (98% smaller; 51 files have symbols)
```

66파일짜리 실제 저장소 기준입니다. 다만 이 비교는 "모든 파일을 통째로 읽기"가 기준이라 후합니다. 실제
에이전트는 grep하고 부분만 읽으므로 체감 절감은 더 작습니다. 그래도 확실한 이득은 **같은 파일·같은 심볼·같은
함정을 두 번 다시 발견하지 않는다**는 점입니다.

## 6. 한계 (솔직하게)

- 개요는 정규식 수준입니다. Python은 `ast`라 정확하지만, 다른 언어는 특이한 문법에서 놓칠 수 있습니다.
- **서버는 없습니다.** 기계 간 공유는 `memdex export --sync <폴더>` / `memdex merge <폴더>`로 하고, 두 기계가
  만났을 때 노트 단위로 합칩니다(마지막 쓰기 승). git·Syncthing·공유 폴더가 그대로 전송로가 됩니다.
- 일반 `claim`은 경고이지 잠금이 아닙니다. `--exclusive`만 잠금이고, 그 잠금에는 파일 해시가 붙어 낡은 읽기로
  계산한 편집은 거부됩니다. 진실의 원천은 여전히 git입니다.
- 20파일 이하 저장소라면 손으로 쓴 `AGENTS.md`가 더 쌀 수 있습니다. 그래도 `memdex import`로 그 파일을 노트로
  넣어 두면 검색은 됩니다.
- 유지보수자 1명, 스키마 v7입니다. `PRAGMA user_version`으로 버전을 관리하고, 더 새로운 DB는 조용히 덮어쓰지
  않고 거부합니다.

## 7. 마무리

에이전트를 하나만 쓴다면 memdex는 과할 수 있습니다. **에이전트를 둘 이상 같은 체크아웃에서 돌린다면**
`claim`/`inbox`/`handoff` 세 가지가 바로 값을 합니다. 설치 후 `memdex doctor` 한 번, 그리고 두 번째 에이전트에
`MEMDEX_AGENT`만 다르게 붙여 보세요.

- GitHub: https://github.com/ChoYugyeong/memdex
- PyPI: https://pypi.org/project/memdex-mcp/
- MCP 레지스트리: `io.github.ChoYugyeong/memdex`
- 라이선스: MIT

---

## 메모 (올리기 전)

- velog는 코드 블록과 실제 출력이 잘 먹힙니다. 데모 GIF를 본문 상단에 넣으면 체류 시간이 올라갑니다.
- 제목 후보: "에이전트 둘, 체크아웃 하나 — memdex", "Claude Code와 Codex가 한 저장소에서 싸우지 않게 하기".
- 시리즈로 쪼갤 수 있습니다: ① 문제 ② 설치·연결 ③ 실제 워크플로 ④ bench와 한계.
- 마지막에 "써 보시고 안 맞으면 왜 안 맞는지 알려 주세요"를 붙이면 피드백이 붙습니다.
