"""Isolated, budgeted measurements through the production extraction Service."""
import copy
import hashlib
import json
import math
import re
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agents import CLI, CLI_PROVIDERS, validate_model
from .cli_timing import read_cli_timing
from .documents import chunks
from .extraction_checkpoint import CHECKPOINT_LIMIT, ExtractionCheckpoint
from .extraction_evaluation import (
    DATA_ROOT,
    _read,
    evaluate_case,
    ideal_outputs,
    load_case,
    load_manifest,
)
from .extraction_timing import read_extraction_timing
from .ontology import (
    EXTRACTION_CONTRACT_VERSION,
    EXTRACTION_PROMPT,
    QUOTE_REPAIR_PROMPT,
    extraction_prompts,
)
from .service import Service
from .store import Store, digest
from .usage import TOKEN_KEYS, amount, atomic_json, empty_usage, number

MAX_REPORT_BYTES = 8 * 1024 * 1024


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Limits:
    requests: int = 40
    repetitions: int = 2
    seconds: float = 120
    tokens: int = 100000
    missing_usage: str = 'stop'

    def __post_init__(self):
        for value, upper in ((self.requests, 1000), (self.repetitions, 100), (self.tokens, 10**12)):
            if type(value) is not int or not 1 <= value <= upper: raise ValueError('측정 한도 오류')
        if type(self.seconds) not in (int, float) or not math.isfinite(self.seconds) or not 0 < self.seconds <= 86400:
            raise ValueError('전체 시간 한도 오류')
        if self.missing_usage not in ('stop', 'continue'): raise ValueError('사용량 누락 정책 오류')


class Stopped(RuntimeError): pass


