"""File→HTTP→pipeline→history/download integration, with no external network."""
import copy
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_assistant import answer, tool
from test_openrouter import Connection, Response, completion
from test_openrouter_pipeline import Fixture, Queue, dictionary
from test_workflow import DICTIONARY, FACTS, SOURCE, extraction_response

from verinote.agents import CLI
from verinote.config import create_config, load_config
from verinote.server import Server
from verinote.service import Service
from verinote.store import Store, digest
from verinote.transports import ExecutionOptions

KEY = 'sk-or-integration-synthetic-709'


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = create_config(self.root/'config.toml')
        self.config.write_text('[openrouter]\napi_key = '+json.dumps(KEY)+'\n')
        credentials = load_config(self.config)
        self.store = Store(self.root/'state.sqlite3')
        self.service = Service(self.store, self.root, credentials=credentials)
        self.service.pool.shutdown(); self.queue = Queue(); self.service.pool = self.queue
        self.server = Server(('127.0.0.1', 0), self.store, self.service, credentials=credentials)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.public = []
        # Any accidental network socket, including an unmocked provider, fails.
        real_connect = socket.socket.connect
        port = self.server.server_port
        def local_only(sock, address):
            if not isinstance(address, tuple) or address[:2] != ('127.0.0.1', port):
                raise AssertionError('external network forbidden in integration fixture')
            return real_connect(sock, address)
        self.network = patch('socket.socket.connect', local_only); self.network.start()
        self.addCleanup(self.network.stop)
        self.request('/api/settings', 'PUT', {'selected':'openrouter','models':{'openrouter':'vendor/default'}})

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, method='GET', data=None, *, raw=False):
        req = Request(self.url+path, method=method, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type':'application/json'})
        with urlopen(req, timeout=5) as response:
            body = response.read()
        self.public.append(body)
        return body if raw else json.loads(body)

    def upload(self, source=SOURCE):
        return self.request('/api/documents','POST',{'title':'synthetic','filename':'fixture.md','text':source})

    def start(self, doc, stage='ontology', **override):
        # UI reads defaults and sends them explicitly with the job request.
        settings = self.request('/api/settings')
        return self.request(f'/api/documents/{doc["id"]}/{stage}','POST',
                            {'agent':settings['selected'],'model':settings['models'][settings['selected']],
                             'expected_version':doc['version'],'expected_generation':doc['generation']} | override)

    def approve(self, doc):
        for item in self.request('/api/documents/'+doc['id'])['items']:
            if item['status'] == 'pending':
                self.request(f'/api/items/{item["id"]}/review','POST',
                             {'revision':item['revision'],'action':'approved','actor':'fixture','reason':'checked synthetic source'})
                self.request(f'/api/items/{item["id"]}/history')

    def finish(self, job):
        self.queue.drain()
        return self.request('/api/runs/'+job['id'])

    def audit(self):
        for body in self.public:
            self.assertNotIn(KEY.encode(), body); self.assertNotIn(digest(KEY.encode()).encode(), body)
            # Ordinary API/download JSON must not embed raw provider envelopes.
            self.assertNotIn(b'"choices"', body)
        for path in self.root.rglob('*'):
            if path.is_file() and path != self.config:
                body = path.read_bytes()
                self.assertNotIn(KEY.encode(), body, str(path))
                self.assertNotIn(digest(KEY.encode()).encode(), body, str(path))

    def test_file_settings_approval_quote_repair_question_and_download(self):
        doc = self.upload()
        bad = extraction_response(); bad['facts'][0]['quote'] = '상기 '+FACTS[0]['quote']
        fixture = Fixture([dictionary, bad, {'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]},
                           tool(action='path', subject='주문', predicate='depends_on', target='원장'), answer()])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            ontology = self.finish(self.start(doc))
            self.assertEqual(ontology['status'], 'success')
            self.assertEqual(self.store.snapshot()['facts'], [])
            self.approve(doc)
            extracted = self.finish(self.start(doc, 'extract'))
            self.assertEqual(extracted['status'], 'success', extracted['detail'])
            self.assertEqual(self.store.snapshot()['facts'], [])
            self.approve(doc)
            self.assertEqual(len(self.store.snapshot()['facts']), 2)
            job = self.request('/api/questions','POST',{'question':'주문에서 원장 경로?', 'agent':'openrouter','model':'vendor/default'})
            self.queue.drain()
        saved = self.request('/api/questions/'+job['id'])
        self.assertEqual(saved['status'],'success',saved['error'])
        self.assertEqual(saved['evidence']['E1']['result']['rows'],[['주문','depends_on','원장']])
        self.assertEqual(self.request(f'/api/documents/{doc["id"]}/raw',raw=True), SOURCE.encode())
        self.assertEqual(self.request('/api/questions')[0]['id'],job['id'])
        self.request('/api/runs'); usage=self.request('/api/usage')
        self.assertIn('summary',usage)
        self.assertEqual(len(fixture.requests),5)
        self.assertTrue(all(r['model']=='vendor/default' and r['max_tokens']==16384 for r in fixture.requests))
        self.assertTrue(599 < fixture.requests[0]['timeout'] <=600)
        self.assertTrue(all(359 < r['timeout'] <=360 for r in fixture.requests[1:3]))
        self.assertTrue(all(179 < r['timeout'] <=180 for r in fixture.requests[3:]))
        records = [json.loads(p.read_text()) for p in (self.root/'runs'/extracted['id']).rglob('run.json')]
        self.assertEqual(sorted(r['caller_validation_status'] for r in records), ['failed','success'])
        self.assertEqual(len({r['provider_call_id'] for r in records}),2)
        self.assertTrue(all(r['transport']=='http' and r['max_tokens']==16384 for r in records))
        # Same synthetic secret also must not reach a real CLI child environment.
        child = 'import os,json; print(json.dumps({"status":"SUCCESS","response":json.dumps({"key_present":"OPENROUTER_API_KEY" in os.environ})}))'
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':KEY}), \
             patch('verinote.agents.command',return_value=([sys.executable,'-c',child],'')), \
             patch('verinote.agents.executable',return_value=sys.executable):
            self.assertEqual(CLI().complete('agy','','synthetic',self.root/'cli-audit'),{'key_present':False})
        self.audit()

    def test_api_resume_pinning_and_mismatch_do_not_rebill_reuse(self):
        doc=self.upload(SOURCE.ljust(4000)+'B'*30)
        fixture=Fixture([dictionary, TimeoutError('synthetic timeout'), {'entities':[],'aliases':[],'relations':[]}])
        with patch('verinote.openrouter._HTTPS',side_effect=fixture):
            first=self.finish(self.start(doc)); self.assertEqual(first['status'],'error')
            queued=self.start(doc)
            self.request('/api/settings','PUT',{'selected':'codex','models':{'openrouter':'changed/model'}})
            self.service.cli.options=ExecutionOptions(max_tokens=7,timeout=1)
            second=self.finish(queued)
        self.assertEqual(second['status'],'success',second['detail'])
        self.assertIn('재사용',second['detail'])
        self.assertEqual(len(fixture.requests),3)
        self.assertTrue(all(r['model']=='vendor/default' and r['max_tokens']==16384 for r in fixture.requests))
        self.assertEqual(len(list((self.root/'runs'/second['id']).rglob('usage.json'))),1)
        self.assertEqual(second['usage']['details'][0]['reported_credits'],.1)
        self.request('/api/settings','PUT',{'selected':'openrouter','models':{'openrouter':'vendor/default'}})
        for mismatch in ('max_tokens','adapter_contract_version'):
            with self.subTest(mismatch=mismatch):
                doc=self.upload(SOURCE.ljust(4000)+'B'*30)
                self.service.cli.options=ExecutionOptions(max_tokens=321)
                fixture=Fixture([dictionary,TimeoutError('synthetic'),dictionary,{'entities':[],'aliases':[],'relations':[]}])
                with patch('verinote.openrouter._HTTPS',side_effect=fixture):
                    old=self.finish(self.start(doc))
                    if mismatch=='max_tokens': self.service.cli.options=ExecutionOptions(max_tokens=322)
                    else:
                        p=self.root/'runs'/old['id']/'dictionary-checkpoint.json'
                        c=json.loads(p.read_text()); c['contract']['execution_options']['adapter_contract_version']=0
                        p.write_text(json.dumps(c))
                    new=self.finish(self.start(doc))
                self.assertEqual(new['status'],'success'); self.assertIn('실행 옵션이 다른',new['detail'])
                self.assertEqual(len(list((self.root/'runs'/new['id']).rglob('usage.json'))),2)
        self.audit()

    def test_api_preflight_rejects_missing_model_and_key_without_jobs(self):
        doc=self.upload()
        with patch('verinote.openrouter._HTTPS') as outbound:
            for no_key in (False,True):
                if no_key: self.service.cli._credentials=None
                for model in ('', 'vendor/model') if no_key else ('',' '):
                    with self.assertRaises(HTTPError) as err: self.start(doc,model=model)
                    self.assertEqual(err.exception.code,400); err.exception.close()
                    with self.assertRaises(HTTPError) as err:
                        self.request('/api/questions','POST',{'question':'fixture','agent':'openrouter','model':model})
                    self.assertEqual(err.exception.code,400); err.exception.close()
            outbound.assert_not_called()
        self.assertEqual(self.request('/api/runs'),[]); self.assertEqual(self.request('/api/questions'),[])

    def test_api_failure_matrix_has_no_candidates_answers_or_secret_payload(self):
        doc=self.upload()
        with patch('verinote.openrouter._HTTPS',side_effect=Fixture([dictionary])):
            self.assertEqual(self.finish(self.start(doc))['status'],'success')
        self.approve(doc)
        invalid=extraction_response(); invalid['facts'][0]['predicate']='invented'
        cases=[('provider',completion(),None),('length',completion(),None),('domain',completion(json.dumps(invalid)),None),
               ('secret',completion(json.dumps({'echo':KEY})),None),('timeout',None,TimeoutError(KEY))]
        cases[0][1]['error']={'code':503,'message':'synthetic'}
        cases[1][1]['choices'][0]['finish_reason']='length'
        for name,payload,error in cases:
            with self.subTest(name=name):
                attempts=[]
                def factory(call):
                    return Connection(call,Response(json.dumps(payload).encode()),attempts,error=error)  # noqa: B023 (intentional: same-iteration payload, attempts, error)
                with patch('verinote.openrouter._HTTPS',side_effect=factory):
                    run=self.finish(self.start(doc,'extract'))
                    job=self.request('/api/questions','POST',{'question':'synthetic','agent':'openrouter','model':'vendor/default'})
                    self.queue.drain()
                self.assertEqual(len(attempts),2)  # one per explicit job, no automatic retry
                self.assertEqual(run['status'],'error')
                saved=self.request('/api/questions/'+job['id']); self.assertEqual(saved['status'],'error')
                self.assertFalse(saved['answer'])
                self.assertEqual(len(self.request('/api/documents/'+doc['id'])['items']),1)
                if name in ('secret','timeout'):
                    self.assertEqual(run['usage']['details'][0]['reported_credits'],None)
                    self.assertEqual(list((self.root/'runs'/run['id']).rglob('response.json')),[])
                else: self.assertEqual(run['usage']['details'][0]['reported_credits'],.01)
        self.request('/api/runs'); self.request('/api/questions'); self.request('/api/usage'); self.audit()

    def test_large_valid_dictionary_extraction_preserves_initial_cap(self):
        names=[f'service{i:03}' for i in range(180)]
        source=''.join(n+'.\n' for n in names)+SOURCE
        entities=[{'id':n,'name':n,'type':'Service','quote':n} for n in names]
        schema=copy.deepcopy(DICTIONARY); schema['entities']+=entities
        # 183 entities and 80 candidate facts: substantial valid boundary fixtures.
        facts=[{'subject':names[i], 'predicate':'depends_on', 'object':names[i+1], 'quote':names[i]+'.\n'+names[i+1]+'.'} for i in range(80)]
        doc=self.upload(source)
        def large_dictionary(prompt):
            spans=json.loads(prompt.split('입력(JSON):\n',1)[1])['sources']
            result=copy.deepcopy(schema)
            for rows in result.values():
                for row in rows:
                    quote=row.pop('quote')
                    choices=[(b-a,a,b) for a in range(len(spans)) for b in range(a,len(spans))
                             if quote in ''.join(s['text'] for s in spans[a:b+1])]
                    _,a,b=min(choices)
                    row['quote_ref']=[spans[a]['id'],spans[b]['id']]
            return result
        fixture=Fixture([large_dictionary,extraction_response(facts=facts,dictionary=schema)])
        with patch('verinote.openrouter._HTTPS',side_effect=fixture):
            ontology=self.finish(self.start(doc)); self.assertEqual(ontology['status'],'success',ontology['detail'])
            self.approve(doc); run=self.finish(self.start(doc,'extract'))
        self.assertEqual(run['status'],'success',run['detail'])
        self.assertEqual(len(fixture.requests),2)
        self.assertTrue(all(r['max_tokens']==16384 for r in fixture.requests))
        self.assertEqual(len(self.request('/api/documents/'+doc['id'])['items']),82)
        self.assertEqual(self.store.snapshot()['facts'],[])
        self.audit()
