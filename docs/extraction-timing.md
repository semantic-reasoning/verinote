# 승인 사전 CLI 추출 시간 (형식 1·2)

`read_extraction_timing(root, store, run_id)`를
`verinote.extraction_timing`에서 가져와 읽는다. 파일은
`runs/<run_id>/extraction-timing.json`이며 128 KiB, 16청크, 96개 span으로 제한한다.
CLI의 `stage=extract`에만 기록한다. HTTP와 사전 생성, 운영 추출 규칙은 바꾸지
않는다. 화면/API 추가 없이 #727 반복 측정 도구가 사용할 파일·reader 계약이다.

## 기준점과 포함 관계

접수 기준은 모델/옵션 확인과 대기열 여유 확인 후, **DB 실행 등록 직전**이다.
같은 프로세스의 `monotonic_ns`를 메모리 객체로 worker에 전달한다. 따라서 대기는
DB 등록과 executor 대기를 포함한다. DB 생성 시각이나 벽시계에서 역산하지 않는다.
직접 worker 호출은 접수 관측이 없으므로 queue와 total은 null이고, worker 구간만
확인한다. 등록 실패 요청은 실행 파일을 만들지 않는다.

시간은 같은 작업 기준점에 대한 **초 offset**이다. `worker_started`는 worker
진입 때, `worker_finished`는 후보 등록 또는 기존 실패 처리·slot 반납 뒤 측정한다.
마지막 선택적 측정 파일 직렬화·쓰기 시간은 제외한다.

- `queue_seconds`: 접수부터 worker 진입까지. total에 포함된다.
- `worker_seconds`: worker 진입부터 종료 관측까지.
- `total_seconds`: 접수부터 종료 관측까지. 대기와 worker를 포함한다.
- `restore`: checkpoint 확인·재검증·기존 결과 재구성·관련 기록 시간이다.
- `chunk`: 이번에 처리한 청크 전체다. 준비·호출·검증·보정·checkpoint 저장을 포함한다.
- `request`, `repair_request`: 각각 최초/보정 `complete()` 호출 시도다. 실패도 남는다.
  전송·과금 여부를 확정하는 횟수가 아니다. 기존 deadline 사전 확인을 통과한 뒤
  시작하며 timeout clone과 호출 예산/횟수 정책은 변경하지 않는다.
- `validation_with_bookkeeping`: 기존 검증과 coverage/checkpoint 기록까지 포함한다.
- `repair`: 보정 prompt 준비·호출·적용·재검증·변경 기록을 포함하는 **비가산 부모**다.
- `repair_validation_with_bookkeeping`: 보정 응답 적용·재검증·변경 기록이다.
- `publish`: 문서 전체 성공 후 후보를 등록하는 기존 트랜잭션이다.

reader의 summary는 nesting과 순서를 검사한 뒤 가산 가능한 leaf만 계산한다.
검증 시간에서 repair 부모를 빼고, repair 안에서는 호출·검증을 빼 `repair_other`를
구한다. 청크 전체에서 leaf 합을 뺀 값은 준비·기록·계측 연결 조회 등의 `other`다.
worker 전체에서 restore·청크·publish를 뺀 값도 별도다. 부모와 자식, #724의 내부
CLI 시간과 orchestration 호출 시간을 더하면 중복이다. 차감 결과가 음수·모순이면
0으로 보정하지 않고 미확인으로 둔다. 해당 단계에 들어가지 않았으면 null,
측정된 실제 0초는 0, 측정 파일 부재/손상/시계 오류는 available=false다.

## 호출·사용량과 재개

`request`와 `repair_request`는 청크 번호로 호출 폴더를 정한다. #724 reader가
run/usage/timing ID와 agent/model/phase를 검증했을 때만 `cli_call_id`를 연결한다.
나중에 기록이 없어지거나 다른 ID이면 `cli_link_available=false`가 된다. 문서 시간은
독립적으로 사용할 수 있다. 가짜 응답에 CLI 기록이 없으면 ID는 null이며 만들지 않는다.

