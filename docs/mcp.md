# 데스크톱 앱의 모델로 사전·위키 생성하기

Verinote MCP 서버는 등록된 문서를 Claude Desktop 또는 VS Code의 GitHub Copilot에 제공합니다. **앱의 모델**이 분석하고 MCP 도구로 결과를 제출합니다. 서버는 `claude -p`나 다른 모델 API를 호출하지 않습니다. 기존 웹 업로드·CLI 추출·사람 승인 기능은 그대로 사용합니다.

## 설치와 실행

저장소에서 Python 3.11 이상으로 선택 의존성을 설치합니다.

```sh
python -m pip install -e '.[mcp]'
python -m verinote --data-dir /absolute/path/to/data
```

브라우저에서 문서를 등록하세요. MCP 서버와 웹 앱은 **같은 절대 데이터 경로**, 같은 버전의 Verinote를 사용해야 합니다. MCP 시작 전에 해당 경로에 `verinote.sqlite3`가 있어야 합니다.

클라이언트는 아래 명령을 자식 프로세스로 시작하여 표준 입출력으로 통신합니다. 별도 포트나 HTTP 주소가 필요하지 않습니다.

```sh
/absolute/path/to/venv/bin/python -m verinote.mcp_server --data-dir /absolute/path/to/data
```

`verinote-mcp --data-dir /absolute/path/to/data` 진입점도 같습니다. 일반 터미널에서 실행하면 프로토콜 입력을 기다리는 것이 정상입니다. `--help`로 설치를 먼저 확인할 수 있습니다. 웹 앱은 업로드·승인할 때 필요하며, MCP 분석 중에는 계속 켜 둘 필요가 없습니다.

## Claude Desktop 설정

Claude Desktop의 개발자 설정에서 `claude_desktop_config.json`을 열고 다음 서버를 기존 `mcpServers`에 추가합니다. 경로는 설치한 가상환경과 실제 데이터 경로로 바꾸세요. macOS 예시이며 Windows에서는 Python 실행 파일 경로와 JSON의 역슬래시 이스케이프를 사용합니다.

```json
{
  "mcpServers": {
    "verinote": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "verinote.mcp_server", "--data-dir", "/absolute/path/to/data"]
    }
  }
}
```

