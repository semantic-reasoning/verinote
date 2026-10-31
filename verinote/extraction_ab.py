"""Frozen extraction A/B contract v1: schedule and pure, conservative verdicts."""
import math
import random
import re
import statistics

from .extraction_benchmark import resolved_model, sha


def experiment_plan():
    series, documents, owner = [], [], 0
    for phase, cases, count in [('primary', ('long', 'boundary'), 10),
                                ('secondary', ('short', 'dense', 'none'), 3),
                                ('cold', ('long', 'boundary'), 3)]:
        for case_index, case in enumerate(cases):
            if phase == 'primary':
                schedule = ['AB']*5+['BA']*5
                random.Random(75020261031+case_index).shuffle(schedule)
            else:
                schedule = ['AB', 'BA', 'AB'] if case_index % 2 == 0 else ['BA', 'AB', 'BA']
            item = {'phase': phase, 'case_id': case, 'pairs': count, 'schedule': schedule,
                    'bootstrap_seed': 75020261041+case_index if phase == 'primary' else None}
            series.append(item)
            owners = {}
            if phase != 'cold':
                for arm, capacity in [('A', 1), ('B', 2)]:
                    owner += 1; owners[arm] = owner
                    for slot in range(1, capacity+1):
                        documents.append({'key': f'{phase}-{case}-warmup-{arm.lower()}-{slot}', 'phase': phase,
                            'case_id': case, 'source_case': 'short', 'pair': 0, 'arm': arm, 'slot': slot,
                            'purpose': 'warmup', 'owner_id': owner, 'concurrency': capacity, 'chunks': 1})
            for pair, order in enumerate(schedule, 1):
                for arm in order:
                    if phase == 'cold': owner += 1; owners[arm] = owner
                    documents.append({'key': f'{phase}-{case}-measurement-{arm.lower()}-{pair}', 'phase': phase,
                        'case_id': case, 'source_case': case, 'pair': pair, 'arm': arm, 'slot': None,
                        'purpose': 'measurement', 'owner_id': owners[arm], 'concurrency': 1 if arm=='A' else 2,
                        'chunks': 2 if case in ('long', 'boundary') else 1})
    return {'schema_version': 1, 'contract_version': 1, 'timeout_seconds': 90, 'series': series,
            'documents': documents, 'measurement_documents': 70, 'warmup_documents': 15,
            'initial_requests': 137, 'maximum_repair_requests': 137, 'maximum_requests': 274}


def percentile(values, p):
    values = sorted(values)
    position = (len(values)-1)*p; low = math.floor(position); high = math.ceil(position)
    return values[low]+(values[high]-values[low])*(position-low)


def paired_statistics(ratios, seed=None):
    result = {'median': statistics.median(ratios), 'ratios': ratios, 'bootstrap_95': None}
    if seed is not None:
        if len(ratios) != 10: raise ValueError('primary requires ten paired ratios')
        rng = random.Random(seed)
        values = [statistics.median([ratios[rng.randrange(10)] for _ in range(10)]) for _ in range(20000)]
        result['bootstrap_95'] = [percentile(values, .025), percentile(values, .975)]
    return result


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _attempts(row, attempts):
    start, count = row.get('attempt_start'), row.get('attempt_count')
    if type(start) is not int or type(count) is not int or start < 1 or count < 0 or start+count-1 > len(attempts):
        raise ValueError('attempt range')
    values = attempts[start-1:start-1+count]
    seen = set()
    for ordinal, value in enumerate(values, start):
        if (not isinstance(value, dict) or type(value.get('attempt')) is not int or value.get('attempt') != ordinal or value.get('document_key') != row['key']
                or value.get('owner_id') != row['owner_id'] or value.get('case_id') != row['source_case']
                or value.get('purpose') != row['purpose'] or value.get('repetition') != (row['pair'] or row['slot'])):
            raise ValueError('attempt linkage')
        chunk, repair = value.get('chunk'), value.get('repair')
        if (type(chunk) is not int or not 1 <= chunk <= row['chunks'] or type(repair) is not bool
                or (chunk, repair) in seen or (repair and (chunk, False) not in seen)):
            raise ValueError('call shape')
        seen.add((chunk, repair))
    return values


def _known(values):
    return bool(values) and all(isinstance(v.get('cli_call_id'), str) and re.fullmatch(r'[a-f0-9]{32}', v['cli_call_id'])
        and v.get('admission_state') == 'finished' and v.get('usage_complete') is True
        and v.get('model_identity_source') == 'claude_modelUsage' and resolved_model(v.get('resolved_model')) is not None and type(v.get('reported_usd')) in (int, float)
        and math.isfinite(v['reported_usd']) and v['reported_usd'] >= 0
        and isinstance(v.get('tokens'), dict)
        and all(type(v['tokens'].get(k)) is int and v['tokens'][k] >= 0 for k in ('input', 'output')) for v in values)


