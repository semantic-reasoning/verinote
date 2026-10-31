"""Deterministic decision controls for the frozen v2 experiment; no models."""
import copy
import unittest

from verinote.extraction_benchmark import sha
from verinote.extraction_options import CHUNK_THRESHOLD_POLICY
from verinote.extraction_threshold_ab import (
    evaluate_report,
    experiment_plan,
    resolved_sonnet_model,
    sonnet_model,
)
from verinote.extraction_transport import ExtractionSpec

MODEL = 'claude-sonnet-5-5'


def perfect_report():
    plan = experiment_plan(); rows = []; attempts = []; started = set()
    for spec in plan['documents']:
        seconds = 10 if spec['arm'] == 'A' or spec['concurrency'] == 1 else 7
        transport = ExtractionSpec('/fake/claude', 'sonnet', spec['concurrency'], 90, 1048576, 'null').to_dict()
        options = {'schema_version': 1, 'concurrency': spec['concurrency'], 'transport': transport, 'document_generation': 1}
        if spec['extraction_policy'] is not None:
            options.update(schema_version=2, policy=CHUNK_THRESHOLD_POLICY, original_chunk_count=spec['chunks'])
        row = spec | {'status': 'success', 'extraction_contract': 2, 'extraction_options': options,
            'quality': {'complete': True, 'passed': True}, 'safety': {'complete': True, 'passed': True},
            'document_timing': {'available': True, 'status': 'success', 'summary': {'total_seconds': seconds}},
            'cold_seconds': seconds, 'cleanup_succeeded': True, 'attempt_start': len(attempts)+1,
            'attempt_count': spec['chunks']}
        for index in range(1, spec['chunks']+1):
            slot = spec['slot'] or ((index-1) % spec['concurrency']+1)
            identity = {'pool_id': f'{spec["owner_id"]:032x}', 'slot_id': slot,
                        'worker_id': f'{spec["owner_id"]*10+slot:032x}', 'generation': 1}
            key = (spec['owner_id'], slot); observation = 'reused' if key in started else 'started'
            started.add(key)
            attempts.append({'attempt': len(attempts)+1, 'cli_call_id': f'{len(attempts)+1000:032x}',
                'document_key': spec['key'], 'owner_id': spec['owner_id'], 'case_id': spec['source_case'],
                'repetition': spec['pair'] or spec['slot'], 'purpose': spec['purpose'], 'chunk': index,
                'repair': False, 'status': 'success', 'admission_state': 'finished', 'usage_complete': True,
                'resolved_model': MODEL, 'model_identity_source': 'claude_modelUsage', 'reported_usd': 1.,
                'tokens': {'input': 100, 'output': 20}, 'worker_scope': 'extraction_pool',
                'worker_identity': identity, 'process_observation': observation, 'cli_timing': {}})
        rows.append(row)
    return {'schema_version': 2, 'plan': plan, 'documents': rows, 'attempts': attempts,
            'status': 'completed', 'cleanup_errors': 0, 'synthetic_responses': False}


def truncate(report, count):
    report['documents'] = report['documents'][:count]
    last = report['documents'][-1]
    report['attempts'] = report['attempts'][:last['attempt_start']+last['attempt_count']-1]
    report['status'] = 'stopped'


def repair(report, row, chunk=1):
    position = row['attempt_start']+row['attempt_count']-1
    first = next(v for v in report['attempts'] if v['document_key'] == row['key'] and v['chunk'] == chunk)
    value = copy.deepcopy(first)
    value.update(repair=True, cli_call_id=f'{100000+len(report["attempts"]):032x}', process_observation='reused')
    report['attempts'].insert(position, value)
    row['attempt_count'] += 1
    for other in report['documents']:
        if other is not row and other['attempt_start'] > position: other['attempt_start'] += 1
    for i, attempt in enumerate(report['attempts'], 1): attempt['attempt'] = i


