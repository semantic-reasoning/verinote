# 승인 사전 추출 측정 결과와 다음 실험

**실제 모델 성능은 미측정이다.** 이번 결과는 가짜 응답으로 측정 도구의 연결, 인용 보정, 실패·중단 처리와 정확도 평가가 작동하는지 확인한 기록이다. 실제 CLI가 느린 이유나 특정 최적화의 개선율은 아직 알 수 없다. 다음 작업으로는 **승인 후 같은 조건의 순차 실제 CLI 기준선 측정**을 권한다.

## 무엇을 실행했나

2026-10-24에 #727이 병합된 커밋 `ceac163104ba0be5ed38440a8031a0f390d14175`(tree `badbc9ba50f86da32a9c09da2d648ad0333fce51`)를 `git archive`로 따로 내보내 실행했다. 작업 공간에 있던 무관한 수정은 실행 코드에 포함하지 않았다. 원본 결과는 [결과 폴더](../benchmarks/extraction/results/728/)에 수정 없이 보존했다. [index.json](../benchmarks/extraction/results/728/index.json)에 명령 인자, 예상/실제 종료 코드, 파일 크기와 SHA256이 있다.

| 조건 | 이번 실행의 값 |
|---|---|
| 실행 환경 | Darwin / arm64, Python 3.14.7 |
| 패키지 | verinote 0.1.0, pyrewire 1.1.2, pypdf 6.20.0 |
| 자료·평가기·결과 형식 | 모두 버전 1; 추출 계약 버전은 각 결과의 `conditions.extraction_contract` |
| 원문·사전·추출/보정 요청문 | 고정 합성 자료; 각 결과의 `conditions`에 해시 기록 |
| 사람 검토 | #726 정답표를 사용자가 검토·확인한 자료 사용 |
| 응답·에이전트·요청 모델 | `fake` / `agy` / `synthetic-oracle-v1`; 이 모델 이름은 **가짜 응답 표시**이며 실제 모델 ID가 아님 |
| CLI 버전·실제 모델·추론 강도 | 미확인/미실행; `cli_version=null`, `unknown_not_probed`; 기본값을 추정하지 않음 |
| 분할·재사용 | 8,000자, 순차 처리; 매 자료·반복마다 새 DB·승인 사전·checkpoint, 이전 결과 재사용 없음 |
| 공통 한도 | 자료별 3회, 전체 요청 60회·120초·100,000토큰, 호출당 최대 30초, 사용량 미확인 시 중단 |
| 한도 시험의 차이 | long만 사용하고 요청 상한을 1회로 축소 |

자료 순서는 short → long → dense → none → boundary이고, 이 순서를 3회 반복했다. 보정·실패·timeout은 각각 short 3회다. 워밍업 제외나 순서 무작위화는 하지 않았다. 로컬 파일·OS 캐시는 통제하지 않았으며, provider 캐시는 실제 호출이 없어 측정하지 않았다.

### 재현 명령

아래 명령은 위 커밋의 **깨끗한 checkout 또는 export**에서 패키지 의존성을 설치한 뒤 실행한다. `PYTHONPATH=.`로 그 revision의 코드를 우선 사용한다. 출력 폴더는 없어야 한다. 기존 결과에 덮어쓰지 말고 새 폴더 이름을 사용한다. 시간·UUID가 같아지는 시험이 아니라 조건과 결과 의미를 재현하는 시험이다.

```sh
benchmark() {
  PYTHONPATH=. python scripts/benchmark_extraction.py \
    --mode fake --agent agy --model synthetic-oracle-v1 \
    --repetitions 3 --max-requests 60 --max-seconds 120 \
    --max-tokens 100000 --call-timeout 30 --missing-usage stop "$@"
}
benchmark --output ./measurement-normal --cases short long dense none boundary
benchmark --output ./measurement-repair --cases short --scenario repair
benchmark --output ./measurement-failure --cases short --scenario failure
benchmark --output ./measurement-timeout --cases short --scenario timeout
benchmark --output ./measurement-request-limit --cases long --max-requests 1
```

