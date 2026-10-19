"""Offline checked-envelope accounting, recording and public consumption."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from verinote.agents import CLI
from verinote.http_records import RecordConflict, update_caller_validation
from verinote.transports import Credentials, HTTPResult, SafeCallResult
from verinote.usage import decode_http, empty_usage, calculate, job_calls, summarize, validate_rates

FIXTURE = json.loads((Path(__file__).parent/'fixtures/usage/openrouter.json').read_text())
RATE = dict(model='vendor/actual', input=2, output=10, cache_read=.2, cache_write=2.5,
            credit_unit='', usd_per_credit=None)


def envelope(usage=None, *, status='success', code=None, verified=True, call_id='a'*32, payload=None):
    meta = {'usage': copy.deepcopy(FIXTURE['usage'] if usage is None else usage),
            'resolved_model': 'vendor/actual', 'generation_id': 'gen-synthetic_usage_706',
            'http_status': 200 if status == 'success' else None,
            'transmission_state': 'received' if verified else 'sent_unknown',
            'validation_status': 'failed' if code else 'valid'}
    if code:
        meta['error_code'] = code
    return SafeCallResult(call_id, status, meta, verified, payload)


class Fake:
    def __init__(self, *, code=None, status='success', verified=True, response='{"ok":true}', usage=None):
        self.code, self.status, self.verified, self.response, self.usage = code, status, verified, response, usage
        self.calls = 0

    def ready(self, credentials):
        return True

    def complete(self, *args, **kwargs):
        self.calls += 1
        return HTTPResult(envelope(self.usage, status=self.status, code=self.code, verified=self.verified,
                                   call_id=kwargs['call_id'], payload=FIXTURE if self.verified else None),
                          self.response if not self.code else None, self.code)


class Usage(unittest.TestCase):
    def test_envelope_mapping_credits_and_rate_basis(self):
        value = decode_http(envelope())
        value.update(requested_model='vendor/request', rate_snapshot=RATE)
        calculate(value)
        self.assertEqual(value['tokens'], dict(input=100, output=20, cache_read=30, cache_write=10, reasoning=5))
        self.assertEqual(value['reported_credits'], .25)
        self.assertEqual(value['credit_unit'], 'OpenRouter credits')
        self.assertIsNone(value['reported_usd'])
        self.assertAlmostEqual(value['estimated_usd'], (60*2+20*10+30*.2+10*2.5)/1e6)
        self.assertEqual(value['rate_basis'], 'resolved_model')
        self.assertTrue(value['complete'])
        self.assertEqual(validate_rates({'openrouter': RATE}), {'openrouter': RATE})
        totals = summarize([value])
        self.assertIsNone(totals['reported_usd']['value'])
        self.assertEqual(totals['credits'], [{'unit': 'OpenRouter credits', 'provenance': 'reported', 'value': .25, 'known_calls': 1}])

    def test_missing_partial_zero_invalid_and_subset_contradictions(self):
        for raw in ({}, {'prompt_tokens': 1}, {'prompt_tokens': 0, 'completion_tokens': 0}):
            value = decode_http(envelope(raw))
            self.assertFalse(value['complete']); self.assertIsNone(value['tokens']['cache_read'])
            self.assertIsNone(value['reported_credits'])
        zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0,
                'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                'completion_tokens_details': {'reasoning_tokens': 0}}
        value = decode_http(envelope(zero)); self.assertTrue(value['complete'])
        self.assertEqual(value['tokens'], dict.fromkeys(value['tokens'], 0)); self.assertEqual(value['reported_credits'], 0)
        for invalid in (True, -1, .5, float('nan'), float('inf'), 10**400):
            raw = copy.deepcopy(FIXTURE['usage']); raw['prompt_tokens'] = invalid; raw['cost'] = invalid
            value = decode_http(envelope(raw)); self.assertIsNone(value['tokens']['input']); self.assertFalse(value['complete'])
            if invalid != .5:
                self.assertIsNone(value['reported_credits'])
        for field in ('cached_tokens', 'cache_write_tokens'):
            raw = copy.deepcopy(FIXTURE['usage']); raw['prompt_tokens_details'][field] = 101
            value = decode_http(envelope(raw)); self.assertIsNone(value['tokens']['input']); self.assertFalse(value['complete'])
            self.assertEqual(value['reported_credits'], .25)
        for reasoning in (21, True, -1):
            raw = copy.deepcopy(FIXTURE['usage']); raw['completion_tokens_details']['reasoning_tokens'] = reasoning
            value = decode_http(envelope(raw)); value['rate_snapshot'] = RATE; calculate(value)
            self.assertFalse(value['complete']); self.assertIsNone(value['estimated_usd'])
        value = decode_http(envelope(status='timeout', verified=False))
        self.assertTrue(all(v is None for v in value['tokens'].values()))
        self.assertIsNone(value['reported_credits']); self.assertFalse(value['complete'])

    def test_checked_payload_fallback_preserves_independent_cost(self):
        payload = copy.deepcopy(FIXTURE)
        payload['usage']['prompt_tokens'] = -1
        meta = {'validation_status': 'failed', 'resolved_model': 'vendor/actual'}
        event = SafeCallResult('a'*32, 'success', meta, True, payload)
        value = decode_http(event)
        self.assertIsNone(value['tokens']['input'])
        self.assertEqual(value['tokens']['output'], 20)
        self.assertEqual(value['reported_credits'], .25)
        self.assertFalse(value['complete'])
        self.assertIsNone(value['estimated_usd'])
        # Neither this fake answer nor another nested content object is evidence.
        payload = {'choices': FIXTURE['choices']}
        event = SafeCallResult('a'*32, 'success', {}, True, payload)
        self.assertTrue(all(v is None for v in decode_http(event)['tokens'].values()))



class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.owner = {'id': 'b'*32, 'agent': 'openrouter', 'model': 'vendor/request', 'status': 'success', 'stage': 'extract'}
        self.folder = self.root/'runs'/self.owner['id']/'1'
        self.events = []
        self.adapter = Fake()
        self.cli = CLI(http_adapter=self.adapter, credentials=Credentials('synthetic-key-706'), sink=self.events.append,
                       rates=lambda: {'openrouter': RATE})

    def tearDown(self):
        self.tmp.cleanup()

    def run_call(self):
        return self.cli.complete('openrouter', 'vendor/request', 'PRIVATE PROMPT', self.folder, phase='ontology')

    def read(self, name):
        return json.loads((self.folder/name).read_text())

    def test_records_allowlist_validation_reconstruction_and_no_duplicate(self):
        self.assertEqual(self.run_call(), {'ok': True})
        call_id = self.events[0].call_id
        run, usage = self.read('run.json'), self.read('usage.json')
        self.assertEqual(run['transport'], 'http'); self.assertEqual(run['phase'], 'ontology')
        self.assertEqual(run['timeout_seconds'], 600); self.assertEqual(run['max_tokens'], 16384)
        self.assertEqual(run['requested_model'], 'vendor/request'); self.assertEqual(run['resolved_model'], 'vendor/actual')
        self.assertNotIn('returncode', run); self.assertNotIn('prompt', run)
        self.assertEqual(self.read('response.json'), FIXTURE)
        self.assertEqual(run['caller_validation_status'], 'pending')
        self.assertTrue(update_caller_validation(self.folder, call_id, 'failed'))
        self.assertTrue(CLI().update_caller_validation(self.folder, call_id, 'failed'))
        self.assertFalse(update_caller_validation(self.folder, 'c'*32, 'success'))
        with self.assertRaises(RecordConflict):
            update_caller_validation(self.folder, call_id, 'success')
        self.assertEqual(self.read('usage.json'), usage)
        self.assertEqual(self.read('run.json')['transport_status'], 'success')
        calls = job_calls(self.root, 'runs', self.owner)
        self.assertEqual(len(calls), 1); self.assertEqual(calls[0]['caller_validation_status'], 'failed')
        self.assertEqual(calls[0]['provider_call_id'], call_id); self.assertEqual(calls[0]['status'], 'error')
        self.assertEqual(calls[0]['tokens']['input'], 100)
        public = json.dumps(calls)
        for secret in ('PRIVATE PROMPT', 'synthetic-key-706', 'choices', 'prompt_sha256', 'Authorization'):
            self.assertNotIn(secret, public)
        with self.assertRaises(RecordConflict):
            self.run_call()
        self.assertEqual(self.adapter.calls, 1)

    def test_failures_preserve_confirmed_usage_and_unknown_timeout(self):
        for index, code in enumerate(('content', 'refusal', 'finish', 'timeout'), 1):
            self.folder = self.folder.parent/str(index)
            self.adapter.code = code
            self.adapter.status = 'timeout' if code == 'timeout' else 'success'
            self.adapter.verified = code != 'timeout'
            with self.assertRaises((ValueError, TimeoutError)):
                self.run_call()
            usage, run = self.read('usage.json'), self.read('run.json')
            self.assertEqual(run['caller_validation_status'], 'not_applicable')
            self.assertEqual(usage['tokens']['input'], None if code == 'timeout' else 100)
            self.assertEqual(usage['reported_credits'], None if code == 'timeout' else .25)
            self.assertEqual((self.folder/'response.json').exists(), code != 'timeout')
            self.assertEqual(run['transport_status'], self.adapter.status)

    def test_optional_writes_fail_without_changing_primary_outcome(self):
        from verinote import http_records
        original = http_records.write_record
        for index, filename in enumerate(('run.json', 'usage.json', 'response.json', 'all'), 1):
            self.folder = self.folder.parent/str(index)
            def fail(folder, name, value, limit=256*1024):
                if filename in (name, 'all'):
                    raise OSError('untrusted error text synthetic-key-706')
                return original(folder, name, value, limit)
            with patch('verinote.http_records.write_record', side_effect=fail):
                self.assertEqual(self.run_call(), {'ok': True})
            if filename in ('usage.json', 'response.json'):
                self.assertIn('usage_warning', self.read('run.json'))
            if filename == 'run.json':
                calls = job_calls(self.root, 'runs', self.owner)
                entry = next(c for c in calls if c['call_id'].endswith('/'+str(index)))
                self.assertIsNone(entry['tokens']['input'])
        self.folder = self.folder.parent/'5'; self.adapter.code = 'content'
        with patch('verinote.http_records.write_record', side_effect=OSError('synthetic-key-706')):
            with self.assertRaisesRegex(ValueError, 'JSON') as caught:
                self.run_call()
        self.assertNotIn('synthetic-key-706', str(caught.exception))

    def test_symlinks_context_secrets_and_identity_mismatch(self):
        outside = self.root/'outside'; outside.mkdir()
        self.folder.parent.mkdir(parents=True); self.folder.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_call(), {'ok': True})
        self.assertEqual(list(outside.iterdir()), [])
        self.folder.unlink(); self.folder.mkdir()
        (self.folder/'usage.json').symlink_to(outside/'usage.json')
        with self.assertRaises(RecordConflict):
            self.run_call()
        (self.folder/'usage.json').unlink()
        self.assertEqual(self.run_call(), {'ok': True})
        data = self.read('usage.json'); data['provider_call_id'] = 'f'*32
        (self.folder/'usage.json').write_text(json.dumps(data))
        self.assertIsNone(job_calls(self.root, 'runs', self.owner)[0]['tokens']['input'])

    def test_real_adapter_blocks_context_secret_before_persistence(self):
        from verinote.openrouter import OpenRouter
        from verinote.store import digest
        key = 'synthetic-key-706'
        self.cli.http_adapter = OpenRouter()
        self.cli.rates = lambda: {'openrouter': RATE | {'model': key}}
        with patch('verinote.openrouter._HTTPS') as connection:
            with self.assertRaises(ValueError):
                self.cli.complete('openrouter', key, key, self.folder)
            connection.assert_not_called()
        run = self.read('run.json')
        self.assertIsNone(run['requested_model']); self.assertIsNone(run['prompt_sha256'])
        self.assertIsNone(self.read('usage.json')['rate_snapshot'])
        for path in self.folder.iterdir():
            self.assertNotIn(key, path.read_text())
            self.assertNotIn(digest(key.encode()), path.read_text())
        self.assertFalse((self.folder/'response.json').exists())

    def test_fractional_timeout_remains_exact_in_public_metadata(self):
        self.cli.timeout = .1
        self.run_call()
        self.assertEqual(self.read('run.json')['timeout_seconds'], .1)
        self.assertEqual(job_calls(self.root, 'runs', self.owner)[0]['timeout_seconds'], .1)

    def test_caller_notification_waits_for_dispatcher_return(self):
        early = []
        def sink(event):
            self.events.append(event)
            early.append(update_caller_validation(self.folder, event.call_id, 'success'))
        self.cli.sink = sink
        self.run_call()
        self.assertEqual(early, [False])
        self.assertTrue(update_caller_validation(self.folder, self.events[0].call_id, 'success'))
        self.assertFalse(update_caller_validation(self.root/'missing', 'a'*32, 'success'))
        self.assertFalse((self.root/'missing').exists())

    def test_partial_final_run_write_is_not_a_fresh_or_complete_call(self):
        from verinote.http_records import write_record
        def interrupt(folder, name, value, limit=256*1024):
            if name == 'run.json' and value.get('finished_at'):
                raise OSError('interrupted')
            return write_record(folder, name, value, limit)
        with patch('verinote.http_records.write_record', side_effect=interrupt):
            self.assertEqual(self.run_call(), {'ok': True})
        calls = job_calls(self.root, 'runs', self.owner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['tokens']['input'], 100)
        self.assertFalse(calls[0]['complete'])
        self.assertFalse(update_caller_validation(self.folder, self.events[0].call_id, 'success'))
        with self.assertRaises(RecordConflict):
            self.run_call()

    def test_discovery_rate_snapshots_and_fully_resumed_run(self):
        rates = copy.deepcopy(RATE)
        self.cli.rates = lambda: {'openrouter': rates}
        self.run_call()
        rates['input'] = 999
        self.assertEqual(self.read('usage.json')['rate_snapshot']['input'], 2)
        self.folder = self.folder.parent/'2'/'quote-repair'
        self.run_call()
        self.assertEqual(self.read('usage.json')['rate_snapshot']['input'], 999)
        calls = job_calls(self.root, 'runs', self.owner)
        self.assertEqual(len(calls), 2)
        self.assertEqual({c['stage'] for c in calls}, {'extract', 'quote_repair'})
        question = self.owner | {'id': 'c'*32}
        self.folder = self.root/'questions'/question['id']/'llm-1'
        self.run_call()
        self.assertEqual(len(job_calls(self.root, 'questions', question)), 1)
        resumed = self.owner | {'id': 'd'*32, 'stage': 'ontology'}
        folder = self.root/'runs'/resumed['id']; folder.mkdir()
        (folder/'dictionary-progress.json').write_text(json.dumps({
            'schema_version': 1, 'pipeline': 'source-spans-v1', 'run_id': resumed['id'], 'status': 'complete',
            'calls_started': 0, 'resumed_from': self.owner['id'], 'total_chars': 10, 'resumed_chars': 10, 'completed_chars': 10}))
        self.assertEqual(job_calls(self.root, 'runs', resumed), [])

    def test_concurrent_terminal_updates_one_wins(self):
        self.run_call(); call_id = self.events[0].call_id
        barrier, results = threading.Barrier(2), []
        def update(status):
            barrier.wait()
            try:
                results.append(update_caller_validation(self.folder, call_id, status))
            except RecordConflict:
                results.append('conflict')
        threads = [threading.Thread(target=update, args=(status,)) for status in ('success', 'failed')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results, [True, 'conflict'])
        self.assertEqual(len(job_calls(self.root, 'runs', self.owner)), 1)
