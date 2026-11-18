"""A name merged away in an earlier chunk still refuses spacing-only ties (#884).

The provenance (`merged_names`) lives in the run checkpoint only; a whitespace-free comparison only refuses. Synthetic spellings.
"""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_dictionary_exclusions as exclusion_tests
import test_workflow as workflow
from test_spacing_links import aliases, entity, ids, revision, stored

from verinote import dictionary_pipeline
from verinote.dictionary_filter import reconcile_generated_dictionary
from verinote.dictionary_reconciliation import (
    MAX_MERGED_NAMES_BYTES,
    SPACING_REFUSAL,
    merged_name_ties,
    quote_spacing_variant,
    read_report,
    spacing_conflict,
    validate_merged_names,
)
from verinote.ontology import dictionary_context, source_spans
from verinote.usage import atomic_json as real_atomic_json

NAME = '납품 계획'
B, C, D, E, G = '납품계획', '납품계 획', '납 품 계획', '납 품계획', '납품 계 획'
FIRST = f'T-1 Target A-1 {NAME} U-1 Other W-1 Winner Y-1 Why Z-1 Zed {B} {C} {D} {E} {G}'
SECOND = f'T-1 Target U-1 Other W-1 Winner Y-1 Why Z-1 Zed Whyy Quux {C} {D} {E} {B}'
EMPTY = {'entities': [], 'aliases': [], 'relations': []}
FIRST_OUTPUT = {
    'entities': [entity('T-1', 'Target'), entity('A-1', NAME), entity('U-1', 'Other'), entity('W-1', 'Winner'),
                 entity('Y-1', 'Why'), entity('Z-1', 'Zed')],
    'aliases': [{'alias': 'A-1', 'entity': 'T-1'}, {'alias': B, 'entity': 'U-1'}, {'alias': G, 'entity': 'W-1'}],
    'relations': [],
}
UNRELATED = [{'alias': 'Z-1', 'entity': 'Y-1'}, {'alias': 'Whyy', 'entity': 'Y-1'}]
CASES = {
    'alias_add': dict(aliases=[{'alias': C, 'entity': 'T-1'}], gone=lambda d: (C, 'T-1') not in aliases(d)),
    'id_merge': dict(entities=[entity(D, 'Quux')], aliases=[{'alias': D, 'entity': 'T-1'}], gone=lambda d: D in ids(d)),
    'alias_move': dict(revisions=lambda r: [revision('aliases', B, {'alias': B, 'entity': 'U-1'}, {'alias': B, 'entity': 'T-1'}, r)],
                       gone=lambda d: (B, 'U-1') in aliases(d)),
    'rename': dict(revisions=lambda r: [revision('entities', 'T-1', {'id': 'T-1', 'name': 'Target', 'type': '산출물'},
                                                  {'id': 'T-1', 'name': E, 'type': '산출물'}, r)],
                   gone=lambda d: next(x for x in d['entities'] if x['id'] == 'T-1')['name'] == 'Target'),
    'merge_target': dict(aliases=[{'alias': 'T-1', 'entity': 'W-1'}], gone=lambda d: 'T-1' in ids(d)),
}


def step(previous, merged, prefix, chunk, spec):
    spans = source_spans(chunk)
    ref = [spans[0]['id'], spans[-1]['id']]
    proposed = {kind: [dict(row, quote_ref=ref) for row in spec.get(kind, [])] for kind in ('entities', 'aliases', 'relations')}
    if 'revisions' in spec:
        proposed['revisions'] = spec['revisions'](ref)
    fresh: list[dict] = []
    result = reconcile_generated_dictionary(previous, proposed, spans, prefix + chunk, chunk, merged_names=merged, new_merged=fresh)
    return result + (fresh,)


def first_chunk():
    dictionary, excluded, changes, merged = step(copy.deepcopy(EMPTY), [], '', FIRST, FIRST_OUTPUT)
    assert excluded == [] and all(c['status'] == 'applied' for c in changes), (excluded, changes)
    return dictionary, merged


