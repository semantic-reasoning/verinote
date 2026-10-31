# 승인 사전 추출 반복 측정

`scripts/benchmark_extraction.py`는 버전이 고정된 합성 원문과 사람이 검토한 승인 사전으로 기존 `Service.start()`를 실행한다. 운영 저장소나 설정 파일을 받지 않는다. 사전 생성은 측정하지 않으며, 매 자료·매 반복마다 새 임시 DB에 합성 사전을 설치하고 승인한 뒤 문서를 추출한다. 문서 분할, 요청문, 인용 보정, 검증, checkpoint와 후보 저장은 운영 코드와 같다. 반복 사이에 이전 결과를 재사용하지 않는다.

## 인터넷 없이 실행하기

저장소 루트에서 패키지가 설치된 Python으로 실행한다. 결과 폴더는 **새 폴더**여야 하고 부모는 이미 있어야 한다. 기존 폴더나 symlink 경로는 거부한다. macOS의 `/tmp`처럼 시스템 별칭이 포함된 경로 대신 실제 경로 또는 저장소 아래 새 경로를 사용한다.

```sh
python scripts/benchmark_extraction.py --output ./benchmark-normal --cases short long --repetitions 2 --max-requests 40 --max-seconds 120 --max-tokens 100000
python scripts/benchmark_extraction.py --output ./benchmark-repair --cases short --scenario repair --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-failure --cases short --scenario failure --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-timeout --cases short --scenario timeout --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-partial --cases long --max-requests 1
```

기본 `fake` 모드는 #726의 합성 모범 응답을 반환한다. `repair`는 별도 오류 자료와 기대 보정을 사용하며 short만 지원한다. `failure`와 `timeout`은 고정된 가짜 오류를 낸다. 정상 응답도 실제 검증과 평가를 거친다. 이 모드의 토큰(호출당 입력 100·출력 20)과 시간은 도구 검증용이며 **실제 모델의 속도·정확도·요금이 아니다**. 가짜 응답에는 실제 subprocess가 없으므로 CLI 호출 시간은 미확인이고, 문서 처리 시간과 호출 시도 수는 정상 경로에서 측정한다.

## 실제 CLI를 선택하는 방법

실제 실행은 별도 승인 후 `--mode cli --allow-model-calls --agent <provider> --model <explicit-model-id>`를 함께 지정한다. 지원 provider는 agy, claude, copilot, cursor, codex다. HTTP/OpenRouter 실험과 effort 변경은 이 도구의 범위 밖이다. 예를 들어 앞의 정상 명령에 이 네 옵션을 추가할 수 있다. 도구는 실제 모델을 자동 호출하거나 실패 시 다른 모델로 바꾸지 않는다. 이 구현을 검증하는 동안 실제 모델을 호출하지 않았다.

CLI 인증은 기존 CLI 방식으로 처리한다. Verinote 사용자 설정·원본 문서·사용자 DB를 읽지 않는다. CLI 자체가 관리하는 인증·캐시·계정 사용량은 그 CLI의 동작을 따른다. 결과에는 실행 모드, 요청 모델, timeout·출력 크기 등 허용한 설정만 기록한다. CLI 버전은 자동 탐색하지 않아 기본은 `null / unknown_not_probed`다. 별도 확인한 버전은 `--cli-version 2.1.311`처럼 전달할 수 있다(영문·숫자·`.`·`+`·`_`·`-`, 1~80자). 이 값은 `operator_supplied_unverified`로 기록하며 도구가 설치 버전을 검증했다는 뜻이 아니다. 알 수 없는 제공자 기본 설정이나 실제 해석된 모델도 추정하지 않는다.

## 프로세스 수명 비교

`--process-mode`의 기본값은 `oneshot`이다. 기존 fake 및 실제 CLI 단발 기준선은 유지한다. `document`와 `batch`는 `--mode cli --agent claude`에서만 지원하며 잘못된 조합은 결과 폴더 생성 전에 거부한다. 승인된 실모델 실험에서 다음 명령의 `MODEL_ID`와 버전은 실제 확인한 값으로 바꾼다.

