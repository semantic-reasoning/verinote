"""Generation-end warning (#825) when extraction by the same agent would be refused by the input budget; recomputing it after an edit or a policy change is #850."""
import contextlib
import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

import test_large_dictionary_refusal as T
from test_large_dictionary_refusal import (
    ServiceCase,
    Stub,
    chunk_response,
    generated,
    generation_source,
)

from verinote import dictionary_context as dc
from verinote import dictionary_pipeline as dp
from verinote.dictionary_context import (
    ADMISSION_RECORD,
    EMPTY_DEFINITION_BYTES,
    RequestTooLarge,
    read_admission,
)
from verinote.service import Service

ROOT = Path(__file__).resolve().parents[1]
SRC = generation_source(2)
R = 86_075          # measured: same-agent extraction request of SRC's generated dictionary at approval revision 2
BIG = generation_source(3)
HEAD = '추출 입력 한도 경고: 이 사전을 수정하지 않고 이 실행과 같은 에이전트(GitHub Copilot)로 추출하면 '
MAY_DIFFER = '입력 예산은 에이전트에 따라 다르므로(앱 정책 최대 120,000바이트) 다른 에이전트로는 거부되지 않을 수 있습니다.'
ALL_REFUSE = '어느 에이전트의 입력 예산도 앱 정책 120,000바이트를 넘지 않으므로 다른 에이전트로도 거부됩니다.'
OTHER = '입력 예산이 120,000바이트인 다른 에이전트로 추출(원문과 사전이 해당 제공자에게 전달됨)'
SPLIT = '문서를 나누어 각각 사전 생성·승인(이 사전은 재사용되지 않음, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함)'


def trim(defs, needed):
    return f'사전을 수정해 전송 정의 {defs:,}바이트 중 약 {needed:,}바이트 이상을 줄인 뒤 승인(뺀 정의는 추출에 쓸 수 없음)'


class Case(ServiceCase):
    def generate(self, source, limit, ready=None, **kw):
        doc = self.register(source)
        stub = Stub(generated, limit, ready)
        run = self.run_job(doc, stub, 'ontology', **kw)
        self.assertEqual(run['status'], 'success', run['detail'])
        return doc, run, stub

    def approve_and_extract(self, doc, limit):
        item = self.item(doc)
        self.store.review(item['id'], item['revision'], 'approved', 'R', 'ok')
        stub = Stub(chunk_response(self.item(doc)['payload']), limit)
        return self.run_job(doc, stub), stub

    def record(self, run):
        return json.loads((self.root / 'runs' / run['id'] / ADMISSION_RECORD).read_text(encoding='utf-8'))


class BothSides(Case):
    def test_one_byte_over_warns_with_the_size_extraction_then_refuses_and_the_exact_limit_does_not(self):
        doc, run, stub = self.generate(SRC, R - 1)
        self.assertEqual(len(stub.calls), 2)  # no extra model call
        expected = (HEAD + f'전체 요청이 {R:,}바이트 이상으로 입력 예산 {R - 1:,}바이트를 넘어 첫 호출 전에 거부됩니다. {MAY_DIFFER} '
                    f'다음 단계: {trim(72_740, 1)} 또는 {SPLIT}.')
        self.assertTrue(run['detail'].startswith(expected + ' · '), run['detail'])
        self.assertEqual(self.record(run)['warning'], expected)
        self.assertEqual(self.budget_folders(run), ['1', '2'])  # the check writes no context-budget record
        refused, ext = self.approve_and_extract(doc, R - 1)
        self.assertEqual((refused['status'], ext.calls), ('error', []))
        self.assertIn(f'전체 요청 {R:,}바이트가 입력 예산 {R - 1:,}바이트', refused['detail'])

        doc, run, _ = self.generate(SRC, R)
        self.assertNotIn('추출 입력 한도', run['detail'])
        self.assertIsNone(self.record(run)['warning'])
        done, _ = self.approve_and_extract(doc, R)
        self.assertEqual(done['status'], 'success', done['detail'])

    def test_regeneration_counts_the_digit_its_approval_adds(self):
        doc = self.register(SRC)
        self.run_job(doc, Stub(generated, 10 ** 6), 'ontology')
        item = self.item(doc)
        while not (item['status'] == 'rejected' and item['revision'] == 8):
            act = 'rejected' if item['revision'] == 7 else ('held' if item['status'] != 'held' else 'pending')
            self.store.review(item['id'], item['revision'], act, 'R', 'x'); item = self.item(doc)
        service = Service(self.store, self.root, Stub(generated, R))
        self.addCleanup(service.close)
        job = service.start(doc['id'], 'copilot', 'auto', 'ontology', doc['version'], doc['generation'], regenerate={'id': item['id'], 'revision': 8})
        service.pool.shutdown(wait=True)
        run = self.store.run(job['id'])
        self.assertIn(f'전체 요청이 {R + 1:,}바이트 이상으로 입력 예산 {R:,}바이트', run['detail'])  # approval at r10
        refused, _ = self.approve_and_extract(doc, R)
        self.assertEqual(self.item(doc)['revision'], 10)
        self.assertIn(f'전체 요청 {R + 1:,}바이트', refused['detail'])


