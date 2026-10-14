# Verinote 구조

## 데이터와 승인 경계

`documents`는 논리 문서 ID와 현재 버전을, `versions`는 불변 원본 바이트·텍스트·파일명·해시를 저장한다. 새 버전 등록은 낙관적 `expected_version` 확인과 한 트랜잭션으로 진행한다. 문서 제목은 논리 문서의 제목이며 파일명은 버전별로 보존한다. 명시적인 최신 리비전/전체 문서 삭제를 제공한다. 과거 버전 덮어쓰기는 제공하지 않는다.

`runs`는 에이전트·모델·대상 문서 버전을 고정한다. 실행 중 원문 버전이 올라가도 결과는 원래 버전에만 저장한다. 문서별 동시 실행은 DB unique index로 차단한다. 사전 생성과 관계 추출을 구분한다. 성공한 동일 사전 revision의 재추출은 거부하며 새 승인 revision의 추출은 기존 검토를 보존하고 새 pending 후보를 추가한다. 실패한 실행은 부분 후보를 남기지 않으며 재시도 가능하다.

`items`는 ontology(추출 사전), page, fact를 같은 검토 계약으로 저장한다. 상태는 pending/approved/held/rejected다. `revision`은 내용 수정 및 판정마다 증가하고 요청자가 본 revision을 제출해야 한다. `events`는 생성·승인·보류·반려·철회·수정의 당시 payload, 행위자, 이유, 시각을 보존한다. 사전 및 사실 수정은 반드시 pending으로 전환한다. 위키 승인만으로 관계가 승인되지는 않는다.

`snapshots`는 하나의 SQLite 트랜잭션에서 읽은 현재 문서 버전과 승인 사실 전체를 해시와 함께 저장한다. 과거 후보 내용이 변경되어도 snapshot의 payload는 바뀌지 않는다. `queries`는 질의 결과와 원래 요청·규칙 본문·규칙 해시·엔진 버전을 기록한다. 재조회에는 현재 설치된 엔진과 규칙을 사용하므로 버전이 변경된 환경에서는 기록된 버전·해시를 비교해야 한다.

## 모듈

- `documents.py`: 형식 검사, UTF-8/PDF/DOCX 텍스트 추출, 8,000자 청크.
- `agents.py`: provider별 고정 argv, 모델 값 검증, 별도 cwd, 시간/출력 제한, 종료 및 로그.
- `ontology.py`: 사전 스키마·누적 크기·별칭 충돌·관계 타입 검사, 두 단계 프롬프트.
- `service.py`: 단계당 최대 16회 청크 처리, 사전 생성 및 승인 사전에 따른 추출, 원문 인용 검증, 원자적 후보 저장, 비동기 작업.
- `store.py`: SQLite 문서 버전과 검토 이력, 트랜잭션, 스냅샷.
- `rules.dl` / `engine.py`: 사용자 값을 코드로 보간하지 않고 `EasySession.insert`로 넣는 실제 Wirelog 평가. 모든 symbol은 public API를 통해 처리한다. native engine 접근은 lock으로 직렬화한다.
- `server.py` / `web/`: loopback HTTP API, buildless 한국어 UI.

## API

| 경로 | 동작 |
| --- | --- |
| GET/POST `/api/documents` | 목록 / 신규 또는 다음 버전 등록 (`document_id`, `expected_version`, `expected_generation`) |
| GET `/api/documents/{id}?version=N` | 해당 버전 원문·초안·관계·버전 목록 |
| GET `/api/documents/{id}/raw?version=N` | 정확한 원본 바이트 다운로드 |
| GET `/api/documents/{id}/diff?version=N` | 직전 버전 대비 추출 텍스트 diff |
| POST `/api/documents/{id}/ontology` | `{agent,model?,expected_version,expected_generation}`로 사전 생성 시작, 202 응답 |
| POST `/api/documents/{id}/extract` | 같은 요청 형식으로 승인 사전 기반 추출 시작, 202 응답. 미승인/기추출 사전 또는 오래된 문서 토큰은 409 |
| POST `/api/items/{id}/review` | `{revision,action,actor,reason,payload?}` |
| GET `/api/items/{id}/history` | 불변 검토 이벤트 목록 |
| GET `/api/runs`, `/api/runs/{id}` | 작업 상태와 오류 |
| GET/PUT `/api/settings` | 선택 에이전트 및 provider별 모델 ID |
| POST `/api/query` | `{action,subject,predicate,target,snapshot?}` |
| GET `/api/queries` | 최근 결과 30개와 재현 입력 |

