# 합성 추출 정답 자료 — 사람 검토 요청

상태: **사람 검토 확인 완료**. 합성 원문과 정답은 에이전트가 작성했으며, 사용자가 이 표를 검토한 뒤 현재 대화에서 **“표를 검토했고 정답 기준이 맞습니다”**라고 확인했습니다(2026-10-24).
이 확인은 #726의 원문 의미·전체 정답·허용 표현·금지 관계·인용 위치 표에 대한 사람 검토이며, 에이전트 대조와 구분합니다. 실제 모델 성능을 확인한 것은 아닙니다.

평가 대상은 원문에 **명시된 직접 호출만**입니다. 역방향·전이·부정·가정은 사실이 아닙니다.
아래 정답 목록은 각 합성 문서의 평가 대상 전체 집합입니다. 다른 관계는 금지 예시 목록에 없어도 FP입니다.

| 자료 | 원문 의미와 전체 정답 | 대표 금지 관계 |
|---|---|---|
| short | Atlas가 Billing을 직접 호출. Billing이 Ledger를 직접 호출. Atlas→Ledger는 명시적으로 부정. Billing의 별칭은 BILL. 정답은 인접 두 호출만. | Atlas→Ledger, Billing→Atlas |
| long | 청크1의 Atlas→Billing, 청크2의 Billing→Ledger 두 호출. 나머지는 관계 없는 합성 padding이며 Atlas→Ledger는 부정. | Atlas→Ledger, Ledger→Billing |
| dense | S0→S1→S2→S3→S4→S5→S6→S7의 **인접 7개 직접 호출만**. 다른 호출은 명시하지 않음. | S0→S2, S1→S0 |
| none | Atlas와 Billing 이름만 등장하며 직접 호출은 진술하지 않음. 정답 0개. | Atlas→Billing, Billing→Atlas |
| boundary | 청크1은 `Hypothetical example only: Atlas calls Billing.`. 청크2는 `Actual configuration: Atlas calls Billing.` 및 다른 hypothetical 예시. 정답은 청크2의 실제 Atlas→Billing 하나. | Billing→Atlas |

short의 Atlas→Billing에 `Atlas calls BILL`도 사전의 명시적 별칭으로 허용합니다. 다른 임의 동의어·대소문자 변환은 하지 않습니다.
중복은 TP를 늘리지 않으며, 맞는 triple의 잘못된 인용도 별도 오류로 남깁니다.

## 원문에서 확인할 인용 위치

Python 문자열 기준 [start,end)이며 UTF-8 byte offset이 아닙니다. 인용은 해당 청크에 있어야 하고 아래 근거 전체를 포함해야 합니다.
반복 문구가 같은 청크 안에서 실제/가정 위치 모두에 매칭되면 모호함으로 보고합니다. boundary의 모범 인용은 구분되는 `Actual configuration: ...` 전체입니다.

| 정답 ID | 직접 호출 | 근거 위치 | 정확한 근거 문자열 |
|---|---|---|---|
| boundary-1 | Atlas → Billing | [8022,8042) | `Atlas calls Billing.` |
| dense-1 | S0 → S1 | [0,21) | `S0 directly calls S1.` |
| dense-2 | S1 → S2 | [22,43) | `S1 directly calls S2.` |
| dense-3 | S2 → S3 | [44,65) | `S2 directly calls S3.` |
| dense-4 | S3 → S4 | [66,87) | `S3 directly calls S4.` |
| dense-5 | S4 → S5 | [88,109) | `S4 directly calls S5.` |
| dense-6 | S5 → S6 | [110,131) | `S5 directly calls S6.` |
| dense-7 | S6 → S7 | [132,153) | `S6 directly calls S7.` |
| long-1 | Atlas → Billing | [0,29) | `Atlas directly calls Billing.` |
| long-2 | Billing → Ledger | [8000,8030) | `Billing directly calls Ledger.` |
| short-1 | Atlas → Billing | [0,29) | `Atlas directly calls Billing.` |
| short-2 | Billing → Ledger | [30,60) | `Billing directly calls Ledger.` |

## 자료 파일과 식별 정보

- [boundary](../verinote/benchmark_data/v1/boundary.json): 8094자, source SHA256 `2e7f61628ac20bea53fd48afefd07548c7f5511290d34fd4f6d038e71b8fcf80`
- [dense](../verinote/benchmark_data/v1/dense.json): 188자, source SHA256 `72a40739d4e7795874ffe9e993b5967525b822f2511576fa2faf2512ae7315df`
- [long](../verinote/benchmark_data/v1/long.json): 8068자, source SHA256 `cda309a9b6447b8d6722317a21f339327632139f185807f7ac6259abdaecc299`
- [none](../verinote/benchmark_data/v1/none.json): 67자, source SHA256 `09da66e9641414fb667c11e7f1e21d4ef57470a9f10904fa4cee74cfc5bfd765`
- [short](../verinote/benchmark_data/v1/short.json): 127자, source SHA256 `79733f4de70a615dc31ba44a3714d5e9f1778d51d37c6a1ef8dd03eb140c86d4`

의도적 잘못된 인용과 기대 보정은 별도 repair-cases/prefixed_quote.json에 있습니다. 정상 인용을 바로 반환해도 정답입니다.
