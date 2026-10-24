# 합성 사실 추출 평가 — 자료 v1 / 평가자 v1

이미 승인된 사전을 적용하는 사실 추출을 비교하는 고정 자료다. 운영 프롬프트·승인·
검증·청크 규칙을 바꾸지 않고 모델·네트워크·인증 정보 없이 평가한다. 실제 사용자
자료를 포함하지 않으며 실제 모델 성능은 미측정이다.

## 실행

저장소 루트에서:

```sh
python -m scripts.evaluate_extraction --case short \
  --result benchmarks/extraction/examples/short-perfect-input.json --output /tmp/score.json
python -m scripts.evaluate_extraction --case short \
  --result benchmarks/extraction/examples/short-missing-input.json
python -m unittest discover -s tests -p test_extraction_evaluation.py
```

두 번째 입력은 기존 구조 검증을 통과하지만 두 정답 중 하나를 빠뜨려 recall=0.5다.
CLI는 malformed 파일에 오류를 반환한다. 점수가 낮은 유효 입력은 평가 JSON으로 남기며
명령의 프로세스 실패와 혼동하지 않는다. 자료 파일은 2 MiB, 결과 입력은 4 MiB 이하다.
출력 파일 쓰기 외에는 입력을 수정하지 않고 subprocess나 모델·네트워크를 호출하지 않는다.

```python
from verinote.extraction_evaluation import load_case, evaluate_case, evaluate_many
case = load_case('short')
score = evaluate_case(case, [{'index': 1, 'output': {'facts': []}}])
combined = evaluate_many([score])
```

입력은 `{"case_id":"short","chunks":[{"index":1,"output":{"facts":[...]}}]}`다.
index는 1부터 시작하고 생산 코드의 정확한 8,000자 분할을 사용한다. output은 생산
추출 응답을 그대로 넣을 수 있으나 evaluator는 facts 채널만 평가한다. 위키 claim의
내용이나 coverage의 의미 정확도를 대신 평가하지 않는다. 운영 구조 검증은 별도로 유지한다.
알 수 없는·중복 청크 번호, facts 없는 output, 배열이 아닌 결과는 평가 불가 오류다.
청크 결과를 일부 생략하거나 빈 배열을 명시하면 그 사실은 누락으로 평가한다.

## 자료와 신원

패키지 `verinote/benchmark_data/v1`에 다섯 자료가 있으며 wheel에도 포함된다.
short/long은 직접 호출 두 개, dense는 인접 직접 호출 일곱 개, none은 관계 0개,
boundary는 가정/실제 문장의 위치를 구별하는 관계 하나다. 정답은 각 자료의 평가 대상
관계 **전체 집합**이다. 금지 목록은 대표 오류 설명이며 목록 밖의 모든 추가 관계도 FP다.

manifest에는 자료 버전, 생산 추출 계약, 청크 크기와 각 case의 canonical JSON
SHA256이 있다. case는 source UTF-8 SHA256, 합성 원문, 유효한 사전, gold ID별
허용 triple/근거 위치/모범 인용, 금지 triple을 포함한다. 위치는 Python 문자열
`[start,end)`이며 byte offset이 아니다. loader는 hash·사전·근거 범위/문자열·허용
표현의 모호함·금지 관계 중복을 검증한다. 원문을 trim/교정하지 않는다.

`human_review`는 실제 사람 검토의 상태다. 구조 테스트와 에이전트 대조는 사람 검토를
대신하지 않는다. 현재 검토 상태와 모든 정답·금지 관계·근거 표는
[검토 자료](extraction-evaluation-review.md)에 있다. CLI 결과에도 manifest의 상태를
첨부한다. `evaluate_case`는 전달한 case를 평가하는 순수 함수이며 사람 검토를 보증하지 않는다.

## 사실 식별·중복

subject/object는 사전의 명시적 별칭만 canonical ID로 바꾼다. 양끝 공백 정리는 생산
문자열 규칙과 일치한다. 대소문자, 유사어, 방향, 부정·조건·시점을 임의로 제거하거나
추측하지 않는다. 자료에 적힌 variant가 다른 gold ID와 겹치면 loader 오류다.

