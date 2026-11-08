"""Entity IDs and aliases that lookup cannot select: refused when a person adds them in a dictionary edit, listed when already stored. Spellings are synthetic."""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from verinote.entity_identity import _pin
from verinote.ontology import lookup_spelling_problem, lookup_spelling_problems
from verinote.server import Server
from verinote.service import Service
from verinote.store import Store, digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '주문(entity:x)은 결제에 의존한다. 결제는\n원장에 의존한다. 결제(Entity:p)는 같다.'
RELATIONS = [{'id': 'depends_on', 'description': 'd', 'subject_types': ['Service'], 'object_types': ['Service'], 'quote': SOURCE}]


def entity(identifier, name):
    return {'id': identifier, 'name': name, 'type': 'Service', 'quote': SOURCE}


BASE = {'entities': [entity('주문', '주문'), entity('결제', '결제'), entity('원장', '원장')], 'aliases': [], 'relations': RELATIONS}


def edited(**added):
    value = copy.deepcopy(BASE)
    for kind, rows in added.items():
        value[kind] = value[kind] + rows
    return value


def alias(spelling, target, quote):
    return {'alias': spelling, 'entity': target, 'quote': quote}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')

    def generated(self, dictionary=BASE):
        """A dictionary stored the way a model run stores it, which applies no lookup-spelling rule."""
        self.doc = self.store.register('d', 'x.md', SOURCE.encode(), SOURCE)
        run = self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1)
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], dictionary=copy.deepcopy(dictionary))
        return self.item()

    def item(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def revise(self, item, payload):
        return self.store.review(item['id'], item['revision'], 'revise', 'R', 'e', payload)

    def refused(self, payload, item=None):
        item = item or self.generated()
        with self.assertRaises(ValueError) as caught:
            self.revise(item, payload)
        self.assertEqual(self.item()['revision'], item['revision'])
        return str(caught.exception)


class NewRows(Base):
    def test_clean_edit_is_accepted(self):
        self.assertEqual(self.revise(self.generated(), edited(entities=[entity('주문2', '주문')]))['status'], 'pending')

    def test_new_entity_id_with_a_lookup_reference_prefix_is_refused(self):
        for prefix in ('entity:', 'relation:'):
            with self.subTest(prefix=prefix):
                message = self.refused(edited(entities=[entity(prefix + 'svc', '주문')]))
                self.assertEqual(message, f'새로 넣을 수 없는 개체 ID·별칭이 있습니다. entities[4] "{prefix}svc": entity: 또는 relation:으로 시작하면 조회가 개체·관계 참조로 해석하므로, 이 표기로는 이 개체를 찾지 못합니다. 접두사 없이 쓰세요.')
                self.setUp()

    def test_new_alias_with_a_lookup_reference_prefix_is_refused(self):
        message = self.refused(edited(aliases=[alias('entity:x', '주문', '주문(entity:x)은')]))
        self.assertIn('aliases[1] "entity:x": entity: 또는 relation:으로 시작하면', message)

    def test_new_entity_id_with_a_newline_or_a_tab_is_refused_and_an_alias_may_hold_one(self):
        for spelling in ('결\n제2', '결\t제2'):
            with self.subTest(spelling=spelling):
                message = self.refused(edited(entities=[entity(spelling, '결제')]))
                self.assertIn(f'entities[4] {json.dumps(spelling, ensure_ascii=False)}: 개체 ID에는 줄바꿈이나 탭을 쓸 수 없습니다. 원문 표기에 줄바꿈이나 탭이 있으면 별칭으로 넣으세요.', message)
                self.setUp()
        item = self.generated()
        self.assertEqual(self.revise(item, edited(aliases=[alias('결제는\n원장', '원장', '결제는\n원장')]))['status'], 'pending')

    def test_a_prefix_in_another_letter_case_is_selectable_and_accepted(self):
        self.assertEqual(self.revise(self.generated(), edited(aliases=[alias('Entity:p', '결제', '결제(Entity:p)는')]))['status'], 'pending')

    def test_the_spelling_is_judged_as_stored_after_stripping(self):
        self.assertIn('entities[4] "entity:svc"', self.refused(edited(entities=[entity(' entity:svc', '주문')])))

    def test_refusal_names_input_position_after_an_identity_alias(self):
        message = self.refused(edited(aliases=[alias('주문', '주문', '주문(entity:x)은'), alias('entity:x', '주문', '주문(entity:x)은')]))
        self.assertIn('aliases[2]', message)
        self.assertNotIn('aliases[1]', message)

    def test_identity_alias_that_validate_dictionary_drops_is_not_refused_and_never_stored(self):
        item = self.generated(edited(entities=[entity('entity:x', '주문')]))  # a stored row the edit keeps
        keep = copy.deepcopy(item['payload'])
        keep['aliases'].append(alias('entity:x', 'entity:x', '주문(entity:x)은'))
        self.assertEqual(self.revise(item, keep)['status'], 'pending')
        self.assertEqual(self.item()['payload']['aliases'], [])
        self.assertEqual([p['kind'] for p in lookup_spelling_problems(keep)], ['entities'])
        item = self.generated()
        identity = edited(entities=[entity('entity:x', '주문')], aliases=[alias('entity:x', 'entity:x', '주문(entity:x)은')])
        message = self.refused(identity, item)
        self.assertIn('entities[4] "entity:x"', message)
        self.assertNotIn('aliases[1]', message)

    def test_more_than_five_new_rows_names_five_and_counts_the_rest(self):
        message = self.refused(edited(entities=[entity(f'entity:{n}', '주문') for n in range(7)]))
        self.assertEqual(message.count('entity: 또는 relation:으로 시작하면'), 5)
        self.assertTrue(message.endswith(' 외 2개'), message)
        self.assertIn('entities[8] "entity:4"', message)
        self.assertNotIn('"entity:5"', message)

    def test_generation_stores_a_prefixed_entity_id_and_a_historical_pin_still_resolves(self):
        stored = edited(entities=[entity('entity:svc', '주문')])
        self.generated(stored)  # a model run stores it
        pin = {'id': 'x', 'revision': 1, 'payload': json.loads(json.dumps(stored)), 'sha256': digest(stored)}
        self.assertIsNotNone(_pin(pin))  # a historical pin still resolves


class StoredRows(Base):
    STORED = edited(entities=[entity('entity:svc', '주문')])

    def test_stored_row_is_listed_and_clean_dictionary_lists_nothing(self):
        item = self.generated(self.STORED)
        self.assertEqual([(p['kind'], p['index'], p['spelling'], p['reason']) for p in lookup_spelling_problems(item['payload'])], [('entities', 4, 'entity:svc', 'prefix')])
        self.assertEqual(lookup_spelling_problems(BASE), [])
        self.assertEqual([lookup_spelling_problem('entities', s) for s in ('a', 'entity:a', 'relation:a', 'Entity:a', 'a\nb', 'a\tb')], [None, 'prefix', 'prefix', None, 'control', 'control'])
        self.assertEqual([lookup_spelling_problem('aliases', s) for s in ('a\nb', 'a\tb', 'entity:a')], [None, None, 'prefix'])

    def test_unrelated_edit_and_approval_keep_the_stored_row(self):
        item = self.generated(self.STORED)
        keep = copy.deepcopy(item['payload'])
        keep['relations'][0]['description'] = 'changed'
        self.assertEqual(self.revise(item, keep)['status'], 'pending')
        item = self.item()
        self.assertEqual(self.store.review(item['id'], item['revision'], 'approved', 'R', 'ok')['status'], 'approved')

    def test_adding_a_second_bad_row_names_only_the_new_one(self):
        item = self.generated(self.STORED)
        more = copy.deepcopy(item['payload'])
        more['aliases'].append(alias('entity:x', '주문', '주문(entity:x)은'))
        message = self.refused(more, item)
        self.assertIn('aliases[1] "entity:x"', message)
        self.assertNotIn('entities[', message)

    def test_same_spelling_retarget_of_a_stored_alias_is_accepted_and_re_adding_after_removal_is_refused(self):
        item = self.generated(edited(aliases=[alias('entity:x', '주문', '주문(entity:x)은')]))
        retarget = copy.deepcopy(item['payload'])
        retarget['aliases'][0]['entity'] = '결제'
        retarget['aliases'][0]['quote'] = '주문(entity:x)은 결제'
        self.assertEqual(self.revise(item, retarget)['status'], 'pending')
        item = self.item()
        removed = copy.deepcopy(item['payload'])
        removed['aliases'] = []
        self.revise(item, removed)
        item = self.item()
        again = copy.deepcopy(item['payload'])
        again['aliases'].append(alias('entity:x', '주문', '주문(entity:x)은'))
        self.assertIn('aliases[1] "entity:x"', self.refused(again, item))

    def test_stored_prefixed_alias_moved_to_entity_id_is_refused(self):
        item = self.generated(edited(aliases=[alias('entity:x', '주문', '주문(entity:x)은')]))
        moved = copy.deepcopy(item['payload'])
        moved['aliases'] = []
        moved['entities'].append(entity('entity:x', '주문'))
        self.assertIn('entities[4] "entity:x"', self.refused(moved, item))


class Routes(Base):
    def test_document_get_lists_stored_rows_and_empty_for_clean(self):
        service = Service(self.store, self.root)
        server = Server(('127.0.0.1', 0), self.store, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(service.pool.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def listed():
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/documents/{self.doc['id']}") as response:
                return next(i for i in json.loads(response.read())['items'] if i['kind'] == 'ontology')['lookup_spelling_problems']
        self.generated(edited(aliases=[alias('entity:x', '주문', '주문(entity:x)은')]))
        self.assertEqual(listed(), [{'kind': 'aliases', 'index': 1, 'spelling': 'entity:x', 'reason': 'prefix'}])
        item = self.item()
        self.revise(item, copy.deepcopy(BASE))
        self.assertEqual(listed(), [])


SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const cut=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b));
const ctx=vm.createContext({console});
vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
vm.runInContext(cut('function renderLookupSpellingProblems(','function renderItem('),ctx);
const render=p=>vm.runInContext(`renderLookupSpellingProblems(${JSON.stringify(p)})`,ctx);
assert.equal(render([]),'');assert.equal(render(undefined),'');
const out=render([{kind:'entities',index:2,spelling:'a\nb<i>',reason:'control'},{kind:'aliases',index:1,spelling:'entity:x',reason:'prefix'}]);
assert(out.includes('새로 넣을 수 없는 개체 ID·별칭 2개'));
assert(!out.includes('role='),'a card panel is not a live region');
assert(out.includes('entities[2] &quot;a\\nb&lt;i&gt;&quot;')||out.includes('entities[2] "a\\nb&lt;i&gt;"'),out);
assert(!out.includes('<i>'));
assert(out.includes('새 개체 ID에는 줄바꿈이나 탭을 쓸 수 없습니다.'));
assert(out.includes('entity: 또는 relation:으로 시작하면 조회가 개체·관계 참조로 해석하므로, 이 표기로는 이 개체를 찾지 못합니다.'));
assert(out.includes('이미 저장된 행이므로 이 행 때문에 승인이나 다른 내용 수정이 거부되지는 않습니다.'));
const item=source.slice(source.indexOf('function renderItem('),source.indexOf('function renderProgress('));
assert(item.indexOf('renderLookupSpellingProblems(item.lookup_spelling_problems)')>0);
assert(item.indexOf('renderLookupSpellingProblems(item.lookup_spelling_problems)')<item.indexOf('renderDictionary(p,'));
Object.assign(ctx,{renderRelationTypeWarnings:()=>'',renderDictionaryReconciliations:()=>'',renderDictionaryExclusions:()=>'',renderExtractionAdmission:()=>'',renderDictionary:()=>'DICT',bulkItemLabel:()=>'',badge:()=>'',labels:{}});
vm.runInContext(cut('function renderItem(','function renderProgress('),ctx);
ctx.it={id:'o',kind:'ontology',status:'approved',revision:1,run_id:'r',payload:{},lookup_spelling_problems:[{kind:'aliases',index:1,spelling:'entity:x',reason:'prefix'}]};
const card=vm.runInContext('renderItem(it)',ctx);
assert(card.includes('aliases[1]')&&card.indexOf('aliases[1]')<card.indexOf('DICT'));
ctx.it.lookup_spelling_problems=[];
assert(!vm.runInContext('renderItem(it)',ctx).includes('새로 넣을 수 없는'));
// the review dialog
const elements=new Map();
const el=s=>{if(!elements.has(s))elements.set(s,{hidden:false,textContent:'',innerHTML:'',elements:{actor:{value:''},reason:{value:''}},reset(){},showModal(){}});return elements.get(s);};
const dialog=vm.createContext({console,$:el,JSON,labels:{},esc:String,selectedDoc:null});
vm.runInContext('let reviewTarget=null;',dialog);
vm.runInContext(cut('function renderLookupSpellingProblems(','function renderItem('),dialog);
vm.runInContext(cut('function openReview(',"$('#cancel-review')"),dialog);
dialog.selectedDoc={latest_run:null,items:[
 {id:'o',kind:'ontology',status:'approved',revision:1,payload:{aliases:[]},lookup_spelling_problems:[{kind:'entities',index:1,spelling:'entity:s',reason:'prefix'}]},
 {id:'c',kind:'ontology',status:'approved',revision:1,payload:{aliases:[]},lookup_spelling_problems:[]},
 {id:'f',kind:'fact',status:'pending',revision:1,payload:{subject:'',predicate:'',object:'',quote:''}}]};
vm.runInContext("openReview('o','approved')",dialog);
assert.equal(el('#review-lookup-note').hidden,false);assert(el('#review-lookup-note').innerHTML.includes('entities[1]'));
vm.runInContext("openReview('c','approved')",dialog);
assert.equal(el('#review-lookup-note').hidden,true);assert.equal(el('#review-lookup-note').innerHTML,'');
vm.runInContext("openReview('o','approved')",dialog);vm.runInContext("openReview('f','revise')",dialog);
assert.equal(el('#review-lookup-note').hidden,true);
'''


class Browser(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_panel_and_review_dialog_render_stored_rows_escaped_and_hide_when_clean(self):
        result = subprocess.run(['node', '-e', SCRIPT], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_dialog_element_exists_in_index_html(self):
        self.assertIn('id="review-lookup-note"', (ROOT / 'verinote/web/index.html').read_text())


if __name__ == '__main__':
    unittest.main()
