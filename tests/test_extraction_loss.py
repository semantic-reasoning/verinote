"""Synthetic loss-prevention contracts; these do not measure model semantic recall."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.extraction_loss import (
    RecoveryBudget,
    merge_recovery,
    new_audit,
    read_loss,
    strict_write,
)
from verinote.service import Service
from verinote.store import Conflict


class LossWorkflow(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def execute(self, outputs, doc=None, version=None):
        doc = doc or self.store.document(self.doc['id'])
        iterator = iter(outputs)
        calls=[]
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append((folder, prompt))
                value=next(iterator)
                if isinstance(value, Exception): raise value
                if callable(value): value=value(folder)
                return copy.deepcopy(value)
        service=Service(self.store,self.root,Fake())
        run=service.start(doc['id'],'copilot','model',expected_version=doc['version'],expected_generation=doc['generation'],extraction_contract=version)
        service.close()
        return self.store.run(run['id']),calls

    def test_default_recovers_omission_without_approval_and_keeps_v2_reextractable(self):
        approved_dictionary(self.store,self.doc['id'])
        old,calls=self.execute([extraction_response([])],version=2)
        self.assertEqual(old['status'],'success',old['detail']); self.assertEqual(len(calls),1)
        self.assertTrue(self.store.document(self.doc['id'])['can_extract'])
        run,calls=self.execute([extraction_response([]),{'facts':FACTS+[FACTS[0]]}])
        self.assertEqual(run['status'],'success',run['detail']); self.assertEqual(len(calls),2)
        report=read_loss(self.root,self.store,run)
        self.assertTrue(report['available']); self.assertEqual(report['recovered_fact_count'],2)
        self.assertEqual(report['recovery_completed'],1)
        self.assertEqual(self.store.snapshot()['facts'],[])
        self.assertFalse(self.store.document(self.doc['id'])['can_extract'])
        with self.assertRaises(Conflict): self.execute([])

    def test_long_rejected_evidence_preserved_without_inventing_types(self):
        source=SOURCE+' X 설명'*180
        doc=self.store.register('long','long.txt',source.encode(),source)
        dictionary=copy.deepcopy(DICTIONARY); dictionary['entities'].append({'id':'X','name':'X','type':'Other','quote':source})
        approved_dictionary(self.store,doc['id'],dictionary)
        bad=FACTS[0]|{'object':'X','quote':source}
        unknown=FACTS[0]|{'object':'unknown','quote':'fabricated'}
        run,_=self.execute([extraction_response([FACTS[0],bad,unknown],dictionary)],doc)
        self.assertEqual(run['status'],'success',run['detail'])
        report=read_loss(self.root,self.store,run)
        self.assertEqual((report['located_unresolved_count'],report['unverified_count']),(1,1))
        rows=report['chunks'][0]['unresolved']
        self.assertEqual(rows[0]['quote'],source); self.assertEqual(rows[0]['span'],[0,len(source)])
        self.assertEqual(rows[0]['reason'],'type_mismatch'); self.assertEqual(rows[0]['row'],1)
        self.assertEqual(self.store.snapshot()['facts'],[])

    def test_eleven_chunks_budget_preserves_unexamined_tail(self):
        source=SOURCE.ljust(8000)*10+SOURCE
        doc=self.store.register('eleven','eleven.txt',source.encode(),source)
        approved_dictionary(self.store,doc['id'])
        outputs=[extraction_response()]*3
        for _ in range(4): outputs += [extraction_response([]),{'facts':[]}]
        outputs += [extraction_response([])]*4
        run,calls=self.execute(outputs,doc)
        self.assertEqual(run['status'],'success',run['detail'])
        self.assertEqual(len(calls),15)
        report=read_loss(self.root,self.store,run)
        self.assertEqual(report['recovery_completed'],4); self.assertEqual(report['recovery_skipped'],4)
        self.assertEqual(report['zero_fact_chunks'],8)
        self.assertEqual([c['recovery_status'] for c in report['chunks'][7:]],['budget_exhausted']*4)
        self.assertTrue(all('all_no_evidence' in c['flags'] for c in report['chunks'][3:]))

    def test_failed_reserved_recovery_never_repeats_on_retry(self):
        approved_dictionary(self.store,self.doc['id'])
        first,calls=self.execute([extraction_response([]),TimeoutError('unknown outcome')])
        self.assertEqual(first['status'],'error'); self.assertEqual(len(calls),2)
        second,calls=self.execute([extraction_response([])])
        self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),1)
        self.assertEqual(read_loss(self.root,self.store,second)['chunks'][0]['recovery_status'],'previous_attempt')

    def test_missing_attempt_ledger_suppresses_recovery(self):
        approved_dictionary(self.store,self.doc['id'])
        first,_=self.execute([extraction_response([]),TimeoutError('stop')])
        (self.root/'runs'/first['id']/'recovery-attempts.json').unlink()
        second,calls=self.execute([extraction_response([])])
        self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),1)
        self.assertEqual(read_loss(self.root,self.store,second)['chunks'][0]['recovery_status'],'budget_unknown')

    def test_resume_requires_audit_and_preserves_completed_recovery(self):
        source=SOURCE.ljust(8000)+SOURCE
        doc=self.store.register('two','two.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
        first,_=self.execute([extraction_response([]),{'facts':[FACTS[0]]},TimeoutError('stop')],doc)
        self.assertEqual(first['status'],'error')
        second,calls=self.execute([extraction_response()],doc)
        self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),1)
        self.assertEqual(read_loss(self.root,self.store,second)['recovered_fact_count'],1)
        audit=self.root/'runs'/second['id']/'1/loss-audit.json'
        saved=json.loads(audit.read_text()); saved['sha256']='bad'; audit.write_text(json.dumps(saved))
        self.assertFalse(read_loss(self.root,self.store,second)['available'])

    def test_corrupt_audit_cannot_resume_accepted_prefix(self):
        source=SOURCE.ljust(8000)+SOURCE
        doc=self.store.register('two','two.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
        first,_=self.execute([extraction_response(),TimeoutError('stop')],doc)
        (self.root/'runs'/first['id']/'1/loss-audit.json').unlink()
        second,calls=self.execute([extraction_response(),extraction_response()],doc)
        self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),2)

    def test_mandatory_audit_write_failure_prevents_publication(self):
        approved_dictionary(self.store,self.doc['id'])
        with patch('verinote.extraction_loss.save_audit',side_effect=OSError('disk unavailable')):
            run,_=self.execute([extraction_response()])
        self.assertEqual(run['status'],'error')
        self.assertEqual([i['kind'] for i in self.store.document(self.doc['id'])['items']],['ontology'])

    def test_partial_recovery_keeps_invalid_rows_outside_graph(self):
        approved_dictionary(self.store,self.doc['id'])
        wrong=FACTS[0]|{'object':'unknown'}
        invented=FACTS[0]|{'quote':'invented'}
        run,_=self.execute([extraction_response([]),{'facts':[FACTS[0],wrong,invented]}])
        self.assertEqual(run['status'],'success',run['detail'])
        report=read_loss(self.root,self.store,run)
        self.assertEqual((report['recovered_fact_count'],report['located_unresolved_count'],report['unverified_count']),(1,1,1))
        self.assertEqual(self.store.snapshot()['facts'],[])

    def test_cancel_after_reservation_never_dispatches_recovery(self):
        approved_dictionary(self.store,self.doc['id'])
        original=RecoveryBudget.reserve
        def reserve(budget,index):
            result=original(budget,index)
            self.store.cancel_run(budget.run_id, 'running')
            return result
        with patch.object(RecoveryBudget,'reserve',reserve):
            run,calls=self.execute([extraction_response([])])
        self.assertEqual(run['status'],'cancelled'); self.assertEqual(len(calls),1)

    def test_v3_quote_repair_retains_unique_validation_without_false_unresolved(self):
        approved_dictionary(self.store,self.doc['id'])
        bad=extraction_response(); bad['facts'][0]['quote']='absent'
        run,calls=self.execute([bad,{'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}])
        self.assertEqual(run['status'],'success',run['detail']); self.assertEqual(len(calls),2)
        self.assertEqual(read_loss(self.root,self.store,run)['unverified_count'],0)

    def test_resumed_prefix_consumes_all_four_reservations(self):
        source=SOURCE.ljust(8000)*4+SOURCE
        doc=self.store.register('five','five.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
        outputs=[]
        for _ in range(4): outputs += [extraction_response([]),{'facts':[]}]
        first,_=self.execute(outputs+[TimeoutError('fifth')],doc)
        self.assertEqual(first['status'],'error')
        second,calls=self.execute([extraction_response([])],doc)
        self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),1)
        self.assertEqual(read_loss(self.root,self.store,second)['chunks'][-1]['recovery_status'],'budget_exhausted')

    def test_expired_initial_deadline_never_starts_recovery(self):
        import time
        approved_dictionary(self.store,self.doc['id'])
        real=time.monotonic
        def expired(folder):
            timer=patch('verinote.service.time.monotonic',side_effect=lambda:real()+1000)
            timer.start(); self.addCleanup(timer.stop)
            return extraction_response([])
        run,calls=self.execute([expired])
        self.assertEqual(run['status'],'error'); self.assertEqual(len(calls),1)


    def test_later_parallel_failure_blocks_new_recovery(self):
        import threading

        from verinote.copilot_extraction import CopilotExtractionSpec
        from verinote.extraction_scheduler import ChunkScheduler
        source=SOURCE.ljust(8000)+SOURCE
        doc=self.store.register('parallel','parallel.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
        release,failed=threading.Event(),threading.Event()
        calls=[]
        class Fake:
            extraction_spec=CopilotExtractionSpec('/synthetic/copilot','model',2,None,1024*1024,'null')
            def complete(self,agent,model,prompt,folder):
                calls.append(folder.name)
                if folder.name=='2':
                    if not release.wait(5): raise RuntimeError('test barrier')
                    raise ValueError('later chunk failed')
                return extraction_response([])
        original_fail=ChunkScheduler.fail
        def fail(scheduler,index,exc):
            result=original_fail(scheduler,index,exc)
            if index==2: failed.set()
            return result
        def audit(*args):
            release.set()
            if not failed.wait(5): raise RuntimeError('failure barrier')
            return new_audit(*args)
        fake=Fake(); service=Service(self.store,self.root,fake,extraction_caller=fake)
        with patch.object(ChunkScheduler,'fail',fail), patch('verinote.extraction_loss.new_audit',side_effect=audit):
            run=service.start(doc['id'],'copilot','model',expected_version=1,expected_generation=1,extraction_concurrency=2)
            service.close()
        saved=self.store.run(run['id'])
        self.assertEqual(saved['status'],'error'); self.assertNotIn('recovery',calls)
        attempts=json.loads((self.root/'runs'/run['id']/'recovery-attempts.json').read_text())
        self.assertEqual(attempts['attempts'],[])


    def test_same_triple_does_not_hide_a_rejected_original_row(self):
        approved_dictionary(self.store,self.doc['id'])
        raw=extraction_response([FACTS[0],FACTS[0]|{'quote':'fabricated'}])
        run,_=self.execute([raw,{'repairs':[{'path':'facts.1.quote','quote':''}]}])
        self.assertEqual(run['status'],'success',run['detail'])
        audit=read_loss(self.root,self.store,run)
        self.assertEqual(audit['unverified_count'],1)
        self.assertEqual(audit['chunks'][0]['unresolved'][0]['row'],1)
        self.assertEqual(audit['chunks'][0]['unresolved'][0]['quote'],'fabricated')

    def test_non_object_checkpoints_are_rejected_then_reextracted(self):
        for malformed in (None,1,'x',[]):
            with self.subTest(malformed=malformed):
                source=SOURCE.ljust(8000)+SOURCE
                doc=self.store.register('bad','bad.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
                first,_=self.execute([extraction_response(),TimeoutError('stop')],doc)
                path=self.root/'runs'/first['id']/'extraction-checkpoint.json'
                record=json.loads(path.read_text()); record['outputs']=[malformed]
                path.write_text(json.dumps(record))
                second,calls=self.execute([extraction_response(),extraction_response()],doc)
                self.assertEqual(second['status'],'success',second['detail']); self.assertEqual(len(calls),2)


    def test_timeout_only_retry_keeps_budget_or_suppresses_unknown_ledger(self):
        from verinote.extraction_checkpoint import ExtractionCheckpoint, reuse_contract
        for missing in (False,True):
            with self.subTest(missing=missing):
                source=SOURCE.ljust(8000)*4+SOURCE
                doc=self.store.register('http','http.txt',source.encode(),source); approved_dictionary(self.store,doc['id'])
                options={'adapter_contract_version':1,'max_tokens':256,'timeouts':{'default':180,'ontology':600}}
                first=self.store.create_run(doc['id'],'openrouter','model',expected_version=1,expected_generation=1,execution_options=options)
                first_checkpoint=ExtractionCheckpoint(self.store,self.root.resolve(),first,self.store.document(doc['id']))
                budget=RecoveryBudget(self.store,self.root.resolve(),first,first_checkpoint.contract)
                for index in range(1,5): self.assertEqual(budget.reserve(index),'reserved')
                self.store.run_status(first['id'],'error','interrupted')
                if missing: (self.root/'runs'/first['id']/'recovery-attempts.json').unlink()
                changed=options|{'timeouts':{'default':300,'ontology':600}}
                retry=self.store.create_run(doc['id'],'openrouter','model',expected_version=1,expected_generation=1,execution_options=changed)
                retry_checkpoint=ExtractionCheckpoint(self.store,self.root.resolve(),retry,self.store.document(doc['id']))
                self.assertEqual(reuse_contract(first_checkpoint.contract),reuse_contract(retry_checkpoint.contract))
                resumed=RecoveryBudget(self.store,self.root.resolve(),retry,retry_checkpoint.contract)
                self.assertEqual(resumed.reserve(5),'budget_unknown' if missing else 'budget_exhausted')
                if not missing: self.assertEqual(resumed.reserve(1),'previous_attempt')
                self.store.run_status(retry['id'],'error','test complete')



class LossValidation(unittest.TestCase):
    def test_malformed_and_overflow_recovery_fail(self):
        output=extraction_response([])
        for response in ({'facts':[],'other':1},{'facts':[FACTS[0]]*21},{'facts':[None]}):
            audit=new_audit(output,output,SOURCE,0,DICTIONARY)
            with self.assertRaises(ValueError): merge_recovery(audit,output,response,SOURCE,DICTIONARY)

    def test_ambiguous_quote_is_not_located(self):
        output=extraction_response([])
        audit=new_audit(output,output,SOURCE*2,0,DICTIONARY)
        result=merge_recovery(audit,output,{'facts':[FACTS[0]]},SOURCE*2,DICTIONARY)
        self.assertEqual(result['facts'],[]); self.assertIsNone(audit['unresolved'][0]['span'])

    def test_size_limit_refuses_before_writing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'audit.json'
            with self.assertRaises(ValueError): strict_write(target,{'huge':'a'*100},20)
            self.assertFalse(target.exists())

class LossUIAndUsage(unittest.TestCase):
    def test_ui_escapes_full_evidence_and_distinguishes_skips(self):
        import subprocess
        app=Path('verinote/web/app.js').read_text()
        function=app[app.index('function renderLossAudit('):]
        script="const esc=x=>String(x).replaceAll('<','&lt;');\n"+function+"\nconsole.log(renderLossAudit("+json.dumps({'available':True,'processed_chunks':1,'total_chunks':1,'initial_fact_count':1,'recovered_fact_count':0,'located_unresolved_count':1,'unverified_count':0,'zero_fact_chunks':1,'recovery_completed':0,'recovery_skipped':1,'chunks':[{'recovery_status':'budget_exhausted','retained_fact_count':0,'flags':['zero_retained'],'unresolved':[{'subject':'<script>','predicate':'r','object':'b','reason':'type_mismatch','origin':'initial','row':0,'span':[0,500],'quote':'<img>'+'a'*400}]}]})+"));"
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout
        self.assertNotIn('<script>',result); self.assertIn('&lt;img>',result)
        self.assertIn('a'*400,result); self.assertIn('4회 한도로 미점검',result)

    def test_recovery_usage_is_discovered_once(self):
        import tempfile

        from verinote.usage import atomic_json, empty_usage, job_calls
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); key='a'*32
            target=root/'runs'/key/'1/recovery'; target.mkdir(parents=True)
            usage=empty_usage('copilot','model'); usage.update(status='success',reported_credits=2)
            atomic_json(target/'usage.json',usage)
            calls=job_calls(root,'runs',{'id':key,'agent':'copilot','stage':'extract','model':'model'})
            self.assertEqual(len(calls),1); self.assertEqual(calls[0]['stage'],'extraction_recovery')
            self.assertEqual(calls[0]['reported_credits'],2)
