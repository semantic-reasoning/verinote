"""Synthetic transports only: a logged call with positive evidence that its model request never reached the provider is listed as a
not-started call, kept out of the confirmed call count and the usage totals; a call that started, or whose start cannot be ruled out,
is counted as before. The run detail's 새 호출 시도 N회 counts attempts, not-started calls included."""
import http.client
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_failed_run_detail import Base as RunBase
from test_failed_run_detail import Parallel, Sequential, number_of
from test_quote_repair import broken
from test_usage import record
from test_workflow import SOURCE, approved_dictionary, extraction_response

import verinote.agents as agents
import verinote.claude_worker as claude_worker
import verinote.openrouter as openrouter
from verinote.agents import CLI
from verinote.cli_timing import read_cli_timing
from verinote.config import Credentials
from verinote.ollama import Job
from verinote.service import Service
from verinote.store import Store
from verinote.transports import Credentials as TransportCredentials
from verinote.transports import ExecutionOptions
from verinote.usage import NS_WARNING, atomic_json, job_calls, summarize

ROOT = Path(__file__).resolve().parents[1]
QUESTION = '주문에서 원장까지 경로는?'
KEY = 'c' * 32
OLLAMA_MODEL = 'qwen2.5:7b'
CATALOG = {'models': [{'name': OLLAMA_MODEL, 'digest': 'a' * 64}]}


def script(folder, name, body, mode=0o755):
    path = Path(folder) / name
    path.write_text(body)
    path.chmod(mode)
    return str(path)


def owner(agent, model='', status='error', stage='extract'):
    return {'id': KEY, 'agent': agent, 'model': model, 'status': status, 'stage': stage}


def counted(root, run_owner):
    """(calls, not_started_calls, statuses) of the run whose call folder 1 the test wrote."""
    calls = job_calls(root, 'runs', run_owner)
    usage = summarize(calls)
    return usage['calls'], usage['not_started_calls'], [c['status'] for c in calls]


def render_usage(usage):
    app = (ROOT / 'verinote/web/app.js').read_text()
    source = ('const esc=String;' + app[app.index('function usageStage('):app.index('function downloadJSON(')]
              + f'\nprocess.stdout.write(renderUsage({json.dumps(usage)}));')
    return subprocess.check_output(['node'], input=source, text=True)


class Queue:
    """Runs submitted jobs on drain, after before() (the moment a transport can go down between registration and the call)."""
    def __init__(self):
        self.tasks, self.before = [], lambda: None

    def submit(self, fn, *args):
        self.tasks.append((fn, args))

    def shutdown(self, wait=True): pass

    def drain(self):
        self.before()
        while self.tasks:
            fn, args = self.tasks.pop(0)
            fn(*args)


def assert_unaccepted_usage_is_counted(case, run_owner, mismatched=True):
    """A not-started call whose usage.json is missing, corrupt or (where the transport checks it) does not match run.json is counted
    as a confirmed call with unknown usage, whatever run.json says; the evidence is read only with an accepted usage.json."""
    path = case.folder / 'usage.json'
    original = path.read_text()
    changes = {'missing': path.unlink, 'corrupt': lambda: path.write_text('{not json')}
    if mismatched:
        changes['mismatched'] = lambda: path.write_text(json.dumps(json.loads(original) | {'provider_call_id': 'f' * 32}))
    for name, change in changes.items():
        with case.subTest(usage_json=name):
            path.write_text(original)
            change()
            case.assertEqual(counted(case.root, run_owner), (1, 0, ['unknown']))


class FakeSocket:
    def __init__(self, on_send):
        self.on_send = on_send

    def settimeout(self, seconds): pass
    def sendall(self, data): self.on_send()
    def close(self): pass
    def shutdown(self, how): pass


def reset(): raise ConnectionResetError('synthetic reset after request bytes')


