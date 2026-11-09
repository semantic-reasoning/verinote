"""A model-proposed alias cannot link spellings that differ only in whitespace.

The whitespace-removed comparison only refuses. All spellings are synthetic.
"""
import copy
import hashlib
import unittest

from verinote import ontology
from verinote.dictionary_exclusions import (
    add_chunk,
    new_report,
    public_report,
    validate_report,
)
from verinote.dictionary_filter import (
    FILTER_POLICY,
    REASONS,
    SUPPORTED_FILTER_POLICIES,
    reconcile_generated_dictionary,
)
from verinote.dictionary_reconciliation import SPACING_REFUSAL
from verinote.ontology import source_spans, validate_dictionary

EMPTY = {'entities': [], 'aliases': [], 'relations': []}
SPACING = '공백만 다른'
A, B = '납품 계획', '납품계획'
PROMPT_SHA256 = {
    'DICTIONARY_PROMPT': '22370cf02987e110d642d195fc37cc50e9688f6e97ae8c9441eb5c83b01d9c2f',
    'EXTRACTION_PROMPT': '39db0e9639b65d9aa2f342fdfb80934b3cfee99c54363caff55a8d4d5725fa62',
    'EXTRACTION_PROMPT_V1': '01f1cd0fdd8ee0d4063d7de559a6d74b2b79f27ad5b0313b2be36e365e815e65',
    'QUOTE_REPAIR_PROMPT': 'd0aa5b0658b5dc42d3dcd64cb1c01e2f023e4f8fa2cfe4868e3fd361132cbc78',
    'QUOTE_REPAIR_PROMPT_V1': '59b4172e55f90dc32ee2f2bab351f80f6db446e79487ec74235bf7fcb3e9aea0',
}


def entity(identifier, name=None, kind='산출물', quote=None):
    return {'id': identifier, 'name': name or identifier, 'type': kind, **({'quote': quote} if quote else {})}


def reconcile(source, output, previous=None):
    spans = source_spans(source)
    ref = [spans[0]['id'], spans[-1]['id']]
    proposed = {kind: [dict(row, quote_ref=ref) for row in output.get(kind, [])] for kind in ('entities', 'aliases', 'relations')}
    if 'revisions' in output:
        proposed['revisions'] = output['revisions'](ref)
    return reconcile_generated_dictionary(previous or copy.deepcopy(EMPTY), proposed, spans, source, source)


def stored(source, entities, aliases):
    return validate_dictionary({'entities': entities, 'aliases': aliases, 'relations': []}, source)


def aliases(dictionary):
    return [(row['alias'], row['entity']) for row in dictionary['aliases']]


def ids(dictionary):
    return [row['id'] for row in dictionary['entities']]


def codes(excluded):
    return [row['code'] for row in excluded]


def revision(kind, key, before, after, ref):
    return {'kind': kind, 'key': key, 'before': before, 'after': dict(after, quote_ref=ref), 'reason': 'r'}


