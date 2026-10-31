"""The production scheduler and budget wrapper with local protocol subprocesses only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_claude_worker import FAKE

from verinote.extraction_benchmark import Limits, ideal_outputs, load_case, measure


class SchedulerBenchmark(unittest.TestCase):
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
