"""Synthetic calls only: cancelling a queued or running run, with gated fake transports instead of models."""
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_ontology import wire_dictionary
from test_quote_repair import broken
from test_quote_repair import repair as repair_response
from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

import verinote.dictionary_context as dictionary_context
import verinote.extraction_checkpoint as extraction_checkpoint
import verinote.service as service_module
from verinote import ollama
from verinote.agents import CLI, PROVIDERS
from verinote.copilot_extraction import CopilotExtractionSpec
from verinote.dictionary_context import RequestTooLarge
from verinote.extraction_transport import ExtractionSpec
from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, Store

C1 = '대기 중에 취소되었습니다. 이 실행은 모델을 호출하지 않았습니다.'
C2 = '취소 요청됨: 이미 허용한 호출은 중단하지 않고 끝날 때까지 기다린 뒤, 새 호출 없이 멈춥니다. 멈출 때까지 같은 문서의 다음 실행은 기다리며, 다른 문서와 질문은 각 실행 한도 안에서 계속됩니다.'
HEAD = '처리 중에 취소되었습니다. 취소 요청 뒤로는 새 호출을 허용하지 않았고 {} 저장하지 않았습니다.'
EXTRACT_HEAD = HEAD.format('위키·관계 후보는')
DICTIONARY_HEAD = HEAD.format('사전 초안은')


