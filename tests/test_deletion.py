import concurrent.futures
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_workflow import SOURCE

from verinote.engine import query
from verinote.service import Service
from verinote.store import Conflict, Store


class Deletion(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve

    def add_version(self):
        d = self.store.document(self.doc['id'])
        return self.store.register('운영','next.md',SOURCE.encode(),SOURCE,d['id'],d['current_version'],d['generation'])

    def delete(self, latest=False, doc=None):
        d = doc or self.store.document(self.doc['id'])
        return self.store.delete_document(d['id'],latest,d['current_version'],d['generation'])

    def test_latest_restores_approvals_no_reuse_and_aba(self):
        original = self.doc
        self.approve(self.extract())
        old_snapshot = self.store.snapshot()
        self.add_version()
        self.approve(self.extract())
        removed_snapshot = self.store.snapshot()
        self.delete(True)
        d = self.store.document(self.doc['id'])
        self.assertEqual((d['version'],d['next_version'],d['generation']), (1,3,3))
        self.assertEqual(len(query(self.store.snapshot())['rows']),2)
        self.assertEqual(self.store.snapshot(old_snapshot['id']),old_snapshot)
        with self.assertRaises(ValueError):self.store.snapshot(removed_snapshot['id'])
        with self.assertRaises(ValueError):self.store.raw(d['id'],2)
        with self.assertRaises(Conflict):self.delete(False,original)
        with self.assertRaises(Conflict):
            self.store.register('x','x.md',b'x','x',d['id'],1,original['generation'])
        with self.assertRaises(ValueError):
            self.store.register('x','x.md',b'x','x',d['id'],1)
        d = self.add_version()
        self.assertEqual(d['version'],3)
        self.assertEqual(self.store.documents()[0]['version_count'],2)
        self.assertEqual([v['version'] for v in d['versions']], [3,1])

    def test_whole_purges_related_history_and_preserves_unaffected(self):
        # An earlier snapshot has no reference to our target document.
        other = self.store.register('Other','o.md',b'other','other')
        with self.store.connect(True) as db:
            # Build an unaffected historical snapshot through actual persisted state.
            db.execute('DELETE FROM documents WHERE id=?',(self.doc['id'],))
        unaffected = self.store.snapshot()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO documents(id,title,current_version,next_version,generation) VALUES(?,?,1,2,1)',(self.doc['id'],'운영'))
        items = self.extract() # pending facts: snapshot has only document map for target.
        snapshot = self.store.snapshot()
        q = self.store.create_question('source question','agy','',snapshot['id'])
        q.update(status='success',answer={'source':SOURCE})
        self.store.save_question(q)
        self.store.save_query(query(snapshot))
        run_id=items[0]['run_id']
        for kind,key in [('runs',run_id),('questions',q['id'])]:
            folder=self.root/kind/key;folder.mkdir(parents=True);(folder/'prompt.txt').write_text(SOURCE)
        service=Service(self.store,self.root)
        d=self.store.document(self.doc['id'])
        result=service.delete_document(d['id'],False,{'expected_version':1,'expected_generation':1})
        service.pool.shutdown()
        self.assertEqual(result['cleanup_status'],'complete')
        self.assertEqual(result['removed_questions'],1)
        self.assertEqual(self.store.questions(),[]);self.assertEqual(self.store.queries(),[])
        self.assertFalse((self.root/'runs'/run_id).exists())
        self.assertFalse((self.root/'questions'/q['id']).exists())
        for table in ('versions','items','events','runs'):
            with self.store.connect() as db:
                count=db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                self.assertEqual(count,1 if table=='versions' else 0)
        self.assertEqual(self.store.document(other['id'])['text'],'other')
        self.assertEqual(self.store.snapshot(unaffected['id']),unaffected)
        for fn in [lambda:self.store.document(d['id']),lambda:self.store.raw(d['id'],1),lambda:self.store.snapshot(snapshot['id']),lambda:self.store.question(q['id']),lambda:self.store.save_question(q),lambda:self.store.create_question('x','agy','',snapshot['id']),lambda:self.store.save_query(query(snapshot))]:
            with self.assertRaises(ValueError):fn()

    def test_single_version_and_active_jobs_block_deletion(self):
        with self.assertRaises(Conflict):self.delete(True)
        run=self.store.create_run(self.doc['id'],'agy','','ontology',1,1)
        for status in ('queued','running'):
            self.store.run_status(run['id'],status)
            with self.assertRaises(Conflict):self.delete()
        self.store.run_status(run['id'],'error')
        q=self.store.create_question('q','agy','',self.store.snapshot()['id'])
        for status in ('queued','running'):
            q['status']=status;self.store.save_question(q)
            with self.assertRaises(Conflict):self.delete()
        q['status']='error';self.store.save_question(q)
        self.delete()

    def test_cleanup_failure_restart_retry_and_symlink_containment(self):
        items=self.extract();key=items[0]['run_id']
        folder=self.root/'runs'/key;folder.mkdir(parents=True);(folder/'prompt.txt').write_text(SOURCE)
        service=Service(self.store,self.root)
        with patch('verinote.service.shutil.rmtree',side_effect=PermissionError('denied')):
            result=service.delete_document(self.doc['id'],False,{'expected_version':1,'expected_generation':1})
        self.assertEqual(result['cleanup_status'],'pending')
        self.assertEqual(self.store.documents(),[])
        self.assertTrue(folder.exists());service.pool.shutdown()
        restarted=Service(Store(self.root/'state.sqlite3'),self.root)
        self.assertFalse(folder.exists());self.assertEqual(self.store.pending_cleanup(),[])
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('safe')
        folder.symlink_to(outside,target_is_directory=True)
        with self.store.connect(True) as db:db.execute('INSERT INTO log_cleanup VALUES(?,?)',('runs',key))
        self.assertEqual(restarted.cleanup_logs()['cleanup_status'],'pending')
        self.assertTrue((outside/'keep').exists());restarted.pool.shutdown()

    def test_existing_database_migration(self):
        path=self.root/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT,current_version INTEGER)')
            db.execute('CREATE TABLE versions(document_id TEXT,version INTEGER,filename TEXT,text TEXT,raw BLOB,sha256 TEXT,created_at TEXT,PRIMARY KEY(document_id,version))')
            db.execute("INSERT INTO documents VALUES('legacy','old',4)")
            db.execute("INSERT INTO versions VALUES('legacy',4,'a.md','old',X'61','hash','date')")
        migrated=Store(path)
        d=migrated.document('legacy')
        self.assertEqual((d['next_version'],d['generation']),(5,1))
        d=migrated.register('old','b.md',b'b','b','legacy',4,1)
        self.assertEqual(d['version'],5)
        self.assertEqual(Store(path).document('legacy')['next_version'],6)

    def test_snapshot_registration_and_query_save_races(self):
        for operation in ('question','query'):
            with self.subTest(operation=operation):
                # Fresh store for each controlled delete-between-read-and-write race.
                store=Store(self.root/(operation+'.sqlite3'))
                doc=store.register('x','x.md',b'x','x')
                service=Service(store,self.root/operation)
                ready,release=threading.Event(),threading.Event()
                method=store.create_question if operation=='question' else store.save_query
                def delayed(*args):
                    ready.set();release.wait(5);return method(*args)  # noqa: B023 (intentional: same-iteration ready/release/method)
                with patch.object(store,'create_question' if operation=='question' else 'save_query',side_effect=delayed):
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future=pool.submit(service.ask,{'question':'q'}) if operation=='question' else pool.submit(service.query,{})
                        self.assertTrue(ready.wait(3))
                        store.delete_document(doc['id'],False,1,1)
                        release.set()
                        with self.assertRaises(Conflict):future.result()
                self.assertEqual(store.questions(),[]);self.assertEqual(store.queries(),[])
                self.assertTrue(service.slots.acquire(blocking=False));service.slots.release()
                service.pool.shutdown()

    def test_concurrent_upload_delete_and_duplicate_delete(self):
        self.add_version();d=self.store.document(self.doc['id'])
        gate=threading.Barrier(2)
        def work(upload):
            gate.wait()
            try:
                if upload:self.store.register('x','x.md',b'x','x',d['id'],2,2)
                else:self.delete(True,d)
                return 'success'
            except Conflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(work,[True,False])),['success','conflict'])
        d=self.store.document(self.doc['id']);gate=threading.Barrier(2)
        def erase(_):
            gate.wait()
            try:self.delete(False,d);return 'success'
            except ValueError:return 'gone'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(erase,range(2))),['success','gone'])


