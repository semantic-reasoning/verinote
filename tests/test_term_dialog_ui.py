"""Browser-side behaviour of the dictionary add control, its dialog and the approval view, run through the real app.js in node's vm."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const cut=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b));
const SPELLING='납품 계획',NAME='납품계획';
function make(over={}){
 const elements=new Map(),errors=[],requests=[],opened=[],notices=[],timers=[],listeners=[];
 function el(selector){
  if(!elements.has(selector)) elements.set(selector,{hidden:false,disabled:false,textContent:'',innerHTML:'',value:'',checked:false,
   elements:{actor:{value:''},reason:{value:''}},addEventListener(){},reset(){},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(){}});
  return elements.get(selector);
 }
 const state={routes:over.routes||{}};
 const ctx=vm.createContext({console,$:el,
  attempt:fn=>{const task=fn();Promise.resolve(task).catch(e=>errors.push(e));return task;},
  setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:id=>{if(id)timers[id-1]=null;},
  api:(path,method='GET',data)=>{requests.push({path,method,data});return state.router(path,method,data);},
  openDoc:async(...args)=>opened.push(args),notice:(...args)=>notices.push(args),
  document:{addEventListener(type,fn){listeners.push([type,fn]);}},selectedDoc:null});
 vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
 vm.runInContext('let reviewTarget,selectedDoc=null;const labels={pending:"검토 대기",approved:"승인",rejected:"반려",held:"보류"};',ctx);
 vm.runInContext(cut('function renderCoverage(','function renderReasoningDiagnostic('),ctx);
 vm.runInContext(cut('function judgedAlias(','async function openDoc('),ctx);
 vm.runInContext(cut('function openReview(',"$('#cancel-review')"),ctx);
 vm.runInContext(cut("document.addEventListener('click',e=>{\n  const b=e.target.closest('button');",'let searchTimer'),ctx);
 const call=code=>vm.runInContext(code,ctx);
 const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));if(errors.length)throw errors.shift();};
 const fire=async()=>{const due=timers.splice(0).filter(Boolean);for(const fn of due)fn();await flush();};
 const term=(field,spelling,over2={})=>({field,spelling,known:false,type_fits:false,type:null,name_of:[],name_of_total:0,...over2});
 const entities=over.entities||[{id:'plan',name:NAME,type:'문서',quote:'납품계획은 문서다.'}];
 const run={id:'r1',document_id:'d1',ontology:{id:'o1',revision:4},status:'success',
  usage:over.usage||{details:[]},
  coverage:{available:true,status:'success',published:true,total_chunks:11,version_current:true,current_dictionary_status:'approved',run_id:'r1',
   chunks:[{excluded_facts:[{subject:SPELLING,predicate:'depends_on',object:'원장',code:'unknown_entity',quote:'q'}],
    excluded_fact_terms:[[term('subject',SPELLING,over.termOver||{}),term('object','원장',{known:true,type_fits:true,type:'문서'})]],
    excluded_fact_allowed:{depends_on:{subject:['문서'],object:['문서']}}}],...(over.cover||{})},...(over.runOver||{})};
 const doc={id:'d1',version:2,current_version:2,generation:3,latest_run:over.latest_run||{status:'success'},
  items:[{id:'o1',kind:'ontology',revision:4,status:'approved',payload:{entities,aliases:over.aliases||[],relations:over.relations||[{id:'depends_on',description:'d',subject_types:['문서'],object_types:['문서'],quote:'q'}]}}]};
 const passageMap=over.passages||{[SPELLING]:{passages:[{offset:3,quote:`앞 ${SPELLING} 뒤`}],total:1},[NAME]:{passages:[{offset:9,quote:`앞 ${NAME} 뒤`}],total:1}};
 state.router=over.router||((path,method,data)=>{
  if(path==='/api/runs/r1')return Promise.resolve(run);
  if(path==='/api/documents/d1')return Promise.resolve(doc);
  const m=path.match(/^\/api\/documents\/d1\/passages\?version=2&text=(.*)$/);
  if(m){const key=decodeURIComponent(m[1]);return Promise.resolve(passageMap[key]||{passages:[],total:0});}
  if(path==='/api/items/o1/dictionary-terms')return Promise.resolve({});
  throw new Error('unexpected '+path);});
 const click=async dataset=>{const b={dataset};for(const [type,fn] of listeners)if(type==='click')fn({target:{closest:()=>b}});await flush();};
 const open=()=>click({termAdd:'',run:'r1',chunk:'0',row:'0',field:'subject'});
 const form=el('#term-form');
 const change=async(name,value)=>{form.onchange({target:{name,value}});await flush();};
 const input=async(name,value)=>{form.oninput({target:{name,value}});};
 const submit=async()=>{form.elements.actor.value='검토자';form.elements.reason.value='이유';form.onsubmit({preventDefault(){},target:form});await flush();};
 return {el,ctx,call,flush,fire,click,open,change,input,submit,requests,opened,notices,timers,run,doc,form,state,term};
}
const html=h=>[h.el('#term-targets').innerHTML,h.el('#term-types').innerHTML,h.el('#term-passages').innerHTML,h.el('#term-preview').innerHTML,h.el('#term-disclosures').innerHTML,h.el('#term-heading').innerHTML].join('\n');
const groups={};

groups.condition1_alias=async()=>{
 const h=make();await h.open();
 assert(h.el('#term-dialog').open);assert.equal(h.el('#term-submit').disabled,true);
 await h.change('target','0');assert.equal(h.el('#term-submit').disabled,true);
 await h.change('passage','0');assert.equal(h.el('#term-submit').disabled,false);
 await h.submit();
 const post=h.requests.find(r=>r.method==='POST');
 assert.equal(post.path,'/api/items/o1/dictionary-terms');
 assert.deepEqual(JSON.parse(JSON.stringify(post.data)),{revision:4,expected_version:2,expected_generation:3,kind:'alias',spelling:SPELLING,quote:`앞 ${SPELLING} 뒤`,actor:'검토자',reason:'이유',entity:'plan'});
 assert.equal(h.el('#term-dialog').open,false);assert.deepEqual(h.opened,[['d1',2]]);assert.equal(h.notices.length,1);
};
groups.condition1_entity=async()=>{
 const h=make();await h.open();
 await h.change('kind','entity');
 assert(h.requests.some(r=>r.path.endsWith('text='+encodeURIComponent(SPELLING))));
 await h.change('etype','문서');await h.change('passage','0');assert.equal(h.el('#term-submit').disabled,false);
 await h.submit();
 const post=h.requests.find(r=>r.method==='POST');
 assert.deepEqual(JSON.parse(JSON.stringify(post.data)),{revision:4,expected_version:2,expected_generation:3,kind:'entity',spelling:SPELLING,quote:`앞 ${SPELLING} 뒤`,actor:'검토자',reason:'이유',name:SPELLING,entity_type:'문서'});
 assert.deepEqual(h.opened,[['d1',2]]);
};
groups.b1_zero_spelling=async()=>{
 const h=make({passages:{[NAME]:{passages:[{offset:9,quote:`앞 ${NAME} 뒤`}],total:1}}});await h.open();
 assert.equal(h.el('#term-kind-alias').disabled,true);
 assert(h.el('#term-alias-absent').textContent.includes('이 표기는 원문에 그대로 나오지 않아 별칭 근거를 만들 수 없습니다'));
 assert(h.el('#term-passages').innerHTML.includes('이 이름도 원문에 그대로 나오지 않아 근거로 쓸 구간이 없습니다'));
 assert.equal(h.el('#term-submit').disabled,true);
 h.requests.length=0;
 await h.input('name',NAME);await h.fire();
 assert(h.requests.some(r=>r.path.endsWith('text='+encodeURIComponent(NAME))),'the name is queried');
 await h.change('etype','문서');await h.change('passage','0');assert.equal(h.el('#term-submit').disabled,false);
 await h.submit();
 const post=h.requests.find(r=>r.method==='POST');
 assert.equal(post.data.name,NAME);assert.equal(post.data.quote,`앞 ${NAME} 뒤`);assert.equal(post.data.spelling,SPELLING);
};
groups.b1_name_zero_disables=async()=>{
 const h=make({passages:{[NAME]:{passages:[{offset:9,quote:`앞 ${NAME} 뒤`}],total:1}}});await h.open();
 await h.input('name',NAME);await h.fire();await h.change('etype','문서');await h.change('passage','0');
 assert.equal(h.el('#term-submit').disabled,false);
 await h.input('name','없는 이름');await h.fire();
 assert(h.el('#term-passages').innerHTML.includes('근거로 쓸 구간이 없습니다'));
 assert.equal(h.el('#term-submit').disabled,true);
};
groups.b1_late_response=async()=>{
 const held={};
 const h=make({passages:{},router:(path)=>{
  if(path==='/api/runs/r1')return Promise.resolve(h.run);if(path==='/api/documents/d1')return Promise.resolve(h.doc);
  const key=decodeURIComponent(path.split('text=')[1]);
  if(key===SPELLING)return Promise.resolve({passages:[],total:0});
  return new Promise(r=>held[key]=r);}});
 await h.open();
 await h.input('name','가');await h.fire();await h.input('name','나');await h.fire();
 assert(held['가']&&held['나']);
 held['나']({passages:[{offset:0,quote:'나의 구간'}],total:1});await h.flush();
 held['가']({passages:[{offset:0,quote:'가의 구간'}],total:1});await h.flush();
 const shown=h.el('#term-passages').innerHTML;
 assert(shown.includes('나의 구간'));assert(!shown.includes('가의 구간'));
};
groups.b2_labels=async()=>{
 const entities=[{id:'plan',name:'납품 계획',type:'문서',quote:'납품 계획 본문'}];
 const lacking={alias:'납품계획',entity:'plan',quote:'납품계획만 있는 구간',judged_alias:'납품계획',judged_entity:'plan',judged_name:'납품 계획'};
 const holding={alias:'계획서',entity:'plan',quote:'계획서와 납품 계획이 함께 있는 구간',judged_alias:'계획서',judged_entity:'plan',judged_name:'납품 계획'};
 const plain={alias:'평범한별칭',entity:'plan',quote:'평범한별칭 납품 계획'};
 const h=make();
 const render=(aliases,judgements)=>h.call(`renderDictionary(${JSON.stringify({entities,aliases,relations:[]})},${JSON.stringify(judgements)})`);
 const LACK='원문이 두 표기를 한 구간에서 연결하지 않음 · 검토자 판단',HOLD='이 구간에 두 표기가 함께 있지만 검토자 판단으로 기록된 별칭';
 const a=render([lacking],{});assert(a.includes(LACK));assert(!a.includes(HOLD));
 const b=render([holding],{});assert(b.includes(HOLD));assert(!b.includes(LACK));
 const judgements={'납품계획':{actor:'홍길동',reason:'<b>같은 문서</b>',revision:5}};
 const c=render([plain,lacking],judgements);
 assert(c.indexOf('납품계획만 있는 구간')<c.indexOf('평범한별칭 납품 계획'),'judged rows are listed first');
 assert(c.includes('납품계획만 있는 구간'));assert(c.split('납품 계획 본문').length-1>=2,'the entity quote, besides the entity list');
 assert(c.includes('납품<span class="ws" title="공백">␣</span>계획'),'the judged name shows its space');
 assert(c.includes('홍길동')&&c.includes('&lt;b&gt;같은 문서&lt;/b&gt;')&&!c.includes('<b>같은'));
 assert(c.includes('r5'));
};
groups.control_conditions=async()=>{
 const h=make();
 const base={status:'success',version_current:true,current_dictionary_status:'approved',run_id:'r1'};
 const chunk=(extra={})=>({excluded_facts:[{subject:'<i>x</i>',predicate:'p',object:'o',code:'unverifiable_quote',quote:'q'},{subject:'<i>x</i>',predicate:'p',object:'o',code:'unknown_entity',quote:'q',...extra}],
  excluded_fact_terms:[[],[{field:'subject',spelling:'<i>x</i>',known:false,type_fits:false,type:null,name_of:[],name_of_total:0}]],excluded_fact_allowed:{p:{subject:['t'],object:['t']}}});
 const render=(c,ch=chunk())=>{h.ctx.c=c;h.ctx.chunk=ch;return h.call('renderExcludedFacts(chunk,c,3)');};
 const present=render(base);
 assert.equal((present.match(/data-term-add/g)||[]).length,1);
 assert(present.includes('data-run="r1"')&&present.includes('data-chunk="3"')&&present.includes('data-row="1"')&&present.includes('data-field="subject"'));
 const button=present.match(/<button[^>]*>/)[0];assert(!button.includes('x'),'a button holds no spelling');
 assert(!present.includes('<i>x'));
 for(const [label,c] of [['running',{...base,status:'running'}],['queued',{...base,status:'queued'}],['old version',{...base,version_current:false}],
   ['rejected',{...base,current_dictionary_status:'rejected'}],['missing dictionary',{...base,current_dictionary_status:null}]])
  assert(!render(c).includes('data-term-add'),label);
 const mismatch=render(base,chunk({code:'type_mismatch'}));assert(!mismatch.includes('data-term-add'),'type_mismatch');
 const quoteOnly=render(base,{excluded_facts:[{subject:'s',predicate:'p',object:'o',code:'unverifiable_quote',quote:'q'}],excluded_fact_terms:[[]],excluded_fact_allowed:{}});
 assert(!quoteOnly.includes('data-term-add'),'unverifiable_quote');
};
groups.control_chunk_index=async()=>{
 const h=make();
 const unknown={excluded_facts:[{subject:'S',predicate:'p',object:'o',code:'unknown_entity',quote:'q'}],excluded_fact_terms:[[{field:'subject',spelling:'S',known:false,type_fits:false,type:null,name_of:[],name_of_total:0}]],excluded_fact_allowed:{p:{subject:['t'],object:['t']}}};
 const quiet={excluded_facts:[],excluded_fact_terms:[],excluded_fact_allowed:{},coverage:[],dictionary_gaps:[],claim_base:0,offset:0,length:1};
 h.ctx.c={available:true,status:'success',published:true,version_current:true,current_dictionary_status:'approved',run_id:'r1',excluded_fact_count:1,excluded_spelling_count:1,excluded_type_count:0,
  chunks:[quiet,{...quiet,...unknown}]};
 const out=h.call('renderCoverage(c)');
 assert(out.includes('data-chunk="1"')&&out.includes('data-row="0"'));assert(!out.includes('data-chunk="0"'));
};
groups.visible=async()=>{
 const h=make(),v=s=>{h.ctx.s=s;return h.call('visibleSpelling(s)');};
 assert.notEqual(v('납품 계획'),v('납품계획'));
 assert.equal(v(' '),'<span class="ws" title="공백">␣</span>');
 assert.equal((v(' ').match(/␣/g)||[]).length,1);
 assert(v('a\nb').includes('↵'));assert(v('a\tb').includes('⇥'));
 for(const [ch,code] of [[' ','00A0'],['　','3000'],['\u0085','0085'],['​','200B'],['‍','200D'],['﻿','FEFF'],['­','00AD']])
  assert(v('a'+ch+'b').includes(`[U+${code}]`),code);
 assert(v('<b> x').includes('&lt;b&gt;')&&!v('<b> x').includes('<b>'));
 assert(!v('가').includes('<span'));
};
groups.target_search=async()=>{
 const entities=[{id:'plan',name:NAME,type:'문서',quote:'q'},{id:'x',name:'납품 계획 보고',type:'문서',quote:'q'},{id:'y',name:SPELLING,type:'문서',quote:'q'}];
 const h=make({entities,termOver:{name_of:['plan'],name_of_total:1}});await h.open();
 let t=h.el('#term-targets').innerHTML;
 assert(!t.includes('checked'),'nothing is preselected');assert.equal(h.el('#term-submit').disabled,true);
 assert(t.indexOf('이름이 같은 개체')>=0&&t.indexOf('value="2"')<t.indexOf('value="0"'),'name_of ids come first');
 await h.input('search','납품계획');
 t=h.el('#term-targets').innerHTML;
 assert(t.includes('value="0"'));assert(!t.includes('value="1"'),'a spacing variant is not matched');
 assert(!t.includes('checked'));
};
groups.type_preview=async()=>{
 const entities=[{id:'plan',name:NAME,type:'문서',quote:'q'},{id:'org',name:'조직명',type:'조직',quote:'q'}];
 const h=make({entities});await h.open();
 const WARN='경고: 개체 유형: 조직 · 이 관계 주어의 허용 타입(문서)에 없습니다';
 await h.change('target','1');assert(h.el('#term-preview').innerHTML.includes(WARN));
 await h.change('target','0');assert(h.el('#term-preview').innerHTML.includes('개체 유형: 문서 · 이 관계 주어의 허용 타입에 들어갑니다'));
 await h.change('kind','entity');await h.change('etype','조직');assert(h.el('#term-preview').innerHTML.includes(WARN));
 await h.change('etype','문서');assert(!h.el('#term-preview').innerHTML.includes('경고'));
};
groups.equal_name=async()=>{
 const h=make();await h.open();await h.change('kind','entity');
 assert(!h.el('#term-preview').innerHTML.includes('같은 이름의 개체가 이미 있습니다'));
 await h.input('name',NAME);assert(h.el('#term-preview').innerHTML.includes('같은 이름의 개체가 이미 있습니다'));
 await h.input('name','납품  계획');assert(!h.el('#term-preview').innerHTML.includes('같은 이름의 개체가 이미 있습니다'),'only an exact match warns');
};
groups.cost=async()=>{
 const d=(stage,credits)=>({stage,reported_credits:credits,credit_unit:credits==null?null:'OpenRouter credits'});
 const h=make({usage:{credits:[{value:999,unit:'OpenRouter credits'}],details:[d('extract',1),d('extract',2),d('extract',3),d('extract',4),d('extract',null),d('quote_repair',100)]}});
 await h.open();
 const n=h.el('#term-disclosures').innerHTML;
 assert(n.includes('11개 청크를 모두 새로 호출합니다'));
 assert(n.includes('27.5 OpenRouter credits'),n);assert(n.includes('이 실행의 에이전트·모델 기준 추정 · 보정 제외'));assert(!n.includes('999'));
 const u=make({usage:{credits:[{value:999,unit:'x'}],details:[d('extract',null),d('extract',null)]}});await u.open();
 const m=u.el('#term-disclosures').innerHTML;assert(m.includes('예상 비용(재추출 한 번): 미확인'));assert(!m.includes('999'));
};
groups.run_note_and_409=async()=>{
 const NOTE='이 문서 버전의 실행이 대기·처리 중으로 표시되어 있습니다. 끝나기 전에 저장하면 거부됩니다.';
 const running=make({latest_run:{status:'running'}});await running.open();
 assert.equal(running.el('#term-run-note').textContent,NOTE);assert.equal(running.el('#term-run-note').hidden,false);
 const done=make();await done.open();assert.equal(done.el('#term-run-note').hidden,true);
 const base=make();
 const h=make({router:(path,method)=>{
  if(method==='POST')return Promise.reject(Object.assign(new Error('실행이 사전을 쓰는 중입니다'),{status:409}));
  return base.state.router(path,method);}});
 await h.open();await h.change('target','0');await h.change('passage','0');await h.submit();
 assert.equal(h.el('#term-error').textContent,'실행이 사전을 쓰는 중입니다');
 assert.equal(h.el('#term-dialog').open,true);assert.equal(h.opened.length,0);assert.equal(h.el('#term-submit').disabled,false);
};
groups.disclosures=async()=>{
 const h=make({entities:[{id:'plan',name:SPELLING,type:'문서',quote:'q'}]});await h.open();await h.change('target','0');await h.change('passage','0');
 let n=h.el('#term-disclosures').innerHTML;
 for(const s of ['승인 전까지 이 사전의 정의는 질문의 사전 검색에서 빠집니다. 이미 승인된 사실에 고정된 의미는 그대로 보입니다.','재승인 뒤 이전에 승인된 사실이 있는 개체는 의미 후보 두 개로 보일 수 있습니다.',
   '추가를 되돌리는 버튼은 없습니다. 내용 수정에서 지우세요.','사전을 반려하고 다시 생성하면 추가한 항목은 이력에만 남습니다.','여러 표기를 고치면 모두 추가한 뒤 한 번만 승인하세요.'])
  assert(n.includes(s),s);
 assert(!n.includes('이어갈 수 있었던 기록'),'the failed-run note only on failed runs');
 assert(!n.includes('같은 대상이라는 연결은 이 검토자의 판단으로 기록됩니다'),'the passage holds the name');
 const failed=make({runOver:{}});failed.run.coverage.published=false;failed.run.status='error';await failed.open();
 assert(failed.el('#term-disclosures').innerHTML.includes('같은 revision으로 다시 실행하면 이어갈 수 있었던 기록은 새 revision에서 쓰이지 않습니다.'));
 const j=make({entities:[{id:'plan',name:'납품 계획서',type:'문서',quote:'q'}]});await j.open();await j.change('target','0');await j.change('passage','0');
 const note=j.el('#term-disclosures').innerHTML;
 assert(note.includes('원문의 이 구간에는 대상 개체의 이름(납품<span class="ws" title="공백">␣</span>계획서)이 없습니다. 같은 대상이라는 연결은 이 검토자의 판단으로 기록됩니다.'),note);
};
groups.known_spelling=async()=>{
 const h=make({aliases:[{alias:SPELLING,entity:'plan',quote:'q'}]});await h.open();
 assert.equal(h.el('#term-known').hidden,false);assert.equal(h.el('#term-fields').hidden,true);
 assert(!h.requests.some(r=>r.path.includes('/passages')));
};
groups.escaping=async()=>{
 const evil='<img src=x onerror=1>';
 const h=make({entities:[{id:'<b>id</b>',name:'<u>n</u>',type:'<i>t</i>',quote:'q'}],termOver:{spelling:evil},
  passages:{[evil]:{passages:[{offset:0,quote:'<script>alert(1)</script> '+evil}],total:1}}});
 h.run.coverage.chunks[0].excluded_fact_terms[0][0].spelling=evil;
 await h.open();await h.change('target','0');await h.change('passage','0');
 const aliasOut=html(h);await h.change('kind','entity');await h.change('passage','0');
 const out=aliasOut+html(h);
 for(const raw of ['<img','<script','<b>','<u>','<i>'])assert(!out.includes(raw),raw);
 await h.change('kind','alias');
 assert(!html(h).includes('<script'));
};
groups.review_note=async()=>{
 const judged={alias:'a',entity:'e',quote:'q',judged_alias:'a',judged_entity:'e',judged_name:'n'};
 const h=make();

 h.ctx.docForReview={latest_run:{status:'success'},items:[{id:'o',kind:'ontology',payload:{aliases:[judged,{...judged,alias:'b',judged_alias:'b'},{alias:'c',entity:'e',quote:'q'}]}},{id:'p',kind:'ontology',payload:{aliases:[]}}]};
 h.call('selectedDoc=docForReview');
 h.call("openReview('o','approved')");
 assert.equal(h.el('#review-judged-note').hidden,false);assert(h.el('#review-judged-note').textContent.includes('2개'));
 h.call("openReview('p','approved')");assert.equal(h.el('#review-judged-note').hidden,true);
 h.call("openReview('o','rejected')");assert.equal(h.el('#review-judged-note').hidden,true);
};
groups.approval_escaping=async()=>{
 const h=make();
 const e={id:'<e1>',name:'<n0>',type:'t',quote:'<q2>'};
 const a={alias:'<a1>',entity:'<e1>',quote:'<q1>',judged_alias:'<a1>',judged_entity:'<e1>',judged_name:'<n1>'};
 const out=h.call(`renderDictionary(${JSON.stringify({entities:[e],aliases:[a],relations:[]})},${JSON.stringify({'<a1>':{actor:'<x1>',reason:'<r1>',revision:2}})})`);
 for(const raw of ['<a1>','<e1>','<q1>','<q2>','<n1>','<x1>','<r1>'])assert(!out.includes(raw),raw);
 assert(out.includes('&lt;q1&gt;')&&out.includes('&lt;q2&gt;')&&out.includes('&lt;x1&gt;'));
};
groups.item_passes_judgements=async()=>{
 const h=make();
 Object.assign(h.ctx,{renderRelationTypeWarnings:()=>'',renderDictionaryReconciliations:()=>'',renderDictionaryExclusions:()=>'',renderExtractionAdmission:()=>'',bulkItemLabel:()=>'',badge:()=>''});
 vm.runInContext(source.slice(source.indexOf('function renderItem('),source.indexOf('function openReview(')).split('\nfunction ')[0],h.ctx);
 const payload={entities:[{id:'plan',name:'납품 계획',type:'t',quote:'eq'}],aliases:[{alias:'별칭',entity:'plan',quote:'aq',judged_alias:'별칭',judged_entity:'plan',judged_name:'납품 계획'}],relations:[]};
 h.ctx.item={id:'o',kind:'ontology',status:'approved',revision:1,run_id:'r',payload,alias_judgements:{'별칭':{actor:'검토자갑',reason:'이유을',revision:3}}};
 const out=h.call('renderItem(item)');
 assert(out.includes('검토자갑')&&out.includes('이유을'));
};
let finished=false;process.on('exit',()=>{if(!finished){console.error('the test never finished');process.exitCode=1;}});
groups.same_name_current_dictionary=async()=>{
 // The report's pin says `plan` shares the spelling's name; the current dictionary renamed it and gave the name to `p2`.
 const renamed=[{id:'plan',name:'다른 이름',type:'문서',quote:'q'},{id:'p2',name:SPELLING,type:'문서',quote:'q'}];
 const h=make({entities:renamed,termOver:{name_of:['plan'],name_of_total:1}});await h.open();
 const t=h.el('#term-targets').innerHTML;
 const head=t.indexOf('이름이 같은 개체'),rest=t.indexOf('그 밖의 개체');
 assert(head>=0&&rest>head);
 const p2=t.indexOf('p2'),plan=t.indexOf('plan');
 assert(p2>head&&p2<rest,'p2 shares the name');assert(plan>rest,'plan no longer does');
 // More than five entities share the name: none is capped.
 const many=Array.from({length:8},(_,i)=>({id:'e'+i,name:SPELLING,type:'문서',quote:'q'})).concat([{id:'other',name:'기타',type:'문서',quote:'q'}]);
 const g=make({entities:many,termOver:{name_of:['e0','e1','e2','e3','e4'],name_of_total:8}});await g.open();
 const u=g.el('#term-targets').innerHTML,r2=u.indexOf('그 밖의 개체');
 for(let i=0;i<8;i++)assert(u.indexOf('e'+i)<r2&&u.indexOf('e'+i)>=0,'e'+i);
 assert(u.indexOf('other')>r2);
 // Without any same-name entity there is no heading.
 const none=make({entities:[{id:'plan',name:'다른 이름',type:'문서',quote:'q'}]});await none.open();
 assert(!none.el('#term-targets').innerHTML.includes('이름이 같은 개체'));
};
groups.preview_current_relation=async()=>{
 const entities=[{id:'doc',name:'문서명',type:'문서',quote:'q'},{id:'prod',name:'산출물명',type:'산출물',quote:'q'}];
 // The run's report allowed only 문서; the current relation was widened to 산출물: no warning.
 const wide=make({entities,relations:[{id:'depends_on',description:'d',subject_types:['문서','산출물'],object_types:['문서'],quote:'q'}]});
 wide.run.coverage.chunks[0].excluded_fact_allowed={depends_on:{subject:['문서'],object:['문서']}};
 await wide.open();await wide.change('target','1');
 let p=wide.el('#term-preview').innerHTML;
 assert(!p.includes('경고'),p);assert(p.includes('허용 타입에 들어갑니다'));
 // The reverse: the report allowed 산출물, the current relation no longer does: warn with the current list.
 const narrow=make({entities,relations:[{id:'depends_on',description:'d',subject_types:['문서'],object_types:['문서'],quote:'q'}]});
 narrow.run.coverage.chunks[0].excluded_fact_allowed={depends_on:{subject:['문서','산출물'],object:['문서']}};
 await narrow.open();await narrow.change('target','1');
 p=narrow.el('#term-preview').innerHTML;assert(p.includes('경고')&&p.includes('허용 타입(문서)에 없습니다'),p);
 await narrow.change('kind','entity');await narrow.change('etype','산출물');assert(narrow.el('#term-preview').innerHTML.includes('경고'));
 // A relation that is gone shows nothing.
 const gone=make({entities,relations:[]});await gone.open();await gone.change('target','1');
 assert.equal(gone.el('#term-preview').innerHTML,'');
};
groups.kind_switch_during_first_response=async()=>{
 let release;const base=make();
 const h=make({router:(path,method,data)=>{
  if(path.includes('text='+encodeURIComponent(SPELLING))&&!release){return new Promise(r=>release=()=>r({passages:[{offset:3,quote:`앞 ${SPELLING} 뒤`}],total:1}));}
  return base.state.router(path,method,data);}});
 const opening=h.open();await h.flush();
 await h.change('kind','entity');
 release();await h.flush();
 assert.equal(h.call('termState.kind'),'entity');
 assert.equal(h.el('#term-kind-entity').checked,true);assert.equal(h.el('#term-kind-alias').checked,false);
 assert.equal(h.el('#term-alias-fields').hidden,true);
 await opening;
};
groups.open_race_guards=async()=>{
 // Guard after the document fetch: a first open that answers late must not replace the second.
 const base=make();let late;let docCalls=0;
 const h=make({router:(path,method,data)=>{
  if(path==='/api/documents/d1'&&++docCalls===1)return new Promise(r=>late=()=>r(base.doc));
  return base.state.router(path,method,data);}});
 const a=h.open();await h.flush();
 await h.open();
 const second=h.call('termState');
 late();await h.flush();
 assert.equal(h.call('termState'),second,'the late first open is dropped');
 // Guard after the first passages fetch.
 const b2=make();let firstPassages,calls=0;
 const g=make({router:(path,method,data)=>{
  if(path.includes('/passages')&&++calls===1)return new Promise(r=>firstPassages=()=>r({passages:[],total:0}));
  return b2.state.router(path,method,data);}});
 const x=g.open();await g.flush();
 await g.open();
 assert.equal(g.el('#term-kind-alias').checked,true);
 firstPassages();await g.flush();
 assert.equal(g.el('#term-kind-alias').checked,true,'the late first open does not flip the radios');
 assert.equal(g.call('termState.spellingTotal'),1);
};
groups.cancel_resets=async()=>{
 const NAMEQ=encodeURIComponent('늦은 이름');
 // A held name query answers after cancel.
 let late;const base=make({passages:{}});
 const h=make({router:(path,method,data)=>{
  if(path.includes('text='+NAMEQ))return new Promise(r=>late=()=>r({passages:[{offset:0,quote:'늦게 온 구간'}],total:1}));
  return base.state.router(path,method,data);}});
 await h.open();await h.input('name','늦은 이름');await h.fire();
 h.el('#cancel-term').onclick();
 late();await h.flush();
 assert(!h.el('#term-passages').innerHTML.includes('늦게 온 구간'),'a response after cancel is dropped');
 // Cancel while the first passages fetch is held.
 let first;const b2=make();
 const g=make({router:(path,method,data)=>{
  if(path.includes('/passages')&&!first)return new Promise(r=>first=()=>r({passages:[{offset:0,quote:'q'}],total:1}));
  return b2.state.router(path,method,data);}});
 const opening=g.open();await g.flush();
 g.el('#cancel-term').onclick();first();await g.flush();
 assert.equal(g.el('#term-kind-alias').checked,false,'open continues after cancel');
 // A pending name timer is cleared by cancel.
 const t=make({passages:{}});await t.open();await t.input('name','타이머 이름');
 t.el('#cancel-term').onclick();await t.fire();
 assert(!t.requests.some(r=>r.path.includes('text='+encodeURIComponent('타이머 이름'))));
};
groups.kind_switch_clears_timer=async()=>{
 const h=make();await h.open();
 await h.change('kind','entity');await h.input('name','대기 이름');
 await h.change('kind','alias');
 h.requests.length=0;await h.fire();
 assert(!h.requests.some(r=>r.path.includes('text='+encodeURIComponent('대기 이름'))),'the pending name query is cancelled by the kind switch');
};
groups.mixed_credit_units=async()=>{
 const d=(unit,v)=>({stage:'extract',reported_credits:v,credit_unit:unit});
 const h=make({usage:{details:[d('A credits',1),d('B credits',3)]}});await h.open();
 assert(h.el('#term-disclosures').innerHTML.includes('예상 비용(재추출 한 번): 미확인'));
 const same=make({usage:{details:[d('A credits',1),d('A credits',3)]}});await same.open();
 assert(same.el('#term-disclosures').innerHTML.includes('22 A credits'));
};
groups.field_escaped=async()=>{
 const h=make();
 const evil='"><i>x</i>';
 h.ctx.c={status:'success',version_current:true,current_dictionary_status:'approved',run_id:'r1'};
 h.ctx.chunk={excluded_facts:[{subject:'S',predicate:'p',object:'o',code:'unknown_entity',quote:'q'}],
  excluded_fact_terms:[[{field:evil,spelling:'S',known:false,type_fits:false,type:null,name_of:[],name_of_total:0}]],excluded_fact_allowed:{p:{[evil]:['t']}}};
 let out='';try{out=h.call('renderExcludedFacts(chunk,c,0)');}catch(e){out='THROW '+e.message;}
 assert(out.includes('data-term-add'),out);assert(!out.includes('"><i>'),out);
};
groups.approval_alias_marked=async()=>{
 const h=make();
 const e={id:'plan',name:'납품 계획',type:'t',quote:'eq'};
 const a={alias:'납품 계획서',entity:'plan',quote:'납품 계획서만',judged_alias:'납품 계획서',judged_entity:'plan',judged_name:'납품 계획'};
 const out=h.call(`renderDictionary(${JSON.stringify({entities:[e],aliases:[a],relations:[]})},{})`);
 assert(out.includes('<h4>납품<span class="ws" title="공백">␣</span>계획서 →'),out);
};
groups.esc_is_the_real_one=async()=>{
 const h=make();
 assert.equal(h.call("esc('<\"&\\'>')"),'&lt;&quot;&amp;&#39;&gt;');
};
groups.search_panel_headings=async()=>{
 const REST='그 밖의 개체',NONE='검색어가 들어간 개체 ID·이름이 없습니다',SAME='이름이 같은 개체';
 // Every match shares the spelling's name: no empty "other" block and no "none" sentence.
 const same=[{id:'y',name:SPELLING,type:'문서',quote:'q'},{id:'z',name:SPELLING,type:'문서',quote:'q'},{id:'other',name:'기타',type:'문서',quote:'q'}];
 const a=make({entities:same});await a.open();await a.input('search','납품');
 let t=a.el('#term-targets').innerHTML;
 assert(t.includes(SAME)&&t.includes('value="0"')&&t.includes('value="1"')&&!t.includes('value="2"'));
 assert(!t.includes(REST),t);assert(!t.includes(NONE),t);
 // Same-name matches and others: both headings.
 await a.input('search','');t=a.el('#term-targets').innerHTML;
 assert(t.includes(SAME)&&t.includes(REST)&&!t.includes(NONE));
 // Only other matches: a plain list, no headings.
 await a.input('search','기타');t=a.el('#term-targets').innerHTML;
 assert(t.includes('value="2"')&&!t.includes(SAME)&&!t.includes(REST)&&!t.includes(NONE));
 // No match at all.
 await a.input('search','없는검색어');t=a.el('#term-targets').innerHTML;
 assert(t.includes(NONE)&&!t.includes(SAME)&&!t.includes(REST)&&!t.includes('<input'));
};
groups.forced_entity_when_spelling_absent=async()=>{
 // The user touched the kind choice before the first response, which then reports no occurrence of the spelling.
 let release;const base=make({passages:{}});
 const h=make({passages:{},router:(path,method,data)=>{
  if(path.includes('text='+encodeURIComponent(SPELLING))&&!release)return new Promise(r=>release=()=>r({passages:[],total:0}));
  return base.state.router(path,method,data);}});
 const opening=h.open();await h.flush();
 await h.change('kind','entity');await h.change('kind','alias');
 assert.equal(h.call('termState.kind'),'alias');
 h.requests.length=0;release();await h.flush();
 assert.equal(h.call('termState.kind'),'entity','alias is not possible without an occurrence');
 assert.equal(h.el('#term-kind-entity').checked,true);assert.equal(h.el('#term-kind-alias').checked,false);
 assert.equal(h.el('#term-kind-alias').disabled,true);
 assert(h.requests.some(r=>r.path.endsWith('text='+encodeURIComponent(SPELLING))),'the name is queried for the entity form');
 await opening;
};
groups[process.argv[1]]().then(()=>{finished=true;console.log('ok');},error=>{finished=true;console.error(error);process.exit(1);});
'''


def run_group(group):
    result = subprocess.run(['node', '-e', SCRIPT, group], cwd=ROOT, text=True, capture_output=True)
    return result.returncode, result.stdout + result.stderr


class TermDialogUI(unittest.TestCase):
    def check(self, group):
        code, output = run_group(group)
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip(), 'ok')


def _add(group):
    setattr(TermDialogUI, f'test_{group}', lambda self: self.check(group))


for _group in ('condition1_alias', 'condition1_entity', 'b1_zero_spelling', 'b1_name_zero_disables', 'b1_late_response', 'b2_labels',
               'control_conditions', 'control_chunk_index', 'visible', 'target_search', 'type_preview', 'equal_name', 'cost',
               'run_note_and_409', 'disclosures', 'known_spelling', 'escaping', 'review_note', 'approval_escaping', 'item_passes_judgements', 'same_name_current_dictionary', 'preview_current_relation',
               'kind_switch_during_first_response', 'open_race_guards', 'cancel_resets', 'kind_switch_clears_timer', 'mixed_credit_units',
               'field_escaped', 'approval_alias_marked', 'esc_is_the_real_one', 'search_panel_headings', 'forced_entity_when_spelling_absent'):
    _add(_group)


class TermDialogMarkup(unittest.TestCase):
    def test_every_element_the_dialog_code_uses_exists_in_index_html(self):
        source = (ROOT / 'verinote/web/app.js').read_text()
        html = (ROOT / 'verinote/web/index.html').read_text()
        code = source[source.index('function visibleSpelling('):source.index('let searchTimer;')]
        code += source[source.index('function openReview('):source.index("$('#cancel-review')")]
        used = set(re.findall(r"\$\('#([a-z-]+)'\)", code))
        self.assertGreater(len(used), 10)
        missing = sorted(i for i in used if f'id="{i}"' not in html)
        self.assertEqual(missing, [])

    def test_the_dialog_has_a_named_input_for_every_field_the_handlers_read(self):
        html = (ROOT / 'verinote/web/index.html').read_text()
        dialog = html[html.index('<dialog id="term-dialog"'):html.index('</dialog>', html.index('<dialog id="term-dialog"'))]
        for name in ('kind', 'search', 'name', 'actor', 'reason'):
            self.assertIn(f'name="{name}"', dialog)

    def test_no_inline_handler_is_added(self):
        html = (ROOT / 'verinote/web/index.html').read_text()
        source = (ROOT / 'verinote/web/app.js').read_text()
        self.assertIsNone(re.search(r'\son[a-z]+="', html))
        self.assertNotIn('onclick="', source)


if __name__ == '__main__':
    unittest.main()
