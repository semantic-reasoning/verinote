# Sonnet 임계 추출 정책 A/B v2 실측 결과

고정 계약의 판정은 **rejected**다. 첫 `cold-three-measurement-b-1` 문서에서 유한 annotation 품질이 실패해 새 호출을 중단했다. 생산 경로의 구조·인용 검증과 게시 안전성은 통과했지만 전체 품질 통과는 아니다. A/B 쌍이 완성되지 않아 시간·비용의 개선이나 회귀를 판단할 수 없다. 이 B 문서는 3청크·동시성1이므로 병렬 정책이 실패 원인이라는 근거도 없다.

## 승인 범위와 고정 조건

2026-11-02 사용자의 “실측은 claude를 통해서 진행하라” 지시에 따라 이전 Claude 실측 보류를 해제하고, [공개된 예산안](extraction-threshold-ab-budget.md)의 Sonnet 단일 실험을 실행했다. 최대 856요청·14,400초·관측 입력+출력 30,000,000토큰이다. 이미 시작한 요청의 종료까지 포함하므로 관측 한도는 엄격한 hard cap이 아니다. 과거 이슈 댓글의 7,200초 제안과 구분한다. 추가 실행이나 실패 표본 대체는 하지 않았다.

- 실행 revision `be948abe439f3b5bbafc4d9721ea069b7ad12607`, tree `fa16f831bda316829ef80432fe23150a75ca6a07`.
- Claude CLI `2.1.316`, 요청 alias `sonnet`, 세 호출 모두 `modelUsage`로 확인한 실제 모델 `claude-sonnet-5-5`, timeout 90초.
- dataset v2·추출 인용 계약2·임계 정책 `chunk_threshold_v1`. A는 순차, B는 전체 1–3청크 순차·4이상 최대2다. 이번 문서는 B/3청크/동시성1이었다.
- [사전 계약](extraction-chunk-threshold-ab-contract.md)의 cold→warm, 3→4→8청크, 각 10쌍·총 120측정+8준비 문서를 유지했다. plan SHA256 `edef365b348cd9110bd742835da42edd598998cdac62fa8614081e3235d86eec`.
- 첫 호출 전에 environment·plan·limits·fixture·prompt·실행 코드 사본/해시를 보존했다. provider cache 상태는 unknown이며 cold는 새 로컬 owner를 뜻한다.

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_threshold_ab.py run \
  --output benchmarks/extraction/results/768-threshold-v2-sonnet \
  --model sonnet --max-requests 856 --max-seconds 14400 \
  --max-tokens 30000000 --allow-model-calls
