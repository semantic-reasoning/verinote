"""Synthetic callers only: cancelling a queued or running question, with gated fake transports instead of models."""
import ast
import json
import subprocess
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_workflow as workflow
from test_assistant import Fake, answer, tool

from verinote.assistant import Assistant
from verinote.server import Server
from verinote.service import Service
from verinote.store import (
    QUESTION_CANCEL_REQUESTED,
    QUESTION_QUEUED_CANCEL,
    QUESTION_RESTART_CANCEL,
    Conflict,
)
from verinote.usage import job_calls

ROOT = Path(__file__).resolve().parents[1]
HEAD = Service.QUESTION_CANCEL_HEAD
QUESTION = '주문에서 원장까지 경로는?'


class Base(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve

    def service(self, fake):
        self.approve(self.extract())
        service = Service(self.store, self.root, fake)
        self.addCleanup(service.close)
        return service

    def gated(self, first, rest=(), before=()):
        """A Fake that answers before, then blocks on the call answered by first until released; entered is set once that call has started."""
        entered, release = threading.Event(), threading.Event()
        def hold(prompt):
            entered.set()
            if not release.wait(20): raise TimeoutError('gate')
            return first
        self.addCleanup(release.set)
        return entered, release, Fake([*before, hold, *rest])

    def free_slots(self, service):
        taken = 0
        while service.slots.acquire(blocking=False): taken += 1
        for _ in range(taken): service.slots.release()
        return taken

    def manifest(self, key):
        return [c['status'] for c in self.store.question(key).get('call_manifest', [])]


class RunningCancel(Base):
    def cancel_during_the_gated_call(self, first, before, manifest):
        entered, release, fake = self.gated(first, [answer()] * 3, before)
        service = self.service(fake)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        self.assertEqual(service.cancel_question(job['id'], 'running'), 'running')
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelling', QUESTION_CANCEL_REQUESTED))
        release.set()
        service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error'], stored['answer']), ('cancelled', HEAD, None))
        self.assertEqual(len(fake.calls), len(manifest))
        self.assertEqual(self.manifest(job['id']), manifest)
        self.assertEqual((self.free_slots(service), service.queue, service.futures), (8, [], {}))
        with self.assertRaises(Conflict) as refused:
            service.cancel_question(job['id'], 'running')
        self.assertEqual(str(refused.exception), '이미 끝난 질문이라 취소하지 않았습니다 (상태: 취소됨).')

    def test_cancel_during_the_gated_call_of_a_tool_round_records_one_call_and_ends_cancelled(self):
        self.cancel_during_the_gated_call(tool(subject='주문'), (), ['dispatched'])

    def test_cancel_during_the_gated_call_of_an_answer_round_records_one_call_and_ends_cancelled(self):
        self.cancel_during_the_gated_call(answer(), (tool(subject='주문'),), ['complete', 'dispatched'])

    def test_with_the_stored_status_read_raising_a_cancel_is_still_recorded_with_the_exception_type_alone(self):
        entered, release, fake = self.gated(tool(subject='주문'), [answer()] * 3)
        service = self.service(fake)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        service.cancel_question(job['id'], 'running')
        with patch.object(self.store, 'question', side_effect=OSError('read')):
            release.set()
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelled', HEAD))

    def test_with_save_question_and_end_unstored_question_raising_oserror_the_stored_status_read_still_ends_it_cancelled(self):
        entered, release, fake = self.gated(answer(), [answer()] * 3)
        service = self.service(fake)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        service.cancel_question(job['id'], 'running')
        with (patch.object(self.store, 'save_question', side_effect=OSError('database')),
              patch.object(self.store, 'end_unstored_question', side_effect=OSError('database'))):
            release.set()
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelled', HEAD + ' · 멈추기 전 마지막 오류: database'))

    def test_a_cancel_committed_after_the_answer_was_validated_stores_no_answer_and_ends_cancelled(self):
        fake = Fake([tool(subject='주문'), answer(), answer()])
        service = self.service(fake)
        real = Assistant.validate_answer
        def validate(assistant, value):
            result = real(assistant, value)
            self.assertEqual(self.store.cancel_question(assistant.job['id'], 'running'), 'running')
            return result
        with patch.object(Assistant, 'validate_answer', validate):
            job = service.ask({'question': QUESTION})
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error'], stored['answer']), ('cancelled', Service.QUESTION_CANCEL_AFTER_ANSWER, None))
        self.assertEqual(len(fake.calls), 2)

    def test_with_end_unstored_question_raising_oserror_the_refused_answer_save_alone_ends_it_cancelled(self):
        service = self.service(Fake([tool(subject='주문'), answer(), answer()]))
        real = Assistant.validate_answer
        def validate(assistant, value):
            result = real(assistant, value)
            self.store.cancel_question(assistant.job['id'], 'running')
            return result
        with (patch.object(Assistant, 'validate_answer', validate),
              patch.object(self.store, 'end_unstored_question', side_effect=OSError('database'))):
            job = service.ask({'question': QUESTION})
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error'], stored['answer']), ('cancelled', Service.QUESTION_CANCEL_AFTER_ANSWER, None))

    def test_a_cancel_committed_after_validation_with_every_later_save_raising_oserror_ends_cancelled_through_the_fallback(self):
        fake = Fake([tool(subject='주문'), answer(), answer()])
        service = self.service(fake)
        real, cancelled = Assistant.validate_answer, []
        def validate(assistant, value):
            result = real(assistant, value)
            self.store.cancel_question(assistant.job['id'], 'running')
            cancelled.append(True)
            return result
        real_save = self.store.save_question
        def save(job):
            if cancelled: raise OSError('locked')
            return real_save(job)
        with patch.object(Assistant, 'validate_answer', validate), patch.object(self.store, 'save_question', save):
            job = service.ask({'question': QUESTION})
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error'], stored['answer']), ('cancelled', Service.QUESTION_CANCEL_AFTER_ANSWER, None))

    def test_a_conflict_raised_after_the_cancel_is_kept_as_the_last_error(self):
        entered, release, fake = self.gated(tool(subject='주문'), [answer()] * 3)
        service = self.service(fake)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        service.cancel_question(job['id'], 'running')
        with patch.object(self.store, 'save_question', side_effect=Conflict('근거 스냅샷이 삭제되었습니다. 새로 조회하세요.')):
            release.set()
            service.pool.wait_idle()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelled', HEAD + ' · 멈추기 전 마지막 오류: 근거 스냅샷이 삭제되었습니다. 새로 조회하세요.'))

    def test_a_question_whose_answer_was_saved_refuses_the_cancel_and_keeps_its_answer(self):
        service = self.service(Fake([tool(subject='주문'), answer()]))
        job = service.ask({'question': QUESTION})
        service.pool.wait_idle()
        for expected in ('running', 'queued'):
            with self.assertRaises(Conflict) as refused:
                service.cancel_question(job['id'], expected)
            self.assertEqual(str(refused.exception), '이미 끝난 질문이라 취소하지 않았습니다 (상태: 완료).')
        stored = self.store.question(job['id'])
        self.assertEqual(stored['status'], 'success')
        self.assertIsNotNone(stored['answer'])

    def test_a_running_question_cancelled_as_queued_is_refused_and_left_running(self):
        entered, release, fake = self.gated(tool(subject='주문'), [answer()])
        service = self.service(fake)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        with self.assertRaises(Conflict) as refused:
            service.cancel_question(job['id'], 'queued')
        self.assertEqual(str(refused.exception), '대기 중이던 질문이 처리 중으로 바뀌어 취소하지 않았습니다. 처리 중인 질문을 취소할지 다시 확인하세요.')
        self.assertEqual(self.store.question(job['id'])['status'], 'running')
        with self.assertRaises(ValueError):
            service.cancel_question(job['id'], 'cancelled')
        release.set()
        service.pool.wait_idle()
        self.assertEqual(self.store.question(job['id'])['status'], 'success')


class QueuedCancel(Base):
    def queued_behind_a_gated_question(self):
        entered, release, fake = self.gated(tool(subject='주문'), [answer()] * 3)
        service = self.service(fake)
        first = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        second = service.ask({'question': '두 번째 질문'})
        self.assertEqual(self.store.question(second['id'])['status'], 'queued')
        return service, release, fake, first, second

    def test_a_queued_cancel_releases_the_slot_dequeues_and_drops_the_future_before_the_earlier_question_ends(self):
        service, release, fake, first, second = self.queued_behind_a_gated_question()
        self.assertEqual(self.free_slots(service), 6)
        self.assertEqual(service.cancel_question(second['id'], 'queued'), 'queued')
        # measured while the first question is still gated, so no worker has taken the second one
        self.assertEqual(self.free_slots(service), 7)
        self.assertEqual(service.queue, [('question', first['id'])])
        self.assertNotIn(second['id'], service.futures)
        self.assertEqual(self.store.question(second['id'])['status'], 'cancelled')
        release.set()
        service.pool.wait_idle()
        stored = self.store.question(second['id'])
        self.assertEqual((stored['status'], stored['error'], self.manifest(second['id'])), ('cancelled', QUESTION_QUEUED_CANCEL, []))
        self.assertFalse(any('두 번째 질문' in prompt for _, _, prompt in fake.calls))
        self.assertEqual((self.free_slots(service), service.queue, service.futures), (8, [], {}))
        self.assertEqual(job_calls(self.root, 'questions', stored), [])

    def test_a_queued_cancel_a_worker_already_took_keeps_the_zero_call_text(self):
        service, release, fake, first, second = self.queued_behind_a_gated_question()
        with patch.object(Future, 'cancel', lambda future: False):
            self.assertEqual(service.cancel_question(second['id'], 'queued'), 'queued')
        self.assertEqual(self.free_slots(service), 6)  # the worker that takes it returns the slot
        release.set()
        service.pool.wait_idle()
        stored = self.store.question(second['id'])
        self.assertEqual((stored['status'], stored['error'], self.manifest(second['id'])), ('cancelled', QUESTION_QUEUED_CANCEL, []))
        self.assertFalse(any('두 번째 질문' in prompt for _, _, prompt in fake.calls))
        self.assertEqual((self.free_slots(service), service.queue, service.futures), (8, [], {}))

    def test_a_question_cancelled_while_its_submit_fails_keeps_the_zero_call_text_and_returns_its_slot(self):
        service = self.service(Fake([]))
        def submit(fn, job, *args):
            self.store.cancel_question(job['id'], 'queued')
            raise RuntimeError('submit')
        with patch.object(service.pool, 'submit', submit), self.assertRaises(RuntimeError):
            service.ask({'question': QUESTION})
        [stored] = self.store.questions()
        self.assertEqual((stored['status'], stored['error']), ('cancelled', QUESTION_QUEUED_CANCEL))
        self.assertEqual((self.free_slots(service), service.queue, service.futures), (8, [], {}))

    def test_a_queued_question_carries_the_earlier_question_it_waits_for_and_no_document_blockers(self):
        service, release, fake, first, second = self.queued_behind_a_gated_question()
        queue = service.with_usage('questions', self.store.question(second['id']))['queue']
        self.assertEqual((queue['stage'], queue['capacity'], queue['document_blockers']), ('question', 1, []))
        self.assertEqual([(e['kind'], e['id'], e['status']) for e in queue['ahead']], [('question', first['id'], 'running')])
        self.assertNotIn('queue', service.with_usage('questions', self.store.question(first['id'])))
        text = render_answer(service.with_usage('questions', self.store.question(second['id'])))
        self.assertIn(f"질문 동시 실행 1개 한도에서 자리 배정을 기다립니다. 같은 단계 작업 1개: 질문 {first['id']} 「{QUESTION}」 · 처리 중", text)
        self.assertNotIn('이 문서의 이전 실행', text)
        release.set()
        service.pool.wait_idle()


class StoredState(Base):
    def cancelling_question(self):
        self.approve(self.extract())
        job = self.store.create_question(QUESTION, 'codex', '', self.store.snapshot()['id'])
        job['status'] = 'running'
        self.store.save_question(job)
        self.assertEqual(self.store.cancel_question(job['id'], 'running'), 'running')
        return job

    def test_document_deletion_is_refused_while_a_question_is_cancelling(self):
        job = self.cancelling_question()
        doc = self.store.document(self.doc['id'])
        with self.assertRaises(Conflict) as refused:
            self.store.delete_document(doc['id'], False, doc['current_version'], doc['generation'])
        self.assertTrue(str(refused.exception).startswith('취소한 질문이 아직 멈추지 않았습니다.'), str(refused.exception))
        self.store.finish_question_cancel(job['id'], HEAD)
        self.store.delete_document(doc['id'], False, doc['current_version'], doc['generation'])

    def test_a_restart_moves_a_cancelling_question_to_cancelled(self):
        job = self.cancelling_question()
        self.store.recover()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['error']), ('cancelled', QUESTION_RESTART_CANCEL))

    def test_a_cancelled_question_without_a_call_manifest_shows_zero_calls_only_for_the_queued_cancel_text(self):
        owner = {'id': 'a' * 32, 'agent': 'codex', 'model': '', 'status': 'cancelled', 'error': QUESTION_QUEUED_CANCEL}
        self.assertEqual(job_calls(self.root, 'questions', owner), [])
        [unknown] = job_calls(self.root, 'questions', owner | {'error': HEAD})
        self.assertEqual((unknown['status'], unknown['call_count_known']), ('unknown', False))

    def test_a_cancelled_question_refuses_every_later_save(self):
        job = self.cancelling_question()
        with self.assertRaises(Exception) as refused:
            self.store.save_question(job | {'status': 'success', 'answer': {'claims': [], 'unknowns': []}})
        self.assertEqual(type(refused.exception).__name__, 'RunCancelled')
        self.assertEqual(self.store.question(job['id'])['status'], 'cancelling')