class Refused(unittest.TestCase):
    S = f'{A}은 {B}이라고도 한다. 정산 문서가 있다.'

    def test_alias_row(self):
        d, x, _ = reconcile(self.S, {'entities': [entity(A)], 'aliases': [{'alias': B, 'entity': A}]})
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])
        self.assertIn(SPACING, REASONS['alias_spacing_only'])

    def test_draft_merge_by_id(self):
        d, x, _ = reconcile(self.S, {'entities': [entity(A), entity(B)], 'aliases': [{'alias': B, 'entity': A}]})
        self.assertEqual(ids(d), [A, B])
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_draft_merge_by_name_when_ids_differ(self):
        d, x, _ = reconcile(self.S, {'entities': [entity('ENT-1', A), entity('ENT-2', B)],
                                     'aliases': [{'alias': 'ENT-2', 'entity': 'ENT-1'}]})
        self.assertEqual(ids(d), ['ENT-1', 'ENT-2'])
        self.assertEqual(codes(x), ['alias_spacing_only'])
        # The row's ID does not differ from its target only in whitespace; the reason must not say so.
        self.assertNotIn('ID', REASONS['alias_spacing_only'])
        self.assertIn(SPACING, REASONS['alias_spacing_only'])

    def test_chain_merge_by_id(self):
        d, x, c = reconcile(f'{B} 정산 {A}', {'entities': [entity(B), entity('정산'), entity(A)],
                                              'aliases': [{'alias': B, 'entity': '정산'}, {'alias': '정산', 'entity': A}]})
        self.assertEqual(ids(d), [B, '정산', A])
        self.assertTrue(any(SPACING in e['reason'] for e in c))
        self.assertEqual(set(codes(x)), {'alias_spacing_only'})

    def test_chain_merge_by_name(self):
        d, x, c = reconcile(f'ENT-1 ENT-2 ENT-3 {B} 정산 {A}',
                            {'entities': [entity('ENT-1', B), entity('ENT-2', '정산'), entity('ENT-3', A)],
                             'aliases': [{'alias': 'ENT-1', 'entity': 'ENT-2'}, {'alias': 'ENT-2', 'entity': 'ENT-3'}]})
        self.assertEqual(ids(d), ['ENT-1', 'ENT-2', 'ENT-3'])
        self.assertTrue(any(SPACING in e['reason'] for e in c))
        self.assertEqual(set(codes(x)), {'alias_spacing_only'})

    def test_revision_retarget(self):
        src = f'{B} 정산 {A}'
        prev = stored(src, [entity('정산', quote='정산'), entity(A, quote=A)], [{'alias': B, 'entity': '정산', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision(
            'aliases', B, {'alias': B, 'entity': '정산'}, {'alias': B, 'entity': A}, r)]}, prev)
        self.assertEqual(aliases(d), [(B, '정산')])
        self.assertEqual(c[0]['status'], 'deferred')
        self.assertIn(SPACING, c[0]['reason'])

    def test_revision_renames_target(self):
        src = f'{B} 정산 {A}'
        prev = stored(src, [entity('정산', quote='정산')], [{'alias': B, 'entity': '정산', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision(
            'entities', '정산', entity('정산'), entity('정산', A), r)]}, prev)
        self.assertEqual(d['entities'][0]['name'], '정산')
        self.assertIn(SPACING, c[0]['reason'])

    def test_redirect_moves_alias_onto_spacing_equal_entity(self):
        src = f'{B} 정산 {A} 문장'
        prev = stored(src, [entity('정산', quote='정산'), entity(A, quote=A)], [{'alias': B, 'entity': '정산', 'quote': src}])
        d, x, c = reconcile(src, {'aliases': [{'alias': '정산', 'entity': A}]}, prev)
        self.assertEqual(aliases(d), [(B, '정산')])
        self.assertTrue(any(SPACING in e['reason'] for e in c))
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_redirect_creates_sibling_aliases(self):
        src = f'{A} 갑 {B} 을 갑 을'
        prev = stored(src, [entity('갑', quote='갑'), entity('을', quote='을')],
                      [{'alias': A, 'entity': '갑', 'quote': src}, {'alias': B, 'entity': '을', 'quote': src}])
        d, x, c = reconcile(src, {'aliases': [{'alias': '갑', 'entity': '을'}]}, prev)
        self.assertEqual(ids(d), ['갑', '을'])
        self.assertTrue(any(SPACING in e['reason'] for e in c))
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_sibling_aliases_same_response(self):
        d, x, _ = reconcile(f'갑 {A} {B} 끝', {'entities': [entity('갑')],
                                              'aliases': [{'alias': A, 'entity': '갑'}, {'alias': B, 'entity': '갑'}]})
        self.assertEqual(aliases(d), [(A, '갑')])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_line_break_and_other_whitespace_count(self):
        for variant in ('납품\n계획', '납품\t계획', '납품 계획', '납품  계획'):
            d, x, _ = reconcile(f'{A} {variant} 끝', {'entities': [entity(A)], 'aliases': [{'alias': variant, 'entity': A}]})
            self.assertEqual(aliases(d), [], repr(variant))
            self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_fan_in_by_name_in_one_response(self):
        d, x, c = reconcile(f'ENT-1 ENT-2 {A} {B} 갑', {'entities': [entity('ENT-1', A), entity('ENT-2', B), entity('갑')],
                                                       'aliases': [{'alias': 'ENT-1', 'entity': '갑'}, {'alias': 'ENT-2', 'entity': '갑'}]})
        self.assertEqual(ids(d), ['ENT-1', 'ENT-2', '갑'])
        self.assertEqual({e['status'] for e in c if e['origin'] == 'canonicalization'}, {'deferred'})
        self.assertEqual(codes(x), ['alias_spacing_only', 'alias_spacing_only'])

    def test_fan_in_by_name_onto_a_target_that_already_holds_the_alias(self):
        src = f'ENT-2 {B} 갑 {A}'
        prev = stored(src, [entity('갑', quote='갑')], [{'alias': A, 'entity': '갑', 'quote': src}])
        d, x, c = reconcile(src, {'entities': [entity('ENT-2', B)], 'aliases': [{'alias': 'ENT-2', 'entity': '갑'}]}, prev)
        self.assertEqual(ids(d), ['갑', 'ENT-2'])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_held_rows_say_they_were_grouped_with_a_spacing_pair(self):
        # '끝' differs from nothing by spacing; it is held only because it shares a chain with the pair.
        d, x, c = reconcile(f'{B} 정산 {A} 끝', {'entities': [entity(B), entity('정산'), entity(A), entity('끝')],
                                                'aliases': [{'alias': B, 'entity': '정산'}, {'alias': '정산', 'entity': A}, {'alias': '끝', 'entity': A}]})
        self.assertIn('끝', ids(d))
        self.assertIn('끝', [row['identifier'] for row in x])
        self.assertEqual(set(codes(x)), {'alias_spacing_only'})
        for text in (REASONS['alias_spacing_only'], SPACING_REFUSAL):
            self.assertIn('공백 차이가 없을 수 있습니다', text)
            self.assertIn('사람이 사전에 추가하세요', text)
        self.assertIn('묶음', REASONS['alias_spacing_only'])
        self.assertIn('묶음 전체', SPACING_REFUSAL)

    def test_bundle_bystander_revision_is_held_with_a_group_reason(self):
        src = f'{B} 정산 {A} 끝'
        prev = stored(src, [entity('정산', quote='정산'), entity(A, quote=A), entity('끝', quote='끝')],
                      [{'alias': B, 'entity': '정산', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [
            revision('aliases', B, {'alias': B, 'entity': '정산'}, {'alias': B, 'entity': A}, r),
            revision('entities', '끝', entity('끝'), entity('끝', kind='문서'), r)]}, prev)
        self.assertEqual([e['status'] for e in c], ['deferred', 'deferred'])
        self.assertEqual({e['reason'] for e in c}, {SPACING_REFUSAL})
        self.assertIn('묶음 전체', c[1]['reason'])

    def test_a_type_mismatch_keeps_its_own_failure_when_a_spacing_pair_is_also_present(self):
        src = f'{B} 정산 {A}'
        d, x, c = reconcile(src, {'entities': [entity(B), entity('정산', kind='문서'), entity(A)],
                                  'aliases': [{'alias': B, 'entity': '정산'}, {'alias': '정산', 'entity': A}]})
        self.assertNotIn(SPACING_REFUSAL, [e['reason'] for e in c])
        self.assertNotIn('alias_spacing_only', codes(x))

    def test_inherited_tied_alias_moved_onto_another_tied_target_is_deferred(self):
        src = f'{A} {B} 끝'
        prev = stored(src, [entity('갑', A, quote=A), entity('을', A, quote=A)], [{'alias': B, 'entity': '갑', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision(
            'aliases', B, {'alias': B, 'entity': '갑'}, {'alias': B, 'entity': '을'}, r)]}, prev)
        self.assertEqual(aliases(d), [(B, '갑')])
        self.assertEqual(c[0]['status'], 'deferred')

    def test_entity_whose_own_id_and_name_differ_only_in_spacing_still_merges(self):
        # The ID/name pair is the entity row itself (out of scope); the merge links nothing new.
        for entity_id, name in ((A, '납품\n계획'), (B, A)):
            d, x, c = reconcile(f'{entity_id} {name} 갑 끝', {'entities': [entity(entity_id, name), entity('갑'), entity('끝')],
                                                           'aliases': [{'alias': entity_id, 'entity': '갑'}, {'alias': '끝', 'entity': '갑'}]})
            self.assertEqual(ids(d), ['갑'], repr(name))
            self.assertEqual(x, [])
            self.assertEqual({e['status'] for e in c}, {'applied'})

    def test_alias_spacing_equal_to_the_id_only(self):
        d, x, _ = reconcile('AB 정산 문서', {'entities': [entity('A B', '정산 문서')], 'aliases': [{'alias': 'AB', 'entity': 'A B'}]})
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])


