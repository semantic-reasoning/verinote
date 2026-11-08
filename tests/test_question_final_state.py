"""Synthetic callers only: a question worker or a failed registration leaves its question in a final state and returns its slot."""
import json
import subprocess
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_assistant import Fake, answer, tool

from verinote.service import Service

ROOT = Path(__file__).resolve().parents[1]

RENDER = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const elements=new Map(),$=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',value:''});return elements.get(s);};
const ctx=vm.createContext({$,labels:{},renderUsage:()=>'',renderQuestionWindow:()=>'',renderQuestionMeanings:()=>'',executionGuidance:()=>'',bindMeaningControls:()=>{},renderEvidence:()=>'',renderAnalysis:()=>''});
vm.runInContext(source.match(/^const esc = .*$/m)[0]+';'+source.match(/^const badge = .*$/m)[0],ctx);
vm.runInContext(source.slice(source.indexOf('function renderAnswer('),source.indexOf('let deleteTarget;')),ctx);
ctx.job=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInContext('renderAnswer(job)',ctx);
process.stdout.write($('#answer-result').innerHTML.replace(/<[^>]+>/g,'|'));
'''


class Unreadable(Exception):
    def __str__(self): raise RuntimeError('str')


def raising(exc):
    def output(prompt): raise exc
    return output


class QuestionFinalState(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve

    def service(self, outputs):
        service = Service(self.store, self.root, Fake(outputs))
        self.addCleanup(service.close)
        return service

    def free_slots(self, service):
        taken = 0
        while service.slots.acquire(blocking=False): taken += 1
        for _ in range(taken): service.slots.release()
        return taken

    def ask(self, outputs, writes=None):
        service = self.service(outputs)
        with writes or nullcontext():
            job = service.ask({'question': '주문에서 원장까지 경로는?'})
            service.pool.shutdown(wait=True)
        return self.store.question(job['id']), service

    def final_writes(self, refuse, commit_first=False):
        """Patches save_question and end_unstored_question: every write of an ended question (success or error) is recorded, and raises when
        refuse(n, job) is true; the fixed-sentence write of end_unstored_question is recorded as an error when the stored question is queued or
        running, and when refused, not applied."""
        real, real_end, seen = self.store.save_question, self.store.end_unstored_question, []
        def write(job):
            if job['status'] in ('success', 'error'):
                seen.append((job['status'], job['error']))
                if refuse(len(seen), job):
                    if commit_first: real(job)
                    raise OSError('database')
            return real(job)
        def end(key, error):
            if self.store.question(key)['status'] not in ('queued', 'running'): return real_end(key, error)  # nothing is written: not recorded
            seen.append(('error', error))
            if refuse(len(seen), {'status': 'error', 'error': error}): raise OSError('database')
            return real_end(key, error)
        @contextmanager
        def patched():
            with patch.object(self.store, 'save_question', write), patch.object(self.store, 'end_unstored_question', end): yield
        return patched(), seen

    def assert_deletable(self):
        doc = self.store.document(self.doc['id'])
        self.store.delete_document(doc['id'], False, doc['version'], doc['generation'])

    def test_error_write_raising_once_is_retried_with_the_same_error(self):
        writes, seen = self.final_writes(lambda n, job: n == 1)
        job, service = self.ask([raising(ValueError('transport'))], writes)
        self.assertEqual((job['status'], job['error'], seen, self.free_slots(service)), ('error', 'transport', [('error', 'transport')] * 2, 8))
        self.assert_deletable()

    def test_success_write_raising_once_is_retried(self):
        self.approve(self.extract())
        writes, seen = self.final_writes(lambda n, job: n == 1)
        job, _ = self.ask([tool(), answer()], writes)
        self.assertEqual((job['status'], seen), ('success', [('success', '')] * 2), job['error'])

    def test_error_with_characters_sqlite_cannot_store_is_written_escaped(self):
        # A stubbed exception: no model output was found that puts such a character into a question's saved content.
        job, _ = self.ask([raising(ValueError('bad \ud800'))])  # the real store refuses the raw text: no store patch
        self.assertEqual((job['status'], job['error']), ('error', 'bad \\ud800'))
        self.assert_deletable()

    def test_error_that_cannot_be_written_falls_back_to_the_stored_question_with_a_fixed_sentence(self):
        writes, seen = self.final_writes(lambda n, job: job['error'] != Service.QUESTION_FAILURE_FALLBACK)
        job, _ = self.ask([raising(ValueError('transport'))], writes)
        self.assertEqual((job['status'], job['error'], len(seen)), ('error', Service.QUESTION_FAILURE_FALLBACK, 3))
        self.assert_deletable()

    def test_answer_that_cannot_be_written_falls_back_to_error_with_a_fixed_sentence(self):
        self.approve(self.extract())
        writes, seen = self.final_writes(lambda n, job: job['status'] == 'success')
        job, _ = self.ask([tool(), answer()], writes)
        self.assertEqual((job['status'], job['error'], job['answer'], len(seen)), ('error', Service.ANSWER_FALLBACK, None, 3))
        self.assertEqual(len(job['trace']), 1)  # what the last progress write stored stays

    def test_ended_write_that_committed_before_raising_is_not_overwritten(self):
        self.approve(self.extract())
        writes, seen = self.final_writes(lambda n, job: True, commit_first=True)
        job, _ = self.ask([tool(), answer()], writes)
        self.assertEqual((job['status'], len(seen)), ('success', 2))

    def test_every_ended_write_raising_leaves_the_question_as_it_was_and_returns_the_slot(self):
        writes, seen = self.final_writes(lambda n, job: True)
        job, service = self.ask([raising(ValueError('transport'))], writes)
        self.assertEqual((job['status'], seen[-1], len(seen), self.free_slots(service)), ('running', ('error', Service.QUESTION_FAILURE_FALLBACK), 3, 8))

    def test_exception_whose_text_cannot_be_read_still_ends_the_question_as_error(self):
        job, service = self.ask([raising(Unreadable())])
        self.assertEqual((job['status'], job['error'], self.free_slots(service)), ('error', 'Unreadable', 8))

    def registration_failure(self, refuse, error=None):
        error = error or RuntimeError('submit')
        service = self.service([])
        writes, seen = self.final_writes(refuse)
        def submit(*args, **kwargs): raise error
        with writes, patch.object(service.pool, 'submit', submit), self.assertRaises(type(error)) as raised:
            service.ask({'question': '주문에서 원장까지 경로는?'})
        (job,) = self.store.questions()
        return job, seen, self.free_slots(service), raised.exception

    def test_registration_failure_is_retried_and_returns_the_slot(self):
        job, seen, free, error = self.registration_failure(lambda n, job: n == 1)
        self.assertEqual((job['status'], job['error'], seen, free, str(error)), ('error', 'submit', [('error', 'submit')] * 2, 8, 'submit'))

    def test_registration_failure_with_every_ended_write_raising_reraises_the_original_error(self):
        job, seen, free, error = self.registration_failure(lambda n, job: True)
        self.assertEqual((job['status'], len(seen), free, str(error)), ('queued', 3, 8, 'submit'))

    def test_registration_failure_whose_text_cannot_be_read_still_ends_the_question_as_error(self):
        job, seen, free, error = self.registration_failure(lambda n, job: False, Unreadable())
        self.assertEqual((job['status'], job['error'], free), ('error', 'Unreadable', 8))
        self.assertIsInstance(error, Unreadable)

    def test_final_save_raising_still_returns_the_slot(self):  # finish_question swallows its own errors; this pins the slot release apart from that
        def boom(service, job): raise OSError('final')
        with patch.object(Service, 'finish_question', boom):
            job, service = self.ask([raising(ValueError('transport'))])
            self.assertEqual((job['status'], self.free_slots(service)), ('running', 8))
            def submit(*args, **kwargs): raise RuntimeError('submit')
            with patch.object(service.pool, 'submit', submit), self.assertRaises(OSError) as raised:
                service.ask({'question': '주문에서 원장까지 경로는?'})
            self.assertEqual((self.free_slots(service), str(raised.exception.__cause__)), (8, 'submit'))


    def test_error_write_that_committed_before_raising_keeps_its_detail(self):
        writes, seen = self.final_writes(lambda n, job: True, commit_first=True)
        job, _ = self.ask([raising(ValueError('transport'))], writes)
        self.assertEqual((job['status'], job['error'], len(seen)), ('error', 'transport', 2))

    def test_errors_are_stored_at_most_2000_characters_on_both_paths(self):
        job, _ = self.ask([raising(ValueError('z' * 3000))])  # the answer path's plain text, nothing to escape
        self.assertEqual((job['status'], job['error']), ('error', 'z' * 2000))
        self.tearDown(); self.setUp()
        job, _ = self.ask([raising(ValueError('x' * 1990 + '\ud800' * 10))])  # escaping lengthens the text past 2000
        self.assertEqual((job['status'], job['error']), ('error', 'x' * 1990 + '\\ud800'))
        self.tearDown(); self.setUp()
        job, _, _, error = self.registration_failure(lambda n, job: False, RuntimeError('y' * 3000))
        self.assertEqual((job['error'], len(str(error))), ('y' * 2000, 3000))

    def test_escaped_error_is_cut_before_an_escape_would_be_split(self):
        for length, kept in ((1997, 'x' * 1997), (1991, 'x' * 1991 + '\\ud800')):  # one escape is 6 characters: 1997 + 6 and 1991 + 12 pass 2000
            with self.subTest(length=length):
                self.tearDown(); self.setUp()
                job, _ = self.ask([raising(ValueError('x' * length + '\ud800\ud801'))])
                self.assertEqual((job['status'], job['error']), ('error', kept))

    def stuck_question(self):
        writes, _ = self.final_writes(lambda n, job: True)
        job, service = self.ask([raising(ValueError('transport'))], writes)
        self.assertEqual(job['status'], 'running')
        return job, service

    def test_fallback_keeps_a_question_stored_as_ended_by_another_writer(self):
        job, service = self.stuck_question()
        self.store.save_question(job | {'status': 'success', 'error': '', 'answer': {'claims': [], 'unknowns': []}})
        self.assertFalse(service.store.end_unstored_question(job['id'], Service.QUESTION_FAILURE_FALLBACK))
        self.assertEqual(self.store.question(job['id'])['status'], 'success')

    def test_fallback_reads_the_status_inside_the_transaction_that_writes_it(self):
        job, service = self.stuck_question()
        ended = job | {'status': 'success', 'error': '', 'answer': {'claims': [], 'unknowns': []}}
        real, armed = self.store.connect, [True]
        def connect(write=False):  # another writer commits success just before the fallback's first write transaction opens
            if write and armed[0]:
                armed[0] = False
                self.store.save_question(ended)
            return real(write)
        with patch.object(self.store, 'connect', connect):
            moved = service.store.end_unstored_question(job['id'], Service.QUESTION_FAILURE_FALLBACK)
        self.assertEqual((moved, self.store.question(job['id'])['status']), (False, 'success'))

    def test_registration_error_that_cannot_be_written_falls_back_to_the_fixed_sentence(self):
        job, seen, free, error = self.registration_failure(lambda n, job: job['error'] != Service.QUESTION_FAILURE_FALLBACK)
        self.assertEqual((job['status'], job['error'], len(seen), free, str(error)), ('error', Service.QUESTION_FAILURE_FALLBACK, 3, 8, 'submit'))

    def render(self, job):
        """The answer panel's text, rendered from the stored question by the real renderAnswer in app.js, tags replaced by |."""
        return subprocess.run(['node', '-e', RENDER], input=json.dumps(job), cwd=ROOT, check=True, capture_output=True, text=True).stdout

    def test_answer_panel_of_a_stored_error_question_says_only_what_is_stored(self):
        self.approve(self.extract())
        writes, _ = self.final_writes(lambda n, job: job['status'] == 'success')
        unsaved, _ = self.ask([tool(), answer()], writes)
        failed, _ = self.ask([raising(ValueError('transport'))])
        for job, error in ((unsaved, Service.ANSWER_FALLBACK), (failed, 'transport')):
            with self.subTest(error=error):
                self.assertEqual((job['status'], job['error'], job['answer']), ('error', error, None))
                self.assertIn(f'|{error}||저장된 답변이 없습니다.|', self.render(job))

if __name__ == '__main__':
    unittest.main()
