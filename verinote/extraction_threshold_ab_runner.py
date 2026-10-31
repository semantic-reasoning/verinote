"""Durable v2 threshold experiment. Only run_experiment may invoke a model."""
import copy
import math
import platform
import re
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

from .agents import CLI
from .extraction_ab import _identity, _known
from .extraction_ab_runner import _command, _hash, file_index
from .extraction_benchmark import (
    MAX_REPORT_BYTES,
    Budget,
    BudgetCaller,
    Limits,
    ReportWriter,
    Stopped,
    execute_document,
    sha,
)
from .extraction_evaluation import DATA_ROOT, _read, load_case, load_manifest
from .extraction_threshold_ab import (
    evaluate_report,
    experiment_plan,
    resolved_sonnet_model,
    sonnet_model,
)
from .extraction_threshold_artifacts import verify_document
from .extraction_transport import ExtractionSpec, ExtractionTransport
from .ontology import extraction_prompts
from .store import digest
from .usage import atomic_json

CASES = ('short', 'three', 'four', 'eight')
CONTRACT_PATH = 'docs/extraction-chunk-threshold-ab-contract.md'
COLD_BOUNDARY = 'before_store_and_owner_through_raw_quality_safety_evaluation_and_owner_close'


def cases():
    return {key: load_case(key, DATA_ROOT/'v2') for key in CASES}


class EvidenceBudget(Budget):
    """Stop admission on unknown cost/model or changed identity at call completion."""
    def __init__(self, limits, plan, *, clock=None):
        super().__init__(limits, clock=clock)
        self.documents = {d['key']: d for d in plan['documents']}
        self.ready = False
        self.models: set[str] = set()
        self.slots: dict[tuple, tuple] = {}
        self.workers: dict[str, tuple] = {}

    def reserve(self, *args, **kwargs):
        if not self.ready: self.stop('manifest_unverified'); raise Stopped('manifest_unverified')
        return super().reserve(*args, **kwargs)

    def finish(self, row, usage, **kwargs):
        with self.lock:
            super().finish(row, usage, **kwargs)
            if row['status'] != 'success': self.stop('call_failed')
            if not _known([row]): self.stop('comparison_evidence_unknown'); return
            if not resolved_sonnet_model(row['resolved_model']): self.stop('model_changed'); return
            self.models.add(row['resolved_model'])
            if len(self.models) != 1: self.stop('model_changed')
            try:
                document = self.documents[row['document_key']]
                identity = _identity(row); slot = (row['owner_id'], identity[1])
                previous = self.slots.get(slot)
                if identity[1] > document['concurrency'] or identity[3] != 1: raise ValueError('identity')
                if document['purpose'] == 'warmup' and identity[1] != document['slot']: raise ValueError('warmup_slot')
                if document['phase'] == 'warm' and document['purpose'] == 'measurement' and previous is None:
                    raise ValueError('unprepared_slot')
                if (previous is not None and previous != identity
                        or row['process_observation'] != ('reused' if previous is not None else 'started')):
                    raise ValueError('lifecycle')
                if identity[2] in self.workers and self.workers[identity[2]] != slot: raise ValueError('shared_worker')
                self.workers[identity[2]] = slot; self.slots[slot] = identity
            except (ValueError, KeyError, TypeError): self.stop('worker_lifecycle_changed')


