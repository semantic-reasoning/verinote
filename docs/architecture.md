# Verinote 구조

## 데이터와 승인 경계

`documents`는 논리 문서 ID와 현재 버전을, `versions`는 불변 원본 바이트·텍스트·파일명·해시를 저장한다. 새 버전 등록은 낙관적 `expected_version` 확인과 한 트랜잭션으로 진행한다. 문서 제목은 논리 문서의 제목이며 파일명은 버전별로 보존한다. 명시적인 최신 리비전/전체 문서 삭제를 제공한다. 과거 버전 덮어쓰기는 제공하지 않는다.

`runs`는 에이전트·모델·대상 문서 버전을 고정한다. 실행 중 원문 버전이 올라가도 결과는 원래 버전에만 저장한다. 문서별 동시 실행은 DB unique index로 차단한다. 성공한 같은 버전의 추출을 다시 실행하여 기존 검토를 덮어쓰지 않는다. 실패한 실행은 부분 후보를 남기지 않으며 재시도 가능하다.

`items`는 page와 fact를 같은 검토 계약으로 저장한다. 상태는 pending/approved/held/rejected다. `revision`은 내용 수정 및 판정마다 증가하고 요청자가 본 revision을 제출해야 한다. `events`는 생성·승인·보류·반려·철회·수정의 당시 payload, 행위자, 이유, 시각을 보존한다. 사실 수정은 반드시 pending으로 전환한다. 위키 승인만으로 관계가 승인되지는 않는다.

`snapshots`는 하나의 SQLite 트랜잭션에서 읽은 현재 문서 버전과 승인 사실 전체를 해시와 함께 저장한다. 과거 후보 내용이 변경되어도 snapshot의 payload는 바뀌지 않는다. `queries`는 질의 결과와 원래 요청·규칙 본문·규칙 해시·엔진 버전을 기록한다. 재조회에는 현재 설치된 엔진과 규칙을 사용하므로 버전이 변경된 환경에서는 기록된 버전·해시를 비교해야 한다.

## 모듈

- `documents.py`: 형식 검사, UTF-8/PDF/DOCX 텍스트 추출, 8,000자 청크.
- `agents.py`: provider별 고정 argv, 모델 값 검증, 별도 cwd, 시간/출력 제한, 종료 및 로그.
- `service.py`: 최대 16회 청크 추출, 원문 인용 검증, 원자적 후보 저장, 비동기 작업.
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
| POST `/api/documents/{id}/extract` | `{agent,model}`로 작업 시작, 202 응답 |
| POST `/api/items/{id}/review` | `{revision,action,actor,reason,payload?}` |
| GET `/api/items/{id}/history` | 불변 검토 이벤트 목록 |
| GET `/api/runs`, `/api/runs/{id}` | 작업 상태와 오류 |
| GET/PUT `/api/settings` | 선택 에이전트 및 provider별 모델 ID |
| POST `/api/query` | `{action,subject,predicate,target,snapshot?}` |
| GET `/api/queries` | 최근 결과 30개와 재현 입력 |

변경 API는 application/json만 받는다. 원문 업로드는 `content_base64` 또는 `text`, `title`, `filename`을 받는다. 충돌 409, 잘못된 입력 400, 교차 출처 403, 처리 실패 500을 구분한다. 웹 파일은 고정 allowlist만 제공한다.

## 채택한 범위

첫 원자적 구현 단위는 버전별 문서 등록 → 실제 CLI 추출 → 사람 검토 → Wirelog 결과와 원문 증명이다. 기존 교육자료의 서비스·문서명·승인 예시를 하드코딩하지 않는다. 복수 사용자 인증, 사용자 정의 온톨로지 편집기, OCR, 의미 검증 자동화는 이 초기 구현 범위에 포함하지 않는다.

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
