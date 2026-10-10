# llmwiki-demos 학습과 적용

참고 경로: `../llmwiki-demos/part01`부터 `part08`, `../llmwiki-demos/llmwiki-wirelog`. README뿐 아니라 chunk pipeline, review workflow 설명, engine/graph/transport/agent 코드와 후반 교시 실행기를 함께 확인했다. 참조 프로젝트는 실행 의존성이 아니다.

| 자료 | 배운 원칙 | verinote 적용 |
| --- | --- | --- |
| part01 | 작은 청크와 정확한 원문 인용, 생성물은 derived | 원문 8,000자 청크, quote 존재 검사, 위키 검토 대기 |
| part02 | 타입·관계의 방향, 별칭과 이름만으로 동치 추정 금지 | 일반 triple, 명시적 방향과 predicate, 별칭 자동 병합 없음 |
| part03 | 후보와 승인 분리, 수정 시 새 revision, 과거 승인 무효화 | 웹 검토, 낙관적 동시성, append-only 판정 이력, 수정→pending |
| part04 | 실제 PyreWire 평가, 잘못된 방향은 엔진 타입 검사만으로 잡지 못함 | EasySession API, 고정 규칙, 사용자 원문 대조 |
| part05 | 스냅샷과 철회, 시나리오와 현실 구분 | 승인 사실 스냅샷 보존, 철회 이후 새 질의, 과거 재현; 증분 세션 대신 스냅샷 재평가 |
| part06 | 구조 결과와 문맥 근거, 현재 미확인 정보 구분 | 위키 문장과 관계 사실 별도 검토, 조건 있는 관계 추출 제한 |
| part07 | 예산, 실제 도구 기록, error와 empty 구분 | 청크·출력·시간·작업 큐 제한, 실제 transport 기록, empty 결과와 오류 구분 |
| part08 | 주장별 근거, 초안 승인, 변경 이력 | 인용 연결 위키, 페이지 승인, 원문 버전 diff, 사실 수정 이력 |
| llmwiki-wirelog | CLI 어댑터, 실제 엔진, source-first evidence, 질의 설명 | 5개 provider/모델 선택, 사실 ID 경로·해시·엔진 버전·규칙 공개 |

사용자 추가 요구에 따라 입력 문서까지 논리 ID/불변 버전으로 관리한다. 최신 원문 등록은 과거 원문을 덮어쓰지 않으며, 과거 승인도 새 문서 버전으로 자동 승계하지 않는다.

교육자료의 자연어 도구 선택 루프는 결정성 경계를 넓히지 않기 위해 초기 구현에서 제외했다. 관계 질의는 사용자가 고정 연산을 선택한다. 이후 자연어 planner를 추가하더라도 제안된 질의와 실제 실행 요청을 보존하고, 생성 문장 자체를 결정적 결과라고 부르지 않아야 한다.

후속 자연어 구현은 참조 `llmwiki/agent.py`의 고정 도구·실행 증거 ID·unknowns 계약을 적용했다. Verinote에서는 교육용 개체 하드코딩 대신 승인 개체/관계와 고정 원문 버전 목록을 모델에 제공하고, 모든 최종 답변에 앞서 실제 모델 선택 Wirelog 조회를 요구한다.
