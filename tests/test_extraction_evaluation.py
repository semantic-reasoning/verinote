"""Closed-world synthetic gold and offline fact/citation scoring."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from verinote.extraction_evaluation import (
    DATA_ROOT,
    evaluate_case,
    evaluate_many,
    ideal_outputs,
    load_case,
    load_manifest,
    validate_case,
)
from verinote.ontology import (
    apply_quote_repairs,
    dictionary_fact,
    extraction_quote_issues,
)


class Evaluation(unittest.TestCase):
    def test_all_fixtures_oracles_pass_existing_validation_and_evaluation(self):
        self.assertEqual({r['id'] for r in load_manifest()['cases']}, {'short', 'long', 'dense', 'none', 'boundary'})
        for row in load_manifest()['cases']:
            case = load_case(row['id']); outputs = ideal_outputs(case)
            before = copy.deepcopy((case, outputs)); score = evaluate_case(case, outputs)
            self.assertTrue(score['passed'], row['id'])
            self.assertEqual(score['true_positive_count'], len(case['gold']))
            self.assertEqual(score['grounded_true_positive_count'], len(case['gold']))
            self.assertEqual((case, outputs), before)
            self.assertEqual(evaluate_case(case, outputs), score)
            self.assertEqual(len(score['case_sha256']), 64)
        self.assertGreater(len(load_case('long')['source']), 8000)
        self.assertGreater(len(load_case('boundary')['source']), 8000)

    def test_structurally_valid_missing_fact_reduces_recall(self):
        case = load_case('short'); outputs = ideal_outputs(case); output = outputs[0]['output']
        output['facts'].pop(); output['coverage'][0]['fact_indices'] = [0]
        self.assertEqual(extraction_quote_issues(output, case['source'], case['dictionary'], require_coverage=True), [])
        for row in output['facts']: dictionary_fact(row, case['source'], case['dictionary'])
        score = evaluate_case(case, outputs)
        self.assertEqual(score['recall']['value'], .5); self.assertEqual(score['false_negatives'], ['short-2'])
        self.assertEqual(score['precision']['value'], 1)

    def test_aliases_variants_and_duplicates_do_not_inflate_tp(self):
        case = load_case('short'); outputs = ideal_outputs(case)
        outputs[0]['output']['facts'][0]['object'] = 'BILL'
        outputs[0]['output']['facts'].append(copy.deepcopy(outputs[0]['output']['facts'][0]))
        score = evaluate_case(case, outputs)
        self.assertEqual((score['true_positive_count'], score['prediction_count'], score['raw_prediction_count']), (2, 2, 3))
        self.assertEqual(len(score['duplicates']), 1); self.assertFalse(score['passed'])
        outputs[0]['output']['facts'][-1]['quote'] = 'not in source'
        score = evaluate_case(case, outputs)
        self.assertEqual(len(score['citation_errors']), 1); self.assertEqual(score['grounded_true_positive_count'], 2)
        self.assertFalse(score['passed'])

    def test_forbidden_and_unlisted_additions_are_both_false_positives(self):
        case = load_case('short'); outputs = ideal_outputs(case)
        quote = outputs[0]['output']['facts'][0]['quote']
        outputs[0]['output']['facts'] += [{'subject': 'Atlas', 'predicate': 'calls', 'object': 'Ledger', 'quote': quote},
                                         {'subject': 'Ledger', 'predicate': 'calls', 'object': 'Atlas', 'quote': quote}]
        score = evaluate_case(case, outputs)
        self.assertEqual(score['false_positive_count'], 2); self.assertEqual(score['precision']['value'], .5)
        self.assertEqual(sum(r['forbidden'] for r in score['false_positives']), 1)

    def test_same_triple_bad_quote_is_semantic_tp_but_not_grounded_tp(self):
        case = load_case('short'); outputs = ideal_outputs(case)
        outputs[0]['output']['facts'][0]['quote'] = 'invented quote'
        score = evaluate_case(case, outputs)
        self.assertEqual((score['true_positive_count'], score['grounded_true_positive_count']), (2, 1))
        self.assertEqual(score['precision']['value'], 1); self.assertEqual(score['citation_accuracy']['value'], .5)
        self.assertEqual(score['grounded_missing'], ['short-1'])

    def test_quote_from_another_chunk_is_not_globally_accepted(self):
        case = load_case('long'); outputs = ideal_outputs(case)
        outputs[1]['output']['facts'].append(outputs[0]['output']['facts'].pop())
        score = evaluate_case(case, outputs)
        self.assertEqual(score['true_positive_count'], 2)
        self.assertEqual(score['citation_errors'][0]['reason'], 'not_in_origin_chunk')
        self.assertEqual(score['citation_errors'][0]['chunk'], 2)

    def test_boundary_repeated_occurrence_ambiguity_and_context(self):
        case = load_case('boundary'); outputs = ideal_outputs(case)
        outputs[1]['output']['facts'][0]['quote'] = 'Atlas calls Billing.'
        score = evaluate_case(case, outputs)
        self.assertEqual(score['citation_errors'][0]['reason'], 'ambiguous_occurrence')
        self.assertEqual(len(score['quote_ambiguities'][0]['occurrences']), 2)
        self.assertEqual(score['grounded_true_positive_count'], 0)
        outputs[0]['output']['facts'] = outputs[1]['output']['facts']; outputs[1]['output']['facts'] = []
        score = evaluate_case(case, outputs)
        self.assertEqual(score['citation_errors'][0]['reason'], 'wrong_or_incomplete_evidence')
        self.assertTrue(evaluate_case(case, ideal_outputs(case))['passed'])

    def test_partial_quote_rejected_broad_literal_quote_is_documented_policy(self):
        case = load_case('short'); outputs = ideal_outputs(case)
        outputs[0]['output']['facts'][0]['quote'] = 'Atlas directly calls'
        self.assertEqual(evaluate_case(case, outputs)['grounded_true_positive_count'], 1)
        outputs[0]['output']['facts'][0]['quote'] = case['source']
        self.assertEqual(evaluate_case(case, outputs)['grounded_true_positive_count'], 2)

    def test_empty_truth_and_predictions_have_explicit_zero_denominators(self):
        none = load_case('none'); score = evaluate_case(none, [])
        self.assertTrue(score['no_relation_case_pass']); self.assertTrue(score['passed'])
        self.assertIsNone(score['precision']['value']); self.assertEqual(score['precision']['reason'], 'not_applicable')
        self.assertIsNone(score['recall']['value'])
        case = load_case('short'); score = evaluate_case(case, [])
        self.assertEqual(score['recall']['value'], 0); self.assertIsNone(score['precision']['value'])
        self.assertEqual(score['false_negative_count'], 2)
        extras = [{'index': 1, 'output': {'facts': [{'subject': 'Atlas', 'predicate': 'calls', 'object': 'Billing', 'quote': none['source']}]}}]
        score = evaluate_case(none, extras)
        self.assertEqual(score['precision']['value'], 0); self.assertIsNone(score['recall']['value']); self.assertFalse(score['no_relation_case_pass'])

    def test_invalid_rows_are_reported_and_global_structure_cannot_be_empty_success(self):
        case = load_case('short'); outputs = ideal_outputs(case)
        outputs[0]['output']['facts'].append({'subject': 'Atlas'})
        score = evaluate_case(case, outputs)
        self.assertEqual(score['prediction_count'], 3); self.assertEqual(score['precision']['value'], 2/3)
        self.assertEqual(score['invalid_facts'], [{'chunk': 1, 'row': 3, 'reason': 'invalid_fact_shape'}])
        for broken in (None, [{'index': 99, 'output': {'facts': []}}], [{'index': True, 'output': {'facts': []}}],
                       [{'index': 1, 'output': {}}], [outputs[0], outputs[0]]):
            with self.assertRaises(ValueError): evaluate_case(case, broken)

    def test_loader_rejects_bad_positions_hash_ambiguous_variants_and_forbidden_overlap(self):
        original = load_case('short')
        for mutate in (lambda c: c.update(source_sha256='a'*64), lambda c: c['gold'][0]['evidence'][0].update(start=1),
                       lambda c: c['gold'][1].update(variants=c['gold'][0]['variants']),
                       lambda c: c['forbidden'].append(['Atlas','calls','BILL']),
                       lambda c: c['gold'][0]['variants'].append(c['gold'][0]['variants'][0])):
            case = copy.deepcopy(original); mutate(case)
            with self.assertRaises(ValueError): validate_case(case)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manifest = load_manifest(); (root/'manifest.json').write_text(json.dumps(manifest))
            (root/'short.json').write_text(json.dumps(original | {'notes': 'changed'}))
            with self.assertRaises(ValueError): load_case('short', root)
            manifest['cases'][0]['file'] = '../secret.json'; (root/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): load_manifest(root)

    def test_intentional_repair_case_is_separate_and_direct_correct_is_equally_valid(self):
        fixture = json.loads((DATA_ROOT/'repair-cases/prefixed_quote.json').read_text()); case = load_case(fixture['case_id'])
        bad = fixture['bad_output']; issues = extraction_quote_issues(bad, case['source'], case['dictionary'], require_coverage=True)
        self.assertTrue(issues)
        repaired, _ = apply_quote_repairs(bad, case['source'], issues, fixture['expected_repair'])
        self.assertEqual(repaired, fixture['expected_output'])
        self.assertFalse(evaluate_case(case, [{'index': 1, 'output': bad}])['passed'])
        self.assertTrue(evaluate_case(case, [{'index': 1, 'output': repaired}])['passed'])
        self.assertTrue(evaluate_case(case, ideal_outputs(case))['passed'])

    def test_micro_metrics_use_raw_counts_and_validate_versions(self):
        a = load_case('short'); b = load_case('dense')
        results = [evaluate_case(a, []), evaluate_case(b, ideal_outputs(b))]
        report = evaluate_many(results)
        self.assertEqual(report['micro_recall']['value'], 7/9)
        self.assertEqual(report['micro_precision']['value'], 1)
        with self.assertRaises(ValueError): evaluate_many([results[0] | {'evaluator_version': 99}])

    def test_unicode_offsets_are_characters_and_occurrence_count_is_bounded(self):
        case = load_case('short'); prefix = '합성 자료\n'
        case['source'] = prefix + case['source']; case['source_sha256'] = hashlib.sha256(case['source'].encode()).hexdigest()
        for gold in case['gold']:
            for e in gold['evidence']: e['start'] += len(prefix); e['end'] += len(prefix)
        self.assertTrue(evaluate_case(case, ideal_outputs(case))['passed'])
        wrong = copy.deepcopy(case); delta = len(prefix.encode()) - len(prefix)
        wrong['gold'][0]['evidence'][0]['start'] += delta; wrong['gold'][0]['evidence'][0]['end'] += delta
        with self.assertRaises(ValueError): validate_case(wrong)
        case = load_case('short'); original = case['source']; line = case['gold'][0]['sample_quote']
        case['source'] += (line+'\n')*70; case['source_sha256'] = hashlib.sha256(case['source'].encode()).hexdigest()
        case['gold'][0]['sample_quote'] = original
        outputs = ideal_outputs(case); outputs[0]['output']['facts'][0]['quote'] = line
        score = evaluate_case(case, outputs)
        self.assertEqual(score['citation_errors'][0]['reason'], 'occurrence_limit')
        self.assertEqual(len(score['quote_ambiguities'][0]['occurrences']), 64)

    def test_machine_readable_examples_and_human_review_record(self):
        review = load_manifest()['human_review']
        self.assertEqual(review['status'], 'approved'); self.assertIn('표를 검토했고 정답 기준이 맞습니다', review['evidence'])
        for name in ('short-perfect', 'short-missing'):
            root = Path('benchmarks/extraction/examples')
            inputs = json.loads((root/f'{name}-input.json').read_text())
            expected = json.loads((root/f'{name}-score.json').read_text())
            actual = evaluate_case(load_case(inputs['case_id']), inputs['chunks']); actual['dataset_human_review'] = review
            self.assertEqual(actual, expected)

    def test_cli_is_deterministic_offline_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'input.json'; output = Path(tmp)/'score.json'
            source.write_text(json.dumps({'case_id': 'short', 'chunks': ideal_outputs(load_case('short'))}))
            command = [sys.executable, '-m', 'scripts.evaluate_extraction', '--case', 'short', '--result', str(source)]
            environment = dict(os.environ, OPENROUTER_API_KEY='ignored-synthetic-key')
            one = subprocess.run(command, capture_output=True, text=True, check=True, env=environment).stdout
            two = subprocess.run(command, capture_output=True, text=True, check=True, env=environment).stdout
            self.assertEqual(one, two); self.assertTrue(json.loads(one)['passed']); self.assertNotIn('ignored-synthetic-key', one)
            subprocess.run(command+['--output', str(output)], check=True)
            self.assertEqual(output.read_text(), one)
            source.write_text('x'*(4*1024*1024+1))
            failure = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failure.returncode, 0); self.assertNotIn('Traceback', failure.stderr)
