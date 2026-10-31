"""Real subprocesses, synthetic responses: benchmark ownership through Service."""
import json
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from verinote.claude_worker import Worker
from verinote.documents import chunks
from verinote.extraction_benchmark import Limits, ReportWriter, Stopped, measure
from verinote.extraction_evaluation import ideal_outputs, load_case

PROGRAM = r'''
import json, os, sys, time, uuid
from pathlib import Path
root=Path(sys.argv[1]); persistent=sys.argv[2]=='persistent'
answers=json.loads((root/'answers.json').read_text()); session=uuid.uuid4().hex; history=[]
def emit(value): print(json.dumps(value),flush=True)
def envelope(body, error=False):
    return dict(type='result',subtype='success',is_error=error,session_id=session,result=body,
        usage=dict(input_tokens=100,output_tokens=20,cache_read_input_tokens=0,cache_creation_input_tokens=0),
        total_cost_usd=.01,modelUsage={'synthetic-model':{}})
def request(text):
    global session, history
    with (root/'transcript').open('a') as f:
        f.write(json.dumps(dict(pid=os.getpid(),text=text,history=history,session=session))+'\n')
    if text=='/clear':
        old=session; session=uuid.uuid4().hex; history=[]
        emit(dict(type='conversation_reset',session_id=old,new_conversation_id=uuid.uuid4().hex))
        value=envelope(''); value['usage']={k:0 for k in value['usage']}; value['total_cost_usd']=0; value['modelUsage']={}
        emit(dict(type='system',subtype='init',session_id=session)); emit(value); return
    mode=(root/'mode').read_text() if (root/'mode').exists() else ''
    (root/'started').write_text('yes')
    if mode=='hang':time.sleep(30)
    if mode=='wait':
        while not (root/'release').exists():time.sleep(.01)
    payload=json.loads(text[text.index('{"filename":'):])
    output=answers[payload['text']]; history.append(text)
    error=mode=='fail_once' and not (root/'failed').exists()
    if error:(root/'failed').write_text('yes')
    emit(envelope(json.dumps(output),error))
if persistent:
    for line in sys.stdin:request(json.loads(line)['message']['content'])
else:request(sys.stdin.read())
'''


