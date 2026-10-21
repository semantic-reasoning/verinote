"""Relation type breadth is advisory; valid definitions are never truncated."""
import copy
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_openrouter_integration as integration
import test_workflow as workflow
from test_openrouter_pipeline import Fixture

from verinote.config import ConfigError, Credentials, load_config
from verinote.dictionary_filter import merge_generated_dictionary
from verinote.dictionary_type_warnings import read_run_warnings, type_warnings
from verinote.ontology import dictionary_fact, source_spans, validate_dictionary
from verinote.service import Service

SOURCE = 'Node links Node.'


def dictionary(count=32):
    return {'entities': [{'id': f'node{i}', 'name': 'Node', 'type': f'T{i}', 'quote': SOURCE} for i in range(count)],
            'aliases': [], 'relations': [{'id': 'links', 'description': 'Direct link',
                                        'subject_types': ['T0'], 'object_types': [f'T{i}' for i in range(count)], 'quote': SOURCE}]}


def wire(payload, count=32):
    result = dictionary(count)
    ref = [payload['sources'][0]['id']] * 2
    for rows in result.values():
        for row in rows:
            row.pop('quote'); row['quote_ref'] = ref
    return result


class TypePolicy(unittest.TestCase):
    def test_32_types_survive_generation_approval_and_extraction_validation(self):
        value = validate_dictionary(dictionary(), SOURCE)
        payload = wire({'sources': source_spans(SOURCE)})
        merged, excluded = merge_generated_dictionary({'entities': [], 'aliases': [], 'relations': []}, payload,
                                                       source_spans(SOURCE), SOURCE, SOURCE)
        self.assertEqual(value, merged); self.assertEqual(excluded, [])
        fact = {'subject': 'node0', 'predicate': 'links', 'object': 'node31', 'quote': SOURCE}
        self.assertEqual(dictionary_fact(fact, SOURCE, value), fact)
        report = type_warnings(value)
        self.assertEqual((report['relation_count'], report['field_count']), (1, 1))
        self.assertEqual(report['items'][0]['count'], 32)
        self.assertEqual(type_warnings(value, 32)['items'], [])
        both = copy.deepcopy(value); both['relations'][0]['subject_types'] = both['relations'][0]['object_types']
        self.assertEqual(type_warnings(both)['field_count'], 2)

    def test_boundaries_and_duplicates_use_unique_types(self):
        self.assertEqual(type_warnings(dictionary(20))['field_count'], 0)
        self.assertEqual(type_warnings(dictionary(21))['field_count'], 1)
        value = dictionary(20); value['relations'][0]['object_types'] *= 10
        result = validate_dictionary(value, SOURCE)
        self.assertEqual(len(result['relations'][0]['object_types']), 20)
        self.assertEqual(type_warnings(result)['field_count'], 0)
        self.assertEqual(type_warnings(result, 1)['field_count'], 1)

    def test_breadth_does_not_hide_shape_unknown_types_or_conflicts(self):
        for field in ([], 'T0', [True], ['unknown']):
            value = dictionary(); value['relations'][0]['object_types'] = field
            with self.subTest(field=field), self.assertRaises(ValueError): validate_dictionary(value, SOURCE)
        payload = wire({'sources': source_spans(SOURCE)})
        payload['relations'][0]['description'] = 'Conflicting description'
        with self.assertRaisesRegex(ValueError, '충돌'):
            merge_generated_dictionary(dictionary(), payload, source_spans(SOURCE), SOURCE, SOURCE)
        huge = dictionary(200)
        for entity in huge['entities']: entity['id'] += 'x' * 180
        with patch('verinote.ontology.MAX_DICTIONARY_CONTEXT_BYTES', 1000), self.assertRaisesRegex(ValueError, '문맥'):
            validate_dictionary(huge, SOURCE)

    def test_config_defaults_range_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'; path.touch(mode=0o600)
            for number in (1, 20, 40, 200):
                path.write_text(f'[openrouter]\napi_key="synthetic-secret"\n[dictionary]\nrelation_type_warning_threshold={number}\n')
                config = load_config(path)
                self.assertEqual(config.relation_type_warning_threshold, number)
                self.assertNotIn('synthetic-secret', repr(config))
                self.assertEqual(config.public_status(), {'openrouter': {'configured': True}})
            for invalid in ('true', '0', '-1', '201', '20.0', '"synthetic-secret"', '[]'):
                path.write_text(f'[dictionary]\nrelation_type_warning_threshold={invalid}\n')
                with self.assertRaises(ConfigError) as caught: load_config(path)
                self.assertNotIn('synthetic-secret', str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
            path.write_text(''); self.assertEqual(load_config(path).relation_type_warning_threshold, 20)
        self.assertEqual(Credentials().relation_type_warning_threshold, 20)
        for invalid in (True, 0, 201, 1.5):
            with self.assertRaises(ValueError): type_warnings(dictionary(), invalid)


class Generation(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def new_doc(self, long=False):
        text = SOURCE.ljust(4000) + SOURCE if long else SOURCE
        return self.store.register('types', 'types.md', text.encode(), text)

    def execute(self, doc, outputs, threshold=20):
        outputs, calls = iter(outputs), []
        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                payload = json.loads(prompt.split('입력(JSON):\n', 1)[1]); calls.append(payload)
                result = next(outputs)
                if isinstance(result, Exception): raise result
                return wire(payload, result)
        service = Service(self.store, self.root, Fake(), relation_type_warning_threshold=threshold)
        run = service.start(doc['id'], 'agy', '', 'ontology', 1, 1)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), calls

    def checkpoint(self, run):
        return self.root / 'runs' / run['id'] / 'dictionary-checkpoint.json'

    def test_warning_continues_without_calls_or_exclusions_and_can_approve(self):
        doc = self.new_doc(); run, calls = self.execute(doc, [32])
        self.assertEqual(run['status'], 'success', run['detail']); self.assertEqual(len(calls), 1)
        self.assertIn('모든 타입 보존', run['detail'])
        item = self.store.document(doc['id'])['items'][0]
        self.assertEqual(len(item['payload']['relations'][0]['object_types']), 32)
        self.assertEqual(item['status'], 'pending')
        self.store.review(item['id'], 1, 'revise', 'Reviewer', 'same valid definition', item['payload'])
        self.store.review(item['id'], 2, 'approved', 'Reviewer', 'checked breadth')
        self.assertTrue(self.store.document(doc['id'])['can_extract'])
        report = read_run_warnings(self.root, self.store, run['id'])
        self.assertEqual((report['threshold'], report['field_count'], report['partial']), (20, 1, False))
        raw = json.loads(self.checkpoint(run).read_text())
        self.assertEqual(sum(row['direct'] for row in raw['exclusions']['chunks']), 0)

    def test_resume_preserves_warning_and_failing_chunk_does_not_count(self):
        doc = self.new_doc(True)
        failed, calls = self.execute(doc, [32, ValueError('fixture stop')])
        self.assertEqual(failed['status'], 'error'); self.assertEqual(len(calls), 2)
        report = read_run_warnings(self.root, self.store, failed['id'])
        self.assertTrue(report['partial']); self.assertEqual(report['field_count'], 1)
        resumed, calls = self.execute(doc, [32])
        self.assertEqual(resumed['status'], 'success', resumed['detail']); self.assertEqual(len(calls), 1)
        self.assertIn(failed['id'], resumed['detail'])
        self.assertEqual(read_run_warnings(self.root, self.store, resumed['id'])['field_count'], 1)

    def test_threshold_change_restarts_and_legacy_only_compatible_at_default(self):
        doc = self.new_doc(True); failed, _ = self.execute(doc, [20, ValueError('stop')])
        raw = json.loads(self.checkpoint(failed).read_text())
        raw['contract'].pop('type_warning_policy'); raw['contract'].pop('relation_type_warning_threshold')
        self.checkpoint(failed).write_text(json.dumps(raw))
        self.assertEqual(read_run_warnings(self.root, self.store, failed['id'])['threshold'], 20)
        resumed, calls = self.execute(doc, [ValueError('stop')])
        self.assertEqual(len(calls), 1); self.assertIn('4,000자 재사용', resumed['detail'])
        changed, calls = self.execute(doc, [20, 20], 40)
        self.assertEqual(changed['status'], 'success', changed['detail']); self.assertEqual(len(calls), 2)
        self.assertEqual(read_run_warnings(self.root, self.store, changed['id'])['threshold'], 40)

    def test_missing_corrupt_foreign_or_future_report_is_unknown(self):
        doc = self.new_doc(); run, _ = self.execute(doc, [32]); path = self.checkpoint(run); original = path.read_text()
        for change in (lambda r: r.update(run_id='foreign'), lambda r: r['contract'].update(source_sha256='other'),
                       lambda r: r.update(offset=True), lambda r: r['contract'].update(type_warning_policy=99),
                       lambda r: r['contract'].update(relation_type_warning_threshold=True)):
            raw = json.loads(original); change(raw); path.write_text(json.dumps(raw))
            report = read_run_warnings(self.root, self.store, run['id'])
            self.assertFalse(report['available']); self.assertNotIn('field_count', report)
        path.unlink(); self.assertFalse(read_run_warnings(self.root, self.store, run['id'])['available'])

    def test_queued_job_captures_threshold(self):
        doc = self.new_doc(); gate = threading.Event()
        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                return wire(json.loads(prompt.split('입력(JSON):\n', 1)[1]))
        service = Service(self.store, self.root, Fake(), relation_type_warning_threshold=20)
        service.pool.submit(lambda: gate.wait(5))
        try:
            run = service.start(doc['id'], 'agy', '', 'ontology', 1, 1)
            service.relation_type_warning_threshold = 40
        finally:
            gate.set(); service.pool.shutdown(wait=True)
        report = read_run_warnings(self.root, self.store, run['id'])
        self.assertEqual((report['threshold'], report['field_count']), (20, 1))


class WarningHTTP(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_current_and_historical_threshold_and_preapproval_report(self):
        doc = self.request('/api/documents', 'POST', {'title': 'types', 'filename': 't.md', 'text': SOURCE})
        with patch.object(self.service.cli, 'complete', side_effect=lambda a, m, p, f, **kw: wire(json.loads(p.split('입력(JSON):\n', 1)[1]))):
            run = self.request(f'/api/documents/{doc["id"]}/ontology', 'POST', {'agent': 'agy', 'expected_version': 1, 'expected_generation': 1})
            self.service.pool.shutdown(wait=True)
        item = self.request(f'/api/documents/{doc["id"]}')['items'][0]
        self.assertEqual(item['status'], 'pending'); self.assertEqual(item['relation_type_warnings']['field_count'], 1)
        self.service.relation_type_warning_threshold = 40
        item = self.request(f'/api/documents/{doc["id"]}')['items'][0]
        self.assertEqual(item['relation_type_warnings']['field_count'], 0)
        old = self.request('/api/runs/' + run['id'])['relation_type_warnings']
        self.assertEqual((old['threshold'], old['field_count']), (20, 1))
        self.assertEqual(self.request('/api/settings')['dictionary']['relation_type_warning_threshold'], 40)
        listed = next(r for r in self.request('/api/runs') if r['id'] == run['id'])['relation_type_warnings']
        self.assertEqual(listed['field_count'], 1); self.assertEqual(listed['items'], [])


class CallerVerdict(unittest.TestCase):
    setUp = integration.Integration.setUp
    close = integration.Integration.close
    request = integration.Integration.request
    upload = integration.Integration.upload
    finish = integration.Integration.finish
    start = integration.Integration.start
    audit = integration.Integration.audit

    def test_warning_alone_is_success_and_mixed_exclusions_remain_failed(self):
        for exclude in (False, True):
            with self.subTest(exclude=exclude):
                doc = self.upload()
                def output(prompt, exclude=exclude):
                    payload = json.loads(prompt.split('입력(JSON):\n', 1)[1])
                    result = wire(payload)
                    for row in result['entities']: row['name'] = '주문'
                    if exclude:
                        result['entities'].append({'id': 'ghost', 'name': 'missingname', 'type': 'Other',
                                                   'quote_ref': result['entities'][0]['quote_ref']})
                    return result
                fixture = Fixture([output])
                with patch('verinote.openrouter._HTTPS', side_effect=fixture):
                    run = self.finish(self.start(doc))
                self.assertEqual(run['status'], 'success', run['detail'])
                self.assertEqual(run['relation_type_warnings']['field_count'], 1)
                self.assertEqual(run['usage']['details'][0]['caller_validation_status'], 'failed' if exclude else 'success')
                self.assertEqual(run['dictionary_exclusions']['total_count'], int(exclude))
                self.audit()


class WarningUI(unittest.TestCase):
    def test_preservation_current_historical_partial_and_escape(self):
        source = Path('verinote/web/app.js').read_text()
        function = source[source.index('function renderRelationTypeWarnings'):source.index('function renderDictionaryExclusions')]
        script = "const esc=s=>String(s).replaceAll('<','&lt;');\n" + function + """
const base={available:true,threshold:20,field_count:1,relation_count:1,items:[{relation_id:'<unsafe>',field:'object_types',count:32}]};
const current=renderRelationTypeWarnings(base);if(!current.includes('현재 서버 설정')||!current.includes('&lt;unsafe>')||!current.includes('모든 타입 보존'))process.exit(1);
const old=renderRelationTypeWarnings({...base,basis:'execution_configuration',partial:true});if(!old.includes('실행 당시 설정')||!old.includes('완료한 구간만'))process.exit(2);
if(!renderRelationTypeWarnings({available:false,reason:'missing'}).includes('미확인'))process.exit(3);
"""
        subprocess.run(['node', '-e', script], check=True)
        self.assertIn("item.kind==='ontology'?renderRelationTypeWarnings(item.relation_type_warnings)+", source)