class Budget:
    """One ledger: capacity rejection does not revoke a previously reserved slot."""
    def __init__(self, limits, *, clock=None):
        self.limits, self.clock = limits, clock or time.monotonic
        self.started = self.clock(); self.deadline = self.started + limits.seconds
        self.lock = threading.RLock()
        self._stop_reason = None; self.capacity_exhausted = False
        self.attempts = []; self.observed_tokens = 0; self.unknown_usage = 0
        self._states = {}

    @property
    def reason(self):
        with self.lock:
            return self._stop_reason or ('request_limit' if self.capacity_exhausted else None)

    def stop(self, reason):
        with self.lock:
            if self._stop_reason is None: self._stop_reason = reason

    def remaining(self): return max(0, self.deadline-self.clock())

    def _check_terminal(self):
        if self.remaining() <= 0: self.stop('time_limit')
        if self.observed_tokens >= self.limits.tokens: self.stop('token_limit')
        if self._stop_reason: raise Stopped(self._stop_reason)

    def check(self):
        with self.lock:
            self._check_terminal()
            if len(self.attempts) >= self.limits.requests:
                self.capacity_exhausted = True
                raise Stopped('request_limit')

    def reserve(self, chunk, repair, *, identity=None, purpose='measurement'):
        identity = dict(identity or {})
        if (set(identity) - {'owner_id', 'case_id', 'repetition', 'document_key'}
                or any(type(v) not in (int, str) for v in identity.values())
                or purpose not in ('measurement', 'warmup')
                or type(chunk) is not int or chunk < 1 or type(repair) is not bool):
            raise ValueError('attempt metadata')
        if 'document_key' in identity and (not isinstance(identity['document_key'], str) or not re.fullmatch(r'[a-z0-9_-]{1,80}', identity['document_key'])):
            raise ValueError('attempt document key')
        with self.lock:
            self.check()
            row = {'attempt': len(self.attempts)+1, 'chunk': chunk, 'repair': repair, 'status': 'running',
                   'purpose': purpose, 'request_kind': 'repair' if repair else ('warmup' if purpose == 'warmup' else 'initial'),
                   'admission_state': 'reserved', 'dispatch_authorized': False, 'blocked_reason': None,
                   'worker_scope': None, 'worker_identity': None, 'resolved_model': None, 'model_identity_source': 'unknown',
                   'tokens': dict.fromkeys(TOKEN_KEYS), 'usage_complete': False, 'reported_usd': None,
                   'estimated_usd': None, 'reported_credits': None, 'estimated_credits': None,
                   'credit_unit_sha256': None, 'estimated_credit_unit_sha256': None, 'cli_call_id': None, 'cli_timing': None,
                   **identity}
            self.attempts.append(row)
            self._states[id(row)] = 'reserved'
            return row

    def _require(self, row, state):
        if (not any(item is row for item in self.attempts) or self._states.get(id(row)) != state):
            raise ValueError('attempt ownership/state')

    def authorize_dispatch(self, row):
        """Linearization of adapter admission, not evidence of provider transmission."""
        with self.lock:
            self._require(row, 'reserved')
            self._check_terminal()
            self._states[id(row)] = 'authorized'
            row.update(admission_state='authorized', dispatch_authorized=True)

    def block(self, row, *, seconds=0):
        with self.lock:
            self._require(row, 'reserved')
            self._states[id(row)] = 'blocked'
            row.update(admission_state='blocked', status='blocked', wrapper_seconds=seconds,
                       blocked_reason=self._stop_reason or 'adapter_setup_failed', process_observation='unknown')

    def finish(self, row, usage, *, status='success', seconds=0, observation='unknown'):
        with self.lock:
            self._require(row, 'authorized')
            if status not in ('success', 'error'): raise ValueError('attempt status')
            # Only the same numeric export contract can modify usage fields.
            fields = {'tokens', 'usage_complete', 'reported_usd', 'estimated_usd', 'reported_credits',
                      'estimated_credits', 'credit_unit_sha256', 'estimated_credit_unit_sha256', 'cli_call_id', 'cli_timing',
                      'worker_scope', 'worker_identity', 'resolved_model', 'model_identity_source'}
            if set(usage) - fields: raise ValueError('attempt usage fields')
            data = copy.deepcopy(usage)
            if (not isinstance(data.get('tokens'), dict) or set(data['tokens']) != set(TOKEN_KEYS)
                    or any(n is not None and number(n, True) is None for n in data['tokens'].values())
                    or type(data.get('usage_complete')) is not bool):
                raise ValueError('attempt usage values')
            if data.get('resolved_model') is not None and resolved_model(data['resolved_model']) is None:
                raise ValueError('attempt model identity')
            if data.get('model_identity_source', 'unknown') not in ('unknown', 'claude_modelUsage'):
                raise ValueError('attempt model source')
            counts = [data['tokens'][k] for k in ('input', 'output')]
            known = data['usage_complete'] and all(n is not None for n in counts)
            row.update(data)
            row.update(admission_state='finished', status=status, wrapper_seconds=seconds, process_observation=observation)
            self._states[id(row)] = 'finished'
            self.observed_tokens += sum(n for n in counts if n is not None)
            if not known:
                self.unknown_usage += 1
                if self.limits.missing_usage == 'stop': self.stop('usage_unknown')
            if self.observed_tokens >= self.limits.tokens: self.stop('token_limit')
            if self.remaining() <= 0: self.stop('time_limit')

    def summary(self):
        with self.lock:
            states = list(self._states.values())
            pending = sum(state in ('reserved', 'authorized') for state in states)
            return {'requests_started': len(self.attempts), 'dispatch_authorized': sum(state in ('authorized', 'finished') for state in states),
                    'blocked_before_dispatch': states.count('blocked'), 'finished': states.count('finished'),
                    'pending': pending, 'observed_tokens_lower_bound': self.observed_tokens,
                    'unknown_usage_calls': self.unknown_usage, 'token_usage_verified': self.unknown_usage == 0 and pending == 0,
                    'token_hard_cap_guaranteed': False, 'stop_reason': self.reason,
                    'elapsed_seconds': max(0, self.clock()-self.started)}


def resolved_model(value):
    """Allow only versioned Claude family identifiers reported by modelUsage."""
    return value if (isinstance(value, str) and len(value) <= 150
                     and not value.endswith('-latest')
                     and re.fullmatch(r'claude-(?:[0-9]+-)*(?:sonnet|opus|haiku)-[0-9]+(?:[.-][0-9a-z]+)*', value)) else None


