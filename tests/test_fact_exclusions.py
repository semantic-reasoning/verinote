"""Off-dictionary facts in a fresh response: exact entity match, visible exclusion, strict reload."""
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_extraction_checkpoint import FakeCLI
from test_workflow import approved_dictionary, extraction_response

from verinote.extraction_coverage import (
    MAX_EXCLUDED_QUOTE,
    MAX_REPORT_BYTES,
    exclude_off_dictionary_facts,
    read_report,
    repair_missing_indices,
    validate_accounting,
)
from verinote.ontology import (
    MAX_FACTS,
    VocabularyError,
    dictionary_fact,
    dictionary_terms,
    extraction_quote_issues,
    validate_dictionary,
)
from verinote.service import Service
from verinote.store import digest

S = ('입찰 준비 단계 설명. 입찰준비 단계 표기. 제안요청 활동 수행. 프로젝트 관리 영역. 프로젝트관리 활동. '
     '가 나 별칭 표기. 사업 단계 목록.')
def entity(key, kind): return {'id': key, 'name': key, 'type': kind, 'quote': S}
D = {'entities': [entity('사업 단계', '단계'), entity('입찰 준비 단계', '단계'), entity('제안요청 활동', '활동'),
                  entity('프로젝트 관리', '평가 영역'), entity('프로젝트관리', '활동')],
     'aliases': [],
     'relations': [{'id': 'has_activity', 'description': 'd', 'subject_types': ['단계'], 'object_types': ['활동'], 'quote': S},
                   {'id': 'has_phase', 'description': 'd', 'subject_types': ['단계'], 'object_types': ['단계'], 'quote': S}]}
GOOD = {'subject': '입찰 준비 단계', 'predicate': 'has_activity', 'object': '제안요청 활동', 'quote': '제안요청 활동 수행'}
MISTYPED = {'subject': '사업 단계', 'predicate': 'has_activity', 'object': '입찰 준비 단계', 'quote': '입찰 준비 단계 설명'}
SPACED = {'subject': '사업 단계', 'predicate': 'has_phase', 'object': '입찰준비 단계', 'quote': '입찰준비 단계 표기'}
PHASE = {'subject': '사업 단계', 'predicate': 'has_phase', 'object': '입찰 준비 단계', 'quote': '사업 단계 목록'}
UNKNOWN = GOOD | {'object': '승인 사전에 전혀 없는 개체'}
PAGE = {'title': 't', 'claims': [{'heading': 'h', 'text': 'x', 'quote': '사업 단계 목록'}]}
RECORD = {'code': 'unknown_entity', 'subject': 'a', 'predicate': 'has_activity', 'object': 'b', 'quote': 'c'}


def alias_dict(alias, target):
    value = copy.deepcopy(D)
    value['aliases'] = [{'alias': alias, 'entity': target, 'quote': S}]
    return value


def fresh(facts):
    return extraction_response(facts, D, PAGE)


def row_of(value, predicate):
    return next(r for r in value['coverage'] if r['predicate'] == predicate)


class ExactMatch(unittest.TestCase):
    def ref(self, value, dictionary=D):
        return dictionary_terms({'subject': '사업 단계', 'predicate': 'has_phase', 'object': value}, dictionary)['object']

    def code(self, value, dictionary=D):
        with self.assertRaises(VocabularyError) as caught:
            self.ref(value, dictionary)
        return caught.exception.code

    def test_ascii_space_tab_and_nbsp_variants_of_an_entity_id_raise_unknown_entity(self):
        for value in ('입찰준비 단계', '입찰 준비단계', '입찰\t준비 단계', '입찰\u00a0준비 단계', '입찰  준비 단계'):
            with self.subTest(value=value):
                self.assertEqual(self.code(value), 'unknown_entity')

    def test_spacing_variant_of_an_alias_raises_unknown_entity_and_the_exact_alias_returns_its_target(self):
        dictionary = alias_dict('가 나 별칭', '입찰 준비 단계')
        self.assertEqual(self.code('가나별칭', dictionary), 'unknown_entity')
        self.assertEqual(self.ref('가 나 별칭', dictionary), '입찰 준비 단계')

    def test_unknown_entity_message_names_the_exact_spelling_not_the_absence_of_the_concept(self):
        with self.assertRaisesRegex(VocabularyError, '이 표기와 정확히 일치하는 것이 없습니다: 입찰준비 단계'):
            self.ref('입찰준비 단계')

    def test_ids_differing_by_a_space_each_return_their_own_entity_on_exact_match(self):
        dictionary = copy.deepcopy(D)
        dictionary['relations'].append({'id': 'scored_by', 'description': 'd', 'subject_types': ['단계'], 'object_types': ['평가 영역', '활동'], 'quote': S})
        fact = {'subject': '사업 단계', 'predicate': 'scored_by'}
        self.assertEqual(dictionary_terms(fact | {'object': '프로젝트 관리'}, dictionary)['object'], '프로젝트 관리')
        self.assertEqual(dictionary_terms(fact | {'object': '프로젝트관리'}, dictionary)['object'], '프로젝트관리')

    def test_type_is_checked_against_the_exactly_matched_entity(self):
        fact = {'subject': '사업 단계', 'predicate': 'has_activity'}
        self.assertEqual(dictionary_terms(fact | {'object': '프로젝트관리'}, D)['object'], '프로젝트관리')
        with self.assertRaises(VocabularyError) as caught:
            dictionary_terms(fact | {'object': '프로젝트 관리'}, D)
        self.assertEqual(caught.exception.code, 'type_mismatch')

    def test_third_spelling_of_two_ids_differing_by_a_space_raises_unknown_entity(self):
        self.assertEqual(self.code('프로 젝트관리'), 'unknown_entity')

    def test_absent_reference_raises_unknown_entity(self):
        self.assertEqual(self.code('없는 개체'), 'unknown_entity')

    def test_mistyped_exact_reference_raises_type_mismatch(self):
        with self.assertRaises(VocabularyError) as caught:
            dictionary_terms(MISTYPED, D)
        self.assertEqual(caught.exception.code, 'type_mismatch')

    def test_unknown_predicate_raises_plain_valueerror_with_valid_or_unknown_entities(self):
        for fact in ({'subject': '사업 단계', 'predicate': 'invented', 'object': '입찰 준비 단계'},
                     {'subject': '없는 개체', 'predicate': 'invented', 'object': '입찰 준비 단계'}):
            with self.subTest(fact=fact), self.assertRaises(ValueError) as caught:
                dictionary_terms(fact, D)
            self.assertNotIsInstance(caught.exception, VocabularyError)

    def test_dictionary_fact_returns_the_canonical_id_for_an_exact_alias_and_the_model_quote(self):
        fact = PHASE | {'object': '가 나 별칭', 'quote': '가 나 별칭 표기'}
        self.assertEqual(dictionary_fact(fact, S, alias_dict('가 나 별칭', '입찰 준비 단계')), PHASE | {'quote': '가 나 별칭 표기'})


