import json
import threading
import unittest
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import SOURCE
from verinote.service import Service
from verinote.store import Store


def tool(name='query_graph', **args):
    return {'tools': [{'name': name, 'arguments': args}]}


def answer(kind='graph', ids=None):
    return {'answer': {'claims': [{'text': '근거 기반 답변', 'kind': kind, 'citations': ids or ['E1']}], 'unknowns': []}}


class Fake:
    def __init__(self, outputs):
        self.outputs, self.calls = iter(outputs), []

    def complete(self, agent, model, prompt, folder):
        self.calls.append((agent, model, prompt))
        output = next(self.outputs)
        return output(prompt) if callable(output) else output


class AssistantTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve
    def ask(self, outputs, **request):
        fake = Fake(outputs)
        service = Service(self.store, self.root, fake)
        job = service.ask({'question': '주문에서 원장까지 경로는?', **request})
        service.pool.shutdown(wait=True)
        return self.store.question(job['id']), fake

    def test_model_tool_result_and_provider_roundtrip(self):
        self.approve(self.extract())
        self.store.save_settings({'selected': 'codex', 'models': {'codex': 'chosen'}})
        job, fake = self.ask([tool(action='path', subject='주문', predicate='depends_on', target='원장'), answer()])
        self.assertEqual(job['status'], 'success')
        evidence = job['evidence']['E1']['result']
        self.assertEqual(evidence['rows'], [['주문','depends_on','원장']])
        self.assertEqual(len(evidence['proofs'][0]['fact_ids']), 2)
        self.assertIn('pyrewire', fake.calls[1][2])
        self.assertIn('E1', fake.calls[1][2])
        self.assertEqual(fake.calls[0][:2], ('codex','chosen'))
        self.assertEqual(Store(self.root/'state.sqlite3').question(job['id']), job)

    def test_upstream_empty_and_multiple_queries(self):
        self.approve(self.extract())
        job, _ = self.ask([tool(action='upstream', subject='원장', predicate='depends_on'), tool(subject='없는 서비스'), answer()])
        self.assertEqual(job['status'], 'success')
        self.assertEqual(len(job['evidence']['E1']['result']['rows']), 2)
        self.assertEqual(job['evidence']['E2']['result']['status'], 'empty')
        job, _ = self.ask([tool(subject='없는 서비스'), {'answer': {'claims': [], 'unknowns': ['현재 승인 스냅샷에서 확인할 수 없습니다.']}}])
        self.assertEqual(job['status'], 'success')

    def test_reject_invalid_model_contracts(self):
        self.approve(self.extract())
        cases = [[answer()], [tool(),answer(ids=['invented'])], [tool(),answer(kind='source')],
                 [tool(),tool()], [tool(name='execute', code='bad')], [tool(code='bad')],
                 [tool(subject='없는 서비스'),answer()], [tool(name='read_document', document_id='../secret')],
                 [tool(name='read_document',document_id=self.doc['id'],offset=-1)],
                 [tool(name='search_documents',query='주문'),answer(ids=['E1'])]]
        for outputs in cases:
            with self.subTest(outputs=outputs):
                job, _ = self.ask(outputs)
                self.assertEqual(job['status'], 'error')
                self.assertIsNone(job['answer'])

    def test_submission_pins_graph_search_and_source(self):
        self.approve(self.extract())
        entered, release = threading.Event(), threading.Event()
        def first(prompt):
            entered.set(); release.wait(5)
            return tool(name='search_documents', query='주문')
        fake = Fake([first, tool(name='read_document', document_id=self.doc['id']), tool(action='path',subject='주문',predicate='depends_on',target='원장'), answer(kind='source',ids=['E1'])])
        service = Service(self.store,self.root,fake)
        job = service.ask({'question':'주문의 원문과 경로'})
        self.assertTrue(entered.wait(3))
        fact = self.store.snapshot()['facts'][0]
        self.store.review(fact['id'],2,'pending','R','철회')
        self.store.register('운영','new.md',b'new','new',self.doc['id'],1)
        self.store.register('새 문서','x.md',SOURCE.encode(),SOURCE)
        release.set();service.pool.shutdown(wait=True)
        result = self.store.question(job['id'])
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['evidence']['E1']['text'],SOURCE)
        self.assertEqual(result['evidence']['E1']['version'],1)
        self.assertEqual(len(result['trace'][0]['result']['matches']),1)
        self.assertEqual(len(result['evidence']['E2']['result']['rows']),1)

    def test_pending_rejected_not_graph_evidence(self):
        items=self.extract()
        for item in items:
            self.store.review(item['id'],1,'rejected','R','test')
        job,_=self.ask([tool(),{'answer':{'claims':[],'unknowns':['승인된 결과 없음']}}])
        self.assertEqual(job['status'],'success')
        self.assertEqual(job['evidence']['E1']['result']['facts'],[])

    def test_budgets_and_engine_failure_preserve_trace(self):
        with patch('verinote.assistant.MAX_CONTEXT', 10):
            job,_=self.ask([])
            self.assertEqual(job['status'],'error')
        with patch('verinote.assistant.MAX_ROUNDS', 1):
            job,_=self.ask([tool()])
            self.assertEqual(job['status'],'error')
            self.assertEqual(job['trace'][0]['status'],'success')
        with patch('verinote.assistant.engine.query', side_effect=RuntimeError('native failed')):
            job,_=self.ask([tool()])
            self.assertEqual(job['trace'][0]['status'],'error')
            self.assertEqual(job['evidence'],{})
        job=self.store.create_question('q','agy','',self.store.snapshot()['id'])
        self.store.recover()
        self.assertEqual(self.store.question(job['id'])['status'],'interrupted')


class QuestionHTTP(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request
    def test_questions_api_and_bad_fields(self):
        from urllib.error import HTTPError
        self.service.cli=Fake([tool(),{'answer':{'claims':[],'unknowns':['자료 없음']}}])
        job=self.request('/api/questions','POST',{'question':'알려줘'})
        self.service.pool.shutdown(wait=True)
        result=self.request('/api/questions/'+job['id'])
        self.assertEqual(result['status'],'success')
        self.assertEqual(self.request('/api/questions')[0]['id'],job['id'])
        for data in [{'question':'q','code':'x'},{'question':'q','agent':[]},{'question':'q','snapshot':[]}]:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/questions','POST',data)
            self.assertEqual(error.exception.code,400)
            error.exception.close()
