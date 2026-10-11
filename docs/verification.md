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
