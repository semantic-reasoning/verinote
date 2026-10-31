"""Explicit, frozen Claude extraction ownership; ordinary CLI paths stay unchanged."""
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from .agents import CLI, executable, validate_model
from .claude_worker import ExtractionPool
from .transports import ExecutionOptions
from .usage import validate_rates


@dataclass(frozen=True)
class ExtractionSpec:
    binary: str
    model: str
    capacity: int
    timeout: float | None
    max_output: int
    rate_json: str

    @classmethod
    def capture(cls, cli, model, capacity=2):
        if not isinstance(cli, CLI): raise ValueError('추출 worker에는 CLI 설정이 필요합니다.')
        if type(capacity) is not int or capacity not in (1, 2): raise ValueError('추출 worker 수는 1 또는 2여야 합니다.')
        model = validate_model(model, 'claude')
        binary = executable('claude')
        if not binary: raise ValueError('Claude CLI가 설치되지 않았습니다.')
        if type(cli.max_output) is not int or cli.max_output <= 0: raise ValueError('CLI 출력 한도 오류')
        ExecutionOptions(timeout=cli.effective_timeout('claude'))
        rate = copy.deepcopy(cli.rates().get('claude'))
        spec = cls(str(Path(binary).resolve()), model, capacity, cli.timeout, cli.max_output,
                   json.dumps(rate, sort_keys=True, allow_nan=False))
        return cls.from_dict(spec.to_dict())


    def to_dict(self):
        return {'binary': self.binary, 'model': self.model, 'capacity': self.capacity, 'timeout': self.timeout,
                'max_output': self.max_output, 'rate': json.loads(self.rate_json)}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {'binary', 'model', 'capacity', 'timeout', 'max_output', 'rate'}
                or not isinstance(value['binary'], str) or not 1 <= len(value['binary']) <= 4096
                or any(ord(c) < 32 for c in value['binary']) or not Path(value['binary']).is_absolute()
                or type(value['capacity']) is not int or value['capacity'] not in (1, 2)
                or type(value['max_output']) is not int or value['max_output'] <= 0):
            raise ValueError('추출 transport 계약 오류')
        model = validate_model(value['model'], 'claude')
        if value['timeout'] is not None: ExecutionOptions(timeout=value['timeout'])
        rate = copy.deepcopy(value['rate'])
        if rate is not None: rate = validate_rates({'claude': rate})['claude']
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 8192:
            raise ValueError('추출 transport 계약 크기 제한')
        return cls(value['binary'], model, value['capacity'], value['timeout'], value['max_output'],
                   json.dumps(rate, sort_keys=True, allow_nan=False))


class ExtractionTransport:
    """Owner closes a dedicated pool; copies and timeout views never own it."""
    @classmethod
    def from_cli(cls, cli, model, *, capacity=2):
        return cls(ExtractionSpec.capture(cli, model, capacity))

    def __init__(self, spec):
        self._spec = spec
        self._timeout = spec.timeout
        self._owns_pool = True
        self._delegate = CLI(timeout=spec.timeout, max_output=spec.max_output, persistent=False,
                             rates=lambda: {'claude': json.loads(spec.rate_json)})
        self._pool = ExtractionPool(spec.binary, spec.model, spec.capacity)
        self._delegate._claude = self._pool
        self._delegate._pinned_claude_binary = spec.binary

    @property
    def identity(self): return self._spec

    @property
    def closed(self): return self._pool.closed.is_set()

    def view(self):
        view = object.__new__(type(self))
        view._spec, view._timeout = self._spec, self._timeout
        view._delegate, view._pool, view._owns_pool = self._delegate, self._pool, False
        return view

    def __copy__(self): return self.view()

    def _check(self, agent, model=None, phase=None):
        if agent != 'claude' or phase is not None or (model is not None and model != self._spec.model):
            raise ValueError('고정 Claude 추출 호출 조건이 다릅니다.')
        if self.closed: raise ValueError('추출 worker가 닫혔습니다.')

    def ready(self, agent): return agent == 'claude' and not self.closed

    def effective_timeout(self, agent, phase=None):
        self._check(agent, phase=phase)
        return 180 if self._timeout is None else self._timeout

    def execution_options(self, agent, phase=None):
        return ExecutionOptions(timeout=self.effective_timeout(agent, phase))

    def limited_timeout(self, agent, seconds):
        self._check(agent)
        ExecutionOptions(timeout=seconds)
        view = self.view()
        # Preserve CLI's unspecified-cap behavior on the first timeout view.
        view._timeout = seconds if self._timeout is None else min(self._timeout, seconds)
        return view

    def complete(self, agent, model, prompt, folder, *, phase=None):
        self._check(agent, model, phase)
        delegate = self._delegate.limited_timeout(agent, self.effective_timeout(agent))
        return delegate.complete(agent, model, prompt, folder, phase=phase)

    def update_caller_validation(self, folder, call_id, status):
        return self._delegate.update_caller_validation(folder, call_id, status)

    def retire_if_idle(self):
        if not self._owns_pool: raise ValueError('추출 owner만 교체할 수 있습니다.')
        self._pool.retire_if_idle()

    def close(self):
        if self._owns_pool: self._pool.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
