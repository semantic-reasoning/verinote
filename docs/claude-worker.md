# Claude 프로세스 재사용

`CLI()`는 Claude 요청에 처음 쓰일 때 subprocess 하나를 시작하고 같은 실행 파일·모델의 후속 요청에서 재사용한다. 애플리케이션의 사전 생성·추출·질문이 이 경로를 사용한다. 다른 CLI 제공자와 OpenRouter는 기존 전송 방식을 유지한다. 별도 OS 서비스나 포트를 만들지 않으며 서버 종료 시 worker도 종료한다.

`CLI(persistent=False)`는 기존 단발 실행을 선택한다. `CLI.close()` 또는 `with CLI() as cli:`로 자원을 닫는다. `copy.copy`와 `limited_timeout` 복사본은 manager를 공유하므로 하나를 닫으면 모두 닫힌다. Service는 주입한 CLI도 종료 시 `close()`가 있으면 닫는다. 기존 추출 benchmark는 명시적으로 `persistent=False`를 사용해 이전의 새 프로세스 기준선을 유지한다.

## 요청 격리와 실패

고정된 shell-free 명령은 `claude -p --input-format stream-json --output-format stream-json --verbose --tools '' --strict-mcp-config --no-session-persistence --prompt-suggestions false --no-chrome --setting-sources '' --settings '{"disableAllHooks":true}'`이며 모델을 지정하면 `--model ID`를 추가한다. stdin을 열어 둔 채 JSON Lines로 요청한다. private 임시 cwd, 도구·MCP 제한, 사용자/프로젝트 설정 및 hooks 비활성화로 요청 간 부수효과를 줄인다. 기존 CLI 인증을 사용하고 `OPENROUTER_API_KEY`는 자식 환경에서 제거한다.

매 호출에서 먼저 `/clear`를 전송한다. `conversation_reset` 이벤트와 다른 session_id의 성공 result, 0인 사용량·비용 기준선을 확인한 뒤 실제 프롬프트를 보낸다. 첫 호출도 동일한 handshake를 거친다. 초기화 확인 실패 시 문서를 보내지 않고 오류를 반환한다. 명령으로 해석될 수 있는 `/`로 시작하는 애플리케이션 요청은 거부한다.

사용한 프로토콜은 로컬 Claude Code 2.1.311에서 모델 호출 없이 `/clear` 두 번과 같은 프로세스 유지로 확인했다. 이전 버전의 지원을 가정하지 않는다. 공식 [대화 초기화](https://code.claude.com/docs/en/agent-sdk/skills#reset-context-with-clear)와 [스트리밍 사용량](https://code.claude.com/docs/en/agent-sdk/cost-tracking#track-costs-in-streaming-input-mode) 계약을 따른다.

하나의 잠금으로 초기화·입력·응답·기록을 직렬화한다. timeout은 잠금 대기, 시작, 초기화, stdin 쓰기, 응답 수신을 포함한다. 대기 중인 호출의 timeout은 현재 실행 중인 호출을 죽이지 않는다. stdout/stderr는 계속 읽되 큐를 256 KiB로 제한하고, 초기화와 실제 응답을 합친 호출별 출력 한도도 적용한다. 잘못된 JSON·세션 불일치·지원하지 않는 제어 요청·EOF·시간/크기 초과는 worker를 폐기한다. 다음 명시적 호출에서 새 프로세스를 만들며 실패한 프롬프트를 자동 재전송하지 않는다.

## 로그·사용량·시간

기존 호출별 `prompt.txt`, `run.json`, `usage.json`을 유지한다. `stream.jsonl`은 초기화와 요청의 원시 stdout 스트림, `stderr.txt`는 stderr, `stdout.txt`는 실제 요청의 최종 result envelope다. 초기화 result는 모델 호출 수나 사용량에 더하지 않는다. `usage`는 해당 turn의 토큰이며, 누적 비용과 modelUsage는 확인된 초기화 기준선 이후 한 요청의 값이다. 초기화 기준선이 불명확하면 진행하지 않는다. 모델 응답 오류·JSON 본문 오류도 확인된 사용량은 남긴다. result가 없으면 토큰/비용은 미확인이다.

`cli-timing.json` 형식 2는 프로세스 재사용 여부, worker 획득·초기화·입력 전송·응답 완료를 기록한다. 정상 응답을 프로세스 종료로 기록하지 않는다. 재사용 호출의 spawn, 정상 호출의 exit_code/exit_observed는 null이다. 형식 1의 단발 실행 기록도 계속 읽을 수 있다. [시간 기록 계약](cli-timing.md)을 참고한다.

가짜 subprocess 테스트는 동일 PID 재사용·대화 격리·사용량 분리·동시 호출·실패·종료를 검증한다. 변경 후 실제 모델 품질이나 속도 개선율은 아직 측정하지 않았다. `/clear` 처리와 모델 응답 시간은 여전히 발생한다.
