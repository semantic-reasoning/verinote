# 승인 사전 추출 반복 측정

`scripts/benchmark_extraction.py`는 버전이 고정된 합성 원문과 사람이 검토한 승인 사전으로 기존 `Service.start()`를 실행한다. 운영 저장소나 설정 파일을 받지 않는다. 사전 생성은 측정하지 않으며, 매 자료·매 반복마다 새 임시 DB에 합성 사전을 설치하고 승인한 뒤 문서를 추출한다. 문서 분할, 요청문, 인용 보정, 검증, checkpoint와 후보 저장은 운영 코드와 같다. 반복 사이에 이전 결과를 재사용하지 않는다.

## 인터넷 없이 실행하기

저장소 루트에서 패키지가 설치된 Python으로 실행한다. 결과 폴더는 **새 폴더**여야 하고 부모는 이미 있어야 한다. 기존 폴더나 symlink 경로는 거부한다. macOS의 `/tmp`처럼 시스템 별칭이 포함된 경로 대신 실제 경로 또는 저장소 아래 새 경로를 사용한다.

```sh
python scripts/benchmark_extraction.py --output ./benchmark-normal --cases short long --repetitions 2 --max-requests 40 --max-seconds 120 --max-tokens 100000
python scripts/benchmark_extraction.py --output ./benchmark-repair --cases short --scenario repair --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-failure --cases short --scenario failure --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-timeout --cases short --scenario timeout --repetitions 2
python scripts/benchmark_extraction.py --output ./benchmark-partial --cases long --max-requests 1
```

기본 `fake` 모드는 #726의 합성 모범 응답을 반환한다. `repair`는 별도 오류 자료와 기대 보정을 사용하며 short만 지원한다. `failure`와 `timeout`은 고정된 가짜 오류를 낸다. 정상 응답도 실제 검증과 평가를 거친다. 이 모드의 토큰(호출당 입력 100·출력 20)과 시간은 도구 검증용이며 **실제 모델의 속도·정확도·요금이 아니다**. 가짜 응답에는 실제 subprocess가 없으므로 CLI 호출 시간은 미확인이고, 문서 처리 시간과 호출 시도 수는 정상 경로에서 측정한다.

## 실제 CLI를 선택하는 방법

실제 실행은 별도 승인 후 `--mode cli --allow-model-calls --agent <provider> --model <explicit-model-id>`를 함께 지정한다. 지원 provider는 agy, claude, copilot, cursor, codex다. HTTP/OpenRouter 실험과 effort 변경은 이 도구의 범위 밖이다. 예를 들어 앞의 정상 명령에 이 네 옵션을 추가할 수 있다. 도구는 실제 모델을 자동 호출하거나 실패 시 다른 모델로 바꾸지 않는다. 이 구현을 검증하는 동안 실제 모델을 호출하지 않았다.

CLI 인증은 기존 CLI 방식으로 처리한다. Verinote 사용자 설정·원본 문서·사용자 DB를 읽지 않는다. CLI 자체가 관리하는 인증·캐시·계정 사용량은 그 CLI의 동작을 따른다. 결과에는 실행 모드, 요청 모델, timeout·출력 크기 등 허용한 설정만 기록한다. CLI 버전은 자동 탐색하지 않아 `null / unknown_not_probed`다. 알 수 없는 제공자 기본 설정이나 실제 해석된 모델도 추정하지 않는다.

## 한도의 의미