def _totals(values):
    return {'reported_usd': sum(v['reported_usd'] for v in values),
            'tokens': sum(v['tokens']['input']+v['tokens']['output'] for v in values),
            'errors': sum(v.get('status') != 'success' for v in values),
            'timeouts': sum((v.get('cli_timing') or {}).get('error_code') == 'timeout' for v in values),
            'repairs': sum(v.get('repair') is True for v in values)}


def _ratio_totals(a, b):
    if not positive(a['reported_usd']) or not positive(a['tokens']): raise ValueError('cost denominator')
    return {'reported_usd': b['reported_usd']/a['reported_usd'], 'tokens': b['tokens']/a['tokens']}


def _identity(value):
    identity = value.get('worker_identity')
    if (value.get('worker_scope') != 'extraction_pool' or not isinstance(identity, dict)
            or set(identity) != {'pool_id', 'slot_id', 'worker_id', 'generation'}
            or any(not isinstance(identity[k], str) or not re.fullmatch(r'[a-f0-9]{32}', identity[k]) for k in ('pool_id', 'worker_id'))
            or type(identity['slot_id']) is not int or identity['slot_id'] not in (1, 2)
            or type(identity['generation']) is not int or not 1 <= identity['generation'] <= 10**9):
        raise ValueError('worker identity')
    return tuple(identity[k] for k in ('pool_id', 'slot_id', 'worker_id', 'generation'))


def validate_lifecycle(row, values, warmed):
    """Shared online stop and offline comparability check; never retries a worker."""
    if row['phase'] == 'cold':
        cold_slots: dict[int, tuple] = {}
        for value in values:
            identity = _identity(value)
            if identity[1] > row['concurrency'] or identity[3] != 1: raise ValueError('cold_identity')
            previous = cold_slots.get(identity[1])
            if value.get('process_observation') != ('reused' if previous else 'started') or (previous and previous != identity):
                raise ValueError('cold_lifecycle')
            cold_slots[identity[1]] = identity
        if len({i[0] for i in cold_slots.values()}) != 1: raise ValueError('cold_pool_identity')
    elif row['purpose'] == 'warmup':
        identity = _identity(values[0])
        if identity[1] != row['slot'] or identity[3] != 1 or values[0].get('process_observation') != 'started':
            raise ValueError('warmup_identity')
        for i, value in enumerate(values):
            if _identity(value) != identity or (i and value.get('process_observation') != 'reused'):
                raise ValueError('warmup_restarted')
        if any(v[0] != identity[0] for (owner, _), v in warmed.items() if owner == row['owner_id']):
            raise ValueError('warmup_pool_identity')
        warmed[(row['owner_id'], row['slot'])] = identity
    else:
        for value in values:
            identity = _identity(value)
            if warmed.get((row['owner_id'], identity[1])) != identity or value.get('process_observation') != 'reused':
                raise ValueError('warm_measurement_not_reused')


