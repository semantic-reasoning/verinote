"""Synthetic homonyms exercise navigation independently of model prose."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_assistant import Fake, tool

from verinote.engine import query
from verinote.question_meanings import MAX_GUIDANCE_BYTES, context, mentions
from verinote.service import Service
from verinote.store import Store, canonical


class QuestionMeanings(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.store = Store(self.root/'state.sqlite3')

    def fixture(self, person, kind, *, approve=False):
        source = f'{person}은 차를 좋아한다. 여기서 차는 {kind}다.'
        doc = self.store.register(kind+'자료', kind+'.txt', source.encode(), source)
        dictionary = {'entities': [{'id': person, 'name': person, 'type': '사람', 'quote': source},
                                   {'id': '차', 'name': '차', 'type': kind, 'quote': source}], 'aliases': [],
                      'relations': [{'id': 'likes', 'description': '주체가 대상에 대해 직접 표현한 선호',
                                     'subject_types': ['사람'], 'object_types': [kind], 'quote': source}]}
        run = self.store.create_run(doc['id'], 'agy', 'test', 'ontology', 1, 1)
        self.store.run_status(run['id'], 'running'); self.store.finish_run(run['id'], dictionary=dictionary)
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(item['id'], 1, 'approved', 'Test', '합성 어휘 검토')
        run = self.store.create_run(doc['id'], 'agy', 'test', 'extract', 1, 1)
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], {'title': person, 'claims': [{'heading': '선호', 'text': source, 'quote': source}]},
                              [{'subject': person, 'predicate': 'likes', 'object': '차', 'quote': source}])
        for item in self.store.document(doc['id'])['items']:
            if item['kind'] == 'page' or item['kind'] == 'fact' and approve:
                self.store.review(item['id'], 1, 'approved', 'Test', '합성 근거 검토')
        return doc

    def test_one_character_particles_alias_boundaries_and_unicode(self):
        for question_text in ('차', '차는?', '차를 좋아하는 사람은?', '차에 관해'):
            self.assertTrue(mentions('차', question_text), question_text)
        for question_text in ('차고', '주차장', '차이는?', '자동차'):
            self.assertFalse(mentions('차', question_text), question_text)
        self.assertTrue(mentions('café', 'cafe\u0301?'))
        self.assertFalse(mentions('tea', 'steak'))

    def test_pending_facts_excluded_but_both_approved_dictionary_meanings_visible(self):
        self.fixture('가람', '음료'); self.fixture('나래', '이동수단')
        snapshot = self.store.snapshot(); self.assertEqual(snapshot['facts'], [])
        result = context(snapshot, '차를 좋아하는 사람은?')
        group = next(g for g in result['groups'] if g['term'] == '차')
        self.assertTrue(group['ambiguous']); self.assertEqual(group['total_candidates'], 2)
        self.assertEqual({c['type'] for c in group['candidates']}, {'음료', '이동수단'})
        self.assertTrue(all(c['current_approved'] and c['fact_count'] == 0 and not c['approved_facts'] for c in group['candidates']))
        self.assertEqual({r['id'] for r in result['relations']}, {'likes'}); self.assertFalse(result['citable'])

    def test_approved_facts_keep_each_interpretation_source_and_snapshot(self):
        self.fixture('가람', '음료', approve=True); self.fixture('나래', '이동수단', approve=True)
        snapshot = self.store.snapshot(); result = context(snapshot, '차는?')
        people = {c['type']: c['approved_facts'][0]['subject'] for c in result['groups'][0]['candidates']}
        self.assertEqual(people, {'음료': '가람', '이동수단': '나래'})
        for candidate in result['groups'][0]['candidates']:
            self.assertEqual(candidate['fact_count'], 1)
            self.assertEqual(candidate['approved_facts'][0]['document_id'], candidate['document_id'])
        before = copy.deepcopy(result)
        item = next(i for d in snapshot['documents'] for i in self.store.document(d['id'])['items'] if i['kind'] == 'fact')
        self.store.review(item['id'], item['revision'], 'held', 'Test', '합성 철회')
        self.assertEqual(context(self.store.snapshot(snapshot['id']), '차는?'), before)
        self.assertNotEqual(context(self.store.snapshot(), '차는?'), before)

    def test_model_omission_does_not_hide_server_guidance_or_become_graph_evidence(self):
        self.fixture('가람', '음료'); self.fixture('나래', '이동수단')
        fake = Fake([tool(subject='차'), {'answer': {'claims': [], 'unknowns': ['승인된 관계를 확인하지 못했습니다.']}}])
        service = Service(self.store, self.root, fake); self.addCleanup(service.close)
        job = service.ask({'question': '차를 좋아하는 사람은?', 'agent': 'agy', 'model': 'test'})
        service.pool.shutdown(wait=True); result = self.store.question(job['id'])
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(result['meanings']['groups'][0]['total_candidates'], 2)
        self.assertEqual(result['evidence']['E1']['result']['rows'], [])
        self.assertIn('relation_meanings', fake.calls[0][2]); self.assertIn('likes', fake.calls[0][2])
        self.assertNotIn('좋아하다', json.dumps(result['meanings']['relations'], ensure_ascii=False))
        self.assertEqual(self.store.snapshot(result['snapshot'])['facts'], [])

    def test_large_catalog_is_explicitly_bounded_without_selecting_first_meaning(self):
        self.fixture('가람', '음료')
        snapshot = self.store.snapshot(); base = snapshot['ontologies'][0]
        snapshot['ontologies'] = [copy.deepcopy(base) | {'id': f'o{i}', 'document_id': f'd{i}'} for i in range(100)]
        result = context(snapshot, '차는?')
        self.assertTrue(result['truncated']); self.assertTrue(result['groups'][0]['truncated'])
        self.assertEqual(result['groups'][0]['total_candidates'], 100)
        self.assertLessEqual(len(canonical(result).encode()), MAX_GUIDANCE_BYTES)
        self.assertGreater(len(result['groups'][0]['candidates']), 1)
        legacy = {k: v for k, v in snapshot.items() if k != 'graph_identity_version'}
        self.assertEqual(context(legacy, '차는?')['contract'], 'legacy')

    def test_aliases_find_distinct_meanings_and_unrelated_compound_does_not(self):
        self.fixture('가람', '음료'); self.fixture('나래', '이동수단')
        snapshot = self.store.snapshot()
        for item in snapshot['ontologies']:
            item['payload']['aliases'] = [{'alias': '공통별칭', 'entity': '차', 'quote': '차 공통별칭'}]
        result = context(snapshot, '공통별칭은?')
        self.assertEqual(result['groups'][0]['total_candidates'], 2)
        self.assertEqual(context(snapshot, '주차장은?')['groups'], [])

    def test_ui_shows_both_meanings_escaped_and_keeps_scoped_proofs_separate(self):
        self.fixture('가람', '음료', approve=True); self.fixture('나래', '이동수단', approve=True)
        snapshot = self.store.snapshot(); result = context(snapshot, '차는?')
        result['groups'][0]['candidates'][0]['document_title'] = '<img onerror=bad>'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const nodes={};const c=vm.createContext({esc:v=>String(v??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x])),sourceLink:()=>'<a>원문</a>',$:s=>nodes[s],notice:()=>{},value:VALUE});
vm.runInContext(source.slice(source.indexOf('function renderEvidence('),source.indexOf('function renderAnswer(')),c);
const html=vm.runInContext('renderQuestionMeanings(value,"frozen")',c);
assert(html.includes('음료'));assert(html.includes('이동수단'));assert(html.includes('가람'));assert(html.includes('나래'));
assert(html.includes('&lt;img'));assert(!html.includes('<img'));assert(html.includes('관계 사실의 근거가 아닙니다'));
assert.equal((html.match(/class="item meaning-candidate"/g)||[]).length,2);
const form={elements:{subject:{},snapshot:{}},closest:()=>({}),scrollIntoView(){}};nodes['#query-form']=form;
const button={dataset:{meaningField:'subject',meaningRef:'entity:opaque',meaningSnapshot:'frozen'}};
c.root={querySelectorAll:()=>[button]};vm.runInContext('bindMeaningControls(root)',c);button.onclick();
assert.equal(form.elements.subject.value,'entity:opaque');assert.equal(form.elements.snapshot.value,'frozen');
c.r={entities:[{ref:'one',name:'동명이인',type:'사람',document_title:'첫자료',version:1},{ref:'two',name:'동명이인',type:'사람',document_title:'둘째자료',version:1}],relations:[]};
assert.notEqual(vm.runInContext('scopedRow(r,["동명이인"],["one"])',c),vm.runInContext('scopedRow(r,["동명이인"],["two"])',c));
assert(vm.runInContext('renderGraphMeanings({})',c).includes('이전 그래프 계약'));
'''.replace('VALUE', json.dumps(result, ensure_ascii=False))
        subprocess.run(['node', '-e', script], check=True, cwd=Path(__file__).parents[1], capture_output=True, text=True)

    def test_ui_duplicate_display_triples_keep_each_proof_and_ambiguity_status(self):
        self.fixture('가람', '음료', approve=True); self.fixture('가람', '이동수단', approve=True)
        snapshot = self.store.snapshot(); result = query(snapshot, 'upstream', '차', 'likes')
        self.assertEqual(result['rows'], [['가람', 'likes', '차']]*2)
        script = r'''const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const nodes={'#query-result':{innerHTML:'',querySelectorAll:()=>[]},'#download-query':{}};
const c=vm.createContext({esc:String,sourceLink:()=>'',r:VALUE,$:s=>nodes[s]});
vm.runInContext(source.slice(source.indexOf('function renderEvidence('),source.indexOf('function renderAnswer(')),c);
vm.runInContext(source.slice(source.indexOf('function renderQuery('),source.indexOf('async function loadQueries(')),c);
vm.runInContext('renderQuery(r)',c);let html=nodes['#query-result'].innerHTML;
assert.equal((html.match(/class="query-row"/g)||[]).length,2);assert(html.includes('음료'));assert(html.includes('이동수단'));
for(const fact of c.r.facts)assert(html.includes(fact.id));
c.r={...c.r,status:'ambiguous',rows:[],row_refs:[],proofs:[],facts:[]};vm.runInContext('renderQuery(r)',c);
html=nodes['#query-result'].innerHTML;assert(html.includes('의미를 선택해 주세요'));assert(!html.includes('일치 결과 없음'));
'''.replace('VALUE', json.dumps(result, ensure_ascii=False))
        subprocess.run(['node', '-e', script], check=True, cwd=Path(__file__).parents[1], capture_output=True, text=True)
