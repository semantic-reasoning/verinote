"""Optional monotonic observations of one CLI call; no prompt or usage copy."""
import json
import math
import re
import time
from pathlib import Path
from uuid import uuid4

from .usage import AGENTS, safe_file

MAX_TIMING_BYTES = 8192
POLL_SECONDS = .05
MARKS = ('spawn_requested', 'spawn_returned', 'first_stdout', 'exit_observed',
         'interpret_started', 'interpret_completed', 'finished')
PERSISTENT_MARKS = (*MARKS, 'worker_acquired', 'reset_completed', 'request_sent', 'response_received')
ERRORS = ('preparation', 'executable_missing', 'spawn', 'exit_nonzero', 'timeout', 'output_limit', 'interpretation', 'process', 'recording')


class CallTiming:
    """Methods fail closed for telemetry and never replace a primary outcome."""
    def __init__(self, agent, model, phase, timeout, max_output, *, clock=None):
        self.call_id = uuid4().hex
        self.clock = clock or time.monotonic_ns
        self.disabled = False
        self.value = {'schema_version': 1, 'cli_call_id': self.call_id, 'agent': agent, 'model': model,
                      'phase': phase, 'cli_version': None, 'cli_version_source': 'unknown',
                      'settings': {'timeout_seconds': timeout, 'max_output_bytes': max_output,
                                   'stdout_poll_seconds': POLL_SECONDS},
                      'status': 'not_started', 'error_code': 'preparation', 'exit_code': None,
                      'offsets_seconds': dict.fromkeys(MARKS)}
        self.last_ns = None
        try:
            self.start_ns = self.clock()
            if type(self.start_ns) is not int:
                raise ValueError('clock')
            self.last_ns = self.start_ns
        except Exception:
            self.disabled = True

    def mark(self, name):
        try:
            if self.disabled or self.value['offsets_seconds'][name] is not None:
                return
            observed = self.clock()
            if type(observed) is not int or self.last_ns is None or observed < self.last_ns:
                raise ValueError('clock')
            self.last_ns = observed
            self.value['offsets_seconds'][name] = (observed - self.start_ns) / 1e9
        except Exception:
            self.disabled = True

    def persistent(self):
        self.value.update(schema_version=2, process_reused=None)
        self.value['offsets_seconds'] = dict.fromkeys(PERSISTENT_MARKS)

    def reused(self, value):
        self.value['process_reused'] = value

    def is_reused(self):
        return self.value.get('process_reused')

    def observe_stdout(self, path):
        try:
            if self.disabled or self.value['offsets_seconds']['first_stdout'] is not None:
                return
            if Path(path).stat().st_size:
                self.mark('first_stdout')
        except Exception:
            self.disabled = True

    def outcome(self, status, error_code=None, exit_code=None):
        try:
            self.value.update(status=status, error_code=error_code, exit_code=exit_code)
        except Exception:
            self.disabled = True

    def finish(self, folder):
        try:
            self.mark('finished')
            if not self.disabled:
                validate_timing(self.value)
                from .http_records import write_record
                write_record(folder, 'cli-timing.json', self.value, MAX_TIMING_BYTES)
        except Exception:
            pass


