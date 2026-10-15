import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_workflow import DICTIONARY, FACTS, PAGE, SOURCE, approved_dictionary, legacy_run
from verinote.ontology import (MAX_FACTS, bounded_prompt, dictionary_fact,
                               merge_dictionary, validate_dictionary)
from verinote.service import Service
from verinote.engine import query
from verinote.store import Conflict, SCHEMA, Store, canonical, digest

ARCH = '주문 서비스(order)는 결제 서비스(pay)를 호출한다. 결제 서비스(pay)는 원장 서비스(ledger)를 호출한다. 결제 서비스는 결제팀이 운영한다. 문서 ARCH 담당은 문서팀이다.'
ARCH_DICTIONARY = {
    'entities': [{'id': key, 'name': name, 'type': kind, 'quote': ARCH} for key, name, kind in
                 [('order','주문 서비스','Service'),('pay','결제 서비스','Service'),('ledger','원장 서비스','Service'),('결제팀','결제팀','Team'),('ARCH','ARCH','Document')]],
    'aliases': [{'alias':'결제 서비스','entity':'pay','quote':ARCH}],
    'relations': [
        {'id':'depends_on','description':'내부 서비스가 직접 호출하는 서비스에 의존한다. 간접 경로, DB 접속과 외부 시스템은 제외.', 'subject_types':['Service'],'object_types':['Service'],'quote':ARCH},
        {'id':'owned_by','description':'서비스의 운영 책임 팀. 문서 담당은 제외.', 'subject_types':['Service'],'object_types':['Team'],'quote':ARCH},
    ],
}


def wire_dictionary(dictionary, prompt):
    """Fixture adapter: encode valid literal test quotes as actual source references."""
    spans = json.loads(prompt.split('입력(JSON):\n', 1)[1])['sources']
    result = copy.deepcopy(dictionary)
    for rows in result.values():
        for row in rows:
            quote = row.pop('quote')
            match = next(([spans[a]['id'], spans[b]['id']] for a in range(len(spans)) for b in range(a, len(spans))
                          if quote in ''.join(s['text'] for s in spans[a:b+1])), None)
            row['quote_ref'] = match or ['invalid', 'invalid']
    return result


