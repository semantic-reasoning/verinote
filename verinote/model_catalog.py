"""Read-only installed-CLI model catalogs. Never submit a user prompt."""
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata

from .agents import CLI_PROVIDERS, executable, validate_model

TIMEOUT = 20
MAX_STDOUT = 1024 * 1024
MAX_STDERR = 256 * 1024
MAX_MODELS = 500
MAX_PAGES = 10
MESSAGES = {
    'ok': 'CLI가 안내하는 모델 목록입니다. 실제 사용 권한은 실행 시 확인됩니다.',
    'empty': 'CLI가 모델 목록을 반환하지 않았습니다. 모델 ID를 직접 입력할 수 있습니다.',
    'not_installed': 'CLI가 설치되지 않았습니다. 설치 후 다시 불러오세요.',
    'unavailable': '목록을 불러오지 못했습니다. CLI 로그인과 버전을 확인하거나 모델 ID를 직접 입력하세요.',
    'timeout': '모델 목록 조회 시간이 초과됐습니다. 다시 불러오거나 모델 ID를 직접 입력하세요.',
    'too_large': 'CLI 모델 목록이 조회 한도를 초과했습니다. 모델 ID를 직접 입력하세요.',
    'busy': '모델 목록을 조회 중입니다. 잠시 후 다시 불러오세요.',
    'unsupported': '이 환경에서는 모델 목록 조회를 지원하지 않습니다. 모델 ID를 직접 입력하세요.',
}


class CatalogError(Exception):
    def __init__(self, status='unavailable'):
        self.status = status


