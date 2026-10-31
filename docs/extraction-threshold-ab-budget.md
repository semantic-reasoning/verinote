# 청크 임계 A/B v2 실측 예산 제안

상태: **승인 대기·실모델 미실행**. #768의 고정 계약을 실행하기 위한 예산 제안이며 모델 호출 승인이 아니다. 실제 실행은 별도 승인 후 깨끗한 checkout에서 수행한다. 기존 v1 결과와 기준을 변경하지 않는다.

## 승인 요청 범위

| 항목 | 제안 |
|---|---|
| 제공자·요청 모델 | Claude CLI, `sonnet` |
| 실제 모델 조건 | 모든 호출에서 버전이 식별되는 같은 Sonnet; 미확인·변경 시 중단 |
| 자료 | v2 합성 short/three/four/eight만, 독립 새 저장소 |
| 문서 | 측정 120개와 준비 8개, 총 128개 |
| 최초·준비 요청 | 608회 |
| 보정 | 청크당 1회·문서당 2회, 최대 248회 |
| 전체 요청 한도 | 856회 |
| 전체 wall 관측 한도 | 14,400초(4시간) |
| 입력+출력 관측 토큰 한도 | 30,000,000토큰 |
| 요청 timeout | 90초 |

실패·미확인 사용량/비용/모델·worker 변경·기록/정리 실패·예산 도달 시 새 호출을 중단한다. 이미 시작한 호출은 종료·집계하므로 wall·토큰 한도의 초과 관측이 가능하다. 금액 상한이나 토큰 hard cap이 아니다. 자동 재시도·표본 교체·추가 실험은 포함하지 않는다. #756의 운영 재측정도 이 승인에 포함하지 않는다.

## 입력·출력 가정과 근거

현재 v2 자료에 production `bounded_prompt`와 `dictionary_context`를 적용한 오프라인 크기는 아래와 같다. UTF-8 바이트 수이며 토큰 수가 아니다. 문서 filename은 자료명+.txt, dictionary payload만 포함한 수치로 run ID·revision 등 메타데이터와 CLI의 별도 문맥은 제외했다. production 형식의 합성 Store pin까지 포함하면 최대 약 24,138바이트였으며 실제 실행 manifest는 prompt 템플릿·해시·환경을 고정하고, 완성된 요청문은 호출별 원본에 보존한다.

| 자료 | 최초 prompt UTF-8 바이트/청크 | oracle 응답 UTF-8 바이트/청크 |
|---|---:|---:|
| short | 6,408 | 2,111 |
| three | 23,682–23,777 | 2,111 |
| four | 23,722–23,817 | 2,111 |
| eight | 23,887–23,982 | 2,111 |

[보존된 v1 관측](extraction-sonnet-ab-results.md)의 137호출은 입력 합계 825,623·중앙값 4,992·최대 7,784토큰, 출력 합계 55,255·중앙값 372·최대 681토큰이었다. v2는 긴 source와 더 많은 coverage/gap 설명을 요구하므로 이 관측을 그대로 확대하지 않는다. v1은 보정 0회여서 보정 비용·시간의 실측 근거는 없다.

608회 각각 입력 24,000·출력 4,000토큰을 계획 가정으로 잡는다. 보정 248회는 원래 source·전체 사전·issues를 포함하므로 입력 40,000·출력 4,000으로 잡는다. 실제 tokenizer 계산이나 출력 제한값이 아니며 byte 크기로부터 정한 보수적인 예산 가정이다. CLI 문맥·cache 계상·모델 응답 길이에 따라 달라질 수 있다.

- 최초·준비: `608 × (24,000 + 4,000) = 17,024,000`
- 최대 보정: `248 × (40,000 + 4,000) = 10,912,000`
- 합계 가정: `27,936,000`
- 관측 한도까지 여유: `30,000,000 − 27,936,000 = 2,064,000`

실제 출력은 oracle와 동일하다고 가정하지 않는다. 이 수치는 예상 청구액 또는 사용량 예측이 아니라 미측정 v2를 위한 관측 중단 예산이다. CLI reported USD를 원본에서 집계하며 추정 단가로 대체하지 않는다.

## 시간 가정

v1 호출 wrapper 시간은 중앙값 3.367초·선형 보간한 95백분위 4.768초·최대 5.034초, 합계 474.270초였고 전체 실험 wall은 399.057초였다. 동시 호출의 합과 전체 wall은 서로 다르다. v2 성능은 아직 측정하지 않았다.

계획상 최대 856회에 각 15초를 배정한 직렬 계산은 12,840초다. 새 저장소·owner 준비, 평가, 원본 해시·보고, 종료·정리에 1,560초를 더해 14,400초를 제안한다. 15초와 1,560초는 측정값이 아닌 여유 가정이며 병렬 속도 향상을 예산에 미리 반영하지 않았다. 모든 호출이 timeout까지 소요되면 `856 × 90 = 77,040초`이므로 4시간에 전체 표본을 완료한다는 보장은 없다. 부족하면 중단·미완료를 기록하며 임의 축소나 추가 실행으로 보충하지 않는다.

## 실행과 재평가

승인 후 실행할 명령은 다음과 같다. 출력 경로가 이미 존재하면 다른 새 경로를 정하고 기록한다. 첫 호출 전에 plan·environment·CLI 버전·실제 argv·code revision/tree·fixture/gold/prompt/repair prompt 해시를 보존하고 검증한다.

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_threshold_ab.py run \
  --output benchmarks/extraction/results/768-threshold-v2-sonnet \
  --model sonnet --max-requests 856 --max-seconds 14400 \
  --max-tokens 30000000 --allow-model-calls
```

모델 없는 재평가:

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_extraction_threshold_ab.py evaluate \
  benchmarks/extraction/results/768-threshold-v2-sonnet
```

[고정 계약](extraction-chunk-threshold-ab-contract.md)과 [실행·증거 한계](extraction-threshold-ab-runner.md)를 적용한다. 합성 사전의 실행용 승인은 gold의 사람 검토와 다르며 manifest의 사람 검토 상태는 pending이다. 이 도구의 유한 평가를 임의 자연어 의미의 완전 검증으로 해석하지 않는다. 충분한 실제 accepted/rejected 판정 전에는 #768을 닫지 않고, accepted와 별도 운영 경로 검증 전에는 #756/#748을 완료하지 않는다.
