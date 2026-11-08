"""Synthetic calls only: what a queued run waits for, shown from the in-memory queue list and the stored statuses."""
import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from test_run_cancel import Base, Gate

from verinote.server import Server


class HoldsQuestions(Gate):
    """Blocks the model call of a question until released, then fails it; runs are answered as Gate answers them."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.question_entered, self.question_release = threading.Event(), threading.Event()

    def complete(self, agent, model, prompt, folder, **kw):
        if not Path(folder).name.startswith('llm-'): return super().complete(agent, model, prompt, folder, **kw)
        self.question_entered.set()
        self.question_release.wait(20)
        raise ValueError('stub')


class Queue(Base):
    def documents(self, count):
        return [self.document(1, f'doc{i}') for i in range(count)]

    def ask(self, service, text):
        return service.ask({'question': text, 'agent': 'agy', 'model': ''})

    def test_a_queued_run_lists_the_running_run_and_the_question_ahead_in_order(self):
        first, second = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        question = self.ask(service, 'x' * 60)
        b = self.start(service, second)
        ahead = service.queue_ahead(b['id'])
        self.assertEqual([(entry['kind'], entry['id'], entry['status']) for entry in ahead], [('run', a['id'], 'running'), ('question', question['id'], 'queued')])
        self.assertEqual(ahead[0] | {'status': None}, {'kind': 'run', 'id': a['id'], 'status': None, 'stage': 'extract', 'document_id': first['id'], 'title': 'doc0'})
        self.assertEqual(ahead[1]['question'], 'x' * 40)
        shown = service.with_usage('runs', self.store.run(b['id']))
        self.assertEqual(shown['queue'], {'ahead': ahead})
        self.assertEqual(service.queue_ahead(a['id']), [])
        self.finish(service, gate)

    def test_a_question_in_flight_ahead_of_a_run_shows_the_status_it_has_now(self):
        doc, gate = self.document(1), HoldsQuestions(hold=())
        service = self.service(gate)
        self.addCleanup(gate.question_release.set)
        question = self.ask(service, 'q')
        self.assertTrue(gate.question_entered.wait(5))
        run = self.start(service, doc)
        self.assertEqual(self.store.run(run['id'])['status'], 'queued')
        self.assertEqual([(entry['kind'], entry['id'], entry['status']) for entry in service.queue_ahead(run['id'])], [('question', question['id'], 'running')])
        gate.question_release.set()
        self.finish(service)
        self.assertEqual(self.store.run(run['id'])['status'], 'success')

    def test_a_queued_run_that_is_not_listed_yet_carries_no_queue_instead_of_an_empty_one(self):
        first, second = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        pinned = self.store.document(second['id'])
        created = self.store.create_run(second['id'], 'agy', '', 'extract', pinned['version'], pinned['generation'])
        self.assertEqual(self.store.run(created['id'])['status'], 'queued')
        self.assertIsNone(service.queue_ahead(created['id']))
        self.assertNotIn('queue', service.with_usage('runs', self.store.run(created['id'])))
        self.finish(service, gate)

    def test_only_a_queued_run_carries_the_queue_in_its_answer(self):
        first, second = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)
        self.assertIn('queue', service.with_usage('runs', self.store.run(b['id'])))
        self.assertNotIn('queue', service.with_usage('runs', self.store.run(a['id'])))
        self.finish(service, gate)
        for run in (a, b):
            self.assertNotIn('queue', service.with_usage('runs', self.store.run(run['id'])))
        self.assertEqual(service.queue, [])

    def test_a_cancelled_queued_run_leaves_the_list_so_the_next_run_no_longer_waits_for_it(self):
        first, second, third = self.documents(3)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)
        c = self.start(service, third)
        self.assertEqual([entry['id'] for entry in service.queue_ahead(c['id'])], [a['id'], b['id']])
        service.cancel(b['id'], 'queued')
        self.assertEqual([entry['id'] for entry in service.queue_ahead(c['id'])], [a['id']])
        self.assertEqual(service.queue, [('run', a['id']), ('run', c['id'])])
        self.finish(service, gate)
        self.assertEqual(service.queue, [])

    def test_a_queued_run_a_worker_already_took_leaves_the_list_when_it_ends(self):
        first, second = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)

        class Started:
            def cancel(self): return False
        service.futures[b['id']] = Started()
        service.cancel(b['id'], 'queued')
        self.assertIn(('run', b['id']), service.queue)
        self.finish(service, gate)
        self.assertEqual(service.queue, [])

    def test_a_run_waiting_behind_a_cancelling_run_shows_it_as_cancelling(self):
        doc, gate = self.document(2), Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        service.cancel(a['id'], 'running')
        a2 = self.start(service, doc)
        self.assertEqual([(entry['id'], entry['status']) for entry in service.queue_ahead(a2['id'])], [(a['id'], 'cancelling')])
        self.finish(service, gate)
        self.assertEqual(self.store.run(a['id'])['status'], 'cancelled')

    def test_an_entry_is_dropped_when_its_run_or_question_cannot_be_submitted(self):
        doc, gate = self.document(1), Gate(hold=())
        service = self.service(gate)
        submit = service.pool.submit

        def refuse(*a, **k): raise RuntimeError('pool closed')
        service.pool.submit = refuse
        with self.assertRaises(RuntimeError): self.start(service, doc)
        self.assertEqual(service.queue, [])
        with self.assertRaises(RuntimeError): self.ask(service, 'q')
        self.assertEqual(service.queue, [])
        self.assertEqual(service.slots._value, 8)
        service.pool.submit = submit

    def test_the_list_order_is_the_pool_order_while_a_submit_is_still_returning(self):
        doc, gate = self.document(1), Gate(hold=(1,))
        service = self.service(gate)
        self.start(service, doc)
        self.assertTrue(gate.wait_entered(1))
        executed, answer = [], service.answer
        service.answer = lambda job, *a, **k: (executed.append(job['id']), answer(job, *a, **k))[1]
        real, submitted = service.pool.submit, threading.Event()

        def hold_the_first_submit_open(*a, **k):
            future = real(*a, **k)
            if threading.current_thread().name == 'first':
                submitted.set()
                time.sleep(.02)
            return future
        service.pool.submit = hold_the_first_submit_open
        first = threading.Thread(target=self.ask, args=(service, 'one'), name='first')
        second = threading.Thread(target=self.ask, args=(service, 'two'), name='second')
        first.start()
        self.assertTrue(submitted.wait(5))
        second.start()
        first.join(5)
        second.join(5)
        listed = [key for kind, key in service.queue if kind == 'question']
        self.finish(service, gate)
        self.assertEqual(len(executed), 2)
        self.assertEqual(listed, executed)

    def test_a_question_asked_while_a_run_submit_is_still_returning_is_listed_after_the_run(self):
        first_doc, second_doc = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        a = self.start(service, first_doc)
        self.assertTrue(gate.wait_entered(1))
        executed, extract, answer = [], service.extract, service.answer
        service.extract = lambda run, *a, **k: (executed.append(('run', run['id'])), extract(run, *a, **k))[1]
        service.answer = lambda job, *a, **k: (executed.append(('question', job['id'])), answer(job, *a, **k))[1]
        real, submitted = service.pool.submit, threading.Event()

        def hold_the_run_submit_open(*a, **k):
            future = real(*a, **k)
            if threading.current_thread().name == 'first':
                submitted.set()
                time.sleep(.02)
            return future
        service.pool.submit = hold_the_run_submit_open
        starting = threading.Thread(target=self.start, args=(service, second_doc), name='first')
        asking = threading.Thread(target=self.ask, args=(service, 'one'), name='second')
        starting.start()
        self.assertTrue(submitted.wait(5))
        asking.start()
        starting.join(5)
        asking.join(5)
        listed = [entry for entry in service.queue if entry != ('run', a['id'])]
        self.finish(service, gate)
        self.assertEqual(len(executed), 2)
        self.assertEqual(listed, executed)

    def test_questions_that_end_before_their_submit_returns_leave_no_entry(self):
        gate = Gate(hold=())
        service = self.service(gate)
        real = service.pool.submit

        def return_when_done_or_after_a_tenth_of_a_second(*a, **k):
            future = real(*a, **k)
            deadline = time.monotonic() + .1
            while not future.done() and time.monotonic() < deadline: time.sleep(.001)
            return future
        service.pool.submit = return_when_done_or_after_a_tenth_of_a_second
        for number in range(20):
            self.ask(service, f'q{number}')
            deadline = time.monotonic() + 5
            while service.slots._value < 8 and time.monotonic() < deadline: time.sleep(.001)
        service.pool.shutdown(wait=True)
        self.assertEqual((service.queue, service.slots._value), ([], 8))

    def test_the_run_answer_over_http_carries_the_queue(self):
        first, second = self.documents(2)
        gate = Gate(hold=(1,))
        service = self.service(gate)
        server = Server(('127.0.0.1', 0), self.store, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        a = self.start(service, first)
        self.assertTrue(gate.wait_entered(1))
        b = self.start(service, second)
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/runs/{b["id"]}') as response:
            body = json.loads(response.read())
        self.assertEqual([entry['id'] for entry in body['queue']['ahead']], [a['id']])
        self.finish(service, gate)


class Interface(unittest.TestCase):
    def run_js(self, script):
        return subprocess.check_output(['node'], input=script, text=True)

    def test_what_a_queued_run_waits_for_is_listed_five_at_a_time_and_escaped(self):
        app = Path('verinote/web/app.js').read_text()
        functions = app[app.index('function renderProgress('):app.index('function bulkCandidates(')]
        script = """const assert=require('node:assert/strict');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={queued:'대기 중',running:'처리 중',cancelling:'취소 중'};let uploadBusy=false,uploadTarget=null;const runWatchers=new Map();
