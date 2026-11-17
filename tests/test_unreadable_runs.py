"""A run row whose id is not stored as strict UTF-8 text (BLOB, NULL, non-UTF-8 text) and a log_cleanup row with such a column are read without
raising: listings leave the run out, document detail counts it, recovery addresses it by rowid, deleting its document removes it and reports
the logs it could not locate, and no response carries the stored bytes (#921)."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from verinote.extraction_loss import RecoveryBudget
from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, Store, ident

SECRET = 'SECRET921'
BAD = SECRET.encode() + b'\xff'
HEX = 'ab' * 16

SHAPES = {
    'blob_hex': ('CAST(? AS BLOB)', HEX.encode()),
    'null': ('?', None),
    'bad_text': ('CAST(? AS TEXT)', BAD),
}


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / SECRET
        self.store = Store(self.root / 'verinote.sqlite3')
        self.a = self.store.register('A', 'a.md', b'source a', 'source a')
        self.b = self.store.register('B', 'b.md', b'source b', 'source b')

    def insert(self, shape, status='success', document=None, version=1, stage='extract', key=None):
        expr, value = SHAPES[shape]
        if shape == 'blob_hex' and key:
            value = key.encode()
        elif shape == 'bad_text' and key:
            value = BAD + key.encode()
        with self.store.connect(True) as db:
            db.execute(f'INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES({expr},?,?,?,?,?,?,?,?)',
                       (value, (document or self.a)['id'], version, 'agy', 'm', status, '', '2026-01-01', stage))

    def plain(self, doc, status='interrupted', created='2026-01-02'):
        key = ident()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES(?,?,1,?,?,?,?,?,?)',
                       (key, doc['id'], 'agy', 'm', status, '', created, 'extract'))
        return self.store.run(key)

    def runs_left(self):
        with self.store.connect() as db:
            return db.execute('SELECT count(*) FROM runs').fetchone()[0]

    def delete(self, store, doc):
        service = Service(store, self.root)
        self.addCleanup(service.close)
        doc = store.document(doc['id'])
        return service.delete_document(doc['id'], False, {'expected_version': doc['current_version'], 'expected_generation': doc['generation']})

class UnreadableRuns(Fixture):
    def test_readers_skip_each_shape_and_detail_counts_it(self):
        for shape in SHAPES:
            with self.subTest(shape):
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM runs')
                self.insert(shape)
                self.assertEqual(self.store.runs(), [])
                self.assertEqual(self.store.usage_owners(), ([], 1))
                self.assertEqual(self.store.document(self.a['id'])['unreadable_runs'], 1)
                self.assertEqual(self.store.document(self.b['id'])['unreadable_runs'], 0)
                json.dumps(self.store.document(self.a['id']))

    def test_runs_listing_is_filled_from_readable_rows_after_many_unreadable_ones(self):
        for _ in range(101):
            self.insert('null')
        run = self.plain(self.b, 'success')
        self.assertEqual([r['id'] for r in self.store.runs()], [run['id']])
        self.assertEqual([r['id'] for r in self.store.usage_owners(1)[0]], [run['id']])

    def test_recover_cancels_and_interrupts_each_shape_by_rowid(self):
        for shape in SHAPES:
            for status in ('cancelling', 'running'):
                self.insert(shape, status, document={'id': ident()}, key=ident())
        self.store.recover()
        with self.store.connect() as db:
            self.assertEqual(sorted(r[0] for r in db.execute('SELECT status FROM runs')), ['cancelled'] * 3 + ['interrupted'] * 3)
        self.delete(self.store, self.b)

    def test_cancelling_row_with_a_null_id_does_not_block_other_documents_after_restart(self):
        self.insert('null', 'cancelling')
        with self.assertRaises(Conflict):
            self.delete(self.store, self.b)
        self.store.recover()
        self.assertEqual(self.delete(self.store, self.b)['document_id'], self.b['id'])

    def test_delete_removes_each_shape_and_reports_logs_it_cannot_locate(self):
        for shape, located in (('blob_hex', True), ('null', False), ('bad_text', False)):
            with self.subTest(shape):
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM runs')
                    db.execute('DELETE FROM log_cleanup')
                doc = self.store.register(shape, 'x.md', b'x', 'x')
                self.insert(shape, document=doc)
                folder = self.root / 'runs' / HEX
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'prompt.txt').write_text(SECRET)
                result = self.delete(self.store, doc)
                self.assertEqual(self.runs_left(), 0)
                self.assertEqual(result['unlocated_run_logs'], 0 if located else 1)
                self.assertEqual(result['cleanup_status'], 'complete' if located else 'incomplete')
                self.assertEqual(folder.exists(), not located)
                self.assertEqual(self.store.pending_cleanup(), [])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_a_hex_blob_run_does_not_remove_the_logs_of_a_surviving_text_run(self):
        self.insert('blob_hex')
        with self.store.connect(True) as db:
            db.execute("INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES(?,?,1,'agy','m','success','','2026-01-01','extract')",
                       (HEX, self.b['id']))
        folder = self.root / 'runs' / HEX
        folder.mkdir(parents=True)
        result = self.delete(self.store, self.a)
        self.assertEqual(result['unlocated_run_logs'], 1)
        self.assertTrue(folder.exists())
        self.assertEqual(self.runs_left(), 1)

    def test_retry_lookups_skip_unreadable_rows_before_the_limit(self):
        for shape in SHAPES:
            self.insert(shape, 'interrupted')
        current = self.plain(self.a, 'running')
        for i in range(9):
            self.plain(self.a, created=f'2025-01-0{i + 1}')
        retries = self.store.extraction_retries(current)
        self.assertEqual(len(retries), 8)
        self.assertTrue(all(isinstance(r['id'], str) and r['id'] != current['id'] for r in retries))

    def test_deleting_a_text_run_keeps_the_logs_of_a_surviving_blob_run_with_the_same_bytes(self):
        self.insert('blob_hex', document=self.b)
        with self.store.connect(True) as db:
            db.execute("INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES(?,?,1,'agy','m','success','','2026-01-01','extract')",
                       (HEX, self.a['id']))
        folder = self.root / 'runs' / HEX
        folder.mkdir(parents=True)
        result = self.delete(self.store, self.a)
        self.assertEqual((result['unlocated_run_logs'], result['cleanup_status']), (1, 'incomplete'))
        self.assertTrue(folder.exists())

    def test_unreadable_run_with_an_external_session_is_interrupted_on_restart(self):
        with self.store.connect(True) as db:
            db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage) VALUES(CAST(? AS TEXT),?,1,?,?,?,?,?,?)',
                       (BAD, self.a['id'], 'mcp', 'm', 'running', '', '2026-01-01', 'extract'))
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT INTO external_sessions VALUES(CAST(? AS TEXT),?)', (BAD, '{}'))
        self.store.recover()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], 'interrupted')
        self.delete(self.store, self.b)

    def test_dictionary_retries_and_unchanged_document_refusal_read_unreadable_rows(self):
        self.insert('bad_text', 'interrupted', stage='ontology')
        current = self.plain(self.a, 'running')
        self.assertEqual(self.store.dictionary_retries(current), [])
        with self.store.connect(True) as db:
            db.execute("INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage,ontology) VALUES(NULL,?,1,'agy','m','queued','','2026-01-01','ontology','{}')",
                       (self.b['id'],))
            with self.assertRaises(Conflict) as caught:
                Store.refuse_while_run_needs_unchanged_document(db, self.b['id'])
        self.assertIn('(읽을 수 없는 ID)', str(caught.exception))

    def test_server_starts_with_each_unreadable_shape_in_runs_and_log_cleanup(self):
        import contextlib
        import io
        from unittest.mock import patch

        from verinote.server import main
        for shape in SHAPES:
            self.insert(shape, 'cancelling', document={'id': ident()}, key=ident())
        with self.store.connect(True) as db:
            db.execute("INSERT INTO log_cleanup(kind,id) VALUES('runs',CAST(? AS TEXT))", (BAD,))
            db.execute("INSERT INTO log_cleanup(kind,id) VALUES(CAST(? AS TEXT),'x')", (BAD,))
        err = io.StringIO()
        from verinote.config import load_config
        with patch('verinote.server.load_config', return_value=load_config(None)), patch('verinote.server.Server') as server, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(['--data-dir', str(self.root)])
        self.assertNotIn(SECRET, err.getvalue())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs WHERE status='cancelled'").fetchone()[0], 3)
        self.assertEqual(self.store.pending_cleanup(), [])

    def test_budget_of_an_unreadable_predecessor_is_unknown(self):
        self.insert('bad_text', 'interrupted')
        run = self.plain(self.a, 'running')
        (self.root / 'runs' / run['id']).mkdir(parents=True)
        budget = RecoveryBudget(self.store, self.root.resolve(), run, {})
        self.assertTrue(budget.unknown)

    def test_active_unreadable_run_still_refuses_without_naming_its_id(self):
        self.insert('bad_text', 'running')
        with self.store.connect(True) as db:
            with self.assertRaises(Conflict) as caught:
                Store.refuse_while_run_holds_dictionary(db, self.a['id'], 1)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn('(읽을 수 없는 ID)', str(caught.exception))

    def test_log_cleanup_with_an_unreadable_column_does_not_stop_service_creation(self):
        for column in ('id', 'kind'):
            with self.subTest(column):
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM log_cleanup')
                    db.execute(f"INSERT INTO log_cleanup({'kind,id' if column == 'id' else 'id,kind'}) VALUES('runs',CAST(? AS TEXT))", (BAD,))
                self.assertEqual(self.store.pending_cleanup()[0][column], None)
                try:
                    service = Service(self.store, self.root)
                except sqlite3.Error as error:
                    self.fail(str(error))
                service.close()
                self.assertEqual(self.store.pending_cleanup(), [])


class UnreadableRunsHttp(Fixture):
    def setUp(self):
        super().setUp()
        self.service = Service(self.store, self.root)
        self.addCleanup(self.service.close)
        server = Server(('127.0.0.1', 0), self.store, self.service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        self.url = f'http://127.0.0.1:{server.server_port}'

    def call(self, path, method='GET', data=None):
        request = Request(self.url + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request) as response:
                body, status = response.read().decode(), response.status
        except HTTPError as error:
            with error:
                body, status = error.read().decode(), error.code
        self.assertNotIn(SECRET, body)
        return status, json.loads(body)

    def test_api_answers_each_shape_without_error(self):
        for shape in SHAPES:
            with self.subTest(shape):
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM runs')
                self.insert(shape)
                self.assertEqual(self.call('/api/runs'), (200, []))
                self.assertEqual(self.call('/api/usage')[0], 200)
                self.assertEqual(self.call(f"/api/documents/{self.a['id']}")[0], 200)
                self.assertEqual(self.call(f"/api/documents/{self.b['id']}")[0], 200)

    def test_api_deletes_each_shape_and_answers_incomplete_when_logs_are_unlocated(self):
        for shape, status in (('blob_hex', 'complete'), ('null', 'incomplete'), ('bad_text', 'incomplete')):
            with self.subTest(shape):
                doc = self.store.register(shape, 'x.md', b'x', 'x')
                self.insert(shape, document=doc)
                (self.root / 'runs' / HEX).mkdir(parents=True, exist_ok=True)
                detail = self.store.document(doc['id'])
                code, body = self.call(f"/api/documents/{doc['id']}/delete", 'POST',
                                       {'expected_version': detail['current_version'], 'expected_generation': detail['generation']})
                self.assertEqual((code, body['cleanup_status']), (200, status))
                self.assertFalse((self.root / 'runs' / HEX).exists() and shape == 'blob_hex')
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM runs')


if __name__ == '__main__':
    unittest.main()
