# Sonnet 승인 사전 추출: 보완 기준선과 후속 판정 계약

2026-10-31의 별도 승인으로 누락 표본만 보완했다. **3모드 × 5자료 × 3회 = 45개 성공·정답 통과 표본**을 확보했다. #750의 탐색 기준선과 후보 선택은 완료하며, **#752·#753·#754의 최대 2청크 동시 처리를 다음 구현·검증 후보로 선택한다.** #751 응답 간결화는 이번 분기에서 선택하지 않는다. 성능 개선을 채택했다는 의미가 아니며 실제 개선·운영 적용 여부는 #755·#756에서 판정한다.

## 실행과 보존

[최초 부분 기록](extraction-sonnet-baseline.md)과 [원본](../benchmarks/extraction/results/750/)은 그대로 보존한다. 최초 44요청의 관측 토큰은 302,172이며 승인 관측 한도를 마지막 응답에서 2,172 초과한 사실은 변경되지 않는다.

추가 승인은 Sonnet, 최대 30요청·900초·200,000관측 토큰이었다. 보완 실행은 **21요청·203.358초·154,387토큰·CLI 보고 USD 0.5766367**로 끝났다. 14개 문서가 모두 성공했고 보정·실패 요청·cleanup 오류는 0, 사용량 확인은 21/21이다. 전체 잔여 예산을 명령 간 공유했고 timeout은 90초, 사용량 누락 시 중단했다. 이 승인을 후속 A/B에 전용하지 않는다.

두 실행 합계는 **65요청·456,559토큰·CLI 보고 USD 1.666694**다. 원래 부분 추출 2개의 비용도 포함한다. CLI 보고 비용은 실제 청구서 확인값이 아니다. 관측 토큰은 응답 후 집계되므로 admission 한도이며 엄격한 토큰·wall-clock hard cap을 주장하지 않는다.

- 보완 revision: `319ae0aa3f45415d3652f1c2aaf69408f1a5745d`. 최초 `11c2d79`와 verinote/scripts/tests/pyproject.toml의 diff가 없음을 실제 확인했다.
- Claude Code `2.1.311`, 요청 모델 `sonnet`. 실제 resolved model은 export되지 않아 **unknown_not_exported**다. 동일 실제 모델 버전을 보증하지 않는다.
- 각 문서는 새 DB·사전·checkpoint, 자동 재시도·별도 warm-up 없음. 자료·평가 계약은 동일한 버전 1이다.
- [ledger](../benchmarks/extraction/results/750-supplement/ledger.json)는 실제 argv·실행 순서·전역 잔액·논리 반복 매핑을 기록한다. [환경](../benchmarks/extraction/results/750-supplement/environment.json), [파일 해시](../benchmarks/extraction/results/750-supplement/index.json), [통합 요약](../benchmarks/extraction/results/750-supplement/combined-summary.json)을 함께 보존한다.

| 보완 명령 | 자료 | local 반복 | logical 반복 | 요청 |
|---|---|---:|---:|---:|
| oneshot-boundary-2 | boundary | 1 | 2 | 2 |
| oneshot-3 | short, long, dense, none, boundary | 1 | 3 | 7 |
| document-3 | long, dense, none, boundary | 1 | 3 | 6 |
| batch-3 | long, dense, none, boundary | 1 | 3 | 6 |

기존 성공 표본은 다시 호출하지 않았다. 기존 document/batch의 long 3회는 부분 추출로 별도 보존하고, 새 저장소에서 문서 전체를 다시 측정했다. 보완 batch의 첫 long은 **새 owner의 cold 시작**이며 최초 batch의 warm 연속 실행이 아니다. 통합 표본은 logical key를 채운 기록이지 동일 조건의 연속 3반복 실험이 아니다. summary의 series/report/local_repetition/process_observations로 원본을 추적할 수 있다.

## 통합 결과와 변동

