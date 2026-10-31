"""Reproduce rejected evidence without calling a model or changing the evidence."""
import copy
import hashlib
import io
import json
import runpy
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from verinote.extraction_ab_runner import file_index
from verinote.extraction_diagnostics import diagnose_saved, interval_union, known_sum
from verinote.extraction_evaluation import evaluate_case, ideal_outputs, load_case
from verinote.ontology import extraction_quote_issues

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT/'benchmarks/extraction/results/755-ab-v1-sonnet'


class Diagnostics(unittest.TestCase):
    def test_actual_evidence_deterministic_and_read_only(self):
        before = file_index(EVIDENCE)
        with patch('subprocess.Popen', side_effect=AssertionError('no subprocess')):
            result = diagnose_saved(EVIDENCE)
            self.assertEqual(result, diagnose_saved(EVIDENCE))
        self.assertEqual(file_index(EVIDENCE), before)
        self.assertEqual(result['accounting'], {'calls': 137, 'tokens': 880878, 'reported_usd': 2.8507081})
        self.assertEqual(len(result['timing']), 85)
        self.assertEqual(len(result['cold_long_calls']), 12)
        failure, = result['quote_failures']
        self.assertEqual(failure['key'], 'cold-boundary-measurement-a-3')
        self.assertEqual(failure['production_preflight_issues'], [[], []])
        self.assertFalse(failure['original_evaluation']['passed'])
        self.assertTrue(failure['separate_context_control_evaluation']['passed'])
        self.assertEqual(failure['original_evaluation']['quote_ambiguities'][0]['occurrences'],
                         [[8022, 8042], [8073, 8093]])
        self.assertTrue(any(t['request_work'] > t['request_wall']*1.5 for t in result['timing']))

    def test_minimal_ambiguous_context_contract_gap(self):
        case = copy.deepcopy(load_case('boundary'))
        source = 'Actual configuration: Atlas calls Billing.\nAnother hypothetical example: Atlas calls Billing.\n'
        case['source'] = source
        case['source_sha256'] = hashlib.sha256(source.encode()).hexdigest()
        for row in case['dictionary']['entities']+case['dictionary']['relations']:
            row['quote'] = 'Actual configuration: Atlas calls Billing.'
        case['gold'][0]['evidence'] = [{'start': 22, 'end': 42, 'text': 'Atlas calls Billing.'}]
        outputs = ideal_outputs(case)
        outputs[0]['output']['facts'][0]['quote'] = 'Atlas calls Billing.'
        self.assertEqual(extraction_quote_issues(outputs[0]['output'], source, case['dictionary'], require_coverage=True), [])
        score = evaluate_case(case, outputs)
        self.assertEqual(score['citation_errors'][0]['reason'], 'ambiguous_occurrence')
        self.assertEqual(score['grounded_true_positive_count'], 0)
        outputs[0]['output']['facts'][0]['quote'] = 'Actual configuration: Atlas calls Billing.'
        self.assertTrue(evaluate_case(case, outputs)['passed'])

    def test_missing_corrupt_and_partially_verified_rejected_evidence_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'evidence'; shutil.copytree(EVIDENCE, root)
            report = root/'report.json'; original = report.read_bytes()
            report.write_bytes(original+b' ')
            with self.assertRaises(ValueError): diagnose_saved(root)
            report.write_bytes(original)
            with patch('verinote.extraction_diagnostics.evaluate_saved', return_value={
                    'verdict': 'rejected', 'unverified_other_records': True}):
                with self.assertRaises(ValueError): diagnose_saved(root)
            (root/'plan.json').unlink()
            with self.assertRaises(ValueError): diagnose_saved(root)

    def test_overlap_zero_and_unknown_are_not_added_or_invented(self):
        self.assertEqual(interval_union([(0, 4), (1, 3), (3, 6), (8, 9)]), 7)
        self.assertEqual(interval_union([]), 0)
        self.assertEqual(interval_union([(0, 0)]), 0)
        for spans in ([(-1, 0)], [(3, 2)], [(0, float('inf'))]):
            with self.assertRaises(ValueError): interval_union(spans)
        self.assertIsNone(known_sum([1, None]))
        self.assertIsNone(known_sum([float('nan')]))
        self.assertEqual(known_sum([0, 0]), 0)

    def test_cli_no_subprocess_no_overwrite_or_original_output(self):
        script = str(ROOT/'scripts/diagnose_extraction.py')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'out.json'
            for args in ([script, str(EVIDENCE)], [script, str(EVIDENCE), '--output', str(output)]):
                with patch('sys.argv', args), patch('subprocess.Popen', side_effect=AssertionError()), redirect_stdout(io.StringIO()) as stdout:
                    runpy.run_path(script, run_name='__main__')
                if len(args)==2: self.assertEqual(json.loads(stdout.getvalue())['verdict'], 'rejected')
            saved = output.read_bytes()
            for dest in (output, EVIDENCE/'forbidden.json'):
                with patch('sys.argv', [script, str(EVIDENCE), '--output', str(dest)]):
                    with self.assertRaises(SystemExit) as exc: runpy.run_path(script, run_name='__main__')
                    self.assertEqual(exc.exception.code, 2)
            self.assertEqual(output.read_bytes(), saved)
