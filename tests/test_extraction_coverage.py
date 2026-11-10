"""Synthetic contract tests, not measurements of real-model semantic recall."""
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    PAGE,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.extraction_coverage import (
    MAX_REPORT_BYTES,
    read_report,
    repair_missing_indices,
    validate_accounting,
    write_report,
)
from verinote.ontology import (
    EXTRACTION_CONTRACT_VERSION,
    EXTRACTION_PROMPT,
    bounded_prompt,
    dictionary_context,
    extraction_quote_issues,
)
from verinote.service import Service
from verinote.store import Conflict, canonical, digest


def shifted_coverage_response():
    """Synthetic equivalent of the observed 28-fact boundary shift; no live document content."""
    groups = [('applies_to', 1), ('defines', 1), ('separates_into', 4), ('precedes', 1),
              ('based_on', 1), ('has_component', 3), ('supports_activity', 5),
              ('has_stage', 4), ('has_activity', 8)]
    dictionary = DICTIONARY | {'relations': [DICTIONARY['relations'][0] | {'id': p} for p, _ in groups]}
    facts = [FACTS[0] | {'predicate': p} for p, count in groups for _ in range(count)]
    value = extraction_response(facts, dictionary)
    wrong = {'supports_activity': list(range(11, 17)), 'has_stage': list(range(17, 21)),
             'has_activity': list(range(21, 28))}
    for row in value['coverage']:
        if row['predicate'] in wrong:
            row['fact_indices'] = wrong[row['predicate']]
    return value, dictionary


def overflow_coverage_response():
    """56 synthetic facts: the last two coverage groups were counted one slot too far."""
    groups = [('other', 28), ('contains_process', 23), ('has_evaluation_area', 5)]
    dictionary = DICTIONARY | {'relations': [DICTIONARY['relations'][0] | {'id': p} for p, _ in groups]}
    facts = [FACTS[0] | {'predicate': p} for p, count in groups for _ in range(count)]
    value = extraction_response(facts, dictionary)
    value['coverage'][1]['fact_indices'] = list(range(28, 52))
    value['coverage'][2]['fact_indices'] = list(range(52, 57))
    return value, dictionary


