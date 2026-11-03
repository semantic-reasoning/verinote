# 추출 평가 기준의 제품 타당성 검토

**현재 점수만을 근거로 추가 모델 실측을 우선하지 않는다.** 인용 구간·참조에 대한 결정적 검사와 설명문의 의미 적합성을 구분하고, [후속 #788](https://github.com/semantic-reasoning/verinote/issues/788)에서 별도 버전의 제품 기준을 정하는 순서를 권고한다. 이것은 에이전트 작성 검토이며 사람 검토 완료나 새 채택 기준이 아니다.

## 재현되는 관측과 해석

[보존 Sonnet 결과](extraction-threshold-sonnet-v2-results.md)는 production 3/3·사실 gold 3/3이지만 annotation 오류 16개로 **rejected**다. 원본·gold·평가기·기존 판정은 변경하지 않았다. 검토 도구는 인증된 replay를 사용하고, 각 오류에 실제 인용·참조·고정 요구·제품 사용 위치를 연결한다. 관측과 `agent_proposed` 판단은 별도 필드다.

| 관측된 증상 | 제품에서 확인할 문제 | 검토 해석 |
| --- | --- | --- |
| 청크 1·2 부정 설명의 quote가 부정 문장이 아님 | 페이지/일괄 승인 화면과 승인 위키 질문 근거에서 설명을 해당 quote로 확인할 수 없음 | 근거 연결 문제. 금지된 긍정 fact가 게시됐다는 뜻은 아님 |
| 세 청크의 소유 관계 gap이 미등록 호출 대상 quote를 참조 | gap의 claim 번호를 따라가도 소유 제약의 근거가 없음 | 실제 오연결. missing-claim·unexpected-gap·gap-completeness 증상이 겹침 |
| calls coverage가 이미 있는 미등록 대상 claim을 참조하지 않음 | 관계별 점검에서 관련 claim으로 가는 경로가 누락됨 | 근거 탐색 문제와 표현 민감도가 겹침. 위키 claim 자체가 사라진 것은 아님 |
| 청크 1·2 archives는 no_evidence지만 올바른 과거 quote와 종료 이유를 보존 | 현재 승인 사실의 부재와 관련 과거 근거의 표현 불가 중 status가 무엇을 뜻하는지 불명확 | 정책 미해결. 과거 한정어가 없어진 것으로 집계하지 않음 |
| qualifier gap의 일부 참조가 없지만 한정어가 페이지/coverage에 남음 | 모든 한정어를 gap에도 반복해야 하는지, gap의 단위가 무엇인지 결정 필요 | 정책 미해결. 별도의 소유 근거 오연결까지 무해하다고 취급하지 않음 |

5개 증상 그룹은 에이전트가 제안한 묶음이다. 일부 오류가 여러 그룹에 들어가므로 합계를 독립 결함 수나 심각도 점수로 사용하지 않는다. 16개 원래 오류를 전부 보존한다. 제품 사용 근거는 실제 [페이지/coverage 렌더링](../verinote/web/app.js), [승인 위키 질문 문맥](../verinote/assistant.py), [coverage 기록](../verinote/extraction_coverage.py) 및 고정 평가기의 구간·참조 검사에서 추적한다. 보고서에 전체 파일 hash·구간·발췌가 있다.

## 합성 대조 사례 15개

이는 새 모델 응답이 아니라 bundled short의 통제된 복사·변환이다. 제품 기대는 에이전트 작성이며 `human_review=pending`이다. 결과를 새로운 의미 판정기로 사용하지 않는다.

| 대조군 | production / finite 관측 | 해석 |
| --- | --- | --- |
| 기준 응답, 순서·인덱스 재매핑, 충분한 연속 인용으로 claim 결합, 부수 문구 변경, gap 행 분리·결합, 더 긴 인용의 7개 | 모두 통과 / 통과 | 이 표현 차이는 기존 검사도 허용함 |
| 정확히 지정한 다섯 claim에서 구간 표지만 뺀 인용 | 통과 / 실패(12개 annotation 오류) | 작성한 의미 보존 기대와 지정된 전체 evidence 구간 포함 요구가 다름 |
| 조건 한정어를 quote에서 제거, 잘못된 gap 참조 | 통과 / 실패 | 위험한 근거 변형도 별도로 대조함 |
| coverage 행 누락, fact가 있는데 status를 부재로 변경 | 실패 / 미평가 | production 구조 검증에서 거부됨 |
| 과거 근거를 유지한 status 변경, 과거 claim을 유지한 gap 참조 생략 | 통과 / 실패 | 제품 정책이 미해결인 사례이며 무조건 동등하다고 하지 않음 |
| 올바른 부정 quote와 반대 의미의 긍정 설명문 | 통과 / 통과 | 인용 포함 검사는 설명문의 의미 일치까지 검사하지 않음 |

구체적으로 `구간 1 부정: S1는 S0를 직접 호출하지 않는다.`의 quote를 `S1는 S0를 직접 호출하지 않는다.`로 줄이는 대조군은 부정을 보존한다. 반대로 원래 quote를 유지하고 설명문만 `S1는 S0를 현재 무조건 직접 호출한다.`로 바꾸는 대조군은 현재 finite 검사에서 통과한다. 뒤의 관측은 기존 도구가 범용 entailment를 보장한다고 약속했다는 뜻이 아니라, **통과 점수의 해석 범위**를 보여준다.

표지 제거는 이 fixture의 다섯 명시적 대조 사례만 대상으로 한다. 실제 구성과 가정 예시를 구분하는 문맥, 부정·조건·시간을 일반적으로 제거하는 규칙을 제안하지 않는다. 기존 rejection을 이 대조군으로 무효화하지 않는다.

## 다음 결정

후속 #788에서 검토한 대체 근거 구간, 한정어 보존, claim 결합·분리·참조 불변성, coverage 상태와 gap 단위를 별도 버전으로 명세한다. 결정적 검사, 의미 검토가 필요한 부분, 미판정을 구분한다. agent/Claude 의견을 사람 승인으로 표시하지 않는다.

그 뒤의 작은 실측은 **고정된 기준에 대한 후보의 준수도**를 확인할 수 있다. Opus 교체나 새 prompt가 제품 설명의 정확성을 보장하는지, 기준 자체가 타당한지, 일반 문서의 품질이나 속도가 개선됐는지는 한 번의 통과로 확인할 수 없다. 모델을 바꾸면서 기준도 바꾼 결과를 모델 효과로 해석하지 않는다. #785 후보 평가, #756 운영 적용, #748 성능 개선은 별도 요구로 남는다.

## 원본·재현·출처

[review.json](../benchmarks/extraction/results/785-criteria-review/review.json) SHA256: `77357b149da5a7d7fe2a0c46e8070d610369893ed1a32878b542b175d578dad6`.

검토 도구 커밋 `f1dc31ed196e8be10fbac56c7215ab2e8c2843a7`, 구현 전 기준 revision `eafa841de6980dd08f2cf11a1413688038ab9fe2`다. 아티팩트는 도구/대조 정의와 실제 점검한 제품·평가기의 hash를 과거 실행 인증과 분리해 기록한다. 과거 report SHA256은 `72a63ca70e3cfdc9bd9a771999a65ed537bb584d5c8aacfe054c2e4ece981a5f`다. `review_base_revision`은 검토 도구의 새 commit을 인증하는 필드가 아니며, 새 구현은 별도 source hash와 위 commit으로 식별한다.

```sh
R=$(git rev-parse --show-toplevel); D=$(mktemp -d)
git -C "$R" archive 9b808a54173fd81c751f16d20f27bb262fb5ae5d | tar -x -C "$D"
(cd "$D" && PYTHONPATH=. "$R/.venv/bin/python" scripts/review_extraction_criteria.py) > /tmp/criteria-review.json
cmp /tmp/criteria-review.json "$R/benchmarks/extraction/results/785-criteria-review/review.json"
```

위 사본에서 JSON이 바이트 단위로 재생성된다. #792 수정이 `diagnose()` 출력과 `tests/test_extraction_criteria_review.py`를 바꿨으므로 HEAD에서는 재생성되지 않는다. HTTP/subprocess를 차단하고 SQLite를 읽기 전용으로 제한한 재생성도 같으며, 기존 91개 원본 파일의 해시는 전후 동일하다. 추가 생성 요청은 0회다. 필요한 증거가 원본과 결정적 대조에서 확보되어 Claude 보조 호출은 사용하지 않았다.
