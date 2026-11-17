"""The fact review card warns when an endpoint's spellings are in the quote only with different spacing. All spellings are synthetic."""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from verinote.dictionary_reconciliation import (
    OUT_OF_BUDGET,
    fact_endpoint_index,
    fact_spacing_warnings,
    new_request_budget,
    quote_spacing_variant,
)
from verinote.server import Server
from verinote.service import Service
from verinote.store import Store

ROOT = Path(__file__).resolve().parents[1]
NL = '\n'
PLAN = '납품 계획'
FILE = '정산 문서'


def pin(entities, aliases=()):
    return {'payload': {'entities': [{'id': i, 'name': n, 'type': '산출물'} for i, n in entities],
                        'aliases': [{'alias': a, 'entity': e} for a, e in aliases], 'relations': []}}


def warn(subject, obj, quote, entities, aliases=()):
    return fact_spacing_warnings({'subject': subject, 'predicate': 'p', 'object': obj, 'quote': quote}, fact_endpoint_index(pin(entities, aliases)))


def fields(result):
    return [(w['field'], w['spelling']) for w in result['warnings']]


class Variant(unittest.TestCase):
    def test_run_widths_and_added_or_missing_spaces_are_variants(self):
        self.assertEqual(quote_spacing_variant(PLAN, '납품  계획이 왔다'), '납품  계획')
        self.assertEqual(quote_spacing_variant(PLAN, '납품계획이 왔다'), '납품계획')
        self.assertEqual(quote_spacing_variant('납품계획', '납품 계획이 왔다'), '납품 계획')
        self.assertEqual(quote_spacing_variant('가 나 다', '가나 다'), '가나 다')

    def test_exact_absent_and_different_text_give_nothing(self):
        for quote in ('납품 계획이 왔다', '전혀 다른 문장', '납품 일정'):
            self.assertIsNone(quote_spacing_variant(PLAN, quote))

    def test_a_line_break_alone_is_ignored_but_a_mixed_passage_counts(self):
        self.assertIsNone(quote_spacing_variant(PLAN, f'납품{NL}계획'))
        self.assertIsNone(quote_spacing_variant(PLAN, f'납품 {NL} 계획'))
        self.assertEqual(quote_spacing_variant('가 나 다', f'가{NL}나다'), f'가{NL}나다')

    def test_an_ignored_passage_does_not_hide_a_later_variant(self):
        self.assertEqual(quote_spacing_variant(PLAN, f'납품{NL}계획 그리고 납품계획'), '납품계획')

    def test_invisible_characters_alone_are_not_a_spacing_difference(self):
        self.assertIsNone(quote_spacing_variant(PLAN, '납품 \u200b계획'))
        self.assertEqual(quote_spacing_variant('납품계획', '납품\u200b 계획'), '납품\u200b 계획')

    def test_a_single_character_or_empty_spelling_has_no_variant(self):
        self.assertIsNone(quote_spacing_variant('가', '가'))
        self.assertIsNone(quote_spacing_variant('  ', '가 나'))