class AliasEvidence(unittest.TestCase):
    NAME, ALIAS = '입찰 준비 단계', '입찰준비 단계'

    def dictionary(self, quote):
        return {'entities': [{'id': self.NAME, 'name': self.NAME, 'type': '단계', 'quote': self.NAME}],
                'aliases': [{'alias': self.ALIAS, 'entity': self.NAME, 'quote': quote}],
                'relations': [{'id': 'r', 'description': 'd', 'subject_types': ['단계'], 'object_types': ['단계'], 'quote': self.NAME}]}

    def test_spellings_at_most_8000_characters_apart_accept_one_continuous_alias_quote(self):
        source = self.NAME + '가' * 7000 + self.ALIAS
        self.assertEqual(validate_dictionary(self.dictionary(source), source)['aliases'][0]['entity'], self.NAME)

    def test_spellings_over_8000_characters_apart_reject_the_only_quote_containing_both(self):
        source = self.NAME + '가' * 8000 + self.ALIAS
        with self.assertRaisesRegex(ValueError, '8000자 이하'):
            validate_dictionary(self.dictionary(source), source)

    def test_quote_with_only_one_of_the_two_spellings_is_rejected(self):
        source = self.NAME + '가' * 8000 + self.ALIAS
        for quote in (self.ALIAS, self.NAME):
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, '별칭'):
                validate_dictionary(self.dictionary(quote), source)


