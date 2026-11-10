"""Entity IDs, names and facts that differ only in spacing: what is refused, what is listed, and the documented limits.

A whitespace-removed comparison only refuses or lists; nothing here connects spellings by it. All spellings are synthetic.
"""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import test_workflow as workflow
from test_spacing_links import (
    A,
    B,
    aliases,
    codes,
    entity,
    ids,
    reconcile,
    revision,
    stored,
)

from verinote.dictionary_reconciliation import (
    SPACING_REFUSAL,
    SPACING_RENAME_REFUSAL,
    spacing_kind,
    spacing_name_rows,
)
from verinote.entity_identity import candidates, identity_graph
from verinote.ontology import dictionary_fact, validate_dictionary
from verinote.server import Server
from verinote.service import Service
from verinote.store import Store

ROOT = Path(__file__).resolve().parents[1]
WRAPPED = '납품\n계획'
ZWSP = '​'
FILLER = 'ㅤ'
FOOTER = ('조회와 질문 안내는 ID와 원문 명칭 중 어느 표기로도 이 개체를 찾으므로, 띄어쓰기만 다른 두 표기가 한 개체로 연결됩니다. '
          '띄어쓰기는 뜻을 바꿀 수 있으니 같은 대상인지 원문과 대조하고, 다르면 내용 수정으로 ID나 원문 명칭을 고치세요. '
          '이 목록은 승인을 막지 않습니다. 한쪽에 줄바꿈이 있는 자리, 공백 폭이나 종류만 다른 자리는 띄어쓰기 차이로 보지 않습니다. '
          '공백이 아닌 문자(길이가 없는 문자, 빈칸처럼 보이는 한글 채움 문자 U+3164 등)는 공백으로 보지 않으므로, '
          '이런 문자가 한쪽에만 있거나 자리가 달라 공백을 빼도 두 표기가 같지 않으면 표시하지 않습니다.')


def graph(dictionary):
    return identity_graph({'graph_identity_version': 1, 'documents': [], 'facts': [],
                           'ontologies': [{'id': 'o', 'document_id': 'd', 'version': 1, 'revision': 1, 'payload': dictionary}]})


def listed(*pairs):
    return [(r['index'], r['id'], r['name']) for r in spacing_name_rows({'entities': [entity(i, n) for i, n in pairs]})]


class GeneratedEntityRow(unittest.TestCase):
    """A model entity row whose ID and name differ only in spacing is stored; lookup then selects it by both."""

    def test_generated_entity_row_with_id_and_name_differing_in_spacing_is_kept_and_lookup_selects_it_by_both(self):
        d, x, _ = reconcile(f'{A} {B}', {'entities': [entity(A, B)]})
        self.assertEqual((ids(d), x), ([A], []))
        g = graph(d)
        self.assertEqual([r['id'] for r in candidates(g, A)], [A])
        self.assertEqual([r['id'] for r in candidates(g, B)], [A])
        self.assertEqual(candidates(g, '납품  계획'), [])  # exact spellings only
        self.assertEqual(listed((A, B)), [(1, A, B)])


class Criterion(unittest.TestCase):
    def test_spacing_name_rows_list_a_space_present_on_one_side_only(self):
        for pair in ((A, B), (B, A), (B, '납품　계획'), (B, '납품 계획'), ('가\n나다', '가나 다'), ('가 나다', '가나 다')):
            with self.subTest(pair=pair):
                self.assertEqual(spacing_kind(*pair), 'spacing')
                self.assertEqual(listed(pair), [(1, *pair)])

    def test_a_line_break_on_either_side_or_a_width_or_kind_difference_is_layout_and_not_listed(self):
        for pair in ((B, WRAPPED), (WRAPPED, B), (B, '납품\r\n계획'), (B, '납품 계획'), (B, '납품\x85계획'),
                     (A, WRAPPED), (A, '납품  계획'), (A, '납품\t계획'), (A, '납품 계획'), (A, '납품　계획'),
                     ('가\n나다', '가나다'), ('가나다라', '가 \n나다라')):
            with self.subTest(pair=pair):
                self.assertEqual(spacing_kind(*pair), 'layout')
                self.assertEqual(listed(pair), [])

    def test_a_non_whitespace_character_on_one_side_or_at_another_spot_is_neither(self):
        for pair in ((B, f'납품{ZWSP}계획'), (A, f'납품 {ZWSP}계획'), (B, f'납품{FILLER}계획'), (A, f'납품{FILLER}계획'),
                     (f'납{ZWSP}품 계획', f'납품{ZWSP}계획'), (A, A), (A, '정산 문서')):
            with self.subTest(pair=pair):
                self.assertIsNone(spacing_kind(*pair))
                self.assertEqual(listed(pair), [])

    def test_the_same_invisible_character_at_the_same_spot_on_both_sides_is_listed(self):
        pair = (f'납{ZWSP}품 계획', f'납{ZWSP}품계획')
        self.assertEqual(spacing_kind(*pair), 'spacing')
        self.assertEqual(listed(pair), [(1, *pair)])

    def test_rows_carry_their_1_based_position(self):
        self.assertEqual(listed(('정산', '정산'), (A, WRAPPED), (A, B)), [(3, A, B)])