class NameDiffersFromIdOnlyInSpacing(unittest.TestCase):
    """An alias equal to the target's name still links two spellings when that name is a spacing variant of the ID."""

    def test_alias_equal_to_such_a_name_is_refused(self):
        d, x, _ = reconcile(f'{A} {B}', {'entities': [entity(A, B)], 'aliases': [{'alias': B, 'entity': A}]})
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_it_cannot_merge_two_entities(self):
        d, x, _ = reconcile(f'{A} {B}', {'entities': [entity(A, B), entity(B)], 'aliases': [{'alias': B, 'entity': A}]})
        self.assertEqual(ids(d), [A, B])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_it_cannot_follow_a_rename_in_the_same_response(self):
        src = f'{A} {B} 정산'
        prev = stored(src, [entity(A, quote=A)], [])
        d, x, c = reconcile(src, {'aliases': [{'alias': B, 'entity': A}], 'revisions': lambda r: [revision(
            'entities', A, entity(A), entity(A, B), r)]}, prev)
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])

    def test_wrapped_name(self):
        wrapped = '납품\n계획'
        d, x, _ = reconcile(f'{A} {wrapped}', {'entities': [entity(A, wrapped)], 'aliases': [{'alias': wrapped, 'entity': A}]})
        self.assertEqual(aliases(d), [])
        self.assertEqual(codes(x), ['alias_spacing_only'])


