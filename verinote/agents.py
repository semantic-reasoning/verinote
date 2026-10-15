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
        'claude': ['--output-format', 'text', '--tools', '', '--strict-mcp-config'],
        'agy': ['--output-format', 'text', '--mode', 'plan', '--sandbox', '--disable-slash-commands'],
        'copilot': ['--silent', '--available-tools', '', '--disable-builtin-mcps'],
        'cursor': ['--output-format', 'text', '--mode', 'ask', '--sandbox', 'enabled', '--trust'],
        'codex': ['exec', '--skip-git-repo-check', '--ephemeral', '--sandbox', 'read-only', '--color', 'never'],
    }[agent]
    args = [binary, *flags, *(['--model', model] if model else [])]
    return (args + ['-'], prompt) if agent == 'codex' else (args + ['-p', prompt], '')


class CLI:
    def __init__(self, timeout=None, max_output=1024 * 1024):
        self.timeout, self.max_output = timeout, max_output

    def complete(self, agent, model, prompt, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        args, stdin = command(agent, model, prompt)
        timeout = self.timeout if self.timeout is not None else (600 if agent == 'agy' else 180)
        meta = {'agent': agent, 'model': model, 'prompt_sha256': digest(prompt.encode()), 'started_at': now(), 'status': 'running', 'timeout_seconds': timeout}
        def record(status, **fields):
            meta.update(status=status, **fields)
            (folder / 'run.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        record('running')
        proc = None
        try:
            args[0] = executable(agent)
            if not args[0]:
                raise ValueError(f'{PROVIDERS[agent]} CLI가 설치되지 않았습니다.')
            work = folder / 'work'
            work.mkdir(exist_ok=True)
            with (folder / 'stdout.txt').open('wb') as out, (folder / 'stderr.txt').open('wb') as err, (folder / 'stdin.txt').open('wb+') as inp:
                inp.write(stdin.encode()); inp.seek(0)
                proc = subprocess.Popen(args, stdin=inp, stdout=out, stderr=err, cwd=work, start_new_session=True)
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f'CLI 실행 시간 제한 초과 ({timeout}초)')
                    if out.tell() + err.tell() > self.max_output:
                        raise ValueError('CLI 응답 크기 제한 초과')
                    time.sleep(.05)
                if out.tell() + err.tell() > self.max_output:
                    raise ValueError('CLI 응답 크기 제한 초과')
            if proc.returncode:
                raise ValueError(f'CLI 종료 코드 {proc.returncode}. 실행 로그의 stderr를 확인하세요.')
            response = (folder / 'stdout.txt').read_text(encoding='utf-8').strip()
            response = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', response)
            fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', response, re.S)
            if fence:
                response = fence[1]
            result = json.loads(response)
            record('success', returncode=proc.returncode)
            return result
        except Exception as exc:
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            record('error', error=str(exc))
            raise
