"""Read-only diagnostics of the complete frozen extraction A/B evidence."""
import copy
import hashlib
import math
from pathlib import Path

from .documents import chunks
from .extraction_ab import _attempts, _known, experiment_plan
from .extraction_ab_runner import evaluate_saved, file_index
from .extraction_benchmark import MAX_REPORT_BYTES
from .extraction_evaluation import _read, evaluate_case, load_case
from .ontology import extraction_quote_issues


def interval_union(intervals):
    """Wall time, distinct from summed work when intervals overlap."""
    end = None
    total = 0.0
    for start, stop in sorted(intervals):
        if not all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in (start, stop)) or stop < start:
            raise ValueError('invalid timing interval')
        total += stop-max(start, end if end is not None else start) if end is None or stop > end else 0
        end = max(stop, end if end is not None else stop)
    return total


def known_sum(values):
    values = list(values)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values): return None
    return sum(values) if all(type(v) is int for v in values) else math.fsum(values)


def diagnose_saved(root):
    root = Path(root)
    before = file_index(root)
    evaluation = evaluate_saved(root)
    report = _read(root/'report.json', MAX_REPORT_BYTES)
    plan = experiment_plan()
    # A rejected safety verdict may coexist with unverified later records.
    if (evaluation.get('unverified_other_records') or evaluation.get('synthetic_responses')
            or evaluation.get('verdict') not in ('accepted', 'rejected')
            or set(evaluation.get('reasons', [])) - {'incomplete_execution'} - {
                'quality:'+d['key'] for d in plan['documents']}
            or evaluation != _read(root/'evaluation.json')
            or len(report['documents']) != len(plan['documents'])
            or not _known(report['attempts']) or report['budget']['pending'] or report['cleanup_errors']):
        raise ValueError('complete verified evidence required')
    linked = [_attempts(row, report['attempts']) for row in report['documents']]
    if [a['attempt'] for values in linked for a in values] != list(range(1, len(report['attempts'])+1)):
        raise ValueError('attempt linkage')
    result = {'schema_version': 1, 'input_hashes': {
        name: hashlib.sha256((root/name).read_bytes()).hexdigest()
        for name in ('report.json', 'index.json', 'environment.json', 'plan.json')},
        'verdict': evaluation['verdict'], 'comparisons': evaluation['comparisons'],
        'timing': [], 'cold_long_calls': [], 'quote_failures': [],
        'accounting': {'calls': len(report['attempts']),
            'tokens': sum(a['tokens']['input']+a['tokens']['output'] for a in report['attempts']),
            'reported_usd': known_sum(a['reported_usd'] for a in report['attempts'])},
        'interpretation': {
            'tokens': 'input includes cache_read and cache_write; do not add cache tokens again',
            'timing': 'work sums may overlap; CLI response/reset/wait are nested inside request, not additive to it',
            'cost': 'reported USD, not recalculated pricing; provider cache causality is unknown'}}
    for row, values in zip(report['documents'], linked, strict=True):
        folder = root/row['artifact']/'runs'/row['run_id']
        timing = _read(folder/'extraction-timing.json')
        spans = timing['spans']
        requests = [(s['start'], s['end']) for s in spans if s['kind'] in ('request', 'repair_request')]
        local = [(s['start'], s['end']) for s in spans if s['kind'] in (
            'restore', 'validation_with_bookkeeping', 'repair_validation_with_bookkeeping', 'publish')]
        total = row['document_timing']['summary']['total_seconds']
        result['timing'].append({'key': row['key'], 'phase': row['phase'], 'case_id': row['case_id'],
            'purpose': row['purpose'], 'arm': row['arm'], 'pair': row['pair'], 'service_total': total,
            'request_work': known_sum(stop-start for start, stop in requests), 'request_wall': interval_union(requests),
            'explicit_local_wall': interval_union(local),
            'unattributed_wall': max(0, total-interval_union(requests+local)),
            'cli_work': {key: known_sum(a['cli_timing']['durations_seconds'].get(key) for a in values)
                         for key in ('response_wait', 'reset', 'worker_wait')},
            'output_tokens': sum(a['tokens']['output'] for a in values),
            'call_ids': [a['cli_call_id'] for a in values]})
        if row['phase']=='cold' and row['case_id']=='long':
            for a in values:
                result['cold_long_calls'].append({'key': row['key'], 'pair': row['pair'], 'arm': row['arm'],
                    'chunk': a['chunk'], 'repair': a['repair'], 'call_id': a['cli_call_id'],
                    'tokens': a['tokens'], 'reported_usd': a['reported_usd']})
        if row['quality']['passed']: continue
        case = load_case(row['source_case'])
        checkpoint = _read(folder/'extraction-checkpoint.json')
        outputs = [{'index': i, 'output': output} for i, output in enumerate(checkpoint['outputs'], 1)]
        score = evaluate_case(case, outputs)
        if score != row['quality']['evaluation']: raise ValueError('quote evidence mismatch')
        preflight = [extraction_quote_issues(o['output'], text, case['dictionary'], require_coverage=True, contract_version=checkpoint['contract']['dictionary']['extraction_contract'])
                     for o, (_, text) in zip(outputs, chunks(case['source']), strict=True)]
        control = copy.deepcopy(outputs)
        for error in score['citation_errors']:
            if error['reason'] != 'ambiguous_occurrence': continue
            gold = next(g for g in case['gold'] if g['id']==error['gold_id'])
            control[error['chunk']-1]['output']['facts'][error['row']-1]['quote'] = gold['sample_quote']
        result['quote_failures'].append({'key': row['key'], 'source_sha256': score['source_sha256'],
            'case_sha256': score['case_sha256'], 'original_outputs': outputs, 'gold': case['gold'],
            'production_preflight_issues': preflight, 'original_evaluation': score,
            'separate_context_control_evaluation': evaluate_case(case, control)})
    if file_index(root) != before: raise ValueError('evidence changed during diagnosis')
    return result
