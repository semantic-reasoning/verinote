# #781 Ollama 추출 계약 충실도 진단

두 보존 실패는 서로 다르다. qwen2.5:7b는 production coverage 구조 검증에 실패했고, qwen3.8:27b-mlx의 준비 응답은 production을 통과했지만 유한 gold의 claim/coverage/gap 근거 대조에 실패했다. 그 이후의 qwen3.8 3청크 HTTP timeout은 별도 전송/시간 관측이다.

## 모델 없는 재생

```sh
PYTHONPATH=. .venv/bin/python scripts/diagnose_ollama_fidelity.py replay
```

진단은 저장 report의 알려진 SHA-256, 전체 원본 index와 기존 baseline evaluator를 먼저 검사한다. 첫 short 응답 원본을 변경하지 않고 현재 production과 같은 순서로 비coverage preflight, 기존 fact_indices 기계적 보정(복사본), 엄격한 quote/coverage/사전/위키 검증을 재생한다. 보정 내역은 별도로 반환하며 관계별 판단·누락 coverage 행을 생성하지 않는다. 추가 모델 인용 보정이 필요하면 그 필요만 기록하고 실행하지 않는다. production을 통과한 경우만 기존 v2 finite-gold 평가를 실행한다. HTTP·모델·subprocess·DB 쓰기 없이 실행하며 모든 파일 해시가 유지돼야 한다. 손상된 원본/지원하지 않는 실험은 거부한다. 보존 구현 코드를 실행하지 않는다.

| 보존 응답 | production | finite gold | 확인된 결론 |
|---|---|---|---|
| qwen2.5:7b | 거부 | 후속 의미 평가는 수행하지 않음 | 승인 calls/archives/mirrors 중 coverage가 calls 한 행뿐 |
| qwen3.8:27b-mlx | 통과 | 실패 | 필수 부정·조건·과거·미등록 개체·미등록 관계 주장 근거, coverage 상태/참조, gap 근거/완전성 실패 |

[기존 qwen2.5 결과](extraction-ollama-results.md)와 [qwen3.8 결과](extraction-ollama-qwen38-results.md)의 raw/DB/usage/index를 그대로 사용한다. qwen3.8의 직접 호출 fact와 인용이 맞았다는 사실이 나머지 표현 제약의 완전성을 증명하지 않는다. qwen2.5의 mirrors를 missing_relation으로 설명한 내용 등은 첫 구조 오류와 구분되는 raw 관찰이며, 구조 거부 뒤 의미 평가가 모두 완료됐다고 주장하지 않는다.

## 회귀 범위

`tests/test_ollama_fidelity.py`는 실제 production과 변경하지 않은 evaluator를 호출한다. 승인 관계 각각의 누락/중복/미등록 관계, 범위 밖 참조, 모든 필수 주장의 근거 교체, 각 gap의 개별 근거 누락, 잘못된 gap code/무관한 근거, 승인 관계를 missing_relation으로 오분류하는 경우, no_evidence와 not_representable 혼동, 부정/조건/과거를 무조건 사실로 바꾸는 경우, 모호한 인용을 거부하는지 검사한다. 정답 외에도 claims/coverage/gaps를 재정렬하고 참조를 일관되게 바꾼 유효 응답이 통과한다.

이 검사는 유한 fixture와 명시된 근거 구간을 대조하며 임의 자연어 설명문의 논리적 함의를 일반적으로 판정하지 않는다. 긍정 oracle 통과나 JSON 구조 보장은 실제 모델 개선의 증거가 아니다.

## 후보 방향과 남은 평가

기존 응답에 coverage를 임의로 보충하거나 validator/gold를 완화하지 않는다. 다음 후보는 기존 생성 설정과 JSON 형태를 유지하면서 실제 승인 관계 목록을 명시하고, 조건·시점·부정·어휘 제약 주장과 gap 근거를 따로 점검하도록 유도하는 opt-in prompt다. 후보 ID/template hash와 실제 prompt를 고정해 기존 작업/checkpoint와 구분해야 한다. 이 진단 단계는 새 후보의 실제 품질을 평가하지 않았으며, 이 기존 실패 재보고만으로 #781을 완료하지 않는다.
