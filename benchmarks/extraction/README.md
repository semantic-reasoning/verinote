# 합성 승인 사전 추출 평가

단일 원본 자료는 패키지에도 포함되는
[`verinote/benchmark_data/v1`](../../verinote/benchmark_data/v1)에 있습니다.
의도적인 인용 오류/보정 자료는 별도
[`repair-cases`](../../verinote/benchmark_data/repair-cases)에 있습니다.

- [평가 규칙·명령·API](../../docs/extraction-evaluation.md)
- [사람 검토용 원문 의미·정답·근거 표](../../docs/extraction-evaluation-review.md)
- [완전 정답 예시 입력](examples/short-perfect-input.json) / [평가](examples/short-perfect-score.json)
- [형식은 유효하지만 사실을 빠뜨린 입력](examples/short-missing-input.json) / [평가](examples/short-missing-score.json)

모든 자료는 합성이며 실제 모델 성능은 미측정입니다. manifest의 `human_review` 상태와
사람 검토 표를 확인하세요. 사람 검토를 완료했다고 에이전트 대조로 대신 주장하지 않습니다.

반복 실행과 예산 제한은 [측정 도구 문서](../../docs/extraction-benchmark.md)를 참고한다. `examples/benchmark-fake-*.json`은 정상·보정·부분·실패의 가짜 응답 실행 예제이며 실제 모델 성능이 아니다.

#728의 새 실행 결과와 해석은 [측정 결과 보고서](../../docs/extraction-performance-results.md) 및 [원본 결과 색인](results/728/index.json)에서 확인한다. 실제 모델 성능은 미측정이다.