정상·보정은 종료 코드 0, 실패·timeout·한도 시험은 의도적으로 1이다. `index.json.command_argv`는 위 공통 인자를 펼친 명령이다. timeout 시험은 즉시 가짜 TimeoutError를 내므로 **30초를 기다리는 실제 프로세스 timeout 측정이 아니다**. 프로세스 정리는 #727의 별도 합성 subprocess 테스트에서 검증했다.

## 실행 상태·정확도·사용량

아래 TP는 맞은 사실, FN은 놓친 사실, FP는 잘못 추가한 사실이다. 값은 각 실행의 `repetitions[].quality.evaluation`에서 전체 정답을 분모로 합산했다. 보정 횟수와 요청 수는 `attempts` 및 `summary`를 사용한다.

| 결과 파일 | 끝난 자료 실행 수 | 성공/실패 | 예산 허용 요청 | 보정 요청 | TP/FN/FP | 근거까지 맞은 TP | 최종 인용 오류 | 합성 입력+출력 토큰 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| [normal.json](../benchmarks/extraction/results/728/normal.json) | 15 | 15/0 | 21 | 0 | 36/0/0 | 36 | 0 | 2,520 |
| [repair.json](../benchmarks/extraction/results/728/repair.json) | 3 | 3/0 | 6 | 3 | 6/0/0 | 6 | 0 | 720 |
| [failure.json](../benchmarks/extraction/results/728/failure.json) | 3 | 0/3 | 3 | 0 | 0/6/0 | 0 | 0 | 360 |
| [timeout.json](../benchmarks/extraction/results/728/timeout.json) | 3 | 0/3 | 3 | 0 | 0/6/0 | 0 | 0 | 360 |
| [request-limit.json](../benchmarks/extraction/results/728/request-limit.json) | 1 | 0/1 | 1 | 0 | 1/1/0 | 1 | 0 | 120 |

- 정상 자료의 결과는 고정된 합성 정답을 반환하는 fake의 기대 동작이며, 실제 모델의 능력을 보여주는 결과가 아니다.
- normal의 합산 precision·recall·근거 precision·recall은 1이다. 정답이 없는 none 3회는 precision/recall이 null이고 `no_relation_case_pass=true`다. null을 1로 바꿔 평균 내지 않았다.
- repair는 각 첫 응답에 의도적으로 잘못된 인용을 넣었고, 한 번씩 보정한 **최종 결과**에서 오류가 0이다. 이 표는 최초 응답부터 오류가 없었다는 뜻이 아니다.
- failure/timeout은 검증된 청크가 0이며 recall 0, precision null이다. 반환된 사실이 없어 인용 오류 수도 0이지만, 추출 성공이 아니다.
- request-limit은 계획한 long 3회 중 첫 실행에서 멈췄다. 2청크 중 1개만 검증됐고 `quality.complete=false`, `quality.passed=false`, 전체 정답 2개 중 1개를 찾아 recall 0.5다. 나머지 두 반복은 시작하지 않았다.
- 이 사례의 Service 호출 구간 진입은 2회이고 예산 허용은 1회다. 두 번째 `complete()`는 예산 예약 전에 차단됐다. `document_timing.summary.new_call_attempts`를 실제 허용 요청이나 전송 수로 읽지 않는다. 요청 상한은 `budget.requests_started`와 `attempts`로 확인한다.
- 전체 배치는 failure/timeout에서도 `completed`다. 이는 실패한 3회의 반복이 끝났다는 뜻이다. request-limit만 `stopped / request_limit`이다. 실패·중단을 성공 지연 표본에 섞지 않았다.

