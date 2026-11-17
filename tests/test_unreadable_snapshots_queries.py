"""Unreadable queries and snapshots rows do not make document deletion fail or silently keep rows; the response counts what was kept,
the query list skips them, and a corrupt snapshot with the current id is rewritten rather than bound (#916)."""
import json
import tempfile
import unittest
from pathlib import Path

from verinote.service import Service
from verinote.store import Store, ident

SECRET = 'SECRET916'
BAD = SECRET.encode() + b'\xff'


def result(snapshot):
    return {'status': 'empty', 'snapshot': snapshot, 'engine': 'pyrewire', 'query': {'action': 'direct', 'subject': '', 'predicate': '', 'target': ''},
            'rows': [], 'proofs': [], 'facts': []}


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'verinote.sqlite3')
        self.a = self.store.register('A', 'a.md', b'source a', 'source a')
        self.b = self.store.register('B', 'b.md', b'source b', 'source b')
        self.snapshot = self.store.snapshot()

    def put(self, table, key, payload, blob_id=False):
        with self.store.connect(True) as db:
            expr = 'CAST(? AS BLOB)' if blob_id else '?'
            db.execute(f'INSERT INTO {table} VALUES({expr},CAST(? AS TEXT),?)', (key, payload, '2026-01-01'))

    def query(self, snapshot, key=None):
        key = key or ident()
        self.put('queries', key, json.dumps(result(snapshot)).encode())
        return key

    def delete(self, doc):
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        doc = self.store.document(doc['id'])
        return service.delete_document(doc['id'], False, {'expected_version': doc['current_version'], 'expected_generation': doc['generation']})

    def count(self, table):
        with self.store.connect() as db:
            return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


class Queries(Fixture):
    def test_each_unreadable_query_shape_is_kept_and_counted(self):
        shapes = [b'not json', BAD, b'{}', b'[]', b'{"snapshot":[1]}', b'{"snapshot":{"a":1}}', b'{"snapshot":5}', b'{"snapshot":null}']
        for raw in shapes:
            self.put('queries', ident(), raw)
        self.query(self.snapshot['id'])
        result = self.delete(self.a)
        self.assertEqual((result['unreadable_queries_kept'], result['removed_queries']), (len(shapes), 1))
        self.assertEqual(self.count('queries'), len(shapes))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_list_skips_unreadable_rows_beyond_the_newest_thirty(self):
        self.put('queries', ident(), b'not json')
        self.put('queries', ident(), BAD)
        self.put('queries', ident(), b'{"snapshot":1}')
        for _ in range(31):
            self.query(self.snapshot['id'])
        listed = self.store.queries()
        self.assertEqual(len(listed), 30)
        self.assertTrue(all(isinstance(row['payload'], dict) and isinstance(row['id'], str) for row in listed))
        with self.store.connect(True) as db:
            db.execute("DELETE FROM queries WHERE payload LIKE '%snapshot%'")
        self.assertEqual(self.store.queries(), [])

    def test_list_returns_the_complete_stored_result_of_a_readable_row(self):
        stored = result('s') | {'rows': [['a', 'p', 'b']], 'scope': 'x'}
        key = ident()
        self.put('queries', key, json.dumps(stored).encode())
        self.assertEqual(self.store.queries(), [{'id': key, 'payload': stored, 'created_at': '2026-01-01'}])

    def test_ambiguous_and_full_results_are_listed_and_deleted_with_their_snapshot(self):
        ambiguous = result(self.snapshot['id']) | {'status': 'ambiguous', 'proofs': [{'row': ['a', 'p', 'b'], 'fact_ids': ['f']}], 'rows': [['a', 'p', 'b']],
                                                    'facts': [{'id': 'f', 'payload': {'subject': 'a'}}]}
        key = ident()
        self.put('queries', key, json.dumps(ambiguous).encode())
        self.assertEqual([row['id'] for row in self.store.queries()], [key])
        self.assertEqual(self.store.unreadable_queries(), {'count': 0})
        result_ = self.delete(self.a)
        self.assertEqual((result_['removed_queries'], result_['unreadable_queries_kept']), (1, 0))

    def test_blob_query_id_does_not_break_list_or_delete(self):
        self.put('queries', b'\x00\x01', json.dumps({'snapshot': self.snapshot['id']}).encode(), blob_id=True)
        self.assertEqual(self.store.queries(), [])
        self.assertEqual(self.delete(self.a)['unreadable_queries_kept'], 1)


    def test_rows_the_list_cannot_show_are_left_out_and_counted_however_old(self):
        for raw in [b'{"snapshot":"s"}', b'{"snapshot":"s","query":5}', b'{"snapshot":"s","query":{"action":3}}',
                    b'{"snapshot":"s","query":{"action":"\\ud800"}}', BAD,
                    b'{"snapshot":"s","query":{"action":"direct","x":1}}', b'{"snapshot":"s","query":{"action":"direct","subject":NaN}}',
                    b'{"snapshot":"s","query":{"action":"direct"},"n":Infinity}', b'{"snapshot":"s","query":{"action":"direct"},"n":1e999}',
                    b'{"snapshot":"s","query":{"action":"direct"}}',
                    json.dumps(result('s') | {'proofs': [SECRET]}).encode(), json.dumps(result('s') | {'rows': ['x']}).encode(),
                    json.dumps(result('s') | {'facts': [1]}).encode(),
                    json.dumps(result('s') | {'proofs': [{'row': ['x'], 'fact_ids': []}]}).encode(),
                    json.dumps(result('s') | {'facts': [{'id': SECRET}]}).encode()]:
            self.put('queries', ident(), raw)
        for _ in range(31):
            self.query(self.snapshot['id'])
        self.assertEqual(len(self.store.queries()), 30)
        self.assertEqual(self.store.unreadable_queries(), {'count': 15})


