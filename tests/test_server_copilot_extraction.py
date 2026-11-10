"""Persisted Copilot concurrency, startup fallback, and frozen new-job settings; no model calls."""
import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_workflow import SOURCE, approved_dictionary

from verinote.server import Server, main
from verinote.store import Store


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

    def test_per_job_http_override_remains_forbidden(self):
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

    def test_saved_setting_overrides_startup_for_new_copilot_extractions_only(self):
        body = {'selected':'copilot', 'models':{}}
        for startup in (None, 2):
            self.server.copilot_extraction_concurrency = startup
            for concurrency in (2, 1):
                self.request('/api/settings', 'PUT', body | {'copilot_extraction_concurrency':concurrency})
                status = self.request('/api/settings')['copilot_extraction']
                self.assertEqual((status['concurrency'], status['enabled'], status['source']), (concurrency, concurrency==2, 'settings'))
                with patch.object(self.service, 'start', return_value={'id':'fake'}) as start:
                    for agent, stage in [('copilot','extract'), ('copilot','ontology'), ('claude','extract'), ('agy','extract')]:
                        self.request(f'/api/documents/example/{stage}', 'POST', {'agent':agent})
                        expected = {'extraction_concurrency':2} if (agent, stage, concurrency)==('copilot','extract',2) else {}
                        self.assertEqual(start.call_args.kwargs, expected)

    def test_optional_settings_survive_older_writers_and_store_recreation(self):
        body = {'selected':'copilot', 'models':{}}
        self.request('/api/settings', 'PUT', body)
        self.assertNotIn('copilot_extraction_concurrency', self.store.settings())
        self.request('/api/settings', 'PUT', body | {'copilot_extraction_concurrency':2})
        self.request('/api/settings', 'PUT', body | {'dictionary_context_bytes':1234})
        self.request('/api/settings', 'PUT', body | {'copilot_extraction_concurrency':1})
        saved = self.request('/api/settings', 'PUT', body | {'models':{'copilot':'auto'}})
        self.assertEqual(saved['copilot_extraction_concurrency'], 1)
        self.assertEqual(saved['dictionary_context_bytes'], 1234)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.settings(), saved)
        restarted = Server(('127.0.0.1', 0), reopened, self.service, copilot_extraction_concurrency=2)
        try:
            self.assertEqual(restarted.copilot_extraction_settings(reopened.settings())['concurrency'], 1)
        finally: restarted.server_close()

    def test_invalid_combined_setting_updates_are_atomic(self):
        body = {'selected':'copilot', 'models':{}, 'dictionary_context_bytes':1234, 'copilot_extraction_concurrency':2}
        self.request('/api/settings', 'PUT', body)
        for bad in (None, True, False, '2', 2.0, 0, 3, [], {}):
            with self.subTest(bad=bad), self.assertRaises(HTTPError) as caught:
                self.request('/api/settings', 'PUT', body | {'dictionary_context_bytes':4567, 'copilot_extraction_concurrency':bad})
            self.assertEqual(caught.exception.code, 400); caught.exception.close()
            self.assertEqual(self.store.settings(), body)
            with self.assertRaises(ValueError): self.store.save_settings(body | {'copilot_extraction_concurrency':bad})
            self.assertEqual(self.store.settings(), body)

    def test_registered_job_keeps_its_spec_when_web_setting_changes(self):
        docs = [self.store.register('synthetic', f'{i}.md', SOURCE.encode(), SOURCE) for i in range(2)]
        for doc in docs: approved_dictionary(self.store, doc['id'])
        body = {'selected':'copilot', 'models':{}}
        with patch.object(self.service.pool, 'submit') as submit, patch('verinote.copilot_extraction.executable', return_value=sys.executable):
            self.request('/api/settings', 'PUT', body | {'copilot_extraction_concurrency':2})
            first = self.request(f'/api/documents/{docs[0]["id"]}/extract', 'POST', {'agent':'copilot','model':'auto','expected_version':1,'expected_generation':1})
            pinned = self.store.run(first['id'])['extraction_options']
            self.assertEqual(pinned['concurrency'], 2)
            self.request('/api/settings', 'PUT', body | {'copilot_extraction_concurrency':1})
            second = self.request(f'/api/documents/{docs[1]["id"]}/extract', 'POST', {'agent':'copilot','model':'auto','expected_version':1,'expected_generation':1})
            self.assertIsNone(self.store.run(second['id'])['extraction_options'])
            self.assertEqual(self.store.run(first['id'])['extraction_options'], pinned)
            self.assertEqual(submit.call_args_list[0].args[1]['extraction_options'], pinned)
            self.assertIsNone(submit.call_args_list[1].args[1]['extraction_options'])


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
        for arguments, expected, effective, source in [([], None, 1, 'server_startup'),
                (['--copilot-extraction-concurrency','2'], 2, 2, 'server_startup'),
                (['--copilot-extraction-concurrency','2'], 2, 1, 'settings')]:
            output = io.StringIO()
            with patch('verinote.server.load_config'), patch('verinote.server.Store'), patch('verinote.server.Service'), patch('verinote.server.Server') as server, contextlib.redirect_stdout(output):
                server.return_value.copilot_extraction_settings.return_value = {'concurrency':effective, 'source':source}
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                main(arguments)
            self.assertEqual(server.call_args.kwargs['copilot_extraction_concurrency'], expected)
            self.assertIn(f'agent=copilot, stage=extract, source={source}', output.getvalue())
            self.assertIn(f'Copilot extraction concurrency at startup: {effective}', output.getvalue())
