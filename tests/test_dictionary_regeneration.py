"""Rejected dictionary regeneration preserves history and isolates retries."""
import copy
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_ontology import wire_dictionary
from test_workflow import DICTIONARY, FACTS, PAGE, SOURCE, approved_dictionary

from verinote.service import Service
from verinote.store import Conflict, Store, canonical


class Regeneration(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def dictionary(self):
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind'] == 'ontology')

    def rejected(self):
        item = approved_dictionary(self.store, self.doc['id'])
        self.store.review(item['id'], item['revision'], 'rejected', 'Reviewer', '초안 다시 생성 필요')
        return self.dictionary()

    def start(self, item):
        doc = self.store.document(self.doc['id'])
        return self.store.create_run(doc['id'], 'agy', '', 'ontology', doc['version'], doc['generation'],
                                     regenerate={'id': item['id'], 'revision': item['revision']})

    def test_success_replaces_single_item_pending_and_preserves_provenance(self):
        item = self.rejected(); old_history = self.store.history(item['id'])
        run = self.start(item)
        self.assertEqual(self.dictionary(), item)
        self.store.run_status(run['id'], 'running')
        changed = copy.deepcopy(DICTIONARY); changed['relations'][0]['description'] += ' 새 설명'
        self.store.finish_run(run['id'], dictionary=changed)
        current = self.dictionary()
        self.assertEqual((current['id'], current['revision'], current['status'], current['run_id']),
                         (item['id'], item['revision'] + 1, 'pending', run['id']))
        self.assertIsNone(current['dictionary'])
        history = self.store.history(item['id'])
        self.assertEqual(history[:-1], old_history)
        self.assertEqual(history[-1]['payload'], changed)
        self.assertIn(item['run_id'], history[-1]['reason']); self.assertIn(run['id'], history[-1]['reason'])
        self.assertEqual(len(self.store.document(self.doc['id'])['items']), 1)
        self.assertFalse(self.store.document(self.doc['id'])['can_extract'])
        with self.assertRaises(Conflict): self.start(item)

    def test_only_exact_rejected_target_and_well_formed_request_allowed(self):
        item = self.rejected()
        for bad in ({'id': item['id'], 'revision': True}, {}, [], {'id': '', 'revision': 1},
                    {'id': item['id'], 'revision': 1, 'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1, regenerate=bad)
        for target in ({'id': 'other', 'revision': item['revision']}, {'id': item['id'], 'revision': 1}):
            with self.assertRaises(Conflict):
                self.store.create_run(self.doc['id'], 'agy', '', 'ontology', 1, 1, regenerate=target)
        for status in ('pending', 'held', 'approved'):
            current = self.dictionary()
            self.store.review(current['id'], current['revision'], status, 'Reviewer', '상태 변경')
            with self.assertRaises(Conflict): self.start(self.dictionary())
        with self.assertRaises(ValueError):
            self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1, regenerate={'id': item['id'], 'revision': 3})

    def test_failed_validation_or_transaction_keeps_previous_dictionary(self):
        item = self.rejected(); history = self.store.history(item['id']); run = self.start(item)
        self.store.run_status(run['id'], 'running')
        with self.assertRaises(ValueError): self.store.finish_run(run['id'], dictionary={})
        with self.store.connect(True) as db:
            db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.finish_run(run['id'], dictionary=DICTIONARY)
        self.assertEqual(self.dictionary(), item); self.assertEqual(self.store.history(item['id']), history)
        self.assertEqual(self.store.run(run['id'])['status'], 'running')

    def test_concurrent_change_or_new_source_cannot_be_overwritten(self):
        item = self.rejected(); run = self.start(item)
        with self.assertRaises(Conflict): self.start(item)
        self.store.run_status(run['id'], 'running')
        self.store.review(item['id'], item['revision'], 'pending', 'Reviewer', '수동 검토')
        current = self.dictionary()
        with self.assertRaises(Conflict): self.store.finish_run(run['id'], dictionary=DICTIONARY)
        self.assertEqual(self.dictionary(), current)
        self.store.run_status(run['id'], 'error')
        self.store.review(current['id'], current['revision'], 'rejected', 'Reviewer', '다시 생성')
        run = self.start(self.dictionary()); self.store.run_status(run['id'], 'running')
        self.store.register('new', 'new.md', SOURCE.encode(), SOURCE, self.doc['id'], expected_version=1, expected_generation=1)
        with self.assertRaises(Conflict): self.store.finish_run(run['id'], dictionary=DICTIONARY)
        self.assertEqual(self.store.document(self.doc['id'], 1)['items'][0]['status'], 'rejected')

    def test_old_approved_results_and_pins_remain_unchanged(self):
        item = approved_dictionary(self.store, self.doc['id'])
        run = self.store.create_run(self.doc['id'], 'agy', '', 'extract', 1, 1)
        self.store.run_status(run['id'], 'running'); self.store.finish_run(run['id'], PAGE, FACTS)
        for row in self.store.document(self.doc['id'])['items']:
            if row['kind'] != 'ontology': self.store.review(row['id'], 1, 'approved', 'Reviewer', '확인')
        before = self.store.document(self.doc['id'])['items']; pin = self.store.run(run['id'])['ontology']
        self.store.review(item['id'], item['revision'], 'rejected', 'Reviewer', '재생성')
        job = self.start(self.dictionary()); self.store.run_status(job['id'], 'running')
        self.store.finish_run(job['id'], dictionary=DICTIONARY)
        after = self.store.document(self.doc['id'])['items']
        self.assertEqual([i for i in before if i['kind'] != 'ontology'], [i for i in after if i['kind'] != 'ontology'])
        self.assertEqual(self.store.run(run['id'])['ontology'], pin)

    def test_retry_query_filters_target_before_limit_and_survives_restart(self):
        item = self.rejected(); valid = self.start(item); self.store.run_status(valid['id'], 'error')
        for index in range(12):
            with self.store.connect(True) as db:
                db.execute("INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage,ontology) VALUES(?,?,?,?,?,'error','','9999','ontology',?)",
                           (f'foreign{index}', self.doc['id'], 1, 'agy', '', canonical(valid['ontology'] | {'revision': index + 50})))
        self.store = Store(self.store.path); job = self.start(item)
        self.assertEqual([r['id'] for r in self.store.dictionary_retries(job)], [valid['id']])

    def test_generation_fresh_then_resumes_only_own_target(self):
        source = SOURCE.ljust(4000) + SOURCE
        self.doc = self.store.register('long', 'long.md', source.encode(), source)
        calls = []
        outputs = iter([DICTIONARY, DICTIONARY, DICTIONARY, ValueError('stop'), DICTIONARY])
        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                calls.append(json.loads(prompt.split('입력(JSON):\n', 1)[1]))
                output = next(outputs)
                if isinstance(output, Exception): raise output
                return wire_dictionary(output, prompt)
        def execute(regenerate=None):
            service = Service(self.store, self.root, Fake())
            run = service.start(self.doc['id'], 'agy', '', 'ontology', 1, 1, regenerate=regenerate)
            service.pool.shutdown(wait=True)
            return self.store.run(run['id'])
        initial = execute(); self.assertEqual(initial['status'], 'success')
        item = self.dictionary(); self.store.review(item['id'], 1, 'rejected', 'Reviewer', '재생성')
        target = {'id': item['id'], 'revision': 2}
        failed = execute(target); self.assertEqual(failed['status'], 'error')
        self.assertEqual(len(calls), 4); self.assertEqual(self.dictionary()['run_id'], initial['id'])
        resumed = execute(target); self.assertEqual(resumed['status'], 'success', resumed['detail'])
        self.assertEqual(len(calls), 5)
        self.assertIn(failed['id'], resumed['detail'])
        self.assertEqual(self.dictionary()['run_id'], resumed['id'])
        self.assertTrue((self.root / 'runs' / initial['id'] / 'dictionary-exclusions.json').exists())


class RegenerationHTTP(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_api_regeneration_and_warning_origin(self):
        doc = self.request('/api/documents', 'POST', {'title': 'fixture', 'filename': 'x.md', 'text': SOURCE})
        item = approved_dictionary(self.store, doc['id'])
        self.store.review(item['id'], item['revision'], 'rejected', 'Reviewer', '재생성')
        body = {'agent': 'agy', 'model': '', 'expected_version': 1, 'expected_generation': 1,
                'regenerate': {'id': item['id'], 'revision': 3}}
        for bad in (None, True, {}, {'id': item['id'], 'revision': True}):
            with self.assertRaises(HTTPError) as error:
                self.request(f'/api/documents/{doc["id"]}/ontology', 'POST', body | {'regenerate': bad})
            self.assertEqual(error.exception.code, 400); error.exception.close()
        with patch.object(self.service.cli, 'complete', side_effect=lambda a, m, p, f, **kw: wire_dictionary(DICTIONARY, p)):
            run = self.request(f'/api/documents/{doc["id"]}/ontology', 'POST', body)
            self.service.pool.shutdown(wait=True)
        current = self.request(f'/api/documents/{doc["id"]}')['items'][0]
        self.assertEqual((current['status'], current['run_id']), ('pending', run['id']))
        self.assertTrue(current['dictionary_exclusions']['available'])
        self.assertIsNone(current['dictionary'])
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/documents/{doc["id"]}/ontology', 'POST', body)
        self.assertEqual(error.exception.code, 409); error.exception.close()


class RegenerationUI(unittest.TestCase):
    def test_rejected_button_and_request_target(self):
        from pathlib import Path
        source = Path('verinote/web/app.js').read_text()
        function = source[source.index('function extractionControls'):source.index('function renderDictionaryExclusions')]
        script = "const config={selected:'agy'};function providerSelect(){return ''}function providerHint(){return ''}function tokenLimitControls(){return ''}\n" + function + """
const base={items:[{id:'o',revision:3,kind:'ontology',status:'rejected'}],can_extract:false};
const output=extractionControls(base);
if(!output.includes('반려한 사전 다시 생성')||/id="ontology" disabled/.test(output))process.exit(1);
for(const status of ['approved','pending','held']){base.items[0].status=status;if(!/id="ontology" disabled/.test(extractionControls(base)))process.exit(2);}
"""
        subprocess.run(['node', '-e', script], check=True)
        self.assertIn("payload.regenerate={id:dictionary.id,revision:dictionary.revision}", source)
