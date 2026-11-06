"""Whole-record request windows: bounded bytes, explicit omissions, no lost proof."""
import copy

# Integration exercises actual approved wiki reads, current-call eligibility,
# and the same CLI adapter budget used by production questions.
import json
import unittest
from unittest.mock import patch

import test_assistant as assistant_tests
import test_workflow as workflow
from test_assistant import answer, batch, deliver, fallback, tool
from test_question_budget import LimitedFake

from verinote.assistant import Assistant
from verinote.question_context import PROTOCOL, project, resolve
from verinote.question_window import WindowTooLarge, fit
from verinote.service import Service
from verinote.store import canonical, digest


def state():
    return {'snapshot': 'snapshot', 'question': '대상과 방법론을 비교하세요.', 'round': 3,
            'catalog': {'documents': [], 'entity_meanings': [], 'relation_meanings': []},
            'trace': [{'name': 'query_graph', 'arguments': {'subject': '대상'}, 'status': 'success', 'evidence_id': 'E1'}],
            'evidence': {'E1': {'kind': 'graph', 'result': {'rows': [['대상', '관련', '방법론']], 'query': {'subject': '대상'}}}}}


class QuestionWindow(unittest.TestCase):
    def test_full_state_and_exact_utf8_boundary_are_unchanged(self):
        raw = state(); before = copy.deepcopy(raw)
        prompt = PROTOCOL+'한국어'+canonical(project(raw).state)
        window = fit(raw, '한국어', len(prompt.encode()))
        self.assertEqual(window.prompt, prompt)
        self.assertEqual(window.report['selection'], 'full')
        self.assertEqual(resolve(window.projection.state), raw)
        self.assertEqual(raw, before)
        self.assertEqual(window.report['model_token_admission'], 'unknown')

    def test_catalog_overflow_fits_without_omitting_evidence(self):
        raw = state(); raw['catalog']['entity_meanings'] = '가' * 21000
        before = copy.deepcopy(raw)
        window = fit(raw, '', 60000)
        self.assertGreater(window.report['full_prompt_bytes'], 60000)
        self.assertLessEqual(len(window.prompt.encode()), 60000)
        self.assertEqual(window.report['omitted_evidence'], {})
        self.assertEqual(resolve(window.projection.state)['evidence'], raw['evidence'])
        self.assertEqual(window.report['omitted_catalog']['entity_meanings'], digest(raw['catalog']['entity_meanings']))
        self.assertEqual(raw, before)

    def large(self):
        raw = state()
        for i in (2, 3, 4):
            raw['evidence'][f'E{i}'] = {'kind': 'wiki', 'text': chr(0xAC00+i)*1800,
                                        'page_id': f'p{i}', 'sha256': str(i)*64}
        return raw

    def test_observed_60015_byte_shape_fits_without_raising_the_limit(self):
        raw = state()
        raw['catalog']['entity_meanings'] = 'x'
        size = len((PROTOCOL+canonical(project(raw).state)).encode())
        raw['catalog']['entity_meanings'] = 'x'*(60015-size+1)
        self.assertEqual(len((PROTOCOL+canonical(project(raw).state)).encode()), 60015)
        window = fit(raw, '', 60000)
        self.assertEqual(window.report['full_prompt_bytes'], 60015)
        self.assertLessEqual(len(window.prompt.encode()), 60000)
        self.assertEqual(resolve(window.projection.state)['evidence'], raw['evidence'])

    def test_requested_and_new_records_keep_full_payloads_and_hashes(self):
        raw = self.large(); before = copy.deepcopy(raw)
        window = fit(raw, '', 14000, requested=['E2'], fresh=['E4'])
        shown = resolve(window.projection.state)
        self.assertEqual(set(shown['evidence']), {'E1', 'E2', 'E4'})
        self.assertEqual(window.report['omitted_evidence']['E3']['sha256'], digest(raw['evidence']['E3']))
        for key in shown['evidence']:
            self.assertEqual(shown['evidence'][key], raw['evidence'][key])
        self.assertNotIn('E3', window.projection.evidence_ids)
        self.assertEqual(raw, before)
        self.assertEqual(fit(raw, '', 14000, requested=['E2'], fresh=['E4']), window)
        self.assertLessEqual(len(window.prompt.encode()), 14000)

    def test_lookup_omission_is_not_delivery_and_recall_restores_complete_result(self):
        raw = self.large()
        body = {'lookup_id': 'K1', 'matches': [{'id': 'p1', 'guidance': '가'*1800}], 'truncated': False}
        raw['trace'].append({'name': 'search_knowledge', 'status': 'success', 'result': body})
        window = fit(raw, '', 14000)
        self.assertNotIn('K1', window.projection.lookup_ids)
        self.assertEqual(window.report['omitted_lookups']['K1']['sha256'], digest(body))
        recalled = fit(raw, '', 14000, requested=['K1'])
        self.assertIn('K1', recalled.projection.lookup_ids)
        self.assertEqual(resolve(recalled.projection.state)['trace'][1]['result'], body)
        self.assertEqual(raw['trace'][1]['result'], body)

    def test_answer_only_preserves_recalled_lookup_or_reports_it_unavailable(self):
        raw = self.large()
        raw['answer_only'] = True
        body = {'lookup_id': 'K1', 'matches': ['x'*1000]}
        raw['trace'].append({'name': 'search_knowledge', 'status': 'success', 'result': body})
        window = fit(raw, '', 19000, requested=['K1'])
        self.assertIn('K1', window.projection.lookup_ids)
        self.assertEqual(resolve(window.projection.state)['trace'][1]['result'], body)
        body['matches'] = ['x'*20000]
        window = fit(raw, '', 5000, requested=['K1'])
        self.assertNotIn('K1', window.projection.lookup_ids)
        self.assertEqual(window.report['unavailable_requests'], ['K1'])
        self.assertIn('E1', window.projection.evidence_ids)

    def test_unfit_recall_is_explicit_and_leaves_usable_graph(self):
        raw = state(); raw['evidence']['E2'] = {'kind': 'wiki', 'text': '가'*9000}
        window = fit(raw, '', 5000, requested=['E2'])
        self.assertEqual(window.report['unavailable_requests'], ['E2'])
        self.assertEqual(window.projection.evidence_ids, {'E1'})
        self.assertNotIn('E2', resolve(window.projection.state)['evidence'])
        self.assertLessEqual(len(window.prompt.encode()), 5000)

    def test_larger_raw_graph_can_be_the_only_graph_that_fits_after_projection(self):
        raw = state()
        raw['evidence']['E1']['result']['proofs'] = ['가'*3000]
        raw['evidence']['E2'] = {'kind': 'graph', 'result': {'rows': [['a', 'p', 'b']], 'proofs': ['나'*100]*60}}
        window = fit(raw, '', 5500)
        self.assertEqual(window.projection.evidence_ids, {'E2'})
        self.assertEqual(resolve(window.projection.state)['evidence']['E2'], raw['evidence']['E2'])
        self.assertLessEqual(len(window.prompt.encode()), 5500)

    def test_irreducible_instruction_and_graph_do_not_fit(self):
        with self.assertRaises(WindowTooLarge):
            fit(state(), '명령'*1000, 100)
        raw = state(); raw['evidence']['E1']['result']['proofs'] = ['정확한 증거'*3000]
        with self.assertRaises(WindowTooLarge):
            fit(raw, '', 3000)



