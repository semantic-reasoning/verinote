"""A row-count mismatch found first is reported as counts; the response is still not kept as a candidate (#807)."""
import json
import unittest
from unittest.mock import patch

import test_extraction_checkpoint as checkpoint
import test_extraction_scheduler as scheduler
import test_workflow as workflow
from test_workflow import extraction_response

from verinote.extraction_coverage import validate_accounting
from verinote.ollama_fidelity import diagnose
from verinote.service import Service

IDS = [f'rel{i}' for i in range(7)]
DICTIONARY = {'relations': [{'id': i} for i in IDS]}


def value(rows):
    return {'facts': [], 'claims': [], 'dictionary_gaps': [],
            'coverage': [{'predicate': p, 'fact_indices': [], 'claim_indices': [], 'status': 'no_evidence', 'reason': 'r'} for p in rows]}


class RowCountReport(unittest.TestCase):
    def message(self, rows):
        with self.assertRaises(ValueError) as caught:
            validate_accounting(value(rows), DICTIONARY)
        return str(caught.exception)

    def test_retained_qwen25_response_replayed_through_the_production_gate_reports_counts(self):
        small, _ = diagnose()['cases'].values()
        error = small['production_error']
        self.assertIn('승인 관계 3개, 응답 행 1개', error)
        self.assertIn('행이 없는 관계: archives, mirrors', error)

    def test_count_gate_reports_missing_ids_and_says_not_kept(self):
        text = self.message(IDS[:5])
        self.assertIn('승인 관계 7개, 응답 행 5개, 행이 없는 관계: rel5, rel6)', text)
        self.assertIn('저장하지 않았습니다', text)
        self.assertIn('재사용할 복구 기록이 없는 청크를 새 모델 호출(비용이 들 수 있음)로 요청', text)
        self.assertIn('같은 결과가 나올 수 있습니다', text)
        self.assertNotIn('모든 청크', text)
        self.assertNotIn('이 청크부터', text)

    def test_non_dict_row_is_counted_as_unapproved(self):
        rows = value(IDS)['coverage'] + ['junk']
        broken = value([]) | {'coverage': rows}
        with self.assertRaises(ValueError) as caught:
            validate_accounting(broken, DICTIONARY)
        self.assertIn('응답 행 8개, 승인되지 않은 행 1개)', str(caught.exception))

    def test_extra_and_duplicate_rows_are_counted_at_the_count_gate(self):
        self.assertIn('응답 행 8개, 승인되지 않은 행 1개)', self.message(IDS + ['other']))
        self.assertIn('응답 행 8개, 중복 관계 1개)', self.message(IDS + ['rel0']))

    def test_equal_count_gate_reports_the_same_counts(self):
        text = self.message(IDS[:5] + ['other', 'rel0'])
        self.assertIn('응답 행 7개, 승인되지 않은 행 1개, 중복 관계 1개, 행이 없는 관계: rel5, rel6)', text)
        self.assertIn('누락·중복', text)

    def test_row_field_error_found_first_reports_only_that_error_and_no_counts(self):
        rows = value(IDS)['coverage']
        rows[0] = dict(rows[0], status='bad')
        rows[1] = dict(rows[1], predicate='unknown')
        for first in (rows, ['junk'] + rows[1:]):
            with self.subTest(first=first[0] if isinstance(first[0], str) else 'bad status'):
                response = value(IDS)
                response['coverage'] = first
                with self.assertRaises(ValueError) as caught:
                    validate_accounting(response, DICTIONARY)
                text = str(caught.exception)
                self.assertTrue('status 오류' in text or 'coverage 필드 오류' in text, text)
                self.assertNotIn('승인 관계 7개', text)
                self.assertNotIn('후보로 저장하지 않았습니다', text)

    def test_model_written_predicate_is_never_echoed(self):
        text = self.message(IDS[:6] + ['secret-' + 'x' * 5000])
        self.assertNotIn('secret-', text)
        self.assertLess(len(text), 600)

    def test_only_five_missing_ids_are_listed(self):
        text = self.message([])
        self.assertIn('rel0, rel1, rel2, rel3, rel4 외 2개', text)
        self.assertNotIn('rel5', text)
        self.assertNotIn('rel6', text)

    def test_non_list_coverage_reports_counts_without_another_error(self):
        broken = value(IDS)
        broken['coverage'] = {'predicate': 'rel0'}
        with self.assertRaises(ValueError) as caught:
            validate_accounting(broken, DICTIONARY)
        self.assertIn('승인 관계 7개, coverage 배열 없음', str(caught.exception))

    def test_complete_rows_still_pass(self):
        self.assertEqual(len(validate_accounting(value(IDS), DICTIONARY)['coverage']), 7)