class Stated(Case):
    def test_removing_the_stated_bytes_then_approving_passes_and_one_byte_short_is_refused(self):
        doc = self.register(SRC); self.run_job(doc, Stub(generated, R - 1), 'ontology')
        full = self.item(doc)['payload']
        smaller = {**full, 'entities': full['entities'][:-10]}
        defs = T.table_bytes(full)
        removed = defs - T.table_bytes(smaller)
        for limit, ok in ((R - removed, True), (R - removed - 1, False)):
            doc = self.register(SRC)
            run = self.run_job(doc, Stub(generated, limit), 'ontology')
            self.assertIn(trim(defs, R - limit), run['detail'])
            self.revise_and_approve(doc, smaller)
            self.assertEqual(self.item(doc)['revision'], 3)
            done, _ = self.run_job(doc, Stub(chunk_response(smaller), limit)), None
            self.assertEqual(done['status'] == 'success', ok, done['detail'])

    def test_every_chunk_is_checked_not_only_the_first(self):
        source = SRC + '가' * 8000
        doc = self.register(source, 'h')
        probe = self.run_job(doc, Stub(generated, 10 ** 6), 'ontology')
        self.assertNotIn('추출 입력 한도', probe['detail'])
        doc2 = self.register(source, 'h')
        run = self.run_job(doc2, Stub(generated, 95_000), 'ontology')
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('추출 입력 한도 경고', run['detail'])
        refused, ext = self.approve_and_extract(doc2, 95_000)
        self.assertTrue(refused['detail'].startswith('청크 2 · '), refused['detail'])


