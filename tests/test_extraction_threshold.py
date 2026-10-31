"""Frozen full-document chunk policy, with only synthetic callers."""
import copy
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_workflow as workflow
from test_extraction_scheduler import Caller
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI
from verinote.extraction_options import (
    CHUNK_THRESHOLD_POLICY,
    extraction_options,
    policy_request,
)
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service
from verinote.store import Conflict


class Threshold(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, count=4, length=None):
        source = SOURCE.ljust(8000)*(count-1)+SOURCE if length is None else SOURCE.ljust(length)
        doc = self.store.register('threshold', 'threshold.txt', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def request(self):
        return policy_request(ExtractionSpec('/fake/claude', 'model', 2, 7, 1048576, 'null'), CHUNK_THRESHOLD_POLICY)

    def execute(self, doc, fn=None, *, explicit=None):
        created = []
        def factory(spec):
            # Owner creation must happen after the transactional decision exists.
            with self.store.connect() as db:
                raw = db.execute("SELECT extraction_options FROM runs WHERE document_id=? AND stage='extract' AND status='queued'", (doc['id'],)).fetchone()
            self.assertIsNotNone(raw)
            self.assertEqual(json.loads(raw[0])['transport'], spec.to_dict())
            caller = Caller(fn or (lambda i, r: extraction_response()), spec.capacity)
            caller.extraction_spec = spec
            created.append(caller)
            return caller
        service = Service(self.store, self.root, CLI(timeout=7, persistent=False), extraction_caller_factory=factory)
        self.addCleanup(service.close)
        with patch('verinote.extraction_transport.executable', return_value=sys.executable):
            run = service.start(doc['id'], 'claude', 'model', expected_version=doc['version'], expected_generation=doc['generation'],
                **({'extraction_concurrency': explicit} if explicit else {'extraction_policy': CHUNK_THRESHOLD_POLICY}))
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), created

    def test_count_is_transactional_and_exact_boundary(self):
        for length in (8000, 8001, 16000, 16001, 24000, 24001, 56001, 64000):
            doc = self.document(length=length)
            request = self.request(); before = copy.deepcopy(request)
            run = self.store.create_run(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_options=request)
            options = run['extraction_options']; count = (length+7999)//8000
            self.assertEqual(options['original_chunk_count'], count)
            self.assertEqual(options['concurrency'], 1 if count <= 3 else 2)
            self.assertEqual(options['transport']['capacity'], options['concurrency'])
            self.assertEqual(request, before)
            self.store.run_status(run['id'], 'error')

    def test_cold_policy_maximum_and_no_preparation_calls(self):
        for count in (1, 2, 3, 4, 8):
            doc = self.document(count)
            lock = threading.Lock(); active = 0; peak = 0
            barrier = threading.Barrier(2) if count >= 4 else None
            def call(index, repair, lock=lock, barrier=barrier):
                nonlocal active, peak
                self.assertFalse(repair)
                with lock: active += 1; peak = max(peak, active)
                try:
                    if barrier is not None and index <= 2: barrier.wait(2)
                    time.sleep(.01)
                    return extraction_response()
                finally:
                    with lock: active -= 1
            run, created = self.execute(doc, call)
            self.assertEqual(run['status'], 'success', run['detail'])
            self.assertEqual(len(created), 1)
            self.assertEqual(peak, 1 if count <= 3 else 2)
            self.assertEqual(sorted(created[0].calls), [(i, False) for i in range(1, count+1)])

    def test_policy_resume_uses_original_four_not_remaining_three(self):
        doc = self.document()
        def fail(index, repair):
            if index == 2:
                time.sleep(.03)
                raise TimeoutError('stop')
            return extraction_response()
        first, _ = self.execute(doc, fail)
        self.assertEqual(first['status'], 'error')
        saved = json.loads((self.root/'runs'/first['id']/'extraction-checkpoint.json').read_text())
        self.assertEqual(len(saved['outputs']), 1)
        final, callers = self.execute(doc)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(final['extraction_options']['original_chunk_count'], 4)
        self.assertEqual(callers[0].extraction_spec.capacity, 2)
        self.assertEqual(sorted(callers[0].calls), [(2, False), (3, False), (4, False)])

    def test_explicit_two_prefix_not_reused_by_policy(self):
        doc = self.document()
        def fail(index, repair):
            if index == 2:
                time.sleep(.03)
                raise TimeoutError('stop')
            return extraction_response()
        self.execute(doc, fail, explicit=2)
        final, callers = self.execute(doc)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(sorted(callers[0].calls), [(i, False) for i in range(1, 5)])

    def test_bad_requests_and_stale_generation_leave_no_registered_run(self):
        doc = self.document()
        request = self.request()
        for key, value in (('original_chunk_count', 3), ('concurrency', 2), ('document_generation', 1), ('policy', 'other'), ('schema_version', True)):
            with self.assertRaises(ValueError):
                self.store.create_run(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_options=request|{key: value})
        for version, generation in ((2, 1), (1, 2)):
            with self.assertRaises(Conflict):
                self.store.create_run(doc['id'], 'claude', 'model', expected_version=version, expected_generation=generation, extraction_options=request)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE stage='extract'").fetchone()[0], 0)

    def test_count_corruption_fails_before_owner_and_settings_are_frozen(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_options=self.request())
        options = run['extraction_options']
        for update in ({'original_chunk_count': True}, {'original_chunk_count': 3}, {'concurrency': 1}, {'policy': 'other'}):
            with self.assertRaises(ValueError): extraction_options(options|update)
        run['extraction_options']['original_chunk_count'] = 8  # Internally valid decision, wrong source.
        called = []
        service = Service(self.store, self.root, CLI(), extraction_caller_factory=lambda spec: called.append(spec))
        self.addCleanup(service.close)
        service.slots.acquire(); service.extract(run)
        self.assertEqual(called, [])
        self.assertEqual(self.store.run(run['id'])['status'], 'error')

    def test_queued_cli_settings_do_not_change_registered_spec(self):
        doc = self.document(3); captured = []
        def factory(spec):
            captured.append(spec)
            caller = Caller(lambda i, r: extraction_response(), spec.capacity); caller.extraction_spec = spec
            return caller
        cli = CLI(timeout=7, persistent=False)
        service = Service(self.store, self.root, cli, extraction_caller_factory=factory)
        self.addCleanup(service.close)
        pending = []
        with patch.object(service.pool, 'submit', side_effect=lambda fn, *args: pending.append((fn, args))), patch('verinote.extraction_transport.executable', return_value=sys.executable):
            run = service.start(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_policy=CHUNK_THRESHOLD_POLICY)
        cli.timeout = 99; cli.max_output = 17
        self.assertEqual(captured, [])
        fn, args = pending.pop(); fn(*args)
        self.assertEqual(captured[0].timeout, 7)
        self.assertEqual(captured[0].max_output, 1048576)
        self.assertEqual(captured[0].capacity, 1)
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_exclusive_and_unsupported_policy_options(self):
        doc = self.document()
        service = Service(self.store, self.root, CLI()); self.addCleanup(service.close)
        for kwargs in ({'extraction_concurrency': 1}, {'stage': 'ontology'}, {'agent': 'agy'}, {'extraction_policy': 'bad'}):
            with patch('verinote.extraction_transport.executable', return_value=sys.executable), self.assertRaises(ValueError):
                service.start(doc['id'], **({'agent': 'claude', 'model': 'model', 'extraction_policy': CHUNK_THRESHOLD_POLICY}|kwargs))
