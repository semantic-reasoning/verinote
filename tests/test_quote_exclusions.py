"""Row-level exclusion after a repair call: what is saved, what is reported, what stays a failure."""
import ast
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.extraction_coverage import (
    MAX_EXCLUDED_QUOTE,
    MAX_EXCLUDED_TEXT,
    read_report,
    saved_quote_paths,
    validate_accounting,
)
from verinote.extraction_timing import read_extraction_timing, validate_record
from verinote.extraction_transport import ExtractionSpec
from verinote.ontology import extraction_quote_issues
from verinote.service import Service
from verinote.store import Conflict

CLAIM1 = {'heading': '가', 'text': '주문', 'quote': FACTS[0]['quote']}
CLAIM2 = {'heading': '나', 'text': '결제', 'quote': FACTS[1]['quote']}
MISSING = '원문에 없는 문구'
OFF_DICTIONARY = FACTS[0] | {'subject': '승인 사전에 없는 개체'}
CONSUMED = '이 사전 revision과 추출 방식으로는 다시 추출할 수 없고'
COMPARE = '의미가 맞는지 원문과 대조한 뒤 승인하세요'
DICTIONARY_WORDING = '승인 사전의 표기·타입과 일치하지 않아'


def resp(claims, facts=(), row=None, gaps=()):
    value = extraction_response(list(facts)) | {'claims': copy.deepcopy(list(claims)), 'dictionary_gaps': copy.deepcopy(list(gaps))}
    if row:
        value['coverage'][0].update(row)
    return value


def bad(row):
    return row | {'quote': MISSING}


def reply(*rows):
    return {'repairs': [{'path': path, 'quote': quote} for path, quote in rows]}


def padded(count):
    return SOURCE.ljust(8000) * count


