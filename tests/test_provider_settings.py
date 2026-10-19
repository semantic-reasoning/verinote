"""Transport-specific public settings without credential serialization."""
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_openrouter_pipeline import Fixture, dictionary
from test_workflow import SOURCE

from verinote.agents import CLI_PROVIDERS
from verinote.config import Credentials
from verinote.store import Store, digest
from verinote.transports import Credentials as TransportCredentials

KEY = 'sk-or-settings-synthetic-708'


class Settings(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def reject(self, payload):
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/settings','PUT',payload)
        self.assertEqual(caught.exception.code,400); caught.exception.close()

    def test_transport_fields_presence_only_no_http_executable_lookup(self):
        for configured in (False, True):
            self.server.credentials = Credentials(KEY if configured else '')
            with patch('verinote.server.executable', return_value='/fake') as executable:
                value = self.request('/api/settings')
            self.assertEqual({call.args[0] for call in executable.call_args_list}, set(CLI_PROVIDERS))
            api = next(p for p in value['providers'] if p['id']=='openrouter')
            self.assertEqual(api, {'id':'openrouter','name':'OpenRouter (API)','transport':'http','configured':configured})
            self.assertTrue(all(p['transport']=='cli' and p['installed'] is True and 'configured' not in p for p in value['providers'] if p['id']!='openrouter'))
            text=json.dumps(value); self.assertNotIn(KEY,text);self.assertNotIn(digest(KEY.encode()),text)
            self.assertNotIn('config.toml',text)

    def test_model_rules_keyless_save_and_existing_models(self):
        cli_models={key:'chosen-'+key for key in CLI_PROVIDERS}
        # Simulate a saved pre-HTTP settings payload, then reopen existing DB.
        self.store.save_settings({'selected':'codex','models':cli_models})
        Store(self.store.path)
        self.assertEqual(self.request('/api/settings')['models'],cli_models)
        for models in ({}, {'openrouter':''}, {'openrouter':' '}, {'openrouter':'a'*151}, {'openrouter':'model\n'}, {'openrouter':'\tmodel'}):
            self.reject({'selected':'openrouter','models':models})
        self.reject({'selected':'agy','models':{'openrouter':'model\n'}})
        self.reject({'selected':'agy','models':{},'api_key':KEY})
        self.reject({'selected':'agy','models':{'unknown':'model'}})
        self.reject({'selected':[],'models':{}})
        saved=self.request('/api/settings','PUT',{'selected':'agy','models':cli_models|{'openrouter':' '}})
        self.assertEqual(saved['models'],cli_models|{'openrouter':''})
        saved=self.request('/api/settings','PUT',{'selected':'openrouter','models':cli_models|{'openrouter':' vendor/model '}})
        self.assertEqual(saved['models']['openrouter'],'vendor/model')
        self.assertFalse(next(p for p in self.request('/api/settings')['providers'] if p['id']=='openrouter')['configured'])
        self.assertEqual(Store(self.store.path).settings(),saved)

    def test_execution_overrides_use_server_validation_before_registration(self):
        doc=self.store.register('synthetic','s.md',SOURCE.encode(),SOURCE)
        self.request('/api/settings','PUT',{'selected':'openrouter','models':{'openrouter':'vendor/default'}})
        for model in ('', ' ', 'model\n', 'model'):
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/documents/'+doc['id']+'/ontology','POST',{'agent':'openrouter','model':model,'expected_version':1,'expected_generation':1})
            self.assertEqual(caught.exception.code,400);caught.exception.close()
        self.assertEqual(self.store.runs(),[])
        self.service.cli._credentials=TransportCredentials(KEY)
        self.server.credentials=Credentials(KEY)
        fixture=Fixture([dictionary])
        with patch('verinote.openrouter._HTTPS',side_effect=fixture):
            job=self.request('/api/documents/'+doc['id']+'/ontology','POST',{'agent':'openrouter','model':'vendor/override','expected_version':1,'expected_generation':1})
            end=time.monotonic()+3
            while self.store.run(job['id'])['status'] in ('queued','running') and time.monotonic()<end:
                time.sleep(.01)
        self.assertEqual(self.store.run(job['id'])['status'],'success')
        self.assertEqual(fixture.requests[0]['model'],'vendor/override')


class UIHelpers(unittest.TestCase):
    def test_provider_hints_failure_and_cost_copy(self):
        source=(Path(__file__).parents[1]/'verinote/web/app.js').read_text()
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const nodes={};const context={config:{selected:'openrouter',models:{openrouter:'vendor/model'},providers:[{id:'openrouter',name:'OpenRouter (API)',transport:'http',configured:true},{id:'agy',name:'Antigravity',transport:'cli',installed:false}]},esc:v=>String(v??''),$:s=>nodes[s]};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function providerById('),source.indexOf('function extractionControls(')),context);
assert(context.providerStatus(context.config.providers[0]).includes('키 설정됨'));
assert(!context.providerStatus(context.config.providers[0]).includes('설치'));
assert(context.providerHint('openrouter').includes('라우팅된 모델 제공자'));
assert(context.providerHint('openrouter').includes('과금'));
assert(context.modelPlaceholder('openrouter').includes('필수'));
assert.equal(context.modelPlaceholder('agy'),'CLI 기본 모델');
nodes['#ask-agent']={value:'openrouter'};nodes['#ask-model']={};nodes['#ask-provider-hint']={};
context.syncProviderControls('ask',true);assert.equal(nodes['#ask-model'].value,'vendor/model');assert(nodes['#ask-model'].required);assert.equal(nodes['#ask-model'].maxLength,150);
nodes['#ask-agent'].value='agy';context.syncProviderControls('ask',true);assert(!nodes['#ask-model'].required);assert.equal(nodes['#ask-model'].placeholder,'CLI 기본 모델');
assert(context.executionGuidance('openrouter').includes('추가 과금'));
assert(context.executionGuidance('openrouter').includes('max_tokens'));
assert.equal(context.executionGuidance('agy'),'');
'''
        subprocess.run(['node','-e',script],check=True,cwd=Path(__file__).parents[1])
        self.assertNotIn('CLI 보고 비용',source)
        self.assertNotIn('localStorage',source);self.assertNotIn('sessionStorage',source)
        html=(Path(__file__).parents[1]/'verinote/web/index.html').read_text()
        self.assertIn('0600',html);self.assertIn('네이티브 Windows',html)
        self.assertIn('서버를 재시작',html)
        self.assertNotIn('name="api_key"',html)