class Gate:
    """Records every call and blocks the calls of the chunks (or dictionary segments) in `hold` until released."""

    def __init__(self, hold=(1,), spec=None):
        self.calls, self.lock, self.entered, self.release, self.hold = [], threading.Lock(), {}, threading.Event(), set(hold)
        if spec is not None: self.extraction_spec = spec
        self.ontology = False

    def limited_timeout(self, agent, seconds): return self

    def complete(self, agent, model, prompt, folder, **kw):
        folder = Path(folder)
        repair = folder.name == 'quote-repair'
        index = int(folder.parent.name if repair else folder.name)
        with self.lock:
            self.calls.append((index, repair))
            self.entered.setdefault(index, threading.Event()).set()
        if index in self.hold and not self.release.wait(20): raise TimeoutError('gate')
        return wire_dictionary(DICTIONARY, prompt) if self.ontology else extraction_response()

    def wait_entered(self, index, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self.lock: event = self.entered.get(index)
            if event is not None: return event.wait(max(0, deadline - time.monotonic()))
            time.sleep(.005)
        return False

    def close(self): pass


def occupy_worker(service, release, stage='extract', key='reserved-worker'):
    """Occupy one real lane worker so queued-state tests have a deterministic full lane."""
    entered = threading.Event()
    def hold(job):
        entered.set()
        if not release.wait(20): raise TimeoutError('reserved worker')
    future = service.pool.submit(hold, {'id': key, 'stage': stage, 'document_id': key})
    if not entered.wait(3): raise AssertionError('worker did not start')
    return future


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')

    def document(self, chunks=3, title='doc'):
        source = SOURCE.ljust(8000) * (chunks - 1) + SOURCE
        doc = self.store.register(title, f'{title}.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return self.store.document(doc['id'])

    def service(self, gate, **kw):
        service = Service(self.store, self.root, gate, **kw)
        self.addCleanup(lambda: (gate.release.set(), service.pool.shutdown(wait=True)))
        return service

    def start(self, service, doc, **kw):
        current = self.store.document(doc['id'])
        return service.start(doc['id'], kw.pop('agent', 'agy'), kw.pop('model', ''), kw.pop('stage', 'extract'), current['version'], current['generation'], **kw)

    def finish(self, service, gate=None):
        if gate is not None: gate.release.set()
        service.pool.shutdown(wait=True)

    def checkpoint_chunks(self, run_id):
        path = self.root / 'runs' / run_id / 'extraction-checkpoint.json'
        return len(json.loads(path.read_text())['outputs']) if path.exists() else 0

    def candidates(self, doc):
        return [item for item in self.store.document(doc['id'])['items'] if item['kind'] != 'ontology']

    def rerun(self, doc, **kw):
        gate = Gate(hold=())
        gate.ontology = kw.pop('ontology', False)
        service = self.service(gate)
        run = self.start(service, doc, **kw)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), gate


class Sequential(Base):
    def test_cancel_stops_after_the_call_in_flight_and_the_checkpoint_is_reused(self):
        doc, gate = self.document(3), Gate(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        self.assertEqual(service.cancel(run['id'], 'running'), 'running')
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], row['detail']), ('cancelling', C2))
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled')
        self.assertTrue(row['detail'].startswith(EXTRACT_HEAD + ' · '), row['detail'])
        self.assertNotIn('멈추기 전 마지막 오류', row['detail'])
        self.assertIn('검증 완료 1/3청크 · 새 호출 시도 1회', row['detail'])
        self.assertIn('다시 추출할 때 청크 2부터 호출합니다', row['detail'])
        self.assertEqual(gate.calls, [(1, False)])
        self.assertEqual(self.checkpoint_chunks(run['id']), 1)
        self.assertEqual(self.candidates(doc), [])
        self.assertEqual(service.slots._value, 8)
        again, second = self.rerun(doc)
        self.assertEqual(again['status'], 'success', again['detail'])
        self.assertEqual(sorted(second.calls), [(2, False), (3, False)])
        self.assertIn('1청크 재사용', again['detail'])

    def test_the_row_read_just_before_the_cancel_end_write_still_holds_the_cancel_request_text(self):
        doc, gate = self.document(3), Gate(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        finish, seen = self.store.finish_cancel, []

        def read_then_finish(run_id, detail):
            row = self.store.run(run_id)
            seen.append((row['status'], row['detail']))
            finish(run_id, detail)
        self.store.finish_cancel = read_then_finish
        self.finish(service, gate)
        self.assertEqual(seen, [('cancelling', C2)])

    def test_cancel_in_the_last_chunk_saves_nothing_and_the_rerun_needs_no_call(self):
        doc, gate = self.document(2), Gate(hold=(2,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(2))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(self.candidates(doc), [])
        self.assertIn('새 호출 없이 저장을 다시 시도합니다', row['detail'])
        again, second = self.rerun(doc)
        self.assertEqual((again['status'], second.calls), ('success', []))
        self.assertTrue(self.candidates(doc))

    def test_cancel_after_publish_is_refused_and_the_run_stays_success(self):
        doc, gate = self.document(1), Gate(hold=())
        service = self.service(gate)
        run = self.start(service, doc)
        self.finish(service)
        self.assertEqual(self.store.run(run['id'])['status'], 'success')
        with self.assertRaises(Conflict) as refused: service.cancel(run['id'], 'running')
        self.assertEqual(str(refused.exception), '이미 끝난 실행이라 취소하지 않았습니다 (상태: 완료).')
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_failure_after_cancel_ends_cancelled_with_the_last_error(self):
        class Fail(Gate):
            def complete(self, *a, **k):
                super().complete(*a, **k)
                raise ValueError('boom')
        doc, gate = self.document(2), Fail(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled')
        self.assertTrue(row['detail'].startswith(EXTRACT_HEAD + ' · 멈추기 전 마지막 오류: boom'), row['detail'])

    def test_a_cancel_that_commits_after_the_failure_path_read_the_status_ends_cancelled(self):
        armed = {}

        class Fail(Gate):
            def complete(self, *a, **k):
                super().complete(*a, **k)
                armed['now'] = True
                raise ValueError('boom')
        doc, gate = self.document(2), Fail(hold=())
        service = self.service(gate)
        read = self.store.run

        def run_then_cancel(run_id):
            row = read(run_id)
            if armed.pop('now', False): service.cancel(run_id, 'running')
            return row
        self.store.run = run_then_cancel
        run = self.start(service, doc)
        self.finish(service)
        self.store.run = read
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled', row['detail'])
        self.assertTrue(row['detail'].startswith(EXTRACT_HEAD + ' · 멈추기 전 마지막 오류: '), row['detail'])
        self.assertIn('boom', row['detail'])
        self.assertEqual(service.slots._value, 8)

    def test_a_cancel_during_the_call_stops_a_response_that_needs_repair_before_the_repair_call(self):
        class Broken(Gate):
            def complete(self, agent, model, prompt, folder, **kw):
                super().complete(agent, model, prompt, folder, **kw)
                return repair_response() if Path(folder).name == 'quote-repair' else broken()
        doc, gate = self.document(2), Broken(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], gate.calls), ('cancelled', [(1, False)]), row['detail'])

    def test_a_cancel_committed_by_the_repair_status_write_stops_the_repair_call_quietly(self):
        class Broken(Gate):
            def complete(self, agent, model, prompt, folder, **kw):
                super().complete(agent, model, prompt, folder, **kw)
                return repair_response() if Path(folder).name == 'quote-repair' else broken()
        doc, gate = self.document(2), Broken(hold=())
        service = self.service(gate)
        write, fired = self.store.run_status, []

        def write_then_cancel(run_id, status, detail=''):
            write(run_id, status, detail)
            if '원문 인용 보정 중' in detail and not fired:
                fired.append(1)
                service.cancel(run_id, 'running')
        self.store.run_status = write_then_cancel
        run = self.start(service, doc)
        self.finish(service)
        self.store.run_status = write
        row = self.store.run(run['id'])
        self.assertTrue(fired)
        self.assertEqual((row['status'], gate.calls), ('cancelled', [(1, False)]), row['detail'])
        self.assertNotIn('인용 보정 실패', row['detail'])
        self.assertNotIn('멈추기 전 마지막 오류', row['detail'])

    def test_a_cancel_that_commits_during_the_size_check_before_a_refusal_keeps_the_summary_and_drops_the_retry_notice(self):
        doc, gate = self.document(2), Gate(hold=())
        service = self.service(gate)
        original, box = service_module.worst_refusal, {}

        def cancel_then_refuse(*a, **k):
            service.cancel(box['id'], 'running')
            raise RequestTooLarge(70000, 60000, 1000)
        submit = service.pool.submit

        def remember(fn, run, *a, **k):
            box['id'] = run['id']
            return submit(fn, run, *a, **k)
        service.pool.submit = remember
        service_module.worst_refusal = cancel_then_refuse
        try:
            run = self.start(service, doc)
            self.finish(service)
        finally:
            service_module.worst_refusal = original
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], gate.calls), ('cancelled', []), row['detail'])
        self.assertTrue(row['detail'].startswith(EXTRACT_HEAD + ' · 멈추기 전 마지막 오류: '), row['detail'])
        self.assertIn('검증 완료 0/2청크 · 새 호출 시도 0회', row['detail'])
        self.assertNotIn('다시 추출할 때', row['detail'])


