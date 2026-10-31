import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.agents import CLI
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.ollama import DEFAULTS
from verinote.service import Service
from verinote.store import Store


class OllamaService(unittest.TestCase):
    def test_production_service_pins_options_and_publishes_only_complete_extraction(self):
        case = load_case('short', DATA_ROOT/'v2')
        output = ideal_outputs(case, contract_version=2)[0]['output']
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); store = Store(root/'state.sqlite3')
                doc = store.register('fixture', 'synthetic.md', case['source'].encode(), case['source'])
                seed = store.create_run(doc['id'], 'ollama', 'qwen2.5:7b', 'ontology', 1, 1)
                store.run_status(seed['id'], 'running'); store.finish_run(seed['id'], dictionary=case['dictionary'])
                item = next(i for i in store.document(doc['id'])['items'] if i['kind'] == 'ontology')
                store.review(item['id'], item['revision'], 'approved', 'Synthetic fixture', 'Test only')
                def request(path, payload=None, fail=fail, **kwargs):
                    if path == '/api/tags': return {'models': [{'name': 'qwen2.5:7b', 'digest': 'a'*64}]}
                    return {'model': 'qwen2.5:7b', 'response': json.dumps(output), 'done': True,
                            'done_reason': 'length' if fail else 'stop', 'prompt_eval_count': 100, 'eval_count': 20}
                cli = CLI(persistent=False); service = Service(store, root, cli)
                try:
                    with patch('verinote.ollama.request', side_effect=request):
                        run = service.start(doc['id'], 'ollama', 'qwen2.5:7b', expected_version=1, expected_generation=1)
                        service.pool.shutdown(wait=True)
                    final = store.run(run['id'])
                    self.assertEqual(final['execution_options'], DEFAULTS | {'model':'qwen2.5:7b','model_digest':'a'*64})
                    self.assertEqual(final['status'], 'error' if fail else 'success')
                    candidates = [i for i in store.document(doc['id'])['items'] if i['kind'] != 'ontology']
                    self.assertEqual(bool(candidates), not fail)
                    self.assertTrue(all(i['status'] == 'pending' for i in candidates))
                finally: service.close(); cli.close()

    def test_dictionary_and_question_use_local_provider_and_frozen_options(self):
        from test_openrouter_pipeline import dictionary
        from test_workflow import SOURCE

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); store = Store(root/'state.sqlite3')
            doc = store.register('fixture', 'synthetic.md', SOURCE.encode(), SOURCE)
            def request(path, payload=None, **kwargs):
                if path == '/api/tags': return {'models': [{'name': 'qwen2.5:7b', 'digest': 'a'*64}]}
                if '개체' in payload['prompt'] and '입력(JSON)' in payload['prompt']:
                    try: output = dictionary(payload['prompt'])
                    except (ValueError, KeyError): output = {'answer': {'claims': [], 'unknowns': ['승인된 근거가 없습니다.']}}
                else: output = {'answer': {'claims': [], 'unknowns': ['승인된 근거가 없습니다.']}}
                return {'model': 'qwen2.5:7b', 'response': json.dumps(output), 'done': True,
                        'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 20}
            cli = CLI(persistent=False); service = Service(store, root, cli)
            try:
                with patch('verinote.ollama.request', side_effect=request):
                    run = service.start(doc['id'], 'ollama', 'qwen2.5:7b', stage='ontology', expected_version=1, expected_generation=1)
                    service.pool.shutdown(wait=True)
                self.assertEqual(store.run(run['id'])['status'], 'success', store.run(run['id'])['detail'])
            finally: service.close(); cli.close()
            cli = CLI(persistent=False); service = Service(store, root, cli)
            try:
                result = {'model': 'qwen2.5:7b', 'response': json.dumps({'answer': {'claims': [], 'unknowns': ['승인된 근거가 없습니다.']}}),
                          'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 20}
                first = dict(result, response=json.dumps({'tools': [{'name': 'query_graph', 'arguments': {'subject': '없는개체'}}]}))
                with patch('verinote.ollama.catalog', return_value=[{'id': 'qwen2.5:7b', 'digest': 'a'*64}]), patch('verinote.ollama.request', side_effect=[first, result]):
                    job = service.ask({'question': '승인된 지식은?', 'agent': 'ollama', 'model': 'qwen2.5:7b'})
                    service.pool.shutdown(wait=True)
                self.assertEqual(store.question(job['id'])['status'], 'success', store.question(job['id']))
                self.assertEqual(store.question(job['id'])['execution_options'], DEFAULTS | {'model':'qwen2.5:7b','model_digest':'a'*64})
            finally: service.close(); cli.close()


class OllamaSettings(unittest.TestCase):
    from test_workflow import HTTP
    setUp = HTTP.setUp
    tearDown = HTTP.tearDown
    request = HTTP.request

    def test_local_catalog_and_settings_round_trip(self):
        with patch('verinote.ollama.ready', return_value=True):
            settings = self.request('/api/settings')
        local = next(p for p in settings['providers'] if p['id'] == 'ollama')
        self.assertTrue(local['available']); self.assertEqual(local['transport'], 'local_http')
        rows = [{'id': 'qwen2.5:7b', 'name': 'qwen2.5:7b', 'digest': 'a'*64}]
        with patch('verinote.ollama.catalog', return_value=rows):
            self.assertEqual(self.request('/api/providers/ollama/models', 'POST', {})['models'], rows)
        result = self.request('/api/settings', 'PUT', {'selected': 'ollama', 'models': {'ollama': 'qwen2.5:7b'}})
        self.assertEqual(result['models']['ollama'], 'qwen2.5:7b')
