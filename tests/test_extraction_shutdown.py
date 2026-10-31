"""Graceful queue drain uses frozen registered owners; no subprocess/model calls."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_workflow as workflow
from test_extraction_scheduler import Caller
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI
from verinote.service import Service


class Owner(Caller):
    @property
    def identity(self): return self.extraction_spec
    def view(self): return self
    def retire_if_idle(self): self.close()


class Shutdown(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, suffix):
        source = SOURCE+'\n'+suffix
        doc = self.store.register(suffix, suffix+'.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def start(self, service, doc):
        return service.start(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_concurrency=2)

    def test_registered_active_and_queued_jobs_drain_before_concurrent_close_cleanup(self):
        for initially_active in (False, True):
            with self.subTest(initially_active=initially_active):
                entered = threading.Event(); release = threading.Event(); closing = threading.Event()
                def response(i, r, entered=entered, release=release):
                    entered.set()
                    if not release.wait(3): raise TimeoutError('test barrier')
                    return extraction_response()
                owner = Owner(response)
                service = Service(self.store, self.root, CLI()); self.addCleanup(service.close)
                first = self.document(str(initially_active)+'-first')
                second = self.document(str(initially_active)+'-second')
                shutdown = service.pool.shutdown
                def draining(*args, closing=closing, shutdown=shutdown, **kwargs):
                    closing.set(); return shutdown(*args, **kwargs)
                with patch('verinote.extraction_transport.ExtractionSpec.capture', return_value=owner.identity), patch('verinote.extraction_transport.ExtractionTransport', return_value=owner), patch.object(service.pool, 'shutdown', side_effect=draining), ThreadPoolExecutor(max_workers=2) as closers:
                    if not initially_active:
                        service.pool.submit(lambda entered=entered, release=release: (entered.set(), release.wait(3)))
                    one = self.start(service, first)
                    self.assertTrue(entered.wait(2))
                    two = self.start(service, second)
                    ending = closers.submit(service.close)
                    self.assertTrue(closing.wait(2))
                    concurrent = closers.submit(service.close)
                    count = len(self.store.runs()); slots = service.slots._value
                    with self.assertRaisesRegex(ValueError, '닫혔'): self.start(service, second)
                    with self.assertRaisesRegex(ValueError, '닫혔'): service.extraction_transport('model')
                    self.assertEqual(len(self.store.runs()), count); self.assertEqual(service.slots._value, slots)
                    self.assertFalse(owner.closed)
                    release.set(); ending.result(3); concurrent.result(3)
                for run in (one, two):
                    saved = self.store.run(run['id']); self.assertEqual(saved['status'], 'success', saved['detail'])
                self.assertTrue(owner.closed); self.assertFalse(service.draining)
                self.assertEqual(service.registered_extractions, {}); self.assertEqual(service.slots._value, 8)
                with self.assertRaises(ValueError): service._registered_extraction_transport(one, owner.identity)
                service.close()

    def test_submit_failure_releases_slot_and_marks_registered_row_error(self):
        service = Service(self.store, self.root, CLI()); self.addCleanup(service.close)
        owner = Owner(lambda i, r: extraction_response()); doc = self.document('submit')
        with patch('verinote.extraction_transport.ExtractionSpec.capture', return_value=owner.identity), patch.object(service.pool, 'submit', side_effect=RuntimeError('submit failed')):
            with self.assertRaisesRegex(RuntimeError, 'submit failed'): self.start(service, doc)
        runs = [r for r in self.store.runs() if r['stage']=='extract']
        self.assertEqual(len(runs), 1); self.assertEqual(runs[0]['status'], 'error')
        self.assertEqual(service.slots._value, 8); self.assertEqual(service.registered_extractions, {})

    def test_interrupted_drain_retains_owner_until_retry(self):
        service = Service(self.store, self.root, CLI()); self.addCleanup(service.close)
        owner = Owner(lambda i, r: extraction_response()); service.extraction_owner = owner
        with patch.object(service.pool, 'shutdown', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError): service.close()
        self.assertTrue(service.closed); self.assertTrue(service.draining); self.assertFalse(owner.closed)
        service.close(); self.assertFalse(service.draining); self.assertTrue(owner.closed)
