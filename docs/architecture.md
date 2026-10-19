# Verinote 구조

## 데이터와 승인 경계

`documents`는 논리 문서 ID와 현재 버전을, `versions`는 불변 원본 바이트·텍스트·파일명·해시를 저장한다. 새 버전 등록은 낙관적 `expected_version` 확인과 한 트랜잭션으로 진행한다. 문서 제목은 논리 문서의 제목이며 파일명은 버전별로 보존한다. 명시적인 최신 리비전/전체 문서 삭제를 제공한다. 과거 버전 덮어쓰기는 제공하지 않는다.

`runs`는 에이전트·모델·대상 문서 버전을 고정한다. 실행 중 원문 버전이 올라가도 결과는 원래 버전에만 저장한다. 문서별 동시 실행은 DB unique index로 차단한다. 사전 생성과 관계 추출을 구분한다. 성공한 동일 사전 revision·추출 방식 버전의 재추출은 거부한다. 새 승인 revision 또는 개선된 추출 방식의 실행은 기존 검토를 보존하고 새 pending 후보를 추가한다. 실패한 실행은 부분 후보를 남기지 않으며 재시도 가능하다.

`items`는 ontology(추출 사전), page, fact를 같은 검토 계약으로 저장한다. 상태는 pending/approved/held/rejected다. `revision`은 내용 수정 및 판정마다 증가하고 요청자가 본 revision을 제출해야 한다. `events`는 생성·승인·보류·반려·철회·수정의 당시 payload, 행위자, 이유, 시각을 보존한다. 사전 및 사실 수정은 반드시 pending으로 전환한다. 위키 승인만으로 관계가 승인되지는 않는다.

`snapshots`는 하나의 SQLite 트랜잭션에서 읽은 현재 문서 버전·메타데이터와 승인 사실·위키·사전 전체를 knowledge_version=1 JSON 및 해시와 함께 저장한다. SQL 스키마 변경이나 기존 스냅샷 backfill은 없으며, 옛 스냅샷은 당시 고정된 관계만 사용한다. 과거 후보 내용이 변경되어도 snapshot의 payload는 바뀌지 않는다. `queries`는 질의 결과와 원래 요청·규칙 본문·규칙 해시·엔진 버전을 기록한다. 재조회에는 현재 설치된 엔진과 규칙을 사용하므로 버전이 변경된 환경에서는 기록된 버전·해시를 비교해야 한다.

## 모듈

- `config.py`: 서버 전용 TOML 자격 증명의 경로 선택, POSIX 소유자·권한 검증, 빈 파일 생성, 안전한 오류 및 메모리 주입 계약.
- `documents.py`: 형식 검사, UTF-8/PDF/DOCX 텍스트 추출, 관계 추출 8,000자 청크와 사전용 경계 우선 분할.
- `agents.py`: provider별 고정 argv, 모델 값 검증, 별도 cwd, 시간/출력 제한, 종료 및 로그.
- `ontology.py`: 사전 스키마·누적 크기·별칭 충돌·관계 타입 검사, 두 단계 프롬프트, 추출 응답 사전 검사와 인용 전용 보정 계약.
- `service.py`/`dictionary_pipeline.py`: 사전 최대 64구간·관계 추출 최대 16청크 처리, 복구 가능한 사전 생성 및 승인 사전에 따른 추출, 원문 인용 검증과 추출 실행당 최대 2회 추가 보정 호출, 원자적 후보 저장, 비동기 작업.
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

`questions` 테이블은 제출 시 고정한 snapshot, agent/model, 질문, 상태, 누적 trace/evidence, 답변과 오류를 저장한다. 검색·읽기는 snapshot의 문서 버전만 사용하고 그래프는 snapshot의 승인 payload만 사용한다. 기존 runs와 같은 worker/8개 슬롯을 사용하며 재시작 시 미완료 질문도 interrupted로 복구한다.