class Parallel(Base):
    def run_case(self, agent, model, concurrency, spec):
        doc, gate = self.document(4), Gate(hold=(1, 2), spec=spec)
        service = self.service(gate, extraction_caller=gate)
        run = self.start(service, doc, agent=agent, model=model, extraction_concurrency=concurrency)
        self.assertTrue(gate.wait_entered(1) and gate.wait_entered(2))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled', row['detail'])
        self.assertEqual(sorted(gate.calls), [(1, False), (2, False)])
        self.assertEqual(self.checkpoint_chunks(run['id']), 2)
        self.assertIn('검증 완료 2/4청크 · 새 호출 시도 2회 · ', row['detail'])
        self.assertTrue(row['detail'].endswith('다시 추출할 때 청크 3부터 호출합니다.'), row['detail'])
        self.assertEqual(service.slots._value, 8)

    def test_cancel_saves_nothing_from_a_chunk_that_needs_a_repair(self):
        """Pins behaviour that is not new: the cancel commits before chunk 1's response is released, so store.run_status raises
        RunCancelled at the repair announcement of chunk 1 (not the scheduler's drain). No repair call is admitted, and the chunk
        that needed one is not saved."""
        class Broken(Gate):
            def complete(self, agent, model, prompt, folder, **kw):
                value = super().complete(agent, model, prompt, folder, **kw)
                return broken() if Path(folder).name == '1' else value
        doc, gate = self.document(4), Broken(hold=(1, 2), spec=ExtractionSpec('/fake/claude', 'model', 2, 10, 1048576, 'null'))
        service = self.service(gate, extraction_caller=gate)
        run = self.start(service, doc, agent='claude', model='model', extraction_concurrency=2)
        self.assertTrue(gate.wait_entered(1) and gate.wait_entered(2))
        service.cancel(run['id'], 'running')
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelling')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual(row['status'], 'cancelled', row['detail'])
        self.assertEqual(sorted(gate.calls), [(1, False), (2, False)])
        self.assertEqual(self.checkpoint_chunks(run['id']), 0)
        self.assertIn('검증 완료 0/4청크 · 새 호출 시도 2회', row['detail'])
        self.assertIn('첫 청크부터', row['detail'])
        timing = self.root / 'runs' / run['id'] / 'extraction-timing.json'
        spans = json.loads(timing.read_text())['spans'] if timing.exists() else []
        self.assertEqual([s for s in spans if s['kind'].startswith('repair')], [])

    def test_one_worker_cancel_ends_at_the_refused_chunk_without_waiting_for_its_deadline(self):
        """With one worker the cancel refuses chunk 2's admission while chunk 1 is pending: the run saves chunk 1 and ends at once
        instead of waiting out chunk 2's deadline."""
        from unittest.mock import patch
        doc, gate = self.document(3), Gate(hold=(1,), spec=ExtractionSpec('/fake/claude', 'model', 1, 10, 1048576, 'null'))
        service = self.service(gate, extraction_caller=gate)
        with patch('verinote.service.CHUNK_TIMEOUT', 30):
            run = self.start(service, doc, agent='claude', model='model', extraction_concurrency=1)
            self.assertTrue(gate.wait_entered(1))
            service.cancel(run['id'], 'running')
            began = time.monotonic()
            self.finish(service, gate)
        self.assertLess(time.monotonic() - began, 15)
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], gate.calls, self.checkpoint_chunks(run['id'])), ('cancelled', [(1, False)], 1), row['detail'])

    def test_claude_threshold_policy_cancel_saves_the_admitted_second_chunk(self):
        import sys
        from unittest.mock import patch

        from verinote.extraction_options import CHUNK_THRESHOLD_POLICY
        doc, gate = self.document(4), Gate(hold=(1, 2))
        def factory(spec):
            gate.extraction_spec = spec
            return gate
        service = Service(self.store, self.root, CLI(), extraction_caller_factory=factory)
        self.addCleanup(lambda: (gate.release.set(), service.pool.shutdown(wait=True)))
        with patch('verinote.extraction_transport.executable', return_value=sys.executable):
            run = self.start(service, doc, agent='claude', model='model', extraction_policy=CHUNK_THRESHOLD_POLICY)
        self.assertTrue(gate.wait_entered(1) and gate.wait_entered(2))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], sorted(gate.calls), self.checkpoint_chunks(run['id'])), ('cancelled', [(1, False), (2, False)], 2), row['detail'])

    def test_claude_two_chunk_cancel_admits_no_third_call(self):
        self.run_case('claude', 'model', 2, ExtractionSpec('/fake/claude', 'model', 2, 10, 1048576, 'null'))

    def test_copilot_two_chunk_cancel_admits_no_third_call(self):
        self.run_case('copilot', 'auto', 2, CopilotExtractionSpec('/fake/copilot', 'auto', 2, 10, 1048576, 'null'))


