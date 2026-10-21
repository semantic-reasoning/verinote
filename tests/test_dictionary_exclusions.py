"""Partial dictionary generation without weakening strict approval validation."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_openrouter_integration as integration
import test_workflow as workflow
from test_ontology import wire_dictionary
from test_openrouter_pipeline import Fixture
from test_workflow import DICTIONARY, SOURCE

from verinote.dictionary_exclusions import (
    MAX_DETAILS,
    MAX_REPORT_BYTES,
    read_report,
    validate_report,
)
from verinote.dictionary_filter import merge_generated_dictionary
from verinote.ontology import LIMITS, source_spans
from verinote.service import Service

EMPTY = {'entities': [], 'aliases': [], 'relations': []}


def bad_name(payload):
    ref = [payload['sources'][0]['id']] * 2
    return {'entities': [{'id': 'phantom', 'name': '없는이름', 'type': 'Other', 'quote_ref': ref}],
            'aliases': [], 'relations': []}


class FilterRules(unittest.TestCase):
    def sample(self, shared_type=False):
        source = 'Alpha (A) links things.'.ljust(400) + 'Beta (B) is a device.'
        spans = source_spans(source)
        a, b = [spans[0]['id']] * 2, [spans[-1]['id']] * 2
        kind = 'Service' if shared_type else 'Device'
        output = {'entities': [{'id': 'a', 'name': 'Alpha', 'type': 'Service', 'quote_ref': a},
                               {'id': 'b', 'name': 'Beta', 'type': kind, 'quote_ref': a}],
                  'aliases': [{'alias': 'B', 'entity': 'b', 'quote_ref': b}],
                  'relations': [{'id': 'links', 'description': 'Direct link', 'subject_types': ['Service'],
                                 'object_types': [kind], 'quote_ref': a}]}
        return source, spans, output

    def test_normalized_name_keeps_valid_entity_and_dependencies(self):
        source, spans, output = self.sample(shared_type=True)
        output['entities'] = [output['entities'][0]]
        output['entities'][0]['name'] = ' Alpha '
        output['aliases'] = [{'alias': 'A', 'entity': 'a', 'quote_ref': output['entities'][0]['quote_ref']}]
        merged, excluded = merge_generated_dictionary(EMPTY, output, spans, source, source)
        self.assertEqual(merged['entities'][0]['name'], 'Alpha')
        self.assertEqual(len(merged['aliases']), 1)
        self.assertEqual(len(merged['relations']), 1)
        self.assertEqual(excluded, [])

    def test_only_bad_evidence_and_its_dependencies_are_removed(self):
        source, spans, output = self.sample()
        merged, excluded = merge_generated_dictionary(EMPTY, output, spans, source, source)
        self.assertEqual([r['id'] for r in merged['entities']], ['a'])
        self.assertEqual(merged['aliases'], []); self.assertEqual(merged['relations'], [])
        self.assertEqual([r['code'] for r in excluded], ['entity_evidence', 'excluded_entity', 'excluded_type'])

    def test_remaining_same_type_and_previous_valid_entity_are_preserved(self):
        source, spans, output = self.sample(shared_type=True)
        merged, excluded = merge_generated_dictionary(EMPTY, output, spans, source, source)
        self.assertEqual([r['id'] for r in merged['relations']], ['links'])
        self.assertEqual(len(excluded), 2)
        source, spans, output = self.sample()
        previous = {'entities': [{'id': 'b', 'name': 'Beta', 'type': 'Device', 'quote': spans[-1]['text']}], 'aliases': [], 'relations': []}
        merged, excluded = merge_generated_dictionary(previous, output, spans, source, source)
        self.assertEqual(len(merged['entities']), 2)
        self.assertEqual(len(merged['aliases']), 1); self.assertEqual(len(merged['relations']), 1)
        self.assertEqual(len(excluded), 1)

    def test_conflicts_cannot_be_laundered_by_invalid_evidence(self):
        source, spans, output = self.sample()
        previous = {'entities': [{'id': 'a', 'name': 'Alpha', 'type': 'Service', 'quote': spans[0]['text']}], 'aliases': [], 'relations': []}
        output['entities'][0].update(type='Changed', quote_ref=['missing', 'missing'])
        with self.assertRaisesRegex(ValueError, '충돌'):
            merge_generated_dictionary(previous, output, spans, source, source)
        source, spans, output = self.sample()
        duplicate = dict(output['entities'][0], name='Invented', quote_ref=['missing'] * 2)
        output['entities'].append(duplicate)
        with self.assertRaisesRegex(ValueError, '충돌'):
            merge_generated_dictionary(EMPTY, output, spans, source, source)
        source, spans, output = self.sample()
        output['aliases'].append(dict(output['aliases'][0], entity='a', quote_ref=['missing'] * 2))
        with self.assertRaisesRegex(ValueError, '충돌'):
            merge_generated_dictionary(EMPTY, output, spans, source, source)
        source, spans, output = self.sample()
        output['relations'].append(dict(output['relations'][0], description='Different', quote_ref=['missing'] * 2))
        with self.assertRaisesRegex(ValueError, '충돌'):
            merge_generated_dictionary(EMPTY, output, spans, source, source)

    def test_structure_values_and_original_unknown_dependencies_remain_fatal(self):
        for change in (lambda o: o['entities'][1].update(name=123),
                       lambda o: o['entities'][1].update(quote_ref='bad'),
                       lambda o: o['entities'][1].update(quote_ref=['one']),
                       lambda o: o['entities'][1].update(quote_ref=[1, 2]),
                       lambda o: o['entities'][1].update(extra='field'),
                       lambda o: o['aliases'][0].update(entity='never-declared'),
                       lambda o: o['relations'][0].update(object_types=['never-declared']),
                       lambda o: o['relations'][0].update(id='bad relation')):
            source, spans, output = self.sample(); change(output)
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_generated_dictionary(EMPTY, output, spans, source, source)

    def test_unknown_and_reversed_refs_are_recoverable(self):
        for ref in (['missing', 'missing'], None):
            source, spans, output = self.sample()
            output['entities'][1]['quote_ref'] = ref or [spans[-1]['id'], spans[0]['id']]
            merged, excluded = merge_generated_dictionary(EMPTY, output, spans, source, source)
            self.assertEqual(excluded[0]['code'], 'reference')
            self.assertEqual([r['id'] for r in merged['entities']], ['a'])


class Generation(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def new_doc(self, source):
        return self.store.register('synthetic', 'fixture.md', source.encode(), source)

    def run_job(self, doc, outputs):
        pending, calls = iter(outputs), []
        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                payload = json.loads(prompt.split('입력(JSON):\n', 1)[1]); calls.append(payload)
                value = next(pending)
                if isinstance(value, Exception):
                    raise value
                return value(payload) if callable(value) else wire_dictionary(value, prompt)
        service = Service(self.store, self.root, Fake())
        job = service.start(doc['id'], 'agy', '', 'ontology', doc['version'], doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(job['id']), calls

    def checkpoint(self, run):
        return self.root / 'runs' / run['id'] / 'dictionary-checkpoint.json'

    def test_bad_name_does_not_stop_later_chunks_and_stays_pending(self):
        doc = self.new_doc(SOURCE.ljust(4000) * 2 + SOURCE)
        run, calls = self.run_job(doc, [DICTIONARY, bad_name, DICTIONARY])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 3)
        self.assertIn('제외 1개', run['detail'])
        report = read_report(self.root, self.store, run['id'])
        self.assertTrue(report['available']); self.assertEqual(report['total_count'], 1)
        self.assertEqual(report['items'][0]['chunk'], 2)
        item = self.store.document(doc['id'])['items'][0]
        self.assertEqual(item['status'], 'pending'); self.assertEqual(len(item['payload']['entities']), 3)
        self.assertEqual(self.store.snapshot()['facts'], [])

    def test_resume_inherits_exclusions_without_counting_them_twice(self):
        doc = self.new_doc(SOURCE.ljust(4000) * 2 + SOURCE)
        first, _ = self.run_job(doc, [DICTIONARY, bad_name, TimeoutError('fixture timeout')])
        self.assertEqual(first['status'], 'error')
        second, calls = self.run_job(doc, [DICTIONARY])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertEqual([r['offset'] for r in calls], [8000])
        self.assertEqual(read_report(self.root, self.store, second['id'])['total_count'], 1)
        saved = json.loads(self.checkpoint(second).read_text())
        self.assertEqual(saved['exclusions']['identity']['run_id'], second['id'])

    def test_fatal_chunk_rolls_back_exclusions_and_candidate_changes(self):
        doc = self.new_doc(SOURCE.ljust(4000) + SOURCE)
        def fatal(payload):
            result = bad_name(payload)
            result['relations'] = [{'id': 'broken', 'description': 'Unknown type', 'subject_types': ['Unknown'],
                                    'object_types': ['Unknown'], 'quote_ref': [payload['sources'][0]['id']] * 2}]
            return result
        run, _ = self.run_job(doc, [DICTIONARY, fatal])
        self.assertEqual(run['status'], 'error')
        saved = json.loads(self.checkpoint(run).read_text())
        self.assertEqual(saved['offset'], 4000)
        self.assertEqual(read_report(self.root, self.store, run['id'])['total_count'], 0)
        self.assertEqual(self.store.document(doc['id'])['items'], [])

    def test_strict_legacy_checkpoint_reuses_validated_prefix(self):
        doc = self.new_doc(SOURCE.ljust(4000) + SOURCE)
        first, _ = self.run_job(doc, [DICTIONARY, TimeoutError('fixture timeout')])
        path = self.checkpoint(first); saved = json.loads(path.read_text())
        saved['schema_version'] = 1; saved.pop('exclusions'); saved['contract'].pop('filter_policy')
        path.write_text(json.dumps(saved))
        second, calls = self.run_job(doc, [DICTIONARY])
        self.assertEqual(second['status'], 'success', second['detail'])
        self.assertEqual([r['offset'] for r in calls], [4000])
        self.assertEqual(read_report(self.root, self.store, second['id'])['total_count'], 0)

    def test_invalid_warning_checkpoint_or_future_policy_restarts(self):
        for mutate in (lambda c: c['exclusions']['identity'].update(version=True),
                       lambda c: c['exclusions']['items'].clear(),
                       lambda c: c['contract'].update(filter_policy=99),
                       lambda c: c.update(schema_version=99)):
            doc = self.new_doc(SOURCE.ljust(4000) + SOURCE)
            first, _ = self.run_job(doc, [bad_name, TimeoutError('fixture timeout')])
            path = self.checkpoint(first); saved = json.loads(path.read_text()); mutate(saved); path.write_text(json.dumps(saved))
            second, calls = self.run_job(doc, [DICTIONARY, DICTIONARY])
            self.assertEqual(second['status'], 'success', second['detail'])
            self.assertEqual([r['offset'] for r in calls], [0, 4000])

    def test_missing_or_wrong_report_is_unknown_not_zero(self):
        doc = self.new_doc(SOURCE)
        run, _ = self.run_job(doc, [bad_name])
        path = self.root / 'runs' / run['id'] / 'dictionary-exclusions.json'
        path.unlink()
        report = read_report(self.root, self.store, run['id'])
        self.assertFalse(report['available']); self.assertEqual(report['total_count'], 1)
        self.assertTrue(report['partial'])
        self.checkpoint(run).unlink()
        self.assertIsNone(read_report(self.root, self.store, run['id'])['total_count'])
        self.assertFalse(read_report(self.root, self.store, run['id'], document_id='other')['available'])

    def test_report_limit_counts_every_exclusion_and_exposes_truncation(self):
        doc = self.new_doc('x' * 8000)
        def crowded(payload):
            ref = [payload['sources'][0]['id']] * 2
            return {'entities': [{'id': f'e{i}', 'name': 'Absent', 'type': 'Thing', 'quote_ref': ref} for i in range(LIMITS['entities'])],
                    'aliases': [{'alias': f'a{i}', 'entity': 'e0', 'quote_ref': ref} for i in range(LIMITS['aliases'])],
                    'relations': [{'id': f'relation_{i}', 'description': 'Direct relation', 'subject_types': ['Thing'], 'object_types': ['Thing'], 'quote_ref': ref} for i in range(LIMITS['relations'])]}
        run, calls = self.run_job(doc, [crowded, crowded])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 2)
        report = read_report(self.root, self.store, run['id'])
        self.assertEqual(report['total_count'], 1280); self.assertEqual(len(report['items']), MAX_DETAILS)
        self.assertEqual(report['truncated_count'], 1280 - MAX_DETAILS)
        self.assertEqual(report['direct_count'], 400); self.assertEqual(report['dependent_count'], 880)
        self.assertLess(self.checkpoint(run).stat().st_size, MAX_REPORT_BYTES)
        saved = json.loads(self.checkpoint(run).read_text())['exclusions']
        validate_report(saved, run, doc, len(doc['text']))

    def test_wrong_source_oversized_and_symlink_details_are_not_trusted(self):
        for mode in ('identity', 'oversized', 'symlink'):
            doc = self.new_doc(SOURCE)
            run, _ = self.run_job(doc, [bad_name])
            path = self.root / 'runs' / run['id'] / 'dictionary-exclusions.json'
            if mode == 'identity':
                value = json.loads(path.read_text()); value['identity']['source_sha256'] = 'wrong'
                path.write_text(json.dumps(value))
            elif mode == 'oversized':
                path.write_text(' ' * (MAX_REPORT_BYTES + 1))
            else:
                outside = self.root / 'untrusted.json'; outside.write_bytes(path.read_bytes())
                path.unlink(); path.symlink_to(outside)
            report = read_report(self.root, self.store, run['id'])
            self.assertFalse(report['available']); self.assertEqual(report['total_count'], 1)
            self.assertEqual(report['items'], [])

    def test_optional_report_write_failure_preserves_result_and_warns(self):
        doc = self.new_doc(SOURCE)
        with patch('verinote.dictionary_pipeline.write_report', return_value=False):
            run, _ = self.run_job(doc, [bad_name])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('제외 상세 저장 실패', run['detail'])
        report = read_report(self.root, self.store, run['id'])
        self.assertFalse(report['available']); self.assertEqual(report['total_count'], 1)


class PublicWarnings(unittest.TestCase):
    setUp = integration.Integration.setUp
    close = integration.Integration.close
    request = integration.Integration.request
    upload = integration.Integration.upload
    finish = integration.Integration.finish
    start = integration.Integration.start
    audit = integration.Integration.audit

    def test_warning_visible_before_approval_and_call_verdict_is_failed(self):
        doc = self.upload()
        def output(prompt):
            payload = json.loads(prompt.split('입력(JSON):\n', 1)[1])
            result = bad_name(payload)
            result['entities'].insert(0, {'id': 'order', 'name': '주문', 'type': 'Service', 'quote_ref': [payload['sources'][0]['id']] * 2})
            return result
        fixture = Fixture([output])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            run = self.finish(self.start(doc))
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(run['usage']['details'][0]['caller_validation_status'], 'failed')
        item = self.request('/api/documents/' + doc['id'])['items'][0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['dictionary_exclusions']['total_count'], 1)
        self.assertEqual(self.store.snapshot()['facts'], [])
        summary = self.request('/api/runs')[0]['dictionary_exclusions']
        self.assertFalse(summary['details_loaded']); self.assertEqual(summary['items'], [])
        self.audit()


class WarningUI(unittest.TestCase):
    def test_warning_details_escape_and_missing_truncated_states(self):
        source = Path('verinote/web/app.js').read_text()
        helper = source[source.index('function renderDictionaryExclusions('):source.index('function renderDictionary(p)')]
        script = r'''
const assert=require('node:assert/strict');const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
''' + helper + r'''
const report={available:true,total_count:2,direct_count:1,dependent_count:1,truncated_count:1,details_loaded:true,items:[{chunk:2,kind:'entities',row:17,identifier:'<img>',reason:'<script>'}]};
const html=renderDictionaryExclusions(report);assert(html.includes('2개 제외'));assert(html.includes('구간 2'));assert(html.includes('상세는 생략'));assert(!html.includes('<img>'));assert(!html.includes('<script>'));
assert.equal(renderDictionaryExclusions({available:true,total_count:0}),'');
const missing=renderDictionaryExclusions({available:false,total_count:2,reason:'상세 없음'});assert(missing.includes('최소 2개'));assert(missing.includes('상세 없음'));
assert(renderDictionaryExclusions({available:false,total_count:null}).includes('제외가 없었다는 뜻은 아닙니다'));
'''
        subprocess.run(['node', '-e', script], check=True)
        item = source[source.index('function renderItem('):source.index('function renderProgress(')]
        self.assertLess(item.index('renderDictionaryExclusions'), item.index('data-review'))
