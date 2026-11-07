"""#806: the stop message of a response mixing off-dictionary facts with a chunk-stopping error."""
import copy
import itertools
import json
import re
import unittest

import test_workflow as workflow
from test_extraction_checkpoint import FakeCLI
from test_fact_exclusions import GOOD, MISTYPED, PAGE, PHASE, UNKNOWN, D, S

from verinote.extraction_coverage import exclude_off_dictionary_facts
from verinote.ontology import VocabularyError, dictionary_terms, extraction_quote_issues
from verinote.service import Service

INVENTED = GOOD | {'predicate': 'invented'}
BAD_QUOTE = GOOD | {'quote': 7}
OFF_BAD_QUOTE = UNKNOWN | {'quote': 7}
EMPTY_SUBJECT = GOOD | {'subject': ''}
MISSING_QUOTE = GOOD | {'quote': '원문에 전혀 없는 문장'}
VOCABULARY = '정확히 일치하는 것이 없습니다'
BASIS = '(관계 번호는 사전 불일치 제외 뒤 기준)'
UNAPPROVED = '승인 사전에 없는 관계: invented'


def response(facts, **fields):
    rows = []
    for relation in D['relations']:
        found = [i for i, f in enumerate(facts) if isinstance(f, dict) and f.get('predicate') == relation['id']]
        rows.append({'predicate': relation['id'], 'fact_indices': found, 'claim_indices': [],
                     'status': 'extracted' if found else 'no_evidence', 'reason': 'r'})
    return copy.deepcopy(PAGE | {'facts': facts, 'coverage': rows, 'dictionary_gaps': []}) | fields


