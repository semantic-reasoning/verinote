import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from verinote.agents import CLI, CLI_PROVIDERS, PROVIDERS, TRANSPORTS, command, executable, validate_model
from verinote.service import Service
from verinote.transports import Credentials, ExecutionOptions, HTTPResult, SafeCallResult


class FakeHTTP:
    """Fixture for #705/#706: terminal envelope precedes domain parsing."""
    def __init__(self, response='{"ok":true}', status='success', verified=True):
        self.response, self.status, self.verified = response, status, verified
        self.calls = []

    def ready(self, credentials):
        return bool(credentials.api_key)

    def complete(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return HTTPResult(SafeCallResult(
            kwargs['call_id'], self.status, {'usage': {'total_tokens': 12}},
            self.verified, {'usage': {'total_tokens': 12}} if self.verified and self.status == 'success' else None), self.response)


class Transports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def folder(self):
        self.counter += 1
        return Path(self.tmp.name).resolve()/str(self.counter)

    def test_copilot_windows_installations_outside_inherited_path(self):
        root = Path(self.tmp.name).resolve()
        environment = {'PATH': str(root / 'empty'), 'LOCALAPPDATA': str(root / 'local'),
                       'APPDATA': str(root / 'roaming')}
        for directory, name in [(root / 'local' / 'Microsoft' / 'WinGet' / 'Links', 'copilot.exe'),
                                (root / 'roaming' / 'npm', 'copilot.cmd')]:
            with self.subTest(installation=name):
                directory.mkdir(parents=True)
                binary = directory / name
                binary.write_text('')
                binary.chmod(0o700)
                with patch.dict(os.environ, environment), patch('verinote.agents.sys.platform', 'win32'):
                    self.assertEqual(executable('copilot'), str(binary))
                    self.assertTrue(CLI().ready('copilot'))
                binary.unlink()

    def test_copilot_path_takes_precedence_over_installation_fallback(self):
        with patch('verinote.agents.sys.platform', 'win32'), patch('verinote.agents.shutil.which', return_value='chosen-copilot') as which:
            self.assertEqual(executable('copilot'), 'chosen-copilot')
            which.assert_called_once_with('copilot')

    def test_copilot_missing_installation_is_not_reported_as_installed(self):
        root = Path(self.tmp.name).resolve()
        with patch.dict(os.environ, {'PATH': str(root), 'LOCALAPPDATA': str(root), 'APPDATA': str(root)}), patch('verinote.agents.sys.platform', 'win32'):
            self.assertIsNone(executable('copilot'))
            self.assertFalse(CLI().ready('copilot'))

    def test_copilot_non_windows_uses_path_only(self):
        with patch('verinote.agents.sys.platform', 'linux'), patch('verinote.agents.shutil.which', return_value=None) as which:
            self.assertIsNone(executable('copilot'))
            which.assert_called_once_with('copilot')

    def test_http_dispatch_records_without_cli_effects(self):
        adapter, events = FakeHTTP(), []
        options, credentials = ExecutionOptions(timeout=600), Credentials('synthetic-only')
        cli = CLI(http_adapter=adapter, credentials=credentials, sink=events.append, options=options)
        with tempfile.TemporaryDirectory() as tmp, patch('verinote.agents.subprocess.Popen') as popen, patch('verinote.agents.shutil.which') as which:
            folder = Path(tmp).resolve() / '1'
            for index in range(2):
                target = folder.parent / str(index + 1)
                self.assertEqual(cli.complete('openrouter', ' vendor/model ', 'prompt', target, phase='ontology'), {'ok': True})
                self.assertTrue((target/'run.json').exists())
                self.assertFalse((target/'stdout.txt').exists())
            popen.assert_not_called(); which.assert_not_called()
        args, kwargs = adapter.calls[0]
        self.assertEqual(args, ('openrouter', 'vendor/model', 'prompt', folder))
        self.assertEqual(kwargs['phase'], 'ontology')
        self.assertIs(kwargs['options'], options)
        self.assertIs(kwargs['credentials'], credentials)
        self.assertNotEqual(events[0].call_id, events[1].call_id)
        self.assertEqual(events[0].safe_response_payload['usage']['total_tokens'], 12)
        self.assertNotIn('synthetic-only', repr(credentials))
        self.assertEqual(set(dataclasses.asdict(options)), {'adapter_contract_version', 'max_tokens', 'timeout'})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.max_tokens = 1

    def test_terminal_evidence_survives_parsing_and_sink_failure(self):
        events = []
        cli = CLI(http_adapter=FakeHTTP('broken JSON'), credentials=Credentials('synthetic'), sink=events.append)
        with self.assertRaises(json.JSONDecodeError):
            cli.complete('openrouter', 'model', 'prompt', self.folder())
        self.assertEqual(events[0].transport_status, 'success')
        cli.sink = Mock(side_effect=OSError('sink unavailable'))
        with self.assertRaises(json.JSONDecodeError):
            cli.complete('openrouter', 'model', 'prompt', self.folder())
        cli.http_adapter.response = '{"ok":true}'
        self.assertEqual(cli.complete('openrouter', 'model', 'prompt', self.folder()), {'ok': True})
        for status, error in [('error', ValueError), ('timeout', TimeoutError), ('not_started', ValueError)]:
            cli.http_adapter = FakeHTTP(None, status, False)
            cli.sink = events.append
            with self.assertRaises(error):
                cli.complete('openrouter', 'model', 'prompt', self.folder())
            self.assertIsNone(events[-1].safe_response_payload)
            self.assertEqual(events[-1].transport_status, status)
        with self.assertRaises(ValueError):
            SafeCallResult('call', 'success', response_verified=False, safe_response_payload={})
        with self.assertRaises(ValueError):
            SafeCallResult('call', 'timeout', response_verified=True, safe_response_payload={})

    def test_configuration_models_and_timeouts(self):
        for cli in (CLI(), CLI(http_adapter=FakeHTTP()), CLI(credentials=Credentials('synthetic'))):
            with self.assertRaisesRegex(ValueError, '구성되지'):
                cli.complete('openrouter', 'model', 'prompt', self.folder())
        with patch('verinote.agents.shutil.which') as which:
            self.assertIsNone(executable('openrouter'))
            which.assert_not_called()
        with self.assertRaises(ValueError):
            command('openrouter', 'model', 'prompt')
        self.assertEqual(PROVIDERS['openrouter'], 'OpenRouter (API)')
        self.assertEqual(TRANSPORTS['openrouter'], 'http')
        for model in ('', ' ', 'a'*151, 'model\n', '\tmodel', 'model\x00', 'model\x7f'):
            with self.assertRaises(ValueError):
                validate_model(model, 'openrouter')
        for agent in CLI_PROVIDERS:
            self.assertEqual(validate_model('', agent), '')
            self.assertEqual(CLI().effective_timeout(agent), 600 if agent == 'agy' else 180)
        self.assertEqual(CLI().execution_options('openrouter').max_tokens, 16384)
        self.assertEqual(CLI().effective_timeout('openrouter'), 180)
        self.assertEqual(CLI().effective_timeout('openrouter', 'ontology'), 600)
        for override in (0, .1, 120):
            for phase in (None, 'ontology'):
                self.assertEqual(CLI(timeout=override).effective_timeout('openrouter', phase), override)
        with self.assertRaises(ValueError):
            CLI().effective_timeout('openrouter', 'unknown')
        for kwargs in ({'max_tokens': 0}, {'adapter_contract_version': 2}, {'timeout': float('nan')}, {'timeout': -1}):
            with self.assertRaises(ValueError):
                ExecutionOptions(**kwargs)

    def test_invalid_http_model_has_no_side_effects(self):
        adapter = FakeHTTP()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'invalid'
            with self.assertRaises(ValueError):
                CLI(http_adapter=adapter, credentials=Credentials('synthetic')).complete('openrouter', '', 'p', folder)
            self.assertFalse(folder.exists())
            self.assertEqual(adapter.calls, [])

    def test_service_rejects_empty_http_model_before_queueing(self):
        service = Service.__new__(Service)
        service.slots = Mock()
        service.store = Mock()
        service.store.settings.return_value = {'selected': 'openrouter', 'models': {}}
        with self.assertRaises(ValueError):
            service.start('document', 'openrouter', ' ')
        with self.assertRaises(ValueError):
            service.ask({'question': 'question'})
        service.slots.acquire.assert_not_called()
        service.store.create_run.assert_not_called()
        service.store.create_question.assert_not_called()

    def test_cli_child_environment_removes_key_and_preserves_parent(self):
        proc = Mock(returncode=0)
        proc.poll.return_value = 0
        def start(*args, **kwargs):
            self.assertNotIn('OPENROUTER_API_KEY', kwargs['env'])
            self.assertEqual(kwargs['env']['TRANSPORT_TEST'], 'preserved')
            kwargs['stdout'].write(json.dumps({'status': 'SUCCESS', 'response': json.dumps({'ok': True})}).encode())
            return proc
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-only', 'TRANSPORT_TEST': 'preserved'}), patch('verinote.agents.executable', return_value='/fake'), patch('verinote.agents.subprocess.Popen', side_effect=start):
            self.assertEqual(CLI().complete('agy', '', 'prompt', tmp), {'ok': True})
            self.assertEqual(os.environ['OPENROUTER_API_KEY'], 'synthetic-only')
            for path in Path(tmp).rglob('*'):
                if path.is_file():
                    self.assertNotIn('synthetic-only', path.read_text())