class Cases(unittest.TestCase):
    def test_first_chunk_records_the_merged_name_and_where_it_stands(self):
        dictionary, merged = first_chunk()
        self.assertEqual(ids(dictionary), ['T-1', 'U-1', 'W-1', 'Y-1', 'Z-1'])
        self.assertEqual(len(merged), 1)
        row = merged[0]
        self.assertEqual((row['name'], row['carrier']), (NAME, 'A-1'))
        self.assertEqual(FIRST[row['offset']:row['offset'] + len(NAME)], NAME)

    def test_five_cases_are_deferred_and_unrelated_changes_still_apply(self):
        dictionary, merged = first_chunk()
        for label, case in CASES.items():
            spec = {k: v for k, v in case.items() if k != 'gone'}
            spec['aliases'] = spec.get('aliases', []) + UNRELATED
            spec['entities'] = spec.get('entities', [])
            with self.subTest(label=label):
                control, _, _, _ = step(dictionary, [], FIRST, SECOND, spec)
                self.assertFalse(case['gone'](control), 'without provenance the tie is linked (the bug)')
                result, excluded, changes, fresh = step(dictionary, merged, FIRST, SECOND, spec)
                self.assertTrue(case['gone'](result))
                self.assertTrue(any(c['status'] == 'deferred' and c['reason'] == SPACING_REFUSAL for c in changes) or excluded)
                if 'revisions' not in spec:
                    self.assertNotIn('Z-1', ids(result))
                    self.assertIn(('Whyy', 'Y-1'), aliases(result))
                    self.assertIn(('Z-1', 'Y-1'), aliases(result))
                self.assertNotIn(NAME, [row['name'] for row in fresh])

    def test_unrelated_revision_and_name_still_apply(self):
        dictionary, merged = first_chunk()
        spec = {'revisions': lambda r: [revision('entities', 'Y-1', {'id': 'Y-1', 'name': 'Why', 'type': '산출물'},
                                                  {'id': 'Y-1', 'name': 'Quux', 'type': '산출물'}, r)]}
        result, _, changes, _ = step(dictionary, merged, FIRST, SECOND, spec)
        self.assertEqual({c['status'] for c in changes}, {'applied'})
        self.assertEqual(next(x for x in result['entities'] if x['id'] == 'Y-1')['name'], 'Quux')

    def test_chain_follows_the_carrier_and_remembers_the_new_name(self):
        dictionary, merged = first_chunk()
        text = 'T-1 Target Y-1 Why'
        merged_into, excluded, _, fresh = step(dictionary, merged, FIRST, text, {'aliases': [{'alias': 'T-1', 'entity': 'Y-1'}]})
        self.assertEqual((ids(merged_into), excluded), (['U-1', 'W-1', 'Y-1', 'Z-1'], []))
        self.assertEqual([(r['name'], r['carrier']) for r in fresh], [('Target', 'T-1')])
        late = FIRST + text
        for variant in ('납품 계  획', 'Tar get'):
            with self.subTest(variant=variant):
                result, x, _, _ = step(merged_into, merged + fresh, late, f'Y-1 Why {variant}', {'aliases': [{'alias': variant, 'entity': 'Y-1'}]})
                self.assertEqual(([a for a in aliases(result) if a[0] == variant], [r['code'] for r in x]), ([], ['alias_spacing_only']))

    def test_inherited_tie_does_not_block_unrelated_changes_but_cannot_move(self):
        source = f'T-1 Target A-1 {NAME} W-1 Winner {B} Whyy Y-1 Why'
        previous = stored(source, [entity('T-1', 'Target', quote=source), entity('W-1', 'Winner', quote=source), entity('Y-1', 'Why', quote=source)],
                          [{'alias': 'A-1', 'entity': 'T-1', 'quote': source}, {'alias': B, 'entity': 'T-1', 'quote': source}])
        merged = [{'name': NAME, 'carrier': 'A-1', 'offset': source.index(NAME)}]
        later = 'T-1 Target Whyy Y-1 Why W-1 Winner Other'
        result, excluded, changes, _ = step(previous, merged, source, later, {'aliases': [{'alias': 'Whyy', 'entity': 'Y-1'}, {'alias': 'Other', 'entity': 'T-1'}]})
        self.assertEqual((excluded, [(a, e) for a, e in aliases(result) if a in ('Whyy', 'Other')]), ([], [('Whyy', 'Y-1'), ('Other', 'T-1')]))
        moved, _, events, _ = step(previous, merged, source, later, {'aliases': [{'alias': 'T-1', 'entity': 'W-1'}]})
        self.assertIn('T-1', ids(moved))
        self.assertEqual([e['status'] for e in events], ['deferred'])

    def test_exact_name_still_held_is_not_a_lost_name(self):
        source = f'T-1 Target A-1 {NAME} {B}'
        previous = stored(source, [entity('T-1', NAME, quote=source)], [{'alias': 'A-1', 'entity': 'T-1', 'quote': source}])
        merged = [{'name': NAME, 'carrier': 'A-1', 'offset': source.index(NAME)}]
        entities = {r['id']: r for r in previous['entities']}
        self.assertFalse(spacing_conflict('Other', 'T-1', entities, previous['aliases'], merged))
        self.assertTrue(spacing_conflict(B, 'T-1', entities, previous['aliases'], []))


