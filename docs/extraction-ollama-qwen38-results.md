# qwen3.8:27b-mlx 실제 순차 진단 결과

**판정은 failed, 실행 일정은 미완료다.** 준비 문서는 Service 검증과 게시 안전성을 통과했지만 gold 품질이 실패했다. 새 v2 계약대로 이 실패를 기록하고 3청크 첫 반복으로 진행했으나, 첫 청크 생성이 180초 HTTP timeout에 걸려 중단됐다. 4·8청크는 미실행이며 정상 완료한 세 반복 집계는 없다.

## 고정 조건과 원본

- 실행 revision `d148c0d4bffd67a6ae2458101c45f03ce70211c2`, tree `a1f276c8733d3520c35152a7762cd321075b22aa`.
- Ollama `0.40.13`, 모델 `qwen3.8:27b-mlx`, 설치 digest `5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e`.
- [qwen38-v2 사전 계약](extraction-ollama-qwen38-contract.md): context32768·생성한도4096·temperature0·JSON·keep_alive5m·모델 기본 thinking·순차·호출timeout최대180초. 최대65생성 요청·14400초·관측입출력250만토큰이다. 별도의 145초 탐색 표본은 재사용하지 않았다.
- [environment/argv/코드·모델 해시](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/environment.json), [plan](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/plan.json), [report](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/report.json), [index](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/index.json), [평가](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/evaluation.json).
- report SHA-256 `93bcebcb870385b42807617b6d552920bdcbf7f7576643970ce1396d7d96db20`. 보존 원본은 76파일·1,423,608바이트다.

## 관측과 완료 상태

| 항목 | 결과 |
|---|---:|
| 생성 시도 / 인용 보정 | 2 / 0 |
| 로컬 종료 기록 / pending | 2 / 0 |
| 사용량 미확인 호출 | 1 |
| 확인된 입력 / 출력 토큰 | 1,581 / 3,234 |
| 확인된 토큰 하한 | 4,815 |
| 전체 예산 경과 관측 | 317.876468초 |

**4,815는 전체 소비량이 아닌 확인된 하한이다.** 두 번째 호출은 응답 본문과 토큰 사용량을 받지 못했으며 실제 resolved response model도 unknown이다. 호출 후 설치 digest/version 확인이 누락 응답의 사용량이나 모델 확인을 대신하지 않는다. 출력 보고량에는 추론이 포함될 수 있고 최종 답변 길이가 아니다. cache·USD는 unknown이다.

| 문서 | 시도 / Service 완료 / gold 통과 | Service 시간 | 문서 준비–종료·평가 | 검증 청크 | 후보 |
|---|---|---:|---:|---|---|
| short 준비 | 1 / 1 / 0 | 137.521962초 | 137.579831초 | 1/1 | pending fact1·page1 |
| three 첫 반복 | 1 / 0 / 0 | 180.065788초 | 180.115152초 | 0/3 | 없음 |
| four | 0 / 0 / 0 | 미측정 | 미측정 | 미측정 | 없음 |
| eight | 0 / 0 / 0 | 미측정 | 미측정 | 미측정 | 없음 |

준비 호출의 API total/load/prompt-eval/output-eval은 각각 137.416959 / 2.413526 / 9.111458 / 125.889032초다. timeout 호출 wrapper는 180.005043초다. 이 시간 구간들은 겹치므로 합산하지 않는다. 전체 해시·재평가 뒤 마지막 경과를 관측했고 최종 report/index/verdict 기록에는 계약에 명시한 작은 미관측 꼬리가 있다.

`pending=0`은 로컬 ledger에 미완료 시도가 없다는 뜻이며 timeout 이후 서버 계산 취소를 보장하지 않는다. 모델/cache 거주 상태는 통제하지 않았으므로 cold/warm이라고 단정하지 않는다.

## 품질과 중단 이유

준비 응답은 `calls`, `archives`, `mirrors`의 coverage 행을 모두 반환했고 production 검증을 완료했다. 직접 호출 사실과 인용은 정답에 부합했지만, 유한 gold 대조에서는 부정·조건·시점·미등록 개체·소유 관계 등의 필수 claim 근거, coverage 의미/참조, gap 근거·완전성이 실패했다. **행이 모두 존재하는 것과 그 판단이 정확한 것은 다르다.** 해당 문서의 후보2개는 pending일 뿐 gold 수용이나 사람 승인이 아니다.

[준비 원본 응답](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/short-1/runs/53bbee4f3bfc44f09fd768e1f35cd8e7/1/response.json), [준비 사용량](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/short-1/runs/53bbee4f3bfc44f09fd768e1f35cd8e7/1/usage.json), [준비 DB](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/short-1/state.sqlite3)로 확인할 수 있다. 합성 실행용 승인 사전은 gold 사람 검토와 구분하며 fixture의 human_review 상태는 pending이다.

그 뒤 예정된 three-1을 시작했으므로 준비의 gold 실패가 전체 실행을 즉시 중단한 것은 아니다. [첫 청크 실행 기록](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/three-1/runs/01b081304a474a8083a54380b2131d4c/1/run.json)과 [사용량 기록](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/three-1/runs/01b081304a474a8083a54380b2131d4c/1/usage.json)은 timeout/error와 unknown 값을 보존한다. [3청크 문서 DB](../benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline/documents/three-1/state.sqlite3)에는 부분 fact/page 후보가 없었다. 두 문서 모두 publication safety 검사는 통과했다.

최종 이유는 `quality:short-1`, `extraction_failed:three-1`, `incomplete_baseline`이며 `execution_complete=false`다. three/four/eight의 정상 완료 반복은 각각0회이므로 세 반복 latency 중앙값/범위는 모두 null이다. 품질 실패를 통계에서 제거해서 성공처럼 만들지 않았으며, 표본 교체·재시도·모델 교체·timeout 변경도 하지 않았다. Sonnet A/B 수용, 속도 개선, 모델의 일반적 우월성이나 완료한 성능 기준선을 주장하지 않는다.

## 모델 없는 재현과 과거 호환성

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py evaluate \
  benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py evaluate \
  benchmarks/extraction/results/ollama-qwen25-7b-baseline
```

현재 verifier가 HTTP/subprocess/DB 쓰기 없이 두 계약을 해석해 각각 저장된 failed 판정을 정확히 재현했고, 원본 해시가 변하지 않았다. 보존 구현 소스를 실행하거나 현재 코드와 실행 당시 코드가 같다고 가정하지 않는다. [기존 qwen2.5 보고서](extraction-ollama-results.md)와 67개 과거 원본은 변경하지 않았다. 해시는 누락·변조 탐지용이며 서명이 아니다.

계약 충실도 후속 진단은 [#781](https://github.com/semantic-reasoning/verinote/issues/781)에서 추적한다. 더 긴 요청 deadline이나 thinking 설정 등 새 조건을 평가하려면 이 timeout을 보존한 채 별도 사전 계약으로 구분해야 한다. 이 결과로 기존 Sonnet #768, 운영 #756 또는 상위 #748을 완료하지 않는다.
