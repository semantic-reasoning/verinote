"""Historical observation integrity, controlled boundaries and pending judgments."""
import copy
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.diagnostics import extraction_criteria_review as criteria
from verinote.diagnostics.extraction_fidelity import DEFAULT_ROOT, diagnose
from verinote.extraction_ab_runner import _hash, file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case


class CriteriaReview(unittest.TestCase):
    def test_authenticated_sixteen_errors_unchanged_and_grouped_without_new_verdict(self):
        before = file_index(DEFAULT_ROOT); original = diagnose(); report = criteria.review()
        self.assertEqual(report['historical_observations']['finite_evaluation'], original['finite_gold'])
        self.assertEqual(len(original['finite_gold']['annotation_errors']), 16)
        self.assertFalse(original['finite_gold']['passed']); self.assertTrue(original['production_passed'])
        rows = report['criteria_review']['errors']
        self.assertEqual([r['observations']['frozen_error'] for r in rows], original['finite_gold']['annotation_errors'])
        self.assertEqual(file_index(DEFAULT_ROOT), before)
        self.assertEqual(report['human_review'], 'pending'); self.assertEqual(report['judgment_author'], 'agent_proposed')
        self.assertNotIn('verdict', report['criteria_review']); self.assertNotIn('passed', report['criteria_review'])
        self.assertLess(len(report['criteria_review']['shared_symptoms']), 16)
        for row in rows:
            observations, proposal = row['observations'], row['agent_proposed_review']
            self.assertEqual(proposal['human_review'], 'pending'); self.assertTrue(proposal['judgments'])
            self.assertTrue(observations['actual_claims']); self.assertTrue(observations['expected_source_evidence'])
            for claim in observations['actual_claims']:
                self.assertTrue(claim['quote']); self.assertTrue(claim['text'])
            for requirement in observations['prompt_requirement_ids']:
                self.assertIn(requirement, report['historical_observations']['prompt_requirements'])
            for judgment in proposal['judgments']:
                self.assertTrue(judgment['rationale']); self.assertTrue(judgment['consumer_consequence']); self.assertTrue(judgment['uncertainty'])
                for source in judgment['consumer_refs']:
                    self.assertIn(source, report['inspected_consumer_and_evaluator_sources'])
        gaps = [r for r in rows if r['observations']['frozen_error']['reason'] == 'gap_completeness']
        self.assertEqual(len(gaps), 3)
        for row in gaps:
            self.assertEqual(set(row['agent_proposed_review']['categories']), {'product_evidence_gap', 'representation_sensitive', 'unresolved'})

    def test_observed_historical_status_keeps_quote_reason_and_correct_indices(self):
        report = criteria.review()
        rows = [r for r in report['criteria_review']['errors'] if r['observations']['frozen_error'].get('predicate') == 'archives']
        self.assertEqual(len(rows), 2)
        for row, expected_index in zip(rows, (2, 3), strict=True):
            observed = row['observations']; coverage = observed['actual_coverage'][0]
            self.assertEqual(coverage['status'], 'no_evidence'); self.assertEqual(coverage['claim_indices'], [expected_index])
            self.assertIn('과거', coverage['reason'])
            quote = observed['actual_claims'][expected_index]['quote']
            self.assertIn('2020년', quote); self.assertIn('2025년', quote); self.assertIn('중단', quote)
            self.assertEqual(row['agent_proposed_review']['categories'], ['representation_sensitive', 'unresolved'])
        missing = [r for r in report['criteria_review']['errors'] if r['observations']['frozen_error']['reason'] == 'unexpected_gap_evidence']
        for row in missing:
            observed = row['observations']; gap = observed['actual_gaps'][0]
            self.assertEqual(gap['code'], 'missing_relation')
            for index in gap['claim_indices']:
                self.assertIn('승인 개체에 없다', observed['actual_claims'][index]['quote'])
                self.assertNotIn('소유', observed['actual_claims'][index]['quote'])

    def test_classification_abstains_when_observed_relationship_no_longer_matches(self):
        original = diagnose(); changed = copy.deepcopy(original)
        obligation = changed['obligations'][0]
        row = next(r for r in obligation['coverage_obligations'] if r['predicate'] == 'archives')
        row['missing_claim_ids'] = ['historical']
        reviewed = criteria.historical_review(changed)
        found = next(r for r in reviewed['errors'] if r['observations']['frozen_error'] == {'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'archives'})
        self.assertEqual(found['agent_proposed_review']['categories'], ['unresolved'])
        self.assertEqual(found['agent_proposed_review']['judgments'], [])
        self.assertIsNotNone(found['agent_proposed_review']['abstention'])

    def test_valid_invariances_remain_production_and_finite_passes(self):
        controls = {row['id']: row for row in criteria.controls()['cases']}
        for key in ('split_claims_baseline', 'order_and_remap', 'combined_contiguous_claim', 'incidental_wording',
                    'split_gap_rows', 'group_gap_rows', 'longer_contiguous_quotes'):
            with self.subTest(control=key):
                self.assertTrue(controls[key]['observed']['production_passed'])
                self.assertTrue(controls[key]['observed']['finite_passed'])
                self.assertEqual(controls[key]['agent_proposed_product_expectation']['human_review'], 'pending')
        combined = controls['combined_contiguous_claim']['output']
        self.assertEqual(len(combined['claims']), 1)
        self.assertTrue(all(row['claim_indices'] in ([], [0]) for row in combined['coverage']+combined['dictionary_gaps']))

    def test_only_identified_fixture_labels_removed_and_frozen_rejection_preserved(self):
        case = load_case('short', DATA_ROOT/'v2'); before = copy.deepcopy(case)
        oracle = ideal_outputs(case, contract_version=2)[0]['output']
        control = next(r for r in criteria.controls()['cases'] if r['id'] == 'short_quotes_without_fixture_labels')
        output = control['output']
        self.assertEqual(output['facts'], oracle['facts']); self.assertEqual(output['coverage'], oracle['coverage'])
        self.assertEqual(output['dictionary_gaps'], oracle['dictionary_gaps'])
        labels = ('구간 1 부정: ', '구간 1 조건: ', '구간 1 과거 기록: ', '구간 1 미등록 대상: ', '구간 1 소유: ')
        for actual, expected, label in zip(output['claims'], oracle['claims'], labels, strict=True):
            self.assertEqual(label+actual['quote'], expected['quote'])
            self.assertEqual(actual['text'], expected['text']); self.assertEqual(actual['heading'], expected['heading'])
        self.assertIn('않는다', output['claims'][0]['quote']); self.assertIn('장애가 발생할 때만', output['claims'][1]['quote'])
        self.assertIn('2025년부터', output['claims'][2]['quote']); self.assertIn('중단', output['claims'][2]['quote'])
        self.assertTrue(control['observed']['production_passed']); self.assertFalse(control['observed']['finite_passed'])
        self.assertEqual(len(control['observed']['finite_evaluation']['annotation_errors']), 12)
        self.assertEqual(case, before); self.assertEqual(load_case('short', DATA_ROOT/'v2'), before)

    def test_correct_quote_with_contradictory_prose_documents_scope_limit(self):
        control = next(r for r in criteria.controls()['cases'] if r['id'] == 'contradictory_prose_with_correct_quote')
        quote = control['output']['claims'][0]['quote']; text = control['output']['claims'][0]['text']
        self.assertIn('호출하지 않는다', quote); self.assertIn('무조건 직접 호출한다', text)
        self.assertTrue(control['observed']['production_passed']); self.assertTrue(control['observed']['finite_passed'])
        self.assertEqual(control['agent_proposed_product_expectation']['expectation'], 'material_failure')
        self.assertEqual(control['agent_proposed_product_expectation']['human_review'], 'pending')

    def test_dangerous_and_policy_controls_do_not_get_automatic_overrides(self):
        controls = {row['id']: row for row in criteria.controls()['cases']}
        for key in ('dangerous_conditional_dropping', 'wrong_gap_reference'):
            self.assertTrue(controls[key]['observed']['production_passed'])
            self.assertFalse(controls[key]['observed']['finite_passed'])
            self.assertEqual(controls[key]['agent_proposed_product_expectation']['expectation'], 'material_failure')
        for key in ('missing_coverage_row', 'status_contradicts_emitted_fact'):
            self.assertFalse(controls[key]['observed']['production_passed'])
            self.assertIsNone(controls[key]['observed']['finite_passed'])
        for key in ('historical_status_with_preserved_evidence', 'historical_gap_omitted_but_claim_retained'):
            self.assertTrue(controls[key]['observed']['production_passed']); self.assertFalse(controls[key]['observed']['finite_passed'])
            self.assertEqual(controls[key]['agent_proposed_product_expectation']['expectation'], 'policy_unresolved')

    def test_readonly_deterministic_report_without_network_subprocess_or_db_writes(self):
        before = file_index(DEFAULT_ROOT); connect = sqlite3.connect
        def readonly(database, *args, **kwargs):
            self.assertTrue(kwargs.get('uri')); self.assertTrue(str(database).endswith('?mode=ro'))
            return connect(database, *args, **kwargs)
        with patch('urllib.request.urlopen', side_effect=AssertionError('HTTP prohibited')), \
             patch('http.client.HTTPConnection.connect', side_effect=AssertionError('HTTP prohibited')), \
             patch('subprocess.Popen', side_effect=AssertionError('subprocess prohibited')), \
             patch('subprocess.run', side_effect=AssertionError('subprocess prohibited')), \
             patch('sqlite3.connect', side_effect=readonly), \
             patch.object(Path, 'write_text', side_effect=AssertionError('write prohibited')), \
             patch.object(Path, 'write_bytes', side_effect=AssertionError('write prohibited')):
            first = criteria.review(); second = criteria.review()
        self.assertEqual(first, second); self.assertEqual(before, file_index(DEFAULT_ROOT))
        self.assertEqual(first['generation_requests'], 0)
        self.assertEqual(first['review_base_revision'], 'eafa841de6980dd08f2cf11a1413688038ab9fe2')
        for source in first['inspected_consumer_and_evaluator_sources'].values():
            path = criteria.REPO/source['path']
            self.assertEqual(_hash(path), source['sha256'])
            self.assertEqual('\n'.join(path.read_text().splitlines()[source['line_start']-1:source['line_end']]), source['excerpt'])
        historical = first['historical_authentication']['recorded_source_hashes']
        self.assertTrue(all(path not in historical for path in criteria.REVIEW_PATHS))
        self.assertEqual(set(first['review_implementation']['source_hashes']), set(criteria.REVIEW_PATHS))