모든 토큰 값은 fake가 호출당 입력 100·출력 20을 기록한 **합성 계수**다. `token_usage_verified=true`도 이 합성 기록이 완전하다는 뜻이며 실제 provider 사용량 확인이 아니다. reported/estimated USD는 모든 파일에서 null(알려진 호출 0개), 크레딧 집계는 빈 배열이다. 비용이 없다고 결론 내릴 수 없다. 사후 토큰 집계로 마지막 호출의 초과를 막을 수 없어 `token_hard_cap_guaranteed=false`이며, 입력에 포함된 캐시·출력에 포함된 추론을 중복 합산하지 않는다.

## 시간은 어디까지 확인했나

모든 수치는 밀리초(ms)로 환산하고 소수 셋째 자리로 반올림했다. 표준편차는 모집단 기준이다. 표본 1개에서 표준편차 0은 안정성의 증거가 아니다.

| 정상 자료 | 표본 수 | 반복 구간 평균 | 중앙값 | 표준편차 | 최소–최대 |
|---|---:|---:|---:|---:|---:|
| short | 3 | 25.745 | 23.161 | 5.708 | 20.414–33.660 |
| long | 3 | 22.597 | 23.159 | 1.847 | 20.107–24.526 |
| dense | 3 | 23.469 | 23.551 | 0.281 | 23.091–23.764 |
| none | 3 | 21.645 | 21.675 | 1.138 | 20.237–23.023 |
| boundary | 3 | 22.732 | 22.992 | 0.396 | 22.173–23.032 |

이 표는 `repetitions[].seconds`의 자료별 분포이며 `summary.per_case`와 일치한다. 임시 저장소 생성 직전부터 worker 종료·평가 수집까지를 재고, 임시 폴더 삭제와 다음 배치 보고서 갱신은 포함하지 않는다. Service의 접수–완료 시간과는 기준점이 다르다. 전체 배치 경과 시간(`budget.elapsed_seconds`)은 normal 385.410ms, repair 89.296ms, failure 86.614ms, timeout 85.905ms, request-limit 38.429ms다. 명령 실행 전 입력 확인과 출력 폴더 생성은 이 예산 시계에 포함하지 않는다.

| short 또는 부분 실행 | 표본 수 | 성공/실패 분류 | 반복 구간 평균 | 중앙값 | 표준편차 |
|---|---:|---|---:|---:|---:|
| repair / short | 3 | 성공 | 25.919 | 23.604 | 4.210 |
| failure / short | 3 | 실패 | 25.368 | 22.720 | 4.119 |
| timeout / short | 3 | 실패 | 25.158 | 22.953 | 3.431 |
| request-limit / long | 1 | 실패·중단 | 31.154 | 31.154 | 0.000 |

다음 표는 #725 문서 시간 기록의 **자료 실행 1회당 평균**이다. normal은 서로 다른 자료 15회, repair는 short 3회의 평균이므로 두 열의 차이를 보정 비용이나 성능 차이로 해석하지 않는다. 원본은 `repetitions[].document_timing.summary`다.

| 문서 시간 구간 | normal 평균 ms | repair 평균 ms |
|---|---:|---:|
| 대기: DB 실행 등록·executor 진입까지 | 0.874 | 1.061 |
| worker 전체 (아래 구간들을 포함) | 6.338 | 7.611 |
| **Service 접수–완료: 대기+worker** | **7.212** | **8.672** |
| 복구 확인 | 0.524 | 0.597 |
| 최초 호출: 청크들의 합 | 1.845 | 1.203 |
| 검증·기록: 보정 부모 구간 제외 | 1.034 | 1.268 |
| 보정 요청 | — | 1.072 |
| 보정 적용·재검증·기록 | — | 0.202 |
| 보정 기타 | — | 0.485 |
| 청크 기타: 준비·기록 등 | 1.301 | 0.987 |
| 후보 등록 | 0.717 | 0.791 |
| worker 기타 | 0.918 | 1.006 |

