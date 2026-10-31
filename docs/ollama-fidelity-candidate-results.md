# Ollama fidelity checklist v1 단일 short 평가

`ollama_fidelity_checklist_v1`는 **rejected**다. production 구조·인용·게시 조건은 통과했지만, 변경하지 않은 v2 유한 gold의 `coverage_semantics(calls)`와 `gap_completeness`가 실패했다. 이 후보를 기본 동작으로 채택하지 않는다. 원래 실패의 재현만으로 끝내지 않고, 명시적 후보를 구현·고정·새로 평가한 #781의 진단 결과다. 성능·운영 적용 및 #768/#756/#748의 완료 근거는 아니다.

## 고정 조건과 실행

실행 revision `a3d53688d17f69c92d294df5d5cf528fd5a3ea4e`, tree `4f54a96c0b5b2fddb017fb0ad0d8b765425771dc`. [후보 계약](ollama-fidelity-candidate-contract.md)의 short 한 문서, 최초 1회+기존 인용 수정 최대 1회, 2요청·600초·관측 100,000토큰 범위를 실행했다. 원래 중단된 baseline의 표본을 대체하거나 자동 재개하지 않았다.

- Ollama `0.40.13`, `qwen3.8:27b-mlx`, digest `5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e`.
- `num_ctx=32768`, `num_predict=4096`, `temperature=0`, JSON, `keep_alive=5m`, thinking 모델 기본값, 요청 timeout 최대 180초. 모델·생성 옵션·평가 기준은 baseline과 동일하다.
- template SHA256 `94b0982ea3b35f298818e8d45d95c4796438f939049acaaa28b8c62b4ba785b2`. 원본 production 입력 JSON은 그대로 보존하고 승인 관계 ID와 일반 점검 지시만 추가했다. gold·정답·이전 응답은 compiler의 입력이 아니다.
- 첫 generation 전에 fixture/gold manifest, 원본/보정/template prompt, 모델/version/options, 계획·예산, 실행 코드 사본·해시, argv·revision/tree를 [원본 폴더](../benchmarks/extraction/results/781-fidelity-short/)에 기록했다. 기존 두 baseline은 변경하지 않았다.

## 관측 결과

| 항목 | 관측 |
| --- | --- |
| 생성·인용 수정 | 최초 1회, 인용 수정 0회 |
| 토큰 | 입력 1,912 + 출력 3,939 = 5,851; unknown 호출 0 |
| 문서 wall | 156.048316초 |
| 최종 회계 wall | 156.206640초; 해시·offline 재평가 포함, 마지막 기록 쓰기 제외 |
| production/Service | 1/1 청크 검증, success |
| 유한 gold | failed: coverage_semantics(calls), gap_completeness |
| 게시 상태 | fact 1개·page 1개 pending, 사전만 합성 fixture로 approved |
| 모델 식별 | 호출 전 digest 고정, 호출 후 version/digest 확인 |
| cache 토큰·USD | unknown; 0으로 대체하지 않음 |
| 종료 | document_failed, 추가 요청 없음 |

fact의 유한 precision/recall과 인용 정확도는 각각 1/1이지만 전체 품질 통과가 아니다. 응답은 필수 근거 claim들을 반환하고 승인 calls/archives/mirrors를 각 한 행씩 포함했다. 그러나 `calls.claim_indices=[0,1,2,3]`에는 미등록 대상 근거 claim 5가 없다. `qualifier_not_representable`은 조건부 근거 claim 3만 가리키고, 과거·중단된 보관 근거 claim 4를 누락했다. gap과 coverage의 의미를 코드에서 채우거나 평가 기준을 완화하지 않았다.

production validator의 구조·인용 확인과 유한 annotation 평가가 다른 층위임을 보여준다. production success 때문에 pending 후보는 생성됐으나 사람 승인 사실로 승격되지 않았다. 이 한 문서·한 번의 결과로 일반 성능 향상, 자연어 의미의 완전성, 모델 간 우열을 판단하지 않는다. gold는 합성·agent 작성이며 사람 검토 pending이라는 기존 한계도 유지된다.

## 원본과 재평가

[report.json](../benchmarks/extraction/results/781-fidelity-short/report.json) SHA256: `c4c6ebcbcd78ef189adbc06e08f6dfc242479a621e0de2f416056b0ec9c2c68d`. 원본 71개 파일, 1,254,584바이트를 보존했다.

- [원본 응답](../benchmarks/extraction/results/781-fidelity-short/documents/short-1/runs/d7d3ae8cefcf4fb5bb20c65470afc9a7/1/response.json), [원본 prompt](../benchmarks/extraction/results/781-fidelity-short/documents/short-1/runs/d7d3ae8cefcf4fb5bb20c65470afc9a7/1/original-prompt.txt), [실제 prompt](../benchmarks/extraction/results/781-fidelity-short/documents/short-1/runs/d7d3ae8cefcf4fb5bb20c65470afc9a7/1/prompt.txt).
- [evaluation.json](../benchmarks/extraction/results/781-fidelity-short/evaluation.json), [고정 environment](../benchmarks/extraction/results/781-fidelity-short/environment.json), [전체 파일 index](../benchmarks/extraction/results/781-fidelity-short/index.json).
- call ID `317ba744c7b74282823ab30e5c10cde5`; 원본 사용량·호출 후 identity·SQLite·checkpoint·timing을 같은 실행에 연결해 재검증한다.

```sh
PYTHONPATH=. .venv/bin/python scripts/evaluate_ollama_fidelity.py evaluate \
  benchmarks/extraction/results/781-fidelity-short
PYTHONPATH=. .venv/bin/python scripts/diagnose_ollama_fidelity.py replay
```

HTTP와 subprocess를 차단한 offline 재평가가 저장 결과와 완전히 같았으며, 전후 모든 원본 파일 해시가 같았다. 보존된 Python 소스를 실행하지 않는다. 실행 소스·fixture·계약이 바뀌면 같은 도구 revision에서 재평가해야 한다. 추가 generation·프롬프트 개정·장문 성능 시험은 이번 결과에 포함하지 않는다.
