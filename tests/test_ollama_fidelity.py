"""Production replay and every finite claim/coverage/gap obligation, without models."""
import copy
import unittest
from unittest.mock import patch

from verinote.extraction_ab_runner import file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.ollama_fidelity import HISTORIES, REPO, assess_output, diagnose


class FidelityDiagnosis(unittest.TestCase):
    def setUp(self):
        self.case = load_case('short', DATA_ROOT/'v2')
        self.output = ideal_outputs(self.case, contract_version=2)[0]['output']

    def assess(self, output): return assess_output(self.case, output)

    def test_retained_failures_replay_readonly_without_network_or_subprocess(self):
        roots = [REPO/'benchmarks/extraction/results'/name for name in HISTORIES]
        before = [file_index(root) for root in roots]
        with patch('verinote.ollama.request', side_effect=AssertionError('network')), patch('subprocess.run', side_effect=AssertionError('subprocess')):
            result = diagnose()
        small, large = result['cases'].values()
        self.assertFalse(small['production_passed']); self.assertIn('승인 관계마다 정확히 한 행', small['production_error'])
        self.assertIsNone(small['finite_gold'])
        self.assertTrue(large['production_passed']); self.assertFalse(large['finite_gold']['passed'])
        self.assertIn('gap_completeness', {e['reason'] for e in large['finite_gold']['annotation_errors']})
        self.assertEqual(before, [file_index(root) for root in roots])
        self.assertEqual(result['generation_requests'], 0)

    def test_valid_reordered_output_is_not_tied_to_oracle_order(self):
        out = copy.deepcopy(self.output); size = len(out['claims']); out['claims'].reverse()
        out['coverage'].reverse(); out['dictionary_gaps'].reverse()
        for row in out['coverage']+out['dictionary_gaps']:
            row['claim_indices'] = [size-1-i for i in row['claim_indices']]
        self.assertTrue(self.assess(out)['finite_gold']['passed'])

    def test_every_required_claim_needs_its_own_source_evidence(self):
        for annotation in self.case['annotations'][0]['claims']:
            with self.subTest(claim=annotation['id']):
                out = copy.deepcopy(self.output)
                claim = next(c for c in out['claims'] if annotation['evidence']['text'] in c['quote'])
                claim['quote'] = self.case['gold'][0]['sample_quote']
                result = self.assess(out); self.assertTrue(result['production_passed'])
                self.assertIn({'chunk': 1, 'reason': 'missing_claim_evidence', 'id': annotation['id']}, result['finite_gold']['annotation_errors'])

    def test_every_approved_relation_is_required_once(self):
        for predicate in ('calls', 'archives', 'mirrors'):
            for mode in ('missing', 'duplicate'):
                with self.subTest(predicate=predicate, mode=mode):
                    out = copy.deepcopy(self.output); row = next(c for c in out['coverage'] if c['predicate'] == predicate)
                    if mode == 'missing': out['coverage'].remove(row)
                    else: out['coverage'].append(copy.deepcopy(row))
                    self.assertFalse(self.assess(out)['production_passed'])
        out = copy.deepcopy(self.output); out['coverage'][0]['claim_indices'] = [99]
        self.assertFalse(self.assess(out)['production_passed'])
        out = copy.deepcopy(self.output); out['coverage'][1]['predicate'] = 'unknown'
        self.assertFalse(self.assess(out)['production_passed'])

    def test_gap_obligations_and_wrong_categories_do_not_pass(self):
        for gap in self.case['annotations'][0]['gaps']:
            for annotation_id in gap['claim_ids']:
                with self.subTest(code=gap['code'], claim=annotation_id):
                    out = copy.deepcopy(self.output)
                    evidence = next(a['evidence']['text'] for a in self.case['annotations'][0]['claims'] if a['id'] == annotation_id)
                    index = next(i for i, row in enumerate(out['claims']) if evidence in row['quote'])
                    for row in out['dictionary_gaps']:
                        if row['code'] == gap['code']: row['claim_indices'] = [i for i in row['claim_indices'] if i != index]
                    out['dictionary_gaps'] = [row for row in out['dictionary_gaps'] if row['claim_indices']]
                    result = self.assess(out); self.assertTrue(result['production_passed'])
                    self.assertIn('gap_completeness', {e['reason'] for e in result['finite_gold']['annotation_errors']})
        for mode in ('unrelated', 'wrong_code', 'approved_but_missing_relation'):
            out = copy.deepcopy(self.output)
            if mode == 'unrelated': out['dictionary_gaps'][1]['claim_indices'] = [0]
            elif mode == 'wrong_code': out['dictionary_gaps'][1]['code'] = 'type_mismatch'
            else: out['dictionary_gaps'].append({'code': 'missing_relation', 'detail': 'mirrors의 직접 근거가 없음', 'claim_indices': [0]})
            result = self.assess(out); self.assertTrue(result['production_passed'])
            self.assertFalse(result['finite_gold']['passed'])

    def test_coverage_semantics_are_not_just_array_shape(self):
        out = copy.deepcopy(self.output); out['coverage'][1].update(status='no_evidence', claim_indices=[])
        result = self.assess(out); self.assertTrue(result['production_passed'])
        self.assertIn({'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'archives'}, result['finite_gold']['annotation_errors'])
        out = copy.deepcopy(self.output); out['coverage'][0]['claim_indices'] = [0]
        self.assertFalse(self.assess(out)['finite_gold']['passed'])

    def test_forbidden_flattening_and_ambiguous_citations_are_rejected(self):
        for triple, index in [(('S1', 'calls', 'S0'), 0), (('S0', 'calls', 'S2'), 1), (('S0', 'archives', 'S1'), 2)]:
            out = copy.deepcopy(self.output)
            out['facts'].append(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=out['claims'][index]['quote']))
            coverage = next(r for r in out['coverage'] if r['predicate'] == triple[1]); coverage.update(status='extracted'); coverage['fact_indices'].append(1)
            self.assertFalse(self.assess(out)['finite_gold']['passed'])
        out = copy.deepcopy(self.output); out['facts'][0]['quote'] = 'S0는 S1를 직접 호출한다.'
        self.assertFalse(self.assess(out)['production_passed'])

    def test_existing_mechanical_index_repair_is_replayed_without_mutating_input(self):
        from verinote.extraction_coverage import repair_missing_indices
        out = copy.deepcopy(self.output); out['coverage'][0]['fact_indices'] = []
        before = copy.deepcopy(out)
        normalized, changes = repair_missing_indices(out, self.case['dictionary'])
        result = self.assess(out)
        self.assertEqual(out, before)
        self.assertTrue(result['production_passed']); self.assertTrue(result['finite_gold']['passed'])
        self.assertEqual(result['accounting_changes'], changes)
        self.assertEqual(normalized['coverage'][0]['fact_indices'], [0])
        bad = copy.deepcopy(out); bad['coverage'][0]['fact_indices'] = [99]
        self.assertFalse(self.assess(bad)['production_passed'])

    def test_modified_report_is_not_trusted(self):
        # Test the identity boundary without mutating any retained artifact.
        with patch('verinote.ollama_fidelity.file_index', return_value={'report.json': {'sha256': '0'*64}}), self.assertRaises(ValueError):
            diagnose()


class FidelityCandidate(unittest.TestCase):
    def setUp(self):
        from verinote.ontology import (
            bounded_prompt,
            dictionary_context,
            extraction_prompts,
        )
        case = load_case('short', DATA_ROOT/'v2')
        self.payload = {'filename': 'synthetic.md', 'version': 1, 'offset': 0, 'text': case['source'],
                        'approved_dictionary': {'payload': dictionary_context(case['dictionary'])}}
        self.base = bounded_prompt(extraction_prompts(2)[0], self.payload)

    def test_compiler_preserves_input_and_uses_only_approved_relation_ids(self):
        import json

        from verinote import ollama_fidelity as f
        with patch.object(f, 'evaluate_case', side_effect=AssertionError('gold')), patch.object(f, 'load_case', side_effect=AssertionError('fixture')), patch.object(f, 'diagnose', side_effect=AssertionError('history')):
            effective = f.compile_prompt(self.base)
        original_header, original_input = self.base.split('입력(JSON):\n', 1)
        header, actual_input = effective.split('입력(JSON):\n', 1)
        self.assertEqual(actual_input, original_input); self.assertTrue(header.startswith(original_header))
        self.assertIn(json.dumps(['calls', 'archives', 'mirrors'], ensure_ascii=False), header)
        self.assertNotIn('S0', f.TEMPLATE); self.assertNotIn('archives', f.TEMPLATE)
        self.assertLess(len(effective.encode()), 32768-4096-1024)
        self.assertEqual(f.compile_prompt(self.base), effective)
        self.assertNotEqual(f.template_hash(), '0'*64)

    def test_snapshot_is_explicit_strict_and_template_bound(self):
        from verinote import ollama_fidelity as f
        from verinote.agents import CLI
        from verinote.ollama import DEFAULTS, options
        from verinote.transports import job_options
        baseline = options(); candidate = f.candidate_options(baseline)
        self.assertEqual(baseline, DEFAULTS)
        self.assertEqual(candidate['schema_version'], 2)
        self.assertEqual(job_options(candidate), candidate)
        self.assertEqual(CLI(persistent=False).ollama_fidelity, False)
        for key, wrong in [('candidate_id', 'unknown'), ('candidate_template_sha256', '0'*64)]:
            bad = copy.deepcopy(candidate); bad[key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError): options(bad)
        with patch.object(f, 'TEMPLATE', f.TEMPLATE+'changed'), self.assertRaises(ValueError): options(candidate)
        with self.assertRaises(TypeError): CLI(persistent=False, ollama_fidelity='yes')

    def test_nonproduction_or_annotation_payloads_are_rejected(self):
        from verinote import ollama_fidelity as f
        from verinote.ontology import bounded_prompt, extraction_prompts
        with self.assertRaises(ValueError): f.compile_prompt('not a production extraction prompt')
        for key in ('gold', 'annotations', 'expected_statuses'):
            payload = copy.deepcopy(self.payload); payload[key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                f.compile_prompt(bounded_prompt(extraction_prompts(2)[0], payload))
