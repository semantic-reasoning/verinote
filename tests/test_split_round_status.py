"""A split-processing round (context_processing) stored as running or analyzed ends with the question that stores it ended: cancelled,
interrupted, or failed for error (#909). Step statuses keep the values #876 stores."""
import json
import subprocess
import unittest
from pathlib import Path

import test_question_synthesis as synthesis
from test_question_synthesis import scripted, unpack
from test_split_step_status import SplitStepStatus

from verinote.assistant import PROMPT
from verinote.question_synthesis import run
from verinote.store import RunCancelled, Store

ROOT = Path(__file__).resolve().parents[1]


class Restart(BaseException):
    """Stands in for the process stopping during a call: nothing in run() or Assistant.invoke catches it."""


def first_leaf(prompt):
    return 'assigned_ids' in unpack(prompt)


def decision(prompt):
    return 'analysis' in unpack(prompt)


class SplitRoundStatus(unittest.TestCase):
    setUp = synthesis.CompleteContext.setUp
    assistant = synthesis.CompleteContext.assistant
    stored = SplitStepStatus.stored
    pairs = SplitStepStatus.pairs
    start = SplitStepStatus.start

    def rounds(self, key):
        return [t['status'] for t in self.stored(key).get('context_processing', [])]

    def steps(self, key):
        return [node for _, node, _ in self.pairs(self.stored(key))]

    def during(self, when, *effects):
        """A scripted response that, on the first prompt matching when, runs each effect in order before answering."""
        done = []
        def response(prompt):
            if not done and when(prompt):
                done.append(True)
                for effect in effects: effect()
            return scripted(prompt)
        return response

    def cancel(self):
        self.store.cancel_question(self.key, 'running')

    def restart(self):
        raise Restart()

    def begin(self, response, **kwargs):
        a, raw = self.start(response, **kwargs)
        self.key = a.job['id']
        return a, raw

    def test_cancel_during_the_first_leaf_call_stores_the_round_cancelled_and_keeps_the_steps(self):
        a, raw = self.begin(self.during(first_leaf, self.cancel))
        with self.assertRaises(RunCancelled): run(a, raw, PROMPT, 2)
        self.assertEqual(self.rounds(self.key), ['running'])
        self.store.finish_question_cancel(self.key, '취소')
        self.assertEqual(self.stored(self.key)['status'], 'cancelled')
        self.assertEqual(self.rounds(self.key), ['cancelled'])
        self.assertEqual(self.steps(self.key), ['dispatched', 'planned', 'planned'])

    def test_cancel_committed_at_the_budget_record_save_stores_the_round_cancelled_and_keeps_the_steps_planned(self):
        def cancel(a): self.store.cancel_question(a.job['id'], 'running')
        a, raw = self.begin(scripted, trigger=lambda job: bool(job.get('context_budget')), action=cancel)
        with self.assertRaises(RunCancelled): run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])
        self.store.finish_question_cancel(self.key, '취소')
        self.assertEqual(self.rounds(self.key), ['cancelled'])
        self.assertEqual(self.steps(self.key), ['planned', 'planned', 'planned'])

    def test_cancel_during_the_decision_call_stores_the_analyzed_round_cancelled(self):
        a, raw = self.begin(self.during(decision, self.cancel))
        with self.assertRaises(RunCancelled): run(a, raw, PROMPT, 2)
        self.assertEqual(self.rounds(self.key), ['analyzed'])
        self.store.finish_question_cancel(self.key, '취소')
        self.assertEqual(self.rounds(self.key), ['cancelled'])
        self.assertTrue(all(node == 'complete' for node in self.steps(self.key)))

    def test_cancelling_question_keeps_its_running_round_until_the_cancel_is_finished(self):
        a, raw = self.begin(self.during(first_leaf, self.cancel, self.restart))
        with self.assertRaises(Restart): run(a, raw, PROMPT, 2)
        self.assertEqual(self.stored(self.key)['status'], 'cancelling')
        self.assertEqual(self.rounds(self.key), ['running'])

    def test_restart_during_a_leaf_call_then_recover_stores_the_round_interrupted_and_keeps_the_steps(self):
        a, raw = self.begin(self.during(first_leaf, self.restart))
        with self.assertRaises(Restart): run(a, raw, PROMPT, 2)
        self.assertEqual(self.rounds(self.key), ['running'])
        Store(self.store.path).recover()
        self.assertEqual(self.stored(self.key)['status'], 'interrupted')
        self.assertEqual(self.rounds(self.key), ['interrupted'])
        self.assertEqual(self.steps(self.key), ['dispatched', 'planned', 'planned'])

    def test_restart_while_cancelling_then_recover_stores_the_round_cancelled(self):
        a, raw = self.begin(self.during(first_leaf, self.cancel, self.restart))
        with self.assertRaises(Restart): run(a, raw, PROMPT, 2)
        Store(self.store.path).recover()
        self.assertEqual(self.stored(self.key)['status'], 'cancelled')
        self.assertEqual(self.rounds(self.key), ['cancelled'])

    def test_recover_rewrites_only_rows_whose_open_round_it_ends_counted_by_an_update_trigger(self):
        a, raw = self.begin(self.during(first_leaf, self.restart))
        with self.assertRaises(Restart): run(a, raw, PROMPT, 2)
        legacy = self.stored(self.key) | {'status': 'cancelled', 'error': '이전 버전'}
        ended = self.store.create_question('끝난 질문', 'claude', 'sonnet', self.store.snapshot()['id'])
        ended |= {'status': 'error', 'error': '실패', 'context_processing': [{'round': 1, 'status': 'failed', 'nodes': []}]}
        plain = self.store.create_question('답한 질문', 'claude', 'sonnet', self.store.snapshot()['id'])
        plain |= {'status': 'success'}
        with self.store.connect(True) as db:
            for job in (legacy, ended, plain):
                db.execute('UPDATE questions SET payload=? WHERE id=?', (json.dumps(job), job['id']))
            db.execute('CREATE TABLE writes(id TEXT)')
            db.execute('CREATE TRIGGER counted AFTER UPDATE ON questions BEGIN INSERT INTO writes VALUES(NEW.id); END')
        Store(self.store.path).recover()
        with self.store.connect() as db:
            writes = [r[0] for r in db.execute('SELECT id FROM writes')]
        self.assertEqual(writes, [self.key])
        self.assertEqual(self.rounds(self.key), ['cancelled'])
        self.assertEqual(self.rounds(ended['id']), ['failed'])

    def test_failure_of_the_first_tree_save_then_error_save_stores_the_round_failed(self):
        def fail(a): raise RuntimeError('save failed')
        a, raw = self.begin(scripted, trigger=lambda job: bool(job.get('context_processing')), action=fail)
        with self.assertRaisesRegex(RuntimeError, 'save failed'): run(a, raw, PROMPT, 2)
        self.assertEqual(a.job['context_processing'][0]['status'], 'running')
        self.store.save_question(a.job | {'status': 'error', 'error': 'save failed'})
        self.assertEqual(self.rounds(self.key), ['failed'])
        self.assertEqual(self.steps(self.key), ['planned', 'planned', 'planned'])

    def test_end_unstored_question_stores_a_running_round_failed(self):
        a, raw = self.begin(self.during(first_leaf, self.restart))
        with self.assertRaises(Restart): run(a, raw, PROMPT, 2)
        self.assertTrue(self.store.end_unstored_question(self.key, '실패'))
        self.assertEqual(self.rounds(self.key), ['failed'])
        self.assertEqual(self.steps(self.key), ['dispatched', 'planned', 'planned'])

    def test_finish_question_cancel_keeps_complete_and_failed_rounds(self):
        job = self.store.create_question('질문', 'claude', 'sonnet', self.store.snapshot()['id'])
        job |= {'status': 'running', 'context_processing': [{'round': n, 'status': s, 'nodes': []}
                                                             for n, s in ((1, 'complete'), (2, 'failed'), (3, 'running'))]}
        self.store.save_question(job)
        self.store.cancel_question(job['id'], 'running')
        self.store.finish_question_cancel(job['id'], '취소')
        self.assertEqual(self.rounds(job['id']), ['complete', 'failed', 'cancelled'])

    def test_save_question_leaves_the_callers_job_unchanged(self):
        job = self.store.create_question('질문', 'claude', 'sonnet', self.store.snapshot()['id'])
        job |= {'status': 'error', 'error': '실패', 'context_processing': [{'round': 1, 'status': 'running', 'nodes': []}]}
        self.store.save_question(job)
        self.assertEqual(job['context_processing'][0]['status'], 'running')
        self.assertEqual(self.rounds(job['id']), ['failed'])


