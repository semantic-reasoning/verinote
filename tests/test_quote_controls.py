"""Literal source controls survive quotations, never authored dictionary fields."""
import copy
import unittest
from unittest.mock import Mock, patch

import test_dictionary_pipeline as pipeline
from test_workflow import DICTIONARY, extraction_response

from verinote.documents import decode_document
from verinote.entity_identity import _pin, identity_graph
from verinote.ontology import (
    apply_quote_repairs,
    extraction_quote_issues,
    validate_dictionary,
)
from verinote.service import Service, evidence, quote_text, text
from verinote.store import digest

SOURCE = '주문\x01은 결제에 의존한다.\r\n결제\x0c는 원장에 의존한다.'


def dictionary():
    result = copy.deepcopy(DICTIONARY)
    for rows in result.values():
        for row in rows:
            row['quote'] = SOURCE
    return result


def extraction():
    output = extraction_response()
    for row in [*output['claims'], *output['facts']]:
        row['quote'] = SOURCE
    return output


class QuoteControls(unittest.TestCase):
    def test_ingestion_and_literal_controls_agree_without_changing_source(self):
        controls = ''.join(chr(i) for i in range(1, 32))
        source = '앞'+controls+'뒤 한글é'
        self.assertEqual(decode_document('control.txt', source.encode()), source)
        reader = Mock(is_encrypted=False, pages=[Mock(extract_text=lambda: source)])
        with patch('pypdf.PdfReader', return_value=reader):
            self.assertEqual(decode_document('control.pdf', b'synthetic-pdf'), source)
        self.assertEqual(evidence(source, source), source)
        self.assertEqual(evidence(' \r\n'+source+'\t ', source), source)
        with self.assertRaisesRegex(ValueError, '원문에 없는'):
            evidence('앞\x02뒤', '앞\x01뒤')
        with self.assertRaisesRegex(ValueError, '원문에 없는'):
            evidence('e\u0301', 'é')  # No hidden Unicode normalization.

    def test_shape_limits_nul_and_control_only_quotes_remain_invalid(self):
        for value in (None, 7, [], {}, '', ' \r\n\t', '\x01\x02\n\t', '본문\x00', 'x'*8001, ' '+ 'x'*8000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                quote_text(value)
        self.assertEqual(quote_text('x'*8000), 'x'*8000)
        self.assertEqual(quote_text('x'*7999+' '), 'x'*7999)
        with self.assertRaises(ValueError):
            evidence('본문\x00', '본문\x00')

    def test_authored_fields_stay_strict(self):
        for control in ('\x01', '\x0c', '\x1b', '\r'):
            with self.subTest(control=repr(control)):
                with self.assertRaises(ValueError): text('이름'+control, 'name')
                bad = dictionary(); bad['entities'][0]['type'] += control
                with self.assertRaises(ValueError): validate_dictionary(bad, SOURCE)
                bad_output = extraction(); bad_output['claims'][0]['text'] += control
                with self.assertRaises(ValueError): extraction_quote_issues(bad_output, SOURCE, dictionary())

    def test_extraction_exact_missing_ambiguous_and_repair_keep_contract(self):
        output = extraction(); before = copy.deepcopy(output)
        self.assertEqual(extraction_quote_issues(output, SOURCE, dictionary()), [])
        for version in (0, 1, 2):
            issues = extraction_quote_issues(output, SOURCE+'\n'+SOURCE, dictionary(), contract_version=version)
            self.assertEqual(len(issues), 3 if version == 2 else 0)
            if version == 2: self.assertEqual({v['reason'] for v in issues}, {'ambiguous'})
        output['facts'][0]['quote'] = SOURCE.replace('\x01', '\x02')
        issues = extraction_quote_issues(output, SOURCE, dictionary())
        self.assertEqual([(v['path'], v['reason']) for v in issues], [('facts.0.quote', 'missing')])
        fixed, changes = apply_quote_repairs(output, SOURCE, issues,
                                            {'repairs': [{'path': 'facts.0.quote', 'quote': SOURCE}]})
        self.assertEqual(fixed, before); self.assertEqual(changes[0]['after'], SOURCE)
        self.assertEqual(extraction_quote_issues(fixed, SOURCE, dictionary()), [])

    def test_pin_still_requires_hash_normalized_rows_and_strict_definitions(self):
        payload = validate_dictionary(dictionary(), SOURCE)
        pin = {'id': 'dictionary', 'revision': 1, 'payload': payload, 'sha256': digest(payload)}
        self.assertEqual(_pin(pin), pin)
        self.assertIsNone(_pin(pin | {'sha256': '0'*64}))
        for field, value in (('name', '주문\x01'), ('quote', ' '+SOURCE), ('quote', 'x\x00'), ('quote', '\x01')):
            bad = copy.deepcopy(payload); bad['entities'][0][field] = value
            self.assertIsNone(_pin(pin | {'payload': bad, 'sha256': digest(bad)}))


class SourceControlWorkflow(unittest.TestCase):
    setUp = pipeline.Pipeline.setUp
    tearDown = pipeline.Pipeline.tearDown
    new_doc = pipeline.Pipeline.new_doc
    run_job = pipeline.Pipeline.run_job
    checkpoint = pipeline.Pipeline.checkpoint

    def test_dictionary_generation_approval_extraction_and_identity_preserve_source(self):
        doc = self.new_doc(SOURCE)
        original_metadata = self.store.document_metadata(doc['id'], 1)
        result, calls = self.run_job(doc, [dictionary()])
        self.assertEqual(result['status'], 'success', result['detail'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(''.join(s['text'] for s in calls[0]['sources']), SOURCE)
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(item['payload'], dictionary())
        self.assertEqual(item['status'], 'pending')
        self.store.review(item['id'], item['revision'], 'approved', 'test', 'synthetic source')
        graph = identity_graph(self.store.snapshot())
        self.assertEqual(len(graph['entities']), 3)
        self.assertTrue(all(e['identity_status'] == 'pinned' and e['quote'] == SOURCE for e in graph['entities']))
        class Fake:
            def complete(self, *args, **kwargs): return extraction()
        service = Service(self.store, self.root, Fake()); self.addCleanup(service.close)
        run = service.start(doc['id'], 'copilot', 'auto', 'extract', doc['version'], doc['generation'])
        service.pool.shutdown(wait=True)
        final = self.store.run(run['id'])
        self.assertEqual(final['status'], 'success', final['detail'])
        facts = [i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'fact']
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(i['payload']['quote'] == SOURCE and i['status'] == 'pending' for i in facts))
        self.assertEqual(self.store.source_text(doc['id'], 1), SOURCE)
        self.assertEqual(self.store.document_metadata(doc['id'], 1), original_metadata)

    def test_validated_controlled_prefix_resumes_without_losing_quotes(self):
        first = '주문\x01 근거'.ljust(4000, '가')
        doc = self.new_doc(first+'다음 구간')
        delta = {'entities': [{'id': 'order', 'name': '주문', 'type': 'Service', 'quote': first}], 'aliases': [], 'relations': []}
        failed, _ = self.run_job(doc, [delta, RuntimeError('synthetic second chunk failure')])
        self.assertEqual(failed['status'], 'error')
        retried, calls = self.run_job(doc, [{'entities': [], 'aliases': [], 'relations': []}])
        self.assertEqual(retried['status'], 'success', retried['detail'])
        self.assertEqual([c['offset'] for c in calls], [4000])
        item = next(i for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(item['payload'], delta)
