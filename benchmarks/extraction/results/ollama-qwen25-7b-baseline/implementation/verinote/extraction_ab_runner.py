"""Durable synthetic A/B runs; only run_experiment can invoke a provider."""
import copy
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

from .agents import CLI, validate_model
from .extraction_ab import (
    _attempts,
    _known,
    evaluate_report,
    experiment_plan,
    validate_lifecycle,
)
from .extraction_benchmark import (
    MAX_REPORT_BYTES,
    Budget,
    BudgetCaller,
    ReportWriter,
    Stopped,
    call_metrics,
    execute_document,
    quality_report,
    resolved_model,
    sha,
)
from .extraction_evaluation import _read, load_case
from .extraction_timing import read_extraction_timing
from .extraction_transport import ExtractionSpec, ExtractionTransport
from .ontology import (
    EXTRACTION_CONTRACT_VERSION,
    EXTRACTION_PROMPT,
    QUOTE_REPAIR_PROMPT,
    extraction_prompts,
    extraction_version_from_hashes,
)
from .store import Store, canonical, digest
from .usage import atomic_json, decode_transport


def _hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): result.update(block)
    return result.hexdigest()


def file_index(root, *, excluded=()):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink(): raise ValueError('artifact symlink')
        if path.is_file() and relative not in excluded:
            result[relative] = {'bytes': path.stat().st_size, 'sha256': _hash(path)}
    return result


