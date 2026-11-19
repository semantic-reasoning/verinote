"""Restoring a quote-excluded wiki claim from a reviewer-chosen source span: zero calls, a new pending page, once per origin."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_quote_exclusions as base
import test_workflow as workflow
from test_quote_exclusions import CLAIM1, CLAIM2, bad, reply, resp
from test_workflow import FACTS

from verinote.service import Service
from verinote.store import Conflict


class Restoration(unittest.TestCase):
    def setUp(self):
        workflow.Workflow.setUp(self)
        if self.CONTRACT == 2:
            contract = patch('verinote.store.EXTRACTION_CONTRACT_VERSION', 2)
            contract.start(); self.addCleanup(contract.stop)

    CONTRACT = 3
    tearDown, document, run_it, report = (workflow.Workflow.tearDown, base.Exclusions.document, base.Exclusions.run_it, base.Exclusions.report)
    render = base.Exclusions.render

    def setup_run(self):
        doc = self.document()
        run, calls = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.service = Service(self.store, self.root, None)
        return doc, run, calls

    def body(self, **change):
        return {'chunk': 1, 'path': 'claims.0.quote', 'quote': CLAIM1['quote'], 'actor': 'reviewer', 'reason': '원문 확인'} | change

    def test_restores_a_new_pending_page_without_touching_existing_items_or_calling_a_model(self):
        doc, run, calls = self.setup_run()
        before = self.store.document(doc['id'])['items']
        result = self.service.restore_claim(run['id'], self.body())
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(len(calls), 2)
        after = self.store.document(doc['id'])['items']
        self.assertEqual(len(after), len(before) + 1)
        for item in before:
            self.assertIn(item, after)
        added = next(i for i in after if i['id'] == result['item_id'])
        self.assertEqual((added['status'], added['kind']), ('pending', 'page'))
        self.assertEqual(added['payload']['claims'], [{'heading': CLAIM1['heading'], 'text': CLAIM1['text'], 'quote': CLAIM1['quote']}])
        self.assertEqual([r['origin'] for r in self.store.restorations(run['id'])], [result['origin']])
        self.assertEqual(self.report(run)['excluded_claim_count'], 1)

    def test_same_origin_is_restored_once_even_when_submitted_twice_at_once(self):
        _, run, _ = self.setup_run()
        outcomes = []
        def submit():
            try:
                outcomes.append(self.service.restore_claim(run['id'], self.body()))
            except Conflict as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(isinstance(o, dict) for o in outcomes), 1, outcomes)
        self.assertEqual(len(self.store.restorations(run['id'])), 1)

    def test_refuses_unknown_origin_missing_or_ambiguous_span_and_a_claim_already_a_candidate(self):
        _, run, _ = self.setup_run()
        for change, message in ((dict(path='claims.1.quote'), '찾을 수 없습니다'), (dict(chunk=2), '청크'), (dict(quote='원문에 없는 문구'), '원문에 없는'),
                                (dict(quote=' '), 'quote')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.service.restore_claim(run['id'], self.body(**change))
        self.assertEqual(self.store.restorations(run['id']), [])

    def test_refuses_a_possibly_truncated_claim_and_a_changed_dictionary(self):
        _, run, _ = self.setup_run()
        with self.assertRaisesRegex(ValueError, '필요'):
            self.service.restore_claim(run['id'], {'chunk': 1})
        with self.store.connect(True) as db:
            db.execute("UPDATE items SET status='rejected' WHERE kind='ontology'")
        with self.assertRaises(Conflict):
            self.service.restore_claim(run['id'], self.body())

    def test_run_detail_lists_the_restoration_apart_from_the_historical_report(self):
        _, run, _ = self.setup_run()
        self.service.restore_claim(run['id'], self.body())
        detail = self.service.with_usage('runs', self.store.run(run['id']))
        self.assertEqual(len(detail['coverage']['restorations']), 1)
        self.assertEqual(detail['coverage']['excluded_claim_count'], 1)
        json.dumps(detail)


    def test_run_detail_offers_the_form_until_restored_then_names_the_new_candidate(self):
        _, run, _ = self.setup_run()
        offered = self.render(self.service.with_usage('runs', self.store.run(run['id']))['coverage'])
        self.assertIn('data-restore-claim', offered)
        self.assertIn('호출 0회', offered)
        result = self.service.restore_claim(run['id'], self.body())
        done = self.render(self.service.with_usage('runs', self.store.run(run['id']))['coverage'])
        self.assertNotIn('data-restore-claim', done)
        self.assertIn(result['item_id'], done)

    def test_overlapping_occurrences_make_a_span_ambiguous(self):
        doc, run, _ = self.setup_run()
        with self.store.connect(True) as db:
            db.execute("UPDATE versions SET text=text||'\nxaaay' WHERE document_id=?", (doc['id'],))
        with self.assertRaisesRegex(ValueError, '한 위치'):
            self.store.restore_claim(run['id'], 1, 'claims.0.quote', '가', '주문', 'aa', 'reviewer', '확인')
        self.assertEqual(self.store.restorations(run['id']), [])

    def test_refuses_a_claim_whose_full_text_is_not_visible_in_the_report(self):
        doc = self.document()
        long_claim = CLAIM1 | {'text': '긴 본문 ' * 100}
        run, _ = self.run_it(doc, [resp([bad(long_claim), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertEqual((run['status'], run['ontology']['extraction_contract']), ('success', 3), run['detail'])
        self.service = Service(self.store, self.root, None)
        with self.assertRaisesRegex(ValueError, '검증할 수 없어'):
            self.service.restore_claim(run['id'], self.body())
        self.assertEqual(self.store.restorations(run['id']), [])

    def test_refuses_when_the_saved_original_response_disagrees_with_the_report(self):
        _, run, _ = self.setup_run()
        path = Path(self.root) / 'runs' / run['id'] / 'extraction-checkpoint.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['outputs'][0]['loss_audit']['initial']['claims'][0]['text'] = '다른 본문'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '검증할 수 없어'):
            self.service.restore_claim(run['id'], self.body())
        self.assertEqual(self.store.restorations(run['id']), [])


class ContractTwo(unittest.TestCase):
    CONTRACT = 2
    setUp, tearDown, document, run_it, setup_run, body = (Restoration.setUp, Restoration.tearDown, Restoration.document,
                                                          Restoration.run_it, Restoration.setup_run, Restoration.body)

    def test_refuses_a_run_without_a_saved_original_response(self):
        _, run, _ = self.setup_run()
        with self.assertRaisesRegex(ValueError, '계약 v3'):
            self.service.restore_claim(run['id'], self.body())

if __name__ == '__main__':
    unittest.main()