class Accounting(unittest.TestCase):
    def assert_excluded_row(self, value):
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertTrue(excluded)
        row = row_of(result, 'has_activity')
        self.assertEqual((row['status'], row['fact_indices']), ('not_representable', []))
        self.assertIn('로컬 처리', row['reason'])
        repaired, _ = repair_missing_indices(result, D)
        validate_accounting(repaired, D)

    def assert_rejected_by_accounting(self, value):
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertTrue(excluded)
        with self.assertRaises(ValueError):
            repair_missing_indices(result, D)

    def test_later_fact_indices_are_renumbered_to_the_same_fact(self):
        result, excluded = exclude_off_dictionary_facts(fresh([MISTYPED, GOOD, PHASE]), D)
        self.assertEqual([e['code'] for e in excluded], ['type_mismatch'])
        self.assertEqual(row_of(result, 'has_activity')['fact_indices'], [0])
        self.assertEqual(row_of(result, 'has_phase')['fact_indices'], [1])
        self.assertEqual(result['facts'], [GOOD, PHASE])
        validate_accounting(result, D)

    def test_cross_predicate_repair_uses_post_exclusion_fact_numbers(self):
        value = fresh([MISTYPED, GOOD, PHASE])
        row_of(value, 'has_activity')['fact_indices'] = [0, 2]
        row_of(value, 'has_phase')['fact_indices'] = [1]
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertEqual(len(excluded), 1)
        self.assertEqual(row_of(result, 'has_activity')['fact_indices'], [1])
        self.assertEqual(row_of(result, 'has_phase')['fact_indices'], [0])
        repaired, changes = repair_missing_indices(result, D)
        self.assertEqual(repaired['facts'], [GOOD, PHASE])
        self.assertEqual(repaired['excluded_facts'], excluded)
        self.assertEqual(row_of(repaired, 'has_activity')['fact_indices'], [0])
        self.assertEqual(row_of(repaired, 'has_phase')['fact_indices'], [1])
        self.assertEqual([(c['before'], c['after']) for c in changes], [([1], [0]), ([0], [1])])
        validate_accounting(repaired, D)

    def test_exclusion_sentinel_rejects_even_beside_repairable_cross_predicate_indices(self):
        value = fresh([MISTYPED, GOOD, PHASE])
        row_of(value, 'has_activity')['fact_indices'] = [0, 2]
        row_of(value, 'has_phase')['fact_indices'] = [0, 1]
        result, _ = exclude_off_dictionary_facts(value, D)
        self.assertTrue(any(i >= len(result['facts']) for i in row_of(result, 'has_phase')['fact_indices']))
        original = copy.deepcopy(result)
        with self.assertRaises(ValueError): repair_missing_indices(result, D)
        self.assertEqual(result, original)

    def test_row_extracted_naming_only_the_excluded_fact_becomes_not_representable(self):
        self.assert_excluded_row(fresh([MISTYPED, PHASE]))

    def test_row_no_evidence_with_no_index_beside_its_excluded_fact_becomes_not_representable(self):
        value = fresh([MISTYPED, PHASE])
        row_of(value, 'has_activity').update(status='no_evidence', fact_indices=[])
        self.assert_excluded_row(value)

    def test_row_no_evidence_naming_the_excluded_fact_becomes_not_representable(self):
        value = fresh([MISTYPED, PHASE])
        row_of(value, 'has_activity').update(status='no_evidence')
        self.assert_excluded_row(value)

    def test_row_extracted_with_no_index_beside_its_excluded_fact_becomes_not_representable(self):
        value = fresh([MISTYPED, PHASE])
        row_of(value, 'has_activity').update(fact_indices=[])
        self.assert_excluded_row(value)

    def test_row_limit_reached_with_only_excluded_facts_becomes_not_representable(self):
        value = fresh([MISTYPED, PHASE])
        row_of(value, 'has_activity').update(status='limit_reached', fact_indices=[])
        self.assert_excluded_row(value)

    def test_duplicate_index_on_an_excluded_fact_is_rejected_by_accounting(self):
        value = fresh([MISTYPED, PHASE])
        row_of(value, 'has_activity').update(fact_indices=[0, 0])
        self.assert_rejected_by_accounting(value)

    def test_index_naming_another_predicates_excluded_fact_is_rejected_by_accounting(self):
        value = fresh([MISTYPED, PHASE, PHASE | {'quote': '입찰 준비 단계 설명'}])
        row_of(value, 'has_phase').update(fact_indices=[0, 1, 2])
        self.assert_rejected_by_accounting(value)

    def test_wrong_predicate_index_is_rejected_when_its_raw_slot_would_name_a_kept_same_predicate_fact(self):
        # Index 1 is excluded and the row omits index 2, so raw 1 would name that same-predicate fact after removal.
        value = fresh([PHASE, MISTYPED, PHASE | {'quote': '입찰 준비 단계 설명'}])
        row_of(value, 'has_phase').update(fact_indices=[0, 1])
        self.assert_rejected_by_accounting(value)

    def test_extracted_row_with_a_kept_sibling_keeps_status_and_is_filled_by_index_repair(self):
        value = fresh([MISTYPED, GOOD])
        row_of(value, 'has_activity').update(fact_indices=[0])
        result, _ = exclude_off_dictionary_facts(value, D)
        self.assertEqual(row_of(result, 'has_activity')['status'], 'extracted')
        repaired, changes = repair_missing_indices(result, D)
        self.assertEqual(row_of(repaired, 'has_activity')['fact_indices'], [0])
        self.assertEqual(len(changes), 1)

    def test_no_evidence_row_beside_a_kept_fact_of_that_predicate_is_rejected_by_accounting(self):
        value = fresh([MISTYPED, GOOD])
        row_of(value, 'has_activity').update(status='no_evidence', fact_indices=[])
        self.assert_rejected_by_accounting(value)

    def test_model_authored_not_representable_without_a_claim_is_rejected(self):
        value = fresh([PHASE])
        row_of(value, 'has_activity').update(status='not_representable')
        with self.assertRaises(ValueError):
            validate_accounting(value, D)

    def test_not_representable_without_claim_is_rejected_when_only_another_predicate_has_an_exclusion(self):
        value = fresh([GOOD]) | {'excluded_facts': [RECORD]}  # RECORD's predicate is has_activity.
        row_of(value, 'has_phase').update(status='not_representable')
        with self.assertRaisesRegex(ValueError, '표현 제약'):
            validate_accounting(value, D)

    def test_limit_reached_counts_kept_plus_excluded_facts(self):
        value = fresh([GOOD] * (MAX_FACTS - 1) + [MISTYPED])
        row_of(value, 'has_phase').update(status='limit_reached')
        result, _ = exclude_off_dictionary_facts(value, D)
        validate_accounting(result, D)

    def test_response_with_neither_key_nor_exclusion_returns_the_same_object(self):
        value = fresh([GOOD])
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertIs(result, value)
        self.assertEqual(excluded, [])
        self.assertNotIn('excluded_facts', result)

    def test_inbound_key_is_discarded_with_no_real_exclusion(self):
        for inbound in ([RECORD] * 5, 'junk', None):
            with self.subTest(inbound=inbound):
                clean = fresh([GOOD])
                result, excluded = exclude_off_dictionary_facts(clean | {'excluded_facts': inbound}, D)
                self.assertEqual(result, clean)
                self.assertEqual(excluded, [])

    def test_inbound_key_is_replaced_by_local_records_when_a_fact_is_excluded(self):
        result, _ = exclude_off_dictionary_facts(fresh([MISTYPED, GOOD]) | {'excluded_facts': [RECORD] * 5}, D)
        self.assertEqual(len(result['excluded_facts']), 1)
        self.assertEqual(result['excluded_facts'][0]['object'], '입찰 준비 단계')

    def test_inbound_key_does_not_satisfy_the_not_representable_relaxation_after_filtering(self):
        value = fresh([PHASE]) | {'excluded_facts': [RECORD]}
        row_of(value, 'has_activity').update(status='not_representable')
        result, _ = exclude_off_dictionary_facts(value, D)
        with self.assertRaisesRegex(ValueError, '표현 제약'):
            validate_accounting(result, D)

    def test_malformed_fact_returns_input_for_ordinary_preflight(self):
        value = fresh([MISTYPED, GOOD])
        value['facts'].append('invalid')
        self.assertIs(exclude_off_dictionary_facts(value, D)[0], value)

    def test_unknown_predicate_with_unknown_entity_excludes_nothing(self):
        value = fresh([GOOD])
        value['facts'].append({'subject': '없는 개체', 'predicate': 'invented', 'object': '없는 개체', 'quote': 'x'})
        self.assertEqual(exclude_off_dictionary_facts(value, D), (value, []))

    def test_non_string_quote_on_an_excluded_fact_is_recorded_empty(self):
        for quote in (123, None, ['x']):
            with self.subTest(quote=quote):
                value = fresh([GOOD])
                value['facts'].append(UNKNOWN | {'quote': quote})
                result, excluded = exclude_off_dictionary_facts(value, D)
                self.assertEqual(excluded[0]['quote'], '')
                validate_accounting(result, D)

    def test_whitespace_padded_predicate_is_recorded_as_the_approved_id_and_empties_its_row(self):
        value = fresh([PHASE])
        value['facts'].append(UNKNOWN | {'predicate': ' has_activity '})
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertEqual(excluded[0]['predicate'], 'has_activity')
        self.assertEqual(row_of(result, 'has_activity')['status'], 'not_representable')
        validate_accounting(result, D)

    def test_quote_in_the_record_is_cut_to_the_bound(self):
        value = fresh([GOOD])
        value['facts'].append(UNKNOWN | {'quote': '가' * (MAX_EXCLUDED_QUOTE + 1)})
        _, excluded = exclude_off_dictionary_facts(value, D)
        self.assertEqual(len(excluded[0]['quote']), MAX_EXCLUDED_QUOTE)

    def test_kept_plus_excluded_over_max_facts_is_rejected_by_validate_accounting(self):
        value = fresh([GOOD] * MAX_FACTS) | {'excluded_facts': [RECORD]}
        with self.assertRaisesRegex(ValueError, '형식 오류'):
            validate_accounting(value, D)

    def test_excluded_record_with_bad_code_predicate_or_length_is_rejected(self):
        for bad in ({'code': 'invented'}, {'code': 'ambiguous_entity'}, {'predicate': 'invented'}, {'quote': '가' * (MAX_EXCLUDED_QUOTE + 1)},
                    {'subject': '가' * 201}, {'object': '가' * 201}, {'object': 1}, {'extra': 'x'}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, '필드 오류'):
                validate_accounting(fresh([GOOD]) | {'excluded_facts': [RECORD | bad]}, D)

    def test_raw_count_over_the_limit_reports_the_count_not_the_exclusion_format(self):
        value = fresh([GOOD])
        value['facts'] = [UNKNOWN] * (MAX_FACTS + 11)
        result, excluded = exclude_off_dictionary_facts(value, D)
        self.assertEqual(excluded, [])
        with self.assertRaisesRegex(ValueError, '청크당 facts'):
            extraction_quote_issues(result, S, D)

    def test_exclusion_leaves_the_input_object_unchanged(self):
        value = fresh([MISTYPED, GOOD])
        snapshot = copy.deepcopy(value)
        exclude_off_dictionary_facts(value, D)
        self.assertEqual(value, snapshot)