class Forms(Case):
    def test_plain_form_smaller_refusal_has_the_same_size_in_warning_and_extraction(self):
        source = ''.join(f'{"ABCDEFGH"[part]}{0:05} ' + '가' * 3993 for part in range(2))
        doc = self.register(source)
        stub = Stub(generated, 10 ** 6)
        self.run_job(doc, stub, 'ontology')
        payload = self.item(doc)['payload']
        from verinote.documents import chunks
        from verinote.ontology import EXTRACTION_CONTRACT_VERSION, extraction_prompts
        from verinote.store import digest
        pin = {'id': 'x' * 32, 'revision': 2, 'payload': payload, 'sha256': digest(payload), 'extraction_contract': EXTRACTION_CONTRACT_VERSION}
        exc = dc.worst_refusal(Stub(None, 1), 'copilot', extraction_prompts(EXTRACTION_CONTRACT_VERSION)[0],
                               dc.chunk_requests(doc, pin, list(enumerate(chunks(source), 1))), 60000)[1]
        request = exc.request_bytes
        doc = self.register(source)
        run = self.run_job(doc, Stub(generated, request - 1), 'ontology')
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn(f'전체 요청이 {request:,}바이트 이상으로', run['detail'])
        refused, _ = self.approve_and_extract(doc, request - 1)
        record = self.budget(refused, '1')
        self.assertLess(record['raw_prompt_bytes'], record['encoded_prompt_bytes'])  # the plain form is the smaller one
        self.assertEqual(record['raw_prompt_bytes'], request)
        self.assertIn(f'전체 요청 {request:,}바이트', refused['detail'])

    def test_equal_largest_excess_reports_the_first_such_chunk(self):
        names = ['C00001']
        dictionary = T.rows(names, 'C00001')
        source = 'C00001 ' + 'x' * 7993 + 'x' * 8000 + '가' * 8000 + '가' * 8000
        doc = self.register(source)
        T.approved_dictionary(self.store, doc['id'], dictionary)
        sizes = self.request_sizes_for(doc)
        self.assertEqual(sizes[2], sizes[3])
        self.assertLess(sizes[1], sizes[2])
        stub = Stub(chunk_response(dictionary), sizes[2] - 1)
        refused = self.run_job(doc, stub)
        self.assertEqual(stub.calls, [])
        self.assertTrue(refused['detail'].startswith('청크 3 · '), refused['detail'])

    def request_sizes_for(self, doc):
        from verinote.dictionary_context import refusal
        from verinote.documents import chunks
        from verinote.ontology import extraction_prompts
        item = self.item(doc)
        from verinote.store import digest
        pin = {'id': item['id'], 'revision': item['revision'], 'payload': item['payload'], 'sha256': digest(item['payload']), 'extraction_contract': 2}
        text = self.store.document(doc['id'])['text']
        return [refusal(Stub(None, 1), 'copilot', extraction_prompts(2)[0], payload, 60000).request_bytes
                for _, payload in dc.chunk_requests(self.store.document(doc['id']), pin, list(enumerate(chunks(text), 1)))]


class OtherAgents(Case):
    def test_a_ready_provider_adds_the_step_only_when_the_policy_budget_admits_the_request(self):
        _, run, _ = self.generate(SRC, R - 1, ready={'claude'})
        self.assertIn(f'다음 단계: {OTHER} 또는 ', run['detail'])
        self.assertIn(MAY_DIFFER, run['detail'])
        _, run, _ = self.generate(BIG, None, ready={'claude'})
        self.assertIn(ALL_REFUSE, run['detail'])
        self.assertNotIn(OTHER, run['detail'])
        self.assertIn('이상으로 입력 예산 120,000바이트를 넘어', run['detail'])


class Copy(unittest.TestCase):
    def warn(self, request, limit, defs, rev=2, ready=False):
        return RequestTooLarge(request, limit, defs).approval_warning('X', ready, rev)

    def test_other_agent_sentence_switches_at_exactly_the_policy_budget(self):
        self.assertIn(MAY_DIFFER, self.warn(120_000, 119_999, 10 ** 6))
        self.assertIn(ALL_REFUSE, self.warn(120_001, 120_000, 10 ** 6))
        self.assertIn(OTHER, self.warn(120_000, 119_999, 10 ** 6, ready=True))
        self.assertNotIn(OTHER, self.warn(120_001, 120_000, 10 ** 6, ready=True))
        # The sentence follows the request, not the budget: the Ollama budget on both sides.
        self.assertIn(ALL_REFUSE, self.warn(120_001, 27_648, 10 ** 6))
        self.assertIn(MAY_DIFFER, self.warn(120_000, 27_648, 10 ** 6))

    def test_no_variant_names_execution_options_as_a_budget_lever(self):
        # ollama.options() rejects any num_ctx/num_predict but the defaults, and no other transport has a budget reader.
        for request, limit, defs, ready in ((120_000, 27_648, 10 ** 6, True), (120_001, 120_000, 10 ** 6, True), (30_000, 27_648, 0, False)):
            self.assertNotIn('실행 옵션', self.warn(request, limit, defs, ready=ready))

    def test_trim_figure_covers_the_digit_a_revision_and_reapproval_add_for_an_approved_item(self):
        # Pending: revise then approve lands at approval_revision + 1; approved: at + 2. The figure is sufficient for both.
        self.assertIn(trim(10 ** 6, 6), self.warn(105, 100, 10 ** 6, rev=8))    # approved r8 -> r10 adds a digit; pending would need 5
        self.assertIn(trim(10 ** 6, 5), self.warn(105, 100, 10 ** 6, rev=10))
        self.assertIn(trim(10 ** 6, 5), self.warn(105, 100, 10 ** 6, rev=97))   # 97 -> 99
        self.assertIn(trim(10 ** 6, 6), self.warn(105, 100, 10 ** 6, rev=98))   # 98 -> 100

    def test_trim_step_stops_at_the_bytes_a_revision_can_remove(self):
        self.assertIn('사전을 수정해', self.warn(105, 100, 5 + EMPTY_DEFINITION_BYTES))
        self.assertNotIn('사전을 수정해', self.warn(106, 100, 5 + EMPTY_DEFINITION_BYTES))