class Measurement(unittest.TestCase):
    """Labeled synthetic outcomes: provenance versus inferring ties from the target's alias quotes (the option not chosen)."""
    SYLLABLES = '가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허'
    N = 60

    def words(self, i):
        s = self.SYLLABLES
        return (s[i % 25] + s[(i * 7 + 3) % 25], s[(i * 11 + 5) % 25] + s[(i * 3 + 1) % 25])

    def cases(self):
        for i in range(self.N):
            w1, w2 = self.words(i)
            name = f'{w1} {w2}'
            quote = f'T-1 Target A-1 {name}'
            yield 'tie', f'{w1}{w2}', name, quote, True
            yield 'unrelated', f'Q{i}', name, quote, False
            yield 'incidental', f'{w1} {w2}서', name, f'{quote} {w1}{w2}서', False

    def test_false_rejections_are_measured(self):
        outcome = {}
        for label, spelling, name, quote, should_defer in self.cases():
            target = {'id': 'T-1', 'name': 'Target', 'type': 'X'}
            rows = [{'alias': 'A-1', 'entity': 'T-1', 'quote': quote}]
            merged = [{'name': name, 'carrier': 'A-1', 'offset': quote.index(name)}]
            provenance = spacing_conflict(spelling, 'T-1', {'T-1': target}, rows, merged)
            by_quote = any(quote_spacing_variant(spelling, row['quote']) is not None for row in rows)
            for method, refused in (('provenance', provenance), ('alias_quote', by_quote)):
                counts = outcome.setdefault((method, label), [0, 0])
                counts[refused == should_defer] += 1  # [wrong, right]
        measured = {key: wrong for key, (wrong, _) in outcome.items()}
        self.assertEqual(measured, {('provenance', 'tie'): 0, ('provenance', 'unrelated'): 0, ('provenance', 'incidental'): 0,
                                    ('alias_quote', 'tie'): 0, ('alias_quote', 'unrelated'): 0, ('alias_quote', 'incidental'): self.N})


