"""Real bounded containers + fake model/transport events, without paid calls."""
import base64
import io
import json
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import test_workflow as workflow
from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

from verinote.documents import MAX_BYTES, decode_document
from verinote.progress import analysis_progress
from verinote.server import (
    MAX_BASE64_CHARS,
    MAX_JSON_BYTES,
    MAX_UPLOAD_JSON_BYTES,
    Handler,
)
from verinote.service import Service
from verinote.usage import atomic_json


def docx(padding=0, compression=zipfile.ZIP_STORED):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression) as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>'+SOURCE+'</w:t></w:r></w:p></w:document>')
        if padding:z.writestr('word/media/attachment.bin',b'x'*padding)
    return stream.getvalue()


class LargeUpload(unittest.TestCase):
    setUp=workflow.HTTP.setUp
    tearDown=workflow.HTTP.tearDown
    request=workflow.HTTP.request

    def test_compressed_large_media_upload_preserves_original(self):
        raw=docx(20*1024*1024,zipfile.ZIP_DEFLATED)
        self.assertLess(len(raw),1024*1024)
        document=self.request('/api/documents','POST',{'title':'compressed media','filename':'media.docx','content_base64':base64.b64encode(raw).decode()})
        self.assertEqual(document['text'],SOURCE)
        self.assertEqual(self.store.raw(document['id'],1)[0],raw)
        self.assertEqual(self.store.runs(),[])

    def test_real_large_original_upload_and_immutable_revision(self):
        raw=docx(5*1024*1024)
        self.assertGreater(len(raw),4*1024*1024)
        body={'title':'large original','filename':'large.docx','content_base64':base64.b64encode(raw).decode()}
        self.assertGreater(len(json.dumps(body)),MAX_JSON_BYTES)
        self.assertLess(len(json.dumps(body)),MAX_UPLOAD_JSON_BYTES)
        d=self.request('/api/documents','POST',body)
        self.assertEqual(d['text'],SOURCE)
        with urlopen(self.url+f"/api/documents/{d['id']}/raw") as r:self.assertEqual(r.read(),raw)
        target={'document_id':d['id'],'expected_version':1,'expected_generation':1}
        newer=self.request('/api/documents','POST',body|target)
        self.assertEqual(newer['version'],2)
        with self.assertRaises(HTTPError) as e:self.request('/api/documents','POST',body|target)
        self.assertEqual(e.exception.code,409);e.exception.close()
        self.assertEqual(self.store.raw(d['id'],1)[0],raw)
        self.assertEqual(self.store.runs(),[])
        limits=self.request('/api/settings')['document_limits']
        self.assertEqual(limits,{'original_bytes':MAX_BYTES,'text_chars':128000,'pdf_pages':200,'docx_expanded_bytes':16*1024*1024})

    def test_raw_exact_boundary_and_unchanged_text_budget(self):
        small=docx()
        # ZIP allows a self-extracting preamble; expanded content remains short.
        exact=b'x'*(MAX_BYTES-len(small))+small
        self.assertEqual(decode_document('exact.docx',exact),SOURCE)
        with self.assertRaisesRegex(ValueError,'32 MiB'):decode_document('too-large.docx',exact+b'x')
        with self.assertRaisesRegex(ValueError,'128,000'):decode_document('long.txt',b'x'*128001)

    def test_encoded_bounds_types_malformed_and_origin(self):
        body={'title':'bad','filename':'bad.docx'}
        for encoded in ([],123,'%%%'):
            with self.assertRaises(HTTPError) as e:self.request('/api/documents','POST',body|{'content_base64':encoded})
            self.assertEqual(e.exception.code,400);e.exception.close()
        with patch('verinote.server.base64.b64decode') as decode:
            with self.assertRaises(HTTPError) as e:self.request('/api/documents','POST',body|{'content_base64':'A'*(MAX_BASE64_CHARS+1)})
            self.assertEqual(e.exception.code,400);e.exception.close();decode.assert_not_called()
        with self.assertRaises(HTTPError) as e:self.request('/api/documents','POST',body,{'Origin':'https://foreign.invalid'})
        self.assertEqual(e.exception.code,403);e.exception.close()
        self.assertEqual(self.store.documents(),[])

    def test_route_scoped_body_limit_is_checked_before_read(self):
        handler=object.__new__(Handler);handler.connection=Mock();handler.rfile=Mock()
        for cap in (MAX_JSON_BYTES,MAX_UPLOAD_JSON_BYTES):
            handler.headers={'Content-Type':'application/json','Content-Length':str(cap+1)}
            with self.assertRaisesRegex(ValueError,'크기'):handler.body(cap)
        handler.rfile.read.assert_not_called()
        handler.headers={'Content-Type':'application/json','Content-Length':str(MAX_JSON_BYTES+1)}
        with self.assertRaises(ValueError):handler.body()
        self.assertEqual(MAX_UPLOAD_JSON_BYTES,((MAX_BYTES+2)//3)*4+64*1024)


class AnalysisProgress(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def dictionary_run(self):
        source=SOURCE.ljust(4000)+'END'
        doc=self.store.register('progress','progress.md',source.encode(),source)
        run=self.store.create_run(doc['id'],'agy','','ontology',1,1)
        return run,doc

    def marker(self,run,doc,completed=0,resumed=0):
        return {'schema_version':1,'pipeline':'source-spans-v1','run_id':run['id'],
                'calls_started':1,'resumed_from':'a'*32 if resumed else None,'resumed_chars':resumed,
                'completed_chars':completed,'total_chars':len(doc['text']),'status':'running'}

    def save(self,run,marker):
        path=self.root/'runs'/run['id']/'dictionary-progress.json';path.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(path,marker);return path

    def test_dictionary_resume_saving_error_restart_and_terminal_authority(self):
        run,doc=self.dictionary_run()
        self.assertEqual(analysis_progress(self.root,self.store,run['id'])['percent'],0)
        self.store.run_status(run['id'],'running')
        self.assertIsNone(analysis_progress(self.root,self.store,run['id'])['percent'])
        self.save(run,self.marker(run,doc,4000,4000))
        p=analysis_progress(self.root,self.store,run['id'])
        self.assertEqual((p['completed_chars'],p['completed_chunks'],p['resumed_chars']),(4000,1,4000))
        self.assertEqual(p['percent'],99)
        self.save(run,self.marker(run,doc,len(doc['text'])))
        self.assertEqual(analysis_progress(self.root,self.store,run['id'])['phase'],'saving')
        self.store.run_status(run['id'],'error','publication failed')
        p=analysis_progress(self.root,self.store,run['id']);self.assertEqual((p['phase'],p['percent']),('error',99))
        self.store.run_status(run['id'],'running');self.store.recover()
        self.assertEqual(analysis_progress(self.root,self.store,run['id'])['phase'],'interrupted')
        self.store.run_status(run['id'],'running');self.store.finish_run(run['id'],dictionary=DICTIONARY)
        p=analysis_progress(self.root,self.store,run['id']);self.assertEqual((p['phase'],p['percent']),('success',100))
        self.assertEqual(self.store.document(doc['id'])['latest_run']['id'],run['id'])

    def test_malformed_missing_symlink_and_deleted_progress_are_unknown(self):
        run,doc=self.dictionary_run();self.store.run_status(run['id'],'running')
        for changes in ({'run_id':'b'*32},{'completed_chars':True},{'completed_chars':23},
                        {'completed_chars':len(doc['text'])+1},{'total_chars':4},{'calls_started':65},
                        {'resumed_chars':4000},{'schema_version':True},{'unexpected':1}):
            self.save(run,self.marker(run,doc)|changes)
            self.assertFalse(analysis_progress(self.root,self.store,run['id'])['known'])
        path=self.save(run,self.marker(run,doc));path.write_bytes(b'x'*8193)
        self.assertFalse(analysis_progress(self.root,self.store,run['id'])['known'])
        path.unlink();target=self.root/'other';atomic_json(target,self.marker(run,doc));path.symlink_to(target)
        self.assertFalse(analysis_progress(self.root,self.store,run['id'])['known'])
        path.unlink();self.save(run,self.marker(run,doc));self.store.run_status(run['id'],'error')
        self.store.delete_document(doc['id'],False,1,1)
        self.assertTrue(path.exists());self.assertFalse(analysis_progress(self.root,self.store,run['id'])['known'])
        with self.assertRaises(ValueError):self.store.run(run['id'])

    def test_extraction_only_validated_chunks_and_commit_failure(self):
        source=SOURCE.ljust(8000)+SOURCE
        doc=self.store.register('extract','x.md',source.encode(),source);approved_dictionary(self.store,doc['id'])
        captured=[]
        class Fake:
            def complete(adapter,*args):
                run=self.store.runs()[0]
                captured.append(analysis_progress(self.root,self.store,run['id']))
                return extraction_response()
        service=Service(self.store,self.root,Fake())
        real_finish=self.store.finish_run
        def finish(*args,**kwargs):
            captured.append(analysis_progress(self.root,self.store,args[0]))
            return real_finish(*args,**kwargs)
        with patch.object(self.store,'finish_run',side_effect=finish):
            run=service.start(doc['id'],'agy','',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        self.assertIsNone(captured[0]['percent'])
        self.assertEqual(captured[1]['completed_chars'],8000)
        self.assertEqual(captured[2]['phase'],'saving');self.assertEqual(captured[2]['percent'],99)
        self.assertEqual(service.with_usage('runs',self.store.run(run['id']))['progress']['percent'],100)
        # The report reaching the end cannot claim success after DB rejection.
        self.store.run_status(run['id'],'error','synthetic rollback state')
        self.assertEqual(analysis_progress(self.root,self.store,run['id'])['phase'],'error')
        self.assertLess(analysis_progress(self.root,self.store,run['id'])['percent'],100)

    def test_reporting_failure_remains_optional_and_api_has_no_partial_approval(self):
        approved_dictionary(self.store,self.doc['id'])
        class Fake:
            def complete(adapter,*args):return extraction_response()
        service=Service(self.store,self.root,Fake())
        with patch('verinote.extraction_coverage.atomic_json',side_effect=OSError('disk')):
            run=service.start(self.doc['id'],'agy','',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        result=service.with_usage('runs',self.store.run(run['id']))
        self.assertEqual(result['status'],'success');self.assertEqual(result['progress']['percent'],100)
        self.assertFalse(result['coverage']['available']);self.assertEqual(self.store.snapshot()['facts'],[])


class BrowserProgress(unittest.TestCase):
    def run_js(self,script):
        return subprocess.check_output(['node'],input=script,text=True)

    def functions(self):
        app=Path('verinote/web/app.js').read_text()
        return app[app.index('function renderProgress('):app.index('function bulkCandidates(')]

    def test_xhr_transfer_not_success_error_abort_unknown_and_escaped_render(self):
        script="""const assert=require('node:assert/strict');
const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){}});return elements.get(s)};
const labels={};let uploadBusy=false,uploadTarget=null;const runWatchers=new Map();
class XHR {constructor(){this.upload={};XHR.last=this}open(...args){this.opened=args}setRequestHeader(){}send(body){this.body=body}};global.XMLHttpRequest=XHR;
"""+self.functions()+"""
(async()=>{
 let events=[],settled=false;const p=uploadDocument({text:'hello'},(...e)=>events.push(e)).then(x=>{settled=true;return x});
 const x=XHR.last;x.upload.onprogress({lengthComputable:true,loaded:50,total:100});assert.equal(events.at(-1)[0],50);
 x.upload.onprogress({lengthComputable:false,loaded:50,total:0});assert.equal(events.at(-1)[0],null);
 x.upload.onload();await Promise.resolve();assert.equal(settled,false);assert.equal(events.at(-1)[0],null);
 x.status=201;x.responseText='{"id":"saved"}';x.onload();assert.equal((await p).id,'saved');assert.equal(events.at(-1)[0],100);
 for(const event of ['onerror','ontimeout','onabort']){const p=uploadDocument({},()=>{});XHR.last[event]();await assert.rejects(p);}
 {const p=uploadDocument({},()=>{});XHR.last.status=400;XHR.last.responseText='{"error":"too large"}';XHR.last.onload();await assert.rejects(p,/too large/);}
 {const p=uploadDocument({},()=>{});XHR.last.status=201;XHR.last.responseText='bad';XHR.last.onload();await assert.rejects(p,/응답/);}
 assert(!renderProgress(null,'<img>','<script>').includes('value='));assert(!renderProgress(50,'<img>','<script>').includes('<img>'));
 const failed=renderProgress(null,'원문 등록 실패','<script>본문 XML 초과</script>',true);
 assert(failed.includes('처리 중단'));assert(failed.includes('본문 XML 초과'));assert(failed.includes('role="status"'));
 assert(!failed.includes('진행률 확인 중'));assert(!failed.includes('<progress'));assert(!failed.includes('<script>'));
 assert(renderProgress(null,'처리 중').includes('<progress'));assert(renderProgress(null,'처리 중').includes('진행률 확인 중'));
 assert(renderProgress(50,'실패','',true).includes('value="50"'));
 const r={stage:'ontology',status:'error',detail:'<script>',progress:{known:true,phase:'error',percent:99,completed_chars:4000,total_chars:4000,completed_chunks:1,total_chunks:1,resumed_chars:0}};
 assert(renderAnalysisProgress(r).includes('실패'));assert(!renderAnalysisProgress(r).includes('value="100"'));
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok',self.run_js(script))

    def test_upload_locks_controls_and_captures_target_before_async_preparation(self):
        script="""const assert=require('node:assert/strict');const esc=String;const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){},close(){},innerHTML:''});return elements.get(s)};
let uploadBusy=false,uploadTarget={id:'old',current_version:2,generation:7};const runWatchers=new Map();const config={document_limits:{original_bytes:33554432}};
const file={size:10};const controls=[{disabled:false},{disabled:true}];const form={elements:{file:{files:[file]},title:{value:'captured'}},querySelectorAll(){return controls}};
let notices=[];function notice(...x){notices.push(x)}async function loadLibrary(){}async function openDoc(){};
"""+self.functions()+"""
(async()=>{
 let resume,body,calls=0;encodeUploadFile=()=>new Promise(r=>resume=r);uploadDocument=async d=>{calls++;body=d;return {id:'saved'}};
 const first=submitUpload(form);assert(uploadBusy);assert(controls.every(c=>c.disabled));await submitUpload(form);assert.equal(calls,0);
 uploadTarget={id:'wrong',current_version:9,generation:9};form.elements.title.value='changed';resume('YWJj');await first;
 assert.equal(body.document_id,'old');assert.equal(body.expected_version,2);assert.equal(body.expected_generation,7);assert.equal(body.title,'captured');assert.equal(calls,1);
 assert(!uploadBusy);assert.deepEqual(controls.map(c=>c.disabled),[false,true]);
 encodeUploadFile=async()=>{throw Error('read error')};await submitUpload(form);assert(!uploadBusy);assert.deepEqual(controls.map(c=>c.disabled),[false,true]);assert(notices.at(-1)[0].includes('read error'));
 assert($('#upload-progress').innerHTML.includes('원문 등록 실패'));assert(!$('#upload-progress').innerHTML.includes('진행률 확인 중'));assert(!$('#upload-progress').innerHTML.includes('<progress'));
 encodeUploadFile=async()=> 'YWJj';await submitUpload(form);assert.equal(calls,2);assert(!uploadBusy);
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok',self.run_js(script))

    def test_single_watcher_reload_stale_navigation_and_deleted_400_stop(self):
        script="""const assert=require('node:assert/strict');const esc=String;const labels={};let selectedDoc={id:'doc',version:1,latest_run:{id:'run'}},documentRequest=1,uploadBusy=false,uploadTarget;
const runWatchers=new Map(),timers=[];global.setTimeout=fn=>{timers.push(fn);return timers.length};global.clearTimeout=()=>{};
const elements=new Map([['#library',{hidden:false}],['#activity',{hidden:true}],['#document-progress',{innerHTML:''}]]);const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){}});return elements.get(s)};
global.document={querySelector:()=>null};let resolve,requests=0;function api(){requests++;return new Promise(r=>resolve=r)}async function loadLibrary(){}async function openDoc(){};
"""+self.functions()+"""
(async()=>{
 watchRun('run','doc',1);watchRun('run','doc',1);assert.equal(requests,1);
 selectedDoc={id:'elsewhere',version:1};documentRequest++;
 resolve({id:'run',status:'running',stage:'ontology'});await new Promise(r=>setImmediate(r));assert.equal($('#document-progress').innerHTML,'');
 await timers.shift()();assert.equal(runWatchers.size,0);
 selectedDoc={id:'doc',version:1,latest_run:{id:'run'}};api=async()=>{const e=Error('실행을 찾을 수 없습니다.');e.status=400;throw e};
 watchRun('run','doc',1);await new Promise(r=>setImmediate(r));assert.equal(runWatchers.size,0);assert($('#document-progress').innerHTML.includes('조회 실패'));
 api=async()=>({id:'run',status:'running',stage:'ontology',progress:{known:false,phase:'running',percent:null}});watchRun('run','doc',1);await new Promise(r=>setImmediate(r));assert.equal(runWatchers.size,1);assert($('#document-progress').innerHTML.includes('분석 중'));
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok',self.run_js(script))

    def test_fast_terminal_refresh_and_navigation_only_resume(self):
        app=Path('verinote/web/app.js').read_text()
        show=app[app.index('function showView('):app.index('async function loadLibrary(')]
        script="""const assert=require('node:assert/strict');const esc=String;const labels={};let selectedDoc,documentRequest=1,uploadBusy=false,uploadTarget;
const runWatchers=new Map(),timers=[];global.setTimeout=fn=>{timers.push(fn);return timers.length};global.clearTimeout=()=>{};
const views=['library','activity','settings','query'].map(id=>({id,hidden:id!=='library'}));const elements=new Map(views.map(v=>['#'+v.id,v]));elements.set('#document-progress',{innerHTML:''});
const $=s=>{if(!elements.has(s))elements.set(s,{addEventListener(){}});return elements.get(s)};
global.document={querySelector:()=>null,querySelectorAll:s=>s==='.view'?views:[]};let requests=0,libraryLoads=0,docLoads=0,response;
async function api(){requests++;return response}async function loadLibrary(){libraryLoads++}async function openDoc(){docLoads++}function attempt(fn){return fn()}
async function loadQueries(){}async function loadQuestions(){}async function loadRuns(){}
"""+show+self.functions()+"""
(async()=>{
 for(const state of ['success','error']){
  selectedDoc={id:'doc',version:1,latest_run:{id:'run',status:'queued'}};
  response={id:'run',status:state,stage:'ontology'};
  const old=docLoads;watchRun('run','doc',1,'queued');await new Promise(r=>setImmediate(r));assert.equal(docLoads,old+1);assert.equal(runWatchers.size,0);
 }
 // Merely reopening a completed run must not recursively reopen the document.
 const old=docLoads;watchRun('run','doc',1,'success');await new Promise(r=>setImmediate(r));assert.equal(docLoads,old);
 response={id:'run',status:'running',stage:'ontology'};selectedDoc.latest_run.status='running';
 watchRun('run','doc',1,'running');await new Promise(r=>setImmediate(r));assert.equal(runWatchers.size,1);
 showView('settings');await timers.shift()();assert.equal(runWatchers.size,0);
 const before=requests;showView('library');await new Promise(r=>setImmediate(r));assert.equal(requests,before+1);assert.equal(runWatchers.size,1);
 // Query exit and navigation-only return also restore it.
 showView('query');await timers.shift()();assert.equal(runWatchers.size,0);showView('library');await new Promise(r=>setImmediate(r));assert.equal(runWatchers.size,1);
 // A terminal response racing away-and-back navigation must refetch and refresh.
 let resolve;api=()=>{requests++;return new Promise(r=>resolve=r)};
 const polling=timers.shift()();showView('settings');showView('library');const loaded=docLoads;
 resolve({id:'run',status:'success',stage:'ontology'});await polling;assert.equal(docLoads,loaded);assert.equal(runWatchers.size,1);
 api=async()=>({id:'run',status:'success',stage:'ontology'});await timers.shift()();assert.equal(docLoads,loaded+1);assert.equal(runWatchers.size,0);
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        self.assertIn('ok',self.run_js(script))


if __name__=='__main__':unittest.main()