- `--repetitions`(기본 2)는 자료별 반복 횟수다. `--max-requests`(40), `--max-seconds`(120), `--max-tokens`(100000)는 전체 실행이 공유한다. `--call-timeout`(30초)은 개별 호출의 상한이며 청크 및 전체 잔여 시간으로 더 줄어든다.
- 최초 요청·인용 보정·실패한 시도를 전송 전에 하나씩 예약한다. `limited_timeout()`으로 만든 객체도 동일 예산을 사용한다. 예약된 attempt는 실제 전송이나 청구가 입증됐다는 뜻은 아니다. 실패나 실행 파일 누락에도 환불하거나 숨은 재시도를 하지 않는다.
- 전체 시간은 사전 조건 확인과 결과 폴더 생성 후 예산을 만들 때부터 잰다. 문서 설치·추출·검증·기록·정리가 포함된다. 만료 후 새 호출을 허용하지 않으며 기존 CLI timeout과 프로세스 트리 정리를 기다린다. OS 스케줄링·검증·저장·정리 때문에 최종 경과 시간은 설정값을 조금 넘을 수 있다. 엄밀한 프로세스 전체 종료 시각이나 원격 요금 상한을 보장하지 않는다.
- 토큰은 응답 후 기존 usage 기록의 입력+출력을 합친다. 입력에 포함된 캐시와 출력에 포함된 추론은 다시 더하지 않는다. 마지막 호출이 한도를 넘겨도 그 사용량을 기록하고 다음 호출을 막는다. 사후 정보이므로 토큰 사용량의 강제 상한은 항상 `token_hard_cap_guaranteed: false`다.
- 누락·불완전·일치하지 않는 usage는 기본 `--missing-usage stop`으로 중단한다. 명시적인 `continue`는 요청 수 상한을 유지하며 반복하지만 `token_usage_verified: false`로 표시한다. 부분적으로 알려진 토큰도 `observed_tokens_lower_bound`에 남긴다. 알 수 없는 토큰·비용을 0으로 채우지 않는다. 비용은 reported/estimated를 분리하고, 크레딧은 단위의 SHA256과 출처별로 구분한다.
- 인터럽트와 저장 실패는 새 호출 허용을 먼저 취소한다. 이미 허용된 호출은 남은 timeout 안에 정리하고 worker를 기다린 뒤 임시 DB·로그를 삭제한다. 인터럽트가 원격 요청을 취소하거나 요금을 되돌렸다는 뜻은 아니다.

## report.json 읽기

새 결과 폴더에는 버전 1의 `report.json`만 남는다. `conditions`에 자료/사전/추출·보정 요청문 해시, 평가·계약 버전, 사람 검토 상태, 요청 모델, 안전한 설정, 분할 크기, 반복 수와 새 저장소 조건을 고정한다. `attempts`는 시도 순서·청크·보정 여부·성공/실패·토큰·비용·검증된 CLI 호출 ID와 #724 시간 기록(미확인 시 null), 측정 wrapper 경과 시간을 담는다. #725의 문서별 집계는 `repetitions[].document_timing`에 연결한다. 시간과 usage가 서로 독립적으로 미확인일 수 있다.

`repetitions`의 각 행은 자료 하나의 반복 실행이다. `quality`는 현재 run에 속한 검증된 checkpoint prefix를 #726 평가기로 점수화한다. 실패로 1/2청크만 검증됐다면 `complete: false`이고, recall 분모는 **전체 문서 정답**이다. 유효 prefix가 없는 경우에도 빈 결과의 누락을 평가한다. 실행 성공과 정답 평가의 `passed`는 별개다. 상위 `quality.passed`는 전체 청크 완료와 평가 통과를 모두 요구한다. 실제 원문·인용·잘못된 triple 문자열은 내보내지 않으며, 추가 관계는 해시와 위치로 식별한다. 합성 원문은 버전 고정 자료에서 별도로 대조한다.

`summary`는 성공/실패 시간 분포와 자료별 분포, 실제 실행 표본 수, 보정·실패 시도, 시간 기록 coverage, 비용의 알려진 호출 수를 표시한다. 분포는 최소·최대·평균·중앙값·모집단 표준편차다. 표본 0개는 값이 null, 표본 1개는 표준편차 0이다. 서로 다른 자료를 합친 분포만으로 모델을 비교하지 않는다.

정상·실패·한도 중단 모두 보고서를 기록한다. `status: completed`는 계획된 반복이 끝났다는 뜻이며, 개별 실행은 실패할 수 있다. `stopped`이면 `stop_reason`과 실제 attempt 수를 확인한다. 성공 실행만 종료 코드 0, 실패·중단은 1, 설정·저장 실패는 2다. 매 호출과 반복 뒤 원자적으로 갱신하므로 중간 파일의 `running`은 부분 결과다. 디스크 오류가 나면 마지막 정상 파일을 보존하고 정적 오류 메시지를 반환한다. 저장장치가 처음부터 쓰기 불가능하면 보고서를 새로 만들 수 없다.

가짜 결과 예제는 [benchmarks/extraction/examples](../benchmarks/extraction/examples)의 `benchmark-fake-*.json`에 있다. 동일 조건은 위 명령 또는 보고서의 `conditions`를 사용해 재실행한다. 시간·호출 ID는 재실행마다 달라진다. API 사용자는 `verinote.extraction_benchmark.measure(output, cases=..., limits=Limits(...))`를 호출할 수 있다. `caller_factory`와 `clock`은 오프라인 검증용 주입 지점이다. 실제 호출을 주입하면서 fake로 표시해서는 안 된다.

실제 가짜 응답 반복 실행의 결과·시간 편차·추가 측정 계획은 [#728 결과 보고서](extraction-performance-results.md)에 정리했다.
