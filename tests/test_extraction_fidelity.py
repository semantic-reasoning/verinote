"""Frozen finite obligations and strict read-only historical authentication."""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote import extraction_threshold_ab_runner as runner
from verinote.diagnostics import extraction_fidelity as diagnostic
from verinote.extraction_ab_runner import file_index
from verinote.extraction_benchmark import sha
from verinote.extraction_evaluation import (
    DATA_ROOT,
    evaluate_case,
    ideal_outputs,
    load_case,
)
from verinote.usage import atomic_json

RETAINED_REPORT = json.loads((diagnostic.DEFAULT_ROOT/'report.json').read_text(encoding='utf-8'))


class FidelityObligations(unittest.TestCase):
    def setUp(self):
        self.case = load_case('short', DATA_ROOT/'v2')
        self.output = ideal_outputs(self.case, contract_version=2)[0]['output']

    def assess(self, output):
        return diagnostic.assess_outputs(self.case, [{'index': 1, 'output': output}])

    def test_split_combined_and_reordered_claims_are_all_valid(self):
        variants = [copy.deepcopy(self.output)]
        combined = copy.deepcopy(self.output)
        combined['claims'] = [{'heading': 'Constraints', 'text': 'Source constraints', 'quote': self.case['source']}]
        for row in combined['coverage'] + combined['dictionary_gaps']:
            if row['claim_indices']: row['claim_indices'] = [0]
        variants.append(combined)
        reordered = copy.deepcopy(self.output); size = len(reordered['claims']); reordered['claims'].reverse()
        reordered['coverage'].reverse(); reordered['dictionary_gaps'].reverse()
        for row in reordered['coverage']+reordered['dictionary_gaps']:
            row['claim_indices'] = [size-1-i for i in row['claim_indices']]
        variants.append(reordered)
        for output in variants:
            with self.subTest(claims=len(output['claims'])):
                before = copy.deepcopy(output); result = self.assess(output)
                self.assertEqual(result['status'], 'passed'); self.assertTrue(result['finite_gold']['passed'])
                self.assertEqual(output, before)
                matrix = result['obligations'][0]
                for row in matrix['claim_obligations']:
                    self.assertTrue(row['passed']); self.assertTrue(row['matched_claim_indices'])
                    for clause in row['prompt_requirements']:
                        self.assertIn(result['prompt_requirements'][clause]['text'], diagnostic.extraction_prompts(2)[0])
                self.assertTrue(all(row['passed'] for row in matrix['coverage_obligations']+matrix['gap_obligations']))
        self.assertEqual([r['matched_claim_indices'] for r in self.assess(combined)['obligations'][0]['claim_obligations']], [[0]]*5)

    def test_all_three_chunk_indices_are_preserved_and_evaluated_together(self):
        case = load_case('three', DATA_ROOT/'v2'); outputs = ideal_outputs(case, contract_version=2)
        result = diagnostic.assess_outputs(case, list(reversed(outputs)))
        self.assertTrue(result['finite_gold']['passed']); self.assertEqual(result['finite_gold'], evaluate_case(case, list(reversed(outputs))))
        self.assertEqual([r['index'] for r in result['chunks']], [3, 2, 1])
        self.assertEqual([r['offset'] for r in result['chunks']], [16000, 8000, 0])
        for rows in ([outputs[0], outputs[0]], [{'index': 0, 'output': outputs[0]['output']}]):
            with self.assertRaisesRegex(ValueError, 'chunk identity'): diagnostic.assess_outputs(case, rows)
        partial = diagnostic.assess_outputs(case, outputs[:1])
        self.assertFalse(partial['finite_gold']['complete']); self.assertFalse(partial['finite_gold']['passed'])

    def test_every_claim_obligation_requires_quoted_evidence_not_prose(self):
        for claim in self.case['annotations'][0]['claims']:
            with self.subTest(claim=claim['id']):
                output = copy.deepcopy(self.output)
                row = next(row for row in output['claims'] if row['quote'] == claim['evidence']['text'])
                row['quote'] = self.case['gold'][0]['sample_quote']
                # Correct claim prose is deliberately retained; it cannot rescue
                # a quote that does not support this obligation.
                result = self.assess(output)
                self.assertTrue(result['production_passed']); self.assertFalse(result['finite_gold']['passed'])
                failure = next(r for r in result['obligations'][0]['claim_obligations'] if r['id'] == claim['id'])
                self.assertEqual(failure['matched_claim_indices'], []); self.assertFalse(failure['passed'])
                self.assertEqual(failure['evaluator_errors'], [{'chunk': 1, 'reason': 'missing_claim_evidence', 'id': claim['id']}])

    def test_missing_wrong_and_unrelated_gaps_are_explained(self):
        for gap in self.case['annotations'][0]['gaps']:
            for claim_id in gap['claim_ids']:
                output = copy.deepcopy(self.output)
                evidence = next(c['evidence']['text'] for c in self.case['annotations'][0]['claims'] if c['id'] == claim_id)
                index = next(i for i, c in enumerate(output['claims']) if c['quote'] == evidence)
                for row in output['dictionary_gaps']:
                    if row['code'] == gap['code']: row['claim_indices'] = [i for i in row['claim_indices'] if i != index]
                output['dictionary_gaps'] = [r for r in output['dictionary_gaps'] if r['claim_indices']]
                result = self.assess(output)
                self.assertTrue(result['production_passed']); self.assertFalse(result['finite_gold']['passed'])
                row = next(r for r in result['obligations'][0]['gap_obligations'] if r['code'] == gap['code'])
                self.assertIn(claim_id, row['missing_claim_ids']); self.assertFalse(row['passed'])
        for mode in ('unrelated', 'wrong_code', 'invented_missing_relation'):
            output = copy.deepcopy(self.output)
            if mode == 'unrelated': output['dictionary_gaps'][1]['claim_indices'] = [0]
            elif mode == 'wrong_code': output['dictionary_gaps'][1]['code'] = 'type_mismatch'
            else: output['dictionary_gaps'].append({'code': 'missing_relation', 'detail': 'No current direct mirror fact', 'claim_indices': [0]})
            result = self.assess(output)
            self.assertTrue(result['production_passed']); self.assertFalse(result['finite_gold']['passed'])
            self.assertTrue(any(r['unexpected_claim_indices'] for r in result['obligations'][0]['actual_gaps']))

    def test_coverage_status_linkage_and_production_shape_are_distinct(self):
        output = copy.deepcopy(self.output); output['coverage'][1].update(status='no_evidence', claim_indices=[])
        result = self.assess(output)
        self.assertTrue(result['production_passed']); self.assertEqual(result['status'], 'fidelity_failure')
        row = next(r for r in result['obligations'][0]['coverage_obligations'] if r['predicate'] == 'archives')
        self.assertEqual((row['expected_status'], row['actual']['status']), ('not_representable', 'no_evidence'))
        self.assertFalse(row['passed']); self.assertTrue(row['missing_claim_ids'])
        output = copy.deepcopy(self.output); output['coverage'][0]['claim_indices'] = [0]
        result = self.assess(output); self.assertFalse(result['finite_gold']['passed'])
        for mode in ('missing', 'duplicate', 'invalid_index', 'unknown_predicate'):
            output = copy.deepcopy(self.output)
            if mode == 'missing': output['coverage'].pop()
            elif mode == 'duplicate': output['coverage'].append(copy.deepcopy(output['coverage'][0]))
            elif mode == 'invalid_index': output['coverage'][0]['claim_indices'] = [99]
            else: output['coverage'][0]['predicate'] = 'unknown'
            result = self.assess(output)
            self.assertEqual(result['status'], 'production_failure'); self.assertIsNone(result['finite_gold'])
            self.assertFalse(result['production_passed']); self.assertEqual(result['obligations'], [])

    def test_negated_conditional_historical_facts_and_ambiguous_quotes_fail(self):
        for triple, index in [(('S1', 'calls', 'S0'), 0), (('S0', 'calls', 'S2'), 1), (('S0', 'archives', 'S1'), 2)]:
            output = copy.deepcopy(self.output)
            output['facts'].append(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=output['claims'][index]['quote']))
            coverage = next(row for row in output['coverage'] if row['predicate'] == triple[1])
            coverage.update(status='extracted'); coverage['fact_indices'].append(1)
            result = self.assess(output)
            self.assertTrue(result['production_passed']); self.assertFalse(result['finite_gold']['passed'])
            self.assertTrue(result['finite_gold']['false_positives'])
        output = copy.deepcopy(self.output); output['facts'][0]['quote'] = 'S0는 S1를 직접 호출한다.'
        result = self.assess(output)
        self.assertEqual(result['status'], 'production_failure'); self.assertTrue(result['chunks'][0]['quote_repair_required'])

    def test_mechanical_accounting_is_recorded_on_copy_in_production_order(self):
        output = copy.deepcopy(self.output); output['coverage'][0]['fact_indices'] = []
        before = copy.deepcopy(output); result = self.assess(output)
        self.assertEqual(output, before); self.assertTrue(result['finite_gold']['passed'])
        chunk = result['chunks'][0]
        self.assertEqual(chunk['accounting_changes'], [{'row': 0, 'predicate': 'calls', 'before': [], 'after': [0]}])
        self.assertEqual(chunk['stages'], ['shape_quote_preflight', 'mechanical_accounting', 'page_validation', 'fact_validation', 'checkpoint_validation'])
        output['coverage'][0]['fact_indices'] = [99]
        self.assertFalse(self.assess(output)['production_passed'])


