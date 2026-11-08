"""Which exclusions a dictionary addition could fix, read against the dictionary that excluded them. Spellings are synthetic."""
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import test_quote_exclusions as quotes
import test_workflow as workflow
from test_extraction_checkpoint import FakeCLI
from test_quote_exclusions import (
    CLAIM1,
    FACTS,
    OFF_DICTIONARY,
    bad,
    padded,
    reply,
    resp,
)
from test_workflow import approved_dictionary, extraction_response

from verinote.extraction_coverage import exclusion_allowed, exclusion_terms, read_report
from verinote.ontology import VocabularyError, dictionary_terms
from verinote.service import Service

S = '납품 계획을 세운다. 검수 활동을 한다. 정산 문서를 낸다. 납품 계획서라고 부른다. 납품계획 표기. 끝.'
D = {'entities': [{'id': '납품 계획', 'name': '납품 계획', 'type': '산출물', 'quote': '납품 계획을 세운다.'},
                  {'id': '검수 활동', 'name': '검수 활동', 'type': '활동', 'quote': '검수 활동을 한다.'},
                  {'id': '정산 문서', 'name': '정산 문서', 'type': '문서', 'quote': '정산 문서를 낸다.'}],
     'aliases': [{'alias': '납품 계획서', 'entity': '납품 계획', 'quote': '납품 계획을 세운다. 검수 활동을 한다. 정산 문서를 낸다. 납품 계획서'}],
     'relations': [{'id': 'produces', 'description': 'd', 'subject_types': ['활동'], 'object_types': ['산출물', '결과'], 'quote': '납품 계획을 세운다.'}]}
D['entities'].append({'id': '<결과"\'>', 'name': '끝', 'type': '결과', 'quote': '끝.'})
GOOD = {'subject': '검수 활동', 'predicate': 'produces', 'object': '납품 계획', 'quote': '검수 활동을 한다.'}


def row(code, subject, obj):
    return {'code': code, 'subject': subject, 'predicate': 'produces', 'object': obj, 'quote': 'q'}


def term(rows, index, field):
    return next(t for t in exclusion_terms(rows, D)[index] if t['field'] == field)


class Terms(unittest.TestCase):
    def test_both_fields_unknown(self):
        terms = exclusion_terms([row('unknown_entity', '없는 주어', '없는 목적어')], D)[0]
        self.assertEqual([(t['field'], t['known'], t['type'], t['type_fits']) for t in terms],
                         [('subject', False, None, None), ('object', False, None, None)])

    def test_unknown_subject_beside_a_mistyped_object_reports_both(self):
        terms = exclusion_terms([row('unknown_entity', '없는 주어', '정산 문서')], D)[0]
        self.assertFalse(terms[0]['known'])
        self.assertEqual((terms[1]['known'], terms[1]['type'], terms[1]['type_fits']), (True, '문서', False))

    def test_type_mismatch_marks_only_the_failing_field(self):
        terms = exclusion_terms([row('type_mismatch', '검수 활동', '정산 문서')], D)[0]
        self.assertEqual([t['type_fits'] for t in terms], [True, False])
        self.assertNotIn('allowed', terms[1])
        self.assertEqual(exclusion_allowed([row('type_mismatch', '검수 활동', '정산 문서')], D),
                         {'produces': {'subject': D['relations'][0]['subject_types'], 'object': D['relations'][0]['object_types']}})

    def test_alias_resolves_to_its_entity_and_type(self):
        t = term([row('type_mismatch', '납품 계획서', '납품 계획')], 0, 'subject')
        self.assertEqual((t['spelling'], t['entity'], t['type'], t['type_fits']), ('납품 계획서', '납품 계획', '산출물', False))

    def test_spelling_loses_surrounding_whitespace_only(self):
        t = term([row('unknown_entity', '  납품계획 표기\n', '납품 계획')], 0, 'subject')
        self.assertEqual(t['spelling'], '납품계획 표기')
        self.assertFalse(t['known'])

    def test_name_of_lists_entities_whose_name_equals_the_spelling_exactly(self):
        value = copy.deepcopy(D)
        value['entities'].append({'id': 'E-9', 'name': '정산 서류', 'type': '문서', 'quote': '끝.'})
        def named(spelling):
            return exclusion_terms([row('unknown_entity', '검수 활동', spelling)], value)[0][1]['name_of']
        self.assertEqual((named(' 정산 서류\n'), named('정산서류'), named('정산  서류')), (['E-9'], [], []))
        self.assertEqual(named('검수 활동'), [])

    def test_a_spacing_variant_stays_unknown(self):
        self.assertFalse(term([row('unknown_entity', '검수 활동', '납품계획')], 0, 'object')['known'])

    def test_quote_exclusion_has_no_reading_and_keeps_its_position(self):
        rows = [row('unverifiable_quote', '검수 활동', '납품 계획') | {'path': 'facts.0.quote'}, row('unknown_entity', '없음', '납품 계획')]
        terms = exclusion_terms(rows, D)
        self.assertIsNone(terms[0])
        self.assertEqual(terms[1][0]['spelling'], '없음')

    def test_reading_agrees_with_the_code_dictionary_terms_gives(self):
        for subject in ('검수 활동', '없는 주어', '납품 계획', '납품 계획서'):
            for obj in ('납품 계획', '없는 목적어', '정산 문서', '<결과"\'>'):
                fact = {'subject': subject, 'predicate': 'produces', 'object': obj}
                try:
                    dictionary_terms(fact, D)
                    code = None
                except VocabularyError as exc:
                    code = exc.code
                terms = exclusion_terms([row('unknown_entity', subject, obj)], D)[0]
                derived = ('unknown_entity' if not all(t['known'] for t in terms)
                           else 'type_mismatch' if not all(t['type_fits'] for t in terms) else None)
                self.assertEqual(derived, code, (subject, obj))