class Queued(Base):
    def service(self, gate, **kw):
        service = super().service(gate, **kw)
        occupy_worker(service, gate.release)
        return service

    def two(self):
        first = self.document(1, 'first')
        second = self.document(1, 'second')
        return first, second

    def test_cancelling_a_queued_run_frees_its_slot_and_it_never_calls_a_model(self):
        first, second = self.two()
        gate = Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        before = service.slots._value
        b = self.start(service, second)
        self.assertEqual(self.store.run(b['id'])['status'], 'queued')
        self.assertEqual(service.cancel(b['id'], 'queued'), 'queued')
        row = self.store.run(b['id'])
        self.assertEqual((row['status'], row['detail']), ('cancelled', C1))
        self.assertEqual(service.slots._value, before)
        self.finish(service, gate)
        self.assertEqual(gate.calls, [(1, False)])
        self.assertEqual(self.store.run(a['id'])['status'], 'success')
        self.assertEqual(self.store.run(b['id'])['detail'], C1)
        self.assertEqual(service.slots._value, 8)

    def test_a_run_cancelled_while_queued_shows_zero_calls_on_the_run_and_in_the_usage_report(self):
        first, second = self.two()
        gate = Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)
        service.cancel(b['id'], 'queued')
        self.finish(service, gate)
        usage = service.with_usage('runs', self.store.run(b['id']))['usage']
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['details']), (0, 0, []))
        from verinote.usage import job_calls
        owner = next(o for o in self.store.usage_owners()[0] if o['id'] == b['id'])  # the owner usage_report reads
        self.assertEqual(job_calls(self.root, 'runs', owner), [])

    def test_only_the_queued_cancel_marker_reads_as_zero_calls(self):
        from verinote.usage import job_calls
        for stage in ('extract', 'ontology'):
            for status, detail, unknown in (('cancelled', C1, False), ('cancelled', EXTRACT_HEAD, True), ('error', C1, True)):
                with self.subTest(stage=stage, status=status, detail=detail):
                    owner = {'id': 'c' * 32, 'agent': 'agy', 'model': '', 'stage': stage, 'status': status, 'detail': detail}
                    calls = job_calls(self.root, 'runs', owner)
                    self.assertEqual([c['call_count_known'] for c in calls], [False] if unknown else [])
        folder = self.root / 'runs' / ('d' * 32) / '1'
        folder.mkdir(parents=True)
        (folder / 'usage.json').write_text('{}')
        owner = {'id': 'd' * 32, 'agent': 'agy', 'model': '', 'stage': 'extract', 'status': 'cancelled', 'detail': C1}
        self.assertEqual([c['call_id'] for c in job_calls(self.root, 'runs', owner)], [f"runs/{'d' * 32}/1"])  # a log outranks the marker

    def test_a_queued_run_whose_worker_already_started_keeps_the_queued_detail_and_releases_its_slot(self):
        first, second = self.two()
        gate = Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)

        class Started:
            def cancel(self): return False
        service.futures[b['id']] = Started()
        service.cancel(b['id'], 'queued')
        self.finish(service, gate)
        row = self.store.run(b['id'])
        self.assertEqual((row['status'], row['detail']), ('cancelled', C1))
        self.assertEqual(gate.calls, [(1, False)])
        self.assertEqual(service.slots._value, 8)
        self.assertEqual(service.with_usage('runs', row)['usage']['calls'], 0)

    def test_runs_cancelled_while_queued_do_not_fill_the_window_of_retry_candidates(self):
        target, blocker = self.document(3, 'target'), self.document(2, 'blocker')
        first_gate = Gate(hold=(1,))
        first_service = self.service(first_gate)
        first = self.start(first_service, target)
        self.assertTrue(first_gate.wait_entered(1))
        first_service.cancel(first['id'], 'running')
        self.finish(first_service, first_gate)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, blocker)
        self.assertTrue(gate.wait_entered(1))
        for _ in range(9):
            queued = self.start(service, target)
            service.cancel(queued['id'], 'queued')
            self.assertEqual(self.store.run(queued['id'])['detail'], C1)
        self.finish(service, gate)
        again, second = self.rerun(target)
        self.assertEqual(again['status'], 'success', again['detail'])
        self.assertEqual(sorted(second.calls), [(2, False), (3, False)])
        self.assertIn('1청크 재사용', again['detail'])

    def test_both_retry_searches_skip_runs_cancelled_while_queued(self):
        extract_doc = self.document(1, 'extract')
        bare = self.store.register('bare', 'bare.md', SOURCE.encode(), SOURCE)
        for doc, stage, search in ((extract_doc, 'extract', self.store.extraction_retries), (bare, 'ontology', self.store.dictionary_retries)):
            earlier = self.create(doc, stage)
            self.store.run_status(earlier['id'], 'error')
            for _ in range(9):
                self.store.cancel_run(self.create(doc, stage)['id'], 'queued')
            self.assertEqual([run['id'] for run in search(self.create(doc, stage))], [earlier['id']], stage)

    def create(self, doc, stage):
        pinned = self.store.document(doc['id'])
        return self.store.create_run(doc['id'], 'agy', '', stage, pinned['version'], pinned['generation'])

    def test_a_run_that_started_between_the_dialog_and_the_cancel_is_refused_not_cancelled(self):
        doc, gate = self.document(2), Gate(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        with self.assertRaises(Conflict) as stale: service.cancel(run['id'], 'queued')
        self.assertEqual(str(stale.exception), '대기 중이던 실행이 처리 중으로 바뀌어 취소하지 않았습니다. 처리 중인 실행을 취소할지 다시 확인하세요.')
        self.assertEqual(self.store.run(run['id'])['status'], 'running')
        with self.assertRaises(ValueError): service.cancel(run['id'], 'cancelling')
        with self.assertRaises(ValueError): service.cancel('0' * 32, 'running')
        self.assertEqual(service.cancel(run['id'], 'running'), 'running')
        with self.assertRaises(Conflict) as twice: service.cancel(run['id'], 'running')
        self.assertEqual(str(twice.exception), '이미 취소를 요청한 실행입니다. 아직 멈추지 않았습니다.')
        self.finish(service, gate)


class Dictionary(Base):
    def source(self, size):
        return self.store.register('dictionary', 'dictionary.md', size.encode(), size)

    def test_a_cancel_in_the_first_segment_calls_no_more_and_the_rerun_reuses_it(self):
        source = SOURCE.ljust(4000) * 2 + SOURCE
        doc = self.source(source)
        gate = Gate(hold=(1,))
        gate.ontology = True
        service = self.service(gate)
        run = self.start(service, doc, stage='ontology')
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        row = self.store.run(run['id'])
        self.assertEqual((row['status'], gate.calls), ('cancelled', [(1, False)]), row['detail'])
        self.assertTrue(row['detail'].startswith(DICTIONARY_HEAD + ' · 검증 완료 '), row['detail'])
        self.assertNotIn('멈추기 전 마지막 오류', row['detail'])
        self.assertIn('새 호출 시도 1회', row['detail'])
        again, second = self.rerun(self.store.document(doc['id']), stage='ontology', ontology=True)
        self.assertEqual((again['status'], sorted(second.calls)), ('success', [(2, False), (3, False)]), again['detail'])
        self.assertIn('4,000자 재사용', again['detail'])

    def test_a_cancel_committed_by_the_segment_status_write_stops_the_first_call(self):
        doc = self.source(SOURCE)
        gate = Gate(hold=())
        gate.ontology = True
        service = self.service(gate)
        write, fired = self.store.run_status, []

        def write_then_cancel(run_id, status, detail=''):
            write(run_id, status, detail)
            if detail.startswith('사전 생성 · 구간 1/') and not fired:
                fired.append(1)
                service.cancel(run_id, 'running')
        self.store.run_status = write_then_cancel
        run = self.start(service, doc, stage='ontology')
        self.finish(service)
        self.store.run_status = write
        row = self.store.run(run['id'])
        self.assertTrue(fired)
        self.assertEqual((row['status'], gate.calls), ('cancelled', []), row['detail'])
        self.assertTrue(row['detail'].startswith(DICTIONARY_HEAD), row['detail'])
        self.assertNotIn('멈추기 전 마지막 오류', row['detail'])

    def finish_first(self, doc):
        gate = Gate(hold=(1,))
        gate.ontology = True
        service = self.service(gate)
        run = self.start(service, doc, stage='ontology')
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        return self.store.run(run['id'])

    def drafts(self, doc):
        return [item for item in self.store.document(doc['id'])['items'] if item['kind'] == 'ontology']

    def test_a_cancel_in_the_only_segment_saves_no_draft_and_keeps_the_progress(self):
        doc = self.source(SOURCE)
        row = self.finish_first(doc)
        self.assertEqual(row['status'], 'cancelled', row['detail'])
        self.assertEqual(row['detail'], DICTIONARY_HEAD + f' · 검증 완료 {len(SOURCE):,}/{len(SOURCE):,}자 · 새 호출 시도 1회')
        self.assertEqual(self.drafts(doc), [])

    def test_the_admission_notice_of_an_unsaved_draft_stays_out_of_a_cancelled_run(self):
        original = dictionary_context.worst_refusal

        def broken_estimate(*a, **k): raise RuntimeError('estimate')
        dictionary_context.worst_refusal = broken_estimate
        try:
            row = self.finish_first(self.source(SOURCE))
        finally:
            dictionary_context.worst_refusal = original
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['detail'], DICTIONARY_HEAD + f' · 검증 완료 {len(SOURCE):,}/{len(SOURCE):,}자 · 새 호출 시도 1회')
        self.assertNotIn('추출', row['detail'].partition('저장하지 않았습니다.')[2])


