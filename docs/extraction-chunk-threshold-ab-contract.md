# 3청크 이하 순차·4청크 이상 병렬: 사전 A/B 계약 v2

이 문서는 다음 후보의 **실측 전 판정 기준**이다. 1–3청크는 순차, 4청크 이상은 최대2 병렬이라는 정책을 비교한다. 기존 [v1 기각 결과](extraction-sonnet-ab-results.md)는 변경하지 않는다. 이 문서는 모델 호출 승인이 아니며 실제 성능 채택을 뜻하지 않는다.

## 선행 조건과 비교 대상

- #766의 공통 인용 문맥 품질 수정과 #767의 임계 정책 구현을 완료한 같은 revision/tree에서 A/B를 실행한다.
- A는 모든 문서를 동시성1, B는 원문 전체 production `chunks()` 기준 1–3이면1, 4 이상이면2다. cold 4·8청크도 즉시 최대2를 허용한다. 준비 상태에 따른 추가 eligibility나 실제 문서 준비 단계는 없다.
- 전체 청크 수·정책 버전·동시성은 등록 시 고정한다. 재개 suffix 길이로 다시 선택하지 않는다. 성능 표본은 모두 새 Store에서 실행하며 기존 checkpoint를 재사용하지 않는다.
- 제공자 Claude, 요청 모델 Sonnet, 실제 resolved model은 모든 호출에서 확인되고 하나로 동일해야 한다. CLI 버전·prompt·사전·자료·코드·timeout90초를 동일하게 유지한다. provider cache 상태는 관측 불가능하면 unknown으로 남긴다.
- 새 버전의 합성 자료는 production 기준 정확히 3·4·8청크다. 명시적 사실, 부정·조건·시점, 승인 관계 coverage, dictionary_gaps와 청크 경계 인용을 포함하며 gold와 유일 문맥을 offline 검증한다. 준비용 short도 새 품질 계약을 통과해야 한다.
- 구현된 fixture/source/dictionary/gold/prompt/repair prompt/contract/plan 해시와 실제 코드 revision/tree·Python·CLI 버전·전체 argv를 첫 호출 전에 manifest로 묶는다. 아직 없는 자료의 해시를 고정했다고 주장하지 않는다. manifest 검증 전에는 실행할 수 없다. 기존 fixture/gold를 덮어쓰지 않는다.

## 표본과 실행 순서

cold→warm 순서, 각 phase에서 3→4→8청크 순서다. 6개 series index는 cold3=0,cold4=1,cold8=2,warm3=3,warm4=4,warm8=5다. 각 series는 **10쌍**, 총 **120측정 문서**다.

series마다 `schedule=['AB']*5+['BA']*5`에 `random.Random(76420261101+index).shuffle(schedule)`을 한 번 적용한다. 생성된 순서와 Python 버전을 호출 전에 저장한다. 한 쌍의 두 arm은 순차 실행한다. 모든 문서는 독립된 새 저장소를 사용하고 A/B는 owner를 공유하지 않는다.

- **Cold:** 매 arm 직전에 새 owner를 생성한다. 준비 호출은 없다. 저장소 준비·owner 생성 전부터 추출·평가·owner 종료 완료까지 측정한다. 이후 artifact hashing/보고는 개별 cold 지표에서 제외하지만 전체 예산에는 포함한다. 종료 실패는 유효한 cold 표본이 아니다.
- **Warm:** 자료별 A/B owner를 각 series 동안 유지한다. A는1worker, B는3청크에서1worker·4/8청크에서2worker다. 각 worker에 short 문서를 정확히1회 실행해 준비한다. 총 준비는2+3+3=**8문서**다. 모든 준비 호출도 동일 예산·Service·품질 검증을 거친다. 준비는 실험 조건이며 운영 정책의 readiness gate가 아니다.
- warm 지연은 Service 접수–완료 시간이다. 측정 호출의 pool/slot/worker/generation이 준비된 identity와 같고 reused여야 한다. 재시작·준비 실패 후 재준비하지 않는다. cold와 warm 결과를 합쳐 지연을 계산하지 않는다.

## 고정 판정식

시간 비율은 같은 pair의 B/A다. 분모0·unknown에 epsilon을 넣지 않는다.