def evaluate_report(report):
    """Recompute verdicts; stored summaries/verdicts are never inputs."""
    plan = experiment_plan(); reasons = []; comparisons = []; violations = []
    if not isinstance(report, dict) or sha(report.get('plan')) != sha(plan): return {'verdict': 'inconclusive', 'reasons': ['plan_mismatch'], 'comparisons': []}
    rows, attempts = report.get('documents'), report.get('attempts')
    if not isinstance(rows, list) or not isinstance(attempts, list):
        return {'verdict': 'inconclusive', 'reasons': ['missing_records'], 'comparisons': []}
    expected = {r['key']: r for r in plan['documents']}
    actual: dict[str, dict] = {}; linked: dict[str, list] = {}; seen: set[int] = set()
    structure_ok = True
    for row in rows:
        if not isinstance(row, dict): structure_ok = False; continue
        key = row.get('key')
        if not isinstance(key, str) or key not in expected or key in actual or any(type(row.get(k)) is not type(v) or row.get(k) != v for k, v in expected[key].items()):
            structure_ok = False; continue
        actual[key] = row
        quality = row.get('quality') or {}
        if not isinstance(quality, dict): quality = {}; structure_ok = False
        if quality.get('complete') is True and quality.get('passed') is False: violations.append('quality:'+key)
        try:
            values = _attempts(row, attempts)
            identifiers = {v['attempt'] for v in values}
            if seen & identifiers: raise ValueError('shared attempts')
            seen.update(identifiers); linked[key] = values
        except (ValueError, KeyError, TypeError): structure_ok = False
    if [r.get('key') for r in rows if isinstance(r, dict)] != [r['key'] for r in plan['documents'][:len(rows)]]:
        structure_ok = False
    if seen != set(range(1, len(attempts)+1)): structure_ok = False
    call_ids = [v.get('cli_call_id') for v in attempts if isinstance(v, dict) and isinstance(v.get('cli_call_id'), str)]
    if len(call_ids) != len(set(call_ids)): structure_ok = False
    if not structure_ok: reasons.append('record_linkage')
    models = {v.get('resolved_model') for v in attempts if isinstance(v, dict) and resolved_model(v.get('resolved_model')) is not None}
    if len(models) != 1: reasons.append('model_mismatch_or_unknown')
    if report.get('stop_reason') == 'source_changed':
        structure_ok = False; reasons.append('source_changed')
    pool_owners: dict[str, int] = {}; owner_pools: dict[int, str] = {}
    for key, values in linked.items():
        for value in values:
            try:
                pool = _identity(value)[0]; owner = actual[key]['owner_id']
                if pool in pool_owners and pool_owners[pool] != owner: structure_ok = False
                if owner in owner_pools and owner_pools[owner] != pool: structure_ok = False
                pool_owners[pool] = owner; owner_pools[owner] = pool
            except (ValueError, TypeError): pass
    for series in plan['series']:
        phase, case = series['phase'], series['case_id']
        result = {'phase': phase, 'case_id': case, 'verdict': 'inconclusive', 'reasons': [], 'statistics': None,
                  'measurement_totals': None, 'measurement_ratios': None, 'warmup_totals': None, 'series_ratios': None}
        comparisons.append(result)
        specs = [r for r in plan['documents'] if r['phase']==phase and r['case_id']==case]
        group = [actual.get(r['key']) for r in specs]
        if not structure_ok or len(models) != 1 or any(r is None for r in group):
            result['reasons'].append('missing_or_invalid_comparison'); continue
        members = [actual[r['key']] for r in specs]
        try:
            by_arm: dict[str, dict[str, list[dict]]] = {arm: {'measurement': [], 'warmup': []} for arm in ('A', 'B')}; warmed: dict[tuple, tuple] = {}
            for row in members:
                values = linked[row['key']]
                if (row.get('status') != 'success' or (row.get('quality') or {}).get('passed') is not True
                        or (row.get('quality') or {}).get('complete') is not True or not _known(values)):
                    raise ValueError('completion_quality_model_or_usage')
                if sorted(v['chunk'] for v in values if not v['repair']) != list(range(1, row['chunks']+1)):
                    raise ValueError('missing_initial_calls')
                by_arm[row['arm']][row['purpose']].extend(values)
                validate_lifecycle(row, values, warmed)
            ratios = []
            for pair in range(1, series['pairs']+1):
                times = {}
                for arm in ('A', 'B'):
                    row = next(r for r in members if r['pair']==pair and r['arm']==arm and r['purpose']=='measurement')
                    if phase == 'cold': value = row.get('cold_seconds')
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
            if result['statistics']['median'] > (.8 if phase=='primary' else 1.1): failures.append('latency_median')
            if phase == 'primary' and result['statistics']['bootstrap_95'][1] >= 1: failures.append('latency_uncertainty')
            if any(value > 1.1 for value in result['measurement_ratios'].values()): failures.append('measurement_cost_or_tokens')
            if any(totals['B'][k] > totals['A'][k] for k in ('errors', 'timeouts', 'repairs')): failures.append('error_timeout_or_repair')
            if phase == 'primary':
                whole = {arm: _totals(by_arm[arm]['measurement']+by_arm[arm]['warmup']) for arm in ('A', 'B')}
                result['series_ratios'] = _ratio_totals(whole['A'], whole['B'])
                if any(value > 1.1 for value in result['series_ratios'].values()): failures.append('series_cost_or_tokens')
            result.update(verdict='rejected' if failures else 'accepted', reasons=failures)
        except (ValueError, KeyError, TypeError, StopIteration, IndexError, AttributeError) as exc:
            result['reasons'].append(str(exc))
    if report.get('cleanup_errors', 0): reasons.append('cleanup_error')
    if report.get('synthetic_responses'): reasons.append('synthetic_only')
    if report.get('status') != 'completed': reasons.append('incomplete_execution')
    if len(actual) != len(expected): reasons.append('missing_documents')
    verdict = ('rejected' if violations or any(c['verdict']=='rejected' for c in comparisons) else
               'inconclusive' if reasons or any(c['verdict']!='accepted' for c in comparisons) else 'accepted')
    if report.get('synthetic_responses'): verdict = 'inconclusive'
    return {'verdict': verdict, 'reasons': violations+reasons, 'resolved_models': sorted(models),
            'synthetic_responses': bool(report.get('synthetic_responses')), 'comparisons': comparisons}