class TargetsOwnNameAndId(unittest.TestCase):
    """An alias spelling resolves to the ID; a demoted entity's name does not, so a target's own pair is not a tie there."""
    W = '납품\n계획'

    def test_identity_row_on_an_entity_whose_name_is_a_spacing_variant_of_its_id(self):
        for name in (B, self.W):
            d, x, _ = reconcile(f'{A} {name}', {'entities': [entity(A, name)], 'aliases': [{'alias': A, 'entity': A}]})
            self.assertEqual((ids(d), x), ([A], []), repr(name))

    def test_same_named_duplicate_merges_into_such_a_target(self):
        for name in (B, self.W):
            d, x, c = reconcile(f'{A} {name} ENT-9', {'entities': [entity(A, name), entity('ENT-9', name)],
                                                    'aliases': [{'alias': 'ENT-9', 'entity': A}]})
            self.assertEqual((ids(d), x), ([A], []), repr(name))
            self.assertEqual({e['status'] for e in c}, {'applied'})

    def test_same_named_duplicate_merges_through_a_chain_into_such_a_target(self):
        d, x, c = reconcile(f'ENT-9 정산 {A} {self.W}', {'entities': [entity('ENT-9', self.W), entity('정산'), entity(A, self.W)],
                                                      'aliases': [{'alias': 'ENT-9', 'entity': '정산'}, {'alias': '정산', 'entity': A}]})
        self.assertEqual((ids(d), x), ([A], []))
        self.assertEqual({e['status'] for e in c}, {'applied'})


