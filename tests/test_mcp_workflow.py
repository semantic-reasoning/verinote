import concurrent.futures
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

from verinote.mcp_workflow import MCPWorkflow
from verinote.service import Service
from verinote.store import Conflict, Store


class MCPWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'verinote.sqlite3')
        self.flow = MCPWorkflow(self.store)
        self.doc = self.store.register('문서', 'test.txt', SOURCE.encode(), SOURCE)

    def start(self, stage='ontology'):
        d = self.store.document(self.doc['id'])
        return self.flow.start(d['id'], stage, d['version'], d['generation'])['id']

    def accept_dictionary(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        self.flow.finalize(key)
        item = self.store.document(self.doc['id'])['items'][0]
        self.store.review(item['id'], item['revision'], 'approved', 'human', '원문 검토')
        return item

    def test_complete_host_workflow_pending_and_idempotent_publication(self):
        with patch('verinote.agents.CLI.complete', side_effect=AssertionError('must not call CLI')):
            self.accept_dictionary()
            key = self.start('extract')
            task = self.flow.task(key)
            self.assertIn('approved_dictionary', task['prompt'])
            self.assertIn(SOURCE, task['prompt'])
            self.assertEqual(task['dictionary_pin']['revision'], 2)
            self.flow.submit(key, 1, extraction_response(), 'extract')
            first = self.flow.finalize(key)
            with self.store.connect() as db:
                events = db.execute('SELECT count(*) FROM events').fetchone()[0]
            self.assertEqual(first, self.flow.finalize(key))
            with self.store.connect() as db:
                self.assertEqual(events, db.execute('SELECT count(*) FROM events').fetchone()[0])
            items = [i for i in self.store.document(self.doc['id'])['items'] if i['kind'] != 'ontology']
            self.assertEqual(len(items), 3)
            self.assertTrue(all(i['status'] == 'pending' for i in items))
            snapshot = self.store.snapshot()
            self.assertEqual(snapshot['facts'], [])
            self.assertTrue(first['review_required'])
            self.assertTrue(self.store.document(self.doc['id'])['can_extract'])

    def test_no_extraction_before_human_approval(self):
        with self.assertRaises(Conflict): self.start('extract')
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        self.flow.finalize(key)
        with self.assertRaises(Conflict): self.start('extract')

    def test_reconnect_and_recovery_preserve_only_external_sessions(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        other = self.store.register('other', 'other.txt', b'other', 'other')
        ordinary = self.store.create_run(other['id'], 'claude', '', 'ontology', 1, 1)
        reopened = Store(self.root / 'verinote.sqlite3')
        self.assertEqual(reopened.run(ordinary['id'])['status'], 'queued')
        reopened.recover()
        service = Service(reopened, self.root)
        self.addCleanup(service.close)
        resumed = MCPWorkflow(reopened)
        self.assertEqual(resumed.status(key)['accepted_chunks'], 1)
        self.assertEqual(reopened.run(ordinary['id'])['status'], 'interrupted')
        self.assertEqual(resumed.finalize(key)['status'], 'success')

    def test_chunk_retry_rejects_changes_out_of_order_and_incomplete_finalize(self):
        key = self.start()
        with self.assertRaises(Conflict): self.flow.submit(key, 2, DICTIONARY, 'ontology')
        with self.assertRaises(Conflict): self.flow.finalize(key)
        one = self.flow.submit(key, 1, DICTIONARY, 'ontology')
        self.assertEqual(one, self.flow.submit(key, 1, copy.deepcopy(DICTIONARY), 'ontology'))
        changed = copy.deepcopy(DICTIONARY)
        changed['relations'][0]['description'] = 'changed'
        with self.assertRaises(Conflict): self.flow.submit(key, 1, changed, 'ontology')

    def test_invalid_dictionary_does_not_consume_chunk(self):
        key = self.start()
        for change in ('quote', 'judged', 'fields'):
            bad = copy.deepcopy(DICTIONARY)
            if change == 'quote': bad['entities'][0]['quote'] = 'absent'
            if change == 'judged': bad['aliases'] = [{'alias': 'fake', 'entity': '주문', 'quote': SOURCE, 'judged_alias': 'fake'}]
            if change == 'fields': bad['extra'] = True
            with self.subTest(change=change), self.assertRaises(ValueError): self.flow.submit(key, 1, bad, 'ontology')
        self.assertEqual(self.flow.status(key)['accepted_chunks'], 0)

    def test_extraction_rejects_invalid_quote_vocabulary_and_coverage(self):
        self.accept_dictionary()
        key = self.start('extract')
        for kind in ('quote', 'entity', 'predicate', 'coverage', 'extra'):
            bad = extraction_response()
            if kind == 'quote': bad['facts'][0]['quote'] = 'absent'
            if kind == 'entity': bad['facts'][0]['subject'] = 'unknown'
            if kind == 'predicate': bad['facts'][0]['predicate'] = 'unknown'
            if kind == 'coverage': bad['coverage'] = []
            if kind == 'extra': bad['extra'] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.flow.submit(key, 1, bad, 'extract')
        self.assertEqual(self.flow.status(key)['accepted_chunks'], 0)

    def test_cross_chunk_quotes_and_ambiguous_quotes_rejected(self):
        source = SOURCE + ' ' * 8000 + '마지막 문장.'
        d = self.store.register('long', 'long.txt', source.encode(), source)
        approved_dictionary(self.store, d['id'])
        key = self.flow.start(d['id'], 'extract', 1, 1)['id']
        bad = extraction_response()
        bad['claims'][0]['quote'] = '마지막 문장.'
        with self.assertRaisesRegex(ValueError, '인용'): self.flow.submit(key, 1, bad, 'extract')
        self.flow.cancel(key)
        repeated = SOURCE + SOURCE
        d = self.store.register('repeat', 'repeat.txt', repeated.encode(), repeated)
        approved_dictionary(self.store, d['id'])
        key = self.flow.start(d['id'], 'extract', 1, 1)['id']
        with self.assertRaisesRegex(ValueError, '인용'): self.flow.submit(key, 1, extraction_response(), 'extract')

    def test_multi_chunk_dictionary_and_extraction_resume(self):
        source = SOURCE + '\n' + '내용. ' * 2100
        d = self.store.register('long', 'long.txt', source.encode(), source)
        key = self.flow.start(d['id'], 'ontology', 1, 1)['id']
        count = self.flow.status(key)['total_chunks']
        for i in range(1, count + 1):
            task = self.flow.task(key)
            self.assertEqual(task['chunk_index'], i)
            self.flow.submit(key, i, DICTIONARY if i == 1 else {'entities': [], 'aliases': [], 'relations': []}, 'ontology')
            self.flow = MCPWorkflow(Store(self.root / 'verinote.sqlite3'))
        self.flow.finalize(key)
        item = self.store.document(d['id'])['items'][0]
        self.store.review(item['id'], 1, 'approved', 'human', 'review')
        key = self.flow.start(d['id'], 'extract', 1, 1)['id']
        for i in range(1, self.flow.status(key)['total_chunks'] + 1):
            task = self.flow.task(key)
            part = source[task['start']:task['end']]
            out = extraction_response()
            out['claims'][0]['quote'] = part
            out['claims'][0]['text'] = '근거 있는 설명'
            if i > 1:
                out['facts'] = []
                out['coverage'][0].update(fact_indices=[], status='no_evidence')
            self.flow.submit(key, i, out, 'extract')
        self.assertEqual(self.flow.finalize(key)['status'], 'success')

    def test_stale_generation_and_approval_rejected_before_read_submit_publish(self):
        item = self.accept_dictionary()
        key = self.start('extract')
        self.flow.submit(key, 1, extraction_response(), 'extract')
        with self.store.connect(True) as db:
            db.execute('UPDATE documents SET generation=generation+1 WHERE id=?', (self.doc['id'],))
        with self.assertRaises(Conflict): self.flow.task(key)
        with self.assertRaises(Conflict): self.flow.finalize(key)
        self.flow.cancel(key)
        key = self.start('extract')
        with self.store.connect(True) as db:
            db.execute("UPDATE items SET status='pending' WHERE id=?", (item['id'],))
        with self.assertRaises(Conflict): self.flow.task(key)
        with self.assertRaises(Conflict): self.flow.submit(key, 1, extraction_response(), 'extract')

    def test_web_cancel_terminal_no_worker_and_no_reviving(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        service.cancel(key, 'running')
        self.assertEqual(self.flow.status(key)['status'], 'cancelled')
        self.assertEqual(self.flow.cancel(key)['status'], 'cancelled')
        with self.assertRaises(Conflict): self.flow.finalize(key)
        with self.assertRaises(Conflict): self.flow.submit(key, 1, DICTIONARY, 'ontology')
        self.assertEqual(self.store.document(self.doc['id'])['items'], [])

    def test_delete_cascades_session_and_old_database_upgrade_is_additive(self):
        key = self.start()
        self.flow.cancel(key)
        self.store.delete_document(self.doc['id'], False, 1, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_sessions').fetchone()[0], 0)
        # Simulate the previous schema; rerunning initialization preserves old rows.
        with self.store.connect(True) as db: db.execute('DROP TABLE external_sessions')
        d = self.store.register('kept', 'kept.txt', b'kept', 'kept')
        Store(self.root / 'verinote.sqlite3')
        Store(self.root / 'verinote.sqlite3')
        self.assertEqual(self.store.document(d['id'])['text'], 'kept')

    def test_concurrent_changed_submissions_and_finalize_are_atomic(self):
        key = self.start()
        other = copy.deepcopy(DICTIONARY)
        other['relations'][0]['description'] = 'another definition'
        def submit(value):
            try: return self.flow.submit(key, 1, value, 'ontology')['accepted_chunks']
            except Conflict: return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(submit, [DICTIONARY, other])), [1, 'conflict'])
            receipts = list(pool.map(self.flow.finalize, [key, key]))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(len(self.store.document(self.doc['id'])['items']), 1)

    def test_finalize_cancel_race_cannot_publish_after_cancel(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        def action(fn):
            try: return fn(key)
            except (Conflict, ValueError): return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(action, [self.flow.finalize, self.flow.cancel]))
        status = self.flow.status(key)
        self.assertIn(status['status'], ('success', 'cancelled'))
        self.assertEqual(len(status['candidate_ids']), 1 if status['status'] == 'success' else 0)

    def test_cli_success_and_mcp_success_have_distinct_dedup_keys(self):
        approved_dictionary(self.store, self.doc['id'])
        run = self.store.create_run(self.doc['id'], 'claude', '', 'extract', 1, 1, extraction_contract=2)
        self.store.run_status(run['id'], 'running')
        out = extraction_response()
        self.store.finish_run(run['id'], page=out, facts=out['facts'])
        key = self.start('extract')
        self.flow.submit(key, 1, out, 'extract')
        self.flow.finalize(key)
        with self.assertRaises(Conflict): self.start('extract')
        run = self.store.create_run(self.doc['id'], 'claude', '', 'extract', 1, 1)
        self.assertEqual(run['status'], 'queued')

    def test_bounds_rollback_and_invalid_primitives(self):
        key = self.start()
        for index in (True, 0, -1, '1'):
            with self.assertRaises(ValueError): self.flow.submit(key, index, DICTIONARY, 'ontology')
        with self.assertRaises(ValueError): self.flow.submit(key, 1, {'large': 'x' * (1024 * 1024)}, 'ontology')
        with patch('verinote.mcp_workflow.bounded_prompt', side_effect=ValueError('context too large')):
            with self.assertRaises(ValueError): self.flow.submit(key, 1, DICTIONARY, 'ontology')
        self.assertEqual(self.flow.status(key)['accepted_chunks'], 0)
        self.flow.cancel(key)
        with patch('verinote.mcp_workflow.bounded_prompt', side_effect=ValueError('context too large')):
            with self.assertRaises(ValueError): self.start()
        self.assertEqual(len(self.flow.list_runs(self.doc['id'])), 1)
        for key in ('../secret', '', None):
            with self.assertRaises(ValueError): self.flow.status(key)
        with self.assertRaises(ValueError): self.flow.list_documents(True)

    def test_public_store_finish_cannot_bypass_mcp_chunk_validation(self):
        key = self.start()
        with self.assertRaises(Conflict): self.store.finish_run(key, dictionary=DICTIONARY)

    def test_aggregate_limits_reject_before_acceptance(self):
        source = ' '.join(f'구절{i}' for i in range(200))
        outputs = [{'title': 't', 'claims': [{'heading': 'h', 'text': 't', 'quote': f'구절{i}'} for i in range(193)], 'facts': []}]
        with self.assertRaises(ValueError): self.flow._aggregate({'outputs': outputs}, source)

    def test_sqlite_backup_restore_preserves_resumable_session(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        backup = self.root / 'backup.sqlite3'
        with sqlite3.connect(self.store.path) as src, sqlite3.connect(backup) as dst: src.backup(dst)
        restored = MCPWorkflow(Store(backup))
        self.assertEqual(restored.finalize(key)['status'], 'success')
        self.assertEqual(self.flow.status(key)['status'], 'running')

    def test_publication_failure_rolls_back_candidates_and_can_retry(self):
        key = self.start()
        self.flow.submit(key, 1, DICTIONARY, 'ontology')
        publish = self.store._finish_run
        def fail_after_publish(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError('simulated failure before commit')
        with patch.object(self.store, '_finish_run', side_effect=fail_after_publish):
            with self.assertRaises(RuntimeError): self.flow.finalize(key)
        self.assertEqual(self.flow.status(key)['status'], 'running')
        self.assertEqual(self.flow.status(key)['candidate_ids'], [])
        self.assertEqual(self.flow.finalize(key)['status'], 'success')

    def test_latest_version_deletion_cleans_only_its_session(self):
        old = self.start()
        self.flow.cancel(old)
        d = self.store.register('v2', 'v2.txt', SOURCE.encode(), SOURCE, self.doc['id'], 1, 1)
        current = self.flow.start(d['id'], 'ontology', d['version'], d['generation'])['id']
        self.flow.cancel(current)
        self.store.delete_document(d['id'], True, d['version'], d['generation'])
        self.assertEqual(self.flow.status(old)['status'], 'cancelled')
        with self.assertRaises(ValueError): self.flow.status(current)

    def test_same_agent_name_does_not_exempt_ordinary_run_from_recovery(self):
        run = self.store.create_run(self.doc['id'], 'mcp', 'untrusted-label', 'ontology', 1, 1)
        self.store.recover()
        self.assertEqual(self.store.run(run['id'])['status'], 'interrupted')