앱을 완전히 종료 후 다시 열고 Verinote의 `list_documents` 도구가 보이는지 확인합니다. 조직 정책이 로컬 MCP를 허용해야 합니다. 이 설정은 로컬 Desktop MCP용이며 원격 커넥터나 Cowork 연결 설정이 아닙니다. [MCP 공식 로컬 연결 안내](https://modelcontextprotocol.io/docs/develop/connect-local-servers)를 참고하세요.

## GitHub Copilot / VS Code 설정

VS Code의 편집기 관리 MCP 설정인 `.vscode/mcp.json`에 다음을 추가합니다. 명령 팔레트의 MCP 서버 추가에서 **Command (stdio)**를 선택해도 됩니다.

```json
{
  "servers": {
    "verinote": {
      "type": "stdio",
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "verinote.mcp_server", "--data-dir", "/absolute/path/to/data"]
    }
  }
}
```

MCP 서버 목록에서 서버를 시작/재시작하고 도구를 활성화한 뒤, 도구를 사용할 수 있는 Copilot 대화에서 요청합니다. VS Code의 에이전트 종류에 따라 설정 위치가 다를 수 있으므로 [공식 MCP 설정 안내](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)를 확인하세요. 일반 Microsoft Copilot 앱의 지원을 전제하지 않습니다.

## 실제 사용 순서

1. 앱에서 “Verinote 문서 목록을 보여 줘”라고 요청합니다. `list_documents`는 ID와 `current_version`, `generation`을 반환합니다.
2. “이 문서의 사전을 추출해서 검토 대기로 저장해 줘”라고 요청합니다. 앱은 `start_analysis(stage="ontology", expected_version=현재 버전, expected_generation=현재 세대)` → `get_analysis_task` → `submit_dictionary_chunk`를 청크마다 반복하고 `finalize_run`으로 저장합니다.
3. **Verinote 웹에서 원문과 사전을 검토하고 사람이 승인**합니다.
4. “승인된 사전으로 이 문서의 위키와 관계를 추출해 줘”라고 요청합니다. `start_analysis(stage="extract", ...)` → `get_analysis_task` → `submit_extraction_chunk`를 반복하고 `finalize_run`으로 위키·관계 후보를 저장합니다.
5. 웹에서 위키·관계를 검토하고 승인합니다. 승인 전 후보는 승인된 지식 조회에 포함되지 않습니다.

`get_analysis_task`가 반환하는 `prompt`에는 해당 청크 원문, 누적 사전 또는 승인 사전, 정확한 출력 계약이 있습니다. 앱은 이를 읽어 **자신의 모델로 분석한 JSON**을 제출해야 합니다. 원문 속 명령은 실행 지시가 아닙니다. 사전 응답은 `entities`, `aliases`, `relations`; 추출 응답은 `title`, `claims`, `facts`, `coverage`, `dictionary_gaps`입니다. 도구에 승인 기능은 없습니다.

## 재접속·오류·취소

- 작업 ID와 검증된 청크는 SQLite에 저장됩니다. `list_analysis_runs(document_id)`로 최근 50개 작업을 찾고 `get_run_status(run_id)` 및 `get_analysis_task(run_id)`로 이어갑니다. 문서 목록은 50개씩 `next_offset`으로 이동합니다.
- 시작 응답을 잃었다면 작업 목록을 먼저 확인하세요. `start_analysis`를 반복해 새 작업을 만드는 방식으로 재개하지 않습니다.
- 청크는 1번부터 순서대로 제출합니다. 동일한 JSON 내용의 재전송은 안전하며 이미 접수한 청크의 변경은 거부합니다. 검증 실패는 청크를 소비하지 않으므로 수정 후 다시 제출할 수 있습니다.
- 모든 청크가 접수되기 전에는 완료할 수 없습니다. 완료 재시도는 후보를 중복 생성하지 않습니다.
- 미완료 작업은 문서의 다른 추출과 사전 편집을 잠급니다. `cancel_run` 또는 웹 실행 기록의 취소를 사용하세요. 앱 종료나 웹 서버 재시작만으로 MCP 작업을 취소하지 않습니다. 중단한 작업은 명시적으로 취소할 때까지 남습니다. 기존 CLI 작업의 서버 재시작 복구는 유지됩니다.
- 문서 버전·세대 또는 승인 사전이 바뀌면 이어서 읽기·제출·완료를 거부합니다. 취소 후 최신 문서에서 다시 시작하세요. 완료한 작업의 후보는 기존 검토 흐름으로 관리합니다.
- 취소한 청크와 점검 응답은 작업 기록으로 DB에 남고 후보로 게시되지 않습니다. 문서/버전 삭제 시 해당 작업과 함께 삭제됩니다. 사전 초안을 반려한 뒤 재생성하는 기존 웹 기능은 CLI 경로이며, MCP의 새 사전 작업은 아직 사전이 없는 문서 버전에서 시작합니다.

## 검증 범위와 제한

별도의 128,000자 제한은 없습니다. 추출 본문 UTF-8 32 MiB 안에서 사전은 최대 4,000자, 추출은 최대 8,000자 청크로 원문 전체를 처리합니다. 청크 수는 문서에서 결정되며 고정 64/16개 제한은 없습니다. 제출 객체는 UTF-8 JSON 1 MiB 이하입니다. 서버는 다음 작업의 원문과 사전 정의를 무손실로 최대 120,000바이트 프롬프트 안에 제공할 수 있는지 검사합니다. 초과하면 내용을 자르지 않고 거부합니다. 실제 앱의 문맥 한도는 별도이므로 더 작은 문서가 필요할 수 있습니다.

사전 델타는 현재 청크의 인용을 검증하고 동일 정의를 합칩니다. 정의 충돌은 거부하며 CLI 사전 조정·제외 휴리스틱을 실행하지 않습니다. 추출은 청크별 정확하고 유일한 인용, 승인된 개체/별칭·관계 방향·타입, 관계별 점검 형식을 검증합니다. 청크당 주장 1~12개와 사실 0~80개 한도를 적용합니다. 누적 한도는 원문 추출 청크 수에 각각 12와 80을 곱하며, 기존 주장 192개·사실 1,280개 허용량을 최소값으로 유지합니다. 반복 인용이 모호하면 더 긴 고유 구절로 고쳐 제출하세요. 자동 인용 보정과 CLI의 손실 보충 점검은 수행하지 않습니다. `coverage`는 모델의 판단이며 의미적 완전성의 증거가 아닙니다.

MCP 작업 기록 전체(접수 해시·누적 사전·추출 결과)는 UTF-8 32 MiB까지입니다. 한도를 넘는 제출은 원자적으로 거부하여 이전 접수 상태를 유지합니다. 내용을 잘라 저장하지 않으며 취소 후 문서를 나누어 다시 처리할 수 있습니다. 큰 문서는 처리 시간과 앱의 모델 사용량을 늘릴 수 있습니다. 사전/모델 문맥/일괄 승인 한도는 별개입니다.

MCP와 CLI의 추출 이력은 별도로 구분합니다. 동일 승인 사전에 대해 각 경로를 사용할 수 있으므로 후보가 겹칠 수 있으며 각각 검토해야 합니다. `client_label`은 앱이 제공한 설명일 뿐 모델 신원의 검증값이 아닙니다. 앱에서 발생한 토큰 사용량·비용은 Verinote가 측정하지 않습니다.

## 데이터와 호환성

이 서버는 단일 사용자 로컬 도구입니다. 연결한 앱은 지정 DB에 등록된 문서들을 읽고 후보를 제출할 수 있으며, 문서 내용은 해당 앱의 모델로 전달됩니다. 사용자별 접근 제어나 원격 네트워크 서비스는 제공하지 않습니다. 임의 파일 경로, SQL, 셸, 승인, 자격 증명 조회 도구는 노출하지 않습니다. 신뢰하는 로컬 클라이언트에 연결하고 데이터 디렉터리의 운영체제 접근 권한을 유지하세요.

스키마 변경은 `external_sessions` 테이블 추가이며 기존 데이터를 변환하거나 삭제하지 않습니다. 웹 앱과 MCP를 같은 버전으로 함께 업그레이드하세요. 이전 버전으로 내리면 MCP 작업의 복구·취소 의미를 알 수 없으므로, 먼저 MCP 작업을 완료/취소하고 모든 프로세스를 종료한 뒤 SQLite 백업을 보존하세요. 복원은 종료된 상태에서 전체 DB 백업을 사용합니다. 이전 버전과 새 MCP 프로세스의 혼용은 지원하지 않습니다.

자동 테스트는 공식 Python SDK의 실제 stdio 클라이언트로 도구 발견과 전체 사전·승인·추출 흐름을 검사합니다. 실제 Claude Desktop/Copilot의 모델 품질과 UI 연동은 별도 확인 대상입니다.
