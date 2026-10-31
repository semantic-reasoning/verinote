# Sonnet 최대 2청크 A/B 실측 결과

**고정 계약 v1 판정: 기각(rejected). 운영 적용하지 않는다.** 주요 warm 문서의 속도 개선은 확인됐지만 dense 지연·cold 비용 기준을 넘었고, 마지막 cold 기준선 문서의 인용 근거 품질이 실패했다. #755의 실험은 기각 근거를 확보했으며 #748의 성능 개선 및 #756의 운영 적용은 완료되지 않았다.

## 실행과 증거

- 실행 revision `5b55189b4f14919682c1027b3523940366c51b80`, tree `5aa782ee7286d61f90c31a2fa789d30ea8be627c`. 이 문서를 추가한 commit과 구분한다.
- 요청 모델 `sonnet`, 모든 137회 실제 모델 `claude-sonnet-5-5`; Claude CLI `2.1.311`, Python `3.15.0`, `macOS-27.0.1-arm64-arm-64bit-Mach-O`.
- 승인 예산: 최대 274회·3,600초·관측 입출력 2,500,000토큰. 각 호출 timeout 90초. A/B는 같은 코드의 명시적 동시성 1/2이며 별도 owner를 사용한다.
- [plan](../benchmarks/extraction/results/755-ab-v1-sonnet/plan.json), [environment·실제 argv·해시](../benchmarks/extraction/results/755-ab-v1-sonnet/environment.json), [전체 report](../benchmarks/extraction/results/755-ab-v1-sonnet/report.json), [원본 파일 index](../benchmarks/extraction/results/755-ab-v1-sonnet/index.json), [재검증 가능한 판정](../benchmarks/extraction/results/755-ab-v1-sonnet/evaluation.json), [문서별 DB·CLI·timing 원본](../benchmarks/extraction/results/755-ab-v1-sonnet/documents/)을 변경 없이 함께 보존한다. 합성 자료만 사용했다.
- report SHA256: `5379d55927e8830d2c7a400d22d2bc5c96d36b6799080970d5a2bbed732fb5cc`.

## 실행 회계와 품질

예정된 85문서(측정 70·준비 15), 최초 137회 호출을 모두 수행했다. Service 상태는 85/85 success지만 품질은 **84/85 통과**다. 마지막 문서 평가 후 `status=stopped`, `stop_reason=document_failed`로 종료했다. 미실행 문서 0, pending 0, 호출 오류·timeout·인용 보정 0, cleanup 오류 0이다. 실패한 품질 표본도 제거하거나 교체하지 않았다.

전체 관측 입출력 토큰 **880,878**, CLI reported 비용 **USD 2.8507081**, report의 전체 예산 경과 **399.056795초**다. 최종 index/verdict 파일 기록은 이 report snapshot 이후다. 준비 15호출은 83,855토큰·USD 0.3512195, 측정 122호출은 797,023토큰·USD 2.4994886이다. 사용량·비용·실제 모델은 137/137 확인됐고 예산 초과는 없었다. 관측 예산은 이미 시작한 호출까지 엄격히 차단하는 hard cap이 아니다. CLI reported USD는 실제 청구액을 확정한 값이 아니다.

모든 문서 합계 TP 162·FN 0·FP 0, grounded TP 161이다. `cold-boundary-measurement-a-3`(기준선 A)의 chunk 2 인용은 원문 내 `[8022,8042]`, `[8073,8093]` 두 위치에 존재해 `ambiguous_occurrence`로 판정됐다. 해당 문서의 TP는 1이지만 grounded TP와 citation accuracy는 0이다. 이 실패를 B 동시성으로 인한 회귀라고 단정하지 않는다. none 자료의 정의 불가 precision/recall은 원본의 null로 유지한다.

## 고정 기준에 따른 비교

