"""Finite v2 semantic controls; expected triples are stated independently of the builder."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from verinote.documents import chunks
from verinote.extraction_ab_runner import evaluate_saved, file_index
from verinote.extraction_diagnostics import diagnose_saved
from verinote.extraction_evaluation import (
    DATA_ROOT,
    evaluate_case,
    evaluate_many,
    ideal_outputs,
    load_case,
    load_manifest,
    validate_case,
)

ROOT = Path(__file__).resolve().parents[1]


class ThresholdEvaluation(unittest.TestCase):
    def setUp(self):
        self.case = load_case('three', DATA_ROOT/'v2')
        self.outputs = ideal_outputs(self.case, contract_version=2)

    def test_explicit_gold_meaning_and_production_boundaries(self):
        for name, count in [('short', 1), ('three', 3), ('four', 4), ('eight', 8)]:
            case = load_case(name, DATA_ROOT/'v2')
            self.assertEqual(len(list(chunks(case['source']))), count)
            self.assertEqual([g['variants'] for g in case['gold']], [[[f'S{i}', 'calls', f'S{i+1}']] for i in range(count)])
            for i, gold in enumerate(case['gold']):
                self.assertEqual(gold['sample_quote'], f'구간 {i+1} 현재 실제 구성: S{i}는 S{i+1}를 직접 호출한다.')
                self.assertIn([f'S{i+1}', 'calls', f'S{i}'], case['forbidden'])
                self.assertIn([f'S{i}', 'calls', f'S{i+2}'], case['forbidden'])
                self.assertIn([f'S{i}', 'archives', f'S{i+1}'], case['forbidden'])
                if name != 'short': self.assertEqual(gold['evidence'][0]['end'], (i+1)*8000-1)
            result = evaluate_case(case, ideal_outputs(case, contract_version=2))
            self.assertTrue(result['passed'], result)
            self.assertTrue(result['complete'])
            self.assertEqual(result['evaluator_version'], 2)
            self.assertEqual(result['grounded_true_positive_count'], count)

    def test_hypothetical_duplicate_requires_actual_context(self):
        self.outputs[0]['output']['facts'][0]['quote'] = 'S0는 S1를 직접 호출한다.'
        result = evaluate_case(self.case, self.outputs)
        self.assertFalse(result['passed'])
        self.assertEqual(result['citation_errors'][0]['reason'], 'wrong_or_incomplete_evidence')
        self.assertEqual(len(result['quote_ambiguities'][0]['occurrences']), 2)

    def test_negation_condition_and_past_cannot_be_flattened_into_facts(self):
        for triple, claim_index in [(['S1', 'calls', 'S0'], 0), (['S0', 'calls', 'S2'], 1), (['S0', 'archives', 'S1'], 2)]:
            outputs = copy.deepcopy(self.outputs); out = outputs[0]['output']
            out['facts'].append(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=out['claims'][claim_index]['quote']))
            coverage = next(c for c in out['coverage'] if c['predicate'] == triple[1])
            coverage['status'] = 'extracted'; coverage['fact_indices'].append(1)
            result = evaluate_case(self.case, outputs)
            self.assertFalse(result['passed'])
            self.assertEqual(result['false_positive_count'], 1)
            self.assertTrue(result['false_positives'][0]['forbidden'])

    def test_omitted_fact_is_not_rescued_by_coverage(self):
        out = self.outputs[0]['output']; out['facts'] = []
        out['coverage'][0].update(status='no_evidence', fact_indices=[])
        result = evaluate_case(self.case, self.outputs)
        self.assertFalse(result['passed'])
        self.assertEqual(result['false_negative_count'], 1)

    def test_missing_gaps_and_wrong_gap_evidence_fail(self):
        for mutate in ('missing', 'wrong_evidence', 'wrong_code'):
            outputs = copy.deepcopy(self.outputs); gaps = outputs[0]['output']['dictionary_gaps']
            if mutate == 'missing': gaps.pop()
            elif mutate == 'wrong_evidence': gaps[1]['claim_indices'] = [0]
            else: gaps[1]['code'] = 'missing_relation'
            result = evaluate_case(self.case, outputs)
            self.assertFalse(result['passed'])
            self.assertIn('gap_completeness', [e['reason'] for e in result['annotation_errors']])

    def test_historical_gap_needs_historical_evidence(self):
        self.outputs[0]['output']['claims'][2]['quote'] = self.case['gold'][0]['sample_quote']
        result = evaluate_case(self.case, self.outputs)
        self.assertFalse(result['passed'])
        self.assertIn({'chunk': 1, 'reason': 'missing_claim_evidence', 'id': 'historical'}, result['annotation_errors'])

    def test_no_evidence_is_distinct_from_not_representable(self):
        self.outputs[0]['output']['coverage'][1].update(status='no_evidence', claim_indices=[])
        result = evaluate_case(self.case, self.outputs)
        self.assertFalse(result['passed'])
        self.assertIn({'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'archives'}, result['annotation_errors'])

    def test_incomplete_chunks_and_bad_annotation_identity_fail(self):
        self.assertFalse(evaluate_case(self.case, self.outputs[:-1])['passed'])
        for mutate in ('index', 'span', 'gap', 'coverage'):
            case = copy.deepcopy(self.case); annotation = case['annotations'][0]
            if mutate == 'index': annotation['index'] = True
            elif mutate == 'span': annotation['claims'][0]['evidence']['start'] += 1
            elif mutate == 'gap': annotation['gaps'][0]['claim_ids'] = ['unknown']
            else: annotation['coverage'][0]['status'] = 'no_evidence'
            with self.assertRaises(ValueError): validate_case(case)

    def test_versions_and_review_provenance_are_not_interchangeable(self):
        manifest = load_manifest(DATA_ROOT/'v2')
        self.assertEqual(manifest['human_review'], {'status': 'pending', 'evidence': None})
        self.assertEqual(manifest['extraction_contract'], 2)
        with self.assertRaises(ValueError): ideal_outputs(self.case)
        old = load_case('short'); old_result = evaluate_case(old, ideal_outputs(old))
        new_result = evaluate_case(self.case, self.outputs)
        self.assertEqual(evaluate_many([new_result])['evaluator_version'], 2)
        with self.assertRaises(ValueError): evaluate_many([old_result, new_result])
        with tempfile.TemporaryDirectory() as temp:
            manifest['extraction_contract'] = 1
            (Path(temp)/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): load_manifest(temp)

    def test_v1_saved_rejection_and_diagnostics_are_exact_and_readonly(self):
        evidence = ROOT/'benchmarks/extraction/results/755-ab-v1-sonnet'
        before = file_index(evidence)
        self.assertEqual(evaluate_saved(evidence), json.loads((evidence/'evaluation.json').read_text()))
        self.assertEqual(diagnose_saved(evidence), json.loads((ROOT/'benchmarks/extraction/diagnostics/755-ab-v1-sonnet.json').read_text()))
        self.assertEqual(file_index(evidence), before)
