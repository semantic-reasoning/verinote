"""Synthetic calls only: a worker that ends leaves its run in a final state and returns its slot."""
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from test_failed_run_detail import (
    Base,
    Parallel,
    Sequential,
    extraction_response,
    fails_at,
    from_chunk,
    ok,
)

from verinote.extraction_checkpoint import ExtractionCheckpoint
from verinote.extraction_timing import ExtractionTiming
from verinote.service import Service
from verinote.store import Store


class RunFinalState(Base):
    def free_slots(self, service):
        taken = 0
        while service.slots.acquire(blocking=False): taken += 1
        for _ in range(taken): service.slots.release()
        return taken

    def test_worker_start_raising_ends_queued_run_as_error_and_returns_slot(self):
        doc = self.document()
        def boom(timing): raise RuntimeError('worker_start')
        for capacity in (None, 1):
            with self.subTest(capacity=capacity):
                caller = Sequential(ok) if capacity is None else Parallel(ok, capacity)
                service = Service(self.store, self.root, caller, **({} if capacity is None else {'extraction_caller': caller}))
                self.addCleanup(service.close)
                version, generation = self.current(doc)
                with patch.object(ExtractionTiming, 'worker_start', boom):
                    run = service.start(doc['id'], 'copilot' if capacity is None else 'claude', 'model', expected_version=version,
                                        expected_generation=generation, **({} if capacity is None else {'extraction_concurrency': capacity}))
                    service.pool.shutdown(wait=True)
                run = self.store.run(run['id'])
                self.assertEqual((run['status'], run['detail'], caller.calls), ('error', 'worker_start', []))
                self.assertEqual(self.free_slots(service), 8)
                with self.store.connect(True) as db:  # #829's refusal reads the same rows
                    Store.refuse_while_run_holds_dictionary(db, doc['id'], run['version'])
        run, _ = self.sequential(doc, ok)
        self.assertEqual(run['status'], 'success', run['detail'])

    def failing_error_writes(self, refuse, commit_first=False):
        """Patches run_status and end_unstored_run: every error write is recorded, and raises when refuse(n, detail) is true (after
        committing when commit_first); the fixed-sentence write of end_unstored_run is recorded only when the stored run is queued or
        running, and when refused, not applied."""
        real, real_end, seen = self.store.run_status, self.store.end_unstored_run, []
        def write(run_id, status, detail=''):
            if status == 'error':
                seen.append(detail)
                if refuse(len(seen), detail):
                    if commit_first: real(run_id, status, detail)
                    raise OSError('database')
            return real(run_id, status, detail)
        def end(run_id, detail):
            if self.store.run(run_id)['status'] not in ('queued', 'running'): return real_end(run_id, detail)  # nothing is written: not recorded
            seen.append(detail)
            if refuse(len(seen), detail): raise OSError('database')
            return real_end(run_id, detail)
        @contextmanager
        def patched():
            with patch.object(self.store, 'run_status', write), patch.object(self.store, 'end_unstored_run', end): yield
        return patched(), seen

    def test_error_write_that_committed_before_raising_is_not_overwritten(self):
        doc = self.document()
        writes, seen = self.failing_error_writes(lambda n, detail: True, commit_first=True)
        with writes: run, _ = self.sequential(doc, fails_at(2))
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['detail'].endswith(' · ' + from_chunk(2)), run['detail'])
        self.assertEqual(seen, [run['detail']] * 2)

    def test_registration_error_that_cannot_be_written_falls_back_to_the_fixed_sentence(self):
        run, seen, free, error = self.registration_failure(lambda n, detail: detail != Service.FAILURE_FALLBACK)
        self.assertEqual((run['status'], run['detail'], seen, free, str(error)), ('error', Service.FAILURE_FALLBACK, ['disk', 'disk', Service.FAILURE_FALLBACK], 8, 'disk'))

    def test_a_run_cancelled_before_the_fixed_sentence_ends_cancelled_with_its_last_error(self):
        doc = self.document()
        holder = {}
        def refuse(n, detail):
            if n == 2: self.store.cancel_run(holder['id'], 'running')
            return detail != Service.FAILURE_FALLBACK
        writes, seen = self.failing_error_writes(refuse)
        real_create = self.store.create_run
        def create(*a, **k):
            run = real_create(*a, **k); holder['id'] = run['id']; return run
        with writes, patch.object(self.store, 'create_run', create): run, _ = self.sequential(doc, fails_at(2))
        self.assertEqual(run['status'], 'cancelled', run['detail'])
        self.assertTrue(run['detail'].startswith('처리 중에 취소되었습니다. '), run['detail'])
        self.assertIn('멈추기 전 마지막 오류: 청크 2 · stop', run['detail'])
        self.assertEqual(len(seen), 2)

    def test_fallback_sees_a_cancel_committed_before_its_write_transaction_opens(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', *self.current(doc))
        self.store.run_status(run['id'], 'running')
        real, armed = self.store.connect, [True]
        def connect(write=False):  # a cancel commits just before the fallback's first write transaction opens
            if write and armed[0]:
                armed[0] = False
                self.store.cancel_run(run['id'], 'running')
            return real(write)
        from verinote.store import RunCancelled
        with patch.object(self.store, 'connect', connect), self.assertRaises(RunCancelled):
            self.store.end_unstored_run(run['id'], Service.FAILURE_FALLBACK)
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelling')

    def test_fallback_reads_the_status_and_writes_in_one_write_transaction(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', *self.current(doc))
        self.store.run_status(run['id'], 'running')
        real, opened, cancelling = self.store.connect, [], [False]
        def connect(write=False):  # a cancel would commit between two transactions, so a second connection is where it could slip in
            if cancelling[0]: return real(write)
            opened.append(write)
            if len(opened) > 1:
                cancelling[0] = True
                self.store.cancel_run(run['id'], 'running')
                cancelling[0] = False
            return real(write)
        with patch.object(self.store, 'connect', connect):
            ended = self.store.end_unstored_run(run['id'], Service.FAILURE_FALLBACK)
        self.assertEqual((ended, opened), (True, [True]))
        self.assertEqual((self.store.run(run['id'])['status'], self.store.run(run['id'])['detail']), ('error', Service.FAILURE_FALLBACK))

    def test_fallback_keeps_a_run_stored_as_ended(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', *self.current(doc))
        self.store.run_status(run['id'], 'error', 'kept')
        self.assertFalse(self.store.end_unstored_run(run['id'], Service.FAILURE_FALLBACK))
        self.assertEqual((self.store.run(run['id'])['status'], self.store.run(run['id'])['detail']), ('error', 'kept'))

    def test_error_write_raising_once_is_retried_with_the_same_detail(self):
        doc = self.document()
        writes, seen = self.failing_error_writes(lambda n, detail: n == 1)
        with writes: run, _ = self.sequential(doc, fails_at(2))
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['detail'].endswith(' · ' + from_chunk(2)), run['detail'])
        self.assertEqual(seen, [run['detail']] * 2)

    def test_detail_that_cannot_be_written_falls_back_to_the_fixed_sentence(self):
        doc = self.document()
        writes, seen = self.failing_error_writes(lambda n, detail: detail != Service.FAILURE_FALLBACK)
        with writes: run, _ = self.sequential(doc, fails_at(2))
        self.assertEqual((run['status'], run['detail']), ('error', Service.FAILURE_FALLBACK))
        self.assertEqual(len(seen), 3)

    def test_failure_bookkeeping_raising_still_ends_the_run_as_error(self):
        doc = self.document()
        def boom(checkpoint, status): raise RuntimeError('bookkeeping')
        with patch.object(ExtractionCheckpoint, 'finish', boom):
            run, _ = self.sequential(doc, fails_at(2))
        self.assertEqual((run['status'], run['detail']), ('error', '청크 2 · stop'))

    def test_detail_with_characters_sqlite_cannot_store_is_written_escaped(self):
        doc = self.document()
        def respond(index, repairing):  # a model answer whose relation ID holds a lone surrogate, as the JSON escape \ud800 parses
            value = extraction_response()
            if index == 2: value['facts'][0]['predicate'] = 'rel\ud800'
            return value
        run, _ = self.sequential(doc, respond)  # the real store refuses the raw text: no store patch
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['detail'].startswith('청크 2 · '), run['detail'])
        self.assertIn('승인 사전에 없는 관계: rel\\ud800 · 검증 완료 1/3청크', run['detail'])
        self.assertTrue(run['detail'].endswith(' · ' + from_chunk(2)), run['detail'])

    def test_every_error_write_raising_leaves_the_run_as_it_was_and_returns_the_slot(self):
        for capacity in (None, 1):
            with self.subTest(capacity=capacity):
                self.tearDown(); self.setUp()
                self.every_error_write_raising(capacity)

    def every_error_write_raising(self, capacity):
        doc = self.document()
        writes, seen = self.failing_error_writes(lambda n, detail: True)
        registered = []
        def respond(index, repairing):
            if index == 2: registered.append(dict(service.registered_extractions))
            return fails_at(2)(index, repairing)
        caller = Sequential(respond) if capacity is None else Parallel(respond, capacity)
        service = Service(self.store, self.root, caller, **({} if capacity is None else {'extraction_caller': caller}))
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        with writes:
            run = service.start(doc['id'], 'copilot' if capacity is None else 'claude', 'model', expected_version=version, expected_generation=generation,
                                **({} if capacity is None else {'extraction_concurrency': capacity}))
            service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'running')
        self.assertEqual((len(seen), seen[-1], self.free_slots(service), service.registered_extractions), (3, Service.FAILURE_FALLBACK, 8, {}))
        self.assertEqual([bool(r) for r in registered], [capacity is not None])  # a parallel run is registered while it runs

    def test_status_write_retries_after_a_failed_registration_do_not_hold_the_extraction_lock(self):
        doc = self.document()
        entered, release, real = threading.Event(), threading.Event(), self.store.run_status
        def write(run_id, status, detail=''):
            if status == 'error':
                entered.set()
                self.assertTrue(release.wait(10))
            return real(run_id, status, detail)
        from verinote import usage
        def disk(path, value): raise OSError('disk')
        service = Service(self.store, self.root, Sequential(ok))
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        failure = []
        def start():
            try: service.start(doc['id'], 'copilot', 'model', expected_version=version, expected_generation=generation)
            except OSError as exc: failure.append(exc)
        held = False
        with patch.object(self.store, 'run_status', write), patch.object(usage, 'atomic_json', disk):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                held = service.extraction_lock.acquire(timeout=3)  # what a concurrent start does first
                if held: service.extraction_lock.release()
            finally:
                release.set()
                thread.join(10)
        self.assertTrue(held, 'a status write is waiting while extraction_lock is held')
        self.assertEqual([str(e) for e in failure], ['disk'])
        self.assertEqual(self.free_slots(service), 8)

    def test_exception_whose_text_cannot_be_read_still_ends_the_run_as_error(self):
        class Unreadable(Exception):
            def __str__(self): raise RuntimeError('str')
        doc = self.document()
        def respond(index, repairing):
            if index == 2: raise Unreadable()
            return extraction_response()
        run, _ = self.sequential(doc, respond)
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['detail'].startswith('청크 2 · Unreadable · '), run['detail'])

    def test_registration_failure_whose_text_cannot_be_read_still_ends_the_run_as_error(self):
        class Unreadable(OSError):
            def __str__(self): raise RuntimeError('str')
        doc = self.document()
        from verinote import usage
        def disk(path, value): raise Unreadable()
        service = Service(self.store, self.root, Sequential(ok))
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        with patch.object(usage, 'atomic_json', disk), self.assertRaises(Unreadable):
            service.start(doc['id'], 'copilot', 'model', expected_version=version, expected_generation=generation)
        (run,) = [r for r in self.store.runs() if r['document_id'] == doc['id'] and r['stage'] == 'extract']
        self.assertEqual((run['status'], run['detail'], self.free_slots(service)), ('error', 'Unreadable', 8))

    def registration_failure(self, refuse):
        doc = self.document()
        from verinote import usage
        real_json = usage.atomic_json
        def write(path, value):
            if path.name == 'context-policy.json': raise OSError('disk')
            return real_json(path, value)
        writes, seen = self.failing_error_writes(refuse)
        service = Service(self.store, self.root, Sequential(ok))
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        with writes, patch('verinote.usage.atomic_json', write), self.assertRaises(OSError) as raised:
            service.start(doc['id'], 'copilot', 'model', expected_version=version, expected_generation=generation)
        (run,) = [r for r in self.store.runs() if r['document_id'] == doc['id'] and r['stage'] == 'extract']
        return run, seen, self.free_slots(service), raised.exception

    def test_registration_failure_after_the_run_row_is_retried_and_returns_slot(self):
        run, seen, free, _ = self.registration_failure(lambda n, detail: n == 1)
        self.assertEqual((run['status'], run['detail'], seen, free), ('error', 'disk', ['disk'] * 2, 8))

    def test_registration_failure_with_every_error_write_raising_reraises_the_original_error(self):
        run, seen, free, error = self.registration_failure(lambda n, detail: True)
        self.assertEqual((run['status'], seen, free, str(error)), ('queued', ['disk', 'disk', Service.FAILURE_FALLBACK], 8, 'disk'))

if __name__ == '__main__':
    unittest.main()
