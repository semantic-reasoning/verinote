"""Sequential local baseline, with independently checked retained evidence."""
import copy
import json
import math
import platform
import re
import shutil
import sqlite3
import statistics
import time
from pathlib import Path

from . import ollama
from .agents import CLI, parse_response
from .documents import chunks
from .extraction_ab_runner import _command, _hash, file_index
from .extraction_benchmark import (
    MAX_REPORT_BYTES,
    SYNTHETIC_REVIEW_REASON_V2,
    ReportWriter,
    Stopped,
    _Executor,
    quality_report,
    sha,
)
from .extraction_checkpoint import ExtractionCheckpoint
from .extraction_coverage import repair_missing_indices
from .extraction_evaluation import DATA_ROOT, _read, load_case, load_manifest
from .extraction_threshold_artifacts import EvidenceStore
from .extraction_timing import read_extraction_timing
from .ontology import (
    apply_quote_repairs,
    bounded_prompt,
    dictionary_context,
    extraction_prompts,
    extraction_quote_issues,
)
from .service import Service
from .store import Store, canonical, digest
from .usage import atomic_json, number

REPO = Path(__file__).resolve().parents[1]
MODEL = 'qwen2.5:7b'
MODEL_DIGEST = '845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e'
VERSION = '0.40.13'
LIMITS = {'requests': 65, 'seconds': 3600, 'tokens': 2500000}
CONTRACT = 'docs/extraction-ollama-baseline-contract.md'
SCRIPT = 'scripts/benchmark_extraction_ollama.py'


def execution_options():
    return ollama.options() | {'model': MODEL, 'model_digest': MODEL_DIGEST}


def plan():
    return {'schema_version': 1, 'documents': [
        {'key': f'{name}-{rep}', 'case_id': name, 'repetition': rep,
         'purpose': 'preparation' if name == 'short' else 'measurement',
         'chunks': {'short': 1, 'three': 3, 'four': 4, 'eight': 8}[name]}
        for name in ('short', 'three', 'four', 'eight')
        for rep in range(1, 2 if name == 'short' else 4)],
        'initial_requests': 46, 'maximum_repairs': 19, 'limits': dict(LIMITS),
        'model': MODEL, 'model_digest': MODEL_DIGEST, 'ollama_version': VERSION,
        'options': execution_options(), 'concurrency': 1, 'automatic_retry': False,
        'provider_cache_state': 'unknown', 'temperature_interpretation': 'no determinism guarantee'}


def _sources():
    return sorted([*(p.relative_to(REPO).as_posix() for p in (REPO/'verinote').glob('*.py')), CONTRACT, SCRIPT])


def _identity_matches(value):
    return (value.get('version', {}).get('version') == VERSION
            and any(m['id'] == MODEL and m['digest'] == MODEL_DIGEST for m in value.get('models', [])))


def _identity():
    value = {'version': ollama.request('/api/version', timeout=5), 'models': ollama.catalog(timeout=5)}
    if not _identity_matches(value):
        raise ValueError('Ollama version/model digest differs from baseline contract')
    return value


class Ledger:
    """Sequential admission; a finished in-flight call always contributes usage."""
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock(); self.reason = None; self.attempts = []; self.source_hashes = {}

    def stop(self, reason):
        if self.reason is None: self.reason = reason

    def remaining(self): return max(0, LIMITS['seconds']-(self.clock()-self.started))

    def summary(self):
        counts = [v for a in self.attempts for v in (a.get('input_tokens'), a.get('output_tokens')) if v is not None]
        return {'requests': len(self.attempts), 'finished': sum(a['status'] != 'running' for a in self.attempts),
                'pending': sum(a['status'] == 'running' for a in self.attempts),
                'observed_tokens': sum(counts), 'elapsed_seconds': max(0, self.clock()-self.started),
                'unknown_usage_calls': sum(a.get('input_tokens') is None or a.get('output_tokens') is None for a in self.attempts),
                'token_hard_cap_guaranteed': False, 'reported_usd': None, 'cache_tokens': None}

    def check(self):
        if any(not (REPO/p).is_file() or _hash(REPO/p) != value for p, value in self.source_hashes.items()):
            self.stop('source_changed')
        if self.remaining() <= 0: self.stop('time_limit')
        if self.summary()['observed_tokens'] >= LIMITS['tokens']: self.stop('token_limit')
        if len(self.attempts) >= LIMITS['requests']: self.stop('request_limit')
        if self.reason: raise Stopped(self.reason)


