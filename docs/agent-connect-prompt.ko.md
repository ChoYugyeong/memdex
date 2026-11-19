# 다른 AI를 memdex에 연결하기

아래 프롬프트를 연결할 AI 채팅에 붙여넣는다. 프로젝트 경로와 에이전트 이름을 바꾸고, 이미 맡은 작업이 있으면 작업 ID와 이름도 적는다.

## 복사할 프롬프트

```text
이 Windows PC의 기존 로컬 memdex에 참여해줘.
프로젝트: C:/Dev/memdex
에이전트 이름: <예: qwen 또는 gemini>
작업 ID와 이름: <이미 배정된 경우만, 아니면 생략>

1. memdex MCP가 보이면 project_brief(project=위 경로)를 호출해서 프로젝트와 내 에이전트 이름을 확인해줘.
2. MCP가 없고 셸을 쓸 수 있으면, 실제 클라이언트 이름을 확인해서 `memdex connect --client <클라이언트> --agent <에이전트> --no-claude --serve-check`로 해당 클라이언트만 설정해줘. 다른 AI의 기존 설정을 보존해줘. 재연결이 필요하면 내가 할 동작을 한 줄로 알려줘. `--client`에 없는 앱에는 임의 설정을 쓰지 말고 공식 MCP 추가 방법을 안내해줘.
3. MCP를 아직 불러올 수 없지만 셸이 있으면 `memdex brief --path "<프로젝트>" --agent <에이전트>`로 읽기 연결을 확인해줘. 기존 로컬 저장소를 사용하고 별도 DB나 API 키를 요구하지 마.
4. 작업 ID가 명시돼 있고 이 채팅에 네이티브 ID가 있을 때만, 해당 프로젝트에서 `session_routes(task_id=<ID>, label="<작업 이름>")`로 자기 채팅을 연결해줘. ID가 없거나 연결이 거절되면 결과를 알려줘. 다른 채팅의 연결을 바꾸거나 제목으로 대상을 추측하지 마.
5. 마우스를 조작하지 말고, UI에서 설정해야 하면 정확한 메뉴 경로와 붙여넣을 설정을 알려줘. 프로젝트 내용이나 브리프 전문 대신 `연결 성공/추가 조치 필요 · 에이전트 · 프로젝트 · 작업 ID(해당 시)`만 보고해줘.
```

## Qwen 데스크톱

**Settings → MCP → Add MCP → Add using JSON**에서 아래 설정을 붙여넣고 **Save and enable**을 누른다. 이미 **My MCP**에서 `memdex`가 켜져 있으면 위 프롬프트로 연결만 확인한다.

새 채팅에서는 메시지를 보내기 전에 입력창의 **MCP** 버튼을 눌러 `memdex`를 활성화한다. 설정 화면의 초록색 표시는 서버 상태이고, 채팅에서 MCP를 사용하는 설정은 별도다. [Qwen 데스크톱의 채팅별 MCP 활성화 예시](https://docs.kinescope.com/kinescope-mcp/kinescope-mcp-server/#option-4-setup-in-qwen3-desktop-application)

```json
{
  "mcpServers": {
    "memdex": {
      "command": "uvx",
      "args": ["--from", "C:/Dev/memdex", "memdex", "serve", "--path", "C:/Dev/memdex"],
      "env": { "MEMDEX_AGENT": "qwen" }
    }
  }
}
```

## Gemini CLI

처음 연결하는 경우 `memdex connect --client gemini --agent gemini --no-claude --serve-check`를 실행한다. 프로젝트 폴더에서 `gemini --allowed-mcp-server-names memdex`로 시작하고, 폴더 신뢰 확인이 뜨면 경로를 확인한 뒤 승인한다. Windows용 Gemini 데스크톱 앱과 CLI의 설정은 별개다.

## Qwen Code 작업 수신기

Ollama로 수신기를 실행하려면 `python -m memdex.assigned_wake --workspace C:/Dev --qwen-ollama`를 사용한다. 이 모드에서는 요청 본문을 로컬 `qwen2.5`에 전달하고 결과는 기존 memdex 회신 경로로 남긴다.

자동 호출을 쓸 때는 `todo` 작업의 담당자를 `qwen-code`로 지정하고, 프로젝트에서 `memdex post "검토할 내용" --to qwen-code --group --task <ID>`를 실행한다. 수신기는 Qwen Code에 요청 본문을 보내고 원래 발신자의 memdex 받은편지함에 결과를 남긴다. 검토에 필요한 내용을 요청 본문에 포함하고, 모델 제공자에게 보내도 되는 내용만 넣는다. 에이전트에게는 “Qwen 불러줘. 이 작업을 qwen-code에 배정하고 요청을 보내줘”라고 말하면 된다.
