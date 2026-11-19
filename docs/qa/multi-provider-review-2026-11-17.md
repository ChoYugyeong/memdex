# 독립 검토 provider 확장 검증

2026-11-17 · memdex 작업 #131

## 지원 흐름

`review(question, files, task_id, provider, model)`과 CLI `memdex review --provider …`가
같은 자료 스냅샷, 작업/원 채팅 범위 확인, 중복 예약 방지, 백그라운드 실행, 정확한 회신을 사용한다.

| 선택 | 실행 경로 | 기본 모델 |
|---|---|---|
| claude | 설치된 Claude Code 일회성 CLI | 기존 CLI 설정 |
| deepseek | 설치된 DeepSeek Harness headless, 기존 앱 계정 인증 | deepseek-flash |
| qwen | localhost:11434 Ollama API | 설치된 qwen2.5 |
| ollama | 같은 로컬 API | 명시한 설치 모델 |

DeepSeek headless 기본값은 API 키 경로여서 처음 연결 검사에서 MISSING_CREDENTIAL을 반환했다.
설치 패키지 문서에서 확인한 계정 인증 복원과 `deepseek-account` 선택을 호출별 임시 overlay로
적용했다. 사용자 프로필 파일의 수정 없이 기존 로그인으로 실행됐다. 읽기 전용/승인 never,
도구 등록·MCP 연결·저장·레포 지침 로드를 제한한다. `tools` 서비스와 실행기는 에이전트
생성에 필요해 유지하고 실제 도구 등록 행을 비활성화한다.

Qwen/Ollama는 전체 입력을 잘라 보내지 않는다. 질문·JSON envelope를 포함한 12000 UTF-8
바이트를 초과하면 제출 전 오류를 반환해 호출 에이전트가 범위를 줄이게 한다. 요청 context는
16384, 출력 한도는 2048이며 길이 제한 응답을 truncated로 구분한다. 모델 자동 다운로드는 없다.

## 확인한 결과

- DeepSeek 요청 #212 → 회신 #213, `reply_to=212`, task #131, 현재 채팅 route 수신.
- Qwen 요청 #211 → 회신 #214, `reply_to=211`, task #131, 현재 채팅 route 수신.
- 두 모델 모두 실제 첨부한 memdex/__init__.py에서 버전 0.4.2를 답했다.
- 두 완료 job에서 파일 전문이 제거되고 경로/해시가 남은 것을 DB 조회로 확인했다.
- `python -B -m unittest discover -s tests` 707개 통과.
- 검토 관련 21개 테스트에 기존 테이블 업그레이드, provider/model 중복 판정,
  다른 provider로 같은 명시적 키 재사용 거부, provider별 회신 발신자, Ollama 한도/절단,
  DeepSeek 비정상 종료·도구 호출 거부·profile overlay 검사가 포함된다.
- `git diff --check` 통과.
- provider를 생략한 Claude 호출의 기존 fingerprint/요청 ID는 보존된다.

## Shearwater 작업 #127의 요청도 수행

사용자가 앞서 요청한 Claude 검토를 함께 진행했다. 브리프와 지정 소스/문서 총 8개를 담은
요청 #207의 결과 #208을 받아, 브리프의 원 작업 채팅 route로 메시지 #209를 전송했다.
전송 시 대상 native chat ID와 task #127을 확인했고, Reason 알림 수락을 확인했다.
후속 읽기 전용 조회에서 대상 route의 받은편지함 cursor가 209로 진행돼 메시지 읽힘을 확인했다.
논문 원문 검증이나 실행 측정은 포함되지 않았다. Claude가 본문에서 파일 수를 7개라고
기술한 부분은 전달 메시지에서 8개로 정정했다.

원문은 `shearwater-127-claude-review-2026-11-17.md`에 보관했다.

## 남는 조건

기존 MCP 연결은 재연결 후 새 provider 인자를 발견한다. CLI는 즉시 사용할 수 있다.
호출 에이전트는 결과가 올 때까지 inbox와 review_status로 대기해야 한다.
DeepSeek는 현재 설치된 headless profile 형식을 사용하며 다른 버전 형식은 거부할 수 있다.
계정 로그인 만료나 사용량 제한에는 계정 조치가 필요할 수 있다.
다른 Ollama 모델은 공통 경로와 모의 테스트로 검증했으며 실제 모델 실행은 qwen2.5로 했다.