class _Process:
    """One deadline/byte budget across a complete catalog conversation."""
    def __init__(self, args, cwd, timeout):
        self.end = time.monotonic() + timeout
        self.deadline = self.end - min(1, timeout / 10)
        self.counts = {'out': 0, 'err': 0}
        self.buffer = b''
        self.selector = selectors.DefaultSelector()
        env = os.environ.copy()
        env.pop('OPENROUTER_API_KEY', None)
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True, bufsize=0)
        except BaseException:
            self.selector.close()
            raise
        for stream, name in ((self.proc.stdout, 'out'), (self.proc.stderr, 'err')):
            self.selector.register(stream, selectors.EVENT_READ, name)

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CatalogError('timeout')
        return left

    def read(self):
        while self.selector.get_map():
            events = self.selector.select(self.remaining())
            if not events:
                raise CatalogError('timeout')
            output = False
            for key, _ in events:
                data = os.read(key.fd, 65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    continue
                self.counts[key.data] += len(data)
                if self.counts[key.data] > (MAX_STDOUT if key.data == 'out' else MAX_STDERR):
                    raise CatalogError('too_large')
                if key.data == 'out':
                    self.buffer += data
                    output = True
            if output:
                return True
        return False

    def send(self, message, framed=False):
        self.remaining()
        raw = json.dumps(message, ensure_ascii=True).encode()
        if framed:
            raw = b'Content-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw
        else:
            raw += b'\n'
        # All requests are bounded fixed control messages (no prompts/credentials).
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def receive(self, framed=False):
        while True:
            self.remaining()
            if framed and b'\r\n\r\n' in self.buffer:
                head, body = self.buffer.split(b'\r\n\r\n', 1)
                lengths = re.findall(rb'(?im)^Content-Length:\s*(\d+)\s*$', head)
                if len(lengths) != 1:
                    raise CatalogError()
                size = int(lengths[0])
                if not 0 < size <= MAX_STDOUT:
                    raise CatalogError('too_large')
                if len(body) >= size:
                    raw, self.buffer = body[:size], body[size:]
                    break
            elif not framed and b'\n' in self.buffer:
                raw, self.buffer = self.buffer.split(b'\n', 1)
                break
            if not self.read():
                raise CatalogError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CatalogError()
        return value

    def rpc(self, method, params, request_id, *, framed=False):
        self.send({'jsonrpc':'2.0', 'id':request_id, 'method':method, 'params':params}, framed)
        while True:
            value = self.receive(framed)
            if type(value.get('id')) is not int or value['id'] != request_id:
                continue
            if 'error' in value or not isinstance(value.get('result'), dict):
                raise CatalogError()
            return value['result']

    def text(self):
        self.proc.stdin.close()
        while self.read():
            pass
        if self.proc.wait(timeout=self.remaining()) != 0:
            raise CatalogError()
        return self.buffer.decode('utf-8')

    def close(self):
        # Kill our isolated process group, including descendants holding pipes.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=max(.001, self.end-time.monotonic()))
        finally:
            self.selector.close()
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()


def _args(agent):
    return {
        'agy': ['models'],
        'cursor': ['--list-models'],
        'claude': ['-p','--input-format','stream-json','--output-format','stream-json','--verbose',
                   '--tools','','--strict-mcp-config','--setting-sources','','--settings','{"disableAllHooks":true}',
                   '--no-session-persistence'],
        'codex': ['app-server'],
        'copilot': ['--headless','--no-auto-update','--log-level','error','--stdio'],
    }[agent]


def _read_catalog(agent, process):
    if agent in ('agy','cursor'):
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', process.text())
        rows = []
        for line in text.splitlines():
            if agent == 'agy' and '\t' in line:
                rows.append(line.split('\t',1))
            elif agent == 'cursor' and ' - ' in line:
                rows.append(line.split(' - ',1))
        # An unknown output must not masquerade as an empty successful catalog.
        if not rows and text.strip() and text.strip() != 'Available models':
            raise CatalogError()
        return rows
    if agent == 'claude':
        process.send({'type':'control_request','request_id':'catalog', 'request':{'subtype':'initialize'}})
        while True:
            value = process.receive()
            if value.get('type') != 'control_response':
                continue
            response = value.get('response', {})
            if not isinstance(response, dict) or response.get('request_id') != 'catalog':
                continue
            if response.get('subtype') != 'success':
                raise CatalogError()
            body = response.get('response')
            if not isinstance(body, dict):
                raise CatalogError()
            rows = body.get('models')
            return _pairs(rows, 'value', 'displayName')
    if agent == 'copilot':
        process.rpc('connect', {}, 1, framed=True)
        result = process.rpc('models.list', {}, 2, framed=True)
        return _pairs(result.get('models'), 'id', 'name')
    process.rpc('initialize', {'clientInfo':{'name':'verinote_models','version':'0.1.0'}}, 1)
    process.send({'method':'initialized'})
    cursor, seen, rows = None, set(), []
    for page in range(MAX_PAGES):
        params = {'limit':100,'includeHidden':False}
        if cursor is not None:
            params['cursor'] = cursor
        result = process.rpc('model/list', params, page+2)
        rows.extend(_pairs(result.get('data'), 'model', 'displayName'))
        if len(rows) > MAX_MODELS:
            raise CatalogError('too_large')
        cursor = result.get('nextCursor')
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen:
            raise CatalogError()
        seen.add(cursor)
    raise CatalogError('too_large')


def _pairs(rows, id_key, name_key):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CatalogError()
    return [(row.get(id_key), row.get(name_key)) for row in rows]


def _normalize(rows):
    if len(rows) > MAX_MODELS:
        raise CatalogError('too_large')
    output, seen = [], set()
    for model, name in rows:
        try:
            model = validate_model(model)
        except ValueError:
            continue
        if not model or model in seen:
            continue
        if not isinstance(name, str) or not name.strip():
            name = model
        name = ''.join(c for c in name if not unicodedata.category(c).startswith('C')).strip()[:200]
        output.append({'id':model, 'name':name or model})
        seen.add(model)
    if rows and not output:
        raise CatalogError()
    return output


class ModelCatalog:
    def __init__(self, timeout=TIMEOUT):
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(2)
        self.locks = {agent:threading.Lock() for agent in CLI_PROVIDERS}

    def discover(self, agent):
        if agent not in CLI_PROVIDERS:
            raise ValueError('CLI 제공자의 모델 목록만 조회할 수 있습니다.')
        models, status = [], 'unavailable'
        if os.name != 'posix':
            return self.result('unsupported')
        lock = self.locks[agent]
        if not lock.acquire(blocking=False):
            return self.result('busy')
        try:
            if not self.slots.acquire(blocking=False):
                return self.result('busy')
            try:
                binary = executable(agent)
                if not binary:
                    return self.result('not_installed')
                with tempfile.TemporaryDirectory(prefix='verinote-models-') as cwd:
                    process = _Process([binary,*_args(agent)], cwd, self.timeout)
                    try:
                        models = _normalize(_read_catalog(agent, process))
                        status = 'ok' if models else 'empty'
                    finally:
                        process.close()
            except CatalogError as exc:
                status = exc.status
            except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
                status = 'unavailable'
            finally:
                self.slots.release()
        finally:
            lock.release()
        return self.result(status, models if status == 'ok' else [])

    @staticmethod
    def result(status, models=None):
        return {'status':status, 'models':models or [], 'message':MESSAGES[status]}
