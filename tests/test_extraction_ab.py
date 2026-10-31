"""Deterministic records for the frozen A/B decision contract; no models."""
import copy
import unittest

from verinote.extraction_ab import (
    _totals,
    evaluate_report,
    experiment_plan,
    paired_statistics,
    percentile,
)
from verinote.extraction_benchmark import resolved_model

MODEL = 'claude-sonnet-4-5-20250929'


def perfect_report():
    plan = experiment_plan(); rows = []; attempts = []
    for spec in plan['documents']:
        row = dict(spec, status='success', quality={'complete': True, 'passed': True},
                   document_timing={'available': True, 'status': 'success', 'summary': {'total_seconds': 7.5 if spec['arm']=='B' and spec['phase']=='primary' else 10}},
                   cold_seconds=10, attempt_start=len(attempts)+1)
        count = 2 if spec['source_case'] in ('long', 'boundary') else 1
        row['attempt_count'] = count
        for index in range(1, count+1):
            slot = spec['slot'] or (index if spec['concurrency']==2 else 1)
            identity = {'pool_id': f'{spec["owner_id"]:032x}', 'slot_id': slot,
                        'worker_id': f'{spec["owner_id"]*10+slot:032x}', 'generation': 1}
            cold = spec['purpose']=='warmup' or spec['phase']=='cold'
            started = cold and (index==1 or spec['concurrency']==2)
            attempts.append({'attempt': len(attempts)+1, 'cli_call_id': f'{len(attempts)+1000:032x}', 'document_key': spec['key'], 'owner_id': spec['owner_id'],
                'case_id': spec['source_case'], 'repetition': spec['pair'] or spec['slot'], 'purpose': spec['purpose'],
                'chunk': index, 'repair': False, 'status': 'success', 'admission_state': 'finished',
                'usage_complete': True, 'resolved_model': MODEL, 'model_identity_source': 'claude_modelUsage',
                'reported_usd': 1.0, 'tokens': {'input': 100, 'output': 20}, 'worker_scope': 'extraction_pool',
                'worker_identity': identity, 'process_observation': 'started' if started else 'reused', 'cli_timing': {}})
        rows.append(row)
    return {'schema_version': 1, 'plan': plan, 'documents': rows, 'attempts': attempts,
            'cleanup_errors': 0, 'status': 'completed', 'synthetic_responses': False}


def truncate(report, documents):
    report['documents'] = report['documents'][:documents]
    last = report['documents'][-1]
    report['attempts'] = report['attempts'][:last['attempt_start']-1+last['attempt_count']]
    report['status'] = 'stopped'


