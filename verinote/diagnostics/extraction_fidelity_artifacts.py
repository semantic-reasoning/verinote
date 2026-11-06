"""Read-only candidate identity, raw prompt/response and checkpoint verification."""
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..agents import parse_response
from ..documents import chunks
from ..extraction_ab import _identity, _known
from ..extraction_ab_runner import _hash, file_index
from ..extraction_benchmark import MAX_REPORT_BYTES, sha
from ..extraction_checkpoint import CHUNK_TIMEOUT, PIPELINE, ExtractionCheckpoint
from ..extraction_coverage import exclude_off_dictionary_facts, repair_missing_indices
from ..extraction_coverage import identity as coverage_identity
from ..extraction_evaluation import DATA_ROOT, _read, load_case, load_manifest
from ..extraction_threshold_ab_runner import verify_budget
from ..extraction_threshold_artifacts import EvidenceStore, verify_document
from ..extraction_transport import ExtractionSpec
from ..ontology import (
    apply_quote_repairs,
    bounded_prompt,
    dictionary_context,
    extraction_prompts,
    extraction_quote_issues,
)
from ..store import digest
from ..usage import decode_transport
from .extraction_fidelity_candidate import (
    CANDIDATE_ID,
    TEMPLATE,
    compile_prompt,
    template_hash,
)

REPO = Path(__file__).resolve().parents[2]
CONTRACT = 'docs/extraction-fidelity-candidate-contract.md'
SCRIPT = 'scripts/evaluate_extraction_fidelity.py'
MODEL = 'sonnet'
RESOLVED_MODEL = 'claude-sonnet-5-5'
LIMITS: dict[str, Any] = {'requests': 5, 'repetitions': 1, 'seconds': 600, 'tokens': 300000, 'missing_usage': 'stop'}
ROW: dict[str, Any] = {'key': 'three-1', 'case_id': 'three', 'source_case': 'three', 'pair': 1, 'slot': None,
       'purpose': 'measurement', 'owner_id': 1, 'concurrency': 1, 'chunks': 3,
       'extraction_policy': 'chunk_threshold_v1'}


def sources():
    return sorted([*(p.relative_to(REPO).as_posix() for p in (REPO/'verinote').rglob('*.py')), SCRIPT, CONTRACT])


def plan():
    return {'schema_version': 1, 'candidate_id': CANDIDATE_ID, 'template_sha256': template_hash(),
            'documents': [dict(ROW)], 'initial_requests': 3, 'maximum_repairs': 2,
            'limits': dict(LIMITS), 'requested_model': MODEL, 'resolved_model': RESOLVED_MODEL,
            'timeout_seconds': 90, 'automatic_retry': False, 'resume': False,
            'provider_cache_state': 'unknown'}


def prompts():
    return dict(zip(('extraction', 'repair'), extraction_prompts(2), strict=True)) | {'candidate_template': TEMPLATE}


