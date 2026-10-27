"""Draft corrections, namespace reconciliation, durable audit and legacy resume."""
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dictionary_exclusions as exclusion_tests
import test_workflow as workflow

from verinote.dictionary_filter import reconcile_generated_dictionary
from verinote.dictionary_pipeline import PREVIOUS_IMMUTABLE_PROMPT
from verinote.dictionary_reconciliation import MAX_DETAILS, read_report, validate_report
from verinote.ontology import source_spans, validate_dictionary
from verinote.usage import atomic_json

EMPTY = {'entities': [], 'aliases': [], 'relations': []}


def entity(key, name=None, kind='Concept', quote='Alpha Beta A B Gamma'):
    return dict(id=key, name=name or key, type=kind, quote=quote)


def wire(row, source):
    spans = source_spans(source)
    return {k:v for k,v in row.items() if k!='quote'} | {'quote_ref':[spans[0]['id'],spans[-1]['id']]}


def revision(kind, before, after, source):
    return dict(kind=kind, key=before['alias' if kind=='aliases' else 'id'],
                before={k:v for k,v in before.items() if k!='quote'}, after=wire(after,source), reason='Later source corrects the draft')


class Reconcile(unittest.TestCase):
    def merge(self, previous, output, current, prior_source=''):
        return reconcile_generated_dictionary(previous, output, source_spans(current), prior_source+current, current)

    def test_actual_bilingual_same_response_collision_merges(self):
        source='초개인화 (Hyper-Personalization)는 같은 개념이다.'
        value=copy.deepcopy(EMPTY)
        value['entities']=[wire(entity(key,quote=source),source) for key in ('초개인화','Hyper-Personalization')]
        value['aliases']=[wire(dict(alias='Hyper-Personalization',entity='초개인화',quote=source),source)]
        merged, excluded, changes=self.merge(EMPTY,value,source)
        self.assertEqual([r['id'] for r in merged['entities']],['초개인화'])
        self.assertEqual(merged['aliases'][0]['entity'],'초개인화')
        self.assertEqual(excluded,[]);self.assertEqual(changes[0]['status'],'applied')
        self.assertEqual(changes[0]['before']['id'],'Hyper-Personalization')
        self.assertEqual(validate_dictionary(merged,source),merged)

    def test_prior_entity_can_be_demoted_and_alias_dependents_redirected(self):
        source='Alpha Beta A B Gamma'
        previous=dict(entities=[entity('Alpha'),entity('Beta')],aliases=[dict(alias='A',entity='Alpha',quote=source)],relations=[])
        value=copy.deepcopy(EMPTY);value['aliases']=[wire(dict(alias='Alpha',entity='Beta'),source)]
        merged, excluded, changes=self.merge(previous,value,source)
        self.assertEqual([r['id'] for r in merged['entities']],['Beta'])
        self.assertEqual({r['entity'] for r in merged['aliases']},{'Beta'})
        self.assertEqual(len(changes),2);self.assertEqual(excluded,[])
        self.assertEqual(previous['aliases'][0]['entity'],'Alpha')

    def test_joint_name_and_alias_revision_validates_as_one_transaction(self):
        old='Alpha (A).'; current='Beta (A) is the correct name.'
        previous=dict(entities=[entity('id','Alpha',quote=old)],aliases=[dict(alias='A',entity='id',quote=old)],relations=[])
        value=copy.deepcopy(EMPTY)
        value['revisions']=[revision('entities',previous['entities'][0],entity('id','Beta'),current),
                            revision('aliases',previous['aliases'][0],previous['aliases'][0],current)]
        merged, excluded, changes=self.merge(previous,value,current,old)
        self.assertEqual(merged['entities'][0]['name'],'Beta')
        self.assertEqual(merged['aliases'][0]['quote'],current)
        self.assertEqual([r['status'] for r in changes],['applied','applied']);self.assertEqual(excluded,[])
        value['revisions'].pop()
        merged, _, changes=self.merge(previous,value,current,old)
        self.assertEqual(merged,previous);self.assertEqual(changes[0]['status'],'deferred')

    def test_explicit_alias_retarget_corrects_old_mapping(self):
        source='Alpha Beta A B Gamma'
        previous=dict(entities=[entity('Alpha'),entity('Beta')],aliases=[dict(alias='A',entity='Alpha',quote=source)],relations=[])
        value=copy.deepcopy(EMPTY);value['revisions']=[revision('aliases',previous['aliases'][0],dict(alias='A',entity='Beta'),source)]
        merged, _, changes=self.merge(previous,value,source)
        self.assertEqual(merged['aliases'][0]['entity'],'Beta');self.assertEqual(changes[0]['status'],'applied')

    def test_joint_type_and_relation_revision_preserves_referential_integrity(self):
        source='Alpha is a Device.'
        before_entity=entity('Alpha',kind='Service',quote=source)
        relation=dict(id='uses',description='Uses',subject_types=['Service'],object_types=['Service'],quote=source)
        previous=dict(entities=[before_entity],aliases=[],relations=[relation])
        value=copy.deepcopy(EMPTY)
        value['revisions']=[revision('entities',before_entity,before_entity|{'type':'Device'},source),
                            revision('relations',relation,relation|{'subject_types':['Device'],'object_types':['Device']},source)]
        merged,_,changes=self.merge(previous,value,source)
        self.assertEqual(merged['entities'][0]['type'],'Device')
        self.assertEqual(merged['relations'][0]['subject_types'],['Device'])
        self.assertTrue(all(r['status']=='applied' for r in changes))
        value['revisions'].pop()
        merged,_,changes=self.merge(previous,value,source)
        self.assertEqual(merged,previous);self.assertEqual(changes[0]['status'],'deferred')

    def test_new_alias_uses_revised_name_and_failed_revision_keeps_valid_additions(self):
        old='Alpha';current='Beta (B) and Gamma (G).'
        previous=dict(entities=[entity('stable','Alpha',quote=old)],aliases=[],relations=[])
        value=dict(entities=[wire(entity('Gamma'),current)],
                   aliases=[wire(dict(alias='B',entity='stable'),current),wire(dict(alias='G',entity='Gamma'),current)],relations=[],
                   revisions=[revision('entities',previous['entities'][0],entity('stable','Beta'),current)])
        merged,excluded,changes=self.merge(previous,value,current,old)
        self.assertEqual({r['alias'] for r in merged['aliases']},{'B','G'})
        self.assertEqual(excluded,[]);self.assertEqual(changes[0]['status'],'applied')
        value['revisions'][0]['before']['name']='stale'
        merged,excluded,changes=self.merge(previous,value,current,old)
        self.assertEqual(merged['entities'][0]['name'],'Alpha')
        self.assertEqual([r['alias'] for r in merged['aliases']],['G'])
        self.assertEqual(excluded[0]['code'],'alias_evidence');self.assertEqual(changes[0]['status'],'deferred')

    def test_new_relation_uses_revised_type_and_deferral_filters_invalid_addition(self):
        source='Alpha is a Device.'
        previous=dict(entities=[entity('Alpha',kind='Service',quote=source)],aliases=[],relations=[])
        relation=dict(id='uses',description='Uses',subject_types=['Device'],object_types=['Device'])
        value=dict(entities=[],aliases=[],relations=[wire(relation,source)],
                   revisions=[revision('entities',previous['entities'][0],entity('Alpha',kind='Device'),source)])
        merged,excluded,changes=self.merge(previous,value,source)
        self.assertEqual(merged['relations'][0]['subject_types'],['Device'])
        self.assertEqual(excluded,[]);self.assertEqual(changes[0]['status'],'applied')
        value['revisions'][0]['before']['type']='stale'
        merged,excluded,changes=self.merge(previous,value,source)
        self.assertEqual(merged,previous);self.assertEqual(excluded[0]['code'],'relation_undeclared_type')
        self.assertEqual(changes[0]['status'],'deferred')

    def test_stale_and_duplicate_revisions_defer_without_losing_additions(self):
        source='Alpha Beta A B Gamma';previous=dict(entities=[entity('Alpha')],aliases=[],relations=[])
        value=copy.deepcopy(EMPTY);value['entities']=[wire(entity('Gamma'),source)]
        proposal=revision('entities',previous['entities'][0],entity('Alpha','Beta'),source)
        for proposals in ([proposal,proposal], [dict(proposal,before=proposal['before']|{'name':'Other'})]):
            value['revisions']=proposals
            merged,_,changes=self.merge(previous,value,source)
            self.assertEqual([r['name'] for r in merged['entities']],['Alpha','Gamma'])
            self.assertTrue(all(r['status']=='deferred' for r in changes))

    def test_invalid_dependencies_prevent_merge_without_stopping(self):
        old='Alpha A';current='Alpha Beta'
        previous=dict(entities=[entity('Alpha',quote=old),entity('Beta',quote=current)],aliases=[dict(alias='A',entity='Alpha',quote=old)],relations=[])
        value=copy.deepcopy(EMPTY);value['aliases']=[wire(dict(alias='Alpha',entity='Beta'),current)]
        merged,excluded,changes=self.merge(previous,value,current,old)
        self.assertEqual(merged,previous);self.assertEqual(len(excluded),1)
        self.assertEqual(changes[0]['status'],'deferred')

    def test_cycles_competing_targets_and_reverse_namespace_are_deferred(self):
        source='Alpha Beta A B Gamma';previous=dict(entities=[entity('Alpha'),entity('Beta'),entity('Gamma')],aliases=[],relations=[])
        for pairs in ([('Alpha','Beta'),('Beta','Alpha')],[('Alpha','Beta'),('Alpha','Gamma')]):
            value=copy.deepcopy(EMPTY);value['aliases']=[wire(dict(alias=a,entity=b),source) for a,b in pairs]
            merged,_,changes=self.merge(previous,value,source)
            self.assertEqual(merged,previous);self.assertTrue(changes);self.assertTrue(all(e['status']=='deferred' for e in changes))
        previous['aliases']=[dict(alias='A',entity='Alpha',quote=source)]
        value=copy.deepcopy(EMPTY);value['entities']=[wire(entity('A'),source)]
        merged,_,changes=self.merge(previous,value,source)
        self.assertEqual(merged,previous);self.assertEqual(changes[0]['status'],'deferred')

    def test_revision_and_canonicalization_overlap_defer_both(self):
        source='Alpha Beta A B Gamma';previous=dict(entities=[entity('Alpha'),entity('Beta')],aliases=[],relations=[])
        value=copy.deepcopy(EMPTY);value['aliases']=[wire(dict(alias='Alpha',entity='Beta'),source)]
        value['revisions']=[revision('entities',previous['entities'][0],entity('Alpha','Gamma'),source)]
        merged,_,changes=self.merge(previous,value,source)
        self.assertEqual(merged,previous);self.assertEqual([r['status'] for r in changes],['deferred','deferred'])
        current='Alpha Gamma'
        value['aliases']=[wire(dict(alias='Alpha',entity='Beta'),current)]
        value['revisions']=[revision('entities',previous['entities'][1],entity('Beta','Gamma'),current)]
        merged,_,changes=self.merge(previous,value,current,source)
        self.assertEqual(merged,previous);self.assertEqual(changes[0]['status'],'deferred')

    def test_structural_errors_still_fail_before_any_mutation(self):
        source='Alpha Beta A B Gamma';previous=dict(entities=[entity('Alpha')],aliases=[],relations=[])
        value=copy.deepcopy(EMPTY);value['revisions']=[revision('entities',previous['entities'][0],entity('Alpha','Beta'),source)]
        value['aliases']=[wire(dict(alias='A',entity='unknown'),source)]
        before=copy.deepcopy(previous)
        with self.assertRaises(ValueError):self.merge(previous,value,source)
        self.assertEqual(previous,before)


