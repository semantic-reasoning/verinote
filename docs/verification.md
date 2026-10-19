# 검증 기록

2026-10-10, Python 3.14.7 / PyreWire 1.1.2 / pypdf 6.20.0 환경에서 확인했다.

- `python -m unittest discover -s tests -v`: 23개 통과. 실제 Wirelog 경로·순환·관계 분리, 승인과 철회, 과거 스냅샷, 원문 버전과 정확한 바이트, 동시 승인, 추출 중 새 버전, 실패 시 원자성, HTTP 흐름·출처 차단, PDF/DOCX와 압축량 제한을 포함한다.
- `node --check verinote/web/app.js`: 통과.
- `python -m compileall -q verinote`: 통과.
- `python -m pip install -e .`: 설치와 console entry point 생성 성공.
- 실제 Antigravity(`agy`, `gemini-3.8-flash-low`)로 합성 문서의 위키 초안 1개와 관계 후보 2개를 생성했다. 자동 승인하지 않았다.
- Claude, Copilot, Cursor, Codex는 실제 기본 모델로 최소 JSON 응답을 확인했다. 모든 공급자에 대해 장문 문서 전체 추출 품질까지 검증했다는 뜻은 아니다.
- 초기 Copilot `--deny-tool '*'`는 설치 버전에서 거부되어 `--available-tools ''`로 수정하고 실제 호출을 재검증했다. Cursor는 격리된 빈 작업 폴더 신뢰가 필요하여 `--trust`를 지정했다. `--mode ask --sandbox enabled`는 유지한다.
- Chrome은 `net::ERR_BLOCKED_BY_CLIENT`로 차단되었으나 Safari에서 `http://localhost:8766`에 접속하여 자연어 질문 입력·모델 선택·비동기 완료·답변 렌더링·Wirelog 증거 펼치기를 확인했다. 사용자 서버와 별도 합성 데이터로 검증했다.

실제 CLI 프롬프트/응답 로그와 워크플로 선택 기록은 로컬 `.git/verinote-workflow/`에 보존한다. 테스트 데이터는 사용자의 `data/`에 주입하지 않았다.

## 자연어 → 실제 LLM → Wirelog 검증

실제 `agy / gemini-3.8-flash-low`로 합성 문서를 추출하고 명시적 의존성 두 후보를 QA 승인한 뒤 HTTP로 질문했다. 서버나 테스트 transport에 정답 경로를 지정하지 않았다.

| 질문 | 모델이 선택한 실제 도구 | 관찰된 답변 |
| --- | --- | --- |
| 주문 서비스는 원장 서비스에 어떻게 의존하나요? | query_graph path | 주문→결제→원장, 원문 v1 두 사실의 증명 경로 |
| 원장 서비스에 직간접 의존하는 서비스는? | query_graph upstream | 결제 직접, 주문 간접 |
| 오늘 주문 서비스 당직 담당자는? | query_graph direct + search_documents 당직 | claims 없음, 자료에서 확인 불가 |

세 질문 모두 실제 PyreWire 1.1.2 / triple-v1을 경유했다. 스냅샷은 `45eb468c44601fbb276909171d2bdfb875577a9f667991cc5e390c25e050e000`이다. Safari 웹 폼에서도 별도 경로 질문을 제출하여 실제 CLI 실행 후 같은 두 사실 인용과 완료 상태를 확인했다. 자동 테스트는 transport를 통제하면서 실제 엔진 결과의 다음 프롬프트 전달, 부정확한 인용 거부, 미승인 제외, 제출 후 버전 변경·철회·새 문서 등록의 격리, 오류 trace 및 재시작을 확인한다.

로그: `.git/verinote-natural/live-results.json`, `live-check.log`, `live/questions/`. 이는 합성 관계 예시의 관찰 결과이며 임의 문서·모든 모델의 의미적 정답률을 보증하지 않는다.

## 원문 삭제 검증 (2026-10-11)

전체 31개 테스트를 통과했다. 삭제 집중 테스트 8개는 최신 리비전 rollback과 이전 승인 복원, 삭제 번호 비재사용, generation을 통한 ABA 차단, 기존 DB migration, 버전 간격 diff, 원문·후보·검토 이력·스냅샷·질문·조회 제거, 무관한 데이터 보존을 검증한다. 로그 삭제 실패와 시작 시 재시도, symlink 외부 경로 보호, 활성 작업 거부, 업로드/중복 삭제 경쟁, snapshot 읽기 이후 삭제와 질문 등록/질의 저장 경쟁도 포함한다. Python 컴파일·JS 문법 검사를 수행했다.

별도 합성 데이터 서버 `http://localhost:8767`의 HTTP 등록·다음 버전 등록을 확인했다. 이번 실행에서는 컴퓨터 사용 도구가 `Sky Computer Use native pipe startup failed`를 반환하고 사용 가능한 앱/브라우저가 없어 삭제 확인창 시각검증은 수행하지 못했다. 삭제 동작과 API 계약은 HTTP 통합 테스트로 검증했다. 사용자 문서는 삭제하지 않았다.