def verify_manifest(root, report):
    env = report['environment']
    if (report['schema_version'] != 1 or report['plan'] != plan() or report['limits'] != LIMITS or
            _read(root/'plan.json') != plan() or _read(root/'environment.json') != env or
            _read(root/'limits.json') != LIMITS or _read(root/'prompts.json') != prompts() or
            type(env['synthetic']) is not bool or report['synthetic_responses'] is not env['synthetic'] or
            env['candidate_id'] != CANDIDATE_ID or env['template_sha256'] != template_hash() or
            env['compiler_sha256'] != _hash(REPO/'verinote/diagnostics/extraction_fidelity_candidate.py') or
            env['prompt_hashes'] != {k: digest(v.encode()) for k, v in prompts().items()}):
        raise ValueError('candidate manifest')
    if (not isinstance(env['invocation'], list) or not env['invocation'] or any(not isinstance(a, str) for a in env['invocation']) or
            not env['synthetic'] and (any(not re.fullmatch(r'[a-f0-9]{40}', env.get(k, '')) for k in ('revision', 'tree')) or
                                     not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', env['cli_version']))):
        raise ValueError('execution provenance')
    expected = set(sources())
    if set(env['source_hashes']) != expected or set(file_index(root/'implementation')) != expected:
        raise ValueError('recursive source inventory')
    for path, value in env['source_hashes'].items():
        if (REPO/path).is_symlink() or _hash(REPO/path) != value or _hash(root/'implementation'/path) != value:
            raise ValueError('source identity')
    if (env['dataset_manifest'] != load_manifest(root/'fixtures') or env['dataset_manifest'] != load_manifest(DATA_ROOT/'v2') or
            load_case('three', root/'fixtures') != load_case('three', DATA_ROOT/'v2') or
            env['case_sha256'] != sha(load_case('three', root/'fixtures'))):
        raise ValueError('fixture identity')
    spec = ExtractionSpec.from_dict(env['transport'])
    if spec.model != MODEL or spec.timeout != 90 or spec.capacity != 1: raise ValueError('transport identity')
    if not env['synthetic'] and (not Path(spec.binary).is_file() or _hash(Path(spec.binary)) != env['binary_sha256']):
        raise ValueError('binary identity')
    return spec


def sidecar(original, effective, *, chunk, repair, run_id, manifest_sha256, call_id=None):
    return {'schema_version': 1, 'candidate_id': CANDIDATE_ID, 'template_sha256': template_hash(),
            'compiler_sha256': _hash(REPO/'verinote/diagnostics/extraction_fidelity_candidate.py'),
            'manifest_sha256': manifest_sha256, 'run_id': run_id, 'chunk': chunk, 'repair': repair,
            'original_prompt_sha256': digest(original.encode()), 'effective_prompt_sha256': digest(effective.encode()),
            'cli_call_id': call_id}


def verify_raw(artifact, row, attempts, case, environment):
    store = EvidenceStore(artifact/'state.sqlite3'); run = store.run(row['run_id'])
    doc = store.document(run['document_id'], run['version'])
    folder = artifact/'runs'/run['id']; slices = list(chunks(case['source']))
    validator = ExtractionCheckpoint.__new__(ExtractionCheckpoint); validator.run = run
    outputs: list[dict] = []; seen: set[tuple[int, bool]] = set(); ids = set()
    originals: dict[int, Any] = {}; issues_by_chunk: dict[int, Any] = {}; normalized: dict[int, Any] = {}
    states: list[dict] = []; expected_calls = set(); expected_repairs = set(); expected_accounting = set()
    prefix, repair_prefix = extraction_prompts(2)
    for attempt in attempts:
        index, repair = attempt['chunk'], attempt['repair']
        if (attempt['admission_state'] != 'finished' or type(index) is not int or not 1 <= index <= 3 or
                type(repair) is not bool or (index, repair) in seen or
                not repair and index != len(originals)+1 or repair and (index, False) not in seen or
                repair and index != len(originals) or len([p for p in seen if p[1]]) > 2):
            raise ValueError('candidate call schedule')
        if not repair and index > 1 and (len(outputs) != index-1 or not states[-1]['passed']):
            raise ValueError('call after incomplete chunk')
        seen.add((index, repair))
        offset, text = slices[index-1]
        call = folder/str(index)
        if repair: call /= 'quote-repair'
        expected_calls.add(call.relative_to(artifact).as_posix())
        meta = _read(call/'run.json'); cid = meta['cli_call_id']
        if cid in ids or cid != attempt['cli_call_id']: raise ValueError('call identity')
        ids.add(cid)
        payload = {'filename': doc['filename'], 'version': doc['version'], 'offset': offset, 'text': text}
        if repair:
            if not issues_by_chunk.get(index): raise ValueError('repair without quote issues')
            expected_repairs.add(call.relative_to(artifact).as_posix())
            original = bounded_prompt(repair_prefix, payload | {'issues': issues_by_chunk[index], 'approved_dictionary': run['ontology']})
            effective = original
        else:
            original = bounded_prompt(prefix, payload | {'approved_dictionary': run['ontology'] | {'payload': dictionary_context(run['ontology']['payload'])}})
            effective = compile_prompt(original)
        if ((call/'original-prompt.txt').read_text() != original or (call/'prompt.txt').read_text() != effective or
                meta['prompt_sha256'] != digest(effective.encode()) or
                _read(call/'candidate.json') != sidecar(original, effective, chunk=index, repair=repair, run_id=run['id'],
                                                       manifest_sha256=digest(environment), call_id=cid)):
            raise ValueError('candidate prompt or sidecar mismatch')
        stdout = (call/'stdout.txt').read_text() if (call/'stdout.txt').is_file() else ''
        answer, usage, terminal = decode_transport('claude', stdout)
        if (usage['tokens'] != attempt['tokens'] or usage['reported_usd'] != attempt['reported_usd'] or
                usage['resolved_model'] != attempt['resolved_model']): raise ValueError('raw usage identity')
        complete = isinstance(answer, str) and not terminal and bool(stdout)
        state = {'index': index, 'repair': repair, 'response_complete': complete, 'passed': False, 'reason': 'response_incomplete'}
        states.append(state)
        if not complete: continue
        try:
            value, normalizations = parse_response('claude', answer)
            if meta.get('response_normalizations', []) != normalizations: raise EvidenceMismatch('response framing')
            if repair:
                value, changes = apply_quote_repairs(normalized[index], text, issues_by_chunk[index], value)
                changes_path = call/'changes.json'
                if changes_path.is_file() and _audit(changes_path) != changes: raise EvidenceMismatch('quote repair audit')
            else:
                originals[index] = value
                before = value
                value, _ = exclude_off_dictionary_facts(value, case['dictionary'])
                issues_by_chunk[index] = extraction_quote_issues(value, text, case['dictionary'], contract_version=2)
                value, changes = repair_missing_indices(value, case['dictionary'])
                normalized[index] = value
                audit = call/'coverage-repair.json'
                if changes:
                    expected_accounting.add(call.relative_to(artifact).as_posix())
                    expected = {'schema_version': 1, 'identity': coverage_identity(run), 'chunk': index, 'offset': offset,
                                'source_sha256': doc['sha256'], 'text_sha256': digest(doc['text'].encode()),
                                'before_sha256': digest(before), 'after_sha256': digest(value),
                                'scope': 'mechanical_indices_only_not_chunk_acceptance', 'changes': changes, 'cli_call_id': cid}
                    if not audit.is_file() or _audit(audit) != expected: raise EvidenceMismatch('accounting repair audit')
                elif audit.exists(): raise EvidenceMismatch('unexpected accounting audit')
                if issues_by_chunk[index]:
                    state['reason'] = 'production_contract' if sum(r for _, r in seen) >= 2 else 'quote_repair_required'  # The count is this benchmark's own repair stop, not a product rule; stored artifacts keep the label.
                    continue
            output = validator.validated(value, text)
            outputs.append(output); state.update(passed=True, reason=None)
        except EvidenceMismatch:
            raise
        except (ValueError, TypeError, KeyError, IndexError):
            state['reason'] = 'production_contract'
    if len([p for p in seen if p[1]]) > 2: raise ValueError('repair budget')
    for filename in ('usage.json', 'run.json', 'prompt.txt', 'original-prompt.txt', 'candidate.json', 'stdout.txt'):
        actual = {p.parent.relative_to(artifact).as_posix() for p in folder.rglob(filename)}
        # A timeout may have no terminal stdout; all other call records are mandatory.
        if filename == 'stdout.txt':
            if not actual <= expected_calls: raise ValueError('unaccounted response')
        elif actual != expected_calls: raise ValueError('unaccounted candidate call')
    if any(p.parent.relative_to(artifact).as_posix() not in expected_repairs for p in folder.rglob('changes.json')):
        raise ValueError('unaccounted quote repair')
    if {p.parent.relative_to(artifact).as_posix() for p in folder.rglob('coverage-repair.json')} != expected_accounting:
        raise ValueError('unaccounted accounting repair')
    checkpoint_path = folder/'extraction-checkpoint.json'
    if checkpoint_path.exists():
        checkpoint = _read(checkpoint_path, MAX_REPORT_BYTES)
        pin = run['ontology']
        contract = {'pipeline': PIPELINE, 'prompt': digest(prefix.encode()), 'repair_prompt': digest(repair_prefix.encode()),
                    'chunk_size': 8000, 'chunk_timeout': CHUNK_TIMEOUT, 'document_id': run['document_id'], 'version': run['version'],
                    'source_sha256': doc['sha256'], 'text_sha256': digest(doc['text'].encode()), 'agent': 'claude', 'model': MODEL,
                    'dictionary': {k: pin[k] for k in ('id', 'revision', 'sha256', 'extraction_contract')},
                    'dictionary_payload_sha256': digest(pin['payload']), 'extraction_options': run['extraction_options']}
        if (checkpoint['schema_version'] != 1 or checkpoint['run_id'] != run['id'] or
                checkpoint['contract'] != contract or checkpoint['outputs'] != outputs):
            raise ValueError('candidate checkpoint mismatch')
    elif outputs: raise ValueError('missing checkpoint')
    if run['status'] == 'success' and len(outputs) != 3:
        raise ValueError('incomplete published candidate')
    if run['status'] == 'success' and any(not (artifact/p/'changes.json').is_file() for p in expected_repairs):
        raise ValueError('missing quote repair audit')
    progress = _read(folder/'extraction-progress.json')
    if progress['resumed_from'] is not None or progress['resumed_chars'] != 0 or progress['calls_started'] != len(attempts):
        raise ValueError('resume or call mismatch')
    return {'complete': len(outputs) == 3, 'passed': all(s['passed'] for s in states if not s['reason'] == 'quote_repair_required') and len(outputs) == 3,
            'verified_failure': any(s['response_complete'] and s['reason'] == 'production_contract' for s in states),
            'responses_complete': all(s['response_complete'] for s in states), 'chunks': states}


class EvidenceMismatch(ValueError): pass


def _audit(path):
    try: return _read(path)
    except (OSError, ValueError, TypeError) as exc:
        raise EvidenceMismatch('corrupt repair audit') from exc


def decision(report, safety, production):
    attempts = report['attempts']; row = report['documents'][0]; budget = report['budget']
    try:
        identities = [_identity(a) for a in attempts]
        identity_known = bool(identities) and len(set(identities)) == 1 and identities[0][1] == 1 and identities[0][3] == 1
    except ValueError:
        identity_known = False
    known = _known(attempts) and identity_known and all(a['resolved_model'] == RESOLVED_MODEL for a in attempts) and production['responses_complete']
    reasons = []
    if production['verified_failure']: reasons.append('production_contract_failed')
    if (row.get('quality') or {}).get('complete') and row['quality']['passed'] is False: reasons.append('finite_gold_failed')
    if safety['passed'] is False: reasons.append('publication_safety_failed')
    admissible = (all(r in ('document_failed', 'call_failed') for r in report['stop_reasons']) and
                  report['cleanup_errors'] == 0 and budget['elapsed_seconds'] < LIMITS['seconds'] and
                  budget['observed_tokens_lower_bound'] < LIMITS['tokens'])
    passed = (known and admissible and production['passed'] and safety['passed'] and row['quality']['complete'] and
              row['quality']['passed'] and row['status'] == 'success' and report['status'] == 'completed' and not report['stop_reasons'])
    rejected = known and admissible and bool(reasons)
    verdict = 'accepted_for_further_testing' if passed else 'rejected' if rejected else 'inconclusive'
    if not known: reasons.append('usage_identity_or_response_unknown')
    if not passed and not rejected: reasons.append('incomplete_trial')
    if report['synthetic_responses']: verdict = 'inconclusive'; reasons.append('synthetic_only')
    return {'verdict': verdict, 'reasons': reasons, 'candidate_id': CANDIDATE_ID, 'quality': row.get('quality'),
            'safety': safety, 'production': production, 'requests': len(attempts),
            'observed_tokens': budget['observed_tokens_lower_bound'], 'seconds': budget['elapsed_seconds'],
            'scope': 'One three-chunk finite fixture; no general fidelity, speedup or operational acceptance.'}


def verify_trial(root, report):
    """Verify raw trial components; finalization is a separate publication gate."""
    spec = verify_manifest(root, report)
    if len(report['documents']) != 1: raise ValueError('single document')
    row = report['documents'][0]
    if (any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in ROW.items()) or
            row['artifact'] != 'documents/three-1' or row['attempt_start'] != 1 or
            row['attempt_count'] != len(report['attempts']) or not 1 <= len(report['attempts']) <= 5 or
            {p.name for p in (root/'documents').iterdir()} != {'three-1'}): raise ValueError('document schedule')
    artifact = root/row['artifact']
    if row['artifact_files'] != file_index(artifact): raise ValueError('document index')
    case = load_case('three', root/'fixtures')
    safety = verify_document(artifact, row, report['attempts'], case, MODEL, spec)
    if safety != row['safety']: raise ValueError('safety summary')
    production = verify_raw(artifact, row, report['attempts'], case, report['environment'])
    for name in ('usage.json', 'run.json', 'prompt.txt', 'stdout.txt', 'candidate.json', 'original-prompt.txt', 'state.sqlite3'):
        if {p.relative_to(root).as_posix() for p in root.rglob(name)} != {p.relative_to(root).as_posix() for p in artifact.rglob(name)}:
            raise ValueError('global unaccounted evidence')
    verify_budget(report)
    if (not isinstance(report['stop_reasons'], list) or any(not isinstance(r, str) for r in report['stop_reasons']) or
            (report['stop_reason'] is None) != (not report['stop_reasons']) or
            report['stop_reason'] is not None and report['stop_reason'] not in report['stop_reasons'] or
            type(report['cleanup_errors']) is not int or report['cleanup_errors'] < 0 or
            report['status'] not in ('completed', 'stopped') or
            report['final_write_tail'] != 'final report/index/evaluation writes follow the last wall observation'):
        raise ValueError('terminal accounting')
    return safety, production


def evaluate(root):
    """Offline only: no execution of retained code, HTTP, subprocess or DB writes."""
    fallback = {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed']}
    try:
        root = Path(root)
        if root.is_symlink(): raise ValueError('root symlink')
        root = root.resolve(); before = file_index(root)
        if (_read(root/'index.json', MAX_REPORT_BYTES) != {p: v for p, v in before.items() if p not in ('index.json', 'evaluation.json')} or
                _read(root/'finalization.json') != {'complete': True}):
            raise ValueError('raw index or finalization')
        report = _read(root/'report.json', MAX_REPORT_BYTES)
        safety, production = verify_trial(root, report)
        if before != file_index(root): raise ValueError('raw changed during evaluation')
        return decision(report, safety, production)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error):
        return fallback
