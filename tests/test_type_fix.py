"""One exact type change for a relation excluded for a type mismatch: widen the relation's allowed types, or retype the entity.

Spellings are synthetic and the model is a fake. The end-to-end cases run the product extraction contract, whose chunk without facts
gets one recovery call: the fake answers in the order chunk 1, recovery, chunk 2.
"""
import copy
import json
import threading
import unittest
from urllib.error import HTTPError

import test_judged_aliases as aliases
import test_workflow as workflow
from test_extraction_checkpoint import FakeCLI
from test_workflow import approved_dictionary, extraction_response

from verinote.entity_identity import identity_graph
from verinote.extraction_coverage import read_report
from verinote.service import Service
from verinote.store import Conflict

S = aliases.S
D = copy.deepcopy(aliases.D)
D['entities'].append({'id': '다른표기', 'name': '다른표기', 'type': '조직', 'quote': '다른표기가 있다.'})
D['aliases'].append({'alias': '세운다', 'entity': '납품 계획', 'quote': '납품 계획을 세운다.'})
D['relations'].append({'id': 'owns', 'description': 'd', 'subject_types': ['조직'], 'object_types': ['산출물'], 'quote': '정산 문서도 있다.'})
MIS = {'subject': '정산 문서', 'predicate': 'produces', 'object': '끝', 'quote': '끝.'}
OWN = {'subject': '다른표기', 'predicate': 'owns', 'object': '정산 문서', 'quote': '끝.'}
KEPT = aliases.KEPT
WIDEN = dict(kind='widen', relation='produces', field='subject', entity='정산 문서', entity_type='산출물')
RETYPE = dict(kind='retype', relation='produces', field='subject', entity='정산 문서', entity_type='활동')
TYPE = '사전에 있는 개체 유형을 고르세요[.]$'


