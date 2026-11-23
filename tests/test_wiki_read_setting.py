"""The read_wiki chunk size comes from the agents setting, is frozen in the question job, and is what the prompt says (#940)."""
import ast
import json
import subprocess
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import test_assistant as assistant_tests
import test_workflow as workflow
from test_assistant import Fake, answer, batch, tool
from test_workflow import SOURCE

from verinote.assistant import MAX_CONTEXT, Assistant, prompt
from verinote.service import Service
from verinote.wiki_read import (
    MAX_WIKI_READ_CHARS,
    MIN_WIKI_READ_CHARS,
    WIKI_READ_CHARS,
    validate_wiki_read_chars,
)

ROOT = Path(__file__).resolve().parents[1]
CLAIM = {'heading': '구조', 'text': '설명' * 700, 'quote': SOURCE}


def said(n):
    return f'최대{n}자씩'


def state(prompt_text):
    return json.loads(prompt_text.split('입력 상태(JSON):\n', 1)[1])


class WikiReadQuestions(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    wiki = assistant_tests.WikiQuestionTests.wiki
    lookup = assistant_tests.WikiQuestionTests.lookup
    ask = assistant_tests.AssistantTests.ask

    def save(self, chars):
        self.store.save_settings({'selected': 'agy', 'models': {}, 'wiki_read_chars': chars})

    def long_page(self):
        """A page longer than MAX_WIKI_READ_CHARS, so every allowed width cuts it."""
        claim_chars = len(Assistant.wiki_text({'payload': {'claims': [CLAIM]}})[0])
        page = self.wiki(claims=[CLAIM] * (MAX_WIKI_READ_CHARS // claim_chars + 2))
        return page

    def read_flow(self, page, check=None):
        def capture(prompt_text):
            if check: check(prompt_text)
            return answer(kind='wiki', ids=['E2'])
        return [batch(tool(), self.lookup()), tool(name='read_wiki', page_id=page['id']), capture]

    def test_new_question_reads_and_describes_the_saved_size(self):
        v = MIN_WIKI_READ_CHARS
        self.assertNotEqual(v, WIKI_READ_CHARS)
        self.save(v)
        page = self.long_page()
        job, fake = self.ask(self.read_flow(page))
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(job['wiki_read_chars'], v)
        e2 = job['evidence']['E2']
        self.assertGreater(e2['total_chars'], MAX_WIKI_READ_CHARS)
        self.assertEqual((e2['end_offset'] - e2['offset'], e2['next_offset']), (v, v))
        self.assertEqual(len(fake.calls), 3)
        for _, _, prompt_text in fake.calls:
            self.assertEqual(prompt_text.count(said(v)), 1)
            self.assertNotIn(said(WIKI_READ_CHARS), prompt_text)

    def test_settings_are_read_once_by_ask_and_never_by_the_worker(self):
        self.save(MIN_WIKI_READ_CHARS)
        page = self.long_page()
        service = Service(self.store, self.root, Fake(self.read_flow(page)))
        with patch.object(self.store, 'settings', wraps=self.store.settings) as spy:
            job = service.ask({'question': '주문에서 원장까지 경로는?'})
            at_submit = spy.call_count
            service.pool.shutdown(wait=True)
            total = spy.call_count
        self.assertEqual(self.store.question(job['id'])['status'], 'success')
        self.assertEqual((at_submit, total), (1, 1))

    def test_setting_changed_mid_question_does_not_change_that_question(self):
        v, v2 = MIN_WIKI_READ_CHARS, MAX_WIKI_READ_CHARS
        self.save(v)
        page = self.long_page()
        def change_then_read(prompt_text):
            self.save(v2)
            return tool(name='read_wiki', page_id=page['id'])
        job, fake = self.ask([batch(tool(), self.lookup()), change_then_read, lambda p: answer(kind='wiki', ids=['E2'])])
        self.assertEqual(job['status'], 'success', job['error'])
        self.assertEqual(self.store.settings()['wiki_read_chars'], v2)  # the change landed before the read ran
        self.assertEqual(job['wiki_read_chars'], v)
        e2 = job['evidence']['E2']
        self.assertEqual(e2['end_offset'] - e2['offset'], v)
        self.assertIn(said(v), fake.calls[2][2])
        self.assertNotIn(said(v2), fake.calls[2][2])

    def test_assistant_uses_job_value_not_store_setting(self):
        self.save(MAX_WIKI_READ_CHARS)
        page = self.long_page()
        snapshot = self.store.snapshot()
        for job, width in [({'wiki_read_chars': MIN_WIKI_READ_CHARS}, MIN_WIKI_READ_CHARS), ({}, WIKI_READ_CHARS)]:
            with self.subTest(width=width):
                a = Assistant(self.store, None, snapshot, job, self.root, lambda *args: None)
                a.execute({'name': 'read_wiki', 'arguments': {'page_id': page['id']}})
                e = a.evidence[a.trace[-1]['evidence_id']]
                self.assertEqual(e['end_offset'] - e['offset'], width)
                self.assertEqual(a.prompt, prompt(width))
        for bad in (True, MIN_WIKI_READ_CHARS - 0.5, float(WIKI_READ_CHARS), MAX_WIKI_READ_CHARS + 1, MIN_WIKI_READ_CHARS - 1, str(WIKI_READ_CHARS), None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Assistant(self.store, None, snapshot, {'wiki_read_chars': bad}, self.root, lambda *args: None)

    def test_split_path_receives_the_derived_prompt(self):
        v = MIN_WIKI_READ_CHARS
        self.save(v)
        with patch('verinote.assistant.MAX_CONTEXT', 1000), \
             patch('verinote.assistant.question_synthesis.run', side_effect=ValueError('split stopped')) as run:
            job, fake = self.ask([])
        self.assertEqual(job['status'], 'error')
        self.assertEqual(fake.calls, [])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[2], prompt(v))

    def test_max_read_of_hangul_in_a_minimal_question_is_delivered_unsplit(self):
        """Measures one fixture: after one read_wiki of MAX_WIKI_READ_CHARS mostly-Hangul characters, the next round of a
        minimal question is one unsplit call within MAX_CONTEXT. It does not cover larger catalogs or traces."""
        self.save(MAX_WIKI_READ_CHARS)
        page = self.long_page()
        job, fake = self.ask(self.read_flow(page))
        self.assertEqual(job['status'], 'success', job['error'])
        e2 = job['evidence']['E2']
        self.assertEqual(e2['end_offset'] - e2['offset'], MAX_WIKI_READ_CHARS)
        self.assertGreaterEqual(len(e2['text'].encode()), 2.9 * len(e2['text']))  # the read text is mostly 3-byte Hangul
        paths = [c['path'] for c in job['call_manifest']]
        self.assertIn('llm-3', paths)
        self.assertFalse([p for p in paths if p.startswith('context-')], paths)
        self.assertLessEqual(len(fake.calls[2][2].encode()), MAX_CONTEXT)
        round3 = [r for r in job['context_budget'] if r['round'] == 3]
        self.assertTrue(round3 and all(r['prompt_bytes'] <= MAX_CONTEXT for r in round3), round3)


class WikiReadPrompt(unittest.TestCase):
    def test_default_prompt_has_the_digest_of_the_constant_it_replaced(self):
        # SHA-256 of the UTF-8 bytes of verinote.assistant.PROMPT at commit 64924bc.
        self.assertEqual(sha256(prompt(WIKI_READ_CHARS).encode()).hexdigest(), '813623729c5366544357a6d2c0ef4d333f270c110058baacfc0bf8d4d940532c')

    def test_default_prompt_is_byte_identical_to_the_constant_it_replaced(self):
        source = subprocess.run(['git', 'show', '64924bc:verinote/assistant.py'], cwd=ROOT, capture_output=True, text=True)
        if source.returncode:
            self.skipTest('commit 64924bc is not in this checkout (shallow clone); the digest test above still pins the bytes')
        tree = ast.parse(source.stdout)
        old = next(node.value.value for node in tree.body if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'PROMPT')
        self.assertEqual(prompt(WIKI_READ_CHARS).encode(), old.encode())

    def test_prompt_template_has_one_derived_number(self):
        sizes = (MIN_WIKI_READ_CHARS, WIKI_READ_CHARS, MAX_WIKI_READ_CHARS)
        for n in sizes:
            self.assertEqual(prompt(n).count(said(n)), 1)
            self.assertEqual(prompt(n).count('read_document: document_id, offset(기본0), fallback 필수. 고정 버전 원문 최대2000자.'), 1)
        self.assertEqual(len({prompt(n).replace(said(n), '') for n in sizes}), 1)
        for bad in (True, MIN_WIKI_READ_CHARS - 1, MAX_WIKI_READ_CHARS + 1, float(WIKI_READ_CHARS)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prompt(bad)

    def test_validator_bounds(self):
        for n in (MIN_WIKI_READ_CHARS, WIKI_READ_CHARS, MAX_WIKI_READ_CHARS):
            self.assertEqual(validate_wiki_read_chars(n), n)
        self.assertLessEqual(MIN_WIKI_READ_CHARS, WIKI_READ_CHARS)
        self.assertLessEqual(WIKI_READ_CHARS, MAX_WIKI_READ_CHARS)


class WikiReadSettings(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request
    body = {'selected': 'copilot', 'models': {}}

    def test_settings_put_get_persist_and_validate(self):
        expected = {'chars': WIKI_READ_CHARS, 'min': MIN_WIKI_READ_CHARS, 'max': MAX_WIKI_READ_CHARS, 'default': WIKI_READ_CHARS}
        self.assertEqual(self.request('/api/settings')['wiki_read'], expected)
        for n in (MIN_WIKI_READ_CHARS, WIKI_READ_CHARS, MAX_WIKI_READ_CHARS):
            self.request('/api/settings', 'PUT', self.body | {'wiki_read_chars': n})
            self.assertEqual(self.request('/api/settings')['wiki_read']['chars'], n)
            self.request('/api/settings', 'PUT', self.body)
            self.assertEqual(self.store.settings()['wiki_read_chars'], n)
        before = self.store.settings()
        for bad in (True, False, MIN_WIKI_READ_CHARS - 1, MAX_WIKI_READ_CHARS + 1, 0, -1, float(WIKI_READ_CHARS), 1.5,
                    str(WIKI_READ_CHARS), None, [], {}):
            with self.subTest(bad=bad):
                for data in (self.body | {'wiki_read_chars': bad}, self.body | {'dictionary_context_bytes': 4567, 'wiki_read_chars': bad}):
                    with self.assertRaises(HTTPError) as caught:
                        self.request('/api/settings', 'PUT', data)
                    self.assertEqual(caught.exception.code, 400); caught.exception.close()
                    self.assertEqual(self.store.settings(), before)
                with self.assertRaises(ValueError):
                    self.store.save_settings(self.body | {'wiki_read_chars': bad})
                self.assertEqual(self.store.settings(), before)

    def test_save_settings_keeps_omitted_wiki_read_chars(self):
        v = MIN_WIKI_READ_CHARS
        self.store.save_settings(self.body | {'wiki_read_chars': v})
        self.store.save_settings(self.body)
        self.assertEqual(self.store.settings()['wiki_read_chars'], v)
        self.store.save_settings(self.body | {'dictionary_context_bytes': 1234})
        self.assertEqual(self.store.settings()['wiki_read_chars'], v)
        self.store.save_settings(self.body | {'copilot_extraction_concurrency': 2})
        self.assertEqual((self.store.settings()['wiki_read_chars'], self.store.settings()['dictionary_context_bytes']), (v, 1234))

    def test_unsaved_setting_is_not_written(self):
        self.store.save_settings(self.body)
        self.assertNotIn('wiki_read_chars', self.store.settings())


if __name__ == '__main__':
    unittest.main()
