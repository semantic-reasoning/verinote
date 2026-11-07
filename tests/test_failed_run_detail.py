"""Synthetic calls only: a failed run's detail against what the next run calls."""
import copy
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service
from verinote.usage import atomic_json

CONDITION = '문서 버전·승인 사전·에이전트·모델·병렬 설정 등 추출 조건이 이 실행과 같으면 다시 추출할 때 '
FROM_FIRST = f'{CONDITION}첫 청크부터 호출합니다.'


def from_chunk(number):
    return f'{CONDITION}청크 {number}부터 호출합니다.'


def number_of(folder):
    return int(folder.parent.name if folder.name == 'quote-repair' else folder.name)


class Sequential(CLI):
    def __init__(self, fn):
        super().__init__()
        self.fn, self.calls = fn, []

    def complete(self, agent, model, prompt, folder, *, phase=None):
        self.calls.append(number_of(folder))
        return copy.deepcopy(self.fn(number_of(folder), folder.name == 'quote-repair'))


class Parallel:
    def __init__(self, fn, capacity=2, timeout=10):
        self.extraction_spec = ExtractionSpec('/fake/claude', 'model', capacity, timeout, 1048576, 'null')
        self.fn, self.calls, self.lock = fn, [], threading.Lock()

    def limited_timeout(self, agent, seconds): return self

    def complete(self, agent, model, prompt, folder):
        with self.lock: self.calls.append(number_of(folder))
        return copy.deepcopy(self.fn(number_of(folder), folder.name == 'quote-repair'))

    def close(self): pass


def ok(index, repairing):
    return extraction_response()


def fails_at(number):
    def respond(index, repairing):
        if index == number: raise ValueError('stop')
        return extraction_response()
    return respond


def later_failure():
    """Chunk 2 fails once chunk 3 has started, so chunk 3 is already counted in the run's calls."""
    third = threading.Event()

    def respond(index, repairing):
        if index == 3: third.set()
        if index == 2:
            third.wait(5)
            raise ValueError('stop')
        return extraction_response()
    return respond


def once_at_third(action):
    """Runs action in the first call of chunk 3, then answers normally."""
    fired, lock = [], threading.Lock()

    def respond(index, repairing):
        with lock:
            if index == 3 and not fired:
                fired.append(index)
                action()
        return extraction_response()
    return respond


class Base(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, count=3):
        source = SOURCE.ljust(8000) * (count - 1) + SOURCE
        doc = self.store.register('detail', 'detail.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def current(self, doc):
        now = self.store.document(doc['id'])
        return now['version'], now['generation']

    def sequential(self, doc, fn):
        cli = Sequential(fn)
        service = Service(self.store, self.root, cli)
        version, generation = self.current(doc)
        run = service.start(doc['id'], 'copilot', 'chosen', expected_version=version, expected_generation=generation)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), cli.calls

    def parallel(self, doc, fn, capacity=2, timeout=10):
        caller = Parallel(fn, capacity, timeout)
        service = Service(self.store, self.root, caller, extraction_caller=caller)
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        run = service.start(doc['id'], 'claude', 'model', expected_version=version, expected_generation=generation, extraction_concurrency=capacity)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), caller.calls


def failing_checkpoint_writes(*failing):
    """failing: 1-based numbers of extraction-checkpoint.json writes that raise."""
    count = {'n': 0}

    def write(path, value):
        if Path(path).name == 'extraction-checkpoint.json':
            count['n'] += 1
            if count['n'] in failing: raise OSError('disk')
        atomic_json(path, value)
    return patch('verinote.extraction_checkpoint.atomic_json', side_effect=write)


