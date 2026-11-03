"""Product-fitness v1 (#788): tests that fail when one loader, validator or verifier check is disabled.

Inputs are in-memory copies, temporary files, or a patched module function; no test writes the committed criteria JSON.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.diagnostics import product_fitness_v1 as pf
from verinote.documents import chunks
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from verinote.store import digest


def _raw(name):
    return json.loads((pf.CRITERIA_ROOT/name).read_text(encoding='utf-8'))


class ProductFitnessGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = pf.load_spec(); cls.registry = pf.load_registry(); cls.authored = _raw('authored-pf1.json')
        cls.case = load_case('short', DATA_ROOT/'v2'); cls.entry = cls.registry['cases']['short']
        cls.outputs = pf.fixtures(cls.case, cls.entry['chunks'][0], ideal_outputs(cls.case, contract_version=2)[0]['output'])

    def load_edited(self, name, edit, loader):
        raw = _raw(name); edit(raw)
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/name).write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
            return loader(root)

    def assess(self, fixture_id, entry=None, spec=None, judgments=None):
        spec = spec or self.spec
        return pf.assess(self.case, [{'index': 1, 'output': self.outputs[fixture_id]}], entry or self.entry, spec,
                         spec['semantic_judgments'] if judgments is None else judgments)

    # loaders
    def test_load_spec_from_a_directory_without_spec_json_raises_product_fitness_file(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(ValueError, r'^product_fitness_file:spec\.json$'):
            pf.load_spec(root)

    def test_load_spec_with_spec_id_or_schema_version_changed_raises_product_fitness_spec_identity(self):
        for field, value in (('spec_id', 'other'), ('schema_version', 2)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, r'^product_fitness_spec_identity$'):
                self.load_edited('spec.json', lambda raw, field=field, value=value: raw.update({field: value}), pf.load_spec)

    def test_load_spec_without_p_order_raises_product_fitness_policy_set(self):
        with self.assertRaisesRegex(ValueError, r'^product_fitness_policy_set$'):
            self.load_edited('spec.json', lambda raw: raw['policies'].pop('P-ORDER'), pf.load_spec)

    def test_load_registry_without_three_raises_product_fitness_registry_cases(self):
        with self.assertRaisesRegex(ValueError, r'^product_fitness_registry_cases$'):
            self.load_edited('registry.json', lambda raw: raw['cases'].pop('three'), pf.load_registry)

    def test_load_registry_with_a_changed_short_source_sha_raises_product_fitness_registry_case_identity(self):
        with self.assertRaisesRegex(ValueError, r'^product_fitness_registry_case_identity$'):
            self.load_edited('registry.json', lambda raw: raw['cases']['short'].update(source_sha256='0'*64), pf.load_registry)

    # validate_chunk_registry on the short registry row
    def validate_short_row(self, edit):
        row = copy.deepcopy(self.entry['chunks'][0]); edit(row)
        offset, text = list(chunks(self.case['source']))[0]
        return pf.validate_chunk_registry(self.case['source'], offset, text, row, [r['id'] for r in self.case['dictionary']['relations']])

    def test_validate_chunk_registry_rejects_each_edited_short_row_with_its_reason(self):
        def empty_canonical(row):
            canonical = row['obligations'][0]['canonical']; canonical.update(end=canonical['start'], text='')
        cases = (('canonical_text', empty_canonical),
                 ('marker_not_in_canonical', lambda row: row['obligations'][0]['required_markers'].update(markers=[])),
                 ('marker_not_in_canonical', lambda row: row['obligations'][0]['required_markers']['markers'].append('')),
                 ('coverage_row', lambda row: row['coverage'][0].update(expected_status='bogus')),
                 ('gap_pairs', lambda row: row['gap_pairs'].append(list(row['gap_pairs'][0]))),
                 ('gap_pairs', lambda row: row['gap_pairs'][0].__setitem__(0, 'bogus')))
        self.validate_short_row(lambda row: None)
        for index, (reason, edit) in enumerate(cases):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, rf'^product_fitness_registry:{reason}$'):
                self.validate_short_row(edit)

    # validate_authored
    def test_validate_authored_rejects_each_edited_copy_with_its_message(self):
        def longer_source(raw):
            raw['case']['source'] += '\n'*8000
            raw['case']['source_sha256'] = hashlib.sha256(raw['case']['source'].encode('utf-8')).hexdigest()
        def three_template(raw):
            raw['template_of']['case_id'] = 'three'; raw['chunk']['derived_from']['template_case_id'] = 'three'
        cases = (('authored_identity', lambda raw: raw.update(fixture_id='other')),
                 ('authored_identity', lambda raw: raw['case'].update(case_id='other')),
                 ('authored_identity', lambda raw: raw['case'].update(source_sha256='0'*64)),
                 ('authored_dictionary', lambda raw: raw['case']['dictionary']['relations'][0].update(extra='x')),
                 ('authored_chunks', longer_source),
                 ('authored_chunks', lambda raw: raw['chunk'].update(index=2)),
                 ('authored_chunks', three_template),
                 ('authored_template', lambda raw: raw['chunk']['derived_from'].update(relation='')))
        pf.validate_authored(copy.deepcopy(self.authored))
        for index, (message, edit) in enumerate(cases):
            raw = copy.deepcopy(self.authored); edit(raw)
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, rf'^product_fitness_{message}$'):
                pf.validate_authored(raw)

    # provenance_differences
    def test_provenance_differences_reports_a_shifted_canonical_start_whose_deviation_lacks_one_matching_field(self):
        changed = copy.deepcopy(self.registry); row = changed['cases']['short']['chunks'][0]
        row['obligations'][0]['canonical']['start'] += 1
        found = pf.provenance_differences(changed)[0]
        full = {'field': 'obligations', 'v2': found['v2'], 'v1': found['v1'], 'rationale': 'r', 'author': 'agent_proposed'}
        variants = {'full': full, 'field': full | {'field': 'coverage'}, 'v2': full | {'v2': []},
                    'rationale': {k: v for k, v in full.items() if k != 'rationale'},
                    'author': {k: v for k, v in full.items() if k != 'author'}}
        for name, deviation in variants.items():
            with self.subTest(deviation=name):
                row['deviations'] = [deviation]
                self.assertEqual([r['field'] for r in pf.provenance_differences(changed)], [] if name == 'full' else ['obligations'])

    # evaluate_chunk exemptions
    def test_historical_no_evidence_linked_keeps_status_mismatch_a_failure_when_archives_requires_no_obligation(self):
        entry = copy.deepcopy(self.entry)
        next(c for c in entry['chunks'][0]['coverage'] if c['predicate'] == 'archives')['required_obligation_ids'] = []
        spec = pf.with_policy(self.spec, 'P-STATUS-HISTORICAL', 'historical_no_evidence_linked')
        result = self.assess('historical_status_no_evidence', entry=entry, spec=spec)
        self.assertEqual(([f['reason'] for f in result['failures']], result['chunks'][0]['exemptions']), (['status_mismatch'], []))

    def test_current_gap_decision_with_obligation_ids_keeps_missing_gap_pair_a_failure(self):
        spec = copy.deepcopy(self.spec); spec['policies']['P-GAP-QUALIFIER-DUP']['obligation_ids'] = ['historical']
        result = self.assess('historical_qualifier_gap_omitted', spec=spec)
        self.assertEqual(([f['reason'] for f in result['failures']], result['chunks'][0]['exemptions']), (['missing_gap_pair'], []))

    def test_evaluate_chunk_with_missing_link_absent_from_spec_reasons_raises_product_fitness_internal_reason(self):
        spec = copy.deepcopy(self.spec); spec['reasons'].remove('missing_link')
        with self.assertRaisesRegex(ValueError, r'^product_fitness_internal:reason$'):
            self.assess('coverage_link_dropped', spec=spec)

    # assess
    def test_assess_contradictory_prose_is_undecided_when_the_judgment_case_chunk_or_verdict_differs(self):
        self.assertEqual(self.assess('contradictory_prose')['overall'], 'undecided_agent_flagged')
        for field, value in (('case_id', 'authored-pf1'), ('chunk', 2), ('verdict', 'no_material_failure')):
            with self.subTest(field=field):
                judgments = [dict(j, **{field: value}) for j in self.spec['semantic_judgments']]
                self.assertEqual(self.assess('contradictory_prose', judgments=judgments)['overall'], 'undecided')

    def test_assess_overall_reasons_for_negation_deleted_and_for_a_human_approved_material_failure_on_baseline(self):
        self.assertEqual(self.assess('negation_deleted')['overall_reasons'], ['unreviewed_quote'])
        spec = copy.deepcopy(self.spec)
        spec['semantic_approvals'] = [{'id': 'h', 'case_id': 'short', 'human_review': 'approved', 'verdict': 'material_failure',
                                       'chunk_outputs_sha256': digest([{'index': 1, 'output': self.outputs['baseline']}])}]
        result = self.assess('baseline', spec=spec)
        self.assertEqual((result['overall'], result['overall_reasons']), ('fail', ['human_material_failure']))

    # with_policy
    def test_with_policy_for_a_decision_no_alternative_has_raises_product_fitness_alternative(self):
        with self.assertRaisesRegex(ValueError, r'^product_fitness_alternative:P-COVERAGE-LINK$'):
            pf.with_policy(self.spec, 'P-COVERAGE-LINK', 'no_such_decision')

    def test_with_policy_without_label_selects_the_single_remaining_gap_alternative(self):
        spec = copy.deepcopy(self.spec); del spec['policies']['P-GAP-QUALIFIER-DUP']['rejected_alternatives'][1]
        policy = pf.with_policy(spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage')['policies']['P-GAP-QUALIFIER-DUP']
        self.assertEqual(policy['obligation_ids'], ['historical'])

    def test_with_policy_drops_the_current_decision_obligation_scope_and_ids_the_alternative_lacks(self):
        spec = copy.deepcopy(self.spec); spec['policies']['P-GAP-QUALIFIER-DUP']['obligation_scope'] = 'all_qualifier_pairs'
        a = pf.with_policy(spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'A')['policies']['P-GAP-QUALIFIER-DUP']
        self.assertNotIn('obligation_scope', a)
        spec = copy.deepcopy(self.spec); spec['policies']['P-GAP-QUALIFIER-DUP']['obligation_ids'] = ['conditional']
        b = pf.with_policy(spec, 'P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'B')['policies']['P-GAP-QUALIFIER-DUP']
        self.assertNotIn('obligation_ids', b)

    # verify_proposal
    def test_verify_proposal_with_an_undeclared_shifted_canonical_start_raises_undeclared_provenance_difference(self):
        changed = copy.deepcopy(self.registry); changed['cases']['short']['chunks'][0]['obligations'][0]['canonical']['start'] += 1
        with self.assertRaisesRegex(ValueError, r'^product_fitness_undeclared_provenance_difference$'):
            pf.verify_proposal(registry=changed)

    def test_verify_proposal_with_a_baseline_builder_title_change_raises_product_fitness_baseline_builder(self):
        real = pf.baseline_output
        with patch.object(pf, 'baseline_output', side_effect=lambda row, facts: dict(real(row, facts), title='other')), \
             self.assertRaisesRegex(ValueError, r'^product_fitness_baseline_builder$'):
            pf.verify_proposal()

    def test_verify_proposal_with_an_expectation_for_no_recipe_raises_product_fitness_fixture_set_short(self):
        spec = copy.deepcopy(self.spec); spec['fixture_expectations']['no_such_recipe'] = spec['fixture_expectations']['baseline']
        with self.assertRaisesRegex(ValueError, r'^product_fitness_fixture_set:short$'):
            pf.verify_proposal(spec=spec)

    def test_verify_proposal_with_a_flipped_785_finite_observation_raises_product_fitness_785_observation(self):
        review = json.loads(pf.REVIEW_PATH.read_text(encoding='utf-8'))
        control = review['controlled_contrasts']['cases'][0]; control['observed']['finite_passed'] = not control['observed']['finite_passed']
        with patch.object(pf, '_load_review', return_value=review), \
             self.assertRaisesRegex(ValueError, rf"^product_fitness_785_observation:{control['id']}$"):
            pf.verify_proposal()

    def test_verify_proposal_with_a_non_differing_fixture_declared_deliberate_raises_undeclared_v2_difference(self):
        spec = copy.deepcopy(self.spec); declared = spec['deliberate_differences_from_v2']
        declared.append(dict(declared[0], subject='fixture:short:baseline', v2='pass', v1='undecided'))
        with self.assertRaisesRegex(ValueError, r'^product_fitness_undeclared_v2_difference:fixture:short:baseline$'):
            pf.verify_proposal(spec=spec)

    def test_verify_proposal_with_an_agreeing_control_declared_a_785_disagreement_raises_undeclared_785_disagreement(self):
        spec = copy.deepcopy(self.spec); declared = spec['disagreements_with_785']
        declared.append(dict(declared[0], subject='control:dangerous_conditional_dropping'))
        with self.assertRaisesRegex(ValueError, r'^product_fitness_undeclared_785_disagreement:control:dangerous_conditional_dropping$'):
            pf.verify_proposal(spec=spec)


if __name__ == '__main__': unittest.main()