## 문서 제목 질문 회귀 검증 (2026-10-11)

‘보정서’ 질문의 기존 기록은 첫 2,000자 원문 읽기, 본문 문자열 검색 실패, 빈 그래프 조회 후 unknown-only 답변이었다. 원문 제목의 일반 정의가 없다는 점과 등록 문서 내용 자체를 설명할 수 있다는 점을 혼동했다. 수정된 로컬 검색은 실제 등록 문서를 title/filename에서 찾는다. 사용자 원문을 모델에 다시 전송하지 않고 별도 합성 자료로 품질을 검증했다.

합성 문서는 제목만 ‘보정서’이고 본문은 팬 제어 청구항 및 삭제·정정 표시로 구성했다. 실제 `agy` 기본 모델에서 수정 전은 정의를 확인할 수 없다는 unknown-only 응답을 재현했다. 수정 후 localhost HTTP 질문에서는 실제 원문 읽기와 PyreWire direct 조회(empty)를 수행한 후, 팬 제어·삭제 청구항·정정된 기록 장치를 설명하는 source 인용 답변을 생성했다. 일반적 정의·법적 성격/효력은 별도의 미확인 항목으로 남겼다. 모델이 일반 정의를 학습하거나 검증했다는 의미는 아니다.

집중 도우미 테스트 10개, 전체 34개 통과. 제목만/파일명만 검색, NFC/NFD 및 casefold 확장과 안전한 원본 offset, 동일 제목 후보 구분, 2,000자 이후 이어 읽기, 빈 실제 그래프와 source 답변, 검색 결과 인용 금지·근거 유형 검사 및 기존 버전 고정/삭제 회귀를 포함한다. Python 컴파일 통과. UI 코드는 변경하지 않았다. 실제 호출 로그는 `.git/verinote-definition/`에 보존한다.

## OpenRouter 최종 통합 검증 (2026-10-19, #709 / #702)

기본 검증은 실제 키·외부 네트워크 없이 수행한다. `test_openrouter_integration.py`는 임시 `0600` 파일을 실제 로더로 읽고 실제 localhost HTTP 설정/등록/승인/추출/질문/이력/다운로드 경로를 사용한다. 외부 HTTPS만 순서가 정해진 fixture로 교체하며 socket guard로 외부 연결을 거부한다. 승인 전 그래프 제외, 별도 인용 보정 호출·caller verdict, 실패 후 재개와 과거 비용 중복 방지, 계약/상한 불일치, 대기 작업의 고정 모델·옵션, 키/모델 누락의 사전 거부, HTTP 200 오류·length·도메인 형식 오류·키 에코·timeout을 검증한다. 합성 키와 키 해시는 설정 파일 이외 DB/로그/checkpoint/API/다운로드에 없어야 하며, 실제 합성 CLI 자식 환경에도 상속되지 않아야 한다.

대표 큰 fixture는 개체 183개 사전과 사실 80개 추출을 검토 대기로 보존하고 실제 outbound 요청의 `max_tokens=16384`를 검사한다. 이는 로컬 크기·상태·저장 경계 검증이며 특정 실제 모델이 이 길이를 지원하거나 의미적으로 정확하다는 증거가 아니다. 실패 응답은 후보·성공 답변을 만들지 않고 안전하게 확인된 사용량만 보존한다. 키 에코/미수신 timeout에서는 응답 파일과 확인되지 않은 비용을 보존하지 않는다.

### 공통 완료 조건 교차 확인