class FrozenContract(unittest.TestCase):
    def test_cost_sum_is_stable_across_python_versions(self):
        rows = [{'reported_usd': cost, 'tokens': {'input': 1, 'output': 2}}
                for cost in [1e16, 1.0, 1.0]]
        totals = _totals(rows)
        self.assertEqual(totals['reported_usd'], 10000000000000002.0)
        self.assertEqual(totals['tokens'], 9)
        self.assertIs(type(totals['tokens']), int)
        self.assertEqual(_totals([])['reported_usd'], 0)

    def test_actual_model_allowlist_does_not_export_aliases_or_provider_text(self):
        for value in ('sonnet', 'claude-sonnet-latest', 'claude-sonnet-4-latest', '(multiple models)', 'PRIVATE_NESTED', None):
            self.assertIsNone(resolved_model(value))
        for value in (MODEL, 'claude-3-5-sonnet-20241022', 'claude-opus-4-6'):
            self.assertEqual(resolved_model(value), value)

    def test_schedule_counts_and_fixed_bootstrap(self):
        plan = experiment_plan()
        self.assertEqual(len(plan['documents']), 85)
        self.assertEqual(sum(r['purpose']=='warmup' for r in plan['documents']), 15)
        self.assertEqual(len(perfect_report()['attempts']), 137)
        self.assertEqual(plan['maximum_requests'], 274)
        self.assertEqual(plan['series'][0]['schedule'], ['BA', 'BA', 'AB', 'AB', 'BA', 'AB', 'AB', 'AB', 'BA', 'BA'])
        self.assertEqual(percentile([1, 2, 3, 4], .25), 1.75)
        value = paired_statistics([.8]*10, 75020261041)
        self.assertEqual(value['median'], .8); self.assertEqual(value['bootstrap_95'], [.8, .8])
        self.assertEqual(paired_statistics([.5+i/10 for i in range(10)], 75020261041)['bootstrap_95'], [.7, 1.2000000000000002])

    def test_full_complete_comparison_is_accepted_and_warmup_cost_is_separate(self):
        result = evaluate_report(perfect_report())
        self.assertEqual(result['verdict'], 'accepted', result)
        primary, secondary = result['comparisons'][0], result['comparisons'][2]
        self.assertAlmostEqual(primary['series_ratios']['reported_usd'], 22/21)
        self.assertEqual(secondary['warmup_totals']['B']['reported_usd'], 2)
        self.assertIsNone(secondary['series_ratios'])
        self.assertEqual(secondary['measurement_ratios']['reported_usd'], 1)

    def test_exact_latency_boundaries_and_cost_sum_not_mean(self):
        report = perfect_report()
        for row in report['documents']:
            if row['purpose']=='measurement' and row['arm']=='B':
                row['document_timing']['summary']['total_seconds'] = 8 if row['phase']=='primary' else 11
                row['cold_seconds'] = 11
        self.assertEqual(evaluate_report(report)['verdict'], 'accepted')
        row = next(r for r in report['documents'] if r['phase']=='secondary' and r['arm']=='B' and r['pair']==2)
        row['document_timing']['summary']['total_seconds'] = 12
        # One of three still has median 11; a second violating pair changes the median.
        self.assertEqual(evaluate_report(report)['verdict'], 'accepted')
        row = next(r for r in report['documents'] if r['phase']=='secondary' and r['arm']=='B' and r['pair']==3)
        row['document_timing']['summary']['total_seconds'] = 12
        self.assertEqual(evaluate_report(report)['verdict'], 'rejected')
        report = perfect_report()
        next(a for a in report['attempts'] if a['document_key']=='secondary-short-measurement-b-1')['reported_usd'] = 2
        result = evaluate_report(report)
        self.assertEqual(result['verdict'], 'rejected')
        self.assertAlmostEqual(result['comparisons'][2]['measurement_ratios']['reported_usd'], 4/3)

    def test_unknown_usage_model_and_zero_denominator_are_inconclusive(self):
        for field, value in [('resolved_model', None), ('resolved_model', '(multiple models)'), ('usage_complete', False)]:
            with self.subTest(field=field):
                report = perfect_report(); report['attempts'][0][field] = value
                self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')
        report = perfect_report(); report['attempts'][0]['resolved_model'] = 'claude-opus-4-6'
        self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')
        report = perfect_report()
        for a in report['attempts']:
            if a['document_key'].startswith('primary-long-measurement-a'): a['reported_usd'] = 0
        self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')

    def test_partial_data_does_not_hide_complete_failure_or_confirm_partial_ratios(self):
        report = perfect_report(); truncate(report, 23)
        for row in report['documents']:
            if row['arm']=='B': row['document_timing']['summary']['total_seconds'] = 12
        self.assertEqual(evaluate_report(report)['verdict'], 'rejected')
        truncate(report, 5)
        self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')
        report['documents'][0]['quality']['passed'] = False
        self.assertEqual(evaluate_report(report)['verdict'], 'rejected')

    def test_restarts_owner_sharing_unknown_identity_and_duplicate_rows(self):
        for mutation in ('restart', 'identity', 'shared', 'duplicate', 'missing', 'extra'):
            with self.subTest(mutation=mutation):
                report = perfect_report()
                if mutation=='restart': report['attempts'][3]['process_observation']='restarted'
                elif mutation=='identity': report['attempts'][3]['worker_identity']=None
                elif mutation=='shared': report['attempts'][1]['worker_identity']['pool_id']=report['attempts'][0]['worker_identity']['pool_id']
                elif mutation=='duplicate': report['documents'].append(copy.deepcopy(report['documents'][0]))
                elif mutation=='missing': report['documents'].pop(3)
                else: report['documents'].append({'key':'unknown'})
                self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')

    def test_synthetic_records_never_authorize_operational_adoption(self):
        report = perfect_report(); report['synthetic_responses'] = True
        self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')