```sh
python scripts/benchmark_extraction.py --output ./benchmark-oneshot --mode cli --allow-model-calls --agent claude --model MODEL_ID --process-mode oneshot --cases short long --repetitions 2 --max-requests 20 --max-seconds 600 --max-tokens 100000 --call-timeout 90
python scripts/benchmark_extraction.py --output ./benchmark-document --mode cli --allow-model-calls --agent claude --model MODEL_ID --process-mode document --cases short long --repetitions 2 --max-requests 20 --max-seconds 600 --max-tokens 100000 --call-timeout 90
python scripts/benchmark_extraction.py --output ./benchmark-batch --mode cli --allow-model-calls --agent claude --model MODEL_ID --process-mode batch --cases short long --repetitions 2 --max-requests 20 --max-seconds 600 --max-tokens 100000 --call-timeout 90
```

각 명령은 별도 배치와 예산이다. 이 예시는 호출 승인이나 권장 실험 표본 수가 아니다. 모델 호출 없이 세 모드의 수명을 검사하려면 `python -m unittest discover -s tests -p test_benchmark_persistent.py`를 실행한다.

| 모드 | 프로세스와 owner 수명 | 문서 경계 |
|---|---|---|
| oneshot | 문서 owner, 요청마다 새 subprocess | 매번 새 DB·사전·checkpoint |
| document | 문서 owner, 같은 문서의 청크·보정에서 worker 재사용 | 문서 종료 시 close, 다음 문서는 새 worker |
| batch | 배치 owner, 문서 사이에도 worker 재사용 | DB·사전·checkpoint만 새로 생성, worker는 배치 종료 시 close |

매 Claude persistent 요청은 기존 `/clear` 확인을 거친다. `BudgetCaller`의 owner만 실제 caller를 닫고 timeout 복사본은 비소유 view다. document/oneshot Service는 owner를 닫지만 batch의 문서 Service는 비소유 wrapper를 닫으므로 다음 문서의 worker를 종료하지 않는다. 생성 중 오류, 정상 종료, timeout, 중단, 호출 예산 소진, 결과 저장 실패에도 owner를 회수한다. 시작한 호출의 usage 수집과 Service drain 후 임시 로그를 삭제한다. cleanup 오류는 원시 예외 대신 `cleanup_errors`에 집계하고 `stopped`로 보고한다. 먼저 발생한 stop reason은 보존하며 정리 오류만 있으면 `cleanup_error`다.

`conditions.process_mode`, `owner_scope`, `provider_cache_state: unknown`, `warmup: none`를 기록한다. 숨은 warm-up 호출은 추가하지 않는다. 최초 측정 요청도 예산에 포함된다. 프로세스 재사용은 provider prompt cache 적중이나 OS cache 초기화를 의미하지 않는다.

각 attempt는 `case_id`, `repetition`, `owner_id`로 문서와 수명 소유자를 연결한다. CLI timing v2의 실제 `process_reused`, `spawn_returned`와 이전 동일 owner의 관측으로 `process_observation`을 분류한다.

- `started`: 동일 owner에서 처음 관측한 persistent 프로세스 시작(cold).
- `reused`: 실제 동일 worker 재사용 관측(warm). 문서 순서나 옵션 이름으로 추정하지 않는다.
- `restarted`: 같은 owner에서 이전 프로세스 관측 이후 새 시작(cold). 재시작의 원인은 이 분류만으로 단정하지 않는다.
- `unknown`: 검증된 v2 시작/재사용 근거가 없음. fake·v1 단발 기록·누락/신원 불일치도 포함한다. v1 단발 시작 시간은 기존 offsets/durations로 확인할 수 있지만 persistent 재시작으로 분류하지 않는다.

