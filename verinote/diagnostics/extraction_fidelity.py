"""Authenticated offline Claude replay and finite evidence-obligation tracing."""
import copy
from pathlib import Path

from ..agents import parse_response
from ..documents import chunks
from ..extraction_ab_runner import _hash, file_index
from ..extraction_benchmark import MAX_REPORT_BYTES
from ..extraction_coverage import repair_missing_indices
from ..extraction_evaluation import _read, evaluate_case, validate_case
from ..extraction_threshold_ab_runner import evaluate_saved
from ..extraction_threshold_artifacts import EvidenceStore
from ..extraction_threshold_evaluation import _supported
from ..ontology import (
    bounded_prompt,
    dictionary_context,
    dictionary_fact,
    extraction_prompts,
    extraction_quote_issues,
)
from ..service import validate_page
from ..store import canonical, digest

SCHEMA_VERSION = 1
DIAGNOSTIC_VERSION = 'extraction-fidelity-v1'
REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO/'benchmarks/extraction/results/768-threshold-v2-sonnet'
REPORT_SHA256 = '72a63ca70e3cfdc9bd9a771999a65ed537bb584d5c8aacfe054c2e4ece981a5f'
DIAGNOSTIC_PATHS = ('verinote/diagnostics/__init__.py', 'verinote/diagnostics/extraction_fidelity.py',
                    'scripts/diagnose_extraction_fidelity.py', 'docs/extraction-fidelity-diagnosis.md')
CLAUSE_PREFIXES = {'qualified_claims': '특허·설계 문서에서', 'coverage_links': '반환 전 coverage',
                   'coverage_status': 'status:', 'dictionary_gaps': 'dictionary_gaps는',
                   'literal_quote': 'quote는', 'quote_provenance': 'claims와 facts의 quote'}


class ArtifactVerificationError(ValueError):
    """An unauthenticated artifact is not a production or fidelity failure."""


def prompt_requirements():
    prompt = extraction_prompts(2)[0]
    result = {}
    for key, prefix in CLAUSE_PREFIXES.items():
        lines = [line for line in prompt.splitlines() if line.startswith(prefix)]
        if len(lines) != 1: raise ValueError('diagnostic_prompt_clause_missing')
        result[key] = {'text': lines[0], 'sha256': digest(lines[0].encode())}
    return result


def _production(case, index, original):
    """Replay Service's content gates on a copy, including checkpoint validation.

    Quote repair would require a new model call and is reported, never invented.
    No page/claim/gap semantics are repaired by this function.
    """
    offset, text = list(chunks(case['source']))[index-1]
    output = copy.deepcopy(original)
    result = {'index': index, 'offset': offset, 'text_sha256': digest(text.encode()),
              'production_passed': False, 'production_error': None, 'failed_stage': None,
              'quote_repair_required': False, 'accounting_changes': [], 'stages': []}
    stage = 'shape_quote_preflight'
    try:
        issues = extraction_quote_issues(output, text, case['dictionary'], contract_version=2)
        result['stages'].append(stage)
        stage = 'mechanical_accounting'
        output, changes = repair_missing_indices(output, case['dictionary'])
        result['accounting_changes'] = changes
        result['stages'].append(stage)
        if issues:
            result.update(quote_repair_required=True, quote_issues=issues)
            stage = 'quote_repair_required'
            raise ValueError('offline diagnosis does not generate quote repairs')
        stage = 'page_validation'
        page = validate_page(output, text)
        result['stages'].append(stage)
        stage = 'fact_validation'
        facts = [dictionary_fact(fact, text, case['dictionary']) for fact in output['facts']]
        result['stages'].append(stage)
        stage = 'checkpoint_validation'
        if extraction_quote_issues(output, text, case['dictionary'], require_coverage=True, contract_version=2):
            raise ValueError('checkpoint contains ungrounded quotes')
        # This is ExtractionCheckpoint.validated's representation, without its
        # constructor/save methods or any database/file writes.
        normalized = page | {'facts': facts, 'coverage': copy.deepcopy(output['coverage']),
                             'dictionary_gaps': copy.deepcopy(output['dictionary_gaps'])}
        result['stages'].append(stage)
        result.update(production_passed=True, normalized_output=normalized)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        result.update(production_error=str(exc), failed_stage=stage)
    return result