class Ending(Base):
    def cancel_with_failing_finish(self, mode):
        doc, gate = self.document(2), Gate(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        finish, attempts = self.store.finish_cancel, []

        def flaky(run_id, detail):
            attempts.append(detail)
            if mode == 'always' or len(attempts) == 1: raise sqlite3.OperationalError('database is locked')
            return finish(run_id, detail)
        self.store.finish_cancel = flaky
        service.cancel(run['id'], 'running')
        self.finish(service, gate)
        return self.store.run(run['id']), attempts, service

    def test_a_failed_end_write_is_retried_and_the_slot_is_released(self):
        row, attempts, service = self.cancel_with_failing_finish('once')
        self.assertEqual((row['status'], len(attempts), service.slots._value), ('cancelled', 2, 8))

    def test_an_end_write_that_always_fails_leaves_cancelling_but_releases_the_slot(self):
        row, attempts, service = self.cancel_with_failing_finish('always')
        self.assertEqual((row['status'], len(attempts), service.slots._value), ('cancelling', 3, 8))
        self.assertEqual(attempts[2], '취소된 실행이 멈췄지만 취소 상세를 저장하지 못했습니다.')
        self.assertEqual(attempts[0], attempts[1])

    def test_a_long_plain_detail_is_tried_twice_cut_and_then_the_fallback_only(self):
        written = []
        service = self.service(Gate())

        def reject(text):
            written.append(text)
            raise sqlite3.OperationalError('locked')
        service._ladder(reject, 'a' * 2500, 'fallback')
        self.assertEqual(written, ['a' * 2000, 'a' * 2000, 'fallback'])

    def test_the_escaped_attempt_is_cut_before_an_escape_would_be_split(self):
        written = []
        service = self.service(Gate())

        def reject(text):
            written.append(text)
            raise sqlite3.OperationalError('locked')
        service._ladder(reject, '\ud800' * 400 + '끝', 'fallback')
        self.assertEqual(len(written), 4)
        self.assertEqual(written[2], '\\ud800' * 333)
        self.assertEqual(written[3], 'fallback')

    def test_cancel_holds_the_admission_lock_until_the_admission_is_counted(self):
        doc, gate = self.document(2), Gate(hold=(1,))
        service = self.service(gate)
        entered, proceed = threading.Event(), threading.Event()
        read = self.store.raise_if_cancelled

        def read_then_wait(run_id):
            read(run_id)
            entered.set()
            proceed.wait(10)
        self.store.raise_if_cancelled = read_then_wait
        run = self.start(service, doc)
        self.assertTrue(entered.wait(5))
        cancelling = threading.Thread(target=service.cancel, args=(run['id'], 'running'))
        cancelling.start()
        cancelling.join(.3)
        self.assertTrue(cancelling.is_alive())
        proceed.set()
        cancelling.join(5)
        progress = self.root / 'runs' / run['id'] / 'extraction-progress.json'
        counted = json.loads(progress.read_text())['calls_started']
        self.assertEqual(counted, 1)
        self.assertTrue(gate.wait_entered(1))
        self.finish(service, gate)
        self.assertEqual(json.loads(progress.read_text())['calls_started'], counted)
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelled')


class Refusals(Base):
    def attempt(self, call):
        try:
            call()
            return 'allowed'
        except Conflict as refused:
            return str(refused)

    def test_cancelling_lifts_the_review_and_registration_refusals_but_keeps_the_deletion_one(self):
        doc = self.document(2)
        gate = Gate(hold=(1,), spec=ExtractionSpec('/fake/claude', 'model', 1, 10, 1048576, 'null'))
        service = self.service(gate, extraction_caller=gate)
        run = self.start(service, doc, agent='claude', model='model', extraction_concurrency=1)
        self.assertTrue(gate.wait_entered(1))
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        current = self.store.document(doc['id'])

        def review(): return self.store.review(item['id'], item['revision'], 'held', 'r', 'x')

        def delete(): return self.store.delete_document(doc['id'], False, current['current_version'], current['generation'])
        self.assertIn('끝나지 않아 사전을 바꿀 수 없습니다', self.attempt(review))
        self.assertIn('진행 중입니다', self.attempt(delete))
        service.cancel(run['id'], 'running')
        self.assertEqual(self.attempt(review), 'allowed')
        current = self.store.document(doc['id'])

        def register(): return self.store.register('doc', 'x.md', b'new', 'new', doc['id'], current['version'], current['generation'])
        self.assertEqual(self.attempt(register), 'allowed')
        current = self.store.document(doc['id'])
        self.assertEqual(self.attempt(delete), '취소한 실행이 아직 멈추지 않았습니다. 실행 기록에서 취소됨으로 바뀐 뒤 삭제하세요. '
                         '제한 시간(추출 청크 360초, 기본 사전 생성 호출 600초)이 지나도 바뀌지 않으면 서버를 재시작하세요. 재시작하면 대기·처리 중인 실행과 질문은 중단으로, 취소 중인 실행과 질문은 취소됨으로 표시됩니다.')
        self.finish(service, gate)
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelled')
        current = self.store.document(doc['id'])
        self.assertEqual(self.attempt(delete), 'allowed')

    def test_the_deletion_refusal_names_the_limits_the_code_enforces(self):
        self.assertEqual(extraction_checkpoint.CHUNK_TIMEOUT, 360)
        self.assertEqual({CLI().effective_timeout(agent, 'ontology') for agent in PROVIDERS}, {600})
        self.assertEqual(ollama.DEFAULTS['timeouts']['ontology'], 600)
        self.assertEqual(ollama.options()['timeouts']['ontology'], 600)
        with self.assertRaises(ValueError): ollama.options(ollama.DEFAULTS | {'timeouts': {'default': 180, 'ontology': 601}})

    def test_a_queued_question_keeps_the_generic_deletion_refusal_next_to_a_cancelling_run(self):
        doc = self.document(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        run = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        service.cancel(run['id'], 'running')
        snapshot = self.store.snapshot()
        self.store.create_question('q', 'agy', '', snapshot['id'])
        current = self.store.document(doc['id'])
        refused = self.attempt(lambda: self.store.delete_document(doc['id'], False, current['current_version'], current['generation']))
        self.assertEqual(refused, '추출 또는 질문 작업이 진행 중입니다. 모든 작업 완료 후 삭제하세요.')
        self.finish(service, gate)

    def test_recover_ends_a_cancelling_run_as_cancelled_by_stage(self):
        extract_doc = self.document(1, 'extract')
        bare = self.store.register('bare', 'bare.md', SOURCE.encode(), SOURCE)
        for doc, stage, noun in ((extract_doc, 'extract', '위키·관계 후보는'), (bare, 'ontology', '사전 초안은')):
            pinned = self.store.document(doc['id'])
            run = self.store.create_run(doc['id'], 'agy', '', stage, pinned['version'], pinned['generation'])
            self.store.run_status(run['id'], 'running')
            self.store.cancel_run(run['id'], 'running')
            self.store.recover()
            row = self.store.run(run['id'])
            self.assertEqual(row['status'], 'cancelled')
            self.assertEqual(row['detail'], f'취소를 요청한 뒤 실행이 멈춘 것을 기록하기 전에 서버가 재시작되었습니다. {noun} 저장하지 않았습니다.')


class Route(Base):
    def setUp(self):
        super().setUp()
        self.gate = Gate(hold=(1,))
        self.service = Service(self.store, self.root, self.gate)
        occupy_worker(self.service, self.gate.release)
        self.server = Server(('127.0.0.1', 0), self.store, self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(lambda: (self.gate.release.set(), self.service.pool.shutdown(wait=True), self.server.shutdown(), self.server.server_close()))

    def call(self, path, method='POST', data=None, headers=None):
        request = Request(self.url + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error:
            with error: return error.code, json.loads(error.read())

    def test_body_status_codes_and_the_previous_status_in_the_answer(self):
        doc = self.document(2)
        run = self.start(self.service, doc)
        self.assertTrue(self.gate.wait_entered(1))
        path = f"/api/runs/{run['id']}/cancel"
        for bad in ({}, {'expected_status': 'running', 'x': 1}, {'expected_status': 'cancelled'}, {'expected_status': ['running']}, {'status': 'running'}):
            self.assertEqual(self.call(path, data=bad)[0], 400, bad)
        self.assertEqual(self.call('/api/runs/' + '0' * 32 + '/cancel', data={'expected_status': 'running'})[0], 400)
        queued = self.start(self.service, self.document(1, 'second'))
        self.assertEqual(self.store.run(queued['id'])['status'], 'queued')
        code, body = self.call(f"/api/runs/{queued['id']}/cancel", data={'expected_status': 'queued'})
        self.assertEqual((code, body['cancel'], body['status'], body['detail']), (200, {'previous': 'queued'}, 'cancelled', C1))
        self.assertEqual(self.call(path, 'GET')[0], 404)
        self.assertEqual(self.call(path, data={'expected_status': 'running'}, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.store.run(run['id'])['status'], 'running')
        code, body = self.call(path, data={'expected_status': 'queued'})
        self.assertEqual(code, 409, body)
        code, body = self.call(path, data={'expected_status': 'running'})
        self.assertEqual((code, body['cancel'], body['status']), (200, {'previous': 'running'}, 'cancelling'))
        self.assertIn('usage', body)
        code, body = self.call(path, data={'expected_status': 'running'})
        self.assertEqual((code, body['error']), (409, '이미 취소를 요청한 실행입니다. 아직 멈추지 않았습니다.'))
        self.gate.release.set()
        self.service.pool.shutdown(wait=True)
        code, body = self.call(path, data={'expected_status': 'running'})
        self.assertEqual((code, body['error']), (409, '이미 끝난 실행이라 취소하지 않았습니다 (상태: 취소됨).'))
        self.assertEqual(self.call(f"/api/runs/{run['id']}", 'GET')[1]['status'], 'cancelled')


class Interface(unittest.TestCase):
    def run_js(self, script):
        return subprocess.check_output(['node'], input=script, text=True)

    def source(self):
        app = Path('verinote/web/app.js').read_text()
        lines = app.split('\n')
        helpers = '\n'.join(line for line in lines if line.startswith(('function notice', 'async function attempt', 'const labels')))
        progress = app[app.index('function renderProgress('):app.index('function bulkCandidates(')]
        dialog = app[app.index('let cancelTarget;'):app.index('async function loadCleanup(')]
        return helpers + '\n' + progress + '\n' + dialog

    def harness(self, body):
        return ("""const assert=require('node:assert/strict');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let uploadBusy=false,uploadTarget=null,documentRequest=1;let selectedDoc={id:'doc',version:1,latest_run:{id:'run'}};
const runWatchers=new Map(),timers=[];global.setTimeout=fn=>{timers.push(fn);return timers.length};global.clearTimeout=()=>{};
const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){},textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,open:false,showModal(){this.open=true},close(){this.open=false}});return elements.get(s)};
$('#library').hidden=false;$('#activity').hidden=true;
const panels=new Map();global.document={querySelector:s=>panels.get(s)||null};
function renderRun(r){return 'RUN:'+r.id+':'+r.status}
let libraryLoads=0;async function loadLibrary(){libraryLoads++}async function openDoc(){}
let api;
"""
                + self.source() + "\n(async()=>{\n" + body + "\nconsole.log('ok');\n})().catch(e=>{console.error(e);process.exitCode=1});\n")

    def test_button_dialog_notice_and_rerender(self):
        script = self.harness("""
 const run=(status,extra={})=>({id:'run',stage:'extract',status,detail:'',progress:{known:false,phase:status,percent:null},...extra});
 for(const status of ['queued','running'])assert(renderAnalysisProgress(run(status)).includes(`data-cancel-run="run" data-cancel-status="${status}"`),status);
 for(const status of ['cancelling','cancelled','success','error','interrupted'])assert(!renderAnalysisProgress(run(status)).includes('data-cancel-run'),status);
 assert(renderAnalysisProgress(run('cancelling')).includes('취소 중 · 허용한 호출이 끝나면 멈춤'));
 assert(renderAnalysisProgress(run('cancelled',{detail:'x'})).includes('failed'));
 openCancelRun('run','queued','extract');
 assert.equal($('#cancel-run-body').textContent,'대기 중인 실행은 모델을 호출하기 전에 취소됩니다. 그사이 처리가 시작되면 취소하지 않고 다시 확인을 요청합니다. 다른 실행과 질문은 계속됩니다.');
 assert($('#cancel-run-dialog').open);
 openCancelRun('run','running','ontology');
 assert($('#cancel-run-body').textContent.includes('이 실행은 사전 초안을 저장하지 않습니다.'));
 openCancelRun('run','running','extract');
 assert($('#cancel-run-body').textContent.includes('이 실행은 위키·관계 후보를 저장하지 않습니다.'));
 assert($('#cancel-run-body').textContent.includes('이미 허용한 호출은 중단하지 않으므로 끝날 때까지 기다린 뒤 멈추며, 그 호출의 비용은 줄지 않습니다.'));
 // the notice follows cancel.previous, not the status of the returned run
 const calls=[];
 api=async(path,method,data)=>{calls.push([path,method,data]);return {...run('cancelled'),cancel:{previous:'running'}}};
 const panel={outerHTML:''};panels.set('[data-run-panel="run"]',panel);
 await $('#confirm-cancel-run').onclick();
 assert.deepEqual(calls,[['/api/runs/run/cancel','POST',{expected_status:'running'}]]);
 assert.equal($('#notice').textContent,'취소를 요청했습니다. 이미 허용한 호출이 끝나면 멈춥니다.');
 assert.equal(panel.outerHTML,'RUN:run:cancelled');
 assert($('#document-progress').innerHTML.includes('취소됨'));
 assert(!$('#cancel-run-dialog').open);
 openCancelRun('run','queued','extract');
 calls.length=0;
 api=async(path,method,data)=>{calls.push([path,method,data]);return {...run('cancelled'),cancel:{previous:'queued'}}};
 await $('#confirm-cancel-run').onclick();
 assert.deepEqual(calls,[['/api/runs/run/cancel','POST',{expected_status:'queued'}]]);
 assert.equal($('#notice').textContent,'대기 중인 실행을 취소했습니다.');
 assert.equal(runWatchers.size,0);
 openCancelRun('run','running','extract');
 api=async()=>({...run('cancelling',{document_id:'doc',version:1}),cancel:{previous:'running'}});
 await $('#confirm-cancel-run').onclick();
 assert(runWatchers.has('run'));
""")
        self.assertIn('ok', self.run_js(script))

    def test_every_409_refetches_and_reopens_only_for_queued_that_is_now_running(self):
        script = self.harness("""
 const run=status=>({id:'run',stage:'ontology',status,detail:'',progress:{known:false,phase:status,percent:null}});
 const conflict=message=>{const e=Error(message);e.status=409;return e};
 const attempt409=async(opened,now,message)=>{
  const paths=[];
  api=async(path,method)=>{paths.push(method||'GET');if(method==='POST')throw conflict(message);return run(now)};
  openCancelRun('run',opened,'ontology');
  await $('#confirm-cancel-run').onclick();
  assert.deepEqual(paths,['POST','GET']);
 };
 await attempt409('queued','running','대기 중이던 실행이 처리 중으로 바뀌어 취소하지 않았습니다. 처리 중인 실행을 취소할지 다시 확인하세요.');
 assert($('#cancel-run-dialog').open);
 assert($('#cancel-run-body').textContent.includes('이 실행은 사전 초안을 저장하지 않습니다.'));
 assert.equal($('#cancel-run-error').textContent,'대기 중이던 실행이 처리 중으로 바뀌어 취소하지 않았습니다. 처리 중인 실행을 취소할지 다시 확인하세요.');
 $('#cancel-run-dialog').close();
 for(const [opened,now] of [['queued','cancelled'],['running','cancelling'],['running','success'],['queued','success']]){
  $('#notice').textContent='';
  await attempt409(opened,now,'이미 끝난 실행이라 취소하지 않았습니다.');
  assert(!$('#cancel-run-dialog').open,opened+now);
  assert.equal($('#notice').textContent,'이미 끝난 실행이라 취소하지 않았습니다.');assert.equal($('#notice').className,'error');
 }
 // a 400 stays in the dialog with the button usable
 api=async()=>{const e=Error('실행을 찾을 수 없습니다.');e.status=400;throw e};
 openCancelRun('run','queued','ontology');await $('#confirm-cancel-run').onclick();
 assert($('#cancel-run-dialog').open);assert.equal($('#cancel-run-error').textContent,'실행을 찾을 수 없습니다.');assert.equal($('#confirm-cancel-run').disabled,false);
""")
        self.assertIn('ok', self.run_js(script))

    def test_the_run_list_watches_every_run_that_can_still_change(self):
        app = Path('verinote/web/app.js').read_text()
        load_runs = app[app.index('async function loadRuns()'):app.index('function renderRun(')]
        script = ("const assert=require('node:assert/strict');const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',onclick:null});return elements.get(s)};\n"
                  "const esc=String,watched=[];const providerById=()=>null,downloadJSON=()=>{},renderUsage=()=>'',renderRun=r=>r.id,watchRun=id=>watched.push(id),loadCleanup=async()=>{};\n"
                  + load_runs + "\n"
                  "(async()=>{\n"
                  " const rows=['queued','running','cancelling','cancelled','success','error','interrupted'].map(status=>({id:status,status,document_id:'d',version:1}));\n"
                  " global.api=async path=>path==='/api/usage'?{included_jobs:0,total_jobs:0,truncated:false,summary:{},groups:[]}:rows;\n"
                  " await loadRuns();\n"
                  " assert.deepEqual(watched,['queued','running','cancelling']);\n"
                  " console.log('ok');\n"
                  "})().catch(e=>{console.error(e);process.exitCode=1});\n")
        self.assertIn('ok', self.run_js(script))

    def test_the_button_carries_the_stage_and_the_badge_labels_name_the_cancel_states(self):
        script = self.harness("""
 const run=(stage,status)=>({id:'run',stage,status,detail:'',progress:{}});
 assert(renderAnalysisProgress(run('ontology','queued')).includes('data-cancel-stage="ontology"'));
 assert(renderAnalysisProgress(run('extract','running')).includes('data-cancel-stage="extract"'));
 assert(renderAnalysisProgress(run('extract','cancelling')).includes('취소 중'));
 assert(renderAnalysisProgress(run('extract','cancelled')).includes('취소됨'));
 assert.equal(labels.cancelling,'취소 중');assert.equal(labels.cancelled,'취소됨');
""")
        self.assertIn('ok', self.run_js(script))

    def test_a_watcher_started_on_a_cancelling_run_reloads_the_library_when_it_ends(self):
        script = self.harness("""
 elements.set('#document-progress',{innerHTML:''});
 api=async()=>({id:'run',stage:'extract',status:'cancelled',progress:{known:false,phase:'cancelled',percent:null}});
 watchRun('run','doc',1,'cancelling');
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.equal(libraryLoads,1);assert.equal(runWatchers.size,0);
 libraryLoads=0;watchRun('run','doc',1,'success');
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.equal(libraryLoads,0);
""")
        self.assertIn('ok', self.run_js(script))

    def test_the_poller_continues_through_cancelling_and_stops_at_cancelled(self):
        script = self.harness("""
 elements.set('#document-progress',{innerHTML:''});
 let status='cancelling';api=async()=>({id:'run',stage:'extract',status,progress:{known:false,phase:status,percent:null}});
 watchRun('run','doc',1,'cancelling');
 await new Promise(r=>setImmediate(r));
 assert.equal(runWatchers.size,1);assert.equal(timers.length,1);assert($('#document-progress').innerHTML.includes('취소 중'));
 status='cancelled';await timers.shift()();await new Promise(r=>setImmediate(r));
 assert.equal(runWatchers.size,0);assert.equal(timers.length,0);
""")
        self.assertIn('ok', self.run_js(script))


if __name__ == '__main__':
    unittest.main()