class RetrySentence(Base):
    def assert_tail_and_next_calls(self, doc, run, tail, calls):
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['detail'].endswith(' · ' + tail), run['detail'])
        named = re.search(r'청크 (\d+)부터', tail)
        self.assertEqual(calls[:1], [int(named.group(1))] if named else [1] if '첫 청크부터' in tail else [], tail)
        final, called = self.sequential(doc, ok)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(called, calls)

    def test_saved_prefix_tail_names_next_chunk_and_next_run_calls_from_it(self):
        doc = self.document()
        run, _ = self.sequential(doc, fails_at(2))
        self.assert_tail_and_next_calls(doc, run, from_chunk(2), [2, 3])

    def test_no_validated_chunk_tail_says_first_chunk_and_next_run_calls_all(self):
        doc = self.document()
        run, _ = self.sequential(doc, fails_at(1))
        self.assert_tail_and_next_calls(doc, run, FROM_FIRST, [1, 2, 3])

    def test_every_checkpoint_write_raising_tail_says_first_chunk_despite_one_validated(self):
        doc = self.document()
        with failing_checkpoint_writes(1, 2, 3, 4):
            run, _ = self.sequential(doc, fails_at(2))
        self.assertIn('검증 완료 1/3청크', run['detail'])
        self.assertIn('복구 기록 저장 실패', run['detail'])
        self.assert_tail_and_next_calls(doc, run, FROM_FIRST, [1, 2, 3])

    def test_second_write_raising_tail_names_chunk_after_first_saved(self):
        doc = self.document(4)
        with failing_checkpoint_writes(2):
            run, _ = self.sequential(doc, fails_at(3))
        self.assertIn('검증 완료 2/4청크', run['detail'])
        self.assert_tail_and_next_calls(doc, run, from_chunk(2), [2, 3, 4])

    def test_first_write_raising_then_second_saved_tail_counts_the_later_save(self):
        doc = self.document(4)
        with failing_checkpoint_writes(1):
            run, _ = self.sequential(doc, fails_at(3))
        self.assert_tail_and_next_calls(doc, run, from_chunk(3), [3, 4])

    def test_resumed_run_with_writes_raising_tail_counts_the_record_it_resumed_from(self):
        doc = self.document(4)
        self.sequential(doc, fails_at(2))
        with failing_checkpoint_writes(1, 2, 3):
            run, _ = self.sequential(doc, fails_at(3))
        self.assertIn('검증 완료 2/4청크', run['detail'])
        self.assert_tail_and_next_calls(doc, run, from_chunk(2), [2, 3, 4])

    def test_checkpoint_over_size_limit_tail_names_chunk_after_the_last_saved_one(self):
        size_of = self.document(4)
        first, _ = self.sequential(size_of, fails_at(2))
        size = (self.root / 'runs' / first['id'] / 'extraction-checkpoint.json').stat().st_size
        doc = self.document(4)
        with patch('verinote.extraction_checkpoint.CHECKPOINT_LIMIT', size + 50):
            run, _ = self.sequential(doc, fails_at(3))
        self.assertIn('검증 완료 2/4청크', run['detail'])
        self.assertIn('복구 기록 저장 실패', run['detail'])
        self.assert_tail_and_next_calls(doc, run, from_chunk(2), [2, 3, 4])

    def test_progress_write_raising_alone_leaves_tail_on_the_saved_prefix(self):
        doc = self.document()

        def write(path, value):
            if Path(path).name == 'extraction-progress.json': raise OSError('disk')
            atomic_json(path, value)
        with patch('verinote.extraction_checkpoint.atomic_json', side_effect=write):
            run, _ = self.sequential(doc, fails_at(2))
        self.assert_tail_and_next_calls(doc, run, from_chunk(2), [2, 3])

    def test_publish_failure_with_all_saved_tail_says_no_new_call_and_next_run_calls_none(self):
        doc = self.document()
        with patch.object(self.store, 'finish_run', side_effect=ValueError('publish-down')):
            run, _ = self.sequential(doc, ok)
        self.assertEqual(run['detail'], f'publish-down · 검증 완료 3/3청크 · 새 호출 3회 · {CONDITION}새 호출 없이 저장을 다시 시도합니다.')
        final, called = self.sequential(doc, ok)
        self.assertEqual((final['status'], called), ('success', []))

    def test_publish_failure_with_last_write_raising_tail_names_the_unsaved_chunk(self):
        doc = self.document()
        with failing_checkpoint_writes(3), patch.object(self.store, 'finish_run', side_effect=ValueError('publish-down')):
            run, _ = self.sequential(doc, ok)
        self.assertIn('검증 완료 3/3청크', run['detail'])
        self.assertTrue(run['detail'].endswith(' · ' + from_chunk(3)), run['detail'])
        final, called = self.sequential(doc, ok)
        self.assertEqual((final['status'], called), ('success', [3]))

    def test_dictionary_reapproved_during_last_chunk_says_nothing_about_the_retry_and_next_run_calls_all(self):
        doc = self.document()

        def reapprove():
            item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
            self.store.review(item['id'], item['revision'], 'revise', 'R', 'change', DICTIONARY)
            item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
            self.store.review(item['id'], item['revision'], 'approved', 'R', 'approve')
        run, _ = self.sequential(doc, once_at_third(reapprove))
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['detail'], '추출 중 사전이 변경되거나 승인이 철회되었습니다. 다시 승인 후 추출하세요. · 검증 완료 3/3청크 · 새 호출 3회')
        final, called = self.sequential(doc, ok)
        self.assertEqual((final['status'], called), ('success', [1, 2, 3]))

    def test_document_version_uploaded_during_parallel_run_says_nothing_about_the_retry_and_next_run_calls_all(self):
        doc = self.document()

        def upload_version():
            version, generation = self.current(doc)
            self.store.register('detail', 'detail.md', b'z' * 20, 'z' * 20, document_id=doc['id'], expected_version=version, expected_generation=generation)
        run, _ = self.parallel(doc, once_at_third(upload_version))
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['detail'], '추출 중 문서 버전이 변경되었습니다. 다시 추출하세요. · 검증 완료 3/3청크 · 새 호출 3회')
        version, generation = self.current(doc)
        self.store.delete_document(doc['id'], True, version, generation)
        final, called = self.parallel(doc, ok)
        self.assertEqual((final['status'], sorted(called)), ('success', [1, 2, 3]))

    def test_condition_names_parallel_setting_and_next_run_at_changed_capacity_calls_all(self):
        doc = self.document()
        run, _ = self.parallel(doc, later_failure())
        self.assertEqual(run['detail'], f'청크 2 · stop · 검증 완료 1/3청크 · 새 호출 3회 · {from_chunk(2)}')
        final, called = self.parallel(doc, ok, capacity=1)
        self.assertEqual((final['status'], called), ('success', [1, 2, 3]))

    def test_one_worker_run_transport_timeout_change_next_run_calls_all(self):
        doc = self.document()
        run, _ = self.parallel(doc, fails_at(2), capacity=1)
        self.assertEqual(run['detail'], f'청크 2 · stop · 검증 완료 1/3청크 · 새 호출 2회 · {from_chunk(2)}')
        final, called = self.parallel(doc, ok, capacity=1, timeout=20)
        self.assertEqual((final['status'], called), ('success', [1, 2, 3]))

    def test_eight_later_failed_runs_at_another_capacity_evict_the_record_and_seven_do_not(self):
        for later, calls in ((7, [2, 3, 4]), (8, [1, 2, 3, 4])):
            with self.subTest(later=later):
                doc = self.document(4)
                self.parallel(doc, later_failure())
                for _ in range(later):
                    self.parallel(doc, fails_at(1), capacity=1)
                final, called = self.parallel(doc, ok)
                self.assertEqual((final['status'], sorted(called)), ('success', calls))

    def test_same_capacity_next_run_calls_from_named_chunk(self):
        doc = self.document()
        self.parallel(doc, later_failure())
        final, called = self.parallel(doc, ok)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertCountEqual(called, [2, 3])

    def test_transport_timeout_change_next_parallel_run_calls_all(self):
        doc = self.document()
        self.parallel(doc, later_failure())
        final, called = self.parallel(doc, ok, timeout=20)
        self.assertEqual((final['status'], sorted(called)), ('success', [1, 2, 3]))


if __name__ == '__main__':
    unittest.main()
