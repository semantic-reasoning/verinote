# 추출 충실도 다음 후보 중단 결정

**[#795](https://github.com/semantic-reasoning/verinote/issues/795)는 새 versioned 후보를 만들지 않고 중단한다.**
중단은 사람이 정했다(아래 '사람의 결정').
나머지 절의 정리와 해석은 에이전트가 썼고(`agent_proposed`), `human_review`는 pending이다.
`tests/test_extraction_fidelity_stop_decision.py`가 이 문서를 검사한다.

## 사람의 결정

Claude Code 세션의 AskUserQuestion 질문과 답을 에이전트가 옮겨 적었고, 답의 원본은 그 세션 기록이다.
에이전트도 사용자 계정으로 커밋하고 댓글을 쓰므로, 커밋이나 GitHub의 작성자 표시는 이 답이 사람의 것이라는 증명이 아니다.

- 질문: "#795: evidence_links_v1이 기각된 뒤, 다음 추출 충실도 후보를 진행할까요, 아니면 근거 있는 중단 결정을 남길까요?"
- 답: "중단, #756·#748도 닫기"
- 답이 세션 기록에 남은 시각(UTC): 2026-11-03T05:35:41.699Z
- 고른 선택지의 설명(에이전트 작성): "#795에 중단 사유를 기록하고 결정으로 닫습니다. #756(운영 적용)은 채택 후보가 없어 진행하지 않는 것으로, #748(우산)은 '개선 미달·중단 사유 기록'으로 닫습니다."
- 같은 답의 #794 결정 3: "(a) v2 gold만 (Recommended)"
- 같은 답의 #794 결정 1·2: "둘 다 보류 (Recommended)"

`(Recommended)`는 에이전트가 선택지에 붙인 표시다.

## 남은 오류와 원인

[#785 평가 보고서](../benchmarks/extraction/results/785-evidence-links-v1-assessment/report.json)의 SHA256은 `45adc7b1ee6046d5842cd09f875d8fecbdeee0d7ddb688adbd7d29fca6beea24`이다.
보고서의 `trial.limits.repetitions`는 1이고, 판정은 `rejected`, 사유는 `finite_gold_failed`다.
남은 v2 annotation 오류는 청크 1의 `archives` coverage(`coverage_semantics`)와 청크 1·3의 `gap_completeness`다.
청크 1의 `archives` coverage 행은 과거 기록 claim을 연결했지만 status가 `no_evidence`이고, v2 gold의 status는 `not_representable`이다.
보고서 `v1.mapping`은 오류마다 원인을 하나씩 적는다: `c1:predicate:archives:D6`, `c1:historical:P-GAP-QUALIFIER-DUP`, `c3:historical:P-GAP-QUALIFIER-DUP`.
위 원인의 category는 `unresolved`이고, 원인이 된 v1 실패의 `obligation_ids`는 `historical`이다.
보고서 `v1.sensitivity`에서 이 오류를 옮긴 정책은 P-STATUS-HISTORICAL·P-GAP-QUALIFIER-DUP이고, 보존 기준 사본에서 그 `human_review`는 pending이다.
보고서 `v1`의 판정은 결정적 층 `fail`, 전체 `fail`이고, 시험 뒤에 고른 정책 조합(`v1.post_hoc_combinations`)의 판정은 결정적 층 `pass`, 전체 `undecided`다.
이 조합은 시험 뒤에 골랐으므로 채택 근거가 아니다(#795).
위 #794 답 (a)에서는 v2 유한 gold가 채택을 좌우한다(에이전트 읽기).

## #768 원인과의 관계

annotation 오류는 #768 실행이 16개, 이 시험이 3개다.
보고서 `comparison_768`에서 시험에만 있는 원인(`causes_only_trial`)은 없고, `trial_causes_within_768`은 `true`다.
`historical:P-GAP-QUALIFIER-DUP` 원인은 #768 실행에서 청크 1·2·3에, 이 시험에서 청크 1·3에 있다.
`predicate:archives:D6` 원인은 #768 실행에서 청크 1·2에, 이 시험에서 청크 1에 있다.
이 비교만으로는 줄어든 원인을 후보 지시의 효과로 보지 않는다(에이전트 판단, 아래 분기 절).

## 같은 시험 안의 청크 분기

저장소 루트에서 다음 명령은 청크마다 청크 번호, 숫자열을 `#`로 바꾼 prompt.txt의 SHA256 앞 12자, `qualifier_not_representable` gap의 `claim_indices`를 출력한다.

```sh
.venv/bin/python -c "
import glob,hashlib,re
from verinote.agents import parse_response; from verinote.usage import decode_transport
for d in sorted(glob.glob('benchmarks/extraction/results/785-evidence-links-v1-trial/documents/three-1/runs/*/[0-9]')):
  p=open(d+'/prompt.txt',encoding='utf-8').read(); o=parse_response('claude',decode_transport('claude',open(d+'/stdout.txt',encoding='utf-8').read())[0])[0]
  print(d[-1], hashlib.sha256(re.sub('[0-9]+','#',p).encode()).hexdigest()[:12], [g['claim_indices'] for g in o['dictionary_gaps'] if g['code']=='qualifier_not_representable'])"
# 1 57408d26b2dc [[3]]
# 2 b19589b37a07 [[3], [4]]
# 3 b19589b37a07 [[3]]
```

청크 2·3의 prompt.txt는 숫자열을 `#`로 바꾸면 같고, 청크 1은 다르다.
청크 2는 과거 기록 claim(인덱스 4)을 `qualifier_not_representable` gap에 연결했다.
청크 3은 같은 인덱스의 과거 기록 claim을 `archives` coverage 행에 연결했지만 gap에는 연결하지 않았다.
과거 기록 claim을 이 gap에 연결하지 않은 청크는 청크 1·3이고, v2 `gap_completeness` 오류가 남은 청크와 같다.
같은 실행에서 숫자만 다른 입력의 출력이 갈렸으므로, 실행 1회의 통과·실패를 prompt 변경의 효과로 볼 수 없다(에이전트 판단).

## 기존 template 지시

후보 모듈 `verinote/diagnostics/extraction_fidelity_candidate.py`의 `TEMPLATE`에 다음 지시가 있고, 시험의 청크별 prompt.txt에는 들어 있으며 original-prompt.txt에는 없다.

- `직접 근거가 없는 no_evidence와 근거는 있으나 승인 정의로 의미를 보존할 수 없는 not_representable을 구분하세요`
- `각 dictionary_gaps 항목의 claim_indices는 해당 code의 제약을 직접 뒷받침하는 인용에 연결하세요`
- `조건·시점 제약은 실제 의미에 맞는 code로 구분하세요`

이 지시가 남은 의무(`archives` coverage 상태, 과거 한정어 gap)에 해당한다는 것은 에이전트 판단이다.
남은 오류에 맞춘 문구를 더하면 사례 `three`에 맞추는 변경이 될 수 있다(에이전트 판단).

## 결정의 범위

사람의 답에 따라 #756은 채택 후보가 없어 진행하지 않는 것으로, #748은 개선 미달·중단 사유를 기록하고 닫는다.
이 결정은 #748과 #756의 성공 완료가 아니다.
이 결정은 #785의 시험 원본·평가 보고서, v2 gold와 후보 모듈을 고치지 않는다.
다음 후보를 다시 시작하려면 새 이슈가 #795의 계약·예산 승인·held-out 또는 과적합 방지 조건을 갖춰야 한다(에이전트 제안).
