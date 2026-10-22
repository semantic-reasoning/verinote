"""Budget observations preserve accounting and never create provider calls."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_http_records import Fake

from verinote.agents import CLI
from verinote.reasoning_diagnostics import non_reasoning_output, reasoning_diagnostic
from verinote.transports import Credentials, ExecutionOptions
from verinote.usage import empty_usage, job_calls, summarize

CALL = {'transport': 'http', 'adapter_contract_version': 2, 'max_tokens': 16384,
        'reasoning_max_tokens': 4096, 'answer_reserve_tokens': 8192,
        'tokens': {'output': 16384, 'reasoning': 14991}, 'finish_reason': 'length'}
USAGE = {'prompt_tokens': 15995, 'completion_tokens': 16384, 'total_tokens': 32379, 'cost': .01059125,
         'is_byok': False, 'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
         'completion_tokens_details': {'reasoning_tokens': 14991}}


class Diagnostics(unittest.TestCase):
    def test_observed_overrun_and_non_reasoning_output(self):
        d = reasoning_diagnostic(CALL)
        self.assertEqual((d['status'], d['excess_tokens'], d['non_reasoning_output']), ('exceeded', 10895, 1393))
        self.assertEqual((d['requested_reasoning'], d['answer_target'], d['reported_reasoning']), (4096, 8192, 14991))
        self.assertEqual(CALL['tokens']['reasoning'], 14991)

    def test_equal_below_zero_legacy_and_unknown_budget(self):
        for reasoning in (4096, 100, 0):
            d = reasoning_diagnostic(CALL | {'tokens': {'output': 16384, 'reasoning': reasoning}})
            self.assertEqual(d['status'], 'within_budget'); self.assertEqual(d['excess_tokens'], 0)
        zero = reasoning_diagnostic(CALL | {'tokens': {'output': 0, 'reasoning': 0}})
        self.assertEqual(zero['non_reasoning_output'], 0)
        for version in (None, 1, True):
            d = reasoning_diagnostic(CALL | {'adapter_contract_version': version})
            self.assertEqual(d['status'], 'not_applicable'); self.assertIsNone(d['requested_reasoning'])
            self.assertEqual(d['non_reasoning_output'], 1393)
        for override in ({'reasoning_max_tokens': True}, {'max_tokens': None}, {'answer_reserve_tokens': 0},
                         {'reasoning_max_tokens': 8192}, {'max_tokens': float('nan')}):
            self.assertEqual(reasoning_diagnostic(CALL | override)['status'], 'unknown')

    def test_missing_bad_or_contradictory_tokens_are_unknown(self):
        for invalid in (None, True, -1, .5, float('nan'), float('inf'), 10**16):
            for field in ('output', 'reasoning'):
                tokens = CALL['tokens'] | {field: invalid}
                self.assertIsNone(non_reasoning_output(tokens))
                self.assertEqual(reasoning_diagnostic(CALL | {'tokens': tokens})['status'], 'unknown')
        for tokens in (None, {}, {'output': 100, 'reasoning': 101}):
            self.assertIsNone(non_reasoning_output(tokens))
            self.assertEqual(reasoning_diagnostic(CALL | {'tokens': tokens})['status'], 'unknown')

    def test_partial_aggregates_compute_difference_per_call(self):
        calls = []
        for output, reasoning in ((100, 25), (900, None), (10, 11), (0, 0)):
            value = empty_usage('openrouter', 'fixture'); value['tokens'].update(output=output, reasoning=reasoning)
            calls.append(value)
        summary = summarize(calls)
        self.assertEqual(summary['non_reasoning_output'], {'value': 75, 'known_calls': 2})
        self.assertEqual(summary['tokens']['output']['known_calls'], 4)
        self.assertIsNone(summarize([])['non_reasoning_output']['value'])
        self.assertEqual(summarize(calls[-1:])['non_reasoning_output']['value'], 0)


class RecordedCalls(unittest.TestCase):
    def record(self, root, *, failure=True, version=2):
        class Adapter(Fake):
            def complete(adapter, *args, **kwargs):
                result = super().complete(*args, **kwargs)
                result.envelope.safe_metadata['finish_reason'] = 'length' if failure else 'stop'
                return result
        adapter = Adapter(code='length' if failure else None, usage=USAGE)
        options = ExecutionOptions(2, 16384, 180, 4096, 8192) if version == 2 else ExecutionOptions()
        cli = CLI(http_adapter=adapter, credentials=Credentials('synthetic-secret-diagnostic'), options=options)
        owner = {'id': 'a'*32, 'agent': 'openrouter', 'model': 'openrouter/auto', 'stage': 'extract',
                 'status': 'error' if failure else 'success'}
        folder = root / 'runs' / owner['id'] / '1'
        if failure:
            with self.assertRaisesRegex(ValueError, '토큰 한도'): cli.complete('openrouter', owner['model'], 'synthetic', folder)
        else:
            self.assertEqual(cli.complete('openrouter', owner['model'], 'synthetic', folder), {'ok': True})
        self.assertEqual(adapter.calls, 1)
        return owner, folder

    def test_historical_length_metrics_cost_and_no_log_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); owner, folder = self.record(root)
            before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
            calls = job_calls(root, 'runs', owner); c = calls[0]; summary = summarize(calls)
            self.assertEqual(c['status'], 'error'); self.assertEqual(c['finish_reason'], 'length')
            self.assertEqual(c['reasoning_diagnostic']['status'], 'exceeded')
            self.assertEqual(c['reasoning_diagnostic']['non_reasoning_output'], 1393)
            self.assertEqual(c['tokens']['reasoning'], 14991)
            self.assertEqual(summary['reported_usd']['value'], None)
            self.assertEqual(summary['credits'][0]['value'], .01059125)
            self.assertEqual(summary['non_reasoning_output'], {'value': 1393, 'known_calls': 1})
            self.assertNotIn('synthetic-secret-diagnostic', json.dumps(calls))
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})
            # A mismatching usage identity is not usable diagnostic evidence.
            usage = json.loads((folder / 'usage.json').read_text()); usage['provider_call_id'] = 'b'*32
            (folder / 'usage.json').write_text(json.dumps(usage))
            self.assertEqual(job_calls(root, 'runs', owner)[0]['reasoning_diagnostic']['status'], 'unknown')

    def test_success_overrun_is_observation_and_v1_comparison_is_inapplicable(self):
        for version in (1, 2):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); owner, _ = self.record(root, failure=False, version=version)
                c = job_calls(root, 'runs', owner)[0]
                self.assertEqual(c['status'], 'success')
                self.assertEqual(c['reasoning_diagnostic']['status'], 'exceeded' if version == 2 else 'not_applicable')
                self.assertEqual(c['tokens']['reasoning'], 14991)

    def test_invalid_request_metadata_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); owner, folder = self.record(root)
            metadata = json.loads((folder / 'run.json').read_text()); metadata['reasoning_max_tokens'] = True
            (folder / 'run.json').write_text(json.dumps(metadata))
            self.assertEqual(job_calls(root, 'runs', owner)[0]['reasoning_diagnostic']['status'], 'unknown')


class UI(unittest.TestCase):
    def test_visible_observation_counts_router_guidance_and_escaping(self):
        source = Path('verinote/web/app.js').read_text()
        functions = source[source.index('function usageStage('):source.index('function downloadJSON(')]
        guidance = source[source.index('function reasoningModelGuidance('):source.index('function executionGuidance(')]
        call = copy.deepcopy(CALL); call.update(requested_model='<unsafe>', status='error', stage='extract', warnings=[],
                                               reasoning_diagnostic=reasoning_diagnostic(CALL))
        call['finish_reason'] = '<unsafe>'
        def metric(value):
            return {'value': value, 'known_calls': int(value is not None)}
        summary = {'calls': 1, 'incomplete_calls': 0, 'tokens': {'input': metric(15995), 'output': metric(16384), 'reasoning': metric(14991)},
                   'reported_usd': metric(None), 'estimated_usd': metric(None), 'credits': [],
                   'non_reasoning_output': metric(1393), 'details': [call]}
        script = "const esc=v=>String(v??'').replaceAll('<','&lt;');\n" + functions + guidance
        script += '\nconst u=' + json.dumps(summary) + ';\n' + r'''
const html=renderUsage(u);
if(!html.includes('14,991')||!html.includes('1,393')||!html.includes('4,096')||!html.includes('초과 관측')||!html.includes('1/1개 기록'))process.exit(1);
if(html.includes('<unsafe>')||!html.includes('&lt;unsafe>'))process.exit(2);
for(const model of ['openrouter/auto','openrouter/free'])if(!reasoningModelGuidance('openrouter',model).includes('명시적인 모델 ID'))process.exit(3);
if(reasoningModelGuidance('openrouter','vendor/fixed').includes('실제 모델이 바뀔'))process.exit(4);
if(reasoningModelGuidance('codex','openrouter/auto')!=='')process.exit(5);
'''
        subprocess.run(['node', '-e', script], check=True)
