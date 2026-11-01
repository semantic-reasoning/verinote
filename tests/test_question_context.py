import copy
import unittest

from verinote.assistant import MAX_CONTEXT, PROMPT
from verinote.question_context import PROTOCOL, project, resolve
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
        facts = [v['value'] for v in projected.state['context_tables'].values() if v['kind'] == 'fact']
        self.assertEqual({f['revision'] for f in facts}, {1, 2})
        self.assertEqual(resolve(projected.state), raw)
        changed = copy.deepcopy(raw); changed['snapshot'] = 's2'
        self.assertFalse(set(projected.state['context_tables']) & set(project(changed).state['context_tables']))

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
