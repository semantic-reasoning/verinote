# 로컬 Ollama 순차 추출 기준선 계약

이 기준선은 Sonnet A/B 채택 판정과 별개다. `qwen2.5:7b`의 로컬 품질·시간·사용량을 관측하며 속도 향상, 운영 채택, cold/warm 또는 결정적 생성이라고 주장하지 않는다. v1/v2 Sonnet 원본과 임계값은 변경하지 않는다.

## 고정 조건

- loopback `http://127.0.0.1:11434`, Ollama `0.40.13`; 설치된 모델 `qwen2.5:7b`, digest `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`만 사용한다. 자동 설치·pull·다른 서버 접속은 하지 않는다.
- `num_ctx=32768`, `num_predict=4096`, `temperature=0`, `keep_alive=5m`, 동시성 1, 호출 timeout 최대 180초다. 실제 청크 및 전체 예산 잔여 시간에 따라 timeout을 줄인다.
- 새 v2 합성 short 1문서를 준비로 실행하고 three 3회, four 3회, eight 3회를 순서대로 실행한다. 총 10문서·46최초 호출, 청크당 1회/문서당 2회 인용 보정으로 최대 19보정·65전체 호출이다. 준비도 예산과 품질 검증에 포함한다.
- 전체 wall 관측 한도 3,600초, 입력+출력 관측 토큰 한도 2,500,000이다. 이미 시작한 호출은 종료·집계하므로 wall·토큰 hard cap은 아니다. 보정·최초 요청을 같은 ledger에 기록한다.
- 모든 문서는 새 Store와 production Service를 사용한다. 합성 사전을 실행용으로 승인하며 gold의 사람 검토와 구분한다. fact/page는 pending으로만 게시하고 실패 시 부분 게시하지 않는다.

## 중단·증거

실패, 미확인 입력/출력 사용량, 모델·digest·버전 변경, 품질 실패, 예산 도달, 코드 변경, 기록·정리 실패는 새 호출을 중단한다. 자동 재시도, 표본 교체, 추가 실험은 없다. 모델 내부 load/prompt/eval/total nanosecond timing은 API가 보고한 경우만 원본과 함께 보존한다. 로컬 USD와 cache 토큰은 unknown이며 0으로 대체하거나 Sonnet 비용 비율에 넣지 않는다.

첫 생성 전에 plan/environment/limits/prompts, fixture/gold manifest 및 코드·계약 사본/해시, CLI 인자, revision/tree, 설치 모델 digest와 Ollama 버전을 보존하고 검증한다. 각 호출은 `prompt.txt`, `response.json`, `run.json`, `usage.json`의 call ID·prompt hash·모델 identity로 연결한다. 실패 응답이 없으면 해당 사용량은 unknown이다. 호출 후 version/model 목록을 `identity-after.json`에 보존하고 변경 여부를 다시 확인한다. DB/checkpoint/repair audit/quality/timing도 보존한다. 원본 해시는 실수와 누락을 검사하며 서명이 아니다.

`evaluate`는 HTTP·모델·subprocess·DB 쓰기 없이 raw API 사용량, metadata, request prompt, checkpoint와 게시 DB를 다시 검증하고 저장된 verdict를 무시한다. metadata가 손상되면 inconclusive다. 뒤 문서가 손상되어도 검증된 연속 앞 문서의 품질/게시 실패를 유지한다. 미검증 뒷문서·전체회계는 성공 근거가 될 수 없다.

실제 전체 완료·품질/안전 통과는 `complete`, 확인된 품질/게시/추출 실패는 `failed`, 나머지는 `inconclusive`다. fake 테스트 응답은 항상 synthetic/inconclusive다. 문서 wall 시간은 새 Store 생성 직전부터 Service 종료·품질/원본 게시 검증까지이며 이후 artifact hash/보고는 전체 예산에만 포함한다. 최종 전체 원본 해시와 재평가 뒤 마지막 경과 시간을 관측하며, 그 뒤 최종 report/index/verdict 기록에는 작은 미관측 꼬리가 남는다. 완료한 3반복 자료군만 median/min/max를 보고하며 준비 시간을 섞지 않는다. 3회는 일반적 성능이나 꼬리 지연을 입증하지 않는다.

## 명령

모델 없는 계획:

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py plan
```

별도 승인한 고정 범위 실행은 깨끗한 checkout과 존재하지 않는 새 출력 경로를 사용한다:

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py run \
  --output benchmarks/extraction/results/ollama-qwen25-7b-baseline \
  --model qwen2.5:7b --max-requests 65 --max-seconds 3600 \
  --max-tokens 2500000 --allow-model-calls
```

모델 없는 재평가:

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_ollama.py evaluate \
  benchmarks/extraction/results/ollama-qwen25-7b-baseline
```

일반 사용자 저장소·설정·서버 재시작은 이 실험에 필요하지 않다. 추출 제공자 지원과 운영 적용은 별도 검증한다.
