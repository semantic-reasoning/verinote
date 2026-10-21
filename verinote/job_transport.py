"""Capture a dispatcher once per registered HTTP job; no mutable defaults reads."""
import copy
from dataclasses import asdict

from .transports import ExecutionOptions, job_options


class JobTransport:
    def __init__(self, dispatcher, options, *, rates=None):
        from .agents import CLI
        self.options = job_options(options)
        rates = copy.deepcopy(dispatcher.rates() if rates is None else rates)
        self.base = CLI(max_output=dispatcher.max_output, rates=lambda: copy.deepcopy(rates),
                        http_adapter=dispatcher.http_adapter, credentials=dispatcher._credentials,
                        sink=dispatcher.sink)

    def effective_timeout(self, agent, phase=None):
        if agent != 'openrouter' or phase not in (None, 'ontology'):
            raise ValueError('고정 HTTP 작업의 제공자 또는 단계 오류')
        return self.options['timeouts']['ontology' if phase == 'ontology' else 'default']

    def complete(self, agent, model, prompt, folder, *, phase=None):
        from .agents import CLI
        options = ExecutionOptions(self.options['adapter_contract_version'], self.options['max_tokens'],
                                   self.effective_timeout(agent, phase), self.options.get('reasoning_max_tokens'),
                                   self.options.get('answer_reserve_tokens'))
        call = CLI(max_output=self.base.max_output, rates=self.base.rates, http_adapter=self.base.http_adapter,
                   credentials=self.base._credentials, sink=self.base.sink, options=options)
        return call.complete(agent, model, prompt, folder, phase=phase)

    def limited_timeout(self, agent, seconds):
        timeout = min(self.effective_timeout(agent), seconds)
        ExecutionOptions(timeout=timeout)
        limited = copy.copy(self)
        limited.options = copy.deepcopy(self.options)
        limited.options['timeouts']['default'] = timeout
        return limited

    def update_caller_validation(self, folder, call_id, status):
        return self.base.update_caller_validation(folder, call_id, status)


def capture_options(dispatcher):
    normal = asdict(dispatcher.execution_options('openrouter'))
    value = {'adapter_contract_version': normal['adapter_contract_version'],
             'max_tokens': normal['max_tokens'],
             'timeouts': {'default': normal['timeout'],
                          'ontology': dispatcher.effective_timeout('openrouter', 'ontology')}}
    if normal['adapter_contract_version'] == 2:
        value.update(reasoning_max_tokens=normal['reasoning_max_tokens'], answer_reserve_tokens=normal['answer_reserve_tokens'])
    return job_options(value)