def _command(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def capture_environment(model):
    repo = Path(__file__).resolve().parents[1]
    if _command(['git', 'status', '--porcelain'], repo): raise ValueError('실측에는 clean checkout이 필요합니다.')
    spec = ExtractionSpec.capture(CLI(timeout=90, persistent=False), model, 1)
    version = _command([spec.binary, '--version'])
    match = re.fullmatch(r'([0-9]+\.[0-9]+\.[0-9]+)(?: \(Claude Code\))?', version)
    if not match: raise ValueError('Claude CLI 버전을 확인할 수 없습니다.')
    source_files = sorted([*repo.glob('verinote/*.py'), repo/'scripts/benchmark_extraction_ab.py',
                           repo/'docs/extraction-sonnet-baseline-completed.md'])
    return {'revision': _command(['git', 'rev-parse', 'HEAD'], repo),
            'tree': _command(['git', 'rev-parse', 'HEAD^{tree}'], repo),
            'cli_version': match[1], 'cli_version_source': 'cli_version_command',
            'python': platform.python_version(), 'platform': platform.platform(),
            'requested_model': model, 'extraction_contract': EXTRACTION_CONTRACT_VERSION, 'transport': spec.to_dict(), 'provider_cache_state': 'unknown',
            'contract_sha256': _hash(repo/'docs/extraction-sonnet-baseline-completed.md'), 'plan_sha256': sha(experiment_plan()),
            'dictionary_sha256': {key: digest(load_case(key)['dictionary']) for key in ('short', 'long', 'dense', 'none', 'boundary')},
            'source_hashes': {p.relative_to(repo).as_posix(): _hash(p) for p in source_files},
            'prompt_sha256': digest(EXTRACTION_PROMPT.encode()), 'repair_prompt_sha256': digest(QUOTE_REPAIR_PROMPT.encode()),
            'cases': {key: sha(load_case(key)) for key in ('short', 'long', 'dense', 'none', 'boundary')}}


def run_experiment(output, *, model, limits, owner_factory=None, clock=None, invocation=None):
    """Private factory/clock seams are offline-only and mark the evidence synthetic."""
    model = validate_model(model, 'claude')
    if not model or limits.repetitions != 1 or limits.missing_usage != 'stop' or limits.requests > 274:
        raise ValueError('A/B 모델·예산 설정 오류')
    budget = Budget(limits, clock=clock)
    synthetic = owner_factory is not None
    environment = ({'requested_model': model, 'python': platform.python_version(), 'synthetic': True,
                    'extraction_contract': EXTRACTION_CONTRACT_VERSION}
                   if synthetic else capture_environment(model))
    environment['invocation'] = list(invocation) if invocation is not None else ['python_api', 'run_experiment']
    writer = ReportWriter(output); root = writer.folder
    plan = experiment_plan()
    if owner_factory is None:
        spec = ExtractionSpec.from_dict(environment['transport'])
        def owner_factory(capacity): return ExtractionTransport(replace(spec, capacity=capacity))
    report = {'schema_version': 1, 'plan': plan, 'environment': environment, 'synthetic_responses': synthetic,
              'limits': vars(limits), 'status': 'running', 'stop_reason': None, 'documents': [],
              'attempts': budget.attempts, 'budget': budget.summary(), 'cleanup_errors': 0,
              'cold_boundary': 'before_store_and_owner_through_evaluation_and_owner_close'}
    atomic_json(root/'plan.json', plan); atomic_json(root/'environment.json', environment)
    def save():
        with budget.lock:
            report['budget'] = budget.summary()
            try: writer.write(copy.deepcopy(report))
            except (OSError, ValueError): budget.stop('report_write_failed'); raise Stopped('report_write_failed') from None
    def close(resource):
        if resource is None: return True
        try:
            resource.close()
            return True
        except Exception:
            report['cleanup_errors'] += 1; budget.stop('cleanup_error')
            return False
    save()
    warmed: dict[tuple, tuple] = {}; models: set[str] = set()
    try:
        for series in plan['series']:
            owners = {}
            try:
                for document in (d for d in plan['documents'] if d['phase']==series['phase'] and d['case_id']==series['case_id']):
                    budget.check()
                    row = dict(document, status='running', quality=None, document_timing=None, cold_seconds=None, cold_cleanup_ok=None,
                               attempt_start=len(budget.attempts)+1, attempt_count=0, artifact=f'documents/{document["key"]}')
                    report['documents'].append(row); save()
                    started = budget.clock(); row['elapsed_start'] = started; owner = None
                    artifact = root/row['artifact']
                    try:
                        if document['owner_id'] not in owners:
                            owners[document['owner_id']] = owner_factory(document['concurrency'])
                        owner = owners[document['owner_id']]
                        view = owner.slot_view(document['slot']) if document['purpose']=='warmup' else owner.view()
                        caller = BudgetCaller(view, budget, save, purpose=document['purpose'], identity={
                            'owner_id': document['owner_id'], 'case_id': document['source_case'],
                            'repetition': document['pair'] or document['slot'], 'document_key': document['key']})
                        artifact.mkdir(parents=True, exist_ok=False)
                        execute_document(artifact, load_case(document['source_case']), lambda caller=caller: caller, budget, row, close,
                                         agent='claude', model=model, extraction_concurrency=document['concurrency'])
                    finally:
                        if document['phase']=='cold':
                            row['cold_cleanup_ok'] = owner is not None and close(owner)
                            if row['cold_cleanup_ok']: owners.pop(document['owner_id'], None)
                        # Frozen cold endpoint: evaluation and owner termination, before evidence hashing.
                        row['elapsed_end'] = budget.clock()
                        row['elapsed_seconds'] = max(0, row['elapsed_end']-started)
                        if document['phase']=='cold' and row['cold_cleanup_ok']:
                            row['cold_seconds'] = row['elapsed_seconds']
                        row['attempt_count'] = len(budget.attempts)-row['attempt_start']+1
                        row['artifact_files'] = file_index(artifact) if artifact.exists() else {}
                        save()
                    if row['status'] != 'success' or not (row['quality'] or {}).get('passed'):
                        budget.stop(budget.reason or 'document_failed'); raise Stopped('document_failed')
                    values = budget.attempts[row['attempt_start']-1:row['attempt_start']-1+row['attempt_count']]
                    if not _known(values):
                        budget.stop('comparison_evidence_unknown'); raise Stopped('comparison_evidence_unknown')
                    models.update(v['resolved_model'] for v in values)
                    if len(models) != 1:
                        budget.stop('model_changed'); raise Stopped('model_changed')
                    try: validate_lifecycle(row, values, warmed)
                    except (ValueError, KeyError, TypeError):
                        budget.stop('worker_lifecycle_changed'); raise Stopped('worker_lifecycle_changed') from None
            finally:
                for owner in owners.values(): close(owner)
    except Stopped: pass
    except KeyboardInterrupt: budget.stop('interrupted')
    except Exception: budget.stop('runner_error')
    finally:
        if budget.remaining() <= 0: budget.stop('time_limit')
        if not synthetic:
            repo = Path(__file__).resolve().parents[1]
            if any(not (repo/p).is_file() or _hash(repo/p) != value for p, value in environment['source_hashes'].items()):
                budget.stop('source_changed')
        report['status'] = 'stopped' if budget.reason else 'completed'
        report['stop_reason'] = budget.reason
        save()
        atomic_json(root/'index.json', file_index(root, excluded=('index.json', 'evaluation.json')))
    evaluation = evaluate_saved(root)
    atomic_json(root/'evaluation.json', evaluation)
    return report, evaluation


def evaluate_saved(root):
    """No subprocesses/model calls; verify raw linkage before computing the verdict."""
    root = Path(root).resolve()
    verified_violations: list[str] = []
    try:
        index = _read(root/'index.json')
        if index != file_index(root, excluded=('index.json', 'evaluation.json')): raise ValueError('artifact_hash_mismatch')
        report = _read(root/'report.json', MAX_REPORT_BYTES)
        if (sha(report['plan']) != sha(experiment_plan()) or report['plan'] != _read(root/'plan.json')
                or report['environment'] != _read(root/'environment.json')):
            raise ValueError('metadata_mismatch')
        if report.get('cold_boundary') != 'before_store_and_owner_through_evaluation_and_owner_close':
            raise ValueError('cold_boundary_contract')
        environment = report['environment']
        contract_version = environment.get('extraction_contract', 1)
        if type(contract_version) is not int or contract_version not in (1, 2):
            raise ValueError('extraction_contract_version')
        extraction_prompts(contract_version)
        if not report.get('synthetic_responses'):
            contract_version = extraction_version_from_hashes(
                environment.get('prompt_sha256'), environment.get('repair_prompt_sha256'),
                environment.get('extraction_contract'))
            if (environment.get('synthetic') or environment.get('cli_version_source') != 'cli_version_command'
                    or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', environment.get('cli_version', ''))
                    or not all(re.fullmatch(r'[a-f0-9]{40}', environment.get(k, '')) for k in ('revision', 'tree'))
                    or environment.get('plan_sha256') != sha(experiment_plan())
                    or environment.get('contract_sha256') != _hash(Path(__file__).resolve().parents[1]/'docs/extraction-sonnet-baseline-completed.md')
                    or environment.get('dictionary_sha256') != {key: digest(load_case(key)['dictionary']) for key in ('short', 'long', 'dense', 'none', 'boundary')}
                    or environment.get('cases') != {key: sha(load_case(key)) for key in ('short', 'long', 'dense', 'none', 'boundary')}):
                raise ValueError('environment_contract')
            spec = ExtractionSpec.from_dict(environment['transport'])
            if spec.timeout != 90 or spec.model != environment['requested_model']: raise ValueError('transport_contract')
        for ordinal, row in enumerate(report['documents']):
            expected = report['plan']['documents'][ordinal]
            if any(type(row.get(k)) is not type(value) or row.get(k) != value for k, value in expected.items()):
                raise ValueError('planned_document_identity')
            values = _attempts(row, report['attempts'])
            row_violations = []
            if (row['elapsed_end'] < row['elapsed_start'] or row['elapsed_seconds'] != row['elapsed_end']-row['elapsed_start']
                    or (row['phase']=='cold' and (type(row.get('cold_cleanup_ok')) is not bool
                        or row['cold_seconds'] != (row['elapsed_seconds'] if row['cold_cleanup_ok'] else None)))):
                raise ValueError('measurement_boundary')
            if row['artifact'] != 'documents/'+row['key'] or not re.fullmatch(r'[a-z0-9_-]{1,80}', row['key']):
                raise ValueError('artifact_path')
            artifact = root/row['artifact']
            if row['artifact_files'] != file_index(artifact): raise ValueError('document_hash_mismatch')
            if row.get('run_id') is None: continue
            store = Store.__new__(Store); store.path = str(artifact/'state.sqlite3')
            run = store.run(row['run_id']); case = load_case(row['source_case'])
            doc = store.document(run['document_id'], run['version'])
            options = run['extraction_options']
            if (doc['text'] != case['source'] or run['model'] != report['environment']['requested_model']
                    or type(run['ontology'].get('extraction_contract')) is not int
                    or run['ontology']['extraction_contract'] != contract_version
                    or options['concurrency'] != row['concurrency'] or run['version'] != 1
                    or run['ontology']['sha256'] != digest(case['dictionary'])):
                raise ValueError('document_contract')
            if not report.get('synthetic_responses') and options['transport'] != replace(spec, capacity=row['concurrency']).to_dict():
                raise ValueError('frozen_transport_mismatch')
            candidates = [i for i in doc['items'] if i['kind'] != 'ontology']
            published = bool(candidates) or run['status']=='success'
            if doc['current_version'] != 1 or doc['generation'] != 1:
                if published: row_violations.append('publication_contract')
                else: raise ValueError('document_changed_without_publication')
            dictionaries = [i for i in doc['items'] if i['kind']=='ontology']
            dictionary_current = (len(dictionaries)==1 and dictionaries[0]['id']==run['ontology']['id']
                                  and dictionaries[0]['revision']==run['ontology']['revision']
                                  and digest(dictionaries[0]['payload'])==run['ontology']['sha256']
                                  and dictionaries[0]['status']=='approved')
            if not dictionary_current:
                if published: row_violations.append('dictionary_publication_contract')
                else: raise ValueError('dictionary_changed_without_publication')
            if any(i['status'] != 'pending' for i in candidates):
                row_violations.append('human_approval_contract')
            if row['status'] != run['status'] or row['quality'] != quality_report(case, artifact, run): raise ValueError('quality_mismatch')
            if run['status'] == 'success':
                checkpoint = _read(artifact/'runs'/run['id']/'extraction-checkpoint.json')
                if checkpoint['contract']['extraction_options'] != options: raise ValueError('checkpoint_contract')
                expected_facts = {canonical(f) for output in checkpoint['outputs'] for f in output['facts']}
                actual_facts = {canonical(i['payload']) for i in candidates if i['kind']=='fact'}
                pages = [i for i in candidates if i['kind']=='page']
                expected_claims = [c for output in checkpoint['outputs'] for c in output['claims']]
                if (expected_facts != actual_facts or len(pages) != 1 or pages[0]['payload']['claims'] != expected_claims
                        or any(i['run_id'] != run['id'] for i in candidates)):
                    row_violations.append('candidate_publication')
            elif candidates: row_violations.append('partial_publication')
            timing = read_extraction_timing(artifact, store, run['id'])
            expected_timing = ({'available': True, 'summary': timing['summary'], 'status': timing['status'], 'error_code': timing['error_code']}
                               if timing['available'] else {'available': False})
            if row['document_timing'] != expected_timing: raise ValueError('timing_mismatch')
            expected_usage = set()
            for attempt in values:
                if attempt['admission_state'] != 'finished': continue
                folder = artifact/'runs'/run['id']/str(attempt['chunk'])
                if attempt['repair']: folder /= 'quote-repair'
                expected_usage.add((folder/'usage.json').relative_to(artifact).as_posix())
                metrics = call_metrics(folder, 'claude', run['model'])
                if any(attempt.get(k) != v for k, v in metrics.items()): raise ValueError('call_metrics_mismatch')
                if metrics['usage_complete']:
                    _, parsed, _ = decode_transport('claude', json.dumps(_read(folder/'stdout.txt')))
                    if (parsed['tokens'] != metrics['tokens'] or parsed['reported_usd'] != metrics['reported_usd']
                            or resolved_model(parsed['resolved_model']) != metrics['resolved_model']):
                        raise ValueError('provider_usage_mismatch')
                observation = 'unknown'; timing = metrics['cli_timing']; identity = metrics['worker_identity']
                if timing and timing['schema_version']==2 and identity and metrics['worker_scope']=='extraction_pool':
                    if timing['process_reused'] is True: observation = 'reused'
                    elif timing['process_reused'] is False and timing['offsets_seconds']['spawn_returned'] is not None:
                        observation = 'started' if identity['generation']==1 else 'restarted'
                if attempt.get('process_observation') != observation: raise ValueError('lifecycle_mismatch')
                meta = _read(folder/'run.json')
                if attempt['status'] != ('success' if meta.get('status')=='success' else 'error'): raise ValueError('call_status_mismatch')
            if {p.relative_to(artifact).as_posix() for p in artifact.rglob('usage.json')} != expected_usage:
                raise ValueError('unaccounted_calls')
            # A DB state is confirmed only after this document's summary, timing and calls agree with raw evidence.
            verified_violations.extend(reason+':'+row['key'] for reason in row_violations)
        result = evaluate_report(report)
        if verified_violations:
            result.update(verdict='rejected', reasons=verified_violations+result['reasons'])
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error):
        if verified_violations:
            return {'verdict': 'rejected', 'reasons': verified_violations, 'comparisons': [],
                    'synthetic_responses': bool(report.get('synthetic_responses')), 'unverified_other_records': True}
        return {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed'], 'comparisons': []}
