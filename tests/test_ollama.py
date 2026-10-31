import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.agents import CLI, validate_model
from verinote.ollama import DEFAULTS, Job, options
from verinote.ollama import request as http_request
from verinote.usage import job_calls

MODEL = 'qwen2.5:7b'


class OllamaProvider(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.folder = self.root/'runs'/('a'*32)/'1'
        self.response = {'model': MODEL, 'done': True, 'done_reason': 'stop', 'response': '{"answer":"ok"}',
                         'prompt_eval_count': 12, 'eval_count': 4, 'total_duration': 123}
        self.calls = []
        def request(path, payload=None, **kwargs):
            self.calls.append((path, payload, kwargs))
            if path == '/api/tags': return {'models': [{'name': MODEL, 'digest': 'b'*64}]}
            return self.response
        self.mock = patch('verinote.ollama.request', side_effect=request); self.mock.start(); self.addCleanup(self.mock.stop)
        self.cli = CLI(persistent=False); self.addCleanup(self.cli.close)

    def test_local_dispatch_has_linked_records_and_unknown_cost_cache(self):
        self.assertEqual(self.cli.complete('ollama', MODEL, 'return JSON', self.folder), {'answer': 'ok'})
        meta = json.loads((self.folder/'run.json').read_text())
        usage = json.loads((self.folder/'usage.json').read_text())
        self.assertEqual(meta['transport'], 'local_http')
        self.assertEqual(meta['provider_call_id'], usage['provider_call_id'])
        self.assertEqual(usage['tokens']['input'], 12)
        self.assertIsNone(usage['tokens']['cache_read']); self.assertIsNone(usage['reported_usd'])
        owner = {'id': 'a'*32, 'agent': 'ollama', 'model': MODEL, 'stage': 'extract', 'status': 'success'}
        self.assertTrue(job_calls(self.root, 'runs', owner)[0]['complete'])
        payload = self.calls[-1][1]
        self.assertFalse(payload['stream']); self.assertEqual(payload['format'], 'json')
        self.assertNotIn('context', payload)

    def test_incomplete_and_malformed_outputs_retain_usage_but_fail(self):
        for key, value in [('done_reason', 'length'), ('model', 'other'), ('response', 'bad JSON')]:
            original = self.response[key]; self.response[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.cli.complete('ollama', MODEL, 'JSON', self.folder)
            usage = json.loads((self.folder/'usage.json').read_text())
            self.assertEqual(usage['status'], 'error'); self.assertEqual(usage['tokens']['output'], 4)
            self.response[key] = original

    def test_missing_usage_remains_unknown(self):
        del self.response['eval_count']
        self.cli.complete('ollama', MODEL, 'JSON', self.folder)
        usage = json.loads((self.folder/'usage.json').read_text())
        self.assertFalse(usage['complete']); self.assertIsNone(usage['tokens']['output'])

    def test_options_are_immutable_and_context_admission_precedes_generation(self):
        job = Job(self.cli); snapshot = options()
        limited = job.limited_timeout('ollama', 1)
        self.assertEqual(job.options, snapshot); self.assertEqual(limited.effective_timeout('ollama'), 1)
        bad = options(); bad['endpoint'] = 'http://example.com'
        with self.assertRaises(ValueError): options(bad)
        with self.assertRaises(ValueError): validate_model('', 'ollama')
        with self.assertRaises(ValueError): job.complete('ollama', MODEL, 'x'*32768, self.folder)
        self.assertEqual(self.calls, [])
        self.assertEqual(DEFAULTS, snapshot)

    def test_unavailable_model_never_generates(self):
        with self.assertRaises(ValueError): self.cli.complete('ollama', 'absent', 'JSON', self.folder)
        self.assertEqual([c[0] for c in self.calls], ['/api/tags'])

    def test_failure_records_are_terminal(self):
        for error in (TimeoutError('timeout'), OSError('unavailable')):
            with patch('verinote.ollama.request', side_effect=error), self.assertRaises(type(error)):
                self.cli.complete('ollama', MODEL, 'JSON', self.folder)
            self.assertEqual(json.loads((self.folder/'run.json').read_text())['status'], 'error')

    def test_http_boundary_rejects_redirects_and_oversized_response(self):
        from unittest.mock import MagicMock

        for status, chunks in [(302, []), (200, [b'x'*11])]:
            connection = MagicMock(); response = connection.getresponse.return_value
            response.status = status; response.read1.side_effect = chunks
            with patch('verinote.ollama.http.client.HTTPConnection', return_value=connection) as constructor:
                with self.assertRaises(ValueError): http_request('/api/generate', {}, timeout=1, limit=10)
                constructor.assert_called_once_with('127.0.0.1', 11434, timeout=1)
                connection.close.assert_called_once()

    def test_registered_digest_change_blocks_generation(self):
        job = Job(self.cli, model=MODEL)
        with patch('verinote.ollama.catalog', return_value=[{'id': MODEL, 'digest': 'c'*64}]), self.assertRaises(ValueError):
            job.complete('ollama', MODEL, 'JSON', self.folder)
        self.assertFalse(any(path == '/api/generate' for path, _, _ in self.calls))
