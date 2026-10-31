"""Explicit provider adapters. No shell execution or caller-supplied flags."""
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .store import digest, now
from .transports import Credentials, ExecutionOptions, HTTPResult
from .usage import MAX_METADATA, atomic_json, calculate, decode_transport, empty_usage

PROVIDERS = {'claude': 'Claude', 'agy': 'Antigravity', 'copilot': 'GitHub Copilot', 'cursor': 'Cursor', 'codex': 'Codex', 'openrouter': 'OpenRouter (API)'}
TRANSPORTS = {agent: ('http' if agent == 'openrouter' else 'cli') for agent in PROVIDERS}
CLI_PROVIDERS = {agent: name for agent, name in PROVIDERS.items() if TRANSPORTS[agent] == 'cli'}


def executable(agent):
    if agent not in CLI_PROVIDERS:
        return None
    names = ['cursor-agent', 'agent'] if agent == 'cursor' else [agent]
    for name in names:
        if binary := shutil.which(name):
            return binary
    if agent == 'copilot' and sys.platform == 'win32':
        # Long-lived server processes can inherit PATH before CLI installation.
        for variable, parts in [('LOCALAPPDATA', ('Microsoft', 'WinGet', 'Links')),
                                ('APPDATA', ('npm',))]:
            if root := os.environ.get(variable):
                directory = Path(root).joinpath(*parts)
                for name in ('copilot.exe', 'copilot.cmd'):
                    if binary := shutil.which(name, path=str(directory)):
                        return binary
    return None


def validate_model(model, agent=None):
    if not isinstance(model, str) or len(model) > 150 or (model and (model.startswith('-') or not re.fullmatch(r'[\w./:@+\[\],= -]+', model))):
        raise ValueError('모델 ID 형식이 올바르지 않습니다.')
    model = model.strip()
    if agent == 'openrouter' and not model:
        raise ValueError('OpenRouter 모델 ID가 필요합니다.')
    return model


def command(agent, model, prompt):
    if agent not in CLI_PROVIDERS:
        raise ValueError('지원하지 않는 에이전트')
    model = validate_model(model)
    binary = 'cursor-agent' if agent == 'cursor' else agent
    flags = {
        'claude': ['--output-format', 'json', '--tools', '', '--strict-mcp-config'],
        'agy': ['--output-format', 'json', '--mode', 'plan', '--sandbox', '--disable-slash-commands'],
        'copilot': ['--silent', '--available-tools', '', '--disable-builtin-mcps'],
        'cursor': ['--output-format', 'stream-json', '--mode', 'ask', '--sandbox', 'enabled', '--trust'],
        'codex': ['exec', '--skip-git-repo-check', '--ephemeral', '--sandbox', 'read-only', '--color', 'never', '--json'],
    }[agent]
    args = [binary, *flags, *(['--model', model] if model else [])]
    if agent == 'copilot' and sys.platform == 'win32':
        return args, prompt
    return (args + ['-'], prompt) if agent == 'codex' else (args + ['-p', prompt], '')


def _kill_process_tree(proc, *, timeout=None):
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if proc.poll() is None:
                raise
    if timeout is None:
        proc.wait()
    else:
        proc.wait(timeout=timeout)


def parse_response(agent, response):
    """Recover only observed, unambiguous terminal-text framing artifacts."""
    response = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', response).strip()
    normalizations = []
    if agent == 'copilot':
        prefix = re.match(r'<final_answer>\s*</final_answer>\s*(?=\{)', response)
        if prefix:
            response = response[prefix.end():]
            normalizations.append('copilot_empty_final_answer_prefix')
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', response, re.S)
    if fence:
        response = fence[1].strip()
    try:
        result = json.loads(response)
    except json.JSONDecodeError:
        if agent != 'cursor':
            raise
        # Exact text equality avoids conflating e.g. true and 1, or choosing
        # between conflicting answers. A third object or trailing prose fails.
        result, end = json.JSONDecoder().raw_decode(response)
        if not isinstance(result, dict) or response[end:].strip() != response[:end]:
            raise
        normalizations.append('cursor_duplicate_json_object')
    if not isinstance(result, dict):
        raise ValueError('CLI 최종 응답은 JSON 객체여야 합니다.')
    return result, normalizations


