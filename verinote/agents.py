"""Explicit provider adapters. No shell execution or caller-supplied flags."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from .store import digest, now
from .usage import MAX_METADATA, atomic_json, calculate, decode_transport, empty_usage

PROVIDERS = {'claude': 'Claude', 'agy': 'Antigravity', 'copilot': 'GitHub Copilot', 'cursor': 'Cursor', 'codex': 'Codex'}


def executable(agent):
    names = ['cursor-agent', 'agent'] if agent == 'cursor' else [agent]
    return next((shutil.which(n) for n in names if shutil.which(n)), None)


def validate_model(model):
    if not isinstance(model, str) or len(model) > 150 or (model and (model.startswith('-') or not re.fullmatch(r'[\w./:@+\[\],= -]+', model))):
        raise ValueError('모델 ID 형식이 올바르지 않습니다.')
    return model.strip()


def command(agent, model, prompt):
    if agent not in PROVIDERS:
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
    return (args + ['-'], prompt) if agent == 'codex' else (args + ['-p', prompt], '')


class CLI:
    def __init__(self, timeout=None, max_output=1024 * 1024, rates=None):
        self.timeout, self.max_output = timeout, max_output
        self.rates = rates or (lambda: {})

    def effective_timeout(self, agent, phase=None):
        if phase not in (None, 'ontology'):
            raise ValueError('알 수 없는 CLI 호출 단계')
        return self.timeout if self.timeout is not None else (600 if phase == 'ontology' or agent == 'agy' else 180)

    def complete(self, agent, model, prompt, folder, *, phase=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        args, stdin = command(agent, model, prompt)
        usage_path = folder / 'provider-usage.json'
        if agent == 'copilot':
            args[1:1] = ['--usage-output-file', str(usage_path.resolve())]
        usage = empty_usage(agent, model, self.rates().get(agent))
        timeout = self.effective_timeout(agent, phase)
        meta = {'agent': agent, 'model': model, 'phase': phase, 'prompt_sha256': digest(prompt.encode()), 'started_at': now(), 'status': 'running', 'timeout_seconds': timeout}
        def record(status, **fields):
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
        record('running')
        proc = None
        captured = False
        try:
            args[0] = executable(agent)
            if not args[0]:
                raise ValueError(f'{PROVIDERS[agent]} CLI가 설치되지 않았습니다.')
            work = folder / 'work'
            work.mkdir(exist_ok=True)
            with (folder / 'stdout.txt').open('wb') as out, (folder / 'stderr.txt').open('wb') as err, (folder / 'stdin.txt').open('wb+') as inp:
                inp.write(stdin.encode()); inp.seek(0)
                proc = subprocess.Popen(args, stdin=inp, stdout=out, stderr=err, cwd=work, start_new_session=True)
                usage['status'] = 'running'
                atomic_json(folder / 'usage.json', usage)
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f'CLI 실행 시간 제한 초과 ({timeout}초)')
                    if out.tell() + err.tell() > self.max_output or (usage_path.exists() and usage_path.stat().st_size > MAX_METADATA):
                        raise ValueError('CLI 응답 크기 제한 초과')
                    time.sleep(.05)
                if out.tell() + err.tell() > self.max_output or (usage_path.exists() and usage_path.stat().st_size > MAX_METADATA):
                    raise ValueError('CLI 응답 크기 제한 초과')
            response, provider_error = capture()
            captured = True
            if proc.returncode:
                raise ValueError(f'CLI 종료 코드 {proc.returncode}. 실행 로그의 stderr를 확인하세요.')
            if provider_error or response is None:
                raise ValueError(provider_error or 'CLI의 최종 응답을 읽지 못했습니다.')
            response = response.strip()
            response = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', response)
            fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', response, re.S)
            if fence:
                response = fence[1]
            result = json.loads(response)
            usage['status'] = 'success'
            record('success', returncode=proc.returncode)
            return result
        except Exception as exc:
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            if not captured:
                try:
                    capture()
                except Exception:
                    usage['warnings'].append('실패한 호출의 사용량을 복구하지 못했습니다.')
            usage['status'] = 'error' if proc else 'not_started'
            if not proc or proc.returncode:
                usage['complete'] = False
            try:
                record('error', error=str(exc))
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
                    record(meta['status'], usage_warning='사용량 저장에 실패해 이 호출의 사용량 기록이 불완전합니다.')
                except OSError:
                    pass
