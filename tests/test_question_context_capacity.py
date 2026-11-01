"""Rich synthetic catalogs through real Store/graph and a mocked local API."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_question_meanings as meaning_tests
from test_assistant import answer, batch, tool

from verinote import engine
from verinote.agents import CLI
from verinote.assistant import PROMPT
from verinote.ollama import input_byte_limit
from verinote.question_context import resolve
from verinote.service import Service
from verinote.store import Store, canonical


class RichQuestionCapacity(unittest.TestCase):
    def test_two_and_four_document_catalogs_fit_after_real_graph_and_search(self):
        for count in (2, 4):
            for search in (False, True):
                with self.subTest(documents=count, search=search), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); fixture = meaning_tests.QuestionMeanings(); fixture.root = root
                    fixture.store = Store(root/'state.sqlite3')
                    people = ['가람', '나래', '다온', '라온']
                    kinds = ['음료', '이동수단', '수집품', '장식품']
                    for person, kind in list(zip(people, kinds, strict=True))[:count]: fixture.fixture(person, kind, approve=True)
                    snapshot = fixture.store.snapshot(); original_snapshot = copy.deepcopy(snapshot)
                    third = (tool(name='search_knowledge', query='차') if search
                             else tool(subject='가람', predicate='likes'))
                    first = batch(tool(), tool(action='upstream', subject='차', predicate='likes'), third)
                    prompts = []; raw_states = []; expected_graphs = []
                    actual_query = engine.query

                    def graph(*args, _query=actual_query, _expected=expected_graphs, **kwargs):
                        result = _query(*args, **kwargs)
                        _expected.append(copy.deepcopy(result))
                        return result

                    def request(path, payload=None, *, _prompts=prompts, _states=raw_states, _first=first, **kwargs):
                        if path == '/api/tags': return {'models': [{'name': 'qwen3.8:27b-mlx', 'digest': 'a'*64}]}
                        self.assertEqual(path, '/api/generate')
                        prompt = payload['prompt']; _prompts.append(prompt)
                        state = resolve(json.loads(prompt.split('입력 상태(JSON):\n', 1)[1])); _states.append(state)
                        result = _first if len(_prompts) == 1 else answer(ids=['E1', 'E2'])
                        return {'model': 'qwen3.8:27b-mlx', 'response': json.dumps(result, ensure_ascii=False),
                                'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 20}

                    cli = CLI(persistent=False); service = Service(fixture.store, root, cli)
                    try:
                        with patch('verinote.ollama.request', side_effect=request), patch('verinote.assistant.engine.query', side_effect=graph):
                            job = service.ask({'question': '차를 좋아하는 사람은?', 'agent': 'ollama', 'model': 'qwen3.8:27b-mlx'})
                            service.pool.shutdown(wait=True)
                        result = fixture.store.question(job['id'])
                        self.assertEqual(result['status'], 'success', result['error'])
                        self.assertEqual(len(prompts), 2)
                        limit = input_byte_limit(result['execution_options'])
                        self.assertEqual(limit, 27648)
                        self.assertTrue(all(len(prompt.encode()) <= limit for prompt in prompts))
                        self.assertEqual(raw_states[0]['catalog']['entity_meanings'], result['meanings'])
                        self.assertEqual(len(result['meanings']['groups'][0]['candidates']), count)
                        self.assertEqual(raw_states[-1]['evidence'], result['evidence'])
                        self.assertEqual(raw_states[-1]['trace'], result['trace'])
                        self.assertEqual(fixture.store.snapshot(snapshot['id']), original_snapshot)
                        for i, graph_result in enumerate(expected_graphs):
                            self.assertEqual(result['evidence'][f'E{i+1}']['result'], graph_result)
                        proofs = result['evidence']['E2']['result']['proofs']
                        self.assertEqual(len(proofs), count)
                        self.assertEqual(len({tuple(p['row_refs']) for p in proofs}), count)
                        if search:
                            lookup = result['trace'][2]['result']
                            self.assertFalse(lookup['truncated']); self.assertFalse(lookup['citable'])
                            self.assertIsNone(lookup['next_offset'])
                        original_bytes = len((PROMPT + canonical(raw_states[-1])).encode())
                        self.assertGreater(original_bytes, limit)
                        measurements = {'documents': count, 'search': search,
                                        'first_prompt_bytes': len(prompts[0].encode()),
                                        'second_prompt_bytes': len(prompts[1].encode()),
                                        'raw_second_bytes': original_bytes,
                                        'raw_catalog_bytes': len(canonical(raw_states[0]['catalog']).encode()),
                                        'limit_bytes': limit}
                        print('rich_context_capacity '+json.dumps(measurements, sort_keys=True))
                        self.assertEqual(result['context_budget'][-1]['prompt_bytes'], measurements['second_prompt_bytes'])
                    finally:
                        service.close(); cli.close()
