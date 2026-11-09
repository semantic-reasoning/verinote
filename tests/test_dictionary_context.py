"""Retained definitions are independent of the configurable table-form threshold."""
import copy
import json
import unittest
from urllib.error import HTTPError

import test_dictionary_pipeline as pipeline
import test_workflow as workflow
from test_workflow import approved_dictionary, extraction_response

from verinote.dictionary_context import PROTOCOL, decode, encode, validate_context_bytes
from verinote.dictionary_pipeline import PREVIOUS_CONTEXT_PROMPT
from verinote.ontology import (
    bounded_prompt,
    dictionary_context,
)
from verinote.service import Service
from verinote.store import canonical, digest

EMPTY = {'entities': [], 'aliases': [], 'relations': []}


def rows(start, count, source):
    return {'entities': [{'id': f'ID{i:04}-'+'i'*150, 'name': f'Component{i:04}', 'type': 'Type'+'t'*140, 'quote': source}
                         for i in range(start, start+count)], 'aliases': [], 'relations': []}


def payload(prompt):
    raw = json.loads(prompt.split('입력(JSON):\n', 1)[1])
    return raw


def restored(value):
    return decode(value) if 'dictionary_wire_version' in value else value


class Codec(unittest.TestCase):
    def test_lossless_all_fields_unicode_controls_and_order(self):
        value = {'entities': [{'id': 'id', 'name': '이름\x01', 'type': 'T'}, {'id': 'id2', 'name': 'e\u0301', 'type': 'T'}],
                 'aliases': [{'alias': '다른 이름', 'entity': 'id'}],
                 'relations': [{'id': 'rel', 'description': '의미와 조건\r\n그대로', 'subject_types': ['T'], 'object_types': ['T', 'U']}]}
        before = copy.deepcopy(value); wire = encode(value)
        self.assertEqual(decode(wire), value); self.assertEqual(value, before)
        self.assertEqual(wire, encode(value)); self.assertEqual(wire['sha256'], digest(value))
        mutations = [lambda w:w.update(extra=1), lambda w:w.update(dictionary_wire_version=True),
                     lambda w:w['rows']['entities'][0].__setitem__(0, True),
                     lambda w:w['rows']['entities'][0].__setitem__(0, 9999),
                     lambda w:w['rows']['entities'][0].append(0),
                     lambda w:w['rows']['entities'].append(w['rows']['entities'][0]),
                     lambda w:w['rows']['relations'][0].__setitem__(3, 0),
                     lambda w:w.update(sha256='0'*64), lambda w:w['strings'].__setitem__(0, 'changed')]
        for mutate in mutations:
            bad = copy.deepcopy(wire); mutate(bad)
            with self.assertRaises(ValueError): decode(bad)

    def test_both_limits_exact_utf8_and_user_setting_effect(self):
        definitions = dictionary_context(rows(0, 240, 'unused'))
        body = {'dictionary': definitions, 'sources': [{'id': 's', 'text': '가'*100}]}
        raw_bytes = len(canonical(definitions).encode()); self.assertGreater(raw_bytes, 60000)
        plain = bounded_prompt('input\n', body, context_limit=100000)
        self.assertFalse(plain.startswith(PROTOCOL))
        report = {}; packed = bounded_prompt('input\n', body, context_limit=60000, diagnostics=report)
        self.assertTrue(packed.startswith(PROTOCOL)); self.assertLess(report['transmitted_definition_bytes'], 60000)
        self.assertEqual(decode(json.loads(packed.split('input\n')[1])['dictionary']), definitions)
        size = len(packed.encode())
        self.assertEqual(bounded_prompt('input\n', body, context_limit=60000, prompt_limit=size), packed)
        with self.assertRaisesRegex(ValueError, '전체 요청'):
            bounded_prompt('input\n', body, context_limit=60000, prompt_limit=size-1)
        report = {}; forced = bounded_prompt('input\n', body, context_limit=100, diagnostics=report)
        self.assertEqual(decode(json.loads(forced.split('input\n')[1])['dictionary']), definitions)
        self.assertEqual(report['status'], 'over_context_limit'); self.assertGreater(report['transmitted_definition_bytes'], 100)
        for value in (True, False, 0, -1, 120001, 1.5, '60000', None):
            with self.assertRaises(ValueError): validate_context_bytes(value)
        self.assertEqual(validate_context_bytes(120000), 120000)
        with self.assertRaises(ValueError): bounded_prompt('', body, prompt_limit=True)

    def test_codec_may_fit_definition_threshold_despite_protocol_overhead(self):
        definitions = dictionary_context(rows(0, 3, 'unused'))
        body = {'dictionary': definitions}
        limit = len(json.dumps(encode(definitions), ensure_ascii=False, separators=(',', ':')).encode())
        raw = 'input\n'+json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        self.assertLess(limit, len(canonical(definitions).encode()))
        packed = bounded_prompt('input\n', body, context_limit=limit)
        self.assertGreater(len(packed.encode()), len(raw.encode()))
        self.assertEqual(decode(json.loads(packed.split('input\n')[1])['dictionary']), definitions)

    def test_repair_small_plain_unchanged_large_wire_keeps_pin_and_issues(self):
        small = {'approved_dictionary': {'id': 'pin', 'payload': rows(0, 1, 'quote'), 'sha256': 'pin-hash'},
                 'text': 'source', 'issues': [{'path': 'facts.0.quote', 'item': {'quote': 'offending'}}]}
        expected = 'repair\n'+json.dumps(small, ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(bounded_prompt('repair\n', small), expected)
        large = copy.deepcopy(small); large['approved_dictionary']['payload'] = rows(0, 240, 'quote'*1000)
        before = copy.deepcopy(large)
        packed = bounded_prompt('repair\n', large)
        decoded = json.loads(packed.split('repair\n')[1])
        self.assertEqual(decoded['issues'], large['issues']); self.assertEqual(decoded['text'], 'source')
        self.assertEqual(decoded['approved_dictionary']['sha256'], 'pin-hash')
        self.assertEqual(decode(decoded['approved_dictionary']['payload']), dictionary_context(large['approved_dictionary']['payload']))
        self.assertEqual(large, before)


SERIALIZE = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))  # noqa: E731


