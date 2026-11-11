"""Question rows that are corrupt in a way #911/#914 did not cover (#917): a payload missing a field create_question always writes as text, or
holding it as another type (A1-A7); a status that is not a question status; a stored id that is not the row id (A8); and a row id that is not
stored as strict UTF-8 text (B1 invalid UTF-8 text, B2 BLOB, NULL). Startup recovery skips each without writing it, every reader classifies
it as unreadable, document deletion keeps and counts it, and log cleanup removes an obligation whose id the server never makes without
touching files. Rows are seeded into SQLite directly; no response carries the question text or the data directory."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_recover_corrupt_rows import DROP, full, insert

from verinote.server import Server, main
from verinote.service import Service
from verinote.store import Conflict, Store, UnreadableQuestion, ident

SECRET = 'SECRET917'
DETAIL = '손상되어 읽을 수 없는 질문 기록입니다. 내용을 추측해 고치지 않으므로 질문·상태·답변을 표시하지 않습니다.'
WAITING = '추출 또는 질문 작업이 진행 중입니다. 모든 작업 완료 후 삭제하세요.'

# name -> overrides of full() for that shape's one defect; status is running unless the shape is about status. Hand-written, each is
# expected unreadable.
A_SHAPES = {
    'A1_no_id': {'id': DROP}, 'A1_int_id': {'id': 5},
    'A2_no_agent': {'agent': DROP}, 'A2_null_agent': {'agent': None},
    'A3_no_model': {'model': DROP}, 'A3_list_model': {'model': ['m']}, 'A3_null_model': {'model': None},
    'A4_no_created_at': {'created_at': DROP}, 'A4_null_created_at': {'created_at': None},
    'A5_no_snapshot': {'snapshot': DROP}, 'A6_list_snapshot': {'snapshot': ['s']}, 'A6_dict_snapshot': {'snapshot': {}},
    'A7_int_snapshot': {'snapshot': 5}, 'A7_null_snapshot': {'snapshot': None},
    'A8_other_id': {'id': 'b' * 32},
    'no_question': {'question': DROP},
    'status_null': {'status': None}, 'status_int': {'status': 5}, 'status_foo': {'status': 'foo'},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.make()

    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / SECRET
        self.store = Store(self.root / 'verinote.sqlite3')
        self.doc = self.store.register('문서', 'a.md', b'source', 'source')
        self.snapshot = self.store.snapshot()['id']

    def readable(self, status='success'):
        job = self.store.create_question('readable', 'agy', '', self.snapshot)
        if status != 'queued':
            job['status'] = status
            self.store.save_question(job)
        return job['id']

    def a_payload(self, key, overrides):
        """A shape's payload on the document's snapshot, so a row read as readable would be deleted with the document."""
        override = {'question': SECRET, 'snapshot': self.snapshot} | overrides
        return full(key, **override)

    def insert_null_id(self, payload):
        with self.store.connect(True) as db:
            db.execute('INSERT INTO questions VALUES(NULL,?,?)', (payload, '2026-01-01'))

    def raw_rows(self):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute('SELECT typeof(id),CAST(id AS BLOB),CAST(payload AS BLOB) FROM questions ORDER BY rowid')]

    def count_writes(self):
        with self.store.connect(True) as db:
            db.execute('CREATE TABLE writes(id BLOB)')
            db.execute('CREATE TRIGGER counted AFTER UPDATE ON questions BEGIN INSERT INTO writes VALUES(NEW.id); END')

    def writes(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute('SELECT id FROM writes')]

    def pending(self):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute('SELECT kind,typeof(id),hex(id) FROM log_cleanup ORDER BY kind,hex(id)')]

    def delete(self):
        doc = self.store.document(self.doc['id'])
        return self.store.delete_document(doc['id'], False, doc['current_version'], doc['generation'])

    def request(self):
        doc = self.store.document(self.doc['id'])
        return {'expected_version': doc['current_version'], 'expected_generation': doc['generation']}


class Classification(Base):
    def test_each_a_shape_is_skipped_by_recover_unwritten_and_classified_unreadable_by_every_reader(self):
        shapes = dict(A_SHAPES, A1_null=None)
        for name, overrides in shapes.items():
            with self.subTest(name):
                self.make()
                readable = self.readable()
                if overrides is None:
                    key = None
                    self.insert_null_id(self.a_payload(None, {}))
                else:
                    key = ident()
                    with self.store.connect(True) as db:
                        insert(db, key, self.a_payload(key, overrides))
                before = self.raw_rows()
                self.count_writes()
                self.assertEqual(self.store.recover(), [key])
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.store.unreadable_questions(), {'ids': [], 'hidden': 1} if key is None else {'ids': [key], 'hidden': 0})
                if key is not None:
                    with self.assertRaises(UnreadableQuestion):
                        self.store.question(key)
                    with self.assertRaises(UnreadableQuestion):
                        self.store.cancel_question(key, 'running')
                    self.assertEqual(self.store.queue_rows([('question', key)]),
                                     [{'kind': 'question', 'id': key, 'status': None, 'unreadable': True, 'question': ''}])
                self.assertEqual([q['id'] for q in self.store.questions()], [readable])
                owners, total = self.store.usage_owners()
                self.assertEqual(([o['id'] for o in owners], total), ([readable], 2))
                result = self.delete()
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept']), (1, 1))
                self.assertEqual(self.raw_rows(), [row for row in before if row[1] != readable.encode()])
                if key is not None:
                    self.assertEqual(self.store.delete_unreadable_question(key), {'deleted_question': key})
                    self.assertEqual(self.raw_rows(), [])
                self.assertEqual(self.writes(), [])

    def test_a_running_a_shape_does_not_refuse_deletion_but_a_readable_running_question_still_does(self):
        for name in ('A5_no_snapshot', 'A1_no_id'):
            with self.store.connect(True) as db:
                key = ident()
                insert(db, key, self.a_payload(key, A_SHAPES[name]))
        result = self.delete()
        self.assertEqual(result['unreadable_questions_kept'], 2)
        self.doc = self.store.register('문서', 'a.md', b'source', 'source')
        self.snapshot = self.store.snapshot()['id']
        self.readable('queued')
        with self.assertRaises(Conflict) as refused:
            self.delete()
        self.assertEqual(str(refused.exception), WAITING)


class Routes(Base):
    def serve(self):
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        server = Server(('127.0.0.1', 0), self.store, service)
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
        self.assertNotIn(str(self.root.parent), body)
        return status, json.loads(body)

    def test_routes_answer_next_to_each_a_shape(self):
        for name, overrides in A_SHAPES.items():
            if not name.startswith('A'):
                continue
            with self.subTest(name):
                self.make()
                self.serve()
                readable = self.readable()
                key = ident()
                with self.store.connect(True) as db:
                    insert(db, key, self.a_payload(key, overrides))
                before = self.raw_rows()[-1]
                status, rows = self.call('/api/questions')
                self.assertEqual((status, [r['id'] for r in rows]), (200, [readable]))
                self.assertEqual(self.call(f'/api/questions/{readable}')[0], 200)
                self.assertEqual(self.call(f'/api/questions/{key}'), (409, {'error': DETAIL}))
                status, usage = self.call('/api/usage')
                self.assertEqual((status, usage['included_jobs'], usage['total_jobs']), (200, 1, 2))
                status, result = self.call(f"/api/documents/{self.doc['id']}/delete", 'POST', self.request())
                self.assertEqual((status, result['removed_questions'], result['unreadable_questions_kept']), (200, 1, 1))
                self.assertEqual(self.raw_rows(), [before])

    def test_b1_document_delete_keeps_and_counts_the_row(self):
        bad_id = b'\xff' + b'dead' * 8
        for via in ('store', 'http'):
            with self.subTest(via):
                self.make()
                self.readable()
                with self.store.connect(True) as db:
                    db.execute('INSERT INTO questions VALUES(CAST(? AS TEXT),?,?)', (bad_id, self.a_payload(ident(), {}), '2026-01-01'))
                if via == 'store':
                    result = self.delete()
                else:
                    self.serve()
                    status, result = self.call(f"/api/documents/{self.doc['id']}/delete", 'POST', self.request())
                    self.assertEqual(status, 200)
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept']), (1, 1))
                self.assertEqual([row[:2] for row in self.raw_rows()], [('text', bad_id)])
                self.assertEqual(self.store.unreadable_questions(), {'ids': [], 'hidden': 1})


class RowIds(Base):
    def test_b2_document_delete_commits_reports_success_and_service_restarts(self):
        for name in ('blob', 'null'):
            with self.subTest(name):
                self.make()
                self.readable()
                key = ident()
                with self.store.connect(True) as db:
                    if name == 'blob':
                        db.execute('INSERT INTO questions VALUES(CAST(? AS BLOB),?,?)', (key, self.a_payload(key, {'status': 'success'}), '2026-01-01'))
                    else:
                        db.execute('INSERT INTO questions VALUES(NULL,?,?)', (self.a_payload(None, {'status': 'success'}), '2026-01-01'))
                service = Service(self.store, self.root)
                self.addCleanup(service.close)
                result = service.delete_document(self.doc['id'], False, self.request())
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept'], result['cleanup_status']), (1, 1, 'complete'))
                self.assertEqual(self.pending(), [])
                self.assertEqual([row[0] for row in self.raw_rows()], [name])
                self.assertEqual(self.store.unreadable_questions(), {'ids': [], 'hidden': 1})
                Service(self.store, self.root).close()

    def test_b3_text_id_the_server_never_makes(self):
        # An integer bound into questions.id (TEXT affinity) is stored as the text '12345'.
        cases = {'integer_id_payload_differs': (12345, '99999', 1), 'integer_id_payload_equal': (12345, '12345', 0),
                 'null_id_payload_null': (None, None, 1)}
        for name, (row_id, payload_id, kept) in cases.items():
            with self.subTest(name):
                self.make()
                self.readable()
                with self.store.connect(True) as db:
                    db.execute('INSERT INTO questions VALUES(?,?,?)', (row_id, self.a_payload(payload_id, {'status': 'success'}), '2026-01-01'))
                service = Service(self.store, self.root)
                self.addCleanup(service.close)
                result = service.delete_document(self.doc['id'], False, self.request())
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept'], result['cleanup_status']), (2 - kept, kept, 'complete'))
                self.assertEqual(self.pending(), [])
                self.assertEqual(len(self.raw_rows()), kept)
                self.assertEqual(self.store.unreadable_questions(), {'ids': [], 'hidden': kept})

    def test_cleanup_drops_obligations_with_an_id_the_server_never_makes_and_touches_no_file(self):
        valid, upper = ident(), 'A' * 32
        for key in (valid, upper):
            folder = self.root / 'questions' / key
            folder.mkdir(parents=True)
            (folder / 'prompt.txt').write_text(SECRET)
        with self.store.connect(True) as db:
            db.executemany('INSERT INTO log_cleanup VALUES(?,?)', [('questions', ident().encode()), ('questions', None), ('runs', '12345'),
                                                                   ('questions', upper), ('questions', valid)])
        service = Service(self.store, self.root)  # runs cleanup_logs once
        self.addCleanup(service.close)
        # Which (kind, typeof(id), hex(id)) obligations remain, not how many: none.
        self.assertEqual(self.pending(), [])
        self.assertEqual(service.cleanup_logs(), {'cleanup_pending': 0, 'cleanup_status': 'complete'})
        self.assertFalse((self.root / 'questions' / valid).exists())
        self.assertEqual((self.root / 'questions' / upper / 'prompt.txt').read_text(), SECRET)


class Startup(unittest.TestCase):
    def test_b1_server_main_starts_repairs_other_rows_and_prints_no_id_bytes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / SECRET
        store = Store(root / 'verinote.sqlite3')
        running = ident()
        with store.connect(True) as db:
            db.execute('INSERT INTO questions VALUES(CAST(? AS TEXT),?,?)', (b'\xff' + b'dead' * 8, full(ident(), question=SECRET), '2026-01-01'))
            insert(db, running, full(running))
        err = io.StringIO()
        with patch('verinote.server.load_config'), patch('verinote.server.Service'), patch('verinote.server.Server') as server, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(['--data-dir', str(root)])
        server.assert_called_once()
        self.assertEqual(store.question(running)['status'], 'interrupted')
        out = err.getvalue()
        self.assertIn('Startup recovery skipped 1 question row(s)', out)
        self.assertIn('Ids: none shown (1 more with an id not shown)', out)
        for absent in ('dead', SECRET, str(root.parent)):
            self.assertNotIn(absent, out)


if __name__ == '__main__':
    unittest.main()
