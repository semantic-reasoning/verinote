"""A quote that differs from its chunk only in the width of whitespace runs, settled without a model call."""
import copy
import json
import sys
import unittest
from itertools import product
from unittest.mock import patch

import test_workflow as workflow
from test_extraction_scheduler import Caller
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.agents import CLI
from verinote.extraction_coverage import (
    locate_whitespace_variants,
    whitespace_variant_span,
)
from verinote.extraction_options import CHUNK_THRESHOLD_POLICY
from verinote.ontology import extraction_quote_issues
from verinote.service import Service

COMPARE = '의미가 맞는지 원문과 대조한 뒤 승인하세요'
SETTLED = '원문 구간으로 확정'
COUNTED = '공백만 다른 인용'
SUPPORT = '확정 구간에는 모델이 인용한 내용만 있으므로 그 항목을 뒷받침하는지 확인하세요'
WRAPPED = '가나 \n다라.'
TEXT = SOURCE + '\n' + WRAPPED
CLAIM = {'heading': '가', 'text': '주문', 'quote': '가나 다라.'}
OTHER = {'heading': '나', 'text': '결제', 'quote': '결제는 원장에 의존한다.'}
WRONG = {'heading': '다', 'text': '없음', 'quote': '원문에 없는 문구'}


def response(claims, facts=()):
    return extraction_response(list(facts)) | {'claims': copy.deepcopy(list(claims))}


