"""Explicit bulk scope, atomic approval and UI request snapshot regressions."""
import concurrent.futures
import copy
import json
from pathlib import Path
import subprocess
import threading
import unittest
from urllib.error import HTTPError

import test_workflow as workflow
from test_workflow import SOURCE, PAGE, FACTS, approved_dictionary, legacy_run
from verinote.store import Conflict, MAX_BULK_APPROVAL, canonical


class BulkApproval(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def candidates(self, doc=None, modern=False):
        doc = doc or self.doc
        if modern:
            approved_dictionary(self.store, doc['id'])
            run = self.store.create_run(doc['id'], 'copilot', '', 'extract', doc['version'], doc['generation'])
        else:
            run = legacy_run(self.store, doc['id'])
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], PAGE, FACTS)
        return [i for i in self.store.document(doc['id'], doc['version'])['items'] if i['kind'] != 'ontology']

    def request(self, items, doc=None, **overrides):
        doc = doc or self.doc
        return dict(version=doc['version'], expected_version=doc['current_version'], expected_generation=doc['generation'],
                    items=[{'id': i['id'], 'revision': i['revision']} for i in items], actor=' Reviewer ', reason=' 원문 대조 완료 ') | overrides

    def approve(self, request, doc=None):
        return self.store.bulk_approve((doc or self.doc)['id'], **request)

    def state(self):
        with self.store.connect() as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM items ORDER BY id')],
                    [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY seq')])

    def test_selected_mixed_items_only_with_per_item_audit_and_graph(self):
        items = self.candidates(modern=True)
        chosen = [next(i for i in items if i['kind'] == 'page'), next(i for i in items if i['kind'] == 'fact')]
        snapshot = self.store.snapshot()
        result = self.approve(self.request(chosen))
        self.assertEqual(result['approved_count'], 2)
        self.assertEqual(result['version'], 1)
        for item in self.store.document(self.doc['id'])['items']:
            if item['id'] in {i['id'] for i in chosen}:
                self.assertEqual((item['status'], item['revision']), ('approved', 2))
                event = self.store.history(item['id'])[-1]
                self.assertEqual((event['action'], event['actor'], event['reason']), ('approved', 'Reviewer', '원문 대조 완료'))
                self.assertEqual(event['payload'], item['payload'])
            elif item['kind'] != 'ontology':
                self.assertEqual((item['status'], item['revision']), ('pending', 1))
        self.assertEqual(len(self.store.snapshot()['facts']), 1)
        self.assertEqual(self.store.snapshot(snapshot['id'])['facts'], [])

    def test_invalid_payload_in_last_item_rolls_back_updates_and_events(self):
        for field, bad in [('quote', 'invented'), ('predicate', 'not_in_dictionary')]:
            doc = self.store.register('synthetic', 's.md', SOURCE.encode(), SOURCE)
            items = self.candidates(doc, modern=True)
            fact = next(i for i in items if i['kind'] == 'fact')
            page = next(i for i in items if i['kind'] == 'page')
            with self.store.connect(True) as db:
                db.execute('UPDATE items SET payload=? WHERE id=?', (canonical(fact['payload'] | {field: bad}), fact['id']))
            before = self.state()
            with self.assertRaises(ValueError):
                self.approve(self.request([page, fact], doc), doc)
            self.assertEqual(self.state(), before)

    def test_pending_only_and_dictionary_exclusion(self):
        items = self.candidates(modern=True)
        dictionary = next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')
        before = self.state()
        with self.assertRaisesRegex(ValueError, '사전'):
            self.approve(self.request([items[0], dictionary]))
        self.assertEqual(self.state(), before)
        for state in ('held', 'rejected', 'approved'):
            item = self.store.document(self.doc['id'])['items'][0]
            self.store.review(item['id'], item['revision'], state, 'R', 'single review')
            item = next(i for i in self.store.document(self.doc['id'])['items'] if i['id'] == item['id'])
            before = self.state()
            with self.assertRaises(Conflict):
                self.approve(self.request([items[-1], item]))
            self.assertEqual(self.state(), before)

    def test_strict_batch_shapes_types_and_limits(self):
        items = self.candidates(); valid = self.request(items)
        changes = [{'items': v} for v in ([], None, {}, [None], valid['items'] * 2,
                   [{'id': str(i), 'revision': 1} for i in range(MAX_BULK_APPROVAL + 1)],
                   [valid['items'][0] | {'extra': 1}], [{'id': [], 'revision': 1}],
                   [{'id': '', 'revision': 1}])]
        changes += [{'items': [valid['items'][0] | {'revision': v}]} for v in (True, 1.0, '1', 0, -1)]
        changes += [{field: v} for field in ('version', 'expected_version', 'expected_generation') for v in (True, 1.0, 0)]
        changes += [{'actor': ''}, {'reason': ' '}, {'actor': 'a' * 101}, {'reason': 'a' * 1001}]
        before = self.state()
        for delta in changes:
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.approve(valid | delta)
            self.assertEqual(self.state(), before)

    def test_foreign_document_missing_id_and_stale_revision_are_atomic(self):
        items = self.candidates()
        other = self.store.register('other', 'other.md', SOURCE.encode(), SOURCE)
        foreign = self.candidates(other)[0]
        for final in (foreign, items[1] | {'id': 'missing'}, items[1] | {'revision': 9}):
            before = self.state()
            with self.assertRaises(Conflict):
                self.approve(self.request([items[0], final]))
            self.assertEqual(self.state(), before)

    def test_historical_scope_and_current_document_tokens_are_distinct(self):
        items = self.candidates()
        stale = self.request(items)
        new = self.store.register('new', 'new.md', SOURCE.encode(), SOURCE, self.doc['id'], 1, 1)
        before = self.state()
        with self.assertRaises(Conflict): self.approve(stale)
        with self.assertRaises(Conflict): self.approve(self.request(items, new))
        self.assertEqual(self.state(), before)
        historical = self.store.document(self.doc['id'], 1)
        result = self.approve(self.request(items, historical))
        self.assertEqual(result['approved_count'], 3)
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.store.delete_document(new['id'], True, new['version'], new['generation'])
        self.assertEqual(len(self.store.snapshot()['facts']), 2)
        with self.assertRaises(Conflict): self.approve(stale)

    def test_deleted_source_or_document_never_resurrects_candidates(self):
        items = self.candidates(); request = self.request(items)
        self.store.delete_document(self.doc['id'], False, 1, 1)
        before = self.state()
        with self.assertRaises(Conflict): self.approve(request)
        self.assertEqual(self.state(), before)

    def test_overlapping_bulk_transactions_have_one_winner(self):
        items = self.candidates(); barrier = threading.Barrier(2)
        def run(selected):
            barrier.wait()
            try: return self.approve(self.request(selected))
            except Conflict: return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, ([items[0], items[1]], [items[1], items[2]])))
        self.assertEqual(results.count('conflict'), 1)
        latest = self.store.document(self.doc['id'])['items']
        self.assertEqual(sum(i['status'] == 'approved' for i in latest), 2)
        self.assertEqual(sum(len(self.store.history(i['id'])) - 1 for i in latest), 2)

    def test_individual_review_race_does_not_partially_approve_losing_batch(self):
        items = self.candidates(); barrier = threading.Barrier(2)
        def run(bulk):
            barrier.wait()
            try:
                if bulk: return ('bulk', self.approve(self.request(items)))
                return ('single', self.store.review(items[1]['id'], 1, 'approved', 'R', 'single'))
            except Conflict: return ('conflict', None)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, (True, False)))
        self.assertEqual(sum(x[0] == 'conflict' for x in results), 1)
        expected = 3 if any(x[0] == 'bulk' for x in results) else 1
        latest = self.store.document(self.doc['id'])['items']
        self.assertEqual(sum(i['status'] == 'approved' for i in latest), expected)
        self.assertEqual(sum(len(self.store.history(i['id'])) - 1 for i in latest), expected)

    def test_later_pending_items_and_dictionary_revocation_do_not_widen_scope(self):
        items = self.candidates(modern=True); request = self.request(items)
        dictionary = next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(dictionary['id'], dictionary['revision'], 'pending', 'R', 'separate review')
        added = self.candidates()
        self.approve(request)
        current = self.store.document(self.doc['id'])['items']
        added_ids = {i['id'] for i in added} - {i['id'] for i in items}
        self.assertTrue(all(i['status'] == 'pending' for i in current if i['id'] in added_ids or i['kind'] == 'ontology'))
        self.assertEqual(sum(i['status'] == 'approved' for i in current), 3)