class Screen(Case):
    def serve(self):
        from verinote.config import Credentials
        from verinote.server import Server
        service = Service(self.store, self.root, Stub(generated))
        server = Server(('127.0.0.1', 0), self.store, service, credentials=Credentials())
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(), service.close()))
        return lambda path: json.loads(urlopen(f'http://127.0.0.1:{server.server_port}{path}', timeout=5).read())

    def test_pending_and_approved_items_carry_the_warning_until_the_dictionary_changes(self):
        doc, run, _ = self.generate(SRC, R - 1)
        get = self.serve()
        item = get('/api/documents/' + doc['id'])['items'][0]
        self.assertEqual(item['extraction_admission'], {'available': True, 'warning': self.record(run)['warning']})
        self.store.review(item['id'], item['revision'], 'approved', 'R', 'ok')
        self.assertEqual(get('/api/documents/' + doc['id'])['items'][0]['extraction_admission']['warning'], self.record(run)['warning'])
        item = self.item(doc)
        smaller = {**item['payload'], 'entities': item['payload']['entities'][:-1]}
        self.store.review(item['id'], item['revision'], 'revise', 'R', 'trim', payload=smaller)
        self.assertIsNone(get('/api/documents/' + doc['id'])['items'][0]['extraction_admission'])

    def hidden_under(self, patches, canary_changes=True):
        """The record of one generation is shown, then hidden while `patches` is active; the canary is recomputed under them."""
        doc, run, _ = self.generate(SRC, R - 1)
        item = self.item(doc)
        self.assertTrue(read_admission(self.root, item)['available'])
        self.addCleanup(dc._admission_canary.cache_clear)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            if canary_changes:
                dc._admission_canary.cache_clear()
            return read_admission(self.root, item)

    def test_a_changed_policy_budget_hides_the_record(self):
        for name, patch in (
                ('MAX_PROMPT_BYTES', mock.patch('verinote.ontology.MAX_PROMPT_BYTES', 119_000)),
                ('Ollama budget', mock.patch('verinote.ollama.input_byte_limit', return_value=30_000)),
                ('extraction prompt', mock.patch('verinote.ontology.extraction_prompts', return_value=('changed', 'r'))),
                ('PROTOCOL', mock.patch.object(dc, 'PROTOCOL', dc.PROTOCOL + 'x'))):
            with self.subTest(name):
                self.assertIsNone(self.hidden_under([patch], canary_changes=False))

    def test_a_contract_version_bump_hides_the_record_although_the_prompt_text_is_the_same(self):
        from verinote.ontology import EXTRACTION_CONTRACT_VERSION, extraction_prompts
        same_text = lambda version: extraction_prompts(EXTRACTION_CONTRACT_VERSION)  # noqa: E731
        patches = [mock.patch('verinote.ontology.EXTRACTION_CONTRACT_VERSION', EXTRACTION_CONTRACT_VERSION + 1),
                   mock.patch('verinote.ontology.extraction_prompts', same_text)]
        # The canary is not recomputed: its pin carries the version as one digit, so it could not tell these apart anyway.
        self.assertIsNone(self.hidden_under(patches, canary_changes=False))

    def test_a_change_to_request_construction_hides_the_record(self):
        from verinote import documents, ontology
        real_chunks, real_encode, real_payload = documents.chunks, dc.encode, dc.chunk_payload
        def padded(definitions):
            result = real_encode(definitions)
            return result | {'strings': result['strings'] + ['pad']}
        for name, patch in (
                ('chunk size', mock.patch('verinote.documents.chunks', lambda text, size=4000: real_chunks(text, size))),
                ('guidance keys, quote kept', mock.patch.object(ontology, 'NOT_GUIDANCE', ontology.JUDGED_FIELDS)),
                ('guidance keys, judged fields kept', mock.patch.object(ontology, 'NOT_GUIDANCE', ('quote',))),
                ('chunk payload keys', mock.patch.object(dc, 'chunk_payload', lambda doc, offset, chunk: real_payload(doc, offset, chunk) | {'extra': 1})),
                ('table encoding', mock.patch.object(dc, 'encode', padded))):
            with self.subTest(name):
                self.assertIsNone(self.hidden_under([patch]))

    def test_the_canary_has_more_than_one_chunk_and_one_dictionary_per_smaller_form(self):
        from verinote.ontology import bounded_prompt, dictionary_context
        sizes = dc._admission_canary()
        for regime in ('plain', 'table'):
            self.assertGreater(len([row for row in sizes if row[0] == regime]), 1, regime)
        for regime, dictionary in zip(('plain', 'table'), dc._canary_dictionaries(), strict=True):
            report = {}
            with self.assertRaises(RequestTooLarge):
                bounded_prompt('i\n', {'approved_dictionary': {'payload': dictionary_context(dictionary)}}, context_limit=1, prompt_limit=1, diagnostics=report)
            self.assertEqual(report['encoded_prompt_bytes'] < report['raw_prompt_bytes'], regime == 'table', (regime, report))

    def test_a_policy_that_cannot_be_computed_hides_the_record_instead_of_failing_the_read(self):
        self.assertIsNone(self.hidden_under([mock.patch.object(dc, 'admission_policy', side_effect=RuntimeError('x'))], canary_changes=False))

    def test_unreadable_record_is_reported_and_a_missing_one_is_silent(self):
        doc, run, _ = self.generate(SRC, R - 1)
        item = self.item(doc)
        (self.root / 'runs' / run['id'] / ADMISSION_RECORD).write_text('{', encoding='utf-8')
        self.assertEqual(read_admission(self.root, item), {'available': False})
        (self.root / 'runs' / run['id'] / ADMISSION_RECORD).unlink()
        self.assertIsNone(read_admission(self.root, item))

    def test_a_well_hashed_record_of_the_wrong_shape_is_unavailable_not_an_error(self):
        doc, run, _ = self.generate(SRC, R - 1)
        item = self.item(doc)
        path = self.root / 'runs' / run['id'] / ADMISSION_RECORD
        good = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(read_admission(self.root, item)['available'], True)
        for name, record in (('key missing', {k: v for k, v in good.items() if k != 'warning'}),
                             ('warning not text', good | {'warning': 5}),
                             ('extra key', good | {'extra': 1})):
            with self.subTest(name):
                path.write_text(json.dumps(record), encoding='utf-8')
                self.assertEqual(read_admission(self.root, item), {'available': False})

    def test_a_record_in_a_folder_that_cannot_be_read_is_unavailable_not_missing(self):
        if os.name == 'nt' or os.geteuid() == 0:
            self.skipTest('permission bits do not deny this process')
        doc, run, _ = self.generate(SRC, R - 1)
        item = self.item(doc)
        folder = self.root / 'runs' / run['id']
        self.addCleanup(os.chmod, folder, 0o755)
        os.chmod(folder, 0)
        self.assertEqual(read_admission(self.root, item), {'available': False})

