"""Per-file Service overlap and ownership with local fake callers only."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_claude_worker import FAKE
from test_ontology import wire_dictionary
from test_workflow import DICTIONARY, SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI
from verinote.service import Service
from verinote.store import Store


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'db')
        self.cv = threading.Condition(); self.calls = {}; self.releases = {}; self.fail = set()
        test = self
        class Caller:
            def limited_timeout(self, *args): return self
            def complete(self, agent, model, prompt, folder, *, phase=None):
                run = Path(folder).parent.name
                with test.cv:
                    test.calls.setdefault(run, []).append(phase)
                    release = test.releases.setdefault(run, threading.Event()); test.cv.notify_all()
                if not release.wait(8): raise TimeoutError('file gate')
                if run in test.fail: raise ValueError('file failure')
                return wire_dictionary(DICTIONARY, prompt) if phase == 'ontology' else extraction_response()
        self.service = Service(self.store, self.root, Caller())
        self.addCleanup(self.cleanup)

    def cleanup(self):
        # Release both already active and subsequently admitted jobs.
        with self.cv:
            for event in self.releases.values(): event.set()
        with patch.object(self.service.cli, 'complete', side_effect=ValueError('cleanup')):
            self.service.close()

    def doc(self, name, stage, chunks=1):
        source = SOURCE.ljust(8000) * (chunks-1) + SOURCE
        doc = self.store.register(name, name+'.md', source.encode(), source)
        if stage == 'extract': approved_dictionary(self.store, doc['id'])
        return self.store.document(doc['id'])

    def start(self, name, stage, chunks=1):
        doc = self.doc(name, stage, chunks)
        return self.service.start(doc['id'], 'agy', '', stage, doc['version'], doc['generation'])

    def entered(self, *runs):
        with self.cv:
            self.assertTrue(self.cv.wait_for(lambda: all(r['id'] in self.calls for r in runs), timeout=3))

    def release(self, *runs):
        with self.cv:
            for r in runs: self.releases[r['id']].set()

    def test_four_documents_overlap_thirds_wait_and_publish_independently(self):
        first = [self.start(s+str(i), s) for s in ('ontology','extract') for i in range(2)]
        self.entered(*first)
        queued = [self.start(s+'third', s) for s in ('ontology','extract')]
        self.assertTrue(all(self.store.run(r['id'])['status']=='queued' for r in queued))
        for r in queued:
            context = self.service.queue_context(r['id'])
            self.assertEqual(context['capacity'], 2)
            self.assertEqual(len(context['ahead']), 2)
            self.assertTrue(all(j['stage']==r['stage'] for j in context['ahead']))
        self.release(first[0], first[2]); self.entered(*queued)
        self.release(*first, *queued); self.service.pool.wait_idle()
        for r in first+queued:
            row = self.store.run(r['id']); self.assertEqual(row['status'], 'success', row['detail'])
            self.assertTrue((self.root/'runs'/r['id']/'context-policy.json').exists())
        self.assertEqual(self.service.slots._value, 8)

    def test_failure_and_cancellation_do_not_stop_other_document(self):
        a, b = self.start('a','extract'), self.start('b','extract'); self.entered(a,b)
        self.service.cancel(a['id'], 'running')
        self.release(a)
        self.release(b); self.service.pool.wait_idle()
        self.assertEqual(self.store.run(a['id'])['status'], 'cancelled')
        self.assertEqual(self.store.run(b['id'])['status'], 'success')
        c, d = self.start('c','ontology'), self.start('d','ontology'); self.entered(c,d)
        self.fail.add(c['id']); self.release(c,d); self.service.pool.wait_idle()
        self.assertEqual(self.store.run(c['id'])['status'], 'error')
        self.assertEqual(self.store.run(d['id'])['status'], 'success')
        self.assertEqual(self.store.document(c['document_id'])['items'], [])
        self.assertEqual(self.store.document(d['document_id'])['items'][0]['status'], 'pending')

    def test_same_document_retry_waits_while_unrelated_document_runs(self):
        first = self.start('first','extract', 2); self.entered(first)
        self.service.cancel(first['id'], 'running')
        doc = self.store.document(first['document_id'])
        retry = self.service.start(doc['id'], 'agy','', 'extract',doc['version'],doc['generation'])
        other = self.start('other','extract'); self.entered(other)
        self.assertEqual(self.store.run(retry['id'])['status'], 'queued')
        self.assertEqual(self.service.queue_context(retry['id'])['document_blockers'][0]['id'], first['id'])
        self.release(first); self.entered(retry); self.release(retry,other)
        self.service.pool.wait_idle()
        self.assertEqual(self.store.run(retry['id'])['status'], 'success')

    def test_eight_admitted_jobs_bound_and_queued_cancel_returns_one_slot(self):
        runs = [self.start(str(i),'ontology') for i in range(8)]
        self.entered(*runs[:2])
        with self.assertRaisesRegex(ValueError, '가득'): self.start('ninth','extract')
        self.service.cancel(runs[-1]['id'], 'queued')
        self.assertEqual(self.service.slots._value, 1)
        extra = self.start('extra','extract'); self.entered(extra)
        self.assertEqual(self.service.slots._value, 0)

    def test_cli_fork_preserves_config_but_owns_its_manager(self):
        cli = CLI(timeout=19, max_output=20000); self.addCleanup(cli.close)
        cli._pinned_claude_binary = '/pinned/claude'
        cli._pinned_copilot_binary = '/pinned/copilot'
        a, b = cli.fork_job(), cli.fork_job()
        self.addCleanup(a.close); self.addCleanup(b.close)
        self.assertIsNot(a._claude, b._claude); self.assertIsNot(a._claude, cli._claude)
        self.assertEqual((a.timeout, a.max_output, a._pinned_claude_binary, a._pinned_copilot_binary),
                         (19,20000,'/pinned/claude','/pinned/copilot'))
        self.assertIs(a.limited_timeout('claude', 5)._claude, a._claude)
        self.assertIsNone(CLI(persistent=False).fork_job()._claude)

    def test_shutdown_rejects_questions_as_well_as_runs(self):
        self.service.close()
        with self.assertRaisesRegex(ValueError, '닫혔'): self.service.ask({'question':'q','agent':'agy'})

    def test_default_cli_processes_are_isolated(self):
        self.assert_cli_processes(None)

    def test_experimental_cli_processes_are_isolated(self):
        self.assert_cli_processes(2)

    def assert_cli_processes(self, concurrency):
        self.service.close()
        stub = self.root/'worker'; stub.mkdir()
        script = stub/'fake.py'
        code = FAKE.replace("if text=='wait':", "if text!='/clear':")
        old = "body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)"
        code = code.replace(old, 'body=json.loads('+repr(json.dumps(extraction_response()))+')')
        script.write_text(code)
        cli = CLI(timeout=5)
        self.service = Service(self.store, self.root, cli)
        self.addCleanup(lambda: (stub/'release').touch())
        with patch('verinote.agents.executable', return_value=sys.executable), patch('verinote.extraction_transport.executable', return_value=sys.executable), patch('verinote.claude_worker.command',
                side_effect=lambda binary, model: [sys.executable,str(script),str(stub),model]):
            docs = [self.doc(str(i), 'extract') for i in range(2)]
            runs = [self.service.start(d['id'],'claude',model,'extract',d['version'],d['generation'], extraction_concurrency=concurrency)
                    for d,model in zip(docs,('model-a','model-b'),strict=True)]
            deadline = time.monotonic()+3
            records = []
            while time.monotonic()<deadline:
                try: records = [r for line in (stub/'transcript').read_text().splitlines() if (r := json.loads(line))['text'] != '/clear']
                except (FileNotFoundError, json.JSONDecodeError): pass
                if len(records)==2: break
                time.sleep(.005)
            self.assertEqual(len(records),2)
            self.assertEqual(len({r['pid'] for r in records}),2)
            self.assertTrue(all(r['history']==[] for r in records))
            (stub/'release').touch(); self.service.pool.wait_idle()
        for run in runs:
            self.assertEqual(self.store.run(run['id'])['status'],'success')
            usage = self.service.with_usage('runs',self.store.run(run['id']))['usage']
            self.assertEqual(len(usage['details']), 1)
            self.assertTrue((self.root/'runs'/run['id']/'extraction-checkpoint.json').exists())
        self.assertIsNone(cli._claude.worker)

    def test_owner_creation_and_close_failure_release_capacity_without_harming_other_job(self):
        self.service.close()
        cli = CLI(); self.service = Service(self.store,self.root,cli)
        doc = self.doc('owner-fail','extract')
        with patch.object(cli,'fork_job',side_effect=RuntimeError('owner creation')):
            run = self.service.start(doc['id'],'agy','','extract',doc['version'],doc['generation'])
            self.service.pool.wait_idle()
        self.assertEqual(self.store.run(run['id'])['status'],'error')
        self.assertEqual(self.service.slots._value,8)
        owners = [CLI(), CLI()]
        for owner in owners: owner.complete = Mock(return_value=extraction_response())
        owners[0].close = Mock(side_effect=[RuntimeError('owner close'), None])
        owners[1].close = Mock()
        with patch.object(cli,'fork_job',side_effect=owners):
            runs = []
            for name in ('owner-a','owner-b'):
                doc = self.doc(name,'extract')
                runs.append(self.service.start(doc['id'],'agy','','extract',doc['version'],doc['generation']))
            self.service.pool.wait_idle()
        self.assertTrue(all(self.store.run(r['id'])['status']=='success' for r in runs))
        self.assertEqual(self.service.slots._value,8)
        self.assertEqual(self.service.failed_owners,[owners[0]])
        self.service.close()
        self.assertEqual(owners[0].close.call_count,2)
        self.assertEqual(owners[1].close.call_count,1)

    def test_same_document_retry_in_other_stage_waits_for_old_calls(self):
        first = self.start('cross-stage','extract'); self.entered(first)
        self.service.cancel(first['id'],'running')
        item = self.store.document(first['document_id'])['items'][0]
        self.store.review(item['id'],item['revision'],'rejected','tester','regenerate')
        doc = self.store.document(first['document_id']); item = doc['items'][0]
        retry = self.service.start(doc['id'],'agy','','ontology',doc['version'],doc['generation'],
                                   regenerate={'id':item['id'],'revision':item['revision']})
        other = self.start('cross-other','ontology'); self.entered(other)
        self.assertEqual(self.store.run(retry['id'])['status'],'queued')
        context = self.service.queue_context(retry['id'])
        self.assertEqual([r['id'] for r in context['document_blockers']],[first['id']])
        self.assertNotIn(first['id'],[r['id'] for r in context['ahead']])
        self.release(first); self.entered(retry); self.release(retry,other)
        self.service.pool.wait_idle()
        self.assertEqual(self.store.run(first['id'])['status'],'cancelled')
        self.assertEqual(self.store.run(retry['id'])['status'],'success')
