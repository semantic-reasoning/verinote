"""Single-user loopback HTTP server with same-origin mutation protection."""
import argparse
import base64
import binascii
import difflib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .agents import PROVIDERS, executable, validate_model
from .documents import MAX_BYTES, decode_document
from .service import Service, text
from .store import Conflict, Store

WEB = Path(__file__).with_name('web')


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, service):
        self.store, self.service = store, service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
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

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('application/json만 지원합니다.')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 6 * 1024 * 1024:
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
            if route.path == '/api/settings':
                if method == 'GET':
                    return self.send(store.settings() | {'providers': [{'id': k, 'name': v, 'installed': bool(executable(k))} for k, v in PROVIDERS.items()]})
                if method == 'PUT':
                    data = self.body()
                    if data.get('selected') not in PROVIDERS or not isinstance(data.get('models'), dict) or set(data['models']) - set(PROVIDERS):
                        raise ValueError('에이전트 설정 형식 오류')
                    return self.send(store.save_settings({'selected': data['selected'], 'models': {k: validate_model(v) for k, v in data['models'].items()}}))
            if route.path == '/api/documents':
                if method == 'GET':
                    return self.send(store.documents())
                if method == 'POST':
                    data = self.body()
                    title = text(data.get('title'), '제목')
                    filename = text(data.get('filename'), '파일명')
                    if 'content_base64' in data:
                        raw = base64.b64decode(data['content_base64'], validate=True)
                    else:
                        text(data.get('text'), '본문', 128000)
                        raw = data['text'].encode()
                    if len(raw) > MAX_BYTES:
                        raise ValueError('파일 크기 제한 초과')
                    content = decode_document(filename, raw)
                    return self.send(store.register(title, filename, raw, content, data.get('document_id'), data.get('expected_version')), 201)
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
                    previous = store.document(doc_id, doc['version'] - 1)['text'] if doc['version'] > 1 else ''
                    return self.send({'diff': ''.join(difflib.unified_diff(previous.splitlines(True), doc['text'].splitlines(True), fromfile=f'v{doc["version"] - 1}', tofile=f'v{doc["version"]}'))})
                if len(parts) == 4 and parts[3] == 'extract' and method == 'POST':
                    data = self.body()
                    return self.send(self.server.service.start(doc_id, data.get('agent'), data.get('model', '')), 202)
            if len(parts) == 4 and parts[:2] == ['api', 'items']:
                if parts[3] == 'review' and method == 'POST':
                    d = self.body()
                    return self.send(store.review(parts[2], d.get('revision'), d.get('action'), d.get('actor'), d.get('reason'), d.get('payload')))
                if parts[3] == 'history' and method == 'GET':
                    return self.send(store.history(parts[2]))
            if route.path == '/api/runs' and method == 'GET':
                return self.send(store.runs())
            if len(parts) == 3 and parts[:2] == ['api', 'runs'] and method == 'GET':
                return self.send(store.run(parts[2]))
            if route.path == '/api/questions':
                if method == 'GET':
                    return self.send(store.questions())
                if method == 'POST':
                    return self.send(self.server.service.ask(self.body()), 202)
            if len(parts) == 3 and parts[:2] == ['api', 'questions'] and method == 'GET':
                return self.send(store.question(parts[2]))
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


def main():
    parser = argparse.ArgumentParser(description='Verinote — versioned evidence wiki')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    args = parser.parse_args()
    root = args.data_dir.resolve()
    store = Store(root / 'verinote.sqlite3')
    store.recover()
    service = Service(store, root)
    server = Server(('127.0.0.1', args.port), store, service)
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