class Snapshots(Fixture):
    def other(self, payload, key=None, blob_id=False):
        key = key or ident()
        self.put('snapshots', key, payload, blob_id)
        return key

    def test_unreadable_snapshot_shapes_do_not_fail_delete_and_are_kept_with_their_rows(self):
        a = self.a['id']
        shapes = {
            'S1': b'not json', 'S2': BAD, 'S3': b'{"id":"x"}', 'S4': b'{"documents":[1]}',
            'S5': b'{"documents":[{"current_version":1}]}',
            'S6': json.dumps({'documents': [{'id': a}]}).encode(),
        }
        ids = {name: self.other(raw) for name, raw in shapes.items()}
        question_like = [self.query(key) for key in ids.values()]
        result = self.delete(self.a)
        self.assertEqual(result['unreadable_snapshots_kept'], len(shapes))
        self.assertEqual(result['queries_kept_with_snapshot'], len(question_like))
        self.assertEqual(result['removed_snapshots'], 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_s6_blocks_only_its_own_document(self):
        self.other(json.dumps({'documents': [{'id': self.a['id']}, {'id': self.b['id'], 'current_version': 1}]}).encode())
        result = self.delete(self.b)
        self.assertEqual(result['unreadable_snapshots_kept'], 0)
        self.assertEqual(result['removed_snapshots'], 2)

    def test_blob_snapshot_id_keeps_queries_that_name_it(self):
        self.other(json.dumps({'documents': [{'id': self.a['id'], 'current_version': 1}]}).encode(), key=b'blobsnap', blob_id=True)
        kept = self.query('blobsnap')
        removed = self.query(self.snapshot['id'])
        result = self.delete(self.a)
        self.assertEqual((result['unreadable_snapshots_kept'], result['queries_kept_with_snapshot'], result['removed_queries']), (1, 1, 1))
        with self.store.connect() as db:
            ids = {r[0] for r in db.execute('SELECT id FROM queries')}
        self.assertEqual(ids, {kept})
        self.assertNotIn(removed, ids)

    def test_text_snapshot_sharing_bytes_with_a_blob_snapshot_keeps_the_rows_naming_it(self):
        self.other(b'not json', key=self.snapshot['id'].encode(), blob_id=True)
        kept = self.query(self.snapshot['id'])
        result = self.delete(self.a)
        self.assertEqual((result['removed_snapshots'], result['removed_queries'], result['queries_kept_with_snapshot']), (1, 0, 1))
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM queries')], [kept])

    def test_surviving_text_snapshot_with_a_blob_twin_still_counts_the_rows_naming_it(self):
        self.other(b'not json', key=self.snapshot['id'].encode(), blob_id=True)
        self.query(self.snapshot['id'])
        result = self.delete(self.b)
        self.assertEqual((result['removed_snapshots'], result['removed_queries'], result['queries_kept_with_snapshot']), (1, 0, 1))

    def test_corrupt_snapshot_whose_values_only_compare_equal_is_rewritten(self):
        with self.store.connect(True) as db:
            payload = db.execute('SELECT payload FROM snapshots WHERE id=?', (self.snapshot['id'],)).fetchone()[0]
            self.assertIn('"knowledge_version":1', payload)
            db.execute('UPDATE snapshots SET payload=? WHERE id=?', (payload.replace('"knowledge_version":1', '"knowledge_version":true'), self.snapshot['id']))
        self.store.snapshot()
        with self.store.connect() as db:
            self.assertIn('"knowledge_version":1', db.execute('SELECT payload FROM snapshots WHERE id=?', (self.snapshot['id'],)).fetchone()[0])

    def test_corrupt_snapshot_with_the_current_id_is_rewritten_not_bound(self):
        with self.store.connect(True) as db:
            db.execute('UPDATE snapshots SET payload=? WHERE id=?', ('not json', self.snapshot['id']))
        with self.assertRaises(ValueError) as caught:
            self.store.snapshot(self.snapshot['id'])
        self.assertNotIn('not json', str(caught.exception))
        self.assertEqual(self.store.snapshot()['id'], self.snapshot['id'])
        self.assertEqual(self.store.snapshot(self.snapshot['id'])['id'], self.snapshot['id'])
        self.assertEqual(self.delete(self.a)['unreadable_snapshots_kept'], 0)


if __name__ == '__main__':
    unittest.main()
