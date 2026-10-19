"""Single-user loopback HTTP server with same-origin mutation protection."""
import argparse
import base64
import binascii
import difflib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from .agents import CLI_PROVIDERS, PROVIDERS, TRANSPORTS, executable, validate_model
from .model_catalog import ModelCatalog
from .config import ConfigError, Credentials, create_config, load_config
from .documents import MAX_BYTES, MAX_TEXT, decode_document
from .ontology import MAX_DICTIONARY_BYTES
from .service import Service, text
from .store import Conflict, Store

MAX_JSON_BYTES = 6 * 1024 * 1024
MAX_DICTIONARY_JSON_BYTES = MAX_DICTIONARY_BYTES + 2 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_BYTES + 2) // 3) * 4
MAX_UPLOAD_JSON_BYTES = MAX_BASE64_CHARS + 64 * 1024
DOCUMENT_LIMITS = {'original_bytes': MAX_BYTES, 'text_chars': MAX_TEXT, 'pdf_pages': 200, 'docx_expanded_bytes': 16 * 1024 * 1024}

WEB = Path(__file__).with_name('web')


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, service, *, credentials=None):
        self.store, self.service = store, service
        self.model_catalog = ModelCatalog()
        self.credentials = credentials if credentials is not None else Credentials()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server
    def log_message(self, fmt, *args):
        pass

    def send(self, value, status=200, mime='application/json; charset=utf-8', headers=None):
        body = json.dumps(value, ensure_ascii=False).encode() if mime.startswith('application/json') else value
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def guard(self):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            raise PermissionError('로컬 Host만 허용합니다.')
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{h}' for h in hosts}:
            raise PermissionError('다른 출처의 요청은 허용하지 않습니다.')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise PermissionError('교차 사이트 요청은 허용하지 않습니다.')

    def body(self, max_bytes=MAX_JSON_BYTES):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('application/json만 지원합니다.')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= max_bytes:
            raise ValueError('요청 크기 제한 초과')
        self.connection.settimeout(15)
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('JSON 객체가 필요합니다.')
        return value

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def dispatch(self, method):
        try:
            self.guard()
            route = urlsplit(self.path)
            parts = route.path.strip('/').split('/')
            params = parse_qs(route.query)
            store = self.server.store
            if method == 'GET' and route.path in {'/', '/app.js', '/style.css'}:
                name = 'index.html' if route.path == '/' else route.path[1:]
                mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                return self.send((WEB / name).read_bytes(), mime=mime)
            if len(parts) == 4 and parts[:2] == ['api', 'providers'] and parts[3] == 'models' and method == 'POST':
                if parts[2] not in CLI_PROVIDERS:
                    raise ValueError('CLI 제공자의 모델 목록만 조회할 수 있습니다.')
                if self.body():
                    raise ValueError('모델 목록 조회에는 빈 JSON 객체를 보내세요.')
                return self.send(self.server.model_catalog.discover(parts[2]))
            if route.path == '/api/settings':
                if method == 'GET':
                    providers = []
                    for key, name in PROVIDERS.items():
                        value = {'id': key, 'name': name, 'transport': TRANSPORTS[key]}
                        if TRANSPORTS[key] == 'cli':
                            value['installed'] = bool(executable(key))
                        else:
                            value['configured'] = self.server.credentials.public_status()['openrouter']['configured']
                        providers.append(value)
                    return self.send(store.settings() | {'document_limits': DOCUMENT_LIMITS, 'providers': providers})
                if method == 'PUT':
                    data = self.body()
                    if not isinstance(data, dict) or set(data) != {'selected', 'models'} or not isinstance(data.get('selected'), str) or data.get('selected') not in PROVIDERS or not isinstance(data.get('models'), dict) or set(data['models']) - set(PROVIDERS):
                        raise ValueError('에이전트 설정 형식 오류')
                    models = {key: validate_model(value) for key, value in data['models'].items()}
                    if data['selected'] == 'openrouter':
                        models['openrouter'] = validate_model(data['models'].get('openrouter', ''), 'openrouter')
                    return self.send(store.save_settings({'selected': data['selected'], 'models': models}))
            if route.path == '/api/usage-rates':
                if method == 'GET':
                    return self.send(store.usage_rates())
                if method == 'PUT':
                    return self.send(store.save_usage_rates(self.body()))
            if route.path == '/api/usage' and method == 'GET':
                return self.send(self.server.service.usage_report())
            if route.path == '/api/documents':
                if method == 'GET':
                    return self.send(store.documents())
                if method == 'POST':
                    data = self.body(MAX_UPLOAD_JSON_BYTES)
                    title = text(data.get('title'), '제목')
                    filename = text(data.get('filename'), '파일명')
                    if 'content_base64' in data:
                        encoded = data['content_base64']
                        if not isinstance(encoded, str) or len(encoded) > MAX_BASE64_CHARS:
                            raise ValueError('파일 base64는 32 MiB 이내 원본을 인코딩한 문자열이어야 합니다.')
                        raw = base64.b64decode(encoded, validate=True)
                    else:
                        text(data.get('text'), '본문', MAX_TEXT)
                        raw = data['text'].encode()
                    if len(raw) > MAX_BYTES:
                        raise ValueError('파일 크기 제한 초과')
                    content = decode_document(filename, raw)
                    return self.send(store.register(title, filename, raw, content, data.get('document_id'), data.get('expected_version'), data.get('expected_generation')), 201)
            if len(parts) >= 3 and parts[:2] == ['api', 'documents']:
                doc_id = parts[2]
                version = int(params['version'][0]) if 'version' in params else None
                if len(parts) == 3 and method == 'GET':
                    return self.send(store.document(doc_id, version))
                if len(parts) == 4 and parts[3] == 'raw' and method == 'GET':
                    raw, _ = store.raw(doc_id, version or store.document(doc_id)['version'])
                    return self.send(raw, mime='application/octet-stream', headers={'Content-Disposition': 'attachment; filename="original"'})
                if len(parts) == 4 and parts[3] == 'diff' and method == 'GET':
                    doc = store.document(doc_id, version)
                    prior = max((v['version'] for v in doc['versions'] if v['version'] < doc['version']), default=0)
                    previous = store.document(doc_id, prior)['text'] if prior else ''
                    return self.send({'diff': ''.join(difflib.unified_diff(previous.splitlines(True), doc['text'].splitlines(True), fromfile=f'v{prior}', tofile=f'v{doc["version"]}'))})
                if len(parts) == 4 and parts[3] in ('delete-latest', 'delete') and method == 'POST':
                    return self.send(self.server.service.delete_document(doc_id, parts[3] == 'delete-latest', self.body()))
                if len(parts) == 4 and parts[3] == 'approve-items' and method == 'POST':
                    data = self.body()
                    if set(data) != {'version', 'expected_version', 'expected_generation', 'items', 'actor', 'reason'}:
                        raise ValueError('일괄 승인에는 version, expected_version, expected_generation, items, actor, reason이 필요합니다.')
                    return self.send(store.bulk_approve(doc_id, **data))
                if len(parts) == 4 and parts[3] in ('extract', 'ontology') and method == 'POST':
                    data = self.body()
                    if set(data) - {'agent', 'model', 'expected_version', 'expected_generation'}:
                        raise ValueError('알 수 없는 추출 필드')
                    return self.send(self.server.service.start(doc_id, data.get('agent'), data.get('model', ''), parts[3], data.get('expected_version'), data.get('expected_generation')), 202)
            if len(parts) == 4 and parts[:2] == ['api', 'items']:
                if parts[3] == 'review' and method == 'POST':
                    d = self.body(MAX_DICTIONARY_JSON_BYTES)
                    return self.send(store.review(parts[2], d.get('revision'), d.get('action'), d.get('actor'), d.get('reason'), d.get('payload')))
                if parts[3] == 'history' and method == 'GET':
                    return self.send(store.history(parts[2]))
            if route.path == '/api/cleanup':
                if method == 'GET':
                    return self.send({'cleanup_pending': len(store.pending_cleanup())})
                if method == 'POST':
                    if self.body():
                        raise ValueError('정리 재시도에는 빈 JSON 객체를 보내세요.')
                    return self.send(self.server.service.cleanup_logs())
            if route.path == '/api/runs' and method == 'GET':
                return self.send([self.server.service.with_usage('runs', row, coverage_details=False) for row in store.runs()])
            if len(parts) == 3 and parts[:2] == ['api', 'runs'] and method == 'GET':
                return self.send(self.server.service.with_usage('runs', store.run(parts[2])))
            if route.path == '/api/questions':
                if method == 'GET':
                    return self.send([self.server.service.with_usage('questions', row) for row in store.questions()])
                if method == 'POST':
                    return self.send(self.server.service.ask(self.body()), 202)
            if len(parts) == 3 and parts[:2] == ['api', 'questions'] and method == 'GET':
                return self.send(self.server.service.with_usage('questions', store.question(parts[2])))
            if route.path == '/api/query' and method == 'POST':
                return self.send(self.server.service.query(self.body()))
            if route.path == '/api/queries' and method == 'GET':
                return self.send(store.queries())
            self.send({'error': '경로를 찾을 수 없습니다.'}, 404)
        except PermissionError as exc:
            self.send({'error': str(exc)}, 403)
        except Conflict as exc:
            self.send({'error': str(exc)}, 409)
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            self.send({'error': str(exc)}, 400)
        except Exception as exc:
            self.send({'error': f'처리 실패: {type(exc).__name__}: {exc}'}, 500)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Verinote — versioned evidence wiki')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--config', type=Path, help='서버 전용 TOML 설정 파일 경로')
    parser.add_argument('--init-config', action='store_true', help='빈 비밀 설정 파일을 안전하게 생성하고 종료')
    args = parser.parse_args(argv)
    try:
        if args.init_config:
            created = create_config(args.config)
            print(f'설정 파일 생성 완료: {created} (키를 로컬에서 편집한 뒤 서버를 시작하세요.)')
            return
        credentials = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    root = args.data_dir.resolve()
    store = Store(root / 'verinote.sqlite3')
    store.recover()
    service = Service(store, root, credentials=credentials)
    server = Server(('127.0.0.1', args.port), store, service, credentials=credentials)
    print(f'Verinote: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
