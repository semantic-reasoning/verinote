"""Single-shot OpenRouter HTTPS transport. No persistence, retry or routing policy."""
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
from typing import Any

from .transports import Credentials, HTTPResult, SafeCallResult
from .usage import MAX_METADATA

HOST = 'openrouter.ai'
PATH = '/api/v1/chat/completions'
MAX_OUTPUT = 1024 * 1024
# Timed-out system resolvers cannot be killed by Python. Retain their slot until
# they exit, so stalled DNS cannot create an unbounded number of background jobs.
_WORKERS = threading.BoundedSemaphore(8)

ERROR_MESSAGES = {
    'configuration': 'OpenRouter 실행 설정이 올바르지 않습니다.',
    'busy': 'OpenRouter 호출 자원이 사용 중입니다.',
    'secret': 'OpenRouter 요청 또는 응답에서 자격 증명이 감지되었습니다.',
    'auth': 'OpenRouter 인증에 실패했습니다 (401).',
    'balance': 'OpenRouter 잔액이 부족합니다 (402).',
    'request': 'OpenRouter 요청 또는 모델이 올바르지 않습니다.',
    'rate_limit': 'OpenRouter 호출 한도를 초과했습니다 (429).',
    'server': 'OpenRouter 서버 오류가 발생했습니다.',
    'http': 'OpenRouter HTTP 요청에 실패했습니다.',
    'redirect': 'OpenRouter HTTP redirect는 허용되지 않습니다.',
    'tls': 'OpenRouter TLS 인증 또는 연결에 실패했습니다.',
    'connection': 'OpenRouter 연결이 끊겼거나 연결할 수 없습니다. 전송 후 과금 여부는 미확인입니다.',
    'timeout': 'OpenRouter 전체 호출 시간 제한을 초과했습니다. 전송 후 과금 여부는 미확인입니다.',
    'size': 'OpenRouter 응답 크기 제한을 초과했습니다.',
    'encoding': 'OpenRouter 응답 인코딩이 올바르지 않거나 지원되지 않습니다.',
    'format': 'OpenRouter 응답 형식이 올바르지 않습니다.',
    'provider': 'OpenRouter 제공자가 호출 오류를 반환했습니다.',
    'refusal': 'OpenRouter 제공자가 응답을 거부했습니다.',
    'tools': 'OpenRouter 도구 호출 응답은 허용되지 않습니다.',
    'finish': 'OpenRouter 응답이 정상적으로 완료되지 않았습니다.',
    'content': 'OpenRouter 최종 응답은 비어 있지 않은 JSON 객체여야 합니다.',
}


class _Failure(Exception):
    def __init__(self, code):
        self.code = code


class _Call:
    def __init__(self, deadline, clock):
        self.deadline, self.clock = deadline, clock
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.connection = None
        self.response = None
        self.socket = None
        self.sent = False
        self.http_status = None
        self.received = False

    def remaining(self):
        remaining = self.deadline - self.clock()
        if self.cancelled.is_set() or remaining <= 0:
            raise _Failure('timeout')
        return remaining

    def abort(self):
        self.cancelled.set()
        with self.lock:
            conn = self.connection
            sock = self.socket or getattr(conn, 'sock', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def snapshot(self):
        with self.lock:
            return {'transmission_state': ('received' if self.received else 'sent_unknown' if self.sent else 'not_sent'),
                    'http_status': self.http_status}


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, call):
        super().__init__(HOST, timeout=call.remaining(), context=ssl.create_default_context())
        self.call = call

    def connect(self):
        self.call.remaining()
        super().connect()
        # DNS/handshake can finish after the caller has already timed out.
        self.sock.settimeout(self.call.remaining())

    def send(self, data):
        self.call.remaining()
        if self.sock is None:
            self.connect()
        self.sock.settimeout(self.call.remaining())
        # Serialize send admission with cancellation state. sent is conservative:
        # an interrupted send never means "unbilled" or "remotely cancelled".
        with self.call.lock:
            self.call.remaining()
            self.call.sent = True
        super().send(data)