def _matrix(case, index, output, errors):
    annotation = case['annotations'][index-1]
    offset, text = list(chunks(case['source']))[index-1]
    # Deliberately reuse the frozen evaluator's literal span test. This is an
    # explanation of its result, not an additional semantic judge.
    supported = [{claim['id'] for claim in annotation['claims']
                  if _supported(case['source'], row['quote'].strip(), offset, text, claim['evidence'])}
                 for row in output['claims']]
    matches = {claim['id']: [i for i, ids in enumerate(supported) if claim['id'] in ids]
               for claim in annotation['claims']}
    local_errors = [e for e in errors if e['chunk'] == index]
    claims = [{'id': claim['id'], 'source_evidence': copy.deepcopy(claim['evidence']),
               'prompt_requirements': ['qualified_claims', 'literal_quote', 'quote_provenance'],
               'matched_claim_indices': matches[claim['id']],
               'matched_claims': [{'index': i, **copy.deepcopy(output['claims'][i])} for i in matches[claim['id']]],
               'passed': bool(matches[claim['id']]),
               'evaluator_errors': [e for e in local_errors if e.get('id') == claim['id']]}
              for claim in annotation['claims']]
    coverage = []
    for expected in annotation['coverage']:
        actual = next(row for row in output['coverage'] if row['predicate'] == expected['predicate'])
        ids = set().union(*(supported[i] for i in actual['claim_indices']))
        related_errors = [e for e in local_errors if e.get('predicate') == expected['predicate']]
        coverage.append({'predicate': expected['predicate'], 'prompt_requirements': ['coverage_links', 'coverage_status'],
                         'expected_status': expected['status'], 'actual': copy.deepcopy(actual),
                         'expected_claim_ids': expected['claim_ids'],
                         'eligible_claim_indices': {key: matches[key] for key in expected['claim_ids']},
                         'actual_supported_claim_ids': sorted(ids),
                         'missing_claim_ids': sorted(set(expected['claim_ids'])-ids),
                         'expected_fact_indices': [i for i, f in enumerate(output['facts']) if f['predicate'] == expected['predicate']],
                         'passed': not related_errors, 'evaluator_errors': related_errors})
    pairs = {(row['code'], key) for row in annotation['gaps'] for key in row['claim_ids']}
    actual_gaps = []
    for i, row in enumerate(output['dictionary_gaps']):
        eligible = {j: sorted(key for key in supported[j] if (row['code'], key) in pairs) for j in row['claim_indices']}
        actual_gaps.append({'index': i, 'actual': copy.deepcopy(row), 'supported_expected_claim_ids': sorted(set().union(*map(set, eligible.values()))),
                            'unexpected_claim_indices': [j for j, ids in eligible.items() if not ids]})
    gaps = []
    for expected in annotation['gaps']:
        actual = [row for row in actual_gaps if row['actual']['code'] == expected['code']]
        credited = set().union(*(set(row['supported_expected_claim_ids']) for row in actual))
        missing = sorted(set(expected['claim_ids'])-credited)
        gaps.append({'code': expected['code'], 'prompt_requirements': ['dictionary_gaps', 'qualified_claims'],
                     'expected_claim_ids': expected['claim_ids'],
                     'eligible_claim_indices': {key: matches[key] for key in expected['claim_ids']},
                     'actual_rows': actual, 'missing_claim_ids': missing,
                     'passed': not missing and not any(row['unexpected_claim_indices'] for row in actual),
                     'evaluator_errors': [e for e in local_errors if e['reason'] == 'gap_completeness' or e.get('code') == expected['code']]})
    return {'index': index, 'claim_obligations': claims, 'coverage_obligations': coverage, 'gap_obligations': gaps,
            'actual_gaps': actual_gaps, 'evaluator_errors': local_errors}


def assess_outputs(case, chunk_outputs):
    """Pure finite-fixture diagnosis, preserving actual one-based chunk indices."""
    validate_case(case)
    if case['dataset_version'] != 2: raise ValueError('diagnostic requires frozen dataset v2')
    count = len(list(chunks(case['source'])))
    if (not isinstance(chunk_outputs, list) or len(chunk_outputs) > count or
            any(not isinstance(row, dict) or set(row) != {'index', 'output'} or
                type(row['index']) is not int or not 1 <= row['index'] <= count for row in chunk_outputs) or
            len({row['index'] for row in chunk_outputs}) != len(chunk_outputs)):
        raise ValueError('diagnostic chunk identity')
    assessed = [_production(case, row['index'], row['output']) for row in chunk_outputs]
    production_passed = all(row['production_passed'] for row in assessed)
    outputs = [{'index': row['index'], 'output': row['normalized_output']} for row in assessed if row['production_passed']]
    gold = evaluate_case(case, outputs) if production_passed else None
    matrix = [_matrix(case, row['index'], row['output'], gold['annotation_errors']) for row in outputs] if gold is not None else []
    status = 'production_failure' if gold is None else 'passed' if gold['passed'] else 'fidelity_failure'
    return {'production_passed': production_passed, 'chunks': assessed, 'finite_gold': gold,
            'obligations': matrix, 'prompt_requirements': prompt_requirements(),
            'status': status,
            'scope': 'Frozen finite source-span obligations; no general language entailment or model judge.'}


def _authenticate_current(environment):
    """The historical verifier checks retained copies, not current core hashes."""
    hashes = environment['source_hashes']
    for relative, expected in hashes.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in ('verinote', 'scripts', 'docs'):
            raise ArtifactVerificationError('current_implementation_path')
        current = REPO/path
        if current.is_symlink() or _hash(current) != expected:
            raise ArtifactVerificationError('current_implementation_mismatch:'+relative)
    return dict(hashes)