class PinnedClaudeReplay(unittest.TestCase):
    def test_authenticated_history_is_readonly_without_http_subprocess_or_db_writes(self):
        before = file_index(diagnostic.DEFAULT_ROOT)
        original_connect = sqlite3.connect
        def readonly_connect(database, *args, **kwargs):
            self.assertTrue(kwargs.get('uri')); self.assertTrue(str(database).endswith('?mode=ro'))
            return original_connect(database, *args, **kwargs)
        with patch('urllib.request.urlopen', side_effect=AssertionError('HTTP prohibited')), \
             patch('http.client.HTTPConnection.connect', side_effect=AssertionError('HTTP prohibited')), \
             patch('subprocess.Popen', side_effect=AssertionError('subprocess prohibited')), \
             patch('subprocess.run', side_effect=AssertionError('subprocess prohibited')), \
             patch('sqlite3.connect', side_effect=readonly_connect), \
             patch.object(Path, 'write_bytes', side_effect=AssertionError('write prohibited')), \
             patch.object(Path, 'write_text', side_effect=AssertionError('write prohibited')):
            result = diagnostic.diagnose()
        self.assertEqual(file_index(diagnostic.DEFAULT_ROOT), before)
        self.assertTrue(result['production_passed']); self.assertEqual(result['status'], 'fidelity_failure')
        self.assertEqual(result['generation_requests'], 0); self.assertFalse(result['candidate_evaluated'])
        self.assertEqual([r['index'] for r in result['call_linkages']], [1, 2, 3])
        self.assertEqual(len({r['cli_call_id'] for r in result['call_linkages']}), 3)
        self.assertEqual(len(result['finite_gold']['annotation_errors']), 16)
        missing = [(r['index'], c['id']) for r in result['obligations'] for c in r['claim_obligations'] if not c['passed']]
        self.assertEqual(missing, [(1, 'negated'), (1, 'missing_relation'), (2, 'negated'), (2, 'missing_relation'), (3, 'missing_relation')])
        self.assertEqual(set(result['diagnostic_implementation']['source_hashes']), set(diagnostic.DIAGNOSTIC_PATHS))
        self.assertTrue(all(p not in result['historical_authentication']['recorded_source_hashes'] for p in diagnostic.DIAGNOSTIC_PATHS))

    def test_recorded_source_hashes_equal_the_retained_report_environment_source_hashes(self):
        result = diagnostic.diagnose()
        self.assertEqual(result['historical_authentication']['recorded_source_hashes'],
                         RETAINED_REPORT['environment']['source_hashes'])

    def test_check_recorded_paths_raises_recorded_implementation_path_for_an_absolute_a_dotdot_an_empty_and_a_tests_path(self):
        for relative in ('/verinote/x.py', 'verinote/../tests/x.py', '', 'tests/x.py'):
            with self.subTest(relative=relative), \
                 self.assertRaisesRegex(diagnostic.ArtifactVerificationError, '^recorded_implementation_path$'):
                diagnostic._check_recorded_paths({'source_hashes': {'verinote/documents.py': '0'*64, relative: '0'*64}})

    def test_diagnose_and_evaluate_saved_results_equal_their_baselines_with_both_modules_hash_forged_for_checkout_paths_of_each_non_verinote_recorded_key_verinote_documents_py_and_files_directly_under_verinote(self):
        baseline = diagnostic.diagnose(); verified = runner.evaluate_saved(diagnostic.DEFAULT_ROOT)
        top = diagnostic.REPO/'verinote'
        fixed = sorted(p for p in RETAINED_REPORT['environment']['source_hashes'] if not p.startswith('verinote/'))
        targets = [(key, lambda path, key=key: path == diagnostic.REPO/key) for key in fixed]
        targets.append(('verinote/documents.py', lambda path: path == top/'documents.py'))
        targets.append(('non-verinote recorded keys and files directly under verinote at once',
                        lambda path: path.parent == top or path in {diagnostic.REPO/key for key in fixed}))
        original_hash = diagnostic._hash
        for label, forged in targets:
            def altered(path, forged=forged):
                return '0'*64 if forged(Path(path)) else original_hash(path)
            with self.subTest(label), patch.object(diagnostic, '_hash', side_effect=altered), \
                 patch.object(runner, '_hash', side_effect=altered):
                self.assertEqual(diagnostic.diagnose(), baseline)
                self.assertEqual(runner.evaluate_saved(diagnostic.DEFAULT_ROOT), verified)

    def test_verify_manifest_raises_manifest_fixture_and_evaluate_saved_returns_inconclusive_with_cases_patched_to_append_a_space_to_the_four_source(self):
        original_cases = runner.cases
        def changed():
            data = original_cases(); data['four'] = data['four'] | {'source': data['four']['source']+' '}
            return data
        environment = RETAINED_REPORT['environment']
        with patch.object(runner, 'cases', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '^manifest_fixture$'):
                runner.verify_manifest(diagnostic.DEFAULT_ROOT, environment, RETAINED_REPORT['plan'], RETAINED_REPORT['limits'])
            self.assertEqual(runner.evaluate_saved(diagnostic.DEFAULT_ROOT)['verdict'], 'inconclusive')

    def test_raw_corruption_and_rejected_unverified_prefix_are_not_a_diagnosis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'history'; shutil.copytree(diagnostic.DEFAULT_ROOT, root)
            response = next(root.glob('documents/*/runs/*/2/stdout.txt'))
            response.write_text(response.read_text()+' ')
            with self.assertRaisesRegex(diagnostic.ArtifactVerificationError, 'raw_index_mismatch'):
                diagnostic.diagnose(root)
        with patch.object(diagnostic, 'evaluate_saved', return_value={'verdict': 'rejected', 'unverified_other_records': True, 'reasons': ['artifact_verification_failed']}):
            with self.assertRaisesRegex(diagnostic.ArtifactVerificationError, 'historical_verification_failed'):
                diagnostic.diagnose()

    def test_prompt_and_checkpoint_linkage_are_rechecked_after_strict_verifier(self):
        original_read = diagnostic._read
        def altered(path, *args):
            value = original_read(path, *args)
            if Path(path).name == 'extraction-checkpoint.json':
                value['outputs'][1]['claims'][0]['text'] += ' changed'
            return value
        with patch.object(diagnostic, '_read', side_effect=altered):
            with self.assertRaisesRegex(diagnostic.ArtifactVerificationError, 'production_checkpoint_linkage'):
                diagnostic.diagnose()
        original_text = Path.read_text
        def altered_prompt(path, *args, **kwargs):
            value = original_text(path, *args, **kwargs)
            return value+' ' if path.name == 'prompt.txt' else value
        with patch.object(Path, 'read_text', altered_prompt):
            with self.assertRaisesRegex(diagnostic.ArtifactVerificationError, 'prompt_chunk_linkage'):
                diagnostic.diagnose()

    def test_report_identity_is_pinned(self):
        with patch.object(diagnostic, 'file_index', return_value={'report.json': {'sha256': '0'*64}}):
            with self.assertRaisesRegex(diagnostic.ArtifactVerificationError, 'report_identity_mismatch'):
                diagnostic.diagnose()