각 청크의 leaf 시간을 합한 뒤 자료 실행 수로 나눴다. worker와 접수–완료는 부모 구간이므로 leaf에 다시 더하지 않는다. `repair` 부모와 그 안의 보정 요청/검증도 중복 합산하지 않는다. `—`는 보정 단계 미진입(null)이다. 첫 출력 시각으로 AI 추론 시작이나 답변 시작을 추정하지 않는다.

문서 시간 기록은 normal 15/15, 나머지는 각각 3/3·3/3·3/3·1/1로 확인됐다. #724의 실제 CLI 내부 시간은 **모든 배치에서 0개 확인**이다. fake가 프로세스를 띄우지 않으므로 실행 준비·프로세스 생성·첫 출력·CLI 해석 시간을 나눌 수 없다. 가짜 호출 구간에는 파일 기록·예산 확인 등 도구 비용이 포함된다. 네트워크나 모델 추론 대기를 측정한 값이 아니다.

## 다음 개선 후보 비교

아래의 기대 효과는 **가설**이다. 이번 시험은 다섯 후보를 A/B 비교하지 않았고, 어떤 후보의 속도 향상도 실측하지 않았다.

| 후보 | 이번에 확인한 근거와 부족한 증거 | 다음에 필요한 실험 | 정확도·호환성 위험 |
|---|---|---|---|
| 조각 두 개 동시 처리 | long/boundary의 2청크와 prefix 평가가 동작함. 실제 호출 대기 비중·동시 요청 제한·순차 대비 이득은 미측정 | 순차 실모델 기준선 뒤 동일 자료를 최대 2개 동시 처리와 교차 비교 | 실패 후 추가 호출, 보정 배분, 순서가 뒤섞인 checkpoint·부분 후보 등록, 요금 증가 |
| 공식 지원되는 추론 강도 조절 | 현재 Verinote CLI 명령은 effort를 지정하지 않음. Claude Code 공식 CLI 문서는 `--effort`를 설명하고 지원 수준이 모델에 따라 다름을 명시함. 설치 버전·선택 모델·다른 CLI 지원은 미확인 | 해당 버전/모델의 공식 지원과 실제 적용 값을 먼저 확인하고, 같은 모델에서 기본값과 더 낮은 지원 수준만 비교 | 사실 누락·가정/부정 오판·인용 오류, 모델 fallback/기본값 차이가 effort 효과로 보일 수 있음 |
| 반복 입력 재사용 | 시험은 매 반복 새 상태로 실행해 checkpoint 재사용이 없음. provider 캐시 적중과 토큰 절약은 미측정 | 같은 요청문의 첫 호출과 반복 호출을 분리하고 cache 사용량·정확도를 비교; 검증 결과 재사용과 provider 캐시를 별도 조건으로 둠 | 문서·사전·모델·설정 변경 뒤 오래된 결과 재사용, 자료간 문맥 혼입, 확인 안 된 cache를 적중으로 간주 |
| 응답 길이 축소 | 최종 사실·인용 검증과 정확도 지표가 연결됨. 실제 출력 토큰 비중과 단축 효과는 미측정 | 기준선에서 출력 비중이 큰지 확인 후, 필수 계약을 유지한 간결한 표현을 별도 요청문 버전으로 비교 | 정답·근거·coverage 누락, 절단·보정 증가, checkpoint 계약 비호환 |
| CLI 프로그램 재사용 | 이번 fake에는 subprocess가 없어 시작 비용을 측정하지 못함. 세션 이어가기와 같은 OS 프로세스 재사용은 다름 | #724로 실제 시작/출력/해석 구간을 확인하고 공식 지속 프로토콜·버전·요청 경계를 검증한 뒤 새 프로세스와 비교 | 이전 문맥·인증·설정의 잔류, 응답 경계/호출 수/사용량 귀속 실패, 종료·복구 방식 변화 |