class Accounting(unittest.TestCase):
    def test_fresh_claim_references_prune_only_dangling_links_and_keep_content(self):
        value = extraction_response(); value['claims'] *= 5
        value['coverage'][0]['claim_indices'] = [0, 2, 3, 4, 5]
        value['dictionary_gaps'] = [{'code':'missing_entity', 'detail':'합성 누락', 'claim_indices':[4, 12, 1]}]
        original = copy.deepcopy(value)
        with self.assertRaises(ValueError): repair_missing_indices(value, DICTIONARY)
        result, changes = repair_missing_indices(value, DICTIONARY, prune_claim_references=True)
        self.assertEqual(result['coverage'][0]['claim_indices'], [0, 2, 3, 4])
        self.assertEqual(result['dictionary_gaps'][0]['claim_indices'], [4, 1])
        restored = copy.deepcopy(result)
        for change in changes:
            self.assertEqual(change['field'], 'claim_indices')
            restored[change['collection']][change['row']][change['field']] = change['before']
        self.assertEqual(restored, original); self.assertEqual(value, original)
        self.assertEqual(repair_missing_indices(result, DICTIONARY, prune_claim_references=True), (result, []))
        validate_accounting(result, DICTIONARY)

    def test_claim_pruning_rejects_malformed_or_entirely_missing_links(self):
        for collection in ('coverage', 'dictionary_gaps'):
            for refs in ([1], [0, 0, 1], [0, True], [0, -1], [0, 13], [0, 1.0], [0, '1'], list(range(13)), None):
                value = extraction_response()
                if collection == 'dictionary_gaps':
                    value[collection] = [{'code':'missing_entity', 'detail':'합성 누락', 'claim_indices':refs}]
                else: value[collection][0]['claim_indices'] = refs
                original = copy.deepcopy(value)
                with self.subTest(collection=collection, refs=refs), self.assertRaises(ValueError):
                    repair_missing_indices(value, DICTIONARY, prune_claim_references=True)
                self.assertEqual(value, original)
        for mutate in (
            lambda v: v['coverage'][0].update(status='no_evidence'),
            lambda v: v['coverage'][0].update(predicate='unknown'),
            lambda v: v['coverage'][0].update(reason=''),
            lambda v: v['coverage'].append(copy.deepcopy(v['coverage'][0])),
            lambda v: v.update(dictionary_gaps=[{'code':'missing_entity','detail':'x','claim_indices':[]}]),
            lambda v: v.update(dictionary_gaps=[{'code':'unknown','detail':'x','claim_indices':[0,1]}]),
        ):
            value = extraction_response(); value['coverage'][0]['claim_indices'] = [0, 1]
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                repair_missing_indices(value, DICTIONARY, prune_claim_references=True)

    def test_model_range_opt_in_repairs_56_fact_boundary_without_changing_content(self):
        value, dictionary = overflow_coverage_response()
        original = copy.deepcopy(value)
        with self.assertRaises(ValueError): repair_missing_indices(value, dictionary)
        with self.assertRaises(ValueError): validate_accounting(value, dictionary)
        result, changes = repair_missing_indices(value, dictionary, allow_model_range=True)
        self.assertEqual(value, original)
        self.assertEqual([c['before'] for c in changes], [list(range(28, 52)), list(range(52, 57))])
        self.assertEqual([c['after'] for c in changes], [list(range(28, 51)), [52, 53, 54, 55, 51]])
        restored = copy.deepcopy(result)
        for c in changes: restored['coverage'][c['row']]['fact_indices'] = c['before']
        self.assertEqual(restored, original)
        self.assertEqual(repair_missing_indices(result, dictionary, allow_model_range=True), (result, []))
        validate_accounting(result, dictionary)

    def test_model_range_bounds_do_not_relax_claims_statuses_or_shapes(self):
        value = extraction_response(); value['coverage'][0]['fact_indices'] = [80]
        result, changes = repair_missing_indices(value, DICTIONARY, allow_model_range=True)
        self.assertEqual(result['coverage'][0]['fact_indices'], [0, 1])
        self.assertEqual(changes[0]['before'], [80])
        for bad in ([81], [-1], [True], [1.0], ['1'], [2, 2], list(range(81)), None):
            value = extraction_response(); value['coverage'][0]['fact_indices'] = bad
            original = copy.deepcopy(value)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                repair_missing_indices(value, DICTIONARY, allow_model_range=True)
            self.assertEqual(value, original)
        for mutate in (
            lambda v: v['coverage'][0].update(status='no_evidence'),
            lambda v: v['coverage'][0].update(claim_indices=[1]),
            lambda v: v['coverage'][0].update(reason=''),
            lambda v: v['coverage'][0].update(predicate='unknown'),
            lambda v: v['coverage'].append(copy.deepcopy(v['coverage'][0])),
            lambda v: v.update(facts=[]),
        ):
            value = extraction_response(); value['coverage'][0]['fact_indices'] = [2]
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                repair_missing_indices(value, DICTIONARY, allow_model_range=True)

    def test_shifted_boundaries_repair_only_fresh_index_membership(self):
        value, dictionary = shifted_coverage_response()
        original = copy.deepcopy(value)
        with self.assertRaises(ValueError): validate_accounting(value, dictionary)
        result, changes = repair_missing_indices(value, dictionary)
        self.assertEqual(value, original)
        self.assertEqual([c['predicate'] for c in changes], ['supports_activity', 'has_stage', 'has_activity'])
        self.assertEqual([c['after'] for c in changes], [list(range(11, 16)), [17, 18, 19, 16], list(range(21, 28)) + [20]])
        restored = copy.deepcopy(result)
        for change in changes:
            self.assertEqual(change['before'], original['coverage'][change['row']]['fact_indices'])
            restored['coverage'][change['row']]['fact_indices'] = change['before']
        self.assertEqual(restored, original)
        self.assertEqual(repair_missing_indices(result, dictionary), (result, []))
        validate_accounting(result, dictionary)

    def test_wrong_only_references_and_cross_row_overlap_use_existing_facts(self):
        dictionary = DICTIONARY | {'relations': [DICTIONARY['relations'][0], DICTIONARY['relations'][0] | {'id':'other'}]}
        for supplied in ([1], [0, 1]):
            value = extraction_response([FACTS[0], FACTS[1] | {'predicate':'other'}], dictionary)
            value['coverage'][0]['fact_indices'] = supplied
            result, changes = repair_missing_indices(value, dictionary)
            self.assertEqual(result['coverage'][0]['fact_indices'], [0])
            self.assertEqual(result['coverage'][1]['fact_indices'], [1])
            self.assertEqual(len(changes), 1)

    def test_missing_subset_indices_only_append_and_preserve_everything_else(self):
        for supplied in ([], [2], [2, 0], [2, 0, 1]):
            value = extraction_response([FACTS[0]] * 3)
            value['coverage'][0]['fact_indices'] = supplied
            before = copy.deepcopy(value)
            result, changes = repair_missing_indices(value, DICTIONARY)
            expected = supplied + [i for i in range(3) if i not in supplied]
            self.assertEqual(result['coverage'][0]['fact_indices'], expected)
            self.assertEqual(value, before)
            restored = copy.deepcopy(result); restored['coverage'][0]['fact_indices'] = supplied
            self.assertEqual(restored, before)
            self.assertEqual(len(changes), int(len(supplied) < 3))
            self.assertEqual(repair_missing_indices(result, DICTIONARY), (result, []))
            validate_accounting(result, DICTIONARY)

    def test_local_repair_does_not_hide_conflicts_or_other_invalid_fields(self):
        mutations = [
            lambda v: v['coverage'][0].update(fact_indices=[0, 0]),
            lambda v: v['coverage'][0].update(fact_indices=[True]),
            lambda v: v['coverage'][0].update(fact_indices=[-1]),
            lambda v: v['coverage'][0].update(fact_indices=[2]),
            lambda v: v['coverage'][0].update(status='no_evidence'),
            lambda v: v['coverage'][0].update(predicate='unknown'),
            lambda v: v['coverage'][0].update(claim_indices=[True]),
            lambda v: v['coverage'][0].update(reason=''),
            lambda v: v['coverage'].append(copy.deepcopy(v['coverage'][0])),
            lambda v: v.update(dictionary_gaps=[{'code':'unknown','detail':'bad','claim_indices':[0]}]),
            lambda v: v.update(facts=[]),
        ]
        for mutate in mutations:
            value = extraction_response(); value['coverage'][0]['fact_indices'] = []
            mutate(value); before = copy.deepcopy(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                repair_missing_indices(value, DICTIONARY)
            self.assertEqual(value, before)
        dictionary = DICTIONARY | {'relations': [*DICTIONARY['relations'], DICTIONARY['relations'][0] | {'id':'other'}]}
        value = extraction_response(dictionary=dictionary)
        value['coverage'][0]['fact_indices'] = []
        value['coverage'][1].update(status='extracted', fact_indices=[0])
        with self.assertRaises(ValueError): repair_missing_indices(value, dictionary)

    def test_zero_facts_zero_predicates_and_all_statuses(self):
        for dictionary in (DICTIONARY, DICTIONARY | {'relations': []}):
            value = extraction_response([], dictionary)
            self.assertEqual(validate_accounting(value, dictionary)['coverage'], value['coverage'])
        value = extraction_response([])
        row = value['coverage'][0]
        row.update(status='not_representable', claim_indices=[0], reason='조건부 동작')
        value['dictionary_gaps'] = [{'code': 'qualifier_not_representable', 'detail': '조건을 제거할 수 없음', 'claim_indices': [0]}]
        validate_accounting(value, DICTIONARY)
        value = extraction_response([FACTS[0]] * 80)
        extra = DICTIONARY['relations'][0] | {'id': 'absent'}
        dictionary = DICTIONARY | {'relations': [*DICTIONARY['relations'], extra]}
        value['coverage'].append({'predicate': 'absent', 'fact_indices': [], 'claim_indices': [], 'status': 'limit_reached', 'reason': '80개 한도'})
        validate_accounting(value, dictionary)
        value['coverage'][0]['claim_indices'] = [0]
        value['coverage'][0]['reason'] = '일부 조건은 위키에 보존'
        validate_accounting(value, dictionary)

    def test_malformed_accounting_rejected_even_with_invalid_quote(self):
        mutations = [
            lambda v: v.pop('coverage'), lambda v: v.pop('dictionary_gaps'),
            lambda v: v['coverage'].append(v['coverage'][0]),
            lambda v: v['coverage'][0].update(predicate='foreign'),
            lambda v: v['coverage'][0].update(fact_indices=[0]),
            lambda v: v['coverage'][0].update(fact_indices=[0, 0]),
            lambda v: v['coverage'][0].update(fact_indices=[True, 1]),
            lambda v: v['coverage'][0].update(fact_indices=[-1, 1]),
            lambda v: v['coverage'][0].update(fact_indices=[0, 2]),
            lambda v: v['coverage'][0].update(claim_indices=[True]),
            lambda v: v['coverage'][0].update(status='no_evidence'),
            lambda v: v['coverage'][0].update(status='not_representable', fact_indices=[]),
            lambda v: v['coverage'][0].update(status='limit_reached', fact_indices=[]),
            lambda v: v['coverage'][0].update(reason='x' * 121),
            lambda v: v['coverage'][0].update(extra='unknown'),
            lambda v: v.update(dictionary_gaps=[{'code': 'invent', 'detail': 'bad', 'claim_indices': [0]}]),
            lambda v: v.update(dictionary_gaps=[{'code': 'missing_entity', 'detail': 'bad', 'claim_indices': []}]),
            lambda v: v.update(dictionary_gaps=[{'code': 'missing_relation', 'detail': 'bad', 'claim_indices': [1]}]),
        ]
        for mutate in mutations:
            value = extraction_response(); value['facts'][0]['quote'] = 'absent'
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                extraction_quote_issues(value, SOURCE, DICTIONARY, require_coverage=True)
        # A fact assigned to a different approved row must also fail.
        other = DICTIONARY['relations'][0] | {'id': 'other'}
        dictionary = DICTIONARY | {'relations': [*DICTIONARY['relations'], other]}
        value = extraction_response(dictionary=dictionary)
        value['coverage'][0].update(fact_indices=[0])
        value['coverage'][1].update(fact_indices=[1], status='extracted')
        with self.assertRaises(ValueError): validate_accounting(value, dictionary)


class CoverageWorkflow(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def execute(self, outputs, source=SOURCE, dictionary=DICTIONARY):
        doc = self.store.register('합성', 'synthetic.txt', source.encode(), source)
        approved_dictionary(self.store, doc['id'], dictionary)
        pending, calls = iter(outputs), []
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append(prompt)
                value = next(pending)
                if isinstance(value, Exception): raise value
                return value() if callable(value) else copy.deepcopy(value)
        self.service = Service(self.store, self.root, Fake())
        run = self.service.start(doc['id'], 'copilot', '', expected_version=1, expected_generation=1)
        self.service.pool.shutdown(wait=True)
        return self.store.run(run['id']), self.store.document(doc['id']), calls

    def report(self, run):
        return read_report(self.root, self.store, run['id'])

    def test_claim_pruning_after_fact_exclusion_has_typed_mixed_audit_and_keeps_all_claims(self):
        value = extraction_response([FACTS[0] | {'object':'unknown'}, *FACTS]); value['claims'] *= 5
        value['coverage'][0].update(fact_indices=[], claim_indices=[0, 2, 3, 4, 5])
        value['dictionary_gaps'] = [{'code':'missing_entity','detail':'합성 누락','claim_indices':[1,5]}]
        original = copy.deepcopy(value)
        from verinote.extraction_coverage import exclude_off_dictionary_facts
        filtered, _ = exclude_off_dictionary_facts(value, DICTIONARY)
        normalized, changes = repair_missing_indices(filtered, DICTIONARY, prune_claim_references=True)
        run, doc, calls = self.execute([value])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 1)
        folder = self.root/'runs'/run['id']
        audit = json.loads((folder/'1/coverage-repair.json').read_text())
        self.assertEqual(audit['schema_version'], 2)
        self.assertEqual(audit['changes'], [({'collection':'coverage','field':'fact_indices'} | c) for c in changes])
        self.assertEqual([(c['collection'],c['field']) for c in audit['changes']],
                         [('coverage','fact_indices'),('coverage','claim_indices'),('dictionary_gaps','claim_indices')])
        self.assertEqual(audit['before_sha256'], digest(original)); self.assertEqual(audit['after_sha256'], digest(normalized))
        saved = json.loads((folder/'extraction-checkpoint.json').read_text())['outputs'][0]
        self.assertEqual(saved['facts'], FACTS); self.assertEqual(saved['claims'], original['claims'])
        self.assertEqual(saved['coverage'], normalized['coverage'])
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.assertTrue(all(i['status']=='pending' for i in doc['items'] if i['kind']!='ontology'))
        self.assertTrue(self.report(run)['available'])
        path = folder/'extraction-coverage.json'; report = json.loads(path.read_text())
        report['chunks'][0]['coverage'][0]['claim_indices'] = original['coverage'][0]['claim_indices']
        path.write_text(json.dumps(report)); self.assertFalse(self.report(run)['available'])

    def test_claim_pruning_requires_audit_before_quote_repair_or_checkpoint(self):
        value = extraction_response(); value['coverage'][0]['claim_indices'] = [0, 1]
        value['facts'][0]['quote'] = 'absent'
        repair = {'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}
        run, _, calls = self.execute([value, repair])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 2)
        from verinote.usage import atomic_json
        def write(path, data):
            if Path(path).name == 'coverage-repair.json': raise OSError('disk unavailable')
            atomic_json(path, data)
        with patch('verinote.extraction_coverage.atomic_json', side_effect=write):
            run, doc, calls = self.execute([value, repair])
        self.assertEqual(run['status'], 'error'); self.assertIn('인덱스 보정 기록 저장 실패', run['detail'])
        self.assertEqual(len(calls), 1); self.assertEqual([i['kind'] for i in doc['items']], ['ontology'])
        self.assertFalse((self.root/'runs'/run['id']/'extraction-checkpoint.json').exists())

    def test_mixed_audit_count_bound_includes_both_fields_and_gaps(self):
        dictionary = DICTIONARY | {'relations':[DICTIONARY['relations'][0] | {'id':f'r{i}'} for i in range(40)]}
        facts = [FACTS[0] | {'predicate':f'r{i % 40}'} for i in range(80)]
        value = extraction_response(facts, dictionary)
        for row in value['coverage']: row.update(fact_indices=[], claim_indices=[0, 1])
        value['dictionary_gaps'] = [{'code':'missing_entity','detail':'합성 누락','claim_indices':[0,1]} for _ in range(12)]
        run, doc, calls = self.execute([value], dictionary=dictionary)
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 1)
        path = self.root/'runs'/run['id']/'1/coverage-repair.json'
        raw = path.read_bytes(); audit = json.loads(raw)
        self.assertEqual(audit['schema_version'], 2); self.assertEqual(len(audit['changes']), 92)
        from verinote.extraction_coverage import write_repair_audit
        with self.assertRaisesRegex(ValueError, '인덱스 보정 기록 저장 실패'):
            write_repair_audit(self.root, run, doc, 1, 0, value, value, [audit['changes'][-1]] * 133)
        self.assertEqual(path.read_bytes(), raw)

    def test_fresh_out_of_range_references_are_audited_without_extra_calls(self):
        value, dictionary = overflow_coverage_response()
        normalized, changes = repair_missing_indices(value, dictionary, allow_model_range=True)
        run, doc, calls = self.execute([value], dictionary=dictionary)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertIn('인덱스 2건 로컬 보정', run['detail'])
        folder = self.root/'runs'/run['id']
        audit = json.loads((folder/'1/coverage-repair.json').read_text())
        self.assertEqual(audit['changes'], changes)
        self.assertEqual(audit['before_sha256'], digest(value))
        self.assertEqual(audit['after_sha256'], digest(normalized))
        saved = json.loads((folder/'extraction-checkpoint.json').read_text())['outputs'][0]
        self.assertEqual(saved['facts'], value['facts'])
        self.assertEqual(saved['coverage'], normalized['coverage'])
        self.assertEqual(len(saved['facts']), 56)
        self.assertTrue(self.report(run)['available'])
        self.assertTrue(all(i['status']=='pending' for i in doc['items'] if i['kind']!='ontology'))
        self.assertEqual(self.store.snapshot()['facts'], [])
        path = folder/'extraction-coverage.json'; report = json.loads(path.read_text())
        report['chunks'][0]['coverage'] = value['coverage']
        path.write_text(json.dumps(report))
        self.assertFalse(self.report(run)['available'])

    def test_range_repair_audit_failure_prevents_acceptance(self):
        value, dictionary = overflow_coverage_response()
        from verinote.usage import atomic_json
        def write(path, data):
            if Path(path).name == 'coverage-repair.json': raise OSError('disk unavailable')
            atomic_json(path, data)
        with patch('verinote.extraction_coverage.atomic_json', side_effect=write):
            run, doc, calls = self.execute([value], dictionary=dictionary)
        self.assertEqual(run['status'], 'error')
        self.assertIn('인덱스 보정 기록 저장 실패', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertEqual([i['kind'] for i in doc['items']], ['ontology'])
        self.assertFalse((self.root/'runs'/run['id']/'extraction-checkpoint.json').exists())

    def test_shifted_indices_are_audited_before_quote_repair_and_checkpoint(self):
        value, dictionary = shifted_coverage_response()
        value['facts'][6]['quote'] = 'absent'
        original = copy.deepcopy(value)
        normalized, changes = repair_missing_indices(value, dictionary)
        repair = {'repairs':[{'path':'facts.6.quote', 'quote':FACTS[0]['quote']}]}
        run, doc, calls = self.execute([value, repair], dictionary=dictionary)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assertIn('인덱스 3건 로컬 보정', run['detail'])
        folder = self.root/'runs'/run['id']
        audit = json.loads((folder/'1/coverage-repair.json').read_text())
        self.assertEqual(audit['changes'], changes)
        self.assertEqual(audit['before_sha256'], digest(original))
        self.assertEqual(audit['after_sha256'], digest(normalized))
        saved = json.loads((folder/'extraction-checkpoint.json').read_text())['outputs'][0]
        self.assertEqual(saved['coverage'], normalized['coverage'])
        self.assertEqual(len(saved['facts']), 28)
        self.assertEqual(saved['facts'][6]['quote'], FACTS[0]['quote'])
        self.assertTrue(self.report(run)['available'])
        self.assertTrue(all(i['status']=='pending' for i in doc['items'] if i['kind']!='ontology'))
        self.assertEqual(self.store.snapshot()['facts'], [])
        # Persisted reports stay strict: fresh normalization must not silently repair them.
        path = folder/'extraction-coverage.json'
        report = json.loads(path.read_text())
        report['chunks'][0]['coverage'] = original['coverage']
        path.write_text(json.dumps(report))
        self.assertFalse(self.report(run)['available'])

    def test_shifted_indices_cannot_be_accepted_without_mandatory_audit(self):
        value, dictionary = shifted_coverage_response()
        from verinote.usage import atomic_json
        def write(path, data):
            if Path(path).name == 'coverage-repair.json': raise OSError('disk unavailable')
            atomic_json(path, data)
        with patch('verinote.extraction_coverage.atomic_json', side_effect=write):
            run, doc, calls = self.execute([value], dictionary=dictionary)
        self.assertEqual(run['status'], 'error')
        self.assertIn('인덱스 보정 기록 저장 실패', run['detail'])
        self.assertEqual(len(calls), 1)
        self.assertEqual([i['kind'] for i in doc['items']], ['ontology'])
        self.assertFalse((self.root/'runs'/run['id']/'extraction-checkpoint.json').exists())

    def test_local_repair_audits_original_response_without_an_extra_call(self):
        value = extraction_response(); value['coverage'][0]['fact_indices'] = []
        original = copy.deepcopy(value)
        run, doc, calls = self.execute([value])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 1)
        self.assertIn('인덱스 1건 로컬 보정', run['detail'])
        audit = json.loads((self.root/'runs'/run['id']/'1/coverage-repair.json').read_text())
        self.assertEqual((audit['identity']['run_id'], audit['chunk'], audit['offset']), (run['id'], 1, 0))
        self.assertEqual(audit['source_sha256'], doc['sha256'])
        self.assertEqual(audit['identity']['sha256'], run['ontology']['sha256'])
        self.assertEqual(audit['before_sha256'], digest(original))
        self.assertEqual(audit['after_sha256'], digest(extraction_response()))
        self.assertEqual(audit['changes'][0]['before'], [])
        self.assertEqual(audit['changes'][0]['after'], [0, 1])
        self.assertEqual(self.report(run)['chunks'][0]['coverage'][0]['fact_indices'], [0, 1])
        self.assertTrue(all(i['status']=='pending' for i in doc['items'] if i['kind']!='ontology'))
        self.assertEqual(self.store.snapshot()['facts'], [])

    def test_accounting_repair_keeps_quote_repair_and_fails_before_it_if_audit_cannot_save(self):
        value = extraction_response(); value['coverage'][0]['fact_indices'] = []
        value['facts'][0]['quote'] = 'absent'
        repair = {'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}
        run, _, calls = self.execute([value, repair])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 2)
        self.assertIn('인덱스 1건 로컬 보정', run['detail']); self.assertIn('원문 인용 1개 보정', run['detail'])
        from verinote.usage import atomic_json
        def write(path, data):
            if Path(path).name == 'coverage-repair.json': raise OSError('disk unavailable')
            atomic_json(path, data)
        with patch('verinote.extraction_coverage.atomic_json', side_effect=write):
            run, doc, calls = self.execute([value, repair])
        self.assertEqual(run['status'], 'error'); self.assertIn('인덱스 보정 기록 저장 실패', run['detail'])
        self.assertEqual(len(calls), 1); self.assertEqual([i['kind'] for i in doc['items']], ['ontology'])
        self.assertFalse((self.root/'runs'/run['id']/'extraction-checkpoint.json').exists())
        self.assertNotIn('1건 로컬 보정', run['detail'])

    def test_same_call_technical_enumeration_aliases_and_compact_pin(self):
        source = '제안된 설계에서 제어기 C는 센서 A와 센서 B로 구성된다. C는 연산기를 사용한다. 장애 시 센서 A를 차단할 수 있다. 센서 B는 차단하지 않는다.'
        dictionary = {'entities': [{'id': n, 'name': n, 'type': t, 'quote': source} for n,t in [('C','Control'),('센서 A','Sensor'),('센서 B','Sensor'),('연산기','Processor')]],
                      'aliases': [{'alias':'제어기 C','entity':'C','quote':source}],
                      'relations': [{'id':p,'description':d,'subject_types':['Control'],'object_types':[t],'quote':source} for p,d,t in [('composed_of','제안 설계의 직접 구성. 실제 배치 주장이 아님.','Sensor'),('uses','제안 설계의 직접 사용 구성. 실제 운영 주장이 아님.','Processor')]]}
        facts = [{'subject':'제어기 C','predicate':'composed_of','object':n,'quote':source.split('. ')[0]+'.'} for n in ('센서 A','센서 B')]
        facts += [{'subject':'C','predicate':'uses','object':'연산기','quote':'C는 연산기를 사용한다.'}]
        page = {'title':'설계','claims':[{'heading':'조건과 부정','text':source,'quote':source}]}
        value = extraction_response(facts,dictionary,page)
        value['dictionary_gaps'] = [{'code':'qualifier_not_representable','detail':'조건부 차단 및 부정을 무조건적 관계로 표현할 수 없음','claim_indices':[0]}]
        run, doc, calls = self.execute([value], source, dictionary)
        self.assertEqual(run['status'],'success',run['detail']); self.assertEqual(len(calls),1)
        saved = [i for i in doc['items'] if i['kind']=='fact']
        self.assertEqual(len(saved),3); self.assertTrue(all(i['status']=='pending' and i['payload']['subject']=='C' for i in saved))
        prompt_pin = json.loads(calls[0].split('입력(JSON):\n')[1])['approved_dictionary']
        self.assertEqual(prompt_pin, run['ontology'] | {'payload':dictionary_context(dictionary)})
        self.assertEqual(run['ontology']['payload'],dictionary)
        full = bounded_prompt(EXTRACTION_PROMPT, {'approved_dictionary':run['ontology']})
        compact = bounded_prompt(EXTRACTION_PROMPT, {'approved_dictionary':prompt_pin})
        self.assertLess(len(compact.encode()),len(full.encode()))
        for phrase in ('승인된 모든 관계 정의를 하나씩','실제 배치·운영 사실로 바꾸지','최소 개수를 채우지'):
            self.assertIn(phrase,calls[0])
        self.assertEqual(self.report(run)['gap_count'],1)
        self.assertEqual(self.store.snapshot()['facts'],[])

    def test_zero_fact_and_empty_dictionary_are_valid_extractions(self):
        for dictionary in (DICTIONARY, DICTIONARY | {'relations': []}):
            run, doc, calls = self.execute([extraction_response([], dictionary), {'facts':[]}], dictionary=dictionary)
            self.assertEqual(run['status'], 'success', run['detail'])
            report = self.report(run)
            self.assertTrue(report['available']); self.assertTrue(report['published'])
            self.assertEqual(report['saved_fact_count'], 0); self.assertEqual(len(calls), 2)
            self.assertEqual([i['kind'] for i in doc['items']], ['ontology', 'page'])

    def test_invalid_report_does_not_spend_repair_or_save(self):
        value=extraction_response();value['coverage']=[];value['facts'][0]['quote']='absent'
        run,doc,calls=self.execute([value])
        self.assertEqual(run['status'],'error');self.assertIn('coverage',run['detail']);self.assertEqual(len(calls),1)
        self.assertEqual([i['kind'] for i in doc['items']],['ontology'])

    def test_multichunk_dedup_claim_mapping_and_summary(self):
        first=extraction_response(); second=extraction_response([FACTS[0], FACTS[1] | {'quote':SOURCE}])
        run,doc,calls=self.execute([first,second],SOURCE.ljust(8000)+SOURCE)
        report=self.report(run)
        self.assertEqual(run['status'],'success',run['detail'])
        self.assertEqual((report['emitted_fact_count'],report['saved_fact_count']),(4,3))
        self.assertEqual([c['claim_base'] for c in report['chunks']],[0,1])
        self.assertEqual([c['offset'] for c in report['chunks']],[0,8000])
        self.assertTrue(report['published']);self.assertEqual(len(calls),2)
        self.assertNotIn('chunks',self.service.with_usage('runs',run,coverage_details=False)['coverage'])
        self.assertEqual(report,self.service.with_usage('runs',run)['coverage'])

    def test_partial_failure_and_commit_rejection_never_publish(self):
        run,doc,calls=self.execute([extraction_response(),TimeoutError('timeout')],SOURCE.ljust(8000)+SOURCE)
        report=self.report(run)
        self.assertEqual((report['validated_chunks'],report['total_chunks']),(1,2))
        self.assertFalse(report['published']);self.assertEqual(report['saved_fact_count'],0)
        self.assertEqual([i['kind'] for i in doc['items']],['ontology'])
        original=self.store.finish_run
        def reject(*args,**kwargs):
            if kwargs.get('dictionary') is not None:return original(*args,**kwargs)
            raise ValueError('synthetic transaction failure')
        with patch.object(self.store,'finish_run',side_effect=reject):
            run,doc,calls=self.execute([extraction_response()])
        self.assertEqual(run['status'],'error');self.assertFalse(self.report(run)['published'])
        self.assertEqual(self.report(run)['saved_fact_count'],0)

    def test_optional_report_io_failure_does_not_erase_success(self):
        for failures in ([OSError('disk')],[OSError('first'),None],[None,OSError('last')]):
            from verinote.usage import atomic_json
            events=iter(failures)
            def write(path,value):
                event=next(events)  # noqa: B023 (intentional: same-iteration events)
                if event:raise event
                return atomic_json(path,value)
            count=len(failures)
            with self.subTest(failures=failures), patch('verinote.extraction_coverage.atomic_json',side_effect=write):
                run,doc,calls=self.execute([extraction_response()]*count,SOURCE.ljust(8000)*(count-1)+SOURCE)
            self.assertEqual(run['status'],'success',run['detail'])
            self.assertEqual(len([i for i in doc['items'] if i['kind']=='fact']),2)
            if failures[-1]:
                self.assertIn('기록 저장 실패',run['detail']);self.assertFalse(self.report(run)['available'])

    def test_corrupt_bounded_owned_report_and_deletion(self):
        run,doc,_=self.execute([extraction_response()]);path=self.root/'runs'/run['id']/'extraction-coverage.json'
        raw=path.read_bytes();original=json.loads(raw)
        bad=[]
        for mutate in (lambda r:r.update(extra=1),lambda r:r['identity'].update(revision=True),
                       lambda r:r['identity'].update(sha256='wrong'),lambda r:r.update(source_length=1),
                       lambda r:r['chunks'][0].update(offset=True),lambda r:r['chunks'][0].update(claim_count=12),lambda r:r['chunks'][0]['coverage'][0].update(fact_indices=[0])):
            report=copy.deepcopy(original);mutate(report);bad.append(json.dumps(report).encode())
        for data in [b'{broken',b'x'*(MAX_REPORT_BYTES+1),*bad]:
            path.write_bytes(data);self.assertFalse(self.report(run)['available'])
        path.unlink();target=self.root/'elsewhere.json';target.write_bytes(raw);path.symlink_to(target)
        self.assertFalse(self.report(run)['available'])
        self.assertFalse(write_report(self.root,original));self.assertEqual(target.read_bytes(),raw)
        path.unlink();path.write_bytes(raw)
        self.assertTrue(self.report(run)['available'])
        self.store.delete_document(doc['id'],False,doc['current_version'],doc['generation'])
        self.assertTrue(path.exists())  # DB ownership disappears before asynchronous cleanup.
        self.assertFalse(self.report(run)['available'])
        self.assertFalse(self.service.with_usage('runs',run)['coverage']['available'])
        self.service.cleanup_logs();self.assertFalse(path.exists())

    def test_legacy_rerun_preserves_all_review_history(self):
        approved_dictionary(self.store,self.doc['id'])
        run=self.store.create_run(self.doc['id'],'agy','','extract',1,1)
        with self.store.connect(True) as db:
            pin=run['ontology'];pin.pop('extraction_contract')
            db.execute('UPDATE runs SET ontology=? WHERE id=?',(canonical(pin),run['id']))
        self.store.run_status(run['id'],'running');self.store.finish_run(run['id'],PAGE,FACTS)
        items=[i for i in self.store.document(self.doc['id'])['items'] if i['kind']!='ontology']
        for item,status in zip(items,('approved','held','rejected'),strict=True):
            self.store.review(item['id'],item['revision'],status,'Reviewer','keep history')
        before={i['id']:i for i in self.store.document(self.doc['id'])['items']}
        with self.store.connect() as db:events=[tuple(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]
        doc=self.store.document(self.doc['id']);self.assertTrue(doc['can_extract']);self.assertTrue(doc['extraction_upgrade'])
        retry=self.store.create_run(doc['id'],'agy','','extract',1,1)
        self.assertEqual(retry['ontology']['extraction_contract'],EXTRACTION_CONTRACT_VERSION)
        with self.assertRaises(Conflict):self.store.create_run(doc['id'],'agy','','extract',1,1)
        self.store.run_status(retry['id'],'error','test')
        retry=self.store.create_run(doc['id'],'agy','','extract',1,1)
        self.store.run_status(retry['id'],'running');self.store.finish_run(retry['id'],PAGE,FACTS)
        after=self.store.document(doc['id']);self.assertFalse(after['can_extract']);self.assertFalse(after['extraction_upgrade'])
        with self.assertRaises(Conflict):self.store.create_run(doc['id'],'agy','','extract',1,1)
        for i in after['items']:
            if i['id'] in before:self.assertEqual(i,before[i['id']])
            else:self.assertEqual(i['status'],'pending');self.assertEqual(i['run_id'],retry['id'])
        with self.store.connect() as db:self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM events ORDER BY seq')][:len(events)],events)
        self.assertFalse(self.report(self.store.run(run['id']))['available'])

    def test_maximum_report_fits_bound_and_renders_escaped(self):
        dictionary=copy.deepcopy(DICTIONARY)
        dictionary['relations']=[DICTIONARY['relations'][0]|{'id':('r'+str(i)).ljust(100,'x')} for i in range(40)]
        source=SOURCE.ljust(8000)*16
        page=PAGE|{'claims':PAGE['claims']*12}
        facts=[FACTS[0]|{'predicate':dictionary['relations'][i%40]['id']} for i in range(80)]
        value=extraction_response(facts,dictionary,page)
        for row in value['coverage']:row.update(reason='한'*120,claim_indices=list(range(12)))
        value['dictionary_gaps']=[{'code':'missing_relation','detail':'한'*160,'claim_indices':list(range(12))} for _ in range(12)]
        run,doc,_=self.execute([value]*16,source,dictionary)
        self.assertEqual(run['status'],'success',run['detail']);report=self.report(run)
        self.assertTrue(report['available']);self.assertEqual(report['validated_chunks'],16);self.assertTrue(report['capped'])
        self.assertEqual(report['saved_fact_count'],40)
        report['chunks'][1]['coverage'][0]['reason']='<script>bad</script>'
        report['chunks'][1]['coverage'][0]['predicate']='<img onerror=x>'
        report['chunks'][1]['dictionary_gaps'][0]['detail']='<b>bad</b>'
        app=Path('verinote/web/app.js').read_text();function=app[app.index('function renderCoverage('):app.index('function usageStage(')]
        script="const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');\n"+function
        script+='\nconst c='+json.dumps(report)+';console.log(renderCoverage(c));c.published=false;console.log(renderCoverage(c));console.log(renderCoverage({available:false,warning:"<bad>"}));'
        html=subprocess.check_output(['node'],input=script,text=True)
        self.assertNotIn('<script>',html);self.assertNotIn('<img',html);self.assertNotIn('<b>bad',html)
        self.assertIn('저장 위키 주장 #13',html);self.assertIn('미저장 주장 #13',html);self.assertIn('&lt;bad&gt;',html)


if __name__=='__main__':unittest.main()