변경 API는 application/json만 받는다. 원문 업로드는 `content_base64` 또는 `text`, `title`, `filename`을 받는다. 충돌 409, 잘못된 입력 400, 교차 출처 403, 처리 실패 500을 구분한다. 웹 파일은 고정 allowlist만 제공한다.

## 채택한 범위

첫 원자적 구현 단위는 버전별 문서 등록 → 실제 CLI 추출 → 사람 검토 → Wirelog 결과와 원문 증명이다. 기존 교육자료의 서비스·문서명·승인 예시를 하드코딩하지 않는다. 복수 사용자 인증, 전역 온톨로지 병합, OCR, 의미 검증 자동화는 이 초기 구현 범위에 포함하지 않는다.

## 자연어 질문

`assistant.py`는 선택 CLI의 JSON 요청을 `query_graph`, `search_documents`, `read_document`만으로 제한한다. 모델이 선택한 실제 graph 실행이 없으면 최종 답변을 거부한다. graph/source/inference 주장 종류와 실제 반환된 증거 ID를 대조한다. 빈 graph는 긍정 주장 근거가 될 수 없다. 의미적 함의 검증은 사람이 한다.

`questions` 테이블은 제출 시 고정한 snapshot, agent/model, 질문, 상태, 누적 trace/evidence, 답변과 오류를 저장한다. 검색·읽기는 snapshot의 문서 버전만 사용하고 그래프는 snapshot의 승인 payload만 사용한다. 기존 runs와 같은 worker/8개 슬롯을 사용하며 재시작 시 미완료 질문도 interrupted로 복구한다.

POST `/api/questions`는 `{question,agent?,model?,snapshot?}`를 받아 202로 작업을 반환한다. GET `/api/questions`는 최근 30개, GET `/api/questions/{id}`는 저장 결과와 진행 상태를 반환한다. UI polling은 선택 질문 ID를 확인하여 이전 실행이 새 화면을 덮어쓰지 않는다. 고급 구조화 질의 API는 유지한다.

## 삭제와 동시성

`documents.next_version`은 기존 최대 버전+1로 migration하며 삭제 시 내려가지 않는다. 별도의 `generation`은 새 버전 등록/최신 삭제마다 증가한다. 두 변경 토큰을 필수로 확인해 버전 rollback의 ABA를 방지한다. version_count는 생존 원문 행을 센다.

POST `/api/documents/{id}/delete-latest`, `/delete`는 정확히 `{expected_version,expected_generation}`를 받는다. 최신-only는 마지막 하나를 삭제하지 않고 409를 반환한다. DB 트랜잭션은 전체 active runs/questions를 확인한 뒤 대상 버전과 파생 데이터, 해당 버전을 포함한 snapshots, 관련 queries/questions를 제거한다. 원문의 일부가 들어간 질문뿐 아니라 해당 스냅샷을 사용한 모든 이력이 제거된다.

`create_question`의 스냅샷 존재 검사와 INSERT는 하나의 BEGIN IMMEDIATE에 속한다. 질문 등록이 먼저면 삭제는 active 작업 때문에 거부되고, 삭제가 먼저면 질문 등록은 삭제된 snapshot 때문에 거부된다. 따라서 별도 service lifecycle lock 없이 DB가 수명 경쟁을 직렬화한다. `save_question`은 UPDATE만 허용하고 스냅샷 존재를 재확인한다. 동기 그래프 조회의 `save_query`도 스냅샷 존재를 같은 저장 트랜잭션에서 재확인하여 삭제 후 결과가 재생성되지 않게 한다.

삭제 트랜잭션은 `log_cleanup(kind,id)`에 로그 정리 의무를 함께 저장한다. Service는 삭제 후와 시작 시 처리하고 GET `/api/cleanup`, POST `/api/cleanup` (`{}`)으로 상태/재시도를 제공한다. 서버가 생성한 32자리 hex ID와 고정 runs/questions 루트만 사용하며 루트/대상 symlink는 따라가지 않고 pending으로 남긴다. 없는 폴더는 정리 성공이다. 파일 오류는 의무를 남기고 DB 삭제 성공과 구분한다. 복구 가능한 휴지통이나 포렌식 소거는 제공하지 않는다.

## 문서 설명과 용어 정의

`search_documents`는 고정 스냅샷의 제목·파일명·본문을 NFC/casefold로 비교한다. 결과에 filename, matched_fields, total_chars, offset_strategy를 추가한다. offset은 원문 읽기 시작점이다. 본문 literal 일치는 원본 문자열 좌표를 반환하고, 제목/파일명이나 정규화 전용 본문 일치는 0(`start`)에서 읽게 한다. 정규화 문자열의 달라진 길이를 원문 좌표로 사용하지 않는다. 결과 상한 20개와 non-citable 계약은 유지한다.

