"""Offline persistent Claude protocol, lifecycle and accounting contracts."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from verinote.agents import CLI
from verinote.claude_worker import command
from verinote.cli_timing import read_cli_timing

FAKE = r'''
import json,os,sys,time,uuid,subprocess
from pathlib import Path
root=Path(sys.argv[1]); session=uuid.uuid4().hex; history=[]
(root/'pid').write_text(str(os.getpid()))
mode=(root/'mode').read_text() if (root/'mode').exists() else ''
if mode=='no_read': time.sleep(30)
def emit(value):
    raw=(json.dumps(value,ensure_ascii=False)+'\n').encode()
    if mode=='fragment':
        for part in range(0,len(raw),3): os.write(1,raw[part:part+3])
    else: os.write(1,raw)
def result(body,**fields):
    return dict(type='result',subtype='success',is_error=False,session_id=session,result=body,
        usage=dict(input_tokens=10,output_tokens=2,cache_read_input_tokens=3,cache_creation_input_tokens=4),
        total_cost_usd=.25,modelUsage={'fake-model':{}},**fields)
for line in sys.stdin:
    text=json.loads(line)['message']['content']
    with (root/'transcript').open('a') as f: f.write(json.dumps(dict(pid=os.getpid(),text=text,history=history,session=session))+'\n')
    if text=='/clear':
        old=session;session=uuid.uuid4().hex;history=[]
        if mode!='no_reset': emit(dict(type='conversation_reset',session_id=old,new_conversation_id=uuid.uuid4().hex))
        if mode=='same_session': session=old
        reset=result('');reset['usage']={k:0 for k in reset['usage']};reset['total_cost_usd']=0;reset['modelUsage']={}
        if mode=='bad_baseline': reset['total_cost_usd']=.25
        emit(dict(type='system',subtype='init',session_id=session));emit(reset)
        if mode=='no_read_after_reset':time.sleep(30)
        continue
    if text=='hang':time.sleep(30)
    if text=='wait':
        (root/'waiting').write_text('yes')
        while not (root/'release').exists():time.sleep(.01)
    if text=='descendant':
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        (root/'child').write_text(str(child.pid));time.sleep(30)
    if text=='eof':sys.exit(7)
    if text=='oversize':os.write(1,b'x'*100000);time.sleep(30)
    if text=='stderr':os.write(2,b'x'*100000);time.sleep(30)
    if text=='badjson':os.write(1,b'not-json\n');continue
    if text=='control':emit(dict(type='control_request',request_id='x',request={'subtype':'can_use_tool'}));continue
    body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)
    history.append(text);value=result(json.dumps(body))
    if text=='foreign':value['session_id']='foreign-session'
    if text=='error':value['is_error']=True
    if text=='invalid_body':value['result']='bad json'
    emit(value)
    if text=='duplicate':emit(value)
'''


class PersistentClaude(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.script = self.root / 'fake.py'; self.script.write_text(FAKE)
        self.cli = CLI(timeout=3)
        self.addCleanup(self.cli.close)
        self.addCleanup(patch.stopall)
        patch('verinote.agents.executable', return_value=sys.executable).start()
        patch('verinote.claude_worker.command', side_effect=lambda binary, model: [binary, str(self.script), str(self.root), model]).start()
        self.count = 0

    def call(self, prompt='hello', *, cli=None, model='model'):
        self.count += 1
        return (cli or self.cli).complete('claude', model, prompt, self.root / str(self.count))

    def mode(self, mode):
        (self.root / 'mode').write_text(mode)

    def transcript(self):
        return [json.loads(line) for line in (self.root / 'transcript').read_text().splitlines()]

    def test_default_reuses_pid_across_copies_and_resets_context_and_usage(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-only'}), patch('verinote.claude_worker.subprocess.Popen', wraps=subprocess.Popen) as popen:
            first = self.call('한국어 one')
            second = self.call('two', cli=self.cli.limited_timeout('claude', 2))
            third = self.call('three', cli=copy.copy(self.cli))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual({first['pid'], second['pid'], third['pid']}, {first['pid']})
        self.assertEqual([first['history'], second['history'], third['history']], [[], [], []])
        self.assertFalse(first['key_present'])
        transcript = self.transcript()
        self.assertEqual([r['text'] for r in transcript], ['/clear', '한국어 one', '/clear', 'two', '/clear', 'three'])
        self.assertEqual(len({r['session'] for r in transcript if r['text'] != '/clear'}), 3)
        ids = set()
        for index in (1, 2, 3):
            folder = self.root / str(index)
            usage = json.loads((folder / 'usage.json').read_text())
            ids.add(usage['cli_call_id']); self.assertEqual(usage['tokens']['input'], 17)
            self.assertEqual(usage['tokens']['output'], 2); self.assertEqual(usage['reported_usd'], .25)
            timing = read_cli_timing(self.root, str(index))
            self.assertTrue(timing['available'], timing)
            self.assertEqual(timing['schema_version'], 2)
            self.assertEqual(timing['process_reused'], index > 1)
            self.assertIsNone(timing['exit_code']); self.assertIsNone(timing['offsets_seconds']['exit_observed'])
            self.assertEqual(timing['durations_seconds']['spawn'] is None, index > 1)
        self.assertEqual(len(ids), 3)
        self.assertNotIn('한국어 one', (self.root/'2'/'stream.jsonl').read_text())

    def test_model_change_retires_worker(self):
        self.call(); old = self.cli._claude.worker
        second = self.call(model='other')
        self.assertNotEqual(second['pid'], old.proc.pid)
        self.assertIsNotNone(old.proc.poll())
        self.assertFalse(Path(old.directory.name).exists())

    def test_fragmented_unicode(self):
        self.mode('fragment')
        self.assertEqual(self.call('안녕 🙂')['prompt'], '안녕 🙂')

    def test_reset_failure_never_dispatches_prompt(self):
        for mode in ('no_reset', 'same_session', 'bad_baseline'):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaises(ValueError): self.call('private')
                self.assertIsNone(self.cli._claude.worker)
                self.assertNotIn('private', [r['text'] for r in self.transcript()])
        self.mode(''); self.assertEqual(self.call()['history'], [])

    def test_failures_poison_worker_without_replaying_prompt_and_allow_next_call(self):
        for prompt in ('eof', 'badjson', 'foreign', 'control', 'error', 'invalid_body'):
            with self.subTest(prompt=prompt):
                before = len(self.transcript()) if (self.root/'transcript').exists() else 0
                with self.assertRaises(ValueError): self.call(prompt)
                self.assertIsNone(self.cli._claude.worker)
                self.assertEqual([r['text'] for r in self.transcript()[before:]], ['/clear', prompt])
                if prompt in ('error', 'invalid_body'):
                    usage = json.loads((self.root/str(self.count)/'usage.json').read_text())
                    self.assertEqual(usage['reported_usd'], .25)
                self.assertEqual(self.call()['history'], [])

    def test_late_duplicate_result_cannot_become_next_answer(self):
        self.call('duplicate')
        with self.assertRaises(ValueError): self.call('next')
        self.assertNotIn('next', [r['text'] for r in self.transcript()])
        self.assertEqual(self.call('fresh')['prompt'], 'fresh')

    def test_output_limits_cover_stderr_and_unterminated_stdout(self):
        for prompt in ('oversize', 'stderr'):
            with self.subTest(prompt=prompt):
                limited = copy.copy(self.cli); limited.max_output = 2000
                with self.assertRaisesRegex(ValueError, '크기 제한'): self.call(prompt, cli=limited)
                self.assertIsNone(self.cli._claude.worker)
                for name in ('stream.jsonl', 'stderr.txt'):
                    self.assertLessEqual((self.root/str(self.count)/name).stat().st_size, 2000)

    def test_timeout_and_blocked_stdin_are_bounded(self):
        limited = self.cli.limited_timeout('claude', .2)
        with self.assertRaises(TimeoutError): self.call('hang', cli=limited)
        self.assertIsNone(self.cli._claude.worker)
        self.mode('no_read_after_reset')
        started = time.monotonic()
        with self.assertRaises(TimeoutError): self.call('x'*2000000, cli=limited)
        self.assertLess(time.monotonic()-started, 3)
        self.assertIsNone(self.cli._claude.worker)

    def test_concurrent_calls_serialize_and_keep_separate_results(self):
        results = []; errors = []
        def run(prompt):
            try:
                results.append(self.cli.complete('claude', 'model', prompt, self.root/prompt))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(p,)) for p in ('alpha', 'beta')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        self.assertFalse(errors)
        self.assertEqual({r['prompt'] for r in results}, {'alpha', 'beta'})
        self.assertEqual(len({r['pid'] for r in results}), 1)
        self.assertTrue(all(r['history'] == [] for r in results))

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group observation')
    def test_timeout_kills_descendants(self):
        with self.assertRaises(TimeoutError):
            self.call('descendant', cli=self.cli.limited_timeout('claude', .3))
        pid = int((self.root/'child').read_text())
        # A reaped child is absent; a zombie is also no longer executing.
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)

    def test_active_close_stops_exchange_and_readers(self):
        errors = []
        def run():
            try: self.call('wait')
            except ValueError as exc: errors.append(exc)
        thread = threading.Thread(target=run); thread.start()
        end = time.monotonic()+2
        while not (self.root/'waiting').exists() and time.monotonic()<end: time.sleep(.01)
        self.assertTrue((self.root/'waiting').exists())
        worker = self.cli._claude.worker
        self.cli.close(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(len(errors), 1)
        self.assertIsNotNone(worker.proc.poll())

    def test_queued_timeout_does_not_kill_active_owner(self):
        results = []; errors = []
        def run():
            try: results.append(self.call('wait'))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=run); thread.start()
        try:
            end = time.monotonic()+2
            while not (self.root/'waiting').exists() and time.monotonic()<end: time.sleep(.01)
            self.assertTrue((self.root/'waiting').exists())
            with self.assertRaises(TimeoutError): self.call('queued', cli=self.cli.limited_timeout('claude', .05))
            self.assertIsNone(self.cli._claude.worker.proc.poll())
        finally:
            (self.root/'release').write_text('yes'); thread.join(3)
        self.assertFalse(errors); self.assertEqual(results[0]['prompt'], 'wait')
        self.assertNotIn('queued', [r['text'] for r in self.transcript()])
        self.assertEqual(self.call()['history'], [])

    def test_close_reaps_and_is_idempotent_and_closes_copies(self):
        self.call(); worker = self.cli._claude.worker
        copy.copy(self.cli).close(); self.cli.close()
        self.assertIsNotNone(worker.proc.poll()); self.assertFalse(Path(worker.directory.name).exists())
        self.assertTrue(all(not t.is_alive() for t in worker.threads))
        with self.assertRaises(ValueError): self.call()

    def test_fixed_command_preserves_isolation_flags(self):
        args = command('/claude', 'model')
        for option, value in (('--tools', ''), ('--input-format', 'stream-json'), ('--output-format', 'stream-json'), ('--setting-sources', ''), ('--model', 'model')):
            self.assertEqual(args[args.index(option)+1], value)
        self.assertIn('--strict-mcp-config', args); self.assertIn('--no-session-persistence', args)
        self.assertIn('{"disableAllHooks":true}', args)

    def test_service_closes_transport_after_draining_pool(self):
        from verinote.service import Service
        service = Service.__new__(Service)
        service.extraction_lock = threading.RLock(); service.extraction_owner = None; service.closed = False
        calls = []
        service.pool = Mock()
        service.pool.shutdown.side_effect = lambda **_: calls.append('drained')
        service.cli = Mock()
        service.cli.close.side_effect = lambda: calls.append('closed')
        service.close()
        self.assertEqual(calls, ['drained', 'closed'])
        service.pool.shutdown.assert_called_once_with(wait=True)


if __name__ == '__main__':
    unittest.main()
