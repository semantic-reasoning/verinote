"""Product-fitness v1 (#788): provenance, substitutes, D1 and D3-D6, overall verdict and v2 differences."""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.diagnostics import product_fitness_v1 as pf
from verinote.diagnostics.extraction_fidelity import DEFAULT_ROOT, assess_outputs
from verinote.documents import chunks
from verinote.extraction_ab_runner import file_index
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case

MEANING_PRESERVING = ('baseline', 'order_remap', 'merge_contiguous', 'label_free_substitutes', 'longer_quote',
                      'gap_split', 'gap_group', 'incidental_wording')
COUNTEREXAMPLES = ('negation_deleted', 'condition_deleted', 'time_deleted', 'generic_prefix_strip',
                   'wrong_gap_link', 'coverage_link_dropped')


def authorship_violations(value, path='$'):
    """Every human_review must be pending and every author agent_proposed; semantic_approvals stays empty.

    A record carrying both keys, other than an authorship block or a document
    root, must also carry a non-empty rationale.
    """
    found = []
    if isinstance(value, dict):
        if 'human_review' in value and value['human_review'] != 'pending': found.append(path+'.human_review')
        if 'author' in value and value['author'] != 'agent_proposed': found.append(path+'.author')
        if ('human_review' in value and 'author' in value and path != '$' and not path.endswith('.authorship') and
                not str(value.get('rationale', '')).strip()):
            found.append(path+'.rationale')
        for key, item in value.items():
            if key == 'semantic_approvals':
                if item != []: found.append(path+'.semantic_approvals')
                continue
            found += authorship_violations(item, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value): found += authorship_violations(item, f'{path}[{index}]')
    return found