class Report(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def run_with(self, facts):
        doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, doc['id'], D)
        cli = FakeCLI([extraction_response(facts, D, {'title': 't', 'claims': [{'heading': 'h', 'text': 'x', 'quote': '끝.'}]})])
        service = Service(self.store, self.root, cli)
        run = service.start(doc['id'], 'copilot', 'm', expected_version=doc['version'], expected_generation=doc['generation'])
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'success', self.store.run(run['id'])['detail'])
        return doc, run, service

    def facts(self):
        return [GOOD, GOOD | {'object': '없는 표기'}, GOOD | {'object': '정산 문서'}, GOOD | {'subject': '정산 문서'}]

    def test_counts_separate_spelling_from_type(self):
        _, run, _ = self.run_with(self.facts())
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['excluded_fact_count'], report['excluded_spelling_count'], report['excluded_type_count']), (3, 1, 2))

    def test_terms_parallel_the_rows_and_stay_out_of_the_stored_file(self):
        _, run, _ = self.run_with(self.facts())
        chunk = read_report(self.root, self.store, run['id'])['chunks'][0]
        self.assertEqual(len(chunk['excluded_fact_terms']), len(chunk['excluded_facts']))
        self.assertEqual(chunk['excluded_fact_terms'][0][1]['spelling'], chunk['excluded_facts'][0]['object'])
        allowed = chunk['excluded_fact_allowed']
        self.assertEqual(list(allowed), ['produces'])
        self.assertEqual({k: sorted(v) for k, v in allowed['produces'].items()}, {'subject': ['활동'], 'object': ['결과', '산출물']})
        stored = json.loads((self.root / 'runs' / run['id'] / 'extraction-coverage.json').read_text())
        self.assertNotIn('excluded_fact_terms', stored['chunks'][0])

    def test_a_chunk_without_exclusions_has_no_terms_key(self):
        _, run, _ = self.run_with([GOOD])
        chunk = read_report(self.root, self.store, run['id'])['chunks'][0]
        self.assertNotIn('excluded_fact_terms', chunk)
        self.assertNotIn('excluded_fact_allowed', chunk)

    def test_summary_list_carries_counts_without_terms(self):
        _, run, service = self.run_with(self.facts())
        listed = service.with_usage('runs', self.store.run(run['id']), coverage_details=False)['coverage']
        self.assertEqual(listed['excluded_spelling_count'], 1)
        self.assertNotIn('chunks', listed)

    def test_bad_stored_row_is_unavailable_not_an_exception(self):
        _, run, _ = self.run_with(self.facts())
        path = self.root / 'runs' / run['id'] / 'extraction-coverage.json'
        stored = json.loads(path.read_text())
        stored['chunks'][0]['excluded_facts'][0]['subject'] = 7
        path.write_text(json.dumps(stored))
        self.assertFalse(read_report(self.root, self.store, run['id'])['available'])

    def test_status_and_run_id_are_reported_for_the_add_control(self):
        doc, run, _ = self.run_with(self.facts())
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['current_dictionary_status'], report['run_id']), ('approved', run['id']))
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(item['id'], item['revision'], 'rejected', 'R', '반려')
        self.assertEqual(read_report(self.root, self.store, run['id'])['current_dictionary_status'], 'rejected')

    def test_revision_and_version_are_reported_as_they_change(self):
        doc, run, _ = self.run_with(self.facts())
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['dictionary_revision'], report['current_dictionary_revision'], report['version_current']), (2, 2, True))
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(item['id'], item['revision'], 'pending', 'R', '재검토')
        self.assertEqual(read_report(self.root, self.store, run['id'])['current_dictionary_revision'], 3)
        self.store.register('d', 'd.md', (S + ' 추가').encode(), S + ' 추가', doc['id'], doc['version'], doc['generation'])
        self.assertFalse(read_report(self.root, self.store, run['id'])['version_current'])


