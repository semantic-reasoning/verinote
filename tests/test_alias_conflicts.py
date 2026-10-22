"""Only new aliases colliding with prior entity IDs are recoverable."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_openrouter_integration as integration
import test_workflow as workflow
from test_openrouter_pipeline import Fixture

from verinote.dictionary_exclusions import (
    add_chunk,
    new_report,
    public_report,
    read_report,
    validate_report,
)
from verinote.dictionary_filter import merge_generated_dictionary
from verinote.documents import bounded_spans
from verinote.ontology import source_spans, validate_dictionary
from verinote.service import Service

SOURCE = '테디노트 이경록 김철수.'
EMPTY = {'entities': [], 'aliases': [], 'relations': []}
PREVIOUS = {'entities': [{'id': '테디노트', 'name': '테디노트', 'type': '주제어', 'quote': SOURCE}],
            'aliases': [], 'relations': []}


def output(payload, mode='collision'):
    ref = [payload['sources'][0]['id']] * 2
    if mode == 'initial':
        return {'entities': [{k: v for k, v in PREVIOUS['entities'][0].items() if k != 'quote'} | {'quote_ref': ref}],
                'aliases': [], 'relations': []}
    if mode == 'empty': return copy.deepcopy(EMPTY)
    return {'entities': [{'id': '이경록', 'name': '이경록', 'type': '인물', 'quote_ref': ref}],
            'aliases': [{'alias': '경록', 'entity': '이경록', 'quote_ref': ref},
                        {'alias': '테디노트', 'entity': '이경록', 'quote_ref': ref}], 'relations': []}


class Filter(unittest.TestCase):
    def merge(self, value, previous=PREVIOUS):
        return merge_generated_dictionary(previous, value, source_spans(SOURCE), SOURCE, SOURCE)

    def sample(self):
        return output({'sources': source_spans(SOURCE)})

    def test_preserve_prior_entities_and_valid_new_items_drop_only_alias(self):
        before = copy.deepcopy(PREVIOUS)
        merged, excluded = self.merge(self.sample())
        self.assertEqual(PREVIOUS, before)
        self.assertEqual(merged['entities'][0], PREVIOUS['entities'][0])
        self.assertEqual([a['alias'] for a in merged['aliases']], ['경록'])
        self.assertEqual(excluded, [{'kind': 'aliases', 'row': 2, 'identifier': '테디노트', 'code': 'alias_existing_entity'}])
        self.assertEqual(validate_dictionary(merged, SOURCE), merged)
        invalid = copy.deepcopy(merged); invalid['aliases'].append({'alias': '테디노트', 'entity': '이경록', 'quote': SOURCE})
        with self.assertRaises(ValueError): validate_dictionary(invalid, SOURCE)

    def test_same_response_new_entity_collision_stays_fatal(self):
        value = self.sample(); value['entities'].insert(0, output({'sources': source_spans(SOURCE)}, 'initial')['entities'][0])
        with self.assertRaisesRegex(ValueError, '충돌'): self.merge(value, EMPTY)
        merged, excluded = self.merge(value)
        self.assertEqual(len(merged['entities']), 2); self.assertEqual(len(excluded), 1)

    def test_conflicts_and_malformed_rows_cannot_be_hidden_by_exclusion(self):
        for mutate in (lambda v: v['aliases'].append(v['aliases'][1] | {'entity': '테디노트'}),
                       lambda v: v['aliases'][1].update(entity='unknown'),
                       lambda v: v['aliases'][1].update(quote_ref=[1, 2]),
                       lambda v: v['aliases'][1].update(extra=True),
                       lambda v: v['entities'].append(output({'sources': source_spans(SOURCE)}, 'initial')['entities'][0] | {'type': '다른타입'})):
            value = self.sample(); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.merge(value)

    def test_old_alias_target_and_reverse_namespace_conflicts_stay_fatal(self):
        previous = copy.deepcopy(PREVIOUS)
        previous['entities'].append({'id': '이경록', 'name': '이경록', 'type': '인물', 'quote': SOURCE})
        previous['aliases'] = [{'alias': '경록', 'entity': '이경록', 'quote': SOURCE}]
        value = self.sample(); value['aliases'][0]['entity'] = '테디노트'
        with self.assertRaisesRegex(ValueError, '충돌'): self.merge(value, previous)
        value = self.sample(); value['entities'].append({'id': '경록', 'name': '이경록', 'type': '인물', 'quote_ref': value['entities'][0]['quote_ref']})
        with self.assertRaisesRegex(ValueError, '충돌'): self.merge(value, previous)

    def test_normalization_identity_and_prior_reference_errors(self):
        value = self.sample(); value['aliases'][1].update(alias=' 테디노트 ', entity=' 이경록 ')
        merged, excluded = self.merge(value)
        self.assertEqual(excluded[0]['identifier'], '테디노트')
        value = self.sample(); value['aliases'][1]['entity'] = '테디노트'
        merged, excluded = self.merge(value)
        self.assertEqual(excluded, []); self.assertEqual([r['alias'] for r in merged['aliases']], ['경록'])
        value = self.sample(); value['aliases'][1]['quote_ref'] = ['missing'] * 2
        _, excluded = self.merge(value)
        self.assertEqual(excluded[0]['code'], 'alias_existing_entity')

    def test_audit_kind_policy_and_cap_validation(self):
        source = (SOURCE.ljust(3988, 'x') + '\n') * 2 + SOURCE.ljust(1058, 'x')
        run = {'id': 'r', 'document_id': 'd', 'version': 1}; doc = {'text': source, 'sha256': 'hash'}
        report = new_report(run, doc)
        for index, (offset, chunk) in enumerate(bounded_spans(source, 4000)):
            rows = [] if index == 0 else [{'kind': 'aliases', 'row': n, 'identifier': '테디노트', 'code': 'alias_existing_entity'} for n in range(1, 401)]
            report = add_chunk(report, offset, len(chunk), rows)
        validate_report(report, run, doc, len(source), filter_policy=2)
        public = public_report(report)
        self.assertEqual((public['total_count'], public['direct_count'], public['dependent_count'], public['truncated_count']), (800, 800, 0, 300))
        with self.assertRaises(ValueError): validate_report(report, run, doc, filter_policy=1)
        wrong = copy.deepcopy(report); wrong['items'][0]['kind'] = 'entities'
        with self.assertRaises(ValueError): validate_report(wrong, run, doc)


class Pipeline(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def execute(self, doc, modes):
        modes, calls = iter(modes), []
        class Fake:
            def complete(adapter, agent, model, prompt, folder, *, phase=None):
                payload = json.loads(prompt.split('입력(JSON):\n', 1)[1]); calls.append(payload['offset'])
                mode = next(modes)
                if isinstance(mode, Exception): raise mode
                return output(payload, mode)
        service = Service(self.store, self.root, Fake())
        run = service.start(doc['id'], 'agy', '', 'ontology', 1, 1)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), calls

    def checkpoint(self, run):
        return self.root / 'runs' / run['id'] / 'dictionary-checkpoint.json'

    def new_doc(self):
        text = (SOURCE.ljust(3988, 'x') + '\n') * 2 + SOURCE.ljust(1058, 'x')
        return self.store.register('fixture', 'fixture.md', text.encode(), text)

    def test_v1_3989_prefix_resumes_and_saves_v2_audit_without_extra_calls(self):
        doc = self.new_doc(); first, _ = self.execute(doc, ['initial', ValueError('stop')])
        path = self.checkpoint(first); saved = json.loads(path.read_text()); saved['contract']['filter_policy'] = 1
        path.write_text(json.dumps(saved))
        (path.parent / 'dictionary-exclusions.json').unlink()
        self.assertTrue(read_report(self.root, self.store, first['id'])['partial'])
        second, calls = self.execute(doc, ['collision', 'empty'])
        self.assertEqual(second['status'], 'success', second['detail']); self.assertEqual(calls, [3989, 7978])
        self.assertIn('3,989자 재사용', second['detail'])
        saved = json.loads(self.checkpoint(second).read_text()); self.assertEqual(saved['contract']['filter_policy'], 2)
        audit = read_report(self.root, self.store, second['id'])
        self.assertEqual(audit['total_count'], 1); self.assertEqual(audit['items'][0]['code'], 'alias_existing_entity')
        item = self.store.document(doc['id'])['items'][0]
        self.assertEqual(item['status'], 'pending'); self.assertEqual([a['alias'] for a in item['payload']['aliases']], ['경록'])
        self.assertIn('기존 개체 ID', audit['items'][0]['reason'])

    def test_v1_cannot_claim_new_reason_and_unknown_policies_restart(self):
        for policy in (1, 99):
            doc = self.new_doc(); first, _ = self.execute(doc, ['initial', 'collision', ValueError('stop')])
            path = self.checkpoint(first); saved = json.loads(path.read_text()); saved['contract']['filter_policy'] = policy
            path.write_text(json.dumps(saved)); (path.parent / 'dictionary-exclusions.json').unlink()
            self.assertFalse(read_report(self.root, self.store, first['id'])['available'])
            second, calls = self.execute(doc, ['initial', 'collision', 'empty'])
            self.assertEqual(second['status'], 'success', second['detail']); self.assertEqual(calls, [0, 3989, 7978])

    def test_v1_source_mismatch_does_not_reuse_prefix(self):
        doc = self.new_doc(); first, _ = self.execute(doc, ['initial', ValueError('stop')])
        path = self.checkpoint(first); saved = json.loads(path.read_text())
        saved['contract'].update(filter_policy=1, source_sha256='other'); path.write_text(json.dumps(saved))
        second, calls = self.execute(doc, ['initial', 'collision', 'empty'])
        self.assertEqual(second['status'], 'success', second['detail']); self.assertEqual(calls[0], 0)


class PublicWarning(unittest.TestCase):
    setUp = integration.Integration.setUp
    close = integration.Integration.close
    request = integration.Integration.request
    upload = integration.Integration.upload
    finish = integration.Integration.finish
    start = integration.Integration.start
    audit = integration.Integration.audit

    def test_preapproval_warning_and_partial_caller_failure(self):
        source = (SOURCE.ljust(3988, 'x') + '\n') + SOURCE
        doc = self.upload(source)
        def initial(prompt): return output(json.loads(prompt.split('입력(JSON):\n', 1)[1]), 'initial')
        def collision(prompt): return output(json.loads(prompt.split('입력(JSON):\n', 1)[1]))
        with patch('verinote.openrouter._HTTPS', side_effect=Fixture([initial, collision])):
            run = self.finish(self.start(doc))
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual([c['caller_validation_status'] for c in run['usage']['details']], ['success', 'failed'])
        item = self.request('/api/documents/' + doc['id'])['items'][0]
        self.assertEqual(item['status'], 'pending'); self.assertEqual(item['dictionary_exclusions']['total_count'], 1)
        self.assertIn('기존 개체 ID', item['dictionary_exclusions']['items'][0]['reason'])
        self.audit()


class UI(unittest.TestCase):
    def test_direct_exclusion_label_covers_namespace_conflict(self):
        source = Path('verinote/web/app.js').read_text()
        self.assertIn('직접 제외 ${report.direct_count.toLocaleString()}개', source)
        self.assertNotIn('근거 오류 ${report.direct_count.toLocaleString()}개', source)
