"""Copilot opt-in through real Service/CLI with local subprocesses, never a model."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_quote_repair import broken, repair
from test_workflow import SOURCE, approved_dictionary, extraction_response

from verinote.agents import CLI, command
from verinote.copilot_extraction import (
    CopilotExtractionSpec,
    CopilotExtractionTransport,
)
from verinote.extraction_options import copilot_request, extraction_options
from verinote.extraction_scheduler import ChunkScheduler
from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service
from verinote.store import Store
from verinote.usage import job_calls, summarize

STUB = r'''
import json, pathlib, sys, time
root = pathlib.Path(CONTROL)
config = json.loads((root/'config.json').read_text())
folder = pathlib.Path.cwd().parent
is_repair = folder.name == 'quote-repair'
index = int(folder.parent.name if is_repair else folder.name)
run_id = folder.parent.parent.name if is_repair else folder.parent.name
record = root/run_id
record.mkdir(exist_ok=True)
key = str(index) + ('-repair' if is_repair else '-initial')
started = record/(key+'.start')
writing = record/(key+'.tmp')
writing.write_text(json.dumps({'at': time.monotonic_ns(), 'argv': sys.argv, 'binary': __file__}))
writing.replace(started)
if not is_repair:
    for pair in config.get('barriers', []):
        if index in pair:
            deadline = time.monotonic()+12
            while not all((record/(str(i)+'-initial.start')).exists() for i in pair):
                if time.monotonic()>deadline: sys.exit(13)
                time.sleep(.005)
if not is_repair and index in config.get('gates', []):
    deadline = time.monotonic()+12
    while not (root/('release-'+str(index))).exists():
        if time.monotonic()>deadline: sys.exit(12)
        time.sleep(.005)
if index not in config.get('missing', []):
    usage = {'currentModel': 'synthetic-model', 'totalNanoAiu': index*100000000,
             'tokenDetails': {k: {'tokenCount': 1} for k in ('input','output','cache_read','cache_write')},
             'aiCreditsStatus': 'complete'}
    pathlib.Path(sys.argv[sys.argv.index('--usage-output-file')+1]).write_text(json.dumps(usage))
if index in config.get('fail', []):
    print('synthetic process failure', file=sys.stderr)
    (record/(key+'.end')).write_text(str(time.monotonic_ns()))
    sys.exit(7)
value = config['repair'] if is_repair else config['bad'] if index in config.get('broken', []) else config['good']
if not is_repair: value['claims'][0]['heading'] = str(index)
print(json.dumps(value, ensure_ascii=False))
(record/(key+'.end')).write_text(str(time.monotonic_ns()))
'''


class CopilotIntegration(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    def tearDown(self):
        self.doCleanups()
        self.temp.cleanup()

    def setup_cli(self, **options):
        self.control = self.root/'control'; self.control.mkdir()
        self.config = dict(good=extraction_response(), bad=broken(), repair=repair(), **options)
        self.configure()
        self.bin_dir = self.root/'bin'; self.bin_dir.mkdir()
        self.binary = self.bin_dir/'copilot'
        self.binary.write_text('#!'+sys.executable+'\nCONTROL = '+repr(str(self.control))+'\n'+STUB)
        self.binary.chmod(0o755)
        environment = patch.dict(os.environ, {'PATH': str(self.bin_dir)+os.pathsep+os.environ['PATH']})
        environment.start(); self.addCleanup(environment.stop)
        self.rates = {'copilot': {'model': 'synthetic-model', 'input': 1, 'output': 2, 'cache_read': 0, 'cache_write': 1,
                                 'credit_unit': '', 'usd_per_credit': None}}
        self.cli = CLI(timeout=10, rates=lambda: self.rates, persistent=False)
        self.service = Service(self.store, self.root, self.cli)
        self.addCleanup(self.service.close)
        self.addCleanup(lambda control=self.control: [(control/f'release-{i}').touch() for i in range(1, 17)] if control.exists() else None)

    def configure(self, **options):
        self.config.update(options)
        (self.control/'config.json').write_text(json.dumps(self.config))

    def release(self, index): (self.control/f'release-{index}').touch()

    def wait(self, predicate):
        deadline = time.monotonic()+8
        while not predicate():
            if time.monotonic()>deadline: self.fail('stub synchronization timed out')
            time.sleep(.005)

    def marker(self, run, index, phase='start', repair_call=False):
        return self.control/run['id']/f'{index}-{"repair" if repair_call else "initial"}.{phase}'

    def document(self, count=4):
        source = SOURCE.ljust(8000)*(count-1)+SOURCE
        doc = self.store.register('copilot', 'copilot.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def start(self, doc, **kwargs):
        return self.service.start(doc['id'], 'copilot', 'auto', expected_version=doc['version'],
                                  expected_generation=doc['generation'], **kwargs)

    def finish(self, run):
        self.service.pool.wait_idle()
        return self.store.run(run['id'])

    def checkpoint(self, run):
        path = self.root/'runs'/run['id']/'extraction-checkpoint.json'
        return json.loads(path.read_text())['outputs'] if path.exists() else []

    def started(self, run): return sorted((self.control/run['id']).glob('*.start'))

    def test_default_argv_count_and_order_are_unchanged(self):
        self.setup_cli(); run = self.finish(self.start(self.document(3)))
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIsNone(run['extraction_options'])
        previous = 0
        for i, path in enumerate(self.started(run), 1):
            record = json.loads(path.read_text()); argv = record['argv']
            self.assertNotIn('--no-auto-update', argv)
            expected, _ = command('copilot', 'auto', argv[argv.index('-p')+1])
            expected[0] = str(self.binary)
            expected[1:1] = ['--usage-output-file', str((self.root/'runs'/run['id']/str(i)/'provider-usage.json').resolve())]
            self.assertEqual(argv, expected)
            self.assertGreater(record['at'], previous)
            previous = int(self.marker(run, i, 'end').read_text())
        self.assertEqual(len(self.started(run)), 3)

    def test_overlap_reverse_completion_contiguous_prefix_and_one_publication(self):
        self.setup_cli(gates=[1, 3], barriers=[[1, 2], [3, 4]]); doc = self.document()
        with patch.object(self.store, 'finish_run', wraps=self.store.finish_run) as publish:
            run = self.start(doc, extraction_concurrency=2)
            self.wait(lambda: self.marker(run, 2, 'end').exists())
            self.assertEqual(len(self.started(run)), 2)
            self.assertEqual(self.checkpoint(run), [])
            publish.assert_not_called()
            self.release(1)
            self.wait(lambda: self.marker(run, 4, 'end').exists())
            self.assertEqual([v['claims'][0]['heading'] for v in self.checkpoint(run)], ['1', '2'])
            publish.assert_not_called()
            self.release(3); final = self.finish(run)
            self.assertEqual(final['status'], 'success', final['detail']); self.assertEqual(publish.call_count, 1)
        self.assertEqual([v['claims'][0]['heading'] for v in self.checkpoint(run)], ['1','2','3','4'])
        events = [(json.loads(p.read_text())['at'], 1) for p in self.started(run)]
        events += [(int(p.read_text()), -1) for p in (self.control/run['id']).glob('*.end')]
        active = maximum = 0
        for _, change in sorted(events): active += change; maximum = max(active, maximum)
        self.assertEqual(maximum, 2); self.assertEqual(active, 0)
        calls = job_calls(self.root, 'runs', final); total = summarize(calls)
        self.assertEqual(len(calls), 4); self.assertTrue(all(c['complete'] for c in calls))
        self.assertIsNone(total['reported_usd']['value'])
        self.assertEqual(total['credits'][0]['unit'], 'GitHub AI credits')
        self.assertAlmostEqual(total['credits'][0]['value'], 1.0)
        self.assertEqual(self.store.snapshot()['facts'], [])

    def test_registration_pins_path_and_mutable_settings_for_initial_and_repair(self):
        self.setup_cli(broken=[1]); doc = self.document(2)
        release = threading.Event()
        from test_run_cancel import occupy_worker
        for i in range(2): occupy_worker(self.service, release, key=f'pin-{i}')
        old_rates = copy.deepcopy(self.rates['copilot'])
        run = self.start(doc, extraction_concurrency=2)
        saved = run['extraction_options']
        self.assertEqual(saved['schema_version'], 3); self.assertEqual(saved['agent'], 'copilot')
        self.assertEqual(saved['transport']['binary'], str(self.binary.resolve()))
        self.assertTrue(saved['transport']['no_auto_update'])
        self.assertEqual(saved['transport']['timeout'], 10)
        self.assertEqual(saved['transport']['max_output'], 1048576)
        other = self.root/'other'; other.mkdir()
        replacement = other/'copilot'; replacement.write_text('#!'+sys.executable+'\nraise RuntimeError("wrong PATH")\n'); replacement.chmod(0o755)
        self.cli.timeout = .001; self.cli.max_output = 1; self.rates['copilot']['input'] = 999
        with patch.dict(os.environ, {'PATH': str(other)+os.pathsep+os.environ['PATH']}):
            release.set(); final = self.finish(run)
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(len(self.started(run)), 3)
        self.assertTrue(self.marker(run, 1, repair_call=True).exists())
        for path in self.started(run):
            argv = json.loads(path.read_text())['argv']
            self.assertEqual(argv[0], str(self.binary.resolve())); self.assertIn('--no-auto-update', argv)
            self.assertEqual(argv[argv.index('--model')+1], 'auto')
        for path in (self.root/'runs'/run['id']).rglob('usage.json'):
            self.assertEqual(json.loads(path.read_text())['rate_snapshot'], old_rates)
        self.assertEqual(Store(self.store.path).run(run['id'])['extraction_options'], saved)

    def test_failures_stop_new_calls_and_repairs_but_drain_usage(self):
        for failure in (1, 2):
            with self.subTest(failure=failure):
                # Separate roots also prove no transport state leaks between runs.
                with tempfile.TemporaryDirectory() as tmp:
                    original_root, original_store = self.root, self.store
                    self.root = Path(tmp); self.store = Store(self.root/'state.sqlite3')
                    self.setup_cli(gates=[3-failure], fail=[failure], broken=[1], missing=[3-failure])
                    failed = threading.Event(); original_fail = ChunkScheduler.fail
                    def observe(scheduler, index, exc, original=original_fail, event=failed):
                        result = original(scheduler, index, exc); event.set(); return result
                    with patch.object(ChunkScheduler, 'fail', observe):
                        run = self.start(self.document(), extraction_concurrency=2)
                        self.assertTrue(failed.wait(8))
                        self.release(3-failure); final = self.finish(run)
                    self.assertEqual(final['status'], 'error'); self.assertIn('CLI 종료 코드 7', final['detail'])
                    self.assertNotIn('rate limit', final['detail'].lower())
                    self.assertEqual(len(self.started(run)), 2); self.assertEqual(self.checkpoint(run), [])
                    self.assertFalse(any('repair' in p.name for p in self.started(run)))
                    calls = job_calls(self.root, 'runs', final)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(sum(c['reported_credits'] is not None for c in calls), 1)
                    self.assertTrue(all(not c['complete'] for c in calls if c['reported_credits'] is None))
                    total = summarize(calls)
                    self.assertIsNone(total['reported_usd']['value'])
                    self.assertAlmostEqual(total['credits'][0]['value'], failure/10)
                    self.assertEqual([i['kind'] for i in self.store.document(run['document_id'])['items']], ['ontology'])
                    self.service.close()
                    self.root, self.store = original_root, original_store

    def test_schema3_resume_and_changed_binary_restart(self):
        self.setup_cli(gates=[3], fail=[3]); doc = self.document()
        run = self.start(doc, extraction_concurrency=2)
        self.wait(lambda: self.marker(run, 4, 'end').exists()); self.release(3)
        failed = self.finish(run)
        self.assertEqual(failed['status'], 'error'); self.assertEqual(len(self.checkpoint(run)), 2)
        self.configure(gates=[], fail=[])
        resumed = self.finish(self.start(doc, extraction_concurrency=2))
        self.assertEqual(resumed['status'], 'success', resumed['detail'])
        self.assertEqual([p.name for p in self.started(resumed)], ['3-initial.start', '4-initial.start'])
        # A second failed document's same-model checkpoint cannot cross a binary pin.
        doc = self.document(3); self.configure(gates=[3], fail=[3])
        run = self.start(doc, extraction_concurrency=2)
        failed = self.finish(run)
        self.assertEqual(len(self.checkpoint(run)), 2)
        self.configure(gates=[], fail=[])
        old = self.binary; self.binary = self.bin_dir/'copilot-new'; self.binary.write_text(old.read_text()); self.binary.chmod(0o755)
        with patch('verinote.copilot_extraction.executable', return_value=str(self.binary)):
            restarted = self.finish(self.start(doc, extraction_concurrency=2))
        self.assertEqual(restarted['status'], 'success', restarted['detail'])
        self.assertEqual(len(self.started(restarted)), 3)

    def test_invalid_or_corrupted_contract_fails_before_process_admission(self):
        self.setup_cli(); doc = self.document(1)
        for kwargs in ({'extraction_concurrency': 1}, {'extraction_concurrency': True},
                       {'extraction_policy': 'chunk_threshold_v1'}, {'extraction_concurrency': 2, 'stage': 'ontology'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.start(doc, **kwargs)
        self.assertFalse(list(self.control.glob('*/*.start')))
        request = copilot_request(CopilotExtractionSpec.capture(self.cli, 'auto'))
        run = self.store.create_run(doc['id'], 'copilot', 'auto', expected_version=doc['version'], expected_generation=doc['generation'], extraction_options=request)
        corrupt = copy.deepcopy(run); corrupt['extraction_options']['transport']['agent'] = 'claude'
        self.service.slots.acquire(); self.service.extract(corrupt)
        self.assertEqual(self.store.run(run['id'])['status'], 'error')
        self.assertFalse(list(self.control.glob('*/*.start')))
        with self.store.connect(True) as db:
            db.execute('UPDATE runs SET extraction_options=? WHERE id=?', (json.dumps(corrupt['extraction_options']), run['id']))
        with self.assertRaises(ValueError): Store(self.store.path).run(run['id'])

    def test_missing_usage_file_stays_unknown_in_stored_service_summary(self):
        self.setup_cli(); run = self.finish(self.start(self.document(2), extraction_concurrency=2))
        self.assertEqual(run['status'], 'success', run['detail'])
        (self.root/'runs'/run['id']/'2'/'usage.json').unlink()
        summary = self.service.with_usage('runs', self.store.run(run['id']))['usage']
        self.assertEqual(summary['calls'], 2); self.assertEqual(summary['incomplete_calls'], 1)
        self.assertEqual(summary['unknown_calls'], 1)
        self.assertIsNone(summary['reported_usd']['value'])
        self.assertEqual(summary['credits'][0]['unit'], 'GitHub AI credits')
        self.assertAlmostEqual(summary['credits'][0]['value'], .1)
        self.assertIsNotNone(summary['estimated_usd']['value'])

    def test_owner_close_blocks_new_calls_and_drains_active_views(self):
        self.setup_cli(gates=[1]); run = self.start(self.document(2), extraction_concurrency=2)
        self.wait(lambda: self.marker(run, 1).exists())
        self.wait(lambda: self.marker(run, 2).exists())
        owner = self.service.run_owners[run['id']]; view = owner.view()
        with ThreadPoolExecutor() as pool:
            closing = pool.submit(owner.close)
            self.wait(lambda: owner.closed)
            self.assertFalse(closing.done())
            with self.assertRaises(ValueError): view.complete('copilot', 'auto', 'unused', self.root/'unused')
            self.release(1); closing.result(timeout=8)
        self.assertEqual(self.finish(run)['status'], 'success')

    def test_transport_views_retirement_and_service_close_drain_active_processes(self):
        self.setup_cli(gates=[1]); doc = self.document(2)
        run = self.start(doc, extraction_concurrency=2)
        self.wait(lambda: self.marker(run, 1).exists())
        owner = self.service.run_owners[run['id']]; view = owner.view(); view.close()
        self.assertFalse(owner.closed)
        with self.assertRaises(ValueError): owner.retire_if_idle()
        with ThreadPoolExecutor() as pool:
            closing = pool.submit(self.service.close)
            self.wait(lambda: self.service.closed)
            self.assertFalse(closing.done())
            with self.assertRaises(ValueError): self.start(doc, extraction_concurrency=2)
            self.release(1); closing.result(timeout=8)
        self.assertTrue(owner.closed); owner.close()
        with self.assertRaises(ValueError): view.complete('copilot', 'auto', 'no call', self.root/'unused')
        self.assertEqual(self.store.run(run['id'])['status'], 'success')


class CopilotContracts(unittest.TestCase):
    def spec(self): return CopilotExtractionSpec(str(Path(sys.executable).resolve()), 'auto', 2, 10, 1048576, 'null')

    def test_strict_schema_and_provider_separation(self):
        value = copilot_request(self.spec())
        invalid = [value | {'schema_version': True}, value | {'agent': 'claude'}, value | {'concurrency': True},
                   value | {'concurrency': 1}, value | {'policy': 'chunk_threshold_v1'}, value | {'document_generation': 1}]
        for field, bad in [('binary','relative'), ('no_auto_update',False), ('no_auto_update',1), ('agent','claude'),
                           ('capacity',1), ('capacity',True), ('model','other'), ('timeout',-1), ('max_output',True)]:
            invalid.append(value | {'transport': value['transport'] | {field: bad}})
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                extraction_options(entry, registered=False, agent='copilot', model='auto')
        for agent, stage in [('claude','extract'), ('copilot','ontology')]:
            with self.assertRaises(ValueError): extraction_options(value, registered=False, agent=agent, stage=stage)
        for version in (1, 2):
            from verinote.extraction_options import experiment_request, policy_request
            legacy = ExtractionSpec('/fake/claude','auto',2,10,1048576,'null')
            entry = experiment_request(legacy) if version == 1 else policy_request(legacy, 'chunk_threshold_v1')
            with self.assertRaises(ValueError): extraction_options(entry, registered=False, agent='copilot')
        transport = CopilotExtractionTransport(self.spec()); self.addCleanup(transport.close)
        for agent, model, phase in [('claude','auto',None), ('copilot','other',None), ('copilot','auto','ontology')]:
            with self.assertRaises(ValueError): transport.complete(agent, model, 'unused', Path('/unused'), phase=phase)