def call_evidence(folder, model=MODEL):
    """Recompute usage directly from the retained API body, with linked call IDs."""
    folder = Path(folder)
    meta = _read(folder/'run.json'); usage = _read(folder/'usage.json')
    cid = meta.get('provider_call_id')
    if (not isinstance(cid, str) or len(cid) != 32 or any(c not in '0123456789abcdef' for c in cid)
            or usage.get('provider_call_id') != cid or meta.get('agent') != 'ollama'
            or usage.get('agent') != 'ollama' or meta.get('model') != model or usage.get('requested_model') != model
            or meta.get('status') != usage.get('status') or meta.get('status') not in ('success', 'error')):
        raise ValueError('call linkage')
    opts = ollama.options(meta['execution_options'])
    expected_options = execution_options(); expected_options['timeouts']['default'] = opts['timeouts']['default']
    if opts != expected_options or not 0 < opts['timeouts']['default'] <= 180: raise ValueError('call options or timeout')
    prompt_path = folder/'prompt.txt'
    if prompt_path.is_symlink() or _hash(prompt_path) != meta['prompt_sha256']: raise ValueError('prompt identity')
    response = _read(folder/'response.json') if (folder/'response.json').exists() else None
    counts = {k: number(response.get(field), True) if response else None
              for k, field in (('input', 'prompt_eval_count'), ('output', 'eval_count'))}
    resolved = response.get('model') if response and response.get('model') == model else None
    timings = {k: number(response.get(k), True) if response else None for k in
               ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')}
    if (usage.get('tokens', {}).get('input') != counts['input'] or usage.get('tokens', {}).get('output') != counts['output']
            or usage.get('resolved_model') != resolved or usage.get('complete') is not all(v is not None for v in counts.values())
            or any(usage.get(k) is not None for k in ('reported_usd', 'estimated_usd', 'reported_credits', 'estimated_credits'))
            or any(usage.get('tokens', {}).get(k) is not None for k in ('cache_read', 'cache_write', 'reasoning'))):
        raise ValueError('provider usage mismatch')
    if response and usage.get('provider_timings_ns') != timings: raise ValueError('provider timing mismatch')
    if meta['status'] == 'success' and (not response or response.get('done') is not True or response.get('done_reason') != 'stop'
            or resolved != model or not isinstance(response.get('response'), str)):
        raise ValueError('incomplete provider response')
    after = _read(folder/'identity-after.json')
    return {'provider_call_id': cid, 'resolved_model': resolved, 'model_digest': meta.get('model_digest'),
            'identity_after_verified': _identity_matches(after),
            'input_tokens': counts['input'], 'output_tokens': counts['output'], 'provider_timings_ns': timings,
            'provider_status': meta['status']}


class Caller:
    def __init__(self, job, ledger, row, save):
        self.job, self.ledger, self.row, self.save = job, ledger, row, save

    def limited_timeout(self, agent, seconds):
        return Caller(self.job.limited_timeout(agent, seconds), self.ledger, self.row, self.save)

    def close(self): self.job.close()

    def complete(self, agent, model, prompt, folder):
        ledger = self.ledger; ledger.check()
        folder = Path(folder); repair = folder.name == 'quote-repair'
        chunk = int(folder.parent.name if repair else folder.name)
        if repair and (sum(a['repair'] and a['document_key'] == self.row['key'] for a in ledger.attempts) >= 2
                or any(a['repair'] and a['document_key'] == self.row['key'] and a['chunk'] == chunk for a in ledger.attempts)):
            ledger.stop('repair_limit'); raise Stopped('repair_limit')
        row = {'attempt': len(ledger.attempts)+1, 'document_key': self.row['key'], 'chunk': chunk,
               'repair': repair, 'status': 'running', 'input_tokens': None, 'output_tokens': None}
        ledger.attempts.append(row)
        try:
            self.save()
        except Exception:
            row['status'] = 'blocked'; ledger.stop('report_write_failed'); raise
        started = ledger.clock()
        try:
            result = self.job.limited_timeout(agent, min(180, ledger.remaining())).complete(agent, model, prompt, folder)
            row['status'] = 'success'
            return result
        except BaseException:
            row['status'] = 'error'; ledger.stop('call_failed'); raise
        finally:
            row['wrapper_seconds'] = max(0, ledger.clock()-started)
            # Retain post-call observations, including an identity change, for offline reassessment.
            try:
                after = {'version': ollama.request('/api/version', timeout=5), 'models': ollama.catalog(timeout=5)}
                atomic_json(folder/'identity-after.json', after)
                if not _identity_matches(after): ledger.stop('model_changed')
            except (OSError, ValueError):
                ledger.stop('model_identity_unknown')
            try:
                row.update(call_evidence(folder))
                if row['resolved_model'] != MODEL or row['model_digest'] != MODEL_DIGEST:
                    ledger.stop('model_changed')
                if row['input_tokens'] is None or row['output_tokens'] is None: ledger.stop('usage_unknown')
            except (OSError, ValueError, KeyError, TypeError): ledger.stop('call_evidence_unknown')
            if ledger.remaining() <= 0: ledger.stop('time_limit')
            if ledger.summary()['observed_tokens'] >= LIMITS['tokens']: ledger.stop('token_limit')
            try: self.save()
            except Exception:
                ledger.stop('report_write_failed'); raise


class BaselineService(Service):
    def __init__(self, *args, ledger, row, save, **kwargs):
        self.ledger, self.row, self.save = ledger, row, save
        super().__init__(*args, **kwargs)

    def register_transport(self, *args, **kwargs):
        options, job = super().register_transport(*args, **kwargs)
        if options != execution_options(): raise ValueError('baseline options mismatch')
        return options, Caller(job, self.ledger, self.row, self.save)


def _timing(root, store, run_id):
    value = read_extraction_timing(root, store, run_id)
    return {'available': True, 'summary': value['summary'], 'status': value['status'], 'error_code': value['error_code']} if value['available'] else {'available': False}


def verify_document(root, row, attempts, case):
    store = EvidenceStore(root/'state.sqlite3')
    run = store.run(row['run_id']); doc = store.document(run['document_id'], run['version'])
    if (run['agent'] != 'ollama' or run['model'] != MODEL or run['stage'] != 'extract' or run['version'] != 1
            or run['document_id'] != row['document_id'] or run['status'] != row['status']
            or run.get('execution_options') != execution_options() or run.get('extraction_options') is not None
            or run['ontology']['payload'] != case['dictionary'] or run['ontology']['sha256'] != digest(case['dictionary'])
            or run['ontology']['extraction_contract'] != 2 or doc['text'] != case['source']
            or store.raw(run['document_id'], 1)[0] != case['source'].encode()):
        raise ValueError('document contract')
    with store.connect() as db:
        if any(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] != count for table, count in
               (('documents', 1), ('versions', 1), ('runs', 2), ('questions', 0))): raise ValueError('fresh store')
        events = db.execute('SELECT action,actor,reason,revision FROM events WHERE item_id=? ORDER BY revision', (run['ontology']['id'],)).fetchall()
    if (len(events) != 2 or (events[0]['action'], events[0]['actor'], events[0]['revision']) != ('created', 'agent:ollama', 1)
            or dict(events[1]) != {'action': 'approved', 'actor': 'Synthetic fixture', 'reason': SYNTHETIC_REVIEW_REASON_V2,
                                  'revision': run['ontology']['revision']}): raise ValueError('synthetic approval provenance')
    quality = quality_report(case, root, run)
    if quality != row['quality'] or _timing(root, store, run['id']) != row['document_timing']: raise ValueError('quality or timing mismatch')
    candidates = [i for i in doc['items'] if i['kind'] != 'ontology']; violations = []
    if doc['current_version'] != 1 or doc['generation'] != 1: violations.append('document_publication_contract')
    dictionaries = [i for i in doc['items'] if i['kind'] == 'ontology']
    if (len(dictionaries) != 1 or dictionaries[0]['status'] != 'approved' or dictionaries[0]['id'] != run['ontology']['id']
            or dictionaries[0]['revision'] != run['ontology']['revision']
            or digest(dictionaries[0]['payload']) != run['ontology']['sha256']): violations.append('dictionary_publication_contract')
    if any(i['status'] != 'pending' for i in candidates): violations.append('human_approval_contract')
    local = [a for a in attempts if a['document_key'] == row['key']]
    expected_usage = set(); ids = set(); replay: dict[int, dict] = {}
    repairs = [a for a in local if a['repair']]
    initial = [a['chunk'] for a in local if not a['repair'] and a['status'] != 'blocked']
    if (len(repairs) > min(2, row['chunks']) or len({a['chunk'] for a in repairs}) != len(repairs)
            or initial != list(range(1, len(initial)+1))): raise ValueError('call schedule')
    slices = list(chunks(case['source'])); prompt, repair_prompt = extraction_prompts(2)
    for attempt in local:
        if attempt['status'] == 'blocked': continue
        index = attempt['chunk']
        if type(index) is not int or not 1 <= index <= len(slices): raise ValueError('chunk index')
        folder = root/'runs'/run['id']/str(index)
        if attempt['repair']: folder /= 'quote-repair'
        expected_usage.add((folder/'usage.json').relative_to(root).as_posix())
        metrics = call_evidence(folder)
        if (any(attempt.get(k) != v for k, v in metrics.items()) or metrics['provider_call_id'] in ids
                or attempt['status'] != metrics['provider_status']):
            raise ValueError('attempt linkage')
        ids.add(metrics['provider_call_id'])
        if metrics['model_digest'] != MODEL_DIGEST or metrics['resolved_model'] not in (None, MODEL): raise ValueError('model identity')
        offset, text = slices[index-1]
        payload = {'filename': doc['filename'], 'version': 1, 'offset': offset, 'text': text}
        if attempt['repair']:
            if index not in replay: raise ValueError('repair without initial output')
            original = replay[index]
            issues = extraction_quote_issues(original, text, case['dictionary'], contract_version=2)
            expected_prompt = bounded_prompt(repair_prompt, payload | {'issues': issues, 'approved_dictionary': run['ontology']})
        else:
            if index in replay: raise ValueError('duplicate initial call')
            expected_prompt = bounded_prompt(prompt, payload | {'approved_dictionary': run['ontology'] | {'payload': dictionary_context(case['dictionary'])}})
        actual_prompt = (folder/'prompt.txt').read_text()
        actual_prefix, actual_payload = actual_prompt.split('입력(JSON):\n', 1)
        expected_prefix, expected_payload = expected_prompt.split('입력(JSON):\n', 1)
        if actual_prefix != expected_prefix or json.loads(actual_payload) != json.loads(expected_payload):
            raise ValueError('request prompt mismatch')
        if metrics['provider_status'] == 'success':
            value = parse_response('ollama', _read(folder/'response.json')['response'])[0]
            if attempt['repair']:
                normalized, _ = repair_missing_indices(original, case['dictionary'])
                value, changes = apply_quote_repairs(normalized, text, issues, value)
                if _read(folder/'changes.json') != changes: raise ValueError('repair audit mismatch')
            replay[index] = value
    if {p.relative_to(root).as_posix() for p in root.rglob('usage.json')} != expected_usage: raise ValueError('unaccounted usage')
    cp_path = root/'runs'/run['id']/'extraction-checkpoint.json'
    outputs = []
    if cp_path.exists():
        cp = _read(cp_path)
        if cp['contract'].get('execution_options') != run['execution_options']: raise ValueError('checkpoint options')
        validator = ExtractionCheckpoint.__new__(ExtractionCheckpoint); validator.run = run
        for index, output in enumerate(cp['outputs'], 1):
            normalized, _ = repair_missing_indices(replay[index], case['dictionary'])
            if validator.validated(normalized, slices[index-1][1]) != output: raise ValueError('checkpoint provider mismatch')
        outputs = cp['outputs']
    if run['status'] == 'success':
        expected = {canonical(f) for output in outputs for f in output['facts']}
        actual = [i for i in candidates if i['kind'] == 'fact']; pages = [i for i in candidates if i['kind'] == 'page']
        if (len(outputs) != len(slices) or {canonical(i['payload']) for i in actual} != expected or len(actual) != len(expected)
                or len(pages) != 1 or pages[0]['payload']['claims'] != [c for output in outputs for c in output['claims']]
                or any(i['kind'] not in ('fact', 'page') or i['run_id'] != run['id'] for i in candidates)):
            violations.append('candidate_publication')
    elif candidates: violations.append('partial_publication')
    return {'complete': True, 'passed': not violations, 'violations': violations}