class NeverFails(Case):
    def test_a_failing_check_keeps_the_generation_and_says_it_was_not_computed(self):
        with mock.patch.object(dc, 'worst_refusal', side_effect=RuntimeError('x')):
            _, run, _ = self.generate(SRC, R - 1)
        self.assertTrue(run['detail'].startswith('추출 입력 한도를 미리 계산하지 못했습니다. 추출을 시작하면 첫 호출 전에 검사합니다. · '))

    def failing_record(self, error):
        real = dp.atomic_json
        def failing(path, value):
            if Path(path).name == ADMISSION_RECORD: raise error
            return real(path, value)
        return mock.patch.object(dp, 'atomic_json', failing)

    def test_an_unsaved_record_is_named_in_the_detail_with_or_without_a_warning(self):
        with self.failing_record(OSError('disk')):
            _, run, _ = self.generate(SRC, R - 1)
        self.assertIn(' 이 경고의 기록을 저장하지 못해 문서 화면에는 표시되지 않습니다. · ', run['detail'])
        with self.failing_record(OSError('disk')):
            _, run, _ = self.generate(SRC, R)
        self.assertTrue(run['detail'].startswith('추출 입력 한도 검사 기록을 저장하지 못했습니다. · '), run['detail'])

    def test_any_exception_saving_the_record_keeps_the_generation(self):
        with self.failing_record(TypeError('not serialisable')):
            doc, run, _ = self.generate(SRC, R - 1)
        self.assertEqual(self.item(doc)['status'], 'pending')
        self.assertIn('이 경고의 기록을 저장하지 못해', run['detail'])