RENDER = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const ctx=vm.createContext({});
vm.runInContext(source.match(/^const esc = .*$/m)[0]+';'+source.match(/^const labels = .*$/m)[0],ctx);
vm.runInContext(source.slice(source.indexOf('function renderQuestionWindow('),source.indexOf('function renderAnalysis(')),ctx);
ctx.job=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(vm.runInContext('renderQuestionWindow(job)',ctx));
'''


class RoundStatusLabel(unittest.TestCase):
    def summary(self, status):
        job = {'physical_call_bound': 4, 'context_processing': [{'round': 2, 'status': status, 'processed_records': 1, 'planned_records': 2,
                                                                 'processed_batches': 1, 'planned_batches': 2, 'nodes': []}]}
        html = subprocess.check_output(['node', '-e', RENDER], input=json.dumps(job), text=True, cwd=ROOT)
        return html.split('<summary>', 1)[1].split(' · 기록', 1)[0]

    def test_renderQuestionWindow_labels_cancelled_and_interrupted_rounds_and_keeps_other_values(self):
        for status, shown in (('cancelled', '취소됨'), ('interrupted', '중단'), ('failed', 'failed'), ('complete', 'complete'),
                              ('running', 'running'), ('analyzed', 'analyzed')):
            with self.subTest(status=status):
                self.assertEqual(self.summary(status), f'회차 2 · {shown}')


if __name__ == '__main__':
    unittest.main()