```

위 명령은 실행 이력이다. 중단된 실험을 재개하거나 다시 실행하는 지시가 아니다.

## 실제 관측

| 항목 | 관측 |
| --- | --- |
| 완료 문서 | 첫 cold 3청크 B 문서 1개; A·나머지 series·준비 문서는 미실행 |
| 요청 | 최초 3회, 인용 수정 0회, 시작/종료 3회, pending 0 |
| 관측 토큰 | 입력 39,408 + 출력 3,629 = 43,037; unknown usage 0 |
| 별도 cache 항목 | cache read 5,131, cache write 34,271; 입력에 다시 더하지 않음 |
| CLI reported USD | 0.1738991; 추정 요율이나 청구서 금액이 아님 |
| cold 문서 시간 | 30.796282292초; 평가·owner 종료 포함 |
| Service 총 시간 | 30.736341958초 |
| 전체 관측 wall | 30.910894834초; 마지막 기록 쓰기 제외 |
| production | success, 3/3청크 검증 |
| fact gold | precision/recall/grounding/citation 각각 3/3, 잘못된 fact 0 |
| 유한 annotation | failed: claim 근거·coverage 의미·gap 완전성 |
| 게시 안전성·cleanup | passed, 위반 없음; owner cleanup 성공 |
| 게시 상태 | fact 3개·page 1개 pending; 합성 사전만 approved |
| 종료 | document_failed; 추가 호출·반복·대체 없음 |

모든 호출은 하나의 pool/slot1/generation1에서 수행됐으며 첫 process 시작 후 같은 worker를 재사용했다. 고정 기준의 전역 기각 근거는 `quality:cold-three-measurement-b-1`이다. `incomplete_execution`, `missing_documents`도 함께 기록된다. 여섯 cold/warm 비교는 모두 `inconclusive`, 통계·비율은 null이다. 확인된 품질 위반이 있으므로 전역 판정은 rejected로 남는다.

## 품질 실패 근거와 한계

첫 두 청크는 부정을 설명하는 prose가 있지만 그 부정 원문을 인용한 claim이 없어 `missing_claim_evidence(negated)`다. 세 청크 모두 소유 관계의 gap을 미등록 대상의 인용 claim에 연결해 `missing_claim_evidence(missing_relation)` 및 `unexpected_gap_evidence(missing_relation)`이 발생했다.

세 청크의 calls coverage는 미등록 대상 근거를 포함하지 않아 `coverage_semantics(calls)`다. 첫 두 청크는 과거·중단된 보관을 `no_evidence`로 표시해 `coverage_semantics(archives)`도 실패했다. 조건·시점 표현 불가 gap이 완전하지 않아 세 청크 모두 `gap_completeness`가 실패했다. 응답을 의미적으로 보충하거나 gold·기준을 완화하지 않았다.

이는 구조 검증으로 검출되지 않는 주장·근거·사전 표현 범위 연결의 실패다. 모델 일반 능력이나 모델 간 우열, 운영 문서 전체의 품질을 한 문서로 일반화하지 않는다. 합성·agent 작성 gold의 사람 검토는 기존과 같이 pending이다. 임계 정책의 성능 채택 및 #756 운영 적용, #748 성능 개선 완료 근거는 아니다. Architect·Critic의 독립 원본 검토와 논의를 거쳐 [후속 #785](https://github.com/semantic-reasoning/verinote/issues/785)에 공통 충실도 진단·개선 후보를 등록했다. 후속 후보는 별도 계약과 검증이 필요하다.

## 보존 원본과 모델 없는 재평가

[report.json](../benchmarks/extraction/results/768-threshold-v2-sonnet/report.json) SHA256은 `72a63ca70e3cfdc9bd9a771999a65ed537bb584d5c8aacfe054c2e4ece981a5f`다. 원본 91개 파일·1,586,365바이트를 보존했다.

- [evaluation](../benchmarks/extraction/results/768-threshold-v2-sonnet/evaluation.json), [environment](../benchmarks/extraction/results/768-threshold-v2-sonnet/environment.json), [전체 파일 index](../benchmarks/extraction/results/768-threshold-v2-sonnet/index.json).
- [첫 실제 prompt](../benchmarks/extraction/results/768-threshold-v2-sonnet/documents/cold-three-measurement-b-1/runs/ad6cb91d14ea413cb360c493a31f8c1e/1/prompt.txt), [첫 CLI stdout](../benchmarks/extraction/results/768-threshold-v2-sonnet/documents/cold-three-measurement-b-1/runs/ad6cb91d14ea413cb360c493a31f8c1e/1/stdout.txt). 같은 run 폴더에 2·3번 호출 및 checkpoint·coverage·timing이 있다.
- 문서 SQLite, 원본 fixture/gold, 코드 사본, CLI stream·usage·timing을 같은 실행에 연결해 검증한다. 보존된 Python 소스는 실행하지 않는다.

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_threshold_ab.py evaluate \
  benchmarks/extraction/results/768-threshold-v2-sonnet
```

HTTP와 subprocess를 차단한 offline 재평가가 저장 판정과 완전히 같았고 전후 모든 원본 해시가 같았다. 재평가는 읽기 전용이며 모델을 호출하지 않는다. 실행 코드나 fixture가 바뀌어 HEAD의 재평가가 inconclusive이면 위 고정 revision에서 재평가한다. `evaluate`는 기록된 hash를 보존 사본과 대조하므로 사본과 report·environment를 함께 바꾼 변조를 탐지하지 못할 수 있고, `diagnose()`는 `report.json`을 고정 SHA256과도 대조한다. rejected의 CLI 종료 코드는 0이며 품질 통과를 뜻하지 않는다.