def input_state(prompt):
    return resolve(json.loads(prompt.split('입력 상태(JSON):\n', 1)[1]))


class WindowWorkflow(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    wiki = assistant_tests.WikiQuestionTests.wiki

    def ask(self, outputs, limit=18500):
        fake = LimitedFake(outputs, limit)
        service = Service(self.store, self.root, fake)
        self.addCleanup(service.close)
        job = service.ask({'question': '두 문서의 근거를 비교하세요.', 'agent': 'copilot', 'model': 'auto'})
        service.pool.shutdown(wait=True)
        return self.store.question(job['id']), fake

    def test_multi_round_eviction_and_recall_finish_with_real_wiki_citation(self):
        pages = []
        for title in ('기술', '방법론'):
            self.doc = self.store.register(title, title+'.md', workflow.SOURCE.encode(), workflow.SOURCE)
            pages.append(self.wiki(title=title, claims=[{'heading': title, 'text': title*400, 'quote': workflow.SOURCE} for _ in range(2)]))
        def recall(prompt):
            raw = input_state(prompt)
            self.assertNotIn('E2', raw['evidence'])
            self.assertIn('E3', raw['evidence'])
            self.assertIn('E2', raw['context_window']['omitted_evidence'])
            return tool(name='recall_context', id='E2')
        def final(prompt):
            raw = input_state(prompt)
            self.assertIn('E2', raw['evidence'])
            self.assertNotIn('E3', raw['evidence'])
            return answer(kind='wiki', ids=['E2'])
        job, fake = self.ask([
            batch(tool(), tool(name='search_knowledge', query='기술'), tool(name='search_knowledge', query='방법론')),
            batch(*(tool(name='read_wiki', page_id=page['id']) for page in pages)), recall, final,
        ], limit=21000)
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(len(job['trace']), 6)
        self.assertEqual(set(job['evidence']), {'E1', 'E2', 'E3'})
        self.assertIn('전체 근거', job['answer']['unknowns'][-1])
        self.assertTrue(all(len(prompt.encode()) <= 21000 for _, _, prompt in fake.calls))
        self.assertTrue(all(r['model_token_admission'] == 'unknown' for r in job['context_budget']))
        for key, value in job['context_budget'][-1]['omitted_evidence'].items():
            self.assertEqual(value['sha256'], digest(job['evidence'][key]))

    def prepared(self, outputs, limit=18500):
        fake = LimitedFake(outputs, limit)
        a = Assistant(self.store, fake, self.store.snapshot(),
                      {'question': '합성 질문', 'agent': 'copilot', 'model': 'auto'}, self.root, lambda *args: None)
        a.evidence = QuestionWindow().large()['evidence']
        a.trace = state()['trace']
        return a, fake

    def test_historical_delivery_does_not_authorize_omitted_citation(self):
        a, fake = self.prepared([answer(kind='wiki', ids=['E2'])]*2)
        a.delivered_evidence = set(a.evidence)
        with self.assertRaisesRegex(ValueError, '인용'):
            a.run()
        self.assertNotIn('E2', a.delivered_evidence)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn('E2', a.evidence)

    def test_recall_does_not_authorize_source_in_same_batch_and_can_repeat(self):
        page = self.wiki()
        a = Assistant(self.store, None, self.store.snapshot(), {}, self.root, lambda *args: None)
        a.execute(tool()['tools'][0])
        a.execute(tool(name='search_knowledge', query='요약', document_id=self.doc['id'])['tools'][0])
        a.execute(tool(name='read_wiki', page_id=page['id'])['tools'][0])
        deliver(a)
        a.delivered_lookups.clear()
        a.delivered_evidence.discard('E2')
        for key in ('K1', 'E2', 'K1'):
            a.execute(tool(name='recall_context', id=key)['tools'][0])
        self.assertEqual(len(a.trace), 6)
        self.assertEqual(set(a.evidence), {'E1', 'E2'})
        with self.assertRaisesRegex(ValueError, '검색 결과'):
            a.authorize_source(self.doc['id'], fallback('insufficient_wiki', ['E2']))
        a.delivered_lookups = {'K1'}
        with self.assertRaisesRegex(ValueError, '위키 읽기'):
            a.authorize_source(self.doc['id'], fallback('insufficient_wiki', ['E2']))
        deliver(a)
        self.assertEqual(a.authorize_source(self.doc['id'], fallback('insufficient_wiki', ['E2']))['wiki_evidence_ids'], ['E2'])

    def test_omitted_lookup_loses_earlier_authorization(self):
        page = self.wiki()
        a, fake = self.prepared([answer()])
        body = {'lookup_id': 'K1', 'matches': ['가'*5000], 'truncated': False}
        a.trace.append({'name': 'search_knowledge', 'status': 'success', 'result': body})
        a.lookups['K1'] = {'scope': [self.doc['id']], 'page_hits': {self.doc['id']: [page['id']]}, 'truncated': False}
        a.delivered_lookups = {'K1'}
        a.run()
        self.assertNotIn('K1', a.delivered_lookups)
        with self.assertRaisesRegex(ValueError, '검색 결과'):
            a.authorize_source(self.doc['id'], fallback('insufficient_wiki', ['E4']))
        self.assertEqual(len(fake.calls), 1)

    def test_repeated_recalls_consume_tool_budget_and_force_answer_only(self):
        def final(prompt):
            raw = input_state(prompt)
            self.assertTrue(raw['answer_only'])
            self.assertEqual(raw['tool_slots_remaining'], 0)
            return answer(kind='wiki', ids=['E2'])
        recall = tool(name='recall_context', id='E2')
        a, fake = self.prepared([batch(recall, recall, recall)]*3+[final])
        result = a.run()
        self.assertEqual(result['claims'][0]['citations'], ['E2'])
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(len(a.trace), 10)
        self.assertEqual(set(a.evidence), {'E1', 'E2', 'E3', 'E4'})

    def test_provider_failure_does_not_commit_new_delivery_set(self):
        def fail(prompt):
            self.assertNotIn('E2', input_state(prompt)['evidence'])
            raise ValueError('provider failure')
        a, fake = self.prepared([fail])
        a.delivered_evidence = {'E2'}
        a.delivered_lookups = {'K1'}
        with self.assertRaisesRegex(ValueError, 'provider failure'):
            a.run()
        self.assertEqual(a.delivered_evidence, {'E2'})
        self.assertEqual(a.delivered_lookups, {'K1'})
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_and_final_round_recalls_are_rejected(self):
        a, _ = self.prepared([])
        with self.assertRaisesRegex(ValueError, '저장된'):
            a.execute(tool(name='recall_context', id='E999')['tools'][0])
        a, fake = self.prepared([tool(name='recall_context', id='E2')])
        with patch('verinote.assistant.MAX_ROUNDS', 1), self.assertRaisesRegex(ValueError, '답변 전용'):
            a.run()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(a.trace), 1)
