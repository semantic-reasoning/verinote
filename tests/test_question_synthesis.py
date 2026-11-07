"""Complete-record processing and synthesis are not raw-evidence authorization."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_assistant import answer, tool
from test_question_budget import LimitedFake

from verinote.assistant import PROMPT, Assistant
from verinote.question_batches import MARKER, leaf_prompt, plan, units_for
from verinote.question_context import resolve
from verinote.question_synthesis import run, supports, validate
from verinote.store import Store, digest
from verinote.usage import job_calls


def unpack(prompt):
    value = json.loads(prompt.split(MARKER, 1)[1])
    return resolve(value) if 'context_projection_version' in value else value


def scripted(prompt):
    value = unpack(prompt)
    if 'assigned_ids' in value:
        return {'records': [{'id': key, 'text': '초기 중요 단서' if key == 'E1' else '관련 기록 분석',
                             'citations': [key] if key in value['evidence'] else [], 'unknowns': []}
                            for key in value['assigned_ids']]}
    if 'children' in value:
        return {'children': [c['id'] for c in value['children']], 'text': '초기 중요 단서를 포함하여 종합',
                'citations': sorted({e for c in value['children'] for e in c['supported_evidence']}), 'unknowns': []}
    if 'analysis' in value:
        return answer('inference', [value['analysis']['id']])
    return tool()


def state():
    return {'snapshot': 'fixed', 'question': '원래 질문 전체를 유지하세요.', 'round': 2,
            'answer_only': True, 'max_rounds': 6, 'exploration_rounds_remaining': 0, 'tool_slots_remaining': 0,
            'catalog': {'documents': [{'id': 'doc', 'title': '전체 안내'}]},
            'trace': [{'name': 'query_graph', 'status': 'success', 'evidence_id': 'E1'}],
            'evidence': {f'E{i}': {'kind': 'graph', 'result': {'rows': [[str(i), 'p', 'b']],
                         'proofs': [''.join(chr(0x4e00+j) for j in range(i*1500, (i+1)*1500))]}}
                         for i in range(1, 9)}}


class CompleteContext(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root/'state.sqlite3')

    def assistant(self, raw, response=scripted, limit=18000):
        class Scripted(LimitedFake):
            def complete(inner, agent, model, prompt, folder):
                inner.calls.append((agent, model, prompt))
                return response(prompt)
        cli = Scripted([], limit)
        a = Assistant(self.store, cli, self.store.snapshot(),
                      {'id': 'a'*32, 'question': raw['question'], 'agent': 'claude', 'model': 'sonnet'}, self.root, lambda *args: None)
        a.evidence = copy.deepcopy(raw['evidence']); a.trace = copy.deepcopy(raw['trace'])
        a.job['physical_call_bound'] = 2*6*(len(units_for(raw))+20)
        return a

    def test_exhaustive_partition_preserves_exact_records_and_original_question(self):
        raw = state(); before = copy.deepcopy(raw)
        prepared = plan(raw, PROMPT, 18000)
        self.assertGreater(len(prepared.batches), 1)
        seen = []
        for batch in prepared.batches:
            prompt = leaf_prompt(prepared.base, batch, prepared.allowance)
            self.assertLessEqual(len(prompt.encode()), 18000)
            decoded = unpack(prompt)
            self.assertEqual(decoded['question'], raw['question'])
            for key, evidence in decoded['evidence'].items():
                self.assertEqual(evidence, raw['evidence'][key])
            seen.extend(decoded['assigned_ids'])
        self.assertEqual(seen, [u['id'] for u in prepared.units])
        self.assertEqual(raw, before)
        self.assertEqual(plan(raw, PROMPT, 18000), prepared)

    def test_all_records_and_early_clue_reach_root_with_more_than_six_calls(self):
        raw = state(); a = self.assistant(raw, limit=14000)
        output, _ = run(a, raw, PROMPT, 2)
        tree = a.job['context_processing'][0]
        self.assertEqual(tree['status'], 'complete')
        self.assertEqual(tree['processed_records'], tree['planned_records'])
        self.assertEqual(tree['processed_batches'], tree['planned_batches'])
        self.assertGreater(len(a.cli.calls), 6)
        self.assertLessEqual(len(a.cli.calls), 2*tree['planned_batches'])
        self.assertTrue(all(len(c[2].encode()) <= 14000 for c in a.cli.calls))
        self.assertEqual(a.validate_answer(output['answer']), output['answer'])
        self.assertIn('E1', supports(a, tree['root']))
        self.assertEqual(a.evidence, raw['evidence'])
        self.assertEqual(a.delivered_evidence, set())
        self.assertEqual(a.delivered_lookups, set())
        self.assertEqual(len(job_calls(self.root, 'questions', dict(a.job, status='success'))), len(a.cli.calls))
        self.assertTrue(all(c['status'] == 'unknown' for c in job_calls(self.root, 'questions', dict(a.job, status='success'))))
        with self.assertRaises(ValueError):
            a.validate_answer(answer('graph', ['E1'])['answer'])
        a.evidence['E1']['result']['rows'] = []
        with self.assertRaisesRegex(ValueError, '무결성'):
            supports(a, tree['root'])

    def test_missing_leaf_or_child_receipt_fails_and_keeps_completed_work(self):
        for phase in ('leaf', 'merge'):
            with self.subTest(phase=phase):
                def bad(prompt, selected_phase=phase):
                    result = scripted(prompt)
                    if selected_phase == 'leaf' and 'records' in result:
                        result['records'].pop()
                    if selected_phase == 'merge' and 'children' in result:
                        result['children'].pop()
                    return result
                raw = state(); a = self.assistant(raw, bad)
                with self.assertRaisesRegex(ValueError, '처리 범위'):
                    run(a, raw, PROMPT, 2)
                self.assertEqual(a.job['context_processing'][0]['status'], 'failed')
                self.assertEqual(a.job['call_manifest'][len(a.cli.calls)-1]['status'], 'failed')
                self.assertFalse(a.delivered_analysis)
                if phase == 'merge':
                    self.assertTrue(a.job['analysis_nodes'])

    def test_oversized_analysis_and_mandatory_question_fail_without_truncating(self):
        raw = state(); a = self.assistant(raw, lambda _: {'text': 'x'*20000})
        with self.assertRaisesRegex(ValueError, '예산'):
            run(a, raw, PROMPT, 2)
        self.assertEqual(len(a.cli.calls), 1)
        raw['question'] = ''.join(chr(0x4e00+i) for i in range(10000))
        a = self.assistant(raw)
        with self.assertRaisesRegex(ValueError, '입력 예산'):
            run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])

    def test_catalog_and_empty_evidence_cannot_support_claims_or_fallback(self):
        empty = {'kind': 'graph', 'result': {'rows': []}}
        for unit in ({'id': 'C1', 'category': 'catalog', 'value': {'title': 'X'}},
                     {'id': 'E1', 'category': 'evidence', 'value': empty}):
            with self.subTest(unit=unit['id']):
                response = {'records': [{'id': unit['id'], 'text': '발견', 'citations': [unit['id']], 'unknowns': []}]}
                with self.assertRaisesRegex(ValueError, '인용'):
                    validate(response, {'stage': 'analysis_leaf', 'units': [unit]}, 1000, {})
        raw = state(); a = self.assistant(raw)
        run(a, raw, PROMPT, 2)
        with self.assertRaises(ValueError):
            a.authorize_source('doc', {'lookup_id': 'K1', 'reason': 'no_approved_wiki', 'detail': '부족', 'wiki_evidence_ids': []})

    def test_persistence_failure_prevents_dispatch_and_unknown_usage_is_not_zero(self):
        raw = state(); a = self.assistant(raw)
        def fail(*args): raise OSError('save failed')
        a.progress = fail
        with self.assertRaisesRegex(OSError, 'save failed'):
            run(a, raw, PROMPT, 2)
        self.assertEqual(a.cli.calls, [])
        owner = {'id': 'a'*32, 'agent': 'claude', 'status': 'error', 'model': 'sonnet', 'call_manifest': [
            {'path': 'context-1-1', 'stage': 'analysis_leaf', 'status': 'failed'},
            {'path': 'context-1-1', 'stage': 'analysis_leaf', 'status': 'failed'},
            {'path': '../outside', 'stage': 'analysis_leaf', 'status': 'failed'},
            {'path': 'context-1-2', 'stage': 'analysis_leaf', 'status': 'planned'}]}
        calls = job_calls(self.root, 'questions', owner)
        self.assertEqual(len(calls), 1); self.assertEqual(calls[0]['status'], 'unknown')

    def test_whole_assistant_overflow_automatically_processes_every_record(self):
        raw = state(); a = self.assistant(raw)
        a.evidence = {}; a.trace = []
        # First direct request asks for a graph; subsequent oversized state is automatic.
        with patch('verinote.assistant.engine.query', return_value=raw['evidence']['E1']['result']):
            a.job['question'] += ' 질문에 충실한 분석'
            a.cli.limit = 14000
            result = a.run()
        self.assertEqual(result['claims'][0]['kind'], 'inference')
        self.assertEqual(a.job['context_processing'][-1]['status'], 'complete')
        self.assertTrue(a.job['analysis_nodes'])

    def test_synthesis_correction_stays_in_logical_rounds_and_cannot_cite_raw_e(self):
        raw = state()
        def response(prompt):
            output = scripted(prompt); decoded = unpack(prompt)
            if 'analysis' in decoded and decoded['round'] == 2:
                return answer('graph', ['E1'])  # Synthesis must not masquerade as approved fact.
            return output
        a = self.assistant(raw, response, limit=14000)
        a.evidence = {}; a.trace = []
        with patch('verinote.assistant.engine.query', return_value=raw['evidence']['E1']['result']):
            result = a.run()
        self.assertEqual(result['claims'][0]['kind'], 'inference')
        self.assertEqual([t['round'] for t in a.job['context_processing']], [2, 3])
        self.assertEqual(len(a.trace), 1)
        self.assertTrue(unpack(a.cli.calls[-1][2])['answer_only'])

    def test_failed_provider_does_not_deliver_new_analysis_or_raw_authorization(self):
        raw = state()
        def fail(prompt):
            if 'analysis' in unpack(prompt):
                raise RuntimeError('provider failed')
            return scripted(prompt)
        a = self.assistant(raw, fail)
        a.delivered_evidence = {'previous'}; a.delivered_lookups = {'Kprevious'}
        with self.assertRaisesRegex(RuntimeError, 'provider failed'):
            run(a, raw, PROMPT, 2)
        self.assertEqual(a.delivered_evidence, {'previous'})
        self.assertEqual(a.delivered_lookups, {'Kprevious'})
        self.assertEqual(a.delivered_analysis, set())
        self.assertEqual(a.job['context_processing'][-1]['status'], 'failed')
        self.assertEqual(a.job['call_manifest'][-1]['status'], 'failed')

    def test_record_hashes_are_bound_to_original_values(self):
        for unit in units_for(state()):
            self.assertEqual(unit['sha256'], digest(unit['value']))