class Pipeline(unittest.TestCase):
    def setUp(self):
        workflow.Workflow.setUp(self)
        # These historical fixtures assert contract-2 call counts and persistence semantics.
        contract = patch('verinote.store.EXTRACTION_CONTRACT_VERSION', 2)
        contract.start(); self.addCleanup(contract.stop)
    tearDown = workflow.Workflow.tearDown

    def document(self, count=1, dictionary=D):
        source = S.ljust(8000) * (count - 1) + S
        doc = self.store.register('x', 'x.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'], dictionary)
        return doc

    def execute(self, doc, outputs, model='chosen'):
        cli = FakeCLI(outputs)
        service = Service(self.store, self.root, cli)
        current = self.store.document(doc['id'])
        run = service.start(doc['id'], 'copilot', model, expected_version=current['version'], expected_generation=current['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), cli.calls

    def saved(self, doc, run):
        return [i['payload'] for i in self.store.document(doc['id'])['items'] if i['kind'] == 'fact' and i['run_id'] == run['id']]

    def test_range_repair_stays_disabled_after_real_exclusions(self):
        for refs in ([0, 2], [2, 3]):
            value = fresh([MISTYPED, GOOD, PHASE])
            # First case becomes an exclusion sentinel; second was already out of range.
            row_of(value, 'has_phase')['fact_indices'] = refs
            value['excluded_facts'] = []  # Untrusted model metadata cannot opt into the relaxed path.
            doc = self.document()
            with patch('verinote.service.repair_missing_indices', wraps=repair_missing_indices) as repair:
                run, calls = self.execute(doc, [value])
            self.assertEqual(run['status'], 'error', run['detail'])
            self.assertFalse(repair.call_args.kwargs['allow_model_range'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.saved(doc, run), [])
            self.assertFalse((self.root/'runs'/run['id']/'1/coverage-repair.json').exists())

    def test_empty_exclusions_do_not_bypass_vocabulary_preflight(self):
        value = fresh([GOOD, PHASE | {'predicate':'unapproved'}])
        row_of(value, 'has_activity')['fact_indices'] = [80]
        self.assertEqual(exclude_off_dictionary_facts(value, D)[1], [])
        doc = self.document()
        with patch('verinote.service.repair_missing_indices', wraps=repair_missing_indices) as repair:
            run, calls = self.execute(doc, [value])
        self.assertEqual(run['status'], 'error')
        self.assertIn('승인 사전에 없는 관계', run['detail'])
        repair.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.saved(doc, run), [])

    def test_mistyped_fact_is_excluded_and_detail_and_report_state_the_counts(self):
        doc = self.document()
        run, calls = self.execute(doc, [fresh([MISTYPED, GOOD, PHASE])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertCountEqual(self.saved(doc, run), [GOOD, PHASE])
        self.assertIn('관계 후보 2개', run['detail'])
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual((report['available'], report['emitted_fact_count'], report['excluded_fact_count'], report['saved_fact_count']), (True, 3, 1, 2))
        self.assertEqual(report['chunks'][0]['excluded_facts'][0]['object'], '입찰 준비 단계')

    def test_unknown_entity_fact_is_excluded_and_the_others_saved(self):
        doc = self.document()
        run, _ = self.execute(doc, [fresh([UNKNOWN, PHASE])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(self.saved(doc, run), [PHASE])
        record = read_report(self.root, self.store, run['id'])['chunks'][0]['excluded_facts'][0]
        self.assertEqual(record['code'], 'unknown_entity')

    def test_partial_exclusion_detail_starts_with_ratio_and_remedy(self):
        run, _ = self.execute(self.document(), [fresh([MISTYPED, GOOD, PHASE])])
        self.assertTrue(run['detail'].startswith('응답 관계 3개 중 1개는 승인 사전의 표기·타입과 일치하지 않아 저장하지 않음 · 되살리려면 사전 수정·재승인 후 재추출'), run['detail'])

    def test_all_facts_excluded_ends_as_error_without_publication_or_resume_promise(self):
        doc = self.document()
        run, calls = self.execute(doc, [fresh([MISTYPED, UNKNOWN])])
        self.assertEqual(run['status'], 'error', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertIn('응답 관계 2개가 모두', run['detail'])
        self.assertIn('사전을 수정·재승인하면 모든 청크를 새로 호출', run['detail'])
        self.assertIn('그 모델의 호환되는 복구 기록을 찾으면 검증된 청크를 재사용하므로 같은 실패로 끝날 수 있습니다', run['detail'])
        self.assertNotIn('어느 쪽이든', run['detail'])
        self.assertNotIn('추출 조건이 이 실행과 같으면', run['detail'])
        self.assertEqual([i for i in self.store.document(doc['id'])['items'] if i.get('run_id') == run['id']], [])

    def test_all_facts_excluded_leaves_the_revision_extractable_and_a_model_with_no_prior_record_calls_every_chunk(self):
        doc = self.document(2)
        self.execute(doc, [fresh([MISTYPED]), fresh([MISTYPED])])
        self.assertTrue(self.store.document(doc['id'])['can_extract'])
        second, calls = self.execute(doc, [fresh([GOOD]), fresh([GOOD])], model='another')
        self.assertEqual((second['status'], len(calls)), ('success', 2), second['detail'])

    def test_model_with_a_complete_record_from_an_earlier_run_makes_0_calls_and_repeats_the_error_after_another_models_run(self):
        doc = self.document()
        first, _ = self.execute(doc, [fresh([MISTYPED])], model='b')
        self.execute(doc, [fresh([MISTYPED])], model='a')
        third, calls = self.execute(doc, [], model='b')
        self.assertEqual((first['status'], third['status'], len(calls)), ('error', 'error', 0), third['detail'])
        self.assertIn('응답 관계 1개가 모두', third['detail'])

    def test_model_with_a_partial_record_from_an_earlier_run_calls_only_the_remaining_chunk_after_another_models_run(self):
        doc = self.document(2)
        self.execute(doc, [fresh([MISTYPED]), TimeoutError('stop')], model='b')
        self.execute(doc, [fresh([MISTYPED]), fresh([MISTYPED])], model='a')
        third, calls = self.execute(doc, [fresh([GOOD])], model='b')
        self.assertEqual((third['status'], len(calls)), ('success', 1), third['detail'])

    def test_all_facts_excluded_then_a_new_approved_revision_calls_every_chunk(self):
        doc = self.document(2)
        self.execute(doc, [fresh([MISTYPED]), fresh([MISTYPED])])
        dictionary = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(dictionary['id'], dictionary['revision'], 'revise', 'R', 'change', D)
        self.store.review(dictionary['id'], dictionary['revision'] + 1, 'approved', 'R', 'approve')
        second, calls = self.execute(doc, [fresh([GOOD]), fresh([GOOD])])
        self.assertEqual((second['status'], len(calls)), ('success', 2), second['detail'])

    def test_all_facts_excluded_with_the_same_model_reuses_the_chunk_and_stays_an_error(self):
        doc = self.document()
        self.execute(doc, [fresh([MISTYPED])])
        second, calls = self.execute(doc, [])
        self.assertEqual((second['status'], len(calls)), ('error', 0), second['detail'])
        self.assertIn('모두', second['detail'])

    def test_response_with_no_facts_and_no_exclusion_still_succeeds(self):
        run, _ = self.execute(self.document(), [fresh([])])
        self.assertEqual(run['status'], 'success', run['detail'])

    def test_partial_exclusion_success_consumes_the_revision(self):
        doc = self.document()
        self.execute(doc, [fresh([MISTYPED, GOOD])])
        self.assertFalse(self.store.document(doc['id'])['can_extract'])

    def test_notify_receives_failed_validation_when_a_fact_is_excluded(self):
        with patch('verinote.call_validation.notify') as notify:
            run, _ = self.execute(self.document(), [fresh([MISTYPED, GOOD])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([call.args[3] for call in notify.call_args_list], [False])

    def test_notify_receives_passed_validation_when_nothing_is_excluded(self):
        with patch('verinote.call_validation.notify') as notify:
            self.execute(self.document(), [fresh([GOOD])])
        self.assertEqual([call.args[3] for call in notify.call_args_list], [True])

    def test_spacing_variant_of_an_existing_entity_is_excluded_and_that_entity_is_in_no_saved_fact(self):
        doc = self.document()
        kept = {'subject': '사업 단계', 'predicate': 'has_activity', 'object': '제안요청 활동', 'quote': '제안요청 활동 수행'}
        run, _ = self.execute(doc, [fresh([SPACED, kept])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(self.saved(doc, run), [kept])
        record = read_report(self.root, self.store, run['id'])['chunks'][0]['excluded_facts'][0]
        self.assertEqual((record['code'], record['object']), ('unknown_entity', '입찰준비 단계'))
        self.assertNotIn('띄어쓰기', run['detail'])

    def test_spacing_variant_on_reload_makes_the_retry_skip_the_checkpoint(self):
        _, calls = self.tamper(self.document(2), lambda output: output['facts'].append(SPACED))
        self.assertEqual(len(calls), 2)

    def test_spacing_variant_at_store_finish_run_is_rejected_and_the_run_stays_running(self):
        doc = self.document()
        current = self.store.document(doc['id'])
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', current['version'], current['generation'])
        self.store.run_status(run['id'], 'running')
        with self.assertRaisesRegex(ValueError, '정확히 일치'):
            self.store.finish_run(run['id'], {'title': 't', 'claims': PAGE['claims']}, [SPACED])
        self.assertEqual(self.store.run(run['id'])['status'], 'running')

    def test_spacing_variant_at_review_revise_is_rejected_and_the_stored_fact_is_unchanged(self):
        doc = self.document()
        run, _ = self.execute(doc, [fresh([PHASE])])
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'fact' and i['run_id'] == run['id'])
        with self.assertRaisesRegex(ValueError, '정확히 일치'):
            self.store.review(item['id'], item['revision'], 'revise', 'R', 'edit', SPACED)
        self.assertEqual(self.saved(doc, run), [PHASE])

    def test_alias_added_with_evidence_and_reapproved_stores_the_spacing_variant_under_the_canonical_id(self):
        doc = self.document(2)
        first, _ = self.execute(doc, [fresh([SPACED, GOOD]), fresh([GOOD])])
        self.assertEqual(self.saved(doc, first), [GOOD])
        dictionary = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        revised = alias_dict('입찰준비 단계', '입찰 준비 단계')
        self.store.review(dictionary['id'], dictionary['revision'], 'revise', 'R', 'alias', revised)
        self.store.review(dictionary['id'], dictionary['revision'] + 1, 'approved', 'R', 'approve')
        second, calls = self.execute(doc, [fresh([SPACED, GOOD]), fresh([GOOD])])
        self.assertEqual((second['status'], len(calls)), ('success', 2), second['detail'])
        self.assertCountEqual(self.saved(doc, second), [GOOD, SPACED | {'object': '입찰 준비 단계'}])
        self.assertEqual(read_report(self.root, self.store, second['id'])['excluded_fact_count'], 0)

    def test_alias_with_evidence_turns_a_mistyped_spacing_variant_into_a_type_mismatch_exclusion_and_stores_nothing_for_it(self):
        doc = self.document(dictionary=alias_dict('입찰준비 단계', '입찰 준비 단계'))
        mistyped = MISTYPED | {'object': '입찰준비 단계', 'quote': '입찰준비 단계 표기'}
        run, _ = self.execute(doc, [fresh([mistyped, GOOD])])
        self.assertEqual(self.saved(doc, run), [GOOD])
        record = read_report(self.root, self.store, run['id'])['chunks'][0]['excluded_facts'][0]
        self.assertEqual((record['code'], record['object']), ('type_mismatch', '입찰준비 단계'))

    def test_alias_quote_missing_the_entity_name_is_rejected_at_review_revise_and_the_revision_is_unchanged(self):
        doc = self.document()
        dictionary = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        revised = copy.deepcopy(D)
        revised['aliases'] = [{'alias': '입찰준비 단계', 'entity': '입찰 준비 단계', 'quote': '입찰준비 단계 표기'}]
        with self.assertRaisesRegex(ValueError, '별칭과 개체 name이 함께'):
            self.store.review(dictionary['id'], dictionary['revision'], 'revise', 'R', 'alias', revised)
        after = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual((after['revision'], after['status']), (dictionary['revision'], 'approved'))

    def test_rendered_row_with_only_excluded_facts_labels_them_excluded_not_unrepresentable(self):
        run, _ = self.execute(self.document(), [fresh([SPACED, GOOD])])
        html = self.render(read_report(self.root, self.store, run['id']))
        self.assertIn('승인 사전과 일치하지 않아 제외 · 응답 관계 1개 (제외 1개)', html)
        self.assertNotIn('사전으로 표현 불가', html)

    def test_excluded_fact_with_a_non_string_quote_ends_as_success_with_one_call(self):
        doc = self.document()
        run, calls = self.execute(doc, [fresh([MISTYPED | {'quote': 123}, GOOD])])
        self.assertEqual((run['status'], len(calls)), ('success', 1), run['detail'])
        self.assertEqual(self.saved(doc, run), [GOOD])
        html = self.render(read_report(self.root, self.store, run['id']))
        self.assertIn('승인 관계의 방향/타입 불일치', html)
        self.assertIn('미검증 모델 인용: 없음', html)

    def test_excluded_fact_with_absent_quote_makes_one_call(self):
        run, calls = self.execute(self.document(), [fresh([MISTYPED | {'quote': '원문에 없는 인용'}, GOOD])])
        self.assertEqual((run['status'], len(calls)), ('success', 1), run['detail'])

    def test_exclusion_record_survives_a_quote_repair_on_a_kept_fact(self):
        doc = self.document()
        bad = GOOD | {'quote': '원문에 없는 인용'}
        run, calls = self.execute(doc, [fresh([MISTYPED, bad]), {'repairs': [{'path': 'facts.0.quote', 'quote': '제안요청 활동 수행'}]}])
        self.assertEqual((run['status'], len(calls)), ('success', 2), run['detail'])
        self.assertIn('응답 관계 2개 중 1개', run['detail'])
        self.assertEqual(self.saved(doc, run), [GOOD])

    def test_repair_audit_hashes_the_original_response_and_numbers_kept_facts(self):
        value = fresh([MISTYPED, GOOD])
        row_of(value, 'has_activity').update(fact_indices=[0])
        run, _ = self.execute(self.document(), [value])
        self.assertEqual(run['status'], 'success', run['detail'])
        audit = json.loads((self.root / 'runs' / run['id'] / '1' / 'coverage-repair.json').read_text())
        self.assertEqual(audit['before_sha256'], digest(value))
        self.assertEqual(audit['changes'], [{'row': 0, 'predicate': 'has_activity', 'before': [], 'after': [0]}])

    def test_resumed_run_counts_exclusions_from_reused_chunks(self):
        doc = self.document(2)
        first, _ = self.execute(doc, [fresh([MISTYPED, GOOD]), TimeoutError('stop')])
        self.assertEqual(first['status'], 'error')
        self.assertIn('응답 관계 2개 중 1개', first['detail'])
        second, calls = self.execute(doc, [fresh([GOOD])])
        self.assertEqual((second['status'], len(calls)), ('success', 1), second['detail'])
        self.assertIn('1청크 재사용', second['detail'])
        self.assertIn('응답 관계 3개 중 1개', second['detail'])
        self.assertEqual(read_report(self.root, self.store, second['id'])['excluded_fact_count'], 1)

    def tamper(self, doc, mutate):
        first, _ = self.execute(doc, [fresh([MISTYPED, GOOD]), TimeoutError('stop')])
        path = self.root / 'runs' / first['id'] / 'extraction-checkpoint.json'
        value = json.loads(path.read_text())
        mutate(value['outputs'][0])
        path.write_text(json.dumps(value, ensure_ascii=False))
        return self.execute(doc, [fresh([GOOD]), fresh([GOOD])])

    def test_stored_off_dictionary_fact_makes_the_reload_skip_the_checkpoint(self):
        _, calls = self.tamper(self.document(2), lambda output: output['facts'].append(MISTYPED))
        self.assertEqual(len(calls), 2)

    def test_stored_malformed_exclusion_record_makes_the_reload_skip_the_checkpoint(self):
        _, calls = self.tamper(self.document(2), lambda output: output['excluded_facts'][0].update(code='invented'))
        self.assertEqual(len(calls), 2)

    def test_unknown_predicate_ends_the_run_as_error(self):
        response = fresh([GOOD]) | {'facts': [GOOD | {'predicate': 'invented'}]}
        run, _ = self.execute(self.document(), [response])
        self.assertEqual(run['status'], 'error')

    def test_malformed_fact_beside_an_excludable_one_ends_the_run_as_error(self):
        value = fresh([MISTYPED, GOOD])
        value['facts'].append('invalid')
        run, _ = self.execute(self.document(), [value])
        self.assertEqual(run['status'], 'error')

    def test_clean_checkpoint_output_has_no_exclusion_key(self):
        run, _ = self.execute(self.document(), [fresh([GOOD])])
        value = json.loads((self.root / 'runs' / run['id'] / 'extraction-checkpoint.json').read_text())
        self.assertNotIn('excluded_facts', value['outputs'][0])

    def test_capped_is_true_when_kept_plus_excluded_reach_the_limit(self):
        run, _ = self.execute(self.document(), [fresh([GOOD] * (MAX_FACTS - 1) + [MISTYPED])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertTrue(read_report(self.root, self.store, run['id'])['capped'])

    def test_legacy_contract_response_is_not_filtered(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', doc['version'], doc['generation'])
        pin = dict(run['ontology'], extraction_contract=0)
        with self.store.connect(True) as db:
            db.execute('UPDATE runs SET ontology=? WHERE id=?', (json.dumps(pin, ensure_ascii=False), run['id']))
        run = self.store.run(run['id'])
        cli = FakeCLI([fresh([MISTYPED, GOOD])])
        service = Service(self.store, self.root, cli)
        service.slots.acquire()
        service.registered_extractions[run['id']] = object()
        service.extract(run, cli)
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'error', self.store.run(run['id'])['detail'])

    def render(self, report):
        app = Path('verinote/web/app.js').read_text(encoding='utf-8')
        function = app[app.index('function renderCoverage('):app.index('function usageStage(')]
        script = ("const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');\n"
                  + function + '\nconsole.log(renderCoverage(' + json.dumps(report) + '));')
        return subprocess.check_output(['node'], input=script, text=True)

    def test_rendered_report_states_excluded_counts_and_labels_quotes_unverified(self):
        malicious = MISTYPED | {'object': '<img src=x onerror=y>', 'quote': '<script>bad</script>'}
        run, _ = self.execute(self.document(), [fresh([malicious, GOOD, PHASE])])
        html = self.render(read_report(self.root, self.store, run['id']))
        self.assertIn('청크 응답 관계 3개 · 제외 1개 · 저장 후보 2개', html)
        self.assertIn('제외 후 관계가 남은 종류 2/2', html)
        self.assertIn('제외 1개는 승인 사전의 표기·타입과 일치하지 않아', html)
        self.assertIn('모델이 보고한 사전 제약 0개 (제외한 응답 관계는 이 수에 포함되지 않음)', html)
        self.assertIn('제외로 바뀐 행은 로컬 처리', html)
        self.assertIn('응답 관계 2개 (제외 1개)', html)
        self.assertIn('미검증 모델 인용: &lt;script&gt;', html)
        self.assertIn('제외한 응답 관계 1개', html)
        self.assertIn('승인된 개체·별칭 중 이 표기와 정확히 일치하는 것 없음', html)
        self.assertNotIn('승인 사전에 없는 개체', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)

    def test_rendered_report_without_exclusions_has_no_exclusion_wording(self):
        run, _ = self.execute(self.document(), [fresh([GOOD])])
        html = self.render(read_report(self.root, self.store, run['id']))
        self.assertNotIn('제외', html)
        self.assertIn('관계가 나온 종류 1/2', html)

    def bound_size_response(self):
        facts = [{'subject': '개' * 200, 'predicate': 'has_phase', 'object': '체' * 200, 'quote': '가' * 8000}] * MAX_FACTS
        value = fresh([]) | {'facts': facts}
        row_of(value, 'has_phase').update(status='extracted', fact_indices=list(range(MAX_FACTS)))
        return value

    def test_failed_run_over_the_byte_limit_keeps_the_last_report_prefix_that_fit_and_detail_states_the_full_count(self):
        run, _ = self.execute(self.document(16), [self.bound_size_response()] * 15 + [TimeoutError('stop')])
        self.assertEqual(run['status'], 'error', run['detail'])
        self.assertIn(f'응답 관계 {15 * MAX_FACTS}개 중 {15 * MAX_FACTS}개', run['detail'])
        report = read_report(self.root, self.store, run['id'])
        self.assertTrue(report['available'])
        self.assertGreater(report['validated_chunks'], 0)
        self.assertLess(report['validated_chunks'], 15)
        self.assertEqual(report['excluded_fact_count'], report['validated_chunks'] * MAX_FACTS)
        self.assertLessEqual((self.root / 'runs' / run['id'] / 'extraction-coverage.json').stat().st_size, MAX_REPORT_BYTES)

    def test_resumed_run_whose_reused_chunks_exceed_the_byte_limit_writes_no_report_file_and_read_report_is_unavailable(self):
        for outcome, status in ((TimeoutError('stop'), 'error'), (fresh([GOOD]), 'success')):
            with self.subTest(status=status):
                doc = self.document(16)
                first, _ = self.execute(doc, [self.bound_size_response()] * 15 + [TimeoutError('stop')])
                self.assertEqual(first['status'], 'error', first['detail'])
                second, calls = self.execute(doc, [outcome])
                self.assertEqual((second['status'], len(calls)), (status, 1), second['detail'])
                self.assertFalse((self.root / 'runs' / second['id'] / 'extraction-coverage.json').exists())
                self.assertFalse(read_report(self.root, self.store, second['id'])['available'])
                self.assertIn(f'{15 * MAX_FACTS}개', second['detail'])

    def test_failed_run_detail_is_cut_at_2000_characters_and_a_long_error_text_removes_the_exclusion_count(self):
        run, _ = self.execute(self.document(2), [fresh([MISTYPED, GOOD]), TimeoutError('x' * 3000)])
        self.assertEqual((run['status'], len(run['detail'])), ('error', 2000))
        self.assertNotIn('응답 관계 2개 중', run['detail'])

    def test_partial_exclusion_with_another_validation_failure_ends_as_error(self):
        run, _ = self.execute(self.document(), [fresh([MISTYPED, GOOD]) | {'coverage': []}])
        self.assertEqual(run['status'], 'error', run['detail'])

    def test_one_chunk_with_every_fact_excluded_beside_a_chunk_with_a_kept_fact_ends_as_success(self):
        run, _ = self.execute(self.document(2), [fresh([MISTYPED]), fresh([GOOD])])
        self.assertEqual(run['status'], 'success', run['detail'])

    def test_kept_fact_with_absent_quote_beside_an_excluded_fact_spends_a_repair_call(self):
        bad = GOOD | {'quote': '원문에 없는 인용'}
        _, calls = self.execute(self.document(), [fresh([MISTYPED, bad]), {'repairs': [{'path': 'facts.0.quote', 'quote': '제안요청 활동 수행'}]}])
        self.assertEqual(len(calls), 2)

    def test_stored_fact_quote_is_stripped_and_a_padded_subject_stays_as_written_in_the_record(self):
        self.assertEqual(dictionary_fact(GOOD | {'quote': '  제안요청 활동 수행  '}, S, D)['quote'], '제안요청 활동 수행')
        _, excluded = exclude_off_dictionary_facts(fresh([GOOD]) | {'facts': [UNKNOWN | {'subject': ' 입찰 준비 단계 '}]}, D)
        self.assertEqual(excluded[0]['subject'], ' 입찰 준비 단계 ')

    def test_report_over_the_byte_limit_still_succeeds_and_detail_keeps_the_count(self):
        # 15 of 16 chunks at the record bound: subject and object 200 characters, quote at its cut.
        value = self.bound_size_response()
        records = exclude_off_dictionary_facts(value, D)[1]
        per_chunk = len(json.dumps(records, ensure_ascii=False, indent=2).encode())
        self.assertGreater(per_chunk * 16, MAX_REPORT_BYTES)
        run, _ = self.execute(self.document(16), [value] * 15 + [fresh([GOOD])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertTrue(run['detail'].startswith(f'응답 관계 {15 * MAX_FACTS + 1}개 중 {15 * MAX_FACTS}개'), run['detail'][:120])
        self.assertIn('보고서를 사용할 수 없습니다', run['detail'])
        self.assertFalse(read_report(self.root, self.store, run['id'])['available'])


if __name__ == '__main__':
    unittest.main()
