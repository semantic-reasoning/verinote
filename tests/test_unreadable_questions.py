"""A question row startup recovery skips is classified by every API reader with recover's own computation (question_job): it is never read
as in progress, does not block or join document deletion, is listed on its own, and only the user-initiated delete-unreadable route removes
it. No response carries its stored text or the data directory (#914)."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_recover_corrupt_rows import insert

from verinote.server import Server
from verinote.service import Service
from verinote.store import Conflict, Store, UnreadableQuestion, ident

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'SECRET914'
ROUND = [{'round': 1, 'status': 'running', 'nodes': []}]
DETAIL = '손상되어 읽을 수 없는 질문 기록입니다. 내용을 추측해 고치지 않으므로 질문·상태·답변을 표시하지 않습니다.'
CANCEL = '손상되어 읽을 수 없는 질문 기록이라 취소하지 않았습니다.'
READABLE_DELETE = '읽을 수 있는 질문 기록은 삭제하지 않습니다. 손상되어 읽을 수 없는 기록만 삭제할 수 있습니다.'
DB_ERROR = '처리 실패: 데이터베이스 오류 OperationalError (코드 미확인). 저장된 내용은 오류 문구에 표시하지 않습니다.'
WAITING = '추출 또는 질문 작업이 진행 중입니다. 모든 작업 완료 후 삭제하세요.'
QUESTION_CANCELLING = ('취소한 질문이 아직 멈추지 않았습니다. 질문 이력에서 취소됨으로 바뀐 뒤 삭제하세요. 이미 허용한 모델 호출의 제한 시간이 지나도 '
                       '바뀌지 않으면 서버를 재시작하세요. 재시작하면 대기·처리 중인 실행과 질문은 중단으로, 취소 중인 실행과 질문은 취소됨으로 표시됩니다.')

# The issue's five shapes; each holds SECRET.
SHAPES = {
    'running_round_x': json.dumps({'status': 'running', 'question': SECRET, 'context_processing': ['x']}),
    'not_json': '{"question":"%s"' % SECRET,
    'no_status': json.dumps({'question': SECRET, 'agent': 'agy', 'snapshot': 'x'}),
    'invalid_utf8': b'{"status":"running","question":"' + SECRET.encode() + b'\xff"}',
    'cancelling_round_x': json.dumps({'status': 'cancelling', 'question': SECRET, 'context_processing': ['x']}),
}

# name -> (payload, expected unreadable). Expected values are written here by hand, not derived from the code under test.
ALL_SHAPES = {
    'not_json': (b'not json{', True), 'no_status': (json.dumps({'id': 'x', 'question': 'q'}), True),
    'cancelled_str_round': (json.dumps({'status': 'cancelled', 'context_processing': ['x']}), True), 'null': (None, True),
    'array': (b'[]', True), 'bad_utf8': (b'{"status":"running","q":"\xff"}', True),
    'utf8_surrogate': (b'{"status":"running","q":"\xed\xa0\x80"}', True), 'bom': (b'\xef\xbb\xbf{"status":"running"}', True),
    'escaped_surrogate_running': (b'{"status":"running","q":"\\ud800"}', True), 'deep': (b'[' * 200000, True),
    'success_utf8_surrogate': (b'{"status":"success","q":"\xed\xa0\x80"}', True),
    'running_str_round': (json.dumps({'status': 'running', 'context_processing': ['x']}), True),
    'cancelling_str_round': (json.dumps({'status': 'cancelling', 'context_processing': ['x']}), True),
    'running_str_cp': (json.dumps({'status': 'running', 'context_processing': 'ab'}), True),
    'status_list': (json.dumps({'status': []}), True), 'status_dict': (json.dumps({'status': {}}), True),
    'pairs_running': (json.dumps([['status', 'running']]), True), 'pairs_success': (json.dumps([['status', 'success']]), True),
    'success_str_round': (json.dumps({'status': 'success', 'context_processing': ['x']}), False),
    'escaped_surrogate_success': (b'{"status":"success","q":"\\ud800"}', False),
    'live_running': (json.dumps({'status': 'running', 'context_processing': ROUND}), False),
    'live_queued': (json.dumps({'status': 'queued'}), False), 'status_int': (json.dumps({'status': 5}), False),
}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / SECRET
        self.store = Store(self.root / 'verinote.sqlite3')
        self.doc = self.store.register('문서', 'a.md', b'source', 'source')
        self.snapshot = self.store.snapshot()

    def question(self, status):
        job = self.store.create_question('readable', 'agy', '', self.snapshot['id'])
        if status != 'queued':
            job['status'] = status
            self.store.save_question(job)
        return job

    def corrupt(self, payload, key=None, created_at='2026-01-01'):
        key = key or ident()
        with self.store.connect(True) as db:
            if created_at == '2026-01-01':
                insert(db, key, payload)
            else:
                db.execute('INSERT INTO questions VALUES(?,CAST(? AS TEXT),?)' if isinstance(payload, bytes) else 'INSERT INTO questions VALUES(?,?,?)',
                           (key, payload, created_at))
        return key

    def count_writes(self):
        with self.store.connect(True) as db:
            db.execute('CREATE TABLE writes(id TEXT)')
            db.execute('CREATE TRIGGER counted AFTER UPDATE ON questions BEGIN INSERT INTO writes VALUES(NEW.id); END')

    def writes(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute('SELECT id FROM writes')]

    def raw(self, key):
        with self.store.connect() as db:
            row = db.execute('SELECT CAST(payload AS BLOB) FROM questions WHERE id=?', (key,)).fetchone()
            return row and row[0]

    def delete(self):
        doc = self.store.document(self.doc['id'])
        return self.store.delete_document(doc['id'], False, doc['current_version'], doc['generation'])


class Classification(Base):
    def test_rows_recover_skips_equal_unreadable_questions_ids_and_question_raising_over_23_shapes(self):
        ids = {name: self.corrupt(payload) for name, (payload, _) in ALL_SHAPES.items()}
        self.assertEqual(len(ids), 23)
        expected = {ids[name] for name, (_, unreadable) in ALL_SHAPES.items() if unreadable}
        self.count_writes()
        skipped = self.store.recover()
        self.assertEqual(set(skipped), expected)
        self.assertEqual(set(self.store.unreadable_questions()['ids']), expected)
        raising = set()
        for key in ids.values():
            try:
                self.store.question(key)
            except UnreadableQuestion:
                raising.add(key)
        self.assertEqual(raising, expected)
        self.assertEqual(sorted(self.writes()), sorted([ids['live_running'], ids['live_queued']]))

    def test_questions_list_leaves_unreadable_rows_out_and_still_returns_30_readable(self):
        readable = []
        for i in range(31):
            key = ident()
            with self.store.connect(True) as db:
                db.execute('INSERT INTO questions VALUES(?,?,?)', (key, json.dumps({'id': key, 'status': 'success'}), f'2026-02-{i:02d}'))
            readable.append(key)
        for payload in SHAPES.values():
            self.corrupt(payload, created_at='2026-12-31')
        listed = self.store.questions()
        self.assertEqual([q['id'] for q in listed], readable[::-1][:30])

    def test_an_unreadable_row_older_than_31_readable_questions_and_one_with_null_created_at_are_listed(self):
        old = self.corrupt(SHAPES['not_json'], created_at='2000-01-01')
        null = self.corrupt(SHAPES['running_round_x'], created_at=None)
        for i in range(31):
            key = ident()
            with self.store.connect(True) as db:
                db.execute('INSERT INTO questions VALUES(?,?,?)', (key, json.dumps({'id': key, 'status': 'success'}), f'2026-02-{i:02d}'))
        self.assertEqual(self.store.unreadable_questions(), {'ids': [old, null], 'hidden': 0})

    def test_an_unreadable_row_whose_id_is_not_32_lowercase_hex_text_is_counted_in_hidden_without_its_id(self):
        self.corrupt(SHAPES['invalid_utf8'], key=SECRET)
        self.corrupt(SHAPES['not_json'], key='A' * 32)
        blob = ident()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO questions VALUES(CAST(? AS BLOB),?,?)', (blob, 'not json{', '2026-01-01'))
        with self.assertRaises(ValueError):
            self.store.delete_unreadable_question(blob)
        listed = self.store.unreadable_questions()
        self.assertEqual(listed, {'ids': [], 'hidden': 3})
        self.assertNotIn(SECRET, json.dumps(listed))
        self.assertNotIn('A' * 32, json.dumps(listed))

    def test_issue_repro_a_running_row_with_round_x_cannot_be_cancelled_stays_skipped_and_listed(self):
        key = self.corrupt(SHAPES['running_round_x'])
        self.assertEqual(self.store.recover(), [key])
        with self.assertRaises(UnreadableQuestion):
            self.store.cancel_question(key, 'running')
        self.assertEqual(self.store.recover(), [key])
        with self.assertRaises(UnreadableQuestion):
            self.store.question(key)
        self.assertEqual(self.store.unreadable_questions()['ids'], [key])

    def test_question_detail_raises_with_the_exact_text_for_each_shape(self):
        for name, payload in SHAPES.items():
            with self.subTest(name):
                key = self.corrupt(payload)
                with self.assertRaises(UnreadableQuestion) as raised:
                    self.store.question(key)
                self.assertEqual(str(raised.exception), DETAIL)

    def test_cancel_raises_with_the_exact_text_before_any_write_for_each_shape(self):
        keys = {name: self.corrupt(payload) for name, payload in SHAPES.items()}
        before = {key: self.raw(key) for key in keys.values()}
        self.count_writes()
        for name, key in keys.items():
            with self.subTest(name):
                with self.assertRaises(UnreadableQuestion) as raised:
                    self.store.cancel_question(key, 'running')
                self.assertEqual(str(raised.exception), CANCEL)
        self.assertEqual(self.writes(), [])
        self.assertEqual({key: self.raw(key) for key in keys.values()}, before)

    def test_queue_rows_shows_each_shape_as_unreadable_without_its_text(self):
        for name, payload in SHAPES.items():
            with self.subTest(name):
                key = self.corrupt(payload)
                rows = self.store.queue_rows([('question', key)])
                self.assertEqual(rows, [{'kind': 'question', 'id': key, 'status': None, 'unreadable': True, 'question': ''}])
                self.assertNotIn(SECRET, json.dumps(rows))

    def test_usage_report_leaves_an_unreadable_row_out_but_counts_it_in_total_jobs(self):
        readable = self.question('success')
        self.corrupt(SHAPES['no_status'])
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        report = service.usage_report()
        self.assertEqual((report['included_jobs'], report['total_jobs'], report['truncated']), (1, 2, True))
        owners, total = self.store.usage_owners()
        self.assertEqual(([o['id'] for o in owners], total), ([readable['id']], 2))


class DocumentDeletion(Base):
    def test_deletion_succeeds_with_each_shape_and_keeps_the_unreadable_row_unwritten(self):
        for name, payload in SHAPES.items():
            with self.subTest(name):
                self.setUp()
                readable = self.question('success')
                key = self.corrupt(payload)
                before = self.raw(key)
                self.count_writes()
                result = self.delete()
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept']), (1, 1))
                self.assertEqual(self.raw(key), before)
                self.assertIsNone(self.raw(readable['id']))
                self.assertEqual(self.writes(), [])

    def test_unreadable_questions_kept_counts_every_unreadable_row_in_the_table(self):
        for count in (0, 2):
            with self.subTest(count):
                self.setUp()
                self.question('success')
                for name in list(SHAPES)[:count]:
                    self.corrupt(SHAPES[name])
                result = self.delete()
                self.assertEqual((result['removed_questions'], result['unreadable_questions_kept']), (1, count))

    def test_a_readable_queued_question_still_refuses_deletion_next_to_an_unreadable_row(self):
        self.corrupt(SHAPES['running_round_x'])
        self.question('queued')
        with self.assertRaises(Conflict) as refused:
            self.delete()
        self.assertEqual(str(refused.exception), WAITING)

    def test_a_readable_cancelling_question_still_refuses_deletion_next_to_an_unreadable_row(self):
        self.corrupt(SHAPES['cancelling_round_x'])
        job = self.question('running')
        self.store.cancel_question(job['id'], 'running')
        with self.assertRaises(Conflict) as refused:
            self.delete()
        self.assertEqual(str(refused.exception), QUESTION_CANCELLING)


class Routes(Base):
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
                body = response.read().decode()
                status = response.status
        except HTTPError as error:
            with error:
                body = error.read().decode()
                status = error.code
        self.assertNotIn(SECRET, body)
        self.assertNotIn(str(self.root.parent), body)
        return status, json.loads(body)

    def test_each_route_answers_each_shape_without_its_text_or_the_data_dir(self):
        for name, payload in SHAPES.items():
            with self.subTest(name):
                readable = self.question('success')
                key = self.corrupt(payload)
                status, rows = self.call('/api/questions')
                self.assertEqual((status, [r['id'] for r in rows]), (200, [readable['id']]))
                self.assertEqual(self.call('/api/questions/unreadable'), (200, {'ids': [key], 'hidden': 0}))
                self.assertEqual(self.call(f'/api/questions/{key}'), (409, {'error': DETAIL}))
                self.assertEqual(self.call(f'/api/questions/{key}/cancel', 'POST', {'expected_status': 'running'}), (409, {'error': CANCEL}))
                status, usage = self.call('/api/usage')
                self.assertEqual((status, usage['included_jobs'], usage['total_jobs']), (200, 1, 2))
                doc = self.store.document(self.doc['id'])
                status, result = self.call(f"/api/documents/{doc['id']}/delete", 'POST',
                                           {'expected_version': doc['current_version'], 'expected_generation': doc['generation']})
                self.assertEqual((status, result['unreadable_questions_kept']), (200, 1))
                with self.store.connect(True) as db:
                    db.execute('DELETE FROM questions')
                self.doc = self.store.register('문서', 'a.md', b'source', 'source')
                self.snapshot = self.store.snapshot()

    def test_document_delete_with_an_invalid_utf8_queries_row_returns_500_without_stored_text(self):
        with self.store.connect(True) as db:
            db.execute('INSERT INTO queries VALUES(?,CAST(? AS TEXT),?)', (ident(), b'{"snapshot":"' + SECRET.encode() + b'\xff"}', 'c'))
        doc = self.store.document(self.doc['id'])
        status, body = self.call(f"/api/documents/{doc['id']}/delete", 'POST',
                                 {'expected_version': doc['current_version'], 'expected_generation': doc['generation']})
        self.assertEqual((status, body), (500, {'error': DB_ERROR}))

    def test_a_route_raising_a_sqlite_error_that_quotes_stored_text_answers_500_without_it(self):
        with patch.object(self.store, 'documents', side_effect=sqlite3.OperationalError(f"Could not decode to UTF-8 column 'x' with text '{SECRET}'")):
            self.assertEqual(self.call('/api/documents'), (500, {'error': DB_ERROR}))

    def test_delete_unreadable_removes_the_row_and_its_logs_and_refuses_everything_else(self):
        key = self.corrupt(SHAPES['invalid_utf8'])
        logs = self.root / 'questions' / key
        logs.mkdir(parents=True)
        (logs / 'prompt.txt').write_text(SECRET)
        readable = self.question('success')
        before = self.raw(readable['id'])
        self.assertEqual(self.call(f"/api/questions/{readable['id']}/delete-unreadable", 'POST', {}), (409, {'error': READABLE_DELETE}))
        self.assertEqual(self.raw(readable['id']), before)
        self.assertEqual(self.call(f'/api/questions/{key}/delete-unreadable', 'POST', {'x': 1}),
                         (400, {'error': '손상된 질문 기록 삭제에는 빈 JSON 객체를 보내세요.'}))
        self.assertEqual(self.call(f'/api/questions/{ident()}/delete-unreadable', 'POST', {}), (400, {'error': '질문을 찾을 수 없습니다.'}))
        self.assertIsNotNone(self.raw(key))
        status, result = self.call(f'/api/questions/{key}/delete-unreadable', 'POST', {})
        self.assertEqual((status, result['cleanup_pending']), (200, 0))
        self.assertIsNone(self.raw(key))
        self.assertFalse(logs.exists())
        self.assertEqual(self.store.pending_cleanup(), [])

    def test_delete_unreadable_refuses_an_id_that_is_not_32_lowercase_hex_and_listing_hides_it(self):
        for key in (SECRET, 'A' * 32):
            with self.subTest(key):
                self.corrupt(SHAPES['not_json'], key=key)
                self.assertEqual(self.call(f'/api/questions/{key}/delete-unreadable', 'POST', {}), (400, {'error': '질문 ID 형식이 맞지 않습니다.'}))
                self.assertIsNotNone(self.raw(key))
        status, listed = self.call('/api/questions/unreadable')
        self.assertEqual((status, listed), (200, {'ids': [], 'hidden': 2}))
        self.assertNotIn('A' * 32, json.dumps(listed))


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser logic tests.')
class Interface(unittest.TestCase):
    def run_js(self, body):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const ctx=vm.createContext({});
vm.runInContext(source.match(/^const esc = .*$/m)[0]+';'+source.match(/^const labels = .*$/m)[0],ctx);
vm.runInContext(source.slice(source.indexOf('function renderQueueAhead('),source.indexOf('function renderAnalysisProgress(')),ctx);
vm.runInContext(source.slice(source.indexOf('function deleteNotice('),source.indexOf('let unreadableTarget;')),ctx);
''' + body + "\nconsole.log('ok');"
        self.assertIn('ok', subprocess.check_output(['node', '-e', script], text=True, cwd=ROOT))

    def test_the_unreadable_section_the_delete_notice_and_the_queue_item(self):
        a = 'a' * 32
        self.run_js(r'''
const html=vm.runInContext('renderUnreadableQuestions({ids:["%s"],hidden:1})',ctx);
assert(html.includes('<h3>손상된 질문 기록 2개</h3>'),html);
assert(html.includes('저장된 질문 기록이 손상되어 읽을 수 없습니다. 내용을 추측해 고치지 않으므로 질문·상태·답변을 표시하지 않습니다. 이 기록은 문서를 삭제해도 함께 지워지지 않으며 삭제한 문서의 내용이 들어 있을 수 있습니다. ID가 표시된 기록은 문서 삭제를 막지 않습니다.'),html);
assert(html.includes('질문 기록 %s <button class="danger" data-delete-unreadable="%s">삭제</button>'),html);
assert(html.includes('ID 형식이 맞지 않는 손상 기록 1개는 화면에서 삭제할 수 없습니다.'),html);
assert.equal(vm.runInContext('renderUnreadableQuestions({ids:[],hidden:0})',ctx),'');
assert.equal(vm.runInContext('deleteNotice({cleanup_pending:0,unreadable_questions_kept:2},false)',ctx),
  '문서와 모든 리비전을 삭제했습니다. 읽을 수 없는 질문 기록 2개는 이 문서와 관련 있는지 확인할 수 없어 지우지 않았습니다. 질문 이력의 손상된 질문 기록 목록에서 확인하세요.');
assert.equal(vm.runInContext('deleteNotice({cleanup_pending:0,unreadable_questions_kept:0},true)',ctx),'최신 리비전을 삭제하고 이전 리비전으로 돌아갔습니다.');
const queue=vm.runInContext('renderQueueAhead({status:"queued",stage:"question",queue:{stage:"question",capacity:1,ahead:[{kind:"question",id:"%s",status:null,unreadable:true,question:""}]}})',ctx);
assert(queue.includes('질문 %s · 손상되어 읽을 수 없음'),queue);
''' % (a, a, a, a, a))

    def test_the_confirm_handler_closes_and_reloads_on_400_and_409_and_keeps_the_dialog_open_on_500(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
(async()=>{
  for(const [status,closed] of [[400,true],[409,true],[500,false]]){
    const nodes={},log=[];
    const $=s=>nodes[s]||(nodes[s]={textContent:'',disabled:false,close:()=>log.push('close'),showModal:()=>{}});
    let pending;
    const ctx=vm.createContext({$,unreadableTarget:'a'.repeat(32),attempt:fn=>{pending=fn();},notice:m=>log.push('notice:'+m),
      loadUnreadableQuestions:async()=>log.push('reload'),loadCleanup:async()=>log.push('cleanup'),
      api:async()=>{const e=new Error('질문을 찾을 수 없습니다.');e.status=status;throw e;}});
    vm.runInContext(source.slice(source.indexOf("$('#confirm-unreadable').onclick="),source.indexOf('async function loadCleanup(){')),ctx);
    nodes['#confirm-unreadable'].onclick();await pending;
    if(closed){assert.deepEqual(log,['close','reload','notice:질문을 찾을 수 없습니다.']);assert.equal($('#unreadable-error').textContent,'');}
    else{assert.deepEqual(log,[]);assert.equal($('#unreadable-error').textContent,'질문을 찾을 수 없습니다.');assert.equal(nodes['#confirm-unreadable'].disabled,false);}
  }
  console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.assertIn('ok', subprocess.check_output(['node', '-e', script], text=True, cwd=ROOT))

    def test_the_delete_dialog_names_kept_unreadable_rows_and_the_unreadable_dialog_exists(self):
        html = (ROOT / 'verinote' / 'web' / 'index.html').read_text()
        dialog = html[html.index('<dialog id="delete-dialog"'):]
        dialog = dialog[:dialog.index('</dialog>')]
        self.assertIn('질문 이력의 손상된 질문 기록 목록에 ID가 표시된 기록과 그 로그는 이 문서와 관련 있는지 확인할 수 없어 지우지 않고 수를 알립니다.', dialog)
        unreadable = html[html.index('<dialog id="unreadable-dialog"'):]
        unreadable = unreadable[:unreadable.index('</dialog>')]
        for part in ('이 손상된 질문 기록과 그 로컬 호출 로그를 삭제합니다. 되돌릴 수 없습니다.', 'id="unreadable-error"', 'id="confirm-unreadable"', 'id="cancel-unreadable"'):
            self.assertIn(part, unreadable)
        self.assertIn('<div id="question-history"></div><div id="unreadable-questions"></div>', html)
        self.assertIn('<dialog id="cancel-run-dialog" aria-labelledby="cancel-run-heading">', html)


if __name__ == '__main__':
    unittest.main()
