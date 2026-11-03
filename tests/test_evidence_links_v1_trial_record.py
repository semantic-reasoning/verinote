"""#785 evidence_links_v1 trial record: offline verdict re-derivation and the retained-v1 assessment."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import scripts.assess_extraction_fidelity_trial as S
from verinote.diagnostics import extraction_fidelity_artifacts as A
from verinote.diagnostics import product_fitness_v1 as pf
from verinote.extraction_ab_runner import file_index

LIVE_READS = {'verinote/diagnostics/extraction_fidelity_candidate.py', 'verinote/benchmark_data/v2/manifest.json',
              'verinote/benchmark_data/v2/short.json', 'verinote/benchmark_data/v2/three.json'}
EVENTS = ('open', 'os.listdir', 'os.scandir', 'sqlite3.connect')
LAUNCHES = ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'socket.connect')


class Reads:
    """A sys.addaudithook hook (added once), so it sees audit events raised in this interpreter only. While recorded() is active it
    records, from any thread, the first argument of EVENTS events and, in launches, the names of LAUNCHES events."""
    hooked, active, names, launches = False, False, [], []

    @classmethod
    def hook(cls, event, args):
        if cls.active and event in EVENTS and args: cls.names.append((event, args[0]))
        if cls.active and event in LAUNCHES: cls.launches.append(event)

    @classmethod
    @contextlib.contextmanager
    def recorded(cls):
        if not cls.hooked: sys.addaudithook(cls.hook); cls.hooked = True
        names = cls.names = []; cls.launches = []; paths: set = set(); cls.active = True
        try: yield paths
        finally:
            cls.active = False
            for event, name in names:
                if not isinstance(name, (str, bytes, os.PathLike)): paths.add(f'{event}:{type(name).__name__}'); continue
                name = os.fsdecode(name)
                if event == 'sqlite3.connect' and name.startswith('file:'): name = unquote(name[len('file:'):].split('?')[0])
                paths.add(Path(os.path.abspath(name)).resolve())


def outside(paths, *roots):
    """paths under none of roots, checkout-relative inside S.REPO and absolute elsewhere; non-path event markers unchanged."""
    roots = tuple(Path(r).resolve() for r in roots)
    return {p if not isinstance(p, Path) else p.relative_to(S.REPO).as_posix() if p.is_relative_to(S.REPO) else str(p)
            for p in paths if not (isinstance(p, Path) and any(p.is_relative_to(r) for r in roots))}


class Recorder(unittest.TestCase):
    def test_a_read_only_sqlite_uri_is_recorded_as_its_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp)/'a b é.sqlite3'; sqlite3.connect(database).close()
            with Reads.recorded() as paths: sqlite3.connect(database.as_uri()+'?mode=ro', uri=True).close()
            self.assertIn(database.resolve(), paths)

    def test_os_listdir_on_a_worker_thread_and_os_scandir_are_each_recorded_as_the_resolved_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            def worker_listdir(path): worker = threading.Thread(target=os.listdir, args=(path,)); worker.start(); worker.join()
            for event, listing in (('os.listdir', worker_listdir), ('os.scandir', lambda path: os.scandir(path).close())):
                with Reads.recorded() as paths: listing(tmp)
                with self.subTest(event=event): self.assertIn(Path(tmp).resolve(), paths)

    def test_worker_thread_popen_os_system_posix_spawn_spawnv_forkpty_and_socket_connect_are_recorded_as_launches(self):
        with Reads.recorded():
            worker = threading.Thread(target=subprocess.run, args=([sys.executable, '-c', ''],), kwargs={'check': True})
            worker.start(); worker.join()
            os.system(':')
            os.waitpid(os.posix_spawn(sys.executable, [sys.executable, '-c', ''], os.environ), 0)
            os.spawnv(os.P_WAIT, sys.executable, [sys.executable, '-c', ''])
            pid, fd = os.forkpty()
            if pid == 0: os._exit(0)
            os.waitpid(pid, 0); os.close(fd)
            with socket.socket() as server, socket.socket() as client:
                server.bind(('127.0.0.1', 0)); server.listen(); client.connect(server.getsockname())
        self.assertEqual(set(Reads.launches), {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'socket.connect'})
        self.assertEqual(set(Reads.launches), set(LAUNCHES))


class TrialRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = S.build()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def copy(self, source=None, name='trial'):
        root = Path(self.tmp.name)/name; shutil.copytree(source or S.TRIAL_ROOT, root); return root

    def forged(self, report_edit=None, evaluation_edit=None, finalization=None):
        """A copy whose JSON edits are re-indexed and re-pinned consistently."""
        root = self.copy()
        for name, edit in (('report.json', report_edit), ('evaluation.json', evaluation_edit), ('finalization.json', finalization)):
            if edit is None: continue
            value = json.loads((root/name).read_bytes()); edit(value); (root/name).write_text(json.dumps(value), encoding='utf-8')
        index = {k: v for k, v in file_index(root).items() if k not in ('index.json', 'evaluation.json')}
        (root/'index.json').write_text(json.dumps(index), encoding='utf-8')
        files = file_index(root)
        return root, patch.multiple(S, INDEX_SHA256=files['index.json']['sha256'], EVALUATION_SHA256=files['evaluation.json']['sha256'])

    def test_committed_report_regenerates_byte_identically(self):
        committed = S.REPORT_PATH.read_bytes()
        self.assertEqual(pf.render(self.report).encode('utf-8'), committed)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout): self.assertEqual(S.main([]), 0)
        self.assertEqual(stdout.getvalue().encode('utf-8'), committed)

    def test_an_appended_or_extra_trial_file_or_a_reserialized_report_json_raises_trial_raw_index(self):
        for tamper in ('implementation', 'extra', 'report.json'):
            with self.subTest(tamper=tamper):
                root = self.copy(name='trial-'+tamper)
                if tamper == 'implementation':
                    with (root/'implementation/verinote/ontology.py').open('a', encoding='utf-8') as f: f.write('#')
                elif tamper == 'extra': (root/'notes.txt').write_text('x', encoding='utf-8')
                else: (root/'report.json').write_text(json.dumps(json.loads((root/'report.json').read_bytes())), encoding='utf-8')
                with self.assertRaisesRegex(S.TrialRecordError, '^trial_raw_index$'): S.build(root)

    def test_index_pin_rejects_a_consistently_reindexed_copy(self):
        root = self.copy()
        with (root/'implementation/verinote/ontology.py').open('a', encoding='utf-8') as f: f.write('#')
        index = {k: v for k, v in file_index(root).items() if k not in ('index.json', 'evaluation.json')}
        (root/'index.json').write_text(json.dumps(index), encoding='utf-8')
        with self.assertRaisesRegex(S.TrialRecordError, '^trial_index_identity$'): S.build(root)

    def test_evaluation_pin_rejects_a_reformatted_evaluation(self):
        # Same JSON value, different bytes: the verdict linkage compares values only.
        root = self.copy()
        (root/'evaluation.json').write_text(json.dumps(json.loads((root/'evaluation.json').read_bytes())), encoding='utf-8')
        with self.assertRaisesRegex(S.TrialRecordError, '^trial_evaluation_identity$'): S.build(root)

    def test_a_failed_document_verification_raises_trial_verdict_linkage(self):
        failed = {'complete': True, 'passed': False, 'violations': ['publication_contract']}
        with patch.object(S, 'verify_document', return_value=failed), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_verdict_linkage$'): S.build()

    def test_a_forged_quality_summary_raises_quality_mismatch(self):
        def edit(value): value['documents'][0]['quality']['validated_chunks'] -= 1
        def edit_evaluation(value): value['quality']['validated_chunks'] -= 1
        root, pins = self.forged(edit, edit_evaluation)
        with pins, self.assertRaisesRegex(ValueError, '^quality_mismatch$'): S.build(root)

    def test_a_forged_budget_raises_budget_summary(self):
        def edit(value): value['budget']['observed_tokens_lower_bound'] += 1
        def edit_evaluation(value): value['observed_tokens'] += 1
        root, pins = self.forged(edit, edit_evaluation)
        with pins, self.assertRaisesRegex(ValueError, '^budget_summary$'): S.build(root)

    def test_forged_finalization_and_terminal_accounting_leave_the_verdict_unchanged(self):
        forgeries = {'finalization': {'finalization': lambda value: value.update(complete=False)},
                     'terminal accounting': {'report_edit': lambda value: value.update(final_write_tail='x')}}
        for name, edits in forgeries.items():
            with self.subTest(check=name):
                root, pins = self.forged(**edits)
                with pins: self.assertEqual(S.build(root)['verdict'], self.report['verdict'])
                shutil.rmtree(root)

    def test_a_changed_compile_prompt_raises_candidate_prompt_or_sidecar_mismatch(self):
        with patch.object(A, 'compile_prompt', side_effect=lambda prompt: prompt+'\n'), \
             self.assertRaisesRegex(ValueError, '^candidate prompt or sidecar mismatch$'): S.build()

    def test_a_changed_parsed_output_raises_trial_checkpoint_linkage(self):
        real = S.parse_response
        def changed(agent, answer):
            value, normalizations = real(agent, answer)
            value = copy.deepcopy(value); value['claims'][0]['text'] += '.'
            return value, normalizations
        with patch.object(S, 'parse_response', side_effect=changed), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_checkpoint_linkage$'): S.build()

    def test_a_dropped_v2_error_raises_trial_v2_linkage(self):
        real = S.assess_outputs
        def dropped(case, outputs):
            value = real(case, outputs); value['finite_gold']['annotation_errors'].pop(); return value
        with patch.object(S, 'assess_outputs', side_effect=dropped), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_v2_linkage$'): S.build()

    def test_a_changed_registry_case_sha_raises_trial_registry_case_identity(self):
        registry = pf.load_registry(S.CRITERIA_ROOT); registry['cases']['three']['case_sha256'] = '0'*64
        with patch.object(pf, 'load_registry', return_value=registry), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_registry_case_identity$'): S.build()

    def test_a_changed_768_report_pin_raises_trial_768_identity(self):
        with patch.object(S, 'REPORT_768_SHA256', '0'*64), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_768_identity$'): S.build()

    def test_a_changed_768_case_sha_raises_trial_768_case_identity(self):
        root = self.copy(S.ROOT_768, '768')
        value = json.loads((root/'report.json').read_bytes()); value['environment']['cases']['three'] = '0'*64
        (root/'report.json').write_text(json.dumps(value), encoding='utf-8')
        with patch.object(S, 'REPORT_768_SHA256', file_index(root)['report.json']['sha256']), \
             self.assertRaisesRegex(S.TrialRecordError, '^trial_768_case_identity$'): S.build(root_768=root)

    def test_an_appended_768_run_file_raises_trial_768_file_identity(self):
        for name in ('1/stdout.txt', 'extraction-checkpoint.json', '1/prompt.txt'):
            with self.subTest(name=name):
                root = self.copy(S.ROOT_768, '768')
                row = json.loads((root/'report.json').read_bytes())['documents'][0]
                with (root/row['artifact']/'runs'/row['run_id']/name).open('a', encoding='utf-8') as f: f.write(' ')
                with self.assertRaisesRegex(S.TrialRecordError, '^trial_768_file_identity$'): S.build(root_768=root)
                shutil.rmtree(root)

    def test_a_reformatted_criteria_copy_raises_trial_criteria_identity(self):
        for name in S.CRITERIA_SHA256:
            with self.subTest(name=name):
                root = self.copy(S.CRITERIA_ROOT, 'criteria-'+name)
                (root/name).write_text(json.dumps(json.loads((root/name).read_bytes())), encoding='utf-8')
                with self.assertRaisesRegex(S.TrialRecordError, '^trial_criteria_identity$'): S.build(criteria=root)

    def test_build_is_unchanged_with_edited_criteria_served_for_the_live_paths(self):
        # pf._load serves edited retained copies for the live paths, so the stimulus does not depend on what the live files hold.
        real = pf._load
        spec, registry = real(S.CRITERIA_ROOT/'spec.json'), copy.deepcopy(real(S.CRITERIA_ROOT/'registry.json'))
        three = registry['cases']['three']['chunks']
        next(c for c in three[0]['coverage'] if c['predicate'] == 'archives')['expected_status'] = 'extracted'
        three[2]['gap_pairs'].remove(['qualifier_not_representable', 'historical'])
        live = {pf.CRITERIA_ROOT/'spec.json': pf.with_policy(spec, 'P-STATUS-HISTORICAL', 'historical_no_evidence_linked'),
                pf.CRITERIA_ROOT/'registry.json': registry}
        with patch.object(pf, '_load', side_effect=lambda path: copy.deepcopy(live[Path(path)]) if Path(path) in live else real(path)):
            self.assertNotEqual(pf.load_spec(), pf.load_spec(S.CRITERIA_ROOT))
            self.assertNotEqual(pf.load_registry(), pf.load_registry(S.CRITERIA_ROOT))
            self.assertEqual(S.build(), self.report)

    def test_reads_recorded_during_build_outside_the_retained_records_are_exactly_the_named_live_files(self):
        # Runs after setUpClass's build, so modules a first build imports lazily are already loaded.
        with Reads.recorded() as paths: self.assertEqual(S.build(), self.report)
        self.assertEqual(outside(paths, S.TRIAL_ROOT, S.CRITERIA_ROOT, S.ROOT_768), LIVE_READS)
        self.assertEqual(outside({'open:int'}, S.REPO), {'open:int'})

    def test_evaluate_is_inconclusive_under_each_listed_checkout_dataset_and_binary_drift(self):
        env = json.loads((S.TRIAL_ROOT/'report.json').read_bytes())['environment']; binary = Path(env['transport']['binary'])
        real_is_file, real_hash = Path.is_file, A._hash
        def evaluate(drift=lambda repo, data: None, binary_sha256=env['binary_sha256'], present=True):
            work = Path(tempfile.mkdtemp(dir=self.tmp.name)); repo, data = work/'repo', work/'data'
            shutil.copytree(S.TRIAL_ROOT/'implementation', repo); shutil.copytree(S.TRIAL_ROOT/'fixtures', data/'v2')
            drift(repo, data)
            def is_file(path): return present if path == binary else real_is_file(path)
            def hashed(path): return binary_sha256 if Path(path) == binary else real_hash(path)
            with patch.multiple(A, REPO=repo, DATA_ROOT=data, _hash=hashed), patch.object(Path, 'is_file', is_file):
                return A.evaluate(S.TRIAL_ROOT)
        def append(repo, data):
            with (repo/'verinote/service.py').open('a', encoding='utf-8') as f: f.write('# drift\n')
        def approve(repo, data):
            manifest = json.loads((data/'v2/manifest.json').read_bytes()); manifest['human_review'] = {'status': 'approved', 'evidence': 'x'}
            (data/'v2/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with Reads.recorded() as paths: self.assertEqual(evaluate(), self.report['verdict'])
        self.assertEqual(outside(paths, S.TRIAL_ROOT, self.tmp.name), set())
        drifts = {'checkout file': {'drift': append}, 'checkout module added': {'drift': lambda repo, data: (repo/'verinote/zz_new.py').write_text('')},
                  'dataset manifest': {'drift': approve}, 'binary bytes': {'binary_sha256': '0'*64}, 'binary absent': {'present': False}}
        for name, drift in drifts.items():
            with self.subTest(drift=name):
                self.assertEqual(evaluate(**drift), {'verdict': 'inconclusive', 'reasons': ['artifact_verification_failed']})

    def test_each_listed_live_manifest_or_case_change_exits_2_with_empty_stdout(self):
        real = pf.load_manifest
        def case_hash(case_id):
            def changed(path):
                value = copy.deepcopy(real(path))
                for row in value['cases']:
                    if row['id'] == case_id: row['sha256'] = '0'*64
                return value
            return patch.object(pf, 'load_manifest', side_effect=changed), 'product_fitness_registry_case_identity'
        def manifest_key():
            data = self.copy(S.TRIAL_ROOT/'fixtures', 'data/v2').parent
            manifest = json.loads((data/'v2/manifest.json').read_bytes()); manifest['added'] = None
            (data/'v2/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            return patch.object(pf, 'DATA_ROOT', data), '평가 manifest 오류'
        def non_object_case():
            data = self.copy(S.TRIAL_ROOT/'fixtures', 'non-object/v2').parent
            (data/'v2/short.json').write_text('[]', encoding='utf-8')
            manifest = json.loads((data/'v2/manifest.json').read_bytes())
            next(row for row in manifest['cases'] if row['id'] == 'short')['sha256'] = hashlib.sha256(b'[]').hexdigest()
            (data/'v2/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            return patch.object(pf, 'DATA_ROOT', data), 'has no attribute'
        for name, change in (('short hash', lambda: case_hash('short')), ('three hash', lambda: case_hash('three')), ('manifest key', manifest_key),
                             ('non-object case', non_object_case)):
            stdout, stderr = io.StringIO(), io.StringIO(); changed, message = change()
            with self.subTest(change=name), changed, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                 self.assertRaises(SystemExit) as raised:
                S.main([])
            self.assertEqual((raised.exception.code, stdout.getvalue()), (2, ''))
            self.assertIn(message, stderr.getvalue())

    def test_main_exits_2_with_empty_stdout_and_trial_index_identity_on_stderr_with_the_index_pin_changed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(S, 'INDEX_SHA256', '0'*64), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
             self.assertRaises(SystemExit) as raised:
            S.main([])
        self.assertEqual((raised.exception.code, stdout.getvalue()), (2, ''))
        self.assertIn('trial_index_identity', stderr.getvalue())

    def test_script_calls_pf_assess_and_pf_map_errors_has_no_Name_or_Attribute_node_add_argument_and_rejects_a_positional_argument(self):
        tree = ast.parse(Path(S.__file__).read_text(encoding='utf-8'))
        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == 'pf'}
        self.assertLessEqual({'assess', 'map_errors'}, called)
        self.assertNotIn('add_argument', {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised: S.main(['x'])
        self.assertEqual(raised.exception.code, 2); self.assertIn('unrecognized arguments: x', stderr.getvalue())

    def test_a_changed_live_candidate_module_hash_raises_candidate_prompt_or_sidecar_mismatch(self):
        real = A._hash; candidate = A.REPO/'verinote/diagnostics/extraction_fidelity_candidate.py'
        with patch.object(A, '_hash', side_effect=lambda path: '0'*64 if Path(path) == candidate else real(path)), \
             self.assertRaisesRegex(ValueError, '^candidate prompt or sidecar mismatch$'):
            S.build()

    def test_build_returns_the_report_with_popen_posixsubprocess_fork_exec_and_socket_socket_patched_to_raise_and_reads_records_no_launch_during_build(self):
        def blocked(*args, **kwargs): raise AssertionError('process or network use')
        with patch.object(subprocess, 'Popen', side_effect=blocked), patch.object(socket, 'socket', side_effect=blocked), \
             patch('_posixsubprocess.fork_exec', side_effect=blocked):
            self.assertEqual(S.build(), self.report)
        with Reads.recorded(): self.assertEqual(S.build(), self.report)
        self.assertEqual(Reads.launches, [])

    def test_policy_flips_move_the_errors_their_fields_predict(self):
        errors = self.report['v2_annotation_errors']
        def ids(predicate): return {f'error-{i}' for i, e in enumerate(errors, 1) if predicate(e)}
        archives = ids(lambda e: e['reason'] == 'coverage_semantics' and e.get('predicate') == 'archives')
        gaps = ids(lambda e: e['reason'] == 'gap_completeness')
        rows = {(r['policy'], r['decision'], r['label']): r for r in self.report['v1']['sensitivity']}
        self.assertEqual({k for k, r in rows.items() if r['executable'] and r['moved_errors']},
                         {('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None),
                          ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'A'),
                          ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'B')})
        self.assertEqual({m['id'] for m in rows[('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None)]['moved_errors']}, archives)
        for label in ('A', 'B'):
            self.assertEqual({m['id'] for m in rows[('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', label)]['moved_errors']}, gaps)
        self.assertTrue(archives and gaps)

    def test_combined_flips_move_the_union_of_single_flips(self):
        single = {(r['policy'], r['decision'], r['label']): {m['id'] for m in r.get('moved_errors', [])} for r in self.report['v1']['sensitivity']}
        for row in self.report['v1']['post_hoc_combinations']:
            with self.subTest(policies=row['policies']):
                expected = set().union(*(single[(p['policy'], p['decision'], p['label'])] for p in row['policies']))
                self.assertEqual(set(row['moved_errors']), expected)


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def calls(function):
    """Name ids and Attribute attrs of the Calls under function, including its own decorators, defaults and annotations, but nothing under
    a SCOPES node, including that node's decorators and defaults."""
    names, stack = set(), list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            names.add(node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', None))
        if not isinstance(node, SCOPES): stack.extend(ast.iter_child_nodes(node))
    return names-{None}


def functions(module):
    return {n.name: n for n in ast.parse(Path(module.__file__).read_text(encoding='utf-8')).body if isinstance(n, ast.FunctionDef)}


def verdict_chain():
    """(names S.rederive_verdict calls directly that A.evaluate or A.verify_trial also calls directly, names in functions(A) that A.evaluate or
    A.verify_trial calls directly and S.rederive_verdict does not)."""
    defined, script = functions(A), functions(S)
    chain = calls(defined['evaluate']) | calls(defined['verify_trial'])
    kept = calls(script['rederive_verdict']) & chain
    return kept, (chain & set(defined))-kept


class ChainDerivation(unittest.TestCase):
    def test_rederivation_docstring_names_exactly_the_omitted_directly_called_A_functions(self):
        _, omitted = verdict_chain()
        named = {name for name in functions(A) if re.search(rf'\b{name}\b', S.rederive_verdict.__doc__)}
        # The docstring names evaluate as the owner of the chain.
        self.assertEqual(named-{'evaluate'}, omitted)
        self.assertTrue(omitted)

    def test_script_has_no_identifier_evaluate_verify_trial_verify_manifest_sources__authenticate_current_or_diagnose(self):
        tree = ast.parse(Path(S.__file__).read_text(encoding='utf-8'))
        identifiers = {part for node in ast.walk(tree) if not isinstance(node, ast.Constant) for field, value in ast.iter_fields(node)
                       if field != 'type_comment' for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)
                       for part in item.split('.')}
        self.assertEqual(identifiers & {'verify_manifest', 'verify_trial', 'evaluate', 'sources', '_authenticate_current', 'diagnose'}, set())


