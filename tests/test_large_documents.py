"""Large sources through real validators and storage, using synthetic model outputs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_ontology import wire_dictionary

from verinote.dictionary_context import decode, encode
from verinote.documents import (
    MAX_TEXT_BYTES,
    bounded_spans,
    decode_document,
    validate_text,
)
from verinote.extraction_coverage import read_report
from verinote.extraction_loss import RecoveryBudget
from verinote.extraction_timing import read_extraction_timing
from verinote.mcp_workflow import (
    MCPWorkflow,
    check_context,
    initial_state,
    layout,
    task_prompt,
)
from verinote.ontology import dictionary_context, validate_dictionary
from verinote.progress import analysis_progress
from verinote.service import Service
from verinote.store import Store
from verinote.usage import atomic_json, chunk_directories, job_calls

EMPTY = {'entities': [], 'aliases': [], 'relations': []}


def long_fixture(count=17):
    outputs, parts = [], []
    for index in range(count):
        quotes = [f'A depends on B in case {index}-{i}.' for i in range(80)]
        parts.append(('\n'.join(quotes) + '\n').ljust(8000))
        outputs.append({'title': 'Large document',
                        'claims': [{'heading': 'Details', 'text': q, 'quote': q} for q in quotes[:12]],
                        'facts': [{'subject': 'A', 'predicate': 'depends_on', 'object': 'B', 'quote': q} for q in quotes],
                        'coverage': [{'predicate': 'depends_on', 'fact_indices': list(range(80)), 'claim_indices': [], 'status': 'extracted', 'reason': 'synthetic direct evidence'}],
                        'dictionary_gaps': []})
    quote = outputs[0]['facts'][0]['quote']
    dictionary = {'entities': [{'id': name, 'name': name, 'type': 'Service', 'quote': quote} for name in ('A', 'B')],
                  'aliases': [], 'relations': [{'id': 'depends_on', 'description': 'Direct dependency', 'subject_types': ['Service'], 'object_types': ['Service'], 'quote': quote}]}
    return ''.join(parts), dictionary, outputs


class Admission(unittest.TestCase):
    def test_unicode_byte_boundaries_and_large_text(self):
        value = '한글🙂ab'
        self.assertEqual(len(value.encode()), 12)
        with patch('verinote.documents.MAX_TEXT_BYTES', 12):
            self.assertEqual(validate_text(value), value)
            with self.assertRaisesRegex(ValueError, 'UTF-8'): validate_text(value + 'c')
            with self.assertRaisesRegex(ValueError, 'NUL'): validate_text('a\x00')
            with self.assertRaises(ValueError): validate_text(' \n')
        source = '한' * 128001
        self.assertEqual(decode_document('long.txt', source.encode()), source)
        self.assertEqual(validate_text('x' * MAX_TEXT_BYTES), 'x' * MAX_TEXT_BYTES)
        with self.assertRaisesRegex(ValueError, 'UTF-8'): validate_text('x' * (MAX_TEXT_BYTES + 1))

    def test_pdf_counts_inserted_newlines_and_expansion(self):
        reader = Mock(is_encrypted=False, pages=[Mock(extract_text=lambda: '한글'), Mock(extract_text=lambda: '🙂')])
        with patch('pypdf.PdfReader', return_value=reader):
            with patch('verinote.documents.MAX_TEXT_BYTES', 11): self.assertEqual(decode_document('test.pdf', b'pdf'), '한글\n🙂')
            with patch('verinote.documents.MAX_TEXT_BYTES', 10), self.assertRaisesRegex(ValueError, 'UTF-8'): decode_document('test.pdf', b'pdf')
        reader.pages = [Mock(extract_text=lambda: 'x' * 128001)]
        with patch('pypdf.PdfReader', return_value=reader): self.assertEqual(len(decode_document('test.pdf', b'pdf')), 128001)

    def test_source_specific_dictionary_counts_and_global_decoder(self):
        value = {'entities': [{'id': str(i), 'name': 'A', 'type': 'T', 'quote': 'A'} for i in range(12801)], 'aliases': [], 'relations': []}
        with self.assertRaises(ValueError): validate_dictionary(value, 'A')
        accepted = validate_dictionary(value, 'A' * 132000)
        definitions = dictionary_context(accepted)
        self.assertEqual(decode(encode(definitions)), definitions)


class HTTPAdmission(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_paste_above_old_limit_and_rejection_is_atomic(self):
        source = '한글 본문\n' * 25000
        doc = self.request('/api/documents', 'POST', {'title': 'long', 'filename': 'long.txt', 'text': source})
        self.assertEqual(doc['text'], source)
        self.assertEqual(self.request('/api/settings')['document_limits']['text_bytes'], MAX_TEXT_BYTES)
        before = len(self.store.documents())
        with patch('verinote.documents.MAX_TEXT_BYTES', 10), self.assertRaises(HTTPError) as error:
            self.request('/api/documents', 'POST', {'title': 'too big', 'filename': 'bad.txt', 'text': '한' * 4})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.store.documents()), before)


class LargeWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'verinote.sqlite3')
        self.addCleanup(layout.cache_clear)

    def document(self, source):
        return self.store.register('large', 'large.txt', source.encode(), decode_document('large.txt', source.encode()))

    def execute(self, doc, responder, stage='extract'):
        calls = []
        class Fake:
            def complete(_, agent, model, prompt, folder, *, phase=None):
                payload = json.loads(prompt.split('입력(JSON):\n', 1)[1])
                calls.append(payload)
                folder.mkdir(parents=True, exist_ok=True)
                atomic_json(folder / 'run.json', {'status': 'running'})
                return responder(payload, prompt)
        service = Service(self.store, self.root, Fake())
        try:
            run = service.start(doc['id'], 'agy', 'test', stage, doc['version'], doc['generation'])
            service.pool.shutdown(wait=True)
            return self.store.run(run['id']), calls
        finally:
            service.close()

    def approve_dictionary(self, doc):
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(item['id'], item['revision'], 'approved', 'human', 'review')

    def test_cli_large_publication_resume_progress_usage_and_review(self):
        source, dictionary, outputs = long_fixture()
        doc = self.document(source)
        run, calls = self.execute(doc, lambda p, prompt: wire_dictionary(dictionary if p['offset'] == 0 else EMPTY, prompt), 'ontology')
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(sum(len(''.join(s['text'] for s in p['sources'])) for p in calls), len(source))
        self.approve_dictionary(doc)
        def interrupt(payload, prompt):
            index = payload['offset'] // 8000
            if index == 16: raise TimeoutError('synthetic interruption')
            return copy.deepcopy(outputs[index])
        first, _ = self.execute(doc, interrupt)
        self.assertEqual(first['status'], 'error', first['detail'])
        progress = analysis_progress(self.root, self.store, first['id'])
        self.assertEqual(progress['completed_chunks'], 16)
        self.assertEqual(progress['total_chunks'], 17)
        self.store = Store(self.root / 'verinote.sqlite3')
        final, calls = self.execute(doc, lambda p, prompt: copy.deepcopy(outputs[p['offset'] // 8000]))
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([p['offset'] for p in calls], [128000])
        report = read_report(self.root, self.store, final['id'])
        self.assertTrue(report['available'], report)
        self.assertEqual(len(report['chunks']), 17)
        timing = read_extraction_timing(self.root, self.store, final['id'])
        self.assertTrue(timing['available'], timing)
        self.assertEqual(len(timing['chunks']), 17)
        self.assertEqual(analysis_progress(self.root, self.store, final['id'])['percent'], 100)
        logged = job_calls(self.root, 'runs', final, source_length=len(source))
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0]['call_id'].endswith('/17'))
        items = self.store.document(doc['id'])['items']
        page = next(i for i in items if i['kind'] == 'page')
        self.assertEqual(len(page['payload']['claims']), 204)
        self.assertEqual(sum(i['kind'] == 'fact' for i in items), 1360)
        self.store.review(page['id'], page['revision'], 'approved', 'human', 'large review')
        reviewed = next(i for i in self.store.document(doc['id'])['items'] if i['id'] == page['id'])
        self.assertEqual(reviewed['status'], 'approved')

    def test_dictionary_more_than_64_chunks_and_resumed_prefix(self):
        source = ('line.\n' * 700) * 70
        doc = self.document(source)
        expected = list(bounded_spans(source, 4000))
        self.assertGreater(len(expected), 64)
        def interrupt(payload, prompt):
            if payload['offset'] == expected[65][0]: raise TimeoutError('stop after 65')
            return copy.deepcopy(EMPTY)
        first, calls = self.execute(doc, interrupt, 'ontology')
        self.assertEqual(first['status'], 'error')
        self.assertEqual(analysis_progress(self.root, self.store, first['id'])['completed_chunks'], 65)
        final, calls = self.execute(doc, lambda p, prompt: copy.deepcopy(EMPTY), 'ontology')
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([p['offset'] for p in calls], [offset for offset, _ in expected[65:]])
        logged = job_calls(self.root, 'runs', first, source_length=len(source))
        self.assertEqual(len(logged), 66)
        # A previous prompt hash is not silently reinterpreted as the new contract.
        state = json.loads((self.root / 'runs' / first['id'] / 'dictionary-checkpoint.json').read_text())
        self.assertEqual(state['offset'], expected[65][0])

    def test_mcp_large_aggregate_reconnect_and_transactional_budget(self):
        source, dictionary, outputs = long_fixture()
        doc = self.document(source)
        flow = MCPWorkflow(self.store)
        key = flow.start(doc['id'], 'ontology', 1, 1)['id']
        for i in range(flow.status(key)['total_chunks']):
            flow.submit(key, i + 1, dictionary if i == 0 else EMPTY, 'ontology')
        flow.finalize(key)
        self.approve_dictionary(doc)
        key = flow.start(doc['id'], 'extract', 1, 1)['id']
        for i, output in enumerate(outputs, 1):
            flow = MCPWorkflow(Store(self.root / 'verinote.sqlite3'))
            self.assertEqual(flow.task(key)['chunk_index'], i)
            if i == 17:
                with self.store.connect() as db:
                    before = db.execute('SELECT payload FROM external_sessions WHERE run_id=?', (key,)).fetchone()[0]
                with patch('verinote.mcp_workflow.MAX_SESSION_BYTES', len(before.encode()) + 1), self.assertRaisesRegex(ValueError, '32 MiB'):
                    flow.submit(key, i, output, 'extract')
                self.assertEqual(flow.status(key)['accepted_chunks'], 16)
            flow.submit(key, i, output, 'extract')
        self.assertEqual(flow.finalize(key)['status'], 'success')
        items = self.store.document(doc['id'])['items']
        self.assertEqual(len(next(i for i in items if i['kind'] == 'page')['payload']['claims']), 204)
        self.assertEqual(sum(i['kind'] == 'fact' for i in items), 1360)

    def test_context_preflight_uses_largest_serialized_remaining_chunk_once(self):
        source = '한' * 4000 + '\"\\\n' * 2000 + 'x' * 5000
        state = initial_state(source, 1, 'ontology', None)
        for accepted in range(state['total_chunks']):
            state['accepted'] = ['hash'] * accepted
            sizes = [(len(task_prompt(source, state, 'ontology', None, i).encode()), i) for i in range(accepted, state['total_chunks'])]
            chosen = layout(source, 'ontology')[1][accepted]
            self.assertEqual(len(task_prompt(source, state, 'ontology', None, chosen).encode()), max(s[0] for s in sizes))
            with patch('verinote.mcp_workflow.task_prompt', wraps=task_prompt) as prompt:
                check_context(source, state, 'ontology', None)
                self.assertEqual(prompt.call_count, 1)

    def test_recovery_budget_retains_attempt_at_chunk_17(self):
        source, dictionary, _ = long_fixture(18)
        doc = self.document(source)
        run, _ = self.execute(doc, lambda p, prompt: wire_dictionary(dictionary if p['offset'] == 0 else EMPTY, prompt), 'ontology')
        self.approve_dictionary(doc)
        first = self.store.create_run(doc['id'], 'agy', 'test', 'extract', 1, 1)
        contract = {'test': 'same contract'}
        budget = RecoveryBudget(self.store, self.root, first, contract)
        self.assertEqual(budget.reserve(17), 'reserved')
        self.store.run_status(first['id'], 'error')
        second = self.store.create_run(doc['id'], 'agy', 'test', 'extract', 1, 1)
        resumed = RecoveryBudget(self.store, self.root, second, contract)
        self.assertEqual(resumed.status(17), 'previous_attempt')
        self.assertFalse(resumed.unknown)

    def test_usage_discovery_ignores_unowned_and_hostile_directories(self):
        key = 'a' * 32
        folder = self.root / 'runs' / key
        for name in ('1', '17', '18', '017', '0', '-1', '9' * 200, 'unrelated'):
            (folder / name).mkdir(parents=True)
        (folder / '2').symlink_to(folder / '1', target_is_directory=True)
        self.assertEqual(chunk_directories(self.root, key, 17), ([1, 17], False))
        for i in range(150): (folder / f'junk-{i}').touch()
        self.assertTrue(chunk_directories(self.root, key, 17)[1])
        owner = {'id': key, 'agent': 'agy', 'model': '', 'status': 'success', 'stage': 'extract'}
        self.assertFalse(job_calls(self.root, 'runs', owner, source_length=136000)[-1]['call_count_known'])
