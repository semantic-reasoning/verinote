"""Product-fitness v1 (#788): historical mapping, sensitivity, committed report and doc numbers."""
import ast
import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from scripts.verify_product_fitness_v1 import main
from verinote.diagnostics import extraction_fidelity as efid
from verinote.diagnostics import product_fitness_v1 as pf
from verinote.diagnostics.extraction_fidelity import (
    REPORT_SHA256,
    diagnose,
)
from verinote.extraction_ab_runner import _hash
from verinote.store import digest

DOC_PATH = pf.REPO/'docs/extraction-product-fitness-v1.md'
AUTHORED_TEST_PATH = pf.REPO/'tests/test_product_fitness_v1_authored.py'
QUOTED = r'`([A-Za-z_@][\w.@-]*)=([^`]+)`'
CATEGORIES = ('product_evidence', 'unresolved', 'representation_sensitive')
# Counter words that may not follow a number in the doc, written as regex escapes.
COUNTER_WORDS = r'\uac1c|\ud68c|\uac74|\ubc88|\uac00\uc9c0|\ubc30|\ud589|\uc30d|\uc885|\uc0ac\ub840|\uba85|%'
IDENTIFIER = re.compile(r'v[12]|v1_pass|D[1-6]|D3-D6|S[0-4]|authored-pf1')
# Direction verbs in sensitivity rows (removal, addition), written as regex escapes.
DIRECTION_VERBS = {'removed': r'\uc81c\uac70', 'added': r'\ucd94\uac00'}
PATH = re.compile(r'(\.\.?/|/)?[A-Za-z_.][\w.\-]*(/[\w.\-*]+)+/?')
FILE_DIRS = ('docs', 'verinote', 'scripts', 'tests', 'benchmarks')
EXEMPTIONS = (
    ('issue_reference', lambda token, c: re.fullmatch(r'#\d+|https://github\.com/semantic-reasoning/verinote/issues/\d+', token)),
    ('error_id', lambda token, c: re.fullmatch(r'(historical:)?error-\d+', token) and token.split(':')[-1] in c['error_ids']),
    ('repository_path', lambda token, c: '/' in token and PATH.fullmatch(token) and
     not any(re.fullmatch(r'\d+', part) for part in token.split('/'))),
    ('chunk_id', lambda token, c: token in c['chunk_ids']),
    ('cause_id', lambda token, c: token in c['cause_ids']),
    ('identifier', lambda token, c: IDENTIFIER.fullmatch(token)),
    ('json_key', lambda token, c: token in c['keys']),
    ('file_name', lambda token, c: re.search(r'\.(json|md|py)$', token) and token in c['file_names']),
)
CRITERION_FILES = ('benchmarks/extraction/criteria/product-fitness-v1/spec.json',
                'benchmarks/extraction/criteria/product-fitness-v1/registry.json',
                'benchmarks/extraction/criteria/product-fitness-v1/authored-pf1.json',
                'verinote/diagnostics/product_fitness_v1.py', 'scripts/verify_product_fitness_v1.py',
                'tests/test_product_fitness_v1.py', 'tests/test_product_fitness_v1_authored.py')


def lookup(report, path):
    value = report
    for part in path.split('.'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def rendered(value):
    """How the doc writes a report value: JSON for booleans and null, comma-joined lists, str otherwise."""
    if isinstance(value, bool) or value is None: return json.dumps(value)
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value): return ','.join(map(str, value))
    if isinstance(value, (dict, list)): raise AssertionError('doc quotes a non-scalar report value')
    return str(value)


def table_rows(doc):
    """Data rows (cells) of every Markdown table, without header and separator rows."""
    rows, lines = [], doc.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith('|') or re.fullmatch(r'\|[ \-|]+\|', line): continue
        if i+1 < len(lines) and re.fullmatch(r'\|[ \-|]+\|', lines[i+1]): continue
        rows.append([cell.strip() for cell in line.strip('|').split('|')])
    return rows