def capture_environment(model, *, synthetic=False):
    repo = Path(__file__).resolve().parents[1]
    if not synthetic and _command(['git', 'status', '--porcelain'], repo):
        raise ValueError('real experiment requires a clean checkout')
    spec = ExtractionSpec.capture(CLI(timeout=90, persistent=False), model, 1)
    prompt, repair = extraction_prompts(2)
    source_paths = sorted([*repo.glob('verinote/*.py'), repo/'scripts/benchmark_extraction_threshold_ab.py',
                           repo/CONTRACT_PATH, repo/'docs/extraction-threshold-fixture-review.md',
                           repo/'docs/extraction-threshold-decision.md'])
    data = cases()
    environment = {'schema_version': 2, 'synthetic': synthetic, 'requested_model': model,
        'python': platform.python_version(), 'platform': platform.platform(), 'transport': spec.to_dict(),
        'dataset_version': 2, 'extraction_contract': 2, 'contract_version': 2, 'provider_cache_state': 'unknown',
        'contract_sha256': _hash(repo/CONTRACT_PATH), 'plan_sha256': sha(experiment_plan()),
        'prompt_sha256': digest(prompt.encode()), 'repair_prompt_sha256': digest(repair.encode()),
        'cases': {key: sha(value) for key, value in data.items()},
        'source_sha256': {key: value['source_sha256'] for key, value in data.items()},
        'dictionary_sha256': {key: digest(value['dictionary']) for key, value in data.items()},
        'gold_sha256': {key: sha({'gold': value['gold'], 'forbidden': value['forbidden'], 'annotations': value['annotations']}) for key, value in data.items()},
        'dataset_manifest': load_manifest(DATA_ROOT/'v2'),
        'source_hashes': {p.relative_to(repo).as_posix(): _hash(p) for p in source_paths}}
    if synthetic:
        environment.update(revision=None, tree=None, cli_version=None, cli_version_source='synthetic_no_probe')
    else:
        version = _command([spec.binary, '--version'])
        match = re.fullmatch(r'([0-9]+\.[0-9]+\.[0-9]+)(?: \(Claude Code\))?', version)
        if not match: raise ValueError('cannot verify Claude CLI version')
        environment.update(revision=_command(['git', 'rev-parse', 'HEAD'], repo),
                           tree=_command(['git', 'rev-parse', 'HEAD^{tree}'], repo),
                           cli_version=match[1], cli_version_source='cli_version_command')
    return environment


def verify_manifest(root, environment, plan, limits):
    """All frozen artifacts must exist before Budget can admit its first call."""
    if (plan != experiment_plan() or _read(root/'plan.json') != plan
            or _read(root/'environment.json') != environment or _read(root/'limits.json') != limits):
        raise ValueError('manifest_metadata')
    checked_limits = Limits(**limits)
    if checked_limits.repetitions != 1 or checked_limits.missing_usage != 'stop' or checked_limits.requests > 856:
        raise ValueError('manifest_limits')
    prompt, repair = extraction_prompts(2)
    if (environment.get('dataset_version') != 2 or environment.get('extraction_contract') != 2
            or environment.get('contract_version') != 2 or environment.get('schema_version') != 2
            or environment.get('plan_sha256') != sha(plan)
            or environment.get('prompt_sha256') != digest(prompt.encode())
            or environment.get('repair_prompt_sha256') != digest(repair.encode())
            or _read(root/'prompts.json') != {'extraction': prompt, 'repair': repair}
            or environment.get('contract_sha256') != _hash(Path(__file__).resolve().parents[1]/CONTRACT_PATH)):
        raise ValueError('manifest_contract')
    if not isinstance(environment.get('invocation'), list) or not environment['invocation'] or any(not isinstance(a, str) for a in environment['invocation']):
        raise ValueError('manifest_invocation')
    spec = ExtractionSpec.from_dict(environment['transport'])
    if spec.timeout != 90 or spec.capacity != 1 or spec.model != sonnet_model(environment['requested_model']):
        raise ValueError('manifest_transport')
    if type(environment.get('synthetic')) is not bool: raise ValueError('manifest_synthetic_flag')
    if not environment['synthetic']:
        if checked_limits.requests < 608: raise ValueError('manifest_request_budget')
        if (environment.get('cli_version_source') != 'cli_version_command'
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', environment.get('cli_version', ''))
                or not all(re.fullmatch(r'[a-f0-9]{40}', environment.get(k, '')) for k in ('revision', 'tree'))):
            raise ValueError('manifest_environment')
    if environment['dataset_manifest'] != load_manifest(root/'fixtures'): raise ValueError('manifest_dataset')
    for key, expected in cases().items():
        case = load_case(key, root/'fixtures')
        if (case != expected or environment['cases'][key] != sha(case)
                or environment['source_sha256'][key] != case['source_sha256']
                or environment['dictionary_sha256'][key] != digest(case['dictionary'])
                or environment['gold_sha256'][key] != sha({'gold': case['gold'], 'forbidden': case['forbidden'], 'annotations': case['annotations']})):
            raise ValueError('manifest_fixture')
    source_hashes = environment['source_hashes']
    if not isinstance(source_hashes, dict) or not source_hashes: raise ValueError('manifest_source')
    repo = Path(__file__).resolve().parents[1]
    required = {p.relative_to(repo).as_posix() for p in repo.glob('verinote/*.py')} | {
        'scripts/benchmark_extraction_threshold_ab.py', CONTRACT_PATH,
        'docs/extraction-threshold-fixture-review.md', 'docs/extraction-threshold-decision.md'}
    if set(source_hashes) != required or set(file_index(root/'implementation')) != required:
        raise ValueError('manifest_source_completeness')
    if source_hashes[CONTRACT_PATH] != environment['contract_sha256']: raise ValueError('manifest_contract_linkage')
    for relative, digest_value in source_hashes.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in ('verinote', 'scripts', 'docs'):
            raise ValueError('manifest_source_path')
        if _hash(root/'implementation'/path) != digest_value: raise ValueError('manifest_source_hash')
    return spec


