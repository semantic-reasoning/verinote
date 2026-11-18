import json
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.error import HTTPError

import test_workflow as workflow


class LibrarySearch(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def add(self, title, text):
        return self.store.register(title, 'a.md', text.encode(), text)

    def test_search_filters_in_one_listing_without_source_text(self):
        a = self.add('Alpha', 'Gamma 서비스 운영')
        self.add('Beta', '다른 내용')
        self.assertTrue(len(self.store.documents()) >= 2)
        found = self.store.search_documents('gamma')
        self.assertEqual([d['id'] for d in found], [a['id']])
        self.assertNotIn('source_text', found[0])
        self.assertNotIn('source_text', self.store.documents()[0])
        self.assertEqual(self.store.search_documents('x' * 20000), [])
        self.assertEqual([d['id'] for d in self.store.search_documents('ALPHA')], [a['id']])

    def test_spacing_variants_are_not_merged(self):
        self.add('Doc', '서비스 운영')
        self.assertEqual(len(self.store.search_documents('서비스 운영')), 1)
        self.assertEqual(self.store.search_documents('서비스운영'), [])

    def test_search_documents_returns_newest_first(self):
        first = self.add('Doc one', 'shared')
        second = self.add('Doc two', 'shared')
        found = self.store.search_documents('shared')
        self.assertEqual({d['id'] for d in found}, {first['id'], second['id']})
        self.assertEqual([d['created_at'] for d in found], sorted((d['created_at'] for d in found), reverse=True))


class LibrarySearchHTTP(unittest.TestCase):
    setUp = workflow.HTTP.setUp
    tearDown = workflow.HTTP.tearDown
    request = workflow.HTTP.request

    def test_long_query_and_validation(self):
        self.request('/api/documents', 'POST', {'title': 'T', 'filename': 'a.md', 'text': '서비스 운영'})
        self.assertEqual(len(self.request('/api/documents/search', 'POST', {'search': '서비스 운영'})), 1)
        self.assertEqual(self.request('/api/documents/search', 'POST', {'search': '가' * 8000}), [])
        with self.assertRaises(HTTPError):
            self.request('/api/documents/search', 'POST', {'search': 3})


@unittest.skipUnless(shutil.which('node'), 'node is required')
class LibrarySearchClient(unittest.TestCase):
    def test_one_request_per_search_input_for_many_documents(self):
        js = (Path(__file__).parents[1] / 'verinote/web/app.js').read_text()
        start = js.index('async function renderDocs')
        fn = js[start:js.index('\n}\n', start) + 2]
        script = """
const calls = [];
const el = {value: ' Gamma ', innerHTML: ''};
const $ = () => el;
const docs = Array.from({length: 25}, (_, i) => ({id: 'd' + i, title: 't', filename: 'f', current_version: 1, created_at: '2024-01-01T00:00:00Z'}));
const api = async (path, method, data) => { calls.push([path, method, data]); return docs.slice(0, 1); };
const esc = s => s, date = s => s;
""" + fn + """
(async () => { await renderDocs(); console.log(JSON.stringify(calls)); })();
"""
        out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [['/api/documents/search', 'POST', {'search': 'gamma'}]])


if __name__ == '__main__':
    unittest.main()