class Kept(unittest.TestCase):
    def test_alias_equal_to_target_name(self):
        d, x, _ = reconcile(f'ENT-7은 {A}이다.', {'entities': [entity('ENT-7', A)], 'aliases': [{'alias': A, 'entity': 'ENT-7'}]})
        self.assertEqual(aliases(d), [(A, 'ENT-7')])
        self.assertEqual(x, [])

    def test_identity_row_when_id_differs_from_name(self):
        d, x, _ = reconcile(f'ENT-7 {A}', {'entities': [entity('ENT-7', A)], 'aliases': [{'alias': 'ENT-7', 'entity': 'ENT-7'}]})
        self.assertEqual(ids(d), ['ENT-7'])
        self.assertEqual(x, [])

    def test_alias_with_no_spacing_relation(self):
        d, x, _ = reconcile(f'{A}은 정산 문서이다.', {'entities': [entity(A)], 'aliases': [{'alias': '정산 문서', 'entity': A}]})
        self.assertEqual(aliases(d), [('정산 문서', A)])
        self.assertEqual(x, [])

    def test_exact_replay_of_a_kept_alias(self):
        src = f'갑 {A}'
        prev = stored(src, [entity('갑', quote='갑')], [{'alias': A, 'entity': '갑', 'quote': src}])
        d, x, _ = reconcile(src, {'entities': [entity('갑')], 'aliases': [{'alias': A, 'entity': '갑'}]}, prev)
        self.assertEqual(aliases(d), [(A, '갑')])
        self.assertEqual(x, [])

    def test_same_type_merge_without_spacing_relation(self):
        d, x, _ = reconcile('초개인화 (Hyper-Personalization)', {
            'entities': [entity('초개인화'), entity('Hyper-Personalization')],
            'aliases': [{'alias': 'Hyper-Personalization', 'entity': '초개인화'}]})
        self.assertEqual(ids(d), ['초개인화'])
        self.assertEqual(x, [])

    def test_variants_on_different_targets(self):
        d, x, _ = reconcile(f'갑 을 {A} {B}', {'entities': [entity('갑'), entity('을')],
                                              'aliases': [{'alias': A, 'entity': '갑'}, {'alias': B, 'entity': '을'}]})
        self.assertEqual(len(aliases(d)), 2)
        self.assertEqual(x, [])

    def test_refused_row_does_not_block_revision_on_those_entities(self):
        src = f'{A} {B} 끝'
        prev = stored(src, [entity(A, quote=A), entity(B, quote=B)], [])
        d, x, c = reconcile(src, {'aliases': [{'alias': B, 'entity': A}], 'revisions': lambda r: [revision(
            'entities', B, entity(B), entity(B, kind='문서'), r)]}, prev)
        self.assertEqual(c[0]['status'], 'applied')
        self.assertEqual(next(e for e in d['entities'] if e['id'] == B)['type'], '문서')

    def test_existing_tie_in_a_resumed_prefix_does_not_defer_unrelated_revision(self):
        src = f'{A} {B} 끝'
        prev = stored(src, [entity(A, quote=A), entity('끝', quote='끝')], [{'alias': B, 'entity': A, 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision(
            'entities', '끝', entity('끝'), entity('끝', kind='문서'), r)]}, prev)
        self.assertEqual(c[0]['status'], 'applied')

    def test_existing_tie_does_not_defer_unrelated_merge(self):
        src = f'{A} {B} 끝 끝말 문장'
        prev = stored(src, [entity(A, quote=A), entity('끝', quote='끝'), entity('끝말', quote='끝말')],
                      [{'alias': B, 'entity': A, 'quote': src}])
        d, x, c = reconcile(src, {'aliases': [{'alias': '끝말', 'entity': '끝'}]}, prev)
        self.assertEqual(ids(d), [A, '끝'])

    def test_person_can_still_add_in_one_passage(self):
        src = f'{A} {B}'
        d = stored(src, [entity(A, quote=A)], [{'alias': B, 'entity': A, 'quote': src}])
        self.assertEqual(aliases(d), [(B, A)])


class Neighbours(unittest.TestCase):
    def test_variants_on_different_targets_can_move_one_of_them(self):
        src = f'갑 을 병 {A} {B}'
        prev = stored(src, [entity('갑', quote='갑'), entity('을', quote='을'), entity('병', quote='병')],
                      [{'alias': A, 'entity': '갑', 'quote': src}, {'alias': B, 'entity': '을', 'quote': src}])
        d, x, c = reconcile(src, {'revisions': lambda r: [revision(
            'aliases', B, {'alias': B, 'entity': '을'}, {'alias': B, 'entity': '병'}, r)]}, prev)
        self.assertEqual(c[0]['status'], 'applied')
        self.assertIn((B, '병'), aliases(d))

    def test_demoted_name_against_an_alias_on_another_target_still_merges(self):
        src = f'병 {A} ENT-2 {B} 갑'
        prev = stored(src, [entity('병', quote='병')], [{'alias': A, 'entity': '병', 'quote': src}])
        d, x, c = reconcile(src, {'entities': [entity('ENT-2', B), entity('갑')], 'aliases': [{'alias': 'ENT-2', 'entity': '갑'}]}, prev)
        self.assertEqual(ids(d), ['병', '갑'])
        self.assertEqual(x, [])

    def test_merge_that_moves_an_inherited_sibling_pair_is_deferred(self):
        src = f'{A} {B} 갑 을'
        prev = stored(src, [entity('갑', quote='갑'), entity('을', quote='을')],
                      [{'alias': A, 'entity': '갑', 'quote': src}, {'alias': B, 'entity': '갑', 'quote': src}])
        d, x, c = reconcile(src, {'aliases': [{'alias': '갑', 'entity': '을'}]}, prev)
        self.assertEqual(ids(d), ['갑', '을'])
        self.assertTrue(any(e['reason'] == SPACING_REFUSAL for e in c))


