"""Concurrent timing is observational; these tests never call a provider."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_workflow import SOURCE, approved_dictionary

from verinote.extraction_timing import (
    ExtractionTiming,
    create_timing,
    read_extraction_timing,
    summarize_record,
    validate_record,
)
from verinote.store import Store


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds * 10**9


class ConcurrentTiming(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.store = Store(self.root/'state.sqlite3')
        text = SOURCE.ljust(8000) + SOURCE
        self.doc = self.store.register('Synthetic', 'synthetic.md', text.encode(), text)
        approved_dictionary(self.store, self.doc['id'])
        self.run = self.store.create_run(self.doc['id'], 'agy', 'test-model', 'extract', 1, 1)
        self.clock = Clock()
        self.timing = ExtractionTiming(self.root, clock=self.clock, schema_version=2)
        self.timing.bind(self.run); self.timing.worker_start()
        self.timing.configure(self.store.document(self.doc['id'], 1))
        with self.timing.section('restore'): self.clock.advance(1)

    def finish(self, status='success'):
        if status == 'success':
            with self.timing.section('publish'): self.clock.advance(1)
        self.timing.outcome(status, None if status == 'success' else 'timeout')
        self.store.run_status(self.run['id'], status)
        self.timing.finish()
        return read_extraction_timing(self.root, self.store, self.run['id'])

    def overlapping(self):
        t = self.timing
        t.begin_chunk(1)
        first = t.call(1); first.__enter__()
        with t.section('worker_wait', 1): self.clock.advance(1)
        t.begin_chunk(2)
        with t.call(2): self.clock.advance(3)
        with t.section('order_wait', 2): self.clock.advance(1)
        with t.section('validation_with_bookkeeping', 2): self.clock.advance(1)
        t.end_chunk(index=2)
        self.clock.advance(2); first.__exit__(None, None, None)
        with t.section('validation_with_bookkeeping', 1): self.clock.advance(1)
        t.end_chunk(index=1)

    def test_reverse_finish_work_union_and_wait_inclusion(self):
        self.overlapping(); report = self.finish()
        self.assertTrue(report['available']); s = report['summary']
        self.assertEqual((s['total_seconds'], s['worker_seconds']), (11, 11))
        self.assertEqual((s['chunk_work_seconds'], s['chunk_wall_seconds']), (14, 9))
        self.assertEqual((s['request_work_seconds'], s['request_wall_seconds']), (11, 8))
        self.assertEqual(s['worker_other_seconds'], 0)
        first, second = s['chunks']
        self.assertEqual((first['request_seconds'], first['worker_wait_seconds']), (8, 1))
        self.assertEqual((second['order_wait_seconds'], second['other_seconds']), (1, 0))
        self.assertEqual(first['other_seconds'], 0)
        raw = copy.deepcopy(self.timing.value); raw['schema_version'] = 1
        with self.assertRaises(ValueError): validate_record(raw)

    def test_front_timeout_after_later_success_and_repair_wait(self):
        t = self.timing; t.begin_chunk(1); t.begin_chunk(2)
        with t.call(2): self.clock.advance(1)
        with t.section('validation_with_bookkeeping', 2):
            with t.section('repair', 2):
                with t.call(2, repair=True):
                    with t.section('repair_worker_wait', 2): self.clock.advance(1)
                    self.clock.advance(2)
                with t.section('repair_validation_with_bookkeeping', 2): self.clock.advance(1)
        t.end_chunk(index=2)
        with self.assertRaises(TimeoutError):
            with t.call(1):
                self.clock.advance(2)
                raise TimeoutError('original')
        t.end_chunk('timeout', index=1)
        report = self.finish('error'); self.assertTrue(report['available'])
        second = report['summary']['chunks'][1]
        self.assertEqual((second['repair_request_seconds'], second['repair_worker_wait_seconds']), (3, 1))
        self.assertEqual(report['summary']['new_call_attempts'], 3)
        self.assertEqual(report['spans'][-1]['status'], 'timeout')

    def test_partial_and_full_resume_exclude_previous_work(self):
        for count in (1, 2):
            with self.subTest(count=count):
                t = ExtractionTiming(self.root, clock=self.clock, schema_version=2)
                t.bind(self.run); t.worker_start(); t.configure(self.store.document(self.doc['id'], 1))
                with t.section('restore'): self.clock.advance(2)
                t.reuse(count, 'a'*32)
                if count == 1:
                    t.begin_chunk(2)
                    with t.call(2): self.clock.advance(3)
                    with t.section('validation_with_bookkeeping', 2): pass
                    t.end_chunk(index=2)
                self.timing = t; report = self.finish()
                self.assertTrue(report['available'])
                self.assertIsNone(report['summary']['chunks'][0]['total_seconds'])
                self.assertEqual(report['summary']['new_call_attempts'], 2-count)
                self.assertEqual(report['summary']['request_work_seconds'], 3 if count == 1 else None)

    def test_barrier_proves_context_bodies_do_not_hold_recorder_lock(self):
        t = self.timing; t.begin_chunk(1); t.begin_chunk(2)
        entered = threading.Barrier(2); second_finished = threading.Event()
        def execute(index):
            with t.call(index): entered.wait(timeout=3)
            if index == 1: self.assertTrue(second_finished.wait(3))
            with t.section('validation_with_bookkeeping', index): pass
            t.end_chunk(index=index)
            if index == 2: second_finished.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, i) for i in (1, 2)]
            for future in futures: future.result(timeout=5)
        self.assertTrue(self.finish()['available'])

    def test_corrupt_v2_records_are_rejected(self):
        self.overlapping(); self.finish(); original = self.timing.value
        def span(record, kind, chunk): return next(s for s in record['spans'] if s['kind'] == kind and s['chunk'] == chunk)
        mutations = [
            lambda r: span(r, 'worker_wait', 1).update(end=10),
            lambda r: span(r, 'order_wait', 2).update(start=1),
            lambda r: span(r, 'restore', None).update(end=3),
            lambda r: span(r, 'publish', None).update(start=8),
            lambda r: r['spans'].append(copy.deepcopy(r['spans'][-1])),
            lambda r: r['spans'].extend([copy.deepcopy(r['spans'][-1])]*129),
            lambda r: r['chunks'][1].update(reused=True),
            lambda r: span(r, 'request', 1).update(cli_call_id='bad'),
            lambda r: [span(r, 'request', i).update(cli_call_id='a'*32) for i in (1, 2)],
        ]
        for mutate in mutations:
            record = copy.deepcopy(original); mutate(record)
            with self.assertRaises(ValueError): validate_record(record)
        path = self.root/'runs'/self.run['id']/'extraction-timing.json'
        saved = json.loads(path.read_text()); saved['identity']['source_sha256'] = 'a'*64
        path.write_text(json.dumps(saved)); self.assertFalse(read_extraction_timing(self.root, self.store, self.run['id'])['available'])

    def test_ambiguous_close_unfinished_clock_and_write_failure_are_observational(self):
        self.timing.begin_chunk(1); self.timing.begin_chunk(2); self.timing.end_chunk()
        self.assertFalse(self.finish('error')['available'])
        for fault in ('open', 'clock', 'write'):
            t = ExtractionTiming(self.root, clock=self.clock, schema_version=2)
            t.bind(self.run); t.worker_start(); t.configure(self.store.document(self.doc['id'], 1))
            with t.section('restore'): pass
            t.begin_chunk(1)
            if fault == 'clock': t.clock = lambda: (_ for _ in ()).throw(ValueError('clock'))
            with self.assertRaisesRegex(TimeoutError, 'original'):
                with t.call(1): raise TimeoutError('original')
            if fault != 'open': t.end_chunk('timeout', index=1)
            t.outcome('error', 'timeout')
            if fault == 'write':
                with patch('verinote.http_records.write_record', side_effect=OSError('write')): t.finish()
            else: t.finish()
            self.assertTrue(t.finished)
            self.assertFalse((self.root/'runs'/self.run['id']/'extraction-timing.json').exists())
        no = create_timing(self.root, 'openrouter', 'extract', schema_version=2)
        no.end_chunk(index=2); no.finish()
        self.assertEqual(create_timing(self.root, 'agy', 'extract').value['schema_version'], 1)
        self.assertEqual(create_timing(self.root, 'agy', 'extract', schema_version=2).value['schema_version'], 2)

    def test_zero_and_absent_union_stay_distinct(self):
        self.timing.reuse(2, 'a'*32); report = self.finish()
        self.assertTrue(report['available'])
        self.assertIsNone(report['summary']['chunk_wall_seconds'])
        self.assertEqual(summarize_record(self.timing.value)['worker_other_seconds'], 0)
