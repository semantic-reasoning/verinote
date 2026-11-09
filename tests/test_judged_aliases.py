"""An alias a person judged, with no passage holding both spellings. Spellings are synthetic."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_workflow as workflow
from test_extraction_checkpoint import FakeCLI
from test_workflow import approved_dictionary, extraction_response

from verinote.assistant import Assistant
from verinote.dictionary_context import encode
from verinote.entity_identity import _pin, identity_graph
from verinote.extraction_coverage import read_report
from verinote.ontology import (
    JUDGED_FIELDS,
    dictionary_context,
    merge_dictionary,
    validate_dictionary,
)
from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, digest

FAR = '가' * 7000
NEAR = '검수 활동은 납품계획을 만든다.'
S = ('납품 계획을 세운다. 정산 문서도 있다. 다른표기가 있다. 또다른표기도 있다. ' + FAR).ljust(8000, '나') + NEAR + ' 끝.'
JUDGE = '이 항목의 표기를 같은 대상으로 판단함'
D = {'entities': [{'id': '납품 계획', 'name': '납품 계획', 'type': '산출물', 'quote': '납품 계획을 세운다.'},
                  {'id': '정산 문서', 'name': '정산 문서', 'type': '산출물', 'quote': '정산 문서도 있다.'},
                  {'id': '검수 활동', 'name': '검수 활동', 'type': '활동', 'quote': '검수 활동은'},
                  {'id': '끝', 'name': '끝', 'type': '산출물', 'quote': '끝.'}],
     'aliases': [], 'relations': [{'id': 'produces', 'description': 'd', 'subject_types': ['활동'], 'object_types': ['산출물'], 'quote': '만든다'}]}
ROW = {'alias': '납품계획', 'entity': '납품 계획', 'quote': NEAR, 'judged_alias': '납품계획', 'judged_entity': '납품 계획', 'judged_name': '납품 계획'}
KEPT = {'subject': '검수 활동', 'predicate': 'produces', 'object': '끝', 'quote': '끝.'}
FACT = {'subject': '검수 활동', 'predicate': 'produces', 'object': '납품계획', 'quote': '납품계획을 만든다'}
PAGE1 = {'title': 't', 'claims': [{'heading': 'h', 'text': 'x', 'quote': '정산 문서도 있다.'}]}
PAGE2 = {'title': 't', 'claims': [{'heading': 'h', 'text': 'x', 'quote': '끝.'}]}
STALE = '다시 판단해야 하는 별칭'


def judged(change=None, rows=None):
    value = copy.deepcopy(D)
    value['aliases'] = copy.deepcopy(rows if rows is not None else [ROW])
    if change:
        change(value)
    return value


class Rule(unittest.TestCase):
    def refused(self, change, text=STALE):
        with self.assertRaisesRegex(ValueError, text):
            validate_dictionary(judged(change), S)

    def test_valid_row_keeps_its_keys(self):
        self.assertEqual(validate_dictionary(judged(), S)['aliases'], [ROW])

    def test_rename_with_entity_quote_unchanged_refused_by_the_alias_rule(self):
        self.refused(lambda v: v['entities'][0].update(name='계획'))

    def test_rename_to_a_string_inside_the_alias_quote_refused(self):
        self.refused(lambda v: v['entities'][0].update(name='납품계획', quote='납품계획'))

    def test_retarget_to_a_different_name_refused(self):
        self.refused(lambda v: v['aliases'][0].update(entity='정산 문서'))

    def test_retarget_to_a_same_named_entity_refused(self):
        def change(v):
            v['entities'][1].update(name='납품 계획', quote='납품 계획을 세운다.')
            v['aliases'][0].update(entity='정산 문서')
        self.refused(change)

    def test_spelling_edit_refused(self):
        self.refused(lambda v: v['aliases'][0].update(alias='다른표기', quote='다른표기가 있다.'))

    def test_spelling_swap_between_judged_rows_refused(self):
        other = ROW | {'alias': '다른표기', 'quote': '다른표기가 있다.', 'judged_alias': '다른표기'}
        value = judged(rows=[ROW | {'alias': '다른표기', 'quote': '다른표기가 있다.'}, other | {'alias': '납품계획', 'quote': NEAR}])
        with self.assertRaisesRegex(ValueError, STALE):
            validate_dictionary(value, S)

    def test_all_keys_updated_is_a_new_judgement(self):
        def rename(v):
            v['entities'][0]['name'] = '계획'
            v['aliases'][0]['judged_name'] = '계획'
        self.assertEqual(validate_dictionary(judged(rename), S)['aliases'][0]['judged_name'], '계획')
        def retarget(v):
            v['aliases'][0].update(entity='정산 문서', judged_entity='정산 문서', judged_name='정산 문서')
        self.assertEqual(validate_dictionary(judged(retarget), S)['aliases'][0]['entity'], '정산 문서')
        def respell(v):
            v['aliases'][0].update(alias='다른표기', quote='다른표기가 있다.', judged_alias='다른표기')
        self.assertEqual(validate_dictionary(judged(respell), S)['aliases'][0]['alias'], '다른표기')

    def test_each_key_is_required(self):
        for field in JUDGED_FIELDS:
            self.refused(lambda v, f=field: v['aliases'][0].pop(f))

    def test_non_string_key_refused(self):
        self.refused(lambda v: v['aliases'][0].update(judged_name=['납품 계획']))

    def test_alias_outside_its_own_quote_refused(self):
        self.refused(lambda v: v['aliases'][0].update(quote='끝.'), '별칭 인용에는 별칭이 있어야')

    def test_far_alias_without_keys_keeps_the_existing_message(self):
        def change(v):
            for field in JUDGED_FIELDS:
                del v['aliases'][0][field]
        self.refused(change, '별칭 인용에는 별칭과 개체 name이 함께')

    def test_judged_row_stays_judged_when_the_name_is_in_the_quote(self):
        row = ROW | {'quote': '납품 계획을 세운다.', 'alias': '세운다', 'judged_alias': '세운다'}
        self.assertEqual(validate_dictionary(judged(rows=[row]), S)['aliases'], [row])

    def test_co_located_plain_row_is_unchanged(self):
        row = {'alias': '세운다', 'entity': '납품 계획', 'quote': '납품 계획을 세운다.'}
        result = validate_dictionary(judged(rows=[row]), S)
        self.assertEqual(result['aliases'], [row])
        self.assertEqual(digest(result), digest(judged(rows=[row])))

    def test_type_change_is_not_a_new_judgement(self):
        self.assertEqual(validate_dictionary(judged(lambda v: v['entities'][0].update(type='문서')), S)['aliases'], [ROW])

    def test_error_names_every_stale_alias_at_once(self):
        rows = [ROW, ROW | {'alias': '끝', 'quote': '끝.', 'judged_alias': '끝'}]
        with self.assertRaisesRegex(ValueError, '별칭 2개: 납품계획, 끝'):
            validate_dictionary(judged(lambda v: v['entities'][0].update(name='계획'), rows), S)

    def test_error_names_at_most_five_and_counts_the_rest(self):
        rows = [ROW | {'alias': f'x{n}', 'judged_alias': f'x{n}'} for n in range(1, 8)]
        with self.assertRaisesRegex(ValueError, '별칭 7개: x1, x2, x3, x4, x5 외 2개[.]') as caught:
            validate_dictionary(judged(lambda v: v['entities'][0].update(name='계획'), rows), S)
        self.assertNotIn('x6', str(caught.exception))


class Consumers(unittest.TestCase):
    def pin(self, value):
        return {'id': 'x', 'revision': 1, 'payload': value, 'sha256': digest(value)}

    def test_pin_accepts_a_judged_row(self):
        self.assertIsNotNone(_pin(self.pin(validate_dictionary(judged(), S))))

    def test_pin_rejects_a_half_judged_row(self):
        for field in JUDGED_FIELDS:
            value = judged(lambda v, f=field: v['aliases'][0].pop(f))
            self.assertIsNone(_pin(self.pin(value)), field)

    def test_context_omits_the_keys_and_the_wire_form_encodes(self):
        context = dictionary_context(validate_dictionary(judged(), S))
        self.assertEqual(context['aliases'], [{'alias': '납품계획', 'entity': '납품 계획'}])
        encode(context)

    def test_merge_refuses_the_keys(self):
        with self.assertRaisesRegex(ValueError, '병합'):
            merge_dictionary({'entities': [], 'aliases': [], 'relations': []}, judged(), S, S)


class Flow(unittest.TestCase):
    def setUp(self):
        workflow.Workflow.setUp(self)
        # These historical fixtures assert contract-2 call counts and persistence semantics.
        contract = patch('verinote.store.EXTRACTION_CONTRACT_VERSION', 2)
        contract.start(); self.addCleanup(contract.stop)
    tearDown = workflow.Workflow.tearDown

    def start(self):
        self.doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, self.doc['id'], D)
        return self.item()

    def item(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def add(self, kind='alias', spelling='납품계획', quote=NEAR, reason='같은 대상으로 판단', **kw):
        item, doc = self.item(), self.store.document(self.doc['id'])
        args = {'entity': '납품 계획'} if kind == 'alias' else {'entity_type': '활동'}
        return self.store.add_dictionary_term(kw.pop('item_id', item['id']), kw.pop('revision', item['revision']), kw.pop('version', doc['version']),
                                              kw.pop('generation', doc['generation']), kind, spelling, quote, kw.pop('actor', 'R'), reason, **(args | kw))

    def extract(self, outputs):
        cli = FakeCLI(outputs)
        service = Service(self.store, self.root, cli)
        doc = self.store.document(self.doc['id'])
        run = service.start(self.doc['id'], 'copilot', 'm', expected_version=doc['version'], expected_generation=doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), cli

    def responses(self, dictionary, facts):
        return [extraction_response([], dictionary, PAGE1), extraction_response(facts, dictionary, PAGE2)]

    def approve(self):
        item = self.item()
        self.store.review(item['id'], item['revision'], 'approved', 'R2', '승인')
        return self.item()['payload']

    def test_condition_2_alias_with_a_passage_lacking_the_name(self):
        self.start()
        first, _ = self.extract(self.responses(D, [FACT, KEPT]))
        self.assertEqual((first['status'], read_report(self.root, self.store, first['id'])['excluded_fact_count']), ('success', 1))
        self.add()
        self.assertEqual(self.item()['payload']['aliases'][0], ROW)
        payload = self.approve()
        second, _ = self.extract(self.responses(payload, [FACT, KEPT]))
        self.assertEqual(read_report(self.root, self.store, second['id'])['excluded_fact_count'], 0)
        stored = [i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'fact' and i['run_id'] == second['id'] and i['payload']['object'] == '납품 계획']
        self.assertEqual(len(stored), 1)
        self.store.review(stored[0]['id'], stored[0]['revision'], 'approved', 'R', 'ok')
        entity = next(e for e in identity_graph(self.store.snapshot())['entities'] if e['id'] == '납품 계획' and e['fact_count'])
        self.assertEqual((entity['aliases'], entity['identity_status']), (['납품계획'], 'pinned'))

    def test_condition_2_new_entity(self):
        self.start()
        self.add('entity', '다른표기', '다른표기가 있다.', entity_type='산출물')
        payload = self.approve()
        run, _ = self.extract(self.responses(payload, [FACT | {'object': '다른표기', 'quote': '끝.'}]))
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertTrue(any(i['kind'] == 'fact' and i['payload']['object'] == '다른표기' for i in self.store.document(self.doc['id'])['items']))

    def test_new_entity_for_a_spelling_absent_from_the_source_uses_a_passage_of_its_name(self):
        self.start()
        spelling = '정산 문서:세부 항목'
        self.assertEqual(self.store.spelling_passages(self.doc['id'], 1, spelling), ([], 0))
        with self.assertRaisesRegex(ValueError, '개체 name은 해당 원문 인용에 있어야'):
            self.add('entity', spelling, '끝.', entity_type='산출물')
        passages, total = self.store.spelling_passages(self.doc['id'], 1, '정산 문서')
        self.assertEqual(total, 1)
        self.add('entity', spelling, passages[0]['quote'], name='정산 문서', entity_type='산출물')
        added = self.item()['payload']['entities'][-1]
        self.assertEqual((added['id'], added['name']), (spelling, '정산 문서'))

    def test_alias_with_an_unfitting_target_becomes_a_type_mismatch(self):
        self.start()
        self.add(entity='검수 활동')
        payload = self.approve()
        run, _ = self.extract(self.responses(payload, [FACT, KEPT]))
        rows = read_report(self.root, self.store, run['id'])['chunks'][1]['excluded_facts']
        self.assertEqual([r['code'] for r in rows], ['type_mismatch'])

    def test_condition_3_spacing_variant_stays_excluded_until_a_person_adds_it(self):
        self.start()
        run, _ = self.extract(self.responses(D, [FACT, KEPT]))
        rows = read_report(self.root, self.store, run['id'])['chunks'][1]['excluded_facts']
        self.assertEqual([(r['code'], r['object']) for r in rows], [('unknown_entity', '납품계획')])

    def test_approval_is_not_skipped(self):
        self.start()
        self.add()
        doc = self.store.document(self.doc['id'])
        self.assertEqual(self.item()['status'], 'pending')
        self.assertFalse(doc['can_extract'])
        with self.assertRaises(Conflict):
            self.store.create_run(self.doc['id'], 'copilot', 'm', 'extract', doc['version'], doc['generation'])

    def test_refusals(self):
        self.start()
        item = self.item()
        cases = [(dict(revision=item['revision'] + 1), '후보가 변경'), (dict(generation=9), '문서가 변경'), (dict(version=9), '문서가 변경'),
                 (dict(quote='없는 인용'), '원문에 없는'), (dict(quote=7), 'quote:'), (dict(entity=['납품 계획']), '기존 개체를 고르세요'), (dict(spelling='납품 계획'), '이미 사전에'), (dict(entity='없음'), '기존 개체를 고르세요'),
                 (dict(spelling='entity:납품'), '조회 참조'), (dict(reason=' '), '검토자')]
        for kw, text in cases:
            with self.assertRaisesRegex((ValueError, Conflict), text):
                self.add(**kw)
        base = {'spelling': '다른표기', 'quote': '다른표기가 있다.'}
        for kw, text in [(dict(entity_type='없는 유형'), '개체 유형을 고르세요'), (dict(entity_type=['활동']), '개체 유형을 고르세요'), (dict(spelling='끝\n끝'), '줄바꿈'), (dict(spelling='relation:끝'), '조회 참조')]:
            with self.assertRaisesRegex(ValueError, text):
                self.add('entity', **(base | kw))
        self.assertEqual(self.item()['revision'], item['revision'])

    def test_rejected_dictionary_refused(self):
        item = self.start()
        self.store.review(item['id'], item['revision'], 'rejected', 'R', '반려')
        with self.assertRaises(Conflict):
            self.add()

    def test_old_version_refused(self):
        item = self.start()
        self.store.register('d', 'd.md', (S + ' 더').encode(), S + ' 더', self.doc['id'], self.doc['version'], self.doc['generation'])
        doc = self.store.document(self.doc['id'])
        with self.assertRaises(Conflict):
            self.store.add_dictionary_term(item['id'], item['revision'], doc['current_version'], doc['generation'], 'alias', '납품계획', NEAR, 'R', 'x', entity='납품 계획')

    def test_running_run_refuses_and_still_publishes(self):
        self.start()
        doc = self.store.document(self.doc['id'])
        run = self.store.create_run(self.doc['id'], 'copilot', 'm', 'extract', doc['version'], doc['generation'])
        self.store.run_status(run['id'], 'running')
        with self.assertRaisesRegex(Conflict, '끝나지 않아 사전을 바꿀 수 없습니다'):
            self.add()
        self.store.finish_run(run['id'], page=PAGE2, facts=[])
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_two_connections_on_one_revision(self):
        self.start()
        results, barrier = [], threading.Barrier(2)
        item, doc = self.item(), self.store.document(self.doc['id'])

        def go(spelling, quote):
            barrier.wait()
            try:
                results.append(self.store.add_dictionary_term(item['id'], item['revision'], doc['version'], doc['generation'], 'alias', spelling, quote, 'R', 'x', entity='납품 계획')['revision'])
            except Conflict:
                results.append('conflict')
        threads = [threading.Thread(target=go, args=a) for a in (('납품계획', NEAR), ('다른표기', '다른표기가 있다.'))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(map(str, results)), sorted([str(item['revision'] + 1), 'conflict']))
        self.assertEqual(len(self.item()['payload']['aliases']), 1)

    def test_co_located_passage_writes_a_plain_row(self):
        self.start()
        self.add(spelling='세운다', quote='납품 계획을 세운다.')
        self.assertEqual(self.item()['payload']['aliases'], [{'alias': '세운다', 'entity': '납품 계획', 'quote': '납품 계획을 세운다.'}])

    def test_event_records_the_judgement_and_the_payload_does_not(self):
        self.start()
        long_reason = JUDGE + '.' * (1000 - len(JUDGE))
        self.add(reason=long_reason, actor='검토자A')
        event = self.store.history(self.item()['id'])[-1]
        self.assertEqual((event['action'], event['actor']), ('revise', '검토자A'))
        self.assertTrue(event['reason'].startswith('사전 추가(별칭): 납품계획 → 납품 계획 · '))
        self.assertTrue(event['reason'].endswith(long_reason))
        self.assertNotIn(JUDGE, json.dumps(self.item()['payload'], ensure_ascii=False))
        self.assertNotIn('검토자A', json.dumps(self.item()['payload'], ensure_ascii=False))

    def test_alias_judgements_follow_the_event_that_set_the_keys(self):
        self.start()
        self.add(actor='검토자A')
        item = self.item()
        self.assertEqual(self.store.alias_judgements(item['id'], item['payload'])['납품계획']['actor'], '검토자A')
        self.approve()
        item = self.item()
        self.assertEqual(self.store.alias_judgements(item['id'], item['payload'])['납품계획']['actor'], '검토자A')
        value = copy.deepcopy(item['payload'])
        value['entities'][0]['name'] = '계획'
        value['aliases'][0]['judged_name'] = '계획'
        self.store.review(item['id'], item['revision'], 'revise', '검토자B', '다시 판단', payload=value)
        item = self.item()
        self.assertEqual(self.store.alias_judgements(item['id'], item['payload'])['납품계획']['actor'], '검토자B')
        self.assertEqual(self.store.alias_judgements('없음', {'aliases': []}), {})

    def test_first_call_prompts_omit_keys_and_repair_prompt_shows_keys_but_no_judgement(self):
        self.start()
        self.add(actor='검토자A', reason=JUDGE)
        payload = self.approve()
        bad = FACT | {'quote': '없는 인용'}
        run, cli = self.extract(self.responses(payload, [bad, KEPT]) + [{'repairs': [{'path': 'facts.0.quote', 'quote': '납품계획을 만든다'}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        first, repair = json.dumps(cli.calls[1]['payload'], ensure_ascii=False), json.dumps(cli.calls[2]['payload'], ensure_ascii=False)
        self.assertNotIn('judged_name', first)
        self.assertIn('judged_name', repair)
        for text in (first, repair):
            self.assertNotIn(JUDGE, text)
            self.assertNotIn('검토자A', text)

    def test_question_search_does_not_return_or_match_on_the_keys(self):
        self.start()
        self.add()
        self.approve()
        snapshot = self.store.snapshot()
        self.assertIn('judged_name', json.dumps(snapshot['ontologies'], ensure_ascii=False))
        searcher = Assistant.__new__(Assistant)
        searcher.ontologies, searcher.pages = snapshot['ontologies'], {}
        searcher.documents = {self.doc['id']: {'title': 'z', 'filename': 'z.md'}}
        found, _, _ = searcher.knowledge_matches('납품계획', [self.doc['id']])
        guidance = [m['guidance'] for m in found if 'guidance' in m and m['guidance'].get('alias') == '납품계획']
        self.assertEqual(guidance, [{'alias': '납품계획', 'entity': '납품 계획'}])
        self.assertEqual(searcher.knowledge_matches('judged_name', [self.doc['id']])[0], [])


class Passages(unittest.TestCase):
    def setUp(self):
        workflow.Workflow.setUp(self)
        # These historical fixtures assert contract-2 call counts and persistence semantics.
        contract = patch('verinote.store.EXTRACTION_CONTRACT_VERSION', 2)
        contract.start(); self.addCleanup(contract.stop)
    tearDown = workflow.Workflow.tearDown

    def test_exact_occurrences_with_overlaps_and_a_true_total(self):
        source = '가가가 ' + '납품계획 ' * 25 + '납품 계획'
        doc = self.store.register('p', 'p.md', source.encode(), source)
        passages, total = self.store.spelling_passages(doc['id'], 1, '가가')
        self.assertEqual((total, [p['offset'] for p in passages]), (2, [0, 1]))
        passages, total = self.store.spelling_passages(doc['id'], 1, '납품계획')
        self.assertEqual((total, len(passages)), (25, 20))
        self.assertTrue(all('납품계획' in p['quote'] and p['quote'] in source for p in passages))

    def test_no_whitespace_tolerance(self):
        source = '납품 계획 하나. 납품  계획 둘.'
        doc = self.store.register('p', 'p.md', source.encode(), source)
        self.assertEqual(self.store.spelling_passages(doc['id'], 1, '납품계획'), ([], 0))
        self.assertEqual(self.store.spelling_passages(doc['id'], 1, '납품 계획')[1], 1)

    def test_blank_or_long_text_refused(self):
        doc = self.store.register('p', 'p.md', S.encode(), S)
        for value in ('', ' ', 'x' * 201, None):
            with self.assertRaises(ValueError):
                self.store.spelling_passages(doc['id'], 1, value)


class HTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = workflow.Store(root / 'db')
        self.root = root
        self.service = Service(self.store, root)
        self.server = Server(('127.0.0.1', 0), self.store, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.pool.shutdown()
        self.temp.cleanup()

    def request(self, path, method='GET', data=None):
        req = Request(self.url + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
        with urlopen(req) as response:
            return json.loads(response.read())

    def test_round_trip_through_the_routes(self):
        doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, doc['id'], D)
        found = self.request(f"/api/documents/{doc['id']}/passages?version=1&text=" + '%EB%82%A9%ED%92%88%EA%B3%84%ED%9A%8D')
        self.assertEqual(found['total'], 1)
        item = next(i for i in self.request(f"/api/documents/{doc['id']}")['items'] if i['kind'] == 'ontology')
        self.assertEqual(item['alias_judgements'], {})
        result = self.request(f"/api/items/{item['id']}/dictionary-terms", 'POST', {
            'revision': item['revision'], 'expected_version': 1, 'expected_generation': 1, 'kind': 'alias', 'spelling': '납품계획',
            'quote': found['passages'][0]['quote'], 'actor': 'R', 'reason': '같은 대상', 'entity': '납품 계획'})
        self.assertEqual(result['status'], 'pending')
        item = next(i for i in self.request(f"/api/documents/{doc['id']}")['items'] if i['kind'] == 'ontology')
        self.assertEqual(item['alias_judgements']['납품계획']['actor'], 'R')

    def test_unknown_field_and_missing_text_are_client_errors(self):
        doc = self.store.register('d', 'd.md', S.encode(), S)
        approved_dictionary(self.store, doc['id'], D)
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        valid = {'revision': item['revision'], 'expected_version': 1, 'expected_generation': 1, 'kind': 'alias', 'spelling': '납품계획',
                 'quote': NEAR, 'actor': 'R', 'reason': 'x', 'entity': '납품 계획'}
        for path, method, data, text in ((f"/api/items/{item['id']}/dictionary-terms", 'POST', valid | {'extra': 1}, '알 수 없는 사전 추가 필드'),
                                         (f"/api/documents/{doc['id']}/passages?version=1&text=", 'GET', None, 'version과 text가 필요합니다.')):
            with self.assertRaises(HTTPError) as caught:
                self.request(path, method, data)
            body = json.loads(caught.exception.read())
            caught.exception.close()
            self.assertEqual((caught.exception.code, body['error']), (400, text))
        self.assertEqual(self.store.document(doc['id'])['items'][0]['revision'], item['revision'])