POST `/api/questions`는 `{question,agent?,model?,snapshot?}`를 받아 202로 작업을 반환한다. GET `/api/questions`는 최근 30개, GET `/api/questions/{id}`는 저장 결과와 진행 상태를 반환한다. UI polling은 선택 질문 ID를 확인하여 이전 실행이 새 화면을 덮어쓰지 않는다. 고급 구조화 질의 API는 유지한다.

## 삭제와 동시성

`documents.next_version`은 기존 최대 버전+1로 migration하며 삭제 시 내려가지 않는다. 별도의 `generation`은 새 버전 등록/최신 삭제마다 증가한다. 두 변경 토큰을 필수로 확인해 버전 rollback의 ABA를 방지한다. version_count는 생존 원문 행을 센다.

POST `/api/documents/{id}/delete-latest`, `/delete`는 정확히 `{expected_version,expected_generation}`를 받는다. 최신-only는 마지막 하나를 삭제하지 않고 409를 반환한다. DB 트랜잭션은 전체 active runs/questions를 확인한 뒤 대상 버전과 파생 데이터, 해당 버전을 포함한 snapshots, 관련 queries/questions를 제거한다. 원문의 일부가 들어간 질문뿐 아니라 해당 스냅샷을 사용한 모든 이력이 제거된다.

`create_question`의 스냅샷 존재 검사와 INSERT는 하나의 BEGIN IMMEDIATE에 속한다. 질문 등록이 먼저면 삭제는 active 작업 때문에 거부되고, 삭제가 먼저면 질문 등록은 삭제된 snapshot 때문에 거부된다. 따라서 별도 service lifecycle lock 없이 DB가 수명 경쟁을 직렬화한다. `save_question`은 UPDATE만 허용하고 스냅샷 존재를 재확인한다. 동기 그래프 조회의 `save_query`도 스냅샷 존재를 같은 저장 트랜잭션에서 재확인하여 삭제 후 결과가 재생성되지 않게 한다.

삭제 트랜잭션은 `log_cleanup(kind,id)`에 로그 정리 의무를 함께 저장한다. Service는 삭제 후와 시작 시 처리하고 GET `/api/cleanup`, POST `/api/cleanup` (`{}`)으로 상태/재시도를 제공한다. 서버가 생성한 32자리 hex ID와 고정 runs/questions 루트만 사용하며 루트/대상 symlink는 따라가지 않고 pending으로 남긴다. 없는 폴더는 정리 성공이다. 파일 오류는 의무를 남기고 DB 삭제 성공과 구분한다. 복구 가능한 휴지통이나 포렌식 소거는 제공하지 않는다.

## 문서 설명과 용어 정의

`Assistant.run`은 호출 1~5에서 탐색 또는 조기 답변을 허용하고, 호출 6에서는 `answer_only=true`를 강제한다. 도구 슬롯 10개를 소진해도 다음 호출이 답변 전용이다. `exploration_rounds_remaining`은 현재 회차를 포함한 남은 탐색 호출 수이며 답변 전용에서는 0이다. `tool_slots_remaining`은 아직 사용하지 않은 요청 수다. 직전 탐색의 모든 trace/evidence를 다음 프롬프트에 전달한 뒤, 답변 전용에서는 정확히 answer 객체인 응답만 기존 인용 검증을 거쳐 반환한다. 추가 도구·혼합 객체는 실행 전에 거부한다. 실제 그래프 조회·유효 근거 없이 답변을 만들어 성공시키지 않는다. 모델 호출 최대 6회, 도구 최대 10회·회당 3개, 프롬프트/결과 크기 제한과 llm-1..6 사용량 기록은 유지한다.