class RetainedCopyTampering(unittest.TestCase):
    def _outcomes(self, tamper):
        """Copy the 768 root, apply tamper, rebuild index.json, and return the diagnose() message
        (None if it returns), the evaluate_saved verdict, and verify_manifest's (type, str) or None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'history'; shutil.copytree(diagnostic.DEFAULT_ROOT, root)
            tamper(root)
            atomic_json(root/'index.json', file_index(root, excluded=('index.json', 'evaluation.json')))
            try:
                diagnostic.diagnose(root); diagnosis = None
            except diagnostic.ArtifactVerificationError as exc:
                diagnosis = str(exc)
            verdict = runner.evaluate_saved(root)['verdict']
            report = json.loads((root/'report.json').read_text(encoding='utf-8'))
            try:
                runner.verify_manifest(root, report['environment'], report['plan'], report['limits']); manifest = None
            except (ValueError, KeyError, OSError) as exc:
                manifest = (type(exc), str(exc))
            return diagnosis, verdict, manifest

    @staticmethod
    def _rewrite_environment(root, change):
        """Apply change to the environment in report.json and to environment.json."""
        report = json.loads((root/'report.json').read_text(encoding='utf-8')); change(report['environment'])
        (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        environment = json.loads((root/'environment.json').read_text(encoding='utf-8')); change(environment)
        atomic_json(root/'environment.json', environment)

    def test_service_py_copy_with_a_comment_and_its_hash_updated_in_report_and_environment_json_gives_report_identity_mismatch_rejected_and_no_verify_manifest_error(self):
        def tamper(root):
            retained = root/'implementation/verinote/service.py'
            retained.write_bytes(retained.read_bytes()+b'# c\n'); value = diagnostic._hash(retained)
            self._rewrite_environment(root, lambda environment: environment['source_hashes'].__setitem__('verinote/service.py', value))
        # diagnose() rejects this edit through its REPORT_SHA256 check; verify_manifest raises nothing
        # and evaluate_saved returns verdict rejected.
        self.assertEqual(self._outcomes(tamper), ('report_identity_mismatch', 'rejected', None))

    @staticmethod
    def _append(path, data):
        path.write_bytes(path.read_bytes()+data)

    @staticmethod
    def _add_copy_and_key(root, key):
        """Write a retained copy at implementation/<key> and add its hash under key to both JSONs."""
        retained = root/'implementation'/key; retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text('"""x"""\n', encoding='utf-8'); value = diagnostic._hash(retained)
        def add(environment):
            environment['source_hashes'] = dict(sorted({**environment['source_hashes'], key: value}.items()))
        RetainedCopyTampering._rewrite_environment(root, add)

    def test_service_py_copy_with_one_appended_byte_gives_historical_verification_failed_inconclusive_and_manifest_source_hash(self):
        self.assertEqual(self._outcomes(lambda root: self._append(root/'implementation/verinote/service.py', b'#')), ('historical_verification_failed', 'inconclusive', (ValueError, 'manifest_source_hash')))

    def test_contract_copy_with_an_appended_newline_gives_historical_verification_failed_inconclusive_and_manifest_source_hash(self):
        self.assertEqual(self._outcomes(lambda root: self._append(root/'implementation'/runner.CONTRACT_PATH, b'\n')), ('historical_verification_failed', 'inconclusive', (ValueError, 'manifest_source_hash')))

    def test_deleted_service_py_copy_gives_historical_verification_failed_inconclusive_and_manifest_source_completeness(self):
        self.assertEqual(self._outcomes(lambda root: (root/'implementation/verinote/service.py').unlink()), ('historical_verification_failed', 'inconclusive', (ValueError, 'manifest_source_completeness')))

    def test_added_zz_extra_py_copy_gives_historical_verification_failed_inconclusive_and_manifest_source_completeness(self):
        self.assertEqual(self._outcomes(lambda root: (root/'implementation/verinote/zz_extra.py').write_text('"""x"""\n', encoding='utf-8')), ('historical_verification_failed', 'inconclusive', (ValueError, 'manifest_source_completeness')))

    def test_report_json_with_an_appended_space_gives_report_identity_mismatch_rejected_and_no_verify_manifest_error(self):
        self.assertEqual(self._outcomes(lambda root: self._append(root/'report.json', b' ')), ('report_identity_mismatch', 'rejected', None))

    def test_four_json_source_with_its_first_character_changed_and_its_manifest_sha256_updated_gives_historical_verification_failed_inconclusive_and_manifest_dataset(self):
        def tamper(root):
            case = json.loads((root/'fixtures/four.json').read_text(encoding='utf-8'))
            case['source'] = ('X' if case['source'][0] != 'X' else 'Y')+case['source'][1:]
            (root/'fixtures/four.json').write_text(json.dumps(case, ensure_ascii=False), encoding='utf-8')
            manifest = json.loads((root/'fixtures/manifest.json').read_text(encoding='utf-8'))
            next(entry for entry in manifest['cases'] if entry['id'] == 'four')['sha256'] = sha(case)
            (root/'fixtures/manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self._outcomes(tamper), ('historical_verification_failed', 'inconclusive', (ValueError, 'manifest_dataset')))

    def test_second_call_stdout_with_an_appended_space_gives_historical_verification_failed_inconclusive_and_no_verify_manifest_error(self):
        self.assertEqual(self._outcomes(lambda root: self._append(next(root.glob('documents/*/runs/*/2/stdout.txt')), b' ')), ('historical_verification_failed', 'inconclusive', None))

    # Accepted loss (#792) in the contract-sentence, deleted-copies and added-zz_extra tests below: verify_manifest
    # raises nothing for this edit of a retained copy together with both JSONs, and evaluate_saved returns verdict
    # rejected. diagnose() rejects it through its REPORT_SHA256 check.
    def test_contract_copy_with_a_sentence_and_both_contract_hashes_updated_in_report_and_environment_json_gives_report_identity_mismatch_rejected_and_no_verify_manifest_error(self):
        def tamper(root):
            retained = root/'implementation'/runner.CONTRACT_PATH
            self._append(retained, '추가 문장.\n'.encode()); value = diagnostic._hash(retained)
            def change(environment):
                environment['contract_sha256'] = value; environment['source_hashes'][runner.CONTRACT_PATH] = value
            self._rewrite_environment(root, change)
        self.assertEqual(self._outcomes(tamper), ('report_identity_mismatch', 'rejected', None))

    def test_service_py_copy_or_the_verinote_copies_deleted_with_their_keys_from_report_and_environment_json_gives_report_identity_mismatch_rejected_and_no_verify_manifest_error(self):
        modules = [p for p in RETAINED_REPORT['environment']['source_hashes'] if p.startswith('verinote/')]
        for label, keys in (('verinote/service.py', ['verinote/service.py']), ('every recorded verinote/ key', modules)):
            def tamper(root, keys=keys):
                for key in keys: (root/'implementation'/key).unlink()
                def change(environment):
                    for key in keys: del environment['source_hashes'][key]
                self._rewrite_environment(root, change)
            with self.subTest(label):
                self.assertEqual(self._outcomes(tamper), ('report_identity_mismatch', 'rejected', None))

    def test_added_zz_extra_py_copy_and_key_in_report_and_environment_json_gives_report_identity_mismatch_rejected_and_no_verify_manifest_error(self):
        self.assertEqual(self._outcomes(lambda root: self._add_copy_and_key(root, 'verinote/zz_extra.py')), ('report_identity_mismatch', 'rejected', None))

    def test_each_non_verinote_key_deleted_with_its_copy_from_report_and_environment_json_gives_report_identity_mismatch_inconclusive_and_manifest_source_completeness(self):
        fixed = [p for p in RETAINED_REPORT['environment']['source_hashes'] if not p.startswith('verinote/')]
        self.assertEqual(set(fixed), set(runner.FIXED_SOURCE_PATHS))
        for key in fixed:
            def tamper(root, key=key):
                (root/'implementation'/key).unlink()
                self._rewrite_environment(root, lambda environment: environment['source_hashes'].pop(key))
            with self.subTest(key):
                self.assertEqual(self._outcomes(tamper), ('report_identity_mismatch', 'inconclusive', (ValueError, 'manifest_source_completeness')))

    def test_contract_sha256_set_to_ones_in_report_and_environment_json_gives_report_identity_mismatch_inconclusive_and_manifest_contract_linkage(self):
        self.assertEqual(self._outcomes(lambda root: self._rewrite_environment(root, lambda environment: environment.__setitem__('contract_sha256', '1'*64))), ('report_identity_mismatch', 'inconclusive', (ValueError, 'manifest_contract_linkage')))

    def test_copy_and_key_in_report_and_environment_json_outside_the_module_glob_and_fixed_paths_gives_report_identity_mismatch_inconclusive_and_manifest_source_completeness(self):
        # Dropping the name match fails verinote/zz.md alone; PurePath.match, which matches from the right, fails
        # docs/verinote/zz.py alone; dropping the parent match fails scripts/zz.py, verinote/web/zz.py and docs/verinote/zz.py.
        for key in ('docs/zz.md', 'scripts/zz.py', 'verinote/web/zz.py', 'verinote/zz.md', 'docs/verinote/zz.py'):
            with self.subTest(key):
                outcome = self._outcomes(lambda root, key=key: self._add_copy_and_key(root, key))
                self.assertEqual(outcome, ('report_identity_mismatch', 'inconclusive', (ValueError, 'manifest_source_completeness')))


SPY = r"""
import builtins, fnmatch, io, json, os, os.path
from pathlib import Path