class Provenance(unittest.TestCase):
    def valid(self):
        dictionary, merged = first_chunk()
        report = {'chunks': [{'offset': 0, 'length': len(FIRST), 'applied': 1, 'deferred': 0}]}
        return dictionary, merged, report

    def test_validation_rejects_inconsistent_records(self):
        dictionary, merged, report = self.valid()
        self.assertEqual(validate_merged_names(merged, 1, dictionary, FIRST, report, len(FIRST)), merged)
        offset = merged[0]['offset']
        wrong = {'offset': [dict(merged[0], offset=offset + 1)], 'carrier': [dict(merged[0], carrier='Nope')],
                 'name': [dict(merged[0], name='납품 계획서')], 'extra': [dict(merged[0], quote='x')],
                 'twice': merged * 2, 'type': [dict(merged[0], offset=float(offset))], 'shape': {'a': 1}, 'beyond': merged}
        for label, rows in wrong.items():
            with self.subTest(label=label):
                accepted = len(FIRST) if label != 'beyond' else offset
                with self.assertRaises(ValueError):
                    validate_merged_names(rows, len(rows) if isinstance(rows, list) else 1, dictionary, FIRST, report, accepted)
        with self.assertRaises(ValueError):
            validate_merged_names(merged, 1, dictionary, FIRST, {'chunks': [dict(report['chunks'][0], applied=0)]}, len(FIRST))

    def test_count_must_match_the_rows(self):
        dictionary, merged, report = self.valid()
        extra = merged + [dict(merged[0], carrier='T-1')]
        for label, rows, count in (('row_dropped', [], 1), ('row_omitted_of_two', merged, 2), ('extra_row', extra, 1),
                                   ('count_low', merged, 0), ('count_missing', merged, None), ('count_bool', merged, True),
                                   ('count_str', merged, '1')):
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_merged_names(rows, count, dictionary, FIRST, report, len(FIRST))

    def test_requoted_or_moved_carrier_still_validates(self):
        dictionary, merged, report = self.valid()
        moved = copy.deepcopy(dictionary)
        carrier = next(row for row in moved['aliases'] if row['alias'] == 'A-1')
        carrier.update(entity='U-1', quote='U-1 Other')
        self.assertNotIn(NAME, carrier['quote'])
        self.assertEqual(validate_merged_names(merged, 1, moved, FIRST, report, len(FIRST)), merged)

    def test_ties_scale_with_names_and_spellings(self):
        count = 5000
        names = [f'가{i} 나{i}' for i in range(count)]
        dictionary = {'entities': [{'id': 'T', 'name': 'Target'}] + [{'id': f'E{i}', 'name': f'다{i}'} for i in range(count)],
                      'aliases': [{'alias': f'M{i}', 'entity': 'T'} for i in range(count)]
                      + [{'alias': f'가{i}나{i}', 'entity': 'T'} for i in range(0, count, 50)]}
        merged = [{'name': name, 'carrier': f'M{i}', 'offset': i} for i, name in enumerate(names)]
        started = time.perf_counter()
        ties = merged_name_ties(dictionary, merged)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(ties), count // 50)
        self.assertLess(elapsed, 0.5)

    def test_size_budget(self):
        name = '가' * 200
        count = MAX_MERGED_NAMES_BYTES // 700 + 1
        source = ''.join(f'{i:05}{name}' for i in range(count + 400))
        dictionary = {'aliases': [{'alias': f'A{i}', 'entity': 'T', 'quote': f'{i:05}{name}'} for i in range(count + 400)]}
        report = {'chunks': [{'offset': 0, 'length': len(source), 'applied': 1, 'deferred': 0}]}
        step_size = 205

        def rows(n):
            return [{'name': name, 'carrier': f'A{i}', 'offset': i * step_size + 5} for i in range(n)]
        limit = max(n for n in range(count, count + 400) if len(json.dumps(rows(n), ensure_ascii=False).encode()) <= MAX_MERGED_NAMES_BYTES)
        self.assertEqual(len(validate_merged_names(rows(limit), limit, dictionary, source, report, len(source))), limit)
        with self.assertRaises(ValueError):
            validate_merged_names(rows(limit + 1), limit + 1, dictionary, source, report, len(source))


