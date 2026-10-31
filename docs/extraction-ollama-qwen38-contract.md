# Ollama qwen3.8:27b-mlx 순차 진단 계약 v2

이 계약은 PR #780의 새 기준이다. 이전 qwen2.5:7b v1 계약·67개 원본·실패 판정은 그대로 보존한다. 이전에 별도로 실행한 약145초짜리 short 탐색은 모델 선택과 시간 예산의 참고이며, 이번 준비 표본이나 반복 표본으로 재사용하지 않는다.

## 고정 조건과 표본

- profile `qwen38-v2`, schema version2, Ollama `0.40.13`, 모델 `qwen3.8:27b-mlx`, digest `5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e`.
- loopback `127.0.0.1:11434`, context32768·생성한도4096·temperature0·JSON·keep_alive5m·동시성1·호출timeout최대180초. think를 override하지 않아 모델 기본 추론 정책을 사용한다. 출력 토큰은 Ollama 보고량이며 추론을 포함할 수 있고 최종 답변 길이가 아니다.
- v2 short 준비1회 이후 three→four→eight를 각3회 실행한다. 모든 문서는 독립 새 Store와 합성 실행용 승인 사전을 사용한다. 총10문서·46최초 요청·최대19인용보정, 최대65생성 요청이다. 표본 교체나 자동 재시도·모델 교체는 없다.
- 전체 wall 관측 한도14400초(4시간), 관측 입력+출력2500000토큰. 탐색의144.96초를46회에 단순 확대하면 약6668초다. 최대65회×180초=11700초에 준비·검사·기록·정리 여유2700초를 둔 계획이며 실행시간 예측이나 완료 보장은 아니다. 토큰 가정은46×(24000+4000)+19×(40000+4000)=2124000에376000 여유다. 이미 시작한 호출 때문에 시간·토큰 한도를 초과 관측할 수 있다.

## 실패와 계속 조건

**Service 성공·완전한 호출/문서 증거·게시 안전성 통과 상태에서 유한 gold만 실패하면**, 그 실패를 기록하고 다음 예정 문서로 진행한다. 준비 문서에도 같은 규칙을 적용한다. 이는 이전 v1의 품질 실패 즉시 중단을 바꾼 새 진단 계약이며, 기존 실패를 재해석하지 않는다.

Service/domain 추출 오류, transport 오류·timeout, 미확인 usage/model/digest/version, 안전하지 않은 게시, 코드 변경, 불완전/손상 증거, 기록/정리 실패와 예산 소진은 신규 호출을 중단한다. 기존 production validator와 승인 관계 coverage·claim/gap·정확 인용·문서 전체 게시 조건을 완화하지 않는다. 모든 게시 후보는 pending이며 자동 승인하지 않는다. 중단 이후 이미 시작한 요청은 종료·집계한다.

문서별 Service 완료와 gold 통과를 별도로 공개한다. 자료별 attempted·Service-completed·gold-passed 수를 함께 표시하며, 세 반복 모두 Service 완료·게시 안전이면 **gold 실패도 포함한 전체 실행 시간**의 중앙값/최소/최대를 표시한다. 이는 부적합한 출력의 실행 시간을 포함할 수 있다. 품질 통과 표본만 골라 빠른 결과로 제시하지 않는다. 확인된 gold 실패가 하나라도 있으면 전체 판정은 failed이며, 일정 완료와 품질 통과를 구분한다.

## 원본과 재검증

첫 생성 전에 모델/digest/버전/옵션·plan·fixture/gold·prompt·repair prompt·코드 사본과 해시·revision/tree·실제 argv를 고정한다. 매 호출 뒤 identity 관측을 남기며 raw API·사용량·timing·문서DB·checkpoint·게시 상태를 보존한다. cache·USD는 unknown이다. 임의의 현재 소스와 실행 당시 소스가 같다고 주장하지 않는다.

`evaluate`는 지원하는 v1/v2 계약의 전체 identity·옵션·표본·예산·prompt·fixture 및 보존한 구현 파일 목록/해시를 현재 verifier로 검증한다. 보존 소스를 실행하지 않고 현재 소스와 과거 소스의 바이트 동일성을 요구하지 않는다. 알 수 없거나 일관되지 않은 profile은 inconclusive다. qwen2.5 역사적 원본의 동일 failed 판정을 회귀 검사한다. 원본 해시는 서명이나 악의적 전체 위조 방지가 아니다.

문서 경과에는 준비부터 Service 종료·품질/게시 검증까지를 포함한다. artifact hashing/보고는 전체 예산에 포함한다. 최종 전체 해시·재평가 후 마지막 경과를 관측하며 최종 report/index/verdict 기록에는 작은 미관측 꼬리가 남는다. provider cache는 통제되지 않았으므로 cold/warm이라고 단정하지 않는다. Sonnet A/B 또는 성능 개선/운영 채택 결과로 해석하지 않는다.

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py plan
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py run \
  --output benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline \
  --model qwen3.8:27b-mlx --max-requests 65 --max-seconds 14400 \
  --max-tokens 2500000 --allow-model-calls
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py evaluate \
  benchmarks/extraction/results/ollama-qwen38-27b-mlx-baseline
```