def verinote_imports(path):
    """verinote source files a module imports, resolving relative imports; lazy imports included."""
    package = path.relative_to(pf.REPO).with_suffix('').parts[:-1]; found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            if node.level: base = list(package[:len(package)-(node.level-1)])+(node.module.split('.') if node.module else [])
            elif node.module and node.module.split('.')[0] == 'verinote': base = node.module.split('.')
            else: continue
            candidates = [base]+[base+[alias.name] for alias in node.names]
        elif isinstance(node, ast.Import):
            candidates = [alias.name.split('.') for alias in node.names if alias.name.split('.')[0] == 'verinote']
        else: continue
        for parts in candidates:
            for file in (pf.REPO.joinpath(*parts).with_suffix('.py'), pf.REPO.joinpath(*parts)/'__init__.py'):
                if file.is_file(): found.add(file)
    return found


def doc_token_context(report, spec, registry):
    """Report-, spec-, registry- and repository-derived sets the exemption classes consult."""
    causes = {c['id'] for c in report['historical_mapping']['causes']}
    causes |= {c for row in report['sensitivity'] for m in row.get('moved_errors', []) for c in m['causes_added']+m['causes_removed']}
    return {'error_ids': {r['id'] for r in report['historical_mapping']['errors']},
            'chunk_ids': {f"c{r['frozen_error']['chunk']}" for r in report['historical_mapping']['errors']},
            'cause_ids': causes,
            'keys': set(json_keys(report)) | set(json_keys(spec)) | set(json_keys(registry)),
            'file_names': {p.name for d in FILE_DIRS for p in (pf.REPO/d).rglob('*') if p.is_file()}}


def _exempt_token(token, context):
    """The name of the first exemption class in EXEMPTIONS that accepts a digit-bearing doc token, else None."""
    return next((name for name, accepts in EXEMPTIONS if accepts(token, context)), None)


def json_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_keys(item)
    elif isinstance(value, list):
        for item in value: yield from json_keys(item)


def json_leaves(value):
    if isinstance(value, dict):
        for item in value.values(): yield from json_leaves(item)
    elif isinstance(value, list):
        for item in value: yield from json_leaves(item)
    else: yield value


def doc_sentences(doc):
    """Each line split after a period that is followed by whitespace and lies outside backtick code spans."""
    for line in doc.splitlines():
        masked = re.sub(r'`[^`]*`', lambda m: '`'*len(m.group()), line)
        cuts = [0]+[m.end() for m in re.finditer(r'\.\s+', masked)]+[len(line)]
        yield from (line[a:b] for a, b in zip(cuts, cuts[1:], strict=False) if line[a:b].strip())


class ProductFitnessReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = pf.load_spec(); cls.registry = pf.load_registry(); cls.diagnosis = diagnose()
        cls.report = pf.verify_proposal(); cls.mapping = cls.report['historical_mapping']
        cls.errors = cls.diagnosis['finite_gold']['annotation_errors']
        cls.obligation_ids = sorted({o['id'] for entry in cls.registry['cases'].values() for row in entry['chunks'] for o in row['obligations']})

    def test_18_historical_errors_map_once_to_unique_causes(self):
        review = json.loads(pf.REVIEW_PATH.read_text(encoding='utf-8'))['criteria_review']['errors']
        rows = self.mapping['errors']
        self.assertEqual([r['id'] for r in rows], [f'error-{i}' for i in range(1, len(self.errors)+1)])
        self.assertEqual([r['frozen_error'] for r in rows], self.errors)
        self.assertEqual([r['observations']['frozen_error'] for r in review], self.errors)
        self.assertEqual([r['id'] for r in review], [r['id'] for r in rows])
        causes = {c['id']: c for c in self.mapping['causes']}
        self.assertEqual(len(causes), len(self.mapping['causes']))
        for row in rows:
            self.assertTrue(row['causes'], row['id'])
            for cause in row['causes']: self.assertIn(row['id'], causes[cause]['error_ids'])
        for cause in causes.values():
            for error_id in cause['error_ids']: self.assertIn(cause['id'], next(r for r in rows if r['id'] == error_id)['causes'])
        counts = self.mapping['cause_counts']
        self.assertEqual(counts['total'], len(causes))
        for category in ('product_evidence', 'unresolved', 'representation_sensitive'):
            self.assertEqual(counts[category], sum(c['category'] == category for c in causes.values()))
        for chunk in sorted({e['chunk'] for e in self.errors}):
            sharing = [r['id'] for r, e in zip(rows, self.errors, strict=True) if e['chunk'] == chunk and
                       (e.get('id') == 'missing_relation' or e.get('code') == 'missing_relation' or e['reason'] == 'gap_completeness')]
            self.assertGreater(len(sharing), 1)
            self.assertEqual(causes[f'c{chunk}:missing_relation:D3']['error_ids'], sharing)
        self.assertLess(counts['total'], sum(len(r['causes']) for r in rows))
        # Measured categories per frozen error reason under the v1 decisions.
        by_reason = {'missing_claim_evidence': ['product_evidence'], 'unexpected_gap_evidence': ['product_evidence'],
                     'coverage_semantics': ['unresolved'], 'gap_completeness': ['product_evidence', 'unresolved']}
        for row, error in zip(rows, self.errors, strict=True):
            self.assertEqual(row['categories'], by_reason[error['reason']], row['id'])
            self.assertEqual(row['review_785']['groups'], [j['group'] for j in review[int(row['id'].split('-')[1])-1]['agent_proposed_review']['judgments']])
        # #785 assigned its qualifier group for the missing historical pair and named no separate conditional cause.
        qualifier_rows = {c['index']: any(g['code'] == pf.QUALIFIER_CODE for g in c['normalized_output']['dictionary_gaps'])
                          for c in self.diagnosis['chunks']}
        self.assertEqual(qualifier_rows, {1: False, 2: True, 3: True})
        disagreeing = {r['id']: r['review_785'] for r in rows if not r['review_785']['agrees']}
        self.assertEqual(set(disagreeing), {'error-6'})
        self.assertEqual((disagreeing['error-6']['causes_only_v1'], disagreeing['error-6']['causes_only_785']),
                         (['c1:conditional:P-GAP-QUALIFIER-DUP'], []))
        for row, error in zip(rows, self.errors, strict=True):
            if error['reason'] == 'coverage_semantics' and error['predicate'] == 'calls':
                self.assertIn('product_evidence', row['review_785']['categories_only_785'])
            if error['reason'] == 'gap_completeness' or error.get('predicate') == 'archives':
                self.assertIn('representation_sensitive', row['review_785']['categories_only_785'])
            self.assertNotIn('representation_sensitive', row['categories'])

    def test_18_each_error_reason_selects_its_own_failures(self):
        # Fixture design: some failures name two obligations, and gap_completeness sees a non-qualifier missing pair.
        failures = [
            {'layer': 'D3', 'reason': 'unreviewed_quote', 'kind': 'evidence', 'category': 'product_evidence', 'obligation_ids': ['negated']},
            {'layer': 'D4', 'reason': 'missing_link', 'kind': 'label', 'category': 'unresolved', 'predicate': 'calls', 'obligation_ids': ['missing_entity']},
            {'layer': 'D6', 'reason': 'status_mismatch', 'kind': 'label', 'category': 'unresolved', 'predicate': 'calls',
             'obligation_ids': ['conditional', 'missing_entity']},
            {'layer': 'D5', 'reason': 'unrelated_gap_link', 'kind': 'label', 'category': 'unresolved', 'code': 'missing_entity', 'obligation_ids': ['missing_entity']},
            {'layer': 'D5', 'reason': 'unrelated_gap_link', 'kind': 'label', 'category': 'unresolved', 'code': pf.QUALIFIER_CODE,
             'obligation_ids': ['conditional', 'historical']},
            {'layer': 'D5', 'reason': 'missing_gap_pair', 'kind': 'label', 'category': 'unresolved', 'code': pf.QUALIFIER_CODE, 'obligation_ids': ['historical']},
            {'layer': 'D5', 'reason': 'missing_gap_pair', 'kind': 'label', 'category': 'unresolved', 'code': 'missing_relation', 'obligation_ids': ['missing_relation']}]
        evaluation = {'obligations': [{'id': 'negated', 'failure': 'unreviewed_quote'}], 'failures': failures, 'exemptions': []}
        errors = [{'chunk': 1, 'reason': 'missing_claim_evidence', 'id': 'negated'},
                  {'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'calls'},
                  {'chunk': 1, 'reason': 'unexpected_gap_evidence', 'code': 'missing_entity'},
                  {'chunk': 1, 'reason': 'unexpected_gap_evidence', 'code': pf.QUALIFIER_CODE},
                  {'chunk': 1, 'reason': 'gap_completeness'},
                  {'chunk': 1, 'reason': 'missing_claim_evidence', 'id': 'conditional'}]
        result = pf.map_errors(errors, {1: evaluation}); rows = result['errors']
        qualifier_link = f'c1:code:{pf.QUALIFIER_CODE}:D5:{pf.QUALIFIER_CODE}'
        self.assertEqual([r['causes'] for r in rows],
                         [['c1:negated:D3'],
                          ['c1:missing_entity:P-COVERAGE-LINK', 'c1:predicate:calls:D6'],
                          ['c1:code:missing_entity:D5:missing_entity'],
                          [qualifier_link],
                          ['c1:code:missing_relation:D5:missing_relation', 'c1:historical:P-GAP-QUALIFIER-DUP'],
                          ['c1:missing_claim_evidence:conditional:v1_pass']])
        self.assertEqual(result['cause_counts']['total'], 8)
        self.assertEqual(rows[-1]['categories'], ['representation_sensitive'])
        with self.assertRaisesRegex(ValueError, 'product_fitness_mapping_unsupported_error:missing_chunks'):
            pf.map_errors([{'chunk': 1, 'reason': 'missing_chunks'}], {1: evaluation})
        exempted = {'obligations': [], 'failures': [], 'exemptions': [
            {'layer': 'D4', 'reason': 'missing_link', 'kind': 'label', 'category': 'unresolved', 'predicate': 'calls',
             'obligation_ids': ['missing_entity'], 'policy': 'P-COVERAGE-LINK'}]}
        row = pf.map_errors([{'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'calls'}], {1: exempted})['errors'][0]
        self.assertEqual(row['v1_exempted'], [{'layer': 'D4', 'reason': 'missing_link', 'policy': 'P-COVERAGE-LINK',
                                               'obligation_ids': ['missing_entity'], 'predicate': 'calls'}])
        self.assertEqual(row['causes'], ['c1:coverage_semantics:calls:v1_pass'])
        mixed = {'obligations': [], 'exemptions': [], 'failures': [
            {'layer': 'D4', 'reason': 'missing_link', 'kind': 'label', 'category': 'unresolved', 'predicate': 'calls', 'obligation_ids': ['missing_entity']},
            {'layer': 'D4', 'reason': 'missing_link', 'kind': 'evidence', 'category': 'product_evidence', 'predicate': 'calls', 'obligation_ids': ['missing_entity']}]}
        with self.assertRaisesRegex(ValueError, 'product_fitness_internal:mixed_cause_category'):
            pf.map_errors([{'chunk': 1, 'reason': 'coverage_semantics', 'predicate': 'calls'}], {1: mixed})

    def test_18_policy_flips_move_the_errors_their_fields_predict(self):
        rows = {(r['policy'], r['decision'], r['label']): r for r in self.report['sensitivity']}
        expected_keys = {(p, a['decision'], a.get('label')) for p, policy in self.spec['policies'].items() for a in policy['rejected_alternatives']}
        self.assertEqual(set(rows), expected_keys)
        for (policy_id, decision, _label), row in rows.items():
            self.assertEqual(row['executable'], decision in pf.IMPLEMENTED_DECISIONS[policy_id])
        def ids(predicate):
            return {f'error-{i}' for i, e in enumerate(self.errors, 1) if predicate(e)}
        calls = ids(lambda e: e['reason'] == 'coverage_semantics' and e['predicate'] == 'calls')
        archives = ids(lambda e: e['reason'] == 'coverage_semantics' and e['predicate'] == 'archives')
        gaps = ids(lambda e: e['reason'] == 'gap_completeness')
        chunk_of = {f'error-{i}': e['chunk'] for i, e in enumerate(self.errors, 1)}
        def moved(key): return {m['id']: m for m in rows[key]['moved_errors']}
        for key, error_ids, categories in ((('P-COVERAGE-LINK', 'links_optional', None), calls, ['representation_sensitive']),
                                           (('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None), archives, ['representation_sensitive']),
                                           (('P-FAILURE-KIND', 'missing_link_always_evidence', None), calls, ['product_evidence'])):
            with self.subTest(policy=key[0]):
                self.assertEqual(set(moved(key)), error_ids)
                self.assertTrue(all(m['categories'] == categories for m in moved(key).values()))
        for label in ('A', 'B'):
            with self.subTest(gap=label):
                found = moved(('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', label))
                self.assertEqual(set(found), gaps)
                for error_id, row in found.items():
                    removed = {c for c in next(r for r in self.mapping['errors'] if r['id'] == error_id)['causes']
                               if c.endswith(':P-GAP-QUALIFIER-DUP') and (label == 'B' or ':historical:' in c)}
                    self.assertEqual((set(row['causes_removed']), row['causes_added']), (removed, []))
                    self.assertTrue(all(c.startswith(f'c{chunk_of[error_id]}:') for c in removed))

    def test_18_historical_disagreements_are_declared_exactly(self):
        spec = copy.deepcopy(self.spec); spec['historical_mapping_disagreements_with_785'] = []
        with self.assertRaisesRegex(ValueError, 'product_fitness_undeclared_historical_disagreement:historical:error-6$'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec); spec['historical_mapping_disagreements_with_785'][0]['causes_only_785'] = ['c1:x:D3']
        with self.assertRaisesRegex(ValueError, 'product_fitness_historical_disagreement_value:historical:error-6$'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['historical_mapping_disagreements_with_785'].append(dict(spec['historical_mapping_disagreements_with_785'][0], subject='historical:error-1'))
        with self.assertRaisesRegex(ValueError, 'product_fitness_spurious_historical_disagreement:historical:error-1$'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['group_correspondence_785']['unquoted_negation']['causes'].append({'target': 'negated', 'basis': 'P-COVERAGE-LINK'})
        with self.assertRaisesRegex(ValueError, 'product_fitness_undeclared_historical_disagreement:historical:error-1$'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec); del spec['group_correspondence_785']['coverage_navigation']
        with self.assertRaisesRegex(ValueError, 'product_fitness_785_group_unmapped'):
            pf.verify_proposal(spec=spec)
        changed = copy.deepcopy(self.diagnosis); changed['finite_gold']['annotation_errors'][0]['id'] = 'conditional'
        with self.assertRaisesRegex(ValueError, 'product_fitness_785_error_identity'):
            pf.historical_mapping(self.spec, self.registry, changed)
        review = json.loads(pf.REVIEW_PATH.read_text(encoding='utf-8'))
        renamed = copy.deepcopy(review); renamed['criteria_review']['errors'][0]['id'] = 'error-0'
        with patch.object(pf, '_load_review', return_value=renamed), self.assertRaisesRegex(ValueError, 'product_fitness_785_error_identity'):
            pf.historical_mapping(self.spec, self.registry, self.diagnosis)
        forged = copy.deepcopy(review); forged['controlled_contrasts']['cases'][0]['output_sha256'] = '0'*64
        with patch.object(pf, '_load_review', return_value=forged), self.assertRaisesRegex(ValueError, 'product_fitness_785_output_identity'):
            pf.verify_proposal()
        for field, value in (('source_sha256', '0'*64), ('production_passed', False)):
            with self.subTest(identity=field):
                changed = copy.deepcopy(self.diagnosis); changed[field] = value
                with self.assertRaisesRegex(ValueError, 'product_fitness_historical_identity'):
                    pf.historical_mapping(self.spec, self.registry, changed)

    def test_19_committed_report_regenerates_byte_identically(self):
        committed = pf.REPORT_PATH.read_bytes()
        self.assertEqual(pf.render(self.report).encode('utf-8'), committed)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout): self.assertEqual(main([]), 0)
        self.assertEqual(stdout.getvalue().encode('utf-8'), committed)
        report = json.loads(committed)
        self.assertEqual(report['generation_requests'], 0)
        self.assertEqual(report['review_785_sha256'], _hash(pf.REVIEW_PATH))
        self.assertEqual(report['historical_authentication']['report_sha256'], REPORT_SHA256)
        paths = pf.report_source_paths()
        self.assertEqual(list(report['source_hashes']), paths)
        self.assertTrue(set(CRITERION_FILES) | {'tests/test_product_fitness_v1_report.py'} <= set(paths))
        self.assertNotIn('docs/extraction-product-fitness-v1.md', paths)
        self.assertNotIn(pf.REPORT_PATH.relative_to(pf.REPO).as_posix(), paths)
        for path, sha in report['source_hashes'].items(): self.assertEqual(_hash(pf.REPO/path), sha, path)
        # The private _production import lives in a file whose hash the report carries.
        diagnostic = report['diagnostic_implementation']['source_hashes']
        self.assertEqual(diagnostic['verinote/diagnostics/extraction_fidelity.py'], _hash(pf.REPO/'verinote/diagnostics/extraction_fidelity.py'))
        for rows in report['fixtures'].values():
            for row in rows.values(): self.assertNotEqual(row['chunk_outputs_sha256'], row['output_sha256'])
        self.assertNotIn('criterion_commit', report)
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root); criteria = repo/pf.CRITERIA_ROOT.relative_to(pf.REPO); criteria.mkdir(parents=True); (repo/'tests').mkdir()
            for name in ('spec.json', '._spec.json', '.env.json', '.DS_Store', 'notes.txt'): (criteria/name).write_text('x', encoding='utf-8')
            (repo/'tests/test_product_fitness_v1.py').write_text('x', encoding='utf-8')
            self.assertEqual(pf.report_source_paths(repo),
                             ['benchmarks/extraction/criteria/product-fitness-v1/spec.json', 'verinote/diagnostics/product_fitness_v1.py',
                              'scripts/verify_product_fitness_v1.py', 'tests/test_product_fitness_v1.py'])

    def test_19_import_closure_without_top_level_verinote_modules_is_in_the_report_or_diagnostic_hash_maps(self):
        # Static import closure of the module (lazy imports included), plus each package __init__.
        seen, todo = set(), [pf.REPO/'verinote/diagnostics/product_fitness_v1.py']
        while todo:
            path = todo.pop()
            if path in seen: continue
            seen.add(path); todo += verinote_imports(path)
            for parent in path.relative_to(pf.REPO).parents:
                if str(parent) != '.' and (pf.REPO/parent/'__init__.py').is_file(): todo.append(pf.REPO/parent/'__init__.py')
        imported = {p.relative_to(pf.REPO).as_posix() for p in seen}
        self.assertIn('verinote/documents.py', imported)
        # config.py enters the closure through a function-level import in transports.py.
        self.assertIn('verinote/config.py', imported)
        top = {p for p in imported if PurePosixPath(p).parent == PurePosixPath('verinote')}
        diagnostic = set(self.report['diagnostic_implementation']['source_hashes'])
        hashed = set(self.report['source_hashes'])
        self.assertEqual(sorted(imported - top - diagnostic - hashed), [])

    def test_19_verify_proposal_result_equals_the_report_with_extraction_fidelity_hash_forged_for_checkout_files_directly_under_verinote(self):
        real = efid._hash
        def forged(path): return '0'*64 if Path(path).parent == pf.REPO/'verinote' else real(path)
        with patch.object(efid, '_hash', side_effect=forged):
            self.assertEqual(pf.verify_proposal(), self.report)

    def test_19_registry_with_a_changed_short_case_sha_raises_product_fitness_registry_case_identity(self):
        with tempfile.TemporaryDirectory() as root:
            registry = json.loads((pf.CRITERIA_ROOT/'registry.json').read_text(encoding='utf-8'))
            registry['cases']['short']['case_sha256'] = '0'*64
            (Path(root)/'registry.json').write_text(json.dumps(registry), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'product_fitness_registry_case_identity'):
                pf.load_registry(root)

    def test_20_doc_quotes_only_report_values(self):
        doc = DOC_PATH.read_text(encoding='utf-8')
        report = json.loads(pf.REPORT_PATH.read_bytes())
        report_sha = digest(pf.REPORT_PATH.read_bytes())
        quoted = re.findall(QUOTED, doc)
        self.assertTrue(quoted)
        for path, value in quoted:
            with self.subTest(path=path):
                if path == '@report_sha256': expected = report_sha
                elif path.startswith('@spec.'): expected = rendered(lookup(self.spec, path[len('@spec.'):]))
                else: expected = rendered(lookup(report, path))
                self.assertEqual(value, expected)
        self.assertIn(('@report_sha256', report_sha), quoted)
        # This test's rules for text outside verified spans are the assertions below, COUNTER_WORDS and EXEMPTIONS.
        # Forms the product-fitness tests leave undetected include, for example, counts written as Korean number words,
        # a verified span next to a subject it does not describe, a policy-table description swapped with another's, a
        # hash-location sentence written without the word hash or its Korean form, and a hash-location sentence whose
        # hash word appears inside a code span and not outside one.
        rest = re.sub(QUOTED, ' ', doc)
        self.assertEqual(re.findall(r'(?<![A-Za-z0-9_:.#-])\d+(?=[\uac00-\ud7a3])', rest), [])
        self.assertEqual(re.findall(r'\d+(?=\s*(?:'+COUNTER_WORDS+'))', rest), [])
        context = doc_token_context(report, self.spec, self.registry)
        sole_acceptors = set()
        for token in re.findall(r'[A-Za-z0-9_#./:\-]*\d[A-Za-z0-9_#./:\-]*', rest):
            token = token.strip('.:-')
            with self.subTest(token=token):
                self.assertIsNotNone(_exempt_token(token, context), token)
                accepting = {name for name, accepts in EXEMPTIONS if accepts(token, context)}
                if len(accepting) == 1: sole_acceptors |= accepting
        # A class that no doc token needs would be dead: each class must be the sole acceptor of some token.
        self.assertEqual(sole_acceptors, {name for name, _ in EXEMPTIONS})

    def test_20_doc_tables_match_the_report(self):
        doc = DOC_PATH.read_text(encoding='utf-8')
        report = json.loads(pf.REPORT_PATH.read_bytes())
        rows = table_rows(doc)
        policy_rows = [row for row in rows if re.fullmatch(r'P-[A-Z-]+', row[0])]
        self.assertEqual([row[0] for row in policy_rows], list(self.spec['policies']))
        for row in policy_rows:
            with self.subTest(policy=row[0]):
                policy = self.spec['policies'][row[0]]
                self.assertIn(f'`@spec.policies.{row[0]}.decision=', row[1])
                alternatives = re.findall(rf'`@spec\.policies\.{re.escape(row[0])}\.rejected_alternatives\.(\d+)\.decision=', row[2])
                self.assertEqual(sorted(map(int, alternatives)), list(range(len(policy['rejected_alternatives']))))
                for index, alternative in enumerate(policy['rejected_alternatives']):
                    for key in ('label', 'obligation_ids', 'obligation_scope'):
                        if key in alternative:
                            self.assertIn(f'`@spec.policies.{row[0]}.rejected_alternatives.{index}.{key}', row[2])
        errors = {r['id']: r for r in report['historical_mapping']['errors']}
        causes = {c['id']: c for c in report['historical_mapping']['causes']}
        listed = []
        for row in [row for row in rows if 'error-' in row[0]]:
            ids = re.findall(r'error-\d+', row[0]); listed += ids
            with self.subTest(row=row[0]):
                categories = {c for c in CATEGORIES if f'`{c}`' in row[1]}
                self.assertTrue(categories)
                for error_id in ids: self.assertEqual(set(errors[error_id]['categories']), categories, error_id)
                row_causes = {c for error_id in ids for c in errors[error_id]['causes']}
                for cause in re.findall(r'`(c[1-3]:[^`]+)`', row[1]): self.assertIn(cause, row_causes)
                bases = {causes[c]['basis'] for c in row_causes}
                for basis in re.findall(r'\b(D3|D6|P-[A-Z-]+)\b', row[1]): self.assertIn(basis, bases)
        self.assertEqual(sorted(listed), sorted(errors))
        indices = []
        for row in [row for row in rows if '`sensitivity.' in ' | '.join(row)]:
            text = ' | '.join(row)
            with self.subTest(row=row[0]):
                found = {int(n) for n in re.findall(r'`sensitivity\.(\d+)\.', text)}
                self.assertEqual(len(found), 1, text)
                index = found.pop(); indices.append(index); entry = report['sensitivity'][index]
                self.assertIn(f'`sensitivity.{index}.decision={entry["decision"]}`', text)
                moved = entry['moved_errors']
                self.assertEqual(re.findall(r'error-\d+', row[1]), [m['id'] for m in moved])
                categories = {c for c in CATEGORIES if f'`{c}`' in row[1]}
                self.assertEqual(categories, {c for m in moved for c in m['categories']})
                # Enforced in each sensitivity row: cause ids appear only inside spans whose path names a moved-cause
                # direction; the text before each cause span holds a direction verb of that direction; the first span
                # after each direction verb in its cell is a cause span of that direction; each chunk id outside spans
                # equals the chunk of the next cause span in its cell; each registry obligation id outside spans, after
                # the first cell, is the target of a cause this row's moved errors add or remove. Undetected forms
                # include, for example, a negated verb, a span whose verb is borrowed from an earlier clause, a cause
                # paraphrased in Korean, and a swapped alternative name in the first cell.
                self.assertEqual(re.findall(r'c\d+:[\w:-]+', re.sub(QUOTED, ' ', text)), [])
                targets = {c.split(':')[1] for m in moved for c in m['causes_added']+m['causes_removed']}
                for position, cell in enumerate(row):
                    spans, last = [], 0
                    for match in re.finditer(QUOTED, cell):
                        span_path, value = match.groups()
                        if re.match(r'c\d+:', value) or 'causes' in span_path:
                            direction = re.fullmatch(rf'sensitivity\.{index}\.moved_errors\.\d+\.causes_(added|removed)\.\d+', span_path)
                            self.assertTrue(direction, span_path)
                            verbs = {name for name, verb in DIRECTION_VERBS.items() if re.search(verb, cell[last:match.start()])}
                            self.assertEqual(verbs, {direction.group(1)}, span_path)
                            spans.append((match.start(), direction.group(1), value))
                        else: spans.append((match.start(), None, value))
                        last = match.end()
                    outside = re.sub(QUOTED, lambda m: ' '*len(m.group()), cell)
                    for name, verb in DIRECTION_VERBS.items():
                        for found in re.finditer(verb, outside):
                            following = next((s for s in spans if s[0] > found.start()), None)
                            self.assertEqual(following and following[1], name, cell)
                    for found in re.finditer(r'(?<![A-Za-z0-9_])c\d+(?![A-Za-z0-9_])', outside):
                        following = next((s for s in spans if s[0] > found.start() and s[1]), None)
                        self.assertEqual(following and following[2].split(':')[0], found.group(), cell)
                    if position:
                        for obligation in re.findall(r'(?<![A-Za-z0-9_])('+'|'.join(map(re.escape, self.obligation_ids))+r')(?![A-Za-z0-9_])', outside):
                            self.assertIn(obligation, targets, cell)
        self.assertEqual(sorted(indices), [i for i, r in enumerate(report['sensitivity']) if r['executable']])

    def test_20_doc_sentences_with_a_hash_word_outside_code_spans_backtick_no_top_level_report_key_whose_value_lacks_the_extraction_fidelity_hash(self):
        doc = DOC_PATH.read_text(encoding='utf-8')
        report = json.loads(pf.REPORT_PATH.read_bytes())
        target = _hash(pf.REPO/'verinote/diagnostics/extraction_fidelity.py')
        holders = {key for key, value in report.items() if target in set(json_leaves(value))}
        self.assertTrue(holders)
        sentences = 0
        for sentence in doc_sentences(doc):
            if not re.search(r'(?i)hash|\ud574\uc2dc', re.sub(r'`[^`]*`', ' ', sentence)): continue
            sentences += 1
            for name in re.findall(r'`([^`]*)`', sentence):
                if name in report: self.assertIn(name, holders, sentence)
        self.assertGreater(sentences, 0)

    def test_20_doc_backticked_authored_test_functions_equal_the_test_16_calls_feeding_unknown_tokens(self):
        doc = DOC_PATH.read_text(encoding='utf-8')
        tree = ast.parse(AUTHORED_TEST_PATH.read_text(encoding='utf-8'))
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        test = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith('test_16_'))
        assigned = {target.id: node.value for node in ast.walk(test) if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        def called(expression):
            return {node.func.id for node in ast.walk(expression)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions}
        fed = set()
        for node in ast.walk(test):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'unknown_tokens':
                argument = node.args[0]; fed |= called(argument)
                for name in ast.walk(argument):
                    if isinstance(name, ast.Name) and name.id in assigned: fed |= called(assigned[name.id])
        named = {name for name in re.findall(r'`([^`]*)`', doc) if name in functions}
        self.assertTrue(fed)
        self.assertEqual(named, fed)


if __name__ == '__main__': unittest.main()