def validate_timing(value):
    keys = {'schema_version', 'cli_call_id', 'agent', 'model', 'phase', 'cli_version', 'cli_version_source',
            'settings', 'status', 'error_code', 'exit_code', 'offsets_seconds'}
    persistent = isinstance(value, dict) and value.get('schema_version') == 2
    if persistent:
        keys.add('process_reused')
    if not isinstance(value, dict) or set(value) != keys or type(value['schema_version']) is not int or value['schema_version'] not in (1, 2):
        raise ValueError('timing schema')
    if (not isinstance(value['cli_call_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', value['cli_call_id'])
            or value['agent'] not in AGENTS - {'openrouter'}
            or not isinstance(value['model'], str) or len(value['model']) > 150
            or (value['model'].startswith('-') or not re.fullmatch(r'[\w./:@+\[\],= -]*', value['model']))
            or value['phase'] not in (None, 'ontology')
            or value['cli_version'] is not None or value['cli_version_source'] != 'unknown'):
        raise ValueError('timing identity')
    settings, marks = value['settings'], value['offsets_seconds']
    if (not isinstance(settings, dict) or set(settings) != {'timeout_seconds', 'max_output_bytes', 'stdout_poll_seconds'}
            or type(settings['max_output_bytes']) is not int or settings['max_output_bytes'] <= 0
            or type(settings['timeout_seconds']) not in (int, float) or not 0 <= settings['timeout_seconds'] <= 10**9 or not math.isfinite(settings['timeout_seconds']) or type(settings['stdout_poll_seconds']) not in (int, float)
            or settings['stdout_poll_seconds'] != POLL_SECONDS
            or not isinstance(marks, dict) or set(marks) != set(PERSISTENT_MARKS if persistent else MARKS)):
        raise ValueError('timing settings')
    for number in marks.values():
        if number is not None and (type(number) not in (int, float) or not 0 <= number <= 10**9 or not math.isfinite(number)):
            raise ValueError('timing offsets')
    if marks['finished'] is None or any(n is not None and n > marks['finished'] for n in marks.values()):
        raise ValueError('timing end')
    if persistent:
        return _validate_persistent(value)
    chain = [marks[k] for k in ('spawn_requested', 'spawn_returned', 'exit_observed', 'interpret_started', 'interpret_completed')]
    if any(chain[i] is None and any(n is not None for n in chain[i + 1:]) for i in range(len(chain))):
        raise ValueError('timing missing predecessor')
    known = [n for n in chain if n is not None]
    if known != sorted(known) or (marks['first_stdout'] is not None and
                                 (marks['spawn_returned'] is None or marks['first_stdout'] < marks['spawn_returned'])):
        raise ValueError('timing order')
    status, code = value['status'], value['error_code']
    if status not in ('success', 'not_started', 'error', 'timeout') or code not in (None, *ERRORS):
        raise ValueError('timing result')
    if value['exit_code'] is not None and type(value['exit_code']) is not int:
        raise ValueError('timing exit')
    if status == 'success':
        if code is not None or value['exit_code'] != 0 or marks['interpret_completed'] is None:
            raise ValueError('timing success')
    elif code is None or (status == 'timeout') != (code == 'timeout'):
        raise ValueError('timing failure')
    if status == 'not_started' and (code not in ('preparation', 'executable_missing', 'spawn')
                                    or marks['spawn_returned'] is not None or value['exit_code'] is not None):
        raise ValueError('timing prestart')
    if code == 'spawn' and marks['spawn_requested'] is None:
        raise ValueError('timing spawn failure')
    if code == 'executable_missing' and marks['spawn_requested'] is not None:
        raise ValueError('timing executable failure')
    if code == 'exit_nonzero' and value['exit_code'] == 0:
        raise ValueError('timing exit failure')
    if status in ('error', 'timeout') and (code in ('preparation', 'executable_missing', 'spawn')
                                         or marks['exit_observed'] is None or value['exit_code'] is None):
        raise ValueError('timing process end')
    return value


def _validate_persistent(value):
    marks = value['offsets_seconds']
    reused = value['process_reused']
    if value['agent'] != 'claude' or (reused is not None and type(reused) is not bool):
        raise ValueError('persistent identity')
    chain = [marks[k] for k in ('worker_acquired', 'reset_completed', 'request_sent', 'response_received',
                               'interpret_started', 'interpret_completed')]
    known = [v for v in chain if v is not None]
    if known != sorted(known) or any(chain[i] is None and any(v is not None for v in chain[i+1:]) for i in range(len(chain))):
        raise ValueError('persistent turn order')
    if reused is True and any(marks[k] is not None for k in ('spawn_requested', 'spawn_returned')):
        raise ValueError('reused process spawned')
    if marks['spawn_returned'] is not None and (marks['spawn_requested'] is None or marks['spawn_returned'] < marks['spawn_requested']):
        raise ValueError('persistent spawn order')
    if marks['reset_completed'] is not None and (reused is None or (reused is False and marks['spawn_returned'] is None)):
        raise ValueError('persistent worker missing')
    status, error, code = value['status'], value['error_code'], value['exit_code']
    if status not in ('success', 'not_started', 'error', 'timeout') or error not in (None, *ERRORS):
        raise ValueError('persistent result')
    if code is not None and type(code) is not int:
        raise ValueError('persistent exit')
    if (code is None) != (marks['exit_observed'] is None):
        raise ValueError('persistent exit observation')
    if status == 'success':
        if error is not None or code is not None or marks['interpret_completed'] is None:
            raise ValueError('persistent success')
    elif error is None or (status == 'timeout') != (error == 'timeout'):
        raise ValueError('persistent failure')
    return value


def read_cli_timing(root, relative_folder):
    """Read only a bounded record linked to the same run and usage call ID."""
    unavailable = {'available': False, 'reason': 'CLI 호출 측정 기록을 확인할 수 없습니다.'}
    try:
        values = []
        for name, limit in (('cli-timing.json', MAX_TIMING_BYTES), ('run.json', 256*1024), ('usage.json', 256*1024)):
            raw = safe_file(root, f'{relative_folder}/{name}', limit)
            values.append(json.loads(raw) if raw else None)
        value, run, usage = values
        value = validate_timing(value)
        if (not isinstance(run, dict) or not isinstance(usage, dict)
                or run.get('cli_call_id') != value['cli_call_id'] or usage.get('cli_call_id') != value['cli_call_id']
                or run.get('agent') != value['agent'] or usage.get('agent') != value['agent']
                or run.get('model') != value['model'] or usage.get('requested_model') != value['model']
                or run.get('phase') != value['phase']
                or type(run.get('timeout_seconds')) not in (int, float)
                or run['timeout_seconds'] != value['settings']['timeout_seconds']
                or run.get('status') != ('success' if value['status'] == 'success' else 'error')
                or usage.get('status') != ('error' if value['status'] in ('error', 'timeout') else value['status'])
                or type(usage.get('schema_version')) is not int or usage['schema_version'] != 1):
            return unavailable
        marks = value['offsets_seconds']
        def elapsed(start, end):
            return marks[end] - marks[start] if marks[start] is not None and marks[end] is not None else None
        durations = {
            'spawn': elapsed('spawn_requested', 'spawn_returned'),
            'process_observed': elapsed('spawn_returned', 'exit_observed'),
            'interpret': elapsed('interpret_started', 'interpret_completed'), 'total': marks['finished']}
        if value['schema_version'] == 2:
            if run.get('transport_mode') != 'persistent' or run.get('process_reused') != value['process_reused']:
                return unavailable
            durations.update(worker_wait=marks['worker_acquired'], reset=elapsed('worker_acquired', 'reset_completed'),
                             response_wait=elapsed('request_sent', 'response_received'))
        return {'available': True, **value, 'durations_seconds': durations}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return unavailable


class _UnavailableTiming:
    call_id = None

    def mark(self, name): pass
    def observe_stdout(self, path): pass
    def outcome(self, status, error_code=None, exit_code=None): pass
    def finish(self, folder): pass
    def persistent(self): pass
    def reused(self, value): pass
    def is_reused(self): return None


def start_timing(agent, model, phase, timeout, max_output):
    try:
        return CallTiming(agent, model, phase, timeout, max_output)
    except Exception:
        return _UnavailableTiming()