| 문서 | cold 기준 | warm 기준 |
|---|---|---|
| 3청크 | paired 중앙값 ≤1.10 | paired 중앙값 ≤1.10 |
| 4청크 | 중앙값 ≤0.80, bootstrap95% 상한 <1 | 중앙값 ≤0.80, bootstrap95% 상한 <1 |
| 8청크 | 중앙값 ≤0.80, bootstrap95% 상한 <1 | 중앙값 ≤0.80, bootstrap95% 상한 <1 |

4·8청크의 cold까지20% 이상 개선을 요구하는 것은 **새로 선택한 채택 목표**이며 사용자 정책에서 자동 도출된 사실이나 기존 v1 결과의 재해석이 아니다.

bootstrap은 series별 `random.Random(76420261111+index)`로20,000회 수행한다. 매회10개 paired ratio에서 `randrange(10)`으로10개 복원추출하여 중앙값을 구한다. 정렬한20,000개 중앙값에서 `h=(N−1)p` 양옆을 선형 보간해 p=0.025/0.975 분위수를 구한다. 3청크도 구간을 공개하되 추가 CI 통과 기준은 적용하지 않는다.

모든 series에 다음 조건을 각각 적용한다.

- 측정 합계 reported USD B/A ≤1.10, input+output 토큰 B/A ≤1.10. 모든 warm series(3청크 포함)는 **준비 포함** 합계 비용·토큰도 각각 ≤1.10. 비용은 CLI reported로 확인하며 추정 요율로 대체하지 않는다.
- 예정된128문서(측정120+준비8)의 완료·gold/근거/인용/coverage/부정·조건·시점/dictionary_gaps 품질100%. 부분 게시 금지와 각 경로의 문서·승인 사전 게시 계약을 보존한다. none의 정의 불가 metric은 null이다.
- 사용량·reported 비용·실제 모델 확인100%. 자료/phase별 B의 오류·timeout·repair 수가 A보다 늘지 않는다. 준비/측정을 나누어 공개하고 warm 전체도 오류·timeout·repair 비증가를 요구한다.
- 확인된 품질·안전 위반 또는 유효한 완전 비교의 임계값 실패는 **rejected**다. 다른 비교가 미완료여도 확인된 기각 근거를 숨기지 않는다. 검증 불가능한 원본·표본 부족·모델/worker 조건 미확인은 **inconclusive**다. 모든 요구를 통과한 경우만 **accepted**다.

실패·unknown usage/model/cost·worker 조건 변경·기록 실패·cleanup 오류·예산 소진 시 새 호출을 중단하고 이미 시작한 요청은 종료·집계한다. 실패 표본 교체, 추가 반복, 자동 재시도, 임계값 변경, 유리한 자료군만의 채택은 하지 않는다. 새 설계가 필요하면 새 계약과 승인으로 분리한다.

## 요청 계산과 별도 예산 승인

최초 측정 요청은 `(3+4+8)×10쌍×2arm×2phase=600`, 준비8을 더해 **608회**다. 기존 보정 한도(청크당1·문서당2)를 유지하면 측정120×2+short준비8×1=**최대248회 보정**, 전체 **최대856회**다.

`856×90=77,040초`는 보수적인 직렬 호출 timeout 합이며 wall-clock 승인값이나 예상 실행시간이 아니다. 자료·prompt·도구 완성 후608개 최초/준비와248개 보정의 입력·출력 가정, 기존 관측과 불확실성, 준비·평가·기록·정리 시간을 근거로 **요청·wall 시간·관측 입출력 토큰 예산을 구체적으로 산정해 별도 승인받는다**. 현시점에 실모델 호출은 승인되지 않았다.

관측 한도는 이미 시작한 호출의 종료까지 엄격히 제한하는 hard cap이 아니며 초과 관측량도 공개한다. 예산이 충분하지 않아도 표본을 임의 축소하지 않는다. 원본 plan/environment/report/index/문서DB/CLI/timing/usage/quality를 보존하고 모델 없는 재평가를 제공한다. accepted 이후에도 운영 적용과 실제 서버 재측정은 #756에서 별도로 검증한다.

실제 A/B 도구·자료·별도 예산 승인·판정 작업은 [#768](https://github.com/semantic-reasoning/verinote/issues/768)에서 추적한다.
