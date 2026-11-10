# Verinote 구조

## 데이터와 승인 경계

`documents`는 논리 문서 ID와 현재 버전을, `versions`는 불변 원본 바이트·텍스트·파일명·해시를 저장한다. 새 버전 등록은 낙관적 `expected_version` 확인과 한 트랜잭션으로 진행한다. 문서 제목은 논리 문서의 제목이며 파일명은 버전별로 보존한다. 명시적인 최신 리비전/전체 문서 삭제를 제공한다. 과거 버전 덮어쓰기는 제공하지 않는다.

`runs`는 에이전트·모델·대상 문서 버전을 고정한다. 사전 첫 생성과 기본 관계 추출은 실행 중 원문 버전이 올라가도 결과를 원래 버전에만 저장한다. 사전 재생성과 `extraction_options`를 쓰는 추출은 요청부터 완료까지 원문 버전 등록·삭제가 없을 때만 저장하므로, 이런 실행이 대기·처리 중이면 `register`가 같은 쓰기 트랜잭션에서 새 버전 등록을 거부한다. 문서별 동시 실행은 DB unique index로 차단한다. 대기·처리 중인 실행은 `cancel_run`으로 취소한다: 대기 중이면 곧바로 `cancelled`, 처리 중이면 `cancelling`이 되고, 워커가 새 호출 허용을 멈춘 뒤 `cancelled`로 바꾼다. `cancelling`과 `cancelled`는 대기·처리 중 거부 조건(`active_extraction`, 사전 변경과 새 버전 등록 거부)에 들지 않지만 `cancelling`은 문서 삭제를 거부한다. `Store.recover`는 `cancelling` 실행을 `cancelled`로 끝낸다. 취소한 실행은 후보를 저장하지 않는다. 사전 생성과 관계 추출을 구분한다. 성공한 동일 사전 revision·추출 방식 버전의 재추출은 거부한다. 새 승인 revision 또는 개선된 추출 방식의 실행은 기존 검토를 보존하고 새 pending 후보를 추가한다. 실패한 실행은 부분 후보를 남기지 않으며 재시도 가능하다.

`items`는 ontology(추출 사전), page, fact를 같은 검토 계약으로 저장한다. 상태는 pending/approved/held/rejected다. `revision`은 내용 수정 및 판정마다 증가하고 요청자가 본 revision을 제출해야 한다. `events`는 생성·승인·보류·반려·철회·수정의 당시 payload, 행위자, 이유, 시각을 보존한다. 사전 및 사실 수정은 반드시 pending으로 전환한다. 위키 승인만으로 관계가 승인되지는 않는다.

`snapshots`는 하나의 SQLite 트랜잭션에서 읽은 현재 문서 버전·메타데이터와 승인 사실·위키·사전 전체를 knowledge_version=1 JSON 및 해시와 함께 저장한다. SQL 스키마 변경이나 기존 스냅샷 backfill은 없으며, 옛 스냅샷은 당시 고정된 관계만 사용한다. 과거 후보 내용이 변경되어도 snapshot의 payload는 바뀌지 않는다. `queries`는 질의 결과와 원래 요청·규칙 본문·규칙 해시·엔진 버전을 기록한다. 재조회에는 현재 설치된 엔진과 규칙을 사용하므로 버전이 변경된 환경에서는 기록된 버전·해시를 비교해야 한다.

## 모듈

- `config.py`: 서버 전용 TOML 자격 증명의 경로 선택, POSIX 소유자·권한 검증, 빈 파일 생성, 안전한 오류 및 메모리 주입 계약.
- `documents.py`: 형식 검사, UTF-8/PDF/DOCX 텍스트 추출, 관계 추출 8,000자 청크와 사전용 경계 우선 분할.
- `agents.py`: provider별 고정 argv, 모델 값 검증, 별도 cwd, 시간/출력 제한, 종료 및 로그.
- `ontology.py`: 사전 스키마·누적 크기·별칭 충돌·관계 타입 검사, 사람 판단 별칭의 `judged_*` 키 검사, 두 단계 프롬프트, 추출 응답 사전 검사와 인용 전용 보정 계약.
- `service.py`/`dictionary_pipeline.py`: 사전 최대 64구간·관계 추출 최대 16청크 처리, 복구 가능한 사전 생성 및 승인 사전에 따른 추출, 원문 인용 검증, 공백 런 폭 차이의 호출 없는 위치 확정과 청크당 1회 추가 보정 호출, 원자적 후보 저장, 비동기 작업.
- `store.py`: SQLite 문서 버전과 검토 이력, 트랜잭션, 스냅샷.
- `rules.dl` / `engine.py`: 사용자 값을 코드로 보간하지 않고 `EasySession.insert`로 넣는 실제 Wirelog 평가. 모든 symbol은 public API를 통해 처리한다. native engine 접근은 lock으로 직렬화한다.
- `server.py` / `web/`: loopback HTTP API, buildless 한국어 UI.

## 서버 비밀 설정 경계

