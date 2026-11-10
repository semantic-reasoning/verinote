"""#808: a whole-request refusal states real sizes and only next steps available in that state."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ontology import wire_dictionary
from test_workflow import approved_dictionary, extraction_response

from verinote import dictionary_context as dc
from verinote.agents import CLI
from verinote.dictionary_context import RequestTooLarge, decode
from verinote.extraction_transport import ExtractionSpec
from verinote.ollama import input_byte_limit
from verinote.ontology import (
    MAX_PROMPT_BYTES,
    bounded_prompt,
    dictionary_context,
    validate_dictionary,
)
from verinote.service import Service
from verinote.store import Store
from verinote.transports import Credentials
from verinote.usage import atomic_json

TAIL = ' · 문서 버전·승인 사전·에이전트·모델·병렬 설정 등 추출 조건이 이 실행과 같으면 다시 추출할 때 첫 청크부터 호출합니다.'
OTHER = '입력 예산이 120,000바이트인 다른 에이전트로 처음부터 실행(완료분을 재사용하지 않고 새로 호출, 원문과 사전이 해당 제공자에게 전달됨)'
SPLIT = '문서를 나누어 각각 사전 생성·승인(승인된 사전은 재사용되지 않음, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함)'
RERUN = '같은 문서 버전·승인 사전·에이전트로 다시 추출해도 같은 크기로 거부됩니다. 다음 단계: '
RETRY = '보정 요청 크기는 문제 인용에 따라 달라지므로 같은 문서 버전·승인 사전·에이전트·모델로 다시 추출하면 통과할 수 있지만 보장되지 않고 이 청크를 새로 호출합니다. 반복되면 다음 단계: '
GENERATION_RERUN = '복구 기록이 유효하면 같은 에이전트·모델로 다시 실행해도 같은 크기로 거부됩니다. 다음 단계: '
GENERATION_COST = '(검증된 구간은 재사용되지 않고 새로 호출, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함, 추출은 완성된 사전 전체를 보내므로 별도로 거부될 수 있음)'
GENERATION_END = f'{GENERATION_COST}. 위 단계를 따라도 사전 생성의 뒤 구간이나 더 조밀한 부분에서 다시 거부될 수 있으며 그 전 호출 비용은 발생합니다.'
EMPTY = {'entities': [], 'aliases': [], 'relations': []}
RISING = ('x', 'x가', '가')
INSTRUCTIONS = 'i\n'


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def table_bytes(dictionary):
    return len(serialized(dc.encode(dictionary_context(dictionary))).encode())


def rows(names, quote=None):
    return {'entities': [{'id': f'{name}-' + 'i' * 150, 'name': name, 'type': 'T' + 't' * 40, 'quote': name if quote is None else quote} for name in names],
            'aliases': [], 'relations': []}


def revise_text(definitions, excess):
    return f'사전을 수정해 전송 정의 {definitions:,}바이트 중 {excess:,}바이트 이상을 줄이고 재승인(모든 청크를 새로 호출하며 뺀 정의는 추출에 쓸 수 없음)'


def refused_text(request, limit):
    return f'무손실 표현을 포함한 전체 요청 {request:,}바이트가 입력 예산 {limit:,}바이트에 들어가지 않습니다. 검증된 사전은 보존됩니다. '


def body_of(prompt):
    return json.loads(prompt.split('입력(JSON):\n', 1)[1])


def budget_sizes(record):
    """Smaller form's request and definition bytes, read from the record build wrote rather than from the exception."""
    if record['encoded_prompt_bytes'] <= record['raw_prompt_bytes']:
        return record['encoded_prompt_bytes'], record['encoded_definition_bytes']
    return record['raw_prompt_bytes'], record['raw_definition_bytes']


class Stub:
    def __init__(self, respond, limit=None, ready=None):
        self.respond, self.calls = respond, []
        if limit is not None:
            self.prompt_byte_limit = lambda agent: limit
        if ready is not None:
            self.ready = lambda agent: agent in ready

    def complete(self, agent, model, prompt, folder, **_):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / 'run.json', {'status': 'running'})
        self.calls.append(prompt)
        return self.respond(prompt, len(self.calls))


def chunk_response(dictionary):
    def respond(prompt, _):
        text = body_of(prompt)['text']
        unique = re.search(r'[A-H]\d{5}', text)
        return extraction_response(facts=[], dictionary=dictionary, page={'title': 'L', 'claims': [{'heading': 'h', 'text': 't', 'quote': unique.group() if unique else text[:20]}]})
    return respond


def generated(prompt, _):
    """Every name in this call's source, as a generation response: the dictionary follows the text it is given."""
    source = ''.join(span['text'] for span in body_of(prompt)['sources'])
    return wire_dictionary(rows(re.findall(r'[A-H]\d{5}', source)), prompt)


def generation_source(count):
    return ''.join((' '.join(f'{"ABCDEFGH"[part]}{i:05}' for i in range(200))).ljust(4000, 'x') for part in range(count))