class ProductFitnessV1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = pf.load_spec(); cls.registry = pf.load_registry()
        cls.case = load_case('short', DATA_ROOT/'v2'); cls.entry = cls.registry['cases']['short']
        cls.row = cls.entry['chunks'][0]
        cls.baseline = ideal_outputs(cls.case, contract_version=2)[0]['output']
        cls.outputs = pf.fixtures(cls.case, cls.row, cls.baseline)

    def run_fixture(self, fixture_id, spec=None, registry_case=None, output=None):
        output = self.outputs[fixture_id] if output is None else output
        spec = spec or self.spec
        return pf.assess(self.case, [{'index': 1, 'output': output}], registry_case or self.entry, spec, spec['semantic_judgments'])

    def failed(self, result, reason):
        return [f for f in result['failures'] if f['reason'] == reason]

    def test_01_registry_provenance_has_no_undeclared_difference_from_v2(self):
        self.assertEqual(pf.provenance_differences(self.registry), [])
        for case_id, entry in self.registry['cases'].items():
            for row in entry['chunks']:
                self.assertEqual(row['derived_from'], {'case_id': case_id, 'case_sha256': entry['case_sha256'], 'annotation_index': row['index']})
        changed = copy.deepcopy(self.registry)
        canonical = changed['cases']['short']['chunks'][0]['obligations'][0]['canonical']
        canonical['start'] += 1
        found = pf.provenance_differences(changed)
        self.assertEqual([(r['case_id'], r['chunk'], r['field']) for r in found], [('short', 1, 'obligations')])
        row = changed['cases']['short']['chunks'][0]
        row['deviations'].append({'field': 'obligations', 'v2': found[0]['v2'], 'v1': found[0]['v1'],
                                  'rationale': 'in-memory declared deviation', 'author': 'agent_proposed'})
        self.assertEqual(pf.provenance_differences(changed), [])
        stale = copy.deepcopy(changed)
        stale['cases']['short']['chunks'][0]['deviations'][0]['v1'] = found[0]['v2']
        self.assertEqual([(r['case_id'], r['chunk'], r['field']) for r in pf.provenance_differences(stale)], [('short', 1, 'obligations')])
        misnamed = copy.deepcopy(self.registry); misnamed['cases']['short']['chunks'][0]['derived_from']['annotation_index'] = 2
        self.assertEqual(pf.provenance_differences(misnamed), [{'case_id': 'short', 'chunk': 1, 'field': 'derived_from'}])
        changed['cases']['three']['chunks'][2]['gap_pairs'].pop()
        self.assertEqual([(r['case_id'], r['chunk'], r['field']) for r in pf.provenance_differences(changed)], [('three', 3, 'gap_pairs')])

    def test_02_substitute_validator_rejects_each_violation(self):
        # A synthetic in-test source built from two registry lines, so the
        # uniqueness check can be exercised by duplicating one body.
        negated, owned = (next(o for o in self.row['obligations'] if o['id'] == key) for key in ('negated', 'missing_relation'))
        canonical = negated['canonical']['text']; body = negated['substitutes'][0]['text']
        markers = negated['required_markers']['markers']
        source = canonical+'\n'+owned['canonical']['text']+'\n'
        offset, text = list(chunks(source))[0]
        start = source.index(body)

        def row(substitute=None, markers=tuple(markers)):
            substitute = substitute or {'start': start, 'end': start+len(body), 'text': body}
            return {'obligations': [{'id': 'negated', 'canonical': {'start': 0, 'end': len(canonical), 'text': canonical},
                                     'required_markers': {'markers': list(markers)}, 'substitutes': [dict(substitute, id='s')]}],
                    'coverage': [{'predicate': 'calls', 'expected_status': 'no_evidence', 'required_obligation_ids': []}],
                    'gap_pairs': []}
        pf.validate_chunk_registry(source, offset, text, row(), ['calls'])
        short = body.split(markers[0])[0].strip()
        other = owned['substitutes'][0]['text']
        self.assertTrue(short and markers[0] not in short and other not in canonical)
        cases = {
            'substitute_marker_missing': (source, row({'start': start, 'end': start+len(short), 'text': short})),
            'substitute_outside_canonical': (source, row({'start': source.index(other), 'end': source.index(other)+len(other), 'text': other})),
            'substitute_text': (source, row({'start': start, 'end': start+len(body), 'text': body.replace('S0', 'S2')})),
            'marker_not_in_canonical': (source, row(markers=(*markers, owned['required_markers']['markers'][0]))),
            'substitute_not_unique': (source+body+'\n', row()),
            'canonical_text': (source, dict(row(), obligations=[dict(row()['obligations'][0], canonical={'start': 1, 'end': len(canonical), 'text': canonical})])),
            'canonical_not_unique': (source+canonical+'\n', row()),
            'obligation_identity': (source, dict(row(), obligations=row()['obligations']*2)),
            'coverage_predicates': (source, dict(row(), coverage=[])),
            'coverage_row': (source, dict(row(), coverage=[{'predicate': 'calls', 'expected_status': 'no_evidence', 'required_obligation_ids': ['historical']}])),
            'gap_pairs': (source, dict(row(), gap_pairs=[['missing_relation', 'historical']])),
        }
        for reason, (text_source, value) in cases.items():
            with self.subTest(reason=reason):
                chunk_offset, chunk_text = list(chunks(text_source))[0]
                with self.assertRaisesRegex(ValueError, 'product_fitness_registry:'+reason+'$'):
                    pf.validate_chunk_registry(text_source, chunk_offset, chunk_text, value, ['calls'])

    def test_03_removing_a_reviewed_substitute_rejects_its_label_free_quote(self):
        self.assertEqual(self.run_fixture('label_free_substitutes')['failures'], [])
        changed = copy.deepcopy(self.entry)
        negated = next(o for o in changed['chunks'][0]['obligations'] if o['id'] == 'negated')
        negated['substitutes'] = []
        result = self.run_fixture('label_free_substitutes', registry_case=changed)
        self.assertEqual([(f['reason'], f['obligation_ids']) for f in result['failures']], [('unreviewed_quote', ['negated'])])
        self.assertEqual(result['overall'], 'fail')

    def test_04_qualifier_deleted_substrings_of_evidence_fail(self):
        for fixture_id, key in (('negation_deleted', 'negated'), ('condition_deleted', 'conditional'), ('time_deleted', 'historical')):
            with self.subTest(fixture=fixture_id):
                obligation = next(o for o in self.row['obligations'] if o['id'] == key)
                quote = self.outputs[fixture_id]['claims'][[o['id'] for o in self.row['obligations']].index(key)]['quote']
                self.assertIn(quote, obligation['canonical']['text'])
                self.assertTrue(all(marker not in quote for marker in obligation['required_markers']['markers']))
                result = self.run_fixture(fixture_id)
                self.assertTrue(result['production_passed'])
                index = [o['id'] for o in self.row['obligations']].index(key)
                self.assertEqual((result['chunks'][0]['claims'][index]['supports'], result['chunks'][0]['claims'][index]['semantic']),
                                 ({}, 'out_of_scope'))
                self.assertIn({'layer': 'D3', 'reason': 'unreviewed_quote', 'kind': 'evidence', 'category': 'product_evidence',
                               'obligation_ids': [key], 'chunk': 1},
                              result['failures'])
                self.assertEqual(result['overall'], 'fail')

    def test_05_label_free_quotes_pass_v1_while_frozen_v2_rejects(self):
        output = self.outputs['label_free_substitutes']
        frozen = assess_outputs(self.case, [{'index': 1, 'output': output}])
        self.assertEqual((frozen['production_passed'], frozen['finite_gold']['passed'], len(frozen['finite_gold']['annotation_errors'])),
                         (True, False, 12))
        result = self.run_fixture('label_free_substitutes')
        self.assertEqual(result['deterministic'], 'pass'); self.assertEqual(result['overall'], 'undecided')
        reasons = {r for claim in result['chunks'][0]['claims'] for r in claim['supports'].values()}
        self.assertEqual(reasons, {'substitute_supported'})

    def test_06_meaning_preserving_variants_pass_deterministically(self):
        for fixture_id in MEANING_PRESERVING:
            with self.subTest(fixture=fixture_id):
                result = self.run_fixture(fixture_id)
                self.assertTrue(result['production_passed']); self.assertEqual(result['failures'], [])
                self.assertEqual(result['overall'], 'undecided')
        self.assertEqual(len(self.outputs['merge_contiguous']['claims']), 1)
        self.assertNotEqual(self.outputs['order_remap']['claims'], self.baseline['claims'])

    def test_07_split_conditional_claim_is_never_joined(self):
        output = self.outputs['split_conditional_claim']
        quotes = [c['quote'] for c in output['claims']]
        substitute = next(o for o in self.row['obligations'] if o['id'] == 'conditional')['substitutes'][0]['text']
        self.assertEqual(' '.join(c['quote'] for c in output['claims'] if c['heading'] == 'conditional'), substitute)
        self.assertTrue(all(substitute not in quote for quote in quotes))
        result = self.run_fixture('split_conditional_claim')
        self.assertEqual([f['obligation_ids'] for f in self.failed(result, 'split_across_quotes')], [['conditional']])
        self.assertEqual(result['overall'], 'fail')

    def test_08_wrong_and_dropped_links_are_named(self):
        ids = [o['id'] for o in self.row['obligations']]
        result = self.run_fixture('wrong_gap_link')
        self.assertEqual([(f['code'], f['claim_index'], f['kind']) for f in self.failed(result, 'unrelated_gap_link')],
                         [('missing_relation', ids.index('missing_entity'), 'label')])
        self.assertEqual([(f['code'], f['obligation_ids'], f['kind']) for f in self.failed(result, 'missing_gap_pair')],
                         [('missing_relation', ['missing_relation'], 'label')])
        result = self.run_fixture('coverage_link_dropped')
        self.assertEqual([(f['predicate'], f['obligation_ids'], f['kind']) for f in result['failures']],
                         [('calls', ['missing_entity'], 'label')])
        self.assertEqual(self.failed(result, 'missing_link'), result['failures'])

    def test_09_exact_overall_values(self):
        for fixture_id in MEANING_PRESERVING+('hypothetical_line_claim',):
            self.assertEqual(self.run_fixture(fixture_id)['overall'], 'undecided', fixture_id)
        for fixture_id in COUNTEREXAMPLES+('split_conditional_claim', 'historical_status_no_evidence', 'historical_qualifier_gap_omitted'):
            self.assertEqual(self.run_fixture(fixture_id)['overall'], 'fail', fixture_id)
        contradictory = self.run_fixture('contradictory_prose')
        self.assertEqual(contradictory['deterministic'], 'pass')
        self.assertEqual(contradictory['overall'], 'undecided_agent_flagged')
        self.assertEqual(contradictory['agent_flags'], ['short-negated-contradiction'])
        self.assertEqual(self.run_fixture('generic_prefix_strip')['failures'][0]['reason'], 'production_failure')
        self.assertEqual(set(self.outputs), set(self.spec['fixture_expectations']))

    def test_10_pass_requires_a_human_approved_record(self):
        report = pf.verify_proposal()
        rows = report['fixtures']['short']
        self.assertNotEqual(rows['baseline']['chunk_outputs_sha256'], rows['baseline']['output_sha256'])
        approval = {'id': 'in-memory', 'case_id': 'short', 'chunk_outputs_sha256': rows['baseline']['chunk_outputs_sha256'],
                    'verdict': 'no_material_failure', 'author': 'human', 'human_review': 'approved', 'evidence': 'in-memory test'}
        spec = copy.deepcopy(self.spec); spec['semantic_approvals'] = [approval]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'pass')
        spec['semantic_approvals'] = [dict(approval, case_id='authored-pf1')]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'undecided')
        spec['semantic_approvals'] = [dict(approval, chunk_outputs_sha256=rows['baseline']['output_sha256'])]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'undecided')
        spec['semantic_approvals'] = [dict(approval, human_review='pending')]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'undecided')
        spec['semantic_approvals'] = [dict(approval, verdict='material_failure')]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'fail')
        spec['semantic_approvals'] = [dict(approval, chunk_outputs_sha256=rows['order_remap']['chunk_outputs_sha256'])]
        self.assertEqual(self.run_fixture('baseline', spec=spec)['overall'], 'undecided')
        self.assertEqual(self.run_fixture('order_remap', spec=spec)['overall'], 'pass')
        spec['semantic_approvals'] = [dict(approval, chunk_outputs_sha256=rows['negation_deleted']['chunk_outputs_sha256'])]
        self.assertEqual(self.run_fixture('negation_deleted', spec=spec)['overall'], 'fail')
        self.assertEqual(self.spec['semantic_approvals'], [])
        overalls = [row['overall'] for rows in report['fixtures'].values() for row in rows.values()]
        overalls += [row['v1_overall'] for row in report['differences_from_v2']]
        self.assertNotIn('pass', overalls)

    def test_11_every_judgment_is_agent_proposed_and_pending(self):
        criteria = [pf.REPO/path for path in pf.report_source_paths() if path.endswith('.json')]
        documents = {path.name: json.loads(path.read_text(encoding='utf-8')) for path in criteria}
        documents['report (generated)'] = pf.verify_proposal()
        if pf.REPORT_PATH.exists(): documents['report (committed)'] = json.loads(pf.REPORT_PATH.read_text(encoding='utf-8'))
        self.assertTrue({'spec.json', 'registry.json', 'authored-pf1.json'} <= set(documents))
        for name, document in documents.items():
            self.assertEqual(authorship_violations(document), [], name)
        self.assertIs(self.spec['assisted_review']['claude_used'], False)
        self.assertTrue(self.spec['assisted_review']['rationale'].strip())
        # Prove the walk is live.
        mutations = (('spec.json', lambda d: d['disagreements_with_785'][0].update(human_review='approved')),
                     ('spec.json', lambda d: d['disagreements_with_785'][0].update(author='human')),
                     ('registry.json', lambda d: d['authorship'].update(human_review='approved')),
                     ('spec.json', lambda d: d['policies']['P-EQUIV']['rejected_alternatives'][0].update(human_review='approved')),
                     ('spec.json', lambda d: d['semantic_approvals'].append({'human_review': 'approved'})),
                     ('spec.json', lambda d: d['historical_mapping_disagreements_with_785'][0].update(rationale=' ')))
        for name, mutate in mutations:
            with self.subTest(mutation=name):
                changed = copy.deepcopy(documents[name]); mutate(changed)
                self.assertNotEqual(authorship_violations(changed), [])

    def test_12_785_controls_and_differences_from_v2(self):
        report = pf.verify_proposal()
        controls = {row['subject']: row for row in report['differences_from_v2'] if row['subject'].startswith('control:')}
        self.assertEqual(len(controls), len(pf._review_controls()['cases']))
        for subject, row in controls.items():
            if row['expectation_785'] in ('preserves_evidence', 'preserves_evidence_proposed'):
                self.assertEqual(row['v1_deterministic'], 'pass', subject)
        differing = {row['subject'] for row in report['differences_from_v2'] if row['differs']}
        self.assertEqual(differing, {'control:short_quotes_without_fixture_labels', 'control:contradictory_prose_with_correct_quote',
                                     'fixture:short:label_free_substitutes', 'fixture:short:contradictory_prose'})
        self.assertEqual(differing, {row['subject'] for row in self.spec['deliberate_differences_from_v2']})
        for subject in ('control:dangerous_conditional_dropping', 'control:wrong_gap_reference'):
            self.assertEqual(controls[subject]['v1_overall'], 'fail')
        for subject in ('control:missing_coverage_row', 'control:status_contradicts_emitted_fact'):
            self.assertEqual(controls[subject]['v1_failures'], [['production_failure', None]])
        for subject in ('control:historical_status_with_preserved_evidence', 'control:historical_gap_omitted_but_claim_retained'):
            self.assertEqual({kind for _, kind in controls[subject]['v1_failures']}, {'label'})
        spec = copy.deepcopy(self.spec); spec['deliberate_differences_from_v2'].pop(0)
        with self.assertRaisesRegex(ValueError, 'product_fitness_undeclared_v2_difference'):
            pf.verify_proposal(spec=spec)
        rows = {row['subject']: row for row in report['differences_from_v2']}
        for declared in self.spec['deliberate_differences_from_v2']:
            row = rows[declared['subject']]
            self.assertEqual((declared['v2'], declared['v1']), ('pass' if row['v2_finite_passed'] else 'fail', row['v1_overall']))
        spec = copy.deepcopy(self.spec); spec['deliberate_differences_from_v2'][0]['v1'] = 'pass'
        with self.assertRaisesRegex(ValueError, 'product_fitness_declared_v2_difference_value'):
            pf.verify_proposal(spec=spec)
        disagreeing = {row['subject'] for row in report['differences_from_v2'] if row['disagreement_with_785']}
        self.assertEqual(disagreeing, {'control:wrong_gap_reference'})
        self.assertEqual(disagreeing, {d['subject'] for d in self.spec['disagreements_with_785']})
        self.assertEqual(rows['control:wrong_gap_reference']['expectation_785'], 'material_failure')
        spec = copy.deepcopy(self.spec); spec['disagreements_with_785'] = []
        with self.assertRaisesRegex(ValueError, 'product_fitness_undeclared_785_disagreement:control:wrong_gap_reference'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec); spec['disagreements_with_785'][0]['v1_kinds'] = ['evidence']
        with self.assertRaisesRegex(ValueError, 'product_fitness_785_disagreement_value:control:wrong_gap_reference$'):
            pf.verify_proposal(spec=spec)
        for key, subject, error in (('deliberate_differences_from_v2', 'fixture:short:no_such_fixture', 'orphan_declaration'),
                                    ('disagreements_with_785', 'control:no_such_control', 'orphan_declaration'),
                                    ('disagreements_with_785', 'cause:unowned', 'unowned_declaration'),
                                    ('disagreements_with_785', 'historical:error-1', 'misfiled_declaration'),
                                    ('historical_mapping_disagreements_with_785', 'historical:error-99', 'orphan_declaration')):
            with self.subTest(subject=subject):
                spec = copy.deepcopy(self.spec); spec[key].append(dict(spec[key][0], subject=subject))
                with self.assertRaisesRegex(ValueError, f'product_fitness_{error}:{subject}$'):
                    pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec); spec['disagreements_with_785'].append(copy.deepcopy(spec['disagreements_with_785'][0]))
        with self.assertRaisesRegex(ValueError, 'product_fitness_duplicate_declaration:disagreements_with_785$'):
            pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['disagreements_with_785'].append(dict(spec['disagreements_with_785'][0], subject='dummy:x'))
        with patch.dict(pf.DECLARATION_OWNERS, {'dummy:': 'dummy'}), \
             self.assertRaisesRegex(ValueError, 'product_fitness_unchecked_owner:dummy$'):
            pf.verify_proposal(spec=spec)

    def test_13_hypothetical_line_claim_is_out_of_scope(self):
        result = self.run_fixture('hypothetical_line_claim')
        claims = result['chunks'][0]['claims']
        self.assertIn('예시(실제 아님):', self.outputs['hypothetical_line_claim']['claims'][-1]['quote'])
        self.assertEqual((claims[-1]['supports'], claims[-1]['semantic']), ({}, 'out_of_scope'))
        self.assertTrue(all(c['semantic'] == 'semantic_review_required' for c in claims[:-1]))
        self.assertEqual(result['deterministic'], 'pass')

    def test_14_verify_proposal_raises_on_a_flipped_expectation(self):
        pf.verify_proposal()
        for fixture_id, flipped in (('baseline', 'fail'), ('contradictory_prose', 'undecided'), ('wrong_gap_link', 'undecided')):
            with self.subTest(fixture=fixture_id):
                spec = copy.deepcopy(self.spec); spec['fixture_expectations'][fixture_id]['overall'] = flipped
                with self.assertRaisesRegex(ValueError, 'product_fitness_fixture_expectation:short:'+fixture_id):
                    pf.verify_proposal(spec=spec)
        spec = copy.deepcopy(self.spec); spec['fixture_expectations']['coverage_link_dropped']['failures'] = [['missing_link', 'evidence']]
        with self.assertRaisesRegex(ValueError, 'product_fitness_fixture_expectation:short:coverage_link_dropped'):
            pf.verify_proposal(spec=spec)

    def test_15_readonly_without_network_subprocess_or_writes(self):
        roots = (DEFAULT_ROOT, DATA_ROOT/'v2', pf.REVIEW_PATH.parent, pf.CRITERIA_ROOT)
        before = [file_index(root) for root in roots]
        real_open = io.open; connect = sqlite3.connect
        def readonly_connect(database, *args, **kwargs):
            if not (kwargs.get('uri') and str(database).endswith('?mode=ro')): raise AssertionError('writable sqlite prohibited')
            return connect(database, *args, **kwargs)
        def readonly_open(file, mode='r', *args, **kwargs):
            if set(mode) & set('wax+'): raise AssertionError('write prohibited')
            return real_open(file, mode, *args, **kwargs)
        with patch('urllib.request.urlopen', side_effect=AssertionError('HTTP prohibited')), \
             patch('http.client.HTTPConnection.connect', side_effect=AssertionError('HTTP prohibited')), \
             patch('subprocess.Popen', side_effect=AssertionError('subprocess prohibited')), \
             patch('subprocess.run', side_effect=AssertionError('subprocess prohibited')), \
             patch.object(Path, 'write_text', side_effect=AssertionError('write prohibited')), \
             patch.object(Path, 'write_bytes', side_effect=AssertionError('write prohibited')), \
             patch('io.open', side_effect=readonly_open), patch('builtins.open', side_effect=readonly_open), \
             patch('sqlite3.connect', side_effect=readonly_connect):
            first = pf.verify_proposal(); second = pf.verify_proposal()
        self.assertEqual(first, second); self.assertEqual(first['generation_requests'], 0)
        self.assertEqual([file_index(root) for root in roots], before)
        with patch.object(pf, 'REVIEW_SHA256', '0'*64), self.assertRaisesRegex(ValueError, 'product_fitness_785_review_identity'):
            pf.verify_proposal()

    def test_policy_alternatives_flip_only_their_policy_fixture(self):
        cases = (('P-COVERAGE-LINK', 'links_optional', None, 'coverage_link_dropped'),
                 ('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None, 'historical_status_no_evidence'),
                 ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'A', 'historical_qualifier_gap_omitted'),
                 ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'B', 'historical_qualifier_gap_omitted'))
        for policy, decision, label, fixture_id in cases:
            with self.subTest(policy=policy, label=label):
                self.assertEqual(self.run_fixture(fixture_id)['overall'], 'fail')
                spec = pf.with_policy(self.spec, policy, decision, label)
                result = self.run_fixture(fixture_id, spec=spec)
                self.assertEqual((result['overall'], result['failures']), ('undecided', []))
                self.assertEqual([e['policy'] for e in result['chunks'][0]['exemptions']], [policy])
                self.assertEqual(self.run_fixture('condition_deleted', spec=spec)['overall'], 'fail')
        spec = pf.with_policy(self.spec, 'P-FAILURE-KIND', 'missing_link_always_evidence')
        self.assertEqual(pf.failure_vector(self.run_fixture('coverage_link_dropped', spec=spec)), [('missing_link', 'evidence')])
        spec = pf.with_policy(self.spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'A')
        conditional_only = copy.deepcopy(self.baseline)
        qualifier = next(g for g in conditional_only['dictionary_gaps'] if g['code'] == 'qualifier_not_representable')
        qualifier['claim_indices'] = [[o['id'] for o in self.row['obligations']].index('historical')]
        self.assertEqual(pf.failure_vector(self.run_fixture(None, spec=spec, output=conditional_only)), [('missing_gap_pair', 'label')])
        # Alternative B is scoped by obligation_scope rather than obligation_ids, so it exempts conditional too.
        spec = pf.with_policy(self.spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'B')
        self.assertEqual(self.run_fixture(None, spec=spec, output=conditional_only)['failures'], [])
        ids = [o['id'] for o in self.row['obligations']]
        no_entity_gap = copy.deepcopy(self.baseline)
        no_entity_gap['dictionary_gaps'] = [g for g in no_entity_gap['dictionary_gaps'] if g['code'] != 'missing_entity']
        self.assertIn(ids.index('missing_entity'), next(c for c in no_entity_gap['coverage'] if c['predicate'] == 'calls')['claim_indices'])
        for label in ('A', 'B'):
            with self.subTest(non_qualifier_pair=label):
                spec = pf.with_policy(self.spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', label)
                result = self.run_fixture(None, spec=spec, output=no_entity_gap)
                self.assertEqual([(f['code'], f['obligation_ids']) for f in self.failed(result, 'missing_gap_pair')],
                                 [('missing_entity', ['missing_entity'])])
                self.assertEqual(result['chunks'][0]['exemptions'], [])
        status_a = pf.with_policy(self.spec, 'P-STATUS-HISTORICAL', 'historical_no_evidence_linked')
        calls_absent = copy.deepcopy(self.baseline)
        keep = [i for i, f in enumerate(calls_absent['facts']) if f['predicate'] != 'calls']
        calls_absent['facts'] = [calls_absent['facts'][i] for i in keep]
        for row in calls_absent['coverage']:
            row['fact_indices'] = [keep.index(i) for i in row['fact_indices'] if i in keep]
            if row['predicate'] == 'calls': row['status'] = 'no_evidence'
        archives_extracted = copy.deepcopy(self.baseline)
        historical = next(o for o in self.row['obligations'] if o['id'] == 'historical')
        archives_extracted['facts'].append({'subject': 'S0', 'predicate': 'archives', 'object': 'S1', 'quote': historical['canonical']['text']})
        next(c for c in archives_extracted['coverage'] if c['predicate'] == 'archives').update(
            status='extracted', fact_indices=[len(archives_extracted['facts'])-1])
        for name, output, predicate in (('expected_not_representable', calls_absent, 'calls'),
                                        ('actual_no_evidence', archives_extracted, 'archives')):
            with self.subTest(status_a_condition=name):
                result = self.run_fixture(None, spec=status_a, output=output)
                self.assertTrue(result['production_passed'])
                self.assertEqual([f['predicate'] for f in self.failed(result, 'status_mismatch')], [predicate])
                self.assertEqual(result['chunks'][0]['exemptions'], [])
        unlinked_status = copy.deepcopy(self.outputs['historical_status_no_evidence'])
        next(c for c in unlinked_status['coverage'] if c['predicate'] == 'archives')['claim_indices'] = []
        spec = pf.with_policy(self.spec, 'P-STATUS-HISTORICAL', 'historical_no_evidence_linked')
        result = self.run_fixture(None, spec=spec, output=unlinked_status)
        self.assertEqual([f['predicate'] for f in self.failed(result, 'status_mismatch')], ['archives'])
        self.assertEqual(result['chunks'][0]['exemptions'], [])
        unlinked_gap = copy.deepcopy(self.outputs['historical_qualifier_gap_omitted'])
        # A not_representable row must link a claim in production, so the unlinked row is no_evidence.
        next(c for c in unlinked_gap['coverage'] if c['predicate'] == 'archives').update(status='no_evidence', claim_indices=[])
        self.assertNotIn(ids.index('historical'), [i for c in unlinked_gap['coverage'] for i in c['claim_indices']])
        for label in ('A', 'B'):
            spec = pf.with_policy(self.spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', label)
            result = self.run_fixture(None, spec=spec, output=unlinked_gap)
            self.assertTrue(result['production_passed'], label)
            self.assertEqual([f['obligation_ids'] for f in self.failed(result, 'missing_gap_pair')], [['historical']], label)
            self.assertEqual([e['policy'] for e in result['chunks'][0]['exemptions']], [], label)
        with self.assertRaisesRegex(ValueError, 'product_fitness_alternative'):
            pf.with_policy(self.spec, 'P-ORDER', 'anything')
        with self.assertRaisesRegex(ValueError, 'product_fitness_alternative:P-GAP-QUALIFIER-DUP$'):
            pf.with_policy(self.spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage')
        for target in ('policy', 'alternative'):
            with self.subTest(scope_typo=target):
                spec = copy.deepcopy(self.spec); policy = spec['policies']['P-GAP-QUALIFIER-DUP']
                row = policy if target == 'policy' else policy['rejected_alternatives'][1]
                row['obligation_scope'] = 'all_qualifier_pair'
                with self.assertRaisesRegex(ValueError, 'product_fitness_policy_scope:P-GAP-QUALIFIER-DUP$'):
                    self.run_fixture('baseline', spec=spec)
        spec = copy.deepcopy(self.spec); spec['policies']['P-EQUIV']['decision'] = 'generic_prefix_strip'
        with self.assertRaisesRegex(ValueError, 'product_fitness_policy_unimplemented:P-EQUIV'):
            self.run_fixture('baseline', spec=spec)

    def test_internal_invariant_rejects_a_duplicated_quote(self):
        offset, text = list(chunks(self.case['source']))[0]
        fact_quote = self.baseline['facts'][0]['quote']
        duplicated = fact_quote.split(': ', 1)[1]
        self.assertGreater(text.count(duplicated), 1)
        normalized = copy.deepcopy(self.baseline); normalized['claims'][0]['quote'] = duplicated
        with self.assertRaisesRegex(ValueError, 'product_fitness_internal:quote_not_unique'):
            pf.evaluate_chunk(self.case['source'], offset, text, self.row, normalized, self.spec)

    def test_v1_layers_never_call_the_v2_evaluator(self):
        refuse = AssertionError('v2 evaluator called')
        with patch('verinote.extraction_threshold_evaluation._supported', side_effect=refuse), \
             patch('verinote.extraction_threshold_evaluation.evaluate_annotations', side_effect=refuse), \
             patch('verinote.diagnostics.extraction_fidelity._supported', side_effect=refuse):
            for fixture_id in self.outputs:
                self.run_fixture(fixture_id)
        with patch('verinote.extraction_threshold_evaluation.evaluate_annotations', side_effect=refuse), \
             self.assertRaisesRegex(AssertionError, 'v2 evaluator called'):
            assess_outputs(self.case, [{'index': 1, 'output': self.baseline}])

    def test_category_is_separate_from_the_overall_verdict(self):
        label = self.run_fixture('coverage_link_dropped')
        self.assertEqual((label['overall'], label['categories']), ('fail', ['unresolved']))
        self.assertEqual({(f['kind'], f['category']) for f in label['failures']}, {('label', 'unresolved')})
        evidence = self.run_fixture('negation_deleted')
        self.assertEqual((evidence['overall'], evidence['categories']), ('fail', ['product_evidence']))
        production = self.run_fixture('generic_prefix_strip')
        self.assertEqual((production['overall'], production['categories']), ('fail', []))
        self.assertEqual(self.run_fixture('baseline')['categories'], [])
        report = pf.verify_proposal()
        self.assertEqual(report['fixtures']['short']['coverage_link_dropped']['categories'], ['unresolved'])

    def test_status_mismatch_is_evidence_when_its_obligation_fails_d3(self):
        output = copy.deepcopy(self.outputs['historical_status_no_evidence'])
        ids = [o['id'] for o in self.row['obligations']]
        output['claims'][ids.index('historical')]['quote'] = self.outputs['time_deleted']['claims'][ids.index('historical')]['quote']
        result = self.run_fixture(None, output=output)
        self.assertTrue(result['production_passed'])
        status = self.failed(result, 'status_mismatch')
        self.assertEqual([(f['predicate'], f['kind'], f['category']) for f in status], [('archives', 'evidence', 'product_evidence')])
        self.assertEqual(pf.failure_vector(self.run_fixture('historical_status_no_evidence')), [('status_mismatch', 'label')])
    def test_load_spec_with_a_symlinked_spec_json_raises_product_fitness_file(self):
        with tempfile.TemporaryDirectory() as root:
            link = Path(root)/'spec.json'; link.symlink_to(pf.CRITERIA_ROOT/'spec.json')
            with self.assertRaisesRegex(ValueError, r'^product_fitness_file:spec\.json$'):
                pf.load_spec(root)

    def test_assess_with_no_chunk_outputs_raises_product_fitness_chunk_identity(self):
        with self.assertRaisesRegex(ValueError, r'^product_fitness_chunk_identity$'):
            pf.assess(self.case, [], self.entry, self.spec)

    def test_load_registry_with_three_chunk_indices_reordered_or_its_first_chunk_removed_raises_product_fitness_registry_chunks(self):
        def reorder(rows):
            for row, index in zip(rows, (1, 3, 2), strict=True): row['index'] = index
        for name, edit in (('reordered', reorder), ('first_removed', lambda rows: rows.pop(0))):
            raw = json.loads((pf.CRITERIA_ROOT/'registry.json').read_text(encoding='utf-8')); edit(raw['cases']['three']['chunks'])
            with self.subTest(edit=name), tempfile.TemporaryDirectory() as root:
                (Path(root)/'registry.json').write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, r'^product_fitness_registry_chunks$'):
                    pf.load_registry(root)

if __name__ == '__main__': unittest.main()