APP = Path('verinote/web/app.js').read_text(encoding='utf-8')


def render(report):
    esc = APP[APP.index('const esc ='):APP.index('const labels =')]
    function = APP[APP.index('function renderCoverage('):APP.index('function usageStage(')]
    return subprocess.check_output(['node'], input=esc + function + '\nconsole.log(renderCoverage(' + json.dumps(report) + '));', text=True)


def report(rows, **extra):
    return {'available': True, 'published': True, 'validated_chunks': 1, 'total_chunks': 1, 'extracted_predicates': 1, 'approved_predicates': 1,
            'emitted_fact_count': len(rows), 'excluded_fact_count': len(rows), 'unverifiable_fact_count': 0, 'saved_fact_count': 0,
            'claim_count': 1, 'excluded_claim_count': 0, 'excluded_gap_count': 0, 'gap_count': 0, 'capped': False,
            'excluded_spelling_count': sum(r['code'] == 'unknown_entity' for r in rows), 'excluded_type_count': sum(r['code'] == 'type_mismatch' for r in rows),
            'dictionary_revision': 2, 'current_dictionary_revision': 2, 'version_current': True,
            'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                        'excluded_fact_terms': exclusion_terms(rows, D),
                        'excluded_fact_allowed': exclusion_allowed(rows, D)}]} | extra