def run_experiment(output, *, model, limits, owner_factory=None, clock=None, invocation=None):
    """A private owner factory marks all evidence synthetic and never claims adoption."""
    model = sonnet_model(model); synthetic = owner_factory is not None; plan = experiment_plan()
    if (limits.repetitions != 1 or limits.missing_usage != 'stop' or limits.requests > 856
            or not synthetic and limits.requests < 608): raise ValueError('threshold budget contract')
    budget = EvidenceBudget(limits, plan, clock=clock)
    environment = capture_environment(model, synthetic=synthetic)
    environment['invocation'] = list(invocation) if invocation is not None else ['python_api', 'run_experiment']
    writer = ReportWriter(output); root = writer.folder; repo = Path(__file__).resolve().parents[1]
    shutil.copytree(DATA_ROOT/'v2', root/'fixtures')
    for relative in environment['source_hashes']:
        destination = root/'implementation'/relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo/relative, destination)
    prompt, repair = extraction_prompts(2)
    atomic_json(root/'prompts.json', {'extraction': prompt, 'repair': repair})
    atomic_json(root/'plan.json', plan); atomic_json(root/'environment.json', environment); atomic_json(root/'limits.json', vars(limits))
    spec = verify_manifest(root, environment, plan, vars(limits))
    budget.ready = True
    if owner_factory is None: owner_factory = ExtractionTransport
    report = {'schema_version': 2, 'plan': plan, 'environment': environment, 'synthetic_responses': synthetic,
              'limits': vars(limits), 'status': 'running', 'stop_reason': None, 'documents': [],
              'attempts': budget.attempts, 'budget': budget.summary(), 'cleanup_errors': 0, 'cold_boundary': COLD_BOUNDARY}
    def save():
        with budget.lock:
            report['budget'] = budget.summary()
            try: writer.write(copy.deepcopy(report))
            except (OSError, ValueError): budget.stop('report_write_failed'); raise Stopped('report_write_failed') from None
    def close(resource):
        if resource is None: return True
        try: resource.close(); return True
        except Exception:
            report['cleanup_errors'] += 1; budget.stop('cleanup_error'); return False
    save()
    try:
        for series in plan['series']:
            owners: dict[int, ExtractionTransport] = {}
            try:
                for document in (d for d in plan['documents'] if d['phase'] == series['phase'] and d['case_id'] == series['case_id']):
                    budget.check()
                    row = dict(document, status='running', quality=None, safety=None, document_timing=None,
                               cold_seconds=None, cleanup_succeeded=None, attempt_start=len(budget.attempts)+1,
                               attempt_count=0, artifact=f'documents/{document["key"]}')
                    report['documents'].append(row); save()
                    started = budget.clock(); row['elapsed_start'] = started
                    artifact = root/row['artifact']; artifact.mkdir(parents=True, exist_ok=False)
                    desired = replace(spec, capacity=document['concurrency'])
                    def make_caller(registered=None, document=document, desired=desired, owners=owners):
                        if document['extraction_policy'] is not None and registered != desired:
                            raise ValueError('registered transport differs from manifest')
                        if document['owner_id'] not in owners:
                            budget.check(); owners[document['owner_id']] = owner_factory(desired)
                        owner = owners[document['owner_id']]
                        if owner.identity != desired: raise ValueError('owner transport mismatch')
                        view = owner.slot_view(document['slot']) if document['purpose'] == 'warmup' else owner.view()
                        return BudgetCaller(view, budget, save, purpose=document['purpose'], identity={
                            'owner_id': document['owner_id'], 'case_id': document['source_case'],
                            'repetition': document['pair'] or document['slot'], 'document_key': document['key']})
                    try:
                        registration_cli = CLI(timeout=90, max_output=spec.max_output, persistent=False,
                                               rates=lambda: {'claude': environment['transport']['rate']})
                        execute_document(artifact, load_case(document['source_case'], root/'fixtures'), make_caller, budget, row, close,
                            agent='claude', model=model, extraction_concurrency=document['concurrency'] if document['extraction_policy'] is None else None,
                            extraction_policy=document['extraction_policy'], registration_cli=registration_cli)
                    finally:
                        row['attempt_count'] = len(budget.attempts)-row['attempt_start']+1
                        try:
                            row['safety'] = verify_document(artifact, row, budget.attempts, load_case(document['source_case'], root/'fixtures'), model, desired)
                        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                            row['safety'] = {'complete': False, 'passed': None, 'violations': []}
                            budget.stop('document_evidence_unknown')
                        if document['phase'] == 'cold':
                            owner = owners.get(document['owner_id'])
                            row['cleanup_succeeded'] = owner is not None and close(owner)
                            if row['cleanup_succeeded']: owners.pop(document['owner_id'], None)
                        row['elapsed_end'] = budget.clock(); row['elapsed_seconds'] = max(0, row['elapsed_end']-started)
                        if document['phase'] == 'cold' and row['cleanup_succeeded']: row['cold_seconds'] = row['elapsed_seconds']
                        row['artifact_files'] = file_index(artifact)
                        save()
                    if (row['status'] != 'success' or not (row['quality'] or {}).get('passed')
                            or not (row['safety'] or {}).get('passed')):
                        budget.stop(budget.reason or 'document_failed'); raise Stopped('document_failed')
            finally:
                for owner in owners.values(): close(owner)
    except Stopped: pass
    except KeyboardInterrupt: budget.stop('interrupted')
    except Exception: budget.stop('runner_error')
    finally:
        if any(not (repo/p).is_file() or _hash(repo/p) != value for p, value in environment['source_hashes'].items()):
            budget.stop('source_changed')
        if budget.remaining() <= 0: budget.stop('time_limit')
        report['status'] = 'stopped' if budget.reason else 'completed'; report['stop_reason'] = budget.reason
        # Online raw validation is complete before each cold endpoint. The independent
        # evaluate command can repeat it later without spending a model-call budget.
        evaluation = evaluate_report(report)
        atomic_json(root/'evaluation.json', evaluation)
        index = file_index(root, excluded=('report.json', 'index.json', 'evaluation.json'))
        if budget.remaining() <= 0:
            budget.stop('time_limit'); report.update(status='stopped', stop_reason=budget.reason)
            evaluation = evaluate_report(report); atomic_json(root/'evaluation.json', evaluation)
        save()
        index['report.json'] = {'bytes': (root/'report.json').stat().st_size, 'sha256': _hash(root/'report.json')}
        atomic_json(root/'index.json', index)
    return report, evaluation