class InheritedTie(unittest.TestCase):
    """A tie a resumed older prefix already holds is not a new link, and is not reported as refused."""
    def test_exact_replay_of_a_tied_alias_is_kept_and_not_reported(self):
        prev = stored(f'갑 {A} {B}', [entity('갑', A, quote=A)], [{'alias': B, 'entity': '갑', 'quote': f'갑 {A} {B}'}])
        d, x, _ = reconcile(f'갑 {A} {B}', {'entities': [entity('갑', A)], 'aliases': [{'alias': B, 'entity': '갑'}]}, prev)
        self.assertEqual(aliases(d), [(B, '갑')])
        self.assertEqual(x, [])

    def test_identity_row_on_an_entity_holding_a_tied_alias_is_not_reported(self):
        src = f'{A} {B}'
        prev = stored(src, [entity(A, quote=A)], [{'alias': B, 'entity': A, 'quote': src}])
        d, x, _ = reconcile(src, {'entities': [entity(A)], 'aliases': [{'alias': A, 'entity': A}]}, prev)
        self.assertEqual(x, [])

    def test_identity_row_when_id_differs_from_name_beside_an_alias_spacing_equal_to_the_id(self):
        src = f'{A} {B} 정산'
        prev = stored(src, [entity(B, '정산', quote='정산')], [{'alias': A, 'entity': B, 'quote': src}])
        d, x, _ = reconcile(src, {'entities': [entity(B, '정산')], 'aliases': [{'alias': B, 'entity': B}]}, prev)
        self.assertEqual(x, [])

    def test_alias_equal_to_the_name_is_kept_beside_an_inherited_spacing_alias(self):
        src = f'ENT-1 {A} {B}'
        prev = stored(src, [entity('ENT-1', A, quote=A)], [{'alias': B, 'entity': 'ENT-1', 'quote': src}])
        d, x, _ = reconcile(src, {'entities': [entity('ENT-1', A)], 'aliases': [{'alias': A, 'entity': 'ENT-1'}]}, prev)
        self.assertEqual(x, [])
        self.assertIn((B, 'ENT-1'), aliases(d))


class Policy(unittest.TestCase):
    def test_filter_policy_is_6_and_older_policies_stay_readable(self):
        self.assertEqual(FILTER_POLICY, 6)
        self.assertEqual(SUPPORTED_FILTER_POLICIES, (1, 2, 3, 4, 5, 6))

    def test_code_refused_under_older_policy_and_wrong_kind(self):
        run = {'id': 'r', 'document_id': 'd', 'version': 1}
        doc = {'sha256': 's', 'text': 'x' * 10}
        report = add_chunk(new_report(run, doc), 0, 10, [{'kind': 'aliases', 'row': 1, 'identifier': 'a', 'code': 'alias_spacing_only'}])
        self.assertEqual(validate_report(report, run, doc, filter_policy=FILTER_POLICY)['items'][0]['code'], 'alias_spacing_only')
        with self.assertRaises(ValueError):
            validate_report(report, run, doc, filter_policy=4)
        wrong = copy.deepcopy(report)
        wrong['items'][0]['kind'] = 'relations'
        wrong['chunks'][0]['counts'] = {'entities': 0, 'aliases': 0, 'relations': 1}
        with self.assertRaises(ValueError):
            validate_report(wrong, run, doc)
        self.assertIn(SPACING, public_report(report)['items'][0]['reason'])

    def test_prompts_are_unchanged(self):
        for name, digest in PROMPT_SHA256.items():
            self.assertEqual(hashlib.sha256(getattr(ontology, name).encode()).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