class PersistentBenchmark(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.script = self.root/'provider.py'; self.script.write_text(PROGRAM)
        answers = {}
        for name in ('short', 'long', 'dense', 'none', 'boundary'):
            case = load_case(name)
            for (_, text), result in zip(chunks(case['source']), ideal_outputs(case), strict=True):
                answers[text] = result['output']
        (self.root/'answers.json').write_text(json.dumps(answers))
        self.workers = []
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('verinote.agents.executable', return_value=sys.executable))
        self.stack.enter_context(patch('verinote.claude_worker.command', side_effect=lambda *_: [sys.executable, str(self.script), str(self.root), 'persistent']))
        self.stack.enter_context(patch('verinote.agents.command', side_effect=lambda agent, model, prompt, **kw:
                                      ([sys.executable, str(self.script), str(self.root), 'oneshot'], prompt)))
        def create(*args):
            worker = Worker(*args); self.workers.append(worker); return worker
        self.stack.enter_context(patch('verinote.claude_worker.Worker', side_effect=create))
        self.counter = 0

    def run_measure(self, process_mode='batch', **kwargs):
        self.counter += 1
        return measure(self.root/f'out{self.counter}', process_mode=process_mode, mode='cli', agent='claude',
                       model='synthetic-model', cases=kwargs.pop('cases', ['long']),
                       limits=kwargs.pop('limits', Limits(repetitions=2)), timeout=kwargs.pop('timeout', 3), **kwargs)

    def transcript(self):
        return [json.loads(x) for x in (self.root/'transcript').read_text().splitlines()]

    def assert_closed(self):
        for worker in self.workers:
            self.assertIsNotNone(worker.proc.poll())
            self.assertTrue(all(not t.is_alive() for t in worker.threads))
            self.assertFalse(Path(worker.directory.name).exists())

    def test_three_modes_preserve_quality_and_have_distinct_process_boundaries(self):
        for mode, count, observations in (
                ('oneshot', 4, ['unknown']*4),
                ('document', 2, ['started', 'reused']*2),
                ('batch', 1, ['started', 'reused', 'reused', 'reused'])):
            with self.subTest(mode=mode):
                (self.root/'transcript').unlink(missing_ok=True)
                report = self.run_measure(mode, cli_version='2.1.311')
                self.assertEqual(report['status'], 'completed', report['stop_reason'])
                self.assertTrue(all(r['quality']['passed'] for r in report['repetitions']))
                calls = [r for r in self.transcript() if r['text'] != '/clear']
                self.assertEqual(len(calls), 4)
                self.assertEqual(len({r['pid'] for r in calls}), count)
                self.assertTrue(all(r['history'] == [] for r in calls))
                attempts = report['attempts']
                self.assertEqual([a['process_observation'] for a in attempts], observations)
                self.assertEqual(len({a['cli_call_id'] for a in attempts}), 4)
                self.assertEqual([a['repetition'] for a in attempts], [1, 1, 2, 2])
                self.assertEqual(report['budget']['observed_tokens_lower_bound'], 480)
                self.assertAlmostEqual(report['summary']['reported_usd']['value'], .04)
                self.assertEqual(report['conditions']['cli_version_source'], 'operator_supplied_unverified')
                self.assertEqual(report['conditions']['row_seconds_includes_transport_close'], mode != 'batch')
                if mode != 'oneshot':
                    self.assertEqual(len(self.transcript()), 8)
                    self.assertEqual(len({r['session'] for r in calls}), 4)
                    self.assertTrue(all(r['text'] == '/clear' for r in self.transcript()[::2]))
                    self.assertEqual(attempts[1]['cli_timing']['schema_version'], 2)
                    self.assertIsNone(attempts[1]['cli_timing']['durations_seconds']['spawn'])
                if mode == 'batch': self.assertGreaterEqual(report['batch_teardown_seconds'], 0)
                else: self.assertIsNone(report['batch_teardown_seconds'])
                self.assert_closed()

    def test_batch_crosses_cases_without_reusing_documents_or_call_identity(self):
        report = self.run_measure(cases=['short', 'long'])
        self.assertEqual(len(report['attempts']), 6)
        self.assertEqual([a['case_id'] for a in report['attempts']], ['short', 'long', 'long']*2)
        self.assertEqual({a['owner_id'] for a in report['attempts']}, {1})
        self.assertTrue(all(r['quality']['passed'] for r in report['repetitions']))
        self.assertTrue(all(r['document_timing']['summary']['new_call_attempts'] == r['attempt_count'] for r in report['repetitions']))
        self.assertEqual(len(self.workers), 1); self.assert_closed()

    def test_failure_restarts_on_next_explicit_repetition_without_retry(self):
        (self.root/'mode').write_text('fail_once')
        report = self.run_measure(cases=['short'], limits=Limits(repetitions=2, missing_usage='continue'))
        self.assertEqual([r['status'] for r in report['repetitions']], ['error', 'success'])
        self.assertEqual([a['process_observation'] for a in report['attempts']], ['started', 'restarted'])
        self.assertEqual(len(report['attempts']), 2)
        self.assertEqual(len(self.workers), 2)
        self.assertAlmostEqual(report['summary']['reported_usd']['value'], .02)
        self.assert_closed()

    def test_timeout_cleans_spawned_worker_and_has_no_followup(self):
        (self.root/'mode').write_text('hang')
        report = self.run_measure(timeout=.3)
        self.assertEqual(report['stop_reason'], 'usage_unknown')
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['attempts'][0]['cli_timing']['status'], 'timeout')
        self.assertTrue(self.workers); self.assert_closed()

    def test_interrupt_after_spawn_collects_usage_before_cleanup(self):
        (self.root/'mode').write_text('wait')
        real_wait = threading.Event().wait
        interrupted = False
        def interrupt(seconds):
            nonlocal interrupted
            if interrupted or threading.current_thread() is not threading.main_thread():
                return real_wait(seconds)
            for _ in range(300):
                if (self.root/'started').exists():
                    (self.root/'release').write_text('yes')
                    interrupted = True
                    raise KeyboardInterrupt
                real_wait(.01)
            self.fail('synthetic worker never started')
        with patch('verinote.extraction_benchmark.time.sleep', side_effect=interrupt):
            report = self.run_measure()
        self.assertEqual(report['stop_reason'], 'interrupted')
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['attempts'][0]['tokens']['output'], 20)
        self.assertTrue(self.workers); self.assert_closed()

    def test_report_failure_after_spawn_still_closes_worker(self):
        original = ReportWriter.write
        def fail(writer, report):
            if report['attempts']: raise OSError('synthetic write failure')
            original(writer, report)
        with patch.object(ReportWriter, 'write', fail):
            with self.assertRaises(Stopped): self.run_measure()
        self.assertTrue(self.workers); self.assert_closed()
        self.assertEqual(len([r for r in self.transcript() if r['text'] != '/clear']), 1)

    def test_budget_stop_and_final_report_failure_close_live_batch_worker(self):
        report = self.run_measure(limits=Limits(requests=1, repetitions=3))
        self.assertEqual(report['stop_reason'], 'request_limit')
        self.assertEqual(len(report['attempts']), 1)
        self.assert_closed()
        original = ReportWriter.write
        def fail(writer, report):
            if report['summary'] is not None: raise OSError('final save failure')
            original(writer, report)
        with patch.object(ReportWriter, 'write', fail):
            with self.assertRaises(Stopped): self.run_measure()
        self.assert_closed()


if __name__ == '__main__': unittest.main()
