"""Frozen threshold A/B contract v2. Pure evaluation, never model calls."""
import random
import re

from .agents import validate_model
from .extraction_ab import (
    _attempts,
    _identity,
    _known,
    _ratio_totals,
    _totals,
    paired_statistics,
    positive,
    validate_lifecycle,
)
from .extraction_benchmark import resolved_model, sha
from .extraction_options import CHUNK_THRESHOLD_POLICY, extraction_options


def resolved_sonnet_model(value):
    return resolved_model(value) is not None and re.fullmatch(r'claude-(?:[0-9]+-)*sonnet-[0-9]+(?:[.-][0-9a-z]+)*', value) is not None


def sonnet_model(value):
    value = validate_model(value, 'claude')
    if value != 'sonnet' and not resolved_sonnet_model(value):
        raise ValueError('threshold experiment requires an explicit Sonnet model')
    return value


def experiment_plan():
    series: list[dict] = []
    documents = []; owner = 0
    for phase in ('cold', 'warm'):
        for case, count in (('three', 3), ('four', 4), ('eight', 8)):
            index = len(series)
            schedule = ['AB']*5+['BA']*5
            random.Random(76420261101+index).shuffle(schedule)
            series.append({'index': index, 'phase': phase, 'case_id': case, 'chunks': count,
                           'pairs': 10, 'schedule': schedule, 'bootstrap_seed': 76420261111+index})
            owners = {}
            if phase == 'warm':
                for arm, capacity in [('A', 1), ('B', 1 if count == 3 else 2)]:
                    owner += 1; owners[arm] = owner
                    for slot in range(1, capacity+1):
                        documents.append({'key': f'{phase}-{case}-warmup-{arm.lower()}-{slot}', 'phase': phase,
                            'case_id': case, 'source_case': 'short', 'pair': 0, 'arm': arm, 'slot': slot,
                            'purpose': 'warmup', 'owner_id': owner, 'concurrency': capacity, 'chunks': 1,
                            'extraction_policy': None})
            for pair, order in enumerate(schedule, 1):
                for arm in order:
                    if phase == 'cold': owner += 1; owners[arm] = owner
                    documents.append({'key': f'{phase}-{case}-measurement-{arm.lower()}-{pair}', 'phase': phase,
                        'case_id': case, 'source_case': case, 'pair': pair, 'arm': arm, 'slot': None,
                        'purpose': 'measurement', 'owner_id': owners[arm],
                        'concurrency': 1 if arm == 'A' or count == 3 else 2, 'chunks': count,
                        'extraction_policy': CHUNK_THRESHOLD_POLICY if arm == 'B' else None})
    return {'schema_version': 2, 'contract_version': 2, 'dataset_version': 2, 'extraction_contract': 2,
            'timeout_seconds': 90, 'series': series, 'documents': documents,
            'measurement_documents': 120, 'warmup_documents': 8, 'initial_requests': 608,
            'maximum_repair_requests': 248, 'maximum_requests': 856}


def validate_policy(row):
    """Warm-up's explicit capacity is an exception only for planned warm-up rows."""
    options = extraction_options(row.get('extraction_options'))
    sonnet_model(options['transport']['model'])
    if row.get('extraction_contract') != 2 or type(row.get('extraction_contract')) is not int:
        raise ValueError('extraction_contract')
    if options['concurrency'] != row['concurrency'] or options['document_generation'] != 1:
        raise ValueError('registered_concurrency_or_generation')
    if options['transport']['timeout'] != 90: raise ValueError('registered_timeout')
    if row['extraction_policy'] is None:
        if options['schema_version'] != 1: raise ValueError('explicit_policy_required')
    elif (options['schema_version'] != 2 or options['policy'] != CHUNK_THRESHOLD_POLICY
          or options['original_chunk_count'] != row['chunks']):
        raise ValueError('threshold_policy_required')
    return options