class ThresholdContract(unittest.TestCase):
    def test_sonnet_family_required_for_requested_and_resolved_models(self):
        for model in ('sonnet', MODEL, 'claude-3-sonnet-20240229'):
            self.assertEqual(sonnet_model(model), model)
        for model in ('haiku', 'claude-haiku-4-5', 'claude-sonnet-4-latest'):
            with self.assertRaises(ValueError): sonnet_model(model)
            self.assertFalse(resolved_sonnet_model(model))
        report = perfect_report()
        for attempt in report['attempts']: attempt['resolved_model'] = 'claude-haiku-4-5'
        result = evaluate_report(report)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('model_family', result['reasons'])

    def test_plan_counts_order_policy_and_owners(self):
        plan = experiment_plan(); docs = plan['documents']
        self.assertEqual([(s['phase'], s['chunks']) for s in plan['series']],
                         [('cold', 3), ('cold', 4), ('cold', 8), ('warm', 3), ('warm', 4), ('warm', 8)])
        self.assertEqual(len(docs), 128)
        self.assertEqual(sum(d['purpose'] == 'measurement' for d in docs), 120)
        self.assertEqual(sum(d['chunks'] for d in docs), 608)
        self.assertEqual(sum(min(2, d['chunks']) for d in docs), 248)
        self.assertEqual(plan['maximum_requests'], 856)
        self.assertEqual(len({d['owner_id'] for d in docs}), 66)
        self.assertEqual(sha(plan), 'edef365b348cd9110bd742835da42edd598998cdac62fa8614081e3235d86eec')
        self.assertEqual([''.join(order[0] for order in s['schedule']) for s in plan['series']],
                         ['BAABBAAABB', 'ABABBABBAA', 'BBBABBAAAA', 'BBAABABAAB', 'BAABABAABB', 'AAABBABABB'])
        for i, series in enumerate(plan['series']):
            self.assertEqual(series['bootstrap_seed'], 76420261111+i)
            self.assertEqual(sorted(series['schedule']), ['AB']*5+['BA']*5)
        for d in docs:
            if d['purpose'] == 'measurement':
                self.assertEqual(d['concurrency'], 1 if d['arm'] == 'A' or d['chunks'] == 3 else 2)
                self.assertEqual(d['extraction_policy'], CHUNK_THRESHOLD_POLICY if d['arm'] == 'B' else None)
            else:
                self.assertEqual(d['phase'], 'warm'); self.assertIsNone(d['extraction_policy'])

    def test_all_six_complete_comparisons_required_for_acceptance(self):
        result = evaluate_report(perfect_report())
        self.assertEqual(result['verdict'], 'accepted', result)
        self.assertEqual(len(result['comparisons']), 6)
        self.assertTrue(all(c['statistics']['bootstrap_95'] for c in result['comparisons']))
        self.assertTrue(all(c['series_ratios'] for c in result['comparisons'] if c['phase'] == 'warm'))

    def test_three_chunk_has_no_confidence_interval_gate(self):
        report = perfect_report()
        for row in report['documents']:
            if row['case_id'] == 'three' and row['arm'] == 'B' and row['purpose'] == 'measurement':
                row['cold_seconds'] = 11
                row['document_timing']['summary']['total_seconds'] = 11
        result = evaluate_report(report)
        self.assertEqual(result['verdict'], 'accepted', result)
        self.assertGreater(result['comparisons'][0]['statistics']['bootstrap_95'][1], 1)

    def test_four_and_eight_require_median_and_confidence_in_both_phases(self):
        for phase, case in [('cold', 'four'), ('cold', 'eight'), ('warm', 'four'), ('warm', 'eight')]:
            report = perfect_report()
            for row in report['documents']:
                if (row['phase'], row['case_id'], row['arm'], row['purpose']) == (phase, case, 'B', 'measurement'):
                    value = 5 if row['pair'] <= 6 else 12
                    row['cold_seconds'] = value; row['document_timing']['summary']['total_seconds'] = value
            result = evaluate_report(report)
            comparison = next(c for c in result['comparisons'] if (c['phase'], c['case_id']) == (phase, case))
            self.assertEqual(result['verdict'], 'rejected')
            self.assertLess(comparison['statistics']['median'], .8)
            self.assertIn('latency_uncertainty', comparison['reasons'])

    def test_warmup_inclusive_cost_and_tokens_apply_to_three_chunks_too(self):
        for key in ('reported_usd', 'tokens'):
            report = perfect_report()
            for v in report['attempts']:
                if v['document_key'] == 'warm-three-warmup-b-1':
                    if key == 'tokens': v['tokens']['input'] = 10000
                    else: v['reported_usd'] = 100
            result = evaluate_report(report)
            c = result['comparisons'][3]
            self.assertEqual(result['verdict'], 'rejected')
            self.assertEqual(c['measurement_ratios'], {'reported_usd': 1., 'tokens': 1.})
            self.assertIn('series_cost_or_tokens', c['reasons'])

    def test_warmup_repair_cannot_be_hidden_by_clean_measurements(self):
        report = perfect_report()
        row = next(r for r in report['documents'] if r['key'] == 'warm-three-warmup-b-1')
        repair(report, row)
        result = evaluate_report(report)
        self.assertEqual(result['verdict'], 'rejected', result)
        self.assertIn('series_error_timeout_or_repair', result['comparisons'][3]['reasons'])

    def test_measurement_cost_and_tokens_are_gated_separately(self):
        for kind in ('reported_usd', 'tokens'):
            report = perfect_report()
            for value in report['attempts']:
                if value['document_key'].startswith('cold-four-measurement-b-'):
                    if kind == 'reported_usd': value[kind] = 1.2
                    else: value['tokens']['input'] = 130
            result = evaluate_report(report)
            self.assertEqual(result['verdict'], 'rejected')
            self.assertIn('measurement_cost_or_tokens', result['comparisons'][1]['reasons'])

    def test_confirmed_quality_safety_and_complete_failure_survive_incomplete_other_series(self):
        for kind in ('quality', 'safety'):
            report = perfect_report(); truncate(report, 1)
            report['documents'][0][kind]['passed'] = False
            self.assertEqual(evaluate_report(report)['verdict'], 'rejected')
        report = perfect_report(); truncate(report, 20)
        for row in report['documents']:
            if row['arm'] == 'B': row['cold_seconds'] = 20
        result = evaluate_report(report)
        self.assertEqual(result['verdict'], 'rejected', result)
        self.assertEqual(result['comparisons'][0]['verdict'], 'rejected')
        self.assertEqual(result['comparisons'][1]['verdict'], 'inconclusive')

    def test_missing_duplicate_reordered_or_unlinked_records_never_pass(self):
        for kind in ('missing', 'duplicate', 'reordered', 'extra_call', 'duplicate_call'):
            report = perfect_report()
            if kind == 'missing': report['documents'].pop()
            elif kind == 'duplicate': report['documents'].append(copy.deepcopy(report['documents'][0]))
            elif kind == 'reordered': report['documents'][:2] = reversed(report['documents'][:2])
            elif kind == 'extra_call': report['attempts'].append(copy.deepcopy(report['attempts'][0]))
            else: report['attempts'][1]['cli_call_id'] = report['attempts'][0]['cli_call_id']
            self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive', kind)

    def test_b_measurement_requires_real_registered_threshold_policy(self):
        for kind in ('explicit', 'count', 'generation', 'timeout'):
            report = perfect_report()
            row = next(r for r in report['documents'] if r['key'] == 'cold-four-measurement-b-1')
            options = row['extraction_options']
            if kind == 'explicit':
                options['schema_version'] = 1; options.pop('policy'); options.pop('original_chunk_count')
            elif kind == 'count': options['original_chunk_count'] = 8
            elif kind == 'generation': options['document_generation'] = 2
            else: options['transport']['timeout'] = 180
            self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive', kind)

    def test_worker_restarts_unknown_usage_and_shared_pools_are_inconclusive(self):
        for kind in ('restart', 'unknown', 'shared_pool', 'shared_worker', 'owner_change', 'other_transport'):
            report = perfect_report()
            if kind == 'restart':
                v = next(v for v in report['attempts'] if v['document_key'] == 'warm-four-measurement-b-1')
                v['worker_identity']['generation'] = 2; v['process_observation'] = 'restarted'
            elif kind == 'unknown': report['attempts'][0]['usage_complete'] = False
            elif kind == 'shared_pool':
                report['attempts'][3]['worker_identity']['pool_id'] = report['attempts'][0]['worker_identity']['pool_id']
            elif kind == 'shared_worker':
                report['attempts'][3]['worker_identity']['worker_id'] = report['attempts'][0]['worker_identity']['worker_id']
            elif kind == 'owner_change': report['attempts'][1]['worker_identity']['pool_id'] = 'f'*32
            else:
                for row in report['documents']:
                    if row['case_id'] == 'eight': row['extraction_options']['transport']['max_output'] = 2048
            self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive', kind)

    def test_repair_limit_cleanup_and_synthetic_records_do_not_pass(self):
        report = perfect_report(); row = report['documents'][0]
        for chunk in (1, 2, 3): repair(report, row, chunk)
        self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive')
        for mutate in ('cleanup', 'cold_cleanup', 'synthetic', 'source_changed'):
            report = perfect_report()
            if mutate == 'cleanup': report['cleanup_errors'] = 1
            elif mutate == 'cold_cleanup': report['documents'][0]['cleanup_succeeded'] = False
            elif mutate == 'source_changed': report['stop_reason'] = 'source_changed'
            else: report['synthetic_responses'] = True
            self.assertEqual(evaluate_report(report)['verdict'], 'inconclusive', mutate)