def verify_budget(report):
    """Recompute the terminal ledger from the verified provider-linked attempts."""
    limits = Limits(**report['limits']); attempts = report['attempts']; summary = report['budget']
    states = [v['admission_state'] for v in attempts]
    if any(s not in ('finished', 'blocked', 'reserved', 'authorized') for s in states): raise ValueError('budget_states')
    finished = [v for v in attempts if v['admission_state'] == 'finished']
    observed = 0; unknown = 0
    for value in finished:
        counts = [value['tokens'][key] for key in ('input', 'output')]
        if any(v is not None and (type(v) is not int or v < 0) for v in counts): raise ValueError('budget_tokens')
        observed += sum(v for v in counts if v is not None)
        unknown += value['usage_complete'] is not True or any(v is None for v in counts)
    pending = sum(s in ('reserved', 'authorized') for s in states)
    expected = {'requests_started': len(attempts), 'dispatch_authorized': sum(s in ('finished', 'authorized') for s in states),
                'blocked_before_dispatch': states.count('blocked'), 'finished': len(finished), 'pending': pending,
                'observed_tokens_lower_bound': observed, 'unknown_usage_calls': unknown,
                'token_usage_verified': unknown == 0 and pending == 0, 'token_hard_cap_guaranteed': False,
                'stop_reason': report['stop_reason']}
    if any(type(summary.get(k)) is not type(v) or summary.get(k) != v for k, v in expected.items()):
        raise ValueError('budget_summary')
    if (len(attempts) > limits.requests or pending or type(summary.get('elapsed_seconds')) not in (int, float)
            or not math.isfinite(summary['elapsed_seconds']) or summary['elapsed_seconds'] < 0
            or report['status'] == 'completed' and (report['stop_reason'] is not None or observed >= limits.tokens
                                                   or summary['elapsed_seconds'] >= limits.seconds)):
        raise ValueError('budget_limit_or_pending')