`server.main()`은 `--config`로 선택한 파일 또는 OS별 기본 파일을 DB 생성·복구와 worker 시작 전에 `load_config()`로 읽는다. `--init-config`는 `create_config()`로 빈 템플릿을 만들고 종료한다. 명시 경로 하나만 사용하고 환경변수 키를 대체 입력으로 읽지 않는다. 기본 파일 미존재·키 누락/빈 문자열은 미설정이고, 명시 파일 미존재·파싱/타입·권한 오류는 시작 오류다. 경로와 운영 방법은 [README](../README.md#서버-전용-비밀-설정-파일)에 정리한다.

POSIX 읽기는 `O_NOFOLLOW | O_NONBLOCK`로 파일을 열고 같은 디스크립터의 `fstat`으로 일반 파일·현재 UID·정확한 `0600`을 검사한 뒤 최대 64 KiB를 읽는다. 경로의 파일이 교체되어도 검사한 디스크립터를 읽으며 FIFO 읽기로 멈추지 않는다. 생성은 직접 부모 디렉터리 디스크립터를 고정해 소유자·그룹/다른 사용자 쓰기 금지를 검사하고 그 안에서 `O_CREAT | O_EXCL | O_NOFOLLOW`, 최초 모드 `0600`으로 수행한다. 새 전용 디렉터리만 `0700`으로 만들고 기존 경로를 chmod하거나 덮어쓰지 않는다. 읽기/생성 모두 선택한 파일 또는 직접 부모 자체의 링크를 따라가지 않지만, 조상 디렉터리와 같은 UID의 악의적인 내용 변경에 대한 완전한 격리는 제공하지 않는다.

로더가 반환하는 `Credentials`는 dict/dataclass가 아닌 private slots 객체다. `repr`/`str`에는 설정 여부만 있고 JSON·pickle·`vars()` 직렬화는 거부한다. 외부 직렬화에는 `public_status()`의 `{'openrouter': {'configured': bool}}`만 사용한다. HTTP 어댑터는 명시적인 `openrouter_key()` 접근으로 키를 서버 메모리에서 주입받아야 하며 반환된 문자열의 추가 보관·로그·응답 비밀 검사는 소비자의 책임이다. 서버 시작 시 `Server(..., credentials=...)`와 `Service(..., credentials=...)`에 메모리 객체를 주입하고 Service는 transport Credentials로 명시적으로 변환하여 HTTP dispatcher에 전달한다. DB/API에는 키를 전달하지 않는다. 기존 `Server(address, store, service)`와 `Service(..., cli=...)` 호출은 유지한다.

파싱·OS 오류는 원문을 포함하는 예외를 연결하지 않은 정적 `ConfigError`로 변환한다. 환경변수의 `OPENROUTER_API_KEY`를 읽거나 쓰지 않으며 기존 CLI 자식 환경에서의 제거는 #704, HTTP 응답 키 에코 검사는 #705, 상태 API/화면은 #708 책임이다. 런타임 재로딩과 DB 설정 이관은 없다. Windows는 ACL 미검증으로 파일 생성/로딩을 거부하며 기본 파일 미존재만 CLI 시작을 허용한다. 플랫폼 분기는 테스트에서 모의 검증하고 Windows ACL 지원은 주장하지 않는다.

## API

| 경로 | 동작 |
| --- | --- |
| GET/POST `/api/documents` | 목록 / 신규 또는 다음 버전 등록 (`document_id`, `expected_version`, `expected_generation`) |
| GET `/api/documents/{id}?version=N` | 해당 버전 원문·초안·관계·버전 목록 |
| GET `/api/documents/{id}/raw?version=N` | 정확한 원본 바이트 다운로드 |
| GET `/api/documents/{id}/diff?version=N` | 직전 버전 대비 추출 텍스트 diff |
| POST `/api/documents/{id}/ontology` | `{agent,model?,expected_version,expected_generation}`로 사전 생성 시작, 202 응답 |
| POST `/api/documents/{id}/extract` | 같은 요청 형식으로 승인 사전 기반 추출 시작, 202 응답. 미승인/현재 방식으로 기추출한 사전 또는 오래된 문서 토큰은 409 |
| POST `/api/items/{id}/review` | `{revision,action,actor,reason,payload?}` |
| POST `/api/documents/{id}/approve-items` | `{version,expected_version,expected_generation,items:[{id,revision}],actor,reason}`로 선택한 검토 대기 위키·관계를 원자적으로 승인 |
| GET `/api/items/{id}/history` | 불변 검토 이벤트 목록 |
| GET `/api/runs`, `/api/runs/{id}` | 작업 상태와 오류 |
| GET/PUT `/api/settings` | 선택 에이전트 및 provider별 모델 ID |
| GET/PUT `/api/usage-rates` | 별도 settings 키에 에이전트별 선택적 USD/토큰 단가·크레딧 환산 저장 |
| GET `/api/usage` | 보존된 최근 1,000개 작업의 에이전트·모델·단계별 사용량, 범위와 확인된 값의 소계 |
| POST `/api/query` | `{action,subject,predicate,target,snapshot?}` |
| GET `/api/queries` | 최근 결과 30개와 재현 입력 |

변경 API는 application/json만 받는다. 원문 업로드는 `content_base64` 또는 `text`, `title`, `filename`을 받는다. 충돌 409, 잘못된 입력 400, 교차 출처 403, 처리 실패 500을 구분한다. 웹 파일은 고정 allowlist만 제공한다.

## 일괄 승인

`bulk_approve`는 명시된 1~2,000개 ID/revision만 승인한다. `version`은 화면에서 선택한 원문 버전이며 `expected_version`/`expected_generation`은 그 화면이 읽은 현재 문서의 동시성 토큰이다. 따라서 과거 버전도 명시적으로 검토할 수 있지만 도중의 새 버전 등록·삭제·롤백은 충돌로 처리한다. 빈 배치, 중복, 잘못된 타입(bool revision 포함), 추가 필드, 다른 문서/버전의 항목, 사전 및 pending이 아닌 항목은 거부한다.

문서 토큰·멤버십·revision·상태 검사와 각 후보 검증·상태 변경·이력 기록은 하나의 `BEGIN IMMEDIATE` 안에서 실행한다. 개별 `review`와 공유하는 `_review_item`이 원문 인용과 추출 당시 고정 사전을 다시 검증하며, 하나라도 실패하면 앞선 변경과 events까지 모두 롤백한다. 성공하면 항목별 revision이 한 번 증가하고 같은 검토자/이유로 개별 승인 이벤트를 기록한다. 현재 사전의 승인 철회를 이미 추출된 사실의 승인 금지로 바꾸지 않는다. 개별 검토와 동일한 계약이다.

브라우저는 초기 선택을 비우고 pending page/fact에만 체크박스를 표시한다. 전체 선택은 한도를 넘어도 몰래 잘라내지 않고 한도 초과를 표시한다. 확인창을 열 때 문서 ID·표시 버전·문서 토큰·선택 ID/revision을 별도 객체에 고정한다. 전송 중에는 중복 제출을 막고, 충돌 시 사용자가 새로고침해서 다시 선택한다. 늦게 도착한 문서 응답은 요청 순서로 차단하며 승인 완료 후에도 같은 문서/버전을 보고 있을 때만 해당 화면을 갱신한다. 모델 호출이나 DB 스키마 변경은 없다.

## 채택한 범위

첫 원자적 구현 단위는 버전별 문서 등록 → 실제 CLI 추출 → 사람 검토 → Wirelog 결과와 원문 증명이다. 기존 교육자료의 서비스·문서명·승인 예시를 하드코딩하지 않는다. 복수 사용자 인증, 전역 온톨로지 병합, OCR, 의미 검증 자동화는 이 초기 구현 범위에 포함하지 않는다.

## 자연어 질문

`assistant.py`는 `query_graph`, `search_knowledge`, `read_wiki`, `search_documents`, `read_document`만 허용한다. 실제 graph 실행 없이 최종 답변할 수 없다. graph/wiki/source/inference와 실제 증거 ID를 대조하며 빈 graph는 긍정 근거가 아니다. 승인 사전 검색은 문서별 어휘 안내이고 인용 근거가 아니다. `read_wiki`는 고정 페이지의 주장과 원문 인용을 렌더링한 문자열을 2,000자씩 제공하며 page_id/revision/version/sha256, claim_indices, offset/end_offset/next_offset/partial을 기록한다.

Assistant 초기화는 고정 메타데이터만 사용한다. 과거 스냅샷에 메타데이터가 없을 때도 Store.document_metadata가 버전별 컬럼만 조회하며 원문이나 현재 승인 항목을 읽지 않는다. 원문은 authorize_source를 통과한 뒤 source_text로 지연 로딩한다. search_documents 기본 mode=metadata는 제목·파일명만 검색하며 mode=body는 document_id와 fallback이 필수다.

fallback은 lookup_id, reason, detail, wiki_evidence_ids로 구성한다. reason은 legacy_snapshot/no_approved_wiki/no_match/insufficient_wiki 중 실제 상태와 일치해야 한다. 해당 문서를 포함한 잘리지 않은 search_knowledge 결과 및 graph 결과가 이전 모델 입력에 전달되어야 한다. 관련 위키가 있으면 검색된 각 페이지의 읽기 근거도 이전 회차에서 전달되어야 한다. 같은 tools 배열에서 추측한 근거 ID로 우회할 수 없다. 거부 요청도 trace에 error로 남고 원문 조회는 발생하지 않는다. 검사는 순서·범위만 보장하며 검색어와 의미적 충분성은 모델 판단이다. 부분 위키 읽기 범위를 명시하고 이를 전체 부재로 표현하지 않도록 요청한다.

새 스냅샷 pages/ontologies는 facts 및 documents와 동일 트랜잭션에서 승인된 현재 버전 내용만 고정한다. 기존 스냅샷의 누락 필드를 최신 데이터로 채우지 않는다. 자연어 질문에서만 문서 100/사실 500/위키 500/사전 100개와 지식 8 MiB 한도를 적용하며 직접 그래프 조회 한도는 바꾸지 않는다. 검색 20개/위키·원문 읽기 2,000자/결과 30,000바이트/프롬프트 60,000바이트, 모델 6회·도구 10회 예산을 유지한다. 잘린 지식 검색으로 fallback을 허용하지 않으므로 검색 범위를 좁혀야 한다. 삭제는 기존 documents 참조를 따라 새 지식 payload를 포함한 스냅샷 전체와 관련 질문을 제거한다.

`questions` 테이블은 제출 시 고정한 snapshot, agent/model, 질문, 상태, 누적 trace/evidence, 답변과 오류를 저장한다. 검색·읽기는 snapshot의 문서 버전만 사용하고 그래프는 snapshot의 승인 payload만 사용한다. 사전 생성 2개·승인 사전 기반 추출 2개와 별도로 질문은 1개씩 실행하며, 대기·실행 합계 8개 슬롯은 함께 사용한다. 재시작 시 미완료 질문도 interrupted로 복구하고, 취소 중(cancelling)이던 질문은 cancelled로 복구한다. 이때 cancelled·interrupted·error로 저장된 질문(이전에 그렇게 저장된 질문 포함)의 running·analyzed 분할 처리 회차(`context_processing`)는 각각 cancelled·interrupted·failed로 바꾸며, 바뀌는 내용이 없는 질문 행은 다시 쓰지 않는다. `save_question`(error 저장), `finish_question_cancel`, `end_unstored_question`도 질문을 끝난 상태로 저장할 때 같은 회차 변경을 함께 저장한다. 질문 취소(`POST /api/questions/{id}/cancel`, 정확히 `{expected_status}`)는 실행 취소처럼 확인창을 열 때의 상태와 저장된 상태가 같을 때만 대기 중인 질문을 바로 cancelled로, 처리 중인 질문을 cancelling으로 바꾼다. 저장된 상태가 cancelling이나 cancelled이면 `save_question`이 쓰기를 거부(RunCancelled)한다. 모델 호출 직전의 dispatched 호출 기록 저장도 거부되므로 취소가 커밋된 뒤에는 새 호출이 시작되지 않고, 그 뒤 저장할 차례가 온 답변도 저장되지 않는다. 작업자는 질문을 멈춘 뒤 cancelled로 기록한다. 결과 저장이 다른 오류로 실패할 때 쓰는 고정 문구 기록(`end_unstored_question`)도 취소된 질문에는 거부되므로 같은 cancelled 기록으로 이어진다. 그 기록 쓰기마저 모두 실패하면 cancelling으로 남고 재시작 때 cancelled로 복구된다. `Service.queue`는 등록된 실행·질문의 프로세스 메모리 목록이며 완료 순서를 보장하지 않는다. 목록 추가와 제출은 하나의 `queue_lock` 안에서 하고 끝나거나 취소된 항목은 지운다. 재시작으로 미완료 작업을 interrupted로 처리하므로 목록은 복원하지 않는다. 스케줄러는 작업 스레드를 시작한 뒤에만 작업을 큐에 넣는다. 따라서 스레드 시작 실패로 제출이 거부된 작업은 나중에 실행되지 않는다. 제출 실패 시 목록과 등록 상태를 정리하고 error 기록을 시도하며 슬롯을 한 번만 반환한다. 제출 래퍼가 작업을 넣은 뒤 오류를 던지는 경우에는 그 작업의 실행도 억제한다. 대기 중인 실행의 `queue.ahead`는 같은 단계에서 자리를 경쟁하는 작업이고, `queue.document_blockers`는 같은 문서의 호출·정리를 마치지 않은 이전 실행이다. 상태는 응답 시점의 DB 값이며, 다른 단계나 질문이 모두 끝나야 한다는 의미가 아니다. 대기 중인 질문의 `queue.ahead`는 질문 단계에서 앞선 질문뿐이고 질문에는 문서가 없으므로 `queue.document_blockers`는 비어 있다.

POST `/api/questions`는 `{question,agent?,model?,snapshot?}`를 받아 202로 작업을 반환한다. GET `/api/questions`는 최근 30개, GET `/api/questions/{id}`는 저장 결과와 진행 상태를 반환한다. UI polling은 선택 질문 ID를 확인하여 이전 실행이 새 화면을 덮어쓰지 않는다. 고급 구조화 질의 API는 유지한다.

## 삭제와 동시성

`documents.next_version`은 기존 최대 버전+1로 migration하며 삭제 시 내려가지 않는다. 별도의 `generation`은 새 버전 등록/최신 삭제마다 증가한다. 두 변경 토큰을 필수로 확인해 버전 rollback의 ABA를 방지한다. version_count는 생존 원문 행을 센다.

POST `/api/documents/{id}/delete-latest`, `/delete`는 정확히 `{expected_version,expected_generation}`를 받는다. 최신-only는 마지막 하나를 삭제하지 않고 409를 반환한다. DB 트랜잭션은 전체 active runs/questions를 확인한 뒤 대상 버전과 파생 데이터, 해당 버전을 포함한 snapshots, 관련 queries/questions를 제거한다. 원문의 일부가 들어간 질문뿐 아니라 해당 스냅샷을 사용한 모든 이력이 제거된다.

`create_question`의 스냅샷 존재 검사와 INSERT는 하나의 BEGIN IMMEDIATE에 속한다. 질문 등록이 먼저면 삭제는 active 작업 때문에 거부되고, 삭제가 먼저면 질문 등록은 삭제된 snapshot 때문에 거부된다. 따라서 별도 service lifecycle lock 없이 DB가 수명 경쟁을 직렬화한다. `save_question`은 UPDATE만 허용하고 스냅샷 존재를 재확인하며, 저장된 질문이 cancelling이나 cancelled이면 쓰지 않는다. 동기 그래프 조회의 `save_query`도 스냅샷 존재를 같은 저장 트랜잭션에서 재확인하여 삭제 후 결과가 재생성되지 않게 한다.

삭제 트랜잭션은 `log_cleanup(kind,id)`에 로그 정리 의무를 함께 저장한다. Service는 삭제 후와 시작 시 처리하고 GET `/api/cleanup`, POST `/api/cleanup` (`{}`)으로 상태/재시도를 제공한다. 서버가 생성한 32자리 hex ID와 고정 runs/questions 루트만 사용하며 루트/대상 symlink는 따라가지 않고 pending으로 남긴다. 없는 폴더는 정리 성공이다. 파일 오류는 의무를 남기고 DB 삭제 성공과 구분한다. 복구 가능한 휴지통이나 포렌식 소거는 제공하지 않는다.

## 문서 설명과 용어 정의

`Assistant.run`은 논리 회차 1~5에서 탐색 또는 조기 답변을 허용하고, 회차 6에서는 `answer_only=true`를 강제한다. 도구 슬롯 10개를 소진해도 다음 회차가 답변 전용이다. `exploration_rounds_remaining`은 현재 회차를 포함한 남은 탐색 회차 수이며 답변 전용에서는 0이다. `tool_slots_remaining`은 아직 사용하지 않은 요청 수다. 직전 탐색의 모든 trace/evidence를 다음 프롬프트에 전달한 뒤, 답변 전용에서는 정확히 answer 객체인 응답만 기존 인용 검증을 거쳐 반환한다. 추가 도구·혼합 객체는 실행 전에 거부한다. 실제 그래프 조회·유효 근거 없이 답변을 만들어 성공시키지 않는다. 논리 회차 최대 6회, 도구 최대 10회·회당 3개, 프롬프트/결과 크기 제한은 유지한다. 회차의 프롬프트가 입력 한도 안이면 그 회차는 모델 호출 1회이고 사용량을 `llm-{회차}`에 기록한다. 한도를 넘으면 그 회차를 분할 처리해 `context-{회차}-{번호}`(분석·이진 종합)와 `context-{회차}-decision`(최종 결정)에 기록한다. 질문 전체의 물리 호출 상한(`physical_call_bound`)은 2 × 6 × 2048 = 24,576회다.

`search_knowledge`는 요청 문서 범위 전체에서 먼저 기존 문자열 포함 검색을 수행한다. 승인 지식 일치가 0개일 때만 문장 안의 문서 제목·파일명 stem·승인 개체 ID/명칭·별칭을 보조 검색 이름으로 선택한다. NFC/casefold를 적용하고 문서 제목 탐색에만 공백을 생략해 비교한다. 한 글자와 명시된 일반 이름은 보조 검색에서 제외하며, 영문·숫자 이름은 다른 영문 단어 안의 부분 문자열을 이름으로 인정하지 않는다. 별칭은 같은 사전의 정확한 대상 ID/명칭으로만 확장하며 이름 하나당 변형은 최대 3개다. 이름은 긴 순서와 문서/종류/문자열 순서로 정렬해 최대 12개를 선택한다. 문서별 이름은 서로 합치지 않으며 명시한 document_id 밖의 이름은 선택하지 않는다. 제목 일치는 해당 문서의 위키를 탐색하도록 안내할 뿐 다른 문서를 요청 범위에서 제거하지 않는다.

위키 제목·주장 heading/text와 quote를 제외한 사전 안내만 검색한다. 전체 일치 위키를 사전 안내보다 앞에 안정적으로 정렬한 뒤 20개씩 페이지를 자른다. `query`, `match_mode`(literal/catalog), `matched_terms`, `anchors_truncated`로 검색 방법을 남기며 결과는 계속 non-citable이다. 전체 이름 후보가 한도를 넘으면 반환 일치가 0개여도 모든 페이지에서 result와 내부 lookup의 truncated를 true로 유지한다. 따라서 누락된 후보를 근거로 no_match 원문 fallback을 허용하지 않는다. 결과 페이지가 비었다는 이유로 검색 방식을 바꾸지 않으며, 페이지별 부분 조회를 합쳐 완전한 조회로 간주하지 않는다. 원문과 인용문은 후보 검색의 입력이 아니다.

`search_documents`는 기본 metadata 모드에서 고정 스냅샷의 제목·파일명만 NFC/casefold로 비교한다. body 모드는 한 문서의 ID와 검증된 fallback을 요구한 뒤 본문을 검색한다. 결과에 filename, matched_fields, total_chars, offset_strategy를 제공한다. offset은 원문 읽기 시작점이다. 허용된 본문 literal 일치는 원본 문자열 좌표를 반환하고, 제목/파일명이나 정규화 전용 본문 일치는 0(`start`)에서 읽게 한다. 정규화 문자열의 달라진 길이를 원문 좌표로 사용하지 않는다. 결과 상한 20개와 non-citable 계약은 유지한다.

`read_document`는 fallback 검증 후 원문을 변환하지 않고 title/filename, offset/end_offset/total_chars/next_offset을 제공한다. 문서 카탈로그는 본문 없이 filename과 길이를 제공한다. 문서 설명 질문은 승인 위키를 먼저 검색·읽고 부족한 경우에만 원문으로 보충한다. 빈 승인 그래프만으로 위키·원문 설명 불가를 단정하지 않는다. 일반 정의·외부 지식은 별도 근거 없이 보충하지 않으며 부분 읽기의 미확인을 문서 전체 부재로 단정하지 않는다. 실제 Wirelog 실행과 wiki/source/graph 인용 검증을 요구하며, 질문이 자료 밖이면 unknown-only 응답도 허용한다.

`query_graph`는 같은 Assistant와 고정 스냅샷 안에서 정규화된 인자가 같은 성공 결과를 재사용한다. 인자 검증 후 실제 엔진 실행·결과 크기 검사·근거 등록까지 성공한 결과만 저장한다. 빈 조회도 재사용되지만 긍정 주장에 필요한 근거를 만들지는 않는다. 재사용 trace는 원래 `evidence_id`와 1부터 시작하는 최초 성공 trace 번호 `reused_from`을 가지며 새로운 근거를 만들지 않는다. 매 요청의 trace와 progress, 도구 10회·모델 6회·누적 프롬프트 한도를 유지한다. 결과를 다음 모델 회차에 전달하기 전에는 재사용해도 원문 fallback이 허용되지 않는다. 그래프 이외의 중복 요청은 기존처럼 거부한다.


## 승인 사전 기반 추출

문서 버전별 ontology item 하나를 먼저 생성하고 사람이 검토한다. 개체는 정규 ID·원문 명칭·타입·인용, 별칭은 명칭·대상 ID·동일성을 검토할 인용, 관계는 snake_case ID·의미/포함/제외 설명·주어/목적어 타입·인용을 가진다. 구간별 응답은 개체 200/별칭 400/관계 40개로 제한한다. 누적 사전의 개수 제한은 각 구간 한도의 최대 구간 수(64) 배이며, 검증된 전체 사전과 반복 인용 24 MiB를 저장 한도로 적용한다. 사전 표 전환 기준은 설정의 dictionary_context_bytes(기본60,000, 1~120,000)이며 저장 유효성 한도도, 실행을 거부하는 한도도 아니다. 작업 등록 시 고정한다. 평범한 JSON 정의가 이 기준이나 전체 요청 한도를 넘으면 모든 정의를 해시 검증 가능한 행·문자열 표로 무손실 변환해 전송한다. 표도 기준을 넘거나 표가 전체 요청에 들어가지 않으면, 전체 요청에 들어가는 더 작은 쪽 형식을 그대로 전송하며 기준 초과만으로 중단하지 않는다. 전체 요청120,000바이트 및 더 작은 어댑터 예산은 별도로 검사하며 두 형식 모두 넘을 때만 거부한다. 승인 사전 추출은 남은 청크를 첫 호출 전에 같은 예산으로 모두 검사해 넘는 청크가 있으면 호출 없이 거부한다. 거부 상세는 실제 크기와 그 상태에서 가능한 다음 단계를 담는다. 사전 생성이 성공하면 같은 요청 구성·검사 함수(chunk_requests·worst_refusal)로, 생성한 사전을 바로 승인하면 고정될 사전(새 사전은 revision 2, 재생성은 반려 revision+2, 같은 길이의 ID)과 생성 실행의 에이전트 예산을 써서 모든 청크를 모델 호출 없이 검사한다. 이후 내용을 바꾸지 않는 검토 단계가 revision을 더 올려도 요청은 줄지 않으므로 경고의 요청 크기는 하한이다. 거부되면 실행 상세 앞에 경고를 붙인다. 거부 여부와 관계없이 runs/<id>/extraction-admission.json에 경고(거부되지 않으면 없음), 사전 해시, 정책 해시(표 해석 규칙·추출 프롬프트·계약 버전·120,000바이트·Ollama 예산, 그리고 고정 문서·고정 사전 두 개의 청크별 거부 크기)를 기록하며, 문서 API는 현재 버전의 반려되지 않은 사전에는 기록 대신, 현재 사전(승인된 사전은 그 revision, 승인 전 사전은 revision+1로 고정)·현재 정책과 생성 실행 에이전트의 예산으로, 직렬화한 청크 필드가 가장 긴 청크 하나를 같은 함수(chunk_requests·worst_refusal)로 모델 호출·파일 기록 없이 다시 검사해 붙이고, 다시 검사하지 못하면 미확인으로 붙인다. 두 형식의 요청 크기는 청크 필드 길이만큼만 청크마다 달라지므로 그 청크의 결과가 전체 청크 검사의 결과와 같다. 반려된 사전과 이전 버전 사전에는 두 해시가 모두 같을 때만 기록의 경고를 붙인다. 청크 크기·사전에서 인용·판단 키를 빼는 규칙·요청 구성·표 변환이 바뀌어 고정 문서의 거부 크기가 달라지면 정책 해시가 달라지고, 크기가 같으면 달라지지 않는다. 경고는 승인·추출을 막지 않고, 계산이나 기록 저장의 실패도 생성을 실패시키지 않는다. 누적 사전을 검토·승인하거나 복구할 때 구간별 개수 제한을 다시 적용하지 않는다. 한도 설명만 달랐던 직전 프롬프트 해시의 체크포인트도 다른 계약·원문·사전·제외 기록이 모두 유효하면 재사용한다. 알 수 없는 프롬프트 변경은 계속 거부한다. 인용을 의미 문맥 크기 계산에서 분리해, 여러 항목이 같은 긴 원문을 인용해도 전체 사전 저장 한도를 넘지 않는 한 검증된다. 별칭과 ID의 충돌, 타입 참조 오류, 원문에 없는 인용은 저장을 거부한다. 사람의 내용 수정은 새로 넣는 개체 ID·이름·별칭이 조회 참조 접두사(`entity:`, `relation:`)로 시작하거나 새 개체 ID에 줄바꿈·탭이 있으면 거부하며(사전 추가 경로도 같은 `lookup_spelling_problem`을 쓴다), 이미 저장된 그런 행은 유지하고 문서 GET의 `lookup_spelling_problems`(kind `entities`·`names`·`aliases`)로 알린다. 문서 GET은 ID와 이름이 띄어쓰기만 다른 개체(`spacing_kind`가 `spacing`인 쌍, [사전 조정](dictionary-reconciliation.md)의 기준)도 `spacing_name_rows`로 알리며, 경고만 하고 승인과 사람의 내용 수정은 막지 않는다. 개체 이름은 고유하지 않으므로 저장된 같은 표기의 개수를 넘는 행만 새 행으로 거부하고, 한 번의 내용 수정에서 다른 행으로 옮기는 것은 받아들인다. 모델 생성·체크포인트·pin 검증은 이 규칙을 적용하지 않으며, 생성하거나 재개한 사전에는 이런 행이 제외 기록 없이 저장된다. 개체 name은 자동 별칭으로 취급하지 않는다. 별칭은 인용에 개체 name이 함께 있거나, 사람이 판단한 표기·대상·대상 이름을 고정하는 `judged_alias`·`judged_entity`·`judged_name` 세 키를 가져야 한다. 이 키가 있는 행은 별칭 표기·대상·대상 이름이 바뀌면 거부되고, 프롬프트와 질문 안내에서는 빠진다. 같은 이름의 서로 다른 개체를 구분하려면 범위를 명시한 ID를 쓴다.

`create_run`은 `BEGIN IMMEDIATE` 안에서 문서 version/generation을 확인하고 같은 버전의 approved 사전을 읽어 ID·revision·payload·SHA-256을 `runs.ontology`에 고정한다. active run 유니크 제약은 두 단계에 공통이다. 기존 성공 추출도 같은 트랜잭션에서 검사한다. 사전 생성은 기존 위키/사실에 관계없이 허용하되 사전 item이 이미 있으면 편집을 안내한다. 승인 사전 revision·추출 방식 버전마다 한 번의 성공 추출을 허용한다. 재승인으로 revision이 바뀌거나 이전 방식에서 현재 방식으로 바뀌면 새 추출이 가능하다.

사전 생성은 최대 4,000자 구간(최대 64개)과 인용문을 제외한 기존 사전 문맥으로 추가 항목을 요청한다. 구간 분할은 후반부의 문장/줄 경계를 우선하며 모든 문자를 정확히 보존한다. 각 구간을 다시 32자 이후 첫 문장/줄 경계를 우선하는 최대 400자 source span으로 나누고 내용 해시를 포함한 ID를 부여한다. 모델의 `quote_ref:[시작ID,끝ID]`를 현재 구간의 ID 목록으로 검증하고 연속 원문으로 복원한다. 숫자 문자 오프셋 추측·이전 구간 ID·literal quote 대체 응답은 허용하지 않는다. 저장/검토 사전은 기존 literal quote 형식이다. CRLF 인용도 원문 그대로 검증한다. 전체 사전의 이름·별칭·타입·정의 충돌 검증은 유지한다. 경계를 가로지르는 의미의 손실 가능성은 남는다.

사전 호출만 `CLI.complete(..., phase='ontology')`로 600초 기본 제한을 적용한다. 명시적 CLI timeout은 우선하며 다른 단계 기본값은 바꾸지 않는다. 자동 재시도는 없다. 성공적으로 병합·검증한 구간만 `runs/{id}/dictionary-checkpoint.json`에 atomic replace로 저장한다. `dictionary-progress.json`에는 완료/재사용 글자 수, 이전 실행 ID, 시작하지 않은 호출을 포함한 호출 시도 수(`calls_started`)를 별도로 기록한다. schema 3 체크포인트는 사전·offset·제외·조정 감사 상태를 함께 저장한다. 저장에 실패한 구간은 진행률이나 최종 후보에 반영하지 않는다. 선택적 진행 표시 파일 쓰기는 경고로 처리한다.

재시도는 DB에서 같은 문서 버전·에이전트·요청 모델의 error/interrupted/cancelled 실행 최대 8개를 조회한다. 대기 중에 취소되어 상세가 `QUEUED_CANCEL_DETAIL`인 cancelled 실행은 모델을 호출하지 않았으므로 조회에서 제외한다. symlink 경로/파일 및 30 MiB 초과 파일을 거부한다. 스키마·소유 실행·문서/원문 해시·프롬프트/처리 버전·분할 크기와 결정적 완료 경계를 확인하고, 완료 prefix 전체로 사전을 다시 검증한다. 호환되지 않는 기록은 제외하고 그 이유를 상세에 남긴다. 상속한 진행도 새 실행에 저장하여 반복 재시도를 지원한다. 삭제된 DB 실행은 파일 정리가 지연되어도 재사용할 수 없다. SQL 마이그레이션은 없으며 과거 실행에 복구 파일이 없으면 새로 처리한다. 요청 모델이 빈 문자열인 경우 제공자의 실제 기본 모델 변경은 검출하지 못한다.

완료된 prefix의 재사용은 추가 모델 호출이 아니며 기존 호출 로그를 복사하지 않는다. 모든 실제 호출은 현재 실행의 구간 번호 폴더에 기록한다. 전체 완료 prefix만 재사용한 성공 실행은 명시적인 완료 marker로 새 호출 시도 0회를 확인한다. marker가 없거나 손상되면 미확인으로 남긴다. 최종 `finish_run`은 전체 원문 검증·실행 상태·사전 유일성을 재검사한 후 하나의 pending 사전을 저장한다. 관계 추출은 모든 청크에 동일한 승인 사전을 보내며 청크당 80개 사실까지 검증한다. 프롬프트 UTF-8 상한은 120,000바이트다.

관계 추출은 `exclude_off_dictionary_facts`로 승인 사전과 일치하지 않는 사실을 제외한 뒤 `extraction_quote_issues`에서 남은 응답의 형식·필드 길이·사전 어휘·타입을 검사한다. 문자열이지만 현재 청크에 없거나 계약 v2에서 두 위치 이상에 있는 인용을 문제 항목으로 모은다. 계약 v2의 제품 실행(`quote_repair_limit`이 None)은 `locate_whitespace_variants`로 먼저 각 문제 인용을 스페이스·탭·`\r`·`\n` 런으로 나눠, 같은 단어가 폭만 다른 런으로 이어지는 구간을 시작 위치마다 중첩까지 세어 찾는다. 런이 사라지거나 생기는 일치는 없다. 매치가 둘 이상이거나 어떤 매치든 원문 런 안의 줄바꿈(`\r\n`은 1개)이 2개 이상이면 그 인용은 확정하지 않는다. 매치가 정확히 하나면 복사한 응답에서 quote만 그 원문 구간으로 바꾸고 문제 항목에서 뺀다. 저장값은 원문 구간이고 그 occurrence마다 같은 단어의 매치인데 매치가 하나뿐이었으므로 v2 유일성과 checkpoint 재검증을 그대로 통과한다. 정확·모호 인용은 occurrence마다 그 자체로 매치라 매치가 둘 이상이므로 바뀌지 않는다. 모델 보정이 고를 수 있었던 더 넓은 문맥은 들어오지 않으므로 구간이 행을 뒷받침하는지는 검토자의 판단이다. 보정 응답에는 이 단계를 적용하지 않는다. 남은 문제 항목이 있으면 같은 에이전트·모델에 원문 청크, 고정 사전, 문제 항목을 보내 `path`와 `quote`만 받는다. 경로는 사전 불일치 제외 후 번호로 `facts.44.quote`처럼 0 기반이며 UI 오류는 `관계 45 인용`처럼 1 기반이다. 제외가 있었던 응답에서 `관계 N 인용` 오류로 chunk가 끝나면 상세에 `(관계 번호는 사전 불일치 제외 뒤 기준)`을 덧붙인다. 반환 경로 집합이 요청 집합과 정확히 같아야 하고 중복·누락·추가 필드와 문자열이 아닌 인용은 실패한다. `resolve_quote_repairs`가 행 단위로 판정해, 복사한 응답에서 통과 행의 인용만 바꾸고 원문의 한 위치로 확인되지 않은 행은 제외한 뒤 기존 원문·사전 검증을 모두 다시 수행한다. 공백 런 폭 외의 유사 문자열 매칭, 단어 자동 삭제, 보정 응답의 경로 누락 허용 또는 전체 응답 재생성은 하지 않는다.

추가 보정 호출은 청크당 1회이며 제품에는 실행당 상한이 없다. 형식 오류·시간 초과·호출 실패·프롬프트 한도 초과도 추가 재시도 없이 실행을 끝낸다. 원래 호출과 보정 호출의 로그 폴더는 분리하고 교체한 변경 전후 인용은 `quote-repair/changes.json`에, 제외한 행은 `quote-repair/refused.json`에 기록한다. 두 기록의 저장 실패는 그 청크를 확정하지 않는다. 위치를 확정한 행의 변경 전후는 보정 호출 전에 청크 폴더의 `quote-located.json`에 쓴다. 이 저장 실패도 그 청크를 확정하지 않지만, 파일이 있다고 청크가 저장된 것은 아니며(뒤이은 보정·검증 실패에도 남는다) `path`는 `changes.json`처럼 근거 불가 제외 전 번호다. 벤치마크는 `Service(quote_repair_limit=2)`로 실행해 다음 보정 전에 멈추고 `apply_quote_repairs`로 전부 실패를 유지하므로 위치 확정을 적용하지 않고 제외 경로에 도달하지 않는다. 진단 재생도 위치 확정을 호출하지 않으며, `ollama_fidelity.py`의 `production_passed`와 `diagnostics/extraction_fidelity.py`의 `quote_repair_required`는 위치 확정 없이 판정하므로 제품이 호출 없이 저장하는 응답도 보정 필요로 표시할 수 있다. 벤치마크·진단 코드의 보정 횟수 검사는 각 벤치마크의 지출 한도이고 제품 규칙이 아니며, 그 결과는 제외 동작의 근거가 아니다. `docs/extraction-ollama-baseline-contract.md`, `docs/ollama-fidelity-candidate-contract.md`, `docs/extraction-fidelity-candidate-contract.md`는 보존 결과의 `environment.json`이 해시로 등록한 문서라 고치지 않는다. 그 문서들의 `production Service`는 등록된 실행 당시의 Service로, 문서당 보정 2회 고정 상수와 전부 실패 동작을 썼다. 이후의 벤치마크는 같은 한도를 `quote_repair_limit=2`로 넘기고, 제품은 그 상수를 쓰지 않고 이 인자도 넘기지 않는다. 성공 상세의 인용 안내에는 근거 불가 제외 건수·위치와 이번 실행의 보정 개수·위치·추가 호출 수, 위치 확정 개수·위치(제외 뒤 번호)를 넣고 모델 문구와 원문은 넣지 않는다. 위치 확정 개수는 `checkpoint.accept`를 지난 청크만 세므로 실패 상세에는 검증을 마치지 못한 청크의 행이 나오지 않는다. 재사용 청크의 확정은 세지 않으며, 위치를 보이려면 checkpoint 형식을 바꿔야 하므로 일반 대조 안내만 낸다. `finish_run(detail=...)`은 기존 호출과 호환되는 선택 인자이며 성공 상세도 후보와 같은 트랜잭션에 저장한다. 사전 생성·legacy 수동 수정·자연어 질문의 검증 계약에는 위치 확정과 보정을 추가하지 않는다. 정확한 인용도 의미적 근거를 보장하지 않으므로 후보는 pending이다.

`finish_run`도 사전 및 사실을 검증한다. 추출 단계에서는 현재 사전이 고정된 revision/hash로 여전히 approved인지 확인한 뒤 전체 후보를 한 트랜잭션으로 저장한다. 기본 추출에서는 실행 중 문서의 새 버전이 생기는 것을 기존처럼 허용하되 후보는 시작 버전에만 저장한다. 같은 문서 버전에 대기·실행 중인 실행이 있으면 `_review_item`이 사전 변경을 같은 쓰기 트랜잭션 안에서 거부한다. `finish_run`의 이 검사는 저장소 밖에서 사전이 바뀐 경우를 막는 마지막 방어선으로 남는다. 이미 완료된 후보 및 승인 사실의 상태는 변경하지 않는다. 수동 fact revision은 해당 실행의 고정 사전으로 검증하며 legacy 사실은 일반 triple 검증을 유지한다. `finish_run`이 저장을 커밋한 뒤 예외를 내도(예: 연결 닫기 실패) 실행은 성공으로 남는다. 관계 추출은 저장된 상태를 한 번 다시 읽어 성공이면 성공으로 마무리하고, 실패 기록은 성공으로 저장된 실행을 바꾸지 않는다.

문서 GET 응답은 각 후보에 `dictionary`(ID/revision/hash)와 문서에 `can_extract`를 제공한다. UI는 과거 버전의 실행 버튼을 숨기며 POST는 표시된 version/generation을 서버에서도 확인한다. 사전은 JSON 편집(`revise`), `POST /api/items/{id}/dictionary-terms`(추출 점검에서 제외된 표기를 별칭이나 새 개체로 추가하며 항상 검토 대기 revision을 만든다) 또는 `POST /api/items/{id}/dictionary-types`(타입 불일치를 고치는 한 곳의 변경: `widen`은 관계 한 자리의 허용 타입에 개체의 현재 타입을 넣고 `retype`은 개체 타입을 그 자리 허용 타입 중 하나로 바꾼다. 타입은 정확한 문자열로 비교하고, 다른 관계나 개체는 바꾸지 않으며, 그 타입의 마지막 개체인데 그 타입을 허용하는 관계가 있으면 바꾸기를 거부하고, 항상 검토 대기 revision을 만든다)로 고치고, 원문 인용 확인, 기존 검토 API로 승인한다. 추가와 타입 수정은 그 문서 버전의 실행이 대기·처리 중이면 기존 사전 검토와 같은 이유로 거부된다. `GET /api/documents/{id}/passages?version=N&text=`는 표기가 원문에 그대로 나오는 구간을 겹치는 출현까지 세어 앞뒤 80자와 함께 20곳까지 주고 전체 출현 수를 알린다. 문서 GET 응답은 사전 후보에 판단 기록 별칭마다 마지막 판단자·이유·revision을 `alias_judgements`로 싣는다. 사전/위키/사실 승인은 서로 독립이다. Wirelog는 여전히 approved fact만 읽으며 canonical ID 문자열로 문서를 연결한다. 사전은 버전별이므로 문서 간 ID 충돌·불일치는 검토자가 조정해야 한다.

마이그레이션은 기존 runs에 `stage TEXT NOT NULL DEFAULT 'legacy'`, `ontology TEXT`를 추가한다. 재실행 시 열 존재를 검사하며 기존 items/events/snapshots를 재작성하지 않는다. 새 INSERT는 열을 명시한다. 이전 바이너리는 8열 위치 기반 INSERT를 사용하므로 확장된 DB로 롤백할 수 없다. 서버 종료 후 데이터 폴더 백업 및 이전 DB 복원으로 롤백한다.

## CLI/API 사용량 산출

`usage.py`는 CLI 전송 형식과 검증된 HTTP envelope를 각각 읽으며 모델이 생성한 답변의 `usage` 키를 계측 근거로 사용하지 않는다. `CLI.complete`는 기존처럼 애플리케이션 JSON을 반환한다. Claude/agy는 JSON result의 본문을, Cursor는 stream-json의 최종 result를, Codex는 JSON events의 agent_message와 최종 turn.completed 카운터를 분리한다. Copilot은 호출마다 지정한 `--usage-output-file`을 읽는다. 중간·중복 이벤트를 합산하지 않는다. 실제 설치 CLI의 무해한 단문 응답을 정제한 5개 fixture가 `tests/fixtures/usage`에 있다.

`agents.parse_response`는 최종 본문의 ANSI·전체 JSON 코드 블록 처리를 유지하며 객체만 반환한다. Copilot에 한해 JSON 객체 앞의 공백뿐인 `<final_answer>...</final_answer>` 접두 태그 하나를 제거한다. Cursor에 한해 전체 본문이 정확히 같은 JSON 객체 텍스트 두 개일 때 하나로 읽는다. 객체 밖 공백만 무시하며 서로 다른 필드 순서·숫자 표기·값, 세 번째 객체, 덜 끝난 객체, 부가 설명은 수용하지 않는다. 임의 텍스트에서 JSON을 찾아내거나 여러 응답을 합치지 않는다. 프로세스 종료·제공자 실패 검사가 우선하고 원본 stdout과 사용량은 그대로 보존하며 성공한 정리의 고정 라벨만 `run.json.response_normalizations`에 남긴다. 이 공통 파서는 사전·추출·질문에 적용되며 별도 모델 재호출은 없다.

정규화된 입력은 캐시를 포함하는 총량, 출력은 추론을 포함하는 총량이다. Claude·Cursor·Copilot은 일반 입력+캐시 읽기+캐시 쓰기로 입력을 복원한다. agy는 input_tokens+cache_read_tokens, Codex는 이미 캐시를 포함한 input_tokens를 사용한다. agy/Codex는 별도 청구되는 캐시 쓰기 구간이 없어 계산상 0이다. cache_write는 물리적인 캐시 생성 통계가 아니라 별도 가격을 적용할 구간이다. 추론을 출력에 다시 더하지 않는다. 근거는 [agy headless](https://www.antigravity.google/docs/cli/headless), [Claude 비용](https://code.claude.com/docs/en/costs), [Cursor 출력](https://cursor.com/docs/cli/reference/output-format), [Codex 비대화형](https://learn.chatgpt.com/docs/non-interactive-mode) 문서와 실제 CLI 응답이다. Cursor usage 세부 키는 관측된 형식이므로 형식 변경은 미확인으로 처리한다.

`usage.json`은 시작 전/프로세스 실행 후/종료 시 임시 파일 교체로 기록한다. 프로세스 정리 후 남은 제한된 출력을 읽어 JSON 파싱 실패·비정상 종료·타임아웃·크기 초과에서도 가능한 카운터를 남긴다. 전송 성공 후 온톨로지 검증이 실패한 경우 호출 상태는 success, 부모 작업은 error일 수 있다. 단가 snapshot을 호출 시작에 복사하고 결과의 resolved_model, 없으면 requested_model을 대조한다. 모델이 없는 기본값 단가는 명시적 가정이며 모든 추정에 rate_basis를 남긴다. 계산은 `(일반입력×입력단가 + 캐시읽기×읽기단가 + 캐시쓰기×쓰기단가 + 출력×출력단가) / 1e6`; 크레딧 추정은 그 비용을 지정 환산값으로 나눈다. 토큰 구간이 미확인이면 비용을 만들지 않는다. 모델별 복수 단가 자동 선택과 구독 청구 재현은 제공하지 않는다.

별도 `settings.usage_rates` 키를 사용하므로 기존 `/api/settings` roundtrip과 충돌하지 않는다. 에이전트 최대 6개(CLI 5종과 OpenRouter), 각 단가 객체의 필드·숫자·단위·모델 길이를 검증한다. DB 스키마 변경·과거 로그 backfill은 없다. 이전 버전으로 코드만 되돌려도 새 키와 sidecar는 무시되며, 원래 데이터 백업을 복원하면 설정과 로그까지 되돌릴 수 있다. 실행 중 작업은 종료 후 배포하고 데이터 폴더를 백업한다.

조회는 DB가 소유한 ID만 대상으로 runs의 사전 1~64번 구간, 관계 추출 1~16번 청크와 각 quote-repair·recovery를 확인하고, questions는 `call_manifest`가 목록이면 그 목록의 dispatched·complete·failed 항목 경로를, 아니면 llm-1~6을 확인한다. 경로의 symlink를 거부하고 파일당 256 KiB로 제한한다. 프롬프트/원본 응답을 API 계측값으로 노출하지 않는다. run/question GET 응답에 읽기 전용 usage를 덧붙이며 질문 저장 payload를 변경하지 않는다. 집계는 기존 목록의 100/30개 제한과 독립적으로 최근 1,000개 작업을 선택한다. 작업 수·truncated, 지표별 known_calls, 불완전 기록 수와 호출 횟수조차 모르는 작업 수를 함께 반환한다. DB와 파일은 하나의 트랜잭션이 아니며 읽는 시점의 보존 기록만 나타낸다. 부분적으로 삭제된 로그의 정확한 호출 수는 복원할 수 없다. 삭제된 DB 소유자는 물리 로그 정리 실패와 무관하게 이후 집계에서 제외된다. 대기 중에 취소된 실행은 호출 로그가 없고 실행 상세가 대기 중 취소 문구(`QUEUED_CANCEL_DETAIL`)이면 호출 0회로 집계한다. 이 문구는 대기에서 취소로 바뀌는 전환에서만 기록되고 그 뒤 바뀌지 않으며, 재시도 후보 8개에서 제외하는 조건과 같은 값을 쓴다. 대기 중에 취소된 질문도 호출 로그가 없고 오류가 질문용 대기 중 취소 문구(`QUESTION_QUEUED_CANCEL`)이면 호출 0회로 집계한다. 이 문구도 대기에서 취소로 바뀌는 전환에서만 기록되며, 그 뒤 작업자가 질문을 가져가도 첫 저장이 거부되어 모델을 호출하지 않고 문구도 바뀌지 않는다. 질문은 제출할 때 빈 호출 계획 목록(`call_manifest`)으로 저장되고, 모델 호출 직전에 그 호출의 dispatched 기록을 저장하며 이 저장이 실패하면 호출하지 않고 그 항목을 이전 상태로 되돌린다. 따라서 error·cancelled·interrupted로 끝난 질문의 목록이 모두 planned이면(빈 목록 포함) 호출 0회로 집계한다. planned가 아닌 항목이 하나라도 있으면 이 규칙은 적용하지 않는다. error·cancelled·interrupted로 끝난 질문의 dispatched·failed 항목에 읽을 수 있는 run.json과 usage.json이 모두 없으면(각 파일은 경로에 symlink가 없고 256 KiB 이하이며 열어 읽을 수 있는 파일이면 내용이 비어 있거나 손상되어도 읽을 수 있는 로그로 보고, 그런 로그가 하나라도 있는 항목은 호출로 세되, 받아들인 usage.json과 함께 시작하지 않은 근거가 남은 항목은 합계에서 뺀다. 사용량은 usage.json에서 읽고 OpenRouter·Ollama는 run.json의 `provider_call_id` 등과도 맞아야 하며, usage.json이 없거나 해석할 수 없거나 맞지 않는 사용량은 미확인으로 남는다) 그 항목은 호출로 세지 않고, 그런 항목이 하나라도 있는 질문에는 `call_count_known: false` 항목 하나를 `question` 단계로 덧붙인다. dispatched 기록은 호출 전에 저장되므로 그 뒤의 저장이 실패하거나 서버가 재시작되면 호출 없이 남을 수 있다. 로컬 CLI(지속 Claude 작업자 포함)와 Ollama는 첫 호출 로그를 쓰지 못하면 요청하지 않지만 OpenRouter는 기록 저장이 실패해도 요청을 보내고, 쓴 로그도 나중에 지워지거나 읽을 수 없게 될 수 있으므로, 로그가 없다는 것은 호출 0회의 근거도 아니다. 끝나지 않았거나(queued·running·cancelling) 성공한 질문, complete 항목, 두 로그 중 하나라도 읽을 수 있는 항목은 이전처럼 호출로 세고, 아래의 시작하지 않은 호출만 합계에서 뺀다. 목록이 없는 예전 질문은 llm-1~6 로그로 세고, 로그도 없으면 대기 중 취소 문구가 아닌 한 미확인으로 둔다. 집계용 작업 목록(`usage_owners`)도 질문의 `error`와 `call_manifest`를 전달하므로 실행 기록 합계와 질문 상세가 같은 규칙을 쓴다. 처리 중에 실패하거나 취소된 사전 생성·관계 추출 실행은 호출 로그가 없고 그 실행의 진행 기록(`dictionary-progress.json`, `extraction-progress.json`)이 `schema_version` 1이고 그 단계의 `pipeline`(사전 생성 `source-spans-v1`, 관계 추출 `approved-extraction-v1`)과 같은 실행 ID·`status: error`·`calls_started: 0`을 가지면 호출 0회로 집계한다. `calls_started`는 모델 호출을 시작하기 전에 1씩 올리고 `status: error`는 실행이 실패나 취소로 끝날 때 메모리의 값으로 기록되므로, 중간 기록이 실패했어도 이 값이 0이면 그 실행은 모델을 호출하지 않았다. 진행 기록이 없거나 `running`으로 남은 경우를 비롯해 그 밖에는 미확인으로 둔다. `calls_started`는 시작하지 않은 호출을 포함한 호출 시도 수다. `summarize`는 `calls`가 0일 때도 토큰·`non_reasoning_output`·`reported_usd`·`estimated_usd`를 `value: null, known_calls: 0`으로, `credits`를 빈 목록으로 두며, 이 `null`과 빈 목록은 합산할 시작한 호출의 기록이 없다는 뜻이다(시작하지 않은 호출의 기록은 있을 수 있다). 화면의 `renderUsage`는 `calls`만으로 이 경우를 가려 토큰·비용·크레딧을 미확인 대신 합계 0으로 표시한다. `calls`에는 호출 횟수 미확인 작업도 들어가므로, 확인된 호출이 0회여도 그런 작업이 있으면 미확인으로 남는다. error·cancelled·interrupted로 끝난 OpenRouter 작업에 로그가 있는 항목이 하나라도 있으면 `job_calls`는 항목 루프 뒤, 목록의 `unconfirmed` 항목을 덧붙이기 전에 기록 없이 끝난 시도를 확인한다(#905). OpenRouter만 기록 저장이 실패해도 요청을 보내므로 다른 전송 방식은 확인하지 않는다. 그 방식에서 로그 없는 시도는 요청하지 않은 시도다. 실행은 `error_marker_attempts`가 받아들인 진행 기록(위 0회 규칙과 같은 `schema_version`·`pipeline`·실행 ID·`status: error` 조건에 0 이상의 정수 `calls_started`)이 있으면 `calls_started`가 로그가 있는 항목 수(시작하지 않은 호출 포함)보다 클 때, 없으면 로그가 있는 항목이 모두 `call_started: false`일 때 `call_count_known: false` 항목 하나를 덧붙인다. 초과분은 시도 수이며 요청 전에 로그 없이 예외로 끝난 시도(자격 증명 없음, 모델 ID 거부, 수를 올린 뒤의 진행 기록 저장 실패)도 들어가므로 요청이 있었다고 단정하지 않고 미확인으로 둔다. 호출 계획 목록이 없는 예전 질문은 회차를 차례로 실행하고 N회차를 llm-N에 기록하므로, 로그가 있는 가장 높은 회차보다 앞선 회차에 llm-N 로그도 context-N-* 폴더도 없으면 같은 항목을 덧붙인다. 목록이 있는 질문은 위의 dispatched·failed 규칙이 맡는다. 받아들인 진행 기록이 없고 시작한 호출의 로그가 있는 실행, 예전 질문에서 로그 없이 끝난 마지막 회차, 그 밖에 로그를 남기지 못했거나 나중에 지워진 호출은 드러나지 않으므로 합계는 보존된 기록의 합계다.

`job_calls`는 로그가 있는 항목마다 `call_started`를 붙인다. usage.json을 받아들였고 모델 요청이 제공자에 닿기 전에 실패했다는 근거가 로그에 있으면 `false`이고 상태를 `not_started`로 둔다. 근거는 `usage.not_started(agent, usage, metadata)` 한 곳에서 제공자별로 판단한다. 로컬 CLI와 지속 Claude 작업자는 usage.json `status: not_started`다. 이 호출에 프로세스가 없었다는 뜻이며, 지속 작업자는 `Popen` 직전에 `process_requested`를 올리고 `Popen`이 `OSError`를 낼 때만 내리므로 Manager·ExtractionPool 대기 시간 초과도 여기에 들고 프로세스를 띄운 뒤의 실패는 `error`다. OpenRouter는 run.json `transmission_state: not_sent`이고 `transport_status`가 `not_started` 또는 `timeout`이다. 경계는 첫 HTTP 요청 바이트이며 DNS·TCP·TLS는 그 전에 오간다. Ollama는 run.json과 usage.json이 모두 `status: error`이고 `generate_requested: false`다. 이 값은 `/api/generate` 요청 직전에 true가 되며 `/api/tags`는 생성 요청이 아니다. 이 필드가 없는 예전 기록은 센다. 근거가 없거나 기록이 `starting`·`running`으로 남은 호출, usage.json이 없거나 읽을 수 없거나 run.json과 맞지 않아 받아들이지 않은 호출은 run.json에 근거가 있어도 센다. `summarize`는 `call_started`가 `false`인 항목을 `calls`와 모든 합계에서 빼고 `not_started_calls`로만 센다. 이 제외는 `job_calls`가 호출 횟수 미확인 항목을 덧붙인 뒤에 적용되므로, 시작하지 않은 호출이 있는 작업에도 미확인 항목은 그대로 남는다. 실행 상세의 `새 호출 시도 N회`는 `calls_started`이므로 시작하지 않은 호출을 포함한다.


## 사전 중복과 정의 충돌

`merge_dictionary`는 직접 사용하는 엄격 병합 계약이며, 자동 생성은 아래의 조정 필터 v5를 사용한다. 엄격 병합은 기존 사전을 재검증하고 모든 새 항목을 정규화·검증한 뒤 합친다. 개체의 ID/name/type, 별칭의 alias/entity, 관계의 ID/description/subject_types/object_types가 같으면 인용 차이는 정의 충돌이 아니다. 문자열은 기존의 양끝 공백 정리만 적용하고 관계 타입 배열은 기존 규칙대로 정렬된 집합으로 비교한다. 유사 표현·대소문자·관계 설명을 추측해 같은 뜻으로 합치지 않는다. 첫 유효 인용을 유지하므로 추가 인용을 모두 저장하는 기능은 아니다.

중복으로 버리는 항목도 근거·이름·별칭·타입 검사를 거친다. 정확한 자기 ID 매핑만 대상 개체의 존재와 ID를 포함한 원문 근거를 확인한 뒤 제외하며, 이 경우 정규 표시 명칭은 개체 자체의 근거로 검증한다. 실제 별칭은 별칭과 정규 명칭이 같은 인용에 있어야 하며, `judged_*` 세 키를 가진 별칭 행만 예외로 인용에 정규 명칭이 없어도 된다(별칭 표기는 그 인용에 있어야 한다). 입력 개수 한도와 최종 전체 개수/크기/충돌 검사는 유지하며 직접 JSON 검토 입력의 중복 개체·실제 별칭·관계 선언은 계속 거부한다.

원래 전달받은 기존 사전 항목을 정규화 후 인용까지 정확히 반복하는 경우에만 이전 원문 범위의 근거를 재사용할 수 있다. 새 인용과 같은 응답 안에서 처음 나온 항목은 현재 구간의 근거가 필수다. 실제 충돌 메시지는 이번 응답 내부와 기존 사전/현재 구간 사이를 구분하고 종류·위치·변경 필드를 표시한다. 선언되지 않은 관계 타입도 관계 ID·타입 필드를 명시한다.

사전 생성 복구 계약에는 `merge_contract` 버전이 포함된다. 이전 처리 규칙의 부분 기록은 호환 검사에서 제외되어 새로 시작할 수 있다. 완료된 사전·승인 기록을 다시 쓰지 않으며 `source-spans-v1` 진행 표식과 기존 0회 호출 사용량 해석은 유지한다.

## 승인 사전 기반 추출 점검

`EXTRACTION_CONTRACT_VERSION`을 `runs.ontology` wrapper에 고정한다. 사전 payload/hash는 변경하지 않는다. 성공 중복 키는 `extraction_key(pin)`의 `(id, revision, extraction_contract)`이며 문서 조회와 실행 생성에 동일한 규칙을 사용한다. 버전 필드 없는 pin은 legacy 0으로 취급하여 현재 방식의 재추출 한 번을 허용한다. 기존 후보·이력은 덮어쓰지 않고 새 pending 후보를 추가한다. 승인 철회·사전 변경·동시 실행·문서 버전 검증은 유지한다.

새 추출 응답은 관계마다 `coverage`의 predicate/fact_indices/claim_indices/status/reason과 최대 12개 `dictionary_gaps`를 포함한다. reason 120자, gap detail 160자, 청크당 facts 80개·claims 12개 제한이다. `extracted`, `no_evidence`, `not_representable`, `limit_reached`를 구분하고 모든 사실의 인덱스와 관계를 검사한다. 보고서 형식·인덱스는 인용 보정 호출 전에 검사하고 인용 보정 후 다시 검사한다. 계약 1/2에서는 점검표 보정이나 보충 추출을 위한 모델 호출이 없다. 계약 3은 [손실 점검](extraction-loss.md)에 따라 관계 0개 구간을 제한적으로 보충하며 초기 점검표와 보충에 따른 로컬 집계를 구분한다. 새 응답은 일반 형식·사전 어휘를 먼저 검증한 뒤, 상태가 이미 extracted이고 실제 같은 predicate 사실이 하나 이상인 행의 fact_indices를 로컬 보정한다. 기본 보정은 기존 인덱스가 행 내 중복 없는 범위 내 정수인지 검사한다. 서비스가 사전 불일치 제외 처리의 반환값으로 제외가 없음을 확인한 새 응답에만 `allow_model_range`를 켜며, 이 경우 실제 배열 범위를 넘는 참조도 최대 80개·값 0~80의 중복 없는 정수라는 한도 내에서 보정한다. 공유 `indices()`와 최종 검증의 실제 배열 범위 검사는 완화하지 않는다. 올바른 기존 참조의 순서는 유지하고 다른 predicate 사실을 가리키는 참조를 제거한 뒤, 누락된 같은 predicate 인덱스를 사실 배열 순서로 추가한다. 모든 비인덱스 필드는 유지하고 전체 엄격 accounting 검증을 다시 통과해야 한다. 상태 추정·사실의 관계 변경·저장된 보고서나 checkpoint 보정은 하지 않는다. 사전 불일치 제외 후 번호를 사용하며, 제외가 하나라도 있으면 기본 엄격 모드로 처리한다. 따라서 제외된 사실에 대한 모순된 참조를 표시하는 범위 밖 인덱스와 실제 모델의 범위 밖 참조를 구분하지 않고 계속 거부한다. 새 응답에서만 `prune_claim_references`로 coverage와 dictionary_gaps의 claim_indices 중 존재하지 않는 번호를 제거할 수 있다. 원래 배열은 최대 12개·0~12의 중복 없는 정수여야 하고, 제거 후 유효한 참조가 하나 이상 남아야 한다. 실제 claims·facts·인용·상태·이유·유효 참조 순서는 바꾸지 않고 대체 연결을 추측하지 않는다. 사실 제외는 claims 번호를 바꾸지 않으므로 이 처리는 사실 제외 후에도 가능하며, 인용 보정에서 claims를 제거하기 전에만 실행한다. 전체 엄격 accounting 검사는 유지한다. 정확한 인용과 승인 사전의 어휘·방향·타입 검증 및 기존 인용 보정(청크당 1회)은 유지한다. 이 검사는 모델 판단의 의미적 타당성이나 recall을 증명하지 않는다. 행 수가 승인 관계 수와 다르거나, 행을 순서대로 검사하다 미승인·중복 관계를 만나면 청크를 중단하고, 오류 상세에 승인 관계 수·응답 행 수·미승인 행 수·중복 관계 수·행이 없는 관계 ID(최대 5개) 중 해당하는 항목을 적고(coverage가 배열이 아니면 승인 관계 수만), 고정 안내 문구를 덧붙인다. 모델이 쓴 관계 문자열은 싣지 않는다. 실행 상세는 먼저 기록된 오류 하나만 보고하므로, 다른 오류(응답 형식·사전 오류, 행 필드 오류, 다른 청크 실패, 시간 초과 등)가 먼저 걸리면 이 수치는 나오지 않는다. 그 응답은 후보로 저장하지 않으며, 다시 추출하면 재사용할 복구 기록이 없는 청크를 새로 호출한다.

새 응답에서 `dictionary_terms`가 `VocabularyError`(`unknown_entity`, `type_mismatch`)를 낸 사실은 `exclude_off_dictionary_facts`가 제거해 `excluded_facts`에 코드, 표기 그대로의 주어·목적어와 관계 ID(앞뒤 공백만 정리), 최대 300자의 인용을 남긴다. 제외한 사실의 인용은 검증하지 않으며 문자열이 아니면 빈 값으로 남긴다. 화면의 주어·목적어별 판정(`exclusion_terms`)은 보고서를 읽을 때 그 실행의 사전 pin으로 계산하며 저장하지 않는다. 응답이 보낸 같은 키는 먼저 버린다. 남은 사실의 fact_indices를 다시 매기고, predicate의 사실이 모두 제외되어 인덱스가 남지 않은 행은 이전 status와 무관하게 로컬 reason의 `not_representable`이 된다. 다른 predicate의 제외 사실을 가리키거나 중복된 인덱스는 지우지 않고 accounting 검증에서 거부한다. 승인되지 않은 predicate와 주어·관계·목적어의 형식 오류는 제외하지 않고 chunk를 중단한다. 이때 그 응답은 번호가 바뀌지 않은 채 preflight로 넘어가고, 계약 v1·v2의 제품 경로는 `skip_off_dictionary`로 제외 대상 사실을 건너뛰므로 제목·위키 주장·사실 개수 오류가 없으면 오류는 사실 중 응답 순서로 처음 걸리는 중단 원인을 응답 번호로 가리킨다. 건너뛴 사실이 남는 경우는 그 원인이 있을 때뿐이라 어떤 경우든 예외가 나고 저장 대상은 넓어지지 않는다. 키워드 기본값은 엄격 검사이고, 계약 0과 오프라인 재현은 건너뛰지 않으므로 응답 순서로 어휘 오류가 먼저면 그 어휘 오류를 보고한다. 제외가 있으면 caller validation은 실패로 기록한다. 개체 참조는 양끝 공백 정리 후 승인 별칭이나 ID와 정확히 일치할 때만 연결한다. 띄어쓰기는 뜻을 바꿀 수 있으므로 공백만 다른 표기도 연결하지 않는다. 새 응답에서는 `unknown_entity`로 제외하고, 재적재·검토 수정·`finish_run`에서는 거부한다. 사람이 같은 대상으로 판단해 별칭을 추가하려면 그 표기와 개체 name을 모두 포함하는 8,000자 이하의 연속 인용이 있거나 `judged_*` 세 키를 가진 형태여야 하고(`validate_dictionary`, 키가 있는 행은 표기·대상·대상 이름이 바뀌면 거부된다. 추가 경로는 인용에 개체 name이 없을 때만 키를 쓴다. 추가 경로와 JSON 편집(`revise`)은 모두 `entity:`·`relation:`으로 시작하는 새 개체 ID·이름·별칭과 줄바꿈·탭이 든 새 개체 ID를 거부한다, #842·#845), 새 revision의 재승인과 전체 청크 재호출이 따르며, 타입 검사는 별칭으로 연결된 개체에 그대로 적용된다. 타입 불일치는 `dictionary-types` 경로로 고치며, 그 진입점은 첫 응답에서 제외한 `excluded_facts`에만 있고 보충 점검 응답의 미해결 후보(`loss_audit`)에는 없다(#878). 저장 사실과 checkpoint 사실은 별칭을 정규 ID로 바꾼 값이다. `excluded_facts`는 비어 있지 않을 때만 checkpoint output과 보고서 청크에 쓰는 선택 키이며, 재적재 때는 제외하지 않고 형식만 검증한다. 모든 청크 검증 뒤 저장할 사실이 없는데 사전 불일치 제외 수가 0보다 크면 `finish_run` 전에 오류로 끝나 revision을 소비하지 않는다. 다른 실패가 없는 일부 제외는 성공이며 상세에 비율과 재승인 후 재추출을 표시한다.

보정 호출 뒤 인용을 확인하지 못한 사실은 `excluded_facts`에 코드 `unverifiable_quote`와 보정을 요청한 응답 기준 `path`로 기록한다. 이 코드에만 `path`가 있으므로 이전 checkpoint의 행은 그대로 유효하다. 주장은 `excluded_claims`에 `path`, heading, 최대 300자 text·quote, 유일한 근거를 잃은 `not_representable` 행의 `predicates`, 근거가 모두 사라진 gap의 `gaps`로 기록한다. 남은 `fact_indices`, `claim_indices`, gap의 `claim_indices`는 다시 매기고, 근거를 잃은 행은 로컬 reason을 받으며 `validate_accounting`은 이 기록이 있을 때만 근거 없는 `not_representable` 행을 허용한다. `claims` 최소 1개 검사는 `excluded_claims`가 있을 때만 완화한다(`allow_no_claims`). 응답이 보낸 `excluded_claims`는 `excluded_facts`와 함께 먼저 버린다. 모든 청크 검증 뒤 남은 주장이 없으면 `NothingToPublish`로 끝나 revision을 소비하지 않는다. 남은 사실이 없어도 사전 불일치 제외가 없으면 주장을 게시하고, 사전 불일치 제외가 있으면 위 규칙대로 끝나며 두 원인을 함께 적는다. 근거 불가 제외가 있는 성공 상세는 그 건수·위치와 재추출 불가를 맨 앞에 둔다.

보정은 원래 응답(제외 전 파싱 결과)과 정규화된 응답(사전 불일치 제외와 보정 후)의 SHA256, 실행/문서/사전 신원, 원문 hash, 청크/offset, 변경 행·predicate·인덱스(사전 불일치 제외 후 번호, 이후 근거 불가 제외로 최종 번호와 달라질 수 있음) 및 가능한 호출 ID를 `runs/<id>/<chunk>/coverage-repair.json`에 최대64 KiB로 원자 저장한 뒤에만 인용 보정이나 checkpoint 확정을 진행한다. 사실 인덱스만 변경하면 기존 schema 1과 변경 행 형식을 유지한다. 설명 참조 제거가 있으면 schema 2로 모든 변경에 collection(coverage 또는 dictionary_gaps)·field(fact_indices 또는 claim_indices)·row·before·after를 명시한다. schema 2 변경 수 상한은 MAX_FACTS + 승인 관계 수 + 12이며 64 KiB 바이트 제한은 동일하다. schema 1 전용 진단 검증기는 schema 2를 사실 보정 기록으로 해석하지 않고 거부한다. 원본 stdout/HTTP 응답은 유지하며, 이 기록은 기계적 참조 보정 기록이지 청크의 사실·인용 검증 성공을 뜻하지 않는다. 감사 저장 실패는 해당 청크를 중단한다. 원래 제공자 응답은 기존 caller-validation 경로에서 실패로 표시하며(지원되는 HTTP 기록에 적용), 보정 후 앱 처리는 성공할 수 있다. 실행 상세의 보정 건수는 감사 저장에 성공한 이번 실행만 세며 이후 청크 실패 시에도 표시한다. 프롬프트·추출 계약·checkpoint schema1은 그대로라 이전에 검증한 청크를 재사용한다. 재사용한 보정의 감사 파일은 원래 실행에 남으며 기존 resumed_from으로 연결한다. 연결이나 기록 누락은 과거 보정이 없었다는 증거가 아니다.

`extraction_coverage.py`는 검증된 청크의 인덱스/판단을 `runs/<id>/extraction-coverage.json`에 최대 2 MiB로 저장한다. 원문 전체와 저장 사실은 복제하지 않고, 제외한 응답 관계는 `excluded_facts`에 주어·목적어·관계 ID와 최대 300자 인용으로, 제외한 위키 주장은 `excluded_claims`에 남긴다. 이 상한을 넘는 쓰기는 버려진다. 그 실행에서 먼저 상한 안에 쓴 파일이 있으면 그 접두 구간이 남고, 없으면(예: 재개 실행의 재사용 청크가 첫 쓰기에서 이미 상한을 넘은 경우) 파일이 없다. 파일이 없으면 실행 결과와 무관하게 unavailable이다. 접두 구간이 남은 경우 성공 실행의 보고서는 청크 수가 맞지 않아 unavailable이고, 실패 실행의 보고서는 그 접두 구간만 집계하므로 실행 상세의 수보다 작을 수 있다. 전체 제외 수는 실행 상세에 표시하되, 상세는 2,000자에서 잘리므로 실패 실행의 오류 문구가 길면 빠질 수 있다. schema/실행/문서 버전/사전 pin과 source 길이, 청크 offset/length, 개수 및 인덱스를 검사한다. symlink/초과 크기/손상/identity 불일치는 unavailable이다. 청크 응답 관계 수(남은 사실과 제외 사실의 합), 사전 불일치 제외 수(`excluded_fact_count`), 근거 불가 제외 수(`unverifiable_fact_count`, `excluded_claim_count`)와 DB의 실제 저장 후보 수(관계와 인용이 모두 같을 때 중복 제거)를 구분한다. 위키 주장 인덱스는 청크별 claim_base로 전역 번호에 대응한다. `read_report`는 검증 뒤 파일에 없는 값(`excluded_fact_terms`, 판정에 쓴 관계별 허용 타입 `excluded_fact_allowed`, 표기·타입 제외 수, 현재 사전 revision과 상태 `current_dictionary_status`, 현재 문서 버전 여부, 실행 ID `run_id`)을 덧붙이고, 판정은 개체 이름에서 ID로 가는 색인(앞뒤 공백만 정리한 표기와 정확히 일치하는 이름만)으로 이름이 같은 개체를 알리되 그 개체로 연결하지는 않는다. 파일은 바꾸지 않는다. 목록 API는 요약만, 실행 상세 API는 구간별 점검을 반환한다.

보고서는 DB 완료 전에 best effort로 저장하며 DB 실행 상태만 게시 여부를 결정한다. 보고서 저장 실패는 후보 저장을 취소하지 않는다. 실패·중단·취소 실행의 보고서는 검증된 접두 구간일 뿐 저장 후보가 아니다. 소유 run을 DB에서 재확인하므로 삭제 이후 파일 정리가 지연되어도 보고서를 노출하지 않는다. 기존 실행의 보고서가 없으면 unavailable이며 0개나 완전 추출을 뜻하지 않는다. 추가 SQL migration이나 승인 자동화는 없다.

## 큰 원본 업로드와 분석 진행률

원본 크기 한도는 32 MiB다. `POST /api/documents`의 JSON 요청 한도만 `ceil(MAX_BYTES / 3) * 4 + 64 KiB`로 확장한다. 다른 mutation의 JSON 한도는 6 MiB를 유지하며 Host/Origin 보호는 공통이다. base64 타입·인코딩 문자열 길이를 디코딩 전에 검사하고 디코딩한 원본 크기를 다시 검사한다. 원본 bytes는 그대로 저장한다. settings의 `document_limits`가 브라우저의 파일 검사/한도 안내 기준이다. 본문128000자, PDF200쪽, DOCX 압축 해제16MiB, 모델 호출/청크/후보 한도는 변경하지 않는다. JSON/base64의 일시적 메모리 복사 비용은 남는다.

브라우저는 FileReader 준비 이벤트와 XMLHttpRequest upload 이벤트를 분리한다. 전송 loaded/total은 전체 요청 바이트이며 업로드 load 후에는 서버 처리 중으로 전환한다. HTTP201만 등록 완료다. 타깃 문서/version/generation과 제목·텍스트는 await 전에 복사하고 진행 중에는 폼과 modal을 잠근다. 오류/timeout/abort에서는 잠금을 해제하고 저장 여부 확인을 안내한다. XHR 응답 대기는180초이며 timeout은 서버 작업 취소를 보장하지 않는다.

`progress.py`는 DB 소유 run/고정 원문 길이와 기존 `dictionary-progress.json` 또는 검증된 extraction coverage를 읽는 선택적 조회 어댑터다. 사전 marker는 최대8192바이트, 스키마/실행/전체 길이/정수 counters/결정적 구간 끝/재사용 범위를 검사한다. 추출은 인용 보정까지 검증된 청크만 센다. 누락·손상·symlink·삭제 소유자는 unknown이고 완료 호출 수나 경과 시간으로 진행률을 추정하지 않는다. DB queued는0%, running은 검증 비율(최대99%), 전 구간 검증은saving, DBsuccess만100%다. 실패·중단은 검증된 마지막 비율과 종료 상태를 함께 표시한다. 진행 기록은 후보 저장과 승인의 조건이 아니다.

문서 상세의 `latest_run`은 선택한 버전의 최신 실행 ID/stage/status/version만 반환한다. run API의 progress는 추가 필드다. 문서 상세와 실행 기록은 run ID별 단일 polling registry를 사용하고 선택 문서/version/최신 실행/요청 번호가 맞을 때만 화면을 갱신한다. terminal, 조회 오류(삭제된 run의 현재 응답은400), 관련 화면 이탈 시 watcher가 종료된다. 재진입은 다시 조회하며 새로고침 이후에는 DB 실행에서 복구한다. 분석 진행률은 native progress 요소와 텍스트로 제공한다. SQL migration이나 유료 호출 추가는 없다.

## Provider transport contract (#704)

`agents.PROVIDERS` retains display names; `TRANSPORTS` identifies `cli|http` and
`CLI_PROVIDERS` identifies the five executable providers. OpenRouter is
`openrouter` / `OpenRouter (API)`. `executable('openrouter')` returns `None`
without searching PATH; installed still means a CLI executable is present.
`command()` accepts CLI providers only. HTTP readiness is the local
`CLI.ready(agent)` contract, separate from installation.
On Windows, Copilot prompts are piped through stdin rather than passed as a
command-line argument, avoiding the Windows process command-line length limit.

`CLI` remains the injectable dispatcher with
`complete(agent, model, prompt, folder, *, phase=None) -> dict` and
`effective_timeout(agent, phase=None)`. `Service(..., cli=fake)` requires no new
fake methods. Construct an HTTP dispatcher with `http_adapter=`,
`credentials=Credentials(server_memory_key)`, optional `sink=` and `options=`.
Credentials are loaded by the server startup integration (#703), never by this
module, and never copied into options, job/checkpoint records, telemetry, or the
process environment. Every CLI child receives a copied environment with
`OPENROUTER_API_KEY` removed; the parent environment is unchanged.

`transports.HTTPAdapter` specifies `ready(credentials)` (local configuration
only) and `complete(..., phase=, options=, credentials=, call_id=)`.
`ExecutionOptions` is frozen and `dataclasses.asdict(options)` contains only
`adapter_contract_version=1`, `max_tokens=16384`, and `timeout` in seconds.
`CLI.execution_options(agent, phase)` captures these defaults. The caller can
bind that exact snapshot to a task-local dispatcher with `options=snapshot`;
it is passed unchanged to each adapter call. A conflicting constructor timeout
is rejected. Without a pinned snapshot, ontology calls use 600 seconds and
other OpenRouter calls use 180; explicit timeout overrides (including zero)
win. Existing CLI defaults, including Antigravity's 600 seconds, are retained.
Job persistence/resume binding is described in #707 below. Models retain the common 150-character/character validation;
OpenRouter additionally requires a nonempty trimmed model. No catalog or model
substitution is performed.

The trusted HTTP adapter (#705) returns `HTTPResult(envelope, response)`.
`response` is domain response text, parsed by the dispatcher into a dict.
The `SafeCallResult` envelope carries the dispatcher's unique `call_id`, terminal
`transport_status` (`success`, `error`, `timeout`, `not_started`), bounded,
secret-checked `safe_metadata` (including safe usage evidence), and optional
`safe_response_payload`. The adapter must perform size and secret checks before
returning any response text or metadata, sanitize exceptions, and return a
terminal envelope for expected transport failures. `success` means a complete
HTTP response; invalid domain JSON can still have transport success. Only a
complete, size/secret-checked response may set `response_verified=True` and
provide a payload. Partial, truncated, unverified, and failed transport responses
must omit the payload. These are trusted adapter attestations, not a replacement
for the actual checks in #705.

The dispatcher sends the envelope to `sink(envelope)` before domain JSON parsing,
so usage evidence survives domain parsing failure. Sink exceptions are ignored
to preserve the primary response/error. The domain return value never includes
the envelope. Sink file storage is owned by #706; the HTTP adapter creates no
files or subprocesses; dispatcher recording is described in #706 below. `tests/test_transports.py::FakeHTTP` is the synthetic
fixture for this boundary; it exercises safe success, parsing failure, and
terminal failures without external API calls. API/UI availability is #708.

## Bounded OpenRouter HTTP adapter (#705)

`openrouter.OpenRouter(max_output=1048576)` implements the trusted HTTP boundary.
Inject it explicitly into `CLI(http_adapter=..., credentials=..., sink=...)`.
`transports.Credentials.from_server(config_credentials)` bridges the already
loaded #703 memory credential. This adapter never loads files or environment
keys, writes logs, or normalizes usage. Startup/pipeline wiring is described in #707 below.
`CLI.max_output` applies through a per-call adapter copy using the smaller of
the CLI and adapter limits; shared adapter state and fake adapter signatures
are unchanged. `ExecutionOptions` supplies pinned timeout/max_tokens.

The adapter performs exactly one direct verified-TLS POST to
`https://openrouter.ai/api/v1/chat/completions`, with only model, a user prompt
message, `stream=false`, and max_tokens in the JSON body. It refuses redirects,
compressed responses, retries, extra lookups, application fallback, and custom
endpoints/headers. OpenRouter's default upstream routing remains unchanged.
The request/response shape follows the [official API reference](https://openrouter.ai/docs/api_reference/overview);
[HTTP errors](https://openrouter.ai/docs/api_reference/errors-and-debugging)
map to static diagnostics, never provider messages.

A monotonic deadline covers configuration, DNS/connect/TLS, request transmission,
headers, incremental body reads and validation. A bounded daemon worker owns the
connection; the caller stops waiting at the total deadline, marks cancellation,
and shuts down its retained socket (including after the HTTP response takes ownership). Connect/send guards prevent a delayed resolver or
handshake from sending credentials after cancellation. Python cannot terminate
an operating-system resolver: a stuck resolver retains one of eight worker
slots until it returns. Busy capacity fails before sending instead of spawning
unbounded threads. Timed-out workers cannot publish late results. Owned
connections close when their worker exits. A local deadline neither proves
remote cancellation nor that a transmitted request was unbilled.

Both success/error bodies are read incrementally with the output bound plus one
byte, independent of advertised Content-Length. Requests ask for connection close;
nonchunked responses are read to EOF and any declared-length mismatch fails.
Chunked framing is handled by the standard library. Unsupported Content-Encoding
fails without a payload. `MAX_METADATA` (256 KiB) separately caps extracted safe
metadata; oversized or invalidly typed usage is omitted; documented `is_byok` booleans and nullable `server_tool_cost` are retained, while a verified complete
payload can still fit the larger body limit.

A response must contain exactly one choice, nonempty string content and finish
reason `stop`; provider errors, refusal, native tool calls, abnormal finish,
reasoning-only output and invalid/nonobject domain JSON fail. Domain dictionaries
still need the caller's ontology/evidence validation. The adapter checks raw
strings and decoded JSON keys/string values (including JSON unicode escapes and
embedded domain objects) for the known key before returning content, payload or
usage. Outbound prompts/models containing the known key are blocked before
transmission. This detects known plaintext/JSON escapes, not arbitrary encodings.
Untrusted exception strings and parser documents/chains never cross the boundary.

`HTTPResult.error_code` selects a static diagnostic after sink delivery.
A complete checked HTTP exchange uses transport status `success` even for HTTP
401/429/5xx or invalid domain content: `safe_metadata.validation_status` and
`error_code` distinguish provider/domain failure from transport completion.
`transmission_state` is `not_sent`, `sent_unknown`, or `received`.
`sent` becomes true just before the first HTTP request byte, after DNS, TCP and TLS.
Usage lists a call that ended `not_sent` with transport status `not_started` or `timeout`
as not started and leaves it out of the totals; `timeout` with `sent_unknown` is counted.
Safe metadata includes HTTP status, requested/resolved model, validated `gen-`
generation ID and raw finite numeric usage with documented boolean/nullable fields when present. Invalid content can
retain verified usage and payload. Key echoes, oversized/incomplete responses,
and unverifiable JSON have no payload or response-derived usage. No request,
headers, raw exceptions or unchecked text are sent to the sink. #706 owns safe
payload storage and usage normalization. Offline fixtures in
`tests/test_openrouter.py` include socketpair HTTP framing and real-time slow
header/body and delayed-connect cancellation tests, without paid API requests.

## OpenRouter call recording and accounting (#706)

The HTTP dispatcher uses `http_records.HTTPRecord` as the sole process-local
writer of `run.json`, `usage.json`, and optional `response.json` in the existing
call folder. It writes no stdout, prompt, request headers, or fake subprocess
return code. The transport call ID is `provider_call_id`; public `call_id` stays
the existing path-derived discovery ID. One call folder owns one provider ID.
Reusing a populated folder fails before another transmission. `run.json` is
authoritative for identity and caller-validation state; consumers reject usage
with an absent, unreadable, or mismatched HTTP run identity. Individual files
are atomically replaced, not a multi-file transaction; partial recording is
reported as incomplete rather than combining calls.

The recording context pins requested model, phase, prompt hash, timestamps,
timeout, max_tokens and the current rate snapshot. Known-key checks happen
before context construction: a secret-bearing model, prompt or rate snapshot
is omitted (including the prompt hash), while the adapter still rejects the
unsafe request. The recorder receives no credentials. `response.json` is
written only from the optional complete checked payload attested by #705;
never from generated content or unchecked text. It remains local and is never
read by usage APIs/downloads. Run/usage records are capped at `MAX_METADATA`;
payload serialization has a bounded budget of `6 * max_output + 1024` bytes
for JSON escaping/formatting. All writes use no-follow directory descriptors,
exclusive random temporary files and private permissions. Supply call paths
under a trusted canonical data root; symlink parents/destinations are rejected.
Optional recording failures preserve the domain result/original error, with a
static `usage_warning` in run metadata when that file remains writable.

Transport completion (`transport_status`), reception/transmission uncertainty
(`transmission_state`), adapter JSON validation (`adapter_validation_status`)
and subsequent caller/domain validation (`caller_validation_status`) are
separate. A valid dict leaves caller validation pending, never automatically
approved. #707 can use
`CLI.update_caller_validation(folder, provider_call_id, 'success' | 'failed')`
or `http_records.update_caller_validation` without directly rewriting records.
The shared writer lock serializes read/compare/write operations in one process.
The API reconstructs from run metadata, returns false for stale/unknown IDs,
unreadable records or optional write failure, repeats the same terminal status
idempotently and rejects conflicting final status. It accepts no free-form
error text. Updating validation does not change token/cost evidence, transport
status, timestamps or the discovered call count. #707 below connects actual notifications
and resume integration; no cross-process concurrent writers are supported.

`usage.decode_http` consumes only secret-checked top-level API usage from safe
metadata (or the checked payload when extraction omitted it), bounded separately
to `MAX_METADATA`. It never reads message content for usage. Prompt tokens map
to input including cache; completion tokens map to output including reasoning.
Cache read/write and reasoning are subsets, not additive totals. Missing fields
stay null; reported zero stays zero. Invalid numbers, cache subsets above input,
reasoning above output and inconsistent reported totals prevent fabricated
completeness or estimates while independently confirmed cost is retained.
`usage.cost` is `reported_credits` in `OpenRouter credits`; `reported_usd` stays
null and upstream inference cost is not added. User-rate USD estimates retain
the existing pinned model/snapshot rules. Reported and estimated amounts remain
separate summary provenance. Existing CLI telemetry, discovery (including quote
repair/questions) and resumed-segment exclusion remain unchanged.

## Registered OpenRouter jobs and domain validation (#707)

Server startup passes the already-loaded credential object into `Service`.
A missing key leaves CLI jobs usable. OpenRouter registration locally checks
credentials/adapter readiness, required model and secret-free input/options/rates
before acquiring a queue slot or creating a job. This is configuration readiness,
not authentication, balance or model-access verification. No network probe runs.

Each v1 HTTP job stores `execution_options` containing only
`adapter_contract_version`, `max_tokens`, and `timeouts.default`/`timeouts.ontology`.
Explicit answer-headroom requests use v2 and additionally pin
`answer_reserve_tokens` and `reasoning_max_tokens` (described below).
A nullable `runs.execution_options` column is added by the existing idempotent
column migration; legacy CLI rows retain null. Questions use their existing
JSON payload. The strict option allowlist rejects malformed/missing OpenRouter
snapshots before any resumed transmission. No credentials or key hashes are
stored. Backup/restore of the SQLite file remains the rollback path; migration
is additive and preserves old rows. Startup recovery marks queued/running jobs
interrupted, ends cancelling runs as cancelled, and preserves their snapshots; it never automatically retransmits.
It also sets the running or analyzed split-processing rounds of each question
stored as cancelled, interrupted or error to cancelled, interrupted or failed.

Registration also captures a job-local dispatcher: adapter, credentials, sink,
output limit and a copied rate snapshot. Agent/model and persisted options are
copied before queueing. Every dictionary call, extraction chunk, quote repair and
question round uses that capture, with the appropriate fixed phase timeout;
subsequent settings or dispatcher-default changes do not alter the queued job.
Existing fake CLI complete signatures remain unchanged. Direct worker invocation
reconstructs HTTP options from the stored job and fails closed on absent/invalid
snapshots. Explicit retries are new jobs and use their own registration snapshot.

Domain consumers use `call_validation.caller_validation` only after `complete`
returns. The helper reads the authoritative bounded call record for its ID and
notifies #706's single writer; it never edits run metadata or uses a global last
call ID. Dictionary span resolution and merge, extraction page/fact/coverage
checks, and each question tool/answer policy boundary must pass before caller
success. A quote-invalid original response stays failed even when a separate
repair succeeds or every problem quote is located with no repair call; repairs
are validated and counted separately. Optional telemetry
failure cannot replace a domain result/error. Approval and fixed-snapshot evidence
rules remain unchanged, as do repair and question budgets. HTTP errors add no
retries or model fallback.

Only OpenRouter dictionary checkpoint contracts include the complete normalized
execution option snapshot (including both timeouts). Mismatches skip reuse with
a static explanation. CLI checkpoint serialization stays unchanged. Matching
options support reuse of validated progress, not model-output reproducibility.
Previously paid segments are not copied as new calls; fully resumed checkpoints
can complete with zero new calls. Local tests exercise the real HTTP adapter via
network-free fixtures through dictionary approval, extraction review/approval and
snapshot-backed questions; no paid request is part of validation.

## Public provider settings and execution guidance (#708)

`GET /api/settings` returns provider `id`, `name`, and `transport`. CLI rows
retain `installed` (executable presence); OpenRouter returns only `configured`
from startup credential presence, with no executable discovery, key value,
fragment, hash or actual credential path. Configured does not verify auth,
balance or model access. `PUT /api/settings` accepts exactly selected/models,
validates original strings before trim, and requires a nonempty OpenRouter model
when selected as default. Other defaults may retain an empty OpenRouter model.
No key is needed to read/save settings; Service rejects unconfigured execution
before creating jobs. Existing CLI models and missing model-map entries retain
their meanings; no DB schema change is introduced.

Settings, extraction/dictionary and question controls share transport-specific
status, model placeholders/requiredness, and disclosure updates, including
settings reload and preparing a historical question again. HTTP prompts go to
OpenRouter and its routed model provider and may incur charges. No model catalog,
key input/save API, browser credential storage, or connectivity probe exists.
The setup panel explains init-config, editing the server-local file, POSIX
ownership/0600, restart and the native Windows fail-closed policy (use POSIX/WSL
or keyless CLI). Model and missing-key errors remain server-authoritative.

HTTP failure panels preserve the static error and explain local timeout or
disconnect is neither remote cancellation nor proof of no billing. Manual rerun
can be charged again. For length/model-limit errors, the user can change the
explicit model ID. Dictionary/extraction controls also expose a per-job token slider. For
programmatic server construction the existing `CLI(options=ExecutionOptions(
max_tokens=..., timeout=...))` sets a fixed token cap and explicit timeout for
all phases. Dictionary/extraction POSTs accept an optional `max_tokens` integer
override from 1 through 65,536 for OpenRouter only; omitted values retain the
server default, including historical defaults outside that UI range. Explicit
nulls, booleans, non-integers and CLI overrides fail before registration. The
advanced-control checkbox defaults off; the slider applies only when explicitly
enabled. The captured job options fix that value across all chunks and quote
repairs, and existing checkpoint comparisons reject changed token limits. No DB
schema or question API changes are introduced. The range is an application input
bound, not a guarantee of model support or a whole-document cost cap.
`GET /api/settings` publishes the bounds and effective dispatcher default through
`extraction_token_limits`. Defaults without a programmatic timeout override
retain ontology600/other180 seconds. CLI defaults use 600 seconds for
Antigravity and Copilot, in addition to the 600-second ontology phase.
Approved-dictionary extraction defaults to 360 seconds per chunk across the initial
call and any quote repair, including jobs with larger explicit timeouts.
Shorter explicit timeouts remain effective; ordinary provider defaults do not
shorten the extraction budget. Timeout-only changes do not invalidate validated
checkpoint content on the default path. A Claude run registered with a concurrency
or threshold policy option, even with one worker, also binds its concurrency
settings, timeouts, binary path, output limit, rates and document generation, and a
record under a different contract is not reused.
Validated chunk prefixes are persisted in `extraction-checkpoint.json`, bound
to source, dictionary revision/hash, provider/model, prompts, HTTP token/adapter options
and, for runs registered with a concurrency or threshold policy, the contract above.
Failed/interrupted/cancelled jobs can reuse a compatible prefix after revalidation;
partial results remain unpublished until the final DB transaction succeeds.
Register a new job after any adjustment;
queued jobs retain their old snapshot. No automatic resend or fallback is added.

Usage screens use provider-reported cost labels for CLI/API, keep reported
OpenRouter credits separate from configured USD estimates, and retain unknown
versus zero and provenance separation. OpenRouter rate hints ask for an explicit
matching model rather than a CLI default assumption.


## Cross-module verification and live smoke (#709)

`tests/test_openrouter_integration.py` starts the real loopback HTTP handler with a private file loaded by the production config loader. Settings, source registration, dictionary approval, extraction/quote repair, separate review, question tools, history and download JSON go through those handlers. Only the outbound HTTPS connection is replaced by ordered fixtures; a socket guard forbids external network. No private user file is used. The fixtures verify pending/approved graph boundaries, individual caller verdicts, model/options/deadline pinning, failure-to-resume billing, incompatible checkpoints, safe/unknown failed usage, and no key/hash persistence. Large fixtures use 183 entities and 80 facts with the actual initial request cap 16,384; this verifies application boundaries, not any model's real capacity.

`python -m scripts.openrouter_smoke` is a separate opt-in operator procedure. Explicit `--allow-network`, `--config`, and `--model` are mandatory before credentials are loaded. It sends exactly one fixed synthetic JSON request with 256 output tokens and a 60-second total deadline. It prints allowlisted adapter-attested metadata and an expected-object boolean, never raw response/errors/headers/configuration. It fails if the expected object is absent; no retry, fallback or generation lookup occurs. Default tests mock this procedure and require no network or real key.

The verification matrix in `docs/verification.md` maps umbrella #702 and children #703–#709 to tests and actual desktop screen evidence. Live smoke success is independent from offline approval/pipeline correctness. Native Windows credential loading is deliberately unsupported; no actual Windows ACL execution or arbitrary-model long-output suitability is claimed.

## Installed CLI model catalogs

`POST /api/providers/<cli-id>/models` accepts an empty JSON object through the existing loopback Host/Origin guard. Only five CLI IDs are accepted; OpenRouter and arbitrary flags/paths are rejected before spawning. The server's process-local `ModelCatalog` allows two concurrent inventories and one per provider. No catalog or raw output is persisted. UI discovery is lazy on provider name/radio clicks, including an already selected provider. A separate selector copies an ID into the editable field without saving or changing input during loading. A render generation guard drops stale responses; pending same-provider requests share a browser promise. CLI defaults/manual IDs remain available, and OpenRouter has no selector or discovery request.

Adapters use actual installed capabilities, never hardcoded model names: `agy models` (tab-separated), `cursor-agent --list-models` (`id - label`), Claude stream-json control `initialize`/`models`, Copilot headless stdio framed JSON-RPC `connect` then `models.list`, and Codex app-server `initialize`/`initialized` then paginated `model/list`. No user message, turn, inference or session creation is sent. Catalog availability reflects what the CLI advertises, not a guarantee of model/account access. Unknown CLI versions or authentication failures produce static unavailable guidance and leave manual entry usable. Codex pagination is capped at ten pages, catalogs at 500 entries; IDs use the existing 150-character model validation and labels are bounded/escaped.

Fixed shell-free arguments run in an empty temporary working directory with `OPENROUTER_API_KEY` removed. Claude uses no tools, strict MCP configuration, no setting sources, disabled hooks and no session persistence. Copilot disables auto-update. All five CLI protocols are supported on POSIX and Windows. POSIX process groups or Windows process trees are killed/reaped after inventory, including success; Windows uses reader threads because selectors cannot monitor subprocess pipes there. One 20-second budget reserves time for cleanup; stdout is limited to 1 MiB and discarded stderr to 256 KiB across initialization/notifications/pagination. Raw errors/account/protocol fields never enter API results; only normalized ID/name pairs and static status messages do.

Protocol references: [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), [Copilot SDK client](https://github.com/github/copilot-sdk/blob/main/python/copilot/client.py), [Codex app-server model/list](https://developers.openai.com/codex/app-server#list-models-modellist). CLI help and actual read-only inventories were also checked locally; no paid generation was needed.


### Requested reasoning budget and answer headroom

New document UI dictionary/extraction submissions include `answer_reserve_tokens`
R = min(8192, floor(T/2)), even when the total-token override is off. The protected
UI range starts at 2048; a smaller server default is shown as an actionable error
rather than silently raised. Legacy API overrides remain 1..65536.

An explicit R opts into adapter contract v2. R must be an integer >=1024 and leave
at least 1024 tokens for reasoning. The pinned budget B is min(4096, T-R). The
strict v2 snapshot adds `answer_reserve_tokens` and `reasoning_max_tokens` to the
three v1 keys. Missing, extra or inconsistent fields are rejected. ExecutionOptions
keeps its original positional arguments and adds optional budget fields; v1 cannot
carry them. Legacy API omission preserves v1; existing v1 jobs and ordinary
questions/CLI remain unchanged. No DB migration is needed.

Every v2 call sends `reasoning: {max_tokens: B}` and
`provider: {require_parameters: true}`; it does not also send effort, enabled or
exclude. OpenRouter may map a numeric budget to effort for some models. R is
therefore planned answer headroom and B a requested budget, not evidence of a hard
cap, reserved visible tokens or successful output. The application does not claim
per-model numeric-budget capability. Requiring parameters can reject a provider
that previously accepted a v1 request; failure does not trigger a settings-free
retry or model change. These routing semantics are part of v2.

The registered T/R/B/version survives queued work, phase selection, limited-timeout
copies, all chunks and quote repair. Request policy is recorded separately from
reported token usage and exposed through bounded allowlists. A version or policy
change invalidates checkpoint reuse; timeout-only extraction changes retain the
existing exception. Length termination is an explicit token-limit error and keeps
safely reported usage; reasoning content is never substituted for the final answer.

References: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens and
https://openrouter.ai/docs/guides/routing/provider-selection#requiring-providers-to-support-all-parameters


### Evidence exclusions during dictionary generation

`dictionary_filter.merge_generated_dictionary` is generation-only. Existing
strict resolve/merge/approval functions retain their behavior. A shared definition
validator first checks every wire row's shape, values and declared alias targets.
Malformed references/fields, unknown original alias targets and resource limits
remain fatal. Missing/reversed source references, grounding failures and undeclared
relation types are excluded. Spacing-only model aliases (`alias_spacing_only`) are excluded. Semantic definition and namespace conflicts are
reconciled or retained as deferred proposals rather than aborting the document. A new alias
whose entity was excluded and a new relation whose type disappeared are excluded
as dependencies; surviving previous entities or same-type entities prevent that
cascade. The retained draft is strictly validated after generation-only reconciliation (see below).

A chunk commits its dictionary, processed offset and exclusion state together.
Fatal chunks publish none of them. Exclusions add no model calls. HTTP domain
validation is marked failed for a partially rejected response even if generation
continues with retained rows; transport success and candidate publication remain
separate. Only complete document processing publishes a pending dictionary.

`dictionary-exclusions.json` stores run/document/version/source identity, ordered
source chunk boundaries, exact per-kind/direct/dependent counts, and the first
500 detail records (kind, response row, bounded identifier, static reason). The
512KiB bound fits within the existing checkpoint margin; full responses and source
quotes are not copied. Missing, truncated and zero-exclusion reports are distinct.
Readers enforce identity, boundaries, count consistency, size and symlink checks.
The current dictionary item is connected to its origin run without a DB change.
Warnings shown with an edited dictionary describe generation-time exclusions.

The original filter used checkpoint schema 2 with filter_policy=1. Current generation writes schema 3 with filter_policy=6 and reconciliation_policy=1, embedding both audits.
Policy 6 excludes otherwise valid ordinary rows missing only quote_ref with the existing reference audit reason; supplied malformed references and revisions remain strict. Validated schema-3 policy-5 prefixes are reusable because the new behavior only handles previously failing rows; policies 4 and earlier remain incompatible. Old checkpoint files are not rewritten.
Resume inherits exclusions without double counting or charging old calls. Strict
legacy schema 1 is compatible only when its original full contract and dictionary
validate; zero-exclusion chunk state is synthesized for its prefix. Unknown policy
versions or damaged audit state force a fresh start. The existing progress marker
format stays unchanged; processed characters do not imply semantic completeness.
Optional sidecar failure warns without invalidating accepted data. A valid
checkpoint may expose a known prefix count while missing details remain explicitly
unavailable. The existing explicit approval and strict final validation are kept.

### 반려 사전 재생성

사전 재생성은 새 item을 추가하지 않고 반려된 ontology item을 새 pending revision으로 교체한다. 시작 요청의 regenerate `{id, revision}`은 rejected 대상과 정확히 일치해야 한다. ontology 단계의 runs.ontology에는 재생성 대상 ID/revision/hash/이전 run_id/document generation을 고정한다(초기 생성은 null). extract 단계의 승인 사전 snapshot과 stage로 구분하며, item.dictionary는 extract 출처에만 붙인다. 완료 트랜잭션은 대상과 문서 토큰을 재검사하고 payload/revision/run_id/감사 이벤트/성공 상태를 함께 저장한다. 실행이 대기·처리 중이면 새 원문 버전 등록이 거부되므로 문서 토큰 재검사는 저장소 밖 변경에 대한 마지막 방어선이다. 기존 이벤트는 이전 내용을, regenerated 이벤트는 이전·새 run 연결을 보존한다. 위키·사실과 이전 추출 snapshot은 변경하지 않는다. 복구 후보는 LIMIT 전에 동일 target으로 제한하고 checkpoint 계약에도 target을 포함한다. 현재 사전의 제외 경고는 새 run에서 조회하며 과거 run의 경고 파일은 유지한다.

### 관계 타입 개수 경고

`[dictionary].relation_type_warning_threshold`는 기본20,1~200 정수의 경고 기준이며 데이터 절단 한도가 아니다. 공통 검증은 20개 상한만 제거하고 선언/형식/충돌/전체 크기 검증을 유지한다. 각 관계의 주어·목적어 타입을 따로 집계하며 누적 사전의 관계별 2필드 경고를 사전에서 결정적으로 계산한다. 별도 경고 파일 없이 원자적으로 반영된 checkpoint 사전에서 재계산하므로 실패 구간이나 재개로 경고가 중복되지 않는다. 작업 등록 시 Service 숫자 설정을 캡처하고 checkpoint 계약에 정책/기준을 고정한다. 이전 checkpoint는 기본20일 때만 명시적으로 호환한다. 실행 API는 신원/원문 hash/offset/사전 검증 후 당시 기준으로 계산하고 누락·손상은 미확인으로 표시한다. 현재 사전은 현재 서버 기준을 명시해 재평가하므로 설정을 바꿔도 과거 실행 기록의 기준을 바꾸지 않는다. 경고만 있는 응답은 caller validation 성공이며 자동 재호출하지 않는다.

### 기존 개체와 새 별칭 이름 충돌

생성 필터 v6는 구조·참조 preflight와 근거 제외를 유지하면서 의미 충돌을 보류 제안으로 기록한다. 명시적 revisions는 구간 시작 시점의 불변 before와 비교하고, 모든 변경·의존 항목이 함께 엄격 검증될 때만 한 묶음으로 반영한다. 개체 ID를 가리키는 명시적 alias는 같은 타입·양쪽 이름의 근거가 있으면 연결 성분 단위로 중복 개체를 정규화한다. 순환·경쟁 대상·조정과 겹친 대상·유효하지 않은 의존 별칭은 보류하며 문자열 유사성으로 통합하지 않는다. v5부터 모델이 제안한 별칭이 대상 개체의 ID·이름이나 같은 개체의 다른 별칭과 공백(스페이스·탭·줄바꿈, 런 폭 차이 포함)만 다르면 그 행에서 바로 드러나는 별칭과 통합 제안은 그 행만 제외하고(`alias_spacing_only`), 여러 행이 함께 통합되어야 드러나는 경우와 `revisions`의 대상 변경·이름 변경·정규 이동은 묶음 단위로 보류한다. 묶음 단위로 보류할 때는 같은 묶음의 공백 차이가 없는 행도 보류되며 이유가 이를 밝힌다(`revisions`는 조정 내역의 이유에만 나타난다). 별칭이 대상의 `name`과 정확히 같으면 유지하지만, 그 `name`이 대상의 ID와 공백만 다르면 제외한다. `revisions`로 개체 이름을 그 개체 ID와 띄어쓰기만 다른 표기로 바꾸면(한쪽에 줄바꿈이 있는 자리와 공백 폭·종류만 다른 자리는 차이로 보지 않는다) 묶음 단위로 보류하며 이유는 `SPACING_RENAME_REFUSAL`이다(같은 묶음이 별칭의 공백 차이도 새로 만들면 `SPACING_REFUSAL`). 필터 정책은 올리지 않았으므로 이어받은 v5·v6 구간의 이름은 이 규칙으로 다시 검사하지 않는다. 통합되어 사라진 개체의 이름과 공백만 다른 표기가 이후 청크에서 같은 대상에 들어오는 경우는 그 이름이 사전의 ID·이름·별칭에 남지 않아 막지 못한다(#884). 공백을 뺀 비교는 거부와 표시에만 쓰며 사람이 직접 추가하는 경로는 막지 않는다. 모델이 만든 개체 행의 ID와 이름이 띄어쓰기만 다르면 거부하지 않고 표시만 하며, 추출 사실의 주어·목적어 표기가 인용에 있는지는 검사하지 않는다(#885).

새 schema 3 체크포인트에 filter_policy=6와 reconciliation_policy=1, 적용/보류 감사 기록을 사전과 함께 저장한다. v1/v2는 기존 정책·허용된 이전 프롬프트·원문·옵션·재생성 대상을 검증한 뒤 재사용하며 이전 구간의 조정 이력은 미확인 범위로 표시한다. 기존 체크포인트·승인 사전·snapshot을 다시 쓰지 않는다. 이전 정책(v4 이하)으로 저장된 schema 3 체크포인트는 재사용하지 않으므로, 정책 v6로 올린 뒤 실패한 생성을 다시 시작하면, 호환되는 다른 기록이 없을 때 모든 구간을 처음부터 다시 호출한다. API/UI는 실행 출처별 변경 전후와 근거를 제공하며, 최대 20개 상세와 전체 건수·생략 건수를 구분한다. [상세 조정 계약](dictionary-reconciliation.md)을 참조한다.

### 추론 예산 관측 진단

기존 v2 run.json 요청값과 동일 call ID로 검증된 usage.json 토큰으로 읽기 시 진단을 계산한다. 로그·checkpoint·요청 body·실행 옵션 계약은 바꾸지 않는다. 출력과 추론이 유효하고 추론≤출력일 때만 비추론 차이와 예산 초과를 계산한다. v1은 비교 미적용, 누락/모순은 미확인이다. 시작하지 않은 호출(`call_started` false)에는 진단을 붙이지 않는다. run.json이 요청을 보내지 않았다고 기록해도 usage.json이 없거나 받아들이지 않아 시작한 호출로 세는 호출은 진단을 유지한다. 요약 비추론 출력은 호출별 유효 차이를 합산하며 부분 추론 합계를 전체 출력에서 빼지 않는다. 성공 응답의 초과는 관측 경고이고 length 실패는 그대로 유지한다. 원시 reasoning/메시지/provider 오류를 공개하지 않는다. UI에는 동적 router와 숫자 예산→effort 변환 가능성을 안내하고 명시적 모델/지원 확인을 유도하되 조회·준수 보장을 주장하지 않는다.

### CLI 단일 호출 시간

CLI 경로는 stdout 파일과 기존 polling을 유지한 채 별도 monotonic_ns 관측을 기록한다. run/usage/timing의 cli_call_id가 같은 경우만 연결하며 토큰·비용은 기존 기록에서 한 번만 집계한다. 계측 실패는 결과·예외·timeout/kill 동작과 분리하고 HTTP 실행은 변경하지 않는다. 필드·오차·null·구간 중첩 계약은 [cli-timing.md](cli-timing.md)에 정의한다. 문서·청크·인용 수정 집계는 이 레이어 밖에서 수행한다.

### CLI 승인 사전 추출 시간 집계

등록 직전 단조시계 기준점을 worker에 전달하고 CLI extract에만 선택적 span을 붙인다. 8000자 순차 처리/청크당 보정 1회/공유 deadline/전체 성공 후 후보 등록과 checkpoint 계약은 그대로다. orchestration 호출 span은 #724 cli_call_id로 연결하되 그 내부 시간·사용량을 중복 합산하지 않는다. 재사용 청크는 과거 시간 null, 현재 restore 검증은 별도다. 파일·신원/경계/nesting 검증·비가산 부모와 leaf 차감 계약은 [extraction-timing.md](extraction-timing.md)에 정의한다. UI·API나 DB 변경은 추가하지 않는다.

## Claude 프로세스 수명

`Service`는 실행별 `CLI.fork_job()`으로 독립 Claude worker manager를 만들며, 같은 실행 안의 timeout 제한 복사본만 그 manager를 공유한다. 매 요청의 대화 초기화와 결과 수신을 잠금으로 직렬화하며, 요청별 deadline에는 잠금 대기도 포함된다. 모델·실행 파일 변경 또는 실패 시 프로세스를 폐기한다. `Service.close()`는 작업을 마친 뒤 CLI를 닫고 서버 시작 실패·정상 종료에서도 호출된다. 프로토콜·사용량·시간 기록의 상세 계약은 [Claude 프로세스 재사용](claude-worker.md)에 있다.

새 사전 문맥 표 v2는 종류별 고정 열과 원래 문자열 값으로 된 행, 전체 정의의 canonical SHA-256을 포함한다. 문자열 참조 표를 제거하여 개체·별칭·관계 ID를 전송 참조 번호와 혼동하지 않도록 한다. 타입 배열도 원래 문자열을 유지하며 숫자처럼 생긴 ID도 문자열 그대로 전달한다. 기존 v1의 0기반 문자열 참조는 과거 기록을 읽을 때만 엄격하게 복원하고 새 요청에는 사용하지 않는다. 응답 숫자를 ID로 추측하여 바꾸지 않는다. 표를 원래 정의와 동일하게 복원한 뒤에만 전송한다. 작은 요청은 기존 JSON을 유지한다. 직전 고정 문맥 안내 프롬프트의 schema-3 체크포인트는 명시한 해시와 기존의 모든 계약·출처·보고 검증이 맞을 때만 재사용한다. 새 전송 표현과 전환 기준은 완료된 사전의 의미를 바꾸지 않는다.


### 파일별 동시 실행

`JobScheduler`는 사전 생성 2개, 승인 사전 기반 추출 2개, 질문 1개를 별도 실행 한도로 관리한다. 대기·실행 합계 8개 제한은 유지한다. 대기 중인 실행과 질문의 Future는 취소할 수 있으며, 취소 중인 이전 실행이 가진 문서 lease는 호출·게시/실패 처리·호출자 정리가 끝날 때까지 유지한다. 같은 문서의 재실행은 기다리지만 실행 가능한 다른 문서는 건너뛰어 배정한다. 작업자 시작 실패 시에는 작업을 큐에 넣지 않아 나중에 실행될 수 없다. `pool.wait_idle()`은 모든 단계의 완료를 기다리며 `pool.shutdown(wait=True)`은 등록을 닫고 모든 단계를 drain한다. 빈 작업 하나의 완료는 다른 단계의 완료를 뜻하지 않는다.

등록된 병렬 추출의 전용 transport는 run ID별 소유자를 사용하며 public benchmark용 singleton과 공유하지 않는다. 실행별 로그·checkpoint·고정 옵션·취소 상태·게시 검증은 그대로 유지한다. Service는 실제 CLI의 job fork와 `extraction_caller_factory`가 반환한 호출자를 실행 종료 때 닫는다. 직접 주입한 `extraction_caller`와 임의 fake CLI는 빌린 객체로 취급하므로 복제하거나 실행 종료마다 닫지 않는다. factory는 매번 독립적인 소유자를 반환해야 한다. 닫기에 실패한 소유자는 서버 종료 때 재시도하며 다른 실행의 정리를 건너뛰지 않는다.

run API의 queue는 `stage`, `capacity`, 같은 단계의 `ahead` 작업과 문서 lease의 `document_blockers`를 제공한다. 목록은 순간 관측이며 순서대로 모두 끝나야 한다는 약속이 아니다. 다른 단계·질문은 문서 단계의 용량을 소비하지 않는다. 두 추출 파일이 각각 청크 병렬 2를 켜면 추출 호출 4개와 사전 생성 호출 2개, 질문 호출 1개가 겹칠 수 있다. 파일 수 제한은 전체 모델 호출 수나 제공자 할당량 보장이 아니다.

계약 3은 `extraction_loss.py`의 필수 감사 기록과 보충 호출 예약을 사용한다. 재개 시 감사가 없는 구간은 재사용하지 않고, 보충 예약이 불확실하면 추가 보충을 억제한다. 기존 계약 0/1/2 프롬프트와 연구용 계약 2는 유지한다. 세부 저장 경계와 한도는 [손실 점검](extraction-loss.md)에 정의한다.

사전 문맥 v2는 사전 생성·승인 사전 추출·인용 보정·누락 보충에 같은 빌더로 적용된다. 반복 문자열이 많은 사전은 v1보다 커질 수 있으며 일반 JSON과 v2 모두 전체 입력 한도를 넘으면 호출 전에 거부한다. 오래된 v1으로 되돌려 전송하지 않는다. `admission_policy()`가 새 프로토콜과 실제 크기를 반영하므로 과거 경고는 무효화되고 문서 화면에서 다시 계산된다. 이 변경은 무손실 전송 표현만 바꾸므로 추출 계약 3과 검증된 semantic checkpoint 재사용 규칙을 유지하며, 저장된 프롬프트·응답·승인 사전·기존 checkpoint는 수정하지 않는다.
