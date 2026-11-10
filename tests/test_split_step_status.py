"""A split-processing step's stored status is its call manifest record's status in every successful Assistant.progress save during
question_synthesis.run (#876). Saves outside run(), such as the worker's, are not recorded here."""
import json
import unittest

import test_question_synthesis as synthesis
from test_question_synthesis import scripted, state

from verinote.assistant import PROMPT
from verinote.question_synthesis import run
from verinote.store import RunCancelled


def budget_saved(job):
    return bool(job.get('context_budget'))


def dispatched_saved(job):
    return any(c['status'] == 'dispatched' for c in job['call_manifest'])


class SplitStepStatus(unittest.TestCase):
    setUp = synthesis.CompleteContext.setUp
    assistant = synthesis.CompleteContext.assistant

    def stored(self, key):
        with self.store.connect() as db:
            return json.loads(db.execute('SELECT payload FROM questions WHERE id=?', (key,)).fetchone()[0])

    def pairs(self, job):
        manifest = {c['path']: c['status'] for c in job['call_manifest']}
        return [(n['path'], n['status'], manifest[n['path']]) for tree in job.get('context_processing', []) for n in tree['nodes']]

    def start(self, response=scripted, trigger=lambda job: False, action=None):
        """Run the split processing with an Assistant.progress that saves to a real Store. Before the first progress save whose job
        satisfies trigger, action runs. After each progress save that succeeds, the stored node and manifest statuses are appended to
        self.saves; a progress save that raises appends nothing."""
        raw = state(); a = self.assistant(raw, response)
        question = self.store.create_question(raw['question'], 'claude', 'sonnet', self.store.snapshot()['id'])
        question['status'] = 'running'; self.store.save_question(question)
        a.job.update(id=question['id'], snapshot=question['snapshot'], status='running', call_manifest=[], created_at=question['created_at'])
        self.saves = []; fired = []
        def progress(*args):
            if not fired and trigger(a.job):
                fired.append(True); action(a)
            a.job['trace'], a.job['evidence'] = a.trace, a.evidence
            self.store.save_question(a.job)
            self.saves.append(self.pairs(self.stored(a.job['id'])))
        a.progress = progress
        return a, raw

    def assertEachRecordedProgressSaveAgrees(self):
        self.assertTrue(self.saves)
        for pairs in self.saves:
            self.assertEqual([(path, manifest) for path, _, manifest in pairs], [(path, node) for path, node, _ in pairs])

    def test_failed_budget_record_save_leaves_the_step_planned_like_its_manifest_record(self):
        def fail(a): raise RuntimeError('save failed')
        a, raw = self.start(trigger=budget_saved, action=fail)
        with self.assertRaisesRegex(RuntimeError, 'save failed'): run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])
        stored = self.stored(a.job['id'])
        self.assertEqual(stored['context_processing'][0]['status'], 'failed')
        self.assertEqual(self.pairs(stored)[0][1:], ('planned', 'planned'))
        self.assertEachRecordedProgressSaveAgrees()

    def test_failed_dispatched_record_save_leaves_the_step_planned_like_its_manifest_record(self):
        def fail(a): raise RuntimeError('save failed')
        a, raw = self.start(trigger=dispatched_saved, action=fail)
        with self.assertRaisesRegex(RuntimeError, 'save failed'): run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])
        stored = self.stored(a.job['id'])
        self.assertEqual(stored['context_processing'][0]['status'], 'failed')
        self.assertEqual(self.pairs(stored)[0][1:], ('planned', 'planned'))
        self.assertEachRecordedProgressSaveAgrees()

    def test_step_whose_call_started_and_failed_is_failed_like_its_manifest_record(self):
        def raises(prompt): raise RuntimeError('provider failed')
        def missing_receipt(prompt):
            result = scripted(prompt); result['records'].pop()
            return result
        for response, error in ((raises, 'provider failed'), (missing_receipt, '처리 범위')):
            with self.subTest(error=error):
                a, raw = self.start(response)
                with self.assertRaisesRegex(Exception, error): run(a, raw, PROMPT, 2)
                self.assertEqual(len(a.cli.calls), 1)
                self.assertEqual(self.pairs(self.stored(a.job['id']))[0][1:], ('failed', 'failed'))
                self.assertEachRecordedProgressSaveAgrees()

    def test_cancel_committed_before_the_dispatched_save_stores_the_step_planned_like_its_manifest_record(self):
        def cancel(a): self.store.cancel_question(a.job['id'], 'running')
        a, raw = self.start(trigger=dispatched_saved, action=cancel)
        with self.assertRaises(RunCancelled): run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])
        stored = self.stored(a.job['id'])
        self.assertEqual(stored['status'], 'cancelling')
        self.assertEqual(self.pairs(stored)[0][1:], ('planned', 'planned'))
        self.assertEachRecordedProgressSaveAgrees()

    def test_each_progress_save_of_a_successful_split_run_stores_each_leaf_and_merge_step_like_its_manifest_record(self):
        a, raw = self.start()
        run(a, raw, PROMPT, 2)
        stages = {n['stage'] for n in self.stored(a.job['id'])['context_processing'][0]['nodes']}
        self.assertEqual(stages, {'analysis_leaf', 'analysis_merge'})
        self.assertTrue(all(node == 'complete' for _, node, _ in self.saves[-1]))
        self.assertEachRecordedProgressSaveAgrees()


if __name__ == '__main__':
    unittest.main()
