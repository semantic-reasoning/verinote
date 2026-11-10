"""Synthetic calls only: exception text inside a failed or cancelled run's detail and a question's error (#894)."""
import copy
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import test_question_cancel as question_cancel
import test_workflow as workflow
from test_assistant import Fake as QuestionFake
from test_assistant import answer, tool
from test_quote_repair import broken
from test_workflow import FACTS, SOURCE, approved_dictionary, extraction_response

from verinote.service import Service
from verinote.store import Conflict

# Expected text is written out here, not imported from the code under test.
EMPTY = 'RuntimeError (오류 문구 없음)'
UNREADABLE = 'Unreadable (오류 문구를 읽을 수 없음)'
FROM_FIRST = '문서 버전·승인 사전·에이전트·모델·병렬 설정 등 추출 조건이 이 실행과 같으면 다시 추출할 때 첫 청크부터 호출합니다.'
FROM_TWO = '문서 버전·승인 사전·에이전트·모델·병렬 설정 등 추출 조건이 이 실행과 같으면 다시 추출할 때 청크 2부터 호출합니다.'
ONE_CALL = '검증 완료 0/1청크 · 새 호출 시도 1회'
TWO_CALLS = '검증 완료 0/1청크 · 새 호출 시도 2회'
REPAIR_FAILED = '청크 1 · 관계 1 인용: 인용 보정 실패 (청크당 1회).'
BASIS = '(관계 번호는 사전 불일치 제외 뒤 기준)'
CANCELLED = '처리 중에 취소되었습니다. 취소 요청 뒤로는 새 호출을 허용하지 않았고 위키·관계 후보는 저장하지 않았습니다. · 멈추기 전 마지막 오류:'
DICTIONARY_DONE = '검증 완료 0/27자 · 새 호출 시도 1회'
QUESTION_CANCELLED = '처리 중에 취소되었습니다. 취소 요청 뒤로는 새 모델 호출을 허용하지 않았고 답변은 저장하지 않았습니다. · 멈추기 전 마지막 오류:'
UNKNOWN_ENTITY = dict(subject='주문', predicate='depends_on', object='미지', quote='주문은 결제에 의존한다.')


class Unreadable(Exception):
    def __str__(self): raise RuntimeError('str')


def broken_with_exclusion():
    value = extraction_response(FACTS + [UNKNOWN_ENTITY])
    value['facts'][0]['quote'] = '상기 ' + FACTS[0]['quote']
    return value