class Exclusions(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, source=SOURCE):
        doc = self.store.register('t', 's.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def run_it(self, doc, outputs, **options):
        calls, pending = [], iter(outputs)
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append(str(folder))
                result = next(pending)
                if isinstance(result, Exception):
                    raise result
                return copy.deepcopy(result)
        service = Service(self.store, self.root, Fake(), **options)
        run = service.start(doc['id'], 'copilot', 'm', expected_version=1, expected_generation=doc.get('generation', 1))
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), calls

    def saved(self, doc):
        items = [i for i in self.store.document(doc['id'])['items'] if i['kind'] != 'ontology']
        return [i['payload'] for i in items if i['kind'] == 'page'], [i['payload'] for i in items if i['kind'] == 'fact']

    def report(self, run):
        return read_report(self.root, self.store, run['id'])

    def render(self, report):
        app = Path('verinote/web/app.js').read_text(encoding='utf-8')
        function = app[app.index('function renderCoverage('):app.index('function usageStage(')]
        script = ("const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');\n"
                  + function + '\nconsole.log(renderCoverage(' + json.dumps(report) + '));')
        return subprocess.check_output(['node'], input=script, text=True)

    def checkpoint_path(self, run):
        return Path(self.root) / 'runs' / run['id'] / 'extraction-checkpoint.json'

    def test_mixed_response_replaces_passed_row_excludes_failed_row_and_detail_starts_with_the_loss(self):
        doc = self.document()
        run, calls = self.run_it(doc, [resp([bad(CLAIM1), bad(CLAIM2)], FACTS[:1]), reply(('claims.0.quote', ''), ('claims.1.quote', CLAIM2['quote']))])
        self.assertEqual(run['status'], 'success', run['detail'])
        pages, _ = self.saved(doc)
        self.assertEqual([c['heading'] for c in pages[0]['claims']], ['나'])
        self.assertEqual(pages[0]['claims'][0]['quote'], CLAIM2['quote'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(run['detail'].startswith('근거 불가로 저장하지 않음: 위키 주장 2개 중 1개·응답 관계 1개 중 0개 (청크 1: 위키 주장 1 인용 · 번호는 보정을 요청한 응답 기준)'), run['detail'])
        # The replaced claim is the first claim left on the page, not the second of the response.
        self.assertIn('이번 실행 원문 인용 1개 보정 · 추가 호출 1회 (청크 1: 위키 주장 1 인용 · 번호는 제외 뒤 기준)', run['detail'])
        self.assertEqual(self.report(run)['excluded_claim_count'], 1)
        self.assertNotIn(MISSING, run['detail'])

    def test_success_detail_states_the_revision_is_consumed_and_store_refuses_the_same_revision(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertIn(CONSUMED, run['detail'])
        self.assertIn('제외 항목만 복구하는 기능은 없습니다', run['detail'])
        self.assertIn('모든 청크를 새로 호출하며 같은 결과가 나올 수 있습니다', run['detail'])
        self.assertFalse(self.store.document(doc['id'])['can_extract'])

    def test_row_without_a_failed_quote_makes_no_repair_call_and_records_no_exclusion(self):
        doc = self.document()
        run, calls = self.run_it(doc, [resp([CLAIM1], FACTS[:1])])
        self.assertEqual((run['status'], len(calls)), ('success', 1), run['detail'])
        self.assertEqual(self.report(run)['excluded_claim_count'], 0)
        self.assertNotIn('근거 불가', run['detail'])

    def test_three_chunks_each_make_one_repair_call_past_the_old_limit_of_two(self):
        doc = self.document(padded(3))
        run, calls = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))] * 3)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(sum(c.endswith('quote-repair') for c in calls), 3)
        self.assertEqual(len(calls), 6)
        self.assertIn('이번 실행 인용 보정 추가 호출 3회: 반영된 보정 없음.', run['detail'])
        self.assertIn(COMPARE, run['detail'])

    def test_timing_record_is_available_after_three_repair_requests(self):
        doc = self.document(padded(3))
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))] * 3)
        self.assertTrue(read_extraction_timing(self.root, self.store, run['id'])['available'])

    def test_sole_evidence_claim_of_a_not_representable_row_is_recorded_and_passes_accounting(self):
        doc = self.document()
        output = resp([bad(CLAIM1), CLAIM2], [], {'status': 'not_representable', 'claim_indices': [0]})
        run, _ = self.run_it(doc, [output, reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        chunk = self.report(run)['chunks'][0]
        row = chunk['coverage'][0]
        self.assertEqual((row['status'], row['claim_indices']), ('not_representable', []))
        self.assertIn('모델 판단이 아닌 로컬 처리', row['reason'])
        self.assertIn('근거 주장의 인용', row['reason'])
        self.assertEqual(chunk['excluded_claims'][0]['predicates'], ['depends_on'])

    def test_relation_losing_its_facts_and_its_evidence_keeps_the_fact_reason_and_records_the_claim(self):
        output = resp([bad(CLAIM1), CLAIM2], [bad(FACTS[0])], {'claim_indices': [0]})
        run, _ = self.run_it(self.document(), [output, reply(('claims.0.quote', ''), ('facts.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        chunk = self.report(run)['chunks'][0]
        self.assertIn('응답 관계의 인용을', chunk['coverage'][0]['reason'])
        self.assertEqual(chunk['excluded_claims'][0]['predicates'], ['depends_on'])

    def test_claim_indices_renumber_and_a_sole_gap_moves_to_the_record(self):
        doc = self.document()
        gap_a = {'code': 'missing_entity', 'detail': 'a', 'claim_indices': [0]}
        gap_b = {'code': 'missing_entity', 'detail': 'b', 'claim_indices': [1]}
        output = resp([bad(CLAIM1), CLAIM2], FACTS[:1], {'claim_indices': [0, 1]}, [gap_a, gap_b])
        run, _ = self.run_it(doc, [output, reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        report = self.report(run)
        chunk = report['chunks'][0]
        self.assertEqual(chunk['coverage'][0]['claim_indices'], [0])
        self.assertEqual([g['detail'] for g in chunk['dictionary_gaps']], ['b'])
        self.assertEqual(chunk['dictionary_gaps'][0]['claim_indices'], [0])
        self.assertEqual(chunk['excluded_claims'][0]['gaps'], [{'code': 'missing_entity', 'detail': 'a'}])
        self.assertEqual((report['gap_count'], report['excluded_gap_count'], report['claim_count']), (1, 1, 1))

    def test_fact_exclusion_renumbers_uses_its_own_code_and_counts_apart_from_the_dictionary(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([CLAIM1], [bad(FACTS[0]), FACTS[1]]), reply(('facts.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        report = self.report(run)
        self.assertEqual(report['chunks'][0]['coverage'][0]['fact_indices'], [0])
        self.assertEqual((report['unverifiable_fact_count'], report['excluded_fact_count'], report['emitted_fact_count']), (1, 0, 2))
        row = report['chunks'][0]['excluded_facts'][0]
        self.assertEqual((row['code'], row['path']), ('unverifiable_quote', 'facts.0.quote'))
        self.assertNotIn(DICTIONARY_WORDING, run['detail'])
        self.assertEqual([f['quote'] for f in self.saved(doc)[1]], [FACTS[1]['quote']])

    def test_every_fact_failing_its_quote_publishes_the_verified_claims_and_states_the_exclusion(self):
        doc = self.document()
        run, calls = self.run_it(doc, [resp([CLAIM1], [bad(FACTS[0])]), reply(('facts.0.quote', ''))])
        self.assertEqual((run['status'], len(calls)), ('success', 2), run['detail'])
        pages, facts = self.saved(doc)
        self.assertEqual(([c['heading'] for c in pages[0]['claims']], facts), (['가'], []))
        self.assertTrue(run['detail'].startswith('근거 불가로 저장하지 않음: 위키 주장 1개 중 0개·응답 관계 1개 중 1개'), run['detail'])
        self.assertIn(CONSUMED, run['detail'])
        row = self.report(run)['chunks'][0]['coverage'][0]
        self.assertEqual(row['status'], 'not_representable')
        self.assertIn('응답 관계의 인용을 원문에서', row['reason'])
        self.assertNotIn('승인 사전', row['reason'])

    def test_dictionary_and_quote_exclusions_leaving_no_fact_stop_with_both_causes_stated(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([CLAIM1], [OFF_DICTIONARY, bad(FACTS[1])]), reply(('facts.0.quote', ''))])
        self.assertEqual(run['status'], 'error', run['detail'])
        self.assertEqual(self.saved(doc), ([], []))
        self.assertIn('응답 관계 2개 중 1개는 승인 사전의 표기·타입과 일치하지 않고 1개는 원문 인용을 확인하지 못해 저장할 후보가 없습니다', run['detail'])
        self.assertIn('응답 관계 2개 중 1개', run['detail'])
        self.assertIn('사전을 수정·재승인하면 모든 청크를 새로 호출합니다', run['detail'])
        self.assertNotIn(CONSUMED, run['detail'])
        self.assertTrue(self.store.document(doc['id'])['can_extract'])
        row = self.report(run)['chunks'][0]['coverage'][0]
        self.assertIn('승인 사전 불일치 또는 인용 미확인', row['reason'])

    def test_dictionary_only_facts_with_an_excluded_claim_stop_and_mention_the_claim(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], [OFF_DICTIONARY]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'error', run['detail'])
        self.assertIn('응답 관계 1개가 모두 승인 사전의 표기·타입과 일치하지 않아', run['detail'])
        self.assertIn('근거 불가로 저장하지 않음: 위키 주장 2개 중 1개·응답 관계 1개 중 0개', run['detail'])

    def test_mixed_chunk_keeps_the_dictionary_row_and_reports_each_notice_separately(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([CLAIM1], [OFF_DICTIONARY, bad(FACTS[0]), FACTS[1]]), reply(('facts.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        report = self.report(run)
        self.assertEqual([r['code'] for r in report['chunks'][0]['excluded_facts']], ['unknown_entity', 'unverifiable_quote'])
        self.assertEqual((report['excluded_fact_count'], report['unverifiable_fact_count'], report['emitted_fact_count']), (1, 1, 3))
        self.assertTrue(run['detail'].startswith('근거 불가로'), run['detail'])
        self.assertIn('응답 관계 3개 중 1개는 승인 사전의 표기·타입과 일치하지 않아', run['detail'])

    def test_one_claim_chunk_confirmed_with_zero_claims_keeps_facts_and_is_reused_without_a_repeat_repair(self):
        doc = self.document(padded(2))
        run, calls = self.run_it(doc, [resp([bad(CLAIM1)], FACTS[:1]), reply(('claims.0.quote', '')), TimeoutError('stop')])
        self.assertEqual((run['status'], len(calls)), ('error', 3))
        again, calls = self.run_it(doc, [resp([CLAIM2], FACTS[1:])])
        self.assertEqual(again['status'], 'success', again['detail'])
        self.assertEqual(len(calls), 1)
        pages, facts = self.saved(doc)
        self.assertEqual([c['heading'] for c in pages[0]['claims']], ['나'])
        self.assertEqual(len(facts), 2)
        report = self.report(again)
        self.assertEqual((report['excluded_claim_count'], report['chunks'][0]['claim_count']), (1, 0))
        self.assertIn('위키 주장 2개 중 1개', again['detail'])
        self.assertEqual(again['detail'].count('근거 불가로 저장하지 않음'), 1)

    def test_whole_run_with_zero_claims_publishes_nothing_names_remedies_and_rerun_makes_no_call(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([bad(CLAIM1)], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(self.saved(doc), ([], []))
        self.assertNotIn(DICTIONARY_WORDING, run['detail'])
        self.assertIn('저장할 주장이 없습니다', run['detail'])
        self.assertIn('사전을 수정·재승인하면 모든 청크를 새로 호출하며 비용이 다시 들고', run['detail'])
        self.assertIn('다른 모델로 바꿔도 모든 청크를 새로 호출', run['detail'])
        self.assertIn('근거 불가로 저장하지 않음: 위키 주장 1개 중 1개', run['detail'])
        self.assertNotIn(CONSUMED, run['detail'])
        self.assertNotIn('추출 조건이 이 실행과 같으면', run['detail'])
        self.assertEqual(run['detail'].count('근거 불가로 저장하지 않음'), 1)
        self.assertTrue(self.store.document(doc['id'])['can_extract'])
        again, calls = self.run_it(doc, [])
        self.assertEqual((again['status'], calls), ('error', []))
        self.assertIn('새 호출 없이 같은 결과로 끝납니다', again['detail'])

    def test_zero_claims_with_dictionary_rows_states_both_notices(self):
        run, _ = self.run_it(self.document(), [resp([bad(CLAIM1)], [OFF_DICTIONARY, FACTS[1]]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'error', run['detail'])
        self.assertIn('저장할 주장이 없습니다', run['detail'])
        self.assertIn('근거 불가로 저장하지 않음: 위키 주장 1개 중 1개', run['detail'])
        self.assertIn('응답 관계 2개 중 1개는 승인 사전의 표기·타입과 일치하지 않아 저장하지 않음', run['detail'])

    def test_malformed_repair_response_and_call_failures_stay_failures_after_two_calls(self):
        for label, correction in (('missing', reply()), ('path', reply(('claims.5.quote', 'x'))), ('list', reply(('claims.0.quote', ['x']))),
                                  ('none', reply(('claims.0.quote', None))), ('row key', {'repairs': [{'path': 'claims.0.quote', 'quote': '', 'x': 1}]}),
                                  ('extra', {'repairs': [], 'x': 1}), ('object', ['x']), ('timeout', TimeoutError('t')), ('call', RuntimeError('c'))):
            with self.subTest(label):
                doc = self.document()
                run, calls = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), correction])
                self.assertEqual((run['status'], len(calls)), ('error', 2), label)
                self.assertEqual(self.saved(doc), ([], []))
                self.assertNotIn('근거 불가', run['detail'])

    def test_inbound_provenance_is_discarded_and_cannot_justify_an_empty_row_or_zero_claims(self):
        fake = {'path': 'claims.0.quote', 'heading': 'h', 'text': 't', 'quote': 'q', 'predicates': ['depends_on'], 'gaps': []}
        run, _ = self.run_it(self.document(), [resp([CLAIM1], FACTS[:1]) | {'excluded_claims': [fake]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(self.report(run)['excluded_claim_count'], 0)
        run, _ = self.run_it(self.document(), [resp([], FACTS[:1]) | {'excluded_claims': [fake]}])
        self.assertEqual(run['status'], 'error', run['detail'])
        empty = resp([CLAIM1], [], {'status': 'not_representable', 'claim_indices': []}) | {'excluded_claims': [fake]}
        run, _ = self.run_it(self.document(), [empty])
        self.assertEqual(run['status'], 'error', run['detail'])

    def test_reply_still_ambiguous_excludes_only_that_row(self):
        doc = self.document(SOURCE + '\n예시: ' + FACTS[0]['quote'])
        run, _ = self.run_it(doc, [resp([CLAIM1, CLAIM2], FACTS[1:]), reply(('claims.0.quote', FACTS[0]['quote']))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([c['heading'] for c in self.saved(doc)[0][0]['claims']], ['나'])

    def test_replied_quote_with_boundary_spaces_is_stored_stripped_and_whitespace_removal_is_never_a_match(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), bad(CLAIM2)]), reply(('claims.0.quote', '  ' + CLAIM1['quote'] + ' \n'), ('claims.1.quote', CLAIM2['quote'].replace(' ', '')))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([c['quote'] for c in self.saved(doc)[0][0]['claims']], [CLAIM1['quote']])
        self.assertEqual(self.report(run)['excluded_claim_count'], 1)

    def test_invalid_reply_strings_exclude_the_row(self):
        for label, quote in (('nul', CLAIM1['quote'] + '\x00'), ('long', 'x' * 8001), ('blank', '  \n')):
            with self.subTest(label):
                doc = self.document()
                run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2]), reply(('claims.0.quote', quote))])
                self.assertEqual(run['status'], 'success', run['detail'])
                self.assertEqual([c['heading'] for c in self.saved(doc)[0][0]['claims']], ['나'])

    def test_accounting_and_preflight_refuse_zero_claims_without_a_record(self):
        value = {'facts': [], 'claims': [], 'coverage': [{'predicate': 'depends_on', 'fact_indices': [], 'claim_indices': [], 'status': 'not_representable', 'reason': 'r'}], 'dictionary_gaps': []}
        with self.assertRaises(ValueError):
            validate_accounting(value, DICTIONARY)
        with self.assertRaisesRegex(ValueError, 'claims'):
            extraction_quote_issues(resp([], FACTS[:1]), SOURCE, DICTIONARY)

    def test_refused_file_is_written_and_its_write_failure_stops_the_chunk(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        folder = Path(self.root) / 'runs' / run['id'] / '1' / 'quote-repair'
        self.assertEqual(json.loads((folder / 'refused.json').read_text(encoding='utf-8'))[0]['path'], 'claims.0.quote')
        self.assertEqual(json.loads((folder / 'changes.json').read_text(encoding='utf-8')), [])
        original = Path.write_text
        def failing(path, *args, **kwargs):
            if path.name == 'refused.json':
                raise OSError('disk full')
            return original(path, *args, **kwargs)
        doc = self.document()
        with patch.object(Path, 'write_text', failing):
            run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'error')
        self.assertFalse(self.checkpoint_path(run).exists())

    def test_repair_call_with_a_refused_row_notifies_failed_caller_validation(self):
        seen = []
        def record(cli, folder, agent, success):
            seen.append((Path(folder).name, success))
        with patch('verinote.call_validation.notify', record):
            run, _ = self.run_it(self.document(), [resp([bad(CLAIM1), CLAIM2]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn(('quote-repair', False), seen)

    def test_saved_page_and_fact_quotes_are_source_substrings_and_exclude_the_refused_text(self):
        doc = self.document(padded(2))
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', '')),
                                   resp([CLAIM1], [bad(FACTS[1]), FACTS[0]]), reply(('facts.0.quote', MISSING))])
        self.assertEqual(run['status'], 'success', run['detail'])
        pages, facts = self.saved(doc)
        for row in pages[0]['claims'] + facts:
            self.assertIn(row['quote'], padded(2))
        self.assertNotIn(MISSING, json.dumps([pages, facts], ensure_ascii=False))

    def test_long_excluded_fields_are_capped_when_written_and_the_run_succeeds(self):
        long_quote = '없' * 5000
        doc = self.document()
        run, _ = self.run_it(doc, [resp([CLAIM1 | {'text': '본' * 1500, 'quote': long_quote}, CLAIM2], [FACTS[0] | {'quote': long_quote}, FACTS[1]]),
                                   reply(('claims.0.quote', '답' * 5000), ('facts.0.quote', '답' * 5000))])
        self.assertEqual(run['status'], 'success', run['detail'])
        chunk = self.report(run)['chunks'][0]
        self.assertEqual(len(chunk['excluded_claims'][0]['text']), MAX_EXCLUDED_TEXT)
        self.assertEqual(len(chunk['excluded_claims'][0]['quote']), MAX_EXCLUDED_QUOTE)
        self.assertEqual(len(chunk['excluded_facts'][0]['quote']), MAX_EXCLUDED_QUOTE)
        refused = json.loads((Path(self.root) / 'runs' / run['id'] / '1' / 'quote-repair' / 'refused.json').read_text(encoding='utf-8'))
        self.assertEqual({len(row['reply']) for row in refused} | {len(row['before']) for row in refused}, {MAX_EXCLUDED_QUOTE})

    def test_detail_of_maximal_exclusions_on_sixteen_chunks_stays_under_the_stored_limit(self):
        claims = [CLAIM1] + [bad(CLAIM2)] * 11
        facts = [bad(FACTS[0])] * 30 + [bad(FACTS[1])] * 30
        rows = [(f'claims.{i}.quote', '') for i in range(1, 11)] + [('claims.11.quote', CLAIM2['quote'])]
        rows += [(f'facts.{i}.quote', '') for i in range(30)] + [(f'facts.{i}.quote', FACTS[1]['quote']) for i in range(30, 60)]
        doc = self.document(padded(16))
        run, calls = self.run_it(doc, [resp(claims, facts), reply(*rows)] * 16)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 32)
        self.assertLess(len(run['detail']), 2000)
        self.assertIn('외 11개 청크 · 번호는 보정을 요청한 응답 기준', run['detail'])
        self.assertIn('외 11개 청크 · 번호는 제외 뒤 기준). 의미가 맞는지', run['detail'])
        self.assertIn(CONSUMED, run['detail'])
        self.assertIn('검증 완료 16/16청크', run['detail'])
        self.assertIn(COMPARE, run['detail'])

    def test_failed_run_detail_states_quote_exclusions_of_validated_chunks_without_the_consumed_clause(self):
        run, _ = self.run_it(self.document(padded(2)), [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', '')), TimeoutError('stop')])
        self.assertEqual(run['status'], 'error')
        self.assertIn('근거 불가로 저장하지 않음: 위키 주장 2개 중 1개', run['detail'])
        self.assertNotIn(CONSUMED, run['detail'])

    def test_resume_detail_says_this_run_and_keeps_the_compare_sentence(self):
        for label, tail, expected in (('no repair this run', [resp([CLAIM2], FACTS[1:])], None),
                                      ('repair applied nothing', [resp([bad(CLAIM1), CLAIM2], FACTS[1:]), reply(('claims.0.quote', ''))], '이번 실행 인용 보정 추가 호출 1회: 반영된 보정 없음.')):
            with self.subTest(label):
                doc = self.document(padded(2))
                first, _ = self.run_it(doc, [resp([bad(CLAIM1)], FACTS[:1]), reply(('claims.0.quote', CLAIM1['quote'])), TimeoutError('stop')])
                self.assertEqual(first['status'], 'error')
                run, _ = self.run_it(doc, tail)
                self.assertEqual(run['status'], 'success', run['detail'])
                self.assertIn(COMPARE, run['detail'])
                self.assertIn('1청크 재사용', run['detail'])
                self.assertNotIn('  ', run['detail'])
                if expected:
                    self.assertIn(expected, run['detail'])
                else:
                    self.assertNotIn('반영된 보정 없음', run['detail'])

    def test_chunk_whose_repair_replaced_nothing_is_not_listed_as_repaired(self):
        doc = self.document(padded(2))
        run, _ = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', CLAIM1['quote'])),
                                   resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('이번 실행 원문 인용 1개 보정 · 추가 호출 2회 (청크 1: 위키 주장 1 인용 · 번호는 제외 뒤 기준).', run['detail'])

    def test_store_refusal_and_rendered_controls_do_not_present_reapproval_as_recovery(self):
        doc = self.document()
        self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))])
        with self.assertRaisesRegex(Conflict, '모든 청크를 새로 호출하고 제외된 항목이 되살아난다는 보장은 없습니다'):
            self.run_it(doc, [])
        app = Path('verinote/web/app.js').read_text(encoding='utf-8')
        function = app[app.index('function extractionControls('):app.index('function renderRelationTypeWarnings(')]
        state = {'items': [{'kind': 'ontology', 'status': 'approved'}], 'can_extract': False}
        script = ("const providerSelect=()=>'',providerHint=()=>'',tokenLimitControls=()=>'',config={};\n"
                  + function + '\nconsole.log(extractionControls(' + json.dumps(state) + '));')
        self.assertIn('그 추출은 모든 청크를 새로 호출하며 제외된 항목이 되살아난다는 보장은 없습니다',
                      subprocess.check_output(['node'], input=script, text=True))

    def test_clean_run_detail_has_no_compare_sentence(self):
        run, _ = self.run_it(self.document(), [resp([CLAIM1], FACTS[:1])])
        self.assertNotIn(COMPARE, run['detail'])

    def test_more_than_five_repairs_in_a_chunk_list_five_locations(self):
        doc = self.document()
        claims = [bad(CLAIM1)] * 7
        run, _ = self.run_it(doc, [resp(claims), reply(*[(f'claims.{i}.quote', CLAIM1['quote']) for i in range(7)])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('위키 주장 5 인용 외 2개', run['detail'])

    def test_saved_quote_paths_shift_only_past_refused_rows_of_the_same_kind(self):
        changes = [{'path': 'claims.3.quote'}, {'path': 'facts.2.quote'}, {'path': 'claims.0.quote'}]
        refused = [{'path': 'claims.1.quote'}, {'path': 'facts.0.quote'}, {'path': 'facts.5.quote'}]
        self.assertEqual(saved_quote_paths(changes, refused), ['claims.2.quote', 'facts.1.quote', 'claims.0.quote'])

    def test_report_with_zero_claims_and_no_record_is_unavailable(self):
        run, _ = self.run_it(self.document(padded(2)), [resp([CLAIM1], FACTS[:1]), TimeoutError('stop')])
        self.assertTrue(self.report(run)['available'])
        path = Path(self.root) / 'runs' / run['id'] / 'extraction-coverage.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['chunks'][0]['claim_count'] = 0
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        self.assertFalse(self.report(run)['available'])

    def test_two_claims_sharing_a_not_representable_row_both_record_its_predicate(self):
        third = {'heading': '다', 'text': '전체', 'quote': SOURCE}
        output = resp([bad(CLAIM1), bad(CLAIM2), third], [], {'status': 'not_representable', 'claim_indices': [0, 1]})
        run, _ = self.run_it(self.document(), [output, reply(('claims.0.quote', ''), ('claims.1.quote', ''))])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([row['predicates'] for row in self.report(run)['chunks'][0]['excluded_claims']], [['depends_on'], ['depends_on']])

    def test_second_repair_request_span_of_a_chunk_is_refused_as_a_duplicate(self):
        output = resp([bad(CLAIM1), CLAIM2], FACTS[:1])
        run, _ = self.run_it(self.document(), [output, reply(('claims.0.quote', CLAIM1['quote']))])
        value = json.loads((Path(self.root) / 'runs' / run['id'] / 'extraction-timing.json').read_text(encoding='utf-8'))
        validate_record(value)
        index = next(i for i, span in enumerate(value['spans']) if span['kind'] == 'repair_request')
        span = value['spans'][index]
        value['spans'].insert(index, span | {'end': span['start'], 'cli_call_id': None})
        with self.assertRaisesRegex(ValueError, 'timing duplicate'):
            validate_record(value)

    def test_lone_surrogate_fails_the_run_at_the_refused_write_or_the_repair_prompt(self):
        for label, expected, outputs in (('reply, refused.json write', 2, [resp([bad(CLAIM1), CLAIM2]), reply(('claims.0.quote', '\ud800x'))]),
                                         ('original quote, repair prompt', 1, [resp([CLAIM1 | {'quote': '\ud800x'}, CLAIM2])])):
            with self.subTest(label):
                doc = self.document()
                run, calls = self.run_it(doc, outputs)
                self.assertEqual((run['status'], len(calls)), ('error', expected), run['detail'])
                self.assertIn('인용 보정 실패', run['detail'])
                self.assertEqual(self.saved(doc), ([], []))

    # Benchmark contract: Service(quote_repair_limit=N).

    def test_ast_of_every_service_construction_shows_limit_two_at_benchmarks_and_none_at_the_server(self):
        found: dict[str, list] = {}
        for path in sorted([*Path('verinote').rglob('*.py'), *Path('scripts').glob('*.py')]):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('Service', 'BaselineService', 'CandidateService'):
                    limit = next((k.value for k in node.keywords if k.arg == 'quote_repair_limit'), None)
                    found.setdefault(path.as_posix(), []).append(limit.value if isinstance(limit, ast.Constant) else limit)
        self.assertEqual(found, {'verinote/extraction_benchmark.py': [2, 2], 'verinote/extraction_ollama_benchmark.py': [2],
                                 'verinote/ollama_fidelity_evaluation.py': [2], 'verinote/server.py': [None]})

    def test_limit_keeps_all_or_nothing_on_a_failed_row(self):
        doc = self.document(padded(3))
        run, calls = self.run_it(doc, [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', ''))], quote_repair_limit=2)
        self.assertEqual((run['status'], len(calls)), ('error', 2))
        self.assertIn('보정 실패', run['detail'])
        self.assertNotIn('근거 불가', run['detail'])

    def test_limit_stops_before_the_third_repair_call(self):
        output, good = resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', CLAIM1['quote']))
        run, calls = self.run_it(self.document(padded(3)), [output, good, output, good, output], quote_repair_limit=2)
        self.assertEqual((run['status'], len(calls)), ('error', 5))
        self.assertIn('벤치마크 인용 보정 2회 한도', run['detail'])

    def test_legacy_contract_stays_strict_after_a_refused_reply(self):
        doc = self.document()
        run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', doc['version'], doc['generation'])
        pin = dict(run['ontology'], extraction_contract=0)
        with self.store.connect(True) as db:
            db.execute('UPDATE runs SET ontology=? WHERE id=?', (json.dumps(pin, ensure_ascii=False), run['id']))
        run = self.store.run(run['id'])
        pending = iter([resp([bad(CLAIM1), CLAIM2]), reply(('claims.0.quote', ''))])
        class Fake:
            def complete(self, agent, model, prompt, folder):
                return copy.deepcopy(next(pending))
        cli = Fake()
        service = Service(self.store, self.root, cli)
        service.slots.acquire()
        service.extract(run, cli)
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'], 'error')
        self.assertEqual(self.saved(doc), ([], []))

    # Checkpoints.

    def test_checkpoint_dictionary_row_has_no_path_and_is_reused_without_a_call(self):
        doc = self.document(padded(2))
        first, _ = self.run_it(doc, [resp([CLAIM1], [OFF_DICTIONARY, FACTS[1]]), TimeoutError('stop')])
        value = json.loads(self.checkpoint_path(first).read_text(encoding='utf-8'))
        self.assertEqual(set(value['outputs'][0]['excluded_facts'][0]), {'code', 'subject', 'predicate', 'object', 'quote'})
        run, calls = self.run_it(doc, [resp([CLAIM2], FACTS[:1])])
        self.assertEqual((run['status'], len(calls)), ('success', 1), run['detail'])
        self.assertEqual(self.report(run)['excluded_fact_count'], 1)

    def tampered(self, first_outputs, edit):
        doc = self.document(padded(2))
        first, _ = self.run_it(doc, first_outputs + [TimeoutError('stop')])
        path = self.checkpoint_path(first)
        value = json.loads(path.read_text(encoding='utf-8'))
        edit(value['outputs'])
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return self.run_it(doc, [resp([CLAIM1], FACTS[:1]), resp([CLAIM2], FACTS[1:])])

    def test_tampered_checkpoint_rows_are_refused_and_the_chunk_is_paid_again(self):
        with_fact = [resp([CLAIM1], [bad(FACTS[0]), FACTS[1]]), reply(('facts.0.quote', ''))]
        with_claim = [resp([bad(CLAIM1)], FACTS[:1]), reply(('claims.0.quote', ''))]
        dictionary = [resp([CLAIM1], [OFF_DICTIONARY, FACTS[1]])]
        cases = (
            ('quote row without path', with_fact, lambda o: o['excluded_facts'][0].pop('path')),
            ('quote row with garbled path', with_fact, lambda o: o['excluded_facts'][0].update(path='nonsense')),
            ('dictionary row with path', dictionary, lambda o: o['excluded_facts'][0].update(path='facts.0.quote')),
            ('zero claims without a record', with_claim, lambda o: o.pop('excluded_claims')),
            ('claim record with garbled path', with_claim, lambda o: o['excluded_claims'][0].update(path='nonsense')),
            ('claim record with unapproved predicate', with_claim, lambda o: o['excluded_claims'][0].update(predicates=['nope'])),
            ('claim record with unhashable predicate', with_claim, lambda o: o['excluded_claims'][0].update(predicates=[['nope']])),
            ('claim record with invented gap code', with_claim, lambda o: o['excluded_claims'][0].update(gaps=[{'code': 'x', 'detail': 'd'}])),
            ('claim record over the text cap', with_claim, lambda o: o['excluded_claims'][0].update(text='x' * (MAX_EXCLUDED_TEXT + 1))),
            ('more claim records than a chunk has claims', with_claim, lambda o: o.update(excluded_claims=o['excluded_claims'] * 13)),
        )
        for label, outputs, edit in cases:
            with self.subTest(label):
                run, calls = self.tampered(outputs, lambda rows, edit=edit: edit(rows[0]))
                self.assertEqual((run['status'], len(calls)), ('success', 2), run['detail'])
                self.assertIn('복구 기록', run['detail'])
        for replacement in (1, None, 'x', ['x']):
            with self.subTest(non_object=replacement):
                run, calls = self.tampered(with_claim, lambda rows, value=replacement: rows.__setitem__(0, value))
                self.assertEqual((run['status'], len(calls)), ('success', 2), run['detail'])
                self.assertIn('복구 기록', run['detail'])

    def test_scheduler_path_excludes_rows_and_makes_three_repair_calls_on_three_chunks(self):
        class Caller:
            extraction_spec = ExtractionSpec('/fake/claude', 'model', 2, 10, 1048576, 'null')
            calls: list = []
            def limited_timeout(self, agent, seconds):
                return self
            def complete(self, agent, model, prompt, folder):
                repair = folder.name == 'quote-repair'
                self.calls.append(repair)
                return copy.deepcopy(reply(('claims.0.quote', '')) if repair else resp([bad(CLAIM1), CLAIM2], FACTS[:1]))
            def close(self):
                pass
        doc = self.document(padded(3))
        caller = Caller()
        service = Service(self.store, self.root, caller, extraction_caller=caller)
        self.addCleanup(service.close)
        run = service.start(doc['id'], 'claude', 'model', expected_version=1, expected_generation=doc.get('generation', 1), extraction_concurrency=2)
        service.pool.shutdown(wait=True)
        run = self.store.run(run['id'])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(sum(caller.calls), 3)
        self.assertEqual(self.report(run)['excluded_claim_count'], 3)
        self.assertTrue(read_extraction_timing(self.root, self.store, run['id'])['available'])

    # Rendered report.

    def test_rendered_quote_rows_lack_dictionary_wording_and_say_they_were_compared(self):
        evil = FACTS[0] | {'subject': '주문', 'quote': '<script>bad</script>'}
        run, _ = self.run_it(self.document(), [resp([CLAIM1], [evil, FACTS[1]]), reply(('facts.0.quote', ''))])
        html = self.render(self.report(run))
        self.assertIn('근거 불가 제외 1개 · 저장 후보 1개', html)
        self.assertIn('제외 후 관계가 남은 종류', html)
        self.assertIn('근거 불가로 제외한 응답 관계 1개', html)
        self.assertIn('원문의 한 위치로 확인되지 않은 모델 문구: &lt;script&gt;', html)
        self.assertIn('(제외한 응답 관계는 이 수에 포함되지 않음)', html)
        self.assertIn('제외로 바뀐 행은 로컬 처리', html)
        self.assertIn('구간 1 · 원문 0~', html)
        self.assertIn(CONSUMED, html)
        for wording in ('승인 사전의 표기', '승인 사전과 일치', '원문과 대조하지 않은', '미검증 모델 인용', '<script>'):
            self.assertNotIn(wording, html)

    def test_rendered_row_label_when_a_quote_exclusion_empties_the_relation(self):
        doc = self.document()
        run, _ = self.run_it(doc, [resp([CLAIM1], [bad(FACTS[0])]), reply(('facts.0.quote', ''))])
        html = self.render(self.report(run))
        self.assertIn('원문 인용을 확인하지 못해 제외 · 응답 관계 1개 (제외 1개)', html)
        self.assertNotIn('사전으로 표현 불가', html)

    def test_rendered_row_label_when_both_causes_empty_the_relation(self):
        run, _ = self.run_it(self.document(), [resp([CLAIM1], [OFF_DICTIONARY, bad(FACTS[1])]), reply(('facts.0.quote', ''))])
        html = self.render(self.report(run))
        self.assertIn('승인 사전 불일치·원문 인용 미확인으로 제외', html)
        self.assertIn('제외 1개는 승인 사전의 표기·타입과', html)
        self.assertIn('근거 불가 제외 1개', html)

    def test_rendered_dictionary_rows_lack_quote_wording(self):
        run, _ = self.run_it(self.document(), [resp([CLAIM1], [OFF_DICTIONARY, FACTS[1]])])
        html = self.render(self.report(run))
        self.assertIn('미검증 모델 인용', html)
        self.assertIn('원문과 대조하지 않은 모델 작성 문구', html)
        for wording in ('근거 불가', '원문의 한 위치', '다시 추출할 수 없고'):
            self.assertNotIn(wording, html)

    def test_rendered_excluded_claim_is_escaped_with_summary_count_gap_note_and_chunk_count(self):
        evil = CLAIM1 | {'heading': '<b>h</b>', 'text': '<i>t</i>'}
        gap = {'code': 'missing_entity', 'detail': '<u>d</u>', 'claim_indices': [0]}
        output = resp([bad(evil), CLAIM2], [], {'status': 'not_representable', 'claim_indices': [0]}, [gap])
        run, _ = self.run_it(self.document(), [output, reply(('claims.0.quote', ''))])
        html = self.render(self.report(run))
        self.assertIn('청크 응답 위키 주장 2개 중 근거 불가 제외 1개', html)
        self.assertIn('제외한 위키 주장에 딸린 사전 제약 1개는 이 수에 포함되지 않음', html)
        self.assertIn('자 · 근거 불가 제외 1개</summary>', html)
        self.assertIn('근거 불가로 제외한 위키 주장 1개', html)
        self.assertIn('위키 주장 1 · &lt;b&gt;h&lt;/b&gt; · &lt;i&gt;t&lt;/i&gt;', html)
        self.assertIn('개체 부족 · &lt;u&gt;d&lt;/u&gt;', html)
        self.assertIn('이 주장 제외로 표현 제약 근거가 남지 않은 관계: depends_on', html)
        self.assertIn('관계가 나온 종류', html)
        for wording in ('<b>', '<i>', '<u>', '제외한 응답 관계', '승인 사전과 일치'):
            self.assertNotIn(wording, html)

    def test_rendered_failed_run_with_exclusions_omits_the_consumed_clause(self):
        run, _ = self.run_it(self.document(padded(2)), [resp([bad(CLAIM1), CLAIM2], FACTS[:1]), reply(('claims.0.quote', '')), TimeoutError('stop')])
        html = self.render(self.report(run))
        self.assertIn('근거 불가 제외는 보정 뒤에도', html)
        self.assertNotIn('다시 추출할 수 없고', html)


if __name__ == '__main__':
    unittest.main()