def call_metrics(folder, agent, model):
    """Numbers and verified identifiers only; no provider text reaches exports."""
    clean: dict = {'resolved_model': None, 'model_identity_source': 'unknown', 'worker_scope': None, 'worker_identity': None, 'tokens': dict.fromkeys(TOKEN_KEYS), 'usage_complete': False, 'reported_usd': None,
             'estimated_usd': None, 'reported_credits': None, 'estimated_credits': None,
                   'credit_unit_sha256': None, 'estimated_credit_unit_sha256': None, 'cli_call_id': None, 'cli_timing': None}
    try:
        usage, meta = _read(Path(folder)/'usage.json'), _read(Path(folder)/'run.json')
        cid = usage.get('cli_call_id')
        if (not isinstance(cid, str) or len(cid) != 32 or any(c not in '0123456789abcdef' for c in cid)
                or meta.get('cli_call_id') != cid or meta.get('agent') != agent or meta.get('model') != model
                or usage.get('agent') != agent or usage.get('requested_model') != model
                or type(usage.get('schema_version')) is not int or usage['schema_version'] != 1
                or not isinstance(usage.get('tokens'), dict)): return clean
        marked = any(key in record for record in (meta, usage) for key in ('worker_scope', 'worker_identity'))
        if marked:
            clean['worker_scope'] = ('extraction_pool' if meta.get('worker_scope') == usage.get('worker_scope') == 'extraction_pool'
                                     else 'unknown')
        clean.update(tokens={k: number(usage['tokens'].get(k), True) for k in TOKEN_KEYS},
                     usage_complete=usage.get('complete') is True, cli_call_id=cid, resolved_model=resolved_model(usage.get('resolved_model')) if agent == 'claude' else None,
                     reported_usd=amount(usage.get('reported_usd')), estimated_usd=amount(usage.get('estimated_usd')))
        if clean['resolved_model'] is not None: clean['model_identity_source'] = 'claude_modelUsage'
        for provenance in ('reported', 'estimated'):
            clean[provenance+'_credits'] = amount(usage.get(provenance+'_credits'))
            key = 'credit_unit' if provenance == 'reported' else 'estimated_credit_unit'
            unit = usage.get(key)
            clean[key+'_sha256'] = sha(unit) if isinstance(unit, str) and unit else None
        if any(clean['tokens'][k] is None for k in TOKEN_KEYS[:4]): clean['usage_complete'] = False
        t = clean['tokens']
        if ((all(t[k] is not None for k in ('input', 'cache_read', 'cache_write')) and t['cache_read']+t['cache_write'] > t['input'])
                or (t['reasoning'] is not None and t['output'] is not None and t['reasoning'] > t['output'])):
            clean['usage_complete'] = False
    except (OSError, ValueError, TypeError, KeyError, AttributeError): pass
    timing = read_cli_timing(folder, '.')
    if timing.get('available') and timing['cli_call_id'] == clean['cli_call_id'] and timing['agent'] == agent and timing['model'] == model:
        clean['cli_timing'] = {k: timing[k] for k in ('schema_version', 'status', 'error_code', 'exit_code', 'offsets_seconds', 'durations_seconds', 'settings')}
        clean['cli_timing']['process_reused'] = timing.get('process_reused')
        if clean['worker_scope'] == 'extraction_pool':
            clean['worker_identity'] = _worker_identity(meta, usage)
    return clean


def _worker_identity(meta, usage):
    value = meta.get('worker_identity')
    if (meta.get('worker_scope') != 'extraction_pool' or usage.get('worker_scope') != 'extraction_pool'
            or not isinstance(value, dict) or set(value) != {'pool_id', 'slot_id', 'worker_id', 'generation'}
            or usage.get('worker_identity') != value
            or any(not isinstance(value[k], str) or not re.fullmatch(r'[a-f0-9]{32}', value[k]) for k in ('pool_id', 'worker_id'))
            or type(value['slot_id']) is not int or value['slot_id'] not in (1, 2)
            or type(value['generation']) is not int or not 1 <= value['generation'] <= 10**9):
        return None
    return dict(value)