한 gold ID는 TP를 한 번만 얻는다. canonical/variant가 같은 정답은 한 단위로 묶고,
오답은 canonical triple 단위로 묶는다. 중복은 별도 행 위치로 보고한다. 중복된
오답을 여러 번 써도 FP가 추가로 늘지는 않는 **고유 사실 단위** 평가다. 잘못된
형식의 개별 fact 행은 조용히 버리지 않고 invalid_facts에 넣고 각각 precision
분모와 FP 수에 포함한다. 인용이 문자열이 아닌 행도 형식 오류다.

## 지표와 인용

- semantic TP: 허용 triple에 매칭된 고유 gold ID 수. 인용이 틀려도 의미 일치와 구별한다.
- FP: 정답에 없는 고유 triple + 형식 오류 행. FN: 매칭되지 않은 gold ID 수.
- precision = TP / 고유 예측 단위 수(형식 오류 행 포함), recall = TP / 정답 수.
- grounded TP: 의미 일치와 인용 기준을 모두 통과한 gold ID 수.
- grounded precision/recall: 같은 분모에서 grounded TP를 사용한다.
- citation_accuracy = grounded TP / semantic TP. 인용 문제는 행별 citation_errors에도 남는다.
- 중복 중 하나가 좋은 인용이어도 다른 중복의 잘못된 인용은 상세 오류에서 사라지지 않는다.
- `passed`는 누락·추가·형식·인용 오류·중복이 전부 없어야 true다.

인용은 응답의 원래 청크에서 정확한 literal occurrence를 찾는다. 그 범위가 gold
근거 전체를 포함해야 하며, 다른 청크의 인용이나 근거 일부만 자른 인용은 실패다.
생산 인용 한도(8,000자) 안에서 넓은 인용이 근거 전체를 포함하면 허용한다. 따라서
이 도구는 인용의 간결함 점수는 아니다. 완성된 응답을 자동 교정하지 않는다.

반복 인용은 위치를 추측하지 않는다. 같은 청크의 모든 occurrence가 허용 근거를
포함하면 사용할 수 있지만 실제/가정 등 허용되지 않은 occurrence와 섞이면
`ambiguous_occurrence`로 표시하고 grounded TP를 주지 않는다. 문자열만으로 모델이
어느 위치를 의도했는지는 알 수 없다. quote_ambiguities에 가능한 위치를 제공한다.
한 인용의 occurrence가 64개를 넘으면 자원 제한으로 미확인(`occurrence_limit`) 처리한다.

분모 0인 지표는 value=null, reason=not_applicable이며 원시 수는 0으로 보존한다.
빈 정답·빈 결과는 no_relation_case_pass=true이고 precision/recall은 null이다.
빈 결과·정답 있음은 recall=0, precision=null이다. 빈 정답에 추가가 있으면
precision=0, recall=null이다. 여러 자료는 TP/FP/FN 원시 수를 합친 micro 지표로
집계하며 문서별 precision의 단순 평균과 구별한다.

## 자료 작성·검토

1. 실제 사용자 자료를 사용하지 않는다. 평가 대상 관계의 완전한 집합과 가정·부정·전이
   제외를 합성 원문에 명확히 쓴다.
2. 유효한 사전·명시 별칭·gold variants·금지 triple·정확 근거 위치를 작성한다.
3. 해시를 갱신하고 모든 모범 응답이 기존 생산 검증과 evaluator를 통과하는지 확인한다.
4. 원문과 정답·허용 표현·금지·인용 위치를 사람이 검토하고 실제 확인 증거와 상태를 남긴다.
   내용/규칙 변경 시 dataset/evaluator 버전 변경을 검토하고 이전 보고서와 구분한다.
5. 의도적 잘못된 인용과 기대 보정은 repair-cases에 분리한다. 보정 없이 올바른 인용을
   반환해도 정답이다. `ideal_outputs`는 별도 합성 oracle 생성 도구이며 evaluator가
   결과를 고쳐 정답으로 만드는 함수가 아니다.

[#727](https://github.com/semantic-reasoning/verinote/issues/727) 반복 도구는 같은 load_case/evaluate_case API와 운영 Service 결과를 사용한다.
저장소의 예시 입력·기계 결과는 [benchmarks/extraction](../benchmarks/extraction/README.md)에 있다.
