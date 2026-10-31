# Ollama qwen2.5:7b 실제 추출 기준선 결과

**준비 문서에서 기준선 실패(`failed`). 3·4·8청크 측정은 미실행이다.** 로컬 API는 완성된 JSON 응답을 반환했지만, production 추출 검증에서 승인 관계 coverage 누락을 발견해 계획대로 첫 문서 뒤에 중단했다. 부분 후보는 게시하지 않았다. 이 결과는 모델의 전반적 성능이나 Sonnet A/B 채택 결과를 뜻하지 않는다.

## 실행과 원본

- 실행 revision `414e338fd7fd8e5a44b7d25287d6fd5485f8c94d`, tree `1b7780727d725bb1682e0ec2bb598cdaf295de35`.
- Ollama `0.40.13`, 요청/응답 모델 `qwen2.5:7b`, 설치 digest `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`. 호출 전후 version/digest를 확인했다.
- 고정 옵션: context 32,768·최대 생성 4,096·temperature 0·JSON·keep_alive 5m·순차·요청 timeout 최대 180초.
- [사전 계약](extraction-ollama-baseline-contract.md)의 10문서·46최초/최대65전체 요청·3,600초·관측250만토큰 계획을 유지했다. 준비 실패 뒤 반복·교체·다른 모델 호출은 하지 않았다.
- [environment/argv/해시](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/environment.json), [plan](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/plan.json), [report](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/report.json), [index](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/index.json), [판정](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/evaluation.json)을 원본 그대로 보존한다.
- report SHA-256: `731ce57244b8139c870f2d13d2c3745ac9fc512c122743970d93d849f8b659cb`. 전체 증거는 67파일·1,125,927바이트다.

## 관측 결과

| 항목 | 결과 |
|---|---:|
| 실제 생성/보정 요청 | 1 / 0 |
| 입력/출력 토큰 | 1,868 / 262 |
| 관측 합계 토큰 | 2,130 |
| 진행 중/사용량 미확인 호출 | 0 / 0 |
| API total | 12.464584초 |
| API load / prompt eval / output eval | 2.300793 / 2.956356 / 7.193852초 |
| Service 접수–종료 | 12.548954초 |
| 문서 준비–종료·평가 | 12.609924초 |
| 전체 예산 경과 관측 | 12.767860초 |
| 검증 완료 청크/준비 문서 청크 | 0 / 1 |
| 게시된 fact/page 후보 | 0 |

시간 구간들은 겹치므로 합산하지 않는다. 최종 전체 해시·재평가 뒤 경과 시간을 관측했고, 마지막 report/index/verdict 기록에는 계약에 명시한 작은 미관측 꼬리가 있다. cache·USD는 unknown이며 로컬 전력/하드웨어 비용을 0으로 간주하지 않는다. 처음 요청의 load 시간만으로 통제된 cold 조건을 주장하지 않는다.

## 실패와 보호 동작

[실제 응답](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/runs/051f919193494e8cbca550e692cefebb/1/response.json)은 `calls` 사실 1개와 coverage 행 1개를 반환했다. 승인 사전에는 `calls`, `archives`, `mirrors` 세 관계가 있어 각 관계의 판단 행이 필요하다. production 검증이 `승인 관계마다 정확히 한 행이 필요합니다`로 응답을 거부했다. API의 `done=true`, `done_reason=stop`은 추출 계약 통과와 별개다.

raw 사실 1개를 반환한 것과 승인 가능한 추출을 완료한 것은 구분한다. 검증 완료 청크는 0개이고, [문서 DB](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/state.sqlite3)에는 합성 실행용 승인 사전만 남았으며 fact/page 후보는 없다. gold의 사람 검토 상태는 pending 그대로다. 추가 raw 관찰로 이미 승인된 `mirrors`를 missing_relation이라고 설명하고, 조건·시점 등의 필수 주장을 빠뜨린 정황도 있다. 이는 최초 coverage 오류와 구분되는 후속 진단 대상이다.

판정은 `extraction_failed:short-1`, `incomplete_baseline`이다. three/four/eight의 세 반복은 각각 0회로 중앙값·범위는 null이다. 속도 개선이나 품질 비열등, 완료한 성능 기준선을 주장하지 않는다.

## 모델 없는 재현

같은 실행 코드의 checkout에서 다음을 실행한다. HTTP·모델·subprocess·DB 쓰기를 금지한 상태에서도 저장 판정과 정확히 같았고 파일 해시가 변하지 않았다.

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py evaluate \
  benchmarks/extraction/results/ollama-qwen25-7b-baseline
```

[요청문](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/runs/051f919193494e8cbca550e692cefebb/1/prompt.txt), [호출 실행](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/runs/051f919193494e8cbca550e692cefebb/1/run.json), [사용량](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/runs/051f919193494e8cbca550e692cefebb/1/usage.json), [호출 후 identity](../benchmarks/extraction/results/ollama-qwen25-7b-baseline/documents/short-1/runs/051f919193494e8cbca550e692cefebb/1/identity-after.json)으로 관측을 추적할 수 있다. 원본 해시는 누락·변조 탐지용이며 서명이나 모델 의미의 완전한 판정이 아니다.

다음 단계는 이 보존 응답을 offline로 재현해 계약 누락 원인을 진단하고, 명시적인 새 후보와 실험 계약을 고정하는 것이다. 기존 validator를 완화하거나 누락 coverage를 임의 생성하지 않으며, 중단한 실험을 자동 재개하지 않는다. 기존 Sonnet #768과 운영 적용 #756 및 상위 #748은 이 결과로 완료하지 않는다.