class Span(unittest.TestCase):
    def test_run_widths_may_differ_and_the_source_bytes_come_back(self):
        for source, quote in (('가나 \n다라', '가나 다라'), ('가나\n다라', '가나 다라'), ('가나\t  다라', '가나 다라'),
                              ('가나\r\n다라', '가나 다라'), ('가나\r다라', '가나 다라'), ('가나 다라', '가나\n다라'), ('가나\n\t 다라', '가나 \n\n 다라'),
                              ('앞 가나 \n다라 뒤', '  가나 다라\n')):
            with self.subTest(source=source, quote=quote):
                found = whitespace_variant_span(quote, source)
                self.assertEqual(found, source.strip(' 앞뒤'))

    def test_a_run_is_never_removed_or_added(self):
        for source, quote in (('아버지가 방에', '아버지 가방에'), ('아버지 가방에', '아버지가 방에'), ('가나다', '가 나다'),
                              ('가 나다', '가나다'), ('가나다 라', '가나 다 라')):
            with self.subTest(source=source, quote=quote):
                self.assertIsNone(whitespace_variant_span(quote, source))

    def test_a_source_run_with_two_line_breaks_refuses(self):
        for run in ('\n\n', '\n \n', '\n\t\n', '\r\n\r\n', '\r\r', '\n\r', '\n\n\n'):
            with self.subTest(run=run):
                self.assertIsNone(whitespace_variant_span('가나 다라', f'가나{run}다라'))

    def test_only_space_tab_and_line_breaks_are_runs(self):
        for mark in '\f\v\x1c\x1d\x1e\x1f\x85\u2028\u2029\xa0\u3000':
            with self.subTest(mark=mark):
                self.assertIsNone(whitespace_variant_span('가나 다라', f'가나{mark}다라'))
                self.assertIsNone(whitespace_variant_span('가나 다라', f'가나 {mark} 다라'))

    def test_other_whitespace_in_the_response_is_not_a_run_either(self):
        for mark in '\f\v\x1c\x85\u2028\u2029\xa0\u3000':
            with self.subTest(mark=mark):
                self.assertIsNone(whitespace_variant_span(f'가나{mark}다라', '가나 다라'))

    def test_the_input_response_is_not_mutated(self):
        value = {'claims': [{'quote': '가나 다라'}], 'facts': []}
        issues = [{'path': 'claims.0.quote', 'item': value['claims'][0], 'reason': 'missing'}]
        result, changes, left = locate_whitespace_variants(value, '가나\n다라', issues)
        self.assertEqual(value['claims'][0]['quote'], '가나 다라')
        self.assertEqual(result['claims'][0]['quote'], '가나\n다라')
        self.assertEqual((len(changes), left), (1, []))

    def test_two_positions_refuse_including_nested_ones(self):
        self.assertIsNone(whitespace_variant_span('가나 다라', '가나\n다라 또 가나  다라'))
        self.assertIsNone(whitespace_variant_span('가\n가', '가 가 가'))

    def test_an_ambiguous_exact_quote_is_never_located(self):
        self.assertIsNone(whitespace_variant_span('가 나', '가 나 그리고 가 나'))
        self.assertIsNone(whitespace_variant_span('가\n\n나', '가\n\n나 그리고 가\n\n나'))

    def test_a_match_crossing_two_line_breaks_counts_as_a_position_in_either_order(self):
        self.assertIsNone(whitespace_variant_span('가 나', '가\n\n나 또 가\n나'))
        self.assertIsNone(whitespace_variant_span('가 나', '가\n나 또 가\n\n나'))

    def test_a_crossing_at_any_run_of_a_longer_quote_refuses_and_a_later_failed_comparison_does_not(self):
        self.assertIsNone(whitespace_variant_span('가 나 다', '가\n\n나 다'))
        self.assertIsNone(whitespace_variant_span('가 나 다', '가 나\n\n다'))
        self.assertIsNone(whitespace_variant_span('가 나 다 라', '가 나\n\n다 라'))
        self.assertEqual(whitespace_variant_span('가\n다', '가 다 또 가\n\n나'), '가 다')

    def test_an_exact_ambiguous_quote_with_a_one_newline_sibling_is_left_for_repair(self):
        source = '가나\n\n다라 첫째 가나\n\n다라 둘째 가나\n다라 셋째'
        value = {'claims': [{'quote': '가나\n\n다라'}], 'facts': []}
        issues = [{'path': 'claims.0.quote', 'item': value['claims'][0], 'reason': 'ambiguous'}]
        self.assertIsNone(whitespace_variant_span('가나\n\n다라', source))
        result, changes, left = locate_whitespace_variants(value, source, issues)
        self.assertEqual((result, changes, left), (value, [], issues))

    def test_enumerated_sources_against_a_passage_count_oracle(self):
        """Every source of up to three passages, each `가<run>나`, with each run as the quote's own run."""
        runs = (' ', '\n', '\t\n ', '\r\n', '\n\n', '\r\n\r\n', '\n \n')
        crossing = runs[4:]
        ambiguous_with_sibling = 0
        for quote_run in runs:
            quote = f'가{quote_run}나'
            for size in (0, 1, 2, 3):
                for picked in product(runs, repeat=size):
                    passages = [f'가{run}나' for run in picked]
                    source = '또 ' + ' 또 '.join(passages)
                    with self.subTest(quote=quote, source=source):
                        found = whitespace_variant_span(quote, source)
                        if len(picked) == 1 and picked[0] not in crossing:
                            self.assertEqual(found, passages[0])
                        else:
                            self.assertIsNone(found)
                        # A quote with two exact occurrences is what the v2 check calls ambiguous.
                        if source.count(quote) >= 2:
                            self.assertIsNone(found)
                            ambiguous_with_sibling += any(run != quote_run and run not in crossing for run in picked)
        self.assertGreater(ambiguous_with_sibling, 0)

    def test_every_returned_span_is_exact_and_unique_under_the_v2_check(self):
        for source, quote in (('가 가', '가\n가'), ('가 가 나', '가\n가 나'), ('가나 \n다라', '가나 다라')):
            span = whitespace_variant_span(quote, source)
            first = source.find(span)
            self.assertGreaterEqual(first, 0)
            self.assertLess(source.find(span, first + 1), 0)