class Warnings(unittest.TestCase):
    ENTITIES = [(PLAN, PLAN), ('file', FILE)]

    def test_exact_spelling_of_any_kind_suppresses_the_warning(self):
        self.assertEqual(warn(PLAN, 'file', f'{PLAN} 계획 그리고 {FILE}', self.ENTITIES)['warnings'], [])
        self.assertEqual(warn('file', 'file', f'{FILE} 그리고 정산문서', self.ENTITIES, [('정산문서', 'file')])['warnings'], [])

    def test_an_exact_occurrence_after_a_variant_still_suppresses(self):
        self.assertEqual(warn(PLAN, PLAN, f'납품계획 그리고 {PLAN}', self.ENTITIES)['warnings'], [])

    def test_absent_spellings_give_no_warning(self):
        result = warn(PLAN, 'file', '무관한 인용', self.ENTITIES)
        self.assertEqual(result, {'available': True, 'incomplete': False, 'warnings': []})

    def test_each_endpoint_is_judged_separately(self):
        result = warn(PLAN, 'file', '납품계획이 정산문서를 보냈다', self.ENTITIES)
        self.assertEqual(fields(result), [('subject', PLAN), ('object', FILE)])
        self.assertEqual([w['found'] for w in result['warnings']], ['납품계획', '정산문서'])
        self.assertEqual(fields(warn(PLAN, 'file', f'납품계획이 {FILE}를 보냈다', self.ENTITIES)), [('subject', PLAN)])

    def test_an_alias_endpoint_resolves_to_its_entity_and_alias_spellings_are_checked(self):
        result = warn('알림', 'file', '납품계획이 왔다', self.ENTITIES, [('알림', PLAN)])
        self.assertEqual(fields(result), [('subject', PLAN)])
        result = warn(PLAN, 'file', '별칭 표기가 왔다', self.ENTITIES, [('별칭 표기', PLAN)])
        self.assertEqual(result['warnings'], [])
        result = warn(PLAN, 'file', '별칭표기가 왔다', self.ENTITIES, [('별칭 표기', PLAN)])
        self.assertEqual(fields(result), [('subject', '별칭 표기')])

    def test_an_endpoint_is_never_found_by_a_space_stripped_spelling(self):
        self.assertEqual(warn('납품계획', 'file', '납품 계획이 왔다', self.ENTITIES), {'available': True, 'incomplete': False, 'warnings': []})

    def test_unreadable_pin_or_payload_is_unavailable(self):
        for bad in (None, {}, {'payload': None}, {'payload': {'entities': [{'id': 'x'}], 'aliases': []}}):
            self.assertIsNone(fact_endpoint_index(bad))
        self.assertEqual(fact_spacing_warnings({'subject': 'a'}, None), {'available': False, 'warnings': []})
        index = fact_endpoint_index(pin(self.ENTITIES))
        self.assertEqual(fact_spacing_warnings({'subject': ['x'], 'object': 'file', 'quote': 'q'}, index), {'available': False, 'warnings': []})
        self.assertEqual(fact_spacing_warnings('not a fact', index), {'available': False, 'warnings': []})

    def test_malformed_dictionary_rows_make_the_pin_unavailable(self):
        good = pin(self.ENTITIES)
        self.assertIsNotNone(fact_endpoint_index(good))
        for entities, aliases in (([{'id': 'a', 'name': ''}], []), ([{'id': '', 'name': 'a'}], []), (['x'], []), ([{'id': 'a', 'name': 3}], []),
                                  ([{'id': 'a', 'name': 'a'}], [{'alias': '', 'entity': 'a'}]), ([{'id': 'a', 'name': 'a'}], [{'alias': 'b', 'entity': 'zz'}]),
                                  ([{'id': 'a', 'name': 'a'}], ['x']), ([{'id': 'a', 'name': 'a'}], [{'alias': 4, 'entity': 'a'}]), ({}, [])):
            self.assertIsNone(fact_endpoint_index({'payload': {'entities': entities, 'aliases': aliases}}), (entities, aliases))

    def test_an_empty_or_blank_only_spelling_makes_the_pin_unavailable_and_cannot_suppress(self):
        for blank_name in ('', ' ', NL, '\u200b', '\u3164', ' \u200b\u3164'):
            for entities, aliases in (([('a', blank_name)], []), ([(blank_name, 'a')], []), ([('a', 'a')], [(blank_name, 'a')])):
                self.assertIsNone(fact_endpoint_index(pin(entities, aliases)), (blank_name, entities, aliases))
            self.assertEqual(fact_spacing_warnings({'subject': 'a', 'object': 'a', 'quote': '납품계획'}, fact_endpoint_index(pin([('a', blank_name)]))),
                             {'available': False, 'warnings': []})

    def test_the_fast_compact_form_equals_the_blank_definition_for_every_code_point(self):
        from verinote.dictionary_reconciliation import blank, compact_fast
        text = ''.join(chr(c) for c in range(0x110000) if not 0xD800 <= c < 0xE000)
        self.assertEqual(compact_fast(text), ''.join(c for c in text if not blank(c)))

    def timed(self, subject, quote, entities, aliases):
        began = time.monotonic()
        index = fact_endpoint_index(pin(entities, aliases))
        built = time.monotonic()
        result = fact_spacing_warnings({'subject': subject, 'predicate': 'p', 'object': subject, 'quote': quote}, index)
        self.assertLess(time.monotonic() - built, 0.5)
        self.assertLess(built - began, 0.5)
        return result

    def test_many_aliases_on_a_long_quote_stay_fast(self):
        quote = '가 나 다 ' * 1600
        aliases = [(f'가나다{n}', PLAN) for n in range(500)] + [(f'가 나 다 {n}', PLAN) for n in range(500)]
        self.assertEqual(self.timed(PLAN, quote, [(PLAN, PLAN)], aliases)['warnings'], [])

    def test_thousands_of_long_aliases_on_a_long_quote_stay_fast(self):
        quote = ('가나다라 ' * 1600)[:8000]
        for count in (6000, 10000):
            aliases = [(f'{n:05d}' + '마바사' * 18, 'x') for n in range(count)]
            result = self.timed('x', quote, [('x', 'x')], aliases)
            self.assertEqual(result, {'available': True, 'incomplete': False, 'warnings': []})

    def test_a_repeated_prefix_quote_stays_fast(self):
        quote = 'a' * 8000
        aliases = [('a' * 60 + 'b' * n, 'x') for n in range(1, 40)] + [('a' * n, 'x') for n in range(2, 40)]
        self.assertEqual(self.timed('x', quote, [('x', 'x')], aliases)['warnings'], [])
        self.timed('x', 'a ' * 4000, [('x', 'x')], [('a' * n, 'x') for n in range(2, 60)])

    def test_long_blank_runs_stay_fast_and_still_warn(self):
        quote = '납품' + ' ' * 3000 + '계획' + ' ' * 3000 + '끝'
        result = self.timed(PLAN, quote, [(PLAN, PLAN)], [])
        self.assertEqual(fields(result), [('subject', PLAN), ('object', PLAN)])
        self.assertEqual(self.timed(PLAN, '\u200b' * 7990 + '납품계획', [(PLAN, PLAN)], [])['warnings'][0]['found'], '납품계획')

    def test_a_variant_after_many_ignored_line_break_occurrences_is_still_found(self):
        quote = f'납품{NL}계획 ' * 400 + '납품계획'
        result = self.timed(PLAN, quote, [(PLAN, PLAN)], [])
        self.assertEqual((result['incomplete'], [w['found'] for w in result['warnings']]), (False, ['납품계획', '납품계획']))

    def test_a_dense_1600_character_quote_gives_a_definite_warning(self):
        quote = 'a' + NL + 'a' + NL + ('a' + NL) * 780 + 'a  a'
        self.assertLess(len(quote), 1700)
        aliases = [('a' + ' ' * k + 'a', 'x') for k in [1] + list(range(3, 40))]
        result = self.timed('x', quote, [('x', 'x')], aliases)
        self.assertFalse(result['incomplete'])
        self.assertEqual({w['found'] for w in result['warnings']}, {'a  a'})
        self.assertEqual(len([w for w in result['warnings'] if w['field'] == 'subject']), len(aliases))

    def test_a_realistic_request_is_never_incomplete(self):
        index = fact_endpoint_index(pin([(PLAN, PLAN)], [(f'별칭 {n}', PLAN) for n in range(200)]))
        budget = new_request_budget()
        quote = '납품계획이 왔다. ' * 120
        for _ in range(40):
            result = fact_spacing_warnings({'subject': PLAN, 'predicate': 'p', 'object': '별칭 3', 'quote': quote}, index, budget)
            self.assertFalse(result['incomplete'])
            self.assertEqual(fields(result), [('subject', PLAN), ('object', PLAN)])

    def test_the_request_budget_bounds_many_facts_and_marks_the_rest_incomplete(self):
        aliases = [(f'{n:05d}' + '마바사' * 18, 'x') for n in range(10000)]
        index = fact_endpoint_index(pin([('x', 'x')], aliases))
        quote = ('가나다라 ' * 1600)[:8000]
        budget = new_request_budget()
        began = time.monotonic()
        results = [fact_spacing_warnings({'subject': 'x', 'predicate': 'p', 'object': 'x', 'quote': quote}, index, budget) for _ in range(50)]
        self.assertLess(time.monotonic() - began, 1.5)
        flags = [r['incomplete'] for r in results]
        self.assertEqual(flags, sorted(flags))
        self.assertFalse(flags[0])
        self.assertTrue(flags[-1])
        self.assertTrue(all(r['available'] for r in results))

    def test_index_construction_is_charged_to_the_request_budget(self):
        quote = ('가나다라 ' * 1600)[:8000]
        big = pin([('x', 'x')], [(f'{n:05d}' + '마바사' * 18, 'x') for n in range(10000)])
        budget = new_request_budget()
        began = time.monotonic()
        flags = []
        for _ in range(10):
            index = fact_endpoint_index(big, budget)
            flags.append(fact_spacing_warnings({'subject': 'x', 'predicate': 'p', 'object': 'x', 'quote': quote}, index, budget)['incomplete'])
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(flags, sorted(flags))
        self.assertFalse(flags[0])
        self.assertTrue(flags[-1])
        spent = new_request_budget()
        spent['steps'] = 5
        self.assertIs(fact_endpoint_index(big, spent), OUT_OF_BUDGET)
        self.assertEqual(spent['steps'], 5)
        self.assertEqual(fact_spacing_warnings({'subject': 'x', 'object': 'x', 'quote': quote}, OUT_OF_BUDGET),
                         {'available': True, 'incomplete': True, 'warnings': []})

    def test_a_normal_multi_run_request_is_never_incomplete_and_unchanged(self):
        budget = new_request_budget()
        for run in range(6):
            index = fact_endpoint_index(pin([(PLAN, PLAN), ('file', FILE)], [(f'별칭 {n}', PLAN) for n in range(100)]), budget)
            for _ in range(5):
                result = fact_spacing_warnings({'subject': PLAN, 'predicate': 'p', 'object': 'file', 'quote': '납품계획이 정산문서를 보냈다'}, index, budget)
                self.assertEqual((result['incomplete'], fields(result)), (False, [('subject', PLAN), ('object', FILE)]), run)

    def test_an_exhausted_budget_is_reported_as_incomplete_with_what_was_found(self):
        quote = ('a' + NL) * 4000
        aliases = [('a' + ' ' * k + 'a', 'x') for k in range(1, 30)]
        result = self.timed('x', quote, [('x', 'x')], aliases)
        self.assertEqual((result['available'], result['incomplete'], result['warnings']), (True, True, []))
        found = self.timed('x', 'ab ' + quote, [('x', 'x')], [('a b', 'x')] + aliases)
        self.assertEqual((found['incomplete'], [w['found'] for w in found['warnings']]), (True, ['ab']))