def diagnose(root=DEFAULT_ROOT):
    """Strict complete raw authentication, then read-only replay of pinned calls."""
    root = Path(root)
    if root.is_symlink(): raise ArtifactVerificationError('artifact_symlink')
    root = root.resolve()
    try:
        before = file_index(root)
        if before.get('report.json', {}).get('sha256') != REPORT_SHA256:
            raise ArtifactVerificationError('report_identity_mismatch')
        retained = _read(root/'index.json', MAX_REPORT_BYTES)
        if retained != {key: value for key, value in before.items() if key not in ('index.json', 'evaluation.json')}:
            raise ArtifactVerificationError('raw_index_mismatch')
        report = _read(root/'report.json', MAX_REPORT_BYTES)
        current_hashes = _authenticate_current(report['environment'])
        verified = evaluate_saved(root)
        if (verified.get('verdict') != 'rejected' or verified.get('unverified_other_records') or
                any('verification_failed' in reason for reason in verified.get('reasons', []))):
            raise ArtifactVerificationError('historical_verification_failed')
        if len(report['documents']) != 1 or len(report['attempts']) != 3:
            raise ArtifactVerificationError('historical_call_schedule')
        row = report['documents'][0]
        if row['source_case'] != 'three' or row['attempt_count'] != 3 or row['status'] != 'success':
            raise ArtifactVerificationError('historical_document_identity')
        from ..extraction_evaluation import load_case
        case = load_case('three', root/'fixtures')
        artifact = root/row['artifact']
        store = EvidenceStore(artifact/'state.sqlite3')
        run = store.run(row['run_id']); doc = store.document(run['document_id'], run['version'])
        folder = artifact/'runs'/run['id']
        checkpoint = _read(folder/'extraction-checkpoint.json', MAX_REPORT_BYTES)
        if checkpoint['run_id'] != run['id'] or len(checkpoint['outputs']) != 3:
            raise ArtifactVerificationError('checkpoint_identity')
        prompt, _ = extraction_prompts(2)
        outputs, linkages = [], []
        for index, (offset, text) in enumerate(chunks(case['source']), 1):
            call = folder/str(index); attempt = report['attempts'][index-1]
            meta = _read(call/'run.json'); stdout = _read(call/'stdout.txt')
            if (attempt['chunk'] != index or attempt['repair'] is not False or
                    meta['cli_call_id'] != attempt['cli_call_id'] or meta['status'] != 'success'):
                raise ArtifactVerificationError('call_identity')
            payload = {'filename': doc['filename'], 'version': doc['version'], 'offset': offset, 'text': text,
                       'approved_dictionary': run['ontology'] | {'payload': dictionary_context(run['ontology']['payload'])}}
            expected_prompt = bounded_prompt(prompt, payload)
            actual_prompt = (call/'prompt.txt').read_text(encoding='utf-8')
            if actual_prompt != expected_prompt or digest(actual_prompt.encode()) != meta['prompt_sha256']:
                raise ArtifactVerificationError('prompt_chunk_linkage')
            output, normalizations = parse_response('claude', stdout['result'])
            if normalizations != meta['response_normalizations']:
                raise ArtifactVerificationError('response_normalization_linkage')
            outputs.append({'index': index, 'output': output})
            linkages.append({'index': index, 'offset': offset, 'cli_call_id': meta['cli_call_id'],
                             'prompt_sha256': meta['prompt_sha256'], 'stdout_sha256': _hash(call/'stdout.txt'),
                             'raw_output_sha256': digest(output), 'checkpoint_output_sha256': digest(checkpoint['outputs'][index-1])})
        result = assess_outputs(case, outputs)
        for chunk in result['chunks']:
            if not chunk['production_passed'] or canonical(chunk['normalized_output']) != canonical(checkpoint['outputs'][chunk['index']-1]):
                raise ArtifactVerificationError('production_checkpoint_linkage')
            if chunk['accounting_changes']:
                # This pinned three-call history contains no accounting repairs.
                # Do not silently extend its authenticated replay contract.
                raise ArtifactVerificationError('unexpected_historical_accounting_repair')
        if result['finite_gold'] != row['quality']['evaluation']:
            raise ArtifactVerificationError('frozen_evaluator_linkage')
        if before != file_index(root): raise ArtifactVerificationError('raw_artifacts_changed')
        _authenticate_current(report['environment'])
        result.update(schema_version=SCHEMA_VERSION, diagnostic_version=DIAGNOSTIC_VERSION,
                      diagnostic_implementation={'scope': 'Current diagnostic, not authenticated by the historical capture',
                          'source_hashes': {path: _hash(REPO/path) for path in DIAGNOSTIC_PATHS}},
                      historical_authentication={'report_sha256': REPORT_SHA256, 'raw_index_sha256': digest(before),
                          'current_source_hashes': current_hashes, 'verification': verified, 'raw_unchanged': True},
                      call_linkages=linkages, source_sha256=case['source_sha256'], generation_requests=0,
                      candidate_evaluated=False)
        return result
    except ArtifactVerificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ArtifactVerificationError('artifact_verification_failed:'+str(exc)) from exc