`read_document`는 원문을 변환하지 않고 title/filename, offset/end_offset/total_chars/next_offset을 제공한다. 문서 카탈로그에도 filename과 길이를 제공한다. 프롬프트는 제목을 가리키는 문서 설명 질문을 원문에 근거해 답하고, 빈 승인 그래프와 원문 설명 가능성을 구분한다. 일반 정의·외부 지식은 별도 근거 없이 보충하지 않으며 부분 읽기의 미확인을 문서 전체 부재로 단정하지 않는다. 실제 Wirelog 실행과 source/graph 인용 검증은 그대로이며, 관련 원문이 없거나 질문이 자료 밖이면 unknown-only 응답도 여전히 허용한다.


## 승인 사전 기반 추출

문서 버전별 ontology item 하나를 먼저 생성하고 사람이 검토한다. 개체는 정규 ID·원문 명칭·타입·인용, 별칭은 명칭·대상 ID·동일성을 검토할 인용, 관계는 snake_case ID·의미/포함/제외 설명·주어/목적어 타입·인용을 가진다. 사전은 개체 200/별칭 400/관계 40개 및 UTF-8 60,000바이트로 제한한다. 별칭과 ID의 충돌, 타입 참조 오류, 원문에 없는 인용은 저장을 거부한다. 개체 name은 자동 별칭으로 취급하지 않는다. 같은 이름의 서로 다른 개체를 구분하려면 범위를 명시한 ID를 쓴다.

`create_run`은 `BEGIN IMMEDIATE` 안에서 문서 version/generation을 확인하고 같은 버전의 approved 사전을 읽어 ID·revision·payload·SHA-256을 `runs.ontology`에 고정한다. active run 유니크 제약은 두 단계에 공통이다. 기존 성공 추출도 같은 트랜잭션에서 검사한다. 사전 생성은 기존 위키/사실에 관계없이 허용하되 사전 item이 이미 있으면 편집을 안내한다. 승인 사전 revision마다 한 번의 성공 추출을 허용하며 재승인으로 revision이 바뀌면 새 추출이 가능하다.

사전 생성은 기존 사전과 현재 8,000자 청크를 모델에 보내 추가 항목만 받는다. 완전히 동일한 항목은 중복 제거하고 정의 충돌은 실패시킨다. 신규 인용은 현재 청크에서 검증하고 누적 사전은 읽은 원문 범위에서 다시 검증한다. 원문 청크 경계 손실은 아직 해결하지 않는다. 관계 추출은 모든 청크에 동일한 승인 사전을 보내며 청크당 80개 사실까지 검증한다. 프롬프트 UTF-8 상한은 120,000바이트다.

`finish_run`도 사전 및 사실을 검증한다. 추출 단계에서는 현재 사전이 고정된 revision/hash로 여전히 approved인지 확인한 뒤 전체 후보를 한 트랜잭션으로 저장한다. 실행 중 문서의 새 버전이 생기는 것은 기존처럼 허용하되 후보는 시작 버전에만 저장한다. 사전 수정/철회는 진행 중 추출의 결과 저장을 막는다. 이미 완료된 후보 및 승인 사실의 상태는 변경하지 않는다. 수동 fact revision은 해당 실행의 고정 사전으로 검증하며 legacy 사실은 일반 triple 검증을 유지한다.

문서 GET 응답은 각 후보에 `dictionary`(ID/revision/hash)와 문서에 `can_extract`를 제공한다. UI는 과거 버전의 실행 버튼을 숨기며 POST는 표시된 version/generation을 서버에서도 확인한다. 사전은 JSON 편집, 원문 인용 확인, 기존 검토 API로 승인한다. 사전/위키/사실 승인은 서로 독립이다. Wirelog는 여전히 approved fact만 읽으며 canonical ID 문자열로 문서를 연결한다. 사전은 버전별이므로 문서 간 ID 충돌·불일치는 검토자가 조정해야 한다.

마이그레이션은 기존 runs에 `stage TEXT NOT NULL DEFAULT 'legacy'`, `ontology TEXT`를 추가한다. 재실행 시 열 존재를 검사하며 기존 items/events/snapshots를 재작성하지 않는다. 새 INSERT는 열을 명시한다. 이전 바이너리는 8열 위치 기반 INSERT를 사용하므로 확장된 DB로 롤백할 수 없다. 서버 종료 후 데이터 폴더 백업 및 이전 DB 복원으로 롤백한다.