def _linked_records(rows, attempts, plan):
    expected = {r['key']: r for r in plan['documents']}
    actual: dict[str, dict] = {}; linked: dict[str, list] = {}; invalid: set[str] = set(); reasons = []
    owners: dict[int, set[str]] = {}; calls: dict[str, str] = {}; ordinals: dict[int, str] = {}
    workers: dict[str, tuple[int, int, str]] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict): reasons.append('invalid_document'); continue
        key = row.get('key')
        if not isinstance(key, str) or key not in expected:
            reasons.append('unexpected_document'); continue
        if key in actual:
            invalid.add(key); reasons.append('duplicate_document'); continue
        actual[key] = row
        if (any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in expected[key].items())
                or position >= len(plan['documents']) or key != plan['documents'][position]['key']):
            invalid.add(key); reasons.append('document_plan_or_order'); continue
        try:
            values = _attempts(row, attempts)
            if sum(v['repair'] for v in values) > min(2, row['chunks']): raise ValueError('repair_limit')
            linked[key] = values
            for value in values:
                ordinal = value['attempt']
                if ordinal in ordinals: invalid.update((key, ordinals[ordinal])); reasons.append('shared_attempt')
                ordinals[ordinal] = key
                call = value.get('cli_call_id')
                if isinstance(call, str):
                    if call in calls: invalid.update((key, calls[call])); reasons.append('duplicate_call_id')
                    calls[call] = key
                try:
                    identity = _identity(value); pool = identity[0]
                except (ValueError, TypeError): continue  # Comparability check below reports unknown identity.
                owners.setdefault(row['owner_id'], set()).add(pool)
                if identity[2] in workers and workers[identity[2]][:2] != (row['owner_id'], identity[1]):
                    invalid.update((key, workers[identity[2]][2])); reasons.append('shared_worker')
                workers[identity[2]] = (row['owner_id'], identity[1], key)
        except (ValueError, KeyError, TypeError, IndexError):
            invalid.add(key); reasons.append('attempt_linkage')
    if set(ordinals) != set(range(1, len(attempts)+1)): reasons.append('unlinked_attempts')
    # A pool cannot be shared by owners, even if each owner's rows look coherent alone.
    pool_owners: dict[str, set[int]] = {}
    for owner, pools in owners.items():
        for pool in pools: pool_owners.setdefault(pool, set()).add(owner)
    for key, row in actual.items():
        current_owner = row.get('owner_id')
        if type(current_owner) is not int or current_owner not in owners: continue
        if len(owners[current_owner]) > 1:
            invalid.add(key); reasons.append('owner_pool_changed')
        if any(len(pool_owners[pool]) > 1 for pool in owners[current_owner]):
            invalid.add(key); reasons.append('shared_pool')
    return actual, linked, invalid, sorted(set(reasons))


