"""Browser-side behaviour of the type-fix control and its dialog, run through the real app.js in node's vm with the term dialog's harness."""
import re
import subprocess
import unittest
from pathlib import Path

from test_term_dialog_ui import SCRIPT as TERM_SCRIPT

from verinote.extraction_loss import MAX_RECOVERY_CALLS

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = TERM_SCRIPT[:TERM_SCRIPT.index('const groups={};')] + r'''
const known=(field,spelling,entity,type,fits)=>({field,spelling,known:true,entity,type,type_fits:fits,name_of:[],name_of_total:0});
const ENT=()=>[{id:'정산 문서',name:'정산 문서',type:'산출물',quote:'q'},{id:'납품 계획',name:'납품 계획',type:'산출물',quote:'q'},{id:'끝',name:'끝',type:'산출물',quote:'q'},
 {id:'검수 활동',name:'검수 활동',type:'활동',quote:'q'},{id:'다른표기',name:'다른표기',type:'조직',quote:'q'}];
const REL=()=>[{id:'produces',description:'d',subject_types:['활동'],object_types:['산출물'],quote:'q'},{id:'owns',description:'d',subject_types:['조직'],object_types:['산출물'],quote:'q'}];
const ALLOWED={produces:{subject:['활동'],object:['산출물']},owns:{subject:['조직'],object:['산출물']}};
const MIS=[{subject:'정산 문서',predicate:'produces',object:'끝',code:'type_mismatch',quote:'q'},[known('subject','정산 문서','정산 문서','산출물',false),known('object','끝','끝','산출물',true)]];
function typeHarness(over={}){
 const rows=[over.first||MIS,...(over.rows||[])];
 const h=make({entities:over.entities||ENT(),relations:over.relations||REL(),cover:{chunks:[{excluded_facts:rows.map(r=>r[0]),excluded_fact_terms:rows.map(r=>r[1]),excluded_fact_allowed:over.allowed||ALLOWED}]}});
 const route=h.state.router;h.state.router=(path,method,data)=>path==='/api/items/o1/dictionary-types'?Promise.resolve({}):route(path,method,data);
 h.doc.items.push(...(over.facts||[]));Object.assign(h.doc.items[0],over.item||{});
 h.openType=(field='subject')=>h.click({typeFix:'',run:'r1',chunk:'0',row:'0',field});
 h.pick=async(name,value)=>{h.el('#type-form').onchange({target:{name,value}});await h.flush();};
 h.send=async()=>{const f=h.el('#type-form');f.elements.actor.value='검토자';f.elements.reason.value='이유';f.onsubmit({preventDefault(){},target:f});await h.flush();
  return JSON.parse(JSON.stringify(h.requests.find(r=>r.method==='POST')?.data??null));};
 h.text=()=>['#type-heading','#type-context','#type-options','#type-disclosures'].map(s=>h.el(s).innerHTML).join('\n');
 return h;
}
const fact=(subject,predicate,object,status)=>({id:subject+predicate+object+status,kind:'fact',status,payload:{subject,predicate,object,quote:'q'}});
const groups={};
groups.button_gate=async()=>{
 const h=make();
 const both={excluded_facts:[{subject:'정산 문서',predicate:'owns',object:'검수 활동',code:'type_mismatch',quote:'q'}],
  excluded_fact_terms:[[known('subject','정산 문서','정산 문서','산출물',false),known('object','검수 활동','검수 활동','활동',false)]],excluded_fact_allowed:ALLOWED};
 const base={status:'success',version_current:true,current_dictionary_status:'approved',run_id:'r1'};
 const render=c=>{h.ctx.c=c;h.ctx.chunk=both;return h.call('renderExcludedFacts(chunk,c,2)');};
 const out=render(base);
 assert.equal((out.match(/data-type-fix/g)||[]).length,2,out);
 assert(out.includes('data-field="subject"')&&out.includes('data-field="object"')&&out.includes('data-chunk="2"')&&out.includes('data-row="0"'));
 assert(!out.includes('data-term-add'));
 for(const [label,c] of [['old version',{...base,version_current:false}],['running',{...base,status:'running'}],['queued',{...base,status:'queued'}],
   ['rejected',{...base,current_dictionary_status:'rejected'}],['missing dictionary',{...base,current_dictionary_status:null}]])
  assert(!render(c).includes('data-type-fix'),label);
};
groups.mixed_row=async()=>{
 const h=make();
 h.ctx.c={status:'success',version_current:true,current_dictionary_status:'approved',run_id:'r1'};
 h.ctx.chunk={excluded_facts:[{subject:'없는 표기',predicate:'produces',object:'검수 활동',code:'unknown_entity',quote:'q'}],
  excluded_fact_terms:[[{field:'subject',spelling:'없는 표기',known:false,type_fits:null,type:null,entity:null,name_of:[],name_of_total:0},known('object','검수 활동','검수 활동','활동',false)]],excluded_fact_allowed:ALLOWED};
 const out=h.call('renderExcludedFacts(chunk,c,0)');
 assert.equal((out.match(/data-term-add/g)||[]).length,1);assert.equal((out.match(/data-type-fix/g)||[]).length,1);
 assert(/data-term-add[^>]*data-field="subject"/.test(out),out);assert(/data-type-fix[^>]*data-field="object"/.test(out),out);
};
groups.no_double_space=async()=>{
 const h=make();
 for(const current of [true,false]){
  h.ctx.row={code:'type_mismatch',versionCurrent:current,allowed:{subject:['활동'],object:['산출물']},terms:MIS[1]};
  const out=h.call('renderExclusionTerms(row)');
  assert(!out.includes('  '),out);assert(!out.includes(' <br>'),out);
  assert.equal(out.includes('사전의 개체 타입을 바꾸거나'),current);
 }
};
groups.no_default=async()=>{
 const h=typeHarness();await h.openType();
 assert(h.el('#type-dialog').open);
 const o=h.el('#type-options').innerHTML;
 assert(o.includes('name="kind" value="widen"')&&o.includes('name="kind" value="retype"'));
 assert(!/checked/.test(o),o);assert.equal(h.el('#type-submit').disabled,true);
 await h.pick('kind','retype');assert.equal(h.el('#type-submit').disabled,true,'retype needs a type');
 await h.pick('etype','활동');assert.equal(h.el('#type-submit').disabled,false);
};
groups.post_widen=async()=>{
 const h=typeHarness();await h.openType();await h.pick('kind','widen');
 assert.deepEqual(await h.send(),{revision:4,expected_version:2,expected_generation:3,kind:'widen',relation:'produces',field:'subject',entity:'정산 문서',entity_type:'산출물',actor:'검토자',reason:'이유'});
 assert.equal(h.requests.find(r=>r.method==='POST').path,'/api/items/o1/dictionary-types');
 assert.equal(h.el('#type-dialog').open,false);assert.deepEqual(h.opened,[['d1',2]]);
 assert.deepEqual(h.notices,[['사전 타입을 고쳤습니다. 사전은 승인할 때까지 검토 대기 상태입니다.']]);
};
groups.post_retype=async()=>{
 const h=typeHarness();await h.openType();await h.pick('kind','retype');await h.pick('etype','활동');
 assert.deepEqual(await h.send(),{revision:4,expected_version:2,expected_generation:3,kind:'retype',relation:'produces',field:'subject',entity:'정산 문서',entity_type:'활동',actor:'검토자',reason:'이유'});
};
groups.post_alias_and_current_type=async()=>{
 // The row used the alias spelling 정산문서, and the run's pin said 산출물; the current dictionary says 조직.
 const entities=ENT();entities[0].type='조직';
 const h=typeHarness({entities,first:[{...MIS[0],subject:'정산문서'},[known('subject','정산문서','정산 문서','산출물',false),MIS[1][1]]]});
 await h.openType();
 assert(h.el('#type-context').innerHTML.includes('별칭, 개체 ID 정산<span class="ws" title="공백">␣</span>문서'));
 await h.pick('kind','widen');
 const body=await h.send();
 assert.equal(body.entity,'정산 문서');assert.equal(body.entity_type,'조직');
};
groups.retype_choices=async()=>{
 const entities=ENT().concat([{id:'관리',name:'관리',type:'관리 활동',quote:'q'}]),relations=REL();relations[0].subject_types=['관리 활동','활동'];
 const h=typeHarness({entities,relations});await h.openType();
 const radios=[...h.el('#type-options').innerHTML.matchAll(/name="etype" value="([^"]*)"/g)].map(m=>m[1]);
 assert.deepEqual(radios,['관리 활동','활동']);
 assert(h.el('#type-options').innerHTML.includes('> 관리<span class="ws" title="공백">␣</span>활동</label>'));
};
groups.notes_by_subject=async()=>{
 const h=typeHarness();await h.openType();
 const n=h.el('#type-disclosures').innerHTML;
 for(const s of ['추가한 뒤','지우세요','추가한 항목'])assert(!n.includes(s),s);
 for(const s of ['원래 타입으로 고치세요','이 타입을 빼고',`보충 점검 호출이 최대 ${process.argv[2]}회 더 있을 수 있습니다`,'여러 곳의 타입을 고치려면 모두 고친 뒤 한 번만 승인하세요.','사전을 반려하고 다시 생성하면 이 수정은 이력에만 남습니다.'])assert(n.includes(s),s);
 const t=make();await t.open();
 const m=t.el('#term-disclosures').innerHTML;
 for(const s of ['여러 표기를 고치면 모두 추가한 뒤 한 번만 승인하세요.','추가를 되돌리는 버튼은 없습니다. 내용 수정에서 지우세요.','사전을 반려하고 다시 생성하면 추가한 항목은 이력에만 남습니다.',`최대 ${process.argv[2]}회`])assert(m.includes(s),s);
 assert(!m.includes('원래 타입으로'));
};
groups.effect_counts=async()=>{
 const unknownRow=[{subject:'미지',predicate:'produces',object:'끝',code:'unknown_entity',quote:'q'},[{field:'subject',spelling:'미지',known:false,type_fits:null,type:null,entity:null,name_of:[],name_of_total:0},MIS[1][1]]];
 const ownsRow=[{subject:'정산 문서',predicate:'owns',object:'끝',code:'type_mismatch',quote:'q'},[known('subject','정산 문서','정산 문서','산출물',false),MIS[1][1]]];
 const facts=[fact('다른표기','owns','정산 문서','approved'),fact('검수 활동','produces','정산 문서','pending'),fact('다른표기','owns','정산 문서','rejected'),fact('검수 활동','produces','끝','approved')];
 // A quote exclusion has no per-field reading; it never counts.
 const quoteRow=[{subject:'정산 문서',predicate:'produces',object:'끝',code:'unverifiable_quote',quote:'q'},null];
 const h=typeHarness({rows:[unknownRow,ownsRow,quoteRow],facts});await h.openType();
 let o=h.el('#type-options').innerHTML;
 assert(o.includes('이 관계의 주어 자리에 타입 산출물인 개체 3개를 모두 허용합니다.'),o);
 assert(o.includes('이 실행이 제외한 응답 관계 중 1개가 이 수정으로 사전 검사를 통과합니다.'),o);
 await h.pick('etype','활동');o=h.el('#type-options').innerHTML;
 assert(o.includes('그 관계의 허용 타입에 없는 것: 2개(승인 1개).'),o);
 assert.equal((o.match(/중 1개가 이 수정으로/g)||[]).length,2,o);
 const one=typeHarness({first:[{subject:'다른표기',predicate:'produces',object:'끝',code:'type_mismatch',quote:'q'},[known('subject','다른표기','다른표기','조직',false),MIS[1][1]]]});
 await one.openType();
 assert(one.el('#type-options').innerHTML.includes('타입 조직인 개체 1개를 허용합니다.'),one.el('#type-options').innerHTML);
};
groups.counts_by_revision=async()=>{
 const h=typeHarness({item:{revision:5}});await h.openType();
 const o=h.el('#type-options').innerHTML;
 assert(o.includes('이 화면에는 실행 당시 사전 r4 기준 판정만 있어, 현재 사전 r5로 통과할 관계 수는 세지 않았습니다.'),o);
 assert(!o.includes('사전 검사를 통과합니다'));
};
groups.blocked_states=async()=>{
 // Last of its type: 다른표기 is the only 조직 and owns allows 조직.
 const last=typeHarness({first:[{subject:'다른표기',predicate:'produces',object:'끝',code:'type_mismatch',quote:'q'},[known('subject','다른표기','다른표기','조직',false),MIS[1][1]]]});
 await last.openType();
 let o=last.el('#type-options').innerHTML;
 assert(o.includes('개체 다른표기: 타입 조직의 마지막 개체입니다. 관계 owns에서 이 타입을 허용하므로 바꿀 수 없습니다. 대신 허용 타입 넓히기를 고르거나 내용 수정에서 관계 정의를 먼저 고치세요.'),o);
 assert(/value="retype"\s+disabled/.test(o),o);assert(!o.includes('name="etype"'));
 await last.pick('kind','retype');assert.equal(last.el('#type-submit').disabled,true);
 await last.pick('kind','widen');assert.equal(last.el('#type-submit').disabled,false);
 // Already fits in the current dictionary.
 const entities=ENT();entities[0].type='활동';
 const fits=typeHarness({entities});await fits.openType();
 o=fits.el('#type-options').innerHTML;
 assert(o.includes('현재 사전에서는 이 개체의 타입이 이미 허용 타입에 들어갑니다. 고칠 것이 없습니다.'));assert(!o.includes('name="kind"'));
 await fits.pick('kind','widen');assert.equal(fits.el('#type-submit').disabled,true);
 // The entity is gone from the current dictionary.
 const gone=typeHarness({entities:ENT().slice(1)});await gone.openType();
 o=gone.el('#type-options').innerHTML;
 assert(o.includes('현재 사전에 이 개체 ID가 없습니다. 고칠 대상이 없습니다.'));assert(!o.includes('name="kind"'));
 await gone.pick('kind','widen');assert.equal(gone.el('#type-submit').disabled,true);
 // The relation is gone from the current dictionary.
 const norel=typeHarness({relations:REL().slice(1)});await norel.openType();
 o=norel.el('#type-options').innerHTML;
 assert(o.includes('현재 사전에 이 관계가 없습니다. 고칠 대상이 없습니다.'));assert(!o.includes('name="kind"'));
 await norel.pick('kind','widen');assert.equal(norel.el('#type-submit').disabled,true);
};
groups.threshold=async()=>{
 const over=typeHarness({item:{relation_type_warnings:{threshold:1}}});await over.openType();
 assert(over.el('#type-options').innerHTML.includes('넓히면 이 관계의 주어 허용 타입이 2개가 되어 경고 기준 1개를 넘습니다.'));
 const at=typeHarness({item:{relation_type_warnings:{threshold:2}}});await at.openType();
 assert(!at.el('#type-options').innerHTML.includes('경고 기준'));
};
groups.spacing=async()=>{
 const WARN='공백이나 보이지 않는 문자만 다른 허용 타입이 있습니다: 산출<span class="ws" title="공백">␣</span>물 · 이 개체의 타입: 산출물. 띄어쓰기나 보이지 않는 문자가 다르면 같은 타입으로 보지 않습니다.';
 const entities=ENT().concat([{id:'x',name:'x',type:'산출 물',quote:'q'}]),relations=REL();relations[0].subject_types=['산출 물'];
 const h=typeHarness({entities,relations});await h.openType();
 const o=h.el('#type-options').innerHTML;
 assert(o.includes(WARN),o);assert(!/checked/.test(o));assert.equal(h.el('#type-submit').disabled,true);
 const plain=typeHarness();await plain.openType();assert(!plain.el('#type-options').innerHTML.includes('보이지 않는 문자만 다른'));
 const zw=ENT().concat([{id:'z',name:'z',type:'산출\u200b물',quote:'q'}]),zr=REL();zr[0].subject_types=['산출\u200b물'];
 const z=typeHarness({entities:zw,relations:zr});await z.openType();
 assert(z.el('#type-options').innerHTML.includes('보이지 않는 문자만 다른 허용 타입이 있습니다: 산출<span class="ws" title="보이지 않는 문자">[U+200B]</span>물'),z.el('#type-options').innerHTML);
};
groups.escaping=async()=>{
 const evil='<img src=x onerror=1>';
 const entities=ENT();entities[0].id=evil;entities[0].type='<i>t</i>';
 const relations=REL();relations[0].id='<b>r</b>';
 const h=typeHarness({entities,relations,allowed:{'<b>r</b>':{subject:['활동'],object:['산출물']}},first:[{...MIS[0],subject:evil,predicate:'<b>r</b>'},[known('subject',evil,evil,'<i>t</i>',false),MIS[1][1]]]});
 await h.openType();await h.pick('kind','retype');await h.pick('etype','활동');
 const out=h.text();
 for(const raw of ['<img','<i>','<b>'])assert(!out.includes(raw),raw+out);
};
groups.run_note=async()=>{
 const NOTE='이 문서 버전의 실행이 대기·처리 중으로 표시되어 있습니다. 끝나기 전에 저장하면 거부됩니다.';
 const running=typeHarness();running.doc.latest_run={status:'running'};await running.openType();
 assert.equal(running.el('#type-run-note').textContent,NOTE);assert.equal(running.el('#type-run-note').hidden,false);
 const done=typeHarness();await done.openType();
 assert.equal(done.el('#type-run-note').textContent,'');assert.equal(done.el('#type-run-note').hidden,true);
};
groups.stale_open=async()=>{
 // A first open whose document answers after a second open must not replace the second.
 const h=typeHarness(),route=h.state.router;let late,calls=0;
 h.state.router=(path,method,data)=>path==='/api/documents/d1'&&++calls===1?new Promise(r=>late=()=>r(route(path,method,data))):route(path,method,data);
 h.openType('object');await h.flush();
 await h.openType('subject');
 const second=h.call('typeState');
 assert.equal(second.field,'subject');
 late();await h.flush();
 assert.equal(h.call('typeState'),second);assert.equal(h.call('typeState.field'),'subject');
};
let finished=false;process.on('exit',()=>{if(!finished){console.error('the test never finished');process.exitCode=1;}});
groups[process.argv[1]]().then(()=>{finished=true;console.log('ok');},error=>{finished=true;console.error(error);process.exit(1);});
'''