def _contains_secret(value, key):
    """Check decoded JSON keys/strings, including JSON embedded in content."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if key in item:
                return True
            # JSON string content can itself contain escaped JSON keys/values.
            # Unescape JSON's exact unicode escape form, not arbitrary encodings.
            unescaped = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), item)
            if key in unescaped:
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _json(raw):
    def invalid_constant(_):
        raise ValueError('nonfinite')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('nonfinite')
        return parsed
    return json.loads(raw, parse_constant=invalid_constant, parse_float=finite_float, object_pairs_hook=unique)


def _usage(value):
    """Preserve raw numeric usage evidence, without normalization or pricing."""
    if not isinstance(value, dict):
        return None
    pending: list[tuple[tuple[str, ...], Any]] = [((), value)]
    while pending:
        path, item = pending.pop()
        if isinstance(item, dict):
            pending.extend((path + (key,), part) for key, part in item.items())
        elif path == ('is_byok',) and type(item) is bool:
            continue
        elif path == ('cost_details', 'server_tool_cost') and item is None:
            continue
        elif type(item) not in (int, float) or not 0 <= item <= 10**15 or not math.isfinite(item):
            return None
    return value


def _metadata(payload, model, status):
    meta = {'requested_model': model, 'http_status': status, 'transmission_state': 'received'}
    resolved = payload.get('model')
    if isinstance(resolved, str) and len(resolved) <= 150 and re.fullmatch(r'[\w./:@+\[\],= -]+', resolved):
        meta['resolved_model'] = resolved
    generation = payload.get('id')
    if isinstance(generation, str) and re.fullmatch(r'gen-[A-Za-z0-9_-]{1,200}', generation):
        meta['generation_id'] = generation
    choices = payload.get('choices')
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        finish = choices[0].get('finish_reason')
        if isinstance(finish, str) and finish in ('stop', 'length', 'tool_calls', 'content_filter', 'function_call', 'error'):
            meta['finish_reason'] = finish
    usage = _usage(payload.get('usage'))
    if usage is not None:
        meta['usage'] = usage
    if len(json.dumps(meta, ensure_ascii=True).encode()) > MAX_METADATA:
        meta.pop('usage', None)
        meta['usage_unavailable'] = True
    return meta


def _http_error(status):
    if 300 <= status < 400:
        return 'redirect'
    if status == 401:
        return 'auth'
    if status == 402:
        return 'balance'
    if status == 429:
        return 'rate_limit'
    if status >= 500:
        return 'server'
    return 'request' if status in (400, 403, 404, 405, 413, 422) else 'http'


def _provider_error(value):
    code = value.get('code') if isinstance(value, dict) else None
    return _http_error(code) if type(code) is int and 400 <= code <= 599 else 'provider'


def _content(payload):
    if 'error' in payload:
        raise _Failure(_provider_error(payload['error']))
    choices = payload.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _Failure('format')
    choice = choices[0]
    if 'error' in choice:
        raise _Failure(_provider_error(choice['error']))
    message = choice.get('message')
    if not isinstance(message, dict):
        raise _Failure('format')
    if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
        raise _Failure('refusal')
    if message.get('tool_calls') or message.get('function_call'):
        raise _Failure('tools')
    if choice.get('finish_reason') != 'stop':
        raise _Failure('finish')
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        raise _Failure('content')
    return content, choice['finish_reason']


class OpenRouter:
    def __init__(self, max_output=MAX_OUTPUT):
        if type(max_output) is not int or max_output <= 0:
            raise ValueError('max_output은 양의 정수여야 합니다.')
        self.max_output = max_output

    def with_max_output(self, limit):
        """Per-call copy: never mutate a shared adapter or change fake contracts."""
        return OpenRouter(min(self.max_output, limit))

    def ready(self, credentials):
        return isinstance(credentials, Credentials) and bool(credentials.api_key)

    def complete(self, agent, model, prompt, folder, *, phase, options, credentials, call_id):
        call = _Call(time.monotonic() + options.timeout, time.monotonic)
        done = threading.Event()
        results = []
        # Configuration and secret checks also run inside the total time budget.
        def worker():
            try:
                results.append(self._run(call, agent, model, prompt, options, credentials, call_id))
            except _Failure as failure:
                results.append(self._failed(call, call_id, failure.code))
            except ssl.SSLError:
                results.append(self._failed(call, call_id, 'tls'))
            except (TimeoutError, socket.timeout):
                results.append(self._failed(call, call_id, 'timeout'))
            except Exception:
                results.append(self._failed(call, call_id, 'connection'))
            finally:
                try:
                    if call.response is not None:
                        call.response.close()
                except Exception:
                    pass
                try:
                    if call.connection is not None:
                        call.connection.close()
                except Exception:
                    pass
                _WORKERS.release()
                done.set()

        if not _WORKERS.acquire(blocking=False):
            return self._failed(call, call_id, 'busy')
        threading.Thread(target=worker, name='verinote-openrouter', daemon=True).start()
        remaining = max(0, call.deadline - time.monotonic())
        if not done.wait(remaining) or time.monotonic() >= call.deadline:
            call.abort()
            return self._failed(call, call_id, 'timeout')
        return results[0]

    @staticmethod
    def _failed(call, call_id, code):
        meta = call.snapshot() | {'error_code': code, 'validation_status': 'unverified'}
        status = 'timeout' if code == 'timeout' else 'error' if call.sent else 'not_started'
        return HTTPResult(SafeCallResult(call_id, status, meta), error_code=code)

    def _run(self, call, agent, model, prompt, options, credentials, call_id):
        call.remaining()
        if agent != 'openrouter' or not self.ready(credentials) or not isinstance(prompt, str):
            raise _Failure('configuration')
        key = credentials.api_key
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise _Failure('configuration')
        from .agents import parse_response, validate_model
        try:
            model = validate_model(model, agent)
        except ValueError:
            raise _Failure('configuration') from None
        body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                'stream': False, 'max_tokens': options.max_tokens}
        if _contains_secret(body, key):
            raise _Failure('secret')
        data = json.dumps(body, ensure_ascii=True).encode('utf-8')
        conn = _HTTPS(call)
        with call.lock:
            call.connection = conn
        conn.connect()
        with call.lock:
            call.socket = conn.sock
        call.remaining()
        conn.request('POST', PATH, body=data,
                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'})
        call.remaining()
        response = conn.getresponse()
        with call.lock:
            call.response = response
            call.http_status = response.status
        call.remaining()
        encoding = response.getheader('Content-Encoding', 'identity').lower().strip()
        if encoding not in ('', 'identity'):
            raise _Failure('encoding')
        # Ignore Content-Length as a bound. HTTP framing is still enforced by
        # http.client; a truncated declared body fails rather than becoming safe.
        expected = response.length
        if not response.chunked:
            response.length = None  # Read until EOF; detect dishonest short lengths.
        chunks, total = [], 0
        while True:
            call.remaining()
            if conn.sock is not None:
                conn.sock.settimeout(call.remaining())
            chunk = response.read1(min(65536, self.max_output - total + 1))
            call.remaining()
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_output:
                raise _Failure('size')
            chunks.append(chunk)
        if expected is not None and total != expected:
            raise _Failure('connection')
        with call.lock:
            call.received = True
        raw = b''.join(chunks)
        try:
            decoded = raw.decode('utf-8')
        except UnicodeError:
            raise _Failure('encoding') from None
        if _contains_secret(decoded, key):
            raise _Failure('secret')
        try:
            payload = _json(decoded)
        except (ValueError, RecursionError):
            raise _Failure(_http_error(response.status) if response.status != 200 else 'format') from None
        if _contains_secret(payload, key):
            raise _Failure('secret')
        if not isinstance(payload, dict):
            raise _Failure('format')
        meta = _metadata(payload, model, response.status)
        code, content = None, None
        finish = None
        if response.status != 200:
            code = _http_error(response.status)
        else:
            try:
                content, finish = _content(payload)
                result, _ = parse_response('openrouter', content)
                if _contains_secret(result, key):
                    raise _Failure('secret')
                # The common parser accepts Python nonfinite values; HTTP does not.
                _json(json.dumps(result, allow_nan=False))
            except _Failure as failure:
                code = failure.code
            except (ValueError, TypeError, RecursionError):
                code = 'content'
        if code == 'secret':
            raise _Failure('secret')
        call.remaining()
        meta['validation_status'] = 'failed' if code else 'valid'
        if finish is not None:
            meta['finish_reason'] = finish
        if code:
            meta['error_code'] = code
        return HTTPResult(SafeCallResult(call_id, 'success', meta, True, payload),
                          content if not code else None, error_code=code)