토큰·비용은 이 파일에 넣지 않는다. 기존 `job_calls(root, 'runs', owner)`를 한 번
사용하고 동일 `cli_call_id`/호출 폴더로 참조한다. usage 부재는 0이 아니다.

재사용된 청크는 원래 offset/길이와 `reused=true`만 기록한다. 이번 worker가 수행한
복구 검증은 restore 시간에 포함하지만 과거 요청 시간·비용은 합산하지 않는다.
완전 재개는 새 호출 시도 0회이며 재사용 청크의 시간은 null이다. checkpoint 계약과
순차 처리, 청크당 1회·문서당 2회 보정, 공유 deadline, 전체 성공 후 후보 등록은 유지한다.

## 실패와 보호

span의 상태는 success/error/timeout, 전체 상태는 success/error다. 전체 error_code는
error/timeout이며 원시 예외를 넣지 않는다. 최초 요청 실패, 보정 요청 실패, 보정 후
검증 실패, 후보 저장 실패는 해당 span으로 구분한다. 측정 생성·시계·쓰기 실패는
원래 실행·예외·정리를 바꾸지 않는다. 원문·파일명·prompt·인증 정보·예외 원문을
복제하지 않는다. 원문 버전 hash와 내부 실행·문서 ID만 신원 확인에 사용한다.

reader는 저장소의 실행 stage/agent/model/status, 문서 버전/hash/청크 경계를 확인한다.
알 수 없는 형식·큰 파일·stale 실행·손상·symlink를 미확인으로 반환한다. 소스 신원
확인 전 실패하거나 worker가 강제 종료되어 완전한 측정을 못 남겼어도 시간을 추정하지
않는다. 새 측정 파일과 기존 사용자 자료를 수정하는 읽기 경로는 없다.

## 합성 구조 예시

`tests/test_extraction_timing.py`의 가짜 시계·응답으로 실제 형식과 가산 규칙을 검증한다.
아래 예시는 전체 14초 = 대기 2초 + worker 12초이고, worker는 restore 1초 + chunk
9초 + publish 1초 + 기타 1초다. chunk는 호출 5초 + 검증·기록 2초 + 기타 2초다.
ID/hash와 청크 길이는 예시이므로 실제 reader에는 해당 저장소의 신원과 경계가 필요하다.

```json
{
  "schema_version": 1,
  "identity": {"run_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "document_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "version": 1, "source_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "agent": "agy", "model": "test-model", "submitted_observed": true,
  "worker_started": 2, "worker_finished": 14, "status": "success", "error_code": null,
  "resumed_from": null,
  "chunks": [{"index": 1, "offset": 0, "length": 100, "reused": false}],
  "spans": [
    {"kind": "restore", "chunk": null, "start": 2, "end": 3, "status": "success", "cli_call_id": null},
    {"kind": "chunk", "chunk": 1, "start": 3, "end": 12, "status": "success", "cli_call_id": null},
    {"kind": "request", "chunk": 1, "start": 4, "end": 9, "status": "success", "cli_call_id": null},
    {"kind": "validation_with_bookkeeping", "chunk": 1, "start": 9, "end": 11, "status": "success", "cli_call_id": null},
    {"kind": "publish", "chunk": null, "start": 12, "end": 13, "status": "success", "cli_call_id": null}
  ]
}
```

실행: `python -m unittest discover -s tests -p test_extraction_timing.py`.
실제 모델·네트워크·API 키가 필요 없다. 이는 계측 도구 검증이며 실제 모델 성능은 미측정이다.

## 명시적 동시 기록 형식 2

