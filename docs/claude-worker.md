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

## 추출 전용 bounded owner (#753)

기본 CLI의 single manager와 질의·사전 생성·순차 추출 선택은 유지한다. 다음 인터페이스는
후속 scheduler가 명시적으로 사용하는 자원 기반이며 그 자체로 청크를 병렬 제출하지 않는다.

```python
from verinote.extraction_transport import ExtractionTransport

with ExtractionTransport.from_cli(cli, model, capacity=2) as owner:
    view = owner.view()  # 자원을 소유하지 않는다.
    limited = view.limited_timeout("claude", 30)
    result = limited.complete("claude", model, prompt, folder)
```

Owner 생성 시 실행 파일의 절대 경로, 모델, capacity(정수 1 또는 2), 설정 timeout,
출력 한도와 Claude 단가 snapshot을 고정한다. 이후 PATH 조회나 원래 CLI 설정 변경이 이 owner에 전파되지 않는다.
다른 제공자·모델·ontology phase는 거부한다. timeout=None의 기존 CLI 계약도 보존한다:
직접 호출 기본은 180초이고 처음 limited_timeout은 caller deadline을 사용할 수 있다.
명시적 timeout 또는 이미 줄인 view의 timeout은 이후 view에서 늘릴 수 없다.

`copy.copy(owner)`, `view()`, `limited_timeout()`는 같은 pool을 참조하는 비소유 view다.
view.close는 owner를 종료하지 않으며 owner.close만 실제 자원을 닫는다. 독립 owner는 명시적으로 닫아야 한다.
기존 CLI의 copy.close가 공유 기본 manager를 닫는 계약은 변경하지 않는다.

Pool은 독립 Manager를 최대 2개만 보유하고 subprocess는 첫 사용 때 생성한다. 세 번째 lease는 빈 slot을 기다린다.
슬롯 선택·반납만 condition으로 보호하며 spawn/exchange/close 동안 pool 잠금을 유지하지 않는다.
CLI 호출 시작에서 계산한 **같은 absolute deadline**을 pool 대기와 Manager.acquire에 전달한다.
lease 획득 후 timeout을 새로 시작하지 않는다. CLI timing의 worker_wait 구간에는 이 대기가 포함되지만
호출 준비 시간도 포함하므로 순수 lease 대기만 측정한 값으로 해석하지 않는다.

기존 `/clear` 확인, 세션 격리, 도구 제한, 출력 한도, 호출별 폴더/로그/usage/call ID를 그대로 사용한다.
한 worker 오류는 해당 Manager의 worker만 retire한다. 대기 호출 timeout은 활성 worker를 종료하지 않는다.
자동 재전송은 없으며 실패 slot의 다음 명시적 호출에서만 새 process를 시작할 수 있다.

정상 종료는 caller가 모든 작업을 drain한 뒤 owner를 닫는다. 강제 close는 pool을 닫아 신규 lease를 막고
waiter를 깨운 다음 **모든 Manager의 closed event를 먼저 설정**하고 각 Manager를 정리한다.
Exchange는 자신을 생성한 Manager의 종료 event를 관찰하므로 기존 atexit의 개별 Manager.close도
해당 활성 호출을 중단한다. 직접 닫힌 Manager가 하나라도 있는 pool은 새 호출을 거부하며
slot을 자동 재생성하지 않는다. 다른 slot에서 이미 실행 중인 호출은 개별 종료로 취소하지 않는다.
하나의 close가 실패해도 나머지 정리를 시도하며 오류는 caller에 전달한다. 반복 close는 안전하다.
정리에 실패한 worker 참조는 유지하고 stopped로 표시한다. 다음 acquire는 이를 재사용하지 않으며
retire 재시도가 성공하기 전에는 새 process를 만들지 않는다. 다시 close하여 남은 자원을 정리할 수 있다.

Service의 `extraction_transport(model, capacity=2)`는 Service당 전용 owner 하나만 보유하는 명시적 factory다.
같은 frozen 조건은 같은 owner의 view를 반환한다. 조건 변경은 pool condition 안에서 idle 확인과
admission 폐쇄를 원자적으로 수행한 뒤 기존 owner를 닫고 교체한다. 활성 lease나 waiter가 있으면 거부한다.
교체된 owner의 stale view는 새 호출을 시작하지 못한다. Service 종료 이후 새 owner 요청은 거부한다.
Service.close는 기존 executor를 drain한 뒤 전용 owner와 기본 CLI 모두의 정리를 시도한다.
기본 start/job_cli 경로에는 아직 이 factory를 자동 연결하지 않는다.

상한은 **Service당 전용 최대 2 + 기존 공유 최대 1 = 최대 3 subprocess**다. 여러 Service나 외부에서
별도로 만든 owner를 합친 전역 OS 상한이 아니다. 외부 benchmark owner의 수명은 benchmark가 책임진다.

### Worker identity와 기록 순서

전용 호출은 run/usage에 worker_scope=extraction_pool과 동일한 worker_identity를 남긴다.
identity는 opaque pool_id/worker_id(32 hex), slot_id(1~2), 해당 slot의 양의 generation이다.
새 Worker가 lease에 제공될 때 generation을 확정하고 호출별 복사본을 보존한다. PID를 영속 identity로 쓰지 않는다.
같은 slot의 다음 호출이 먼저 보고되거나 새 generation 보고 뒤 이전 호출이 도착해도 생성 세대는 바뀌지 않는다.
Benchmark는 run/usage/timing의 call ID·agent/model이 검증된 경우만 허용 필드를 export한다.
process_reused=false와 실제 spawn 관측이 있고 generation=1이면 started, 이후 generation이면 restarted다.
process_reused=true이면 reused다. 누락·불일치는 unknown이며 callback 도착 순서로 재시작을 추정하지 않는다.

문서 timing의 worker_wait/repair_worker_wait 연결과 최대 2청크 scheduler는 #754의 범위다.
검증은 `python -m unittest discover -s tests -p test_extraction_transport.py`로 가짜 subprocess만 호출한다.
이는 자원·수명 검증이며 실제 모델 지연 개선의 증거가 아니다.
