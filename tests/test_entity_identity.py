"""Scoped meanings and proof identity through real Store snapshots and Wirelog."""
import copy
import tempfile
import unittest
from pathlib import Path

from test_workflow import legacy_run

from verinote.engine import query
from verinote.entity_identity import identity_graph, meaning_candidates
from verinote.store import Store, canonical, digest, now


class EntityIdentity(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'state.sqlite3')

    def document(self, title, entities, triples=(), *, aliases=(), description='명시된 직접 관계', legacy=False):
        source = title+' '+ ' '.join(entities)+' '+ ' '.join(a for a, _ in aliases)+' 명시된 합성 관계.'
        doc = self.store.register(title, title+'.txt', source.encode(), source)
        dictionary = {'entities': [{'id': name, 'name': name, 'type': kind, 'quote': source} for name, kind in entities.items()],
                      'aliases': [{'alias': alias, 'entity': name, 'quote': source} for alias, name in aliases],
                      'relations': [{'id': predicate, 'description': description,
                                     'subject_types': sorted(set(entities.values())), 'object_types': sorted(set(entities.values())), 'quote': source}
                                    for predicate in sorted({p for _, p, _ in triples} or {'likes'})]}
        if legacy:
            run = legacy_run(self.store, doc['id'])
        else:
            run = self.store.create_run(doc['id'], 'agy', 'test', 'ontology', 1, 1)
            self.store.run_status(run['id'], 'running'); self.store.finish_run(run['id'], dictionary=dictionary)
            item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
            self.store.review(item['id'], item['revision'], 'approved', 'Test', '합성 사전')
            if not triples: return doc, dictionary, []
            run = self.store.create_run(doc['id'], 'agy', 'test', 'extract', 1, 1)
        self.store.run_status(run['id'], 'running')
        facts = [{'subject': a, 'predicate': p, 'object': b, 'quote': source} for a, p, b in triples]
        self.store.finish_run(run['id'], {'title': title, 'claims': [{'heading': '합성', 'text': source, 'quote': source}]}, facts)
        items = [i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'fact']
        for item in items: self.store.review(item['id'], item['revision'], 'approved', 'Test', '합성 사실')
        return doc, dictionary, items

    def test_different_and_same_type_homonyms_keep_separate_proofs(self):
        for kind in ('음료', '이동수단', '음료'):
            self.document(f'자료{len(self.store.snapshot()["documents"])}', {'도윤': '사람', '차': kind}, [('도윤', 'likes', '차')])
        snapshot = self.store.snapshot(); meanings = meaning_candidates(snapshot, '차')
        self.assertEqual(len(meanings), 3); self.assertEqual(len({m['ref'] for m in meanings}), 3)
        self.assertEqual(sorted(m['type'] for m in meanings), ['음료', '음료', '이동수단'])
        result = query(snapshot, 'upstream', '차', 'likes')
        self.assertEqual(result['rows'], [['도윤', 'likes', '차']]*3)
        self.assertEqual(len({tuple(p['row_refs']) for p in result['proofs']}), 3)
        self.assertEqual(len({p['fact_ids'][0] for p in result['proofs']}), 3)
        self.assertEqual(query(snapshot, 'direct', '도윤', 'likes')['rows'], result['rows'])
        self.assertEqual(len(result['entities']), 6)
        edge_refs = {edge['id']: edge for edge in result['fact_refs']}
        for proof in result['proofs']:
            edge = edge_refs[proof['fact_ids'][0]]
            self.assertEqual([edge['subject_ref'], edge['predicate_ref'], edge['object_ref']], proof['row_refs'])
        for meaning in meanings:
            exact = query(snapshot, 'upstream', meaning['ref'], 'likes')
            self.assertEqual(len(exact['rows']), 1)
            self.assertEqual(exact['row_refs'][0][2], meaning['ref'])

    def test_aliases_resolve_within_pin_and_short_korean_labels_are_exact(self):
        self.document('음료자료', {'도윤': '사람', '차': '음료', '차고': '장소'}, [('도윤', 'likes', '차')], aliases=[('따뜻한 차', '차'), ('공통별칭', '차')])
        self.document('이동자료', {'도윤': '사람', '차': '이동수단'}, [('도윤', 'likes', '차')], aliases=[('자동차', '차'), ('공통별칭', '차')])
        snapshot = self.store.snapshot()
        selected = meaning_candidates(snapshot, '따뜻한 차')
        self.assertEqual(len(selected), 1); self.assertEqual(selected[0]['type'], '음료')
        self.assertEqual(len(query(snapshot, 'upstream', '따뜻한 차', 'likes')['rows']), 1)
        self.assertEqual(meaning_candidates(snapshot, '차는'), [])
        self.assertEqual(len(meaning_candidates(snapshot, '차고')), 1)
        self.assertEqual(len(meaning_candidates(snapshot, '차')), 2)
        self.assertEqual(len(meaning_candidates(snapshot, '공통별칭')), 2)
        self.assertEqual(query(snapshot, 'downstream', '공통별칭', 'likes')['status'], 'ambiguous')
        self.assertEqual(len(query(snapshot, 'upstream', '공통별칭', 'likes')['rows']), 2)

    def test_cross_document_reach_is_blocked_and_ambiguous_traversal_needs_refs(self):
        self.document('첫문서', {'시작': '노드', '공유': '노드'}, [('시작', 'connects', '공유')], description='첫 문서의 직접 연결')
        self.document('둘째문서', {'공유': '노드', '끝': '노드'}, [('공유', 'connects', '끝')], description='다른 정의의 연결')
        snapshot = self.store.snapshot()
        self.assertEqual(query(snapshot, 'path', '시작', 'connects', '끝')['rows'], [])
        ambiguous = query(snapshot, 'downstream', '공유', 'connects')
        self.assertEqual(ambiguous['status'], 'ambiguous'); self.assertEqual(ambiguous['rows'], [])
        self.assertEqual(len(ambiguous['entity_candidates']['subject']), 2)
        self.assertTrue(ambiguous['warnings']); self.assertEqual(ambiguous['fact_refs'], [])
        chosen = next(m for m in meaning_candidates(snapshot, '공유') if m['document_title'] == '둘째문서')
        explicit = query(snapshot, 'downstream', chosen['ref'], 'connects')
        self.assertEqual(explicit['rows'], [['공유', 'connects', '끝']])
        self.assertEqual(len(identity_graph(snapshot)['relations']), 2)

    def test_same_document_path_uses_refs_and_reports_intermediate_meanings(self):
        _, _, facts = self.document('경로', {'시작': '노드', '중간': '노드', '끝': '노드'}, [('시작', 'connects', '중간'), ('중간', 'connects', '끝')])
        snapshot = self.store.snapshot(); result = query(snapshot, 'path', '시작', 'connects', '끝')
        self.assertEqual(result['rows'], [['시작', 'connects', '끝']])
        self.assertEqual(set(result['proofs'][0]['fact_ids']), {f['id'] for f in facts})
        self.assertEqual({m['label'] for m in result['entities']}, {'시작', '중간', '끝'})
        edge_refs = {edge['id']: edge for edge in result['fact_refs']}
        proof = result['proofs'][0]; first, second = [edge_refs[fact_id] for fact_id in proof['fact_ids']]
        self.assertEqual(first['subject_ref'], proof['row_refs'][0])
        self.assertEqual(first['object_ref'], second['subject_ref'])
        self.assertEqual(second['object_ref'], proof['row_refs'][2])
        self.assertEqual({first['predicate_ref'], second['predicate_ref']}, {proof['row_refs'][1]})
        self.assertEqual(len(snapshot['ontology_pins']), 1)
        self.assertEqual(len(set(snapshot['fact_ontologies'].values())), 1)
        relation = result['relations'][0]['ref']
        self.assertEqual(query(snapshot, 'path', result['row_refs'][0][0], relation, result['row_refs'][0][2])['rows'], result['rows'])

    def test_current_guidance_and_historical_pin_are_separate_after_revision_and_revocation(self):
        doc, dictionary, _ = self.document('고정사전', {'도윤': '사람', '차': '음료'}, [('도윤', 'likes', '차')])
        old_snapshot = self.store.snapshot(); old_meaning = meaning_candidates(old_snapshot, '차')[0]
        self.assertTrue(old_meaning['current_approved']); self.assertTrue(old_meaning['historical_pin'])
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        revised = copy.deepcopy(dictionary); revised['entities'][1]['type'] = '수집품'
        revised['relations'][0]['object_types'] = ['사람', '수집품']; revised['relations'][0]['subject_types'] = ['사람', '수집품']
        self.store.review(item['id'], item['revision'], 'revise', 'Test', '의미 개정', revised)
        self.store.review(item['id'], item['revision']+1, 'approved', 'Test', '개정 승인')
        snapshot = self.store.snapshot(); meanings = meaning_candidates(snapshot, '차')
        self.assertEqual({m['type'] for m in meanings}, {'음료', '수집품'})
        historical = next(m for m in meanings if m['historical_pin']); current = next(m for m in meanings if m['current_approved'])
        self.assertEqual(historical['ref'], old_meaning['ref']); self.assertFalse(historical['current_approved'])
        self.assertEqual(historical['fact_count'], 1); self.assertEqual(current['fact_count'], 0)
        self.assertEqual(query(snapshot, 'upstream', historical['ref'], 'likes')['rows'], [['도윤', 'likes', '차']])
        self.assertEqual(query(snapshot, 'upstream', current['ref'], 'likes')['rows'], [])
        self.store.review(item['id'], item['revision']+2, 'rejected', 'Test', '현재 사전 반려')
        after = meaning_candidates(self.store.snapshot(), '차')
        self.assertEqual(len(after), 1); self.assertEqual(after[0]['ref'], old_meaning['ref'])
        self.assertEqual(self.store.snapshot(old_snapshot['id']), old_snapshot)

    def test_dictionary_only_refs_select_empty_graph_and_unknown_refs_fail(self):
        self.document('사전만', {'차': '음료'})
        snapshot = self.store.snapshot(); meaning = meaning_candidates(snapshot, '차')[0]
        result = query(snapshot, 'direct', meaning['ref'])
        self.assertEqual(result['status'], 'empty'); self.assertEqual(result['entities'][0]['ref'], meaning['ref'])
        self.assertEqual(result['entities'][0]['fact_count'], 0)
        for ref in ('entity:missing', 'entity:'+'0'*64, 'relation:missing'):
            with self.subTest(ref=ref), self.assertRaises(ValueError): query(snapshot, subject=ref)
        with self.assertRaises(ValueError): query(snapshot, predicate=meaning['ref'])
        with self.assertRaises(ValueError): query(snapshot, target='entity:missing')

    def test_missing_or_invalid_pins_use_document_local_legacy_identity(self):
        doc, _, _ = self.document('기존1', {'시작': '노드', '공유': '노드', '끝': '노드'}, [('시작', 'connects', '공유'), ('공유', 'connects', '끝')], legacy=True)
        self.document('기존2', {'끝': '노드', '외부': '노드'}, [('끝', 'connects', '외부')], legacy=True)
        snapshot = self.store.snapshot()
        self.assertTrue(all(m['identity_status'] == 'legacy_unresolved' for m in identity_graph(snapshot)['entities']))
        local_target = next(m['ref'] for m in meaning_candidates(snapshot, '끝') if m['document_id'] == doc['id'])
        result = query(snapshot, 'path', '시작', 'connects', local_target)
        self.assertEqual(len(result['proofs'][0]['fact_ids']), 2); self.assertTrue(result['warnings'])
        self.assertEqual(query(snapshot, 'path', '시작', 'connects', '외부')['rows'], [])
        snapshot['fact_ontologies'] = {f['id']: {'id': 'bad', 'revision': 1, 'payload': {}, 'sha256': 'wrong'} for f in snapshot['facts']}
        self.assertEqual(query(snapshot, 'path', '시작', 'connects', local_target)['rows'], result['rows'])

    def test_valid_hash_malformed_dictionaries_never_supply_pinned_meanings(self):
        self.document('구조검증', {'도윤': '사람', '차': '음료'}, [('도윤', 'likes', '차')], aliases=[('따뜻한 차', '차')])
        snapshot = self.store.snapshot()
        mutations = {
            'duplicate_entity_different_type': lambda p: p['entities'].append(p['entities'][1] | {'type': '다른타입'}),
            'nonstring_name': lambda p: p['entities'][0].update(name=None),
            'nonstring_type': lambda p: p['entities'][0].update(type=[]),
            'duplicate_relation': lambda p: p['relations'].append(copy.deepcopy(p['relations'][0])),
            'undeclared_relation_type': lambda p: p['relations'][0].update(subject_types=['미선언']),
            'dangling_alias': lambda p: p['aliases'][0].update(entity='미선언'),
            'colliding_alias': lambda p: p['aliases'][0].update(alias='도윤'),
            'duplicate_alias': lambda p: p['aliases'].append(copy.deepcopy(p['aliases'][0])),
            'nonstring_alias': lambda p: p['aliases'][0].update(alias=7),
            'nonstring_quote': lambda p: p['entities'][0].update(quote={}),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                damaged = copy.deepcopy(snapshot)
                pin = next(iter(damaged['ontology_pins'].values()))
                mutate(pin['payload']); pin['sha256'] = digest(pin['payload'])
                damaged['ontologies'][0]['payload'] = copy.deepcopy(pin['payload'])
                graph = identity_graph(damaged)
                self.assertTrue(graph['entities'])
                self.assertTrue(all(m['identity_status'] == 'legacy_unresolved' for m in graph['entities']+graph['relations']))
                self.assertFalse(any(m['current_approved'] or m['historical_pin'] for m in graph['entities']+graph['relations']))
                self.assertEqual(query(damaged, 'direct')['rows'], [['도윤', 'likes', '차']])
                self.assertTrue(query(damaged, 'direct')['warnings'])
                damaged['facts'] = []; damaged['fact_ontologies'] = {}; damaged['ontology_pins'] = {}
                self.assertEqual(identity_graph(damaged)['entities'], [])
                self.assertEqual(identity_graph(damaged)['relations'], [])
        bad_hash = copy.deepcopy(snapshot); bad_hash['ontologies'] = []
        next(iter(bad_hash['ontology_pins'].values()))['sha256'] = '0'*64
        self.assertTrue(all(m['identity_status'] == 'legacy_unresolved' for m in identity_graph(bad_hash)['entities']))
        self.assertTrue(all(m['identity_status'] == 'pinned' for m in identity_graph(snapshot)['entities']))


    def test_persisted_snapshot_without_gate_replays_identical_legacy_results(self):
        self.document('옛자료1', {'시작': '노드', '공유': '노드'}, [('시작', 'connects', '공유')], legacy=True)
        self.document('옛자료2', {'공유': '노드', '끝': '노드'}, [('공유', 'connects', '끝')], legacy=True)
        current = self.store.snapshot()
        old = {k: v for k, v in current.items() if k not in ('id', 'graph_identity_version', 'fact_ontologies', 'ontology_pins')}
        old = {'id': digest(old), **old}
        with self.store.connect(True) as db: db.execute('INSERT INTO snapshots VALUES(?,?,?)', (old['id'], canonical(old), now()))
        before = query(old, 'path', '시작', 'connects', '끝')
        self.assertEqual(before['rows'], [['시작', 'connects', '끝']])
        self.assertNotIn('row_refs', before); self.assertNotIn('identity_contract', before)
        self.assertEqual(query(self.store.snapshot(old['id']), 'path', '시작', 'connects', '끝'), before)
        self.assertEqual(query(current, 'path', '시작', 'connects', '끝')['rows'], [])


if __name__ == '__main__': unittest.main()