`search_knowledge`는 요청 문서 범위 전체에서 먼저 기존 문자열 포함 검색을 수행한다. 승인 지식 일치가 0개일 때만 문장 안의 문서 제목·파일명 stem·승인 개체 ID/명칭·별칭을 보조 검색 이름으로 선택한다. NFC/casefold를 적용하고 문서 제목 탐색에만 공백을 생략해 비교한다. 한 글자와 명시된 일반 이름은 보조 검색에서 제외하며, 영문·숫자 이름은 다른 영문 단어 안의 부분 문자열을 이름으로 인정하지 않는다. 별칭은 같은 사전의 정확한 대상 ID/명칭으로만 확장하며 이름 하나당 변형은 최대 3개다. 이름은 긴 순서와 문서/종류/문자열 순서로 정렬해 최대 12개를 선택한다. 문서별 이름은 서로 합치지 않으며 명시한 document_id 밖의 이름은 선택하지 않는다. 제목 일치는 해당 문서의 위키를 탐색하도록 안내할 뿐 다른 문서를 요청 범위에서 제거하지 않는다.

위키 제목·주장 heading/text와 quote를 제외한 사전 안내만 검색한다. 전체 일치 위키를 사전 안내보다 앞에 안정적으로 정렬한 뒤 20개씩 페이지를 자른다. `query`, `match_mode`(literal/catalog), `matched_terms`, `anchors_truncated`로 검색 방법을 남기며 결과는 계속 non-citable이다. 전체 이름 후보가 한도를 넘으면 반환 일치가 0개여도 모든 페이지에서 result와 내부 lookup의 truncated를 true로 유지한다. 따라서 누락된 후보를 근거로 no_match 원문 fallback을 허용하지 않는다. 결과 페이지가 비었다는 이유로 검색 방식을 바꾸지 않으며, 페이지별 부분 조회를 합쳐 완전한 조회로 간주하지 않는다. 원문과 인용문은 후보 검색의 입력이 아니다.

`search_documents`는 기본 metadata 모드에서 고정 스냅샷의 제목·파일명만 NFC/casefold로 비교한다. body 모드는 한 문서의 ID와 검증된 fallback을 요구한 뒤 본문을 검색한다. 결과에 filename, matched_fields, total_chars, offset_strategy를 제공한다. offset은 원문 읽기 시작점이다. 허용된 본문 literal 일치는 원본 문자열 좌표를 반환하고, 제목/파일명이나 정규화 전용 본문 일치는 0(`start`)에서 읽게 한다. 정규화 문자열의 달라진 길이를 원문 좌표로 사용하지 않는다. 결과 상한 20개와 non-citable 계약은 유지한다.

`read_document`는 fallback 검증 후 원문을 변환하지 않고 title/filename, offset/end_offset/total_chars/next_offset을 제공한다. 문서 카탈로그는 본문 없이 filename과 길이를 제공한다. 문서 설명 질문은 승인 위키를 먼저 검색·읽고 부족한 경우에만 원문으로 보충한다. 빈 승인 그래프만으로 위키·원문 설명 불가를 단정하지 않는다. 일반 정의·외부 지식은 별도 근거 없이 보충하지 않으며 부분 읽기의 미확인을 문서 전체 부재로 단정하지 않는다. 실제 Wirelog 실행과 wiki/source/graph 인용 검증을 요구하며, 질문이 자료 밖이면 unknown-only 응답도 허용한다.

`query_graph`는 같은 Assistant와 고정 스냅샷 안에서 정규화된 인자가 같은 성공 결과를 재사용한다. 인자 검증 후 실제 엔진 실행·결과 크기 검사·근거 등록까지 성공한 결과만 저장한다. 빈 조회도 재사용되지만 긍정 주장에 필요한 근거를 만들지는 않는다. 재사용 trace는 원래 `evidence_id`와 1부터 시작하는 최초 성공 trace 번호 `reused_from`을 가지며 새로운 근거를 만들지 않는다. 매 요청의 trace와 progress, 도구 10회·모델 6회·누적 프롬프트 한도를 유지한다. 결과를 다음 모델 회차에 전달하기 전에는 재사용해도 원문 fallback이 허용되지 않는다. 그래프 이외의 중복 요청은 기존처럼 거부한다.


## 승인 사전 기반 추출

