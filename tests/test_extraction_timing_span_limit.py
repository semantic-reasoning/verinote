"""Span counts of the longest real extraction paths against the span limits."""
from collections import Counter
from unittest.mock import patch

from test_failed_run_detail import Base
from test_quote_exclusions import CLAIM1, CLAIM2, bad, reply, resp
from test_quote_repair import broken, repair
from test_workflow import FACTS

from verinote.extraction_timing import (
    MAX_SPANS,
    MAX_SPANS_V2,
    ExtractionTiming,
    read_extraction_timing,
)


def all_repaired(index, repairing): return repair() if repairing else broken()
def row_excluded(index, repairing): return reply(('claims.0.quote', '')) if repairing else resp([bad(CLAIM1), CLAIM2], FACTS[:1])
def all_claims_excluded(index, repairing): return reply(('claims.0.quote', '')) if repairing else resp([bad(CLAIM1)], FACTS[:1])
def last_reply_malformed(index, repairing): return {'nope': 1} if repairing and index == 16 else all_repaired(index, repairing)
def last_repair_call_fails(index, repairing):
    if repairing and index == 16: raise RuntimeError('repair-down')
    return all_repaired(index, repairing)


PER_CHUNK = ('chunk', 'request', 'validation_with_bookkeeping', 'repair', 'repair_request', 'repair_validation_with_bookkeeping')


def counts(publish=1, last_validation=16, order=0):
    expected = Counter({kind: 16 for kind in PER_CHUNK}) + Counter(restore=1)
    if publish: expected['publish'] = 1
    if last_validation != 16: expected['repair_validation_with_bookkeeping'] = last_validation
    if order: expected['order_wait'] = order
    return expected


PATHS = ((all_repaired, 'success', {}), (row_excluded, 'success', {}),
         (all_claims_excluded, 'error', {'publish': 0}), (last_reply_malformed, 'error', {'publish': 0}),
         (last_repair_call_fails, 'error', {'publish': 0, 'last_validation': 15}))


class SpanLimit(Base):
    def spans(self, run):
        report = read_extraction_timing(self.root, self.store, run['id'])
        self.assertTrue(report['available'], run['detail'])
        return report, Counter(span['kind'] for span in report['spans'])

    def test_format_one_span_counts_on_16_chunk_paths_through_the_sequential_service(self):
        for fn, status, shape in PATHS:
            with self.subTest(fn.__name__):
                run, _ = self.sequential(self.document(16), fn)
                self.assertEqual(run['status'], status, run['detail'])
                report, kinds = self.spans(run)
                self.assertEqual(report['schema_version'], 1)
                self.assertEqual(kinds, counts(**shape))
                if fn is all_repaired: self.assertEqual(sum(kinds.values()), MAX_SPANS)

    def test_format_two_span_counts_on_16_chunk_paths_through_the_real_scheduler(self):
        for capacity in (1, 2):
            for fn, status, shape in PATHS:
                with self.subTest(fn.__name__, capacity=capacity):
                    run, _ = self.parallel(self.document(16), fn, capacity=capacity)
                    self.assertEqual(run['status'], status, run['detail'])
                    report, kinds = self.spans(run)
                    self.assertEqual(report['schema_version'], 2)
                    self.assertEqual(kinds, counts(order=16, **shape))
                    self.assertLessEqual(sum(kinds.values()), MAX_SPANS_V2)
                    if fn is all_repaired: self.assertEqual(sum(kinds.values()), 114)
                    waits = {chunk[name] for chunk in report['summary']['chunks'] for name in ('worker_wait_seconds', 'repair_worker_wait_seconds')}
                    self.assertEqual(waits, {None})

    def test_a_span_past_the_format_one_limit_disables_timing_and_the_run_still_succeeds(self):
        seen, outcome = [], ExtractionTiming.outcome

        def one_more(timing, status, code=None):
            with timing.section('publish'): pass
            seen.append(timing)
            outcome(timing, status, code)
        with patch.object(ExtractionTiming, 'outcome', one_more):
            run, _ = self.sequential(self.document(16), all_repaired)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertTrue(seen[0].disabled)
        self.assertEqual(len(seen[0].value['spans']), MAX_SPANS)
        self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])