`attempts[].cli_timing`은 schema version, process_reused, offsets와 durations를 보존한다. 재사용 호출의 spawn/정상 종료 exit 값은 null이며 0으로 바꾸지 않는다. 실제 모델·설정을 확인하지 못한 값도 임의로 채우지 않는다.

문서별 `seconds`는 임시 저장소 생성 직전부터 Service drain/정리와 평가 수집까지이며 임시 폴더 삭제 시간은 제외한다. document/oneshot는 해당 transport close를 포함하지만 batch는 문서마다 닫지 않는다(`conditions.row_seconds_includes_transport_close`). batch 최종 close는 `batch_teardown_seconds`로 별도 기록하고 전체 `budget.elapsed_seconds`에 포함한다. 문서·batch 간 수명 차이를 숨겨 문서별 시간만으로 속도 향상을 판단하지 않는다. `document_timing`은 기존 접수–worker 종료 정의로 이 outer teardown을 포함하지 않는다.

## 한도의 의미

- `--repetitions`(기본 2)는 자료별 반복 횟수다. `--max-requests`(40), `--max-seconds`(120), `--max-tokens`(100000)는 전체 실행이 공유한다. `--call-timeout`(30초)은 개별 호출의 상한이며 청크 및 전체 잔여 시간으로 더 줄어든다.
- 최초 요청·인용 보정·실패한 시도를 전송 전에 하나씩 예약한다. `limited_timeout()`으로 만든 객체도 동일 예산을 사용한다. 예약된 attempt는 실제 전송이나 청구가 입증됐다는 뜻은 아니다. 실패나 실행 파일 누락에도 환불하거나 숨은 재시도를 하지 않는다.
- 전체 시간은 사전 조건 확인과 결과 폴더 생성 후 예산을 만들 때부터 잰다. 문서 설치·추출·검증·기록·정리가 포함된다. 만료 후 새 호출을 허용하지 않으며 기존 CLI timeout과 프로세스 트리 정리를 기다린다. OS 스케줄링·검증·저장·정리 때문에 최종 경과 시간은 설정값을 조금 넘을 수 있다. 엄밀한 프로세스 전체 종료 시각이나 원격 요금 상한을 보장하지 않는다.
- 토큰은 응답 후 기존 usage 기록의 입력+출력을 합친다. 입력에 포함된 캐시와 출력에 포함된 추론은 다시 더하지 않는다. 마지막 호출이 한도를 넘겨도 그 사용량을 기록하고 다음 호출을 막는다. 사후 정보이므로 토큰 사용량의 강제 상한은 항상 `token_hard_cap_guaranteed: false`다.
- 누락·불완전·일치하지 않는 usage는 기본 `--missing-usage stop`으로 중단한다. 명시적인 `continue`는 요청 수 상한을 유지하며 반복하지만 `token_usage_verified: false`로 표시한다. 부분적으로 알려진 토큰도 `observed_tokens_lower_bound`에 남긴다. 알 수 없는 토큰·비용을 0으로 채우지 않는다. 비용은 reported/estimated를 분리하고, 크레딧은 단위의 SHA256과 출처별로 구분한다.
- 인터럽트와 저장 실패는 새 호출 허용을 먼저 취소한다. 이미 허용된 호출은 남은 timeout 안에 정리하고 worker를 기다린 뒤 임시 DB·로그를 삭제한다. 인터럽트가 원격 요청을 취소하거나 요금을 되돌렸다는 뜻은 아니다.

## report.json 읽기

