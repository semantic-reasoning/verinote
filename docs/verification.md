# 검증 기록

2026-10-10, Python 3.14.7 / PyreWire 1.1.2 / pypdf 6.20.0 환경에서 확인했다.

- `python -m unittest discover -s tests -v`: 16개 통과. 실제 Wirelog 경로·순환·관계 분리, 승인과 철회, 과거 스냅샷, 원문 버전과 정확한 바이트, 동시 승인, 추출 중 새 버전, 실패 시 원자성, HTTP 흐름·출처 차단, PDF/DOCX와 압축량 제한을 포함한다.
- `node --check verinote/web/app.js`: 통과.
- `python -m compileall -q verinote`: 통과.
- `python -m pip install -e .`: 설치와 console entry point 생성 성공.
- 실제 Antigravity(`agy`, `gemini-3.8-flash-low`)로 합성 문서의 위키 초안 1개와 관계 후보 2개를 생성했다. 자동 승인하지 않았다.
- Claude, Copilot, Cursor, Codex는 실제 기본 모델로 최소 JSON 응답을 확인했다. 모든 공급자에 대해 장문 문서 전체 추출 품질까지 검증했다는 뜻은 아니다.
- 초기 Copilot `--deny-tool '*'`는 설치 버전에서 거부되어 `--available-tools ''`로 수정하고 실제 호출을 재검증했다. Cursor는 격리된 빈 작업 폴더 신뢰가 필요하여 `--trust`를 지정했다. `--mode ask --sandbox enabled`는 유지한다.
- 브라우저 자동화는 in-app browser 부재 및 Chrome의 `net::ERR_BLOCKED_BY_CLIENT`로 localhost를 열지 못했다. HTTP 통합 테스트와 JS 문법 검사를 수행했으며 시각적 렌더링·브라우저 상호작용 검증은 미완료다.

실제 CLI 프롬프트/응답 로그와 워크플로 선택 기록은 로컬 `.git/verinote-workflow/`에 보존한다. 테스트 데이터는 사용자의 `data/`에 주입하지 않았다.
