import json
import threading
import unittest
import unicodedata
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
        self.store.register('운영','new.md',b'new','new',self.doc['id'],1,1)
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

    def test_document_explanation_with_empty_graph_and_title_search(self):
        source = '【청구항 1】 온도에 따라 팬을 제어한다. <정정>【청구항 3】 작동 시간을 기록한다.'
        doc = self.store.register('보정서', 'claims.docx', source.encode(), source)
        def read_found(prompt):
            state = json.loads(prompt.split('입력 상태(JSON):\n', 1)[1])
            match = state['trace'][1]['result']['matches'][0]
            self.assertEqual(match['document_id'], doc['id'])
            self.assertEqual(match['matched_fields'], ['title'])
            self.assertEqual(match['offset_strategy'], 'start')
            self.assertFalse(state['trace'][1]['result']['citable'])
            self.assertEqual(state['evidence']['E1']['result']['rows'], [])
            return tool(name='read_document', document_id=match['document_id'], offset=match['offset'])
        def explain(prompt):
            state = json.loads(prompt.split('입력 상태(JSON):\n', 1)[1])
            evidence = state['evidence']['E2']
            self.assertEqual(evidence['text'], source)
            self.assertEqual(evidence['title'], '보정서')
            self.assertEqual(evidence['filename'], 'claims.docx')
            self.assertEqual(evidence['end_offset'], evidence['total_chars'])
            return {'answer': {'claims': [{'text': '등록한 문서는 팬 제어와 작동 시간 기록을 다룬다.', 'kind': 'source', 'citations': ['E2']}], 'unknowns': ['이 원문은 제목 용어의 일반 정의를 명시하지 않습니다.']}}
        job, _ = self.ask([tool(subject='보정서'), tool(name='search_documents',query='보정서'), read_found, explain], question='보정서가 무엇인가?')
        self.assertEqual(job['status'], 'success')
        self.assertEqual(job['evidence']['E2']['version'], 1)

    def test_normalized_metadata_and_body_offsets_remain_original(self):
        body = '앞부분 ß ' + unicodedata.normalize('NFD', '설명') + ' 끝'
        doc = self.store.register('제목', unicodedata.normalize('NFD', '보정서') + '.DOCX', body.encode(), body)
        from verinote.assistant import Assistant
        assistant = Assistant(self.store, None, self.store.snapshot(), {}, self.root, lambda *args: None)
        for term, field in [('보정서.docx','filename'), ('SS','text'), ('설명','text')]:
            assistant.execute({'name':'search_documents','arguments':{'query':term}})
            result = assistant.trace[-1]['result']
            match = next(m for m in result['matches'] if m['document_id']==doc['id'])
            self.assertIn(field, match['matched_fields'])
            self.assertEqual((match['offset'],match['offset_strategy']), (0,'start'))
        assistant.execute({'name':'search_documents','arguments':{'query':'끝'}})
        match=assistant.trace[-1]['result']['matches'][0]
        self.assertEqual(match['offset'],body.index('끝'))
        assistant.execute({'name':'read_document','arguments':{'document_id':doc['id'],'offset':match['offset']}})
        self.assertEqual(assistant.evidence['E1']['text'],'끝')
        self.assertEqual(assistant.evidence['E1']['total_chars'],len(body))
        # Ambiguous titles remain distinct navigation candidates, never merged evidence.
        second=self.store.register('제목','other.md',b'other','other')
        assistant=Assistant(self.store,None,self.store.snapshot(),{},self.root,lambda *args:None)
        assistant.execute({'name':'search_documents','arguments':{'query':'제목'}})
        self.assertEqual({m['document_id'] for m in assistant.trace[-1]['result']['matches']},{doc['id'],second['id']})

    def test_continuation_reads_later_source_and_search_is_not_evidence(self):
        source='가'*2050+'나중에 확인할 설명입니다.'
        doc=self.store.register('운영 지침','manual.md',source.encode(),source)
        def continuation(prompt):
            state=json.loads(prompt.split('입력 상태(JSON):\n',1)[1])
            evidence=state['evidence']['E2']
            self.assertEqual(evidence['text'],source[:2000])
            self.assertEqual(evidence['next_offset'],2000)
            self.assertEqual(evidence['total_chars'],len(source))
            return tool(name='read_document',document_id=doc['id'],offset=evidence['next_offset'])
        job,_=self.ask([tool(),tool(name='read_document',document_id=doc['id']),continuation,answer(kind='source',ids=['E3'])])
        self.assertEqual(job['status'],'success')
        self.assertEqual(job['evidence']['E3']['text'],source[2000:])
        self.assertIsNone(job['evidence']['E3']['next_offset'])
        job,_=self.ask([tool(),tool(name='search_documents',query='운영 지침'),answer(kind='source')])
        self.assertEqual(job['status'],'error')
        job,_=self.ask([tool(),tool(name='read_document',document_id=doc['id']),answer(kind='graph',ids=['E2'])])
        self.assertEqual(job['status'],'error')


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
