import concurrent.futures
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from verinote.agents import CLI, PROVIDERS, command
from verinote.documents import decode_document
from verinote.engine import query
from verinote.server import Server
from verinote.service import Service, validate_fact
from verinote.store import Conflict, Store

SOURCE = '주문은 결제에 의존한다. 결제는 원장에 의존한다.'
FACTS = [dict(subject='주문', predicate='depends_on', object='결제', quote='주문은 결제에 의존한다.'), dict(subject='결제', predicate='depends_on', object='원장', quote='결제는 원장에 의존한다.')]
PAGE = {'title': '결제', 'claims': [{'heading': '구조', 'text': SOURCE, 'quote': SOURCE}]}


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.doc = self.store.register('운영', 'test.md', SOURCE.encode(), SOURCE)

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, facts=None):
        run = self.store.create_run(self.doc['id'], 'agy', 'test')
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], PAGE, FACTS if facts is None else facts)
        return self.store.document(self.doc['id'])['items']

    def approve(self, items):
        for item in items:
            self.store.review(item['id'], item['revision'], 'approved', 'Reviewer', '원문 대조 완료')

    def test_review_engine_proofs_revoke_and_replay(self):
        items = self.extract()
        self.assertEqual(query(self.store.snapshot())['rows'], [])
        self.approve(items)
        snapshot = self.store.snapshot()
        result = query(snapshot, 'path', '주문', 'depends_on', '원장')
        self.assertEqual(result['rows'], [['주문', 'depends_on', '원장']])
        path = result['proofs'][0]['fact_ids']
        facts = {f['id']: f for f in result['facts']}
        self.assertEqual([facts[i]['payload']['subject'] for i in path], ['주문', '결제'])
        self.assertEqual(result, query(snapshot, 'path', '주문', 'depends_on', '원장'))
        self.store.review(path[0], 2, 'pending', 'Reviewer', '재검토')
        self.assertEqual(query(self.store.snapshot(), 'path', '주문', 'depends_on', '원장')['rows'], [])
        self.assertEqual(result, query(self.store.snapshot(snapshot['id']), 'path', '주문', 'depends_on', '원장'))

    def test_immutable_document_versions_and_stale_upload(self):
        self.approve(self.extract())
        snapshot = self.store.snapshot()
        updated = self.store.register('운영', 'new.md', b'new version', 'new version', self.doc['id'], 1)
        self.assertEqual(updated['version'], 2)
        self.assertEqual(self.store.document(self.doc['id'], 1)['text'], SOURCE)
        self.assertEqual(self.store.raw(self.doc['id'], 1)[0], SOURCE.encode())
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.assertEqual(len(self.store.snapshot(snapshot['id'])['facts']), 2)
        with self.assertRaises(Conflict):
            self.store.register('운영', 'bad.md', b'bad', 'bad', self.doc['id'], 1)
        self.assertEqual(Store(self.root / 'state.sqlite3').document(self.doc['id'])['version'], 2)

    def test_concurrent_review_has_one_winner(self):
        item = self.extract()[0]
        gate = threading.Barrier(2)
        def review(action):
            gate.wait()
            try:
                Store(self.root / 'state.sqlite3').review(item['id'], 1, action, 'R', 'test')
                return 'success'
            except Conflict:
                return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(review, ['approved', 'rejected']))
        self.assertCountEqual(results, ['success', 'conflict'])
        self.assertEqual(len(self.store.history(item['id'])), 2)

    def test_fact_revision_invalidates_approval(self):
        item = next(i for i in self.extract() if i['kind'] == 'fact')
        self.approve([item])
        revised = item['payload'] | {'predicate': 'calls'}
        self.store.review(item['id'], 2, 'revise', 'R', '관계 교정', revised)
        self.assertEqual(self.store.snapshot()['facts'], [])
        history = self.store.history(item['id'])
        self.assertEqual(history[0]['payload']['predicate'], 'depends_on')
        self.assertEqual(history[-1]['payload']['predicate'], 'calls')
        with self.assertRaises(Conflict):
            self.store.review(item['id'], 2, 'approved', 'R', '오래된 화면')

    def test_cycles_and_predicates_do_not_mix(self):
        more = [FACTS[0], FACTS[1] | {'predicate': 'owned_by'}, FACTS[0] | {'subject':'결제', 'object':'주문'}]
        self.approve(self.extract(more))
        snap = self.store.snapshot()
        self.assertEqual(query(snap, 'path', '주문', 'depends_on', '원장')['rows'], [])
        cycle = query(snap, 'path', '주문', 'depends_on', '주문')
        self.assertEqual(len(cycle['proofs'][0]['fact_ids']), 2)

    def test_symbol_values_are_not_datalog_code(self):
        odd = '한글 " ); edge( injected \\ newline\n'
        fact = FACTS[0] | {'subject': odd}
        self.approve(self.extract([fact]))
        self.assertEqual(query(self.store.snapshot())['rows'], [[odd.strip(), 'depends_on', '결제']])

    def test_active_job_and_restart(self):
        run = self.store.create_run(self.doc['id'], 'agy', '')
        with self.assertRaises(Conflict):
            self.store.create_run(self.doc['id'], 'claude', '')
        self.store.recover()
        self.assertEqual(self.store.run(run['id'])['status'], 'interrupted')
        self.store.create_run(self.doc['id'], 'claude', '')

    def test_async_protocol_and_failed_quote_atomicity(self):
        class Fake:
            def complete(self, *args):
                return PAGE | {'facts': [FACTS[0] | {'quote': 'invented quote'}]}
        service = Service(self.store, self.root, Fake())
        run = service.start(self.doc['id'], 'codex', 'test-model')
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'error')
        self.assertEqual(self.store.document(self.doc['id'])['items'], [])
        self.assertEqual(self.store.document(self.doc['id'])['text'], SOURCE)

    def test_new_version_during_extraction_stays_historical(self):
        entered, release = threading.Event(), threading.Event()
        class Fake:
            def complete(self, *args):
                entered.set(); release.wait(5)
                return PAGE | {'facts': FACTS}
        service = Service(self.store, self.root, Fake())
        run = service.start(self.doc['id'], 'agy', '')
        self.assertTrue(entered.wait(3))
        self.store.register('new', 'new.md', b'new', 'new', self.doc['id'], 1)
        release.set(); service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'success')
        self.approve(self.store.document(self.doc['id'], 1)['items'])
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.assertEqual(self.store.document(self.doc['id'])['items'], [])


