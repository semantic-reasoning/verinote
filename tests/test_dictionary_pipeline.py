import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_workflow as workflow
from test_ontology import wire_dictionary
from test_workflow import DICTIONARY, SOURCE

from verinote.agents import CLI, CLI_PROVIDERS
from verinote.dictionary_pipeline import CHECKPOINT_LIMIT
from verinote.documents import bounded_spans, chunks
from verinote.ontology import (
    MAX_DICTIONARY_BYTES,
    dictionary_context,
    merge_dictionary,
    resolve_dictionary,
    source_spans,
    validate_dictionary,
)
from verinote.service import Service
from verinote.store import Conflict
from verinote.usage import atomic_json, job_calls

EMPTY = {'entities': [], 'aliases': [], 'relations': []}


class SpanContract(unittest.TestCase):
    def test_exact_partition_boundaries_and_limits(self):
        for source in ['가나다\r\n\tA. B\n\n' * 9000, '가' * 128000, 'x\n' * 64000]:
            source = source[:128000]
            parts = list(bounded_spans(source, 4000))
            self.assertEqual(''.join(body for _, body in parts), source)
            self.assertLessEqual(len(parts), 64)
            offset = 0
            for start, body in parts:
                self.assertEqual(start, offset); self.assertLessEqual(len(body), 4000)
                self.assertTrue(body); offset += len(body)
                spans = source_spans(body)
                self.assertEqual(''.join(s['text'] for s in spans), body)
                self.assertTrue(all(len(s['text']) <= 400 for s in spans))
        self.assertEqual([len(body) for _, body in chunks('x'*9000)], [8000, 1000])

    def test_refs_ground_exact_text_and_reject_foreign_or_malformed(self):
        source = '주문\r\n\t(ORDER)'+'. A\n'*250
        spans = source_spans(source)
        row = {'id':'order','name':'주문','type':'Service','quote_ref':[spans[0]['id'],spans[-1]['id']]}
        wire = copy.deepcopy(EMPTY);wire['entities']=[row]
        literal=resolve_dictionary(wire,spans)
        self.assertEqual(literal['entities'][0]['quote'],source)
        self.assertEqual(validate_dictionary(literal,source)['entities'][0]['quote'],source.strip())
        badrefs = [[True,1],[1,1],[],[spans[-1]['id'],spans[0]['id']],
                   [source_spans('other')[0]['id']]*2, ['missing']*2, 'bad']
        for ref in badrefs:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                resolve_dictionary({'entities':[row|{'quote_ref':ref}],'aliases':[],'relations':[]},spans)
        for fields in ({'quote':'주문'}, {'name':'not in source'}):
            with self.assertRaises(ValueError):resolve_dictionary({'entities':[row|fields],'aliases':[],'relations':[]},spans)
        with self.assertRaises(ValueError):resolve_dictionary(DICTIONARY,source_spans(SOURCE))
        aliases={'entities':[row],'aliases':[{'alias':'ORDER','entity':'order','quote_ref':[spans[-1]['id']]*2}],'relations':[]}
        with self.assertRaisesRegex(ValueError,'별칭 인용'):
            validate_dictionary(resolve_dictionary(aliases,spans),source)

    def test_compaction_preserves_semantics_and_reduces_bytes(self):
        source=' '.join(f'개체{i}' for i in range(40))+' '+('문맥. '*100)
        dictionary={'entities':[{'id':f'개체{i}','name':f'개체{i}','type':'Thing','quote':source} for i in range(40)],'aliases':[], 'relations':[]}
        compact=dictionary_context(dictionary)
        self.assertEqual(compact,{'entities':[{k:v for k,v in r.items() if k!='quote'} for r in dictionary['entities']], 'aliases':[],'relations':[]})
        def encode(value):
            return len(json.dumps(value,ensure_ascii=False).encode())
        self.assertLess(encode(compact),encode(dictionary)*.2)
        spans=source_spans(source)
        wire={'entities':[{k:v for k,v in row.items() if k!='quote'}|{'quote_ref':[spans[0]['id'],spans[-1]['id']]} for row in dictionary['entities']], 'aliases':[], 'relations':[]}
        self.assertLess(encode(wire),encode(dictionary)*.25)
        self.assertEqual(resolve_dictionary(wire,spans),dictionary)
        semantics=dictionary_context(DICTIONARY)
        self.assertEqual(semantics['relations'][0]['description'],DICTIONARY['relations'][0]['description'])
        self.assertEqual(semantics['relations'][0]['subject_types'],['Service'])
        with self.assertRaisesRegex(ValueError,'충돌'):
            merge_dictionary(DICTIONARY,{'entities':[DICTIONARY['entities'][0]|{'type':'Other'}],'aliases':[],'relations':[]},SOURCE,SOURCE)

    def test_dense_sixty_entity_document_retains_every_entity_within_byte_limit(self):
        sentences=[f'구성요소{i:02}는 데이터 수집과 처리 결과의 검증 및 전달 기능을 담당하는 독립적인 구성요소이다.' for i in range(60)]
        source=' '.join(sentences)
        spans=source_spans(source)
        wire=copy.deepcopy(EMPTY)
        for i in range(60):
            name=f'구성요소{i:02}'
            span=next(s for s in spans if name in s['text'])
            wire['entities'].append({'id':name,'name':name,'type':'Component','quote_ref':[span['id']]*2})
        literal=resolve_dictionary(wire,spans)
        validated=validate_dictionary(literal,source)
        self.assertEqual(len(validated['entities']),60)
        self.assertLess(len(json.dumps(validated,ensure_ascii=False).encode()),60000)
        self.assertTrue(all(row['quote'] in source for row in validated['entities']))
        # Dense line boundaries stay bounded without the former200-character minimum.
        self.assertLessEqual(len(source_spans('x\n'*2000)),125)

    def test_ontology_timeout_is_per_call_and_explicit_override_wins(self):
        cli=CLI()
        for agent in CLI_PROVIDERS:
            self.assertEqual(cli.effective_timeout(agent,'ontology'),600)
            self.assertEqual(cli.effective_timeout(agent),600 if agent in ('agy','copilot') else 180)
            for override in (0,.1,120):
                self.assertEqual(CLI(timeout=override).effective_timeout(agent,'ontology'),override)

    def test_ontology_deadline_logged_and_process_group_killed(self):
        cli=CLI()
        with tempfile.TemporaryDirectory() as tmp:
            for index, agent in enumerate(CLI_PROVIDERS):
                proc=Mock(pid=12345,returncode=-9);proc.poll.return_value=None
                with patch('verinote.agents.executable',return_value='/fake/cli'), patch('verinote.agents.subprocess.Popen',return_value=proc), patch('verinote.agents.time.monotonic',side_effect=[1000,1601]), patch('verinote.agents._kill_process_tree',side_effect=lambda process:process.wait()) as kill:
                    with self.assertRaisesRegex(TimeoutError,'600초'):
                        cli.complete(agent,'','synthetic',Path(tmp)/str(index),phase='ontology')
                kill.assert_called_once_with(proc);proc.wait.assert_called_once_with()
                meta=json.loads((Path(tmp)/str(index)/'run.json').read_text())
                self.assertEqual(meta['phase'],'ontology');self.assertEqual(meta['timeout_seconds'],600)
                self.assertEqual(cli.effective_timeout(agent),600 if agent in ('agy','copilot') else 180)


