# verinote

원문을 등록하고, 에이전트가 추출한 위키와 관계를 사람이 검토한 뒤, **실제 Wirelog로 승인된 지식만 조회**하는 로컬 웹 애플리케이션입니다. 문서 버전·정확한 인용·승인 이력·질의 스냅샷을 보존합니다. 여기서 xAI는 설명 가능한 AI(eXplainable AI)를 의미합니다.

## 실행

Python 3.11 이상과 사용하는 에이전트 CLI의 설치·로그인이 필요합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m verinote
```

브라우저에서 <http://127.0.0.1:8765>를 엽니다. 데이터 위치와 포트를 바꾸려면 `python -m verinote --data-dir /path/to/storage --port 8766`을 사용합니다. `verinote` 명령도 동일합니다. 프로그램은 `127.0.0.1`에만 바인딩됩니다.

## 사용 흐름

1. **문서 등록**: 파일을 선택하거나 텍스트를 붙여 넣습니다. TXT/MD/CSV/JSON/RST/LOG(UTF-8), 텍스트 PDF, DOCX를 지원합니다. 최대 원본 4 MiB, 추출 텍스트 128,000자, PDF 200쪽입니다. OCR·암호화 PDF·이미지는 지원하지 않습니다. DOCX 본문 텍스트를 읽으며 페이지 배치·이미지·주석을 복원하지 않습니다.
2. **위키 초안 추출**: 에이전트와 모델을 선택합니다. 최대 8,000자 청크 16개로 처리하며 청크마다 실제 CLI를 호출합니다. 원문은 해당 모델 제공자에게 전달됩니다. 생성한 문장과 관계는 검토 대기이며 원문에 없는 인용은 거부합니다.
3. **검토**: 각 위키 초안과 관계 사실을 원문과 대조하여 승인·보류·반려합니다. 검토자와 이유를 기록합니다. 위키 승인과 관계 승인은 별개입니다. 관계를 수정하면 새 후보 revision이 생기고 다시 검토해야 합니다. 기존 승인 철회도 가능합니다.
4. **관계 탐색**: 직접 관계 또는 특정 관계의 도달 대상·역방향 출발점·경로를 조회합니다. 예를 들어 `주문 / depends_on / 원장`을 경로 조회하면 실제 PyreWire 결과와 사실 ID 경로, 원문 인용을 표시합니다. 문장 생성과 관계 조회는 구분됩니다.
5. **새 문서 버전**: 문서 상세의 `새 버전 등록`으로 같은 문서의 다음 버전을 추가합니다. 버전 선택·원본 다운로드·이전 버전 텍스트 diff를 제공합니다. 새 버전 등록 즉시 최신 버전이 현재 기준이 되며, 이전 버전의 승인 사실은 현재 그래프에서 제외됩니다. 새 버전은 다시 추출·검토합니다. 과거 원문과 판정은 삭제하지 않습니다.
6. **재현**: 질의 결과 JSON을 다운로드하거나 최근 질의에서 과거 스냅샷을 재조회합니다. 당시 승인 사실의 내용과 문서 버전을 보존하므로 이후 수정·철회와 분리하여 탐색할 수 있습니다.

## 에이전트와 모델

| UI 이름 | 실행 방식 | 모델 선택 |
| --- | --- | --- |
| Claude | `claude --output-format text --tools '' --strict-mcp-config -p PROMPT` | `--model ID` |
| Antigravity | `agy --output-format text --mode plan --sandbox --disable-slash-commands -p PROMPT` | `--model ID` |
| GitHub Copilot | `copilot --silent --available-tools '' --disable-builtin-mcps -p PROMPT` | `--model ID` |
| Cursor | `cursor-agent --output-format text --mode ask --sandbox enabled --trust -p PROMPT` | `--model ID` |
| Codex | `codex exec --skip-git-repo-check --ephemeral --sandbox read-only --color never -` | `--model ID`, prompt는 stdin |

Cursor는 `cursor-agent`가 없으면 `agent` 실행 파일을 탐색합니다. 설정 화면에서 기본 에이전트와 **각 에이전트별 모델 ID**를 저장하고, 추출 직전에 해당 작업의 모델을 바꿀 수도 있습니다. 모델 목록을 하드코딩하지 않으며 빈 값은 CLI 기본 모델을 뜻합니다. 모델 가용성·로그인은 CLI 계정에 따릅니다. 설치 여부만으로 모델 접근 가능성을 보장하지 않습니다.

CLI는 별도 작업 폴더에서 실행하며 shell 문자열을 사용하지 않습니다. 권한 우회 옵션은 사용하지 않습니다. 실행 제한은 호출당 180초, stdout+stderr 1 MiB이며 오류·시간 초과는 실패로 기록합니다. 작업 큐는 최대 8개, 실행은 한 번에 1개입니다. 서버 재시작 시 미완료 작업은 중단 상태로 바뀝니다. 실행 폴더 격리가 OS 수준의 완전한 격리를 뜻하지 않으며 각 CLI 자체 권한 정책도 적용됩니다.

CLI 옵션은 설치된 도움말과 [Claude 공식 CLI 문서](https://docs.anthropic.com/en/docs/claude-code/cli-usage), [Cursor CLI 문서](https://cursor.com/docs/cli/overview), [Copilot 도구 제한 문서](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools)를 기준으로 확인했습니다. CLI 버전이 달라 옵션 오류가 발생하면 실행 로그를 확인하세요.

## 결정성과 설명 가능성의 경계

- LLM 추출 자체는 비결정적입니다. **같은 승인 사실 스냅샷 + 같은 규칙 + 같은 PyreWire 버전 + 같은 질의**에서 정렬된 관계 결과와 선택된 증명 경로가 같습니다.
- 모든 관계는 일반 triple입니다. 전이는 사용자가 지정한 동일 predicate 내에서만 계산합니다. 관계 이름이 같다는 이유로 인과관계·장애 발생·시간적 유효성을 보장하지 않습니다. 필요하면 의미가 다른 관계는 후보 검토 시 이름을 분리하세요.
- 증명 경로는 정렬된 승인 사실에서 결정적으로 고른 최단 경로 하나입니다. 엔진 내부 provenance 전체를 출력하는 기능은 아닙니다. 직접 관계에는 이를 지지하는 모든 승인 사실 ID가 표시됩니다.
- 정확한 인용의 존재는 의미의 정확성을 증명하지 않습니다. 조건·부정·시점이 손실된 관계는 승인하지 않아야 합니다. 시스템은 사람의 판정을 대신하지 않습니다.
- 현재 릴리스의 질문 방식은 **구조화된 관계 질의와 원문 키워드 검색**입니다. 자연어 자율 도구 루프, 임베딩 검색, 사용자 정의 Datalog 실행은 제공하지 않습니다.

## 저장과 운영 범위

기본 `data/verinote.sqlite3`에 원본 바이트·추출 텍스트·SHA-256·문서 버전·초안·사실·검토 이벤트·설정·스냅샷·질의 기록이 저장됩니다. `data/runs/<run-id>/<chunk>/`에는 실제 prompt, stdout, stderr, 실행 메타데이터가 저장됩니다. 이 폴더는 Git에서 제외합니다. 백업할 때 서버를 종료한 뒤 데이터 폴더 전체를 복사하세요.

단일 사용자 로컬 애플리케이션입니다. 검토자 이름은 사용자가 입력하는 감사 기록이며 인증된 신원이 아닙니다. 인터넷 공개·다중 사용자 권한 관리·데이터 암호화를 제공하지 않습니다. 같은 데이터 디렉터리에 서버를 여러 개 실행하지 마세요. HTTP 서버는 로컬 Origin/Host 검사와 JSON 요청 제한을 사용합니다. UI는 원문·모델 출력을 HTML로 실행하지 않습니다.

## 검증

```sh
python -m unittest discover -s tests -v
node --check verinote/web/app.js
```

테스트는 실제 PyreWire 엔진으로 승인/미승인, 경로, 사이클, 관계 혼합 방지, 철회 및 스냅샷 재현을 확인합니다. 문서 버전·동시 승인·추출 중 새 버전·잘못된 인용·실패/타임아웃·HTTP 신뢰 경계도 검사합니다. 외부 CLI는 테스트 transport와 실제 연동 검증을 구분합니다.

설계와 참고 자료 학습 내용은 [architecture](docs/architecture.md), [learning notes](docs/learning-notes.md)에 있습니다.