class DetailAndAgent(Case):
    def test_a_detail_over_the_limit_keeps_the_generation_and_its_warning_first(self):
        with mock.patch.object(dp, 'admission_notice', return_value='가' * 2100):
            _, run, _ = self.generate(SRC, R)
        self.assertEqual(len(run['detail']), 2000)
        self.assertTrue(run['detail'].startswith('가' * 2000))

    def test_the_warning_names_the_agent_and_the_budget_the_generation_ran_under(self):
        from verinote.ollama import input_byte_limit
        doc, run, _ = self.generate(SRC, 10 ** 6)
        item = self.item(doc)
        limit = input_byte_limit()
        notice = dp.admission_notice(Stub(None, limit), self.root, {'id': run['id'], 'agent': 'ollama', 'ontology': None},
                                     self.store.document(doc['id']), item['payload'], 60000, lambda: False)
        self.assertIn('이 실행과 같은 에이전트(Ollama (로컬 API))로 추출하면', notice)
        self.assertIn(f'입력 예산 {limit:,}바이트를 넘어', notice)
        self.assertIn(MAY_DIFFER, notice)


class UI(unittest.TestCase):
    def render(self, record):
        script = """
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const ctx=vm.createContext({});
vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
vm.runInContext(source.slice(source.indexOf('function renderExtractionAdmission('),source.indexOf('function renderRelationTypeWarnings(')),ctx);
process.stdout.write(vm.runInContext('renderExtractionAdmission('+process.argv[1]+')',ctx));
"""
        return subprocess.run(['node', '-e', script, json.dumps(record)], cwd=ROOT, text=True, capture_output=True, check=True).stdout

    def test_the_panel_escapes_the_warning_and_is_empty_without_one(self):
        shown = self.render({'available': True, 'warning': '<script>x</script>'})
        self.assertIn('추출 입력 한도 경고', shown)
        self.assertIn('&lt;script&gt;', shown)
        self.assertNotIn('<script>', shown)
        self.assertEqual((self.render(None), self.render({'available': True, 'warning': None})), ('', ''))
        self.assertIn('기록 미확인', self.render({'available': False}))

    def test_approved_sentence_and_panel(self):
        source = (ROOT / 'verinote/web/app.js').read_text(encoding='utf-8')
        self.assertIn("'사전이 승인되었습니다. 이 사전을 기준으로 추출을 시작할 수 있으며, 청크 요청이 입력 예산을 넘으면 첫 호출 전에 거부됩니다.'", source)
        self.assertNotIn('이 사전을 기준으로 추출할 수 있습니다', source)
        self.assertIn('renderExtractionAdmission(item.extraction_admission)', source)
        self.assertIn("'관계별 점검이 포함된 방식으로 다시 추출을 시작할 수 있으며, 청크 요청이 입력 예산을 넘으면 첫 호출 전에 거부됩니다. 이전 승인 이력은 유지되며 중복 관계가 새 검토 후보로 추가될 수 있습니다.'", source)
        self.assertNotIn('다시 추출할 수 있습니다', source[source.index('function extractionControls('):source.index('function renderRelationTypeWarnings(')])


if __name__ == '__main__':
    unittest.main()
