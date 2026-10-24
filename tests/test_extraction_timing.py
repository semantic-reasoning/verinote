"""Unpaid orchestration timing without changing extraction or resume policy."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_quote_repair import broken, repair
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI
from verinote.extraction_timing import (
    read_extraction_timing,
    summarize_record,
    validate_record,
)
from verinote.service import Service
from verinote.usage import job_calls


class Clock:
    def __init__(self): self.ns = 0
    def __call__(self): return self.ns
    def advance(self, seconds): self.ns += int(seconds * 1e9)


class Queue:
    def __init__(self): self.jobs = []
    def submit(self, fn, *args): self.jobs.append((fn, args))
    def run(self):
        fn, args = self.jobs.pop(0); fn(*args)


class Fake:
    def __init__(self, outputs, clock):
        self.outputs, self.clock, self.calls = iter(outputs), clock, []
    def limited_timeout(self, agent, seconds): return self
    def complete(self, agent, model, prompt, folder):
        self.calls.append(folder)
        self.clock.advance(3 if Path(folder).name == 'quote-repair' else 2)
        output = next(self.outputs)
        if isinstance(output, Exception): raise output
        return copy.deepcopy(output)


class Timing(unittest.TestCase):
    def setUp(self):
        workflow.Workflow.setUp(self); self.root = self.root.resolve()
    tearDown = workflow.Workflow.tearDown

    def document(self, count=1):
        text = SOURCE.ljust(8000) * (count-1) + SOURCE
        doc = self.store.register('PRIVATE_TITLE', 'PRIVATE_FILENAME.md', text.encode(), text)
        approved_dictionary(self.store, doc['id'])
        return doc

    def execute(self, doc, outputs, *, clock=None, queue=5, cli=None):
        clock = clock or Clock(); cli = cli or Fake(outputs, clock)
        service = Service(self.store, self.root, cli)
        service.pool.shutdown(wait=True); service.pool = Queue()
        with patch('verinote.extraction_timing.time.monotonic_ns', side_effect=clock):
            run = service.start(doc['id'], 'agy', 'test-model', expected_version=1, expected_generation=1)
            clock.advance(queue); service.pool.run()
        return self.store.run(run['id']), read_extraction_timing(self.root, self.store, run['id']), cli

    def path(self, run): return self.root / 'runs' / run['id'] / 'extraction-timing.json'

    def test_queue_multi_chunk_and_non_additive_repair(self):
        doc = self.document(2)
        run, report, fake = self.execute(doc, [broken(), repair(), extraction_response()])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertTrue(report['available'])
        summary = report['summary']
        self.assertEqual((summary['queue_seconds'], summary['worker_seconds'], summary['total_seconds']), (5, 7, 12))
        self.assertEqual(summary['new_call_attempts'], 3)
        first, second = summary['chunks']
        self.assertEqual((first['request_seconds'], first['repair_request_seconds'], first['total_seconds']), (2, 3, 5))
        self.assertEqual((first['validation_with_bookkeeping_seconds'], first['repair_validation_with_bookkeeping_seconds']), (0, 0))
        self.assertEqual(second['request_seconds'], 2)
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(s['cli_call_id'] is None for s in report['spans']))
        raw = self.path(run).read_text()
        for secret in ('PRIVATE_TITLE', 'PRIVATE_FILENAME', SOURCE, 'prompt', '토큰'):
            self.assertNotIn(secret, raw)

    def test_initial_failure_and_timeout_leave_attempt_and_no_candidates(self):
        for error in (ValueError('PRIVATE_FAILURE'), TimeoutError('PRIVATE_TIMEOUT')):
            doc = self.document(); run, report, _ = self.execute(doc, [error])
            self.assertEqual(run['status'], 'error'); self.assertTrue(report['available'])
            self.assertEqual(report['summary']['new_call_attempts'], 1)
            attempts = [s for s in report['spans'] if s['kind'] == 'request']
            self.assertEqual(attempts[0]['status'], 'timeout' if isinstance(error, TimeoutError) else 'error')
            self.assertEqual(report['error_code'], attempts[0]['status'])
            self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])
            self.assertNotIn('PRIVATE_', self.path(run).read_text())

    def test_repair_call_and_repair_validation_failure_are_distinct(self):
        for second in (TimeoutError('PRIVATE_REPAIR_TIMEOUT'), {'repairs': []}):
            doc = self.document(); run, report, _ = self.execute(doc, [broken(), second])
            self.assertEqual(run['status'], 'error'); self.assertTrue(report['available'])
            repair_call = next(s for s in report['spans'] if s['kind'] == 'repair_request')
            if isinstance(second, Exception):
                self.assertEqual(repair_call['status'], 'timeout')
                self.assertFalse(any(s['kind'] == 'repair_validation_with_bookkeeping' for s in report['spans']))
            else:
                self.assertEqual(repair_call['status'], 'success')
                self.assertEqual(next(s for s in report['spans'] if s['kind'] == 'repair_validation_with_bookkeeping')['status'], 'error')

    def test_candidate_storage_failure_leaves_publish_failure(self):
        doc = self.document()
        with patch.object(self.store, 'finish_run', side_effect=ValueError('PRIVATE_PUBLISH_FAILURE')):
            run, report, _ = self.execute(doc, [extraction_response()])
        self.assertEqual(run['status'], 'error'); self.assertTrue(report['available'])
        self.assertEqual(next(s for s in report['spans'] if s['kind'] == 'publish')['status'], 'error')
        self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])

    def test_partial_and_full_resume_exclude_past_calls_and_times(self):
        doc = self.document(2)
        first, prior, _ = self.execute(doc, [extraction_response(), ValueError('stop')])
        self.assertEqual(prior['summary']['new_call_attempts'], 2)
        second, report, _ = self.execute(doc, [extraction_response()])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertTrue(report['available']); self.assertEqual(report['resumed_from'], first['id'])
        self.assertTrue(report['chunks'][0]['reused']); self.assertIsNone(report['summary']['chunks'][0]['total_seconds'])
        self.assertEqual((report['summary']['new_call_attempts'], report['summary']['worker_seconds']), (1, 2))
        self.assertEqual(report['summary']['restore_seconds'], 0)
        # A full validated prefix from a failed publication is reusable with zero new calls.
        doc = self.document(2)
        with patch.object(self.store, 'finish_run', side_effect=ValueError('publication stop')):
            failed, _, _ = self.execute(doc, [extraction_response(), extraction_response()])
        resumed, report, fake = self.execute(doc, [])
        self.assertEqual(resumed['status'], 'success', resumed['detail'])
        self.assertEqual((report['summary']['new_call_attempts'], report['summary']['worker_seconds']), (0, 0))
        self.assertEqual(fake.calls, []); self.assertTrue(all(c['reused'] for c in report['chunks']))
        self.assertEqual(job_calls(self.root, 'runs', resumed), [])
        self.assertEqual(report['resumed_from'], failed['id'])

    def test_direct_worker_has_unknown_queue_and_total(self):
        doc = self.document(); clock = Clock(); fake = Fake([extraction_response()], clock)
        service = Service(self.store, self.root, fake); service.pool.shutdown(wait=True)
        run = self.store.create_run(doc['id'], 'agy', 'test-model', 'extract', 1, 1)
        service.slots.acquire()
        with patch('verinote.extraction_timing.time.monotonic_ns', side_effect=clock): service.extract(run)
        report = read_extraction_timing(self.root, self.store, run['id'])
        self.assertTrue(report['available']); self.assertIsNone(report['summary']['queue_seconds'])
        self.assertIsNone(report['summary']['total_seconds']); self.assertEqual(report['summary']['worker_seconds'], 2)

    def test_write_and_clock_failures_do_not_change_results(self):
        original = __import__('verinote.http_records', fromlist=['write_record']).write_record
        def fail(folder, name, value, limit):
            if name == 'extraction-timing.json': raise OSError('PRIVATE_WRITE_FAILURE')
            return original(folder, name, value, limit)
        doc = self.document()
        with patch('verinote.http_records.write_record', side_effect=fail):
            run, report, _ = self.execute(doc, [extraction_response()])
        self.assertEqual(run['status'], 'success'); self.assertFalse(report['available'])
        doc = self.document()
        with patch('verinote.extraction_timing.ExtractionTiming', side_effect=OSError('PRIVATE_CLOCK_FAILURE')):
            run, report, _ = self.execute(doc, [TimeoutError('original timeout')])
        self.assertEqual(run['status'], 'error'); self.assertIn('original timeout', run['detail']); self.assertFalse(report['available'])

    def test_partial_clock_failure_does_not_break_success(self):
        doc = self.document(); clock = Clock(); fake = Fake([extraction_response()], clock)
        calls = 0
        def observed():
            nonlocal calls
            calls += 1
            if calls > 4: raise OSError('PRIVATE_PARTIAL_CLOCK')
            return clock()
        service = Service(self.store, self.root, fake); service.pool.shutdown(); service.pool = Queue()
        with patch('verinote.extraction_timing.time.monotonic_ns', side_effect=observed):
            run = service.start(doc['id'], 'agy', 'test-model', expected_version=1, expected_generation=1); service.pool.run()
        self.assertEqual(self.store.run(run['id'])['status'], 'success')
        self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])

    def test_nested_overlapping_or_stale_reports_are_rejected(self):
        doc = self.document(); run, report, _ = self.execute(doc, [broken(), repair()])
        original = json.loads(self.path(run).read_text())
        for mutate in (lambda r: r['identity'].update(source_sha256='b'*64),
                       lambda r: r['identity'].update(run_id='b'*32),
                       lambda r: r['spans'][1].update(end=r['worker_finished']+1),
                       lambda r: r['chunks'][0].update(length=1),
                       lambda r: r.update(worker_finished=True)):
            saved = copy.deepcopy(original); mutate(saved); self.path(run).write_text(json.dumps(saved))
            self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])
        saved = copy.deepcopy(original)
        request = next(s for s in saved['spans'] if s['kind'] == 'request')
        validation = next(s for s in saved['spans'] if s['kind'] == 'validation_with_bookkeeping')
        request['end'] = validation['end']
        with self.assertRaises(ValueError): validate_record(saved)
        self.path(run).unlink(); self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])
        target = self.root / 'timing-target'; target.write_text(json.dumps(original))
        try: self.path(run).symlink_to(target)
        except (OSError, NotImplementedError): self.skipTest('Symlink creation unavailable')
        self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])

    def test_call_link_uses_724_record_and_stale_link_is_separately_unavailable(self):
        doc = self.document()
        script = self.root / 'fake_cli.py'; script.write_text('print(' + repr(json.dumps({'status': 'SUCCESS', 'response': json.dumps(extraction_response())})) + ')')
        with patch('verinote.agents.executable', return_value=sys.executable), patch('verinote.agents.command', return_value=([sys.executable, str(script)], '')):
            run, report, _ = self.execute(doc, [], cli=CLI())
        self.assertEqual(run['status'], 'success', run['detail']); self.assertTrue(report['available'])
        call = next(s for s in report['spans'] if s['kind'] == 'request')
        self.assertTrue(call['cli_link_available'])
        self.assertEqual(call['cli_call_id'], job_calls(self.root, 'runs', run)[0]['cli_call_id'])
        (self.root / 'runs' / run['id'] / '1' / 'cli-timing.json').unlink()
        report = read_extraction_timing(self.root, self.store, run['id'])
        self.assertTrue(report['available']); self.assertFalse(next(s for s in report['spans'] if s['kind'] == 'request')['cli_link_available'])

    def test_restore_and_nonzero_validation_are_measured_without_overlap(self):
        from verinote import service as module
        doc = self.document(); clock = Clock()
        original_checkpoint, original_issues, original_page = module.ExtractionCheckpoint, module.extraction_quote_issues, module.validate_page
        def restore(*args):
            result = original_checkpoint(*args); clock.advance(1); return result
        def issues(*args, **kwargs):
            result = original_issues(*args, **kwargs); clock.advance(1); return result
        def page(*args):
            result = original_page(*args); clock.advance(1); return result
        with patch('verinote.service.ExtractionCheckpoint', side_effect=restore), patch('verinote.service.extraction_quote_issues', side_effect=issues), patch('verinote.service.validate_page', side_effect=page):
            run, report, _ = self.execute(doc, [broken(), repair()], clock=clock)
        self.assertEqual(run['status'], 'success', run['detail']); self.assertTrue(report['available'])
        row = report['summary']['chunks'][0]
        self.assertEqual(report['summary']['restore_seconds'], 1)
        self.assertEqual((row['validation_with_bookkeeping_seconds'], row['repair_validation_with_bookkeeping_seconds']), (3, 2))
        self.assertEqual((row['total_seconds'], report['summary']['worker_seconds']), (10, 12))
        self.assertEqual(report['summary']['publish_seconds'], 1)

    def test_http_and_ontology_do_not_create_timing(self):
        from verinote.extraction_timing import create_timing
        with patch('verinote.extraction_timing.ExtractionTiming') as constructor:
            for agent, stage in (('openrouter', 'extract'), ('agy', 'ontology')):
                timing = create_timing(self.root, agent, stage)
                timing.bind({}); timing.worker_start(); timing.configure({}); timing.finish()
            constructor.assert_not_called()

    def test_documented_example_and_limits(self):
        example = Path('docs/extraction-timing.md').read_text().split('```json\n', 1)[1].split('```', 1)[0]
        value = validate_record(json.loads(example)); summary = summarize_record(value)
        self.assertEqual((summary['total_seconds'], summary['queue_seconds'], summary['worker_seconds']), (14, 2, 12))
        self.assertEqual(summary['worker_other_seconds'], 1)
        self.assertEqual(summary['chunks'][0]['other_seconds'], 2)
        for mutate in (lambda r: r.update(schema_version=99), lambda r: r['spans'][0].update(kind='unknown'),
                       lambda r: r['spans'][1].update(end=float('inf')), lambda r: r['spans'][1].update(end=10**1000)):
            bad = copy.deepcopy(value); mutate(bad)
            with self.assertRaises(ValueError): validate_record(bad)
        doc = self.document(); run, _, _ = self.execute(doc, [extraction_response()])
        self.path(run).write_text('x' * (128*1024 + 1))
        self.assertFalse(read_extraction_timing(self.root, self.store, run['id'])['available'])

    def test_non_overlapping_derived_sum_and_absent_measurements(self):
        doc = self.document(); _, report, _ = self.execute(doc, [broken(), repair()])
        summary = summarize_record({k: v for k, v in report.items() if k not in ('available', 'summary')})
        row = summary['chunks'][0]
        leaves = sum(row[key] for key in ('request_seconds', 'validation_with_bookkeeping_seconds', 'repair_request_seconds',
                                         'repair_validation_with_bookkeeping_seconds', 'repair_other_seconds', 'other_seconds'))
        self.assertEqual(leaves, row['total_seconds'])
        self.assertEqual(row['total_seconds'], 5)