class BudgetCaller:
    def __init__(self, caller, budget, notify=None, *, owns_caller=False, identity=None, lifecycle=None, purpose='measurement', retry_close=False):
        self.caller, self.budget, self.notify = caller, budget, notify
        self.owns_caller, self.closed = owns_caller, False
        self.retry_close = retry_close
        self.identity = dict(identity or {})
        self.purpose = purpose
        self.lifecycle = lifecycle if lifecycle is not None else {'observed': False}

    def close(self):
        if self.owns_caller and not self.closed:
            if not self.retry_close: self.closed = True
            if hasattr(self.caller, 'close'): self.caller.close()
            self.closed = True

    @property
    def extraction_spec(self): return self.caller.identity

    def limited_timeout(self, agent, seconds):
        return BudgetCaller(self.caller.limited_timeout(agent, seconds), self.budget, self.notify,
                            identity=self.identity, lifecycle=self.lifecycle, purpose=self.purpose)

    def process_observation(self, timing, worker_identity=None, worker_scope=None):
        with self.budget.lock:
            if not timing or timing['schema_version'] != 2:
                return 'unknown'
            if worker_scope is not None:
                if worker_scope != 'extraction_pool' or worker_identity is None: return 'unknown'
                # Generation is fixed at worker creation, independent of callback arrival order.
                if timing['process_reused'] is True: return 'reused'
                if timing['process_reused'] is False and timing['offsets_seconds']['spawn_returned'] is not None:
                    return 'started' if worker_identity['generation'] == 1 else 'restarted'
                return 'unknown'
            if timing['process_reused'] is True:
                self.lifecycle['observed'] = True
                return 'reused'
            if timing['process_reused'] is False and timing['offsets_seconds']['spawn_returned'] is not None:
                state = 'restarted' if self.lifecycle['observed'] else 'started'
                self.lifecycle['observed'] = True
                return state
            return 'unknown'

    def complete(self, agent, model, prompt, folder):
        folder = Path(folder); repair = folder.name == 'quote-repair'
        chunk = int(folder.parent.name if repair else folder.name)
        row = self.budget.reserve(chunk, repair, identity=self.identity, purpose=self.purpose)
        started = self.budget.clock()
        dispatched, status, primary_error = False, 'error', False
        previous = None
        try:
            previous = call_metrics(folder, agent, model)['cli_call_id']
            remaining = self.budget.remaining()
            if remaining <= 0: self.budget.stop('time_limit'); raise Stopped('time_limit')
            caller = self.caller.limited_timeout(agent, remaining)
            self.budget.authorize_dispatch(row)
            dispatched = True
            output = caller.complete(agent, model, prompt, folder)
            status = 'success'
            return output
        except BaseException:
            primary_error = True
            raise
        finally:
            if dispatched:
                metrics = call_metrics(folder, agent, model)
                if previous is not None and metrics['cli_call_id'] == previous:
                    metrics = {'tokens': dict.fromkeys(TOKEN_KEYS), 'usage_complete': False,
                               'reported_usd': None, 'estimated_usd': None, 'reported_credits': None, 'estimated_credits': None,
                               'credit_unit_sha256': None, 'estimated_credit_unit_sha256': None, 'cli_call_id': None, 'cli_timing': None}
                with self.budget.lock:
                    observation = self.process_observation(metrics['cli_timing'], metrics.get('worker_identity'), metrics.get('worker_scope'))
                    self.budget.finish(row, metrics, status=status, seconds=max(0, self.budget.clock()-started), observation=observation)
            else:
                self.budget.block(row, seconds=max(0, self.budget.clock()-started))
            if self.notify is not None:
                # Serialize callbacks/snapshots with every row update, never with model work.
                with self.budget.lock:
                    try: self.notify()
                    except Exception:
                        self.budget.stop('report_write_failed')
                        if not primary_error: raise Stopped('report_write_failed') from None


class FakeCLI(CLI):
    """Labeled synthetic oracle, exercising normal validation and repair paths."""
    def __init__(self, case, scenario):
        super().__init__()
        self.outputs = ideal_outputs(case, contract_version=EXTRACTION_CONTRACT_VERSION); self.scenario = scenario
        self.state = {'calls': 0}; self.case = case
    def complete(self, agent, model, prompt, folder, *, phase=None):
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        self.state['calls'] += 1
        repair = folder.name == 'quote-repair'; index = int(folder.parent.name if repair else folder.name)
        cid = uuid4().hex
        usage = empty_usage(agent, model)
        usage.update(cli_call_id=cid, tokens={'input': 100, 'output': 20, 'cache_read': 0, 'cache_write': 0, 'reasoning': 0},
                     complete=True, status='success')
        atomic_json(folder/'run.json', {'cli_call_id': cid, 'agent': agent, 'model': model, 'status': 'error' if self.scenario in ('failure', 'timeout') else 'success',
            'phase': None, 'timeout_seconds': self.effective_timeout(agent)})
        atomic_json(folder/'usage.json', usage)
        if self.scenario == 'failure' or self.scenario == 'timeout':
            usage['status'] = 'error'; atomic_json(folder/'usage.json', usage)
            if self.scenario == 'timeout': raise TimeoutError('synthetic timeout')
            raise ValueError('synthetic failure')
        if repair:
            result = _read(DATA_ROOT/'repair-cases'/'prefixed_quote.json')['expected_repair']
        else:
            result = copy.deepcopy(self.outputs[index-1]['output'])
            if self.scenario == 'repair' and index == 1:
                result = _read(DATA_ROOT/'repair-cases'/'prefixed_quote.json')['bad_output']
        return result


