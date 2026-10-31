# Ollama fidelity 후보의 단일 short 평가 계약

이 도구는 `ollama_fidelity_checklist_v1` 후보를 v2 합성 short 한 문서로 평가한다. 통과는 추가 시험의 근거일 뿐 자연어 의미의 완전 검증, 운영 채택, 성능 향상 또는 Sonnet A/B 채택이 아니다. 기존 원본·gold·품질 기준은 변경하지 않는다.

## 고정 입력과 예산

- 모델 `qwen3.8:27b-mlx`, digest `5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e`, Ollama `0.40.13`, loopback `http://127.0.0.1:11434`만 사용한다. 자동 pull·서버 변경은 없다.
- `num_ctx=32768`, `num_predict=4096`, `temperature=0`, `keep_alive=5m`, thinking은 모델 기본값이다. 호출 timeout 최대 180초이며 잔여 예산으로 줄인다.
- 새 Store의 production Service에 short 한 문서를 등록한다. 최초 1요청과 기존 인용 보정 최대 1요청만 허용한다. 총 2요청, wall 관측 600초, 입력+출력 관측 100,000토큰이다. 이미 시작한 요청을 종료·집계하므로 wall/토큰 hard cap은 아니다.
- 후보는 명시적 opt-in이며 최초 추출 prompt에만 적용한다. 사전 생성·질의·인용 보정의 prompt나 생성 옵션은 바꾸지 않는다. 새 의미를 합성하거나 모델 응답을 의미적으로 보정하지 않는다.
- 합성 사전은 실행용으로 승인한다. gold 사람 검토의 pending 상태와 구분한다. fact/page는 pending으로만 게시하며 실패 시 부분 게시가 없어야 한다.

## 증거와 판정

첫 생성 전에 모델/version/options, candidate ID와 template hash, 원본 production/repair/template 텍스트, fixture/gold manifest, plan/limits, 코드 전체 Python 모듈·스크립트·계약 사본과 해시, argv·revision/tree를 고정한다. 실제 요청은 `original-prompt.txt`와 `prompt.txt`, `run.json`의 원본/효과 prompt 해시로 확인한다. 보정은 변환하지 않아 original 파일을 만들지 않는다. 각 API `response.json`, `usage.json`, call ID 및 호출 후 version/model identity를 보존한다. cache 토큰과 USD는 unknown이며 0으로 대체하지 않는다.

raw 응답을 같은 production validator와 기존 quote-repair 규칙으로 다시 검사한다. 기존 기계적 fact_indices 보정이 있으면 원본·정규화 hash와 call ID에 연결된 coverage-repair audit도 검증한다. DB/checkpoint/prompt compiler/repair audit/게시 상태/유한 gold를 연결한다. 저장 verdict는 무시한다. offline `evaluate`는 HTTP·subprocess·DB 쓰기를 하지 않으며 보존된 Python 소스를 실행하지 않는다. 현재 도구·계약·fixture가 고정 원본과 다르면 inconclusive로 두므로 같은 revision에서 재평가한다. 해시는 누락·손상을 탐지하지만 서명은 아니다.

- `accepted_for_further_testing`: 단일 전체 production 처리·유한 gold·게시 안전성 통과, 모든 호출의 모델/digest/사용량 확인, 완료된 예산 회계.
- `rejected`: 완전하고 확인 가능한 응답의 production 계약 또는 유한 gold 실패, 혹은 확인된 게시 안전 위반.
- `inconclusive`: timeout, unknown usage/identity, 미완료 보정, 예산·기록·원본 검증 실패 등. fake 응답은 항상 synthetic/inconclusive다.

실패·미확인 사용량/identity·예산 소진·코드 변경·기록/정리 실패에 새 호출을 중단한다. 자동 재시도·표본 교체·다른 모델·추가 실행은 없다. 이 한 문서로 일반적 회귀나 성능을 판정하지 않는다.

## 명령

모델 없는 계획과 재평가:

```sh
PYTHONPATH=. .venv/bin/python scripts/evaluate_ollama_fidelity.py plan
PYTHONPATH=. .venv/bin/python scripts/evaluate_ollama_fidelity.py evaluate /path/to/retained-result
```

별도 승인한 고정 범위 실행은 깨끗한 checkout과 존재하지 않는 새 출력 경로를 사용한다:

```sh
PYTHONPATH=. .venv/bin/python scripts/evaluate_ollama_fidelity.py run \
  --output benchmarks/extraction/results/781-fidelity-short \
  --model qwen3.8:27b-mlx --max-requests 2 --max-seconds 600 --max-tokens 100000 \
  --allow-model-calls --allow-fidelity-candidate
```