class CLI:
    def __init__(self, timeout=None, max_output=1024 * 1024, rates=None, *,
                 http_adapter=None, credentials=None, sink=None, options=None, persistent=True):
        self.timeout, self.max_output = timeout, max_output
        self.rates: Callable[[], Any] = rates or (lambda: {})
        self.http_adapter, self._credentials, self.sink = http_adapter, credentials, sink
        if credentials is not None and not isinstance(credentials, Credentials):
            raise TypeError('credentials는 Credentials여야 합니다.')
        if options is not None and not isinstance(options, ExecutionOptions):
            raise TypeError('options는 ExecutionOptions여야 합니다.')
        if options is not None and timeout is not None and timeout != options.timeout:
            raise ValueError('고정 옵션과 timeout override가 일치해야 합니다.')
        self.options = options
        from .claude_worker import ExtractionPool, Manager
        self._claude: Manager | ExtractionPool | None = Manager() if persistent else None
        self._pinned_claude_binary: str | None = None

    def close(self):
        """Close the shared worker, including workers used by limited_timeout copies."""
        if self._claude is not None:
            self._claude.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def effective_timeout(self, agent, phase=None):
        if agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        if phase not in (None, 'ontology'):
            raise ValueError('알 수 없는 CLI 호출 단계')
        if agent == 'openrouter' and self.options is not None:
            return self.options.timeout
        return self.timeout if self.timeout is not None else (600 if phase == 'ontology' or agent in ('agy', 'copilot') else 180)

    def execution_options(self, agent, phase=None):
        timeout = self.effective_timeout(agent, phase)
        return self.options if agent == 'openrouter' and self.options is not None else ExecutionOptions(timeout=timeout)

    def limited_timeout(self, agent, seconds):
        timeout = self.effective_timeout(agent)
        if self.timeout is None and not (agent == 'openrouter' and self.options is not None):
            timeout = seconds
        timeout = min(timeout, seconds)
        ExecutionOptions(timeout=timeout)
        limited = copy.copy(self)
        limited.timeout = timeout
        if limited.options is not None:
            limited.options = replace(limited.options, timeout=timeout)
        return limited

    def ready(self, agent):
        if agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        if TRANSPORTS[agent] == 'cli':
            return bool(executable(agent))
        return bool(self.http_adapter is not None and self._credentials is not None
                    and self.http_adapter.ready(self._credentials))

    def update_caller_validation(self, folder, call_id, status):
        from .http_records import update_caller_validation
        return update_caller_validation(folder, call_id, status)

    def complete(self, agent, model, prompt, folder, *, phase=None):
        if agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        model = validate_model(model, agent)
        timeout = self.effective_timeout(agent, phase)
        if TRANSPORTS[agent] == 'http':
            if not self.ready(agent):
                raise ValueError('OpenRouter HTTP 어댑터 또는 API 자격 증명이 구성되지 않았습니다.')
            call_id = uuid4().hex
            from .http_records import HTTPRecord
            from .openrouter import _contains_secret
            options = self.execution_options(agent, phase)
            rate = self.rates().get(agent)
            key = self._credentials.api_key
            record = HTTPRecord(
                folder, call_id, model=None if _contains_secret(model, key) else model,
                phase=phase, prompt_hash=None if _contains_secret(prompt, key) else digest(prompt.encode()),
                started_at=now(), options=options,
                rate=None if _contains_secret(rate, key) else rate, max_output=self.max_output).begin()
            adapter = self.http_adapter
            from .openrouter import OpenRouter
            if isinstance(adapter, OpenRouter):
                adapter = adapter.with_max_output(self.max_output)
            try:
                result = adapter.complete(
                    agent, model, prompt, Path(folder), phase=phase,
                    options=options, credentials=self._credentials, call_id=call_id)
            except Exception:
                record.no_envelope()
                raise
            if not isinstance(result, HTTPResult) or result.envelope.call_id != call_id:
                record.no_envelope()
                raise ValueError('HTTP 어댑터 호출 결과 계약 오류')
            record.consume(result.envelope)
            if self.sink is not None:
                try:
                    self.sink(result.envelope)
                except Exception:
                    pass  # Optional telemetry cannot replace the primary outcome.
            if result.error_code is not None:
                record.adapter_finished(False)
                from .openrouter import ERROR_MESSAGES
                message = ERROR_MESSAGES.get(result.error_code, 'HTTP 제공자 호출 실패')
                if result.error_code == 'timeout':
                    raise TimeoutError(message)
                raise ValueError(message)
            if result.envelope.transport_status == 'timeout':
                record.adapter_finished(False)
                raise TimeoutError('HTTP 실행 시간 제한 초과')
            if result.envelope.transport_status != 'success':
                record.adapter_finished(False)
                raise ValueError('HTTP 제공자 호출 실패')
            if not isinstance(result.response, str):
                record.adapter_finished(False)
                raise ValueError('HTTP 최종 응답이 없습니다.')
            try:
                parsed = parse_response(agent, result.response)[0]
            except (ValueError, TypeError):
                record.adapter_finished(False)
                raise
            record.adapter_finished(True)
            return parsed
        from .cli_timing import start_timing
        timing = start_timing(agent, model, phase, timeout, self.max_output)
        try:
            if agent == 'claude' and self._claude is not None:
                timing.persistent()
                return self._complete_persistent(model, prompt, folder, phase, timing)
            return self._complete_cli(agent, model, prompt, folder, phase, timing)
        finally:
            timing.finish(folder)

    def _complete_persistent(self, model, prompt, folder, phase, timing):
        from .claude_worker import Exchange
        assert self._claude is not None
        timeout = self.effective_timeout('claude', phase)
        deadline = time.monotonic() + timeout
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        usage = empty_usage('claude', model, self.rates().get('claude'))
        usage.update(cli_call_id=timing.call_id, started_at=now())
        meta = dict(cli_call_id=timing.call_id, agent='claude', model=model, phase=phase,
                    prompt_sha256=digest(prompt.encode()), started_at=usage['started_at'],
                    status='running', timeout_seconds=timeout, transport_mode='persistent',
                    process_reused=None, returncode=None)
        atomic_json(folder / 'usage.json', usage)
        atomic_json(folder / 'run.json', meta)
        if getattr(self._claude, 'pool_id', None) is not None:
            meta['worker_scope'] = usage['worker_scope'] = 'extraction_pool'
            meta['worker_identity'] = usage['worker_identity'] = None
        worker = None
        error_code = 'preparation'
        try:
            if prompt.lstrip().startswith('/'):
                raise ValueError('Claude 지속 연결에는 명령 대신 일반 요청문이 필요합니다.')
            binary = self._pinned_claude_binary or executable('claude')
            if not binary:
                error_code = 'executable_missing'
                raise ValueError('Claude CLI가 설치되지 않았습니다.')
            error_code = 'process'
            with self._claude.acquire(binary, model, deadline, timing) as worker:
                if worker.extraction_identity is not None:
                    meta['worker_identity'] = copy.deepcopy(worker.extraction_identity)
                    usage['worker_identity'] = copy.deepcopy(worker.extraction_identity)
                meta['process_reused'] = timing.is_reused()
                usage['status'] = 'running'
                atomic_json(folder / 'usage.json', usage)
                with (folder / 'stream.jsonl').open('wb') as stream, (folder / 'stderr.txt').open('wb') as stderr:
                    closed = worker.manager_closed if worker.manager_closed is not None else self._claude.closed
                    exchange = Exchange(worker, deadline, closed, self.max_output, stream, stderr, timing)
                    exchange.send('/clear')
                    exchange.terminal(reset=True)
                    timing.mark('reset_completed')
                    exchange.inference = True
                    exchange.send(prompt)
                    timing.mark('request_sent')
                    envelope = exchange.terminal()
                    timing.mark('response_received')
                    # Keep the existing single-result decoder and per-call usage contract.
                    raw = json.dumps(envelope, ensure_ascii=False)
                    (folder / 'stdout.txt').write_text(raw, encoding='utf-8')
                    timing.mark('interpret_started')
                    error_code = 'interpretation'
                    response, parsed, provider_error = decode_transport('claude', raw)
                    usage.update({k: v for k, v in parsed.items() if k not in ('requested_model', 'rate_snapshot', 'status', 'schema_version')})
                    calculate(usage)
                    if provider_error or response is None or envelope.get('subtype') != 'success':
                        raise ValueError(provider_error or 'Claude 실행 실패')
                    result, normalizations = parse_response('claude', response)
                    timing.mark('interpret_completed')
                    timing.outcome('success')
                    usage['status'] = 'success'
                    meta.update(status='success', response_normalizations=normalizations)
                    error_code = 'recording'
                    atomic_json(folder / 'run.json', meta)
                    return result
        except BaseException as exc:
            status = 'timeout' if isinstance(exc, TimeoutError) else 'error' if worker is not None else 'not_started'
            if isinstance(exc, TimeoutError):
                error_code = 'timeout'
            elif '크기 제한' in str(exc):
                error_code = 'output_limit'
            code = worker.proc.poll() if worker is not None else None
            if code is not None:
                timing.mark('exit_observed')
            timing.outcome(status, error_code, code)
            usage['status'] = 'not_started' if status == 'not_started' else 'error'
            meta.update(status='error', error=str(exc), returncode=code, process_reused=timing.is_reused())
            try:
                atomic_json(folder / 'run.json', meta)
            except OSError:
                pass
            raise
        finally:
            usage['finished_at'] = now()
            try:
                atomic_json(folder / 'usage.json', usage)
            except OSError:
                pass

    def _complete_cli(self, agent, model, prompt, folder, phase, timing):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        args, stdin = command(agent, model, prompt)
        usage_path = folder / 'provider-usage.json'
        if agent == 'copilot':
            args[1:1] = ['--usage-output-file', str(usage_path.resolve())]
        usage = empty_usage(agent, model, self.rates().get(agent))
        usage['cli_call_id'] = timing.call_id
        timeout = self.effective_timeout(agent, phase)
        meta = {'cli_call_id': timing.call_id, 'agent': agent, 'model': model, 'phase': phase, 'prompt_sha256': digest(prompt.encode()), 'started_at': now(), 'status': 'running', 'timeout_seconds': timeout}
        def record_meta(status, **fields):
            meta.update(status=status, **fields)
            atomic_json(folder / 'run.json', meta)
        def capture():
            nonlocal usage
            raw = ''
            if (folder / 'stdout.txt').exists():
                with (folder / 'stdout.txt').open('rb') as source:
                    raw = source.read(self.max_output).decode('utf-8', errors='replace')
            provider = None
            try:
                if usage_path.exists() and usage_path.stat().st_size <= MAX_METADATA:
                    provider = json.loads(usage_path.read_text(encoding='utf-8'))
            except (OSError, ValueError, UnicodeError):
                pass
            answer, parsed, error = decode_transport(agent, raw, provider)
            usage.update({k: v for k, v in parsed.items() if k not in ('requested_model', 'rate_snapshot', 'status', 'schema_version')})
            calculate(usage)
            return answer, error
        usage['started_at'] = meta['started_at']
        atomic_json(folder / 'usage.json', usage)
        record_meta('running')
        proc = None
        captured = False
        timing_error = 'executable_missing'
        try:
            args[0] = executable(agent)
            if not args[0]:
                raise ValueError(f'{PROVIDERS[agent]} CLI가 설치되지 않았습니다.')
            timing_error = 'preparation'
            work = folder / 'work'
            work.mkdir(exist_ok=True)
            with (folder / 'stdout.txt').open('wb') as out, (folder / 'stderr.txt').open('wb') as err, (folder / 'stdin.txt').open('wb+') as inp:
                inp.write(stdin.encode()); inp.seek(0)
                child_env = os.environ.copy()
                child_env.pop('OPENROUTER_API_KEY', None)
                process_options = {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt' else {'start_new_session': True}
                timing_error = 'spawn'
                timing.mark('spawn_requested')
                proc = subprocess.Popen(args, stdin=inp, stdout=out, stderr=err, cwd=work, env=child_env, **process_options)
                timing.mark('spawn_returned')
                timing_error = 'process'
                usage['status'] = 'running'
                atomic_json(folder / 'usage.json', usage)
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    timing.observe_stdout(folder / 'stdout.txt')
                    if time.monotonic() > deadline:
                        raise TimeoutError(f'CLI 실행 시간 제한 초과 ({timeout}초)')
                    if out.tell() + err.tell() > self.max_output or (usage_path.exists() and usage_path.stat().st_size > MAX_METADATA):
                        timing_error = 'output_limit'
                        raise ValueError('CLI 응답 크기 제한 초과')
                    time.sleep(.05)
                timing.mark('exit_observed')
                timing.observe_stdout(folder / 'stdout.txt')
                if out.tell() + err.tell() > self.max_output or (usage_path.exists() and usage_path.stat().st_size > MAX_METADATA):
                    timing_error = 'output_limit'
                    raise ValueError('CLI 응답 크기 제한 초과')
            timing_error = 'interpretation'
            timing.mark('interpret_started')
            response, provider_error = capture()
            captured = True
            if proc.returncode:
                timing_error = 'exit_nonzero'
                raise ValueError(f'CLI 종료 코드 {proc.returncode}. 실행 로그의 stderr를 확인하세요.')
            if provider_error or response is None:
                raise ValueError(provider_error or 'CLI의 최종 응답을 읽지 못했습니다.')
            try:
                result, normalizations = parse_response(agent, response)
            except json.JSONDecodeError as exc:
                raise json.JSONDecodeError(f'{PROVIDERS[agent]} 최종 JSON 응답 형식 오류: {exc.msg}', exc.doc, exc.pos) from exc
            timing.mark('interpret_completed')
            timing.outcome('success', exit_code=proc.returncode)
            usage['status'] = 'success'
            timing_error = 'recording'
            record_meta('success', returncode=proc.returncode, response_normalizations=normalizations)
            return result
        except Exception as exc:
            timing.outcome('not_started' if proc is None else 'timeout' if isinstance(exc, TimeoutError) else 'error',
                           'timeout' if isinstance(exc, TimeoutError) and proc is not None else timing_error)
            if proc and proc.poll() is None:
                _kill_process_tree(proc)
            if proc is not None:
                timing.mark('exit_observed')
                timing.observe_stdout(folder / 'stdout.txt')
                timing.outcome('timeout' if isinstance(exc, TimeoutError) else 'error',
                               'timeout' if isinstance(exc, TimeoutError) else timing_error, proc.returncode)
            if not captured:
                try:
                    capture()
                except Exception:
                    usage['warnings'].append('실패한 호출의 사용량을 복구하지 못했습니다.')
            usage['status'] = 'error' if proc else 'not_started'
            if not proc or proc.returncode:
                usage['complete'] = False
            try:
                record_meta('error', error=str(exc))
            except OSError:
                pass  # Preserve the primary failure if log storage is unavailable.
            raise
        finally:
            usage['finished_at'] = now()
            try:
                atomic_json(folder / 'usage.json', usage)
            except OSError:
                # Optional telemetry must preserve both valid responses and primary failures.
                try:
                    record_meta(meta['status'], usage_warning='사용량 저장에 실패해 이 호출의 사용량 기록이 불완전합니다.')
                except OSError:
                    pass
