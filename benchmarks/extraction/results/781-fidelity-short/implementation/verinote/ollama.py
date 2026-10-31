"""Bounded loopback-only Ollama generation and independent call evidence."""
import copy
import http.client
import json
import math
import socket
import time
from pathlib import Path
from uuid import uuid4

from .store import digest, now
from .usage import atomic_json, calculate, empty_usage, number

ENDPOINT = 'http://127.0.0.1:11434'
DEFAULTS = {'provider': 'ollama', 'schema_version': 1, 'endpoint': ENDPOINT,
            'num_ctx': 32768, 'num_predict': 4096, 'temperature': 0, 'keep_alive': '5m',
            'timeouts': {'default': 180, 'ontology': 600}, 'model': None, 'model_digest': None}


def options(value=None):
    value = copy.deepcopy(DEFAULTS if value is None else value)
    candidate = isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 2
    required = set(DEFAULTS) | ({'candidate_id', 'candidate_template_sha256'} if candidate else set())
    if not isinstance(value, dict) or set(value) != required: raise ValueError('Ollama 실행 옵션 형식 오류')
    if any(value[k] != DEFAULTS[k] or type(value[k]) is not type(DEFAULTS[k])
           for k in DEFAULTS if k not in ('timeouts', 'model', 'model_digest', 'schema_version')): raise ValueError('지원하지 않는 Ollama 생성 옵션')
    if type(value['schema_version']) is not int or value['schema_version'] not in (1, 2): raise ValueError('Ollama 옵션 version 오류')
    if candidate:
        from .ollama_fidelity import CANDIDATE_ID, template_hash
        if value['candidate_id'] != CANDIDATE_ID or value['candidate_template_sha256'] != template_hash():
            raise ValueError('Ollama 후보 identity 오류')
    if not isinstance(value['timeouts'], dict) or set(value['timeouts']) != {'default', 'ontology'}:
        raise ValueError('Ollama timeout 형식 오류')
    for v in value['timeouts'].values():
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 600:
            raise ValueError('Ollama timeout 범위 오류')
    if value['model'] is not None or value['model_digest'] is not None:
        from .agents import validate_model
        validate_model(value['model'], 'ollama')
        identity = value['model_digest']
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Ollama 고정 모델 digest 오류')
    return value