공식 지원 확인 출처는 [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference#cli-flags)와 [model configuration](https://code.claude.com/docs/en/model-config#adjust-effort-level)이다(2026-10-24 확인). 이 문서는 향후 실험 가능성을 확인한 자료이며, 현재 설치된 CLI나 agy의 지원을 입증하지 않는다. 현재 측정 도구에는 effort 실험 옵션이 없으므로 임의 인자를 넣어 실행하지 않는다.

동시 처리를 나중에 시험하더라도 다음 규칙을 유지해야 한다. **연속해서 검증된 앞부분만** 재개에 쓰고, 문서 전체 성공 뒤에만 후보를 등록한다. 보정은 청크 번호 순으로 결정적으로 배분해 청크당 한 번·문서당 두 번을 넘지 않으며, 최초 요청·보정·검증은 같은 청크 제한 시간을 공유한다. 모델·승인 사전·결과에 영향을 주는 설정을 고정하고, 실패하면 새 호출 허용을 중단한다. 이미 허용된 호출은 모두 종료·사용량·프로세스 정리를 확인한다. 이 규칙을 만족하는 실험 구현은 이번에 하지 않았다.

## 먼저 할 일: 승인 후 순차 실모델 기준선

지금은 다섯 후보 중 최적화 승자를 고를 증거가 부족하다. 우선 실제 CLI 한 종류에서 **변경 없는 순차 처리**의 반복 측정을 제안한다. 다음 조건은 미실행 계획이며 별도 승인이 필요하다.

1. provider 하나와 명시적인 모델 ID·CLI 버전을 선택한다. 모델 alias/fallback, 지원되는 설정, 요청문·사전·자료 해시를 기록한다. 실제 적용 모델/설정을 확인하지 못하면 그 사실을 남기고 다른 조건과 같은 것으로 합치지 않는다.
2. 같은 5개 자료를 각 3회, 요청 최대 60회·전체 900초·토큰 관측 한도 200,000·호출당 최대 90초·사용량 미확인 시 중단으로 제한한다. 토큰은 사후 정보이므로 한 번의 초과 가능성이 남는다. 실제 요청·보정·실패·이미 시작한 호출의 마지막 사용량을 모두 남긴다.
3. 매 문서 새 저장소를 유지한다. 각 호출은 새 CLI 프로세스다. 첫 실행과 뒤의 반복을 따로 표시하고, OS/provider 캐시를 확실히 비웠다고 주장하지 않는다. provider 캐시 상태가 미확인이면 `unknown`으로 남기며, 이후 warm-cache 실험과 합치지 않는다.
4. 자료별 성공/실패 시간 분포, 대기·최초/보정 호출·검증·기타, #724 시작/첫 출력/해석 구간, TP/FN/FP·근거 정확도·인용 오류·사용량/비용의 확인률을 함께 비교한다. 첫 출력은 추론 시작의 증거가 아니다.
5. 실제 변동과 실패율을 확인한 뒤 **다음 A/B 실험을 시작하기 전에** 시간·정확도·실패·보정·비용의 수용 기준과 필요한 표본 수를 정한다. 이번 fake의 3회 편차로 실모델 표본 수나 개선율 목표를 확정하지 않는다.

기준선에서 모델 호출 대기가 지배적이고 두 요청을 허용한다는 증거가 확보되면, 위 보존 규칙을 만족하는 **최대 2청크 동시 처리**를 첫 최적화 실험 후보로 검토할 수 있다. 실제 시작 비용이 지배적이면 프로그램 재사용의 공식 프로토콜 확인부터, 출력 비중이 크면 응답 축소의 품질 실험부터 다시 판단한다. 어느 경우도 “2개를 동시에 실행하면 두 배 빨라진다” 같은 산술 추정을 실측 성과로 쓰지 않는다.

운영 코드·기본값·요청문·응답 형식은 이번에 변경하지 않았다. 이 보고서의 제안은 자동으로 후속 이슈를 만들거나 실제 모델을 실행한 것이 아니다. 실행 도구의 한도와 결과 해석은 [측정 도구 문서](extraction-benchmark.md), 정답 기준은 [평가 도구 문서](extraction-evaluation.md)를 참고한다.