| #702 요구사항 / 하위 작업 | 근거 |
| --- | --- |
| 새/기존 DB, CLI 5종, 설정·작업 override | `test_provider_settings`, `test_workflow`, `test_openrouter_pipeline.Migration`, `test_openrouter_integration` |
| 키 파일 생성·로딩·권한·시작 시 스냅샷 (#703) | `test_config`의 실 POSIX 파일/descriptor 검사와 Windows 안전 미지원 정책 시뮬레이션 |
| 기존 complete/effective_timeout/Service 주입 계약 (#704) | `test_workflow`, `test_transports`, `test_openrouter_pipeline` |
| 사전·승인·추출·인용 보정·질문·실행/사용량 연결 (#707/#709) | `test_openrouter_integration`의 HTTP 전체 흐름, `test_openrouter_pipeline`의 호출 횟수·단계·도구 한도 |
| 정상 JSON/non-object/invalid JSON, 오류/choices/content/refusal/tools/length, HTTP 오류/TLS/연결 (#705) | `test_openrouter.OpenRouterTests` failure matrix와 `test_openrouter_integration`의 저장 경계 검사; 전송 횟수로 자동 재시도 없음 확인 |
| 느린 응답·크기·framing·redirect·deadline (#705) | `test_openrouter`의 실제 socketpair framing/trickle/trailer/DNS·TLS 지연 및 헤더 없는/거짓 길이 응답 검사 |
| 일반·escape 키 에코·예외 및 통합 비밀 제외 (#705/#709) | `test_openrouter`, `test_http_records`, `test_config`, `test_openrouter_integration`의 DB/로그/API/checkpoint/다운로드/CLI 환경 검사 |
| usage 없음/부분/0/캐시/reasoning/모순/실패 비용·선택적 쓰기 실패 (#706) | `test_usage`, `test_http_records`; 보고 credits/추정 USD 분리와 provenance 집계 |
| 재개·계약/상한 불일치·기존 CLI checkpoint (#707) | `test_openrouter_integration`, `test_openrouter_pipeline`, `test_dictionary_pipeline`; 재사용 호출은 새 비용에 중복 포함하지 않음 |
| 큰 fixture·한도·잘림 수동 안내 (#708/#709) | 위 183/80 fixture, length 실패 검사, `test_provider_settings.UIHelpers` 및 실제 화면 확인 |
| 설정/실행/사용량 화면 (#708/#709) | 아래 실제 Chrome 확인; 이 작업은 UI 코드를 변경하지 않아 같은 화면이 적용됨 |
| 전체 unittest 및 JS syntax, 기본 CI의 네트워크·실 키 불필요 | 아래 최종 명령 결과; opt-in smoke와 별도 경로 |

### 실제 화면 확인

macOS의 연결된 데스크톱 Chrome에서 별도 임시 DB와 합성 키·fake adapter로 확인했다. 설정의 CLI 설치 여부/API 키 설정 여부 구분, 키 입력란 부재, 필수 모델의 브라우저 검증, CLI 기본 선택 시 빈 HTTP 모델 저장, HTTP 모델 trim·저장·새로고침 복원, 추출/사전·질문 제공자 전환과 전송·과금 안내, 재질문 준비의 모델/스냅샷 복원, 키 파일 준비 링크, 권한·재시작·Windows 정책 안내가 표시됐다. 실행/질문 실패 화면에서 timeout이 원격 취소·미과금이 아니라는 안내와 수동 재실행/모델·서버 한도 조정 안내를 확인했다. 사용량 화면은 OpenRouter credits와 추정 USD를 구분하고 미확인을 0으로 표시하지 않았다. 로컬 검증 기록과 스크린샷은 `issue708-ui.md`, `issue708-{settings,http-setup,extract,question-usage,run-error}.png`로 실행 세션에 보존했다.

처음 CUA native pipe 연결 실패가 있었지만 세션 초기화 후 Chrome 연결이 복구되어 실제 검증을 완료했다. 독립 Reviewer가 기록과 다섯 스크린샷을 검토하고 Architect/Critic도 해당 검증을 확인했다. 데스크톱 기본 viewport에서 확인했으며 모든 반응형 크기·native Windows 런타임을 검증했다는 뜻은 아니다. macOS POSIX 소유자/0600 검증은 실제 파일로 수행했다. Windows는 안전 미지원 분기만 테스트하며 실제 ACL 지원을 주장하지 않는다.

### 별도 명시적 실 API smoke

사용자가 실제 키 테스트를 명시적으로 요청해 정상 설정 로더와 실제 OpenRouter adapter로 한 번 호출했다. 고정 합성 프롬프트 `{"ok":true}`만 전송했고 사용자 문서나 질문 자료는 보내지 않았다. `openrouter/free`, `max_tokens=256`, 전체 deadline 60초에서 HTTP 200/finish stop, 기대 JSON 일치를 확인했다. 응답 모델은 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`, 보고 입력 31·출력 39(추론 35 포함)·총 70토큰, 보고 cost 0이었다. 이 수치는 해당 응답의 보고값이며 향후 무료 경로·가용 모델·가격을 보장하지 않는다. 키·원시 응답·Authorization은 검증 문서에 포함하지 않는다.

재현 절차는 README의 `python -m scripts.openrouter_smoke --allow-network --config PATH --model ID`이다. 기본 테스트는 이 스크립트의 opt-in/한 번 호출/안전 출력/실패 exit를 mock으로 검증하며 실제 요청은 하지 않는다. 이 작은 smoke는 기본 상한 16,384의 실제 모델 수용성, 장문 사전/추출 품질, 모든 모델 호환성을 검증하지 않는다. 그런 검증은 수행하지 않았다.


### 최종 명령 결과

- `PYTHONPATH=tests python -m unittest test_openrouter_integration test_openrouter_smoke test_openrouter_pipeline test_config test_provider_settings -v`: 44개 통과.
- `python -m unittest discover -s tests -v`: 253개 통과. 새 통합 5개·smoke 3개를 포함한다. 기본 suite에서 외부 네트워크·실제 키를 사용하지 않았다.
- `node --check verinote/web/app.js`, `git diff --check`: 통과.
- `python -m scripts.openrouter_smoke --help`: 명령·필수 opt-in 안내 확인, 네트워크 호출 없음.
