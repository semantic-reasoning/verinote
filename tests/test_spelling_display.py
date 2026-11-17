"""Dictionary and extraction spellings on the review screens show whitespace kinds and invisible characters, run through the real app.js in node's vm."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const grab=name=>{const start=source.search(new RegExp('^(async )?function '+name+'\\('+'','m'));assert(start>=0,name);
 const rest=source.slice(start+1),found=rest.search(/\n(?=function |async function |let |const |\/\/|document\.)/),end=found<0?rest.length:found;return source.slice(start,start+1+end);};
const ctx=vm.createContext({console,$:()=>({}),document:{addEventListener(){}}});
vm.runInContext(source.match(/^const esc = .*$/m)[0],ctx);
vm.runInContext(source.match(/^const SPELLING_KEYS=.*$/m)[0],ctx);
vm.runInContext('const labels={pending:"검토 대기",approved:"승인",rejected:"반려",held:"보류"};function sourceLink(){return "";}function badge(){return "";}function date(v){return String(v);}function usageNumber(v){return String(v);}function bulkItemLabel(){return "";}',ctx);
for(const name of ['visibleSpelling','spellingJSON','renderDictionaryReconciliations','renderDictionaryExclusions','judgedAlias','renderDictionary','renderLookupSpellingProblems','renderItem','renderCoverage','renderExcludedFacts','renderExcludedClaims','renderExclusionTerms','renderLossAudit','scopedRow','meaningTitleHtml','meaningStatus','meaningTitle','renderMeaning','bulkItemLabelHtml','renderRelationTypeWarnings','renderExtractionAdmission','renderSpacingNameRows'])
  vm.runInContext(grab(name),ctx);
const call=(fn,...args)=>{ctx.args=args;return vm.runInContext(fn+'(...args)',ctx);};
const NBSP='\u00a0',ZWSP='\u200b';
const rec=(before,after)=>({available:true,total_count:1,applied:1,deferred:0,details_loaded:true,items:[{chunk:0,key:before.name,status:'applied',reason:'r',before,after}]});
const groups={
 async reproductions(){
  for(const [after,mark] of [[{id:'a',name:'정산'+NBSP+'문서',type:'문서'},'[U+00A0]'],[{id:'a',name:'정산 '+ZWSP+'문서',type:'문서'},'[U+200B]']]){
   const html=call('renderDictionaryReconciliations',rec({id:'a',name:'정산 문서',type:'문서'},after));
   const [before,proposal]=[...html.matchAll(/<pre class="source">([\s\S]*?)<\/pre>/g)].map(m=>m[1]);
   assert.notStrictEqual(before,proposal);assert(proposal.includes(mark),mark);assert(!before.includes('[U+'));
  }
 },
 async escaping(){
  const bad='<b>&"\'',spell={id:bad,name:bad,type:bad,alias:bad,entity:bad,subject:bad,object:bad,subject_types:[bad],object_types:[bad]};
  const dict={entities:[{id:bad,name:bad,type:bad,quote:'q'}],aliases:[{alias:bad,entity:bad,quote:'q'}],relations:[{id:'r',subject_types:[bad],object_types:[bad],description:'d',quote:'q'}]};
  const outputs={
   reconciliation:call('renderDictionaryReconciliations',rec(spell,spell)),
   exclusions:call('renderDictionaryExclusions',{available:true,total_count:1,direct_count:1,dependent_count:0,details_loaded:true,items:[{chunk:0,kind:'entities',row:0,identifier:bad,reason:'r'}]}),
   dictionary:call('renderDictionary',dict,{}),
   problems:call('renderLookupSpellingProblems',[{kind:'entities',index:0,spelling:bad,reason:'prefix'}]),
   fact:call('renderItem',{id:'i',kind:'fact',status:'pending',revision:1,run_id:'r',payload:{subject:bad,predicate:'p',object:bad,quote:'q'}}),
   loss:call('renderLossAudit',{available:true,chunks:[{flags:[],unresolved:[{subject:bad,predicate:'p',object:bad,reason:'x',origin:'recovery',row:0,span:null,quote:'q'}]}]}),
   row:call('scopedRow',{entities:[],relations:[]},[bad,'p',bad],null),
   meaning:call('renderMeaning',{ref:'e',name:bad,type:bad,id:'i',fact_count:0,version:1,approved_facts:[{subject:bad,object:bad,predicate:'p',subject_type:bad,object_type:bad,quote_excerpt:'q'}]},'s')};
  for(const [name,html] of Object.entries(outputs)){assert(html.includes('&lt;b&gt;&amp;'),name);assert(!html.includes('<b>'),name);}
 },
 async json_layout(){
  const plain={id:'a',name:'정산 문서',n:[1,{x:null}],empty:{},list:[],subject_types:['사람','문서'],flag:true,quote:'"q"\n'};
  const out=call('spellingJSON',plain);
  assert.strictEqual(out.replace(/&quot;/g,'"').replace(/<span[^>]*>(.*?)<\/span>/g,'$1').replace(/␣/g,' '),JSON.stringify(plain,null,2).replace(/&/g,'&amp;'));
  assert.strictEqual(call('spellingJSON',{a:[1,2],b:{c:'d'}}).replace(/&quot;/g,'"'),JSON.stringify({a:[1,2],b:{c:'d'}},null,2));
  assert.strictEqual(call('spellingJSON',{description:'a b',quote:'a'+NBSP+'b'}).includes('<span'),false);
  assert(call('spellingJSON',{name:'a"b\\c'}).includes('a\\&quot;b\\\\c'));
  assert(call('spellingJSON',{name:'a\nb\tc'}).includes('↵') && call('spellingJSON',{name:'a\nb\tc'}).includes('⇥'));
 },
 async reveals(){
  const odd='a'+NBSP+'b'+ZWSP+'c\nd';
  const dict={entities:[{id:odd,name:odd,type:odd,quote:'q'}],aliases:[{alias:odd,entity:odd,quote:'q'}],relations:[{id:'r',subject_types:[odd],object_types:[odd],description:'d',quote:'q'}]};
  const term={field:'subject',spelling:odd,known:false,type_fits:false,type:null,name_of:[odd],name_of_total:1};
  const chunk={offset:0,length:1,claim_base:0,coverage:[],dictionary_gaps:[],excluded_claims:[],excluded_facts:[{subject:odd,predicate:'p',object:odd,code:'unknown_entity',quote:'q'}],excluded_fact_terms:[[term,{...term,field:'object',known:true,type_fits:false,type:odd}]],excluded_fact_allowed:{p:{subject:[odd],object:[odd]}}};
  const cov={available:true,status:'success',published:true,validated_chunks:1,total_chunks:1,extracted_predicates:0,approved_predicates:0,emitted_fact_count:1,excluded_fact_count:1,excluded_spelling_count:1,excluded_type_count:0,saved_fact_count:0,claim_count:0,excluded_claim_count:0,gap_count:0,excluded_gap_count:0,version_current:true,current_dictionary_revision:1,dictionary_revision:1,chunks:[chunk]};
  const proof=(h)=>h.includes('[U+00A0]')&&h.includes('[U+200B]')&&h.includes('↵');
  const outputs={
   exclusions:call('renderDictionaryExclusions',{available:true,total_count:1,direct_count:1,dependent_count:0,details_loaded:true,items:[{chunk:0,kind:'entities',row:0,identifier:odd,reason:'r'}]}),
   dictionary:call('renderDictionary',dict,{}),
   judged:call('renderDictionary',{...dict,aliases:[{alias:odd,entity:odd,quote:'q',judged_name:odd}]},{}),
   problems:call('renderLookupSpellingProblems',[{kind:'names',index:0,spelling:odd,reason:'control'}]),
   fact:call('renderItem',{id:'i',kind:'fact',status:'pending',revision:1,run_id:'r',payload:{subject:odd,predicate:'p',object:odd,quote:'q'}}),
   coverage:call('renderCoverage',cov),
   loss:call('renderLossAudit',{available:true,chunks:[{flags:[],unresolved:[{subject:odd,predicate:'p',object:odd,reason:'x',origin:'recovery',row:0,span:null,quote:'q'}]}]}),
   row:call('scopedRow',{entities:[],relations:[]},[odd,'p',odd],null),
   meaning:call('renderMeaning',{ref:'e',name:odd,type:odd,id:'i',fact_count:0,version:1,approved_facts:[{subject:odd,object:odd,predicate:'p',quote_excerpt:'q'}]},'s'),
   meaningRow:call('scopedRow',{entities:[{ref:'e',name:odd,id:'i',version:1}],relations:[]},['x','p','y'],['e',null,null]),
   json:call('spellingJSON',{subject:odd,object:odd,subject_types:[odd]}),
   rows:call('spellingJSON',{rows:[[odd,'p',odd]],proofs:[{row:[odd,'p',odd]}]}),
   bulk:call('bulkItemLabelHtml',{kind:'fact',payload:{subject:odd,predicate:'p',object:odd}})};
  for(const [name,html] of Object.entries(outputs))assert(name==='meaningRow'?html.includes('[U+00A0]'):proof(html),name);
  assert((outputs.rows.match(/\[U\+00A0\]/g)||[]).length===4);
  assert(call('spellingJSON',{rows:[['a','p'+NBSP+'q','b']]}).includes('p'+NBSP+'q'));
  const reconciliation=call('renderDictionaryReconciliations',{available:true,total_count:1,applied:1,deferred:0,details_loaded:true,items:[{chunk:0,key:odd,status:'applied',reason:'r',before:{id:odd},after:{id:odd}}]});
  assert(proof(reconciliation));
  assert(!call('renderItem',{id:'i',kind:'fact',status:'pending',revision:1,run_id:'r',payload:{subject:'a b',predicate:'p',object:'c',quote:'q'}}).includes('data-bulk-item="i" aria-label="␣'));
 },
};
groups[process.argv[1]]().then(()=>console.log('ok'),error=>{console.error(error);process.exit(1);});
'''


@unittest.skipUnless(shutil.which('node'), 'node is required')
class SpellingDisplay(unittest.TestCase):
    def check(self, group):
        result = subprocess.run(['node', '-e', SCRIPT, group], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')

    def test_issue_reproductions_render_differently(self):
        self.check('reproductions')

    def test_html_special_characters_stay_escaped(self):
        self.check('escaping')

    def test_json_layout_matches_for_plain_data(self):
        self.check('json_layout')

    def test_each_site_reveals_invisible_characters(self):
        self.check('reveals')

    def test_no_raw_json_dump_of_spellings_remains(self):
        source = (ROOT / 'verinote/web/app.js').read_text(encoding='utf-8')
        dumps = re.findall(r'esc\(JSON\.stringify\([^)]*\)\)', source)
        self.assertEqual(dumps, ['esc(JSON.stringify(item.payload,null,2))', 'esc(JSON.stringify(c,null,2))', 'esc(JSON.stringify(k))', 'esc(JSON.stringify(value))'])


if __name__ == '__main__':
    unittest.main()
