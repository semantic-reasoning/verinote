"""Synthetic calls only: ordered publication under bounded concurrent completion."""
import copy
import json
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import test_workflow as workflow
from test_quote_repair import broken, repair
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.extraction_checkpoint import CHUNK_TIMEOUT
from verinote.extraction_scheduler import ChunkScheduler
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service


class Caller:
    def __init__(self, fn, capacity=2):
        self.extraction_spec = ExtractionSpec('/fake/claude', 'model', capacity, 10, 1048576, 'null')
        self.fn = fn
        self.calls = []
        self.lock = threading.Lock()
        self.closed = False

    def limited_timeout(self, agent, seconds): return self

    def complete(self, agent, model, prompt, folder):
        is_repair = folder.name == 'quote-repair'
        index = int(folder.parent.name if is_repair else folder.name)
        with self.lock: self.calls.append((index, is_repair))
        return copy.deepcopy(self.fn(index, is_repair))

    def close(self): self.closed = True


class SchedulerIntegration(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, count=3):
        source = SOURCE.ljust(8000)*(count-1)+SOURCE
        doc = self.store.register('parallel', 'parallel.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def start(self, doc, caller):
        service = Service(self.store, self.root, caller, extraction_caller=caller)
        self.addCleanup(service.close)
        run = service.start(doc['id'], 'claude', 'model', expected_version=doc['version'], expected_generation=doc['generation'], extraction_concurrency=caller.extraction_spec.capacity)
        return service, run

    def finish(self, service, run):
        service.pool.shutdown(wait=True)
        return self.store.run(run['id'])

    def saved(self, run):
        path = self.root/'runs'/run['id']/'extraction-checkpoint.json'
        return json.loads(path.read_text())['outputs'] if path.exists() else []

    def test_reverse_completion_window_and_ordered_pending_publication(self):
        second = threading.Event(); release = threading.Event()
        def call(index, _):
            if index == 1:
                self.assertTrue(second.wait(2)); self.assertTrue(release.wait(2))
            if index == 2: second.set()
            value = extraction_response(); value['claims'][0]['heading'] = str(index)
            return value
        caller = Caller(call); doc = self.document()
        service, run = self.start(doc, caller)
        self.assertTrue(second.wait(2))
        time.sleep(.03)
        self.assertCountEqual(caller.calls, [(1, False), (2, False)])
        self.assertEqual(self.saved(run), [])
        release.set(); run = self.finish(service, run)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([v['claims'][0]['heading'] for v in self.saved(run)], ['1', '2', '3'])
        items = self.store.document(doc['id'])['items']
        self.assertTrue(all(i['status']=='pending' for i in items if i['kind']!='ontology'))
        self.assertEqual(self.store.snapshot()['facts'], [])
        from verinote.extraction_timing import read_extraction_timing
        timing = read_extraction_timing(self.root, self.store, run['id'])
        self.assertTrue(timing['available'], timing)
        self.assertFalse(caller.closed)

    def test_later_failure_blocks_repair_and_dispatch_and_preserves_original(self):
        failed = threading.Event()
        def call(index, _):
            if index == 2:
                failed.set(); raise ValueError('second-original')
            self.assertTrue(failed.wait(2)); time.sleep(.03)
            return broken()
        caller = Caller(call); service, run = self.start(self.document(4), caller)
        run = self.finish(service, run)
        self.assertEqual(run['status'], 'error'); self.assertIn('청크 2', run['detail'])
        self.assertIn('second-original', run['detail'])
        self.assertCountEqual(caller.calls, [(1, False), (2, False)])
        self.assertEqual(self.saved(run), [])
        self.assertFalse(caller.closed)

    def test_front_failure_drains_later_result_without_checkpoint(self):
        entered = threading.Event(); drained = threading.Event()
        def call(index, _):
            if index == 1:
                self.assertTrue(entered.wait(2)); raise ValueError('front')
            entered.set(); time.sleep(.03); drained.set(); return extraction_response()
        caller = Caller(call); service, run = self.start(self.document(), caller)
        run = self.finish(service, run)
        self.assertEqual(run['status'], 'error'); self.assertTrue(drained.is_set())
        self.assertEqual(self.saved(run), []); self.assertEqual(len(caller.calls), 2)

    def test_three_chunks_each_get_their_repair_past_the_old_limit_of_two(self):
        caller = Caller(lambda i, r: repair() if r else broken())
        service, run = self.start(self.document(), caller); run = self.finish(service, run)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(sum(r for i, r in caller.calls), 3)
        self.assertEqual(len(self.saved(run)), 3)

    def test_later_failure_during_admitted_repair_saves_the_repaired_earlier_chunk(self):
        repairing = threading.Event(); failed = threading.Event()
        def call(index, is_repair):
            if index == 2:
                self.assertTrue(repairing.wait(2)); failed.set(); raise ValueError('later-original')
            if is_repair:
                repairing.set(); self.assertTrue(failed.wait(2)); time.sleep(.02); return repair()
            return broken()
        caller = Caller(call); service, run = self.start(self.document(), caller)
        run = self.finish(service, run)
        self.assertEqual(run['status'], 'error'); self.assertIn('later-original', run['detail'])
        self.assertEqual(len(self.saved(run)), 1)
        self.assertCountEqual(caller.calls, [(1, False), (2, False), (1, True)])

    @contextmanager
    def jumping_clock(self, dispatch_delay):
        """Yield the offset that the patched time.monotonic adds to the real clock; the fake caller raises it to pass the real
        CHUNK_TIMEOUT. With dispatch_delay, every initial and repair call also waits 50 ms of real time before it is sent: more
        than the 30 ms these tests once used as the whole deadline, so a 30 ms real-time deadline would expire before dispatch."""
        offset, real_clock = [0.0], time.monotonic
        def delayed(method):
            def wrapper(*args, **kwargs):
                time.sleep(.05)
                return method(*args, **kwargs)
            return wrapper
        with ExitStack() as stack:
            stack.enter_context(patch('time.monotonic', lambda: real_clock() + offset[0]))
            if dispatch_delay:
                stack.enter_context(patch.object(ChunkScheduler, '_run', delayed(ChunkScheduler._run)))
                stack.enter_context(patch.object(ChunkScheduler, 'repair', delayed(ChunkScheduler.repair)))
            yield offset

    def test_repair_and_checkpoint_validation_share_initial_deadline(self):
        for dispatch_delay in (False, True):
            with self.subTest(dispatch_delay=dispatch_delay):
                self.tearDown(); self.setUp()
                with self.jumping_clock(dispatch_delay) as offset:
                    def call(index, repairing, offset=offset):
                        # Each call takes 0.6 × CHUNK_TIMEOUT: the repair answers past the deadline set when chunk 1 started
                        # (1.2×), but within one that started at the repair call (0.6× + 1×).
                        offset[0] += .6 * CHUNK_TIMEOUT
                        return repair() if repairing else broken()
                    caller = Caller(call, 1)
                    service, run = self.start(self.document(1), caller); run = self.finish(service, run)
                self.assertEqual(run['status'], 'error'); self.assertEqual(self.saved(run), [])
                self.assertIn(f'인용 보정 실패 (청크당 1회). 청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)', run['detail'])
                self.assertIn('새 호출 시도 2회', run['detail'])
                self.assertEqual(caller.calls, [(1, False), (1, True)])

    def test_first_chunk_response_past_deadline_after_both_calls_sent_is_refused(self):
        for dispatch_delay in (False, True):
            with self.subTest(dispatch_delay=dispatch_delay):
                self.tearDown(); self.setUp()
                second_sent = threading.Event()
                with self.jumping_clock(dispatch_delay) as offset:
                    def call(index, repairing, offset=offset, second_sent=second_sent):
                        if index == 1:
                            self.assertTrue(second_sent.wait(2))  # raise the clock only once chunk 2's call is recorded
                            offset[0] += CHUNK_TIMEOUT + 1
                        if index == 2: second_sent.set()
                        return extraction_response()
                    caller = Caller(call)
                    service, run = self.start(self.document(), caller); run = self.finish(service, run)
                self.assertEqual(run['status'], 'error'); self.assertEqual(self.saved(run), [])
                self.assertTrue(run['detail'].startswith(f'청크 1 · 청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)'), run['detail'])
                self.assertIn('새 호출 시도 2회', run['detail'])
                self.assertCountEqual(caller.calls, [(1, False), (2, False)])

    def test_db_failure_never_marks_checkpoint_complete(self):
        caller = Caller(lambda i, r: extraction_response(), 1); doc = self.document(1)
        with patch.object(self.store, 'finish_run', side_effect=ValueError('db-failed')):
            service, run = self.start(doc, caller); run = self.finish(service, run)
        progress = json.loads((self.root/'runs'/run['id']/'extraction-progress.json').read_text())
        self.assertEqual(run['status'], 'error'); self.assertEqual(progress['status'], 'error')
        self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])

    def test_postcommit_optional_failure_cannot_overwrite_success(self):
        caller = Caller(lambda i, r: extraction_response(), 1)
        with patch('verinote.extraction_checkpoint.ExtractionCheckpoint.finish', side_effect=OSError('optional')):
            service, run = self.start(self.document(1), caller); run = self.finish(service, run)
        self.assertEqual(run['status'], 'success', run['detail'])

    def test_resume_same_contract_and_mismatch_restart(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                doc = self.document(3)
                def first(i, r):
                    if i == 2: raise ValueError('stop')
                    return extraction_response()
                caller = Caller(first, 1)
                service, run = self.start(doc, caller); run = self.finish(service, run)
                self.assertEqual(len(self.saved(run)), 1)
                next_caller = Caller(lambda i, r: extraction_response(), 2 if mismatch else 1)
                service, final = self.start(doc, next_caller); final = self.finish(service, final)
                self.assertEqual(final['status'], 'success', final['detail'])
                self.assertEqual({i for i, r in next_caller.calls}, {1, 2, 3} if mismatch else {2, 3})
                # New source name alone deduplicates, so remove before next subcase.
                with self.store.connect(True) as db: db.execute('DELETE FROM documents WHERE id=?', (doc['id'],))
