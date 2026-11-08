"""Opt-in two-process Copilot extraction; no shared Claude worker/session state."""
import copy
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .agents import CLI, executable, validate_model
from .transports import ExecutionOptions
from .usage import validate_rates


@dataclass(frozen=True)
class CopilotExtractionSpec:
    binary: str
    model: str
    capacity: int
    timeout: float | None
    max_output: int
    rate_json: str

    @classmethod
    def capture(cls, cli, model, capacity=2):
        if not isinstance(cli, CLI): raise ValueError('Copilot 추출에는 CLI 설정이 필요합니다.')
        binary = executable('copilot')
        if not binary: raise ValueError('GitHub Copilot CLI가 설치되지 않았습니다.')
        return cls.from_dict({'agent': 'copilot', 'binary': str(Path(binary).resolve()),
                              'model': model, 'capacity': capacity, 'timeout': cli.timeout,
                              'max_output': cli.max_output, 'rate': copy.deepcopy(cli.rates().get('copilot')),
                              'no_auto_update': True})

    def to_dict(self):
        return {'agent': 'copilot', 'binary': self.binary, 'model': self.model, 'capacity': self.capacity,
                'timeout': self.timeout, 'max_output': self.max_output, 'rate': json.loads(self.rate_json),
                'no_auto_update': True}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {'agent', 'binary', 'model', 'capacity', 'timeout', 'max_output', 'rate', 'no_auto_update'}
                or value['agent'] != 'copilot' or value['no_auto_update'] is not True
                or not isinstance(value['binary'], str) or not 1 <= len(value['binary']) <= 4096
                or any(ord(c) < 32 for c in value['binary']) or not Path(value['binary']).is_absolute()
                or type(value['capacity']) is not int or value['capacity'] != 2
                or type(value['max_output']) is not int or value['max_output'] <= 0):
            raise ValueError('Copilot 추출 transport 계약 오류')
        model = validate_model(value['model'], 'copilot')
        if value['timeout'] is not None: ExecutionOptions(timeout=value['timeout'])
        rate = copy.deepcopy(value['rate'])
        if rate is not None: rate = validate_rates({'copilot': rate})['copilot']
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 8192:
            raise ValueError('Copilot 추출 transport 계약 크기 제한')
        return cls(value['binary'], model, 2, value['timeout'], value['max_output'],
                   json.dumps(rate, sort_keys=True, allow_nan=False))


class _State:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.active = 0


class CopilotExtractionTransport:
    def __init__(self, spec):
        self._spec = CopilotExtractionSpec.from_dict(spec.to_dict())
        self._timeout = spec.timeout
        self._owns_state = True
        self._state = _State()
        self._delegate = CLI(timeout=spec.timeout, max_output=spec.max_output, persistent=False,
                             rates=lambda: {'copilot': json.loads(spec.rate_json)})
        self._delegate._pinned_copilot_binary = spec.binary

    @property
    def identity(self): return self._spec

    @property
    def closed(self):
        with self._state.condition: return self._state.closed

    def view(self):
        view = object.__new__(type(self))
        view._spec, view._timeout = self._spec, self._timeout
        view._state, view._delegate, view._owns_state = self._state, self._delegate, False
        return view

    def __copy__(self): return self.view()

    def _check(self, agent, model=None, phase=None):
        if agent != 'copilot' or phase is not None or (model is not None and model != self._spec.model):
            raise ValueError('고정 Copilot 추출 호출 조건이 다릅니다.')
        if self.closed: raise ValueError('Copilot 추출 transport가 닫혔습니다.')

    def ready(self, agent): return agent == 'copilot' and not self.closed

    def effective_timeout(self, agent, phase=None):
        self._check(agent, phase=phase)
        return 600 if self._timeout is None else self._timeout

    def execution_options(self, agent, phase=None):
        return ExecutionOptions(timeout=self.effective_timeout(agent, phase))

    def limited_timeout(self, agent, seconds):
        self._check(agent)
        ExecutionOptions(timeout=seconds)
        view = self.view()
        view._timeout = seconds if self._timeout is None else min(self._timeout, seconds)
        return view

    def complete(self, agent, model, prompt, folder, *, phase=None):
        self._check(agent, model, phase)
        deadline = time.monotonic() + self.effective_timeout(agent)
        state = self._state
        with state.condition:
            while True:
                if state.closed: raise ValueError('Copilot 추출 transport가 닫혔습니다.')
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('Copilot 추출 호출 대기 시간 제한 초과')
                if state.active < self._spec.capacity: break
                state.condition.wait(remaining)
            state.active += 1
        try:
            return self._delegate.limited_timeout(agent, remaining).complete(agent, model, prompt, folder, phase=phase)
        finally:
            with state.condition:
                state.active -= 1
                state.condition.notify_all()

    def update_caller_validation(self, folder, call_id, status):
        return self._delegate.update_caller_validation(folder, call_id, status)

    def retire_if_idle(self):
        if not self._owns_state: raise ValueError('추출 owner만 교체할 수 있습니다.')
        with self._state.condition:
            if self._state.active: raise ValueError('Copilot 추출 호출이 진행 중입니다.')
            self._state.closed = True
            self._state.condition.notify_all()

    def close(self):
        if self._owns_state:
            with self._state.condition:
                self._state.closed = True
                self._state.condition.notify_all()
                while self._state.active: self._state.condition.wait()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
