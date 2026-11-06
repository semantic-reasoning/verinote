# 추출 제품 적합성 기준 v1

**기존 평가 계약과 분리된 v1 기준으로, 인용 구간·참조의 결정적 검사와 설명문의 의미 적합성 검토를 나눈다.** [#788](https://github.com/semantic-reasoning/verinote/issues/788)의 명세·fixture·offline 검증기와 그 결과 보고서다. 모든 정책·기대·판단은 에이전트 작성(`agent_proposed`)이며 `human_review=pending`이다. 사람 검토 완료나 새 채택 기준이 아니다. 기존 dataset v2 gold·fixture·평가기·원본과 [보존 Sonnet 결과](extraction-threshold-sonnet-v2-results.md)의 **rejected** 판정은 바꾸지 않았다.

## 층 구분

| 층 | 내용 |
| --- | --- |
| D1 production | 기존 `_production` replay를 읽기 전용으로 재사용한다. 실패하면 `production_failure`이고 D3–D6은 건너뛴다. D3–D6은 정규화된 출력만 읽는다. |
| D3 claim 근거 | 각 claim quote는 청크에서 중복 없이 나와야 하고, 그 출현 위치가 의무의 canonical 구간이나 등록된 대체 구간을 포함해야 한다. 여러 quote를 이어 붙인 근거는 받지 않는다(`split_across_quotes`). |
| D4 coverage 연결 | 관계별 행이 등록된 필수 의무를 지지하는 claim을 연결해야 한다. 추가 연결은 허용한다. |
| D5 gap | 단위는 (code, 의무) 쌍이다. 무관한 claim 연결은 `unrelated_gap_link`, 빠진 쌍은 `missing_gap_pair`다. 행의 분리·결합은 허용한다. |
| D6 status | 등록된 기대 status와 다르면 `status_mismatch`다. v1 결정에는 면제가 없다. |
| S1 설명문 적합성 | 계산하지 않는다. 등록 의무를 지지하는 claim은 `semantic_review_required`로 남는다. |

전체 판정 값은 명세의 `overall`에 정의돼 있다. 에이전트 의미 판단은 `undecided_agent_flagged`까지만 만들고 fail·pass를 만들지 않는다. `pass`에는 사람 승인 기록이 필요하며, 커밋된 기록은 없다. 승인 기록은 단일 출력 digest가 아니라 `case_id`와 평가한 청크 출력 목록의 digest인 `chunk_outputs_sha256`가 모두 같고 `human_review`가 approved일 때만 적용된다. 보고서 행은 이 값을 단일 출력 digest인 `output_sha256`와 별도 필드로 싣는다.

전체 판정과 제품 중요도 category는 별개다. 실패 종류가 evidence면 `product_evidence`, label이면 `unresolved`다. v1의 P-FAILURE-KIND 결정에서 evidence는 실패에 필요한 의무가 D3에서 실패한 경우이며, 기각한 대안은 quote가 보이는 실패에도 evidence를 줄 수 있다(민감도 절). `representation_sensitive`는 실패 종류가 아니라, 고정 v2 오류의 필드로 고른 관련 v1 실패가 없다는 표시다. 따라서 label 실패는 전체 판정이 `fail`이면서 category가 `unresolved`일 수 있다. `assistant.wiki_text`가 승인 페이지의 모든 claim을 quote와 함께 보여 주므로, v1 결정의 승인 출력에서는 D3 실패가 근거 도달 여부의 기준이 된다.

claim 단위의 `out_of_scope`는 어떤 등록 의무도 지지하지 않는 claim에 붙는다. 가정 예시 줄을 인용한 claim뿐 아니라 부정·조건·시간 한정어가 빠져 D3에서 실패한 조각 인용도 여기에 해당한다. 그 의무를 다른 claim이 지지하지 않으면 D3 실패로 전체 판정이 `fail`이 된다. 범용 entailment·모순 탐지와 다중 문서는 범위 밖이다.

## 정책

| 정책 | v1 결정 | 기각한 대안 |
| --- | --- | --- |
| P-EQUIV | 등록·검토한 대체 구간만 인용 동등으로 인정 `@spec.policies.P-EQUIV.decision=registry_substitutes` | 일반 접두사 제거 `@spec.policies.P-EQUIV.rejected_alternatives.0.decision=generic_prefix_strip`, evidence 부분 문자열 `@spec.policies.P-EQUIV.rejected_alternatives.1.decision=substring_of_evidence` |
| P-ORDER | 일관된 인덱스 재매핑에 불변 `@spec.policies.P-ORDER.decision=index_remap_invariant` | 없음 |
| P-CLAIM-MERGE | quote가 각 구간을 포함하면 claim 결합 허용 `@spec.policies.P-CLAIM-MERGE.decision=one_quote_covers_each_span` | 의무마다 claim을 따로 요구 `@spec.policies.P-CLAIM-MERGE.rejected_alternatives.0.decision=require_one_claim_per_obligation` |
| P-CLAIM-SPLIT | 여러 quote의 결합을 거부 `@spec.policies.P-CLAIM-SPLIT.decision=reject_joined_quotes` | quote 합집합 인정 `@spec.policies.P-CLAIM-SPLIT.rejected_alternatives.0.decision=accept_union_of_quotes` |
| P-COVERAGE-LINK | 필수 의무를 관계별 행에 모두 연결 `@spec.policies.P-COVERAGE-LINK.decision=all_required_linked` | 연결 선택 `@spec.policies.P-COVERAGE-LINK.rejected_alternatives.0.decision=links_optional` |
| P-STATUS-HISTORICAL | 등록된 기대 status, D6 면제 없음 `@spec.policies.P-STATUS-HISTORICAL.decision=registry_expected_status` | 행이 지지 claim을 연결하면 `no_evidence` 허용 `@spec.policies.P-STATUS-HISTORICAL.rejected_alternatives.0.decision=historical_no_evidence_linked` |
| P-FAILURE-KIND | 필요한 의무가 D3에서 실패할 때만 evidence `@spec.policies.P-FAILURE-KIND.decision=evidence_iff_obligation_fails_d3` | D4 연결 누락은 항상 evidence `@spec.policies.P-FAILURE-KIND.rejected_alternatives.0.decision=missing_link_always_evidence` |
| P-GAP-QUALIFIER-DUP | 한정 의무마다 qualifier 쌍 요구 `@spec.policies.P-GAP-QUALIFIER-DUP.decision=every_qualified_obligation` | `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.0.label=A`: coverage가 연결한 historical 면제 `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.0.decision=exempt_linked_from_coverage` `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.0.obligation_ids.0=historical`, `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.1.label=B`: coverage가 연결한 모든 qualifier 쌍 의무 면제 `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.1.decision=exempt_linked_from_coverage` `@spec.policies.P-GAP-QUALIFIER-DUP.rejected_alternatives.1.obligation_scope=all_qualifier_pairs` |

등록된 대체 구간은 표지만 뺀 본문이며, 그 검사 항목은 `validate_chunk_registry`가 정한다(예: 원문 일치, 손으로 지정한 한정어 표지 포함). 잘못 연결된 gap 행처럼 지지 claim이 페이지에 남아 있는 경우도 P-FAILURE-KIND에 따라 label이다.

## 합성 fixture와 v2와의 차이

bundled short와, short의 entity를 줄마다 바꾸고 줄 순서를 바꾼 변형 `authored-pf1`에 같은 변환을 적용한다. 변형의 label은 entity 치환 아래 short 등록 행을 그대로 따른다. 그래서 사례 `summary.fixture_cases=2`개의 판정이 같다는 것은 이 변형에 대해 줄 위치·entity 이름·줄 순서가 v1 판정을 바꾸지 않았다는 뜻일 뿐이며, 다른 변형이나 독립적으로 작성한 label의 일치를 보여 주지 않는다. 기준 출력(변환 없음)을 포함한 recipe는 `summary.fixture_recipes=19`개이며 의미 보존 변형, 한정어 삭제·오연결·설명 모순 반례, 정책 사례와 범위 밖 사례를 포함한다.

`differences_from_v2`는 #785 대조군 `summary.controls_785=15`개와 short fixture를 합친 `summary.differences_from_v2_rows=34`개 행이다. v2 finite 통과 여부와 v1 전체 판정이 다른 행은 `summary.v2_differences=4`개이며 모두 명세에 의도한 차이로 선언했다. 등록된 표지 제거 대체 구간은 v2에서 실패하지만 v1에서 `undecided`이고, 올바른 부정 quote에 반대 의미 설명문을 붙인 출력은 v2에서 통과하지만 v1에서 `undecided_agent_flagged`다.

#785가 material_failure로 본 대조군 중 v1이 label 실패로만 fail한 행은 `summary.disagreements_with_785=1`개(`summary.controls_disagreeing_with_785=control:wrong_gap_reference`)이며, 명세의 `disagreements_with_785`에 기록했다. 설명문이 quote와 모순되는 대조군은 #785가 material_failure로 봤고 v1에서는 에이전트 표시(`undecided_agent_flagged`)로만 드러난다. #785가 policy_unresolved로 본 대조군은 v1에서 `fail`이지만 category는 `unresolved`다. 이런 경우는 전체 판정이 아니라 에이전트 표시와 category로 맞닿으며, 위 집계에 넣지 않는다.

## 과거 오류 연결

인증된 보존 실행의 정규화 출력에 D3–D6을 적용하고, 고정 annotation 오류 `summary.historical_errors=16`개를 오류 필드에서 유도한 규칙으로 v1 원인에 연결했다. 코드에 오류별 표는 없다. 오류 종류마다 고르는 v1 실패는 `_related_failures`가 정한다. 예를 들어 gap-completeness 오류는 그 청크의 빠진 (code, 의무) 쌍(`missing_gap_pair`)을 고르며, v2도 기대 쌍이 빠졌을 때만 이 오류를 낸다. 원인 키 (청크, 대상, 근거)의 형태는 `_cause_keys`와 `map_errors`가 정한다. 예를 들어 D3에서 실패한 의무가 필요한 실패는 그 D3 원인으로 모이고, D6 label 실패는 `predicate:` 대상과 D6 근거를 쓰며, 대응하는 v1 실패가 없는 오류는 오류 대상과 `v1_pass` 근거를 쓴다. label 실패는 여러 의무를 가리켜도 원인을 나누지 않는다.

| 오류 | v1 원인과 category |
| --- | --- |
| error-1, error-7 | 부정 의무의 D3 실패 `c1:negated:D3`·`c2:negated:D3`, `product_evidence` |
| error-2, error-5, error-8, error-11, error-13, error-15 | 청크별로 공유되는 소유 의무 D3 원인 `c1:missing_relation:D3`·`c2:missing_relation:D3`·`c3:missing_relation:D3`, `product_evidence` |
| error-3, error-9, error-14 | calls 행의 미등록 대상 연결 누락(P-COVERAGE-LINK), `unresolved` |
| error-4, error-10 | archives status 불일치(D6) `c1:predicate:archives:D6`·`c2:predicate:archives:D6`, `unresolved` |
| error-6, error-12, error-16 | 소유 의무 D3 원인과 historical qualifier 쌍(P-GAP-QUALIFIER-DUP), `c1`은 `c1:conditional:P-GAP-QUALIFIER-DUP`도 추가. `product_evidence`·`unresolved` |

고유 원인은 `historical_mapping.cause_counts.total=14`개로, `product_evidence`가 `historical_mapping.cause_counts.product_evidence=5`개, `unresolved`가 `historical_mapping.cause_counts.unresolved=9`개, `representation_sensitive`가 `historical_mapping.cause_counts.representation_sensitive=0`개다. 공유 원인은 중복 없이 센다. 이 값은 독립 결함 수나 심각도 점수가 아니다.

#785와의 원인 수준 비교는 #785 자체가 아니라, 명세에 에이전트가 작성한 `group_correspondence_785`(#785 그룹별 대응 원인)를 기준으로 측정한다. 이 기준에서 일치하는 오류는 `summary.historical_errors_agreeing_with_785=15`개이고, 불일치하는 오류는 `summary.historical_errors_disagreeing_with_785=error-6`이다. `c1` 출력에는 qualifier gap 행이 없어 conditional과 historical 쌍이 모두 빠졌으며, #785의 error-6 관측도 conditional과 historical 쌍을 함께 나열한다. #785의 qualifier 그룹은 historical 쌍이 빠지고 historical claim이 인용된 경우에 붙는 규칙이며, 그 근거문은 평가기가 모든 conditional/historical 쌍을 요구한다고 쓰면서 historical 근거가 다른 곳에 보존됐다고 논증한다. conditional 쌍에 대한 별도 원인은 두지 않았다. v1은 그 쌍을 `c1:conditional:P-GAP-QUALIFIER-DUP` label 원인으로 따로 기록한다.

category 차이는 따로 기록한다. error-3, error-9, error-14에 #785가 붙인 `product_evidence_gap`·`representation_sensitive`는 v1에서 `unresolved`이며, #785 검토의 "위키 claim 자체가 사라진 것은 아님"과 같은 방향이다. #785 검토에서 `representation_sensitive`는 정책·표현 의존을 가리키는 데 쓰였고 v1의 같은 이름은 관련 v1 실패가 없음을 뜻하므로, 정의가 다르다. #785가 error-4, error-10과 gap-completeness 오류에 붙인 그 태그가 v1 결정에서 나오지 않는 것은 근거 차이와 함께 정의 차이도 반영한다. 에이전트 판단으로는 #785의 그 태그에 v1의 `unresolved`가 더 가깝다.

## 민감도

실행 가능한 기각 대안 `summary.sensitivity_executable=5`개를 각각 넣어 연결을 다시 계산했다. 나머지 대안은 실행하지 않고 그대로 기록한다(전체 `summary.sensitivity_alternatives=9`개).

| 대안 | 바뀐 오류 | 고유 원인 |
| --- | --- | --- |
| 연결 선택 `sensitivity.4.decision=links_optional` | error-3, error-9, error-14가 `representation_sensitive` | `sensitivity.4.cause_counts.total=14`, 그중 `representation_sensitive` `sensitivity.4.cause_counts.representation_sensitive=3` |
| historical status 면제 `sensitivity.5.decision=historical_no_evidence_linked` | error-4, error-10이 `representation_sensitive` | `sensitivity.5.cause_counts.representation_sensitive=2` |
| 연결 누락 항상 evidence `sensitivity.6.decision=missing_link_always_evidence` | error-3, error-9, error-14가 `product_evidence` | `sensitivity.6.cause_counts.product_evidence=8` |
| qualifier 대안 A `sensitivity.7.decision=exempt_linked_from_coverage` `sensitivity.7.label=A` | error-6, error-12, error-16에서 historical qualifier 원인 제거(예: `sensitivity.7.moved_errors.0.causes_removed.0=c1:historical:P-GAP-QUALIFIER-DUP`), 바뀐 오류의 category는 `product_evidence`·`unresolved` | `sensitivity.7.cause_counts.total=11` |
| qualifier 대안 B `sensitivity.8.decision=exempt_linked_from_coverage` `sensitivity.8.label=B` | error-6, error-12, error-16에서 위 historical qualifier 원인을 제거하고, 이 가운데 `c1` 오류에서는 conditional 원인도 제거(`sensitivity.8.moved_errors.0.causes_removed.0=c1:conditional:P-GAP-QUALIFIER-DUP`), 바뀐 오류의 category는 `product_evidence` | `sensitivity.8.cause_counts.total=10` |

실행한 어느 대안에서도 소유·부정 의무의 D3 원인은 그대로 남는다. 이 원인들은 실행한 정책 대안으로는 사라지지 않는다.

## 측정이 답할 수 있는 것과 없는 것

새 모델 실측은 **고정된 v1 결정적 층에 대한 후보 출력의 준수도**에 답할 수 있다. v1 실패의 종류와 근거 정책별 분포도 같은 기준으로 볼 수 있다. 다만 `scripts/verify_product_fitness_v1.py`가 평가하는 입력은 `verify_proposal`에 고정돼 있으므로, 새 실행은 `assess`·`map_errors`를 그 출력에 직접 적용해야 한다. #785 후보 시험에는 `scripts/assess_extraction_fidelity_trial.py`가 그렇게 적용한다. 이 스크립트는 입력을 인자로 받지 않으며, 비교를 위해 같은 v1 기준 사본으로 #768 보존 실행도 다시 평가한다. 설명문이 quote와 의미상 맞는지, 기준 자체가 제품에 타당한지, 일반 문서의 품질·속도가 나아졌는지는 답하지 못한다. 모델과 기준을 동시에 바꾼 결과를 모델 효과로 해석하지 않는다. 이 작업만으로 #785 후보 품질, #756 운영 적용, #748 성능 개선을 완료하지 않는다.

## 범위와 한계

- 어휘 검사 대상은 `tests/test_product_fitness_v1_authored.py`의 어휘 테스트에서 `authored_texts`·`recipe_prose`·`hangul_literals`가 모은 문자열이며, 모은 문자열은 문자 종류와 무관하게 토큰 단위로 dataset v2 어휘와 대조한다. `hangul_literals`는 product-fitness 테스트 파일(`TEST_FILES`)에서 한글이 들어 있는 문자열만 모은다. 모으지 않는 문자열의 예로 판단의 rationale 필드와 한글이 없는 테스트 문자열(영문 docstring·메시지 포함)이 있다.
- `extraction_fidelity_artifacts.sources()`는 `verinote` 아래 모든 Python 파일을 모으므로, 앞으로의 후보 시험 inventory에 `verinote/diagnostics/product_fitness_v1.py`가 포함된다.
- 명세의 `assisted_review.claude_used=false`는 이슈가 허용한 선택적 Claude 보조 검토 단계를 사용하지 않았다는 뜻이다. 개발 과정의 에이전트 게이트(계획 비판, 리뷰, 최종 게이트)는 그 보조 검토 단계도 사람 검토도 아니다. `agent_proposed` 판단은 개발 에이전트(Claude 모델)가 작성했고, 그 작성자는 따로 `agent_proposed`로 기록한다.
- 정책 결정과 #785 그룹 대응은 에이전트 판단이며 사람 검토로 뒤집힐 수 있다. 민감도 절이 정책 대안의 결과를 보여 준다.
- `_production`은 private 이름이다. 보고서의 `diagnostic_implementation`이 그 파일의 hash를 담으므로, 파일이 바뀌면 커밋된 보고서와의 바이트 비교가 실패한다.

## 원본·재현·출처

보고서 [report.json](../benchmarks/extraction/results/788-product-fitness-v1/report.json) 해시: `@report_sha256=386b837823bca45f86dcdd31d5ce2a9674a025b3afab54d4b3ab524ae336fb1a`.

작업 출발 main revision은 `base_revision=7831b96f4ec756599174200e68c6b2669d534e43`이며, #788 이슈 본문이 기준 revision으로 적은 재현 revision `issue_reproduction_revision=eafa841de6980dd08f2cf11a1413688038ab9fe2`과 다르다. 보고서의 `source_hashes`가 묶는 파일은 `report_source_paths`가 정하며, 기준 디렉터리에서는 이름이 점으로 시작하지 않는 JSON 파일만 넣는다. 이 문서는 보고서 해시를 인용하므로 제외한다. 모듈이 import하는 진단 파일(`extraction_fidelity.py` 등)의 hash는 `diagnostic_implementation`에 기록된다. `verify_proposal()`이 호출하는 `diagnose()`는 그 밖에 import하는 최상위 `verinote` 모듈의 현재 파일 hash를 보존 실행 report의 environment hash와 비교하지 않고, 보존 호출을 현재 코드로 다시 처리해 보존 기록과 비교한다. 보존 실행 report는 `historical_authentication.report_sha256=72a63ca70e3cfdc9bd9a771999a65ed537bb584d5c8aacfe054c2e4ece981a5f`로, #785 [review.json](../benchmarks/extraction/results/785-criteria-review/review.json)은 `review_785_sha256=77357b149da5a7d7fe2a0c46e8070d610369893ed1a32878b542b175d578dad6`로, 평가에 쓴 dataset v2의 short·three 사례는 등록부의 `case_sha256`으로 고정한다.

```sh
PYTHONPATH=. .venv/bin/python scripts/verify_product_fitness_v1.py > /tmp/pf.json
cmp /tmp/pf.json benchmarks/extraction/results/788-product-fitness-v1/report.json
```

같은 고정 코드·자료에서 JSON이 바이트 단위로 재생성된다. 추가 생성 요청은 `generation_requests=0`회다. 보존 원본의 hash와 #785 review.json은 재생성 전후로 같다.
