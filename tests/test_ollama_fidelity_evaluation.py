"""Local fake API responses exercise the real Service and retained evidence checks."""
import copy
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from verinote import ollama_fidelity_evaluation as trial
from verinote.extraction_ab_runner import file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.usage import atomic_json


class FidelityTrial(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.output = self.root/'result'
        self.ideal = ideal_outputs(load_case('short', DATA_ROOT/'v2'), contract_version=2)[0]['output']
        self.calls = []; self.mode = None; self.ticks = 0
        patch('verinote.ollama.request', side_effect=self.request).start(); self.addCleanup(patch.stopall)

    def request(self, path, payload=None, **kwargs):
        if path == '/api/version':
            if self.mode == 'identity_unknown' and self.calls: raise TimeoutError('synthetic identity probe timeout')
            return {'version': trial.VERSION}
        if path == '/api/tags':
            identity = '0'*64 if self.mode == 'digest' and self.calls else trial.MODEL_DIGEST
            return {'models': [{'name': trial.MODEL, 'digest': identity}]}
        self.assertEqual(path, '/api/generate'); self.calls.append(copy.deepcopy(payload))
        data = json.loads(payload['prompt'].split('입력(JSON):\n')[1])
        output = copy.deepcopy(self.ideal)
        if self.mode in ('repair', 'bad_repair', 'repair_tokens', 'repair_time'):
            if len(self.calls) == 1: output['facts'][0]['quote'] = 'deliberately absent quotation'
            else:
                output = {'repairs': [{'path': issue['path'], 'quote': self.ideal[issue['path'].split('.')[0]][int(issue['path'].split('.')[1])]['quote']} for issue in data['issues']]}
                if self.mode == 'bad_repair': output = {'repairs': []}
        if self.mode == 'mechanical': output['coverage'][0]['fact_indices'] = []
        if self.mode == 'contract': output['coverage'][0]['predicate'] = 'invented_relation'
        if self.mode in ('gold', 'gold_tokens'):
            output['facts'] = []; output['coverage'][0].update(status='not_representable', fact_indices=[])
        if self.mode == 'timeout': raise TimeoutError('synthetic timeout')
        if self.mode == 'repair_time': self.ticks = 601
        result = {'model': trial.MODEL, 'done': True, 'done_reason': 'stop',
                  'response': json.dumps(output, ensure_ascii=False), 'prompt_eval_count': 100, 'eval_count': 50,
                  'total_duration': 1000000, 'load_duration': 100000, 'prompt_eval_duration': 200000, 'eval_duration': 700000}
        if self.mode == 'malformed': result['response'] = '{not json'
        if self.mode == 'unknown': result.pop('prompt_eval_count')
        if self.mode == 'truncated': result['done_reason'] = 'length'
        if self.mode in ('repair_tokens', 'gold_tokens'): result['prompt_eval_count'] = 100000
        return result

    def run_fake(self): return trial.run(self.output, synthetic=True, clock=lambda: self.ticks)

    def reindex(self, report):
        atomic_json(self.output/'report.json', report)
        atomic_json(self.output/'index.json', file_index(self.output, excluded=('index.json', 'evaluation.json')))

    def actual_classification(self, report):
        # Fabricated classification control, never real-evidence claims or model calls.
        report['synthetic_responses'] = False
        report['environment'].update(synthetic=False, revision='a'*40, tree='b'*40)
        atomic_json(self.output/'environment.json', report['environment']); self.reindex(report)
        return trial.evaluate(self.output)

    def test_one_initial_success_and_readonly_reassessment(self):
        report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['reasons'], ['synthetic_only'])
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertTrue(result['production']['passed'] and result['quality']['passed'] and result['safety']['passed'])
        self.assertEqual(report['budget']['observed_tokens'], 150)
        self.assertIsNone(result['cache_tokens']); self.assertIsNone(result['reported_usd'])
        self.assertEqual(self.actual_classification(report)['verdict'], 'accepted_for_further_testing')
        before = file_index(self.output)
        with patch('verinote.ollama.request', side_effect=AssertionError('offline HTTP')), patch('subprocess.run', side_effect=AssertionError('offline process')):
            self.assertEqual(trial.evaluate(self.output)['verdict'], 'accepted_for_further_testing')
        self.assertEqual(file_index(self.output), before)
        original = next(self.output.rglob('original-prompt.txt'))
        self.assertNotEqual(original.read_text(), (original.parent/'prompt.txt').read_text())
        self.assertNotIn('think', self.calls[0])
        self.assertEqual(self.calls[0]['options'], {'num_ctx': 32768, 'num_predict': 4096, 'temperature': 0})

    def test_quote_repair_is_second_and_final_call_with_unchanged_prompt(self):
        self.mode = 'repair'; report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([a['repair'] for a in report['attempts']], [False, True])
        self.assertEqual(report['budget']['pending'], 0)
        self.assertEqual(result['observed_tokens'], 300)
        repair = next(self.output.rglob('changes.json')).parent
        self.assertFalse((repair/'original-prompt.txt').exists())
        self.assertEqual(self.actual_classification(report)['verdict'], 'accepted_for_further_testing')
        with patch('verinote.ollama.request', side_effect=AssertionError('offline HTTP')):
            self.assertEqual(trial.evaluate(self.output)['verdict'], 'accepted_for_further_testing')

    def test_complete_production_and_gold_failures_rejected(self):
        for mode in ('contract', 'gold', 'malformed', 'bad_repair'):
            with self.subTest(mode=mode):
                self.mode = mode; self.output = self.root/mode; self.calls = []
                report, result = self.run_fake()
                self.assertEqual(result['verdict'], 'inconclusive')
                classified = self.actual_classification(report)
                self.assertEqual(classified['verdict'], 'rejected', classified)
                self.assertEqual(len(self.calls), 2 if mode == 'bad_repair' else 1)
                self.assertEqual(report['budget']['pending'], 0)
                if mode != 'gold':
                    row = report['documents'][0]
                    with closing(sqlite3.connect(self.output/row['artifact']/'state.sqlite3')) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE kind!='ontology'").fetchone()[0], 0)

    def test_timeout_unknown_digest_and_budget_incomplete_are_not_rejected(self):
        for mode in ('timeout', 'unknown', 'digest', 'identity_unknown', 'truncated', 'repair_tokens', 'repair_time'):
            with self.subTest(mode=mode):
                self.mode = mode; self.output = self.root/mode; self.calls = []; self.ticks = 0
                report, _ = self.run_fake()
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(report['budget']['pending'], 0)
                self.assertEqual(self.actual_classification(report)['verdict'], 'inconclusive')
                self.assertEqual(len(list(self.output.rglob('usage.json'))), 1)
                if mode == 'identity_unknown': self.assertEqual(report['budget']['observed_tokens'], 150)

    def test_reindexed_usage_original_effective_prompt_and_checkpoint_tampering(self):
        report, _ = self.run_fake(); row = report['documents'][0]
        folder = self.output/row['artifact']/'runs'/row['run_id']/'1'
        for relative in ('usage.json', 'original-prompt.txt', 'prompt.txt', '../extraction-checkpoint.json'):
            path = folder/relative; original = path.read_bytes()
            with self.subTest(path=relative):
                if relative == 'usage.json':
                    value = json.loads(original); value['tokens']['input'] += 1; atomic_json(path, value)
                elif relative.endswith('checkpoint.json'):
                    value = json.loads(original); value['outputs'][0]['title'] = 'forged'; atomic_json(path, value)
                else: path.write_text(original.decode()+' altered')
                row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
                self.assertEqual(trial.evaluate(self.output)['reasons'], ['artifact_verification_failed'])
                path.write_bytes(original)
        row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
        self.assertEqual(trial.evaluate(self.output)['reasons'], ['synthetic_only'])

    def test_repair_audit_tampering_is_unverified_not_model_rejection(self):
        self.mode = 'repair'; report, _ = self.run_fake(); row = report['documents'][0]
        path = next(self.output.rglob('changes.json')); atomic_json(path, [])
        row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
        result = self.actual_classification(report)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['reasons'], ['artifact_verification_failed'])

    def test_mechanical_indices_audit_is_mandatory_and_bound_to_raw_output(self):
        self.mode = 'mechanical'; report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(result['reasons'], ['synthetic_only'])
        row = report['documents'][0]; audit = next(self.output.rglob('coverage-repair.json'))
        self.assertTrue(json.loads(audit.read_text())['changes'])
        self.assertEqual(self.actual_classification(report)['verdict'], 'accepted_for_further_testing')
        audit.unlink(); row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
        self.assertEqual(trial.evaluate(self.output)['reasons'], ['artifact_verification_failed'])


    def test_metadata_missing_index_and_symlink_fail_closed(self):
        report, _ = self.run_fake()
        for relative in ('index.json', 'environment.json', 'prompts.json'):
            path = self.output/relative; original = path.read_bytes(); path.unlink()
            self.assertEqual(trial.evaluate(self.output)['reasons'], ['artifact_verification_failed']); path.write_bytes(original)
        database = self.output/report['documents'][0]['artifact']/'state.sqlite3'; original = database.read_bytes()
        database.unlink(); database.symlink_to(self.root/'missing')
        self.assertEqual(trial.evaluate(self.output)['reasons'], ['artifact_verification_failed'])
        database.unlink(); database.write_bytes(original)

    def test_publication_violation_is_rejected_and_record_failure_drains(self):
        report, _ = self.run_fake(); row = report['documents'][0]
        with closing(sqlite3.connect(self.output/row['artifact']/'state.sqlite3')) as db:
            db.execute("UPDATE items SET status='approved' WHERE kind='fact'"); db.commit()
        row['safety'].update(passed=False, violations=['human_approval_contract'])
        row['artifact_files'] = file_index(self.output/row['artifact']); self.reindex(report)
        self.assertEqual(self.actual_classification(report)['verdict'], 'rejected')
        self.output = self.root/'record-failure'; self.calls = []
        original = trial.ReportWriter.write; failed = False
        def write(writer, report):
            nonlocal failed
            if not failed and any(a['status'] == 'success' for a in report['attempts']):
                failed = True
                raise OSError('synthetic write failure')
            return original(writer, report)
        with patch.object(trial.ReportWriter, 'write', new=write): report, result = self.run_fake()
        self.assertEqual(report['stop_reason'], 'report_write_failed')
        self.assertEqual(report['budget']['pending'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list(self.output.rglob('usage.json'))), 1)
        self.assertEqual(result['verdict'], 'inconclusive')


    def test_final_reassessment_time_is_counted_once_without_erasing_failure_reasons(self):
        original = trial.evaluate
        for synthetic in (False, True):
            for mode in (None, 'contract'):
                with self.subTest(synthetic=synthetic, mode=mode):
                    self.mode = mode; self.output = self.root/f'final-{synthetic}-{mode}'
                    self.calls = []; self.ticks = 0
                    def assessed(root):
                        result = original(root)
                        self.ticks = 601  # Hashing/DB reassessment crosses the wall deadline.
                        return result
                    def command(argv, *args):
                        if argv[1] == 'status': return ''
                        return 'a'*40
                    with patch.object(trial, 'evaluate', side_effect=assessed) as evaluate, patch.object(trial, '_command', side_effect=command):
                        report, result = trial.run(self.output, synthetic=synthetic, clock=lambda: self.ticks)
                    self.assertEqual(evaluate.call_count, 1)
                    self.assertEqual(report['budget']['elapsed_seconds'], 601)
                    self.assertEqual(report['budget']['pending'], 0)
                    self.assertEqual(report['status'], 'stopped')
                    self.assertEqual(report['stop_reason'], 'time_limit')
                    self.assertEqual(len(self.calls), 1)
                    self.assertEqual(result, original(self.output))
                    self.assertEqual(json.loads((self.output/'evaluation.json').read_text()), result)
                    if mode:
                        self.assertIn('production_contract_failed', result['reasons'])
                        self.assertIn('incomplete_trial', result['reasons'])
                        self.assertEqual(result['verdict'], 'inconclusive')
                    else:
                        self.assertIn('incomplete_trial', result['reasons'])
                        self.assertEqual(result['verdict'], 'inconclusive')


    def test_limits_cleanup_and_record_failures_override_verified_model_rejection(self):
        self.mode = 'gold_tokens'
        report, _ = self.run_fake()
        self.assertEqual(report['stop_reason'], 'token_limit')
        result = self.actual_classification(report)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('finite_gold_failed', result['reasons'])
        self.assertIn('incomplete_trial', result['reasons'])
        original_close = trial.CandidateService.close
        def failed_close(service):
            original_close(service)
            raise OSError('synthetic cleanup failure after draining')
        for mode in ('gold', 'malformed'):
            with self.subTest(cleanup_after=mode):
                self.mode = mode; self.output = self.root/f'cleanup-{mode}'; self.calls = []
                with patch.object(trial.CandidateService, 'close', new=failed_close): report, _ = self.run_fake()
                self.assertEqual(report['stop_reason'], 'cleanup_error')
                self.assertEqual(report['budget']['pending'], 0)
                result = self.actual_classification(report)
                self.assertEqual(result['verdict'], 'inconclusive')
                self.assertIn('finite_gold_failed' if mode == 'gold' else 'production_contract_failed', result['reasons'])
        self.mode = 'malformed'; self.output = self.root/'record-after-call-failure'; self.calls = []
        original_write = trial.ReportWriter.write; failed = False
        def failed_write(writer, report):
            nonlocal failed
            if not failed and any(a['status'] == 'error' for a in report['attempts']):
                failed = True
                raise OSError('synthetic recording failure after model rejection')
            return original_write(writer, report)
        with patch.object(trial.ReportWriter, 'write', new=failed_write): report, _ = self.run_fake()
        self.assertEqual(report['stop_reason'], 'report_write_failed')
        result = self.actual_classification(report)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('production_contract_failed', result['reasons'])


    def test_ledger_blocks_third_request_and_time_before_admission(self):
        ledger = trial.Ledger(lambda: self.ticks); self.ticks = 600
        with self.assertRaises(trial.Stopped): ledger.check()
        self.assertEqual(ledger.reason, 'time_limit')
        ledger = trial.Ledger(); ledger.attempts = [{'status': 'success', 'input_tokens': 1, 'output_tokens': 1} for _ in range(2)]
        with self.assertRaises(trial.Stopped): ledger.check()
        self.assertEqual(ledger.reason, 'request_limit')

    def test_cli_requires_both_explicit_allow_flags_before_calls(self):
        spec = importlib.util.spec_from_file_location('fidelity_cli', trial.REPO/trial.SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        args = ['run', '--output', str(self.output), '--model', trial.MODEL, '--max-requests', '2', '--max-seconds', '600', '--max-tokens', '100000']
        for flags in ([], ['--allow-model-calls'], ['--allow-fidelity-candidate']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                module.main(args+flags)
            self.assertEqual(result.exception.code, 2)
        self.assertEqual(self.calls, [])


if __name__ == '__main__': unittest.main()