class RowCountRetry(unittest.TestCase):
    # Borrowed, not inherited: inheriting would re-run every ExtractionRecovery test here.
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    document = checkpoint.ExtractionRecovery.document
    execute = checkpoint.ExtractionRecovery.execute
    checkpoint = checkpoint.ExtractionRecovery.checkpoint

    @staticmethod
    def short():
        value = extraction_response()
        value['coverage'] = []
        return value

    def offsets(self, calls):
        return [c['payload']['offset'] for c in calls]

    def test_service_reports_counts_saves_nothing_and_retry_resumes_from_the_failed_chunk(self):
        doc = self.document(3)
        first, calls = self.execute(doc, [self.short()])
        self.assertEqual(first['status'], 'error', first['detail'])
        self.assertIn('승인 관계 1개, 응답 행 0개, 행이 없는 관계: depends_on', first['detail'])
        self.assertIn('저장하지 않았습니다', first['detail'])
        self.assertEqual(self.offsets(calls), [0])
        self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])
        saved = self.checkpoint(first)
        self.assertFalse(saved.exists() and json.loads(saved.read_text())['outputs'])
        # Failure before the last chunk: the retry re-calls that chunk and every later one.
        final, calls = self.execute(doc, [extraction_response()] * 3)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(self.offsets(calls), [0, 8000, 16000])

    def test_retry_after_a_later_failure_skips_validated_chunks_of_the_same_model(self):
        doc = self.document(3)
        first, _ = self.execute(doc, [extraction_response(), self.short()])
        self.assertEqual(first['status'], 'error', first['detail'])
        final, calls = self.execute(doc, [extraction_response()] * 2)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(self.offsets(calls), [8000, 16000])

    def test_other_model_reuses_only_chunks_it_has_a_record_for_in_both_directions(self):
        doc = self.document(3)
        x_run, _ = self.execute(doc, [extraction_response(), extraction_response(), self.short()], model='x')
        self.assertEqual(x_run['status'], 'error', x_run['detail'])
        # y has no record: it starts at chunk 1 although x validated two chunks.
        y_run, calls = self.execute(doc, [extraction_response(), self.short()], model='y')
        self.assertEqual(y_run['status'], 'error', y_run['detail'])
        self.assertEqual(self.offsets(calls), [0, 8000])
        self.assertIn('재사용할 복구 기록이 없는 청크', y_run['detail'])
        # Back to x: its own two chunks are reused, only chunk 3 is called.
        x_final, calls = self.execute(doc, [extraction_response()], model='x')
        self.assertEqual(x_final['status'], 'success', x_final['detail'])
        self.assertEqual(self.offsets(calls), [16000])
        self.assertIn('2청크 재사용', x_final['detail'])

    def test_retry_re_calls_every_chunk_after_the_dictionary_is_re_approved(self):
        doc = self.document(3)
        first, _ = self.execute(doc, [extraction_response(), self.short()])
        self.assertEqual(first['status'], 'error', first['detail'])
        dictionary = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.store.review(dictionary['id'], dictionary['revision'], 'revise', 'R', 'change', workflow.DICTIONARY)
        self.store.review(dictionary['id'], dictionary['revision'] + 1, 'approved', 'R', 'approve')
        final, calls = self.execute(doc, [extraction_response()] * 3)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(self.offsets(calls), [0, 8000, 16000])

    def test_retry_re_calls_every_chunk_when_the_checkpoint_write_failed(self):
        doc = self.document(3)
        with patch('verinote.extraction_checkpoint.atomic_json', side_effect=OSError('disk')):
            first, _ = self.execute(doc, [extraction_response(), self.short()])
        self.assertEqual(first['status'], 'error', first['detail'])
        final, calls = self.execute(doc, [extraction_response()] * 3)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(self.offsets(calls), [0, 8000, 16000])

    def test_parallel_retry_resumes_from_the_failed_head_chunk(self):
        doc = self.document(3)

        def parallel(fn):
            caller = scheduler.Caller(fn)
            service = Service(self.store, self.root, caller, extraction_caller=caller)
            self.addCleanup(service.close)
            run = service.start(doc['id'], 'claude', 'model', expected_version=doc['version'],
                                expected_generation=doc['generation'], extraction_concurrency=2)
            service.pool.shutdown(wait=True)
            return self.store.run(run['id']), caller

        first, _ = parallel(lambda index, _: self.short() if index == 3 else extraction_response())
        self.assertEqual(first['status'], 'error', first['detail'])
        self.assertIn('승인 관계 1개, 응답 행 0개', first['detail'])
        final, retry = parallel(lambda index, _: extraction_response())
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(sorted({index for index, _ in retry.calls}), [3])


if __name__ == '__main__':
    unittest.main()
