"""Deterministic admission races and accounting; no real model calls."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from verinote.extraction_benchmark import (
    Budget,
    BudgetCaller,
    FakeCLI,
    Limits,
    ReportWriter,
    Stopped,
    call_metrics,
)
from verinote.extraction_evaluation import load_case
from verinote.usage import TOKEN_KEYS, atomic_json


def usage(tokens=120):
    return {'tokens': {k: (tokens if k == 'input' else 0) for k in TOKEN_KEYS}, 'usage_complete': True,
            'reported_usd': .01, 'cli_timing': None}


class ConcurrentBudget(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_capacity_race_does_not_revoke_reserved_slots(self):
        budget = Budget(Limits(requests=1)); barrier = threading.Barrier(4)
        def reserve(index):
            barrier.wait(timeout=3)
            try: return budget.reserve(index, False)
            except Stopped: return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(reserve, range(1, 5)))
        row, = [r for r in rows if r is not None]
        self.assertEqual(budget.reason, 'request_limit')
        budget.authorize_dispatch(row); budget.finish(row, usage())
        self.assertEqual(budget.summary()['dispatch_authorized'], 1)
        self.assertEqual(budget.observed_tokens, 120)
        self.assertEqual(budget.summary()['pending'], 0)

    def test_capacity_marker_cannot_hide_later_terminal_stop(self):
        budget = Budget(Limits(requests=2))
        rows = [budget.reserve(i, False) for i in (1, 2)]
        with self.assertRaises(Stopped): budget.reserve(3, False)
        barrier = threading.Barrier(2); stopped = threading.Event()
        def stop():
            barrier.wait(timeout=3); budget.stop('report_write_failed'); stopped.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stop)
            budget.authorize_dispatch(rows[0]); barrier.wait(timeout=3)
            self.assertTrue(stopped.wait(3))
            with self.assertRaisesRegex(Stopped, 'report_write_failed'): budget.authorize_dispatch(rows[1])
            budget.block(rows[1]); budget.finish(rows[0], usage())
            future.result(timeout=3)
        self.assertEqual(budget.summary()['blocked_before_dispatch'], 1)
        self.assertEqual(budget.summary()['unknown_usage_calls'], 0)
        self.assertTrue(budget.summary()['token_usage_verified'])

    def test_stop_after_reservation_prevents_adapter_entry(self):
        budget = Budget(Limits()); entries = []
        class Caller:
            def limited_timeout(self, *args): budget.stop('interrupted'); return self
            def complete(self, *args): entries.append(1)
        with self.assertRaisesRegex(Stopped, 'interrupted'):
            BudgetCaller(Caller(), budget).complete('agy', '', 'p', self.root/'1')
        self.assertEqual(entries, [])
        self.assertEqual(budget.attempts[0]['admission_state'], 'blocked')
        self.assertEqual(budget.summary()['dispatch_authorized'], 0)
        self.assertEqual(budget.unknown_usage, 0)
        self.assertIsNone(budget.attempts[0]['tokens']['input'])

    def test_timeout_clone_expiry_is_checked_again_before_authorization(self):
        now = [0]; budget = Budget(Limits(seconds=1), clock=lambda: now[0]); entries = []
        class Caller:
            def limited_timeout(self, *args): now[0] = 2; return self
            def complete(self, *args): entries.append(1)
        with self.assertRaisesRegex(Stopped, 'time_limit'):
            BudgetCaller(Caller(), budget).complete('agy', '', 'p', self.root/'1')
        self.assertEqual(entries, []); self.assertEqual(budget.reason, 'time_limit')

    def test_reverse_inflight_finish_keeps_usage_after_token_stop(self):
        budget = Budget(Limits(tokens=1)); both = threading.Barrier(2); first_done = threading.Event()
        rows = [budget.reserve(i, i == 2) for i in (1, 2)]
        def execute(index):
            row = rows[index]; budget.authorize_dispatch(row); both.wait(timeout=3)
            if index == 0: self.assertTrue(first_done.wait(3))
            budget.finish(row, usage(100+index))
            if index == 1: first_done.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, i) for i in range(2)]
            for f in futures: f.result(timeout=5)
        self.assertEqual(budget.observed_tokens, 201)
        self.assertEqual(budget.reason, 'token_limit')
        with self.assertRaises(Stopped): budget.reserve(3, False)
        with self.assertRaises(ValueError): budget.finish(rows[1], usage())
        with self.assertRaises(ValueError): budget.finish(copy.deepcopy(rows[0]), usage())
        self.assertEqual(budget.observed_tokens, 201)
        self.assertEqual(budget.summary()['finished'], 2)

    def test_unknown_usage_stops_new_admission_but_collects_other_call(self):
        budget = Budget(Limits()); a = budget.reserve(1, False); b = budget.reserve(2, False)
        budget.authorize_dispatch(a); budget.authorize_dispatch(b)
        unknown = usage(); unknown['tokens']['output'] = None; unknown['usage_complete'] = False
        budget.finish(b, unknown, status='error')
        with self.assertRaisesRegex(Stopped, 'usage_unknown'): budget.reserve(3, False)
        budget.finish(a, usage())
        self.assertEqual((budget.observed_tokens, budget.unknown_usage), (240, 1))
        self.assertFalse(budget.summary()['token_usage_verified'])
        self.assertEqual(budget.summary()['finished'], 2)

    def test_metadata_copy_states_and_warmup_share_the_ledger(self):
        budget = Budget(Limits()); identity = {'owner_id': 1, 'case_id': 'short'}
        a = budget.reserve(1, False, identity=identity, purpose='warmup'); identity['owner_id'] = 99
        self.assertEqual(a['owner_id'], 1); self.assertEqual(a['request_kind'], 'warmup')
        with self.assertRaises(ValueError): budget.reserve(1, False, identity={'attempt': 90})
        with self.assertRaises(ValueError): budget.reserve(1, False, identity={'owner_id': []})
        with self.assertRaises(ValueError): budget.finish(a, usage())
        budget.authorize_dispatch(a)
        with self.assertRaises(ValueError): budget.authorize_dispatch(a)
        with self.assertRaises(ValueError): budget.finish(a, usage() | {'status': 'success'})
        invalid = usage(); invalid['tokens']['input'] = -1
        with self.assertRaises(ValueError): budget.finish(a, invalid)
        self.assertEqual(budget.observed_tokens, 0)
        data = usage(); budget.finish(a, data); data['tokens']['input'] = 900
        self.assertEqual(a['tokens']['input'], 120)
        wrapped = BudgetCaller(FakeCLI(load_case('short'), 'normal'), budget, purpose='warmup')
        wrapped.limited_timeout('agy', 1).complete('agy', '', 'p', self.root/'1')
        self.assertEqual(budget.attempts[-1]['purpose'], 'warmup')
        self.assertEqual(budget.summary()['requests_started'], 2)
        self.assertEqual(budget.summary()['dispatch_authorized'], 2)

    def concurrent_callers(self, budget, notify=None):
        barrier = threading.Barrier(2)
        class Caller(FakeCLI):
            def complete(self, *args, **kwargs):
                barrier.wait(timeout=3)
                return super().complete(*args, **kwargs)
        # Separate fake state models independent transports; the Budget is shared.
        return [BudgetCaller(Caller(load_case('short'), 'normal'), budget, notify).limited_timeout('agy', 3)
                for _ in range(2)]

    def test_calls_overlap_and_notify_serializes_consistent_snapshots(self):
        budget = Budget(Limits()); writer = ReportWriter(self.root/'out'); snapshots = []
        def notify():
            report = {'budget': budget.summary(), 'attempts': copy.deepcopy(budget.attempts)}
            writer.write(report); snapshots.append(report)
        callers = self.concurrent_callers(budget, notify)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fs = [pool.submit(c.complete, 'agy', '', 'p', self.root/str(i)/'1') for i, c in enumerate(callers, 1)]
            for f in fs: f.result(timeout=5)
        final = json.loads(writer.path.read_text())
        self.assertEqual(final['budget']['finished'], 2)
        self.assertEqual(final['budget']['observed_tokens_lower_bound'], 240)
        for snap in snapshots:
            finished = [r for r in snap['attempts'] if r['admission_state'] == 'finished']
            self.assertEqual(snap['budget']['observed_tokens_lower_bound'], sum(r['tokens']['input']+r['tokens']['output'] for r in finished))
        self.assertEqual(final, snapshots[-1])

    def test_notify_write_failure_preserves_inflight_usage_and_last_file(self):
        budget = Budget(Limits()); writer = ReportWriter(self.root/'out'); writer.write({'sentinel': True})
        def notify(): writer.write({'attempts': copy.deepcopy(budget.attempts)})
        callers = self.concurrent_callers(budget, notify)
        def fail_report(path, value):
            if path == writer.path: raise OSError('PRIVATE_WRITE')
            atomic_json(path, value)
        with patch('verinote.extraction_benchmark.atomic_json', side_effect=fail_report):
            with ThreadPoolExecutor(max_workers=2) as pool:
                fs = [pool.submit(c.complete, 'agy', '', 'p', self.root/str(i)/'1') for i, c in enumerate(callers, 1)]
                for f in fs:
                    with self.assertRaises(Stopped): f.result(timeout=5)
        self.assertEqual(budget.observed_tokens, 240)
        self.assertEqual(budget.summary()['finished'], 2)
        self.assertEqual(budget.reason, 'report_write_failed')
        self.assertEqual(json.loads(writer.path.read_text()), {'sentinel': True})
        with self.assertRaises(Stopped): budget.reserve(3, False)

    def test_callback_failure_does_not_replace_original_caller_exception(self):
        budget = Budget(Limits())
        class Caller:
            def limited_timeout(self, *args): return self
            def complete(self, *args): raise TimeoutError('original timeout')
        def notify(): raise OSError('PRIVATE_NOTIFY')
        with self.assertRaisesRegex(TimeoutError, 'original timeout'):
            BudgetCaller(Caller(), budget, notify).complete('agy', '', 'p', self.root/'1')
        self.assertEqual(budget.summary()['finished'], 1)
        self.assertEqual(budget.unknown_usage, 1)
        self.assertEqual(budget.reason, 'usage_unknown')
        self.assertFalse(call_metrics(self.root/'1', 'agy', '')['usage_complete'])
