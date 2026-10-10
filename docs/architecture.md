# Verinote 구조

## 데이터와 승인 경계

`documents`는 논리 문서 ID와 현재 버전을, `versions`는 불변 원본 바이트·텍스트·파일명·해시를 저장한다. 새 버전 등록은 낙관적 `expected_version` 확인과 한 트랜잭션으로 진행한다. 문서 제목은 논리 문서의 제목이며 파일명은 버전별로 보존한다. 원문 삭제·과거 버전 덮어쓰기는 제공하지 않는다.

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
| GET/POST `/api/documents` | 목록 / 신규 또는 다음 버전 등록 (`document_id`, `expected_version`) |
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

첫 원자적 구현 단위는 버전별 문서 등록 → 실제 CLI 추출 → 사람 검토 → Wirelog 결과와 원문 증명이다. 기존 교육자료의 서비스·문서명·승인 예시를 하드코딩하지 않는다. 자연어 planner, 복수 사용자 인증, 사용자 정의 온톨로지 편집기, OCR, 의미 검증 자동화는 이 초기 구현 범위에 포함하지 않는다.