class Detail(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def execute(self, *outputs, contract=None):
        doc = self.store.register('x', 'x.md', S.encode(), S)
        workflow.approved_dictionary(self.store, doc['id'], D)
        cli = FakeCLI(outputs)
        service = Service(self.store, self.root, cli)
        if contract is None:
            current = self.store.document(doc['id'])
            run = service.start(doc['id'], 'copilot', 'chosen', expected_version=current['version'], expected_generation=current['generation'])
            service.pool.shutdown(wait=True)
        else:
            run = self.store.create_run(doc['id'], 'copilot', 'chosen', 'extract', doc['version'], doc['generation'])
            pin = dict(run['ontology'], extraction_contract=contract)
            with self.store.connect(True) as db:
                db.execute('UPDATE runs SET ontology=? WHERE id=?', (json.dumps(pin, ensure_ascii=False), run['id']))
            run = self.store.run(run['id'])
            service.slots.acquire()
            service.registered_extractions[run['id']] = object()
            service.extract(run, cli)
            service.pool.shutdown(wait=True)
        items = [i for i in self.store.document(doc['id'])['items'] if i['kind'] != 'ontology']
        return self.store.run(run['id']), len(cli.calls), items

    def test_off_dictionary_then_unknown_predicate_detail_cites_the_unknown_predicate_with_its_response_number(self):
        run, calls, items = self.execute(response([UNKNOWN, INVENTED]))
        self.assertEqual((run['status'], calls, items), ('error', 1, []))
        self.assertIn(f'관계 2 인용: {UNAPPROVED}', run['detail'])
        self.assertNotIn(VOCABULARY, run['detail'])
        self.assertNotIn(BASIS, run['detail'])

    def test_each_mixed_shape_cites_its_first_chunk_stopping_fact_by_response_number(self):
        rows = [([UNKNOWN, 'invalid'], '관계 2 인용: JSON 객체'), (['invalid', UNKNOWN], '관계 1 인용: JSON 객체'),
                ([UNKNOWN, 'invalid', INVENTED], '관계 2 인용: JSON 객체'), ([UNKNOWN, INVENTED, 'invalid', MISTYPED], f'관계 2 인용: {UNAPPROVED}'),
                ([UNKNOWN, EMPTY_SUBJECT], '관계 2 인용: subject'), ([OFF_BAD_QUOTE, INVENTED], f'관계 2 인용: {UNAPPROVED}'),
                ([MISTYPED, INVENTED], f'관계 2 인용: {UNAPPROVED}'), ([GOOD, UNKNOWN, INVENTED], f'관계 3 인용: {UNAPPROVED}'),
                ([BAD_QUOTE, UNKNOWN, INVENTED], '관계 1 인용: quote')]
        for facts, expected in rows:
            with self.subTest(expected=expected, facts=facts):
                self.tearDown()
                self.setUp()
                run, _, items = self.execute(response(facts))
                self.assertEqual((run['status'], items), ('error', []))
                self.assertIn(expected, run['detail'])
                self.assertNotIn(VOCABULARY, run['detail'])
                self.assertNotIn('방향/타입', run['detail'])

    def test_detail_keeps_a_title_claims_or_count_error_ahead_of_the_facts(self):
        over = [UNKNOWN] * 81
        rows = [(response([UNKNOWN, INVENTED], title=''), 'title:'), (response([UNKNOWN, INVENTED], claims=[]), '청크당 claims'),
                (response(over), '청크당 facts'), (response([UNKNOWN] + [INVENTED] * 80), '청크당 facts')]
        for value, expected in rows:
            with self.subTest(expected=expected):
                self.tearDown()
                self.setUp()
                run, _, _ = self.execute(value)
                self.assertEqual(run['status'], 'error')
                self.assertIn(expected, run['detail'])
                self.assertNotIn(UNAPPROVED, run['detail'])

    def test_kept_fact_bad_quote_after_an_exclusion_states_the_numbering_basis(self):
        run, _, items = self.execute(response([UNKNOWN, BAD_QUOTE]))
        self.assertEqual((run['status'], items), ('error', []))
        self.assertIn(f'관계 1 인용: quote: NUL 없이 본문이 포함된 8000자 이하 문자열이 필요합니다. {BASIS}', run['detail'])

    def test_repair_failure_after_an_exclusion_states_the_numbering_basis(self):
        run, calls, _ = self.execute(response([UNKNOWN, MISSING_QUOTE]), {'repairs': 'bad'})
        self.assertEqual((run['status'], calls), ('error', 2))
        self.assertIn('관계 1 인용: 인용 보정 실패', run['detail'])
        self.assertIn(BASIS, run['detail'])

    def test_basis_clause_follows_a_message_naming_a_claim_before_the_fact(self):
        claims = [{'heading': 'h', 'text': 'x', 'quote': '원문에 전혀 없는 주장'}]
        run, _, _ = self.execute(response([UNKNOWN, MISSING_QUOTE], claims=claims), {'repairs': 'bad'})
        self.assertIn('위키 주장 1 인용, 관계 1 인용: 인용 보정 실패', run['detail'])
        self.assertIn(BASIS, run['detail'])

    def test_no_basis_clause_without_an_exclusion_or_without_a_fact_number(self):
        rows = [(response([BAD_QUOTE]), '관계 1 인용: quote'), (response([BAD_QUOTE, UNKNOWN, INVENTED]), '관계 1 인용: quote'),
                (response([UNKNOWN, GOOD], claims=[{'heading': 'h', 'text': 'x', 'quote': 5}]), '위키 주장 1 인용'),
                (response([UNKNOWN, GOOD], coverage=[]), '추출 점검 coverage')]
        for value, expected in rows:
            with self.subTest(expected=expected):
                self.tearDown()
                self.setUp()
                run, _, _ = self.execute(value)
                self.assertIn(expected, run['detail'])
                self.assertNotIn(BASIS, run['detail'])

    def test_legacy_contract_detail_still_cites_the_vocabulary_error_it_stops_on(self):
        run, _, _ = self.execute(response([UNKNOWN, INVENTED]), contract=0)
        self.assertEqual(run['status'], 'error')
        self.assertIn(f'관계 1 인용: 승인된 개체·별칭 중 이 표기와 {VOCABULARY}', run['detail'])
        self.assertNotIn(BASIS, run['detail'])

    def test_contract_v1_detail_cites_the_stopping_fact(self):
        run, _, _ = self.execute(response([UNKNOWN, INVENTED]), contract=1)
        self.assertIn(f'관계 2 인용: {UNAPPROVED}', run['detail'])


class Preflight(unittest.TestCase):
    def test_default_call_still_raises_the_vocabulary_error(self):
        value = response([UNKNOWN, INVENTED])
        with self.assertRaisesRegex(ValueError, f'관계 1 인용: 승인된 개체·별칭 중 이 표기와 {VOCABULARY}'):
            extraction_quote_issues(value, S, D)

    def test_over_sequences_up_to_three_facts_skipping_cites_a_row_that_alone_raises_and_no_earlier_row_does(self):
        kinds = (GOOD, PHASE, UNKNOWN, MISTYPED, OFF_BAD_QUOTE, INVENTED, 'invalid', EMPTY_SUBJECT, BAD_QUOTE, MISSING_QUOTE)

        def alone(output, fact):
            extraction_quote_issues(output | {'facts': [fact]}, S, D, skip_off_dictionary=True)

        for size in range(4):
            for facts in itertools.product(kinds, repeat=size):
                output, _ = exclude_off_dictionary_facts(response(list(facts)), D)
                try:
                    strict = extraction_quote_issues(output, S, D)
                except ValueError:
                    strict = None
                try:
                    skipped = extraction_quote_issues(output, S, D, skip_off_dictionary=True)
                except ValueError as exc:
                    skipped = None
                    found = re.match(r'관계 (\d+) 인용', str(exc))
                    cited = int(found[1]) - 1 if found else None
                    if cited is not None:
                        for earlier in output['facts'][:cited]:
                            alone(output, earlier)
                        self.assertRaises(ValueError, alone, output, output['facts'][cited])
                        if isinstance(output['facts'][cited], dict):
                            try:
                                dictionary_terms(output['facts'][cited], D)
                            except VocabularyError:
                                self.fail(f'{facts}: cited a row that skipping passes over')
                            except ValueError:
                                pass
                self.assertEqual(strict, skipped, facts)


if __name__ == '__main__':
    unittest.main()