class RunErrorDetail(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def extract(self, contract, outputs, chunks=1):
        """Runs one extraction; 'CANCEL' in outputs cancels the run inside that call, before the next output is used."""
        with patch('verinote.store.EXTRACTION_CONTRACT_VERSION', contract):
            source = SOURCE.ljust(8000) * (chunks - 1) + SOURCE
            doc = self.store.register('공백 테스트', 'synthetic.md', source.encode(), source)
            approved_dictionary(self.store, doc['id'])
            pending, started = iter(outputs), {}

            class Fake:
                def complete(self, agent, model, prompt, folder):
                    output = next(pending)
                    if output == 'CANCEL':
                        started['service'].cancel(started['run'], 'running')
                        output = next(pending)
                    if isinstance(output, BaseException): raise output
                    return copy.deepcopy(output)
            service = started['service'] = Service(self.store, self.root, Fake())
            run = service.start(doc['id'], 'copilot', 'chosen-model', expected_version=1, expected_generation=1)
            started['run'] = run['id']
            service.pool.shutdown(wait=True)
        run = self.store.run(run['id'])
        return run['status'], run['detail']

    def dictionary(self, error, prepare_error=None):
        doc = self.store.register('공백 테스트', 'synthetic.md', SOURCE.encode(), SOURCE)

        class Fake:
            def complete(self, agent, model, prompt, folder, **kwargs): raise error
        service = Service(self.store, self.root, Fake())
        failing = patch('verinote.dictionary_pipeline.prepare', side_effect=prepare_error) if prepare_error else nullcontext()
        with failing:
            run = service.start(doc['id'], 'agy', '', stage='ontology', expected_version=1, expected_generation=1)
            service.pool.shutdown(wait=True)
        run = self.store.run(run['id'])
        return run['status'], run['detail']

    # Relation extraction, the chunk call itself fails.
    def test_contract_0_empty_error_text_shows_the_type(self):
        self.assertEqual(self.extract(0, [RuntimeError()]), ('error', f'청크 1 · {EMPTY}'))

    def test_contract_0_error_text_ending_with_a_line_break_ends_without_it(self):
        self.assertEqual(self.extract(0, [ValueError('끝 줄\n')]), ('error', '청크 1 · 끝 줄'))

    def test_contract_2_empty_error_text_shows_the_type(self):
        self.assertEqual(self.extract(2, [RuntimeError()]), ('error', f'청크 1 · {EMPTY} · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_2_whitespace_only_error_text_shows_the_type(self):
        self.assertEqual(self.extract(2, [ValueError('  ')]),
                         ('error', f'청크 1 · ValueError (오류 문구 없음) · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_2_leading_space_of_the_error_text_is_dropped(self):
        self.assertEqual(self.extract(2, [ValueError(' 앞 공백')]), ('error', f'청크 1 · 앞 공백 · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_2_spacing_inside_the_error_text_is_kept(self):
        self.assertEqual(self.extract(2, [ValueError('안  두 칸')]), ('error', f'청크 1 · 안  두 칸 · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_3_empty_error_text_shows_the_type(self):
        self.assertEqual(self.extract(3, [RuntimeError()]), ('error', f'청크 1 · {EMPTY} · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_3_trailing_space_of_the_error_text_is_dropped(self):
        self.assertEqual(self.extract(3, [ValueError('끝 공백 ')]), ('error', f'청크 1 · 끝 공백 · {ONE_CALL} · {FROM_FIRST}'))

    def test_contract_3_ideographic_space_at_the_end_of_the_error_text_is_dropped(self):
        self.assertEqual(self.extract(3, [ValueError('끝\u3000')]), ('error', f'청크 1 · 끝 · {ONE_CALL} · {FROM_FIRST}'))

    # Quote repair call fails.
    def test_contract_2_quote_repair_failure_with_empty_error_text_shows_the_type(self):
        self.assertEqual(self.extract(2, [broken(), RuntimeError()]),
                         ('error', f'{REPAIR_FAILED} {EMPTY} · {TWO_CALLS} · {FROM_FIRST}'))

    def test_contract_2_quote_repair_failure_whose_error_text_cannot_be_read_shows_the_type(self):
        self.assertEqual(self.extract(2, [broken(), Unreadable()]),
                         ('error', f'{REPAIR_FAILED} {UNREADABLE} · {TWO_CALLS} · {FROM_FIRST}'))

    def test_contract_3_quote_repair_failure_after_an_exclusion_with_trailing_space_keeps_one_space_before_the_basis(self):
        self.assertEqual(self.extract(3, [broken_with_exclusion(), ValueError('끝 공백 ')]),
                         ('error', f'{REPAIR_FAILED} 끝 공백 {BASIS} · {TWO_CALLS} · {FROM_FIRST}'))

    def test_contract_3_quote_repair_failure_after_an_exclusion_with_empty_error_text_shows_the_type(self):
        self.assertEqual(self.extract(3, [broken_with_exclusion(), RuntimeError()]),
                         ('error', f'{REPAIR_FAILED} {EMPTY} {BASIS} · {TWO_CALLS} · {FROM_FIRST}'))

    # Cancelled while a call was running: the last error.
    def test_contract_2_cancel_with_empty_last_error_text_shows_the_type(self):
        self.assertEqual(self.extract(2, [extraction_response(), 'CANCEL', RuntimeError()], chunks=2),
                         ('cancelled', f'{CANCELLED} {EMPTY} · 검증 완료 1/2청크 · 새 호출 시도 2회 · {FROM_TWO}'))

    def test_contract_0_cancel_with_trailing_space_in_the_last_error_ends_without_it(self):
        self.assertEqual(self.extract(0, [extraction_response(), 'CANCEL', ValueError('끝 공백 ')], chunks=2),
                         ('cancelled', f'{CANCELLED} 끝 공백'))

    # Dictionary generation.
    def test_dictionary_empty_error_text_shows_the_type(self):
        self.assertEqual(self.dictionary(RuntimeError()), ('error', f'사전 구간 1/1 · {EMPTY} · {DICTIONARY_DONE}'))

    def test_dictionary_trailing_space_of_the_error_text_is_dropped(self):
        self.assertEqual(self.dictionary(ValueError('끝 공백 ')), ('error', f'사전 구간 1/1 · 끝 공백 · {DICTIONARY_DONE}'))

    def test_dictionary_error_whose_text_cannot_be_read_keeps_the_section_and_shows_the_type(self):
        self.assertEqual(self.dictionary(Unreadable()), ('error', f'사전 구간 1/1 · {UNREADABLE} · {DICTIONARY_DONE}'))

    def test_dictionary_failure_before_the_call_with_empty_error_text_shows_the_type(self):
        self.assertEqual(self.dictionary(AssertionError('no call'), prepare_error=RuntimeError()),
                         ('error', f'{EMPTY} · 검증 완료 0/27자 · 새 호출 시도 0회'))


class QuestionErrorDetail(question_cancel.Base):
    def test_question_failure_with_empty_error_text_shows_the_type(self):
        def raising(prompt): raise RuntimeError()
        service = Service(self.store, self.root, QuestionFake([raising]))
        self.addCleanup(service.close)
        job = service.ask({'question': question_cancel.QUESTION})
        service.pool.shutdown(wait=True)
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('error', EMPTY))

    def test_question_cancel_with_empty_last_error_text_shows_the_type(self):
        entered, release, fake = self.gated(tool(subject='주문'), [answer()] * 3)
        service = self.service(fake)
        job = service.ask({'question': question_cancel.QUESTION})
        self.assertTrue(entered.wait(5))
        service.cancel_question(job['id'], 'running')
        with patch.object(self.store, 'save_question', side_effect=Conflict()):
            release.set()
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelled', f'{QUESTION_CANCELLED} Conflict (오류 문구 없음)'))
