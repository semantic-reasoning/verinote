"""Only local fake API responses; production Service and durable evidence are real."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from verinote import extraction_ollama_benchmark as benchmark
from verinote.documents import chunks
from verinote.extraction_ab_runner import file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.usage import atomic_json


class OllamaBaseline(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.output = self.root/'result'; self.calls = []
        self.oracle = {}
        for name in ('short', 'three', 'four', 'eight'):
            case = load_case(name, DATA_ROOT/'v2')
            for (offset, text), output in zip(chunks(case['source']), ideal_outputs(case, contract_version=2), strict=True):
                self.oracle[(offset, text)] = output['output']
        self.mode = None; self.generation_count = 0
        self.api = patch('verinote.ollama.request', side_effect=self.request).start(); self.addCleanup(patch.stopall)

    def request(self, path, payload=None, **kwargs):
        if path == '/api/version': return {'version': benchmark._profile().version}
        if path == '/api/tags':
            digest = '0'*64 if self.mode == 'digest' and self.generation_count else benchmark._profile().model_digest
            return {'models': [{'name': benchmark._profile().model, 'digest': digest}]}
        self.assertEqual(path, '/api/generate'); self.generation_count += 1
        self.calls.append(copy.deepcopy(payload))
        data = json.loads(payload['prompt'].split('입력(JSON):\n')[1])
        value = copy.deepcopy(self.oracle[data['offset'], data['text']])
        if self.mode == 'repair' and self.generation_count == 1:
            value['facts'][0]['quote'] = 'not in source'
        elif 'issues' in data:
            value = {'repairs': [{'path': issue['path'], 'quote': value[issue['path'].split('.')[0]][int(issue['path'].split('.')[1])]['quote']} for issue in data['issues']]}
        if self.mode == 'gold' and 'coverage' in value:
            next(row for row in value['coverage'] if row['predicate'] == 'archives')['status'] = 'no_evidence'
        if self.mode == 'failure' and self.generation_count == 3: value = {}
        response = {'model': benchmark._profile().model, 'done': True, 'done_reason': 'stop',
                    'response': json.dumps(value, ensure_ascii=False), 'prompt_eval_count': 100, 'eval_count': 50,
                    'total_duration': 10000000, 'load_duration': 1000000,
                    'prompt_eval_duration': 2000000, 'eval_duration': 7000000}
        if self.mode == 'unknown': response.pop('prompt_eval_count')
        if self.mode == 'model': response['model'] = 'different:7b'
        if self.mode == 'tokens': response['prompt_eval_count'] = 2500000
        if self.mode == 'timeout': raise TimeoutError('synthetic timeout')
        return response

    def run_fake(self): return benchmark.run(self.output, synthetic=True)

    def reindex(self, report):
        atomic_json(self.output/'report.json', report)
        atomic_json(self.output/'index.json', file_index(self.output, excluded=('index.json', 'evaluation.json')))

    def test_fixed_plan_and_full_schedule_reassessed_without_http(self):
        plan = benchmark.plan()
        self.assertEqual(sum(r['chunks'] for r in plan['documents']), 46)
        self.assertEqual(len(plan['documents']), 10)
        self.assertEqual(sum(min(2, r['chunks']) for r in plan['documents']), 19)
        report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(report['attempts']), 46)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertEqual(report['budget']['observed_tokens'], 6900)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['reasons'], ['synthetic_only'])
        self.assertTrue(all(r['seconds'] is not None for r in result['series']))
        self.assertTrue(all(r['quality']['passed'] and r['safety']['passed'] for r in report['documents']))
        for call in self.calls:
            self.assertEqual(call['options'], {'num_ctx': 32768, 'num_predict': 4096, 'temperature': 0})
        before = file_index(self.output)
        with patch('verinote.ollama.request', side_effect=AssertionError('offline HTTP')):
            self.assertEqual(benchmark.evaluate(self.output), result)
        self.assertEqual(file_index(self.output), before)

    def test_unknown_usage_model_digest_token_and_timeout_stop_admission(self):
        for mode in ('unknown', 'model', 'digest', 'tokens', 'timeout'):
            with self.subTest(mode=mode):
                self.mode = mode; self.output = self.root/mode; self.generation_count = 0
                report, result = self.run_fake()
                self.assertEqual(self.generation_count, 1)
                self.assertEqual(report['status'], 'stopped')
                self.assertEqual(report['budget']['pending'], 0)
                self.assertEqual(result['verdict'], 'inconclusive')
                self.assertEqual(len(list(self.output.rglob('usage.json'))), 1)

    def test_repair_raw_response_and_publication_are_reassessed(self):
        self.mode = 'repair'
        report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(report['attempts']), 47)
        self.assertEqual(sum(a['repair'] for a in report['attempts']), 1)
        repair = next(self.output.rglob('changes.json'))
        self.assertTrue((repair.parent/'response.json').is_file())
        self.assertEqual(benchmark.evaluate(self.output), result)
        (repair.parent/'usage.json').unlink()
        self.assertIn('artifact_verification_failed', benchmark.evaluate(self.output)['reasons'])

    def test_failure_has_no_partial_publication(self):
        self.mode = 'failure'
        report, _ = self.run_fake()
        self.assertEqual(self.generation_count, 3)
        row = report['documents'][-1]
        self.assertEqual(row['status'], 'error')
        with closing(sqlite3.connect(self.output/row['artifact']/'state.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE kind!='ontology'").fetchone()[0], 0)
        self.assertEqual(report['budget']['pending'], 0)

    def test_retained_usage_and_checkpoint_cannot_be_forged_by_reindex(self):
        report, _ = self.run_fake()
        first = report['documents'][0]
        usage_path = next((self.output/first['artifact']).rglob('usage.json'))
        usage = json.loads(usage_path.read_text()); usage['tokens']['input'] += 1; atomic_json(usage_path, usage)
        report['attempts'][0]['input_tokens'] += 1
        first['artifact_files'] = file_index(self.output/first['artifact']); self.reindex(report)
        result = benchmark.evaluate(self.output)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('artifact_verification_failed', result['reasons'])

    def test_later_corruption_preserves_verified_safety_failure(self):
        report, _ = self.run_fake()
        first = report['documents'][0]
        with closing(sqlite3.connect(self.output/first['artifact']/'state.sqlite3')) as db:
            db.execute("UPDATE items SET status='approved' WHERE kind='fact'"); db.commit()
        first['safety'] = {'complete': True, 'passed': False, 'violations': ['human_approval_contract']}
        first['artifact_files'] = file_index(self.output/first['artifact']); self.reindex(report)
        reason = 'safety:'+first['key']
        self.assertIn(reason, benchmark.evaluate(self.output)['reasons'])
        later = self.output/report['documents'][1]['artifact']/'state.sqlite3'
        later.unlink(); later.symlink_to(self.root/'missing')
        result = benchmark.evaluate(self.output)
        self.assertIn(reason, result['reasons']); self.assertTrue(result['unverified_other_records'])
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_metadata_damage_drops_untrusted_summary(self):
        self.run_fake()
        (self.output/'environment.json').write_text('{}')
        result = benchmark.evaluate(self.output)
        self.assertEqual(result['reasons'], ['artifact_verification_failed'])

    def test_real_classification_complete_and_failed_without_real_calls(self):
        # Classification control, not real benchmark evidence.
        report, _ = self.run_fake()
        report['synthetic_responses'] = False
        report['environment'].update(synthetic=False, revision='a'*40, tree='b'*40)
        atomic_json(self.output/'environment.json', report['environment']); self.reindex(report)
        self.assertEqual(benchmark.evaluate(self.output)['verdict'], 'complete')
        row = report['documents'][0]
        with closing(sqlite3.connect(self.output/row['artifact']/'state.sqlite3')) as db:
            db.execute("UPDATE items SET status='approved' WHERE kind='fact'"); db.commit()
        row['safety'] = {'complete': True, 'passed': False, 'violations': ['human_approval_contract']}
        row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
        self.assertEqual(benchmark.evaluate(self.output)['verdict'], 'failed')

    def test_time_and_request_limits_block_before_admission(self):
        clock = [0]
        ledger = benchmark.Ledger(lambda: clock[0]); clock[0] = benchmark.LIMITS['seconds']
        with self.assertRaises(benchmark.Stopped): ledger.check()
        self.assertEqual(ledger.reason, 'time_limit')
        ledger = benchmark.Ledger()
        ledger.attempts = [{'status': 'success', 'input_tokens': 1, 'output_tokens': 1} for _ in range(65)]
        with self.assertRaises(benchmark.Stopped): ledger.check()
        self.assertEqual(ledger.reason, 'request_limit')

    def test_record_failure_preserves_finished_usage(self):
        original = benchmark.ReportWriter.write; failed = False
        def write(writer, report):
            nonlocal failed
            if not failed and any(a['status'] == 'success' for a in report['attempts']):
                failed = True
                raise OSError('synthetic record failure')
            return original(writer, report)
        with patch.object(benchmark.ReportWriter, 'write', new=write):
            report, result = self.run_fake()
        self.assertEqual(report['stop_reason'], 'report_write_failed')
        self.assertEqual(self.generation_count, 1)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertEqual(len(list(self.output.rglob('usage.json'))), 1)
        self.assertEqual(result['verdict'], 'inconclusive')


    def test_source_change_stops_adoption_without_mutating_source(self):
        original = benchmark._hash
        def changed(path):
            value = original(path)
            if self.generation_count and Path(path) == benchmark.REPO/'verinote/agents.py': return '0'*64
            return value
        with patch.object(benchmark, '_hash', side_effect=changed):
            report, result = self.run_fake()
        self.assertEqual(report['stop_reason'], 'source_changed')
        self.assertEqual(result['verdict'], 'inconclusive')



    def test_final_hashing_and_reassessment_are_in_last_wall_observation(self):
        clock = [0.0]; charged = False
        original_evaluate = benchmark.evaluate
        def indexing(root, **kwargs):
            nonlocal charged
            value = file_index(root, **kwargs)
            if Path(root) == self.output and not charged:
                charged = True; clock[0] += benchmark.LIMITS['seconds']-2
            return value
        def assessing(root):
            value = original_evaluate(root); clock[0] += 7
            return value
        with patch.object(benchmark, 'file_index', side_effect=indexing), patch.object(benchmark, 'evaluate', side_effect=assessing):
            report, result = benchmark.run(self.output, synthetic=True, clock=lambda: clock[0])
        self.assertEqual(report['budget']['elapsed_seconds'], benchmark.LIMITS['seconds']+5)
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(report['stop_reason'], 'time_limit')
        self.assertIn('incomplete_baseline', result['reasons'])
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(original_evaluate(self.output), result)


    def test_gold_failure_continues_and_latency_does_not_select_only_passes(self):
        self.mode = 'gold'
        report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(self.generation_count, 46)
        self.assertTrue(all(r['status'] == 'success' and not r['quality']['passed'] for r in report['documents']))
        self.assertTrue(result['execution_complete'])
        for series in result['series']:
            self.assertEqual(series['attempted_repetitions'], 3)
            self.assertEqual(series['service_completed_repetitions'], 3)
            self.assertEqual(series['gold_passed_repetitions'], 0)
            self.assertIsNotNone(series['seconds'])
        report['synthetic_responses'] = False
        report['environment'].update(synthetic=False, revision='a'*40, tree='b'*40)
        atomic_json(self.output/'environment.json', report['environment']); self.reindex(report)
        checked = benchmark.evaluate(self.output)
        self.assertEqual(checked['verdict'], 'failed')
        self.assertNotIn('incomplete_baseline', checked['reasons'])

    def test_profile_is_pinned_across_real_worker_threads_and_restored(self):
        token = benchmark._ACTIVE.set(benchmark.LEGACY)
        try:
            self.mode = 'repair'
            report, result = self.run_fake()
            self.assertIs(benchmark._profile(), benchmark.LEGACY)
            self.assertEqual(report['plan']['model'], benchmark.CURRENT.model)
            self.assertEqual(report['limits']['seconds'], 14400)
            self.assertEqual(self.generation_count, 47)
            self.assertEqual(benchmark.evaluate(self.output), result)
            self.assertIs(benchmark._profile(), benchmark.LEGACY)
            self.output = self.root/'exception'; self.mode = 'timeout'; self.generation_count = 0
            stopped, _ = self.run_fake()
            self.assertEqual(stopped['status'], 'stopped')
            self.assertIs(benchmark._profile(), benchmark.LEGACY)
        finally: benchmark._ACTIVE.reset(token)

    def test_legacy_quality_stop_and_retained_actual_evidence_are_unchanged(self):
        token = benchmark._ACTIVE.set(benchmark.LEGACY)
        try:
            self.mode = 'gold'
            report, _ = benchmark._run(self.output, synthetic=True)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(self.generation_count, 1)
            self.assertEqual(report['stop_reason'], 'document_failed')
        finally: benchmark._ACTIVE.reset(token)
        historical = Path(__file__).resolve().parents[1]/'benchmarks/extraction/results/ollama-qwen25-7b-baseline'
        before = file_index(historical)
        with patch('verinote.ollama.request', side_effect=AssertionError('offline HTTP')), patch('subprocess.run', side_effect=AssertionError('offline subprocess')):
            self.assertEqual(benchmark.evaluate(historical), json.loads((historical/'evaluation.json').read_text()))
        self.assertEqual(before, file_index(historical))

    def test_unknown_or_inconsistent_profile_is_not_interpreted(self):
        report, _ = self.run_fake()
        report['plan']['profile'] = 'unknown'
        atomic_json(self.output/'plan.json', report['plan']); self.reindex(report)
        self.assertEqual(benchmark.evaluate(self.output)['verdict'], 'inconclusive')


if __name__ == '__main__': unittest.main()
