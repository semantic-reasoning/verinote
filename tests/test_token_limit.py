"""Per-job OpenRouter limits, with synthetic HTTP only."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import test_openrouter_integration as integration
from test_openrouter_pipeline import Fixture, dictionary
from test_workflow import FACTS, SOURCE, approved_dictionary, extraction_response

from verinote.transports import ExecutionOptions
from verinote.usage import job_calls


class TokenLimits(unittest.TestCase):
    setUp = integration.Integration.setUp
    close = integration.Integration.close
    request = integration.Integration.request
    upload = integration.Integration.upload
    finish = integration.Integration.finish
    approve = integration.Integration.approve
    audit = integration.Integration.audit

    def start(self, doc, stage='ontology', **extra):
        return self.request(f'/api/documents/{doc["id"]}/{stage}', 'POST', {
            'agent': 'openrouter', 'model': 'vendor/model',
            'expected_version': doc['version'], 'expected_generation': doc['generation'], **extra})

    def test_validation_before_registration_and_omission_compatibility(self):
        doc = self.upload()
        for stage in ('ontology', 'extract'):
            for value in (None, True, False, '32768', 32768.0, 0, -1, 65537, [], {}):
                with self.subTest(stage=stage, value=value):
                    with self.assertRaises(HTTPError) as caught:
                        self.start(doc, stage, max_tokens=value)
                    self.assertEqual(caught.exception.code, 400); caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self.start(doc, agent='agy', model='', max_tokens=32768)
        self.assertEqual(caught.exception.code, 400); caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/questions', 'POST', {'question': 'q', 'agent': 'openrouter', 'model': 'vendor/model', 'max_tokens': 32768})
        self.assertEqual(caught.exception.code, 400); caught.exception.close()
        self.assertEqual(self.store.runs(), []); self.assertEqual(self.queue.tasks, [])
        self.service.cli.options = ExecutionOptions(max_tokens=70000)
        self.assertEqual(self.request('/api/settings')['extraction_token_limits'], {'min': 1, 'max': 65536, 'step': 1, 'default': 70000})
        job = self.start(doc)
        self.assertEqual(job['execution_options']['max_tokens'], 70000)
        for value in (1, 65536):
            other = self.upload()
            self.assertEqual(self.start(other, max_tokens=value)['execution_options']['max_tokens'], value)
        self.audit()

    def test_two_queued_jobs_freeze_limits_without_mutating_defaults(self):
        first, second = self.upload(), self.upload()
        a = self.start(first, max_tokens=8192)
        b = self.start(second, max_tokens=32768)
        self.assertEqual(self.service.cli.execution_options('openrouter').max_tokens, 16384)
        self.service.cli.options = ExecutionOptions(max_tokens=65536)
        fixture = Fixture([dictionary, dictionary])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            self.queue.drain()
        self.assertEqual([r['max_tokens'] for r in fixture.requests], [8192, 32768])
        for job, limit in ((a, 8192), (b, 32768)):
            saved = self.request('/api/runs/' + job['id'])
            self.assertEqual(saved['status'], 'success', saved['detail'])
            self.assertEqual(saved['execution_options']['max_tokens'], limit)
        self.audit()

    def test_extraction_and_quote_repair_use_the_selected_limit(self):
        doc = self.upload()
        approved_dictionary(self.store, doc['id'])
        bad = extraction_response(); bad['facts'][0]['quote'] = '상기 ' + FACTS[0]['quote']
        fixture = Fixture([bad, {'repairs': [{'path': 'facts.0.quote', 'quote': FACTS[0]['quote']}]}])
        with patch('verinote.openrouter._HTTPS', side_effect=fixture):
            job = self.start(doc, 'extract', max_tokens=32768)
            self.service.cli.options = ExecutionOptions(max_tokens=65536)
            saved = self.finish(job)
        self.assertEqual(saved['status'], 'success', saved['detail'])
        self.assertEqual([r['max_tokens'] for r in fixture.requests], [32768, 32768])
        self.assertTrue(all(0 < r['timeout'] <= 360 for r in fixture.requests))
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.audit()

    def test_resume_uses_only_matching_user_limits_in_both_stages(self):
        for stage in ('ontology', 'extract'):
            for changed in (False, True):
                with self.subTest(stage=stage, changed=changed):
                    source = SOURCE.ljust(8000 if stage == 'extract' else 4000) + SOURCE
                    doc = self.upload(source)
                    if stage == 'extract':
                        approved_dictionary(self.store, doc['id'])
                    response = extraction_response() if stage == 'extract' else dictionary
                    outputs = [response, TimeoutError('fixture timeout'), response]
                    if changed:
                        outputs.append(response)
                    fixture = Fixture(outputs)
                    with patch('verinote.openrouter._HTTPS', side_effect=fixture):
                        first = self.finish(self.start(doc, stage, max_tokens=32768))
                        self.assertEqual(first['status'], 'error')
                        limit = 65536 if changed else 32768
                        second = self.finish(self.start(doc, stage, max_tokens=limit))
                    self.assertEqual(second['status'], 'success', second['detail'])
                    calls = job_calls(self.root, 'runs', second)
                    self.assertEqual(len(calls), 2 if changed else 1)
                    self.assertTrue(all(r['max_tokens'] == limit for r in fixture.requests[2:]))
        self.audit()


class TokenLimitUI(unittest.TestCase):
    def test_slider_state_payload_capture_and_default_compatibility(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const nodes={};const context={config:{selected:'openrouter',models:{},providers:[{id:'openrouter',transport:'http'},{id:'agy',transport:'cli'}],extraction_token_limits:{min:1,max:65536,step:1,default:16384}},esc:String,$:s=>nodes[s]};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function providerById('),source.indexOf('function extractionControls(')),context);
assert(context.tokenLimitControls().includes('type="range"'));assert(context.tokenLimitControls().includes('고급 설정'));
for(const id of ['extract-max-tokens','extract-token-override','extract-token-controls','extract-token-cli','extract-token-value','extract-answer-budget','extract-agent','extract-model'])nodes['#'+id]={};
const slider=nodes['#extract-max-tokens'],toggle=nodes['#extract-token-override'];
nodes['#extract-agent'].value='openrouter';nodes['#extract-model'].value='vendor/model';slider.value='16384';toggle.checked=false;
context.syncTokenLimitControls();assert(slider.disabled);assert(!nodes['#extract-token-controls'].hidden);
const doc={version:2,generation:4};assert(!('max_tokens' in context.extractionRequest(doc)));
assert.equal(context.extractionRequest(doc).answer_reserve_tokens,8192);assert(nodes['#extract-answer-budget'].textContent.includes((4096).toLocaleString()));
toggle.checked=true;toggle.onchange();assert(!slider.disabled);slider.value='32768';slider.oninput();assert.equal(nodes['#extract-token-value'].textContent,(32768).toLocaleString());
const captured=context.extractionRequest(doc);assert.equal(captured.max_tokens,32768);assert.equal(captured.agent,'openrouter');
slider.value='65536';nodes['#extract-agent'].value='agy';context.syncProviderControls('extract',true);
assert(slider.disabled);assert(toggle.disabled);assert(nodes['#extract-token-controls'].hidden);assert(!('max_tokens' in context.extractionRequest(doc)));assert.equal(captured.max_tokens,32768);
assert(!('answer_reserve_tokens' in context.extractionRequest(doc)));
nodes['#extract-agent'].value='openrouter';context.syncProviderControls('extract',true);assert(!slider.disabled);assert.equal(context.extractionRequest(doc).max_tokens,65536);
toggle.checked=false;context.config.extraction_token_limits.default=70000;context.syncTokenLimitControls();
assert.equal(nodes['#extract-token-value'].textContent,(70000).toLocaleString());assert(!('max_tokens' in context.extractionRequest(doc)));
context.config.extraction_token_limits.default=1000;context.syncTokenLimitControls();assert(nodes['#extract-answer-budget'].textContent.includes('2,048'));assert.throws(()=>context.extractionRequest(doc),/2,048/);
toggle.checked=true;slider.value='2048';toggle.onchange();assert.equal(context.extractionRequest(doc).answer_reserve_tokens,1024);assert(nodes['#extract-answer-budget'].textContent.includes((1024).toLocaleString()));
assert(source.includes('const payload=extractionRequest(d)'));assert(source.includes("for (const stage of ['ontology','extract'])"));
'''
        subprocess.run(['node', '-e', script], check=True, cwd=Path(__file__).parents[1])
