import copy
import json
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from verinote.assistant import MAX_CONTEXT, PROMPT
from verinote.question_context import PROJECTION_VERSION, PROTOCOL, project, resolve
from verinote.store import canonical


def state():
    meaning = {'id': 'tea', 'ref': 'entity:tea', 'name': '차', 'type': '음료',
               'document_id': 'd1', 'ontology_revision': 1, 'quote': '합성 인용문 ' * 300,
               'approval': 'approved', 'historical_pin': True}
    other = dict(meaning, id='vehicle', ref='entity:vehicle', type='이동수단')
    relation = {'id': 'likes', 'ref': 'relation:likes', 'approval': 'approved'}
    fact = {'id': 'f1', 'revision': 1, 'document_id': 'd1', 'version': 1,
            'payload': {'subject': '차', 'predicate': 'likes', 'object': '차', 'quote': '정확한 증거 ' * 100}}
    refs = [meaning['ref'], relation['ref'], other['ref']]
    graph = {'rows': [['차', 'likes', '차']], 'row_refs': [refs], 'status': 'success',
             'query': {'subject': '차'}, 'entities': [meaning, other], 'relations': [relation],
             'entity_candidates': {'subject': [meaning, other], 'target': []},
             'predicate_candidates': [relation], 'facts': [fact],
             'fact_refs': [{'id': 'f1', 'subject_ref': refs[0], 'predicate_ref': refs[1], 'object_ref': refs[2]}],
             'proofs': [{'row': ['차', 'likes', '차'], 'row_refs': refs, 'fact_ids': ['f1']}],
             'rules': 'relation(X,Y) :- fact(X,Y).\n' * 20, 'warnings': ['scope limited'], 'truncated': False}
    return {'snapshot': 's1', 'catalog': {'entity_meanings': [meaning, other], 'relation_meanings': [relation]},
            'evidence': {f'E{i}': {'kind': 'graph', 'result': dict(graph, query={'subject': f'query{i}'})} for i in range(1, 4)},
            'trace': [{'status': 'success', 'result': {'lookup_id': 'K1', 'truncated': False,
                       'documents': [{'document_id': 'd1'}], 'next_offset': None}}]}


class ContextTests(unittest.TestCase):
    def test_repeated_valid_results_fit_without_losing_scoped_proofs(self):
        raw = state(); original = copy.deepcopy(raw)
        self.assertTrue(all(len(canonical(e).encode()) < 30000 for e in raw['evidence'].values()))
        self.assertGreater(len((PROMPT + canonical(raw)).encode()), MAX_CONTEXT)
        projected = project(raw)
        self.assertLess(len((PROTOCOL + PROMPT + canonical(projected.state)).encode()), MAX_CONTEXT)
        self.assertEqual(resolve(projected.state), original)
        self.assertEqual(raw, original)
        self.assertEqual(project(raw), projected)
        self.assertEqual(projected.evidence_ids, {'E1', 'E2', 'E3'})
        self.assertEqual(projected.lookup_ids, {'K1'})

    def test_same_ids_different_content_revisions_and_snapshot_are_distinct(self):
        raw = state()
        raw['evidence']['E2']['result'] = copy.deepcopy(raw['evidence']['E2']['result'])
        raw['evidence']['E2']['result']['facts'][0]['revision'] = 2
        raw['evidence']['E2']['result']['facts'][0]['payload']['quote'] = '수정된 인용'
        projected = project(raw)
        restored = resolve(projected.state)
        facts = [e['result']['facts'][0] for e in restored['evidence'].values()]
        self.assertEqual({f['revision'] for f in facts}, {1, 2})
        self.assertEqual(facts[1]['payload']['quote'], '수정된 인용')
        self.assertEqual(resolve(projected.state), raw)
        changed = copy.deepcopy(raw); changed['snapshot'] = 's2'
        self.assertNotEqual(projected.state['context_raw_sha256'], project(changed).state['context_raw_sha256'])
        corrupt = copy.deepcopy(projected.state); corrupt['snapshot'] = 's2'
        with self.assertRaisesRegex(ValueError, 'content mismatch'): resolve(corrupt)

    def test_tampering_and_missing_scoped_refs_fail_closed(self):
        projected = project(state()).state
        corrupt = copy.deepcopy(projected); corrupt['context_tables'].pop(next(iter(corrupt['context_tables'])))
        with self.assertRaises(ValueError): resolve(corrupt)
        corrupt = copy.deepcopy(projected); next(iter(corrupt['context_tables'].values()))['value'] = 'changed'
        with self.assertRaises(ValueError): resolve(corrupt)
        raw = state(); raw['evidence']['E1']['result']['fact_refs'][0]['subject_ref'] = 'missing'
        with self.assertRaises(ValueError): project(raw)

    def test_partial_text_offsets_and_lookup_completeness_are_inline_exact(self):
        raw = state()
        raw['evidence']['E4'] = {'kind': 'wiki', 'text': '차는 합성 문서의 예시', 'offset': 2000,
            'end_offset': 2013, 'next_offset': 2013, 'total_chars': 3000, 'partial': True,
            'claim_indices': [3, 4], 'revision': 7, 'approval': 'approved'}
        raw['trace'][0]['result']['truncated'] = True
        projection = project(raw)
        self.assertEqual(projection.state['evidence']['E4'], raw['evidence']['E4'])
        self.assertEqual(projection.state['trace'], raw['trace'])
        self.assertEqual(resolve(projection.state), raw)

    def test_failed_lookup_not_delivered_and_reserved_marker_rejected(self):
        raw = state(); raw['trace'][0]['status'] = 'error'
        self.assertFalse(project(raw).lookup_ids)
        raw['catalog']['malicious'] = {'$context_ref': 'not-a-record'}
        with self.assertRaises(ValueError): project(raw)


