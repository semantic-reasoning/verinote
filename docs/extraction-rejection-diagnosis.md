# 기각된 추출 A/B의 offline 재현과 다음 구현

#764는 #755의 **rejected**를 그대로 두고, 원본으로 재현 가능한 관측과 다음 구현을 구분한다. 모델 호출 없이 [진단 JSON](../benchmarks/extraction/diagnostics/755-ab-v1-sonnet.json)을 생성했다. 입력 report/index/environment/plan 해시와 호출 ID가 JSON에 있으며, [원본 보고서](extraction-sonnet-ab-results.md)와 모든 DB·호출 기록은 수정하지 않았다.

```sh
.venv/bin/python scripts/diagnose_extraction.py benchmarks/extraction/results/755-ab-v1-sonnet
# 파일 저장은 원본 밖의 새 경로만 허용한다.
.venv/bin/python scripts/diagnose_extraction.py benchmarks/extraction/results/755-ab-v1-sonnet --output /tmp/new-diagnosis.json
.venv/bin/python -m unittest discover -s tests -p test_extraction_diagnostics.py
```

도구는 전체 index/hash와 offline 재평가·문서/호출 연결을 검증한다. `rejected`만으로 검증 성공을 판단하지 않으며, `unverified_other_records`나 기록 손상·누락·부분 표본이면 진단하지 않는다. 저장된 판정과 재평가가 일치해야 한다. 실행 전후 파일 index도 일치해야 하며 원본 DB를 쓰는 기능이 없다. 출력은 실행 시간·출력 절대 경로를 넣지 않아 결정적이다.

## 반복 인용: 확인된 계약 차이

`cold-boundary-measurement-a-3`의 실제 chunk2에는 다음 두 문맥이 있다.

```text
Actual configuration: Atlas calls Billing.
Another hypothetical example: Atlas calls Billing.
```

실제 응답은 `Atlas calls Billing.`만 인용했다. 이 문자열은 `[8022,8042]`, `[8073,8093]`에 존재하며 gold는 실제 설정인 첫 위치를 가리킨다. 운영 `extraction_quote_issues()`는 출처 청크의 substring 존재와 구조·coverage를 확인해 통과한다. evaluator는 인용이 가정 예시와 구분되지 않아 `ambiguous_occurrence`를 반환한다. 관계 TP=1과 grounded TP=0이 동시에 성립한다. 기준선 A의 실패이며 B의 회귀라는 증거가 아니다.

진단은 보존된 checkpoint 응답을 그대로 재평가한다. 별도 대조군 복사본에서만 `Actual configuration: Atlas calls Billing.`으로 문맥을 확장하면 품질이 통과한다. 최소 회귀 fixture도 같은 차이를 재현한다. 기존 gold·평가 코드·실제 출력은 바꾸지 않았다. 이는 fixture 오류보다 운영의 위치 식별 요구가 평가 계약보다 약하다는 근거다. 유일 위치가 의미 정확성을 보증하는 것은 아니며, 가정 문맥을 유일하게 인용해도 사실로 주장할 수 없다.

후속 [#766](https://github.com/semantic-reasoning/verinote/issues/766)은 새 품질 계약에서 충분한 인용 문맥을 요구하고 기존 quote-only 보정 한도로 처리한다. 첫 occurrence를 임의 선택하지 않고, 위치를 식별하지 못하면 실패한다. 의미·coverage·기존 경로별 게시 계약을 유지하며 재개 호환성을 명시한다.

## Dense 지연: 응답 대기가 지배적, 원인은 미확정

| 쌍 | arm | Service 초 | response wait 초 | 출력 토큰 |
|---|---|---:|---:|---:|
| 1 | A | 3.785477 | 3.735737 | 573 |
| 1 | B | 4.264068 | 4.212643 | 614 |
| 2 | A | 3.925080 | 3.871914 | 572 |
| 2 | B | 4.607915 | 4.560453 | 680 |
| 3 | A | 4.668328 | 4.601994 | 681 |
| 3 | B | 3.939658 | 3.896413 | 586 |

paired 지연 비율 중앙값 1.126428은 고정 허용치1.10을 넘었다. worker wait는 약0.000479–0.000659초이고 명시적 로컬 검증·복원·게시 구간은 약0.002564–0.004266초다. 관측상 차이는 response wait가 지배적이며, 출력 토큰도 변한다. scheduler overhead 또는 특정 provider 동작의 인과로 확정할 수 없고 3쌍으로 일반적 회귀·꼬리 지연을 추정하지 않는다. 단일 청크 우회가 줄일 수 있는 작은 로컬 대기를 일반적 속도 개선으로 약속하지 않는다.

JSON은 request의 work 합과 interval union인 wall을 구분한다. CLI response/reset/worker wait는 request 안의 자식 구간이라 request와 더하지 않는다. 명시적 local 구간과 request의 union을 Service total에서 제외한 나머지는 `unattributed_wall`이며 특정 로컬 overhead로 이름 붙이지 않는다. unknown 합은 null, 0은0으로 유지한다.

## Cold long 비용: 캐시 구성 차이가 관측됨

두 번째 청크의 A cache_read는2269–2270, cache_write는2672–2679이고 B cache_read는649, cache_write는4293–4299다. 해당 3회 reported 비용 합은 A USD0.0438349, B USD0.0630427이다. 전체 cold long 비용 B/A는1.139526으로 허용1.10을 넘었지만 토큰 B/A는1.000872다.

원본 input은 cache_read와 cache_write를 포함하므로 캐시 토큰을 다시 더하지 않는다. 진단은 각 pair/arm/chunk의 input/output/cache와 reported USD를 그대로 분해하며 알려지지 않은 단가로 비용을 재계산하지 않는다. 동일 총량이라도 구성 차이가 있다는 관측은 확인했으나 provider cache의 인과나 새 정책의 절감 효과는 미확인이다.

## 다음 성능 후보와 의존 관계

사용자가 지정한 다음 후보는 **원문 전체 1–3청크는 순차, 4청크 이상은 최대2 병렬**이다. [#767](https://github.com/semantic-reasoning/verinote/issues/767)에 이 정책을 구현한다. 청크 수·정책 버전·실효 동시성을 등록 시 고정하며 재개 suffix 길이로 바꾸지 않는다. cold에서도 같은 임계값을 적용하고 worker readiness나 실제 문서를 통한 준비 단계를 추가하지 않는다.

[사전 A/B 계약 v2](extraction-chunk-threshold-ab-contract.md)는 3·4·8청크의 cold/warm 각10쌍을 비교한다. 총120측정 문서와8준비 문서, 최초608회·최대보정248회로 전체 요청 상한856회를 계산했다. 3청크는 지연 비율≤1.10,4·8청크는≤0.80 및95% bootstrap 상한<1, 각 series 비용·토큰≤1.10(모든 warm은 준비 포함도 적용)을 요구한다. cold20% 개선은 새로 선택한 채택 목표이며 v1을 소급 변경하지 않는다.

[#766](https://github.com/semantic-reasoning/verinote/issues/766)의 인용 품질 수정은 별도로 진행하며 새 실측의 선행 조건이다. 코드·새 자료·prompt가 완성되면 해시를 첫 호출 전에 묶고 구체 요청·시간·관측 토큰 예산으로 별도 승인을 받는다. 이번 설계·구현 지시는 추가 모델 호출 승인이 아니다. 기존 v1 원본·기각과 #748/#756 미완료 상태를 유지한다.

실제 A/B 도구·자료·별도 예산 승인·판정 작업은 [#768](https://github.com/semantic-reasoning/verinote/issues/768)에서 추적한다.
