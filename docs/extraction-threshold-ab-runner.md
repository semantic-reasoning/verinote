# 임계 청크 A/B 실행과 원본 재검증

`scripts/benchmark_extraction_threshold_ab.py`는 [고정 계약 v2](extraction-chunk-threshold-ab-contract.md)의 128문서·608최초 요청·최대248보정 요청을 실행한다. 기존 v1 자료와 기각 판정은 변경하지 않는다. 이 도구의 구현과 가짜 워커 테스트는 실제 성능 개선의 증거가 아니다.

## 모델 없는 명령

저장소 루트에서 실행한다.

```sh
PYTHONPATH=. python scripts/benchmark_extraction_threshold_ab.py plan
PYTHONPATH=. python scripts/benchmark_extraction_threshold_ab.py evaluate /path/to/retained-result
```

`evaluate`는 저장된 verdict를 신뢰하지 않고 파일 해시, 계획, fixture, 코드 사본, DB, checkpoint, CLI stdout/usage/timing을 읽어 품질·게시 안전성·호출 집계와 판정을 다시 계산한다. DB를 읽기 전용으로 열고 subprocess나 모델 호출을 하지 않는다. accepted/rejected는 종료 코드0, inconclusive는1이다. 실행 설정·기록 실패는2다.

## 별도 승인 후 실행

구체적인 모델·요청·시간·관측 토큰 예산과 계산 근거는 [v2 실측 예산 제안](extraction-threshold-ab-budget.md)에 있다. 제안은 아직 호출 승인이 아니다.

실모델 실행에는 깨끗한 checkout, 설치된 Claude CLI, Sonnet 요청 모델, 구체적인 요청·wall 시간·관측 입출력 토큰 예산의 별도 승인이 필요하다. 아래는 인자 형식이며 실행 승인이나 확정 예산이 아니다.

```sh
PYTHONPATH=. python scripts/benchmark_extraction_threshold_ab.py run \
  --output /path/to/new-result --model sonnet \
  --max-requests APPROVED_REQUESTS --max-seconds APPROVED_SECONDS \
  --max-tokens APPROVED_TOKENS --allow-model-calls
```

실행 요청 한도는608~856이다. 출력 폴더는 새 폴더여야 한다. CLI 플래그는 사용자의 예산 승인을 대신하지 않는다. 모든 실제 resolved model은 버전이 확인되는 동일한 Sonnet이어야 한다. alias만 보고 모델을 추정하지 않는다.

첫 호출 전에 계획·예산·전체 argv·코드 revision/tree·Python/CLI 버전·production transport·fixture/source/dictionary/gold·추출/보정 prompt를 기록하고 해시와 사본을 검증한다. B의 owner는 Store에 고정한 schema2 정책과 transport를 확인한 뒤 생성한다. 1~3청크는1worker, 4청크 이상은2worker다. Warm 준비는 각 slot에 한 번이며 실제 측정에서 같은 pool/slot/worker/generation을 재사용해야 한다.

Cold 시간은 Store 및 owner 준비 전부터 원본 기반 품질·게시 안전성 검사와 owner 종료까지다. 개별 artifact 해시는 cold 지표 밖에서 계산하되 전역 예산에 포함한다. Warm 시간은 Service 접수부터 완료까지다. 최종 보고·index 쓰기는 마지막 경과 시간 관측 뒤의 작은 기록 꼬리를 갖는다. 관측 예산을 엄격한 wall/token 상한으로 표현하지 않는다.

실패, 알 수 없는 사용량·비용·모델, 모델/worker 변경, 기록·정리 실패 또는 예산 소진은 새 호출 admission을 중단한다. 이미 승인된 호출은 종료·집계하며 대체 표본이나 자동 재준비를 만들지 않는다. 예약, adapter dispatch 승인, 완료, 전송 전 차단을 구분한다. dispatch 승인은 제공자 수신의 증거가 아니다. 시작된 요청 때문에 시간·토큰 관측 한도를 초과할 수 있으며 초과량을 숨기지 않는다.

## 증거의 범위

각 문서의 새 SQLite 저장소, 승인 이력, 고정 사전·정책, checkpoint, 추출 후보, CLI 원본과 사용량을 보존한다. 합성 사전 승인은 실행을 위한 synthetic 승인으로 기록하며, fixture manifest의 사람 검토 상태 `pending`과 구별한다. [품질 평가](extraction-threshold-fixture-review.md)는 고정된 gold·인용·coverage·gap annotation을 검사하는 유한 평가이며 임의의 자연어 의미를 판정하지 않는다.

개인 owner factory를 사용하는 테스트 결과는 항상 synthetic이며 채택할 수 없다. 원본 해시는 실수·누락·요약 불일치를 탐지하지만 서명이나 악의적 전체 위조 방지는 아니다. 모델 응답의 비용은 CLI reported 값이며 청구서 검증이 아니다. provider cache 상태는 unknown이다.

원본이 불완전하면 inconclusive로 남기고, 검증된 앞선 품질·게시 안전성 또는 완전 비교의 실패는 이후의 미완료 때문에 지우지 않는다. 원본·요약의 예산 수치, 미계상 usage 파일과 계획 밖 문서도 검사한다. 코드의 증거 형식이나 현재 fixture/계약이 달라지면 재평가가 불가능할 수 있으므로 같은 revision에서 재평가한다. 실제 판정과 운영 채택은 각각 #768과 #756에 남는다.