아래는 자료별 3개 성공 표본의 **Service 접수–완료 중앙값 [최솟값, 최댓값]**, 단위 초다. 초기/보완 series가 섞인 기술 통계이며 모드 간 인과적 개선율을 계산하지 않는다. 모집단 표준편차와 개별 값은 통합 요약에 있다. 자료 준비·평가·batch 최종 종료가 포함된 benchmark 시간과 구분한다.

| 자료 | oneshot | document | batch |
|---|---:|---:|---:|
| short | 17.590 [17.374, 19.770] | 5.146 [4.793, 5.595] | 4.521 [3.692, 4.784] |
| long | 33.007 [32.753, 33.546] | 8.270 [6.332, 9.556] | 6.588 [5.964, 7.291] |
| dense | 17.677 [17.611, 17.789] | 5.349 [5.290, 5.909] | 4.514 [4.283, 4.669] |
| none | 16.204 [15.597, 16.219] | 3.635 [3.258, 3.799] | 2.631 [2.515, 2.682] |
| boundary | 37.758 [35.961, 38.805] | 7.520 [7.026, 7.923] | 7.123 [6.235, 7.363] |

batch long의 최초 warm 표본은 5.964·6.588초, 보완 cold 표본은 7.291초다. batch boundary는 세 표본 모두 재사용 호출이다. batch 최초 short 1회와 보완 long 3회의 첫 호출만 started이며 나머지 성공 호출은 reused다. PID 재사용은 provider cache 적중을 뜻하지 않는다. oneshot은 프로세스 관측 분류 unknown, document는 매 문서 첫 호출 started이고 다중 청크의 다음 호출만 reused다.

각 모드의 성공 15문서는 TP 36·FN 0·FP 0·grounded TP 36이다. 인용·금지 관계 평가 회귀는 없으며 none의 precision/recall은 null 그대로 유지한다. 최초 부분 long 2개는 complete=false, recall=0.5로 별도 분류하며 성공 표본으로 계산하지 않는다. 전체 65요청에서 보정 0, 사용량 확인 65/65다. 성공만의 품질과 예산 중단을 포함한 실행 결과를 혼동하지 않는다.

성공 문서의 최초/보정 요청 구간 합 ÷ Service 접수–완료 시간 합은 oneshot 99.956%, document 99.821%, batch 99.748%다. complete() 구간은 CLI 호출·응답·기록을 포함하며 모델 추론 전용 시간이 아니다. 성공 15문서의 큐 대기 합은 각각 0.0173/0.0165/0.0187초, 검증·기록 합은 0.0414/0.0418/0.0504초, 후보 등록 합은 0.0293/0.0301/0.0414초다. 원본에는 문서별 restore/worker/청크 및 호출별 spawn/reset/response/exit도 있다. 부모·자식 시간을 중복 합산하지 않는다. 독립 worker 획득 대기는 아직 별도 지표가 없으며 #752에서 추가한다.

운영과 가까운 batch의 long/boundary도 두 청크를 순차 호출하고 요청 구간이 대부분이므로 대기를 겹치는 최대 2청크 후보를 선택한다. OS spawn만 줄이거나 로컬 검증·DB를 최적화하는 방안은 우선하지 않는다. 응답 간결화의 품질·지연 효과도 아직 관측하지 않았다.

고정 실행 순서, 시점·provider cache·실제 모델 미확인, 단발/persistent 명령 설정 차이, 작은 합성 자료와 중단 후 보완이 한계다. 논리적 표본 수를 채웠다고 통제 실험이 되지는 않는다. 단일 문서 순차 제출 시험이므로 다중 작업 큐 병목 부재, 꼬리 지연, 일반적인 품질·실패율을 주장하지 않는다.

## 재현과 검증

새 측정은 별도 승인이 필요하다. ledger의 argv에서 Python/checkout/output 경로만 새 위치로 바꾸고 기록된 revision·환경·자료를 사용한다. 기존 결과를 덮어쓰지 않는다. 네 명령의 예산을 독립 초기화하지 않고 실제 요청·토큰·경과를 차감한다. unknown usage/cleanup 오류/손상/예산 소진이면 후속 명령을 중단한다.

