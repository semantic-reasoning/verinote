"""Offline CLI timing, observation semantics and optional-failure isolation."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.agents import CLI
from verinote.cli_timing import CallTiming, read_cli_timing, validate_timing
from verinote.usage import job_calls

OK = "import json; print(json.dumps({'status':'SUCCESS','response':json.dumps({'ok':True})}))"


class Timing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'runs' / ('a'*32) / '1'
        self.owner = {'id': 'a'*32, 'agent': 'agy', 'model': 'test-model', 'stage': 'extract', 'status': 'success'}

    def program(self, body):
        script = self.root / 'fake_cli.py'
        script.write_text(body, encoding='utf-8')
        return script

    def invoke(self, body, *, timeout=2, max_output=100000, folder=None):
        script = self.program(body)
        with patch('verinote.agents.executable', return_value=sys.executable), \
                patch('verinote.agents.command', return_value=([sys.executable, str(script)], '')):
            return CLI(timeout=timeout, max_output=max_output).complete('agy', 'test-model', 'PRIVATE_PROMPT', folder or self.folder)

    def read(self):
        return read_cli_timing(self.root, f'runs/{self.owner["id"]}/1')

    def test_success_fast_final_output_and_usage_identity(self):
        response = self.invoke("import json; print(json.dumps({'status':'SUCCESS','response':'{\"ok\":true}'}))")
        self.assertEqual(response, {'ok': True})
        report = self.read(); self.assertTrue(report['available']); self.assertEqual(report['status'], 'success')
        marks = report['offsets_seconds']
        self.assertIsNotNone(marks['first_stdout'])
        self.assertLessEqual(marks['spawn_requested'], marks['spawn_returned'])
        self.assertLessEqual(marks['exit_observed'], marks['interpret_started'])
        self.assertLessEqual(marks['interpret_started'], marks['interpret_completed'])
        self.assertLessEqual(marks['interpret_completed'], marks['finished'])
        self.assertIsNone(report['cli_version'])
        call = job_calls(self.root, 'runs', self.owner)[0]
        self.assertEqual(call['cli_call_id'], report['cli_call_id'])
        for name in ('run.json', 'usage.json'):
            self.assertEqual(json.loads((self.folder / name).read_text())['cli_call_id'], report['cli_call_id'])
        raw = (self.folder / 'cli-timing.json').read_text()
        self.assertNotIn('PRIVATE_PROMPT', raw); self.assertNotIn(str(self.root), raw)
        self.assertNotIn('tokens', report); self.assertNotIn('cost', report)

    def test_empty_stderr_only_and_nonzero(self):
        for index, body in enumerate(('pass', 'import sys; print("PRIVATE_STDERR",file=sys.stderr)', 'import sys; sys.exit(7)')):
            self.folder = self.root / 'runs' / ('a'*32) / str(index+1)
            with self.assertRaises(ValueError): self.invoke(body)
            report = read_cli_timing(self.root, f'runs/{self.owner["id"]}/{index+1}')
            self.assertTrue(report['available']); self.assertEqual(report['status'], 'error')
            self.assertIsNone(report['offsets_seconds']['first_stdout'])
            self.assertEqual(report['error_code'], 'exit_nonzero' if index == 2 else 'interpretation')
            self.assertNotIn('PRIVATE_STDERR', json.dumps(report))

    def test_missing_executable_and_spawn_failure(self):
        for executable in (None, '/no/such/PRIVATE_EXECUTABLE'):
            with patch('verinote.agents.executable', return_value=executable):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    CLI().complete('agy', 'test-model', 'PRIVATE_PROMPT', self.folder)
            report = self.read(); self.assertTrue(report['available']); self.assertEqual(report['status'], 'not_started')
            self.assertIsNone(report['offsets_seconds']['spawn_returned'])
            self.assertEqual(report['error_code'], 'executable_missing' if executable is None else 'spawn')
            self.assertNotIn('PRIVATE_EXECUTABLE', json.dumps(report))

    def test_timeout_cleanup_and_last_stdout(self):
        with self.assertRaises(TimeoutError):
            self.invoke('import time; print("PRIVATE_OUTPUT",flush=True); time.sleep(10)', timeout=.8)
        report = self.read(); self.assertTrue(report['available']); self.assertEqual(report['status'], 'timeout')
        self.assertIsNotNone(report['offsets_seconds']['first_stdout'])
        self.assertIsNotNone(report['exit_code'])
        self.assertLess(report['durations_seconds']['total'], 3)
        self.assertNotIn('PRIVATE_OUTPUT', json.dumps(report))

    def test_parse_and_size_failures(self):
        for index, (body, limit, code) in enumerate((('print("not-json")', 100000, 'interpretation'),
                                                                  ("import json; print(json.dumps({'status':'SUCCESS','response':'not-json'}))", 100000, 'interpretation'), ('print("x"*10000)', 100, 'output_limit'))):
            self.folder = self.root / 'runs' / ('a'*32) / str(index+1)
            with self.assertRaises(ValueError): self.invoke(body, max_output=limit)
            report = read_cli_timing(self.root, f'runs/{self.owner["id"]}/{index+1}')
            self.assertTrue(report['available']); self.assertEqual(report['error_code'], code)
            self.assertIsNone(report['offsets_seconds']['interpret_completed'])

    def test_optional_write_failure_keeps_primary_success_and_failure(self):
        original = __import__('verinote.http_records', fromlist=['write_record']).write_record
        def fail(folder, name, value, limit):
            if name == 'cli-timing.json': raise OSError('PRIVATE_EXCEPTION')
            return original(folder, name, value, limit)
        with patch('verinote.http_records.write_record', side_effect=fail):
            self.assertEqual(self.invoke(OK), {'ok': True})
            self.assertFalse(self.read()['available'])
            with self.assertRaisesRegex(TimeoutError, '시간 제한'):
                self.invoke('import time; time.sleep(10)', timeout=.05)
        self.assertEqual(json.loads((self.folder / 'usage.json').read_text())['status'], 'error')

    def test_clock_and_constructor_failures_are_optional(self):
        with patch('verinote.cli_timing.time.monotonic_ns', side_effect=OSError('PRIVATE_CLOCK')):
            self.assertEqual(self.invoke(OK), {'ok': True})
        self.assertFalse(self.read()['available'])
        with patch('verinote.cli_timing.CallTiming', side_effect=RuntimeError('PRIVATE_CREATION')):
            self.assertEqual(self.invoke(OK), {'ok': True})
        self.assertFalse(self.read()['available'])
        with patch('verinote.cli_timing.time.monotonic_ns', side_effect=OSError('PRIVATE_CLOCK')):
            with self.assertRaises(TimeoutError): self.invoke('import time; time.sleep(10)', timeout=.05)
        self.assertFalse(self.read()['available'])

    def test_mid_call_clock_failure_preserves_timeout_cleanup(self):
        from verinote.agents import _kill_process_tree
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls > 3: raise OSError('PRIVATE_MID_CALL_CLOCK')
            return calls * 1000
        with patch('verinote.cli_timing.time.monotonic_ns', side_effect=clock), patch('verinote.agents._kill_process_tree', wraps=_kill_process_tree) as kill:
            with self.assertRaises(TimeoutError): self.invoke('import time; time.sleep(10)', timeout=.1)
        self.assertTrue(kill.called); self.assertIsNotNone(kill.call_args.args[0].poll())
        self.assertFalse(self.read()['available'])

    def test_partial_files_and_documented_schema(self):
        self.invoke(OK)
        for name in ('run.json', 'usage.json'):
            path = self.folder / name; saved = path.read_bytes(); path.unlink()
            self.assertFalse(self.read()['available']); path.write_bytes(saved)
        run_path = self.folder / 'run.json'; original = run_path.read_text()
        run = json.loads(original); run['cli_call_id'] = 'd'*32; run_path.write_text(json.dumps(run))
        self.assertFalse(self.read()['available']); run_path.write_text(original)
        self.assertTrue(self.read()['available'])
        example = Path('docs/cli-timing.md').read_text().split('```json\n', 1)[1].split('```', 1)[0]
        self.assertEqual(validate_timing(json.loads(example))['schema_version'], 1)

    def test_record_reader_missing_stale_id_corruption_and_symlink(self):
        self.invoke(OK)
        path = self.folder / 'cli-timing.json'; original = path.read_text()
        for change in (lambda v: v.update(schema_version=True), lambda v: v.update(cli_call_id='b'*32),
                       lambda v: v['offsets_seconds'].update(finished=-1), lambda v: v.update(agent='openrouter'),
                       lambda v: v['offsets_seconds'].update(spawn_returned=None),
                       lambda v: v['offsets_seconds'].update(finished=10**1000)):
            value = json.loads(original); change(value); path.write_text(json.dumps(value))
            self.assertFalse(self.read()['available'])
        path.write_text(original); usage_path = self.folder / 'usage.json'; usage = usage_path.read_text()
        raw = json.loads(usage); raw['cli_call_id'] = 'c'*32; usage_path.write_text(json.dumps(raw))
        self.assertFalse(self.read()['available']); usage_path.write_text(usage)
        path.write_text('x'*9000); self.assertFalse(self.read()['available'])
        path.unlink(); self.assertFalse(self.read()['available'])
        target = self.root / 'target'; target.write_text(original)
        try: path.symlink_to(target)
        except (OSError, NotImplementedError): self.skipTest('Symlink creation unavailable')
        self.assertFalse(self.read()['available'])

    def test_monotonic_offsets_zero_and_clock_regression(self):
        record = CallTiming('agy', '', None, 2, 1000, clock=iter([10**9, 10**9, 2*10**9, 3*10**9, 4*10**9, 5*10**9, 6*10**9]).__next__)
        for name in ('spawn_requested', 'spawn_returned', 'exit_observed', 'interpret_started', 'interpret_completed', 'finished'):
            record.mark(name)
        record.outcome('success', exit_code=0)
        value = validate_timing(record.value)
        self.assertEqual(value['offsets_seconds']['spawn_requested'], 0)
        self.assertEqual(value['offsets_seconds']['spawn_returned'], 1)
        self.assertEqual(value['offsets_seconds']['finished'], 5)
        self.assertIsNone(value['offsets_seconds']['first_stdout'])
        regressed = CallTiming('agy', '', None, 2, 1000, clock=iter([10, 9]).__next__)
        regressed.mark('spawn_requested'); self.assertTrue(regressed.disabled)
        malformed = copy.deepcopy(value); malformed['offsets_seconds']['interpret_completed'] = 0
        with self.assertRaises(ValueError): validate_timing(malformed)

    def test_http_path_does_not_create_cli_record(self):
        from test_http_records import Fake

        from verinote.transports import Credentials
        CLI(http_adapter=Fake(), credentials=Credentials('synthetic-secret')).complete('openrouter', 'vendor/model', 'PRIVATE_PROMPT', self.folder)
        self.assertFalse((self.folder / 'cli-timing.json').exists())

    def test_no_extra_process_for_cli_version_or_sensitive_environment(self):
        with patch.dict(os.environ, {'PRIVATE_TOKEN': 'PRIVATE_ENV'}), patch('verinote.agents.subprocess.Popen', wraps=subprocess.Popen) as popen:
            self.assertEqual(self.invoke(OK), {'ok': True})
        self.assertEqual(popen.call_count, 1)
        report = self.read(); self.assertTrue(report['available'])
        self.assertNotIn('PRIVATE_ENV', json.dumps(report)); self.assertIsNone(report['cli_version'])
