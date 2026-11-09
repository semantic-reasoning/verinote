"""Deterministic lane and document lease tests; no external providers."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from verinote.job_scheduler import JobScheduler


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.pool = JobScheduler()
        self.release = threading.Event()
        self.addCleanup(lambda: (self.release.set(), self.pool.shutdown()))

    def job(self, key, stage='extract', doc=None):
        return {'id': key, 'stage': stage, 'document_id': doc or key} if stage != 'question' else {'id': key}

    def hold(self, event):
        def run(job):
            event.set()
            if not self.release.wait(5): raise TimeoutError('test gate')
        return run

    def test_two_each_stage_and_one_question_with_third_jobs_pending(self):
        events = [threading.Event() for _ in range(5)]
        jobs = [self.job(str(i), lane) for i, lane in enumerate(('ontology','ontology','extract','extract','question'))]
        for e, j in zip(events, jobs, strict=True): self.pool.submit(self.hold(e), j)
        for e in events: self.assertTrue(e.wait(2))
        later = [self.pool.submit(lambda j: j['id'], self.job('next-'+s, s)) for s in ('ontology','extract','question')]
        self.assertTrue(all(not f.running() for f in later))
        self.release.set()
        for f in later: self.assertTrue(f.result(3).startswith('next'))

    def test_document_blocker_is_bypassed_and_held_across_stages(self):
        entered = threading.Event()
        first = self.pool.submit(self.hold(entered), self.job('old', 'ontology', 'doc'))
        self.assertTrue(entered.wait(2))
        retry = self.pool.submit(lambda j: 'retry', self.job('retry', 'extract', 'doc'))
        other = self.pool.submit(lambda j: 'other', self.job('other', 'extract'))
        self.assertEqual(other.result(2), 'other')
        self.assertFalse(retry.running())
        self.assertEqual(self.pool.context('retry')['document_blockers'], [('run', 'old')])
        self.release.set(); first.result(2); self.assertEqual(retry.result(2), 'retry')

    def test_cancelled_pending_job_never_executes_or_holds_lease(self):
        e = threading.Event(); self.pool.submit(self.hold(e), self.job('a', doc='same'))
        self.assertTrue(e.wait(2))
        calls = []
        pending = self.pool.submit(lambda j: calls.append(j), self.job('b', doc='same'))
        self.assertTrue(pending.cancel()); self.release.set(); self.pool.wait_idle()
        self.assertEqual(calls, [])

    def test_thread_start_failure_leaves_no_executable_job(self):
        calls = []
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                self.pool.submit(lambda j: calls.append(j), self.job('failed'))
        self.pool.submit(lambda j: calls.append(j['id']), self.job('later')).result(2)
        self.assertEqual(calls, ['later'])

    def test_shutdown_drains_document_blocked_jobs_and_rejects_new_work(self):
        e = threading.Event(); self.pool.submit(self.hold(e), self.job('a', doc='same'))
        self.assertTrue(e.wait(2))
        pending = self.pool.submit(lambda j: 'done', self.job('b', doc='same'))
        with ThreadPoolExecutor(1) as other:
            close = other.submit(self.pool.shutdown)
            with self.pool.condition:
                self.pool.condition.wait_for(lambda: self.pool.closed, timeout=2)
            with self.assertRaises(RuntimeError): self.pool.submit(lambda: None)
            self.assertFalse(close.done()); self.release.set(); close.result(2)
        self.assertEqual(pending.result(), 'done'); self.pool.shutdown()

    def test_future_callback_can_submit_without_scheduler_lock(self):
        entered = threading.Event()
        first = self.pool.submit(self.hold(entered), self.job('a'))
        self.assertTrue(entered.wait(2))
        results = []
        done = threading.Event()
        def callback(future):
            results.append(self.pool.submit(lambda: 42).result(2)); done.set()
        first.add_done_callback(callback)
        self.release.set(); self.assertTrue(done.wait(3)); self.assertEqual(results, [42])

    def test_partial_worker_start_failure_does_not_admit_failed_job(self):
        start = threading.Thread.start
        count = []
        calls = []
        def partial(thread):
            count.append(thread)
            if len(count)==2: raise RuntimeError('second worker')
            return start(thread)
        with patch.object(threading.Thread,'start',side_effect=partial,autospec=True):
            with self.assertRaises(RuntimeError): self.pool.submit(lambda j: calls.append('failed'),self.job('failed'))
        self.pool.submit(lambda j: calls.append('ok'),self.job('ok')).result(2)
        self.assertEqual(calls,['ok'])
