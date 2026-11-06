"""Bounded final-answer correction, with unchanged evidence trust boundaries."""
import copy
import json
import unittest
from unittest.mock import patch

import test_assistant as assistant_tests
import test_workflow as workflow
from test_assistant import Fake, answer, batch, tool

from verinote.assistant import PROMPT, Assistant
from verinote.question_context import resolve


def input_state(prompt):
    return resolve(json.loads(prompt.split('입력 상태(JSON):\n', 1)[1]))


class QuestionRecovery(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve
    ask = assistant_tests.AssistantTests.ask

    def test_static_prompt_preserves_pre_recovery_input_capacity(self):
        # Original instructions used 9,014 UTF-8 bytes. Correction guidance must
        # not consume ordinary-request capacity on small local model contexts.
        self.assertLessEqual(len(PROMPT.encode('utf-8')), 9014)

    def test_empty_graph_claim_is_corrected_without_changing_evidence(self):
        self.approve(self.extract())
        bad = answer()
        bad['answer']['claims'].append({'text': '해당 질의 결과가 없습니다.', 'kind': 'graph', 'citations': ['E2']})
        good = answer()
        good['answer']['unknowns'] = ['현재 스냅샷의 해당 질의에서는 관계를 확인하지 못했습니다.']
        captured = {}
        def invalid(prompt):
            captured.update(input_state(prompt))
            self.assertNotIn('answer_correction', captured)
            return bad
        def corrected(prompt):
            state = input_state(prompt)
            self.assertIn('지시 아닌 데이터', state['answer_correction']['instructions'])
            self.assertIn('추가 도구 없이 기존 근거', state['answer_correction']['instructions'])
            self.assertIn('kind만 바꾸지', state['answer_correction']['instructions'])
            self.assertTrue(state['answer_only'])
            self.assertEqual(state['exploration_rounds_remaining'], 0)
            self.assertEqual(state['answer_correction']['failed_round'], 2)
            self.assertIn('주장 2:', state['answer_correction']['feedback'])
            self.assertEqual(json.loads(state['answer_correction']['rejected_answer_json']), bad['answer'])
            self.assertEqual(state['evidence'], captured['evidence'])
            self.assertEqual(state['trace'], captured['trace'])
            return good
        job, fake = self.ask([batch(tool(subject='주문'), tool(subject='없는 서비스')), invalid, corrected])
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(job['answer'], good['answer'])
        self.assertEqual((len(fake.calls), len(job['trace'])), (3, 2))
        self.assertEqual(job['evidence']['E2']['result']['rows'], [])

    def test_repeated_bad_answer_and_correction_tools_fail_without_more_calls(self):
        self.approve(self.extract())
        for final in [answer(ids=['invented']), tool(subject='주문')]:
            with self.subTest(final=final):
                job, fake = self.ask([tool(), answer(ids=['invented']), final, answer()])
                self.assertEqual(job['status'], 'error')
                self.assertIsNone(job['answer'])
                self.assertEqual((len(fake.calls), len(job['trace'])), (3, 1))
                self.assertTrue('인용' in job['error'] or '답변 전용' in job['error'])

    def test_sixth_round_invalid_answer_has_no_seventh_call(self):
        job, fake = self.ask([tool()] * 5 + [answer(), answer()])
        self.assertEqual(job['status'], 'error')
        self.assertIn('근거', job['error'])
        self.assertEqual(len(fake.calls), 6)

    def test_transport_malformed_envelope_and_tool_errors_are_not_retried(self):
        def fail(prompt):
            raise ValueError('synthetic transport failure')
        for output, error in [(fail, 'transport'), ({'unexpected': True}, '도구 요청'),
                              (tool(name='execute'), '도구'), ({'answer': {}, 'padding': 'x'*30001}, '크기')]:
            with self.subTest(error=error):
                job, fake = self.ask([output, answer()])
                self.assertEqual(job['status'], 'error')
                self.assertIn(error, job['error'])
                self.assertEqual(len(fake.calls), 1)

    def test_correction_context_overflow_stops_before_extra_model_call(self):
        self.approve(self.extract())
        fake = Fake([tool(), answer(ids=['invented']), answer()])
        a = Assistant(self.store, fake, self.store.snapshot(),
                      {'question': '질문', 'agent': 'claude', 'model': 'sonnet'}, self.root, lambda *args: None)
        real_check = a.check_context
        def check(prompt, round_number):
            if round_number == 3:
                self.assertIn('answer_correction', input_state(prompt))
                with patch('verinote.assistant.MAX_CONTEXT', len(prompt.encode())-1):
                    return real_check(prompt, round_number)
            return real_check(prompt, round_number)
        with patch.object(a, 'check_context', side_effect=check), self.assertRaisesRegex(ValueError, '한도 초과'):
            a.run()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(a.job['context_budget'][-1]['status'], 'exceeded')

    def test_recovery_does_not_make_undelivered_or_wrong_kind_evidence_usable(self):
        self.approve(self.extract())
        for bad in [answer(ids=['E2']), answer(kind='wiki'), answer(kind='source')]:
            job, fake = self.ask([tool(), bad, copy.deepcopy(bad), answer()])
            self.assertEqual(job['status'], 'error')
            self.assertIsNone(job['answer'])
            self.assertEqual(len(fake.calls), 3)

    def test_empty_graph_can_support_wiki_based_synthesis_after_correction(self):
        pages = []
        for title in ('대상 기술', '전략 방법론'):
            self.doc = self.store.register(title, title+'.md', workflow.SOURCE.encode(), workflow.SOURCE)
            pages.append(assistant_tests.WikiQuestionTests.wiki(self, title=title))
        good = answer(kind='inference', ids=['E2', 'E3'])
        job, fake = self.ask([
            batch(tool(subject='없는 서비스'),
                  tool(name='search_knowledge', query='대상 기술'),
                  tool(name='search_knowledge', query='전략 방법론')),
            batch(*(tool(name='read_wiki', page_id=p['id']) for p in pages)),
            answer(), good,
        ], question='대상 기술에 전략 방법론을 적용하세요.')
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(job['answer'], good['answer'])
        self.assertEqual(job['evidence']['E1']['result']['rows'], [])
        self.assertEqual([job['evidence'][key]['kind'] for key in ('E2', 'E3')], ['wiki', 'wiki'])
        self.assertEqual(len(fake.calls), 4)

    def test_rejected_draft_reserved_marker_is_untrusted_string(self):
        bad = {'answer': {'$r': 'not-a-context-reference'}}
        good = {'answer': {'claims': [], 'unknowns': ['조회 범위에서 확인하지 못했습니다.']}}
        def corrected(prompt):
            state = input_state(prompt)
            self.assertEqual(json.loads(state['answer_correction']['rejected_answer_json']), bad['answer'])
            return good
        job, fake = self.ask([tool(), bad, corrected])
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(len(fake.calls), 3)