def run(output, *, synthetic=False, clock=None, invocation=None):
    """Only this entrypoint contacts Ollama; synthetic is for injected test responses."""
    if type(synthetic) is not bool: raise ValueError('synthetic flag')
    if not synthetic and _command(['git', 'status', '--porcelain'], REPO): raise ValueError('clean checkout required')
    ledger = Ledger(clock)
    identity = _identity()
    writer = ReportWriter(output); root = writer.folder
    shutil.copytree(DATA_ROOT/'v2', root/'fixtures')
    source_hashes = {p: _hash(REPO/p) for p in _sources()}
    ledger.source_hashes = source_hashes
    for relative in source_hashes:
        dest = root/'implementation'/relative; dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(REPO/relative, dest)
    prompts = dict(zip(('extraction', 'repair'), extraction_prompts(2), strict=True))
    environment = {'schema_version': 1, 'synthetic': synthetic, 'identity': identity, 'options': execution_options(),
                   'python': platform.python_version(), 'platform': platform.platform(), 'invocation': invocation or ['python_api', 'run'],
                   'revision': None if synthetic else _command(['git', 'rev-parse', 'HEAD'], REPO),
                   'tree': None if synthetic else _command(['git', 'rev-parse', 'HEAD^{tree}'], REPO),
                   'source_hashes': source_hashes, 'dataset_manifest': load_manifest(DATA_ROOT/'v2'),
                   'cases': {name: sha(load_case(name, DATA_ROOT/'v2')) for name in ('short', 'three', 'four', 'eight')},
                   'prompts': {k: digest(v.encode()) for k, v in prompts.items()}}
    for name, value in [('plan', plan()), ('environment', environment), ('prompts', prompts), ('limits', LIMITS)]:
        atomic_json(root/(name+'.json'), value)
    report = {'schema_version': 1, 'environment': environment, 'plan': plan(), 'limits': dict(LIMITS),
              'synthetic_responses': synthetic, 'documents': [], 'attempts': ledger.attempts,
              'status': 'running', 'stop_reason': None, 'budget': ledger.summary()}
    def save():
        report['budget'] = ledger.summary(); writer.write(copy.deepcopy(report))
    save()
    _verify_manifest(root, report)
    try:
        for entry in plan()['documents']:
            ledger.check(); row = dict(entry, artifact='documents/'+entry['key'], status='running', quality=None,
                                       safety=None, document_timing=None, attempt_start=len(ledger.attempts)+1)
            report['documents'].append(row); save()
            artifact = root/row['artifact']; artifact.mkdir(parents=True)
            started = ledger.clock()
            store = Store(artifact/'state.sqlite3'); service = None; job = None
            case = load_case(entry['case_id'], root/'fixtures')
            try:
                doc = store.register('Synthetic '+entry['key'], 'synthetic.md', case['source'].encode(), case['source'])
                seed = store.create_run(doc['id'], 'ollama', MODEL, 'ontology', 1, 1)
                store.run_status(seed['id'], 'running'); store.finish_run(seed['id'], dictionary=case['dictionary'])
                item = next(i for i in store.document(doc['id'])['items'] if i['kind'] == 'ontology')
                store.review(item['id'], item['revision'], 'approved', 'Synthetic fixture', SYNTHETIC_REVIEW_REASON_V2)
                service = BaselineService(store, artifact, CLI(), ledger=ledger, row=row, save=save)
                service.pool.shutdown(wait=True); service.pool = _Executor(max_workers=1)
                job = service.start(doc['id'], 'ollama', MODEL, expected_version=1, expected_generation=1)
                row.update(run_id=job['id'], document_id=doc['id'])
                service.pool.future.result()
            finally:
                if service is not None:
                    try: service.close()
                    except Exception: ledger.stop('cleanup_error')
                row['attempt_count'] = len(ledger.attempts)-row['attempt_start']+1
                if job is not None:
                    job = store.run(job['id']); row['status'] = job['status']
                    row['quality'] = quality_report(case, artifact, job)
                    row['document_timing'] = _timing(artifact, store, job['id'])
                    try: row['safety'] = verify_document(artifact, row, ledger.attempts, case)
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        ledger.stop('document_evidence_unknown')
                row['seconds'] = max(0, ledger.clock()-started)
                row['artifact_files'] = file_index(artifact); save()
            if row['status'] != 'success' or not row['quality']['passed'] or not (row['safety'] or {}).get('passed'):
                ledger.stop('document_failed')
            if ledger.reason: break
    except Stopped: pass
    except KeyboardInterrupt: ledger.stop('interrupted')
    except Exception: ledger.stop('runner_error')
    finally:
        if any(not (REPO/p).is_file() or _hash(REPO/p) != value for p, value in source_hashes.items()): ledger.stop('source_changed')
        if ledger.remaining() <= 0: ledger.stop('time_limit')
        report.update(status='stopped' if ledger.reason else 'completed', stop_reason=ledger.reason)
        save()
        index = file_index(root, excluded=('index.json', 'evaluation.json'))
        atomic_json(root/'index.json', index)
        result = evaluate(root)
        # Account for full evidence hashing and reassessment before the last
        # observation; only final report/index/verdict writes remain afterward.
        if ledger.remaining() <= 0: ledger.stop('time_limit')
        report.update(status='stopped' if ledger.reason else 'completed', stop_reason=ledger.reason)
        save()
        if ledger.reason and result.get('series'):
            if 'incomplete_baseline' not in result['reasons']:
                position = result['reasons'].index('synthetic_only') if 'synthetic_only' in result['reasons'] else len(result['reasons'])
                result['reasons'].insert(position, 'incomplete_baseline')
            if result['verdict'] == 'complete': result['verdict'] = 'inconclusive'
        index['report.json'] = {'bytes': (root/'report.json').stat().st_size, 'sha256': _hash(root/'report.json')}
        atomic_json(root/'index.json', index)
        atomic_json(root/'evaluation.json', result)
    return report, result