class Pipeline(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown
    new_doc=exclusion_tests.Generation.new_doc
    run_job=exclusion_tests.Generation.run_job
    checkpoint=exclusion_tests.Generation.checkpoint

    def generated(self,payload):
        source=''.join(s['text'] for s in payload['sources'])
        value=copy.deepcopy(EMPTY)
        value['entities']=[wire(entity(k,quote=source),source) for k in ('Alpha','Beta')]
        value['aliases']=[wire(dict(alias='Alpha',entity='Beta'),source)]
        return value

    def test_applied_audit_resumes_once_and_remains_pending(self):
        doc=self.new_doc('Alpha Beta'.ljust(4000)+'END')
        first,_=self.run_job(doc,[self.generated,TimeoutError('stop')])
        saved=json.loads(self.checkpoint(first).read_text());self.assertEqual(saved['schema_version'],3)
        self.assertEqual(saved['reconciliations']['chunks'][0]['applied'],1)
        second,calls=self.run_job(doc,[EMPTY]);self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([r['offset'] for r in calls],[4000])
        report=read_report(self.root,self.store,second['id'])
        self.assertEqual(report['applied'],1);self.assertEqual(report['deferred'],0)
        self.assertEqual(self.store.document(doc['id'])['items'][0]['status'],'pending')
        self.assertEqual(self.store.snapshot()['facts'],[])

    def test_deferred_duplicate_alias_exclusions_remain_valid_and_resumable(self):
        doc=self.new_doc('Alpha Beta'.ljust(4000)*2+'END')
        initial=dict(entities=[entity('Alpha',kind='Service',quote='Alpha Beta'),
                               entity('Beta',kind='Device',quote='Alpha Beta')],aliases=[],relations=[])
        def conflicting(payload):
            ref=[payload['sources'][0]['id']]*2
            return dict(entities=[],relations=[],aliases=[dict(alias='Alpha',entity='Beta',quote_ref=ref),
                                                         dict(alias='Alpha',entity='Beta',quote_ref=['missing','missing'])])
        first,_=self.run_job(doc,[initial,conflicting,TimeoutError('stop')])
        self.assertEqual(first['status'],'error');self.assertIn('stop',first['detail'])
        report=exclusion_tests.read_report(self.root,self.store,first['id'])
        self.assertTrue(report['available']);self.assertEqual(report['total_count'],2)
        self.assertEqual([(r['row'],r['code']) for r in report['items']],[(1,'alias_existing_entity'),(2,'reference')])
        self.assertEqual(read_report(self.root,self.store,first['id'])['deferred'],1)
        second,calls=self.run_job(doc,[EMPTY])
        self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([r['offset'] for r in calls],[8000])
        self.assertEqual(exclusion_tests.read_report(self.root,self.store,second['id'])['total_count'],2)
        self.assertEqual(self.store.document(doc['id'])['items'][0]['status'],'pending')

    def test_audit_write_failure_does_not_publish_candidate_or_advance_prefix(self):
        doc=self.new_doc('Alpha Beta'.ljust(4000)+'Alpha Beta')
        initial=dict(entities=[entity('Alpha',quote='Alpha Beta')],aliases=[],relations=[])
        def write(path,value):
            if Path(path).name=='dictionary-checkpoint.json' and value['offset']>4000:raise OSError('disk failure')
            atomic_json(path,value)
        with patch('verinote.dictionary_pipeline.atomic_json',side_effect=write):
            first,_=self.run_job(doc,[initial,self.generated])
        self.assertEqual(first['status'],'error');self.assertIn('확정하지 않았습니다',first['detail'])
        saved=json.loads(self.checkpoint(first).read_text());self.assertEqual(saved['offset'],4000)
        self.assertEqual(saved['dictionary'],initial);self.assertEqual(self.store.document(doc['id'])['items'],[])
        second,calls=self.run_job(doc,[self.generated]);self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([r['offset'] for r in calls],[4000]);self.assertEqual(read_report(self.root,self.store,second['id'])['applied'],1)

    def test_v2_prefix_reused_with_old_prompt_and_exclusions(self):
        doc=self.new_doc('Alpha Beta'.ljust(4000)+'Alpha Beta')
        initial=dict(entities=[entity('Alpha',quote='Alpha Beta')],aliases=[],relations=[])
        first,_=self.run_job(doc,[initial,TimeoutError('stop')])
        saved=json.loads(self.checkpoint(first).read_text());saved['schema_version']=2;saved.pop('reconciliations')
        saved['contract'].pop('reconciliation_policy');saved['contract'].update(filter_policy=3,prompt=PREVIOUS_IMMUTABLE_PROMPT)
        self.checkpoint(first).write_text(json.dumps(saved))
        second,calls=self.run_job(doc,[self.generated]);self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([r['offset'] for r in calls],[4000]);report=read_report(self.root,self.store,second['id'])
        self.assertEqual(report['legacy_chars'],4000);self.assertEqual(report['applied'],1)

    def test_43859_character_63_exclusion_legacy_prefix_resumes_at_chunk12(self):
        sizes=[3969]+[3989]*10
        prefix=''.join('Alpha Beta'.ljust(size-1)+'\n' for size in sizes)
        source=prefix+('Alpha Beta'.ljust(3988)+'\n')*2+'Alpha Beta'.ljust(110)
        self.assertEqual((len(prefix),len(source)),(43859,51947))
        def initial(count):
            def generate(payload):
                ref=[payload['sources'][0]['id']]*2
                return dict(entities=[dict(id='Alpha',name='Alpha',type='Concept',quote_ref=ref)]+
                            [dict(id=f'bad{i}',name='Absent',type='Concept',quote_ref=ref) for i in range(count)],aliases=[],relations=[])
            return generate
        doc=self.new_doc(source)
        first,_=self.run_job(doc,[initial(3),*[initial(6)]*10,TimeoutError('stop')])
        saved=json.loads(self.checkpoint(first).read_text())
        self.assertEqual(saved['offset'],43859)
        self.assertEqual(sum(r['direct']+r['dependent'] for r in saved['exclusions']['chunks']),63)
        saved['schema_version']=2;saved.pop('reconciliations');saved['contract'].pop('reconciliation_policy')
        saved['contract'].update(filter_policy=3,prompt=PREVIOUS_IMMUTABLE_PROMPT)
        self.checkpoint(first).write_text(json.dumps(saved))
        second,calls=self.run_job(doc,[self.generated,EMPTY,EMPTY])
        self.assertEqual(second['status'],'success',second['detail']);self.assertEqual(len(calls),3)
        self.assertEqual(calls[0]['offset'],43859)
        current=json.loads(self.checkpoint(second).read_text())
        self.assertEqual(current['offset'],51947)
        self.assertEqual(sum(r['direct']+r['dependent'] for r in current['exclusions']['chunks']),63)
        self.assertEqual(read_report(self.root,self.store,second['id'])['applied'],1)

    def test_corrupt_audit_cannot_resume_and_missing_is_unknown(self):
        doc=self.new_doc('Alpha Beta'.ljust(4000)+'END')
        first,_=self.run_job(doc,[self.generated,TimeoutError('stop')])
        saved=json.loads(self.checkpoint(first).read_text());saved['reconciliations']['items'][0]['after']['quote']='invented'
        self.checkpoint(first).write_text(json.dumps(saved))
        self.assertFalse(read_report(self.root,self.store,first['id'])['available'])
        second,calls=self.run_job(doc,[self.generated,EMPTY]);self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([r['offset'] for r in calls],[0,4000])

    def test_audit_counts_truncation_identity_and_escaping(self):
        names=[f'Alpha{i}' for i in range(MAX_DETAILS+1)];source=' '.join(names)+' Beta'
        def generated(payload):
            value=copy.deepcopy(EMPTY)
            value['entities']=[wire(entity(k,quote=source),source) for k in [*names,'Beta']]
            value['aliases']=[wire(dict(alias=k,entity='Beta'),source) for k in names]
            return value
        doc=self.new_doc(source);run,_=self.run_job(doc,[generated]);self.assertEqual(run['status'],'success',run['detail'])
        report=read_report(self.root,self.store,run['id']);self.assertEqual(report['applied'],len(names))
        self.assertEqual(report['truncated_count'],1);self.assertEqual(len(report['items']),MAX_DETAILS)
        saved=json.loads(self.checkpoint(run).read_text())['reconciliations']
        saved['identity']['version']=True
        with self.assertRaises(ValueError):validate_report(saved,run,doc,len(source))
        self.assertFalse(read_report(self.root,self.store,run['id'],document_id='other')['available'])
        js=Path('verinote/web/app.js').read_text();helper=js[js.index('function renderDictionaryReconciliations('):js.index('function renderDictionaryExclusions(')]
        script="const assert=require('node:assert/strict');const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');\n"+helper+"\n"+r'''
const html=renderDictionaryReconciliations({available:true,applied:1,deferred:1,total_count:2,items:[{chunk:1,key:'<img>',status:'applied',reason:'<script>',before:{name:'<img>'},after:{quote:'<script>'}}],truncated_count:1});
assert(!html.includes('<img>'));assert(!html.includes('<script>'));assert(html.includes('변경 전'));assert(html.includes('생략'));
assert(renderDictionaryReconciliations({available:false,reason:'미확인'}).includes('미확인'));
'''
        subprocess.run(['node','-e',script],check=True)