class Transports(unittest.TestCase):
    """T1/T2: each transport x (question, ontology run, extract run), failing before the call started and after it."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        self.failing = script(self.bin, 'fails.sh', '#!/bin/sh\nexit 3\n')

    def transport(self, name, started):
        """(service kwargs, agent, model, patches) for one transport whose first call fails before or after it started."""
        if name == 'local_cli':
            return {'cli': CLI(persistent=False)}, 'codex', '', [patch('verinote.agents.executable', return_value=self.failing if started else None)]
        if name == 'persistent_claude':
            return {'cli': CLI()}, 'claude', '', [patch('verinote.agents.executable', return_value=self.failing if started else None)]
        if name == 'openrouter':
            def connect(conn):
                if not started: raise ConnectionRefusedError('synthetic refusal')
                conn.sock = FakeSocket(reset)
            return {'credentials': Credentials('sk-or-synthetic-880')}, 'openrouter', 'vendor/model', [patch.object(http.client.HTTPSConnection, 'connect', connect)]
        state = {'up': True}

        def request(path, payload=None, **kwargs):
            if path == '/api/tags' and (state['up'] or started): return CATALOG
            raise ConnectionRefusedError('synthetic: Ollama is down') if path == '/api/tags' else ConnectionResetError('synthetic generate failure')
        self.down = lambda: state.update(up=False)
        return {'cli': CLI(persistent=False)}, 'ollama', OLLAMA_MODEL, [patch('verinote.ollama.request', side_effect=request)]

    def job(self, name, kind, started):
        """Runs one job on a fresh store; returns (owner, detail usage, the usage report group of the job's agent and stage)."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        store = Store(root / 'state.sqlite3')
        kwargs, agent, model, patches = self.transport(name, started)
        service = Service(store, root, **kwargs)
        service.pool.shutdown()
        service.pool = queue = Queue()
        self.addCleanup(service.close)
        if agent == 'ollama': queue.before = self.down
        doc = store.register('synthetic', 'synthetic.md', SOURCE.encode(), SOURCE)
        for p in patches: p.start()
        try:
            if kind == 'question':
                fake = workflow.Workflow()
                fake.store, fake.doc = store, doc
                workflow.Workflow.approve(fake, workflow.Workflow.extract(fake))
                job = service.ask({'question': QUESTION, 'agent': agent, 'model': model})
                queue.drain()
                stored = store.question(job['id'])
            else:
                if kind == 'extract': approved_dictionary(store, doc['id'])
                current = store.document(doc['id'])
                run = service.start(doc['id'], agent, model, kind, current['version'], current['generation'])
                queue.drain()
                stored = store.run(run['id'])
        finally:
            for p in patches: p.stop()
        usage = service.with_usage('questions' if kind == 'question' else 'runs', stored)['usage']
        stage = 'question' if kind == 'question' else kind
        group = next(g['usage'] for g in service.usage_report()['groups'] if g['agent'] == agent and g['stage'] == stage)
        return root, stored, usage, group

    def test_a_call_that_never_started_is_listed_but_not_counted_on_every_transport_and_job_kind(self):
        for name in ('local_cli', 'persistent_claude', 'openrouter', 'ollama'):
            for kind in ('question', 'ontology', 'extract'):
                with self.subTest(transport=name, kind=kind):
                    root, stored, usage, group = self.job(name, kind, started=False)
                    self.assertEqual(stored['status'], 'error')
                    self.assertEqual((usage['calls'], usage['not_started_calls'], usage['unknown_call_count_jobs'], usage['incomplete_calls']),
                                     (0, 1, 0, 0))
                    self.assertEqual([(c['status'], c['call_started']) for c in usage['details']], [('not_started', False)])
                    self.assertIn(NS_WARNING, usage['details'][0]['warnings'])
                    self.assertEqual(group, {k: v for k, v in usage.items() if k != 'details'})
                    if kind != 'question':
                        self.assertIn('새 호출 시도 1회', stored['detail'])
                        marker = 'dictionary-progress.json' if kind == 'ontology' else 'extraction-progress.json'
                        self.assertEqual(json.loads((root / 'runs' / stored['id'] / marker).read_text())['calls_started'], 1)

    def test_a_call_that_failed_after_it_started_is_counted_on_every_transport_and_job_kind(self):
        for name in ('local_cli', 'persistent_claude', 'openrouter', 'ollama'):
            for kind in ('question', 'ontology', 'extract'):
                with self.subTest(transport=name, kind=kind):
                    _, stored, usage, _ = self.job(name, kind, started=True)
                    self.assertEqual(stored['status'], 'error')
                    self.assertEqual((usage['calls'], usage['not_started_calls'], usage['unknown_call_count_jobs']), (1, 0, 0))
                    self.assertEqual([c['call_started'] for c in usage['details']], [True])
                    self.assertNotEqual(usage['details'][0]['status'], 'not_started')


class OpenRouterBoundary(unittest.TestCase):
    """T2b/T7/T7b: OpenRouter's not-started evidence is decided at the first HTTP request byte. The run's error marker records one
    call attempt, as a run with this one call writes it."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'runs' / KEY / '1'
        self.events = []
        self.folder.mkdir(parents=True)
        atomic_json(self.folder.parent / 'extraction-progress.json', {'schema_version': 1, 'pipeline': 'approved-extraction-v1',
                                                                      'run_id': KEY, 'status': 'error', 'calls_started': 1})

    def call(self, connect):
        holder, real_init = {}, openrouter._HTTPS.__init__

        def init(conn, call):
            holder['call'] = call
            real_init(conn, call)

        def recording_connect(conn):
            self.events.append(('connect', holder['call'].sent))
            connect(conn, holder['call'])
        cli = CLI(http_adapter=openrouter.OpenRouter(), credentials=TransportCredentials('sk-or-synthetic-880'), options=ExecutionOptions(timeout=1))
        with patch.object(openrouter._HTTPS, '__init__', init), patch.object(http.client.HTTPSConnection, 'connect', recording_connect):
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                cli.complete('openrouter', 'vendor/model', 'hello', self.folder)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and json.loads((self.folder / 'run.json').read_text()).get('dispatcher_finished') is not True:
            time.sleep(0.05)
        return json.loads((self.folder / 'run.json').read_text())

    def socket(self, call, after=lambda: None):
        def send():
            self.events.append(('sendall', call.sent))
            after()
        return FakeSocket(send)

    def test_sent_is_false_at_connect_and_true_at_the_first_request_byte(self):
        def connect(conn, call): conn.sock = self.socket(call, reset)
        meta = self.call(connect)
        self.assertEqual(self.events[:2], [('connect', False), ('sendall', True)])
        self.assertEqual((meta['transport_status'], meta['transmission_state']), ('error', 'sent_unknown'))
        self.assertEqual(counted(self.root, owner('openrouter', 'vendor/model'))[:2], (1, 0))

    def test_a_refused_connection_is_not_started(self):
        def connect(conn, call): raise ConnectionRefusedError('synthetic refusal')
        meta = self.call(connect)
        self.assertEqual((meta['transport_status'], meta['transmission_state']), ('not_started', 'not_sent'))
        self.assertEqual(counted(self.root, owner('openrouter', 'vendor/model')), (0, 1, ['not_started']))

    def test_a_connect_past_the_deadline_sends_no_byte_and_is_not_started(self):
        def connect(conn, call):
            time.sleep(1.5)
            conn.sock = self.socket(call)
        meta = self.call(connect)
        self.assertEqual([e for e in self.events if e[0] == 'sendall'], [])
        self.assertEqual((meta['transport_status'], meta['transmission_state']), ('timeout', 'not_sent'))
        self.assertEqual(counted(self.root, owner('openrouter', 'vendor/model')), (0, 1, ['not_started']))

    def test_request_bytes_sent_then_the_deadline_is_counted(self):
        def connect(conn, call): conn.sock = self.socket(call, lambda: time.sleep(1.6))
        meta = self.call(connect)
        self.assertIn(('sendall', True), self.events)
        self.assertEqual((meta['transport_status'], meta['transmission_state']), ('timeout', 'sent_unknown'))
        self.assertEqual(counted(self.root, owner('openrouter', 'vendor/model'))[:2], (1, 0))

    def test_a_record_left_at_its_first_write_is_counted(self):
        def connect(conn, call): raise ConnectionRefusedError('synthetic refusal')
        meta = self.call(connect)
        atomic_json(self.folder / 'run.json', meta | {'status': 'running', 'transport_status': 'running'})
        self.assertEqual(counted(self.root, owner('openrouter', 'vendor/model'))[:2], (1, 0))

    def test_a_not_sent_call_without_an_accepted_usage_record_is_counted(self):
        def connect(conn, call): raise ConnectionRefusedError('synthetic refusal')
        meta = self.call(connect)
        self.assertEqual((meta['transport_status'], meta['transmission_state']), ('not_started', 'not_sent'))
        assert_unaccepted_usage_is_counted(self, owner('openrouter', 'vendor/model'))


class Ollama(unittest.TestCase):
    """T8: an Ollama call is not started until just before its generate request; /api/tags alone is not a generate request."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'runs' / KEY / '1'

    def call(self, request):
        with patch('verinote.ollama.request', side_effect=request), self.assertRaises((OSError, ValueError, RuntimeError)):
            Job(CLI(persistent=False)).complete('ollama', OLLAMA_MODEL, 'hello', self.folder)
        return [json.loads((self.folder / name).read_text()) for name in ('run.json', 'usage.json')]

    def test_a_refused_model_list_is_not_started(self):
        def request(path, payload=None, **kwargs): raise ConnectionRefusedError('synthetic: Ollama is down')
        meta, usage = self.call(request)
        self.assertEqual((meta['generate_requested'], usage['generate_requested'], meta['status'], usage['status']), (False, False, 'error', 'error'))
        self.assertEqual(counted(self.root, owner('ollama', OLLAMA_MODEL)), (0, 1, ['not_started']))

    def test_a_failed_generate_request_is_counted(self):
        def request(path, payload=None, **kwargs):
            if path == '/api/tags': return CATALOG
            raise ConnectionResetError('synthetic generate failure')
        meta, usage = self.call(request)
        self.assertEqual((meta['generate_requested'], usage['generate_requested']), (True, True))
        self.assertEqual(counted(self.root, owner('ollama', OLLAMA_MODEL)), (1, 0, ['error']))

    def test_a_record_left_running_is_counted(self):
        def request(path, payload=None, **kwargs): raise ConnectionRefusedError('synthetic: Ollama is down')
        meta, usage = self.call(request)
        atomic_json(self.folder / 'run.json', meta | {'status': 'running'})
        atomic_json(self.folder / 'usage.json', usage | {'status': 'running'})
        self.assertEqual(counted(self.root, owner('ollama', OLLAMA_MODEL))[:2], (1, 0))

    def test_a_record_written_before_the_field_existed_is_counted(self):
        def request(path, payload=None, **kwargs): raise ConnectionRefusedError('synthetic: Ollama is down')
        meta, usage = self.call(request)
        atomic_json(self.folder / 'run.json', {k: v for k, v in meta.items() if k != 'generate_requested'})
        atomic_json(self.folder / 'usage.json', {k: v for k, v in usage.items() if k != 'generate_requested'})
        self.assertEqual(counted(self.root, owner('ollama', OLLAMA_MODEL)), (1, 0, ['error']))

    def test_a_call_before_generate_without_an_accepted_usage_record_is_counted(self):
        def request(path, payload=None, **kwargs): raise ConnectionRefusedError('synthetic: Ollama is down')
        meta, _ = self.call(request)
        self.assertEqual((meta['generate_requested'], meta['status']), (False, 'error'))
        assert_unaccepted_usage_is_counted(self, owner('ollama', OLLAMA_MODEL))


class LocalProcess(unittest.TestCase):
    """T6/T6b/T6c/T9: the local CLI and persistent Claude transports record not_started only when the call was given no CLI process."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.marker = self.root / 'ran'
        self.running = script(self.root, 'runs.sh', f'#!/bin/sh\necho x >> {self.marker}\nsleep 5\n')
        self.failing = script(self.root, 'fails.sh', f'#!/bin/sh\necho x >> {self.marker}\nexit 3\n')
        self.not_executable = script(self.root, 'nox.sh', f'#!/bin/sh\necho x >> {self.marker}\n', 0o644)
        self.folder = self.root / 'runs' / KEY / '1'

    def call(self, agent, binary, cli, *extra):
        popen, real = [], claude_worker.subprocess.Popen

        def spy(*args, **kwargs):
            result = real(*args, **kwargs)
            popen.append('returned')
            return result
        patches = [patch.object(claude_worker.subprocess, 'Popen', spy), patch('verinote.agents.executable', return_value=binary), *extra]
        for p in patches: p.start()
        try:
            with self.assertRaises(BaseException) as raised:
                cli.complete(agent, '', 'hello', self.folder)
        finally:
            for p in patches: p.stop()
        return raised.exception, popen

    def usage_status(self):
        return json.loads((self.folder / 'usage.json').read_text())['status']

    def test_a_local_cli_call_without_an_accepted_usage_record_is_counted(self):
        """The local CLI keeps its evidence in usage.json and does not compare it with run.json, so there is no mismatched case."""
        self.call('codex', None, CLI(persistent=False, timeout=5))
        self.assertEqual(self.usage_status(), 'not_started')
        assert_unaccepted_usage_is_counted(self, owner('codex'), mismatched=False)

    def test_local_cli_missing_or_not_executable_is_not_started_and_a_cli_that_ran_is_counted(self):
        for binary, status, ran in ((None, 'not_started', False), (self.not_executable, 'not_started', False), (self.failing, 'error', True)):
            with self.subTest(binary=binary and Path(binary).name):
                if self.marker.exists(): self.marker.unlink()
                self.call('codex', binary, CLI(persistent=False, timeout=5))
                self.assertEqual((self.usage_status(), self.marker.exists()), (status, ran))
                self.assertEqual(counted(self.root, owner('codex'))[:2], (0, 1) if status == 'not_started' else (1, 0))

    def test_persistent_thread_start_failure_after_spawn_is_counted(self):
        class Boom(claude_worker.threading.Thread):
            def start(self): raise RuntimeError('synthetic thread start failure')
        error, popen = self.call('claude', self.running, CLI(timeout=1), patch.object(claude_worker.threading, 'Thread', Boom))
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(popen, ['returned'])
        self.assertEqual(self.usage_status(), 'error')
        self.assertEqual(counted(self.root, owner('claude'))[:2], (1, 0))

    def test_persistent_spawn_refused_by_the_os_is_not_started(self):
        error, popen = self.call('claude', self.not_executable, CLI(timeout=1))
        self.assertIsInstance(error, PermissionError)
        self.assertEqual((popen, self.marker.exists(), self.usage_status()), ([], False, 'not_started'))
        self.assertEqual(counted(self.root, owner('claude'))[:2], (0, 1))

    def assert_wait_timeout_not_started(self, cli, *extra):
        error, popen = self.call('claude', self.running, cli, *extra)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual((popen, self.marker.exists(), self.usage_status()), ([], False, 'not_started'))
        timing = read_cli_timing(self.root / 'runs' / KEY, '1')
        self.assertEqual((timing['available'], timing['status'], timing['error_code']), (True, 'not_started', 'process'))
        self.assertEqual(counted(self.root, owner('claude'))[:2], (0, 1))

    def test_persistent_manager_lock_wait_timeout_is_not_started(self):
        cli = CLI(timeout=1)
        cli._claude.lock.acquire()
        try:
            self.assert_wait_timeout_not_started(cli)
        finally:
            cli._claude.lock.release()

    def test_persistent_extraction_pool_wait_timeout_is_not_started(self):
        cli = CLI(timeout=1)
        pool = claude_worker.ExtractionPool(self.running, '', 1)
        cli._claude = pool
        pool.leased.add(0)
        try:
            self.assert_wait_timeout_not_started(cli, patch.object(agents.CLI, '_pinned_claude_binary', self.running, create=True))
        finally:
            pool.leased.discard(0)
            pool.close()


def not_started_call(folder, agent):
    """What a local CLI transport leaves when it could not start the process: a not_started usage log, then the failure."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    atomic_json(Path(folder) / 'usage.json', record(agent, status='not_started', complete=False, tokens=dict.fromkeys(record()['tokens'])))
    raise FileNotFoundError('synthetic: CLI executable missing')


class Logging(Sequential):
    """Sequential that writes a success log for every call and makes the call at folder_name (a chunk number or quote-repair) not start."""
    def __init__(self, fn, folder_name, agent='copilot'):
        super().__init__(fn)
        self.folder_name, self.agent = folder_name, agent

    def complete(self, agent, model, prompt, folder, *, phase=None):
        if Path(folder).name == self.folder_name:
            self.calls.append(number_of(Path(folder)))
            not_started_call(folder, self.agent)
        Path(folder).mkdir(parents=True, exist_ok=True)
        atomic_json(Path(folder) / 'usage.json', record(self.agent))
        return super().complete(agent, model, prompt, folder, phase=phase)


class LoggingParallel(Parallel):
    def __init__(self, fn, folder_name, capacity=2):
        super().__init__(fn, capacity)
        self.folder_name = folder_name

    def complete(self, agent, model, prompt, folder):
        if Path(folder).name == self.folder_name:
            with self.lock: self.calls.append(number_of(Path(folder)))
            not_started_call(folder, 'claude')
        Path(folder).mkdir(parents=True, exist_ok=True)
        atomic_json(Path(folder) / 'usage.json', record('claude'))
        return super().complete(agent, model, prompt, folder)


class Attempts(RunBase):
    """T4/T4b: the extraction marker and detail count attempts; usage counts the started calls only."""
    def run_with(self, caller, parallel=False):
        doc = self.document(1)
        version, generation = self.current(doc)
        service = Service(self.store, self.root, caller, **({'extraction_caller': caller} if parallel else {}))
        self.addCleanup(service.close)
        run = service.start(doc['id'], 'claude' if parallel else 'copilot', 'model' if parallel else 'chosen', expected_version=version,
                            expected_generation=generation, **({'extraction_concurrency': 2} if parallel else {}))
        service.pool.shutdown(wait=True)
        stored = self.store.run(run['id'])
        marker = json.loads((self.root / 'runs' / run['id'] / 'extraction-progress.json').read_text())
        usage = service.with_usage('runs', stored)['usage']
        return service, stored, marker['calls_started'], (usage['calls'], usage['not_started_calls'], usage['unknown_call_count_jobs'])

    def test_sequential_parallel_and_quote_repair_runs_count_the_not_started_attempt(self):
        cases = (('sequential', lambda: Logging(lambda i, r: extraction_response(), '1'), False, 1, (0, 1, 0)),
                 ('parallel', lambda: LoggingParallel(lambda i, r: extraction_response(), '1'), True, 1, (0, 1, 0)),
                 ('quote_repair', lambda: Logging(lambda i, r: broken(), 'quote-repair'), False, 2, (1, 1, 0)))
        for label, caller, parallel, attempts, usage in cases:
            with self.subTest(label):
                _, stored, calls_started, counts = self.run_with(caller(), parallel)
                self.assertEqual(stored['status'], 'error')
                self.assertIn(f'새 호출 시도 {attempts}회', stored['detail'])
                self.assertEqual((calls_started, counts), (attempts, usage))

    def test_a_not_started_only_run_whose_logs_are_gone_reads_call_count_unknown(self):
        service, stored, calls_started, counts = self.run_with(Logging(lambda i, r: extraction_response(), '1'))
        self.assertEqual((calls_started, counts), (1, (0, 1, 0)))
        (self.root / 'runs' / stored['id'] / '1' / 'usage.json').unlink()
        usage = service.with_usage('runs', self.store.run(stored['id']))['usage']
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls']), (1, 1, 0))


class Rendering(unittest.TestCase):
    """T3/T10: the headline from app.js renderUsage, with #879's unknown entry kept ahead of the not-started rule."""
    def test_an_ended_question_with_a_not_started_log_and_a_logless_entry_reads_unknown_and_not_started(self):
        case = workflow.Workflow()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.approve(case.extract())
        store, root = case.store, case.root.resolve()
        job = store.create_question(QUESTION, 'codex', '', store.snapshot()['id'])
        folder = root / 'questions' / job['id'] / 'llm-1'
        folder.mkdir(parents=True)
        atomic_json(folder / 'usage.json', record('codex', status='not_started', complete=False, tokens=dict.fromkeys(record()['tokens'])))
        store.save_question(job | {'status': 'running', 'call_manifest': [
            {'path': 'llm-1', 'stage': 'question', 'round': 1, 'status': 'failed'},
            {'path': 'context-1-1', 'stage': 'analysis_leaf', 'round': 2, 'status': 'dispatched'}]})
        store.recover()
        service = Service(store, root)
        self.addCleanup(service.close)
        usage = service.with_usage('questions', store.question(job['id']))['usage']
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['incomplete_calls'], usage['not_started_calls']), (1, 1, 1, 1))
        self.assertIn('<p>확인된 호출 0회 · 호출 횟수 미확인 작업 1개 · 불완전 기록 1개 · 시작하지 않은 호출 1개</p>', render_usage(usage))

    def test_the_not_started_clause_shows_only_when_there_is_one(self):
        base = summarize([])
        self.assertIn('<p>확인된 호출 0회 · 시작하지 않은 호출 1개</p>', render_usage(base | {'not_started_calls': 1}))
        html = render_usage(base)
        self.assertIn('<p>확인된 호출 0회</p>', html)
        self.assertNotIn('<p>확인된 호출 0회 · 시작하지 않은', html)
        self.assertIn('시작하지 않은 호출은 모델 요청을 보내기 전에 실패한 호출', html)


if __name__ == '__main__':
    unittest.main()
