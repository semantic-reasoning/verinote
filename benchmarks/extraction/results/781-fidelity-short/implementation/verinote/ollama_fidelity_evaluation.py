"""One bounded opt-in fidelity candidate trial; offline evidence is never executed."""
import copy
import json
import math
import platform
import re
import shutil
import sqlite3
import time
from pathlib import Path

from . import ollama
from .agents import CLI, parse_response
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
from .extraction_coverage import identity as coverage_identity
from .extraction_coverage import repair_missing_indices
from .extraction_evaluation import DATA_ROOT, _read, load_case, load_manifest
from .extraction_threshold_artifacts import EvidenceStore
from .extraction_timing import read_extraction_timing
from .ollama_fidelity import (
    CANDIDATE_ID,
    TEMPLATE,
    assess_output,
    candidate_options,
    compile_prompt,
    template_hash,
)
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


class EvidenceMismatch(ValueError): pass


REPO = Path(__file__).resolve().parents[1]
MODEL = 'qwen3.8:27b-mlx'
MODEL_DIGEST = '5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e'
VERSION = '0.40.13'
LIMITS = {'requests': 2, 'seconds': 600, 'tokens': 100000}
CONTRACT = 'docs/ollama-fidelity-candidate-contract.md'
SCRIPT = 'scripts/evaluate_ollama_fidelity.py'


def execution_options():
    return candidate_options(ollama.options() | {'model': MODEL, 'model_digest': MODEL_DIGEST})