class Structure(unittest.TestCase):
    def test_the_first_statement_of_answer_is_the_submitted_check(self):
        tree = ast.parse((ROOT / 'verinote/service.py').read_text())
        answer_def = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'answer')
        first = answer_def.body[0]
        self.assertIsInstance(first, ast.If)
        self.assertEqual(ast.unparse(first.test), "not self.submitted('question', job['id'])")
        self.assertIsInstance(first.body[0], ast.Return)

    def test_in_assistant_and_question_modules_the_one_complete_call_is_in_invoke_with_a_progress_call_on_an_earlier_line(self):
        def own_calls(function):
            """Calls in function's body, leaving out the bodies of functions and lambdas nested in it."""
            stack, found = list(function.body), []
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): continue
                if isinstance(node, ast.Call): found.append(node)
                stack.extend(ast.iter_child_nodes(node))
            return found
        sites = []
        for path in [ROOT / 'verinote/assistant.py', *sorted((ROOT / 'verinote').glob('question_*.py'))]:
            tree = ast.parse(path.read_text())
            functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for function in functions:
                for call in own_calls(function):
                    if isinstance(call.func, ast.Attribute) and call.func.attr == 'complete':
                        progress = [c.lineno for c in own_calls(function) if isinstance(c.func, ast.Attribute) and c.func.attr == 'progress']
                        sites.append((path.name, function.name, any(line < call.lineno for line in progress)))
        self.assertEqual(sites, [('assistant.py', 'invoke', True)])


