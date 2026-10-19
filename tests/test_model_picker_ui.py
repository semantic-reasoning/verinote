import shutil
import subprocess
import unittest


class ModelPickerUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js is required for browser logic tests.')
    def test_lazy_loading_selection_preservation_stale_requests_and_recovery(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const nodes={},pending={},calls=[];
const context={Map,esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),providerById:id=>({transport:id==='openrouter'?'http':'cli'}),$:s=>nodes[s],api:(url,method,body)=>{assert.equal(method,'POST');assert.deepEqual(body,{});const id=url.split('/')[3];calls.push(id);return new Promise((resolve,reject)=>pending[id]={resolve,reject});}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('let modelCatalogEpoch='),source.indexOf('async function loadSettings()')),context);
const setup=id=>{nodes[`[data-model-catalog="${id}"]`]={isConnected:true,innerHTML:'initial'};nodes[`#catalog-status-${id}`]={};nodes[`[name="model-${id}"]`]={value:'manual/model'};};
const ok={status:'ok',message:'CLI catalog',models:[{id:'vendor/a',name:'<Model A>'}]};
(async()=>{
  for(const id of ['claude','agy','copilot','cursor','codex']){
    setup(id);assert(context.cliModelPicker({id,transport:'cli'}).includes('사용 가능한 모델'));
    const first=context.loadProviderModels(id),second=context.loadProviderModels(id);
    assert.equal(calls.filter(v=>v===id).length,1);assert(nodes[`[data-model-catalog="${id}"]`].disabled);
    nodes[`[name="model-${id}"]`].value='edited/during-load';pending[id].resolve(ok);await first;await second;
    const select=nodes[`[data-model-catalog="${id}"]`],input=nodes[`[name="model-${id}"]`];
    assert.equal(input.value,'edited/during-load');assert(!select.disabled);assert(select.innerHTML.includes('&lt;Model A>'));
    select.value='vendor/a';select.onchange();assert.equal(input.value,'vendor/a');
    input.value='manual/other';input.oninput();assert.equal(select.selectedIndex,0);
    select.value='vendor/a';select.onchange();assert.equal(input.value,'vendor/a');
    select.value='stale';input.oninput();assert.equal(select.value,'vendor/a');
    select.value='';select.onchange();assert.equal(input.value,'');
    let prevented=false;select.onkeydown({key:'Enter',preventDefault:()=>prevented=true});assert(prevented);
  }
  assert.equal(context.cliModelPicker({id:'openrouter',transport:'http'}),'');await context.loadProviderModels('openrouter');assert(!calls.includes('openrouter'));
  // A settings reload invalidates old DOM and responses; another provider never changes.
  setup('agy');let old=context.loadProviderModels('agy');vm.runInContext('modelCatalogEpoch++',context);
  const oldSelect=nodes['[data-model-catalog="agy"]'];setup('agy');pending.agy.resolve(ok);await old;
  assert.equal(nodes['[data-model-catalog="agy"]'].innerHTML,'initial');assert.equal(oldSelect.innerHTML,'initial');
  // Empty/error responses preserve free text and can be retried by another click.
  for(const status of ['empty','unavailable','timeout','busy','not_installed']){
    const task=context.loadProviderModels('agy');pending.agy.resolve({status,models:[],message:status});await task;
    assert.equal(nodes['[name="model-agy"]'].value,'manual/model');assert(nodes['[data-model-catalog="agy"]'].disabled);
  }
  let failure=context.loadProviderModels('agy');pending.agy.reject(Error('private server error'));await failure;
  assert(!nodes['#catalog-status-agy'].textContent.includes('private'));
  failure=context.loadProviderModels('agy');const serverError=Error('private server detail');serverError.status=500;pending.agy.reject(serverError);await failure;
  assert(nodes['#catalog-status-agy'].textContent.includes('HTTP 500'));
  assert(!nodes['#catalog-status-agy'].textContent.includes('private'));
  let recovered=context.loadProviderModels('agy');pending.agy.resolve(ok);await recovered;assert(!nodes['[data-model-catalog="agy"]'].disabled);
  assert(source.includes('radio.onclick=()=>loadProviderModels(radio.value)'));
  assert(!source.slice(source.indexOf('async function loadSettings()'),source.indexOf("$('#settings-form').onsubmit")).includes('await loadProviderModels'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run(['node','-e',script],check=True)
