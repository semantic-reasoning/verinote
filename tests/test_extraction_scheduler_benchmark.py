"""The production scheduler and budget wrapper with local protocol subprocesses only."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_claude_worker import FAKE

from verinote.documents import chunks
from verinote.extraction_benchmark import (
    Budget,
    BudgetCaller,
    Limits,
    ideal_outputs,
    load_case,
    measure,
)
from verinote.extraction_transport import ExtractionTransport
from verinote.ontology import dictionary_context


class SchedulerBenchmark(unittest.TestCase):
    def test_retry_cleanup_owner_is_idempotent_and_views_do_not_own(self):
        caller = Mock()
        owner = BudgetCaller(caller, Budget(Limits()), owns_caller=True, retry_close=True)
        view = owner.limited_timeout('claude', 1)
        view.close()
        caller.limited_timeout.return_value.close.assert_not_called()
        self.assertFalse(view.retry_close)
        owner.close(); owner.close()
        caller.close.assert_called_once()

    def policy_batch(self, root, *, cleanup_failure=False, requests=40):
        fixtures = {}
        oracle = {}
        for name, count in (('three', 3), ('four', 4), ('three_again', 3)):
            case = copy.deepcopy(load_case('short')); case['case_id'] = name
            case['source'] = case['source'].ljust(8000)+''.join(
                f'No approved direct relation in block {i}.\n'.ljust(8000) if i<count else f'No approved direct relation in block {i}.'
                for i in range(2, count+1))
            case['source_sha256'] = hashlib.sha256(case['source'].encode()).hexdigest()
            fixtures[name] = case
            for (offset, text), output in zip(chunks(case['source']), ideal_outputs(case, contract_version=2), strict=True):
                key = json.dumps({'text': text, 'offset': offset, 'dictionary': dictionary_context(case['dictionary'])}, sort_keys=True)
                oracle[key] = output['output']
        (root/'oracle.json').write_text(json.dumps(oracle))
        script = root/'fake.py'
        script.write_text(FAKE.replace("    body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)",
            "    payload=json.loads(text.split('입력(JSON):\\n')[1])\n    key=json.dumps(dict(text=payload['text'],offset=payload['offset'],dictionary=payload['approved_dictionary']['payload']),sort_keys=True)\n    body=json.loads((root/'oracle.json').read_text())[key]"))
        owners = []
        def owner(spec):
            self.assertTrue(all(o.closed for o in owners))
            value = ExtractionTransport(spec)
            if cleanup_failure and not owners:
                close = value.close
                attempts = []
                value.close_attempts = attempts
                def fail_once():
                    attempts.append(True)
                    if len(attempts)==1: raise OSError('synthetic cleanup failure')
                    close()
                    if cleanup_failure == 'always': raise OSError('synthetic final cleanup failure')
                value.close = fail_once
            owners.append(value)
            return value
        with patch('verinote.agents.executable', return_value=sys.executable), patch('verinote.extraction_transport.executable', return_value=sys.executable), patch('verinote.claude_worker.command', side_effect=lambda b, m: [b, str(script), str(root), m]), patch('verinote.extraction_transport.ExtractionTransport', side_effect=owner), patch('verinote.extraction_benchmark.load_case', side_effect=lambda key: fixtures[key]):
            result = measure(root/'result', cases=list(fixtures), mode='cli', agent='claude', model='model',
                process_mode='batch', extraction_policy='chunk_threshold_v1', timeout=5,
                limits=Limits(requests, 1, 30, 100000, 'stop'))
        self.assertTrue(all(o.closed for o in owners))
        return result, owners

    def test_threshold_batch_replaces_1_2_1_only_after_close(self):
        with tempfile.TemporaryDirectory() as temp:
            result, owners = self.policy_batch(Path(temp).resolve())
        self.assertEqual(result['status'], 'completed', result['stop_reason'])
        self.assertEqual([o.identity.capacity for o in owners], [1, 2, 1])
        self.assertEqual([r['extraction_options']['original_chunk_count'] for r in result['repetitions']], [3, 4, 3])
        self.assertEqual([r['extraction_options']['concurrency'] for r in result['repetitions']], [1, 2, 1])
        self.assertEqual([a['owner_id'] for a in result['attempts']], [1]*3+[2]*4+[3]*3)
        self.assertTrue(all('owner_replacement_seconds' in r for r in result['repetitions'][1:]))
        self.assertEqual(result['budget']['requests_started'], 10)

    def test_threshold_batch_cleanup_failure_prevents_new_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            result, owners = self.policy_batch(Path(temp).resolve(), cleanup_failure=True)
        self.assertEqual(len(owners), 1)
        self.assertEqual(result['stop_reason'], 'cleanup_error')
        self.assertEqual(result['cleanup_errors'], 1)
        self.assertEqual(result['budget']['requests_started'], 3)
        self.assertEqual(len(owners[0].close_attempts), 2)

    def test_threshold_final_cleanup_failure_is_reported_without_new_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            result, owners = self.policy_batch(Path(temp).resolve(), cleanup_failure='always')
        self.assertEqual(len(owners), 1)
        self.assertEqual(len(owners[0].close_attempts), 2)
        self.assertEqual(result['stop_reason'], 'cleanup_error')
        self.assertEqual(result['cleanup_errors'], 2)
        self.assertEqual(result['budget']['requests_started'], 3)

    def test_threshold_dispatches_respect_shared_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.policy_batch(Path(temp).resolve(), requests=4)
        self.assertEqual(result['status'], 'stopped')
        self.assertLessEqual(result['budget']['dispatch_authorized'], 4)
        self.assertEqual(result['budget']['pending'], 0)

    def test_same_scheduler_bounded_budget_and_owner_lifetimes(self):
        for mode in ('document', 'batch'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                case = load_case('long')
                (root/'outputs.json').write_text(json.dumps(ideal_outputs(case)))
                script = root/'fake.py'
                script.write_text(FAKE.replace("    body=dict(prompt=text,pid=os.getpid(),history=list(history),key_present='OPENROUTER_API_KEY' in os.environ)",
                    "    payload=json.loads(text.split('입력(JSON):\\n')[1])\n    time.sleep(.02)\n    body=json.loads((root/'outputs.json').read_text())[payload['offset']//8000]['output']"))
                with patch('verinote.agents.executable', return_value=sys.executable), patch('verinote.extraction_transport.executable', return_value=sys.executable), patch('verinote.claude_worker.command', side_effect=lambda b, m, script=script, root=root: [b, str(script), str(root), m]):
                    report = measure(root/'result', cases=('long',), mode='cli', agent='claude', model='model',
                                     process_mode=mode, extraction_concurrency=2, timeout=5,
                                     limits=Limits(40, 2, 30, 100000, 'stop'))
                self.assertEqual(report['status'], 'completed', report.get('stop_reason'))
                self.assertEqual(report['summary']['failed_repetitions'], 0, report['repetitions'])
                attempts = report['attempts']
                self.assertEqual(len(attempts), 2*len(ideal_outputs(case)))
                self.assertTrue(all(a['status']=='success' for a in attempts), attempts)
                self.assertTrue(all(r['document_timing']['available'] for r in report['repetitions']))
                transcript = [json.loads(line) for line in (root/'transcript').read_text().splitlines()]
                calls = [r for r in transcript if r['text']!='/clear']
                self.assertEqual(len(calls), len(attempts))
                self.assertTrue(all(r['history']==[] for r in calls))
                self.assertEqual(len({r['pid'] for r in calls}), 2 if mode=='batch' else 4)
                self.assertEqual(report['cleanup_errors'], 0)

    def test_rejects_unsupported_modes_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'out'
            for options in ({'mode':'fake'}, {'mode':'cli','process_mode':'oneshot'},
                            {'mode':'cli','process_mode':'batch','extraction_concurrency':True}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    measure(output, agent='claude', model='model', **({'extraction_concurrency':2}|options))
                self.assertFalse(output.exists())