class Routes(unittest.TestCase):
    SOURCE = f'{A}은 {B}이라고도 한다. 정산 문서가 있다.'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.doc = self.store.register('d', 'x.md', self.SOURCE.encode(), self.SOURCE)
        dictionary = validate_dictionary({'entities': [entity('정산 문서', quote=self.SOURCE), entity(A, B, quote=self.SOURCE)],
                                          'aliases': [], 'relations': []}, self.SOURCE)
        run = self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1)
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], dictionary=dictionary)

    def item(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def test_document_get_carries_the_rows_and_approval_still_succeeds(self):
        service = Service(self.store, self.root)
        server = Server(('127.0.0.1', 0), self.store, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(service.pool.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def rows():
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/documents/{self.doc['id']}") as response:
                return next(i for i in json.loads(response.read())['items'] if i['kind'] == 'ontology')['spacing_name_rows']
        self.assertEqual(rows(), [{'index': 2, 'id': A, 'name': B}])
        item = self.item()
        self.assertEqual(self.store.review(item['id'], item['revision'], 'approved', 'R', 'checked')['status'], 'approved')
        self.assertEqual(rows(), [{'index': 2, 'id': A, 'name': B}])
        item = self.item()
        clean = copy.deepcopy(item['payload'])
        clean['entities'][1]['name'] = A
        self.store.review(item['id'], item['revision'], 'revise', 'R', 'fixed', clean)
        self.assertEqual(rows(), [])


SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const cut=(a,b)=>{const i=source.indexOf(a),j=source.indexOf(b,i);assert(i>=0&&j>i,a);return source.slice(i,j);};
const footer=process.argv[1];
const ctx=vm.createContext({console});
vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
vm.runInContext(cut('function visibleSpelling(','let termState'),ctx);
vm.runInContext(cut('function renderSpacingNameRows(','function renderItem('),ctx);
const render=r=>vm.runInContext(`renderSpacingNameRows(${JSON.stringify(r)})`,ctx);
assert.equal(render([]),'');assert.equal(render(undefined),'');
const out=render([{index:2,id:'<b>가 나</b>',name:'<b>가나</b>'},{index:5,id:'납품 계획',name:'납품계획'}]);
assert(out.includes('ID와 원문 명칭이 띄어쓰기만 다른 개체 2개'),out);
assert(out.includes('entities[2] ID &lt;b&gt;가<span class="ws" title="공백">␣</span>나&lt;/b&gt; · 원문 명칭 &lt;b&gt;가나&lt;/b&gt;'),out);
assert(!out.includes('<b>'),out);
assert(out.includes('entities[5] ID 납품<span class="ws" title="공백">␣</span>계획 · 원문 명칭 납품계획'),out);
assert(out.includes(`<p>${footer}</p>`),out);
const item=source.slice(source.indexOf('function renderItem('),source.indexOf('function renderProgress('));
assert(item.indexOf('renderSpacingNameRows(item.spacing_name_rows)')>0);
assert(item.indexOf('renderSpacingNameRows(item.spacing_name_rows)')<item.indexOf('renderDictionary(p,'));
// the review dialog shows the same rows next to the lookup note, and hides the note when both are empty
const elements=new Map();
const el=s=>{if(!elements.has(s))elements.set(s,{hidden:false,textContent:'',innerHTML:'',elements:{actor:{value:''},reason:{value:''}},reset(){},showModal(){}});return elements.get(s);};
const dialog=vm.createContext({console,$:el,JSON,labels:{},selectedDoc:null});
vm.runInContext(source.match(/^const esc = .*$/m)[0],dialog);
vm.runInContext('let reviewTarget=null;',dialog);
vm.runInContext(cut('function visibleSpelling(','let termState'),dialog);
vm.runInContext(cut('function renderLookupSpellingProblems(','function renderItem('),dialog);
vm.runInContext(cut('function openReview(',"$('#cancel-review')"),dialog);
dialog.selectedDoc={latest_run:null,items:[
 {id:'o',kind:'ontology',status:'pending',revision:1,payload:{aliases:[]},lookup_spelling_problems:[],spacing_name_rows:[{index:1,id:'납품 계획',name:'납품계획'}]},
 {id:'c',kind:'ontology',status:'pending',revision:1,payload:{aliases:[]},lookup_spelling_problems:[],spacing_name_rows:[]}]};
vm.runInContext("openReview('o','approved')",dialog);
assert.equal(el('#review-lookup-note').hidden,false);assert(el('#review-lookup-note').innerHTML.includes('entities[1] ID 납품'));
vm.runInContext("openReview('c','approved')",dialog);
assert.equal(el('#review-lookup-note').hidden,true);assert.equal(el('#review-lookup-note').innerHTML,'');
'''


class Browser(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_panel_and_review_dialog_render_rows_through_visible_spelling(self):
        result = subprocess.run(['node', '-e', SCRIPT, FOOTER], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RenameRefusal(unittest.TestCase):
    """A `revisions` rename that makes an entity's name a spacing variant of its own ID defers the whole bundle."""
    SRC = f'{A} {B} {WRAPPED} 납품　계획 정산 문서 끝'

    def prev(self, *rows):
        return stored(self.SRC, [entity(i, n, quote=self.SRC) for i, n in rows], [])

    def rename(self, prev, key, old_name, new_name, extra=()):
        return reconcile(self.SRC, {'revisions': lambda r: [revision('entities', key, entity(key, old_name), entity(key, new_name), r),
                                                             *[revision('entities', k, entity(k), entity(k, kind='문서'), r) for k in extra]]}, prev)

    def name_of(self, dictionary, key):
        return next(e['name'] for e in dictionary['entities'] if e['id'] == key)

    def test_revision_renaming_a_name_to_a_spacing_variant_of_its_id_defers_the_bundle(self):
        for key, new in ((A, B), (B, A), (B, '납품　계획')):
            with self.subTest(key=key, new=new):
                d, x, c = self.rename(self.prev((key, key), ('끝', '끝')), key, key, new, extra=('끝',))
                self.assertEqual(self.name_of(d, key), key)
                self.assertEqual(self.name_of(d, '끝'), '끝')
                self.assertEqual([e['status'] for e in c], ['deferred', 'deferred'])
                self.assertEqual({e['reason'] for e in c}, {SPACING_RENAME_REFUSAL})

    def test_the_rename_reason_names_the_remedy_for_a_name(self):
        self.assertNotIn('사전에 추가', SPACING_RENAME_REFUSAL)
        self.assertIn('내용 수정으로 이름을 바꾸세요', SPACING_RENAME_REFUSAL)
        self.assertIn('묶음 전체', SPACING_RENAME_REFUSAL)
        self.assertLessEqual(len(SPACING_RENAME_REFUSAL), 300)

    def test_renaming_to_a_line_break_variant_is_applied(self):
        for key in (A, B):
            with self.subTest(key=key):
                d, x, c = self.rename(self.prev((key, key)), key, key, WRAPPED)
                self.assertEqual([e['status'] for e in c], ['applied'])
                self.assertEqual(self.name_of(d, key), WRAPPED)

    def test_unrelated_rename_is_applied(self):
        d, x, c = self.rename(self.prev(('정산', '정산')), '정산', '정산', '정산 문서')
        self.assertEqual([e['status'] for e in c], ['applied'])

    def test_inherited_tie_does_not_defer_an_unrelated_revision_and_untying_is_applied(self):
        prev = self.prev((A, B), ('끝', '끝'))
        d, x, c = reconcile(self.SRC, {'revisions': lambda r: [revision('entities', '끝', entity('끝'), entity('끝', kind='문서'), r)]}, prev)
        self.assertEqual([e['status'] for e in c], ['applied'])
        self.assertEqual(spacing_name_rows(d), [{'index': 1, 'id': A, 'name': B}])
        d, x, c = self.rename(prev, A, B, A)
        self.assertEqual([e['status'] for e in c], ['applied'])
        self.assertEqual(spacing_name_rows(d), [])

    def test_a_bundle_that_also_ties_an_alias_keeps_the_alias_reason(self):
        src = f'{B} 정산 {A}'
        prev = stored(src, [entity('정산', quote='정산'), entity(A, quote=A)], [{'alias': B, 'entity': '정산', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision('entities', '정산', entity('정산'), entity('정산', A), r),
                                                          revision('entities', A, entity(A), entity(A, B), r)]}, prev)
        self.assertEqual([e['status'] for e in c], ['deferred', 'deferred'])
        self.assertEqual({e['reason'] for e in c}, {SPACING_REFUSAL})


class CrossChunkLimit(unittest.TestCase):
    """Documented limit (#884): once a name merged by ID has left the dictionary, a later chunk links its spacing variant."""
    SRC = f'계획A는 {A}이며 정산 문서이다. ENT-2는 {B}이며 정산 문서이다.'

    def test_after_a_name_left_the_dictionary_a_later_chunk_links_its_spacing_variant(self):
        first, x, c = reconcile(self.SRC, {'entities': [entity('계획A', A), entity('정산 문서')],
                                           'aliases': [{'alias': '계획A', 'entity': '정산 문서'}]})
        self.assertEqual((ids(first), x), (['정산 문서'], []))
        spellings = {s for e in first['entities'] for s in (e['id'], e['name'])} | {a['alias'] for a in first['aliases']}
        self.assertNotIn(A, spellings)
        self.assertTrue(all(A in a['quote'] for a in first['aliases']))  # the merged name survives only in the alias quote
        for output in ({'aliases': [{'alias': B, 'entity': '정산 문서'}]},
                       {'entities': [entity('ENT-2', B)], 'aliases': [{'alias': 'ENT-2', 'entity': '정산 문서'}]}):
            with self.subTest(output=output):
                d, x, c = reconcile(self.SRC, output, copy.deepcopy(first))
                self.assertEqual(x, [])
                self.assertIn(output['aliases'][0]['alias'], [alias for alias, _ in aliases(d)])
                self.assertEqual(ids(d), ['정산 문서'])
                self.assertTrue(all(e['status'] == 'applied' for e in c))


class ExtractionLimit(unittest.TestCase):
    """Documented limit (#885): a fact endpoint's spelling is not checked against its quote."""
    SRC = f'{B} 정산 문서를 만든다.'
    D = {'entities': [entity(A, quote=SRC), entity('정산 문서', quote=SRC)], 'aliases': [],
         'relations': [{'id': 'makes', 'description': 'd', 'subject_types': ['산출물'], 'object_types': ['산출물'], 'quote': SRC}]}

    def test_extraction_stores_an_approved_id_whose_spelling_is_absent_from_the_quote(self):
        fact = dictionary_fact({'subject': A, 'predicate': 'makes', 'object': '정산 문서', 'quote': self.SRC}, self.SRC, self.D)
        self.assertEqual(fact['subject'], A)
        self.assertNotIn(A, fact['quote'])


class InvisibleCharacterLimit(unittest.TestCase):
    """Documented limit (#886): a spelling with a non-whitespace character on one side only is neither refused nor listed."""
    SRC = f'{A} {B} 납품 {ZWSP}계획 납품{FILLER}계획 끝'

    def test_model_alias_with_an_invisible_character_is_not_refused(self):
        d, x, _ = reconcile(self.SRC, {'entities': [entity(B)], 'aliases': [{'alias': f'납품 {ZWSP}계획', 'entity': B}]})
        self.assertEqual(aliases(d), [(f'납품 {ZWSP}계획', B)])
        self.assertEqual(codes(x), [])

    def test_rename_with_an_invisible_or_filler_character_is_applied_and_not_listed(self):
        for key, new in ((B, f'납품 {ZWSP}계획'), (A, f'납품{FILLER}계획'), (B, f'납품{FILLER}계획')):
            with self.subTest(key=key, new=new):
                prev = stored(self.SRC, [entity(key, quote=self.SRC)], [])
                d, x, c = reconcile(self.SRC, {'revisions': lambda r, key=key, new=new: [revision('entities', key, entity(key), entity(key, new), r)]}, prev)
                self.assertEqual([e['status'] for e in c], ['applied'])
                self.assertEqual(spacing_name_rows(d), [])


class ResumedRecord(unittest.TestCase):
    """A resumed checkpoint prefix keeps the spacing pairs it holds, and they are listed.

    The first test's pair comes from a generated entity row, which no rule refuses. The second test's pair comes from a
    `revisions` rename applied while the rename rule was disabled, as in a record written before the rule existed; the
    resume reuses it, so the prefix is not re-checked by that rule.
    """
    SRC = f'{A}은 {B}이라고도 한다.'.ljust(4000) + f'{A}은 {B}이라고도 한다.'

    def setUp(self):
        workflow.Workflow.setUp(self)
        self.addCleanup(workflow.Workflow.tearDown, self)

    def run_job(self, doc, outputs):
        pending = iter(outputs)
        calls = []

        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                payload = json.loads(prompt.split('입력(JSON):\n', 1)[1])
                calls.append(payload['offset'])
                value = next(pending)
                if isinstance(value, Exception):
                    raise value
                return value(payload)
        service = Service(self.store, self.root, Fake())
        job = service.start(doc['id'], 'agy', '', 'ontology', doc['version'], doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(job['id']), calls

    def test_a_resumed_prefix_holding_a_spacing_pair_is_reused_and_listed(self):
        def tied(payload):
            ref = [payload['sources'][0]['id'], payload['sources'][-1]['id']]
            return {'entities': [{'id': A, 'name': B, 'type': '산출물', 'quote_ref': ref}], 'aliases': [], 'relations': []}
        doc = self.store.register('g', 'g.md', self.SRC.encode(), self.SRC)
        first, _ = self.run_job(doc, [tied, TimeoutError('fixture timeout')])
        self.assertEqual(first['status'], 'error')
        second, calls = self.run_job(doc, [lambda payload: {'entities': [], 'aliases': [], 'relations': []}])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertEqual(calls, [4000])
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(spacing_name_rows(item['payload']), [{'index': 1, 'id': A, 'name': B}])

    def test_a_resumed_prefix_holding_an_applied_spacing_rename_is_reused_and_listed(self):
        def declared(payload):
            ref = [payload['sources'][0]['id'], payload['sources'][-1]['id']]
            return {'entities': [{'id': A, 'name': A, 'type': '산출물', 'quote_ref': ref}], 'aliases': [], 'relations': []}

        def renamed(payload):
            ref = [payload['sources'][0]['id'], payload['sources'][-1]['id']]
            return {'entities': [], 'aliases': [], 'relations': [], 'revisions': [{
                'kind': 'entities', 'key': A, 'before': {'id': A, 'name': A, 'type': '산출물'},
                'after': {'id': A, 'name': B, 'type': '산출물', 'quote_ref': ref}, 'reason': 'r'}]}
        source = f'{A}은 사업 문서이다.'.ljust(4000) + f'{A}은 {B}이라고도 한다.'.ljust(4000) + '끝 문장이다.'
        doc = self.store.register('g', 'g.md', source.encode(), source)
        with patch('verinote.dictionary_reconciliation.spacing_name_rows', return_value=[]):
            first, _ = self.run_job(doc, [declared, renamed, TimeoutError('fixture timeout')])
        self.assertEqual(first['status'], 'error')
        second, calls = self.run_job(doc, [lambda payload: {'entities': [], 'aliases': [], 'relations': []}])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertEqual(calls, [8000])
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(spacing_name_rows(item['payload']), [{'index': 1, 'id': A, 'name': B}])


if __name__ == '__main__':
    unittest.main()