class Derivations(unittest.TestCase):
    """Direct calls of compare_768 and failure_summary on constructed inputs, and of replay on the retained trial's inputs with a
    constructed repair attempt added."""
    PREFIX = 'P:'

    def inputs(self):
        env = {'cli_version': '1.0.0', 'transport': {'binary': 'b'}, 'binary_sha256': 'h', 'prompt_hashes': {'extraction': 'e', 'repair': 'r'},
               'invocation': ['a.py', 'run']}
        env_768 = {'cli_version': '1.0.0', 'transport': {'binary': 'b'}, 'prompt_sha256': 'e', 'repair_prompt_sha256': 'r', 'invocation': ['b.py', 'run']}
        error = {'chunk': 1, 'reason': 'gap_completeness'}
        mapping = {'errors': [{'id': 'error-1', 'frozen_error': error, 'causes': ['c1:x:D5']}], 'causes': [{'id': 'c1:x:D5'}]}
        mapping_768 = {'errors': [{'id': 'error-1', 'frozen_error': dict(error), 'causes': ['c1:x:D5', 'c1:y:D3']}, {'id': 'error-2', 'frozen_error': {'chunk': 2}, 'causes': ['c2:z:D6']},
                                  {'id': 'error-3', 'frozen_error': dict(error), 'causes': ['c1:w:D6']}],
                       'causes': [{'id': 'c1:x:D5'}, {'id': 'c1:y:D3'}, {'id': 'c2:z:D6'}, {'id': 'c1:w:D6'}]}
        payload = json.dumps({'approved_dictionary': {'id': 1, 'sha256': 's'}, 'text': 't'})
        prompts = [(1, self.PREFIX+payload, 'E', self.PREFIX+payload)]
        return env, env_768, self.PREFIX, prompts, mapping, mapping_768

    def compare(self, edit=None):
        values = self.inputs()
        if edit: edit(*values)
        return S.compare_768(*values)

    def test_environment_fields_follow_their_inputs(self):
        same = self.compare()
        self.assertEqual((same['cli_version_equal'], same['transport_equal'], same['base_prompt_sha256_equal'], same['binary_sha256_recorded'],
                          same['invocation_script']),
                         (True, True, {'extraction': True, 'repair': True}, {'trial': True, '768': False}, {'trial': 'a.py', '768': 'b.py'}))
        edits = [('cli_version_equal', lambda env, e68, *_: e68.update(cli_version='1.0.1'), False),
                 ('transport_equal', lambda env, e68, *_: e68.update(transport={'binary': 'c'}), False),
                 ('base_prompt_sha256_equal', lambda env, e68, *_: e68.update(repair_prompt_sha256='x'), {'extraction': True, 'repair': False}),
                 ('base_prompt_sha256_equal', lambda env, e68, *_: e68.update(prompt_sha256='x'), {'extraction': False, 'repair': True}),
                 ('binary_sha256_recorded', lambda env, e68, *_: (env.pop('binary_sha256'), e68.update(binary_sha256='h')), {'trial': False, '768': True})]
        for field, edit, expected in edits:
            with self.subTest(field=field, expected=expected): self.assertEqual(self.compare(edit)[field], expected)

    def test_prompt_fields_follow_their_inputs(self):
        self.assertEqual(self.compare()['chunks'], [{'index': 1, 'effective_prompt_equal': False, 'original_payload_differences': []}])
        def dictionary_id(env, e68, prefix, prompts, *_):
            index, original, effective, other = prompts[0]
            prompts[0] = (index, original, other, self.PREFIX+json.dumps({'approved_dictionary': {'id': 2, 'sha256': 's'}, 'text': 't'}))
        def other_prefix(env, e68, prefix, prompts, *_): prompts[0] = prompts[0][:3]+('Q:'+prompts[0][3][2:],)
        self.assertEqual(self.compare(dictionary_id)['chunks'][0]['original_payload_differences'], ['approved_dictionary.id'])
        self.assertIsNone(self.compare(other_prefix)['chunks'][0]['original_payload_differences'])
        def one_sided_key(env, e68, prefix, prompts, *_):
            prompts[0] = prompts[0][:3]+(self.PREFIX+json.dumps({'approved_dictionary': {'id': 1}, 'text': 't'}),)
        self.assertEqual(self.compare(one_sided_key)['chunks'][0]['original_payload_differences'], ['approved_dictionary.sha256'])
        def key_only_768(env, e68, prefix, prompts, *_):
            prompts[0] = prompts[0][:3]+(self.PREFIX+json.dumps({'approved_dictionary': {'id': 1, 'sha256': 's', 'version': 2}, 'text': 't'}),)
        self.assertEqual(self.compare(key_only_768)['chunks'][0]['original_payload_differences'], ['approved_dictionary.version'])
        def identical(env, e68, prefix, prompts, *_): prompts[0] = prompts[0][:2]+(prompts[0][3], prompts[0][3])
        self.assertTrue(self.compare(identical)['chunks'][0]['effective_prompt_equal'])

    def test_error_fields_follow_their_inputs(self):
        same = self.compare()
        self.assertEqual(same['annotation_errors'], {'trial': 1, '768': 3})
        self.assertEqual(same['errors'], [{'id': 'error-1', 'equal_768_errors': ['error-1', 'error-3'], 'causes': ['c1:x:D5'],
                                           'causes_768': ['c1:w:D6', 'c1:x:D5', 'c1:y:D3'], 'causes_only_trial': [], 'causes_only_768': ['c1:w:D6', 'c1:y:D3']}])
        self.assertEqual((same['causes_only_trial'], same['causes_only_768'], same['trial_causes_within_768']), ([], ['c1:w:D6', 'c1:y:D3', 'c2:z:D6'], True))
        def absent(env, e68, prefix, prompts, mapping, m68): mapping['errors'][0]['frozen_error'] = {'chunk': 3}; mapping['causes'].append({'id': 'c3:w:D4'})
        moved = self.compare(absent)
        self.assertEqual((moved['errors'][0]['equal_768_errors'], moved['errors'][0]['causes_768'], moved['errors'][0]['causes_only_trial']),
                         ([], [], ['c1:x:D5']))
        self.assertEqual((moved['causes_only_trial'], moved['trial_causes_within_768']), (['c3:w:D4'], False))

    def test_replay_returns_the_same_value_with_a_repair_attempt_added(self):
        report = json.loads((S.TRIAL_ROOT/'report.json').read_bytes()); row = report['documents'][0]; artifact = S.TRIAL_ROOT/row['artifact']
        case = S.load_case('three', S.TRIAL_ROOT/'fixtures')
        def read(relative): return (artifact/relative).read_text(encoding='utf-8')
        repair = dict(report['attempts'][0], repair=True)
        self.assertEqual(S.replay(case, row, report['attempts']+[repair], read, 'trial'), S.replay(case, row, report['attempts'], read, 'trial'))

    def test_failure_summary_follows_the_failures(self):
        failures = [{'layer': 'D3', 'reason': 'unsupported_obligation', 'kind': 'evidence'}, {'layer': 'D6', 'reason': 'status_mismatch', 'kind': 'label'}]
        self.assertEqual(S.failure_summary({'failures': failures}),
                         {'layers': ['D3', 'D6'], 'vector': [['status_mismatch', 'label'], ['unsupported_obligation', 'evidence']]})
        self.assertEqual(S.failure_summary({'failures': []}), {'layers': [], 'vector': []})


if __name__ == '__main__': unittest.main()
