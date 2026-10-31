"""Versioned quote location checks, bounded repair, and historical replay."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.extraction_ab_runner import evaluate_saved, file_index
from verinote.extraction_checkpoint import ExtractionCheckpoint
from verinote.ontology import (
    extraction_prompts,
    extraction_quote_issues,
    extraction_version_from_hashes,
)
from verinote.service import Service
from verinote.store import Conflict
from verinote.usage import atomic_json


class QuoteContracts(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, source):
        doc = self.store.register('contract', 'contract.txt', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def run_version(self, doc, version, outputs):
        with patch('verinote.store.EXTRACTION_CONTRACT_VERSION', version):
            run = self.store.create_run(doc['id'], 'copilot', 'model', 'extract', doc['version'], doc['generation'])
        calls = []
        pending = iter(outputs)
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append((prompt, folder))
                value = next(pending)
                if isinstance(value, Exception): raise value
                return copy.deepcopy(value)
        service = Service(self.store, self.root, Fake())
        service.slots.acquire()
        try: service.extract(run)
        finally: service.close()
        return self.store.run(run['id']), calls

    def test_versions_missing_unique_ambiguous_and_overlap(self):
        output = extraction_response()
        self.assertEqual(extraction_quote_issues(output, SOURCE, DICTIONARY), [])
        repeated = SOURCE+' '+SOURCE
        for version in (0, 1):
            self.assertEqual(extraction_quote_issues(output, repeated, DICTIONARY, contract_version=version), [])
        issues = extraction_quote_issues(output, repeated, DICTIONARY, contract_version=2)
        self.assertEqual({i['path'] for i in issues}, {'claims.0.quote', 'facts.0.quote', 'facts.1.quote'})
        self.assertEqual({i['reason'] for i in issues}, {'ambiguous'})
        output['claims'][0]['quote'] = 'aaa'
        self.assertEqual(extraction_quote_issues(output, SOURCE+' aaaa', DICTIONARY)[0]['reason'], 'ambiguous')
        output['claims'][0]['quote'] = 'absent'
        self.assertEqual(extraction_quote_issues(output, SOURCE, DICTIONARY)[0]['reason'], 'missing')
        for version in (True, False, None, '2', 3, -1):
            with self.assertRaises(ValueError): extraction_prompts(version)
            with self.assertRaises(ValueError): extraction_quote_issues(output, SOURCE, DICTIONARY, contract_version=version)
        output['claims'][0]['quote'] = 'x'*8001
        with self.assertRaises(ValueError): extraction_quote_issues(output, 'x'*8002, DICTIONARY)

    def test_queued_v1_keeps_old_prompt_then_v2_can_reextract(self):
        source = SOURCE+'\n가정 예시: '+FACTS[0]['quote']
        doc = self.document(source)
        first, calls = self.run_version(doc, 1, [extraction_response()])
        self.assertEqual(first['status'], 'success', first['detail'])
        self.assertTrue(calls[0][0].startswith(extraction_prompts(1)[0]))
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.store.document(doc['id'])['can_extract'])
        repair = {'repairs': [{'path': 'facts.0.quote', 'quote': SOURCE}]}
        final, calls = self.run_version(doc, 2, [extraction_response(), repair])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].startswith(extraction_prompts(2)[0]))
        self.assertTrue(calls[1][0].startswith(extraction_prompts(2)[1]))
        payload = json.loads(calls[1][0].split('입력(JSON):\n')[1])
        self.assertEqual(payload['issues'][0]['reason'], 'ambiguous')
        self.assertFalse(self.store.document(doc['id'])['can_extract'])
        with self.assertRaises(Conflict): self.run_version(doc, 2, [])

    def test_still_ambiguous_repair_fails_once_without_publication(self):
        doc = self.document(SOURCE+'\n가정 예시: '+FACTS[0]['quote'])
        repair = {'repairs': [{'path': 'facts.0.quote', 'quote': FACTS[0]['quote']}]}
        run, calls = self.run_version(doc, 2, [extraction_response(), repair])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(calls), 2)
        self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])

    def test_ambiguous_repairs_share_existing_document_limit(self):
        chunk = (SOURCE+'\n가정 예시: '+FACTS[0]['quote']).ljust(8000)
        doc = self.document(chunk*3)
        repair = {'repairs': [{'path': 'facts.0.quote', 'quote': SOURCE}]}
        run, calls = self.run_version(doc, 2, [extraction_response(), repair]*3)
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(calls), 5)
        self.assertIn('2회 한도', run['detail'])
        self.assertEqual([i['kind'] for i in self.store.document(doc['id'])['items']], ['ontology'])

    def test_same_quote_in_other_chunks_is_valid_and_v1_prefix_is_not_reused_by_v2(self):
        doc = self.document(SOURCE.ljust(8000)+SOURCE)
        first, _ = self.run_version(doc, 1, [extraction_response(), TimeoutError('stop')])
        self.assertEqual(first['status'], 'error')
        final, calls = self.run_version(doc, 2, [extraction_response(), extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([p.name for _, p in calls], ['1', '2'])
        self.assertNotIn('1청크 재사용', final['detail'])

    def test_historical_v1_prefix_remains_reusable(self):
        doc = self.document(SOURCE.ljust(8000)+SOURCE)
        self.run_version(doc, 1, [extraction_response(), TimeoutError('stop')])
        final, calls = self.run_version(doc, 1, [extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([p.name for _, p in calls], ['2'])
        self.assertIn('1청크 재사용', final['detail'])

    def test_v2_checkpoint_validator_refuses_ambiguous_output(self):
        validator = ExtractionCheckpoint.__new__(ExtractionCheckpoint)
        validator.run = {'ontology': {'payload': DICTIONARY, 'extraction_contract': 2}}
        with self.assertRaises(ValueError): validator.validated(extraction_response(), SOURCE+' '+SOURCE)
        validator.run['ontology']['extraction_contract'] = 1
        self.assertTrue(validator.validated(extraction_response(), SOURCE+' '+SOURCE))

    def test_v2_labeled_ambiguous_prefix_is_discarded_on_resume(self):
        source = (SOURCE+'\n가정 예시: '+FACTS[0]['quote']).ljust(8000)
        doc = self.document(source*2)
        repair = {'repairs': [{'path': 'facts.0.quote', 'quote': SOURCE}]}
        first, _ = self.run_version(doc, 2, [extraction_response(), repair, TimeoutError('stop')])
        path = self.root/'runs'/first['id']/'extraction-checkpoint.json'
        saved = json.loads(path.read_text())
        self.assertEqual(saved['contract']['dictionary']['extraction_contract'], 2)
        saved['outputs'][0]['facts'][0]['quote'] = FACTS[0]['quote']
        path.write_text(json.dumps(saved))
        final, calls = self.run_version(doc, 2, [extraction_response(), repair]*2)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([p.name for _, p in calls], ['1', 'quote-repair', '2', 'quote-repair'])
        self.assertNotIn('1청크 재사용', final['detail'])

    def test_historical_hash_pair_only_and_explicit_v2(self):
        hashes = {v: tuple(hashlib.sha256(p.encode()).hexdigest() for p in extraction_prompts(v)) for v in (1, 2)}
        self.assertEqual(extraction_version_from_hashes(*hashes[1]), 1)
        self.assertEqual(extraction_version_from_hashes(*hashes[2], version=2), 2)
        for pair, version in ((hashes[2], None), ((hashes[1][0], hashes[2][1]), None), (hashes[1], 2), (hashes[1], True)):
            with self.assertRaises(ValueError): extraction_version_from_hashes(*pair, version=version)

    def test_saved_evidence_rejects_unknown_hash_or_mixed_run_version(self):
        source = Path(__file__).resolve().parents[1]/'benchmarks/extraction/results/755-ab-v1-sonnet'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'evidence'; shutil.copytree(source, root)
            original = json.loads((root/'report.json').read_text())
            for mixed in (False, True):
                report = copy.deepcopy(original)
                env = report['environment']
                if mixed:
                    env['extraction_contract'] = 2
                    env['prompt_sha256'], env['repair_prompt_sha256'] = (
                        hashlib.sha256(p.encode()).hexdigest() for p in extraction_prompts(2))
                else: env['prompt_sha256'] = '0'*64
                atomic_json(root/'environment.json', env)
                atomic_json(root/'report.json', report)
                atomic_json(root/'index.json', file_index(root, excluded=('index.json', 'evaluation.json')))
                self.assertEqual(evaluate_saved(root)['verdict'], 'inconclusive')