class Screen(unittest.TestCase):
    NOT_FIXED = '별칭이나 새 개체를 추가해도 해결되지 않습니다'

    def test_type_row_says_an_addition_cannot_fix_it_and_a_spelling_row_does_not(self):
        html = render(report([row('type_mismatch', '검수 활동', '정산 문서')]))
        self.assertIn(self.NOT_FIXED, html)
        self.assertIn('목적어 정산 문서: 타입 문서, 허용 타입 산출물, 결과', html)
        self.assertNotIn('주어 검수 활동: 타입', html)
        self.assertNotIn(self.NOT_FIXED, render(report([row('unknown_entity', '검수 활동', '없는 표기')])))

    def test_spelling_row_names_the_field_and_allowed_types(self):
        html = render(report([row('unknown_entity', '검수 활동', '없는 표기')]))
        self.assertIn('목적어 없는 표기: 개체 ID·별칭에 없는 표기, 허용 타입 산출물, 결과', html)

    def test_spelling_row_warns_when_the_other_field_would_still_fail(self):
        html = render(report([row('unknown_entity', '없는 주어', '정산 문서')]))
        self.assertIn('이 표기를 추가해도 다음 타입 때문에 저장되지 않습니다. 목적어 정산 문서: 타입 문서', html)
        self.assertNotIn('이 표기를 추가해도', render(report([row('unknown_entity', '없는 주어', '납품 계획')])))

    def test_rows_read_their_own_terms_when_a_quote_exclusion_comes_first(self):
        rows = [row('unverifiable_quote', '검수 활동', '납품 계획') | {'path': 'facts.0.quote'}, row('unknown_entity', '검수 활동', '없는 표기')]
        self.assertIn('목적어 없는 표기: 개체 ID·별칭에 없는 표기', render(report(rows, excluded_fact_count=1)))

    def test_counts_shown(self):
        html = render(report([row('type_mismatch', '검수 활동', '정산 문서'), row('unknown_entity', '검수 활동', '없는 표기')]))
        self.assertIn('개체 ID·별칭에 없는 표기로 제외 1개 · 타입 불일치로 제외 1개(두 문제가 함께 있으면 표기로 셉니다)', html)

    def test_a_row_with_both_problems_is_counted_as_spelling_and_the_header_says_so(self):
        html = render(report([row('unknown_entity', '없는 주어', '정산 문서')]))
        self.assertIn('개체 ID·별칭에 없는 표기로 제외 1개 · 타입 불일치로 제외 0개(두 문제가 함께 있으면 표기로 셉니다)', html)
        self.assertIn('목적어 정산 문서: 타입 문서', html)

    def named_report(self, spelling):
        value = copy.deepcopy(D)
        value['entities'].append({'id': 'E-9', 'name': '정산 서류', 'type': '문서', 'quote': '끝.'})
        rows = [row('unknown_entity', '검수 활동', spelling)]
        return report(rows) | {'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                                           'excluded_fact_terms': exclusion_terms(rows, value),
                                           'excluded_fact_allowed': exclusion_allowed(rows, value)}]}

    def test_a_spelling_equal_to_an_entity_name_says_it_is_neither_id_nor_alias(self):
        html = render(self.named_report('정산 서류'))
        self.assertIn('목적어 정산 서류: 개체 ID·별칭에 없는 표기(개체 이름과는 같지만 ID나 별칭이 아닙니다. ID E-9)', html)

    def test_a_spacing_variant_of_a_name_is_not_reported_as_matching(self):
        for spelling in ('정산서류', '정산  서류'):
            html = render(self.named_report(spelling))
            self.assertIn('개체 ID·별칭에 없는 표기', html)
            self.assertNotIn('개체 이름과는 같지만', html)
            self.assertNotIn('E-9', html)

    def test_the_name_note_lists_five_ids_and_states_the_remainder(self):
        def html_for(count):
            value = copy.deepcopy(D)
            value['entities'] += [{'id': f'E-{n}', 'name': '정산 서류', 'type': '문서', 'quote': '끝.'} for n in range(1, count + 1)]
            rows = [row('unknown_entity', '검수 활동', '정산 서류')]
            return render(report(rows) | {'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                                                      'excluded_fact_terms': exclusion_terms(rows, value),
                                                      'excluded_fact_allowed': exclusion_allowed(rows, value)}]})
        html = html_for(7)
        self.assertIn('ID E-1, E-2, E-3, E-4, E-5 외 2개)', html)
        self.assertNotIn('E-6', html)
        self.assertNotIn(' 외 ', html_for(5))
        self.assertIn('E-5)', html_for(5))

    def test_the_entity_id_in_the_name_note_is_escaped(self):
        value = copy.deepcopy(D)
        value['entities'].append({'id': '<i>"', 'name': '정산 서류', 'type': '문서', 'quote': '끝.'})
        rows = [row('unknown_entity', '검수 활동', '정산 서류')]
        html = render(report(rows) | {'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                                                  'excluded_fact_terms': exclusion_terms(rows, value),
                                                  'excluded_fact_allowed': exclusion_allowed(rows, value)}]})
        self.assertNotIn('<i>', html)
        self.assertIn('ID &lt;i&gt;&quot;', html)

    def test_revision_note_only_when_revisions_differ(self):
        rows = [row('unknown_entity', '검수 활동', '없는 표기')]
        self.assertNotIn('실행 당시 사전', render(report(rows)))
        self.assertIn('실행 당시 사전 r2 기준이며 이 버전의 사전은 지금 r5입니다', render(report(rows, current_dictionary_revision=5)))

    def test_a_dictionary_item_that_cannot_be_found_is_stated_not_shown_as_a_revision(self):
        html = render(report([row('unknown_entity', '검수 활동', '없는 표기')], current_dictionary_revision=None))
        self.assertIn('이 버전의 사전은 지금 확인할 수 없습니다', html)
        self.assertNotIn('rnull', html)

    def test_type_row_instruction_is_qualified_and_dropped_on_an_old_version(self):
        rows = [row('type_mismatch', '검수 활동', '정산 문서')]
        html = render(report(rows))
        self.assertIn('관계의 허용 타입을 넓혀 다시 승인한 뒤 재추출하면 저장될 수 있습니다', html)
        self.assertIn('모든 청크를 새로 호출하며 되살아난다는 보장은 없습니다', html)
        self.assertNotIn('재추출', render(report(rows, version_current=False)))

    def test_old_version_note_only_on_an_old_version(self):
        rows = [row('unknown_entity', '검수 활동', '없는 표기')]
        self.assertNotIn('현재 문서 버전이 아닙니다', render(report(rows)))
        self.assertIn('현재 문서 버전이 아닙니다', render(report(rows, version_current=False)))

    def test_no_controls_in_this_unit(self):
        for rows in ([row('unknown_entity', '검수 활동', '없는 표기')], [row('type_mismatch', '검수 활동', '정산 문서')]):
            self.assertNotIn('<button', render(report(rows)))

    def test_model_written_text_is_escaped(self):
        hostile = '<img src=x onerror="y">\''
        value = copy.deepcopy(D)
        value['relations'][0]['object_types'] = ['<b>"\'']
        rows = [row('unknown_entity', hostile, '<i>')]
        html = render(report(rows) | {'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                                                  'excluded_fact_terms': exclusion_terms(rows, value),
                                                  'excluded_fact_allowed': exclusion_allowed(rows, value)}]})
        self.assertNotIn('<img', html)
        self.assertNotIn('<i>', html)
        self.assertNotIn('<b>', html)
        self.assertIn('&lt;img src=x onerror=&quot;y&quot;&gt;&#39;', html)

    def test_known_spelling_and_its_type_are_escaped(self):
        value = copy.deepcopy(D)
        value['entities'].append({'id': '<s>"', 'name': '끝', 'type': '<t>', 'quote': '끝.'})
        rows = [row('type_mismatch', '검수 활동', '<s>"')]
        html = render(report(rows) | {'chunks': [{'offset': 0, 'length': 10, 'claim_base': 0, 'coverage': [], 'dictionary_gaps': [], 'excluded_facts': rows,
                                                  'excluded_fact_terms': exclusion_terms(rows, value),
                                                  'excluded_fact_allowed': exclusion_allowed(rows, value)}]})
        self.assertNotIn('<s>', html)
        self.assertNotIn('<t>', html)
        self.assertIn('목적어 &lt;s&gt;&quot;: 타입 &lt;t&gt;', html)

    def test_older_report_without_terms_still_renders(self):
        value = report([row('unknown_entity', '검수 활동', '없는 표기')])
        del value['chunks'][0]['excluded_fact_terms']
        self.assertNotIn('목적어 없는 표기', render(value))


