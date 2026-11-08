"""Synthetic calls only: a failed run's detail against what the next run calls."""
import copy
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_quote_exclusions import OFF_DICTIONARY
from test_quote_repair import broken
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    change_dictionary_behind_review,
    extraction_response,
)

from verinote.agents import CLI
from verinote.extraction_scheduler import ChunkScheduler
from verinote.extraction_timing import read_extraction_timing
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service, prepare_dictionary_prompt
from verinote.usage import atomic_json

CONDITION = '문서 버전·승인 사전·에이전트·모델·병렬 설정 등 추출 조건이 이 실행과 같으면 다시 추출할 때 '
FROM_FIRST = f'{CONDITION}첫 청크부터 호출합니다.'
COVERAGE = ('추출 점검 coverage: 승인 관계마다 정확히 한 행이 필요합니다. (승인 관계 1개, 응답 행 0개, 행이 없는 관계: depends_on) '
            '이 응답은 후보로 저장하지 않았습니다. 다시 추출하면 재사용할 복구 기록이 없는 청크를 새 모델 호출(비용이 들 수 있음)로 요청하며 같은 결과가 나올 수 있습니다.')


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


def no_coverage_at(number):
    def respond(index, repairing):
        value = extraction_response()
        if index == number: value['coverage'] = []
        return value
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
            change_dictionary_behind_review(self.store, item, revision=item['revision'] + 2, payload=DICTIONARY)  # revise then approve
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


class ChunkPrefix(Base):
    def test_sequential_validation_failure_full_detail(self):
        run, _ = self.sequential(self.document(), no_coverage_at(1))
        self.assertEqual(run['detail'], f'청크 1 · {COVERAGE} · 검증 완료 0/3청크 · 새 호출 1회 · {FROM_FIRST}')

    def test_sequential_repair_call_failure_full_detail(self):
        def respond(index, repairing):
            if index != 2: return extraction_response()
            if repairing: raise RuntimeError('repair-down')
            return broken()
        run, _ = self.sequential(self.document(), respond)
        self.assertEqual(run['detail'], f'청크 2 · 관계 1 인용: 인용 보정 실패 (청크당 1회). repair-down · 검증 완료 1/3청크 · 새 호출 3회 · {from_chunk(2)}')

    def test_parallel_validation_failure_at_second_chunk_full_detail(self):
        run, _ = self.parallel(self.document(), no_coverage_at(2))
        self.assertEqual(run['detail'], f'청크 2 · {COVERAGE} · 검증 완료 1/3청크 · 새 호출 3회 · {from_chunk(2)}')

    def test_parallel_worker_failure_at_second_with_first_in_flight_full_detail(self):
        failed = threading.Event()
        fail = ChunkScheduler.fail

        def recorded(scheduler, index, exc):
            result = fail(scheduler, index, exc)
            failed.set()
            return result

        def respond(index, repairing):
            if index == 2: raise ValueError('second')
            self.assertTrue(failed.wait(2))
            return extraction_response()
        with patch.object(ChunkScheduler, 'fail', recorded):
            run, calls = self.parallel(self.document(), respond)
        self.assertEqual(run['detail'], f'청크 2 · second · 검증 완료 0/3청크 · 새 호출 2회 · {FROM_FIRST}')
        self.assertCountEqual(calls, [1, 2])

    def prepare_raising_at(self, number, error=OSError):
        def prepare(cli, agent, prompt, payload, limit, folder):
            if folder.name == str(number): raise error('prepare-down')
            return prepare_dictionary_prompt(cli, agent, prompt, payload, limit, folder)
        return patch('verinote.service.prepare_dictionary_prompt', side_effect=prepare)

    def test_sequential_prepare_raising_names_that_chunk_not_the_previous(self):
        for error in (OSError, ValueError):
            with self.subTest(error=error.__name__), self.prepare_raising_at(2, error):
                run, _ = self.sequential(self.document(), ok)
                self.assertEqual(run['detail'], f'청크 2 · prepare-down · 검증 완료 1/3청크 · 새 호출 1회 · {from_chunk(2)}')

    def test_parallel_prepare_raising_names_that_chunk_not_a_neighbour_or_zero(self):
        for number in (1, 2, 3):
            with self.subTest(number=number), self.prepare_raising_at(number):
                run, _ = self.parallel(self.document(), ok)
                self.assertTrue(run['detail'].startswith(f'청크 {number} · prepare-down · 검증 완료 '), run['detail'])

    def repair_call_fails(self, exc, at=2):
        def respond(index, repairing):
            if index != at: return extraction_response()
            if repairing: raise exc
            return broken()
        return respond

    def test_repair_call_failure_detail_is_the_same_on_sequential_and_parallel_paths(self):
        run, _ = self.sequential(self.document(), self.repair_call_fails(RuntimeError('repair-down')))
        self.assertEqual(run['detail'], f'청크 2 · 관계 1 인용: 인용 보정 실패 (청크당 1회). repair-down · 검증 완료 1/3청크 · 새 호출 3회 · {from_chunk(2)}')
        for capacity, calls in ((1, 3), (2, 4)):
            with self.subTest(capacity=capacity):
                doc = self.document()
                run, _ = self.parallel(doc, self.repair_call_fails(RuntimeError('repair-down')), capacity=capacity)
                self.assertEqual(run['detail'], f'청크 2 · 관계 1 인용: 인용 보정 실패 (청크당 1회). repair-down · 검증 완료 1/3청크 · 새 호출 {calls}회 · {from_chunk(2)}')
                final, called = self.parallel(doc, ok, capacity=capacity)
                self.assertEqual((final['status'], sorted(called)), ('success', [2, 3]))

    def test_parallel_repair_timeout_keeps_the_worded_detail_and_the_timeout_code(self):
        run, _ = self.parallel(self.document(), self.repair_call_fails(TimeoutError('late')))
        self.assertIn('청크 2 · 관계 1 인용: 인용 보정 실패 (청크당 1회). late', run['detail'])
        report = read_extraction_timing(self.root, self.store, run['id'])
        self.assertEqual((report['available'], report['error_code']), (True, 'timeout'))

    def test_repair_call_failure_keeps_the_exclusion_numbering_note_on_both_paths(self):
        def respond(index, repairing):
            if repairing: raise RuntimeError('repair-down')
            value = extraction_response()
            value['facts'] = [OFF_DICTIONARY] + copy.deepcopy(value['facts'])
            value['facts'][1]['quote'] = '상기 ' + FACTS[0]['quote']
            return value
        for label, go in (('sequential', self.sequential), ('parallel', self.parallel)):
            with self.subTest(label):
                run, _ = go(self.document(), respond)
                self.assertIn('repair-down (관계 번호는 사전 불일치 제외 뒤 기준) ·', run['detail'])

    def test_a_later_chunk_failure_recorded_before_a_repair_failure_is_the_one_reported(self):
        third_failed = threading.Event()
        fail = ChunkScheduler.fail

        def recorded(scheduler, index, exc):
            result = fail(scheduler, index, exc)
            third_failed.set()
            return result

        def respond(index, repairing):
            if index == 3: raise ValueError('third-down')
            if index == 2 and repairing:
                self.assertTrue(third_failed.wait(5))
                raise RuntimeError('repair-down')
            return broken() if index == 2 else extraction_response()
        with patch.object(ChunkScheduler, 'fail', recorded):
            run, _ = self.parallel(self.document(), respond)
        self.assertEqual(run['detail'], f'청크 3 · third-down · 검증 완료 1/3청크 · 새 호출 3회 · {from_chunk(2)}')


if __name__ == '__main__':
    unittest.main()
