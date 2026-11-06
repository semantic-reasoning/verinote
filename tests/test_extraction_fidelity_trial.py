"""Bounded synthetic owner through production Service; no real model calls."""
import copy
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import test_claude_worker as workers
from test_extraction_ab_runner import Clock

from verinote.diagnostics import extraction_fidelity_artifacts as artifacts
from verinote.diagnostics import extraction_fidelity_trial as trial
from verinote.extraction_ab_runner import file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.extraction_transport import ExtractionTransport
from verinote.usage import atomic_json


class FidelityTrial(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.output = self.root/'result'; self.clock = Clock()
        self.case = load_case('three', DATA_ROOT/'v2')
        self.outputs = ideal_outputs(self.case, contract_version=2)
        self.script = self.root/'fake.py'
        self.script.write_text(workers.FAKE.replace("modelUsage={'fake-model':{}}", "modelUsage={'claude-sonnet-5-5':{}}")
            .replace("    body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)",
                "    payload=json.loads(text.split('입력(JSON):\\n')[1])\n"
                "    index=payload['offset']//8000\n"
                "    records=json.loads((root/'oracle.json').read_text())\n"
                "    body=records[index]['repair' if 'issues' in payload else 'output']"))
        self.owners = []
        for target in ('verinote.agents.executable', 'verinote.extraction_transport.executable'):
            self.enterContext(patch(target, return_value=sys.executable))
        self.enterContext(patch('verinote.claude_worker.command', side_effect=lambda binary, model: [binary, str(self.script), str(self.root)]))

    def factory(self, spec):
        owner = ExtractionTransport(spec); self.owners.append(owner); self.addCleanup(owner.close)
        return owner

    def run_fake(self, outputs=None, factory=None):
        records = outputs if outputs is not None else [{'output': r['output']} for r in self.outputs]
        (self.root/'oracle.json').write_text(json.dumps(records))
        return trial.run(self.output, owner_factory=factory or self.factory, clock=self.clock)

    def refresh(self, report):
        report['documents'][0]['artifact_files'] = file_index(self.output/report['documents'][0]['artifact'])
        atomic_json(self.output/'report.json', report)
        atomic_json(self.output/'index.json', file_index(self.output, excluded=('index.json', 'evaluation.json')))

    def test_full_trial_uses_three_sequential_candidate_calls_and_offline_replay(self):
        report, result = self.run_fake()
        self.assertEqual(report['status'], 'completed', report['stop_reasons'])
        self.assertNotIn('artifact_verification_failed', result['reasons'], result)
        self.assertEqual(result['verdict'], 'inconclusive'); self.assertIn('synthetic_only', result['reasons'])
        self.assertTrue(result['production']['passed']); self.assertTrue(result['quality']['passed'])
        self.assertEqual([a['chunk'] for a in report['attempts']], [1, 2, 3])
        self.assertTrue(all(owner.closed for owner in self.owners)); self.assertEqual(len(self.owners), 1)
        before = file_index(self.output); original_connect = sqlite3.connect
        def readonly(database, *args, **kwargs):
            self.assertTrue(kwargs.get('uri')); self.assertTrue(str(database).endswith('?mode=ro'))
            return original_connect(database, *args, **kwargs)
        with patch('subprocess.Popen', side_effect=AssertionError('offline subprocess')), \
             patch('subprocess.run', side_effect=AssertionError('offline subprocess')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('offline HTTP')), \
             patch('sqlite3.connect', side_effect=readonly):
            self.assertEqual(artifacts.evaluate(self.output), result)
        self.assertEqual(file_index(self.output), before)
        row = report['documents'][0]
        for index in range(1, 4):
            call = self.output/row['artifact']/'runs'/row['run_id']/str(index)
            base = (call/'original-prompt.txt').read_text(); actual = (call/'prompt.txt').read_text()
            self.assertNotEqual(actual, base); self.assertEqual(base.split('입력(JSON):\n')[1], actual.split('입력(JSON):\n')[1])
        with self.assertRaises(ValueError): self.run_fake()
        with self.assertRaisesRegex(ValueError, 'allow_model_calls'):
            trial.run(self.root/'unauthorized')

    def test_off_dictionary_fact_replays_as_production_pass_with_quote_repair_and_index_audit(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        for record in records:
            output = record['output']; first = output['facts'][0]
            row = next(r for r in output['coverage'] if r['predicate'] == first['predicate'])
            output['facts'].append(first | {'subject': 'entity absent from the dictionary'})
            row['fact_indices'].append(len(output['facts'])-1)
        quote = records[0]['output']['facts'][0]['quote']
        records[0]['output']['facts'][0]['quote'] = 'not present in the synthetic source'
        records[0]['repair'] = {'repairs': [{'path': 'facts.0.quote', 'quote': quote}]}
        next(r for r in records[1]['output']['coverage'] if r['fact_indices'])['fact_indices'] = []
        report, result = self.run_fake(records)
        self.assertEqual(report['status'], 'completed', report['stop_reasons'])
        self.assertEqual(len(report['attempts']), 4)
        self.assertNotIn('artifact_verification_failed', result['reasons'], result)
        self.assertTrue(result['production']['passed'], result)

    def test_production_failure_stops_without_replacement(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        records[0]['output']['coverage'] = []
        report, result = self.run_fake(records)
        self.assertEqual(len(report['attempts']), 1); self.assertEqual(len(self.owners), 1)
        self.assertTrue(result['production']['verified_failure'], result)
        self.assertIn('production_contract_failed', result['reasons'])
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_gold_failure_remains_distinct_from_production_success(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        for record in records:
            record['output']['dictionary_gaps'] = []
        report, result = self.run_fake(records)
        self.assertEqual(len(report['attempts']), 3)
        self.assertTrue(result['production']['passed']); self.assertFalse(result['quality']['passed'])
        self.assertIn('finite_gold_failed', result['reasons'])

    def test_two_quote_repairs_keep_original_repair_prompt_and_match_checkpoint(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        for record in records[:2]:
            quote = record['output']['facts'][0]['quote']
            record['output']['facts'][0]['quote'] = 'not present in the synthetic source'
            record['repair'] = {'repairs': [{'path': 'facts.0.quote', 'quote': quote}]}
        report, result = self.run_fake(records)
        self.assertEqual(len(report['attempts']), 5, report)
        self.assertTrue(result['production']['passed'], result)
        self.assertEqual([a['repair'] for a in report['attempts']], [False, True, False, True, False])
        row = report['documents'][0]
        for call in (self.output/row['artifact']).rglob('quote-repair'):
            self.assertEqual((call/'prompt.txt').read_bytes(), (call/'original-prompt.txt').read_bytes())
            self.assertTrue((call/'changes.json').is_file())
        self.assertEqual(artifacts.evaluate(self.output), result)

    def test_tampering_is_not_cured_by_reindexing(self):
        report, _ = self.run_fake(); row = report['documents'][0]
        call = self.output/row['artifact']/'runs'/row['run_id']/'1'
        paths = [call/'candidate.json', call/'prompt.txt', call/'original-prompt.txt', call/'stdout.txt',
                 call.parent/'extraction-checkpoint.json', self.output/'plan.json',
                 self.output/'implementation/verinote/diagnostics/extraction_fidelity_candidate.py']
        for path in paths:
            original = path.read_bytes()
            try:
                if path.suffix == '.json':
                    value = json.loads(original)
                    if path.name == 'candidate.json': value['template_sha256'] = '0'*64
                    elif path.name == 'extraction-checkpoint.json': value['outputs'][0]['claims'][0]['text'] += ' modified'
                    else: value['candidate_id'] = 'different'
                    path.write_text(json.dumps(value))
                else: path.write_bytes(original+b' changed')
                self.refresh(report)
                with self.subTest(path=path.name):
                    self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])
            finally:
                path.write_bytes(original); self.refresh(report)
        extra = call.parent/'4'; extra.mkdir(); (extra/'usage.json').write_text('{}')
        self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])

    def test_unknown_usage_and_changed_model_stop_before_second_call(self):
        for mode in ('usage', 'model'):
            with self.subTest(mode=mode):
                if mode == 'usage':
                    modified = self.script.read_text().replace('output_tokens=2', 'output_tokens=None')
                else: modified = self.script.read_text().replace("modelUsage={'claude-sonnet-5-5':{}}", "modelUsage={'claude-sonnet-unknown':{}}")
                original = self.script.read_text(); self.script.write_text(modified)
                self.output = self.root/('result-'+mode)
                report, result = self.run_fake()
                self.assertEqual(len(report['attempts']), 1); self.assertEqual(result['verdict'], 'inconclusive')
                self.assertTrue(report['stop_reasons'])
                self.script.write_text(original)

    def test_cleanup_failure_overrides_known_gold_failure(self):
        def factory(spec):
            owner = self.factory(spec); original = owner.close
            def close():
                original(); raise OSError('synthetic cleanup failure')
            owner.close = close
            # Cleanup hook uses the original method to avoid failing test teardown.
            self._cleanups.pop(); self.addCleanup(original)
            return owner
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        for record in records: record['output']['dictionary_gaps'] = []
        report, result = self.run_fake(records, factory)
        self.assertIn('cleanup_error', report['stop_reasons']); self.assertIn('document_failed', report['stop_reasons'])
        self.assertEqual(result['verdict'], 'inconclusive'); self.assertIn('incomplete_trial', result['reasons'])

    def test_final_write_failure_cannot_publish_a_complete_trial(self):
        original = trial.atomic_json
        def fail(path, value):
            if Path(path).name == 'finalization.json' and value == {'complete': True}: raise OSError('write failure')
            return original(path, value)
        with patch.object(trial, 'atomic_json', side_effect=fail): report, result = self.run_fake()
        self.assertEqual(result['verdict'], 'inconclusive'); self.assertIn('finalization_failed', report['stop_reasons'])
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])

    def test_timeout_stops_without_retry_and_never_accepts(self):
        self.script.write_text(self.script.read_text().replace("    index=payload['offset']//8000", "    time.sleep(1)\n    index=payload['offset']//8000"))
        def factory(spec):
            owner = self.factory(spec); view = owner.view
            owner.view = lambda: view().limited_timeout('claude', .05)
            return owner
        report, result = self.run_fake(factory=factory)
        self.assertEqual(len(report['attempts']), 1); self.assertEqual(len(self.owners), 1)
        self.assertEqual(result['verdict'], 'inconclusive'); self.assertNotEqual(report['status'], 'completed')
        self.assertTrue(all(owner.closed for owner in self.owners))

    def test_token_boundary_and_final_verification_time_are_accounted(self):
        # First completed call consumes the entire allowance; no second call.
        self.script.write_text(self.script.read_text().replace('output_tokens=2', 'output_tokens=299983'))
        report, result = self.run_fake()
        self.assertEqual(len(report['attempts']), 1); self.assertEqual(report['budget']['observed_tokens_lower_bound'], 300000)
        self.assertIn('token_limit', report['stop_reasons']); self.assertEqual(result['verdict'], 'inconclusive')
        self.output = self.root/'wall'
        self.script.write_text(self.script.read_text().replace('output_tokens=299983', 'output_tokens=2'))
        original = trial.verify_trial
        def measured(*args):
            value = original(*args); self.clock.advance(600); return value
        with patch.object(trial, 'verify_trial', side_effect=measured) as verification:
            report, result = self.run_fake()
        self.assertEqual(verification.call_count, 1)
        self.assertIn('time_limit', report['stop_reasons']); self.assertEqual(report['budget']['elapsed_seconds'], 600)
        self.assertEqual(result['verdict'], 'inconclusive'); self.assertEqual(artifacts.evaluate(self.output), result)

    def test_mechanical_repair_audit_and_database_cannot_be_forged(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        records[0]['output']['coverage'][0]['fact_indices'] = []
        report, result = self.run_fake(records)
        self.assertTrue(result['quality']['passed'], result)
        artifact = self.output/report['documents'][0]['artifact']
        audit = next(artifact.rglob('coverage-repair.json')); original = audit.read_bytes()
        value = json.loads(original); value['changes'][0]['after'] = []
        audit.write_text(json.dumps(value)); self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])
        audit.write_bytes(original); self.refresh(report)
        with closing(sqlite3.connect(artifact/'state.sqlite3')) as db:
            db.execute("UPDATE items SET status='approved' WHERE kind='fact'"); db.commit()
        self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])

    def test_repair_audit_and_global_extra_call_records_are_verified(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        quote = records[0]['output']['facts'][0]['quote']
        records[0]['output']['facts'][0]['quote'] = 'synthetic missing quote'
        records[0]['repair'] = {'repairs': [{'path': 'facts.0.quote', 'quote': quote}]}
        report, result = self.run_fake(records); self.assertTrue(result['production']['passed'], result)
        audit = next(self.output.glob('documents/*/runs/*/1/quote-repair/changes.json'))
        original = audit.read_bytes(); audit.write_text('[]'); self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])
        audit.write_bytes(original); self.refresh(report)
        extra = self.output/'extra'; extra.mkdir(); (extra/'usage.json').write_text('{}'); self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])

    def test_missing_started_call_differs_from_unstarted_failure_suffix(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        records[0]['output']['coverage'] = []
        report, result = self.run_fake(records)
        self.assertTrue(result['production']['verified_failure'])
        real_snapshot = copy.deepcopy(report); real_snapshot['synthetic_responses'] = False
        verdict = artifacts.decision(real_snapshot, result['safety'], result['production'])
        self.assertEqual(verdict['verdict'], 'rejected')
        call = next(self.output.glob('documents/*/runs/*/1/candidate.json')); call.unlink(); self.refresh(report)
        self.assertIn('artifact_verification_failed', artifacts.evaluate(self.output)['reasons'])

    def test_decision_requires_real_complete_clean_evidence(self):
        report, result = self.run_fake()
        real_snapshot = copy.deepcopy(report); real_snapshot['synthetic_responses'] = False
        self.assertEqual(artifacts.decision(real_snapshot, result['safety'], result['production'])['verdict'], 'accepted_for_further_testing')
        for stop in ('cleanup_error', 'report_write_failed', 'source_changed', 'token_limit', 'time_limit'):
            changed = copy.deepcopy(real_snapshot)
            changed.update(status='stopped', stop_reason=stop, stop_reasons=['document_failed', stop])
            changed['documents'][0]['quality']['passed'] = False
            with self.subTest(stop=stop):
                self.assertEqual(artifacts.decision(changed, result['safety'], result['production'])['verdict'], 'inconclusive')

    def test_report_writer_failure_is_inconclusive_and_no_extra_requests(self):
        original = trial.ReportWriter.write
        def failure(writer, report):
            if len(report['attempts']) >= 1: raise OSError('synthetic report write failure')
            return original(writer, report)
        with patch.object(trial.ReportWriter, 'write', failure): report, result = self.run_fake()
        self.assertEqual(len(report['attempts']), 1)
        self.assertIn('report_write_failed', report['stop_reasons']); self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(artifacts.evaluate(self.output)['verdict'], 'inconclusive')


    def test_third_bad_quote_stops_at_existing_repair_cap(self):
        records = [{'output': copy.deepcopy(r['output'])} for r in self.outputs]
        for record in records:
            quote = record['output']['facts'][0]['quote']
            record['output']['facts'][0]['quote'] = 'synthetic missing quote'
            record['repair'] = {'repairs': [{'path': 'facts.0.quote', 'quote': quote}]}
        report, result = self.run_fake(records)
        self.assertEqual(len(report['attempts']), 5)
        self.assertEqual(sum(a['repair'] for a in report['attempts']), 2)
        self.assertTrue(result['production']['verified_failure'], result)
        self.assertIn('production_contract_failed', result['reasons'])

    def test_pre_dispatch_recording_time_cannot_cross_wall_limit_and_call_provider(self):
        original = trial.atomic_json
        def slow_record(path, value):
            result = original(path, value)
            if Path(path).name == 'candidate.json' and value['cli_call_id'] is None:
                self.clock.advance(600)
            return result
        with patch.object(trial, 'atomic_json', side_effect=slow_record): report, result = self.run_fake()
        self.assertIn('time_limit', report['stop_reasons']); self.assertEqual(result['verdict'], 'inconclusive')
        self.assertFalse((self.root/'transcript').exists())
        self.assertEqual(len(report['attempts']), 1)


    def test_external_worker_death_never_constructs_a_replacement_in_real_pipeline(self):
        original_write = trial.atomic_json
        constructor = trial.Worker
        def kill_after_first(path, value):
            result = original_write(path, value)
            if (Path(path).name == 'candidate.json' and value['chunk'] == 1 and
                    value['repair'] is False and value['cli_call_id'] is not None):
                worker = self.owners[0]._pool.managers[0].worker
                worker.proc.kill(); worker.proc.wait(timeout=2)
            return result
        with patch.object(trial, 'Worker', wraps=constructor) as created, \
             patch.object(trial, 'atomic_json', side_effect=kill_after_first):
            report, result = self.run_fake()
        self.assertEqual(created.call_count, 1)
        self.assertIn('worker_replacement_forbidden', report['stop_reasons'])
        self.assertEqual(result['verdict'], 'inconclusive')
        requests = [json.loads(line) for line in (self.root/'transcript').read_text().splitlines()]
        self.assertEqual(sum(row['text'] != '/clear' for row in requests), 1)
        self.assertEqual(len(report['attempts']), 2)  # Failed lease still has real admission evidence.


class SingleWorkerGuard(unittest.TestCase):
    class StubWorker:
        def __init__(self, binary, model, timing):
            self.key = (binary, model); self.proc = Mock(); self.proc.poll.return_value = None
            self.stopped = threading.Event(); self.manager_closed = None; self.closed = False
        def close(self): self.closed = True; self.stopped.set()

    def setUp(self):
        self.reasons = []; self.manager = trial.SingleWorkerManager(self.reasons.append)
        self.addCleanup(self.manager.close)
        self.timing = Mock()
        self.constructor = self.enterContext(patch.object(trial, 'Worker', side_effect=self.StubWorker))

    def lease(self, binary='binary', model='model', deadline=None):
        return self.manager.acquire(binary, model, time.monotonic()+1 if deadline is None else deadline, self.timing)

    def test_healthy_worker_reused_and_timing_preserved(self):
        with self.lease() as first: self.assertIs(first.manager_closed, self.manager.closed)
        with self.lease() as second: self.assertIs(first, second)
        self.assertEqual(self.constructor.call_count, 1); self.assertTrue(self.manager.creation_attempted)
        self.assertEqual([call.args for call in self.timing.reused.call_args_list], [(False,), (True,)])
        self.assertEqual(self.timing.mark.call_count, 2); self.assertFalse(self.manager.lock.locked())

    def test_dead_stopped_changed_retired_or_missing_worker_never_recreated(self):
        for mode in ('dead', 'stopped', 'key_changed', 'retired', 'missing'):
            with self.subTest(mode=mode):
                manager = trial.SingleWorkerManager(self.reasons.append)
                with manager.acquire('binary', 'model', time.monotonic()+1, self.timing) as worker: pass
                baseline = self.constructor.call_count
                if mode == 'dead': worker.proc.poll.return_value = 1
                elif mode == 'stopped': worker.stopped.set()
                elif mode == 'key_changed': worker.key = ('different', 'model')
                elif mode == 'retired':
                    with manager.lock: manager.retire()
                else:
                    worker.close(); manager.worker = None
                try:
                    for _ in range(2):
                        with self.assertRaisesRegex(ValueError, 'second worker'):
                            with manager.acquire('binary', 'model', time.monotonic()+1, self.timing): pass
                    self.assertEqual(self.constructor.call_count, baseline)
                    self.assertTrue(manager.creation_attempted); self.assertFalse(manager.lock.locked())
                finally: manager.close()

    def test_constructor_failure_consumes_permanent_allowance(self):
        self.constructor.side_effect = OSError('constructor failed')
        with self.assertRaises(OSError):
            with self.lease(): pass
        with self.manager.lock: self.manager.retire()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'second worker'):
                with self.lease(): pass
        self.assertEqual(self.constructor.call_count, 1); self.assertFalse(self.manager.lock.locked())
        self.assertIn('worker_creation_failed', self.reasons)

    def test_exchange_exception_retires_without_resetting_allowance(self):
        with self.assertRaisesRegex(RuntimeError, 'exchange failed'):
            with self.lease() as worker: raise RuntimeError('exchange failed')
        self.assertTrue(worker.closed); self.assertIsNone(self.manager.worker)
        with self.assertRaisesRegex(ValueError, 'second worker'):
            with self.lease(): pass
        self.assertEqual(self.constructor.call_count, 1); self.assertIn('worker_exchange_failed', self.reasons)

    def test_death_after_health_check_can_fail_exchange_but_never_respawns(self):
        with self.lease() as worker: pass
        def die_after_check(reused):
            if reused: worker.proc.poll.return_value = 9
        self.timing.reused.side_effect = die_after_check
        with self.assertRaises(ConnectionError):
            with self.lease() as same:
                self.assertIs(same, worker); self.assertEqual(same.proc.poll(), 9)
                raise ConnectionError('process died after health check')
        with self.assertRaisesRegex(ValueError, 'second worker'):
            with self.lease(): pass
        self.assertEqual(self.constructor.call_count, 1)

    def test_expired_deadline_lock_wait_and_closed_manager_do_not_construct(self):
        with self.assertRaises(TimeoutError):
            with self.lease(deadline=time.monotonic()-1): pass
        self.manager.lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                with self.lease(deadline=time.monotonic()+.01): pass
            self.manager.closed.set()
            with self.assertRaises(ValueError):
                with self.lease(): pass
        finally: self.manager.lock.release()
        with self.assertRaises(ValueError):
            with self.lease(): pass
        self.assertEqual(self.constructor.call_count, 0); self.assertFalse(self.manager.lock.locked())

    def test_close_signals_active_exchange_then_releases_and_never_reopens(self):
        closer = None
        with self.lease() as worker:
            closer = threading.Thread(target=self.manager.close); closer.start()
            self.assertTrue(worker.manager_closed.wait(timeout=1))
        closer.join(timeout=1)
        self.assertFalse(closer.is_alive()); self.assertTrue(worker.closed)
        with self.assertRaises(ValueError):
            with self.lease(): pass
        self.assertEqual(self.constructor.call_count, 1)

    def test_installation_only_replaces_unused_single_slot_manager(self):
        spec = trial.ExtractionSpec(sys.executable, 'sonnet', 1, 90, 1048576, 'null')
        owner = trial.ExtractionTransport(spec); self.addCleanup(owner.close)
        old = owner._pool.managers[0]
        guarded = trial.install_single_worker_guard(owner, trial.TrialBudget())
        self.assertIs(owner._pool.managers[0], guarded); self.assertTrue(old.closed.is_set())
        self.assertEqual(self.constructor.call_count, 0)
        with self.assertRaises(ValueError): trial.install_single_worker_guard(owner, trial.TrialBudget())
        for mode in ('leased', 'waiting', 'generation', 'identity', 'worker', 'locked', 'closed'):
            other = trial.ExtractionTransport(spec); manager = other._pool.managers[0]
            try:
                if mode == 'leased': other._pool.leased.add(0)
                elif mode == 'waiting': other._pool.waiting = 1
                elif mode == 'generation': other._pool.generations[0] = 1
                elif mode == 'identity': other._pool.worker_ids[0] = 'prior'
                elif mode == 'worker': manager.worker = self.StubWorker('binary', 'model', self.timing)
                elif mode == 'locked': manager.lock.acquire()
                else: manager.closed.set()
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    trial.install_single_worker_guard(other, trial.TrialBudget())
                self.assertIs(other._pool.managers[0], manager)
            finally:
                if mode == 'locked': manager.lock.release()
                other._pool.leased.clear(); other._pool.waiting = 0
                other.close()
        self.assertEqual(self.constructor.call_count, 0)