class DeletionHTTP(unittest.TestCase):
    setUp=workflow.HTTP.setUp
    tearDown=workflow.HTTP.tearDown
    request=workflow.HTTP.request

    def test_endpoints_contract_diff_gaps_and_origin(self):
        d=self.request('/api/documents','POST',{'title':'x','filename':'x.md','text':'one'})
        self.request('/api/documents','POST',{'title':'x','filename':'x.md','text':'two','document_id':d['id'],'expected_version':1,'expected_generation':1})
        path=f'/api/documents/{d["id"]}/delete-latest'
        for data,status,headers in [({'expected_version':2},400,{}),({'expected_version':True,'expected_generation':2},400,{}),({'expected_version':2,'expected_generation':True},400,{}),({'expected_version':2,'expected_generation':1},409,{}),({'expected_version':2,'expected_generation':2},403,{'Origin':'https://evil.example'})]:
            with self.assertRaises(HTTPError) as error:self.request(path,'POST',data,headers)
            self.assertEqual(error.exception.code,status);error.exception.close()
        self.request(path,'POST',{'expected_version':2,'expected_generation':2})
        d=self.request('/api/documents/'+d['id'])
        self.assertEqual(d['version'],1)
        d=self.request('/api/documents','POST',{'title':'x','filename':'x.md','text':'three','document_id':d['id'],'expected_version':1,'expected_generation':3})
        diff=self.request(f'/api/documents/{d["id"]}/diff')['diff']
        self.assertIn('--- v1',diff);self.assertIn('+++ v3',diff)
        self.request(f'/api/documents/{d["id"]}/delete','POST',{'expected_version':3,'expected_generation':4})
        self.assertEqual(self.request('/api/documents'),[])
        self.assertEqual(self.request('/api/cleanup')['cleanup_pending'],0)
        self.assertEqual(self.request('/api/cleanup','POST',{})['cleanup_status'],'complete')