class Pipeline(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def new_doc(self, source):
        return self.store.register('synthetic','synthetic.md',source.encode(),source)

    def run_job(self, doc, outputs, agent='agy', model='', patcher=None):
        pending=iter(outputs);calls=[]
        class Fake:
            def complete(adapter,a,m,prompt,folder,*,phase=None):
                self.assertEqual(phase,'ontology')
                payload=json.loads(prompt.split('입력(JSON):\n',1)[1]);calls.append(payload)
                folder.mkdir(parents=True,exist_ok=True)
                atomic_json(folder/'run.json',{'status':'running'})
                output=next(pending)
                if isinstance(output,Exception):raise output
                return wire_dictionary(output,prompt)
        service=Service(self.store,self.root,Fake())
        job=service.start(doc['id'],agent,model,'ontology',doc['version'],doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(job['id']),calls

    def checkpoint(self, run):
        return self.root/'runs'/run['id']/'dictionary-checkpoint.json'

    def test_single_document_dictionary_with_repeated_evidence_exceeds_old_byte_limit(self):
        chunks=[]
        outputs=[]
        for group in range(2):
            names=[f'component-{group}-{index:02}' for index in range(10)]
            chunk=' '.join(names).ljust(4000,'x')
            chunks.append(chunk)
            outputs.append({'entities':[{'id':name,'name':name,'type':'Component','quote':chunk} for name in names],
                            'aliases':[],'relations':[]})
        doc=self.new_doc(''.join(chunks))
        result,calls=self.run_job(doc,outputs)
        self.assertEqual(result['status'],'success',result['detail'])
        item=next(row for row in self.store.document(doc['id'])['items'] if row['kind']=='ontology')
        size=len(json.dumps(item['payload'],ensure_ascii=False,separators=(',',':')).encode())
        self.assertGreater(size,60_000)
        self.assertLess(size,MAX_DICTIONARY_BYTES)
        self.assertEqual(len(item['payload']['entities']),20)
        self.assertEqual([call['offset'] for call in calls],[0,4000])
        self.assertGreater(CHECKPOINT_LIMIT,MAX_DICTIONARY_BYTES)
        self.store.review(item['id'],item['revision'],'approved','Reviewer','대용량 사전 확인')
        class Extractor:
            def complete(adapter,agent,model,prompt,folder,*,phase=None):
                return {'title':'Large dictionary extraction',
                        'claims':[{'heading':'Components','text':'The document lists components.',
                                   'quote':chunks[0][:100]}],
                        'facts':[],'coverage':[],'dictionary_gaps':[]}
        service=Service(self.store,self.root,Extractor())
        run=service.start(doc['id'],'agy','','extract',doc['version'],doc['generation'])
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success',self.store.run(run['id'])['detail'])

    def test_large_evidence_checkpoint_resumes_after_failure(self):
        first_chunk='가'*4000
        second_chunk='나'*4000
        dictionary={'entities':[{'id':f'component-{i:02}','name':'가','type':'Component','quote':first_chunk}
                                for i in range(12)],
                    'aliases':[],'relations':[]}
        doc=self.new_doc(first_chunk+second_chunk)
        first,calls=self.run_job(doc,[dictionary,TimeoutError('synthetic timeout')])
        checkpoint=self.checkpoint(first)
        self.assertEqual(first['status'],'error')
        self.assertEqual(json.loads(checkpoint.read_text(encoding='utf-8'))['offset'],4000)
        self.assertGreater(checkpoint.stat().st_size,128_000)
        second,calls=self.run_job(doc,[EMPTY])
        self.assertEqual(second['status'],'success',second['detail'])
        self.assertEqual([call['offset'] for call in calls],[4000])

    def test_timeout_resume_lineage_and_pending_finalization(self):
        doc=self.new_doc(SOURCE.ljust(4000)+('B'*4000)+('C'*20))
        first,calls=self.run_job(doc,[DICTIONARY,TimeoutError('CLI 실행 시간 제한 초과')])
        self.assertEqual(first['status'],'error');self.assertEqual(len(calls),2)
        self.assertIn('시간 제한',first['detail']);self.assertIn('구간 2/3',first['detail'])
        self.assertEqual(self.store.document(doc['id'])['items'],[])
        saved=json.loads(self.checkpoint(first).read_text());self.assertEqual(saved['offset'],4000)
        second,calls=self.run_job(doc,[EMPTY,TimeoutError('timeout')])
        self.assertEqual([c['offset'] for c in calls],[4000,8000])
        self.assertEqual(json.loads(self.checkpoint(second).read_text())['offset'],8000)
        third,calls=self.run_job(doc,[EMPTY])
        self.assertEqual(third['status'],'success',third['detail']);self.assertEqual([c['offset'] for c in calls],[8000])
        items=self.store.document(doc['id'])['items'];self.assertEqual(len(items),1);self.assertEqual(items[0]['status'],'pending')
        self.assertIn(second['id'],third['detail']);self.assertNotIn('quote',calls[0]['dictionary']['entities'][0])
        self.assertEqual(len(job_calls(self.root,'runs',first)),2)
        self.assertEqual(len(job_calls(self.root,'runs',second)),2)
        self.assertEqual(len(job_calls(self.root,'runs',third)),1)

    def test_complete_checkpoint_zero_calls_and_marker_loss_unknown(self):
        doc=self.new_doc(SOURCE)
        with patch.object(self.store,'finish_run',side_effect=OSError('commit failed')):
            first,calls=self.run_job(doc,[DICTIONARY])
        self.assertEqual(first['status'],'error')
        second,calls=self.run_job(doc,[])
        self.assertEqual(second['status'],'success');self.assertEqual(calls,[])
        self.assertEqual(job_calls(self.root,'runs',second),[])
        marker=self.root/'runs'/second['id']/'dictionary-progress.json';marker.unlink()
        self.assertFalse(job_calls(self.root,'runs',second)[0]['call_count_known'])

    def test_invalid_checkpoint_restarts_with_visible_reason(self):
        mutations=[lambda v:v.update(offset=True), lambda v:v.update(offset=3999),lambda v:v.update(offset=99999),
                   lambda v:v['contract'].update(pipeline='old'),lambda v:v['contract'].update(agent='other'),
                   lambda v:v['contract'].update(model='other'),lambda v:v['contract'].update(version=True),
                   lambda v:v['contract'].update(source_sha256='wrong'),lambda v:v.update(run_id='a'*32),
                   lambda v:v['dictionary']['entities'][0].update(quote='not in prefix')]
        for change in mutations:
            doc=self.new_doc(SOURCE.ljust(4000)+'END')
            first,_=self.run_job(doc,[DICTIONARY,TimeoutError('timeout')])
            path=self.checkpoint(first);value=json.loads(path.read_text());change(value);path.write_text(json.dumps(value))
            result,calls=self.run_job(doc,[DICTIONARY,EMPTY])
            self.assertEqual(result['status'],'success',result['detail']);self.assertEqual(calls[0]['offset'],0)
            self.assertIn('제외',result['detail'])

    def test_missing_corrupt_oversized_and_symlink_checkpoint(self):
        for mode in ('missing','corrupt','large','symlink'):
            doc=self.new_doc(SOURCE.ljust(4000)+'END')
            first,_=self.run_job(doc,[DICTIONARY,TimeoutError('timeout')]);path=self.checkpoint(first)
            if mode=='missing':path.unlink()
            elif mode=='corrupt':path.write_text('{')
            elif mode=='large':path.write_text(' '*(CHECKPOINT_LIMIT+1))
            else:
                if os.name=='nt':continue
                target=self.root/f'{mode}.json';target.write_bytes(path.read_bytes());path.unlink();path.symlink_to(target)
            result,calls=self.run_job(doc,[DICTIONARY,EMPTY])
            self.assertEqual(result['status'],'success');self.assertEqual(calls[0]['offset'],0)
            self.assertIn('제외',result['detail'])

    def test_checkpoint_write_failures_preserve_success(self):
        for fail_at in (1,2):
            doc=self.new_doc(SOURCE.ljust(4000)+'END');writes=0
            def write(path,value):
                nonlocal writes
                if Path(path).name=='dictionary-checkpoint.json':
                    writes+=1
                    if writes==fail_at:raise OSError('disk failure')  # noqa: B023 (intentional: same-iteration fail_at)
                atomic_json(path,value)
            with patch('verinote.dictionary_pipeline.atomic_json',side_effect=write):
                result,calls=self.run_job(doc,[DICTIONARY,EMPTY])
            self.assertEqual(result['status'],'success',result['detail']);self.assertIn('저장 실패',result['detail'])
            self.assertEqual(len(self.store.document(doc['id'])['items']),1)

    def test_invalid_delta_not_cached_and_no_retry(self):
        doc=self.new_doc(SOURCE.ljust(4000)+'END')
        bad=copy.deepcopy(DICTIONARY);bad['entities'][0]['quote']='invented'
        bad['entities'][0]['unexpected']=True  # Malformed rows stay fatal even with bad evidence.
        result,calls=self.run_job(doc,[DICTIONARY,bad])
        self.assertEqual(result['status'],'error');self.assertEqual(len(calls),2)
        self.assertEqual(json.loads(self.checkpoint(result).read_text())['offset'],4000)
        self.assertEqual(self.store.document(doc['id'])['items'],[])
        self.assertIn('quote_ref',result['detail'])

    def test_provider_model_version_and_deleted_owner_isolation(self):
        doc=self.new_doc(SOURCE.ljust(4000)+'END')
        first,_=self.run_job(doc,[DICTIONARY,TimeoutError('timeout')])
        for agent,model in [('claude',''),('agy','other')]:
            result,calls=self.run_job(doc,[TimeoutError('timeout')],agent,model)
            self.assertEqual(calls[0]['offset'],0)
        newer=self.store.register('new','new.md',doc['text'].encode(),doc['text'],doc['id'],1,1)
        result,calls=self.run_job(newer,[TimeoutError('timeout')])
        self.assertEqual(calls[0]['offset'],0)
        with self.assertRaises(Conflict):self.run_job(doc,[])
        # Even a physically retained checkpoint loses eligibility with its DB owner.
        with self.store.connect(True) as db:db.execute('DELETE FROM runs WHERE id=?',(first['id'],))
        self.assertTrue(self.checkpoint(first).exists())
        self.assertEqual(self.store.dictionary_retries(first),[])

    def test_restart_and_deletion_remove_checkpoint_owned_by_run(self):
        doc=self.new_doc(SOURCE.ljust(4000)+'END')
        first,_=self.run_job(doc,[DICTIONARY,TimeoutError('timeout')])
        self.store.run_status(first['id'],'running');self.store.recover()
        second,calls=self.run_job(doc,[EMPTY]);self.assertEqual(calls[0]['offset'],4000)
        service=Service(self.store,self.root)
        result=service.delete_document(doc['id'],False,{'expected_version':1,'expected_generation':1})
        service.pool.shutdown(wait=True)
        self.assertEqual(result['cleanup_pending'],0)
        self.assertFalse(self.checkpoint(first).parent.exists());self.assertFalse(self.checkpoint(second).parent.exists())

    def test_usage_covers_late_calls_and_marker_never_hides_calls(self):
        key='b'*32;owner={'id':key,'agent':'agy','model':'','stage':'ontology','status':'success'}
        for i in (1,17,64):
            folder=self.root/'runs'/key/str(i);folder.mkdir(parents=True)
            atomic_json(folder/'run.json',{'status':'error'})
        marker={'schema_version':1,'pipeline':'source-spans-v1','run_id':key,'status':'complete','calls_started':0,
                'resumed_from':'a'*32,'resumed_chars':100,'completed_chars':100,'total_chars':100}
        atomic_json(self.root/'runs'/key/'dictionary-progress.json',marker)
        self.assertEqual(len(job_calls(self.root,'runs',owner)),3)

    def test_many_actual_slices_and_inflight_new_version(self):
        doc=self.new_doc('x'*128000)
        result,calls=self.run_job(doc,[EMPTY]*32)
        self.assertEqual(result['status'],'success',result['detail'])
        self.assertEqual(len(calls),32);self.assertEqual(len(job_calls(self.root,'runs',result)),32)
        doc=self.new_doc(SOURCE)
        class Fake:
            def complete(adapter,a,m,prompt,folder,**kwargs):
                self.store.register('new','new.md',b'new','new',doc['id'],1,1)
                return wire_dictionary(DICTIONARY,prompt)
        service=Service(self.store,self.root,Fake())
        run=service.start(doc['id'],'agy','','ontology',1,1);service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        self.assertEqual(self.store.document(doc['id'])['items'],[])
        self.assertEqual(self.store.document(doc['id'],1)['items'][0]['status'],'pending')

    def test_zero_call_marker_write_failure_remains_unknown(self):
        doc=self.new_doc(SOURCE)
        with patch.object(self.store,'finish_run',side_effect=OSError('commit failed')):
            first,_=self.run_job(doc,[DICTIONARY])
        def write(path,value):
            if Path(path).name=='dictionary-progress.json':raise OSError('disk')
            atomic_json(path,value)
        with patch('verinote.dictionary_pipeline.atomic_json',side_effect=write):
            result,calls=self.run_job(doc,[])
        self.assertEqual(result['status'],'success');self.assertEqual(calls,[])
        self.assertIn('저장 실패',result['detail'])
        self.assertFalse(job_calls(self.root,'runs',result)[0]['call_count_known'])

    def test_identity_alias_reproduction_and_real_conflict_checkpoint(self):
        from test_ontology import DictionaryMergeTests
        sample=DictionaryMergeTests();dictionary=sample.dictionary()
        dictionary['aliases'] += [{'alias':'Conformance Region','entity':'Conformance Region','quote':q} for q in (sample.q1,sample.identity_only)]
        source=sample.q1+'\n'+sample.identity_only
        doc=self.new_doc(source)
        result,calls=self.run_job(doc,[dictionary])
        self.assertEqual(result['status'],'success',result['detail']);self.assertEqual(len(calls),1)
        item=self.store.document(doc['id'])['items'][0]
        self.assertEqual(item['status'],'pending')
        self.assertEqual([a['alias'] for a in item['payload']['aliases']],['CR'])
        doc=self.new_doc(source.ljust(4000)+sample.q1)
        conflict=sample.dictionary();conflict['entities'][0]['type']='Device'
        result,calls=self.run_job(doc,[dictionary,conflict])
        self.assertEqual(result['status'],'error');self.assertIn('기존 사전과 현재 구간 사이',result['detail'])
        self.assertEqual(json.loads(self.checkpoint(result).read_text())['offset'],4000)
        self.assertEqual(self.store.document(doc['id'])['items'],[])

    def test_old_merge_contract_restarts_visibly(self):
        doc=self.new_doc(SOURCE.ljust(4000)+'END')
        first,_=self.run_job(doc,[DICTIONARY,TimeoutError('timeout')])
        path=self.checkpoint(first);value=json.loads(path.read_text());value['contract']['merge_contract']=1
        path.write_text(json.dumps(value))
        result,calls=self.run_job(doc,[DICTIONARY,EMPTY])
        self.assertEqual(result['status'],'success');self.assertEqual(calls[0]['offset'],0)
        self.assertIn('제외',result['detail'])
