"""Server-selected local extraction policy, with no real model calls."""
import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_extraction_scheduler import Caller

from verinote.extraction_options import CHUNK_THRESHOLD_POLICY
from verinote.server import Server, main


class LocalPolicy(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_readonly_status_and_default_routing(self):
        with patch.object(self.service, 'start', return_value={'id': 'fake'}) as start:
            status = self.request('/api/settings')['extraction_policy']
            self.assertEqual(status, {'policy': None, 'agent': 'claude', 'stage': 'extract', 'source': 'server_startup'})
            start.assert_not_called()
            self.request('/api/documents/example/extract', 'POST', {'agent': 'claude', 'model': 'model'})
            self.assertNotIn('extraction_policy', start.call_args.kwargs)

    def test_only_claude_extract_receives_policy(self):
        self.server.claude_extraction_policy = CHUNK_THRESHOLD_POLICY
        with patch.object(self.service, 'start', return_value={'id': 'fake'}) as start:
            self.assertEqual(self.request('/api/settings')['extraction_policy']['policy'], CHUNK_THRESHOLD_POLICY)
            start.assert_not_called()
            for agent, stage in (('claude', 'extract'), ('claude', 'ontology'), ('agy', 'extract'), ('openrouter', 'extract')):
                self.request(f'/api/documents/example/{stage}', 'POST', {'agent': agent, 'model': 'model'})
                self.assertEqual(start.call_args.kwargs, {'extraction_policy': CHUNK_THRESHOLD_POLICY} if (agent, stage)==('claude', 'extract') else {})

    def test_client_cannot_override_policy_or_settings(self):
        self.server.claude_extraction_policy = CHUNK_THRESHOLD_POLICY
        with patch.object(self.service, 'start') as start:
            for key in ('extraction_policy', 'extraction_concurrency'):
                for value in (None, 1, CHUNK_THRESHOLD_POLICY):
                    with self.assertRaises(HTTPError) as error:
                        self.request('/api/documents/example/extract', 'POST', {'agent': 'claude', key: value})
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
            with self.assertRaises(HTTPError) as error:
                self.request('/api/settings', 'PUT', {'selected': 'claude', 'models': {}, 'extraction_policy': None})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            start.assert_not_called()

    def test_web_to_service_uses_three_and_four_original_chunks(self):
        self.server.claude_extraction_policy = CHUNK_THRESHOLD_POLICY
        owners = []
        def factory(spec):
            caller = Caller(lambda i, r: workflow.extraction_response(), spec.capacity)
            caller.extraction_spec = spec
            owners.append(caller)
            return caller
        self.service.extraction_caller_factory = factory
        for count in (3, 4):
            source = workflow.SOURCE.ljust(8000)*(count-1)+workflow.SOURCE
            doc = self.store.register('local', 'local.txt', source.encode(), source)
            workflow.approved_dictionary(self.store, doc['id'])
            with patch('verinote.extraction_transport.executable', return_value=sys.executable):
                run = self.request(f'/api/documents/{doc["id"]}/extract', 'POST',
                    {'agent': 'claude', 'model': 'model', 'expected_version': 1, 'expected_generation': 1})
            self.service.pool.submit(lambda: None).result(timeout=5)
            stored = self.store.run(run['id'])
            self.assertEqual(stored['status'], 'success', stored['detail'])
            self.assertEqual(stored['extraction_options']['original_chunk_count'], count)
            self.assertEqual(owners[-1].extraction_spec.capacity, 1 if count == 3 else 2)


class StartupPolicy(unittest.TestCase):
    def test_invalid_policy_rejected_before_binding_or_recovery(self):
        with patch('verinote.server.ThreadingHTTPServer.__init__') as bind:
            with self.assertRaises(ValueError): Server(('127.0.0.1', 0), Mock(), Mock(), claude_extraction_policy='bad')
            bind.assert_not_called()
        with patch('verinote.server.Store') as store, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--claude-extraction-policy', 'bad'])
        store.assert_not_called()

    def test_startup_flag_is_passed_and_reported(self):
        output = io.StringIO()
        with patch('verinote.server.load_config'), patch('verinote.server.Store'), patch('verinote.server.Service'), patch('verinote.server.Server') as server, contextlib.redirect_stdout(output):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(['--claude-extraction-policy', CHUNK_THRESHOLD_POLICY])
        self.assertEqual(server.call_args.kwargs['claude_extraction_policy'], CHUNK_THRESHOLD_POLICY)
        self.assertIn(f'{CHUNK_THRESHOLD_POLICY} (agent=claude, stage=extract, source=server_startup)', output.getvalue())