def distribution(values):
    return {'count': len(values), 'min': min(values) if values else None, 'max': max(values) if values else None,
            'mean': statistics.mean(values) if values else None, 'median': statistics.median(values) if values else None,
            'population_stddev': statistics.pstdev(values) if values else None}


def quality_report(case, root, run):
    outputs = []
    try:
        value = _read(root/'runs'/run['id']/'extraction-checkpoint.json', CHECKPOINT_LIMIT)
        contract = value['contract']; pin = run['ontology']
        prompt, repair_prompt = extraction_prompts(pin['extraction_contract'])
        expected = {'document_id': run['document_id'], 'version': run['version'], 'text_sha256': digest(case['source'].encode()),
                    'source_sha256': digest(case['source'].encode()), 'agent': run['agent'], 'model': run['model'],
                    'dictionary_payload_sha256': digest(pin['payload']), 'prompt': digest(prompt.encode()),
                    'repair_prompt': digest(repair_prompt.encode()), 'chunk_size': 8000,
                    'dictionary': {k: pin[k] for k in ('id', 'revision', 'sha256', 'extraction_contract')}}
        if run.get('extraction_options') is not None:
            from .extraction_options import validate_extraction_source
            validate_extraction_source(run['extraction_options'], case['source'])
            expected['extraction_options'] = run['extraction_options']
        if (value['run_id'] != run['id'] or value['schema_version'] != 1 or any(contract.get(k) != v for k, v in expected.items())
                or not isinstance(value['outputs'], list) or not 1 <= len(value['outputs']) <= len(list(chunks(case['source'])))):
            raise ValueError('checkpoint identity')
        validator = ExtractionCheckpoint.__new__(ExtractionCheckpoint); validator.run = run
        for index, output in enumerate(value['outputs'], 1):
            validator.validated(output, list(chunks(case['source']))[index-1][1])
            outputs.append({'index': index, 'output': output})
    except (OSError, ValueError, KeyError, TypeError, AttributeError): outputs = []
    result = evaluate_case(case, outputs)
    # No untrusted triple/quote/provider text is exported, even inside nested evaluation rows.
    result['false_positives'] = [{'triple_sha256': sha(r['triple']), 'forbidden': r['forbidden'], 'chunk': r['chunk'], 'row': r['row']} for r in result['false_positives']]
    result['duplicates'] = [{k: r[k] for k in ('chunk', 'row', 'gold_id')} | {'triple_sha256': sha(r['triple'])} for r in result['duplicates']]
    complete = run['status'] == 'success' and len(outputs) == len(list(chunks(case['source'])))
    return {'validated_chunks': len(outputs), 'total_chunks': len(list(chunks(case['source']))),
            'complete': complete, 'passed': complete and result['passed'], 'evaluation': result}


