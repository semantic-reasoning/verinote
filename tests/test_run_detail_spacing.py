"""Synthetic calls only: a successful extraction run's detail joins its sentences with one space (#877)."""
import contextlib
import copy
import unittest
from unittest.mock import patch

import test_workflow as workflow
from test_quote_repair import broken, repair
from test_workflow import FACTS, SOURCE, approved_dictionary, extraction_response

from verinote.service import Service

# Expected sentences are written out here, not imported from the code under test.
PREFIX = '관계 후보 2개 · 관계별 점검은 실행 기록에서 확인하세요. 새 후보는 별도 승인 전까지 그래프에 포함되지 않습니다.'
LOSS = '손실 점검 기록에서 보충·미해결·미점검 구간을 확인하세요. 처리 완료는 의미적 완전성을 보증하지 않습니다.'
REPAIRED = ('이번 실행 원문 인용 1개 보정 · 추가 호출 1회 (청크 1: 관계 1 인용 · 번호는 제외 뒤 기준). '
            '의미가 맞는지 원문과 대조한 뒤 승인하세요.')
REPORT_FAILED = '관계별 추출 점검 기록 저장 실패: 보고서를 사용할 수 없습니다.'
EXCLUDED = '응답 관계 3개 중 1개는 승인 사전의 표기·타입과 일치하지 않아 저장하지 않음 · 되살리려면 사전 수정·재승인 후 재추출'
ONE_CALL = '검증 완료 1/1청크 · 새 호출 시도 1회'
TWO_CALLS = '검증 완료 1/1청크 · 새 호출 시도 2회'
UNKNOWN_ENTITY = dict(subject='주문', predicate='depends_on', object='미지', quote='주문은 결제에 의존한다.')


class SuccessDetailSpacing(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def assert_detail(self, contract, outputs, expected, report_saved=True):
        with patch('verinote.store.EXTRACTION_CONTRACT_VERSION', contract):
            doc = self.store.register('공백 테스트', 'synthetic.md', SOURCE.encode(), SOURCE)
            approved_dictionary(self.store, doc['id'])
            pending = iter(outputs)

            class Fake:
                def complete(self, agent, model, prompt, folder):
                    return copy.deepcopy(next(pending))
            service = Service(self.store, self.root, Fake())
            failing_report = (contextlib.nullcontext() if report_saved
                              else patch('verinote.service.write_report', return_value=False))
            with failing_report:
                run = service.start(doc['id'], 'copilot', 'chosen-model', expected_version=1, expected_generation=1)
                service.pool.shutdown(wait=True)
        run = self.store.run(run['id'])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(run['detail'], expected)
        self.assertNotIn('  ', run['detail'])

    def test_contract_1_without_repair(self):
        self.assert_detail(1, [extraction_response()], f'{PREFIX} {ONE_CALL}')

    def test_contract_2_without_repair(self):
        self.assert_detail(2, [extraction_response()], f'{PREFIX} {ONE_CALL}')

    def test_contract_3_without_repair(self):
        self.assert_detail(3, [extraction_response()], f'{PREFIX} {LOSS} {ONE_CALL}')

    def test_contract_2_with_quote_repair(self):
        self.assert_detail(2, [broken(), repair()], f'{PREFIX} {REPAIRED} {TWO_CALLS}')

    def test_contract_3_with_quote_repair(self):
        self.assert_detail(3, [broken(), repair()], f'{PREFIX} {REPAIRED} {LOSS} {TWO_CALLS}')

    def test_contract_2_with_off_dictionary_exclusion(self):
        self.assert_detail(2, [extraction_response(FACTS + [UNKNOWN_ENTITY])], f'{EXCLUDED} · {PREFIX} {ONE_CALL}')

    def test_contract_3_with_off_dictionary_exclusion(self):
        self.assert_detail(3, [extraction_response(FACTS + [UNKNOWN_ENTITY])],
                           f'{EXCLUDED} · {PREFIX} {LOSS} {ONE_CALL}')

    def test_contract_3_with_loss_recovery_call(self):
        self.assert_detail(3, [extraction_response([]), {'facts': FACTS}], f'{PREFIX} {LOSS} {TWO_CALLS}')

    def test_contract_2_with_report_save_failure(self):
        self.assert_detail(2, [extraction_response()], f'{PREFIX} {REPORT_FAILED} {ONE_CALL}', report_saved=False)

    def test_contract_3_with_report_save_failure(self):
        self.assert_detail(3, [extraction_response()], f'{PREFIX} {LOSS} {REPORT_FAILED} {ONE_CALL}',
                           report_saved=False)


if __name__ == '__main__':
    unittest.main()
