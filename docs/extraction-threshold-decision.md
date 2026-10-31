# 청크 임계 실험의 고정 계획·판정기

`verinote.extraction_threshold_ab`는 [사전 계약](extraction-chunk-threshold-ab-contract.md)의 계획 생성과 순수 판정만 구현한다. 모델이나 서버를 호출하지 않는다. 자료는 별도 dataset v2, 추출 품질은 contract v2, 실험 판정은 contract v2다. 기존 v1 판정기는 변경하지 않는다.

계획 canonical SHA-256은 `edef365b348cd9110bd742835da42edd598998cdac62fa8614081e3235d86eec`다. 이 값은 완성된 실행 manifest의 코드·자료·prompt 해시를 대신하지 않는다. 전체 manifest를 만들고 검증하는 실행 도구는 별도 단계다.

| index | series | 각 pair의 첫 arm(10쌍) | bootstrap seed |
|---|---|---|---:|
| 0 | cold 3 | BAABBAAABB | 76420261111 |
| 1 | cold 4 | ABABBABBAA | 76420261112 |
| 2 | cold 8 | BBBABBAAAA | 76420261113 |
| 3 | warm 3 | BBAABABAAB | 76420261114 |
| 4 | warm 4 | BAABABAABB | 76420261115 |
| 5 | warm 8 | AAABBABABB | 76420261116 |

각 문자는 pair의 첫 arm이다. `A`면 AB, `B`면 BA 순서다. 60개 cold owner는 문서마다 독립적이며 warm에는 자료/arm별 6개 owner가 있다. warm 준비는 owner의 각 slot에 short를 한 번씩 실행해 총 8문서다. 측정 120문서와 합쳐 128문서, 최초 요청 608회와 최대 보정 248회, 최대 856회다.

B 측정은 저장된 schema2 `chunk_threshold_v1` 계약의 원문 청크 수·동시성을 검사한다. 결과만 동시성2인 schema1 실행으로 대체하면 유효하지 않다. A 측정은 schema1 동시성1이다. warm 준비만 schema1의 owner capacity와 지정 slot을 사용하며, 이 예외를 B 측정에 적용할 수 없다. 모든 문서는 새 Store의 generation1과 인용 계약2를 사용한다.

판정은 시간, measurement 비용·토큰, 모든 warm의 준비 포함 비용·토큰, 오류·timeout·repair 비증가를 검사한다. 3청크도 bootstrap 구간을 표시하되 상한 조건은 추가하지 않는다. 4/8청크는 cold와 warm 각각 중앙값≤0.80·95% 상한<1을 요구한다. 비용·토큰은 각각≤1.10이며 reported USD를 사용한다. 원본 계약의 seed·20,000회·선형 분위수 계산을 사용한다.

누락·중복·잘못된 순서·연결되지 않은 호출, 최대 보정 초과, owner/pool/worker 공유나 변경, 재시작, unknown usage/model, transport 불일치는 완전한 채택을 막는다. warm 측정 identity는 준비된 pool/slot/worker/generation과 같고 reused여야 한다. cold 표본은 owner cleanup 성공과 양의 전체 시간을 필요로 한다.

각 비교의 유효성을 따로 판단한다. 뒤의 series가 미완료여도 앞서 유효하게 완료된 비교의 임계값 위반, 확인된 품질·게시 안전 위반은 rejected로 남는다. 필요한 증거가 부족하면 inconclusive이며, 모든 요구를 충족해야 accepted다. 합성 응답으로 만든 검증 실행은 accepted/rejected 실측 결과로 취급하지 않고 항상 inconclusive로 표시한다.

**입력 신뢰 경계:** 순수 `evaluate_report`는 이미 원본 검증을 거친 기록에 판정식을 적용하는 내부 함수다. 임의로 작성한 `quality/safety: passed` 플래그를 실제 증거로 인증하지 않는다. 후속 실행 도구의 모델 없는 재평가는 DB 게시 상태, checkpoint, source/dictionary/prompt/plan 해시, CLI timing/usage/model과 정책 연결을 원본에서 다시 확인해야 한다. 이 계층만으로 실측을 수행하거나 #768을 닫지 않는다.