class NestedContextTests(unittest.TestCase):
    @staticmethod
    def encoded(payload, tables=None, shapes=None):
        return {'snapshot': 's', 'evidence': {}, 'trace': [], 'payload': payload,
                'context_projection_version': PROJECTION_VERSION,
                'context_tables': tables or {}, 'context_shapes': shapes or {},
                'context_raw_sha256': '0' * 64}

    def test_immutable_v1_fixture_remains_resolvable(self):
        path = Path(__file__).with_name('fixtures') / 'question-context-v1.json'
        blob = path.read_bytes()
        self.assertEqual(sha256(blob).hexdigest(), '5ae2440e15d84574e2b829697a206c183102d58bb34ebf5ac6c0c13ee1b4d7da')
        fixture = json.loads(blob)
        self.assertEqual(fixture['projected']['context_projection_version'], 1)
        self.assertEqual(resolve(fixture['projected']), fixture['raw'])

    def test_nested_tables_and_shapes_roundtrip_types_keys_and_order(self):
        raw = state()
        fields = ['긴 Unicode 필드 이름 ' + str(i) for i in range(10)]
        values = [None, False, True, 1, 1.0, [], {}, '문자열 $r 또는 $o', ['차'], {'x': 'y'}]
        records = [dict(zip(fields, values, strict=True)), dict(zip(fields, reversed(values), strict=True))]
        raw['catalog']['entity_meanings'] = records + copy.deepcopy(records)
        projected = project(raw)
        self.assertTrue(projected.state['context_shapes'])
        self.assertTrue(any('"$r"' in canonical(e['value']) for e in projected.state['context_tables'].values()))
        recovered = resolve(projected.state)
        self.assertEqual(canonical(recovered), canonical(raw))
        self.assertEqual(list(recovered['catalog']['entity_meanings'][0]), fields)
        self.assertIs(type(recovered['catalog']['entity_meanings'][0][fields[3]]), int)
        self.assertIs(type(recovered['catalog']['entity_meanings'][0][fields[4]]), float)
        # Different object key orders retain their own ordered shape declarations.
        raw['catalog']['entity_meanings'] += [dict(reversed(list(records[0].items())))] * 2
        recovered = resolve(project(raw).state)
        self.assertEqual(list(recovered['catalog']['entity_meanings'][-1]), list(reversed(fields)))

    def test_small_object_key_order_survives_repeated_subtrees(self):
        raw = state()
        first = {'a': 'repeated string value', 'b': None}
        second = {'b': None, 'a': 'repeated string value'}
        raw['catalog']['entity_meanings'] = [first, second, first, second]
        restored = resolve(project(raw).state)['catalog']['entity_meanings']
        self.assertEqual([list(v) for v in restored], [['a', 'b'], ['b', 'a'], ['a', 'b'], ['b', 'a']])

    def test_missing_cycles_and_invalid_table_schema(self):
        cases = [self.encoded({'$r': 'absent'}),
                 self.encoded({'$r': 'a'}, {'a': {'kind': 'array', 'value': [{'$r': 'a'}]}}),
                 self.encoded({'$r': 'a'}, {'a': {'kind': 'array', 'value': [{'$r': 'b'}]},
                                            'b': {'kind': 'array', 'value': [{'$r': 'a'}]}}),
                 self.encoded(None, {'a': {'kind': 'unknown', 'value': []}}),
                 self.encoded(None, {'a': {'kind': 'array', 'value': [], 'extra': 1}}),
                 self.encoded({'$r': ['a']}), self.encoded({'$r': 'a', 'other': 1})]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError): resolve(item)

    def test_malformed_shapes_rejected_before_reconstruction(self):
        cases = [self.encoded({'$o': ['missing', []]}),
                 self.encoded({'$o': ['s', [1]]}, shapes={'s': ['a', 'a']}),
                 self.encoded({'$o': ['s', [1]]}, shapes={'s': [1]}),
                 self.encoded({'$o': ['s', [1]]}, shapes={'s': ['$r']}),
                 self.encoded({'$o': ['s', []]}, shapes={'s': ['a']}),
                 self.encoded({'$o': ['s', [1, 2]]}, shapes={'s': ['a']}),
                 self.encoded({'$o': [{'x': 1}, []]}, shapes={'s': ['a']}),
                 self.encoded({'$o': ['s', []], 'extra': 1}, shapes={'s': []})]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError): resolve(item)

    def test_exponential_dag_charged_for_every_logical_occurrence(self):
        tables = {'0': {'kind': 'string', 'value': 'shared text'}}
        for i in range(1, 30):
            tables[str(i)] = {'kind': 'array', 'value': [{'$r': str(i - 1)}] * 2}
        with self.assertRaisesRegex(ValueError, 'expansion'):
            resolve(self.encoded({'$r': '29'}, tables))
        with patch('verinote.question_context.MAX_EXPANDED_NODES', 100):
            with self.assertRaisesRegex(ValueError, 'expansion'):
                resolve(self.encoded({'$r': '8'}, dict(list(tables.items())[:9])))

    def test_expanded_shape_keys_and_reference_depth_are_bounded(self):
        encoded = self.encoded([{'$o': ['s', [None]]}] * 12, shapes={'s': ['긴키' * 500]})
        self.assertLess(len(canonical(encoded).encode()), 10000)
        with patch('verinote.question_context.MAX_EXPANDED_BYTES', 10000):
            with self.assertRaisesRegex(ValueError, 'expansion'): resolve(encoded)
        tables = {'0': {'kind': 'string', 'value': 'leaf'}}
        for i in range(1, 110):
            tables[str(i)] = {'kind': 'array', 'value': [{'$r': str(i - 1)}]}
        with self.assertRaisesRegex(ValueError, 'depth'): resolve(self.encoded({'$r': '109'}, tables))
        raw = state(); raw['catalog']['entity_meanings'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'finite'): project(raw)

    def test_no_profitable_sharing_keeps_values_inline(self):
        raw = {'snapshot': 's', 'evidence': {}, 'trace': [], 'catalog': {'entity_meanings': [[], [], {}, {}]}}
        projected = project(raw)
        self.assertEqual(projected.state['context_tables'], {})
        self.assertEqual(projected.state['context_shapes'], {})
        self.assertEqual(resolve(projected.state), raw)

    def test_hash_collisions_are_checked_internally(self):
        class ConstantHash:
            def hexdigest(self): return '0' * 64
        with patch('verinote.question_context.sha256', return_value=ConstantHash()):
            with self.assertRaisesRegex(ValueError, 'collision'): project(state())