문서 버전별 ontology item 하나를 먼저 생성하고 사람이 검토한다. 개체는 정규 ID·원문 명칭·타입·인용, 별칭은 명칭·대상 ID·동일성을 검토할 인용, 관계는 snake_case ID·의미/포함/제외 설명·주어/목적어 타입·인용을 가진다. 사전은 개체 200/별칭 400/관계 40개, 검증된 전체 사전과 반복 인용 24 MiB, 인용을 제외한 정의 문맥 60,000바이트로 각각 제한한다. 인용을 의미 문맥 한도에서 분리해, 여러 항목이 같은 긴 원문을 인용해도 전체 사전 저장 한도를 넘지 않는 한 검증된다. 별칭과 ID의 충돌, 타입 참조 오류, 원문에 없는 인용은 저장을 거부한다. 개체 name은 자동 별칭으로 취급하지 않는다. 같은 이름의 서로 다른 개체를 구분하려면 범위를 명시한 ID를 쓴다.

`create_run`은 `BEGIN IMMEDIATE` 안에서 문서 version/generation을 확인하고 같은 버전의 approved 사전을 읽어 ID·revision·payload·SHA-256을 `runs.ontology`에 고정한다. active run 유니크 제약은 두 단계에 공통이다. 기존 성공 추출도 같은 트랜잭션에서 검사한다. 사전 생성은 기존 위키/사실에 관계없이 허용하되 사전 item이 이미 있으면 편집을 안내한다. 승인 사전 revision·추출 방식 버전마다 한 번의 성공 추출을 허용한다. 재승인으로 revision이 바뀌거나 이전 방식에서 현재 방식으로 바뀌면 새 추출이 가능하다.

사전 생성은 최대 4,000자 구간(최대 64개)과 인용문을 제외한 기존 사전 문맥으로 추가 항목을 요청한다. 구간 분할은 후반부의 문장/줄 경계를 우선하며 모든 문자를 정확히 보존한다. 각 구간을 다시 32자 이후 첫 문장/줄 경계를 우선하는 최대 400자 source span으로 나누고 내용 해시를 포함한 ID를 부여한다. 모델의 `quote_ref:[시작ID,끝ID]`를 현재 구간의 ID 목록으로 검증하고 연속 원문으로 복원한다. 숫자 문자 오프셋 추측·이전 구간 ID·literal quote 대체 응답은 허용하지 않는다. 저장/검토 사전은 기존 literal quote 형식이다. CRLF 인용도 원문 그대로 검증한다. 전체 사전의 이름·별칭·타입·정의 충돌 검증은 유지한다. 경계를 가로지르는 의미의 손실 가능성은 남는다.

사전 호출만 `CLI.complete(..., phase='ontology')`로 600초 기본 제한을 적용한다. 명시적 CLI timeout은 우선하며 다른 단계 기본값은 바꾸지 않는다. 자동 재시도는 없다. 성공적으로 병합·검증한 구간만 `runs/{id}/dictionary-checkpoint.json`에 atomic replace로 저장한다. `dictionary-progress.json`에는 완료/재사용 글자 수, 이전 실행 ID, 실제 시작 호출 수를 별도로 기록한다. 체크포인트 쓰기 실패는 경고를 유지하고 유효한 메모리 결과의 최종 저장을 막지 않는다.

재시도는 DB에서 같은 문서 버전·에이전트·요청 모델의 error/interrupted 실행 최대 8개를 조회한다. symlink 경로/파일 및 26 MiB 초과 파일을 거부한다. 스키마·소유 실행·문서/원문 해시·프롬프트/처리 버전·분할 크기와 결정적 완료 경계를 확인하고, 완료 prefix 전체로 사전을 다시 검증한다. 호환되지 않는 기록은 제외하고 그 이유를 상세에 남긴다. 상속한 진행도 새 실행에 저장하여 반복 재시도를 지원한다. 삭제된 DB 실행은 파일 정리가 지연되어도 재사용할 수 없다. SQL 마이그레이션은 없으며 과거 실행에 복구 파일이 없으면 새로 처리한다. 요청 모델이 빈 문자열인 경우 제공자의 실제 기본 모델 변경은 검출하지 못한다.

