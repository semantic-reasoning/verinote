"""Synthetic calls only: registering a new source version of a document while a run on it is queued or running."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_ontology import wire_dictionary
from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

from verinote.extraction_transport import ExtractionSpec
from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, Store

JOB = {'regenerate': '사전 재생성', 'policy_extract': '병렬 수나 청크 임계 정책을 등록한 관계 추출'}


class Gate:
    """Blocks every model call until released; answers by the stage of the run that owns the folder."""

    def __init__(self):
        self.entered, self.release, self.calls, self.stage = threading.Event(), threading.Event(), [], {}
        self.extraction_spec = ExtractionSpec('/fake/claude', 'model', 1, 10, 1048576, 'null')

    def limited_timeout(self, agent, seconds): return self
    def close(self): pass

    def complete(self, agent, model, prompt, folder, **kw):
        self.calls.append(str(folder))
        self.entered.set()
        if not self.release.wait(20): raise TimeoutError('gate')
        if any(run in str(folder) and stage == 'ontology' for run, stage in self.stage.items()):
            return wire_dictionary(DICTIONARY, prompt)
        return extraction_response()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gates = []
        self.services = []

    def tearDown(self):
        for gate in self.gates: gate.release.set()
        for service in self.services:
            service.pool.shutdown(wait=True)
            service.close()
        self.temp.cleanup()

    def held(self, kind, queued=False):
        """A run of `kind` (first_dictionary, regenerate, extract, policy_extract) held in its first model call, or queued behind another document's run."""
        store, gate = Store(self.root / 'db'), Gate()
        service = Service(store, self.root, gate, extraction_caller=gate if kind == 'policy_extract' else None)
        self.gates.append(gate); self.services.append(service)
        doc = store.register('d', 'x.md', SOURCE.encode(), SOURCE)
        if queued:
            other = store.register('o', 'o.md', SOURCE.encode(), SOURCE)
            approved_dictionary(store, other['id'])
            service.start(other['id'], 'agy', '', 'extract', 1, 1)
            self.assertTrue(gate.entered.wait(10))
        kw = {}
        if kind == 'first_dictionary':
            stage = 'ontology'
        elif kind == 'regenerate':
            item = approved_dictionary(store, doc['id'])
            store.review(item['id'], item['revision'], 'rejected', 'R', 'x')
            item = next(i for i in store.document(doc['id'])['items'] if i['kind'] == 'ontology')
            stage, kw['regenerate'] = 'ontology', {'id': item['id'], 'revision': item['revision']}
        else:
            approved_dictionary(store, doc['id'])
            stage = 'extract'
            if kind == 'policy_extract': kw['extraction_policy'] = 'chunk_threshold_v1'
        current = store.document(doc['id'])
        with patch('verinote.extraction_transport.executable', return_value=sys.executable):
            run = service.start(doc['id'], 'claude' if kind == 'policy_extract' else 'agy', 'model' if kind == 'policy_extract' else '', stage, current['version'], current['generation'], **kw)
        gate.stage[run['id']] = stage
        if not queued: self.assertTrue(gate.entered.wait(10))
        return store, service, gate, doc, run

    def finish(self, service, gate):
        gate.release.set()
        service.pool.shutdown(wait=True)

    def serve(self, store):
        server = Server(('127.0.0.1', 0), Store(store.path), self.services[-1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post_document(self, server, doc_id, version=1, generation=1):
        body = {'title': 'd', 'filename': 'y.md', 'text': 'new', 'document_id': doc_id, 'expected_version': version, 'expected_generation': generation}
        request = Request(f'http://127.0.0.1:{server.server_port}/api/documents', method='POST', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error:
            with error: return error.code, json.loads(error.read())

    def jump(self, store, doc_id, mode):
        """A document change that does not go through register: a new current version, or only a generation bump."""
        with store.connect(True) as db:
            if mode == 'version':
                db.execute('INSERT INTO versions SELECT document_id,2,filename,text,raw,sha256,created_at FROM versions WHERE document_id=?', (doc_id,))
                db.execute('UPDATE documents SET current_version=2,next_version=3,generation=generation+1 WHERE id=?', (doc_id,))
            else:
                db.execute('UPDATE documents SET generation=generation+2 WHERE id=?', (doc_id,))


class RegisterDuringRun(Base):
    def test_register_refused_while_regeneration_or_policy_extract_is_queued_or_running(self):
        for kind in ('regenerate', 'policy_extract'):
            for queued in (False, True):
                with self.subTest(kind=kind, queued=queued):
                    self.tearDown(); self.setUp()
                    try:
                        store, service, gate, doc, run = self.held(kind, queued)
                        state = '대기 중' if queued else '처리 중'
                        self.assertEqual(store.run(run['id'])['status'], 'queued' if queued else 'running')
                        code, body = self.post_document(self.serve(store), doc['id'])
                        agent = 'claude / model' if kind == 'policy_extract' else 'agy / 기본 모델'
                        waits = '대기 중인 실행은 앞서 등록된 다른 문서의 실행이나 질문이 끝나야 시작합니다. ' if queued else ''
                        self.assertEqual(code, 409, body)
                        self.assertEqual(body['error'],
                                         f"이 문서의 {JOB[kind]} 실행 {run['id']}({agent} · {state})이 끝나지 않아 새 원문 버전을 등록할 수 없습니다. "
                                         '이 실행은 요청된 때부터 끝날 때까지 이 문서에 원문 버전 등록이나 삭제가 없어야 결과를 저장하므로, 지금 등록하면 호출 비용을 쓴 결과를 저장하지 못합니다. '
                                         f'{waits}실행 기록에서 끝난 것을 확인한 뒤 다시 등록하세요. '
                                         '실행이 멈춰 끝나지 않을 때만 서버를 재시작하세요. 재시작하면 대기·처리 중인 모든 실행과 질문이 중단으로 표시됩니다.')
                        self.assertEqual(store.document(doc['id'])['version'], 1)
                        self.finish(service, gate)
                        final = store.run(run['id'])
                        self.assertEqual(final['status'], 'success', final['detail'])
                        self.assertTrue(any(i['run_id'] == run['id'] for i in store.document(doc['id'], 1)['items']))
                    finally:
                        self.tearDown()

    def test_stale_token_is_reported_before_the_active_run_refusal(self):
        # A stale view is refused with the refresh message first: the run named by the run refusal may already be over for a reader who refreshed.
        for kind in ('regenerate', 'policy_extract'):
            with self.subTest(kind=kind):
                self.tearDown(); self.setUp()
                store, service, gate, doc, run = self.held(kind)
                with self.assertRaises(Conflict) as stale:
                    Store(store.path).register('d', 'y.md', b'new', 'new', doc['id'], 1, 2)
                self.assertEqual(str(stale.exception), '문서가 변경되었습니다. 새로고침 후 다시 선택하세요.')
                with self.assertRaises(Conflict) as current:
                    Store(store.path).register('d', 'y.md', b'new', 'new', doc['id'], 1, 1)
                self.assertIn(run['id'], str(current.exception))
                self.finish(service, gate)

    def test_register_accepted_during_first_dictionary_and_plain_extract(self):
        for kind in ('first_dictionary', 'extract'):
            for queued in (False, True):
                with self.subTest(kind=kind, queued=queued):
                    self.tearDown(); self.setUp()
                    try:
                        store, service, gate, doc, run = self.held(kind, queued)
                        self.assertEqual(Store(store.path).register('d', 'y.md', b'new', 'new', doc['id'], 1, 1)['version'], 2)
                        self.finish(service, gate)
                        self.assertEqual(store.run(run['id'])['status'], 'success')
                    finally:
                        self.tearDown()

    def test_register_of_another_document_accepted_while_this_one_regenerates(self):
        store, service, gate, doc, run = self.held('regenerate')
        other = store.register('b', 'b.md', b'b', 'b')
        self.assertEqual(Store(store.path).register('b', 'b2.md', b'b2', 'b2', other['id'], 1, 1)['version'], 2)
        self.finish(service, gate)
        self.assertEqual(store.run(run['id'])['status'], 'success')

    def regeneration_target(self, store, doc):
        item = approved_dictionary(store, doc['id'])
        store.review(item['id'], item['revision'], 'rejected', 'R', 'x')
        item = next(i for i in store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        return {'id': item['id'], 'revision': item['revision']}

    def test_run_created_just_before_register_writes_is_seen(self):
        first, second = Store(self.root / 'db'), Store(self.root / 'db')
        doc = first.register('d', 'x.md', SOURCE.encode(), SOURCE)
        target = self.regeneration_target(first, doc)
        real, fired = Store.connect, []

        def connect(store, write=False):
            if store is second and write and not fired:
                fired.append(1)
                first.create_run(doc['id'], 'agy', '', 'ontology', 1, 1, regenerate=target)
            return real(store, write)
        with patch.object(Store, 'connect', connect), self.assertRaises(Conflict) as refused:
            second.register('d', 'y.md', b'n', 'n', doc['id'], 1, 1)
        self.assertEqual(fired, [1])
        self.assertIn('새 원문 버전을 등록할 수 없습니다.', str(refused.exception))
        self.assertEqual(first.document(doc['id'])['version'], 1)

    def test_start_with_token_read_before_register_is_refused(self):
        first, second = Store(self.root / 'db'), Store(self.root / 'db')
        doc = first.register('d', 'x.md', SOURCE.encode(), SOURCE)
        target = self.regeneration_target(first, doc)
        second.register('d', 'y.md', b'n', 'n', doc['id'], 1, 1)
        with self.assertRaises(Conflict):
            first.create_run(doc['id'], 'agy', '', 'ontology', 1, 1, regenerate=target)
        with first.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs WHERE document_id=? AND status IN ('queued','running')", (doc['id'],)).fetchone()[0], 0)

    def test_publish_after_sql_version_change_names_current_version(self):
        for kind in ('regenerate', 'policy_extract'):
            with self.subTest(kind=kind):
                self.tearDown(); self.setUp()
                try:
                    store, service, gate, doc, run = self.held(kind)
                    self.jump(store, doc['id'], 'version')
                    self.finish(service, gate)
                    final = store.run(run['id'])
                    self.assertEqual(final['status'], 'error')
                    self.assertIn('실행이 요청된 뒤 이 문서의 현재 원문 버전이 바뀌어(실행 버전 1, 현재 버전 2) 결과를 저장하지 않았습니다. '
                                  '새 실행은 현재 버전에서만 시작하므로 버전 1에서는 다시 실행할 수 없습니다. 현재 버전에서 사전부터 다시 진행하세요.', final['detail'])
                    self.assertEqual(sum(i['run_id'] == run['id'] for i in store.document(doc['id'], 1)['items']), 0)
                finally:
                    self.tearDown()

    def test_publish_after_sql_generation_change_names_generations(self):
        for kind in ('regenerate', 'policy_extract'):
            with self.subTest(kind=kind):
                self.tearDown(); self.setUp()
                try:
                    store, service, gate, doc, run = self.held(kind)
                    self.jump(store, doc['id'], 'generation')
                    self.finish(service, gate)
                    final = store.run(run['id'])
                    self.assertEqual(final['status'], 'error')
                    self.assertIn('실행이 요청된 뒤 이 문서의 원문 버전 기록이 바뀌어(문서 세대 1 → 3) 결과를 저장하지 않았습니다. '
                                  '현재 버전이 실행 버전과 같으므로 같은 버전에서 다시 실행할 수 있습니다.', final['detail'])
                    self.assertNotIn('다시 추출하세요', final['detail'])
                    current = store.document(doc['id'])
                    if kind == 'regenerate':
                        item = next(i for i in current['items'] if i['kind'] == 'ontology')
                        rerun = store.create_run(doc['id'], 'agy', '', 'ontology', current['version'], current['generation'], regenerate={'id': item['id'], 'revision': item['revision']})
                    else:
                        rerun = store.create_run(doc['id'], 'agy', '', 'extract', current['version'], current['generation'])
                    self.assertEqual(rerun['status'], 'queued')
                finally:
                    self.tearDown()

    def test_missing_document_row_names_the_deletion(self):
        store, service, gate, doc, run = self.held('regenerate')
        with store.connect(True) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('DELETE FROM documents WHERE id=?', (doc['id'],))
        self.finish(service, gate)
        self.assertEqual(store.run(run['id'])['detail'], '실행이 요청된 뒤 문서가 삭제되어 결과를 저장하지 않았습니다.')

    def test_needs_unchanged_document_on_four_run_kinds(self):
        for kind, expected in (('first_dictionary', False), ('regenerate', True), ('extract', False), ('policy_extract', True)):
            with self.subTest(kind=kind):
                self.tearDown(); self.setUp()
                try:
                    store, service, gate, doc, run = self.held(kind)
                    with store.connect() as db:
                        row = db.execute('SELECT * FROM runs WHERE id=?', (run['id'],)).fetchone()
                    self.assertEqual(Store.needs_unchanged_document(row), expected)
                    self.assertEqual(Store.needs_unchanged_document(store.run(run['id'])), expected)
                    self.finish(service, gate)
                finally:
                    self.tearDown()


if __name__ == '__main__':
    unittest.main()
