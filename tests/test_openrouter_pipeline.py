"""Offline full HTTP pipeline, queued snapshots, recovery and caller verdicts."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_workflow import SOURCE, DICTIONARY, FACTS, extraction_response, approved_dictionary
from test_ontology import wire_dictionary
from test_assistant import tool, answer
from test_openrouter import Response, Connection
from verinote.agents import CLI
from verinote.config import Credentials
from verinote.openrouter import OpenRouter
from verinote.service import Service
from verinote.store import SCHEMA, Store, digest
from verinote.transports import Credentials as TransportCredentials, ExecutionOptions
from verinote.usage import job_calls

KEY = 'sk-or-pipeline-synthetic-707'


class Queue:
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args):
        self.tasks.append((fn, args))

    def drain(self):
        while self.tasks:
            fn, args = self.tasks.pop(0)
            fn(*args)


class Fixture:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []
        self.after = None

    def __call__(self, call):
        fixture = self
        class HTTP(Connection):
            def __init__(self):
                super().__init__(call, None, [])
            def request(self, *args, **kwargs):
                super().request(*args, **kwargs)
                body = json.loads(kwargs['body'])
                fixture.requests.append(body | {'timeout': call.deadline-call.clock()})
                value = next(fixture.outputs)
                if isinstance(value, Exception):
                    raise value
                output = value(body['messages'][0]['content']) if callable(value) else value
                payload = {'id': 'gen-pipeline-707', 'model': body['model'],
                           'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
                           'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
                                     'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}, 'cost': .1}}
                self.response = Response(json.dumps(payload).encode())
                if fixture.after:
                    fixture.after()
        return HTTP()


def dictionary(prompt):
    return wire_dictionary(DICTIONARY, prompt)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root/'state.sqlite3')
        self.doc = self.store.register('synthetic', 'synthetic.md', SOURCE.encode(), SOURCE)
        self.service = Service(self.store, self.root, credentials=Credentials(KEY))
        self.service.pool.shutdown()
        self.queue = Queue(); self.service.pool = self.queue

    def start(self, stage='ontology', **kwargs):
        return self.service.start(self.doc['id'], 'openrouter', kwargs.get('model','vendor/model'), stage, 1, 1)

    def approve(self):
        for item in self.store.document(self.doc['id'])['items']:
            if item['status'] == 'pending':
                self.store.review(item['id'], item['revision'], 'approved', 'tester', 'fixture')

    def calls(self, run):
        return job_calls(self.root, 'runs', self.store.run(run['id']))

    def test_dictionary_approval_extraction_question_and_secret_exclusion(self):
        fixture = Fixture([dictionary, extraction_response(), tool(action='path', subject='주문', predicate='depends_on', target='원장'), answer()])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            ontology = self.start(); self.queue.drain()
            self.assertEqual(self.store.run(ontology['id'])['status'], 'success')
            self.assertEqual(self.store.snapshot()['facts'], [])
            self.approve()
            extraction = self.start('extract'); self.queue.drain()
            self.assertEqual(self.store.run(extraction['id'])['status'], 'success')
            self.assertEqual(self.store.snapshot()['facts'], [])
            self.assertTrue(all(i['status']=='pending' for i in self.store.document(self.doc['id'])['items'] if i['kind']!='ontology'))
            self.approve()
            self.assertEqual(len(self.store.snapshot()['facts']), 2)
            job = self.service.ask({'question': '주문에서 원장 경로?', 'agent': 'openrouter', 'model': 'vendor/model'})
            self.queue.drain()
        saved = self.store.question(job['id']); self.assertEqual(saved['status'], 'success', saved['error'])
        self.assertEqual(saved['evidence']['E1']['result']['rows'], [['주문','depends_on','원장']])
        self.assertEqual(len(fixture.requests), 4)
        self.assertEqual(ontology['execution_options']['timeouts'], {'default':180,'ontology':600})
        self.assertTrue(599 < fixture.requests[0]['timeout'] <=600)
        self.assertEqual(extraction['execution_options']['timeouts']['default'], 360)
        self.assertTrue(359 < fixture.requests[1]['timeout'] <= 360)
        self.assertTrue(all(179 < r['timeout'] <=180 for r in fixture.requests[2:]))
        for owner in (ontology, extraction):
            self.assertEqual(self.calls(owner)[0]['caller_validation_status'], 'success')
        qcalls = job_calls(self.root, 'questions', saved)
        self.assertEqual(len(qcalls), 2); self.assertTrue(all(c['caller_validation_status']=='success' for c in qcalls))
        for path in self.root.rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(KEY.encode(), data); self.assertNotIn(digest(KEY.encode()).encode(), data)
        self.assertNotIn(KEY, json.dumps(saved))

    def test_registration_readiness_no_jobs_and_cli_without_key(self):
        service = Service(self.store, self.root)
        try:
            for model in ('vendor/model', '', ' ', 'bad\nmodel'):
                with self.assertRaises(ValueError):
                    service.start(self.doc['id'], 'openrouter', model, 'ontology', 1, 1)
                with self.assertRaises(ValueError):
                    service.ask({'question': 'question', 'agent':'openrouter', 'model':model})
            self.assertEqual(self.store.runs(), []); self.assertEqual(self.store.questions(), [])
            with self.assertRaises(ValueError):
                self.service.start(self.doc['id'], 'openrouter', KEY, 'ontology', 1, 1)
            saved_credentials = self.service.cli._credentials
            self.service.cli._credentials = TransportCredentials('bad\nkey')
            with self.assertRaises(ValueError):
                self.start()
            self.service.cli._credentials = saved_credentials
            self.assertEqual(self.store.runs(), [])
            class Fake:
                def complete(self, agent, model, prompt, folder, *, phase=None):
                    return wire_dictionary(DICTIONARY, prompt)
            cli_service = Service(self.store, self.root, Fake())
            run = cli_service.start(self.doc['id'], 'agy', '', 'ontology', 1, 1)
            cli_service.pool.shutdown()
            self.assertEqual(self.store.run(run['id'])['status'], 'success')
        finally:
            service.pool.shutdown()

    def test_queued_options_model_and_question_rounds_stay_pinned(self):
        approved_dictionary(self.store, self.doc['id'])
        fixture = Fixture([extraction_response(), tool(subject='주문'), {'answer': {'claims': [], 'unknowns': ['승인 사실 없음']}}])
        self.service.cli.options = ExecutionOptions(max_tokens=321, timeout=17)
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            run = self.start('extract', model='override/model')
            self.service.cli.options = ExecutionOptions(max_tokens=999, timeout=55)
            self.store.save_settings({'selected':'agy','models':{'openrouter':'changed/model'}})
            run['model'] = 'tampered-returned-object'
            self.queue.drain()
            self.assertEqual(fixture.requests[0]['model'], 'override/model')
            self.assertEqual(fixture.requests[0]['max_tokens'], 321)
            self.assertGreater(fixture.requests[0]['timeout'], 16)
            self.service.cli.options = ExecutionOptions(max_tokens=222, timeout=23)
            job = self.service.ask({'question':'question', 'agent':'openrouter', 'model':'question/model'})
            self.service.cli.options = ExecutionOptions(max_tokens=1000, timeout=99)
            fixture.after = lambda: setattr(self.service.cli, 'options', ExecutionOptions(max_tokens=2000,timeout=100))
            self.queue.drain()
        saved = self.store.question(job['id']); self.assertEqual(saved['status'], 'success', saved['error'])
        self.assertEqual([r['max_tokens'] for r in fixture.requests], [321,222,222])
        self.assertTrue(all(r['model']=='question/model' and 22 < r['timeout'] <= 23 for r in fixture.requests[1:]))

    def test_quote_repair_separate_failed_original_and_successful_repair(self):
        approved_dictionary(self.store, self.doc['id'])
        bad = extraction_response(); bad['facts'][0]['quote'] = '상기 '+FACTS[0]['quote']
        fixture = Fixture([bad, {'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            run = self.start('extract'); self.queue.drain()
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        calls = self.calls(run); self.assertEqual(len(calls),2)
        self.assertEqual([c['caller_validation_status'] for c in calls], ['failed','success'])
        self.assertTrue(all(c['transport_status']=='success' and c['reported_credits']==.1 for c in calls))

    def test_invalid_domain_and_tools_no_automatic_retry(self):
        approved_dictionary(self.store, self.doc['id'])
        invalid = extraction_response(); invalid['facts'][0]['predicate']='invented'
        fixture = Fixture([invalid, tool(name='execute', code='unsafe'), TimeoutError('fixture timeout')])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            run = self.start('extract'); self.queue.drain()
            self.assertEqual(self.store.run(run['id'])['status'],'error')
            call = self.calls(run)[0]
            self.assertEqual(call['transport_status'],'success'); self.assertEqual(call['caller_validation_status'],'failed')
            self.assertEqual(call['reported_credits'],.1)
            job = self.service.ask({'question':'q','agent':'openrouter','model':'vendor/model'}); self.queue.drain()
            saved = self.store.question(job['id']); self.assertEqual(saved['status'],'error')
            self.assertEqual(job_calls(self.root,'questions',saved)[0]['caller_validation_status'],'failed')
            run2 = self.start('extract'); self.queue.drain()
            self.assertEqual(self.store.run(run2['id'])['status'],'error')
        self.assertEqual(len(fixture.requests),3)

    def test_checkpoint_resume_and_option_mismatch(self):
        source = SOURCE.ljust(4000)+'B'*30
        self.doc = self.store.register('long','long.md',source.encode(),source)
        fixture = Fixture([dictionary, TimeoutError('timeout'), {'entities':[],'aliases':[],'relations':[]}])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            first = self.start(); self.queue.drain()
            self.assertEqual(self.store.run(first['id'])['status'],'error')
            second = self.start(); self.queue.drain()
            self.assertEqual(self.store.run(second['id'])['status'],'success')
        self.assertEqual(len(self.calls(second)),1)
        self.assertIn('재사용',self.store.run(second['id'])['detail'])
        checkpoint = json.loads((self.root/'runs'/first['id']/'dictionary-checkpoint.json').read_text())
        self.assertEqual(checkpoint['contract']['execution_options'],first['execution_options'])
        # New document, same model, changed output limit must begin at offset0.
        self.doc = self.store.register('long2','long2.md',source.encode(),source)
        fixture = Fixture([dictionary, TimeoutError('timeout'), dictionary, {'entities':[],'aliases':[],'relations':[]}])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            self.service.cli.options = ExecutionOptions(max_tokens=321)
            old = self.start(); self.queue.drain()
            self.service.cli.options = ExecutionOptions(max_tokens=322)
            new = self.start(); self.queue.drain()
        self.assertEqual(len(self.calls(new)),2)
        self.assertIn('실행 옵션이 다른',self.store.run(new['id'])['detail'])

    def test_question_round_limit_and_quote_repair_budget(self):
        fixture = Fixture([tool(subject='없는 서비스') for _ in range(6)])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            job = self.service.ask({'question':'q','agent':'openrouter','model':'model'})
            self.queue.drain()
        saved = self.store.question(job['id'])
        self.assertEqual(saved['status'],'error'); self.assertEqual(len(fixture.requests),6)
        self.assertEqual(len(saved['trace']),5)
        self.assertEqual(job_calls(self.root,'questions',saved)[-1]['caller_validation_status'],'failed')
        source = SOURCE.ljust(8000)*3
        self.doc = self.store.register('three','three.md',source.encode(),source)
        approved_dictionary(self.store,self.doc['id'])
        bad = extraction_response(); bad['facts'][0]['quote']='상기 '+FACTS[0]['quote']
        correction = {'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}
        fixture = Fixture([bad,correction,bad,correction,bad])
        self.service.cli.options = ExecutionOptions(max_tokens=333,timeout=31)
        fixture.after = lambda: setattr(self.service.cli,'options',ExecutionOptions(max_tokens=999,timeout=99))
        with patch('verinote.openrouter._HTTPS',side_effect=fixture):
            run = self.start('extract'); self.queue.drain()
        saved = self.store.run(run['id'])
        self.assertEqual(saved['status'],'error'); self.assertIn('한도',saved['detail'])
        self.assertEqual(len(fixture.requests),5)
        self.assertTrue(all(r['max_tokens']==333 and 30 < r['timeout'] <=31 for r in fixture.requests))
        self.assertEqual(len(self.calls(run)),5)
        self.assertEqual(len(self.store.document(self.doc['id'])['items']),1)

    def test_extraction_resume_caps_each_chunk_and_checks_http_options(self):
        for changed_options in (False, True):
            with self.subTest(changed_options=changed_options):
                source = SOURCE.ljust(8000) + SOURCE
                self.doc = self.store.register('extraction retry', 'retry.md', source.encode(), source)
                approved_dictionary(self.store, self.doc['id'])
                self.service.cli.options = ExecutionOptions(max_tokens=321, timeout=600)
                outputs = [extraction_response(), TimeoutError('fixture timeout'), extraction_response()]
                if changed_options:
                    outputs.append(extraction_response())
                fixture = Fixture(outputs)
                with patch('verinote.openrouter._HTTPS', side_effect=fixture):
                    first = self.start('extract'); self.queue.drain()
                    self.assertEqual(self.store.run(first['id'])['status'], 'error')
                    if changed_options:
                        self.service.cli.options = ExecutionOptions(max_tokens=322, timeout=600)
                    second = self.start('extract'); self.queue.drain()
                saved = self.store.run(second['id'])
                self.assertEqual(saved['status'], 'success', saved['detail'])
                self.assertEqual(len(self.calls(second)), 2 if changed_options else 1)
                offsets = [json.loads(request['messages'][0]['content'].split('입력(JSON):\n')[1])['offset']
                           for request in fixture.requests[2:]]
                self.assertEqual(offsets, [0, 8000] if changed_options else [8000])
                self.assertTrue(all(0 < request['timeout'] <= 360 for request in fixture.requests))
                self.assertEqual(saved['execution_options']['timeouts']['default'], 360)
                self.assertTrue(all(call['timeout_seconds'] <= 360 for call in self.calls(second)))
                self.assertEqual(self.store.snapshot()['facts'], [])

    def test_extraction_timeout_increase_keeps_validated_http_prefix(self):
        source = SOURCE.ljust(8000) + SOURCE
        self.doc = self.store.register('timeout change', 'retry.md', source.encode(), source)
        approved_dictionary(self.store, self.doc['id'])
        self.service.cli.options = ExecutionOptions(timeout=180)
        fixture = Fixture([extraction_response(), TimeoutError('timeout'), extraction_response()])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            first = self.start('extract'); self.queue.drain()
            self.assertEqual(self.store.run(first['id'])['status'], 'error')
            self.service.cli.options = None
            second = self.start('extract'); self.queue.drain()
        saved = self.store.run(second['id'])
        self.assertEqual(saved['status'], 'success', saved['detail'])
        self.assertEqual(len(self.calls(second)), 1)
        self.assertIn('1청크 재사용', saved['detail'])
        self.assertTrue(359 < fixture.requests[-1]['timeout'] <= 360)

    def test_contract_version_mismatch_and_complete_checkpoint_no_new_cost(self):
        source = SOURCE.ljust(4000)+'B'*30
        self.doc = self.store.register('version','version.md',source.encode(),source)
        fixture = Fixture([dictionary, TimeoutError('timeout'), dictionary, {'entities':[],'aliases':[],'relations':[]}])
        with patch('verinote.openrouter._HTTPS',side_effect=fixture):
            old = self.start(); self.queue.drain()
            path = self.root/'runs'/old['id']/'dictionary-checkpoint.json'
            saved = json.loads(path.read_text()); saved['contract']['execution_options']['adapter_contract_version']=0
            path.write_text(json.dumps(saved))
            new = self.start(); self.queue.drain()
        self.assertEqual(len(self.calls(new)),2)
        self.assertIn('실행 옵션이 다른',self.store.run(new['id'])['detail'])
        self.doc = self.store.register('complete','complete.md',SOURCE.encode(),SOURCE)
        fixture = Fixture([dictionary])
        with patch('verinote.openrouter._HTTPS',side_effect=fixture), patch.object(self.store,'finish_run',side_effect=OSError('fixture commit failure')):
            old = self.start(); self.queue.drain()
        with patch('verinote.openrouter._HTTPS') as transport:
            new = self.start(); self.queue.drain()
            transport.assert_not_called()
        self.assertEqual(self.store.run(new['id'])['status'],'success')
        self.assertEqual(self.calls(new),[])



class Migration(unittest.TestCase):
    def test_old_schema_repeated_upgrade_recovery_and_malformed_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root/'state.sqlite3'
            db = sqlite3.connect(path); db.executescript(SCHEMA)
            db.execute("INSERT INTO runs VALUES('legacy','doc',1,'agy','','queued','','date')"); db.commit()
            backup = sqlite3.connect(root/'before-upgrade.sqlite3'); db.backup(backup); backup.close(); db.close()
            store = Store(path); Store(path); store.recover()
            self.assertEqual(store.run('legacy')['execution_options'],None)
            self.assertEqual(store.run('legacy')['status'],'interrupted')
            restored = Store(root/'before-upgrade.sqlite3')
            self.assertEqual(restored.run('legacy')['status'],'queued')
            self.assertIsNone(restored.run('legacy')['execution_options'])
            with store.connect(True) as db:
                db.execute("INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at) VALUES('old-writer','doc',1,'agy','','success','','date')")
            self.assertIsNone(store.run('old-writer')['execution_options'])
            doc = store.register('test','s.md',SOURCE.encode(),SOURCE)
            service = Service(store,root,credentials=Credentials(KEY)); service.pool.shutdown(); queue=Queue(); service.pool=queue
            run=service.start(doc['id'],'openrouter','model','ontology',1,1)
            with store.connect(True) as db:
                db.execute('UPDATE runs SET execution_options=NULL WHERE id=?',(run['id'],))
            with patch('verinote.openrouter._HTTPS') as transport:
                service.extract(store.run(run['id']))
                transport.assert_not_called()
            self.assertEqual(store.run(run['id'])['status'],'error')
            snapshot = store.snapshot()
            job = store.create_question('q','openrouter','model',snapshot['id'], execution_options={
                'adapter_contract_version':1,'max_tokens':321,'timeouts':{'default':17,'ontology':600}})
            store.recover(); Store(path)
            self.assertEqual(store.question(job['id'])['status'],'interrupted')
            self.assertEqual(store.question(job['id'])['execution_options']['max_tokens'],321)
            bad = store.question(job['id']); bad['execution_options']={'max_tokens':0}
            self.assertTrue(service.slots.acquire(blocking=False))
            with patch('verinote.openrouter._HTTPS') as transport:
                service.answer(bad,snapshot)
                transport.assert_not_called()
            self.assertEqual(store.question(job['id'])['status'],'error')
            store.recover(); Store(path)
            self.assertEqual(store.run(run['id'])['execution_options'],None)