완료된 prefix의 재사용은 추가 모델 호출이 아니며 기존 호출 로그를 복사하지 않는다. 모든 실제 호출은 현재 실행의 구간 번호 폴더에 기록한다. 전체 완료 prefix만 재사용한 성공 실행은 명시적인 완료 marker로 새 호출 0회를 확인한다. marker가 없거나 손상되면 미확인으로 남긴다. 최종 `finish_run`은 전체 원문 검증·실행 상태·사전 유일성을 재검사한 후 하나의 pending 사전을 저장한다. 관계 추출은 모든 청크에 동일한 승인 사전을 보내며 청크당 80개 사실까지 검증한다. 프롬프트 UTF-8 상한은 120,000바이트다.

관계 추출은 `extraction_quote_issues`에서 전체 응답의 형식·필드 길이·사전 어휘·타입을 먼저 검사한다. 문자열이지만 현재 청크에 없는 인용만 보정 대상으로 모은다. 같은 에이전트·모델에 원문 청크, 고정 사전, 문제 항목을 보내 `path`와 `quote`만 받는다. 경로는 `facts.44.quote`처럼 0 기반이며 UI 오류는 `관계 45 인용`처럼 1 기반이다. 반환 경로 집합이 요청 집합과 정확히 같아야 하고 중복·누락·추가 필드는 실패한다. 복사한 응답의 해당 인용만 바꾼 뒤 기존 원문·사전 검증을 모두 다시 수행한다. 유사 문자열 매칭, 단어 자동 삭제, 항목 누락 또는 전체 응답 재생성은 하지 않는다.

추가 보정 호출은 청크당 1회, 실행당 2회로 제한하며 예산은 호출 전에 확인한다. 실패·시간 초과·프롬프트 한도 초과도 추가 재시도 없이 종료한다. 원래 호출과 보정 호출의 로그 폴더는 분리하고 성공한 변경 전후 인용은 `quote-repair/changes.json`에 기록한다. 성공 상세에는 보정 개수·위치·추가 호출 수만 넣고 원문은 넣지 않는다. `finish_run(detail=...)`은 기존 호출과 호환되는 선택 인자이며 성공 상세도 후보와 같은 트랜잭션에 저장한다. 사전 생성·legacy 수동 수정·자연어 질문의 검증 계약에는 보정을 추가하지 않는다. 정확한 인용도 의미적 근거를 보장하지 않으므로 후보는 pending이다.

`finish_run`도 사전 및 사실을 검증한다. 추출 단계에서는 현재 사전이 고정된 revision/hash로 여전히 approved인지 확인한 뒤 전체 후보를 한 트랜잭션으로 저장한다. 실행 중 문서의 새 버전이 생기는 것은 기존처럼 허용하되 후보는 시작 버전에만 저장한다. 사전 수정/철회는 진행 중 추출의 결과 저장을 막는다. 이미 완료된 후보 및 승인 사실의 상태는 변경하지 않는다. 수동 fact revision은 해당 실행의 고정 사전으로 검증하며 legacy 사실은 일반 triple 검증을 유지한다.

문서 GET 응답은 각 후보에 `dictionary`(ID/revision/hash)와 문서에 `can_extract`를 제공한다. UI는 과거 버전의 실행 버튼을 숨기며 POST는 표시된 version/generation을 서버에서도 확인한다. 사전은 JSON 편집, 원문 인용 확인, 기존 검토 API로 승인한다. 사전/위키/사실 승인은 서로 독립이다. Wirelog는 여전히 approved fact만 읽으며 canonical ID 문자열로 문서를 연결한다. 사전은 버전별이므로 문서 간 ID 충돌·불일치는 검토자가 조정해야 한다.

