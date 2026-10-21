"""Requested reasoning budgets and answer headroom, not model compliance claims."""
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_openrouter_integration as integration
from test_openrouter import Connection, Response, completion
from test_openrouter_pipeline import Fixture, dictionary
from test_workflow import FACTS, SOURCE, approved_dictionary, extraction_response

from verinote.job_transport import capture_options
from verinote.transports import ExecutionOptions, job_options, reasoning_budget
from verinote.usage import job_calls


def protected(total=16384, reserve=8192):
    return {'adapter_contract_version': 2, 'max_tokens': total,
            'timeouts': {'default': 180, 'ontology': 600},
            'answer_reserve_tokens': reserve, 'reasoning_max_tokens': reasoning_budget(total, reserve)}


class BudgetContract(unittest.TestCase):
    def test_v1_unchanged_and_v2_strict_validation(self):
        legacy = {'adapter_contract_version': 1, 'max_tokens': 321, 'timeouts': {'default': 180, 'ontology': 600}}
        self.assertEqual(job_options(legacy), legacy)
        self.assertEqual(job_options(protected()), protected())
        self.assertEqual(reasoning_budget(2048, 1024), 1024)
        self.assertEqual(reasoning_budget(32203, 8192), 4096)
        self.assertEqual(reasoning_budget(65536, 8192), 4096)
        for field in ('answer_reserve_tokens', 'reasoning_max_tokens'):
            for value in (None, True, False, 1024.0, '1024', 0, -1):
                broken = protected(); broken[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    job_options(broken)
            missing = protected(); del missing[field]
            with self.assertRaises(ValueError):
                job_options(missing)
        for broken in [legacy | {'answer_reserve_tokens': 1024}, protected() | {'extra': 1},
                       protected() | {'adapter_contract_version': 3}, protected() | {'reasoning_max_tokens': 4095}]:
            with self.assertRaises(ValueError):
                job_options(broken)
        for reserve in (1023, 15361, None, True):
            with self.assertRaises(ValueError):
                reasoning_budget(16384, reserve)
        with self.assertRaises(ValueError):
            ExecutionOptions(reasoning_max_tokens=4096, answer_reserve_tokens=8192)


class ProtectedJobs(unittest.TestCase):
    setUp = integration.Integration.setUp
    close = integration.Integration.close
    request = integration.Integration.request
    upload = integration.Integration.upload
    finish = integration.Integration.finish
    audit = integration.Integration.audit

    def start(self, doc, stage='ontology', **extra):
        return self.request(f'/api/documents/{doc["id"]}/{stage}', 'POST', {
            'agent': 'openrouter', 'model': 'openrouter/auto',
            'expected_version': doc['version'], 'expected_generation': doc['generation'],
            'answer_reserve_tokens': 8192, **extra})

    def test_invalid_budgets_rejected_before_registration(self):
        doc = self.upload()
        for value in (None, True, False, '8192', 8192.0, 0, 1023, 15361):
            with self.subTest(value=value), self.assertRaises(HTTPError) as caught:
                self.start(doc, answer_reserve_tokens=value)
            self.assertEqual(caught.exception.code, 400); caught.exception.close()
        for extra in ({'agent': 'agy', 'model': ''}, {'max_tokens': 2047, 'answer_reserve_tokens': 1024}):
            with self.assertRaises(HTTPError) as caught:
                self.start(doc, **extra)
            self.assertEqual(caught.exception.code, 400); caught.exception.close()
        self.service.cli.options = ExecutionOptions(max_tokens=1000)
        with self.assertRaises(HTTPError) as caught:
            self.start(doc, answer_reserve_tokens=1024)
        self.assertEqual(caught.exception.code, 400); caught.exception.close()
        self.assertEqual(self.store.runs(), []); self.assertEqual(self.queue.tasks, [])

    def test_queued_policy_capture_and_minimum_boundary(self):
        first, second = self.upload(), self.upload()
        job = self.start(first)
        small = self.start(second, max_tokens=2048, answer_reserve_tokens=1024)
        self.assertEqual(job['execution_options'], protected())
        self.assertEqual(capture_options(self.service.cli)['adapter_contract_version'], 1)
        self.service.cli.options = ExecutionOptions(max_tokens=65536)
        fixture = Fixture([dictionary, dictionary])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            self.queue.drain()
        self.assertEqual([r['reasoning'] for r in fixture.requests], [{'max_tokens': 4096}, {'max_tokens': 1024}])
        self.assertTrue(all(r['provider'] == {'require_parameters': True} for r in fixture.requests))
        self.assertTrue(all(set(r['reasoning']) == {'max_tokens'} for r in fixture.requests))
        for item in (job, small):
            saved = self.request('/api/runs/' + item['id'])
            self.assertEqual(saved['status'], 'success', saved['detail'])
            calls = saved['usage']['details']
            self.assertEqual(calls[0]['reasoning_max_tokens'], item['execution_options']['reasoning_max_tokens'])
            self.assertEqual(calls[0]['answer_reserve_tokens'], item['execution_options']['answer_reserve_tokens'])
        self.audit()

    def test_extraction_repair_and_timeout_copy_preserve_budget(self):
        doc = self.upload()
        approved_dictionary(self.store, doc['id'])
        bad = extraction_response(); bad['facts'][0]['quote'] = '상기 ' + FACTS[0]['quote']
        fixture = Fixture([bad, {'repairs': [{'path': 'facts.0.quote', 'quote': FACTS[0]['quote']}]}])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            job = self.start(doc, 'extract', max_tokens=32203)
            transport = self.service.job_cli(job).limited_timeout('openrouter', 5)
            self.assertEqual(transport.options['reasoning_max_tokens'], 4096)
            self.assertEqual(transport.options['answer_reserve_tokens'], 8192)
            saved = self.finish(job)
        self.assertEqual(saved['status'], 'success', saved['detail'])
        self.assertTrue(all(r['max_tokens'] == 32203 and r['reasoning'] == {'max_tokens': 4096} for r in fixture.requests))
        self.assertTrue(all(0 < r['timeout'] <= 360 for r in fixture.requests))
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.audit()

    def test_same_policy_resumes_but_changed_policy_or_v1_does_not(self):
        for stage in ('ontology', 'extract'):
            for change in ('same', 'reserve', 'v1'):
                with self.subTest(stage=stage, change=change):
                    doc = self.upload(SOURCE.ljust(8000 if stage == 'extract' else 4000) + SOURCE)
                    if stage == 'extract':
                        approved_dictionary(self.store, doc['id'])
                    output = extraction_response() if stage == 'extract' else dictionary
                    fixture = Fixture([output, TimeoutError('fixture timeout'), output, output])
                    with patch('verinote.openrouter._HTTPS', side_effect=fixture):
                        first = self.finish(self.start(doc, stage))
                        self.assertEqual(first['status'], 'error')
                        if change == 'v1':
                            retry = self.request(f'/api/documents/{doc["id"]}/{stage}', 'POST', {
                                'agent': 'openrouter', 'model': 'openrouter/auto',
                                'expected_version': 1, 'expected_generation': 1})
                        else:
                            retry = self.start(doc, stage, answer_reserve_tokens=4096 if change == 'reserve' else 8192)
                        saved = self.finish(retry)
                    self.assertEqual(saved['status'], 'success', saved['detail'])
                    self.assertEqual(len(job_calls(self.root, 'runs', saved)), 1 if change == 'same' else 2)
                    self.assertEqual('reasoning' in fixture.requests[-1], change != 'v1')
        self.audit()

    def test_reasoning_only_length_preserves_usage_and_no_retry(self):
        doc = self.upload()
        payload = completion('')
        payload['choices'][0]['finish_reason'] = 'length'
        payload['usage'] = {'prompt_tokens': 20, 'completion_tokens': 16384,
                            'completion_tokens_details': {'reasoning_tokens': 16384}, 'cost': .01}
        attempts = []
        def connection(call):
            return Connection(call, Response(json.dumps(payload).encode()), attempts)
        with patch('verinote.openrouter._HTTPS', side_effect=connection):
            saved = self.finish(self.start(doc))
        self.assertEqual(saved['status'], 'error')
        self.assertIn('토큰 한도', saved['detail'])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.store.document(doc['id'])['items'], [])
        calls = saved['usage']['details']
        self.assertEqual(calls[0]['finish_reason'], 'length')
        self.assertEqual(calls[0]['tokens']['reasoning'], 16384)
        self.assertEqual(calls[0]['reasoning_max_tokens'], 4096)
        self.assertEqual(calls[0]['answer_reserve_tokens'], 8192)
        self.audit()

    def test_unsupported_policy_fails_without_removing_it_and_retrying(self):
        doc = self.upload()
        attempts = []
        def connection(call):
            return Connection(call, Response(b'{"error":{"code":400}}', status=400), attempts)
        with patch('verinote.openrouter._HTTPS', side_effect=connection):
            saved = self.finish(self.start(doc))
        self.assertEqual(saved['status'], 'error')
        self.assertEqual(len(attempts), 1)
        body = json.loads(attempts[0][1]['body'])
        self.assertEqual(body['reasoning'], {'max_tokens': 4096})
        self.assertEqual(body['provider'], {'require_parameters': True})
        self.audit()

    def test_v2_programmatic_capture_and_legacy_question_stay_distinct(self):
        options = ExecutionOptions(2, 16384, 180, 4096, 8192)
        self.service.cli.options = options
        captured = capture_options(self.service.cli)
        expected = protected(); expected['timeouts']['ontology'] = 180
        self.assertEqual(captured, expected)
        self.service.cli.options = None
        question = self.request('/api/questions', 'POST', {'question': 'q', 'agent': 'openrouter', 'model': 'vendor/model'})
        self.assertEqual(question['execution_options']['adapter_contract_version'], 1)
        self.assertNotIn('reasoning_max_tokens', question['execution_options'])
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/questions', 'POST', {'question': 'q', 'agent': 'openrouter', 'model': 'vendor/model', 'answer_reserve_tokens': 8192})
        self.assertEqual(caught.exception.code, 400); caught.exception.close()
        self.audit()