def run_group(group):
    result = subprocess.run(['node', '-e', SCRIPT, group, str(MAX_RECOVERY_CALLS)], cwd=ROOT, text=True, capture_output=True)
    return result.returncode, result.stdout + result.stderr


class TypeDialogUI(unittest.TestCase):
    def check(self, group):
        code, output = run_group(group)
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip(), 'ok')


for _group in re.findall(r'^groups\.([a-z_]+)=', SCRIPT[SCRIPT.index('const groups={};'):], re.M):
    setattr(TypeDialogUI, f'test_{_group}', lambda self, g=_group: self.check(g))


class TypeDialogMarkup(unittest.TestCase):
    def test_the_dialog_has_the_fields_the_handlers_read(self):
        html = (ROOT / 'verinote/web/index.html').read_text()
        dialog = html[html.index('<dialog id="type-dialog"'):html.index('</dialog>', html.index('<dialog id="type-dialog"'))]
        for name in ('actor', 'reason'):
            self.assertIn(f'name="{name}"', dialog)
        for ident in ('type-form', 'type-heading', 'type-run-note', 'type-context', 'type-options', 'type-disclosures', 'type-error', 'cancel-type', 'type-submit'):
            self.assertIn(f'id="{ident}"', dialog)


if __name__ == '__main__':
    unittest.main()
