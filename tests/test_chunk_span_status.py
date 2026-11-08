"""Synthetic calls only: the chunk span of the chunk that failed a run records its cause's error kind."""
import json
import threading
import unittest
from unittest.mock import patch

from test_failed_run_detail import Base
from test_quote_repair import broken
from test_workflow import extraction_response

from verinote.extraction_scheduler import ChunkScheduler
from verinote.extraction_timing import read_extraction_timing


class ChunkSpanStatus(Base):
    def drive(self, capacity, repair, error=TimeoutError):
        """Chunk 2's initial (or repair) call raises; with two workers, chunk 1 answers only after the failure is recorded."""
        recorded = threading.Event()
        real_fail = ChunkScheduler.fail
        def fail(scheduler, index, exc):
            try: return real_fail(scheduler, index, exc)
            finally: recorded.set()
        def respond(index, repairing):
            if index == 2 and repairing == repair: raise error('chunk 2')
            if index == 2: return broken()
            if index == 1 and capacity == 2 and not repair: self.assertTrue(recorded.wait(5))
            return extraction_response()
        doc = self.document()
        with patch.object(ChunkScheduler, 'fail', fail):
            run, calls = self.sequential(doc, respond) if capacity is None else self.parallel(doc, respond, capacity)
        self.assertEqual(run['status'], 'error')
        self.assertTrue(read_extraction_timing(self.root, self.store, run['id'])['available'])
        value = json.loads((self.root / 'runs' / run['id'] / 'extraction-timing.json').read_text())
        spans = {(s['kind'], s['chunk']): s['status'] for s in value['spans']}
        return value['error_code'], {chunk: status for (kind, chunk), status in spans.items() if kind == 'chunk'}, spans

    def test_timed_out_chunk_records_timeout_and_co_terminated_chunks_record_error(self):
        cases = [  # (capacity, repair) -> chunk spans
            ((None, False), {1: 'success', 2: 'timeout'}),
            ((None, True), {1: 'success', 2: 'timeout'}),
            ((1, False), {1: 'success', 2: 'timeout'}),
            ((1, True), {1: 'success', 2: 'timeout'}),
            ((2, False), {1: 'error', 2: 'timeout'}),
            ((2, True), {1: 'success', 2: 'timeout', 3: 'error'}),
        ]
        for (capacity, repair), expected in cases:
            with self.subTest(capacity=capacity, repair=repair):
                self.tearDown(); self.setUp()
                code, chunk_spans, spans = self.drive(capacity, repair)
                self.assertEqual(code, 'timeout')
                self.assertEqual(chunk_spans, expected)
                self.assertEqual(spans[('repair_request' if repair else 'request', 2)], 'timeout')

    def test_other_errors_record_error_on_every_path(self):
        for capacity in (None, 1, 2):
            for repair in (False, True):
                with self.subTest(capacity=capacity, repair=repair):
                    self.tearDown(); self.setUp()
                    code, chunk_spans, _ = self.drive(capacity, repair, RuntimeError)
                    self.assertEqual(code, 'error')
                    self.assertEqual(chunk_spans[2], 'error')


if __name__ == '__main__':
    unittest.main()
