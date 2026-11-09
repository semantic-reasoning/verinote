"""Synthetic calls only: review actions on a dictionary while a run on its document version is queued or running."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from itertools import chain, repeat
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_assistant import answer, tool
from test_ontology import wire_dictionary
from test_run_cancel import occupy_worker
from test_workflow import (
    DICTIONARY,
    EDITED,
    FACTS,
    PAGE,
    SOURCE,
    approved_dictionary,
    change_dictionary_behind_review,
    extraction_response,
)

from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, Store


class Gate:
    """Blocks every model call until released; answers by the stage of the run that owns the folder."""

    def __init__(self):
        self.entered, self.release, self.calls, self.stages, self.outputs = threading.Event(), threading.Event(), [], {}, None

    def complete(self, agent, model, prompt, folder, **kw):
        self.calls.append(str(folder))
        self.entered.set()
        if not self.release.wait(20): raise TimeoutError('gate')
        if self.outputs is not None:
            return next(self.outputs)
        return wire_dictionary(DICTIONARY, prompt) if any(run in str(folder) and stage == 'ontology' for run, stage in self.stages.items()) else extraction_response()


class ReviewDuringRun(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.gate = Gate()
        self.service = Service(self.store, self.root, self.gate)
        self.server = Server(('127.0.0.1', 0), Store(self.root / 'db'), self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.doc = self.store.register('d', 'x.md', SOURCE.encode(), SOURCE)

    def tearDown(self):
        self.gate.release.set()
        self.service.pool.shutdown(wait=True)
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def http(self, path, data):
        request = Request(f'http://127.0.0.1:{self.server.server_port}{path}', method='POST', data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error:
            with error: return error.code, json.loads(error.read())

    def dictionary(self, doc_id=None, version=None):
        return next(i for i in self.store.document(doc_id or self.doc['id'], version)['items'] if i['kind'] == 'ontology')

    def start(self, doc_id, stage, regenerate=None):
        current = self.store.document(doc_id)
        code, run = self.http(f'/api/documents/{doc_id}/{stage}', {'agent': 'agy', 'model': '', 'expected_version': current['version'], 'expected_generation': current['generation'], **({'regenerate': regenerate} if regenerate else {})})
        self.assertEqual(code, 202, run)
        self.gate.stages[run['id']] = stage
        return run

    def review(self, item, action, payload=None):
        return self.http(f"/api/items/{item['id']}/review", {'revision': item['revision'], 'action': action, 'actor': 'R', 'reason': 'x', **({'payload': payload} if payload else {})})

    def events(self):
        with self.store.connect() as db: return db.execute('SELECT count(*) FROM events').fetchone()[0]

    def finish(self):
        self.gate.release.set()
        self.service.pool.shutdown(wait=True)


class Matrix(unittest.TestCase):
    def check(self, stage, state, action):
        case = ReviewDuringRun('setUp'); case.setUp()
        try:
            item = approved_dictionary(case.store, case.doc['id'])
            regenerate = None
            if stage == 'ontology':
                case.store.review(item['id'], item['revision'], 'rejected', 'R', 'x')
                item = case.dictionary()
                regenerate = {'id': item['id'], 'revision': item['revision']}
            if state == 'queued':
                other = case.store.register('o', 'o.md', SOURCE.encode(), SOURCE)
                approved_dictionary(case.store, other['id'])
                case.start(other['id'], 'extract')
                self.assertTrue(case.gate.entered.wait(5))
                for i in range(2 if stage == 'ontology' else 1):
                    occupy_worker(case.service, case.gate.release, stage, key=f'review-reserve-{i}')
            run = case.start(case.doc['id'], stage, regenerate)
            if state == 'running': self.assertTrue(case.gate.entered.wait(5))
            self.assertEqual(case.store.run(run['id'])['status'], state)
            before = case.events()
            code, body = case.review(item, action, EDITED if action == 'revise' else None)
            self.assertEqual(code, 409, body)
            label = {'extract': '관계 추출', 'ontology': '사전 재생성'}[stage]
            self.assertIn(f"이 문서 버전의 {label} 실행 {run['id']}(agy / 기본 모델 · {dict(queued='대기 중', running='처리 중')[state]})", body['error'])
            self.assertIn('지금 바꾸면 호출 비용을 쓴 결과를 저장하지 못합니다.', body['error'])
            self.assertEqual('같은 단계의 실행 자리나 같은 문서의 이전 실행 정리를 기다립니다.' in body['error'], state == 'queued')
            self.assertNotIn('다른 문서의', body['error'])
            self.assertEqual('무엇을 기다리는지는 실행 기록에 표시됩니다. 기다리려면' in body['error'], state == 'queued')
            self.assertIn('기다리려면 실행 기록에서 끝난 것을 확인한 뒤 다시 시도하세요.', body['error'])
            self.assertIn('기다리지 않으려면 실행 기록에서 이 실행을 취소하세요. 취소하면 바로 다시 시도할 수 있고, 다른 실행과 질문은 계속됩니다.', body['error'])
            self.assertNotIn('서버를 재시작하세요', body['error'])
            result = '사전 초안을' if stage == 'ontology' else '위키·관계 후보를'
            self.assertEqual(f'처리 중이던 호출은 끝날 때까지 계속되지만 그 결과로 {result} 저장하지는 않습니다.' in body['error'], state == 'running')
            self.assertEqual(body['error'],
                             f"이 문서 버전의 {label} 실행 {run['id']}(agy / 기본 모델 · {dict(queued='대기 중', running='처리 중')[state]})이 끝나지 않아 사전을 바꿀 수 없습니다. "
                             f"지금 바꾸면 호출 비용을 쓴 결과를 저장하지 못합니다. {'대기 중인 실행은 같은 단계의 실행 자리나 같은 문서의 이전 실행 정리를 기다립니다. 무엇을 기다리는지는 실행 기록에 표시됩니다. ' if state == 'queued' else ''}"
                             '기다리려면 실행 기록에서 끝난 것을 확인한 뒤 다시 시도하세요. 기다리지 않으려면 실행 기록에서 이 실행을 취소하세요. 취소하면 바로 다시 시도할 수 있고, 다른 실행과 질문은 계속됩니다.'
                             + (f' 처리 중이던 호출은 끝날 때까지 계속되지만 그 결과로 {result} 저장하지는 않습니다.' if state == 'running' else ''))
            stale = case.review(item | {'revision': item['revision'] - 1}, action, EDITED if action == 'revise' else None)
            self.assertEqual(stale[0], 409)
            self.assertIn(run['id'], stale[1]['error'])  # the run is named before a stale revision is
            self.assertEqual(case.dictionary(), item)
            self.assertEqual(case.events(), before)
            case.finish()
            final = case.store.run(run['id'])
            self.assertEqual(final['status'], 'success', final['detail'])
            self.assertEqual(sum(run['id'] in call for call in case.gate.calls), 1)
            if stage == 'extract':
                self.assertEqual({i['kind'] for i in case.store.document(case.doc['id'])['items'] if i['run_id'] == run['id']}, {'page', 'fact'})
                self.assertEqual(case.review(case.dictionary(), 'held')[0], 200)  # the same dialog's revision is still good once the run ends
            else:
                self.assertEqual((case.dictionary()['run_id'], case.dictionary()['status']), (run['id'], 'pending'))
        finally:
            case.tearDown()

    def test_each_action_over_http_for_each_stage_and_state(self):
        for stage, actions in (('extract', ('revise', 'pending', 'rejected', 'held', 'approved')), ('ontology', ('revise', 'pending', 'approved', 'held', 'rejected'))):
            for state in ('queued', 'running'):
                for action in actions:
                    with self.subTest(stage=stage, state=state, action=action): self.check(stage, state, action)


class Scope(ReviewDuringRun):
    def test_fact_page_and_bulk_review_stay_open_during_a_run(self):
        approved_dictionary(self.store, self.doc['id'])
        self.gate.release.set()
        first = self.start(self.doc['id'], 'extract')
        self.service.pool.shutdown(wait=True)
        self.service = Service(self.store, self.root, self.gate); self.server.service = self.service
        self.assertEqual(self.store.run(first['id'])['status'], 'success')
        self.assertEqual(self.review(self.dictionary(), 'revise', EDITED)[0], 200)
        self.assertEqual(self.review(self.dictionary(), 'approved')[0], 200)
        self.gate.release.clear(); self.gate.entered.clear()
        run = self.start(self.doc['id'], 'extract')
        self.assertTrue(self.gate.entered.wait(5))
        items = [i for i in self.store.document(self.doc['id'])['items'] if i['run_id'] == first['id']]
        page = next(i for i in items if i['kind'] == 'page'); facts = [i for i in items if i['kind'] == 'fact']
        self.assertEqual(self.review(page, 'held')[0], 200)
        self.assertEqual(self.review(facts[0], 'revise', facts[0]['payload'])[0], 200)
        doc = self.store.document(self.doc['id'])
        code, body = self.http(f"/api/documents/{self.doc['id']}/approve-items", {'version': 1, 'expected_version': doc['version'], 'expected_generation': doc['generation'], 'items': [{'id': f['id'], 'revision': f['revision']} for f in facts[1:]], 'actor': 'R', 'reason': 'x'})
        self.assertEqual(code, 200, body)
        self.finish()
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_other_document_dictionary_at_the_same_version_stays_open_while_a_run_is_running_or_queued(self):
        other = self.store.register('o', 'o.md', SOURCE.encode(), SOURCE)
        approved_dictionary(self.store, self.doc['id'])
        idle = approved_dictionary(self.store, other['id'])
        third = self.store.register('t', 't.md', SOURCE.encode(), SOURCE)
        approved_dictionary(self.store, third['id'])
        running = self.start(third['id'], 'extract')
        self.assertTrue(self.gate.entered.wait(5))
        occupy_worker(self.service, self.gate.release)
        queued = self.start(self.doc['id'], 'extract')
        self.assertEqual((self.store.run(running['id'])['status'], self.store.run(queued['id'])['status']), ('running', 'queued'))
        self.assertEqual({d['version'] for d in (self.doc, other, third)}, {1})
        code, body = self.review(idle, 'held')
        self.assertEqual(code, 200, body)
        self.finish()
        self.assertEqual([self.store.run(r['id'])['status'] for r in (running, queued)], ['success', 'success'])

    def test_older_version_dictionary_stays_open_during_a_run_on_the_newer_version(self):
        old = approved_dictionary(self.store, self.doc['id'])
        self.store.register('d', 'x.md', SOURCE.encode(), SOURCE, self.doc['id'], 1, 1)
        approved_dictionary(self.store, self.doc['id'])
        run = self.start(self.doc['id'], 'extract')
        self.assertTrue(self.gate.entered.wait(5))
        self.assertEqual(self.review(old, 'pending')[0], 200)
        self.finish()
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_a_running_question_does_not_refuse_and_keeps_its_snapshot(self):
        item = approved_dictionary(self.store, self.doc['id'])
        run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], PAGE, FACTS)
        for row in self.store.document(self.doc['id'])['items']:
            if row['kind'] != 'ontology': self.store.review(row['id'], row['revision'], 'approved', 'R', 'x')
        self.gate.outputs = chain([tool(action='path', subject='주문', predicate='depends_on', target='원장')], repeat(answer()))
        code, job = self.http('/api/questions', {'question': '경로는?', 'agent': 'agy', 'model': ''})
        self.assertEqual(code, 202, job)
        self.assertTrue(self.gate.entered.wait(5))
        self.assertEqual(self.review(item, 'pending')[0], 200)
        self.finish()
        final = self.store.question(job['id'])
        self.assertEqual(final['status'], 'success', json.dumps({k: final[k] for k in ('error', 'trace', 'answer')}, ensure_ascii=False)[:1500] + str(len(self.gate.calls)))
        self.assertIn(item['id'], {o['id'] for o in self.store.snapshot(final['snapshot'])['ontologies']})

    def test_each_ended_status_leaves_the_dictionary_open(self):
        for status in ('success', 'error', 'interrupted'):
            with self.subTest(status=status):
                item = self.dictionary() if any(i['kind'] == 'ontology' for i in self.store.document(self.doc['id'])['items']) else approved_dictionary(self.store, self.doc['id'])
                if item['status'] != 'approved':
                    self.store.review(item['id'], item['revision'], 'approved', 'R', 'x'); item = self.dictionary()
                run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
                self.store.run_status(run['id'], status)
                self.assertEqual(self.review(item, 'held')[0], 200)


class Stale(ReviewDuringRun):
    def test_an_orphan_active_row_refuses_until_startup_recovery_ends_it(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                item = self.dictionary() if any(i['kind'] == 'ontology' for i in self.store.document(self.doc['id'])['items']) else approved_dictionary(self.store, self.doc['id'])
                if item['status'] != 'approved':
                    self.store.review(item['id'], item['revision'], 'approved', 'R', 'x'); item = self.dictionary()
                run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
                self.store.run_status(run['id'], status)
                self.assertEqual(self.review(item, 'held')[0], 409)
                Store(self.root / 'db').recover()  # what server.main runs before it serves
                self.assertEqual(self.store.run(run['id'])['status'], 'interrupted')
                self.assertEqual(self.review(item, 'held')[0], 200)


class Backstop(ReviewDuringRun):
    def test_finish_run_refuses_a_dictionary_changed_behind_the_guard(self):
        item = approved_dictionary(self.store, self.doc['id'])
        run = self.start(self.doc['id'], 'extract')
        self.assertTrue(self.gate.entered.wait(5))
        change_dictionary_behind_review(self.store, item, status='pending', revision=item['revision'] + 1)
        self.finish()
        final = self.store.run(run['id'])
        self.assertEqual(final['status'], 'error')
        self.assertIn('추출 중 사전이 변경되거나 승인이 철회되었습니다', final['detail'])
        self.assertEqual([i['kind'] for i in self.store.document(self.doc['id'])['items']], ['ontology'])  # no candidates stored

    def test_extract_publication_refuses_each_dictionary_field_changed_alone(self):
        item = approved_dictionary(self.store, self.doc['id'])
        for field, value in (('status', 'held'), ('revision', item['revision'] + 1), ('payload', EDITED)):
            with self.subTest(field=field):
                run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
                self.store.run_status(run['id'], 'running')
                change_dictionary_behind_review(self.store, item, **{field: value})
                with self.assertRaisesRegex(Conflict, '추출 중 사전이 변경되거나 승인이 철회되었습니다'):
                    self.store.finish_run(run['id'], PAGE, FACTS)
                self.store.run_status(run['id'], 'error')
                change_dictionary_behind_review(self.store, item, status='approved', revision=item['revision'], payload=DICTIONARY)

    def test_regeneration_publication_refuses_each_dictionary_field_changed_alone(self):
        item = approved_dictionary(self.store, self.doc['id'])
        self.store.review(item['id'], item['revision'], 'rejected', 'R', 'x')
        item = self.dictionary()
        for field, value in (('status', 'pending'), ('revision', item['revision'] + 1), ('payload', EDITED), ('run_id', 'another-run')):
            with self.subTest(field=field):
                run = self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1, regenerate={'id': item['id'], 'revision': item['revision']})
                self.store.run_status(run['id'], 'running')
                change_dictionary_behind_review(self.store, item, **{field: value})
                with self.assertRaisesRegex(Conflict, '재생성 중 사전이 변경되었습니다'):
                    self.store.finish_run(run['id'], dictionary=DICTIONARY)
                self.store.run_status(run['id'], 'error')
                change_dictionary_behind_review(self.store, item, status='rejected', revision=item['revision'], payload=item['payload'], run_id=item['run_id'])


class TwoConnections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / 'db'
        self.first, self.second = Store(path), Store(path)
        self.doc = self.first.register('d', 'x.md', SOURCE.encode(), SOURCE)
        self.item = approved_dictionary(self.first, self.doc['id'])

    def tearDown(self):
        self.temp.cleanup()

    def wait_on_the_write_lock(self, fn):
        out = {}

        def body():
            try: out['value'] = fn()
            except Exception as exc: out['error'] = exc
        worker = threading.Thread(target=body)
        worker.start()
        worker.join(0.3)
        self.assertTrue(worker.is_alive(), out)  # BEGIN IMMEDIATE is waiting on the other connection
        return worker, out

    def test_review_waiting_on_an_uncommitted_run_is_refused_once_it_commits(self):
        run_id = 'run-in-flight'
        with self.first.connect(True) as db:
            db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES(?,?,1,?,?,?,?,?,?)', (run_id, self.doc['id'], 'agy', '', 'queued', '', '2026', 'extract'))
            worker, out = self.wait_on_the_write_lock(lambda: self.second.review(self.item['id'], self.item['revision'], 'pending', 'R', 'x'))
        worker.join(5)
        self.assertIsInstance(out.get('error'), Conflict, out)
        self.assertIn(run_id, str(out['error']))
        current = next(i for i in self.first.document(self.doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual((current['status'], current['revision']), ('approved', self.item['revision']))

    def test_run_start_waiting_on_an_uncommitted_withdrawal_is_refused_once_it_commits(self):
        with self.first.connect(True) as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (self.item['id'],)).fetchone()
            self.first._review_item(db, row, self.item['revision'], 'pending', 'R', 'x')
            worker, out = self.wait_on_the_write_lock(lambda: self.second.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1))
        worker.join(5)
        self.assertIsInstance(out.get('error'), Conflict, out)
        self.assertIn('먼저 승인', str(out['error']))
        with self.first.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0], 0)


class UI(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_dialog_warns_on_open_and_keeps_the_typed_json_on_refusal(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const elements=new Map(),tasks=[];
function el(s){if(!elements.has(s))elements.set(s,{hidden:false,textContent:'',innerHTML:'',elements:{actor:{value:'R'},reason:{value:'x'},dictionary:{value:'{}'}},reset(){},showModal(){this.open=true;},close(){this.open=false;}});return elements.get(s);}
const dictionary={id:'o',revision:2,status:'approved',kind:'ontology',payload:{}},fact={id:'f',revision:1,status:'pending',kind:'fact',payload:{subject:'',predicate:'',object:'',quote:''}};
const doc={id:'a',version:1,items:[dictionary,fact],latest_run:{id:'r',status:'running',version:1}};
const context=vm.createContext({console,$:el,selectedDoc:doc,JSON,labels:{},esc:String,attempt:fn=>{const t=fn();tasks.push(t);return t;},openDoc:async()=>{},notice(){},api:async()=>{throw new Error('refused by the server');}});
vm.runInContext('let reviewTarget=null;',context);
vm.runInContext(source.slice(source.indexOf('function renderLookupSpellingProblems('),source.indexOf('function renderItem(')),context);
vm.runInContext(source.slice(source.indexOf('function openReview('),source.indexOf('let modelCatalogEpoch')),context);
const open=id=>vm.runInContext(`openReview(${JSON.stringify(id)},'revise')`,context);
const note=()=>el('#review-run-note');
for(const status of ['queued','running']){doc.latest_run.status=status;open('o');assert(note().textContent.includes('대기·처리 중으로 표시되어 있습니다. 끝나기 전에 저장하면 거부'),status);assert.equal(note().hidden,false);assert.equal(el('#review-error').textContent,'');}
open('f');assert.equal(note().textContent,'');assert(note().hidden);
doc.latest_run.status='success';open('o');assert.equal(note().textContent,'');assert(note().hidden);
doc.latest_run=null;open('o');assert.equal(note().textContent,'');assert(note().hidden);
doc.latest_run={id:'r',status:'running',version:1};open('o');
(async()=>{el('#review-form').onsubmit({preventDefault(){},target:el('#review-form')});await Promise.all(tasks);
assert.equal(el('#review-error').textContent,'refused by the server');assert(el('#review-dialog').open);})().catch(e=>{console.error(e);process.exit(1);});
'''
        result = subprocess.run(['node', '-e', script], text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