class ServiceCase(unittest.TestCase):
    def setUp(self):
        # This protocol measures the exact contract-2 request bytes and call counts.
        contract = mock.patch('verinote.store.EXTRACTION_CONTRACT_VERSION', 2)
        contract.start(); self.addCleanup(contract.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'state.sqlite3')

    def register(self, text, name='d'):
        return self.store.register(name, f'{name}.md', text.encode(), text)

    def run_job(self, doc, cli, stage='extract', agent='copilot', model='auto'):
        service = Service(self.store, self.root, cli)
        self.addCleanup(service.close)
        job = service.start(doc['id'], agent, model, stage, doc['version'], doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(job['id'])

    def budget(self, run, *folder):
        return json.loads((self.root / 'runs' / run['id'] / Path(*folder) / 'context-budget.json').read_text(encoding='utf-8'))

    def budget_folders(self, run):
        return sorted(p.parent.name for p in (self.root / 'runs' / run['id']).glob('*/context-budget.json'))

    def item(self, doc):
        return next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')

    def revise_and_approve(self, doc, payload):
        item = self.item(doc)
        revised = self.store.review(item['id'], item['revision'], 'revise', 'R', '줄임', payload=payload)
        self.store.review(item['id'], revised['revision'], 'approved', 'R', '재승인')

    def three_chunks(self, fills=RISING):
        """Three 8,000-char chunks whose byte sizes follow their repeated fill text, with a pinned 300-entity dictionary."""
        names = [f'C{i:05}' for i in range(300)]
        first = ' '.join(names)
        heads = [f'표지{i} ' + (first if i == 0 else '') for i in range(len(fills))]
        source = ''.join(head + (fill * 8000)[:8000 - len(head)] for head, fill in zip(heads, fills, strict=True))
        dictionary = rows(names, first)
        doc = self.register(source)
        approved_dictionary(self.store, doc['id'], dictionary)
        return doc, dictionary

    def at_revision_eight(self):
        doc, dictionary = self.three_chunks()
        while self.item(doc)['revision'] < 8:
            self.revise_and_approve(doc, dictionary)
        self.assertEqual(self.item(doc)['revision'], 8)
        return doc, dictionary

    def request_sizes(self, fills):
        """Dispatched bytes per chunk, measured on a separate store by a run with no transport budget."""
        other = ServiceCase('setUp')
        other.setUp()
        self.addCleanup(other.doCleanups)
        doc, dictionary = other.three_chunks(fills)
        stub = Stub(chunk_response(dictionary))
        result = other.run_job(doc, stub)
        self.assertEqual(result['status'], 'success', result['detail'])
        return [len(prompt.encode()) for prompt in stub.calls]


class Admission(unittest.TestCase):
    body = {'filename': 'a', 'text': '가' * 100, 'dictionary': rows(f'C{i:05}' for i in range(50))}

    def refused(self, instructions, body, limit):
        record = {}
        with self.assertRaises(RequestTooLarge) as caught:
            bounded_prompt(instructions, body, prompt_limit=limit, diagnostics=record)
        return caught.exception, record

    def test_table_form_sizes_match_the_budget_record_and_one_byte_under_is_refused(self):
        exc, record = self.refused(INSTRUCTIONS, self.body, 1)
        self.assertLess(record['encoded_prompt_bytes'], record['raw_prompt_bytes'])
        self.assertEqual((exc.request_bytes, exc.definition_bytes), (record['encoded_prompt_bytes'], record['encoded_definition_bytes']))
        self.assertEqual(str(exc), f'{refused_text(exc.request_bytes, 1)}'.rstrip())
        bounded_prompt(INSTRUCTIONS, self.body, prompt_limit=exc.request_bytes)
        self.assertEqual(self.refused(INSTRUCTIONS, self.body, exc.request_bytes - 1)[0].excess, 1)

    def test_plain_form_sizes_match_the_budget_record_when_the_table_is_larger(self):
        body = {'text': 'x', 'dictionary': EMPTY}
        exc, record = self.refused(INSTRUCTIONS, body, 1)
        self.assertGreater(record['encoded_prompt_bytes'], record['raw_prompt_bytes'])
        self.assertEqual((exc.request_bytes, exc.definition_bytes), (record['raw_prompt_bytes'], record['raw_definition_bytes']))

    def test_request_with_no_dictionary_is_refused_with_a_plain_value_error(self):
        with self.assertRaises(ValueError) as caught:
            bounded_prompt(INSTRUCTIONS, {'text': 'x' * 100}, prompt_limit=10)
        self.assertNotIsInstance(caught.exception, RequestTooLarge)

    def test_other_agent_step_appears_exactly_when_the_policy_budget_admits_the_request(self):
        body = {'text': 'x', 'dictionary': EMPTY}
        size = len((INSTRUCTIONS + serialized(body)).encode())
        at_policy = INSTRUCTIONS + 'p' * (MAX_PROMPT_BYTES - size)
        bounded_prompt(at_policy, body)
        exc = self.refused(at_policy, body, 27648)[0]
        self.assertEqual(exc.request_bytes, MAX_PROMPT_BYTES)
        self.assertEqual(exc.extraction_advice(True, 2), f'{RERUN}{OTHER} 또는 {SPLIT}.')
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{SPLIT}.')
        over = at_policy + 'p'
        with self.assertRaises(RequestTooLarge):
            bounded_prompt(over, body)
        exc = self.refused(over, body, 27648)[0]
        self.assertEqual(exc.request_bytes, MAX_PROMPT_BYTES + 1)
        self.assertEqual(exc.extraction_advice(True, 2), f'{RERUN}{SPLIT}.')

    def test_revise_step_in_plain_form_appears_exactly_when_emptying_the_dictionary_fits(self):
        body = {'text': 'x', 'dictionary': {'entities': [{'id': 'e', 'name': '가', 'type': 'T'}], 'aliases': [], 'relations': []}}
        raw = len((INSTRUCTIONS + serialized(body)).encode())
        emptied = len((INSTRUCTIONS + serialized({'text': 'x', 'dictionary': EMPTY})).encode())
        exc = self.refused(INSTRUCTIONS, body, emptied)[0]
        self.assertEqual(exc.request_bytes, raw)
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{revise_text(exc.definition_bytes, raw - emptied)} 또는 {SPLIT}.')
        bounded_prompt(INSTRUCTIONS, {'text': 'x', 'dictionary': EMPTY}, prompt_limit=emptied)
        exc = self.refused(INSTRUCTIONS, body, emptied - 1)[0]
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{SPLIT}.')
        with self.assertRaises(RequestTooLarge):
            bounded_prompt(INSTRUCTIONS, {'text': 'x', 'dictionary': EMPTY}, prompt_limit=emptied - 1)

    def test_plain_form_definitions_count_the_quotes_a_repair_request_carries(self):
        quoted = {'text': 'x', 'dictionary': {'entities': [{'id': 'e', 'name': '가', 'type': 'T', 'quote': '가'}], 'aliases': [], 'relations': []}}
        raw = len((INSTRUCTIONS + serialized(quoted)).encode())
        emptied = len((INSTRUCTIONS + serialized({'text': 'x', 'dictionary': EMPTY})).encode())
        exc = self.refused(INSTRUCTIONS, quoted, emptied)[0]
        self.assertEqual(exc.definition_bytes, len(serialized(quoted['dictionary']).encode()))
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{revise_text(exc.definition_bytes, raw - emptied)} 또는 {SPLIT}.')

    def test_tie_between_forms_reports_the_table_sent_when_the_threshold_is_exceeded(self):
        body = {'text': 'x', 'dictionary': rows(f'C{i:05}' for i in range(50))}
        with mock.patch.object(dc, 'PROTOCOL', ''):
            _, record = self.refused(INSTRUCTIONS, body, 1)
        padding = record['raw_prompt_bytes'] - record['encoded_prompt_bytes']
        self.assertGreater(padding, 0)
        with mock.patch.object(dc, 'PROTOCOL', 'x' * padding):
            exc, record = self.refused(INSTRUCTIONS, body, 1)
            self.assertEqual(record['raw_prompt_bytes'], record['encoded_prompt_bytes'])
            self.assertEqual(exc.definition_bytes, record['encoded_definition_bytes'])
            self.assertNotEqual(exc.definition_bytes, record['raw_definition_bytes'])
            self.assertTrue(bounded_prompt(INSTRUCTIONS, body, context_limit=1, prompt_limit=exc.request_bytes).startswith(dc.PROTOCOL))

    def test_extraction_advice_figure_per_pin_revision(self):
        request = self.refused(INSTRUCTIONS, self.body, 1)[0].request_bytes
        exc = self.refused(INSTRUCTIONS, self.body, request - 100)[0]
        table = table_bytes(self.body['dictionary'])
        for revision, stated in ((7, 100), (8, 101), (9, 101), (10, 100), (97, 100), (98, 101), (99, 101), (100, 100)):
            with self.subTest(revision=revision):
                self.assertEqual(exc.extraction_advice(False, revision), f'{RERUN}{revise_text(table, stated)} 또는 {SPLIT}.')

    def test_revise_gate_counts_the_digit_the_revision_gains(self):
        request = self.refused(INSTRUCTIONS, self.body, 1)[0].request_bytes
        table = table_bytes(self.body['dictionary'])
        removable = table - len(serialized(EMPTY).encode())
        exc = self.refused(INSTRUCTIONS, self.body, request - removable + 1)[0]
        self.assertEqual(exc.extraction_advice(False, 8), f'{RERUN}{revise_text(table, removable)} 또는 {SPLIT}.')
        exc = self.refused(INSTRUCTIONS, self.body, request - removable)[0]
        self.assertEqual(exc.extraction_advice(False, 8), f'{RERUN}{SPLIT}.')

    def test_revise_step_in_table_form_stops_at_the_bytes_a_revision_can_remove(self):
        request = self.refused(INSTRUCTIONS, self.body, 1)[0].request_bytes
        table = table_bytes(self.body['dictionary'])
        removable = table - len(serialized(EMPTY).encode())
        exc = self.refused(INSTRUCTIONS, self.body, request - removable)[0]
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{revise_text(table, removable)} 또는 {SPLIT}.')
        exc = self.refused(INSTRUCTIONS, self.body, request - removable - 1)[0]
        self.assertEqual(exc.extraction_advice(False, 2), f'{RERUN}{SPLIT}.')


class ExtractionRefusal(ServiceCase):
    def test_later_chunk_refusal_records_exact_detail_and_dispatches_nothing(self):
        sizes = self.request_sizes(RISING)
        self.assertEqual(sizes, sorted(set(sizes)))
        doc, dictionary = self.three_chunks()
        limit = sizes[2] - 1
        stub = Stub(chunk_response(dictionary), limit)
        result = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(result['status'], 'error')
        request, table = budget_sizes(self.budget(result, '3'))
        self.assertEqual((request, table), (sizes[2], table_bytes(dictionary)))
        self.assertEqual(result['detail'], f'청크 3 · {refused_text(request, limit)}{RERUN}{revise_text(table, 1)} 또는 {SPLIT}. · 검증 완료 0/3청크 · 새 호출 0회')
        self.assertEqual(self.budget(result, '3')['status'], 'unfit')
        self.assertEqual(self.budget_folders(result), ['3'])
        self.assertEqual(self.item(doc)['status'], 'approved')

    def test_reports_largest_excess_and_the_stated_reduction_passes_every_chunk_with_all_definitions(self):
        sizes = self.request_sizes(RISING)
        doc, dictionary = self.three_chunks()
        limit = sizes[1] - 1
        result = self.run_job(doc, Stub(chunk_response(dictionary), limit))
        excess = sizes[2] - limit
        self.assertEqual(result['detail'], f'청크 3 · {refused_text(sizes[2], limit)}{RERUN}{revise_text(table_bytes(dictionary), excess)} 또는 {SPLIT}. · 검증 완료 0/3청크 · 새 호출 0회')
        smaller = copy.deepcopy(dictionary)
        while table_bytes(dictionary) - table_bytes(smaller) < excess:
            smaller['entities'].pop()
        self.revise_and_approve(doc, smaller)
        stub = Stub(chunk_response(smaller), limit)
        done = self.run_job(doc, stub)
        self.assertEqual(done['status'], 'success', done['detail'])
        self.assertEqual(len(stub.calls), 3)
        for prompt in stub.calls:
            self.assertLessEqual(len(prompt.encode()), limit)
            self.assertEqual(decode(body_of(prompt)['approved_dictionary']['payload']), dictionary_context(smaller))

    def test_admission_agrees_with_dispatch_at_the_exact_limit(self):
        sizes = self.request_sizes(('x', 'x', 'x'))
        doc, dictionary = self.three_chunks(('x', 'x', 'x'))
        top = max(sizes)
        stub = Stub(chunk_response(dictionary), top - 1)
        result = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(budget_sizes(self.budget(result, self.budget_folders(result)[0]))[0], top)
        stub = Stub(chunk_response(dictionary), top)
        result = self.run_job(doc, stub)
        self.assertEqual(result['status'], 'success', result['detail'])
        self.assertEqual([len(prompt.encode()) for prompt in stub.calls], sizes)

    def test_fitting_run_writes_a_budget_only_for_the_chunk_it_reached(self):
        sizes = self.request_sizes(('x', 'x', 'x'))
        doc, _ = self.three_chunks(('x', 'x', 'x'))

        def fail(prompt, count):
            raise RuntimeError('stop')
        result = self.run_job(doc, Stub(fail, max(sizes)))
        self.assertEqual(self.budget_folders(result), ['1'])

    def test_ordinary_failure_keeps_the_resume_note(self):
        doc, _ = self.three_chunks(('x', 'x', 'x'))

        def fail(prompt, count):
            raise ValueError('stop')
        result = self.run_job(doc, Stub(fail))
        self.assertTrue(result['detail'].endswith(TAIL), result['detail'])

    def test_refused_rerun_on_any_model_of_the_same_agent_dispatches_nothing_and_repeats_the_detail(self):
        doc, dictionary = self.three_chunks()
        limit = self.request_sizes(RISING)[2] - 1
        first = self.run_job(doc, Stub(chunk_response(dictionary), limit))
        for model in ('auto', 'other-model'):
            with self.subTest(model=model):
                stub = Stub(chunk_response(dictionary), limit)
                again = self.run_job(doc, stub, model=model)
                self.assertEqual(stub.calls, [])
                self.assertTrue(again['detail'].startswith(first['detail']), again['detail'])

    def test_resume_reuses_a_verified_chunk_over_the_new_limit_and_refuses_nothing(self):
        fills = ('가', 'x', 'x')
        sizes = self.request_sizes(fills)
        doc, dictionary = self.three_chunks(fills)
        respond = chunk_response(dictionary)

        def stop_at_second(prompt, count):
            if count == 2:
                raise ValueError('stop')
            return respond(prompt, count)
        self.run_job(doc, Stub(stop_at_second))
        limit = max(sizes[1:])
        self.assertLess(limit, sizes[0])
        stub = Stub(respond, limit)
        result = self.run_job(doc, stub)
        self.assertEqual(result['status'], 'success', result['detail'])
        self.assertEqual(len(stub.calls), 2)

    def test_refusal_after_a_verified_chunk_reports_it_without_the_resume_note_and_revising_calls_every_chunk(self):
        sizes = self.request_sizes(RISING)
        doc, dictionary = self.three_chunks()
        respond = chunk_response(dictionary)

        def stop_at_second(prompt, count):
            if count == 2:
                raise ValueError('stop')
            return respond(prompt, count)
        self.run_job(doc, Stub(stop_at_second))
        limit = sizes[2] - 1
        stub = Stub(respond, limit)
        result = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertTrue(result['detail'].startswith(f'청크 3 · {refused_text(sizes[2], limit)}{RERUN}'), result['detail'])
        self.assertIn('검증 완료 1/3청크 · 새 호출 0회 · 1청크 재사용', result['detail'])
        self.assertNotIn('추출 조건이 이 실행과 같으면', result['detail'])
        smaller = {**dictionary, 'entities': dictionary['entities'][:-10]}
        self.revise_and_approve(doc, smaller)
        stub = Stub(chunk_response(smaller), limit)
        done = self.run_job(doc, stub)
        self.assertEqual(done['status'], 'success', done['detail'])
        self.assertEqual(len(stub.calls), 3)

    def test_other_agent_step_needs_a_ready_provider_other_than_this_one_and_ollama(self):
        fills = ('x', 'x', '가')
        sizes = self.request_sizes(fills)
        doc, dictionary = self.three_chunks(fills)
        limit = sizes[2] - 1
        expected_without = f'{RERUN}{revise_text(table_bytes(dictionary), 1)} 또는 {SPLIT}.'
        expected_with = f'{RERUN}{OTHER} 또는 {revise_text(table_bytes(dictionary), 1)} 또는 {SPLIT}.'

        def raising(agent):
            raise OSError('unknown')
        raises = Stub(chunk_response(dictionary), limit)
        raises.ready = raising

        def first_raises(agent):
            if agent == 'claude':
                raise OSError('unknown')
            return agent == 'cursor'
        mixed = Stub(chunk_response(dictionary), limit)
        mixed.ready = first_raises
        for name, stub, advice in (('no ready method', Stub(chunk_response(dictionary), limit), expected_without),
                                   ('only itself and ollama', Stub(chunk_response(dictionary), limit, {'copilot', 'ollama'}), expected_without),
                                   ('ready raises', raises, expected_without),
                                   ('a later provider is ready after one raises', mixed, expected_with),
                                   ('another ready', Stub(chunk_response(dictionary), limit, {'claude'}), expected_with)):
            with self.subTest(name):
                result = self.run_job(doc, stub)
                self.assertTrue(result['detail'].startswith(f'청크 3 · {refused_text(sizes[2], limit)}{advice} · 검증 완료 0/3청크 · 새 호출 0회'), result['detail'])

    def test_real_cli_readiness_decides_availability_with_executables_patched(self):
        cli = CLI(persistent=False)
        self.addCleanup(cli.close)
        service = Service(self.store, self.root, cli)
        self.addCleanup(service.close)
        with mock.patch('verinote.agents.executable', return_value=None):
            self.assertFalse(service.other_agent_ready('ollama'))
        with mock.patch('verinote.agents.executable', side_effect=lambda agent: '/fake' if agent == 'claude' else None):
            self.assertTrue(service.other_agent_ready('ollama'))
            self.assertFalse(service.other_agent_ready('claude'))

    def test_real_cli_readiness_counts_a_configured_api_key_with_no_executable(self):
        adapter = mock.Mock()
        adapter.ready.side_effect = lambda credentials: bool(credentials.api_key)
        cli = CLI(persistent=False, http_adapter=adapter, credentials=Credentials('key'))
        self.addCleanup(cli.close)
        service = Service(self.store, self.root, cli)
        self.addCleanup(service.close)
        with mock.patch('verinote.agents.executable', return_value=None):
            self.assertTrue(service.other_agent_ready('ollama'))
            self.assertFalse(service.other_agent_ready('openrouter'))

    def test_invalid_transport_budget_keeps_its_own_detail(self):
        doc, dictionary = self.three_chunks()
        stub = Stub(chunk_response(dictionary), 0)
        result = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertTrue(result['detail'].startswith('제공자의 입력 바이트 한도가 올바르지 않습니다.'), result['detail'])

    def test_stated_reduction_across_a_revision_digit_is_enough_and_one_byte_less_is_refused(self):
        doc, dictionary = self.at_revision_eight()
        probe = self.run_job(doc, Stub(chunk_response(dictionary), 1))
        request, table = budget_sizes(self.budget(probe, '3'))
        smaller = {**dictionary, 'entities': dictionary['entities'][:-10]}
        removed = table - table_bytes(smaller)

        # One byte under the stated figure: a reduction of exactly the excess misses by the digit the revision gains.
        limit = request - removed
        short = self.run_job(doc, Stub(chunk_response(dictionary), limit))
        self.assertIn(revise_text(table, removed + 1), short['detail'])
        self.revise_and_approve(doc, smaller)
        self.assertEqual(self.item(doc)['revision'], 10)
        stub = Stub(chunk_response(smaller), limit)
        refused = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(budget_sizes(self.budget(refused, '3'))[0], limit + 1)

        # A fresh document at the same revision, with a limit that makes the stated figure exactly what is removed.
        fresh = ServiceCase('setUp')
        fresh.setUp()
        self.addCleanup(fresh.doCleanups)
        other, _ = fresh.at_revision_eight()
        limit = request - removed + 1
        stated = fresh.run_job(other, Stub(chunk_response(dictionary), limit))
        self.assertIn(revise_text(table, removed), stated['detail'])
        fresh.revise_and_approve(other, smaller)
        stub = Stub(chunk_response(smaller), limit)
        done = fresh.run_job(other, stub)
        self.assertEqual(done['status'], 'success', done['detail'])
        self.assertEqual(len(stub.calls), 3)

    def test_stated_other_agent_step_calls_every_chunk_again_despite_a_verified_chunk(self):
        fills = RISING
        limit = self.request_sizes(fills)[2] - 1
        doc, dictionary = self.three_chunks(fills)
        respond = chunk_response(dictionary)

        def stop_at_second(prompt, count):
            if count == 2:
                raise ValueError('stop')
            return respond(prompt, count)
        self.run_job(doc, Stub(stop_at_second))
        self.assertIn(OTHER, self.run_job(doc, Stub(respond, limit, {'claude'}))['detail'])
        stub = Stub(respond)
        result = self.run_job(doc, stub, agent='claude', model='m')
        self.assertEqual(result['status'], 'success', result['detail'])
        self.assertEqual(len(stub.calls), 3)

    def test_hangul_chunk_refused_at_the_ollama_budget_with_an_empty_dictionary_offers_no_revise(self):
        doc = self.register('가' * 8000)
        approved_dictionary(self.store, doc['id'], EMPTY)
        limit = input_byte_limit()
        for ready, advice in (({'claude'}, f'{RERUN}{OTHER} 또는 {SPLIT}.'), (set(), f'{RERUN}{SPLIT}.')):
            with self.subTest(ready=ready):
                stub = Stub(lambda prompt, count: {}, limit, ready)
                result = self.run_job(doc, stub)
                self.assertEqual(stub.calls, [])
                request = budget_sizes(self.budget(result, '1'))[0]
                self.assertTrue(result['detail'].startswith(f'청크 1 · {refused_text(request, limit)}{advice} · 검증 완료 0/1청크 · 새 호출 0회'), result['detail'])

    def test_scheduler_path_dispatches_nothing_and_records_the_same_detail(self):
        sizes = self.request_sizes(RISING)
        doc, dictionary = self.three_chunks()
        limit = sizes[2] - 1
        respond = chunk_response(dictionary)

        class Caller:
            extraction_spec = ExtractionSpec('/fake/claude', 'model', 2, 10, 1048576, 'null')
            calls: list = []
            def prompt_byte_limit(self, agent): return limit
            def limited_timeout(self, agent, seconds): return self
            def complete(self, agent, model, prompt, folder):
                self.calls.append(prompt)
                return respond(prompt, len(self.calls))
            def close(self): pass
        caller = Caller()
        service = Service(self.store, self.root, caller, extraction_caller=caller)
        self.addCleanup(service.close)
        job = service.start(doc['id'], 'claude', 'model', expected_version=doc['version'], expected_generation=doc['generation'], extraction_concurrency=2)
        service.pool.shutdown(wait=True)
        result = self.store.run(job['id'])
        self.assertEqual(Caller.calls, [])
        self.assertEqual(result['detail'], f'청크 3 · {refused_text(budget_sizes(self.budget(result, "3"))[0], limit)}{RERUN}{revise_text(table_bytes(dictionary), 1)} 또는 {SPLIT}. · 검증 완료 0/3청크 · 새 호출 0회')


class RepairRefusal(ServiceCase):
    def repair_run(self, dictionary, source, ready=None, revisions=0):
        """A chunk call within budget whose quote issues push the repair request over it, as one paid call."""
        doc = self.register(source)
        approved_dictionary(self.store, doc['id'], dictionary)
        for _ in range(revisions):
            self.revise_and_approve(doc, dictionary)
        fits = Stub(chunk_response(dictionary))
        probe = ServiceCase('setUp')
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        probe_doc = probe.register(source)
        approved_dictionary(probe.store, probe_doc['id'], dictionary)
        self.assertEqual(probe.run_job(probe_doc, fits)['status'], 'success')
        limit = len(fits.calls[0].encode()) + 600

        def bad_quote(prompt, count):
            return extraction_response(facts=[], dictionary=dictionary, page={'title': 'L', 'claims': [{'heading': 'h', 'text': '가' * 1400, 'quote': 'absent' * 1300}]})
        stub = Stub(bad_quote, limit, ready)
        result = self.run_job(doc, stub)
        self.assertEqual(len(stub.calls), 1, result['detail'])
        return result, limit, budget_sizes(self.budget(result, '1', 'quote-repair'))

    def large(self):
        names = [f'C{i:05}' for i in range(300)]
        return rows(names, ' '.join(names)), ' '.join(names)

    def test_revise_step_present_and_other_agent_gated_with_the_resume_note_kept(self):
        dictionary, source = self.large()
        for ready, other in ((None, ''), ({'claude'}, f'{OTHER} 또는 ')):
            with self.subTest(ready=ready):
                result, limit, (request, definitions) = self.repair_run(dictionary, source + (' ' if ready else ''), ready)
                self.assertIn(f'인용 보정 실패 (청크당 1회). {refused_text(request, limit)}{RETRY}{other}{revise_text(definitions, request - limit)} 또는 {SPLIT}. · 검증 완료 0/1청크 · 새 호출 1회{TAIL}', result['detail'])

    def test_repair_refusal_figure_at_pin_revision_eight_adds_the_digit(self):
        dictionary, source = self.large()
        result, limit, (request, definitions) = self.repair_run(dictionary, source, revisions=3)
        self.assertEqual(self.item({'id': result['document_id']})['revision'], 8)
        self.assertIn(revise_text(definitions, request - limit + 1), result['detail'])

    def test_revise_step_absent_when_the_dictionary_cannot_cover_the_excess(self):
        dictionary = {'entities': [{'id': 'e', 'name': '나다라마', 'type': 'T', 'quote': '나다라마'}], 'aliases': [], 'relations': []}
        result, limit, (request, definitions) = self.repair_run(dictionary, '표지 ' + '나다라마 ' * 300)
        self.assertGreater(request - limit, definitions)
        self.assertIn(f'인용 보정 실패 (청크당 1회). {refused_text(request, limit)}{RETRY}{SPLIT}. · 검증 완료 0/1청크 · 새 호출 1회{TAIL}', result['detail'])


class GenerationRefusal(ServiceCase):
    def test_refusal_at_the_policy_limit_states_the_passed_prefix_and_offers_only_a_split(self):
        doc = self.register(generation_source(8))
        stub = Stub(generated, None, {'claude'})
        result = self.run_job(doc, stub, 'ontology')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(len(stub.calls), 3)
        request = budget_sizes(self.budget(result, '4'))[0]
        self.assertGreater(request, MAX_PROMPT_BYTES)
        self.assertEqual(result['detail'], f'{refused_text(request, MAX_PROMPT_BYTES)}{GENERATION_RERUN}문서를 앞에서 통과한 12,000자 이하로 나누어 각각 사전 생성{GENERATION_END} · 검증 완료 12,000/32,000자 · 새 호출 3회')

    def test_rerun_refuses_again_on_the_same_model_and_calls_again_on_another(self):
        doc = self.register(generation_source(8))
        first = self.run_job(doc, Stub(generated), 'ontology')
        same = Stub(generated)
        again = self.run_job(doc, same, 'ontology')
        self.assertEqual(same.calls, [])
        self.assertIn(GENERATION_RERUN, again['detail'])
        self.assertEqual(budget_sizes(self.budget(again, '4'))[0], budget_sizes(self.budget(first, '4'))[0])
        for agent, model in (('copilot', 'other-model'), ('claude', 'auto')):
            with self.subTest(agent=agent, model=model):
                other = Stub(generated)
                self.run_job(doc, other, 'ontology', agent=agent, model=model)
                self.assertEqual(len(other.calls), 3)

    def test_refusal_at_the_ollama_budget_gates_the_other_agent_on_readiness(self):
        limit = input_byte_limit()
        for ready, other in (({'claude'}, f'{OTHER} 또는 '), (set(), '')):
            with self.subTest(ready=ready):
                doc = self.register(generation_source(3) + ('y' if ready else 'z'))
                stub = Stub(generated, limit, ready)
                result = self.run_job(doc, stub, 'ontology')
                self.assertEqual(len(stub.calls), 1)
                request = budget_sizes(self.budget(result, '2'))[0]
                self.assertLess(request, MAX_PROMPT_BYTES)
                self.assertEqual(result['detail'], f'{refused_text(request, limit)}{GENERATION_RERUN}{other}문서를 앞에서 통과한 4,000자 이하로 나누어 각각 사전 생성{GENERATION_END} · 검증 완료 4,000/{len(generation_source(3)) + 1:,}자 · 새 호출 1회')

    def test_refusal_on_the_first_section_names_no_prefix(self):
        doc = self.register(generation_source(1))
        probe = self.run_job(doc, Stub(generated, 1), 'ontology')
        request = budget_sizes(self.budget(probe, '1'))[0]
        stub = Stub(generated, request - 1, {'claude'})
        result = self.run_job(doc, stub, 'ontology')
        self.assertEqual(stub.calls, [])
        self.assertTrue(result['detail'].startswith(f'{refused_text(request, request - 1)}{GENERATION_RERUN}{OTHER} 또는 문서를 더 작게 나누어 각각 사전 생성{GENERATION_END} · 검증 완료 0/4,000자 · 새 호출 0회'), result['detail'])


class StatedPaths(ServiceCase):
    def test_followed_other_agent_step_is_refused_at_a_later_section_after_paying_for_its_calls(self):
        doc = self.register(generation_source(8))
        offered = self.run_job(doc, Stub(generated, input_byte_limit(), {'claude'}), 'ontology')
        self.assertIn(OTHER, offered['detail'])
        self.assertIn(GENERATION_END, offered['detail'])
        stub = Stub(generated)
        followed = self.run_job(doc, stub, 'ontology', agent='claude', model='m')
        self.assertEqual(followed['status'], 'error')
        self.assertEqual(len(stub.calls), 3)
        self.assertGreater(budget_sizes(self.budget(followed, '4'))[0], MAX_PROMPT_BYTES)
        self.assertIn(GENERATION_END, followed['detail'])
        self.assertIn('새 호출 3회', followed['detail'])

    def test_part_of_the_stated_size_can_be_refused_again_after_a_call_and_a_smaller_part_passes(self):
        sparse = lambda letter: (' '.join(f'{letter}{i:05}' for i in range(40))).ljust(4000, 'x')  # noqa: E731
        dense = lambda letter: (' '.join(f'{letter}{i:05}' for i in range(150)) + ' ' + '가' * 4000)[:4000]  # noqa: E731
        source = sparse('A') + sparse('B') + dense('C') + dense('D') + dense('E')
        whole = self.register(source, 'whole')
        measured = Stub(generated)
        self.assertEqual(self.run_job(whole, measured, 'ontology')['status'], 'success')
        limit = len(measured.calls[2].encode()) - 1

        fresh = ServiceCase('setUp')
        fresh.setUp()
        self.addCleanup(fresh.doCleanups)
        doc = fresh.register(source, 'whole')
        refused = fresh.run_job(doc, Stub(generated, limit), 'ontology')
        self.assertIn(f'앞에서 통과한 12,000자 이하로 나누어 각각 사전 생성{GENERATION_END}', refused['detail'])

        part = fresh.register(source[12000:], 'part')
        self.assertEqual(len(source) - 12000, 8000)
        stub = Stub(generated, limit)
        again = fresh.run_job(part, stub, 'ontology')
        self.assertEqual(again['status'], 'error')
        self.assertEqual(len(stub.calls), 1)
        self.assertIn(GENERATION_END, again['detail'])
        smaller = fresh.register(source[12000:16000], 'smaller')
        self.assertEqual(fresh.run_job(smaller, Stub(generated, limit), 'ontology')['status'], 'success')

    def test_split_generate_approve_extract_publishes_with_no_revision_and_every_definition_sent(self):
        source = generation_source(8)
        whole = self.register(source, 'whole')
        refused = self.run_job(whole, Stub(generated), 'ontology')
        passed = int(re.search(r'앞에서 통과한 ([\d,]+)자 이하로', refused['detail']).group(1).replace(',', ''))

        # The stated size finishes generation; extraction sends the finished dictionary and may refuse separately, as stated.
        part = self.register(source[:passed], 'part')
        self.assertEqual(self.run_job(part, Stub(generated), 'ontology')['status'], 'success')
        self.store.review(self.item(part)['id'], self.item(part)['revision'], 'approved', 'R', 'ok')
        late = Stub(lambda prompt, count: {})
        refused_extraction = self.run_job(part, late)
        self.assertEqual(late.calls, [])
        self.assertIn(SPLIT, refused_extraction['detail'])

        size = passed // 2
        for number, start in enumerate(range(0, passed, size)):
            doc = self.register(source[start:start + size], f'part{number}')
            with self.assertRaisesRegex(ValueError, '사전'):
                Service(self.store, self.root, Stub(generated)).start(doc['id'], 'copilot', 'auto', 'extract', doc['version'], doc['generation'])
            self.assertEqual(self.run_job(doc, Stub(generated), 'ontology')['status'], 'success')
            item = self.item(doc)
            self.assertEqual(item['revision'], 1)
            self.store.review(item['id'], item['revision'], 'approved', 'R', 'ok')
            payload = self.item(doc)['payload']
            stub = Stub(chunk_response(payload))
            result = self.run_job(doc, stub)
            self.assertEqual(result['status'], 'success', result['detail'])
            sent = body_of(stub.calls[0])['approved_dictionary']['payload']
            self.assertEqual(decode(sent) if 'dictionary_wire_version' in sent else sent, dictionary_context(payload))
            self.assertLessEqual(len(stub.calls[0].encode()), MAX_PROMPT_BYTES)

    def test_term_only_outside_a_part_is_refused_in_that_parts_dictionary(self):
        inside, outside = 'C00001 내용', 'D00002 내용'
        validate_dictionary(rows(['C00001'], inside), inside)
        with self.assertRaises(ValueError):
            validate_dictionary(rows(['D00002'], outside), inside)

    def test_generation_refusal_at_the_policy_limit_mentions_no_setting_and_no_revision(self):
        doc = self.register(generation_source(8))
        detail = self.run_job(doc, Stub(generated), 'ontology')['detail']
        self.assertNotIn('설정', detail)
        self.assertNotIn('사전을 수정', detail)
