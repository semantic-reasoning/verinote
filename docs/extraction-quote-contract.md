# 추출 인용 계약 v2와 역사적 실행

새로 등록하는 승인 사전 추출은 계약 v2를 pin한다. claims/facts의 quote는 **출처 청크 안의 정확히 한 위치**를 식별해야 한다. 원문에 없으면 missing, 두 번째 occurrence가 있으면 ambiguous다. 중첩 occurrence도 검사한다. 다른 청크의 동일 문장은 현재 청크 안의 반복과 구분한다.

모호한 인용이 관계 자체가 거짓이라는 뜻은 아니다. 앞뒤 연속 원문 문맥을 포함해 위치를 식별하되 주장·관계·방향·부정·조건·시점을 바꾸면 안 된다. 유일한 인용도 의미 정합성을 보증하지 않으며, 기존 승인 사전·coverage·dictionary_gaps 요구를 유지한다. 첫 occurrence를 임의 선택하거나 원문·평가 gold를 바꾸지 않는다.

문제 인용은 기존 quote-only repair에서 처리한다. 모든 문제 path를 정확히 한 번 반환하고 quote 이외의 필드는 바꿀 수 없다. 보정 후 전체 응답을 다시 검증한다. 청크당1회·문서당2회, 기존 deadline·공유 호출 예산을 소비하며 실패 시 추가 retry나 부분 후보 게시를 하지 않는다. 원문에서 유일 문맥을 만들 수 없거나 허용 길이를 넘으면 명시적으로 실패한다.

## 버전과 재개

- 누락된 legacy 계약0은 기존 substring 규칙과 기존 coverage 미적용 동작을 유지한다.
- 명시적 v1은 당시 extraction/repair prompt 바이트, substring·coverage·checkpoint 규칙을 유지한다.
- 신규 v2는 유일 위치와 문맥 prompt를 적용한다. queued v1 작업을 v2로 바꾸지 않는다. bool·문자열·null·미지원 버전은 거부한다.
- 실행 pin의 버전을 최초 prompt, preflight, repair prompt, 보정 후 검증, checkpoint 재검증에 끝까지 전달한다.
- 같은 사전 revision의 v1 성공은 v2 재추출을 막지 않는다. 같은 v2의 성공 중복은 계속 막는다. 기존 후보를 삭제하거나 자동 승인하지 않는다.
- v1 checkpoint는 v1에서만 이어 쓸 수 있다. v2로 표시된 checkpoint도 출력 자체를 v2 규칙으로 다시 검증하므로 모호한 prefix는 재사용하지 않는다.

각 경로의 기존 게시 계약을 유지한다. 실험 경로의 현재 문서/사전 검사와 기본 경로의 역사적 버전 게시 정책을 이번 변경에서 바꾸지 않는다. 운영 HTTP에 새 동시성 옵션을 노출하거나 #767의 임계 정책을 구현하는 변경이 아니다.

## 역사적 실험 재현

보존된 #755 environment에는 추출 버전 필드가 없으므로 **알려진 정확한 v1 extraction/repair hash 쌍**으로만 계약을 식별한다. 새 environment는 추출 계약 버전을 명시한다. 알 수 없는 hash, 한쪽만 일치하는 쌍, environment와 run pin/checkpoint가 다른 계약이면 비교 불가다.

데이터셋 버전·추출 버전·A/B 판정 버전은 별개다. 기존 dataset v1·gold·v1의 rejected 결과는 유지한다. 역사적 진단은 명시적 v1 preflight로 당시의 통과/실패를 재현한다. 새 합성 테스트용 v2 oracle은 별도로 충분한 문맥을 생성할 수 있지만 모델 응답이나 과거 결과를 자동 보정하지 않는다.

```sh
.venv/bin/python scripts/benchmark_extraction_ab.py evaluate benchmarks/extraction/results/755-ab-v1-sonnet
.venv/bin/python scripts/diagnose_extraction.py benchmarks/extraction/results/755-ab-v1-sonnet
.venv/bin/python -m unittest discover -s tests -p test_extraction_quote_contract.py
```

새 prompt·품질 계약은 시간과 보정 비용에 영향을 줄 수 있다. #768의 새 실측은 이 수정과 #767 구현이 완료된 같은 revision/prompt에서 사전 기준으로 수행해야 한다. 현재 구현·offline 검증은 실모델 성능 개선이나 추가 호출 승인이 아니다.