def evaluate_saved(root):
    """No subprocesses, model calls or database writes. Ignore the saved verdict."""
    root = Path(root).resolve()
    try:
        retained_index = _read(root/'index.json', MAX_REPORT_BYTES)
        report = _read(root/'report.json', MAX_REPORT_BYTES)
        report_identity = {'bytes': (root/'report.json').stat().st_size, 'sha256': _hash(root/'report.json')}
        if not isinstance(retained_index, dict) or retained_index.get('report.json') != report_identity:
            raise ValueError('report_hash_mismatch')
        try:
            current_index = file_index(root, excluded=('index.json', 'evaluation.json'))
        except (OSError, ValueError):
            current_index = None  # A later unreadable artifact cannot erase intact earlier evidence.
        index_mismatch = retained_index != current_index
        if (report.get('schema_version') != 2 or not isinstance(report.get('documents'), list) or not isinstance(report.get('attempts'), list)
                or report.get('cold_boundary') != COLD_BOUNDARY or report.get('synthetic_responses') is not report['environment']['synthetic']):
            raise ValueError('measurement_contract')
        spec = verify_manifest(root, report['environment'], report['plan'], report['limits'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed'], 'comparisons': []}
    verified = []; next_attempt = 1; invalid = False
    for ordinal, row in enumerate(report['documents']):
        try:
            expected = experiment_plan()['documents'][ordinal]
            if any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in expected.items()): raise ValueError('planned_document')
            if row['artifact'] != 'documents/'+expected['key'] or row['attempt_start'] != next_attempt: raise ValueError('artifact_linkage')
            artifact = root/row['artifact']
            document_index = file_index(artifact)
            retained_document_index = {p[len(row['artifact'])+1:]: value for p, value in retained_index.items()
                                       if p.startswith(row['artifact']+'/')}
            if row['artifact_files'] != document_index or retained_document_index != document_index:
                raise ValueError('document_hash')
            if (any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ('elapsed_start', 'elapsed_end', 'elapsed_seconds'))
                    or row['elapsed_end'] < row['elapsed_start'] or row['elapsed_seconds'] != row['elapsed_end']-row['elapsed_start']
                    or row['phase'] == 'cold' and (type(row['cleanup_succeeded']) is not bool
                        or row['cold_seconds'] != (row['elapsed_seconds'] if row['cleanup_succeeded'] else None))):
                raise ValueError('measurement_boundary')
            safety = verify_document(artifact, row, report['attempts'], load_case(row['source_case'], root/'fixtures'),
                                     report['environment']['requested_model'], replace(spec, capacity=row['concurrency']))
            if safety != row['safety']: raise ValueError('safety_mismatch')
            verified.append(row); next_attempt += row['attempt_count']
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error):
            invalid = True; break
    if invalid:
        # Preserve only the verified prefix, never unverified flags or later summaries.
        report = copy.deepcopy(report)
        report.update(documents=verified, attempts=report['attempts'][:next_attempt-1], status='stopped')
    result = evaluate_report(report)
    if not invalid:
        try:
            verify_budget(report)
            expected_usage = {p.relative_to(root).as_posix() for row in verified for p in (root/row['artifact']).rglob('usage.json')}
            if expected_usage != {p.relative_to(root).as_posix() for p in root.rglob('usage.json')}:
                raise ValueError('global_unaccounted_usage')
            if {p.name for p in (root/'documents').iterdir()} != {row['key'] for row in verified}:
                raise ValueError('unplanned_document_artifact')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            invalid = True
            result['reasons'].append('budget_verification_failed')
    if invalid or index_mismatch:
        result['reasons'].append('artifact_verification_failed'); result['unverified_other_records'] = True
        if result['verdict'] != 'rejected': result['verdict'] = 'inconclusive'
    return result