class DictionaryContract(unittest.TestCase):
    def test_alias_canonicalization_and_signatures(self):
        dictionary = validate_dictionary(ARCH_DICTIONARY, ARCH)
        fact = {'subject':'order','predicate':'depends_on','object':'결제 서비스','quote':ARCH}
        self.assertEqual(dictionary_fact(fact, ARCH, dictionary)['object'], 'pay')
        for fields in ({'subject':'unknown'}, {'predicate':'calls'}, {'subject':'ARCH','predicate':'owned_by','object':'결제팀'}, {'subject':'결제팀','predicate':'owned_by','object':'pay'}, {'quote':'invented'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                dictionary_fact(fact | fields, ARCH, dictionary)

    def test_redundant_self_alias_is_omitted_but_still_grounded(self):
        d=copy.deepcopy(ARCH_DICTIONARY)
        d['aliases'].append({'alias':'pay','entity':'pay','quote':ARCH})
        self.assertEqual(validate_dictionary(d,ARCH),validate_dictionary(ARCH_DICTIONARY,ARCH))
        d['aliases'][-1]['quote']='invented'
        with self.assertRaises(ValueError):validate_dictionary(d,ARCH)

    def test_dictionary_rejects_ungrounded_ambiguous_and_invalid_types(self):
        for mutate in (
            lambda d: d['entities'].append(d['entities'][0]),
            lambda d: d['entities'][0].update(name='invented'),
            lambda d: d['aliases'].append({'alias':'order','entity':'pay','quote':ARCH}),
            lambda d: d['aliases'].append(d['aliases'][0] | {'entity':'order'}),
            lambda d: d['aliases'][0].update(quote='문서 ARCH 담당은 문서팀이다.'),
            lambda d: d['relations'][0].update(subject_types=['Unknown']),
            lambda d: d['relations'][0].update(id='bad predicate'),
            lambda d: d['relations'][0].update(quote='invented'),
        ):
            d=copy.deepcopy(ARCH_DICTIONARY);mutate(d)
            with self.assertRaises(ValueError):validate_dictionary(d,ARCH)

    def test_incremental_dictionary_and_conflicts(self):
        empty={'entities':[],'aliases':[],'relations':[]}
        first=merge_dictionary(empty,ARCH_DICTIONARY,ARCH,ARCH)
        self.assertEqual(merge_dictionary(first,first,ARCH,ARCH),first)
        delta={'entities':[{'id':'batch','name':'배치','type':'Job','quote':'배치 작업이다.'}], 'aliases':[], 'relations':[]}
        merged=merge_dictionary(first,delta,ARCH+'배치 작업이다.','배치 작업이다.')
        self.assertEqual(len(merged['entities']),6)
        for key, field, value in [('entities','type','Other'),('relations','description','다른 의미'),('aliases','entity','order')]:
            delta=copy.deepcopy(first);delta[key][0][field]=value
            with self.assertRaisesRegex(ValueError,'충돌'):merge_dictionary(first,delta,ARCH,ARCH)
        with self.assertRaisesRegex(ValueError,'원문'):
            merge_dictionary(empty,ARCH_DICTIONARY,ARCH,'다른 청크')

    def test_domain_states_and_bounds(self):
        states=['활성 상태','사용 중 상태','냉각 상태','만료 상태','철회 상태']
        source='권한 FSM은 '+', '.join(states)+'를 포함한다.'
        d={'entities':[{'id':'권한 FSM','name':'권한 FSM','type':'FSM','quote':source}]+[{'id':'권한 FSM:'+s,'name':s,'type':'State','quote':source} for s in states], 'aliases':[], 'relations':[{'id':'includes_state','description':'FSM의 명시된 상태. 전이 조건은 제외.', 'subject_types':['FSM'],'object_types':['State'],'quote':source}]}
        d=validate_dictionary(d,source)
        for state in states:
            self.assertEqual(dictionary_fact({'subject':'권한 FSM','predicate':'includes_state','object':'권한 FSM:'+state,'quote':source},source,d)['object'],'권한 FSM:'+state)
        large=copy.deepcopy(d);large['entities']*=40
        with self.assertRaises(ValueError):validate_dictionary(large,source)
        with self.assertRaisesRegex(ValueError,'바이트'):bounded_prompt('',{'text':'가'*50000})
        large={'entities':[{'id':str(i),'name':'가','type':'T','quote':'가'*8000} for i in range(4)],'aliases':[],'relations':[]}
        with self.assertRaisesRegex(ValueError,'바이트'):validate_dictionary(large,'가'*8000)


class OntologyWorkflow(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def start(self, stage='extract'):
        d=self.store.document(self.doc['id'])
        return self.store.create_run(d['id'],'agy','test',stage,d['version'],d['generation'])

    def draft(self):
        run=self.start('ontology');self.store.run_status(run['id'],'running')
        self.store.finish_run(run['id'],dictionary=DICTIONARY)
        return next(i for i in self.store.document(self.doc['id'])['items'] if i['kind']=='ontology')

    def test_gate_edit_approval_pin_and_retry(self):
        with self.assertRaises(Conflict):self.start()
        item=self.draft()
        for status in ('pending','held','rejected'):
            if status!='pending':
                self.store.review(item['id'],item['revision'],status,'R','test')
                item=next(i for i in self.store.document(self.doc['id'])['items'] if i['kind']=='ontology')
            with self.assertRaises(Conflict):self.start()
        self.store.review(item['id'],item['revision'],'approved','R','checked')
        run=self.start();pin=run['ontology']
        self.assertEqual(pin['sha256'],digest(DICTIONARY))
        self.assertEqual(pin['payload'],DICTIONARY)
        self.store.run_status(run['id'],'error')
        retry=self.start();self.store.run_status(retry['id'],'running')
        self.store.finish_run(retry['id'],PAGE,FACTS)
        self.assertEqual(self.store.snapshot()['facts'],[])
        with self.assertRaises(Conflict):self.start()
        self.assertFalse(self.store.document(self.doc['id'])['can_extract'])
        self.store.review(item['id'],pin['revision'],'revise','R','edit',DICTIONARY)
        with self.assertRaises(Conflict):self.start()
        with self.assertRaises(Conflict):self.store.review(item['id'],pin['revision'],'approved','R','stale')
        self.store.review(item['id'],pin['revision']+1,'approved','R','new approval')
        newer=self.start()
        self.assertEqual(newer['ontology']['revision'],pin['revision']+2)
        self.assertEqual(self.store.run(run['id'])['ontology'],pin)

    def test_store_rejects_bad_fact_and_changed_dictionary_atomically(self):
        for change in ('bad_fact','revise','pending'):
            with self.subTest(change=change):
                item=approved_dictionary(self.store,self.doc['id'])
                run=self.start();self.store.run_status(run['id'],'running')
                if change!='bad_fact':
                    self.store.review(item['id'],item['revision'],change,'R','change',DICTIONARY if change=='revise' else None)
                with self.assertRaises(ValueError):
                    self.store.finish_run(run['id'],PAGE,[FACTS[0] | {'predicate':'calls'}] if change=='bad_fact' else FACTS)
                self.assertEqual([i['kind'] for i in self.store.document(self.doc['id'])['items']],['ontology'])
                self.store.run_status(run['id'],'error')
                self.store.delete_document(self.doc['id'],False,1,1)
                self.doc=self.store.register('운영','test.md',SOURCE.encode(),SOURCE)

    def test_existing_results_preserved_and_fact_review_constrained(self):
        legacy=legacy_run(self.store,self.doc['id']);self.store.run_status(legacy['id'],'running');self.store.finish_run(legacy['id'],PAGE,FACTS)
        old=self.store.document(self.doc['id'])['items']
        for item in old:self.store.review(item['id'],1,'approved','R','old checked')
        item=approved_dictionary(self.store,self.doc['id'])
        run=self.start();self.store.run_status(run['id'],'running');self.store.finish_run(run['id'],PAGE,FACTS)
        new=next(i for i in self.store.document(self.doc['id'])['items'] if i['kind']=='fact' and i['run_id']==run['id'])
        with self.assertRaisesRegex(ValueError,'사전에 없는 관계'):
            self.store.review(new['id'],1,'revise','R','invalid',new['payload']|{'predicate':'calls'})
        self.store.review(new['id'],1,'approved','R','checked')
        self.store.review(item['id'],item['revision'],'pending','R','withdraw dictionary')
        self.assertEqual(len(self.store.snapshot()['facts']),3)
        self.assertEqual(new['dictionary']['revision'],2)
        self.store.review(item['id'],3,'approved','R','approve again')
        newer=self.start();self.store.run_status(newer['id'],'running');self.store.finish_run(newer['id'],PAGE,FACTS)
        self.assertEqual(len(self.store.snapshot()['facts']),3)

    def test_version_tokens_restart_and_dictionary_isolation(self):
        item=approved_dictionary(self.store,self.doc['id'])
        run=self.start()
        self.store.recover()
        self.assertEqual(self.store.run(run['id'])['status'],'interrupted')
        retry=self.start();self.store.run_status(retry['id'],'error')
        newer=self.store.register('new','n.md',SOURCE.encode(),SOURCE,self.doc['id'],1,1)
        for stage in ('ontology','extract'):
            with self.assertRaises(Conflict):self.store.create_run(self.doc['id'],'agy','',stage,1,1)
        with self.assertRaises(Conflict):self.start()
        self.assertEqual(self.store.document(self.doc['id'],1)['items'][0]['id'],item['id'])
        self.store.delete_document(self.doc['id'],True,newer['version'],newer['generation'])
        self.assertTrue(self.store.document(self.doc['id'])['can_extract'])

    def test_concurrent_submission_and_revocation_in_flight(self):
        item=approved_dictionary(self.store,self.doc['id'])
        entered,release=threading.Event(),threading.Event()
        class Fake:
            def complete(self,*args,**kwargs):
                entered.set();release.wait(5);return PAGE|{'facts':FACTS}
        service=Service(self.store,self.root,Fake())
        run=service.start(self.doc['id'],'agy','',expected_version=1,expected_generation=1)
        self.assertTrue(entered.wait(3))
        with self.assertRaises(Conflict):self.start()
        self.store.review(item['id'],2,'pending','R','withdraw')
        release.set();service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'error')
        self.assertEqual(len(self.store.document(self.doc['id'])['items']),1)

    def test_real_orchestration_two_stages_and_fact_limit(self):
        prompts=[]
        class Fake:
            def complete(self,agent,model,prompt,folder,**kwargs):
                prompts.append(prompt)
                return wire_dictionary(DICTIONARY, prompt) if '기존 사전은 문맥' in prompt else PAGE|{'facts':[FACTS[0]]*MAX_FACTS}
        service=Service(self.store,self.root,Fake())
        run=service.start(self.doc['id'],'agy','',stage='ontology',expected_version=1,expected_generation=1)
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        item=self.store.document(self.doc['id'])['items'][0]
        self.assertEqual(item['status'],'pending');self.assertEqual(item['kind'],'ontology')
        self.store.review(item['id'],1,'approved','R','checked')
        service=Service(self.store,self.root,Fake())
        run=service.start(self.doc['id'],'agy','',expected_version=1,expected_generation=1)
        service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        self.assertIn('approved_dictionary',prompts[-1]);self.assertIn(digest(DICTIONARY),prompts[-1])
        self.assertEqual(len([i for i in self.store.document(self.doc['id'])['items'] if i['kind']=='fact']),1)
        self.store.review(item['id'],2,'pending','R','retry');self.store.review(item['id'],3,'approved','R','retry')
        with patch.object(Fake,'complete',return_value=PAGE|{'facts':[FACTS[0]]*(MAX_FACTS+1)}):
            service=Service(self.store,self.root,Fake());run=service.start(self.doc['id'],'agy','',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'error')

    def test_multichunk_dictionary_failure_leaves_no_partial_candidate(self):
        source=SOURCE.ljust(4000)+SOURCE
        doc=self.store.register('long','long.md',source.encode(),source)
        class Fake:
            calls=0
            def complete(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1:return wire_dictionary(DICTIONARY, args[2])
                return wire_dictionary({'entities':[DICTIONARY['entities'][0]|{'type':'Other'}],'aliases':[],'relations':[]}, args[2])
        service=Service(self.store,self.root,Fake());run=service.start(doc['id'],'agy','',stage='ontology',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'error')
        self.assertIn('충돌',self.store.run(run['id'])['detail'])
        self.assertEqual(self.store.document(doc['id'])['items'],[])

    def test_more_than_twenty_explicit_states_are_saved(self):
        states=[f'상태{i:02}' for i in range(25)]
        source='제어 FSM의 상태는 '+', '.join(states)+'이다.'
        doc=self.store.register('상태 정의','states.md',source.encode(),source)
        dictionary={'entities':[{'id':'제어 FSM','name':'제어 FSM','type':'FSM','quote':source}]+[{'id':s,'name':s,'type':'State','quote':source} for s in states], 'aliases':[], 'relations':[{'id':'includes_state','description':'명시된 상태 구성','subject_types':['FSM'],'object_types':['State'],'quote':source}]}
        approved_dictionary(self.store,doc['id'],dictionary)
        output={'title':'상태','claims':[{'heading':'구성','text':source,'quote':source}], 'facts':[{'subject':'제어 FSM','predicate':'includes_state','object':s,'quote':source} for s in states]}
        class Fake:
            def complete(self,*args,**kwargs):return output
        service=Service(self.store,self.root,Fake());run=service.start(doc['id'],'agy','',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        self.assertCountEqual([i['payload']['object'] for i in self.store.document(doc['id'])['items'] if i['kind']=='fact'],states)

    def test_approved_canonical_entities_connect_in_wirelog(self):
        doc=self.store.register('구조','arch.md',ARCH.encode(),ARCH)
        approved_dictionary(self.store,doc['id'],ARCH_DICTIONARY)
        output={'title':'구조','claims':[{'heading':'호출','text':'주문에서 결제, 결제에서 원장으로 호출한다.','quote':ARCH}], 'facts':[{'subject':s,'predicate':'depends_on','object':o,'quote':ARCH} for s,o in [('order','결제 서비스'),('pay','ledger')]]}
        class Fake:
            def complete(self,*args,**kwargs):return output
        service=Service(self.store,self.root,Fake());run=service.start(doc['id'],'agy','',expected_version=1,expected_generation=1);service.pool.shutdown(wait=True)
        self.assertEqual(self.store.run(run['id'])['status'],'success')
        self.assertEqual(query(self.store.snapshot())['rows'],[])
        for item in self.store.document(doc['id'])['items']:
            if item['kind']=='fact':self.store.review(item['id'],1,'approved','R','확인')
        self.assertEqual(query(self.store.snapshot(),'path','order','depends_on','ledger')['rows'],[['order','depends_on','ledger']])

    def test_dictionary_and_pinned_runs_are_deleted_with_version(self):
        approved_dictionary(self.store,self.doc['id'])
        run=self.start();self.store.run_status(run['id'],'running');self.store.finish_run(run['id'],PAGE,FACTS)
        service=Service(self.store,self.root)
        for row in self.store.runs():
            folder=self.root/'runs'/row['id'];folder.mkdir(parents=True);(folder/'prompt.txt').write_text(SOURCE)
        result=service.delete_document(self.doc['id'],False,{'expected_version':1,'expected_generation':1})
        service.pool.shutdown()
        self.assertEqual(result['cleanup_status'],'complete')
        self.assertEqual(self.store.runs(),[])
        with self.store.connect() as db:
            for table in ('items','events','versions'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)

    def test_migration_preserves_legacy_and_backup_restore(self):
        path=self.root/'old.sqlite3';backup=self.root/'backup.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO documents VALUES('d','old',1)")
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)',('d',1,'old.md',SOURCE,SOURCE.encode(),'hash','date'))
            db.execute("INSERT INTO runs VALUES('r','d',1,'agy','','success','','date')")
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)',('f','d',1,'fact',canonical(FACTS[0]),'approved',2,'r'))
            db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)',('f',2,'approved','R','old',canonical(FACTS[0]),'date'))
            db.execute("INSERT INTO snapshots VALUES('s','{}','date')")
        with closing(sqlite3.connect(path)) as original, closing(sqlite3.connect(backup)) as saved:original.backup(saved)
        for _ in range(2):
            store=Store(path)
            self.assertEqual(store.run('r')['stage'],'legacy');self.assertIsNone(store.run('r')['ontology'])
            self.assertEqual(store.document('d')['items'][0]['payload'],FACTS[0]);self.assertEqual(store.history('f')[0]['actor'],'R')
            self.assertEqual(store.snapshot('s'),{})
        approved_dictionary(store,'d')
        run=store.create_run('d','agy','','extract',1,1);store.run_status(run['id'],'running');store.finish_run(run['id'],PAGE,FACTS)
        self.assertEqual(len(store.snapshot()['facts']),1)
        # A pre-upgrade backup really remains readable with the old column contract.
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(runs)').fetchall()),8)
            self.assertEqual(db.execute('SELECT status FROM items').fetchone()[0],'approved')
            # Resume a partially upgraded fixture: each additive step is idempotent.
            db.execute("ALTER TABLE runs ADD COLUMN stage TEXT NOT NULL DEFAULT 'legacy'")
            db.commit()
        resumed=Store(backup)
        self.assertEqual(resumed.run('r')['stage'],'legacy')
        self.assertIsNone(resumed.run('r')['ontology'])


class OntologyHTTP(unittest.TestCase):
    setUp=workflow.HTTP.setUp
    tearDown=workflow.HTTP.tearDown
    request=workflow.HTTP.request

    def test_two_stage_api_and_stale_version(self):
        doc=self.request('/api/documents','POST',{'title':'test','filename':'x.md','text':SOURCE})
        base=f'/api/documents/{doc["id"]}'
        args={'agent':'agy','expected_version':1,'expected_generation':1}
        for body,status in [({'agent':'agy'},400),(args,409)]:
            with self.assertRaises(HTTPError) as err:self.request(base+'/extract','POST',body)
            self.assertEqual(err.exception.code,status);err.exception.close()
        with patch.object(self.service.cli,'complete',side_effect=lambda a,m,p,f,**kw:wire_dictionary(DICTIONARY,p)):
            run=self.request(base+'/ontology','POST',args)
            self.service.pool.submit(lambda:None).result(timeout=3)
        item=self.request(base)['items'][0]
        self.request(f'/api/items/{item["id"]}/review','POST',{'revision':1,'action':'revise','actor':'R','reason':'edit','payload':DICTIONARY})
        self.request(f'/api/items/{item["id"]}/review','POST',{'revision':2,'action':'approved','actor':'R','reason':'checked'})
        with patch.object(self.service.cli,'complete',return_value=PAGE|{'facts':FACTS}):
            run=self.request(base+'/extract','POST',args)
            self.service.pool.submit(lambda:None).result(timeout=3)
        self.assertEqual(self.request('/api/runs/'+run['id'])['ontology']['revision'],3)
        self.assertFalse(self.request(base)['can_extract'])
        self.request('/api/documents','POST',{'title':'test','filename':'x.md','text':SOURCE,'document_id':doc['id'],'expected_version':1,'expected_generation':1})
        with self.assertRaises(HTTPError) as err:self.request(base+'/ontology','POST',args)
        self.assertEqual(err.exception.code,409);err.exception.close()


class DictionaryMergeTests(unittest.TestCase):
    q1 = '센서 Sensor는 궤적 일치 영역(Conformance Region, CR)을 사용한다.'
    q2 = '센서 Sensor는 궤적 일치 영역(Conformance Region, CR)을 다시 확인한다.'
    identity_only = 'Conformance Region은 다른 절에서도 사용된다.'

    def dictionary(self, quote=None):
        quote = self.q1 if quote is None else quote
        return {'entities': [
            {'id':'Conformance Region','name':'궤적 일치 영역','type':'Region','quote':quote},
            {'id':'Sensor','name':'센서','type':'Device','quote':quote}],
            'aliases':[{'alias':'CR','entity':'Conformance Region','quote':quote}],
            'relations':[{'id':'uses','description':'센서가 영역을 사용한다.','subject_types':['Device'], 'object_types':['Region'],'quote':quote}]}

    def test_repeated_self_aliases_with_id_only_evidence(self):
        d=self.dictionary();d['aliases'] += [
            {'alias':'Conformance Region','entity':'Conformance Region','quote':q} for q in (self.q1,self.identity_only)]
        source=self.q1+'\n'+self.identity_only
        result=merge_dictionary({'entities':[],'aliases':[],'relations':[]},d,source,source)
        self.assertEqual(result,self.dictionary())
        self.assertEqual(validate_dictionary(d,source),result)
        for fields in ({'entity':'missing'}, {'quote':'invented'}, {'quote':'센서'}, {'alias':None}):
            bad=copy.deepcopy(d);bad['aliases'][-1].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                merge_dictionary({'entities':[],'aliases':[],'relations':[]},bad,source,source)
        real_alias={'alias':'CR','entity':'Conformance Region','quote':'CR은 별칭이다.'}
        d=self.dictionary();d['aliases']=[real_alias]
        with self.assertRaisesRegex(ValueError,'name'):
            validate_dictionary(d,self.q1+'CR은 별칭이다.')

    def test_all_kinds_deduplicate_with_first_valid_evidence(self):
        first,second=self.dictionary(),self.dictionary(self.q2)
        source=self.q1+'\n'+self.q2
        before=copy.deepcopy((first,second))
        self.assertEqual(merge_dictionary(first,second,source,self.q2),first)
        combined={k:first[k]+second[k] for k in first}
        self.assertEqual(merge_dictionary({'entities':[],'aliases':[],'relations':[]},combined,source,source),first)
        self.assertEqual((first,second),before)

    def test_duplicate_validation_cannot_hide_invalid_rows(self):
        first=self.dictionary();source=self.q1+'\n'+self.q2+'\nCR은 약어다.\nConformance Region.'
        for kind in first:
            bad=self.dictionary(self.q2);bad[kind][0]['quote']='invented'
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'원문'):
                merge_dictionary(first,bad,source,self.q2)
        bad=self.dictionary(self.q2);bad['entities'][0]['quote']='Conformance Region.'
        with self.assertRaisesRegex(ValueError,'name'):merge_dictionary(first,bad,source,source)
        bad=self.dictionary(self.q2);bad['aliases'][0]['quote']='CR은 약어다.'
        with self.assertRaisesRegex(ValueError,'name'):merge_dictionary(first,bad,source,source)
        bad=self.dictionary(self.q2);bad['relations'][0]['subject_types']=['missing']
        with self.assertRaisesRegex(ValueError,'타입') as error:merge_dictionary(first,bad,source,source)
        self.assertIn('uses',str(error.exception));self.assertIn('missing',str(error.exception));self.assertIn('subject_types',str(error.exception))

    def test_normalization_is_schema_only(self):
        d=self.dictionary();d['relations'][0]['subject_types']=['Device','Region']
        delta=copy.deepcopy(d)
        delta['entities'][0]['id']=' Conformance Region '
        delta['relations'][0]['subject_types']=['Region','Device','Device']
        delta['relations'][0]['description']=' '+delta['relations'][0]['description']+' '
        self.assertEqual(merge_dictionary(d,delta,self.q1,self.q1),validate_dictionary(d,self.q1))
        delta['relations'][0]['subject_types']=[' Device','Region']
        with self.assertRaises(ValueError):merge_dictionary(d,delta,self.q1,self.q1)

    def test_real_conflicts_identify_origin_and_fields_without_mutation(self):
        first=self.dictionary()
        changes=[('entities','name','영역'),('entities','type','Device'),('aliases','entity','Sensor'),
                 ('relations','description','다른 의미'),('relations','subject_types',['Region']),('relations','object_types',['Device'])]
        for kind,field,value in changes:
            delta=copy.deepcopy(first);delta[kind][0][field]=value
            before=copy.deepcopy((first,delta))
            with self.subTest(kind=kind,field=field),self.assertRaisesRegex(ValueError,'기존 사전과 현재 구간 사이') as error:
                merge_dictionary(first,delta,self.q1,self.q1)
            self.assertIn(field,str(error.exception));self.assertEqual((first,delta),before)
            same={k:first[k]+delta[k] for k in first}
            with self.assertRaisesRegex(ValueError,'이번 응답 내부') as error:
                merge_dictionary({'entities':[],'aliases':[],'relations':[]},same,self.q1,self.q1)
            self.assertNotIn('청크 간',str(error.exception))

    def test_old_quote_exception_only_for_exact_initial_rows(self):
        first=self.dictionary();source=self.q1+'\n'+self.q2+'\n새 구간'
        self.assertEqual(merge_dictionary(first,first,source,'새 구간'),first)
        with self.assertRaisesRegex(ValueError,'원문'):
            merge_dictionary(first,self.dictionary(self.q2),source,'새 구간')
        with self.assertRaisesRegex(ValueError,'원문'):
            merge_dictionary({'entities':[],'aliases':[],'relations':[]},{k:v+v for k,v in first.items()},source,'새 구간')
        invalid=copy.deepcopy(first);invalid['entities'][0]['quote']='invented'
        with self.assertRaises(ValueError):merge_dictionary(invalid,invalid,source,'새 구간')

    def test_bounds_and_new_entity_alias_collisions_still_reject(self):
        d=self.dictionary();bad=copy.deepcopy(d);bad['entities']=[bad['entities'][0]]*201
        with self.assertRaisesRegex(ValueError,'최대'):merge_dictionary(d,bad,self.q1,self.q1)
        bad=copy.deepcopy(d);bad['entities'].append({'id':'CR','name':'CR','type':'Region','quote':self.q1})
        with self.assertRaisesRegex(ValueError,'중복'):merge_dictionary({'entities':[],'aliases':[],'relations':[]},bad,self.q1,self.q1)
        source=' '.join(f'개체{i}' for i in range(201))
        entities=[{'id':f'개체{i}','name':f'개체{i}','type':'T','quote':f'개체{i}'} for i in range(201)]
        with self.assertRaisesRegex(ValueError,'최대'):
            merge_dictionary({'entities':entities[:200],'aliases':[],'relations':[]},{'entities':entities[200:],'aliases':[],'relations':[]},source,source)