def _verify_manifest(root, report):
    env = report['environment']
    if (report['schema_version'] != 1 or report['plan'] != plan() or report['limits'] != LIMITS
            or _read(root/'plan.json') != plan() or _read(root/'limits.json') != LIMITS
            or _read(root/'environment.json') != env or env['options'] != execution_options()
            or type(env['synthetic']) is not bool or report['synthetic_responses'] is not env['synthetic']
            or env['identity']['version'].get('version') != VERSION
            or not any(m['id'] == MODEL and m['digest'] == MODEL_DIGEST for m in env['identity']['models'])):
        raise ValueError('manifest contract')
    if (not isinstance(env.get('invocation'), list) or not env['invocation']
            or any(not isinstance(a, str) for a in env['invocation'])
            or not env['synthetic'] and any(not re.fullmatch(r'[a-f0-9]{40}', env.get(k, '')) for k in ('revision', 'tree'))):
        raise ValueError('execution provenance')
    expected_prompts = dict(zip(('extraction', 'repair'), extraction_prompts(2), strict=True))
    if _read(root/'prompts.json') != expected_prompts or env['prompts'] != {k: digest(v.encode()) for k, v in expected_prompts.items()}:
        raise ValueError('prompt manifest')
    if env['dataset_manifest'] != load_manifest(root/'fixtures') or env['dataset_manifest'] != load_manifest(DATA_ROOT/'v2'):
        raise ValueError('fixture manifest')
    for name in ('short', 'three', 'four', 'eight'):
        case = load_case(name, root/'fixtures')
        if case != load_case(name, DATA_ROOT/'v2') or env['cases'][name] != sha(case): raise ValueError('fixture contract')
    if set(env['source_hashes']) != set(_sources()) or set(file_index(root/'implementation')) != set(_sources()): raise ValueError('source inventory')
    for p, value in env['source_hashes'].items():
        if _hash(root/'implementation'/p) != value or _hash(REPO/p) != value: raise ValueError('source identity')


