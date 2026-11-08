"""Startup-only Copilot extraction opt-in; no HTTP option or real model call."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_workflow as workflow

from verinote.server import Server, main


class CopilotServer(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_default_status_and_opt_in_routes_only_copilot_extract(self):
        with patch.object(self.service, 'start', return_value={'id': 'fake'}) as start:
            settings = self.request('/api/settings')
            self.assertEqual(settings['copilot_extraction'], {'enabled': False, 'concurrency': 1,
                             'agent': 'copilot', 'stage': 'extract', 'source': 'server_startup'})
            self.assertEqual(settings['extraction_policy']['agent'], 'claude')
            self.request('/api/documents/example/extract', 'POST', {'agent': 'copilot'})
            self.assertEqual(start.call_args.kwargs, {})
            self.server.copilot_extraction_concurrency = 2
            status = self.request('/api/settings')['copilot_extraction']
            self.assertTrue(status['enabled']); self.assertEqual(status['concurrency'], 2)
            for agent, stage in [('copilot','extract'), ('copilot','ontology'), ('claude','extract'), ('agy','extract')]:
                self.request(f'/api/documents/example/{stage}', 'POST', {'agent': agent})
                self.assertEqual(start.call_args.kwargs, {'extraction_concurrency': 2} if (agent, stage)==('copilot','extract') else {})

    def test_http_cannot_enable_override_or_disable(self):
        with patch.object(self.service, 'start') as start:
            for enabled in (None, 2):
                self.server.copilot_extraction_concurrency = enabled
                for key in ('copilot_extraction_concurrency', 'extraction_concurrency', 'extraction_policy'):
                    for value in (None, 1, 2):
                        with self.assertRaises(HTTPError) as error:
                            self.request('/api/documents/example/extract', 'POST', {'agent': 'copilot', key: value})
                        self.assertEqual(error.exception.code, 400); error.exception.close()
            with self.assertRaises(HTTPError) as error:
                self.request('/api/settings', 'PUT', {'copilot_extraction': {'enabled': True}})
            self.assertEqual(error.exception.code, 400); error.exception.close()
            start.assert_not_called()


class CopilotStartup(unittest.TestCase):
    def test_invalid_values_fail_before_binding_or_store_recovery(self):
        for value in (1, 0, True, '2'):
            with patch('verinote.server.ThreadingHTTPServer.__init__') as bind, self.assertRaises(ValueError):
                Server(('127.0.0.1', 0), Mock(), Mock(), copilot_extraction_concurrency=value)
            bind.assert_not_called()
        with patch('verinote.server.Store') as store, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--copilot-extraction-concurrency','1'])
        store.assert_not_called()

    def test_help_and_startup_report_scope_and_unknowns(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit): main(['--help'])
        for text in ('--copilot-extraction-concurrency', '공유 상태', '미확인', 'auto', '백오프'):
            self.assertIn(text, output.getvalue())
        for arguments, expected in [([], None), (['--copilot-extraction-concurrency','2'],2)]:
            output = io.StringIO()
            with patch('verinote.server.load_config'), patch('verinote.server.Store'), patch('verinote.server.Service'), patch('verinote.server.Server') as server, contextlib.redirect_stdout(output):
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                main(arguments)
            self.assertEqual(server.call_args.kwargs['copilot_extraction_concurrency'], expected)
            self.assertIn('agent=copilot, stage=extract, source=server_startup', output.getvalue())
            self.assertIn('Copilot extraction concurrency: '+ ('2' if expected else 'disabled (sequential 1)'), output.getvalue())
