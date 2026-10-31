"""Full frozen schedule via local streaming subprocesses, with durable raw records."""
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_claude_worker import FAKE
from test_extraction_ab import MODEL

from verinote.agents import CLI
from verinote.documents import chunks
from verinote.extraction_ab_runner import evaluate_saved, file_index, run_experiment
from verinote.extraction_benchmark import Limits, execute_document
from verinote.extraction_evaluation import ideal_outputs, load_case
from verinote.extraction_transport import ExtractionTransport
from verinote.ontology import dictionary_context
from verinote.store import Store, canonical
from verinote.usage import atomic_json


class Clock:
    value = 0.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class DurableExperiment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        oracle = {}
        for name in ('short', 'long', 'dense', 'none', 'boundary'):
            case = load_case(name)
            for (offset, text), output in zip(chunks(case['source']), ideal_outputs(case, contract_version=2), strict=True):
                key = json.dumps({'text': text, 'offset': offset, 'dictionary': dictionary_context(case['dictionary'])}, sort_keys=True)
                oracle[key] = output['output']
        (self.root/'oracle.json').write_text(json.dumps(oracle))
        self.script = self.root/'fake.py'
        self.script.write_text(FAKE.replace("modelUsage={'fake-model':{}}", "modelUsage={"+repr(MODEL)+":{}}")
            .replace("    body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)",
                     "    payload=json.loads(text.split('입력(JSON):\\n')[1])\n    key=json.dumps(dict(text=payload['text'],offset=payload['offset'],dictionary=payload['approved_dictionary']['payload']),sort_keys=True)\n    body=json.loads((root/'oracle.json').read_text())[key]"))
        self.addCleanup(patch.stopall)
        patch('verinote.agents.executable', return_value=sys.executable).start()
        patch('verinote.extraction_transport.executable', return_value=sys.executable).start()
        patch('verinote.claude_worker.command', side_effect=lambda b, m: [b, str(self.script), str(self.root), m]).start()
        self.owners = []; self.clock = Clock()

    def factory(self, capacity):
        self.clock.advance(10)
        owner = ExtractionTransport.from_cli(CLI(timeout=90, persistent=False), 'model', capacity=capacity)
        self.owners.append(owner); self.addCleanup(owner.close)
        close = owner.close
        def finish():
            if not owner.closed: self.clock.advance(20)
            close()
        owner.close = finish
        return owner

    def test_full_schedule_warms_each_slot_and_retains_verifiable_raw_artifacts(self):
        output = self.root/'result'
        charged = set()
        def hashing(root, **kwargs):
            result = file_index(root, **kwargs)
            if Path(root).name.startswith(('primary-', 'secondary-', 'cold-')) and root not in charged:
                charged.add(root); self.clock.advance(3)
            return result
        def preparing_and_evaluating(*args, **kwargs):
            self.clock.advance(5)  # Store preparation before the production helper.
            result = execute_document(*args, **kwargs)
            self.clock.advance(7)  # Evaluation completes before owner termination.
            return result
        with patch('verinote.extraction_ab_runner.file_index', side_effect=hashing), patch('verinote.extraction_ab_runner.execute_document', side_effect=preparing_and_evaluating):
            report, evaluation = run_experiment(output, model='model', limits=Limits(274, 1, 2000, 1000000),
                                                owner_factory=self.factory, clock=self.clock)
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(report['documents']), 85)
        self.assertEqual(len(report['attempts']), 137)
        self.assertEqual(len(self.owners), 22); self.assertTrue(all(o.closed for o in self.owners))
        self.assertTrue(all(r['quality']['passed'] for r in report['documents']))
        self.assertTrue(all(r['cold_seconds']==42 for r in report['documents'] if r['phase']=='cold'))
        self.assertEqual(report['budget']['elapsed_seconds'], 22*30+85*(5+7+3))
        self.assertEqual(report['budget']['pending'], 0)
        self.assertTrue(all(a['resolved_model']==MODEL and a['model_identity_source']=='claude_modelUsage' for a in report['attempts']))
        for row in report['documents']:
            values = report['attempts'][row['attempt_start']-1:row['attempt_start']-1+row['attempt_count']]
            if row['purpose']=='warmup': self.assertEqual(values[0]['worker_identity']['slot_id'], row['slot'])
            elif row['phase']!='cold': self.assertTrue(all(v['process_observation']=='reused' for v in values))
            self.assertTrue((output/row['artifact']/'state.sqlite3').exists())
        self.assertNotIn('artifact_verification_failed', evaluation['reasons'])
        self.assertNotEqual(evaluation['verdict'], 'accepted')  # Synthetic evidence cannot enable production.
        with patch('subprocess.run', side_effect=AssertionError('offline evaluator spawned process')):
            self.assertEqual(evaluate_saved(output), evaluation)
        forged = copy.deepcopy(report)
        forged['attempts'][0]['process_observation'] = 'reused'
        atomic_json(output/'report.json', forged)
        atomic_json(output/'index.json', file_index(output, excluded=('index.json', 'evaluation.json')))
        self.assertIn('artifact_verification_failed', evaluate_saved(output)['reasons'])
        atomic_json(output/'report.json', report)
        atomic_json(output/'index.json', file_index(output, excluded=('index.json', 'evaluation.json')))
        first = report['documents'][0]
        usage = output/first['artifact']/'runs'/first['run_id']/'1'/'usage.json'
        usage.write_text('{}')
        self.assertIn('artifact_verification_failed', evaluate_saved(output)['reasons'])

    def test_cold_cleanup_failure_preserves_elapsed_but_not_a_valid_cold_sample(self):
        def factory(capacity):
            owner = self.factory(capacity)
            if len(self.owners)==11:  # First cold owner, after all five warm series.
                close = owner.close
                failed = False
                def fail_once():
                    nonlocal failed
                    if not failed:
                        failed = True; self.clock.advance(20); raise OSError('synthetic cleanup failure')
                    close()
                owner.close = fail_once
            return owner
        report, result = run_experiment(self.root/'cleanup', model='model', limits=Limits(274, 1, 2000, 1000000),
                                        owner_factory=factory, clock=self.clock)
        cold = [r for r in report['documents'] if r['phase']=='cold']
        self.assertEqual(len(cold), 1); self.assertIsNone(cold[0]['cold_seconds'])
        self.assertFalse(cold[0]['cold_cleanup_ok']); self.assertEqual(cold[0]['elapsed_seconds'], 30)
        self.assertEqual(report['stop_reason'], 'cleanup_error'); self.assertEqual(report['cleanup_errors'], 1)
        self.assertTrue(all(o.closed for o in self.owners)); self.assertEqual(result['verdict'], 'inconclusive')

    def test_budget_stop_keeps_finished_attempts_and_partial_documents_without_retries(self):
        output = self.root/'partial'
        report, evaluation = run_experiment(output, model='model', limits=Limits(4, 1, 2000, 1000000),
                                            owner_factory=self.factory, clock=self.clock)
        self.assertEqual(report['status'], 'stopped'); self.assertEqual(report['budget']['requests_started'], 4)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertTrue((output/'index.json').exists()); self.assertNotEqual(evaluation['verdict'], 'accepted')
        self.assertTrue(any(list((output/r['artifact']).rglob('usage.json')) for r in report['documents']))
        # Rehashing a tampered summary does not make it authoritative over raw records.
        report['documents'][0]['quality']['passed'] = False
        atomic_json(output/'report.json', report)
        atomic_json(output/'index.json', file_index(output, excluded=('index.json', 'evaluation.json')))
        self.assertIn('artifact_verification_failed', evaluate_saved(output)['reasons'])

    def test_verified_publication_violations_reject_even_with_missing_later_samples(self):
        original = Store.finish_run
        for mode, reason in [('partial', 'partial_publication'), ('approval', 'human_approval_contract'),
                             ('candidate', 'candidate_publication'), ('dictionary', 'dictionary_publication_contract'),
                             ('generation', 'publication_contract')]:
            with self.subTest(mode=mode):
                def unsafe_finish(store, run_id, *args, mode=mode, **kwargs):
                    original(store, run_id, *args, **kwargs)
                    run = store.run(run_id)
                    if run['stage'] != 'extract': return
                    if mode=='partial': raise ValueError('synthetic failure after unsafe commit')
                    with store.connect(True) as db:
                        if mode=='approval': db.execute("UPDATE items SET status='approved' WHERE run_id=? AND kind='fact'", (run_id,))
                        elif mode=='candidate':
                            item = db.execute("SELECT id,payload FROM items WHERE run_id=? AND kind='fact' LIMIT 1", (run_id,)).fetchone()
                            payload = json.loads(item['payload']); payload['object'] = 'Unexpected'
                            db.execute('UPDATE items SET payload=? WHERE id=?', (canonical(payload), item['id']))
                        elif mode=='dictionary': db.execute('UPDATE items SET revision=revision+1 WHERE id=?', (run['ontology']['id'],))
                        else: db.execute('UPDATE documents SET generation=generation+1 WHERE id=?', (run['document_id'],))
                with patch.object(Store, 'finish_run', new=unsafe_finish):
                    report, result = run_experiment(self.root/('unsafe-'+mode), model='model', limits=Limits(1, 1, 2000, 1000000),
                                                    owner_factory=self.factory, clock=self.clock)
                self.assertEqual(len(report['documents']), 1)
                self.assertEqual(result['verdict'], 'rejected', result)
                self.assertTrue(any(value.startswith(reason+':') for value in result['reasons']), result)
                self.assertTrue(all(o.closed for o in self.owners))
                forged = copy.deepcopy(report); forged['attempts'][0]['document_key'] = 'unrelated'
                output = self.root/('unsafe-'+mode)
                atomic_json(output/'report.json', forged)
                atomic_json(output/'index.json', file_index(output, excluded=('index.json', 'evaluation.json')))
                self.assertEqual(evaluate_saved(output)['verdict'], 'inconclusive')

    def test_unknown_actual_model_stops_after_one_warmup_without_topup(self):
        self.script.write_text(self.script.read_text().replace(repr(MODEL), "'sonnet'"))
        output = self.root/'unknown-model'
        report, evaluation = run_experiment(output, model='model', limits=Limits(274, 1, 2000, 1000000),
                                            owner_factory=self.factory, clock=self.clock)
        self.assertEqual(report['stop_reason'], 'comparison_evidence_unknown')
        self.assertEqual(len(report['attempts']), 1); self.assertEqual(len(report['documents']), 1)
        self.assertIsNone(report['attempts'][0]['resolved_model'])
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(evaluation['verdict'], 'inconclusive')

    def test_artifact_recording_failure_stops_admission_without_removing_raw_files(self):
        output = self.root/'recording-failure'
        failed = False
        def failing(root, **kwargs):
            nonlocal failed
            if Path(root).name.startswith('primary-') and not failed:
                failed = True; raise OSError('synthetic disk error')
            return file_index(root, **kwargs)
        with patch('verinote.extraction_ab_runner.file_index', side_effect=failing):
            report, result = run_experiment(output, model='model', limits=Limits(274, 1, 2000, 1000000),
                                            owner_factory=self.factory, clock=self.clock)
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(len(report['attempts']), 1)
        self.assertTrue(list(output.rglob('usage.json'))); self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_plan_and_evaluate_cli_never_spawn_and_run_requires_explicit_flag(self):
        script = Path(__file__).resolve().parents[1]/'scripts/benchmark_extraction_ab.py'
        spec = importlib.util.spec_from_file_location('ab_script', script)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch('subprocess.run', side_effect=AssertionError('offline command spawned')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(['plan']), 0)
            self.assertEqual(json.loads(output.getvalue())['initial_requests'], 137)
            self.assertEqual(module.main(['evaluate', str(self.root/'missing')]), 1)
        with redirect_stderr(io.StringIO()), patch.object(module, 'run_experiment', side_effect=AssertionError('unapproved call')), self.assertRaises(SystemExit) as error:
            module.main(['run', '--output', str(self.root/'no-call'), '--model', 'sonnet', '--max-requests', '274',
                         '--max-seconds', '100', '--max-tokens', '100000'])
        self.assertEqual(error.exception.code, 2)