마이그레이션은 기존 runs에 `stage TEXT NOT NULL DEFAULT 'legacy'`, `ontology TEXT`를 추가한다. 재실행 시 열 존재를 검사하며 기존 items/events/snapshots를 재작성하지 않는다. 새 INSERT는 열을 명시한다. 이전 바이너리는 8열 위치 기반 INSERT를 사용하므로 확장된 DB로 롤백할 수 없다. 서버 종료 후 데이터 폴더 백업 및 이전 DB 복원으로 롤백한다.

## CLI/API 사용량 산출

`usage.py`는 CLI 전송 형식과 검증된 HTTP envelope를 각각 읽으며 모델이 생성한 답변의 `usage` 키를 계측 근거로 사용하지 않는다. `CLI.complete`는 기존처럼 애플리케이션 JSON을 반환한다. Claude/agy는 JSON result의 본문을, Cursor는 stream-json의 최종 result를, Codex는 JSON events의 agent_message와 최종 turn.completed 카운터를 분리한다. Copilot은 호출마다 지정한 `--usage-output-file`을 읽는다. 중간·중복 이벤트를 합산하지 않는다. 실제 설치 CLI의 무해한 단문 응답을 정제한 5개 fixture가 `tests/fixtures/usage`에 있다.

`agents.parse_response`는 최종 본문의 ANSI·전체 JSON 코드 블록 처리를 유지하며 객체만 반환한다. Copilot에 한해 JSON 객체 앞의 공백뿐인 `<final_answer>...</final_answer>` 접두 태그 하나를 제거한다. Cursor에 한해 전체 본문이 정확히 같은 JSON 객체 텍스트 두 개일 때 하나로 읽는다. 객체 밖 공백만 무시하며 서로 다른 필드 순서·숫자 표기·값, 세 번째 객체, 덜 끝난 객체, 부가 설명은 수용하지 않는다. 임의 텍스트에서 JSON을 찾아내거나 여러 응답을 합치지 않는다. 프로세스 종료·제공자 실패 검사가 우선하고 원본 stdout과 사용량은 그대로 보존하며 성공한 정리의 고정 라벨만 `run.json.response_normalizations`에 남긴다. 이 공통 파서는 사전·추출·질문에 적용되며 별도 모델 재호출은 없다.

