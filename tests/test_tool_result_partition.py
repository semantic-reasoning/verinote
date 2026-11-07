"""Oversized individual tool results retain originals and complete fragment lineage."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_question_context as context_fixture
from test_assistant import answer
from test_question_budget import LimitedFake
from test_question_synthesis import scripted, unpack

from verinote.assistant import MAX_ROUNDS, Assistant
from verinote.question_batches import leaf_prompt, plan
from verinote.question_fragments import MAX_PARTITION_UNITS, reconstruct
from verinote.question_synthesis import run, supports, validate
from verinote.store import Store, canonical, digest


def respond(prompt):
    state = unpack(prompt)
    if 'assigned_ids' not in state:
        return scripted(prompt)
    fragments = {r['id']: r for r in state['records'] if r['category'] == 'fragment'}
    records = []
    for key in state['assigned_ids']:
        fragment = fragments.get(key)
        citable = (fragment['value']['source_usable'] if fragment else key in state['evidence'])
        records.append({'id': key, 'text': '배정된 부분의 발견; 빠진 문맥은 미확인',
                        'citations': [key] if citable else [], 'unknowns': []})
    return {'records': records}


def large_state():
    raw = context_fixture.state()
    graph = raw['evidence']['E1']['result']
    # Unique Korean content cannot shrink through duplicate-string projection.
    graph['facts'][0]['payload']['quote'] = ''.join(chr(0x4e00+i) for i in range(18000))
    return dict(raw, question='전체 관계와 인용 내용을 설명하세요.', round=2, max_rounds=6,
                answer_only=True, exploration_rounds_remaining=0, tool_slots_remaining=0,
                evidence={'E1': raw['evidence']['E1']},
                trace=[{'name': 'query_graph', 'status': 'success', 'evidence_id': 'E1'}])


class ToolResultPartition(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.store = Store(self.root/'state.sqlite3')

    def assistant(self, raw, response=respond, limit=18000):
        class Fake(LimitedFake):
            def complete(inner, agent, model, prompt, folder):
                inner.calls.append((agent, model, prompt))
                return response(prompt)
        a = Assistant(self.store, Fake([], limit), self.store.snapshot(),
                      {'id': 'b'*32, 'question': raw['question'], 'agent': 'fake', 'model': 'fake'},
                      self.root, lambda *args: None)
        a.evidence = copy.deepcopy(raw['evidence']); a.trace = copy.deepcopy(raw['trace'])
        a.job['physical_call_bound'] = 2*MAX_ROUNDS*MAX_PARTITION_UNITS
        return a

    def test_single_large_scoped_graph_answers_and_preserves_original(self):
        raw = large_state(); original = copy.deepcopy(raw['evidence']['E1'])
        self.assertGreater(len(canonical(original).encode()), 30000)
        a = self.assistant(raw); a.evidence = {}; a.trace = []
        with patch('verinote.assistant.engine.query', return_value=original['result']):
            output = a.run()
        self.assertEqual(output['claims'][0]['kind'], 'inference')
        self.assertEqual(a.evidence, {'E1': original})
        tree = a.job['context_processing'][-1]
        self.assertTrue(tree['fragments'])
        self.assertEqual(tree['status'], 'complete')
        self.assertEqual(tree['processed_records'], tree['planned_records'])
        self.assertEqual(tree['processed_batches'], tree['planned_batches'])
        self.assertEqual(supports(a, tree['root']), ['E1'])
        self.assertTrue(all(len(c[2].encode()) <= 18000 for c in a.cli.calls))
        self.assertEqual(len(a.cli.calls), len(a.job['call_manifest']))
        self.assertLessEqual(len(a.cli.calls), a.job['physical_call_bound'])
        self.assertFalse(a.delivered_evidence); self.assertFalse(a.delivered_lookups)
        with self.assertRaises(ValueError): a.validate_answer(answer('graph')['answer'])

    def test_fragments_reconstruct_scoped_graph_and_long_unicode_exactly(self):
        from verinote.assistant import PROMPT
        raw = large_state(); original = copy.deepcopy(raw)
        prepared = plan(raw, PROMPT, 18000)
        self.assertEqual(prepared, plan(raw, PROMPT, 18000))
        fragments = [u for u in prepared.units if u['category'] == 'fragment']
        self.assertEqual(reconstruct(fragments), raw['evidence']['E1'])
        parts = [p for u in fragments for p in u['value']['parts']]
        self.assertTrue(any(p['mode'] == 'text' for p in parts))
        assigned = []
        for batch in prepared.batches:
            prompt = leaf_prompt(prepared.base, batch, prepared.allowance)
            self.assertLessEqual(len(prompt.encode()), 18000)
            assigned.extend(unpack(prompt)['assigned_ids'])
        self.assertEqual(assigned, [u['id'] for u in prepared.units])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(raw, original)
        for mutate in ('missing', 'duplicate', 'hash', 'source', 'offset'):
            broken = copy.deepcopy(fragments)
            if mutate == 'missing': broken.pop()
            elif mutate == 'duplicate': broken.append(broken[-1])
            elif mutate == 'hash': broken[0]['sha256'] = '0'*64
            elif mutate == 'source': broken[0]['value']['source_id'] = 'E99'
            else:
                part = next(p for u in broken for p in u['value']['parts'] if p['mode'] == 'text')
                part['offset'] += 1
                for u in broken: u['sha256'] = digest(u['value'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): reconstruct(broken)

    def test_large_search_trace_is_processed_but_cannot_support_or_authorize(self):
        from verinote.assistant import PROMPT
        raw = large_state(); raw['evidence']['E1']['result']['facts'][0]['payload']['quote'] = '짧은 인용'
        lookup = {'lookup_id': 'K1', 'truncated': False, 'matches': [str(i)+'한글'*100 for i in range(200)]}
        raw['trace'].append({'name': 'search_knowledge', 'status': 'success', 'result': lookup})
        a = self.assistant(raw)
        output, _ = run(a, raw, PROMPT, 2)
        self.assertEqual(a.validate_answer(output['answer']), output['answer'])
        tree = a.job['context_processing'][-1]
        self.assertEqual({u['value']['source_category'] for u in tree['fragments'].values()}, {'trace'})
        self.assertEqual(reconstruct(list(tree['fragments'].values())), raw['trace'][1])
        self.assertEqual(supports(a, tree['root']), ['E1'])
        unit = next(iter(tree['fragments'].values()))
        invalid = {'records': [{'id': unit['id'], 'text': '검색을 근거로 주장', 'citations': [unit['id']], 'unknowns': []}]}
        with self.assertRaisesRegex(ValueError, '인용'):
            validate(invalid, {'stage': 'analysis_leaf', 'units': [unit]}, 18000, {})
        self.assertFalse(a.delivered_lookups)
        with self.assertRaises(ValueError): a.authorize_source('doc', {'lookup_id': 'K1'})

    def test_missing_receipt_and_tampered_lineage_or_original_fail(self):
        from verinote.assistant import PROMPT
        raw = large_state()
        for fault in ('missing', 'duplicate', 'citation'):
            def invalid(prompt, selected=fault):
                result = respond(prompt)
                if 'records' in result:
                    if selected == 'missing': result['records'].pop()
                    elif selected == 'duplicate': result['records'].append(result['records'][0])
                    else: result['records'][0]['citations'] = ['E999:part:1']
                return result
            a = self.assistant(raw, invalid)
            with self.subTest(fault=fault), self.assertRaises(ValueError): run(a, raw, PROMPT, 2)
            self.assertEqual(a.job['context_processing'][-1]['status'], 'failed')
            self.assertFalse(a.delivered_analysis)
        for mutate in ('coverage', 'hash', 'source', 'original'):
            a = self.assistant(raw); run(a, raw, PROMPT, 2)
            tree = a.job['context_processing'][-1]; root = tree['root']
            if mutate == 'coverage': a.job['analysis_nodes'][root]['coverage'].pop()
            elif mutate == 'hash': next(iter(tree['fragments'].values()))['sha256'] = '0'*64
            elif mutate == 'source': next(iter(tree['fragments'].values()))['value']['source_id'] = 'E99'
            else: a.evidence['E1']['result']['facts'][0]['payload']['quote'] = '변조'
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): supports(a, root)

    def test_empty_graph_fragments_cannot_support_claims(self):
        from verinote.assistant import PROMPT
        raw = large_state(); graph = raw['evidence']['E1']['result']
        graph.update(rows=[], row_refs=[], proofs=[])
        a = self.assistant(raw)
        prepared = plan(raw, PROMPT, 18000)
        unit = next(u for u in prepared.units if u['category'] == 'fragment')
        self.assertFalse(unit['value']['source_usable'])
        invalid = {'records': [{'id': unit['id'], 'text': '빈 그래프', 'citations': [unit['id']], 'unknowns': []}]}
        with self.assertRaisesRegex(ValueError, '인용'):
            validate(invalid, {'stage': 'analysis_leaf', 'units': [unit]}, 18000, {})
        self.assertFalse(a.delivered_analysis)

    def test_tiny_budget_and_partition_cap_fail_before_dispatch(self):
        from verinote.assistant import PROMPT
        raw = large_state(); a = self.assistant(raw, limit=1000)
        with self.assertRaises(ValueError): run(a, raw, PROMPT, 2)
        self.assertFalse(a.cli.calls)
        a = self.assistant(raw)
        with patch('verinote.question_batches.MAX_PARTITION_UNITS', 1), self.assertRaisesRegex(ValueError, '기록 수'):
            run(a, raw, PROMPT, 2)
        self.assertFalse(a.cli.calls)

    def test_provider_and_persistence_failure_cannot_complete_partition(self):
        from verinote.assistant import PROMPT
        raw = large_state()
        calls = []
        def fail(prompt):
            calls.append(prompt)
            if len(calls) == 2: raise RuntimeError('provider failed')
            return respond(prompt)
        a = self.assistant(raw, fail)
        with self.assertRaisesRegex(RuntimeError, 'provider failed'): run(a, raw, PROMPT, 2)
        self.assertEqual(a.job['context_processing'][-1]['status'], 'failed')
        self.assertFalse(a.delivered_analysis)
        self.assertEqual(a.evidence, raw['evidence'])
        self.assertTrue(a.job['analysis_nodes'])
        a = self.assistant(raw)
        def save_fail(*args): raise OSError('save failed')
        a.progress = save_fail
        with self.assertRaisesRegex(OSError, 'save failed'): run(a, raw, PROMPT, 2)
        self.assertFalse(a.cli.calls)