class Served(unittest.TestCase):
    HEAD = f'{PLAN}과 {FILE} 목록.'
    SOURCE = f'{HEAD} 납품계획이 정산 문서를 보냈다. 납품{NL}계획 메모.'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'db')
        self.doc = self.store.register('d', 'x.md', self.SOURCE.encode(), self.SOURCE)
        dictionary = {'entities': [{'id': n, 'name': n, 'type': '산출물', 'quote': self.HEAD} for n in (PLAN, FILE)],
                      'aliases': [], 'relations': [{'id': 'sends', 'description': '보낸다', 'subject_types': ['산출물'], 'object_types': ['산출물'], 'quote': self.HEAD}]}
        run = self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1)
        self.store.run_status(run['id'], 'running')
        self.store.finish_run(run['id'], dictionary=dictionary)
        item = self.ontology()
        self.store.review(item['id'], item['revision'], 'approved', 'R', 'ok')
        self.run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
        self.store.run_status(self.run['id'], 'running')
        quote = '납품계획이 정산 문서를 보냈다.'
        self.store.finish_run(self.run['id'], {'title': 't', 'claims': [{'heading': 'h', 'text': quote, 'quote': quote}]},
                              [{'subject': PLAN, 'predicate': 'sends', 'object': FILE, 'quote': quote}])
        service = Service(self.store, self.root)
        self.server = Server(('127.0.0.1', 0), self.store, service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(service.pool.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def ontology(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def get(self):
        with urlopen(f"http://127.0.0.1:{self.server.server_port}/api/documents/{self.doc['id']}") as response:
            return json.loads(response.read())

    def fact(self, doc=None):
        return next(i for i in (doc or self.get())['items'] if i['kind'] == 'fact')

    def test_document_get_carries_the_warning_and_storage_is_unchanged(self):
        before = self.store.document(self.doc['id'])
        fact = self.fact()
        self.assertEqual(fact['quote_spacing']['warnings'], [{'field': 'subject', 'entity': PLAN, 'spelling': PLAN, 'found': '납품계획'}])
        self.assertEqual(fact['payload']['subject'], PLAN)
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM items ORDER BY id')]
        self.get()
        with self.store.connect() as db:
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM items ORDER BY id')], rows)
        self.assertEqual({i['id'] for i in before['items']}, {i['id'] for i in self.get()['items']})
        self.assertEqual(self.store.review(fact['id'], fact['revision'], 'approved', 'R', 'ok')['status'], 'approved')
        self.assertEqual(len(self.store.snapshot()['facts']), 1)

    def test_an_exhausted_budget_reaches_the_document_get_as_incomplete(self):
        from unittest.mock import patch
        with patch('verinote.dictionary_reconciliation.MAX_FACT_STEPS', 3):
            self.assertEqual(self.fact()['quote_spacing'], {'available': True, 'incomplete': True, 'warnings': []})
        self.assertFalse(self.fact()['quote_spacing']['incomplete'])

    def test_an_index_that_does_not_fit_the_request_budget_reaches_the_document_get_as_incomplete(self):
        from unittest.mock import patch
        with patch('verinote.dictionary_reconciliation.MAX_REQUEST_STEPS', 10):
            self.assertEqual(self.fact()['quote_spacing'], {'available': True, 'incomplete': True, 'warnings': []})

    def test_a_revised_fact_is_judged_on_its_new_quote(self):
        fact = self.fact()
        payload = dict(fact['payload'], quote='정산 문서를 보냈다.')
        self.store.review(fact['id'], fact['revision'], 'revise', 'R', 'fixed', payload)
        self.assertEqual(self.fact()['quote_spacing']['warnings'], [])
        payload['quote'] = '납품계획이 정산 문서를'
        fact = self.fact()
        self.store.review(fact['id'], fact['revision'], 'revise', 'R', 'again', payload)
        self.assertEqual([w['field'] for w in self.fact()['quote_spacing']['warnings']], ['subject'])

    def test_a_run_without_a_readable_pin_is_unavailable_and_get_still_works(self):
        with self.store.connect(True) as db:
            stored = json.loads(db.execute('SELECT ontology FROM runs WHERE id=?', (self.run['id'],)).fetchone()[0])
            db.execute('UPDATE runs SET ontology=? WHERE id=?', (json.dumps(stored | {'payload': {'entities': 3, 'aliases': []}}), self.run['id']))
        self.assertEqual(self.fact()['quote_spacing'], {'available': False, 'warnings': []})
        with self.store.connect(True) as db:
            db.execute('UPDATE runs SET ontology=NULL WHERE id=?', (self.run['id'],))
        self.assertFalse(self.fact()['quote_spacing']['available'])


SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const cut=(a,b)=>{const i=source.indexOf(a),j=source.indexOf(b,i);assert(i>=0&&j>i,a);return source.slice(i,j);};
const ctx=vm.createContext({console,$:()=>({}),document:{addEventListener(){}}});
vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
vm.runInContext('const labels={pending:"검토 대기"};function badge(){return "";}function bulkItemLabel(){return "";}function bulkItemLabelHtml(){return "";}function renderRelationTypeWarnings(){return "";}',ctx);
vm.runInContext(cut('function visibleSpelling(','let termState'),ctx);
vm.runInContext(cut('function renderQuoteSpacing(','function renderProgress('),ctx);
const card=quote_spacing=>{ctx.item={id:'i',kind:'fact',status:'pending',revision:1,run_id:'r',payload:{subject:'<b>납품 계획</b>',predicate:'p',object:'o',quote:'q'},quote_spacing};return vm.runInContext('renderItem(item)',ctx);};
const warning={available:true,warnings:[{field:'subject',entity:'e',spelling:'납품 계획<i>',found:'납품  계획<i>'},{field:'object',entity:'o',spelling:'a',found:'b'}]};
const html=card(warning);
assert(html.includes('인용에 띄어쓰기만 다른 표기가 있습니다'),html);
assert(html.includes('주어 납품<span class="ws" title="공백">␣</span>계획&lt;i&gt; → 인용 납품<span class="ws" title="공백">␣</span><span class="ws" title="공백">␣</span>계획&lt;i&gt;'),html);
assert(html.includes('목적어 a → 인용 b'));assert(!html.includes('<i>'));
const partial=card({available:true,incomplete:true,warnings:[]});
assert(partial.includes('일부 표기는 확인하지 못했습니다')&&!partial.includes('인용에 띄어쓰기만'),partial);
assert(card({...warning,incomplete:true}).includes('일부 표기는 확인하지 못했습니다')&&card({...warning,incomplete:true}).includes('주어 '));
assert(!card(warning).includes('일부 표기'));
for(const none of [undefined,null,{available:false,warnings:[]},{available:true,incomplete:false,warnings:[]}])assert(!card(none).includes('띄어쓰기만 다른 표기'));
'''


class Card(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_fact_card_shows_the_warning_through_visible_spelling(self):
        result = subprocess.run(['node', '-e', SCRIPT], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