events, state = [], {'phase': None, 'inside': 0}

def spy(owner, attr, label):
    original = getattr(owner, attr)
    def wrapper(*args, **kwargs):
        if state['phase'] is not None and not state['inside']:
            events.append((state['phase'], label, args[0] if args else kwargs.get('file', kwargs.get('path', '.'))))
        return original(*args, **kwargs)
    setattr(owner, attr, wrapper)

def uncounted(owner, attr):
    original = getattr(owner, attr)
    def wrapper(*args, **kwargs):
        state['inside'] += 1
        try: return original(*args, **kwargs)
        finally: state['inside'] -= 1
    setattr(owner, attr, wrapper)

for attr in ('open', 'scandir', 'listdir', 'stat', 'lstat', 'access', 'readlink'): spy(os, attr, 'os.'+attr)
spy(io, 'open', 'io.open'); spy(builtins, 'open', 'builtins.open')
for attr in ('open', 'read_bytes', 'read_text', 'glob', 'rglob', 'iterdir', 'stat', 'lstat', 'exists', 'is_file', 'is_dir', 'is_symlink'):
    spy(Path, attr, 'Path.'+attr)
uncounted(os.path, 'realpath'); uncounted(Path, 'resolve')

state['phase'] = 'import'
from verinote.diagnostics import extraction_fidelity as efid
from verinote import extraction_threshold_ab_runner as runner
errors = {}
for phase, call in (('diagnose', efid.diagnose), ('evaluate_saved', lambda: runner.evaluate_saved(efid.DEFAULT_ROOT))):
    state['phase'] = phase
    try: call()
    except Exception as exc: errors[phase] = repr(exc)
    finally: state['phase'] = None

