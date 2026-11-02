"""The generic compiler is independent of fixture gold and retained answers."""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import evaluate_extraction_fidelity as cli
from verinote.diagnostics import extraction_fidelity_candidate as candidate
from verinote.ontology import bounded_prompt, extraction_prompts


class CandidateCompiler(unittest.TestCase):
    def setUp(self):
        self.payload = {'filename': 'synthetic.md', 'version': 1, 'offset': 0, 'text': 'S0 has a conditional link.',
                        'approved_dictionary': {'payload': {'relations': [{'id': 'rel_a'}, {'id': 'rel_b'}]}}}
        self.original = bounded_prompt(extraction_prompts(2)[0], self.payload)

    def test_exact_payload_and_generic_relation_only_instructions(self):
        with patch('verinote.extraction_evaluation.load_case', side_effect=AssertionError('fixture access')), \
             patch('verinote.extraction_evaluation.evaluate_case', side_effect=AssertionError('gold access')), \
             patch('verinote.diagnostics.extraction_fidelity.diagnose', side_effect=AssertionError('history access')):
            compiled = candidate.compile_prompt(self.original)
        original_prefix, raw = self.original.split('입력(JSON):\n', 1)
        prefix, effective = compiled.split('입력(JSON):\n', 1)
        self.assertEqual(raw, effective); self.assertTrue(prefix.startswith(original_prefix))
        self.assertIn(json.dumps(['rel_a', 'rel_b']), prefix)
        self.assertNotIn('S0', candidate.TEMPLATE); self.assertNotIn('rel_a', candidate.TEMPLATE)
        self.assertIn('미등록 개체·타입·기타 사전 제약', candidate.TEMPLATE)
        self.assertEqual(compiled, candidate.compile_prompt(self.original))
        self.assertNotEqual(compiled, self.original)
        with patch.object(candidate, 'TEMPLATE', candidate.TEMPLATE+' changed'):
            self.assertNotEqual(candidate.compile_prompt(self.original), compiled)

    def test_only_production_payloads_and_unique_relation_ids(self):
        for key in ('annotations', 'gold', 'expected_statuses', 'retained_output'):
            payload = self.payload | {key: []}
            with self.subTest(key=key), self.assertRaises(ValueError):
                candidate.compile_prompt(bounded_prompt(extraction_prompts(2)[0], payload))
        for ids in ([{'id': 'x'}, {'id': 'x'}], [{'id': 7}], [None]):
            payload = self.payload | {'approved_dictionary': {'payload': {'relations': ids}}}
            with self.assertRaises(ValueError): candidate.compile_prompt(bounded_prompt(extraction_prompts(2)[0], payload))
        with self.assertRaises(ValueError): candidate.compile_prompt('repair or arbitrary prompt')


class CandidateCLI(unittest.TestCase):
    def test_real_and_explicit_argv_are_forwarded_without_model_calls(self):
        arguments = ['run', '--output', '/synthetic/new-trial', '--allow-model-calls']
        for explicit in (None, arguments):
            with self.subTest(explicit=explicit), patch.object(sys, 'argv', ['evaluate_extraction_fidelity.py', *arguments]), \
                 patch.object(cli, 'run', return_value=({}, {'verdict': 'inconclusive'})) as run, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(explicit), 0)
                run.assert_called_once_with(Path('/synthetic/new-trial'), allow_model_calls=True,
                                            invocation=['evaluate_extraction_fidelity.py', *arguments] if explicit is None else arguments)

    def test_explicit_empty_argv_does_not_fall_back_to_process_arguments(self):
        arguments = ['evaluate_extraction_fidelity.py', 'run', '--output', '/synthetic/new-trial', '--allow-model-calls']
        with patch.object(sys, 'argv', arguments), patch.object(cli, 'run') as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error: cli.main([])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()