class Base(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def start(self, dictionary=D):
        self.doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, self.doc['id'], dictionary)
        return self.item()

    def item(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def change(self, **kw):
        item, doc = self.item(), self.store.document(self.doc['id'])
        a = WIDEN | {'revision': item['revision'], 'version': doc['version'], 'generation': doc['generation'], 'actor': 'R', 'reason': '원문 대조'} | kw
        return self.store.change_dictionary_type(a.get('item_id', item['id']), a['revision'], a['version'], a['generation'], a['kind'], a['relation'],
                                                 a['field'], a['entity'], a['entity_type'], a['actor'], a['reason'])

    def refused(self, text, **kw):
        revision = self.item()['revision']
        with self.assertRaisesRegex((ValueError, Conflict), text):
            self.change(**kw)
        self.assertEqual(self.item()['revision'], revision)

    def extract(self, payload, facts):
        cli = FakeCLI([extraction_response([], payload, aliases.PAGE1), {'facts': []}, extraction_response(facts, payload, aliases.PAGE2)])
        service = Service(self.store, self.root, cli)
        doc = self.store.document(self.doc['id'])
        run = service.start(self.doc['id'], 'copilot', 'm', expected_version=doc['version'], expected_generation=doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), cli

    def approve(self):
        item = self.item()
        self.store.review(item['id'], item['revision'], 'approved', 'R2', '승인')
        return self.item()['payload']

    def facts(self, run):
        return [i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'fact' and i['run_id'] == run['id']]

    def excluded(self, run):
        return [(e['subject'], e['predicate'], e['code']) for chunk in read_report(self.root, self.store, run['id'])['chunks'] for e in chunk.get('excluded_facts', [])]

    def first_run(self):
        """Run 1 under the product contract excludes MIS; every saved fact is then approved."""
        self.start()
        run, cli = self.extract(D, [MIS, OWN, KEPT])
        self.assertEqual((run['status'], run['ontology']['extraction_contract'], len(cli.calls)), ('success', 3, 3), run['detail'])
        self.assertEqual(read_report(self.root, self.store, run['id'])['excluded_type_count'], 1)
        for fact in self.facts(run):
            self.store.review(fact['id'], fact['revision'], 'approved', 'R', 'ok')
        return run


class EndToEnd(Base):
    def test_widen_then_approve_saves_the_excluded_relation_with_three_calls_under_contract_3(self):
        self.first_run()
        self.change()
        second, cli = self.extract(self.approve(), [MIS, OWN, KEPT])
        self.assertEqual((second['status'], second['ontology']['extraction_contract'], len(cli.calls)), ('success', 3, 3), second['detail'])
        self.assertEqual(read_report(self.root, self.store, second['id'])['excluded_fact_count'], 0)
        saved = {(f['payload']['subject'], f['payload']['predicate'], f['payload']['object']): f['status'] for f in self.facts(second)}
        self.assertEqual(saved[('정산 문서', 'produces', '끝')], 'pending')

    def test_retype_saves_the_relation_and_newly_excludes_one_the_old_type_fitted(self):
        first = self.first_run()
        self.change(**RETYPE)
        second, _ = self.extract(self.approve(), [MIS, OWN, KEPT])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertIn(('정산 문서', 'produces', '끝'), [(f['payload']['subject'], f['payload']['predicate'], f['payload']['object']) for f in self.facts(second)])
        self.assertEqual(self.excluded(second), [('다른표기', 'owns', 'type_mismatch')])
        kept = [f['status'] for f in self.facts(first) if f['payload']['predicate'] == 'owns']
        self.assertEqual(kept, ['approved'])
        meanings = sorted((e['type'], e['ontology_revision'], e['fact_count']) for e in identity_graph(self.store.snapshot())['entities'] if e['id'] == '정산 문서')
        self.assertEqual([(m[0], m[2]) for m in meanings], [('산출물', 1), ('활동', 0)])
        self.assertEqual(meanings[0][1], first['ontology']['revision'])
        self.assertEqual(meanings[1][1], second['ontology']['revision'])


class Change(Base):
    def test_exactly_one_field_changes_for_each_kind(self):
        old = self.start()['payload']
        self.change()
        expected = copy.deepcopy(old)
        relation = next(r for r in expected['relations'] if r['id'] == 'produces')
        relation['subject_types'] = sorted(set(relation['subject_types']) | {'산출물'})
        self.assertEqual(self.item()['payload'], expected)
        old = self.item()['payload']
        self.assertNotIn('활동', next(r for r in old['relations'] if r['id'] == 'owns')['subject_types'])
        self.change(relation='owns', field='object', entity='검수 활동', entity_type='활동')
        expected = copy.deepcopy(old)
        next(r for r in expected['relations'] if r['id'] == 'owns')['object_types'] = ['산출물', '활동']
        self.assertEqual(self.item()['payload'], expected)
        old = self.item()['payload']
        self.change(kind='retype', relation='owns', field='subject', entity='납품 계획', entity_type='조직')
        expected = copy.deepcopy(old)
        next(e for e in expected['entities'] if e['id'] == '납품 계획')['type'] = '조직'
        self.assertEqual(self.item()['payload'], expected)

    def test_approval_is_not_skipped_after_either_kind(self):
        for kind in (WIDEN, RETYPE):
            with self.subTest(kind=kind['kind']):
                self.start()
                self.change(**kind)
                doc = self.store.document(self.doc['id'])
                self.assertEqual(self.item()['status'], 'pending')
                self.assertFalse(doc['can_extract'])
                with self.assertRaises(Conflict):
                    self.store.create_run(self.doc['id'], 'copilot', 'm', 'extract', doc['version'], doc['generation'])

    def test_retype_type_must_be_an_exact_declared_type_and_check_8_names_the_reason(self):
        # The allowed-type check also refuses each of these; the exact message is what makes the declared-type check falsifiable.
        self.start()
        for value in ('활동 ', 'XYZ', '조 직', ['활동']):
            with self.subTest(value=value):
                self.refused(TYPE, **(RETYPE | {'entity_type': value}))

    def test_retype_type_outside_the_relations_allowed_types_refused(self):
        self.start()
        self.refused('이 관계의 허용 타입 중에서 고르세요[.]$', **(RETYPE | {'entity_type': '조직'}))

    def test_widen_type_must_be_an_exact_declared_type_and_check_8_names_the_reason(self):
        # The current-type equality check also refuses each of these; the exact message is what makes the declared-type check falsifiable.
        self.start()
        for value in ('산출물 ', 'XYZ', '산 출물'):
            with self.subTest(value=value):
                self.refused(TYPE, entity_type=value)

    def test_widen_uses_the_entitys_type_in_the_current_dictionary(self):
        item = self.start()
        value = copy.deepcopy(item['payload'])
        next(e for e in value['entities'] if e['id'] == '정산 문서')['type'] = '조직'
        self.store.review(item['id'], item['revision'], 'revise', 'R', '타입 변경', value)
        self.refused('현재 사전에서 이 개체의 타입이 달라졌습니다', entity_type='산출물')
        self.change(entity_type='조직')
        self.assertEqual(next(r for r in self.item()['payload']['relations'] if r['id'] == 'produces')['subject_types'], ['조직', '활동'])

    def test_last_entity_of_a_type_cannot_be_retyped(self):
        self.start()
        self.refused('^개체 다른표기: 타입 조직의 마지막 개체입니다. 관계 owns에서 이 타입을 허용하므로 바꿀 수 없습니다.',
                     **(RETYPE | {'entity': '다른표기'}))

    def test_stale_revision_is_reported_before_content_checks(self):
        stale = self.start()['revision']
        self.change()
        self.refused('^후보가 변경되었습니다', revision=stale)

    def test_refusal_matrix(self):
        item = self.start()
        cases = [
            (dict(kind='alias'), '넓히기 또는 개체 타입 바꾸기만'),
            (dict(relation='없는관계'), '^현재 사전에 없는 관계입니다'),
            (dict(relation=['produces']), '^현재 사전에 없는 관계입니다'),
            (dict(field='predicate'), '주어 또는 목적어를 고르세요'),
            (dict(entity='세운다'), '개체 ID를 고르세요'),
            (dict(entity=['정산 문서']), '개체 ID를 고르세요'),
            (dict(entity_type=['산출물']), TYPE),
            (dict(entity='검수 활동', entity_type='활동'), '^이미 허용 타입에 있습니다'),
            (RETYPE | dict(entity='검수 활동'), '^이미 허용 타입에 들어갑니다'),
            (dict(generation=9), '문서가 변경'),
            (dict(version=9), '문서가 변경'),
            (dict(reason=' '), '검토자'),
            (dict(item_id='없음'), '사전 후보를 찾을 수 없습니다'),
        ]
        for kw, text in cases:
            with self.subTest(kw=kw):
                self.refused(text, **kw)
        self.assertEqual(self.item()['revision'], item['revision'])

    def test_old_version_and_rejected_dictionary_refused(self):
        item = self.start()
        self.store.review(item['id'], item['revision'], 'rejected', 'R', '반려')
        self.refused('^현재 문서 버전의 반려되지 않은 사전만 고칠 수 있습니다')
        self.store.register('d', 'd.md', (S + ' 더').encode(), S + ' 더', self.doc['id'], self.doc['version'], self.doc['generation'])
        old = next(i for i in self.store.document(self.doc['id'], 1)['items'] if i['kind'] == 'ontology')
        doc = self.store.document(self.doc['id'])
        with self.assertRaisesRegex(Conflict, '^현재 문서 버전의 반려되지 않은 사전만 고칠 수 있습니다'):
            self.store.change_dictionary_type(old['id'], old['revision'], doc['current_version'], doc['generation'], *WIDEN.values(), 'R', 'x')

    def test_running_run_refuses(self):
        self.start()
        doc = self.store.document(self.doc['id'])
        run = self.store.create_run(self.doc['id'], 'copilot', 'm', 'extract', doc['version'], doc['generation'])
        self.store.run_status(run['id'], 'running')
        self.refused('끝나지 않아 사전을 바꿀 수 없습니다')

    def test_event_reason_starts_with_the_change_note(self):
        self.start()
        self.change(reason='원문 대조 결과')
        self.assertEqual(self.store.history(self.item()['id'])[-1]['reason'], '타입 수정(관계 허용 타입): produces subject_types + 산출물 · 원문 대조 결과')
        self.change(**(RETYPE | {'relation': 'owns', 'entity': '납품 계획', 'entity_type': '조직'}), reason='다시 대조')
        self.assertEqual(self.store.history(self.item()['id'])[-1]['reason'], '타입 수정(개체 타입): 납품 계획 산출물 → 조직 · 다시 대조')

    def test_add_path_keeps_its_own_refusal_text(self):
        item = self.start()
        self.store.review(item['id'], item['revision'], 'rejected', 'R', '반려')
        with self.assertRaisesRegex(Conflict, '^현재 문서 버전의 반려되지 않은 사전에만 추가할 수 있습니다[.]$'):
            self.store.add_dictionary_term(item['id'], item['revision'] + 1, 1, 1, 'alias', '납품계획', aliases.NEAR, 'R', 'x', entity='납품 계획')

    def test_two_connections_on_one_revision(self):
        self.start()
        results, barrier = [], threading.Barrier(2)

        def go(kw):
            barrier.wait()
            try:
                results.append(self.change(**kw)['revision'])
            except Conflict:
                results.append('conflict')
        item = self.item()
        threads = [threading.Thread(target=go, args=(dict(revision=item['revision']) | kw,)) for kw in (WIDEN, RETYPE | {'relation': 'owns', 'entity': '납품 계획', 'entity_type': '조직'})]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(map(str, results)), sorted([str(item['revision'] + 1), 'conflict']))


class HTTP(unittest.TestCase):
    setUp = aliases.HTTP.setUp
    tearDown = aliases.HTTP.tearDown
    request = aliases.HTTP.request

    def test_round_trip_and_unknown_field(self):
        doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, doc['id'], D)
        item = next(i for i in self.request(f"/api/documents/{doc['id']}")['items'] if i['kind'] == 'ontology')
        body = WIDEN | {'revision': item['revision'], 'expected_version': 1, 'expected_generation': 1, 'actor': 'R', 'reason': '원문 대조'}
        with self.assertRaises(HTTPError) as caught:
            self.request(f"/api/items/{item['id']}/dictionary-types", 'POST', body | {'extra': 1})
        error = json.loads(caught.exception.read())
        caught.exception.close()
        self.assertEqual((caught.exception.code, error['error']), (400, '알 수 없는 사전 타입 수정 필드'))
        result = self.request(f"/api/items/{item['id']}/dictionary-types", 'POST', body)
        self.assertEqual((result['status'], result['revision']), ('pending', item['revision'] + 1))


if __name__ == '__main__':
    unittest.main()