from verinote.extraction_evaluation import DATA_ROOT
repo, root = efid.REPO, efid.DEFAULT_ROOT
recorded = {repo/p for p in json.loads((root/'report.json').read_text(encoding='utf-8'))['environment']['source_hashes']}
v2 = Path(os.path.realpath(DATA_ROOT/'v2'))
allowed = {repo/p for p in efid.DIAGNOSTIC_PATHS} | {v2} | set(v2.glob('*.json'))
module_dir = repo/'verinote'
def counted(phase, path):
    if path == root or root in path.parents: return False
    if phase == 'import':
        return path in recorded or path in (repo, module_dir) or (path.parent == module_dir and fnmatch.fnmatchcase(path.name, '*.py'))
    return path not in allowed
out = {'import': set(), 'diagnose': set(), 'evaluate_saved': set()}
for phase, label, arg in events:
    try: path = Path(os.path.realpath(os.path.abspath(os.fsdecode(arg))))
    except TypeError: out[phase].add((label, type(arg).__name__)); continue
    if counted(phase, path): out[phase].add((label, path.relative_to(repo).as_posix() if repo in path.parents or path == repo else str(path)))
print(json.dumps({'errors': errors, 'counted': {k: sorted(v) for k, v in out.items()}}))
"""


class CheckoutAccess(unittest.TestCase):
    # It counts calls of the functions patched above made outside realpath and resolve. It does not see other
    # functions, for example io.open_code, sqlite3 URI opens and importlib's own posix calls, or subprocesses.
    @classmethod
    def setUpClass(cls):
        cls.child = subprocess.run([sys.executable, '-c', SPY], cwd=diagnostic.REPO, env=os.environ | {'PYTHONPATH': str(diagnostic.REPO)},
                                   capture_output=True, text=True, timeout=300)

    def result(self):
        self.assertEqual(self.child.returncode, 0, self.child.stderr)
        result = json.loads(self.child.stdout.splitlines()[-1])
        self.assertEqual(result['errors'], {})
        return result['counted']

    def test_fresh_interpreter_spy_counts_no_read_or_stat_of_a_recorded_path_the_repo_root_verinote_or_a_verinote_py_shaped_path_during_import(self):
        self.assertEqual(self.result()['import'], [])

    def test_fresh_interpreter_spy_counts_no_read_or_stat_of_checkout_paths_outside_the_768_root_and_allowlist_during_diagnose_and_evaluate_saved(self):
        counted = self.result()
        self.assertEqual((counted['diagnose'], counted['evaluate_saved']), ([], []))