const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){}});return elements.get(s)};
""" + functions + """
const run=(status,ahead)=>({id:'run',stage:'extract',status,detail:'',progress:{known:false,phase:status,percent:null},...(ahead?{queue:{ahead}}:{})});
const document_=(id,status,stage='extract')=>({kind:'run',id,status,stage,document_id:'d',title:'계약 <b>'});
const question=(id,status)=>({kind:'question',id,status,question:'무엇을 <i>묻나'});
assert(renderAnalysisProgress(run('queued',[])).includes('<p class="muted">앞선 작업 없음</p>'));
const two=renderAnalysisProgress(run('queued',[document_('a','running','ontology'),question('q','queued')]));
assert(two.includes('앞선 작업 2개가 끝나야 시작합니다: 사전 생성 실행 a · 문서 「계약 &lt;b&gt;」 · 처리 중; 질문 q 「무엇을 &lt;i&gt;묻나」 · 대기 중'));
assert(!two.includes('<b>')&&!two.includes('<i>'));
const seven=renderAnalysisProgress(run('queued',Array.from({length:7},(_,i)=>document_('r'+i,'queued'))));
assert(seven.includes('앞선 작업 7개가 끝나야 시작합니다: ')&&seven.includes('관계 추출 실행 r4')&&!seven.includes('실행 r5')&&seven.includes(' 외 2개'));
assert(renderAnalysisProgress(run('queued',[{...document_('a','cancelling')}])).includes('· 취소 중'));
for(const status of ['running','cancelling','cancelled','success'])assert(!renderAnalysisProgress(run(status,[document_('a','running')])).includes('앞선 작업'),status);
assert(!renderAnalysisProgress(run('queued')).includes('앞선 작업'));
console.log('ok');
"""
        self.assertIn('ok', self.run_js(script))


if __name__ == '__main__':
    unittest.main()