[사전 계약 v1](extraction-sonnet-baseline-completed.md#755에-적용할-사전-ab-판정-계약-v1)의 표본·순서·seed·임계값을 변경하지 않았다. 아래 비율은 모두 B/A이며 지연은 paired 비율 중앙값, 비용·토큰은 측정 합계의 비율이다. primary는 10쌍·20,000 bootstrap·95% 구간, secondary/cold는 3쌍이다. cold boundary는 품질 조건 실패로 통계 판정이 불가능하며 유리한 쌍만 추려 계산하지 않는다.

| 단계 | 자료 | 지연 B/A 중앙값 | bootstrap 95% | 비용 B/A | 토큰 B/A | 판정 |
|---|---|---:|---|---:|---:|---|
| primary | long | 0.530656 | 0.490484–0.614348 | 1.002906 | 1.001263 | accepted |
| primary | boundary | 0.515214 | 0.508627–0.561844 | 1.002180 | 1.000877 | accepted |
| secondary | short | 0.969293 | — | 1.002228 | 1.000716 | accepted |
| secondary | dense | 1.126428 | — | 1.010432 | 1.003501 | rejected |
| secondary | none | 0.967884 | — | 0.991578 | 0.998449 | accepted |
| cold | long | 0.548059 | — | 1.139526 | 1.000872 | rejected |
| cold | boundary | 미판정 | — | 미판정 | 미판정 | inconclusive |

primary long/boundary 지연 중앙값은 각각 약 46.9%/48.5% 감소했고 bootstrap 상한도 1 미만이다. 준비 포함 비용/토큰 비율은 long 1.059014/1.041516, boundary 1.055933/1.040599로 모두 1.10 이하다. 이 부분 통과를 전체 채택으로 해석하지 않는다.

secondary dense 지연 1.126428은 허용치 1.10을 넘었다. cold long 비용 1.139526도 허용치 1.10을 넘었다. secondary 준비 비용은 측정 비용과 별도로 공개한다:

| 자료 | A 준비 USD / 토큰 | B 준비 USD / 토큰 |
|---|---:|---:|
| short | 0.0235289 / 5604 | 0.0467758 / 11175 |
| dense | 0.0236289 / 5611 | 0.0474918 / 11249 |
| none | 0.0230549 / 5553 | 0.0465718 / 11154 |

warm 호출은 준비된 worker identity·generation 재사용 검사를 통과했다. cold는 저장소·owner 생성 전부터 평가·owner 종료까지 측정했고 이후 hashing은 제외했다. provider cache 상태는 unknown이며, 비용 차이를 특정 cache 원인으로 확정하지 않는다. secondary/cold 3쌍은 꼬리 지연이나 일반적 회귀를 추정하기에 부족하지만, 사전에 정한 이 실험의 기각 조건에는 해당한다.

## 재검증과 후속 작업

원본 index/hash·문서/attempt 연결 및 품질을 확인한 offline 재평가 결과가 저장된 evaluation과 정확히 일치했다. 전체 토큰·비용을 모든 attempt에서 별도 합산했다. 모델을 호출하지 않는 재평가:

```sh
.venv/bin/python scripts/benchmark_extraction_ab.py evaluate benchmarks/extraction/results/755-ab-v1-sonnet
```

재현 실행은 environment의 argv와 고정 revision을 사용하되 출력 경로는 새 폴더로 바꾸고 별도 호출 승인을 받아야 한다. 이번 승인으로 재시도·표본 교체·추가 실험을 하지 않았다. 전체 원본에는 합성 원문과 로컬 실행 경로·CLI session 식별자가 포함되며 인증정보 패턴 검사에서 발견된 항목은 없었다.

다음 후보는 단일 청크·cold 조건의 비용/지연과 인용 위치 모호성을 먼저 분리해 원인을 확인하고 새 설계·판정 계약으로 검증해야 한다. 기존 v1 기준을 결과에 맞춰 완화하지 않으며, 채택 후보가 생기기 전에는 #756의 운영 적용이나 기본 동시성을 변경하지 않는다.

후속 재현·진단과 다음 후보 계약은 [#764](https://github.com/semantic-reasoning/verinote/issues/764)에서 진행한다.
