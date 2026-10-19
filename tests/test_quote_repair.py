"""Synthetic regressions for bounded exact-source quotation repair."""
import copy
import json
import unittest
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.ontology import (
    QUOTE_REPAIR_PROMPT,
    apply_quote_repairs,
    bounded_prompt,
    extraction_quote_issues,
)
from verinote.service import Service


def response():
    return extraction_response()


def broken():
    value = response()
    value['facts'][0]['quote'] = '상기 ' + FACTS[0]['quote']
    return value


def repair(path='facts.0.quote', quote=FACTS[0]['quote']):
    return {'repairs': [{'path': path, 'quote': quote}]}


class QuoteRepair(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def execute(self, outputs, source=SOURCE):
        doc = self.store.register('인용 테스트', 'synthetic.md', source.encode(), source)
        self.current_doc = doc
        self.dictionary = approved_dictionary(self.store, doc['id'])
        calls, pending = [], iter(outputs)
        class Fake:
            def complete(self, agent, model, prompt, folder):
                calls.append({'agent': agent, 'model': model, 'prompt': prompt, 'folder': folder})
                result = next(pending)
                if isinstance(result, Exception):
                    raise result
                return result() if callable(result) else copy.deepcopy(result)
        service = Service(self.store, self.root, Fake())
        run = service.start(doc['id'], 'copilot', 'chosen-model', expected_version=1, expected_generation=1)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), self.store.document(doc['id'], 1), calls

    def assert_no_candidates(self, doc):
        self.assertEqual([i['kind'] for i in doc['items']], ['ontology'])

    def test_repairs_only_bad_fields_and_saves_pending_with_audit(self):
        value = broken()
        value['claims'][0]['quote'] = '상기 ' + SOURCE
        original = copy.deepcopy(value)
        corrections = {'repairs': repair()['repairs'] + repair('claims.0.quote', SOURCE)['repairs']}
        issues = extraction_quote_issues(value, SOURCE, DICTIONARY)
        fixed, changes = apply_quote_repairs(value, SOURCE, issues, corrections)
        self.assertEqual(value, original)
        self.assertEqual(fixed, response())
        self.assertEqual(len(changes), 2)
        run, doc, calls = self.execute([value, corrections])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('인용 2개 보정', run['detail'])
        self.assertIn('추가 호출 1회', run['detail'])
        self.assertIn('관계 1 인용', run['detail'])
        self.assertIn('위키 주장 1 인용', run['detail'])
        candidates = [i for i in doc['items'] if i['kind'] != 'ontology']
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(i['status'] == 'pending' for i in candidates))
        self.assertCountEqual([i['payload'] for i in candidates if i['kind'] == 'fact'], FACTS)
        self.assertEqual(self.store.snapshot()['facts'], [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['folder'], calls[0]['folder'] / 'quote-repair')
        self.assertEqual((calls[1]['agent'], calls[1]['model']), ('copilot', 'chosen-model'))
        payload = json.loads(calls[1]['prompt'].split('입력(JSON):\n')[1])
        self.assertEqual(payload['text'], SOURCE)
        self.assertEqual(payload['approved_dictionary'], run['ontology'])
        self.assertEqual({x['path'] for x in payload['issues']}, {'claims.0.quote', 'facts.0.quote'})
        self.assertEqual(json.loads((calls[1]['folder'] / 'changes.json').read_text()), changes)

    def test_valid_response_needs_no_repair(self):
        run, doc, calls = self.execute([response()])
        self.assertEqual(run['status'], 'success')
        self.assertIn('관계 후보 2개', run['detail'])
        self.assertEqual(len(calls), 1)

    def test_all_non_quote_errors_prevent_extra_call(self):
        mutations = [
            lambda v: v['facts'][1].update(predicate='unknown'),
            lambda v: v['facts'][1].update(subject='unknown'),
            lambda v: v['claims'][0].update(text=''),
            lambda v: v['claims'][0].update(quote=''),
            lambda v: v['facts'][1].update(quote=None),
            lambda v: v.update(facts=v['facts'] * 41),
            lambda v: v.update(title=''),
            lambda v: v['facts'].append('invalid'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = broken(); mutate(value)
                run, doc, calls = self.execute([value])
                self.assertEqual(run['status'], 'error')
                self.assertEqual(len(calls), 1)
                self.assert_no_candidates(doc)

    def test_strict_repair_mapping_and_invalid_replacements_fail_closed(self):
        invalid = [
            {}, {'repairs': []}, {'repairs': [*repair()['repairs'], *repair()['repairs']]},
            {'repairs': repair()['repairs'], 'facts': []},
            {'repairs': [repair()['repairs'][0] | {'subject': 'changed'}]},
            repair('facts.1.quote'), repair('claims.0.quote'), repair('facts.-1.quote'),
            repair(quote='not present'), repair(quote=''), repair(quote=None),
            {'repairs': [{'path': [], 'quote': SOURCE}]},
        ]
        for correction in invalid:
            with self.subTest(correction=correction):
                run, doc, calls = self.execute([broken(), correction])
                self.assertEqual(run['status'], 'error')
                self.assertIn('청크 1', run['detail'])
                self.assertIn('관계 1 인용', run['detail'])
                self.assertIn('보정 실패', run['detail'])
                self.assertEqual(len(calls), 2)
                self.assert_no_candidates(doc)
        # Duplicate membership with the right count must also be rejected.
        value = broken(); value['claims'][0]['quote'] = 'absent'
        run, doc, calls = self.execute([value, {'repairs': repair()['repairs'] * 2}])
        self.assertEqual(run['status'], 'error'); self.assert_no_candidates(doc)

    def test_wrong_chunk_evidence_is_not_accepted(self):
        next_chunk = '다른 문단에만 있는 문장이다.'
        source = SOURCE.ljust(8000) + next_chunk
        run, doc, calls = self.execute([broken(), repair(quote=next_chunk)], source)
        self.assertEqual(run['status'], 'error')
        self.assertIn('원문에 없는 인용', run['detail'])
        self.assertEqual(len(calls), 2)
        self.assert_no_candidates(doc)

    def test_two_repairs_per_run_and_no_partial_chunks(self):
        chunk = SOURCE.ljust(8000)
        run, doc, calls = self.execute([broken(), repair(), broken(), repair()], chunk * 2)
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertIn('추가 호출 2회', run['detail'])
        self.assertEqual(len(calls), 4)
        run, doc, calls = self.execute([broken(), repair(), broken(), repair(), broken()], chunk * 3)
        self.assertEqual(run['status'], 'error')
        self.assertIn('청크 3', run['detail'])
        self.assertIn('2회 한도', run['detail'])
        self.assertEqual(len(calls), 5)
        self.assert_no_candidates(doc)

    def test_repair_timeout_and_prompt_limit_stop_without_retry(self):
        run, doc, calls = self.execute([broken(), TimeoutError('CLI 실행 시간 제한 초과')])
        self.assertEqual(run['status'], 'error')
        self.assertIn('시간 제한', run['detail'])
        self.assertEqual(len(calls), 2); self.assert_no_candidates(doc)
        def limited(instructions, payload):
            if instructions == QUOTE_REPAIR_PROMPT:
                raise ValueError('추출 프롬프트 120,000바이트 제한 초과')
            return bounded_prompt(instructions, payload)
        with patch('verinote.service.bounded_prompt', side_effect=limited):
            run, doc, calls = self.execute([broken()])
        self.assertEqual(run['status'], 'error')
        self.assertIn('바이트 제한', run['detail'])
        self.assertEqual(len(calls), 1); self.assert_no_candidates(doc)

    def test_approval_revocation_or_edit_during_repair_prevents_save(self):
        for action in ('pending', 'revise'):
            def revoke():
                d = self.dictionary
                self.store.review(d['id'], d['revision'], action, 'Reviewer', 'changed during repair', DICTIONARY if action == 'revise' else None)  # noqa: B023 (intentional: same-iteration action)
                return repair()
            with self.subTest(action=action):
                run, doc, calls = self.execute([broken(), revoke])
                self.assertEqual(run['status'], 'error')
                self.assertIn('사전이 변경', run['detail'])
                self.assert_no_candidates(doc)

    def test_source_version_remains_pinned_during_repair(self):
        def upload_version():
            d = self.current_doc
            self.store.register('인용 테스트', 'v2.md', b'new', 'new', d['id'], 1, 1)
            return repair()
        run, old_doc, calls = self.execute([broken(), upload_version])
        self.assertEqual(run['status'], 'success', run['detail'])
        self.assertEqual(old_doc['current_version'], 2)
        self.assertEqual(len(old_doc['items']), 4)
        self.assertEqual(self.store.document(old_doc['id'])['items'], [])
        self.assertEqual(json.loads(calls[1]['prompt'].split('입력(JSON):\n')[1])['text'], SOURCE)

    def test_error_locates_45th_fact_without_exposing_quote(self):
        value = extraction_response([FACTS[0] for _ in range(48)])
        value['facts'][44]['quote'] = 'private-looking-invalid-quote'
        run, doc, calls = self.execute([value, repair('facts.44.quote', 'also-invalid')])
        self.assertEqual(run['status'], 'error')
        self.assertIn('청크 1', run['detail']); self.assertIn('관계 45 인용', run['detail'])
        self.assertNotIn('private-looking', run['detail']); self.assertNotIn('also-invalid', run['detail'])
        self.assert_no_candidates(doc)


if __name__ == '__main__':
    unittest.main()