def evaluate(root):
    """Read-only: no HTTP, subprocess, Store migration, or saved-verdict trust."""
    root = Path(root).resolve(); verified = []; invalid = False
    try:
        retained = _read(root/'index.json', MAX_REPORT_BYTES)
        if not isinstance(retained, dict): raise ValueError('index')
        metadata = {}
        if (root/'documents').is_symlink(): raise ValueError('documents symlink')
        for path in root.iterdir():
            if path.name in ('documents', 'index.json', 'evaluation.json'): continue
            if path.is_symlink(): raise ValueError('metadata symlink')
            if path.is_dir(): metadata.update({path.name+'/'+p: v for p, v in file_index(path).items()})
            elif path.is_file(): metadata[path.name] = {'bytes': path.stat().st_size, 'sha256': _hash(path)}
        if {p: v for p, v in retained.items() if not p.startswith('documents/')} != metadata: raise ValueError('metadata identity')
        report = _read(root/'report.json', MAX_REPORT_BYTES); _verify_manifest(root, report)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed'], 'series': []}
    next_attempt = 1; call_ids = set()
    for ordinal, row in enumerate(report['documents']):
        try:
            expected = plan()['documents'][ordinal]
            if any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in expected.items()): raise ValueError('schedule')
            if row['artifact'] != 'documents/'+expected['key'] or row['attempt_start'] != next_attempt: raise ValueError('document linkage')
            artifact = root/row['artifact']
            if artifact.is_symlink(): raise ValueError('document symlink')
            current = file_index(artifact)
            if current != row['artifact_files'] or current != {p[len(row['artifact'])+1:]: v for p, v in retained.items() if p.startswith(row['artifact']+'/')}:
                raise ValueError('document hash')
            if type(row['seconds']) not in (int, float) or not math.isfinite(row['seconds']) or row['seconds'] < 0: raise ValueError('document seconds')
            if type(row['attempt_count']) is not int or row['attempt_count'] < 0: raise ValueError('attempt count')
            values = report['attempts'][next_attempt-1:next_attempt-1+row['attempt_count']]
            if len(values) != row['attempt_count'] or any(a['document_key'] != row['key'] or a['attempt'] != next_attempt+i for i, a in enumerate(values)):
                raise ValueError('attempt sequence')
            for a in values:
                cid = a.get('provider_call_id')
                if cid is not None:
                    if cid in call_ids: raise ValueError('duplicate provider call')
                    call_ids.add(cid)
            safety = verify_document(artifact, row, values, load_case(row['case_id'], root/'fixtures'))
            if safety != row['safety']: raise ValueError('safety mismatch')
            verified.append(row); next_attempt += row['attempt_count']
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error):
            invalid = True; break
    reasons = []
    for row in verified:
        if row['quality']['complete'] and not row['quality']['passed']: reasons.append('quality:'+row['key'])
        if row['safety']['complete'] and not row['safety']['passed']: reasons.append('safety:'+row['key'])
        if row['status'] == 'error': reasons.append('extraction_failed:'+row['key'])
    failed = bool(reasons)
    if not invalid:
        try:
            if retained != file_index(root, excluded=('index.json', 'evaluation.json')): raise ValueError('global index')
            attempts = report['attempts']; budget = report['budget']
            if (next_attempt-1 != len(attempts) or budget['requests'] != len(attempts) or len(attempts) > LIMITS['requests']
                    or budget['pending'] != 0 or budget['finished'] != len(attempts)
                    or budget['observed_tokens'] != sum(a.get(k) or 0 for a in attempts for k in ('input_tokens', 'output_tokens'))
                    or budget['unknown_usage_calls'] != sum(a.get('input_tokens') is None or a.get('output_tokens') is None for a in attempts)
                    or {p.name for p in (root/'documents').iterdir()} != {r['key'] for r in verified}): raise ValueError('budget ledger')
            if (any(a['status'] not in ('success', 'error', 'blocked') for a in attempts)
                    or type(budget['elapsed_seconds']) not in (int, float) or not math.isfinite(budget['elapsed_seconds'])
                    or budget['elapsed_seconds'] < 0 or budget['token_hard_cap_guaranteed'] is not False
                    or budget['reported_usd'] is not None or budget['cache_tokens'] is not None
                    or report['status'] not in ('completed', 'stopped')
                    or report['status'] == 'completed' and (budget['elapsed_seconds'] >= LIMITS['seconds']
                        or budget['observed_tokens'] >= LIMITS['tokens'] or report['stop_reason'] is not None)):
                raise ValueError('budget completion')
        except (OSError, ValueError, KeyError, TypeError): invalid = True
    if invalid: reasons.append('artifact_verification_failed')
    complete = (not invalid and len(verified) == 10 and report['status'] == 'completed' and report['stop_reason'] is None
                and all(r['status'] == 'success' and r['quality']['passed'] and r['safety']['passed'] for r in verified)
                and all(a.get('input_tokens') is not None and a.get('output_tokens') is not None
                        and a.get('resolved_model') == MODEL and a.get('model_digest') == MODEL_DIGEST
                        and a.get('identity_after_verified') is True for a in report['attempts']))
    if not complete: reasons.append('incomplete_baseline')
    if report['synthetic_responses']: reasons.append('synthetic_only')
    verdict = 'failed' if failed else 'complete' if complete else 'inconclusive'
    if report['synthetic_responses']: verdict = 'inconclusive'
    series = []
    for name in ('three', 'four', 'eight'):
        values = [r['seconds'] for r in verified if r['case_id'] == name and r['status'] == 'success' and r['quality']['passed'] and r['safety']['passed']]
        series.append({'case_id': name, 'completed_repetitions': len(values), 'seconds':
                       {'median': statistics.median(values), 'min': min(values), 'max': max(values)} if len(values) == 3 else None})
    return {'verdict': verdict, 'reasons': reasons, 'series': series, 'unverified_other_records': invalid,
            'provider_cache_state': 'unknown', 'reported_usd': None,
            'comparison_scope': 'local sequential baseline only; no Sonnet acceptance or speedup claim'}