새 결과 폴더에는 버전 3의 `report.json`만 남는다. 기존 버전 1·2 결과는 수정하지 않는다. v1에는 프로세스 수명 정보가 없으므로 해당 필드를 읽는 소비자는 `schema_version`을 확인하고 부재를 unknown으로 해석해야 한다. 과거 결과를 cold/warm/persistent로 소급 분류하지 않는다. v2는 기존 시간·품질·사용량 필드를 보존하고 수명 및 정리 정보를 추가한다. `conditions`에 자료/사전/추출·보정 요청문 해시, 평가·계약 버전, 사람 검토 상태, 요청 모델, 안전한 설정, 분할 크기, 반복 수와 새 저장소 조건을 고정한다. `attempts`는 시도 순서·청크·보정 여부·성공/실패·토큰·비용·검증된 CLI 호출 ID와 #724 시간 기록(미확인 시 null), 측정 wrapper 경과 시간을 담는다. #725의 문서별 집계는 `repetitions[].document_timing`에 연결한다. 시간과 usage가 서로 독립적으로 미확인일 수 있다.

`repetitions`의 각 행은 자료 하나의 반복 실행이다. `quality`는 현재 run에 속한 검증된 checkpoint prefix를 #726 평가기로 점수화한다. 실패로 1/2청크만 검증됐다면 `complete: false`이고, recall 분모는 **전체 문서 정답**이다. 유효 prefix가 없는 경우에도 빈 결과의 누락을 평가한다. 실행 성공과 정답 평가의 `passed`는 별개다. 상위 `quality.passed`는 전체 청크 완료와 평가 통과를 모두 요구한다. 실제 원문·인용·잘못된 triple 문자열은 내보내지 않으며, 추가 관계는 해시와 위치로 식별한다. 합성 원문은 버전 고정 자료에서 별도로 대조한다.

`summary`는 성공/실패 시간 분포와 자료별 분포, 실제 실행 표본 수, 보정·실패 시도, 시간 기록 coverage, 비용의 알려진 호출 수를 표시한다. 분포는 최소·최대·평균·중앙값·모집단 표준편차다. 표본 0개는 값이 null, 표본 1개는 표준편차 0이다. 서로 다른 자료를 합친 분포만으로 모델을 비교하지 않는다.

정상·실패·한도 중단 모두 보고서를 기록한다. `status: completed`는 계획된 반복이 끝났다는 뜻이며, 개별 실행은 실패할 수 있다. `stopped`이면 `stop_reason`과 실제 attempt 수를 확인한다. 성공 실행만 종료 코드 0, 실패·중단은 1, 설정·저장 실패는 2다. 매 호출과 반복 뒤 원자적으로 갱신하므로 중간 파일의 `running`은 부분 결과다. 디스크 오류가 나면 마지막 정상 파일을 보존하고 정적 오류 메시지를 반환한다. 저장장치가 처음부터 쓰기 불가능하면 보고서를 새로 만들 수 없다.

가짜 결과 예제는 [benchmarks/extraction/examples](../benchmarks/extraction/examples)의 `benchmark-fake-*.json`에 있다. 동일 조건은 위 명령 또는 보고서의 `conditions`를 사용해 재실행한다. 시간·호출 ID는 재실행마다 달라진다. API 사용자는 `verinote.extraction_benchmark.measure(output, cases=..., limits=Limits(...))`를 호출할 수 있다. `caller_factory`와 `clock`은 오프라인 검증용 주입 지점이다. factory는 문서별로 새 caller를 반환해야 하며, benchmark가 그 caller를 닫는다. `batch`와 factory의 조합은 첫 문서에 종속된 응답 객체를 다른 문서에서 재사용할 위험이 있어 거부한다. batch 통합 테스트는 실제 CLI 생성자를 유지하고 실행 파일과 명령만 가짜 subprocess로 바꾼다. 실제 호출을 주입하면서 fake로 표시해서는 안 된다.

