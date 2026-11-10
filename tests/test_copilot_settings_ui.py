"""Settings form behavior using the actual browser functions without model calls."""
import shutil
import subprocess
import unittest


class CopilotSettingsUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser logic tests.')
    def test_load_effective_value_and_save_integer_then_reload(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const html=fs.readFileSync('verinote/web/index.html','utf8');
assert(html.includes('name="copilot_extraction_concurrency"'));
assert(html.includes('순차 실행 · 1개')); assert(html.includes('병렬 실행 · 최대 2개'));
const nodes=Object.fromEntries(['#upload-limits','#ask-agent','#ask-model','#dictionary-context-bytes','#provider-settings','#copilot-extraction-concurrency','#settings-form'].map(k=>[k,{}]));
let saved={selected:'copilot',models:{},providers:[],document_limits:{original_bytes:1024,text_chars:100,pdf_pages:1,docx_expanded_bytes:1024},dictionary:{context_bytes:60000},copilot_extraction:{concurrency:2,source:'server_startup'}};
let pending,puts=[],notices=[];
const context={config:null,modelCatalogEpoch:0,$:s=>nodes[s],document:{querySelectorAll:()=>[]},syncProviderControls:()=>{},loadUsageRates:async()=>{},attempt:fn=>pending=fn(),notice:s=>notices.push(s),
  api:async(url,method,body)=>{assert.equal(url,'/api/settings');if(method==='PUT'){puts.push(body);assert.equal(typeof body.copilot_extraction_concurrency,'number');saved={...saved,copilot_extraction:{concurrency:body.copilot_extraction_concurrency,source:'settings'}};}return saved;}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function loadSettings()'),source.indexOf("$('#query-form').onsubmit")),context);
(async()=>{
  await context.loadSettings();assert.equal(nodes['#copilot-extraction-concurrency'].value,'2');
  for(const choice of [1,2]){
    const form={elements:{selected:{value:'copilot'},dictionary_context_bytes:{value:'60000'},copilot_extraction_concurrency:{value:String(choice)}}};
    let prevented=false;
    nodes['#settings-form'].onsubmit({target:form,preventDefault:()=>prevented=true});await pending;
    assert(prevented);assert.equal(puts.at(-1).copilot_extraction_concurrency,choice);
    assert.equal(nodes['#copilot-extraction-concurrency'].value,String(choice));
    assert(notices.at(-1).includes('새 작업부터'));
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