class ReportWriter:
    def __init__(self, folder):
        self.folder = Path(folder)
        if self.folder.is_symlink() or self.folder.exists(): raise ValueError('새 결과 폴더가 필요합니다.')
        parent = self.folder.parent
        if not parent.is_dir() or any(p.is_symlink() for p in (parent, *parent.parents)): raise ValueError('안전한 결과 부모 폴더가 필요합니다.')
        self.folder.mkdir(mode=0o700); self.path = self.folder/'report.json'
        self.lock = threading.RLock()
    def write(self, report):
        with self.lock:
            raw = json.dumps(report, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_REPORT_BYTES: raise ValueError('측정 결과 크기 제한')
            if self.path.is_symlink() or self.path.with_suffix('.json.tmp').is_symlink(): raise ValueError('결과 파일 종류 오류')
            atomic_json(self.path, report)


class _Executor(ThreadPoolExecutor):
    future: Any = None

    def submit(self, fn, /, *args, **kwargs):
        self.future = super().submit(fn, *args, **kwargs)
        return self.future


def execute_document(root, case, make_caller, budget, row, close, *, agent, model, extraction_concurrency=None, extraction_policy=None, registration_cli=None):
    """One fresh synthetic Store and the production Service; caller controls artifact lifetime."""
    store = Store(Path(root)/'state.sqlite3')
    service = None; run = None; caller = None
    try:
        doc = store.register('Synthetic '+case['case_id'], 'synthetic.md', case['source'].encode(), case['source'])
        seed = store.create_run(doc['id'], agent, model, 'ontology', 1, 1)
        store.run_status(seed['id'], 'running'); store.finish_run(seed['id'], dictionary=case['dictionary'])
        item = next(i for i in store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        store.review(item['id'], item['revision'], 'approved', 'Synthetic fixture', 'Versioned human-reviewed gold')
        if extraction_policy is not None:
            def supply(spec):
                nonlocal caller
                caller = make_caller(spec)
                return caller
            service = Service(store, root, registration_cli or CLI(), extraction_caller_factory=supply)
        else:
            caller = make_caller()
            service = Service(store, root, caller, extraction_caller=caller if extraction_concurrency is not None else None)
        service.pool.shutdown(wait=True)
        pool = _Executor(max_workers=1, thread_name_prefix='verinote-benchmark')
        service.pool = pool
        budget.check()
        run = service.start(doc['id'], agent, model, expected_version=1, expected_generation=1,
                            extraction_concurrency=extraction_concurrency, extraction_policy=extraction_policy)
        row.update(run_id=run['id'], document_id=doc['id'])
        if extraction_policy is not None: row['extraction_options'] = copy.deepcopy(run['extraction_options'])
        while not pool.future.done():
            if budget.remaining() <= 0: budget.stop('time_limit')
            time.sleep(.01)
        pool.future.result()
        service.pool.shutdown(wait=True)
    except BaseException:
        budget.stop('interrupted' if sys.exc_info()[0] is KeyboardInterrupt else budget.reason or 'runner_error')
        row['status'] = 'error'
        raise
    finally:
        close(service)
        close(caller)
        if run is not None:
            try:
                run = store.run(run['id'])
                if run['status'] in ('success', 'error'): row['status'] = run['status']
                row['quality'] = quality_report(case, root, run)
                timing = read_extraction_timing(root, store, run['id'])
                row['document_timing'] = ({'available': True, 'summary': timing['summary'],
                    'status': timing['status'], 'error_code': timing['error_code']} if timing['available'] else {'available': False})
            except Exception:
                budget.stop('result_read_failed')


def measure(output, *, cases=('short', 'long'), scenario='normal', mode='fake', agent='agy', model='',
            limits=None, timeout=30, caller_factory=None, clock=None, process_mode='oneshot', cli_version=None, extraction_concurrency=None, extraction_policy=None):
    """No config/user-root parameter: only versioned bundled synthetic documents."""
    limits = limits or Limits()
    if mode not in ('fake', 'cli') or agent not in CLI_PROVIDERS: raise ValueError('측정 실행 모드·에이전트 오류')
    if process_mode not in ('oneshot', 'document', 'batch'):
        raise ValueError('프로세스 수명 모드 오류')
    if process_mode != 'oneshot' and (mode != 'cli' or agent != 'claude'):
        raise ValueError('프로세스 재사용 측정은 CLI Claude만 지원합니다.')
    if process_mode == 'batch' and caller_factory is not None:
        raise ValueError('배치 재사용은 문서별 caller_factory를 지원하지 않습니다.')
    if cli_version is not None and (mode != 'cli' or not isinstance(cli_version, str)
            or not re.fullmatch(r'[0-9A-Za-z.+_-]{1,80}', cli_version)):
        raise ValueError('CLI 버전 선언 오류')
    model = validate_model(model, agent)
    if mode == 'cli' and not model: raise ValueError('실제 측정에는 명시적인 모델 ID가 필요합니다.')
    if scenario not in ('normal', 'repair', 'failure', 'timeout') or (mode == 'cli' and scenario != 'normal'): raise ValueError('측정 시나리오 오류')
    if not isinstance(cases, (tuple, list)) or not 1 <= len(cases) <= 5 or len(set(cases)) != len(cases): raise ValueError('측정 자료 목록 오류')
    fixtures = [load_case(k) for k in cases]
    if scenario == 'repair' and cases != ('short',) and cases != ['short']: raise ValueError('보정 예제는 short만 지원합니다.')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 360: raise ValueError('호출 시간 한도 오류')
    if extraction_concurrency is not None:
        if (type(extraction_concurrency) is not int or extraction_concurrency not in (1, 2)
                or mode != 'cli' or agent != 'claude' or process_mode not in ('document', 'batch')
                or caller_factory is not None):
            raise ValueError('병렬 추출 측정은 명시적 Claude CLI document/batch 모드만 지원합니다.')
    if extraction_policy is not None:
        from .extraction_options import CHUNK_THRESHOLD_POLICY
        if (extraction_policy != CHUNK_THRESHOLD_POLICY or extraction_concurrency is not None
                or mode != 'cli' or agent != 'claude' or process_mode not in ('document', 'batch') or caller_factory is not None):
            raise ValueError('추출 정책은 명시적 Claude CLI document/batch 모드에서 동시성 옵션 없이 지원합니다.')
    writer = ReportWriter(output); budget = Budget(limits, clock=clock)
    try: package_version = version('verinote')
    except PackageNotFoundError: package_version = None
    report = {'schema_version': 3, 'mode': mode, 'synthetic_responses': mode == 'fake', 'status': 'running',
              'stop_reason': None, 'conditions': {'agent': agent, 'requested_model': model, 'scenario': scenario,
              'package_version': package_version, 'cli_version': cli_version,
              'cli_version_source': 'operator_supplied_unverified' if cli_version else 'unknown_not_probed',
              'extraction_concurrency': extraction_concurrency, 'extraction_policy': extraction_policy,
              'process_mode': process_mode, 'owner_scope': 'batch' if process_mode == 'batch' else 'document',
              'provider_cache_state': 'unknown', 'warmup': 'none',
              'row_seconds_includes_transport_close': process_mode != 'batch',
              'row_seconds_includes_previous_owner_close': extraction_policy is not None and process_mode == 'batch',
              'effective_timeout_seconds': timeout, 'max_output_bytes': 1024*1024, 'chunk_size': 8000,
              'input_reuse': 'fresh_store_per_repetition', 'limits': vars(limits),
              'dataset_version': 1, 'evaluator_version': 1, 'extraction_contract': EXTRACTION_CONTRACT_VERSION,
              'prompt_sha256': digest(EXTRACTION_PROMPT.encode()), 'repair_prompt_sha256': digest(QUOTE_REPAIR_PROMPT.encode()),
              'human_review': load_manifest()['human_review'], 'cases': [{'id': c['case_id'], 'source_sha256': c['source_sha256'],
              'case_sha256': sha(c), 'dictionary_sha256': sha(c['dictionary'])} for c in fixtures]},
              'repetitions': [], 'attempts': budget.attempts, 'budget': budget.summary(), 'summary': None,
              'cleanup_errors': 0, 'batch_teardown_seconds': None}
    def save():
        with budget.lock:
            report['budget'] = budget.summary()
            try: writer.write(copy.deepcopy(report))
            except (OSError, ValueError): budget.stop('report_write_failed'); raise Stopped('report_write_failed') from None
    def close(resource):
        if resource is None: return
        try:
            resource.close()
        except Exception:
            report['cleanup_errors'] += 1
            budget.stop('cleanup_error')

    def experiment_transport(spec=None):
        from .extraction_transport import ExtractionTransport
        if spec is not None: return ExtractionTransport(spec)
        return ExtractionTransport.from_cli(CLI(timeout=timeout, persistent=False), model, capacity=extraction_concurrency)

    batch_owner = None
    batch_owner_id = 0
    save()
    try:
        for repetition in range(1, limits.repetitions+1):
            for case in fixtures:
                budget.check()
                row = {'repetition': repetition, 'case_id': case['case_id'], 'status': 'running', 'seconds': None,
                       'quality': None, 'document_timing': None, 'attempt_start': len(budget.attempts)+1, 'attempt_count': 0}
                report['repetitions'].append(row); save(); start = budget.clock()
                with tempfile.TemporaryDirectory(prefix='verinote-benchmark-') as temp:
                    root = Path(temp)
                    def make_caller(spec=None, case=case, repetition=repetition, row=row):
                        nonlocal batch_owner, batch_owner_id
                        if process_mode == 'batch':
                            if spec is not None and batch_owner is not None and batch_owner.extraction_spec != spec:
                                replacement_started = budget.clock()
                                close(batch_owner)
                                row['owner_replacement_seconds'] = max(0, budget.clock()-replacement_started)
                                budget.check()  # Cleanup failure is terminal; never overlap different pools.
                                batch_owner = None
                            if batch_owner is None:
                                batch_owner = BudgetCaller(experiment_transport(spec) if spec is not None or extraction_concurrency is not None else CLI(timeout=timeout), budget, owns_caller=True, retry_close=spec is not None)
                                batch_owner_id += 1
                            caller = BudgetCaller(batch_owner.caller, budget, save, lifecycle=batch_owner.lifecycle,
                                                  identity={'owner_id': batch_owner_id, 'case_id': case['case_id'], 'repetition': repetition})
                        else:
                            base = experiment_transport(spec) if spec is not None or extraction_concurrency is not None else caller_factory(case) if caller_factory else (FakeCLI(case, scenario) if mode == 'fake'
                                    else CLI(timeout=timeout, persistent=process_mode == 'document'))
                            caller = BudgetCaller(base, budget, save, owns_caller=True,
                                                  identity={'owner_id': len(report['repetitions']), 'case_id': case['case_id'], 'repetition': repetition})
                            base.timeout = timeout
                        return caller
                    try:
                        execute_document(root, case, make_caller, budget, row, close,
                                         agent=agent, model=model, extraction_concurrency=extraction_concurrency,
                                         extraction_policy=extraction_policy, registration_cli=CLI(timeout=timeout, persistent=False) if extraction_policy is not None else None)
                    finally:
                        if budget.remaining() <= 0: budget.stop('time_limit')
                        row['seconds'] = max(0, budget.clock()-start)
                        row['attempt_count'] = len(budget.attempts)-row['attempt_start']+1
                save()
    except Stopped: pass
    except KeyboardInterrupt: budget.stop('interrupted')
    except Exception: budget.stop('runner_error')
    finally:
        if batch_owner is not None:
            teardown_started = budget.clock()
            close(batch_owner)
            report['batch_teardown_seconds'] = max(0, budget.clock()-teardown_started)
        if budget.remaining() <= 0: budget.stop('time_limit')
        with budget.lock:
            report['stop_reason'] = budget.reason
            report['status'] = 'stopped' if budget.reason else 'completed'
            report['budget'] = budget.summary()
            report['summary'] = {'completed_case_runs': sum(r['status'] != 'running' for r in report['repetitions']),
                'successful_repetitions': sum(r['status'] == 'success' for r in report['repetitions']),
                'failed_repetitions': sum(r['status'] == 'error' for r in report['repetitions']),
                'success_seconds': distribution([r['seconds'] for r in report['repetitions'] if r['status'] == 'success']),
                'failure_seconds': distribution([r['seconds'] for r in report['repetitions'] if r['status'] == 'error']),
                'per_case': [{'case_id': c['case_id'], 'success_seconds': distribution([r['seconds'] for r in report['repetitions'] if r['case_id'] == c['case_id'] and r['status'] == 'success']),
                              'failure_seconds': distribution([r['seconds'] for r in report['repetitions'] if r['case_id'] == c['case_id'] and r['status'] == 'error'])} for c in fixtures],
                'call_timing_available': sum(a['cli_timing'] is not None for a in budget.attempts),
                'document_timing_available': sum(r['document_timing'] is not None and r['document_timing']['available'] for r in report['repetitions']),
                'failed_attempts': sum(a['status'] == 'error' for a in budget.attempts),
                'credits': [{'provenance': provenance, 'unit_sha256': unit,
                             'value': sum(a[provenance+'_credits'] for a in budget.attempts if a[key] == unit and a[provenance+'_credits'] is not None),
                             'known_calls': sum(a[key] == unit and a[provenance+'_credits'] is not None for a in budget.attempts)}
                            for provenance, key in (('reported', 'credit_unit_sha256'), ('estimated', 'estimated_credit_unit_sha256'))
                            for unit in sorted({a[key] for a in budget.attempts if a[key] is not None and a[provenance+'_credits'] is not None})],
                'repair_attempts': sum(a['repair'] for a in budget.attempts),
                'reported_usd': {'known_calls': sum(a['reported_usd'] is not None for a in budget.attempts),
                                 'value': sum(a['reported_usd'] for a in budget.attempts if a['reported_usd'] is not None) if any(a['reported_usd'] is not None for a in budget.attempts) else None},
                'estimated_usd': {'known_calls': sum(a['estimated_usd'] is not None for a in budget.attempts),
                                 'value': sum(a['estimated_usd'] for a in budget.attempts if a['estimated_usd'] is not None) if any(a['estimated_usd'] is not None for a in budget.attempts) else None}}
        save()
    return report