실제 가짜 응답 반복 실행의 결과·시간 편차·추가 측정 계획은 [#728 결과 보고서](extraction-performance-results.md)에 정리했다.


## 동시 admission 장부 (보고서 v3)

실제 Service는 계속 순차이며 이 절은 후속 독립 worker/scheduler의 공유 예산 계약이다.
`Budget`와 timeout 복사본은 하나의 RLock·deadline·예약 상한·관측 토큰 장부를 공유한다.
모델 호출이나 context 본문 동안 이 잠금을 유지하지 않으며 두 adapter의 실제 중첩을 fake barrier로 검증한다.

상태 전이는 `reserved → authorized → finished` 또는 `reserved → blocked`다.
예약은 슬롯을 소비하고 환불하지 않는다. identity는 owner_id/case_id/repetition의 scalar 복사본만 받으며
상태·토큰·attempt 번호를 덮어쓸 수 없다. `purpose=measurement|warmup`과
`request_kind=initial|repair|warmup`을 기록한다. warm-up의 보정은 purpose=warmup/request_kind=repair다.
이번 변경은 warm-up을 실행하지 않고 미래 harness가 같은 예산으로 예약할 수 있게 한다.

새 예약 용량 소진은 전역 중단과 별개다. N개 정상 예약 뒤 N+1 예약이 거절되어도 이미 확보한 슬롯은
허용될 수 있다. explicit/time/token/usage/report 중단은 아직 허용하지 않은 예약을 막는다.
terminal 중단 사유가 있으면 진단용 request_limit보다 우선하며 첫 terminal 사유를 유지한다.
`authorize_dispatch()`는 stop/time/token과 예약 소유권을 원자적으로 검사한다. 이 시점은 **adapter 진입 허용**이지
실제 네트워크 전송·처리·과금 증거가 아니다. 허용 전에 중단되면 호출하지 않는다. 허용 뒤 발생한 중단은
이미 허용한 호출을 소급 취소하지 않으며 최종 응답/실패/사용량을 수집한다.

`finish()`는 장부가 발행한 authorized attempt만 한 번 확정한다. foreign/duplicate 완료는 오류이며 재집계하지 않는다.
`blocked`는 실제 adapter를 호출하지 않았으므로 unknown_usage_calls에 넣지 않지만 비용/토큰을 임의의 0으로 채우지도 않는다.
허용한 호출의 사용량 누락은 기존 missing_usage 정책을 적용한다. 토큰은 사후 관측이며 여러 in-flight 호출이
종료하면서 한도를 초과할 수 있다. hard cap 보장이 아니다.

v3의 `budget.requests_started`는 이전과 같이 **예약 수**다. `dispatch_authorized`, `blocked_before_dispatch`,
`finished`, `pending`을 별도 집계한다. `token_usage_verified`는 pending=0이고 허용된 호출의 사용량이 모두 확인됐을 때만 true다.
각 attempt에 admission_state/dispatch_authorized/blocked_reason/purpose/request_kind를 추가한다.
기존 v1/v2에 이 필드가 없으면 unknown으로 읽고 예약 수를 전송 수로 소급 해석하지 않는다.

attempt 갱신·lifecycle 갱신·notify snapshot과 저장을 같은 예산 잠금으로 직렬화한다.
ReportWriter도 자체 잠금으로 같은 임시 파일의 쓰기 경쟁을 막는다. snapshot 생성과 저장을 분리해 오래된 상태가
나중에 덮어쓰지 않으며 저장 실패는 신규 admission을 막되 이미 허용된 호출의 완료·사용량 반영을 막지 않는다.
callback 실패가 원래 호출 예외를 덮지 않는다. 정상 파일은 atomic replace 전 오류에서 보존한다.
현재 process_observation의 lifecycle은 단일 manager 단위다. #753에서 여러 독립 worker를 연결할 때에는 worker별
실제 identity에 따라 시작/재시작 관측을 분리해야 하며 한 bool로 두 worker의 시작을 재시작이라고 추정하면 안 된다.

검증: `python -m unittest discover -s tests -p 'test_extraction_budget*.py'` 및 기존 benchmark 테스트.
실제 모델·추가 비용 없이 cap 경쟁, 중단 전후, 역순 완료·중복 방지, in-flight 사용량, callback/저장 경합을 검증한다.