class ImportAndCLI(unittest.TestCase):
    def test_docx_and_unsupported_and_bad_quote(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Hello</w:t><w:tab/><w:t>World</w:t><w:br/><w:t>Again</w:t></w:r></w:p></w:document>')
        self.assertEqual(decode_document('a.docx', stream.getvalue()), 'Hello\tWorld\nAgain')
        for name, data in [('a.exe', b'a'), ('a.txt', b'\xff'), ('a.md', b'  ')]:
            with self.assertRaises(ValueError):
                decode_document(name, data)
        with self.assertRaises(ValueError):
            validate_fact(FACTS[0] | {'quote':'imaginary'}, SOURCE)

    def test_pdf_text_and_scanned_and_docx_expansion_limit(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(200, 200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 10 100 Td (Verinote PDF) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        raw = io.BytesIO(); writer.write(raw)
        self.assertIn('Verinote PDF', decode_document('source.pdf', raw.getvalue()))
        blank = PdfWriter(); blank.add_blank_page(200, 200)
        raw = io.BytesIO(); blank.write(raw)
        with self.assertRaisesRegex(ValueError, 'OCR'):
            decode_document('scan.pdf', raw.getvalue())
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('word/document.xml', 'a' * (16 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, '압축'):
            decode_document('large.docx', raw.getvalue())

    def test_all_agent_commands_and_model_is_one_argument(self):
        for agent in PROVIDERS:
            argv, stdin = command(agent, 'test-model', 'prompt; $(touch nope)')
            self.assertIn('test-model', argv)
            if agent == 'codex':
                self.assertEqual(argv[1], 'exec'); self.assertEqual(stdin, 'prompt; $(touch nope)')
                self.assertNotIn('-p', argv)
            else:
                self.assertEqual(argv[-2:], ['-p', 'prompt; $(touch nope)'])
        with self.assertRaises(ValueError):
            command('agy', '--dangerously-skip-permissions', 'prompt')

    def test_transport_success_error_timeout_and_missing_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root/'fake'
            for index, body, expected in [(0, "print('{\"ok\":true}')", None), (1, 'import sys; sys.exit(7)', ValueError), (2, 'import time; time.sleep(5)', TimeoutError), (3, "print('x'*20000)", ValueError)]:
                script.write_text('#!/usr/bin/env python3\n'+body+'\n'); script.chmod(0o755)
                with patch('verinote.agents.executable', return_value=str(script)):
                    if expected:
                        with self.assertRaises(expected):
                            CLI(timeout=.2,max_output=10000).complete('agy','', 'test', root/str(index))
                    else:
                        self.assertEqual(CLI().complete('agy','','test',root/str(index)), {'ok':True})
                self.assertTrue((root/str(index)/'run.json').exists())
            with patch('verinote.agents.executable', return_value=None), self.assertRaises(ValueError):
                CLI().complete('agy','','test',root/'missing')


class HTTP(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.store=Store(root/'db');self.service=Service(self.store,root)
        self.server=Server(('127.0.0.1',0),self.store,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.service.pool.shutdown();self.temp.cleanup()

    def request(self,path,method='GET',data=None,headers=None):
        req=Request(self.url+path,method=method,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        with urlopen(req) as r:
            return json.loads(r.read())

    def test_http_upload_review_query_version_diff(self):
        doc=self.request('/api/documents','POST',{'title':'test','filename':'test.md','text':SOURCE})
        run=self.store.create_run(doc['id'],'agy','');self.store.run_status(run['id'],'running');self.store.finish_run(run['id'],PAGE,FACTS)
        for i in self.store.document(doc['id'])['items']:
            self.request(f'/api/items/{i["id"]}/review','POST',{'revision':1,'action':'approved','actor':'Tester','reason':'verified'})
        r=self.request('/api/query','POST',{'action':'path','subject':'주문','predicate':'depends_on','target':'원장'})
        self.assertEqual(len(r['proofs'][0]['fact_ids']),2)
        self.request('/api/documents','POST',{'title':'test','filename':'test.md','text':'new','document_id':doc['id'],'expected_version':1})
        diff=self.request(f'/api/documents/{doc["id"]}/diff')['diff'];self.assertIn('+new',diff);self.assertIn('-'+SOURCE,diff)
        self.assertEqual(self.request('/api/query','POST',{})['rows'],[])
        self.assertEqual(self.request('/api/query','POST',{'snapshot':r['snapshot']})['rows'],[['결제','depends_on','원장'],['주문','depends_on','결제']])

    def test_settings_roundtrip_and_exact_pasted_original(self):
        models = {agent: 'chosen-model' for agent in PROVIDERS}
        self.request('/api/settings', 'PUT', {'selected': 'codex', 'models': models})
        self.assertEqual(self.request('/api/settings')['models'], models)
        source = '  original text\n\n'
        doc = self.request('/api/documents', 'POST', {'title': 'test', 'filename': 'x.md', 'text': source})
        self.assertEqual(self.store.raw(doc['id'], 1)[0], source.encode())
        self.assertEqual(self.request('/api/documents/' + doc['id'])['text'], source)

    def test_cross_origin_host_and_malformed_requests(self):
        for headers in [{'Origin':'https://evil.example'},{'Host':'evil.example'},{'Sec-Fetch-Site':'cross-site'}]:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/settings','PUT',{},headers)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/documents','POST',{'title':[], 'filename':'x.md'})
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        with urlopen(self.url) as response:
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
            self.assertIn(b'verinote',response.read())


if __name__=='__main__':
    unittest.main()
