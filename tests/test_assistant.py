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


def fallback(reason='no_approved_wiki', ids=None, lookup_id='K1'):
    return {'lookup_id': lookup_id, 'reason': reason, 'detail': '승인 자료에 질문의 세부 내용이 없어 원문 확인', 'wiki_evidence_ids': ids or []}


def batch(*outputs):
    return {'tools': [call for output in outputs for call in output['tools']]}


def deliver(assistant):
    # Direct-dispatch tests explicitly simulate results delivered in a later prompt.
    assistant.delivered_evidence = set(assistant.evidence)
    assistant.delivered_lookups = set(assistant.lookups)


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
        self.approve([i for i in self.extract() if i['kind']=='fact'])
        entered, release = threading.Event(), threading.Event()
        def first(prompt):
            entered.set(); release.wait(5)
            return batch(tool(action='path',subject='주문',predicate='depends_on',target='원장'),
                         tool(name='search_knowledge',query='주문',document_id=self.doc['id']))
        fake = Fake([first, tool(name='search_documents',query='주문',mode='body',document_id=self.doc['id'],fallback=fallback()),
                     tool(name='read_document',document_id=self.doc['id'],fallback=fallback()), answer(kind='source',ids=['E2'])])
        service = Service(self.store,self.root,fake)
        job = service.ask({'question':'주문의 원문과 경로'})
        self.assertTrue(entered.wait(3))
        fact = self.store.snapshot()['facts'][0]
        self.store.review(fact['id'],2,'pending','R','철회')
        self.store.register('운영','new.md',b'new','new',self.doc['id'],1,1)
        self.store.register('새 문서','x.md',SOURCE.encode(),SOURCE)
        release.set();service.pool.shutdown(wait=True)
        result = self.store.question(job['id'])
        self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(result['evidence']['E2']['text'],SOURCE)
        self.assertEqual(result['evidence']['E2']['version'],1)
        self.assertEqual(len(result['trace'][2]['result']['matches']),1)
        self.assertEqual(len(result['evidence']['E1']['result']['rows']),1)

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
            match = state['trace'][2]['result']['matches'][0]
            self.assertEqual(match['document_id'], doc['id'])
            self.assertEqual(match['matched_fields'], ['title'])
            self.assertEqual(match['offset_strategy'], 'start')
            self.assertFalse(state['trace'][2]['result']['citable'])
            self.assertEqual(state['evidence']['E1']['result']['rows'], [])
            return tool(name='read_document', document_id=match['document_id'], offset=match['offset'],fallback=fallback())
        def explain(prompt):
            state = json.loads(prompt.split('입력 상태(JSON):\n', 1)[1])
            evidence = state['evidence']['E2']
            self.assertEqual(evidence['text'], source)
            self.assertEqual(evidence['title'], '보정서')
            self.assertEqual(evidence['filename'], 'claims.docx')
            self.assertEqual(evidence['end_offset'], evidence['total_chars'])
            return {'answer': {'claims': [{'text': '등록한 문서는 팬 제어와 작동 시간 기록을 다룬다.', 'kind': 'source', 'citations': ['E2']}], 'unknowns': ['이 원문은 제목 용어의 일반 정의를 명시하지 않습니다.']}}
        job, _ = self.ask([batch(tool(subject='보정서'),tool(name='search_knowledge',query='보정서',document_id=doc['id'])), tool(name='search_documents',query='보정서'), read_found, explain], question='보정서가 무엇인가?')
        self.assertEqual(job['status'], 'success')
        self.assertEqual(job['evidence']['E2']['version'], 1)

    def test_normalized_metadata_and_body_offsets_remain_original(self):
        body = '앞부분 ß ' + unicodedata.normalize('NFD', '설명') + ' 끝'
        doc = self.store.register('제목', unicodedata.normalize('NFD', '보정서') + '.DOCX', body.encode(), body)
        from verinote.assistant import Assistant
        assistant = Assistant(self.store, None, self.store.snapshot(), {}, self.root, lambda *args: None)
        assistant.execute(tool()['tools'][0])
        assistant.execute(tool(name='search_knowledge',query='설명',document_id=doc['id'])['tools'][0])
        deliver(assistant)
        for term, field in [('보정서.docx','filename'), ('SS','text'), ('설명','text')]:
            assistant.execute({'name':'search_documents','arguments':{'query':term,'mode':'body','document_id':doc['id'],'fallback':fallback()}})
            result = assistant.trace[-1]['result']
            match = next(m for m in result['matches'] if m['document_id']==doc['id'])
            self.assertIn(field, match['matched_fields'])
            self.assertEqual((match['offset'],match['offset_strategy']), (0,'start'))
        assistant.execute({'name':'search_documents','arguments':{'query':'끝','mode':'body','document_id':doc['id'],'fallback':fallback()}})
        match=assistant.trace[-1]['result']['matches'][0]
        self.assertEqual(match['offset'],body.index('끝'))
        assistant.execute({'name':'read_document','arguments':{'document_id':doc['id'],'offset':match['offset'],'fallback':fallback()}})
        self.assertEqual(assistant.evidence['E2']['text'],'끝')
        self.assertEqual(assistant.evidence['E2']['total_chars'],len(body))
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
            return tool(name='read_document',document_id=doc['id'],offset=evidence['next_offset'],fallback=fallback())
        job,_=self.ask([batch(tool(),tool(name='search_knowledge',query='운영 지침',document_id=doc['id'])),tool(name='read_document',document_id=doc['id'],fallback=fallback()),continuation,answer(kind='source',ids=['E3'])])
        self.assertEqual(job['status'],'success')
        self.assertEqual(job['evidence']['E3']['text'],source[2000:])
        self.assertIsNone(job['evidence']['E3']['next_offset'])
        job,_=self.ask([tool(),tool(name='search_documents',query='운영 지침'),answer(kind='source')])
        self.assertEqual(job['status'],'error')
        job,_=self.ask([batch(tool(),tool(name='search_knowledge',query='운영 지침',document_id=doc['id'])),tool(name='read_document',document_id=doc['id'],fallback=fallback()),answer(kind='graph',ids=['E2'])])
        self.assertEqual(job['status'],'error')

class WikiQuestionTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve
    ask = AssistantTests.ask

    def wiki(self, status='approved', title='요약', claims=None):
        run = workflow.legacy_run(self.store, self.doc['id'])
        self.store.run_status(run['id'], 'running')
        page = {'title': title, 'claims': claims or [{'heading':'구조','text':'승인된 요약 설명','quote':'주문은 결제에 의존한다.'}]}
        self.store.finish_run(run['id'], page, [])
        item = next(i for i in self.store.document(self.doc['id'])['items'] if i['run_id']==run['id'])
        if status != 'pending':
            self.store.review(item['id'], 1, status, 'R', '검토')
        return item

    def lookup(self, query='요약', document_id=None):
        return tool(name='search_knowledge',query=query,document_id=document_id or self.doc['id'])

    def test_wiki_answer_never_loads_or_sends_raw_body(self):
        # The approved quote is allowed, but unrelated raw text must not reach the model.
        source = SOURCE + ' RAW_ONLY_SENTINEL'
        self.doc = self.store.register('운영','raw.md',source.encode(),source,self.doc['id'],1,1)
        page = self.wiki()
        for status in ('pending','held','rejected'):
            self.wiki(status, title='요약 '+status)
        with patch.object(self.store,'source_text',side_effect=AssertionError('raw body read')), patch.object(self.store,'document',side_effect=AssertionError('full document read')):
            job, fake = self.ask([batch(tool(),self.lookup()),tool(name='read_wiki',page_id=page['id']),answer(kind='wiki',ids=['E2'])])
        self.assertEqual(job['status'],'success',job['error'])
        self.assertEqual(len(fake.calls),3)
        self.assertTrue(all('RAW_ONLY_SENTINEL' not in p for _,_,p in fake.calls))
        self.assertEqual([m['page_id'] for m in job['trace'][1]['result']['matches']], [page['id']])
        e=job['evidence']['E2'];self.assertEqual((e['revision'],e['version'],e['approval']), (2,2,'approved'))
        self.assertEqual(e['claim_indices'],[0]);self.assertFalse(e['partial'])

    def test_sufficient_graph_keeps_two_calls_no_source_fetch(self):
        self.approve(self.extract())
        with patch.object(self.store,'source_text',side_effect=AssertionError('raw')), patch.object(self.store,'document',side_effect=AssertionError('full')):
            job,fake=self.ask([tool(action='path',subject='주문',predicate='depends_on',target='원장'),answer()])
        self.assertEqual(job['status'],'success');self.assertEqual(len(fake.calls),2)

    def test_ontology_is_scoped_navigation_not_evidence(self):
        source=SOURCE+' 주문(order)'
        self.doc=self.store.register('운영','alias.md',source.encode(),source,self.doc['id'],1,1)
        dictionary=json.loads(json.dumps(workflow.DICTIONARY))
        dictionary['aliases']=[{'alias':'order','entity':'주문','quote':'주문(order)'}]
        item=workflow.approved_dictionary(self.store,self.doc['id'],dictionary)
        for kind in ('wiki','graph','inference'):
            job,_=self.ask([batch(tool(),self.lookup('order')),answer(kind=kind,ids=['K1'])])
            self.assertEqual(job['status'],'error')
            match=job['trace'][1]['result']['matches'][0]
            self.assertEqual(match['ontology_id'],item['id']);self.assertEqual(match['document_id'],self.doc['id'])
            self.assertEqual(match['guidance']['entity'],'주문')
            self.assertFalse(job['trace'][1]['result']['citable'])

    def test_wiki_citation_types_and_bounds(self):
        page=self.wiki(claims=[{'heading':'구조','text':'설명'*600,'quote':SOURCE} for _ in range(3)])
        def next_part(prompt):
            state=json.loads(prompt.split('입력 상태(JSON):\n',1)[1]);e=state['evidence']['E2']
            self.assertTrue(e['partial']);self.assertEqual(e['next_offset'],2000)
            return tool(name='read_wiki',page_id=page['id'],offset=e['next_offset'])
        job,_=self.ask([batch(tool(),self.lookup()),tool(name='read_wiki',page_id=page['id']),next_part,answer(kind='wiki',ids=['E3'])])
        self.assertEqual(job['status'],'success',job['error'])
        self.assertEqual(job['evidence']['E3']['offset'],2000)
        self.assertIsNone(job['evidence']['E3']['next_offset'])
        for kind,ids in [('source',['E2']),('graph',['E2']),('wiki',['E1'])]:
            job,_=self.ask([batch(tool(),self.lookup()),tool(name='read_wiki',page_id=page['id']),answer(kind=kind,ids=ids)])
            self.assertEqual(job['status'],'error')
        for offset in (-1,True,100000):
            job,_=self.ask([tool(name='read_wiki',page_id=page['id'],offset=offset)])
            self.assertEqual(job['status'],'error')

    def test_insufficient_wiki_fallback_requires_received_results(self):
        page=self.wiki()
        read=tool(name='read_wiki',page_id=page['id'])
        raw=tool(name='read_document',document_id=self.doc['id'],fallback=fallback('insufficient_wiki',['E2']))
        with patch.object(self.store,'source_text',wraps=self.store.source_text) as fetch:
            job,_=self.ask([batch(tool(),self.lookup()),batch(read,raw)])
            self.assertEqual(job['status'],'error');fetch.assert_not_called()
            self.assertEqual(job['trace'][-1]['status'],'error')
            self.assertIn('근거',job['error'])
            job,_=self.ask([batch(tool(),self.lookup()),read,raw,answer(kind='source',ids=['E3'])])
            self.assertEqual(job['status'],'success',job['error']);self.assertEqual(fetch.call_count,1)
        self.assertEqual(job['evidence']['E3']['fallback']['reason'],'insufficient_wiki')

    def test_early_body_read_search_and_fabricated_references_denied(self):
        cases=[tool(name='read_document',document_id=self.doc['id']),
               tool(name='read_document',document_id=self.doc['id'],fallback=fallback()),
               tool(name='search_documents',mode='body',query='주문',document_id=self.doc['id'],fallback=fallback()),
               batch(tool(),self.lookup(),tool(name='read_document',document_id=self.doc['id'],fallback=fallback()))]
        with patch.object(self.store,'source_text',side_effect=AssertionError('source must stay closed')):
            for request in cases:
                job,_=self.ask([request]);self.assertEqual(job['status'],'error');self.assertEqual(job['trace'][-1]['status'],'error')
            for f in [fallback(lookup_id='K999'),fallback('insufficient_wiki',['E1']),fallback()|{'detail':''}]:
                job,_=self.ask([batch(tool(),self.lookup()),tool(name='read_document',document_id=self.doc['id'],fallback=f)])
                self.assertEqual(job['status'],'error')

    def test_fallback_cannot_unlock_another_document_or_foreign_wiki(self):
        page=self.wiki()
        other=self.store.register('별도','other.md',SOURCE.encode(),SOURCE)
        with patch.object(self.store,'source_text',side_effect=AssertionError('raw')):
            job,_=self.ask([batch(tool(),self.lookup()),tool(name='read_document',document_id=other['id'],fallback=fallback())])
            self.assertEqual(job['status'],'error')
            job,_=self.ask([batch(tool(),self.lookup(document_id=other['id'])),tool(name='read_wiki',page_id=page['id']),tool(name='read_document',document_id=other['id'],fallback=fallback(ids=['E2']))])
            self.assertEqual(job['status'],'error')

    def test_no_match_and_no_wiki_fallback_are_distinct(self):
        self.wiki()
        with patch.object(self.store,'source_text',wraps=self.store.source_text) as fetch:
            job,_=self.ask([batch(tool(),self.lookup('검색결과없는단어')),tool(name='read_document',document_id=self.doc['id'],fallback=fallback())])
            self.assertEqual(job['status'],'error');fetch.assert_not_called()
            job,_=self.ask([batch(tool(),self.lookup('검색결과없는단어')),tool(name='read_document',document_id=self.doc['id'],fallback=fallback('no_match')),answer(kind='source',ids=['E2'])])
            self.assertEqual(job['status'],'success',job['error']);self.assertEqual(fetch.call_count,1)

    def test_truncated_search_cannot_authorize_source_and_can_continue(self):
        for _ in range(21):self.wiki()
        def more(prompt):
            state=json.loads(prompt.split('입력 상태(JSON):\n',1)[1]);r=state['trace'][1]['result']
            self.assertTrue(r['truncated']);self.assertEqual(r['next_offset'],20)
            return tool(name='search_knowledge',query='요약',document_id=self.doc['id'],offset=20)
        with patch.object(self.store,'source_text',side_effect=AssertionError('raw')):
            job,_=self.ask([batch(tool(),self.lookup()),more,tool(name='read_document',document_id=self.doc['id'],fallback=fallback('no_match',lookup_id='K2'))])
            self.assertEqual(job['status'],'error');self.assertTrue(job['trace'][2]['result']['truncated'])
            self.assertEqual(len(job['trace'][2]['result']['matches']),1)

    def test_inflight_wiki_dictionary_and_metadata_stay_pinned(self):
        page=self.wiki();ontology=workflow.approved_dictionary(self.store,self.doc['id'])
        entered,release=threading.Event(),threading.Event()
        def first(prompt):
            entered.set();release.wait(5);return batch(tool(),self.lookup('주문'))
        fake=Fake([first,tool(name='read_wiki',page_id=page['id']),answer(kind='wiki',ids=['E2'])])
        service=Service(self.store,self.root,fake)
        job=service.ask({'question':'주문 설명'});self.assertTrue(entered.wait(3))
        old=self.store.snapshot(job['snapshot'])
        self.store.review(page['id'],2,'pending','R','철회')
        self.store.review(ontology['id'],2,'pending','R','철회')
        self.store.register('새 제목','new.md',b'new','new',self.doc['id'],1,1)
        release.set();service.pool.shutdown(wait=True)
        result=self.store.question(job['id']);self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(self.store.snapshot(old['id']),old)
        self.assertNotEqual(self.store.snapshot()['id'],old['id'])
        self.assertEqual(result['evidence']['E2']['version'],1)
        self.assertTrue(any(m['kind']=='ontology' for m in result['trace'][1]['result']['matches']))
        state=json.loads(fake.calls[-1][2].split('입력 상태(JSON):\n',1)[1]);self.assertEqual(state['catalog']['documents'][0]['title'],'운영')

    def test_legacy_snapshot_never_borrows_current_wiki(self):
        from verinote.store import canonical,digest,now
        self.wiki()
        body={'facts':[],'documents':[{'id':self.doc['id'],'current_version':1}]}
        old={'id':digest(body),**body}
        with self.store.connect(True) as db:db.execute('INSERT INTO snapshots VALUES(?,?,?)',(old['id'],canonical(old),now()))
        with patch.object(self.store,'document',side_effect=AssertionError('full document')),patch.object(self.store,'source_text',wraps=self.store.source_text) as fetch:
            job,_=self.ask([batch(tool(),self.lookup()),tool(name='read_document',document_id=self.doc['id'],fallback=fallback('legacy_snapshot')),answer(kind='source',ids=['E2'])],snapshot=old['id'])
            self.assertEqual(job['status'],'success',job['error']);self.assertEqual(fetch.call_count,1)
        self.assertEqual(job['trace'][1]['result']['matches'],[])
        self.assertEqual(self.store.snapshot(old['id']),old)

    def test_knowledge_only_snapshot_changes_and_deletion(self):
        page=self.wiki('pending');first=self.store.snapshot()
        self.store.review(page['id'],1,'approved','R','승인');second=self.store.snapshot()
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(len(second['pages']),1)
        self.store.review(page['id'],2,'held','R','보류');third=self.store.snapshot()
        self.assertNotEqual(second['id'],third['id']);self.assertEqual(third['pages'],[])
        job=self.store.create_question('위키','agy','',second['id']);job.update(status='success');self.store.save_question(job)
        self.store.delete_document(self.doc['id'],False,1,1)
        for snap in (first,second,third):
            with self.assertRaises(ValueError):self.store.snapshot(snap['id'])
        with self.assertRaises(ValueError):self.store.question(job['id'])

    def test_ui_wiki_fallback_and_legacy_escaping(self):
        import subprocess
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const context=vm.createContext({esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),sourceLink:()=>'<a>원문</a>'});
vm.runInContext(source.slice(source.indexOf('function renderEvidence('),source.indexOf('function renderAnswer(')),context);
context.e={kind:'wiki',page_id:'page',revision:2,title:'<img>',text:'<script>',version:1,sha256:'sha',offset:0,end_offset:2000,total_chars:4000,partial:true,claim_indices:[0]};
let out=vm.runInContext('renderEvidence("E1",e)',context);assert(out.includes('승인 위키'));assert(out.includes('일부 구간'));assert(out.includes('r2'));assert(out.includes('&lt;script&gt;'));assert(!out.includes('<img>'));
context.e={kind:'source',text:'old',version:1};assert(vm.runInContext('renderEvidence("E1",e)',context).includes('미검토 원문'));
context.e.fallback={detail:'<bad>',reason:'no_match'};out=vm.runInContext('renderEvidence("E1",e)',context);assert(out.includes('&lt;bad&gt;'));assert(out.includes('원문 확인 이유'));
context.e={kind:'graph',result:{query:{action:'direct'},rows:[],facts:[],proofs:[],scope:'old'}};assert(vm.runInContext('renderEvidence("E1",e)',context).includes('Wirelog'));
'''
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)



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
