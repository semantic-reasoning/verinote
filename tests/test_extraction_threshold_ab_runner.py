"""V2 fixed schedule through local fake streaming workers and production Service."""
import copy
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_claude_worker import FAKE
from test_extraction_ab_runner import Clock

from verinote.documents import chunks
from verinote.extraction_ab_runner import file_index
from verinote.extraction_benchmark import TOKEN_KEYS, Limits, Stopped, execute_document
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.extraction_threshold_ab import experiment_plan
from verinote.extraction_threshold_ab_runner import (
    EvidenceBudget,
    evaluate_saved,
    run_experiment,
    verify_budget,
)
from verinote.extraction_transport import ExtractionTransport
from verinote.ontology import dictionary_context
from verinote.store import Store
from verinote.usage import atomic_json

MODEL = 'claude-sonnet-5-5'


class ThresholdRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.output = self.root/'result'
        oracle = {}
        for name in ('short', 'three', 'four', 'eight'):
            case = load_case(name, DATA_ROOT/'v2')
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
        self.owners = []; self.created_after_policy = []; self.clock = Clock()

    def factory(self, spec):
        registered = []
        for path in self.output.glob('documents/*/state.sqlite3'):
            with closing(sqlite3.connect(path)) as db:
                registered.extend(json.loads(r[0]) for r in db.execute("SELECT extraction_options FROM runs WHERE stage='extract' AND status='queued'"))
        if registered:
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]['schema_version'], 2)
            self.assertEqual(registered[0]['transport'], spec.to_dict())
            self.created_after_policy.append(registered[0]['original_chunk_count'])
        self.clock.advance(10)
        owner = ExtractionTransport(spec); self.owners.append(owner); self.addCleanup(owner.close)
        close = owner.close
        def finish():
            if not owner.closed: self.clock.advance(20)
            close()
        owner.close = finish
        return owner

    def run_fake(self, requests=856, **kwargs):
        return run_experiment(self.output, model='sonnet', limits=Limits(requests, 1, 10000, 1000000),
                              owner_factory=self.factory, clock=self.clock, **kwargs)

    def reindex(self, report):
        atomic_json(self.output/'report.json', report)
        atomic_json(self.output/'index.json', file_index(self.output, excluded=('index.json', 'evaluation.json')))

    def test_partial_budget_preserves_accounting_and_readonly_reassessment(self):
        report, result = self.run_fake(4)
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(report['budget']['requests_started'], 4)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertNotIn('artifact_verification_failed', result['reasons'], result)
        before = file_index(self.output)
        with patch('subprocess.run', side_effect=AssertionError('offline spawned')):
            self.assertEqual(evaluate_saved(self.output), result)
        self.assertEqual(file_index(self.output), before)
        first = report['documents'][0]
        with closing(sqlite3.connect(self.output/first['artifact']/'state.sqlite3')) as db:
            reason = db.execute("SELECT reason FROM events WHERE action='approved'").fetchone()[0]
        self.assertIn('Synthetic dictionary', reason); self.assertNotIn('human-reviewed', reason)
        self.assertEqual(report['environment']['dataset_manifest']['human_review']['status'], 'pending')
        for key in ('requests_started', 'dispatch_authorized', 'observed_tokens_lower_bound', 'pending'):
            forged = copy.deepcopy(report); forged['budget'][key] += 1
            self.reindex(forged)
            self.assertIn('budget_verification_failed', evaluate_saved(self.output)['reasons'])
        self.reindex(report)
        (self.output/'unaccounted').mkdir()
        atomic_json(self.output/'unaccounted'/'usage.json', {})
        self.reindex(report)
        self.assertIn('artifact_verification_failed', evaluate_saved(self.output)['reasons'])
        (self.output/'unaccounted'/'usage.json').unlink()
        forged = copy.deepcopy(report); forged['documents'][0]['quality']['passed'] = False
        self.reindex(forged)
        self.assertIn('artifact_verification_failed', evaluate_saved(self.output)['reasons'])

    def test_full_schedule_policy_warm_slots_cold_boundary_and_global_hashing(self):
        charged = set()
        def hashing(root, **kwargs):
            result = file_index(root, **kwargs)
            if Path(root).parent.name == 'documents' and str(root) not in charged:
                charged.add(str(root)); self.clock.advance(3)
            elif Path(root) == self.output: self.clock.advance(7)
            return result
        def preparing_and_evaluating(*args, **kwargs):
            self.clock.advance(5)
            value = execute_document(*args, **kwargs)
            self.clock.advance(7)
            return value
        with patch('verinote.extraction_threshold_ab_runner.file_index', side_effect=hashing), patch('verinote.extraction_threshold_ab_runner.execute_document', side_effect=preparing_and_evaluating):
            report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reason'])
        self.assertEqual(len(report['documents']), 128)
        self.assertEqual(len(report['attempts']), 608)
        self.assertEqual(len(self.owners), 66)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(self.created_after_policy, [3]*10+[4]*10+[8]*10)
        self.assertTrue(all(r['quality']['passed'] and r['safety']['passed'] for r in report['documents']))
        self.assertTrue(all(r['cold_seconds'] == 42 for r in report['documents'] if r['phase'] == 'cold'))
        self.assertEqual(report['budget']['elapsed_seconds'], 66*30+128*(5+7+3)+7)
        self.assertEqual(report['budget']['pending'], 0)
        for row in report['documents']:
            options = row['extraction_options']
            self.assertEqual(options['schema_version'], 2 if row['purpose'] == 'measurement' and row['arm'] == 'B' else 1)
            values = report['attempts'][row['attempt_start']-1:row['attempt_start']+row['attempt_count']-1]
            if row['purpose'] == 'warmup': self.assertEqual(values[0]['worker_identity']['slot_id'], row['slot'])
            elif row['phase'] == 'warm': self.assertTrue(all(v['process_observation'] == 'reused' for v in values))
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('synthetic_only', result['reasons'])
        before = file_index(self.output)
        self.assertEqual(evaluate_saved(self.output), result)
        self.assertEqual(file_index(self.output), before)
        forged = copy.deepcopy(report)
        forged['attempts'][0]['process_observation'] = 'reused'
        self.reindex(forged)
        self.assertIn('artifact_verification_failed', evaluate_saved(self.output)['reasons'])

    def test_unknown_model_stops_before_second_initial_call(self):
        self.script.write_text(self.script.read_text().replace(repr(MODEL), "'sonnet'"))
        report, result = self.run_fake()
        self.assertEqual(report['stop_reason'], 'comparison_evidence_unknown')
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertNotIn('artifact_verification_failed', evaluate_saved(self.output)['reasons'])

    def retained_violation(self, *, real_classification=False):
        report, _ = self.run_fake(7)
        row = report['documents'][0]
        artifact = self.output/row['artifact']
        with closing(sqlite3.connect(artifact/'state.sqlite3')) as db:
            db.execute("UPDATE items SET status='approved' WHERE kind='fact'"); db.commit()
        row['safety'] = {'complete': True, 'passed': False, 'violations': ['human_approval_contract']}
        row['artifact_files'] = file_index(artifact)
        if real_classification:
            # Fabricated classification control only: no provider is contacted.
            # Exercise the real-evidence decision branch with local fake artifacts.
            report['synthetic_responses'] = False
            report['environment'].update(synthetic=False, revision='a'*40, tree='b'*40,
                cli_version='2.1.311', cli_version_source='cli_version_command')
            report['limits']['requests'] = 856
            atomic_json(self.output/'environment.json', report['environment'])
            atomic_json(self.output/'limits.json', report['limits'])
        self.reindex(report)
        return report, 'safety:'+row['key']

    def test_verified_prefix_failure_survives_later_damage_in_both_classifications(self):
        for real in (False, True):
            self.output = self.root/str(real)
            report, reason = self.retained_violation(real_classification=real)
            expected = 'rejected' if real else 'inconclusive'
            baseline = evaluate_saved(self.output)
            self.assertIn(reason, baseline['reasons']); self.assertEqual(baseline['verdict'], expected)
            later = self.output/report['documents'][1]['artifact']/'state.sqlite3'
            original = later.read_bytes()
            for mutation in ('contents', 'missing', 'symlink'):
                with self.subTest(real=real, mutation=mutation):
                    if mutation == 'contents': later.write_bytes(b'corrupt later database')
                    else:
                        later.unlink()
                        if mutation == 'symlink': later.symlink_to(self.output/'report.json')
                    result = evaluate_saved(self.output)
                    self.assertEqual(result['verdict'], expected)
                    self.assertIn(reason, result['reasons'])
                    self.assertIn('artifact_verification_failed', result['reasons'])
                    self.assertTrue(result['unverified_other_records'])
                    if later.is_symlink(): later.unlink()
                    later.write_bytes(original)

    def test_damaged_first_document_never_supplies_a_confirmed_violation(self):
        report, reason = self.retained_violation(real_classification=True)
        first = self.output/report['documents'][0]['artifact']/'state.sqlite3'
        first.write_bytes(b'corrupt first database')
        result = evaluate_saved(self.output)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertNotIn(reason, result['reasons'])

    def test_document_directory_symlinks_cannot_supply_trusted_evidence(self):
        report, reason = self.retained_violation(real_classification=True)
        for relative, trusted_prefix in [('documents', False), (report['documents'][0]['artifact'], False),
                                         (report['documents'][1]['artifact'], True)]:
            with self.subTest(path=relative):
                source = self.output/relative; moved = self.root/'moved-evidence'
                source.rename(moved); source.symlink_to(moved, target_is_directory=True)
                try:
                    result = evaluate_saved(self.output)
                    self.assertEqual(result['verdict'], 'rejected' if trusted_prefix else 'inconclusive')
                    self.assertEqual(reason in result['reasons'], trusted_prefix)
                    self.assertIn('artifact_verification_failed', result['reasons'])
                finally:
                    source.unlink(); moved.rename(source)

    def test_metadata_and_index_damage_cannot_supply_a_trusted_prefix(self):
        report, reason = self.retained_violation(real_classification=True)
        paths = ['report.json', 'environment.json', 'plan.json', 'limits.json', 'prompts.json',
                 'fixtures/three.json', 'implementation/verinote/extraction_threshold_ab_runner.py', 'index.json']
        for relative in paths:
            path = self.output/relative; original = path.read_bytes()
            with self.subTest(path=relative):
                path.write_bytes(b'{}')
                result = evaluate_saved(self.output)
                self.assertEqual(result['verdict'], 'inconclusive')
                self.assertNotIn(reason, result['reasons'])
                path.write_bytes(original)
        index = self.output/'index.json'; original = index.read_bytes(); index.unlink()
        self.assertEqual(evaluate_saved(self.output)['verdict'], 'inconclusive')
        index.write_bytes(original)
        # Updating the top-level hash cannot bypass the semantic manifest checks.
        atomic_json(self.output/'plan.json', {})
        self.reindex(report)
        self.assertEqual(evaluate_saved(self.output)['verdict'], 'inconclusive')

    def test_retained_global_document_index_is_required_even_with_valid_row_index(self):
        report, reason = self.retained_violation(real_classification=True)
        index = json.loads((self.output/'index.json').read_text())
        del index[report['documents'][0]['artifact']+'/state.sqlite3']
        atomic_json(self.output/'index.json', index)
        result = evaluate_saved(self.output)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertNotIn(reason, result['reasons'])

    def test_quote_repair_raw_evidence_is_reassessed(self):
        script = self.script.read_text()
        script = script.replace("dictionary=payload['approved_dictionary']['payload']",
            "dictionary={name:[{k:v for k,v in row.items() if k!='quote'} for row in rows] for name,rows in payload['approved_dictionary']['payload'].items()}")
        script = script.replace("    history.append(text);value=result(json.dumps(body))", """    if 'issues' in payload:
        repaired=[]
        for issue in payload['issues']:
            kind,number,field=issue['path'].split('.')
            repaired.append({'path':issue['path'],'quote':body[kind][int(number)][field]})
        body={'repairs':repaired}
    elif payload['offset']==0:
        body['facts'][0]['quote']='deliberately absent quotation'
    history.append(text);value=result(json.dumps(body))""")
        self.script.write_text(script)
        report, result = self.run_fake(4)
        self.assertEqual([a['repair'] for a in report['attempts']], [False, True, False, False])
        row = report['documents'][0]
        self.assertTrue(row['quality']['passed'] and row['safety']['passed'])
        self.assertEqual(evaluate_saved(self.output), result)
        repair = self.output/row['artifact']/'runs'/row['run_id']/'1'/'quote-repair'
        (repair/'usage.json').unlink()
        self.assertIn('artifact_verification_failed', evaluate_saved(self.output)['reasons'])

    def test_failed_later_chunk_keeps_no_partial_publication(self):
        self.script.write_text(self.script.read_text().replace(
            '    history.append(text);value=result(json.dumps(body))',
            "    if payload['offset'] != 0: body={}\n    history.append(text);value=result(json.dumps(body))"))
        report, result = self.run_fake()
        self.assertEqual(len(report['attempts']), 2)
        row = report['documents'][0]; self.assertEqual(row['status'], 'error')
        with closing(sqlite3.connect(self.output/row['artifact']/'state.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE kind!='ontology'").fetchone()[0], 0)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(evaluate_saved(self.output), result)

    def test_document_index_write_failure_keeps_finished_usage_and_cleanup(self):
        def fail_document_index(root, **kwargs):
            if Path(root).parent.name == 'documents': raise OSError('recording failure')
            return file_index(root, **kwargs)
        with patch('verinote.extraction_threshold_ab_runner.file_index', side_effect=fail_document_index):
            report, result = self.run_fake()
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(len(report['documents']), 1)
        self.assertEqual(len(report['attempts']), 3)
        self.assertEqual(report['budget']['pending'], 0)
        self.assertEqual(len(list(self.output.rglob('usage.json'))), 3)
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_unknown_cost_and_wrong_model_family_stop_admission(self):
        script = self.script.read_text()
        for name, replacement, reason in (
                ('cost', script.replace('total_cost_usd=.25', 'total_cost_usd=None'), 'comparison_evidence_unknown'),
                ('family', script.replace(repr(MODEL), "'claude-haiku-4-5'"), 'model_changed')):
            with self.subTest(name=name):
                self.output = self.root/name; self.script.write_text(replacement)
                report, result = self.run_fake()
                self.assertEqual(report['stop_reason'], reason)
                self.assertEqual(len(report['attempts']), 1)
                self.assertEqual(report['budget']['pending'], 0)
                self.assertTrue(all(o.closed for o in self.owners))
                self.assertEqual(evaluate_saved(self.output), result)

    def test_repair_model_change_blocks_reserved_call_and_drains_authorized_call(self):
        budget = EvidenceBudget(Limits(10, 1, 100, 10000), experiment_plan(), clock=self.clock)
        budget.ready = True
        document = experiment_plan()['documents'][0]
        identity = {'owner_id': document['owner_id'], 'document_key': document['key']}
        def usage(model):
            return {'tokens': dict.fromkeys(TOKEN_KEYS, 1), 'usage_complete': True,
                    'resolved_model': model, 'model_identity_source': 'claude_modelUsage', 'reported_usd': .25,
                    'cli_call_id': 'c'*32,
                    'worker_scope': 'extraction_pool', 'worker_identity': {'pool_id': 'a'*32, 'slot_id': 1,
                    'worker_id': 'b'*32, 'generation': 1}}
        first = budget.reserve(1, False, identity=identity); budget.authorize_dispatch(first)
        budget.finish(first, usage(MODEL), observation='started')
        repair = budget.reserve(1, True, identity=identity); budget.authorize_dispatch(repair)
        inflight = budget.reserve(2, False, identity=identity); budget.authorize_dispatch(inflight)
        reserved = budget.reserve(3, False, identity=identity)
        budget.finish(repair, usage('claude-haiku-4-5'), observation='reused')
        with self.assertRaises(Stopped): budget.authorize_dispatch(reserved)
        budget.block(reserved)
        budget.finish(inflight, usage(MODEL), observation='reused')
        with self.assertRaises(Stopped): budget.reserve(3, False, identity=identity)
        self.assertEqual(budget.reason, 'model_changed')
        self.assertEqual(budget.summary()['pending'], 0)
        self.assertEqual(budget.summary()['blocked_before_dispatch'], 1)
        verify_budget({'limits': vars(budget.limits), 'attempts': budget.attempts, 'budget': budget.summary(),
                       'stop_reason': budget.reason, 'status': 'stopped'})

    def test_cold_cleanup_failure_is_terminal_and_final_cleanup_retries_once(self):
        original = self.factory
        def factory(spec):
            owner = original(spec); close = owner.close; attempts = []
            def fail_once():
                attempts.append(True)
                if len(attempts) == 1: self.clock.advance(20); raise OSError('synthetic cleanup failure')
                close()
            owner.close = fail_once
            return owner
        self.factory = factory
        report, result = self.run_fake()
        self.assertEqual(len(report['documents']), 1)
        self.assertEqual(len(report['attempts']), 3)
        self.assertEqual(report['stop_reason'], 'cleanup_error')
        self.assertEqual(report['cleanup_errors'], 1)
        self.assertFalse(report['documents'][0]['cleanup_succeeded'])
        self.assertIsNone(report['documents'][0]['cold_seconds'])
        self.assertTrue(all(o.closed for o in self.owners))
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_manifest_must_be_complete_before_any_owner_or_call(self):
        original = atomic_json
        def omit(path, value):
            if Path(path).name != 'plan.json': original(path, value)
        with patch('verinote.extraction_threshold_ab_runner.atomic_json', side_effect=omit), self.assertRaises(ValueError):
            self.run_fake()
        self.assertEqual(self.owners, [])
        self.assertFalse(list(self.output.rglob('usage.json')))

    def test_verified_unsafe_publication_is_not_a_successful_safety_check(self):
        original = Store.finish_run
        def unsafe(store, run_id, *args, **kwargs):
            original(store, run_id, *args, **kwargs)
            run = store.run(run_id)
            if run['stage'] == 'extract':
                with store.connect(True) as db:
                    db.execute(_sql)
        for violation, _sql in (
                ('human_approval_contract', "UPDATE items SET status='approved' WHERE kind='fact'"),
                ('publication_contract', 'UPDATE documents SET generation=generation+1'),
                ('dictionary_publication_contract', "UPDATE items SET status='pending' WHERE kind='ontology'"),
                ('candidate_publication', "DELETE FROM items WHERE kind='fact'")):
            with self.subTest(violation=violation):
                self.output = self.root/violation
                with patch.object(Store, 'finish_run', new=unsafe): report, result = self.run_fake()
                self.assertEqual(len(report['documents']), 1)
                self.assertEqual(report['documents'][0]['safety'], {'complete': True, 'passed': False, 'violations': [violation]})
                self.assertTrue(any(r.startswith('safety:') for r in result['reasons']))
                self.assertEqual(evaluate_saved(self.output), result)

    def test_cli_plan_evaluate_are_offline_and_run_requires_explicit_flag(self):
        path = Path(__file__).resolve().parents[1]/'scripts/benchmark_extraction_threshold_ab.py'
        spec = importlib.util.spec_from_file_location('threshold_script', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch('subprocess.run', side_effect=AssertionError('offline spawned')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(['plan']), 0)
            self.assertEqual(json.loads(output.getvalue())['initial_requests'], 608)
            self.assertEqual(module.main(['evaluate', str(self.root/'missing')]), 1)
        with redirect_stderr(io.StringIO()), patch.object(module, 'run_experiment', side_effect=AssertionError('unapproved call')), self.assertRaises(SystemExit):
            module.main(['run', '--output', str(self.output), '--model', 'sonnet', '--max-requests', '856', '--max-seconds', '100', '--max-tokens', '100000'])
