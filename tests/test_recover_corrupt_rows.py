"""Startup recovery skips a question row it cannot read or repair, leaves that row's stored bytes unwritten, still repairs the other rows,
and returns the skipped ids; server startup reports only their count and ids (#911)."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.server import main
from verinote.store import QUESTION_RESTART_CANCEL, Store, ident

SENTINEL = 'SENTINEL911'
ROUND = [{'round': 1, 'status': 'running', 'nodes': []}]


def insert(db, key, payload):
    """Store payload as the row's TEXT value byte for byte: bytes go in through CAST so invalid UTF-8 stays as stored, None is SQL NULL."""
    if isinstance(payload, bytes):
        db.execute('INSERT INTO questions VALUES(?,CAST(? AS TEXT),?)', (key, payload, '2026-01-01'))
    else:
        db.execute('INSERT INTO questions VALUES(?,?,?)', (key, payload, '2026-01-01'))


class RecoverCorruptRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'v.sqlite3')

    def seed(self, corrupt):
        """Insert the corrupt rows first (lower rowids), then a running, a cancelling, a cancelled-with-running-round and a success row,
        and an AFTER UPDATE trigger that records each written id. Returns the corrupt ids in insertion order."""
        bad = [ident() for _ in corrupt]
        self.valid = {name: ident() for name in ('running', 'cancelling', 'cancelled', 'success')}
        with self.store.connect(True) as db:
            for key, payload in zip(bad, corrupt, strict=True):
                insert(db, key, payload)
            for name, key in self.valid.items():
                job = {'id': key, 'status': name, 'question': name}
                if name == 'cancelled':
                    job['context_processing'] = ROUND
                insert(db, key, json.dumps(job))
            db.execute('CREATE TABLE writes(id TEXT)')
            db.execute('CREATE TRIGGER counted AFTER UPDATE ON questions BEGIN INSERT INTO writes VALUES(NEW.id); END')
        return bad

    def raw(self, key):
        with self.store.connect() as db:
            return db.execute('SELECT CAST(payload AS BLOB) FROM questions WHERE id=?', (key,)).fetchone()[0]

    def writes(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute('SELECT id FROM writes')]

    def check(self, corrupt):
        bad = self.seed(corrupt)
        before = [self.raw(key) for key in bad]
        self.assertEqual(self.store.recover(), bad)
        self.assertEqual([self.raw(key) for key in bad], before)
        self.assertEqual(sorted(self.writes()), sorted(self.valid[n] for n in ('running', 'cancelling', 'cancelled')))
        running = self.store.question(self.valid['running'])
        self.assertEqual((running['status'], running['error']), ('interrupted', '서버 재시작으로 중단되었습니다.'))
        cancelling = self.store.question(self.valid['cancelling'])
        self.assertEqual((cancelling['status'], cancelling['error']), ('cancelled', QUESTION_RESTART_CANCEL))
        cancelled = self.store.question(self.valid['cancelled'])
        self.assertEqual([t['status'] for t in cancelled['context_processing']], ['cancelled'])
        self.assertEqual(self.store.question(self.valid['success'])['status'], 'success')

    def test_payload_not_json_is_skipped(self):
        self.check(['not json{'])

    def test_payload_without_status_is_skipped(self):
        self.check([json.dumps({'id': 'x', 'question': 'q'})])

    def test_cancelled_question_with_a_string_round_is_skipped(self):
        self.check([json.dumps({'status': 'cancelled', 'context_processing': ['x']})])

    def test_null_payload_is_skipped(self):
        self.check([None])

    def test_top_level_json_array_is_skipped(self):
        self.check(['[]'])

    def test_invalid_utf8_byte_is_skipped(self):
        self.check([b'{"status":"running","q":"\xff"}'])

    def test_utf8_encoded_surrogate_is_skipped(self):
        self.check([b'{"status":"running","q":"\xed\xa0\x80"}'])

    def test_utf8_bom_before_json_is_skipped(self):
        self.check([b'\xef\xbb\xbf{"status":"running"}'])

    def test_escaped_lone_surrogate_whose_repair_cannot_be_stored_as_utf8_is_skipped(self):
        self.check([b'{"status":"running","q":"\\ud800"}'])

    def test_too_deeply_nested_json_is_skipped(self):
        self.check([b'[' * 200000])

    def test_success_row_with_utf8_encoded_surrogate_is_returned_though_recover_would_not_change_it(self):
        self.check([b'{"status":"success","q":"\xed\xa0\x80"}'])

    def test_three_issue_shapes_mixed_with_valid_rows_return_their_ids_in_rowid_order(self):
        self.check(['not json{', json.dumps({'id': 'x'}), json.dumps({'status': 'cancelled', 'context_processing': ['x']})])

    def test_skipped_running_and_cancelling_rows_keep_their_stored_status(self):
        running = json.dumps({'status': 'running', 'context_processing': 'ab'})
        cancelling = json.dumps({'status': 'cancelling', 'context_processing': ['x']})
        bad = self.seed([running, cancelling])
        self.assertEqual(self.store.recover(), bad)
        self.assertEqual([self.store.question(key)['status'] for key in bad], ['running', 'cancelling'])

    def test_success_row_with_a_string_round_is_neither_written_nor_returned(self):
        key = ident()
        with self.store.connect(True) as db:
            insert(db, key, json.dumps({'status': 'success', 'context_processing': ['x']}))
            db.execute('CREATE TABLE writes(id TEXT)')
            db.execute('CREATE TRIGGER counted AFTER UPDATE ON questions BEGIN INSERT INTO writes VALUES(NEW.id); END')
        self.assertEqual(self.store.recover(), [])
        self.assertEqual(self.writes(), [])

    def test_update_failure_raised_by_a_trigger_propagates_and_writes_no_row(self):
        key = ident()
        with self.store.connect(True) as db:
            insert(db, key, json.dumps({'status': 'running'}))
            db.execute("CREATE TRIGGER refuse BEFORE UPDATE ON questions BEGIN SELECT RAISE(ABORT,'refused'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.recover()
        self.assertEqual(self.store.question(key)['status'], 'running')


class StartupReport(unittest.TestCase):
    def start(self, rows):
        """Run server.main on a data dir whose name holds the sentinel, with rows inserted first; return stderr."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / SENTINEL
        store = Store(root / 'verinote.sqlite3')
        with store.connect(True) as db:
            for key, payload in rows:
                insert(db, key, payload)
        err = io.StringIO()
        with patch('verinote.server.load_config'), patch('verinote.server.Service'), patch('verinote.server.Server') as server, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(['--data-dir', str(root)])
        return err.getvalue()

    def test_skipped_rows_are_reported_by_count_and_hex_id_without_content_path_or_unreadable_id(self):
        bad = [ident(), ident()]
        rows = [(bad[0], '{"question":"%s"' % SENTINEL), (bad[1], json.dumps({'question': SENTINEL})),
                (SENTINEL, json.dumps({'status': 'cancelled', 'context_processing': [SENTINEL]})), (ident(), json.dumps({'status': 'running'}))]
        err = self.start(rows)
        self.assertIn('Startup recovery skipped 3 question row(s)', err)
        self.assertIn('may still show as in progress', err)
        self.assertIn(f'Ids: {bad[0]}, {bad[1]} (1 more with an id not shown)', err)
        self.assertNotIn(SENTINEL, err)

    def test_clean_store_prints_no_recovery_line(self):
        self.assertEqual(self.start([(ident(), json.dumps({'status': 'running'}))]), '')


if __name__ == '__main__':
    unittest.main()