정규화된 입력은 캐시를 포함하는 총량, 출력은 추론을 포함하는 총량이다. Claude·Cursor·Copilot은 일반 입력+캐시 읽기+캐시 쓰기로 입력을 복원한다. agy는 input_tokens+cache_read_tokens, Codex는 이미 캐시를 포함한 input_tokens를 사용한다. agy/Codex는 별도 청구되는 캐시 쓰기 구간이 없어 계산상 0이다. cache_write는 물리적인 캐시 생성 통계가 아니라 별도 가격을 적용할 구간이다. 추론을 출력에 다시 더하지 않는다. 근거는 [agy headless](https://www.antigravity.google/docs/cli/headless), [Claude 비용](https://code.claude.com/docs/en/costs), [Cursor 출력](https://cursor.com/docs/cli/reference/output-format), [Codex 비대화형](https://learn.chatgpt.com/docs/non-interactive-mode) 문서와 실제 CLI 응답이다. Cursor usage 세부 키는 관측된 형식이므로 형식 변경은 미확인으로 처리한다.

`usage.json`은 시작 전/프로세스 실행 후/종료 시 임시 파일 교체로 기록한다. 프로세스 정리 후 남은 제한된 출력을 읽어 JSON 파싱 실패·비정상 종료·타임아웃·크기 초과에서도 가능한 카운터를 남긴다. 전송 성공 후 온톨로지 검증이 실패한 경우 호출 상태는 success, 부모 작업은 error일 수 있다. 단가 snapshot을 호출 시작에 복사하고 결과의 resolved_model, 없으면 requested_model을 대조한다. 모델이 없는 기본값 단가는 명시적 가정이며 모든 추정에 rate_basis를 남긴다. 계산은 `(일반입력×입력단가 + 캐시읽기×읽기단가 + 캐시쓰기×쓰기단가 + 출력×출력단가) / 1e6`; 크레딧 추정은 그 비용을 지정 환산값으로 나눈다. 토큰 구간이 미확인이면 비용을 만들지 않는다. 모델별 복수 단가 자동 선택과 구독 청구 재현은 제공하지 않는다.

별도 `settings.usage_rates` 키를 사용하므로 기존 `/api/settings` roundtrip과 충돌하지 않는다. 에이전트 최대 6개(CLI 5종과 OpenRouter), 각 단가 객체의 필드·숫자·단위·모델 길이를 검증한다. DB 스키마 변경·과거 로그 backfill은 없다. 이전 버전으로 코드만 되돌려도 새 키와 sidecar는 무시되며, 원래 데이터 백업을 복원하면 설정과 로그까지 되돌릴 수 있다. 실행 중 작업은 종료 후 배포하고 데이터 폴더를 백업한다.

조회는 DB가 소유한 ID만 대상으로 runs의 사전 1~64번 구간, 관계 추출 1~16번 청크와 각 quote-repair, questions의 llm-1~6을 확인한다. 경로의 symlink를 거부하고 파일당 256 KiB로 제한한다. 프롬프트/원본 응답을 API 계측값으로 노출하지 않는다. run/question GET 응답에 읽기 전용 usage를 덧붙이며 질문 저장 payload를 변경하지 않는다. 집계는 기존 목록의 100/30개 제한과 독립적으로 최근 1,000개 작업을 선택한다. 작업 수·truncated, 지표별 known_calls, 불완전 기록 수와 호출 횟수조차 모르는 작업 수를 함께 반환한다. DB와 파일은 하나의 트랜잭션이 아니며 읽는 시점의 보존 기록만 나타낸다. 부분적으로 삭제된 로그의 정확한 호출 수는 복원할 수 없다. 삭제된 DB 소유자는 물리 로그 정리 실패와 무관하게 이후 집계에서 제외된다.


## 사전 중복과 정의 충돌

`merge_dictionary`는 기존 사전을 재검증하고 모든 새 항목을 정규화·검증한 뒤 합친다. 개체의 ID/name/type, 별칭의 alias/entity, 관계의 ID/description/subject_types/object_types가 같으면 인용 차이는 정의 충돌이 아니다. 문자열은 기존의 양끝 공백 정리만 적용하고 관계 타입 배열은 기존 규칙대로 정렬된 집합으로 비교한다. 유사 표현·대소문자·관계 설명을 추측해 같은 뜻으로 합치지 않는다. 첫 유효 인용을 유지하므로 추가 인용을 모두 저장하는 기능은 아니다.

중복으로 버리는 항목도 근거·이름·별칭·타입 검사를 거친다. 정확한 자기 ID 매핑만 대상 개체의 존재와 ID를 포함한 원문 근거를 확인한 뒤 제외하며, 이 경우 정규 표시 명칭은 개체 자체의 근거로 검증한다. 실제 별칭은 별칭과 정규 명칭이 같은 인용에 있어야 한다. 입력 개수 한도와 최종 전체 개수/크기/충돌 검사는 유지하며 직접 JSON 검토 입력의 중복 개체·실제 별칭·관계 선언은 계속 거부한다.

원래 전달받은 기존 사전 항목을 정규화 후 인용까지 정확히 반복하는 경우에만 이전 원문 범위의 근거를 재사용할 수 있다. 새 인용과 같은 응답 안에서 처음 나온 항목은 현재 구간의 근거가 필수다. 실제 충돌 메시지는 이번 응답 내부와 기존 사전/현재 구간 사이를 구분하고 종류·위치·변경 필드를 표시한다. 선언되지 않은 관계 타입도 관계 ID·타입 필드를 명시한다.

사전 생성 복구 계약에는 `merge_contract` 버전이 포함된다. 이전 처리 규칙의 부분 기록은 호환 검사에서 제외되어 새로 시작할 수 있다. 완료된 사전·승인 기록을 다시 쓰지 않으며 `source-spans-v1` 진행 표식과 기존 0회 호출 사용량 해석은 유지한다.

## 승인 사전 기반 추출 점검

`EXTRACTION_CONTRACT_VERSION`을 `runs.ontology` wrapper에 고정한다. 사전 payload/hash는 변경하지 않는다. 성공 중복 키는 `extraction_key(pin)`의 `(id, revision, extraction_contract)`이며 문서 조회와 실행 생성에 동일한 규칙을 사용한다. 버전 필드 없는 pin은 legacy 0으로 취급하여 현재 방식의 재추출 한 번을 허용한다. 기존 후보·이력은 덮어쓰지 않고 새 pending 후보를 추가한다. 승인 철회·사전 변경·동시 실행·문서 버전 검증은 유지한다.

새 추출 응답은 관계마다 `coverage`의 predicate/fact_indices/claim_indices/status/reason과 최대 12개 `dictionary_gaps`를 포함한다. reason 120자, gap detail 160자, 청크당 facts 80개·claims 12개 제한이다. `extracted`, `no_evidence`, `not_representable`, `limit_reached`를 구분하고 모든 사실의 인덱스와 관계를 검사한다. 보고서 형식·인덱스는 인용 보정 호출 전에 검사하고 인용 보정 후 다시 검사한다. 보고서 보정이나 보충 추출 호출은 없다. 정확한 인용과 승인 사전의 어휘·방향·타입 검증 및 기존 인용 보정 최대 2회는 유지한다. 이 검사는 모델 판단의 의미적 타당성이나 recall을 증명하지 않는다.

`extraction_coverage.py`는 검증된 청크의 인덱스/판단을 `runs/<id>/extraction-coverage.json`에 최대 2 MiB로 저장한다. 원문과 전체 사실을 복제하지 않는다. schema/실행/문서 버전/사전 pin과 source 길이, 청크 offset/length, 개수 및 인덱스를 검사한다. symlink/초과 크기/손상/identity 불일치는 unavailable이다. 청크 응답 관계 수와 DB의 실제 저장 후보 수(관계와 인용이 모두 같을 때 중복 제거)를 구분한다. 위키 주장 인덱스는 청크별 claim_base로 전역 번호에 대응한다. 목록 API는 요약만, 실행 상세 API는 구간별 점검을 반환한다.

보고서는 DB 완료 전에 best effort로 저장하며 DB 실행 상태만 게시 여부를 결정한다. 보고서 저장 실패는 후보 저장을 취소하지 않는다. 실패·중단 실행의 보고서는 검증된 접두 구간일 뿐 저장 후보가 아니다. 소유 run을 DB에서 재확인하므로 삭제 이후 파일 정리가 지연되어도 보고서를 노출하지 않는다. 기존 실행의 보고서가 없으면 unavailable이며 0개나 완전 추출을 뜻하지 않는다. 추가 SQL migration이나 승인 자동화는 없다.

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

Each HTTP job stores `execution_options` containing only
`adapter_contract_version`, `max_tokens`, and `timeouts.default`/`timeouts.ontology`.
A nullable `runs.execution_options` column is added by the existing idempotent
column migration; legacy CLI rows retain null. Questions use their existing
JSON payload. The strict option allowlist rejects malformed/missing OpenRouter
snapshots before any resumed transmission. No credentials or key hashes are
stored. Backup/restore of the SQLite file remains the rollback path; migration
is additive and preserves old rows. Startup recovery marks queued/running jobs
interrupted and preserves their snapshots; it never automatically retransmits.

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
repair succeeds; repairs are validated and counted separately. Optional telemetry
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
explicit model ID; server output-limit changes require an administrator. For
programmatic server construction the existing `CLI(options=ExecutionOptions(
max_tokens=..., timeout=...))` sets a fixed token cap and explicit timeout for
all phases. This is not a settings-screen control; defaults without that override
retain ontology600/other180 seconds. Register a new job after any adjustment;
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