class BulkHTTP(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_endpoint_strict_scope_conflict_origin_and_no_server_select_all(self):
        doc = self.store.register('test', 't.md', SOURCE.encode(), SOURCE)
        run = legacy_run(self.store, doc['id']); self.store.run_status(run['id'], 'running'); self.store.finish_run(run['id'], PAGE, FACTS)
        items = self.store.document(doc['id'])['items']
        path = f'/api/documents/{doc["id"]}/approve-items'
        data = {'version': 1, 'expected_version': 1, 'expected_generation': 1, 'items': [{'id': items[0]['id'], 'revision': 1}], 'actor': 'R', 'reason': 'checked'}
        for body, headers, status in ((data | {'action': 'approved'}, {}, 400), ({k: v for k, v in data.items() if k != 'items'}, {}, 400), (data | {'version': True}, {}, 400), (data, {'Origin': 'https://evil.example'}, 403)):
            with self.assertRaises(HTTPError) as e: self.request(path, 'POST', body, headers)
            self.assertEqual(e.exception.code, status); e.exception.close()
        result = self.request(path, 'POST', data)
        self.assertEqual(result['approved_count'], 1)
        self.assertEqual(sum(i['status'] == 'pending' for i in self.store.document(doc['id'])['items']), 2)
        with self.assertRaises(HTTPError) as e: self.request(path, 'POST', data)
        self.assertEqual(e.exception.code, 409); e.exception.close()


class BulkUI(unittest.TestCase):
    def test_selection_snapshot_limits_escaping_submit_lock_and_navigation(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const elements=new Map(),boxes=[],tasks=[],notices=[],requests=[],refreshes=[];
function el(selector){
 if(!elements.has(selector)) elements.set(selector,{hidden:false,disabled:false,textContent:'',innerHTML:'',checked:false,
  elements:{actor:{value:'Reviewer'},reason:{value:'checked'}},addEventListener(){},reset(){},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(){}});
 return elements.get(selector);
}
const doc={id:'a',title:'<document>',version:1,current_version:2,generation:2,items:[
 {id:'f',revision:1,status:'pending',kind:'fact',payload:{subject:'<img src=x>',predicate:'depends_on',object:'B',quote:'<b>evidence</b>'}},
 {id:'p',revision:1,status:'pending',kind:'page',payload:{title:'<wiki>',claims:[{text:'<text>',quote:'<quote>'}]}},
 {id:'o',revision:2,status:'pending',kind:'ontology',payload:{}},
 ...['approved','held','rejected'].map(status=>({id:status,revision:1,status,kind:'fact',payload:{}}))]};
let responder;
const context=vm.createContext({console,doc,$:el,selectedDoc:doc,syncProviderControls:()=>{},
 document:{addEventListener(){},querySelectorAll(selector){return selector.endsWith(':checked')?boxes.filter(b=>b.checked):boxes;}},
 esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 attempt:fn=>{const task=fn();tasks.push(task);return task;},
 api:(path,method,data)=>{requests.push({path,method,data});return responder(path,method,data);},
 loadLibrary:async()=>{},openDoc:async(...args)=>refreshes.push(args),notice:(...args)=>notices.push(args)});
vm.runInContext('let bulkReviewTarget=null,bulkSubmitting=false,documentRequest=0;const MAX_BULK_APPROVAL=2000;',context);
vm.runInContext(source.slice(source.indexOf('function bulkCandidates('),source.indexOf('function openReview(')),context);
function call(code){return vm.runInContext(code,context);}
const html=call('renderBulkControls(doc)');assert(html.includes('선택 0개 승인'));assert(html.includes('전체 선택 (2개)'));
assert.deepEqual(Array.from(call('bulkCandidates(doc).map(i=>i.id)')),['f','p']);
assert.throws(()=>call('captureBulkReview(doc,[])'));
const snap=call('captureBulkReview(doc,["f","p","o"])');
assert.equal(snap.items.length,2);assert.equal(snap.version,1);assert.equal(snap.expected_version,2);
doc.items[0].revision=9;assert.equal(snap.items[0].revision,1);doc.items[0].revision=1;
context.large={...doc,items:Array.from({length:2001},(_,n)=>({...doc.items[0],id:String(n)}))};
assert.throws(()=>call('captureBulkReview(large,large.items.map(i=>i.id))'));
assert.equal(call('captureBulkReview(large,large.items.slice(0,2000).map(i=>i.id)).items.length'),2000);
boxes.push({checked:true,dataset:{bulkItem:'f'}},{checked:false,dataset:{bulkItem:'p'}});
call('updateBulkSelection()');assert.equal(el('#bulk-count').textContent,'선택 1개 · 위키 0개 · 관계 1개');assert(el('#bulk-select-all').indeterminate);
boxes[1].checked=true;call('updateBulkSelection()');assert(el('#bulk-select-all').checked);
call('openBulkReview()');assert(el('#bulk-review-dialog').open);assert.equal(el('#bulk-historical').hidden,false);
assert(el('#bulk-review-scope').textContent.includes('총 2개'));
assert(el('#bulk-review-preview').innerHTML.includes('&lt;img src=x&gt;'));assert(!el('#bulk-review-preview').innerHTML.includes('<img'));
assert(!el('#bulk-review-preview').innerHTML.includes('<b>evidence'));
const event={preventDefault(){},target:el('#bulk-review-form')};
(async()=>{
 let resolve;responder=()=>new Promise(r=>resolve=r);
 el('#bulk-review-form').onsubmit(event);assert(el('#bulk-confirm').disabled);assert(el('#bulk-cancel').disabled);
 el('#bulk-review-form').onsubmit(event);assert.equal(requests.length,1);
 context.selectedDoc={...doc,id:'other',version:3};
 resolve({approved_count:2});await Promise.all(tasks.splice(0));
 assert.equal(requests[0].path,'/api/documents/a/approve-items');assert.equal(requests[0].data.version,1);
 assert.equal(requests[0].data.expected_version,2);assert.equal(requests[0].data.items.length,2);
 assert.equal(refreshes.length,0);assert.equal(el('#bulk-review-dialog').open,false);
 // A newly requested document may still be loading with the old selectedDoc visible.
 context.selectedDoc=doc;call('openBulkReview()');responder=()=>new Promise(r=>resolve=r);
 el('#bulk-review-form').onsubmit(event);call('documentRequest++');
 resolve({approved_count:2});await Promise.all(tasks.splice(0));assert.equal(refreshes.length,0);
 // A conflict stays visible, unlocks the form, and never resubmits automatically.
 context.selectedDoc=doc;call('openBulkReview()');responder=async()=>{throw Object.assign(new Error('stale'),{status:409});};
 el('#bulk-review-form').onsubmit(event);await Promise.all(tasks.splice(0));
 assert.equal(requests.length,3);assert.equal(el('#bulk-review-error').textContent,'stale');
 assert.equal(el('#bulk-refresh').hidden,false);assert.equal(el('#bulk-confirm').disabled,false);assert(el('#bulk-review-dialog').open);
 // Actual openDoc must ignore late older responses and bind fresh empty selection controls.
 const pending={};context.api=path=>new Promise(r=>pending[path]=r);
 context.showView=()=>{};context.extractionControls=()=>'';context.renderItem=()=>'';context.esc=context.esc;
 call('documentRequest=0;');
 vm.runInContext(source.slice(source.indexOf('async function openDoc('),source.indexOf('function renderItem(')),context);
 const make=id=>({id,title:id,version:1,current_version:1,generation:1,filename:'s.md',sha256:'hash',text:'source',items:[],versions:[{version:1}]});
 const first=call('openDoc("first")'),second=call('openDoc("second")');
 pending['/api/documents/second'](make('second'));await second;
 pending['/api/documents/first'](make('first'));await first;
 assert.equal(call('selectedDoc.id'),'second');
 // Select-all/clear callbacks never silently truncate and update the selected count.
 context.selectedDoc=doc;el('#bulk-select-all').onchange({target:{checked:true}});assert(boxes.every(b=>b.checked));
 el('#bulk-clear').onclick();assert(boxes.every(b=>!b.checked));assert.equal(el('#bulk-count').textContent,'선택 없음');
 // Entering another view invalidates an outstanding document response.
 context.loadRuns=()=>{};context.loadQueries=async()=>{};context.loadQuestions=async()=>{};
 context.document.querySelectorAll=()=>[];
 vm.runInContext(source.slice(source.indexOf('function showView('),source.indexOf('async function loadLibrary(')),context);
 const third=call('openDoc("third")');call('showView("settings")');pending['/api/documents/third'](make('third'));await third;
 assert.equal(call('selectedDoc.id'),'a');
 console.log('Bulk UI selection, escaping, frozen scope, lock, conflict and navigation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