def render_answer(job):
    """The answer panel's text from the real renderQueueAhead and renderAnswer in app.js, tags replaced by |."""
    script = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const elements=new Map(),$=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',value:''});return elements.get(s);};
const ctx=vm.createContext({$,renderUsage:()=>'',renderQuestionWindow:()=>'',renderQuestionMeanings:()=>'',executionGuidance:()=>'GUIDANCE',bindMeaningControls:()=>{},renderEvidence:()=>'',renderAnalysis:()=>''});
vm.runInContext(source.match(/^const esc = .*$/m)[0]+';'+source.match(/^const labels = .*$/m)[0]+';'+source.match(/^const badge = .*$/m)[0],ctx);
vm.runInContext(source.slice(source.indexOf('function renderQueueAhead('),source.indexOf('function renderAnalysisProgress(')),ctx);
vm.runInContext(source.slice(source.indexOf('function renderAnswer('),source.indexOf('let deleteTarget;')),ctx);
ctx.job=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInContext('renderAnswer(job)',ctx);
process.stdout.write($('#answer-result').innerHTML);
'''
    return subprocess.check_output(['node', '-e', script], input=json.dumps(job), text=True, cwd=ROOT)


class Interface(unittest.TestCase):
    def job(self, status, **extra):
        return {'id': 'q1', 'question': '질문', 'agent': 'openrouter', 'model': '', 'snapshot': 's' * 64, 'status': status, 'trace': [],
                'evidence': {}, 'answer': None, 'error': '', **extra}

    def test_the_cancel_button_shows_only_for_queued_and_running_and_a_cancel_note_shows_without_execution_guidance(self):
        for status in ('queued', 'running'):
            self.assertIn(f'data-cancel-question="q1" data-cancel-status="{status}"', render_answer(self.job(status)))
        for status in ('cancelling', 'cancelled', 'success', 'error', 'interrupted'):
            self.assertNotIn('data-cancel-question', render_answer(self.job(status)))
        for status, error in (('cancelling', QUESTION_CANCEL_REQUESTED), ('cancelled', HEAD)):
            html = render_answer(self.job(status, error=error))
            self.assertIn(f'<p class="muted">{error}</p>', html)
            self.assertNotIn('GUIDANCE', html)
        self.assertIn('GUIDANCE', render_answer(self.job('error', error='실패')))
        empty = render_answer(self.job('queued', queue={'stage': 'question', 'capacity': 1, 'ahead': [], 'document_blockers': []}))
        self.assertIn('질문 작업자 배정 대기', empty)
        self.assertNotIn('실행 작업자', empty)

    def test_the_dialog_posts_the_question_cancel_and_reopens_only_for_queued_that_is_now_running(self):
        app = (ROOT / 'verinote/web/app.js').read_text()
        dialog = app[app.index('let cancelTarget;'):app.index('async function loadCleanup(')]
        script = """const assert=require('node:assert/strict');
const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{textContent:'',innerHTML:'',disabled:false,open:false,showModal(){this.open=true},close(){this.open=false}});return elements.get(s)};
function notice(message){$('#notice').textContent=message}
async function attempt(fn){return fn()}
let activeQuestion='q1',rendered=[],loads=0;function renderAnswer(job){rendered.push(job.status)}async function loadQuestions(){loads++}
let api;
""" + dialog + """
(async()=>{
 openCancelQuestion('q1','queued');
 assert.equal($('#cancel-run-heading').textContent,'이 질문을 취소할까요?');
 assert.equal($('#confirm-cancel-run').textContent,'질문 취소');
 assert.equal($('#cancel-run-body').textContent,'대기 중인 질문은 모델을 호출하기 전에 취소됩니다. 그사이 처리가 시작되면 취소하지 않고 다시 확인을 요청합니다. 실행과 다른 질문은 계속됩니다.');
 openCancelQuestion('q1','running');
 assert.equal($('#cancel-run-body').textContent,'이미 허용한 모델 호출은 중단하지 않으므로 끝날 때까지 기다린 뒤 멈추며, 그 호출의 비용은 줄지 않습니다. 이 질문은 답변을 저장하지 않습니다. 실행은 계속되고, 다음 질문은 이 질문이 멈춘 뒤 처리됩니다.');
 const calls=[];
 api=async(path,method,data)=>{calls.push([path,method,data]);return {id:'q1',status:'cancelling',cancel:{previous:'running'}}};
 await $('#confirm-cancel-run').onclick();
 assert.deepEqual(calls,[['/api/questions/q1/cancel','POST',{expected_status:'running'}]]);
 assert.equal($('#notice').textContent,'취소를 요청했습니다. 이미 허용한 호출이 끝나면 멈춥니다.');
 assert.deepEqual(rendered,['cancelling']);assert.equal(loads,1);assert(!$('#cancel-run-dialog').open);
 const conflict=message=>{const e=Error(message);e.status=409;return e};
 api=async(path,method)=>{if(method==='POST')throw conflict('바뀜');return {id:'q1',status:'running'}};
 openCancelQuestion('q1','queued');await $('#confirm-cancel-run').onclick();
 assert($('#cancel-run-dialog').open);assert.equal($('#cancel-run-error').textContent,'바뀜');
 assert($('#cancel-run-body').textContent.startsWith('이미 허용한 모델 호출은'));
 api=async(path,method)=>{if(method==='POST')throw conflict('끝남');return {id:'q1',status:'success'}};
 await $('#confirm-cancel-run').onclick();
 assert(!$('#cancel-run-dialog').open);assert.equal($('#notice').textContent,'끝남');
 openCancelRun('r1','queued','extract');
 assert.equal($('#cancel-run-heading').textContent,'이 실행을 취소할까요?');assert.equal($('#confirm-cancel-run').textContent,'실행 취소');
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok', subprocess.check_output(['node'], input=script, text=True, cwd=ROOT))

    def test_a_watched_question_keeps_polling_while_cancelling_and_stops_when_cancelled(self):
        app = (ROOT / 'verinote/web/app.js').read_text()
        watch = app[app.index('async function watchQuestion('):app.index('function renderEvidence(')]
        script = """const assert=require('node:assert/strict');
let activeQuestion='q1',timers=[],loads=0,status;global.setTimeout=fn=>timers.push(fn);
function renderAnswer(){}async function loadQuestions(){loads++}function notice(m){throw Error(m)}
const api=async()=>({id:'q1',status});
""" + watch + """
(async()=>{
 status='cancelling';await watchQuestion('q1');assert.equal(timers.length,1);assert.equal(loads,0);
 status='cancelled';await watchQuestion('q1');assert.equal(timers.length,1);assert.equal(loads,1);
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok', subprocess.check_output(['node'], input=script, text=True, cwd=ROOT))


class Route(Base):
    def call(self, path, method='POST', data=None):
        request = Request(self.url + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request) as response: return response.status, json.loads(response.read())
        except HTTPError as error:
            with error: return error.code, json.loads(error.read())

    def test_body_status_codes_and_the_previous_status_in_the_answer(self):
        entered, release, fake = self.gated(answer(), [answer()] * 3)
        service = self.service(fake)
        server = Server(('127.0.0.1', 0), self.store, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (release.set(), server.shutdown(), server.server_close()))
        self.url = f'http://127.0.0.1:{server.server_port}'
        first = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        second = service.ask({'question': '두 번째 질문'})
        path = f"/api/questions/{first['id']}/cancel"
        for bad in ({}, {'expected_status': 'running', 'x': 1}, {'expected_status': 'cancelled'}, {'status': 'running'}):
            self.assertEqual(self.call(path, data=bad)[0], 400, bad)
        self.assertEqual(self.call('/api/questions/' + '0' * 32 + '/cancel', data={'expected_status': 'running'})[0], 400)
        code, body = self.call(f"/api/questions/{second['id']}/cancel", data={'expected_status': 'queued'})
        self.assertEqual((code, body['cancel'], body['status'], body['error']), (200, {'previous': 'queued'}, 'cancelled', QUESTION_QUEUED_CANCEL))
        self.assertEqual(body['usage']['calls'], 0)
        code, body = self.call(path, data={'expected_status': 'queued'})
        self.assertEqual(code, 409, body)
        code, body = self.call(path, data={'expected_status': 'running'})
        self.assertEqual((code, body['cancel'], body['status']), (200, {'previous': 'running'}, 'cancelling'))
        code, body = self.call(path, data={'expected_status': 'running'})
        self.assertEqual((code, body['error']), (409, '이미 취소를 요청한 질문입니다. 아직 멈추지 않았습니다.'))
        release.set()
        service.pool.wait_idle()
        self.assertEqual(self.call(f"/api/questions/{first['id']}", 'GET')[1]['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