def evaluate_report(report):
    """Raw evidence must be checked by the runner before this pure decision step."""
    plan = experiment_plan()
    if not isinstance(report, dict) or report.get('schema_version') != 2 or sha(report.get('plan')) != sha(plan):
        return {'verdict': 'inconclusive', 'reasons': ['plan_mismatch'], 'comparisons': []}
    rows, attempts = report.get('documents'), report.get('attempts')
    if not isinstance(rows, list) or not isinstance(attempts, list):
        return {'verdict': 'inconclusive', 'reasons': ['missing_records'], 'comparisons': []}
    actual, linked, invalid, reasons = _linked_records(rows, attempts, plan)
    comparisons = []; violations = []
    models = {v.get('resolved_model') for v in attempts if isinstance(v, dict) and resolved_model(v.get('resolved_model')) is not None}
    if len(models) != 1: reasons.append('model_mismatch_or_unknown')
    if any(not resolved_sonnet_model(model) for model in models): reasons.append('model_family')
    configurations = set()
    for key, row in actual.items():
        if key in invalid: continue
        try:
            transport = dict(validate_policy(row)['transport']); transport.pop('capacity')
            configurations.add(sha(transport))
        except (ValueError, KeyError, TypeError): continue
    if len(configurations) != 1: reasons.append('transport_mismatch_or_unknown')
    if report.get('stop_reason') == 'source_changed':
        invalid.update(actual); reasons.append('source_changed')
    for key, row in actual.items():
        if key in invalid: continue
        for kind in ('quality', 'safety'):
            value = row.get(kind)
            if isinstance(value, dict) and value.get('complete') is True and value.get('passed') is False:
                violations.append(kind+':'+key)
    for series in plan['series']:
        phase, case = series['phase'], series['case_id']
        result = {'phase': phase, 'case_id': case, 'verdict': 'inconclusive', 'reasons': [], 'statistics': None,
                  'measurement_totals': None, 'measurement_ratios': None, 'warmup_totals': None, 'series_ratios': None}
        comparisons.append(result)
        specs = [r for r in plan['documents'] if r['phase'] == phase and r['case_id'] == case]
        if any(r['key'] not in actual or r['key'] in invalid or r['key'] not in linked for r in specs):
            result['reasons'].append('missing_or_invalid_comparison'); continue
        members = [actual[r['key']] for r in specs]
        try:
            by_arm: dict[str, dict[str, list[dict]]] = {arm: {'measurement': [], 'warmup': []} for arm in ('A', 'B')}
            warmed: dict[tuple, tuple] = {}; local_models: set[str] = set(); transports = set()
            for row in members:
                options = validate_policy(row)
                transport = dict(options['transport']); transport.pop('capacity')
                transports.add(sha(transport))
                values = linked[row['key']]
                if (row.get('status') != 'success' or not _known(values)
                        or any(not isinstance(row.get(k), dict) or row[k].get('complete') is not True or row[k].get('passed') is not True for k in ('quality', 'safety'))):
                    raise ValueError('completion_quality_safety_model_or_usage')
                if sorted(v['chunk'] for v in values if not v['repair']) != list(range(1, row['chunks']+1)):
                    raise ValueError('missing_initial_calls')
                local_models.update(v['resolved_model'] for v in values)
                by_arm[row['arm']][row['purpose']].extend(values)
                validate_lifecycle(row, values, warmed)
            if len(local_models) != 1 or len(transports) != 1 or any(not resolved_sonnet_model(model) for model in local_models):
                raise ValueError('model_or_transport_mismatch')
            ratios = []
            for pair in range(1, 11):
                times = {}
                for arm in ('A', 'B'):
                    row = next(r for r in members if r['pair'] == pair and r['arm'] == arm and r['purpose'] == 'measurement')
                    if phase == 'cold':
                        if row.get('cleanup_succeeded') is not True: raise ValueError('cold_cleanup')
                        value = row.get('cold_seconds')
                    else:
                        timing = row.get('document_timing') or {}
                        if timing.get('available') is not True or timing.get('status') != 'success': raise ValueError('timing')
                        value = timing.get('summary', {}).get('total_seconds')
                    if not positive(value): raise ValueError('timing')
                    times[arm] = value
                ratios.append(times['B']/times['A'])
            result['statistics'] = paired_statistics(ratios, series['bootstrap_seed'])
            totals = {arm: _totals(by_arm[arm]['measurement']) for arm in ('A', 'B')}
            result['measurement_totals'] = totals
            result['measurement_ratios'] = _ratio_totals(totals['A'], totals['B'])
            result['warmup_totals'] = {arm: _totals(by_arm[arm]['warmup']) for arm in ('A', 'B')}
            failures = []
            if result['statistics']['median'] > (1.1 if series['chunks'] == 3 else .8): failures.append('latency_median')
            if series['chunks'] >= 4 and result['statistics']['bootstrap_95'][1] >= 1: failures.append('latency_uncertainty')
            if any(v > 1.1 for v in result['measurement_ratios'].values()): failures.append('measurement_cost_or_tokens')
            if any(totals['B'][k] > totals['A'][k] for k in ('errors', 'timeouts', 'repairs')): failures.append('error_timeout_or_repair')
            if phase == 'warm':
                whole = {arm: _totals(by_arm[arm]['measurement']+by_arm[arm]['warmup']) for arm in ('A', 'B')}
                result['series_ratios'] = _ratio_totals(whole['A'], whole['B'])
                if any(v > 1.1 for v in result['series_ratios'].values()): failures.append('series_cost_or_tokens')
                if any(whole['B'][k] > whole['A'][k] for k in ('errors', 'timeouts', 'repairs')): failures.append('series_error_timeout_or_repair')
            result.update(verdict='rejected' if failures else 'accepted', reasons=failures)
        except (ValueError, KeyError, TypeError, StopIteration, IndexError, AttributeError) as exc:
            result['reasons'].append(str(exc))
    if report.get('cleanup_errors', 0): reasons.append('cleanup_error')
    if report.get('synthetic_responses'): reasons.append('synthetic_only')
    if report.get('status') != 'completed': reasons.append('incomplete_execution')
    if len(actual) != len(plan['documents']): reasons.append('missing_documents')
    verdict = ('rejected' if violations or any(c['verdict'] == 'rejected' for c in comparisons) else
               'inconclusive' if reasons or any(c['verdict'] != 'accepted' for c in comparisons) else 'accepted')
    if report.get('synthetic_responses'): verdict = 'inconclusive'
    return {'verdict': verdict, 'reasons': violations+sorted(set(reasons)), 'resolved_models': sorted(models),
            'synthetic_responses': bool(report.get('synthetic_responses')), 'comparisons': comparisons}