통합 요약은 원본 repetition에서 status=success만 선택해 ledger의 logical repetition으로 연결한다. `(mode, case_id, logical_repetition)`의 중복이 없어야 하며 정확히 45키여야 한다. 원래 status=error 2개는 partial_or_failed_samples에 보존한다. 각 repetition의 attempt_start(1-based)와 attempt_count 구간에서 비용·토큰을 합산하며 전체 총액은 실패 요청까지 포함한 원본 report budget/summary를 합산한다. Service 통계는 document_timing.summary.total_seconds를 사용한다. index에는 index 자체를 제외한 모든 보완 파일의 크기·SHA256이 있다.

## #755에 적용할 사전 A/B 판정 계약 v1

이 기준은 후보 구현·실측 **전에 고정**한다. 기준선의 batch long 범위 5.964–7.291초, boundary 6.235–7.363초처럼 작은 표본에서도 변동이 있으므로 단순 평균 비교 대신 순서 교차와 paired ratio를 사용한다. 20%를 최소 의미 있는 지연 개선, 10%를 비용·회귀 허용 상한으로 정한다. 표본 수는 primary 자료별 10쌍의 제한된 검증이며 검정력이 입증된 설계가 아니다. 실제 실험의 호출·시간·토큰 예산은 아래 warm-up·실패·보정까지 계산하여 별도 승인을 받아야 한다. 실행 예산이 부족하다고 표본 수나 임계값을 줄이지 않는다.

**동일 조건:** A=동시성 1, B=최대 2. 같은 후보 revision, 자료·사전·prompt 해시, CLI 버전, timeout 90초, `/clear` 계약을 사용하고 한 변수만 바꾼다. 매 측정 문서는 새 DB·사전·checkpoint를 사용한다. 각 호출의 실제 resolved model을 남겨야 하며 확인 불가 또는 불일치는 비교 불가다. 요청 alias만으로 동일 모델을 가정하지 않는다. provider cache 관측 가능 여부와 실제 프로세스 상태도 기록한다.

**primary warm:** long → boundary 순서, 각각 10쌍이다. 자료별 독립 A owner(1worker), B owner(2worker)를 만들고 해당 자료의 모든 쌍 동안 유지한다. 각 worker에 동일 short 합성 문서를 정확히 1회 실행해 준비한다(A 1회, B 2회). warm-up도 품질 검증·호출/토큰/비용 예산에 포함하고 별도 기록한다. 모든 측정 호출은 실제 reused여야 하며 중간 재시작이나 준비 실패를 숨기거나 재준비하지 않는다. 이 조건을 충족할 수 있도록 구현 후 offline 검증한다.

자료별 순서는 Python `random.Random(75020261031 + case_index).shuffle(schedule)`로 `schedule = ['AB'] * 5 + ['BA'] * 5`를 한 번 섞는다(long=0, boundary=1). 생성된 schedule 자체와 Python 버전을 호출 전에 보존한다. 한 쌍 안에서도 두 arm을 순차 실행하며 A/B owner는 공유하지 않는다.

주 지표는 **Service 접수–완료 시간**이다. 각 쌍의 `r_i = B_i / A_i`에 대해 자료별 `median(r_i) ≤ 0.80`, **양측 95% percentile bootstrap CI의 97.5 percentile 상한 < 1.0**을 모두 만족해야 한다. long과 boundary 모두 통과해야 한다.

bootstrap은 자료별 `random.Random(75020261041 + case_index)`로, 10개 paired ratio에서 `rng.randrange(10)`으로 10개를 복원추출하여 중앙값을 계산하는 과정을 20,000회 수행한다. 정렬한 중앙값 배열에서 `h=(N−1)p`의 양옆 값을 선형 보간하여 p=0.025/0.975 분위수를 구한다. baseline 분모가 0·미확인이면 epsilon 등으로 보정하지 않고 비교 불가다.