class Unavailable(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    run_with = Report.run_with

    def coverage_path(self):
        _, run, _ = self.run_with([GOOD, GOOD | {'object': '없는 표기'}])
        return run['id'], self.root / 'runs' / run['id'] / 'extraction-coverage.json'

    def test_missing_report_keeps_the_existing_warning(self):
        run_id, path = self.coverage_path()
        path.unlink()
        value = read_report(self.root, self.store, run_id)
        self.assertFalse(value['available'])
        self.assertIn('관계별 추출 점검 기록을 사용할 수 없습니다', render(value))

    def test_pin_without_an_id_reads_unavailable_and_the_run_list_still_works(self):
        self.check_unavailable(lambda pin: pin.pop('id'))

    def test_pin_with_an_object_id_that_the_report_repeats_reads_unavailable_and_the_run_list_still_works(self):
        self.check_unavailable(lambda pin: pin.update(id={'x': 1}), {'dictionary_id': {'x': 1}})

    def test_corrupt_allowed_types_in_the_pin_read_unavailable(self):
        self.check_unavailable(lambda pin: pin['payload']['relations'][0].update(object_types='산출물'))

    def test_allowed_types_that_are_not_a_list_of_strings_are_refused(self):
        for value_bad in ('산출물', ['산출물', 7], None):
            value = copy.deepcopy(D)
            value['relations'][0]['object_types'] = value_bad
            with self.assertRaises(ValueError):
                exclusion_terms([row('type_mismatch', '검수 활동', '정산 문서')], value)
            with self.assertRaises(ValueError):
                exclusion_allowed([row('type_mismatch', '검수 활동', '정산 문서')], value)

    def check_unavailable(self, change, identity=None):
        _, run, service = self.run_with([GOOD, GOOD | {'object': '없는 표기'}])
        if identity:
            path = self.root / 'runs' / run['id'] / 'extraction-coverage.json'
            stored = json.loads(path.read_text())
            stored['identity'].update(identity)
            path.write_text(json.dumps(stored))
        with self.store.connect() as db:
            pin = json.loads(db.execute('SELECT ontology FROM runs WHERE id=?', (run['id'],)).fetchone()[0])
            change(pin)
            db.execute('UPDATE runs SET ontology=? WHERE id=?', (json.dumps(pin), run['id']))
        self.assertFalse(read_report(self.root, self.store, run['id'])['available'])
        listed = service.with_usage('runs', self.store.run(run['id']), coverage_details=False)
        self.assertFalse(listed['coverage']['available'])

    def test_spelling_count_adds_across_chunks(self):
        other = OFF_DICTIONARY | {'object': '또 없는 개체'}
        outputs = [resp([CLAIM1], [OFF_DICTIONARY, FACTS[1]]), resp([CLAIM1], [OFF_DICTIONARY, other, FACTS[1]])]
        run, _ = quotes.Exclusions.run_it(self, quotes.Exclusions.document(self, padded(2)), outputs)
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['validated_chunks'], report['excluded_spelling_count'], report['excluded_type_count']), (2, 3, 0))

    def test_the_dictionary_lookups_are_built_once_per_report_read(self):
        import verinote.extraction_coverage as coverage
        other = OFF_DICTIONARY | {'object': '또 없는 개체'}
        outputs = [resp([CLAIM1], [OFF_DICTIONARY, FACTS[1]]), resp([CLAIM1], [OFF_DICTIONARY, other, FACTS[1]])]
        run, _ = quotes.Exclusions.run_it(self, quotes.Exclusions.document(self, padded(2)), outputs)
        with mock.patch.object(coverage, 'dictionary_index', wraps=coverage.dictionary_index) as built:
            read_report(self.root, self.store, run['id'])
        self.assertEqual(built.call_count, 1)

    def test_type_count_leaves_out_quote_exclusions(self):
        outputs = [resp([CLAIM1], [OFF_DICTIONARY, bad(FACTS[0]), FACTS[1]]), reply(('facts.0.quote', ''))]
        run, _ = quotes.Exclusions.run_it(self, quotes.Exclusions.document(self), outputs)
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['unverifiable_fact_count'], report['excluded_spelling_count'], report['excluded_type_count']), (1, 1, 0))

    def test_report_over_the_size_limit_keeps_the_existing_warning(self):
        run_id, path = self.coverage_path()
        path.write_text(path.read_text() + ' ' * (2 * 1024 * 1024))
        value = read_report(self.root, self.store, run_id)
        self.assertFalse(value['available'])
        self.assertIn('관계별 추출 점검 기록을 사용할 수 없습니다', render(value))