기본 `ExtractionTiming(..., schema_version=1)`와 Service의 순차 실행은 유지한다.
후속 scheduler는 `ExtractionTiming(..., schema_version=2)` 또는
`create_timing(..., schema_version=2)`로 선택한다. 이 변경만으로 실제 추출이 병렬화되지는 않는다.
형식 1의 reader는 기존 비중첩·순차 규칙을 그대로 적용한다. 형식 2는 128 KiB·16청크·128 span으로 제한한다.

초기화·bind·configure·restore·reuse 후 coordinator가 청크 번호 순으로
`begin_chunk(index)`를 호출한다. 청크별 context manager는 서로 다른 worker에서 겹쳐 실행할 수 있다.
종료는 `end_chunk(status='success', index=index)`로 식별한다. 여러 활성 청크에서 index를
생략하면 계측을 무효화한다. `(kind, chunk)`가 span identity이며 동일 조합은 중복할 수 없다.
원문 경계·재사용 prefix·run identity와 검증된 CLI call ID 연결은 형식 1과 같다.
앞 청크 실패 뒤 이미 시작한 뒤 청크의 성공도 기록할 수 있지만, 이는 checkpoint prefix 정책을 바꾸지 않는다.

형식 2가 추가하는 `section(kind, index)` 구간:

| kind | 부모·순서 | 의미 |
|---|---|---|
| `worker_wait` | `request` 내부 | 최초 요청의 lease 획득 대기 |
| `repair_worker_wait` | `repair_request` 내부 | 보정 요청의 lease 획득 대기 |
| `order_wait` | `chunk` 내부, 최초 request 뒤·validation 앞 | 번호순 확정 차례 대기 |

실제 lease/scheduler는 후속 #753/#754에서 연결한다. 이번 Service에는 가짜 대기를 기록하지 않는다.
restore는 모든 새 청크 전에 끝나고 publish는 시작한 모든 청크가 끝난 후 시작해야 한다.
청크 시작 번호는 재사용 prefix 다음부터 연속한다. 서로 다른 청크는 겹치거나 역순 종료할 수 있다.
같은 청크의 단계 중첩·부모 포함·상태·보정 1회/문서 2회·중복 call ID는 검증한다.

형식 2 summary는 기존 wall time 및 청크별 필드에 다음을 더한다.

- `chunk_work_seconds`: 청크 span 길이의 합. `chunk_wall_seconds`: 구간 합집합 길이.
- `request_work_seconds`: 최초·보정 요청 길이의 합. `request_wall_seconds`: 이 요청 구간의 합집합 길이.
- 청크별 `order_wait_seconds`, `worker_wait_seconds`, `repair_worker_wait_seconds`.
- `worker_other_seconds`는 worker wall time에서 restore/chunk/publish **합집합**을 뺀다.

누적 작업시간은 wall time보다 클 수 있다. lease wait는 request에 포함되므로 다시 더하거나 차감하지 않는다.
order wait는 request 밖의 가산 leaf이며 청크 other 계산에서 차감한다. 겹치거나 맞닿는 interval을 병합하고
비어 있는 단계는 null, 실제 0초 구간은 0으로 둔다. 재사용 청크의 과거 시간·비용은 합산하지 않는다.

RLock은 시계·메타데이터 갱신에만 사용하며 context 본문, 모델 호출, CLI 기록 읽기에는 유지하지 않는다.
coordinator가 모든 worker를 drain한 뒤 outcome/finish를 호출해야 한다. finish는 snapshot을 확정한 뒤
잠금 밖에서 검증·저장하며 반복 finish는 다시 쓰지 않는다. 열린 span·시계 오류·한도 초과·쓰기 실패는
기록 unavailable일 뿐 원래 결과/예외/정리를 바꾸지 않는다. 이미 finish한 recorder를 새 실행에 재사용하지 않는다.

검증: `python -m unittest discover -s tests -p 'test_extraction_timing*.py'`.
가짜 시계와 barrier로 중첩·역순 종료·비가산 wait·보정·timeout·재개·손상·실패 비간섭을 검증한다.