def plan():
    return {'schema_version': 1, 'candidate_id': CANDIDATE_ID, 'template_sha256': template_hash(),
            'documents': [{'key': 'short-1', 'case_id': 'short', 'repetition': 1, 'chunks': 1}],
            'initial_requests': 1, 'maximum_repairs': 1, 'limits': dict(LIMITS),
            'model': MODEL, 'model_digest': MODEL_DIGEST, 'ollama_version': VERSION,
            'options': execution_options(), 'concurrency': 1, 'automatic_retry': False,
            'thinking_policy': 'model_default', 'provider_cache_state': 'unknown'}


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
        # A model/document rejection must not hide later recording, cleanup,
        # identity, or budget failure that makes the trial inconclusive.
        semantic = ('call_failed', 'document_failed')
        if self.reason is None or self.reason in semantic and reason not in semantic:
            self.reason = reason

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
    after = _read(folder/'identity-after.json') if (folder/'identity-after.json').exists() else {}
    response_complete = bool(response and response.get('done') is True and response.get('done_reason') == 'stop'
                             and isinstance(response.get('response'), str) and resolved == model)
    return {'response_complete': response_complete, 'provider_call_id': cid, 'resolved_model': resolved, 'model_digest': meta.get('model_digest'),
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
        if repair and (sum(a['repair'] and a['document_key'] == self.row['key'] for a in ledger.attempts) >= 1
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


class CandidateService(Service):
    def __init__(self, *args, ledger, row, save, **kwargs):
        self.ledger, self.row, self.save = ledger, row, save
        super().__init__(*args, **kwargs)

    def register_transport(self, *args, **kwargs):
        if args[:2] != ('ollama', MODEL) or kwargs.get('stage') != 'extract':
            raise ValueError('candidate trial permits only registered extraction')
        options, job = super().register_transport(*args, **kwargs)
        if options != execution_options(): raise ValueError('candidate options mismatch')
        return options, Caller(job, self.ledger, self.row, self.save)


def _timing(root, store, run_id):
    value = read_extraction_timing(root, store, run_id)
    return {'available': True, 'summary': value['summary'], 'status': value['status'], 'error_code': value['error_code']} if value['available'] else {'available': False}


def verify_document(root, row, attempts, case):
    """Reconstruct prompts/output validation before trusting any published summary."""
    store = EvidenceStore(root/'state.sqlite3')
    run = store.run(row['run_id']); doc = store.document(run['document_id'], run['version'])
    if (run['agent'] != 'ollama' or run['model'] != MODEL or run['stage'] != 'extract' or run['version'] != 1
            or run['document_id'] != row['document_id'] or run['status'] != row['status']
            or run.get('execution_options') != execution_options() or run.get('extraction_options') is not None
            or run['ontology']['payload'] != case['dictionary'] or run['ontology']['sha256'] != digest(case['dictionary'])
            or run['ontology']['extraction_contract'] != 2 or doc['text'] != case['source']
            or doc['sha256'] != digest(case['source'].encode()) or doc['filename'] != 'synthetic.md'
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
    if not 1 <= len(attempts) <= 2: raise ValueError('call count')
    if [a['repair'] for a in attempts] not in ([False], [False, True]): raise ValueError('call sequence')
    expected_usage = set(); ids = set(); original = None; issues = None; validated = None
    production = {'complete': False, 'passed': None, 'reason': 'response_incomplete'}
    prompt, repair_prompt = extraction_prompts(2); text = case['source']
    payload = {'filename': doc['filename'], 'version': 1, 'offset': 0, 'text': text}
    validator = ExtractionCheckpoint.__new__(ExtractionCheckpoint); validator.run = run
    for attempt in attempts:
        if attempt['chunk'] != 1 or type(attempt['chunk']) is not int or attempt['status'] == 'blocked':
            raise ValueError('uncompleted call admission')
        folder = root/'runs'/run['id']/'1'
        if attempt['repair']: folder /= 'quote-repair'
        expected_usage.add((folder/'usage.json').relative_to(root).as_posix())
        metrics = call_evidence(folder)
        if (any(attempt.get(k) != v for k, v in metrics.items()) or metrics['provider_call_id'] in ids
                or attempt['status'] != metrics['provider_status']): raise ValueError('attempt linkage')
        ids.add(metrics['provider_call_id'])
        if metrics['model_digest'] not in (None, MODEL_DIGEST) or metrics['resolved_model'] not in (None, MODEL):
            raise ValueError('model identity')
        if attempt['repair']:
            if original is None or not issues: raise ValueError('repair without repairable initial output')
            expected_prompt = bounded_prompt(repair_prompt, payload | {'issues': issues, 'approved_dictionary': run['ontology']})
        else:
            expected_prompt = bounded_prompt(prompt, payload | {'approved_dictionary': run['ontology'] | {'payload': dictionary_context(case['dictionary'])}})
        actual = (folder/'prompt.txt').read_text()
        meta = _read(folder/'run.json')
        if attempt['repair']:
            if (folder/'original-prompt.txt').exists(): raise ValueError('repair was transformed')
            base_prompt = actual
        else:
            base_path = folder/'original-prompt.txt'
            if base_path.is_symlink() or _hash(base_path) != meta['original_prompt_sha256']: raise ValueError('original prompt identity')
            base_prompt = base_path.read_text()
            if compile_prompt(base_prompt) != actual or actual == base_prompt: raise ValueError('candidate compiler mismatch')
        if meta.get('original_prompt_sha256') != digest(base_prompt.encode()): raise ValueError('base prompt digest')
        actual_prefix, actual_payload = base_prompt.split('입력(JSON):\n', 1)
        expected_prefix, expected_payload = expected_prompt.split('입력(JSON):\n', 1)
        if actual_prefix != expected_prefix or json.loads(actual_payload) != json.loads(expected_payload):
            raise ValueError('request prompt mismatch')
        if not metrics['response_complete']:
            production = {'complete': False, 'passed': None, 'reason': 'response_incomplete'}
            continue
        try:
            value = parse_response('ollama', _read(folder/'response.json')['response'])[0]
            if attempt['repair']:
                normalized, _ = repair_missing_indices(original, case['dictionary'])
                value, changes = apply_quote_repairs(normalized, text, issues, value)
                if (folder/'changes.json').exists() and _read(folder/'changes.json') != changes:
                    raise EvidenceMismatch('repair audit mismatch')
            else:
                original = value
                issues = extraction_quote_issues(value, text, case['dictionary'], contract_version=2)
                value, accounting_changes = repair_missing_indices(value, case['dictionary'])
                audit_path = folder/'coverage-repair.json'
                if accounting_changes:
                    audit = {'schema_version': 1, 'identity': coverage_identity(run), 'chunk': 1, 'offset': 0,
                             'source_sha256': doc['sha256'], 'text_sha256': digest(text.encode()),
                             'before_sha256': digest(original), 'after_sha256': digest(value),
                             'scope': 'mechanical_indices_only_not_chunk_acceptance', 'changes': accounting_changes}
                    for key in ('cli_call_id', 'provider_call_id'):
                        if key in meta: audit[key] = meta[key]
                    if not audit_path.is_file() or _read(audit_path) != audit:
                        raise EvidenceMismatch('mechanical audit mismatch')
                elif audit_path.exists(): raise EvidenceMismatch('unexpected mechanical audit')
                if issues:
                    production = {'complete': False, 'passed': None, 'reason': 'quote_repair_incomplete'}
                    continue
            assessment = assess_output(case, value)
            if not assessment['production_passed']: raise ValueError('production contract')
            validated = validator.validated(value, text)
            production = {'complete': True, 'passed': True, 'reason': None}
        except EvidenceMismatch:
            raise
        except (ValueError, TypeError, KeyError):
            production = {'complete': True, 'passed': False, 'reason': 'production_contract'}
    if {p.relative_to(root).as_posix() for p in root.rglob('usage.json')} != expected_usage: raise ValueError('unaccounted usage')
    cp_path = root/'runs'/run['id']/'extraction-checkpoint.json'
    outputs = []
    if cp_path.exists():
        cp = _read(cp_path)
        if (cp['run_id'] != run['id'] or cp['schema_version'] != 1
                or cp['contract'].get('execution_options') != run['execution_options']
                or validated is None or cp['outputs'] != [validated]): raise ValueError('checkpoint provider mismatch')
        outputs = cp['outputs']
    if run['status'] == 'success':
        expected = {canonical(f) for output in outputs for f in output['facts']}
        actual_facts = [i for i in candidates if i['kind'] == 'fact']; pages = [i for i in candidates if i['kind'] == 'page']
        if (len(outputs) != 1 or {canonical(i['payload']) for i in actual_facts} != expected or len(actual_facts) != len(expected)
                or len(pages) != 1 or pages[0]['payload']['claims'] != [c for output in outputs for c in output['claims']]
                or any(i['kind'] not in ('fact', 'page') or i['run_id'] != run['id'] for i in candidates)):
            violations.append('candidate_publication')
        if any(a['repair'] for a in attempts) and not (root/'runs'/run['id']/'1'/'quote-repair'/'changes.json').is_file():
            raise ValueError('missing repair audit')
    elif candidates: violations.append('partial_publication')
    return {'complete': True, 'passed': not violations, 'violations': violations, 'production': production}

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
    prompts = dict(zip(('extraction', 'repair'), extraction_prompts(2), strict=True)) | {'candidate_template': TEMPLATE}
    environment = {'schema_version': 1, 'synthetic': synthetic, 'identity': identity, 'options': execution_options(),
                   'python': platform.python_version(), 'platform': platform.platform(), 'invocation': invocation or ['python_api', 'run'],
                   'revision': None if synthetic else _command(['git', 'rev-parse', 'HEAD'], REPO),
                   'tree': None if synthetic else _command(['git', 'rev-parse', 'HEAD^{tree}'], REPO),
                   'source_hashes': source_hashes, 'dataset_manifest': load_manifest(DATA_ROOT/'v2'),
                   'cases': {name: sha(load_case(name, DATA_ROOT/'v2')) for name in ('short',)},
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
                service = CandidateService(store, artifact, CLI(ollama_fidelity=True), ledger=ledger, row=row, save=save)
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
        save(); index = file_index(root, excluded=('index.json', 'evaluation.json')); atomic_json(root/'index.json', index)
        result = evaluate(root)
        # Full evidence hashing and DB reassessment precede the final wall
        # observation. Reclassify verified facts without repeating that work.
        report['budget'] = ledger.summary()
        if report['budget']['elapsed_seconds'] >= LIMITS['seconds']: ledger.stop('time_limit')
        report.update(status='stopped' if ledger.reason else 'completed', stop_reason=ledger.reason)
        if 'safety' in result:
            result = _decision(report, report['documents'][0], result['safety'])
        writer.write(copy.deepcopy(report))
        index['report.json'] = {'bytes': (root/'report.json').stat().st_size, 'sha256': _hash(root/'report.json')}
        atomic_json(root/'index.json', index)
        atomic_json(root/'evaluation.json', result)
    return report, result


def _verify_manifest(root, report):
    env = report['environment']
    if (type(report['schema_version']) is not int or report['schema_version'] != 1
            or type(env.get('schema_version')) is not int or env.get('schema_version') != 1 or report['plan'] != plan() or report['limits'] != LIMITS
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
    expected_prompts = dict(zip(('extraction', 'repair'), extraction_prompts(2), strict=True)) | {'candidate_template': TEMPLATE}
    if _read(root/'prompts.json') != expected_prompts or env['prompts'] != {k: digest(v.encode()) for k, v in expected_prompts.items()}:
        raise ValueError('prompt manifest')
    if env['dataset_manifest'] != load_manifest(root/'fixtures') or env['dataset_manifest'] != load_manifest(DATA_ROOT/'v2'):
        raise ValueError('fixture manifest')
    for name in ('short',):
        case = load_case(name, root/'fixtures')
        if case != load_case(name, DATA_ROOT/'v2') or env['cases'][name] != sha(case): raise ValueError('fixture contract')
    if set(env['source_hashes']) != set(_sources()) or set(file_index(root/'implementation')) != set(_sources()): raise ValueError('source inventory')
    for p, value in env['source_hashes'].items():
        if _hash(root/'implementation'/p) != value or _hash(REPO/p) != value: raise ValueError('source identity')


def evaluate(root):
    """Never use HTTP, subprocesses, writable Store, or retained Python execution."""
    root = Path(root).resolve()
    fallback = {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed'],
                'scope': 'one finite short fixture; not general semantic fidelity or operational acceptance'}
    try:
        retained = _read(root/'index.json', MAX_REPORT_BYTES)
        if not isinstance(retained, dict) or retained != file_index(root, excluded=('index.json', 'evaluation.json')):
            raise ValueError('artifact index')
        report = _read(root/'report.json', MAX_REPORT_BYTES); _verify_manifest(root, report)
        if len(report['documents']) != 1: raise ValueError('single document required')
        row = report['documents'][0]; expected = plan()['documents'][0]
        if any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in expected.items()): raise ValueError('schedule')
        if row['artifact'] != 'documents/short-1' or row['attempt_start'] != 1: raise ValueError('document linkage')
        artifact = root/row['artifact']; attempts = report['attempts']; budget = report['budget']
        if artifact.is_symlink() or (root/'documents').is_symlink(): raise ValueError('document symlink')
        current = file_index(artifact)
        if (row['artifact_files'] != current or current != {p[len(row['artifact'])+1:]: v for p, v in retained.items() if p.startswith(row['artifact']+'/')}
                or {p.name for p in (root/'documents').iterdir()} != {'short-1'}): raise ValueError('document index')
        if (type(row['attempt_count']) is not int or row['attempt_count'] != len(attempts) or not 1 <= len(attempts) <= 2
                or any(a['document_key'] != row['key'] or a['attempt'] != i+1 for i, a in enumerate(attempts))):
            raise ValueError('attempt sequence')
        safety = verify_document(artifact, row, attempts, load_case('short', root/'fixtures'))
        if safety != row['safety']: raise ValueError('safety mismatch')
        if (type(row['seconds']) not in (int, float) or not math.isfinite(row['seconds']) or row['seconds'] < 0
                or budget['requests'] != len(attempts) or budget['finished'] != len(attempts) or budget['pending'] != 0
                or any(a['status'] not in ('success', 'error') for a in attempts)
                or budget['observed_tokens'] != sum(a.get(k) or 0 for a in attempts for k in ('input_tokens', 'output_tokens'))
                or budget['unknown_usage_calls'] != sum(a.get('input_tokens') is None or a.get('output_tokens') is None for a in attempts)
                or type(budget['elapsed_seconds']) not in (int, float) or not math.isfinite(budget['elapsed_seconds'])
                or budget['elapsed_seconds'] < 0 or budget['token_hard_cap_guaranteed'] is not False
                or budget['reported_usd'] is not None or budget['cache_tokens'] is not None
                or report['status'] not in ('completed', 'stopped')
                or report['status'] == 'completed' and (report['stop_reason'] is not None
                    or budget['elapsed_seconds'] >= LIMITS['seconds'] or budget['observed_tokens'] >= LIMITS['tokens'])):
            raise ValueError('budget ledger')
        return _decision(report, row, safety)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error):
        return fallback



def _decision(report, row, safety):
    """Classify already verified evidence using the terminal execution snapshot."""
    attempts = report['attempts']; budget = report['budget']
    known = all(a.get('input_tokens') is not None and a.get('output_tokens') is not None
                and a.get('resolved_model') == MODEL and a.get('model_digest') == MODEL_DIGEST
                and a.get('identity_after_verified') is True and a.get('response_complete') is True for a in attempts)
    production = safety['production']; quality = row['quality']; reasons = []
    if not safety['passed']: reasons.append('publication_safety_failed')
    if production['complete'] and production['passed'] is False: reasons.append('production_contract_failed')
    if quality['complete'] and quality['passed'] is False: reasons.append('finite_gold_failed')
    admissible = (report['stop_reason'] in (None, 'document_failed', 'call_failed')
                  and budget['elapsed_seconds'] < LIMITS['seconds']
                  and budget['observed_tokens'] < LIMITS['tokens'])
    rejected = bool(reasons) and known and admissible
    completed = (known and production['complete'] and production['passed'] is True
                 and safety['passed'] and quality['complete'] and quality['passed'] and row['status'] == 'success'
                 and report['status'] == 'completed' and report['stop_reason'] is None)
    verdict = 'rejected' if rejected else 'accepted_for_further_testing' if completed else 'inconclusive'
    if not known: reasons.append('usage_identity_or_response_unknown')
    if not rejected and not completed: reasons.append('incomplete_trial')
    if report['synthetic_responses']: verdict = 'inconclusive'; reasons.append('synthetic_only')
    return {'verdict': verdict, 'reasons': reasons, 'production': production, 'quality': quality, 'safety': safety,
            'observed_tokens': budget['observed_tokens'], 'requests': len(attempts), 'seconds': row['seconds'],
            'reported_usd': None, 'cache_tokens': None, 'candidate_id': CANDIDATE_ID,
            'scope': 'one finite short fixture; not general semantic fidelity or operational acceptance'}