def request(path, payload=None, *, timeout=5, limit=1024*1024):
    """http.client ignores proxy environment and never follows redirects."""
    conn = http.client.HTTPConnection('127.0.0.1', 11434, timeout=timeout)
    deadline = time.monotonic()+timeout
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        conn.request('GET' if body is None else 'POST', path, body, {'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200: raise ValueError(f'Ollama HTTP 오류 ({response.status})')
        parts = []; size = 0
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError('Ollama 요청 시간 초과')
            active_socket = conn.sock or getattr(getattr(response.fp, 'raw', None), '_sock', None)
            if active_socket is not None: active_socket.settimeout(remaining)
            chunk = response.read1(min(65536, limit+1-size))
            if not chunk: break
            size += len(chunk)
            if size > limit: raise ValueError('Ollama 응답 크기 한도 초과')
            parts.append(chunk)
        raw = b''.join(parts).decode('utf-8')
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError('Ollama 응답 객체 오류')
        return value
    except socket.timeout as exc:
        raise TimeoutError('Ollama 요청 시간 초과') from exc
    finally: conn.close()


def catalog(*, timeout=5):
    from .agents import validate_model
    rows = request('/api/tags', timeout=timeout).get('models')
    if not isinstance(rows, list) or len(rows) > 500: raise ValueError('Ollama 모델 목록 오류')
    result = []
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Ollama 모델 정보 오류')
        name = validate_model(row.get('name'), 'ollama')
        identity = row.get('digest')
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Ollama 모델 digest 오류')
        result.append({'id': name, 'name': name, 'digest': identity})
    return result


def ready():
    try:
        return isinstance(request('/api/version', timeout=2).get('version'), str)
    except (OSError, ValueError): return False


class Job:
    def __init__(self, dispatcher, snapshot=None, *, model=None):
        self.options = options(snapshot)
        if model is not None:
            installed = next((r for r in catalog() if r['id'] == model), None)
            if installed is None: raise ValueError('Ollama에 설치된 정확한 모델 ID가 필요합니다.')
            self.options.update(model=model, model_digest=installed['digest'])
            self.options = options(self.options)
        self.max_output = dispatcher.max_output
        self.rates = dispatcher.rates

    def effective_timeout(self, agent, phase=None):
        if agent != 'ollama' or phase not in (None, 'ontology'): raise ValueError('Ollama 작업 제공자 오류')
        return self.options['timeouts']['ontology' if phase == 'ontology' else 'default']

    def limited_timeout(self, agent, seconds):
        value = copy.copy(self); value.options = copy.deepcopy(self.options)
        value.options['timeouts']['default'] = min(self.effective_timeout(agent), seconds)
        return value

    def close(self): pass

    def complete(self, agent, model, prompt, folder, *, phase=None):
        from .agents import parse_response, validate_model
        model = validate_model(model, 'ollama')
        timeout = self.effective_timeout(agent, phase)
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        original_prompt = prompt
        if self.options['schema_version'] == 2 and phase is None and folder.name != 'quote-repair':
            from .ollama_fidelity import compile_prompt
            prompt = compile_prompt(prompt)
        call_id = uuid4().hex
        usage = empty_usage('ollama', model, self.rates().get('ollama'))
        usage.update(provider_call_id=call_id, cli_call_id=call_id, started_at=now(), source='ollama_api')
        meta = {'agent': 'ollama', 'model': model, 'transport': 'local_http', 'provider_call_id': call_id,
                'cli_call_id': call_id, 'prompt_sha256': digest(prompt.encode()), 'started_at': usage['started_at'],
                'status': 'running', 'execution_options': copy.deepcopy(self.options)}
        if self.options['schema_version'] == 2:
            meta['original_prompt_sha256'] = digest(original_prompt.encode())
            if original_prompt != prompt: (folder/'original-prompt.txt').write_text(original_prompt, encoding='utf-8')
        atomic_json(folder/'run.json', meta); atomic_json(folder/'usage.json', usage)
        started = time.monotonic()
        try:
            # Byte admission is intentionally conservative; reserve template framing.
            if len(prompt.encode()) > self.options['num_ctx']-self.options['num_predict']-1024:
                raise ValueError('Ollama 입력이 context 여유 한도를 초과합니다. 문서를 나누세요.')
            (folder/'prompt.txt').write_text(prompt, encoding='utf-8')
            models = catalog(timeout=min(5, timeout))
            installed = next((r for r in models if r['id'] == model), None)
            if installed is None: raise ValueError('Ollama에 설치된 정확한 모델 ID가 필요합니다.')
            if self.options['model'] is not None and (self.options['model'] != model or self.options['model_digest'] != installed['digest']):
                raise ValueError('Ollama 모델이 작업 등록 이후 변경됐습니다. 다시 등록하세요.')
            meta['model_digest'] = installed['digest']
            remaining = timeout-(time.monotonic()-started)
            if remaining <= 0: raise TimeoutError('Ollama 요청 시간 초과')
            value = request('/api/generate', {'model': model, 'prompt': prompt, 'stream': False, 'format': 'json',
                'keep_alive': self.options['keep_alive'], 'options': {k: self.options[k] for k in
                    ('num_ctx', 'num_predict', 'temperature')}}, timeout=remaining, limit=self.max_output)
            atomic_json(folder/'response.json', value)
            usage['resolved_model'] = value.get('model') if value.get('model') == model else None
            usage['tokens']['input'] = number(value.get('prompt_eval_count'), True)
            usage['tokens']['output'] = number(value.get('eval_count'), True)
            usage['complete'] = all(usage['tokens'][k] is not None for k in ('input', 'output'))
            usage['provider_timings_ns'] = {k: number(value.get(k), True) for k in
                ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')}
            calculate(usage)
            if value.get('model') != model: raise ValueError('Ollama 응답 모델이 요청과 다릅니다.')
            if value.get('done') is not True or value.get('done_reason') != 'stop':
                raise ValueError('Ollama 응답이 완성되지 않았습니다.')
            if not isinstance(value.get('response'), str): raise ValueError('Ollama 최종 응답 오류')
            result = parse_response('ollama', value['response'])[0]
            meta['status'] = usage['status'] = 'success'
            return result
        except BaseException:
            meta['status'] = usage['status'] = 'error'
            raise
        finally:
            meta['finished_at'] = usage['finished_at'] = now()
            meta['seconds'] = time.monotonic()-started
            atomic_json(folder/'usage.json', usage); atomic_json(folder/'run.json', meta)
