"""Dedicated leases use only a local fake streaming subprocess."""
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_claude_worker import FAKE

from verinote.agents import CLI
from verinote.cli_timing import read_cli_timing
from verinote.extraction_benchmark import Budget, BudgetCaller, Limits, call_metrics
from verinote.extraction_transport import ExtractionTransport
from verinote.service import Service
from verinote.store import Store

SCRIPT = FAKE.replace("    if text=='wait':", "    if text.startswith('hold-'):\n        (root/text).write_text(str(os.getpid()))\n        while not (root/('release-'+text)).exists():time.sleep(.01)\n    if text=='wait':")


class ExtractionWorkers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.script = self.root/'fake.py'; self.script.write_text(SCRIPT)
        self.addCleanup(patch.stopall)
        patch('verinote.agents.executable', return_value=sys.executable).start()
        patch('verinote.extraction_transport.executable', return_value=sys.executable).start()
        patch('verinote.claude_worker.command', side_effect=lambda b, m: [b, str(self.script), str(self.root), m]).start()
        self.cli = CLI(timeout=3); self.addCleanup(self.cli.close)
        self.owner = ExtractionTransport.from_cli(self.cli, 'model'); self.addCleanup(self.owner.close)
        self.executor = ThreadPoolExecutor(max_workers=3)
        # Signal processes before waiting for outstanding test calls on failure.
        self.addCleanup(self.executor.shutdown, wait=True)
        self.addCleanup(self.owner.close)

    def call(self, prompt, folder, view=None):
        return (view or self.owner.view()).complete('claude', 'model', prompt, self.root/folder)

    def wait_file(self, name):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            path = self.root/name
            if path.exists(): return path
            time.sleep(.005)
        self.fail('fake subprocess did not reach '+name)

    def hold(self, name):
        f = self.executor.submit(self.call, 'hold-'+name, name)
        pid = int(self.wait_file('hold-'+name).read_text())
        return f, pid

    def release(self, name): (self.root/('release-hold-'+name)).write_text('go')

    def test_targeted_slot_views_preserve_target_and_never_borrow_idle_slot(self):
        second = self.owner.slot_view(2)
        first = self.call('slot-two', 'slot-two', second)
        record = call_metrics(self.root/'slot-two', 'claude', 'model')
        self.assertEqual(record['worker_identity']['slot_id'], 2)
        limited = copy.copy(second).limited_timeout('claude', 2)
        again = self.call('slot-two-again', 'slot-two-again', limited)
        self.assertEqual(first['pid'], again['pid'])
        second.close(); self.assertFalse(self.owner.closed)
        held = self.executor.submit(self.call, 'hold-target', 'target', second)
        self.wait_file('hold-target')
        with self.assertRaises(TimeoutError):
            self.call('must-not-use-idle-one', 'target-timeout', second.limited_timeout('claude', .03))
        self.assertIsNone(self.owner._pool.managers[0].worker)
        self.release('target'); held.result(timeout=3)
        for slot in (0, 3, True):
            with self.assertRaises(ValueError): self.owner.slot_view(slot)

    def test_two_pids_overlap_third_waits_and_views_share_capacity(self):
        a, p1 = self.hold('a'); b, p2 = self.hold('b'); self.assertNotEqual(p1, p2)
        view = copy.copy(self.owner).limited_timeout('claude', 2); view.close()
        c = self.executor.submit(self.call, 'third', 'c', view)
        deadline = time.monotonic()+2
        while not self.owner._pool.waiting and time.monotonic() < deadline: time.sleep(.005)
        self.assertEqual(self.owner._pool.waiting, 1); self.assertFalse(c.done())
        self.assertEqual(len(self.owner._pool.managers), 2)
        self.release('a'); first = a.result(timeout=3); third = c.result(timeout=3)
        self.assertEqual(first['pid'], third['pid']); self.assertEqual(third['history'], [])
        self.release('b'); second = b.result(timeout=3)
        self.assertEqual(second['history'], [])
        records = [call_metrics(self.root/f, 'claude', 'model') for f in ('a', 'b', 'c')]
        self.assertEqual(len({r['cli_call_id'] for r in records}), 3)
        self.assertEqual([r['tokens']['input'] for r in records], [17, 17, 17])
        self.assertEqual({r['worker_identity']['slot_id'] for r in records}, {1, 2})
        self.assertEqual({r['worker_identity']['generation'] for r in records}, {1})
        timing = read_cli_timing(self.root/'c', '.')
        self.assertTrue(timing['available']); self.assertGreater(timing['durations_seconds']['worker_wait'], 0)
        transcript = [json.loads(line) for line in (self.root/'transcript').read_text().splitlines()]
        self.assertEqual(sum(row['text']=='/clear' for row in transcript), 3)

    def test_wait_timeout_and_one_failure_leave_other_worker_alive(self):
        a, _ = self.hold('a'); b, p2 = self.hold('b')
        with self.assertRaises(TimeoutError): self.call('never', 'waiting', self.owner.limited_timeout('claude', .05))
        self.assertFalse(a.done()); self.assertFalse(b.done())
        self.release('a'); a.result(timeout=3)
        with self.assertRaises(ValueError): self.call('eof', 'failure')
        self.assertFalse(b.done())
        fresh = self.call('next-explicit', 'next')
        self.assertNotEqual(fresh['pid'], p2)
        failed = call_metrics(self.root/'failure', 'claude', 'model')
        after = call_metrics(self.root/'next', 'claude', 'model')
        self.assertEqual(failed['worker_identity']['slot_id'], after['worker_identity']['slot_id'])
        self.assertEqual(after['worker_identity']['generation'], 2)
        self.release('b'); self.assertEqual(b.result(timeout=3)['pid'], p2)
        texts = [json.loads(line)['text'] for line in (self.root/'transcript').read_text().splitlines()]
        self.assertEqual(texts.count('eof'), 1); self.assertNotIn('never', texts)

    def test_exchange_timeout_and_interpretation_failure_are_slot_local(self):
        healthy, pid = self.hold('healthy')
        for prompt, error in [('hang', TimeoutError), ('invalid_body', ValueError)]:
            with self.subTest(prompt=prompt):
                with self.assertRaises(error): self.call(prompt, prompt, self.owner.limited_timeout('claude', .15))
                self.assertFalse(healthy.done())
        self.release('healthy'); self.assertEqual(healthy.result(timeout=3)['pid'], pid)

    def test_lease_wait_uses_original_deadline(self):
        owner = ExtractionTransport.from_cli(self.cli, 'model', capacity=1); self.addCleanup(owner.close)
        first = self.executor.submit(self.call, 'hold-deadline', 'first', owner.view())
        self.wait_file('hold-deadline')
        started = time.monotonic()
        second = self.executor.submit(self.call, 'hang', 'second', owner.limited_timeout('claude', .35))
        # Hold most of the budget at the lease boundary; exchange must get only the remainder.
        time.sleep(.25); self.release('deadline'); first.result(timeout=3)
        with self.assertRaises(TimeoutError): second.result(timeout=3)
        elapsed = time.monotonic()-started
        self.assertLess(elapsed, .55)
        timing = read_cli_timing(self.root/'second', '.')
        self.assertTrue(timing['available']); self.assertGreater(timing['durations_seconds']['worker_wait'], .20)
        self.assertLess(timing['durations_seconds']['total'], .55)

    def test_close_wakes_waiter_and_reaps_both_active_processes(self):
        a, _ = self.hold('a'); b, _ = self.hold('b')
        workers = [m.worker for m in self.owner._pool.managers]
        c = self.executor.submit(self.call, 'queued', 'c')
        self.owner.close(); self.owner.close()
        for f in (a, b, c):
            with self.assertRaises((ValueError, TimeoutError)): f.result(timeout=3)
        for worker in workers:
            self.assertIsNotNone(worker.proc.poll())
            self.assertFalse(Path(worker.directory.name).exists())
            self.assertFalse(any(t.is_alive() for t in worker.threads))
        with self.assertRaises(ValueError): self.call('after', 'after')

    def test_direct_manager_shutdown_cancels_only_its_active_exchange(self):
        a, _ = self.hold('a'); b, p2 = self.hold('b')
        first_worker = self.owner._pool.managers[0].worker
        self.owner._pool.managers[0].close()
        with self.assertRaises(ValueError): a.result(timeout=3)
        self.assertFalse(b.done())
        self.assertIsNotNone(first_worker.proc.poll())
        self.assertFalse(any(t.is_alive() for t in first_worker.threads))
        # A partially shut-down pool is terminal for new admissions; no silent slot recreation.
        with self.assertRaises(ValueError): self.call('no-replacement', 'blocked')
        self.release('b'); self.assertEqual(b.result(timeout=3)['pid'], p2)

    def test_frozen_settings_pinned_binary_and_default_timeout_semantics(self):
        rate = {'model': 'fake-model', 'input': 1, 'output': 1, 'cache_read': 0, 'cache_write': 0, 'credit_unit': '', 'usd_per_credit': None}
        base = CLI(rates=lambda: {'claude': rate}); self.addCleanup(base.close)
        owner = ExtractionTransport.from_cli(base, 'model'); self.addCleanup(owner.close)
        self.assertEqual(owner.effective_timeout('claude'), 180)
        limited = owner.limited_timeout('claude', 360).limited_timeout('claude', 400)
        self.assertEqual(limited.effective_timeout('claude'), 360)
        base.timeout = .001; base.max_output = 1; rate['input'] = 99
        self.assertEqual(owner.effective_timeout('claude'), 180); self.assertGreater(owner.identity.max_output, 1)
        self.assertIn('1', owner.identity.rate_json); self.assertNotIn('99', owner.identity.rate_json)
        with patch('verinote.agents.executable', side_effect=AssertionError('must use pinned binary')):
            self.assertEqual(self.call('pinned', 'pinned', owner.view())['prompt'], 'pinned')
        for agent, model, phase in [('agy','model',None),('claude','other',None),('claude','model','ontology')]:
            with self.assertRaises(ValueError): owner.complete(agent, model, 'bad', self.root/'bad', phase=phase)
        for capacity in (0, 3, True):
            with self.assertRaises(ValueError): ExtractionTransport.from_cli(base, 'model', capacity=capacity)
        self.owner.close()
        self.assertEqual(self.cli.complete('claude', 'model', 'shared-still-open', self.root/'shared')['prompt'], 'shared-still-open')

    def test_service_identity_replacement_is_atomic_with_old_views(self):
        service = Service(Store(self.root/'state.sqlite3'), self.root, self.cli); self.addCleanup(service.close)
        first = service.extraction_transport('model'); same = service.extraction_transport('model')
        self.assertIs(first._pool, same._pool)
        future = self.executor.submit(self.call, 'hold-service', 'service', first)
        self.wait_file('hold-service')
        with self.assertRaises(ValueError): service.extraction_transport('other')
        self.assertFalse(first.closed)
        self.release('service'); future.result(timeout=3)
        replacement = service.extraction_transport('other')
        self.assertTrue(first.closed); self.assertIsNot(first._pool, replacement._pool)
        with self.assertRaises(ValueError): self.call('stale', 'stale', first)
        barrier = threading.Barrier(2)
        def get_view(): barrier.wait(timeout=3); return service.extraction_transport('other')
        futures = [self.executor.submit(get_view) for _ in range(2)]
        self.assertIs(futures[0].result(timeout=3)._pool, futures[1].result(timeout=3)._pool)
        service.close()
        with self.assertRaises(ValueError): service.extraction_transport('model')

    def test_replacement_closes_admission_before_old_view_can_acquire(self):
        service = Service(Store(self.root/'state.sqlite3'), self.root, self.cli); self.addCleanup(service.close)
        old = service.extraction_transport('model')
        marked = threading.Event(); proceed = threading.Event()
        original = old._pool._mark_closed
        def mark_closed():
            original(); marked.set()
            self.assertTrue(proceed.wait(3))
        with patch.object(old._pool, '_mark_closed', side_effect=mark_closed):
            replacement = self.executor.submit(service.extraction_transport, 'other')
            self.assertTrue(marked.wait(3))
            stale = self.executor.submit(self.call, 'must-not-run', 'stale-race', old)
            with self.assertRaises(ValueError): stale.result(timeout=3)
            proceed.set(); replacement.result(timeout=3)
        self.assertFalse((self.root/'transcript').exists())

    def test_close_failure_still_attempts_other_manager_and_shared_cli(self):
        pool = self.owner._pool
        with patch.object(pool.managers[0], 'close', side_effect=OSError('first')), patch.object(pool.managers[1], 'close') as second:
            with self.assertRaises(OSError): self.owner.close()
            second.assert_called_once()
            self.assertTrue(all(m.closed.is_set() for m in pool.managers))
        service = Service(Store(self.root/'state.sqlite3'), self.root, self.cli); self.addCleanup(service.close)
        service.extraction_transport('model')
        with patch.object(service.extraction_owner, 'close', side_effect=OSError('owner')), patch.object(self.cli, 'close') as shared:
            with self.assertRaises(OSError): service.close()
            shared.assert_called_once()

    def test_retire_failure_retains_resource_and_blocks_reuse_or_spawn(self):
        self.call('first', 'first')
        manager = self.owner._pool.managers[0]; worker = manager.worker
        with patch.object(worker, 'close', side_effect=OSError('cleanup')):
            with self.assertRaises(OSError) as caught: self.call('eof', 'failure')
            self.assertIsNotNone(caught.exception.__context__)
            self.assertIs(manager.worker, worker); self.assertTrue(worker.stopped.is_set())
            with patch('verinote.claude_worker.subprocess.Popen') as spawn:
                with self.assertRaises(OSError): self.call('must-not-send', 'blocked')
                spawn.assert_not_called()
            self.assertIs(manager.worker, worker)
        self.call('explicit-recovery', 'recovered')
        self.assertFalse(Path(worker.directory.name).exists())
        self.assertIsNot(manager.worker, worker)
        self.assertEqual(call_metrics(self.root/'recovered', 'claude', 'model')['worker_identity']['generation'], 2)

    def test_close_retry_can_reap_retained_worker_after_cleanup_error(self):
        self.call('first', 'first'); manager = self.owner._pool.managers[0]; worker = manager.worker
        with patch.object(worker, 'close', side_effect=OSError('cleanup')):
            with self.assertRaises(OSError): self.owner.close()
            self.assertIs(manager.worker, worker)
        self.owner.close()
        self.assertIsNone(manager.worker); self.assertIsNotNone(worker.proc.poll())
        self.assertFalse(Path(worker.directory.name).exists())
        self.assertFalse(any(thread.is_alive() for thread in worker.threads))

    def test_spawn_failure_returns_slot_without_retry(self):
        with patch('verinote.claude_worker.subprocess.Popen', side_effect=OSError('spawn')) as spawn:
            with self.assertRaises(OSError): self.call('failure', 'failure')
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.owner._pool.leased, set())
        self.assertEqual(self.call('explicit-next', 'next')['prompt'], 'explicit-next')

    def test_partial_pool_markers_never_fall_back_to_legacy_classification(self):
        self.call('first', 'identity')
        folder = self.root/'identity'
        meta = json.loads((folder/'run.json').read_text())
        usage = json.loads((folder/'usage.json').read_text())
        wrapper = BudgetCaller(self.owner.view(), Budget(Limits()))
        mutations = [
            lambda m, u: m.pop('worker_scope'),
            lambda m, u: u.pop('worker_scope'),
            lambda m, u: m.pop('worker_identity'),
            lambda m, u: u.pop('worker_identity'),
            lambda m, u: m.update(worker_scope='other'),
            lambda m, u: u.update(worker_scope=None),
            lambda m, u: [r.pop('worker_scope') for r in (m, u)],
            lambda m, u: ([r.pop('worker_scope') for r in (m, u)], u.pop('worker_identity')),
            lambda m, u: (m.clear(), m.update({k: v for k, v in meta.items() if k not in ('worker_scope', 'worker_identity')})),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                m, u = copy.deepcopy(meta), copy.deepcopy(usage); mutate(m, u)
                (folder/'run.json').write_text(json.dumps(m)); (folder/'usage.json').write_text(json.dumps(u))
                row = call_metrics(folder, 'claude', 'model')
                self.assertIsNotNone(row['cli_timing'])
                self.assertIsNotNone(row['worker_scope'])
                self.assertIsNone(row['worker_identity'])
                self.assertEqual(wrapper.process_observation(row['cli_timing'], row['worker_identity'], row['worker_scope']), 'unknown')
        # Actual default CLI records have neither marker; their existing evidence remains valid.
        self.cli.complete('claude', 'model', 'legacy', self.root/'legacy')
        row = call_metrics(self.root/'legacy', 'claude', 'model')
        self.assertIsNone(row['worker_scope']); self.assertIsNone(row['worker_identity'])
        self.assertEqual(wrapper.process_observation(row['cli_timing'], row['worker_identity'], row['worker_scope']), 'started')

    def test_pool_identity_classification_is_independent_of_arrival_order(self):
        a, _ = self.hold('a'); b, _ = self.hold('b')
        self.release('a'); a.result(timeout=3)
        self.call('reuse', 'reuse')
        original = call_metrics(self.root/'a', 'claude', 'model')
        reused = call_metrics(self.root/'reuse', 'claude', 'model')
        with self.assertRaises(ValueError): self.call('eof', 'failed')
        self.call('restart', 'restart')
        restarted = call_metrics(self.root/'restart', 'claude', 'model')
        wrapper = BudgetCaller(self.owner.view(), Budget(Limits()))
        def classify(row): return wrapper.process_observation(row['cli_timing'], row['worker_identity'], row['worker_scope'])
        self.assertEqual(classify(restarted), 'restarted')
        self.assertEqual(classify(reused), 'reused')
        self.assertEqual(classify(original), 'started')
        self.release('b'); b.result(timeout=3)
        self.assertEqual(classify(call_metrics(self.root/'b', 'claude', 'model')), 'started')
        usage_path = self.root/'a'/'usage.json'; value = json.loads(usage_path.read_text())
        value['worker_identity']['generation'] = 99; usage_path.write_text(json.dumps(value))
        bad = call_metrics(self.root/'a', 'claude', 'model')
        self.assertIsNone(bad['worker_identity']); self.assertEqual(classify(bad), 'unknown')