def sent_definitions(prompt):
    """The definition field of a returned prompt, and whether it is the table form."""
    body = json.loads(prompt.split('input\n', 1)[1])
    field = body['dictionary'] if 'dictionary' in body else body['approved_dictionary']['payload']
    return field, 'dictionary_wire_version' in field


class FormSelection(unittest.TestCase):
    def fixtures(self):
        generation = {'dictionary': dictionary_context(rows(0, 60, 'unused')), 'sources': [{'id': 's', 'text': '가'*100}]}
        tiny = {'dictionary': dictionary_context(rows(0, 1, 'unused'))}
        pin = {'approved_dictionary': {'id': 'pin', 'payload': rows(0, 40, 'quote'*1000), 'sha256': 'pin-hash'},
               'text': 'source', 'issues': [{'path': 'facts.0.quote', 'item': {'quote': 'offending'}}]}
        protocol_pushes_table_over = {'dictionary': {'entities': [{'id': f'E{i}', 'name': 'N'*50, 'type': 'T'*50} for i in range(6)],
                                                     'aliases': [], 'relations': []}}
        return {'generation': generation, 'tiny': tiny, 'long-pin-repair': pin, 'protocol-pushes-table-over': protocol_pushes_table_over}

    def sizes(self, body):
        selected = body['dictionary'] if 'dictionary' in body else body['approved_dictionary']['payload']
        definitions = dictionary_context(selected)
        packed = copy.deepcopy(body)
        if 'dictionary' in packed: packed['dictionary'] = encode(definitions)
        else: packed['approved_dictionary']['payload'] = encode(definitions)
        return dict(raw_definition=len(SERIALIZE(definitions).encode()), table_definition=len(SERIALIZE(encode(definitions)).encode()),
                    raw_prompt=len(('input\n'+SERIALIZE(body)).encode()),
                    table_prompt=len((PROTOCOL+'input\n'+SERIALIZE(packed)).encode()), definitions=definitions)

    def test_status_encoding_and_transmitted_bytes_match_the_returned_prompt(self):
        reached = set()
        for name, body in self.fixtures().items():
            n = self.sizes(body)
            contexts = {1, n['table_definition']-1, n['table_definition'], n['raw_definition']-1, n['raw_definition'], 120000}
            prompts = {n['raw_prompt']-1, n['raw_prompt'], n['table_prompt']-1, n['table_prompt'], 10**7}
            for context in sorted(contexts):
                for prompt_limit in sorted(prompts):
                    report = {}
                    with self.subTest(fixture=name, context=context, prompt_limit=prompt_limit):
                        if min(n['raw_prompt'], n['table_prompt']) > prompt_limit:
                            with self.assertRaisesRegex(ValueError, '전체 요청'):
                                bounded_prompt('input\n', body, context_limit=context, prompt_limit=prompt_limit, diagnostics=report)
                            self.assertEqual(report['status'], 'unfit')
                            self.assertNotIn('transmitted_definition_bytes', report); self.assertNotIn('prompt_bytes', report)
                            reached.add('refused'); continue
                        sent = bounded_prompt('input\n', body, context_limit=context, prompt_limit=prompt_limit, diagnostics=report)
                        field, is_table = sent_definitions(sent)
                        self.assertEqual(restored(field), n['definitions'])
                        self.assertEqual(sent.startswith(PROTOCOL), is_table)
                        self.assertEqual(report['encoding'], 'dictionary-wire-v1' if is_table else 'plain')
                        self.assertEqual(report['prompt_bytes'], len(sent.encode())); self.assertLessEqual(report['prompt_bytes'], prompt_limit)
                        self.assertEqual(report['transmitted_definition_bytes'],
                                         len(SERIALIZE(field if is_table else dictionary_context(field)).encode()))
                        if report['status'] == 'within_limit':
                            self.assertLessEqual(report['transmitted_definition_bytes'], context)
                            reached.add('within-table' if is_table else 'within-plain')
                        else:
                            self.assertEqual(report['status'], 'over_context_limit')
                            self.assertGreater(report['transmitted_definition_bytes'], context)
                            self.assertEqual(is_table, n['table_prompt'] <= n['raw_prompt'])
                            reached.add('over-table' if is_table else 'over-plain')
        self.assertEqual(reached, {'refused', 'within-table', 'within-plain', 'over-table', 'over-plain'})

    def test_definitions_within_threshold_with_raw_prompt_over_the_prompt_limit_go_as_table(self):
        body = self.fixtures()['long-pin-repair']; n = self.sizes(body)
        self.assertGreater(n['raw_prompt'], n['table_prompt'])
        report = {}
        sent = bounded_prompt('input\n', body, context_limit=n['raw_definition'], prompt_limit=n['table_prompt'], diagnostics=report)
        self.assertTrue(sent_definitions(sent)[1]); self.assertEqual(report['status'], 'within_limit')

    def test_only_the_smaller_form_is_sent_when_both_exceed_the_threshold(self):
        body = self.fixtures()['tiny']; n = self.sizes(body); self.assertGreater(n['table_prompt'], n['raw_prompt'])
        report = {}
        sent = bounded_prompt('input\n', body, context_limit=1, prompt_limit=n['raw_prompt'], diagnostics=report)
        self.assertFalse(sent.startswith(PROTOCOL)); self.assertEqual(sent, 'input\n'+SERIALIZE(body))
        self.assertEqual((report['encoding'], report['status']), ('plain', 'over_context_limit'))
        body = self.fixtures()['generation']; n = self.sizes(body); report = {}
        sent = bounded_prompt('input\n', body, context_limit=1, prompt_limit=n['raw_prompt'], diagnostics=report)
        self.assertTrue(sent.startswith(PROTOCOL)); self.assertEqual(report['encoding'], 'dictionary-wire-v1')

    def test_table_within_threshold_but_over_the_prompt_limit_sends_plain_and_reports_over_threshold(self):
        body = self.fixtures()['protocol-pushes-table-over']; n = self.sizes(body)
        self.assertGreater(n['table_prompt'], n['raw_prompt'])
        self.assertLess(n['table_definition'], n['raw_definition'])
        report = {}
        sent = bounded_prompt('input\n', body, context_limit=n['table_definition'], prompt_limit=n['raw_prompt'], diagnostics=report)
        self.assertEqual(sent, 'input\n'+SERIALIZE(body))
        self.assertEqual((report['status'], report['encoding']), ('over_context_limit', 'plain'))
        self.assertEqual(report['transmitted_definition_bytes'], n['raw_definition'])

    def test_whole_request_refusals_do_not_advise_changing_the_setting(self):
        body = self.fixtures()['generation']; n = self.sizes(body)
        with self.assertRaises(ValueError) as caught:
            bounded_prompt('input\n', body, context_limit=1, prompt_limit=min(n['raw_prompt'], n['table_prompt'])-1)
        self.assertIn('전체 요청', str(caught.exception)); self.assertNotIn('설정', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            bounded_prompt('input\n', {'sources': [{'id': 's', 'text': 'x'*100}]}, prompt_limit=10)
        self.assertIn('전체 요청', str(caught.exception)); self.assertNotIn('설정', str(caught.exception))


class Generation(unittest.TestCase):
    setUp = pipeline.Pipeline.setUp
    tearDown = pipeline.Pipeline.tearDown
    new_doc = pipeline.Pipeline.new_doc
    run_job = pipeline.Pipeline.run_job
    checkpoint = pipeline.Pipeline.checkpoint

    def test_five_chunk_checkpoint_continues_past_60000_and_encodes_next_chunk(self):
        chunks = [' '.join(f'Component{i:04}' for i in range(start, start+30)).ljust(4000, 'x') for start in range(0, 180, 30)]
        source = ''.join(chunks)+'마지막 구간'; doc = self.new_doc(source)
        deltas = [rows(i*30, 30, chunk) for i, chunk in enumerate(chunks)]
        failed, _ = self.run_job(doc, deltas[:5]+[RuntimeError('old context cap')])
        checkpoint = json.loads(self.checkpoint(failed).read_text())
        self.assertEqual(checkpoint['offset'], 20000)
        checkpoint['contract']['prompt'] = PREVIOUS_CONTEXT_PROMPT
        self.checkpoint(failed).write_text(json.dumps(checkpoint))
        retried, calls = self.run_job(doc, [deltas[5], EMPTY])
        self.assertEqual(retried['status'], 'success', retried['detail'])
        self.assertEqual([c['offset'] for c in calls], [20000, 24000])
        value = next(i['payload'] for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(value['entities'], [r for d in deltas for r in d['entities']])
        self.assertGreater(len(canonical(dictionary_context(value)).encode()), 60000)
        self.assertEqual(restored(calls[1]['dictionary']), dictionary_context(value))
        report = json.loads((self.root/'runs'/retried['id']/'7/context-budget.json').read_text())
        self.assertEqual(report['encoding'], 'dictionary-wire-v1')
        self.assertLessEqual(report['transmitted_definition_bytes'], 60000)
        self.assertEqual(self.store.source_text(doc['id'], 1), source)

    def test_threshold_below_every_form_resumes_from_checkpoint_and_dispatches_remaining_chunk(self):
        source = ' '.join(f'Component{i:04}' for i in range(30)).ljust(4000, 'x')+'next'
        doc = self.new_doc(source)
        failed, _ = self.run_job(doc, [rows(0, 30, source[:4000]), RuntimeError('stop')])
        before = json.loads(self.checkpoint(failed).read_text())
        self.store.save_settings({'selected': 'agy', 'models': {}, 'dictionary_context_bytes': 1})
        retry, calls = self.run_job(doc, [EMPTY])
        self.assertEqual(retry['status'], 'success', retry['detail'])
        self.assertEqual([c['offset'] for c in calls], [before['offset']])
        value = next(i['payload'] for i in self.store.document(doc['id'])['items'] if i['kind'] == 'ontology')
        self.assertEqual(value['entities'], before['dictionary']['entities'])
        self.assertEqual(restored(calls[0]['dictionary']), dictionary_context(value))
        reports = list((self.root/'runs'/retry['id']).glob('*/context-budget.json'))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report['status'], 'over_context_limit'); self.assertGreater(report['transmitted_definition_bytes'], 1)

    def test_whole_request_limit_still_refuses_before_dispatch_and_preserves_prefix(self):
        source = ' '.join(f'Component{i:04}' for i in range(30)).ljust(4000, 'x')+'next'
        doc = self.new_doc(source)
        failed, _ = self.run_job(doc, [rows(0, 30, source[:4000]), RuntimeError('stop')])
        before = json.loads(self.checkpoint(failed).read_text())
        class Small:
            def prompt_byte_limit(self, agent): return 100
            def complete(self, *args, **kwargs): raise AssertionError('must not dispatch')
        service = Service(self.store, self.root, Small()); self.addCleanup(service.close)
        run = service.start(doc['id'], 'agy', '', 'ontology', doc['version'], doc['generation'])
        service.pool.shutdown(wait=True); retry = self.store.run(run['id'])
        self.assertEqual(retry['status'], 'error'); self.assertIn('전체 요청', retry['detail']); self.assertNotIn('설정', retry['detail'])
        after = json.loads(self.checkpoint(retry).read_text())
        self.assertEqual(after['offset'], before['offset']); self.assertEqual(after['dictionary'], before['dictionary'])

    def test_lower_adapter_limit_is_enforced_before_dispatch(self):
        class Fake:
            def prompt_byte_limit(self, agent): return 100
            def complete(self, *args, **kwargs): raise AssertionError('must not dispatch')
        service = Service(self.store, self.root, Fake()); self.addCleanup(service.close)
        run = service.start(self.doc['id'], 'copilot', 'auto', 'ontology', 1, 1)
        service.pool.shutdown(wait=True)
        result = self.store.run(run['id'])
        self.assertEqual(result['status'], 'error'); self.assertIn('전체 요청', result['detail'])
        report = json.loads((self.root/'runs'/run['id']/'1/context-budget.json').read_text())
        self.assertEqual(report['effective_prompt_limit_bytes'], 100)
        self.assertEqual(json.loads((self.root/'runs'/run['id']/'dictionary-progress.json').read_text())['calls_started'], 0)

    def test_large_approved_dictionary_extracts_and_repairs_without_dropping_definitions(self):
        source = ' '.join(f'Component{i:04}' for i in range(240))
        doc = self.new_doc(source); dictionary = rows(0, 240, source)
        approved_dictionary(self.store, doc['id'], dictionary)
        seen = []; output = extraction_response(facts=[], dictionary=dictionary,
                 page={'title': 'Large', 'claims': [{'heading': '항목', 'text': '항목들이 있다.', 'quote': 'absent'}]})
        class Fake:
            def complete(adapter, agent, model, prompt, folder, **kwargs):
                raw = payload(prompt); seen.append(raw)
                self.assertEqual(restored(raw['approved_dictionary']['payload']), dictionary_context(dictionary))
                return {'repairs': [{'path': 'claims.0.quote', 'quote': source}]} if len(seen) == 2 else copy.deepcopy(output)
        service = Service(self.store, self.root, Fake()); self.addCleanup(service.close)
        run = service.start(doc['id'], 'copilot', 'auto', 'extract', doc['version'], doc['generation'], extraction_contract=2)
        service.pool.shutdown(wait=True); saved = self.store.run(run['id'])
        self.assertEqual(saved['status'], 'success', saved['detail']); self.assertEqual(len(seen), 2)
        self.assertEqual(saved['ontology']['payload'], dictionary)
        self.assertEqual(seen[1]['issues'][0]['item']['quote'], 'absent')

    def test_every_call_site_sends_and_reports_over_the_threshold_without_refusing(self):
        self.store.save_settings({'selected': 'copilot', 'models': {}, 'dictionary_context_bytes': 100})
        source = ' '.join(f'Component{i:04}' for i in range(40))
        doc = self.new_doc(source); dictionary = rows(0, 40, source)
        approved_dictionary(self.store, doc['id'], dictionary)
        seen = []; output = extraction_response(facts=[], dictionary=dictionary,
                 page={'title': 'Large', 'claims': [{'heading': '항목', 'text': '항목들이 있다.', 'quote': 'absent'}]})
        class Fake:
            def complete(adapter, agent, model, prompt, folder, **kwargs):
                raw = payload(prompt); seen.append(raw)
                self.assertEqual(restored(raw['approved_dictionary']['payload']), dictionary_context(dictionary))
                return {'repairs': [{'path': 'claims.0.quote', 'quote': source}]} if len(seen) == 2 else copy.deepcopy(output)
        service = Service(self.store, self.root, Fake()); self.addCleanup(service.close)
        run = service.start(doc['id'], 'copilot', 'auto', 'extract', doc['version'], doc['generation'], extraction_contract=2)
        service.pool.shutdown(wait=True); saved = self.store.run(run['id'])
        self.assertEqual(saved['status'], 'success', saved['detail']); self.assertEqual(len(seen), 2)
        folder = self.root/'runs'/run['id']
        chunk_reports = list(folder.glob('*/context-budget.json'))
        self.assertEqual(len(chunk_reports), 1)
        repair_reports = list(folder.glob('*/quote-repair/context-budget.json'))
        self.assertEqual(len(repair_reports), 1)
        for path in chunk_reports+repair_reports:
            with self.subTest(path=str(path.relative_to(folder))):
                report = json.loads(path.read_text())
                self.assertEqual((report['context_limit_bytes'], report['status']), (100, 'over_context_limit'))
                self.assertGreater(report['transmitted_definition_bytes'], 100)

    def test_queued_setting_is_persisted_and_not_reread_by_worker(self):
        self.store.save_settings({'selected': 'copilot', 'models': {}, 'dictionary_context_bytes': 2000})
        class Queue:
            tasks = []
            def submit(self, fn, *args): self.tasks.append((fn, args))
            def shutdown(self, **kwargs): pass
        class Fake:
            def complete(self, *args, **kwargs): return copy.deepcopy(EMPTY)
        service = Service(self.store, self.root, Fake()); service.pool.shutdown(); queue = Queue(); service.pool = queue
        self.addCleanup(service.close)
        run = service.start(self.doc['id'], 'copilot', 'auto', 'ontology', 1, 1)
        self.store.save_settings({'selected': 'copilot', 'models': {}, 'dictionary_context_bytes': 100000})
        for fn, args in queue.tasks: fn(*args)
        self.assertEqual(self.store.run(run['id'])['status'], 'success')
        folder = self.root/'runs'/run['id']
        self.assertEqual(json.loads((folder/'context-policy.json').read_text())['dictionary_context_bytes'], 2000)
        self.assertEqual(json.loads((folder/'1/context-budget.json').read_text())['context_limit_bytes'], 2000)


class Settings(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_default_persistence_old_clients_and_invalid_values(self):
        self.assertEqual(self.request('/api/settings')['dictionary']['context_bytes'], 60000)
        body = {'selected': 'copilot', 'models': {}}
        for number in (1, 30000, 90000, 120000):
            self.request('/api/settings', 'PUT', body | {'dictionary_context_bytes': number})
            self.assertEqual(self.request('/api/settings')['dictionary']['context_bytes'], number)
            self.request('/api/settings', 'PUT', body)
            self.assertEqual(self.store.settings()['dictionary_context_bytes'], number)
        for bad in (True, 0, -1, '90000', 120001, 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(HTTPError) as exc:
                self.request('/api/settings', 'PUT', body | {'dictionary_context_bytes': bad})
            self.assertEqual(exc.exception.code, 400); exc.exception.close()
