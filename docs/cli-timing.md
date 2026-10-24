# CLI 호출 시간 기록 (형식 1)

`verinote.cli_timing.read_cli_timing(root, relative_folder)`로 호출 폴더의
`cli-timing.json`을 읽는다. 예: `read_cli_timing(root, 'runs/<실행 ID>/1')`.
기존 `run.json`, `usage.json`, 측정 파일의 `cli_call_id`·agent·model이 일치하고
최종 상태와 실행 설정이 일치할 때만 `available: true`이다. 오래된 로그, 부분
저장, 다른 호출의 파일, 잘못된 형식·시간 순서, 심볼릭 링크는 미확인으로 반환한다.
파일은 8 KiB 이하이며 토큰·비용을 복제하지 않는다. 기존 사용량 집계는 기존
`usage.json`을 한 번만 사용한다. `job_calls`에도 검증된 `cli_call_id`를 제공한다.

모든 값의 단위는 **초**다. 같은 호출의 계측 시작점에 대한 단조시계 offset이며,
다른 프로세스의 offset끼리 뺄 수 없다. 기존 timeout용 시계와 별개인
`monotonic_ns`로 측정한다. 벽시계와 CLI 실행 기본값은 바꾸지 않는다.

| 필드 (`offsets_seconds` 아래) | 의미 |
| --- | --- |
| spawn_requested | `Popen` 호출 직전 |
| spawn_returned | `Popen` 반환 직후. CLI 초기화나 모델 준비 완료가 아님 |
| first_stdout | stdout 파일에서 처음 크기 증가를 관측한 때. stderr는 제외 |
| exit_observed | 종료 코드 관측 또는 timeout 정리 후 종료 관측 |
| interpret_started | 정상 실행 경로에서 파일 읽기·사용량/provider envelope 해석 시도 시작 |
| interpret_completed | 최종 JSON 해석까지 성공한 시점 |
| finished | 기존 기록 저장·오류 복구 뒤, 선택적 측정 파일 쓰기 직전의 종료 관측 |

첫 출력은 AI의 추론 시작이나 답변 시작이 아니다. 기존 0.05초 polling과 CLI·OS의
버퍼링 오차를 포함한다. 아주 짧은 프로세스는 종료 후 최종 관측에서 처음 발견될
수 있어 `first_stdout`이 `exit_observed`보다 늦을 수도 있다. 정확한 프로세스 종료
시각도 아니다. 실패한 경로의 사용량 복구 해석은 `interpret`에 포함하지 않으며
전체 시간에는 포함한다. 해석 시작 후 실패하면 완료는 null이다.

reader의 `durations_seconds`는 spawn 전후 차이, 프로세스 관측 구간, 정상 해석
구간, 전체 offset을 제공한다. 첫 출력 지연은 프로세스 구간에 중첩되므로 별도
시간처럼 더하면 안 된다. 준비·파일 I/O·기록 저장·정리 등은 전체에만 포함되는
부분도 있다. 구간 합을 전체 실행 시간으로 간주하지 않는다. 실제 0초는 0,
도달하지 않은 단계/관측된 출력 없음은 null, 측정 파일 미확인은 available=false다.

상태는 `success`, `not_started`, `error`, `timeout`이며, 오류는 정해진 코드
`preparation`, `executable_missing`, `spawn`, `exit_nonzero`, `timeout`,
`output_limit`, `interpretation`, `process`, `recording`만 쓴다. 예외 원문,
명령 전체, 경로, 요청문/hash, 환경, stdout/stderr 내용을 측정 자료에 복사하지
않는다. 모델 ID와 agent, phase, 유효 timeout·출력 한도·poll 간격만 기록한다.
CLI 버전은 별도 확인 자료가 없어 `cli_version:null`, `cli_version_source:unknown`이다.
버전을 알아내기 위해 호출마다 추가 프로그램을 띄우지 않는다.

측정 생성·시계·쓰기 실패는 원래 반환값/예외/프로세스 정리를 바꾸지 않는다.
역행한 시계는 0으로 보정하지 않고 측정 미확인으로 둔다. HTTP 경로에는 이
파일을 만들지 않는다. CLI 호출 계층의 관측일 뿐 문서 대기·청크 검증·인용 수정
집계나 사전 생성 정책·스케줄링·재개 조건을 바꾸지 않는다.

## 성공 예시

```json
{
  "schema_version": 1,
  "cli_call_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "agent": "agy",
  "model": "test-model",
  "phase": null,
  "cli_version": null,
  "cli_version_source": "unknown",
  "settings": {"timeout_seconds": 180, "max_output_bytes": 1048576, "stdout_poll_seconds": 0.05},
  "status": "success",
  "error_code": null,
  "exit_code": 0,
  "offsets_seconds": {
    "spawn_requested": 0.001, "spawn_returned": 0.003,
    "first_stdout": 0.06, "exit_observed": 0.06,
    "interpret_started": 0.061, "interpret_completed": 0.062, "finished": 0.063
  }
}
```

검증 명령: `python -m unittest discover -s tests -p test_cli_timing.py`.
가짜 프로그램만 사용하며 모델·네트워크·API 키가 필요 없다.