class Located(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, source=TEXT):
        doc = self.store.register('t', 's.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def run_it(self, doc, outputs, **options):
        calls, prompts, pending = [], [], iter(outputs)
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append(str(folder))
                prompts.append(prompt)
                result = next(pending)
                if isinstance(result, Exception):
                    raise result
                return copy.deepcopy(result)
        service = Service(self.store, self.root, Fake(), **options)
        run = service.start(doc['id'], 'copilot', 'm', expected_version=1, expected_generation=doc.get('generation', 1))
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), calls, prompts

    def saved(self, doc):
        items = [i for i in self.store.document(doc['id'])['items'] if i['kind'] != 'ontology']
        return [i['payload'] for i in items if i['kind'] == 'page'], [i['payload'] for i in items if i['kind'] == 'fact']

    def record(self, run, chunk=1):
        return json.loads((self.root / 'runs' / run['id'] / str(chunk) / 'quote-located.json').read_text(encoding='utf-8'))

    def test_the_stored_quote_is_the_source_passage_with_no_extra_call_and_the_rest_unchanged(self):
        doc = self.document()
        run, calls, _ = self.run_it(doc, [response([CLAIM, OTHER])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 1)
        pages, _ = self.saved(doc)
        self.assertEqual(pages[0]['claims'][0], CLAIM | {'quote': WRAPPED})
        self.assertEqual(pages[0]['claims'][1], OTHER)
        self.assertEqual(self.record(run), [{'path': 'claims.0.quote', 'before': CLAIM['quote'], 'after': WRAPPED}])

    def test_a_located_only_chunk_still_tells_the_reader_to_compare_and_is_not_counted_as_a_repair(self):
        run, _, _ = self.run_it(self.document(), [response([CLAIM, OTHER])])
        self.assertIn('공백만 다른 인용 1개', run['detail'])
        self.assertIn('(청크 1: 위키 주장 1 인용 · 번호는 제외 뒤 기준)', run['detail'])
        self.assertIn(SUPPORT, run['detail'])
        self.assertIn(COMPARE, run['detail'])
        self.assertNotIn('인용 1개 보정', run['detail'])
        self.assertNotIn('인용 보정 추가 호출', run['detail'])

    def test_two_located_rows_in_one_chunk_are_each_recorded_counted_and_named(self):
        second = CLAIM | {'quote': '마바 사아.'}
        doc = self.document(TEXT + '\n마바\n사아.')
        run, calls, _ = self.run_it(doc, [response([CLAIM, second, OTHER])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.record(run), [{'path': 'claims.0.quote', 'before': CLAIM['quote'], 'after': WRAPPED},
                                            {'path': 'claims.1.quote', 'before': '마바 사아.', 'after': '마바\n사아.'}])
        self.assertIn('공백만 다른 인용 2개', run['detail'])
        self.assertIn('청크 1: 위키 주장 1 인용, 위키 주장 2 인용 ·', run['detail'])

    def test_the_label_list_caps_at_five_rows_and_five_chunks_while_the_count_keeps_all(self):
        rows = [f'행{i} 끝.' for i in range(6)]
        doc = self.document(SOURCE + '\n' + '\n'.join(row.replace(' ', '\n') for row in rows))
        run, _, _ = self.run_it(doc, [response([CLAIM | {'quote': row} for row in rows])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('공백만 다른 인용 6개', run['detail'])
        self.assertIn('위키 주장 5 인용 외 1개 ·', run['detail'])
        rows = [f'구간{i} 끝.' for i in range(6)]
        doc = self.document(''.join((SOURCE * (i == 0) + row.replace(' ', '\n')).ljust(8000) for i, row in enumerate(rows)))
        run, _, _ = self.run_it(doc, [response([CLAIM | {'quote': row}]) for row in rows])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('공백만 다른 인용 6개', run['detail'])
        self.assertIn('청크 5: 위키 주장 1 인용 외 1개 청크 ·', run['detail'])

    def test_the_label_is_the_saved_number_after_a_refused_row_while_the_record_keeps_the_response_number(self):
        doc = self.document()
        run, _, _ = self.run_it(doc, [response([WRONG, CLAIM, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': ''}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], WRAPPED)
        self.assertEqual([row['path'] for row in self.record(run)], ['claims.1.quote'])
        self.assertIn('(청크 1: 위키 주장 1 인용 · 번호는 제외 뒤 기준)', run['detail'])  # The exclusion notice has its own basis.

    def test_a_chunk_with_issues_and_none_located_writes_no_record(self):
        run, _, _ = self.run_it(self.document(), [response([WRONG, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': OTHER['quote']}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertFalse((self.root / 'runs' / run['id'] / '1' / 'quote-located.json').exists())
        self.assertNotIn(COUNTED, run['detail'])

    def test_a_located_row_then_a_failed_repair_shows_no_count_and_saves_no_chunk_though_the_record_stays(self):
        for reply in (ValueError('호출 실패'), {'repairs': []}):
            with self.subTest(reply=reply):
                doc = self.store.register(f't{reply!s:.2}', 's.md', TEXT.encode(), TEXT)
                approved_dictionary(self.store, doc['id'])
                run, _, _ = self.run_it(doc, [response([CLAIM, WRONG, OTHER]), reply])
                self.assertEqual(run['status'], 'error')
                self.assertIn('인용 보정 실패', run['detail'])
                self.assertNotIn(COUNTED, run['detail'])
                self.assertEqual(self.saved(doc), ([], []))
                self.assertFalse((self.root / 'runs' / run['id'] / 'extraction-checkpoint.json').exists())
                self.assertEqual(len(self.record(run)), 1)

    def test_a_located_only_chunk_whose_checkpoint_accept_raises_shows_no_count(self):
        with patch('verinote.extraction_checkpoint.ExtractionCheckpoint.accept', side_effect=ValueError('거부')):
            run, _, _ = self.run_it(self.document(), [response([CLAIM, OTHER])])
        self.assertEqual(run['status'], 'error')
        self.assertIn('거부', run['detail'])
        self.assertNotIn(COUNTED, run['detail'])

    def test_a_located_only_chunk_through_the_scheduler_makes_one_call_and_counts_the_row(self):
        doc = self.document()
        created = []
        def factory(spec):
            caller = Caller(lambda index, repair: response([CLAIM, OTHER]), spec.capacity)
            caller.extraction_spec = spec
            created.append(caller)
            return caller
        service = Service(self.store, self.root, CLI(timeout=7, persistent=False), extraction_caller_factory=factory)
        self.addCleanup(service.close)
        with patch('verinote.extraction_transport.executable', return_value=sys.executable):
            run = service.start(doc['id'], 'claude', 'model', expected_version=doc['version'], expected_generation=doc['generation'],
                                extraction_policy=CHUNK_THRESHOLD_POLICY)
        service.pool.shutdown(wait=True)
        run = self.store.run(run['id'])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(created[0].calls, [(1, False)])
        self.assertIn('공백만 다른 인용 1개', run['detail'])
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], WRAPPED)

    def test_an_exact_run_says_neither(self):
        run, _, _ = self.run_it(self.document(SOURCE), [response([OTHER])])
        self.assertNotIn(SETTLED, run['detail'])
        self.assertNotIn(COMPARE, run['detail'])

    def test_only_the_unsettled_row_goes_to_repair(self):
        doc = self.document()
        run, calls, prompts = self.run_it(doc, [response([CLAIM, WRONG, OTHER]), {'repairs': [{'path': 'claims.1.quote', 'quote': OTHER['quote']}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        body = prompts[1].split('입력(JSON):')[-1]
        self.assertIn('claims.1.quote', body)
        self.assertNotIn('claims.0.quote', body)
        self.assertIn('공백만 다른 인용 1개', run['detail'])
        self.assertIn('원문 인용 1개 보정', run['detail'])
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], WRAPPED)

    def test_paths_are_numbered_after_dictionary_exclusion(self):
        off = FACTS[0] | {'subject': '승인 사전에 없는 개체', 'quote': '원문에 없는 문구'}
        run, _, _ = self.run_it(self.document(), [response([OTHER], [off, FACTS[0] | {'quote': CLAIM['quote']}])])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([row['path'] for row in self.record(run)], ['facts.0.quote'])

    def test_an_ambiguous_exact_quote_still_goes_to_repair(self):
        doc = self.document(SOURCE + ' ' + FACTS[0]['quote'])
        run, calls, _ = self.run_it(doc, [response([CLAIM | {'quote': FACTS[0]['quote']}, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': SOURCE}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SETTLED, run['detail'])

    def test_an_exact_ambiguous_quote_with_a_one_newline_sibling_makes_two_calls_and_stores_the_repair(self):
        passage = '가나\n\n다라 첫째'
        doc = self.document(f'{SOURCE}\n{passage}\n가나\n\n다라 둘째\n가나\n다라 셋째')
        run, calls, _ = self.run_it(doc, [response([CLAIM | {'quote': '가나\n\n다라'}, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': passage}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], passage)
        self.assertNotIn(COUNTED, run['detail'])
        self.assertFalse((self.root / 'runs' / run['id'] / '1' / 'quote-located.json').exists())

    def test_a_missing_quote_with_one_crossing_and_one_plain_position_makes_two_calls_and_stores_the_repair(self):
        passage = '가나\n\n다라 첫째'
        doc = self.document(f'{SOURCE}\n{passage}\n가나\n다라 둘째')
        run, calls, _ = self.run_it(doc, [response([CLAIM | {'quote': '가나 다라'}, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': passage}]}])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], passage)
        self.assertNotIn(COUNTED, run['detail'])

    def test_an_exact_unique_quote_with_a_variant_elsewhere_is_left_alone(self):
        source = SOURCE + ' 가나 다라 그리고 가나\n다라'
        doc = self.document(source)
        claim = CLAIM | {'quote': '가나\n다라'}
        run, calls, _ = self.run_it(doc, [response([claim, OTHER])])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.saved(doc)[0][0]['claims'][0]['quote'], '가나\n다라')
        self.assertNotIn(SETTLED, run['detail'])

    def test_a_benchmark_limit_keeps_its_repair_and_all_or_nothing(self):
        doc = self.document()
        run, calls, _ = self.run_it(doc, [response([CLAIM, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': WRAPPED}]}], quote_repair_limit=2)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SETTLED, run['detail'])

    def test_v0_and_v1_pins_keep_their_repair(self):
        for version in (0, 1):
            with self.subTest(version=version), patch('verinote.store.EXTRACTION_CONTRACT_VERSION', version):
                doc = self.store.register(f't{version}', f's{version}.md', TEXT.encode(), TEXT)
                approved_dictionary(self.store, doc['id'])
                run, calls, _ = self.run_it(doc, [response([CLAIM, OTHER]), {'repairs': [{'path': 'claims.0.quote', 'quote': WRAPPED}]}])
                self.assertEqual(run['status'], 'success', run['detail'])
                self.assertEqual(len(calls), 2)
                self.assertNotIn(SETTLED, run['detail'])

    def test_the_failed_call_verdict_stays_for_a_located_only_chunk(self):
        seen = []
        with patch('verinote.call_validation.notify', lambda cli, folder, agent, success: seen.append(success)):
            self.run_it(self.document(), [response([CLAIM, OTHER])])
        self.assertEqual(seen, [False])

    def test_a_record_that_cannot_be_saved_does_not_confirm_the_chunk(self):
        doc = self.document()
        with patch('verinote.extraction_coverage.atomic_json', side_effect=OSError('disk')):
            run, _, _ = self.run_it(doc, [response([CLAIM, OTHER])])
        self.assertEqual(run['status'], 'error')
        self.assertIn('인용 위치 확정 기록 저장 실패', run['detail'])
        self.assertNotIn(COUNTED, run['detail'])
        self.assertEqual(self.saved(doc), ([], []))
        self.assertFalse((self.root / 'runs' / run['id'] / 'extraction-checkpoint.json').exists())

    def test_nothing_is_located_before_the_coverage_audit_is_saved(self):
        doc = self.document()
        value = response([OTHER], [FACTS[0] | {'quote': CLAIM['quote']}])
        value['coverage'][0]['fact_indices'] = []
        with patch('verinote.service.write_repair_audit', side_effect=ValueError('audit')):
            run, _, _ = self.run_it(doc, [value])
        self.assertEqual(run['status'], 'error')
        self.assertFalse((self.root / 'runs' / run['id'] / '1' / 'quote-located.json').exists())

    def test_a_located_chunk_is_reused_on_resume_without_a_call_and_reloads_clean(self):
        source = TEXT.ljust(8000) + OTHER['quote']
        doc = self.document(source)
        run, calls, _ = self.run_it(doc, [response([CLAIM, OTHER]), ValueError('중단')])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(calls), 2)
        self.assertIn('공백만 다른 인용 1개', run['detail'])
        self.assertIn('청크 1: 위키 주장 1 인용', run['detail'])
        self.assertIn(SUPPORT, run['detail'])
        saved = json.loads((self.root / 'runs' / run['id'] / 'extraction-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['outputs'][0]['claims'][0]['quote'], WRAPPED)
        self.assertEqual(extraction_quote_issues(saved['outputs'][0], source[:8000], DICTIONARY, require_coverage=True, contract_version=2), [])
        again, calls, _ = self.run_it(doc, [response([OTHER])])
        self.assertEqual(again['status'], 'success', again['detail'])
        self.assertEqual(len(calls), 1)
        self.assertIn('1청크 재사용', again['detail'])
        self.assertIn(COMPARE, again['detail'])
        self.assertNotIn(SETTLED, again['detail'])


if __name__ == '__main__':
    unittest.main()
