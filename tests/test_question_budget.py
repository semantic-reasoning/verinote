"""Synthetic whole-request budgets through Assistant and the real local adapter."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_assistant import Fake, answer, batch, tool
from test_question_context import state

from verinote.agents import CLI
from verinote.assistant import Assistant
from verinote.ollama import DEFAULTS, Job, input_byte_limit
from verinote.question_context import resolve
from verinote.service import Service
from verinote.store import Store, canonical

MODEL = 'qwen3.8:27b-mlx'


class LimitedFake(Fake):
    def __init__(self, outputs, limit):
        super().__init__(outputs); self.limit = limit

    def prompt_byte_limit(self, agent): return self.limit


class QuestionBudget(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.store = Store(self.root/'state.sqlite3')

    def checker(self, limit=None):
        a = Assistant.__new__(Assistant); a.cli = LimitedFake([], limit)
        a.job = {'agent': 'claude'}; a.trace = []; a.evidence = {}; a.progress = lambda *args: None
        return a

    def test_exact_utf8_boundary_application_transport_minimum_and_invalid_limit(self):
        with patch('verinote.assistant.MAX_CONTEXT', 5):
            a = self.checker(100); a.check_context('가é', 1)
            self.assertEqual(a.job['context_budget'][0]['prompt_bytes'], 5)
            self.assertEqual(a.job['context_budget'][0]['effective_limit_bytes'], 5)
            with self.assertRaisesRegex(ValueError, '질문.*한도 초과'): a.check_context('가éa', 2)
        a = self.checker(5); a.check_context('가é', 1)
        with self.assertRaises(ValueError): a.check_context('가éa', 2)
        self.assertEqual(a.job['context_budget'][-1]['status'], 'exceeded')
        a = self.checker(); a.check_context('가é', 1)
        self.assertIsNone(a.job['context_budget'][0]['transport_limit_bytes'])
        for limit in (True, 0, -1, '5'):
            with self.subTest(limit=limit), self.assertRaises(ValueError): self.checker(limit).check_context('a', 1)

    def test_unknown_provider_initial_overflow_makes_no_model_call(self):
        fake = Fake([]); service = Service(self.store, self.root, fake); self.addCleanup(service.close)
        with patch('verinote.assistant.MAX_CONTEXT', 10):
            job = service.ask({'question': '합성 질문', 'agent': 'claude', 'model': 'sonnet'})
            service.pool.shutdown(wait=True)
        result = self.store.question(job['id'])
        self.assertEqual(result['status'], 'error'); self.assertEqual(fake.calls, [])
        self.assertIsNone(result['context_budget'][0]['transport_limit_bytes'])
        self.assertEqual(result['context_budget'][0]['effective_limit_bytes'], 10)
        self.assertEqual(result['context_budget'][0]['status'], 'exceeded')

    def test_later_unfit_request_preserves_evidence_and_stops_before_second_call(self):
        raw = state()['evidence']['E1']['result']
        for meaning in raw['entities']: meaning['quote'] = '고유 인용 용량 검증'
        # Irreducible exact quote: stronger sharing must not make this a
        # duplicate-metadata test or accidentally allow the next call.
        raw['facts'][0]['payload']['quote'] = ''.join(chr(0x4e00+i) for i in range(6000))
        before = copy.deepcopy(raw)
        fake = LimitedFake([tool()], 20000); service = Service(self.store, self.root, fake)
        self.addCleanup(service.close)
        with patch('verinote.assistant.engine.query', return_value=raw):
            job = service.ask({'question': '차의 승인 근거는?', 'agent': 'claude', 'model': 'sonnet'})
            service.pool.shutdown(wait=True)
        result = self.store.question(job['id'])
        self.assertEqual(result['status'], 'error'); self.assertEqual(len(fake.calls), 1)
        self.assertIn('질문 프롬프트 크기 한도 초과', result['error'])
        self.assertNotIn('문서를 나누', result['error'])
        self.assertEqual(result['evidence']['E1']['result'], before); self.assertEqual(raw, before)
        self.assertEqual([d['status'] for d in result['context_budget']], ['within_limit', 'exceeded'])

    def test_ollama_initial_oversize_is_rejected_before_generate_with_diagnostics(self):
        for i in range(35):
            self.store.register(f'합성{i}'+('가'*180), f'd{i}.txt', b'x', 'x')
        paths = []
        def request(path, *args, **kwargs):
            paths.append(path)
            self.assertEqual(path, '/api/tags')
            return {'models': [{'name': MODEL, 'digest': 'a'*64}]}
        cli = CLI(persistent=False); self.addCleanup(cli.close)
        service = Service(self.store, self.root, cli); self.addCleanup(service.close)
        with patch('verinote.ollama.request', side_effect=request):
            job = service.ask({'question': '합성 자료의 승인 근거는?', 'agent': 'ollama', 'model': MODEL})
            service.pool.shutdown(wait=True)
        result = self.store.question(job['id']); record = result['context_budget'][0]
        self.assertEqual(result['status'], 'error'); self.assertNotIn('/api/generate', paths)
        self.assertEqual(record['transport_limit_bytes'], input_byte_limit(result['execution_options']))
        self.assertGreater(record['prompt_bytes'], record['effective_limit_bytes'])
        self.assertLess(record['prompt_bytes'], record['application_limit_bytes'])
        self.assertFalse(list((self.root/'questions'/job['id']).glob('llm-*')))

    def test_repeated_graphs_reach_second_request_in_actual_ollama_job(self):
        graphs = copy.deepcopy(list(state()['evidence'].values()))
        for entry in graphs:
            for row in entry['result']['entities']: row['quote'] = '합성 인용문 ' * 220
        before = copy.deepcopy(graphs); prompts = []
        outputs = [batch(tool(subject='합성1'), tool(subject='합성2'), tool(subject='합성3')), answer(ids=['E1', 'E2', 'E3'])]
        def request(path, payload=None, **kwargs):
            if path == '/api/tags': return {'models': [{'name': MODEL, 'digest': 'a'*64}]}
            self.assertEqual(path, '/api/generate'); prompts.append(payload['prompt'])
            return {'model': MODEL, 'response': json.dumps(outputs[len(prompts)-1], ensure_ascii=False),
                    'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 20}
        cli = CLI(persistent=False); self.addCleanup(cli.close)
        service = Service(self.store, self.root, cli); self.addCleanup(service.close)
        with patch('verinote.ollama.request', side_effect=request), patch('verinote.assistant.engine.query', side_effect=[g['result'] for g in graphs]):
            job = service.ask({'question': '차의 가능한 의미별 승인 관계는?', 'agent': 'ollama', 'model': MODEL})
            service.pool.shutdown(wait=True)
        result = self.store.question(job['id']); self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(len(prompts), 2); self.assertEqual(len(result['trace']), 3)
        self.assertTrue(all(len(p.encode()) <= input_byte_limit(result['execution_options']) for p in prompts))
        projected = json.loads(prompts[-1].split('입력 상태(JSON):\n', 1)[1]); raw = resolve(projected)
        self.assertGreater(len(canonical(raw).encode()), input_byte_limit(result['execution_options']))
        self.assertEqual(raw['evidence'], result['evidence']); self.assertEqual(graphs, before)
        self.assertEqual(len(result['context_budget']), 2)
        self.assertEqual(result['context_budget'][-1]['prompt_bytes'], len(prompts[-1].encode()))
        self.assertTrue(all(d['status'] == 'within_limit' for d in result['context_budget']))

    def test_claude_repeated_graphs_reach_second_call_below_application_cap(self):
        graphs = copy.deepcopy(list(state()['evidence'].values())); before = copy.deepcopy(graphs)
        fake = Fake([batch(tool(subject='합성1'), tool(subject='합성2'), tool(subject='합성3')), answer(ids=['E1', 'E2', 'E3'])])
        service = Service(self.store, self.root, fake); self.addCleanup(service.close)
        with patch('verinote.assistant.engine.query', side_effect=[g['result'] for g in graphs]):
            job = service.ask({'question': '차의 의미별 근거는?', 'agent': 'claude', 'model': 'sonnet'})
            service.pool.shutdown(wait=True)
        result = self.store.question(job['id']); self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(len(fake.calls), 2)
        sent = fake.calls[-1][2]; projected = json.loads(sent.split('입력 상태(JSON):\n', 1)[1]); raw = resolve(projected)
        self.assertGreater(len(canonical(raw).encode()), 60000)
        self.assertLessEqual(len(sent.encode()), 60000)
        self.assertEqual(raw['evidence'], result['evidence']); self.assertEqual(graphs, before)
        self.assertIsNone(result['context_budget'][-1]['transport_limit_bytes'])

    def test_adapter_boundary_uses_same_frozen_job_limit(self):
        cli = CLI(persistent=False); self.addCleanup(cli.close)
        job = Job(cli, DEFAULTS | {'model': MODEL, 'model_digest': 'a'*64})
        limit = job.prompt_byte_limit('ollama'); self.assertEqual(limit, input_byte_limit(job.options))
        prompt = '가'*(limit//3) + 'a'*(limit%3)
        def request(path, payload=None, **kwargs):
            if path == '/api/tags': return {'models': [{'name': MODEL, 'digest': 'a'*64}]}
            self.assertEqual(len(payload['prompt'].encode()), limit)
            return {'model': MODEL, 'response': '{}', 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 1, 'eval_count': 1}
        with patch('verinote.ollama.request', side_effect=request) as call:
            job.complete('ollama', MODEL, prompt, self.root/'exact')
            self.assertEqual(call.call_count, 2)
        with patch('verinote.ollama.request', side_effect=AssertionError('must not call provider')):
            with self.assertRaisesRegex(ValueError, '입력 크기 한도'): job.complete('ollama', MODEL, prompt+'a', self.root/'over')
