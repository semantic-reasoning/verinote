"""Disposable schema upgrade, immutable experiment contracts and atomic publication."""
import copy
import itertools
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_workflow as workflow
from test_extraction_scheduler import Caller
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.extraction_options import experiment_request, extraction_options
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service
from verinote.store import Store


class ExecutionContract(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self):
        doc = self.store.register('contract', 'contract.md', SOURCE.encode(), SOURCE)
        approved_dictionary(self.store, doc['id'])
        return doc

    def options(self): return experiment_request(ExtractionSpec('/fake/claude', 'model', 2, 10, 1048576, 'null'))

    def register(self, doc, options=None):
        return self.store.create_run(doc['id'], 'claude', 'model', 'extract', doc['version'], doc['generation'], extraction_options=options)

    def test_strict_contract_validation(self):
        request = self.options()
        invalid = [request | {'schema_version': True}, request | {'concurrency': True}, request | {'concurrency': 3},
                   request | {'document_generation': 1}, request | {'unknown': 1},
                   request | {'transport': request['transport'] | {'model': 'other'}},
                   request | {'transport': request['transport'] | {'binary': 'relative'}},
                   request | {'transport': request['transport'] | {'timeout': -1}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                extraction_options(value, registered=False, model='model')
        for agent, stage in [('agy', 'extract'), ('claude', 'ontology')]:
            with self.assertRaises(ValueError): extraction_options(request, registered=False, agent=agent, stage=stage)

    def test_nonnull_malformed_persistence_never_becomes_legacy(self):
        doc = self.document(); run = self.register(doc, self.options())
        for raw in ('', 'null', '{}', 'false', '"bad"'):
            with self.subTest(raw=raw):
                with self.store.connect(True) as db:
                    db.execute('UPDATE runs SET extraction_options=? WHERE id=?', (raw, run['id']))
                with self.assertRaises(ValueError): self.store.run(run['id'])

    def test_generation_injected_and_input_copied(self):
        doc = self.document(); options = self.options(); before = copy.deepcopy(options)
        run = self.register(doc, options)
        self.assertEqual(options, before)
        self.assertEqual(run['extraction_options']['document_generation'], doc['generation'])
        options['transport']['model'] = 'other'
        self.assertEqual(self.store.run(run['id'])['extraction_options']['transport']['model'], 'model')
        self.assertIsNone(run['execution_options'])

    def test_corrupted_saved_contract_fails_before_calls(self):
        doc = self.document(); run = self.register(doc, self.options())
        run['extraction_options']['concurrency'] = True
        caller = Caller(lambda i, r: extraction_response())
        service = Service(self.store, self.root, caller, extraction_caller=caller); self.addCleanup(service.close)
        service.slots.acquire(); service.extract(run)
        self.assertEqual(caller.calls, [])
        self.assertEqual(self.store.run(run['id'])['status'], 'error')

    def test_publication_checks_version_generation_and_dictionary_under_transaction(self):
        for capacity, mutation in itertools.product((1, 2), ('version', 'generation', 'approval', 'revision')):
            with self.subTest(capacity=capacity, mutation=mutation):
                options = self.options(); options['concurrency'] = options['transport']['capacity'] = capacity
                doc = self.document(); run = self.register(doc, options); self.store.run_status(run['id'], 'running')
                with self.store.connect(True) as db:
                    if mutation == 'version': db.execute('UPDATE documents SET current_version=2 WHERE id=?', (doc['id'],))
                    elif mutation == 'generation': db.execute('UPDATE documents SET generation=generation+1 WHERE id=?', (doc['id'],))
                    elif mutation == 'approval': db.execute("UPDATE items SET status='rejected' WHERE id=?", (run['ontology']['id'],))
                    else: db.execute('UPDATE items SET revision=revision+1 WHERE id=?', (run['ontology']['id'],))
                value = extraction_response()
                with self.assertRaises(ValueError): self.store.finish_run(run['id'], value, value['facts'])
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE kind!='ontology'").fetchone()[0], 0)
                # Isolated subcase data cleanup, no production database.
                with self.store.connect(True) as db:
                    for table in ('documents', 'versions', 'items', 'runs', 'events'): db.execute('DELETE FROM '+table)

    def test_nullable_upgrade_idempotency_transaction_rollback_and_backup_restore(self):
        doc = self.document(); legacy = self.register(doc)
        self.store.run_status(legacy['id'], 'error')
        with self.store.connect(True) as db: db.execute('ALTER TABLE runs DROP COLUMN extraction_options')
        backup = self.root/'before-upgrade.sqlite3'
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(backup)) as target: source.backup(target)
        # Interrupted DDL transaction rolls back; rerun then repeated upgrades are safe.
        db = sqlite3.connect(self.store.path)
        db.execute('BEGIN IMMEDIATE'); db.execute('ALTER TABLE runs ADD COLUMN extraction_options TEXT'); db.rollback(); db.close()
        for _ in range(2): self.store = Store(self.store.path)
        self.assertIsNone(self.store.run(legacy['id'])['extraction_options'])
        new = self.register(doc, self.options())
        # Legacy reader selects known columns; legacy writer omits the nullable new column.
        with self.store.connect(True) as db:
            self.assertEqual(db.execute('SELECT id,status,ontology FROM runs WHERE id=?', (new['id'],)).fetchone()['status'], 'queued')
            db.execute("INSERT INTO runs(id,document_id,status,stage) VALUES('legacy-writer','synthetic-other','error','extract')")
            self.assertIsNone(db.execute("SELECT extraction_options FROM runs WHERE id='legacy-writer'").fetchone()[0])
        self.store.recover(); self.assertEqual(self.store.run(new['id'])['status'], 'interrupted')
        restored = self.root/'restored.sqlite3'
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target: source.backup(target)
        restored_store = Store(restored)
        self.assertEqual(restored_store.run(legacy['id'])['status'], 'error')
        with restored_store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs WHERE id=?', (new['id'],)).fetchone()[0], 0)

    def test_registration_freezes_cli_before_queued_execution(self):
        from concurrent.futures import Future

        from verinote.agents import CLI
        cli = CLI(timeout=17); service = Service(self.store, self.root, cli); self.addCleanup(service.close)
        doc = self.document()
        with patch('verinote.extraction_transport.executable', return_value='/fake/claude'), patch.object(service.pool, 'submit', return_value=Future()) as submit:
            run = service.start(doc['id'], 'claude', 'model', expected_version=1, expected_generation=1, extraction_concurrency=2)
        cli.timeout = 99; cli.max_output = 1
        saved = run['extraction_options']['transport']
        self.assertEqual(saved['timeout'], 17); self.assertEqual(saved['max_output'], 1048576)
        self.assertEqual(submit.call_count, 1)