class Pipeline(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    new_doc = exclusion_tests.Generation.new_doc
    run_job = exclusion_tests.Generation.run_job
    checkpoint = exclusion_tests.Generation.checkpoint
    PAD = 3999

    def document(self):
        return self.new_doc(FIRST.ljust(self.PAD, 'x') + '\n' + SECOND)

    @staticmethod
    def model(spec):
        def generate(payload):
            ref = [payload['sources'][0]['id'], payload['sources'][-1]['id']]
            value = {kind: [dict(row, quote_ref=ref) for row in spec.get(kind, [])] for kind in ('entities', 'aliases', 'relations')}
            if 'revisions' in spec:
                value['revisions'] = spec['revisions'](ref)
            return value
        return generate

    def first_run(self, doc):
        first, calls = self.run_job(doc, [self.model(FIRST_OUTPUT), TimeoutError('stop')])
        self.assertEqual(first['status'], 'error')
        return first, calls

    def test_checkpoint_keeps_the_name_and_resume_defers_every_case_without_sending_it_to_the_model(self):
        for label, case in CASES.items():
            spec = {k: v for k, v in case.items() if k != 'gone'}
            spec['aliases'] = spec.get('aliases', []) + UNRELATED
            with self.subTest(label=label):
                doc = self.document()
                first, _ = self.first_run(doc)
                saved = json.loads(self.checkpoint(first).read_text())
                self.assertEqual((saved['schema_version'], saved['contract']['filter_policy']), (4, 7))
                self.assertEqual([(r['name'], r['carrier']) for r in saved['merged_names']], [(NAME, 'A-1')])
                second, calls = self.run_job(doc, [self.model(spec)])
                self.assertEqual(second['status'], 'success', second['detail'])
                self.assertEqual([c['offset'] for c in calls], [saved['offset']])
                self.assertEqual(calls[0]['dictionary'], dictionary_context(saved['dictionary']))
                self.assertNotIn('merged_names', json.dumps(calls[0], ensure_ascii=False))
                self.assertNotIn('carrier', json.dumps(calls[0], ensure_ascii=False))
                result = json.loads(self.checkpoint(second).read_text())
                self.assertTrue(case['gone'](result['dictionary']))
                self.assertEqual(result['merged_names'][0], saved['merged_names'][0])
                refused = read_report(self.root, self.store, second['id'])['deferred'] + exclusion_tests.read_report(
                    self.root, self.store, second['id'])['total_count']
                self.assertGreaterEqual(refused, 1)

    def test_old_checkpoints_reuse_only_where_no_merge_could_be_lost(self):
        doc = self.document()
        first, _ = self.first_run(doc)
        path = self.checkpoint(first)
        saved = json.loads(path.read_text())
        old = dict(saved, schema_version=3)
        old.pop('merged_names')
        old.pop('merged_count')
        old['contract']['filter_policy'] = 6
        path.write_text(json.dumps(old))
        second, calls = self.run_job(doc, [self.model(EMPTY), TimeoutError('stop')])
        self.assertEqual([c['offset'] for c in calls][:1], [0], 'applied merge without a record restarts from the beginning')
        self.assertIn('제외', second['detail'])
        plain = self.document_without_merge()
        run, _ = self.run_job(plain, [{'entities': [entity('T-1', 'Target', quote=FIRST)], 'aliases': [], 'relations': []}, TimeoutError('stop')])
        path = self.checkpoint(run)
        value = json.loads(path.read_text())
        value.update(schema_version=3)
        value.pop('merged_names')
        value.pop('merged_count')
        value['contract']['filter_policy'] = 6
        path.write_text(json.dumps(value))
        again, calls = self.run_job(plain, [EMPTY])
        self.assertEqual((again['status'], [c['offset'] for c in calls]), ('success', [value['offset']]))
        for status in ('old', 'new'):
            with self.subTest(report=status):
                self.assertTrue(read_report(self.root, self.store, run['id'] if status == 'old' else again['id'])['available'])

    def document_without_merge(self):
        return self.new_doc('T-1 Target ' + 'y' * 3987 + '\n' + SECOND)

    def test_corrupt_provenance_restarts(self):
        for label, change in (('missing', lambda v: v.pop('merged_names')),
                              ('count_missing', lambda v: v.pop('merged_count')),
                              ('emptied', lambda v: v.update(merged_names=[])),
                              ('count_zeroed', lambda v: v.update(merged_count=0)),
                              ('extra_row', lambda v: v['merged_names'].append(dict(v['merged_names'][0], carrier='T-1'))),
                              ('count_raised', lambda v: v.update(merged_count=2)),
                              ('offset', lambda v: v['merged_names'][0].update(offset=0)),
                              ('carrier', lambda v: v['merged_names'][0].update(carrier='Nope')),
                              ('extra', lambda v: v['merged_names'][0].update(quote='x'))):
            with self.subTest(label=label):
                doc = self.document()
                first, _ = self.first_run(doc)
                path = self.checkpoint(first)
                value = json.loads(path.read_text())
                change(value)
                path.write_text(json.dumps(value))
                second, calls = self.run_job(doc, [self.model(FIRST_OUTPUT), self.model(EMPTY)])
                self.assertEqual(calls[0]['offset'], 0)

    def test_requoted_carrier_checkpoint_resumes_and_still_defers_the_variant(self):
        doc = self.document()
        first, _ = self.first_run(doc)
        path = self.checkpoint(first)
        saved = json.loads(path.read_text())
        carrier = next(row for row in saved['dictionary']['aliases'] if row['alias'] == 'A-1')
        carrier['quote'] = 'Target A-1'
        path.write_text(json.dumps(saved))
        spec = {'aliases': [{'alias': C, 'entity': 'T-1'}] + UNRELATED}
        second, calls = self.run_job(doc, [self.model(spec)])
        self.assertEqual((second['status'], [c['offset'] for c in calls]), ('success', [saved['offset']]))
        result = json.loads(self.checkpoint(second).read_text())
        self.assertNotIn((C, 'T-1'), aliases(result['dictionary']))
        self.assertIn(('Whyy', 'Y-1'), aliases(result['dictionary']))

    def test_failed_checkpoint_write_does_not_advance_the_provenance(self):
        doc = self.document()
        first, _ = self.first_run(doc)
        before = self.checkpoint(first).read_bytes()
        offset = json.loads(before)['offset']

        def flaky(path, value):
            if path.name == 'dictionary-checkpoint.json' and value.get('offset', 0) > offset:
                raise OSError('disk full')
            return real_atomic_json(path, value)
        spec = {'aliases': [{'alias': C, 'entity': 'T-1'}]}
        with patch.object(dictionary_pipeline, 'atomic_json', flaky):
            second, _ = self.run_job(doc, [self.model(spec)])
        self.assertEqual(second['status'], 'error')
        self.assertEqual(json.loads(self.checkpoint(second).read_text())['merged_names'], json.loads(before)['merged_names'])
        third, calls = self.run_job(doc, [self.model(spec)])
        self.assertEqual((third['status'], [c['offset'] for c in calls]), ('success', [offset]))
        self.assertNotIn(C, [a['alias'] for a in json.loads(self.checkpoint(third).read_text())['dictionary']['aliases']])

    def test_provenance_over_its_budget_fails_the_chunk(self):
        doc = self.document()
        with patch('verinote.dictionary_reconciliation.MAX_MERGED_NAMES_BYTES', 50):
            run, calls = self.run_job(doc, [self.model(FIRST_OUTPUT)])
        self.assertEqual((run['status'], len(calls)), ('error', 1))
        self.assertFalse(self.checkpoint(run).exists())


if __name__ == '__main__':
    unittest.main()