**secondary warm:** short → dense → none, 자료별 3쌍. 자료마다 새 독립 A owner(1worker), B owner(2worker)를 만들고 primary와 동일하게 worker당 short 1회 warm-up 후 3쌍 동안 유지한다. AB/BA 순서는 short=`AB, BA, AB`, dense=`BA, AB, BA`, none=`AB, BA, AB`로 고정한다(난수 없음). 홀수여서 완전 균형이 아님을 표시한다. worker 재시작·사용하지 않은 worker를 숨기지 않으며 아직 존재하지 않는 lazy 정책을 가정하지 않는다. 각 자료의 paired Service 시간 비율 중앙값은 ≤1.10이어야 한다.

**cold 보조:** long → boundary, 각각 3쌍. long=`AB, BA, AB`, boundary=`BA, AB, BA`로 고정한다(난수 없음). 매 arm 시작 직전에 새 owner를 만들고 종료 후 닫는다. warm-up 없음. 시간은 자료용 저장소 준비와 owner 생성 직전부터 추출·평가·owner 종료 완료까지 monotonic 시계로 측정하며 양 arm의 경계를 동일하게 한다. 기존 문서 `seconds`가 이 경계를 충족한다고 가정하지 말고 #755에서 명시적으로 계측한다. 자료별 paired 전체 시간 비율 중앙값 ≤1.10, 품질·완료 보존을 요구한다. warm과 합치지 않는다.

**공통 품질·비용:**

- 예정된 모든 문서가 100% 완료하고 gold·근거·금지 관계·인용·전체 승인 관계 coverage·부정/조건/시점·dictionary_gaps 계약을 보존해야 한다. 부분 후보 공개나 현재 문서/승인 사전 확인 생략은 허용하지 않는다. none의 정의 불가 precision/recall을 1로 바꾸지 않는다.
- 자료·구간별 B의 모델 오류·timeout·보정 요청 수가 A보다 증가하지 않아야 한다. warm-up 품질 실패도 기록하며 성공 준비로 취급하지 않는다.
- 각 자료·구간(primary warm, secondary warm, cold)의 **paired 측정 호출 총비용 비율과 총 입력+출력 토큰 비율 각각 ≤1.10**, 사용량·비용 확인률 100%다. 비용의 분모가 0·미확인이면 비교 불가다.
- primary long/boundary는 자료별 **warm-up을 포함한 전체 series 비용·토큰 비율도 각각 ≤1.10**이어야 한다. secondary는 warm-up 비용을 별도 공개하고 측정 호출 기준을 적용한다. secondary의 3쌍에서 A 1회/B 2회 준비 때문에 발생하는 구조적 비용 차이를 숨기거나 모든 구간 총비용이 1.10 이하라고 주장하지 않는다. cold에는 준비 호출이 없으며 모든 arm 호출을 포함한다.
- 실패·보정·중단·warm-up·사용하지 못한 호출까지 전체 실제 비용을 보존한다. 유리한 표본을 얻기 위한 추가 반복·자동 재시도·실패 표본 교체는 없다.

**판정 순서:** 확인된 품질·안전 계약 위반은 `rejected`다. 모델 식별·사용량·표본·warm 상태 등 비교 조건이 충족되지 않으면 해당 비교는 `inconclusive`이며 불완전 표본의 비율로 임계값 위반을 확정하지 않는다. 유효하고 완결된 비교에서 하나라도 임계값을 위반하면 `rejected`, 위반이 없지만 필요한 비교가 불완전하면 `inconclusive`, 모든 요구 조건을 만족해야 `accepted`다. accepted도 검증된 합성 자료·제공자·모델·설정 범위에서 #756 운영 적용 검토에 진입한다는 의미다.

결과를 본 뒤 기준을 완화하지 않는다. 실행 불가능한 설계 변경이 필요하면 이유·새 계약 버전을 새 호출 전에 남기고 별도 승인 범위와 맞춘다. 이번 보완 승인은 baseline에만 사용했으며 추가 A/B 호출은 수행하지 않았다.
